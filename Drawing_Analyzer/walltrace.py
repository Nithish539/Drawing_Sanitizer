# -*- coding: utf-8 -*-
"""What is wall: two faces a wall's thickness apart, confirmed by the hatch.

The wall-quantity logic worked out in the wall-tracing analysis (Oct 2026,
WALL_TRACING_LOGIC.md), fitted to the engine: it reads one floor's
segments, hatches and texts (already in millimetres, blocks already
flattened, layer 0 already given its parent's layer) and decides which
strips are wall. Room tracing does not use it; it decides the walls drawn
on AI_WALL and their measured area and length.

  1. candidate lines: wall layers by the LAST part of the name (an xref
     BLOCKWALLS$0$A-DOOR is a door layer); never door leaves;
  2. lines grouped by direction (1 degree), each as (u0, u1, v) in its own
     rotated frame, so any angle works;
  3. lines within 2 mm of the same offset are one face (the same face
     drawn twice is common - the biggest source of double counting);
  4. each face pairs only with its nearest facing line, 70-420 mm away,
     over at least 150 mm: a strip;
  5. a strip 90% inside strips of another direction is the same patch
     paired the other way: dropped (shortest first, so one is kept);
  6. the wall hatch is found, not named: a group of hatches (layer,
     pattern, solid) of at least 0.2 sq.m lying more than 60% inside the
     strips is wall material;
  7. strips back to back (sharing a face) are judged: hatched - wall;
     unhatched with hatched wall on both sides - wall; unhatched beside a
     hatched wall with WC / urinal linework near - a ledge, not wall;
     unhatched beside a hatched wall otherwise - review; nothing hatched,
     closed at both ends, a neighbour open - an empty gap;
  8. a strip ending against a perpendicular strip is carried across it
     (the square at every T and corner);
  9. hatched pieces left over, wider than 450 mm, are a shaft (a SHAFT
     label within 1.5 m, never solid fill - 14), a column (near-square, up
     to 900 mm) or review;
 10. structure drawn on a structure layer (a structure xref's COLUMN or
     CORE layer: BHUB's 400-450 mm concrete core walls on
     XREF-T1_STR$1$COLUMN) is paired the same way up to 600 mm and kept
     where the structure hatch fills it and it runs like a wall (at least
     900 mm and twice its thickness) - a column stays a column;
 11. an unhatched strip with one face on the floor plate's edge and a wall
     strip on its other face is the slab edge outside that wall, not wall
     (the 200 mm band along the XA tower's facade);
 12. curved walls: two concentric arcs on wall layers a wall's thickness
     apart are a curved wall (paired as arcs - as short chords they pair
     poorly);
 13. the hatch is the wall where the faces do not pair: a piece of wall
     hatch left over beside a traced wall, no wider than a wall
     (BLOCK_MIN_SIDE), is wall - the 500 x 300 foot of an L-shaped concrete
     column whose faces are 500 mm apart, a block wall carried on behind a
     basin niche that breaks its face (P-14 6th floor); where such pieces
     fill most of a short gap (up to 1.2 m) between two strips of one band,
     the band is carried across it. "No wider than a wall" is the piece's
     thickness, not its box (an L-shaped toilet wall is 12 m x 1.3 m by its
     box and 200 mm thick), and a piece edged by lines drawn on any layer
     along most of its outline is wall too, beside a traced wall or not
     (the P285 toilet walls' faces are on HVAC COMMENTS); one thin only by
     its thickness (its box wider than a wall) must be edged so - a
     hatch's sliver running corner to corner across a room is not a wall;
 14. a solid-filled piece (concrete) is never a shaft: by a shaft label it
     is wall where it stands against a traced wall or is edged (the six
     solid corner blocks by the P285 shafts); with no label, against a
     traced wall it is wall too (the RCC blocks at P285's wall ends), else
     a column or review.

Returns None when the floor has no wall-layer lines; the engine then keeps
its own wall strips.
"""

from __future__ import annotations

import bisect
import collections
import math
import re
from typing import Dict, List, Optional

from .config import CONFIG

_WT = CONFIG.get("wall_trace", {}) if isinstance(CONFIG, dict) else {}

