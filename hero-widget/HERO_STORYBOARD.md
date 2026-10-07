# Payrolla Hero — "Chaos → Flow" Build Doc

Locked concept, for reference: payroll starts fragmented; Payrolla turns it into flow.
Visual protagonist = the transformation. Brand protagonist = the ribbon (revealed as a
payoff at the end, not the opening subject). Style: premium, calm, technological — not
generic fintech.

Flags below mark calls I made that weren't explicitly decided yet — override any of them.

---

## 1. Architecture (how this scene gets driven)

React/GSAP owns scroll progress. Spline owns rendering. One-way data flow:

1. User scrolls the hero section in the browser.
2. GSAP ScrollTrigger (in `hero-widget`) converts scroll position inside the hero into a
   single `progress` value from 0 to 1.
3. On every scroll update, we call `splineApp.setVariable('progress', progress)` on the
   loaded Spline scene instance (via the `onLoad` callback from `@splinetool/react-spline`).
4. Inside Spline, every object's animated properties are derived from that one `progress`
   variable using Spline's own expressions/states — not separate scroll logic.

This keeps one clock. The video fallback (for mobile) is exported by scrubbing this same
`progress` value from 0 → 1 and recording the result, so both tiers stay visually identical.

**Practical implication for the Spline build:** don't use Spline's built-in "on scroll"
trigger at all. Build every animated property as driven by the `progress` number variable
(0–1), controlled externally. This is what makes it scrubbable, reversible, and shareable
between the live scene and the recorded fallback video.

**Important: `progress` is a master timeline, not a shared literal value.** Don't bind
every property directly to raw `progress` (0–1) — that makes everything move in lockstep
and reads as mechanical, not cinematic. Instead, remap `progress` into a different active
sub-range per element:

```
progress (master, 0 → 1)
   ├── camera:      0.00 → 1.00   (moves the whole time)
   ├── fragments:   0.15 → 0.70   (exit/align during processing)
   ├── ribbon:      0.45 → 0.90   (brightens/emerges as fragments clear)
   ├── lighting:    0.35 → 0.85   (cool → clean ramp)
   └── P reveal:    0.85 → 0.94   (fold reads as logo, then keeps moving)
```

Each property clamps and remaps `progress` into its own 0–1 within that sub-range (e.g.
fragments treat progress 0.15 as their "0" and 0.70 as their "1", holding still outside
that window). Still one clock — everything just doesn't have to react at the same rate.

---

## 2. Scroll-progress checkpoints

