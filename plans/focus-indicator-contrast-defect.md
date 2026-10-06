# Keyboard focus is invisible across the tenant portal — WCAG 1.4.11 failure

- **Status**: Tenant portal FIXED on `feature/sms-distribution` (SMS Phase 3,
  2026-10-02): the `portal.css` rule now uses `--brand` (5.67:1 on the page).
  Still open: `.upload-tab:focus-visible` in `styles.css` (operator shell), not
  yet re-measured. The `.portal-topbar` override was kept rather than deleted:
  it also covers the search input, which the portal-wide rule does not.
- **Severity**: fails a Level AA success criterion on every focusable control in
  the client portal except the shell chrome.
- **Where**: `app/static/portal.css`, the global focus rule; `app/static/styles.css`,
  `.upload-tab:focus-visible`
- **Trigger to act**: any accessibility review, procurement questionnaire, or
  VPAT; otherwise the next time anyone navigates the portal by keyboard
- **Found**: while re-deciding focus treatment for the light top bar during the
  client dashboard redesign. The redesign did not cause it and does not fix it.

## The defect

`app/static/portal.css` sets the focus indicator for the whole tenant portal:

```css
a:focus-visible,
.btn:focus-visible,
button:focus-visible,
summary:focus-visible {
    outline: 3px solid var(--brand-soft);
    outline-offset: 2px;
}
```

`--brand-soft` is `#d6f3f1` (`app/static/tokens.css`) — the palest tint in the
teal ramp, intended as a soft fill behind dark text. As an outline colour on the
portal's own surfaces it measures:

| Outline on | Ratio | Required |
|---|---|---|
| `--surface` `#ffffff` | **1.17:1** | 3:1 |
| `--surface-alt` `#f7fbfa` | **1.12:1** | 3:1 |
| `--shell` `#f2f7f5` | **1.08:1** | 3:1 |

**WCAG 2.1 SC 1.4.11 Non-text Contrast (Level AA)** requires a minimum contrast
ratio of 3:1 against adjacent colour for "visual information required to
identify user interface components and their states" — which explicitly
includes the focus indicator. At 1.17:1 the ring is not merely low-contrast, it
is not perceivable at all on white.

The practical consequence: a keyboard-only user tabbing through
`/company/employees`, `/company/runs`, `/company/expenses` or any client form
has **no visible indication of where focus is**. The indicator is being drawn.
Nobody can see it.

## Surfaces still affected

The client dashboard redesign added a shell-scoped override:

```css
.portal-nav a:focus-visible,
.utility-btn:focus-visible,
.nav-toggle:focus-visible,
.portal-logo:focus-visible,
.utility-pop a:focus-visible { outline-color: var(--brand); }
```

`--brand` `#0c6e62` measures 6.14:1 on the bar and 5.67:1 on the page, so the
six nav items and the utility cluster are compliant.

**That override is a local patch, not a fix.** It covers the chrome and nothing
else. Everything below still inherits `--brand-soft`:

- every link and button on Employees, Payroll, Statutory, Expenses, Audit and
  Branding, including all `.btn` and `.btn primary` controls
- the `ui.data_table` action links and `ui.pagination` page links
- the shared empty-state call to action (`macros/ui.html::empty`)
- every form submit in the client portal
- `.upload-tab:focus-visible` in `styles.css`, which is the **operator** shell's
  upload workflow chooser and uses the same token

Not affected, verified rather than assumed:

- `styles.css` global `:focus-visible` and `.toast-close` use `--brand` (passes)
- `dashboard.css` and `admin-dashboard.css` focus rules use `--brand` (passes)
- `styles.css` `.sidebar a:focus-visible` uses `--gold` (= `--accent` `#17c3b2`),
  which sits on the operator sidebar's deep teal `#0d4d4d` at **4.34:1** — this
  one is correct and must not be "unified" onto a light-surface value

## The fix, when it is taken

Change the token in the rule, not the token itself: `--brand-soft` has legitimate
uses as a fill and must keep its value. Point the portal's focus rule at
`--brand`, matching what `styles.css`, `dashboard.css` and `admin-dashboard.css`
already do, and delete the now-redundant shell override rather than leaving two
sources of truth.

Then check `.upload-tab` on its own backdrop before reusing the same value.

Worth doing at the same time: `app/static/tokens.css` carries a contrast
exemption register documenting the two accepted WCAG failures (card hairlines,
and `.donut-slice--accent`). A focus indicator is not an acceptable exemption
and should never be added to it — but the register is the right place to record
that this one was found and closed.

## Verification

There is no automated contrast gate in the suite today, so this needs measuring
rather than testing. The ratio is computable from the two hex values with the
WCAG relative-luminance formula; a contrast sweep harness was described in the
Dashboard Design Excellence Programme notes and can be rebuilt in a scratch
directory.

The manual check is faster and is the one that matters: load any client page,
press Tab repeatedly, and confirm you can see where you are.
