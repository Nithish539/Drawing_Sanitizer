# -*- coding: utf-8 -*-
"""Glass and doors: glass partitions and facade glazing told apart from the
walls, and what each door opening is for.

The building elements of a floor are found in the order an architect
reads a plan for an area statement:

  1. the drawing's own furniture is cleared - grid lines and bubbles,
     section and elevation marks, tags, title blocks, landscape
     (dxf_reader._is_annotation, applied as the drawing is read);
  2. walls: two faces a wall's thickness apart, confirmed by the hatch
     (walltrace.trace_floor);
  3. doors (dxf_reader.door_openings: a swing at a gap in a wall, or a
     leaf drawn open in a board), windows (dxf_reader.window_openings:
     glazing or a louvre across a gap in a wall, and the X-marked outlines
     of WindowFinder) and glass (here);
  4. rooms, traced from the walls with the doors closed.

A door is there to get out of a room: each opening is told by the rooms on
its two sides (door_kinds) - a shaft, riser or lift on either side is a
shaft door, the same room on both sides of a door in a board a cubicle door
(a WC cubicle), anything else an exit. A door between two spaces is drawn
on AI_P_DOORS, a door inside one room (a cubicle's) on AI_S_DOORS
(door_levels); each room's exits are listed from the door layers when the
drawing is read for areas.

Glass here is a line on a glass / glazing / curtain-wall layer (or in a
glass partition block) that is not a mirror, not in a window and not in a
door: two such lines a pane's thickness apart (GLASS_PAIR_MM) are one
glass strip between them; a single line is a strip GLASS_LINE_MM wide
along it. It is drawn on AI_GLASS - never a wall, never a room, never in an
area (P25 1st floor: the AR-GLASS PART shopfront partitions and the
AR-GLAZZING facade).
"""

from __future__ import annotations

import bisect
import collections
import math
import re
from typing import List

from .config import CONFIG

_GL = CONFIG.get("glass", {}) if isinstance(CONFIG, dict) else {}
ENABLED = bool(_GL.get("enabled", True))
GLASS_LAYER_RE = re.compile(str(_GL.get("layer_regex", r"GLASS|GLAZ|CURTAIN|MULLION")), re.I)
GLASS_BLOCK_RE = re.compile(str(_GL.get("block_regex", r"GLASS|GLAZ|CURTAIN")), re.I)
GLASS_NOT_RE = re.compile(str(_GL.get("not_regex", r"MIRROR|^\d+(\.\d+)+\s|\bEXC\b")), re.I)
GLASS_PAIR_MM = tuple(float(v) for v in (_GL.get("pair_mm") or [3.0, 150.0]))
GLASS_LINE_MM = float(_GL.get("single_line_width_mm", 20.0))
GLASS_MIN_RUN_MM = float(_GL.get("min_run_mm", 300.0))
ANGLE_BIN = 1.0
COLLINEAR_TOL = 2.0


def is_glass(layer, blocks=()) -> bool:
    last = str(layer or "").split("$")[-1]
    if GLASS_NOT_RE.search(last) or any(GLASS_NOT_RE.search(str(b or "").split("$")[-1]) for b in blocks or ()):
        return False
    if GLASS_LAYER_RE.search(last):
        return True
    return any(GLASS_BLOCK_RE.search(str(b or "").split("$")[-1]) for b in blocks or ())


def collect_glass(doc, msp, units=None) -> List[tuple]:
    """Glass lines in mm: [(x1, y1, x2, y2)] - on a glass layer or in a glass
    block, not a mirror, not drawing furniture, not a door's."""
    from . import dxf_reader as dr
    from .dxfio import iter_entities, effective_layer
    units = units or dr.MM_UNITS
    out = []
    for e, xf, _d in iter_entities(doc, msp, "LINE LWPOLYLINE POLYLINE"):
        try:
            layer = effective_layer(e, xf)
            blocks = [b or "" for b in xf.blocks]
            if not is_glass(layer, blocks) or dr._is_annotation(layer, blocks) or dr._is_door_leaf(layer, blocks):
                continue
            if e.dxftype() == "LINE":
                pts = [units.point_to_mm(*xf.apply(e.dxf.start.x, e.dxf.start.y, e.dxf.start.z)),
                       units.point_to_mm(*xf.apply(e.dxf.end.x, e.dxf.end.y, e.dxf.end.z))]
            else:
                pts = dr._polyline_points(e, units, xf)
                if dr._is_closed(e) and len(pts) > 1:
                    pts = pts + [pts[0]]
            for i in range(len(pts) - 1):
                if math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) >= 5.0:
                    out.append((pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]))
        except Exception:                             # noqa: BLE001
            continue
    return out


def _rect(k, lo, hi, v0, v1):
    from shapely.geometry import Polygon
    c, s = math.cos(math.radians(k)), math.sin(math.radians(k))
    return Polygon([(u * c - v * s, u * s + v * c) for u, v in [(lo, v0), (hi, v0), (hi, v1), (lo, v1)]])


def _faces(segs):
    by = collections.defaultdict(list)
    for x1, y1, x2, y2 in segs:
        a = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180
        k = round(a / ANGLE_BIN) * ANGLE_BIN % 180
        c, s = math.cos(math.radians(k)), math.sin(math.radians(k))
        u1, u2 = x1 * c + y1 * s, x2 * c + y2 * s
        v = (-x1 * s + y1 * c - x2 * s + y2 * c) / 2
        by[k].append([min(u1, u2), max(u1, u2), v])
    out = {}
    for k, lines in by.items():                       # one face per offset, overlaps joined
        lines = sorted(lines, key=lambda r: r[2])
        merged, i = [], 0
        while i < len(lines):
            j, grp = i, [lines[i]]
            while j + 1 < len(lines) and lines[j + 1][2] - grp[0][2] <= COLLINEAR_TOL:
                j += 1
                grp.append(lines[j])
            v = sum(g[2] for g in grp) / len(grp)
            iv = sorted((g[0], g[1]) for g in grp)
            cur = list(iv[0])
            for a, b in iv[1:]:
                if a <= cur[1] + 1.0:
                    cur[1] = max(cur[1], b)
                else:
                    merged.append([cur[0], cur[1], v])
                    cur = [a, b]
            merged.append([cur[0], cur[1], v])
            i = j + 1
        out[k] = merged
    return out


