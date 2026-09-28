# Payrolla Client Portal — UI Architecture

Status: draft for build
Surface: client-facing company portal only (`pmvp-v1.onrender.com/compan...`)
Out of scope: operator/admin portal, marketing site

---

## 1. What this redesign is and is not

**Is:** a structural rework of the company portal dashboard and its shell — navigation, page grid, card hierarchy — using a banking-dashboard layout grammar as the reference.

**Is not:** a feature change. No module gains or loses capability. Every element in the new layout maps to something that already exists in the app.

**Trigger:** self-initiated. No client, partner, or user complaint prompted this. That matters for scope discipline — there is no external deadline, so correctness beats speed, and anything that can't be justified structurally gets cut rather than shipped as "while we're in here."

---

## 2. Constraints inherited from the UX Product Constitution

These are already-closed decisions. The new layout must not violate them.

| Principle | Consequence for this layout |
|---|---|
| One page, one job | The dashboard's job is oversight. Not a launcher, not a work surface. |
| A dashboard may link to work; it must never perform it | Buttons navigate. No inline payroll edits, no inline approvals. `Run payroll` is a link into the run flow, not a submit. |
| Quiet by default, loud only when a decision is required | Exactly one visually loud element per screen state. When nothing needs a decision, nothing is loud. |
| Oversight / Work / Record planes | Dashboard = Oversight. Payroll run pages = Work. Audit + payslip archive = Record. |
| — | *(An earlier suppression rule for comparative trend chips has been reversed. Chips stay exactly as currently implemented. See below.)* |

**Trend chips — decided, closed.** Comparative trend chips (`-90.6% vs June 2026`, `-100.0% vs last month`) stay exactly as they are today. The earlier suppression decision is reversed and is not to be reopened.

Two consequences to build around rather than fix:

1. The risk engine consumes the same comparatives, which is why "Workers decreased 80%" renders as a compliance warning. That warning will now surface in the Action Required card (§6), which is the loudest element on the page. Accepted.
2. `Workforce 198` and `33 workers · GH₵85,008.90 net` will continue to appear on the same screen with different answers for the same period. The subtext explains the difference (active headcount vs paid in last run). Keep that subtext — it is doing real work.

---

## 3. Navigation

### The problem with going horizontal

Current sidebar carries 8 destinations plus utilities. A horizontal bar comfortably seats 5–6 at 1280px before it needs an overflow menu, and an overflow menu on a top-level nav is a worse outcome than the sidebar we started with.

The Lovable prototype sidesteps this by inventing a 5-item nav (`Dashboard, Employees, Payroll, Payslips, Reports`) that doesn't match the app. Payslips and Reports are not top-level destinations; Statutory, Expenses, Audit and Branding were silently dropped. That nav is not validated — it's untested.

### Resolution: re-tier, don't truncate

Four of the eight items are not peers of the other four.

| Item | Frequency of use | Belongs in |
|---|---|---|
| Dashboard | Every session | Primary nav |
| Employees | Weekly+ | Primary nav |
| Payroll | Weekly+ | Primary nav |
| Statutory | Monthly, cyclical | Primary nav |
| Expenses | Monthly | Primary nav |
| Audit | Rare, investigative | Primary nav |
| Branding | Configured once, then never | Utility menu (gear) |
| Notifications | Ambient | Utility cluster (bell) |
| Log out | Session control | Account menu |

**Primary nav (6):** Dashboard · Employees · Payroll · Statutory · Expenses · Audit
**Right utility cluster:** notifications (bell, badge count) · settings (gear → Branding, company profile) · account menu (avatar → profile, log out)

Six items fits without overflow at 1280px with Satoshi at 15px/500 and 28px gaps. It degrades to a hamburger below 900px.

### Rejected alternative, recorded

Collapsing Statutory + Audit into a single "Compliance" parent to reach 5 items. Rejected: it adds a nesting layer, and a nested destination violates *one page, one job* by making the parent a menu rather than a page. Six flat items beats five with a submenu.

### Company identity

The sidebar currently carries company identity (avatar `MS`, "MSC Limited", "Company portal · MSC Administrator"). Horizontal nav has no room for a three-line block. It moves to the page header, which already renders the company name — merge them so identity appears once, not twice.

---

