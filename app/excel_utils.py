import os
import re
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill


logger = logging.getLogger(__name__)


COLUMN_ALIASES = {
    "staff_id": ["staff no", "staff no.", "staff id", "employee id", "emp id", "staff number", "employee no", "worker id", "s/n", "sn", "no", "serial"],
    "full_name": ["name", "employee name", "full name", "worker name", "employee", "worker", "officer", "personnel"],
    "client_company": ["client", "client company", "company", "company name"],
    "ssnit_number": ["ssnit no", "ssnit number", "ssnit id", "ssnit contribution number"],
    "ghana_card_number": ["ghana card", "ghana card no", "ghana card number"],
    "momo_number": ["momo", "momo number", "momo no", "mobile money", "mobile money number", "phone", "phone number", "phone no", "telephone", "tel", "mobile", "mobile number", "mobile no", "cell", "cellphone", "contact number", "contact no"],
    "email": ["email", "e-mail", "e mail", "email address", "email id", "mail"],
    "bank_name": ["bank", "bank name"],
    "bank_account_number": ["account no", "account number", "bank account", "bank account number"],
    "status": ["status", "employee status", "worker status", "employment status"],
    "service_line": ["service line", "department", "unit"],
    "job_role": ["job role", "role", "position", "designation"],
    "payroll_month": ["payroll month", "month"],
    "basic_salary": ["basic", "basic salary", "base pay", "basic pay", "base salary", "monthly salary", "salary"],
    "transport_allowance": ["transport", "transport allowance", "transportation"],
    "housing_allowance": ["housing", "housing allowance", "rent allowance"],
    "medical_allowance": ["medical", "medical allowance", "med allowance", "med. allowance"],
    "productivity_bonus": ["productivity bonus", "productivity", "prod bonus", "prod. bonus", "bonus"],
    "overtime_hours": ["overtime hours", "ot hours"],
    "overtime_pay": ["overtime", "ot pay", "overtime pay"],
    "other_allowances": ["other allowance", "other allowances", "allowances", "allowance"],
    "gross_pay": ["gross", "gross pay", "gross salary", "total earnings", "gross earnings", "gross amount"],
    "paye": ["paye", "tax", "income tax", "paye tax", "tax deducted"],
    "ssnit": ["ssnit", "social security", "ssnit contribution", "ssnit employee", "ssnit emp", "ssnit (employee)"],
    "tier_2_pension": ["tier 2", "tier 2 pension", "tier two pension", "pension"],
    "pf_fund_employee": ["pf fund / employee", "pf fund employee", "pf fund", "pf employee", "provident fund", "provident fund employee"],
    "loan_deduction": ["loan deduction", "loan deductions", "loan"],
    "other_deductions": ["deduction", "deductions", "other deduction", "other deductions"],
    "total_deductions": ["total deductions", "total deduction"],
    "net_pay": ["net", "net pay", "net salary", "take home", "take home pay", "net amount", "amount payable", "net earnings"],
}

MONEY_FIELDS = {
    "basic_salary",
    "transport_allowance",
    "housing_allowance",
    "medical_allowance",
    "productivity_bonus",
    "pf_fund_employee",
    "overtime_hours",
    "overtime_pay",
    "other_allowances",
    "gross_pay",
    "paye",
    "ssnit",
    "tier_2_pension",
    "loan_deduction",
    "other_deductions",
    "total_deductions",
    "net_pay",
}

META_SHEET_NAMES = {
    "stress test guide",
    "client companies",
    "expected summary",
    "expected validation",
    "upload test cases",
    "summary",
    "guide",
    "instructions",
    "readme",
}

