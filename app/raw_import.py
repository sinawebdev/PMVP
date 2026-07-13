"""Raw-hours import helpers — surviving shared utility.

The old qtarpay parser (``detect_sheet_layout`` / ``parse_qtarpay`` /
``parse_master_tab`` / ``cross_validate`` / ``build_import_preview`` and the
``PAY_CODE_META`` registry) was retired when the tested ``app/raw_engine/``
package became the sole raw entry point — see tests/test_raw_engine_cleanup.py.
Those functions had no remaining importers; keeping them here only invited
confusion between the dead prototype and the live engine.

The one piece still shared across the app is :func:`normalise_emp_id` — the
staff-ID join key. It is kept here as its single source of truth and re-exported
by ``app.raw_engine.cleaning`` (48 call sites across distribution, employees,
payroll, the hourly calculator, and the raw engine itself depend on it).
"""

import re


def normalise_emp_id(raw: str) -> str:
    """'DCL 9' -> 'DCL9', 'DZ 048' -> 'DZ048'.

    Apply to every employee ID read from any sheet and to every DB lookup so
    that 'DCL 9' and 'DCL9' resolve to the same worker."""
    return re.sub(r"\s+", "", str(raw).strip().upper())
