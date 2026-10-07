# Payrolla — Deployment Guide

Payrolla ships ready to deploy on **Render** (`render.yaml`), **Railway**
(`railway.toml`), or any container host (`Dockerfile`, `docker-compose.yml`),
plus a `Procfile` and `runtime.txt`. Every deployed environment must satisfy four
non-negotiables:

1. **PostgreSQL** via `DATABASE_URL` — the app refuses to boot in production on
   SQLite (ephemeral filesystems lose data).
2. **A strong `SECRET_KEY`** — the app refuses to boot in production with the
   insecure development fallback.
3. **A strong `PAYSLIP_TOKEN_KEY`, different from `SECRET_KEY`** — the app
   refuses to boot in production without it, or if the two match.
4. **`FLASK_ENV=production`** — declared explicitly. Detection fails closed (an
   undeclared environment is treated as production), but the deploy states it
   rather than relying on the platform's implicit `RENDER` variable.

## Environment variables

| Variable | Prod value | Purpose |
|---|---|---|
| `FLASK_ENV` | `production` | Declares the environment. Unset/unknown is treated as production. |
| `SECRET_KEY` | strong random | Signs session cookies. |
| `PAYSLIP_TOKEN_KEY` | strong random, ≠ `SECRET_KEY` | Signs no-login payslip links. Rotating it invalidates every outstanding link. |
| `PAYSLIP_LINK_MAX_AGE` | `432000` (5 days) | Payslip link lifetime, in seconds. |
| `DATABASE_URL` | Postgres URL | `postgres://` is auto-normalised to `postgresql://`. |
| `PERSISTENCE_REQUIRED` | `true` | Fail fast if Postgres is missing. |
| `AUTO_INIT_DB` | `false` | Schema is owned by Alembic migrations in prod. |
| `SEED_DEMO_DATA` | `false` | Never seed demo tenants/users into a real deployment. |
| `SESSION_COOKIE_SECURE` | `true` | HTTPS-only session cookie. |
| `LOG_MESSAGE_BODIES` | unset | Development only — production refuses to boot with it enabled. |
| `LOGIN_MAX_ATTEMPTS` | `5` | Failed logins per IP and per account before lockout. |
| `PUBLIC_BASE_URL` | `https://pmvp-v1.onrender.com` | Host used in payslip links. Required (and `https://` in production) whenever `SMS_BACKEND` isn't `console`. |
| `SMS_BACKEND` | `console` until the sandbox check passes, then `sasusync` | `console` logs only. Any other value is refused on a desktop install. |
| `SMS_SENDER_ID` | `Payrolla` | Must be approved on the SasuSync account. Required in production with `sasusync`. |
| `SASUSYNC_API_KEY` | secret | Required in production with `sasusync`. Never logged. |
| `SASUSYNC_BASE_URL` | `https://sms.sasusync.com` | Required in production with `sasusync`. |
| `SASUSYNC_SANDBOX` | `true`, then `false` to go live | `true` posts to the free sandbox endpoint, which delivers nothing. |
| `SASUSYNC_WEBHOOK_SECRET` | secret | Signs SasuSync delivery reports. |
| `PAYSLIP_SMS_LINK_DAYS` | `30` | Lifetime of the short link in an SMS. |
| `PAYSLIP_LINK_RATE_PER_MIN` | `30` | Requests per minute per IP on `/s/<code>`. |

Optional groups (all documented in [.env.example](.env.example)): the branding
seam (`APP_NAME`, …), distribution channels and their credentials, webhook
secrets, rate limits, retry policy, and SLA thresholds. The employer identity on
GRA returns is configured with `CHRISNAT_EMPLOYER_TIN` and `CHRISNAT_TAX_OFFICE`.

## Database migrations

Production schema is owned by **Alembic** (`migrations/`). Apply migrations on
every deploy **before** serving:

```bash
flask db upgrade
```

On Render this is baked into the start command (below). `db.create_all()` +
starter seed only run locally when `AUTO_INIT_DB=true`.

## Render

`render.yaml` defines a free web service. Key settings:

- **Build:** `pip install -r requirements.txt`
- **Start:** `flask db upgrade && gunicorn run:app --bind 0.0.0.0:10000 --timeout 300 --threads 4`
  - `--timeout 300` gives the seed/confirm path room on the free plan's slow CPU.
  - `--threads 4` keeps the `/health` probe answerable while a confirm runs
    (a single sync worker was blocking health checks and triggering restarts).
  - One worker only — the free plan's 512 MB does not fit two pandas-loaded
    workers; threads add concurrency without a second process.
