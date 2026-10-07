"""Build Payrolla's reference logo, icons and animation from one vector master.

Development dependencies: Pillow, numpy, Playwright/Chromium, fonttools and
brotli. Run scripts/fetch_fonts.py first, then this file with --ffmpeg PATH.
The Satoshi Bold wordmark is outlined so SVG images also work offline.
"""
from pathlib import Path
import argparse
import io
import math
import shutil
import subprocess
from urllib.parse import quote

from PIL import Image
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
IMAGES = ROOT / 'app/static/img'
SHELL = ROOT.parent / 'payrolla-desktop/desktop/shell'
# Open counter, rounded top-left shoulder and folded leaf, traced from the
# user's Payrolla brand reference. All variants reuse these exact contours.
RIBBON = ('M0 40C7 16 24 0 48 0H91C124 0 148 23 148 55.5'
          'C148 87 125 111 92 111H46C23 111 6 123 0 140V128'
          'C0 96 21 72 48 72H90C99 72 106 65 106 56'
          'C106 47 99 40 90 40Z')
STEM = 'M0 128C4 117 17 111 30 111H46V126C46 150 26 168 0 168Z'


def defs():
    return '''<defs>
      <linearGradient id="ribbon" x1="10" y1="142" x2="105" y2="0" gradientUnits="userSpaceOnUse">
        <stop stop-color="#008F8B"/><stop offset=".6" stop-color="#17C3B2"/><stop offset="1" stop-color="#42CBBB"/>
      </linearGradient>
      <linearGradient id="fold" x1="0" y1="168" x2="40" y2="111" gradientUnits="userSpaceOnUse">
        <stop stop-color="#073B3D"/><stop offset="1" stop-color="#0D4D4D"/>
      </linearGradient>
      <linearGradient id="tile" x2="1" y2="1"><stop stop-color="#073B3D"/><stop offset="1" stop-color="#08675F"/></linearGradient>
      <linearGradient id="word" x2="1" y2=".65"><stop stop-color="#0D4D4D"/><stop offset=".86" stop-color="#0D4D4D"/><stop offset="1" stop-color="#17C3B2"/></linearGradient>
    </defs>'''


def mark(dark=False, mono=False, part=None):
    stem = '#FFFFFF' if dark else 'url(#fold)'
    if mono:
        stem = '#242424'
    front = '#242424' if mono else 'url(#ribbon)'
    return (('' if part == 'ribbon' else f'<path fill="{stem}" d="{STEM}"/>') +
            ('' if part == 'stem' else f'<path fill="{front}" d="{RIBBON}"/>'))


def svg(box, body, label='Payrolla'):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}" '
            f'role="img" aria-label="{label}">{defs()}{body}</svg>\n')


def wordmark():
    font = TTFont(ROOT / 'app/static/fonts/satoshi-700.woff2')
    glyphs = font.getGlyphSet()
    cmap = font.getBestCmap()
    size = 145 / font['head'].unitsPerEm
    pen = SVGPathPen(glyphs)
    advance = 0
    for letter in 'Payrolla':
        # The reference uses Satoshi's single-storey a and a straight diagonal
        # y tail. Retain the licensed typeface for the other letter contours.
        glyph = glyphs['a.ss01' if letter == 'a' else cmap[ord(letter)]]
        if letter == 'y':
            top = -font['OS/2'].sxHeight * size
            outline = [(0, top), (20, top), (40, -24), (62, top),
                       (82, top), (35, 30), (15, 30), (30, -7)]
            pen.moveTo((195 + advance + outline[0][0], 149 + outline[0][1]))
            for x, y in outline[1:]:
                pen.lineTo((195 + advance + x, 149 + y))
            pen.closePath()
        else:
            glyph.draw(TransformPen(pen, (size, 0, 0, -size, 195 + advance, 149)))
        advance += 86 if letter == 'y' else glyph.width * size - 4
    return pen.getCommands(), math.ceil(195 + advance + 10)


def build_vectors():
    word, width = wordmark()
    for name, dark, mono in [('payrolla-logo.svg', False, False),
                             ('payrolla-logo-dark.svg', True, False),
                             ('payrolla-logo-mono.svg', False, True)]:
        fill = '#242424' if mono else ('#FFFFFF' if dark else 'url(#word)')
        body = f'<g transform="translate(10 10)">{mark(dark, mono)}</g><path fill="{fill}" d="{word}"/>'
        (IMAGES / name).write_text(svg(f'0 0 {width} 188', body), encoding='utf-8')
    (IMAGES / 'payrolla-logo.min.svg').write_bytes((IMAGES / 'payrolla-logo.svg').read_bytes())
    (IMAGES / 'payrolla-wordmark.svg').write_text(svg(f'185 35 {width - 185} 153',
        f'<path fill="url(#word)" d="{word}"/>'), encoding='utf-8')
    if (SHELL / 'ui').is_dir():
        shutil.copyfile(IMAGES / 'payrolla-wordmark.svg', SHELL / 'ui/payrolla-wordmark.svg')
    for name, dark in [('payrolla-icon.svg', False), ('payrolla-icon-dark.svg', True)]:
        (IMAGES / name).write_text(svg('0 0 168 188', f'<g transform="translate(10 10)">{mark(dark)}</g>'), encoding='utf-8')
    (IMAGES / 'payrolla-app-icon.svg').write_text(svg('0 0 1024 1024',
        '<rect width="1024" height="1024" rx="229" fill="url(#tile)"/>' +
        f'<g transform="translate(258.92 224.72) scale(3.42)">{mark(True)}</g>'), encoding='utf-8')
    (IMAGES / 'favicon.svg').write_text(svg('0 0 24 24',
        '<rect x=".5" y=".5" width="23" height="23" rx="5" fill="#FFFFFF" stroke="#D6F3F1"/>' +
        f'<g transform="translate(5.34 4.44) scale(.09)">{mark()}</g>'), encoding='utf-8')


