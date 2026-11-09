"""Public legal pages: privacy policy, terms of service, data protection.

The wording lives in templates/legal/. LEGAL_UPDATED is the date every page
shows, so move it whenever the wording of any of them changes.
"""
from datetime import date

from flask import Blueprint, render_template

legal_bp = Blueprint("legal", __name__)

LEGAL_UPDATED = date(2026, 11, 9)

# (endpoint, label) in the order the pages link to each other.
LEGAL_PAGES = (
    ("legal.privacy", "Privacy policy"),
    ("legal.terms", "Terms of service"),
    ("legal.data_protection", "Data protection and retention"),
)


def _render(template):
    return render_template(template, updated=LEGAL_UPDATED, legal_pages=LEGAL_PAGES)


@legal_bp.route("/privacy")
def privacy():
    return _render("legal/privacy.html")


@legal_bp.route("/terms")
def terms():
    return _render("legal/terms.html")


@legal_bp.route("/data-protection")
def data_protection():
    return _render("legal/data_protection.html")
