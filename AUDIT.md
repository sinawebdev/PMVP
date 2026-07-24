# Payrolla — Tenant Scoping Audit

Every HTTP route and its DB access, classified by how tenant isolation is
enforced. The guarantee: **a tenant (client) user can never read or write another
tenant's data.** Tenancy is decided by `current_user.client_company_id`
(`app/tenancy.py`), never by a URL/form/query value.

## Enforcement model

Three route classes:

1. **Platform-only** — oversight/operator routes that intentionally span all
   tenants. Guarded by `@platform_required` (redirects a tenant user to their
   Company Dashboard) or `@role_required(<operator roles>)` (a tenant user is
   never in an operator role, so `role_required` now redirects them to the
   Company Dashboard too). A tenant user gets **no cross-tenant data**, only a
   302 to `/company`.
2. **Tenant-scoped** — routes a client user uses for their own company. Data is
   filtered through `tenant_query(Model)` / `tenant_get_or_404()`; a platform
   user sees across tenants, a tenant user sees only their `client_company_id`.
3. **Public / token** — unauthenticated (marketing, health) or authorized by an
   unguessable signed token (no-login payslip links).

Child tables with no `client_company_id` (`PayrollItem`, `PaymentVoucher`,
`Remittance`, `PayslipDelivery`, `RawPayEntry`, `RawUploadArchive`) are scoped by
**joining through `payroll_run`** — the single documented strategy, implemented
once in `tenant_query()` / `owns_object()`. `AuditTrail` is scoped by the acting
user's tenant.

## Route inventory

Legend — Access: `platform` (operator only), `tenant` (own company, scoped),
`public`, `token`. Scoped?: how cross-tenant reads are prevented.

### `main` (`app/routes.py`)
| Route | Guard | DB access | Access | Scoped? |
|---|---|---|---|---|
| `/` index | none | — (redirects) | public | n/a — redirects to plane landing |
| `/health` | none | — | public | n/a |
| `/db-health`, `/admin/db-health` | `role_required(admin)` | counts across all tables | platform | operator-only; tenant users bounced |
| `/dashboard` | **`platform_required`** | all clients' runs/items/expenses | platform | tenant users → `/company` |
| `/company` company_dashboard | `login_required` | `tenant_query(Employee/PayrollRun)` | tenant | **yes** — scoped to own `client_company_id` |
| `/clients` | **`platform_required`** | all `ClientCompany` | platform | tenant users → `/company` |
| `/clients/add`,`/clients/<id>/edit` | `role_required(admin)` | ClientCompany write | platform | operator-only |
| `/clients/<id>` client_detail | **`platform_required`** | any client + its runs | platform | tenant users → `/company` |
| `/search` | **`platform_required`** | all clients + payroll items | platform | tenant users → `/company` |

### `payroll` (`app/payroll.py`)
| Route | Guard | Access | Notes |
|---|---|---|---|
| `/runs` (list/upload) | **`platform_required`** | platform | operator upload + all-tenant run list |
| `/runs/<id>` detail | **`platform_required`** | platform | any run; tenant users → `/company` |
| `/preview,/confirm,/calculate,/edit-items,/submit,/approve,/reject,/mark-paid,/delete,/export*,/items/<id>/payslip,/wage-rates` | `role_required(operator roles)` | platform | all operator mutations; tenant users bounced |

### `employees` (`app/employees.py`)
All routes `role_required(REP_ROLES)` or `role_required(admin, md)` → **platform**.
Operator roster management; tenant users bounced. Tenant-scoped, client-side
employee CRUD lives on the `/company` plane (see the `client` section below).

### `payslip` (`app/payslip.py`)
| Route | Guard | Access |
|---|---|---|
| `/payslip` index, `/payslip/generate` | **`platform_required`** | platform (operator payslip picker; tenant distribution is the client `/company/runs/<id>/distribute` routes, Phase 4) |

### `distribution` (`app/distribution/__init__.py`)
| Route | Guard | Access |
|---|---|---|
| `/distribution/run/<id>*`, `/item/<id>/preferred-channel` | `role_required(PAYROLL_ROLES)` | platform |
| `/p/<token>`, `/p/<token>/pdf` public_payslip | **none (signed token)** | token | the token authorizes one payslip; no session, no tenant leak |

### `statutory` (`app/statutory.py`)
`/statutory`, `/statutory/new` → `role_required(admin)` → **platform**. Statutory
rates are global/platform-owned (no `client_company_id`); clients are read-only
on them (read surface exposed in Phase 3). ✔ matches §4.

