"""
Trading logic module for executing orders via Polymarket CLOB (CLOB V2).

This module handles:
- CLOB client initialization with L1/L2 authentication
- Market order execution via FAK (Fill-And-Kill)
- Balance and position management
- Thread-safe order execution with cooldown
- Three markets per match: Team1, Draw, Team2

Order Strategy:
- BUY: FAK with a per-outcome price cap (default MAX_BUY_PRICE = 0.99),
  settable at runtime; spends ~$amount
- SELL: FAK with min_price=0.01, sells all held shares
- FAK fills what's available immediately, cancels unfilled remainder

With a lowered cap the FAK only fills the part of the book below that price
and cancels the rest: if there is nothing below, it does not buy at all. It is
how you say "do not pay more than X for this outcome".

Uses `polymarket-client`, the official unified SDK. Compared to the previous
py-clob-client-v2, place_market_order() signs and posts the order and returns
the server response right away: there is no settlement-hash polling phase (up
to 30s), which that client has performed since the 2026-07-24 rollout and
which here would have held the lock and blocked the next hotkey.
"""

import logging
import math
import time
import traceback
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from threading import Lock

from polymarket import SecureClient
from polymarket.models.clob.order_response import AcceptedOrder
from polymarket.models.clob.requests import PriceRequest

import order_fastpath
from market_info import MarketData
from fill_tracker import FillTracker

log = logging.getLogger(__name__)

BUY = "BUY"
SELL = "SELL"

# The 1X2 outcomes, in display order. They double as the FootballMarkets
# field names and as the keys of the per-outcome buy caps.
OUTCOMES = ("team1", "draw", "team2")


class OrderSide(Enum):
    """Order side enumeration for buy/sell combinations."""
    BUY_TEAM1 = "buy_team1"
    BUY_DRAW = "buy_draw"
    BUY_TEAM2 = "buy_team2"
    SELL_TEAM1 = "sell_team1"
    SELL_DRAW = "sell_draw"
    SELL_TEAM2 = "sell_team2"


@dataclass
class FootballMarkets:
    """Container for 3 football markets (Team1, Draw, Team2)."""
    team1: MarketData
    draw: MarketData
    team2: MarketData


