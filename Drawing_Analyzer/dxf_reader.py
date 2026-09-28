# -*- coding: utf-8 -*-
"""
Drawing_Analyzer - DXF parsing + text extraction + annotation + export.

v4 changes vs v3:
- Drop layer-based X filter. Detect X-marks anywhere in the drawing using
  shape signature: two diagonals with matching length, close midpoints,
  and OPPOSITE slopes (so stair treads, which are parallel, don't pass).
- Block-deep X-diagonal collection (modelspace + 1-level INSERT blocks).
- Replace MIN/MAX_SHAFT_SQM size cap with wall-enclosure validation.
  For each X-cluster, ray-cast to find the smallest wall-bounded rectangle
  around it. Use the enclosure as the shaft bbox.
- If no wall enclosure is found AND the X-cluster bbox itself is plausible
  (>= 0.5 sq.m), keep it as "Unknown Shaft" (option B). Otherwise discard.

v3 still in place:
- Stable matching X <-> text (best pairs first, not greedy by cluster size)
- Stricter label_score (penalty for long descriptive texts)
- Block-internal LWPOLYLINE indentation fix in segment collection

v5 - boundaries follow real walls:
- A wall is two parallel lines a wall's thickness apart (80-400 mm by
  default), on any layer, or any line on a structure layer (WALL, RCC,
  COLUMN...) whatever its thickness. Single lines - WC cubicle partitions,
  urinal screens, door leaves, counters, grid and centre lines - no longer
  stop a room.
- Two lines of similar length a few millimetres apart are a panel (door
  leaf, partition board) and are ignored.
- A door opening is closed only where a door swing spans it, by carrying
  both wall faces straight across. Openings with no door stay open, so a
  urinal bay or alcove belongs to the room it opens off.
- The invented "collinear gap" walls and the 900 mm opening seal are gone:
  on the test plate they produced 3,684 virtual walls, three of which cut
  the right third off the men's toilet.
- Labels are claimed by the traced outline, not its bounding box, and the
  best name inside a traced room titles it.

Public API unchanged:
    get_floor_candidates(dxf_path)              -> list of FloorCandidate
    extract_texts(dxf_path, floors)             -> { floor_name: [row, ...] }
    annotate_dxf(src, dst, floors, results)     -> stats dict
    write_json(results, out_path)
    write_excel(results, out_path)
"""

from __future__ import annotations
import json, math, os, re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import ezdxf
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .config import CONFIG
from .dxfio import (IDENTITY, effective_layer, iter_entities,
                    load_document, load_document_ex, ocs_to_wcs)
from .labels import (TIER_LONG, LabelRules, build_rules, choose_label,
                     keyword_matches, label_rank, label_tier,
                     looks_like_room_name)
from ezdxf.math import bulge_to_arc

from .geometry import (BandIndex, bulge_to_arc_points, polygon_centroid,
                       polygon_perimeter_m, polyline_length_m,
                       shoelace_area_sqm)
from .layers import collect_layers
from .levels import (TITLE_WORDS, clean_title, floor_name, parse_levels,
                     tower_of)
from .roomtrace import RoomTracer
from . import rules as rule_book
from . import userrules
from .units import DrawingUnits

# -- Config ----------------------------------------------------------------
# These were hard-coded here; they now come from config.json so a new
# project can be retuned without editing Python. The module-level names are
# kept so existing call sites keep working. All distances are millimetres,
# applied AFTER the drawing has been normalised to mm from $INSUNITS.
_FD   = CONFIG["floor_detection"]
_BD   = CONFIG["boundary"]
_DR   = CONFIG["doors"]
_XM   = CONFIG["x_marks"]
_AN   = CONFIG["annotation"]
_CV   = CONFIG["curves"]

MIN_FLOOR_AREA_SQM = float(_FD["min_floor_area_sqm"])
EXCLUDED_LAYERS    = set(_FD["excluded_layers"])
FFL_PATTERN        = re.compile(r"^[+\-]?\d+(?:\.\d+)?\s*(FFL|SSL)\b", re.IGNORECASE)
HAS_LETTER         = re.compile(r"[A-Za-z]")
SKIP_WORDS         = {"UP", "DN", "UP.", "DN.", "NTS"}

ANNOTATE_LAYER      = _AN["layer"]
ANNOTATE_COLOR      = int(_AN["color"])
ANNOTATE_HATCH      = bool(_AN.get("hatch", True))
HATCH_TRANSPARENCY  = min(0.9, max(0.0, float(_AN.get("hatch_transparency", 0.0))))
TEXT_HEIGHT         = float(_AN["text_height_mm"])
MAX_HV_SEARCH       = float(_BD["max_hv_search_mm"])
MAX_ROOM_SQM        = float(_BD["max_room_sqm"])
MAX_LEAKED_SQM      = float(_BD.get("max_leaked_raycast_sqm", 60.0))
TRACE_MAX_VS_BOX    = float(_BD.get("trace_max_vs_box_ratio", 3.0))
USE_FLOOR_EDGES     = bool(_BD.get("floor_outline_as_wall", True))
MIN_BOUNDARY_SIDE   = float(_BD["min_boundary_side_mm"])
WALL_AXIS_RATIO     = float(_BD["wall_axis_ratio"])
INDEX_BAND          = float(_BD["spatial_index_band_mm"])
SAGITTA_TOL         = float(_CV["sagitta_tolerance_mm"])

# Door openings are closed where a door swing spans them (see
# _door_bridges). A swing is an arc on a door layer or in a door block.
DOOR_SWING_MIN      = float(_DR.get("swing_radius_min_mm", 300.0))
DOOR_SWING_MAX      = float(_DR.get("swing_radius_max_mm", 1400.0))
DOOR_HINGE_TOL      = float(_DR.get("hinge_tol_mm", 250.0))
DOOR_BRIDGE_MIN     = float(_DR.get("bridge_min_mm", 500.0))
DOOR_BRIDGE_MAX     = float(_DR.get("bridge_max_mm", 2600.0))
# Doors and windows both sit in a wall opening, and both close it: a window
# between an AHU room and its balcony is as much a boundary as the wall.
DOOR_KEYWORDS       = tuple(str(k).upper() for k in _DR.get(
    "opening_keywords", _DR.get("swing_keywords",
                                ["DOOR", "WINDOW", "GLAZ"])))
# Wall-run gaps with no door (only when the close_wall_gaps rule is on).
WALL_GAP_MIN        = float(_DR.get("gap_min_mm", 300.0))
WALL_GAP_MAX        = float(_DR.get("gap_max_mm", 2000.0))

# X-mark detection (drawing-driven, no fixed size range)
X_DIAG_MIN_LEN          = float(_XM["diag_min_len_mm"])
X_DIAG_MAX_LEN          = float(_XM["diag_max_len_mm"])
X_DIAG_MIN_AXIS         = float(_XM["diag_min_axis_mm"])
X_PAIR_MIDPOINT_TOL     = float(_XM["pair_midpoint_tol_mm"])
X_PAIR_LENGTH_TOL       = float(_XM["pair_length_tol"])
X_CLUSTER_GAP           = float(_XM["cluster_gap_mm"])
X_TEXT_RADIUS           = float(_XM["text_radius_mm"])
X_TEXT_GAP              = float(_XM.get("text_gap_mm", 1000.0))
X_ENCLOSURE_MAX_SQM     = float(_XM["enclosure_max_sqm"])
X_ENCLOSURE_SNAP        = float(_XM.get("enclosure_snap_margin_mm", 250.0))
X_FALLBACK_MIN_SQM      = float(_XM["fallback_min_sqm"])

OVERLAP_MERGE_RATIO = float(_BD["overlap_merge_ratio"])
MERGE_MIN_SIZE      = float(_BD.get("overlap_merge_min_size_ratio", 0.5))
ENGULF_RATIO        = float(_BD.get("engulf_reject_ratio", 1.5))
ENGULF_MIN_ROOMS    = int(_BD.get("engulf_min_rooms", 2))
X_UNKNOWN_LABEL     = "Unknown Shaft"
VOID_LAYER          = "ROOM_VOID"
BOUNDARY_METHOD     = str(_BD.get("method", "trace")).lower()
TRACE_CELL_MM       = float(_BD.get("trace_cell_mm", 50.0))
SEAL_OPENINGS_MM    = float(_BD.get("seal_openings_mm", 100.0))
TRACE_SIMPLIFY      = float(_BD.get("trace_simplify_cells", 1.2))

MM_UNITS = DrawingUnits(1.0, "Millimetres")

# Layers excluded from "thick wall" consideration (stair internal lines etc.)
# Keyword-based  -  any layer name CONTAINING any of these strings is excluded.
_WALL_EXCLUDE_KEYWORDS = (
    "STAIR",          # stair tread lines (A-STAIRS, A3-STAIRS, STAIR, etc.)
    "RAILING",        # railing lines
    "CUTOUT",         # cutout markers
    "ST-SLAB LINE",   # stair slab lines
    "DEFPOINTS",      # AutoCAD internal layer
)

# Layers hard-excluded from being walls at all  -  annotation/symbol layers.
_HARD_EXCLUDE_KEYWORDS = (
    "DIM",                 # dimension lines (A-DIM, A-DIM S, etc.)
    "DIMENSION",           # dimensions
    "FURN",                # furniture / fixtures
    "FURNITURE",
    "TEXT",                # text-related (A-TEXT M, A-TEXT R, etc.)
    "ANNO",                # annotation
    "NOTE",                # notes
    "GRID",                # grid lines (span the whole drawing)
    "HATCH",               # hatch patterns (decorative)
    "ELEV SYMBOL",         # elevation symbols
    "BREAKLINE",           # break lines
    "SECTSYMBOL",          # section markers
    # NOTE: 'SECTION' and 'DETAIL' kept OUT of this list  -  some drawings use
    # 'a-details' / 'A-SECTION' for actual structural wall outlines, not
    # annotations. Being conservative: rely on more specific markers.
    "FIRESGN",             # fire signage
    "FIRESIGN",            # fire signage
    "PHOENIX",             # Phoenix-specific overlay layers
    "TILE DROP",           # tile pattern overlays
    "LIFT SPECS",          # lift spec annotations
    # "HVAC COMMENT" was here as an annotation layer. On the test plate it
    # carries the outer walls of both women's toilets and nothing else, so
    # excluding it left those rooms with no wall on one side.
    "UP DIRECTION",        # up/down arrows
    "LEADER",              # leader lines
    "MARK",                # marker symbols (e.g., REVISION MARK)
    "CENTRE",              # centre lines: the cross drawn through a column
    "CENTER",
)

# Both lists live in config.json when it carries them, because which
# layers are walls is a property of the drawing office, not of this
# program: a vanity counter on Q-CASE cuts a toilet in half, and the only
# person who can say that Q-CASE is joinery rather than blockwork is
# whoever set the drawing up. The lists above are the defaults.
_WD = CONFIG.get("walls") or {}
if _WD.get("exclude_from_thick"):
    _WALL_EXCLUDE_KEYWORDS = tuple(str(k) for k in _WD["exclude_from_thick"])
if _WD.get("never_walls"):
    _HARD_EXCLUDE_KEYWORDS = tuple(str(k) for k in _WD["never_walls"])

# How a wall is recognised. A wall is drawn as two faces a wall's thickness
# apart; a partition board, a door leaf or a counter edge is not.
WALL_T_MIN        = float(_WD.get("thickness_min_mm", 80.0))
WALL_T_MAX        = float(_WD.get("thickness_max_mm", 400.0))
WALL_MIN_FACE     = float(_WD.get("min_face_mm", 200.0))
WALL_PANEL_MAX    = float(_WD.get("panel_max_mm", 60.0))
WALL_PARALLEL_DEG = float(_WD.get("parallel_tol_deg", 1.5))
# Structure is a wall whatever its thickness: RCC cores and shear walls run
# to 1,000 mm, far past any thickness test.
_ALWAYS_WALL_KEYWORDS = tuple(str(k).upper() for k in _WD.get(
    "always_walls", ["WALL", "RCC", "SLIP FORM", "COLUMN", "COL-", "-COL",
                     "SHEAR", "CONC", "STRUCT", "UPSTAND", "CUTOUT"]))


# -- Data classes ----------------------------------------------------------
@dataclass
class FloorCandidate:
    index: int
    area_sqm: float
    layer: str
    num_points: int
    entity_type: str
    polygon: List[Tuple[float, float]] = field(default_factory=list)
    perimeter_m: float = 0.0
    # Suggested from the plan title under the plate ("Typical 2nd, 4th &
    # 6th Floor"); "" when the sheet has no title for it.
    name: str = ""
    title: str = ""
    def to_ui(self):
        return {"index": self.index, "area_sqm": round(self.area_sqm, 2),
                "perimeter_m": round(self.perimeter_m, 2),
                "layer": self.layer, "num_points": self.num_points,
                "entity_type": self.entity_type,
                "name": self.name, "title": self.title}


@dataclass
class TextRecord:
    text: str
    source: str
    layer: str
    x: float
    y: float
    rotation: float = 0.0
    height: float = 0.0


# -- Geometry helpers ------------------------------------------------------
def _polyline_points(entity, units=None, xform=None):
    """Vertices in MILLIMETRES, with arc bulges flattened.

    The bulge value used to be dropped, so an arc-cornered outline was
    measured as if it were chorded and its area came out short. Bulges are
    now expanded into intermediate vertices at the configured tolerance.

    Bulges are flattened in the polyline's own coordinates and only then
    placed in the world. Doing it the other way round bent every arc in a
    mirrored block the wrong way. Points stored in an OCS - a polyline
    whose extrusion is not +Z, as mirroring can leave it - are converted
    before placing, and old-style 2D POLYLINE bulges are honoured too.
    """
    units = units or MM_UNITS
    xform = xform or IDENTITY
    # Tolerance in the polyline's own units, so the flattened arc is within
    # SAGITTA_TOL millimetres once scaled into the world.
    tol = SAGITTA_TOL / max(units.mm_per_unit * xform.scale_avg, 1e-9)

    kind = entity.dxftype()
    if kind == "LWPOLYLINE":
        try:
            raw = [(p[0], p[1], p[2]) for p in entity.get_points("xyb")]
        except TypeError:
            raw = [(p[0], p[1], 0.0) for p in entity.get_points()]
        closed = bool(getattr(entity, "closed", False))
        elev = float(entity.dxf.get("elevation", 0.0) or 0.0)
        plane = True
    elif kind == "POLYLINE":
        try:
            if entity.is_poly_face_mesh or entity.is_polygon_mesh:
                return []
            plane = bool(entity.is_2d_polyline)
        except AttributeError:
            plane = True
        raw = []
        for v in entity.vertices:
            loc = v.dxf.location
            raw.append((loc.x, loc.y,
                        float(v.dxf.get("bulge", 0.0) or 0.0) if plane else 0.0))
        closed = bool(getattr(entity, "is_closed", False))
        elev = 0.0
        if plane:
            try:
                elev = float(entity.dxf.elevation.z)
            except (AttributeError, TypeError, ValueError):
                elev = 0.0
    else:
        return []

    n = len(raw)
    if n == 0:
        return []
    limit = n if closed else n - 1
    local = []
    for i in range(n):
        local.append((raw[i][0], raw[i][1]))
        b = raw[i][2]
        if i < limit and b:
            j = (i + 1) % n
            local.extend(bulge_to_arc_points((raw[i][0], raw[i][1]),
                                             (raw[j][0], raw[j][1]), b,
                                             sagitta_tol=tol))

    out = []
    for x, y in local:
        if plane:
            wx, wy, wz = ocs_to_wcs(entity, x, y, elev)
        else:
            wx, wy, wz = x, y, 0.0
        out.append(units.point_to_mm(*xform.apply(wx, wy, wz)))
    return out

def _is_closed(entity):
    if entity.dxftype() == "LWPOLYLINE":
        return bool(entity.closed)
    return bool(entity.is_closed)

def _point_in_polygon(x, y, poly):
    n = len(poly); inside = False; j = n - 1
    for i in range(n):
        xi, yi = poly[i]; xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside

def _polygon_bbox(poly):
    xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)

def _dist(ax, ay, bx, by):
    return math.sqrt((bx - ax) ** 2 + (by - ay) ** 2)


# -- Phase 1: floor candidates ---------------------------------------------
def get_floor_candidates(dxf_path, unit_override=None, layer_inventory=None,
                         min_area_sqm=None, max_area_sqm=None,
                         drawing_name="", loaded_out=None):
    """Closed polylines whose area lies in the floor-plate range.

    The range is the one entered in Step 1 (min_area_sqm..max_area_sqm);
    without it, min_floor_area_sqm from config.json and no upper limit.

    Returns (candidates, units, diagnostics). Areas are in square metres
    regardless of the units the drawing was authored in; the unit itself
    is settled in load_document_ex, which weighs the override, the
    $INSUNITS header and the geometry in that order of authority.

    `layer_inventory`, when a dict is passed in, is filled with the
    drawing's layer inventory from this same parse. Reading the layer
    table needs the document open, and opening a 50 MB drawing a second
    time costs more than everything else in the upload put together.
    """
    doc, msp, units, info = load_document_ex(dxf_path, unit_override)
    # The parsed drawing, for a caller that has more to read from it (the
    # CAD viewer) and should not parse a 40 MB file a second time.
    if loaded_out is not None:
        loaded_out["loaded"] = (doc, msp, units)
    if layer_inventory is not None:
        try:
            layer_inventory.update(collect_layers(
                doc, msp, _FAMILY_RULES, family_for_label,
                never_walls=_HARD_EXCLUDE_KEYWORDS,
                thin_only=_WALL_EXCLUDE_KEYWORDS))
        except Exception:                             # noqa: BLE001
            # A layer table this program cannot read is not a reason to
            # refuse the upload; the floor plates are what matter here.
            layer_inventory.setdefault("layers", [])
            layer_inventory.setdefault("groups", [])
            layer_inventory.setdefault("summary", {})

    lo = float(min_area_sqm) if min_area_sqm is not None else MIN_FLOOR_AREA_SQM
    hi = float(max_area_sqm) if max_area_sqm is not None else float("inf")
    candidates = []
    idx = 0
    all_areas = []
    for e in msp.query("LWPOLYLINE POLYLINE"):
        if not _is_closed(e):
            continue
        try:
            pts = _polyline_points(e, units)
        except (AttributeError, TypeError, ValueError):
            continue
        if len(pts) < 3:
            continue
        area_sqm = shoelace_area_sqm(pts)
        all_areas.append((area_sqm, e.dxf.layer))
        if area_sqm < lo or area_sqm > hi:
            continue
        candidates.append(FloorCandidate(
            index=idx, area_sqm=area_sqm, layer=e.dxf.layer,
            num_points=len(pts), entity_type=e.dxftype(), polygon=pts,
            perimeter_m=polygon_perimeter_m(pts)))
        idx += 1
    candidates.sort(key=lambda c: c.area_sqm, reverse=True)
    before = len(candidates)
    candidates = _dedupe_plates(candidates)
    for ni, c in enumerate(candidates):
        c.index = ni
    if candidates:
        try:
            _name_candidates(candidates, doc, msp, units, drawing_name)
        except Exception:                             # noqa: BLE001
            # Names are only a suggestion; the user can type their own.
            pass

    all_areas.sort(reverse=True)
    diagnostics = dict(info)
    diagnostics.update({
        "min_floor_area_sqm": lo,
        "max_floor_area_sqm": None if hi == float("inf") else hi,
        "largest_areas_sqm": [round(a, 2) for a, _ in all_areas[:8]],
        "above_threshold": len(candidates),
        "duplicate_plates_dropped": before - len(candidates),
    })
    return candidates, units, diagnostics


def _name_candidates(candidates, doc, msp, units, drawing_name=""):
    """Name each plate after its plan title, the same title Step 4 reads
    for the Area Statement: "1ST FLOOR PLAN" -> "1st Floor", "TYPICAL
    FLOOR PLAN-(2nd,4th & 6th)" -> "Typical 2nd, 4th & 6th Floor". When the
    sheet holds more than one tower the tower leads the name.

    A single-floor sheet usually has no title under the plate; the floor is
    in the file name instead ("A-106 4th FLOOR PLAN.dxf"), and the largest
    untitled plate - the one ticked by default - takes it. Names are kept
    unique; other untitled plates are left unnamed."""
    # Plan titles are almost always plain modelspace text, which is read in
    # an instant; the text inside blocks costs seconds on a big sheet, so it
    # is only read when a plate is still without a title.
    texts = _collect_texts_in_modelspace(msp, units)
    plates = [{"pts": c.polygon} for c in candidates]
    _describe_floors(plates, texts)
    if not all(p.get("title") for p in plates):
        texts += _collect_texts_from_blocks(doc, msp, units)
        _describe_floors(plates, texts)
    # "TOWER 1" and "TOWER 01" on one sheet are the same tower.
    towers = {re.sub(r"\b0+(\d)", r"\1", p["tower"])
              for p in plates if p.get("tower")}
    from_file = floor_name(re.sub(r"\.dxf$", "", drawing_name or "",
                                  flags=re.I).replace("_", " "))
    used = set()
    for c, p in zip(candidates, plates):
        c.title = p.get("title") or ""
        name = floor_name(c.title)
        if not name and from_file:
            name, from_file = from_file, ""
        if not name:
            continue
        if len(towers) > 1 and p.get("tower"):
            name = "{0} - {1}".format(p["tower"].title(), name)
        base, n = name, 2
        while name.lower() in used:
            name = "{0} ({1})".format(base, n)
            n += 1
        used.add(name.lower())
        c.name = name


