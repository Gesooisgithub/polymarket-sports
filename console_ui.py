"""
Console UI module for status display and user interaction.

Uses colorama for cross-platform colored output.
Provides real-time feedback for trading operations.
"""

import os
import sys
import time
from typing import Optional

from colorama import init, Fore, Style

from market_info import MarketData

# Initialize colorama for Windows support
init()


def flush_console_input() -> None:
    """
    Throw away whatever is left in the console input buffer.

    The hotkeys are registered with suppress=False (on purpose: that way a
    stream in another window keeps receiving the keys), so when the bot's
    window has focus every combo ALSO lands in the console buffer as a control
    character: CTRL+A is \\x01, CTRL+B \\x02, CTRL+Q \\x11, and CTRL+M is
    actually \\r, i.e. an Enter.

    Those characters pile up for the whole session and resurface wherever they
    can: inside the first available input() ("$ ^A1", which is then not a valid
    number) and, once the process exits, in the shell that launched the bot,
    which finds itself running "^A^A^B" followed by the Enter of a CTRL+M.

    Code opening a prompt FROM a hotkey must not call this directly but
    ConsoleUI.read_line(): the key that just opened the prompt has not reached
    here yet, and draining now would not catch it.
    """
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            STD_INPUT_HANDLE = -10
            kernel32.FlushConsoleInputBuffer(kernel32.GetStdHandle(STD_INPUT_HANDLE))
        else:
            import termios

            termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        # stdin redirected or not a tty: there is no buffer to drain.
        pass


