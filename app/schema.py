from sqlalchemy import inspect, text

from app import db


SCHEMA_COLUMNS = {
    "payroll_run": {
        "reviewed_by": "INTEGER",
        "reviewed_at": "timestamp",
        "approved_at": "timestamp",
        "rejected_at": "timestamp",
        "total_unique_workers": "INTEGER DEFAULT 0",
        "source_sheet_name": "VARCHAR(160)",
        "detected_header_row": "INTEGER DEFAULT 0",
        "import_mode": "VARCHAR(40) DEFAULT 'single_client'",
        "active_workers": "INTEGER DEFAULT 0",
        "inactive_workers": "INTEGER DEFAULT 0",
        "terminated_workers": "INTEGER DEFAULT 0",
        "on_leave_workers": "INTEGER DEFAULT 0",
        "unknown_status_workers": "INTEGER DEFAULT 0",
    },
    "payroll_item": {
        "status": "VARCHAR(40)",
        "service_line": "VARCHAR(120)",
        "job_role": "VARCHAR(120)",
        "payroll_month": "VARCHAR(40)",
        "ghana_card_number": "VARCHAR(80)",
        "bank_name": "VARCHAR(120)",
        "bank_account_number": "VARCHAR(80)",
        "momo_number": "VARCHAR(40)",
        "overtime_hours": "FLOAT DEFAULT 0",
        "tier_2_pension": "FLOAT DEFAULT 0",
        "loan_deduction": "FLOAT DEFAULT 0",
    },
    "payment_voucher": {
        "gross_payroll": "FLOAT",
        "total_deductions": "FLOAT",
        "net_amount_payable": "FLOAT",
        "reviewed_by": "INTEGER",
        "date_approved": "timestamp",
        "date_paid": "timestamp",
    },
    "remittance": {
        "date_paid": "DATE",
    },
    "expense": {
        "receipt_attachment": "VARCHAR(255)",
        "paid_by": "INTEGER",
        "approved_by": "INTEGER",
        "client_company_id": "INTEGER",
        "status": "VARCHAR(40)",
    },
    "import_batch": {
        "import_mode": "VARCHAR(40) DEFAULT 'single_client'",
        "source_sheet_name": "VARCHAR(160)",
        "payload_json": "TEXT",
    },
}


def ensure_phase2_schema():
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    dialect_name = db.engine.dialect.name
    with db.engine.begin() as connection:
        for table_name, columns in SCHEMA_COLUMNS.items():
            if table_name not in existing_tables:
                continue
            existing_columns = {
                column["name"] for column in inspector.get_columns(table_name)
            }
            for column_name, column_type in columns.items():
                if column_name not in existing_columns:
                    if dialect_name == "sqlite" and column_type == "timestamp":
                        column_type = "DATETIME"
                    elif dialect_name != "sqlite" and column_type == "DATETIME":
                        column_type = "TIMESTAMP"
                    connection.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
                    )
