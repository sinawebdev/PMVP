"""Run the compute engine over a parsed seed context (the seed month).

Bridges Phase 1 (parsed :class:`SeedContext`) and Phase 2 (pure
:func:`compute_payslip`): apply each employee's rate table to their hours,
fold in the month's lump adjustments, and produce a full payslip per worker.
The same ``build_inputs`` seam serves the thin monthly path (Phase 3) — only
the source of hours and adjustments differs.
"""
from app.raw_engine.calc import apply_rates
from app.raw_engine.compute import PayslipInputs, compute_payslip


def assemble_inputs(
    staff_id,
    hours_by_code,
    rate_lookup,
    *,
    pay_type="hourly",
    employee_id=None,
    basic_fallback=0.0,
    is_icu_member=False,
    bonus=0.0,
    other_allowance=0.0,
    pay_difference=0.0,
    tax_relief_monthly=0.0,
    provident_fund=0.0,
    loan=0.0,
    donations=0.0,
    other_deductions=0.0,
    welfare=0.0,
    bonus_concession_used_ytd=0.0,
) -> PayslipInputs:
    """Cost one worker's hours (x rate table) plus the month's lump adjustments
    into :class:`PayslipInputs`. Shared by the seed month (Phase 2) and the thin
    monthly upload (Phase 3) — only the source of the hours/adjustments differs.
    Salaried admin (no basic rate line) falls back to the flat basic wage."""
    applied = apply_rates(hours_by_code, rate_lookup)
    # Classification is explicit (Employee.pay_type), never re-inferred from the
    # rate table. An hourly worker's basic is hours x rate — 0 when they worked
    # no normal hours, never the stale flat basic; only a salaried worker falls
    # back to the flat basic wage. This is what stops a zero-hours monthly
    # template paying an hourly worker their previous month's basic.
    if str(pay_type or "").lower() == "salaried":
        basic = applied.basic_wage or basic_fallback
    else:
        basic = applied.basic_wage
    allowances = applied.shift_allowances + other_allowance

    inputs = PayslipInputs(
        staff_id=staff_id,
        employee_id=employee_id,
        basic_wage=basic,
        overtime_pay=applied.overtime_pay,
        allowances=allowances,
        bonus=bonus,
        pay_difference=pay_difference,
        tax_relief_monthly=tax_relief_monthly,
        provident_fund=provident_fund,
        is_icu_member=is_icu_member,
        loan=loan,
        welfare=welfare,
        donations=donations,
        other_deductions=other_deductions,
        bonus_concession_used_ytd=bonus_concession_used_ytd,
    )
    inputs.missing_rate_codes = applied.missing_rate_codes
    return inputs


def build_inputs(emp, bonus_concession_used_ytd=0.0) -> PayslipInputs:
    """Cost one :class:`~app.raw_engine.seed.SeedEmployee` (parsed seed month)
    into :class:`PayslipInputs` — rates come from the employee's parsed rate
    table."""
    rate_lookup = {r.pay_code: (r.hourly_rate, r.category) for r in emp.rates}
    return assemble_inputs(
        emp.staff_id,
        emp.raw_hours,
        rate_lookup,
        pay_type="hourly" if emp.is_hourly else "salaried",
        basic_fallback=emp.basic_salary,
        is_icu_member=emp.icu_member,
        bonus=emp.bonus,
        other_allowance=emp.other_allowance,
        pay_difference=emp.pay_difference,
        tax_relief_monthly=emp.tax_relief_monthly,
        provident_fund=emp.provident_fund,
        loan=emp.loan,
        donations=emp.donations,
        other_deductions=emp.other_deduction,
        welfare=emp.welfare,
        bonus_concession_used_ytd=bonus_concession_used_ytd,
    )


def compute_seed_month(context, statutory_rate):
    """Compute a payslip for every employee in ``context`` under
    ``statutory_rate``. Returns ``{staff_id: Payslip}``."""
    payslips = {}
    for emp in context.employees:
        inputs = build_inputs(emp)
        payslips[emp.staff_id] = compute_payslip(inputs, statutory_rate)
    return payslips
