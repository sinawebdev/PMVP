# 008 — Remove the hover scale from operator table rows

- **Status**: TODO
- **Commit**: 67f9c4b
- **Severity**: HIGH
- **Category**: Purpose & frequency / Performance
- **Estimated scope**: 1 file (`app/static/styles.css`), 2 declarations removed

## Problem

Every row of every Bootstrap `.table` in the operator shell scales up when the
pointer passes over it.

```css
/* app/static/styles.css:321-328 — current */
.table tbody tr {
    transition: background 160ms ease, transform 160ms ease;
}

.table tbody tr:hover {
    background: rgba(204, 251, 241, 0.36);
    transform: scale(1.002);
}
```

Why it matters:

- **Frequency.** Tables are the operator shell's main working surface. `.table`
  appears in 15 templates, and a reviewer's pointer crosses dozens of rows a minute
  while scanning. Hover motion hit this often should be removed, not tuned.
- **Function.** A row is data the user is in the middle of reading: names, amounts,
  statuses. Data being read should not move for style.
- **It buys nothing.** `scale(1.002)` grows a 1000px row by 2px. That is below the
  threshold anyone perceives as an effect, so the row pays the cost of a transform
  (a new compositing layer per hovered row, and text being re-rasterised mid-transition)
  for no visible benefit. The background tint already carries the hover state.
- **The two shells disagree.** The tenant portal styles the identical hover with the
  same tint and no transform:

```css
/* app/static/portal.css:402-403 — current, the exemplar */
tbody tr { transition: background 160ms ease; }
tbody tr:hover { background: rgba(204, 251, 241, 0.36); }
```

- **Ungated on touch.** The transform is not inside `@media (hover: hover) and
  (pointer: fine)`, so on a tablet a tap leaves the row stuck scaled until the next
  tap elsewhere. Removing the transform removes this problem entirely; no gate is
  needed afterwards, because a background tint sticking after a tap is harmless.

## Target

```css
/* app/static/styles.css:321-328 — target */
.table tbody tr {
    transition: background 160ms ease;
}

.table tbody tr:hover {
    background: rgba(204, 251, 241, 0.36);
}
```

This is byte-for-byte the portal's behaviour, scoped to `.table` as the operator
shell already is. Keep `160ms ease` on the background: a colour change is exactly
where the built-in `ease` is correct, and the value matches the portal.

## Repo conventions to follow

- Exemplar: `app/static/portal.css:402-403`, quoted above. Match it.
- Do not add a comment explaining the removal inside the rule. This file's comments
  explain what IS there; a removed transform needs no tombstone. The reasoning lives
  in this plan and in `plans/README.md`.
- The operator shell loads Bootstrap 5.3.3 before `styles.css`
  (`app/templates/base.html:12-15`). Bootstrap does not transform table rows, so
  removing the declaration cannot expose a Bootstrap transform underneath.

## Steps

1. In `app/static/styles.css`, in the `.table tbody tr` rule at line 322, change
   `transition: background 160ms ease, transform 160ms ease;` to
   `transition: background 160ms ease;`.
2. In `app/static/styles.css`, in the `.table tbody tr:hover` rule at lines 325-328,
   delete the line `transform: scale(1.002);`. Leave the `background` declaration
   exactly as it is.
3. Confirm nothing else transforms these rows:
   `grep -n "scale(1.002)" app/static/*.css` returns no matches.

## Boundaries

- Do NOT touch `.excel-grid tbody tr:hover td` (`app/static/styles.css:524`). It is a
  different component and does not transform.
- Do NOT touch `.dt tbody tr:hover` (`app/static/components.css:170`) or
  `.dt tbody tr:hover .dt-actions` (`components.css:199`). They already do not transform.
- Do NOT touch `app/static/portal.css`. It is the exemplar and is already correct.
- Do NOT change the hover background colour, its duration, or its easing.
- Do NOT add a `@media (hover: hover)` wrapper. With the transform gone there is no
  hover motion left to gate.
- Do NOT add an `:active` state to rows. Plan 006 explicitly excluded table rows from
  press feedback.
- Do NOT change markup or JS. Do NOT add dependencies.
- The working tree at the time of writing had uncommitted changes to `styles.css`
  starting at line 748 (the nav scrim). Lines 321-328 were identical to commit
  `67f9c4b`. If lines 321-328 do not match the "current" excerpt above, STOP and
  report instead of improvising.

## Verification

- **Mechanical**:
  - `grep -n "scale(1.002)" app/static/*.css` → no matches.
  - `grep -n "transform 160ms" app/static/styles.css` → exactly one match, the `.btn`
    press-feedback rule from plan 006 (`transition: transform 160ms var(--ease-out);`).
    Before this plan there were two.
  - `grep -c "{" app/static/styles.css` equals `grep -c "}" app/static/styles.css`.
  - `python -m pytest tests/test_ui_components.py tests/test_phase6_vocabulary.py -q`
    → all pass. These are the tests that read CSS; they take about two minutes. The
    full suite baseline is `818 passed, 44 skipped` and takes about an hour.
  - Do not boot the app against the repo's `.env` to check this: it points at
    production. The test suite configures its own app.
- **Feel check**: run the operator shell locally and open a page with a long table,
  such as the payroll runs list or an employee roster.
  - Sweep the pointer down the table quickly. Rows tint and untint; nothing grows,
    shifts, or softens. Text stays crisp throughout the sweep.
  - Hover one row and hold still. Compare it with the same kind of table in the tenant
    portal. The two should now be indistinguishable.
  - DevTools → Animations panel at 10%, hover a row: only a `background-color` track
    should appear, no `transform` track.
  - DevTools → Rendering → "Layer borders": hovering a row should no longer promote it
    to its own layer.
  - DevTools device emulation with touch: tap a row, then tap elsewhere. The row must
    not stay enlarged. (A lingering tint after the tap is expected and fine.)
- **Done when**: no operator table row transforms on hover, the hover tint is unchanged,
  and the operator and portal tables behave identically.
