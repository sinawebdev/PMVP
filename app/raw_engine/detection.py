"""Format detection and engine routing for the raw-hours path.

Two questions the upload flow asks before choosing a code path:

  1. Is this workbook a *rich* RAW-DATA seed workbook? (``is_rich_raw_data``)
  2. Has this company already been *seeded*? (``company_is_seeded``)

Routing rule (spec §2): a raw upload for a company with **no** WageRateProfile
rows goes to the seed flow; a company that is already seeded takes the thin
monthly path. Selection is per upload — there is no ``payroll_mode`` column.
"""
import openpyxl

from app.models import WageRateProfile
from app.raw_engine.mapping import (
    NAME_HEADER_LABEL,
    RAW_DATA_SHEET,
    find_name_header_row,
    HeaderError,
)


def open_raw_data_sheet(path):
    """Load the RAW DATA worksheet (cached values, not formulas). Raises
    HeaderError if the sheet is absent."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    if RAW_DATA_SHEET not in wb.sheetnames:
        raise HeaderError(
            f"Workbook has no '{RAW_DATA_SHEET}' sheet (found: {wb.sheetnames}); "
            "not a DZ-style rich raw workbook."
        )
    return wb[RAW_DATA_SHEET]


def is_rich_raw_data(path) -> bool:
    """True if ``path`` looks like a DZ-style rich RAW-DATA seed workbook: it
    has a RAW DATA sheet with the stacked NAMES header. Never raises — returns
    False on anything it can't confirm."""
    try:
        ws = open_raw_data_sheet(path)
        find_name_header_row(ws)
        return True
    except (HeaderError, Exception):
        return False


def company_is_seeded(client_company_id) -> bool:
    """True if the company already has raw-engine context (any WageRateProfile
    rows). Seeded companies take the thin monthly path; unseeded companies with
    a raw upload go to the seed flow."""
    if not client_company_id:
        return False
    return (
        WageRateProfile.query.filter_by(client_company_id=client_company_id).first()
        is not None
    )