ENABLED = bool(_WT.get("enabled", True))
T_MIN = float(_WT.get("thickness_min_mm", 70.0))
T_MAX = float(_WT.get("thickness_max_mm", 420.0))
MIN_RUN = float(_WT.get("min_run_mm", 150.0))
MIN_SEG = float(_WT.get("min_segment_mm", 30.0))
ANGLE_BIN = float(_WT.get("angle_bin_deg", 1.0))
COLLINEAR_TOL = float(_WT.get("collinear_tol_mm", 2.0))
SWALLOW = float(_WT.get("swallow_share", 0.90))
HATCH_IN_STRIPS = float(_WT.get("hatch_in_strips_share", 0.60))
HATCH_GROUP_MIN = float(_WT.get("hatch_group_min_sqm", 0.2)) * 1e6
FILLED = float(_WT.get("filled_share", 0.50))
CAP_TOL = float(_WT.get("cap_tol_mm", 8.0))
JUNCTION_TOL = float(_WT.get("junction_tol_mm", 6.0))
BLOCK_MIN_SIDE = float(_WT.get("block_min_side_mm", 450.0))
COLUMN_MAX_SIDE = float(_WT.get("column_max_side_mm", 900.0))
SHAFT_LABEL_REACH = float(_WT.get("shaft_label_reach_mm", 1500.0))
LEDGE_REACH = float(_WT.get("ledge_reach_mm", 900.0))
LEDGE_MIN_FIXTURE_SEGS = int(_WT.get("ledge_min_fixture_segments", 20))
# An unhatched strip closed at both ends between two unhatched walls (the
# 200 | 151 | 200 case on the XA tower): "gap" (not wall) or "wall".
UNFILLED_SANDWICH = str(_WT.get("unfilled_sandwich", "gap"))

WALL_LAYER = re.compile(str(_WT.get(
    "wall_layer_regex", r"WALL|(^|[^A-Z])WL([^A-Z]|$)|RCC|SHEAR|SLIP\s*FORM")))
NOT_WALL_LAYER = re.compile(str(_WT.get(
    "not_wall_layer_regex", r"CILL|SILL|HATCH|PATT|TEXT|DIM|IDEN|TILE|FINI|SKIRT|GLASS|GLAZ")))
FIXTURE_LAYER = re.compile(str(_WT.get(
    "fixture_layer_regex", r"TOIL|SANR|SANIT|FIX|URINAL|PL-FURN|TRAPS|AEQ|WC\b|PLUMB")), re.I)
SHAFT_LABEL = re.compile(r"SHAFT|\bSH\b", re.I)
# 10. structure layers (the last part of the name), their thickness and run
STRUCT_LAYER = re.compile(str(_WT.get(
    "structure_layer_regex", r"COLUMN|(^|[^A-Z])COLU([^A-Z]|$)|CORE|SHEAR|STRUCT|CONC")))
T_MAX_STRUCT = float(_WT.get("structure_thickness_max_mm", 600.0))
STRUCT_MIN_RUN = float(_WT.get("structure_min_run_mm", 900.0))
# 11. the slab edge outside a wall
EDGE_TOL = float(_WT.get("plate_edge_tol_mm", 30.0))
EDGE_SHARE = float(_WT.get("plate_edge_share", 0.8))
# 13. wall hatch left over beside a traced wall
HATCH_FILL = bool(_WT.get("hatch_fill", True))
HATCH_FILL_MIN = float(_WT.get("hatch_fill_min_sqm", 0.005)) * 1e6
# ... no wider than a wall anywhere (its thickness, not its box: an L-shaped
# toilet wall is 12 m x 1.3 m by its box, 200 mm thick): eroded by half that
# width nothing is left - or only specks, along a wall at least three times
# that long (a thick wall's corner or junction; a column is short)
HATCH_FILL_MAX_T = float(_WT.get("hatch_fill_max_thickness_mm", BLOCK_MIN_SIDE))
HATCH_FILL_CORE_SQM = float(_WT.get("hatch_fill_core_sqm", 0.01)) * 1e6
# ... or edged by lines drawn on any layer along most of its outline (the
# P285 toilet walls' faces are on HVAC COMMENTS)
HATCH_FILL_EDGED = float(_WT.get("hatch_fill_edged_share", 0.6))
HATCH_FILL_EDGE_TOL = float(_WT.get("hatch_fill_edge_tol_mm", 3.0))
# ... but not a hatch's own boundary, a column's outline (a steel column's
# 25 mm casing hatch is not a wall), dimensions, text, grid or centre lines
EDGE_NOT_LAYER = re.compile(str(_WT.get(
    "hatch_fill_edge_not_layer_regex",
    r"HATCH|PATT|COLUMN|(^|[^A-Z])COLU?([^A-Z]|$)|PILLAR|DIM|TEXT|GRID|CENTRE|CENTER")), re.I)
# 9. solid-filled (concrete) is never a shaft: a solid piece left over by a
# shaft label is wall where it stands against a traced wall or is edged
SOLID_NOT_SHAFT = bool(_WT.get("solid_not_shaft", True))
# ... nor a gap: any solid piece left over against a traced wall is wall (the
# RCC blocks at the P285 wall ends; the final drawing reads no labels, so
# this is what makes it draw them as Step 3 does)
SOLID_AGAINST_WALL = bool(_WT.get("solid_against_wall_is_wall", True))
HATCH_BRIDGE_MAX = float(_WT.get("hatch_bridge_max_mm", 1200.0))
HATCH_BRIDGE_SHARE = float(_WT.get("hatch_bridge_share", 0.5))
# 12. curved walls
ARC_CENTRE_TOL = float(_WT.get("arc_centre_tol_mm", 25.0))
ARC_SAME_FACE = float(_WT.get("arc_same_face_mm", 10.0))


