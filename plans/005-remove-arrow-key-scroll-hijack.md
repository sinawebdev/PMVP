# 005 — Remove the arrow-key scroll hijack on the landing page

- **Status**: TODO
- **Commit**: 34b11fd
- **Severity**: HIGH
- **Category**: Purpose & frequency / Accessibility
- **Estimated scope**: 1 file (`app/templates/landing.html`), ~14 lines deleted

## Problem

The landing page captures the four scroll keys globally and replaces normal scrolling
with an animated full-section jump:

```js
// app/templates/landing.html:725-737 — current
  // keyboard nav
  function currentIndex(){
    const mid=window.scrollY+window.innerHeight/2;
    let idx=0;
    sections.forEach((s,i)=>{if(s.offsetTop<=mid)idx=i;});
    return idx;
  }
  window.addEventListener('keydown',e=>{
    if(['ArrowDown','PageDown'].includes(e.key)){e.preventDefault();
      const n=Math.min(currentIndex()+1,sections.length-1);sections[n].scrollIntoView({behavior:'smooth'});}
    if(['ArrowUp','PageUp'].includes(e.key)){e.preventDefault();
      const n=Math.max(currentIndex()-1,0);sections[n].scrollIntoView({behavior:'smooth'});}
  });
```

Four separate defects in thirteen lines:

1. **Animation on a keyboard-initiated action.** Keys are the highest-frequency input
   there is. A user tapping ArrowDown expects the page to move *now*; instead each
   press starts a long smooth-scroll across a whole viewport height. Holding the key
   queues jump after jump. Keyboard actions should not animate.
2. **Normal scrolling is destroyed.** `e.preventDefault()` on ArrowUp/ArrowDown removes
   the ability to scroll by a line at a time. The page's `.sec` elements are
   `min-height:100vh` (`app/templates/landing.html:87`), so any section whose content
   exceeds the viewport becomes **partially unreachable by keyboard** — the user can
   only land on section boundaries and can never scroll to content in between.
3. **It fires regardless of focus.** The listener is on `window` with no check on
   `e.target`. Arrow keys inside any future input, textarea, or `<select>` on this page
   would be swallowed and turned into a page jump.
4. **It bypasses reduced-motion entirely.** `scrollIntoView({behavior:'smooth'})` is a
   JS API; the page's reduced-motion block at `app/templates/landing.html:352-358` only
   scopes `.lp-root *` in CSS and cannot reach it. A user who has explicitly asked the
   OS for reduced motion still gets animated scrolling on every arrow press. This is
   also the single most common trigger for motion-induced nausea.

The page already provides a deliberate, opt-in way to jump between sections — the dot
navigation at `app/templates/landing.html:665-675` — so removing the key handler costs
no capability.

## Target

The keyboard handler is deleted outright, restoring native browser scrolling. The
`currentIndex()` helper exists only to serve it and is deleted with it. Nothing
replaces them.

The end of the IIFE becomes:

```js
// app/templates/landing.html — target, replacing lines 724-738

  // Keyboard scrolling is intentionally left to the browser: native arrow/page
  // behaviour is instant, respects the user's reduced-motion setting, and reaches
  // content inside tall sections. Section-to-section jumps are available via the
  // dot nav above.
})();
```

Note that the dot-nav click handler at line 673
(`b.addEventListener('click',()=>s.scrollIntoView({behavior:'smooth'}))`) **stays as
is** in this plan. It is an explicit, deliberate user request to travel to a section,
which is a legitimate use of smooth scrolling — unlike an arrow-key press.

## Repo conventions to follow

- All landing-page JS lives in the single IIFE spanning
  `app/templates/landing.html:657-738`, which opens with
  `const root=document.querySelector('.lp-root'); if(!root) return;`. Deleting from
  it must leave the IIFE's closing `})();` intact and the `</script>` tag at line 739
  untouched.
- The file's JS is written in a dense style with minimal whitespace. Keep the
  replacement comment in normal prose sentences (the file already uses full-sentence
  comments, e.g. `// build dot nav` at line 665 and `// active section + reveal` at
  line 686).
- The `sections` array (line 660) is still used by the IntersectionObserver at line
  707 (`sections.forEach(s=>io.observe(s))`) and by the dot-nav builder at line 666.
  **It must not be removed.**

## Steps

1. In `app/templates/landing.html`, delete lines 725-737 in full: the `// keyboard nav`
   comment, the entire `function currentIndex(){…}` declaration, and the entire
   `window.addEventListener('keydown', …)` call.
2. In their place, insert the four-line explanatory comment from the Target section, so
   a future reader does not "restore" the behaviour as a missing feature.
3. Leave line 738 (`})();`) and line 739 (`</script>`) exactly as they are.
4. Confirm the helper has no other callers:
   `grep -n "currentIndex" app/templates/landing.html` must return no results.
5. Confirm the only remaining smooth-scroll calls are the two intentional ones:
   `grep -n "scrollIntoView" app/templates/landing.html` must return exactly one match
   (line ~673, the dot-nav click).

## Boundaries

- Do NOT remove or modify the dot-nav builder or its click handler
  (`app/templates/landing.html:665-675`). Clicking a dot is an explicit navigation
  request and smooth scrolling is appropriate there.
- Do NOT remove `html{scroll-behavior:smooth}` at `app/templates/landing.html:27` in
  this plan. It is a real finding (it is never reset for reduced-motion users, because
  the block at line 352 scopes only `.lp-root *`), but it is tracked separately and is
  out of scope here.
- Do NOT touch the `IntersectionObserver` (lines 686-707), the `sections` array
  (line 660), the scroll-progress handler (lines 677-685), or the counters
  (lines 709-723).
- Do NOT replace the deleted handler with a "fixed" version — no reduced-motion-gated
  variant, no `e.target` guard, no `behavior:'auto'` version. The correct amount of
  custom arrow-key handling on this page is none.
- Do NOT add dependencies.
- If a step does not match the code you find (drift since commit 34b11fd), STOP and
  report instead of improvising.

## Verification

- **Mechanical**:
  - `grep -n "currentIndex\|keydown" app/templates/landing.html` → no matches.
  - `grep -c "scrollIntoView" app/templates/landing.html` → `1`.
  - `grep -n "})();" app/templates/landing.html` → still present, immediately before
    `</script>`.
  - Open the page and check the browser console: **zero** JS errors. A stray brace from
    an over-eager deletion will surface here immediately, and would silently kill the
    dot nav, reveals, and counters along with it.
  - `python -c "from app import create_app; create_app()"` succeeds.
- **Feel check**: serve the app and open the landing page.
  - Click on the page background, then press ArrowDown once: the page must move a small
    amount **instantly**, exactly as it would on any normal web page — not jump a whole
    section.
  - Hold ArrowDown: scrolling must be continuous and smooth-feeling in the native way,
    with no queued jumps and no fighting.
  - Press PageDown: one viewport of native scroll, instantly.
  - Scroll into the tallest section and confirm you can now reach **every line of its
    content** with the arrow keys — this is the capability the hijack removed.
  - Click a dot in the right-hand nav: this must **still** smooth-scroll to that
    section. If it does not, step 1 deleted too much.
  - Scroll the whole page once and confirm the reveals (`.rv` elements fading up), the
    top progress bar, and the counters in the "proof" section all still fire — they
    share the IIFE with the deleted code.
  - In DevTools → Rendering → "Emulate CSS prefers-reduced-motion: reduce", press
    ArrowDown: the page must move instantly with no animated travel.
- **Done when**: arrow keys scroll natively and reach mid-section content, the dot nav
  still works, the reveals and counters still fire, and the console is clean.
