# Changelog

All notable changes to Payrolla. This project targets a **v1.0** production
milestone; entries are grouped by the capabilities that make up that release.

## [1.0.0] — Payrolla v1.0 (in preparation)

The first production-ready release of Payrolla — **Payroll, HR, Compliance &
Workforce Management Platform** by Sinaforte Technologies — consolidating the
multi-tenant SaaS built on top of the original single-operator payroll app.

### Stabilization & brand consolidation

- Migrated the product identity from the legacy "Chrisnat Payroll MVP" to
  **Payrolla** across code comments, docstrings, user-facing strings, the
  front-end JS/CSS namespace, config, and documentation. Business entities and
  persisted identifiers (the `chrisnat_*` role vocabulary, the `@chrisnat.local`
  demo domain, GRA employer defaults, and operator-side export filenames) are
  deliberately preserved — see the branding note in the [README](README.md).
- Product names are read from a single **branding seam** (`APP_NAME`, …), so a
  rebrand or white-label is a config change, not a template sweep.
- Removed dead code (unused imports, an unused local computation, a commented-out
  example block) and stale development artifacts.
- Rewrote and expanded the documentation set for onboarding: `README`,
  [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY.md](SECURITY.md),
  [DEPLOYMENT.md](DEPLOYMENT.md), [CONTRIBUTING.md](CONTRIBUTING.md), and
  refreshed [MULTI_TENANT.md](MULTI_TENANT.md) and [AUDIT.md](AUDIT.md).

### Multi-tenancy

- Two-plane model: a tenant (`/company`) plane and a platform oversight plane,
  separated **only** by `client_company_id` through the `app/tenancy.py` choke
  point. Tenant isolation is pinned by dedicated isolation and cross-tenant
  visibility tests.
- Row-Level Security Stage 1 (default-deny) with application-layer enforcement.

### Client self-service

- Preview-first payroll import (draft → preview → confirm → replace) with
  resumable import drafts and upload progress.
- Self-service employee CRUD, run/payslip views, expenses, audit, and
  notifications — all tenant-scoped.
- Self-service reports & exports (payroll workbook, bank listing, GRA PAYE)
  named for the client, not the bureau.

### Payroll workflow & oversight

- Full operator lifecycle (`Draft → Pending Approval → Approved → Processed`)
  with a status/progress model, activity timeline, and bulk approve/reject/
  distribute.
- Deterministic **risk gate**: client-submitted runs are scored and either
  auto-accepted or held for platform review (`/oversight/risk`).
- Possible-duplicate detection and comparison against the previous closed run.

### Payslip distribution

- Background, DB-backed delivery queue (durable, non-blocking) with an inline or
  dedicated worker, crash-safe batch reclaim, and idempotent sends.
- SMS / WhatsApp / email channels behind one interface (console backends by
  default); branded email templates; provider delivery receipts and webhooks;
  rate limiting; scheduled sends; retries with backoff; searchable history;
  analytics/exports; per-tenant branding packs; and SLA monitoring/alerts.

### Reliability, performance & security

- Startup ORM-mapper configuration to remove a first-request mapper race under
  gunicorn threads + the background worker.
- Query-backed indexes on hot tables and N+1 elimination on the runs list and
  bulk actions.
- Global CSRF protection, fail-closed provider webhooks, secret/email hardening,
  and production guards that refuse SQLite or the insecure dev `SECRET_KEY`.

---

_Older history is available in the Git log; the entries above summarise the
capabilities delivered on the road to v1.0._
