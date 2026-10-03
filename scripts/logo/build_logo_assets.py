"""Build the animated-logo assets the landing page and the desktop splash ship.

Dev tooling, like capture_ui.py: needs Blender 5.x (headless) and Pillow +
numpy from the dev venv. Nothing here runs in production.

Pipeline, per variant:
  1. render -- Blender renders PNG frames of payrolla_logo_anim.py (frames
     0-120; nothing moves after 120, so the brief's hold is the last frame
     simply staying up).
  2. crop   -- every frame is cropped to the box the motion used. A render
     whose motion touches the canvas edge is refused: the crop would cut the
     ribbon mid-flight, which is exactly the v1 defect.
  3. encode
       splash  -> animated WebP that plays once and holds (the desktop splash
                  is WebView2, a local file, so weight does not matter there)
       landing -> stacked-alpha MP4: colour in the top half, the alpha matte
                  in the bottom half, H.264 via Blender's bundled FFmpeg.
                  static/logo-player.js recombines them in WebGL. Transparent
                  WebM would be simpler but Safari ignores VP9 alpha, and an
                  animated WebP of this size is well over a megabyte.
     plus a still of the final frame in the same box, for reduced motion and
     for any browser that cannot play the animation.
  4. report -- where the finished P sits in the box, as the percentages the
     templates' CSS uses. Paste them in if the framing ever changes.

It also rasterises the desktop app's icon set from the SVG masters
(--only icons; needs Playwright's Chromium, not Blender).

Usage:
  python scripts/logo/build_logo_assets.py                  # everything
  python scripts/logo/build_logo_assets.py --skip-render    # re-encode last frames
  python scripts/logo/build_logo_assets.py --only splash    # or landing, or icons
"""

from __future__ import annotations

import argparse
import glob
import io
import os
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote

