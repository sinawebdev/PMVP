# Changelog

All notable changes to Payrolla. This project targets a **v1.0** production
milestone; entries are grouped by the capabilities that make up that release.

## [1.0.0] — Payrolla v1.0 (in preparation)

The first production-ready release of Payrolla — **Payroll, HR, Compliance &
Workforce Management Platform** by Sinaforte Technologies — consolidating the
multi-tenant SaaS built on top of the original single-operator payroll app.

### Operations & onboarding

- **Client onboarding.** Add Company collects the full onboarding record
  (company code, contact, email, phone, address, status, notes), validates it
  inline, and lands the operator on an onboarding summary that restates the key
  details and the one manual step left: provisioning credentials in Supabase
  Authentication. Company creation is deliberately independent of authentication
  — Payrolla never mints an auth user.
- **Client expense management.** A client company records, edits and deletes its
  own operational expenses against a fixed category list, with totals, a
  category breakdown and a by-month chart that feed the company dashboard
  directly.
- **Professional login identities.** The demo roster moved to `@payrolla.com`
  (platform) and per-company domains (`@msc.com`, `@acme.com`), with an optional
  login-page hint panel built from the seed itself. Role strings — and therefore
  every permission check — are unchanged.
- **Executive analytics on the admin dashboard.** Book-wide quick stats, a
  monthly payroll trend, a companies-by-cost ranking, a payroll status donut, a
  client-growth line, a Top Clients table and a payroll activity timeline — all
  server-rendered SVG/CSS with no chart library, aggregated from data the page
  already loads (the client list is now eager-loaded, removing an N+1).
- **`flask demo-reset`.** An explicit, idempotent, transaction-safe command that
  strips demo/test tenants and obsolete logins (child rows first, no orphans;
  surviving history is reassigned rather than deleted) and rebuilds the two
  professional tenants with months of real calculated payroll history, expenses
  and delivered payslips.

### Stabilization & brand consolidation

- Migrated the product identity from the legacy "Chrisnat Payroll MVP" to
  **Payrolla** across code comments, docstrings, user-facing strings, the
  front-end JS/CSS namespace, config, and documentation. Business entities and
  live deployment identifiers (GRA employer defaults, operator-side export
  filenames and letterhead, the `render.yaml` service name) are deliberately
  preserved — see the branding note in the [README](README.md).
- **Platform roles renamed** to `payrolla_admin` / `payrolla_reviewer` (from
  `chrisnat_*`), completing the rebrand of the permission model. A role names a
  capability in this product, not the founding bureau, so it belonged on the
  product's name. Shipped as a reversible data migration (`a4e7c2b81d95`) that
  rewrites `user.role` plus the three historical role columns
  (`audit_trail.user_role`, `domain_event.actor_role`,
  `distribution_batch.initiated_by_role`). No user gains or loses a capability:
  `app.roles.normalise_role` folds the pre-rename spellings onto the current
  names, so access is identical before, during and after the migration, and rows
  that predate it (an un-migrated replica, a restored backup) keep working.
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
