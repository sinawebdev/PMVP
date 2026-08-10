# Operator dashboard query load — latent risk

- **Status**: NOT URGENT — the page works today (verified 2026-08-07 after the F1–F9 deploy)
- **Where**: `app/routes.py:104` — `main.dashboard()`
- **Trigger to act**: real payroll history accumulating, or the page getting perceptibly slower
- **Not a confirmed past cause.** The 2026-08-07 post-login hang happened on the
  older `origin/main` build and resolved with the new deploy. The pre-crash log
  lines were never captured, so that incident's root cause is still unproven. What
  follows is a real cost characteristic of the current code, not a diagnosis of
  that outage.

## Why this is on a list at all

The free plan gives 0.1 CPU and 512 MB, with pandas already resident. `render.yaml`
says so itself: *"the free plan's 512MB doesn't fit two pandas-loaded workers"*.

The failure mode matters more than the slowness. The service runs gunicorn
**gthread** with `--timeout 300`. That timeout watches the *worker heartbeat*, not
the request — so a thread stalled inside a request **does not trip it**. The page
hangs indefinitely rather than returning 500. With `--threads 4`, four stalled
requests take the whole service down with nothing useful in the log.

So when this does bite, it will present as *"login spinner never resolves"* and the
access log will show **no line at all** for the hanging request (gunicorn logs on
response completion). Don't go hunting the login route again — it was innocent last
time too.

## The three specific things

### 1. N+1 over payslip rows — `routes.py:177`

```python
payslips_total = sum(len(run.items) for run in sendable_runs)
```

`PayrollRun.items` is a plain `db.relationship` (`models.py:261`) with default
`lazy="select"`, and `current_runs` (`routes.py:117`) is fetched with **no** eager
loading. So this is one SELECT per sendable run, and it fully materialises every
`PayrollItem` ORM object just to call `len()` on them.

Fix in SQL, not with `selectinload` — eager-loading still pulls every row:

```python
payslips_total = (
    db.session.query(func.count(PayrollItem.id))
    .filter(PayrollItem.payroll_run_id.in_([r.id for r in sendable_runs]))
    .scalar()
    if sendable_runs else 0
)
```

`payslips_delivered` immediately below (`:178-188`) is already done correctly —
match its shape.

### 2. Whole-book eager load — `routes.py:128-135`

```python
all_clients = (
    ClientCompany.query.options(
        selectinload(ClientCompany.employees),
        selectinload(ClientCompany.payroll_runs),
    ).order_by(ClientCompany.name).all()
)
```

Every company, with every employee and every payroll run, into memory on every
render. It feeds `platform_dashboard_analytics(all_clients, ...)` (`:233`), which
aggregates in Python.

This was a **deliberate** trade — the comment explains it replaced a 2N lazy-load
and holds the whole dashboard to three queries. That reasoning is sound at current
scale and the fix is not "undo it". The point is that the cost is O(all history),
not O(selected period), so it grows without bound while the page only ever renders
one month. If it needs fixing, push the aggregates into SQL grouped by company
rather than reverting to per-company lazy loads.

Same pattern feeds `portfolio_trend` (`:141-147`), which flattens every run of every
client.

### 3. Dead variable — `routes.py:175`

```python
period_run_ids = [run.id for run in current_runs]
```

Assigned, never read anywhere in the file. Free deletion.

## Do not "fix" by caching

The held-run count is documented as deliberately cache-free so a release shows on
the next render (`routes.py:193-202`). Don't introduce a dashboard-wide cache to
paper over query cost — it would silently break that guarantee.

## Verifying any change

`tests/test_operator_dashboard.py` and `tests/test_phase5_performance.py` cover this
page. Full suite baseline is **790 passed, 44 skipped, 0 failed** (~42 min). The
44 skips are deliberate: 43 raw-engine tests gated on a real client specimen that
is not in the repo, plus one permanent skip in `test_mvp.py`.

(This line previously read "1 failed, 782 passed", the one failure being a stale
SLA copy assertion. That was fixed in `28da5d3`; a baseline that expects a
failure is how a second, real failure hides.)
