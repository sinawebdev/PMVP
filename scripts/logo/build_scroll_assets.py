"""Encode the approved mark for scroll seeking without changing its frames.

Development only; FFmpeg is not a runtime dependency. Pass a local FFmpeg binary
with --ffmpeg. The original once-and-hold/splash assets are never overwritten.
"""
from pathlib import Path
import argparse
import subprocess

ROOT = Path(__file__).resolve().parents[2]
IMAGES = ROOT / 'app' / 'static' / 'img'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    args = parser.parse_args()
    original = IMAGES / 'payrolla-mark-dark-lg.mp4'
    for size, scale in (('lg', None), ('sm', 'scale=trunc(iw*0.75/2)*2:trunc(ih*0.75/2)*2')):
        output = IMAGES / f'payrolla-mark-scroll-{size}.mp4'
        command = [args.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(original)]
        if scale:
            command += ['-vf', scale]
        command += [
            '-an', '-c:v', 'libx264', '-crf', '19', '-preset', 'slow',
            '-pix_fmt', 'yuv420p', '-g', '4', '-keyint_min', '4',
            '-sc_threshold', '0', '-bf', '0', '-movflags', '+faststart', str(output),
        ]
        subprocess.run(command, check=True)
        print(f'{output.name}: {output.stat().st_size:,} bytes', flush=True)


if __name__ == '__main__':
    main()
