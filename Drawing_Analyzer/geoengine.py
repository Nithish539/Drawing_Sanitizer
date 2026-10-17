"""Geometry engine: architectural elements recognised from shape alone.

A second, independent pair of eyes next to the text engine (dxf_reader's
annotate_dxf). It never reads the drawing's room texts: nothing in this
module receives them, so a classification here can only come from the
linework - shape, size, X marks, tread lines, symbols, neighbours and
repetition. Layer and block names are used only as supporting metadata,
and every such use is marked as metadata in the evidence.

    run(dxf_path, floors, out_dxf=None) -> result dict

Pipeline, per floor:

    candidates (geomextract, with no texts): X marks, tread runs, fixture
        clusters, closed outlines, wall-enclosed spaces
      -> feature profile (size, aspect, rectangularity, X, tread lines,
         symbols, doors, neighbours)
      -> geometry families (same kind and size) and repetition across floors
      -> classification by evidence rules
      -> boundary (the enclosing geometry, not the evidence's bounding box)
      -> generated label (SHAFT_01, LIFT_03, ...)

The ranges every rule uses, and how often each rule agreed with what was
really there on the drawings it was measured on, live in
geo_profiles.json (written by geolearn.py). Confidence is that measured
agreement - a lower bound, see confidence() - never a made-up number.

What it does not find (2026-10-16, final): a shaft, a lift, a window, a
cut-out, a column or a void. Those six are Step 3's - every X mark decided
once by the X sequence (xplace: 2 shaft, 3 lift, 4 window, 5 cut-out,
6 column, 7 void), the rest by the Step 3 pass - and Step 5 and the final
drawing take them from there (STEP3_CLASSES). This engine reads an X mark
or a lift-size square only as evidence for what it does report: the place
of an X is no room, lift wells along both sides make a lift lobby (or a
machine room), flights either side of a long X make a staircase. Its walls
are cut round the shapes of the one column finder Step 3 reads too.
"""
from __future__ import annotations

import collections
import json
import math
import os
import re

import numpy as np
import shapely
from shapely.geometry import Polygon, box as sbox

from . import dxf_reader as dr
from . import geomextract as gx
from . import xregions

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_PATH = os.path.join(HERE, "geo_profiles.json")

# Output classes, in the order they are listed and drawn.
CLASSES = ["LIFT_LOBBY", "MACHINE_ROOM", "SERVICE_LOBBY", "STAIRCASE", "TOILET", "ROOM", "UNKNOWN_GEOMETRY"]
# The six Step 3 decides (the X sequence for every X mark): never a finding
# of this engine, and a finding of one of them in a result written before
# 2026-10-16 is dropped where a result is read (step3_free).
STEP3_CLASSES = ("COLUMN", "WINDOW", "CUTOUT", "SHAFT", "LIFT", "VOID", "STAIR_WELL")
STEP3_FAMILIES = ("Shaft", "Lift", "Window", "Cutout", "Column", "Void")
# What the stages below work out only as evidence for the classes reported
# (never reported): an X mark, a lift-size square in a bank of them.
EVIDENCE_CLASSES = ("X_MARK", "LIFT_WELL")
LABEL_PREFIX = {"UNKNOWN_GEOMETRY": "UNKNOWN_GEO", "LIFT_LOBBY": "LIFT_LOBBY",
                "MACHINE_ROOM": "MACHINE_ROOM", "SERVICE_LOBBY": "SERVICE_LOBBY"}
# Drawn on the text engine's family layers (AI_STAIR, AI_LIFTLOBBY ...); an
# unnamed shape on a layer of its own.
LAYER_OF = {c: dr.LN.geo_class_layer(c) for c in CLASSES}

# The order the geometry is identified in, from config.json
# ("geometry_sequence"). Columns first (the column shapes, as obstacles),
# then the X marks (as evidence); the rest in any order for now. Each stage
# skips a shape an earlier stage has taken.
DEFAULT_SEQUENCE = ["columns", "x_marks", "staircases", "toilets", "lift_wells", "lift_lobbies",
                    "service_lobbies", "rooms"]


def _load_sequence():
    try:
        from .config import CONFIG
        seq = [str(k).strip().lower() for k in (CONFIG.get("geometry_sequence") or [])]
    except Exception:                                 # noqa: BLE001
        seq = []
    # (an older sequence's "shafts" is the X marks' stage; "windows" is gone)
    seq = ["x_marks" if k == "shafts" else k for k in seq]
    return [k for k in seq if k in DEFAULT_SEQUENCE] or list(DEFAULT_SEQUENCE)


GEOMETRY_SEQUENCE = _load_sequence()


def _geo_setting(key, default):
    """A number from config.json "geometry_engine" (see its _notes)."""
    try:
        from .config import CONFIG
        return float((CONFIG.get("geometry_engine") or {}).get(key, default))
    except Exception:                                 # noqa: BLE001
        return float(default)


# The guardrail on a staircase: lines this far inside its outline (treads,
# flights, handrails) do not cut it, and a cut leaving less than this share
# of it is not made.
STAIR_GUARD_INSET_MM = _geo_setting("stair_guard_inset_mm", 100.0)
STAIR_GUARD_MIN_KEEP = _geo_setting("stair_guard_min_keep_share", 0.5)

# Layers a column is drawn on. A closed shape or circle on one of them,
# column-sized, is a column.
COLUMN_LAYER_WORDS = ("COLUMN", "COL-", "-COL", "_COL", "COL_", "PILLAR", "STR-COL", "S-COLS")
# The element pipeline's lock before the wall trace (rule element_pipeline):
# None reads the rule's shipped setting; True / False for a test or an A/B.
LOCK_WALLS = None
COLUMN_SQM = (0.04, 4.0)
COLUMN_ASPECT_MAX = 4.0
# A floor's columns: at least this many in the group, and this share of
# them on a shared grid line both ways.
COLUMN_GRID_MIN = 6
COLUMN_GRID_ALIGNED = 0.6
COLUMN_GRID_DENSITY = 0.25        # members / grid crossings
COLUMN_INNER_MM = 1500.0          # this far in from the plate edge ...
COLUMN_INNER_SHARE = 0.2          # ... stands at least this share of them
COLUMN_HOLLOW_RATIO = _geo_setting("column_hollow_ratio", 1.5)   # hull / filled area: a C or L wall
# A concrete wall drawn on the column layer (rule rcc_walls_on_column_layer):
# no thicker than this on average (2 x area / perimeter) and at least this
# long (half its perimeter) - too big or too long for a column.
RCC_WALL_MAX_MM = _geo_setting("rcc_wall_max_mm", 600.0)
# An X-marked slot at the back of a walled shaft cell - a door in front of
# its long side within SHAFT_CELL_DEPTH_MM, across this share of its length,
# and walls closing the cell at both ends (within SHAFT_CELL_GAP_MM + 300 mm
# of the slot's ends) - is a shaft, not a cut-out in a wall (P285: FHC
# behind its cabinet, was CUTOUT_08).
SHAFT_CELL_DEPTH_MM = _geo_setting("shaft_cell_depth_mm", 1200.0)
SHAFT_CELL_GAP_MM = _geo_setting("shaft_cell_gap_mm", 200.0)
SHAFT_CELL_ALONG_SHARE = _geo_setting("shaft_cell_along_share", 0.4)
RCC_WALL_MIN_LEN_MM = _geo_setting("rcc_wall_min_len_mm", 1500.0)
# A piece no thicker than this lying in a wall line - wall-layer faces along
# both its long sides over this share of them - is the wall, not a column
# (the 755 x 200 jambs of the P285 lift openings were cut out of the walls).
COLUMN_IN_WALL_MAX_MM = _geo_setting("column_in_wall_max_mm", 250.0)
COLUMN_IN_WALL_FACE_SHARE = _geo_setting("column_in_wall_face_share", 0.5)
# Never columns: furniture, fixtures, text, tags, landscape, the area-plan
# overlay, door and window blocks, hatching of finishes.
_NOT_COLUMN_LAYER = re.compile(r"FURN|FIX|SANIT|TEXT|ANNO|TAG|DIM|LAND|PLANT|TREE|CAR|PARK|LIGHT|"
                               r"DOOR|WIND|FINI|TILE|FLOOR|SOLAR|PV|PANEL|^\d+(\.\d+)+\s|SYMB|GRID|NORTH|LEGEND")
# AutoCAD colour index per class, for the annotated DXF.
ACI = {"LIFT_LOBBY": 150, "MACHINE_ROOM": 32, "SERVICE_LOBBY": 112, "STAIRCASE": 3,
       "TOILET": 4, "ROOM": 51, "UNKNOWN_GEOMETRY": 9}
RGB = {"LIFT_LOBBY": "#4dabf7", "MACHINE_ROOM": "#e8590c", "SERVICE_LOBBY": "#66a80f", "STAIRCASE": "#2f9e44",
       "TOILET": "#15aabf", "ROOM": "#fab005", "UNKNOWN_GEOMETRY": "#ae3ec9"}

_NOT_FIXTURE = re.compile(r"WHEEL|CHAIR|CAR|BED|SOFA|TABLE|DESK")


def x_any_layer(layer):
    """An X mark is found by its shape on any layer; only layers that are
    plainly not building elements (columns, symbols, grids, hatches) are
    left out. The layer is reported as metadata, it does not decide."""
    lu = (layer or "").upper()
    return not any(w in lu for w in xregions.NOT_X_LAYER_WORDS)


def _layer_note(layers):
    lu = " ".join(layers).upper()
    if "CUT" in lu:
        return "drawn on a cut-out layer"
    if "HIDDEN" in lu or "HIDN" in lu:
        return "drawn in hidden lines (above / below this level)"
    return ""


# --------------------------------------------------------------- profile ---
def load_profile(path=None):
    with open(path or PROFILE_PATH, encoding="utf-8") as f:
        return json.load(f)


def wilson_lower(ok, n, z=1.96):
    """Lower end of the 95% Wilson interval for a rate of ok in n: how sure
    a rule seen right ok times out of n can be said to be."""
    if n <= 0:
        return None
    p = ok / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - r) / d)


def confidence(profile, rule):
    """(value 0-1 or None, explanation) for a rule, from geo_profiles.json.

    agree / n: how often the rule's finding matched the text engine at the
    same place on the measured drawings. Where disagreements were checked
    by eye ("verified": [right, checked]), that share of the disagreements
    counts as right too - the text engine is a reference, not the truth.
    The value is the 95% lower bound of the resulting rate, so a rule seen
    6 times out of 6 is not reported as certain."""
    st = (profile.get("rules") or {}).get(rule)
    if not st or not st.get("n"):
        return None, "not measured yet"
    agree = st.get("agree", 0)
    conflict = st.get("conflict", st["n"] - agree)
    unref = st.get("unreferenced", 0)
    ver = st.get("verified")
    if ver and ver[1]:
        # findings with no text boundary to compare with (or a text that
        # could not name them) were sampled and checked by eye: that rate
        # stands for all of them
        rate = ver[0] / float(ver[1])
        n = agree + conflict + unref
        ok = agree + unref * rate
        note = ("agreed with the text engine {0} of {1} times where it named the place; "
                "of {2} the text engine had not named, {3} of {4} checked by eye were right").format(
                    agree, agree + conflict, unref, ver[0], ver[1])
    else:
        n = agree + conflict
        ok = float(agree)
        note = "agreed with the text engine {0} of {1} times where it named the place".format(agree, n)
        if unref:
            note += " ({0} more with no text to compare)".format(unref)
        if not n:
            return None, "never met a named text boundary yet ({0} unreferenced)".format(unref)
    if st.get("drawings"):
        note += " (on {0} drawing{1})".format(st["drawings"], "" if st["drawings"] == 1 else "s")
    return wilson_lower(ok, n), note


# ------------------------------------------------------------- geometry ---
def _poly(pts):
    try:
        pg = Polygon(pts)
        if not pg.is_valid:
            pg = pg.buffer(0)
        if pg.geom_type == "MultiPolygon":
            pg = max(pg.geoms, key=lambda g: g.area)
        return pg if not pg.is_empty and pg.area > 0 else None
    except Exception:                                 # noqa: BLE001
        return None


def _shape(pg):
    """Width/height of the minimum rotated rectangle, and how rectangular."""
    mrr = pg.minimum_rotated_rectangle
    xs, ys = mrr.exterior.coords.xy
    e = [math.hypot(xs[i + 1] - xs[i], ys[i + 1] - ys[i]) for i in range(4)]
    w, h = min(e[0], e[1]), max(e[0], e[1])
    return {"width_mm": round(w), "height_mm": round(h),
            "aspect": round(h / w, 2) if w else 0.0,
            "area_sqm": round(pg.area / 1e6, 2),
            "perimeter_m": round(pg.length / 1000.0, 2),
            "rectangularity": round(pg.area / mrr.area, 2) if mrr.area else 0.0,
            "vertices": len(pg.exterior.coords) - 1,
            "centroid": [round(pg.centroid.x), round(pg.centroid.y)],
            "bbox": [round(v) for v in pg.bounds]}


def _wavy(pg):
    """A revision cloud: a closed outline made of a chain of small arcs, so
    its perimeter is much longer than the same outline simplified."""
    nv = len(pg.exterior.coords) - 1
    if nv < 40:
        return False
    simple = pg.simplify(200.0)
    return simple.length > 0 and pg.length / simple.length > 1.12