PAYROLL_HEADER_KEYWORDS = {
    "staff id",
    "staff no",
    "staff no.",
    "employee id",
    "emp id",
    "worker id",
    "s/n",
    "sn",
    "serial",
    "employee",
    "name",
    "employee name",
    "full name",
    "worker",
    "officer",
    "personnel",
    "status",
    "service line",
    "job role",
    "basic salary",
    "basic",
    "base pay",
    "basic pay",
    "base salary",
    "monthly salary",
    "gross pay",
    "gross salary",
    "total earnings",
    "gross earnings",
    "gross amount",
    "paye",
    "income tax",
    "tax deducted",
    "ssnit",
    "ssnit employee",
    "ssnit emp",
    "social security",
    "tier 2 pension",
    "net pay",
    "net salary",
    "net amount",
    "amount payable",
    "net earnings",
    "take home",
    "bank",
    "bank name",
    "bank account",
    "momo",
    "ghana card",
    "ghana card no",
    "ssnit number",
    "payroll month",
    "transport allowance",
    "housing allowance",
    "overtime",
    "overtime pay",
    "allowance",
    "deduction",
    "total deductions",
}

PAYROLL_SHEET_NAME_KEYWORDS = {
    "payroll",
    "salary",
    "wages",
    "staff",
    "workers",
    "personnel",
}


def normalize_label(value):
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_worker(value):
    return normalize_label(value)


def normalize_company_key(value, strip_suffix=True):
    parts = normalize_label(value).split()
    if strip_suffix:
        parts = [part for part in parts if part not in {"ltd", "limited"}]
    return "".join(parts)


def company_tokens(value):
    return {
        part
        for part in normalize_label(value).split()
        if part and part not in {"ltd", "limited", "company", "co"}
    }


def slug_filename(value):
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return cleaned or "report"


def allowed_excel_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in {"xlsx", "xls", "csv"}


def map_columns(columns):
    mapping = {}
    alias_lookup = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            alias_lookup[normalize_label(alias)] = field

    for column in columns:
        normalized = normalize_label(column)
        mapped_field = alias_lookup.get(normalized)
        if mapped_field is None:
            for alias, field in alias_lookup.items():
                if alias and (alias in normalized or normalized in alias):
                    mapped_field = field
                    break
        mapping[column] = mapped_field or "unmapped"
    return mapping


def workbook_sheet_names(file_path):
    ext = file_path.rsplit(".", 1)[1].lower()
    if ext == "csv":
        return [Path(file_path).stem]
    with pd.ExcelFile(file_path, engine="openpyxl" if ext == "xlsx" else None) as excel_file:
        return excel_file.sheet_names


def is_meta_sheet(sheet_name):
    normalized = normalize_label(sheet_name)
    return normalized in META_SHEET_NAMES


def sheet_name_suggests_payroll(sheet_name):
    normalized = normalize_label(sheet_name)
    return any(keyword in normalized for keyword in PAYROLL_SHEET_NAME_KEYWORDS)


def header_score(row):
    known_labels = set()
    for field, aliases in COLUMN_ALIASES.items():
        known_labels.add(normalize_label(field))
        known_labels.update(normalize_label(alias) for alias in aliases)
    known_labels.update(PAYROLL_HEADER_KEYWORDS)

    labels = [normalize_label(value) for value in row if value not in (None, "")]
    score = 0
    for label in labels:
        if label in known_labels:
            score += 2
        elif any(keyword and keyword in label for keyword in PAYROLL_HEADER_KEYWORDS):
            score += 1
    return score


def find_header_row(file_path, sheet_name=None):
    ext = file_path.rsplit(".", 1)[1].lower()
    sheet_arg = sheet_name if sheet_name is not None else 0
    if ext == "csv":
        sample = pd.read_csv(file_path, header=None, nrows=20, dtype=str)
    elif ext == "xlsx":
        sample = pd.read_excel(file_path, sheet_name=sheet_arg, engine="openpyxl", header=None, nrows=20, dtype=str)
    else:
        sample = pd.read_excel(file_path, sheet_name=sheet_arg, header=None, nrows=20, dtype=str)
    rows = sample.fillna("").values.tolist()

    best_row = 1
    best_score = 0
    for row_index, row in enumerate(rows, start=1):
        score = header_score(row)
        if score > best_score:
            best_score = score
            best_row = row_index
    return max(best_row - 1, 0) if best_score >= 2 else 0


