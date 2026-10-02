# SMS payslip distribution (SasuSync)

**Status:** draft for review. Phase 0 is done; nothing is built yet.
**Written:** 29 Sep 2026, against `357ea7a` (branch `chore/finish-payrolla-rename`). Alembic head `a1c5e9b73f40`.
**Owner:** Sina
**Repo:** `Desktop\PMVP\pmvp-v1`. The rename script hasn't been run, so work here.
**Scope:** SMS only. Email and WhatsApp are on hold and their behaviour does not change.

## Goal

From an approved payroll run, an operator (or the company's own admin) sends every employee an SMS
with a link to their payslip. It must work on the free Render plan, show live progress, never
double-send, and leave a record of what was sent to whom. It's the end-to-end demo path for pitching,
and it has to be good enough for a first paying client.

## What changed since the first draft

Phase 0 found that `app/distribution/` already does most of what the first draft proposed to build.
So this plan **extends the existing system**. The `OutboxMessage` table, the `app/sms/` package and
the htmx batch loop are gone.

| Already exists | Where |
|---|---|
| One row per message: run, payslip, channel, recipient, status, attempts, provider message ID, delivery status | `PayslipDelivery`, [models.py:660-706](../app/models.py) |
| Batch queue, one unfinished batch per run, claim with `FOR UPDATE SKIP LOCKED`, recovery of stuck batches | `DistributionBatch`, [queue.py:142-429](../app/distribution/queue.py) |
| A background worker on the free plan: a thread in the web process, on by default in production | [__init__.py:579-582, 1170-1200](../app/__init__.py) |
| A separate worker for a paid plan, already written | `Procfile` `worker:` line, `flask distribution-worker` |
| Signed, expiring, revocable `/p/<token>` links, a public payslip page and a PDF | [tokens.py](../app/distribution/tokens.py), [__init__.py:348-383](../app/distribution/__init__.py) |
| Sender classes behind `get_sender()`, console backend by default | [channels.py](../app/distribution/channels.py) |
| Delivery webhooks (Hubtel, WhatsApp), exempt from CSRF | [webhooks.py](../app/distribution/webhooks.py), [receipts.py](../app/distribution/receipts.py) |
| Operator and tenant send pages with 3-second status polling, cancel, schedule, duplicate-click protection | `distribution/run_status.html`, `client/distribute.html`, `macros/distribution.html` |
| Recipients logged as a keyed hash, message bodies withheld from logs | `recipient_fingerprint`, `_body_for_log` in channels.py |

## Decisions (final, 29 Sep)

1. **Extend the existing system.** No parallel outbox.
2. **Automatic retries only for 429 and 5xx, at most 3 attempts.** Permanent failures and ambiguous
   sends are never retried automatically. A timeout becomes `unknown`.
3. **Keep tenant-admin sending** (`DISTRIBUTION_SEND_ROLES = {client_admin}`).
4. **Short-code links:**
   - random code of 10+ characters; the plan uses 12
   - only a hash of the code is stored
   - each code has its own `expires_at` (30 days for SMS)
   - codes are revocable, and the route is rate-limited
5. **Store the template version, not the message body.**
6. **Restore the leading 0 on 9-digit numbers** with a valid Ghana mobile prefix, and reject
   everything else. Normalise at send time only; production data is never rewritten.
7. **Keyed hash in logs; masked number (`23324***347`) on screen only.**
8. **Keep the MoMo fallback.** The confirm step shows three counts: to phone, to MoMo, no contact.
9. **The SMS is link-only, GSM-7, with no em dash.**
10. **`PUBLIC_BASE_URL` is required (fail-closed) whenever `SMS_BACKEND` is a real provider.** Add it
    to `render.yaml`, `DEPLOYMENT.md` and `.env.example`.
11. **SMS is blocked on `APP_ENV=desktop`.**

## Production facts (Sina, read-only, 29 Sep) and what they mean

| Fact | What it means |
|---|---|
| 198 employees, **0 with a phone** | Every SMS goes through the MoMo fallback. The roster `phone` column isn't used in production. |
| 165 with a MoMo number only, all 9-digit (Excel stripped the zero) | Decision 6 is what makes these 165 (83%) reachable. Without it, SMS reaches no one. |
| 33 with no contact | They show up on the confirm step. No channel reaches them until the roster is fixed. |
| 0 duplicate (item, channel) delivery rows | Phase 2 can add the unique constraint safely. |
| Highest `payroll_item` ID is 429 | Doesn't matter now: short codes are a fixed length. |
| All 33 existing SMS deliveries failed with "no contact on roster", some after 4 attempts | 3 automatic attempts plus 1 manual "Resend failed". Manual resend has no cap ([__init__.py:583-587](../app/__init__.py)). Retrying "no contact" automatically was pointless, and it becomes a permanent failure below. These rows stay as they are. |

## Working rules

- Show raw evidence (diffs, test output, query results) for every claim. Summaries alone don't count.
- `payroll.py` and `excel_utils.py` get no new code. `models.py` gets only the minimum wiring: new
  status constants and new `PayslipDelivery` columns. Other new code goes in new modules, each
  under 500 lines.
- `queue.py` (624 lines) and `dashboard.py` (544) are already over 500 lines. They get one-line
  call sites only, never new logic.
- **Never touch production.** Any script that loads the app sets `SKIP_DOTENV=true` and uses a local
  database. No production queries. Sina runs any production check.
- One commit per phase, on branch `feature/sms-distribution`. No push or deploy without Sina's
  go-ahead.
- Never log API keys, full phone numbers or message bodies. Logs use `recipient_fingerprint()`.
- Baseline for "green": **820 passed, 44 skipped**. The 44 skips are fixture-gated raw-engine tests.

---

## Phase 1: SasuSync sender, phone rules, config guards (no migration)

SasuSync's Python integration brief is saved at `docs/vendor/sasusync-agent.md` (from
`https://sms.sasusync.com/docs/agent.md?lang=python`, fetched 2026-10-01). The base URL, request
shape and response fields come from that file, not from guesses.

