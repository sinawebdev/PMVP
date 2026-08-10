# 001 — Cut the 520ms whole-page entrance animation

- **Status**: TODO
- **Commit**: 34b11fd
- **Severity**: HIGH
- **Category**: Purpose & frequency / Easing & duration
- **Estimated scope**: 2 files (`app/static/styles.css`, `app/templates/base.html`), ~15 lines changed

## Problem

The operator app is a server-rendered multi-page Flask app: every link click is a
full page load. Two 520ms entrance animations are wired into the base layout, so
**every single navigation** replays them — the topbar slides down from −10px while
all page content rises from +16px. Navigation is the most frequent interaction in
the product, and 520ms is nearly double the 300ms budget for UI motion. The result
is that the whole app feels like it is catching up with the user.

Current animation definitions:

```css
/* app/static/styles.css:344-350 — current */
.page-enter {
    animation: slideFade 520ms ease both;
}

.content-enter {
    animation: riseFade 520ms ease both;
}
```

```css
/* app/static/styles.css:882-902 — current keyframes */
@keyframes slideFade {
    from {
        opacity: 0;
        transform: translateY(-10px);
    }
    to {
        opacity: 1;
        transform: translateY(0);
    }
}

@keyframes riseFade {
    from {
        opacity: 0;
        transform: translateY(16px);
    }
    to {
        opacity: 1;
        transform: translateY(0);
    }
}
```

Applied on every page via the base template:

```jinja
{# app/templates/base.html:84 — current #}
<header class="topbar page-enter {% block topbar_class %}{% endblock %}">
```

```jinja
{# app/templates/base.html:110 — current #}
<div class="content-enter">
    {% block content %}{% endblock %}
</div>
```

Two separate problems:

1. **The topbar should not animate at all.** It is persistent chrome — the same
   header on every page. Re-introducing it with a slide on each navigation implies
   it is new content when it is not, which is the opposite of spatial consistency.
2. **The content entrance is too long and moves too far.** A 16px rise over 520ms
   with the weak built-in `ease` curve delays readable content. An opacity-only
   fade masks the paint without making the user wait for travel.

## Target

`.page-enter` is deleted and removed from the markup. `.content-enter` becomes an
opacity-only fade at 200ms on a strong ease-out curve. The `slideFade` keyframe
becomes unused and is deleted. A reusable `--ease-out` token is added to
`tokens.css`.

```css
/* app/static/tokens.css — target, added inside :root alongside the existing --ease */
    /* Motion — respected by both shells */
    --ease: 180ms ease;
    --ease-out: cubic-bezier(0.23, 1, 0.32, 1);   /* entrances, exits — starts fast */
    --ease-in-out: cubic-bezier(0.77, 0, 0.175, 1); /* on-screen movement */
```

```css
/* app/static/styles.css — target, replacing lines 344-350 */
.content-enter {
    animation: contentFade 200ms var(--ease-out) both;
}
```

```css
/* app/static/styles.css — target, replacing the @keyframes slideFade block */
@keyframes contentFade {
    from {
        opacity: 0;
    }
    to {
        opacity: 1;
    }
}
```

```jinja
{# app/templates/base.html:84 — target #}
<header class="topbar {% block topbar_class %}{% endblock %}">
```

`base.html:110` keeps `class="content-enter"` unchanged — only the CSS behind it
changes.

## Repo conventions to follow

- **All motion values belong in `app/static/tokens.css`**, inside the `:root` block
  under the comment `/* Motion — respected by both shells */` (currently
  `app/static/tokens.css:66-67`). That file is documented as "the single source of
  truth… If a value appears twice in a stylesheet, it belongs here instead."
- `tokens.css` is loaded before `styles.css` in `app/templates/base.html:14-15`, so
  `var(--ease-out)` resolves correctly in `styles.css`.
- Keyframes in `styles.css` are grouped together at lines 873-920, immediately
  before the `@media (prefers-reduced-motion: reduce)` block. Keep the new
  `contentFade` keyframe in that group, in the position `slideFade` occupied.
- **Exemplar of a correctly-scoped one-shot entrance**: `app/static/styles.css:627-628`
  (`.login-card { animation: riseFade 540ms ease both; }`). That one is *correct as
  is* — the login screen is seen once per session, so it has earned a longer
  entrance. Do not change it.

## Steps

1. In `app/static/tokens.css`, inside the `:root` block, extend the existing motion
   section (currently `--ease: 180ms ease;` at line 67) to add the two new curve
   tokens exactly as written in the Target section above. Do not remove or change
   `--ease` — `portal.css` depends on it in 8 places.
2. In `app/static/styles.css`, delete the entire `.page-enter` rule (lines 344-346)
   and replace the `.content-enter` rule (lines 348-350) with the target version
   using `contentFade 200ms var(--ease-out) both`.
3. In `app/static/styles.css`, replace the `@keyframes slideFade { … }` block
   (lines 882-891) with the `@keyframes contentFade { … }` block from the Target
   section. **Leave `@keyframes riseFade` (lines 893-902) in place** — it is still
   used by `.login-card` at line 628.
4. In `app/templates/base.html:84`, remove the `page-enter` class from the `<header>`
   element, leaving `class="topbar {% block topbar_class %}{% endblock %}"`.
5. Confirm nothing else references the removed names:
   `grep -rn "page-enter\|slideFade" app/` must return no results.

## Boundaries

- Do NOT touch `.login-card` (`app/static/styles.css:627-628`) or the `riseFade`
  keyframe. A once-per-session entrance at 540ms is a deliberate, correct choice.
- Do NOT touch `app/templates/base.html:110` — the `content-enter` class stays;
  only its CSS definition changes.
- Do NOT touch `app/static/portal.css` — the tenant portal never used these classes.
- Do NOT modify the `@media (prefers-reduced-motion: reduce)` block at
  `app/static/styles.css:922-931` in this plan.
- Do NOT change markup or structure beyond removing the one class name in step 4.
- Do NOT add dependencies.
- If a step does not match the code you find (drift since commit 34b11fd), STOP and
  report instead of improvising.

## Verification

- **Mechanical**:
  - `grep -rn "page-enter\|slideFade" app/` → no matches.
  - `grep -rn "content-enter" app/` → exactly two matches:
    `app/static/styles.css` (the rule) and `app/templates/base.html:110` (the usage).
  - `grep -n "ease-out" app/static/tokens.css` → matches the new token.
  - `python -c "import app"` from the repo root still succeeds (no template syntax
    error introduced): `python -c "from app import create_app; create_app()"`.
- **Feel check**: run the app, log in, and click between Dashboard → Clients →
  Employees several times in a row.
  - The topbar must be **immediately, statically present** on every page — no slide,
    no fade. It should feel like persistent chrome, not new content.
  - Page content should fade in fast enough that rapid navigation never feels like
    waiting. Clicking three links quickly should not produce a visible queue of
    fades.
  - In DevTools → Animations panel, set playback speed to 10% and reload: only ONE
    animation should be listed (`contentFade`), and it must contain no `transform`
    track — opacity only.
  - In DevTools → Rendering → "Emulate CSS prefers-reduced-motion: reduce", reload:
    content appears instantly, and is fully visible (never stuck at `opacity: 0`).
- **Done when**: navigation between two operator pages shows no topbar movement, the
  content fade completes in 200ms, and `slideFade`/`page-enter` no longer exist in
  the codebase.