def payroll_sheet_candidates(file_path):
    candidates = []
    ext = file_path.rsplit(".", 1)[1].lower()
    for sheet_name in workbook_sheet_names(file_path):
        if is_meta_sheet(sheet_name):
            continue
        header_row = find_header_row(file_path, sheet_name if ext != "csv" else None)
        if ext == "csv":
            sample = pd.read_csv(file_path, header=None, nrows=20, dtype=str)
        elif ext == "xlsx":
            sample = pd.read_excel(file_path, sheet_name=sheet_name, engine="openpyxl", header=None, nrows=20, dtype=str)
        else:
            sample = pd.read_excel(file_path, sheet_name=sheet_name, header=None, nrows=20, dtype=str)
        rows = sample.fillna("").values.tolist()
        score = header_score(rows[header_row]) if header_row < len(rows) else 0
        if score >= 2 or sheet_name_suggests_payroll(sheet_name):
            candidates.append(
                {
                    "sheet_name": sheet_name,
                    "detected_header_row": header_row + 1,
                    "score": score,
                }
            )
    return candidates


def match_client_sheet(client_name, sheet_names):
    normalized_client = normalize_company_key(client_name)
    for sheet_name in sheet_names:
        normalized_sheet = normalize_company_key(sheet_name)
        if normalized_client == normalized_sheet:
            return sheet_name
    for sheet_name in sheet_names:
        normalized_sheet = normalize_company_key(sheet_name)
        if normalized_client in normalized_sheet or normalized_sheet in normalized_client:
            return sheet_name
    client_tokens = company_tokens(client_name)
    for sheet_name in sheet_names:
        if len(client_tokens.intersection(company_tokens(sheet_name))) >= 2:
            return sheet_name
    return None


def read_excel_file(file_path, sheet_name=None):
    header_row = find_header_row(file_path, sheet_name)
    ext = file_path.rsplit(".", 1)[1].lower()
    sheet_arg = sheet_name if sheet_name is not None else 0
    if ext == "csv":
        df = pd.read_csv(file_path, header=header_row, dtype=str)
    elif ext == "xlsx":
        df = pd.read_excel(file_path, sheet_name=sheet_arg, engine="openpyxl", header=header_row, dtype=str)
    else:
        df = pd.read_excel(file_path, sheet_name=sheet_arg, header=header_row, dtype=str)
    df = df.dropna(how="all")
    df.columns = [str(column).strip() for column in df.columns]
    df = df.fillna("")
    mapping = map_columns(df.columns)
    return df, mapping


def detect_company_name(file_path, known_company_names=None, sheet_name=None):
    known_company_names = known_company_names or []
    sampled_values = []
    ext = file_path.rsplit(".", 1)[1].lower()
    sheet_arg = sheet_name if sheet_name is not None else 0
    if ext == "csv":
        sample = pd.read_csv(file_path, header=None, nrows=20, dtype=str)
    elif ext == "xlsx":
        sample = pd.read_excel(file_path, sheet_name=sheet_arg, engine="openpyxl", header=None, nrows=20, dtype=str)
    else:
        sample = pd.read_excel(file_path, sheet_name=sheet_arg, header=None, nrows=20, dtype=str)
    for row in sample.fillna("").values.tolist():
        for value in row:
            if value not in (None, ""):
                sampled_values.append(str(value).strip())

    combined = " ".join(sampled_values)
    normalized_combined = normalize_label(combined)
    for company_name in known_company_names:
        if normalize_label(company_name) in normalized_combined:
            return company_name

    for label in ("company", "client", "employer"):
        for index, value in enumerate(sampled_values):
            if label in normalize_label(value) and index + 1 < len(sampled_values):
                return sampled_values[index + 1]
    return ""


def to_number(value):
    if hasattr(value, "tolist"):
        values = [item for item in value.tolist() if item not in (None, "")]
        value = values[0] if values else ""
    if value in (None, ""):
        return 0.0
    try:
        cleaned = (
            str(value)
            .replace(",", "")
            .replace("GHS", "")
            .replace("GH₵", "")
            .replace("₵", "")
            .replace("GH¢", "")
            .replace("¢", "")
            .strip()
        )
        cleaned = cleaned.replace("GHC", "")
        cleaned = re.sub(r"[^0-9.\-]", "", cleaned)
        return float(cleaned or 0)
    except (TypeError, ValueError):
        return 0.0


def scalar_cell_value(value):
    if hasattr(value, "tolist"):
        values = [item for item in value.tolist() if item not in (None, "")]
        return values[0] if values else ""
    return value