def _dedupe_plates(candidates):
    """One entry per floor plate.

    A sheet often draws the same plate outline more than once - the slab
    edge on A-FLOOR PROJ twice over, the glass line on A-Glass just inside
    it. Listed separately they were picked as separate floors, and every
    room on that plate was then drawn twice. Two outlines are one plate
    when their areas agree within 3% and their bounding boxes coincide.
    The copy kept is the one on the layer that carries the most plates,
    which is the drawing's floor layer.
    """
    if len(candidates) < 2:
        return candidates
    layer_count = {}
    for c in candidates:
        layer_count[c.layer] = layer_count.get(c.layer, 0) + 1
    groups = []
    for c in candidates:
        bb = _polygon_bbox(c.polygon)
        for g in groups:
            h = g[0]
            if (abs(h.area_sqm - c.area_sqm) <= 0.03 * max(h.area_sqm, c.area_sqm)
                    and bbox_overlap_ratio(_polygon_bbox(h.polygon), bb) >= 0.95):
                g.append(c)
                break
        else:
            groups.append([c])
    return [max(g, key=lambda c: (layer_count[c.layer], c.area_sqm))
            for g in groups]


# -- Text helpers ----------------------------------------------------------
def _text_value(entity):
    if entity.dxftype() == "MTEXT":
        try: return entity.plain_text()
        except (AttributeError, ValueError, TypeError):
            return entity.dxf.text or ""
    return entity.dxf.text or ""

def _text_position(entity):
    """Insertion point in the container's coordinates.

    MTEXT is stored in world coordinates; TEXT and ATTRIB in their OCS,
    which only differs from world for mirrored or tilted text.
    """
    if entity.dxftype() == "MTEXT":
        ip = entity.dxf.insert
        return float(ip[0]), float(ip[1])
    x, y = _text_position_ocs(entity)
    wx, wy, _wz = ocs_to_wcs(entity, x, y)
    return wx, wy


def _text_position_ocs(entity):
    try: halign = int(entity.dxf.get("halign", 0) or 0)
    except (TypeError, ValueError): halign = 0
    try: valign = int(entity.dxf.get("valign", 0) or 0)
    except (TypeError, ValueError): valign = 0
    if halign != 0 or valign != 0:
        ap = entity.dxf.get("align_point", None)
        if ap is not None:
            try: return float(ap[0]), float(ap[1])
            except (TypeError, ValueError, IndexError): pass
    ip = entity.dxf.insert
    return float(ip[0]), float(ip[1])

def _text_height(entity):
    try:
        if entity.dxftype() == "MTEXT":
            return float(entity.dxf.get("char_height", 0.0) or 0.0)
        return float(entity.dxf.get("height", 0.0) or 0.0)
    except (TypeError, ValueError): return 0.0

def _text_rotation(entity):
    try: return float(entity.dxf.get("rotation", 0.0) or 0.0)
    except (TypeError, ValueError): return 0.0


# -- Text collection -------------------------------------------------------
def _collect_texts_in_modelspace(msp, units=None):
    units = units or MM_UNITS
    out = []
    for e in msp.query("TEXT MTEXT"):
        txt = _text_value(e).strip()
        if not txt: continue
        tx, ty = units.point_to_mm(*_text_position(e))
        out.append(TextRecord(text=txt, source=e.dxftype(),
                              layer=e.dxf.get("layer", "0") or "0",
                              x=tx, y=ty, rotation=_text_rotation(e),
                              height=_text_height(e) * units.mm_per_unit))
    return out

def _collect_texts_from_blocks(doc, msp, units=None):
    """Text inside blocks, to the configured nesting depth.

    Previously this descended exactly one INSERT level, so labels inside
    nested blocks were invisible to the whole pipeline.
    """
    units = units or MM_UNITS
    out = []
    for e, xform, _depth in iter_entities(doc, msp, "TEXT MTEXT"):
        if xform is IDENTITY:
            continue                      # modelspace text handled above
        txt = _text_value(e).strip()
        if not txt: continue
        lx, ly = _text_position(e)
        wx, wy = units.point_to_mm(*xform.apply(lx, ly))
        world_rot = (_text_rotation(e) + xform.rot_deg) % 360.0
        out.append(TextRecord(text=txt, source="BLOCK",
                               layer=effective_layer(e, xform),
                               x=wx, y=wy, rotation=world_rot,
                               height=_text_height(e) * units.mm_per_unit
                                      * xform.scale_avg))
    for ins in msp.query("INSERT"):
        for a in ins.attribs:
            txt = (a.dxf.text or "").strip()
            if not txt: continue
            ax, ay = units.point_to_mm(*_text_position(a))
            out.append(TextRecord(text=txt, source="ATTRIB",
                                   layer=a.dxf.get("layer", "0") or "0",
                                   x=ax, y=ay, rotation=_text_rotation(a),
                                   height=_text_height(a) * units.mm_per_unit))
    return out


# -- Filtering -------------------------------------------------------------
# Text on a sanitary-fixture layer labels the fixture, not the room it
# stands in (see "fixture_layers" in config.json).
FIXTURE_LAYER_KEYWORDS = tuple(str(k).upper() for k in (
    (CONFIG.get("label_filters") or {}).get("fixture_layers")
    or ["FIXTURE", "FIXT", "SAN-FIX", "SAN_FIX", "SANITARY", "SANR"]))


def _is_fixture_layer(layer):
    lu = (layer or "").upper()
    return any(k in lu for k in FIXTURE_LAYER_KEYWORDS)


def _should_keep(rec):
    if rec.layer.strip().upper() in {l.upper() for l in EXCLUDED_LAYERS}: return False
    if _is_fixture_layer(rec.layer): return False
    t = rec.text.strip()
    if not t or FFL_PATTERN.match(t) or not HAS_LETTER.search(t): return False
    if t.upper() in SKIP_WORDS: return False
    return True


# -- Multi-line label merging (unchanged) ----------------------------------
def _merge_stacked_lines(records):
    def is_candidate(r): return r.source in ("TEXT", "MTEXT", "BLOCK", "ATTRIB") and r.height > 0
    mergeable = [r for r in records if is_candidate(r)]
    untouched = [r for r in records if not is_candidate(r)]
    def angle_close(a, b, tol=3.0): return abs((a - b + 180.0) % 360.0 - 180.0) <= tol
    def same_height(h1, h2, tol=0.15):
        if h1 <= 0 or h2 <= 0: return False
        return abs(h1 - h2) / max(h1, h2) <= tol
    def approx_text_width(rec): return max(1, len(rec.text)) * rec.height * 0.6
    def axes(rot_deg):
        r = math.radians(rot_deg)
        return math.cos(r), math.sin(r), -math.sin(r), math.cos(r)
    def are_neighbors(ri, rj):
        if ri.source != rj.source or ri.layer != rj.layer: return False
        if not angle_close(ri.rotation, rj.rotation): return False
        if not same_height(ri.height, rj.height): return False
        bx, by, px, py = axes(ri.rotation)
        dx, dy = rj.x - ri.x, rj.y - ri.y
        along = dx * bx + dy * by; perp = dx * px + dy * py
        h = ri.height
        if abs(perp) < 0.3 * h or abs(perp) > 2.2 * h: return False
        wi, wj = approx_text_width(ri), approx_text_width(rj)
        if abs(along) > 0.5 * max(wi, wj): return False
        return True
    parent = list(range(len(mergeable)))
    def find(a):
        while parent[a] != a: parent[a] = parent[parent[a]]; a = parent[a]
        return a
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[ra] = rb
    buckets = {}
    for i, r in enumerate(mergeable):
        rb_ = round(r.rotation / 3.0); hb_ = round(r.height / 10.0)
        along_axis = r.x * math.cos(math.radians(r.rotation)) + r.y * math.sin(math.radians(r.rotation))
        cb_ = round(along_axis / max(r.height * 2.0, 1.0))
        key = (r.source, r.layer, rb_, hb_, cb_)
        buckets.setdefault(key, []).append(i)
    for key in list(buckets.keys()):
        src, layer, rot_b, h_b, col_b = key
        cands_ = []
        for dcol in (-1, 0, 1):
            cands_.extend(buckets.get((src, layer, rot_b, h_b, col_b + dcol), []))
        seen = set(); uniq = [c for c in cands_ if c not in seen and not seen.add(c)]
        for a in range(len(uniq)):
            ia = uniq[a]
            for b in range(a + 1, len(uniq)):
                ib = uniq[b]
                if find(ia) == find(ib): continue
                if are_neighbors(mergeable[ia], mergeable[ib]): union(ia, ib)
    groups = {}
    for i in range(len(mergeable)):
        groups.setdefault(find(i), []).append(i)
    def is_dim_like(s):
        st = s.strip().upper()
        if not st: return False
        letters = [c for c in st if c.isalpha()]
        return len(letters) > 0 and all(c == "X" for c in letters)
    out = []
    for _, idxs in groups.items():
        if len(idxs) == 1: out.append(mergeable[idxs[0]]); continue
        members = [mergeable[i] for i in idxs]
        anchor = members[0]
        _, _, px, py = axes(anchor.rotation)
        members.sort(key=lambda rec: rec.x * px + rec.y * py, reverse=True)
        if len(members) >= 2:
            if is_dim_like(members[0].text) and not is_dim_like(members[-1].text):
                members.reverse()
        combined = " ".join(m.text.strip() for m in members)
        top = members[0]
        out.append(TextRecord(text=combined, source=top.source, layer=top.layer,
                              x=top.x, y=top.y, rotation=top.rotation, height=top.height))
    return out + untouched


# -- Phase 2: extractor ----------------------------------------------------
def extract_texts(dxf_path, floors, unit_override=None):
    doc, msp, units = load_document(dxf_path, unit_override)
    all_records = (_collect_texts_in_modelspace(msp, units)
                   + _collect_texts_from_blocks(doc, msp, units))
    merged = _merge_stacked_lines(all_records)
    kept = [r for r in merged if _should_keep(r)]
    results = {}
    for floor in floors:
        name = floor["name"]; poly = [tuple(p) for p in floor["polygon"]]
        xmin, ymin, xmax, ymax = _polygon_bbox(poly)
        rows = []; sr = 1
        for r in kept:
            if not (xmin <= r.x <= xmax and ymin <= r.y <= ymax): continue
            if not _point_in_polygon(r.x, r.y, poly): continue
            rows.append({"sr_no": sr, "text": r.text, "source": r.source,
                         "layer": r.layer, "x": round(r.x, 3),
                         "y": round(r.y, 3),
                         "height": round(r.height, 2),
                         "family": family_for_label(r.text,
                                                     _FAMILY_RULES)["name"]})
            sr += 1
        results[name] = rows
    return results


# ===========================================================================
# Phase 3: v4 annotation
# ===========================================================================

# Built from config.json and family_rules.json at the bottom of this
# module, once the family rules exist. Extending the family keywords
# automatically improves label choice, because a text the family rules
# recognise is a room name by definition.
_LABEL_RULES = LabelRules()


def label_score(text, height=0.0, distance=0.0):
    """Sort key for candidate labels. Lower is better.

    The old version preferred short strings containing digits, which
    ranked the equipment tag INID04 above the room name PRESS - and then
    dropped that boundary into UNKNOWN_BOUNDARY, because INID04 matches
    no family keyword. Ranking now leads on whether the text is a
    recognised room name, then text height, then distance.
    """
    return label_rank(text, height, distance, _LABEL_RULES)


def looks_like_primary(text):
    """Whether this text should get its own boundary ahead of the rest."""
    return looks_like_room_name(text, _LABEL_RULES)


def is_note_text(text):
    """A sentence of drawing notes rather than the name of a space."""
    return label_tier(text, _LABEL_RULES) == TIER_LONG


def is_device_tag(text):
    """A device tag rather than the name of a space.

    A code the family rules recognise is a room: ST-01 reads as a device
    tag to the generic pattern, but ST- is how the Staircase family is
    spelled, so it names a stair. Skipping every pattern match took all
    four staircases and several lifts off the drawing. Only an explicit
    equipment tag, or a code that matches no family at all, is a device -
    which keeps the decision in family_rules.json where it can be edited
    per project rather than hard-coded here.
    """
    if _LABEL_RULES.is_equipment(text):
        return True
    return bool(_LABEL_RULES.is_tag(text)
                and not _LABEL_RULES.family_keyword(text))


def is_ignorable_label(text):
    """Dimensions, levels, UP/DN - never a room name."""
    return _LABEL_RULES.is_ignored(text)


def bbox_overlap_ratio(b1, b2):
    ax1, ay1, ax2, ay2 = b1; bx1, by1, bx2, by2 = b2
    ix1 = max(ax1, bx1); iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2); iy2 = min(ay2, by2)
    if ix1 >= ix2 or iy1 >= iy2: return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    a1 = (ax2 - ax1) * (ay2 - ay1); a2 = (bx2 - bx1) * (by2 - by1)
    return inter / min(a1, a2)


def text_in_bbox(tx, ty, bbox):
    mn_x, mn_y, mx_x, mx_y = bbox
    return mn_x <= tx <= mx_x and mn_y <= ty <= mx_y


def _collect_raw_segs(doc, msp, units=None):
    """Wall segments in millimetres, from modelspace and nested blocks.

    Now descends INSERTs to the configured depth rather than one level,
    and flattens polyline bulges so curved walls contribute real geometry
    instead of a single chord.

    Each segment is (sx, sy, ex, ey, layer, length, lineweight, is_door):
    is_door marks a line on a door layer or inside a door block, which is
    evidence of an opening and never a wall face (see _detect_walls).
    """
    units = units or MM_UNITS
    segs = []

    def push(pts, layer, lw, door):
        for i in range(len(pts) - 1):
            sx, sy = pts[i]; ex, ey = pts[i + 1]
            segs.append((sx, sy, ex, ey, layer, _dist(sx, sy, ex, ey), lw,
                         door))

    for e, xform, _depth in iter_entities(doc, msp, "LINE LWPOLYLINE POLYLINE"):
        try:
            # Layer 0 inside a block is shown on the INSERT's layer; a WC
            # cubicle block placed on a furniture layer is furniture.
            layer = effective_layer(e, xform)
            door = _is_door_leaf(layer, xform.blocks)
            try: lw = int(e.dxf.lineweight)
            except (TypeError, ValueError, AttributeError): lw = -1
            if e.dxftype() == "LINE":
                s0, s1 = e.dxf.start, e.dxf.end
                a = units.point_to_mm(*xform.apply(s0.x, s0.y, s0.z))
                b = units.point_to_mm(*xform.apply(s1.x, s1.y, s1.z))
                push([a, b], layer, lw, door)
            else:
                pts = _polyline_points(e, units, xform)
                if _is_closed(e) and len(pts) > 1:
                    pts = pts + [pts[0]]
                push(pts, layer, lw, door)
        except (AttributeError, TypeError, ValueError):
            continue
    return segs


def _collect_diagonals_layered(raw_segs):
    """v3-style layer-restricted X-diagonal collection. Reads from raw_segs
    (already includes block-deep contributions) and keeps only diagonals on
    layers containing CUTOUT, or starting with E- and containing SHAFT.

    This is the FAST PATH: when a drawing follows the standard convention
    (X marks on shaft/cutout layers), this returns clean results.
    If it returns nothing, the caller falls back to _collect_diagonals_anywhere.
    """
    out = []
    for seg in raw_segs:
        sx, sy, ex, ey, layer, l = seg[0], seg[1], seg[2], seg[3], seg[4], seg[5]
        lu = (layer or "").upper()
        is_x = "CUTOUT" in lu or ("SHAFT" in lu and lu.startswith("E-"))
        if not is_x: continue
        if l < X_DIAG_MIN_LEN or l > X_DIAG_MAX_LEN: continue
        dx, dy = abs(ex - sx), abs(ey - sy)
        if dx < X_DIAG_MIN_AXIS or dy < X_DIAG_MIN_AXIS: continue
        cx, cy = (sx + ex) / 2, (sy + ey) / 2
        out.append((sx, sy, ex, ey, cx, cy, layer, l))
    return out


def _collect_diagonals_anywhere(doc, msp, units=None):
    """Layer-agnostic diagonal collection, in millimetres, block-recursive.

    Returns (sx, sy, ex, ey, cx, cy, layer, length).
    """
    units = units or MM_UNITS
    out = []

    def keep(sx, sy, ex, ey):
        dx, dy = abs(ex - sx), abs(ey - sy)
        L = math.hypot(dx, dy)
        if L < X_DIAG_MIN_LEN or L > X_DIAG_MAX_LEN: return None
        if dx < X_DIAG_MIN_AXIS or dy < X_DIAG_MIN_AXIS: return None
        return L

    for e, xform, _depth in iter_entities(doc, msp, "LINE"):
        try:
            a0, a1 = e.dxf.start, e.dxf.end
            s = units.point_to_mm(*xform.apply(a0.x, a0.y, a0.z))
            t = units.point_to_mm(*xform.apply(a1.x, a1.y, a1.z))
        except (AttributeError, TypeError, ValueError):
            continue
        L = keep(s[0], s[1], t[0], t[1])
        if L is None: continue
        out.append((s[0], s[1], t[0], t[1],
                    (s[0] + t[0]) / 2, (s[1] + t[1]) / 2,
                    effective_layer(e, xform), L))
    return out


