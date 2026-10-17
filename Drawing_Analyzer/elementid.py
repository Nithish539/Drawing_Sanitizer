# -*- coding: utf-8 -*-
"""The element identification engine: every door and every circulation
space of a floor classified again, with a confidence score, from the
evidence drawn round it - and the six elements Step 3 decides recorded as
it decided them.

THE SIX STEP 3 DECIDES (2026-10-16, final): Shaft, Lift, Window, Cutout,
Column and Void. Every X mark on the floor is decided once, by the X
sequence of the floor pass (xplace: 1 every X a void, 2 shaft, 3 lift,
4 window, 5 cut-out, 6 column, 7 void), and what Step 3 draws in those six
classes without an X (a lift traced from its label, a shaft named so) is
Step 3's too. The engine does not score them again: each is a fixed record
(score 100, action "Fixed - Step 3", the X sequence's step and reason in
its notes, read from the DRAWING_ANALYZER_X tag dxf_reader stores on what it
draws), never marked for review, never disputed.

GROUP A - doors: every door the walls give, scored as a door (a window or a
cut-out is Step 3's). Windows and cut-outs are listed in group A as fixed
records.
GROUP B - Staircase (tier 1), Lift Lobby, Service Lobby, Lift Machine Room
(tier 2, only once the lift / staircase they stand beside scores 50 or
more - a Step 3 lift counts 100). Shafts, lifts and voids are listed in
group B as fixed records; group C holds the columns the X sequence found
(a column's cross).

Each class an element could be is scored 0-100: the indicators found add
their weights, and the key combinations of the specification set the band
the total may take. Negative rules reject a class outright (a space with a
door is never a lift machine room). Classes within 15 points of each other
are settled by the tie-break rules; two classes both under 50 leave the
element Unresolved.

Bands and actions (CONFIG element_engine):
    80-100 High, 50-79 Medium  -> Auto-classify
    20-49  Low                 -> "possible <class>", Needs review
    0-19   Unresolved          -> Hold - unresolved

The engine never changes an area or a class drawn. Its records (the
specification's JSON, one per element, plus where it is and how it is
drawn) go into the run's stats ("elements"), onto each element as XDATA
(DRAWING_ANALYZER_ELEMENT), and the elements to look at - Low, Unresolved,
or a door / circulation space classified otherwise than drawn - are
outlined on AI_REVIEW with a note.
"""
from __future__ import annotations

import collections
import math
import re

from .config import CONFIG

_EE = CONFIG.get("element_engine") or {}

ENABLED = bool(_EE.get("enabled", True))
AUTO_MIN = float(_EE.get("auto_min", 50))
HIGH_MIN = float(_EE.get("high_min", 80))
REVIEW_MIN = float(_EE.get("review_min", 20))
TIE_MARGIN = float(_EE.get("tie_margin", 15))
# An X in a wall is a window only on clear evidence - glazing, a window's
# frame, or a window tag (W-1) beside it; with none it is a cut-out (2026-10-16: "if it
# cannot be determined clearly, assume it to be a cutout"). Off: an X in a
# solid outline in a wall is a window, as before.
REVIEW_LAYER = str(_EE.get("review_layer", "AI_REVIEW"))
REVIEW_ACI = int(_EE.get("review_color", 6))
REVIEW_TEXT_MM = float(_EE.get("review_text_mm", 120.0))
REVIEW_DISPUTED = bool(_EE.get("review_disputed", True))
NEAR_MM = float(_EE.get("near_mm", 400.0))

ELEMENT_APPID = "DRAWING_ANALYZER_ELEMENT"

DOOR, WINDOW, CUTOUT = "Door", "Window", "Cutout"
SHAFT, LIFT, STAIR, VOID = "Shaft", "Lift", "Staircase", "Void"
LIFT_LOBBY, SERVICE_LOBBY, MACHINE_ROOM = "Lift Lobby", "Service Lobby", "Lift Machine Room"
COLUMN = "Column"
UNRESOLVED = "Unresolved"
GROUP_A = (DOOR, WINDOW, CUTOUT)
TIER1 = (SHAFT, LIFT, STAIR, VOID)
TIER2 = (LIFT_LOBBY, SERVICE_LOBBY, MACHINE_ROOM)
CLASSES = GROUP_A + TIER1 + TIER2 + (COLUMN,)
# The six Step 3 decides - the X sequence for every X mark - recorded as
# drawn, never scored (see the note at the top).
STEP3_CLASSES = (SHAFT, LIFT, WINDOW, CUTOUT, COLUMN, VOID)
X_CLASS = {"shaft": SHAFT, "lift": LIFT, "window": WINDOW, "cutout": CUTOUT, "column": COLUMN, "void": VOID}
X_STEP_NAME = {1: "every X a void", 2: "shaft rules", 3: "lift rules", 4: "window rules",
               5: "cut-out rules", 6: "column rules", 7: "what is left: a void"}
FIXED_ACTION = "Fixed - Step 3"
TIER_OF = {SHAFT: "1", LIFT: "1", STAIR: "1", VOID: "1b",
           LIFT_LOBBY: "2", SERVICE_LOBBY: "2", MACHINE_ROOM: "2"}

# the family a space is drawn in -> the class it is drawn as
FAMILY_CLASS = {"Shaft": SHAFT, "Lift": LIFT, "Staircase": STAIR, "Void": VOID,
                "Lift Lobby": LIFT_LOBBY, "Service Lobby": SERVICE_LOBBY,
                "Machine Room": MACHINE_ROOM}
CIRCULATION_RE = re.compile(r"\bLOBBY\b|LOBBIES|CORRIDOR|PASSAGE|FOYER|\bLANDING\b|\bCIRC", re.I)


def _rx(key, default):
    try:
        return re.compile(str(_EE.get(key) or default), re.I)
    except re.error:
        return re.compile(default, re.I)


SERVICE_TEXT_RE = _rx("service_text_regex",
                      r"SHAFT|\bDUCT|RISER\b(?!\s*\d)|PLUMB|ELEC|\bELE\b|\bEL\b|\bPHE\b|"
                      r"\bCHW\b|AHU\s*PIPE|EXHAUST|TOI\.?\s*EX|\bPRE\b|\bPRE\.|\bPREZ|PRESS(?!.*\bROOM\b)|PRESSURI|"
                      r"\bPR\b|\bFAPA\b|\bTEN\b|\bCOMM\b|\bFHC\b|\bFHR\b|\bFP\b|\bBMS\b|\bELV\b|\bDG\b|\bVRV\b|"
                      r"\bSTP\b|\bB?RWP\b|\bPL\b|CUT\s*-?\s*OUT|\bSH\b|\bLCS\b|\bVENT\b|DOWN\s*TAKE")
LIFT_TEXT_RE = _rx("lift_text_regex",
                   r"\bLIFTS?\b|ELEVATOR|\bPAX\b|P\.\s*LIFT|F-?LIFT|\bL\d{1,2}\b|\bLIFT\s*WELL")
LIFT_NOT_RE = _rx("lift_text_not_regex",
                  r"\bPRE\b|\bPRE\.|\bPRES+|PRESSURI|\bPREZ|LOBBY|MACHINE|\bM\.?\s*R\b|PANEL")
STAIR_TEXT_RE = _rx("stair_text_regex",
                    r"STAIR|STAIRCASE|STAIRWELL|\bST\s*-?\s*\d|\bSTC\b|FIRE\s*(ESCAPE|EXIT)|\bSTEPS\b")
STAIR_ANNOT_RE = _rx("stair_annotation_regex", r"^\s*(UP|DN|DOWN)\s*$|\bUP\b|\bDN\b|TREADS?|\bRISERS?\b|\bNOS\b")
MR_TEXT_RE = _rx("machine_room_regex", r"MACHINE\s*ROOM|\bM\.?\s*ROOM\b|\bLMR\b|\bMMR\b|\bM\.?\s*R\.?$|\bM\.R\b")
EQUIP_TEXT_RE = _rx("equipment_regex",
                    r"PANEL|CONTROLLER|MOTOR|MACHINE|\bDRIVE\b|\bDB\b|\bMCC\b|\bUPS\b|EQUIP|HOIST|GOVERNOR|\bLCP\b")
DOOR_TAG_RE = _rx("door_tag_regex", r"^(?:[A-Z]{0,3}D|DR)\s*-?\s*\d+[A-Z]?$|^DOOR\b")
WINDOW_TAG_RE = _rx("window_tag_regex", r"^(?:W|V|LV|FW|SW)\s*-?\s*\d+[A-Z]?$|^W$|^WINDOW|^LOUV|^VENTILATOR")
CUTOUT_TAG_RE = _rx("cutout_tag_regex", r"^C\s*-?\s*\d+[A-Z]?$|^C$|CUT\s*-?\s*OUT|^OPENING|^CO\s*-?\s*\d+$")

_LAYER_KINDS = (
    ("door", re.compile(r"DOOR|\bDR\b|SHUTTER|\bGATE")),
    ("glass", re.compile(r"GLASS|GLAZ|CURTAIN")),
    ("window", re.compile(r"WINDOW|\bWIN\b|LOUV|VENTILATOR|MULLION|\bSILL")),
    ("cutout", re.compile(r"CUT\s*-?\s*OUT|OPENING")),
    ("stair", re.compile(r"STAIR|STRS|\bSTEP|TREAD|\bSTR\b|ESCAL")),
    ("lift", re.compile(r"LIFT|ELEV|\bLFT")),
    ("wall", re.compile(r"WALL|\bMAS\b|MASON|BRICK|BLOCK|RCC|COLUMN|\bCOL\b|SLIP|CONC|STRUCT|SHEAR|PARTITION")),
    ("equip", re.compile(r"EQUIP|EQPM|PANEL|MECH|HVAC")),
)


