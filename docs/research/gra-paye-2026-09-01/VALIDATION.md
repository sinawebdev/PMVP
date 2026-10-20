# Validation - 20 October 2026

This records the initial PAYE-band phase. Current deduction-phase checks are
recorded in [deductions/VALIDATION.md](deductions/VALIDATION.md).

Validation used the existing project Python 3.13 environment and disposable
in-memory SQLite databases. Dotenv loading and inline distribution delivery
were disabled. No production or customer database was used.

| Scope | Result |
| --- | --- |
| New `tests/test_paye_2026.py` suite, final focused run | 12 passed; 92 subtests passed |
| Existing calculation, compute-spec, parity, raw compute, thin-import and raw-export suites in the broader regression run | 62 passed; 13 skipped because optional real-client workbook fixtures were unavailable |
| Full Alembic baseline through previous head, followed by the new data migration with an existing rate record | Passed |
| Migration downgrade and re-upgrade, retaining rate history | Passed |
| Alembic schema comparison after migration | No new upgrade operations detected |
| Archived source hashes, original PDF signature and source JSON | Passed for all four downloaded source files |
| Changed Python source parsing and seed diff whitespace check | Passed |

The first broader run reported three failures in the new migration tests:
their snapshots compared an in-memory timezone-aware timestamp with SQLite's
timezone-naive persisted representation. The tests were corrected to reload
both snapshots from storage. The final focused run passed all twelve tests;
the PAYE calculation and migration code did not require a change for that
test-fixture correction.

The new tests check all six thresholds below/at/above the boundary, half-up
rounding, zero/top-band incomes, August/September/October selection, preserved
historical client figures, salaried/hourly/raw calculations, ancillary-setting
preservation, custom/future versions, repeated seeding/migration, and the
no-prior-rate case.

An independent source review also confirmed the official band portions,
effective date, cumulative tax bases, migration parent and archived hashes.
PostgreSQL execution, live deployment and an installed desktop executable
were not part of this verification.
