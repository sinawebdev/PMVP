"""The latest desktop release, as JSON the desktop app checks for updates.

Installed copies of Payrolla Desktop ask ``/desktop/latest.json`` (at most twice
a day) whether a newer version exists, and show a notice with a download
button when one does. They send nothing but the request itself; the version
comparison happens on the desktop.

Everything here comes from configuration set on each release, next to the
existing DESKTOP_DOWNLOAD_URL:

- DESKTOP_LATEST_VERSION   e.g. 0.2.0 (major.minor.patch)
- DESKTOP_DOWNLOAD_SHA256  the installer's SHA-256, 64 hex characters (optional)
- DESKTOP_RELEASED_ON      YYYY-MM-DD (optional)
- DESKTOP_RELEASE_NOTES    one short line shown in the notice (optional)

While either the version or the download URL is unset, the route answers 404,
and installed copies treat that as "no update".
"""
import re
from datetime import date

from flask import Blueprint, abort, current_app, jsonify

desktop_release_bp = Blueprint("desktop_release", __name__, url_prefix="/desktop")

_VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
NOTES_MAX = 200


def release_config(environ):
    """Validated release settings from ``environ``; bad values become None."""
    version = (environ.get("DESKTOP_LATEST_VERSION") or "").strip()
    sha = (environ.get("DESKTOP_DOWNLOAD_SHA256") or "").strip().lower()
    released = (environ.get("DESKTOP_RELEASED_ON") or "").strip()
    notes = " ".join((environ.get("DESKTOP_RELEASE_NOTES") or "").split())[:NOTES_MAX]
    try:
        released = date.fromisoformat(released).isoformat() if released else None
    except ValueError:
        released = None
    return {
        "DESKTOP_LATEST_VERSION": version if _VERSION.match(version) else None,
        "DESKTOP_DOWNLOAD_SHA256": sha if _SHA256.match(sha) else None,
        "DESKTOP_RELEASED_ON": released,
        "DESKTOP_RELEASE_NOTES": notes or None,
    }


@desktop_release_bp.route("/latest.json")
def latest():
    config = current_app.config
    version = config.get("DESKTOP_LATEST_VERSION")
    url = config.get("DESKTOP_DOWNLOAD_URL")
    if not version or not url:
        abort(404)
    response = jsonify(
        version=version,
        download_url=url,
        sha256=config.get("DESKTOP_DOWNLOAD_SHA256"),
        released_on=config.get("DESKTOP_RELEASED_ON"),
        notes=config.get("DESKTOP_RELEASE_NOTES"),
    )
    response.headers["Cache-Control"] = "public, max-age=300"
    return response
