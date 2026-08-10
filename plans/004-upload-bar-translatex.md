# 004 — Animate the upload progress bar with transform, not margin-left

- **Status**: TODO
- **Commit**: 34b11fd
- **Severity**: HIGH
- **Category**: Performance / Easing & duration
- **Estimated scope**: 1 file (`app/static/styles.css`), ~12 lines changed

## Problem

The indeterminate bar on the staged upload loader sweeps by animating `margin-left`,
infinitely, in a loop — a layout-triggering property animated continuously at
exactly the moment the browser is busiest.

```css
/* app/static/styles.css:1175-1188 — current */
.upload-progress {
    width: min(320px, 80%);
    height: 8px;
    border-radius: 999px;
    background: var(--brand-soft);
    overflow: hidden;
}
.upload-progress-bar {
    height: 100%;
    width: 40%;
    border-radius: 999px;
    background: linear-gradient(90deg, var(--brand), var(--brand-dark));
    animation: cnIndeterminate 1.4s ease-in-out infinite;
}
```

```css
/* app/static/styles.css:1190-1194 — current */
@keyframes cnIndeterminate {
    0%   { margin-left: -40%; }
    50%  { margin-left: 60%; }
    100% { margin-left: 100%; }
}
```

Why this is the worst possible place for a layout animation: this loader is raised by
`payrollaStagedLoader()` (`app/static/app.js:280-299`) specifically while a payroll
workbook is being parsed — the app's heaviest synchronous operation. `margin-left`
forces layout + paint + composite on every frame of an infinite animation, competing
for the main thread precisely when the page has none to spare. The visible symptom is
a loader that stutters exactly when it is supposed to be reassuring the user.

Two secondary problems in the same block:

1. **Wrong easing for constant motion.** `ease-in-out` on a looping sweep decelerates
   into the 100% keyframe and then jumps back to −40% at 0%, producing a visible hitch
   once per cycle. Continuous/indeterminate motion should be `linear`.
2. **The reduced-motion fallback reads as "finished".** At
   `app/static/styles.css:1195-1197` the bar becomes a static, full-width, fully
   saturated bar — which looks like a completed 100% progress bar rather than an
   indeterminate busy state, and the user is being told the opposite (work in
   progress).

## Target

The sweep is expressed as `translateX` percentages of the bar's own width, which is
mathematically identical to the current motion but runs on the compositor. Easing
becomes `linear`. The reduced-motion state stays static but is de-emphasised so it
does not read as complete.

The percentage conversion (do not re-derive it — the bar is `width: 40%` of the
track, so 100% of its own width equals 40% of the track):

| Old (`margin-left`, % of track) | New (`translateX`, % of bar) |
| --- | --- |
| `-40%` | `-100%` |
| `60%` | `150%` |
| `100%` | `250%` |

```css
/* app/static/styles.css — target, replacing the .upload-progress-bar rule */
.upload-progress-bar {
    height: 100%;
    width: 40%;
    border-radius: 999px;
    background: linear-gradient(90deg, var(--brand), var(--brand-dark));
    animation: cnIndeterminate 1.4s linear infinite;
    will-change: transform;
}
```

```css
/* app/static/styles.css — target, replacing the @keyframes cnIndeterminate block */
@keyframes cnIndeterminate {
    0%   { transform: translateX(-100%); }
    50%  { transform: translateX(150%); }
    100% { transform: translateX(250%); }
}
```

```css
/* app/static/styles.css — target, replacing lines 1195-1197 */
@media (prefers-reduced-motion: reduce) {
    /* No sweep. Keep the bar static but de-emphasised, so it reads as an
       indeterminate busy state rather than a completed 100% bar. */
    .upload-progress-bar {
        animation: none;
        width: 100%;
        opacity: .55;
    }
}
```

## Repo conventions to follow

- `.upload-progress` is the `overflow: hidden` track (`app/static/styles.css:1175-1181`)
  and must stay unchanged — it is what clips the sweeping bar. Only the inner
  `.upload-progress-bar` moves.
- This section of `styles.css` (from line 1146) uses the compact
  `selector { prop: val; }` style with the section banner comment above it. Match the
  surrounding formatting.
