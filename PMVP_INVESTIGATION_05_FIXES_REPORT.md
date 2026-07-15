# PMVP Investigation 05 — Implementation Report

Implementation of the four verified issues from `PMVP_INVESTIGATION_05_POST_SEED_ISSUES.md`, plus the pre-Phase-1 production database cleanup. Raw Engine architecture preserved; standard payroll workflow untouched.

---

## 1. Summary of implemented changes

**Fix 1 — Calculate Pay no longer wipes Raw Engine payrolls.**
The raw branch of `calculate()` used to unconditionally delete every `PayrollItem` and rebuild from `RawPayEntry`. A Raw Engine seed writes items at confirm and never writes `RawPayEntry`, so the rebuild produced nothing and the run was wiped. Now, before the destructive path, we check for `RawPayEntry` rows: if there are none (an Engine-computed run), Calculate Pay is a **safe no-op** that flashes an explanatory message and returns — it never deletes items. The legacy hours-first path (runs that *do* have `RawPayEntry`) is unchanged. The detail-page banner ("gross pay has not been calculated yet — an operator will process the hours") now only shows for raw runs with **zero items**, so a computed Engine run no longer shows the misleading legacy message.

**Fix 2 — Delete Payroll no longer 500s.**
`hard_delete_payroll_run` cleared `PayslipDelivery`, `RawPayEntry`, `ImportBatch` and cascaded `PayrollItem`, but never touched `raw_upload_archives`, which holds a NOT-NULL FK to the run with no cascade. Every Raw Engine run has one, so deletion raised `IntegrityError`. Added `RawUploadArchive.query.filter_by(payroll_run_id=...).delete()` (and the import + docstring). Deletion now works for both raw and standard runs.

**Fix 3 — Seeded employee master data is now correct.**
The master fields (SSNIT, account, bank, branch, department, job title, Ghana card, tax relief) were read from hardcoded columns calibrated to the DZ specimen; Book1 shifts them two columns left, so the seed wrote a bank name into SSNIT and a branch into the account number. Replaced with `resolve_master_columns()` in `mapping.py`, which locates each field by **matching its header label** in the element/NAMES band (merge-resolved), exactly the resilience approach the hours parser uses. Payment-/statutory-critical fields (SSNIT, account, bank) are **required** — a workbook whose headers can't be located is refused with a `HeaderError` instead of silently seeding wrong data. `seed.py` now reads master fields via the resolved columns.

**Fix 4 — Monthly template prefill.**
`generate_monthly_template` now appends read-only reference columns — **Bank, A/C No., SSNIT No., Department, Ghana Card, MoMo No.** — prefilled from each seeded `Employee`, greyed to signal reference-only. They are ignored by `parse_thin_workbook` on re-upload (unknown headers are skipped), so they never affect computation. ICU membership was already prefilled.

---

## 2. Files modified

| File | Fix | Change |
|------|-----|--------|
| `app/payroll.py` | 1, 2 | `calculate()` raw no-op guard; `hard_delete_payroll_run` deletes `RawUploadArchive` + import + docstring |
| `app/templates/payroll_detail.html` | 1 | Banner shows only for raw runs with 0 items |
| `app/raw_engine/mapping.py` | 3 | `_compact_header`, `_MASTER_HEADER_MATCHERS`, `_MASTER_REQUIRED`, `resolve_master_columns()` |
| `app/raw_engine/seed.py` | 3 | Import `resolve_master_columns`; read master fields via resolved columns |
| `app/raw_engine/template.py` | 4 | `REFERENCE_COLUMNS`; prefilled reference block + greyed header fill |
| `render.yaml` | (infra) | `--timeout 300 --threads 4` (from the earlier health-check fix — still uncommitted) |

No model/schema changes were made (no migration required). **Position/job title is parsed but has no `Employee` column** — see Known Issues.

---

## 3. Database changes (production `chrisnat-db`)

Transactional data cleared inside one transaction, FK-safe order (`payslip_delivery` → `payroll_item` → `payment_voucher`/`remittance`/`expense`/`import_batch`/`raw_pay_entries`/`raw_upload_archives` → `payroll_run`).

| Table | Before | After |
|-------|-------:|------:|
| payroll_run | 7 | **0** |
| payroll_item | 315 | **0** |
| import_batch | 23 | **0** |
| raw_upload_archives | 3 | **0** |
| expense | 1 | **0** |
| payslip_delivery / payment_voucher / remittance / raw_pay_entries | 0 | 0 |
| **employee (preserved)** | 397 | **397** |
| **wage_rate_profiles (preserved)** | 3186 | **3186** |
| **client_company (preserved)** | 7 | **7** |
| **user (preserved)** | 5 | **5** |
| **audit_trail (preserved)** | 201 | **201** |

