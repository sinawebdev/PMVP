"""SasuSync SMS sender.

Request shape, paths and response fields come from SasuSync's integration brief,
kept verbatim at ``docs/vendor/sasusync-agent.md``. Two deliberate departures
from that brief (see ``plans/sms-distribution.md``, Phase 1):

* Its reference client is not used. It needs ``requests``, reads the API key
  when the module is imported (crashing any boot where the key is unset), and
  retries a send after a network error while sleeping inside the call. Here a
  send is one POST through the stdlib ``_http_post``; retries happen later,
  through ``next_retry_at`` and the existing sweep, and only when the outcome
  says nothing was sent.
* "On a timeout, check the message status" cannot apply to a send: a send that
  times out never returned a ``task_id``, and the status endpoint needs one. A
  timeout is reported as *ambiguous* and the delivery is never re-sent
  automatically.

Every outcome is a ``SendResult``; nothing is raised to the caller.
"""
import http.client
import json
import re
import socket
import urllib.error

from flask import current_app

from .channels import Sender, SendResult, _http_post, recipient_fingerprint
from .phones import normalise_gh_mobile

SEND_PATH = "/api/v1/send"
SANDBOX_SEND_PATH = "/smssandbox/v1/send"
TIMEOUT_SECONDS = 30

# A provider's error text can quote the number it refused. The error is stored
# on the delivery and shown on screen, so long digit runs are blanked first.
_NUMBER_RUN = re.compile(r"\+?\d{9,}")


def _send_url(cfg):
    base = (cfg.get("SASUSYNC_BASE_URL") or "").rstrip("/")
    path = SANDBOX_SEND_PATH if cfg.get("SASUSYNC_SANDBOX", True) else SEND_PATH
    return f"{base}{path}"


def _parse(body):
    try:
        data = json.loads(body) if body else None
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _task_id(data):
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    value = inner.get("task_id") or data.get("task_id")
    return str(value) if value else None


def _units(data):
    balance = data.get("balance") if isinstance(data.get("balance"), dict) else {}
    try:
        return int(balance.get("deducted"))
    except (TypeError, ValueError):
        return None


def _detail(body):
    data = _parse(body)
    text = data.get("detail") if data else None
    if not isinstance(text, str):
        text = body or ""
    return _NUMBER_RUN.sub("[number]", text)[:200]


def _nothing_was_sent(exc):
    """True only when the request provably never reached SasuSync: the host did
    not resolve, or the connection was refused. urllib wraps connect-time errors
    in URLError, so look through it to the socket error underneath."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, (socket.gaierror, ConnectionRefusedError))


class SasuSyncSmsSender(Sender):
    provider = "sasusync"

    def send(self, message):
        cfg = current_app.config
        api_key = cfg.get("SASUSYNC_API_KEY")
        sender_id = cfg.get("SMS_SENDER_ID")
        if not (api_key and sender_id and cfg.get("SASUSYNC_BASE_URL")):
            return SendResult(False, self.provider, "SasuSync SMS not configured",
                              retryable=False)
        recipient = normalise_gh_mobile(message.recipient)
        if recipient is None:
            return SendResult(False, self.provider, "invalid number", retryable=False)

        payload = {"sender": sender_id, "recipients": [recipient],
                   "message": message.body_text}
        if message.delivery_id is not None:
            payload["metadata"] = {"delivery_id": message.delivery_id}
        try:
            status, body = _http_post(
                _send_url(cfg), headers={"X-API-Key": api_key}, json=payload,
                timeout=TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - every outcome becomes a SendResult
            return self._transport_failure(message, recipient, exc)
        return self._classify(message, recipient, status, body)

    def _classify(self, message, recipient, status, body):
        if 200 <= status < 300:
            data = _parse(body)
            result = SendResult(True, self.provider, message_id=_task_id(data),
                                units=_units(data))
        elif status == 429 or 500 <= status < 600:
            result = SendResult(False, self.provider,
                                f"sasusync HTTP {status}: {_detail(body)}", retryable=True)
        elif 400 <= status < 500:
            # The server answered and refused the request (bad request, bad key,
            # no credit, unapproved sender, validation), so nothing went out and
            # the same request would fail the same way.
            result = SendResult(False, self.provider,
                                f"sasusync HTTP {status}: {_detail(body)}", retryable=False)
        else:
            result = SendResult(False, self.provider,
                                f"sasusync HTTP {status}: unexpected response", ambiguous=True)
        self._log(message, recipient, status, result)
        return result

    def _transport_failure(self, message, recipient, exc):
        if _nothing_was_sent(exc):
            result = SendResult(False, self.provider,
                                f"could not reach SasuSync ({type(exc).__name__})",
                                retryable=True)
        else:
            # A timeout, a reset or a dropped connection once the request was on
            # the wire, or anything not recognised: SasuSync may have queued it.
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if isinstance(reason, (TimeoutError, socket.timeout)):
                kind = "timed out"
            elif isinstance(reason, (ConnectionError, http.client.HTTPException)):
                kind = "connection dropped"
            else:
                kind = "unexpected error"
            result = SendResult(False, self.provider,
                                f"SasuSync {kind}; this message may have been sent",
                                ambiguous=True)
        self._log(message, recipient, type(exc).__name__, result)
        return result

    def _log(self, message, recipient, status, result):
        outcome = "ok" if result.ok else ("ambiguous" if result.ambiguous else "failed")
        current_app.logger.info(
            "[sasusync] provider=%s item=%s delivery=%s to=%s status=%s outcome=%s "
            "units=%s task=%s",
            self.provider, message.item_id, message.delivery_id,
            recipient_fingerprint(recipient), status, outcome, result.units,
            result.message_id,
        )
