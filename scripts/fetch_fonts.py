"""Install original Satoshi web fonts for self-hosting in this application."""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.request import Request, urlopen

FONT_DIR = Path(__file__).resolve().parents[1] / "app" / "static" / "fonts"
FONTS = (
    (400, "TTX2Z3BF3P6Y5BQT3IV2VNOK6FL22KUT/7QYRJOI3JIMYHGY6CH7SOIFRQLZOLNJ6/KFIAZD4RUMEZIYV6FQ3T3GP5PDBDB6JY",
     "50dca57f0b77918e0fb7dac998c3f5ef6b0c2a29657da97658a04f98ac532fc5"),
    (500, "P2LQKHE6KA6ZP4AAGN72KDWMHH6ZH3TA/ZC32TK2P7FPS5GFTL46EU6KQJA24ZYDB/7AHDUZ4A7LFLVFUIFSARGIWCRQJHISQP",
     "af02a72246f53ad49c44a591921edbd39ec8258a03d8cc2e0532aa1e497e85b4"),
    (700, "LAFFD4SDUCDVQEXFPDC7C53EQ4ZELWQI/PXCT3G6LO6ICM5I3NTYENYPWJAECAWDD/GHM6WVH6MILNYOOCXHXB5GTSGNTMGXZR",
     "353a7fbfb4475f0c31470a7449226006cb64211c71055ca9db860a8acdaa9f68"),
    (900, "NHPGVFYUXYXE33DZ75OIT4JFGHITX5PE/PSUTMASCDJTVPERDYJZPN23BVUFUCQIF/J64QX5IPOHK56I2KYUNBQ5M2XWZEYKYX",
     "bd11b5820231420e78046c611aebdd628dc17ad67788258ffe3fe902253efd3b"),
)


def main() -> None:
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    for weight, source, expected in FONTS:
        target = FONT_DIR / f"satoshi-{weight}.woff2"
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            print(f"Verified {target.name}")
            continue
        request = Request(f"https://cdn.fontshare.com/wf/{source}.woff2",
                          headers={"User-Agent": "Payrolla asset build"})
        with urlopen(request, timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError(f"Unexpected font contents: {target.name}; original asset required")
        temporary = target.with_suffix(".download")
        temporary.write_bytes(data)
        temporary.replace(target)
        print(f"Installed {target.name}")


if __name__ == "__main__":
    main()
