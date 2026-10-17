# -*- coding: utf-8 -*-
"""The X sequence (2026-10-16, final): every X mark of a floor decided once.

Step 3 finds every X mark on the floor - two diagonals crossing at their
middles, on any layer, of any size - and decides each one ONCE, here, in
this order. Each step only sees the X marks no earlier step claimed, and
nothing after it (the element engine, the geometry engine, the final
drawing, the area statement, a family picked by hand) changes the answer.

  1  every X is a VOID to start with;
  2  SHAFT   a wall within wall_gap_mm of a side (after any part spilled
             into the wall is cut back) - and not inside a wall, not round,
             not a column's cross, not larger than the void size, not a
             stair's well between its flights, not an unnamed X with a
             cut-out tag beside it, and not what the lift rules take;
  3  LIFT    walled on lift_walled_sides sides or more, and lift size or a
             LIFT label (a label naming a shaft keeps a lift-size X a shaft);
  4  WINDOW  inside a wall, with evidence: glazing, the window finder's
             window, or a window tag nearer than any cut-out tag;
  5  CUT-OUT inside a wall with no window evidence; or a cut-out tag beside
             it; or unnamed with no wall within wall_gap_mm - never one
             standing in a room or a staircase, a column's cross, or one
             larger than the void size;
  6  COLUMN  on a column, ringed by a steel column's outline, or inside a
             solid fill no larger than itself;
  7  what is left stays a VOID.

x_facts() reads, for one X, everything the sequence looks at - the walls
round it, its tags, a column under it - and decides nothing; resolve_x()
is the sequence. "Inside a wall": the wall carries on in line past at least
one end of the X at the X's own thickness, faces aligned (run test), or the
X lies inside a real wall piece (one that runs on past it); an opening is
no thicker than opening_max_mm.

A wall piece holding the X of a shaft candidate is not wall unless the X is
an opening in it (rule 2-1: a wall does not change its thickness except at
a junction or a column, so a piece that does, around an X, is the space the
X marks - P285's RWP chase, 350 mm between a 100 mm wall and a column).

Tags beside an X (tag_kind): a short code ending in D, or D-1, FMD-2,
SD-3A, is a door tag; one ending in W, or W-1, ALW3, a window tag; C-1,
C-10a a cut-out tag. A shaft's name (CHW, ...) is never a tag. A door tag
names nothing; a window tag makes an opening a window; a cut-out tag
makes an X that no shaft name claims a cut-out.

Shapely polygons, millimetres. Pure functions: dxf_reader calls x_facts()
for every X and resolve_x() once for each.
"""
from __future__ import annotations

import math
import re

from .config import CONFIG

_XP = CONFIG.get("x_placement") or {}
GAP_MM = float(_XP.get("wall_gap_mm", 50.0))
RUN_TOL_MM = float(_XP.get("run_tol_mm", 40.0))
RUN_PROBE_MM = float(_XP.get("run_probe_mm", 400.0))
RUN_COVER = float(_XP.get("run_cover", 0.7))
TAG_REACH_MM = float(_XP.get("tag_reach_mm", 600.0))
FRAME_TOL_MM = float(_XP.get("frame_tol_mm", 20.0))
IN_WALL_SHARE = float(_XP.get("in_wall_share", 0.9))
SPILL_MIN_SHARE = float(_XP.get("spill_min_share", 0.02))
COLUMN_HATCH_RATIO = float(_XP.get("column_hatch_ratio", 2.5))
OPENING_MAX_MM = float(_XP.get("opening_max_mm", 600.0))
COLUMN_MAX_SIDE_MM = float(_XP.get("column_max_side_mm", 1200.0))
COLUMN_RING_MM = tuple(_XP.get("column_ring_mm") or (5.0, 60.0))
SIDE_SHARE = float(_XP.get("side_share", 0.5))
LABEL_WALL_CROSS_MM = float(_XP.get("label_wall_cross_mm", 80.0))
RUN_ON_MM = float(_XP.get("run_on_mm", 100.0))
# step 3: a lift's X - sides and aspect, and how many of its sides stand on
# a wall (a lift is walled round; its door is an opening in one of them)
LIFT_SIDE_MM = tuple(_XP.get("lift_side_mm") or (1800.0, 3600.0))
LIFT_ASPECT_MAX = float(_XP.get("lift_aspect_max", 1.5))
LIFT_WALLED_SIDES = int(_XP.get("lift_walled_sides", 3))
# a slot: an X narrower than slot_side_mm (or under slot_area_sqm) that the
# wall mass holds for slot_in_wall_share of it is inside the wall
SLOT_SIDE_MM = float(_XP.get("slot_side_mm", 150.0))
SLOT_AREA_SQM = float(_XP.get("slot_area_sqm", 0.03))
SLOT_IN_WALL_SHARE = float(_XP.get("slot_in_wall_share", 0.99))
# an X larger than this is a void (x_marks.void_area_threshold_sqm)
VOID_MIN_SQM = float((CONFIG.get("x_marks") or {}).get("void_area_threshold_sqm", 30.0))

CLASSES = ("shaft", "lift", "window", "cutout", "column", "void")

DOOR_TAG_RE = re.compile(_XP.get("door_tag_re", r"^[A-Z]{0,4}D(?:[\s\-]?\d+[A-Z]?)?$"))
WINDOW_TAG_RE = re.compile(_XP.get("window_tag_re", r"^[A-Z]{0,4}W(?:[\s\-]?\d+[A-Z]?)?$"))
CUTOUT_TAG_RE = re.compile(_XP.get("cutout_tag_re", r"^C[\s\-]?\d+[A-Z]?$"))


