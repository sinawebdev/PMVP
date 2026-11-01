"""Admin-only management of versioned statutory rates (SSF split + PAYE bands).

Reps never see these screens — every route is gated to the admin role. Rates
are effective-dated: adding a new version never edits or deletes an old one,
which is what keeps historical payroll runs reproducible.
"""
import json
import math
from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.permissions import STATUTORY_ROLES
from app.models import StatutoryRate

statutory_bp = Blueprint("statutory", __name__, url_prefix="/statutory-rates")


@statutory_bp.route("/")
@role_required(*STATUTORY_ROLES)
def index():
    rates = StatutoryRate.query.order_by(StatutoryRate.effective_from.desc()).all()
    active = StatutoryRate.active_for(date.today())
    return render_template("statutory_rates.html", rates=rates, active=active)


def _parse_bands(raw):
    """PAYE bands from the legacy JSON field. Kept so a scripted client posting
    paye_bands_json still works; the form itself now posts band rows."""
    try:
        bands = json.loads(raw)
    except ValueError:
        raise ValueError("The PAYE bands are not valid JSON.")
    if not isinstance(bands, list) or not bands:
        raise ValueError("Bands must be a non-empty JSON list.")
    try:
        rows = [(float(band["over"]), float(band["rate"]) * 100) for band in bands]
    except (KeyError, TypeError, ValueError):
        raise ValueError('Each band needs numeric "over" and "rate" values.')
    return bands_from_rows(rows)


def bands_from_rows(rows):
    """PAYE bands from (threshold, rate in percent) pairs, in any order.

    ``base`` (the tax on all income below a band's threshold) is worked out
    here rather than typed: each band's base is the previous base plus the
    previous band's width taxed at the previous rate. Asking people to type it
    is how the stored bands and the tax tables would drift apart. Returns the
    bands highest threshold first, the order compute_paye walks."""
    if not rows:
        raise ValueError("Add at least one PAYE band.")
    cleaned = []
    for over, rate in rows:
        if not (math.isfinite(over) and math.isfinite(rate)):
            raise ValueError("Every band needs a number in both columns.")
        if over < 0:
            raise ValueError("A band cannot start below GH₵ 0.")
        if not 0 <= rate <= 100:
            raise ValueError("A band's rate must be between 0% and 100%.")
        cleaned.append((over, rate / 100))
    cleaned.sort()
    thresholds = [over for over, _ in cleaned]
    if len(set(thresholds)) != len(thresholds):
        raise ValueError("Two bands start at the same income. Each band needs its own starting amount.")
    bands, base = [], 0.0
    for index, (over, rate) in enumerate(cleaned):
        if index:
            prev_over, prev_rate = cleaned[index - 1]
            base += (over - prev_over) * prev_rate
        bands.append({"over": over, "rate": round(rate, 6), "base": round(base, 2)})
    return sorted(bands, key=lambda b: b["over"], reverse=True)


def _rows_from_form(form):
    """(threshold, percent) pairs from the band editor, skipping fully blank rows."""
    rows = []
    for over, rate in zip(form.getlist("band_over"), form.getlist("band_rate")):
        over, rate = (over or "").strip(), (rate or "").strip()
        if not over and not rate:
            continue
        try:
            rows.append((float(over), float(rate)))
        except ValueError:
            raise ValueError("Every band needs a number in both columns.")
    return rows


def _band_rows_for_display(bands):
    """Bands lowest first, rate as a percent, for the editor."""
    return [
        {"over": b["over"], "rate": round(b["rate"] * 100, 4), "base": b["base"]}
        for b in sorted(bands, key=lambda b: b["over"])
    ]


