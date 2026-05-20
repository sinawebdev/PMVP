# Chrisnat Payroll MVP

Phase 1 MVP for Chrisnat Limited Ghana. This is a Flask and SQLite web app that bridges Excel payroll files into a simple review, approval, accounts, and export workflow.

## What This MVP Does

- Authenticates users with role-based access.
- Manages client companies and employee records.
- Imports `.xlsx` payroll files for a selected client company.
- Suggests smart column mappings from common Excel header names.
- Counts unique workers without simply counting rows.
- Shows validation warnings before creating a payroll run.
- Creates company-specific payroll runs.
- Allows payroll review, MD/Admin approval, and Excel export.
- Generates downloadable individual employee payslip PDFs from payroll items.
- Auto-prepares payment vouchers plus PAYE and SSNIT remittance records after approval.
- Tracks simple operating expenses.
- Adds imported payroll workers to the employee records section.
- Provides a proposal drafting section tied to client companies.

## Setup

```bash
cd chrisnat-payroll-mvp
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Open `http://127.0.0.1:5000`.

The app creates the SQLite database and seed data automatically on first run.

## Render Deployment

This repo includes `render.yaml` and a `Procfile`.

Render settings:

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn run:app --bind 0.0.0.0:10000`
- Health check: `/health`
- Database: Render Postgres through `DATABASE_URL`

Push the project to GitHub, GitLab, or Bitbucket, then open the Render Blueprint flow for that repository.

## Default Login Details

| Role | Email | Password |
| --- | --- | --- |
| Admin | admin@chrisnat.local | password123 |
| MD | md@chrisnat.local | password123 |
| Payroll Officer | payroll@chrisnat.local | password123 |
| Accounts Officer | accounts@chrisnat.local | password123 |

## Uploading Payroll Excel

Go to **Upload Payroll Excel**, select a client company, choose the payroll month/year, then upload a `.xlsx` file.

Expected columns can use common names such as:

- `staff id`, `staff no`, `employee id`
- `name`, `employee name`, `full name`
- `basic`, `basic salary`, `base pay`
- `transport`, `transport allowance`
- `housing`, `housing allowance`
- `overtime`, `ot`, `overtime pay`
- `gross`, `gross pay`
- `paye`, `tax`, `income tax`
- `ssnit`, `social security`
- `deductions`, `other deductions`
- `net`, `net pay`, `take home`

Unknown columns are shown as `unmapped` on the preview page.

## Client Company Separation

Every payroll upload must be attached to a selected client company. The preview page shows:

- Selected client company
- Detected company name from the Excel file, when found
- Total rows found
- Total unique workers
- Duplicate entries found
- Suggested mappings
- Validation warnings

Payroll exports include the client name and payroll month in the filename.

## Validation Warnings

The MVP flags missing staff ID, missing employee name, missing SSNIT number, missing or negative net pay, duplicate workers in the same client payroll, worker overlap across clients for the same month, repeated uploads for the same client/month, gross/basic mismatches, net pay mismatches, empty PAYE, and empty SSNIT.

This is not a Ghana tax compliance engine. Uploaded PAYE and SSNIT values are used as-is for Phase 1.

## Next Improvements After Questionnaire Answers

- Confirm exact approval levels and audit trail requirements.
- Add Flask-Migrate migrations before production-style changes.
- Add configurable statutory rates and payroll calculation policies.
- Add detailed invoice generation per client company.
- Add payslip PDF generation through a dedicated PDF service.
- Add stronger Excel templates, import history, and rollback tools.
- Add automated tests around imports, approvals, exports, permissions, and finance calculations.