import numpy as np
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
BLEND_SCRIPT = os.path.join(HERE, "payrolla_logo_anim.py")
BLENDER = os.environ.get(
    "BLENDER", r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe")
WORK = os.path.join(tempfile.gettempdir(), "payrolla-logo-build")
WEB_IMG = os.path.join(REPO_ROOT, "app", "static", "img")
# The desktop shell is a sibling checkout; its splash loads before the
# backend exists, so its assets live in the shell, not in the web app.
DESKTOP_SHELL = os.path.join(os.path.dirname(REPO_ROOT), "payrolla-desktop", "desktop", "shell")
SPLASH_UI = os.path.join(DESKTOP_SHELL, "ui")
DESKTOP_ICONS = os.path.join(DESKTOP_SHELL, "src-tauri", "icons")
# `tauri icon`'s file list, sized as it sizes them.
ICON_PNGS = {"32x32.png": 32, "64x64.png": 64, "128x128.png": 128, "128x128@2x.png": 256,
             "icon.png": 512, "StoreLogo.png": 50, "Square30x30Logo.png": 30,
             "Square44x44Logo.png": 44, "Square71x71Logo.png": 71, "Square89x89Logo.png": 89,
             "Square107x107Logo.png": 107, "Square142x142Logo.png": 142,
             "Square150x150Logo.png": 150, "Square284x284Logo.png": 284,
             "Square310x310Logo.png": 310}
ICO_SIZES = (16, 24, 32, 48, 64, 256)

FPS = 30
LAST_FRAME = 120
MARK_W, MARK_H = 2.9, 3.45          # finished mark, ribbon units
ALPHA_FLOOR = 2                     # alpha at or below this is empty canvas

# Render window (width, height, centre x, centre y) in ribbon units around
# the finished mark's centre. Measured motion: x -2.52..2.82, y -1.97..5.15;
# this leaves ~0.45 units of margin all round.
REGION = (6.4, 8.0, 0.15, 1.6)
VARIANTS = {
    # The splash window is 520x340; the P shows at ~96px, so 60px/unit is 2x.
    "splash": {"ground": "light", "px_per_unit": 60, "region": REGION},
    # Landing hero: the P tops out at 260px on desktop; 113px/unit is 1.5x.
    # The small cut is for phones, where it shows at 64px.
    "landing": {"ground": "dark", "px_per_unit": 113, "region": REGION, "small_scale": 0.38},
}


# --------------------------------------------------------------------- render
def render(name: str, spec: dict) -> str:
    frames_dir = os.path.join(WORK, name, "frames")
    shutil.rmtree(frames_dir, ignore_errors=True)
    os.makedirs(frames_dir)
    w, h, cx, cy = spec["region"]
    env = dict(os.environ,
               PAYROLLA_LOGO_LAYOUT="mark", PAYROLLA_LOGO_GROUND=spec["ground"],
               PAYROLLA_LOGO_FRAMING="asset", PAYROLLA_LOGO_PX_PER_UNIT=str(spec["px_per_unit"]),
               PAYROLLA_LOGO_SAMPLES="32",
               PAYROLLA_LOGO_REGION_W=str(w), PAYROLLA_LOGO_REGION_H=str(h),
               PAYROLLA_LOGO_REGION_CX=str(cx), PAYROLLA_LOGO_REGION_CY=str(cy))
    cmd = [BLENDER, "-b", "--factory-startup", "--python", BLEND_SCRIPT,
           "-S", "Payrolla_Logo", "-o", os.path.join(frames_dir, "f_####"),
           "-s", "0", "-e", str(LAST_FRAME), "-a"]
    print(f"[{name}] rendering {LAST_FRAME + 1} frames ...", flush=True)
    subprocess.run(cmd, env=env, check=True, stdout=subprocess.DEVNULL)
    return frames_dir


def load_frames(frames_dir: str) -> list[np.ndarray]:
    paths = sorted(glob.glob(os.path.join(frames_dir, "f_*.png")))
    if len(paths) != LAST_FRAME + 1:
        raise SystemExit(f"expected {LAST_FRAME + 1} frames in {frames_dir}, found {len(paths)}")
    return [np.asarray(Image.open(p).convert("RGBA")) for p in paths]


# ----------------------------------------------------------------------- crop
def motion_box(frames: list[np.ndarray], pad: int = 3) -> tuple[int, int, int, int]:
    """Union of every frame's visible pixels, padded, as (left, top, right, bottom)."""
    seen = np.zeros(frames[0].shape[:2], dtype=bool)
    for f in frames:
        seen |= f[..., 3] > ALPHA_FLOOR
    ys, xs = np.nonzero(seen)
    h, w = seen.shape
    if xs.min() == 0 or ys.min() == 0 or xs.max() == w - 1 or ys.max() == h - 1:
        raise SystemExit("the motion touches the edge of the render; widen the region")
    box = [xs.min() - pad, ys.min() - pad, xs.max() + 1 + pad, ys.max() + 1 + pad]
    # even dimensions: H.264's 4:2:0 chroma needs them, and WebP does not mind
    box[2] += (box[2] - box[0]) % 2
    box[3] += (box[3] - box[1]) % 2
    return tuple(int(v) for v in box)


def mark_rect(final: np.ndarray, box: tuple[int, int, int, int]) -> dict[str, float]:
    """Where the finished P sits in the box, as fractions of the box."""
    ys, xs = np.nonzero(final[..., 3] > 128)
    bw, bh = box[2] - box[0], box[3] - box[1]
    return {"left": (xs.min() - box[0]) / bw, "top": (ys.min() - box[1]) / bh,
            "width": (xs.max() + 1 - xs.min()) / bw, "height": (ys.max() + 1 - ys.min()) / bh}


def crop(frames, box, scale=1.0) -> list[Image.Image]:
    out = []
    for f in frames:
        im = Image.fromarray(f[box[1]:box[3], box[0]:box[2]], "RGBA")
        if scale != 1.0:
            size = (round(im.width * scale / 2) * 2, round(im.height * scale / 2) * 2)
            im = im.resize(size, Image.LANCZOS)
        out.append(im)
    return out


# --------------------------------------------------------------------- encode
def frame_durations(n: int) -> list[int]:
    """Millisecond durations that average exactly 1000/FPS."""
    return [round((i + 1) * 1000 / FPS) - round(i * 1000 / FPS) for i in range(n)]


def write_webp(frames: list[Image.Image], path: str) -> None:
    # loop=1: one cycle, then the last frame stays (WebP's loop count is the
    # number of plays; 0 would mean forever). method=4: method 6 took ~9 CPU-
    # minutes on 121 frames for a few percent, and this file never leaves the
    # machine it is installed on.
    frames[0].save(path, "WEBP", save_all=True, append_images=frames[1:],
                   duration=frame_durations(len(frames)), loop=1,
                   quality=82, method=4, alpha_quality=100)


def write_still(frame: Image.Image, path: str) -> None:
    frame.save(path, "WEBP", quality=90, method=6, alpha_quality=100)


def stacked(frame: Image.Image) -> Image.Image:
    """Colour over alpha. The colour is bled outward under transparent pixels
    first: 4:2:0 chroma would otherwise average the edge colour with black and
    leave a dark fringe once the alpha cuts it back out."""
    a = np.asarray(frame, dtype=np.float32) / 255.0
    rgb, alpha = a[..., :3], a[..., 3:4]
    blur = lambda arr: np.asarray(
        Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(6)), dtype=np.float32) / 255.0
    pre = blur(rgb * alpha)
    cov = blur(np.repeat(alpha, 3, axis=2))
    bled = np.where(cov > 0.004, pre / np.maximum(cov, 1e-4), 0.0)
    colour = rgb * alpha + bled * (1.0 - alpha)
    matte = np.repeat(alpha, 3, axis=2)
    both = np.concatenate([colour, matte], axis=0)
    return Image.fromarray((np.clip(both, 0, 1) * 255 + 0.5).astype(np.uint8), "RGB")


