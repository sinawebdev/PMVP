# Payrolla

**Payrolla — Payroll, HR, Compliance & Workforce Management Platform**
_by Sinaforte Technologies._

Payrolla is a multi-tenant SaaS for running Ghanaian payroll end to end:
import or capture hours, compute statutory figures (PAYE, SSNIT tier-1/tier-2,
bonus tax), review and approve runs, generate payslips and statutory returns,
and **push** each worker their payslip by SMS, WhatsApp, or email. Client
companies self-serve on their own isolated plane; the platform operator runs an
oversight/control plane above every tenant.

- **Architecture:** [ARCHITECTURE.md](ARCHITECTURE.md)
- **Multi-tenancy & isolation:** [MULTI_TENANT.md](MULTI_TENANT.md)
- **Route-by-route scoping audit:** [AUDIT.md](AUDIT.md)
- **Security model:** [SECURITY.md](SECURITY.md)
- **Deployment:** [DEPLOYMENT.md](DEPLOYMENT.md)
- **Contributing & local setup:** [CONTRIBUTING.md](CONTRIBUTING.md)
- **Release history:** [CHANGELOG.md](CHANGELOG.md)

---

## What Payrolla does

Payrolla serves two audiences from one codebase, separated into **two planes**
(see [MULTI_TENANT.md](MULTI_TENANT.md)):

**Client companies** (the `/company` plane) self-serve:

- Company dashboard scoped to their own data only.
- Employee roster CRUD (self-service).
- Payroll-run upload (Excel) or raw-hours capture, with pre-import validation.
- Run and payslip views — individual PDF payslips and a whole-run ZIP.
- **Payslip distribution** by SMS / WhatsApp / email, with per-worker delivery
  tracking and retry.
- Statutory rates (read-only), expenses, audit trail, and in-app notifications.
- Per-company payslip branding packs (name, accent colour, sender, reply-to).

**The platform operator** (oversight/control plane) runs the bureau:

- Cross-tenant dashboards, client management, and search.
- The full operator payroll lifecycle (`Draft → Pending Approval → Approved →
  Processed`), calculation, edits, approvals, exports, and payment vouchers.
- The **risk gate** (`/oversight/risk`): every client-submitted run is scored
  deterministically and either auto-accepted or **held** for review.
- Statutory-rate administration, the audit trail, and the distribution
  monitoring / analytics / SLA dashboards.

### Two payroll engines

1. **Standard import engine** — reads `.xlsx` / `.xls` / `.csv` payroll
   workbooks, resolves columns from common header names, counts unique workers,
   validates, and creates a payroll run. Statutory figures are recomputed by
   Payrolla on confirm (`app/payroll_calculations/`), so uploaded PAYE/SSNIT are
   preview-only.
2. **Raw Hours Engine** (`app/raw_engine/`) — for clients who submit hours only.
   Seeds a per-client wage-rate context, ingests a thin hours upload, computes
   gross → statutory → net, and produces the same operational outputs (wage
   sheet, GRA PAYE return, bank listing, payslip PDFs).

The engines are **frozen and parity-pinned** (`tests/test_engine_parity.py`):
identical inputs always produce identical figures.

---

## Tech stack

- **Python 3.11+**, **Flask 3** (blueprints, Jinja templates, server-rendered
  HTML with htmx for partial updates).
- **SQLAlchemy 2 / Flask-SQLAlchemy**, **Flask-Migrate / Alembic** for schema.
- **Flask-Login** (auth), **Flask-WTF** (global CSRF).
- **pandas / openpyxl / xlrd** (Excel), **reportlab** (payslip PDFs).
- **PostgreSQL** in every deployed environment; **SQLite** only for local dev
  and the test suite.
- **gunicorn** in production; an in-process (or dedicated) background worker
  drains the payslip distribution queue.

Exact pinned versions are in [requirements.txt](requirements.txt).

---

