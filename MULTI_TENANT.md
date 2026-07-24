# Payrolla — Multi-Tenant Model

Payrolla is a multi-tenant payroll SaaS: client companies log in to see and manage
**only their own** data, while the platform operator runs an oversight/control
plane above every tenant. This document is the canonical description of *how* that
isolation is guaranteed. For the route-by-route disposition, see
[AUDIT.md](AUDIT.md).

The payroll **engine is frozen**: `app/raw_engine/*`, `app/payroll_calculations/*`,
`app/money.py`, and `StatutoryRate.compute_*` are unchanged. Identical inputs
produce identical figures — pinned by `tests/test_engine_parity.py`.

## Two planes

Every request belongs to exactly one plane, decided **only** by
`current_user.client_company_id`:

| | `client_company_id` | Who | Sees |
|---|---|---|---|
| **Platform (operator)** | `NULL` | operators / oversight | across all tenants |
| **Tenant (client)** | a company id | client_admin / client_preparer | only that company |

Roles (`app/roles.py`) determine permissions *within* a plane; they never decide
the plane. A misassigned role can therefore never widen a tenant user's data
horizon — the plane is set by the (NULL-or-not) company id alone.

- **Platform roles:** `chrisnat_admin`, `chrisnat_reviewer` (+ legacy operator
  roles `admin`, `md`, `payroll_officer`, …). The `chrisnat_*` strings are a
  stable, persisted role vocabulary retained from the founding operator; they are
  treated as identifiers, not branding (see `app/roles.py`).
- **Tenant roles:** `client_admin` (can approve; maker-checker is off for v1) and
  `client_preparer`.

## The tenancy choke point (`app/tenancy.py`)

All scoping goes through one module so it can never be forgotten:

- **`active_tenant_id()`** — the active tenant, read **only** from
  `current_user.client_company_id`. Never from a URL, form field, or query param,
  so a tenant user cannot widen their horizon by editing a request.
- **`tenant_query(Model)`** — a query auto-scoped to the active tenant. Platform
  users get an unscoped query (oversight); tenant users get their company filter.
- **`tenant_get_or_404(Model, id)`** — fetch by id scoped to the tenant, else
  **404** (never a 403 that confirms the row exists, never the row itself).
- **`owns_object(obj)`** — ownership test used by the guards above.
- **Decorators:** `platform_required` (tenant users → their Company Dashboard),
  `tenant_required` (platform users → oversight console), and
  `tenant_role_required(*roles)` (narrows a client-plane action to specific
  tenant roles, e.g. distribution and run-upload are `client_admin`-gated).

### How each table is scoped

- **Tenant-owned** (carry `client_company_id` directly): `User`, `Employee`,
  `EmployeeDeployment`, `PayrollRun`, `Expense`, `Proposal`, `ImportBatch`,
  `WageRateProfile`, `DomainEvent`, `DistributionBatch`.
- **Child-via-run** (no `client_company_id`; scoped by **joining through
  `payroll_run`**): `PayrollItem`, `PaymentVoucher`, `Remittance`,
  `PayslipDelivery`, `RawPayEntry`, `RawUploadArchive`. One documented strategy,
  implemented once in `tenant_query()` / `owns_object()`.
- **`AuditTrail`** is scoped by the acting user's tenant (its users' ids).
- **`Notification`** is owned by one recipient `user_id` — the user *is* the
  scope; every query filters `user_id == current_user.id`.
- **`StatutoryRate`** is global/platform-owned; clients are read-only on it.

## Isolation guarantees (and their tests)

1. **A tenant user never reaches an oversight/operator route** — `platform_required`
   / `role_required` redirect them to `/company` (302), never 200-with-data.
   → `tests/test_tenant_isolation.py`
2. **A cross-tenant object id is 404, never the row** — direct-owned and
   child-via-run alike, through `tenant_get_or_404`.
   → `tests/test_tenant_isolation.py`, `tests/test_client_interface.py`
3. **Nothing of another tenant renders on any client page** — a response-level
   sweep asserts one tenant's marker data never appears in the other tenant's
   rendered `/company/*` pages, events, or notifications.
   → `tests/test_cross_tenant_visibility.py`
4. **Client self-service is tenant-bound at write time** — employee CRUD and
   run-upload force `client_company_id` to the tenant; the uploaded file can never
   choose the company.
   → `tests/test_client_interface.py`, `tests/test_client_run_upload.py`

## Row-Level Security (RLS)

- **Stage 1 (now):** default-deny — RLS enabled on tenant tables with **no
  policies**, so the database rejects any connection that isn't the app role.
  Isolation is enforced at the **application layer** (the choke point above) on
  every request. Reproducible via `scripts/rls_stage1.sql`.
- **Stage 2 (deferred):** JWT-based RLS policies that push tenant scoping into the
  database itself, as defence in depth behind the app layer.

## Run lifecycle + risk gate (Phase 5)

A client-submitted run does not auto-approve. It enters `Submitted`, is scored by
the deterministic risk gate (`app/risk.py`), and lands in either **`Held`** (parked
for platform review) or **`Auto-Accepted`**. Three settled rules; a run tripping
**any** rule is held:

1. **New-client hold** — a client's first **2** runs are always held.
2. **Net-pay variance** — total net pay moves **>15%** from the previous *closed*
   run (Approved/Processed).
3. **Headcount swing** — worker count moves **>20%** from that run.

The platform operator releases a held run into `Pending Approval` from the
oversight Risk Queue (`/oversight/risk`). The operator lifecycle
(`Draft → Pending Approval → Approved → Processed`) is untouched; the gate is
additive. → `tests/test_risk.py`

## Events + notifications (Phase 6)

`DomainEvent` is an append-only, tenant-scoped business-event log (never mutated
in-app). `record_event` (`app/events.py`) appends an event and fans it out to
recipient users as per-user `Notification` rows. The flow is bidirectional:

- The platform **holds/releases** a run → the client's users are notified.
- A client **distributes payslips** or **uploads a run** → platform oversight is
  notified.

→ `tests/test_events.py`

## Client capabilities (the `/company` plane)

Dashboard · Employees (self-service CRUD) · Payroll runs (upload, list, detail) ·
Payslips (single PDF + run ZIP) · **Distribution** (SMS/WhatsApp/email, console in
v1) · Statutory (read-only) · Expenses · **Audit** · **Notifications**. Every route
is `@tenant_required` and scoped through the choke point.

## Money

Money stays **Float (stored) + Decimal (computed via `app/money.py`)** — it is
**not** converted to integer pesewas. This preserves exact parity with the
payroll engine's reference figures (`tests/test_engine_parity.py`).