def glass_elements(segs, plate=None, cut=()):
    """The glass of one floor as polygons: neighbouring glass lines a pane's
    thickness apart (GLASS_PAIR_MM) paired into the strip between them, the
    rest of each line a GLASS_LINE_MM band; less what lies in `cut` (the
    windows, the door openings); pieces shorter than GLASS_MIN_RUN_MM go."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    if not ENABLED or not segs:
        return []
    reach = Polygon(plate).buffer(1500) if plate else None
    if reach is not None:
        from shapely.geometry import Point
        segs = [s for s in segs if reach.contains(Point((s[0] + s[2]) / 2.0, (s[1] + s[3]) / 2.0))]
    faces = _faces(segs)
    pieces = []
    for k, R in faces.items():
        events = collections.defaultdict(lambda: ([], []))
        for r in R:
            if r[1] - r[0] < 1e-6:
                continue
            events[r[0]][0].append(r[2])
            events[r[1]][1].append(r[2])
        xs = sorted(events)
        active = []
        for a, b in zip(xs, xs[1:]):
            adds, rems = events[a]
            for v in rems:
                i = bisect.bisect_left(active, v - 1e-9)
                if i < len(active) and abs(active[i] - v) < 1e-6:
                    active.pop(i)
            for v in adds:
                bisect.insort(active, v)
            if b - a < 1e-6 or not active:
                continue
            used = set()
            for i, (v0, v1) in enumerate(zip(active, active[1:])):
                if GLASS_PAIR_MM[0] <= v1 - v0 <= GLASS_PAIR_MM[1]:
                    pieces.append(_rect(k, a, b, v0, v1))
                    used.update((i, i + 1))
            for i, v in enumerate(active):
                if i not in used:
                    pieces.append(_rect(k, a, b, v - GLASS_LINE_MM / 2.0, v + GLASS_LINE_MM / 2.0))
    if not pieces:
        return []
    u = unary_union([p.buffer(0.5, join_style=2) for p in pieces]).buffer(-0.5, join_style=2)
    cuts = [c if hasattr(c, "geom_type") else Polygon(c).buffer(0) for c in cut or ()]
    cuts = [c for c in cuts if c is not None and not c.is_empty]
    if cuts:
        u = u.difference(unary_union(cuts).buffer(5))
    out = []
    for g in getattr(u, "geoms", [u]):
        if g.geom_type != "Polygon" or g.is_empty:
            continue
        mrr = g.minimum_rotated_rectangle
        xy = list(mrr.exterior.coords)
        L = max(math.hypot(xy[i + 1][0] - xy[i][0], xy[i + 1][1] - xy[i][1]) for i in range(4))
        if L >= GLASS_MIN_RUN_MM:
            out.append(g.simplify(1.0, preserve_topology=True))
    return out


def glass_report(polys):
    """{"count", "length_m", "area_sqm"} of the glass pieces (length: half
    each piece's perimeter less its width, as for walls)."""
    n, L, A = 0, 0.0, 0.0
    for g in polys or ():
        n += 1
        A += g.area
        w = 2.0 * g.area / g.length if g.length else 0.0
        L += max(0.0, g.length / 2.0 - w)
    return {"count": n, "length_m": round(L / 1000.0, 1), "area_sqm": round(A / 1e6, 2)}


# -- Doors: what each opening is for ---------------------------------------
# config.json "wall_output". A door is there to get out of a room: the
# opening between two different rooms (or a room and what is not traced) is
# an exit. A shaft's access door and a WC cubicle's door are not.
_WO = (CONFIG.get("wall_output") or {}) if isinstance(CONFIG, dict) else {}
DOOR_KINDS = bool(_WO.get("door_kinds", True))
DOOR_SIDE_PROBES_MM = tuple(float(v) for v in (_WO.get("door_side_probes_mm") or [150, 300, 500, 800, 1100, 1400]))
# A cubicle's door stands in a board (12-50 mm); one in a wall with the same
# room on both sides is the room's way out, traced through (an exit)
DOOR_CUBICLE_BOARD_MAX_MM = float(_WO.get("door_cubicle_board_max_mm", 60.0))
DOOR_SHAFT_LABEL_MM = float(_WO.get("door_shaft_label_mm", 900.0))
DOOR_SHAFT_FAMILIES = tuple(str(v) for v in (_WO.get("door_shaft_families") or ["Shaft", "Lift"]))
SHAFT_ROOM_RE = re.compile(str(_WO.get(
    "door_shaft_room_regex",
    r"SHAFT|RISER|\bDUCT\b|\bVENT\b|\bSH\b|\bSH\.|\bFHC\b|\bFHR\b|FIRE\s*HOSE|\bTEN\b|TENANT|"
    r"\bCOMM\b|FAPA|\bPHE\b|PRESS(?!.*\bROOM\b)|EXHAUST|CUT[\s-]*OUT|LIFT\s*WELL|\bRWP\b|DOWN\s*TAKE")), re.I)
EXIT, SHAFT, CUBICLE, LIFT = "exit", "shaft", "cubicle", "lift"
DOOR_KIND_NAMES = (EXIT, SHAFT, CUBICLE, LIFT)
# A lift is a lift well with a stop on this floor: not a pressurisation
# shaft named for the lift (LIFT PRE, LIFT PRESS), not its lobby or machine
# room, and not a well that only passes (LIFT WELL BELOW).
LIFT_ROOM_RE = re.compile(str(_WO.get("lift_room_regex", r"\bLIFT\b|ELEVATOR|\bPAX\b|P\.\s*LIFT|\bL\d+\b")), re.I)
LIFT_NOT_RE = re.compile(str(_WO.get(
    "lift_not_regex", r"\bPRE\b|\bPRES+|PRESSURI|LOBBY|MACHINE|\bM\.?R\b|\bBELOW\b|\bABOVE\b|\bPIT\b|\bOHT\b")), re.I)
# A lift entrance: a gap in the lift's own wall this long, facing the floor
LIFT_DOOR_MM = tuple(float(v) for v in (_WO.get("lift_door_mm") or [700, 2600]))
LIFT_DOORS = bool(_WO.get("lift_doors", True))


def is_lift_room(label="", family=""):
    """A lift well with its stop on this floor: the Lift family or a lift's
    name, and not a pressurisation shaft, lobby, machine room or a well
    passing through (LIFT_NOT_RE)."""
    n = " ".join(str(label or "").split())
    fam = str(family or "")
    if n and LIFT_NOT_RE.search(n):
        return False
    if fam == "Lift":
        return True
    return fam in ("", "Unknown") and bool(n and LIFT_ROOM_RE.search(n) and not SHAFT_ROOM_RE.search(n))


def lift_door_quads(lifts, walls, others=(), avoid=(), segs=(), open_front=False):
    """The entrances of the lifts: on each side of a lift well (shapely
    polygons), the stretch of LIFT_DOOR_MM with no wall along it while the
    rest of that side is walled, opening onto the floor - not another lift
    or shaft (`others`: the wells, shapely: lifts, shafts). Its depth is the wall's
    beside it (200 mm with none). A lift's doors slide: there is no swing to
    find. walls: the wall region (shapely); avoid: openings already found.
    Returns [(quad points, evidence)] - evidence the lines drawn in the gap
    along it (a door or sill, from segs: (x1, y1, x2, y2, layer, ...)).

    open_front (rule lift_without_door_mechanical, 2026-10-17): a lift with
    no such entrance whose front is open its whole length - no wall along
    it, at least two of its other sides walled, the floor beyond (not
    another lift or shaft) - has that whole front as its door."""
    from shapely.geometry import Point, LineString, Polygon
    from shapely.strtree import STRtree
    if not LIFT_DOORS or not lifts or walls is None or walls.is_empty:
        return []
    others = [g for g in others or () if g is not None and not g.is_empty]
    otree = STRtree(others) if others else None
    avoid = [g for g in avoid or () if g is not None and not g.is_empty]
    seg_lines = [LineString([s[:2], s[2:4]]) for s in segs or () if math.hypot(s[2] - s[0], s[3] - s[1]) >= 100.0]
    stree = STRtree(seg_lines) if seg_lines else None
    out = []
    STEP = 50.0
    for g in lifts:
        if g is None or g.is_empty:
            continue
        r = g.minimum_rotated_rectangle
        if r.geom_type != "Polygon":
            continue
        cs = list(r.exterior.coords)[:4]
        ctr = r.centroid
        n_before = len(out)
        side_walled = []                               # (share walled, a, b, ux, uy, nx, ny, L)
        for k in range(4):
            a, b = cs[k], cs[(k + 1) % 4]
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            if L < LIFT_DOOR_MM[0]:
                continue
            ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
            nx, ny = -uy, ux
            if (ctr.x - a[0]) * nx + (ctr.y - a[1]) * ny > 0:
                nx, ny = -nx, -ny                      # outward
            n_ = int(L // STEP)
            walled = []
            for i_ in range(n_ + 1):
                s_ = min(i_ * STEP, L)
                px, py = a[0] + ux * s_ + nx * 100.0, a[1] + uy * s_ + ny * 100.0
                walled.append(walls.contains(Point(px, py)))
            side_walled.append((sum(walled) / float(len(walled)), a, b, ux, uy, nx, ny, L))
            if sum(walled) < 0.3 * len(walled):
                continue                               # a side with no wall along it: open, not an entrance
            i_ = 0
            while i_ < len(walled):
                if walled[i_]:
                    i_ += 1
                    continue
                j_ = i_
                while j_ < len(walled) and not walled[j_]:
                    j_ += 1
                s0, s1 = i_ * STEP, min((j_ - 1) * STEP, L)
                i_ = j_
                if not (LIFT_DOOR_MM[0] <= s1 - s0 + STEP <= LIFT_DOOR_MM[1]):
                    continue
                if s0 <= 0.0 or s1 >= L:
                    continue                           # the gap runs off the corner: not a door in this wall
                # the wall's depth beside the gap
                depth = 0.0
                for s_ in (s0 - 2 * STEP, s1 + 2 * STEP):
                    for d in range(20, 601, 20):
                        if walls.contains(Point(a[0] + ux * s_ + nx * d, a[1] + uy * s_ + ny * d)):
                            depth = max(depth, float(d))
                        else:
                            break
                depth = depth or 200.0
                m = (s0 + s1) / 2.0
                beyond = Point(a[0] + ux * m + nx * (depth + 400.0), a[1] + uy * m + ny * (depth + 400.0))
                if otree is not None and any(others[int(q)].contains(beyond) and not others[int(q)].equals(g)
                                             for q in otree.query(beyond)):
                    continue                           # another lift or a shaft beyond: not the floor
                quad = [(a[0] + ux * s0, a[1] + uy * s0), (a[0] + ux * s1, a[1] + uy * s1),
                        (a[0] + ux * s1 + nx * depth, a[1] + uy * s1 + ny * depth),
                        (a[0] + ux * s0 + nx * depth, a[1] + uy * s0 + ny * depth)]
                qg = Polygon(quad).buffer(0)
                if qg.is_empty or any(qg.intersects(v) and qg.intersection(v).area > 0.3 * qg.area for v in avoid):
                    continue
                ev = []
                if stree is not None:
                    for q in stree.query(qg.buffer(20.0)):
                        ln = seg_lines[int(q)]
                        c = ln.coords
                        dx, dy = c[1][0] - c[0][0], c[1][1] - c[0][1]
                        ll = math.hypot(dx, dy)
                        if ll and abs(dx / ll * uy - dy / ll * ux) <= 0.05 and ln.intersection(qg.buffer(20.0)).length >= 0.6 * (s1 - s0):
                            ev.append(ll)
                out.append(([(float(x), float(y)) for x, y in quad], {"lines_along": len(ev)}))
        if open_front and len(out) == n_before and len(side_walled) == 4:
            walled_sides = sum(1 for sw in side_walled if sw[0] >= 0.7)
            fronts = sorted((sw for sw in side_walled if sw[0] < 0.3), key=lambda sw: sw[0])
            for share, a, b, ux, uy, nx, ny, L in fronts[:1]:
                if walled_sides < 2:
                    break
                beyond = Point(a[0] + ux * L / 2.0 + nx * 600.0, a[1] + uy * L / 2.0 + ny * 600.0)
                if otree is not None and any(others[int(q)].contains(beyond) and not others[int(q)].equals(g)
                                             for q in otree.query(beyond)):
                    continue                           # another lift or a shaft beyond: not the floor
                depth = 200.0
                quad = [(a[0], a[1]), (b[0], b[1]), (b[0] + nx * depth, b[1] + ny * depth),
                        (a[0] + nx * depth, a[1] + ny * depth)]
                qg = Polygon(quad).buffer(0)
                if qg.is_empty or any(qg.intersects(v) and qg.intersection(v).area > 0.3 * qg.area for v in avoid):
                    continue
                out.append(([(float(x), float(y)) for x, y in quad], {"lines_along": 0, "open_front": True}))
    return out



def is_shaft_room(label="", family=""):
    """A room a door into is a shaft's access door: a shaft or lift family
    (DOOR_SHAFT_FAMILIES), or a label naming a shaft, riser, duct, vent,
    fire hose cabinet, tenant / comm / pressurisation shaft."""
    if family and str(family) in DOOR_SHAFT_FAMILIES:
        return True
    return bool(label) and bool(SHAFT_ROOM_RE.search(" ".join(str(label).split())))


def door_axes(quad):
    """(cx, cy, ux, uy, nx, ny, width, depth) of a door opening: u along its
    width (the longer side of its rectangle), n across it - the wall's
    thickness, from one room into the other. None for a degenerate one."""
    from shapely.geometry import Polygon
    try:
        g = Polygon([(float(p[0]), float(p[1])) for p in quad]).buffer(0)
    except (ValueError, TypeError, IndexError):
        return None
    if g.is_empty or g.area <= 0:
        return None
    r = g.minimum_rotated_rectangle
    if r.geom_type != "Polygon":
        return None
    xy = list(r.exterior.coords)[:4]
    e0 = (xy[1][0] - xy[0][0], xy[1][1] - xy[0][1])
    e1 = (xy[2][0] - xy[1][0], xy[2][1] - xy[1][1])
    l0, l1 = math.hypot(*e0), math.hypot(*e1)
    if l0 < l1:
        e0, l0, l1 = e1, l1, l0
    if l0 <= 0:
        return None
    ux, uy = e0[0] / l0, e0[1] / l0
    c = g.centroid
    return (c.x, c.y, ux, uy, -uy, ux, l0, l1)


def door_faces(quad):
    """The two faces of a door opening - each a line across it from one
    wall end to the other, on one face of the wall: [((x0, y0), (x1, y1)),
    ...], mm. None for a degenerate opening."""
    ax = door_axes(quad)
    if ax is None:
        return None
    cx, cy, ux, uy, nx, ny, W, D = ax
    out = []
    for sg in (1.0, -1.0):
        mx, my = cx + nx * sg * D / 2.0, cy + ny * sg * D / 2.0
        out.append(((mx - ux * W / 2.0, my - uy * W / 2.0), (mx + ux * W / 2.0, my + uy * W / 2.0)))
    return out


def door_line(quad, rooms=(), tree=None):
    """A door drawn as one line joining the walls either side, on one face
    of its opening ((x0, y0), (x1, y1)), mm - the line the room it opens
    into is traced up to (Test_2; Fix 2: always the inner face of the wall
    of the room the door serves). Since 2026-10-17 the face is the room the
    door belongs to (door_owners); this is what stands where that is not
    settled (the same room on both sides, two like rooms with nothing to
    tell them apart):
      - the face of the room the door leads into: the face a room touches,
        or of two rooms the smaller (the one entered - a lift, a WC, a
        store);
      - a room drawn over the opening itself (it should not be: rooms stop
        at their face) gets the line on the face nearer its body;
      - no room at either face: the face nearer a room, else the first.
    rooms: shapely polygons (tree: their STRtree)."""
    faces = door_faces(quad)
    if faces is None:
        return None
    if not rooms:
        return faces[0]
    from shapely.geometry import LineString, Polygon
    try:
        q = Polygon([(float(p[0]), float(p[1])) for p in quad]).buffer(0)
    except (ValueError, TypeError, IndexError):
        return faces[0]
    if q.is_empty:
        return faces[0]
    if tree is not None:
        cand = [rooms[int(k)] for k in tree.query(q.buffer(60.0))]
    else:
        cand = list(rooms)
    cand = [r for r in cand if r is not None and not r.is_empty and r.distance(q) <= 60.0]
    if not cand:
        return faces[0]
    lines = [LineString(f) for f in faces]
    W = lines[0].length or 1.0
    cover = [r for r in cand if r.intersection(q).area >= 0.5 * q.area]
    if cover:
        r = max(cover, key=lambda g: g.intersection(q).area)
        inner = r.buffer(-5.0)
        inside = [ln.intersection(inner).length if not inner.is_empty else 0.0 for ln in lines]
        return faces[0] if inside[0] <= inside[1] else faces[1]
    touch = []
    for ln in lines:
        best = None
        for r in cand:
            if ln.intersection(r.buffer(25.0)).length >= 0.5 * W and (best is None or r.area < best):
                best = r.area
        touch.append(best)
    if touch[0] is not None and touch[1] is None:
        return faces[0]
    if touch[1] is not None and touch[0] is None:
        return faces[1]
    if touch[0] is not None and touch[1] is not None:
        return faces[0] if touch[0] <= touch[1] else faces[1]
    d = [min(ln.distance(r) for r in cand) for ln in lines]
    return faces[0] if d[0] <= d[1] else faces[1]


def door_sides(quad, rooms, tree=None):
    """The rooms on the two sides of a door opening, (i, j): indices into
    rooms (shapely polygons) or None where no room is found. Each side is
    probed DOOR_SIDE_PROBES_MM beyond the opening's face, at a quarter, half
    and three quarters of its width; the nearest distance that finds a room
    decides, and of the rooms found there the one most probes find (the
    smallest holding a point: a store inside a hall is the store)."""
    ax = door_axes(quad)
    if ax is None or not rooms:
        return (None, None)
    from shapely.geometry import Point
    from shapely.strtree import STRtree
    tree = tree if tree is not None else STRtree(rooms)
    cx, cy, ux, uy, nx, ny, W, D = ax
    sides = []
    for sg in (1.0, -1.0):
        found = None
        for d in DOOR_SIDE_PROBES_MM:
            votes = collections.Counter()
            for f in (-0.25, 0.0, 0.25):
                px = cx + ux * f * W + nx * sg * (D / 2.0 + d)
                py = cy + uy * f * W + ny * sg * (D / 2.0 + d)
                pt = Point(px, py)
                hit = [int(k) for k in tree.query(pt) if rooms[int(k)].contains(pt)]
                if hit:
                    votes[min(hit, key=lambda k: rooms[k].area)] += 1
            if votes:
                found = votes.most_common(1)[0][0]
                break
        sides.append(found)
    return tuple(sides)


def door_kinds(quads, rooms, shaft, labels=(), lift=None):
    """[(kind, i, j)] for each door opening: kind EXIT, SHAFT or CUBICLE, i
    and j the rooms on its two sides (door_sides). rooms: shapely polygons;
    shaft: for each room whether it is a shaft (is_shaft_room); labels:
    [(text, x, y)] - a side with no room traced is a shaft when a label
    naming one stands within DOOR_SHAFT_LABEL_MM of it.

    A shaft, riser or lift on either side: SHAFT. The same room on both
    sides of a door in a board (no deeper than DOOR_CUBICLE_BOARD_MAX_MM):
    CUBICLE (a WC cubicle's door). Anything else - two rooms, a room and
    what is not traced, or the same room on both sides of a door in a wall
    (the room was traced through its own door: H10's toilets, XA's fire
    lift lobby) - an EXIT. lift: for each room whether it is a lift well
    with its stop here (is_lift_room) - a door into one is LIFT."""
    from shapely.strtree import STRtree
    rooms = list(rooms or ())
    tree = STRtree(rooms) if rooms else None
    lab = [(x, y) for t, x, y in labels or () if SHAFT_ROOM_RE.search(" ".join(str(t).split()))]
    out = []
    for q in quads or ():
        i, j = door_sides(q, rooms, tree) if rooms else (None, None)
        if lift is not None and ((i is not None and lift[i]) or (j is not None and lift[j])):
            out.append((LIFT, i, j))
            continue
        sh = (i is not None and shaft[i]) or (j is not None and shaft[j])
        if not sh and lab and (i is None or j is None):
            ax = door_axes(q)
            if ax is not None:
                cx, cy, ux, uy, nx, ny, W, D = ax
                for sg, side in ((1.0, i), (-1.0, j)):
                    if side is not None:
                        continue
                    px, py = cx + nx * sg * (D / 2.0 + 300.0), cy + ny * sg * (D / 2.0 + 300.0)
                    if any(math.hypot(x - px, y - py) <= DOOR_SHAFT_LABEL_MM for x, y in lab):
                        sh = True
        board = False
        if not sh and i is not None and i == j:
            ax = door_axes(q)
            board = ax is not None and ax[7] <= DOOR_CUBICLE_BOARD_MAX_MM
        kind = SHAFT if sh else (CUBICLE if board else EXIT)
        out.append((kind, i, j))
    return out


# -- Doors: primary and secondary (2026-10-17) -----------------------------
# A primary door (AI_P_DOORS) stands between two different spaces: two
# rooms, or a room and the untraced common floor - a lift's door, a shaft's
# door and a door between two rooms included. It stops a room's boundary.
# A secondary door (AI_S_DOORS) stands inside one room: a WC cubicle's door,
# or a door with the same room on both sides. It never stops a room.
# Before 2026-10-17 primary meant "entered from the main accessible area"
# and secondary "reached only through another room" (a lift's doors were
# secondary); the main accessible area is still worked out, for the
# `within` flag only. Which way a door swings is never used.
PRIMARY, SECONDARY = "primary", "secondary"
DOOR_MAIN_FAMILIES = tuple(str(v) for v in (_WO.get("door_main_families") or
                                            ["Lift Lobby", "Circulation", "Staircase", "Service Lobby"]))
MAIN_AREA_RE = re.compile(str(_WO.get(
    "door_main_area_regex",
    r"LOBBY|CORRIDOR|PASSAGE|FOYER|STAIR|LANDING|CIRCULATION|FIRE\s*TOWER|AIR\s*LOCK|WALKWAY|COMMON|"
    r"OFFICE\s*SPACE|OFFICE\s*AREA|OPEN\s*OFFICE|SERVICE\s*AREA")), re.I)
DOOR_OPEN_MIN_SQM = float(_WO.get("door_open_floor_min_sqm", 1.0))
DOOR_OPEN_CONTACT_MM = float(_WO.get("door_open_contact_mm", 600.0))


def is_main_room(label="", family=""):
    """A room that is part of the main accessible area: a circulation family
    (DOOR_MAIN_FAMILIES) or a name such as LOBBY, CORRIDOR, STAIRCASE or
    OFFICE SPACE (MAIN_AREA_RE) - never a shaft or lift."""
    if is_shaft_room(label, family):
        return False
    if family and str(family) in DOOR_MAIN_FAMILIES:
        return True
    return bool(label) and bool(MAIN_AREA_RE.search(" ".join(str(label).split())))


def open_floor(plate, rooms, solid=None, quads=(), min_sqm=None):
    """The untraced floor of a plate as polygons: the plate less the rooms,
    the walls, columns, glass and windows (`solid`) and the door openings,
    slivers under 300 mm wide dropped, pieces of min_sqm or more kept."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    min_sqm = DOOR_OPEN_MIN_SQM if min_sqm is None else min_sqm
    try:
        pl = plate if hasattr(plate, "geom_type") else Polygon([tuple(p[:2]) for p in plate]).buffer(0)
    except (ValueError, TypeError, IndexError):
        return []
    if pl.is_empty:
        return []
    cut = [g for g in rooms or () if g is not None and not g.is_empty]
    if solid is not None and not solid.is_empty:
        cut.append(solid)
    for q in quads or ():
        try:
            cut.append(Polygon([tuple(p[:2]) for p in q]).buffer(0))
        except (ValueError, TypeError, IndexError):
            continue
    rest = pl.difference(unary_union(cut)) if cut else pl
    rest = rest.buffer(-150.0, join_style=2).buffer(150.0, join_style=2)
    return [g for g in getattr(rest, "geoms", [rest]) if g.geom_type == "Polygon" and g.area >= min_sqm * 1e6]


def door_levels(quads, rooms, main, shaft, kinds=None, opens=()):
    """[(level, i, j, within)] for each door opening: level PRIMARY or
    SECONDARY; i and j the spaces on its two sides (indices into rooms +
    opens, or None); within True when both sides are main area.

    rooms: shapely polygons; main / shaft: a flag per room (is_main_room /
    is_shaft_room); kinds: door_kinds for the same quads; opens: the
    untraced floor (open_floor). The main accessible area is the main
    rooms and the largest piece of untraced floor, grown through open
    contact (a shared edge of DOOR_OPEN_CONTACT_MM, no wall between) into
    untraced floor and other main rooms, and through doors into other main
    rooms only - untraced floor behind a door is a room nobody named; it
    decides `within` only. The level:

      * a WC cubicle's door (kind CUBICLE), or a door with the same space on
        both sides, is SECONDARY - it stands inside one room;
      * every other door - two rooms, a room and the common floor, a lift's
        or a shaft's door - is PRIMARY."""
    from shapely.strtree import STRtree
    rooms = list(rooms or ())
    nodes = rooms + list(opens or ())
    nr = len(rooms)
    if not quads:
        return []
    tree = STRtree(nodes) if nodes else None
    is_open = [k >= nr for k in range(len(nodes))]
    adj = collections.defaultdict(set)                # open contact: no wall, no door between
    for k in range(nr, len(nodes)):
        g = nodes[k]
        for m in tree.query(g.buffer(12.0)):
            m = int(m)
            if m >= nr:
                continue
            c = g.intersection(nodes[m].buffer(12.0))
            if not c.is_empty and c.area / 12.0 >= DOOR_OPEN_CONTACT_MM:
                adj[k].add(m)
                adj[m].add(k)
    sides = [door_sides(q, nodes, tree) if nodes else (None, None) for q in quads]
    by_door = collections.defaultdict(set)            # through a door
    for a, b in sides:
        if a is not None and b is not None and a != b:
            by_door[a].add(b)
            by_door[b].add(a)

    def can_be_main(k, through_door):
        # through a door only into a room named as circulation: an untraced
        # space behind a door is a room nobody labelled (the final drawing
        # reads no labels, so every room there is one), not common floor
        if through_door:
            return k < nr and main[k] and not shaft[k]
        return is_open[k] or (main[k] and not shaft[k])

    seeds = [k for k in range(nr) if main[k] and not shaft[k]]
    if len(nodes) > nr:
        # the floor's largest untraced space is its common floor (the
        # corridors and lobbies nobody named, the open floor of a shell)
        seeds.append(max(range(nr, len(nodes)), key=lambda k: nodes[k].area))
    inmain, todo = set(seeds), list(seeds)
    while todo:
        k = todo.pop()
        for m, door in [(m, False) for m in adj[k]] + [(m, True) for m in by_door[k]]:
            if m not in inmain and can_be_main(m, door):
                inmain.add(m)
                todo.append(m)
    out = []
    for n, (a, b) in enumerate(sides):
        kind = kinds[n][0] if kinds else EXIT
        side_main = [None if x is None else (x in inmain) for x in (a, b)]
        if kind == CUBICLE or (a is not None and a == b):
            out.append((SECONDARY, a, b, False))     # inside one room
            continue
        within = kind not in (SHAFT, LIFT) and bool(side_main[0]) and bool(side_main[1])
        out.append((PRIMARY, a, b, within))           # between two spaces
    return out


# -- Doors: the room a door belongs to (2026-10-17) ---------------------------
# Every door belongs to one room, and its line is drawn on that room's inner
# face of the wall - in Step 3, in the geometry drawing and in the final
# drawing alike. The room is decided from what stands on the two sides of
# the opening, the higher rank winning (how big a room is never counts):
#   5  a lift well or a shaft (a riser, duct, FHC cabinet, a shaft's room)
#   4  any other room (a toilet, AHU room, store, electrical room, balcony)
#   3  a staircase (and its landing)
#   2  a lobby, foyer, fire tower, air lock, vestibule
#   1  a corridor, passage, walkway, the common or open office floor
#   0  nothing traced (the common floor), or a void
# Two rooms of one rank: the one this is the only door of; else the side
# the door is drawn on (its swing); else the one with fewer doors; else it
# is not settled and door_line's face stands. A door with the same room on
# both sides (a WC cubicle's, a room traced through its own door) keeps
# door_line's face too.
DOOR_OWNER = bool(_WO.get("door_owner", True))
OWNER_SHAFT, OWNER_ROOM, OWNER_STAIR, OWNER_LOBBY, OWNER_CIRCULATION, OWNER_NONE = 5, 4, 3, 2, 1, 0
OWNER_STAIR_RE = re.compile(str(_WO.get("door_owner_stair_regex", r"STAIR|LANDING")), re.I)
OWNER_LOBBY_RE = re.compile(str(_WO.get("door_owner_lobby_regex",
                                        r"LOBBY|FOYER|FIRE\s*TOWER|AIR\s*LOCK|VESTIBULE")), re.I)
OWNER_CIRCULATION_RE = re.compile(str(_WO.get(
    "door_owner_circulation_regex",
    r"CORRIDOR|PASSAGE|WALKWAY|CIRCULATION|COMMON|OFFICE\s*SPACE|OFFICE\s*AREA|OPEN\s*OFFICE|SERVICE\s*AREA")), re.I)
OWNER_LOBBY_FAMILIES = ("Lift Lobby", "Service Lobby")
OWNER_CIRCULATION_FAMILIES = ("Circulation",)
# a room left in a doorway once the opening is cut out of it: dropped below this
DOORWAY_ROOM_KEEP_SQM = float(_WO.get("doorway_room_keep_sqm", 0.05))
# the least of a room in a door opening that is cut back (mm2): a touch is not
DOORWAY_CUT_MIN_MM2 = float(_WO.get("doorway_cut_min_mm2", 1000.0))
DOOR_SWING_HINGE_MM = 150.0


def door_room_rank(label="", family="", shaft=None, lift=None):
    """The rank of a space a door opens into (see above): 5 a lift or shaft,
    4 a room, 3 a staircase, 2 a lobby, 1 a corridor or open floor, 0 a
    void. shaft / lift: what the caller already knows (is_shaft_room /
    is_lift_room), else read from the label and family."""
    n = " ".join(str(label or "").split())
    fam = str(family or "")
    if fam == "Void":
        return OWNER_NONE
    if lift is None:
        lift = is_lift_room(n, fam)
    if shaft is None:
        shaft = is_shaft_room(n, fam)
    if lift or shaft:
        return OWNER_SHAFT
    if fam == "Staircase" or (n and OWNER_STAIR_RE.search(n)):
        return OWNER_STAIR
    if fam in OWNER_LOBBY_FAMILIES or (n and OWNER_LOBBY_RE.search(n)):
        return OWNER_LOBBY
    if fam in OWNER_CIRCULATION_FAMILIES or (n and OWNER_CIRCULATION_RE.search(n)):
        return OWNER_CIRCULATION
    return OWNER_ROOM


def swing_side(quad, swings=()):
    """The side of a door opening its door is drawn on: 0 (the + face,
    door_faces()[0]) or 1, by the swings hinged at its jambs - an arc or
    ellipse of the leaf's radius (0.3-1.3 x the width) with its hinge within
    DOOR_SWING_HINGE_MM of the opening, whose open end stands beyond a face.
    None with no swing, or swings both ways in equal number. swings:
    dxf_reader._collect_door_arcs [(hx, hy, r, (ax, ay), (bx, by), sweep)], mm."""
    ax = door_axes(quad)
    if ax is None or not swings:
        return None
    from shapely.geometry import Polygon, Point
    cx, cy, ux, uy, nx, ny, W, D = ax
    try:
        Q = Polygon([(float(p[0]), float(p[1])) for p in quad]).buffer(0)
    except (ValueError, TypeError, IndexError):
        return None
    votes = collections.Counter()
    for sw in swings:
        try:
            hx, hy, r, a_, b_ = float(sw[0]), float(sw[1]), float(sw[2]), sw[3], sw[4]
        except (TypeError, ValueError, IndexError):
            continue
        if abs(hx - cx) > W + 2 * DOOR_SWING_HINGE_MM + r or abs(hy - cy) > W + 2 * DOOR_SWING_HINGE_MM + r:
            continue
        if not (0.3 * W <= r <= 1.3 * W) or Point(hx, hy).distance(Q) > DOOR_SWING_HINGE_MM:
            continue
        ts = [(float(p[0]) - cx) * nx + (float(p[1]) - cy) * ny for p in (a_, b_)]
        far = max(ts, key=abs)
        if abs(far) > D / 2.0 + 0.25 * r:
            votes[0 if far > 0 else 1] += 1
    if not votes:
        return None
    top = votes.most_common(2)
    if len(top) > 1 and top[0][1] == top[1][1]:
        return None
    return top[0][0]


def door_owners(quads, rooms, ranks, swings=(), sides=None, tree=None):
    """[(side, owner, how)] for each door opening: side 0 (its + face,
    door_faces()[0]) or 1 - the face of the room the door belongs to, where
    its line is drawn - or None where that is not settled; owner the index
    of that room in rooms (None: the common floor); how what settled it
    ("rank", "only door", "swing", "fewer doors"; "same room both sides",
    "no room either side", "undecided").

    rooms: shapely polygons; ranks: door_room_rank for each; swings: as for
    swing_side; sides: door_sides for each quad when already known."""
    rooms = list(rooms or ())
    quads = list(quads or ())
    if sides is None:
        if rooms:
            from shapely.strtree import STRtree
            tree = tree if tree is not None else STRtree(rooms)
            sides = [door_sides(q, rooms, tree) for q in quads]
        else:
            sides = [(None, None)] * len(quads)
    doors_of = collections.Counter()
    for a, b in sides:
        for x in {a, b}:
            if x is not None:
                doors_of[x] += 1
    out = []
    for q, (a, b) in zip(quads, sides):
        if a is not None and a == b:
            out.append((None, a, "same room both sides"))
            continue
        if a is None and b is None:
            out.append((None, None, "no room either side"))
            continue
        ra = ranks[a] if a is not None else OWNER_NONE
        rb = ranks[b] if b is not None else OWNER_NONE
        if ra != rb:
            out.append((0, a, "rank") if ra > rb else (1, b, "rank"))
            continue
        ca, cb = doors_of[a], doors_of[b]
        if (ca == 1) != (cb == 1):
            out.append((0, a, "only door") if ca == 1 else (1, b, "only door"))
            continue
        sw = swing_side(q, swings)
        if sw is not None:
            out.append((sw, a if sw == 0 else b, "swing"))
            continue
        if ca != cb:
            out.append((0, a, "fewer doors") if ca < cb else (1, b, "fewer doors"))
            continue
        out.append((None, None, "undecided"))
    return out


# -- Doors: the line on the room's inner wall trace (2026-10-17, later) -------
# A door's line is the room's inner wall trace across the opening, read from
# the walls on either side of it (the jambs) - not from the opening found,
# which is often only the door frame (100 mm in a 200 mm wall: XA,
# Centaurus) or drawn behind a shaft's front (P285 TEN / COMM / FAPA):
#   * at each jamb, the end of the wall there and its inner edge - the corner
#     on the room's side; the line joins the two: across the opening, from
#     its middle into the room, the free width between the jambs' walls is
#     read until it first opens wider than the opening (a jamb's wall has
#     ended and the room begins) - the room's face of the wall the door is
#     set in, or of the nibs its frame stands between;
#   * side walls that do not run on past the opening on either side (a shaft
#     whose whole front is its door): the jambs' walls run on into the room,
#     so the line is at their ends, where the frame stands - the room runs to
#     it;
#   * the two jambs' walls ending apart (more than DOOR_TRACE_JOG_MM): at the
#     nearer end, square to the opening, flagged;
#   * a wall drawn over the opening or straight behind it, or no wall at a
#     jamb: no trace - the line stays on the face door_owners gave it.
DOOR_TRACE = bool(_WO.get("door_trace", True))
DOOR_TRACE_REACH_MM = float(_WO.get("door_trace_reach_mm", 400.0))     # past the opening's face, each way
DOOR_TRACE_WIDEN_MM = float(_WO.get("door_trace_widen_mm", 30.0))      # wider than the opening by this: a wall ended
DOOR_TRACE_ALONG_MM = float(_WO.get("door_trace_along_mm", 1500.0))    # how far along the wall a jamb is looked for
DOOR_TRACE_STEP_MM = float(_WO.get("door_trace_step_mm", 2.0))
DOOR_TRACE_BEHIND_MM = float(_WO.get("door_trace_behind_mm", 100.0))   # a wall this close behind: drawn over the room
DOOR_TRACE_JOG_MM = float(_WO.get("door_trace_jog_mm", 20.0))


def door_trace(quad, side, solid):
    """The room's inner wall trace across a door opening: (t, how, jog) - t
    the line's offset from the opening's middle along its + normal
    (door_axes; the line is trace_line(quad, t)), how what it is ("room
    face": the room's side of the jambs' walls; "wall ends": the side walls
    run on into the room and the line is at their ends), jog how far apart
    the two jambs' walls end (None if one runs out of reach) - or (None,
    reason, None): "wall over the opening", "wall behind the opening", "no
    wall at a jamb", "no end in reach".

    side: the room's face as door_owners gives it (0: +, 1: -); solid: the
    walls, columns and what is set in the wall line (shapely, mm; see
    trace_solid)."""
    import math as _m
    from shapely.geometry import Point, LineString, box
    from shapely import affinity
    ax = door_axes(quad)
    if ax is None or side not in (0, 1) or solid is None or solid.is_empty:
        return None, "no wall at a jamb", None
    cx, cy, ux, uy, nx, ny, W, D = ax
    sg = 1.0 if side == 0 else -1.0
    reach = D / 2.0 + DOOR_TRACE_REACH_MM
    R = W / 2.0 + DOOR_TRACE_ALONG_MM + reach
    M = solid.intersection(box(cx - R, cy - R, cx + R, cy + R))
    if M.is_empty:
        return None, "no wall at a jamb", None
    M = affinity.rotate(affinity.translate(M, -cx, -cy), -_m.atan2(uy, ux), origin=(0, 0), use_radians=True)

    def jamb(t, j):
        s0 = W / 2.0 - 10.0
        hit = M.intersection(LineString([(j * s0, t), (j * (W / 2.0 + DOOR_TRACE_ALONG_MM), t)]))
        if hit.is_empty:
            return None
        xs = []
        for g in getattr(hit, "geoms", [hit]):
            if g.geom_type == "Point":
                xs.append(abs(g.x))
            elif g.geom_type == "LineString":
                xs += [abs(c[0]) for c in g.coords]
        return min(xs) if xs else None

    def free(t):
        a, b = jamb(t, 1.0), jamb(t, -1.0)
        return None if a is None or b is None else a + b

    if M.contains(Point(0.0, 0.0)):
        return None, "wall over the opening", None
    w0 = free(0.0)
    if w0 is None:
        return None, "no wall at a jamb", None

    def widened(t):
        f = free(t)
        return f is None or f > w0 + DOOR_TRACE_WIDEN_MM

    def scan(d):
        """(t where the free width first opens wider, going d (+1 / -1) from
        the middle) or (None, 'blocked at t') / (None, None)."""
        t, step = 0.0, DOOR_TRACE_STEP_MM
        while t <= reach:
            if t > 0 and M.contains(Point(0.0, d * t)):
                return None, t
            if widened(d * t):
                lo, hi = max(0.0, t - step), t          # refine to half a millimetre
                while hi - lo > 0.5:
                    mid = (lo + hi) / 2.0
                    if widened(d * mid):
                        hi = mid
                    else:
                        lo = mid
                return d * lo, None
            t += step
        return None, None

    t, blocked = scan(sg)
    how = "room face"
    if t is None:
        if blocked is not None and blocked <= D / 2.0 + DOOR_TRACE_BEHIND_MM:
            return None, "wall behind the opening", None
        t, _b = scan(-sg)
        how = "wall ends"
        if t is None:
            return None, "no end in reach", None
    # where each jamb's wall ends, going the same way (do they coincide?)
    d = 1.0 if t >= 0 else -1.0
    ends = []
    for j in (1.0, -1.0):
        e, tt = None, 0.0
        while tt <= reach:
            a = jamb(d * tt, j)
            if a is None or a > W / 2.0 + DOOR_TRACE_WIDEN_MM:
                e = d * tt
                break
            tt += DOOR_TRACE_STEP_MM
        ends.append(e)
    jog = None if None in ends else abs(ends[0] - ends[1])
    return t, how, jog


def trace_line(quad, t):
    """The line across a door opening at offset t from its middle (along
    door_axes' + normal): ((x0, y0), (x1, y1)), or None."""
    ax = door_axes(quad)
    if ax is None or t is None:
        return None
    cx, cy, ux, uy, nx, ny, W, D = ax
    mx, my = cx + nx * t, cy + ny * t
    return ((mx - ux * W / 2.0, my - uy * W / 2.0), (mx + ux * W / 2.0, my + uy * W / 2.0))


def trace_solid(walls=(), sets=(), quads=()):
    """What a door's jambs are read from (door_trace): the walls and columns
    (shapely), and what is set in the wall line - cut-outs, windows, glass -
    less the door openings themselves (quads)."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    ws = [g for g in walls or () if g is not None and not g.is_empty]
    ss = [g for g in sets or () if g is not None and not g.is_empty]
    if ss and quads:
        qs = []
        for q in quads:
            try:
                g = Polygon([(float(p[0]), float(p[1])) for p in q]).buffer(5.0)
            except (ValueError, TypeError, IndexError):
                continue
            if not g.is_empty:
                qs.append(g)
        if qs:
            Q = unary_union(qs)
            ss = [g.difference(Q) for g in ss]
    gs = [g for g in ws + ss if g is not None and not g.is_empty]
    return unary_union(gs).buffer(0) if gs else None


def door_traces(quads, sides, solid):
    """door_trace for each door opening with its room's side settled (sides:
    0 / 1 / None each): [(t, how, jog)] - (None, None, None) where the side
    is not."""
    out = []
    for q, sd in zip(quads or (), sides or ()):
        if sd is None or not DOOR_TRACE:
            out.append((None, None, None))
            continue
        ax = door_axes(q)
        if ax is None or ax[7] <= DOOR_CUBICLE_BOARD_MAX_MM:
            out.append((None, None, None))               # a WC cubicle's board door
            continue
        out.append(door_trace(q, sd, solid))
    return out


def rooms_to_door_lines(rooms, quads, doors, skip=None):
    """Every room squared up to the door lines on the room's inner wall
    trace: the room a door belongs to runs to its line across the opening
    (taken out to it where the room stopped short - a shaft between side
    walls, traced to the opening's back face - and cut back to it where the
    room ran on into the doorway); no other room crosses the line into the
    door's room. rooms: shapely polygons (None kept); doors: for each quad
    (owner index into rooms or None, side 0 / 1, t) or None; skip: a flag
    per room to leave it (an X of the X sequence). Returns ([new geometry,
    the same object when unchanged, or None], grown sq.m, cut sq.m)."""
    from shapely.geometry import Polygon
    out = list(rooms or ())
    grown = cut = 0.0
    for q, d in zip(quads or (), doors or ()):
        if d is None:
            continue
        owner, sd, t = d
        ax = door_axes(q)
        if ax is None or t is None or sd not in (0, 1) or owner is None:
            continue                                      # no room settled for it: no room squared
        cx, cy, ux, uy, nx, ny, W, D = ax
        sg = 1.0 if sd == 0 else -1.0

        def strip(t0, t1, pad=0.0):
            a, b = min(t0, t1), max(t0, t1)
            pts = []
            for s_, tt in ((-W / 2.0 - pad, a), (W / 2.0 + pad, a), (W / 2.0 + pad, b), (-W / 2.0 - pad, b)):
                pts.append((cx + ux * s_ + nx * tt, cy + uy * s_ + ny * tt))
            return Polygon(pts)
        beyond = strip(t, -sg * (D / 2.0 + 600.0), 2.0)     # the opening, past the line, away from the room
        # the opening on the room's side of the line, and the door's room
        # there: no other room in it
        inside = strip(t, sg * max(D / 2.0, sg * t) + sg * 5.0, 2.0)
        og = out[owner] if owner is not None and 0 <= owner < len(out) else None
        if og is not None and not og.is_empty:
            inside = inside.union(strip(t, sg * (D / 2.0 + 600.0), 2.0).intersection(og.buffer(1.0)))
        for k, g in enumerate(out):
            if g is None or g.is_empty or (skip is not None and k < len(skip) and skip[k]):
                continue
            if k == owner:
                g2 = g
                if g2.intersection(beyond).area > 1.0:
                    g2 = g2.difference(beyond).buffer(0)       # ran on into the doorway: back to the line
                face = sg * D / 2.0                              # the opening's face on the room's side
                if sg * (face - t) > 1.0 and g2.distance(strip(t, face)) <= 5.0:
                    # stopped short (at the opening's back face): out to the line
                    g2 = g2.union(strip(t, face + sg * 2.0)).buffer(0)
                if g2 is not g:
                    parts = [p for p in getattr(g2, "geoms", [g2]) if p.geom_type == "Polygon" and not p.is_empty]
                    if parts:
                        best = max(parts, key=lambda p: p.area)
                        if best.area > g.area:
                            grown += best.area - g.area
                        else:
                            cut += g.area - best.area
                        out[k] = best
            else:
                if g.intersection(inside).area > 1.0 and not (g.contains(inside.centroid)):
                    g2 = g.difference(inside).buffer(0)
                    parts = [p for p in getattr(g2, "geoms", [g2]) if p.geom_type == "Polygon" and not p.is_empty]
                    if parts:
                        best = max(parts, key=lambda p: p.area)
                        cut += g.area - best.area
                        out[k] = best
    return out, grown / 1e6, cut / 1e6


def owner_face(quad, side):
    """The line of a door opening on the face `side` (0 or 1, as
    door_owners gives it): ((x0, y0), (x1, y1)), or None."""
    faces = door_faces(quad)
    if faces is None or side not in (0, 1):
        return None
    return faces[side]


def rooms_out_of_doorways(rooms, quads, skip=None, board_max=None, min_keep_sqm=None):
    """Every room cut back at the door openings it reaches into: a room
    stops at its own face of the wall, the opening is the door's (BHUB
    FP-SH-02's room grown into its doorway, the 10MM DROP IN FINISH
    thresholds; XA's FIRE LIFT LOBBY in a shaft's doorway). Not a door in a
    board (a WC cubicle's), and not a room standing on both sides of the
    door (traced through its own door). What is left of a room is its
    largest piece; a room with less than min_keep_sqm left was the doorway
    itself and is dropped.

    rooms: shapely polygons (None kept as None); skip: a flag per room to
    leave it as it is (an X of the X sequence). Returns ([new geometry, the
    same object when unchanged, or None when dropped], cut sq.m)."""
    from shapely.geometry import Polygon
    from shapely.strtree import STRtree
    from shapely.ops import unary_union
    board_max = DOOR_CUBICLE_BOARD_MAX_MM if board_max is None else board_max
    min_keep = (DOORWAY_ROOM_KEEP_SQM if min_keep_sqm is None else min_keep_sqm) * 1e6
    rooms = list(rooms or ())
    out = list(rooms)
    Qs, keep_q = [], []
    for q in quads or ():
        ax = door_axes(q)
        if ax is None or ax[7] <= board_max:
            continue
        try:
            g = Polygon([(float(p[0]), float(p[1])) for p in q]).buffer(0)
        except (ValueError, TypeError, IndexError):
            continue
        if g.is_empty:
            continue
        Qs.append(g)
        keep_q.append(q)
    live = [k for k, r in enumerate(rooms) if r is not None and not r.is_empty]
    if not Qs or not live:
        return out, 0.0
    rtree = STRtree([rooms[k] for k in live])
    sides = [door_sides(q, [rooms[k] for k in live], rtree) for q in keep_q]
    qtree = STRtree(Qs)
    cut_area = 0.0
    for li, k in enumerate(live):
        if skip is not None and k < len(skip) and skip[k]:
            continue
        g = rooms[k]
        hit = []
        for m in qtree.query(g):
            m = int(m)
            a = g.intersection(Qs[m]).area
            if a < DOORWAY_CUT_MIN_MM2 or a < 0.02 * Qs[m].area:
                continue
            if sides[m][0] == li and sides[m][1] == li:
                continue                              # the room on both sides: traced through its door
            hit.append(Qs[m])
        if not hit:
            continue
        g2 = g.difference(unary_union(hit)).buffer(0)
        parts = [p for p in getattr(g2, "geoms", [g2]) if p.geom_type == "Polygon" and not p.is_empty]
        if not parts or max(p.area for p in parts) < min_keep:
            out[k] = None
            cut_area += g.area
            continue
        best = max(parts, key=lambda p: p.area)
        cut_area += g.area - best.area
        out[k] = best
    return out, cut_area / 1e6


# -- Cut-outs in walls --------------------------------------------------------
# A cut-out (an X-marked slab opening) is part of the wall only when it lies
# in a wall run: the wall goes on past both of its ends in line with it, or
# stands on both of its long sides. One lining the face of a wall inside a
# shaft - the PHE shaft's two strips, FHC-3's (P285) - is the shaft's
# opening: the wall stays at the wall's face.
CUTOUT_IN_WALL_PROBE_MM = float(_WO.get("cutout_in_wall_probe_mm", 60.0))


def cutout_in_wall_run(cut, walls):
    """Whether a cut-out (shapely) lies in a wall run of `walls` (shapely,
    the walls without the openings)."""
    from shapely.geometry import Point, LineString
    if cut is None or cut.is_empty or walls is None or walls.is_empty:
        return False
    r = cut.minimum_rotated_rectangle
    if r.geom_type != "Polygon":
        return False
    cs = list(r.exterior.coords)[:4]
    e0 = (cs[1][0] - cs[0][0], cs[1][1] - cs[0][1])
    e1 = (cs[2][0] - cs[1][0], cs[2][1] - cs[1][1])
    l0, l1 = math.hypot(*e0), math.hypot(*e1)
    if l0 <= 0 or l1 <= 0:
        return False
    if l0 >= l1:
        L, W, ux, uy = l0, l1, e0[0] / l0, e0[1] / l0
    else:
        L, W, ux, uy = l1, l0, e1[0] / l1, e1[1] / l1
    nx, ny = -uy, ux
    c = r.centroid
    p = CUTOUT_IN_WALL_PROBE_MM

    def wall_at(x, y):
        return walls.contains(Point(x, y))
    # on both long sides: inside a thick wall
    if wall_at(c.x + nx * (W / 2 + p), c.y + ny * (W / 2 + p)) and \
            wall_at(c.x - nx * (W / 2 + p), c.y - ny * (W / 2 + p)):
        return True
    # in line with the wall at both ends, the wall there no wider than the cut-out
    for sg in (1.0, -1.0):
        ex, ey = c.x + sg * ux * (L / 2 + p), c.y + sg * uy * (L / 2 + p)
        run = LineString([(ex, ey), (ex + sg * ux * max(W, 150.0), ey + sg * uy * max(W, 150.0))])
        if walls.intersection(run).length < 0.8 * run.length:
            return False
        bx, by = ex + sg * ux * max(W, 150.0) / 2.0, ey + sg * uy * max(W, 150.0) / 2.0
        if wall_at(bx + nx * (W / 2 + p), by + ny * (W / 2 + p)) and wall_at(bx - nx * (W / 2 + p), by - ny * (W / 2 + p)):
            return False                              # a cross wall at that end: the cut-out lines its face
    return True

