# Payrolla — Security Model

How Payrolla protects tenant data, sessions, and outbound integrations. This is a
description of the controls in the codebase, not a compliance certification.

## Authentication & sessions

- **Passwords** are hashed with Werkzeug's `generate_password_hash` /
  `check_password_hash` (`User.set_password` / `check_password`). Plaintext
  passwords are never stored or logged.
- **Session fixation** is prevented by clearing the session before establishing
  the new authenticated session on login (`app/auth.py`).
- **Session cookies** are `HttpOnly`, `SameSite=Lax`, and `Secure` in production
  (`SESSION_COOKIE_SECURE`, defaults on in deployed environments). Sessions have
  an 8-hour lifetime.
- **`SECRET_KEY`** signs session cookies and payslip links. The app **refuses to
  boot in production** with the insecure development fallback — a strong random
  key must be provided (`SECRET_KEY`).

## Authorisation & tenant isolation

Authorisation is layered and centralised:

- **Plane** is decided **only** by `current_user.client_company_id`
  (`app/tenancy.py`) — never by a URL, form field, or query parameter. A tenant
  user cannot widen their horizon by editing a request.
- **Capabilities** are defined once in `app/permissions.py` and reused by both
  templates and views, so nav and server-side checks can never disagree.
- **Scoped access** goes through `tenant_query` / `tenant_get_or_404`. A
  cross-tenant object id returns **404** (never a 403 that confirms the row
  exists, never the row itself).

The guarantees and their tests are catalogued in
[MULTI_TENANT.md](MULTI_TENANT.md) and [AUDIT.md](AUDIT.md), and pinned by
`tests/test_tenant_isolation.py`, `tests/test_cross_tenant_visibility.py`, and
`tests/test_lifecycle_authorization.py`.

### Row-Level Security (defence in depth)

- **Stage 1 (implemented):** RLS is enabled on tenant tables with **no policies**
  (default-deny), so the database rejects any connection that is not the app
  role. Isolation is enforced at the application layer on every request.
  Reproducible via `scripts/rls_stage1.sql`.
- **Stage 2 (deferred):** JWT-based RLS policies that push tenant scoping into the
  database itself, behind the app layer.

## CSRF

Global CSRF protection (Flask-WTF) is on for every mutating request; the token is
carried in a hidden form field or the `X-CSRFToken` header (htmx / fetch). It is
disabled only in the test suite. Provider webhooks are the sole exemption —
they authenticate by HMAC signature / shared secret and carry no browser session
(see below).

## Outbound integrations & webhooks (fail-closed)

- **Distribution channels** (SMS, WhatsApp, email) default to a `console` backend
  — logs only, no credentials, no network. Nothing reaches a real worker until a
  channel's backend and credentials are explicitly configured.
- **Delivery-receipt webhooks** (`/distribution/webhooks/{whatsapp,hubtel}`) stay
  **disabled (404) until their secret/token is set**, so an unconfigured
  deployment cannot be spoofed. WhatsApp verifies the Meta subscription token and
  (optionally) enforces `X-Hub-Signature-256`; Hubtel checks a shared secret on
  the callback URL.

## No-login payslip links

Workers receive a tokenised link to their own payslip with no login
(`app/distribution/tokens.py`). The token is a **signed, expiring**
`URLSafeTimedSerializer` value (not encrypted) bound to a single payslip item and
the app `SECRET_KEY`; it expires after `PAYSLIP_LINK_MAX_AGE` (default 30 days)
and authorises exactly one payslip — no session, no tenant surface.

## Data handling

- **Uploaded workbooks** contain employee PII and salary data. They are streamed
  through temp/session storage during parsing; the extracted preview is stored in
  the database and the raw file is not retained as a durable artifact. Real client
  workbooks are explicitly excluded from version control (`.gitignore`).
- **`/admin/db-health`** is admin-only and reports record counts; it never
  displays the database URL or password.
- **Secrets** (`SECRET_KEY`, `DATABASE_URL`, provider credentials, webhook
  secrets) come from the environment and must never be committed. `.env` is
  git-ignored.

## Deployment hardening

- Production **requires PostgreSQL** (`PERSISTENCE_REQUIRED`); the app refuses to
  run on SQLite in a deployed environment (ephemeral filesystems lose data).
- `MAX_CONTENT_LENGTH` caps upload size (16 MB default).
- Postgres connections use `pool_pre_ping` + `pool_recycle` + TCP keepalives to
  survive pooler idle-drops.

## Reporting a vulnerability

Report suspected security issues privately to the maintainers at Sinaforte
Technologies rather than opening a public issue. Include reproduction steps and
the affected route/module.
