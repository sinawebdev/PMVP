# 006 — Add press feedback to buttons in both shells

- **Status**: TODO
- **Commit**: 34b11fd
- **Severity**: MEDIUM (additive — missed opportunity, not a defect)
- **Category**: Physicality & origin / Interruptibility
- **Estimated scope**: 2 files (`app/static/styles.css`, `app/static/portal.css`), ~20 lines added

## Problem

Nothing in the product responds to being pressed. `grep -n ":active" app/static/*.css`
returns **zero results** across both stylesheets. Every button has a hover state and a
focus-visible state, but the moment of the click itself — the one interaction the user
is physically committing to — produces no acknowledgement.

Current operator-shell button, hover only:

```css
/* app/static/styles.css:607-614 — current */
.btn {
    border-radius: 10px;
    font-weight: 700;
}

.btn i {
    margin-right: 6px;
}
```

Current portal-shell button, hover only:

```css
/* app/static/portal.css:248-263 — current */
.btn {
    background: var(--surface);
    border: 1px solid var(--line-strong);
    border-radius: 10px;
    color: var(--ink);
    cursor: pointer;
    display: inline-block;
    font-family: inherit;
    font-size: 14px;
    font-weight: 700;
    padding: 8px 15px;
    text-decoration: none;
    transition: background var(--ease), border-color var(--ease), color var(--ease);
}

.btn:hover { border-color: var(--brand); color: var(--brand); }
```

Why it matters here specifically:

- **On touch there is no hover at all.** Tablet users get *no* visual acknowledgement
  between tapping "Approve payroll" and the server responding. Press feedback is the
  only affordance available to them.
- **The actions are consequential and often slow.** Buttons here submit payroll runs and
  parse workbooks. A pressed state confirms the tap registered and reduces double-submit
  attempts during the wait.
- **It is the highest feel-per-line change available in this codebase.** Two rules per
  shell, no markup changes, no JS.

## Target

A subtle inward scale on `:active`, with **asymmetric timing**: the press registers
almost immediately (60ms), and the release settles more gently (160ms). Scale stays
within the perceptible-but-not-cartoonish 0.95–0.98 band.

```css
/* app/static/styles.css — target, replacing lines 607-610 */
.btn {
    border-radius: 10px;
    font-weight: 700;
    /* Press feedback. Asymmetric on purpose: the press snaps (60ms), the release
       settles (160ms). Bootstrap's own .btn transition covers colour only. */
    transition: transform 160ms var(--ease-out);
}

.btn:active {
    transform: scale(0.97);
    transition-duration: 60ms;
}
```

```css
/* app/static/portal.css — target: extend the existing .btn transition and add
   an :active rule directly after the existing .btn:hover rule at line 263 */
.btn {
    /* …all existing declarations unchanged… */
    transition: background var(--ease), border-color var(--ease), color var(--ease),
                transform 160ms var(--ease-out);
}

.btn:hover { border-color: var(--brand); color: var(--brand); }

/* Press feedback. Asymmetric on purpose: the press snaps (60ms), the release
   settles (160ms). */
.btn:active {
    transform: scale(0.97);
    transition-duration: 60ms;
}
```

Two deliberate decisions worth stating so they are not "corrected" later:

- **No `@media (hover: hover)` gate.** That gate exists to stop *hover* motion firing
  on touch taps. `:active` is exactly the state that *should* fire on touch — gating it
  would remove the main benefit.
- **No component-scoped reduced-motion block.** The global rule at
  `app/static/styles.css:922-931` already zeroes transition durations, and
  `--ease`/`--ease-out` consumers in `portal.css` are covered by the token override at
  `app/static/tokens.css:104-106`. Under reduced motion the press becomes instant rather
  than animated, which is the correct outcome — the feedback survives, the movement does
  not.

## Repo conventions to follow

- `--ease-out: cubic-bezier(0.23, 1, 0.32, 1)` is added to `app/static/tokens.css` by
  **plan 001**. If that token is not present when you start, add it first inside
  `:root` under the `/* Motion — respected by both shells */` comment (currently
  `app/static/tokens.css:66-67`), exactly as:
  `--ease-out: cubic-bezier(0.23, 1, 0.32, 1);`
  Both stylesheets load after `tokens.css` (`app/templates/base.html:14-15` for the
  operator shell; the equivalent link in `app/templates/client/base.html` for the
  portal), so the token resolves in both.
- **The operator shell is Bootstrap 5.3.3** (`app/templates/base.html:12`) and
  `styles.css` is loaded *after* it (line 15), so a plain `.btn:active` selector wins on
  source order. **Do not add `!important`** and do not raise specificity artificially.
- Bootstrap already applies its own `transition` to `.btn` for colour/background/border/
  box-shadow. Declaring `transition: transform …` in `styles.css` replaces that shorthand
  for `.btn` — which is acceptable because Bootstrap's `.btn:hover` colour change then
  becomes instant, and the operator shell's own hover styling (e.g. `.upload-tab:hover`
  at `app/static/styles.css:1029`) is defined separately with its own transition. Verify
  this in the feel check below rather than assuming it.