def mapped_rows_from_dataframe(df, mapping):
    rows = []
    mapped_fields = {field for field in mapping.values() if field != "unmapped"}
    all_unmapped_skips = 0
    for _, source_row in df.iterrows():
        row = {}
        original_presence = {}
        first_cell = scalar_cell_value(source_row.iloc[0]) if len(source_row.index) else ""
        if normalize_label(first_cell) in {"total", "grand total", "subtotal", "summary"}:
            continue
        for original_column, field in mapping.items():
            if field == "unmapped":
                continue
            value = source_row.get(original_column, "")
            value = scalar_cell_value(value)
            original_presence[field] = str(value or "").strip() not in {"", "nan", "None"}
            row[field] = to_number(value) if field in MONEY_FIELDS else str(value).strip()

        if not mapped_fields:
            all_unmapped_skips += 1
            continue

        identity = " ".join(
            str(row.get(field) or "")
            for field in ("staff_id", "full_name", "basic_salary", "gross_pay", "net_pay")
        )
        if not identity.strip() and not any(row.get(field, 0) for field in MONEY_FIELDS):
            continue
        if normalize_label(row.get("staff_id") or row.get("full_name")) in {
            "total",
            "grand total",
            "subtotal",
            "summary",
        }:
            continue

        for field in MONEY_FIELDS:
            row.setdefault(field, 0.0)
        row.setdefault("staff_id", "")
        row.setdefault("full_name", "")
        row.setdefault("ssnit_number", "")
        row.setdefault("ghana_card_number", "")
        row.setdefault("momo_number", "")
        row.setdefault("bank_name", "")
        row.setdefault("bank_account_number", "")
        row.setdefault("status", "")
        row.setdefault("service_line", "")
        row.setdefault("job_role", "")
        row.setdefault("payroll_month", "")
        row["_missing_original_net_pay"] = not original_presence.get("net_pay", False)
        row["_missing_original_gross_pay"] = not original_presence.get("gross_pay", False)
        row["_missing_original_total_deductions"] = not original_presence.get("total_deductions", False)

        calculated_gross = (
            row["basic_salary"]
            + row["transport_allowance"]
            + row["housing_allowance"]
            + row["overtime_pay"]
            + row["other_allowances"]
        )
        if not row["gross_pay"] and calculated_gross:
            row["gross_pay"] = calculated_gross
        statutory_deductions = row["paye"] + row["ssnit"] + row["tier_2_pension"]
        itemized_other = row["loan_deduction"] + row["other_deductions"]
        if row["total_deductions"]:
            pass
        elif row["other_deductions"] >= statutory_deductions and statutory_deductions:
            row["total_deductions"] = row["other_deductions"]
        else:
            row["total_deductions"] = statutory_deductions + itemized_other
        if not row["net_pay"] and row["gross_pay"]:
            row["net_pay"] = row["gross_pay"] - row["total_deductions"]
        rows.append(row)
    logger.debug("Smart Excel Import Engine: rows skipped due to all-unmapped columns=%s", all_unmapped_skips)
    return rows


def calculate_worker_stats(mapped_rows):
    seen = set()
    duplicate_count = 0
    non_blank_rows = 0

    for row in mapped_rows:
        staff_id = normalize_worker(row.get("staff_id"))
        full_name = normalize_worker(row.get("full_name"))
        if not staff_id and not full_name:
            continue

        non_blank_rows += 1
        worker_key = f"staff:{staff_id}" if staff_id else f"name:{full_name}"
        if worker_key in seen:
            duplicate_count += 1
        else:
            seen.add(worker_key)

    return {
        "total_rows": non_blank_rows,
        "total_unique_workers": len(seen),
        "duplicate_count": duplicate_count,
    }


def calculate_status_breakdown(mapped_rows):
    breakdown = {
        "active": 0,
        "inactive": 0,
        "terminated": 0,
        "on_leave": 0,
        "unknown": 0,
    }
    for row in mapped_rows:
        status = normalize_label(row.get("status"))
        if "terminated" in status:
            breakdown["terminated"] += 1
        elif "inactive" in status:
            breakdown["inactive"] += 1
        elif "leave" in status:
            breakdown["on_leave"] += 1
        elif "active" in status:
            breakdown["active"] += 1
        else:
            breakdown["unknown"] += 1
    return breakdown