**Before any sandbox test:** the brief says the sandbox does not waive sender ID approval. An
unapproved `sender` returns 403 in sandbox exactly as it does live, so the sender ID must be
registered and `approved` on the SasuSync account first. Registration is free.

**Where Payrolla deviates from the vendor brief.** SasuSync also publishes a "build with AI" prompt.
Where it conflicts with this plan, follow this plan:

- **Don't use the brief's reference client.** Use `_http_post` (stdlib `urllib`) as described below.
  Three reasons:
  - It imports `requests`, which isn't in `requirements.txt`.
  - It reads `os.environ["SASUSYNC_API_KEY"]` when the module is imported. The app would crash at
    boot wherever the key is unset (tests, desktop, local dev). That's the same pattern as the
    `PAYSLIP_TOKEN_KEY` crash loop.
  - Its `_request()` retries a send after a network exception, and it sleeps inside the call (up to
    7 seconds over its attempts), which blocks a worker thread. Retries here go through
    `next_retry_at` and the existing sweep instead.
- **"On a timeout, check the message status" isn't possible for a send.** A send that times out never
  returns a `task_id`, and the status endpoint needs one. A timeout becomes `unknown` and is settled
  as Q6 describes.
- **Don't edit `docs/vendor/sasusync-agent.md`.** Keep it identical to SasuSync's copy, so a newer
  version can be diffed against it. The brief's integration checklist gets answered in the Phase 1
  PR description instead, and the two deviations above are named there.

**`app/distribution/phones.py`** (new, pure functions)

- `normalise_gh_mobile(raw) -> str | None` returns `233XXXXXXXXX`, or None if rejected.
  - Strip spaces, dashes, dots and brackets first.
  - Accept `0XXXXXXXXX`, `+233XXXXXXXXX` and `233XXXXXXXXX`.
  - Accept a 9-digit number and restore the leading 0.
  - In every form, the two digits after the 0 (or after 233) must be in `GH_MOBILE_PREFIXES`.
  - Reject everything else. That includes landlines (`0302…`), wrong lengths and letters.
- `GH_MOBILE_PREFIXES` is one constant:
  - Telecel: `20, 50`
  - MTN: `24, 25, 53, 54, 55, 59`
  - AirtelTigo: `26, 27, 56, 57`
  - `23` (Glo) and `28` (Expresso) are excluded by decision (Q1, 2026-10-01): Payrolla does not
    serve those networks.
- `mask_msisdn(number)` returns `23324***347`, for templates only.
- Called at send time and by the confirm-step counter. Roster and payroll rows are never rewritten.
  `PayslipDelivery.recipient` stores the normalised number that was actually sent.