def layer_kind(layer):
    """What a layer's name says a line is: door, glass, window, cutout,
    stair, lift, wall, equip or other (the XREF prefix dropped)."""
    t = re.split(r"[$|]", str(layer or ""))[-1].upper()
    t = re.sub(r"[_\-.]+", " ", t)
    for k, rx in _LAYER_KINDS:
        if rx.search(t):
            return k
    return "other"


def band(score):
    """High / Medium / Low / Unresolved for a 0-100 score."""
    s = float(score or 0)
    if s >= HIGH_MIN:
        return "High"
    if s >= AUTO_MIN:
        return "Medium"
    if s >= REVIEW_MIN:
        return "Low"
    return "Unresolved"


def action(score, classification=None):
    if classification == UNRESOLVED:
        return "Hold - unresolved"
    s = float(score or 0)
    if s >= AUTO_MIN:
        return "Auto-classify"
    if s >= REVIEW_MIN:
        return "Needs review"
    return "Hold - unresolved"


def _seg_key(x0, y0, x1, y1):
    a = (round(x0, 1), round(y0, 1))
    b = (round(x1, 1), round(y1, 1))
    return (a, b) if a <= b else (b, a)


def _norm(t):
    return " ".join(str(t or "").upper().split())


# --------------------------------------------------------------- evidence ---
class Evidence:
    """The drawing round one floor, indexed: its lines (with layer, whether
    on a door layer, whether dashed), its door swings and its texts.

    segs: (x0, y0, x1, y1, layer[, length, lineweight, is_door, ...]) in mm;
    dashed: _seg_key of the dashed ones (dxf_reader._collect_raw_segs);
    arcs: door swings (hinge_x, hinge_y, radius, ...); texts: dicts with
    "text", "x", "y" (or (x, y, text) tuples)."""

    def __init__(self, segs=(), dashed=(), arcs=(), texts=()):
        import numpy as np
        import shapely
        from shapely.strtree import STRtree
        rows = []
        for s in segs or ():
            try:
                x0, y0, x1, y1 = float(s[0]), float(s[1]), float(s[2]), float(s[3])
            except (TypeError, ValueError, IndexError):
                continue
            if math.hypot(x1 - x0, y1 - y0) < 1.0:
                continue
            layer = str(s[4]) if len(s) > 4 else ""
            rows.append((x0, y0, x1, y1, layer, bool(s[7]) if len(s) > 7 else False))
        self.segs = rows
        dashed = frozenset(dashed or ())
        self.kind = [layer_kind(r[4]) for r in rows]
        self.door = [r[5] or k == "door" for r, k in zip(rows, self.kind)]
        self.dashed = [_seg_key(*r[:4]) in dashed for r in rows] if dashed else [False] * len(rows)
        self.length = [math.hypot(r[2] - r[0], r[3] - r[1]) for r in rows]
        if rows:
            arr = np.array([r[:4] for r in rows], dtype=np.float64).reshape(-1, 2, 2)
            self.lines = list(shapely.linestrings(arr))
            self.tree = STRtree(self.lines)
        else:
            self.lines, self.tree = [], None
        self.arcs = []
        for a in arcs or ():
            try:
                self.arcs.append((float(a[0]), float(a[1]), float(a[2])))
            except (TypeError, ValueError, IndexError):
                continue
        self.texts = []
        for t in texts or ():
            try:
                if isinstance(t, dict):
                    self.texts.append((float(t["x"]), float(t["y"]), _norm(t.get("text"))))
                else:
                    self.texts.append((float(t[0]), float(t[1]), _norm(t[2])))
            except (KeyError, TypeError, ValueError, IndexError):
                continue
        self.texts = [t for t in self.texts if t[2]]

    def near(self, geom, dist=0.0):
        """Indices of the lines within dist of geom."""
        if self.tree is None or geom is None or geom.is_empty:
            return []
        g = geom.buffer(dist) if dist > 0 else geom
        return [int(i) for i in self.tree.query(g, predicate="intersects")]

    def texts_in(self, geom, dist=0.0):
        if geom is None or geom.is_empty:
            return []
        from shapely.geometry import Point
        g = geom.buffer(dist) if dist > 0 else geom
        b = g.bounds
        return [t[2] for t in self.texts if b[0] <= t[0] <= b[2] and b[1] <= t[1] <= b[3]
                and g.contains(Point(t[0], t[1]))]

    def arcs_near(self, x, y, dist):
        return [a for a in self.arcs if math.hypot(a[0] - x, a[1] - y) <= dist]


# --------------------------------------------------------------- geometry ---
def _frame(pg):
    """(cx, cy, ux, uy, L, W, corners) of a shape's rotated rectangle: u along
    its long side L, W its short side."""
    r = pg.minimum_rotated_rectangle
    if r.geom_type != "Polygon":
        b = pg.bounds
        cs = [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]
    else:
        cs = list(r.exterior.coords)[:4]
    e0 = (cs[1][0] - cs[0][0], cs[1][1] - cs[0][1])
    e1 = (cs[2][0] - cs[1][0], cs[2][1] - cs[1][1])
    l0, l1 = math.hypot(*e0), math.hypot(*e1)
    if l0 >= l1:
        L, W, ux, uy = l0, l1, (e0[0] / l0 if l0 else 1.0), (e0[1] / l0 if l0 else 0.0)
    else:
        L, W, ux, uy = l1, l0, (e1[0] / l1 if l1 else 1.0), (e1[1] / l1 if l1 else 0.0)
    c = pg.minimum_rotated_rectangle.centroid if r.geom_type == "Polygon" else pg.centroid
    return c.x, c.y, ux, uy, L, W, cs


def _dir(ev, i):
    s = ev.segs[i]
    L = ev.length[i] or 1.0
    return (s[2] - s[0]) / L, (s[3] - s[1]) / L


def _parallel(ev, i, ux, uy, tol=0.08):
    dx, dy = _dir(ev, i)
    return abs(dx * -uy + dy * ux) <= tol


def _square(ev, i, ux, uy, tol=0.08):
    dx, dy = _dir(ev, i)
    return abs(dx * ux + dy * uy) <= tol


AREA_OVERLAY_RE = re.compile(r"AREA|^\s*\d+(\.\d+)+\s|DEFPOINTS|\bDIM", re.I)


def boundary_type(pg, ev, idx=None):
    """("solid" | "dashed" | "unclear", dashed share, solid share) of a
    shape's outline as drawn.

    The opening's own lines decide first: those along its rotated
    rectangle's edges on the layer of its X, or on a cut-out / window /
    glass layer. Without enough of them (a quarter of the perimeter), its
    X's diagonals, when all one line type; then whatever else is drawn
    along the edges. Never a wall's face (a wall layer, or a line running
    on past the outline) or an area take-off overlay. Over 60% of the
    lines read dashed is dashed, over 60% solid is solid; else unclear."""
    from shapely.geometry import LineString
    try:
        cx, cy, ux, uy, L, W, cs = _frame(pg)
    except (ValueError, ZeroDivisionError, AttributeError):
        return "unclear", 0.0, 0.0
    if L <= 0 or W <= 0:
        return "unclear", 0.0, 0.0
    per = 2.0 * (L + W)
    diag = math.hypot(L, W)
    edges = [LineString([cs[k], cs[(k + 1) % 4]]) for k in range(4)]
    band_ = edges[0].buffer(25.0)
    for e in edges[1:]:
        band_ = band_.union(e.buffer(25.0))
    region = pg.buffer(30.0)
    ids = ev.near(region) if idx is None else idx
    x_layers, xd, xs = set(), 0, 0
    for i in ids:
        if _parallel(ev, i, ux, uy) or _square(ev, i, ux, uy) or ev.length[i] < 0.4 * diag:
            continue
        if ev.lines[i].intersection(region).length < 0.8 * ev.length[i]:
            continue
        x_layers.add(ev.segs[i][4])
        if ev.dashed[i]:
            xd += 1
        else:
            xs += 1
    own = [0.0, 0.0]
    other = [0.0, 0.0]
    for i in ids:
        if not (_parallel(ev, i, ux, uy) or _square(ev, i, ux, uy)):
            continue
        lay = ev.segs[i][4]
        if ev.kind[i] == "wall" or AREA_OVERLAY_RE.search(re.split(r"[$|]", str(lay))[-1]):
            continue
        inside = ev.lines[i].intersection(band_).length
        if inside < 20.0:
            continue
        if ev.length[i] - inside > 300.0 and not ev.dashed[i]:
            continue                                  # a line running on past it
        k = 0 if ev.dashed[i] else 1
        if lay in x_layers or ev.kind[i] in ("cutout", "window", "glass"):
            own[k] += inside
        else:
            other[k] += inside

    def verdict(d_, s_):
        t = d_ + s_
        if t <= 0:
            return None
        if d_ >= 0.6 * t:
            return "dashed"
        if s_ >= 0.6 * t:
            return "solid"
        return "unclear"
    dsh, sol = min(1.0, (own[0] + other[0]) / per), min(1.0, (own[1] + other[1]) / per)
    if (own[0] + own[1]) / per >= 0.25:
        return verdict(*own), dsh, sol
    if xd + xs >= 2 and (xd == 0 or xs == 0):
        return ("dashed" if xd else "solid"), dsh, sol
    if (own[0] + own[1] + other[0] + other[1]) / per >= 0.25:
        return verdict(own[0] + other[0], own[1] + other[1]), dsh, sol
    return "unclear", dsh, sol


