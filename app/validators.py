from sqlalchemy import or_

from app.excel_utils import looks_like_header_label, normalize_label, normalize_worker
from app.models import Employee, PayrollItem, PayrollRun
from app.payroll_status import CLOSED_STATUSES


def client_name_matches(selected_name, detected_name):
    if not detected_name:
        return True
    selected = normalize_label(selected_name)
    detected = normalize_label(detected_name)
    return selected in detected or detected in selected


def build_worker_key(row):
    staff_id = normalize_worker(row.get("staff_id"))
    full_name = normalize_worker(row.get("full_name"))
    if staff_id:
        return f"staff:{staff_id}"
    if full_name:
        return f"name:{full_name}"
    return ""


def collect_blocking_errors(mapped_rows, detected_company_name=""):
    """Spec §8 hard-stops: conditions under which the confirm must refuse to
    create the run, not warn-and-proceed. Everything here signals a corrupted
    upload (the wrong header row / data-shifted columns of production run 9),
    not a data-quality nit."""
    errors = []

    # A header-label company name ALONE is not proof of a shifted import —
    # detect_company_name can grab a header cell off a perfectly aligned
    # sheet (the acs 1.xlsx case), and that stays a warning. Block only when
    # the row data corroborates the shift: worker names that are themselves
    # column headings or bare numbers, the run-9 signature.
    if detected_company_name and looks_like_header_label(detected_company_name):
        shifted = [
            row
            for row in mapped_rows
            if build_worker_key(row)
            and looks_like_header_label(
                str(row.get("full_name") or ""), numeric_is_suspicious=True
            )
        ]
        if shifted:
            errors.append(
                f'The detected company name "{detected_company_name}" matches a '
                f"spreadsheet column heading and {len(shifted)} worker name(s) "
                "also read like headings or bare numbers — the wrong header row "
                "was almost certainly picked and the mapped data is shifted. "
                "Fix the sheet and re-upload."
            )

    zero_basic = []
    for row in mapped_rows:
        if not build_worker_key(row):
            continue  # blank rows are dropped before persistence, not blocked
        status = normalize_label(row.get("status"))
        if "inactive" in status or "terminated" in status:
            continue
        if float(row.get("basic_salary") or 0) <= 0:
            zero_basic.append(
                str(row.get("staff_id") or row.get("full_name") or "?").strip()
            )
    if zero_basic:
        shown = ", ".join(zero_basic[:10])
        more = f" (+{len(zero_basic) - 10} more)" if len(zero_basic) > 10 else ""
        errors.append(
            f"{len(zero_basic)} active worker(s) have no basic salary: {shown}{more}. "
            "Every statutory figure is derived from basic salary, so a zero "
            "basic zeroes the whole row — fix the sheet before importing."
        )

    return errors