- `portal.css` uses the `var(--ease)` token throughout and has no Bootstrap. Extend its
  existing `transition` list rather than replacing it.
- **Exemplar of the existing button styling pattern to sit alongside**:
  `app/static/portal.css:246-276` (the `/* --- Buttons --- */` section, with `:hover`,
  `.primary`, `.sm`, `.danger` variants and `:focus-visible` handled separately at
  line 382).

## Steps

1. If `--ease-out` is not already in `app/static/tokens.css` (added by plan 001), add it
   inside `:root` as described under "Repo conventions" above.
2. In `app/static/styles.css`, replace the `.btn` rule at lines 607-610 with the target
   version (adding the `transition` declaration and the comment), then add the new
   `.btn:active` rule immediately after it, before the existing `.btn i` rule at
   line 612.
3. In `app/static/portal.css`, append `, transform 160ms var(--ease-out)` to the existing
   `transition` declaration on line 260. Keep the existing three transition entries
   exactly as they are.
4. In `app/static/portal.css`, add the `.btn:active` rule immediately after the existing
   `.btn:hover` rule on line 263, inside the `/* --- Buttons --- */` section.
5. Verify the rules landed and nothing else grew an `:active`:
   `grep -n ":active" app/static/*.css` → exactly two matches, one per file.

## Boundaries

- Do NOT add `:active` to `.sidebar .nav-link`, `.client-tab`, `.client-subtab`,
  `.upload-tab`, `.portal-nav a`, or table rows. Navigation links are hit constantly and
  already have hover treatment; press feedback belongs on buttons that commit an action.
- Do NOT touch any `:focus-visible` rule (`app/static/styles.css:1030`,
  `app/static/portal.css:382-383`). Keyboard focus styling is correct and separate.
- Do NOT use `!important` anywhere in this change.
- Do NOT change scale values. `scale(0.97)` is the target; anything at or below 0.95
  reads as cartoonish on a finance product and anything above 0.98 is imperceptible.
- Do NOT add a `@media (hover: hover)` wrapper around the `:active` rule — see the
  Target section for why.
- Do NOT change markup, button classes, or any JS.
- Do NOT add dependencies.
- If a step does not match the code you find (drift since commit 34b11fd), STOP and
  report instead of improvising.

## Verification

- **Mechanical**:
  - `grep -n ":active" app/static/styles.css app/static/portal.css` → exactly two
    matches, one per file, both `.btn:active`.
  - `grep -n "transform 160ms" app/static/styles.css app/static/portal.css` → one match
    in `portal.css`, and **two** in `styles.css`. The second styles.css match is
    pre-existing: `.table tbody tr { transition: background 160ms ease, transform 160ms ease; }`
    at line 482, unrelated to this plan. Leave it alone — it is tracked as a separate
    finding. Only one match in each file should be *new*.
  - `grep -rn "!important" app/static/portal.css` → no new matches introduced by this
    change.
  - `python -c "from app import create_app; create_app()"` succeeds.
- **Feel check**: run the app. Test in **both** shells — the operator app (Bootstrap) and
  the tenant portal (no Bootstrap).
  - Press and hold a primary button (e.g. a payroll action button). It must visibly sink
    while held and spring back on release. Held down, it must **stay** at the pressed
    size — not bounce back on its own.
  - The sink must feel immediate and the return slightly softer. Press-and-release
    rapidly ten times: it must track every press without lag or queueing (this is what
    the asymmetric durations buy).
  - **Bootstrap regression check** (specific to step 2): hover a `.btn` in the operator
    app and confirm its colour/background change still looks correct. Because the
    `transition` shorthand is overridden, that colour change is now instant rather than
    150ms-eased. Confirm it reads as crisp, not broken. If it looks abrupt in a way you
    dislike, the fix is to *extend* the declaration in `styles.css` to
    `transition: transform 160ms var(--ease-out), color 150ms ease, background-color 150ms ease, border-color 150ms ease, box-shadow 150ms ease;`
    — not to remove the transform transition.
  - In DevTools → Animations panel, set playback to 10% and press a button: the pressed
    state should be a `transform` track only, with no layout-affecting property.
  - **On a real touch device or DevTools device emulation with touch**: tap a button and
    confirm the pressed state appears. This is the primary payoff — verify it, do not
    assume it.
  - In DevTools → Rendering → "Emulate CSS prefers-reduced-motion: reduce": pressing a
    button must still change its appearance, but instantly. Confirm the feedback has not
    disappeared entirely.
- **Done when**: buttons in both shells visibly and immediately respond to press on both
  mouse and touch, hover colours in the Bootstrap shell still look right, and no
  `!important` was added.