def _tread_side(seg, region, along_vertical):
    """Stair treads in `region` (x0,y0,x1,y1): at least six short lines across
    the flight, evenly spaced. along_vertical: the flight runs along y, so
    the treads are horizontal."""
    if not len(seg):
        return 0, None
    x0, y0, x1, y1 = region
    mx, my = (seg[:, 0] + seg[:, 2]) / 2, (seg[:, 1] + seg[:, 3]) / 2
    m = (mx >= x0) & (mx <= x1) & (my >= y0) & (my <= y1)
    s = seg[m]
    if len(s) < 4:
        return 0, None
    dx, dy = np.abs(s[:, 2] - s[:, 0]), np.abs(s[:, 3] - s[:, 1])
    ln = np.hypot(dx, dy)
    across = (dy <= 0.18 * dx) if along_vertical else (dx <= 0.18 * dy)
    keep = across & (ln >= 500) & (ln <= 2600)
    if keep.sum() < 4:
        return int(keep.sum()), None
    pos = np.sort(my[m][keep] if along_vertical else mx[m][keep])
    uniq = [pos[0]]
    for v in pos[1:]:
        if v - uniq[-1] > 30:
            uniq.append(v)
    gaps = np.diff(uniq)
    return len(uniq), (float(np.median(gaps)) if len(gaps) else None), gaps


def _treads(seg, region, along_vertical, rng, min_lines):
    """(lines, median spacing, regular share) for one flight region."""
    r = _tread_side(seg, region, along_vertical)
    if len(r) < 3:
        return 0, None, 0.0                  # too few lines for a flight
    n, med, gaps = r
    if not med:
        return n, None, 0.0
    reg = float(np.mean(np.abs(gaps - med) <= 0.2 * med)) if len(gaps) else 0.0
    ok = n >= min_lines and rng[0] <= med <= rng[1] and reg >= 0.5
    return (n if ok else 0), med, reg


def _flights(seg, pg, R):
    """Stair flights inside a space: runs of tread lines - short lines of
    about the same length, parallel, side by side at an even spacing.

    Lines are grouped by the span they cover across the flight, so the two
    flights of a dog-leg stair (side by side, treads parallel) come out as
    two runs. Returns (number of flights, [(lines, median spacing mm), ...])."""
    if not len(seg):
        return 0, []
    b = pg.bounds
    mx, my = (seg[:, 0] + seg[:, 2]) / 2, (seg[:, 1] + seg[:, 3]) / 2
    m = (mx >= b[0]) & (mx <= b[2]) & (my >= b[1]) & (my <= b[3])
    s = seg[m]
    if len(s) < 6:
        return 0, []
    s = s[shapely.contains_xy(pg, (s[:, 0] + s[:, 2]) / 2, (s[:, 1] + s[:, 3]) / 2)]
    lo_len, hi_len = R["flight_width_mm"][0] * 0.7, R["flight_width_mm"][1] * 1.2
    sp_lo, sp_hi = R["tread_spacing_mm"]
    best = (0, [])
    for horizontal in (True, False):
        dx, dy = np.abs(s[:, 2] - s[:, 0]), np.abs(s[:, 3] - s[:, 1])
        if horizontal:
            keep = (dy <= 0.1 * dx) & (dx >= lo_len) & (dx <= hi_len)
            pos = (s[keep, 1] + s[keep, 3]) / 2
            a = np.minimum(s[keep, 0], s[keep, 2])
            z = np.maximum(s[keep, 0], s[keep, 2])
        else:
            keep = (dx <= 0.1 * dy) & (dy >= lo_len) & (dy <= hi_len)
            pos = (s[keep, 0] + s[keep, 2]) / 2
            a = np.minimum(s[keep, 1], s[keep, 3])
            z = np.maximum(s[keep, 1], s[keep, 3])
        if keep.sum() < 6:
            continue
        groups = []                                   # [lo, hi, [positions]]
        for p, lo, hi in sorted(zip(pos, a, z), key=lambda t: t[1]):
            for g in groups:
                ov = min(hi, g[1]) - max(lo, g[0])
                if ov >= 0.6 * min(hi - lo, g[1] - g[0]):
                    g[2].append(p)
                    break
            else:
                groups.append([lo, hi, [p]])
        flights = []
        for lo, hi, ps in groups:
            ps = sorted(ps)
            uniq = [ps[0]]
            for v in ps[1:]:
                if v - uniq[-1] > 60:                 # both lines of a nosing count once
                    uniq.append(v)
            if len(uniq) < R["tread_min_lines"]:
                continue
            gaps = np.diff(uniq)
            med = float(np.median(gaps))
            reg = float(np.mean(np.abs(gaps - med) <= 0.2 * med))
            if sp_lo <= med <= sp_hi and reg >= 0.6:
                flights.append((len(uniq), round(med)))
        if len(flights) > best[0]:
            best = (len(flights), flights)
    return best


# Layers whose round shapes are not sanitary fixtures.
_NOT_FIXTURE_LAYER = re.compile(r"FURN|CHAIR|TABLE|LIGHT|LUMIN|TREE|PLANT|LANDSC|COL|GRID|CAR|PARK|"
                                r"DIM|TEXT|ANNO|HATCH|SYMB|ELEC|LAMP|FIRE|SPRINK|DET|SMOKE|"
                                r"TAG|SCHED|MARK|LABEL|BUBBLE|AUDIT|LEGEND|NORTH|SECT|"
                                # door / window schedule tags ("S.S. door sdl."),
                                # text and attribute-tag blocks ("tex", ATRI)
                                r"DOOR|WINDOW|WIND|SDL|TEX|ATTR|ATRI")


def _fixture_shapes(doc, msp, units):
    """Sanitary fixtures by their shape: the oval of a WC pan or a wash
    basin bowl (an ellipse 300-800 mm long), or a round basin (a circle of
    120-300 mm radius) - wherever they are drawn, loose or inside blocks.
    Returns their centres (mm) as rows (x, y, oval): oval is 1 for an
    ellipse (a WC pan's shape), 0 for a circle. Round shapes on furniture,
    lighting, landscape, column and similar layers are left out, and so is
    any shape with a text inside it: that is a tag bubble (W-1, FMD-3A,
    C-7), never a pan or a basin - door and window tags in a fire tower
    and pipe sleeves in an AHU room made both read as toilets."""
    from .dxfio import effective_layer, iter_entities
    out = []
    try:
        from scipy.spatial import cKDTree
        tpts = [(t.x, t.y) for t in dr._collect_texts_in_modelspace(msp, units)
                + dr._collect_texts_from_blocks(doc, msp, units)]
        ttree = cKDTree(np.array(tpts)) if tpts else None
    except Exception:                                 # noqa: BLE001
        ttree = None
    for kind in ("ELLIPSE", "CIRCLE"):
        for e, xf, _d in iter_entities(doc, msp, kind):
            try:
                if _NOT_FIXTURE_LAYER.search((effective_layer(e, xf) or "").upper()):
                    continue
                if any(_NOT_FIXTURE_LAYER.search((b or "").upper()) for b in xf.blocks):
                    continue                          # a door / window tag block, a legend ...
                k = units.mm_per_unit * xf.scale_avg
                if kind == "ELLIPSE":
                    a = e.dxf.major_axis
                    semi = math.hypot(a[0], a[1]) * k
                    ratio = float(e.dxf.ratio)
                    if not (150.0 <= semi <= 400.0 and 0.4 <= ratio <= 0.95):
                        continue
                    inner = semi                  # a tag's text sits anywhere along it
                else:
                    r = float(e.dxf.radius) * k
                    if not (120.0 <= r <= 300.0):
                        continue
                    inner = r * 1.2
                c = e.dxf.center
                cx, cy = units.point_to_mm(*xf.apply(c[0], c[1]))
                if ttree is not None and ttree.query_ball_point((cx, cy), inner):
                    continue                          # a tag bubble: it holds a text
                out.append((cx, cy, 1.0 if kind == "ELLIPSE" else 0.0))
            except Exception:                         # noqa: BLE001
                continue
    return np.array(out) if out else np.zeros((0, 3))


def _content_points(doc, msp, units):
    """Positions of curved symbols (arcs, ellipses, splines, circles) and the
    named inserts - the raw material of fixtures and revision clouds."""
    from .dxfio import iter_entities
    pts = collections.defaultdict(list)
    for kind in ("ARC", "ELLIPSE", "SPLINE", "CIRCLE"):
        for e, xf, _d in iter_entities(doc, msp, kind):
            try:
                if kind == "SPLINE":
                    q = list(e.control_points)
                    if not q:
                        continue
                    p = (sum(v[0] for v in q) / len(q), sum(v[1] for v in q) / len(q))
                else:
                    p = (e.dxf.center.x, e.dxf.center.y)
                pts[kind].append(units.point_to_mm(*xf.apply(*p)))
            except Exception:                         # noqa: BLE001
                continue
    return {k: np.array(v) for k, v in pts.items() if v}


def _fixtures_named(doc, msp, units):
    """Sanitary fixture inserts as (x, y, block name): the same test as the
    text engine's fixture finder (block or layer name), kept separate so the
    name can be shown as metadata and wheelchairs, cars or chairs left out."""
    out = []
    for e in msp.query("INSERT"):
        nm = (e.dxf.get("name") or "").upper()
        ly = (e.dxf.get("layer") or "").upper()
        try:
            if dr._FIXTURE_NAME_RE.search(nm) or dr._FIXTURE_NAME_RE.search(ly) or dr._is_fixture_layer(ly):
                if _NOT_FIXTURE.search(nm):
                    continue
                x, y = units.point_to_mm(e.dxf.insert.x, e.dxf.insert.y)
                out.append((x, y, e.dxf.get("name") or ly))
        except Exception:                             # noqa: BLE001
            continue
    # Fixtures nested in a larger block (a whole toilet block placed once).
    pts = dr._collect_fixtures(doc, msp, units)
    have = {(round(x), round(y)) for x, y, _ in out}
    for x, y in pts:
        if (round(x), round(y)) not in have:
            out.append((x, y, "(inside a block)"))
    return out


def _count_in(arr, pg):
    if arr is None or not len(arr):
        return 0
    b = pg.bounds
    m = (arr[:, 0] >= b[0]) & (arr[:, 0] <= b[2]) & (arr[:, 1] >= b[1]) & (arr[:, 1] <= b[3])
    sub = arr[m]
    return int(shapely.contains_xy(pg, sub[:, 0], sub[:, 1]).sum()) if len(sub) else 0


# --------------------------------------------------------------- entity ---
class _E:
    """One geometry finding while it is being built."""
    __slots__ = ("cls", "sub", "rule", "pg", "src", "evidence", "features",
                 "reason", "family", "floor", "extra")

    def __init__(self, cls, rule, pg, src, floor, sub=""):
        self.cls, self.rule, self.pg, self.src, self.floor, self.sub = cls, rule, pg, src, floor, sub
        self.evidence = []
        self.features = {}
        self.reason = ""
        self.family = None
        self.extra = {}

    def ev(self, ok, text, metadata=False):
        self.evidence.append({"ok": bool(ok), "text": text, "metadata": bool(metadata)})


# ------------------------------------------------------------------ run ---
def _rotated_cluster_ents(doc, msp, units, floor, ctx_floor, profile, unit_override, diag):
    """The floor's rotated cores (rotation.py) classified in a drawing of
    their own turned to the axes: [(core polygon, [entities turned back])]."""
    from . import rotation as _rot
    from shapely import affinity as _aff
    from shapely.geometry import Polygon as _P
    if not dr.rule_book.defaults().get("rotated_cores", True):
        return []
    try:
        clusters = _rot.wall_angle_clusters(ctx_floor.get("segs") or [], floor["polygon"])
    except Exception:                                 # noqa: BLE001
        return []
    out = []
    for cl in clusters:
        theta = cl["theta"]
        try:
            reg = _P(cl["region"]).buffer(0)
            origin = (reg.centroid.x, reg.centroid.y)
            mini, mmsp = _rot.rotated_subdocument(doc, msp, units, cl["region"], theta, origin)
            mini_floor = {"name": floor["name"], "polygon": _rot.rotate_pts(cl["region"], -theta, origin)}
            sub = run(None, [mini_floor], unit_override, profile=profile, loaded=(mini, mmsp, units),
                      keep_context={}, _rotated=True)
            ents = []
            for e in sub.get(floor["name"]) or []:
                try:
                    e.pg = _aff.rotate(e.pg, theta, origin=origin)
                except Exception:                     # noqa: BLE001
                    continue
                keep = {k: v for k, v in (e.features or {}).items()
                        if k not in ("width_mm", "height_mm", "aspect", "area_sqm", "perimeter_m",
                                     "rectangularity", "vertices", "centroid", "bbox")}
                e.features = dict(_shape(e.pg), **keep)
                e.features["rotated_deg"] = theta
                if isinstance(e.src, dict):
                    e.src = dict(e.src)
                    if e.src.get("poly"):
                        e.src["poly"] = [[x, y] for x, y in _rot.rotate_pts(e.src["poly"], theta, origin)]
                    if e.src.get("bbox"):
                        e.src["bbox"] = [round(v) for v in e.pg.bounds]
                e.ev(True, "Core drawn at {0:g} degrees: worked out turned to the axes".format(theta), metadata=True)
                ents.append(e)
            out.append((_P(cl["core"]).buffer(0), ents, theta))
            diag["rotated cores worked out"] += 1
        except Exception:                             # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("rotated core (%s deg) on %s", theta, floor.get("name"))
    return out


