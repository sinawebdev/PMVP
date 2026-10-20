# Deduction implementation - 20 October 2026

This update is in `payrolla-redesign-web`. [README.md](README.md) records the
official rules, exact sources, older documents and unresolved questions.
The saved PDFs/HTML are originals; `sources.json` and `SHA256SUMS.txt` identify
their provenance and bytes.

## Evidence standard

Production defaults use explicit statements from GRA, SSNIT or NPRA primary
sources corroborated where possible. These are assessed as high-confidence
sources for the particular cited statements. This is a source-quality
assessment, not a statistically measured probability of legal correctness.

An official hosting domain alone is insufficient: the NPRA regulations copy
with blank enactment details is retained as unverified supporting material.
It does not supply a calculation default. The older SSNIT FAQ ceiling and
2016 GRA tax bands are superseded and are not used as current values.

## Shared rules and employee data

`app/payroll_calculations/deductions.py` supplies one policy to salaried,
hourly-shift and raw-hours calculations. Relevant replaced calculation blocks
remain commented in the source.

- **Pensions:** employee 5.5% and employer 13% remain separate within the
  verified range. Stored Tier 1 (13.5%) and Tier 2 (5%) remittances are employer
  allocation information, not additional deductions from employee net pay.
  Separate component rounding can differ by a pesewa; the engine flags that
  reconciliation difference instead of silently changing a contribution.
- **Pension guards:** positive basic pay outside GHS 587.80-69,000 stops
  calculation. The engine also stops when the computed SSNIT remittance is
  below the notice's printed GHS 79.40 minimum, when the payroll year differs
  from the configured notice year, or when contribution rates lack the
  verified allocation. No unverified cap, floor, top-up or Tier 2 transfer is
  invented. Zero basic pay produces zero pension contributions; this does not
  certify a statutory exemption or establish every zero/partial-month case.
- **Provident fund:** actual employee cash deductions are distinct from
  tax-allowable contributions. A documented approved scheme is required for
  tax relief. With employee-only PF, the tax deduction is limited to 16.5% of
  basic salary while the full employee contribution remains a cash deduction.
  Employer PF is a separate company cost, never deducted from employee net pay.
  Both parties share the cap; mixed contributions above it stop pending an
  evidenced allocation. The code does not assume which party consumes it first.
- **Overtime:** concessionary tax requires explicit junior status, an annual
  qualifying-income figure of at most GHS 18,000, and a matching assessment
  year. Otherwise overtime joins ordinary PAYE and a warning records why.
  Junior status and annual income are supplied and confirmed by the employer;
  they are not inferred from a current monthly wage or workbook position.
- **Relief:** record the GRA approval reference, category, approved monthly
  amount and start/end dates. There is no automatic grant from marital status,
  age or an imported amount. The approved amount is applied only in its period
  and is capped at the remaining ordinary taxable income. The engine does not
  assume that an annual award should always be divided by twelve.

Operator and company employee forms capture this evidence. Relief awards are
appended to `employee_tax_relief_approval`; older valid awards remain available
for earlier periods. A later explicit zero award can revoke relief for its
specified dates. No approval or junior status is invented for existing workers.

## Inputs, snapshots and exports

Standard imports and the payroll-edit grid accept employer PF separately.
Rich raw uploads and monthly thin files carry employee/employer PF, and the
thin file now carries other allowance. Generated templates maintain correct
column alignment. Imports cannot approve a scheme or a relief merely by
containing an amount. Existing documented relief is not overwritten by a raw
workbook's unapproved amount.

Each calculated payroll item stores the actual relief used, allowable employee
PF, ordinary overtime, pensionable salary and pension remittances. Exports read
these snapshots instead of recalculating old awards from today's employee data.
The employee's actual PF remains visible on the payslip. Employer remittances
are kept outside employee cash deductions.

For the current bundled GRA template, strict-policy exports follow its formula
relationships: `O=F+K+N`, `S=O+P+Q+R`, `U=I+J+T`, `V=S-U`, `Z=M+W+Y`.
Ordinary overtime goes into ordinary cash allowances; concessionary overtime
and bonus stay in their separate fields. Relief and allowed PF use the stored
calculation snapshots. Contract tests verify these relationships. This is not
proof of a successful GRA portal upload or a full certification of every
external filing format.

All standard rows are calculated successfully before new results are applied,
so an unresolved pension/PF case cannot leave an earlier employee partly
recalculated. Raw confirmation retains its transaction rollback boundary.

## Activation and migration

Migration `9d2026c1020` follows the PAYE migration `8b62026a0901`. It adds the
evidence fields, relief history, policy configuration and calculation snapshots.
It does not rewrite existing payroll amounts or promote old relief amounts to
approved awards.

Rate versions before **2026-09-01** retain explicit policy version 0 for
historical workbook reproducibility. September-and-later versions use policy
version 1 when a payroll is calculated again. September is this application's
activation boundary alongside the PAYE update, not a claim that the underlying
deduction rules first became law on that date. Earlier workbook behavior is
not retrospectively certified as legally correct by this compatibility mode.

Use the application's configured database environment and take its normal
backup before applying the migration. Disable automatic development seeding
while migrating an existing schema:

```powershell
$env:AUTO_INIT_DB = 'false'
python -m flask --app run.py db upgrade
```

After applying it, review employee eligibility/approval records and the relevant
draft payroll. Stored completed payrolls are not automatically recalculated.
The new schema's automatic downgrade refuses to delete approval evidence and
payroll snapshots; an audited recovery plan is required for schema removal.

The source update is not a deployment. Production/customer databases, the live
site and the installed desktop application were not changed. Desktop vendor
sync, packaging and installation must follow the existing release workflow.

## Outstanding evidence

The engine deliberately refuses the pension and mixed-PF cases described above.
They need authoritative allocation guidance before automatic processing can be
added. The current work covers the existing resident payroll workflow; it does
not certify casual-worker, non-resident, special-exemption or all non-cash-benefit
cases. Current relief amounts are documented as references; the software applies
an evidenced award rather than guessing a category entitlement.

See [VALIDATION.md](VALIDATION.md) for executed checks and their limits.
