# SasuSync integration brief — Python

> Hand this whole file to your coding agent. It contains everything needed to
> integrate SMS, one-time codes and voice calls, with no other page to read.

- **Base URL:** `https://sms.sasusync.com`
- **Auth:** every request sends the header `X-API-Key: <your key>`
- **Target language:** Python
- **Human documentation:** https://sms.sasusync.com/docs/api
- **Get a key:** https://sms.sasusync.com/portal (shown once, at creation)

---

## Rules — follow these exactly

- Read the API key from an environment variable. Never write it into source, a mobile app, a browser bundle, or a repository.
- Build against `/smssandbox/v1/send` first. It is free, unlimited, and takes the identical body — switch that one URL to `/api/v1/send` to go live. It does NOT waive sender ID approval: an unapproved name returns 403 in sandbox exactly as it does live, so register the sender you intend to use before you start, not at go-live.
- `sender` must be a sender ID approved on this account. There is no shared or default sender name to fall back on — an unapproved name returns 403.
- Billing is per message part, not per message. Up to 160 GSM characters is one part; beyond that every 153 characters is another. Non-GSM characters (emoji, curly quotes, accents) cut a part to 70 characters. Read `balance.deducted` for the real figure.
- Retry only 429, 500, 502, 503, 504 and network timeouts, with exponential backoff. Never retry 400, 401, 402, 403, 409 or 422 — the same request will fail the same way.
- A timeout is not a failure. The message may already be on its way. Check `/api/v1/status/{task_id}` before resending, or the recipient gets it twice and the account pays twice.
- For OTP, never store or compare the code. Call generate, then verify. One pending code per number; a second request while one is live returns 429.
- For webhooks, compute HMAC-SHA256 over the RAW request body bytes exactly as received. Parsing the JSON and re-serialising it changes the bytes and the signature will not match.
- Treat webhook events as at-least-once and possibly out of order. Key handling on `data.message_id` and make a repeat a no-op.
- Ghanaian numbers are accepted as `233552148347`, `+233552148347` or `0552148347`.
- A response carrying `queued: true` and a `task_id` is an acceptance, not an error. The message is held and delivered automatically, usually within a couple of minutes, and nothing is charged until it goes out. Never resend it — that delivers twice and bills twice.

---

## Billing model

The account holds **two separate balances**, spent in a fixed order.

| What is sent | Paid from | Notes |
| --- | --- | --- |
| SMS | SMS credits, then main balance | One credit per message part |
| OTP by SMS | SMS credits, then main balance | 3 credits per code (4 at 7-8 digits), see `rates.otp_sms_credits` |
| OTP by voice | Main balance | It is a phone call |
| Voice message | Main balance | Priced per recipient |

An OTP by SMS is paid in SMS credits, not the main balance — an empty main balance
does not stop it. This overflow is the same for a bulk SMS and for an OTP by SMS:
SMS credits are spent first, and any remainder is charged to the main balance at
the account's SMS rate, so nothing fails halfway or one code short. The main
balance on its own is needed only for voice.

---

## Reference client (Python)

Copy this into the project and use it. It already implements the retry policy,
segment counting and webhook verification described above.

