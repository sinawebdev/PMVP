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

## Newsreader display face

Editorial headings pair Satoshi with the self-hosted Newsreader variable font.
The original font is from the Google Fonts Newsreader project
(https://github.com/google/fonts/tree/main/ofl/newsreader), under the included
SIL Open Font License in `Newsreader-OFL.txt`. The TTF and license are kept in
source so deployment and desktop packaging include the same face.

All fonts use an optional display period and are preloaded by both application
shells. Operational labels and tabular figures stay in Satoshi.

## Montserrat (landing page headings)

The public landing page sets its hero and section headings in Montserrat. The
files are the variable-weight latin and latin-ext subsets from Fontsource
(`@fontsource-variable/montserrat@5.3.0`, `files/montserrat-*-wght-normal.woff2`),
renamed to `montserrat-latin.woff2` and `montserrat-latin-ext.woff2`. They are
under the SIL Open Font License in `Montserrat-OFL.txt`, so unlike Satoshi they
are kept in source (`.gitignore` makes an exception for them) and need no build
step. Unlike the others, Montserrat uses `font-display: swap`, and the landing
page preloads the latin file.

SHA-256: latin `06b16db7a969135d48d38c49183be7fb88d4452e2a3011957c7851941f4e4879`,
latin-ext `54d9a78b7ff60b689ad9f3017ffac8547b5d871afec733f6c1c3ae36577ee504`.
