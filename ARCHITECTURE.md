# Payrolla — Architecture

A map of the codebase for engineers new to the project. It describes the shape of
the application, the request lifecycle, the major modules, and the cross-cutting
concerns (tenancy, permissions, auditing). For the isolation guarantees see
[MULTI_TENANT.md](MULTI_TENANT.md); for the route-level scoping table see
[AUDIT.md](AUDIT.md).

## Big picture

Payrolla is a **server-rendered Flask application** with an application factory
(`app/create_app`), organised as a set of **blueprints** mounted under a shared
layout. Pages are Jinja templates progressively enhanced with **htmx** for partial
updates (live distribution status, staged upload overlays, toasts). There is no
separate SPA front-end.

Two logical planes share the codebase and are separated by tenancy, not by URL
guessing:

```
                       ┌─────────────────────────────────────────┐
   Platform operator → │  Oversight / control plane              │
   (client_company_id  │  dashboards · clients · payroll lifecycle│
        IS NULL)       │  risk gate · statutory · audit · dist.   │
                       └─────────────────────────────────────────┘
                                     ▲  one app, one DB
                                     ▼
                       ┌─────────────────────────────────────────┐
   Client company    → │  Tenant plane (/company)                │
   (client_company_id  │  own employees · runs · payslips ·       │
        = a company)   │  distribution · expenses · notifications │
                       └─────────────────────────────────────────┘
```

## Request lifecycle

1. **`create_app()`** (`app/__init__.py`) loads config from the environment,
   resolves the database URI (Postgres in prod, SQLite locally), wires
   extensions (SQLAlchemy, Login, CSRF, Migrate), registers Jinja filters and
   permission predicates as template globals, registers every blueprint, and
   configures ORM mappers eagerly (to avoid a first-request mapper race under
   gunicorn threads + the background worker).
2. **Auth** — `Flask-Login` loads the user; `@login_required` /
   `@role_required` / the tenancy decorators gate the view.
3. **Tenancy** — the active tenant is resolved **only** from
   `current_user.client_company_id` through `app/tenancy.py`; every tenant-scoped
   query goes through `tenant_query` / `tenant_get_or_404`.
4. **CSRF** — every mutating request must carry a session-bound token (hidden
   form field or `X-CSRFToken` header for htmx). Provider webhooks are exempt
   because they authenticate by HMAC/shared secret.
5. **Domain events + audit** — state transitions append to the `AuditTrail` and
   emit `DomainEvent`s that fan out to per-user `Notification`s.

## Module map

### Cross-cutting core

| Module | Responsibility |
|---|---|
| `app/__init__.py` | Application factory, config, extension wiring, blueprint registration, the inline distribution worker, and the branding/identity context processor. |
| `app/roles.py` | The role vocabulary and plane predicates (`is_platform_user`, `is_tenant_user`). Plane is decided by `client_company_id`, never by role. |
| `app/tenancy.py` | **The single multi-tenancy choke point** — tenant resolution, auto-scoped queries, `tenant_get_or_404`, and the `platform_required` / `tenant_required` / `tenant_role_required` decorators. |
| `app/permissions.py` | Capability predicates (`can_approve_run`, `can_distribute_run`, …) — one source of truth for nav/action gating, reused by templates and views. |
| `app/auth.py` | Login/logout and `role_required`. |
| `app/audit.py` | Audit-trail blueprint and helpers. |
| `app/events.py` | Domain-event append + notification fan-out (`record_event`). |
| `app/money.py` | Decimal money arithmetic used by the compute paths. |
| `app/validators.py` | Pre-import validation warnings for uploaded workbooks. |
| `app/htmx_utils.py` | Small helpers for htmx responses/toasts. |

### Blueprints (routes)

