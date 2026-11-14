"""Admin-only management of versioned statutory rates (SSF split + PAYE bands).

Reps never see these screens — every route is gated to STATUTORY_ROLES. Rates
are effective-dated, and a run uses the version in force for its own period.

A version can be edited in place while no approved or processed run depends on
it, because then no figure anyone has signed off can change. Once one does, it
is locked: the page offers "Copy as a new version" instead, which pre-fills the
add form. Approved and processed runs always keep the version they were stamped
with (``statutory_rate_for_run``), and the effective date never changes on
edit, so the desktop's start-up re-seed (which only inserts missing dates)
cannot bring an edited version back.
"""
import json
import math
from collections import defaultdict
from datetime import date, datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.bureau_scope import current_bureau_id
from app.models import ClientCompany, PayrollRun, StatutoryRate, utc_now
from app.payroll_calculations import period_start
from app.payroll_status import CLOSED_STATUSES, REJECTED
from app.permissions import STATUTORY_ROLES

statutory_bp = Blueprint("statutory", __name__, url_prefix="/statutory-rates")


@statutory_bp.route("/")
@role_required(*STATUTORY_ROLES)
def index():
    # Payrolla staff manage the defaults. A bureau sees the defaults (read-only)
    # beside its own versions, which apply to its own clients only.
    bureau_id = current_bureau_id()
    rates = (
        _visible_versions(bureau_id)
        .order_by(StatutoryRate.effective_from.desc(), StatutoryRate.bureau_id.is_(None))
        .all()
    )
    active = StatutoryRate.active_for(date.today(), bureau_id)
    return render_template(
        "statutory_rates.html", rates=rates, active=active, locks=rate_locks(),
        bureau_id=bureau_id,
    )


def _visible_versions(bureau_id):
    """The defaults, plus this bureau's own versions when there is one."""
    query = StatutoryRate.query
    if bureau_id is None:
        return query.filter(StatutoryRate.bureau_id.is_(None))
    return query.filter(db.or_(StatutoryRate.bureau_id.is_(None), StatutoryRate.bureau_id == bureau_id))


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


# --- Which versions are locked --------------------------------------------------


def rate_locks():
    """{rate id: reason} for every version an approved or processed run depends on.

    A run stamped with a version (``PayrollRun.statutory_rate_id``) depends on
    that one. Runs calculated before the stamp existed (NULL) depend on the
    version that was in force for their period, so those are counted too.
    """
    versions = StatutoryRate.query.all()
    bureau_of = dict(db.session.query(ClientCompany.id, ClientCompany.bureau_id).all())
    counts = defaultdict(int)
    closed = PayrollRun.query.filter(PayrollRun.status.in_(CLOSED_STATUSES)).with_entities(
        PayrollRun.month, PayrollRun.year, PayrollRun.statutory_rate_id,
        PayrollRun.client_company_id,
    )
    for month, year, stamped, company_id in closed:
        if stamped:
            counts[stamped] += 1
            continue
        try:
            start = period_start(month, year)
        except (TypeError, ValueError):
            continue
        in_force = _in_force(versions, start, bureau_of.get(company_id))
        if in_force is not None:
            counts[in_force.id] += 1
    return {
        rate_id: (
            f"{n} approved or processed payroll run{'' if n == 1 else 's'} "
            f"{'uses' if n == 1 else 'use'} this version, so it can no longer be changed"
        )
        for rate_id, n in counts.items()
    }


def _in_force(versions, on_date, bureau_id):
    """StatutoryRate.active_for, over versions already loaded."""
    candidates = [
        v for v in versions
        if v.effective_from <= on_date and (v.bureau_id is None or v.bureau_id == bureau_id)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda v: (v.effective_from, v.bureau_id is not None))


def open_runs_using(rate):
    """Runs not yet approved that were last calculated with ``rate``."""
    return (
        PayrollRun.query.filter(
            PayrollRun.statutory_rate_id == rate.id,
            ~PayrollRun.status.in_(CLOSED_STATUSES + (REJECTED,)),
        )
        .order_by(PayrollRun.id)
        .all()
    )