def summarize_mapped_rows(mapped_rows):
    return {
        "total_rows": len(mapped_rows),
        "valid_rows": len(mapped_rows),
        "invalid_rows": 0,
        "gross_total": sum(float(row.get("gross_pay") or 0) for row in mapped_rows),
        "net_total": sum(float(row.get("net_pay") or 0) for row in mapped_rows),
        "paye_total": sum(float(row.get("paye") or 0) for row in mapped_rows),
        "ssnit_total": sum(float(row.get("ssnit") or 0) for row in mapped_rows),
        "deductions_total": sum(float(row.get("total_deductions") or 0) for row in mapped_rows),
    }


def extract_payroll_sheet(file_path, sheet_name=None):
    header_row = find_header_row(file_path, sheet_name)
    df, mapping = read_excel_file(file_path, sheet_name)
    unmapped_columns = [column for column, field in mapping.items() if field == "unmapped"]
    logger.debug(
        "Smart Excel Import Engine: sheet=%s detected_header_row=%s columns=%s mapped_columns=%s unmapped_columns=%s",
        sheet_name or workbook_sheet_names(file_path)[0],
        header_row + 1,
        list(df.columns),
        {column: field for column, field in mapping.items() if field != "unmapped"},
        unmapped_columns,
    )
    mapped_rows = mapped_rows_from_dataframe(df, mapping)
    worker_stats = calculate_worker_stats(mapped_rows)
    totals = summarize_mapped_rows(mapped_rows)
    return {
        "sheet_name": sheet_name or workbook_sheet_names(file_path)[0],
        "detected_header_row": header_row + 1,
        "columns": list(df.columns),
        "mapping": mapping,
        "preview_rows": df.head(20).astype(str).to_dict(orient="records"),
        "mapped_rows": mapped_rows,
        "worker_stats": worker_stats,
        "status_breakdown": calculate_status_breakdown(mapped_rows),
        "totals": totals,
        "ignored_rows": max(len(df.index) - len(mapped_rows), 0),
    }


def create_workbook(report_title):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = report_title[:31]
    sheet["A1"] = "Chrisnat Limited"
    sheet["A1"].font = Font(bold=True, size=14)
    sheet["A2"] = report_title
    sheet["A2"].font = Font(bold=True)
    sheet["A3"] = f"Date generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    return workbook, sheet


def write_table(sheet, start_row, headers, rows):
    header_fill = PatternFill("solid", fgColor="D9EAF7")
    for col_index, header in enumerate(headers, start=1):
        cell = sheet.cell(row=start_row, column=col_index, value=header)
        cell.font = Font(bold=True)
        cell.fill = header_fill
    for row_index, row in enumerate(rows, start=start_row + 1):
        for col_index, value in enumerate(row, start=1):
            sheet.cell(row=row_index, column=col_index, value=value)
    for column_cells in sheet.columns:
        max_length = max(len(str(cell.value or "")) for cell in column_cells)
        sheet.column_dimensions[column_cells[0].column_letter].width = min(
            max_length + 3, 35
        )


def save_workbook(workbook, export_folder, filename):
    os.makedirs(export_folder, exist_ok=True)
    file_path = os.path.join(export_folder, filename)
    workbook.save(file_path)
    return file_path


def export_employees(employees, export_folder):
    workbook, sheet = create_workbook("Employee List")
    headers = ["Staff ID", "Name", "Client", "Phone", "SSNIT", "Status", "Basic Salary"]
    rows = [
        [
            employee.staff_id,
            employee.full_name,
            employee.client_company.name if employee.client_company else employee.assigned_client,
            employee.phone,
            employee.ssnit_number,
            employee.status,
            employee.basic_salary,
        ]
        for employee in employees
    ]
    write_table(sheet, 5, headers, rows)
    return save_workbook(workbook, export_folder, "Chrisnat_Employee_List.xlsx")


