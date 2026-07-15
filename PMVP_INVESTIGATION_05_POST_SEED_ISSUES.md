# PMVP Investigation 05 — Post-Seed Issues (Calculate Pay, Delete, Seeded Data, Template Prefill)

**Mode:** Investigation only. No code changed. Evidence gathered from the codebase, the live production database (`chrisnat-db`, Supabase project `lmzbzhklvntrwgnzqony`), and the actual `Book1.xlsx` seed workbook.

**Legend:** ✅ VERIFIED (proven by code + DB/file) · 🟡 STRONG HYPOTHESIS (well-supported, one step short of proof) · ❔ UNKNOWN (needs a specific check).

---

## 1. Executive Summary

Three real bugs and one feasible feature, all rooted in the same underlying fact: **there are two different "raw" payroll pipelines in the codebase, and the new Raw Engine (items-first) is colliding with leftovers of the old raw-hours path (hours-first).**

| # | Issue | Root cause (one line) | Confidence |
|---|-------|------------------------|------------|
| 1 | Calculate Pay clears the run | It **deletes all PayrollItems** then rebuilds from `RawPayEntry`, which the Raw Engine seed never writes (0 rows) → wipes to nothing | ✅ |
| 2 | Delete Payroll → 500 | `hard_delete_payroll_run` doesn't delete `raw_upload_archives`, which holds a NOT-NULL FK to the run → `IntegrityError` | ✅ |
| 3 | Seeded fields wrong (bank, account, SSNIT…) | Master fields read from **hardcoded column positions** that don't match `Book1.xlsx`'s layout; these columns are never validated | ✅ |
| 4 | Template prefill (feature) | Feasible and small — the generator already loops the seeded roster — but **must wait on Issue 3** (would prefill garbage today) | ✅ (feasibility) |

Additionally, a **critical security finding** surfaced during DB inspection: Row-Level Security is disabled on all 19 tables (payroll PII fully exposed to the Supabase anon role). See §6.

**Recommended order:** Issue 3 → Issue 2 → Issue 1 → Feature 4, with the RLS security issue on a parallel track. Rationale in §7.

---

## 2. Issue 1 — "Calculate Pay" clears the uploaded/seeded data

