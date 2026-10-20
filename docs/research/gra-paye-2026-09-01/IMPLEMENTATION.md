# Payroll engine implementation

These notes describe the initial PAYE-band update. The subsequent deduction
policy, approval workflow and migration are documented in
[deductions/IMPLEMENTATION.md](deductions/IMPLEMENTATION.md).

The source update is in `payrolla-redesign-web`. The dated `StatutoryRate`
record is shared by the salaried, hourly-shift and raw-hours calculation paths.
Their existing Decimal arithmetic and half-up rounding remain in use.

## Monthly calculation data

These cumulative thresholds and tax bases are derived from the official
band portions cited in [README.md](README.md). At and above each threshold,
PAYE is `base + (chargeable_income - threshold) * marginal_rate`.

| Monthly threshold (GHS) | Marginal rate | Tax on lower portions (GHS) |
| ---: | ---: | ---: |
| 588.00 | 5% | 0.00 |
| 668.00 | 10% | 4.00 |
| 768.00 | 17.5% | 14.00 |
| 3,668.00 | 25% | 521.50 |
| 19,668.00 | 30% | 4,521.50 |
| 50,000.00 | 35% | 13,621.10 |

For example, chargeable monthly income of GHS 5,000 gives PAYE of
`521.50 + (5,000 - 3,668) * 25% = GHS 854.50`.

## Effective dates and saved data

- The new PAYE version starts on **2026-09-01**. Existing run-period selection
  uses the first day of the payroll month, so August keeps the earlier version
  and September uses the new version.
- Earlier saved rate records and stored payroll amounts are not rewritten.
  The old default table and old early-return guard are retained as comments
  in `app/seed.py`, as requested. A separate historical table retains the
  exact workbook-era defaults so earlier client calculations stay reproducible.
  This update does not retrospectively certify or correct those old defaults.
- A new version copies pension, overtime, bonus, ICU and junior-staff settings
  from the latest configured version **before** September. It changes PAYE only.
- Existing records dated September 1 and later remain operator-owned and are
  not overwritten. An already-configured later version will still take
  precedence for its effective period; check that version before relying on it.
- Repeated seeding and migration execution do not add duplicate versions. If
  there is no earlier configuration, the migration leaves the table alone;
  the normal fresh-install seed supplies the initial and September versions.
- The sources do not establish a mixed-rate annual reconciliation formula for
  2026. No such annual rule is added by this monthly PAYE update.

## Existing-install upgrade

`migrations/versions/8b62026a0901_add_september_2026_paye_rates.py` follows
revision `3f2984c70c03`. It supplies the new version to existing databases even
when production disables boot seeding. Use the application's usual configured
environment and migration procedure:

```powershell
python -m flask --app run.py db upgrade
```

Migration downgrade intentionally retains the financial rate record. Removing
it could change historical calculations, and the record may have pre-existed
the upgrade or been edited by an operator.

This work changes source files and tests. It does not deploy the web app,
modify a production/customer database, rebuild a desktop installer, or update
an installed executable. The desktop vendor tree is a separate mechanical
mirror and must be advanced through its existing sync workflow before packaging.

## Verification

The focused tests use disposable in-memory SQLite databases, with dotenv
loading and external delivery disabled. They cover hand-calculated band
boundaries, rounding, period selection, old client results, all three engine
paths, ancillary-setting preservation, idempotence and the migration.

```powershell
python -m pytest -p no:cacheprovider tests/test_paye_2026.py tests/test_calculations.py tests/test_compute_engine_spec.py tests/test_engine_parity.py tests/test_raw_engine_compute.py tests/test_raw_engine_thin.py tests/test_raw_engine_exports.py -q
```

The original downloaded references are listed in `sources.json`; their saved
bytes are fingerprinted in `SHA256SUMS.txt`.