def tag_kind(text, is_name=None):
    """'door', 'window' or 'cutout' for a door / window / cut-out tag, else
    None. Every line of the text must be one: "C-5" over "LOBBY PRESS" is a
    name. is_name(text) -> True for a shaft name or a room-family keyword
    (CHW is chilled water, not a window): those are never tags."""
    kinds = []
    for line in str(text or "").upper().replace("\\P", "\n").splitlines():
        t = " ".join(line.split()).strip(" .")
        if not t:
            continue
        kind = None
        if CUTOUT_TAG_RE.match(t):
            kind = "cutout"
        elif WINDOW_TAG_RE.match(t):
            kind = "window"
        elif DOOR_TAG_RE.match(t):
            kind = "door"
        if kind and not any(ch.isdigit() for ch in t) and sum(ch in "AEIOU" for ch in t) > 1:
            kind = None                               # a word (ROAD, FOOD), not a code
        if kind and is_name is not None and is_name(t):
            kind = None
        if kind is None:
            return None
        kinds.append(kind)
    return kinds[0] if kinds else None


def _box(b):
    from shapely.geometry import box
    return box(float(b[0]), float(b[1]), float(b[2]), float(b[3]))


def frame_lines(segs, boxes, tol=None, layers=None, t_min=80.0, t_max=400.0, skip=None):
    """Indices of the segments that are an X's own frame, held out of the
    walls: axis-aligned, on an edge of its box, within the edge's span (a
    wall face running on past the X's corners is not its frame) and at least
    30% of the edge long - unless it is a real wall face lying along the X,
    whatever its layer: paired outside the X with a line t_min..t_max away
    that is no other wall's face (BHUB: the wall between a cut-out and ELE.
    ROOM, drawn once as the cut-out's edge, stays a wall; P285: LIFT PRE's
    top edge, 250 mm from the masonry wall's face, does not - that line is
    the masonry's own). Paired both ways, a line of another layer than
    the one drawing the X's outline (a closed rectangle round it, nothing
    past its corners) is a wall face, not the frame. skip(seg) -> True for lines that are never a
    partner (the X diagonals). `layers` is accepted and not used."""
    tol = FRAME_TOL_MM if tol is None else tol
    out = set()
    if not boxes:
        return out
    from shapely.geometry import LineString
    from shapely.strtree import STRtree
    bxs = [_box(b) for b in boxes]
    tree = STRtree(bxs)
    ax = [i for i, sg in enumerate(segs) if (abs(sg[0] - sg[2]) <= 1.0) != (abs(sg[1] - sg[3]) <= 1.0)
          and not (skip is not None and skip(sg))]
    lines = [LineString([(segs[i][0], segs[i][1]), (segs[i][2], segs[i][3])]) for i in ax]
    ltree = STRtree(lines) if lines else None

    def partner(i, horiz, pos, lo, hi, out_dir):
        """A line parallel to segment i, out_dir (+1 / -1) of it at t_min..t_max,
        overlapping half its length: (position, length of lo..hi the lines
        at that position cover) or None."""
        if ltree is None:
            return None
        L = hi - lo
        if horiz:
            y_a, y_b = sorted((pos + out_dir * t_min, pos + out_dir * t_max))
            q = _box((lo, y_a, hi, y_b))
        else:
            x_a, x_b = sorted((pos + out_dir * t_min, pos + out_dir * t_max))
            q = _box((x_a, lo, x_b, hi))
        best = None
        cand = []
        for k in ltree.query(q):
            j = ax[k]
            if j == i:
                continue
            sj = segs[j]
            if horiz and abs(sj[1] - sj[3]) <= 1.0:
                p_, a_, b_ = sj[1], min(sj[0], sj[2]), max(sj[0], sj[2])
            elif not horiz and abs(sj[0] - sj[2]) <= 1.0:
                p_, a_, b_ = sj[0], min(sj[1], sj[3]), max(sj[1], sj[3])
            else:
                continue
            d = (p_ - pos) * out_dir
            if d < t_min - 1.0 or d > t_max + 1.0:
                continue
            cand.append((p_, a_, b_))
            if min(b_, hi) - max(a_, lo) < 0.5 * L:
                continue
            if best is None or d < (best - pos) * out_dir:
                best = p_
        if best is None:
            return None
        # how much of the span the lines at that position cover (duplicates
        # on several layers counted once)
        iv = sorted((max(a_, lo), min(b_, hi)) for p_, a_, b_ in cand if abs(p_ - best) <= 1.0 and min(b_, hi) > max(a_, lo))
        cov, cur = 0.0, None
        for a_, b_ in iv:
            if cur is None or a_ > cur[1]:
                if cur is not None:
                    cov += cur[1] - cur[0]
                cur = [a_, b_]
            else:
                cur[1] = max(cur[1], b_)
        if cur is not None:
            cov += cur[1] - cur[0]
        return best, cov

    # the layers that draw an X's own outline: a closed rectangle round it,
    # each side drawn along at least 90% of its length and no line of the
    # layer running on past a corner. A line of another layer along one of
    # its edges was drawn for something else - a wall's face (XA: A-WALL
    # along the FLOR-OTLN outline of STR PRE and SPARE SH, with the stair's
    # landing line 100 mm beyond the wall).
    side_cov, overrun = {}, set()

    def along_edge(k2, a, b, c, d):
        """The line lies on an edge of box k2, within its span."""
        x0, y0, x1, y1 = boxes[k2]
        if abs(b - d) <= 1.0:
            return (abs(b - y0) < tol or abs(b - y1) < tol) and min(a, c) >= x0 - tol and max(a, c) <= x1 + tol
        return (abs(a - x0) < tol or abs(a - x1) < tol) and min(b, d) >= y0 - tol and max(b, d) <= y1 + tol

    for i in ax:
        sg = segs[i]
        lay = (sg[4] if len(sg) > 4 else "") or ""
        a, b, c, d = sg[0], sg[1], sg[2], sg[3]
        near_k = list(tree.query(LineString([(a, b), (c, d)]).buffer(tol)))
        own_edge = any(along_edge(k2, a, b, c, d) for k2 in near_k)
        for k in near_k:
            x0, y0, x1, y1 = boxes[k]
            if abs(b - d) <= 1.0:
                lo_, hi_ = max(min(a, c), x0), min(max(a, c), x1)
                for side, yy in ((0, y0), (1, y1)):
                    if abs(b - yy) < tol and hi_ > lo_:
                        side_cov[(k, lay, side)] = side_cov.get((k, lay, side), 0.0) + (hi_ - lo_)
                        if (min(a, c) < x0 - tol or max(a, c) > x1 + tol) and not own_edge:
                            overrun.add((k, lay))     # (the frame of the X beside it may run on)
            else:
                lo_, hi_ = max(min(b, d), y0), min(max(b, d), y1)
                for side, xx in ((2, x0), (3, x1)):
                    if abs(a - xx) < tol and hi_ > lo_:
                        side_cov[(k, lay, side)] = side_cov.get((k, lay, side), 0.0) + (hi_ - lo_)
                        if (min(b, d) < y0 - tol or max(b, d) > y1 + tol) and not own_edge:
                            overrun.add((k, lay))
    outline = {}
    for (k, lay, side), L_ in side_cov.items():
        x0, y0, x1, y1 = boxes[k]
        full = (x1 - x0) if side in (0, 1) else (y1 - y0)
        if full > 0 and L_ >= 0.9 * full:
            outline.setdefault((k, lay), set()).add(side)
    outline_layers = {}
    for (k, lay), sides in outline.items():
        if len(sides) == 4 and (k, lay) not in overrun:
            outline_layers.setdefault(k, set()).add(lay)

    for i, sg in enumerate(segs):
        a, b, c, d = sg[0], sg[1], sg[2], sg[3]
        if abs(a - c) > 1.0 and abs(b - d) > 1.0:
            continue
        lay = (sg[4] if len(sg) > 4 else "") or ""
        for k in tree.query(LineString([(a, b), (c, d)]).buffer(tol)):
            x0, y0, x1, y1 = boxes[k]
            edge = None
            if abs(b - d) <= 1.0 and min(a, c) >= x0 - tol and max(a, c) <= x1 + tol and abs(a - c) >= 0.3 * (x1 - x0):
                if abs(b - y0) < tol:
                    edge = (True, b, min(a, c), max(a, c), -1)
                elif abs(b - y1) < tol:
                    edge = (True, b, min(a, c), max(a, c), +1)
            if edge is None and abs(a - c) <= 1.0 and min(b, d) >= y0 - tol and max(b, d) <= y1 + tol \
                    and abs(b - d) >= 0.3 * (y1 - y0):
                if abs(a - x0) < tol:
                    edge = (False, a, min(b, d), max(b, d), -1)
                elif abs(a - x1) < tol:
                    edge = (False, a, min(b, d), max(b, d), +1)
            if edge is None:
                continue
            horiz, pos, lo, hi, od = edge
            p1 = partner(i, horiz, pos, lo, hi, od)
            if p1 is not None:
                p2 = partner(None, horiz, p1[0], lo, hi, od)
                # the partner is another wall's face only when it pairs at
                # least as well with the line beyond it (P285 LIFT PRE: the
                # masonry's two faces, both the X's length); XA SPARE SH: the
                # 200 mm wall's far face, 803 mm, against a 503 mm slab edge
                # 300 mm beyond - the wall is the X's edge and that face
                if p2 is None or p2[1] < 0.9 * p1[1]:
                    break                             # a real wall face along the X
                if outline_layers.get(k) and lay not in outline_layers[k]:
                    # paired both ways: a line drawn for something other
                    # than the X's own outline is the wall's face (XA STR
                    # PRE: A-WALL along the FLOR-OTLN outline, the stair's
                    # landing line 100 mm beyond the wall)
                    break
            out.add(i)
            break
    return out


