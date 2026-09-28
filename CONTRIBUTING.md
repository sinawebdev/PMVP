# Contributing to Payrolla

Welcome. This guide gets a new engineer productive and describes the conventions
that keep the codebase stable. Read [ARCHITECTURE.md](ARCHITECTURE.md) first for
the lay of the land.

## Local setup

```bash
python -m venv .venv
.venv\Scripts\activate            # source .venv/bin/activate on POSIX
pip install -r requirements.txt
copy .env.example .env            # cp on POSIX
python run.py                     # http://127.0.0.1:5000
```

Python **3.11+** (see `.python-version`). First run auto-creates a local SQLite
database and seeds demo users/tenants when `SEED_DEMO_DATA=true`. Demo logins are
in the [README](README.md#default-seed-logins).

For a Postgres-backed local stack, use Docker Compose (`make up`) — see
[DEPLOYMENT.md](DEPLOYMENT.md).

## Running the tests

The test suite is the regression gate for every change:

```bash
.venv\Scripts\python.exe -m pytest -q            # full suite (~460 tests)
.venv\Scripts\python.exe -m pytest tests/test_tenancy.py -q   # one module
```

Tests run against in-memory SQLite (`DATABASE_URL=sqlite:///:memory:`, set in the
test modules) with CSRF disabled. The full suite is thorough and can take several
minutes; run the relevant modules while iterating and the full suite before you
consider a change done. **No behavioural regression is acceptable.**

`pytest` is pinned but **not** in `requirements.txt` — that file is the exact set
Render installs into production, and a test framework has no business on a
payroll dyno. Install it explicitly, the same version CI pins
(`.github/workflows/ci.yml`):

```bash
.venv\Scripts\python.exe -m pip install pytest==9.1.1
```

## Screenshot tooling

`scripts/capture_ui.py` boots the app on an ephemeral port against a throwaway
seeded database, signs in as the demo tenant, and captures named screenshots —
including interactive states, which it reaches by **clicking** the control
rather than by injecting the class the control would have set. A broken toggle
therefore fails the capture instead of producing a screenshot that looks right.

```bash
.venv\Scripts\python.exe -m pip install playwright==1.56.0
.venv\Scripts\python.exe -m playwright install chromium   # Chromium ONLY
.venv\Scripts\python.exe scripts/capture_ui.py --list
.venv\Scripts\python.exe scripts/capture_ui.py
```

Same rule as pytest: **Playwright stays out of `requirements.txt`**, so it never
reaches the Render build (`render.yaml`) or the deploy image (`Dockerfile`).

Install **only Chromium**. `playwright install` with no argument also pulls
Firefox and WebKit, roughly half a gigabyte of browser binaries, and nothing
here ever renders in them.

Output lands in `.screenshots/`, which is gitignored. Add a capture by appending
a `Capture` to `CAPTURES` in that file; the runner is deliberately free of
per-capture branching.

## Conventions

- **Tenancy is never optional.** Any tenant-scoped read/write goes through
  `tenant_query` / `tenant_get_or_404` (`app/tenancy.py`) — never bare
  `Model.query` on a tenant-facing route. A cross-tenant id must return 404.
- **Permissions live in `app/permissions.py`.** Gate nav and actions with the
  capability predicates, not inline role-string lists.
- **The payroll engines are frozen.** Changes under `app/raw_engine/`,
  `app/payroll_calculations/`, `app/money.py`, and `StatutoryRate.compute_*` must
  keep `tests/test_engine_parity.py` green — identical inputs, identical figures.
- **Product identity is config, not literals.** User-facing product names read
  from `APP_NAME`, `APP_BRAND_NAME`, etc. (the branding seam). Do not hardcode
  "Payrolla" into a template; add or read a config key.
- **Schema changes are additive Alembic migrations** (`migrations/versions/`).
  Never edit a shipped migration; add a new one.
- **Validate input at system boundaries** (uploads, webhooks, form posts).
- **Keep modules cohesive and under ~500 lines** where practical. The known large
  modules (`payroll.py`, `excel_utils.py`, `models.py`) are tracked as deferred
  debt — don't grow them further without reason.
- **Don't commit secrets or real client workbooks.** `.env`, `instance/`,
  `uploads/`, `exports/`, and `*.xlsx` specimens are git-ignored for a reason.

## Making a change

1. Branch from the current working branch.
2. Make the smallest change that solves the problem; prefer refactoring over
   rewriting.
3. Add or update tests alongside the change.
4. Run the relevant test modules, then the full suite.
5. Keep the commit message factual. Do **not** add a `Co-Authored-By` trailer
   unless the project's `.claude/settings.json` enables commit attribution.

## Where things live

| You want to… | Look in |
|---|---|
| Change tenant scoping | `app/tenancy.py` |
| Change who can do what | `app/permissions.py`, `app/roles.py` |
| Touch the client (tenant) UI | `app/client/`, `app/templates/client/` |
| Touch the operator lifecycle | `app/payroll.py`, `app/payroll_status.py` |
| Change statutory maths | `app/payroll_calculations/`, `app/raw_engine/calc/` |
| Change payslip delivery | `app/distribution/` |
| Change an Excel/PDF export | `app/excel_utils.py`, `app/pdf_service.py` |
| Add a model / column | `app/models.py` + a new migration |