def has_x(pg, ev):
    """Whether two lines cross a shape corner to corner (its X): one along
    each diagonal of its rotated rectangle, each over half its length."""
    try:
        cx, cy, ux, uy, L, W, cs = _frame(pg)
    except (ValueError, ZeroDivisionError, AttributeError):
        return False
    if L <= 0 or W <= 0:
        return False
    diag = math.hypot(L, W)
    d1 = ((cs[2][0] - cs[0][0]) / diag, (cs[2][1] - cs[0][1]) / diag)
    d2 = ((cs[3][0] - cs[1][0]) / diag, (cs[3][1] - cs[1][1]) / diag)
    region = pg.buffer(30.0)
    got = [False, False]
    for i in ev.near(pg):
        if ev.length[i] < 0.5 * diag:
            continue
        dx, dy = _dir(ev, i)
        for k, (ax, ay) in enumerate((d1, d2)):
            if abs(dx * -ay + dy * ax) <= 0.06:
                if ev.lines[i].intersection(region).length >= 0.8 * ev.length[i]:
                    got[k] = True
        if got[0] and got[1]:
            return True
    return False


def _wall_share(walls, geom):
    if walls is None or walls.is_empty or geom is None or geom.is_empty or geom.area <= 0:
        return 0.0
    return walls.intersection(geom).area / geom.area


def _end_probes(cx, cy, ux, uy, L, W, depth=100.0):
    """Probe boxes just beyond each short side of an opening (its jambs)."""
    from shapely.geometry import Polygon
    nx, ny = -uy, ux
    out = []
    for sg in (1.0, -1.0):
        ex, ey = cx + sg * ux * (L / 2.0), cy + sg * uy * (L / 2.0)
        fx, fy = ex + sg * ux * depth, ey + sg * uy * depth
        h = 0.4 * W
        out.append(Polygon([(ex + nx * h, ey + ny * h), (fx + nx * h, fy + ny * h),
                            (fx - nx * h, fy - ny * h), (ex - nx * h, ey - ny * h)]))
    return out


# ----------------------------------------------------------------- scores ---
class _Score:
    """One class's score for one element: the indicators found (name,
    weight) and missing, whether a negative rule fired, and the score the
    key combinations give."""

    def __init__(self, cls):
        self.cls = cls
        self.matched = []
        self.missing = []
        self.negative = False
        self.reroute = None
        self.notes = []
        self.score = 0.0
        self.flags = {}

    def ind(self, name, weight, ok):
        if ok and weight > 0:
            self.matched.append((name, int(round(weight))))
        elif not ok:
            self.missing.append(name)
        return bool(ok)

    @property
    def total(self):
        return min(100.0, float(sum(w for _n, w in self.matched)))

    def ladder(self, rows):
        """The first key combination that holds sets the band the total is
        clamped into; none holding, the total as it is."""
        t = self.total
        for cond, lo, hi in rows:
            if cond:
                self.score = float(max(lo, min(hi, t)))
                return self.score
        self.score = t
        return t

    def reject(self, why, reroute=None):
        self.negative = True
        self.reroute = reroute
        self.notes.append(why)
        self.score = 0.0


# ------------------------------------------------------------ group A ------
def _opening_facts(o, ev, walls):
    """Everything group A asks of an opening, found once."""
    pg = o["poly"]
    cx, cy, ux, uy, L, W, cs = _frame(pg)
    nx, ny = -uy, ux
    from shapely.geometry import Point
    f = {"L": L, "W": W, "ux": ux, "uy": uy}
    probes = _end_probes(cx, cy, ux, uy, L, max(W, 60.0))
    ends = [_wall_share(walls, p) >= 0.4 for p in probes]
    f["wall_ends"] = sum(ends)
    try:
        from . import elements as _el
        f["in_wall_run"] = bool(_el.cutout_in_wall_run(pg, walls))
    except Exception:                                 # noqa: BLE001
        f["in_wall_run"] = False
    # an X-marked slab opening or a cut-out: only what is drawn in it is its
    # own (a shaft's door beside its X strip is not the strip's)
    strict = o.get("kind") in ("x", "cutout")
    own = pg.buffer(40.0)

    def inside(i, share=0.6):
        return ev.lines[i].intersection(own).length >= share * ev.length[i]
    # a partition at a jamb (a WC cubicle's board): a drawn line in the
    # probe beyond it
    part_ends = 0
    for k, p in enumerate(probes):
        if ends[k]:
            part_ends += 1
        elif not strict and any(not ev.door[i] and ev.lines[i].intersection(p).length >= 40.0 for i in ev.near(p)):
            part_ends += 1
    f["part_ends"] = part_ends
    f["through_wall"] = f["wall_ends"] == 2 or f["in_wall_run"] or (not strict and part_ends == 2)
    # in a wall: in a wall run; an opening the walls already took for one
    # (a door, a window, a cut-out in a wall) also between two wall ends. An
    # X-marked slab opening spanning between two cross walls is not.
    f["in_wall"] = f["in_wall_run"] or (o.get("kind") in ("door", "window", "cutout") and f["wall_ends"] == 2)
    jambs = [(cx + sg * ux * L / 2.0, cy + sg * uy * L / 2.0) for sg in (1.0, -1.0)]
    reach = max(L, 600.0)
    ids = ev.near(pg, reach)
    # frame / jamb lines at each jamb: door-layer pieces there, or a line
    # drawn across the opening's end (the wall's end cap)
    fr = [False, False]
    for i in ids:
        if strict and not inside(i, 0.5):
            continue
        for k, (jx, jy) in enumerate(jambs):
            if fr[k]:
                continue
            d = ev.lines[i].distance(Point(jx, jy))
            if ev.door[i] and ev.length[i] <= 300.0 and d <= (60.0 if strict else 150.0):
                fr[k] = True
            elif (not ev.door[i] and not strict and _square(ev, i, ux, uy) and d <= 30.0
                  and ev.length[i] >= 0.5 * W and ev.length[i] <= W + 600.0):
                fr[k] = True
    f["frame_ends"] = sum(fr)
    # the swing: an arc hinged at a jamb, about the opening's width
    sw = []
    for jx, jy in jambs:
        for a in ev.arcs_near(jx, jy, 120.0 if strict else 300.0):
            if 0.35 * L <= a[2] <= 1.25 * L:
                sw.append(a)
    if not sw:
        zone = pg.buffer(60.0 if strict else 300.0)
        for a in ev.arcs:
            if zone.contains(Point(a[0], a[1])) and 0.35 * L <= a[2] <= 1.25 * L:
                sw.append(a)
    f["swing"] = bool(sw)
    # a leaf: a door-layer line about the opening's width at it, a line from
    # a swing's hinge as long as the swing (a WC cubicle's board door), or
    # panels in the opening along it (a sliding door's; a lift's door lines
    # on its own edges)
    leaf = sliding_panels = 0
    lift = (o.get("door_kind") or "") == "lift"
    for i in ids:
        Li = ev.length[i]
        if strict and not inside(i):
            continue
        # a door-layer line across the opening, no longer than the wall is
        # thick, is its jamb (a wall drawn again on the door layer), not a leaf:
        # a leaf runs along the opening, or opens out of it from the hinge
        jamb_line = _square(ev, i, ux, uy) and Li <= W + 80.0
        if ev.door[i] and 0.35 * L <= Li <= 1.2 * L and not jamb_line and \
                ev.lines[i].distance(pg) <= (1.0 if strict else min(0.3 * L, 150.0)):
            leaf += 1
            continue
        if not strict and ev.kind[i] != "wall" and sw:
            sg_ = ev.segs[i]
            if any(min(math.hypot(sg_[0] - a[0], sg_[1] - a[1]), math.hypot(sg_[2] - a[0], sg_[3] - a[1])) <= 80.0
                   and 0.8 * a[2] <= Li <= 1.2 * a[2] for a in sw):
                leaf += 1
                continue
        if strict or ev.kind[i] in ("wall", "glass", "window") or not _parallel(ev, i, ux, uy) or Li < 0.3 * L:
            continue                                  # (glazing is a window's, not a sliding door's)
        # a line jamb to jamb on no door layer is glazing or a sill: a sliding
        # door's panels each run part of the way and overlap
        if not lift and not ev.door[i] and ev.kind[i] != "lift" and Li >= 0.9 * L:
            continue
        if ev.lines[i].intersection(pg.buffer(25.0)).length < 0.6 * Li:
            continue
        mx, my = (ev.segs[i][0] + ev.segs[i][2]) / 2.0, (ev.segs[i][1] + ev.segs[i][3]) / 2.0
        off = abs((mx - cx) * nx + (my - cy) * ny)
        if off <= W / 2.0 - 10.0 or (off <= W / 2.0 + 25.0 and (lift or ev.kind[i] in ("lift", "door"))):
            sliding_panels += 1
    f["leaf"] = leaf > 0
    f["panels"] = sliding_panels
    f["door_kind"] = o.get("door_kind") or ""
    lay_slide = any(re.search(r"SLID", str(ev.segs[i][4]).upper()) for i in ids)
    f["sliding"] = (f["door_kind"] == "lift" or lay_slide or (sliding_panels >= 2 and not sw)) and not strict
    # glazing: lines inside along the long side, clear of the outline
    glz = glz_layer = mull = 0
    for i in ids:
        Li = ev.length[i]
        if Li < 0.5 * L or not _parallel(ev, i, ux, uy):
            if (Li <= W + 50.0 and _square(ev, i, ux, uy) and ev.kind[i] in ("window", "glass")
                    and pg.buffer(5.0).contains(ev.lines[i])):
                mull += 1
            continue
        if ev.lines[i].intersection(pg.buffer(5.0)).length < 0.8 * Li:
            continue
        mx, my = (ev.segs[i][0] + ev.segs[i][2]) / 2.0, (ev.segs[i][1] + ev.segs[i][3]) / 2.0
        off = abs((mx - cx) * nx + (my - cy) * ny)
        if off > W / 2.0 - 15.0:
            continue                                  # its own long edge
        if ev.door[i]:
            continue                                  # a door leaf shut in it
        glz += 1
        if ev.kind[i] in ("glass", "window"):
            glz_layer += 1
    f["glazing_lines"] = glz if not strict else glz_layer
    # an X-marked slab opening's lines inside are its pipes and ducts: only
    # glass or window layers glaze it
    f["glazing"] = glz_layer >= 1 or (glz >= 2 and not strict)
    f["panes"] = glz >= 3 or mull >= 2
    wframe = 0
    for i in ids:
        if ev.kind[i] in ("window", "glass") and ev.lines[i].distance(pg) <= 30.0:
            wframe += 1
    f["window_frame"] = wframe >= 2 and (f["frame_ends"] >= 1 or wframe >= 3)
    f["sill"] = any(ev.kind[i] == "window" and _parallel(ev, i, ux, uy) and 0.0 < ev.lines[i].distance(pg) <= 150.0
                    for i in ids)
    f["x"] = bool(o.get("x")) or has_x(pg, ev)
    f["boundary"], f["dashed_share"], f["solid_share"] = boundary_type(pg, ev)
    f["outline"] = (f["dashed_share"] + f["solid_share"]) >= 0.5 or bool(o.get("x"))
    texts = ev.texts_in(pg, 600.0)
    f["door_tag"] = any(DOOR_TAG_RE.search(t) for t in texts)
    f["window_tag"] = any(WINDOW_TAG_RE.search(t) for t in texts)
    f["cutout_tag"] = any(CUTOUT_TAG_RE.search(t) for t in ev.texts_in(pg, 600.0)) or bool(
        o.get("name") and CUTOUT_TAG_RE.search(_norm(o.get("name"))))
    f["slab_opening"] = bool(o.get("x")) and not f["in_wall"]
    f["service_label"] = bool(o.get("name")) and o.get("kind") == "x" and bool(SERVICE_TEXT_RE.search(_norm(o.get("name"))))
    # the X's context
    if not f["x"]:
        f["x_context"] = "n/a"
    elif f["boundary"] == "dashed":
        f["x_context"] = "cutout"
    elif f["service_label"]:
        f["x_context"] = "shaft"                      # an X named for a service: a shaft's opening
    elif f["boundary"] == "solid" and f["in_wall"] and W <= 450.0:
        f["x_context"] = "window"
    elif f["slab_opening"]:
        f["x_context"] = o.get("x_context") or "undetermined"
    else:
        f["x_context"] = "undetermined"
    return f


