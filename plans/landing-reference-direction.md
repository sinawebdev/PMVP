# Payrolla landing: All in hand

7 October 2026. This replaces the rejected Folded Row study with the user's digital-banking reference direction. The attachment is a visual reference; its copy, claims, pricing, signup route and banking features are not requirements.

## Resulting design

Hero headline: **From payroll / to payslip. / All in hand.**

Dark ink/teal ground, large Satoshi headline on the left, sculptural payroll artwork on the right, adjacent web login and Windows download actions, and two concise product notes along the bottom. The documents are a payslip and a payroll register, rather than payment cards. Teal replaces the reference's gold as the main brand colour; the artwork retains warm studio highlights.

One oversized approved P sits behind the hero and workflow in a sticky decorative layer. Native page scrolling advances the original animation frames, resolves the mark, then holds the final pose until the dark section leaves. Scrolling upward reverses the correspondence. No wheel/touch handlers, inertia library, scroll snapping or forced scrolling are used. Copy and links are available immediately.

The page returns to the accepted warm grey-green application surfaces for the company-workspace preview and records. The dark closing band repeats the hero's actions. Download buttons remain conditional on a validated `DESKTOP_DOWNLOAD_URL`; Windows activation requires a company license.

| Before | After | Why |
| --- | --- | --- |
| Calm light hero with a logo box | Dark hero with a sculptural payroll/payslip image | Follows the reference the user selected |
| Small P playing on a timer | Large P whose frames follow native scroll position | Implements the requested background interaction |
| Multiple generic feature grids | A continuous payroll flow, a sample workspace and a compact record section | Connects the presentation to the application |

## Art and source assets

Generated with the built-in image-generation tool. The original PNG is archived locally under `.collaboration/landing-reference/hero-original.png`; the production page consumes only these responsive assets:

- `app/static/img/payrolla-payday-hand-lg.webp`: 1536×1024, 125,626 bytes.
- `app/static/img/payrolla-payday-hand-sm.webp`: 900×600, 65,768 bytes.

Transparency is preserved. The image contains only the PAYROLL and PAYSLIP headings and graphic document lines, with no invented payroll amounts or customer statistics. The original P artwork is separate and has not been regenerated.

Exact generation prompt:

> Use case: stylized-concept. Asset type: a standalone transparent raster hero illustration for Payrolla, a Ghanaian payroll and payslip application. Create a polished high-end 3D product render in the visual grammar of a premium digital banking hero: an open sculptural metallic hand extends into the scene from the right, palm upwards, fingers pointing left, carrying two slightly floating payroll documents above its palm. This is payroll and payslips, not a payments or bank-card service. Front object: an ivory vertical payslip with rounded edges, subtle physical thickness, the exact heading 'PAYSLIP', clean fine ruled lines and aligned typographic bars beneath, a small soft-teal total-pay field at the bottom. Rear object: a larger dark deep-teal payroll register slab, slightly rotated behind it, the exact heading 'PAYROLL', a small neatly ordered grid of rows, and subtle teal edge details. Documents should be recognisable and readable at hero scale, facing the viewer in three-quarter perspective; no credit cards, card numbers, card chips or bank logos. Material: the hand is beautiful brushed deep-teal metal with warm champagne specular highlights and a realistic open-palm sculptural shape. Palette: deep teal #0d4d4d, restrained bright teal #17c3b2, warm ivory, dark ink; restrained studio side lighting and physically grounded shadows. Composition: balanced wide illustration, all fingers and both documents within the image, the forearm reaches to the right border, transparent space above and around; no cropping of the payslip. Entire output has a genuine transparent background. No backdrop, no web-page layout, no headline or CTA, no floating stars, particles, currency symbols, logos, extra props, numbers or invented numerical metrics. Only the two document headings are readable text; other contents are typographic line treatments. This asset will sit on the right of a dark teal webpage with text on the left.

## Scroll player

`logo-player.js` has an optional `data-mark-scroll` mode. Callers without that attribute retain their original once-and-hold behaviour. The same stacked-alpha shader reconstructs the approved animation; only its playback clock changes. [Setting a video's currentTime seeks it](https://developer.mozilla.org/en-US/docs/Web/API/HTMLMediaElement/currentTime), which makes a direct scroll-to-frame mapping possible without a new UI dependency.

The player quantises seeks to 30fps, coalesces updates while a seek is pending, pauses rendering when the dark region is offscreen or the tab is hidden, and releases listeners and decoder resources on fallback/page exit. The scroll range is 0.8 viewport height on desktop and 1.1 viewport heights on phone. It starts 18% into the approved clip, where the ribbon is already recognisable.

Reduced motion, Save-Data, a reported low-memory device, missing WebGL, loading failure or decoder failure use the resolved still at the same size. No content waits for the video. A runtime reduced-motion change also releases the video and canvas.

`scripts/logo/build_scroll_assets.py` creates separate H.264 copies with at most four frames between keyframes, enabling responsive seeking. Desktop copy: 684,313 bytes; phone copy: 413,140 bytes. Original once-and-hold MP4s, stills and desktop splash assets remain untouched. FFmpeg is a development-only tool and is not shipped as an application dependency.

## Skills and collaboration

The existing Emil, library-selection and UI/UX guidance informed readable contrast, scoped CSS, immediate focus, native controls, responsive art and complete static fallbacks. Native media seeking fits this interaction; no React or animation-library migration is needed.

Actual Claude reviewed only the new planning brief in the approved tools-disabled session `7efd1030-4ba6-42d6-9ae8-ff45f6d04ecf`. Its recommendation connected “All in hand” to the artwork, specified dark-to-light continuity, and suggested a large P reveal behind the workflow. No current source, customer data or credentials were sent for that review. Codex implemented the page, generated and formatted the artwork, and built the scroll player and browser review.

## Verification

Existing landing and constitution tests are run, along with `scripts/review_landing.py`. The latter uses a disposable database and the real application CSP, renders phone/tablet/desktop layouts, compares actual canvas frames before/after scrolling, checks reversibility and stationary holds, verifies adjacent bottom actions, and exercises reduced-motion, no-JavaScript and media-load failure states. Captures and the final report are in `.screenshots/landing-reference/`.

Completed on 7 October 2026:

- `tests/test_landing.py tests/test_constitution_lint.py`: 14 passed and 12 subtests passed.
- Chrome: 81 checks passed at 1440, 1280, 768, 390, 360 and 320px.
- Edge: 34 checks passed at 1280 and 390px.
- Actual canvas frames change when scrolling; the decoder stays paused. Returning to the top restores the starting frame, and stationary scrolling holds the current frame.
- Desktop and phone captures were visually reviewed. Reduced motion, no JavaScript, failed media, runtime preference changes and keyboard skip navigation passed.
- `git diff --check`: passed.

On this Windows machine, Playwright's bundled Chromium reports no H.264 support and correctly shows the static fallback. Installed Chrome and Edge decode both the original and the new scroll MP4s. Use `--browser-channel chrome` or `--browser-channel msedge` to verify the actual animation here; the browser review fails rather than silently accepting a static mark as animated.

Headless-browser results establish layout and behaviour under that browser; they do not establish physical iPhone or integrated-GPU performance. Live deployment and a new desktop installer are separate release steps.