**Symptoms.** After seeding a raw run (which already shows 181 workers with computed pay), clicking **Calculate Pay** empties the run: 0 payroll items, all totals `GH₵ 0.00`, and the "operator will process the hours" banner. The screenshot (run #28) is a run in exactly this post-wipe state.

**Root cause (✅ VERIFIED).** Two incompatible raw pipelines share `upload_type = "raw"`:

- **New Raw Engine (items-first):** the seed/thin confirm computes payslips and writes them straight to `payroll_item` via `write_payroll_items` (`store.py`). It **never writes `raw_pay_entries`.**
- **Legacy raw-hours (hours-first):** `calculate()` for `upload_type == "raw"` **deletes every PayrollItem for the run** and rebuilds them from `RawPayEntry` hours × `WageRateProfile` rates.

Because the seed writes 0 `RawPayEntry` rows, `HourlyShiftCalculator.calculate_run()` iterates an empty set → returns `{}` → the delete runs but nothing is recreated → the run is wiped.

**Evidence.**
- `app/payroll.py:1142` — `PayrollItem.query.filter_by(payroll_run_id=payroll_run.id).delete()` executes *before* rebuild, unconditionally, in the raw branch.
- `app/payroll_calculations/hourly.py:111` — `entries = RawPayEntry.query.filter_by(payroll_run_id=self.run.id).all()` — the calculator's only input is `RawPayEntry`.
- `app/raw_engine/store.py:179–245` — `write_payroll_items` inserts `PayrollItem`; there is **no** `RawPayEntry` write anywhere in the raw_engine.
- **Live DB:** `raw_pay_entries` = **0 rows total**; `payroll_item` = 315 rows. Run #29 (DZVANS/Grimaldi, `Book1.xlsx`, raw, Draft) = **181 items, 0 raw_entries, 1 archive** — a healthy seeded run that Calculate Pay would zero out. Runs #27 and #28 (DZVANS, raw) already sit at **0 items** (consistent with having been wiped, or a thin upload whose staff IDs didn't match the roster — see note).

**Files:** `app/payroll.py` (calculate), `app/payroll_calculations/hourly.py`, `app/raw_engine/store.py`, `app/raw_engine/web.py` (confirm sets `upload_type="raw"`), `app/templates/payroll_detail.html:73–75` (the misleading banner).
**Functions:** `calculate()`, `HourlyShiftCalculator.calculate_run()`, `write_payroll_items()`, `compute_seed_month()`.
**Tables:** `payroll_item`, `raw_pay_entries`.

**Note (🟡):** I did not click Calculate Pay on run #29 myself — the wipe is inferred from the code + the empty `raw_pay_entries`. The mechanism is proven; the live reproduction on #29 is one click from confirmed.

**Secondary observation.** The detail banner ("raw hours upload … gross pay has **not** been calculated yet — an operator will process the hours") is hardcoded to the legacy model. For a Raw Engine run, pay **is** already computed at confirm, so the banner is misleading even when the run isn't empty.

---

## 3. Issue 2 — Deleting a payroll run returns 500

**Symptoms.** Deleting a raw run returns Internal Server Error. Standard (ACS) runs delete fine.

**Root cause (✅ VERIFIED).** `hard_delete_payroll_run` clears `PayslipDelivery`, `RawPayEntry`, `ImportBatch`, and cascades `PayrollItem` — but **does not touch `raw_upload_archives`.** That table has a **NOT-NULL foreign key to `payroll_run` with no `ON DELETE` cascade**, and every raw-seeded run has exactly one archive row. So `db.session.delete(payroll_run)` violates `raw_upload_archives_payroll_run_id_fkey` → the DB raises, the exception is uncaught in `delete_run`, and Flask returns 500.

**Evidence.**
- **Schema (live):** FK `raw_upload_archives_payroll_run_id_fkey`: `raw_upload_archives.payroll_run_id → payroll_run.id`; column `payroll_run_id` is **not** nullable and the FK has no cascade rule.
- `app/payroll.py:1537–1540` — deletes `PayslipDelivery`, `RawPayEntry`, `ImportBatch`, then `db.session.delete(payroll_run)`. The docstring enumerates the tables it handles; `RawUploadArchive` is absent.
- `app/payroll.py:1478–1506` — `payroll_run_delete_blockers` also does **not** check archives (it checks voucher/remittances/sent-payslips/expenses).
- `app/raw_engine/store.py:96–114` — `archive_upload` writes one `RawUploadArchive` per run inside the seed transaction; **live DB:** runs #27, #28, #29 each have `archives = 1`.

**Files:** `app/payroll.py` (`hard_delete_payroll_run`, `payroll_run_delete_blockers`, `delete_run`), `app/models.py` (`RawUploadArchive` FK definition), `app/raw_engine/store.py` (`archive_upload`).
**Functions:** `hard_delete_payroll_run`, `payroll_run_delete_blockers`, `delete_run`, `archive_upload`.
**Tables:** `raw_upload_archives`, `payroll_run`.

**Exact exception (🟡 STRONG HYPOTHESIS):** `sqlalchemy.exc.IntegrityError` wrapping `psycopg2.errors.ForeignKeyViolation` on `raw_upload_archives_payroll_run_id_fkey` ("update or delete on table payroll_run violates foreign key constraint"). This is what the schema guarantees; to convert to ✅, reproduce a delete and read the Postgres/app log, or check `get_logs(postgres)` around a delete attempt.

---

## 4. Issue 3 — Seeded employee fields are wrong (bank, account no., SSNIT, …)

**Symptoms.** After seeding, employee master fields hold obviously wrong values — a bank name sitting in the SSNIT field, a branch name in the account-number field, etc.

**Root cause (✅ VERIFIED).** The Raw Engine reads master fields from **fixed, hardcoded column positions** (`mapping.py`), calibrated against the original `DZ-PAYROLL JAN 2026.xlsx`. `Book1.xlsx`'s master columns are **shifted by ~2 columns**, and — unlike the hours/rate block, which we made merge-aware and NAMES-anchored — **these columns are never validated by `validate_layout`.** So a shifted workbook seeds "successfully" and writes each field from the wrong column.

**Evidence — the mismatch is exact:**

| Field | Seed reads (hardcoded) | `Book1.xlsx` actual header | Result |
|-------|------------------------|----------------------------|--------|
| SSNIT | `BQ` (col 69) | `BQ` = **BANK** | SSNIT ← a bank name |
| Account No. | `BR` (col 70) | `BR` = **BRANCH** | Account ← a branch name |
| Bank | `BS` (col 71) | `BS` = **BRANCH CODE** | Bank ← a branch code |
| Branch | `BT` (col 72) | `BT` = **X** (junk) | Branch ← junk |
| — | (correct SSNIT) | `BO` (67) = SOCIAL SECURITY NUMBER | not read |
| — | (correct Account) | `BP` (68) = A/C NO. | not read |

- Book1 header row 11 (verified from the file): `BO='SOCIAL SECURITY NUMBER'`, `BP='A/C NO.'`, `BQ='BANK'`, `BR='BRANCH'`, `BS='BRANCH CODE'`, `BT='X'`.
- Book1 data, RICHARD WOODE (row 15): `BO='C036902210032'` (SSNIT), `BP='0033010035714'` (account), `BQ='FBN'` (bank), `BR='TEMA COM.1'` (branch). The seed instead stores SSNIT=`FBN`, account=`TEMA COM.1`, bank=`None`.
- **Live DB confirms the garbage:** `GEORGE AKOTO` → `ssnit_number='ADB'` (a bank), `bank_account_number='SPINTEX'` (a branch), `bank_name=NULL`. `FELIX AGBO` → `ssnit_number='SG-GH'` (a bank), `bank_account_number='T.F.H'` (a branch), `bank_name='090110'` (a branch code).

**Files:** `app/raw_engine/mapping.py` (`COL_SSNIT_NO=69`, `COL_ACCOUNT_NO=70`, `COL_BANK=71`, `COL_BRANCH=72`, `COL_DEPARTMENT=63`, `COL_JOB_TITLE=61`, `COL_GHANA_CARD=66`), `app/raw_engine/seed.py:171–176` (the master-field reads), `app/raw_engine/store.py:134–141` (upsert).
**Functions:** `parse_rich_workbook` (master reads), `_upsert_employee`.
**Tables:** `employee` (`bank_name`, `bank_account_number`, `ssnit_number`, `bank_branch`, `department`, `ghana_card_number`).

**Secondary contributor (✅).** `store.py:134–141` uses `record.X = emp.X or record.X` — on re-seed, a falsy new value keeps the old one. Combined with wrong reads, this can preserve/entrench garbage across re-seeds.

**Nuance (🟡).** Some rows look mis-shifted differently — e.g., `GEORGE AKOTO` (staff_id "1", a salaried admin near the top of RAW DATA) has an account-number-looking string in `ghana_card_number`, even though `COL_GHANA_CARD=66/BN` matches Book1's `GHANA CARD` header. This suggests the **salaried-admin block and the hourly block may not share the same column layout**, so the offset isn't uniform. The fix (anchor these fields by header rather than fixed position) resolves both cases; the per-section offset detail is worth confirming during implementation.

---

## 5. Feature Feasibility — Monthly Template Prefill

**What exists today (✅).** `generate_monthly_template` (`template.py`) already:
- queries the seeded roster (`Employee.query.filter_by(client_company_id=…)`),
- prefills **Staff ID + Name + ICU-member flag** per row,
- zero-fills hours and adjustment columns for the operator to fill,
- derives element columns from the client's actual seeded `WageRateProfile` codes.

It does **not** include bank/account/SSNIT/department/position columns — by design, because the thin compute path (`join_and_compute` → `write_payroll_items`) reads those from stored `Employee` context, **not** from the uploaded file. So in the monthly flow the client already does **not** re-enter bank/account.

**Feasibility of adding prefilled reference columns (Bank, A/C No., SSNIT, Dept, Position):** ✅ **HIGH, small change.** The generator already has the `Employee` objects in the loop (`for emp in employees`), so appending `emp.bank_name`, `emp.bank_account_number`, `emp.ssnit_number`, `emp.department`, etc. is a few lines. No new queries or schema needed. It fits the Raw Engine design naturally.

**Dependencies & edge cases:**
- **Blocked by Issue 3 (hard dependency).** Prefilling today would surface the *wrong* bank/account/SSNIT values into the client's template. Fix Issue 3 first, then re-seed, then prefill.
- **Read-only vs editable:** if the prefilled columns are editable, decide whether template edits should update `Employee` (currently the thin flow ignores master fields from the file). Recommend read-only reference columns unless a "correct my details" workflow is wanted.
- **Unseeded client:** no employees → empty template (already handled).
- **Staleness:** the template is a snapshot; if `Employee` is edited after download, the template lags (acceptable).
- **New hires:** only seeded workers appear; new hires require a rich re-seed (unchanged, by design).

**Available data:** everything requested (bank, account, SSNIT, dept, position, ICU, basic wage, rate profiles) is already persisted on `Employee` / `WageRateProfile`, keyed by `(client_company_id, staff_id)`.

---

## 6. Overall Architectural Observations

1. **Two competing "raw" pipelines.** The legacy *hours-first* path (`RawPayEntry` → Calculate Pay → `HourlyShiftCalculator` → items) and the new *items-first* Raw Engine (compute at confirm → `write_payroll_items`) both use `upload_type="raw"` and the same detail UI, but are mutually incompatible. Symptoms: Calculate Pay is destructive for Raw Engine runs (Issue 1); the "not calculated yet / operator will process" banner is wrong for them. **A decision is needed on the intended model** (keep items-first and neutralize the legacy path, or have the seed also emit `RawPayEntry` so Calculate Pay is meaningful).

2. **Master-field mapping is fixed-position and unvalidated.** Same brittleness class we already fixed for the hours header, but never applied to bank/account/SSNIT/dept. This is Issue 3 and it silently corrupts PII.

3. **Delete doesn't track all run-dependent tables.** `raw_upload_archives` was added after `hard_delete_payroll_run` was written and never wired in (Issue 2). Worth auditing every FK into `payroll_run` against the delete routine (verified FKs: `payslip_delivery`, `raw_pay_entries`, `import_batch`, `expense`, `remittance`, `payment_voucher`, `payroll_item`, `raw_upload_archives`).

4. **🔒 SECURITY — Row-Level Security is disabled on all 19 tables.** Supabase's advisory flags this as *critical*: the anon/authenticated roles used by Supabase client libraries can read or modify every row via the project's auto-generated REST API. For a payroll app that's bank account numbers, SSNIT numbers, salaries, and Ghana Card IDs fully exposed if the anon key is public and PostgREST is reachable. The Flask app connects via **direct Postgres** (not the anon key), so this doesn't affect app behavior — but the exposure is real and independent of the app. **Do not blanket-enable RLS without policies** (it will block the direct-Postgres app too if not scoped correctly — the service role bypasses RLS, so the app is fine, but verify before running). Treat as a separate high-priority security task; decide policies deliberately.

---

## 7. Recommended Order of Fixes

1. **Issue 3 — wrong seeded master data (FIRST).** It's silently writing incorrect bank accounts and SSNIT numbers — the highest-harm bug (wrong bank account = wrong payment), and it blocks Feature 4. Direction: anchor the master fields by header (reuse the NAMES-anchored/merge-aware approach we built for the hours block) and/or validate them in `validate_layout` so a shifted workbook fails loud instead of seeding garbage. Re-seed after fixing to correct the existing rows.
2. **Issue 2 — delete 500 (quick, low-risk).** Add `RawUploadArchive` deletion to `hard_delete_payroll_run` (or an `ON DELETE CASCADE` on the FK), and optionally list archives in the blockers. Small, isolated, and it unblocks cleaning up the garbage-seeded test runs (#27–#29).
3. **Issue 1 — Calculate Pay wipes.** Requires the architectural decision in §6.1. Cheapest safe option: make Calculate Pay a no-op/redirect (or a pure statutory recompute over existing items) for Raw Engine runs instead of the delete-then-rebuild-from-`RawPayEntry`, and fix the banner. Do this *after* the model decision.
4. **Feature 4 — template prefill.** Additive and low-risk once Issue 3 is fixed.
5. **🔒 RLS / security.** Parallel track, high priority given the PII. Design policies (or confirm the anon key is not exposed and lock down the API) before enabling.

**Still-open checks (❔ / 🟡 to promote to ✅ during implementation):**
- Reproduce the delete on a raw run and capture the exact `IntegrityError` (or `get_logs(postgres)`).
- Confirm whether the salaried-admin block vs hourly block use different master-column layouts in `Book1.xlsx` (affects the Issue 3 fix's anchoring logic).
- Click Calculate Pay on run #29 to confirm the live wipe (mechanism already proven).