# --- Reading the form -------------------------------------------------------------

# Labels for the audit trail when a version is edited.
RATE_FIELD_LABELS = {
    "ssf_employee_rate": "SSF employee rate",
    "ssf_employer_rate": "SSF employer rate",
    "overtime_rate_low": "Overtime rate (within threshold)",
    "overtime_rate_high": "Overtime rate (excess)",
    "overtime_basic_threshold": "Overtime threshold",
    "bonus_rate": "Bonus concession rate",
    "bonus_annual_basic_threshold": "Bonus threshold",
    "overtime_junior_monthly_threshold": "OT junior-staff threshold",
    "icu_member_rate": "ICU union dues",
    "ssnit_limits_year": "SSNIT notice year",
    "ssnit_min_insurable": "Minimum insurable earnings",
    "ssnit_max_insurable": "Maximum insurable earnings",
    "ssnit_min_monthly_remittance": "Minimum monthly SSNIT payment",
    "provident_fund_tax_limit": "Provident-fund tax limit",
    "overtime_qualifying_annual_limit": "Overtime qualifying annual limit",
    "paye_bands_json": "PAYE bands",
    "notes": "Notes",
}
_DEDUCTION_DEFAULTS = (
    ("ssnit_min_insurable", 587.80), ("ssnit_max_insurable", 69000),
    ("ssnit_min_monthly_remittance", 79.40),
    ("provident_fund_tax_limit", 0.165),
    ("overtime_qualifying_annual_limit", 18000),
)


def _values_from_form(form, fallback):
    """Every field of a version except its effective date, from ``form``.

    ``fallback`` supplies a deduction limit the form left blank (the latest
    version when adding, the version itself when editing). Raises KeyError for
    a missing required field and ValueError for a bad one.
    """
    ssf_employee = float(form["ssf_employee_rate"])
    ssf_employer = float(form["ssf_employer_rate"])
    if form.getlist("band_over"):
        bands = bands_from_rows(_rows_from_form(form))
    else:
        bands = _parse_bands(form["paye_bands_json"])
    values = {
        "ssf_employee_rate": ssf_employee,
        "ssf_employer_rate": ssf_employer,
        "paye_bands_json": json.dumps(bands),
        "overtime_rate_low": float(form["overtime_rate_low"]),
        "overtime_rate_high": float(form["overtime_rate_high"]),
        "overtime_basic_threshold": float(form["overtime_basic_threshold"]),
        "bonus_rate": float(form["bonus_rate"]),
        "bonus_annual_basic_threshold": float(form["bonus_annual_basic_threshold"]),
        "overtime_junior_monthly_threshold": float(
            form.get("overtime_junior_monthly_threshold") or 1500
        ),
        "icu_member_rate": float(form.get("icu_member_rate") or 0.03),
        "notes": form.get("notes") or None,
    }
    # New versions carry explicit, sourced deduction limits. The old
    # basic-pay warning remains only on historical policy versions.
    deduction_config = {
        name: float(form.get(name) or getattr(fallback, name, default) or default)
        for name, default in _DEDUCTION_DEFAULTS
    }
    deduction_config["ssnit_limits_year"] = int(
        form.get("ssnit_limits_year") or getattr(fallback, "ssnit_limits_year", None) or 2026
    )
    if any(not math.isfinite(value) or value <= 0 for value in deduction_config.values()):
        raise ValueError("Deduction limits must be finite positive values.")
    if deduction_config["ssnit_min_insurable"] > deduction_config["ssnit_max_insurable"]:
        raise ValueError("The minimum pensionable earnings cannot exceed the maximum.")
    if deduction_config["provident_fund_tax_limit"] > 1:
        raise ValueError("Provident-fund tax limit must be a fraction no greater than 1.")
    if not 2000 <= deduction_config["ssnit_limits_year"] <= 2100:
        raise ValueError("Enter the year covered by the verified SSNIT notice.")
    if not (0 < ssf_employee < 1 and 0 < ssf_employer < 1):
        raise ValueError("SSF rates must be fractions, e.g. 0.055 for 5.5%.")
    values.update(deduction_config)
    return values


