"""Channel senders for payslip distribution.

Each channel has a console backend (default — logs only, no credentials, no network) and a
real backend, chosen by a *_BACKEND config value. Real senders POST via stdlib urllib so we
add no new dependency, and any failure is returned as SendResult(ok=False) rather than
raised, so one bad recipient never aborts a whole payroll run's distribution.

Ported from the standalone payslip distribution system; adapted to Chrisnat's Flask config.
"""
import base64
import json as _json
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage

from flask import current_app


@dataclass
class OutboundMessage:
    channel: str
    recipient: str
    subject: str
    body_text: str
    body_html: str | None = None


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
        return SendResult(False, self.provider, f"whatsapp HTTP {status}: {body[:200]}")


# --- Email -----------------------------------------------------------------


class ConsoleEmailSender(Sender):
    provider = "console-email"

    def send(self, message):
        current_app.logger.info(
            "[console-email] to=%s subject=%r\n%s",
            message.recipient,
            message.subject,
            message.body_text,
        )
        return SendResult(ok=True, provider=self.provider)


class SmtpEmailSender(Sender):
    provider = "smtp"

    def send(self, message):
        cfg = current_app.config
        host = cfg.get("SMTP_HOST")
        if not host:
            return SendResult(False, self.provider, "SMTP_HOST not set")
        mime = EmailMessage()
        mime["Subject"] = message.subject
        mime["From"] = cfg.get("DEFAULT_FROM_EMAIL")
        mime["To"] = message.recipient
        mime.set_content(message.body_text)
        if message.body_html:
            mime.add_alternative(message.body_html, subtype="html")
        try:
            with smtplib.SMTP(host, cfg.get("SMTP_PORT", 587), timeout=30) as smtp:
                if cfg.get("SMTP_USE_TLS", True):
                    smtp.starttls()
                username = cfg.get("SMTP_USERNAME")
                if username:
                    smtp.login(username, cfg.get("SMTP_PASSWORD") or "")
                smtp.send_message(mime)
            return SendResult(True, self.provider)
        except Exception as exc:
            return SendResult(False, self.provider, str(exc))


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