def score_door(f):
    s = _Score(DOOR)
    frame = s.ind("Frame/jamb lines", 30 if f["frame_ends"] >= 2 else 15, f["frame_ends"] >= 1)
    leaf = s.ind("Leaf line(s)", 30, f["leaf"] or f["panels"] >= 1)
    opening = s.ind("Opening through wall", 15, f["through_wall"])
    attached = f["wall_ends"] == 2 or f.get("part_ends", 0) == 2
    swing = s.ind("Swing arc (attached to wall on both sides)", 15, f["swing"] and attached)
    sliding = s.ind("Sliding-door arrangement", 10, f["sliding"])
    s.ind("Door label/symbol (corroborating only)", 10, f["door_tag"])
    if f["swing"] and not attached and not frame and not leaf:
        s.reject("a swing arc alone, not attached to the wall on both sides")
        return s
    if f.get("x") and f["x_context"] in ("window", "cutout"):
        s.notes.append("an X in it: an X marks a window or a cut-out, never a door")
    s.ladder([
        (frame and leaf and opening and swing, 90, 100),
        (frame and leaf and opening and sliding, 85, 90),
        (frame and leaf and opening, 75, 80),
        (leaf and not frame and not opening and not swing, 30, 40),
        (opening and f["door_tag"] and not frame and not leaf and not swing, 10, 15),
        (not frame and not leaf and not swing, 0, 15),
    ])
    s.flags = {"frame": frame, "leaf": leaf, "swing": swing, "opening": opening}
    return s


def decide_a(f, scores):
    """(winner class or UNRESOLVED, its score, tie-break applied, notes) of
    a door. Only doors are scored: a window or a cut-out is Step 3's (the
    X sequence's steps 4 and 5), recorded as drawn (fixed_record)."""
    d = scores[DOOR]
    notes = []
    winner = DOOR
    if d.score < REVIEW_MIN:
        winner = UNRESOLVED
        notes.append("no door clears %d" % REVIEW_MIN)
    return winner, d.score, False, notes


# ------------------------------------------------------------ group B ------
DOOR_LAYER_TREADS_MIN = int(_EE.get("door_layer_treads_min", 6))


def flights_in(pg, ev):
    """The stair flights drawn inside a shape: chains of four or more
    parallel lines (treads) 180-400 mm apart, each overlapping the next
    along its length over half the shorter. Lines on a stair layer are
    read alone when there are enough of them. [{"n", "angle", "hull"
    (shapely), "offset"}].

    Door-layer lines are left out, so that a door's leaf is never a tread.
    When no flight is found without them, they are read again: some
    drawings draw the treads on the door layer (H10). A run of six or more
    evenly spaced parallel door-layer lines is a flight, never a door."""
    out = _flights(pg, ev, False, 4)
    if not out:
        out = _flights(pg, ev, True, DOOR_LAYER_TREADS_MIN)
    return out


def between_flights(x_poly, stair_poly, segs=None, ev=None):
    """True when an X-marked shape inside a staircase has a flight of the
    staircase on each of its two long sides - the open well between the
    flights (class Void), never a shaft. ev: an Evidence, else one is made
    of the segments (segs) round the staircase."""
    if x_poly is None or stair_poly is None or x_poly.is_empty or stair_poly.is_empty:
        return False
    if ev is None:
        b = stair_poly.bounds
        pad = 300.0
        near = [s for s in segs or () if not (max(s[0], s[2]) < b[0] - pad or min(s[0], s[2]) > b[2] + pad or
                                              max(s[1], s[3]) < b[1] - pad or min(s[1], s[3]) > b[3] + pad)]
        if not near:
            return False
        ev = Evidence(near)
    flights = flights_in(stair_poly, ev)
    if len(flights) < 2:
        return False
    cx, cy, ux, uy, _L, _W, _cs = _frame(x_poly)
    nx, ny = -uy, ux
    sides = set()
    for f in flights:
        c = f["hull"].centroid
        if f["hull"].distance(x_poly) > 400.0 or x_poly.buffer(-20.0).contains(c):
            continue
        sides.add(1 if (c.x - cx) * nx + (c.y - cy) * ny > 0 else -1)
    return len(sides) == 2


def _flights(pg, ev, door_ok, min_n):
    from shapely.geometry import MultiLineString
    if pg is None or pg.is_empty:
        return []
    inner = pg.buffer(20.0)
    edge = pg.exterior.buffer(30.0)
    cand = []
    for i in ev.near(pg):
        Li = ev.length[i]
        if not (500.0 <= Li <= 3500.0) or ev.kind[i] == "wall" or (ev.door[i] and not door_ok):
            continue
        ln = ev.lines[i]
        if ln.intersection(inner).length < 0.8 * Li:
            continue
        if ln.intersection(edge).length >= 0.8 * Li:
            continue                                  # the outline itself
        dx, dy = _dir(ev, i)
        ang = math.degrees(math.atan2(dy, dx)) % 180.0
        cand.append((ang, i))
    groups = collections.defaultdict(list)
    for ang, i in cand:
        groups[int(round(ang / 3.0)) % 60].append(i)
    out = []
    done = set()
    for key in sorted(groups):
        if key in done:
            continue
        ids = list(groups[key])
        for k2 in ((key + 1) % 60, (key - 1) % 60):
            if k2 in groups and k2 not in done:
                ids += groups[k2]
                done.add(k2)
        done.add(key)
        stair_ids = [i for i in ids if ev.kind[i] == "stair"]
        if len(stair_ids) >= 4:
            ids = stair_ids
        if len(ids) < 4:
            continue
        ux, uy = _dir(ev, ids[0])
        nx, ny = -uy, ux
        rows = []
        for i in ids:
            sg = ev.segs[i]
            mx, my = (sg[0] + sg[2]) / 2.0, (sg[1] + sg[3]) / 2.0
            a0 = sg[0] * ux + sg[1] * uy
            a1 = sg[2] * ux + sg[3] * uy
            rows.append((mx * nx + my * ny, min(a0, a1), max(a0, a1), i))
        rows.sort()
        n = len(rows)
        succ = {}
        for k in range(n):
            r = rows[k]
            for m in range(k + 1, n):
                g = rows[m][0] - r[0]
                if g > 400.0:
                    break
                if g < 180.0:
                    continue
                q = rows[m]
                ov = min(r[2], q[2]) - max(r[1], q[1])
                if ov >= 0.5 * min(r[2] - r[1], q[2] - q[1]):
                    succ[k] = m                       # the next tread: the nearest one along
                    break
        has_pred = set(succ.values())
        used = set()
        for k in range(n):
            if k in has_pred or k in used:
                continue
            chain = [k]
            while chain[-1] in succ and len(chain) < 400:
                chain.append(succ[chain[-1]])
            if len(chain) < min_n:
                continue
            used.update(chain)
            run_ = [rows[c] for c in chain]
            hull = MultiLineString([ev.lines[r_[3]] for r_ in run_]).convex_hull
            if any(f["hull"].intersection(hull).area > 0.5 * min(f["hull"].area, hull.area) for f in out):
                continue                              # the same flight from a duplicate line
            out.append({"n": len(run_), "angle": math.degrees(math.atan2(uy, ux)) % 180.0,
                        "hull": hull, "offset": (run_[0][0] + run_[-1][0]) / 2.0})
    return out


