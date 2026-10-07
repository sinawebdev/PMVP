"""Build the landing's dimensional scroll sequence from the approved SVG master.

Development only. Uses Pillow, numpy and Playwright; pass an FFmpeg binary
with libx264 via --ffmpeg. No desktop splash or static brand assets are changed.
"""
from pathlib import Path
import argparse
import math

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
IMAGES = ROOT / 'app/static/img'
RECT = (.2084, .4815, .5288, .4802)


def smooth(t):
    t = max(0., min(1., t))
    return t * t * (3 - 2 * t)


def projected(image, canvas_size, origin, pivot, angles, offset, depth=0.):
    """Project a ribbon plane about its fold, using a camera homography."""
    width, height = image.size
    pitch, yaw, roll = np.radians(angles)
    cx, sx = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cz, sz = math.cos(roll), math.sin(roll)
    rotation = (np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]) @
                np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]]) @
                np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]))
    corners = [(0, 0), (width, 0), (width, height), (0, height)]
    destination = []
    distance = height * 4.5
    for x, y in corners:
        point = rotation @ np.array([x - pivot[0], y - pivot[1], depth])
        scale = distance / (distance - point[2])
        destination.append((origin[0] + pivot[0] + point[0] * scale + offset[0],
                            origin[1] + pivot[1] + point[1] * scale + offset[1]))
    equations, values = [], []
    for (x, y), (u, v) in zip(destination, corners):
        equations += [[x, y, 1, 0, 0, 0, -u*x, -u*y],
                      [0, 0, 0, x, y, 1, -v*x, -v*y]]
        values += [u, v]
    coefficients = np.linalg.solve(equations, values)
    return image.transform(canvas_size, Image.Transform.PERSPECTIVE, coefficients,
                           resample=Image.Resampling.BICUBIC)


def lit_face(image, progress, strength):
    """A broad traveling reflection, clipped to the original ribbon alpha."""
    pixels = np.array(image, dtype=np.float32)
    height, width = pixels.shape[:2]
    y, x = np.mgrid[0:height, 0:width]
    sweep = x / width + .28 * y / height
    centre = -.25 + progress * 1.8
    light = np.exp(-((sweep - centre) / .18) ** 2) * strength
    pixels[..., :3] += (255 - pixels[..., :3]) * light[..., None]
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))


def scroll_frames(page, width, height):
    from build_brand_assets import mark, raster, svg
    x, y, w, h = [round(v * n) for v, n in zip(RECT, (width, height, width, height))]
    layers = [raster(page, svg('0 0 148 168', mark(True, part=part)), w, h)
              for part in ('stem', 'ribbon')]
    sides = []
    for image in layers:
        side = Image.new('RGBA', image.size, '#0A736D')
        side.putalpha(image.getchannel('A'))
        sides.append(side)
    result = []
    for frame in range(121):
        t = frame / 120
        canvas = Image.new('RGBA', (width, height))
        orbit = math.sin(math.pi * t)
        thickness = h * .028 * (1 - smooth((t - .82) / .18))
        for index, image in enumerate(layers):
            fold = smooth((t - .1) / .72) if index == 0 else smooth(t / .72)
            remaining = 1 - fold
            if index == 0:
                # Pivot at the leaf's attachment, not the image centre.
                pivot = (w * 23 / 148, h * 117 / 168)
                angles = (-14 * remaining - 5 * orbit,
                          64 * remaining + 9 * orbit,
                          19 * remaining - 2 * orbit)
                offset = (-w * .07 * remaining, h * .14 * remaining)
            else:
                pivot = (w * .5, h * .33)
                angles = (12 * remaining - 5 * orbit,
                          -25 * remaining + 9 * orbit,
                          -8 * remaining - 2 * orbit)
                offset = (-w * .06 * remaining, -h * .22 * remaining)
            # The shallow edge tapers away into the exact flat master.
            for depth in np.linspace(-thickness, 0, 4)[:-1]:
                canvas.alpha_composite(projected(sides[index], (width, height),
                    (x, y), pivot, angles, offset, depth))
            face = lit_face(image, t, .22 * orbit if index else .1 * orbit)
            canvas.alpha_composite(projected(face, (width, height),
                (x, y), pivot, angles, offset))
        if frame == 120:
            canvas = Image.new('RGBA', (width, height))
            for layer in layers:
                canvas.alpha_composite(layer, (x, y))
        result.append(canvas)
    return result


def build_scroll(page, ffmpeg):
    from build_brand_assets import encode
    for tag, width, height in [('lg', 640, 800), ('sm', 320, 400)]:
        sequence = scroll_frames(page, width, height)
        output = IMAGES / f'payrolla-mark-scroll-{tag}.mp4'
        encode(sequence, output, ffmpeg, scroll=True)
        print(f'{output.name}: {output.stat().st_size:,} bytes', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    args = parser.parse_args()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        build_scroll(browser.new_page(device_scale_factor=1), args.ffmpeg)
        browser.close()


if __name__ == '__main__':
    main()