VSE_SCRIPT = r'''
import bpy, glob, os, sys
args = sys.argv[sys.argv.index("--") + 1:]
src, out = args[0], args[1]
files = sorted(glob.glob(os.path.join(src, "*.png")))
scene = bpy.context.scene
w, h = bpy.data.images.load(files[0]).size[:]
r = scene.render
r.resolution_x, r.resolution_y, r.resolution_percentage = w, h, 100
r.fps, scene.frame_start, scene.frame_end = 30, 1, len(files)
scene.view_settings.view_transform = "Standard"   # identity for sRGB pixels
scene.view_settings.look = "None"
ed = scene.sequence_editor_create()
coll = ed.strips if hasattr(ed, "strips") else ed.sequences
strip = coll.new_image(name="stacked", filepath=files[0], channel=1, frame_start=1,
                       fit_method="ORIGINAL")
for f in files[1:]:
    strip.elements.append(os.path.basename(f))
strip.colorspace_settings.name = "sRGB"
if hasattr(r.image_settings, "media_type"):   # Blender 5 split video out of file_format
    r.image_settings.media_type = "VIDEO"
r.image_settings.file_format = "FFMPEG"
ff = r.ffmpeg
ff.format, ff.codec = "MPEG4", "H264"
ff.constant_rate_factor, ff.ffmpeg_preset = "HIGH", "BEST"
ff.gopsize = len(files)
ff.audio_codec = "NONE"
r.filepath = out
bpy.ops.render.render(animation=True)
'''