- Colours come from `tokens.css` (`var(--brand)`, `var(--brand-dark)`,
  `var(--brand-soft)`). Do not inline hex values.
- Component-scoped `prefers-reduced-motion` blocks placed directly after the
  component they modify are the established pattern here — see
  `app/static/styles.css:1097-1100` (toasts) and `1141-1144` (confirm modal).
- **Exemplar of correct transform-only keyframes in this file**:
  `app/static/styles.css:913-920` (`@keyframes barGrow`, which uses `scaleX` with an
  explicit `transform-origin: left center` on `.sparkbar span` at line 506).

## Steps

1. In `app/static/styles.css`, in the `.upload-progress-bar` rule (lines 1182-1188),
   change the `animation` shorthand's timing function from `ease-in-out` to `linear`
   and add `will-change: transform;` as the final declaration. Leave `height`,
   `width`, `border-radius`, and `background` untouched.
2. Replace the `@keyframes cnIndeterminate { … }` block (lines 1190-1194) with the
   three `translateX` keyframes from the Target section. Use the exact percentages in
   the conversion table — do not recalculate them.
3. Replace the `@media (prefers-reduced-motion: reduce)` block at lines 1195-1197 with
   the target version, including the explanatory comment.
4. Confirm the property is gone: `grep -n "margin-left" app/static/styles.css` must
   return no results inside the `cnIndeterminate` keyframes.

## Boundaries

- Do NOT touch `.upload-progress` (the track, lines 1175-1181) — its
  `overflow: hidden` is what makes the effect work.
- Do NOT touch `.upload-overlay` (lines 1157-1172), including its
  `backdrop-filter: blur(2px)` — 2px is well under the 20px blur budget and is fine.
- Do NOT touch `payrollaStagedLoader()` in `app/static/app.js:280-299` or the
  `UPLOAD_STAGES` copy array at lines 274-279. This plan is CSS-only.
- Do NOT touch the global `@media (prefers-reduced-motion: reduce)` block at
  `app/static/styles.css:922-931`. It is a separate finding, out of scope here.
- Do NOT try to make the bar reflect real parse progress. The comment at
  `app/static/styles.css:1155-1156` and `app/static/app.js:269-273` document that this
  is deliberately perceived progress only ("no background worker on the free tier") —
  that is a settled product decision, not a defect.
- Do NOT add dependencies.
- If a step does not match the code you find (drift since commit 34b11fd), STOP and
  report instead of improvising.

## Verification

- **Mechanical**:
  - `grep -n -A4 "@keyframes cnIndeterminate" app/static/styles.css` → three keyframes,
    all using `transform: translateX(...)`, no `margin-left`.
  - `grep -n "cnIndeterminate 1.4s" app/static/styles.css` → shows `linear`, not
    `ease-in-out`.
  - `python -c "from app import create_app; create_app()"` succeeds.
- **Feel check**: run the app and reach the payroll upload screen (the form carrying
  `data-staged-loader`, wired at `app/static/app.js:305-310`). Submit a workbook so
  the overlay appears. If a real upload is inconvenient, trigger it from the console:
  `payrollaStagedLoader(document.querySelector('.upload-overlay'))`.
  - The bar must sweep left-to-right at a **constant speed** with no pause or
    deceleration before it wraps, and the wrap point must be invisible — watch three
    full cycles specifically looking for a hitch at the loop boundary. A hitch means
    step 1 (`linear`) did not take.
  - The bar must stay fully inside the rounded track and never overflow it.
  - The sweep must cover the same visual travel as before: entering fully off the left
    edge and exiting fully off the right.
  - In DevTools → Rendering, enable **"Paint flashing"**: the bar must show no green
    repaint flashes while sweeping. Green flashing means it is still animating a
    layout property.
  - In DevTools → Performance, record 3 seconds with the loader visible: no repeating
    "Layout" entries in the flame chart.
  - In DevTools → Rendering → "Emulate CSS prefers-reduced-motion: reduce", show the
    loader again: the bar is static, spans the full track, and is visibly translucent
    so it does not look like a finished 100% bar. The cycling stage copy
    ("Reading workbook…" → "Validating rows…") must still advance.
- **Done when**: Paint flashing shows no repaints on the sweeping bar, the loop has no
  visible hitch, and the reduced-motion state is static and translucent.