- **Rule (decided 2026-10-01): no phone or MoMo number is used, counted or badged unless
  `normalise_gh_mobile` accepts it.**

**Contact predicate (Q4).** `has_contact` ([models.py:119-123](../app/models.py)) becomes: email,
or a roster `phone` or `momo_number` that `normalise_gh_mobile` accepts. Update its docstring, which
currently says MoMo isn't a delivery channel. The import-time `no_contact` warning in
`crossref_employee_records` ([payroll.py:132-137](../app/payroll.py)) has its own copy of the check
(`not e.email and not e.phone`). Replace that with the property, so there is one predicate again.

**`SendResult`** (channels.py): add optional fields, keeping the positional order.

```python
units: int | None = None
retryable: bool | None = None   # None = legacy sender, keeps today's auto-retry
ambiguous: bool = False         # may have been sent; never re-sent automatically
```

Also add `delivery_id: int | None = None` last on `OutboundMessage`, so the sender can pass it as
metadata.

**`app/distribution/sasusync.py`** (new): `SasuSyncSmsSender(Sender)`, `provider = "sasusync"`

- POST to the endpoint from the vendor brief, or to the sandbox endpoint when
  `SASUSYNC_SANDBOX=true`.
- `X-API-Key` header, 30-second timeout, `metadata={"delivery_id": <id>}`, sender `SMS_SENDER_ID`.
- Uses stdlib `urllib` through the existing `_http_post`. No new dependency.
- Responses are classified like this:

| Response | Result |
|---|---|
| 2xx, including `queued: true` | `ok`, store the job ID as `message_id`, store `units` |
| 400, 402, 403, 422 | failed, `retryable=False` |
| 429, 500, 503 (and any other 5xx) | failed, `retryable=True` |
| Connection refused, or DNS failure (nothing was sent) | failed, `retryable=True` |
| Timeout, reset or disconnect after the request went out | `ambiguous=True` |
| Anything unclassified | `ambiguous=True`, the conservative default |

**Wiring**

- `get_sender("sms")` returns `SasuSyncSmsSender` when `SMS_BACKEND=sasusync`.
- `simulated_channels()` treats `sasusync` (and `hubtel`) as a real SMS backend.

**Config** (env only; names go in `.env.example`, `render.yaml` and `DEPLOYMENT.md`)

```
PUBLIC_BASE_URL=             # e.g. https://pmvp-v1.onrender.com
SMS_BACKEND=console          # console | hubtel | sasusync
SMS_SENDER_ID=Payrolla       # existing variable, reused
SASUSYNC_API_KEY=
SASUSYNC_BASE_URL=           # from the vendor brief
SASUSYNC_SANDBOX=true
SASUSYNC_WEBHOOK_SECRET=
PAYSLIP_SMS_LINK_DAYS=30
PAYSLIP_LINK_RATE_PER_MIN=30 # per IP, for /s/<code>
```

`render.yaml` is inert: the live service is configured in the Render dashboard. So every variable
also has to be **set in the dashboard before the deploy that adds its guard**. `DEPLOYMENT.md` must
say this, because the `PAYSLIP_TOKEN_KEY` crash loop happened exactly that way.

**Boot guards in `create_app`.** Each one fails closed, the same way the `SECRET_KEY` and
`PAYSLIP_TOKEN_KEY` guards do.

- `SMS_BACKEND` isn't `console` and `PUBLIC_BASE_URL` is unset: refuse to boot, in any environment.
  Without it the worker thread builds messages with **no link**, because it runs outside a request
  ([tokens.py:140-144](../app/distribution/tokens.py)). In production, also require `https://`.
- Production with `SMS_BACKEND=sasusync` and no `SASUSYNC_API_KEY`, `SASUSYNC_BASE_URL` or
  `SMS_SENDER_ID`: refuse to boot.
- `APP_ENV=desktop` with any `SMS_BACKEND` other than `console`: refuse to boot.

**Desktop block.** When `IS_DESKTOP` is true:

- the SMS option is hidden on both send pages
- `auto` routing skips SMS
- an explicit SMS send is refused at enqueue with a flash message

**Phase 1 tests**

- Phone normalisation accepts and rejects the right inputs, including the 9-digit restore and the
  prefix check.
