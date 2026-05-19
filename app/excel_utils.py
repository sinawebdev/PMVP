import os
import re
from datetime import datetime, timezone

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from werkzeug.utils import secure_filename


COLUMN_ALIASES = {
    "staff_id": ["staff no", "staff id", "employee id", "emp id", "staff number"],
    "full_name": ["name", "employee name", "full name", "worker name"],
    "ssnit_number": ["ssnit no", "ssnit number", "ssnit id"],
    "basic_salary": ["basic", "basic salary", "base pay", "salary"],
    "transport_allowance": ["transport", "transport allowance", "transportation"],
    "housing_allowance": ["housing", "housing allowance", "rent allowance"],
    "overtime_pay": ["overtime", "ot", "overtime pay"],
    "other_allowances": ["other allowance", "other allowances", "allowances"],
    "gross_pay": ["gross", "gross pay", "gross salary"],
    "paye": ["paye", "tax", "income tax"],
    "ssnit": ["ssnit", "social security"],
    "other_deductions": ["deduction", "deductions", "other deductions"],
    "net_pay": ["net", "net pay", "take home", "take home pay"],
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
    return "." in filename and filename.rsplit(".", 1)[1].lower() == "xlsx"


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
    workbook = load_workbook(file_path, read_only=True, data_only=True)
    sheet = workbook.active
    known_labels = set()
    for field, aliases in COLUMN_ALIASES.items():
        known_labels.add(normalize_label(field))
        known_labels.update(normalize_label(alias) for alias in aliases)

    best_row = 1
    best_score = 0
    for row_index, row in enumerate(sheet.iter_rows(min_row=1, max_row=20, values_only=True), start=1):
        labels = [normalize_label(value) for value in row if value not in (None, "")]
        score = sum(1 for label in labels if label in known_labels)
        if score > best_score:
            best_score = score
            best_row = row_index
    workbook.close()
    return max(best_row - 1, 0) if best_score >= 2 else 0


def read_excel_file(file_path):
    header_row = find_header_row(file_path)
    df = pd.read_excel(file_path, engine="openpyxl", header=header_row)
    df = df.dropna(how="all")
    df.columns = [str(column).strip() for column in df.columns]
    df = df.fillna("")
    mapping = map_columns(df.columns)
    return df, mapping


def detect_company_name(file_path, known_company_names=None):
    known_company_names = known_company_names or []
    workbook = load_workbook(file_path, read_only=True, data_only=True)
    sheet = workbook.active
    sampled_values = []
    for row in sheet.iter_rows(min_row=1, max_row=20, values_only=True):
        for value in row:
            if value not in (None, ""):
                sampled_values.append(str(value).strip())
    workbook.close()

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
        return float(cleaned or 0)
    except (TypeError, ValueError):
        return 0.0


def mapped_rows_from_dataframe(df, mapping):
    rows = []
    for _, source_row in df.iterrows():
        row = {}
        for original_column, field in mapping.items():
            if field == "unmapped":
                continue
            value = source_row.get(original_column, "")
            row[field] = to_number(value) if field in MONEY_FIELDS else str(value).strip()

        for field in MONEY_FIELDS:
            row.setdefault(field, 0.0)
        row.setdefault("staff_id", "")
        row.setdefault("full_name", "")
        row.setdefault("ssnit_number", "")

        calculated_gross = (
            row["basic_salary"]
            + row["transport_allowance"]
            + row["housing_allowance"]
            + row["overtime_pay"]
            + row["other_allowances"]
        )
        if not row["gross_pay"] and calculated_gross:
            row["gross_pay"] = calculated_gross
        row["total_deductions"] = (
            row["paye"] + row["ssnit"] + row["other_deductions"]
        )
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
    headers = ["Voucher", "Client", "Month", "Workers", "Amount", "Status", "Prepared By"]
    rows = [
        [
            voucher.voucher_number,
            voucher.payroll_run.client_company.name if voucher.payroll_run.client_company else "",
            f"{voucher.payroll_run.month} {voucher.payroll_run.year}",
            voucher.payroll_run.total_workers,
            voucher.total_amount,
            voucher.status,
            voucher.preparer.name if voucher.preparer else "",
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
