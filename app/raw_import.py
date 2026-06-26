"""Raw-hours (qtarpay) import pipeline — billable add-on.

Parses raw-hours Excel workbooks, cross-validates the long-format qtarpay sheet
against the wide Master sheet, and builds a preview payload. This module stores
*hours only*: it never computes gross pay, overtime multipliers, or rates — that
is a separate Chrisnat operator step.
"""

import re

import pandas as pd


# --- Pay code registry -----------------------------------------------------
PAY_CODE_META = {
    "ABNH01": {"label": "Normal Hours",           "type": "normal",    "multiplier": 1.0},
    "OV4123": {"label": "Overtime (weekday)",      "type": "overtime",  "multiplier": None},
    "OV4124": {"label": "Overtime (Saturday)",     "type": "overtime",  "multiplier": None},
    "OV4125": {"label": "Overtime (Sun/Holiday)",  "type": "overtime",  "multiplier": None},
    "SH4131": {"label": "Afternoon allowance",     "type": "allowance", "multiplier": 0.20},
    "SH4119": {"label": "Night allowance",         "type": "allowance", "multiplier": 0.35},
    "SH4133": {"label": "6to6 Night allowance",    "type": "allowance", "multiplier": 0.45},
    "SH4180": {"label": "4-crew shift allowance",  "type": "allowance", "multiplier": 0.43},
}


def normalise_emp_id(raw: str) -> str:
    """'DCL 9' -> 'DCL9', 'DZ 048' -> 'DZ048'.

    Apply to every employee ID read from any sheet and to every DB lookup so
    that 'DCL 9' and 'DCL9' resolve to the same worker."""
    return re.sub(r"\s+", "", str(raw).strip().upper())


def detect_sheet_layout(xl: pd.ExcelFile) -> dict:
    """Returns { 'qtarpay': sheet_name, 'master': sheet_name } for sheets that
    match the expected patterns. Keys are absent if the matching sheet is not
    found. Roles are sniffed from content, not fixed sheet names."""
    layout = {}
    for name in xl.sheet_names:
        df = xl.parse(name, header=None, nrows=5)
        if df.empty:
            continue
        flat = df.iloc[0].astype(str).str.lower().tolist()
        if any("column1" in c for c in flat) and any("column2" in c for c in flat):
            layout["qtarpay"] = name
        elif any("entity" in c or "employee id" in c for c in flat[:3]):
            layout["master"] = name
    return layout


def parse_qtarpay(df_raw: pd.DataFrame):
    """Long-format: one row per (employee, pay code). Columns 0-3 are data;
    columns 5+ are a summary block that is ignored.

    Returns:
        employees : { normalised_id: { pay_code: hours_float } }
        warnings  : [ { employee_id, pay_code, issue } ]
    """
    df = df_raw.iloc[1:, :4].copy()
    df.columns = ["employee_id", "pay_code", "hours", "description"]
    df = df.dropna(subset=["employee_id", "pay_code"])
    df["employee_id"] = df["employee_id"].astype(str).apply(normalise_emp_id)
    df["pay_code"] = df["pay_code"].astype(str).str.strip()
    df["hours"] = pd.to_numeric(df["hours"], errors="coerce")

    employees, warnings = {}, []

    for _, row in df.iterrows():
        emp_id = row["employee_id"]
        code = row["pay_code"]
        hours = row["hours"]

        if not emp_id or emp_id == "NAN":
            continue
        if pd.isna(hours):
            warnings.append({"employee_id": emp_id, "pay_code": code, "issue": "hours missing"})
            continue
        if code not in PAY_CODE_META:
            warnings.append({"employee_id": emp_id, "pay_code": code, "issue": "unknown pay code"})

        employees.setdefault(emp_id, {})[code] = float(hours)

    return employees, warnings


def parse_master_tab(df_raw: pd.DataFrame) -> dict:
    """Wide Master tab for cross-validation against qtarpay.
    Row 0-1: title rows. Row 2: pay codes. Row 3: labels. Row 4+: data.
    Last row is a TOTAL row — skipped via the nan/total guard.
    Returns { normalised_id: { pay_code: hours } }."""
    pay_codes = [str(c).strip() for c in df_raw.iloc[2, 1:9].tolist()]
    result = {}
    for _, row in df_raw.iloc[4:].iterrows():
        emp_id = normalise_emp_id(str(row.iloc[0]))
        if not emp_id or emp_id.lower() in ("nan", "total"):
            continue
        record = {}
        for i, code in enumerate(pay_codes):
            val = pd.to_numeric(row.iloc[i + 1], errors="coerce")
            if pd.notna(val) and val > 0:
                record[code] = float(val)
        result[emp_id] = record
    return result


def cross_validate(qtarpay: dict, master: dict) -> list:
    """Returns list of { employee_id, pay_code, qtarpay_hours, master_hours, diff }
    for any hours that don't match between the two sheets. Warnings, not blockers."""
    discrepancies = []
    for emp_id in set(qtarpay) | set(master):
        q = qtarpay.get(emp_id, {})
        m = master.get(emp_id, {})
        for code in set(q) | set(m):
            q_h = q.get(code, 0)
            m_h = m.get(code, 0)
            if abs(q_h - m_h) > 0.01:
                discrepancies.append({
                    "employee_id": emp_id,
                    "pay_code": code,
                    "qtarpay_hours": q_h,
                    "master_hours": m_h,
                    "diff": round(q_h - m_h, 2),
                })
    return discrepancies


def build_import_preview(employees: dict, db_employees: dict) -> dict:
    """Match parsed employees against existing DB records. Does NOT write to DB.

    db_employees: { normalised_id: { 'name': str, ... } }
    Returns { 'matched': [...], 'unmatched': [...] } where 'unmatched' are file
    IDs with no DB record."""
    matched, unmatched = [], []

    for emp_id, pay_codes in employees.items():
        db_rec = db_employees.get(emp_id)
        if not db_rec:
            unmatched.append({"employee_id": emp_id, "pay_codes": pay_codes})
            continue

        line_items = []
        for code, hours in pay_codes.items():
            meta = PAY_CODE_META.get(code, {})
            line_items.append({
                "pay_code": code,
                "label": meta.get("label", code),
                "type": meta.get("type", "unknown"),
                "hours": hours,
            })

        matched.append({
            "employee_id": emp_id,
            "name": db_rec.get("name", emp_id),
            "line_items": line_items,
        })

    return {"matched": matched, "unmatched": unmatched}