Post-cleanup consistency: **0 orphan employees, 0 orphan wage profiles.** `audit_trail` was preserved (it's a log, not in the remove list; it has no FK to `payroll_run`).

---

## 4. Validation performed

- **Code inspection:** every changed path read and reviewed against the investigation findings.
- **Syntax:** `python -m py_compile` clean on `payroll.py`, `seed.py`, `mapping.py`, `template.py`.
- **Fix 3 functional test (real files):** ran the resolver against Book1 and the DZ fixture.
  - Book1 → `ssnit=BO, account=BP, bank=BQ, branch=BR, ghana=BN` (matches Book1's real headers).
  - DZ → `ssnit=BQ, account=BR, bank=BS, branch=BT, ghana=BP` (matches DZ — legacy behaviour preserved).
  - `GEORGE AKOTO`, previously `ssnit='ADB'` (a bank) / `account='SPINTEX'` (a branch), now reads `ssnit='C017204300395'`, `bank='ADB'`, `branch='SPINTEX'`, `account='1182…201'` — **correct**. This also disproves the earlier "salaried block has a different layout" hypothesis: columns are uniform; the garbage was purely the shifted hardcodes.
  - A headerless sheet is **refused** (`missing ssnit,account_no,bank`) — fail-loud confirmed.
- **Fix 2 validation:** the DB cleanup deleted `raw_upload_archives` before `payroll_run` with no FK error — the exact dependency the fix handles in-app.
- **DB cleanup:** before/after counts + orphan checks (above).

**Not run here (environment limits):** the full `pytest` suite could not run in this sandbox (Python 3.10 vs the pinned pandas 3.x, plus a flaky file mount). Run locally before pushing:
```
pytest tests/test_raw_engine_seed.py tests/test_raw_engine_thin.py tests/test_raw_engine_web.py \
       tests/test_raw_engine_template.py tests/test_shape_guard.py \
       tests/test_raw_engine_header_geometry.py tests/test_raw_engine_cleanup.py -q
```
The seed/compute/template/web tests run against the DZ fixture, whose headers resolve to the same columns as before, so they should stay green.

---

## 5. Remaining known issues

1. **Preserved employees still hold pre-fix (wrong) master data.** The cleanup kept the 397 employees (per your preserve list), but their bank/account/SSNIT were written by the buggy seed. Fix 3 corrects *future* seeds; to correct the existing rows, **re-seed each client's rich RAW DATA workbook** after deploy — the seed upserts by `(client, staff_id)`, so it overwrites the master fields in place. A plain thin monthly upload will NOT fix them (thin reads master data from stored context, not the file).
2. **"Position" isn't persisted.** `Employee` has `department` but no `job_title`/`position` column, so Position can't be seeded or prefilled today. Parsed into `SeedEmployee.job_title` but dropped at persist. Needs a small migration (add `Employee.job_title`) — deferred to keep this pass migration-free.
3. **RLS disabled on all tables (security).** Unchanged from the investigation — payroll PII is exposed via the Supabase auto-REST API. Separate high-priority track; do not blanket-enable without policies.
4. **`render.yaml` and the two PMVP-05 `.md` files are uncommitted** and should go in the branch.

---

## 6. Suggested follow-up for Phase 2

- Add `Employee.job_title` (migration) and wire Position into seed + template prefill.
- Header-anchor the **lump-adjustment** columns (loan/welfare/bonus/etc.) too — they're the same fixed-position fragility, currently correct only because Book1 and DZ happen to align there.
- Decide the canonical raw model and **retire the legacy `RawPayEntry` path** entirely (or make the Engine emit `RawPayEntry`) so `calculate()` has one coherent behaviour; today it's a no-op for Engine runs, which is safe but a code smell.
- Add an `ON DELETE CASCADE` (or a SQLAlchemy relationship cascade) for `raw_upload_archives` so the app-level delete list can't drift out of sync with the schema again.
- Move workbook preservation off Render's ephemeral disk into a Supabase storage bucket (the `_preserve_workbook` TODO).
- Address RLS with scoped policies (service role bypasses RLS, so the app keeps working).

---

## 7. Branch + grouped commits (run on your machine)

I can't create the branch/commit from this environment (no git credentials + a flaky mount that could stage corrupted content). Your files on disk are correct. Run:

```bash
git checkout -b pmvp-investigation-05-fixes

# Fix 3 — seed master mapping (header-anchored)
git add app/raw_engine/mapping.py app/raw_engine/seed.py
git commit -m "PMVP-05 Fix 3: header-anchor seed master columns; fail loud on unlocatable bank/SSNIT/account"

# Fix 4 — monthly template prefill
git add app/raw_engine/template.py
git commit -m "PMVP-05 Fix 4: prefill monthly template with read-only employee reference columns"

# Fix 1 + Fix 2 both live in payroll.py — stage by hunk into two commits:
git add -p app/payroll.py app/templates/payroll_detail.html   # stage the calculate() guard + banner
git commit -m "PMVP-05 Fix 1: Calculate Pay is a no-op for Engine runs (never wipe seeded items)"
git add app/payroll.py                                         # stage the remaining delete/import/docstring hunks
git commit -m "PMVP-05 Fix 2: delete RawUploadArchive in hard_delete_payroll_run (fixes delete 500)"

# Infra + docs
git add render.yaml
git commit -m "Ops: gunicorn --threads 4 --timeout 300 (health-check + long-confirm fix)"
git add PMVP_INVESTIGATION_05_POST_SEED_ISSUES.md PMVP_INVESTIGATION_05_FIXES_REPORT.md
git commit -m "Docs: PMVP-05 investigation + implementation report"
```
(If `git add -p` is fiddly, just `git add app/payroll.py app/templates/payroll_detail.html` and commit Fixes 1+2 together — they're both the raw-run behaviour.)

Then merge to `main` so Render deploys, and **re-seed the client workbooks** to correct the preserved employee master data.