class _Space:
    __slots__ = ("i", "poly", "name", "raw", "family", "layer", "source", "current", "x", "ref", "flights", "rec",
                 "xtag")

    def __init__(self, i, d):
        self.i = i
        self.poly = d["poly"]
        self.raw = " ".join(str(d.get("name") or "").split())
        self.name = _norm(d.get("name"))
        self.family = d.get("family") or ""
        self.layer = d.get("layer") or ""
        self.source = d.get("source") or ""
        self.current = d.get("current_class")
        self.x = bool(d.get("x"))
        self.ref = d.get("ref")
        self.flights = None
        self.rec = None
        self.xtag = d.get("x_tag")                    # (class, step, why) of the X sequence, or None


def _named(sp, rx):
    return bool(sp.name and "UNKNOWN" not in sp.name and rx.search(sp.name))


def _lift_named(sp):
    return _named(sp, LIFT_TEXT_RE) and not LIFT_NOT_RE.search(sp.name)


def _service_named(sp):
    if not sp.name or "UNKNOWN" in sp.name:
        return False
    if SERVICE_TEXT_RE.search(sp.name):
        return True
    try:
        from . import elements as _el
        return bool(_el.is_shaft_room(sp.name, ""))
    except Exception:                                 # noqa: BLE001
        return False


class _Floor:
    """One floor's spaces and openings, indexed for the neighbour tests."""

    def __init__(self, spaces, doors, walls, ev, extra_solid=None):
        from shapely.strtree import STRtree
        self.spaces = spaces
        self.walls = walls
        self.ev = ev
        self.doors = doors                            # [(shapely quad, kind)]
        self.stree = STRtree([s.poly for s in spaces]) if spaces else None
        self.dtree = STRtree([d[0] for d in doors]) if doors else None
        self.solid = extra_solid

    def near(self, sp, dist):
        if self.stree is None:
            return []
        g = sp.poly.buffer(dist)
        return [self.spaces[int(k)] for k in self.stree.query(g, predicate="intersects")
                if self.spaces[int(k)] is not sp]

    def doors_at(self, sp, dist=60.0):
        if self.dtree is None:
            return []
        g = sp.poly.buffer(dist)
        return [self.doors[int(k)] for k in self.dtree.query(g, predicate="intersects")]

    def flights(self, sp):
        if sp.flights is None:
            sp.flights = flights_in(sp.poly, self.ev)
        return sp.flights


def _contact(a, b, dist=60.0):
    """Length of a's outline within dist of b."""
    try:
        return a.exterior.intersection(b.buffer(dist)).length
    except Exception:                                 # noqa: BLE001
        return 0.0


def _tier1(sp, fl):
    """Tier 1's score of a space - Staircase - and the facts behind it. A
    shaft, a lift or a void is Step 3's (the X sequence), never scored."""
    ev = fl.ev
    pg = sp.poly
    near = fl.near(sp, NEAR_MM)
    doors = fl.doors_at(sp)
    flights = fl.flights(sp)
    treads = sum(f["n"] for f in flights)
    # neighbours' flights (this space between two flights of one staircase)
    fl_near = []
    for o in near:
        if o.family == "Staircase" or _named(o, STAIR_TEXT_RE) or o.current == STAIR:
            fl_near.extend(fl.flights(o))
    cx, cy, ux, uy, L, W, _cs = _frame(pg)
    nx, ny = -uy, ux
    sides = set()
    for f in fl_near:
        c = f["hull"].centroid
        if f["hull"].distance(pg) > 400.0 or pg.buffer(-20.0).contains(c):
            continue
        off = (c.x - cx) * nx + (c.y - cy) * ny
        sides.add(1 if off > 0 else -1)
    stair_named = _named(sp, STAIR_TEXT_RE)
    annot = any(STAIR_ANNOT_RE.search(t) for t in ev.texts_in(pg, 300.0))
    svc_named = _service_named(sp) and not _lift_named(sp)
    x_in = sp.x or any(o.x and sp.poly.buffer(50.0).contains(o.poly.representative_point()) for o in near)
    x_ctx = ("staircase" if (flights or fl_near) else "undetermined") if x_in else "n/a"
    facts = {"treads": treads, "flights": len(flights), "sides": len(sides), "lift_doors": 0,
             "doors": len(doors), "x_context": x_ctx, "x": x_in}

    s = _Score(STAIR)
    tr = s.ind("Stair treads / flights present and identifiable", 30, bool(flights))
    land = False
    if flights:
        from shapely.ops import unary_union
        rest = pg.difference(unary_union([f["hull"].buffer(50.0) for f in flights]))
        for part in getattr(rest, "geoms", [rest]):
            if part.is_empty or part.area < 0.8e6:
                continue
            _c = _frame(part)
            if _c[5] >= 800.0:
                land = True
                break
    land = s.ind("Landings present", 20, land)
    upd = s.ind("UP/DOWN arrows or staircase annotations", 15, annot)
    s.ind("Entrance door near the staircase", 10, bool(fl.doors_at(sp, 300.0)))
    st = s.ind('Text evidence ("staircase", "stairwell", "stair")', 15, stair_named and not svc_named)
    s.ind("Long X/cross mark near the flights (corroborating only)", 5, x_in and x_ctx == "staircase")
    if not flights and len(sides) == 2:
        s.reject("the enclosed gap between two flights, no treads of its own: not a staircase")
    else:
        s.ladder([
            (tr and land and upd, 90, 100),
            (tr and land, 80, 85),
            (tr, 55, 60),
            (st and not tr, 20, 25),
            (x_in and not tr and not st, 10, 15),
            (not tr and not st, 0, 10),
        ])
        if tr and len(flights) == 1:
            s.notes.append("treads on one side only")
            s.score = min(s.score, 79.0)
    return {STAIR: s}, facts


def decide_b1(scores, facts):
    """A staircase, or UNRESOLVED under REVIEW_MIN."""
    s = scores[STAIR]
    notes = []
    winner = STAIR
    if s.score < REVIEW_MIN:
        winner = UNRESOLVED
        notes.append("no staircase clears %d" % REVIEW_MIN)
    return winner, s.score, False, notes


def _walls_traceable(sp, fl):
    """Share of a space's outline running along a wall, door, window or
    glass (within 150 mm)."""
    solid = fl.solid
    if solid is None or solid.is_empty:
        return 0.0
    try:
        per = sp.poly.exterior.length
        return sp.poly.exterior.intersection(solid.buffer(150.0)).length / per if per else 0.0
    except Exception:                                 # noqa: BLE001
        return 0.0


