"""Admin-only management of versioned statutory rates (SSF split + PAYE bands).

Reps never see these screens — every route is gated to the admin role. Rates
are effective-dated: adding a new version never edits or deletes an old one,
which is what keeps historical payroll runs reproducible.
"""
import json
from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.models import StatutoryRate

statutory_bp = Blueprint("statutory", __name__, url_prefix="/statutory-rates")


@statutory_bp.route("/")
@role_required("admin")
def index():
    rates = StatutoryRate.query.order_by(StatutoryRate.effective_from.desc()).all()
    active = StatutoryRate.active_for(date.today())
    return render_template("statutory_rates.html", rates=rates, active=active)


def _parse_bands(raw):
    bands = json.loads(raw)
    if not isinstance(bands, list) or not bands:
        raise ValueError("Bands must be a non-empty JSON list.")
    cleaned = []
    for band in bands:
        cleaned.append(
            {
                "over": float(band["over"]),
                "rate": float(band["rate"]),
                "base": float(band["base"]),
            }
        )
    # Highest threshold must come first — compute_paye walks top-down.
    return sorted(cleaned, key=lambda b: b["over"], reverse=True)


@statutory_bp.route("/new", methods=["GET", "POST"])
@role_required("admin")
def new():
    latest = StatutoryRate.query.order_by(StatutoryRate.effective_from.desc()).first()
    if request.method == "POST":
        try:
            effective_from = datetime.strptime(
                request.form["effective_from"], "%Y-%m-%d"
            ).date()
            ssf_employee = float(request.form["ssf_employee_rate"])
            ssf_employer = float(request.form["ssf_employer_rate"])
            bands = _parse_bands(request.form["paye_bands_json"])
            overtime_rate_low = float(request.form["overtime_rate_low"])
            overtime_rate_high = float(request.form["overtime_rate_high"])
            overtime_basic_threshold = float(request.form["overtime_basic_threshold"])
            bonus_rate = float(request.form["bonus_rate"])
            bonus_annual_basic_threshold = float(
                request.form["bonus_annual_basic_threshold"]
            )
        except (KeyError, ValueError) as exc:
            flash(f"Invalid rate version: {exc}", "danger")
            return redirect(url_for("statutory.new"))

        if StatutoryRate.query.filter_by(effective_from=effective_from).first():
            flash("A rate version with that effective date already exists.", "warning")
            return redirect(url_for("statutory.new"))
        if not (0 < ssf_employee < 1 and 0 < ssf_employer < 1):
            flash("SSF rates must be fractions, e.g. 0.055 for 5.5%.", "warning")
            return redirect(url_for("statutory.new"))

        rate = StatutoryRate(
            effective_from=effective_from,
            ssf_employee_rate=ssf_employee,
            ssf_employer_rate=ssf_employer,
            paye_bands_json=json.dumps(bands),
            overtime_rate_low=overtime_rate_low,
            overtime_rate_high=overtime_rate_high,
            overtime_basic_threshold=overtime_basic_threshold,
            bonus_rate=bonus_rate,
            bonus_annual_basic_threshold=bonus_annual_basic_threshold,
            notes=request.form.get("notes") or None,
            created_by=current_user.id,
        )
        db.session.add(rate)
        record_audit(
            "Statutory rate version added",
            rate,
            f"Effective {effective_from.isoformat()}: SSF {ssf_employee:.3%}/{ssf_employer:.3%}, "
            f"{len(bands)} PAYE bands.",
        )
        db.session.commit()
        flash("New statutory rate version saved.", "success")
        return redirect(url_for("statutory.index"))

    prefill = {
        "ssf_employee_rate": latest.ssf_employee_rate if latest else 0.055,
        "ssf_employer_rate": latest.ssf_employer_rate if latest else 0.13,
        "paye_bands_json": json.dumps(latest.paye_bands, indent=2) if latest else "[]",
        "overtime_rate_low": latest.overtime_rate_low if latest else 0.05,
        "overtime_rate_high": latest.overtime_rate_high if latest else 0.10,
        "overtime_basic_threshold": latest.overtime_basic_threshold if latest else 0.50,
        "bonus_rate": latest.bonus_rate if latest else 0.05,
        "bonus_annual_basic_threshold": (
            latest.bonus_annual_basic_threshold if latest else 0.15
        ),
    }
    return render_template("statutory_rate_form.html", prefill=prefill)
