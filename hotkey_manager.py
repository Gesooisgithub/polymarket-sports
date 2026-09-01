"""
Hotkey management module using the keyboard library.

Handles:
- Global hotkey registration
- Thread-safe callbacks
- Hotkey lifecycle management
- Periodic refresh callback

IMPORTANT: On Windows, this requires Administrator privileges to capture
global hotkeys. Run the terminal/IDE as Administrator.
"""

import keyboard
import logging
from typing import Dict, Callable, Optional
from threading import Lock, Event
import time

log = logging.getLogger(__name__)


class HotkeyManager:
    """
    Manages global hotkey registration and callbacks.

    WARNING, contrary to what `keyboard.add_hotkey`'s docs say: with
    suppress=False the callbacks do NOT each run on their own thread. They all
    run one after another on the listener's single `process` thread (see
    `_generic.GenericListener.process` -> `pre_process_event`). So a slow or
    blocking callback pauses every other hotkey, and keys pressed meanwhile are
    silently lost: `pre_process_event` computes the combo from the keys held
    down AT THE MOMENT it drains the event, not from those held when the event
    was generated, and by then the key has already been released.

    That is why the prompts (amount, limit, market change) call suspend_all()
    first: while they are open no other hotkey gets through anyway, so we may
    as well not leave them registered.

    Usage:
        manager = HotkeyManager()
        manager.register_hotkey("buy_team1", "ctrl+f1", lambda: print("Buy 1!"))
        manager.start()  # Blocks until stop() is called
    """

    def __init__(self):
        """Initialize the hotkey manager."""
        self._hotkeys: Dict[str, str] = {}  # action_name -> hotkey combo
        self._callbacks: Dict[str, Callable] = {}  # action_name -> callback
        self._lock = Lock()
        self._stop_event = Event()

        # Periodic refresh callback
        self._refresh_callback: Optional[Callable[[], None]] = None
        self._refresh_interval: float = 2.0  # seconds

    @staticmethod
    def _make_wrapper(callback: Callable, action_name: str) -> Callable:
        """
        Wrap a callback so an exception never propagates into the keyboard
        library's listener thread (which would kill the hotkey silently).

        Used by both register_hotkey and resume_all, so a hotkey re-registered
        after a suspend reports errors exactly as it did on first
        registration.
        """
        def wrapped():
            # Logged at INFO where the hook delivers the event: it separates
            # "the hotkey never arrives" (no line) from "it arrives but the
            # order fails" (line present + error from the Trader).
            log.info("Hotkey '%s' pressed", action_name)
            try:
                callback()
            except Exception as e:
                log.error("Error in the callback of hotkey '%s': %s", action_name, e)

        return wrapped

    def register_hotkey(
        self,
        action_name: str,
        hotkey_combo: str,
        callback: Callable,
    ) -> bool:
        """
        Register a hotkey with its callback.

        The hotkeys are never suppressed: the key still reaches the
        foreground app. That is what lets you trade while watching the match on
        a stream without stealing its input.

        Args:
            action_name: Descriptive name (e.g., "buy_team1")
            hotkey_combo: Key combination (e.g., "ctrl+f1", "ctrl+shift+b")
            callback: Function to call when hotkey is pressed

        Returns:
            True if registration succeeded, False otherwise

        Example:
            manager.register_hotkey("buy_team1", "ctrl+f1", app.buy_team1)
        """
        with self._lock:
            try:
                # Store mapping
                self._hotkeys[action_name] = hotkey_combo
                self._callbacks[action_name] = callback

                # Register with keyboard library
                # Note: callback runs in separate thread automatically
                keyboard.add_hotkey(
                    hotkey_combo,
                    self._make_wrapper(callback, action_name),
                    suppress=False
                )

                return True

            except Exception as e:
                print(f"Failed to register hotkey '{action_name}': {e}")
                return False

    def unregister_all(self) -> None:
        """Remove all registered hotkeys."""
        with self._lock:
            try:
                keyboard.unhook_all_hotkeys()
            except Exception:
                pass
            self._hotkeys.clear()
            self._callbacks.clear()

    def suspend_all(self) -> None:
        """Temporarily unhook all hotkeys without clearing the registry."""
        with self._lock:
            try:
                keyboard.unhook_all_hotkeys()
            except Exception:
                pass

    def resume_all(self) -> None:
        """Re-register all hotkeys after a suspend."""
        with self._lock:
            for action_name, hotkey_combo in list(self._hotkeys.items()):
                callback = self._callbacks.get(action_name)
                if not callback:
                    continue

                try:
                    keyboard.add_hotkey(
                        hotkey_combo,
                        self._make_wrapper(callback, action_name),
                        suppress=False
                    )
                except Exception as e:
                    print(f"Failed to restore hotkey '{action_name}': {e}")

    def set_refresh_callback(self, callback: Callable[[], None], interval: float = 2.0) -> None:
        """
        Set a callback to be called periodically for refreshing data.

        Args:
            callback: Function to call periodically
            interval: Time between calls in seconds
        """
        self._refresh_callback = callback
        self._refresh_interval = interval

    def start(self) -> None:
        """
        Start listening for hotkeys.

        This is a blocking call that keeps the main thread alive
        until stop() is called from a callback or another thread.
        Also runs periodic refresh if configured.
        """
        self._stop_event.clear()

        last_refresh = time.time()

        # Keep the thread alive while listening
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=0.1)

            # Periodic refresh
            if self._refresh_callback:
                now = time.time()
                if now - last_refresh >= self._refresh_interval:
                    try:
                        self._refresh_callback()
                    except Exception as e:
                        print(f"Error in refresh callback: {e}")
                    last_refresh = now

    def stop(self) -> None:
        """
        Stop the hotkey listener.

        Can be called from a hotkey callback to exit the main loop.
        """
        self._stop_event.set()
        self.unregister_all()