"""Pivot consolidated hours into a staff x element matrix (the Summary Extract
shape). Both axes are derived from the data — nothing is dropped because it was
not in a pre-typed list (the Excel static-axis hazard)."""
from dataclasses import dataclass, field


@dataclass
class HoursMatrix:
    elements: list = field(default_factory=list)   # element codes, sorted (columns)
    staff_ids: list = field(default_factory=list)  # staff keys, sorted (rows)
    cells: dict = field(default_factory=dict)      # {staff: {element: hours}}
    grand_total: float = 0.0

    def hours(self, staff_id, element):
        return self.cells.get(staff_id, {}).get(element, 0.0)


def build_matrix(consolidated):
    """``consolidated``: ``{staff: {element: hours}}`` (see consolidation).
    Returns a :class:`HoursMatrix` with derived, sorted axes and the grand total."""
    elements = sorted({code for codes in consolidated.values() for code in codes})
    staff_ids = sorted(consolidated)
    grand_total = sum(
        h for codes in consolidated.values() for h in codes.values()
    )
    return HoursMatrix(
        elements=elements,
        staff_ids=staff_ids,
        cells={s: dict(consolidated[s]) for s in staff_ids},
        grand_total=grand_total,
    )
