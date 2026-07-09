# PMVP Implementation 01 — Minimal Upload Fixes (changelog)

Minimal, architecture-aware stabilization. No multi-tenancy, no approval-flow, no
header/mapping/calculation/validation redesign. Company detection retired at the
source; statutory warnings reworded to match the fact that PMVP computes PAYE/SSNIT
itself. Full test suite green (see "Verification").

## 1. Files modified

| File | Change |
|---|---|
| `app/payroll.py` | Stopped calling the `detect_company_name` heuristic; removed the now-unused import; leaves `detected_company_name` null (not `client.name`). |
| `app/validators.py` | Removed the two company-detection-consequence warnings; reworded PAYE/SSNIT/net messages to reflect PMVP's compute-on-confirm workflow. |
| `app/templates/payroll_preview.html` | Removed the redundant "Detected Company" stat card. |
| `app/seed.py` | Stopped seeding `detected_company_name=client.name`; the demo run leaves the field null too. |
| `tests/test_bank_branch_and_fixes.py` | Updated the one test that pinned the retired "looks like a column heading" company warning. |

## 2 & 3. Each change and why

**`app/payroll.py` — retire the heuristic at the source.**
Both upload paths previously computed `detected_company_name` by scanning the
workbook with `detect_company_name()`. That scan produced the false `"GH CARD"`
(it returned the cell after the `COMPANY ASSIGNED` header). The company is already
authoritative at upload time — single-client uploads *require* selecting a client,
and multi-client resolves each sheet/group to a `ClientCompany` record — so the two
`detect_company_name(...)` calls are dropped, along with the import. Company
detection is retired completely: `PayrollRun.detected_company_name` is left **null**
(not repopulated with `client.name` or the matched `group_name`) — the field no
longer represents anything and will be dropped in a future schema cleanup rather
than repurposed. This is the smallest change that makes `GH CARD` impossible without
touching header detection, mapping, or calculations.

**`app/validators.py` — remove company-consequence warnings.**
With company detection retired, the "Selected client is X, but Excel appears to
mention Y" mismatch warning and the "detected company name looks like a spreadsheet
column heading" warning were both dead-and-misleading (they existed only to flag bad
output of the retired scan, and were the visible symptom of the `GH CARD` bug). Both
blocks were removed and replaced with a comment documenting the retirement. No other
logic in `validate_payroll_rows` changed. (`detected_company_name` is now always
null/empty, so the retained `collect_blocking_errors` header-label guard stays
dormant.)

**`app/templates/payroll_preview.html` — drop the redundant card.**
The preview already shows a "Client Company" card (`client.name`). With company
detection retired there is nothing left to "detect", so the separate "Detected
Company" card was removed. Removed one line.

**`tests/test_bank_branch_and_fixes.py` — realign the pinned test.**
`test_company_name_that_is_a_header_label_warns` asserted the retired warning is
*produced*. It was rewritten as `test_header_label_company_name_no_longer_warns`,
asserting the warning is *not* produced — a regression guard against reintroducing
the noisy company warnings. No other test changed.

## 4. Warnings intentionally retained (and why)

- **"Payroll already exists for {client} in {month} {year}."** — genuine duplicate-run
  detection from the database; nothing to do with company detection. Kept verbatim.
- **"Payroll total is unusually higher than a previous approved payroll."** — genuine
  anomaly signal (it flags the inflated-net situation seen in Investigation 01). Kept.
- **Duplicate-worker, cross-client-worker, missing-bank-details, negative/zero/high
  salary, high-deductions warnings** — all genuine data-quality checks, untouched.
