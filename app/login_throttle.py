"""Login attempt rate limiting and lockout.

Deliberately **not** Flask-Limiter. This codebase already solves in-process rate
limiting once, in :mod:`app.distribution.throttle`, and that module's shape — a
lock plus a dict, config-driven rates, a ``reset()`` seam for tests, injectable
``now`` — is reused here rather than a second, differently-shaped answer being
added beside it. It also keeps a hard project rule: ``requirements.txt`` is
pinned deliberately, and the channel senders go as far as calling providers over
stdlib ``urllib`` "so we add no new dependency". A login limiter is not the place
to break that.

Two independent counters, because they stop different attacks:

  * **per IP** — one source spraying many accounts (credential stuffing)
  * **per account** — many sources against one account (targeted brute force)

Either tripping locks the login route for that key. The account counter is keyed
by the submitted email, which is why :func:`check` is consulted *before* the user
lookup — the response must not depend on whether the address exists.

**Scope, stated plainly:** this state lives in one process. The deployment runs a
single gunicorn worker with threads (see ``render.yaml``), so today one process
sees every login and the limit is exact. Run N workers and each keeps its own
counters, so the effective ceiling becomes N x ``LOGIN_MAX_ATTEMPTS`` — the same
caveat ``distribution/throttle.py`` documents for itself. Surviving a restart, or
being exact across processes, needs shared state (a table or Redis); that is a
schema/infrastructure decision, not one to make silently inside a helper.
"""
import threading
import time as _time

from flask import current_app

_lock = threading.Lock()
# key -> {"failures": int, "first_at": monotonic, "locked_until": monotonic|None}
_state = {}

# Never let the dict grow without bound: a stuffing run against thousands of
# addresses would otherwise turn this defence into a memory leak of its own.
_MAX_TRACKED_KEYS = 10000


def _config(name, default):
    try:
        value = current_app.config.get(name, default)
    except RuntimeError:  # no app context
        return default
    try:
        return type(default)(value)
    except (TypeError, ValueError):
        return default


def max_attempts():
    """Failures allowed within the window before the key is locked."""
    return _config("LOGIN_MAX_ATTEMPTS", 5)


def window_seconds():
    """How long failures accumulate before the count resets."""
    return _config("LOGIN_ATTEMPT_WINDOW_SECONDS", 900)


def lockout_seconds():
    """How long a key stays locked once it trips the limit."""
    return _config("LOGIN_LOCKOUT_SECONDS", 900)


def reset():
    """Forget all attempt state (used by tests)."""
    with _lock:
        _state.clear()


def _prune(now):
    """Drop entries that are neither locked nor inside their window. Caller holds
    the lock."""
    window, expired = window_seconds(), []
    for key, entry in _state.items():
        locked_until = entry["locked_until"]
        if locked_until is not None and locked_until > now:
            continue
        if now - entry["first_at"] < window:
            continue
        expired.append(key)
    for key in expired:
        _state.pop(key, None)


def ip_key(address):
    return f"ip:{address or 'unknown'}"


def account_key(email):
    return f"account:{(email or '').strip().lower()}"


def retry_after(keys, *, now=_time.monotonic):
    """Seconds until the soonest of ``keys`` unlocks, or 0 if none is locked.

    Read-only — checking must never itself count as an attempt, or a locked-out
    user reloading the page would extend their own lockout forever.
    """
    current = now()
    longest = 0.0
    with _lock:
        for key in keys:
            entry = _state.get(key)
            if not entry:
                continue
            locked_until = entry["locked_until"]
            if locked_until is not None and locked_until > current:
                longest = max(longest, locked_until - current)
    return longest


def register_failure(keys, *, now=_time.monotonic):
    """Count one failed attempt against every key. Returns True if any key is now
    locked."""
    current = now()
    window, limit, lock_for = window_seconds(), max_attempts(), lockout_seconds()
    locked = False
    with _lock:
        if len(_state) >= _MAX_TRACKED_KEYS:
            _prune(current)
        for key in keys:
            entry = _state.get(key)
            # Start a fresh window when there is no entry, or the old one aged
            # out — otherwise five typos spread over a month would lock a user.
            if entry is None or (
                entry["locked_until"] is None and current - entry["first_at"] >= window
            ):
                entry = {"failures": 0, "first_at": current, "locked_until": None}
                _state[key] = entry
            if entry["locked_until"] is not None and entry["locked_until"] <= current:
                entry.update(failures=0, first_at=current, locked_until=None)
            entry["failures"] += 1
            if entry["failures"] >= limit:
                entry["locked_until"] = current + lock_for
                locked = True
    return locked


def clear(keys):
    """Forget the failures for ``keys`` — called on a successful login so a user
    who eventually gets their password right does not stay part-way to a lockout."""
    with _lock:
        for key in keys:
            _state.pop(key, None)
