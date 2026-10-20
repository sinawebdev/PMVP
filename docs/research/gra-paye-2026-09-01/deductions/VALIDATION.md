# Deduction validation - 20 October 2026

## Final focused run

**52 tests passed; 122 subtests passed; zero failures.**

```powershell
python -m pytest -p no:cacheprovider tests/test_deduction_workflows.py tests/test_deduction_calculators.py tests/test_deduction_policy.py tests/test_deduction_migration.py tests/test_paye_2026.py tests/test_raw_engine_web.py::MigrationUpDownTests -q
```

This run used the existing project Python 3.13 environment, isolated in-memory
SQLite databases and temporary test files. Dotenv loading and the inline
distribution worker were disabled. No production/customer database was used.

Covered behavior:

- Every current PAYE band boundary, half-up monetary rounding and preserved
  historical workbook tax results.
- Shared salaried, hourly-shift and raw-hours deduction calculations.
- Junior status, qualifying annual income and matching assessment year for
  concessionary overtime; ordinary PAYE when evidence is absent or fails.
- Registered-scheme PF eligibility, actual cash versus allowable tax amounts,
  the combined cap, employer cost separation and ambiguous-allocation refusal.
- Approved monthly GRA awards, valid dates, retained award history, explicit
  revocation and a relief cap preventing negative ordinary chargeable income.
- Operator and company forms, tenant binding, invalid/non-finite metadata and
  employer-PF inputs, payroll-grid input allowlisting and protected snapshots.
- Raw-upload deduction failures return actionable errors and roll back the
  entire new payroll for both operator and tenant routes.
- Standard calculations stage all results first, preventing partial employee
  updates if a later worker requires deduction review.
- Generated thin-template alignment and employee/employer PF plus allowance
  round trips; raw seed calculations require the actual run/roster evidence.
- Pension contribution/remittance separation, verified-range/year guards,
  the published minimum discrepancy and explicit component-rounding warnings.
- Stored GRA relief/PF snapshots remain unchanged by later roster edits, and
  strict exports satisfy the bundled template's column arithmetic.
- Full Alembic baseline to the old schema, seeded pre-update employee/payroll
  records, upgrade through both new revisions, idempotent head upgrade and a
  schema comparison reporting **No new upgrade operations detected**.
- The new migration preserves stored pay and does not invent approvals.
  Destructive downgrade is refused without deleting evidence. The older
  pay-type migration test now exercises its own revision rather than crossing
  unrelated later evidence-preserving migrations.

## Additional regression evidence

- The existing numerical calculator, client-workbook characterization, raw
  unit and engine-parity assertions passed in the calculator regression run.
  Optional decrypted-client-workbook tests were skipped where fixtures were
  unavailable; those specimens are not replaced with invented client data.
- Correctly bootstrapped payroll grid regression class: **4 passed**.
- Compute-engine specification, raw exports and new calculator suite:
  **28 passed, 4 optional fixture skips, 11 subtests passed**.
- Company interfaces, company reports, post-launch fixes and the initial new
  workflow suite: **43 passed**.
- The combined pre-final run passed all demo-reset regressions. Its sole
  failure was the migration test's log-capture harness; the final run above
  tests the actual Alembic downgrade exception directly and passes.
- All ten new official-source copies matched their SHA256 manifest. Relevant
  changed Python files parsed, and scoped Git whitespace checks passed.

Some intermediate failures were corrected in the tests: the old PAYE-only
fixtures assumed unapproved OT/PF/relief concessions, a logging capture was
replaced by direct migration exception checking, and a test switching users
within one outer application context now clears Flask-Login's cached user.
The final focused command above includes the resulting tests and all passes.

## Limits and existing tool warnings

This verifies the local source and SQLite workflows. PostgreSQL execution,
browser screenshot review, a successful live GRA portal upload, production
deployment, desktop packaging and an installed executable were not tested.

Openpyxl warns that it drops an unsupported WMF graphic from the bundled GRA
template; numeric fields and formula relationships were verified. Alembic's
existing environment warns about a deprecated engine accessor. Neither warning
is counted as a test failure or hidden as an untested filing guarantee.

Pension cases outside the verified range/year, the minimum contribution
discrepancy and mixed employer/employee PF above the shared cap remain explicit
review cases. See [README.md](README.md) for the source uncertainty and
[IMPLEMENTATION.md](IMPLEMENTATION.md) for the implemented boundaries.