## 4. Page grid

Container: max-width 1280px, centred, on a light neutral page background. Inner content is a 12-column grid, 24px gutter.

```
┌──────────────────────────────────────────────────────────────┐
│  logo    Dashboard Employees Payroll Statutory Expenses Audit│  shell
│                                        🔔  ⚙  [AN] Ama ▾     │
├──────────────────────────────────────────────────────────────┤
│  MSC Limited                                                 │  header
│  Reporting on July 2026 · pay date 28 Jul     Next run 28 Aug│
├────────────────────────────────────┬─────────────────────────┤
│  ROW 1 — cols 1-8                  │  cols 9-12              │
│  PAYROLL HEALTH (hero)             │  ACTION REQUIRED        │
│  period · status · workers · net   │  one decision, or       │
│  [Run payroll] [Open run]          │  quiet empty state      │
├────────┬────────┬────────┬─────────┴─────────────────────────┤
│ ROW 2 — four equal cards, cols 1-3 / 4-6 / 7-9 / 10-12       │
│ Payroll│Workforc│Operatin│ Compliance                        │
│ cost   │e       │g exp.  │                                   │
├────────────────────────────────────┬─────────────────────────┤
│  ROW 3 — cols 1-8                  │  cols 9-12              │
│  PAYROLL RUN HISTORY               │  EXPENSE BREAKDOWN      │
│  last 6 runs, table                │  donut + legend         │
└────────────────────────────────────┴─────────────────────────┘
```

Row heights are content-driven, not fixed. Rows 1 and 3 use `align-items: stretch` so the pair reads as one band.

---

## 5. Slot mapping — current → new

| New slot | Source in current UI | Change |
|---|---|---|
| Row 1 left — Payroll health | `Payroll health` card, currently below fold | Promoted to hero. Absorbs the `Run payroll` button currently floating in the page header. |
| Row 1 right — Action required | `Risk & compliance watch` | Same content, new treatment (see §6). |
| Row 2 — 4 KPI cards | Payroll cost / Workforce / Operating expenses / Compliance | Position unchanged. Trend chips retained as-is. |
| Row 3 left — Payroll run history | **New composition.** Data exists in Payroll module; not currently surfaced on dashboard. | Last 6 runs. |
| Row 3 right — Expense breakdown | Expenses module, condensed | Donut + category legend + period selector. |
| — | `Quick actions` block (6 tiles) | **Deleted.** See below. |

### Why Quick actions is deleted

Five of six tiles duplicate primary nav: Manage employees → Employees, View reports → Statutory, View expenses → Expenses, Review risks → Audit, Send payslips → Payroll. Rendering navigation twice, in the highest-value position on the page, is what makes the current dashboard read as a launcher rather than an oversight surface.

The sixth, `Download payroll summary`, is not navigation — it's an action scoped to a specific run. It moves to the payroll run detail page, where the run it applies to is unambiguous.

### Row 3 left — payroll run history

Chosen over an audit activity feed. Reasoning: it's consistent with the hero directly above it (both concern runs), the data already exists in the Payroll module, and it keeps the dashboard on the Oversight plane. An audit feed is the more informative option and remains a candidate for a later iteration, but it pulls Record-plane content onto an Oversight page and needs its own decision.

Columns: Period · Run date · Workers · Net · Status · (row → run detail)
Rows: 6. Footer link "View all runs →" to the Payroll module.
Status values reuse existing app vocabulary exactly — `Processed`, `Held`, `Draft`, `Approved`. No new labels.

---

## 6. The Action Required card

This is the one slot where copying the reference directly would be wrong.

In the banking reference, the accent-filled card is promotional — calm, decorative, low stakes. In Payrolla it holds risk and compliance state. A filled brand-teal card reads as *reassurance*. Rendering a compliance warning inside a reassuring container is a real miscommunication, and it contradicts *loud only when a decision is required*.

**Three states, three treatments:**

| State | Condition | Treatment |
|---|---|---|
| Clear | No held runs, no flagged variances, no overdue filings | Quiet. Light surface, no fill, muted text: "Nothing needs your attention. Next run 28 Aug." This is the default and should be common. |
| Attention | Something needs a decision but nothing is overdue | Filled teal. Title, one-sentence explanation, one primary button. |
| Urgent | Overdue filing, held run blocking pay date, failed distribution | Amber/red surface, same structure. Never more than one urgent card. |