def run(dxf_path, floors, unit_override=None, out_dxf=None, profile=None,
        loaded=None, progress=None, keep_context=None, _rotated=False):
    """Classify every floor's geometry. floors: [{"name", "polygon"}] (mm).

    Writes the geometry annotated DXF to out_dxf when given. Returns
    {"floors": {name: [entity]}, "summary", "families", "classes", ...}."""
    profile = profile or load_profile()
    R = profile["ranges"]
    doc, msp, units = loaded if loaded else dr.load_document(dxf_path, unit_override)
    ctx = keep_context if keep_context is not None else {}
    raw = gx.extract_geometry(dxf_path, floors, {}, unit_override=unit_override,
                              keep_context=ctx, loaded=(doc, msp, units),
                              x_layer_ok=x_any_layer)
    if progress:
        progress("candidates")
    curves = _content_points(doc, msp, units)
    curvy = np.vstack([v for k, v in curves.items() if k in ("ARC", "ELLIPSE", "SPLINE")]) \
        if any(k in curves for k in ("ARC", "ELLIPSE", "SPLINE")) else np.zeros((0, 2))
    fixtures = _fixtures_named(doc, msp, units)
    fx_arr = np.array([(x, y) for x, y, _ in fixtures]) if fixtures else np.zeros((0, 2))
    fshapes = _fixture_shapes(doc, msp, units)
    col_rejects = {}                  # C / L shapes on the column layer: core walls
    columns = _column_shapes(doc, msp, units, floors, rejected=col_rejects,
                             segs={f["name"]: (ctx.get(f["name"]) or {}).get("segs") or [] for f in floors})
    circles = dr._collect_circles(doc, msp, units)
    wall_hatches = dr._collect_wall_hatches(doc, msp, units)   # windows, and the guardrail
    # the hanging walls (an object drawn under them) are not walls: out of
    # the hatches and of the C / L column shapes (gx.extract_geometry took
    # them out of the segments already)
    hanging = dr.find_hanging_walls(doc, msp, units, wall_hatches)
    # what the wall tracer reads: every hatch piece and the curved faces
    all_hatches = dr._collect_all_hatches(doc, msp, units)
    wall_arcs = dr._collect_wall_arcs(doc, msp, units)
    door_arcs = dr._collect_door_arcs(doc, msp, units) if dr.DRAW_DOORS else None
    from . import elements as _el
    glass_segs = _el.collect_glass(doc, msp, units) if _el.ENABLED else None
    if hanging:
        _s, _w, all_hatches, _c = dr.drop_hanging_walls(hanging, None, None, all_hatches)
        _s, wall_hatches, _a, _c = dr.drop_hanging_walls(hanging, None, wall_hatches)
        hp = [h["poly"] for h in hanging]

        def _not_hanging(g):
            return not any(g.intersects(p) and g.intersection(p).area >= 0.5 * g.area for p in hp)
        col_rejects = {k: [g for g in v if _not_hanging(g)] for k, v in col_rejects.items()}
        columns = {k: [c for c in v if _not_hanging(c[0])] for k, v in columns.items()}

    ents_by_floor = {}
    diag = collections.Counter()
    # what the element pipeline locks before the walls are traced (Step 3
    # does the same, locked.LockedMask): the X marks' boxes and the columns
    # - no line inside them is a wall face, no hatch inside them is wall
    # material (rule element_pipeline, read at its shipped setting here)
    locked_by_floor = {}
    lock_walls = bool(dr.rule_book.defaults().get("element_pipeline", True)) if LOCK_WALLS is None else bool(LOCK_WALLS)
    from . import jobs as _jobs
    for floor in floors:
        _jobs.check()
        name = floor["name"]
        items = raw["floors"].get(name, [])
        if lock_walls:
            try:
                from shapely.ops import unary_union as _lu
                lk = [g for g in (_poly(it.get("poly")) for it in items if it.get("kind") == "x") if g is not None]
                lk += [pg for pg, _l, _h in (columns.get(name) or []) if pg is not None and not pg.is_empty]
                u_ = _lu(lk).buffer(0) if lk else None
                if u_ is not None and not u_.is_empty:
                    locked_by_floor[name] = u_
                    diag["locked before the walls: X boxes and columns"] += len(lk)
            except Exception:                         # noqa: BLE001
                pass
        c = ctx.get(name, {})
        seg = np.array([(s[0], s[1], s[2], s[3]) for s in c.get("segs") or []]) \
            if c.get("segs") else np.zeros((0, 4))
        swings = c.get("swings") or []
        ents_by_floor[name] = _classify_floor(name, floor, items, seg, swings, curvy, fx_arr,
                                              fixtures, R, diag, fshapes,
                                              columns=columns.get(name), sequence=GEOMETRY_SEQUENCE,
                                              circles=circles, walls=c.get("walls"),
                                              seg_rows=c.get("segs"),
                                              hatches=wall_hatches)
    if not _rotated:
        for floor in floors:
            name = floor["name"]
            for core, ents, theta in _rotated_cluster_ents(doc, msp, units, floor, ctx.get(name) or {},
                                                           profile, unit_override, diag):
                def _in(e):
                    try:
                        return core.contains(e.pg.representative_point())
                    except Exception:                 # noqa: BLE001
                        return False
                kept = [e for e in ents_by_floor.get(name, []) if not _in(e)]
                ents_by_floor[name] = kept + [e for e in ents if _in(e)]
    if progress:
        progress("classified")

    def _turned(e):
        # worked out in a turned core's own run, guarded and cleaned there
        # against its own walls: the main run's walls (read with the axes
        # assumed) must not touch it
        return bool((e.features or {}).get("rotated_deg"))
    _keep_identified(ents_by_floor, profile, diag)
    if (profile.get("identify") or {}).get("_guardrail", True):
        hatches = wall_hatches
        for name in ents_by_floor:
            walls_fl = (ctx.get(name) or {}).get("walls") or []
            for e in ents_by_floor[name]:
                if _turned(e):
                    continue
                _guard_finding(e, walls_fl, hatches, diag)
    # slits and spikes cleaned out of the outlines (dxf_reader.clean_outline)
    for name in ents_by_floor:
        for e in ents_by_floor[name]:
            if e.pg is None or e.pg.geom_type != "Polygon" or _turned(e):
                continue
            pts = list(e.pg.exterior.coords)[:-1]
            holes = [list(r.coords)[:-1] for r in e.pg.interiors]
            np_, nh, changed = dr.clean_outline(pts, holes)
            if changed:
                e.pg = Polygon(np_, nh)
                e.features = dict(e.features or {}, **_shape(e.pg))
                e.ev(True, "Outline cleaned of slits and spikes")
                diag["outlines cleaned"] += 1
    if _rotated:
        # a turned core's own run: the findings as built, guarded and
        # cleaned, for the caller to turn back and merge
        return ents_by_floor
    for name in ents_by_floor:
        for r, s in (ctx.get(name) or {}).get("excluded") or []:
            diag["wall faces excluded: " + r] += 1
    _families_and_repeats(ents_by_floor, floors, R)
    out = _finish(ents_by_floor, profile, floors, diag)
    _add_walls(out, floors, ctx, wall_hatches, columns, col_rejects, all_hatches=all_hatches, arcs=wall_arcs,
               door_arcs=door_arcs, glass_segs=glass_segs, locked=locked_by_floor)
    out["hanging_walls"] = raw.get("hanging_walls") or []
    if out_dxf:
        write_dxf(doc, units, out, out_dxf)
        out["dxf"] = os.path.basename(out_dxf)
    return out



def _add_walls(out, floors, ctx, hatches, columns, col_rejects, all_hatches=None, arcs=None, door_arcs=None,
               window_evidence=None, glass_segs=None, locked=None):
    """The walls of each floor (dxf_reader.wall_polygons, one layer:
    AI_WALL) into the result: out["walls"] = {floor: {AI_WALL: [rings]}},
    out["doors"] = {floor: [opening quads]} and out["walls_summary"] =
    {"total": {AI_WALL: {count, length_m, area_sqm}}, "floors": ...}.

    The walls are traced as Step 3 traces them (walltrace: wall layers,
    each face with its nearest face, confirmed by the hatch), so the final
    drawing and the Step 3 drawing show the same walls. Pairing any two
    parallel lines 80-400 mm apart drew facade glazing, mullions, lift
    entrance graphics and bracing as walls (XA tower: 38% of what was drawn
    was wall). A floor with no wall-layer lines keeps the engine's strips.

    No window, cut-out or lift entrance is found here (2026-10-16): those
    are Step 3's, and the final drawing puts its windows and the cut-outs in
    a wall run into these walls and finds the lifts' entrances in them
    (app.geoengine_final). out["window_openings"] stays, empty;
    window_evidence is not read."""
    from . import walltrace as _wt
    walls, per, doors, gap_windows, glass = {}, {}, {}, {}, {}
    door_review = {}
    # rule doors_need_evidence (rules.py): a door stands in a gap in a wall
    evidence = bool(dr.rule_book.defaults().get("doors_need_evidence", True))
    strip_counts = {}
    total = {c: {"count": 0, "length_m": 0.0, "area_sqm": 0.0} for c in dr.WALL_CLASSES}
    for f in floors:
        name = f["name"]
        c = ctx.get(name) or {}
        cols = [pg for pg, _l, _h in (columns.get(name) or [])]
        wt = None
        if all_hatches is not None and _wt.ENABLED:
            try:
                bx = dr._floor_clip_box(f["polygon"])
                hf = [h for h in all_hatches
                      if bx[0] - 2000 <= min(p[0] for p in h[0]) and max(p[0] for p in h[0]) <= bx[2] + 2000
                      and bx[1] - 2000 <= min(p[1] for p in h[0]) and max(p[1] for p in h[0]) <= bx[3] + 2000]
                af = [a for a in arcs or ()
                      if bx[0] - 2000 <= a[0] + a[2] * math.cos(math.radians((a[3] + a[4]) / 2.0)) <= bx[2] + 2000
                      and bx[1] - 2000 <= a[1] + a[2] * math.sin(math.radians((a[3] + a[4]) / 2.0)) <= bx[3] + 2000]
                # (locked: the X marks' boxes and the columns, as Step 3's
                # element pipeline holds them out of its own wall trace)
                wt = _wt.trace_floor(c.get("segs") or [], hf, c.get("rows") or [], f["polygon"], arcs=af,
                                     locked=(locked or {}).get(name), counts=strip_counts)
            except Exception:                         # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception("wall trace for %s", name)
                wt = None
        # door openings first: they are cut out of the walls
        doors[name] = []
        if door_arcs and dr.DRAW_DOORS:
            try:
                bx = dr._floor_clip_box(f["polygon"])
                da = [a for a in door_arcs if bx[0] - 500 <= a[0] <= bx[2] + 500 and bx[1] - 500 <= a[1] <= bx[3] + 500]
                left_out = []
                doors[name] = [[[float(x), float(y)] for x, y in q]
                               for q in dr.door_openings(da, c.get("segs") or [],
                                                         (wt["strips"] if wt is not None else c.get("strips")) or [],
                                                         evidence=evidence, rejected=left_out)]
                door_review[name] = [r_ for r_ in left_out if r_.get("review")]
            except Exception:                         # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception("door openings for %s", name)
                doors[name] = []
        gap_windows[name] = []                   # (the windows are Step 3's)
        # glass partitions and glazing: not walls, not rooms (AI_GLASS)
        glass[name] = []
        if glass_segs:
            try:
                from . import elements as _el
                bx = dr._floor_clip_box(f["polygon"])
                gs = [w for w in glass_segs if bx[0] - 1000 <= w[0] <= bx[2] + 1000 and bx[1] - 1000 <= w[1] <= bx[3] + 1000]
                glass[name] = [[[float(x), float(y)] for x, y in list(g.exterior.coords)[:-1]]
                               for g in _el.glass_elements(gs, f["polygon"], cut=list(doors[name]))]
            except Exception:                         # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception("glass for %s", name)
        try:
            quads = list(doors[name])                 # not the closures paired: they ran along walls
            pieces, rep = dr.wall_polygons((wt["strips"] if wt is not None else c.get("strips")) or [],
                                           f["polygon"], ([] if wt is not None else hatches), cols,
                                           [], quads, list(col_rejects.get(name) or []),
                                           counts=strip_counts)
        except Exception:                             # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("walls for %s", name)
            continue
        walls[name] = {cls: dr.wall_rings(dr.merge_wall_pieces(p)) for cls, p in pieces.items()}
        per[name] = rep
        for cls, v in rep.items():
            for k in ("count", "length_m", "area_sqm"):
                total[cls][k] = round(total[cls][k] + v[k], 2)
    out["walls"] = walls
    out["doors"] = doors
    out["door_review"] = door_review
    out["plates"] = {f["name"]: [list(map(float, p[:2])) for p in f["polygon"]] for f in floors}
    out["window_openings"] = gap_windows
    out["glass"] = glass
    out["walls_summary"] = {"total": total, "floors": per, "strips": strip_counts}


_DOOR_NOT_ROOM = ("COLUMN", "WINDOW", "CUTOUT", "UNKNOWN_GEOMETRY", "UNKNOWN")
_DOOR_SHAFT_CLASSES = ("SHAFT", "LIFT", "STAIR_WELL")
_DOOR_MAIN_CLASSES = ("LIFT_LOBBY", "STAIRCASE", "SERVICE_LOBBY")