def validate_payroll_rows(
    mapped_rows,
    client_company,
    month,
    year,
    detected_company_name="",
    current_run_id=None,
):
    seen_keys = set()
    duplicate_keys = set()
    for row in mapped_rows:
        key = build_worker_key(row)
        if not key:
            continue
        if key in seen_keys:
            duplicate_keys.add(key)
        seen_keys.add(key)

    warnings = []
    per_row_warnings = {}

    existing_run = PayrollRun.query.filter(
        PayrollRun.client_company_id == client_company.id,
        PayrollRun.month == month,
        PayrollRun.year == int(year),
    )
    if current_run_id:
        existing_run = existing_run.filter(PayrollRun.id != current_run_id)
    if existing_run.first():
        warnings.append(
            f"Payroll already exists for {client_company.name} in {month} {year}."
        )

    # Company detection is retired: the company is authoritative from the
    # client the operator selected (single) or the sheet/group matched to a
    # ClientCompany record (multi), so `detected_company_name` now carries that
    # known client name rather than a free-text guess off the workbook. The old
    # "Excel appears to mention X" mismatch warning and the "looks like a
    # spreadsheet column heading" guard were consequences of that retired scan
    # (they produced the false "GH CARD") and have been removed. The per-row
    # name guard in validate_single_row still flags genuinely data-shifted rows,
    # and collect_blocking_errors still hard-stops a corroborated shift.
    # See PMVP_INVESTIGATION_02_COMPANY_ARCHITECTURE.md.

    # One query each instead of one per row — the per-row versions of these
    # lookups were the bulk of the confirm request's DB round trips and pushed
    # large confirms past the gunicorn worker timeout.
    file_staff_ids = {
        str(row.get("staff_id") or "").strip()
        for row in mapped_rows
        if str(row.get("staff_id") or "").strip()
    }
    file_full_names = {
        str(row.get("full_name") or "").strip()
        for row in mapped_rows
        if str(row.get("full_name") or "").strip()
    }
    cross_client_by_staff_id = {}
    cross_client_by_name = {}
    if file_staff_ids or file_full_names:
        identity_filters = []
        if file_staff_ids:
            identity_filters.append(PayrollItem.staff_id.in_(file_staff_ids))
        if file_full_names:
            identity_filters.append(PayrollItem.full_name.in_(file_full_names))
        cross_client_items = (
            PayrollItem.query.join(PayrollRun)
            .filter(
                PayrollRun.month == month,
                PayrollRun.year == int(year),
                PayrollRun.client_company_id != client_company.id,
            )
            .filter(or_(*identity_filters))
            .all()
        )
        for item in cross_client_items:
            other = item.payroll_run.client_company
            other_name = other.name if other else "another client"
            if item.staff_id:
                cross_client_by_staff_id.setdefault(item.staff_id, other_name)
            if item.full_name:
                cross_client_by_name.setdefault(item.full_name, other_name)
    employees_by_staff_id = (
        {e.staff_id: e for e in Employee.query.filter(Employee.staff_id.in_(file_staff_ids)).all()}
        if file_staff_ids
        else {}
    )

    for index, row in enumerate(mapped_rows, start=1):
        row_warnings = validate_single_row(row, employees_by_staff_id=employees_by_staff_id)
        key = build_worker_key(row)
        if key and key in duplicate_keys:
            row_warnings.append("Worker appears more than once in this client payroll.")

        if key:
            other_client_name = cross_client_by_staff_id.get(
                str(row.get("staff_id") or "").strip()
            ) or cross_client_by_name.get(str(row.get("full_name") or "").strip())
            if other_client_name:
                row_warnings.append(
                    f"Worker also appears in {other_client_name} payroll for {month} {year}."
                )

        if row_warnings:
            per_row_warnings[index] = row_warnings

    if duplicate_keys:
        warnings.append(
            f"{len(duplicate_keys)} worker identifier(s) appear more than once in this upload."
        )

    missing_bank_count = sum(
        1 for row in mapped_rows if not str(row.get("bank_account_number") or "").strip()
    )
    non_blank_count = sum(1 for row in mapped_rows if build_worker_key(row))
    if non_blank_count and missing_bank_count / non_blank_count >= 0.5:
        warnings.append("Majority of workers are missing bank account details.")
    # PMVP computes PAYE and SSNIT itself when the run is confirmed
    # (auto_calculate_on_confirm); any statutory figures in the file are
    # preview-only and never persist. So an upload with no PAYE/SSNIT is normal
    # for a salary-only sheet — inform, don't imply the client left something out.
    if sum(float(row.get("paye") or 0) for row in mapped_rows) <= 0:
        warnings.append("No PAYE in the upload — PMVP will calculate it when the run is confirmed.")
    if sum(float(row.get("ssnit") or 0) for row in mapped_rows) <= 0:
        warnings.append("No SSNIT in the upload — PMVP will calculate it when the run is confirmed.")

    previous_run = (
        PayrollRun.query.filter(
            PayrollRun.client_company_id == client_company.id,
            PayrollRun.status.in_(CLOSED_STATUSES),
        )
        .order_by(PayrollRun.year.desc(), PayrollRun.created_at.desc())
        .first()
    )
    current_net_total = sum(float(row.get("net_pay") or 0) for row in mapped_rows)
    current_worker_count = len({build_worker_key(row) for row in mapped_rows if build_worker_key(row)})
    if previous_run and previous_run.total_net_pay:
        if current_net_total > previous_run.total_net_pay * 1.5:
            warnings.append("Payroll total is unusually higher than a previous approved payroll.")
        if previous_run.total_workers and abs(current_worker_count - previous_run.total_workers) > max(5, previous_run.total_workers * 0.25):
            warnings.append("Worker count changed significantly from a previous approved payroll.")

    return {
        "summary_warnings": warnings,
        "per_row_warnings": per_row_warnings,
        "duplicate_keys": list(duplicate_keys),
    }


