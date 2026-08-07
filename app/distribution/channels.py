"""Channel senders for payslip distribution.

Each channel has a console backend (default — logs only, no credentials, no network) and a
real backend, chosen by a *_BACKEND config value. Real senders POST via stdlib urllib so we
add no new dependency, and any failure is returned as SendResult(ok=False) rather than
raised, so one bad recipient never aborts a whole payroll run's distribution.

Ported from the standalone payslip distribution system; adapted to the platform Flask config.
"""
import base64
import hashlib
import hmac
import json as _json
import re
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr

from flask import current_app

# Pragmatic email check — enough to reject obviously-bad addresses (missing @,
# spaces, no dot in domain) before we bother the SMTP server, not full RFC 5322.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(address):
    """True if `address` looks like a deliverable email address."""
    return bool(address) and _EMAIL_RE.match(address.strip()) is not None


def _header_safe(value):
    """Strip CR/LF from a value bound for an email header, defusing header
    injection: a newline smuggled into a Subject or sender/reply name could inject
    extra headers (BCC, etc.). None-safe; leaves normal text untouched."""
    if value is None:
        return value
    return str(value).replace("\r", " ").replace("\n", " ")


# --- Log hygiene -----------------------------------------------------------
# A payslip message body is the worker's net pay, deductions and name; the
# recipient is their personal phone number or email. Neither belongs in an
# application log, which is shipped to a third-party aggregator, kept far longer
# than the payslip itself, and readable by anyone with dashboard access — a
# wholly different audience from the one worker the message was addressed to.
#
# What a log line legitimately needs is enough to answer "did item N go out, on
# which provider, and did two sends collide?" — that is the provider, the item
# id, and a stable handle for the recipient. None of that requires the plaintext.