- Every row of the classification table, with `_http_post` or `urlopen` mocked (no network).
- A SasuSync send writes neither the API key nor the full number to captured logs.
- The three boot guards.
- The desktop block.
- `has_contact` and the import `no_contact` warning:
  - a valid MoMo number counts
  - a 9-digit MoMo number counts once the leading 0 is restored
  - an invalid number, a landline or a 023/028 number doesn't count
  - email still counts

---

## Phase 2: delivery state, short links, SMS template (migration 1)

**Migration** (Alembic, on top of `a1c5e9b73f40`)

- `payslip_delivery` gains:
  - `units` (int, nullable)
  - `claimed_at` (datetime, nullable)
  - `template_version` (String(32), nullable)
- The index `ix_payslip_delivery_item_channel` becomes a **unique** constraint on
  `(payroll_item_id, channel)`. Production has 0 duplicates (checked 29 Sep).
- A new table, `payslip_link` (below).
- Use `batch_alter_table` for SQLite.
- `downgrade()` must actually work.
- It must build from empty on SQLite and on a local Postgres. Check Postgres by hand, since CI only
  runs SQLite.

**Statuses:** add `DELIVERY_SENDING = "sending"` and `DELIVERY_UNKNOWN = "unknown"`. There's no
separate `delivered` status: as today, a confirmed delivery stays `sent` and sets
`provider_status="delivered"` and `delivered_at`. The UI already shows that.

**Claim before send** (`_attempt_send`, service.py)

1. For a new row, insert it as `pending` and commit.
2. Claim it with a conditional update:
   `UPDATE payslip_delivery SET status='sending', claimed_at=now, attempts=attempts+1
   WHERE id=:id AND status IN ('pending','failed')`.
   Continue only if exactly one row changed. Commit before calling the provider. This works on
   SQLite and Postgres, and it stops a batch and the retry sweep from sending the same row.
3. Call the provider, then write the outcome:
   - `sent`
   - `failed`, with `next_retry_at` set only when `retryable` and attempts < 3
   - `unknown` when `ambiguous`

**Skip rules.** `distribute_run` skips any existing row in `sent`, `sending` or `unknown`.
"Resend failed" picks up `failed` only.

**Stale claims.** A `sending` row whose `claimed_at` is more than 10 minutes old becomes `unknown`,
with the error "Sending stopped mid-way; this message may have been sent." One provider call is
capped at 30 seconds, so a live send can never be that old.

- The function lives in service.py or a new `recovery.py`.
- `queue.drain_once` calls it on one line, before `reclaim_stale_batches`.

**Monitoring `unknown`** (Q7, decided 2026-10-01). The existing monitor only knows `sent` and
`failed`. Without this, a timed-out send shows as "failed" in the completion notice while "Resend
failed" skips it, and no SLA alert ever fires for it.

- **Run summary** (`distribute_run`, [service.py:304-308](../app/distribution/service.py)): count by
  the delivery's status after `_attempt_send`, not by its True/False return. The summary gains
  `unknown` and `unknown_workers`. `failed_workers` no longer includes `unknown` rows.
- **Batch:** a nullable `distribution_batch.unknown_count`, added in migration 1 and written beside
  `sent_count` / `failed_count` in `queue.py`.
- **Completion notice** (`notify_completion`): when `unknown > 0`, the level is `warning` and the
  text says "N may have been sent. Check SasuSync before resending." It never says "completed" with
  no qualifier. `unknown` doesn't count toward `DISTRIBUTION_FAILURE_ALERT_RATE`. Platform admins
  hear about it through the SLA breach below, so they aren't told twice.
- **SLA breach** (`sla.evaluate_sla`): a new breach type, `unknown`. It fires for `unknown` rows
  whose `claimed_at` is older than `SLA_UNKNOWN_MINUTES` (default 30; 0 turns it off). It reuses
  `SLA_ALERT_COOLDOWN_SECONDS`, so it repeats hourly while any row is unresolved. Add it to
  `.env.example` next to the other SLA settings. No Render change is needed, because the default
  applies.
- **Dashboard** (`dashboard.py`): `delivery_counts` already groups by status. Show `unknown` as its
  own "Unconfirmed" count beside sent, failed and pending, linking to the run's status page. The
  dashboard links and never acts (constitution).

**Retry policy (decision 2)**

- "No contact" and "invalid number" are pre-send failures on every channel. They're permanent, so
  they never retry automatically.