def validate_single_row(row, employees_by_staff_id=None):
    """``employees_by_staff_id`` is an optional prefetched ``{staff_id: Employee}``
    map (see validate_payroll_rows) so bulk validation costs one query, not one
    per row. When omitted, falls back to the per-row lookup."""
    warnings = []
    staff_id = str(row.get("staff_id") or "").strip()
    full_name = str(row.get("full_name") or "").strip()
    ssnit_number = str(row.get("ssnit_number") or "").strip()
    ghana_card_number = str(row.get("ghana_card_number") or "").strip()
    bank_account_number = str(row.get("bank_account_number") or "").strip()
    momo_number = str(row.get("momo_number") or "").strip()
    status = normalize_label(row.get("status"))
    net_pay = row.get("net_pay")

    if not staff_id:
        warnings.append("Missing staff ID.")
    if not full_name:
        warnings.append("Missing employee name.")
    # Header-misdetection guard: a "name" that is actually a column heading
    # ("NAMES", "JOB TITLE") or a bare number ("0") means the row is data-shifted
    # — the same failure that gave every acs 1.xlsx worker the name "0".
    elif looks_like_header_label(full_name, numeric_is_suspicious=True):
        warnings.append(
            f'Employee name "{full_name}" looks like a column heading or placeholder, '
            "not a real name — the upload may have the wrong header row."
        )
    if not ssnit_number:
        if employees_by_staff_id is not None:
            employee = employees_by_staff_id.get(staff_id) if staff_id else None
        else:
            employee = Employee.query.filter_by(staff_id=staff_id).first() if staff_id else None
        if not employee or not employee.ssnit_number:
            warnings.append("Missing SSNIT number.")
    if not ghana_card_number:
        warnings.append("Missing Ghana Card number.")
    if not bank_account_number and not momo_number:
        warnings.append("Missing bank and MoMo details.")
    if net_pay in (None, "") or row.get("_missing_original_net_pay"):
        warnings.append("Net pay not provided; PMVP will calculate it on confirm.")
    if float(row.get("net_pay") or 0) < 0:
        warnings.append("Negative net pay.")
    for field in ("basic_salary", "gross_pay", "paye", "ssnit", "tier_2_pension", "pf_fund_employee", "loan_deduction", "welfare_deduction", "iou_deduction", "other_deductions", "total_deductions", "net_pay"):
        if float(row.get(field) or 0) < 0:
            warnings.append("Negative salary or deduction value.")
            break

    gross_pay = float(row.get("gross_pay") or 0)
    basic_salary = float(row.get("basic_salary") or 0)
    if basic_salary <= 0 and "inactive" not in status and "terminated" not in status:
        warnings.append(
            "No basic salary — every derived figure will be zero; "
            "the import will be blocked until this is fixed."
        )
    # The full §5.6 gross component set — missing components here produced
    # false "does not match" warnings on rows with medical/meal/bonus figures.
    calculated_gross = (
        basic_salary
        + float(row.get("transport_allowance") or 0)
        + float(row.get("housing_allowance") or 0)
        + float(row.get("medical_allowance") or 0)
        + float(row.get("meal_allowance") or 0)
        + float(row.get("productivity_bonus") or 0)
        + float(row.get("end_of_year_bonus") or 0)
        + float(row.get("overtime_pay") or 0)
        + float(row.get("other_allowances") or 0)
        + float(row.get("pay_difference") or 0)
    )
    total_deductions = (
        float(row.get("paye") or 0)
        + float(row.get("ssnit") or 0)
        + float(row.get("tier_2_pension") or 0)
        + float(row.get("pf_fund_employee") or 0)
        + float(row.get("loan_deduction") or 0)
        + float(row.get("welfare_deduction") or 0)
        + float(row.get("iou_deduction") or 0)
        + float(row.get("other_deductions") or 0)
    )
    expected_net_pay = gross_pay - total_deductions + float(row.get("loan_advance") or 0)

    if gross_pay < basic_salary:
        warnings.append("Gross pay is less than basic salary.")
    if abs(gross_pay - calculated_gross) > 1:
        warnings.append("Gross pay does not match allowance calculation.")
    if abs(float(row.get("net_pay") or 0) - expected_net_pay) > 1:
        warnings.append("Uploaded net pay doesn't reconcile with its components; PMVP recalculates all figures on confirm.")
    # PMVP derives PAYE/SSNIT on confirm, so an upload without them is expected
    # for salary-only sheets — phrase these as informational, not client errors.
    if not row.get("paye"):
        warnings.append("PAYE not provided; PMVP will calculate it on confirm.")
    if not row.get("ssnit"):
        warnings.append("SSNIT not provided; PMVP will calculate it on confirm.")
    if "inactive" in status:
        warnings.append("Inactive worker appears in payroll.")
    if "terminated" in status:
        warnings.append("Terminated worker appears in payroll.")
    if gross_pay >= 20000:
        warnings.append("Very high salary.")
    if gross_pay <= 0 and float(row.get("net_pay") or 0) <= 0:
        warnings.append("Zero salary.")
    if gross_pay and float(row.get("total_deductions") or 0) > gross_pay * 0.6:
        warnings.append("Very high deductions.")

    return warnings