*(Flag: I set these percentages and durations — they're a starting point, not fixed. Easy
to retune once it's in Spline and you can feel it.)*

| Progress range | Stage | What's happening |
|---|---|---|
| 0 – 15% | **Chaos** | Scene sits mostly at rest (idle drift only). This is the "load-in" state before scrolling starts. |
| 15 – 50% | **Processing** | Camera dollies forward. Fragments begin rotating, drifting toward alignment. |
| 50 – 85% | **Flow** | Fragments merge into one continuous ribbon. Deep Teal → Bright Teal gradient resolves. Lighting warms. |
| 85 – 100% | **Reveal + handoff** | Sub-beats: 85% ribbon folds, 90% fold briefly reads as the Payrolla "P" (don't hold it — a glance, not a caption), 94% already moving past it. Ribbon continues forward/down, gradient bleeds into a real DOM accent line, page content below becomes visible. |

---

## 3. Object inventory (what to actually build in Spline)

*(Flag: these counts are a starting budget for performance reasons — see §6. Start lower,
scale up only if frame rate holds.)*

- **~20–30 fragment objects** representing disorganized payroll info — not literal
  spreadsheet cells. Mix of:
  - Thin flat card-like slivers (rounded rectangle, slightly curved, glass/metal material)
  - A few short curved ribbon-strand pieces (unformed fragments of the final ribbon)
  - Avoid literal digits/number textures — keep it abstract (see clichés list in the concept doc)
- **1 ribbon "hero" object** — exists in the scene from the start, dim/inactive at low
  progress. It does NOT receive a literal merge from the fragments (Spline can't morph
  separate meshes into one continuous object this way). Instead: fragments travel out of
  frame along a consistent trajectory as progress increases, while the ribbon brightens
  and emerges along that same path — the viewer reads it as one continuous motion even
  though it's really a handoff between two separate elements. Much easier to build and
  control than a literal morph. Built as a smooth extruded curve/tube, matching the logo's
  fold language.
- **1 camera** — dolly path from a pulled-back framing (progress 0) to a close push-through
  framing (progress ~0.85), then a final forward/down move for the handoff (0.85–1).
- **Lighting rig:** one cool, low-key rim light for the chaos state; one warmer key light
  that ramps in intensity as progress increases, plus a soft emissive glow on the ribbon
  material that only becomes visible past ~50% progress.

---

## 4. Variables to expose in Spline

Set these up as Spline **State/Number variables** so they're controllable from the
`setVariable()` call in React:

- `progress` (0–1) — the master driver, everything else derives from this
- Optional secondary variables if you want finer manual control later: `bloomIntensity`,
  `cameraZ` — but try driving everything off `progress` alone first before adding more;
  fewer variables = easier to debug.

---

## 5. Color reference

| Name | Hex | Use |
|---|---|---|
| Deep Teal | `#0D4D4D` | Ribbon gradient start, key light warm tone |
| Bright Teal | `#17C3B2` | Ribbon gradient end, glow/bloom accent |
| Soft Teal | `#D6F3F1` | Highlight/rim light tint only, not a fill color |
| White | `#FFFFFF` | Specular highlights only |
| Background (flag: not one of the 4 brand colors) | `#071410` or similar near-black teal | Void backdrop — needs to be darker than Deep Teal so the ribbon reads with contrast. Pick by eye in Spline against the actual materials; this hex is a starting guess, not final. |

---

## 6. Performance budget

- Start the fragment count at ~20, not 30 — add more only if it still runs smoothly on a
  mid-range laptop GPU with the dev build.
- Keep materials flat-color/gradient — no image textures on fragments.
- Bloom/glow: build it, but test toggling it off; if frame rate drops noticeably on
  integrated graphics, ship without bloom rather than degrade the whole scene.
- Target: stable 60fps on a reasonable modern laptop, with graceful degradation below that
  rather than a hard cutoff. Don't chase this number before the scene exists — test once
  there's something real to test, not before.
- Test matrix for the **live scene** specifically: integrated-GPU laptop, mid-range laptop,
  high-end desktop. Mobile is intentionally excluded from this list — mobile gets the
  pre-rendered video fallback, not the live scene, so it never needs to run this at all.
  The only mobile check that matters is "does the exported video look right," which is a
  different kind of test.
- If frame rate holds, don't optimize further. If it doesn't: cut fragment count before
  cutting anything else.

---

## 7. Fallback + reduced-motion

- **Mobile/weak-connection video:** export by scrubbing `progress` 0 → 1 at a fixed frame
  rate and recording — same scene, pre-rendered, no live interaction.
- **Reduced-motion static frame:** freeze at `progress = 1` (fully resolved ribbon, past
  the logo-reveal beat) — the most brand-legible single frame, not the chaotic opening.

---

## 8. Build order (suggested checklist for Spline)

Built as phase gates — don't move to the next phase until the current one actually looks
right scrubbing `progress` manually in Spline's editor. Cheaper to fix a 4-object proof of
concept than to fix 20 objects you already built the wrong way.

1. **Ribbon first.** Build the hero object in isolation — geometry, fold, gradient,
   lighting, and the final "P" silhouette need to be right before anything else depends on
   them.
2. **Camera, two keyframes only.** Progress 0: distant, sees the whole chaotic space.
   Progress 1: close, passing through/along the ribbon. Interpolate between them — don't
   add intermediate camera moves yet.
3. **Prove it with 3–4 fragments.** Wire up idle drift + exit-trajectory + the ribbon
   emerging behind them, all driven by `progress` with the sub-ranges from §1. Scrub the
   full 0–1 range. If chaos → processing → flow → P reveal → handoff already reads
   correctly with just 4 fragments, the concept works — everything after this is scaling,
   not figuring out.
4. **Scale to the full field (~20 objects).** Don't hand-design 20 unique fragments —
   build 4–5 fragment *types* and vary scale/rotation/position/timing offset across
   instances.
5. **Lighting ramp.** Cool/dark → cleaner/brighter, tied to `progress`. Keep it teal-family
   throughout — "warmer" means cleaner and brighter, not literally orange/amber.
6. **Bloom/glow** on the ribbon, gated to only appear past ~50% progress.
7. **Handoff, given real attention.** This is the last impression of the 3D scene — the
   ribbon shouldn't just disappear at the bottom, it should visibly lead the eye into
   whatever's next on the page (the DOM accent line from §1, or similar). Worth spending
   extra time here specifically.
8. Final scrub test: every checkpoint in §2 should look right at that exact `progress`
   value on its own in the Spline editor, independent of scroll, before wiring up React.

---

## 9. Typography / DOM content layer

*(This was missing from the first draft — the hero isn't only the 3D scene, and it needs
copy to actually explain what the animation is gesturing at.)*

Layer real DOM content (HTML, not part of the Spline scene) over/around the canvas:

- Headline
- One supporting line
- CTA button

Division of labor: the 3D animation is the emotional hook (payroll chaos → resolved
flow), the copy is the literal explanation, the CTA is the conversion moment. Visitors
shouldn't need to consciously interpret the animation for the page to work — the words
carry the actual meaning.

**Flag: exact copy is not decided.** Placeholder direction only, not locked —
something in the direction of "payroll, without the chaos" for the headline and a
one-line supporting explanation of what Payrolla actually does. Copywriting is a separate
decision from this build doc and worth treating as its own pass rather than inheriting
placeholder text as final.