def is_wall_layer(layer) -> bool:
    last = str(layer or "").upper().split("$")[-1].split("|")[-1]
    return bool(WALL_LAYER.search(last)) and not NOT_WALL_LAYER.search(last)


def is_structure_layer(layer) -> bool:
    """A structure layer that is not a wall layer: its lines are wall faces
    only where the structure hatch confirms them (10)."""
    last = str(layer or "").upper().split("$")[-1].split("|")[-1]
    return (bool(STRUCT_LAYER.search(last)) and not NOT_WALL_LAYER.search(last)
            and not WALL_LAYER.search(last))


def _rect(k, lo, hi, v0, v1):
    from shapely.geometry import Polygon
    c, s = math.cos(math.radians(k)), math.sin(math.radians(k))
    return Polygon([(u * c - v * s, u * s + v * c) for u, v in [(lo, v0), (hi, v0), (hi, v1), (lo, v1)]])


def _rect_pts(k, lo, hi, v0, v1):
    c, s = math.cos(math.radians(k)), math.sin(math.radians(k))
    return [(u * c - v * s, u * s + v * c) for u, v in [(lo, v0), (hi, v0), (hi, v1), (lo, v1)]]


def faces_by_direction(segs):
    by = collections.defaultdict(list)
    for x1, y1, x2, y2 in segs:
        if math.hypot(x2 - x1, y2 - y1) < MIN_SEG:
            continue
        a = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180
        k = round(a / ANGLE_BIN) * ANGLE_BIN % 180
        c, s = math.cos(math.radians(k)), math.sin(math.radians(k))
        u1, u2 = x1 * c + y1 * s, x2 * c + y2 * s
        v = (-x1 * s + y1 * c - x2 * s + y2 * c) / 2
        by[k].append([min(u1, u2), max(u1, u2), v])
    return by


def merge_collinear(lines, gap=1.0):
    lines = sorted(lines, key=lambda r: r[2])
    out, i = [], 0
    while i < len(lines):
        j, grp = i, [lines[i]]
        while j + 1 < len(lines) and lines[j + 1][2] - grp[0][2] <= COLLINEAR_TOL:
            j += 1
            grp.append(lines[j])
        v = sum(g[2] for g in grp) / len(grp)
        iv = sorted((g[0], g[1]) for g in grp)
        cur = list(iv[0])
        for a, b in iv[1:]:
            if a <= cur[1] + gap:
                cur[1] = max(cur[1], b)
            else:
                out.append([cur[0], cur[1], v])
                cur = [a, b]
        out.append([cur[0], cur[1], v])
        i = j + 1
    return out


def nearest_face_strips(faces, tmax=None):
    """Pair each face only with its nearest neighbour across, sweeping
    along the direction with the set of lines present at each stretch."""
    tmax = T_MAX if tmax is None else tmax
    strips = []
    for k, R in faces.items():
        events = collections.defaultdict(lambda: ([], []))
        for r in R:
            if r[1] - r[0] < 1e-6:
                continue
            events[r[0]][0].append(r[2])
            events[r[1]][1].append(r[2])
        xs = sorted(events)
        active = []
        raw = []
        for a, b in zip(xs, xs[1:]):
            adds, rems = events[a]
            for v in rems:
                i = bisect.bisect_left(active, v - 1e-9)
                if i < len(active) and abs(active[i] - v) < 1e-6:
                    active.pop(i)
            for v in adds:
                bisect.insort(active, v)
            if b - a < 1e-6:
                continue
            for v0, v1 in zip(active, active[1:]):
                if T_MIN <= v1 - v0 <= tmax:
                    raw.append((round(v0, 1), round(v1, 1), a, b))
        raw.sort()
        cur = None

        def flush(cur):
            if cur and cur[3] - cur[2] >= MIN_RUN:
                strips.append(dict(k=k, lo=cur[2], hi=cur[3], v0=cur[0], v1=cur[1], t=cur[1] - cur[0],
                                   g=_rect(k, cur[2], cur[3], cur[0], cur[1])))
        for v0, v1, a, b in raw:
            if cur and cur[0] == v0 and cur[1] == v1 and abs(a - cur[3]) < 1e-6:
                cur[3] = b
            else:
                flush(cur)
                cur = [v0, v1, a, b]
        flush(cur)
    return strips