def door_rooms(rows):
    """The findings a door opening can lead into, for
    dxf_reader.draw_doors_by_level: [(outline points, is a shaft, is main
    accessible area, is a lift with its stop here, the rank of the room a
    door belongs to - elements.door_room_rank)] - a shaft, lift or stair well, or a room whose name or
    family is a shaft's (elements.is_shaft_room); main: a lift lobby,
    staircase or service lobby, or a circulation room by its name or family
    (elements.is_main_room). Columns, windows, cut-outs and unnamed shapes
    are not rooms. rows: the engine's findings ("cls", "poly", "label") or
    the final drawing's rows ("family", "name", "poly")."""
    from . import elements as _el
    out = []
    for r in rows or ():
        poly = r.get("poly") or []
        if len(poly) < 3:
            continue
        fam = str(r.get("family") or "")
        cls = str(r.get("cls") or "").upper()
        if cls in _DOOR_NOT_ROOM or fam in ("Column", "Window", "Cutout", "Ignore"):
            continue
        cat = str(r.get("category_layer") or "").upper()
        name = r.get("label") or r.get("name") or ""
        shaft = (cls in _DOOR_SHAFT_CLASSES and not cat) or cat in ("SHAFT", "LIFT") or _el.is_shaft_room(name, fam)
        main = not shaft and ((cls in _DOOR_MAIN_CLASSES and not cat) or _el.is_main_room(name, fam))
        lift = (((cls == "LIFT" and not cat) or cat == "LIFT" or fam == "Lift")
                and not (name and _el.LIFT_NOT_RE.search(" ".join(str(name).split()))))
        # the rank of the room a door belongs to (elements.door_room_rank)
        if shaft or lift:
            rank = _el.OWNER_SHAFT
        elif fam == "Void" or cls in ("VOID", "STAIR_WELL"):
            rank = _el.OWNER_NONE
        elif cls == "STAIRCASE" and not cat and not name:
            rank = _el.OWNER_STAIR
        elif cls in ("LIFT_LOBBY", "SERVICE_LOBBY") and not cat and not name:
            rank = _el.OWNER_LOBBY
        else:
            rank = _el.door_room_rank(name, fam or ("Staircase" if cls == "STAIRCASE" else ""), shaft=False, lift=False)
        out.append((poly, shaft, main, lift, rank))
    return out


def door_solid(walls_by_class=None, quads=(), rings=(), rows=()):
    """What is not floor for the doors' common-floor search (shapely): the
    walls drawn ({class: [rings of one wall: its outline, then its holes]},
    as draw_walls takes them; a bare ring is read too), window quads, glass
    rings and the column / cut-out findings."""
    from shapely.ops import unary_union
    gs = []
    for _cls, items in (walls_by_class or {}).items():
        for item in items or ():
            rings = item if (item and isinstance(item[0], (list, tuple)) and item[0]
                             and isinstance(item[0][0], (list, tuple))) else [item]
            reg = Polygon()
            for g in sorted((_poly(r) for r in rings), key=lambda g: -(g.area if g is not None else 0)):
                if g is not None:
                    reg = reg.symmetric_difference(g)
            if not reg.is_empty:
                gs.append(reg.buffer(0))
    for q in list(quads or ()) + list(rings or ()):
        g = _poly(q)
        if g is not None:
            gs.append(g)
    for r in rows or ():
        if str(r.get("cls") or "").upper() in ("COLUMN", "CUTOUT") or r.get("family") in ("Column", "Cutout"):
            g = _poly(r.get("poly") or [])
            if g is not None:
                gs.append(g)
    return unary_union(gs) if gs else None


def _column_holes(rows):
    """Where a column stands in a finding (a room), the piece of it inside:
    [(finding row, [hole polygons])] - drawn on AI_VOID, so the area step
    deducts it."""
    # (a cut-out standing in a room is a hole in it the same way)
    cols = [(_poly(r["poly"]), r["cls"] == "CUTOUT") for r in rows if r["cls"] in ("COLUMN", "CUTOUT")]
    cols = [c for c in cols if c[0] is not None]
    out = []
    if not cols:
        return out
    for r in rows:
        if r["cls"] in ("COLUMN", "WINDOW", "CUTOUT", "UNKNOWN_GEOMETRY"):
            continue
        pg = _poly(r["poly"])
        if pg is None:
            continue
        holes = []
        for c, cut in cols:
            # a cut-out is a hole only standing (almost) wholly in the room
            inside = (pg.intersects(c) and pg.intersection(c).area >= 0.9 * c.area) if cut \
                else pg.contains(c.representative_point())
            if inside:
                part = pg.intersection(c)
                if not part.is_empty and part.area > 1.0:
                    holes.extend(g for g in getattr(part, "geoms", [part]) if g.geom_type == "Polygon")
        if holes:
            out.append((r, holes))
    return out


def _guard_finding(e, walls_fl, hatches, diag):
    """The guardrail on one finding: cut back where it crosses a wall, a
    wall hatch or the facade (dxf_reader.guard_against_walls)."""
    if e.cls in ("STAIR_WELL", "WINDOW", "CUTOUT"):
        return                                        # a window lies in the wall itself
    c = e.pg.representative_point()
    pts = list(e.pg.exterior.coords)[:-1]
    holes = [list(r.coords)[:-1] for r in e.pg.interiors]
    w_use, h_use = walls_fl, hatches or []
    if e.cls == "STAIRCASE":
        # Treads, flights and handrails lie inside a staircase; only walls
        # on its outline or crossing in from outside may cut it (else its
        # centre, in the gap between the flights, keeps a 0.6 m sliver of
        # a 36 sq.m stair).
        inner = e.pg.buffer(-STAIR_GUARD_INSET_MM)
        if not inner.is_empty:
            w_use = [w for w in walls_fl
                     if not inner.contains(shapely.LineString([(w[0], w[1]), (w[2], w[3])]))]
            h_use = [h for h in h_use
                     if not (len(h[0]) >= 3 and inner.contains(Polygon(h[0])))]
    np_, nh, _nb, changed = dr.guard_against_walls(
        pts, holes, e.pg.bounds, (c.x, c.y), w_use, h_use)
    if changed and e.cls == "STAIRCASE":
        try:
            kept = Polygon(np_, nh).area
        except (ValueError, TypeError):
            kept = 0.0
        if kept < STAIR_GUARD_MIN_KEEP * e.pg.area:
            e.ev(True, "guardrail cut skipped: would remove most of the staircase "
                       "({0:.0f}% left)".format(100.0 * kept / e.pg.area if e.pg.area else 0))
            diag["staircase guardrail cut skipped"] += 1
            changed = False
    if changed:
        e.pg = Polygon(np_, nh)
        e.features = dict(e.features or {}, **_shape(e.pg))
        e.ev(True, "Cut back where it crossed a wall, hatch or facade")
        diag["cut back at a wall (guardrail)"] += 1


