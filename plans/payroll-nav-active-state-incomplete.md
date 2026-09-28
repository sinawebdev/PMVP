# Payroll nav item goes dark on several payroll routes

- **Status**: NOT URGENT — pre-existing, cosmetic, no route is unreachable
- **Where**: `app/templates/client/base.html`, the Payroll `<a>` in `.portal-nav`
- **Trigger to act**: a tenant analogue of `app/navigation.py`, or any
  complaint about the nav "losing its place" inside the payroll flow
- **Explicitly NOT part of**: the client dashboard redesign. The tuple was
  ported verbatim in Step 1 so the shell change stayed a pure structural move.

## The rule as it stands

```jinja
{% if ep in ('client.runs', 'client.run_detail', 'client.run_upload',
             'client.import_preview', 'client.distribute') %}aria-current="page"{% endif %}
```

Five endpoints, hand-listed.

## What it misses

The client blueprint owns considerably more payroll surface than those five.
At least these render with **no** primary nav item marked current:

| Endpoint | URL |
|---|---|
| `client.run_reports` | `/company/runs/<id>/reports` |
| `client.download_payroll` | `/company/runs/<id>/export` |
| `client.download_bank_listing` | `/company/runs/<id>/export/bank-listing` |
| `client.download_gra_paye` | `/company/runs/<id>/export/gra-paye` |
| `client.import_errors` | `/company/imports/<id>/errors` |
| `client.import_confirm` | `/company/imports/<id>/confirm` |
| `client.import_discard` | `/company/imports/<id>/discard` |
| `client.payslips_zip` | `/company/runs/<id>/payslips.zip` |
| `client.raw_upload` / `raw_confirm` / `raw_template` | `/company/runs/raw/*` |

The download and POST-only endpoints do not matter — they return a file or
redirect and never render the shell. `client.run_reports` and
`client.import_errors` do render it, and both are places a user lands and looks
around.

## Why it is more visible now than it was

In the sidebar, an unhighlighted item sat in a vertical list where the eye was
not tracking a single active marker. In a horizontal bar the active tab
underline is the only positional cue, so a page where nothing is underlined
reads as "you have left the section" rather than "the highlight is imprecise".

The redesign did not cause this and does not make it worse in code. It makes it
easier to notice.

## The fix, when it is taken

Do not extend the tuple. That is the exact maintenance burden
`app/navigation.py` was written to end, and its docstring says so:

> **Active state was string matching.** Each link carried its own
> `path.startswith('/payroll')` test in base.html, so adding a route meant
> editing the template to make the highlight work, and a URL rename silently
> broke it.

The operator side resolves the active item from the request's own **blueprint**.
That does not transfer directly: the tenant portal is one blueprint (`client`)
plus `main.company_dashboard` and `notifications`, so blueprint ownership would
collapse five of the six items into one.

The tenant analogue therefore has to own **endpoint prefixes** rather than
blueprints — `client.run*` and `client.import*` for Payroll, `client.employee*`
for Employees, `client.expense*` for Expenses. Declare it once as data beside
`NAV` in `app/navigation.py`, resolve it the way `active_nav_key` does, and the
template stops carrying any test at all.

Worth doing at the same time: `tests/test_navigation.py` covers only the
operator sidebar (it scrapes `<aside class="sidebar">` from `/dashboard`). A
tenant nav table declared as data is unit-testable the same way the operator one
already is, which is the point at which this stops being able to regress.
