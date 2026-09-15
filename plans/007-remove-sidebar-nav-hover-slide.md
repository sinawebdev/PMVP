# 007 — Remove the hover slide from the operator sidebar navigation

- **Status**: TODO
- **Commit**: 67f9c4b
- **Severity**: HIGH
- **Category**: Purpose & frequency
- **Estimated scope**: 1 file (`app/static/styles.css`), 2 declarations changed

## Problem

Every link in the operator shell's sidebar slides 4px to the right when the pointer
passes over it.

```css
/* app/static/styles.css:56-79 — current */
.sidebar .nav-link {
    align-items: center;
    border: 1px solid transparent;
    border-radius: 10px;
    color: #d1fae5;
    display: flex;
    font-size: 14px;
    gap: 10px;
    padding: 10px 12px;
    transition: transform 180ms ease, background 180ms ease, border-color 180ms ease;
}

.sidebar .nav-link i {
    color: var(--gold);
    font-size: 17px;
    width: 20px;
}

.sidebar .nav-link:hover {
    background: rgba(255, 255, 255, 0.1);
    border-color: rgba(255, 255, 255, 0.18);
    color: #fff;
    transform: translateX(4px);
}
```

Why it matters:

- **Frequency.** This is primary navigation, rendered on every page of the operator
  shell (`app/templates/base.html:50-53`). The pointer crosses it on the way to almost
  everything. Hover motion on the most-crossed element in the product is the textbook
  case for removal: after the first day it is no longer an effect, it is lag between
  the pointer and the thing it is over.
- **Purpose.** The slide communicates nothing the hover background and border do not
  already say. It fails the "why does this animate?" test.
- **Pointer and keyboard disagree.** Keyboard focus on the same link
  (`.sidebar a:focus-visible`, `app/static/styles.css:706-709`) shows an outline and does
  not move. Only pointer users get the slide, so the two ways of reaching the same link
  look different.
- **The two shells disagree.** The tenant portal's primary navigation changes background
  and colour on hover and does not move (exemplar below).
- **Ungated on touch.** At `max-width: 1000px` the sidebar becomes a slide-in drawer
  (`app/static/styles.css:873`). There, tapping a link fires hover, so the link jumps
  sideways at the moment the user commits to navigating.
- **It also hits Logout and the current page.** `.sidebar .nav-link.logout-link` and
  `.sidebar .nav-link[aria-current="page"]` both match `.sidebar .nav-link:hover`, so both
  inherit the slide. Removing it from the base hover rule fixes all of them at once.

## Target

```css
/* app/static/styles.css:56-78 — target */
.sidebar .nav-link {
    align-items: center;
    border: 1px solid transparent;
    border-radius: 10px;
    color: #d1fae5;
    display: flex;
    font-size: 14px;
    gap: 10px;
    padding: 10px 12px;
    transition: background 180ms ease, border-color 180ms ease;
}

.sidebar .nav-link i {
    color: var(--gold);
    font-size: 17px;
    width: 20px;
}

.sidebar .nav-link:hover {
    background: rgba(255, 255, 255, 0.1);
    border-color: rgba(255, 255, 255, 0.18);
    color: #fff;
}
```

Two changes only: `transform 180ms ease, ` is removed from the `transition` list, and
`transform: translateX(4px);` is removed from the hover rule. Keep `180ms ease` on
`background` and `border-color`: these are colour changes, where built-in `ease` is
correct.

## Repo conventions to follow

- Exemplar: the tenant portal's primary navigation, which already does exactly this.

```css
/* app/static/portal.css:106-119 — current, the exemplar */
.portal-nav a {
    align-items: center;
    border-bottom: 2px solid transparent;
    color: var(--ink-soft);
    display: flex;
    font-size: 15px;
    gap: 8px;
    padding: 0 12px;
    text-decoration: none;
    transition: background var(--ease), color var(--ease);
    white-space: nowrap;
}

.portal-nav a:hover { background: var(--surface-alt); color: var(--ink); }
```

- Do not convert the hand-typed `180ms ease` to `var(--ease)` in this plan. Token
  consolidation across `styles.css` is a separate, unplanned finding (see
  `plans/README.md`), and mixing it in would widen the diff past what this plan verifies.
