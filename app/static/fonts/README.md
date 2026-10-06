# Satoshi web fonts

Run `python scripts/fetch_fonts.py` before serving or packaging the app. Docker
and the Render Blueprint run this during their build. A manually configured
Render service must add it to its build command as well.

The script downloads the four original, unmodified WOFF2 assets from Fontshare,
checks their SHA-256 hashes and stores them here. Pages serve them from the
application itself; visitors and the desktop app need no Fontshare connection.
Font binaries are build assets, excluded from the public source repository.

See `FONT-LICENSE.txt` for the ITF Free Font License. Source:
https://www.fontshare.com/fonts/satoshi