def raster(page, source, width, height):
    uri = 'data:image/svg+xml;utf8,' + quote(source)
    page.set_viewport_size({'width': width, 'height': height})
    page.set_content(f'<body style="margin:0;background:transparent"><img src="{uri}" width="{width}" height="{height}" style="display:block"></body>')
    page.wait_for_function('document.images[0].complete && document.images[0].naturalWidth > 0')
    return Image.open(io.BytesIO(page.screenshot(omit_background=True))).convert('RGBA')


def build_icons(page):
    from build_logo_assets import ICON_PNGS, ICO_SIZES
    sizes = sorted(set(ICON_PNGS.values()) | set(ICO_SIZES) | {1024})
    rendered = {}
    for size in sizes:
        # Windows uses the dark app tile even at taskbar size. The browser's
        # white favicon remains a separate variant, as in the reference board.
        rendered[size] = raster(page, (IMAGES / 'payrolla-app-icon.svg').read_text(), size, size)
    for name, size in ICON_PNGS.items():
        rendered[size].save(SHELL / 'src-tauri/icons' / name)
    rendered[256].save(SHELL / 'src-tauri/icons/icon.ico', sizes=[(s, s) for s in ICO_SIZES],
                       append_images=[rendered[s] for s in ICO_SIZES if s != 256])
    rendered[1024].save(SHELL / 'src-tauri/app-icon.png')


def ease(t):
    t = max(0, min(1, t))
    return 1 - (1 - t) ** 3


def frames(page, dark, width, height, rect):
    x, y, w, h = [round(v * n) for v, n in zip(rect, (width, height, width, height))]
    layers = [raster(page, svg('0 0 148 168', mark(dark, part=part)), w, h)
              for part in ('stem', 'ribbon')]
    result = []
    for frame in range(121):
        canvas = Image.new('RGBA', (width, height))
        for index, image in enumerate(layers):
            progress = ease((frame - (12 if index == 0 else 0)) / 78)
            shifted = image.copy()
            shifted.putalpha(shifted.getchannel('A').point(lambda a: round(a * min(1, progress * 3))))
            angle = (1 - progress) * (18 if index == 0 else -12)
            shifted = shifted.rotate(angle, Image.Resampling.BICUBIC, expand=False)
            dy = round((1 - progress) * h * (.35 if index == 0 else -.6))
            canvas.alpha_composite(shifted, (x, y + dy))
        result.append(canvas)
    return result


def encode(frames, path, ffmpeg, scroll=False):
    from build_logo_assets import stacked
    width, height = frames[0].size
    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo',
               '-pix_fmt', 'rgb24', '-s', f'{width}x{height * 2}', '-r', '30', '-i', '-',
               '-an', '-c:v', 'libx264', '-crf', '19', '-preset', 'fast', '-pix_fmt', 'yuv420p',
               '-g', '4' if scroll else '120', '-bf', '0', '-movflags', '+faststart', str(path)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        for frame in frames:
            process.stdin.write(stacked(frame).tobytes())
    finally:
        process.stdin.close()
    if process.wait() != 0:
        raise RuntimeError(f'Encoding failed: {path}')


def build_motion(page, ffmpeg):
    from build_logo_assets import write_still, write_webp
    rect = (.2084, .4815, .5288, .4802)
    for tag, width, height in [('lg', 640, 800), ('sm', 320, 400)]:
        sequence = frames(page, True, width, height, rect)
        write_still(sequence[-1], str(IMAGES / f'payrolla-mark-dark-{tag}.webp'))
        encode(sequence, IMAGES / f'payrolla-mark-dark-{tag}.mp4', ffmpeg)
    from build_scroll_assets import build_scroll
    build_scroll(page, ffmpeg)
    splash = frames(page, False, 384, 480, (.2110, .4839, .5227, .4747))
    write_still(splash[-1], str(SHELL / 'ui/logo-end.webp'))
    write_webp(splash, str(SHELL / 'ui/logo-anim.webp'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--only', choices=['vectors', 'icons', 'motion'])
    args = parser.parse_args()
    build_vectors()
    if args.only == 'vectors':
        return
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(device_scale_factor=1)
        if args.only in (None, 'icons'):
            build_icons(page)
        if args.only in (None, 'motion'):
            build_motion(page, args.ffmpeg)
        browser.close()
    print('Built Payrolla reference logo assets.')


if __name__ == '__main__':
    main()
