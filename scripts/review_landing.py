"""Review the real reference-inspired landing and its native scroll animation.

Uses the disposable capture server and the application's real CSP. No payroll
or customer database is opened. Install Playwright as described in CONTRIBUTING.
"""
from pathlib import Path
import argparse
from contextlib import nullcontext
import json
import time
from playwright.sync_api import sync_playwright, expect

from capture_ui import _serve

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=str(ROOT / '.screenshots' / 'landing-reference'))
    parser.add_argument('--browser-channel', choices=('chrome', 'msedge'), help='Use an installed browser with H.264 support (required for Windows Playwright Chromium).')
    parser.add_argument('--base-url', help='Reuse a disposable local preview instead of seeding another capture server.')
    parser.add_argument('--widths', type=int, nargs='+', default=[1440, 1280, 768, 390, 360, 320])
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report = {'browser': args.browser_channel or 'Playwright Chromium', 'views': [], 'checks': []}

    def check(condition, label):
        report['checks'].append({'label': label, 'passed': bool(condition)})
        if not condition:
            raise AssertionError(label)

    def wait_for_seek(mark, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            video = mark.locator('video')
            if video.count():
                state = video.evaluate('video => ({time: video.currentTime, seeking: video.seeking, ready: video.readyState})')
                if not state['seeking'] and state['ready'] >= 2 and condition(state['time']):
                    return state['time']
            mark.page.wait_for_timeout(100)
        raise AssertionError('Scroll did not produce a decoded frame within five seconds')

    with (nullcontext(args.base_url) if args.base_url else _serve()) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch(**({'channel': args.browser_channel} if args.browser_channel else {}))
        try:
            for width in args.widths:
                context = browser.new_context(viewport={'width': width, 'height': 900})
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                response = page.goto(base, wait_until='networkidle')
                page.evaluate('document.fonts.ready')
                check(response.status == 200, f'{width}: landing returns HTTP 200')
                check(not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1'), f'{width}: no page overflow')
                check(page.locator('.hero-art').evaluate('image => image.complete && image.naturalWidth > 0'), f'{width}: hero artwork loaded')
                check(page.evaluate('document.fonts.check("700 40px Satoshi")'), f'{width}: local Satoshi loaded')
                mark = page.locator('.hero-mark')
                expect(mark).to_have_attribute('data-mark-state', 'scroll', timeout=10000)
                check(mark.locator('video').evaluate('video => video.paused'), f'{width}: video stays paused, scroll controls time')
                initial = mark.locator('video').evaluate('video => video.currentTime')
                initial_frame = mark.locator('canvas').evaluate('canvas => canvas.toDataURL()')
                page.screenshot(path=str(output / f'hero-{width}.png'))
                page.screenshot(path=str(output / f'page-{width}.png'), full_page=True)

                page.evaluate('scrollTo(0, innerHeight * .5)')
                # Playwright polling stays outside the page and does not require
                # an unsafe-eval exception in the application's CSP.
                expect(mark).not_to_have_attribute('data-scroll-progress', '0.000', timeout=5000)
                forward = wait_for_seek(mark, lambda value: value > initial + .3)
                forward_frame = mark.locator('canvas').evaluate('canvas => canvas.toDataURL()')
                check(forward > initial + .3, f'{width}: scrolling advances approved frames')
                check(forward_frame != initial_frame, f'{width}: rendered frame changes, not just a progress label')
                page.screenshot(path=str(output / f'scroll-{width}.png'))
                page.evaluate('scrollTo(0, 0)')
                expect(mark).to_have_attribute('data-scroll-progress', '0.000', timeout=5000)
                reverse = wait_for_seek(mark, lambda value: value < forward - .3)
                check(reverse < forward - .3, f'{width}: scrolling back reverses the animation')
                page.wait_for_timeout(300)
                check(abs(mark.locator('video').evaluate('video => video.currentTime') - reverse) < .05, f'{width}: mark holds while scroll is stationary')
                page.locator('.marketing-close').scroll_into_view_if_needed()
                access = page.locator('.marketing-close .marketing-actions a')
                check(access.count() == 2, f'{width}: bottom login and configured download present')
                if width >= 360:
                    boxes = [link.bounding_box() for link in access.all()]
                    check(abs(boxes[0]['y'] - boxes[1]['y']) < 2, f'{width}: login and download remain adjacent')
                check(not errors, f'{width}: no JavaScript errors {errors}')
                report['views'].append({'width': width, 'start_time': initial, 'forward_time': forward, 'reverse_time': reverse})
                print(f'Reviewed landing at {width}px', flush=True)
                context.close()

            context = browser.new_context(viewport={'width': 390, 'height': 900}, reduced_motion='reduce')
            page = context.new_page()
            page.goto(base, wait_until='networkidle')
            check(page.locator('.hero-mark video').count() == 0, 'Reduced motion: no video downloaded/created')
            check(page.locator('.hero-mark-still').evaluate('image => getComputedStyle(image).visibility === "visible"'), 'Reduced motion: resolved P remains visible')
            context.close()

            context = browser.new_context(viewport={'width': 390, 'height': 900}, java_script_enabled=False)
            page = context.new_page()
            page.goto(base, wait_until='networkidle')
            check(page.locator('.hero-mark video').count() == 0, 'No JavaScript: static P')
            check(page.locator('.hero-copy a[href="/login"]').is_visible(), 'No JavaScript: login available')
            check(page.locator('.hero-art').evaluate('image => image.complete && image.naturalWidth > 0'), 'No JavaScript: hero artwork visible')
            context.close()

            context = browser.new_context(viewport={'width': 1280, 'height': 900})
            context.route('**/payrolla-mark-scroll-*.mp4', lambda route: route.abort())
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base, wait_until='networkidle')
            expect(page.locator('.hero-mark')).to_have_attribute('data-mark-state', 'still', timeout=8000)
            check(page.locator('.hero-mark-still').evaluate('image => getComputedStyle(image).visibility === "visible"'), 'Decode/load failure: static P survives')
            check(not errors, 'Decode/load failure: no JavaScript errors')
            context.close()

            context = browser.new_context(viewport={'width': 1280, 'height': 900})
            page = context.new_page()
            page.goto(base, wait_until='networkidle')
            expect(page.locator('.hero-mark')).to_have_attribute('data-mark-state', 'scroll', timeout=10000)
            page.emulate_media(reduced_motion='reduce')
            expect(page.locator('.hero-mark')).to_have_attribute('data-mark-state', 'still')
            check(page.locator('.hero-mark video').count() == 0, 'Preference change: decoder and video released')
            page.keyboard.press('Tab')
            check(page.locator('.marketing-skip').evaluate('node => node === document.activeElement'), 'Keyboard: skip link receives first focus')
            page.keyboard.press('Enter')
            check(page.evaluate('document.activeElement.id === "marketing-main"'), 'Keyboard: skip link reaches main content')
            context.close()
        finally:
            browser.close()

    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(f'{len(report["checks"])} landing checks passed; report {output / "report.json"}', flush=True)


if __name__ == '__main__':
    main()
