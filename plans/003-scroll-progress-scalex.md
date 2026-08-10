# 003 — Drive the landing scroll progress bar with transform, not width

- **Status**: TODO
- **Commit**: 34b11fd
- **Severity**: HIGH
- **Category**: Performance
- **Estimated scope**: 1 file (`app/templates/landing.html`), ~10 lines changed

## Problem

The landing page's scroll progress indicator is driven by writing `style.width` from
a scroll listener. Animating `width` triggers layout → paint → composite; doing it
from a scroll handler that *also reads* layout first produces a forced
synchronous layout (read-then-write thrash) on the document root for every scroll
event the browser delivers.

```css
/* app/templates/landing.html:71-72 — current */
  .lp-root .progress{position:fixed;top:0;left:0;height:3px;width:0;z-index:40;
    background:linear-gradient(90deg,var(--gold),var(--teal-bright));box-shadow:0 0 12px var(--teal-glow)}
```

```html
<!-- app/templates/landing.html:367 — current markup (unchanged by this plan) -->
<div class="progress" id="lp-prog"></div>
```

```js
// app/templates/landing.html:674-681 — current
  // scroll progress
  function onScroll(){
    const h=document.documentElement;
    const max=h.scrollHeight-h.clientHeight;
    prog.style.width=(max>0?(h.scrollTop/max*100):0)+'%';
  }
  window.addEventListener('scroll',onScroll,{passive:true});
  onScroll();
```

Three compounding costs, all on the hottest path on the page:

1. **`scrollHeight` and `clientHeight` are read on every scroll event.** Both force
   the browser to flush pending layout before returning a value.
2. **`width` is then written**, invalidating layout again — so the next event's read
   has to re-flush. This is textbook layout thrashing.
3. **No frame throttling.** Scroll events can fire more often than the browser
   paints, so the work is done multiple times per rendered frame.

The page also runs three infinite 22–28s aurora animations and an IntersectionObserver,
so this handler is competing for main-thread time exactly when smooth scrolling
matters most.

## Target

The bar is laid out once at full width and scaled horizontally from its left edge.
`transform` is composited — no layout, no paint. The document height is measured
once (and on resize only), and the write is coalesced into one `requestAnimationFrame`
per frame.

```css
/* app/templates/landing.html — target, replacing lines 71-72 */
  .lp-root .progress{position:fixed;top:0;left:0;height:3px;width:100%;z-index:40;
    transform:scaleX(0);transform-origin:0 50%;will-change:transform;
    background:linear-gradient(90deg,var(--gold),var(--teal-bright));box-shadow:0 0 12px var(--teal-glow)}
```

```js
// app/templates/landing.html — target, replacing lines 677-685
  // scroll progress — transform-driven (composited); layout read once, write
  // coalesced to one rAF per frame.
  let progMax=0, progQueued=false;
  function measureProg(){
    const h=document.documentElement;
    progMax=h.scrollHeight-h.clientHeight;
  }
  function paintProg(){
    progQueued=false;
    const p=progMax>0?Math.min(document.documentElement.scrollTop/progMax,1):0;
    prog.style.transform='scaleX('+p+')';
  }
  function onScroll(){
    if(progQueued)return;
    progQueued=true;
    requestAnimationFrame(paintProg);
  }
  window.addEventListener('scroll',onScroll,{passive:true});
  window.addEventListener('resize',function(){measureProg();onScroll();},{passive:true});
  measureProg();
  paintProg();
```

Note the two behavioural details that must be preserved: the bar starts at zero
progress on load (`transform:scaleX(0)` in CSS matches the old `width:0`), and it is
clamped to 1 so overscroll/rubber-banding cannot push it past full.

## Repo conventions to follow

- `app/templates/landing.html` is **fully self-contained**: it loads neither
  `tokens.css` nor `styles.css` (see its `<head>` at lines 1-10 — only Google Fonts
  and a Font Awesome CDN link). **Do not reference `var(--ease-out)` or any token
  from `tokens.css` here** — it will not resolve. All landing-page values stay inline
  in this file.
