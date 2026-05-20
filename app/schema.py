from sqlalchemy import inspect, text

from app import db


SCHEMA_COLUMNS = {
    "payroll_run": {
        "reviewed_by": "INTEGER",
        "reviewed_at": "DATETIME",
        "approved_at": "DATETIME",
        "rejected_at": "DATETIME",
        "total_unique_workers": "INTEGER DEFAULT 0",
        "source_sheet_name": "VARCHAR(160)",
        "detected_header_row": "INTEGER DEFAULT 0",
        "import_mode": "VARCHAR(40) DEFAULT 'single_client'",
    },
    "payment_voucher": {
        "gross_payroll": "FLOAT",
        "total_deductions": "FLOAT",
        "net_amount_payable": "FLOAT",
        "reviewed_by": "INTEGER",
        "date_approved": "DATETIME",
        "date_paid": "DATETIME",
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
}


def ensure_phase2_schema():
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    with db.engine.begin() as connection:
        for table_name, columns in SCHEMA_COLUMNS.items():
            if table_name not in existing_tables:
                continue
            existing_columns = {
                column["name"] for column in inspector.get_columns(table_name)
            }
            for column_name, column_type in columns.items():
                if column_name not in existing_columns:
                    connection.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
                    )
