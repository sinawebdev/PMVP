"""Per-channel send-rate limiting / provider throttling (Phase 4, Slice 2).

Providers (Hubtel SMS, WhatsApp Cloud API, SMTP) enforce their own per-second
quotas; bursting past them gets sends rejected or the account throttled. This
paces outbound sends per channel to a configured rate, right before the provider
call, so a large batch drips out within quota instead of firing all at once.

A simple min-interval limiter keyed by channel, held in this process. The worker
is a single process (inline thread or one `distribution-worker`), so an
in-process limiter is correct and needs no shared state. If you scale to N worker
processes, each paces independently, so the effective ceiling is N x the rate —
size the per-channel rate as (provider limit / worker count) in that case.

Rate 0 / unset = unlimited (the default, so existing behaviour is unchanged).
"""
import threading
import time as _time

from flask import current_app

_lock = threading.Lock()
_last_send_at = {}  # channel -> monotonic timestamp of the next allowed send


def rate_per_sec(channel):
    """Configured sends-per-second for a channel (0 == unlimited)."""
    try:
        value = current_app.config.get(f"RATE_LIMIT_{channel.upper()}_PER_SEC", 0)
    except RuntimeError:  # no app context
        return 0.0
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def reset():
    """Forget all pacing state (used by tests)."""
    with _lock:
        _last_send_at.clear()


def throttle(channel, *, sleep=_time.sleep, now=_time.monotonic):
    """Block just long enough to keep `channel` under its configured send rate.

    Returns the seconds waited (0 when unlimited or already within budget). The
    slot is reserved under a lock, then the sleep happens outside it, so
    concurrent callers serialise onto successive slots rather than colliding.
    `sleep`/`now` are injectable for tests."""
    rate = rate_per_sec(channel)
    if rate <= 0:
        return 0.0
    min_interval = 1.0 / rate
    with _lock:
        current = now()
        earliest = _last_send_at.get(channel, current)
        start = max(current, earliest)
        wait = start - current
        _last_send_at[channel] = start + min_interval
    if wait > 0:
        sleep(wait)
    return wait