class ConsoleUI:
    """
    Console-based UI for displaying trading status.

    Provides:
    - Colored output for status visibility
    - Real-time order notifications
    - Status bar with current state
    - The 1X2 board of the loaded match
    """

    def clear_screen(self) -> None:
        """Clear the console screen."""
        os.system('cls' if os.name == 'nt' else 'clear')

    def print_header(self) -> None:
        """Print the application header."""
        print(f"\n{Fore.CYAN}{'=' * 60}{Style.RESET_ALL}")
        print(f"{Fore.CYAN}  POLYMARKET HOTKEY TRADER - FOOTBALL MODE{Style.RESET_ALL}")
        print(f"{Fore.CYAN}{'=' * 60}{Style.RESET_ALL}\n")

    def print_wallet_info(self, address: str, balance: float) -> None:
        """
        Print wallet information.

        Args:
            address: Wallet address (will be truncated)
            balance: pUSD balance
        """
        # Truncate address for display
        if len(address) > 12:
            display_addr = f"{address[:6]}...{address[-4:]}"
        else:
            display_addr = address

        print(f"{Fore.WHITE}Wallet:{Style.RESET_ALL} {display_addr}")
        print(f"{Fore.WHITE}Balance:{Style.RESET_ALL} ${balance:,.2f} pUSD")
        print()

    # Layout of the guide: one entry per section, as
    # (heading, blank line before it, rows), each row being
    # (colour, config.json action, fallback combo, label).
    _GUIDE_SECTIONS = (
        ("--- BUY ---", False, (
            (Fore.GREEN, "buy_team1", "ctrl+f1", "Buy TEAM 1"),
            (Fore.GREEN, "buy_draw", "ctrl+f2", "Buy DRAW"),
            (Fore.GREEN, "buy_team2", "ctrl+f3", "Buy TEAM 2"),
        )),
        ("--- SELL ---", False, (
            (Fore.MAGENTA, "sell_team1", "ctrl+f4", "Sell TEAM 1"),
            (Fore.MAGENTA, "sell_draw", "ctrl+f5", "Sell DRAW"),
            (Fore.MAGENTA, "sell_team2", "ctrl+f6", "Sell TEAM 2"),
        )),
        ("--- MAX BUY PRICE ---", True, (
            (Fore.BLUE, "limit_team1", "ctrl+shift+f1", "Limit TEAM 1"),
            (Fore.BLUE, "limit_draw", "ctrl+shift+f2", "Limit DRAW"),
            (Fore.BLUE, "limit_team2", "ctrl+shift+f3", "Limit TEAM 2"),
        )),
        (None, True, (
            (Fore.CYAN, "set_amount", "ctrl+a", "Set Amount"),
            (Fore.CYAN, "check_balance", "ctrl+b", "Check Balance"),
            (Fore.YELLOW, "change_markets", "ctrl+m", "Change Markets"),
            (Fore.RED, "quit", "ctrl+q", "Quit"),
        )),
    )

    @staticmethod
    def _combo_text(hotkeys: Optional[dict], action: str, fallback: str) -> str:
        """Render a binding for display: "ctrl+shift+f1" -> "CTRL+SHIFT+F1"."""
        combo = (hotkeys or {}).get(action) or fallback
        return "+".join(part.strip().upper() for part in combo.split("+"))

    def print_hotkey_guide(
        self,
        current_amount: float,
        hotkeys: Optional[dict] = None,
    ) -> None:
        """
        Print the hotkey guide (Team1/Draw/Team2).

        Args:
            current_amount: Current trading amount
            hotkeys: action -> combo map from config.json. The guide is drawn
                from the bindings actually in force, so a customised config.json
                cannot leave this legend naming a key that does something else.
                Falls back to the documented defaults when not provided.
        """
        print(f"{Fore.YELLOW}Hotkeys (Football Mode):{Style.RESET_ALL}")
        print(f"  {Fore.WHITE}Current Amount: ${current_amount:.0f}{Style.RESET_ALL}")
        print()

        # One column width for every combo, so remapped keys of different
        # lengths still line up.
        width = max(
            len(self._combo_text(hotkeys, action, fallback))
            for _, _, rows in self._GUIDE_SECTIONS
            for _, action, fallback, _ in rows
        )

        for heading, blank_before, rows in self._GUIDE_SECTIONS:
            if blank_before:
                print()
            if heading:
                print(f"  {Fore.CYAN}{heading}{Style.RESET_ALL}")
            for colour, action, fallback, label in rows:
                combo = self._combo_text(hotkeys, action, fallback)
                print(f"  {colour}{combo:<{width}}{Style.RESET_ALL} - {label}")
        print()

    def print_football_markets_info(
        self,
        team1: Optional[MarketData],
        draw: Optional[MarketData],
        team2: Optional[MarketData],
        team1_name: str = "Team 1",
        team2_name: str = "Team 2",
        limits: Optional[tuple[float, float, float]] = None,
        default_limit: float = 0.99,
    ) -> None:
        """
        Print football markets information (3 markets).

        Args:
            team1: Market for Team 1 win
            draw: Market for Draw
            team2: Market for Team 2 win
            team1_name: Display name for Team 1
            team2_name: Display name for Team 2
            limits: buy cap for (team1, draw, team2)
            default_limit: default cap, shown dimmed while it is in force
        """
        print(f"{Fore.CYAN}{'-' * 60}{Style.RESET_ALL}")
        print(f"{Fore.WHITE}Football Match:{Style.RESET_ALL} {team1_name} vs {team2_name}")
        print()

        rows = (
            (f"[1] {team1_name}:", team1, Fore.BLUE),
            ("[X] Draw:", draw, Fore.YELLOW),
            (f"[2] {team2_name}:", team2, Fore.BLUE),
        )
        for i, (label, market, color) in enumerate(rows):
            if not market:
                print(f"  {Fore.RED}{label} NOT SET{Style.RESET_ALL}")
                continue
            price = self._ask(market.yes_ask, market.yes_price)
            limit = limits[i] if limits else default_limit
            # A lowered cap is an active restriction on what you will buy: it
            # goes yellow because it has to be noticed. The default one stays
            # visible but dimmed, so the row never shouts without reason.
            if limit < default_limit:
                limit_txt = f"  {Fore.YELLOW}max {limit:.2f}{Style.RESET_ALL}"
            else:
                limit_txt = f"  {Style.DIM}max {limit:.2f}{Style.RESET_ALL}"
            # A market the CLOB reports as closed or inactive cannot be traded:
            # every order on that outcome would be rejected. It stays on the
            # board so the state is visible at the moment of pressing the key,
            # not only in a log line that the next screen clear removes.
            state_txt = ""
            if market.closed or not market.active:
                state_txt = f"  {Fore.RED}NOT TRADABLE{Style.RESET_ALL}"
            print(f"  {color}{label}{Style.RESET_ALL} ${price:.2f}{limit_txt}{state_txt}")

        print(f"{Fore.CYAN}{'-' * 60}{Style.RESET_ALL}\n")

    @staticmethod
    def _ask(ask: float, bid: float) -> float:
        """
        Price to display: the ask, i.e. what it costs to buy.

        The bid (`yes_price`) stays as the fallback for when the book has no
        sell orders, but it is not the right number to show a buyer: on
        illiquid markets the two values diverge a lot.
        """
        return ask if ask > 0 else bid

    def print_load_warnings(self, messages: list[str]) -> None:
        """
        Redraw the warnings raised while the event was being loaded.

        They are logged during the fetch and then wiped by the screen clear
        that precedes this board, so without reprinting them here the warning
        about [1]/[2] being assigned by response order would never be read —
        and that is the one that decides which team a buy hotkey hits.

        Args:
            messages: warning texts from the last successful load
        """
        if not messages:
            return
        for message in messages:
            print(f"{Fore.YELLOW}! {message}{Style.RESET_ALL}")
        print()

    def print_status_ready(self) -> None:
        """Print ready status message."""""
        print(f"{Fore.GREEN}Ready for trading. Press hotkeys to place orders.{Style.RESET_ALL}\n")

    def print_amount_changed(self, new_amount: float) -> None:
        """
        Print notification that amount was changed.

        Args:
            new_amount: The new trading amount
        """
        print(f"\n{Fore.CYAN}Amount changed to: ${new_amount:.0f}{Style.RESET_ALL}\n")

    # How long we keep draining the buffer before reading. It covers the key's
    # trip to the console and its key-up, not the responsiveness of an order:
    # this is a configuration prompt opening, and a fifth of a second goes
    # unnoticed.
    _HOTKEY_SETTLE = 0.2

    def _drain_triggering_hotkey(self) -> None:
        """
        Remove from the console buffer the key that just opened the prompt.

        Draining once is not enough: the `keyboard` hook intercepts the combo
        BEFORE the system delivers it to the console, so by the time the
        callback gets here the character is still in flight and an immediate
        flush cleans an already empty buffer.

        For CTRL+M that is fatal, because its character is \\r: it is literally
        Enter. The "New event URL" prompt used to close the instant it opened,
        and the market change came out cancelled without anyone touching
        anything. For CTRL+A (\\x01) and CTRL+B (\\x02) the effect was milder;
        the text cleanup in read_line removed them anyway.

        So we drain at intervals for a while instead of guessing the right
        moment: that way both the key-down and the key-up of the whole combo
        get caught, in whatever order they arrive.
        """
        deadline = time.monotonic() + self._HOTKEY_SETTLE
        while True:
            flush_console_input()
            if time.monotonic() >= deadline:
                return
            time.sleep(0.02)

    def read_line(self, prompt: str, flush: bool = True) -> Optional[str]:
        """
        Read a line from the console, cleaned of what the hotkeys left behind.

        Before reading we wait for and discard the key that opened the prompt
        (see _drain_triggering_hotkey), so it is not consumed as if it were the
        answer. Callers asking for input BEFORE the hotkeys are registered pass
        flush=False: there is nothing to clean up there, and waiting would only
        throw away an early paste.

        Control characters in the text read are treated two different ways, and
        the difference matters because money is decided here:

        - at the EDGES they are the echo of the hotkey that opened the prompt,
          or of one pressed right after. They are dropped: "^A1" is "1".
        - in the MIDDLE they separate two distinct typings. "5^A1" means "I
          typed 5, I pressed CTRL+A to start over, I typed 1": splicing them
          would give 51, an amount nobody asked for. The line is rejected and
          has to be typed again.

        Returns:
            The text typed, "" if only Enter was pressed, None if the user
            cancelled (CTRL+C, stdin closed) or if the line was broken up by a
            hotkey.
        """
        if flush:
            self._drain_triggering_hotkey()
        try:
            raw = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        text = raw.strip()
        while text and not text[0].isprintable():
            text = text[1:]
        while text and not text[-1].isprintable():
            text = text[:-1]
        text = text.strip()

        if any(not ch.isprintable() for ch in text):
            self.print_error("Typing interrupted by a hotkey: enter the value again.")
            return None

        return text

    def prompt_limit_input(self, outcome_label: str, current_limit: float) -> Optional[float]:
        """
        Ask for the new buy cap of an outcome, in cents.

        You type whole cents (e.g. 60 = $0.60) and not dollars: they are faster
        to type mid-match and they always land on the tick grid, which on these
        markets is 0.01 or 0.001.

        Args:
            outcome_label: the outcome being acted on, for the prompt
            current_limit: current cap in dollars

        Returns:
            New cap in dollars, or None if cancelled
        """
        print(f"\n{Fore.YELLOW}Max buy price - {outcome_label}:{Style.RESET_ALL}")
        print(f"  Current: {current_limit:.2f} ({int(round(current_limit * 100))}c)")
        print("  New limit in cents, 1-99 (Enter to cancel):")

        user_input = self.read_line(f"{Fore.CYAN}c {Style.RESET_ALL}")
        if not user_input:
            # None (cancelled) and "" (bare Enter) mean the same thing.
            return None

        try:
            cents = int(user_input)
        except ValueError:
            self.print_error(f"'{user_input}' is not a number of cents (e.g. 60).")
            return None

        if not 1 <= cents <= 99:
            self.print_error("The limit must be between 1 and 99 cents.")
            return None

        return cents / 100

    def print_limit_changed(self, outcome_label: str, limit: float) -> None:
        """Confirmation of the new buy cap."""
        print(
            f"\n{Fore.CYAN}Max buy price {outcome_label}: "
            f"{limit:.2f} ({int(round(limit * 100))}c){Style.RESET_ALL}\n"
        )

    def prompt_amount_input(self, current_amount: float) -> Optional[float]:
        """
        Prompt user for new trading amount.

        Args:
            current_amount: Current amount for display

        Returns:
            New amount or None if cancelled
        """
        print(f"\n{Fore.YELLOW}Set new trading amount:{Style.RESET_ALL}")
        print(f"  Current: ${current_amount:.0f}")
        print(f"  Enter new amount (or press Enter to cancel):")

        user_input = self.read_line(f"{Fore.CYAN}$ {Style.RESET_ALL}")
        if not user_input:
            return None

        try:
            amount = float(user_input)
        except ValueError:
            # This used to return None inside the same except as EOF/CTRL+C:
            # pasting a URL into the amount prompt said nothing, and was
            # indistinguishable from a deliberate cancel.
            self.print_error(f"'{user_input}' is not a valid amount.")
            return None

        if amount <= 0:
            self.print_error("Amount must be positive.")
            return None

        return amount

    def print_error(self, message: str) -> None:
        """Print an error message."""
        print(f"{Fore.RED}Error: {message}{Style.RESET_ALL}")

    def print_info(self, message: str) -> None:
        """Print an info message."""
        print(f"{Fore.YELLOW}{message}{Style.RESET_ALL}")

    def print_goodbye(self) -> None:
        """
        Print goodbye message.

        The drain goes last, after printing: whatever is left in the buffer
        when the process exits is inherited by the shell that launched the bot,
        which tries to run it as a command. The CTRL+Q itself, and the CTRL+M
        pressed earlier, are the last to get in.
        """
        print(f"\n{Fore.CYAN}Goodbye! Happy trading.{Style.RESET_ALL}\n")
        flush_console_input()