- **The per-row name guard** ("Employee name … looks like a column heading or
  placeholder") in `validate_single_row` — this is *row-level data-shift* detection,
  independent of company detection, and still valuable. Kept, and its tests still pass.
- **`collect_blocking_errors` company-heading hard-block** — deliberately left
  UNCHANGED. It only ever fires when `detected_company_name` looks like a header
  label; since that value is now always the real client name, it is dormant in
  production, but retaining it (a) preserves the hard-stop safety net if a header-like
  company is ever passed in, and (b) avoids churn in `test_compute_engine_spec.py`,
  which still passes. Documented rather than deleted.

## 5. Warnings whose wording changed (and reasoning)

PMVP computes PAYE, SSNIT and the other statutory figures itself when a run is
confirmed (`auto_calculate_on_confirm`; "any SSF/PAYE numbers from the uploaded
file are preview-only and never survive"). The old messages implied the client had
failed to supply values that PMVP is responsible for calculating. Behaviour
(triggers, severity, row tagging) is unchanged — only the text:

| Old | New |
|---|---|
| `PAYE total is missing or zero.` | `No PAYE in the upload — PMVP will calculate it when the run is confirmed.` |
| `SSNIT total is missing or zero.` | `No SSNIT in the upload — PMVP will calculate it when the run is confirmed.` |
| `Missing PAYE.` | `PAYE not provided; PMVP will calculate it on confirm.` |
| `Missing SSNIT.` | `SSNIT not provided; PMVP will calculate it on confirm.` |
| `Net pay missing; calculated by system.` | `Net pay not provided; PMVP will calculate it on confirm.` |
| `Net pay calculation mismatch.` | `Uploaded net pay doesn't reconcile with its components; PMVP recalculates all figures on confirm.` |

Reasoning: each reframes a normal salary-only upload as expected input, not client
error, consistent with PMVP owning the statutory math.

## 6. Issues discovered but intentionally left untouched (out of scope)

- **`detect_company_name()` in `excel_utils.py` is now unused by the pipeline** but
  left in place (a test still exercises it as a convenience). Flagged for later removal.
- **`client_name_matches()` in `validators.py` is now unused** (its only caller was
  the removed mismatch warning). Left defined to keep the diff minimal; safe to delete later.
- **`known_names` is now unused inside `build_single_payload` / passed unused into
  `build_run_payload_from_extraction`.** Left as-is to avoid touching signatures;
  candidate for a small cleanup.
- **`app/imports/header_resolver.py` remains dead code** (noted in Investigation 01).
- **Source-data problems in `acs 1.xlsx`** — the `#REF!` NET PAY column and a stray
  `gross=0 / net=200000` row — are client-file issues, not pipeline bugs. Not fixed here.
- **Per-row "PAYE/SSNIT not provided" still tag every row as `Warning`.** Wording is
  now accurate, but on a salary-only sheet every row still carries the note. Demoting
  it to a single summary line would reduce noise — deferred as a behaviour change
  beyond today's wording-only scope.

## Embedded decisions

1. **Left `detected_company_name` null** in all paths (single, multi-client, and the
   seed run) rather than feeding it `client.name`/`group_name`. Per the agreed
   retirement, the field is not repurposed — it stays unused/null until it is dropped
   in a future schema cleanup. (Corrects an earlier draft that fed `client.name` in.)
2. **Removed the "Detected Company" preview card** rather than relabel it, because
   "Client Company" is already shown right beside it and there is no longer anything
   to detect.
3. **Kept `collect_blocking_errors` intact** (dormant company block) instead of
   stripping the header-label logic everywhere — preserves a safety net and test. It
   is a no-op now that `detected_company_name` is always null/empty.

## Verification

All test suites pass (SQLite in-memory, seeded):
`test_header_mapping` + `test_compute_engine_spec` (17), `test_bank_branch_and_fixes`
(15), `test_distribution` (9), `test_calculations` (38), `test_post_launch_fixes` (14),
`test_mvp` (49, 1 environment-skip). The only failures encountered were missing
sandbox packages (`psycopg2`), unrelated to these changes. `payroll.py` and
`validators.py` compile clean; no pipeline call to `detect_company_name` remains.