- SasuSync results follow the Phase 1 table.
- Senders that don't classify yet (`retryable=None`: Hubtel, WhatsApp, SMTP) keep today's
  behaviour. See A1.
- A failure reported by the provider after the send (webhook) is recorded as `failed` and never
  retried automatically. See A2.
- `unknown` is never touched by the retry sweep, "Resend failed" or batch recovery.

**Short links: `app/distribution/links.py`** (new: model, mint, resolve, revoke)

```python
class PayslipLink(db.Model):
    id, code_hash (String(64), unique, index), payroll_item_id (FK, index),
    payslip_delivery_id (FK, nullable), token_version (int),
    expires_at, revoked_at (nullable), created_at
```

- **Code:** 12 base62 characters from `secrets` (71.5 bits).
- **Stored:** only `code_hash = HMAC-SHA256(PAYSLIP_TOKEN_KEY, code)`. The plain code exists only
  in the outgoing SMS, in memory. Rotating the key kills every code, which matches how signed
  tokens behave today.
- **Resolve:** first check the code's length and character set, so malformed codes cost no
  database hit. Then look up the hash. Reject the link if:
  - no row matches
  - it has expired
  - it has been revoked
  - `token_version` doesn't match `item.payslip_token_version`
- **Revocation:** the version check means the existing `revoke_payslip_links(item)` also kills
  short links. `revoked_at` revokes a single code.
- **Re-minting:** each attempt mints a fresh code, because the old one can't be recovered from its
  hash. When a new code is minted, the delivery's earlier codes are revoked, unless the earlier
  attempt ended `unknown` (that link may already be on the worker's phone).
- **Routes:** `GET /s/<code>` and `/s/<code>/pdf`, on the existing `payslip_link_bp`.
  - They render the same public page and PDF as `/p/<token>`.
  - Any failure returns the same 404 "link expired" page.
  - An in-process per-IP limiter, the same shape as `throttle.py` and `login_throttle.py`, returns
    429 above `PAYSLIP_LINK_RATE_PER_MIN`.
  - Responses carry `Cache-Control: no-store`.
  - The global `Referrer-Policy: strict-origin-when-cross-origin` already keeps the path out of
    cross-site requests, and the page loads only its own CSS.
- Email and web keep the 5-day `/p/<token>` links unchanged.

**SMS template** (render.py)

- `render_payslip_sms(run, client, link)` and `SMS_TEMPLATE_VERSION = "sms-link-v1"`, which is
  stored in `template_version`.
- Text: `{Company}: your {Month YYYY} payslip is ready. View (valid {N} days): {link}`
  - `N` comes from `PAYSLIP_SMS_LINK_DAYS`, so the wording and the actual expiry can't drift apart.
  - With a 43-character link and "September", the body is 105 characters without the company name.
    That leaves 55 for the name. Compute the budget in code; don't hard-code it.
- GSM-7 cleanup of the company name:
  - curly quotes become straight quotes
  - dashes become `-`
  - a non-breaking space becomes a space
  - accented letters lose their accents (NFKD)
  - any other non-GSM character is dropped
  - GSM extension characters count as 2
- Truncate the name to fit, then assert that the body is at most 160 septets and all GSM-7.
- The WhatsApp and email renderers don't change. No body is stored anywhere.

**Contact source.** A helper returns `(number, source)`, where source is one of: roster phone,
roster MoMo, payroll-row MoMo, none. It's shared by the send path and the Phase 3 counter.
It returns the first candidate in that order that `normalise_gh_mobile` accepts, and skips any
that fail. The result is "invalid number" only when at least one number existed and none passed;
it's "no contact" when there were none.

**Phase 2 tests**

- Two concurrent claims on one row: exactly one wins.
- A timeout becomes `unknown`, and is not re-sent by the sweep, by "Resend failed" or by batch
  recovery.
- A stale `sending` row becomes `unknown`.
- A 429 retries automatically, up to 3 attempts in total. A 400 and "no contact" never retry
  automatically.
- A re-run skips `sent`, `sending` and `unknown`.
- Monitoring `unknown`:
  - the run summary counts `unknown` separately and leaves it out of `failed_workers`
  - `batch.unknown_count` is written
  - the completion notice is a warning that names the unconfirmed count
  - the SLA `unknown` breach fires for a row older than `SLA_UNKNOWN_MINUTES`, not for a younger
    one, and not when the setting is 0
  - the dashboard shows the unconfirmed count
- The unique (item, channel) constraint holds.
- Short links:
  - the plain code is never in the database
  - expiry at 30 days
  - both revocation paths
  - a malformed code is a 404
  - the rate limit returns 429
- The body is at most 160 septets and GSM-7 only, for long, curly-quoted and accented company names.
- `template_version` is stored and the body isn't.
- Tenant A's `client_admin` gets 404 on tenant B's run for view, send, resend, cancel and confirm.
- The migration upgrades from empty and downgrades.

---

## Phase 3: send pages (both portals, shared macro)

- **Confirm step** before an SMS or `auto` send. It uses the existing nonce and idempotency, and no
  native `confirm()`. It shows:
  - counts: **to phone / to MoMo / no contact / invalid number**
  - the employees with no contact or an invalid number, paginated
  - estimated credits: recipients × 1. The body is built to be one segment, and SasuSync's actual
    `units` are recorded per message.
- **Status display**
  - badges for `sending` and `unknown`
  - masked numbers
  - `unknown` rows, operator portal: "May have been sent. Check the SasuSync portal, then mark it."
  - `unknown` rows, client portal: "Unconfirmed. Payrolla is checking." No action, because the
    SasuSync login is Payrolla's, not the client's.
- **Settle an `unknown` row (Q2, decided 2026-10-01).** Operator portal only.
  - `POST /distribution/run/<run_id>/delivery/<delivery_id>/settle`, with `outcome=sent` or
    `outcome=not_sent`. It uses `@role_required(*PAYROLL_ROLES)`, the same as the operator's
    "Resend failed" ([distribution/__init__.py:242-244](../app/distribution/__init__.py)). CSRF
    protected.
  - It acts only on a row that is `unknown` right now, using a conditional
    `UPDATE … WHERE status='unknown'`. Anything else returns 409. A delivery that doesn't belong to
    that run returns 404.
  - `sent`: the status becomes `sent` and `provider_status` stays empty. This is the operator's
    word, not a delivery report.
  - `not_sent`: the status becomes `failed`, with the error "Operator confirmed not sent (SasuSync
    portal)". "Resend failed" then picks it up. Because the operator confirmed the earlier link
    never arrived, the resend revokes it like any other retry.
  - Every settle writes `record_audit` with the user, the outcome and the delivery ID.
  - The row shows two small secondary buttons. They don't count against the page's one primary
    action, and there's no native `confirm()`.
  - Settling the last `unknown` row clears the SLA `unknown` breach on the next check.