def write_stacked_mp4(frames: list[Image.Image], path: str) -> None:
    tmp = tempfile.mkdtemp(prefix="payrolla-stacked-")
    try:
        for i, f in enumerate(frames):
            stacked(f).save(os.path.join(tmp, f"s_{i:04d}.png"))
        script = os.path.join(tmp, "vse.py")
        with open(script, "w", encoding="utf8") as fh:
            fh.write(VSE_SCRIPT)
        stem = os.path.join(tmp, "out_")
        subprocess.run([BLENDER, "-b", "--factory-startup", "--python", script, "--", tmp, stem],
                       check=True, stdout=subprocess.DEVNULL)
        made = glob.glob(stem + "*.mp4")
        if len(made) != 1:
            raise SystemExit(f"Blender produced {made!r}, expected one .mp4")
        shutil.move(made[0], path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------- icons
def build_icons() -> None:
    """The desktop app's icon set, rasterised from the web app's SVG masters
    by Chromium (Playwright, as capture_ui.py uses). At 32px and below the
    favicon's optical cut is used: its counter slit is opened so the P is
    still a P on a taskbar."""
    from playwright.sync_api import sync_playwright

    if not os.path.isdir(DESKTOP_ICONS):
        raise SystemExit(f"no desktop shell icons at {DESKTOP_ICONS}")
    sizes = sorted(set(ICON_PNGS.values()) | set(ICO_SIZES))
    rendered = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(device_scale_factor=1)
        for size in sizes:
            name = "favicon.svg" if size <= 32 else "payrolla-app-icon.svg"
            with open(os.path.join(WEB_IMG, name), encoding="utf8") as fh:
                uri = "data:image/svg+xml;utf8," + quote(fh.read())
            page.set_viewport_size({"width": size, "height": size})
            page.set_content(f'<body style="margin:0"><img src="{uri}" width="{size}" '
                             f'height="{size}" style="display:block"></body>')
            page.wait_for_function("document.images[0].complete")
            png = page.screenshot(omit_background=True)
            rendered[size] = Image.open(io.BytesIO(png)).convert("RGBA")
        browser.close()
    for filename, size in ICON_PNGS.items():
        rendered[size].save(os.path.join(DESKTOP_ICONS, filename))
    largest = ICO_SIZES[-1]
    rendered[largest].save(os.path.join(DESKTOP_ICONS, "icon.ico"),
                           sizes=[(s, s) for s in ICO_SIZES],
                           append_images=[rendered[s] for s in ICO_SIZES if s != largest])
    print(f"[icons] wrote {len(ICON_PNGS)} PNGs and icon.ico ({', '.join(map(str, ICO_SIZES))}) "
          f"to {os.path.relpath(DESKTOP_ICONS, os.path.dirname(REPO_ROOT))}")


# --------------------------------------------------------------------- report
def report(name: str, rect: dict[str, float], box_px: tuple[int, int]) -> None:
    print(f"[{name}] box {box_px[0]}x{box_px[1]}  aspect {box_px[0] / box_px[1]:.4f}")
    print(f"[{name}]   --mark-left:{rect['left'] * 100:.2f}%; --mark-top:{rect['top'] * 100:.2f}%;"
          f" --mark-width:{rect['width'] * 100:.2f}%; --mark-height:{rect['height'] * 100:.2f}%;")


def build(name: str, spec: dict, skip_render: bool) -> None:
    frames_dir = os.path.join(WORK, name, "frames")
    if not skip_render:
        frames_dir = render(name, spec)
    frames = load_frames(frames_dir)
    box = motion_box(frames)
    rect = mark_rect(frames[-1], box)
    full = crop(frames, box)
    report(name, rect, full[0].size)

    if name == "splash":
        if not os.path.isdir(SPLASH_UI):
            raise SystemExit(f"no desktop shell at {SPLASH_UI}; check out payrolla-desktop beside this repo")
        write_webp(full, os.path.join(SPLASH_UI, "logo-anim.webp"))
        write_still(full[-1], os.path.join(SPLASH_UI, "logo-end.webp"))
        outputs = [os.path.join(SPLASH_UI, n) for n in ("logo-anim.webp", "logo-end.webp")]
    else:
        small = crop(frames, box, spec["small_scale"])
        outputs = []
        for tag, cut in (("lg", full), ("sm", small)):
            mp4 = os.path.join(WEB_IMG, f"payrolla-mark-dark-{tag}.mp4")
            still = os.path.join(WEB_IMG, f"payrolla-mark-dark-{tag}.webp")
            write_stacked_mp4(cut, mp4)
            write_still(cut[-1], still)
            outputs += [mp4, still]
    for p in outputs:
        print(f"[{name}]   wrote {os.path.relpath(p, os.path.dirname(REPO_ROOT))}"
              f"  {os.path.getsize(p) / 1024:.0f} KB")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", choices=sorted(VARIANTS) + ["icons"])
    parser.add_argument("--skip-render", action="store_true",
                        help=f"re-encode the frames already in {WORK}")
    args = parser.parse_args()
    if args.only != "icons" and not os.path.isfile(BLENDER):
        print(f"Blender not found at {BLENDER}; set $BLENDER", file=sys.stderr)
        return 2
    for name, spec in VARIANTS.items():
        if args.only in (None, name):
            build(name, spec, args.skip_render)
    if args.only in (None, "icons"):
        build_icons()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
