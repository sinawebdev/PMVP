import os
import re
from datetime import datetime, timezone

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from werkzeug.utils import secure_filename


COLUMN_ALIASES = {
    "staff_id": ["staff no", "staff id", "employee id", "emp id", "staff number", "employee no", "worker id"],
    "full_name": ["name", "employee name", "full name", "worker name", "employee"],
    "ssnit_number": ["ssnit no", "ssnit number", "ssnit id", "ssnit contribution number"],
    "bank_name": ["bank", "bank name"],
    "bank_account_number": ["account no", "account number", "bank account", "bank account number"],
    "basic_salary": ["basic", "basic salary", "base pay", "salary"],
    "transport_allowance": ["transport", "transport allowance", "transportation"],
    "housing_allowance": ["housing", "housing allowance", "rent allowance"],
    "overtime_pay": ["overtime", "ot", "overtime pay"],
    "other_allowances": ["other allowance", "other allowances", "allowances", "allowance"],
    "gross_pay": ["gross", "gross pay", "gross salary"],
    "paye": ["paye", "tax", "income tax", "paye tax"],
    "ssnit": ["ssnit", "social security", "ssnit contribution"],
    "other_deductions": ["deduction", "deductions", "other deductions"],
    "net_pay": ["net", "net pay", "net salary", "take home", "take home pay"],
}

MONEY_FIELDS = {
    "basic_salary",
    "transport_allowance",
    "housing_allowance",
    "overtime_pay",
    "other_allowances",
    "gross_pay",
    "paye",
    "ssnit",
    "other_deductions",
    "total_deductions",
    "net_pay",
}


def normalize_label(value):
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_worker(value):
    return normalize_label(value)


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
        mapping[column] = alias_lookup.get(normalized, "unmapped")
    return mapping


def find_header_row(file_path):
    known_labels = set()
    for field, aliases in COLUMN_ALIASES.items():
        known_labels.add(normalize_label(field))
        known_labels.update(normalize_label(alias) for alias in aliases)

    ext = file_path.rsplit(".", 1)[1].lower()
    if ext == "csv":
        sample = pd.read_csv(file_path, header=None, nrows=20, dtype=str)
    else:
        sample = pd.read_excel(file_path, header=None, nrows=20, dtype=str)
    rows = sample.fillna("").values.tolist()

    best_row = 1
    best_score = 0
    for row_index, row in enumerate(rows, start=1):
        labels = [normalize_label(value) for value in row if value not in (None, "")]
        score = sum(1 for label in labels if label in known_labels)
        if score > best_score:
            best_score = score
            best_row = row_index
    return max(best_row - 1, 0) if best_score >= 2 else 0


def read_excel_file(file_path):
    header_row = find_header_row(file_path)
    ext = file_path.rsplit(".", 1)[1].lower()
    if ext == "csv":
        df = pd.read_csv(file_path, header=header_row, dtype=str)
    elif ext == "xlsx":
        df = pd.read_excel(file_path, engine="openpyxl", header=header_row, dtype=str)
    else:
        df = pd.read_excel(file_path, header=header_row, dtype=str)
    df = df.dropna(how="all")
    df.columns = [str(column).strip() for column in df.columns]
    df = df.fillna("")
    mapping = map_columns(df.columns)
    return df, mapping


def detect_company_name(file_path, known_company_names=None):
    known_company_names = known_company_names or []
    sampled_values = []
    ext = file_path.rsplit(".", 1)[1].lower()
    if ext == "csv":
        sample = pd.read_csv(file_path, header=None, nrows=20, dtype=str)
    elif ext == "xlsx":
        sample = pd.read_excel(file_path, engine="openpyxl", header=None, nrows=20, dtype=str)
    else:
        sample = pd.read_excel(file_path, header=None, nrows=20, dtype=str)
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
            .strip()
        )
        cleaned = cleaned.replace("GHC", "").replace("GH₵", "").replace("₵", "")
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
    for _, source_row in df.iterrows():
        row = {}
        for original_column, field in mapping.items():
            if field == "unmapped":
                continue
            value = source_row.get(original_column, "")
            value = scalar_cell_value(value)
            row[field] = to_number(value) if field in MONEY_FIELDS else str(value).strip()

        identity = " ".join(
            str(row.get(field) or "")
            for field in ("staff_id", "full_name", "bank_name", "bank_account_number")
        )
        if not identity.strip() and not any(row.get(field, 0) for field in MONEY_FIELDS):
            continue
        if normalize_label(row.get("staff_id") or row.get("full_name")) in {
            "total",
            "grand total",
            "subtotal",
        }:
            continue

        for field in MONEY_FIELDS:
            row.setdefault(field, 0.0)
        row.setdefault("staff_id", "")
        row.setdefault("full_name", "")
        row.setdefault("ssnit_number", "")
        row.setdefault("bank_name", "")
        row.setdefault("bank_account_number", "")

        calculated_gross = (
            row["basic_salary"]
            + row["transport_allowance"]
            + row["housing_allowance"]
            + row["overtime_pay"]
            + row["other_allowances"]
        )
        if not row["gross_pay"] and calculated_gross:
            row["gross_pay"] = calculated_gross
        statutory_deductions = row["paye"] + row["ssnit"]
        if row["other_deductions"] >= statutory_deductions and statutory_deductions:
            row["total_deductions"] = row["other_deductions"]
        else:
            row["total_deductions"] = statutory_deductions + row["other_deductions"]
        if not row["net_pay"] and row["gross_pay"]:
            row["net_pay"] = row["gross_pay"] - row["total_deductions"]
        rows.append(row)
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


def save_uploaded_file(file_storage, upload_folder):
    filename = secure_filename(file_storage.filename)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    stored_filename = f"{timestamp}_{filename}"
    file_path = os.path.join(upload_folder, stored_filename)
    file_storage.save(file_path)
    return file_path, filename


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
