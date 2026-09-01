"""
Fill confirmation tracker — shows real fill data after each order.

Two paths:
- INSTANT (non-sports): fill data from the order response (making/taking amount)
- DELAYED (football/sports): fill data from the user stream MATCHED trade event

The SDK's user stream is async only, while the rest of the bot (hotkeys,
orders, printing) is synchronous. To hold them together the subscription runs
in a dedicated thread with its own event loop: the fill callbacks then re-enter
the synchronous world simply by printing, as before.
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from colorama import Fore, Style
from polymarket import AsyncSecureClient
from polymarket.streams import UserSpec, UserTradeEvent

log = logging.getLogger(__name__)

PENDING_TIMEOUT = 4  # seconds — max wait for MATCHED on sports markets
RECONNECT_DELAY = 5  # seconds between reconnect attempts after a drop


@dataclass
class _PendingOrder:
    order_id: str
    label: str
    t_keypress: float
    t_post: float


class FillTracker:
    """Tracks order fills and prints confirmation reports to terminal."""

    def __init__(self, private_key: str, wallet_address: str):
        self._private_key = private_key
        # Must be the same wallet the Trader operates on, otherwise the
        # stream listens on a different account and fills never arrive.
        self._wallet_address = wallet_address

        self._pending: dict[str, _PendingOrder] = {}
        self._pending_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._cleanup_thread: Optional[threading.Thread] = None

        self._portfolio_display = None
        self._football_context = None  # (football_markets, team1_name, team2_name)

    def set_portfolio_display(self, display) -> None:
        """Set the portfolio report printed after each confirmed fill."""
        self._portfolio_display = display

    def set_football_context(self, football_markets, team1_name: str, team2_name: str) -> None:
        """Update the football market context for portfolio display."""
        self._football_context = (football_markets, team1_name, team2_name)

    # ── public API ──────────────────────────────────────────────

    def start(self) -> None:
        """Start the user stream and background threads."""
        self._stop_event.clear()

        self._ws_thread = threading.Thread(
            target=self._run_stream_loop,
            daemon=True,
            name="fill-stream",
        )
        self._ws_thread.start()

        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            daemon=True,
            name="fill-cleanup",
        )
        self._cleanup_thread.start()

        log.debug("FillTracker started")

    def stop(self) -> None:
        """Stop the user stream and background threads."""
        self._stop_event.set()
        loop = self._loop
        if loop is not None:
            # Wake the loop from the main thread: without this it would stay
            # blocked on the event iteration until the next message.
            loop.call_soon_threadsafe(lambda: None)
        log.debug("FillTracker stopped")

    def report_instant(
        self,
        label: str,
        making_amount,
        taking_amount,
        is_buy: bool,
        t_keypress: float,
        t_post: float,
    ) -> None:
        """PATH A — immediate fill report from the order response."""
        making = float(making_amount)
        taking = float(taking_amount)

        if making == 0 or taking == 0:
            return

        if is_buy:
            # making = collateral spent, taking = shares received
            price = making / taking
            fill_usd = making
            fill_shares = taking
        else:
            # making = shares sold, taking = collateral received
            price = taking / making
            fill_usd = taking
            fill_shares = making

        self._print_report(
            path="INSTANT",
            label=label,
            price=price,
            fill_usd=fill_usd,
            fill_shares=fill_shares,
            is_buy=is_buy,
            t_keypress=t_keypress,
            t_post=t_post,
            t_matched=None,
        )

    def register_delayed(
        self,
        order_id: str,
        label: str,
        t_keypress: float,
        t_post: float,
    ) -> None:
        """PATH B — register pending order, wait for the MATCHED trade event."""
        if not order_id:
            return
        with self._pending_lock:
            self._pending[order_id] = _PendingOrder(
                order_id=order_id,
                label=label,
                t_keypress=t_keypress,
                t_post=t_post,
            )
        log.debug("Registered delayed order %s", order_id)

    # ── user stream ─────────────────────────────────────────────

    def _run_stream_loop(self) -> None:
        """Thread entry point: owns an event loop for the whole session."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._stream_forever())
        except Exception as e:
            log.debug("Fill stream loop ended: %s", e)
        finally:
            try:
                loop.close()
            finally:
                self._loop = None

    async def _stream_forever(self) -> None:
        """Keep the user subscription alive, reconnecting after drops."""
        while not self._stop_event.is_set():
            try:
                async with await AsyncSecureClient.create(
                    private_key=self._private_key,
                    wallet=self._wallet_address,
                ) as client:
                    handle = await client.subscribe(UserSpec())
                    log.debug("FillTracker user stream connected")
                    try:
                        async for event in handle:
                            if self._stop_event.is_set():
                                break
                            self._handle_event(event)
                    finally:
                        await handle.close()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("Fill stream error: %s", e)

            if self._stop_event.is_set():
                break
            # Reconnect after a drop: without this a single network blip
            # would silently kill every sports fill report.
            await asyncio.sleep(RECONNECT_DELAY)

    def _handle_event(self, event) -> None:
        """Handle one stream event; only MATCHED trades are of interest."""
        if not isinstance(event, UserTradeEvent):
            return

        trade = event.payload
        if str(trade.status).upper() != "MATCHED":
            return

        order_id = trade.taker_order_id or ""
        if not order_id:
            return

        with self._pending_lock:
            pending = self._pending.pop(order_id, None)

        if pending is None:
            return

        t_matched = time.time()
        price = float(trade.price)
        size = float(trade.size)

        self._print_report(
            path="DELAYED",
            label=pending.label,
            price=price,
            fill_usd=price * size,
            fill_shares=size,
            is_buy=pending.label.startswith("BUY"),
            t_keypress=pending.t_keypress,
            t_post=pending.t_post,
            t_matched=t_matched,
        )

    # ── background loops ────────────────────────────────────────

    def _cleanup_loop(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(2)
            if self._stop_event.is_set():
                break
            now = time.time()
            with self._pending_lock:
                expired = [
                    oid for oid, p in self._pending.items()
                    if now - p.t_post > PENDING_TIMEOUT
                ]
                for oid in expired:
                    p = self._pending.pop(oid)
                    # At WARNING (not debug): the order was accepted by the
                    # server and may well have executed, but the stream did not
                    # deliver the MATCHED confirmation in time. The user needs
                    # to know, so they can check balance/positions by hand
                    # instead of being left with no feedback on a real order.
                    log.warning(
                        "%s: no fill confirmation received within %.1fs (order %s) "
                        "- check balance/positions manually",
                        p.label, now - p.t_post, oid,
                    )

    # ── output ──────────────────────────────────────────────────

    def _print_report(
        self,
        path: str,
        label: str,
        price: float,
        fill_usd: float,
        fill_shares: float,
        is_buy: bool,
        t_keypress: float,
        t_post: float,
        t_matched: Optional[float],
    ) -> None:
        send_ms = (t_post - t_keypress) * 1000

        label_color = Fore.GREEN if is_buy else Fore.RED

        # "TEAM1" -> "TEAM 1", "TEAM2" -> "TEAM 2"
        display_label = label.replace("TEAM", "TEAM ")

        t_end = t_matched if t_matched is not None else t_post
        total_ms = (t_end - t_keypress) * 1000

        lines = [
            f"{Fore.CYAN}FILL CONFIRMED ({path}){Style.RESET_ALL}",
            f"  {label_color}{display_label} ${fill_usd:.2f}{Style.RESET_ALL}",
            f"  Fill price: ${price:.4f}/share",
            f"  Send:   {send_ms:.0f}ms",
        ]

        if t_matched is not None:
            delay_ms = (t_matched - t_post) * 1000
            lines.append(f"  Delay:  {delay_ms:.0f}ms")

        lines.append(
            f"  {Fore.CYAN}Total:  {total_ms:.0f}ms (keypress -> fill){Style.RESET_ALL}"
        )

        print("\n".join(lines))

        if self._portfolio_display and self._football_context:
            fm, t1, t2 = self._football_context
            # 3s wait: the book takes a moment to reflect its own fill, and a
            # P/L read too early would show the pre-trade price.
            timer = threading.Timer(3.0, self._portfolio_display.show, kwargs={
                "label": label,
                "football_markets": fm,
                "team1_name": t1,
                "team2_name": t2,
                "fill_price": price,
                "fill_shares": fill_shares,
                "is_buy": is_buy,
            })
            # Daemon: a still-pending timer must not keep the process alive
            # for 3s after a CTRL+Q pressed right after an order.
            timer.daemon = True
            timer.start()
