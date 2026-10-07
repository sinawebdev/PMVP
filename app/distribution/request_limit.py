"""Per-key request rate limiting for public, unauthenticated routes.

The same shape as :mod:`app.distribution.throttle` and :mod:`app.login_throttle`:
a lock and a dict in this process, a ``reset()`` seam for tests, an injectable
clock, no new dependency. Used by the short payslip link (``/s/<code>``), and
meant for the SasuSync webhook too.

A sliding one-minute window per (bucket, key): a key may make ``per_minute``
requests in any 60 seconds. The same scope caveat as login_throttle applies:
state lives in one process, so N gunicorn workers would allow N x the rate.
"""
import threading
import time as _time
from collections import deque

_WINDOW_SECONDS = 60.0
# Bounds the dict against a flood of distinct keys; idle keys are dropped first.
_MAX_TRACKED_KEYS = 10000

_lock = threading.Lock()
_hits = {}  # (bucket, key) -> deque of monotonic timestamps, oldest first


def reset():
    """Forget all request state (used by tests)."""
    with _lock:
        _hits.clear()


def _prune(window_start):
    """Drop keys with nothing inside the window. Caller holds the lock."""
    for name in [name for name, hits in _hits.items() if not hits or hits[-1] <= window_start]:
        del _hits[name]


def allow(bucket, key, per_minute, *, now=_time.monotonic):
    """Count one request for ``key`` and return True, or return False (and count
    nothing) when ``key`` has already made ``per_minute`` requests in the last
    minute. ``per_minute`` of 0 or less means unlimited."""
    if per_minute <= 0:
        return True
    current = now()
    window_start = current - _WINDOW_SECONDS
    with _lock:
        if len(_hits) >= _MAX_TRACKED_KEYS:
            _prune(window_start)
        hits = _hits.setdefault((bucket, key), deque())
        while hits and hits[0] <= window_start:
            hits.popleft()
        if len(hits) >= per_minute:
            return False
        hits.append(current)
        return True
