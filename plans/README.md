# Animation improvement plans

Produced by the `improve-animations` skill against commit `34b11fd` on branch
`feature/phase-2-processing-experience`.

Each plan is **self-contained**: exact file paths, current code verbatim, exact target
values, and a feel-check. An executor needs no context beyond the plan file and the
repo. No plan modifies anything outside the files it names.

## Plans

| # | Title | Severity | Files | Status |
|---|-------|----------|-------|--------|
| [001](001-cut-page-entrance-animation.md) | Cut the 520ms whole-page entrance animation | HIGH | `styles.css`, `tokens.css`, `base.html` | SHIPPED |
| [002](002-stop-infinite-logo-bounce.md) | Stop the infinite logo bounce | HIGH | `styles.css`, `base.html` | SHIPPED |
| [003](003-scroll-progress-scalex.md) | Drive the landing scroll progress bar with transform, not width | HIGH | `landing.html` | SHIPPED |
| [004](004-upload-bar-translatex.md) | Animate the upload progress bar with transform, not margin-left | HIGH | `styles.css` | SHIPPED |
| [005](005-remove-arrow-key-scroll-hijack.md) | Remove the arrow-key scroll hijack on the landing page | HIGH | `landing.html` | SHIPPED |
| [006](006-add-button-press-feedback.md) | Add press feedback to buttons in both shells | MEDIUM | `styles.css`, `portal.css` | SHIPPED |

## Execution status

All six were applied on branch **`fix/animation-audit`**, in the worktree at
`../pmvp-v1-animations`, on top of `34b11fd`, and committed as `352c44f`.

**Status corrected 2026-08-08.** This section previously said the changes were
uncommitted working-tree modifications that had never been merged. That was true
when written and has not been true for some time: `352c44f` is an ancestor of
both `origin/main` and the current feature branch, and the worktree is clean.
The six fixes are shipped. What remains open is only the feel-checks below —
which is the part that needs a browser, and the reason this file is worth
keeping.

Verified after execution: no dead references remain (`page-enter`, `slideFade`,
`cnPulse`, `currentIndex`, the `keydown` handler); every boundary held
(`.login-card`/`riseFade`, `.topbar-visual img`/`floatIn`, `.brand-mark`, the global
reduced-motion block, the aurora animations, the dot-nav `scrollIntoView`); and the
app boots from the worktree with `/` and `/login` both rendering 200.

**Two corrections were made to these plans after execution**, both caught during
review:

- Plan 003's JS line citations were 3 lines high (the `// scroll progress` block is at
  674-681, not 677-685). Corrected. Plan 005's numbering for the same file was already
  correct.
- Plan 006's verification expected one `transform 160ms` match per file; `styles.css`
  has two, because `.table tbody tr` at line 482 already used that duration. Corrected
  to say so explicitly.

### Open items on the applied branch

- **`.btn` transition override (plan 006).** `styles.css:611` replaces Bootstrap's
  four-property colour transition on `.btn`, so hover colour changes are now instant.
  Anticipated and documented in the plan; needs a human eye in the browser to confirm
  it reads as crisp rather than abrupt. Plan 006's feel-check gives the exact
  fallback declaration if it does not.
- **`scaleX` is clamped at 1 but not at 0** (`landing.html`, plan 003).
  `Math.min(scrollTop/progMax, 1)` has no lower bound, so iOS Safari overscroll at the
  top can yield a negative `scaleX`. With `transform-origin: 0 50%` the bar renders
  off-screen left, so it is visually harmless — but `Math.max(0, …)` would be correct.
  The plan specified only `Math.min`, so this is a gap in the plan, not the execution.
- **`--ease-in-out` is currently unused.** Added by plan 001 as part of the token pair;
  nothing consumes it until the easing-consolidation finding is planned.

## Recommended execution order

**001 → 002 → 006 → 004 → 003 → 005**

Rationale: 001 first because it introduces the `--ease-out` token that 002 and 006
consume. Then 002 and 006 to finish the operator shell in one pass. 004 is independent
CSS. 003 and 005 both touch `landing.html` and are best done last, together, since they
share the same `<script>` IIFE.

## Dependencies

- **001 introduces `--ease-out: cubic-bezier(0.23, 1, 0.32, 1)` in `app/static/tokens.css`.**
  Plans **002** and **006** consume it. Both include a step-0 fallback that adds the
  token if absent, so they are safe to run out of order — but running 001 first avoids
  the duplication.
- **003 and 005 both edit the single IIFE at `app/templates/landing.html:657-738`.**
  Run them sequentially, not in parallel, or they will conflict. 003 edits lines 677-685;
  005 deletes lines 725-737. Doing 003 first keeps 005's line numbers accurate.
- **No other plan pairs overlap.** 001/002/004/006 touch disjoint regions of
  `styles.css` and can be executed independently.
- `landing.html` loads **neither** `tokens.css` nor `styles.css` — it is fully
  self-contained. Plans 003 and 005 must not reference any CSS custom property from the
  token file.

## Files never to be touched by these plans

- `app/static/styles.css:627-628` — `.login-card { animation: riseFade 540ms ease both; }`.
  A once-per-session entrance at 540ms is a deliberate, correct choice.
- `app/static/app.js:269-273` and `280-299` — the staged upload loader is documented as
  perceived progress only ("no background worker on the free tier"). That is a settled
  product decision, not a defect.
- `app/static/tokens.css:83-97` — `.brand-mark` is documented as "One definition, one
  look" across both shells.

## Findings deliberately not planned

From the same audit, verified but not selected for planning in this batch:

- Hero CTA on the landing page is `opacity: 0` until 1.5s and still fading at 2.5s
  (`landing.html:120`, chain at `:112`, `:114`, `:129`, `:133`).
- Three `transition: all` on the landing page (`landing.html:77`, `:163`, `:214`);
  `:163` also competes with `lp-popNode` on the same element.
- Reduced motion is a `*` + `!important` nuke (`styles.css:922-931`), implemented twice
  alongside the token override at `tokens.css:104-106` and three per-component blocks.
- `transform: scale(1.002)` on table-row hover (`styles.css:482-489`) — below the
  perception threshold, and the portal shell disagrees (`portal.css:241-242`).
- Skip link animates `top`, a layout property, on keyboard focus (`styles.css:761`,
  `portal.css:375`).
- `scroll-behavior: smooth` is never reset for reduced motion on the landing page
  (`landing.html:27` vs the `.lp-root *`-scoped block at `:352-358`).
- Nine hand-typed durations in `styles.css` while `--ease` (`tokens.css:67`) goes unused
  there; the token also welds duration to timing-function.
- Four near-identical hand-typed cubic-beziers on the landing page (`landing.html:97`,
  `:106`, `:155`, `:165`).
- Every transition in both stylesheets uses the built-in `ease`, including entrances.
- Three 42–60vw aurora gradient layers on infinite 22–28s transform loops
  (`landing.html:42-47`) — flagged to **measure on a low-end device**, not to fix blind.
- HTMX swaps teleport: 6 × `hx-swap="outerHTML"`, no `hx-indicator` anywhere.
- Toast stacking is unexplained — existing toasts jump to their new position instantly
  (`app.js:120`, `styles.css:1043-1053`).
- The `.client-tabs` `<details>` caret rotates (`styles.css:863-871`) but the disclosure
  content pops open with no transition.
