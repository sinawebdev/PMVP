"""The client address to trust for rate limiting, behind Render's proxy.

Render's proxy APPENDS the address it received the connection from to
X-Forwarded-For, so the right-most hop is the one a client cannot forge.
Everything to its left is whatever the client chose to send: keying a limit on
the left-most hop lets a script claim a fresh "IP" on every request and never
trip it. Without the header (local runs, tests) the socket address is used.
"""
from flask import request


def client_ip():
    hops = [h.strip() for h in (request.headers.get("X-Forwarded-For") or "").split(",") if h.strip()]
    return hops[-1] if hops else (request.remote_addr or "unknown")