def drop_swallowed(strips):
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
    S = sorted(strips, key=lambda c: c["hi"] - c["lo"])
    if not S:
        return S
    tree = STRtree([c["g"] for c in S])
    alive = [True] * len(S)
    for i, c in enumerate(S):
        near = [S[int(j)]["g"] for j in tree.query(c["g"])
                if int(j) != i and alive[int(j)] and S[int(j)]["k"] != c["k"]]
        near = [g for g in near if g.intersects(c["g"])]
        if not near:
            continue
        if c["g"].intersection(unary_union(near)).area > SWALLOW * c["g"].area:
            alive[i] = False
    return [c for i, c in enumerate(S) if alive[i]]


def junction_fill(strips):
    """[(k, lo, hi, v0, v1)] carrying strip ends across the perpendicular
    strips they touch."""
    from shapely.strtree import STRtree
    if not strips:
        return []
    tree = STRtree([c["g"].buffer(JUNCTION_TOL) for c in strips])
    out = []
    for i, c in enumerate(strips):
        kc, ks = math.cos(math.radians(c["k"])), math.sin(math.radians(c["k"]))
        for j in tree.query(c["g"].buffer(JUNCTION_TOL)):
            p = strips[int(j)]
            if p is c:
                continue
            d = abs(((p["k"] - c["k"]) + 90) % 180 - 90)
            if abs(d - 90) > 1.5:
                continue
            pts = [(x * kc + y * ks, -x * ks + y * kc) for x, y in list(p["g"].exterior.coords)[:4]]
            pu0, pu1 = min(q[0] for q in pts), max(q[0] for q in pts)
            pv0, pv1 = min(q[1] for q in pts), max(q[1] for q in pts)
            if pv1 < c["v0"] - JUNCTION_TOL or pv0 > c["v1"] + JUNCTION_TOL:
                continue
            if abs(c["hi"] - pu0) <= JUNCTION_TOL:
                out.append((c["k"], c["hi"], pu1, c["v0"], c["v1"]))
            if abs(c["lo"] - pu1) <= JUNCTION_TOL:
                out.append((c["k"], pu0, c["lo"], c["v0"], c["v1"]))
    return out


def end_caps(c, faces):
    kp = (c["k"] + 90) % 180
    res = []
    for uend in (c["lo"], c["hi"]):
        ok = False
        for r in faces.get(kp, []):
            for sgn in (1, -1):
                for sg2 in (1, -1):
                    if abs(sg2 * r[2] - uend) < CAP_TOL:
                        a, b = sorted((sgn * r[0], sgn * r[1]))
                        if (a <= c["v0"] + CAP_TOL and b >= c["v1"] - CAP_TOL
                                and (b - a) < (c["v1"] - c["v0"]) + 2 * CAP_TOL + 400):
                            if min(b, c["v1"]) - max(a, c["v0"]) >= 0.9 * (c["v1"] - c["v0"]):
                                ok = True
        res.append(ok)
    return res


def _overlap(a, b):
    """The overlap of two angle ranges (degrees, lo < hi), wrapping."""
    best = None
    for k in (-360.0, 0.0, 360.0):
        lo, hi = max(a[0], b[0] + k), min(a[1], b[1] + k)
        if hi > lo and (best is None or hi - lo > best[1] - best[0]):
            best = (lo, hi)
    return best


def arc_strips(arcs, reach):
    """12. Curved walls: [{"g", "t", "pts"}] from arcs (cx, cy, r, a0, a1,
    layer) on wall layers - each face paired with the nearest concentric
    face outside it, T_MIN-T_MAX away."""
    from shapely.geometry import Point, Polygon
    A = []
    for cx, cy, r, a0, a1, layer in arcs or ():
        if not is_wall_layer(layer):
            continue
        m = math.radians((a0 + a1) / 2.0)
        if not reach.contains(Point(cx + r * math.cos(m), cy + r * math.sin(m))):
            continue
        A.append([cx, cy, r, a0, a1])
    groups = []
    for a in sorted(A, key=lambda q: (q[0], q[1])):
        for g in groups:
            if math.hypot(g[0][0] - a[0], g[0][1] - a[1]) <= ARC_CENTRE_TOL:
                g.append(a)
                break
        else:
            groups.append([a])
    out = []
    for g in groups:
        faces = []
        for a in sorted(g, key=lambda q: q[2]):          # one face drawn twice is one face
            for f in faces:
                if abs(f[2] - a[2]) <= ARC_SAME_FACE and _overlap((f[3], f[4]), (a[3], a[4])):
                    f[3], f[4] = min(f[3], a[3]), max(f[4], a[4])
                    break
            else:
                faces.append(list(a))
        for f in faces:
            best = None
            for h in faces:
                d = h[2] - f[2]
                if not (T_MIN <= d <= T_MAX):
                    continue
                ov = _overlap((f[3], f[4]), (h[3], h[4]))
                if ov is None or math.radians(ov[1] - ov[0]) * (f[2] + h[2]) / 2.0 < MIN_RUN:
                    continue
                if best is None or d < best[0]:
                    best = (d, h, ov)
            if best is None:
                continue
            d, h, (lo, hi) = best
            n = max(4, int((hi - lo) / 2.0) + 1)
            outer = [(f[0] + h[2] * math.cos(math.radians(lo + (hi - lo) * i / n)),
                      f[1] + h[2] * math.sin(math.radians(lo + (hi - lo) * i / n))) for i in range(n + 1)]
            inner = [(f[0] + f[2] * math.cos(math.radians(lo + (hi - lo) * i / n)),
                      f[1] + f[2] * math.sin(math.radians(lo + (hi - lo) * i / n))) for i in range(n, -1, -1)]
            pg = Polygon(outer + inner).buffer(0)
            if not pg.is_empty and pg.geom_type == "Polygon":
                out.append({"g": pg, "t": d, "pts": [tuple(c) for c in list(pg.exterior.coords)[:-1]]})
    return out


