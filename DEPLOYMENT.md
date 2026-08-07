# Payrolla — Deployment Guide

Payrolla ships ready to deploy on **Render** (`render.yaml`), **Railway**
(`railway.toml`), or any container host (`Dockerfile`, `docker-compose.yml`),
plus a `Procfile` and `runtime.txt`. Every deployed environment must satisfy two
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

> **Service name:** the Render service is historically named
> `chrisnat-payroll-mvp` in `render.yaml`. It is a live deployment identifier —
> renaming it in the Blueprint would create a new service and orphan the running
> one and its `DATABASE_URL` binding, so it is intentionally left unchanged. Rename
> it only as a deliberate, planned infra migration.

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
