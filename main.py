"""
Main entry point for Polymarket Hotkey Trader.

Operates on one football match at a time: the three 1X2 markets (Team1 win,
Draw, Team2 win) resolved from a Polymarket event URL.

Features:
- Single customizable amount for all trades
- Change amount on the fly with CTRL+A
- Optional HTTP proxy via IPROYAL_PROXY in .env (routes the bot's own traffic)

Usage:
    python main.py

Requirements:
    - .env file with POLYMARKET_PRIVATE_KEY (the wallet is not configurable:
      the bot always uses the Deposit Wallet derived from the signer)
    - config.json with hotkey bindings
    - Administrator privileges on Windows for global hotkeys
"""

# ─── Bootstrap proxy (MUST run before any import that uses httpx/websocket) ───
# If IPROYAL_PROXY is set in .env, export it as the HTTPS_PROXY/HTTP_PROXY env
# vars. httpx (used by polymarket-client and market_info.py) picks it up
# automatically at init time thanks to trust_env=True (the default), and
# websockets >=14 does the same for the fill stream (proxy=True by default).
import os
from pathlib import Path
from dotenv import load_dotenv

# The .env next to the script takes precedence; if it is missing we fall back
# to python-dotenv's standard search starting from the cwd. Loaded exactly once
# here: load_credentials() then reads only from os.getenv.
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    load_dotenv(_env_file)
else:
    load_dotenv()
_proxy = os.getenv("IPROYAL_PROXY", "").strip()
if _proxy:
    for _var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        os.environ[_var] = _proxy
    _host = _proxy.split("@", 1)[-1] if "@" in _proxy else _proxy
    print(f"[proxy] IPROYAL_PROXY active (exit via {_host})")
# ─────────────────────────────────────────────────────────────────────────────

import json
import logging
import sys
from functools import partial

from colorama import Fore, Style

from trader import Trader, OrderSide, OUTCOMES
from hotkey_manager import HotkeyManager
from market_info import MarketClient, GammaAPIError
from console_ui import ConsoleUI
from event_fetcher import fetch_football_markets_from_url
from portfolio_display import PortfolioDisplay

log = logging.getLogger(__name__)


def load_config(config_path: str = "config.json") -> dict:
    """
    Load configuration from JSON file.

    Falls back to sensible defaults if file doesn't exist.

    Args:
        config_path: Path to config file

    Returns:
        Configuration dictionary
    """
    default_config = {
        # Kept in sync with config.json and the README: without config.json
        # the documented hotkeys must still apply, not a second divergent set.
        "hotkeys": {
            "buy_team1": "ctrl+f1",
            "buy_draw": "ctrl+f2",
            "buy_team2": "ctrl+f3",
            "sell_team1": "ctrl+f4",
            "sell_draw": "ctrl+f5",
            "sell_team2": "ctrl+f6",
            "limit_team1": "ctrl+shift+f1",
            "limit_draw": "ctrl+shift+f2",
            "limit_team2": "ctrl+shift+f3",
            "set_amount": "ctrl+a",
            "check_balance": "ctrl+b",
            "change_markets": "ctrl+m",
            "quit": "ctrl+q"
        },
        "default_amount": 1.0,
        "cooldown_seconds": 0.5,
        # Used only by market_info.py: the SDK ships its own endpoints and
        # infers chain and signature type from the wallet address.
        "clob_host": "https://clob.polymarket.com",
        "gamma_host": "https://gamma-api.polymarket.com",
    }

    config_file = Path(config_path)
    if config_file.exists():
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                user_config = json.load(f)

            # Merge with defaults (user config overrides)
            for key, value in user_config.items():
                if isinstance(value, dict) and key in default_config and isinstance(default_config[key], dict):
                    default_config[key].update(value)
                else:
                    default_config[key] = value
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load config.json: {e}")
            print("Using default configuration.")

    return default_config