## Quick start (local)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on POSIX
pip install -r requirements.txt
copy .env.example .env          # cp on POSIX — then edit as needed
python run.py
```

Open <http://127.0.0.1:5000>. On first run Payrolla creates the local SQLite
database and seeds starter users and demo tenants automatically. To (re)initialise
explicitly:

```bash
flask --app run:app init-db
```

A minimal local `.env`:

```env
SECRET_KEY=change-this-for-local
AUTO_INIT_DB=true
PERSISTENCE_REQUIRED=false
SEED_DEMO_DATA=true
```

For a Docker-based local stack (Postgres + web + distribution worker) see
[DEPLOYMENT.md](DEPLOYMENT.md) and `docker-compose.yml` (`make up`).

### Default seed logins

Seeded into a fresh local/staging database only — a real deployment provisions
its credentials in Supabase Authentication. All demo accounts use the password
`password123`.

**Platform (operator) plane** — `client_company_id` is NULL:

| Email | Role string | Capability |
|---|---|---|
| `admin@payrolla.com` | `admin` | Full operator administration |
| `operator@payrolla.com` | `payrolla_admin` | Platform superuser (SaaS operator) |
| `support@payrolla.com` | `payrolla_reviewer` | Read-only support/oversight |
| `director@payrolla.com` | `md` | Managing Director — approvals |
| `payroll@payrolla.com` | `payroll_officer` | Payroll processing |
| `accounts@payrolla.com` | `accounts_officer` | Submission, close-out |
| `operations@payrolla.com` | `operations_supervisor` | Operations view |

**Tenant (client) plane** — each bound to one company:

| Company | Code | Email | Role string |
|---|---|---|---|
| MSC Limited | `MSC` | `admin@msc.com` | `client_admin` |
| MSC Limited | `MSC` | `finance@msc.com` | `client_admin` |
| MSC Limited | `MSC` | `payroll@msc.com` | `client_preparer` |
| Acme Manufacturing Ltd | `ACME` | `admin@acme.com` | `client_admin` |
| Acme Manufacturing Ltd | `ACME` | `finance@acme.com` | `client_admin` |
| Acme Manufacturing Ltd | `ACME` | `payroll@acme.com` | `client_preparer` |

> The platform roles were once named after the founding operator
> (`chrisnat_admin` / `chrisnat_reviewer`). They now carry the product's name.
> The old spellings are still accepted on read — `app.roles.normalise_role` folds
> them onto the current names — so an un-migrated row keeps exactly its previous
> permissions. See [the branding note](#a-note-on-the-name) below.

For a demonstration-grade dataset (several months of payroll history, expenses
across every category, both tenants populated) run the explicit reset command:

```bash
flask --app run:app demo-reset --yes
```

It is never triggered at boot — see [Demo data reset](#demo-data-reset).

### Demo data reset

`flask --app run:app demo-reset` prepares a database for a demonstration. It is
**destructive** and deliberately explicit — nothing about it runs automatically.

Without `--yes` it only reports what it would do:

```
demo-reset would:
  keep and rebuild : MSC Limited, Acme Manufacturing Ltd
  delete entirely  : Test Co 3, Old Client Ltd