def _find_x_pairs(diag_segs):
    """Pair up diagonals into X-marks. Two diagonals form an X if:
      - midpoints within X_PAIR_MIDPOINT_TOL
      - lengths within X_PAIR_LENGTH_TOL
      - opposite slope signs (one /, one \\)
    Returns list of pairs as ((seg_i, seg_j))  -  i.e. indices into diag_segs.
    Each diagonal pairs with at most one partner."""
    n = len(diag_segs)
    if n < 2: return []
    GS = X_PAIR_MIDPOINT_TOL * 2  # grid cell size for spatial lookup
    grid = {}
    for i, s in enumerate(diag_segs):
        cx, cy = s[4], s[5]
        g = (int(cx // GS), int(cy // GS))
        grid.setdefault(g, []).append(i)

    used = [False] * n
    pairs = []
    for i in range(n):
        if used[i]: continue
        sx, sy, ex, ey, cx, cy, _, L = diag_segs[i]
        slope_i = 1 if (ex - sx) * (ey - sy) > 0 else -1
        g = (int(cx // GS), int(cy // GS))
        best_j = None
        best_d = float("inf")
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((g[0] + dx, g[1] + dy), []):
                    if j == i or used[j]: continue
                    sx2, sy2, ex2, ey2, cx2, cy2, _, L2 = diag_segs[j]
                    md = math.hypot(cx - cx2, cy - cy2)
                    if md > X_PAIR_MIDPOINT_TOL: continue
                    if abs(L - L2) / max(L, L2) > X_PAIR_LENGTH_TOL: continue
                    slope_j = 1 if (ex2 - sx2) * (ey2 - sy2) > 0 else -1
                    if slope_i == slope_j: continue
                    if md < best_d:
                        best_d = md; best_j = j
        if best_j is not None:
            used[i] = used[best_j] = True
            pairs.append((i, best_j))
    return pairs


def _build_x_clusters_from_pairs(diag_segs, pairs, max_gap=X_CLUSTER_GAP):
    """Cluster X-pairs whose centers are within max_gap of each other.
    Each pair contributes its 4 endpoints to the cluster bbox.
    Returns list of cluster dicts."""
    if not pairs: return []
    centers = []
    for i, j in pairs:
        s1 = diag_segs[i]; s2 = diag_segs[j]
        ccx = (s1[4] + s2[4]) / 2
        ccy = (s1[5] + s2[5]) / 2
        centers.append((ccx, ccy, i, j))

    n = len(centers)
    parent = list(range(n))
    def find(a):
        while parent[a] != a: parent[a] = parent[parent[a]]; a = parent[a]
        return a
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[ra] = rb

    for a in range(n):
        for b in range(a + 1, n):
            if abs(centers[a][0] - centers[b][0]) <= max_gap and \
               abs(centers[a][1] - centers[b][1]) <= max_gap:
                union(a, b)
    groups = {}
    for k in range(n):
        groups.setdefault(find(k), []).append(k)

    clusters = []
    for _, idxs in groups.items():
        all_x, all_y = [], []
        for k in idxs:
            _, _, di, dj = centers[k]
            for d in (diag_segs[di], diag_segs[dj]):
                all_x += [d[0], d[2]]
                all_y += [d[1], d[3]]
        mn_x, mx_x = min(all_x), max(all_x)
        mn_y, mx_y = min(all_y), max(all_y)
        cx, cy = (mn_x + mx_x) / 2, (mn_y + mx_y) / 2
        clusters.append({"cx": cx, "cy": cy, "mn_x": mn_x, "mn_y": mn_y,
                         "mx_x": mx_x, "mx_y": mx_y,
                         "w": mx_x - mn_x, "h": mx_y - mn_y,
                         "bbox": (mn_x, mn_y, mx_x, mx_y)})
    return clusters


# -- Walls -----------------------------------------------------------------
def _axis(seg):
    """(angle_deg in [0, 180), ux, uy, length) of a segment, or None."""
    dx, dy = seg[2] - seg[0], seg[3] - seg[1]
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return None
    ux, uy = dx / L, dy / L
    return (math.degrees(math.atan2(uy, ux)) % 180.0, ux, uy, L)


def _parallel_pairs(segs, axes, idxs, dmin, dmax):
    """Pairs (i, j, distance, overlap) of near-parallel segments whose
    perpendicular distance lies in [dmin, dmax] and whose spans overlap.

    Segments are binned by direction (1 degree), by offset across that
    direction, and by position along it, so a segment is only compared
    with the few that could lie a wall's thickness from it and beside it.
    Without the last key every line on a long grid of walls was compared
    with every other line in line with it across the plate: 40 s of the
    wall pass on A-106.
    """
    cos_tol = math.cos(math.radians(WALL_PARALLEL_DEG))
    band = max(dmax, 100.0)
    ALONG = 2000.0
    MAX_BUCKETS = 25            # longer lines are compared with the whole bin
    bins = {}

    def along_span(s, th):
        ax_, ay_ = math.cos(th), math.sin(th)
        p0, p1 = sorted((ax_ * s[0] + ay_ * s[1], ax_ * s[2] + ay_ * s[3]))
        return int(math.floor(p0 / ALONG)), int(math.floor(p1 / ALONG))

    for i in idxs:
        ang = axes[i][0]
        k = int(round(ang)) % 180
        th = math.radians(k)
        nx, ny = -math.sin(th), math.cos(th)
        s = segs[i]
        off = nx * (s[0] + s[2]) / 2.0 + ny * (s[1] + s[3]) / 2.0
        b = bins.setdefault((k, int(math.floor(off / band))), ({}, []))
        g0, g1 = along_span(s, th)
        if g1 - g0 > MAX_BUCKETS:
            b[1].append(i)
        else:
            for g in range(g0, g1 + 1):
                b[0].setdefault(g, []).append(i)

    reach = int(math.ceil((dmax + band) / band))
    seen, out = set(), []
    for i in idxs:
        ang, ux, uy, L = axes[i]
        s = segs[i]
        nx, ny = -uy, ux
        ci = nx * s[0] + ny * s[1]
        a0, a1 = sorted((ux * s[0] + uy * s[1], ux * s[2] + uy * s[3]))
        k = int(round(ang)) % 180
        for kk in ((k - 1) % 180, k, (k + 1) % 180):
            th = math.radians(kk)
            bnx, bny = -math.sin(th), math.cos(th)
            ob = int(math.floor((bnx * (s[0] + s[2]) / 2.0
                                 + bny * (s[1] + s[3]) / 2.0) / band))
            g0, g1 = along_span(s, th)
            for ob2 in range(ob - reach, ob + reach + 1):
                b = bins.get((kk, ob2))
                if b is None:
                    continue
                by_pos, long_ = b
                if g1 - g0 > MAX_BUCKETS:
                    cands = [j for lst in by_pos.values() for j in lst]
                else:
                    cands = [j for g in range(g0 - 1, g1 + 2)
                             for j in by_pos.get(g, ())]
                for j in cands + long_:
                    if j == i or (min(i, j), max(i, j)) in seen:
                        continue
                    _aj, uxj, uyj, Lj = axes[j]
                    if abs(ux * uxj + uy * uyj) < cos_tol:
                        continue
                    t = segs[j]
                    d = abs(nx * (t[0] + t[2]) / 2.0 + ny * (t[1] + t[3]) / 2.0 - ci)
                    if not (dmin <= d <= dmax):
                        continue
                    b0, b1 = sorted((ux * t[0] + uy * t[1], ux * t[2] + uy * t[3]))
                    ov = min(a1, b1) - max(a0, b0)
                    if ov <= 0:
                        continue
                    seen.add((min(i, j), max(i, j)))
                    out.append((i, j, d, ov))
    return out


def _seg_key(seg):
    """Direction-free identity of a segment, to 0.1 mm."""
    a = (round(seg[0], 1), round(seg[1], 1))
    b = (round(seg[2], 1), round(seg[3], 1))
    return (a, b) if a <= b else (b, a)


def _detect_walls(raw_segs, skip=None):
    """Pick out the wall faces a room boundary should follow.

    A wall is drawn as two faces a wall's thickness apart. Everything else
    in a plan - WC cubicle partitions, urinal screens, door leaves, counter
    edges, centre lines - is a single line or two lines a few millimetres
    apart, and a room runs straight past it. So:

      * lines on never-wall layers (dimensions, furniture, text...) are
        dropped outright;
      * two lines of similar length lying a few millimetres apart are a
        panel - a door leaf, a partition board - and are dropped;
      * of what remains, a line with a parallel partner WALL_T_MIN to
        WALL_T_MAX away, running alongside it, is a wall face;
      * any line on a structure layer (WALL, RCC, COLUMN...) is a wall
        whatever its thickness, because a 1,000 mm core wall fails any
        thickness test;
      * a short line joining two wall-face ends is the end cap of a wall.

    Two kinds of line are never walls, whatever their layer or partner:

      * door lines (a door layer or a door block) - a leaf standing open,
        a frame, a swing drawn as a polyline. They sit in the opening;
      * centre marks - two perpendicular lines crossing at their middles,
        the "+" drawn through a column. Each arm pokes past the column
        into the room, and as a wall it cut a slit into the room that
        came back as a spike or a self-intersecting outline.

    `skip` holds _seg_key()s of lines that are never walls whatever their
    layer - the diagonals of X-marks.

    Returns {"faces": [...], "caps": [...], "panels": n, "centre_marks": n,
    "door_lines": n}, segments as (sx, sy, ex, ey) in millimetres.
    """
    never = [k.upper() for k in _HARD_EXCLUDE_KEYWORDS]
    not_pair = [k.upper() for k in _WALL_EXCLUDE_KEYWORDS]
    skip = skip or set()
    segs, layers = [], []
    door_lines = 0
    for item in raw_segs:
        lu = (item[4] or "").upper()
        if any(k in lu for k in never):
            continue
        if skip and _seg_key(item) in skip:
            continue
        door = item[7] if len(item) > 7 else _is_door_leaf(lu, ())
        if door:
            door_lines += 1
            continue
        segs.append((item[0], item[1], item[2], item[3]))
        layers.append(lu)
    # A line on a wall layer is never taken for a centre mark: an
    # equipment box drawn across a wall stub crosses it at the middle of
    # both, and losing the wall face would open the room.
    marks = set(i for i in _centre_marks(segs) if "WALL" not in layers[i])
    if marks:
        keep = [i for i in range(len(segs)) if i not in marks]
        segs = [segs[i] for i in keep]
        layers = [layers[i] for i in keep]
    axes = [_axis(s) for s in segs]
    idx = [i for i, a in enumerate(axes) if a is not None and a[3] >= 20.0]
    always = [any(k in layers[i] for k in _ALWAYS_WALL_KEYWORDS)
              for i in range(len(segs))]

    panel = set()
    for i, j, _d, ov in _parallel_pairs(segs, axes, idx, 0.5, WALL_PANEL_MAX):
        Li, Lj = axes[i][3], axes[j][3]
        if (min(Li, Lj) >= 0.6 * max(Li, Lj)
                and ov >= 0.8 * min(Li, Lj)):
            for k in (i, j):
                if not always[k]:
                    panel.add(k)

    wall = set(i for i in idx if always[i])
    cand = [i for i in idx if i not in panel and not always[i]
            and axes[i][3] >= WALL_MIN_FACE
            and not any(k in layers[i] for k in not_pair)]
    cand_set = set(cand)
    pair_pool = cand + [i for i in idx if always[i] and i not in panel]
    for i, j, _d, ov in _parallel_pairs(segs, axes, pair_pool,
                                         WALL_T_MIN, WALL_T_MAX):
        if ov >= max(150.0, 0.5 * min(axes[i][3], axes[j][3])):
            for k in (i, j):
                if k in cand_set:
                    wall.add(k)

    faces = [segs[i] for i in sorted(wall)]

    ends = set()
    for f in faces:
        for x, y in ((f[0], f[1]), (f[2], f[3])):
            ends.add((int(round(x / 20.0)), int(round(y / 20.0))))

    def at_face_end(x, y):
        gx, gy = int(round(x / 20.0)), int(round(y / 20.0))
        return any((gx + a, gy + b) in ends for a in (-1, 0, 1)
                   for b in (-1, 0, 1))

    caps = [segs[i] for i in idx
            if i not in wall and i not in panel
            and axes[i][3] <= WALL_T_MAX + 50.0
            and at_face_end(segs[i][0], segs[i][1])
            and at_face_end(segs[i][2], segs[i][3])]
    return {"faces": faces, "caps": caps, "panels": len(panel),
            "centre_marks": len(marks), "door_lines": door_lines}


def _centre_marks(segs, mid_tol=100.0, min_len=200.0, max_len=4000.0):
    """Indices of lines forming "+" centre marks.

    Two lines are a centre mark when they are perpendicular (within 3
    degrees), of comparable length, and cross each other near both their
    midpoints - the cross drawn through a column or a fixture. Wall faces
    meet at their ends; they do not cross at their middles.
    """
    cos_perp = math.sin(math.radians(3.0))
    grid = {}
    for i, s in enumerate(segs):
        L = math.hypot(s[2] - s[0], s[3] - s[1])
        if not (min_len <= L <= max_len):
            continue
        mx, my = (s[0] + s[2]) / 2.0, (s[1] + s[3]) / 2.0
        grid.setdefault((int(mx // mid_tol), int(my // mid_tol)), []).append(
            (i, mx, my, (s[2] - s[0]) / L, (s[3] - s[1]) / L, L))
    out = set()
    for cell, items in grid.items():
        near = []
        for a in (-1, 0, 1):
            for b in (-1, 0, 1):
                near.extend(grid.get((cell[0] + a, cell[1] + b), ()))
        for i, mx, my, ux, uy, L in items:
            for j, mx2, my2, ux2, uy2, L2 in near:
                if j <= i:
                    continue
                if math.hypot(mx - mx2, my - my2) > mid_tol:
                    continue
                if abs(ux * ux2 + uy * uy2) > cos_perp:
                    continue
                if min(L, L2) < 0.5 * max(L, L2):
                    continue
                # They must cross in the middle half of both. A short line
                # whose END touches the middle of a wall stub is not a
                # cross - on A-106 that is how a lift jamb's 400 mm wall
                # stubs, met by an equipment outline, were nearly lost.
                si, sj = segs[i], segs[j]
                den = ux * uy2 - uy * ux2
                qx, qy = sj[0] - si[0], sj[1] - si[1]
                ti = (qx * uy2 - qy * ux2) / den / L
                tj = (qx * uy - qy * ux) / den / L2
                if not (0.25 <= ti <= 0.75 and 0.25 <= tj <= 0.75):
                    continue
                out.add(i)
                out.add(j)
    return out


# -- Doors -----------------------------------------------------------------
def _is_door(layer, blocks):
    lu = (layer or "").upper()
    if any(k in lu for k in DOOR_KEYWORDS):
        return True
    return any(k in (b or "").upper() for b in blocks for k in DOOR_KEYWORDS)


def _is_door_leaf(layer, blocks):
    """A line that belongs to a door: on a door layer or in a door block.

    Only doors, not windows. A door's lines - its leaf standing open, its
    frame, its swing drawn as a polyline - lie inside the opening, and two
    of them a wall's thickness apart read as a wall. On A-106 an open leaf
    paired with the ramp edge beside it that way: the boundary spiked up
    each jamb, and the leaf then served as a jamb for a door closure drawn
    across the middle of the ramp. A window block, by contrast, often
    carries the wall faces running through the opening, which are real.
    OUTDOOR (an outdoor unit, an outdoor deck) is not a door.
    """
    def door_word(name):
        return "DOOR" in (name or "").upper().replace("OUTDOOR", "")
    return door_word(layer) or any(door_word(b) for b in blocks)


def _collect_door_swings(doc, msp, units=None):
    """Door evidence as (hinge_x, hinge_y, leaf_width), in millimetres.

    Taken from anything on a door or window layer, or inside a door or
    window block:

      * a swing arc - an ARC or a bulged polyline span - whose centre is
        the hinge and whose radius is the leaf width;
      * a straight door-sized line - a leaf drawn as a panel, or a
        threshold across the opening - either end of which may be the
        hinge, its length the width.

    Shaft access doors on the test plate are drawn with a 325 mm arc and a
    600 mm leaf panel, so the arc alone does not describe the opening but
    the panel does. A window's frame and glazing lines span its opening
    the same way. A candidate only closes an opening when it stands at
    one end of it and matches its width (see _door_bridges), so a door
    line that describes nothing closes nothing.
    """
    units = units or MM_UNITS
    out = []
    for e, xform, _depth in iter_entities(doc, msp,
                                          "ARC LINE LWPOLYLINE POLYLINE"):
        try:
            if not _is_door(effective_layer(e, xform), xform.blocks):
                continue
            kind = e.dxftype()
            arcs = []
            if kind == "ARC":
                c = e.dxf.center
                arcs.append((c.x, c.y, c.z, float(e.dxf.radius)))
            elif kind == "LINE":
                a, b = e.dxf.start, e.dxf.end
                r = math.hypot(b.x - a.x, b.y - a.y)
                arcs.append((a.x, a.y, a.z, r))
                arcs.append((b.x, b.y, b.z, r))
            elif kind == "LWPOLYLINE":
                pts = list(e.get_points("xyb"))
                elev = float(e.dxf.get("elevation", 0.0) or 0.0)
                n = len(pts)
                last = n if e.closed else n - 1
                for i in range(max(last, 0)):
                    a, b = pts[i], pts[(i + 1) % n]
                    if a[2]:
                        c, _sa, _ea, r = bulge_to_arc((a[0], a[1]),
                                                      (b[0], b[1]), a[2])
                        arcs.append((c.x, c.y, elev, float(r)))
                    else:
                        r = math.hypot(b[0] - a[0], b[1] - a[1])
                        arcs.append((a[0], a[1], elev, r))
                        arcs.append((b[0], b[1], elev, r))
            elif kind == "POLYLINE" and e.is_2d_polyline:
                vs = list(e.vertices)
                n = len(vs)
                last = n if e.is_closed else n - 1
                for i in range(max(last, 0)):
                    bulge = float(vs[i].dxf.get("bulge", 0.0) or 0.0)
                    if bulge:
                        p, q = vs[i].dxf.location, vs[(i + 1) % n].dxf.location
                        c, _sa, _ea, r = bulge_to_arc((p.x, p.y), (q.x, q.y),
                                                      bulge)
                        arcs.append((c.x, c.y, p.z, float(r)))
            scale = units.mm_per_unit * xform.scale_avg
            for cx, cy, cz, r in arcs:
                rw = r * scale
                if not (DOOR_SWING_MIN <= rw <= DOOR_SWING_MAX):
                    continue
                wx, wy, wz = ocs_to_wcs(e, cx, cy, cz)
                hx, hy = units.point_to_mm(*xform.apply(wx, wy, wz))
                out.append((hx, hy, rw))
        except (AttributeError, TypeError, ValueError, ZeroDivisionError):
            continue
    return out


def _ray_hit(ox, oy, ux, uy, s):
    """Distance along the ray (ox, oy) + t*(ux, uy) to segment s, or None."""
    ex, ey = s[2] - s[0], s[3] - s[1]
    den = ux * ey - uy * ex
    if abs(den) < 1e-9:
        return None
    qx, qy = s[0] - ox, s[1] - oy
    t = (qx * ey - qy * ex) / den
    v = (qx * uy - qy * ux) / den
    return t if (t > 0 and -1e-6 <= v <= 1.0 + 1e-6) else None


def _door_bridges(faces, caps, swings):
    """Close each door opening straight across, along the wall faces.

    From the end of every wall face a ray runs on in the face's own
    direction. The first wall it meets beyond the end of its own wall is
    the far side of the opening. That span is closed only when a door
    swing stands in it: a hinge at one end with a leaf as wide as the gap,
    or a hinge at each end whose leaves add up to it (a double door).
    Openings with no door stay open, so a urinal bay or an alcove belongs
    to the room it opens off. A straight door line (a leaf panel or a
    threshold) counts the same way, with its length as the leaf width.

    Both faces of the wall are carried across, so the doorway reads to the
    flood fill as if the wall ran through it: the boundary goes straight
    along the wall line past the door, never into the swing.
    """
    if not faces or not swings:
        return []
    barrier = list(faces) + list(caps)
    CELL = 1000.0
    grid = {}
    for bi, s in enumerate(barrier):
        for gx in range(int(math.floor(min(s[0], s[2]) / CELL)),
                        int(math.floor(max(s[0], s[2]) / CELL)) + 1):
            for gy in range(int(math.floor(min(s[1], s[3]) / CELL)),
                            int(math.floor(max(s[1], s[3]) / CELL)) + 1):
                grid.setdefault((gx, gy), []).append(bi)
    hinges = {}
    for sw in swings:
        hinges.setdefault((int(math.floor(sw[0] / CELL)),
                           int(math.floor(sw[1] / CELL))), []).append(sw)

    def hinges_near(x, y, radius):
        r = int(math.ceil(radius / CELL))
        gx, gy = int(math.floor(x / CELL)), int(math.floor(y / CELL))
        out = []
        for a in range(-r, r + 1):
            for b in range(-r, r + 1):
                for sw in hinges.get((gx + a, gy + b), ()):
                    if math.hypot(sw[0] - x, sw[1] - y) <= radius:
                        out.append(sw)
        return out

    out = set()
    for fi, f in enumerate(faces):
        ax = _axis(f)
        if ax is None:
            continue
        _ang, ux, uy, _L = ax
        for ox, oy, dx, dy in ((f[2], f[3], ux, uy), (f[0], f[1], -ux, -uy)):
            if not hinges_near(ox, oy, DOOR_BRIDGE_MAX + DOOR_HINGE_TOL):
                continue
            tx, ty = ox + dx * DOOR_BRIDGE_MAX, oy + dy * DOOR_BRIDGE_MAX
            cand = set()
            for gx in range(int(math.floor(min(ox, tx) / CELL)),
                            int(math.floor(max(ox, tx) / CELL)) + 1):
                for gy in range(int(math.floor(min(oy, ty) / CELL)),
                                int(math.floor(max(oy, ty) / CELL)) + 1):
                    cand.update(grid.get((gx, gy), ()))
            # Anything within 60 mm is the rest of this wall - a cap or a
            # joining wall - so look past it for the far side.
            hits = sorted(t for bi in cand if bi != fi
                          for t in [_ray_hit(ox, oy, dx, dy, barrier[bi])]
                          if t is not None and 60.0 < t <= DOOR_BRIDGE_MAX)
            if not hits:
                continue
            t = hits[0]
            if t < DOOR_BRIDGE_MIN:
                continue
            hx, hy = ox + dx * t, oy + dy * t
            at_o = hinges_near(ox, oy, DOOR_HINGE_TOL)
            at_h = hinges_near(hx, hy, DOOR_HINGE_TOL)
            single = any(abs(sw[2] - t) <= 0.25 * t for sw in at_o + at_h)
            double = any(abs(a[2] + b[2] - t) <= 0.2 * t
                         for a in at_o for b in at_h)
            if single or double:
                # The same opening is found from both jambs; keep it once.
                a, b = (round(ox, 1), round(oy, 1)), (round(hx, 1), round(hy, 1))
                out.add(a + b if a <= b else b + a)
    return sorted(out)


THRESHOLD_MIN_MM = 300.0
THRESHOLD_MAX_MM = 1500.0
THRESHOLD_END_TOL_MM = 150.0


def _threshold_lines(openings, raw_segs, faces=()):
    """Close each door opening at the inner edge of a ramp standing in it.

    A ramp or raised threshold is drawn in the doorway as a rectangle as
    wide as the opening: on A-106 every AHU room has a 2000 x 750 mm ramp
    on ELE-5 inside its double door. The room stops at the ramp, so the
    opening is closed along the ramp's far edge instead of the wall face.

    For every opening (a door bridge, running along a wall face) the ramp
    edge is a line that
      * runs parallel to the opening and spans the same two jambs, to
        within THRESHOLD_END_TOL_MM at each end;
      * stands 300-1500 mm off the opening, on either side - a ramp on the
        corridor side is no more corridor than it is room;
      * is not structure and not already a wall face: a column or the far
        face of a wall across a corridor also lines up with a doorway, and
        is no ramp;
      * has a side - a line running from one of its ends back to the wall -
        so it outlines a shape standing in the doorway, not a line that
        merely passes it;
      * encloses open floor: no wall face stands inside the strip between
        it and the opening. That is what rules out a column beside a
        facade jamb, whose outline otherwise passes every other test.
    The edge and the two sides joining it to the jambs are returned as
    barriers, straight and square to the opening.
    """
    if not openings or not raw_segs:
        return []
    never = [k.upper() for k in _HARD_EXCLUDE_KEYWORDS]
    not_pair = [k.upper() for k in _WALL_EXCLUDE_KEYWORDS]
    structure = [k.upper() for k in _ALWAYS_WALL_KEYWORDS] + ["WALL"]
    face_keys = set(_seg_key(f) for f in faces)
    CELL = 1000.0
    edges, ends = {}, {}
    for s in raw_segs:
        if s[5] < 200.0 or (len(s) > 7 and s[7]):
            continue
        lu = (s[4] or "").upper()
        if any(k in lu for k in never) or any(k in lu for k in not_pair):
            continue
        for x, y in ((s[0], s[1]), (s[2], s[3])):
            ends.setdefault((int(math.floor(x / CELL)),
                             int(math.floor(y / CELL))), []).append(s)
        if (s[5] < THRESHOLD_MIN_MM or any(k in lu for k in structure)
                or _seg_key(s) in face_keys):
            continue
        mx, my = (s[0] + s[2]) / 2.0, (s[1] + s[3]) / 2.0
        edges.setdefault((int(math.floor(mx / CELL)),
                          int(math.floor(my / CELL))), []).append(s)

    face_ends = {}
    for f in faces:
        for x, y in ((f[0], f[1]), (f[2], f[3])):
            face_ends.setdefault((int(math.floor(x / CELL)),
                                  int(math.floor(y / CELL))), []).append(f)

    cos_tol = math.cos(math.radians(2.0))
    sin_tol = math.sin(math.radians(3.0))

    def strip_is_open(ox, oy, ux, uy, nx, ny, W, d):
        """No wall face ends inside the strip from the opening out to d.

        Ends, not whole lines: a column standing in the strip has its
        corners there, while a long line merely passing through it - on
        A-106 an 11.8 m line on 'server load' crosses one ramp - does not
        make the doorway anything other than a doorway.
        """
        lo, hi = sorted((0.0, d))
        cx, cy = ox + ux * W / 2.0 + nx * d / 2.0, oy + uy * W / 2.0 + ny * d / 2.0
        r = int(math.ceil((W / 2.0 + abs(d)) / CELL)) + 1
        gx, gy = int(math.floor(cx / CELL)), int(math.floor(cy / CELL))
        for a in range(-r, r + 1):
            for b in range(-r, r + 1):
                for f in face_ends.get((gx + a, gy + b), ()):
                    for px, py in ((f[0], f[1]), (f[2], f[3])):
                        t = (px - ox) * ux + (py - oy) * uy
                        o = (px - ox) * nx + (py - oy) * ny
                        if (40.0 < t < W - 40.0
                                and lo + 40.0 < o < hi - 40.0):
                            return False
        return True

    def has_side(px, py, ux, uy, nx, ny, ox, oy):
        """A line leaving (px, py) square to the edge and ending at the wall."""
        gx, gy = int(math.floor(px / CELL)), int(math.floor(py / CELL))
        for a in (-1, 0, 1):
            for b in (-1, 0, 1):
                for s in ends.get((gx + a, gy + b), ()):
                    sa = _axis(s)
                    if sa is None or abs(ux * sa[1] + uy * sa[2]) > sin_tol:
                        continue
                    for (x0, y0), (x1, y1) in (((s[0], s[1]), (s[2], s[3])),
                                               ((s[2], s[3]), (s[0], s[1]))):
                        if math.hypot(x0 - px, y0 - py) > 100.0:
                            continue
                        if abs((x1 - ox) * nx + (y1 - oy) * ny) <= 450.0:
                            return True
        return False

    out = set()
    for op in openings:
        ax_ = _axis(op)
        if ax_ is None:
            continue
        _ang, ux, uy, W = ax_
        nx, ny = -uy, ux
        ox, oy = op[0], op[1]
        cx, cy = (op[0] + op[2]) / 2.0, (op[1] + op[3]) / 2.0
        reach = int(math.ceil((W / 2.0 + THRESHOLD_MAX_MM) / CELL)) + 1
        gx, gy = int(math.floor(cx / CELL)), int(math.floor(cy / CELL))
        best = {}                       # side -> smallest offset found
        for a in range(-reach, reach + 1):
            for b in range(-reach, reach + 1):
                for s in edges.get((gx + a, gy + b), ()):
                    sa = _axis(s)
                    if sa is None or abs(ux * sa[1] + uy * sa[2]) < cos_tol:
                        continue
                    t0, t1 = sorted(((s[0] - ox) * ux + (s[1] - oy) * uy,
                                     (s[2] - ox) * ux + (s[3] - oy) * uy))
                    if (abs(t0) > THRESHOLD_END_TOL_MM
                            or abs(t1 - W) > THRESHOLD_END_TOL_MM):
                        continue
                    d = ((s[0] + s[2]) / 2.0 - ox) * nx                         + ((s[1] + s[3]) / 2.0 - oy) * ny
                    if not (THRESHOLD_MIN_MM <= abs(d) <= THRESHOLD_MAX_MM):
                        continue
                    side = d > 0
                    if side in best and abs(d) >= abs(best[side]):
                        continue
                    if not (has_side(s[0], s[1], ux, uy, nx, ny, ox, oy)
                            or has_side(s[2], s[3], ux, uy, nx, ny, ox, oy)):
                        continue
                    if not strip_is_open(ox, oy, ux, uy, nx, ny, W, d):
                        continue
                    best[side] = d
        for d in best.values():
            p0 = (ox + nx * d, oy + ny * d)
            p1 = (op[2] + nx * d, op[3] + ny * d)
            for seg in (p0 + p1, (ox, oy) + p0, (op[2], op[3]) + p1):
                out.add(tuple(round(v, 1) for v in seg))
    return sorted(out)


def _find_collinear_wall_gaps(wall_segs):
    """Find gaps in a wall run with no door in them - pairs of wall-face
    endpoints that are:
      - 300 to 2000mm apart (typical door gap)
      - belong to walls whose direction vectors are nearly parallel (<=8deg apart)
      - the gap direction continues each wall outward (the gap is along the
        wall, not perpendicular to it)
      - both endpoints "point" toward each other (each wall ends at its
        endpoint, with the rest of the wall extending the other way)

    Only used when the close_wall_gaps rule is switched on, and only over
    wall faces. It used to run over every line in the drawing, where two
    cubicle partitions pointing at a wall end look exactly like a wall with
    a door cut in it: on the test plate that produced 3,684 invented walls,
    three of them straight through the open floor of the men's toilet.
    By default an opening with no door stays open.

    Returns a list of (sx, sy, ex, ey) closing-line tuples.
    """
    if not wall_segs:
        return []

    GAP_MIN = WALL_GAP_MIN
    GAP_MAX = WALL_GAP_MAX
    PARALLEL_TOL = math.cos(math.radians(8.0))   # walls within 8deg of parallel
    GAP_ALIGN_TOL = math.cos(math.radians(12.0)) # gap within 12deg of wall direction

    # Per-wall data
    walls = []
    for sx, sy, ex, ey in wall_segs:
        L = math.hypot(ex - sx, ey - sy)
        # A face no longer than a wall is thick is the END of a wall - its
        # cap on a structure layer - not a run with a gap in it. Two such
        # ends facing each other across a recess are the recess's mouth:
        # closing it cut a ladies' toilet on A-106 down to the 2 sq.m
        # alcove its label stood in.
        if L <= WALL_T_MAX + 50.0: continue
        walls.append({
            "s": (sx, sy), "e": (ex, ey), "L": L,
            "dir": ((ex - sx) / L, (ey - sy) / L),
        })

    # Index endpoints. Each entry is (point, wall_index, "s"|"e")
    BUCKET = 2000.0
    grid = {}
    for wi, w in enumerate(walls):
        for label, pt in (("s", w["s"]), ("e", w["e"])):
            gk = (int(pt[0] // BUCKET), int(pt[1] // BUCKET))
            grid.setdefault(gk, []).append((pt, wi, label))

    closing_set = set()  # canonical sorted tuple of rounded points

    def canonical(p1, p2):
        a = (round(p1[0] / 10) * 10, round(p1[1] / 10) * 10)
        b = (round(p2[0] / 10) * 10, round(p2[1] / 10) * 10)
        return tuple(sorted([a, b]))

    for wi, w in enumerate(walls):
        wdx, wdy = w["dir"]
        for label, pt in (("s", w["s"]), ("e", w["e"])):
            # "Outward" direction at this endpoint = the direction the wall
            # would continue if it extended past this endpoint.
            outward_x = -wdx if label == "s" else wdx
            outward_y = -wdy if label == "s" else wdy
            gx, gy = int(pt[0] // BUCKET), int(pt[1] // BUCKET)
            for ddx in (-1, 0, 1):
                for ddy in (-1, 0, 1):
                    for (other_pt, oi, o_label) in grid.get((gx + ddx, gy + ddy), []):
                        if oi == wi: continue
                        gap_x = other_pt[0] - pt[0]
                        gap_y = other_pt[1] - pt[1]
                        gap_len = math.hypot(gap_x, gap_y)
                        if gap_len < GAP_MIN or gap_len > GAP_MAX: continue

                        # Gap direction must align with wall's outward direction
                        gn_x = gap_x / gap_len
                        gn_y = gap_y / gap_len
                        if outward_x * gn_x + outward_y * gn_y < GAP_ALIGN_TOL:
                            continue

                        # Other wall must be nearly parallel to this one
                        ow = walls[oi]
                        odx, ody = ow["dir"]
                        if abs(wdx * odx + wdy * ody) < PARALLEL_TOL: continue

                        # Other wall's outward direction at its endpoint must
                        # point AWAY from us (so it extends in the opposite
                        # direction  -  i.e. the closing line bridges two walls
                        # both ending at the gap, not two starting walls).
                        oo_x = -odx if o_label == "s" else odx
                        oo_y = -ody if o_label == "s" else ody
                        if oo_x * (-gn_x) + oo_y * (-gn_y) < GAP_ALIGN_TOL:
                            continue

                        # Bridge along the wall, not corner to corner. The
                        # two stubs may be a little out of line with each
                        # other, and joining their endpoints directly tilts
                        # the bridge by up to the alignment tolerance - a
                        # boundary crossing a doorway then comes out at a
                        # slight angle, which is exactly what a wall never
                        # does. Projecting both ends onto the longer wall's
                        # line keeps the crossing straight and square.
                        ref = w if w["L"] >= ow["L"] else ow
                        rx, ry = ref["s"]
                        rdx, rdy = ref["dir"]
                        a = ((pt[0] - rx) * rdx + (pt[1] - ry) * rdy)
                        b = ((other_pt[0] - rx) * rdx
                             + (other_pt[1] - ry) * rdy)
                        p_a = (rx + rdx * a, ry + rdy * a)
                        p_b = (rx + rdx * b, ry + rdy * b)
                        closing_set.add(canonical(p_a, p_b))

    return [(p1[0], p1[1], p2[0], p2[1]) for (p1, p2) in closing_set]


def _polygon_edges(poly):
    """The floor outline as wall segments.

    Nothing on a floor can extend past its own slab edge, so feeding the
    outline in as walls gives every ray a guaranteed stop. Without it a
    label in the middle of an open floor - an OFFICE AREA more than the
    20 m search radius from any partition - finds nothing in some
    direction and is reported as 'no walls', which is how the largest
    space on a plate ends up with no boundary at all.
    """
    segs = []
    for i in range(len(poly)):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % len(poly)]
        if ax != bx or ay != by:
            segs.append((ax, ay, bx, by))
    return segs


def _hv_boundary(px, py, wall_segs, max_search=MAX_HV_SEARCH, index=None):
    """Ray-cast from (px,py) in four cardinal directions to the nearest wall.

    Only axis-aligned segments count as walls; slanted lines are arrowheads,
    hatching and leaders, not geometry.

    `index` is an optional BandIndex over the same segments. It narrows the
    candidate set to segments that could actually cross the ray, which turns
    what was a full scan per label into a bucket lookup. The result is
    identical either way.
    """
    if index is None and not wall_segs:
        return None
    best = {"left": max_search, "right": max_search,
            "down": max_search, "up": max_search}

    if index is not None:
        h_cands = index.crossing_horizontal_ray(py)
        v_cands = index.crossing_vertical_ray(px)
    else:
        h_cands = v_cands = wall_segs

    for sx, sy, ex, ey in h_cands:
        if not (min(sy, ey) <= py <= max(sy, ey)):
            continue
        if index is None:
            dx, dy = abs(ex - sx), abs(ey - sy)
            if dx > 0 and dy > 0 and max(dx, dy) / min(dx, dy) < WALL_AXIS_RATIO:
                continue
            if dy <= dx:
                continue
        diff = (sx + ex) / 2 - px
        if diff > 0: best["right"] = min(best["right"], diff)
        elif diff < 0: best["left"] = min(best["left"], -diff)

    for sx, sy, ex, ey in v_cands:
        if not (min(sx, ex) <= px <= max(sx, ex)):
            continue
        if index is None:
            dx, dy = abs(ex - sx), abs(ey - sy)
            if dx > 0 and dy > 0 and max(dx, dy) / min(dx, dy) < WALL_AXIS_RATIO:
                continue
            if dx <= dy:
                continue
        diff = (sy + ey) / 2 - py
        if diff > 0: best["up"] = min(best["up"], diff)
        elif diff < 0: best["down"] = min(best["down"], -diff)

    if any(v >= max_search for v in best.values()): return None
    mn_x = px - best["left"]; mx_x = px + best["right"]
    mn_y = py - best["down"]; mx_y = py + best["up"]
    if mx_x - mn_x < MIN_BOUNDARY_SIDE or mx_y - mn_y < MIN_BOUNDARY_SIDE:
        return None
    if (mx_x - mn_x) * (mx_y - mn_y) / 1e6 > MAX_ROOM_SQM: return None
    return (mn_x, mn_y, mx_x, mx_y)


def _trace_with(tracer, x, y):
    """One flood-fill trace, or None when the tracer gives up."""
    try:
        return tracer.trace(x, y, simplify_cells=TRACE_SIMPLIFY)
    except (MemoryError, RecursionError, ValueError):
        return None


def _bbox_gap(x, y, bbox):
    """Distance from a point to a box. Zero when the point is inside."""
    dx = max(bbox[0] - x, 0.0, x - bbox[2])
    dy = max(bbox[1] - y, 0.0, y - bbox[3])
    return math.hypot(dx, dy)


def _hugs(enc, bb, margin):
    """True when every side of `enc` lies within `margin` of `bb`."""
    return all(abs(enc[i] - bb[i]) <= margin for i in range(4))


def _validate_x_cluster(cl, wall_thick, wall_all, trust_layer=False,
                        idx_thick=None, idx_all=None):
    """Decide the bbox for an X-cluster.

    Returns (bbox, source), or (None, None) if the cluster is too small to
    be a shaft. source is 'wall' when the walls around the X were used,
    'x_extent' when the X's own extent was.

    **The X is the shaft.** It is drawn corner to corner of the opening,
    so its extent is the boundary. The walls are consulted only to move
    the four sides out onto the wall face, and only when they already hug
    the X - every side within X_ENCLOSURE_SNAP. Any looser enclosure is
    the room the shaft stands in, not the shaft: casting rays from the
    centre of a toilet vent used to return the whole toilet lobby, so the
    vent was drawn at 11.28 sq.m instead of 3.30, and an AHU shaft at
    6.46 instead of 1.73. 21 of 92 shafts on the test plate were coming
    out at least half as large again as the X that marks them.

    trust_layer=True means the X was found on a known shaft layer, so the
    layer already vouches for it and no size floor applies.
    """
    cx, cy = cl["cx"], cl["cy"]
    bb = cl["bbox"]
    enc = _hv_boundary(cx, cy, wall_thick, index=idx_thick) if wall_thick else None
    if enc is None:
        enc = _hv_boundary(cx, cy, wall_all, index=idx_all)
    if enc is not None:
        enc_area = (enc[2] - enc[0]) * (enc[3] - enc[1])
        if (enc_area > 0 and enc_area / 1e6 <= X_ENCLOSURE_MAX_SQM
                and _hugs(enc, bb, X_ENCLOSURE_SNAP)):
            return enc, "wall"
    if trust_layer or cl["w"] * cl["h"] / 1e6 >= X_FALLBACK_MIN_SQM:
        return bb, "x_extent"
    return None, None


FLOOR_APPID = "DRAWING_ANALYZER_FLOOR"


def _tag_floor(doc, entity, name):
    """Store the Step 2 floor name on a floor outline, as XDATA.

    XDATA is invisible in CAD and survives editing and re-saving, so a
    sheet carrying several floors still says which outline is which when
    it comes back for Step 4.
    """
    try:
        if FLOOR_APPID not in doc.appids:
            doc.appids.new(FLOOR_APPID)
        entity.set_xdata(FLOOR_APPID, [(1000, str(name or "Floor"))])
    except (AttributeError, TypeError, ValueError):
        pass


def floor_tag(entity):
    """The floor name _tag_floor stored on an outline, or None."""
    try:
        if entity.has_xdata(FLOOR_APPID):
            for code, value in entity.get_xdata(FLOOR_APPID):
                if code == 1000 and str(value).strip():
                    return str(value).strip()
    except (AttributeError, TypeError, ValueError, KeyError):
        pass
    return None


def _draw_rect(msp, mn_x, mn_y, mx_x, mx_y, layer=None, color=None, units=None):
    """Draw a closed rectangle. Coordinates arrive in millimetres and are
    converted back to the drawing's own units before being written."""
    u = units or MM_UNITS
    f = u.from_mm
    a, b, c, d = f(mn_x), f(mn_y), f(mx_x), f(mx_y)
    pts = [(a, b), (c, b), (c, d), (a, d), (a, b)]
    return msp.add_lwpolyline(pts, dxfattribs={
        "layer": layer or ANNOTATE_LAYER,
        "color": color if color is not None else ANNOTATE_COLOR,
        "closed": True,
    })

def _draw_poly(msp, poly, layer=None, color=None, units=None):
    """Draw a traced room outline. Coordinates arrive in millimetres."""
    u = units or MM_UNITS
    pts = [(u.from_mm(px), u.from_mm(py)) for px, py in poly]
    if not pts:
        return None
    return msp.add_lwpolyline(pts + [pts[0]], dxfattribs={
        "layer": layer or ANNOTATE_LAYER,
        "color": color if color is not None else ANNOTATE_COLOR,
        "closed": True,
    })


def _hatch_boundary(msp, outline, holes=(), layer=None, color=None):
    """Fill a drawn boundary with a solid hatch in its family colour.

    The hatch sits on the outline's own layer and is associative: its
    paths are tied to the outline and void polylines, so moving a vertex
    in CAD reshapes the fill with it. Voids are inner paths and stay open.
    Returns the hatch, or None when there is no outline to fill.
    """
    if outline is None:
        return None
    col = color if color is not None else ANNOTATE_COLOR
    hatch = msp.add_hatch(color=col, dxfattribs={"layer": layer or ANNOTATE_LAYER})
    hatch.set_solid_fill(color=col, style=0)          # 0 = normal: holes stay open
    for i, ent in enumerate([outline] + [h for h in holes if h is not None]):
        pts = [(x, y) for x, y, *_ in ent.get_points("xy")]
        flags = 1 if i == 0 else 0                    # 1 = external path
        path = hatch.paths.add_polyline_path(pts, is_closed=True, flags=flags)
        try:
            hatch.associate(path, [ent])
        except Exception:                             # noqa: BLE001
            pass                                      # a plain fill still reads
    if HATCH_TRANSPARENCY > 0:
        hatch.transparency = HATCH_TRANSPARENCY
    return hatch


def _send_to_back(msp, entities):
    """Draw these entities before everything else in the layout, so the
    fills lie under the walls and texts of the drawing they annotate."""
    entities = [e for e in entities if e is not None]
    if not entities:
        return
    try:
        order = dict(msp.get_redraw_order())
    except Exception:                                 # noqa: BLE001
        order = {}
    # Sort handles below every real handle put these first; the rest keep
    # the order they had (their own handle, or the drawing's own table).
    for i, e in enumerate(entities, start=1):
        order[e.dxf.handle] = "{0:X}".format(i)
    try:
        msp.set_redraw_order(order)
    except Exception:                                 # noqa: BLE001
        pass


def _add_label(msp, text, x, y, layer=None, color=None, units=None):
    """Place an MTEXT label, converting millimetres back to drawing units."""
    u = units or MM_UNITS
    msp.add_mtext(text, dxfattribs={
        "layer": layer or ANNOTATE_LAYER,
        "color": color if color is not None else ANNOTATE_COLOR,
        "insert": (u.from_mm(x), u.from_mm(y)),
        "char_height": u.from_mm(TEXT_HEIGHT), "attachment_point": 5,
    })


def _ensure_family_layers(doc, only_family=None):
    """Create one DXF layer per family (plus the Unknown fallback) so
    that boundaries can be split by family. Each layer's color comes
    from the family rules.

    With only_family set, only that family's layer is created, so a
    filtered run does not add a dozen empty layers to the drawing.
    """
    families = list(_FAMILY_RULES) + [_UNKNOWN_FAMILY]
    if only_family:
        families = [f for f in families if f["name"] == only_family]
    for fam in families:
        layer_name = _family_layer(fam)
        if layer_name not in doc.layers:
            doc.layers.new(layer_name, dxfattribs={"color": int(fam.get("color", 7))})
        else:
            # Update color in case the user changed family_rules.json
            try:
                doc.layers.get(layer_name).color = int(fam.get("color", 7))
            except Exception:
                pass


def _inside_boundary(d, x, y):
    """Whether (x, y) stands inside a drawn boundary.

    A traced room is tested against its outline, less any voids; a box
    against the box. The bounding box of an L-shaped room covers the rooms
    in its corner, so testing that claimed their labels.
    """
    poly = d.get("poly")
    if poly:
        if not _point_in_polygon(x, y, poly):
            return False
        return not any(_point_in_polygon(x, y, h) for h in (d.get("holes") or []))
    return text_in_bbox(x, y, d["bbox"])


def _interior_point(poly, holes=None):
    """A point well inside a polygon (and outside its holes).

    The centroid when it lies inside; otherwise the middle of the widest
    interior span on a few horizontal scan lines.
    """
    cx, cy = polygon_centroid(poly)
    probe = {"poly": poly, "holes": holes or [], "bbox": _polygon_bbox(poly)}
    if _inside_boundary(probe, cx, cy):
        return cx, cy
    x0, y0, x1, y1 = probe["bbox"]
    best = None
    for k in range(1, 20):
        y = y0 + (y1 - y0) * k / 20.0
        xs = []
        n = len(poly)
        for i in range(n):
            (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
            if (ay > y) != (by > y):
                xs.append(ax + (y - ay) * (bx - ax) / (by - ay))
        xs.sort()
        for a, b in zip(xs[0::2], xs[1::2]):
            mid = ((a + b) / 2.0, y)
            if (best is None or b - a > best[0]) and _inside_boundary(probe, *mid):
                best = (b - a, mid)
    return best[1] if best else (cx, cy)


def _same_room_shape(a, b):
    """(bbox, poly, holes) when two boundaries are one traced room.

    Either both are traced outlines of the same space (bounding boxes and
    areas within a few percent), or one is a ray-cast box whose label
    stands inside the other's traced outline. None otherwise.
    """
    pa, pb = a.get("poly"), b.get("poly")
    if pa and pb:
        aa, ab = shoelace_area_sqm(pa), shoelace_area_sqm(pb)
        if (min(aa, ab) >= 0.9 * max(aa, ab)
                and bbox_overlap_ratio(a["bbox"], b["bbox"]) >= 0.9):
            keep = a if aa >= ab else b
            return keep["bbox"], keep["poly"], keep.get("holes") or []
        return None
    for traced, box in ((a, b), (b, a)):
        if traced.get("poly") and not box.get("poly") \
                and _point_in_polygon(box["cx"], box["cy"], traced["poly"]):
            return traced["bbox"], traced["poly"], traced.get("holes") or []
    return None


def annotate_dxf(src_path, dst_path, floors, results, family_overrides=None,
                 unit_override=None, family_filter=None, rule_overrides=None,
                 user_rules=None, project_name=""):
    """Draw room boundaries into a copy of the drawing.

    family_filter limits the output to a single family (its display name,
    e.g. 'Staircase'), so the export carries only STAIR_BOUNDARY. None
    draws every family, which is the default.

    rule_overrides turns individual judgements off - see rules.py for the
    catalogue and what each one costs when it is not applied. None runs
    every rule, which is what the program did before they could be
    switched.
    """
    family_filter = (family_filter or "").strip() or None
    rule = rule_book.resolve(rule_overrides)
    # The user's own rules apply only when they are handed over. Step 3
    # never passes them, so "All boundaries" there means every boundary
    # the drawing pass can find; the rules have their own run.
    my_rules = userrules.validate(user_rules) if user_rules else []
    doc, msp, units = load_document(src_path, unit_override)
    # Keep the legacy ROOM_BOUNDARY layer too (used for the floor outline
    # and as a no-family fallback). Add one layer per family with that
    # family's configured color.
    if ANNOTATE_LAYER not in doc.layers:
        doc.layers.new(ANNOTATE_LAYER, dxfattribs={"color": ANNOTATE_COLOR})
    if VOID_LAYER not in doc.layers:
        doc.layers.new(VOID_LAYER, dxfattribs={"color": 8})
    _ensure_family_layers(doc, only_family=family_filter)

    raw_segs = _collect_raw_segs(doc, msp, units)

    # X detection: try v3 layer-restricted path first (fast and clean if the
    # drawing uses standard CUTOUT/E-*SHAFT layers). If it finds enough
    # X-pairs, use those. Otherwise fall back to v4 shape-only detection
    # which works on drawings that put shaft Xs on layer 0 or A-DETL etc.
    # The layer path always runs, even with the rule off, because the X
    # diagonals must never count as walls (below).
    diag_segs_v3 = _collect_diagonals_layered(raw_segs)
    x_pairs_v3 = _find_x_pairs(diag_segs_v3)
    diag_segs, x_pairs, detection_mode = [], [], "off"
    if rule["x_marks"]:
        detection_mode = "layer"
        diag_segs = diag_segs_v3
        x_pairs = x_pairs_v3

        # If v3 found very few X-pairs, the drawing is probably non-standard.
        # Fall back to layer-agnostic detection.
        if len(x_pairs_v3) < 3:
            diag_segs = _collect_diagonals_anywhere(doc, msp, units)
            x_pairs = _find_x_pairs(diag_segs)
            detection_mode = "shape"

    x_clusters = _build_x_clusters_from_pairs(diag_segs, x_pairs)

    # The diagonals of an X mark the shaft; they are not walls. Slab cutouts
    # count as walls (a room cannot include a hole in the floor), and the X
    # is usually drawn on the cutout layer, so without this a shaft label
    # would trace one triangle of its own X.
    # An X across a tall shaft - DG EXHAUST on the test plate is 6 m - is
    # past the shaft detector's length limit, so crossings on the layers
    # that always count as walls are looked for again with no limit.
    long_diags = []
    for sg in raw_segs:
        lu = (sg[4] or "").upper()
        if not any(k in lu for k in _ALWAYS_WALL_KEYWORDS):
            continue
        if (abs(sg[2] - sg[0]) < X_DIAG_MIN_AXIS
                or abs(sg[3] - sg[1]) < X_DIAG_MIN_AXIS
                or sg[5] < X_DIAG_MIN_LEN):
            continue
        long_diags.append((sg[0], sg[1], sg[2], sg[3], (sg[0] + sg[2]) / 2.0,
                           (sg[1] + sg[3]) / 2.0, sg[4], sg[5]))
    x_lines = set()
    for segs_, pairs_ in ((diag_segs_v3, x_pairs_v3), (diag_segs, x_pairs),
                          (long_diags, _find_x_pairs(long_diags))):
        for i, j in pairs_:
            for k in (i, j):
                x_lines.add(_seg_key(segs_[k]))

    # Rooms are bounded by real walls only: two faces a wall's thickness
    # apart, or anything on a structure layer. Cubicle partitions, screens,
    # counters and door leaves are not walls, so a room runs past them to
    # the wall behind.
    walls = _detect_walls(raw_segs, skip=x_lines)
    faces, caps = walls["faces"], walls["caps"]

    # Door openings are closed straight across, along the wall faces, where
    # a door swing stands in them. Openings without a door stay open unless
    # the close_wall_gaps rule asks for them to be closed too.
    swings = (_collect_door_swings(doc, msp, units)
              if rule["close_doors"] else [])
    door_closing = _door_bridges(faces, caps, swings) if swings else []
    # A ramp or threshold drawn in a doorway closes the opening at its far
    # edge, so the room stops at the ramp rather than running into it.
    threshold = (_threshold_lines(door_closing, raw_segs, faces)
                 if rule["threshold_ramps"] else [])
    gap_closing = (_find_collinear_wall_gaps(faces)
                   if rule["close_wall_gaps"] else [])
    wall_set = faces + caps + door_closing + threshold + gap_closing
    # Doorless openings stay open by default, but a room whose fill leaks
    # through one is traced again with them closed (see resolve_boundary).
    retry_gaps = (_find_collinear_wall_gaps(faces)
                  if rule["close_gaps_on_leak"] and not rule["close_wall_gaps"]
                  else [])


    # The walls, the index and the tracer are all built per floor, because
    # each floor contributes its own outline as a wall.
    wall_idx = tracer = None
    hatches = []                   # one fill per drawn boundary, sent to back

    stats = {
        "units": units.to_ui(),
        "boundary_method": ("trace" if tracer is not None else "raycast"),
        "family_filter": family_filter or "",
        "drawn_boundaries": 0,
        "filtered_out_boundaries": 0,
        "traced_boundaries": 0,
        "traced_fallback_raycast": 0,
        "traced_holes_deducted": 0,
        "x_detection_mode": detection_mode,
        "rules": dict(rule),
        "rules_changed": rule_book.changed_from_default(rule),
        "x_with_label": 0, "x_unknown": 0,
        "x_validated_wall": 0, "x_validated_fallback": 0, "x_discarded": 0,
        "primary_boundaries": 0, "secondary_boundaries": 0,
        "claimed_by_primary": 0, "merged_overlapping": 0,
        "skipped_no_boundary": 0,
        "engulfing_discarded": 0, "x_relabelled": 0, "x_adopted_label": 0,
        "x_unlabelled_skipped": 0,
        "leaked_raycast_rejected": 0, "traced_overspill_boxed": 0,
        "merge_skipped_nested": 0,
        "wall_segs_thick": len(faces),
        "wall_segs_all": len(wall_set),
        "wall_faces": len(faces),
        "wall_end_caps": len(caps),
        "panels_ignored": walls["panels"],
        "door_swings": len(swings),
        "door_closing_lines": len(door_closing),
        "gap_closing_lines": len(gap_closing),
        "threshold_lines": len(threshold),
        "retry_gap_lines": len(retry_gaps),
        "traced_after_closing_gaps": 0,
        "centre_marks_ignored": walls.get("centre_marks", 0),
        "door_lines_ignored": walls.get("door_lines", 0),
        "x_diagonals_found": len(diag_segs),
        "x_pairs_found": len(x_pairs),
        "x_clusters_total": len(x_clusters),
    }

    for floor in floors:
        floor_poly = [tuple(p) for p in floor["polygon"]]
        floor_name = floor["name"]
        rows = results.get(floor_name, [])

        # The slab edge is a wall: it stops rays that would otherwise run
        # off into nothing, and it confines a leaking flood fill.
        edges = (_polygon_edges(floor_poly)
                 if (USE_FLOOR_EDGES and rule["floor_outline_walls"]) else [])
        f_walls = wall_set + edges
        wall_idx = BandIndex(f_walls, band=INDEX_BAND,
                             axis_ratio=WALL_AXIS_RATIO)

        # A fixed 1000 sq.m ceiling silently rejects the main space of any
        # large plate, so the ceiling follows the plate. It still catches a
        # true runaway, which covers most of the floor.
        # Both of these were tried wider and both made the output worse, so
        # they stay as they are. Letting rays run the width of the plate
        # doubled the total boundary area: a ray that escapes through a
        # doorway runs on to the far perimeter, and the radius is the only
        # thing stopping it. Raising the area ceiling changed nothing,
        # because on a real plate the ceiling is not what rejects a room.
        search = MAX_HV_SEARCH
        room_cap = MAX_ROOM_SQM

        # Real-region tracing. The four-ray box is only correct for a
        # rectangular, axis-aligned room; flood-filling the enclosure
        # handles angled, curved and L-shaped rooms, and deducts interior
        # columns. Where a fill leaks we fall back to the box, so this can
        # only improve on the older behaviour, never lose a room.
        tracer = None
        if BOUNDARY_METHOD == "trace" and rule["trace_rooms"]:
            try:
                tracer = RoomTracer(f_walls, cell_mm=TRACE_CELL_MM,
                                    max_area_sqm=room_cap,
                                    seal_openings_mm=SEAL_OPENINGS_MM)
                if not tracer.ok:
                    tracer = None
            except (MemoryError, ValueError, OverflowError):
                tracer = None
        stats["boundary_method"] = "trace" if tracer is not None else "raycast"

        # The same walls with every doorless opening closed, built the first
        # time a room needs it.
        closed_tracer = []

        def gaps_tracer():
            if not closed_tracer:
                t2 = None
                if tracer is not None and retry_gaps:
                    try:
                        t2 = RoomTracer(f_walls + retry_gaps,
                                        cell_mm=TRACE_CELL_MM,
                                        max_area_sqm=room_cap,
                                        seal_openings_mm=SEAL_OPENINGS_MM)
                        if not t2.ok:
                            t2 = None
                    except (MemoryError, ValueError, OverflowError):
                        t2 = None
                closed_tracer.append(t2)
            return closed_tracer[0]

        # Per-text family override chosen by the user in Step 3 (sr_no ->
        # family display name). Keys arrive as strings over JSON.
        floor_family_overrides = {}
        if family_overrides:
            for k, v in (family_overrides.get(floor_name) or {}).items():
                try:
                    floor_family_overrides[int(k)] = v
                except (TypeError, ValueError):
                    pass
        floor_texts = [{"text": r["text"], "x": float(r["x"]),
                        "y": float(r["y"]),
                        "height": float(r.get("height") or 0.0),
                        "sr_no": r.get("sr_no"),
                        "claimed": False} for r in rows]

        # Per-text status that we'll return alongside stats:
        # 'x'          -  claimed by an X-shaft (label appears as the shaft's title)
        # 'primary'    -  got its own primary boundary
        # 'secondary'  -  got its own secondary boundary
        # 'absorbed'   -  claimed by a primary's bbox (text inside, no own boundary)
        # 'merged'     -  its boundary was merged into another (Phase D)
        # 'no_walls'   -  _hv_boundary returned None
        # 'pending'    -  initial state; never seen
        text_status = {}
        for t in floor_texts:
            if t["sr_no"] is not None:
                text_status[t["sr_no"]] = "pending"

        # Validate clusters: keep only those with valid bbox
        validated = []
        for cl in x_clusters:
            if not _point_in_polygon(cl["cx"], cl["cy"], floor_poly):
                continue
            bbox, src = _validate_x_cluster(
                cl, f_walls if rule["x_wall_snap"] else [], [],
                trust_layer=(detection_mode == "layer"),
                idx_thick=wall_idx if rule["x_wall_snap"] else None,
                idx_all=None)
            if bbox is None:
                stats["x_discarded"] += 1
                continue
            if src == "wall":
                stats["x_validated_wall"] += 1
            else:
                stats["x_validated_fallback"] += 1
            validated.append({"bbox": bbox, "source_kind": src,
                              "cx": cl["cx"], "cy": cl["cy"]})

        # -- Phase A: stable matching X-clusters <-> texts --
        pairs = []
        for ci, v in enumerate(validated):
            for ti, t in enumerate(floor_texts):
                if is_ignorable_label(t["text"]):
                    continue
                d = math.hypot(t["x"] - v["cx"], t["y"] - v["cy"])
                inside = text_in_bbox(t["x"], t["y"], v["bbox"])
                # A text sitting inside the shaft outline names it however
                # far it is from the centre; otherwise it has to be beside
                # it. The gap is measured to the edge of the shaft, not to
                # its centre, because distance from the centre says more
                # about how long the shaft is than about how near the text
                # is: measured that way a 200 x 540 cut-out reached 2.2 m
                # across a wall and took LADIES TOILET - 01 for its title,
                # which both mis-titled the cut-out and left the toilet
                # with no boundary at all.
                if not inside and (d > X_TEXT_RADIUS
                                   or _bbox_gap(t["x"], t["y"],
                                                v["bbox"]) > X_TEXT_GAP):
                    continue
                # Containment outranks proximity. The matching gives each
                # cluster one text, so a stair label merely NEAR a
                # neighbouring shaft could be taken by it first, leaving
                # the shaft the label actually stands in to settle for
                # whatever else was lying around.
                pairs.append(((0 if inside else 1,)
                              + tuple(label_score(t["text"],
                                                  t.get("height", 0.0), d)),
                              ci, ti))
        pairs.sort(key=lambda p: p[0])
        cluster_assigned = {}; text_assigned = set()
        for _, ci, ti in pairs:
            if ci in cluster_assigned or ti in text_assigned: continue
            cluster_assigned[ci] = ti
            text_assigned.add(ti)

        drawn = []
        for ci, v in enumerate(validated):
            ti = cluster_assigned.get(ci)
            sr_for_this = None
            if ti is not None:
                label = floor_texts[ti]["text"]
                floor_texts[ti]["claimed"] = True
                sr_for_this = floor_texts[ti]["sr_no"]
                if sr_for_this is not None:
                    text_status[sr_for_this] = "x"
                stats["x_with_label"] += 1
            else:
                if not rule["x_unlabelled"]:
                    stats["x_unlabelled_skipped"] += 1
                    continue
                label = X_UNKNOWN_LABEL
                stats["x_unknown"] += 1
            drawn.append({"bbox": v["bbox"], "poly": None, "holes": [],
                          "label": label, "source": "X",
                          "cx": v["cx"], "cy": v["cy"],
                          "sr_no": sr_for_this, "absorbed_srs": []})

        def holds_other_room(poly, seed):
            """Another room's name standing inside this traced outline.

            A name of a different family - a staircase inside an AHU
            room's fill - means the fill left its room. Names of the same
            family (VANITY COUNTER in a toilet) and tags do not.
            """
            if seed is None:
                return False
            own = family_for_label(seed["text"], _FAMILY_RULES)["name"]
            for o in floor_texts:
                if o is seed or not _point_in_polygon(o["x"], o["y"], poly):
                    continue
                t = o["text"]
                if (is_ignorable_label(t) or is_note_text(t)
                        or is_device_tag(t) or not looks_like_primary(t)):
                    continue
                if family_for_label(t, _FAMILY_RULES)["name"] != own:
                    return True
            return False

        def resolve_boundary(tx, ty, seed=None):
            """Traced polygon where possible, ray-cast box otherwise.

            Returns (bbox, polygon_or_None, holes).
            """
            if tracer is None:
                return raycast_boundary(tx, ty), None, []
            box = raycast_boundary(tx, ty)
            box_sqm = (0.0 if box is None else
                       (box[2] - box[0]) * (box[3] - box[1]) / 1e6)
            tr = _trace_with(tracer, tx, ty)
            # Doorless openings are left open, so an alcove belongs to its
            # room. When that lets the fill escape - it leaks, or it reaches
            # a room of another family - the room is traced again with every
            # doorless opening in a real wall closed straight across, so the
            # label still gets the room its walls enclose.
            if tr is None or not tr.ok or holds_other_room(tr.polygon, seed):
                closed = gaps_tracer()
                tr2 = _trace_with(closed, tx, ty) if closed else None
                if (tr2 is not None and tr2.ok and tr2.area_sqm > 0
                        and not holds_other_room(tr2.polygon, seed)):
                    tr = tr2
                    stats["traced_after_closing_gaps"] += 1
            if tr is not None and tr.ok and tr.area_sqm > 0:
                # A fill that closes, but comes back several times larger
                # than the box the four rays found, escaped the room
                # through a gap and wandered. It looks like a result,
                # which is worse than failing: an AHU room on the typical
                # floor came back at 191 sq.m against a 19.8 sq.m box,
                # swallowed the staircases beside it, and their labels
                # were absorbed so they were never drawn at all. Real
                # rooms measure 1.0 to 2.6 times their box.
                # With only real walls counted, a toilet with its cubicle
                # banks legitimately runs to 7-8 times the box four rays
                # find from its label, so size alone no longer marks a
                # leak. It takes size AND another room's name inside.
                if (rule["overspill_guard"] and box_sqm > 0
                        and tr.area_sqm > box_sqm * TRACE_MAX_VS_BOX
                        and holds_other_room(tr.polygon, seed)):
                    stats["traced_overspill_boxed"] += 1
                    return box, None, []
                stats["traced_boundaries"] += 1
                holes = tr.holes if rule["deduct_voids"] else []
                if holes:
                    stats["traced_holes_deducted"] += len(holes)
                return tr.bbox, tr.polygon, holes
            stats["traced_fallback_raycast"] += 1
            # The fill leaked, so the walls do not enclose this point and
            # the four-ray box is a guess about a room that may not be
            # there. A modest guess is worth keeping - a fill can leak
            # through one doorway, and a women's toilet measures 51.7 sq.m
            # that way - but a large one is how a shaft tag standing in
            # open floor came back holding 555 sq.m of the plate and
            # titled it BMS-4 SHAFT.
            if (rule["leaked_box_cap"] and box is not None
                    and box_sqm > MAX_LEAKED_SQM):
                stats["leaked_raycast_rejected"] += 1
                return None, None, []
            return box, None, []

        def raycast_boundary(tx, ty):
            # The same walls the tracer follows, so the box and the traced
            # outline describe the same room.
            return (_hv_boundary(tx, ty, f_walls, index=wall_idx,
                                 max_search=search) if f_walls else None)

        # -- Phase B: primary labels --
        # Every candidate is resolved before any of them claims, and the
        # tightest enclosure goes first. Claiming in reading order was safe
        # only while a boundary could not exceed 1000 sq.m; now that the
        # main space of a floor can be measured, taking it first would let
        # it absorb every room standing inside it.
        # The ordering pass uses the cheap ray-cast box, not the tracer: it
        # only needs the rooms in roughly the right order, and tracing every
        # candidate - including the ones about to be absorbed - tripled the
        # run time for no change in the result.
        primaries = []
        for t in floor_texts:
            if t["claimed"] or is_ignorable_label(t["text"]): continue
            if not looks_like_primary(t["text"]): continue
            box = raycast_boundary(t["x"], t["y"])
            if box is None:
                if t["sr_no"] is not None:
                    text_status[t["sr_no"]] = "no_walls"
                continue
            primaries.append(((box[2] - box[0]) * (box[3] - box[1]), t))
        primaries.sort(key=lambda c: c[0])

        def engulfs_drawn(bbox, poly=None, holes=None):
            """Would this boundary swallow several rooms already drawn?

            When a fill leaks out of a room through an opening it comes
            back holding its neighbours - one boundary wrapping an AHU
            room, a service lift and a hose cabinet together. The tightest
            enclosures are taken first, so a later one containing them is
            that leak.

            Containing ONE room is not evidence of anything: plans nest a
            janitor inside a toilet block all the time, and rejecting on a
            single hit threw away real rooms. A leak collects several.
            X-marked shafts never count - a niche inside a room is what
            they are for.

            A traced room is tested against its outline, not its bounding
            box: the box of an L-shaped toilet covers the private toilet
            in its corner, which is a neighbour, not a sign of a leak.
            """
            if not rule["engulf_guard"]:
                return False
            new = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            probe = {"bbox": bbox, "poly": poly, "holes": holes or []}
            hits = 0
            for d in drawn:
                if d.get("source") == "X":
                    continue
                a = d["bbox"]
                acx, acy = (a[0] + a[2]) / 2.0, (a[1] + a[3]) / 2.0
                if not _inside_boundary(probe, acx, acy):
                    continue
                own = (a[2] - a[0]) * (a[3] - a[1])
                if new > own * ENGULF_RATIO:
                    hits += 1
                    if hits >= ENGULF_MIN_ROOMS:
                        return True
            return False

        def claim_room(d, t, status):
            """Claim every label standing in this room and title it.

            Labels are claimed by the room's own outline. Using the
            bounding box of a traced L-shaped room claimed the label of the
            room sitting in its corner, which was then never drawn.

            When a room holds several labels - MEN TOILET and VANITY
            COUNTER, say - it is titled by the best of them, not by
            whichever happened to be processed first; that label's row
            moves with the title so its family is the one drawn.
            """
            t["claimed"] = True
            inside = [o for o in floor_texts
                      if o is not t and not o["claimed"]
                      and _inside_boundary(d, o["x"], o["y"])]
            if d.get("poly"):
                names = [o for o in [t] + inside
                         if not is_ignorable_label(o["text"])
                         and not is_note_text(o["text"])
                         and not is_device_tag(o["text"])]
                best = choose_label(names or [t], _LABEL_RULES,
                                    centre=polygon_centroid(d["poly"]))
            else:
                best = t
            if best is not t:
                d.update({"label": best["text"],
                          "height": best.get("height", 0.0),
                          "cx": best["x"], "cy": best["y"],
                          "sr_no": best["sr_no"]})
            for o in [t] + inside:
                o["claimed"] = True
                if o is best:
                    if o["sr_no"] is not None:
                        text_status[o["sr_no"]] = status
                    continue
                if o["sr_no"] is not None:
                    text_status[o["sr_no"]] = "absorbed"
                    d["absorbed_srs"].append(o["sr_no"])
                stats["claimed_by_primary"] += 1

        for _area, t in primaries:
            if t["claimed"]: continue
            bbox, poly, holes = resolve_boundary(t["x"], t["y"], seed=t)
            # A leaked trace often still has a sound ray-cast box, so try
            # that before giving up on the room altogether.
            if bbox is not None and engulfs_drawn(bbox, poly, holes):
                box = raycast_boundary(t["x"], t["y"])
                if box is not None and not engulfs_drawn(box):
                    bbox, poly, holes = box, None, []
                else:
                    bbox = None
                    stats["engulfing_discarded"] += 1
                    if t["sr_no"] is not None:
                        text_status[t["sr_no"]] = "engulfing"
            if bbox is None:
                if t["sr_no"] is not None and \
                        text_status.get(t["sr_no"]) != "engulfing":
                    text_status[t["sr_no"]] = "no_walls"
                continue
            d = {"bbox": bbox, "poly": poly, "holes": holes,
                 "label": t["text"], "height": t.get("height", 0.0),
                 "source": "P", "cx": t["x"], "cy": t["y"],
                 "sr_no": t["sr_no"], "absorbed_srs": []}
            claim_room(d, t, "primary")
            stats["primary_boundaries"] += 1
            drawn.append(d)

        # -- Phase C: secondary boundaries --
        for t in (floor_texts if rule["secondary_boundaries"] else []):
            if t["claimed"] or is_ignorable_label(t["text"]): continue
            # A device tag names its shaft in Phase A if it has one; it
            # never earns a room of its own here. Nor does a sentence of
            # drawing notes, which otherwise takes a boundary that the
            # real room it refers to then merges into.
            if ((rule["equipment_tags"] and is_device_tag(t["text"]))
                    or (rule["skip_notes"] and is_note_text(t["text"]))):
                if t["sr_no"] is not None:
                    text_status[t["sr_no"]] = "equipment"
                continue
            bbox, poly, holes = resolve_boundary(t["x"], t["y"], seed=t)
            if bbox is not None and engulfs_drawn(bbox, poly, holes):
                bbox = None
                stats["engulfing_discarded"] += 1
                if t["sr_no"] is not None:
                    text_status[t["sr_no"]] = "engulfing"
                continue
            if bbox is None:
                stats["skipped_no_boundary"] += 1
                if t["sr_no"] is not None:
                    text_status[t["sr_no"]] = "no_walls"
                continue
            stats["secondary_boundaries"] += 1
            d = {"bbox": bbox, "poly": poly, "holes": holes,
                 "label": t["text"], "height": t.get("height", 0.0),
                 "source": "S", "cx": t["x"], "cy": t["y"],
                 "sr_no": t["sr_no"], "absorbed_srs": []}
            if poly:
                # Other labels in the same traced room would each trace
                # the same outline and then be merged into a rectangle.
                claim_room(d, t, "secondary")
            else:
                t["claimed"] = True
                if t["sr_no"] is not None:
                    text_status[t["sr_no"]] = "secondary"
            drawn.append(d)

        # -- Phase D: merge overlapping (>= 70%) --
        # When two non-X boxes overlap heavily, the winning label is the one
        # whose text position is closest to the union-rectangle center.
        # Each "drawn" entry tracks the sr_no(s) it represents so we can
        # report per-text status to the UI.
        merged = list(drawn); changed = rule["merge_overlaps"]
        while changed:
            changed = False; i = 0
            while i < len(merged):
                j = i + 1
                while j < len(merged):
                    a, b = merged[i], merged[j]
                    if a["source"] == "X" and b["source"] == "X":
                        j += 1; continue
                    r = bbox_overlap_ratio(a["bbox"], b["bbox"])
                    if r >= OVERLAP_MERGE_RATIO:
                        # Overlap is measured against the smaller box, so a
                        # room standing inside a larger one scores 1.0. That
                        # is containment, not duplication: merging them drew
                        # a 19.8 sq.m AHU room as a 167 sq.m rectangle over
                        # its neighbours. Two boundaries that describe the
                        # same room come out close in size.
                        aa = ((a["bbox"][2] - a["bbox"][0])
                              * (a["bbox"][3] - a["bbox"][1]))
                        ba = ((b["bbox"][2] - b["bbox"][0])
                              * (b["bbox"][3] - b["bbox"][1]))
                        if min(aa, ba) < MERGE_MIN_SIZE * max(aa, ba):
                            stats["merge_skipped_nested"] += 1
                            j += 1
                            continue
                        # When merging X-shaft with non-X: the X is only
                        # authoritative if its bbox is similar in size to
                        # the other. A small X (e.g. an FHR icon) inside
                        # a large room shouldn't eat the room.
                        if a["source"] == "X" and b["source"] != "X":
                            if aa < MERGE_MIN_SIZE * ba:
                                j += 1; continue   # don't merge  -  X too small
                        if b["source"] == "X" and a["source"] != "X":
                            if ba < MERGE_MIN_SIZE * aa:
                                j += 1; continue
                        kpoly, kholes = None, []
                        if a["source"] == "X":
                            kb = a["bbox"]; ks = "X"
                            kl = a["label"] if a["label"] != X_UNKNOWN_LABEL else b["label"]
                            kcx, kcy = a["cx"], a["cy"]
                            winner_sr = a.get("sr_no")
                        elif b["source"] == "X":
                            kb = b["bbox"]; ks = "X"
                            kl = b["label"] if b["label"] != X_UNKNOWN_LABEL else a["label"]
                            kcx, kcy = b["cx"], b["cy"]
                            winner_sr = b.get("sr_no")
                        else:
                            ax1, ay1, ax2, ay2 = a["bbox"]; bx1, by1, bx2, by2 = b["bbox"]
                            kb = (min(ax1, bx1), min(ay1, by1),
                                  max(ax2, bx2), max(ay2, by2))
                            # Centered-text-wins: whichever text is closer to the
                            # union rectangle's center wins the label.
                            ucx = (kb[0] + kb[2]) / 2
                            ucy = (kb[1] + kb[3]) / 2
                            da = math.hypot(a["cx"] - ucx, a["cy"] - ucy)
                            db = math.hypot(b["cx"] - ucx, b["cy"] - ucy)
                            # The better room name wins; distance to the
                            # union centre is only a tie-break inside the
                            # ranking.
                            ra = label_score(a["label"], a.get("height", 0.0), da)
                            rb = label_score(b["label"], b.get("height", 0.0), db)
                            winner = a if ra <= rb else b
                            kl = winner["label"]
                            kcx, kcy = winner["cx"], winner["cy"]
                            winner_sr = winner.get("sr_no")
                            ks = "P" if "P" in (a["source"], b["source"]) else "S"
                            # Two boundaries of the same traced room keep
                            # the traced outline. Taking their bounding box
                            # turned a 36 sq.m L-shaped space carrying two
                            # tags into a 100 sq.m rectangle. A box that
                            # sits inside a traced room is a worse guess at
                            # that same room, and gives way to the outline.
                            shape = _same_room_shape(a, b)
                            if shape is not None:
                                kb, kpoly, kholes = shape
                            else:
                                kpoly, kholes = None, []
                        # Combine sr_no lists from both  -  track which texts
                        # were absorbed for status reporting
                        absorbed = list(a.get("absorbed_srs", []))
                        absorbed.extend(b.get("absorbed_srs", []))
                        merged_srs = list(a.get("merged_srs", []))
                        merged_srs.extend(b.get("merged_srs", []))
                        if a.get("sr_no") is not None and a.get("sr_no") != winner_sr:
                            merged_srs.append(a.get("sr_no"))
                        if b.get("sr_no") is not None and b.get("sr_no") != winner_sr:
                            merged_srs.append(b.get("sr_no"))
                        # A merged pair falls back to the rectangle: the
                        # union of two traced outlines is not their
                        # bounding box, and stitching them properly needs
                        # real polygon booleans.
                        merged[i] = {"bbox": kb, "poly": kpoly,
                                     "holes": kholes,
                                     "label": kl, "source": ks,
                                     "height": max(a.get("height", 0.0) or 0.0,
                                                   b.get("height", 0.0) or 0.0),
                                     "cx": kcx, "cy": kcy,
                                     "sr_no": winner_sr,
                                     "absorbed_srs": absorbed,
                                     "merged_srs": merged_srs}
                        merged.pop(j); changed = True
                        stats["merged_overlapping"] += 1
                        continue
                    j += 1
                i += 1

        # A text whose own boundary was merged into another in Phase D is
        # 'merged'. Texts that simply stood inside a room stay 'absorbed';
        # they used to be relabelled 'merged' too, which hid why a room
        # had not been drawn.
        for d in merged:
            for merged_sr in d.get("merged_srs", []) or []:
                if merged_sr is not None and merged_sr in text_status:
                    text_status[merged_sr] = "merged"

        # A shaft that reached the end still holding the placeholder title
        # takes the best real text standing inside it. The stable matching
        # in Phase A gives each cluster at most one text, so a stair whose
        # label went to a neighbouring cluster stayed "Unknown Shaft" - and
        # since that phrase matches no family keyword, three staircases
        # were drawn at the right size on UNKNOWN_BOUNDARY instead of
        # STAIR_BOUNDARY. Ranking picks the title, so this stays general.
        for d in (merged if rule["retitle_inside"] else []):
            current = d.get("label") or ""
            inside = [t for t in floor_texts
                      if _inside_boundary(d, t["x"], t["y"])
                      and not is_ignorable_label(t["text"])
                      and not is_note_text(t["text"])]
            if not inside:
                continue
            best = choose_label(inside, _LABEL_RULES,
                                centre=(d["cx"], d["cy"]))
            if not best or best["text"] == current:
                continue
            # Any label the family rules do not recognise gives way to one
            # they do, when that text stands inside the same boundary. The
            # stairs came out titled FID and FO - ordinary words, matching
            # no tag pattern and no family - while ST-01 sat 788 mm away
            # inside the very same boundary, so three staircases were drawn
            # correctly and filed under Unknown. Ranking decides it, so
            # this follows family_rules.json rather than any fixed name.
            if current == X_UNKNOWN_LABEL or (
                    label_tier(best["text"], _LABEL_RULES)
                    < label_tier(current, _LABEL_RULES)):
                d["label"] = best["text"]
                # The row moves with the title. The layer is picked from
                # the per-text family chosen in Step 3, looked up by
                # sr_no, so leaving the old row behind would retitle a
                # boundary ST-01 and still draw it on the layer belonging
                # to FID - which is exactly how the stairs kept coming out
                # on UNKNOWN_BOUNDARY even once they were named correctly.
                d["sr_no"] = best.get("sr_no")
                stats["x_relabelled"] += 1

        # An X that still has no name of its own takes the nearest text
        # that named nothing else. Labels on a leader sit further from
        # their shaft than the claim gap allows - DUMMY SHAFT-01 stands
        # 1.4 m away, FHC-5 SHAFT 1.9 m - and tightening that gap to stop
        # cut-outs stealing room names left those shafts drawn correctly
        # but titled "Unknown Shaft". A text that has already named a room
        # is never taken, so this can rename a shaft but never strip a
        # room.
        leftovers = [t for t in floor_texts
                     if rule["x_adopt_labels"]
                     and not t["claimed"]
                     and not is_ignorable_label(t["text"])
                     and not is_note_text(t["text"])]
        if leftovers:
            offers = []
            for di, d in enumerate(merged):
                if d.get("source") != "X" or d.get("label") != X_UNKNOWN_LABEL:
                    continue
                for ti, t in enumerate(leftovers):
                    gap = _bbox_gap(t["x"], t["y"], d["bbox"])
                    if gap <= X_TEXT_RADIUS:
                        offers.append((gap, di, ti))
            offers.sort(key=lambda o: o[0])
            taken_d, taken_t = set(), set()
            for _gap, di, ti in offers:
                if di in taken_d or ti in taken_t:
                    continue
                taken_d.add(di); taken_t.add(ti)
                t = leftovers[ti]
                t["claimed"] = True
                merged[di]["label"] = t["text"]
                merged[di]["sr_no"] = t.get("sr_no")
                if t.get("sr_no") is not None:
                    text_status[t["sr_no"]] = "x"
                stats["x_adopted_label"] += 1

        # Outline floor + draw all final boundaries.
        # Floor outline goes on the legacy ROOM_BOUNDARY layer.
        # Each room boundary goes on its family's <FAMILY>_BOUNDARY layer
        # with the family's configured color.
        outline = [(units.from_mm(px), units.from_mm(py)) for px, py in floor_poly]
        outline_e = msp.add_lwpolyline(outline + [outline[0]],
                                       dxfattribs={"layer": ANNOTATE_LAYER,
                                                   "color": ANNOTATE_COLOR})
        # Name the outline after the floor chosen in Step 2, so Step 4 can
        # report each floor of a multi-floor sheet under the same name.
        _tag_floor(doc, outline_e, floor_name)
        # The user's own rules run last, on the finished boundaries: they
        # reshape, drop or refile what the drawing pass decided, and they
        # are deliberately the last word so their effect is visible rather
        # than tangled into the search.
        if my_rules:
            merged, rule_report = _apply_user_rules(
                merged, my_rules, floor_family_overrides, family_filter)
            for entry in rule_report:
                acc = stats.setdefault("user_rule_report", {}).setdefault(
                    entry["id"], {"text": entry["text"],
                                  "summary": entry["summary"],
                                  "matched": 0, "dropped": 0})
                acc["matched"] += entry["matched"]
                acc["dropped"] += entry["dropped"]

        for d in merged:
            mn_x, mn_y, mx_x, mx_y = d["bbox"]
            fam = None
            sr = d.get("sr_no")
            if d.get("family_forced"):
                fam = family_by_name(d["family_forced"], _FAMILY_RULES)
            if fam is None and sr is not None and sr in floor_family_overrides:
                fam = family_by_name(floor_family_overrides[sr], _FAMILY_RULES)
            if fam is None:
                fam = family_for_label(d["label"], _FAMILY_RULES)
            if family_filter and fam["name"] != family_filter:
                stats["filtered_out_boundaries"] += 1
                for other_sr in [sr] + list(d.get("absorbed_srs") or []):
                    if other_sr is not None and other_sr in text_status:
                        text_status[other_sr] = "filtered"
                continue
            layer_name = _family_layer(fam)
            color = int(fam.get("color", 7))
            stats["drawn_boundaries"] += 1
            hole_ents = []
            if d.get("poly"):
                outline_ent = _draw_poly(msp, d["poly"], layer=layer_name,
                                         color=color, units=units)
                # Interior voids go on their own layer, NOT a *_BOUNDARY
                # one: step 4 treats every closed polyline on a
                # *_BOUNDARY layer as a room, so a column drawn there
                # would be counted as a 0.16 sq.m room and its area added
                # back instead of deducted.
                for hole in (d.get("holes") or []):
                    hole_ents.append(_draw_poly(msp, hole, layer=VOID_LAYER,
                                                color=color, units=units))
            else:
                outline_ent = _draw_rect(msp, mn_x, mn_y, mx_x, mx_y,
                                         layer=layer_name, color=color,
                                         units=units)
            if ANNOTATE_HATCH:
                hatches.append(_hatch_boundary(msp, outline_ent, hole_ents,
                                               layer=layer_name, color=color))
            # Step 4 names each polygon from the text standing inside it,
            # so the label has to be inside the outline it titles. A label
            # sitting on a wall line traces from a free cell beside it and
            # can end up just outside its own room.
            lx, ly = d["cx"], d["cy"]
            if d.get("poly") and not _inside_boundary(d, lx, ly):
                lx, ly = _interior_point(d["poly"], d.get("holes"))
            _add_label(msp, d["label"], lx, ly,
                        layer=layer_name, color=color, units=units)

        # Persist this floor's text statuses into the global stats payload.
        # This runs after the draw loop so a boundary held back by
        # family_filter reports as 'filtered' rather than 'drawn'.
        stats.setdefault("text_status_by_floor", {})[floor_name] = dict(text_status)

    # The project name typed in Step 1 travels in the drawing's own header
    # ($PROJECTNAME), so it is still there when the file comes back for
    # Step 4 after editing in CAD.
    if project_name:
        try:
            doc.header["$PROJECTNAME"] = str(project_name)[:120]
        except Exception:                             # noqa: BLE001
            pass
    _send_to_back(msp, hatches)
    stats["hatched_boundaries"] = sum(1 for h in hatches if h is not None)
    doc.saveas(dst_path)
    return stats


# -- Exports (unchanged) --------------------------------------------------
_INVALID_SHEET_CHARS = set(r":\/?*[]")

def _apply_user_rules(merged, my_rules, floor_family_overrides,
                      family_filter):
    """Run the user's rules over one floor's finished boundaries.

    The boundaries are handed over in the shape userrules understands -
    an outline, an area, the family they would be drawn in - and come
    back reshaped. A rule that refiles a boundary sets `family_forced`,
    which the draw loop honours over the label and the Step 3 dropdown,
    because the user saying "these go under Staircase" is later and more
    specific than either.
    """
    items = []
    for d in merged:
        fam = None
        sr = d.get("sr_no")
        if sr is not None and sr in floor_family_overrides:
            fam = family_by_name(floor_family_overrides[sr], _FAMILY_RULES)
        if fam is None:
            fam = family_for_label(d.get("label"), _FAMILY_RULES)
        shape = list(d["poly"]) if d.get("poly") else [
            (d["bbox"][0], d["bbox"][1]), (d["bbox"][2], d["bbox"][1]),
            (d["bbox"][2], d["bbox"][3]), (d["bbox"][0], d["bbox"][3])]
        items.append({
            "ref": d,
            "shape": shape,
            "bbox": d["bbox"],
            "label": d.get("label") or "",
            "family": fam["name"],
            "layer": _family_layer(fam),
            "source": {"X": "shaft"}.get(d.get("source"),
                                         "traced" if d.get("poly") else "box"),
            "area_sqm": userrules._area_sqm(shape),
        })

    kept, report = userrules.apply_rules(items, my_rules)

    out = []
    for item in kept:
        d = dict(item["ref"])
        shape = item["shape"]
        if shape != (list(d["poly"]) if d.get("poly") else None):
            d["bbox"] = item["bbox"]
            # Four corners is a rectangle; the draw loop writes that from
            # the bbox and keeps the DXF free of a redundant polyline.
            d["poly"] = None if len(shape) == 4 and _is_axis_rect(shape)                 else list(shape)
            if d["poly"] is None:
                d["holes"] = []
        if item.get("family_forced"):
            d["family_forced"] = item["family_forced"]
        cx, cy = d.get("cx"), d.get("cy")
        if not text_in_bbox(cx, cy, d["bbox"]):
            # The label has to stay inside the shape it names, or the
            # viewer and Step 4 will match it to the wrong boundary.
            d["cx"] = (d["bbox"][0] + d["bbox"][2]) / 2.0
            d["cy"] = (d["bbox"][1] + d["bbox"][3]) / 2.0
        out.append(d)
    return out, report


def _is_axis_rect(shape, tol=1e-6):
    xs = sorted(set(round(p[0], 6) for p in shape))
    ys = sorted(set(round(p[1], 6) for p in shape))
    return len(xs) == 2 and len(ys) == 2


def _safe_sheet_name(name, used):
    cleaned = "".join("_" if c in _INVALID_SHEET_CHARS else c for c in name).strip()
    cleaned = cleaned[:31] or "Floor"
    base = cleaned; i = 2
    while cleaned in used:
        suffix = "_" + str(i)
        cleaned = (base[: 31 - len(suffix)]) + suffix; i += 1
    used.add(cleaned)
    return cleaned

def write_json(results, out_path):
    payload = {"floors": []}
    for floor_name, rows in results.items():
        payload["floors"].append({"floor": floor_name, "count": len(rows), "texts": rows})
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

def write_excel(results, out_path):
    wb = Workbook(); wb.remove(wb.active)
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="1F3A5F")
    header_align = Alignment(horizontal="center", vertical="center")
    thin = Side(border_style="thin", color="BFBFBF")
    border = Border(top=thin, bottom=thin, left=thin, right=thin)
    headers = ["Sr.No", "Text Detected", "Detected from", "Layer", "X", "Y"]
    used = set()
    if not results:
        ws = wb.create_sheet("Summary"); ws["A1"] = "No floors selected or no texts found."
    else:
        for floor_name, rows in results.items():
            sheet_name = _safe_sheet_name(floor_name, used)
            ws = wb.create_sheet(sheet_name)
            for col, h in enumerate(headers, start=1):
                c = ws.cell(row=1, column=col, value=h)
                c.font = header_font; c.fill = header_fill
                c.alignment = header_align; c.border = border
            for ri, row in enumerate(rows, start=2):
                ws.cell(row=ri, column=1, value=row["sr_no"]).border = border
                ws.cell(row=ri, column=2, value=row["text"]).border = border
                ws.cell(row=ri, column=3, value=row["source"]).border = border
                ws.cell(row=ri, column=4, value=row["layer"]).border = border
                ws.cell(row=ri, column=5, value=row["x"]).border = border
                ws.cell(row=ri, column=6, value=row["y"]).border = border
            for i, w in enumerate([8, 40, 14, 22, 14, 14], start=1):
                ws.column_dimensions[get_column_letter(i)].width = w
            ws.freeze_panes = "A2"
    wb.save(out_path)


# ===========================================================================
# Step 4: Area calculation from a corrected/edited annotated DXF
# ===========================================================================

def list_layers_with_closed_polylines(dxf_path, loaded=None):
    """Return a sorted list of layer names that contain at least one closed
    polyline. Used by the UI so the user can pick which layer holds the
    floor outline."""
    doc, msp, units = loaded if loaded else load_document(dxf_path)
    layers = set()
    for e in msp.query("LWPOLYLINE POLYLINE"):
        if not _is_closed(e): continue
        pts = _polyline_points(e, units)
        if len(pts) < 3: continue
        layers.add(e.dxf.layer or "0")
    return sorted(layers), units


def _shoelace_area_sqm(points):
    """Kept as a thin alias; the implementation now lives in geometry.py so
    it can be unit-tested without a DXF."""
    return shoelace_area_sqm(points)


def _polygon_centroid(points):
    """Thin alias over geometry.polygon_centroid."""
    return polygon_centroid(points)


def compute_room_areas(dxf_path, floor_layer_name, unit_override=None,
                       return_polygons=False, loaded=None):
    """Read a (manually-edited) annotated DXF and compute area for each
    closed polyline on the ROOM_BOUNDARY layer.

    The largest closed polyline on `floor_layer_name` is treated as the
    "floor outline"  -  its area is reported separately under that label.

    All closed polylines on any *_BOUNDARY layer (SHAFT_BOUNDARY,
    LIFT_BOUNDARY, ROOM_BOUNDARY, etc.) become rooms. Each room's name is
    the closest MTEXT/TEXT entity (any layer) whose insertion point lies
    inside the polygon. If no text is inside, the room is named "Unknown".

    Returns a dict:
        {
            "floor_label": "<layer name>",
            "floor_area_sqm": <float or None>,
            "rooms": [
                {"sr_no": int, "name": str, "area_sqm": float},
                ...
            ],
        }
    """
    # `loaded` lets a caller that has already parsed the file hand the open
    # document in, so a single request does not parse a 40 MB DXF twice.
    doc, msp, units = loaded if loaded else load_document(dxf_path, unit_override)

    # 1. Floor outlines. A sheet often carries several floors side by side
    #    on the same layer, so every closed polyline on the chosen layer of
    #    at least the floor-plate size is a floor of its own; the largest is
    #    always kept, as before, even when it is smaller than that. One
    #    standing inside another is the same plate drawn twice.
    #    The outlines the boundary step drew carry the Step 2 floor name
    #    (_tag_floor); a floor takes the name of the one it coincides with.
    candidates, tagged = [], []
    for e in msp.query("LWPOLYLINE POLYLINE"):
        pts = _polyline_points(e, units)
        if len(pts) < 3: continue
        tag = floor_tag(e)
        if tag:
            tagged.append((tag, pts, shoelace_area_sqm(pts)))
        if (e.dxf.layer or "0") != floor_layer_name: continue
        if not (_is_closed(e) or tag): continue
        candidates.append((shoelace_area_sqm(pts), pts, tag))
    candidates.sort(key=lambda x: -x[0])
    floors = []
    for a, pts, tag in candidates:
        if floors and a < MIN_FLOOR_AREA_SQM and not tag:
            continue
        ix, iy = _interior_point(pts)
        if any(_point_in_polygon(ix, iy, f["pts"]) for f in floors):
            continue
        floors.append({"pts": pts, "area": a, "name": tag,
                       "perimeter": polygon_perimeter_m(pts)})
    for f in floors:
        if f["name"]:
            continue
        ix, iy = _interior_point(f["pts"])
        for tag, tpts, ta in tagged:
            if (abs(ta - f["area"]) <= 0.02 * f["area"]
                    and _point_in_polygon(ix, iy, tpts)):
                f["name"] = tag
                break
    for i, f in enumerate(floors, start=1):
        if not f["name"]:
            f["name"] = ("Floor {0}".format(i) if len(floors) > 1
                         else floor_layer_name)
    # Rooms are filed under their floor by name, so two plates may not share
    # one: a sheet copied in CAD carried two outlines tagged Floor_6, and
    # each then reported the rooms of both.
    seen_names = {}
    for f in floors:
        n = seen_names.get(f["name"], 0) + 1
        seen_names[f["name"]] = n
        if n > 1:
            f["name"] = "{0} ({1})".format(f["name"], n)
    floor_area = sum(f["area"] for f in floors) if floors else None
    floor_perimeter = (sum(f["perimeter"] for f in floors)
                       if floors else None)

    # 2. Collect room polylines from any *_BOUNDARY layer.
    # Section 3 writes boundaries to per-family layers like SHAFT_BOUNDARY,
    # LIFT_BOUNDARY, etc. The layer name itself is the source of truth for
    # family classification (Option A): if the user moves a polyline to
    # SHAFT_BOUNDARY in CAD, it becomes a Shaft regardless of its text
    # label. ROOM_BOUNDARY is the legacy/generic layer.
    # Interior voids - columns and ducts standing inside a room - are
    # written to ROOM_VOID (or any *_VOID layer) so they are deducted
    # here rather than counted as rooms in their own right.
    voids = []
    for e in msp.query("LWPOLYLINE POLYLINE"):
        if not _is_closed(e): continue
        vl = (e.dxf.layer or "0").upper()
        if not (vl == VOID_LAYER.upper() or vl.endswith("_VOID")): continue
        vpts = _polyline_points(e, units)
        if len(vpts) < 3: continue
        va = shoelace_area_sqm(vpts)
        if va <= 0: continue
        voids.append({"pts": vpts, "area_sqm": va,
                      "c": polygon_centroid(vpts)})

    room_polys = []
    for e in msp.query("LWPOLYLINE POLYLINE"):
        if not _is_closed(e): continue
        e_layer = (e.dxf.layer or "0")
        if e_layer == floor_layer_name: continue   # don't pick up floor outline as room
        if not e_layer.upper().endswith("_BOUNDARY"): continue
        pts = _polyline_points(e, units)
        if len(pts) < 3: continue
        a = shoelace_area_sqm(pts)
        if a < 0.01:  # ignore degenerate (< 0.01 sq.m)
            continue
        deducted = 0.0
        n_voids = 0
        for v in voids:
            if _point_in_polygon(v["c"][0], v["c"][1], pts):
                deducted += v["area_sqm"]
                n_voids += 1
        room_polys.append({"pts": pts, "area_sqm": max(0.0, a - deducted),
                           "gross_area_sqm": a, "void_area_sqm": deducted,
                           "void_count": n_voids, "layer": e_layer,
                           "perimeter_m": polygon_perimeter_m(pts)})

    # 3. A room polyline that IS a floor outline - the boundary step draws
    #    every floor's outline on ROOM_BOUNDARY, and a user may move one
    #    there too - is dropped so the plate is not also counted as a room.
    #    Every floor is matched, not only the largest: on a sheet with two
    #    floors the second outline used to come through as a 7,600 sq.m
    #    room. Matching by area and position identifies the duplicate
    #    without punishing a genuinely large room for being big.
    def is_floor_outline(rp):
        ix, iy = _interior_point(rp["pts"])
        return any(abs(rp["gross_area_sqm"] - f["area"]) <= 0.02 * f["area"]
                   and _point_in_polygon(ix, iy, f["pts"]) for f in floors)
    room_polys = [rp for rp in room_polys if not is_floor_outline(rp)]
    if len(room_polys) >= 2 and not floors:
        room_polys.sort(key=lambda r: -r["area_sqm"])
        if room_polys[0]["area_sqm"] > 5.0 * room_polys[1]["area_sqm"]:
            big = room_polys.pop(0)
            floor_area = big["area_sqm"]
            floor_perimeter = big.get("perimeter_m")
            floors = [{"pts": big["pts"], "area": floor_area,
                       "name": floor_layer_name, "perimeter": floor_perimeter}]

    # Each room belongs to the floor it stands in.
    for rp in room_polys:
        ix, iy = _interior_point(rp["pts"])
        rp["floor"] = next((f["name"] for f in floors
                            if _point_in_polygon(ix, iy, f["pts"])),
                           "Outside floor outline")

    if floor_area is None:
        raise ValueError(
            "No closed polyline found on layer '{0}'. Pick a different floor "
            "outline layer.".format(floor_layer_name)
        )

    if not room_polys:
        raise ValueError(
            "No closed polylines found on any '*_BOUNDARY' layer (e.g. "
            "ROOM_BOUNDARY, SHAFT_BOUNDARY, MEP_BOUNDARY). Draw room "
            "boundaries in Step 3 first, or rename the relevant layers in "
            "CAD so they end with '_BOUNDARY'."
        )

    # 4. Collect all text entities (modelspace and 1-level blocks) and
    #    merge stacked multi-line labels (e.g., 3 separate entities
    #    "24 PAX" / "P.LIFT" / "2500 X 2500" become one combined label).
    #    This is the SAME merging logic that Section 2's extract_texts uses,
    #    so the labels found here match what the user saw in Section 3.
    raw_text_records = (_collect_texts_in_modelspace(msp, units)
                        + _collect_texts_from_blocks(doc, msp, units))
    text_records = _merge_stacked_lines(raw_text_records)
    _describe_floors(floors, raw_text_records)

    # 5. For each room polygon, find the best label
    rooms_out = []
    for ri, rp in enumerate(room_polys, start=1):
        pts = rp["pts"]
        # Bounding box for fast first-pass filter
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        bb = (min(xs), min(ys), max(xs), max(ys))
        cx, cy = _polygon_centroid(pts)
        # Find candidate texts inside this polygon
        candidates = []
        for tr in text_records:
            if not (bb[0] <= tr.x <= bb[2] and bb[1] <= tr.y <= bb[3]):
                continue
            if not _point_in_polygon(tr.x, tr.y, pts):
                continue
            t = tr.text.strip()
            if not t:
                continue
            # Filter out things that aren't real room labels  -  same FFL/SSL
            # filter as extract_texts uses, plus skip very long descriptive
            # texts that are clearly callouts.
            if FFL_PATTERN.match(t) or is_ignorable_label(t):
                continue
            if t.upper() in SKIP_WORDS or _is_fixture_layer(tr.layer):
                continue
            d = math.hypot(tr.x - cx, tr.y - cy)
            candidates.append({"text": t, "height": tr.height,
                               "distance": d})
        # Same ranking as Step 3, so the name here matches the boundary
        # label the user approved rather than whichever text happened to
        # be nearest the centroid.
        best = choose_label(candidates, _LABEL_RULES, centre=(cx, cy))
        name = best["text"] if best else "Unknown"
        rp["name"] = name
        rooms_out.append({
            "sr_no": ri,
            "name": name,
            "area_sqm": round(rp["area_sqm"], 2),
            "gross_area_sqm": round(rp.get("gross_area_sqm")
                                     or rp["area_sqm"], 2),
            "void_area_sqm": round(rp.get("void_area_sqm") or 0.0, 2),
            "void_count": rp.get("void_count", 0),
            "perimeter_m": round(rp.get("perimeter_m") or 0.0, 2),
            "layer": rp["layer"],
            "floor": rp.get("floor", ""),
        })

    # Floor by floor, largest room first within each.
    floor_order = {f["name"]: i for i, f in enumerate(floors)}
    rooms_out.sort(key=lambda r: (floor_order.get(r["floor"], len(floors)),
                                  -r["area_sqm"]))
    for i, r in enumerate(rooms_out, start=1):
        r["sr_no"] = i
        # Family classification is LAYER-DRIVEN (Option A): the polyline's
        # DXF layer is the sole source of truth. Moving a polyline to a
        # different *_BOUNDARY layer in CAD changes its family - the text
        # label inside is only used as the room's name, not for classification.
        fam = family_from_layer(r["layer"], _FAMILY_RULES)
        r["family"] = fam["name"]

    families_out = _families_of(rooms_out)

    try:
        project_name = str(doc.header.get("$PROJECTNAME", "") or "").strip()
    except Exception:                                 # noqa: BLE001
        project_name = ""
    result = {
        "project_name": project_name,
        "floor_label": floor_layer_name,
        "floor_area_sqm": round(floor_area, 2) if floor_area is not None else None,
        "floor_perimeter_m": round(floor_perimeter, 2) if floor_perimeter else None,
        "units": units.to_ui(),
        "rooms": rooms_out,
        "families": families_out,
        "floors": [{
            "name": f["name"],
            "area_sqm": round(f["area"], 2),
            "perimeter_m": round(f["perimeter"] or 0.0, 2),
            "room_count": sum(1 for r in rooms_out if r["floor"] == f["name"]),
            "rooms_area_sqm": round(sum(r["area_sqm"] for r in rooms_out
                                        if r["floor"] == f["name"]), 2),
            "families": _families_of([r for r in rooms_out
                                      if r["floor"] == f["name"]]),
            "title": f.get("title", ""),
            "levels": f.get("levels", []),
            "tower": f.get("tower"),
        } for f in floors],
    }
    if return_polygons:
        return result, room_polys
    return result


def _describe_floors(floors, text_records):
    """Give each floor plate its title and tower, from the text around it.

    A sheet of plans titles each plate just below it - "1ST FLOOR PLAN",
    "TYPICAL FLOOR PLAN-(2nd,4th,6th & 10th)" - in the largest text near it.
    The title is the largest floor-title text within half a plate's height
    below the plate (above it when nothing is below), across the plate's
    width. The tower is a TOWER heading in the title itself, otherwise the
    nearest "TOWER-1"-style heading on the sheet; None when the sheet has
    none, and the statement then falls back to the file name.
    """
    towers = []
    for r in text_records:
        name = tower_of(r.text)
        if name:
            towers.append((name, r.x, r.y))
    for f in floors:
        x0, y0, x1, y1 = _polygon_bbox(f["pts"])
        w, h = x1 - x0, y1 - y0
        best = None
        for side in ("below", "above"):
            for r in text_records:
                if not (x0 - 0.1 * w <= r.x <= x1 + 0.1 * w):
                    continue
                if side == "below" and not (y0 - 0.5 * h <= r.y < y0):
                    continue
                if side == "above" and not (y1 < r.y <= y1 + 0.5 * h):
                    continue
                title = clean_title(r.text)
                if not TITLE_WORDS.search(title.upper()) or not parse_levels(title):
                    continue
                gap = (y0 - r.y) if side == "below" else (r.y - y1)
                score = (r.height, -gap)
                if best is None or score > best[0]:
                    best = (score, title)
            if best:
                break
        f["title"] = best[1] if best else ""
        f["levels"] = parse_levels(f["title"]) if best else []
        m = re.search(r"\bTOWER\s*[-_ ]?\s*([A-Z]?\d+[A-Z]?)\b", f["title"].upper())
        if m:
            f["tower"] = "TOWER " + m.group(1)
        elif towers:
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            f["tower"] = min(towers, key=lambda t: math.hypot(t[1] - cx,
                                                              t[2] - cy))[0]
        else:
            f["tower"] = None


def _families_of(rooms):
    """Rooms grouped by family, in the order the families appear in the
    rules file; empty families are left out and Unknown goes last."""
    family_order = [fam["name"] for fam in _FAMILY_RULES] + ["Unknown"]
    by_family = {}
    for r in rooms:
        by_family.setdefault(r["family"], []).append({
            "name": r["name"],
            "area_sqm": r["area_sqm"],
            "perimeter_m": r.get("perimeter_m", 0.0),
            "floor": r.get("floor", ""),
        })
    out = []
    for fam_name in family_order + sorted(set(by_family) - set(family_order)):
        items = by_family.get(fam_name)
        if not items:
            continue
        items.sort(key=lambda x: -x["area_sqm"])
        out.append({
            "name": fam_name,
            "count": len(items),
            "total_sqm": round(sum(x["area_sqm"] for x in items), 2),
            "total_perimeter_m": round(
                sum(x.get("perimeter_m") or 0.0 for x in items), 2),
            "rooms": items,
        })
    return out


def family_rules_path():
    """Absolute path of the rules file the UI edits."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "family_rules.json")


def reload_family_rules():
    """Re-read family_rules.json and rebuild everything derived from it.

    Editing the rules in the UI has to take effect without a restart, and
    the label ranking is built from the same rules, so both globals are
    rebound together.
    """
    global _FAMILY_RULES, _LABEL_RULES
    _FAMILY_RULES = _load_family_rules()
    _LABEL_RULES = build_rules(CONFIG, _FAMILY_RULES)
    return _FAMILY_RULES


def list_family_rules(rules=None):
    """The full editable ruleset: name, layer, colour and keywords."""
    rules = _FAMILY_RULES if rules is None else rules
    return [{"name": fam["name"], "layer": fam["layer"],
             "boundary_layer": _family_layer(fam),
             "color": int(fam.get("color", 7)),
             "keywords": list(fam.get("keywords", []))}
            for fam in rules]


def save_family_rules(families, rules_path=None):
    """Validate `families` and write them to the rules file.

    Raises ValueError with a message meant for the user. The comment keys
    already in the file are preserved so the documentation in it is not
    lost every time somebody edits a keyword.
    """
    if not isinstance(families, list) or not families:
        raise ValueError("Send at least one family.")

    path = rules_path or family_rules_path()
    cleaned, seen = [], set()
    for fam in families:
        if not isinstance(fam, dict):
            raise ValueError("Each family must be an object.")
        name = str(fam.get("name") or "").strip()
        if not name:
            raise ValueError("Every family needs a name.")
        if name.upper() in seen:
            raise ValueError("Duplicate family name '{0}'.".format(name))
        seen.add(name.upper())

        layer = _sanitize_layer(str(fam.get("layer") or name))
        try:
            color = int(fam.get("color", 7))
        except (TypeError, ValueError):
            raise ValueError(
                "Colour for '{0}' must be a number from 1 to 255.".format(name))
        if not 1 <= color <= 255:
            raise ValueError(
                "Colour for '{0}' must be from 1 to 255.".format(name))

        keywords = []
        for kw in (fam.get("keywords") or []):
            # Only the trailing space is meaningful, so leading whitespace
            # and blank lines from the textarea are dropped.
            text = str(kw).lstrip().upper()
            if text.strip() and text not in keywords:
                keywords.append(text)
        if not keywords:
            raise ValueError(
                "'{0}' has no keywords. A family with no keywords can never "
                "match, so it would be dropped silently - delete it "
                "deliberately or give it at least one keyword.".format(name))

        cleaned.append({"name": name, "layer": layer, "color": color,
                        "keywords": keywords})

    payload = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
            payload = {k: v for k, v in existing.items() if k.startswith("_")}
        except (OSError, ValueError):
            payload = {}
    payload["families"] = cleaned

    # Write beside the target and swap, so a failure mid-write cannot
    # leave the app with an unreadable ruleset.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return reload_family_rules()


def _load_family_rules(rules_path=None):
    """Load family-mapping rules from a JSON file. Returns a list of
    family dicts in priority order. Each dict has:
        {"name": str, "layer": str, "color": int,
         "keywords": [str, ...]}

    If `rules_path` is None, looks for `family_rules.json` next to this
    Python file. If the file is missing or malformed, falls back to the
    built-in default ruleset.
    """
    if rules_path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        rules_path = os.path.join(here, "family_rules.json")

    if not os.path.exists(rules_path):
        print(
            "[family_rules] WARNING: '{0}' not found. Using built-in defaults "
            "({1} families). To customize, create the file next to "
            "dxf_reader.py.".format(rules_path, len(_DEFAULT_FAMILY_RULES)),
            flush=True,
        )
        return [dict(f) for f in _DEFAULT_FAMILY_RULES]

    try:
        with open(rules_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(
            "[family_rules] ERROR reading '{0}': {1}. Using built-in defaults.".format(
                rules_path, e
            ),
            flush=True,
        )
        return [dict(f) for f in _DEFAULT_FAMILY_RULES]

    out = []
    for fam in data.get("families", []):
        name = (fam.get("name") or "").strip()
        if not name:
            continue
        layer = (fam.get("layer") or _sanitize_layer(name)).strip().upper()
        try:
            color = int(fam.get("color", 7))
        except (TypeError, ValueError):
            color = 7
        if color < 1 or color > 255:
            color = 7
        # NOTE: keywords are NOT stripped - a trailing space is significant
        # (e.g., "TOI " matches "TOI ROOM" but not "TOI.EX.SHAFT").
        kws = []
        for k in fam.get("keywords", []):
            ks = str(k).upper()
            if ks:
                kws.append(ks)
        if not kws:
            continue
        out.append({"name": name, "layer": layer, "color": color, "keywords": kws})

    if not out:
        print(
            "[family_rules] WARNING: '{0}' loaded but contained no valid "
            "families. Using built-in defaults.".format(rules_path),
            flush=True,
        )
        return [dict(f) for f in _DEFAULT_FAMILY_RULES]

    print(
        "[family_rules] Loaded {0} families from '{1}'.".format(
            len(out), rules_path
        ),
        flush=True,
    )
    return out


def _sanitize_layer(name):
    """Make a layer-safe identifier from a family name. Keeps A-Z, 0-9,
    underscore, dash; replaces other chars with underscore."""
    out = []
    for ch in (name or "").upper():
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        elif ch == " " or ch == "/":
            out.append("_")
    s = "".join(out).strip("_") or "FAMILY"
    return s


# Family used as fallback when no rule matches. Matches "Unknown" name
# and gets the UNKNOWN_BOUNDARY layer with color 7 (white/black).
_UNKNOWN_FAMILY = {
    "name": "Unknown",
    "layer": "UNKNOWN",
    "color": 7,
    "keywords": [],
}


def family_for_label(room_name, rules):
    """Return the full family dict for a label (or _UNKNOWN_FAMILY if no
    match). Use this when you need layer + color info, not just the name.
    """
    n = (room_name or "").strip().upper()
    if not n or "UNKNOWN" in n:
        return _UNKNOWN_FAMILY
    for fam in rules:
        for kw in fam["keywords"]:
            # Whole-word, not substring: plain containment put "FRESH AIR
            # ATTENUATORS" into the TEN family and "8M EXPRESS WAY" into
            # PRESS.
            if keyword_matches(kw, n):
                return fam
    return _UNKNOWN_FAMILY


def classify_family(room_name, rules):
    """Return the family NAME for a label (or 'Unknown' if no match).
    Convenience wrapper around family_for_label that returns just the
    name (for backwards compatibility with the area-table column).
    """
    return family_for_label(room_name, rules)["name"]


def family_by_name(name, rules):
    """Look up a family dict by its exact display name (as shown in the
    UI dropdown). Returns None if no family has that name."""
    if not name:
        return None
    n = str(name).strip()
    if n == _UNKNOWN_FAMILY["name"]:
        return _UNKNOWN_FAMILY
    for fam in rules:
        if fam["name"] == n:
            return fam
    return None


def list_family_names(rules=None):
    """Ordered list of family display names for the Step 3 dropdown and
    Step 4 grouping - 'Unknown' always last."""
    rules = _FAMILY_RULES if rules is None else rules
    return [fam["name"] for fam in rules] + [_UNKNOWN_FAMILY["name"]]


def _family_layer(family):
    """Convert a family dict into its DXF layer name: '<LAYER>_BOUNDARY'."""
    return (family.get("layer") or "UNKNOWN").upper() + "_BOUNDARY"


def list_families(rules=None):
    """Display name, DXF layer and color per family, in dropdown order.

    list_family_names gives only the names; the boundary-type picker also
    shows the layer each family writes to (STAIR_BOUNDARY, MEP_BOUNDARY),
    because that is what the user sees in CAD.
    """
    rules = _FAMILY_RULES if rules is None else rules
    return [{"name": fam["name"],
             "layer": _family_layer(fam),
             "color": int(fam.get("color", 7))}
            for fam in list(rules) + [_UNKNOWN_FAMILY]]


def family_from_layer(layer_name, rules):
    """Reverse-lookup: given a DXF layer name (e.g. 'SHAFT_BOUNDARY'),
    return the family dict it belongs to. Used in Section 4 where the
    layer name is the source of truth for classification.

    Returns _UNKNOWN_FAMILY for:
      - Unrecognized layer names
      - 'UNKNOWN_BOUNDARY' (explicitly the Unknown family)
      - 'ROOM_BOUNDARY' (legacy generic layer - treated as Unknown
        unless the user reorganises into family layers)
    """
    if not layer_name:
        return _UNKNOWN_FAMILY
    L = layer_name.strip().upper()
    if not L.endswith("_BOUNDARY"):
        return _UNKNOWN_FAMILY
    suffix = L[:-len("_BOUNDARY")]   # e.g. "SHAFT_BOUNDARY" -> "SHAFT"
    if suffix in ("UNKNOWN", "ROOM", ""):
        return _UNKNOWN_FAMILY
    for fam in rules:
        if (fam.get("layer") or "").strip().upper() == suffix:
            return fam
    return _UNKNOWN_FAMILY


# Built-in default ruleset - matches family_rules.json shipped with the app.
# This is used as a fallback if the JSON file is missing or malformed.
_DEFAULT_FAMILY_RULES = [
    {"name": "Shaft", "layer": "SHAFT", "color": 1, "keywords": [
        "AHU PIPE SHAFT", "TOI.EX.SHAFT", "TOI EX SHAFT",
        "TOILET EXHAUST SHAFT", "PHE SHAFT", "ELE SHAFT", "ELEC SHAFT",
        "ELECTRICAL SHAFT", "CHW SHAFT", "DG EXHAUST", "DG SHAFT",
        "EXHAUST SHAFT", "PIPE SHAFT", "DUCT SHAFT", "SHAFT",
    ]},
    {"name": "Toilet", "layer": "TOILET", "color": 4, "keywords": [
        "WOMEN TOILET", "MEN TOILET", "TOILET",
        "P.C TOI", "P.C. TOI", "PC TOI", "TOI ",
        "JAN", "VANITY COUNTER", "WC", "WASHROOM", "REST ROOM", "RESTROOM",
    ]},
    {"name": "Lift", "layer": "LIFT", "color": 5, "keywords": [
        "LIFT ELE.PANELS", "LIFT LOBBY", "LIFT WELL", "LIFT PRE",
        "P.LIFT", "P LIFT", "PASSENGER LIFT", "SERVICE LIFT",
        "FREIGHT LIFT", "PAX", "ELEVATOR", "LIFT",
    ]},
    {"name": "Staircase", "layer": "STAIR", "color": 3, "keywords": [
        "STAIRCASE", "STAIR PR", "STAIR PREZ", "STAIR",
        "ST-", "STC", "FIRE EXIT",
    ]},
    {"name": "MEP / Services", "layer": "MEP", "color": 6, "keywords": [
        "AHU ROOM", "AHU", "CONDENSER", "FHC", "FHR", "PRESS",
        "FAPA", "FAP", "TEN", "COMM", "FCU", "VRV", "VRF",
        "PUMP ROOM", "ELECTRICAL ROOM", "ELE ROOM", "ELECT ROOM",
        "DG ROOM", "PANEL ROOM", "MEP", "SERVICE ROOM",
        "BOILER", "CHILLER", "TRANSFORMER",
    ]},
    {"name": "Circulation", "layer": "CIRC", "color": 2, "keywords": [
        "LOBBY", "CORRIDOR", "PASSAGE", "FOYER", "ENTRANCE", "EXIT",
    ]},
    {"name": "Balcony / Open", "layer": "BALCONY", "color": 30, "keywords": [
        "BALCONY", "TERRACE", "DECK", "OPEN TO SKY", "OTS",
        "GARDEN", "ATRIUM",
    ]},
    {"name": "Plumbing", "layer": "PLUMB", "color": 140, "keywords": [
        "RWP", "RAINWATER", "RAIN WATER", "WATER TANK",
        "STP", "WTP", "DRAIN",
    ]},
    {"name": "Habitable Room", "layer": "ROOM", "color": 51, "keywords": [
        "BEDROOM", "BED ROOM", "MASTER", "GUEST ROOM",
        "LIVING", "DINING", "KITCHEN", "STUDY", "OFFICE",
        "MEETING", "CONFERENCE", "CABIN", "WORKSTATION", "HALL",
    ]},
    {"name": "Storage", "layer": "STORE", "color": 9, "keywords": [
        "STORE", "STORAGE", "PANTRY", "UTILITY",
    ]},
    {"name": "Parking", "layer": "PARKING", "color": 41, "keywords": [
        "PARKING", "CAR PARK", "DRIVEWAY", "RAMP",
    ]},
]


# Load the rules once at import time. Reload by restarting the app.
_FAMILY_RULES = _load_family_rules()
_LABEL_RULES = build_rules(CONFIG, _FAMILY_RULES)


def write_areas_excel(area_data, out_path):
    """Write the area-calculation result to an Excel file with two sheets:
       Sheet 1 'Areas': Floor Area header + flat Room Areas table.
       Sheet 2 'By Family': rooms grouped by family with subtotals."""
    wb = Workbook(); ws = wb.active; ws.title = "Areas"
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="1F3A5F")
    title_font = Font(bold=True, size=13, color="1F3A5F")
    family_font = Font(bold=True, size=12, color="1F3A5F")
    family_fill = PatternFill("solid", fgColor="E6EEF8")
    subtotal_font = Font(bold=True, italic=True, size=11)
    subtotal_fill = PatternFill("solid", fgColor="F2F5FA")
    header_align = Alignment(horizontal="center", vertical="center")
    thin = Side(border_style="thin", color="BFBFBF")
    border = Border(top=thin, bottom=thin, left=thin, right=thin)

    # ---------- Sheet 1: flat list ----------
    ws.cell(row=1, column=1, value="Floor Area").font = title_font
    floor_label = area_data.get("floor_label") or ""
    floor_area = area_data.get("floor_area_sqm")
    floor_area_str = (f"{floor_area:.2f} sq.m"
                       if floor_area is not None else "-")
    floor_perim = area_data.get("floor_perimeter_m")
    floors = area_data.get("floors") or []
    # Row 2 is the whole plate - every floor together - because that is the
    # row a revision comparison reads back. Each floor follows on its own.
    total_label = (floor_label if len(floors) <= 1 else
                   "All floors ({0}) - layer {1}".format(len(floors),
                                                         floor_label))
    ws.cell(row=2, column=1, value=total_label).font = Font(bold=True)
    ws.cell(row=2, column=2, value=floor_area_str)
    if floor_perim:
        ws.cell(row=2, column=3, value="Perimeter").font = Font(bold=True)
        ws.cell(row=2, column=4, value="{0:.2f} m".format(floor_perim))
    row = 3
    if len(floors) > 1:
        for f in floors:
            ws.cell(row=row, column=1, value=f["name"])
            ws.cell(row=row, column=2, value="{0:.2f} sq.m".format(f["area_sqm"]))
            ws.cell(row=row, column=3, value="Perimeter")
            ws.cell(row=row, column=4, value="{0:.2f} m".format(f["perimeter_m"]))
            ws.cell(row=row, column=5,
                    value="{0} rooms, {1:.2f} sq.m".format(
                        f.get("room_count", 0), f.get("rooms_area_sqm", 0.0)))
            row += 1
    units_label = (area_data.get("units") or {}).get("label")
    if units_label:
        ws.cell(row=row, column=1, value="Drawing units").font = Font(bold=True)
        ws.cell(row=row, column=2, value=units_label)
        row += 1

    rooms_all = area_data.get("rooms", [])
    any_voids = any(r.get("void_count") for r in rooms_all)

    start_row = row + 1
    ws.cell(row=start_row, column=1, value="Room Areas").font = title_font
    headers = ["Sr.No", "Room Name", "Area (sq.m)", "Perimeter (m)", "Family",
               "Floor"]
    if any_voids:
        # Only shown when tracing actually found interior columns or ducts,
        # so the sheet stays narrow on drawings that have none.
        headers += ["Gross (sq.m)", "Voids deducted (sq.m)"]
    for col, h in enumerate(headers, start=1):
        c = ws.cell(row=start_row + 1, column=col, value=h)
        c.font = header_font; c.fill = header_fill
        c.alignment = header_align; c.border = border

    rooms = area_data.get("rooms", [])
    first_room_row = start_row + 2
    for ri, r in enumerate(rooms, start=first_room_row):
        ws.cell(row=ri, column=1, value=r["sr_no"]).border = border
        ws.cell(row=ri, column=2, value=r["name"]).border = border
        ws.cell(row=ri, column=3, value=r["area_sqm"]).border = border
        ws.cell(row=ri, column=4, value=r.get("perimeter_m", 0.0)).border = border
        ws.cell(row=ri, column=5, value=r.get("family", "Unknown")).border = border
        ws.cell(row=ri, column=6, value=r.get("floor", "")).border = border
        if any_voids:
            ws.cell(row=ri, column=7,
                    value=r.get("gross_area_sqm", r["area_sqm"])).border = border
            ws.cell(row=ri, column=8,
                    value=r.get("void_area_sqm", 0.0)).border = border

    # Total row - live formula over the room-area column, so editing any
    # room's area in Excel updates this automatically.
    if rooms:
        last_room_row = first_room_row + len(rooms) - 1
        total_row = last_room_row + 1
        t_label = ws.cell(row=total_row, column=2, value="Total")
        t_label.font = subtotal_font; t_label.fill = subtotal_fill
        t_val = ws.cell(row=total_row, column=3,
                         value=f"=SUM(C{first_room_row}:C{last_room_row})")
        t_val.font = subtotal_font; t_val.fill = subtotal_fill
        t_val.number_format = "0.00"
        p_val = ws.cell(row=total_row, column=4,
                         value=f"=SUM(D{first_room_row}:D{last_room_row})")
        p_val.font = subtotal_font; p_val.fill = subtotal_fill
        p_val.number_format = "0.00"
        ws.cell(row=total_row, column=1).fill = subtotal_fill
        ws.cell(row=total_row, column=5).fill = subtotal_fill
        ws.cell(row=total_row, column=6).fill = subtotal_fill

    ws.column_dimensions[get_column_letter(6)].width = 18
    ws.column_dimensions[get_column_letter(1)].width = 10
    ws.column_dimensions[get_column_letter(2)].width = 36
    ws.column_dimensions[get_column_letter(3)].width = 16
    ws.column_dimensions[get_column_letter(4)].width = 16
    ws.column_dimensions[get_column_letter(5)].width = 22
    ws.freeze_panes = "A" + str(start_row + 2)

    # ---------- Sheet 2: grouped by family ----------
    ws2 = wb.create_sheet("By Family")

    ws2.cell(row=1, column=1, value="Floor Area").font = title_font
    ws2.cell(row=2, column=1, value=floor_label).font = Font(bold=True)
    ws2.cell(row=2, column=2, value=floor_area_str)

    row = 4
    subtotal_refs = []
    overall_count = 0
    floors = area_data.get("floors") or []
    # A sheet with several floors lists each floor's families under that
    # floor; a single floor keeps the plain layout it always had.
    if len(floors) > 1:
        sections = [("{0}  -  {1:,.2f} sq.m".format(f["name"], f["area_sqm"]),
                     f.get("families") or []) for f in floors]
    else:
        sections = [(None, area_data.get("families", []))]
    floor_fill = PatternFill("solid", fgColor="1F3A5F")
    floor_font = Font(bold=True, size=13, color="FFFFFF")

    for section_title, families in sections:
        floor_refs = []
        if section_title:
            for col in range(1, 5):
                ws2.cell(row=row, column=col).fill = floor_fill
            ws2.cell(row=row, column=1, value=section_title).font = floor_font
            row += 2
        for fam in families:
            fam_name = fam["name"]
            fam_count = fam["count"]
            items = fam["rooms"]
            overall_count += fam_count

            # Pre-compute every row this family will occupy so both the
            # header total and the subtotal can reference the same range.
            header_row = row
            subheader_row = row + 1
            item_start_row = row + 2
            item_end_row = item_start_row + len(items) - 1
            subtotal_row = item_end_row + 1
            family_sum_formula = f"=SUM(C{item_start_row}:C{item_end_row})"

            # Family header row - total shown up front as a live formula
            plural = "s" if fam_count != 1 else ""
            title_cell = ws2.cell(row=header_row, column=1,
                value=f"{fam_name}  ({fam_count} item{plural})")
            title_cell.font = family_font
            title_cell.fill = family_fill
            ws2.cell(row=header_row, column=2).fill = family_fill
            c3 = ws2.cell(row=header_row, column=3, value=family_sum_formula)
            c3.font = family_font; c3.fill = family_fill
            c3.alignment = Alignment(horizontal="right")
            c3.number_format = '0.00" sq.m"'

            for col, h in enumerate(["Sr.No", "Room Name", "Area (sq.m)",
                                     "Floor"], start=1):
                c = ws2.cell(row=subheader_row, column=col, value=h)
                c.font = header_font; c.fill = header_fill
                c.alignment = header_align; c.border = border

            for sr, item in enumerate(items, start=0):
                r = item_start_row + sr
                ws2.cell(row=r, column=1, value=sr + 1).border = border
                ws2.cell(row=r, column=2, value=item["name"]).border = border
                ws2.cell(row=r, column=3, value=item["area_sqm"]).border = border
                ws2.cell(row=r, column=4,
                         value=item.get("floor", "")).border = border

            # Subtotal row - live formula over the same item range
            st_label = ws2.cell(row=subtotal_row, column=2,
                                value=f"Subtotal - {fam_name}")
            st_label.font = subtotal_font; st_label.fill = subtotal_fill
            st_val = ws2.cell(row=subtotal_row, column=3,
                              value=family_sum_formula)
            st_val.font = subtotal_font; st_val.fill = subtotal_fill
            st_val.number_format = "0.00"
            ws2.cell(row=subtotal_row, column=1).fill = subtotal_fill
            subtotal_refs.append(f"C{subtotal_row}")
            floor_refs.append(f"C{subtotal_row}")

            row = subtotal_row + 2  # blank spacer row

        if section_title and floor_refs:
            ft = ws2.cell(row=row, column=2, value="Floor total - "
                          + section_title.split("  -  ")[0])
            ft.font = Font(bold=True, size=12, color="1F3A5F")
            fv = ws2.cell(row=row, column=3,
                          value="=SUM(" + ",".join(floor_refs) + ")")
            fv.font = Font(bold=True, size=12, color="1F3A5F")
            fv.number_format = "0.00"
            row += 3

    # Overall total at the bottom - live formula summing every family's
    # subtotal cell, so it updates whenever any of those do.
    if subtotal_refs:
        gt_label = ws2.cell(row=row, column=2, value="Grand Total (all rooms)")
        gt_label.font = Font(bold=True, size=12)
        gt_val = ws2.cell(row=row, column=3,
                           value="=SUM(" + ",".join(subtotal_refs) + ")")
        gt_val.font = Font(bold=True, size=12)
        gt_val.number_format = "0.00"
        ws2.cell(row=row, column=1, value=f"({overall_count} items)").font = Font(italic=True)

    ws2.column_dimensions[get_column_letter(1)].width = 16
    ws2.column_dimensions[get_column_letter(2)].width = 38
    ws2.column_dimensions[get_column_letter(3)].width = 18
    ws2.freeze_panes = "A4"

    wb.save(out_path)