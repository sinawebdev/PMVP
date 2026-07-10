"""Turn consolidated hours into money by applying each employee's rate table.

allowance/basic amount = hours x rate; overtime pay = OT hours x OT rate. The
per-element amounts are bucketed by statutory category (basic / overtime /
allowance) so the tax chain can treat each correctly. A worked element with no
configured rate is never guessed — its pay code is collected on
``missing_rate_codes`` for the operator to seed and recompute.
"""
from dataclasses import dataclass, field

from app.models import WageRateProfile
from app.money import money


@dataclass
class RateApplication:
    basic_wage: float = 0.0        # normal-hours pay (attracts SSF + ordinary PAYE)
    overtime_pay: float = 0.0      # OT element pay (concessionary tax)
    shift_allowances: float = 0.0  # shift-allowance pay (ordinary taxable income)
    lines: list = field(default_factory=list)  # (code, category, hours, rate, amount)
    missing_rate_codes: list = field(default_factory=list)


_CATEGORY_BUCKET = {
    WageRateProfile.CATEGORY_BASIC: "basic_wage",
    WageRateProfile.CATEGORY_OVERTIME: "overtime_pay",
    WageRateProfile.CATEGORY_ALLOWANCE: "shift_allowances",
}


def apply_rates(hours_by_code, rate_lookup):
    """Apply rates to one employee's consolidated hours.

    ``hours_by_code``: ``{pay_code: hours}``.
    ``rate_lookup``: ``{pay_code: (hourly_rate, category)}``.
    Returns a :class:`RateApplication`.
    """
    result = RateApplication()
    for pay_code, hours in hours_by_code.items():
        if not hours:
            continue
        entry = rate_lookup.get(pay_code)
        if entry is None:
            if pay_code not in result.missing_rate_codes:
                result.missing_rate_codes.append(pay_code)
            continue
        hourly_rate, category = entry
        amount = money(float(hours) * float(hourly_rate))
        result.lines.append((pay_code, category, float(hours), float(hourly_rate), amount))
        bucket = _CATEGORY_BUCKET.get(category)
        if bucket:  # a 'bonus'-category rate line would be ignored here — bonus
            setattr(result, bucket, money(getattr(result, bucket) + amount))
    return result
