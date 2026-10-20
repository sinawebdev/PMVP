# Ghana GRA PAYE references — effective 1 September 2026

Checked against the Ghana Revenue Authority's published sources on **20 October 2026**. This folder records the evidence for the resident-individual PAYE band update in the payroll engine.

The subsequent deduction implementation is documented in
[deductions/README.md](deductions/README.md), with its
[implementation notes](deductions/IMPLEMENTATION.md) and
[validation results](deductions/VALIDATION.md).

## Verified schedule

GRA's announcement and one-page implementation notice identify the **Income Tax (Amendment) Act, 2026 (Act 1178)** and give **1 September 2026** as the implementation date. GRA's PAYE page independently repeats the schedule and date. [Sources 1–3](#official-sources)

Amounts below are **Ghana cedis (GH¢)** and represent portions of **chargeable income**, after allowable deductions. Each rate applies only to its portion. “Exceeding” is a total-income threshold, not another band width.

| Monthly income portion | Annual income portion | Rate |
| --- | --- | ---: |
| First 588.00 | First 7,056.00 | 0% |
| Next 80.00 | Next 960.00 | 5% |
| Next 100.00 | Next 1,200.00 | 10% |
| Next 2,900.00 | Next 34,800.00 | 17.5% |
| Next 16,000.00 | Next 192,000.00 | 25% |
| Next 30,332.00 | Next 363,984.00 | 30% |
| Total exceeding 50,000.00 | Total exceeding 600,000.00 | 35% |

Arithmetic cross-check: the six finite monthly portions total **50,000.00**; annual portions total **600,000.00**. Every published annual portion is twelve times its monthly counterpart.

The earlier **Income Tax (Amendment) (No. 2) Act, 2023 (Act 1111)** schedule took effect on **1 January 2024**, according to GRA's earlier implementation announcement. [Source 4](#official-sources)

## Official sources

1. **GRA — Amendments – Income Tax Act, 2026 (Act 1178).** Confirms the new monthly/annual bands and implementation date. [Official page](https://gra.gov.gh/news/portfolio/amendments-income-tax-act-2026-act-1178/) · [Saved HTML](gra-2026-announcement.html).
2. **GRA Domestic Tax Revenue Division — Amendments – Income Tax Act, 2026 (Act 1178).** One-page implementation notice; both band tables and the date are on page 1. [Official PDF](https://gra.gov.gh/wp-content/uploads/2026/09/Amendments-To-The-Income-Tax-2015-Act-896_Updated-1.pdf) · [Saved original PDF](gra-2026-implementation-notice.pdf).
3. **GRA — Pay As You Earn (PAYE).** Confirms the resident-individual tables, 1 September 2026 date, and deductions before PAYE. [Official page](https://gra.gov.gh/domestic-tax/tax-types/paye/) · [Saved HTML](gra-paye-page.html).
4. **GRA — Implementation of New Tax Laws and Amendments.** Published 31 January 2024; its Act 1111 section and closing implementation paragraph support the earlier 1 January 2024 effective date. [Official page](https://gra.gov.gh/implementation-of-new-tax-laws-and-amendments/) · [Saved HTML](gra-2024-implementation-announcement.html).

## Evidence boundaries

- The saved 2026 PDF is GRA's **implementation notice**, not the full gazetted Act. The full Gazette text was not independently inspected for this update.
- The annual figures above are the schedule GRA publishes. These sources do not establish a special reconciliation formula for the mixed-rate 2026 year; no such formula is inferred here. The new monthly bands start with September 2026 payroll, preserving the earlier schedule for earlier payroll periods.
- These references support the resident graduated-band change. They do not support changes to SSNIT, bonuses, overtime, reliefs, or non-resident tax treatment.
- The PDF and HTML files are downloaded source copies. HTML copies may reference online styling/assets. [sources.json](sources.json) maps each copy to its original URL. [SHA256SUMS.txt](SHA256SUMS.txt) fingerprints the saved source bytes for integrity checking; a hash does not prove legal authenticity.