def load_credentials() -> dict:
    """
    Load credentials from .env file.

    Only the private key is needed: the wallet to trade on is not
    configurable, it is always the Deposit Wallet derived from the signer
    (see Trader). A leftover POLYMARKET_FUNDER_ADDRESS in .env is ignored.

    Returns:
        Dictionary with private_key

    Raises:
        ValueError if required credentials not found
    """
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")

    if not private_key:
        raise ValueError(
            "POLYMARKET_PRIVATE_KEY not found in .env file.\n"
            "Please copy .env.example to .env and add your private key."
        )

    if not private_key.startswith("0x"):
        raise ValueError("POLYMARKET_PRIVATE_KEY must start with 0x")

    return {
        "private_key": private_key,
    }


class _LoadWarningCollector(logging.Handler):
    """
    Captures the warnings emitted while an event is being loaded.

    Loading is always followed by _refresh_display(), which starts with a
    screen clear: anything logged during the fetch is wiped a moment later.
    That included the one warning that can cost real money — the outcomes
    assigned by response order, where [1] and [2] may be swapped — which the
    README tells you to act on before pressing BUY.

    Everything logged in that window is collected, not just this module's
    records: any warning raised then would be wiped just the same.
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

    def __enter__(self) -> "_LoadWarningCollector":
        logging.getLogger().addHandler(self)
        return self

    def __exit__(self, *exc_info) -> None:
        logging.getLogger().removeHandler(self)


class PolymarketHotkeyApp:
    """
    Main application class that orchestrates all components.

    Loads the three 1X2 markets of a match and keeps them refreshed, with a
    single amount, changeable on the fly, used for every order.
    """

    def __init__(self):
        """Initialize the application components."""
        self.config = load_config()
        creds = load_credentials()
        self.private_key = creds["private_key"]
        # Filled in after the Trader is created: it is the only component
        # that knows which Deposit Wallet the SDK resolved.
        self.wallet_address = ""

        # Current trading amount (single amount for all trades)
        self.current_amount = self.config.get("default_amount", 1.0)

        # Team names for display
        self.team1_name = "Team 1"
        self.team2_name = "Team 2"

        # Warnings raised by the last successful load, redrawn under the board
        # by _refresh_display() because the fetch output is cleared away.
        self.load_warnings: list[str] = []

        # Initialize UI first for error display
        self.ui = ConsoleUI()

        # Initialize market client (CLOB by condition_id, Gamma for the event)
        self.market_client = MarketClient(
            clob_host=self.config["clob_host"],
            gamma_host=self.config["gamma_host"]
        )

        # Initialize hotkey manager
        self.hotkey_manager = HotkeyManager()

        # Initialize trader (connects to CLOB)
        self.ui.print_info("Connecting to Polymarket CLOB...")
        self.trader = Trader(
            private_key=self.private_key,
            cooldown_seconds=self.config["cooldown_seconds"],
        )

        # Address to display: the one the client actually authenticated.
        self.wallet_address = self.trader.wallet_address

        # Portfolio display — printed by the Trader after every confirmed fill
        self.trader.set_portfolio_display(PortfolioDisplay(price_source=self.trader))

    def _load_football_markets(self, url: str) -> None:
        """
        Resolve an event URL into the 3 markets and attach them to the Trader.

        Raises ValueError / GammaAPIError if the URL or the event is unusable:
        callers catch those to let the user try again.
        """
        with _LoadWarningCollector() as collector:
            team1_m, draw_m, team2_m, name1, name2 = fetch_football_markets_from_url(
                url, self.market_client
            )
        # Only on success: a failed load leaves the previous match on screen,
        # and its warnings still describe what is actually loaded.
        self.load_warnings = collector.messages

        self.trader.set_football_markets(team1_m, draw_m, team2_m)
        self.team1_name = name1
        self.team2_name = name2
        self.trader.set_football_context(name1, name2)

    def _set_amount(self) -> None:
        """Handle set amount hotkey (CTRL+A)."""
        self.hotkey_manager.suspend_all()
        try:
            new_amount = self.ui.prompt_amount_input(self.current_amount)
            if new_amount is not None:
                self.current_amount = new_amount
                self.ui.print_amount_changed(new_amount)
        finally:
            self.hotkey_manager.resume_all()

    def _outcome_label(self, outcome: str) -> str:
        """Readable name of an outcome, using the loaded match's team names."""
        return {
            "team1": f"[1] {self.team1_name}",
            "draw": "[X] Draw",
            "team2": f"[2] {self.team2_name}",
        }[outcome]

    def _set_limit(self, outcome: str) -> None:
        """Handle max-buy-price hotkeys (CTRL+SHIFT+F1/F2/F3)."""
        self.hotkey_manager.suspend_all()
        try:
            label = self._outcome_label(outcome)
            new_limit = self.ui.prompt_limit_input(
                label, self.trader.get_max_buy_price(outcome),
            )
            if new_limit is not None:
                self.trader.set_max_buy_price(outcome, new_limit)
                self.ui.print_limit_changed(label, new_limit)
        finally:
            self.hotkey_manager.resume_all()

    def _check_balance(self) -> None:
        """Handle check balance hotkey (CTRL+B)."""
        balance, _ = self.trader.get_wallet_info()
        print(f"\n{Fore.CYAN}Balance: ${balance:.2f} pUSD{Style.RESET_ALL}\n")

    def _change_markets(self) -> None:
        """Handle market change hotkey (CTRL+M)."""
        self.hotkey_manager.suspend_all()
        try:
            self.ui.print_info("\n--- Changing Markets ---")

            url = self.ui.read_line(f"\n{Fore.CYAN}New event URL: {Style.RESET_ALL}")
            if not url:
                # None (CTRL+C) and "" (bare Enter) both mean "never mind".
                self.ui.print_info("Market change cancelled.")
                return

            try:
                self.ui.print_info("Fetching event data...")
                self._load_football_markets(url)
                # No confirmation message: _refresh_display() clears the
                # screen right away and redraws the match.
                self._refresh_display()
            except (ValueError, GammaAPIError) as e:
                self.ui.print_error(str(e))
        finally:
            self.hotkey_manager.resume_all()

    def _quit(self) -> None:
        """Handle quit hotkey (CTRL+Q)."""
        self.ui.print_info("\nShutting down...")
        # Unblocks the start() loop, which is what keeps the process alive.
        self.hotkey_manager.stop()

    def _refresh_display(self) -> None:
        """Refresh the main display with current state."""
        self.ui.clear_screen()
        self.ui.print_header()

        # Wallet info (single HTTP call)
        balance, _ = self.trader.get_wallet_info()
        self.ui.print_wallet_info(self.wallet_address, balance)

        # Hotkey guide (with current amount)
        self.ui.print_hotkey_guide(self.current_amount, self.config["hotkeys"])

        # Market info
        markets = self.trader.get_football_markets()
        if markets:
            self.trader.refresh_market_prices()
            self.ui.print_football_markets_info(
                markets.team1,
                markets.draw,
                markets.team2,
                self.team1_name,
                self.team2_name,
                limits=tuple(self.trader.get_max_buy_price(o) for o in OUTCOMES),
                default_limit=self.trader.MAX_BUY_PRICE,
            )
        else:
            self.ui.print_football_markets_info(None, None, None)

        # Warnings from the load, which the clear_screen() above would
        # otherwise have taken with it.
        self.ui.print_load_warnings(self.load_warnings)

        # Ready status
        self.ui.print_status_ready()

    def _periodic_refresh(self) -> None:
        """Periodic callback to refresh prices (called every 2 seconds)."""
        try:
            self.trader.refresh_market_prices()
        except Exception:
            pass  # Silently ignore refresh errors

    def _execute_order(self, side: OrderSide) -> None:
        """Shared callback for every buy and sell hotkey."""
        log.debug("HOTKEY: %s, amount=%s", side.name, self.current_amount)
        self.trader.execute_order(side, self.current_amount)

    def _register_hotkeys(self) -> None:
        """
        Register every hotkey declared in config.json.

        This table is the single place where an action is bound to its name:
        adding a hotkey means one line here and one in config.json.

        `partial` and not `lambda`: the argument has to be bound now, otherwise
        every callback would end up using the last value of the loop and each
        key would buy the same outcome.

        The limit combos use SHIFT on purpose: `keyboard` matches on the EXACT
        set of keys held down, so (ctrl, shift, f1) does not also fire
        (ctrl, f1). A ctrl+f1+m would send the buy order first.
        """
        actions = {
            "buy_team1":      partial(self._execute_order, OrderSide.BUY_TEAM1),
            "buy_draw":       partial(self._execute_order, OrderSide.BUY_DRAW),
            "buy_team2":      partial(self._execute_order, OrderSide.BUY_TEAM2),
            "sell_team1":     partial(self._execute_order, OrderSide.SELL_TEAM1),
            "sell_draw":      partial(self._execute_order, OrderSide.SELL_DRAW),
            "sell_team2":     partial(self._execute_order, OrderSide.SELL_TEAM2),
            "limit_team1":    partial(self._set_limit, "team1"),
            "limit_draw":     partial(self._set_limit, "draw"),
            "limit_team2":    partial(self._set_limit, "team2"),
            "set_amount":     self._set_amount,
            "check_balance":  self._check_balance,
            "change_markets": self._change_markets,
            "quit":           self._quit,
        }

        hotkeys = self.config["hotkeys"]
        for action_name, callback in actions.items():
            self.hotkey_manager.register_hotkey(
                action_name, hotkeys[action_name], callback,
            )

    def run(self) -> None:
        """
        Main application loop.

        Flow:
        1. Display header
        2. Show wallet info
        3. Load the 3 markets from an event URL
        4. Register hotkeys
        5. Enter trading loop
        6. Cleanup on exit
        """
        try:
            # Initial display
            self.ui.clear_screen()
            self.ui.print_header()

            # Get and display balance (single HTTP call)
            balance, _ = self.trader.get_wallet_info()
            self.ui.print_wallet_info(self.wallet_address, balance)

            self.ui.print_info("Paste a Polymarket event URL to auto-detect markets\n")
            while True:
                # flush=False: the hotkeys are not registered yet, so there
                # is nothing to clean up here, and draining the buffer would
                # only discard a URL pasted while the balance was loading.
                url = self.ui.read_line(
                    f"{Fore.CYAN}Event URL: {Style.RESET_ALL}", flush=False,
                )
                if url is None:
                    self.ui.print_info("Cancelled.")
                    return
                if not url:
                    continue
                try:
                    self.ui.print_info("Fetching event data...")
                    self._load_football_markets(url)
                    # No price summary here: _refresh_display() below clears
                    # the screen and reprints them as asks.
                    break
                except (ValueError, GammaAPIError) as e:
                    self.ui.print_error(str(e))
                    self.ui.print_info("Try again.\n")

            self._register_hotkeys()

            # Show full display
            self._refresh_display()

            # Set up periodic price refresh (every 2 seconds)
            self.hotkey_manager.set_refresh_callback(self._periodic_refresh, interval=2.0)

            # Main loop: blocks here until CTRL+Q arrives
            self.hotkey_manager.start()

        except KeyboardInterrupt:
            pass
        except Exception as e:
            self.ui.print_error(f"Unexpected error: {e}")
        finally:
            self.trader.shutdown()
            self.hotkey_manager.unregister_all()
            self.market_client.close()
            self.ui.print_goodbye()


def main():
    """Application entry point."""
    # INFO by default: at DEBUG the bot prints full order responses and third
    # party libraries turn very chatty, which clutters the console where the
    # fill reports are read. For troubleshooting:
    #   set POLYMARKET_LOG_LEVEL=DEBUG
    _level = os.getenv("POLYMARKET_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, _level, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)

    # Check Python version: this is the minimum declared by polymarket-client
    # (Requires-Python >=3.11). Below it the SDK import would fail anyway, but
    # with a far less readable error than this one.
    if sys.version_info < (3, 11):
        print("Error: Python 3.11 or higher is required.")
        sys.exit(1)

    # Check for admin on Windows (needed for global hotkeys)
    if os.name == 'nt':
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                print("Warning: Running without Administrator privileges.")
                print("Global hotkeys may not work. Consider running as Administrator.")
                print()
        except Exception:
            pass

    try:
        app = PolymarketHotkeyApp()
        app.run()
    except ValueError as e:
        print(f"\nConfiguration error: {e}")
        sys.exit(1)
    except ImportError as e:
        print(f"\nMissing dependency: {e}")
        print("Run: pip install -r requirements.txt")
        sys.exit(1)
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()