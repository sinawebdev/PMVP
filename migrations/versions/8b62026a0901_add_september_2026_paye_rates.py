"""Add GRA PAYE bands effective 1 September 2026, preserving rate history.

Revision ID: 8b62026a0901
Revises: 3f2984c70c03
Create Date: 2026-10-20

Production disables boot seeding, so an existing database needs this data
migration as well as the updated fresh-install seed. Only a missing September
version is added; older/custom versions and completed payrolls are untouched.
Non-PAYE settings come from the last version before September, not a future one.

Source: https://gra.gov.gh/news/portfolio/amendments-income-tax-act-2026-act-1178/
Archived evidence: docs/research/gra-paye-2026-09-01/.
"""

import json
from datetime import date, datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "8b62026a0901"
down_revision = "3f2984c70c03"
branch_labels = None
depends_on = None

EFFECTIVE_FROM = date(2026, 9, 1)
# Frozen migration data: future changes to the application seed must not
# change the meaning of this historical migration.
PAYE_BANDS = [
    {"over": 50000, "rate": 0.35, "base": 13621.10},
    {"over": 19668, "rate": 0.30, "base": 4521.50},
    {"over": 3668, "rate": 0.25, "base": 521.50},
    {"over": 768, "rate": 0.175, "base": 14.00},
    {"over": 668, "rate": 0.10, "base": 4.00},
    {"over": 588, "rate": 0.05, "base": 0},
]
CARRY_FIELDS = (
    "ssf_employee_rate", "ssf_employer_rate", "overtime_rate_low",
    "overtime_rate_high", "overtime_basic_threshold", "bonus_rate",
    "bonus_annual_basic_threshold", "icu_member_rate",
    "overtime_junior_monthly_threshold",
)


def upgrade():
    bind = op.get_bind()
    rates = sa.table(
        "statutory_rates",
        sa.column("effective_from", sa.Date()),
        sa.column("paye_bands_json", sa.Text()),
        sa.column("notes", sa.Text()),
        sa.column("created_at", sa.DateTime()),
        *(sa.column(name, sa.Float()) for name in CARRY_FIELDS),
    )
    existing = bind.execute(
        sa.select(rates.c.effective_from).where(
            rates.c.effective_from == EFFECTIVE_FROM
        )
    ).first()
    if existing is not None:
        return
    previous = bind.execute(
        sa.select(rates)
        .where(rates.c.effective_from < EFFECTIVE_FROM)
        .order_by(rates.c.effective_from.desc())
        .limit(1)
    ).mappings().first()
    if previous is None:
        # A fresh empty install is populated by seed_statutory_rates(), or by
        # an operator's configured initial rates. Do not invent ancillary rates.
        return
    bind.execute(
        rates.insert().values(
            effective_from=EFFECTIVE_FROM,
            paye_bands_json=json.dumps(PAYE_BANDS),
            notes=(
                "GRA Act 1178 PAYE schedule, effective 2026-09-01. "
                "Source: https://gra.gov.gh/news/portfolio/"
                "amendments-income-tax-act-2026-act-1178/ . "
                "Non-PAYE settings carried from "
                f"{previous['effective_from'].isoformat()}."
            ),
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            **{name: previous[name] for name in CARRY_FIELDS},
        )
    )


def downgrade():
    # Keep financial configuration: this date may have pre-existed the upgrade
    # or been edited since, and deleting it would alter historical calculations.
    pass