@statutory_bp.route("/new", methods=["GET", "POST"])
@role_required(*STATUTORY_ROLES)
def new():
    latest = StatutoryRate.query.order_by(StatutoryRate.effective_from.desc()).first()
    if request.method == "POST":
        try:
            effective_from = datetime.strptime(
                request.form["effective_from"], "%Y-%m-%d"
            ).date()
            ssf_employee = float(request.form["ssf_employee_rate"])
            ssf_employer = float(request.form["ssf_employer_rate"])
            if request.form.getlist("band_over"):
                bands = bands_from_rows(_rows_from_form(request.form))
            else:
                bands = _parse_bands(request.form["paye_bands_json"])
            overtime_rate_low = float(request.form["overtime_rate_low"])
            overtime_rate_high = float(request.form["overtime_rate_high"])
            overtime_basic_threshold = float(request.form["overtime_basic_threshold"])
            bonus_rate = float(request.form["bonus_rate"])
            bonus_annual_basic_threshold = float(
                request.form["bonus_annual_basic_threshold"]
            )
            overtime_junior_monthly_threshold = float(
                request.form.get("overtime_junior_monthly_threshold") or 1500
            )
            icu_member_rate = float(request.form.get("icu_member_rate") or 0.03)
            # New versions carry explicit, sourced deduction limits. The old
            # basic-pay warning remains only on historical policy versions.
            deduction_config = {
                name: float(request.form.get(name) or getattr(latest, name, default) or default)
                for name, default in (
                    ("ssnit_min_insurable", 587.80), ("ssnit_max_insurable", 69000),
                    ("ssnit_min_monthly_remittance", 79.40),
                    ("provident_fund_tax_limit", 0.165),
                    ("overtime_qualifying_annual_limit", 18000),
                )
            }
            deduction_config["ssnit_limits_year"] = int(
                request.form.get("ssnit_limits_year") or getattr(latest, "ssnit_limits_year", None) or 2026
            )
            if any(not math.isfinite(value) or value <= 0 for value in deduction_config.values()):
                raise ValueError("Deduction limits must be finite positive values.")
            if deduction_config["ssnit_min_insurable"] > deduction_config["ssnit_max_insurable"]:
                raise ValueError("The minimum pensionable earnings cannot exceed the maximum.")
            if deduction_config["provident_fund_tax_limit"] > 1:
                raise ValueError("Provident-fund tax limit must be a fraction no greater than 1.")
            if not 2000 <= deduction_config["ssnit_limits_year"] <= 2100:
                raise ValueError("Enter the year covered by the verified SSNIT notice.")
        except KeyError as exc:
            return _rerender(f"{exc.args[0].replace('_', ' ')} is missing.")
        except ValueError as exc:
            return _rerender(str(exc))

        if StatutoryRate.query.filter_by(effective_from=effective_from).first():
            return _rerender("A rate version with that effective date already exists.")
        if not (0 < ssf_employee < 1 and 0 < ssf_employer < 1):
            return _rerender("SSF rates must be fractions, e.g. 0.055 for 5.5%.")

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
            overtime_junior_monthly_threshold=overtime_junior_monthly_threshold,
            icu_member_rate=icu_member_rate,
            deductions_policy_version=1 if effective_from >= date(2026, 9, 1) else 0,
            **deduction_config,
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
        "effective_from": "",
        "notes": "",
        "ssf_employee_rate": latest.ssf_employee_rate if latest else 0.055,
        "ssf_employer_rate": latest.ssf_employer_rate if latest else 0.13,
        "overtime_rate_low": latest.overtime_rate_low if latest else 0.05,
        "overtime_rate_high": latest.overtime_rate_high if latest else 0.10,
        "overtime_basic_threshold": latest.overtime_basic_threshold if latest else 0.50,
        "bonus_rate": latest.bonus_rate if latest else 0.05,
        "bonus_annual_basic_threshold": (
            latest.bonus_annual_basic_threshold if latest else 0.15
        ),
        "overtime_junior_monthly_threshold": (
            latest.overtime_junior_monthly_threshold if latest else 1500.0
        ),
        "icu_member_rate": latest.icu_member_rate if latest else 0.03,
    }
    for name, default in (
        ("ssnit_min_insurable", 587.80), ("ssnit_max_insurable", 69000),
        ("ssnit_min_monthly_remittance", 79.40), ("ssnit_limits_year", 2026),
        ("provident_fund_tax_limit", 0.165), ("overtime_qualifying_annual_limit", 18000),
    ):
        prefill[name] = getattr(latest, name, default) if latest else default
    band_rows = _band_rows_for_display(latest.paye_bands) if latest else [{"over": "", "rate": "", "base": 0}]
    return render_template("statutory_rate_form.html", prefill=prefill, band_rows=band_rows)


def _rerender(error):
    """Show the form again with everything the operator typed, and the reason.

    It used to flash and redirect, which threw away a dozen fields and the
    bands because one of them was wrong."""
    prefill = {key: request.form.get(key, "") for key in request.form.keys()
               if key not in ("band_over", "band_rate", "csrf_token")}
    band_rows = [
        {"over": over, "rate": rate, "base": ""}
        for over, rate in zip(request.form.getlist("band_over"), request.form.getlist("band_rate"))
    ] or [{"over": "", "rate": "", "base": ""}]
    return render_template("statutory_rate_form.html", prefill=prefill, band_rows=band_rows,
                           form_error=error), 400