def trace_floor(segs, hatches, texts, plate, arcs=None, skip=None, locked=None, counts=None) -> Optional[Dict]:
    """One floor. segs: engine raw segments (sx, sy, ex, ey, layer, length,
    lw, is_door, not_wall); hatches: [(outer, holes, layer, pattern, solid)];
    texts: [{"text", "x", "y"}]; plate: the floor outline (points); arcs:
    curved faces [(cx, cy, r, a0, a1, layer)] (dxf_reader._collect_wall_arcs).

    skip(seg) -> True for a line that is never a wall face here (an X's
    diagonals and its own frame, dxf_reader._seg_key); locked: a geometry
    already decided (the element pipeline's LockedMask union: the X marks'
    boxes, the columns) - no hatch inside it is wall material, no line
    inside it a face, nothing left over inside it a shaft or a column.

    {"strips": [{"pts", "t", "layers", "verdict", "hatched"}], "junctions": [pts],
     "material": shapely geometry, "shafts" / "columns" / "review_blocks":
     [Polygon], "verdicts": {verdict: [strip]}, "hatched_traced_pct"}"""
    if not ENABLED:
        return None
    from shapely.geometry import Polygon, Point
    from shapely.ops import unary_union
    try:
        pl = Polygon(plate).buffer(0)
    except (ValueError, TypeError):
        return None
    if pl.is_empty:
        return None
    reach = pl.buffer(1500.0)
    locked_in = None
    if locked is not None and not locked.is_empty:
        locked_in = locked.buffer(-2.0)               # inside it, not along its outline
        if locked_in.is_empty:
            locked_in = None
    wall_lines, struct_lines, fixture_pts = [], [], []
    n_skip = n_locked = 0
    for sg in segs or ():
        layer = sg[4] if len(sg) > 4 else ""
        if len(sg) > 7 and sg[7]:
            continue                                  # a door leaf or frame
        if len(sg) > 8 and sg[8]:
            continue                                  # a cut-out outline
        if skip is not None and skip(sg):
            n_skip += 1
            continue                                  # an X's diagonal or frame
        mx, my = (sg[0] + sg[2]) / 2.0, (sg[1] + sg[3]) / 2.0
        if locked_in is not None and locked_in.contains(Point(mx, my)) and \
                locked_in.contains(Point(sg[0], sg[1])) and locked_in.contains(Point(sg[2], sg[3])):
            n_locked += 1
            continue                                  # drawn inside something decided already
        if is_wall_layer(layer):
            if reach.contains(Point(mx, my)):
                wall_lines.append((sg[0], sg[1], sg[2], sg[3]))
        elif is_structure_layer(layer):
            if reach.contains(Point(mx, my)):
                struct_lines.append((sg[0], sg[1], sg[2], sg[3]))
        elif FIXTURE_LAYER.search(str(layer or "")):
            fixture_pts.append((mx, my))
    curved = arc_strips(arcs, reach)
    if len(wall_lines) < 2 and len(struct_lines) < 2 and not curved:
        return None

    raw_faces = faces_by_direction(wall_lines)
    faces = {k: merge_collinear(R) for k, R in raw_faces.items()}
    strips = drop_swallowed(nearest_face_strips(faces)) if len(wall_lines) >= 2 else []
    # 10. structure: paired on its own, up to T_MAX_STRUCT, wall-like runs
    struct = []
    if len(struct_lines) >= 2:
        sfaces = {k: merge_collinear(R) for k, R in faces_by_direction(struct_lines).items()}
        su = unary_union([c["g"] for c in strips]) if strips else Polygon()
        for c in drop_swallowed(nearest_face_strips(sfaces, tmax=T_MAX_STRUCT)):
            if c["hi"] - c["lo"] < max(STRUCT_MIN_RUN, 2.0 * c["t"]):
                continue
            if not su.is_empty and c["g"].intersection(su).area > 0.5 * c["g"].area:
                continue
            struct.append(c)
    if not strips and not struct and not curved:
        return None
    strips_union = unary_union([c["g"] for c in strips + struct] + [c["g"] for c in curved])

    groups = collections.defaultdict(list)
    for h in hatches or ():
        try:
            g = Polygon(h[0], [r for r in (h[1] or []) if len(r) >= 3]).buffer(0)
        except (ValueError, TypeError, IndexError):
            continue
        if g.is_empty or g.area <= 1e3 or not reach.contains(g.representative_point()):
            continue
        if locked is not None and not locked.is_empty and g.intersects(locked):
            # a hatch inside an X mark's box or a column is that element's,
            # not wall material (BHUB: 18 hatched shaft boxes, 7.6 sq.m,
            # were filled in as wall before the X had been decided)
            g = g.difference(locked)
            if g.is_empty or g.area <= 1e3:
                n_locked += 1
                continue
            g = unary_union([q for q in getattr(g, "geoms", [g]) if q.geom_type == "Polygon" and q.area > 1e3])
            if g.is_empty:
                continue
        groups[(h[2], h[3], bool(h[4]) if len(h) > 4 else False)].append(g)
    if counts is not None:
        counts["walltrace_lines_skipped"] = counts.get("walltrace_lines_skipped", 0) + n_skip
        counts["walltrace_in_locked"] = counts.get("walltrace_in_locked", 0) + n_locked
    material, solid_material = [], []
    for _key, gs in groups.items():
        u = unary_union(gs)
        if u.area >= HATCH_GROUP_MIN and u.intersection(strips_union).area / u.area > HATCH_IN_STRIPS:
            material.append(u)
            if _key[2] or str(_key[1] or "").upper() == "SOLID":
                solid_material.append(u)
    H = unary_union(material) if material else Polygon()
    Hs = unary_union(solid_material) if solid_material else Polygon()

    def filled(g):
        return (g.intersection(H).area / g.area) if not H.is_empty and g.area > 0 else 0.0

    # 10. structure strips stand where the structure hatch fills them
    struct = [c for c in struct if filled(c["g"]) > FILLED]
    if not strips and not struct and not curved:
        return None                                   # nothing traced: the engine keeps its own strips

    edge = pl.exterior.buffer(EDGE_TOL)

    def on_edge(c, v):
        f = _rect(c["k"], c["lo"], c["hi"], v - 0.5, v + 0.5)
        return f.area > 0 and f.intersection(edge).area / f.area >= EDGE_SHARE

    by_k = collections.defaultdict(list)
    for c in strips:
        by_k[c["k"]].append(c)
    verdict = {}
    for c in strips:
        nbrs = [x for x in by_k[c["k"]] if x is not c
                and (abs(x["v0"] - c["v1"]) < .2 or abs(x["v1"] - c["v0"]) < .2)
                and min(x["hi"], c["hi"]) - max(x["lo"], c["lo"]) > 50]
        if not nbrs:
            continue
        if filled(c["g"]) <= FILLED:
            # 11. the slab edge outside a wall: one face on the plate's
            # edge, a wall strip on the other
            if (on_edge(c, c["v0"]) and any(abs(x["v0"] - c["v1"]) < .2 for x in nbrs)) or \
                    (on_edge(c, c["v1"]) and any(abs(x["v1"] - c["v0"]) < .2 for x in nbrs)):
                verdict[id(c)] = "edge"
                continue
        if filled(c["g"]) > FILLED:
            continue
        if max(filled(x["g"]) for x in nbrs) > FILLED:
            side = []
            for lo in (True, False):
                probe = (_rect(c["k"], c["lo"], c["hi"], c["v0"] - 60, c["v0"] - 5) if lo
                         else _rect(c["k"], c["lo"], c["hi"], c["v1"] + 5, c["v1"] + 60))
                side.append(probe.intersection(H).area / probe.area if probe.area else 0.0)
            x0, y0, x1, y1 = c["g"].buffer(LEDGE_REACH).bounds
            nfix = sum(1 for qx, qy in fixture_pts if x0 <= qx <= x1 and y0 <= qy <= y1)
            if min(side) > FILLED:
                verdict[id(c)] = "wall_squeezed"
            elif nfix >= LEDGE_MIN_FIXTURE_SEGS:
                verdict[id(c)] = "ledge"
            else:
                verdict[id(c)] = "review"
        else:
            caps = end_caps(c, faces)
            if all(caps) and any(not any(end_caps(x, faces)) for x in nbrs):
                verdict[id(c)] = "gap" if UNFILLED_SANDWICH == "gap" else "wall_squeezed"

    walls = [c for c in strips if verdict.get(id(c)) not in ("gap", "ledge", "edge")]
    for c in struct:
        verdict[id(c)] = "structure"
    walls += struct
    junctions = junction_fill(walls)
    footprint = unary_union([c["g"] for c in walls] + [_rect(*j) for j in junctions] + [c["g"] for c in curved])

    shafts, columns, review_blocks, hatch_fill = [], [], [], []
    if not H.is_empty:
        no_gap = [c for c in strips if verdict.get(id(c)) != "gap"] + struct
        traced = unary_union([c["g"] for c in no_gap] + [_rect(*j) for j in junction_fill(no_gap)]
                             + [c["g"] for c in curved])
        left = H.difference(traced.buffer(1))
        pieces = [] if left.is_empty else ([left] if left.geom_type == "Polygon" else
                                           [p for p in getattr(left, "geoms", []) if p.geom_type == "Polygon"])
        lab = [(str(t.get("text") or ""), float(t.get("x") or 0), float(t.get("y") or 0)) for t in texts or ()]
        edge_lines = []

        def edged(p):
            """Lines drawn on any layer along most of the piece's outline."""
            if not edge_lines:
                from shapely.geometry import LineString
                from shapely.strtree import STRtree
                ls = [LineString([(sg[0], sg[1]), (sg[2], sg[3])]) for sg in segs or ()
                      if not (len(sg) > 7 and sg[7]) and math.hypot(sg[2] - sg[0], sg[3] - sg[1]) >= 20.0
                      and not EDGE_NOT_LAYER.search(str(sg[4] if len(sg) > 4 else "").split("$")[-1].split("|")[-1])]
                edge_lines.append((ls, STRtree(ls) if ls else None))
            ls, tree = edge_lines[0]
            ring = p.exterior
            if tree is None or ring.length <= 0:
                return False
            near = [ls[int(k)] for k in tree.query(ring.buffer(HATCH_FILL_EDGE_TOL))]
            if not near:
                return False
            cover = unary_union([q.buffer(HATCH_FILL_EDGE_TOL, cap_style=2) for q in near])
            return ring.intersection(cover).length >= HATCH_FILL_EDGED * ring.length

        def thin(p):
            """No wider than a wall anywhere: eroded by half a wall's width,
            nothing is left - or, along a wall at least three times that
            long, only specks (where a thick wall turns a corner or meets
            another). A column 500 x 600 leaves a speck too: it is short."""
            core = p.buffer(-HATCH_FILL_MAX_T / 2.0)
            if core.is_empty:
                return True
            if core.area > HATCH_FILL_CORE_SQM:
                return False
            r = p.minimum_rotated_rectangle
            if r.geom_type != "Polygon":
                return False
            xy = list(r.exterior.coords)[:3]
            return max(math.hypot(xy[1][0] - xy[0][0], xy[1][1] - xy[0][1]),
                       math.hypot(xy[2][0] - xy[1][0], xy[2][1] - xy[1][1])) >= 3.0 * HATCH_FILL_MAX_T

        def fill(p):
            g = p.buffer(1.5, join_style=2).intersection(H)
            return [q for q in getattr(g, "geoms", [g]) if q.geom_type == "Polygon" and q.area > 500]

        for p in pieces:
            w, h = p.bounds[2] - p.bounds[0], p.bounds[3] - p.bounds[1]
            a, b = sorted((w, h))
            if p.area <= 500:
                continue
            by_box = a <= BLOCK_MIN_SIDE
            if by_box or thin(p):
                # 13. no wider than a wall, beside a traced wall or edged by
                # lines on any layer: the hatch is the wall where its faces
                # do not pair. Thin only by its thickness (its box is wider:
                # an L, a diagonal) it must be edged - a hatch's sliver
                # running corner to corner across a room is not a wall (P25)
                if HATCH_FILL and p.area >= HATCH_FILL_MIN and \
                        ((by_box and p.distance(traced) <= 3.0) or edged(p)):
                    hatch_fill += fill(p)
                    continue
                if by_box:
                    continue
            near = p.buffer(SHAFT_LABEL_REACH)
            if any(SHAFT_LABEL.search(s) and near.contains(Point(x, y)) for s, x, y in lab):
                if not (SOLID_NOT_SHAFT and not Hs.is_empty and p.intersection(Hs).area >= 0.8 * p.area):
                    shafts.append(p)
                    continue
                # 9. solid fill is concrete, never a shaft: wall where it
                # stands against a traced wall or is edged
                if HATCH_FILL and (p.distance(traced) <= 3.0 or edged(p)):
                    hatch_fill += fill(p)
                    continue
            if SOLID_AGAINST_WALL and HATCH_FILL and not Hs.is_empty and \
                    p.intersection(Hs).area >= 0.8 * p.area and p.distance(traced) <= 3.0:
                hatch_fill += fill(p)
                continue
            if b <= COLUMN_MAX_SIDE and b <= 1.3 * a:
                columns.append(p)
            else:
                review_blocks.append(p)
        # ... and a wall's run carried on across the gap the filled pieces
        # sit in: two strips of one band, a short gap between, the gap mostly
        # wall hatch - a block wall behind a basin niche keeps its face
        if HATCH_FILL and hatch_fill:
            F = unary_union(hatch_fill)
            bands = collections.defaultdict(list)
            for c in walls:
                bands[(c["k"], round(c["v0"] / 2.0), round(c["v1"] / 2.0))].append(c)
            for lst in bands.values():
                lst.sort(key=lambda c: c["lo"])
                for a_, b_ in zip(lst, lst[1:]):
                    gap = b_["lo"] - a_["hi"]
                    if not (1.0 < gap <= HATCH_BRIDGE_MAX):
                        continue
                    R = _rect(a_["k"], a_["hi"], b_["lo"], a_["v0"], a_["v1"])
                    if R.area > 0 and R.intersection(F).area >= HATCH_BRIDGE_SHARE * R.area:
                        hatch_fill.append(R)
            if len(hatch_fill) > 1:
                u = unary_union(hatch_fill)
                hatch_fill = [q for q in getattr(u, "geoms", [u]) if q.geom_type == "Polygon" and q.area > 500]
        if hatch_fill:
            footprint = unary_union([footprint] + hatch_fill)
        Hw = H.difference(unary_union(shafts + columns + review_blocks)) if (shafts or columns or review_blocks) else H
        pct = round(100.0 * Hw.intersection(footprint).area / Hw.area, 1) if Hw.area >= 3e6 else None
    else:
        pct = None

    out_strips = []
    for c in walls:
        out_strips.append({"pts": _rect_pts(c["k"], c["lo"], c["hi"], c["v0"], c["v1"]),
                           "t": c["t"], "layers": [], "verdict": verdict.get(id(c), "wall"),
                           "hatched": filled(c["g"]) > FILLED})
    for j in junctions:
        k, lo, hi, v0, v1 = j
        if hi - lo <= 1.0:
            continue
        out_strips.append({"pts": _rect_pts(k, lo, hi, v0, v1), "t": v1 - v0, "layers": [], "verdict": "junction"})
    for c in curved:
        out_strips.append({"pts": c["pts"], "t": c["t"], "layers": [], "verdict": "curved"})
    for g in hatch_fill:
        ring = []
        for q in list(g.simplify(0.5, preserve_topology=True).exterior.coords)[:-1]:
            if not ring or math.hypot(q[0] - ring[-1][0], q[1] - ring[-1][1]) > 0.1:
                ring.append((float(q[0]), float(q[1])))
        if len(ring) >= 2 and math.hypot(ring[0][0] - ring[-1][0], ring[0][1] - ring[-1][1]) <= 0.1:
            ring.pop()
        if len(ring) < 3:
            continue
        # the longest side first: wall_polygons reads a strip's direction from it
        k0 = max(range(len(ring)), key=lambda i: math.hypot(ring[(i + 1) % len(ring)][0] - ring[i][0],
                                                            ring[(i + 1) % len(ring)][1] - ring[i][1]))
        out_strips.append({"pts": ring[k0:] + ring[:k0],
                           "t": round(2.0 * g.area / g.length, 1) if g.length else 0.0, "layers": [], "verdict": "hatch",
                           "hatched": True})
    verdicts = collections.Counter(verdict.get(id(c), "wall") for c in strips + struct)
    if curved:
        verdicts["curved"] = len(curved)
    if hatch_fill:
        verdicts["hatch"] = len(hatch_fill)
    runs = collections.defaultdict(float)
    for c in strips + struct:
        runs[verdict.get(id(c), "wall")] += (c["hi"] - c["lo"]) / 1000.0
    return {"strips": out_strips, "material": H, "footprint": footprint,
            "shafts": shafts, "columns": columns, "review_blocks": review_blocks,
            "review_strips": [_rect_pts(c["k"], c["lo"], c["hi"], c["v0"], c["v1"]) for c in strips
                              if verdict.get(id(c)) == "review"],
            "verdicts": dict(verdicts), "runs_m": {k: round(v, 1) for k, v in runs.items()},
            "hatched_traced_pct": pct,
            "footprint_sqm": round(footprint.area / 1e6, 2),
            "hatch_fill_sqm": round(sum(g.area for g in hatch_fill) / 1e6, 2),
            "run_m": round(sum(c["hi"] - c["lo"] for c in walls) / 1000.0, 1)}