def _classify_floor(name, floor, items, seg, swings, curvy, fx_arr, fixtures, R, diag, fshapes=None,
                    columns=None, sequence=None, circles=None, walls=None, shaft_req=None,
                    seg_rows=None, hatches=None):
    ents = []
    xs, stairs, toilets, rooms = [], [], [], []
    for it in items:
        pg = _poly(it["poly"])
        if pg is None:
            continue
        rec = (it, pg)
        if it["kind"] == "x":
            xs.append(rec)
        elif it["kind"] == "stair":
            stairs.append(rec)
        elif it["kind"] == "toilet":
            toilets.append(rec)
        elif it["kind"] in ("outline", "space"):
            if it.get("hidden_layer"):
                diag["outline on a furniture/annotation layer, skipped"] += 1
                continue
            rooms.append(rec)

    # Door lines (door layers and blocks).
    door_lines = [tuple(sg[:4]) for sg in (seg_rows or []) if len(sg) > 7 and sg[7]]
    # a lift's door is an opening in its structural wall, whatever is drawn
    # in it (the landing door, its frame)
    lift_walls = _real_walls(walls, seg_rows, also_not=_LIFT_DOOR_LAYER)

    # Shared by the stages below, which run in the configured order
    # (config.json "geometry_sequence"): each stage skips what an earlier
    # one took, so the order decides who gets a shape both could claim.
    x_ents = []
    spaces = [(it, pg) for it, pg in rooms]
    col_pgs = []

    def taken(pg, iou=0.5):
        """Already found by an earlier stage: a column it stands in, or a
        finding it largely coincides with."""
        for c in col_pgs:
            if c.intersects(pg) and c.intersection(pg).area >= 0.6 * pg.area:
                return True
        for e in ents:
            if _iou(pg, e.pg) >= iou:
                return True
        return False

    def stage_columns():
        # ---- columns: obstacles only (2026-10-16) ----------------------------
        # The shapes the one column finder gives (Step 3 reads the same): a
        # shape standing in one is no room. Never a finding - a column is
        # Step 3's (its column finder, and the X sequence's step 6).
        for col in (columns or []):
            col_pgs.append(col[0])

    def stage_x_marks():
        # ---- X marks: evidence only (2026-10-16) -----------------------------
        # What an X mark is - a shaft, a lift, a window, a cut-out, a column's
        # cross or a void - is decided once, by Step 3's X sequence; nothing
        # here decides it. An X is read only as evidence for what this engine
        # reports: its place is no room (taken), a lift-size X is a lift well
        # for the lobby test, and a long X with tread lines on both long
        # sides is the open well of a staircase (stair_from_well).
        for it, pg in xs:
            if taken(pg):
                diag["X mark inside a column or a shape found earlier, skipped"] += 1
                continue
            f = _shape(pg)
            w, h = f["width_mm"], f["height_mm"]
            bx = pg.bounds
            vertical = (bx[3] - bx[1]) >= (bx[2] - bx[0])
            e = _E("X_MARK", "x_mark", pg, it, name)
            e.ev(True, "X mark {0}x{1} mm, {2:g} sq.m - what it is, Step 3's X sequence decides".format(
                w, h, f["area_sqm"]))
            well_dims = (R["stair_well_short_mm"][0] <= w <= R["stair_well_short_mm"][1] and
                         R["stair_well_long_mm"][0] <= h <= R["stair_well_long_mm"][1] and
                         f["aspect"] >= R["stair_well_aspect_min"])
            if well_dims:
                D = R["flight_width_mm"][1]
                if vertical:
                    regs = [(bx[0] - D, bx[1], bx[0] - 50, bx[3]), (bx[2] + 50, bx[1], bx[2] + D, bx[3])]
                else:
                    regs = [(bx[0], bx[1] - D, bx[2], bx[1] - 50), (bx[0], bx[3] + 50, bx[2], bx[3] + D)]
                sides = tuple(_treads(seg, r, vertical, R["tread_spacing_mm"], R["tread_min_lines"])[0]
                              for r in regs)
                if sides[0] and sides[1]:
                    e.extra["well"] = True            # tread lines on both sides: a stair's open well
                    e.extra["sides"] = sides
            if (not e.extra.get("well") and f["area_sqm"] < R["void_min_sqm"] and
                    f["aspect"] <= R["lift_aspect_max"] and R["lift_side_mm"][0] <= w and h <= R["lift_side_mm"][1]):
                e.extra["lift"] = True                # lift size: a lift well, for the lobby test
            e.features = f
            e.features["x"] = {"detected": True, "size_mm": [w, h], "orientation": "vertical" if vertical else "horizontal"}
            x_ents.append(e)
        ents.extend(x_ents)


    def stage_staircases():
        # ---- 2. staircases --------------------------------------------------------
        st_ents = []
        for it, pg in stairs:
            if taken(pg):
                continue
            f = _shape(pg)
            rng = R["staircase_sqm"]
            fits = rng[0] <= f["area_sqm"] <= rng[1]
            e = _E("STAIRCASE", "stair_treads" if fits else "stair_treads_odd_size", pg, it, name)
            e.ev(True, "Evenly spaced tread lines in runs (flights)")
            e.ev(fits, "Area {0:g} sq.m (staircases measured {1:g}-{2:g} sq.m)".format(
                f["area_sqm"], rng[0], rng[1]))
            e.features = f
            st_ents.append(e)
        wells = [e for e in x_ents if e.extra.get("well")]
        for wl in wells:
            host = next((s for s in st_ents if s.pg.intersection(wl.pg).area >= 0.5 * wl.pg.area), None)
            if host is not None:
                host.ev(True, "Open well between the flights ({0})".format("X-marked"))
                host.extra.setdefault("wells", []).append(wl)
                continue
            if not (wl.extra.get("sides") and all(wl.extra["sides"])):
                continue
            # the staircase around a well: the smallest enclosed space holding it
            best = None
            rng = R["staircase_sqm"]
            for it, pg in spaces:
                a = pg.area / 1e6
                if not (rng[0] <= a <= rng[1]) or pg.area < 2.0 * wl.pg.area:
                    continue
                if pg.contains(wl.pg.representative_point()) and pg.intersection(wl.pg).area >= 0.8 * wl.pg.area:
                    if best is None or pg.area < best[1].area:
                        best = (it, pg)
            if best is not None:
                pg, src, how = best[1], best[0], "the enclosed space around the well"
            else:
                b = wl.pg.bounds
                D = R["flight_width_mm"][1]
                vertical = (b[3] - b[1]) >= (b[2] - b[0])
                pg = sbox(b[0] - D, b[1], b[2] + D, b[3]) if vertical else sbox(b[0], b[1] - D, b[2], b[3] + D)
                src, how = wl.src, "the well plus a flight width either side (no enclosing walls found)"
            e = _E("STAIRCASE", "stair_from_well", pg, src, name)
            e.ev(True, "Flights with tread lines on both sides of an open well")
            e.ev(best is not None, "Boundary: " + how)
            e.features = _shape(pg)
            e.extra["wells"] = [wl]
            st_ents.append(e)
        # two flights side by side in an enclosed space of staircase size -
        # found here, with the others, so the stages after it (service
        # lobbies) see these staircases too
        rng = R["staircase_sqm"]
        st_pgs = [s.pg.buffer(50) for s in st_ents]
        for it, pg in rooms:
            if it["kind"] != "space" or taken(pg, iou=0.6):
                continue
            f = _shape(pg)
            if not (rng[0] <= f["area_sqm"] <= rng[1]):
                continue
            if any(_iou(pg, s.pg) >= 0.6 for s in st_ents) or any(pg.within(h) for h in st_pgs):
                continue                          # a staircase already, or a piece of one
            nfl, fl = _flights(seg, pg, R)
            if nfl < 2:
                continue
            e = _E("STAIRCASE", "stair_flights_space", pg, it, name)
            e.ev(True, "{0} flights side by side: {1}".format(nfl, ", ".join(
                "{0} treads at {1} mm".format(n, sp) for n, sp in fl[:3])))
            e.ev(True, "Enclosed by walls, {0:g} sq.m (staircases measured {1:g}-{2:g} sq.m)".format(
                f["area_sqm"], rng[0], rng[1]))
            e.features = f
            e.features["flights"] = nfl
            st_ents.append(e)
        # one staircase per place
        kept = []
        for e in sorted(st_ents, key=lambda e: -e.pg.area):
            if any(e.pg.intersection(k.pg).area >= 0.6 * min(e.pg.area, k.pg.area) for k in kept):
                continue
            kept.append(e)
        ents.extend(kept)


    def stage_toilets():
        # ---- 3. toilets from sanitary fixtures ---------------------------------------
        for it, pg in toilets:
            if taken(pg):
                continue
            inside = [(x, y, n) for x, y, n in fixtures if pg.buffer(800).contains(shapely.Point(x, y))]
            if len(inside) < 2:
                diag["fixture cluster of wheelchairs/cars/furniture, skipped"] += 1
                continue
            pts = np.array([(x, y) for x, y, _ in inside])
            best = None
            for _it2, sp in spaces:
                a = sp.area / 1e6
                if a > R["toilet_max_sqm"] or a < 1.5:
                    continue
                k = int(shapely.contains_xy(sp, pts[:, 0], pts[:, 1]).sum())
                if k >= max(2, 0.6 * len(pts)) and (best is None or sp.area < best.area):
                    best = sp
            boundary = best if best is not None else pg
            e = _E("TOILET", "toilet_fixtures", boundary, it, name)
            names = collections.Counter(n for _x, _y, n in inside)
            e.ev(True, "{0} sanitary fixtures together".format(len(inside)))
            e.ev(True, "Fixture blocks/layers: " + ", ".join("{0} x{1}".format(k[:24], v) for k, v in names.most_common(3)),
                 metadata=True)
            curv = _count_in(curvy, boundary) / max(boundary.area / 1e6, 0.5)
            e.ev(curv >= 1.0, "Curved symbols (pans, basins): {0:.1f} per sq.m".format(curv))
            e.ev(best is not None, "Boundary: the enclosed room holding the fixtures" if best is not None
                 else "Boundary approximate: no enclosed room found around the fixtures")
            e.features = _shape(boundary)
            e.features["fixtures"] = len(inside)
            ents.append(e)


    def stage_lift_wells():
        # ---- 4. lift wells drawn without an X ---------------------------------------
        # A wall-enclosed square of lift size, in a bank with identical squares
        # or X-marked lifts: a lift well too, X or not.
        lift_x = [e for e in x_ents if e.extra.get("lift")]
        squares = []
        for it, pg in rooms:
            if it["kind"] != "space" or taken(pg):
                continue
            f = _shape(pg)
            if (f["aspect"] <= R["lift_aspect_max"] and f["rectangularity"] >= 0.9 and
                    R["lift_side_mm"][0] <= f["width_mm"] and f["height_mm"] <= R["lift_side_mm"][1]):
                if any(_iou(pg, e.pg) >= 0.6 for e in lift_x):
                    continue                              # the X-marked lift itself
                squares.append((it, pg, f))

        def same(f, g):
            return abs(f["width_mm"] - g["width_mm"]) <= 150 and abs(f["height_mm"] - g["height_mm"]) <= 150

        for it, pg, f in squares:
            mates = [1 for _i, o, g in squares if o is not pg and same(f, g) and pg.distance(o) <= 800.0]
            xmates = [1 for e in lift_x if same(f, e.features) and pg.distance(e.pg) <= 800.0]
            if not (mates or xmates):
                continue
            e = _E("LIFT_WELL", "lift_space_bank", pg, it, name)     # evidence, never reported
            e.ev(True, "Square enclosed space {0}x{1} mm (lift wells measured {2}-{3} mm square)".format(
                f["width_mm"], f["height_mm"], R["lift_side_mm"][0], R["lift_side_mm"][1]))
            e.ev(True, "In a bank with {0} identical square{1}".format(len(mates) + len(xmates),
                                                                       "" if len(mates) + len(xmates) == 1 else "s"))
            twin = [x for x in lift_x if same(f, x.features)]
            e.ev(bool(twin), "Same size as {0} X-marked lift well{1} on this floor".format(len(twin), "" if len(twin) == 1 else "s")
                 if twin else "No X-marked lift of this size on this floor")
            e.ev(False, "No X mark inside")
            e.features = f
            e.extra["bank"] = len(mates) + len(xmates)
            ents.append(e)


    def stage_rooms():
        # ---- 5. enclosed spaces and outlines: rooms, or unknown ----------------------
        # (a space with two flights side by side is a staircase - found in
        # stage_staircases)
        specific = list(ents)
        sp_tree = shapely.STRtree([e.pg for e in specific]) if specific else None
        holders = [e for e in specific if e.cls in ("STAIRCASE", "TOILET") or
                   (e.cls == "X_MARK" and e.features.get("area_sqm", 0.0) >= R["void_min_sqm"])]
        holder_pgs = [e.pg.buffer(50) for e in holders]
        holder_tree = shapely.STRtree(holder_pgs) if holders else None
        for it, pg in rooms:
            if taken(pg, iou=0.6):
                continue
            near = sp_tree.query(pg) if sp_tree is not None else []
            if any(_iou(pg, specific[int(k)].pg) >= 0.6 for k in near):
                continue
            if holder_tree is not None and any(pg.within(holder_pgs[int(k)]) for k in holder_tree.query(pg)):
                continue                          # a piece of a staircase, void or toilet
            f = _shape(pg)
            width = 2.0 * pg.area / pg.length if pg.length else 0.0
            nfx =_count_in(fshapes, pg) if (fshapes is not None and it["kind"] == "space" and
                                             1.5 <= f["area_sqm"] <= R["toilet_max_sqm"]) else 0
            # Round shapes alone are pipe sleeves, valves and symbols as
            # often as basins: a toilet has a WC pan, which is an oval.
            novals = (_count_in(fshapes[fshapes[:, 2] > 0.5], pg)
                      if nfx and fshapes.shape[1] > 2 else 0)
            if nfx >= 2 and novals >= 1:
                e = _E("TOILET", "toilet_fixture_shapes", pg, it, name)
                e.ev(True, "{0} sanitary fixture shapes inside (WC pans / wash basins: ovals and round bowls)".format(nfx))
                e.ev(True, "Enclosed by walls, {0:g} sq.m".format(f["area_sqm"]))
                e.features = f
                e.features["fixtures"] = nfx
                ents.append(e)
                continue
            if _wavy(pg):
                e = _E("UNKNOWN_GEOMETRY", "unknown_cloud", pg, it, name)
                e.reason = "Revision cloud (a chain of arcs), not an architectural space"
                e.ev(True, e.reason)
            elif it["kind"] == "outline" and f["area_sqm"] >= R["room_min_sqm"]:
                # measured: a closed polyline that the walls do not enclose was a
                # room only 5 times in 33 - too weak to call it one
                e = _E("UNKNOWN_GEOMETRY", "unknown_outline", pg, it, name)
                e.reason = "Closed outline {0:g} sq.m, not enclosed by walls, with no X, treads or fixtures".format(f["area_sqm"])
                e.ev(False, e.reason)
            elif f["area_sqm"] >= R["room_min_sqm"] and width >= R["room_min_width_mm"]:
                rule = "room_space"
                e = _E("ROOM", rule, pg, it, name)
                e.ev(True, "Closed space {0:g} sq.m, {1:.1f} m across".format(f["area_sqm"], width / 1000.0))
                e.ev(True, "Enclosed by walls")
                e.ev(False, "No X mark, tread pattern or fixture group to say what it is")
            else:
                e = _E("UNKNOWN_GEOMETRY", "unknown_small", pg, it, name)
                e.reason = ("Too small for a room ({0:g} sq.m) and no X, treads or fixtures".format(f["area_sqm"])
                            if f["area_sqm"] < R["room_min_sqm"] else
                            "Too narrow for a room ({0:.0f} mm across) - a wall cavity, ledge or duct".format(width))
                e.ev(False, e.reason)
            if it.get("layers"):
                e.ev(True, "Layer: " + ", ".join(it["layers"][:3]), metadata=True)
            e.features = f
            e.features["width_across_mm"] = round(width)
            ents.append(e)


    def stage_lift_lobbies():
        # ---- lift lobbies: an enclosed space with lift wells on both of its
        # longer sides (the lifts found above - X-marked lift squares, and
        # lift-size squares in a bank) ------------------------------------
        lifts = [e for e in ents if e.cls == "LIFT_WELL" or (e.cls == "X_MARK" and e.extra.get("lift"))]
        if len(lifts) < 2:
            return
        reach = float(R.get("lift_lobby_side_reach_mm", 600.0))
        # Any polygon will do - a space the walls enclose, or a closed
        # outline drawn round the lobby (its ends often open onto a corridor,
        # so the walls alone do not close it).
        for it, pg in rooms:
            if it["kind"] not in ("space", "outline") or it.get("hidden_layer") or taken(pg):
                continue
            a = pg.area / 1e6
            if not (float(R.get("lift_lobby_min_sqm", 6.0)) <= a <= float(R.get("lift_lobby_max_sqm", 120.0))):
                continue
            b = pg.bounds
            w, h = b[2] - b[0], b[3] - b[1]
            if w >= h:     # long sides are the bottom and the top
                sides = (sbox(b[0], b[1] - reach, b[2], b[1] + reach),
                         sbox(b[0], b[3] - reach, b[2], b[3] + reach))
            else:          # long sides are the left and the right
                sides = (sbox(b[0] - reach, b[1], b[0] + reach, b[3]),
                         sbox(b[2] - reach, b[1], b[2] + reach, b[3]))
            on = []
            for zone in sides:
                hit = [l for l in lifts if l.pg.intersects(zone) and
                       l.pg.intersection(zone).area >= 0.1 * l.pg.area and
                       not l.pg.representative_point().within(pg)]
                on.append(hit)
            if not (on[0] and on[1]):
                continue
            # Do the lifts open onto it? A lift door is a door object (door
            # layer or block, a swing) at the lift, or an opening in the
            # lift's wall on the side facing this space - wide enough for a
            # lift door. Lifts that open onto nothing here are seen from
            # their machine room (on a terrace, a plant floor).
            beside = on[0] + on[1]
            with_door = [l for l in beside if _lift_has_door(l.pg, pg, door_lines, swings, lift_walls, R)]
            # Lifts between two lobbies open onto one of them, so about half
            # do; a stray door swing near one lift of a machine room is not
            # a lobby. A lobby has doors from lift_lobby_door_share of them.
            if with_door and len(with_door) >= float(R.get("lift_lobby_door_share", 0.3)) * len(beside):
                e = _E("LIFT_LOBBY", "lift_lobby_between_lifts", pg, it, name)
                e.ev(True, "Enclosed space {0:g} sq.m with lift wells along both longer sides "
                           "({1} on one, {2} on the other)".format(round(a, 1), len(on[0]), len(on[1])))
                e.ev(True, "{0} of the {1} lifts beside it have a door onto it".format(
                    len(with_door), len(on[0]) + len(on[1])))
            else:
                e = _E("MACHINE_ROOM", "machine_room_lifts_without_doors", pg, it, name)
                e.ev(True, "Enclosed space {0:g} sq.m between lift wells ({1} on one side, {2} on the other)".format(
                    round(a, 1), len(on[0]), len(on[1])))
                e.ev(False, "{0} of the {1} lifts beside it have a door onto it - their machine room, "
                            "not a lift lobby".format(len(with_door) or "None", len(beside)))
            e.features = _shape(pg)
            ents.append(e)

    def stage_service_lobbies():
        # ---- service lobbies: the polygon a staircase's door opens into.
        # A lift opening onto that lobby is its service lift. ---------------
        stairs = [e for e in ents if e.cls == "STAIRCASE"]
        if not stairs or not swings:
            return
        lifts = [e for e in ents if e.cls == "LIFT_WELL" or (e.cls == "X_MARK" and e.extra.get("lift"))]
        lo_sqm = float(R.get("service_lobby_min_sqm", 2.0))
        hi_sqm = float(R.get("service_lobby_max_sqm", 40.0))
        # a lobby holds a min_w square somewhere; a wall cavity round a
        # staircase (about 300 mm) does not
        min_w = float(R.get("service_lobby_min_width_mm", 1000.0))
        for st in stairs:
            edge = st.pg.exterior
            doors = [sw for sw in swings if edge.distance(shapely.Point(sw[0], sw[1])) <= 300.0]
            if not doors:
                continue
            found = []
            for sw in doors:
                hinge = shapely.Point(sw[0], sw[1])
                leaf = float(sw[2]) if len(sw) > 2 and sw[2] else 900.0
                best = None
                for it, pg in rooms:
                    if it["kind"] not in ("space", "outline") or it.get("hidden_layer") or taken(pg):
                        continue
                    a = pg.area / 1e6
                    if not (lo_sqm <= a <= hi_sqm):
                        continue
                    if pg.buffer(-min_w / 2.0, join_style=2).is_empty:
                        diag["service lobby candidate too narrow (wall cavity)"] += 1
                        continue                       # no room for a min_w square: a cavity
                    if _iou(pg, st.pg) >= 0.3 or pg.intersection(st.pg).area > 0.2 * pg.area:
                        continue                       # the staircase itself
                    if pg.distance(hinge) > leaf + 300.0:
                        continue
                    if best is None or pg.area < best[1].area:
                        best = (it, pg)
                if best is not None and all(_iou(best[1], f[1]) < 0.6 for f in found):
                    found.append(best)
            if not found:
                st.ev(False, "Has a door, but no polygon on the other side of it - no service lobby found")
                diag["staircase door with no service lobby polygon"] += 1
                continue
            for it, pg in found:
                e = _E("SERVICE_LOBBY", "service_lobby_off_stair", pg, it, name)
                e.ev(True, "The space {0}'s door opens into ({1:g} sq.m)".format(
                    "the staircase", round(pg.area / 1e6, 1)))
                svc = [l for l in lifts if l.pg.distance(pg) <= float(R.get("lift_lobby_side_reach_mm", 600.0))
                       and _lift_has_door(l.pg, pg, door_lines, swings, lift_walls, R)]
                if svc:
                    e.ev(True, "Service lift opening onto it ({0})".format(len(svc)))
                    e.extra["service_lifts"] = len(svc)
                e.features = _shape(pg)
                ents.append(e)

    stages = {"columns": stage_columns, "x_marks": stage_x_marks,
              "staircases": stage_staircases, "toilets": stage_toilets,
              "lift_wells": stage_lift_wells, "lift_lobbies": stage_lift_lobbies,
              "service_lobbies": stage_service_lobbies, "rooms": stage_rooms}
    order = [k for k in (sequence or GEOMETRY_SEQUENCE) if k in stages]
    # Whatever the sequence leaves out still runs, after it, in the
    # default order - a stage is reordered, never lost.
    order += [k for k in DEFAULT_SEQUENCE if k not in order]
    for key in order:
        stages[key]()

    x_tree = shapely.STRtree([o.pg for o in x_ents]) if x_ents else None
    for e in ents:
        e.features.setdefault("x", {"detected": False})
        near = x_tree.query(e.pg) if x_tree is not None else []
        e.features["x_inside"] = sum(1 for k in near if x_ents[int(k)] is not e and
                                    x_ents[int(k)].pg.intersection(e.pg).area >= 0.6 * x_ents[int(k)].pg.area)
        e.features["source"] = e.src.get("kind")
        e.features["layers"] = (e.src.get("layers") or [])[:4]
    # the evidence (X marks, lift wells) is not reported: the six are Step 3's
    return [e for e in ents if e.cls not in EVIDENCE_CLASSES]


