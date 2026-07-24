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

Seeded only when `SEED_DEMO_DATA=true` (never in a real deployment). All use
password `password123`:

| Plane | Role | Email |
|---|---|---|
| Platform | Admin | `admin@chrisnat.local` |
| Platform | MD | `md@chrisnat.local` |
| Platform | Payroll Officer | `payroll@chrisnat.local` |
| Platform | Accounts Officer | `accounts@chrisnat.local` |
| Tenant | Client user | `msc.client@chrisnat.local` |

> The `@chrisnat.local` demo domain and the `chrisnat_admin` platform role are
> retained from the founding operator (Chrisnat Limited) and are treated as
> stable identifiers, not product branding. See
> [the branding note](#a-note-on-the-name) below.

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
- **Statutory / payroll:** `RAW_BANK_WHITELIST`, `CHRISNAT_EMPLOYER_TIN`,
  `CHRISNAT_TAX_OFFICE` (the employer's TIN and tax office printed on GRA
  returns — employer configuration, not product identity).

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

This repository directory is historically named `pmvp-v1`, and a few internal
identifiers retain the founding operator's name (**Chrisnat Limited**): the
`chrisnat_admin` / `chrisnat_reviewer` role strings, the `@chrisnat.local` demo
login domain, the GRA employer defaults, and the operator-side export filenames.
These are **business entities and persisted identifiers**, deliberately left
unchanged; renaming them would be a data/behaviour migration, not a rebrand. The
**product** is Payrolla throughout. See the branding taxonomy in the project's
engineering notes for the full rationale.
