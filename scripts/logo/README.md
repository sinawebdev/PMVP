# Payrolla logo assets

`build_brand_assets.py` is the source for the rounded ribbon P and Satoshi Bold
wordmark, following the supplied Payrolla brand board, including its
single-storey a and custom straight y tail. All light, dark,
monochrome, app and favicon variants share its contours. Horizontal logos use
outlined lettering so their shape does not depend on font loading.

Install development tooling and fetch the licensed font files:

```powershell
python -m pip install Pillow numpy playwright fonttools brotli
python -m playwright install chromium
python scripts/fetch_fonts.py
python scripts/logo/build_brand_assets.py --ffmpeg C:/tools/ffmpeg.exe
```

Use an FFmpeg build with libx264. `--only vectors`, `--only icons` and
`--only motion` can rebuild individual outputs. The desktop shell must be in
the sibling `payrolla-desktop` checkout. After building, use the desktop
`scripts/sync_upstream.py` workflow to mirror the web source into its vendor.

Motion uses the same vector shapes, assembling once for the desktop splash and
providing short keyframe intervals for the landing page's existing native
scroll player. Still images show the exact resolved master for reduced motion.
The background scroll sequence is built separately by `build_scroll_assets.py`:
the ribbon turns in perspective, the stem folds about its attachment, and a
broad reflection follows the movement. A shallow teal edge gives it depth,
then settles onto the original flat master. The player adds a small camera
drift over the hero and first workflow section, following scroll both ways.

To rebuild only this background effect:

```powershell
python scripts/logo/build_scroll_assets.py --ffmpeg C:/tools/ffmpeg.exe
```

The dark variant uses a white stem and wordmark; the browser favicon uses the
white tile shown in the reference, while Windows uses the dark app tile.

`build_logo_assets.py` retains image encoding helpers and delegates its CLI to
the current master. The Blender scene in `payrolla_logo_anim.py` and
`logo_parts.py` describes the previous exploratory mark; it is not used for
shipping assets.