- **Health check:** `/health`
- **`DATABASE_URL`** is set in the dashboard with `sync: false` so Blueprint syncs
  never overwrite it.

Go-live checklist:

1. Create a PostgreSQL database and copy its internal URL.
2. Set `DATABASE_URL`, `SECRET_KEY` and `PAYSLIP_TOKEN_KEY` (a *different* random
   value) on the web service. `render.yaml` generates the two keys for you.
3. Set `FLASK_ENV=production`, `SEED_DEMO_DATA=false`, `PERSISTENCE_REQUIRED=true`,
   `AUTO_INIT_DB=false`.
4. Deploy, then open `/admin/db-health` (admin login) and confirm it reports
   **PostgreSQL** and `DATABASE_URL Detected: Yes`.
5. Upload a payroll workbook, restart the service, and confirm the records persist.

> **Service name:** the live service (`pmvp-v1.onrender.com`) is configured in the
> Render dashboard, not by this Blueprint, so the `name` in `render.yaml` does not
> bind to it. Render fixes the `onrender.com` subdomain at creation and cannot
> rename it; serving Payrolla on its own hostname means adding a custom domain to
> the existing service, not changing this file.

### SMS (SasuSync): set the dashboard first

`render.yaml` is inert, so nothing in it reaches the live service. The SMS boot
guards refuse to start the app when their variables are missing, which means a
deploy that adds a guard before its variable exists puts the service into a
crash-restart loop. That is exactly how the `PAYSLIP_TOKEN_KEY` outage happened.

1. In the **Render dashboard**, set `PUBLIC_BASE_URL`, `SMS_SENDER_ID` and the
   `SASUSYNC_*` variables **before** deploying the code that checks them.
2. Keep `SMS_BACKEND=console` until a sandbox run (`SASUSYNC_SANDBOX=true`) has
   passed end to end. Register the sender ID on SasuSync first: the sandbox
   returns 403 for an unapproved sender, the same as live.
3. Then set `SMS_BACKEND=sasusync`, and `SASUSYNC_SANDBOX=false` only once the
   sender ID shows `approved`.

## Railway

`railway.toml` is included. Create a Railway PostgreSQL service, connect it, and set:

```env
FLASK_ENV=production
SECRET_KEY=your-secret
PAYSLIP_TOKEN_KEY=a-different-secret
DATABASE_URL=${{ Postgres.DATABASE_URL }}
AUTO_INIT_DB=true
PERSISTENCE_REQUIRED=true
SEED_DEMO_DATA=false
SESSION_COOKIE_SECURE=true
```

Start command: `gunicorn run:app --bind 0.0.0.0:$PORT`. After deploy, open
`/admin/db-health` and confirm PostgreSQL.

## Docker Compose (local prod-like stack)

`docker-compose.yml` brings up Postgres, the web process, and a dedicated
distribution worker (mirroring a production web/worker split):

```bash
make up       # docker compose up -d
make logs     # follow web logs
make down     # stop
```

The stack uses a `payrolla` Postgres role/database on a named volume (`pgdata`).
If you previously ran an older stack under a different role name, reset the volume
with `docker compose down -v` before bringing the renamed stack up.

## The distribution worker

Payslip sending runs on a **DB-backed queue**, so it survives restarts and never
blocks a web request. Two deployment shapes:

- **Inline (default):** the web process runs the worker on a background thread
  (`DISTRIBUTION_WORKER_INLINE=true`, the production default). No extra service is
  needed — the queue lives in Postgres. The scheduled-send and auto-retry sweeps
  run inside the same loop.
- **Dedicated worker:** run `flask --app run:app distribution-worker` as its own
  service (Render Background Worker, the compose `worker` service, or a Railway
  service) and set `DISTRIBUTION_WORKER_INLINE=false` on the web service so only
  the worker sends. It handles `SIGTERM` for a graceful shutdown and supports
  `--once` for cron/scheduled-job platforms. Batch claiming is row-locked
  (`FOR UPDATE SKIP LOCKED`), so inline and dedicated workers are safe to run
  together during a migration between the two.

## Notes

- Render/Railway free web filesystems are **ephemeral**. Uploaded files are only
  used transiently during parsing; all durable data lives in Postgres.
- The app normalises legacy `postgres://` URLs to `postgresql://` for SQLAlchemy.
- `/admin/db-health` never displays the database URL or password.