Re-run with --yes to apply.
```

With `--yes` it:

1. deletes every client company that is not one of the two professional demo
   tenants, together with all of its employees, payroll runs, items, vouchers,
   remittances, deliveries, expenses, imports, events, notifications and audit
   entries — child rows first, so no foreign key is ever left dangling;
2. removes platform logins outside the professional roster, **re-pointing** the
   history they own (approved runs, recorded expenses, statutory rate versions)
   at the retained platform admin rather than deleting it;
3. empties and rebuilds MSC Limited and Acme Manufacturing Ltd with a roster,
   six months of payroll history, expenses across every category, and delivered
   payslips for the closed runs.

Payroll figures are produced by the real statutory calculator against the active
`StatutoryRate` — demo payslips are arithmetically identical to production ones.
Everything runs in one transaction and the command is idempotent, so running it
twice leaves the same database. It refuses to run against a production
deployment; use `--months N` to change the length of the generated history.

---

## Configuration

All configuration is environment-driven; see [.env.example](.env.example) for the
full, commented list. Key groups:

- **Core:** `SECRET_KEY`, `DATABASE_URL`, `AUTO_INIT_DB`, `PERSISTENCE_REQUIRED`,
  `SESSION_COOKIE_SECURE`, `SEED_DEMO_DATA`.
- **Product identity (branding seam):** `APP_NAME`, `APP_BRAND_NAME`,
  `APP_SHORT_NAME`, `APP_BRAND_MARK`, `APP_TAGLINE`, `COMPANY_NAME`,
  `SERVICE_SLUG`. Every user-facing surface (browser title, sidebar, login,
  emails, payslip PDF) reads these, so a rebrand or white-label is a config
  change, not a template sweep.
- **Distribution channels:** `SMS_BACKEND`, `WHATSAPP_BACKEND`, `EMAIL_BACKEND`
  (each defaults to `console` — logs only, no network) plus their credentials,
  webhook secrets, rate limits, retry policy, and SLA thresholds.
- **File storage:** `STORAGE_BACKEND` (default `local`), `STORAGE_ROOT` (default
  `instance/storage`), `RECEIPT_MAX_BYTES` (default 10 MB). Uploaded files are
  addressed by an opaque storage key rather than a path, so swapping the backend
  needs no data change — see [Receipt storage](#receipt-storage). On a platform
  with an ephemeral filesystem, point `STORAGE_ROOT` at a mounted disk.
- **Statutory / payroll:** `RAW_BANK_WHITELIST`, `CHRISNAT_EMPLOYER_TIN`,
  `CHRISNAT_TAX_OFFICE` (the employer's TIN and tax office printed on GRA
  returns — employer configuration, not product identity).

### Receipt storage

Client expense receipts (PDF/PNG/JPG, up to 10 MB) are the first files the app
stores permanently. Two modules own the whole path:

| Module | Owns |
|---|---|
| `app/storage.py` | Backends — `save` / `open` / `delete` / `exists`, keyed by an opaque **storage key**. Knows nothing about receipts. |
| `app/receipts.py` | The receipt rules — allowed formats, the size ceiling, the key layout, attach/detach. Knows nothing about filesystems. |

A storage key looks like `receipts/<company_id>/<uuid4>.<ext>`. It is not a
filesystem path, so it stays valid when the backend changes: **moving to
Supabase Storage means implementing `StorageBackend` once and setting
`STORAGE_BACKEND=supabase`** — no route, model, template, migration or stored
value changes.

Security properties worth knowing:

- Files live under `instance/` by default — outside the package, never served
  statically, and reachable only through a route that has already resolved the
  tenant.
- Receipts are addressed **through their expense** (`/company/expenses/<id>/receipt`),
  never by receipt id or storage key, so there is no identifier to tamper with.
  Another tenant's expense id is a 404.
- The recorded MIME type comes from sniffing the file's leading bytes, not from
  the client's header. A file whose contents disagree with its extension is
  rejected, and responses carry `X-Content-Type-Options: nosniff`.
- Uploaded filenames are kept only to name the download; the stored key is
  uuid-based, so user text never reaches the filesystem.

---

## Testing

```bash
.venv\Scripts\python.exe -m pytest -q
```

The suite (≈460 tests + subtests) runs against in-memory SQLite and covers the
payroll engines, tenant isolation, permissions, the risk gate, the distribution
subsystem, and the client and operator surfaces. It is the regression gate for
every change — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Deployment

Payrolla ships with Blueprint/config for **Render** (`render.yaml`), **Railway**
(`railway.toml`), and **Docker Compose** (`docker-compose.yml`), plus a
`Procfile` and `runtime.txt`. Every deployed environment **must** use PostgreSQL
(`DATABASE_URL`) and a strong `SECRET_KEY`; the app refuses to boot in production
on SQLite or the insecure dev key. Full instructions, the distribution-worker
options, and the go-live checklist are in [DEPLOYMENT.md](DEPLOYMENT.md).

---

## A note on the name

This repository directory is historically named `pmvp-v1`, and a few identifiers
still carry the founding operator's name (**Chrisnat Limited**): the GRA employer
defaults (`CHRISNAT_EMPLOYER_TIN`, `CHRISNAT_TAX_OFFICE`), the operator-side
export filenames and report letterhead, and the `chrisnat-payroll-mvp` service
name in `render.yaml`. Those are **business entities and live deployment
identifiers** — the bureau that the reports are printed for, and a running
service whose rename would orphan its `DATABASE_URL` binding — so they are
deliberately left alone. The **product** is Payrolla throughout.

The platform *role* strings were in that list until the brand cleanup; they are
now `payrolla_admin` / `payrolla_reviewer`, moved by a reversible data migration
(`a4e7c2b81d95`) with the old spellings still accepted on read. A role names a
capability in this product, not the bureau, so it belonged on the product's name.
See the branding taxonomy in the project's engineering notes for the full
rationale.
