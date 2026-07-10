"""ICU union-dues distribution cascade.

The total ICU dues collected for a run are remitted through a fixed cascade
(from the DZ workbook's ICU sheet):

    total ─┬─ 50%  UNION  ─┬─ 75%  ICU-Accra
           │               ├─ 20%  Local
           │               └─  5%  ICU-Tema
           └─ 50%  EDFUND ─┬─ 80%  ICU-EDAC
                           └─ 20%  DCL-EEF

Each split assigns the rounding remainder to its last child, so the five leaf
payouts always sum back to the input total to the cent — which is exactly what
the ICU tie-out validation (Phase 4) asserts. These are the union's fixed
internal allocation shares (organisational structure, not a statutory rate).
"""
from dataclasses import dataclass

from app.money import money

UNION_SHARE = 0.50
EDFUND_SHARE = 0.50
UNION_ICU_ACCRA = 0.75
UNION_LOCAL = 0.20
UNION_ICU_TEMA = 0.05
EDFUND_ICU_EDAC = 0.80
EDFUND_DCL_EEF = 0.20


@dataclass
class UnionDistribution:
    total: float
    union: float
    edfund: float
    icu_accra: float
    local: float
    icu_tema: float
    icu_edac: float
    dcl_eef: float

    @property
    def leaves(self):
        return [self.icu_accra, self.local, self.icu_tema, self.icu_edac, self.dcl_eef]

    @property
    def total_payout(self):
        return money(sum(self.leaves))


def distribute_union_dues(total):
    """Split ``total`` ICU dues through the union cascade. The remainder of each
    split lands on its last child so the leaves tie back to ``total`` exactly."""
    total = money(total)
    union = money(total * UNION_SHARE)
    edfund = money(total - union)  # remainder -> edfund, so union+edfund == total

    icu_accra = money(union * UNION_ICU_ACCRA)
    local = money(union * UNION_LOCAL)
    icu_tema = money(union - icu_accra - local)  # remainder -> ICU-Tema

    icu_edac = money(edfund * EDFUND_ICU_EDAC)
    dcl_eef = money(edfund - icu_edac)  # remainder -> DCL-EEF

    return UnionDistribution(
        total=total,
        union=union,
        edfund=edfund,
        icu_accra=icu_accra,
        local=local,
        icu_tema=icu_tema,
        icu_edac=icu_edac,
        dcl_eef=dcl_eef,
    )