- **No new polling.** The worker sends server-side, so there's no "keep this page open" notice and
  no batch loop. The existing 3-second status polling stays.
- **Where the markup goes:** `macros/distribution.html`, which must stay Bootstrap-free because
  `client/base.html` doesn't load Bootstrap. It serves `distribution/run_status.html` and
  `client/distribute.html`. The page stays in the Work plane; dashboards link to it and never
  send from it.
- **Checks:**
  - `tests/test_constitution_lint.py` passes: one primary action, pagination, and the rules for
    polling regions.
  - Keyboard focus is visible on the tenant page too
    (see `plans/focus-indicator-contrast-defect.md`).
  - Screenshot both pages with `scripts/capture_ui.py`.
- **Settle tests:**
  - `sent` and `not_sent` each move an `unknown` row and write an audit entry
  - a `not_sent` row is picked up by "Resend failed"
  - a row that isn't `unknown` returns 409
  - a delivery from another run returns 404
  - `client_admin` can't reach the route
  - the SLA `unknown` breach clears once no `unknown` rows remain

---

## Phase 4: SasuSync delivery webhook (migration 2; only after Phases 1–3 are green)

- **Endpoint:** `POST /distribution/webhooks/sasusync`, in the existing blueprint, which is already
  exempt from CSRF.
  - 404 when `SASUSYNC_WEBHOOK_SECRET` is unset.
  - Verify `X-Webhook-Signature` as HMAC-SHA256 over `request.get_data()`, compared with
    `hmac.compare_digest`. Return 401 on a mismatch.
  - Rate-limited with the same in-process limiter.
  - Respond in well under 10 seconds: parse and update, with no outgoing calls.