def diagonal_layers(segs, boxes, is_diag):
    """Per box, the layers of the X diagonals standing in it (is_diag(seg))."""
    from shapely.geometry import Point
    from shapely.strtree import STRtree
    out = [set() for _ in boxes]
    if not boxes:
        return out
    bxs = [_box(b).buffer(5.0) for b in boxes]
    tree = STRtree(bxs)
    for sg in segs:
        if abs(sg[0] - sg[2]) <= 1.0 or abs(sg[1] - sg[3]) <= 1.0 or not is_diag(sg):
            continue
        mid = Point((sg[0] + sg[2]) / 2.0, (sg[1] + sg[3]) / 2.0)
        for k in tree.query(mid):
            if bxs[k].contains(mid):
                out[k].add((sg[4] if len(sg) > 4 else "") or "")
    return out


class WallMass:
    """The traced walls of a floor, for placing X marks: the strips between
    paired faces, the wall hatches, the face lines and the columns."""

    def __init__(self, strips, hatch_polys, faces, columns=(), soft=(), lines=()):
        from shapely.geometry import Polygon, LineString
        from shapely.ops import unary_union
        from shapely.strtree import STRtree
        self.strips = []
        self.src = []                                 # index of each strip in `strips`
        for i, st in enumerate(strips or ()):
            try:
                pg = Polygon(st["pts"]).buffer(0)
            except Exception:                         # noqa: BLE001
                continue
            if pg.is_empty or pg.area <= 0:
                continue
            self.strips.append((pg, float(st.get("t") or 0.0)))
            self.src.append(i)
        self.hatches = [h for h in (hatch_polys or ()) if h is not None and not h.is_empty]
        self.columns = []
        for c in columns or ():
            try:
                pg = c if hasattr(c, "geom_type") else Polygon(c)
                pg = pg.buffer(0)
            except Exception:                         # noqa: BLE001
                continue
            if not pg.is_empty:
                self.columns.append(pg)
        self.faces = [LineString([(f[0], f[1]), (f[2], f[3])]) for f in faces or ()
                      if math.hypot(f[2] - f[0], f[3] - f[1]) > 1.0]
        self.ftree = STRtree(self.faces) if self.faces else None
        self.stree = STRtree([s for s, _t in self.strips]) if self.strips else None
        self.htree = STRtree(self.hatches) if self.hatches else None
        self.W = unary_union([s for s, _t in self.strips] + self.hatches).buffer(0) \
            if (self.strips or self.hatches) else Polygon()
        # face pairs the wall finder did not confirm (no hatch on a floor that
        # hatches its walls - a shaft's lining): a wall an X may stand
        # against, never a wall an X is inside
        soft_pg = []
        for b in soft or ():
            try:
                g = _box((min(b[0], b[2]), min(b[1], b[3]), max(b[0], b[2]), max(b[1], b[3])))
            except Exception:                         # noqa: BLE001
                continue
            if g.area > 0:
                soft_pg.append(g)
        self.soft = unary_union(soft_pg).buffer(0) if soft_pg else Polygon()
        # every axis-aligned line of the floor (not the X diagonals): a
        # steel column's outline drawn as lines is no closed shape
        self.lines = [LineString([(l[0], l[1]), (l[2], l[3])]) for l in lines or ()
                      if (abs(l[0] - l[2]) <= 1.0) != (abs(l[1] - l[3]) <= 1.0)]
        self.ltree = STRtree(self.lines) if self.lines else None
        self.xboxes = []                              # every X mark's box on the floor (set_xboxes)
        self.xtree = None

    def set_xboxes(self, boxes):
        """The X marks of the floor: wall found inside another X's box is that
        opening's own outline, not a wall running on (Centaurus FHC: an L of
        two X marks in one cut-out outline)."""
        from shapely.strtree import STRtree
        self.xboxes = [_box(b) for b in boxes or () if b[2] - b[0] > 1.0 and b[3] - b[1] > 1.0]
        self.xtree = STRtree(self.xboxes) if self.xboxes else None

    def _across(self, X, B):
        """The other X stands across B's wall band, not in line with it: its
        own box, not aligned with B's long sides (a run of cut-outs in one
        wall is in line, each the wall's continuation for the next)."""
        if X.intersects(B) and X.intersection(B).area > 0.5 * B.area:
            return False                              # B's own
        x0, y0, x1, y1 = B.bounds
        a0, b0, a1, b1 = X.bounds
        if (x1 - x0) >= (y1 - y0):
            return abs(b0 - y0) > RUN_TOL_MM or abs(b1 - y1) > RUN_TOL_MM
        return abs(a0 - x0) > RUN_TOL_MM or abs(a1 - x1) > RUN_TOL_MM

    def in_other_x(self, g, B, share=0.8):
        """At least `share` of g lies in the boxes of X marks standing across
        B's wall band (not B's own, not in line with it)."""
        if self.xtree is None or g.area <= 0:
            return False
        from shapely.ops import unary_union
        others = [self.xboxes[k] for k in self.xtree.query(g) if self._across(self.xboxes[k], B)]
        if not others:
            return False
        return g.intersection(unary_union(others)).area >= share * g.area

    def without_other_x(self, W, B):
        """W less the boxes of the X marks standing across B's wall band."""
        if self.xtree is None or W is None or W.is_empty:
            return W
        from shapely.ops import unary_union
        others = [self.xboxes[k] for k in self.xtree.query(B.buffer(RUN_PROBE_MM + 300.0))
                  if self._across(self.xboxes[k], B)]
        if not others:
            return W
        return W.difference(unary_union(others))

    def wall_between(self, x, y, bbox, cross=LABEL_WALL_CROSS_MM):
        """True when a wall stands between the point (a label) and the X box:
        the line from the point to the nearest point of the box runs at least
        `cross` mm through the inside of a wall strip or hatch. A label on a
        wall's face, naming the room behind it (XA: CA-ELEC SH on the face of
        the 200 mm wall beside the ELECTRICAL SH X), names that room, not the
        X across the wall."""
        from shapely.geometry import LineString
        if self.W.is_empty:
            return False
        x0, y0, x1, y1 = bbox
        nx, ny = min(max(x, x0), x1), min(max(y, y0), y1)
        if math.hypot(nx - x, ny - y) < cross:
            return False
        seg = LineString([(x, y), (nx, ny)])
        if not hasattr(self, "_Win"):
            self._Win = self.W.buffer(-5.0)
        try:
            return seg.intersection(self._Win).length >= cross
        except Exception:                             # noqa: BLE001
            return False

    def ringed_column(self, B):
        """An X ringed on all four sides by a line just outside it
        (COLUMN_RING_MM) - a steel column's outline and plates round its
        cross - with open floor, not wall, beyond at least two of them. A
        shaft walled in on four sides has wall behind every side."""
        if self.ltree is None:
            return False
        x0, y0, x1, y1 = B.bounds
        w, h = x1 - x0, y1 - y0
        if max(w, h) > COLUMN_MAX_SIDE_MM or min(w, h) < 0.8 * max(w, h):
            return False
        lo, hi = COLUMN_RING_MM
        open_sides = 0
        for name, band, beyond in (
                ("S", _box((x0, y0 - hi, x1, y0 - lo)), _box((x0, y0 - hi - 100, x1, y0 - hi))),
                ("N", _box((x0, y1 + lo, x1, y1 + hi)), _box((x0, y1 + hi, x1, y1 + hi + 100))),
                ("W", _box((x0 - hi, y0, x0 - lo, y1)), _box((x0 - hi - 100, y0, x0 - hi, y1))),
                ("E", _box((x1 + lo, y0, x1 + hi, y1)), _box((x1 + hi, y0, x1 + hi + 100, y1)))):
            horiz = name in ("S", "N")
            L = w if horiz else h
            cov = 0.0
            for k in self.ltree.query(band):
                ln = self.lines[k]
                (ax_, ay_), (bx_, by_) = ln.coords[0], ln.coords[1]
                if horiz != (abs(ay_ - by_) <= 1.0):
                    continue
                cov += ln.intersection(band).length
            if cov < 0.8 * L:
                return False
            if self.W is None or self.W.is_empty or self.W.intersection(beyond).area < 0.3 * beyond.area:
                open_sides += 1
        return open_sides >= 2

    def strips_over(self, B, share=0.3):
        """The strips covering at least `share` of B."""
        if self.stree is None:
            return []
        out = []
        for k in self.stree.query(B):
            pg, t = self.strips[k]
            if pg.intersects(B) and pg.intersection(B).area >= share * B.area:
                out.append(k)
        return out

    def strip_in_run(self, k, tol=None, reach=50.0, own=None):
        """Whether strip k carries on in line - another strip of the same
        thickness, faces aligned, starting within `reach` of one of its
        ends and running on RUN_ON_MM past it: a real wall, its thickness
        unchanged (rule 2-1). The same strip found twice (Centaurus FHC: the
        X's edge paired with two lines 15 mm apart) is not a run."""
        tol = RUN_TOL_MM if tol is None else tol
        pg, t = self.strips[k]
        b = pg.bounds
        horiz = (b[2] - b[0]) >= (b[3] - b[1])
        probe = _box((b[0] - reach, b[1] - 1, b[2] + reach, b[3] + 1)) if horiz \
            else _box((b[0] - 1, b[1] - reach, b[2] + 1, b[3] + reach))
        for j in self.stree.query(probe):
            if j == k:
                continue
            q, tq = self.strips[j]
            if own is not None and self.in_other_x(q, own):
                continue                              # inside another X: that opening's outline
            qb = q.bounds
            if horiz:
                if (qb[2] - qb[0]) < (qb[3] - qb[1]):
                    continue
                if abs(qb[1] - b[1]) > tol or abs(qb[3] - b[3]) > tol:
                    continue
                if qb[2] < b[0] - reach or qb[0] > b[2] + reach:
                    continue
                if qb[0] < b[0] - RUN_ON_MM or qb[2] > b[2] + RUN_ON_MM:
                    return True
            else:
                if (qb[3] - qb[1]) < (qb[2] - qb[0]):
                    continue
                if abs(qb[0] - b[0]) > tol or abs(qb[2] - b[2]) > tol:
                    continue
                if qb[3] < b[1] - reach or qb[1] > b[3] + reach:
                    continue
                if qb[1] < b[1] - RUN_ON_MM or qb[3] > b[3] + RUN_ON_MM:
                    return True
        return False

    def hatch_runs_past(self, B, reach=300.0):
        """A wall hatch covering B that runs on past it along B's long axis:
        a real wall, not a column's or a chase's fill."""
        if self.htree is None:
            return False
        x0, y0, x1, y1 = B.bounds
        horiz = (x1 - x0) >= (y1 - y0)
        for k in self.htree.query(B):
            h = self.hatches[k]
            if not h.intersects(B) or h.intersection(B).area < 0.5 * B.area:
                continue
            hb = h.bounds
            if horiz and (hb[0] < x0 - reach or hb[2] > x1 + reach):
                return True
            if not horiz and (hb[1] < y0 - reach or hb[3] > y1 + reach):
                return True
        return False


