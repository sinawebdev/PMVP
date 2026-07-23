"""Channel senders for payslip distribution.

Each channel has a console backend (default — logs only, no credentials, no network) and a
real backend, chosen by a *_BACKEND config value. Real senders POST via stdlib urllib so we
add no new dependency, and any failure is returned as SendResult(ok=False) rather than
raised, so one bad recipient never aborts a whole payroll run's distribution.

Ported from the standalone payslip distribution system; adapted to Chrisnat's Flask config.
"""
import base64
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


@dataclass
class SendResult:
    ok: bool
    provider: str
    error: str | None = None


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
        current_app.logger.info("[console-sms] to=%s\n%s", message.recipient, message.body_text)
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
            return SendResult(True, self.provider)
        if status == 429:
            return SendResult(False, self.provider, "rate limited by provider (HTTP 429)")
        return SendResult(False, self.provider, f"hubtel HTTP {status}: {body[:200]}")


# --- WhatsApp --------------------------------------------------------------


class ConsoleWhatsAppSender(Sender):
    provider = "console-whatsapp"

    def send(self, message):
        current_app.logger.info(
            "[console-whatsapp] to=%s\n%s", message.recipient, message.body_text
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
            return SendResult(True, self.provider)
        if status == 429:
            return SendResult(False, self.provider, "rate limited by provider (HTTP 429)")
        return SendResult(False, self.provider, f"whatsapp HTTP {status}: {body[:200]}")


# --- Email -----------------------------------------------------------------


def _from_header(cfg):
    """The From header, optionally with a configured display name."""
    address = cfg.get("DEFAULT_FROM_EMAIL")
    name = cfg.get("EMAIL_SENDER_NAME")
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
        current_app.logger.info(
            "[console-email] to=%s subject=%r%s\n%s",
            message.recipient, message.subject, extra, message.body_text,
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
            current_app.logger.warning("[email] invalid recipient %r", message.recipient)
            return SendResult(False, self.provider, f"invalid recipient email: {message.recipient!r}")

        mime = EmailMessage()
        mime["Subject"] = message.subject
        mime["From"] = _from_header(cfg)
        mime["To"] = message.recipient
        reply_to = cfg.get("EMAIL_REPLY_TO")
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
            current_app.logger.warning("[email] recipient refused: %s", message.recipient)
            return SendResult(False, self.provider, f"recipient refused: {message.recipient}")
        except (smtplib.SMTPException, OSError) as exc:
            current_app.logger.warning("[email] send failed to %s: %s", message.recipient, exc)
            return SendResult(False, self.provider, f"{type(exc).__name__}: {exc}")
        current_app.logger.info("[email] sent to %s via %s", message.recipient, self.provider)
        return SendResult(True, self.provider)


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
