# Raw Hours Payroll Engine (`app/raw_engine/`)

A parallel payroll path for **hourly-rate** clients (e.g. DZVANS), running
alongside the untouched Standard Payroll Engine. The engine is chosen **per
upload** — there is no `payroll_mode` column. A company is *seeded* once it has
`WageRateProfile` rows.

## Build status

| Phase | Scope | State |
|-------|-------|-------|
| **1. Seed** | rich RAW-DATA workbook → persist context (employees, rates, ICU membership, basic wage) behind preview→confirm | **built** |
| **2. Compute** | hours + context → full costed payroll (PAYE, OT/bonus tax, SSNIT, ICU, net), cent-accurate to the DZ workbook | **built** |
| **3. Thin monthly** | thin file joined to context → Phase 2; blank = 0; unknown ID blocked; raises only via rich re-upload | **built** |
| **4. Validation** | hours reconciliation (hard block), ICU tie-out (hard block), unknown-ID block, non-member-ICU flag, recompute drift | **built** |
| **5. Exports & bank grouping** | reuse wage sheet/GRA/payslip writers; config bank whitelist routes every worker to exactly one of {bank, PV}; AKOTO grouping; PV; ICU distribution | **built** |
| **6. Template + hardening** | monthly thin-template generation (roster + seeded elements + zero adjustments + scoped ICU column), round-trips through Phase 3, audit trail on seed/confirm/compute, end-to-end DZ cycle | **built** |

**All six gates green.** A complete DZ monthly cycle — seed → template → thin upload → compute → validate → export — runs end to end with cent-accurate reconciliation to the source workbook.

## Modules (Phase 1)

- `detection.py` — is this a rich RAW-DATA workbook? is the company seeded?
- `cleaning.py` — `normalise_emp_id` (reused) + `normalise_element` (case-insensitive).
- `mapping.py` — the DZ RAW DATA positional column map + pay-element registry,
  with fail-loud layout validation (`HeaderError`, never guess a column).
- `seed.py` — `parse_rich_workbook(path, client_company_id)` → `SeedContext`
  (read/preview only, no DB writes).
- `store.py` — `persist_seed(context, source_path)` — one transaction, idempotent
  upsert, workbook preservation.

## Seed model

Read from the `RAW DATA` sheet (input/context columns only — never the computed
`GROSS`/`PAYE`/`NET` columns):

- **Employee master** → `Employee` (staff key, name, Ghana card, SSNIT, bank /
  branch / account, department, monthly tax relief).
- **Basic wage** (col W) → `Employee.basic_salary`.
- **ICU membership** → `Employee.icu_member`, inferred from ICU dues (col BC) > 0.
- **Per-employee hourly rates** (cols N–V) → one `WageRateProfile` row per
  non-zero rate element, tagged `basic` / `overtime` / `allowance`.
- Salaried admin rows (no per-employee rate columns) seed a flat basic wage and
  no rate rows.

Rates are stored **per employee** (`WageRateProfile.employee_id` set) rather than
resolved to a shared rate class: the DZ workbook carries 22 distinct daily rates,
so the per-employee rate table is the faithful source of truth.

## Verified against `DZ-PAYROLL JAN 2026.xlsx`

181 employees · 137 ICU members · George Akoto = non-member, basic 1800 ·
Richard Woode (DCL9) rate table matches the existing hourly calculator fixture.

The specimen is a real client workbook containing PII; it is **gitignored**
(`tests/fixtures/*.xlsx`) and decrypted locally, never committed.

## Migration dry-run on Postgres (head `d2a4f6108e35`)

The test suite runs on SQLite, but `RawUploadArchive.content` is **BYTEA** on
Postgres (SQLAlchemy `LargeBinary`), so the raw-engine migration
`d2a4f6108e35_add_pay_type_and_raw_upload_archive` is dry-run against a real
Postgres before merge. Run against a **throwaway** instance — a local container
or a scratch cluster — **never production**.

**Bootstrap note.** The baseline migration `d450b1a3317e` is FK-only (it adds
foreign keys, it does not create the core tables). The app creates its schema
with `db.create_all()` on boot and treats migrations as incremental deltas, so
the production bootstrap is *create_all + `flask db stamp head`*, not a
from-empty `flask db upgrade`. The dry-run mirrors that, then exercises the
raw-engine migration's Postgres DDL both ways. Run the `flask db` commands with
`AUTO_INIT_DB=false` so `create_all` can't silently re-create a table the
downgrade just dropped (`create_all` adds missing *tables* but never ALTERs an
existing one to re-add a dropped *column*).

```bash
# 1. throwaway Postgres (either works)
docker run -d --name pg_dryrun -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=chrisnat_dryrun -p 5433:5432 postgres:16
#   ...or a scratch cluster: initdb -D <dir> --auth-local=trust && pg_ctl -D <dir> -o "-p 5433" start && createdb chrisnat_dryrun

export FLASK_APP=run.py DATABASE_URL="postgresql://postgres:pw@localhost:5433/chrisnat_dryrun"

# 2. build the schema the production way, then mark migrations applied
AUTO_INIT_DB=true python -c "from app import create_app; create_app()"   # create_all + seed
AUTO_INIT_DB=false python -m flask db stamp head
AUTO_INIT_DB=false python -m flask db heads      # -> d2a4f6108e35 (head)   [single head]

# 3. exercise the raw-engine migration down then back up on Postgres
AUTO_INIT_DB=false python -m flask db downgrade c9e5b2d478a1   # drops raw_upload_archives (BYTEA) + employee.pay_type
AUTO_INIT_DB=false python -m flask db upgrade head             # re-creates them
```

**Result (Postgres 18.4, 2026-07-11):** clean. Single head `d2a4f6108e35`
throughout. Before downgrade `raw_upload_archives.content` is `bytea`; after
`downgrade c9e5b2d478a1` both `raw_upload_archives` and `employee.pay_type` are
gone; after `upgrade head` `content` is `bytea` again, `employee.pay_type` is
back, and index `ix_raw_upload_archives_payroll_run_id` is restored. `upgrade`
and `downgrade` both apply cleanly; the migration is idempotent (inspects before
acting) and dialect-agnostic. Production database untouched.