def export_payroll_run(payroll_run, export_folder):
    client_name = payroll_run.client_company.name if payroll_run.client_company else "Client"
    title = f"{client_name} Payroll {payroll_run.month} {payroll_run.year}"
    workbook, sheet = create_workbook(title)
    headers = [
        "Staff ID",
        "Name",
        "Basic",
        "Transport",
        "Housing",
        "Overtime",
        "Gross",
        "PAYE",
        "SSNIT",
        "Deductions",
        "Net Pay",
        "Warnings",
    ]
    rows = [
        [
            item.staff_id,
            item.full_name,
            item.basic_salary,
            item.transport_allowance,
            item.housing_allowance,
            item.overtime_pay,
            item.gross_pay,
            item.paye,
            item.ssnit,
            item.total_deductions,
            item.net_pay,
            item.warning_notes,
        ]
        for item in payroll_run.items
    ]
    write_table(sheet, 5, headers, rows)
    total_row = len(rows) + 7
    sheet.cell(row=total_row, column=1, value="Totals").font = Font(bold=True)
    sheet.cell(row=total_row, column=7, value=payroll_run.total_gross_pay)
    sheet.cell(row=total_row, column=8, value=payroll_run.total_paye)
    sheet.cell(row=total_row, column=9, value=payroll_run.total_ssnit)
    sheet.cell(row=total_row, column=10, value=payroll_run.total_deductions)
    sheet.cell(row=total_row, column=11, value=payroll_run.total_net_pay)
    filename = (
        f"{slug_filename(client_name)}_Payroll_{payroll_run.month}_{payroll_run.year}.xlsx"
    )
    return save_workbook(workbook, export_folder, filename)


def export_payment_vouchers(vouchers, export_folder):
    workbook, sheet = create_workbook("Payment Vouchers")
    headers = [
        "Voucher",
        "Client",
        "Month",
        "Workers",
        "Gross Payroll",
        "Deductions",
        "Net Payable",
        "Status",
        "Prepared By",
        "Reviewed By",
        "Approved By",
    ]
    rows = [
        [
            voucher.voucher_number,
            voucher.payroll_run.client_company.name if voucher.payroll_run.client_company else "",
            f"{voucher.payroll_run.month} {voucher.payroll_run.year}",
            voucher.payroll_run.total_workers,
            voucher.gross_payroll,
            voucher.total_deductions,
            voucher.net_amount_payable,
            voucher.status,
            voucher.preparer.name if voucher.preparer else "",
            voucher.reviewer.name if voucher.reviewer else "",
            voucher.approver.name if voucher.approver else "",
        ]
        for voucher in vouchers
    ]
    write_table(sheet, 5, headers, rows)
    return save_workbook(workbook, export_folder, "Chrisnat_Payment_Vouchers.xlsx")


def export_remittances(remittances, export_folder):
    workbook, sheet = create_workbook("Remittance Summary")
    headers = ["Client", "Month", "Type", "Amount Due", "Due Date", "Status", "Reference"]
    rows = [
        [
            remittance.payroll_run.client_company.name if remittance.payroll_run.client_company else "",
            f"{remittance.payroll_run.month} {remittance.payroll_run.year}",
            remittance.remittance_type,
            remittance.amount_due,
            remittance.due_date.isoformat() if remittance.due_date else "",
            remittance.status,
            remittance.payment_reference,
        ]
        for remittance in remittances
    ]
    write_table(sheet, 5, headers, rows)
    return save_workbook(workbook, export_folder, "Chrisnat_Remittance_Summary.xlsx")


def export_expenses(expenses, export_folder):
    workbook, sheet = create_workbook("Expense List")
    headers = ["Date", "Category", "Description", "Amount", "Method", "Receipt"]
    rows = [
        [
            expense.expense_date.isoformat(),
            expense.category,
            expense.description,
            expense.amount,
            expense.payment_method,
            expense.receipt_reference,
        ]
        for expense in expenses
    ]
    write_table(sheet, 5, headers, rows)
    total_row = len(rows) + 7
    sheet.cell(row=total_row, column=1, value="Total").font = Font(bold=True)
    sheet.cell(row=total_row, column=4, value=sum(expense.amount for expense in expenses))
    return save_workbook(workbook, export_folder, "Chrisnat_Expenses.xlsx")