- Do not add a comment explaining the removal. This file's comments explain what IS
  there; the reasoning lives in this plan.
- The file already follows the rule that persistent chrome must not animate: see the
  comment above `.logo-bounce` in `app/static/styles.css` ("the sidebar mark is
  persistent chrome and must never animate"). This plan applies the same rule to the
  links beneath that mark.

## Steps

1. In `app/static/styles.css`, in the `.sidebar .nav-link` rule, change line 65 from
   `    transition: transform 180ms ease, background 180ms ease, border-color 180ms ease;`
   to
   `    transition: background 180ms ease, border-color 180ms ease;`.
2. In `app/static/styles.css`, in the `.sidebar .nav-link:hover` rule at lines 74-79,
   delete the line `    transform: translateX(4px);`. Leave `background`,
   `border-color` and `color` exactly as they are.
3. Confirm no other sidebar rule transforms nav links:
   `grep -n "translateX(4px)" app/static/*.css` returns no matches.

## Boundaries

- Do NOT touch `.sidebar .nav-link.logout-link` or its `:hover` rules
  (`app/static/styles.css:82-97`). They inherit the fix and need no change.
- Do NOT touch `.sidebar .nav-link[aria-current="page"]` (`styles.css:711-719`).
- Do NOT touch `.sidebar a:focus-visible` (`styles.css:706-709`) or the global
  `:focus-visible` rule (`styles.css:700-704`).
- Do NOT touch the `min-height: 44px` touch-target rule (`styles.css:722-725`).
- Do NOT touch the mobile drawer rules inside `@media (max-width: 1000px)`
  (`styles.css:873` onward), including the `.sidebar` slide and the nav scrim.
- Do NOT add a `color` entry to the `transition` list. The text colour already changes
  instantly today; changing that is out of scope.
- Do NOT add `:active` to nav links. Plan 006 explicitly excluded `.sidebar .nav-link`
  from press feedback.
- Do NOT touch `app/static/portal.css`. It is the exemplar and is already correct.
- Do NOT change markup or JS. Do NOT add dependencies.
- The working tree at the time of writing had uncommitted changes to `styles.css`
  starting at line 748 (the nav scrim). Lines 56-79 were identical to commit `67f9c4b`.
  If lines 56-79 do not match the "current" excerpt above, STOP and report instead of
  improvising.

## Verification

- **Mechanical**:
  - `grep -n "translateX(4px)" app/static/*.css` → no matches.
  - `grep -n "transform 180ms ease, background 180ms ease" app/static/styles.css` → no
    matches.
  - `sed -n '56,78p' app/static/styles.css` matches the Target block exactly (the hover
    rule is now one line shorter, so it ends at 78).
  - `grep -c "{" app/static/styles.css` equals `grep -c "}" app/static/styles.css`.
  - `python -m pytest tests/test_ui_components.py tests/test_phase6_vocabulary.py -q`
    → all pass. These are the tests that read CSS; they take about two minutes. The
    full suite baseline is `818 passed, 44 skipped` and takes about an hour.
  - Do not boot the app against the repo's `.env` to check this: it points at
    production. The test suite configures its own app.
- **Feel check**: run the operator shell locally at a desktop width.
  - Sweep the pointer up and down the sidebar quickly. Links tint and untint; nothing
    moves sideways. The icons stay in one vertical column throughout the sweep.
  - Hover the current-page link and the Logout link. Neither moves.
  - Tab through the sidebar with the keyboard, then hover the same links with the
    pointer. Apart from outline versus background, the two now look alike: neither
    moves.
  - DevTools → Animations panel at 10%, hover a link: only `background-color` and
    `border-color` tracks should appear, no `transform` track.
  - Narrow the window below 1000px so the sidebar becomes a drawer. Open it and tap
    a link using DevTools touch emulation. The link must not jump sideways before the
    page navigates.
  - DevTools → Rendering → "Emulate CSS prefers-reduced-motion: reduce": hover still
    changes the background, instantly.
- **Done when**: no sidebar link transforms on hover in any state (default, current
  page, Logout), hover tint and border are unchanged, and pointer and keyboard
  treatment agree that the link stays still.