def _prefill_from(rate):
    """Form values taken from ``rate``, or the built-in defaults when there is none."""
    defaults = {
        "ssf_employee_rate": 0.055, "ssf_employer_rate": 0.13,
        "overtime_rate_low": 0.05, "overtime_rate_high": 0.10,
        "overtime_basic_threshold": 0.50, "bonus_rate": 0.05,
        "bonus_annual_basic_threshold": 0.15, "overtime_junior_monthly_threshold": 1500.0,
        "icu_member_rate": 0.03, "ssnit_limits_year": 2026,
        **dict(_DEDUCTION_DEFAULTS),
    }
    prefill = {"effective_from": "", "notes": ""}
    for name, default in defaults.items():
        prefill[name] = getattr(rate, name, default) if rate else default
    return prefill


def _render_form(mode, prefill, band_rows, rate=None, form_error=None, status=200):
    if mode == "edit":
        heading = f"Edit Rate Version · effective {rate.effective_from.strftime('%d %b %Y')}"
    elif mode == "copy":
        heading = "New Rate Version (copied)"
    else:
        heading = "Add Statutory Rate Version"
    if current_bureau_id() is not None:
        heading += " · for your clients"
    return render_template(
        "statutory_rate_form.html", mode=mode, rate=rate, prefill=prefill,
        band_rows=band_rows, form_error=form_error, heading=heading,
    ), status


def _rerender(error, mode="new", rate=None):
    """Show the form again with everything the operator typed, and the reason.

    It used to flash and redirect, which threw away a dozen fields and the
    bands because one of them was wrong."""
    prefill = {key: request.form.get(key, "") for key in request.form.keys()
               if key not in ("band_over", "band_rate", "csrf_token")}
    if mode == "edit" and rate is not None:
        prefill["effective_from"] = rate.effective_from.isoformat()
    band_rows = [
        {"over": over, "rate": percent, "base": ""}
        for over, percent in zip(request.form.getlist("band_over"), request.form.getlist("band_rate"))
    ] or [{"over": "", "rate": "", "base": ""}]
    return _render_form(mode, prefill, band_rows, rate=rate, form_error=error, status=400)


# --- Routes -----------------------------------------------------------------------


@statutory_bp.route("/new", methods=["GET", "POST"])
@role_required(*STATUTORY_ROLES)
def new():
    bureau_id = current_bureau_id()
    # Pre-filled from the newest version this user works from: for a bureau,
    # its own latest version or the newest default, whichever starts later.
    latest = (
        _visible_versions(bureau_id)
        .order_by(StatutoryRate.effective_from.desc(), StatutoryRate.bureau_id.is_(None))
        .first()
    )
    if request.method == "POST":
        try:
            effective_from = datetime.strptime(
                request.form["effective_from"], "%Y-%m-%d"
            ).date()
            values = _values_from_form(request.form, latest)
        except KeyError as exc:
            return _rerender(f"{exc.args[0].replace('_', ' ')} is missing.")
        except ValueError as exc:
            return _rerender(str(exc))

        if StatutoryRate.query.filter_by(effective_from=effective_from, bureau_id=bureau_id).first():
            return _rerender("A rate version with that effective date already exists.")

        rate = StatutoryRate(
            effective_from=effective_from,
            # A bureau's version is its own: taken from the signed-in user, never the form.
            bureau_id=bureau_id,
            deductions_policy_version=1 if effective_from >= date(2026, 9, 1) else 0,
            created_by=current_user.id,
            **values,
        )
        db.session.add(rate)
        record_audit(
            "Statutory rate version added",
            rate,
            f"Effective {effective_from.isoformat()}: SSF "
            f"{values['ssf_employee_rate']:.3%}/{values['ssf_employer_rate']:.3%}, "
            f"{len(rate.paye_bands)} PAYE bands.",
        )
        db.session.commit()
        flash("New statutory rate version saved.", "success")
        return redirect(url_for("statutory.index"))

    band_rows = (
        _band_rows_for_display(latest.paye_bands) if latest
        else [{"over": "", "rate": "", "base": 0}]
    )
    return _render_form("new", _prefill_from(latest), band_rows)