def _tier2(sp, fl):
    """The three tier-2 scores of a space, with the tier-1 elements they
    rest on."""
    near = fl.near(sp, NEAR_MM)
    lifts = [o for o in near if o.rec is not None and o.rec["classification"] == LIFT]
    stairs = [o for o in near if o.rec is not None and o.rec["classification"] == STAIR]
    lifts_ok = [o for o in lifts if o.rec["confidence_score"] >= AUTO_MIN]
    stairs_ok = [o for o in stairs if o.rec["confidence_score"] >= AUTO_MIN]
    doors = fl.doors_at(sp)
    lift_door = [d for d in doors if d[1] == "lift" and any(d[0].distance(o.poly) <= 80.0 for o in lifts_ok)]
    if not lift_door:
        lift_door = [o for o in lifts_ok if any(d[1] == "lift" for d in fl.doors_at(o))
                     and any(d[0].distance(sp.poly) <= 80.0 for d in fl.doors_at(o) if d[1] == "lift")]
    stair_conn = []
    for o in stairs_ok:
        if any(d[0].distance(o.poly) <= 80.0 for d in doors) or _contact(sp.poly, o.poly) >= 600.0:
            stair_conn.append(o)
    walls = _walls_traceable(sp, fl) >= 0.6
    circ = any(o.family in ("Circulation", "Lift Lobby", "Service Lobby", "Staircase") or _named(o, CIRCULATION_RE)
               for o in fl.near(sp, 300.0)) or len([d for d in doors if d[1] != "lift"]) >= 1
    prereq_lift = [{"element_id": o.rec["element_id"], "class": LIFT, "score": o.rec["confidence_score"]}
                   for o in lifts]
    prereq_stair = [{"element_id": o.rec["element_id"], "class": STAIR, "score": o.rec["confidence_score"]}
                    for o in stairs]

    s = _Score(LIFT_LOBBY)
    gate = s.ind("Adjacent Lift confirmed (>=50) - prerequisite", 40, bool(lifts_ok))
    dc = s.ind("Lift door/opening connects directly to candidate space", 25, bool(lift_door))
    wl = s.ind("Enclosing walls traceable around the space", 20, walls)
    cl = s.ind("Connected circulation (corridor/landing linkage)", 15, circ)
    if not gate:
        s.reject("no adjacent lift scoring 50 or more: unresolved by the gate")
    else:
        s.ladder([
            (dc and wl and cl, 90, 100),
            (dc and wl, 75, 80),
            (dc, 50, 55),
            (not dc, 15, 20),
        ])
        if dc and not wl:
            s.notes.append("lift door connects but the wall boundary is unclear: check the boundary")
    ll = s
    ll.flags = {"prereq": prereq_lift, "met": bool(lifts_ok)}

    s = _Score(SERVICE_LOBBY)
    gate = s.ind("Lift >=50 AND Staircase >=50 both present - prerequisite", 40, bool(lifts_ok) and bool(stairs_ok))
    both = s.ind("Both confirmed to open into/connect to the SAME shared space", 35,
                 bool(lift_door) and bool(stair_conn))
    bd = s.ind("Enclosure/wall boundary of the shared space clearly defined", 25, walls)
    if not gate:
        s.reject("needs a lift and a staircase, both 50 or more: unresolved by the gate")
    elif bool(lift_door) != bool(stair_conn):
        s.reject("only one of lift / staircase connects: a lift lobby, landing or corridor",
                 LIFT_LOBBY if lift_door else None)
    else:
        s.ladder([
            (both and bd, 90, 100),
            (both, 65, 70),
            (not both, 20, 25),
        ])
    sl = s
    sl.flags = {"prereq": prereq_lift + prereq_stair, "met": bool(lifts_ok) and bool(stairs_ok)}

    s = _Score(MACHINE_ROOM)
    texts = fl.ev.texts_in(sp.poly) + ([sp.name] if sp.name else [])
    lift_sys = any(o.family in ("Lift", "Shaft") or _lift_named(o) or (o.rec and o.rec["classification"] in (LIFT, SHAFT))
                   for o in near)
    eq = s.ind("Equipment symbols present", 30, any(EQUIP_TEXT_RE.search(t) for t in texts))
    mr = s.ind('"Machine room" label', 25, any(MR_TEXT_RE.search(t) for t in texts) or sp.family == "Machine Room")
    nod = s.ind("Confirmed NO door/opening into adjacent lobby", 20, not doors)
    ctx = s.ind("Lift-system context / adjacency to Lift or Shaft family element", 25, lift_sys)
    if not lift_sys:
        s.reject("not beside a lift or shaft: unresolved by the gate")
    elif doors:
        s.reject("a door into it: not a machine room - a lift, lobby or room")
    else:
        s.ladder([
            (eq and mr and nod, 90, 100),
            (mr and nod, 65, 70),
            (nod and ctx and not mr and not eq, 50, 55),
            (nod and not ctx and not mr and not eq, 10, 15),
        ])
    mrs = s
    mrs.flags = {"prereq": [{"element_id": o.rec["element_id"], "class": o.rec["classification"],
                             "score": o.rec["confidence_score"]} for o in near
                            if o.rec and o.rec["classification"] in (LIFT, SHAFT)], "met": lift_sys}
    facts = {"lift_door": bool(lift_door), "stair_conn": bool(stair_conn)}
    return {LIFT_LOBBY: ll, SERVICE_LOBBY: sl, MACHINE_ROOM: mrs}, facts


def decide_b2(scores, facts):
    order = sorted(scores.values(), key=lambda s: -s.score)
    best, second = order[0], order[1]
    tie = second.score > 0 and best.score - second.score <= TIE_MARGIN
    notes = []
    winner = best.cls
    if {best.cls, second.cls} == {LIFT_LOBBY, SERVICE_LOBBY} and tie:
        winner = SERVICE_LOBBY if (facts["lift_door"] and facts["stair_conn"]) else LIFT_LOBBY
        notes.append("service lobby only when both lift and staircase open into it")
    elif tie and best.score < AUTO_MIN and second.score < AUTO_MIN:
        winner = UNRESOLVED
    sc = scores[winner].score if winner in scores else best.score
    if winner != UNRESOLVED and sc < REVIEW_MIN:
        winner = UNRESOLVED
    return winner, sc, tie, notes


# ---------------------------------------------------------------- records ---
def _record(eid, group, winner, sc, tie, notes, scores, extra):
    w = scores.get(winner)
    if w is None and scores:
        w = max(scores.values(), key=lambda s_: s_.score)   # unresolved: the nearest class's evidence
    rec = {
        "element_id": eid,
        "group": group,
        "tier": "n/a" if group == "A" else TIER_OF.get(winner if winner in TIER_OF else extra.get("_tier_cls"), "1"),
        "classification": winner,
        "confidence_score": int(round(sc)),
        "confidence_band": band(sc) if winner != UNRESOLVED else ("Unresolved" if sc < REVIEW_MIN else band(sc)),
        "indicators_matched": [{"name": n, "weight": wt} for n, wt in (w.matched if w else [])],
        "indicators_missing": list(w.missing) if w else [],
        "boundary_type": extra.get("boundary_type", "n/a"),
        "x_symbol_present": bool(extra.get("x")),
        "x_symbol_context": extra.get("x_context", "n/a") if extra.get("x") else "n/a",
        "prerequisite_elements": extra.get("prereq", []),
        "prerequisite_met": bool(extra.get("prereq_met", True)),
        "negative_rule_triggered": any(s.negative for s in scores.values()),
        "tie_break_applied": bool(tie),
        "competing_classes": [{"class": s.cls, "score": int(round(s.score))}
                              for s in sorted(scores.values(), key=lambda s: -s.score) if s.cls != winner],
        "action": action(sc, winner),
        "notes": "",
    }
    if winner == UNRESOLVED:
        rec["confidence_band"] = "Unresolved"
    if winner != UNRESOLVED and rec["confidence_band"] == "Low":
        notes = ["possible %s - needs review" % winner.lower()] + list(notes)
    allnotes = list(notes) + (w.notes if w else []) + [
        "%s rejected: %s" % (s.cls, "; ".join(s.notes)) for s in scores.values()
        if s.negative and s is not w and s.notes]
    rec["notes"] = "; ".join(n for n in allnotes if n)
    return rec


def fixed_class(d):
    """The Step 3 class of an element (one of STEP3_CLASSES), or None: the X
    sequence's class from its tag, else the class it is drawn as when that
    is one of the six."""
    xt = d.get("x_tag")
    if xt and X_CLASS.get(str(xt[0])):
        return X_CLASS[str(xt[0])]
    kind = d.get("kind")
    if kind == "window":
        return WINDOW
    if kind in ("cutout", "x"):
        return CUTOUT
    if kind == "column":
        return COLUMN
    cur = d.get("current_class")
    return cur if cur in (SHAFT, LIFT, VOID) else None


def fixed_record(eid, group, cls, d):
    """The record of an element Step 3 decided: its class as drawn, score
    100, never reviewed - with the X sequence's step and reason."""
    xt = d.get("x_tag")
    if xt:
        step = int(xt[1] or 0) if len(xt) > 1 else 0
        why = str(xt[2] or "") if len(xt) > 2 else ""
        by = "the X sequence" + (", step %d (%s)" % (step, X_STEP_NAME.get(step, "")) if step else "")
        notes = "decided in Step 3 by %s%s" % (by, (": " + why) if why else "")
    else:
        by = "Step 3"
        notes = "drawn so in Step 3 (%s)" % (d.get("layer") or cls)
    return {
        "element_id": eid,
        "group": group,
        "tier": "n/a" if group in ("A", "C") else TIER_OF.get(cls, "1"),
        "classification": cls,
        "confidence_score": 100,
        "confidence_band": "High",
        "indicators_matched": [],
        "indicators_missing": [],
        "boundary_type": "n/a",
        "x_symbol_present": bool(xt) or bool(d.get("x")),
        "x_symbol_context": (str(xt[0]) if xt else "n/a"),
        "prerequisite_elements": [],
        "prerequisite_met": True,
        "negative_rule_triggered": False,
        "tie_break_applied": False,
        "competing_classes": [],
        "action": FIXED_ACTION,
        "fixed": True,
        "decided_by": by,
        "notes": notes,
    }


