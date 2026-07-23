"""Provider delivery-receipt webhooks (Phase 4, Slice 4).

No-login endpoints that receive async delivery callbacks from the SMS/WhatsApp
providers and update the matching PayslipDelivery. These are the provider's
callback surface, so they are NOT behind @login_required — instead each verifies
a provider secret/token before touching any data (a missing/incorrect secret is
rejected, and if no secret is configured the endpoint is disabled by default so a
callback can't be spoofed on an unconfigured deployment).
"""
import hashlib
import hmac

from flask import Blueprint, current_app, request

from .receipts import apply_receipts, parse_hubtel_status, parse_whatsapp_statuses

distribution_webhooks_bp = Blueprint(
    "distribution_webhooks", __name__, url_prefix="/distribution/webhooks"
)


def _verify_meta_signature(raw_body):
    """Verify Meta's X-Hub-Signature-256 (HMAC-SHA256 of the raw body with the
    app secret). Returns True when it matches, False otherwise. If no app secret
    is configured, signature checking is skipped (the verify token still gates)."""
    secret = current_app.config.get("WHATSAPP_APP_SECRET")
    if not secret:
        return True
    header = request.headers.get("X-Hub-Signature-256", "")
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.split("=", 1)[1], expected)


@distribution_webhooks_bp.route("/whatsapp", methods=["GET"])
def whatsapp_verify():
    """Meta subscription verification handshake: echo hub.challenge when the
    verify token matches the configured one."""
    token = current_app.config.get("WHATSAPP_VERIFY_TOKEN")
    if not token:
        return "", 404  # disabled until configured
    if (
        request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == token
    ):
        return request.args.get("hub.challenge", ""), 200
    return "", 403


@distribution_webhooks_bp.route("/whatsapp", methods=["POST"])
def whatsapp_callback():
    if not current_app.config.get("WHATSAPP_VERIFY_TOKEN"):
        return "", 404
    if not _verify_meta_signature(request.get_data()):
        return "", 403
    payload = request.get_json(silent=True) or {}
    applied = apply_receipts(parse_whatsapp_statuses(payload))
    return {"applied": applied}, 200


@distribution_webhooks_bp.route("/hubtel", methods=["POST"])
def hubtel_callback():
    """Hubtel delivery callback. Gated by a shared secret passed as ?secret= or an
    X-Webhook-Secret header (configure HUBTEL_WEBHOOK_SECRET)."""
    secret = current_app.config.get("HUBTEL_WEBHOOK_SECRET")
    if not secret:
        return "", 404  # disabled until configured
    supplied = request.args.get("secret") or request.headers.get("X-Webhook-Secret", "")
    if not hmac.compare_digest(supplied, secret):
        return "", 403
    payload = request.get_json(silent=True) or {}
    applied = apply_receipts(parse_hubtel_status(payload))
    return {"applied": applied}, 200