def _visible_version_or_404(rate_id):
    """A version this user may read: a default, or their own bureau's."""
    rate = db.session.get(StatutoryRate, rate_id)
    if rate is None or (rate.bureau_id is not None and rate.bureau_id != current_bureau_id()):
        abort(404)
    return rate


def _editable_version_or_404(rate_id):
    """A version this user may change: Payrolla staff the defaults, a bureau
    only its own. Nobody edits another bureau's version, and a bureau never
    edits the defaults everyone else relies on."""
    rate = db.session.get(StatutoryRate, rate_id)
    if rate is None or rate.bureau_id != current_bureau_id():
        abort(404)
    return rate


@statutory_bp.route("/<int:rate_id>/copy")
@role_required(*STATUTORY_ROLES)
def copy(rate_id):
    """The add form, pre-filled from ``rate_id``: how a locked version changes."""
    source = _visible_version_or_404(rate_id)
    prefill = _prefill_from(source)
    prefill["notes"] = (
        f"Copied from the version effective {source.effective_from.strftime('%d %b %Y')}."
    )
    return _render_form("copy", prefill, _band_rows_for_display(source.paye_bands), rate=source)


def _display(name, value):
    """A field's value as the audit trail shows it."""
    if name == "paye_bands_json":
        return "; ".join(
            f"from {b['over']:,.2f} at {b['rate'] * 100:g}%"
            for b in sorted(json.loads(value or "[]"), key=lambda b: b["over"])
        )
    if value in (None, ""):
        return "blank"
    return f"{value:g}" if isinstance(value, float) else str(value)


@statutory_bp.route("/<int:rate_id>/edit", methods=["GET", "POST"])
@role_required(*STATUTORY_ROLES)
def edit(rate_id):
    rate = _editable_version_or_404(rate_id)
    lock = rate_locks().get(rate.id)
    if lock:
        flash(f"{lock}. Copy it as a new version instead.", "warning")
        return redirect(url_for("statutory.index"))

    if request.method == "POST":
        try:
            values = _values_from_form(request.form, rate)
        except KeyError as exc:
            return _rerender(f"{exc.args[0].replace('_', ' ')} is missing.", "edit", rate)
        except ValueError as exc:
            return _rerender(str(exc), "edit", rate)

        changes = []
        for name, new_value in values.items():
            old_value = getattr(rate, name)
            if name == "paye_bands_json":
                same = json.loads(old_value or "[]") == json.loads(new_value)
            else:
                same = old_value == new_value
            if not same:
                changes.append(
                    f"{RATE_FIELD_LABELS.get(name, name)}: "
                    f"{_display(name, old_value)} -> {_display(name, new_value)}"
                )
                setattr(rate, name, new_value)
        if not changes:
            flash("Nothing changed.", "info")
            return redirect(url_for("statutory.index"))

        rate.updated_at = utc_now()
        rate.updated_by = current_user.id
        record_audit(
            "Statutory rate version edited",
            rate,
            f"Effective {rate.effective_from.isoformat()}. " + " | ".join(changes),
        )
        db.session.commit()
        flash(_saved_message(open_runs_using(rate)), "success")
        return redirect(url_for("statutory.index"))

    prefill = _prefill_from(rate)
    prefill["effective_from"] = rate.effective_from.isoformat()
    prefill["notes"] = rate.notes or ""
    return _render_form("edit", prefill, _band_rows_for_display(rate.paye_bands), rate=rate)


def _saved_message(open_runs):
    message = "Rate version saved."
    if not open_runs:
        return message
    count = len(open_runs)
    names = ", ".join(f"{run.month} {run.year}" for run in open_runs[:5])
    more = f" and {count - 5} more" if count > 5 else ""
    return (
        f"{message} {count} payroll run{'' if count == 1 else 's'} not yet approved "
        f"{'uses' if count == 1 else 'use'} it ({names}{more}). "
        "Recalculate them to apply the new rates."
    )