def identify_floor(spaces, openings, walls=None, evidence=None, floor="", prefix="F1", solid=None):
    """Classify one floor's elements.

    spaces: [{"poly" (shapely, mm), "name", "family", "layer", "source",
    "current_class", "x" (an X-marked opening), "x_tag", "ref"}] - every
    room drawn, the engine picks its candidates from them; openings:
    [{"poly", "kind" ("door" | "window" | "cutout" | "column"), "door_kind",
    "name", "x", "x_tag", "current_class", "ref"}]; walls: the floor's
    walls (shapely); evidence: an Evidence; solid: walls, doors, windows
    and glass together (tier 2's traceable walls). A shaft, lift, window,
    cut-out, column or void (fixed_class) is recorded as Step 3 decided it
    (fixed_record); doors, staircases and the tier-2 spaces are scored.
    Returns the records, each with "_ref" (the caller's ref) and "_poly"
    (shapely), which are not for the JSON."""
    ev = evidence or Evidence()
    from shapely.ops import unary_union
    walls_g = walls
    sps = [_Space(i, d) for i, d in enumerate(spaces or ()) if d.get("poly") is not None
           and not d["poly"].is_empty and d["poly"].area > 0]
    door_list = [(o["poly"], o.get("door_kind") or "") for o in openings or () if o.get("kind") == "door"]
    if solid is None:
        parts = [g for g in [walls_g] + [o["poly"] for o in openings or () if o.get("kind") != "column"]
                 if g is not None and not g.is_empty]
        solid = unary_union(parts) if parts else None
    fl = _Floor(sps, door_list, walls_g, ev, solid)
    out = []
    na = nb = nc = 0
    # group A (and C: the columns)
    for o in openings or ():
        pg = o.get("poly")
        if pg is None or pg.is_empty or pg.area <= 0:
            continue
        cls = fixed_class(o) if o.get("kind") != "door" else None
        if cls == COLUMN:
            nc += 1
            out.append(_finish(fixed_record("%s-C%03d" % (prefix, nc), "C", cls, o), o, pg, floor))
            continue
        na += 1
        eid = "%s-A%03d" % (prefix, na)
        if cls is not None:
            out.append(_finish(fixed_record(eid, "A", cls, o), o, pg, floor))
            continue
        try:
            f = _opening_facts(o, ev, walls_g)
        except Exception as exc:                      # noqa: BLE001
            out.append(_failed(eid, "A", o, pg, floor, exc))
            continue
        d = score_door(f)
        scores = {DOOR: d}
        winner, sc, tie, notes = decide_a(f, scores)
        rec = _record(eid, "A", winner, sc, tie, notes, scores,
                      {"boundary_type": f["boundary"] if f["outline"] or f["x"] else "n/a",
                       "x": f["x"], "x_context": f["x_context"]})
        out.append(_finish(rec, o, pg, floor))
    # group B: the shafts, lifts and voids of Step 3 first (fixed), then
    # tier 1 (staircases) and tier 2
    t1, t2 = [], []
    for sp in sps:
        cls = fixed_class({"x_tag": sp.xtag, "current_class": sp.current})
        if cls in (SHAFT, LIFT, VOID):
            nb += 1
            rec = fixed_record("%s-B%03d" % (prefix, nb), "B", cls,
                               {"x_tag": sp.xtag, "x": sp.x, "layer": sp.layer})
            sp.rec = rec
            out.append(_finish(rec, _as_dict(sp), sp.poly, floor))
            continue
        if sp.xtag:
            continue                                  # an X mark the sequence made something else
        cur = sp.current
        if cur == STAIR:
            t1.append(sp)
        elif cur in TIER2 or sp.family == "Circulation" or (_named(sp, CIRCULATION_RE) and not _service_named(sp)):
            t2.append(sp)
        elif _candidate_t1(sp):
            t1.append(sp)
    for sp in t1:
        nb += 1
        eid = "%s-B%03d" % (prefix, nb)
        try:
            scores, facts = _tier1(sp, fl)
        except Exception as exc:                      # noqa: BLE001
            out.append(_failed(eid, "B", _as_dict(sp), sp.poly, floor, exc))
            continue
        winner, sc, tie, notes = decide_b1(scores, facts)
        extra = {"x": facts["x"], "x_context": facts["x_context"], "boundary_type": "n/a",
                 "_tier_cls": STAIR}
        rec = _record(eid, "B", winner, sc, tie, notes, scores, extra)
        if winner == UNRESOLVED:
            rec["tier"] = "1"
        sp.rec = rec
        out.append(_finish(rec, _as_dict(sp), sp.poly, floor))
    for sp in t2:
        try:
            scores, facts = _tier2(sp, fl)
        except Exception as exc:                      # noqa: BLE001
            nb += 1
            out.append(_failed("%s-B%03d" % (prefix, nb), "B", _as_dict(sp), sp.poly, floor, exc))
            continue
        if sp.current not in TIER2 and max(s.score for s in scores.values()) < AUTO_MIN:
            continue                                  # a corridor no lift or stair stands on
        nb += 1
        eid = "%s-B%03d" % (prefix, nb)
        winner, sc, tie, notes = decide_b2(scores, facts)
        w = scores.get(winner)
        extra = {"x": sp.x, "x_context": "n/a", "boundary_type": "n/a", "_tier_cls": LIFT_LOBBY,
                 "prereq": (w.flags.get("prereq") if w else scores[LIFT_LOBBY].flags.get("prereq")) or [],
                 "prereq_met": bool((w.flags.get("met") if w else False))}
        rec = _record(eid, "B", winner, sc, tie, notes, scores, extra)
        rec["tier"] = "2"
        sp.rec = rec
        out.append(_finish(rec, _as_dict(sp), sp.poly, floor))
    return out


def _candidate_t1(sp):
    """A space tier 1 looks at: drawn as a staircase, or named for one (a
    shaft, a lift or a void is Step 3's)."""
    if sp.family == "Staircase":
        return True
    if sp.family in ("Shaft", "Lift", "Void", "Toilet"):
        return False
    return _named(sp, STAIR_TEXT_RE)


def _as_dict(sp):
    return {"name": sp.raw, "family": sp.family, "layer": sp.layer, "current_class": sp.current,
            "ref": sp.ref, "source": sp.source}


def _finish(rec, d, pg, floor):
    rec["floor"] = floor
    rec["name"] = " ".join(str(d.get("name") or "").split())
    rec["layer"] = d.get("layer") or ""
    rec["current_class"] = d.get("current_class") or ""
    cur = rec["current_class"]
    if cur in CLASSES and rec["classification"] in CLASSES:
        rec["agrees"] = rec["classification"] == cur
    elif cur in CLASSES:
        rec["agrees"] = False
    elif cur and rec["classification"] in TIER2 and rec["confidence_score"] >= AUTO_MIN:
        rec["agrees"] = False                         # a corridor or lobby the engine takes for a lift lobby
    else:
        rec["agrees"] = None
    rec["area_sqm"] = round(pg.area / 1e6, 3)
    c = pg.representative_point()
    rec["x"], rec["y"] = round(c.x), round(c.y)
    if d.get("door_kind"):
        rec["door_kind"] = d.get("door_kind")
    rec["review"] = needs_review(rec)
    rec["_ref"] = d.get("ref")
    rec["_poly"] = pg
    return rec


def _failed(eid, group, d, pg, floor, exc):
    rec = {"element_id": eid, "group": group, "tier": "n/a" if group == "A" else "1",
           "classification": UNRESOLVED, "confidence_score": 0, "confidence_band": "Unresolved",
           "indicators_matched": [], "indicators_missing": [], "boundary_type": "n/a",
           "x_symbol_present": False, "x_symbol_context": "n/a", "prerequisite_elements": [],
           "prerequisite_met": False, "negative_rule_triggered": False, "tie_break_applied": False,
           "competing_classes": [], "action": "Hold - unresolved",
           "notes": "the evidence could not be read: %s" % (str(exc)[:120],)}
    return _finish(rec, d, pg, floor)


def needs_review(rec):
    """An element to look at: Low or Unresolved, or classified otherwise
    than it is drawn - never one Step 3 decided (fixed)."""
    if rec.get("fixed"):
        return False
    if rec["confidence_band"] in ("Low", "Unresolved") or rec["classification"] == UNRESOLVED:
        return True
    return bool(REVIEW_DISPUTED and rec.get("agrees") is False)


def summary(records):
    """Counts for the run report: by class and band, reviews, disputes."""
    by = collections.Counter((r["classification"], r["confidence_band"]) for r in records)
    out = {"elements": len(records),
           "auto": sum(1 for r in records if r["action"] == "Auto-classify"),
           "review": sum(1 for r in records if r["action"] == "Needs review"),
           "unresolved": sum(1 for r in records if r["action"] == "Hold - unresolved"),
           "disputed": sum(1 for r in records if r.get("agrees") is False),
           "fixed": sum(1 for r in records if r.get("fixed")),
           "marked": sum(1 for r in records if r.get("review")),
           "by_class": {}}
    for (cls, bd), n in sorted(by.items()):
        out["by_class"].setdefault(cls, {})[bd] = n
    return out


def public(rec):
    """A record without the engine's own keys (for JSON)."""
    return {k: v for k, v in rec.items() if not k.startswith("_")}


# ------------------------------------------------------- drawing it out ---
def tag_entity(doc, entity, recs):
    """Store an element's classification on its entity, as XDATA:
    id, class, score, band, action - one group per record."""
    try:
        if ELEMENT_APPID not in doc.appids:
            doc.appids.new(ELEMENT_APPID)
        vals = []
        for r in recs:
            vals += [(1000, str(r["element_id"])), (1000, str(r["classification"])),
                     (1070, int(r["confidence_score"])), (1000, str(r["confidence_band"])),
                     (1000, str(r["action"]))]
        if vals:
            entity.set_xdata(ELEMENT_APPID, vals)
    except (AttributeError, TypeError, ValueError):
        pass


def element_tag(entity):
    """[(id, class, score, band, action)] tag_entity stored on an entity."""
    try:
        if not entity.has_xdata(ELEMENT_APPID):
            return []
        vals = [v for _c, v in entity.get_xdata(ELEMENT_APPID)]
    except Exception:                                 # noqa: BLE001
        return []
    return [tuple(vals[k:k + 5]) for k in range(0, len(vals) - 4, 5)]


def review_note(rec):
    cls = rec["classification"]
    if cls != UNRESOLVED and rec["confidence_band"] == "Low":
        cls = "possible " + cls
    s = "%s %s %d" % (rec["element_id"], cls, rec["confidence_score"])
    if rec.get("agrees") is False and rec.get("current_class"):
        s += " (drawn %s)" % rec["current_class"]
    return s


def draw_review(doc, msp, units, records):
    """The elements to look at, outlined on AI_REVIEW 60 mm outside their
    shape, with a note (id, class, score). Returns how many."""
    if REVIEW_LAYER not in doc.layers:
        doc.layers.add(REVIEW_LAYER, color=REVIEW_ACI)
    n = 0
    f = units.from_mm
    for r in records:
        if not r.get("review"):
            continue
        pg = r.get("_poly")
        if pg is None or pg.is_empty:
            continue
        try:
            g = pg.buffer(60.0, join_style=2)
            if g.geom_type != "Polygon":
                g = g.convex_hull
            ring = [(f(x), f(y)) for x, y in list(g.exterior.coords)[:-1]]
            e = msp.add_lwpolyline(ring, close=True, dxfattribs={"layer": REVIEW_LAYER, "color": 256})
            tag_entity(doc, e, [r])
            b = g.bounds
            msp.add_mtext(review_note(r), dxfattribs={
                "layer": REVIEW_LAYER, "char_height": f(REVIEW_TEXT_MM), "attachment_point": 7,
                "insert": (f(b[0]), f(b[3] + 30.0))})
            n += 1
        except Exception:                             # noqa: BLE001
            continue
    return n