**Hard rule:** this card holds at most **one** decision. If three things need attention, it names the most urgent and links to the list. A card that lists three problems is a work surface wearing a card's clothes.

The `Compliance 85% · 1 area needs attention` KPI in Row 2 and this card currently report the same signal at two levels of detail. Keep the KPI as a standing score; this card only appears in Attention/Urgent state when there is a *specific actionable item*. They stop overlapping because one is a measure and one is a prompt.

---

## 7. Component inventory

Build order matters — later components depend on earlier ones.

```
shell/
  TopNav              nav items, active state, responsive collapse
  UtilityCluster      NotificationBell, SettingsMenu, AccountMenu
  PageHeader          company identity, reporting period, next pay date

primitives/
  Card                surface, padding scale, optional accent fill
  Money               GHS formatting, decimal de-emphasis (GH₵103,623.30)
  StatusPill          Processed / Held / Draft / Approved
  DataTable           header, rows, empty state, row-click
  Donut               segments, centre total, legend
  EmptyState          used by every data component

dashboard/
  PayrollHealthCard   row 1 left
  ActionRequiredCard  row 1 right — 3 states
  KpiCard             row 2, ×4
  RunHistoryTable     row 3 left — composes DataTable
  ExpenseBreakdown    row 3 right — composes Donut
```

`Money` exists as its own primitive because the reference's decimal treatment (large integer, smaller decimals) is a recurring detail and should not be re-implemented per card.

---

## 8. Data each slot needs

Write these before building the components. If a field isn't available from the current API, that's a backend task surfaced now rather than at integration.

```
PayrollHealth     period, status, workerCount, netTotal, grossTotal,
                  payDate, runId
ActionRequired    state: 'clear'|'attention'|'urgent',
                  title, body, ctaLabel, ctaHref, nextPayDate
KpiCard           label, value, unit, subtext,
                  trend: { direction, percent, comparisonLabel } | null
                  (null when no comparison exists at all; the existing
                   rendering rules for present-but-odd comparisons
                   carry over unchanged)
RunHistory        rows[]: { runId, period, runDate, workerCount,
                            net, status }
ExpenseBreakdown  total, period, segments[]: { label, amount, share }
```

Note the KPI type deliberately has no `trend` field. Making it structurally impossible to pass is cheaper than remembering not to.

---

## 9. Design tokens

Only the axes this redesign actually decides. Existing brand values carry over unchanged.

- **Typeface:** Satoshi throughout. Single family — no display/body split.
- **Type scale:** page title 28/600 · card title 17/600 · hero value 32/600 · KPI value 26/600 · body 15/400 · meta 13/400.
- **Colour:** existing Payrolla teal as the only accent. Amber and red reserved exclusively for the Urgent state — they must not appear as decoration anywhere else, or the Urgent state loses its meaning.
- **Radius:** 12px on cards, 8px on inner elements and buttons. Two values, not one — the difference encodes containment.
- **Elevation:** one shadow value, applied only to cards. Nested elements use border, not shadow.
- **Spacing:** 4px base. Card padding 24px, grid gutter 24px, row gap 20px.

---

## 10. Open decisions

1. **Row 3 left, later iteration** — does the audit activity feed eventually replace or sit alongside run history?
2. **Mobile** — dashboard on mobile is currently unspecified. Desktop-first is fine for build, but the row 1 / row 3 pairings need a defined stacking order before ship.

---

## 11. Build sequence

Each step is verifiable before the next begins.

1. **Shell** — TopNav + UtilityCluster + PageHeader, real 6-item IA, responsive collapse. No dashboard content.
2. **Primitives** — Card, Money, StatusPill, EmptyState. Rendered on a scratch route.
3. **Row 1** — PayrollHealthCard + ActionRequiredCard, all three action states rendered side by side for review.
4. **Row 2** — KpiCard ×4, trend chips ported unchanged.
5. **Row 3** — DataTable + RunHistoryTable, then Donut + ExpenseBreakdown.
6. **Assembly** — full grid, empty states, responsive pass.

Verify each step with `get_diff` against the returned `message_id`, not the agent's summary.