def export_monthly_payroll_summary(payroll_runs, export_folder, month, year):
    workbook, sheet = create_workbook(f"Monthly payroll summary {month} {year}")
    headers = [
        "Client",
        "Month",
        "Workers",
        "Gross Payroll",
        "Deductions",
        "PAYE",
        "SSNIT",
        "Net Payroll",
        "Status",
    ]
    rows = [
        [
            run.client_company.name if run.client_company else "",
            f"{run.month} {run.year}",
            run.total_workers,
            run.total_gross_pay,
            run.total_deductions,
            run.total_paye,
            run.total_ssnit,
            run.total_net_pay,
            run.status,
        ]
        for run in payroll_runs
    ]
    write_table(sheet, 5, headers, rows)
    total_row = len(rows) + 7
    sheet.cell(row=total_row, column=1, value="Totals").font = Font(bold=True)
    sheet.cell(row=total_row, column=3, value=sum(run.total_workers for run in payroll_runs))
    sheet.cell(row=total_row, column=4, value=sum(run.total_gross_pay for run in payroll_runs))
    sheet.cell(row=total_row, column=5, value=sum(run.total_deductions for run in payroll_runs))
    sheet.cell(row=total_row, column=6, value=sum(run.total_paye for run in payroll_runs))
    sheet.cell(row=total_row, column=7, value=sum(run.total_ssnit for run in payroll_runs))
    sheet.cell(row=total_row, column=8, value=sum(run.total_net_pay for run in payroll_runs))
    return save_workbook(
        workbook,
        export_folder,
        f"Chrisnat_Monthly_Payroll_{slug_filename(month)}_{year}.xlsx",
    )


def export_import_error_report(payload, export_folder):
    workbook, sheet = create_workbook("Payroll Import Error Report")
    headers = ["Row", "Staff ID", "Name", "Bank Account", "Net Pay", "Warnings"]
    rows = []
    for row_number, warnings in payload.get("validation", {}).get("per_row_warnings", {}).items():
        index = int(row_number) - 1
        mapped_row = payload.get("mapped_rows", [])[index] if index < len(payload.get("mapped_rows", [])) else {}
        rows.append(
            [
                row_number,
                mapped_row.get("staff_id", ""),
                mapped_row.get("full_name", ""),
                mapped_row.get("bank_account_number", ""),
                mapped_row.get("net_pay", 0),
                "; ".join(warnings),
            ]
        )
    write_table(sheet, 5, headers, rows)
    return save_workbook(
        workbook,
        export_folder,
        f"Import_Errors_{slug_filename(payload.get('source_filename', 'payroll'))}.xlsx",
    )


def export_bank_listing(payroll_run, export_folder):
    """Bank transfer batch listing: employees grouped by bank, each row showing
    account number and net pay, with a subtotal per bank. Derived entirely from
    the run's items — replaces the per-bank tables maintained by hand in the
    source workbooks."""
    client_name = payroll_run.client_company.name if payroll_run.client_company else "Client"
    title = f"{client_name} Bank Listing {payroll_run.month} {payroll_run.year}"
    workbook, sheet = create_workbook(title)

    groups = {}
    for item in payroll_run.items:
        bank = (item.bank_name or "").strip() or "NO BANK ON RECORD"
        groups.setdefault(bank, []).append(item)

    header_fill = PatternFill("solid", fgColor="D9EAF7")
    band_fill = PatternFill("solid", fgColor="052420")
    row_index = 5
    for bank in sorted(groups):
        items = sorted(groups[bank], key=lambda i: (i.full_name or "").upper())
        band = sheet.cell(row=row_index, column=1, value=bank)
        band.font = Font(bold=True, color="FFFFFF")
        band.fill = band_fill
        for col in range(2, 5):
            sheet.cell(row=row_index, column=col).fill = band_fill
        row_index += 1
        for col_index, header in enumerate(
            ["Staff ID", "Employee Name", "Account Number", "Net Pay (GH¢)"], start=1
        ):
            cell = sheet.cell(row=row_index, column=col_index, value=header)
            cell.font = Font(bold=True)
            cell.fill = header_fill
        row_index += 1
        subtotal = 0.0
        for item in items:
            sheet.cell(row=row_index, column=1, value=item.staff_id)
            sheet.cell(row=row_index, column=2, value=item.full_name)
            sheet.cell(row=row_index, column=3, value=item.bank_account_number)
            sheet.cell(row=row_index, column=4, value=round(item.net_pay or 0, 2))
            subtotal += item.net_pay or 0
            row_index += 1
        total_cell = sheet.cell(row=row_index, column=2, value=f"{bank} Total ({len(items)} workers)")
        total_cell.font = Font(bold=True)
        amount_cell = sheet.cell(row=row_index, column=4, value=round(subtotal, 2))
        amount_cell.font = Font(bold=True)
        row_index += 2

    grand = sheet.cell(row=row_index, column=2, value="GRAND TOTAL")
    grand.font = Font(bold=True, size=12)
    grand_amount = sheet.cell(
        row=row_index, column=4,
        value=round(sum(i.net_pay or 0 for i in payroll_run.items), 2),
    )
    grand_amount.font = Font(bold=True, size=12)
    for column_cells in sheet.columns:
        max_length = max(len(str(cell.value or "")) for cell in column_cells)
        sheet.column_dimensions[column_cells[0].column_letter].width = min(max_length + 3, 40)
    filename = (
        f"{slug_filename(client_name)}_Bank_Listing_"
        f"{slug_filename(payroll_run.month)}_{payroll_run.year}.xlsx"
    )
    return save_workbook(workbook, export_folder, filename)


