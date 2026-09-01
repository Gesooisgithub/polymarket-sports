"""
Removes two HTTP round trips from the critical order path.

For every order the SDK calls `fetch_tick_size_sync` and `fetch_neg_risk_sync`,
two uncached GETs, before it even signs. On a hotkey's path they cost ~110ms
each: two thirds of the total latency spent re-downloading values the bot
already read when you picked the event.

Both are known from `MarketData`:
- `neg_risk` is a structural classification decided when the market is created
  (docs: concepts/negative-risk), so it never changes. It picks which exchange
  the order is signed against: getting it wrong produces a signature towards
  the wrong contract, which is why we do NOT guess and use only the market's
  real value.
- `tick_size` varies per market (measured: 3 out of 10 1X2 matches have
  different ticks across the three outcomes) and can change over time, so it is
  refreshed in the background instead of being pinned once.

When a token is not cached we fall back to the real call, so behaviour stays
correct for markets that were never registered.

NOTE: this depends on functions in `polymarket._internal`, which are not public
API and may change between SDK versions. `install()` checks that they exist and
fails explicitly if it cannot find them: better an error at startup than a bot
that silently goes slow again.
"""

import logging
from decimal import Decimal, InvalidOperation
from threading import Lock

log = logging.getLogger(__name__)

_SDK_MODULE = "polymarket._internal.actions.orders.market"

_lock = Lock()
_tick_by_token: dict[str, Decimal] = {}
_neg_risk_by_token: dict[str, bool] = {}

_original_tick = None
_original_neg_risk = None
_installed = False


def _supported_tick(value: Decimal) -> bool:
    """True if the SDK can round with this tick (0.1, 0.01, 0.001, ...)."""
    from polymarket._internal.actions.orders.context import resolve_rounding_config

    try:
        resolve_rounding_config(value)
        return True
    except Exception:
        return False


def install() -> None:
    """
    Replace the SDK's two fetches with versions that read from the cache.

    Idempotent. Raises RuntimeError if the SDK no longer exposes the expected
    functions, so the problem surfaces at startup and not as a mute slowdown.
    """
    global _original_tick, _original_neg_risk, _installed

    with _lock:
        if _installed:
            return

        try:
            import importlib

            module = importlib.import_module(_SDK_MODULE)
        except ImportError as e:
            raise RuntimeError(
                f"order_fastpath: could not import {_SDK_MODULE} ({e}). "
                "The polymarket-client SDK changed its internal structure."
            ) from e

        missing = [
            name
            for name in ("fetch_tick_size_sync", "fetch_neg_risk_sync")
            if not callable(getattr(module, name, None))
        ]
        if missing:
            raise RuntimeError(
                f"order_fastpath: {_SDK_MODULE} no longer exposes {', '.join(missing)}. "
                "Remove this optimisation or update it to the SDK's new API."
            )

        _original_tick = module.fetch_tick_size_sync
        _original_neg_risk = module.fetch_neg_risk_sync

        def fetch_tick_size_sync(ctx, *, token_id: str) -> Decimal:
            cached = _tick_by_token.get(token_id)
            if cached is not None:
                return cached
            return _original_tick(ctx, token_id=token_id)

        def fetch_neg_risk_sync(ctx, *, token_id: str) -> bool:
            cached = _neg_risk_by_token.get(token_id)
            if cached is not None:
                return cached
            return _original_neg_risk(ctx, token_id=token_id)

        module.fetch_tick_size_sync = fetch_tick_size_sync
        module.fetch_neg_risk_sync = fetch_neg_risk_sync
        _installed = True
        log.debug("order_fastpath installed on %s", _SDK_MODULE)


def register_market(market) -> None:
    """
    Register a MarketData's tick size and neg_risk for both of its tokens.

    A tick the SDK does not support is not cached: that token falls back to the
    real call instead of making the order fail.
    """
    if market is None:
        return

    token_ids = [t for t in (market.yes_token_id, market.no_token_id) if t]
    if not token_ids:
        return

    try:
        tick = Decimal(str(market.tick_size))
    except (InvalidOperation, ValueError):
        tick = None

    if tick is not None and not _supported_tick(tick):
        log.warning(
            "tick size %s not supported by the SDK for '%s': keeping the per-order fetch",
            tick, getattr(market, "question", "?"),
        )
        tick = None

    neg_risk = bool(market.neg_risk)

    with _lock:
        for token_id in token_ids:
            if tick is not None:
                _tick_by_token[token_id] = tick
            _neg_risk_by_token[token_id] = neg_risk


def refresh_tick_sizes(client, token_ids) -> None:
    """
    Realign the cached tick sizes by querying the CLOB.

    Must be called from the periodic loop, NEVER from the order path: its whole
    purpose is to move this cost off the keypress. An error here is benign, the
    previous value is kept.
    """
    if not _installed or _original_tick is None:
        return

    ctx = getattr(client, "_ctx", None)
    if ctx is None:
        return

    for token_id in token_ids:
        if not token_id:
            continue
        try:
            fresh = _original_tick(ctx, token_id=token_id)
        except Exception as e:
            log.debug("tick size refresh failed for %s: %s", token_id, e)
            continue

        with _lock:
            previous = _tick_by_token.get(token_id)
            if previous != fresh:
                # Rare but possible change: worth saying, because it would
                # explain any rejections for an off-grid price.
                log.info("tick size changed for %s: %s -> %s", token_id, previous, fresh)
                _tick_by_token[token_id] = fresh