def _wall_faces(segs):
    """The wall-layer lines of a floor (walltrace.is_wall_layer, not a door
    leaf) and an index over them, for _in_wall_line."""
    from . import walltrace as _wt
    from shapely.strtree import STRtree
    ls = [tuple(map(float, sg[:4])) for sg in segs or ()
          if len(sg) > 4 and _wt.is_wall_layer(sg[4]) and not (len(sg) > 7 and sg[7])
          and math.hypot(sg[2] - sg[0], sg[3] - sg[1]) >= 5.0]
    boxes = [shapely.box(min(a[0], a[2]) - .01, min(a[1], a[3]) - .01, max(a[0], a[2]) + .01, max(a[1], a[3]) + .01)
             for a in ls]
    return ls, (STRtree(boxes) if boxes else None)


def _in_wall_line(pg, faces, tmax=None, share=None, tol=5.0):
    """A piece no thicker than tmax (COLUMN_IN_WALL_MAX_MM) with wall-layer
    faces along both its long sides, over `share` of each: part of a wall,
    not a column. faces: _wall_faces."""
    ls, tree = faces
    if tree is None:
        return False
    tmax = COLUMN_IN_WALL_MAX_MM if tmax is None else tmax
    share = COLUMN_IN_WALL_FACE_SHARE if share is None else share
    r = pg.minimum_rotated_rectangle
    if r.geom_type != "Polygon":
        return False
    xy = list(r.exterior.coords)[:4]
    l0 = math.hypot(xy[1][0] - xy[0][0], xy[1][1] - xy[0][1])
    l1 = math.hypot(xy[2][0] - xy[1][0], xy[2][1] - xy[1][1])
    if min(l0, l1) > tmax or min(l0, l1) <= 0:
        return False
    sides = ((xy[0], xy[1]), (xy[2], xy[3])) if l0 >= l1 else ((xy[1], xy[2]), (xy[3], xy[0]))
    for p, q in sides:
        L = math.hypot(q[0] - p[0], q[1] - p[1])
        ux, uy = (q[0] - p[0]) / L, (q[1] - p[1]) / L
        span = []
        for k in tree.query(shapely.LineString([p, q]).buffer(tol + 1.0)):
            a = ls[int(k)]
            sl = math.hypot(a[2] - a[0], a[3] - a[1])
            if abs((a[2] - a[0]) / sl * uy - (a[3] - a[1]) / sl * ux) > 0.02:
                continue                              # not along this side
            v0 = (a[0] - p[0]) * -uy + (a[1] - p[1]) * ux
            v1 = (a[2] - p[0]) * -uy + (a[3] - p[1]) * ux
            if abs(v0) > tol or abs(v1) > tol:
                continue                              # not on this side's line
            s0 = (a[0] - p[0]) * ux + (a[1] - p[1]) * uy
            s1 = (a[2] - p[0]) * ux + (a[3] - p[1]) * uy
            lo, hi = max(0.0, min(s0, s1)), min(L, max(s0, s1))
            if hi > lo:
                span.append((lo, hi))
        got, end = 0.0, 0.0
        for lo, hi in sorted(span):
            lo = max(lo, end)
            if hi > lo:
                got += hi - lo
                end = hi
        if got < share * L:
            return False
    return True