def recipient_fingerprint(recipient):
    """A short, stable, non-reversible handle for a recipient.

    Keyed with SECRET_KEY rather than a bare digest on purpose: a plain
    SHA-256 of a 10-digit phone number (or an address at a known company
    domain) is recovered by brute force in seconds, so an unkeyed "hash" in a
    log is barely better than the number itself. HMAC under a key that never
    leaves the deployment makes the value correlatable across log lines — the
    property we actually want — without being reversible by whoever reads them.
    """
    value = (recipient or "").strip().lower()
    if not value:
        return "-"
    try:
        key = current_app.config.get("SECRET_KEY") or ""
    except RuntimeError:  # no app context (unit tests calling the helper bare)
        key = ""
    digest = hmac.new(
        str(key).encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest[:12]


def log_message_bodies_enabled():
    """True when message bodies may be written to the log.

    Off unless LOG_MESSAGE_BODIES is explicitly turned on, and impossible in
    production — :func:`app.create_app` refuses to boot with it enabled there,
    so this can never be True on a real deployment.
    """
    try:
        return bool(current_app.config.get("LOG_MESSAGE_BODIES", False))
    except RuntimeError:  # no app context
        return False


def _body_for_log(message):
    """The body as it should appear in a log: the text only when an operator has
    deliberately opted in on a non-production box, otherwise a length marker that
    still shows something was rendered."""
    if log_message_bodies_enabled():
        return message.body_text
    return f"<{len(message.body_text or '')} chars withheld>"


@dataclass
class Attachment:
    filename: str
    content: bytes
    mimetype: str = "application/pdf"


@dataclass
class OutboundMessage:
    channel: str
    recipient: str
    subject: str
    body_text: str
    body_html: str | None = None
    attachments: list = field(default_factory=list)
    # Per-message From display name / Reply-To (a tenant branding pack overriding
    # the global config); None falls back to config.
    from_name: str | None = None
    reply_to: str | None = None
    # The PayrollItem this message is for. Carried so a sender can identify the
    # send in a log line without naming the worker (see recipient_fingerprint).
    # Optional and last, so every existing positional construction still works.
    item_id: int | None = None


@dataclass
class SendResult:
    ok: bool
    provider: str
    error: str | None = None
    message_id: str | None = None


def _extract_message_id(body):
    """Best-effort provider message id from a JSON response body. Handles the
    common shapes: Meta WhatsApp {"messages":[{"id":...}]} and Hubtel-style
    {"messageId"|"MessageId"|"id":...} (possibly nested under "data")."""
    try:
        data = _json.loads(body) if isinstance(body, str) else body
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    messages = data.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        mid = messages[0].get("id")
        if mid:
            return str(mid)
    for container in (data, data.get("data") if isinstance(data.get("data"), dict) else {}):
        for key in ("messageId", "MessageId", "message_id", "id", "Id"):
            if container.get(key):
                return str(container[key])
    return None


def _http_post(url, *, headers=None, json=None, timeout=30):
    """POST JSON; return (status, body). HTTPError -> (code, body); transport errors raise."""
    out_headers = dict(headers or {})
    payload = None
    if json is not None:
        payload = _json.dumps(json).encode("utf-8")
        out_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=payload, headers=out_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return getattr(resp, "status", 200), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = str(exc)
        return exc.code, body


class Sender:
    provider = "base"

    def send(self, message: OutboundMessage) -> SendResult:  # pragma: no cover - ABC
        raise NotImplementedError


# --- SMS -------------------------------------------------------------------


class ConsoleSmsSender(Sender):
    provider = "console-sms"

    def send(self, message):
        current_app.logger.info(
            "[console-sms] provider=%s item=%s to=%s body=%s",
            self.provider, message.item_id, recipient_fingerprint(message.recipient),
            _body_for_log(message),
        )
        return SendResult(ok=True, provider=self.provider)


class HubtelSmsSender(Sender):
    provider = "hubtel"

    def send(self, message):
        cfg = current_app.config
        client_id = cfg.get("SMS_HUBTEL_CLIENT_ID")
        secret = cfg.get("SMS_HUBTEL_CLIENT_SECRET")
        sender_id = cfg.get("SMS_SENDER_ID")
        if not (client_id and secret and sender_id):
            return SendResult(False, self.provider, "Hubtel SMS not configured")
        url = cfg.get("SMS_HUBTEL_BASE_URL", "https://sms.hubtel.com/v1/messages/send")
        token = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        try:
            status, body = _http_post(
                url,
                headers={"Authorization": f"Basic {token}"},
                json={"From": sender_id, "To": message.recipient, "Content": message.body_text},
            )
        except Exception as exc:
            return SendResult(False, self.provider, str(exc))
        if 200 <= status < 300:
            return SendResult(True, self.provider, message_id=_extract_message_id(body))
        if status == 429:
            return SendResult(False, self.provider, "rate limited by provider (HTTP 429)")
        return SendResult(False, self.provider, f"hubtel HTTP {status}: {body[:200]}")


# --- WhatsApp --------------------------------------------------------------


class ConsoleWhatsAppSender(Sender):
    provider = "console-whatsapp"

    def send(self, message):
        current_app.logger.info(
            "[console-whatsapp] provider=%s item=%s to=%s body=%s",
            self.provider, message.item_id, recipient_fingerprint(message.recipient),
            _body_for_log(message),
        )
        return SendResult(ok=True, provider=self.provider)


class CloudWhatsAppSender(Sender):
    provider = "whatsapp-cloud"

    def send(self, message):
        cfg = current_app.config
        token = cfg.get("WHATSAPP_TOKEN")
        phone_number_id = cfg.get("WHATSAPP_PHONE_NUMBER_ID")
        if not (token and phone_number_id):
            return SendResult(False, self.provider, "WhatsApp Cloud API not configured")
        base = cfg.get("WHATSAPP_BASE_URL", "https://graph.facebook.com")
        version = cfg.get("WHATSAPP_API_VERSION", "v21.0")
        url = f"{base}/{version}/{phone_number_id}/messages"
        recipient = (message.recipient or "").lstrip("+")
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "text",
            "text": {"body": message.body_text},
        }
        try:
            status, body = _http_post(
                url, headers={"Authorization": f"Bearer {token}"}, json=payload
            )
        except Exception as exc:
            return SendResult(False, self.provider, str(exc))
        if 200 <= status < 300:
            return SendResult(True, self.provider, message_id=_extract_message_id(body))
        if status == 429:
            return SendResult(False, self.provider, "rate limited by provider (HTTP 429)")
        return SendResult(False, self.provider, f"whatsapp HTTP {status}: {body[:200]}")


# --- Email -----------------------------------------------------------------


def _from_header(cfg, from_name=None):
    """The From header. A per-message from_name (a tenant branding pack) wins over
    the global EMAIL_SENDER_NAME; the address is always DEFAULT_FROM_EMAIL."""
    address = cfg.get("DEFAULT_FROM_EMAIL")
    name = _header_safe(from_name or cfg.get("EMAIL_SENDER_NAME"))
    return formataddr((name, address)) if name and address else address


def _attach_all(mime, attachments):
    """Attach validated attachments to a MIME message. Oversized/empty ones are
    skipped with a warning rather than blocking the email (validation happens in
    the service layer; this is a defensive second check)."""
    max_bytes = current_app.config.get("EMAIL_MAX_ATTACHMENT_BYTES", 5 * 1024 * 1024)
    for att in attachments or []:
        content = att.content or b""
        if not content or len(content) > max_bytes:
            current_app.logger.warning(
                "[email] skipping attachment %s (%d bytes, cap %d)",
                att.filename, len(content), max_bytes,
            )
            continue
        maintype, _, subtype = att.mimetype.partition("/")
        mime.add_attachment(
            content, maintype=maintype or "application",
            subtype=subtype or "octet-stream", filename=att.filename,
        )