```python
"""Minimal SasuSync client. Requires: pip install requests"""
import hashlib
import hmac
import math
import os
import time

import requests

BASE_URL = os.environ.get("SASUSYNC_BASE_URL", "https://sms.sasusync.com")
API_KEY = os.environ["SASUSYNC_API_KEY"]

# Only these are worth retrying. A 400 or 403 will fail identically forever.
RETRY_STATUSES = {429, 500, 502, 503, 504}


class SasuSyncError(Exception):
    def __init__(self, status, detail):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def _request(method, path, payload=None, attempts=4):
    url = f"{BASE_URL}{path}"
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}

    for attempt in range(attempts):
        try:
            res = requests.request(method, url, headers=headers, json=payload, timeout=30)
        except requests.RequestException:
            # The request may still have been processed. Never blindly resend a
            # send — look the job up instead.
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)
            continue

        if res.status_code in RETRY_STATUSES and attempt < attempts - 1:
            time.sleep(2 ** attempt)
            continue
        if res.status_code >= 400:
            detail = res.json().get("detail") if res.headers.get(
                "content-type", "").startswith("application/json") else res.text
            raise SasuSyncError(res.status_code, detail)
        return res.json()


def segments(message):
    """How many parts the network will split this into — what you are billed for."""
    length = len(message)
    if length == 0:
        return 1
    return 1 if length <= 160 else math.ceil(length / 153)


def send_sms(sender, recipients, message, sandbox=False):
    if isinstance(recipients, str):
        recipients = [recipients]
    path = "/smssandbox/v1/send" if sandbox else "/api/v1/send"
    return _request("POST", path,
                    {"sender": sender, "recipients": recipients, "message": message})


def schedule_sms(sender, recipients, message, when):
    """`when` is 'YYYY-MM-DD HH:MM'."""
    if isinstance(recipients, str):
        recipients = [recipients]
    return _request("POST", "/schedule/v1/send",
                    {"sender": sender, "recipients": recipients,
                     "message": message, "schedule_time": when})


def message_status(job_id):
    return _request("GET", f"/api/v1/status/{job_id}")


def balance():
    return _request("GET", "/api/v1/balance")


def send_otp(number, sender_id, message="Your code is %otp_code%. It expires in 5 minutes.",
             length=6, expiry=5, medium="sms"):
    if "%otp_code%" not in message:
        raise ValueError("The message must contain %otp_code%")
    return _request("POST", "/otp/generate",
                    {"number": number, "sender_id": sender_id, "message": message,
                     "medium": medium, "length": length, "expiry": expiry})


def verify_otp(number, code):
    return _request("POST", "/otp/verify", {"number": number, "code": code})


def verify_webhook(raw_body: bytes, signature: str) -> bool:
    """Hash the RAW bytes. Re-serialising parsed JSON changes them."""
    secret = os.environ["SASUSYNC_WEBHOOK_SECRET"].encode()
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature or "", expected)
```

---

## Endpoints

### SMS

#### Send an SMS

`POST /api/v1/send`

One call sends to one number or to thousands.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `sender` | string | yes | A sender ID approved on this account, max 11 characters |
| `recipients` | string or array | yes | One number, or many in a single call |
| `message` | string | yes | Up to 650 characters |
| `metadata` | object | no | Your own key/value pairs, echoed back on webhooks |

```python
import os
import requests

res = requests.post(
    "https://sms.sasusync.com/api/v1/send",
    headers={"X-API-Key": os.environ["SASUSYNC_API_KEY"]},
    json={
        "sender": "YourBrand",
        "recipients": ["233552148347"],
        "message": "Hello from SasuSync",
    },
    timeout=30,
)
res.raise_for_status()
print(res.json())
```

Response:

```json
{
  "success": true,
  "balance": {
    "deducted": 1,
    "remaining": 1649
  },
  "data": {
    "recipients_count": 1,
    "status": "queued",
    "task_id": "6f461b5a-7d35-4d55-97b0-00e420bf563b"
  }
}
```

- `balance.deducted` is the number of message parts actually billed. Trust it over your own character count.
- A sender name not approved on the account returns 403.

#### Send a test SMS (free)

`POST /smssandbox/v1/send`

Identical request and response shape. Nothing is delivered and nothing is charged — but the sender must still be an approved sender ID.

```python
import os
import requests

res = requests.post(
    "https://sms.sasusync.com/smssandbox/v1/send",
    headers={"X-API-Key": os.environ["SASUSYNC_API_KEY"]},
    json={
        "sender": "YourBrand",
        "recipients": ["233552148347"],
        "message": "Hello from SasuSync",
    },
    timeout=30,
)
res.raise_for_status()
print(res.json())
```

Response:

```json
{
  "success": true,
  "balance": {
    "deducted": 1,
    "remaining": 1649
  },
  "data": {
    "recipients_count": 1,
    "status": "queued",
    "task_id": "6f461b5a-7d35-4d55-97b0-00e420bf563b"
  }
}
```

- Build the entire integration against this path first, then change the single URL to go live.

#### Schedule an SMS

`POST /schedule/v1/send`

The same body as a normal send, plus `schedule_time`.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `schedule_time` | string | yes | `YYYY-MM-DD HH:MM`, 24-hour |

```python
import os
import requests

res = requests.post(
    "https://sms.sasusync.com/schedule/v1/send",
    headers={"X-API-Key": os.environ["SASUSYNC_API_KEY"]},
    json={
        "sender": "YourBrand",
        "recipients": ["233552148347"],
        "message": "Your appointment is tomorrow at 10am.",
        "schedule_time": "2026-08-09 13:08",
    },
    timeout=30,
)
res.raise_for_status()
print(res.json())
```