# -------------------------------------------------- the annotation step ---
def run_on_annotation(doc, msp, units, floors, jobs, passes, entities, family_of_layer):
    """The engine over a drawing annotate_dxf has just drawn.

    floors / jobs / passes: annotate_dxf's, one per floor (jobs: the floor
    pass inputs - "segs", "dashed", "door_arcs", "rows"; passes: their
    results - "walls"); entities: what annotate_dxf added to modelspace;
    family_of_layer(layer) -> family name. Tags each element, outlines the
    ones to review, and returns (records, summary)."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    from . import layernames as LN
    from . import dxf_reader as dr
    fpolys = []
    for fl in floors:
        try:
            fpolys.append(Polygon([tuple(p) for p in fl["polygon"]]).buffer(0))
        except (ValueError, TypeError):
            fpolys.append(None)

    def pts_of(e):
        try:
            return [units.point_to_mm(p[0], p[1]) for p in e.get_points("xy")]
        except Exception:                             # noqa: BLE001
            return []

    per_floor = collections.defaultdict(lambda: {"spaces": [], "openings": [], "glass": []})
    for e in entities:
        if e.dxftype() != "LWPOLYLINE":
            continue
        layer = (e.dxf.layer or "").upper()
        if layer in LN.DOORS:
            # a door drawn as a line: the opening it closes, from its XDATA
            pts = dr.door_quad(e, units) or []
        elif not e.closed:
            continue
        else:
            pts = pts_of(e)
        if len(pts) < 3:
            continue
        try:
            g = Polygon(pts).buffer(0)
        except (ValueError, TypeError):
            continue
        if g.is_empty or g.area <= 0:
            continue
        if g.geom_type != "Polygon":
            g = max(g.geoms, key=lambda q: q.area)
        c = g.representative_point()
        k = next((k for k, fp in enumerate(fpolys) if fp is not None and fp.buffer(500.0).contains(c)), None)
        if k is None:
            continue
        bucket = per_floor[k]
        xt = dr.x_tag(e)                              # the X sequence's (class, step, why), or None
        if layer in LN.DOORS:
            kind, _lev = dr.door_tag(e)
            bucket["openings"].append({"poly": g, "kind": "door", "door_kind": kind, "current_class": DOOR,
                                       "ref": e, "name": ""})
        elif layer == LN.WINDOW:
            bucket["openings"].append({"poly": g, "kind": "window", "current_class": WINDOW, "ref": e, "name": "",
                                       "x_tag": xt, "layer": e.dxf.layer})
        elif layer == LN.CUTOUT:
            bucket["openings"].append({"poly": g, "kind": "cutout", "current_class": CUTOUT, "ref": e,
                                       "name": "", "x": True, "x_tag": xt, "layer": e.dxf.layer})
        elif layer == LN.COLUMN:
            if xt:                                    # a column's cross: the X sequence's step 6
                bucket["openings"].append({"poly": g, "kind": "column", "current_class": COLUMN, "ref": e,
                                           "name": "C", "x": True, "x_tag": xt, "layer": e.dxf.layer})
        elif layer == LN.GLASS:
            bucket["glass"].append(g)
        elif LN.is_room_layer(layer):
            src, label = "", ""
            try:
                if e.has_xdata(dr.ROOM_APPID):
                    vals = [v for code, v in e.get_xdata(dr.ROOM_APPID) if code == 1000]
                    src = vals[0] if vals else ""
                    label = vals[1] if len(vals) > 1 else ""
            except Exception:                         # noqa: BLE001
                pass
            fam = family_of_layer(e.dxf.layer) or ""
            cur = FAMILY_CLASS.get(fam)
            is_x = src == "X" or bool(xt)
            if xt and X_CLASS.get(str(xt[0])) in (SHAFT, LIFT, VOID):
                cur = X_CLASS[str(xt[0])]             # what the X sequence made it
            if fam == "Circulation":
                cur = "Circulation"
            sp = {"poly": g, "name": label, "family": fam, "layer": e.dxf.layer, "source": src,
                  "current_class": cur, "x": is_x, "x_tag": xt, "ref": e}
            bucket["spaces"].append(sp)
    records = []
    for k, fl in enumerate(floors):
        if k not in per_floor:
            continue
        job = jobs[k] if k < len(jobs) else {}
        res = passes[k] if k < len(passes) else {}
        wall_parts = []
        for _cls, items in ((res or {}).get("walls") or {}).items():
            for rings in items or ():
                try:
                    if rings and len(rings[0]) >= 3:
                        wall_parts.append(Polygon(rings[0], [r for r in rings[1:] if len(r) >= 3]).buffer(0))
                except (ValueError, TypeError):
                    continue
        walls = unary_union(wall_parts) if wall_parts else None
        b = per_floor[k]
        solid_parts = ([walls] if walls is not None else []) + [o["poly"] for o in b["openings"]
                                                                  if o["kind"] in ("door", "window")] + b["glass"]
        solid = unary_union(solid_parts) if solid_parts else None
        ev = Evidence(job.get("segs") or (), job.get("dashed") or (), job.get("door_arcs") or (),
                      job.get("rows") or ())
        recs = identify_floor(b["spaces"], b["openings"], walls, ev, floor=fl.get("name") or "",
                              prefix="F%d" % (k + 1), solid=solid)
        records.extend(recs)
    by_ent = collections.defaultdict(list)
    for r in records:
        if r.get("_ref") is not None:
            by_ent[id(r["_ref"])].append(r)
    ents = {id(r["_ref"]): r["_ref"] for r in records if r.get("_ref") is not None}
    for key, recs in by_ent.items():
        tag_entity(doc, ents[key], recs)
    marked = draw_review(doc, msp, units, records)
    summ = summary(records)
    summ["marked"] = marked
    return records, summ


# ------------------------------------------------------------- workbook ---
_COLUMNS = ["floor", "element_id", "group", "tier", "name", "layer", "current_class", "classification",
            "confidence_score", "confidence_band", "action", "agrees", "review", "boundary_type",
            "x_symbol_present", "x_symbol_context", "prerequisite_met", "negative_rule_triggered",
            "tie_break_applied", "competing_classes", "indicators_matched", "indicators_missing",
            "prerequisite_elements", "door_kind", "area_sqm", "x", "y", "notes"]


def _cell(v):
    if isinstance(v, list):
        if v and isinstance(v[0], dict):
            if "weight" in v[0]:
                return "; ".join("%s (%s)" % (d.get("name"), d.get("weight")) for d in v)
            if "score" in v[0] and "element_id" in v[0]:
                return "; ".join("%s %s %s" % (d.get("element_id"), d.get("class"), d.get("score")) for d in v)
            if "score" in v[0]:
                return "; ".join("%s %s" % (d.get("class"), d.get("score")) for d in v)
        return "; ".join(str(x) for x in v)
    if v is None:
        return ""
    return v


def write_elements_xlsx(records, path, title="Element identification"):
    """The element list as a workbook: one row per element (the
    specification's fields, flattened), and a summary sheet."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = "Elements"
    ws.append(_COLUMNS)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="305496")
        c.alignment = Alignment(wrap_text=True, vertical="top")
    fills = {"High": "C6EFCE", "Medium": "E2EFDA", "Low": "FFEB9C", "Unresolved": "FFC7CE"}
    for r in records:
        ws.append([_cell(r.get(k)) for k in _COLUMNS])
        fill = fills.get(r.get("confidence_band"))
        if fill:
            ws.cell(row=ws.max_row, column=_COLUMNS.index("confidence_band") + 1).fill = PatternFill("solid", fgColor=fill)
        if r.get("agrees") is False:
            ws.cell(row=ws.max_row, column=_COLUMNS.index("classification") + 1).fill = PatternFill("solid", fgColor="FCE4D6")
    widths = {"floor": 18, "name": 26, "layer": 16, "competing_classes": 30, "indicators_matched": 60,
              "indicators_missing": 50, "prerequisite_elements": 30, "notes": 60}
    for k, col in enumerate(_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(k)].width = widths.get(col, 13)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(_COLUMNS)), ws.max_row)
    s = wb.create_sheet("Summary")
    summ = summary(records)
    s.append([title])
    s["A1"].font = Font(bold=True, size=13)
    s.append([])
    for k in ("elements", "auto", "review", "unresolved", "disputed", "marked"):
        label = {"elements": "Elements", "auto": "Auto-classified (50+)", "review": "Needs review (20-49)",
                 "unresolved": "Held - unresolved (under 20)", "disputed": "Classified otherwise than drawn",
                 "marked": "Marked on AI_REVIEW"}[k]
        s.append([label, summ.get(k, 0)])
    s.append([])
    s.append(["Class", "High", "Medium", "Low", "Unresolved"])
    for c in s[s.max_row]:
        c.font = Font(bold=True)
    for cls in list(CLASSES) + [UNRESOLVED]:
        row = summ["by_class"].get(cls)
        if row:
            s.append([cls] + [row.get(b, 0) for b in ("High", "Medium", "Low", "Unresolved")])
    s.column_dimensions["A"].width = 34
    wb.save(path)
    return path