class ConsoleEmailSender(Sender):
    provider = "console-email"

    def send(self, message):
        if not is_valid_email(message.recipient):
            return SendResult(False, self.provider, f"invalid recipient email: {message.recipient!r}")
        extra = f" +{len(message.attachments)} attachment(s)" if message.attachments else ""
        # The subject is withheld alongside the body: an email subject line is
        # rendered per worker and can carry their name, so it is payslip content
        # too, not routing metadata.
        current_app.logger.info(
            "[console-email] provider=%s item=%s to=%s%s body=%s",
            self.provider, message.item_id, recipient_fingerprint(message.recipient),
            extra, _body_for_log(message),
        )
        return SendResult(ok=True, provider=self.provider)


class SmtpEmailSender(Sender):
    provider = "smtp"

    def send(self, message):
        cfg = current_app.config
        host = cfg.get("SMTP_HOST")
        if not host:
            return SendResult(False, self.provider, "SMTP_HOST not set")
        # Validate the recipient before opening a connection — a clear, cheap
        # failure instead of an opaque SMTP rejection.
        if not is_valid_email(message.recipient):
            current_app.logger.warning(
                "[email] invalid recipient to=%s item=%s",
                recipient_fingerprint(message.recipient), message.item_id,
            )
            return SendResult(False, self.provider, f"invalid recipient email: {message.recipient!r}")

        mime = EmailMessage()
        mime["Subject"] = _header_safe(message.subject)
        mime["From"] = _from_header(cfg, message.from_name)
        mime["To"] = message.recipient
        reply_to = _header_safe(message.reply_to or cfg.get("EMAIL_REPLY_TO"))
        if reply_to:
            mime["Reply-To"] = reply_to
        mime.set_content(message.body_text)
        if message.body_html:
            mime.add_alternative(message.body_html, subtype="html")
        _attach_all(mime, message.attachments)

        try:
            with smtplib.SMTP(host, cfg.get("SMTP_PORT", 587), timeout=30) as smtp:
                if cfg.get("SMTP_USE_TLS", True):
                    smtp.starttls()
                username = cfg.get("SMTP_USERNAME")
                if username:
                    smtp.login(username, cfg.get("SMTP_PASSWORD") or "")
                smtp.send_message(mime)
        except smtplib.SMTPAuthenticationError as exc:
            current_app.logger.warning("[email] SMTP auth failed: %s", exc)
            return SendResult(False, self.provider, "SMTP authentication failed")
        except smtplib.SMTPRecipientsRefused:
            current_app.logger.warning(
                "[email] recipient refused to=%s item=%s",
                recipient_fingerprint(message.recipient), message.item_id,
            )
            return SendResult(False, self.provider, f"recipient refused: {message.recipient}")
        except (smtplib.SMTPException, OSError) as exc:
            current_app.logger.warning(
                "[email] send failed to=%s item=%s: %s",
                recipient_fingerprint(message.recipient), message.item_id, exc,
            )
            return SendResult(False, self.provider, f"{type(exc).__name__}: {exc}")
        current_app.logger.info(
            "[email] sent provider=%s item=%s to=%s",
            self.provider, message.item_id, recipient_fingerprint(message.recipient),
        )
        return SendResult(True, self.provider)


def simulated_channels():
    """The channels still on their console backend, i.e. logged but never delivered.

    A console send returns ok=True, so the delivery is recorded `sent` and the
    status screens show a green badge and a 100% success rate for a payslip that
    only ever reached a log line. Until a real provider is configured, the UI has
    to say that plainly rather than let the badge imply a delivery happened —
    callers use this to render that disclosure. Empty once every channel is live.
    """
    cfg = current_app.config
    live = {"sms": "hubtel", "whatsapp": "cloud", "email": "smtp"}
    return [
        channel
        for channel, real in live.items()
        if cfg.get(f"{channel.upper()}_BACKEND") != real
    ]


def delivery_is_simulated():
    """True while any channel is console-backed — the one flag templates gate on."""
    return bool(simulated_channels())


CHANNEL_LABELS = {"sms": "SMS", "whatsapp": "WhatsApp", "email": "email"}


def simulated_channel_labels():
    """:func:`simulated_channels` as display names, for the UI disclosure."""
    return [CHANNEL_LABELS.get(c, c) for c in simulated_channels()]


def get_sender(channel: str) -> Sender:
    """Return the Sender for a channel, console vs real per the *_BACKEND config."""
    cfg = current_app.config
    if channel == "sms":
        return HubtelSmsSender() if cfg.get("SMS_BACKEND") == "hubtel" else ConsoleSmsSender()
    if channel == "whatsapp":
        return (
            CloudWhatsAppSender()
            if cfg.get("WHATSAPP_BACKEND") == "cloud"
            else ConsoleWhatsAppSender()
        )
    if channel == "email":
        return SmtpEmailSender() if cfg.get("EMAIL_BACKEND") == "smtp" else ConsoleEmailSender()
    return ConsoleSmsSender()
