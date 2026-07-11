"""Union (ICU) dues: ``icu_member_rate`` of the basic wage for seeded members,
0 otherwise. Post-tax, derived (never uploaded), and config-rated.

Verified against DZ cell BC: 3% of basic 631.35 -> 18.94 for a member; George
Akoto (non-member) pays 0.
"""


def icu_dues(rate, basic_wage, is_member):
    """ICU union dues for this rate version — 0 for non-members."""
    return rate.icu_dues(basic_wage, is_member)