- The page's CSS lives in one `<style>` block and is written in a dense
  single-line-per-selector style (`selector{prop:val;prop:val}`). Match that
  formatting exactly — do not reformat the surrounding rules to multi-line.
- All landing-page JS lives in the single IIFE at lines 657-740, which starts with
  `const root=document.querySelector('.lp-root'); if(!root) return;`. `prog` is
  already declared at line 662 (`const prog = document.getElementById('lp-prog');`) —
  reuse it, do not re-query.
- **Exemplar of correct transform-based motion already in this file**:
  `app/templates/landing.html:97-99` (`.rv` reveal), which animates `opacity` and
  `transform` only.

## Steps

1. In `app/templates/landing.html`, replace the `.lp-root .progress{…}` rule at lines
   71-72 with the target CSS above. Keep the `background` and `box-shadow`
   declarations byte-identical — only the geometry/transform properties change
   (`width:0` → `width:100%`, plus the three new transform properties).
2. In the `<script>` IIFE, replace the "scroll progress" section (lines 674-681,
   from the `// scroll progress` comment through the `onScroll();` call) with the
   target JS above.
3. Leave `const prog = document.getElementById('lp-prog');` at line 662 in place —
   the new code uses it.
4. Leave the `<div class="progress" id="lp-prog"></div>` markup at line 367 unchanged.
5. Confirm no other code writes to the bar's width:
   `grep -n "prog.style" app/templates/landing.html` must show only the single
   `transform` write from step 2.

## Boundaries

- Do NOT touch the aurora animations at `app/templates/landing.html:42-47`. Those are
  a separate finding, deliberately out of scope.
- Do NOT touch the `IntersectionObserver` block (lines 683-704), the counters
  (lines 706-723), or the keyboard/dot navigation (lines 665-672, 725-737).
- Do NOT add `will-change` to anything other than `.lp-root .progress`. Applied
  broadly it costs more memory than it saves.
- Do NOT convert this page to use `tokens.css` / `styles.css` — it is intentionally
  standalone.
- Do NOT reformat unrelated CSS in the `<style>` block.
- Do NOT add dependencies.
- If a step does not match the code you find (drift since commit 34b11fd), STOP and
  report instead of improvising.

## Verification

- **Mechanical**:
  - `grep -n "prog.style" app/templates/landing.html` → exactly one match, and it
    writes `transform`, not `width`.
  - `grep -n "scaleX" app/templates/landing.html` → matches in both the CSS rule and
    the JS.
  - `python -c "from app import create_app; create_app()"` succeeds, and the landing
    route renders without a Jinja error.
- **Feel check**: serve the app and open the landing page.
  - Scroll from top to bottom: the bar must fill left-to-right smoothly and reach
    exactly the full viewport width at the bottom of the page. Any gap at the right
    edge means `transform-origin` or the clamp is wrong.
  - Reload at the top: the bar must be invisible (zero width), not briefly full.
  - Resize the window narrower, then scroll: the bar must still reach full width at
    the bottom — this proves the `resize` re-measure works.
  - On macOS/trackpad, overscroll past the bottom: the bar must not exceed full width.
  - In DevTools → Performance, record a continuous scroll of the whole page. In the
    flame chart there must be **no "Layout" or "Recalculate Style" entries attributed
    to the scroll handler**, and no purple "Layout Shift"/forced-reflow warnings
    (look for the red-triangle "Forced reflow" markers, which the old code produced
    on every event). Frames should stay at the display's refresh rate.
  - In DevTools → Rendering, enable "Paint flashing" and scroll: the progress bar
    must not repaint (no green flash on it) as it grows.
- **Done when**: a full-page scroll produces no forced-reflow warnings in the
  Performance panel, and the bar still reads 0% at the top and 100% at the bottom
  after a window resize.