def run_ends(B, W, tol=None, probe=None, cover=None):
    """How many ends of the X box B the wall carries on past, in line, at the
    X's own thickness, its faces aligned with the X's long sides."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    tol = RUN_TOL_MM if tol is None else tol
    probe = RUN_PROBE_MM if probe is None else probe
    cover = RUN_COVER if cover is None else cover
    if W is None or W.is_empty:
        return 0
    x0, y0, x1, y1 = B.bounds
    horiz = (x1 - x0) >= (y1 - y0)
    t = (y1 - y0) if horiz else (x1 - x0)
    n = 0
    for end in (0, 1):
        if horiz:
            ex = x0 - probe if end == 0 else x1
            band = _box((ex, y0, ex + probe, y1))
            wide = _box((ex, y0 - 300, ex + probe, y1 + 300))
        else:
            ey = y0 - probe if end == 0 else y1
            band = _box((x0, ey, x1, ey + probe))
            wide = _box((x0 - 300, ey, x1 + 300, ey + probe))
        if W.intersection(band).area < cover * band.area:
            continue
        piece = W.intersection(wide)
        parts = [g for g in getattr(piece, "geoms", [piece]) if g.geom_type == "Polygon" and g.intersects(band)]
        if not parts:
            continue
        b = unary_union(parts).bounds
        if horiz:
            tw, off = b[3] - b[1], (abs(b[1] - y0), abs(b[3] - y1))
        else:
            tw, off = b[2] - b[0], (abs(b[0] - x0), abs(b[2] - x1))
        if abs(tw - t) <= tol and max(off) <= tol:
            n += 1
    return n


def side_gaps(bx, W, faces, ftree, reach=300.0, share=None):
    """Per side of the box (S, N, W, E): the gap in mm to the nearest wall
    outside it - wall mass in a 20 mm band, or a wall face parallel to the
    side, over at least `share` of the side - or None within `reach`."""
    share = SIDE_SHARE if share is None else share
    x0, y0, x1, y1 = bx
    out = {}
    spec = {"S": ((x0, y0), (x1, y0), (0, -1)), "N": ((x0, y1), (x1, y1), (0, 1)),
            "W": ((x0, y0), (x0, y1), (-1, 0)), "E": ((x1, y0), (x1, y1), (1, 0))}
    steps = [0, 5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 90, 100, 125, 150, 200, 250, 300]
    for n, (a, b, (dx, dy)) in spec.items():
        Ls = math.hypot(b[0] - a[0], b[1] - a[1])
        out[n] = None
        if Ls < 50:
            continue
        for d in steps:
            if d > reach:
                break
            if dx == 0:
                yb = a[1] + dy * d
                band = _box((a[0], min(yb, yb + dy * 20), b[0], max(yb, yb + dy * 20)))
            else:
                xb = a[0] + dx * d
                band = _box((min(xb, xb + dx * 20), a[1], max(xb, xb + dx * 20), b[1]))
            hit = W is not None and not W.is_empty and W.intersection(band).area >= share * band.area
            if not hit and ftree is not None:
                cov = 0.0
                for k in ftree.query(band):
                    f = faces[k]
                    fa, fb = f.coords[0], f.coords[1]
                    if abs((fb[0] - fa[0]) * (b[1] - a[1]) - (fb[1] - fa[1]) * (b[0] - a[0])) > 0.05 * f.length * Ls:
                        continue
                    cov += f.intersection(band).length
                hit = cov >= share * Ls
            if hit:
                out[n] = d
                break
    return out


def x_facts(bbox, mass, texts=(), is_name=None, finder_kind=None, glazing=0, cut_back=True,
            area_sqm=None, round_=False):
    """What the X sequence reads for one X - nothing is decided here.

    bbox: (x0, y0, x1, y1) mm; mass: WallMass; texts: [(x, y, text)] of the
    floor; finder_kind: what the window finder made of its outline
    ('window', 'cutout', 'shaft' or None; a dashed outline is never a
    window's); glazing: its glazing lines; area_sqm: its outline's area
    (default: the box's); round_: drawn inside a circle.
    Returns {"bbox" (as drawn), "shaft_bbox" (cut back to the wall face),
    "w", "h", "area_sqm", "wtag", "ctag", "dtag", "column" (why, or None),
    "in_wall" (why, or None), "window" (the window evidence, or None),
    "gap" (mm to the nearest wall, or None), "sides" {S, N, W, E: mm or
    None}, "walled_sides", "lift_size", "round", "shaft_why",
    "drop_strips"}."""
    from shapely.geometry import Point
    x0, y0, x1, y1 = [float(v) for v in bbox]
    B = _box((x0, y0, x1, y1))
    w, h = x1 - x0, y1 - y0
    f = {"bbox": (x0, y0, x1, y1), "shaft_bbox": (x0, y0, x1, y1), "w": w, "h": h,
         "area_sqm": float(area_sqm) if area_sqm is not None else w * h / 1e6,
         "wtag": None, "ctag": None, "dtag": None, "column": None, "in_wall": None, "window": None,
         "gap": None, "sides": {}, "walled_sides": 0, "round": bool(round_), "shaft_why": "",
         "lift_size": (LIFT_SIDE_MM[0] <= min(w, h) and max(w, h) <= LIFT_SIDE_MM[1]
                       and max(w, h) / max(min(w, h), 1.0) <= LIFT_ASPECT_MAX),
         "drop_strips": []}
    # tags within reach of the box (nearest first)
    near = []
    for tx, ty, tt in texts or ():
        d = B.distance(Point(float(tx), float(ty)))
        if d <= TAG_REACH_MM:
            k = tag_kind(tt, is_name)
            if k:
                near.append((d, k, " ".join(str(tt).split())))
    near.sort(key=lambda q: q[0])
    for d, k, tt in near:
        key = {"window": "wtag", "cutout": "ctag", "door": "dtag"}[k]
        if f[key] is None:
            f[key] = tt
    # a column's cross (step 6)
    c = Point((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    if _on_column(B, mass.columns):
        f["column"] = ON_A_COLUMN
        return f
    if mass.ringed_column(B):
        f["column"] = "ringed by a steel column's outline"
        return f
    if _in_column_fill(B, mass.hatches, mass.htree):
        f["column"] = IN_A_FILL
        return f
    W = mass.W
    inW = B.intersection(W).area / B.area if (W is not None and not W.is_empty) else 0.0
    # inside a wall (steps 2, 4, 5): an opening is no thicker than a wall -
    # a lift-sized X beside a thick hatched mass is no opening in it
    nrun = run_ends(B, mass.without_other_x(W, B)) if min(w, h) <= OPENING_MAX_MM else 0
    opening = None
    if nrun >= 1:
        opening = "the wall carries on past {0} of its ends at its thickness".format("both" if nrun == 2 else "one")
    elif inW >= SLOT_IN_WALL_SHARE and (min(w, h) < SLOT_SIDE_MM or w * h < SLOT_AREA_SQM * 1e6):
        # a slot the wall holds whole (BHUB: a 400 x 110 sleeve slot): an
        # opening in the wall, however the strips round it pair
        opening = "a slot inside the wall ({0:.0f} mm across)".format(min(w, h))
    elif inW >= 0.5:
        cover = mass.strips_over(B)
        real = [k for k in cover if mass.strip_in_run(k, own=B)]
        if inW >= IN_WALL_SHARE and min(w, h) <= OPENING_MAX_MM and (real or (not cover and mass.hatch_runs_past(B))):
            opening = "inside a wall that runs on past it"
        else:
            # the piece of wall around it changes thickness there: the space
            # the X marks, not wall (rule 2-1)
            bogus = [k for k in cover if k not in real]
            if bogus:
                from shapely.ops import unary_union
                f["drop_strips"] = [mass.src[k] for k in bogus]
                W = W.difference(unary_union([mass.strips[k][0] for k in bogus]).buffer(1.0, join_style=2))
                inW = B.intersection(W).area / B.area if not W.is_empty else 0.0
            if inW >= IN_WALL_SHARE:
                opening = "inside the wall"
    if opening is None and finder_kind in ("window", "cutout") and inW >= IN_WALL_SHARE and min(w, h) <= OPENING_MAX_MM:
        opening = "an opening in the wall (window test)"
    if opening is not None:
        f["in_wall"] = opening
        f["drop_strips"] = []
        # the window evidence (step 4)
        wt_d = next((d for d, k, _t in near if k == "window"), None)
        ct_d = next((d for d, k, _t in near if k == "cutout"), None)
        if glazing:
            f["window"] = "glazing in it"
        elif finder_kind == "window":
            f["window"] = "the window finder's window frame"
        elif wt_d is not None and (ct_d is None or wt_d < ct_d):
            f["window"] = "window tag {0} nearer than any cut-out tag".format(f["wtag"])
        return f
    # cut back what spilled into a wall
    C = B
    if cut_back and inW > SPILL_MIN_SHARE and W is not None and not W.is_empty:
        rest = B.difference(W)
        pieces = [g for g in getattr(rest, "geoms", [rest]) if g.geom_type == "Polygon" and g.area > 0]
        if pieces:
            held = [g for g in pieces if g.buffer(1.0).contains(c)]
            C = held[0] if held else max(pieces, key=lambda g: g.area)
            if C.area < 0.2 * B.area:
                C = B
    cb = C.bounds
    Wg = W
    if not mass.soft.is_empty:
        Wg = W.union(mass.soft.difference(C.buffer(-1.0))) if W is not None and not W.is_empty else mass.soft
    gaps = side_gaps(cb, Wg, mass.faces, mass.ftree)
    gv = [g for g in gaps.values() if g is not None]
    f["sides"] = dict(gaps)
    f["gap"] = min(gv) if gv else None
    f["walled_sides"] = sum(1 for g in gv if g <= GAP_MM)
    f["shaft_bbox"] = tuple(round(v, 1) for v in cb)
    if f["gap"] is not None and f["gap"] <= GAP_MM:
        f["shaft_why"] = "a wall within {0:.0f} mm of its {1} side{2}".format(
            f["gap"], ",".join(k for k, v in gaps.items() if v is not None and v <= GAP_MM),
            "; cut back {0:.0f}% spilled into the wall".format(100 * (1 - C.area / B.area)) if C is not B else "")
    return f


def resolve_x(f, named=False, lift_label=False, shaft_name=False, in_room=False, stair_well=False):
    """The X sequence for one X (see the note at the top): its class, decided
    once. f: x_facts(); named: a label of its own names it; lift_label: that
    label names a lift; shaft_name: it names a shaft; in_room: it stands in a
    room or a staircase; stair_well: it is a stair's well, between the flights
    of the staircase it stands in.
    Returns {"cls" (one of CLASSES), "step" (2-7), "why", "trail"
    [(step, what that step made of it)]}."""
    trail = [(1, "an X mark: a void to start with")]
    big = f.get("area_sqm", 0.0) > VOID_MIN_SQM
    col = f.get("column")
    walled = f.get("gap") is not None and f["gap"] <= GAP_MM
    lift_like = lift_label or (f.get("lift_size") and not shaft_name)
    lift_ok = (lift_like and f.get("walled_sides", 0) >= LIFT_WALLED_SIDES
               and not f.get("in_wall") and not col and not big)
    sides = f.get("sides") or {}
    near = [v for v in sides.values() if v is not None]

    def done(cls, step, why):
        trail.append((step, cls + ": " + why))
        return {"cls": cls, "step": step, "why": why, "trail": trail}
    # 2 shafts
    if f.get("in_wall"):
        no = "inside a wall ({0}) - never a shaft".format(f["in_wall"])
    elif f.get("round"):
        no = "drawn inside a circle - a shaft is never round"
    elif col:
        no = "a column's cross ({0})".format(col)
    elif big:
        no = "larger than {0:g} sq.m".format(VOID_MIN_SQM)
    elif stair_well:
        no = "a stair's well, between its flights"
    elif not walled:
        no = "no wall within {0:.0f} mm of any side{1}".format(
            GAP_MM, " (nearest {0:.0f} mm)".format(min(near)) if near else "")
    elif not named and f.get("ctag"):
        no = "unnamed, with cut-out tag {0} beside it".format(f["ctag"])
    elif lift_ok:
        no = "walled round, and {0}: left for the lift rules".format(
            "a LIFT label" if lift_label else "lift size")
    else:
        return done("shaft", 2, f.get("shaft_why") or "a wall beside it")
    trail.append((2, "not a shaft: " + no))
    # 3 lifts
    if lift_ok:
        return done("lift", 3, "walled on {0} sides, {1}".format(
            f.get("walled_sides", 0), "a LIFT label" if lift_label else
            "{0:.0f} x {1:.0f} mm (lift size)".format(f.get("w", 0), f.get("h", 0))))
    trail.append((3, "not a lift"))
    # 4 windows
    if f.get("in_wall") and not col and f.get("window"):
        return done("window", 4, "inside a wall, {0}".format(f["window"]))
    trail.append((4, "not a window"))
    # 5 cut-outs
    if not col and not big:
        if f.get("in_wall"):
            return done("cutout", 5, "inside a wall, no glazing, window frame or window tag" +
                        (" (cut-out tag {0})".format(f["ctag"]) if f.get("ctag") else ""))
        if not in_room and not stair_well:
            if f.get("ctag"):
                return done("cutout", 5, "cut-out tag {0} beside it".format(f["ctag"]))
            if not named and not walled:
                return done("cutout", 5, "unnamed, no wall within {0:.0f} mm, not in a room".format(GAP_MM))
    trail.append((5, "not a cut-out" + (": it stands in a room or a staircase" if in_room or stair_well else "")))
    # 6 columns
    if col:
        return done("column", 6, col)
    trail.append((6, "not a column"))
    # 7 voids
    return done("void", 7, "nothing above claimed it" + (": a stair's well" if stair_well else
                                                         " (larger than {0:g} sq.m)".format(VOID_MIN_SQM) if big else ""))


ON_A_COLUMN = "on a column"
IN_A_FILL = "inside a solid fill no larger than itself"


def _on_column(B, columns):
    """The X's box B stands on one of the columns (polygons): its centre
    inside one, or 80 % of it on one."""
    from shapely.geometry import Point
    x0, y0, x1, y1 = B.bounds
    c = Point((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    for col in columns or ():
        if col.contains(c) or (col.intersects(B) and col.intersection(B).area >= 0.8 * B.area):
            return True
    return False


def _in_column_fill(B, hatches, htree):
    """The X's box B, squarish, lies in a solid fill no larger than
    COLUMN_HATCH_RATIO times itself (IN_WALL_SHARE of it covered): the
    hatched column its cross marks. htree: an STRtree over hatches."""
    if htree is None:
        return False
    x0, y0, x1, y1 = B.bounds
    w, h = x1 - x0, y1 - y0
    for k in htree.query(B):
        hp = hatches[int(k)]
        if hp.intersects(B) and hp.intersection(B).area >= IN_WALL_SHARE * B.area \
                and hp.area <= COLUMN_HATCH_RATIO * B.area and min(w, h) >= 0.6 * max(w, h):
            return True
    return False


def early_column(bbox, columns=(), hatches=(), htree=None):
    """Step 00 of Step 3 (rule columns_first): whether the X at bbox (x0, y0,
    x1, y1) is a column's cross by the two tests that read no wall - on a
    column (ON_A_COLUMN), or inside a solid fill no larger than itself
    (IN_A_FILL) - so the column is known before any wall is traced. The same
    tests as x_facts (step 6 of the sequence decides the same X a column);
    the third, a steel column's outline round the cross, reads the walls and
    stays in the sequence. -> ON_A_COLUMN, IN_A_FILL or None."""
    x0, y0, x1, y1 = [float(v) for v in bbox]
    if x1 <= x0 or y1 <= y0:
        return None
    B = _box((x0, y0, x1, y1))
    if _on_column(B, columns):
        return ON_A_COLUMN
    if htree is None and hatches:
        from shapely.strtree import STRtree
        hatches = list(hatches)
        htree = STRtree(hatches)
    if _in_column_fill(B, list(hatches or ()), htree):
        return IN_A_FILL
    return None


def drop_label_strips(strips, points):
    """Rule 2-1 for the walls drawn: a wall strip holding a room or shaft
    name is the space it names, not wall - unless the wall carries on in line
    past it at the same thickness (a name written over a real wall). Returns
    (the strips kept, how many were dropped). points: [(x, y)] of the names."""
    if not strips or not points:
        return list(strips or ()), 0
    from shapely.geometry import Point
    mass = WallMass(strips, (), ())
    if mass.stree is None:
        return list(strips), 0
    gone = set()
    for x, y in points:
        p = Point(float(x), float(y))
        for k in mass.stree.query(p):
            pg, _t = mass.strips[k]
            if pg.buffer(-5.0).contains(p) and not mass.strip_in_run(k):
                gone.add(mass.src[k])
    return [st for i, st in enumerate(strips) if i not in gone], len(gone)