### `audit` (`app/audit.py`)
`/audit`, `/audit/expenses` → `role_required(admin, md)` → **platform**.

### `oversight` (`app/oversight/__init__.py`) — risk-gate control plane (Phase 5)
All routes `@platform_required` (tenant users → Company Dashboard). Platform
oversight sits *above* tenants, so it intentionally spans all clients. Scoring is in
`app/risk.py` (pure/deterministic; thresholds N=2, net-pay 15%, headcount 20%).
| Route | Guard | Access | Notes |
|---|---|---|---|
| `/oversight/risk` | **`platform_required`** | platform | all HELD runs across tenants |
| `/oversight/runs/<id>/risk-check` (POST) | **`platform_required`** | platform | scores a pre-approval run → Held / Auto-Accepted |
| `/oversight/runs/<id>/release` (POST) | **`platform_required`** | platform | Held → Pending Approval |

### `notifications` (`app/notifications/__init__.py`) — in-app inbox (Phase 6)
Per-user, both planes. Every query filters by `current_user.id`, so the owning
user IS the scope — no cross-tenant or cross-user surface.
| Route | Guard | Access | Scoped? |
|---|---|---|---|
| `/notifications` inbox | `login_required` | any user | yes — `user_id == current_user.id` |
| `/notifications/<id>/read` (POST) | `login_required` | any user | yes — 404 if not the owner |
| `/notifications/read-all` (POST) | `login_required` | any user | yes — only the user's own |

`DomainEvent` is the append-only event log (never updated/deleted in-app),
tenant-scoped by `client_company_id`; it is added to `TENANT_OWNED_MODELS`.
Events are emitted (`app/events.py`) inside the oversight risk-check/release
transitions (notifying the tenant's users) and the client distribution send
(notifying platform admins) — the notification fan-out follows the event.

### `raw_engine` (`app/raw_engine/web.py`)
All routes `role_required(admin)` → **platform** (billable raw-hours ingestion is
a platform operator flow).

### `client` (`app/client/__init__.py`) — the tenant plane (Phase 3)
All routes `@tenant_required` (platform users → oversight console) and scoped
through `tenant_query` / `tenant_get_or_404`. **Tenant** access; a cross-tenant
id returns 404.
| Route | DB access | Scoped? |
|---|---|---|
| `/company` dashboard (main.company_dashboard) | `tenant_query(Employee/PayrollRun)` | yes |
| `/company/employees` (+ add/edit/deactivate/reactivate) | `tenant_query(Employee)`, `tenant_get_or_404(Employee)`; `client_company_id` forced to tenant on write | yes |
| `/company/runs`, `/company/runs/<id>` | `tenant_query(PayrollRun)`, `tenant_get_or_404(PayrollRun)` | yes |
| `/company/runs/upload` (GET/POST) | `tenant_role_required(client_admin, client_preparer)`; `build_single_payload`/`create_payroll_run_from_payload` with `client_company_id` **forced to the tenant** (never file-detected); new run → Phase 5 risk gate | yes — file can't choose the company |
| `/company/items/<id>/payslip` | `tenant_get_or_404(PayrollItem)` (child via run) | yes |
| `/company/statutory` | global `StatutoryRate` (view-only) | n/a — platform-owned, read-only |
| `/company/expenses` | `tenant_query(Expense)` | yes |
| `/company/audit` | `AuditTrail` filtered to this tenant's users (§4 acting-user scope) | yes |
| `/company/runs/<id>/distribute` (view) | `tenant_get_or_404(PayrollRun)`; per-item `PayslipDelivery` | yes |
| `/company/runs/<id>/distribute/send`, `/resend-failed` (POST) | `tenant_role_required(client_admin)` + `tenant_get_or_404(PayrollRun)`; `distribute_run` scoped to that run | yes — client_admin only |
| `/company/runs/<id>/payslips.zip` | `tenant_get_or_404(PayrollRun)`; zips this run's payslip PDFs | yes |

## Residual items (tracked, not leaks)

- The tenant-scoped client plane (`/company`: own employees, runs, payslips) is
  implemented and always goes through `tenant_query` / `tenant_get_or_404` —
  never bare `Model.query` — so a cross-tenant id returns **404**, proven by
  `tests/test_tenant_isolation.py` and `tests/test_client_interface.py`.
- Bare `Model.query` still appears inside platform-only routes and helper
  functions; that is intentional (platform oversight spans tenants). It becomes a
  finding only if such a helper is ever called from a tenant-scoped route.
- `inject_sidebar_clients` (context processor) is tenant-scoped: a client user
  only ever sees their own company in the sidebar.
