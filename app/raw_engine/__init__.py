"""Raw Hours Payroll Engine.

A parallel processing path to the Standard Payroll Engine, for hourly-rate
clients whose monthly payroll is driven by raw hours worked. The Standard
Engine is untouched; the engine is chosen per upload, never stored as a mode.

Phase 1 (this milestone) is the **seed path**: a rich RAW-DATA workbook is
parsed and its context persisted — employees, per-employee hourly rates, union
membership and basic wage — behind a preview -> confirm step. A company is
"seeded" once it has WageRateProfile rows; later thin monthly uploads join
against that context.

Public entry points:
  * :func:`app.raw_engine.detection.is_rich_raw_data` — format sniff
  * :func:`app.raw_engine.detection.company_is_seeded` — routing
  * :func:`app.raw_engine.seed.parse_rich_workbook` — parse -> SeedContext (preview)
  * :func:`app.raw_engine.store.persist_seed` — transactional confirm
"""