- **Matching:** use `metadata.delivery_id` first. Otherwise match `data.message_id` against
  `provider_message_id`. Confirm in the sandbox whether `message_id` equals the send response's
  job ID, and record the answer in this file.
- **Ordering:** a new column `provider_status_at` holds the event timestamp. Apply an event only if
  it's newer than the stored one; an identical repeat does nothing.
- **Outcomes:**
  - delivered: `provider_status="delivered"` and `delivered_at`
  - failed or undelivered: `status="failed"`, no automatic retry
  - This is also the only automatic way out of `unknown`. A delivery report for an `unknown` row
    settles it.
- **Tests:**
  - a tampered body returns 401
  - no secret returns 404
  - a valid event is applied
  - an older event is ignored
  - a repeat does nothing
  - an `unknown` row is settled by a report

---

## Before any production deploy (Sina)

1. Set these in the **Render dashboard**, before deploying the code that checks them:
   - `PUBLIC_BASE_URL=https://pmvp-v1.onrender.com`
   - `SMS_SENDER_ID`
   - `SASUSYNC_*`
   - Keep `SMS_BACKEND=console` until the sandbox check passes.
2. Apply migrations 1 and 2 to a local Postgres copy of the schema first.
3. The 33 old failed SMS rows stay as they are. Resending them fails again until the roster has
   contacts.

## Out of scope

- email and WhatsApp changes
- per-tenant sender IDs
- a paid worker service
- rewriting production phone data
- desktop SMS

## Done when

- The full suite is green against 820 passed / 44 skipped, plus the new tests listed in each phase.
- No file over 500 lines, and no new logic in `payroll.py`, `excel_utils.py`, `queue.py` or
  `dashboard.py`.
- **Manual check 1:** a sandbox run end to end on a demo payroll run, done locally with
  `SKIP_DOTENV=true` and `SASUSYNC_SANDBOX=true`.
- **Manual check 2:** once "Payrolla" is approved as a sender ID, one live send to three real
  phones (MTN, Telecel, AirtelTigo), with the payslip opening from each.

## Assumptions to confirm (A) and open questions (Q)

- **A1.** The new retry rules cover SMS and the pre-send failures. Hubtel, WhatsApp and SMTP keep
  retrying every failure as today, because those channels are on hold.
- **A2.** A failure reported later by webhook isn't retried automatically, because the report could
  be wrong and a resend could duplicate. The operator can use "Resend failed".
- **Q1. Decided 2026-10-01:** 023 (Glo) and 028 (Expresso) are excluded. Payrolla won't serve
  those networks.
- **Q2. Decided 2026-10-01:** yes. Operators can settle an `unknown` row as sent or not sent after
  checking the SasuSync portal. See "Settle an `unknown` row" in Phase 3.
- **Q3. Decided 2026-10-01:** manual "Resend failed" stays uncapped ("unlimited recovery"). It
  only picks up `failed` rows, so it can't re-send to `sent` or `unknown` workers.
- **Q4. Decided 2026-10-01:** MoMo numbers count as contact, but only numbers that pass
  `normalise_gh_mobile`. That applies everywhere: sending, counting and badges. See "Contact
  predicate" in Phase 1.
- **Q5. Decided 2026-10-01:** land the rename first. (Correction: `357ea7a` is already pushed, to
  `origin/chore/finish-payrolla-rename`. It isn't on `main`. `main` is its parent, `dd89115`, so it
  fast-forwards.) Fast-forwarding `main` auto-deploys the live Render service "Payrolla"
  (`pmvp-v1`, branch `main`, auto-deploy on commit). The commit changes comments, docs, the roster
  template filename and `render.yaml`'s `name`. That `name` binds to nothing: the live service
  wasn't created from this Blueprint. No env vars, no migration. Cut the SMS branch from `main`
  after the rename lands.
- **Q6. Decided 2026-10-01:** a timed-out send is settled by delivery reports (Phase 4) and by the
  operator's settle action (Q2). There's no API cross-check: after a timeout there's no job ID, and
  the brief has no lookup by metadata or by recipient. Also, the brief's reference `_request()`
  retries on any network exception, sends included. Don't reuse it for sends.
- **Q7. Decided 2026-10-01:** fixed in Phase 2. See "Monitoring `unknown`". The alert repeats every
  hour until each `unknown` row is settled, by the operator's settle action (Q2) or by a delivery
  report (Phase 4).