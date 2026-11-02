"""The client address to trust for rate limiting and the audit trail.

Every request to a Render web service arrives through Cloudflare, which sets
``CF-Connecting-IP`` to the address it accepted the connection from and
discards any value the client sent. That is the one address a client cannot
choose, so it is the default (``CLIENT_IP_HEADER``).

X-Forwarded-For is deliberately NOT used. Render appends to it without
clearing what the client sent, so the left-most entry is client-chosen, and
the right-most entries are Cloudflare / Render load balancers that every
visitor shares. Keying a lockout on either end is wrong: the first lets a
script rotate fake addresses, the last locks every user out together.

Without the header (local runs, tests) the socket address is used. On Render
that would be a shared proxy address, so its absence there is logged.
"""
import ipaddress

from flask import current_app, request

_warned_missing = False


def _valid(value):
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        return None


def client_ip():
    """The trusted client address as a normalised string, or "unknown"."""
    global _warned_missing
    header = current_app.config.get("CLIENT_IP_HEADER", "CF-Connecting-IP")
    if header:
        found = _valid(request.headers.get(header))
        if found:
            return found
        if current_app.config.get("ON_RENDER") and not _warned_missing:
            _warned_missing = True
            current_app.logger.warning(
                "[client-ip] %s missing on Render; rate limits are falling back to the "
                "socket address, which is a shared proxy", header)
    return _valid(request.remote_addr) or "unknown"


def rate_limit_key(address):
    """What a per-address limit counts against: the address itself for IPv4,
    its /64 for IPv6, where one subscriber is handed a whole /64 and can rotate
    through it freely."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address
    if parsed.version == 6:
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return str(parsed)