Response:

```json
{
  "success": true,
  "balance": {
    "deducted": 1,
    "remaining": 1649
  },
  "data": {
    "recipients_count": 1,
    "status": "queued",
    "task_id": "6f461b5a-7d35-4d55-97b0-00e420bf563b"
  }
}
```

- The balance is checked when you schedule and again when it fires.

#### Check delivery status

`GET /api/v1/status/{task_id}`

Uses the `task_id` from the send response. You can only read your own messages.

```python
import os
import requests

res = requests.get(
    "https://sms.sasusync.com/api/v1/status/6f461b5a-7d35-4d55-97b0-00e420bf563b",
    headers={"X-API-Key": os.environ["SASUSYNC_API_KEY"]},
    timeout=30,
)
res.raise_for_status()
print(res.json())
```

Response:

```json
{
  "success": true,
  "status": "completed",
  "task_id": "6f461b5a-7d35-4d55-97b0-00e420bf563b",
  "delivery_status": "delivered",
  "message": "Found 1 message(s)",
  "summary": {
    "by_status": {
      "delivered": 1
    },
    "total": 1
  },
  "messages": [
    {
      "message_id": "e1cdeb12-093a-454d-adce-bc72e8ec175b",
      "recipient": "233248627968",
      "status": "delivered",
      "created_at": "2026-08-21T18:10:07.477061",
      "delivered_at": "2026-08-21T18:10:17.187007"
    }
  ]
}
```

- Prefer a webhook. Poll only as a fallback.
- Read `delivery_status`, not `status` - `status` is the task's own lifecycle (`completed`/`processing`), the same for a batch that landed perfectly and one where everybody bounced. `delivery_status` is always one of: `sent` (accepted by the network, not yet confirmed), `delivered`, `failed`, `partial` (a bulk send where some recipients got it and some didn't), `expired`, `pending` (still in flight with no single outcome yet), or `unknown` (no record found for that id).
- A batch of one recipient carries its detail in `messages[]`; a bulk send summarises it in `summary.by_status` instead - `delivery_status` already reads whichever shape applies, so it is the one field safe to parse either way.
- A sandbox message answers from its own record, instantly: `{"sandbox": true, "status": "delivered", "delivery_status": "delivered", "message": "..."}` - nothing is sent anywhere to check.
- A message that reached a final status more than a day ago answers from our own record in the same short form: `{"status": "delivered", "delivery_status": "delivered"}`.
- If the network is slow to answer, you get the last status we recorded with `"stale": true` instead of an error - ask again in a minute. Repeat polls within a minute return the same answer.

#### Check the balance

`GET /api/v1/balance`

Both `/api/v1/balance` and `/api/v1/check/balance` return this.

```python
import os
import requests

res = requests.get(
    "https://sms.sasusync.com/api/v1/balance",
    headers={"X-API-Key": os.environ["SASUSYNC_API_KEY"]},
    timeout=30,
)
res.raise_for_status()
print(res.json())
```

Response:

```json
{
  "success": true,
  "sms_credits": 1650,
  "main_balance": "25.00",
  "currency": "GHS",
  "sms_sendable": 2150,
  "otp_sendable": 825,
  "otp_voice_sendable": 113,
  "voice_sendable": 100,
  "rates": {
    "sms": "0.0500",
    "otp_sms_credits": 2,
    "otp_voice": "0.2200",
    "voice": "0.2500"
  }
}
```

- `sms_sendable` is credits plus what the cash balance covers. Alert your own team before it reaches zero.

### OTP

#### Generate a one-time code

`POST /otp/generate`

We generate the code, deliver it, and remember it. Never store or compare codes yourself.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `number` | string | yes | The recipient |
| `sender_id` | string | yes | An approved sender ID |
| `message` | string | yes | Must contain `%otp_code%`, max 160 characters |
| `medium` | string | no | `sms` (default) or `voice` |
| `otp_type` | string | no | `numeric` (default) or `alphanumeric` |
| `expiry` | integer | no | 1 to 60 minutes, default 5 |
| `length` | integer | no | 4 to 8 characters, default 6 |

```python
import os
import requests

res = requests.post(
    "https://sms.sasusync.com/otp/generate",
    headers={"X-API-Key": os.environ["SASUSYNC_API_KEY"]},
    json={
        "number": "233552148347",
        "sender_id": "YourApp",
        "message": "Your code is %otp_code%. It expires in 5 minutes.",
        "medium": "sms",
        "otp_type": "numeric",
        "expiry": 5,
        "length": 6,
    },
    timeout=30,
)
res.raise_for_status()
print(res.json())
```