def _column_shapes(doc, msp, units, floors, rejected=None, segs=None, rcc_walls=True):
    """Columns on each floor. All the columns of a floor are one group - one
    kind of CAD object (closed polyline, circle, block, hatch) on one layer
    - standing on a grid (11 x 11, say). So every column-size object is
    collected, grouped by (object type, layer), and the group that lines up
    best on rows and columns of a grid is the floor's columns. A group on a
    column layer is taken too when no group forms a grid.
    {floor name: [(polygon, layer, {"grid", "type", "members", "xs", "ys"})]}

    A column is solid: a candidate whose outline (convex hull) is more than
    COLUMN_HOLLOW_RATIO times its filled area is a C or L shaped core wall.
    Those of the floor's column group are put in `rejected` ({floor name:
    [polygon]}) when it is given - they are walls (AI_WALL).

    A piece no thicker than COLUMN_IN_WALL_MAX_MM lying in a wall line
    (_in_wall_line: wall-layer faces along both its long sides) is part of
    that wall, never a column; segs: the floors' raw segments, as a list or
    {floor name: list}, to find those faces in.

    rcc_walls: an outline on a column layer too big or too long for a column
    and about a wall's thickness (2 x area / perimeter no more than
    RCC_WALL_MAX_MM, RCC_WALL_MIN_LEN_MM or longer) is a concrete wall drawn
    with the columns (Centaurus: the stair and lift core walls on A-COLUMN,
    38-47 m round, 300 mm thick): put in `rejected` - a wall (AI_WALL)."""
    from .dxfio import iter_entities, effective_layer
    import logging
    # Measuring a block's extent makes ezdxf copy it, and it warns about
    # every AutoCAD-only object it leaves behind - noise, not a problem.
    logging.getLogger("ezdxf").setLevel(logging.ERROR)
    plates = [(f["name"], shapely.Polygon([tuple(p) for p in f["polygon"]]).buffer(0)) for f in floors]
    groups = {f["name"]: collections.defaultdict(list) for f in floors}
    hollow = {f["name"]: collections.defaultdict(list) for f in floors}
    rcc = {f["name"]: [] for f in floors}
    block_box = {}

    def rcc_wall(pg, kind, layer):
        # a concrete wall on the column layer: long, a wall's thickness
        if not rcc_walls or kind == "CIRCLE" or kind.startswith("INSERT"):
            return
        if not any(k in layer.upper() for k in COLUMN_LAYER_WORDS):
            return
        per = pg.length
        if per <= 0 or pg.area <= 0:
            return
        thick, long_ = 2.0 * pg.area / per, per / 2.0
        if thick > RCC_WALL_MAX_MM or long_ < RCC_WALL_MIN_LEN_MM:
            return
        c = pg.representative_point()
        for fname, plate in plates:
            if plate.contains(c):
                rcc[fname].append(pg)
                return

    def place(pg, kind, layer):
        a = pg.area / 1e6
        sh_ = _shape(pg)                          # a turned column measures as itself, not its bounding box
        w, h = sh_["width_mm"], sh_["height_mm"]
        hollow_ = pg.convex_hull.area > COLUMN_HOLLOW_RATIO * pg.area
        if hollow_ and a > COLUMN_SQM[0] and rcc_walls:
            # a C, L or U on the column layer, a wall's thickness and a
            # wall's length, is a wall whatever layer the floor's columns
            # stand on (Centaurus S3: the 3.79 sq.m U round the fire lift,
            # 279 mm thick, was dropped as a hollow "column" because its
            # layer was not the grid's) - tested before its size
            n0 = sum(len(v) for v in rcc.values())
            rcc_wall(pg, kind, layer)
            if sum(len(v) for v in rcc.values()) > n0:
                return
        if not (COLUMN_SQM[0] <= a <= COLUMN_SQM[1]) or min(w, h) <= 0 or max(w, h) / min(w, h) > COLUMN_ASPECT_MAX:
            if a > COLUMN_SQM[0]:
                rcc_wall(pg, kind, layer)
            return
        c = pg.representative_point()
        for fname, plate in plates:
            if plate.contains(c):
                if hollow_:
                    hollow[fname][(kind, layer)].append(pg)     # a C / L wall
                else:
                    groups[fname][(kind, layer)].append(pg)
                return

    for e, xform, _depth in iter_entities(doc, msp, "LWPOLYLINE POLYLINE CIRCLE INSERT HATCH"):
        try:
            kind = e.dxftype()
            layer = effective_layer(e, xform) or ""
            lu = layer.upper()
            if dr._other_level_xref(lu) or _NOT_COLUMN_LAYER.search(lu):
                continue
            if kind == "CIRCLE":
                cx, cy = units.point_to_mm(*xform.apply(e.dxf.center.x, e.dxf.center.y))
                r = float(e.dxf.radius) * units.mm_per_unit * (xform.scale_avg or 1.0)
                place(shapely.Point(cx, cy).buffer(r, 16), kind, layer)
            elif kind == "INSERT":
                name = e.dxf.name
                if name not in block_box:
                    from ezdxf import bbox as ebbox
                    try:
                        ext = ebbox.extents(doc.blocks[name])
                        block_box[name] = (ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y) if ext.has_data else None
                    except Exception:                 # noqa: BLE001
                        block_box[name] = None
                bb = block_box[name]
                if not bb:
                    continue
                m = e.matrix44()
                pts = [m.transform((x, y, 0)) for x, y in ((bb[0], bb[1]), (bb[2], bb[1]), (bb[2], bb[3]), (bb[0], bb[3]))]
                pts = [units.point_to_mm(*xform.apply(p.x, p.y)) for p in pts]
                place(shapely.Polygon(pts).buffer(0), kind + ":" + name, layer)
            elif kind == "HATCH":
                from ezdxf import path as ezpath
                rings = [[units.point_to_mm(*xform.apply(v.x, v.y)) for v in p.flattening(5.0)]
                         for p in ezpath.from_hatch(e)]
                rings = [r for r in rings if len(r) >= 3]
                if rings:
                    # every outer ring of the hatch is a shape of its own
                    # (one hatch entity draws every column of a core, or
                    # several shear walls - only its largest ring was read,
                    # and Centaurus lost the corner block and the band under
                    # the lift); a ring inside a larger ring is a hole
                    polys_ = []
                    for r_ in rings:
                        try:
                            q_ = shapely.Polygon(r_).buffer(0)
                        except Exception:             # noqa: BLE001
                            continue
                        if not q_.is_empty and q_.area > 0:
                            polys_.append(q_)
                    polys_.sort(key=lambda q_: -q_.area)
                    outers_ = []
                    for q_ in polys_:
                        pt_ = q_.representative_point()
                        if any(o_.contains(pt_) for o_ in outers_):
                            continue                  # a hole, or drawn twice
                        outers_.append(q_)
                    for q_ in outers_:
                        place(q_, kind, layer)
            else:
                if not dr._is_closed(e):
                    continue
                raw = [xform.apply(p[0], p[1]) for p in e.get_points("xy")] if kind == "LWPOLYLINE" else \
                    [xform.apply(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
                pts = [units.point_to_mm(x, y) for x, y in raw]
                if len(pts) >= 3:
                    place(shapely.Polygon(pts).buffer(0), kind, layer)
        except Exception:                             # noqa: BLE001
            continue

    plate_of = dict(plates)
    out = {}
    for fname, by in groups.items():
        best, best_score = None, (0, 0.0)
        edge = plate_of[fname].exterior
        for key, pgs in by.items():
            pgs = _dedupe_shapes(pgs)
            if len(pgs) < COLUMN_GRID_MIN:
                continue
            g = _grid_fit(pgs)
            # columns stand inside the floor too, not only along its edge
            inner = sum(1 for p in pgs if edge.distance(p.centroid) > COLUMN_INNER_MM) / float(len(pgs))
            score = g["aligned"] * g["density"] * len(pgs) * (1.5 if any(
                k in key[1].upper() for k in COLUMN_LAYER_WORDS) else 1.0)
            named = any(k in key[1].upper() for k in COLUMN_LAYER_WORDS)
            if (g["aligned"] >= COLUMN_GRID_ALIGNED and g["density"] >= COLUMN_GRID_DENSITY
                    and inner >= COLUMN_INNER_SHARE and g["xs"] >= 2 and g["ys"] >= 2):
                # a grid on a column layer is taken over any other grid
                rank = (1 if named else 0, score)
                if best is None or rank > best_score:
                    best, best_score = (key, pgs, g), rank
        chosen = []
        if best is not None:
            (kind, layer), pgs, g = best
            how = {"grid": True, "type": kind.split(":")[0], "members": len(pgs), "xs": g["xs"], "ys": g["ys"]}
            chosen = [(pg, layer, how) for pg in pgs]
            if rejected is not None:
                # the C / L shapes on the columns' layer, whatever they are drawn as
                # (one per place - a C's centre lies outside it, so by its box)
                seen, keep = set(), []
                for p in sorted((p for (_k, lay), lst in hollow[fname].items() if lay == layer for p in lst),
                                key=lambda p: -p.area):
                    k = tuple(int(round(v / 100.0)) for v in p.bounds)
                    if k not in seen:
                        seen.add(k)
                        keep.append(p)
                rejected[fname] = keep
        else:
            for (kind, layer), pgs in by.items():
                if any(k in layer.upper() for k in COLUMN_LAYER_WORDS) and kind.split(":")[0] in ("LWPOLYLINE", "POLYLINE", "CIRCLE"):
                    chosen.extend((pg, layer, {}) for pg in _dedupe_shapes(pgs))
        if chosen and segs is not None and COLUMN_IN_WALL_MAX_MM > 0:
            fs = segs.get(fname) if isinstance(segs, dict) else segs
            if fs:
                pl = plate_of[fname].buffer(2000.0)
                bx = pl.bounds
                faces = _wall_faces([sg for sg in fs if bx[0] <= sg[0] <= bx[2] and bx[1] <= sg[1] <= bx[3]])
                chosen = [c for c in chosen if not _in_wall_line(c[0], faces)]
        out[fname] = chosen
        if rejected is not None and rcc.get(fname):
            # the concrete walls on the column layer, once each (its outline
            # and its hatch are the same wall)
            have = list(rejected.get(fname) or [])
            for p in sorted(rcc[fname], key=lambda p: -p.area):
                if any(p.intersection(o).area >= 0.8 * min(p.area, o.area) for o in have):
                    continue
                have.append(p)
            rejected[fname] = have
    return out


def _dedupe_shapes(pgs, near=300.0):
    """One shape per place: a column drawn as several nested outlines (its
    finish and its structure) is one column - the outermost is kept."""
    out = []
    for pg in sorted(pgs, key=lambda p: -p.area):
        c = pg.centroid
        if any(abs(c.x - o.centroid.x) <= near and abs(c.y - o.centroid.y) <= near
               and (o.contains(c) or pg.contains(o.centroid)) for o in out):
            continue
        out.append(pg)
    return out


def _grid_fit(pgs, tol=150.0):
    """How well these shapes stand on a grid: the share of them whose
    centre shares a grid line with another in x AND in y, and how many grid
    lines each way."""
    if not pgs:
        return {"aligned": 0.0, "xs": 0, "ys": 0}
    cs = [(p.centroid.x, p.centroid.y) for p in pgs]

    def lines(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        groups, cur = [], [order[0]]
        for i in order[1:]:
            if vals[i] - vals[cur[-1]] <= tol:
                cur.append(i)
            else:
                groups.append(cur)
                cur = [i]
        groups.append(cur)
        on = set(i for g in groups if len(g) >= 2 for i in g)
        return on, sum(1 for g in groups if len(g) >= 2)

    onx, nx = lines([c[0] for c in cs])
    ony, ny = lines([c[1] for c in cs])
    both = len(onx & ony)
    # A structural grid is dense: most crossings of its lines hold a
    # column. A run of facade fins or section marks along the edges lines
    # up too, but on 41 x 41 lines with 159 members.
    density = len(cs) / float(max(1, nx * ny))
    return {"aligned": both / float(len(cs)), "xs": nx, "ys": ny, "density": min(1.0, density)}


def _accepts_xform():
    """dr._polyline_points takes no transform; columns inside blocks are
    placed by hand above."""
    return False


def identified_rules(profile):
    """{rule: class reported} from the profile's "identify" section, or
    None when it has none (every finding is reported)."""
    ident = (profile or {}).get("identify")
    if not isinstance(ident, dict):
        return None
    out = {}
    for cls, spec in ident.items():
        if cls.startswith("_") or not isinstance(spec, dict):
            continue
        for rule in spec.get("rules") or []:
            out[str(rule)] = cls
    return out


def _keep_identified(ents_by_floor, profile, diag):
    """Report only what geo_profiles.json "identify" lists - for now shafts
    (an X within an enclosure) and staircases. A finding of another rule
    was still worked out (it can claim a shape another stage would misread)
    but is not reported. An X-marked lift or void is reported as the shaft
    it is by that definition."""
    rules = identified_rules(profile)
    if rules is None:
        return
    for name in list(ents_by_floor):
        kept = []
        for e in ents_by_floor[name]:
            cls = rules.get(e.rule)
            if cls is None:
                diag["not reported (geo_profiles.json identify): " + e.cls.lower()] += 1
                continue
            if e.cls != cls:
                e.ev(True, "Reported as {0}: an X mark within an enclosure (was read as {1})".format(
                    cls.lower(), e.cls.lower().replace("_", " ")))
                e.cls = cls
                e.sub = ""
            kept.append(e)
        ents_by_floor[name] = kept


def run_to_files(dxf_path, floors, unit_override, geo_json, geo_dxf, families):
    """The geometry engine as Step 4 runs it, writing its two outputs: the
    findings (geo_json) and the geometry DXF with the engine's defaults
    (geo_dxf). Run in a process of its own when Step 2's Proceed starts
    it, so it works at the same time as the text side. Returns True."""
    import json as _json
    loaded = dr.load_document(dxf_path, unit_override)
    result = run(dxf_path, floors, unit_override, loaded=loaded)
    tmp = geo_dxf + ".part.dxf"
    write_dxf(loaded[0], loaded[2], build_final(result, None, families), tmp)
    os.replace(tmp, geo_dxf)
    with open(geo_json + ".part", "w", encoding="utf-8") as fh:
        _json.dump(result, fh, ensure_ascii=False)
    os.replace(geo_json + ".part", geo_json)
    return True


_NOT_REAL_WALL = re.compile(r"^\d+(\.\d+)+\s|FINI|FINISH|FLOOR|THRESH|SILL|SKIRT")
# What is drawn in a lift's door opening - the landing door, its frame, an
# elevation of the lift car ("A-ELEV 2") - is not the wall: the opening in
# the structure is the door.
_LIFT_DOOR_LAYER = re.compile(r"ELEV|DOOR|\bDR\b|LANDING|LIFT[ _-]?FR|ARCHITRAVE")


def _real_walls(walls, seg_rows, also_not=None):
    """Wall lines that are drawn walls: the wall set less lines on the
    area-plan overlay ("1.3 SALEABLE-TSIIC-LIFT SHAFT") and finish lines
    ("A3-floor fini") - both run straight across door openings."""
    if not walls:
        return []
    if not seg_rows:
        return list(walls)
    layer = {}
    for sg in seg_rows:
        k = tuple(round(v) for v in sg[:4])
        layer[k] = sg[4] or ""
        layer[(k[2], k[3], k[0], k[1])] = sg[4] or ""
    out = []
    for w in walls:
        lu = (layer.get(tuple(round(v) for v in w[:4])) or "").upper()
        if lu and (_NOT_REAL_WALL.search(lu) or (also_not is not None and also_not.search(lu))):
            continue
        out.append(tuple(w[:4]))
    return out


def _lift_has_door(lift_pg, space_pg, door_lines, swings, walls, R):
    """A lift opening onto this space: a door line or swing at the lift, or
    an opening at least lift_door_min_mm wide in its wall on the side
    facing the space."""
    reach = float(R.get("lift_lobby_side_reach_mm", 600.0))
    zone = lift_pg.buffer(reach)
    for d in door_lines:
        if zone.contains(shapely.Point((d[0] + d[2]) / 2.0, (d[1] + d[3]) / 2.0)):
            return True
    for sw in swings or ():
        if zone.contains(shapely.Point(sw[0], sw[1])):
            return True
    b = lift_pg.bounds
    edges = [((b[0], b[1]), (b[2], b[1])), ((b[2], b[1]), (b[2], b[3])),
             ((b[2], b[3]), (b[0], b[3])), ((b[0], b[3]), (b[0], b[1]))]
    # the side facing the space: the one whose middle is nearest it (a
    # space running past the lift's corner is as near its other side)
    (ax, ay), (bx, by) = min(edges, key=lambda ed: shapely.Point(
        (ed[0][0] + ed[1][0]) / 2.0, (ed[0][1] + ed[1][1]) / 2.0).distance(space_pg))
    L = math.hypot(bx - ax, by - ay)
    if L <= 0:
        return False
    ux, uy = (bx - ax) / L, (by - ay) / L
    band = float(R.get("lift_door_wall_band_mm", 450.0))
    cov = []
    for w in walls or ():
        wl = math.hypot(w[2] - w[0], w[3] - w[1])
        if wl <= 0 or abs(ux * (w[2] - w[0]) / wl + uy * (w[3] - w[1]) / wl) < math.cos(math.radians(5)):
            continue
        if (abs((w[0] - ax) * uy - (w[1] - ay) * ux) > band
                or abs((w[2] - ax) * uy - (w[3] - ay) * ux) > band):
            continue
        t1 = (w[0] - ax) * ux + (w[1] - ay) * uy
        t2 = (w[2] - ax) * ux + (w[3] - ay) * uy
        lo, hi = max(0.0, min(t1, t2)), min(L, max(t1, t2))
        if hi > lo:
            cov.append((lo, hi))
    cov.sort()
    gap, cur = 0.0, 0.0
    for lo, hi in cov:
        gap = max(gap, lo - cur)
        cur = max(cur, hi)
    gap = max(gap, L - cur)
    return gap >= float(R.get("lift_door_min_mm", 700.0)) and gap < L - 1.0


def _iou(a, b):
    try:
        inter = a.intersection(b).area
        if inter <= 0:
            return 0.0
        return inter / a.union(b).area
    except Exception:                                 # noqa: BLE001
        return 0.0


def _door_near(pg, swings, dist):
    b = pg.bounds
    for s in swings or ():
        x, y = s[0], s[1]
        if b[0] - dist <= x <= b[2] + dist and b[1] - dist <= y <= b[3] + dist:
            if pg.distance(shapely.Point(x, y)) <= dist:
                return True
    return False


# ------------------------------------------------- families and repetition ---
def _families_and_repeats(ents_by_floor, floors, R):
    """Group findings of one kind and size (100 mm) into geometry families,
    and count the floors where the same family stands in the same place."""
    fam = collections.defaultdict(list)
    for fl, ents in ents_by_floor.items():
        for e in ents:
            if e.cls in ("ROOM", "UNKNOWN_GEOMETRY"):
                key = (e.cls, int(round(e.features["area_sqm"])))
            else:
                key = (e.cls, int(round(e.features["width_mm"], -2)), int(round(e.features["height_mm"], -2)))
            fam[key].append(e)
    for key, members in fam.items():
        if key[0] in ("ROOM", "UNKNOWN_GEOMETRY"):
            fid = "{0}~{1}sqm".format(key[0], key[1])
            title = "{0} of about {1} sq.m".format(key[0].title().replace("_", " "), key[1])
        else:
            fid = "{0}-{1}x{2}".format(key[0], key[1], key[2])
            title = "{0} {1} x {2} mm".format(key[0].title().replace("_", " "), key[1], key[2])
        for e in members:
            e.family = {"id": fid, "title": title, "members": len(members)}
            if key[0] not in ("ROOM", "UNKNOWN_GEOMETRY") and len(members) >= 3:
                e.ev(True, "Repeated geometry: {0} findings of this exact size ({1})".format(len(members), title))

    if len(floors) < 2:
        return
    origin = {}
    for f in floors:
        xs = [p[0] for p in f["polygon"]]
        ys = [p[1] for p in f["polygon"]]
        origin[f["name"]] = (min(xs), min(ys))
    tol = R["recur_tol_mm"]
    by_floor = {}
    for fl, ents in ents_by_floor.items():
        ox, oy = origin.get(fl, (0, 0))
        by_floor[fl] = [(e.family["id"], e.features["centroid"][0] - ox, e.features["centroid"][1] - oy)
                        for e in ents if e.cls not in ("ROOM", "UNKNOWN_GEOMETRY")]
    others = len(ents_by_floor) - 1
    for fl, ents in ents_by_floor.items():
        ox, oy = origin.get(fl, (0, 0))
        for e in ents:
            if e.cls in ("ROOM", "UNKNOWN_GEOMETRY"):
                continue
            x, y = e.features["centroid"][0] - ox, e.features["centroid"][1] - oy
            k = 0
            for g, rows in by_floor.items():
                if g == fl:
                    continue
                if any(fid == e.family["id"] and abs(rx - x) <= tol and abs(ry - y) <= tol for fid, rx, ry in rows):
                    k += 1
            e.extra["repeats"] = k
            if k:
                e.ev(True, "Repeats at the same place on {0} of {1} other floors".format(k, others))


# --------------------------------------------------------------- output ---
def _finish(ents_by_floor, profile, floors, diag):
    floors_out = {}
    counts = collections.Counter()
    seq = 0
    fams = {}
    for f in floors:
        name = f["name"]
        ents = ents_by_floor.get(name, [])
        # reading order: top to bottom, left to right
        ents.sort(key=lambda e: (CLASSES.index(e.cls), -round(e.features["centroid"][1] / 2000.0), e.features["centroid"][0]))
        per = collections.Counter()
        rows = []
        for e in ents:
            seq += 1
            per[e.cls] += 1
            counts[e.cls] += 1
            if e.cls == "ROOM":
                label = "Room {0}".format(per[e.cls])      # for the user to name
            else:
                label = "{0}_{1:02d}".format(LABEL_PREFIX.get(e.cls, e.cls), per[e.cls])
            if e.extra.get("name"):
                label = e.extra["name"]                   # from its overlay ("FP SHAFT")
            if e.cls == "UNKNOWN_GEOMETRY":
                conf, how = None, "not classified - see the reason"
            else:
                conf, how = confidence(profile, e.rule)
            pts = [[round(x), round(y)] for x, y in list(e.pg.exterior.coords)[:-1]]
            rows.append({
                "id": "GEO-{0:04d}".format(seq), "label": label, "cls": e.cls, "sub": e.sub,
                "floor": name, "rule": e.rule,
                "confidence": None if conf is None else round(conf, 3),
                "confidence_note": how, "evidence": e.evidence, "reason": e.reason,
                "family": e.family, "features": e.features,
                "repeats": e.extra.get("repeats"), "bank": e.extra.get("bank"),
                "poly": pts, "area_sqm": e.features["area_sqm"],
            })
            if e.family:
                fams.setdefault(e.family["id"], {"id": e.family["id"], "title": e.family["title"],
                                                 "cls": e.cls, "members": 0})
                fams[e.family["id"]]["members"] += 1
        # what each finding stands next to (within 0.5 m), by label
        if rows:
            pgs = [e.pg for e in ents]
            tree = shapely.STRtree(pgs)
            for i, e in enumerate(ents):
                near = [int(k) for k in tree.query(e.pg.buffer(500.0)) if int(k) != i]
                near.sort(key=lambda k: e.pg.distance(pgs[k]))
                rows[i]["near"] = [rows[k]["label"] for k in near[:4]]
        floors_out[name] = rows
    return {
        "engine": "geometry", "version": 1,
        "floors": floors_out,
        "summary": {c: counts.get(c, 0) for c in CLASSES},
        "classes": [{"cls": c, "layer": LAYER_OF[c], "rgb": RGB[c]} for c in CLASSES],
        "families": sorted(fams.values(), key=lambda f: -f["members"]),
        "diagnostics": dict(diag),
        "profile_source": profile.get("_source", ""),
        "rules": {k: v for k, v in (profile.get("rules") or {}).items()},
    }


def write_dxf(doc, units, result, out_path):
    """The source drawing plus one layer per class: a closed boundary and a
    label (generated name, id and confidence) for every finding. Original
    layers are not touched."""
    for c in CLASSES:
        name = LAYER_OF[c]
        if name not in doc.layers:
            doc.layers.add(name, color=ACI[c])
    msp = doc.modelspace()
    f = units.from_mm
    for rows in result["floors"].values():
        for r in rows:
            layer = LAYER_OF.get(r["cls"]) or LAYER_OF["UNKNOWN_GEOMETRY"]
            if r.get("category_layer"):
                # a category set by the user: that family's layer
                layer = dr.LN.family_layer(r["category_layer"])
                if layer not in doc.layers:
                    doc.layers.add(layer, color=ACI.get(r["cls"], 7))
            elif layer not in doc.layers:
                doc.layers.add(layer, color=7)
            pts = [(f(x), f(y)) for x, y in r["poly"]]
            if len(pts) < 3:
                continue
            msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer, "color": 256})
            w, h = r["features"]["width_mm"], r["features"]["height_mm"]
            ht = max(120.0, min(500.0, 0.14 * min(w, h)))
            conf = "" if r["confidence"] is None else " {0:.0f}%".format(100 * r["confidence"])
            if r.get("reviewed"):
                conf = " (edited)"
            cx, cy = r["features"]["centroid"]
            msp.add_mtext("{0}\\P{1}{2}".format(r["label"], r["id"], conf), dxfattribs={
                "layer": layer, "char_height": f(ht), "attachment_point": 5,
                "insert": (f(cx), f(cy))})
        # a column standing in a room is a hole in it (deducted from its area)
        for _r, holes in _column_holes(rows):
            if dr.LN.VOID not in doc.layers:
                doc.layers.add(dr.LN.VOID, color=8)
            for h in holes:
                msp.add_lwpolyline([(f(x), f(y)) for x, y in list(h.exterior.coords)[:-1]], close=True,
                                   dxfattribs={"layer": dr.LN.VOID, "color": 256})
    for fl, by_class in (result.get("walls") or {}).items():
        dr.draw_walls(doc, msp, units, by_class)
    dr.draw_hanging_walls(doc, msp, units, result.get("hanging_walls") or [])
    if dr.DRAW_DOORS:
        # primary / secondary, told by the findings on their two sides and
        # the common floor round them (elements.door_levels)
        for fl, quads in (result.get("doors") or {}).items():
            rows_fl = (result.get("floors") or {}).get(fl) or []
            solid_ = door_solid((result.get("walls") or {}).get(fl), (result.get("window_openings") or {}).get(fl),
                                (result.get("glass") or {}).get(fl), rows_fl)
            # each door's line on its room's inner wall trace (elements.door_trace)
            dr.draw_doors_by_level(doc, msp, units, quads, door_rooms(rows_fl),
                                   plate=(result.get("plates") or {}).get(fl), solid=solid_, trace_solid=solid_)
        # the doubtful ones (rule doors_need_evidence): on AI_REVIEW
        for fl, items in (result.get("door_review") or {}).items():
            dr.draw_door_review(doc, msp, units, items)
    for fl, quads in (result.get("window_openings") or {}).items():
        dr.draw_window_quads(doc, msp, units, quads)
    for fl, rings in (result.get("glass") or {}).items():
        dr.draw_glass(doc, msp, units, rings)
    dr.set_insunits(doc, units)
    doc.saveas(out_path)



# --------------------------------------------------------------- reviews ---
# A category picked by the user, as the engine class it is drawn and
# compared under (the rest are rooms of that kind).
# (Shaft, Lift, Window, Cutout, Column and Void are Step 3's: no finding
# here is ever given one of them - STEP3_FAMILIES)
CLASS_OF_FAMILY = {"Lift Lobby": "LIFT_LOBBY", "Machine Room": "MACHINE_ROOM", "Service Lobby": "SERVICE_LOBBY",
                   "Staircase": "STAIRCASE", "Toilet": "TOILET",
                   "Unknown": "UNKNOWN_GEOMETRY"}


def step3_free(result):
    """A result (or the Step 5 output built from one) with nothing of the
    six Step 3 decides: findings of STEP3_CLASSES (a result written before
    2026-10-16) and window openings dropped; a finding the user gave one of
    the six families in Step 4 keeps the engine's own. In place; returns
    the result."""
    if not isinstance(result, dict):
        return result
    gone = 0
    for fl, rows in list((result.get("floors") or {}).items()):
        kept = []
        for r in rows or ():
            if str(r.get("cls") or "").upper() in STEP3_CLASSES and not r.get("category_layer"):
                gone += 1
                continue
            if r.get("family") in STEP3_FAMILIES or r.get("category") in STEP3_FAMILIES:
                gone += 1
                continue
            kept.append(r)
        result["floors"][fl] = kept
    if result.get("window_openings"):
        result["window_openings"] = {}
    if gone:
        result["step3_dropped"] = result.get("step3_dropped", 0) + gone
    return result


def apply_reviews(result, reviews, families):
    """The engine's result with the user's own categories and names on top.

    reviews: {finding id: {"category", "name", "centroid"}}. The engine's
    answer is kept under "machine" so both stay visible; the engine itself
    is not changed by a review. A review whose centroid no longer matches
    the finding (the drawing was analysed again and changed) is ignored."""
    import copy
    res = step3_free(copy.deepcopy(result))
    # family layers read e.g. "MEP_BOUNDARY"; the text engine's class is "MEP"
    layer_of = {f["name"]: dr.LN.room_key(dr.LN.family_layer(f["layer"] or "ROOM")) for f in families}
    counts = collections.Counter()
    n = 0
    for rows in res["floors"].values():
        for r in rows:
            rv = (reviews or {}).get(r["id"])
            if rv and rv.get("centroid"):
                c = r["features"]["centroid"]
                if math.hypot(c[0] - rv["centroid"][0], c[1] - rv["centroid"][1]) > 300:
                    rv = None
            if rv and (rv.get("category") or rv.get("name")):
                n += 1
                r["machine"] = {"cls": r["cls"], "sub": r.get("sub", ""), "label": r["label"],
                                "confidence": r["confidence"], "rule": r["rule"]}
                cat, nm = rv.get("category") or "", (rv.get("name") or "").strip()
                if cat in STEP3_FAMILIES:
                    cat = ""                          # Step 3's, never a geometry finding's
                if cat:
                    r["category"] = cat
                    r["category_layer"] = layer_of.get(cat, "ROOM")
                    r["cls"] = CLASS_OF_FAMILY.get(cat, "ROOM")
                    r["sub"] = ""
                if nm:
                    r["label"] = nm
                r["reviewed"] = True
                r["confidence"] = None
                r["confidence_note"] = "set by you"
                r["evidence"] = [{"ok": True, "metadata": False, "user": True,
                                  "text": "Set by you" + (": " + cat if cat else "") +
                                          (" - named '" + nm + "'" if nm else "")}] + r["evidence"]
            counts[r["cls"]] += 1
    res["summary"] = {c: counts.get(c, 0) for c in CLASSES}
    res["reviewed"] = n
    return res


# ---------------------------------------------------- the user's output ---
# The family a finding starts with in Step 5 (the app's families, plus
# "Room" for an enclosed room the geometry cannot name).
FAMILY_OF_CLASS = {"LIFT_LOBBY": "Lift Lobby", "MACHINE_ROOM": "Machine Room",
                   "SERVICE_LOBBY": "Service Lobby", "STAIRCASE": "Staircase", "TOILET": "Toilet",
                   "ROOM": "Room", "UNKNOWN_GEOMETRY": "Unknown"}


def family_of(row):
    return FAMILY_OF_CLASS.get(row.get("cls"), "Unknown")


def build_final(result, decisions, families):
    """The geometry output the user settled on in Step 5: every finding
    they kept (ticked), with the family and name they gave it, plus the
    shapes they traced, drew or merged. decisions: {floor: {id: {"include",
    "family", "label", ["poly", "kind"]}}}; a finding not mentioned keeps
    its default (drawn unless its family is Unknown). None: all defaults.
    A shaft, lift, window, cut-out, column or void is never drawn from the
    geometry (STEP3_FAMILIES: Step 3's): a decision giving a finding or a
    drawn shape one of those families is not taken."""
    import copy
    result = step3_free(copy.deepcopy(result))
    layer_of = {f["name"]: dr.LN.room_key(dr.LN.family_layer(f["layer"] or "ROOM")) for f in families}
    out = {"floors": {}}
    refused = 0
    for fl, rows in result["floors"].items():
        by = {r["id"]: r for r in rows}
        dec = (decisions or {}).get(fl) or {}
        todo = []
        for r in rows:
            d = dec.get(r["id"])
            if d is None:
                # The category the user gave it in Step 4, else the engine's.
                fam = r.get("category") or family_of(r)
                # Columns are drawn on AI_COLUMN - structure, never a room or an area.
                d = {"include": fam not in ("Unknown",), "family": fam, "label": r["label"]}
            todo.append((r, d))
        for iid, d in dec.items():
            if iid in by or not d.get("poly"):
                continue
            todo.append((None, dict(d, id=iid)))     # traced / drawn / merged by the user
        final = []
        for r, d in todo:
            if not d.get("include"):
                continue
            pg = _poly(d.get("poly") or (r or {}).get("poly") or [])
            if pg is None:
                continue
            fam = d.get("family") or (family_of(r) if r else "Room")
            if fam == "Ignore":
                continue                              # no boundary, nothing further
            if fam in STEP3_FAMILIES:
                refused += 1
                continue                              # Step 3's (the X sequence): never from here
            label = (str(d.get("label") or "").strip() or (r or {}).get("label") or fam)
            f = _shape(pg)
            cls = "ROOM" if fam == "Room" else CLASS_OF_FAMILY.get(fam, "ROOM")
            changed = r is None or fam != family_of(r) or label != r["label"]
            final.append({
                "id": r["id"] if r else d["id"], "floor": fl, "label": label, "family": fam,
                "cls": cls, "sub": "",
                "category": "" if fam in ("Room", "Unknown", "Window", "Cutout", "Column") else fam,
                "category_layer": None if fam in ("Room", "Unknown", "Window", "Cutout", "Column") else layer_of.get(fam, "ROOM"),
                "poly": [[round(x), round(y)] for x, y in list(pg.exterior.coords)[:-1]],
                "features": f, "area_sqm": f["area_sqm"],
                "confidence": (r or {}).get("confidence"),
                "engine": {"cls": r["cls"], "label": r["label"], "rule": r["rule"]} if r else None,
                "source": "engine" if r else (d.get("kind") or "drawn"),
                "reviewed": bool(changed),
            })
        out["floors"][fl] = final
    counts = collections.Counter(r["family"] for rows in out["floors"].values() for r in rows)
    out["summary"] = dict(counts)
    out["step3_refused"] = refused
    out["walls"] = result.get("walls") or {}
    out["walls_summary"] = result.get("walls_summary") or {}
    out["hanging_walls"] = result.get("hanging_walls") or []
    out["doors"] = result.get("doors") or {}
    out["door_review"] = result.get("door_review") or {}
    out["plates"] = result.get("plates") or {}
    out["window_openings"] = result.get("window_openings") or {}
    out["glass"] = result.get("glass") or {}
    return out
