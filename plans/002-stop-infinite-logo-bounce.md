# 002 — Stop the infinite logo bounce

- **Status**: TODO
- **Commit**: 34b11fd
- **Severity**: HIGH
- **Category**: Purpose & frequency / Cohesion
- **Estimated scope**: 4 files, ~12 lines changed

## Problem

The Payrolla brand mark runs a looping bounce **forever**, on every screen, for the
entire time the app is open:

```css
/* app/static/styles.css:49-51 — current */
.logo-bounce {
    animation: cnPulse 2.4s ease-in-out infinite;
}
```

```css
/* app/static/styles.css:873-880 — current keyframes */
@keyframes cnPulse {
    0%, 100% {
        transform: translateY(0) scale(1);
    }
    45% {
        transform: translateY(-6px) scale(1.04);
    }
}
```

It is applied in three places, one of which is the persistent app shell:

```jinja
{# app/templates/base.html:26 — current (sidebar, present on EVERY operator page) #}
<span class="brand-mark logo-bounce"><img src="{{ url_for('static', filename='img/payrolla-icon.svg') }}" alt="{{ app_brand_name }}"></span>
```

```jinja
{# app/templates/login.html:80 — current #}
<span class="brand-mark logo-bounce"><img src="{{ url_for('static', filename='img/payrolla-icon.svg') }}" alt="{{ app_brand_name }}"></span>
```

```jinja
{# app/templates/forgot_password.html:7 — current #}
<span class="brand-mark logo-bounce">{{ app_brand_mark }}</span>
```

Why this matters:

- **It never stops.** Perpetual motion in peripheral vision is the single most
  distracting thing a UI can do. A payroll operator reading a table of statutory
  deductions has a logo hopping in the corner of their eye the entire time.
- **It has no purpose.** It communicates no state, gives no feedback, explains no
  transition. It is decoration on an element seen continuously.
- **It is the wrong personality.** This is a finance product handling PAYE and SSNIT
  calculations; the rest of the app's motion is a crisp 150–220ms. A bouncing,
  scaling logo belongs to a different product.
- **It costs battery.** An infinite compositor animation prevents the browser from
  idling, on every tab, indefinitely.

The login and password-reset screens are seen rarely, so an entrance there is
defensible — but an *infinite* loop is not a delight moment, it is a distraction
that simply happens less often.

## Target

The class is removed from the persistent app shell entirely. On the two rare
anonymous screens it becomes a **one-shot entrance**: a 420ms fade-and-settle on a
strong ease-out curve, starting at `scale(0.92)` (never `scale(0)` — nothing in the
real world appears from nothing). The looping `cnPulse` keyframe is deleted.

```css
/* app/static/styles.css — target, replacing lines 49-51 */
/* One-shot entrance for the brand mark on the anonymous screens (login,
   forgot password). Deliberately NOT used in the app shell: the sidebar mark is
   persistent chrome and must never animate. */
.logo-bounce {
    animation: logoIn 420ms var(--ease-out) both;
}
```

```css
/* app/static/styles.css — target, replacing the @keyframes cnPulse block */
@keyframes logoIn {
    from {
        opacity: 0;
        transform: scale(0.92);
    }
    to {
        opacity: 1;
        transform: none;
    }
}
```

```jinja
{# app/templates/base.html:26 — target (class removed) #}
<span class="brand-mark"><img src="{{ url_for('static', filename='img/payrolla-icon.svg') }}" alt="{{ app_brand_name }}"></span>
```

`login.html:80` and `forgot_password.html:7` keep the `logo-bounce` class unchanged —
only the CSS behind it changes.

## Repo conventions to follow

- `--ease-out: cubic-bezier(0.23, 1, 0.32, 1)` is added to `app/static/tokens.css`
  by **plan 001**. If that token is not present in `tokens.css` when you start,
  add it first inside `:root` under the `/* Motion — respected by both shells */`
  comment (currently `app/static/tokens.css:66-67`), exactly as:
  `--ease-out: cubic-bezier(0.23, 1, 0.32, 1);`
- Keyframes in `styles.css` live in one group at lines 873-920. Put `logoIn` where
  `cnPulse` was, keeping that grouping intact.
- `.brand-mark` itself is defined in `app/static/tokens.css:83-97` and is documented
  there as "One definition, one look: sidebar, portal header, public payslip, error
  pages." Do not touch it — this plan changes only the animation wrapper class.
- **Exemplar of a correct one-shot entrance on a rare screen**:
  `app/static/styles.css:627-628` (`.login-card { animation: riseFade 540ms ease both; }`).

## Steps

1. If `--ease-out` is not already in `app/static/tokens.css` (added by plan 001), add
   it inside `:root` as described under "Repo conventions" above.
2. In `app/static/styles.css`, replace the `.logo-bounce` rule (lines 49-51) with the
   target version, including the explanatory comment.
3. In `app/static/styles.css`, replace the `@keyframes cnPulse { … }` block
   (lines 873-880) with the `@keyframes logoIn { … }` block from the Target section.
4. In `app/templates/base.html:26`, remove the `logo-bounce` class from the `<span>`,
   leaving `class="brand-mark"`. Leave the `<img>` child and all attributes untouched.
5. Leave `app/templates/login.html:80` and `app/templates/forgot_password.html:7`
   exactly as they are — they keep the class and now get the one-shot entrance.
6. Confirm the old keyframe name is gone: `grep -rn "cnPulse" app/` must return no
   results.

## Boundaries

- Do NOT remove the `logo-bounce` class from `login.html` or `forgot_password.html`.
  Those screens are seen rarely and an entrance is appropriate there.
- Do NOT touch `.brand-mark` in `app/static/tokens.css:83-97`.
- Do NOT touch `app/static/styles.css:301-308` (`.topbar-visual img` /
  `floatIn 4.6s infinite`). That is a separate infinite animation and a separate
  finding — it is deliberately out of scope here.
- Do NOT change the `<img>` element, `src`, or `alt` text in `base.html:26`.
- Do NOT add dependencies.
- If a step does not match the code you find (drift since commit 34b11fd), STOP and
  report instead of improvising.

## Verification

- **Mechanical**:
  - `grep -rn "cnPulse" app/` → no matches.
  - `grep -rn "logo-bounce" app/templates/` → exactly two matches:
    `login.html:80` and `forgot_password.html:7`. **Not** `base.html`.
  - `grep -n "logoIn" app/static/styles.css` → two matches (the rule and the keyframe).
  - `python -c "from app import create_app; create_app()"` still succeeds.
- **Feel check**: run the app.
  - Load the login page: the brand mark should fade up and settle **once**, in under
    half a second, then be completely still. Watch it for 10 seconds — it must not
    move again.
  - Log in and sit on the dashboard for 30 seconds without moving the mouse. The
    sidebar brand mark must be **perfectly static**. This is the whole point of the
    plan — if it moves at all, step 4 did not take effect.
  - In DevTools → Performance, record 5 seconds of an idle dashboard. There should be
    no recurring compositor/animation frames attributable to the brand mark.
  - In DevTools → Animations panel on the dashboard: the panel should capture no
    animation on the sidebar mark. On the login page it should capture exactly one,
    ending after 420ms.
  - In DevTools → Rendering → "Emulate CSS prefers-reduced-motion: reduce", load the
    login page: the mark is visible immediately and does not scale (the global
    reduced-motion rule at `app/static/styles.css:922-931` zeroes the duration).
    Confirm it is **visible**, not stuck at `opacity: 0`.
- **Done when**: the sidebar brand mark never moves, the login mark animates exactly
  once, and `cnPulse` no longer exists in the codebase.