Response:

```json
{
  "success": true,
  "otp_id": 4417,
  "message": "OTP sent"
}
```

- `%otp_code%` is mandatory in the message and is replaced with the generated code.
- One pending code per number. Asking for a second while one is live returns 429.
- The code is never in the response. It goes to the handset only.

#### Verify a one-time code

`POST /otp/verify`

Three attempts, then the code is dead. Verification is free.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `number` | string | one of | The number the code went to. Omit if you send `otp_id`. |
| `otp_id` | integer | one of | The id `/otp/generate` returned, so you need not keep the number. An id your application did not send is refused. |
| `code` | string | yes | What the user typed |

```python
import os
import requests

res = requests.post(
    "https://sms.sasusync.com/otp/verify",
    headers={"X-API-Key": os.environ["SASUSYNC_API_KEY"]},
    json={
        "number": "233552148347",
        "code": "123456",
    },
    timeout=30,
)
res.raise_for_status()
print(res.json())
```

Response:

```json
{
  "success": true,
  "message": "OTP verified",
  "verified": true,
  "reason": "verified"
}
```

- **Branch on `verified`, not on the status code.** A wrong code is a **200** with `verified: false` and a `reason` — it is an answer, not an error. A non-2xx means the call itself failed and says nothing about whether the code was right.
- `reason` tells you which 'no' it was: `wrong_code` for a mistyped code, `expired` once it has timed out, `no_pending_code` when nothing was ever sent to that number, and `already_verified` when the code was right but has already been spent — usually a double submit, and worth treating as done rather than as a failure. A wrong code also carries `attempts_remaining` where the provider reports it; three wrong tries kill the code.

#### How many codes can still be sent

`GET /otp/balance`

Derived from SMS credits, because an OTP over SMS is billed in them.

```python
import os
import requests

res = requests.get(
    "https://sms.sasusync.com/otp/balance",
    headers={"X-API-Key": os.environ["SASUSYNC_API_KEY"]},
    timeout=30,
)
res.raise_for_status()
print(res.json())
```

Response:

```json
{
  "success": true,
  "otp_sendable": 825,
  "otp_voice_sendable": 113,
  "sms_credits": 1650,
  "main_balance": "25.00",
  "currency": "GHS",
  "credits_per_otp": 2,
  "voice_otp_cost": "0.2200"
}
```

### Voice

#### Send a voice message

`POST /voice/v1/send`

Send `text` to be read aloud, or upload `voice_file` as multipart.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `recipients` | string or array | yes | One number or many |
| `text` | string | no | The words to read aloud |
| `voice_file` | file | no | An audio file, sent multipart instead of JSON |

```python
import os
import requests

res = requests.post(
    "https://sms.sasusync.com/voice/v1/send",
    headers={"X-API-Key": os.environ["SASUSYNC_API_KEY"]},
    json={
        "recipients": ["233552148347"],
        "text": "Your delivery arrives today between 2 and 4 PM.",
    },
    timeout=30,
)
res.raise_for_status()
print(res.json())
```

Response:

```json
{
  "success": true,
  "balance": {
    "deducted": "0.2500",
    "remaining": "24.75"
  },
  "data": {
    "recipients_count": 1,
    "status": "queued",
    "task_id": "b1f4c9d2-3a77-4e10-9c22-7f0e5a1b8d43"
  }
}
```

- Send either `text` or `voice_file`, not both.
- Voice is billed to the main balance, not to SMS credits. A brand-new account has credits but no cash, so the first voice call returns 402 until the main balance is topped up.

### Sender IDs

#### Register a sender ID

`POST /sender/id/register`

Queues the name for review. Nothing reaches the networks until it is approved.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `sender_name` | string | yes | Max 11 characters |
| `purpose` | string | yes | What it will be used for. A vague purpose is the usual cause of a slow review. |

```python
import os
import requests

res = requests.post(
    "https://sms.sasusync.com/sender/id/register",
    headers={"X-API-Key": os.environ["SASUSYNC_API_KEY"]},
    json={
        "sender_name": "YourBrand",
        "purpose": "Order confirmations for our customers",
    },
    timeout=30,
)
res.raise_for_status()
print(res.json())
```

Response:

```json
{
  "success": true,
  "sender_name": "YourBrand",
  "status": "pending",
  "review_status": "pending_review",
  "message": "Request submitted. Sender IDs are reviewed before registration; poll /sender/id/status for the outcome."
}
```

- A name already registered on the platform returns 409.
- Verification is instant; approval usually completes in 5 to 10 minutes. Use the sandbox meanwhile.

#### Check a sender ID's status

`GET /sender/id/status?sender_name={name}`

Returns `pending`, `approved`, `rejected` or `not_found`.

```python
import os
import requests

res = requests.get(
    "https://sms.sasusync.com/sender/id/status?sender_name=YourBrand",
    headers={"X-API-Key": os.environ["SASUSYNC_API_KEY"]},
    timeout=30,
)
res.raise_for_status()
print(res.json())
```

- Only `approved` can be used on a live send.

---

## Webhooks

Optional. Set a URL against an API key in the portal and a signing secret is
issued at the same moment, shown once. Without a webhook, delivery outcomes are
read from the portal instead — sending works either way.

Per-message events: `sent` (accepted by the network), `delivered` (reached the
handset), `failed` (wrong number, phone off, blocked), `expired` (never delivered
in time).

```json
{
  "event": "delivered",
  "timestamp": "2026-08-02T10:05:15.123456",
  "data": {
    "message_id": "88cc01a5-ca7a-47b8-8fff-b6bcd1fcbe29",
    "recipient": "233552148347",
    "status": "delivered"
  }
}
```

Account-level events, sent to every key on the account that has a webhook set —
these are not tied to one message, so `data.message_id` is absent:

- `low_balance` — SMS credits or main balance just crossed the low threshold.
  Fires once per episode; the next one waits for a top-up in between.
- `topup_succeeded` — a payment settled and a balance was credited.

```json
{
  "event": "low_balance",
  "data": {
    "account_id": 42,
    "sms_credits": 12,
    "wallet_balance": "1.50",
    "currency": "GHS"
  }
}
```

```json
{
  "event": "topup_succeeded",
  "data": {
    "account_id": 42,
    "kind": "main",
    "amount": "50.00",
    "credits": 0,
    "currency": "GHS",
    "sms_credits": 12,
    "wallet_balance": "51.50",
    "reference": "SASU-abc123"
  }
}
```

Requirements for the receiving endpoint:

- HTTPS only. An `http://` URL is refused when saving it.
- Answer `200` within 10 seconds; do the real work afterwards.
- Verify `X-Webhook-Signature` — the hex HMAC-SHA256 of the **raw request body**
  under the signing secret. The verification function is in the client above.
- Expect repeats and out-of-order delivery. Key on `data.message_id`.

Delivery reports are best-effort: not every network reports back. Treat `sent` as
"handed over", not "read", and never block your own flow waiting for `delivered`.

---

## Errors

Failures return the HTTP status and a `detail` string written for a person.

| Code | Meaning | What to do |
| --- | --- | --- |
| `400` | Bad request | Fix the request. Do not retry unchanged. |
| `401` | Invalid or missing API key | Check the header. The key may have been revoked. |
| `402` | Out of credit, or this key's own usage cap is reached | The message says which. Top up, or raise the cap in the portal. |
| `403` | Sender not approved, account suspended, or this key is IP-restricted and the call came from an address not on its list | The message names the sender IDs that may be used, or the address refused. |
| `404` | Not found | The id does not exist, or belongs to someone else. |
| `409` | Already taken | Usually a sender ID someone else holds. |
| `422` | Validation failed | The body names the offending field. |
| `429` | Too many requests, or an OTP is still pending for that number | Back off and retry. |
| `500` | Server error | Retry with backoff. |
| `503` | Upstream unavailable | Retry with backoff. |

---

## Integration checklist

- [ ] API key read from the environment, not committed.
- [ ] Every call goes through one client module, so retries and errors are handled once.
- [ ] Built and tested against `/smssandbox/v1/send` before any live send.
- [ ] A sender ID is registered and shows `approved` before switching to `/api/v1/send`.
- [ ] Retries cover only 429 and 5xx, with backoff.
- [ ] A timeout triggers a status lookup, never a blind resend.
- [ ] Long messages are counted in parts, and `balance.deducted` is what gets logged.
- [ ] If webhooks are used: raw-body signature check, 200 within 10 seconds, repeats are no-ops.
- [ ] The balance is checked on a schedule and someone is alerted before it hits zero.

