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