class Trader:
    """
    Main trading class for executing Polymarket orders.

    Thread-safe for use with hotkey callbacks. Uses FAK (Fill-And-Kill)
    orders with aggressive pricing to sweep the orderbook immediately.

    Operates on one match at a time: the three 1X2 markets (Team1, Draw,
    Team2), always buying and selling the YES token of each.

    Authentication:
    - L1: Private key signs orders (never sent to server)
    - L2: API credentials derived from private key for API auth
    """

    # Aggressive prices for market-like orders
    # These ensure orders fill completely at best available prices
    MAX_BUY_PRICE = 0.99   # Maximum price willing to pay (sweeps orderbook)
    MIN_SELL_PRICE = 0.01  # Minimum price willing to accept

    # Mapping: OrderSide -> (FootballMarkets attribute, clob_side)
    _SIDE_MAP = {
        OrderSide.BUY_TEAM1:  ("team1", BUY),
        OrderSide.SELL_TEAM1: ("team1", SELL),
        OrderSide.BUY_DRAW:   ("draw",  BUY),
        OrderSide.SELL_DRAW:  ("draw",  SELL),
        OrderSide.BUY_TEAM2:  ("team2", BUY),
        OrderSide.SELL_TEAM2: ("team2", SELL),
    }

    def __init__(
        self,
        private_key: str,
        cooldown_seconds: float = 0.5,
    ):
        """
        Initialize the Trader with the Polymarket client.

        The L2 credentials are not passed in: SecureClient.create() derives
        them from the private key. The CLOB endpoint and the contract addresses
        come from the SDK's production Environment, so they are no longer
        parameters.

        Args:
            private_key: Wallet private key (0x...)
            cooldown_seconds: Minimum time between orders (anti-double-click)

        The wallet is not a parameter: the bot always uses the signer's
        Deposit Wallet, which the SDK resolves on its own when `wallet` stays
        None (the legacy UUPS one if deployed, otherwise the beacon). It is
        the only one the CLOB accepts for programmatic orders — an EOA is
        rejected with "maker address not allowed, please use the deposit
        wallet flow" unless it is allowlisted, and the docs describe no
        procedure for that. Making it configurable would only buy you the
        ability to reproduce that error.
        """
        self._lock = Lock()
        self._last_order_time = 0.0
        self._cooldown = cooldown_seconds
        self._last_tick_refresh = 0.0

        # Before creating the client: from here on every order reads tick size
        # and neg_risk from the cache instead of doing two GETs per keypress.
        order_fastpath.install()

        # The three markets of the current match. None until one is loaded.
        self._football_markets: Optional[FootballMarkets] = None

        # Per-outcome buy cap ("team1"/"draw"/"team2"), settable at runtime.
        # An outcome missing here uses MAX_BUY_PRICE.
        self._max_buy_price: dict[str, float] = {}

        # No `wallet=`: the SDK resolves the signer's Deposit Wallet.
        self._client = SecureClient.create(
            private_key=private_key,
        )

        # The address the client actually authenticated (the Deposit Wallet
        # resolved just above). The FillTracker has to attach to this same
        # wallet, otherwise it would listen on a different account and never
        # see its own fills.
        self.wallet_address = str(self._client.wallet)
        self.wallet_type = str(self._client.wallet_type)
        log.info("Active wallet: %s (%s)", self.wallet_address, self.wallet_type)

        self._fill_tracker = FillTracker(
            private_key=private_key,
            wallet_address=self.wallet_address,
        )
        self._fill_tracker.start()

    def set_football_markets(self, team1: MarketData, draw: MarketData, team2: MarketData) -> None:
        """
        Set the three markets for the current match.

        Args:
            team1: Market for Team 1 winning
            draw: Market for Draw
            team2: Market for Team 2 winning
        """
        with self._lock:
            self._football_markets = FootballMarkets(team1=team1, draw=draw, team2=team2)
            # The caps go back to the default: slots 1/X/2 now point at
            # different teams, and an inherited limit would silently block buys
            # on an outcome it was never meant for.
            self._max_buy_price.clear()
        for market in (team1, draw, team2):
            order_fastpath.register_market(market)

    def set_portfolio_display(self, display) -> None:
        """
        Attach the portfolio report printed after every fill.

        It goes through the Trader because the Trader owns the FillTracker:
        that is where fills arrive, and the caller need not know it exists.
        """
        self._fill_tracker.set_portfolio_display(display)

    def set_football_context(self, team1_name: str, team2_name: str) -> None:
        """Team names for the portfolio report."""
        self._fill_tracker.set_football_context(
            self._football_markets, team1_name, team2_name,
        )

    def get_football_markets(self) -> Optional[FootballMarkets]:
        """Get the three markets of the current match (None if not loaded)."""
        return self._football_markets

    def set_max_buy_price(self, outcome: str, price: float) -> None:
        """
        Set the buy cap for a single outcome.

        The range is re-checked here and not only in the prompt: this is the
        last point before the value reaches a real order, and an out-of-scale
        cap (0, negative, or above the default) would silently change how much
        you are willing to pay.

        Args:
            outcome: "team1", "draw" or "team2"
            price: maximum price in dollars (0.01-0.99)
        """
        if outcome not in OUTCOMES:
            raise ValueError(f"Unknown outcome: {outcome}")
        if not 0 < price <= self.MAX_BUY_PRICE:
            raise ValueError(
                f"Cap out of range: {price} (allowed 0.01-{self.MAX_BUY_PRICE})"
            )
        with self._lock:
            self._max_buy_price[outcome] = price

    def get_max_buy_price(self, outcome: str) -> float:
        """Buy cap in force for an outcome (the default if never touched)."""
        return self._max_buy_price.get(outcome, self.MAX_BUY_PRICE)

    def get_wallet_info(self) -> tuple[float, float]:
        """
        Fetch collateral balance and allowance in a single HTTP call.

        Returns:
            Tuple of (balance_usd, allowance)
        """
        try:
            res = self._client.get_balance_allowance(asset_type="COLLATERAL")
            balance = res.balance / 1e6
            # allowances is a contract -> amount map: the bot only needs to
            # know whether there is headroom, so take the highest one.
            allowance = max(res.allowances.values()) / 1e6 if res.allowances else 0.0
            return balance, allowance
        except Exception as e:
            log.debug("get_wallet_info failed: %s", e)
            return 0.0, 0.0

    def get_position(self, token_id: str) -> float:
        """
        Get the number of shares owned for a specific token.

        Args:
            token_id: The token to check

        Returns:
            Number of shares owned (0 if none)
        """
        try:
            res = self._client.get_balance_allowance(
                asset_type="CONDITIONAL",
                token_id=token_id,
            )
            return res.balance / 1e6
        except Exception as e:
            log.debug("get_position failed: %s", e)
            return 0.0

    def get_bid_ask(self, token_ids: list[str]) -> dict[str, tuple[float, float]]:
        """
        Fetch (bid, ask) for several tokens in a single batched call.

        Mind the endpoint's semantics, verified against the book: side=BUY
        returns the best BID (the resting buy orders, i.e. what you collect by
        selling) and side=SELL the best ASK (what you pay to buy). It is not
        "the price for my operation": it is the side of the book.
        """
        if not token_ids:
            return {}
        try:
            requests = []
            for token_id in token_ids:
                requests.append(PriceRequest(token_id=token_id, side=BUY))
                requests.append(PriceRequest(token_id=token_id, side=SELL))
            raw = self._client.get_prices(requests=requests)
        except Exception as e:
            log.debug("get_bid_ask failed: %s", e)
            return {}

        out: dict[str, tuple[float, float]] = {}
        for token_id in token_ids:
            sides = raw.get(token_id) or {}
            try:
                bid = float(sides.get(BUY, 0) or 0)
                ask = float(sides.get(SELL, 0) or 0)
            except (TypeError, ValueError):
                continue
            out[token_id] = (bid, ask)
        return out

    def refresh_market_prices(self) -> None:
        """Refresh bid/ask for the three markets from CLOB (one batched call)."""
        if self._football_markets:
            markets = self._market_list()
            quotes = self.get_bid_ask([m.yes_token_id for m in markets])
            for market in markets:
                bid, ask = quotes.get(market.yes_token_id, (0.0, 0.0))
                if bid > 0:
                    market.yes_price = bid
                if ask > 0:
                    market.yes_ask = ask

        self._refresh_tick_sizes_if_due()

    def _market_list(self) -> list[MarketData]:
        """The three markets in 1-X-2 order, empty list if not loaded."""
        fm = self._football_markets
        if not fm:
            return []
        return [getattr(fm, outcome) for outcome in OUTCOMES]

    # The tick size rarely changes, so there is no need to realign it on every
    # turn of the 2s loop: 30s keeps the cache fresh without pointless traffic.
    TICK_REFRESH_INTERVAL = 30.0

    def _refresh_tick_sizes_if_due(self) -> None:
        """Realign the cached tick sizes, throttled. Never on the order path."""
        now = time.time()
        if now - self._last_tick_refresh < self.TICK_REFRESH_INTERVAL:
            return
        self._last_tick_refresh = now

        token_ids = [m.yes_token_id for m in self._market_list()]
        if not token_ids:
            return

        order_fastpath.refresh_tick_sizes(self._client, token_ids)

    def _resolve_order_params(self, side: OrderSide) -> tuple[str, str, str]:
        """
        Resolve order side to (token_id, clob_side, outcome).

        `outcome` is the 1X2 key ("team1"/"draw"/"team2"): it is used to read
        the limit price set for that single outcome.

        Raises ValueError if the markets aren't loaded yet.
        """
        outcome, clob_side = self._SIDE_MAP[side]

        if not self._football_markets:
            raise ValueError("Markets not loaded")

        market = getattr(self._football_markets, outcome)  # team1, draw, team2
        return market.yes_token_id, clob_side, outcome

    def _check_cooldown(self) -> tuple[bool, float]:
        """
        Check if cooldown period has elapsed.

        Returns:
            Tuple of (can_proceed, remaining_seconds)
        """
        current_time = time.time()
        elapsed = current_time - self._last_order_time
        remaining = self._cooldown - elapsed

        if remaining <= 0:
            return True, 0.0
        return False, remaining

    def execute_order(self, side: OrderSide, amount: float) -> bool:
        """
        Execute a market order via FAK with aggressive pricing.

        This is the main entry point called by hotkey handlers.

        Args:
            side: OrderSide enum
            amount: Amount in collateral to trade

        Returns:
            True if the order was accepted by the server, False otherwise
        """
        start_time = time.time()
        log.debug("execute_order called: side=%s, amount=%s", side, amount)

        with self._lock:
            # Check cooldown
            can_proceed, remaining = self._check_cooldown()
            if not can_proceed:
                # Visible: otherwise a hotkey pressed too fast simply looks
                # like it did nothing at all.
                log.info("Order ignored: cooldown active (%.1fs remaining)", remaining)
                return False

            # Resolve token, side, and outcome from OrderSide
            try:
                token_id, clob_side, outcome = self._resolve_order_params(side)
            except ValueError as e:
                log.error("Order not sent (%s): %s", side.name, e)
                return False

            is_buy = clob_side == BUY

            try:
                if is_buy:
                    # BUY: `amount` is the collateral spend, `max_price` the
                    # cap above which the order must not fill. It is
                    # per-outcome: if the whole book sits above the cap, the FAK
                    # is cancelled without executing, which is exactly the
                    # point.
                    max_price = self._max_buy_price.get(outcome, self.MAX_BUY_PRICE)
                    response = self._client.place_market_order(
                        token_id=token_id,
                        side=BUY,
                        amount=amount,
                        max_price=max_price,
                        order_type="FAK",
                    )
                else:
                    # SELL: sell every share held.
                    shares = self.get_position(token_id)
                    if shares <= 0:
                        log.warning("SELL %s: no shares to sell", side.name)
                        return False

                    # Floor to 2 decimals (API requirement). A leftover
                    # position under 0.01 shares (dust from a partial fill)
                    # rounds to zero: without this second check we would still
                    # send a SELL order for 0 shares.
                    shares = math.floor(shares * 100) / 100
                    if shares <= 0:
                        log.warning("SELL %s: position too small to sell (dust)", side.name)
                        return False

                    response = self._client.place_market_order(
                        token_id=token_id,
                        side=SELL,
                        shares=shares,
                        min_price=self.MIN_SELL_PRICE,
                        order_type="FAK",
                    )

                t_post = time.time()
                self._last_order_time = t_post
                execution_time = (t_post - start_time) * 1000

                # place_market_order returns either AcceptedOrder or
                # RejectedOrder: a rejection is a typed return value, not an
                # exception, so it has to be handled explicitly.
                if not isinstance(response, AcceptedOrder):
                    log.warning(
                        "Order REJECTED in %.0fms: %s - %s",
                        execution_time, response.code, response.message,
                    )
                    return False

                log.debug("Order ACCEPTED in %.0fms: %s", execution_time, response.status)

                # Fill confirmation report
                label = f"{'BUY' if is_buy else 'SELL'} {side.name.split('_', 1)[1]}"

                if response.status == "matched":
                    self._fill_tracker.report_instant(
                        label=label,
                        making_amount=response.making_amount,
                        taking_amount=response.taking_amount,
                        is_buy=is_buy,
                        t_keypress=start_time,
                        t_post=t_post,
                    )
                elif response.status == "delayed":
                    self._fill_tracker.register_delayed(
                        order_id=response.order_id,
                        label=label,
                        t_keypress=start_time,
                        t_post=t_post,
                    )

                return True

            except Exception as e:
                # ERROR, not debug: at this point the order was NOT placed
                # (network down, proxy unreachable, rejection...). Staying silent made the
                # hotkey look broken instead of surfacing the problem.
                log.error("ORDER FAILED (%s): %s", side.name, e)
                log.debug("Full traceback:\n%s", traceback.format_exc())
                return False

    def shutdown(self) -> None:
        """Release resources."""
        self._fill_tracker.stop()
        try:
            self._client.close()
        except Exception:
            pass
