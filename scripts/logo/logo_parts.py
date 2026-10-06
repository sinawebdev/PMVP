"""Building blocks of the animated logo, used by payrolla_logo_anim.py.

The mark's geometry lives here because it has two consumers: the Blender build
extrudes and animates it, and app/static/img/payrolla-icon.svg draws the same
numbers flat (that file's header maps them to SVG units). Change one, change
the other. Nothing here imports bpy; mathutils ships with Blender.
"""

import math

from mathutils import Vector

# ------------------------------------------------------------------ geometry
# Mark designed on a grid where the ribbon is 1 unit wide:
#   left column x 0..1, top band y 0.1..1.1, bottom band y -1.1..-0.1,
#   bowl turns on radius 0.6 so the counter is a thin 0.2 slit,
#   leaf-shaped stem hangs below y -1.1 down to -2.35.
# OFF recentres the finished mark on the origin.
W = 1.0                   # ribbon width (everything is in ribbon-width units)
OFF = Vector((-1.45, 0.625, 0.0))
MARK_W, MARK_H = 2.9, 3.45

R_BOWL = 0.6
N_UP, N_TOP, N_ARC, N_BOT = 10, 8, 40, 6


def turtle_program():
    """Segment lengths and the turn (radians, negative = clockwise) applied
    at the start of each segment. Final path: up the left column, sharp
    90 deg corner, along the top, 180 deg around the bowl, back along the bottom."""
    segs = []
    segs += [(1.7 / N_UP, 0.0)] * N_UP
    segs += [(1.3 / N_TOP, -math.pi / 2 if i == 0 else 0.0) for i in range(N_TOP)]
    step = R_BOWL * math.pi / N_ARC
    for i in range(N_ARC):
        segs.append((step, -math.pi / (2 * N_ARC) if i == 0 else -math.pi / N_ARC))
    segs += [(0.8 / N_BOT, -math.pi / (2 * N_ARC) if i == 0 else 0.0) for i in range(N_BOT)]
    return segs


PROGRAM = turtle_program()
TOTAL_TURN = sum(abs(t) for _, t in PROGRAM)       # 270 deg


def ribbon_outline(turn_budget):
    """Left/right edge points of the ribbon when only `turn_budget` radians of
    its total bending have happened, applied from the base outward, so the
    strip winds up like a real ribbon and its length never changes."""
    pos = Vector((0.5, -1.1))
    heading = math.pi / 2
    remaining = turn_budget
    centre = [pos.copy()]
    heads = []
    for length, turn in PROGRAM:
        applied = math.copysign(min(abs(turn), remaining), turn) if turn else 0.0
        remaining -= abs(applied)
        heading += applied
        heads.append(heading)
        pos = pos + Vector((math.cos(heading), math.sin(heading))) * length
        centre.append(pos.copy())
    lefts, rights = [], []
    for i, p in enumerate(centre):
        h_in = heads[max(i - 1, 0)]
        h_out = heads[min(i, len(heads) - 1)]
        n_in = Vector((-math.sin(h_in), math.cos(h_in)))
        n_out = Vector((-math.sin(h_out), math.cos(h_out)))
        m = (n_in + n_out).normalized()
        miter = (W / 2) / max(math.cos((h_out - h_in) / 2), 0.2)   # sharp mitred corners
        lefts.append(p + m * miter)
        rights.append(p - m * miter)
    # At a sharp corner the inside edge would fold back on itself (points just
    # before/after the corner overshoot the true inner corner). Collapse them
    # onto the corner point; the Weld modifier then removes the zero-area faces.
    for i in range(1, len(centre) - 1):
        turn = heads[i] - heads[i - 1]
        if abs(turn) < math.radians(20):
            continue
        inner = rights if turn < 0 else lefts
        M = inner[i].copy()
        d_in = Vector((math.cos(heads[i - 1]), math.sin(heads[i - 1])))
        d_out = Vector((math.cos(heads[i]), math.sin(heads[i])))
        for j in range(i - 1, -1, -1):
            if (inner[j] - M).dot(d_in) > 0:
                inner[j] = M.copy()
        for j in range(i + 1, len(inner)):
            if (inner[j] - M).dot(d_out) < 0:
                inner[j] = M.copy()
    return lefts, rights


def ribbon_coords(turn_budget):
    lefts, rights = ribbon_outline(turn_budget)
    co = []
    for lp, rp in zip(lefts, rights):
        co.append((lp.x + OFF.x, lp.y + OFF.y, 0.0))
        co.append((rp.x + OFF.x, rp.y + OFF.y, 0.0))
    return co


def stem_outline():
    """Leaf shape below the column: a quarter ellipse (rx 1, ry 1.25)."""
    pts = [Vector((0.0, -1.1)), Vector((1.0, -1.1))]
    for i in range(1, 25):
        a = -math.pi / 2 * i / 24
        pts.append(Vector((math.cos(a), -1.1 + 1.25 * math.sin(a))))
    return list(reversed(pts))           # counter-clockwise -> face normal +Z


# ------------------------------------------------------------------ colour
def lin(hex_str, a=1.0):
    """sRGB hex -> linear RGBA, which is what Blender's colour sockets take."""
    h = hex_str.lstrip("#")
    srgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return (*[c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in srgb], a)


# ------------------------------------------------------------------ keyframes
def _fcurves(owner):
    ad = owner.animation_data
    if not ad or not ad.action:
        return []
    act = ad.action
    out = []
    if hasattr(act, "layers") and len(act.layers):
        for layer in act.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    out.extend(bag.fcurves)
    elif hasattr(act, "fcurves"):
        out = list(act.fcurves)
    return out


def key(owner, path, frame, value, index=-1, out="BEZIER", ease="EASE_IN_OUT", back=None):
    """Keyframe owner.path at frame. `out`/`ease` shape the segment LEAVING this key."""
    if index >= 0:
        owner.path_resolve(path)[index] = value
    elif "." in path:
        parent, attr = path.rsplit(".", 1)
        setattr(owner.path_resolve(parent), attr, value)
    else:
        setattr(owner, path, value)
    owner.keyframe_insert(data_path=path, index=index, frame=frame)
    for fc in _fcurves(owner):
        if fc.data_path == path and (index < 0 or fc.array_index == index):
            for kp in fc.keyframe_points:
                if round(kp.co[0]) == frame:
                    kp.interpolation = out
                    kp.easing = ease
                    if back is not None:
                        kp.back = back