def export_gra_paye_schedule(payroll_run, export_folder, employer_tin="", tax_office=""):
    """Employer's Monthly Tax Deductions Schedule (P.A.Y.E.) — the statutory
    GRA layout from the source workbook's GRA PAYE sheet: employer TIN, tax
    office and month up top, then one row per employee with the PAYE deducted."""
    client_name = payroll_run.client_company.name if payroll_run.client_company else "Client"
    workbook, sheet = create_workbook(
        f"GRA PAYE {payroll_run.month} {payroll_run.year}"
    )
    sheet["A2"] = "EMPLOYER'S MONTHLY TAX DEDUCTIONS SCHEDULE (P.A.Y.E.)"
    sheet["A2"].font = Font(bold=True)
    sheet["A4"] = f"Employer TIN: {employer_tin or ''}"
    sheet["A5"] = f"Tax Office: {tax_office or ''}"
    sheet["A6"] = f"Client / Employer: {client_name}"
    sheet["A7"] = f"Month: {payroll_run.month} {payroll_run.year}"

    headers = [
        "No.",
        "Employee Name",
        "Staff ID",
        "TIN / Ghana Card",
        "SSNIT No.",
        "Basic Salary",
        "Total Cash Emoluments (Gross)",
        "Employee SSF",
        "Taxable Income",
        "Tax Deducted (PAYE)",
    ]
    rows = []
    for index, item in enumerate(
        sorted(payroll_run.items, key=lambda i: (i.full_name or "").upper()), start=1
    ):
        taxable = round(
            (item.gross_pay or 0) - (item.ssnit or 0) - (item.pf_fund_employee or 0), 2
        )
        rows.append([
            index,
            item.full_name,
            item.staff_id,
            item.ghana_card_number,
            item.ssnit_number,
            round(item.basic_salary or 0, 2),
            round(item.gross_pay or 0, 2),
            round(item.ssnit or 0, 2),
            taxable,
            round(item.paye or 0, 2),
        ])
    write_table(sheet, 9, headers, rows)
    total_row = 9 + len(rows) + 1
    sheet.cell(row=total_row, column=2, value="TOTALS").font = Font(bold=True)
    for column, value in [
        (7, payroll_run.total_gross_pay),
        (8, payroll_run.total_ssnit),
        (10, payroll_run.total_paye),
    ]:
        cell = sheet.cell(row=total_row, column=column, value=round(value or 0, 2))
        cell.font = Font(bold=True)
    filename = (
        f"{slug_filename(client_name)}_GRA_PAYE_Schedule_"
        f"{slug_filename(payroll_run.month)}_{payroll_run.year}.xlsx"
    )
    return save_workbook(workbook, export_folder, filename)