| Blueprint | Prefix | Plane | Responsibility |
|---|---|---|---|
| `main` (`routes.py`) | `/` | both | Landing, health, dashboards, client management, search. |
| `auth` | — | both | Login/logout. |
| `client` (`client/`) | `/company` | tenant | The full client self-service surface. `raw.py` and `reports.py` are imported for their route registration. |
| `payroll` | `/payroll` | platform | Operator run lifecycle, calculation, edits, approvals, exports. |
| `payslip` | `/payslip` | platform | Operator payslip picker + PDF generation. |
| `distribution` (`distribution/`) | `/distribution` | both | Payslip delivery subsystem (see below). |
| `employees` | `/employees` | platform | Operator roster management. |
| `statutory` | `/statutory-rates` | platform | Statutory-rate administration (global). |
| `raw_engine` (`raw_engine/web.py`) | `/raw` | platform | Raw Hours Engine ingestion. |
| `oversight` (`oversight/`) | `/oversight` | platform | Risk-gate control plane. |
| `notifications` | `/notifications` | both | Per-user in-app inbox. |

### The two payroll engines

- **Standard import** (`app/payroll.py`, `app/excel_utils.py`,
  `app/imports/header_resolver.py`, `app/raw_import.py`) — workbook → validated
  preview → `PayrollRun` + `PayrollItem`s. Statutory figures come from
  `app/payroll_calculations/` (`hourly.py`, `salaried.py`) and `StatutoryRate`.
- **Raw Hours Engine** (`app/raw_engine/`) — a self-contained pipeline:
  `seed` (per-client wage-rate context) → `detection` / `mapping` /
  `header_resolver` (locate the data) → `thin` (parse the hours upload) →
  `compute` + `calc/` (PAYE, SSNIT, overtime/bonus tax, net) →
  `exports/` (wage sheet, GRA return, bank routing, payslips, reusing the shared
  writers).

### Distribution subsystem (`app/distribution/`)

A background, DB-backed queue so a large send never blocks a web request:

- `queue.py` — `DistributionBatch` state machine + the worker loop
  (`SELECT … FOR UPDATE SKIP LOCKED`), inline or as a dedicated process.
- `service.py` — per-run send orchestration, idempotency, and retry state.
- `channels.py` / `render.py` — SMS / WhatsApp / email backends behind one
  interface; each defaults to a `console` backend.
- `receipts.py` / `webhooks.py` — provider delivery receipts (secret-verified).
- `throttle.py`, `sla.py`, `analytics.py`, `dashboard.py`, `history.py` — rate
  limiting, SLA monitoring, analytics, and operator views.

## Data model

22 SQLAlchemy models in `app/models.py`. Tenant ownership is either **direct**
(a `client_company_id` column) or **via the parent run** (child tables joined
through `payroll_run`); `app/tenancy.py` encodes both strategies once. See
[MULTI_TENANT.md#how-each-table-is-scoped](MULTI_TENANT.md) for the full table.

Core entities: `User`, `ClientCompany`, `Employee`, `PayrollRun`, `PayrollItem`,
`PaymentVoucher`, `Remittance`, `Expense`, `StatutoryRate`, `WageRateProfile`,
`RawPayEntry` / `RawUploadArchive`, plus the delivery (`PayslipDelivery`,
`DistributionBatch`, `IdempotencyKey`) and activity (`AuditTrail`, `DomainEvent`,
`Notification`) tables.

## Schema management

Schema changes are **additive Alembic migrations** (`migrations/versions/`).
Locally, `AUTO_INIT_DB=true` runs `db.create_all()` + a starter seed for
convenience; in production `AUTO_INIT_DB=false` and `flask db upgrade` owns the
schema (run on deploy — see [DEPLOYMENT.md](DEPLOYMENT.md)).

## Conventions

- **Tenancy is never optional:** a tenant-scoped read/write goes through
  `tenant_query` / `tenant_get_or_404`, never bare `Model.query`.
- **Permissions live in `permissions.py`,** not inline role lists.
- **Money** is stored as float and computed as `Decimal` via `app/money.py`; it
  is not converted to integer pesewas (parity with the reference figures).
- **Product identity** is read from config (`APP_NAME`, …), never hardcoded in a
  template.
- Modules stay under ~500 lines where practical; the known exceptions
  (`payroll.py`, `excel_utils.py`, `models.py`) are tracked as deferred debt.
