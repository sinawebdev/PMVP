"""Small helpers for HTMX-enhanced routes.

Routes stay progressively enhanced: a normal (non-HTMX) POST still redirects and
flashes exactly as before, so existing tests and no-JavaScript clients keep
working. When the request comes from htmx (the ``HX-Request`` header), the route
returns a partial and raises feedback through an ``HX-Trigger`` toast instead.
"""
import json


def wants_htmx():
    """True when the current request was issued by htmx."""
    from flask import request

    return request.headers.get("HX-Request") == "true"


def with_toast(response, category, message):
    """Attach a toast to an htmx response via the HX-Trigger header.

    app.js listens for the ``showToast`` event htmx re-emits from this header and
    renders an auto-dismissing toast. ``category`` is a Flask flash category
    (success/danger/info/warning).
    """
    response.headers["HX-Trigger"] = json.dumps(
        {"showToast": {"type": category, "msg": message}}
    )
    return response
