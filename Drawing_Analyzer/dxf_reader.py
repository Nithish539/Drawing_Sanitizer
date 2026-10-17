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
import collections, json, math, os, re, time
from . import jobs as _jobs
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import ezdxf
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .config import CONFIG
from . import layernames as LN
from .layernames import ai_layer               # noqa: F401  (the one naming function)
from .dxfio import (IDENTITY, effective_layer, iter_entities,
                    load_document, load_document_ex, ocs_to_wcs, set_insunits)
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
from .labels import declared_size, declared_area, name_part, match_form, keyword_regex
from . import xregions
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
# Method 2 of Step 2: a floor outline drawn as separate lines / open
# polylines is joined across gaps up to this (metres).
FLOOR_BOUNDARY_CONNECT_TOLERANCE = float(_FD.get("connect_tolerance_m", 2.0))
SOURCE_CLOSED = "Closed Polyline"
SOURCE_LINES = "Connected Open Lines"
EXCLUDED_LAYERS    = set(_FD["excluded_layers"])
FFL_PATTERN        = re.compile(r"^[+\-]?\d+(?:\.\d+)?\s*(FFL|SSL)\b"
                                r"|^(LVL|LEVEL|EL|FFL|SFL|SSL)\.?\s*[:=]?\s*[+\-]?\s*\d+(\.\d+)?\s*(M|MM)?\s*(FFL|SFL|SSL|FL|LVL)?$",
                                re.IGNORECASE)
HAS_LETTER         = re.compile(r"[A-Za-z]")
SKIP_WORDS         = {"UP", "DN", "UP.", "DN.", "NTS"}

ANNOTATE_LAYER      = LN.FLOOR if LN.ai_layer(_AN["layer"]) in (LN.FLOOR, LN.family_layer("ROOM")) \
    else LN.ai_layer(_AN["layer"])            # the floor outline: AI_FLOOR
ANNOTATE_COLOR      = int(_AN["color"])
ANNOTATE_HATCH      = bool(_AN.get("hatch", True))
HATCH_TRANSPARENCY  = min(0.9, max(0.0, float(_AN.get("hatch_transparency", 0.0))))
TEXT_HEIGHT         = float(_AN["text_height_mm"])
MAX_HV_SEARCH       = float(_BD["max_hv_search_mm"])
MAX_ROOM_SQM        = float(_BD["max_room_sqm"])
SIZE_MIN_RATIO      = float(_BD.get("size_check_min_ratio", 0.4))
SIZE_MAX_RATIO      = float(_BD.get("size_check_max_ratio", 2.5))
SEED_SEARCH_MM      = [float(v) for v in _BD.get("seed_search_mm",
                                                 [300.0, 600.0, 1000.0, 1500.0])]
MAX_LEAKED_SQM      = float(_BD.get("max_leaked_raycast_sqm", 60.0))
TRACE_MAX_VS_BOX    = float(_BD.get("trace_max_vs_box_ratio", 3.0))
USE_FLOOR_EDGES     = bool(_BD.get("floor_outline_as_wall", True))
# Rule boundary_on_evidence (2026-10-17). The facade is the glass line where
# glass is drawn: a glass line within this of the plate's edge is the facade
# and a wall to every room trace. The check after the rooms are drawn reads
# a side as standing on evidence within EVIDENCE_TOL_MM of it, and marks a
# stretch of EVIDENCE_MIN_RUN_MM or more standing on none on AI_REVIEW.
FACADE_GLASS_BAND_MM = float(_BD.get("facade_glass_band_mm", 1500.0))
EVIDENCE_TOL_MM = float(_BD.get("evidence_tol_mm", 30.0))
EVIDENCE_MIN_RUN_MM = float(_BD.get("evidence_min_run_mm", 100.0))
# a column's casing - the finish line drawn round it - up to this far off its
# face is the column's (a room stops at the casing)
EVIDENCE_COLUMN_CASING_MM = float(_BD.get("evidence_column_casing_mm", 150.0))
# a wall's finish - the plaster / tile / cladding line the rooms are traced
# against, drawn up to this far off the face of AI_WALL - is the wall's
EVIDENCE_WALL_FINISH_MM = float(_BD.get("evidence_wall_finish_mm", 120.0))
# Rule lift_lobby_to_lift_bank: the lifts within this of a lift lobby's label
# are its bank; the walls round them are followed out at most this far.
LIFT_BANK_REACH_MM = float(_BD.get("lift_bank_reach_mm", 9000.0))
LIFT_BANK_WALL_MM = float(_BD.get("lift_bank_wall_mm", 3000.0))
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
# A wall's end - a face no longer than this, with a longer face leaving each
# of its ends on the same side - is never carried on across the opening: its
# ray runs through the wall's thickness into the room beside (FHC-3 on P285).
DOOR_CAP_FACE_MAX   = float(_DR.get("cap_face_max_mm", 400.0))
DOOR_CAP_CORNER_MM  = float(_DR.get("cap_corner_mm", 30.0))
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
X_CLUSTER_FILL          = float(_XM.get("cluster_fill_min", 0.7))
# Which labels an X (or any shape) may take - config.json x_marks.
LABEL_INSIDE_TOL_MM     = float(_XM.get("label_inside_tol_mm", 100.0))
SHAFT_LIKE_FAMILIES     = tuple(_XM.get("shaft_like_families", ["Shaft", "Plumbing", "Lift", "Void"]))
SHAFT_LIKE_WORDS_RE     = re.compile(r"\b(" + "|".join(re.escape(w) for w in _XM.get(
    "shaft_like_words", ["SHAFT", "SH", "TEN", "COMM", "FAPA", "FA PA", "BMS", "ELV", "PRESS", "PREZ",
                         "RISER", "DUCT", "CUTOUT", "CUT OUT", "FHR", "FHC", "PHE", "DG", "STP", "TFA", "CHW"])) + r")\b")
X_ENCLOSURE_MAX_SQM     = float(_XM["enclosure_max_sqm"])
X_ENCLOSURE_SNAP        = float(_XM.get("enclosure_snap_margin_mm", 250.0))
X_FALLBACK_MIN_SQM      = float(_XM["fallback_min_sqm"])
# A wall line found this far inside an X's extent does not shrink it.
X_INSIDE_TOL_MM         = float(_XM.get("inside_tol_mm", 50.0))
# A label stating an X's exact size may name it from this far (a schedule).
X_STATED_REACH_MM       = float(_XM.get("stated_reach_mm", 6000.0))
# An X-marked region larger than this is a void, not a shaft.
X_VOID_AREA_THRESHOLD   = float(_XM.get("void_area_threshold_sqm", 30.0))
# X marks placed against the walls (xplace, 2026-10-16): door / window /
# cut-out tags never name a space; the gap allowed between a shaft's X and
# its wall.
X_TAGS_NEVER_NAME       = bool((CONFIG.get("x_placement") or {}).get("tags_never_name", True))
X_PLACE_GAP_MM          = float((CONFIG.get("x_placement") or {}).get("wall_gap_mm", 50.0))
SIZE_MATCH_TOL          = float(_XM.get("size_match_tolerance", 0.15))
SIZE_MATCH_MIN_MM       = float(_XM.get("size_match_min_mm", 150.0))
_GT = CONFIG.get("geometry_types") or {}
STAIR_MIN_TREADS        = int(_GT.get("stair_min_treads", 7))
STAIR_RISER_MIN         = float(_GT.get("stair_riser_min_mm", 200.0))
STAIR_RISER_MAX         = float(_GT.get("stair_riser_max_mm", 360.0))
STAIR_TREAD_MIN_LEN     = float(_GT.get("stair_tread_min_len_mm", 600.0))
# a closed outline on a cut-out layer larger than this is a slab cut-out
# round a stair or void, not a wall (see _collect_raw_segs)
CUTOUT_OUTLINE_MAX_SQM  = float((CONFIG.get("walls") or {}).get("cutout_outline_max_sqm", 12.0))
CUTOUT_OUTLINE_PLATE_SQM = float((CONFIG.get("walls") or {}).get("cutout_outline_plate_sqm", 800.0))
# a staircase traced from its treads alone must be this big
STAIR_FOUND_MIN_SQM     = float(_GT.get("stair_found_min_sqm", 8.0))
STAIR_FOUND_MAX_SQM     = float(_GT.get("stair_found_max_sqm", 120.0))
# ... or this big when it is the other part of a staircase traced beside it
# (within STAIR_PART_REACH_MM): the flight down to the basement behind a
# wall across the ground-floor stair core (H10 Tower 1)
STAIR_PART_MIN_SQM      = float(_GT.get("stair_part_min_sqm", 3.0))
STAIR_PART_REACH_MM     = float(_GT.get("stair_part_reach_mm", 1500.0))
TOILET_MIN_FIXTURES     = int(_GT.get("toilet_min_fixtures", 2))

OVERLAP_MERGE_RATIO = float(_BD["overlap_merge_ratio"])
MERGE_MIN_SIZE      = float(_BD.get("overlap_merge_min_size_ratio", 0.5))
ENGULF_RATIO        = float(_BD.get("engulf_reject_ratio", 1.5))
ENGULF_MIN_ROOMS    = int(_BD.get("engulf_min_rooms", 2))
X_UNKNOWN_LABEL     = "Unknown Shaft"
# an X in a notch of a room (traced round it): within this of the room
X_IN_ROOM_REACH_MM = float((CONFIG.get("x_placement") or {}).get("in_room_reach_mm", 150.0))
# The element pipeline (rule element_pipeline, 2026-10-17): the X marks are
# decided before any room is traced. An X stands in a room when the space
# traced from a point in_space_probe_mm outside one of its sides closes,
# between in_space_min_sqm (a wall's cavity is less) and in_space_max_sqm
# (the open floor is more); a stair's well has a flight on either side
# within well_reach_mm.
X_IN_SPACE_PROBE_MM = float((CONFIG.get("x_placement") or {}).get("in_space_probe_mm", 80.0))
X_IN_SPACE_MIN_SQM = float((CONFIG.get("x_placement") or {}).get("in_space_min_sqm", 1.0))
X_IN_SPACE_MAX_SQM = float((CONFIG.get("x_placement") or {}).get("in_space_max_sqm", 400.0))
X_IN_SPACE_PLATE_SHARE = float((CONFIG.get("x_placement") or {}).get("in_space_plate_share", 0.5))
X_WELL_REACH_MM = float((CONFIG.get("x_placement") or {}).get("well_reach_mm", 2500.0))
# what the mask holds that is never wall once decided (locked.LockedMask)
LOCKED_NEVER_WALL = ("x_shaft", "x_lift", "x_void", "x_cutout_loose")
# Rule rooms_from_spaces (2026-10-17): the rooms are the enclosed spaces,
# found with no text and named last (spaces.py; config "spaces").
_SPC = CONFIG.get("spaces") or {}
SPACES_GRID_MM = float(_SPC.get("grid_mm", 50.0))
SPACES_OPEN_FLOOR_SQM = float(_SPC.get("open_floor_sqm", 1000.0))
SPACES_OPEN_FLOOR_SHARE = float(_SPC.get("open_floor_share", 0.5))
SPACES_MIN_HALF_WIDTH_MM = float(_SPC.get("min_half_width_mm", 140.0))
SPACES_MIN_SQM = float(_SPC.get("min_sqm", 0.3))
SPACES_POCKET_MAX_SQM = float(_SPC.get("pocket_max_sqm", 1.5))
SPACES_EDGE_POCKET_MAX_SQM = float(_SPC.get("edge_pocket_max_sqm", 6.0))
SPACES_LOBBY_GAP_MM = tuple(float(v) for v in (_SPC.get("lobby_gap_mm") or (2000.0, 8000.0)))
SPACES_LOBBY_MIN_SQM = float(_SPC.get("lobby_min_sqm", 10.0))
SPACES_DRAW_UNNAMED = bool(_SPC.get("draw_unnamed", True))
SPACES_UNNAMED_LABEL = str(_SPC.get("unnamed_label", "ROOM"))
# what an unnamed X is called once the X sequence made it a lift or a void
X_LIFT_LABEL        = "LIFT"
X_VOID_LABEL        = "VOID"
VOID_LAYER          = LN.VOID              # holes deducted from rooms
BOUNDARY_METHOD     = str(_BD.get("method", "trace")).lower()
TRACE_CELL_MM       = float(_BD.get("trace_cell_mm", 50.0))
SEAL_OPENINGS_MM    = float(_BD.get("seal_openings_mm", 100.0))
# A leaking room: the doorless openings on its leak path are closed one at
# a time (at most LEAK_CLOSE_MAX re-traces) before every opening is.
LEAK_CLOSE_MAX      = int(_BD.get("leak_close_max", 8))
# An opening narrower than this between a wall-face end and another wall
# is a slit, closed for every trace; doorways (300 mm and up) are not.
SLIT_MAX_MM         = float(_BD.get("slit_max_mm", 250.0))
SLIT_TOUCH_MM       = float(_BD.get("slit_touch_mm", 5.0))
# The outlines of the wall hatches are walls to the tracer: a wall drawn
# as a hatch with a face line missing is still a wall.
HATCH_EDGES_AS_WALLS = bool(_BD.get("hatch_edges_as_walls", True))
# A doorway in a hatched wall is bridged up to this width when a room
# whose label states its size is trimmed of its doorway's depth.
DOORWAY_BRIDGE_MM   = float(_BD.get("doorway_bridge_mm", 1200.0))
# A doorway closed by what is drawn in it: a line on any layer lying on the
# wall's face line across the gap between the two wall ends (a door frame
# drawn in an unnamed block on a shaft layer) covering this share of it.
FACE_LINE_COVER     = float(_BD.get("face_line_close_share", 0.8))
FACE_LINE_GAP_MM    = tuple(_BD.get("face_line_gap_mm", [300.0, 2600.0]))
FACE_LINE_TOL_MM    = float(_BD.get("face_line_tol_mm", 20.0))
TRACE_SIMPLIFY      = float(_BD.get("trace_simplify_cells", 1.2))

MM_UNITS = DrawingUnits(1.0, "Millimetres")

# Layers excluded from "thick wall" consideration (stair internal lines etc.)
# Keyword-based  -  any layer name CONTAINING any of these strings is excluded.
_WALL_EXCLUDE_KEYWORDS = (
    "STAIR",          # stair tread lines (A-STAIRS, A3-STAIRS, STAIR, etc.)
    "STRS",           # stair tread lines on A-FLOR-STRS (XA tower)
    "RAILING",        # railing lines
    "CUTOUT",         # cutout markers
    "ST-SLAB LINE",   # stair slab lines
    "DEFPOINTS",      # AutoCAD internal layer
)

# Layers hard-excluded from being walls at all  -  annotation/symbol layers.
_HARD_EXCLUDE_KEYWORDS = (
    "DIM",                 # dimension lines (A-DIM, A-DIM S, etc.)
    "DIMENSION",           # dimensions
    "SEC.LINE",            # section cut lines across a plan (H10: V-SEC.LINE)
    "SECTION LINE",
    "CUT LINE",
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

# Step 1 of the architect's workflow: the drawing's own furniture that is
# not building - grid lines and bubbles, section and elevation marks, door
# and room tags, title blocks, legends, revision marks, landscape (trees) -
# is cleared before anything reads the floor: it never becomes a wall, a
# door, a room's side or an X. Matched on the layer's last part (an xref's
# XREF-T1_STR$0$A3-GRID is a grid) and on the names of the blocks it sits
# in. A block named for a grid is not cleared: column-layout blocks
# ("COLUMN-GRID-750") carry the columns. P25 1st floor: a section line
# (A-SYMB-SEC, in a SECTION LINE block) ran across the plate and closed a
# 530 sq.m "CAFETERIA" against the slab edge.
ANNOTATION_LAYER_RE = re.compile(str(_WD.get(
    "annotation_layer_regex",
    r"(^|[^A-Z])(GRIDS?|SYMB\w*|TAGS?|TITLE|REVISION|LEGEND|NORTH)([^A-Z]|$)"
    r"|SEC(TION)?[ ._-]*(LINE|MARK|SYMB)|ELEV[ ._-]*SYMB|BREAK ?LINE")), re.I)
ANNOTATION_BLOCK_RE = re.compile(str(_WD.get(
    "annotation_block_regex",
    r"SECTION[ ._-]*LINE|SEC(TION)?[ ._-]*MARK|ELEV(ATION)?[ ._-]*MARK|TITLE|LEGEND|NORTH[ ._-]*(ARROW|POINT)"
    r"|ARBOL|(^|[^A-Z])TREES?([^A-Z]|$)|VEGET|DRG[ ._-]*NO|TAG-")), re.I)
ANNOTATION_FILTER = bool(_WD.get("annotation_filter", True))


def _is_annotation(layer, blocks=()):
    """True for drawing furniture that is not building (Step 1: clear the
    grid lines, marks, tags and the like - see ANNOTATION_LAYER_RE)."""
    if not ANNOTATION_FILTER:
        return False
    last = str(layer or "").split("$")[-1]
    if ANNOTATION_LAYER_RE.search(last):
        return True
    for b in blocks or ():
        if b and ANNOTATION_BLOCK_RE.search(str(b).split("$")[-1]):
            return True
    return False


# How a wall is recognised. A wall is drawn as two faces a wall's thickness
# apart; a partition board, a door leaf or a counter edge is not.
WALL_T_MIN        = float(_WD.get("thickness_min_mm", 80.0))
WALL_T_MAX        = float(_WD.get("thickness_max_mm", 400.0))
WALL_MIN_FACE     = float(_WD.get("min_face_mm", 200.0))
WALL_PANEL_MAX    = float(_WD.get("panel_max_mm", 60.0))
# A ledge in front of a wall (see _ledges); config.json walls ledge_*.
LEDGE_MAX_DEPTH_MM   = float(_WD.get("ledge_max_depth_mm", 250.0))
LEDGE_NEEDS_EVIDENCE = bool(_WD.get("ledge_needs_evidence", True))
LEDGE_TOUCH_MM       = float(_WD.get("ledge_touch_mm", 30.0))
LEDGE_PARTITION_MIN  = int(_WD.get("ledge_partition_min", 2))
LEDGE_PARTITION_MM   = tuple(float(v) for v in _WD.get("ledge_partition_length_mm", [900.0, 2500.0]))
# Lines that are not walls, by their geometry (config.json walls).
DEADEND_MAX_LEN_MM     = float(_WD.get("deadend_max_len_mm", 600.0))
DEADEND_TOUCH_MM       = float(_WD.get("deadend_touch_mm", 30.0))
OBLIQUE_MIN_SUPPORT_MM = float(_WD.get("oblique_min_support_mm", 2000.0))
OBLIQUE_AXIS_TOL_DEG   = float(_WD.get("oblique_axis_tol_deg", 2.0))
BREAKMARK_GAP_MM       = float(_WD.get("breakmark_gap_mm", 150.0))
BREAKMARK_COLLINEAR_DEG = float(_WD.get("breakmark_collinear_deg", 3.0))
BREAKMARK_STROKE_RATIO = float(_WD.get("breakmark_stroke_ratio", 0.5))
BREAKMARK_STROKE_DEG   = float(_WD.get("breakmark_stroke_min_deg", 45.0))
# Room outlines are cleaned after tracing (config.json boundary).
OUTLINE_CLOSE_MM       = float(_BD.get("outline_close_mm", 30.0))
OUTLINE_MIN_FEATURE_MM = float(_BD.get("outline_min_feature_mm", 50.0))
OUTLINE_MAX_CHANGE     = float(_BD.get("outline_max_area_change", 0.02))
OUTLINE_SLIVER_MAX_MM  = float(_BD.get("outline_sliver_max_mm", 130.0))
OUTLINE_SLIVER_RATIO   = float(_BD.get("outline_sliver_ratio", 8.0))
OUTLINE_SLOT_MAX_MM    = float(_BD.get("outline_slot_max_mm", 210.0))
OUTLINE_SLOT_RATIO     = float(_BD.get("outline_slot_ratio", 3.0))


def _thin_pieces(geom, max_w, ratio, min_len=500.0):
    """The pieces of `geom` no wider than max_w and at least `ratio`
    times as long as wide (their minimum rotated rectangle)."""
    out = []
    # hair-thin bits left along a piece's base by the buffering are not
    # part of it
    geom = geom.buffer(-2.0, join_style=2).buffer(2.0, join_style=2)
    for q in getattr(geom, "geoms", [geom]):
        if q.geom_type != "Polygon" or q.is_empty or q.area < 1.0:
            continue
        r = q.minimum_rotated_rectangle
        xy = list(r.exterior.coords)[:4]
        if len(xy) < 4:
            continue
        a = math.hypot(xy[1][0] - xy[0][0], xy[1][1] - xy[0][1])
        b = math.hypot(xy[2][0] - xy[1][0], xy[2][1] - xy[1][1])
        w, L = min(a, b), max(a, b)
        if w <= max_w and L >= max(min_len, ratio * w) and q.area >= 0.6 * w * L:
            out.append(q)
    return out
STATED_GROW_TOL        = float(_BD.get("stated_grow_tol", 0.02))
STATED_CLAIM_TOL       = float(_BD.get("stated_size_claim_tol", 0.2))
STATED_SIDE_TOL        = float(_BD.get("stated_side_tol", 0.15))
# A shaft's X (or the cell beside its cut-out strip) is often closed by the
# shutter / door line a band past the shaft itself (H10 Tower 2: 1175 x
# 1200 shafts, the X and the ELEVATION line at 1200, the door line at
# 1400). Over by at most this much on one side, with a drawn line across
# at the stated depth, the shape is cut back to that line.
X_BAND_MAX_MM          = float(_BD.get("x_band_max_mm", 350.0))
# _fit_stated: an extra piece joined to the stated-size rectangle along at
# least this share of one of its sides is part of the room, not a passage.
FIT_WIDE_JOIN          = float(_BD.get("fit_wide_join", 0.6))
# ... when that piece is a strip along the room - at most this share of the
# stated area (the AHU rooms' duct strips: 9-16 %). A block as large as the
# room itself is the space next door: H10 Tower 2's terrace M. ROOM
# 3000X4150 ran into the open area beside it (+61 %), MACHINE ROOM 4000 X
# 11550 into both of its wings (+103 %).
FIT_WIDE_MAX_EXTRA     = float(_BD.get("fit_wide_max_extra", 0.25))
TRAP_DOOR_RE           = re.compile(r"^\s*(TRAP\s*DOOR|OPENABLE\s+HATCH)\b", re.IGNORECASE)
X_BAND_LINE_TOL_MM     = float(_BD.get("x_band_line_tol_mm", 40.0))
SIZE_CHECK_RANGE       = tuple(float(v) for v in _BD.get("size_check_range", [0.8, 1.25]))


def stated_fits(want, w, h):
    """A shape w x h mm fits the size a label states: its area within
    STATED_CLAIM_TOL of W*H and both sides within STATED_SIDE_TOL, either
    way round. No stated size: it fits."""
    if not want:
        return True
    a, b = want
    if a <= 0 or b <= 0 or w <= 0 or h <= 0:
        return False
    if abs(w * h / (a * b) - 1.0) > STATED_CLAIM_TOL:
        return False
    near = lambda x, y: abs(x - y) <= STATED_SIDE_TOL * y
    return (near(w, a) and near(h, b)) or (near(w, b) and near(h, a))


def shaft_like_label(text, family):
    """A label that may name an X it stands beside: of a shaft-like family
    (SHAFT_LIKE_FAMILIES) or naming a shaft (SHAFT_LIKE_WORDS - TEN, COMM,
    FAPA, BMS ...). A room's name - Electrical, Toilet, MEP room,
    Circulation, a lobby, a store - never is."""
    if family in SHAFT_LIKE_FAMILIES:
        return True
    return bool(SHAFT_LIKE_WORDS_RE.search(match_form(name_part(text or "")).upper()))
WALL_PARALLEL_DEG = float(_WD.get("parallel_tol_deg", 1.5))
# Structure is a wall whatever its thickness: RCC cores and shear walls run
# to 1,000 mm, far past any thickness test.
_ALWAYS_WALL_KEYWORDS = tuple(str(k).upper() for k in _WD.get(
    "always_walls", ["WALL", "RCC", "SLIP FORM", "COLUMN", "COL-", "-COL",
                     "SHEAR", "CONC", "STRUCT", "UPSTAND", "CUTOUT"]))
# Toilets take the sanitary fixtures beside them (see _floor_pass): a
# fixture this far outside a toilet's box is looked at, and the closed
# space holding it joins the toilet when it is no larger than this.
TOILET_FIXTURE_REACH_MM = float(_BD.get("toilet_fixture_reach_mm", 2500.0))
TOILET_FIXTURE_CELL_MAX_SQM = float(_BD.get("toilet_fixture_cell_max_sqm", 12.0))
# A room named a toilet from the fixtures in it is at most this big.
TOILET_GEOMETRY_MAX_SQM = float(_BD.get("toilet_geometry_max_sqm", 150.0))
# A shaft whose label states no size is at most this big: "PHE SH" traced
# through its access door filled 105 sq.m of the floor beside it (XA tower).
# An outline holding a plate smaller than this share of it holds another
# floor (a sheet border); a larger one inside it is the same floor.
ENCLOSED_FLOOR_MAX_SHARE = float(_BD.get("enclosed_floor_max_share", 0.6))
SHAFT_UNSIZED_MAX_SQM = float(_BD.get("shaft_unsized_max_sqm", 30.0))
# Nor is an equipment or services label with no size ever this big: a FIRE
# PANEL label traced 915 sq.m - the whole open floor it stands in (XA tower,
# 29th floor).
SERVICE_UNSIZED_MAX_SQM = float(_BD.get("service_unsized_max_sqm", 150.0))
# Nor does a name that matches no family and states no size title a space
# this big: SERVER LOCATION, a note in the open office floor of H10 Tower 1's
# 9th floor, came back holding the whole floor (1000 sq.m) once its doors
# closed it.
UNKNOWN_UNSIZED_MAX_SQM = float(_BD.get("unknown_unsized_max_sqm", 500.0))
SERVICE_FAMILIES = ("Electrical", "MEP / Services", "AHU Room", "Fire / Life Safety", "Plumbing", "Shaft")
# The services that are whole floor areas, never held to that cap: an AHU or
# plant room, a service floor, and a refuge area, which has its own (below).
SERVICE_BIG_ROOM_RE = re.compile(r"\bAHU\b|\bAIR\s*HANDL|\bPLANT\b|\bSERVICE\s*(AREA|FLOOR)\b|\bREFUGE\b")
# A refuge area is a floor area, larger than any services room: 285 FD
# Tower 2, 5th floor, REFUGE AREA-1 traced 159.9 sq.m and was dropped as too
# big for a fire label once the slab-edge line beside the core stopped
# closing it. One over the services cap is traced again with its doorways
# shut, and that is kept when it still holds this share of the room: Tower 1,
# 16th floor, REFUGE AREA-1 ran on into the balcony beyond (202.9 sq.m,
# 158.3 without it). Over the refuge cap it has run out over the floor.
REFUGE_NAME_RE = re.compile(r"\bREFUGE\b")
REFUGE_UNSIZED_MAX_SQM = float(_BD.get("refuge_unsized_max_sqm", 450.0))
REFUGE_CLOSE_MIN_SHARE = float(_BD.get("refuge_close_min_share", 0.5))
# Two overlapping boundaries merge into their bounding box only when that
# box is no more than this many times both rooms together.
MERGE_UNION_MAX_RATIO = float(_BD.get("merge_union_max_ratio", 1.5))
# A room whose label states its area is re-traced when it comes out more
# than this share off it.
AREA_STATED_TOL = float(_BD.get("area_stated_tolerance", 0.06))
_SHAFT_NAME_RE = re.compile(r"\b(SH|SHAFTS?|DUCTS?|RISERS?|CUT[- ]?OUTS?|PRE|PRESS|PREZ)\b")


def _long_strip(got) -> bool:
    """A boundary running more than 6 m and six times longer than wide: the
    cavity along a facade, not a shaft (XA tower: 14.5 x 1.6 m for PHE SH)."""
    try:
        pts = got[1] or (got[0] and [(got[0][0], got[0][1]), (got[0][2], got[0][1]),
                                     (got[0][2], got[0][3]), (got[0][0], got[0][3])])
        if not pts:
            return False
        from shapely.geometry import Polygon as _P
        c = list(_P(pts).buffer(0).minimum_rotated_rectangle.exterior.coords)
        a, b = sorted((math.dist(c[0], c[1]), math.dist(c[1], c[2])))
        return b > 6000.0 and a > 0 and b / a > 6.0
    except Exception:                                 # noqa: BLE001
        return False


def _is_shaft_name(text) -> bool:
    t = " ".join(re.sub(r"[^A-Z0-9]+", " ", str(text or "").upper()).split())
    return bool(_SHAFT_NAME_RE.search(t)) and not re.search(r"\b(LOBBY|ROOM|TOILETS?|STAIR\w*)\b", t)
# A room whose label states its size (FIRE TOWER 3100X4400) and that comes
# out more than this much larger is fitted to the stated rectangle.
STATED_FIT_SLACK = float(_BD.get("stated_size_fit_slack", 0.05))

# A shaft found from an X stands against a wall: one side within this
# distance of a wall line, along at least this share of its length.
SHAFT_WALL_TOL_MM = float(_BD.get("shaft_wall_tolerance_mm", 50.0))
SHAFT_WALL_SHARE = float(_BD.get("shaft_wall_side_share", 0.5))

# Rule 6 / 8: outline steps and spikes narrower than this are straightened.
STRAIGHTEN_MM = float(_BD.get("straighten_mm", 100.0))
# Rule 5: a room closed only by plain lines must be at least this big - a
# smaller cell is the inside of a piece of furniture or a symbol.
LINES_MIN_ROOM_SQM = float(_BD.get("lines_min_room_sqm", 1.0))
# Rule 9: a room keeping less than this share of itself once the rooms it
# overlaps are taken out was the same space twice, and is dropped.
OVERLAP_KEEP_SHARE = float(_BD.get("overlap_keep_share", 0.25))

# A room more than this share of whose outline runs along the plate edge,
# where the drawing has no line of its own, is open on that side and not
# drawn (within this distance of the edge counts as along it).
OPEN_SIDE_FRACTION = float(_BD.get("open_side_fraction", 0.4))
OPEN_SIDE_NEAR_MM = float(_BD.get("open_side_near_mm", 150.0))

# An XREF of another level's structure overlaid on a plan - the basement
# columns and walls under a typical floor (XREF-T1_STR$0$BASE._STR-COLUMN).
# Its lines are on a COLUMN layer, so they read as walls, but they are not
# on this floor: on BHUB one ran 300 mm thick across the women's toilet
# and cut its WC cubicles off. A layer is such an XREF when it is an XREF
# layer ($0$, | or XREF in the name) and names one of these levels.
_OTHER_LEVEL_XREF_WORDS = tuple(str(k).upper() for k in _WD.get(
    "other_level_xref_words",
    ["BASE", "BASEMENT", "BSMT", "FOUNDATION", "FOOTING", "PILE", "PILECAP",
     "RAFT", "PODIUM", "PLINTH", "STILT"]))


def _other_level_xref(layer_upper):
    """Whether a layer is an XREF of another level's structure."""
    lu = layer_upper or ""
    if not ("$0$" in lu or "|" in lu or "XREF" in lu):
        return False
    words = set(w for w in re.split(r"[^A-Z0-9]+", lu) if w)
    return any(k in words for k in _OTHER_LEVEL_XREF_WORDS)


def _floor_uses_lower_structure(floor_name):
    """A basement, podium or stilt floor: the BASE structure XREF is its
    own, and stays a wall."""
    return bool(re.search(r"\b(BASEMENT|BSMT|PODIUM|STILT|B\s*-?\s*\d)\b",
                          (floor_name or "").upper()))


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
    # What the title was read from: "MTEXT", "TEXT", "TEXT (2 lines)",
    # "BLOCK" ...; "" when the plate has no title.
    title_source: str = ""
    # Other candidates standing inside this one. A sheet border or a site
    # railing drawn round every plan is a closed polyline in the range too,
    # but it is not a floor: ticked, it claims every text on the sheet and
    # boundaries get drawn on every floor.
    encloses: int = 0
    # How the plate was found: a closed polyline (method 1) or joined from
    # open lines / polylines (method 2). `sources` lists both when both
    # found the same plate.
    source: str = SOURCE_CLOSED
    sources: List[str] = field(default_factory=list)
    # The floors the plate stands for, from its name or plan title
    # ("Typical 2nd, 4th & 6th Floor" -> 2nd, 4th, 6th): Step 2 lists one
    # row per floor and flags a floor claimed by two plates.
    levels: List[dict] = field(default_factory=list)
    # The floor's usage (Office, Parking, Retail...), read from its title;
    # it decides which families apply on the floor.
    usage: str = ""
    def to_ui(self):
        return {"index": self.index, "area_sqm": round(self.area_sqm, 2),
                "perimeter_m": round(self.perimeter_m, 2),
                "layer": self.layer, "num_points": self.num_points,
                "entity_type": self.entity_type,
                "name": self.name, "title": self.title,
                "title_source": self.title_source,
                "encloses": self.encloses,
                "source": self.source,
                "sources": list(self.sources or [self.source]),
                "levels": list(self.levels or []),
                "usage": self.usage}


@dataclass
class TextRecord:
    text: str
    source: str
    layer: str
    x: float
    y: float
    rotation: float = 0.0
    height: float = 0.0
    # DXF handles of the entities this text came from, so the Geometry tab
    # can move the original text. Empty for text inside a block definition,
    # which cannot move without moving every copy of the block.
    handles: tuple = ()


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
    doc, msp, units, info = load_document_ex(dxf_path, unit_override, readonly=True)
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

    # Method 1b: closed outlines inside blocks. A sheet that pasted some of
    # its floors in as blocks (H10 Tower 2: floors 1-9 sit in A$C... blocks,
    # 10-17 in model space) showed only half its floors. An outline the
    # model-space pass already has stays one plate (_dedupe_plates).
    for e, xform, depth in iter_entities(doc, msp, "LWPOLYLINE POLYLINE"):
        if depth == 0 or not _is_closed(e):
            continue
        try:
            pts = _polyline_points(e, units, xform)
        except (AttributeError, TypeError, ValueError):
            continue
        if len(pts) < 3:
            continue
        area_sqm = shoelace_area_sqm(pts)
        if area_sqm < lo or area_sqm > hi:
            continue
        candidates.append(FloorCandidate(
            index=idx, area_sqm=area_sqm, layer=effective_layer(e, xform),
            num_points=len(pts), entity_type="INSERT / " + e.dxftype(), polygon=pts,
            perimeter_m=polygon_perimeter_m(pts)))
        idx += 1

    # Method 2: outlines joined from open lines and polylines. It runs on
    # its own; a plate both methods find stays one plate, marked as found
    # by both.
    joined, joined_dupes = 0, 0
    try:
        rebuilt = reconstruct_floor_outlines(msp, units, lo, hi, doc=doc)
    except Exception:                                 # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("joining open lines into floor outlines failed")
        rebuilt = []
    for pts, layer, kinds in rebuilt:
        twin = next((c for c in candidates if c.source == SOURCE_CLOSED
                     and _plate_overlap(c.polygon, pts) >= 0.9), None)
        if twin is not None:
            if SOURCE_LINES not in twin.sources:
                twin.sources = [SOURCE_CLOSED, SOURCE_LINES]
            joined_dupes += 1
            continue
        candidates.append(FloorCandidate(
            index=idx, area_sqm=shoelace_area_sqm(pts), layer=layer,
            num_points=len(pts), entity_type=" + ".join(kinds), polygon=pts,
            perimeter_m=polygon_perimeter_m(pts),
            source=SOURCE_LINES, sources=[SOURCE_LINES]))
        idx += 1
        joined += 1
    candidates.sort(key=lambda c: c.area_sqm, reverse=True)
    before = len(candidates)
    candidates = _dedupe_plates(candidates)
    for ni, c in enumerate(candidates):
        c.index = ni
    # A plate "encloses" another floor only when that one is clearly smaller:
    # a sheet border round several plans. The same floor's inner outlines
    # (the wall line, a DEFPOINTS copy - BHUB 4th floor: 2,301 and 2,016
    # inside the 2,480 gross) do not make the gross outline a border.
    for c in candidates:
        c.encloses = sum(
            1 for o in candidates
            if o is not c and o.area_sqm < ENCLOSED_FLOOR_MAX_SHARE * c.area_sqm
            and _point_in_polygon(*_polygon_centroid(o.polygon), c.polygon))
    if candidates:
        try:
            _name_candidates(candidates, doc, msp, units, drawing_name)
        except Exception:                             # noqa: BLE001
            # Names are only a suggestion; the user can type their own.
            pass
    for c in candidates:
        c.levels = plate_levels(c.name or c.title)

    all_areas.sort(reverse=True)
    diagnostics = dict(info)
    diagnostics.update({
        "min_floor_area_sqm": lo,
        "max_floor_area_sqm": None if hi == float("inf") else hi,
        "largest_areas_sqm": [round(a, 2) for a, _ in all_areas[:8]],
        "above_threshold": len(candidates),
        "duplicate_plates_dropped": before - len(candidates),
        "joined_from_open_lines": joined,
        "joined_same_as_closed": joined_dupes,
        "connect_tolerance_m": FLOOR_BOUNDARY_CONNECT_TOLERANCE,
    })
    return candidates, units, diagnostics


# -- Step 2, method 2: floor outlines drawn as separate lines --------------
# Layers whose lines are never a floor outline.
_NOT_OUTLINE_LAYER = re.compile(r"DIM|TEXT|ANNO|HATCH|GRID|NOTE|LEADER|SYMB|TITLE|BORDER|"
                                r"FURN|TREE|PLANT|DOOR|WINDOW|STAIR|FIXT|SANIT", re.I)
_MAX_PIECES_PER_LAYER = 40000


def _plate_overlap(a, b):
    """Intersection over union of two outlines (mm points)."""
    from shapely.geometry import Polygon
    pa, pb = Polygon(a).buffer(0), Polygon(b).buffer(0)
    if pa.is_empty or pb.is_empty:
        return 0.0
    inter = pa.intersection(pb).area
    return inter / pa.union(pb).area if inter else 0.0


def reconstruct_floor_outlines(msp, units, min_sqm, max_sqm, tol_m=None, doc=None):
    """Closed floor outlines made of separate LINEs, ARCs and open
    polylines whose ends meet - or nearly meet - each other.

    Per layer (an outline is drawn on one layer; lines on unrelated layers
    are never joined): every loose end is joined to the nearest end of
    another piece when it lies within `tol_m` (default
    FLOOR_BOUNDARY_CONNECT_TOLERANCE, 2 m); ends that already touch need no
    join. The joined network is then turned into its closed faces
    (polygonize), so a branch - an inner wall meeting the outline - does not
    derail it; each face, and each group of neighbouring faces as one
    outline, is a candidate. Only valid polygons (closed, not
    self-intersecting) whose area lies within min_sqm..max_sqm are kept.

    With `doc`, the pieces inside blocks are read too, placed where their
    block is inserted: a sheet that pasted floors in as blocks may draw a
    floor's slab edge as loose LINEs there (H10 Tower 2, 5th and 11th
    floors: the 4,905.81 sq.m edge is four LINEs in a block, while every
    other floor has it as one closed rectangle).

    Returns [(points in mm, layer, ["LINE", "ARC", ...]), ...]."""
    import numpy as np
    from scipy.spatial import cKDTree
    from shapely.geometry import LineString, Polygon
    from shapely.ops import polygonize, unary_union

    tol = float(FLOOR_BOUNDARY_CONNECT_TOLERANCE if tol_m is None else tol_m) * 1000.0
    lo, hi = float(min_sqm) * 1e6, float(max_sqm) * 1e6
    pieces = collections.defaultdict(list)            # layer -> [(points, kind)]
    sag = units.from_mm(50.0)
    for e in msp.query("LINE ARC LWPOLYLINE POLYLINE"):
        layer = e.dxf.get("layer") or "0"
        if _NOT_OUTLINE_LAYER.search(layer):
            continue
        kind = e.dxftype()
        try:
            if kind == "LINE":
                s, t = e.dxf.start, e.dxf.end
                pts = [units.point_to_mm(s.x, s.y), units.point_to_mm(t.x, t.y)]
            elif kind == "ARC":
                pts = [units.point_to_mm(v.x, v.y) for v in e.flattening(sag)]
            else:
                if _is_closed(e):
                    continue                          # method 1's
                pts = _polyline_points(e, units)
                kind = "POLYLINE"
        except Exception:                             # noqa: BLE001
            continue
        length = sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
                     for i in range(len(pts) - 1))
        if length > 1.0:                              # not a dot
            pieces[layer].append((pts, kind))
    if doc is not None:
        for e, xform, depth in iter_entities(doc, msp, "LINE ARC LWPOLYLINE POLYLINE"):
            if depth == 0:
                continue                              # read above
            layer = effective_layer(e, xform) or "0"
            if _NOT_OUTLINE_LAYER.search(layer):
                continue
            kind = e.dxftype()
            try:
                if kind == "LINE":
                    raw = [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
                    pts = [units.point_to_mm(*xform.apply(x, y)) for x, y in raw]
                elif kind == "ARC":
                    pts = [units.point_to_mm(*xform.apply(v.x, v.y)) for v in e.flattening(sag)]
                else:
                    if _is_closed(e):
                        continue                      # method 1b's
                    pts = _polyline_points(e, units, xform)
                    kind = "POLYLINE"
            except Exception:                         # noqa: BLE001
                continue
            length = sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
                         for i in range(len(pts) - 1))
            if length > 1.0:
                pieces[layer].append((pts, kind))

    found = []
    for layer, items in pieces.items():
        if not items or len(items) > _MAX_PIECES_PER_LAYER:
            continue
        xs = [p[0] for pts, _k in items for p in pts]
        ys = [p[1] for pts, _k in items for p in pts]
        if (max(xs) - min(xs)) * (max(ys) - min(ys)) < lo:
            continue                                  # cannot enclose a floor
        ends = np.array([pts[j] for pts, _k in items for j in (0, -1)], dtype=float)
        owner = np.repeat(np.arange(len(items)), 2)
        tree = cKDTree(ends)
        joins = []
        k = min(8, len(ends))
        dist, idx = tree.query(ends, k=k, distance_upper_bound=tol)
        for a in range(len(ends)):
            for d, b in zip(np.atleast_1d(dist[a]), np.atleast_1d(idx[a])):
                if not np.isfinite(d) or b >= len(ends):
                    break
                if owner[b] == owner[a]:
                    continue
                if d > 1.0:                           # a gap: join to the nearest end
                    joins.append((tuple(ends[a]), tuple(ends[b])))
                break
        # A single polyline drawn round the floor but not flagged closed:
        # its end comes back to its own start (exactly, or within tol).
        for pts, _k in items:
            if len(pts) >= 3:
                gap = math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1])
                if 1.0 < gap <= tol:
                    joins.append((tuple(pts[-1]), tuple(pts[0])))
        lines = [LineString(pts) for pts, _k in items] + [LineString(j) for j in joins]
        try:
            net = unary_union(lines)                  # splits the lines where they cross
            faces = list(polygonize(net))
        except Exception:                             # noqa: BLE001
            continue
        if not faces:
            continue
        shapes = [Polygon(f.exterior) for f in faces]
        merged = unary_union(faces)
        shapes += [Polygon(g.exterior) for g in getattr(merged, "geoms", [merged])
                   if g.geom_type == "Polygon"]
        kinds = sorted({k for _p, k in items})
        for pg in shapes:
            if not pg.is_valid or pg.is_empty or not (lo <= pg.area <= hi):
                continue
            pts = [(round(x, 1), round(y, 1)) for x, y in list(pg.exterior.coords)[:-1]]
            if any(_plate_overlap(pts, f[0]) >= 0.9 for f in found):
                continue                              # the same outline again
            found.append((pts, layer, kinds))
    return found


def plate_levels(name):
    """The floors a plate name stands for, [{"key", "label", "refuge"}];
    [] when the name names no floor (then the plate is one row as named)."""
    try:
        return [{"key": lv["key"], "label": lv["label"], "refuge": bool(lv.get("refuge"))}
                # "Terrace Floor (2)": the (2) tells two plates of one name
                # apart; it is not the 2nd floor (H10 Tower 2)
                for lv in parse_levels(re.sub(r"\s*\(\d+\)\s*$", "", name or ""))]
    except Exception:                                 # noqa: BLE001
        return []


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
        c.title_source = p.get("title_source") or ""
        c.usage = floor_usage(c.title)
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
    keep = []
    for g in groups:
        best = max(g, key=lambda c: (c.source == SOURCE_CLOSED, layer_count[c.layer], c.area_sqm))
        if any(c.source != best.source for c in g):
            best.sources = sorted({s for c in g for s in (c.sources or [c.source])},
                                  key=lambda s: s != SOURCE_CLOSED)
        keep.append(best)
    return keep


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
        if (e.dxf.get("layer", "0") or "").upper() == LN.REVIEW:
            continue                                  # the element engine's review notes
        txt = _text_value(e).strip()
        if not txt: continue
        tx, ty = units.point_to_mm(*_text_position(e))
        out.append(TextRecord(text=txt, source=e.dxftype(),
                              layer=e.dxf.get("layer", "0") or "0",
                              x=tx, y=ty, rotation=_text_rotation(e),
                              height=_text_height(e) * units.mm_per_unit,
                              handles=(e.dxf.handle,)))
    return out

def _collect_texts_from_blocks(doc, msp, units=None):
    """Text inside blocks, to the configured nesting depth.

    Previously this descended exactly one INSERT level, so labels inside
    nested blocks were invisible to the whole pipeline.
    """
    units = units or MM_UNITS
    out = []
    max_depth = int(CONFIG["annotation"].get("block_recursion_depth", 3))

    # Same walk as dxfio.iter_entities, but it keeps the INSERT handles on
    # the path so a text moved in the Geometry tab can be found again
    # ("B:<insert>/<nested insert>...:<text>", see _move_texts).
    found = {}                        # each block's lookups, once per walk

    def query(container, key, types):
        got = found.get((key, types))
        if got is None:
            got = found[(key, types)] = list(container.query(types))
        return got

    def descend(container, ckey, xform, depth, seen, path):
        if depth > max_depth:
            return
        for ins in query(container, ckey, "INSERT"):
            name = ins.dxf.name
            if not name or name in seen or name not in doc.blocks:
                continue
            try:
                child = xform.enter(ins)
            except (ValueError, ZeroDivisionError, AttributeError):
                continue
            here = path + (ins.dxf.handle,)
            block = doc.blocks[name]
            for e in query(block, ("block", name), "TEXT MTEXT"):
                yield e, child, here
            for item in descend(block, ("block", name), child, depth + 1, seen | {name}, here):
                yield item

    for e, xform, path in descend(msp, ("msp",), IDENTITY, 1, frozenset(), ()):
        txt = _text_value(e).strip()
        if not txt: continue
        lx, ly = _text_position(e)
        wx, wy = units.point_to_mm(*xform.apply(lx, ly))
        world_rot = (_text_rotation(e) + xform.rot_deg) % 360.0
        out.append(TextRecord(text=txt, source="BLOCK",
                               layer=effective_layer(e, xform),
                               x=wx, y=wy, rotation=world_rot,
                               height=_text_height(e) * units.mm_per_unit
                                      * xform.scale_avg,
                               handles=("B:%s:%s" % ("/".join(path), e.dxf.handle),)))
    for ins in msp.query("INSERT"):
        for a in ins.attribs:
            txt = (a.dxf.text or "").strip()
            if not txt: continue
            ax, ay = units.point_to_mm(*_text_position(a))
            out.append(TextRecord(text=txt, source="ATTRIB",
                                   layer=a.dxf.get("layer", "0") or "0",
                                   x=ax, y=ay, rotation=_text_rotation(a),
                                   height=_text_height(a) * units.mm_per_unit,
                                   handles=(a.dxf.handle,)))
    return out


# -- Filtering -------------------------------------------------------------
# Text on a sanitary-fixture layer labels the fixture, not the room it
# stands in (see "fixture_layers" in config.json).
FIXTURE_LAYER_KEYWORDS = tuple(str(k).upper() for k in (
    (CONFIG.get("label_filters") or {}).get("fixture_layers")
    or ["FIXTURE", "FIXT", "SAN-FIX", "SAN_FIX", "SANITARY", "SANR"]))


_FIXTURE_MEMO = [None, {}]      # (the keywords it was worked out with, {layer: verdict})


def _is_fixture_layer(layer):
    # asked of every line of a floor, of a few dozen layer names: each
    # name is worked out once
    if _FIXTURE_MEMO[0] is not FIXTURE_LAYER_KEYWORDS:
        _FIXTURE_MEMO[0], _FIXTURE_MEMO[1] = FIXTURE_LAYER_KEYWORDS, {}
    memo = _FIXTURE_MEMO[1]
    try:
        return memo[layer]
    except (KeyError, TypeError):
        pass
    got = _fixture_layer_test(layer)
    try:
        memo[layer] = got
    except TypeError:
        pass
    return got


def _fixture_layer_test(layer):
    lu = (layer or "").upper()
    if any(k in lu for k in FIXTURE_LAYER_KEYWORDS):
        return True
    # "toil-fix", "P-FIX": a word of the layer name that is FIX.
    return any(w in ("FIX", "FIXS", "FIXT", "FIXTS") for w in re.split(r"[^A-Z0-9]+", lu))


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
        # the same name twice on a room layer of this app is two rooms'
        # labels (a shaft's cut-out and its room up to the door), not one
        # label in two lines
        if LN.is_room_layer(ri.layer) and " ".join(ri.text.split()) == " ".join(rj.text.split()): return False
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
                              x=top.x, y=top.y, rotation=top.rotation, height=top.height,
                              handles=tuple(h for m in members for h in m.handles)))
    return out + untouched


# -- Phase 2: extractor ----------------------------------------------------
def extract_texts(dxf_path, floors, unit_override=None):
    doc, msp, units = load_document(dxf_path, unit_override, readonly=True)
    all_records = (_collect_texts_in_modelspace(msp, units)
                   + _collect_texts_from_blocks(doc, msp, units))
    merged = _merge_stacked_lines(all_records)
    kept = [r for r in merged if _should_keep(r)]
    results = {}
    for floor in floors:
        _jobs.check()
        name = floor["name"]; poly = [tuple(p) for p in floor["polygon"]]
        floor_rules = rules_for_usage(floor.get("usage"))
        xmin, ymin, xmax, ymax = _polygon_bbox(poly)
        rows = []; sr = 1
        for r in kept:
            if not (xmin <= r.x <= xmax and ymin <= r.y <= ymax): continue
            if not _point_in_polygon(r.x, r.y, poly): continue
            rows.append({"sr_no": sr, "text": r.text, "source": r.source,
                         "layer": r.layer, "x": round(r.x, 3),
                         "y": round(r.y, 3),
                         "height": round(r.height, 2),
                         "handles": list(r.handles),
                         "family": family_for_label(r.text,
                                                     floor_rules)["name"]})
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


def _eid_boundary(pg, evidence):
    """elementid.boundary_type's verdict ("solid", "dashed", "unclear")."""
    from . import elementid as _eid
    try:
        return _eid.boundary_type(pg, evidence)[0]
    except Exception:                                 # noqa: BLE001
        return "unclear"


class _SegList(list):
    """_collect_raw_segs' segments, with `dashed`: the _seg_key of every
    segment drawn in a dashed linetype (its own, or its layer's)."""
    dashed = frozenset()


def _dashed_linetypes(doc):
    """Names (upper case) of the drawing's linetypes drawn with gaps -
    DASHED, HIDDEN, CENTER ... - not CONTINUOUS or a pattern of shapes."""
    out = set()
    try:
        for lt in doc.linetypes:
            try:
                pp = lt.simplified_line_pattern()
            except Exception:                         # noqa: BLE001
                continue
            if len(pp) >= 2 and any(g > 1e-6 for g in pp[1::2]):
                out.add((lt.dxf.name or "").upper())
    except Exception:                                 # noqa: BLE001
        return set()
    return out


def _collect_raw_segs(doc, msp, units=None):
    """Wall segments in millimetres, from modelspace and nested blocks.

    Now descends INSERTs to the configured depth rather than one level,
    and flattens polyline bulges so curved walls contribute real geometry
    instead of a single chord.

    Each segment is (sx, sy, ex, ey, layer, length, lineweight, is_door,
    not_wall): is_door marks a line on a door layer or inside a door block,
    which is evidence of an opening and never a wall face (see
    _detect_walls); not_wall marks the outline of a room-sized slab cut-out
    (the X marks and the geometry engine still see it, the walls do not).
    """
    units = units or MM_UNITS
    segs = _SegList()

    big_outlines = []          # (first seg index, count, bbox) of room-sized cut-out outlines
    annotation_dropped = [0]
    cut_diags = []             # bboxes of diagonal lines on cut-out layers (an X over a void)
    # dashed lines (the element engine: an X in a dashed outline is a cut-out)
    dashed_lt = _dashed_linetypes(doc) if doc is not None else set()
    layer_lt = {}
    if dashed_lt:
        for l_ in doc.layers:
            layer_lt[(l_.dxf.name or "").upper()] = (l_.dxf.get("linetype", "CONTINUOUS") or "CONTINUOUS").upper()
    dashed_keys = set()

    def is_dashed(e, layer):
        if not dashed_lt:
            return False
        lt = (e.dxf.get("linetype", "BYLAYER") or "BYLAYER").upper()
        if lt in ("BYLAYER", "BYBLOCK"):
            lt = layer_lt.get((layer or "").upper(), "CONTINUOUS")
        return lt in dashed_lt

    def push(pts, layer, lw, door, not_wall=False, dashed=False):
        for i in range(len(pts) - 1):
            sx, sy = pts[i]; ex, ey = pts[i + 1]
            segs.append((sx, sy, ex, ey, layer, _dist(sx, sy, ex, ey), lw,
                         door, not_wall))
            if dashed:
                dashed_keys.add(_seg_key(segs[-1]))

    for e, xform, _depth in iter_entities(doc, msp, "LINE LWPOLYLINE POLYLINE"):
        try:
            # Layer 0 inside a block is shown on the INSERT's layer; a WC
            # cubicle block placed on a furniture layer is furniture.
            layer = effective_layer(e, xform)
            if _is_annotation(layer, xform.blocks):
                annotation_dropped[0] += 1
                continue                              # step 1: grid, marks, tags
            door = _is_door_leaf(layer, xform.blocks)
            try: lw = int(e.dxf.lineweight)
            except (TypeError, ValueError, AttributeError): lw = -1
            dsh = is_dashed(e, layer)
            if e.dxftype() == "LINE":
                s0, s1 = e.dxf.start, e.dxf.end
                a = units.point_to_mm(*xform.apply(s0.x, s0.y, s0.z))
                b = units.point_to_mm(*xform.apply(s1.x, s1.y, s1.z))
                push([a, b], layer, lw, door, dashed=dsh)
                if (_is_cutout_layer_word(layer) and abs(b[0] - a[0]) > 60
                        and abs(b[1] - a[1]) > 60):
                    cut_diags.append((min(a[0], b[0]), min(a[1], b[1]),
                                      max(a[0], b[0]), max(a[1], b[1])))
            else:
                pts = _polyline_points(e, units, xform)
                # An outline on a cut-out layer the size of a room is a
                # slab cut-out drawn round a staircase or a void (285 FD:
                # a rounded 33 sq.m outline on A-CUTOUT round every stair,
                # as two open U-shaped polylines). Unless an X spans it -
                # then it is a void, and its outline is its wall - it is
                # not a wall: the stair's own walls are drawn, and the
                # trace followed the outline instead. A small one - a pipe
                # shaft's kerb inside an AHU room - is a wall to the room
                # round it, as always.
                if _is_cutout_layer_word(layer) and len(pts) > 2:
                    xs_ = [q[0] for q in pts]; ys_ = [q[1] for q in pts]
                    bw, bh = max(xs_) - min(xs_), max(ys_) - min(ys_)
                    big = (abs(shoelace_area_sqm(pts)) > CUTOUT_OUTLINE_MAX_SQM if _is_closed(e)
                           else (min(bw, bh) >= 1500.0 and bw * bh / 1e6 >= CUTOUT_OUTLINE_MAX_SQM / 2.0))
                    # a floor or slab outline kept on a cut-out layer (BHUB
                    # draws both on A3-CUTOUT) is left as it always was
                    if bw * bh / 1e6 > CUTOUT_OUTLINE_PLATE_SQM:
                        big = False
                    if big:
                        big_outlines.append((len(segs), len(pts),
                                             (min(xs_), min(ys_), max(xs_), max(ys_))))
                if _is_closed(e) and len(pts) > 1:
                    pts = pts + [pts[0]]
                push(pts, layer, lw, door, dashed=dsh)
        except (AttributeError, TypeError, ValueError):
            continue
    # (A plan drawn twice a small shift apart is dropped when the drawing
    # is read - dxfio.drop_shifted_copy. Single lines repeated on two
    # layers are kept: the wall layer's copy is the one that makes a wall.)
    for start, count, bb in big_outlines:
        ba = max(1.0, (bb[2] - bb[0]) * (bb[3] - bb[1]))
        spanned = False
        for d in cut_diags:
            w = min(bb[2], d[2]) - max(bb[0], d[0])
            h = min(bb[3], d[3]) - max(bb[1], d[1])
            if w > 0 and h > 0 and w * h >= 0.6 * ba:
                spanned = True
                break
        if spanned:
            continue
        for k in range(start, min(start + count, len(segs))):
            segs[k] = segs[k][:8] + (True,)
    segs.dashed = frozenset(dashed_keys)
    return segs


def _is_cutout_layer_word(layer):
    lu = (layer or "").upper()
    return "CUTOUT" in lu or "CUT-OUT" in lu or "CUT OUT" in lu


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


def _collect_diagonals_anywhere(doc, msp, units=None, segs=None, any_layer=False):
    """Layer-agnostic diagonal collection, in millimetres, block-recursive.

    Returns (sx, sy, ex, ey, cx, cy, layer, length). With segs (the raw
    segments, _collect_raw_segs) those are read instead of the LINEs: a
    polyline's sides count too (XA: the stair well's X drawn as a
    polyline).
    """
    units = units or MM_UNITS
    out = []

    def keep(sx, sy, ex, ey):
        dx, dy = abs(ex - sx), abs(ey - sy)
        # (layer checked by the caller)
        L = math.hypot(dx, dy)
        if L < X_DIAG_MIN_LEN or L > X_DIAG_MAX_LEN: return None
        if dx < X_DIAG_MIN_AXIS or dy < X_DIAG_MIN_AXIS: return None
        return L

    if segs is not None:
        for sg in segs:
            L = keep(sg[0], sg[1], sg[2], sg[3])
            if L is None:
                continue
            lay = sg[4] or ""
            if not any_layer and any(w in lay.upper() for w in xregions.NOT_X_LAYER_WORDS):
                continue
            out.append((sg[0], sg[1], sg[2], sg[3], (sg[0] + sg[2]) / 2, (sg[1] + sg[3]) / 2, lay, L))
        return out

    for e, xform, _depth in iter_entities(doc, msp, "LINE"):
        try:
            a0, a1 = e.dxf.start, e.dxf.end
            s = units.point_to_mm(*xform.apply(a0.x, a0.y, a0.z))
            t = units.point_to_mm(*xform.apply(a1.x, a1.y, a1.z))
        except (AttributeError, TypeError, ValueError):
            continue
        L = keep(s[0], s[1], t[0], t[1])
        if L is None: continue
        lay = effective_layer(e, xform)
        # A cross on a column or symbol layer is a steel column or a
        # drawing symbol (the X over a stair well), never a shaft.
        if any(w in (lay or "").upper() for w in xregions.NOT_X_LAYER_WORDS):
            continue
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

    def rect_of(k):
        _, _, di, dj = centers[k]
        xs, ys = [], []
        for d in (diag_segs[di], diag_segs[dj]):
            xs += [d[0], d[2]]
            ys += [d[1], d[3]]
        return (min(xs), min(ys), max(xs), max(ys))

    # A cluster is the X marks it holds. Two X marks meeting at a corner
    # (an L: a 2760 x 300 cut-out strip and a 300 x 1270 one beside PHE-
    # SHAFT 1420X1270) were one cluster drawn as their bounding box, which
    # also covered the shaft, the wall and a toilet's corner. When the X
    # rectangles fill less than X_CLUSTER_FILL of the box, the X marks
    # that overlap stay together and the rest are clusters of their own.
    from shapely.geometry import box as _xbox
    from shapely.ops import unary_union as _xunion
    split = []
    for _, idxs in groups.items():
        if len(idxs) > 1:
            rects = [rect_of(k) for k in idxs]
            bx = (min(r[0] for r in rects), min(r[1] for r in rects),
                  max(r[2] for r in rects), max(r[3] for r in rects))
            barea = (bx[2] - bx[0]) * (bx[3] - bx[1])
            u = _xunion([_xbox(*r) for r in rects])
            if barea > 0 and u.area / barea < X_CLUSTER_FILL:
                sub = list(range(len(idxs)))

                def sfind(a):
                    while sub[a] != a:
                        sub[a] = sub[sub[a]]
                        a = sub[a]
                    return a
                for a in range(len(idxs)):
                    for b in range(a + 1, len(idxs)):
                        ra, rb = _xbox(*rects[a]), _xbox(*rects[b])
                        if ra.intersection(rb).area > 0.5 * min(ra.area, rb.area):
                            sub[sfind(a)] = sfind(b)
                parts = {}
                for a in range(len(idxs)):
                    parts.setdefault(sfind(a), []).append(idxs[a])
                split.extend(parts.values())
                continue
        split.append(idxs)

    clusters = []
    for idxs in split:
        rects = [rect_of(k) for k in idxs]
        mn_x = min(r[0] for r in rects)
        mn_y = min(r[1] for r in rects)
        mx_x = max(r[2] for r in rects)
        mx_y = max(r[3] for r in rects)
        cx, cy = (mn_x + mx_x) / 2, (mn_y + mx_y) / 2
        ends = []
        for k in idxs:
            _, _, di, dj = centers[k]
            ends.append([(diag_segs[di][0], diag_segs[di][1]), (diag_segs[dj][0], diag_segs[dj][1]),
                         (diag_segs[di][2], diag_segs[di][3]), (diag_segs[dj][2], diag_segs[dj][3])])
        clusters.append({"cx": cx, "cy": cy, "mn_x": mn_x, "mn_y": mn_y,
                         "mx_x": mx_x, "mx_y": mx_y,
                         "w": mx_x - mn_x, "h": mx_y - mn_y,
                         "bbox": (mn_x, mn_y, mx_x, mx_y),
                         "rects": rects,
                         "x_ends": ends,                       # each X's four ends, round the rectangle
                         "diag_len": max(math.hypot(r[2] - r[0], r[3] - r[1]) for r in rects)})
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


def _detect_walls(raw_segs, skip=None, other_level=True, fixtures=True,
                  break_lines=True, overrun_caps=True, plate=None, hatches=None):
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

    With `other_level` (the default) an XREF of another level's structure
    - basement columns under a typical floor - is not a wall either; a
    basement floor passes False, as that structure is its own.

    By their geometry, three more kinds of line are not walls (with
    `break_lines`): a break mark (two collinear pieces across a short gap,
    a short stroke zig-zagging across it), a face in an oblique direction
    no real wall of the floor runs in, and - when the floor `plate` is
    given - a short face with a free end (a glass screen's end standing in
    a toilet). `hatches`: the wall hatches (_collect_wall_hatches).

    Returns {"faces": [...], "caps": [...], "panels": n, "centre_marks": n,
    "door_lines": n, "excluded": [(rule, (x1, y1, x2, y2))]}, segments as
    (sx, sy, ex, ey) in millimetres.
    """
    never = [k.upper() for k in _HARD_EXCLUDE_KEYWORDS]
    not_pair = [k.upper() for k in _WALL_EXCLUDE_KEYWORDS]
    skip = skip or set()
    segs, layers = [], []
    fixture_segs = []              # evidence for ledges (below)
    door_lines = 0
    for item in raw_segs:
        lu = (item[4] or "").upper()
        if any(k in lu for k in never):
            continue
        if other_level and _other_level_xref(lu):
            continue
        # A WC pan or a basin is not a wall: its two outline lines a
        # tank's depth apart read as one, and notched the toilet.
        if fixtures and _is_fixture_layer(lu):
            fixture_segs.append((item[0], item[1], item[2], item[3]))
            continue
        if skip and _seg_key(item) in skip:
            continue
        door = item[7] if len(item) > 7 else _is_door_leaf(lu, ())
        if door:
            door_lines += 1
            continue
        if len(item) > 8 and item[8]:
            continue                                    # a slab cut-out's outline
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
    partners = collections.defaultdict(list)
    for i, j, _d, ov in _parallel_pairs(segs, axes, pair_pool,
                                         WALL_T_MIN, WALL_T_MAX):
        if ov >= max(150.0, 0.5 * min(axes[i][3], axes[j][3])):
            partners[i].append(j)
            partners[j].append(i)
            for k in (i, j):
                if k in cand_set:
                    wall.add(k)

    # Ledges: a WC or basin ledge drawn 150-200 mm in front of a wall's
    # inner face pairs with that face like a wall's other face, and the
    # toilet stopped at the ledge. A wall has two faces: a line whose only
    # partners are, on one side of it and within LEDGE_MAX_DEPTH_MM, faces
    # of a wall already confirmed (a structure layer, or paired with a face
    # on their far side) is a ledge - when fixtures touch it or cubicle
    # partitions stop at it (the outer face of the same wall has neither).
    ledges = _ledges(segs, axes, wall, always, partners, fixture_segs, idx)
    wall -= ledges

    # A break line or cut line drawn across a stair is often two parallel
    # diagonals a wall's thickness apart, so it read as a wall and cut a
    # notch into the stair (or split it in two). A wall at an angle meets
    # other lines at both its ends; a break line stops in the middle of
    # the room at one end at least. Diagonal "walls" with a free end are
    # dropped.
    breaks = _free_ended_diagonals(
        [i for i in wall if not always[i] and _is_diagonal(axes[i][0])],
        segs, axes, idx) if break_lines else set()
    wall -= breaks

    # Break symbols, by their shape: a zig-zag break mark (B), and faces in
    # an oblique direction no real wall of this floor runs in (A). On BHUB
    # the stair break symbols are the only 16-degree lines - 897-1131 mm -
    # and they cut slits into both staircases.
    excluded = []
    hatch_pgs = _hatch_polys(hatches)
    if break_lines:
        marks_b = _break_marks(segs, axes, idx)
        for i in sorted(marks_b & wall):
            excluded.append(("break mark", tuple(round(v) for v in segs[i][:4])))
        wall -= marks_b
        oblique = _unsupported_oblique(segs, axes, wall, hatch_pgs)
        for i in sorted(oblique):
            excluded.append(("oblique unsupported", tuple(round(v) for v in segs[i][:4])))
        wall -= oblique

    def faces_and_caps():
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
        # A wall's end is often closed by a line that runs on past it - the
        # 309 mm line across the end of a 200 mm wall strip below a shaft. It
        # touches neither face end at its own end, so the test above misses it
        # and the strip stays open: a room's fill ran into it. A line crossing
        # the ends of two faces a wall's thickness apart closes that wall.
        overrun = []
        if overrun_caps and faces:
            overrun = _overrun_caps(segs, axes, idx, wall, panel, faces,
                                    set(map(tuple, caps)),
                                    [any(k in layers[i] for k in not_pair) for i in range(len(segs))])
            caps = caps + overrun
        return faces, caps, overrun

    faces, caps, overrun = faces_and_caps()
    # Short dead ends: a short face with an end touching nothing - the
    # 350 mm ends of a glass screen 18 mm apart left a slit in MALE TOILET.
    # Only with the plate known (its edge is something a wall may end on).
    if break_lines and plate is not None:
        closers = [k for k in idx if k not in wall and axes[k][3] <= WALL_T_MAX + 50.0]
        dead = _dead_ends(segs, axes, wall, partners, caps, hatch_pgs, plate, closers)
        if dead:
            for i in sorted(dead):
                excluded.append(("dead end", tuple(round(v) for v in segs[i][:4])))
            wall -= dead
            faces, caps, overrun = faces_and_caps()

    # The wall itself: the strip between each pair of its faces, with the
    # layers of both (for the AI_WALL_* output - see wall_polygons).
    def strip_list():
        out = []
        for i, js in partners.items():
            if i not in wall:
                continue
            for j in js:
                if j <= i or j not in wall:
                    continue
                q = _strip_between(segs, axes, i, j)
                if q is not None:
                    out.append({"pts": q[0], "t": q[1], "layers": (layers[i], layers[j]), "ij": (i, j)})
        return out
    strips = strip_list()
    # On a floor that hatches its walls, a face pair with no hatch needs
    # evidence it is a wall: two loose 2300 mm lines 200 apart inside ELE.
    # ROOM 2500x2300 (the room's divider from a cut-out, and a line 200 on)
    # read as a wall and closed a ring round the cut-out.
    if UNHATCHED_NEEDS_EVIDENCE and hatch_pgs and strips:
        drop = _unhatched_pairs(segs, axes, strips, hatch_pgs, plate)
        if drop:
            kept_faces = set()
            for st in strips:
                if st["ij"] not in drop:
                    kept_faces.update(st["ij"])
            gone = set()
            for st in strips:
                if st["ij"] in drop:
                    excluded.append(("unhatched pair inside a room",
                                     tuple(round(v) for xy in (st["pts"][0], st["pts"][2]) for v in xy)))
                    gone.update(k for k in st["ij"] if k not in kept_faces)
            if gone:
                wall -= gone
                faces, caps, overrun = faces_and_caps()
            strips = [st for st in strip_list() if st["ij"] not in drop]
    return {"faces": faces, "caps": caps, "panels": len(panel),
            "centre_marks": len(marks), "door_lines": door_lines,
            "break_lines": len(breaks), "overrun_caps": len(overrun),
            "ledges": len(ledges), "strips": strips, "excluded": excluded}


def _safe_poly(pts):
    from shapely.geometry import Polygon
    try:
        g = Polygon(pts).buffer(0)
        return g if not g.is_empty and g.area > 0 else None
    except (ValueError, TypeError):
        return None


def _unhatched_pairs(segs, axes, strips, hatch_pgs, plate):
    """The face pairs (i, j) that are not walls: on a floor whose wall
    strips lie on wall hatches (at least HATCH_SHARE_FLOOR of their area),
    a strip with less than UNHATCHED_STRIP_SHARE of it hatched is kept only
    when it continues a hatched wall in line (direction, offset and
    thickness within 20 mm, touching end to end), one of its faces runs at
    least 300 mm past it at both ends (a longer wall line), or it lies on
    the floor plate's edge band (the facade)."""
    from shapely.geometry import Polygon, LineString
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
    hat = _hatch_union(hatch_pgs)
    polys, shares = [], []
    tot, on = 0.0, 0.0
    for st in strips:
        g = _safe_poly(st["pts"])
        polys.append(g)
        if g is None:
            shares.append(1.0)
            continue
        a = g.intersection(hat).area if g.intersects(hat) else 0.0
        shares.append(a / g.area)
        tot += g.area
        on += a
    if tot <= 0 or on / tot < HATCH_SHARE_FLOOR:
        return set()                                  # this floor does not hatch its walls
    band = None
    if plate is not None:
        try:
            pl = Polygon(plate).buffer(0)
            band = pl.boundary.buffer(WALL_EDGE_MM)
        except (ValueError, TypeError):
            band = None

    def geom(k):
        st = strips[k]
        (ax, ay), (bx, by) = st["pts"][0], st["pts"][1]
        L = math.hypot(bx - ax, by - ay) or 1.0
        ux, uy = (bx - ax) / L, (by - ay) / L
        c = polys[k].centroid
        ang = math.degrees(math.atan2(uy, ux)) % 180.0
        nx, ny = -uy, ux
        if ang >= 90.0:
            nx, ny = -nx, -ny
        return ang, c.x * nx + c.y * ny, st["t"]

    hatched = [k for k in range(len(strips)) if polys[k] is not None and shares[k] >= UNHATCHED_STRIP_SHARE]
    htree = STRtree([polys[k] for k in hatched]) if hatched else None
    drop = set()
    for k, st in enumerate(strips):
        g = polys[k]
        if g is None or shares[k] >= UNHATCHED_STRIP_SHARE:
            continue
        # (c) the facade
        if band is not None and g.intersects(band) and g.intersection(band).area >= 0.5 * g.area:
            continue
        # (b) part of a longer wall line
        i, j = st["ij"]
        longer = False
        for a_, b_ in ((i, j), (j, i)):
            s, ax_ = segs[a_], axes[a_]
            t0 = (segs[b_][0] - s[0]) * ax_[1] + (segs[b_][1] - s[1]) * ax_[2]
            t1 = (segs[b_][2] - s[0]) * ax_[1] + (segs[b_][3] - s[1]) * ax_[2]
            lo, hi = max(0.0, min(t0, t1)), min(ax_[3], max(t0, t1))
            if lo >= UNHATCHED_FACE_OVERRUN_MM and ax_[3] - hi >= UNHATCHED_FACE_OVERRUN_MM:
                longer = True
                break
        if longer:
            continue
        # (a) continues a hatched wall in line
        ak, ok_, tk = geom(k)
        cont = False
        if htree is not None:
            for q in htree.query(g.buffer(5.0)):
                h = hatched[int(q)]
                ah, oh, th = geom(h)
                if (_ang_diff(ak, ah) <= 1.0 and abs(ok_ - oh) <= 20.0 and abs(tk - th) <= 20.0
                        and polys[h].distance(g) <= 5.0
                        and polys[h].intersection(g).area <= 0.2 * g.area):
                    cont = True
                    break
        if cont:
            continue
        drop.add(st["ij"])
    return drop


# The wall hatches of the floor being worked, as polygons and their union:
# several steps of one floor pass ask for them (the wall test, the slots,
# the door frames, the guardrail), and on 285 FD the conversion and the
# union took 3 s each time. Kept against the very list they came from.
_HATCH_MEMO = []                  # [[hatches, polygons, union or None]], newest first


def _hatch_polys(hatches):
    """Wall hatches (outer, holes[, layer]) as shapely polygons."""
    for ent in list(_HATCH_MEMO):
        if ent[0] is hatches and hatches is not None:
            return list(ent[1])
    from shapely.geometry import Polygon
    out = []
    for h in hatches or ():
        try:
            pg = Polygon(h[0], [r for r in (h[1] if len(h) > 1 else []) if len(r) >= 3]).buffer(0)
        except (ValueError, TypeError, IndexError):
            continue
        if not pg.is_empty:
            out.append(pg)
    if hatches:
        _HATCH_MEMO.insert(0, [hatches, out, None])
        del _HATCH_MEMO[2:]
    return list(out)


def _hatch_union(pgs):
    """unary_union(pgs), for a list _hatch_polys returned: worked out once."""
    for ent in list(_HATCH_MEMO):
        if len(ent[1]) == len(pgs) and all(a is b for a, b in zip(ent[1], pgs)):
            if ent[2] is None:
                from shapely.ops import unary_union
                ent[2] = unary_union(ent[1])
            return ent[2]
    from shapely.ops import unary_union
    return unary_union(pgs)


def _ang_diff(a, b, period=180.0):
    d = abs(a - b) % period
    return min(d, period - d)


def _break_marks(segs, axes, idx):
    """Zig-zag break marks: two near-collinear pieces (within
    BREAKMARK_COLLINEAR_DEG) across a gap of at most BREAKMARK_GAP_MM, with
    a short stroke (under BREAKMARK_STROKE_RATIO of the pieces) crossing
    the gap steeply (over BREAKMARK_STROKE_DEG). Returns the indices of
    the pieces and their strokes, whatever their angle or layer."""
    import numpy as np
    try:
        from scipy.spatial import cKDTree
    except ImportError:
        return set()
    cand = [i for i in idx if axes[i][3] >= 2.0 * BREAKMARK_GAP_MM]
    if len(cand) < 2:
        return set()
    pts, own = [], []
    for i in cand:
        s = segs[i]
        pts += [(s[0], s[1]), (s[2], s[3])]
        own += [(i, 0), (i, 1)]
    tree = cKDTree(np.array(pts, dtype=np.float64))
    mids = np.array([((segs[k][0] + segs[k][2]) / 2.0, (segs[k][1] + segs[k][3]) / 2.0) for k in idx], dtype=np.float64)
    mtree = cKDTree(mids) if len(mids) else None
    out = set()
    for p, (i, e) in enumerate(own):
        E = pts[p]
        for q in tree.query_ball_point(E, BREAKMARK_GAP_MM):
            j, f = own[q]
            if j <= i:
                continue
            if _ang_diff(axes[i][0], axes[j][0]) > BREAKMARK_COLLINEAR_DEG:
                continue
            F = pts[q]
            gap = math.hypot(F[0] - E[0], F[1] - E[1])
            if gap < 1.0:
                continue                                  # touching: a continuing line, no gap
            ux, uy = axes[i][1], axes[i][2]
            if abs((F[0] - E[0]) * -uy + (F[1] - E[1]) * ux) > max(20.0, 0.3 * gap):
                continue                                  # not in line
            # the two pieces lie on either side of the gap
            oi = (segs[i][2], segs[i][3]) if e == 0 else (segs[i][0], segs[i][1])
            oj = (segs[j][2], segs[j][3]) if f == 0 else (segs[j][0], segs[j][1])
            if math.hypot(oi[0] - oj[0], oi[1] - oj[1]) <= max(axes[i][3], axes[j][3]):
                continue                                  # overlapping, not end to end
            mx, my = (E[0] + F[0]) / 2.0, (E[1] + F[1]) / 2.0
            lim = BREAKMARK_STROKE_RATIO * min(axes[i][3], axes[j][3])
            cand_strokes = []
            if mtree is not None:
                for k in mtree.query_ball_point((mx, my), lim):
                    s = idx[k]
                    if s in (i, j) or axes[s][3] >= lim:
                        continue
                    if _ang_diff(axes[s][0], axes[i][0]) <= BREAKMARK_STROKE_DEG:
                        continue
                    cand_strokes.append(s)
            # The zig-zag: the short strokes join the two pieces' ends in a
            # chain that steps out to both sides of their line (a wall gap
            # closed by caps only ever steps to one side).
            strokes = _zigzag_path(E, F, cand_strokes, segs, ux, uy)
            if strokes:
                out.update([i, j] + strokes)
    return out


def _zigzag_path(E, F, strokes, segs, ux, uy, snap=12.0, max_edges=4):
    """The strokes chaining point E to point F (endpoints within `snap`),
    with points off the line E-F on both sides; [] when there is none."""
    def side(p):
        return (p[0] - E[0]) * -uy + (p[1] - E[1]) * ux

    def near(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1]) <= snap

    ends = [((segs[s][0], segs[s][1]), (segs[s][2], segs[s][3]), s) for s in strokes]
    best = []
    if not ends:
        return best
    # The stroke ends near a point, in the order the search takes them
    # (stroke by stroke, its first end before its second), worked out once
    # per point: a point is reached again and again by different chains.
    # numpy only narrows the field (a hair wider than `snap`); near()
    # decides, as it always has.
    import numpy as np
    ends_xy = np.array([x for a, b, _s in ends for x in (a, b)], dtype=np.float64)
    lim2 = (snap + 1e-6) ** 2
    nb = {}

    def neighbours(p):
        got = nb.get(p)
        if got is None:
            d2 = (ends_xy[:, 0] - p[0]) ** 2 + (ends_xy[:, 1] - p[1]) ** 2
            got = [(int(q) >> 1, int(q) & 1) for q in np.nonzero(d2 <= lim2)[0]
                   if near(p, ends[int(q) >> 1][int(q) & 1])]
            nb[p] = got
        return got

    stack = [(E, [], [])]
    while stack:
        p, used, pts = stack.pop()
        if used and near(p, F):
            sides = [side(q) for q in pts[:-1]]
            if any(v > snap for v in sides) and any(v < -snap for v in sides):
                return used
            continue
        if len(used) >= max_edges:
            continue
        for k, o in neighbours(p):
            a, b, s = ends[k]
            if s in used:
                continue
            y = b if o == 0 else a
            stack.append((y, used + [s], pts + [y]))
    return best


def _point_seg_dist(px, py, s):
    ax, ay, bx, by = s[0], s[1], s[2], s[3]
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    t = 0.0 if ll <= 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll))
    return math.hypot(ax + t * dx - px, ay + t * dy - py)


def _unsupported_oblique(segs, axes, wall, hatch_pgs):
    """Wall faces in a direction no real wall of the floor runs in. The
    floor's own directions - within OBLIQUE_AXIS_TOL_DEG of its dominant
    wall direction or square to it - are always walls; any other direction
    only when a face in it is at least OBLIQUE_MIN_SUPPORT_MM long, or a
    wall hatch runs along the face."""
    if not wall:
        return set()
    hist = collections.Counter()
    for i in wall:
        hist[int(round(axes[i][0])) % 90] += axes[i][3]
    dom = max(hist, key=lambda k: hist[k])
    odd = [i for i in wall if _ang_diff(axes[i][0], dom, 90.0) > OBLIQUE_AXIS_TOL_DEG]
    if not odd:
        return set()
    long_dirs = [axes[i][0] for i in wall if axes[i][3] >= OBLIQUE_MIN_SUPPORT_MM]
    # A curved wall is a chain of short faces joined end to end, each a
    # little turned from the last: no single direction is long, but the
    # chain is (A-109's curved glazing). A face in a chain at least
    # OBLIQUE_MIN_SUPPORT_MM long is supported.
    in_long_chain = _long_chains(segs, axes, wall, odd)
    hb = None
    if hatch_pgs:
        from shapely.strtree import STRtree
        bounds = [pg.boundary for pg in hatch_pgs]
        hb = (STRtree(bounds), bounds)
    out = set()
    from shapely.geometry import LineString
    for i in odd:
        a = axes[i][0]
        if any(_ang_diff(a, b) <= 1.0 for b in long_dirs):
            continue                                      # a real wall runs this way
        if i in in_long_chain:
            continue                                      # part of a curved wall
        if hb is not None:
            ln = LineString([(segs[i][0], segs[i][1]), (segs[i][2], segs[i][3])])
            zone = ln.buffer(DEADEND_TOUCH_MM, cap_style=2)
            if sum(hb[1][k].intersection(zone).length for k in hb[0].query(zone)) >= 0.5 * ln.length:
                continue                                  # a wall hatch runs along it
        out.add(i)
    return out


def _long_chains(segs, axes, wall, faces_of_interest, turn_deg=20.0):
    """The faces among `faces_of_interest` that belong to a chain of wall
    faces joined end to end (ends within DEADEND_TOUCH_MM, turning at most
    turn_deg at each joint) at least OBLIQUE_MIN_SUPPORT_MM long."""
    import numpy as np
    try:
        from scipy.spatial import cKDTree
    except ImportError:
        return set()
    W = sorted(wall)
    if not W or not faces_of_interest:
        return set()
    pts = []
    for i in W:
        s = segs[i]
        pts += [(s[0], s[1]), (s[2], s[3])]
    tree = cKDTree(np.array(pts, dtype=np.float64))
    parent = {i: i for i in W}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in tree.query_pairs(DEADEND_TOUCH_MM):
        i, j = W[a // 2], W[b // 2]
        if i != j and _ang_diff(axes[i][0], axes[j][0]) <= turn_deg:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj
    total = collections.Counter()
    for i in W:
        total[find(i)] += axes[i][3]
    return set(i for i in faces_of_interest if total[find(i)] >= OBLIQUE_MIN_SUPPORT_MM)


def _dead_ends(segs, axes, wall, partners, caps, hatch_pgs, plate, closers=()):
    """Short wall faces with a free end: shorter than DEADEND_MAX_LEN_MM,
    an end with no other wall face, cap, wall hatch or the plate's edge
    within DEADEND_TOUCH_MM, and any partner equally short and free at the
    same end. A line lying alongside (parallel, a few mm off its line - the
    other side of a glass screen) does not close its end; a short line
    across it (`closers`: any line up to a wall's thickness, a cap set
    aside as a panel among them) does."""
    from shapely.geometry import LineString, Point, Polygon
    from shapely.strtree import STRtree
    short = [i for i in wall if axes[i][3] < DEADEND_MAX_LEN_MM]
    if not short:
        return set()
    lines = [(i, LineString([(segs[i][0], segs[i][1]), (segs[i][2], segs[i][3])])) for i in wall]
    lines += [(None, LineString([(c[0], c[1]), (c[2], c[3])])) for c in caps]
    lines += [(k, LineString([(segs[k][0], segs[k][1]), (segs[k][2], segs[k][3])])) for k in closers]
    geoms = [g for _i, g in lines]
    tree = STRtree(geoms)
    other = [pg.boundary for pg in hatch_pgs or ()]
    try:
        pb = Polygon(plate).boundary if not hasattr(plate, "geom_type") else plate.boundary
        other.append(pb)
    except (ValueError, TypeError):
        pass
    otree = STRtree(other) if other else None

    def free(i, E):
        P = Point(E)
        zone = P.buffer(DEADEND_TOUCH_MM)
        for k in tree.query(zone):
            j, g = lines[k]
            if j == i or g.distance(P) > DEADEND_TOUCH_MM:
                continue
            if j is not None and _ang_diff(axes[i][0], axes[j][0]) <= 5.0:
                off = abs(_side(segs, axes, i, j))
                if off > 5.0:
                    continue                              # alongside it, not continuing it
            return False
        if otree is not None and any(other[k].distance(P) <= DEADEND_TOUCH_MM for k in otree.query(zone)):
            return False
        return True

    def ends(i):
        s = segs[i]
        return ((s[0], s[1]), (s[2], s[3]))

    out = set()
    for i in short:
        for E in ends(i):
            if not free(i, E):
                continue
            ok = True
            for j in partners.get(i, ()):
                if j not in wall:
                    continue
                Ej = min(ends(j), key=lambda p: math.hypot(p[0] - E[0], p[1] - E[1]))
                if axes[j][3] >= DEADEND_MAX_LEN_MM or not free(j, Ej):
                    ok = False
                    break
            if ok:
                out.add(i)
                break
    return out


def _join_shaft_strips(merged, family_overrides, stats):
    """Shaft-like rooms traced smaller than their stated size take the
    unnamed X shafts touching them, when the union then fits the stated
    size (stated_fits, and its area within STATED_CLAIM_TOL). Returns the
    sr_no of every room that grew."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    def pg_of(d):
        try:
            g = (Polygon(d["poly"], [h for h in d.get("holes") or () if len(h) >= 3]) if d.get("poly")
                 else Polygon([(d["bbox"][0], d["bbox"][1]), (d["bbox"][2], d["bbox"][1]),
                               (d["bbox"][2], d["bbox"][3]), (d["bbox"][0], d["bbox"][3])])).buffer(0)
            return g if not g.is_empty else None
        except (ValueError, TypeError, KeyError):
            return None
    xs = [(k, d, pg_of(d)) for k, d in enumerate(merged)
          if d.get("source") == "X" and d.get("sr_no") is None and d.get("label") == X_UNKNOWN_LABEL
          and d.get("x_class", "shaft") == "shaft"]          # (only an X the X sequence made a shaft)
    xs = [x for x in xs if x[2] is not None]
    gone, grown = set(), []
    for d in merged:
        if d.get("source") not in ("P", "S") or d.get("sr_no") is None:
            continue
        want = declared_size(d.get("label") or "")
        if not want:
            continue
        sr = d["sr_no"]
        fam = family_overrides.get(sr) or family_for_label(d.get("label") or "", _FAMILY_RULES)["name"]
        if not shaft_like_label(d.get("label") or "", fam):
            continue
        g = pg_of(d)
        if g is None:
            continue
        wa = want[0] * want[1]
        if abs(g.area / wa - 1.0) <= STATED_CLAIM_TOL:
            continue                                      # already its size
        near = [(k, xp) for k, _xd, xp in xs if k not in gone and xp.distance(g) <= 50.0]
        best = None
        cur, used = g, []
        # add the touching strips one by one, nearest the stated size first
        for _ in range(len(near)):
            cands = [(k, xp) for k, xp in near if k not in used]
            if not cands:
                break
            k, xp = min(cands, key=lambda c: abs(unary_union([cur, c[1]]).area / wa - 1.0))
            nxt = unary_union([cur, xp]).buffer(1.0, join_style=2).buffer(-1.0, join_style=2)
            if abs(nxt.area / wa - 1.0) >= abs(cur.area / wa - 1.0):
                break
            cur, used = nxt, used + [k]
            b = cur.bounds
            if cur.geom_type == "Polygon" and stated_fits(want, b[2] - b[0], b[3] - b[1]) \
                    and abs(cur.area / wa - 1.0) <= STATED_CLAIM_TOL:
                best = (cur, list(used))
        if best is None:
            continue
        u, ks = best
        d["poly"] = [(float(x), float(y)) for x, y in list(u.exterior.coords)[:-1]]
        d["holes"] = [[(float(x), float(y)) for x, y in list(r.coords)[:-1]] for r in u.interiors]
        d["bbox"] = tuple(float(v) for v in u.bounds)
        d.setdefault("evidence", []).append("joined to the X strips of the same shaft (stated size)")
        gone.update(ks)
        grown.append(sr)
        stats["shaft_strips_joined"] += len(ks)
    if gone:
        merged[:] = [d for k, d in enumerate(merged) if k not in gone]
    return grown


def _grow_to_stated(merged, wall_lines, hatches, stats, grow_x=True):
    """Rooms traced smaller than the size their label states (by more than
    STATED_FIT_SLACK) grow to it: over a strip along one side whose width
    brings that side to the stated length (within STATED_GROW_TOL), when
    the strip is not a wall hatch, not inside another room, shaft or
    cut-out, and its far edge is a wall face. Returns [(label, stated,
    before sq.m, after sq.m)]."""
    from shapely.geometry import Polygon, box as sbox
    from shapely.strtree import STRtree
    shapes = []
    for d in merged:
        try:
            if d.get("poly"):
                g = Polygon(d["poly"], [h for h in d.get("holes") or () if len(h) >= 3]).buffer(0)
            elif d.get("bbox"):                       # an X shaft drawn as its box
                g = sbox(*d["bbox"])
            else:
                g = None
        except (ValueError, TypeError):
            g = None
        shapes.append(g)
    is_x = {id(shapes[k]): merged[k].get("source") == "X" for k in range(len(merged)) if shapes[k] is not None}
    hatch_u = None
    hp = _hatch_polys(hatches)
    if hp:
        hatch_u = _hatch_union(hp)
    # wall faces by direction: vertical lines by x, horizontal by y
    vert = [w for w in wall_lines if abs(w[0] - w[2]) <= 2.0 and abs(w[1] - w[3]) > 2.0]
    horz = [w for w in wall_lines if abs(w[1] - w[3]) <= 2.0 and abs(w[0] - w[2]) > 2.0]
    others_tree = STRtree([g for g in shapes if g is not None]) if any(g is not None for g in shapes) else None
    others = [g for g in shapes if g is not None]
    out = []
    for k, d in enumerate(merged):
        g = shapes[k]
        if g is None or d.get("source") == "V":
            continue
        if d.get("source") == "X" and not grow_x:
            continue                                  # the X'd strip is the opening (x_grows_to_cell)
        want = declared_size(d.get("label") or "")
        if not want:
            continue
        wa = want[0] * want[1] / 1e6
        area = g.area / 1e6
        if area >= wa * (1.0 - STATED_FIT_SLACK):
            continue
        b = g.bounds
        W, H = b[2] - b[0], b[3] - b[1]
        done = None
        for sw, sh in ((want[0], want[1]), (want[1], want[0])):
            for axis in ("x", "y"):
                cur, other_cur = (W, H) if axis == "x" else (H, W)
                tgt, other_tgt = (sw, sh) if axis == "x" else (sh, sw)
                if abs(other_cur - other_tgt) > STATED_GROW_TOL * other_tgt:
                    continue                          # the other side must already fit
                need = tgt - cur
                if need <= STATED_GROW_TOL * tgt:
                    continue
                for side in (-1, 1):
                    edge = (b[0] if side < 0 else b[2]) if axis == "x" else (b[1] if side < 0 else b[3])
                    lo, hi = (b[1], b[3]) if axis == "x" else (b[0], b[2])
                    pool = vert if axis == "x" else horz
                    best = None
                    for w in pool:
                        pos = w[0] if axis == "x" else w[1]
                        width = (edge - pos) * -side
                        if width <= 0 or abs(cur + width - tgt) > STATED_GROW_TOL * tgt:
                            continue
                        a, c = (min(w[1], w[3]), max(w[1], w[3])) if axis == "x" else (min(w[0], w[2]), max(w[0], w[2]))
                        if min(c, hi) - max(a, lo) < 0.8 * (hi - lo):
                            continue                  # the face must run along the side
                        if best is None or abs(cur + width - tgt) < abs(cur + best - tgt):
                            best = width
                    if best is None:
                        continue
                    far = edge + side * best
                    strip = (sbox(min(edge, far), lo, max(edge, far), hi) if axis == "x"
                             else sbox(lo, min(edge, far), hi, max(edge, far)))
                    if hatch_u is not None and strip.intersection(hatch_u).area > 0.5 * strip.area:
                        continue                      # a wall hatch: a real wall
                    taken, cut = False, []
                    if others_tree is not None:
                        for j in others_tree.query(strip):
                            o = others[j]
                            if o is g:
                                continue
                            if o.intersection(strip).area > 0.1 * strip.area:
                                # an X shaft lying in the strip stays a
                                # shaft, cut out of the room; anything
                                # else there is another room's
                                if is_x.get(id(o)) and o.intersection(strip).area >= 0.9 * o.area:
                                    cut.append(o)
                                    continue
                                taken = True
                                break
                    if taken:
                        continue
                    done = (strip, cut)
                    break
                if done is not None:
                    break
            if done is not None:
                break
        if done is None:
            continue
        strip, cut = done
        ng = g.union(strip)
        for o in cut:
            ng = ng.difference(o)
        ng = ng.buffer(0).simplify(1.0, preserve_topology=True)
        if ng.geom_type != "Polygon":
            continue
        d["poly"] = [(float(x), float(y)) for x, y in list(ng.exterior.coords)[:-1]]
        d["holes"] = [[(float(x), float(y)) for x, y in list(r.coords)[:-1]] for r in ng.interiors]
        d["bbox"] = tuple(float(v) for v in ng.bounds)
        d.setdefault("evidence", []).append("fitted to stated size (grown)")
        d["_grown_strip"] = [(float(x), float(y)) for x, y in list(strip.exterior.coords)[:-1]]
        stats["stated_size_grown"] += 1
        out.append((d.get("label") or "", "{0:g}x{1:g}".format(*want), round(area, 2), round(ng.area / 1e6, 2)))
    return out


def clean_outline(poly, holes=None):
    """A room outline without slits and spikes: closed by
    OUTLINE_CLOSE_MM (a slit narrower than twice that is filled), then
    opened by half OUTLINE_MIN_FEATURE_MM (a spike narrower than that is
    cut off), square corners kept. Notches and holes 100 mm and wider -
    columns, real walls - stay. Returns (poly, holes, changed); unchanged
    when the result would move the area by more than OUTLINE_MAX_CHANGE."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    if not poly or len(poly) < 3:
        return poly, holes, False
    try:
        pg = Polygon(poly, [h for h in holes or () if len(h) >= 3])
        if not pg.is_valid:
            pg = pg.buffer(0)
        if pg.is_empty or pg.area <= 0:
            return poly, holes, False
        r, f = OUTLINE_CLOSE_MM, OUTLINE_MIN_FEATURE_MM / 2.0
        g = pg.buffer(r, join_style=2).buffer(-r, join_style=2)
        g = g.buffer(-f, join_style=2).buffer(f, join_style=2)
        parts = [p for p in getattr(g, "geoms", [g]) if p.geom_type == "Polygon" and not p.is_empty]
        if not parts:
            return poly, holes, False
        g = max(parts, key=lambda p: p.area)
        # slivers: a partition stub the outline ran in along and back out of
        # is filled; a slot up beside the next room is cut
        if OUTLINE_SLIVER_MAX_MM > 0:
            rs = OUTLINE_SLIVER_MAX_MM / 2.0 + 1.0
            closed = g.buffer(rs, join_style=2).buffer(-rs, join_style=2)
            fill = _thin_pieces(closed.difference(g), OUTLINE_SLIVER_MAX_MM, OUTLINE_SLIVER_RATIO)
            if fill:
                g = unary_union([g] + [q.buffer(0.5, join_style=2) for q in fill]).buffer(0)
        if OUTLINE_SLOT_MAX_MM > 0:
            rt = OUTLINE_SLOT_MAX_MM / 2.0 + 1.0
            opened = g.buffer(-rt, join_style=2).buffer(rt, join_style=2)
            cut = _thin_pieces(g.difference(opened), OUTLINE_SLOT_MAX_MM, OUTLINE_SLOT_RATIO)
            if cut:
                g = g.difference(unary_union([q.buffer(0.5, join_style=2) for q in cut])).buffer(0)
        parts = [p for p in getattr(g, "geoms", [g]) if p.geom_type == "Polygon" and not p.is_empty]
        if not parts:
            return poly, holes, False
        g = max(parts, key=lambda p: p.area).simplify(1.0, preserve_topology=True)
    except Exception:                                 # noqa: BLE001
        return poly, holes, False
    if abs(g.area - pg.area) > OUTLINE_MAX_CHANGE * pg.area:
        return poly, holes, False
    new_poly = [(float(x), float(y)) for x, y in list(g.exterior.coords)[:-1]]
    new_holes = [[(float(x), float(y)) for x, y in list(rr.coords)[:-1]] for rr in g.interiors]
    changed = (len(new_poly) != len(poly) or len(new_holes) != len([h for h in holes or () if len(h) >= 3])
               or g.symmetric_difference(pg).area > 1.0)
    return (new_poly, new_holes, True) if changed else (poly, holes, False)


def _strip_between(segs, axes, i, j, min_len=50.0):
    """The quadrilateral between segment i and its parallel partner j, over
    the stretch they run alongside each other: ([4 points], thickness)."""
    s, a = segs[i], axes[i]
    ux, uy, L = a[1], a[2], a[3]
    t0 = (segs[j][0] - s[0]) * ux + (segs[j][1] - s[1]) * uy
    t1 = (segs[j][2] - s[0]) * ux + (segs[j][3] - s[1]) * uy
    lo, hi = max(0.0, min(t0, t1)), min(L, max(t0, t1))
    if hi - lo < min_len:
        return None
    d = _side(segs, axes, i, j)                       # across, to j's line
    nx, ny = -uy, ux
    A = (s[0] + ux * lo, s[1] + uy * lo)
    B = (s[0] + ux * hi, s[1] + uy * hi)
    return [A, B, (B[0] + nx * d, B[1] + ny * d), (A[0] + nx * d, A[1] + ny * d)], abs(d)


def pair_quads(lines, dmin=None, dmax=None):
    """Quadrilaterals between parallel pairs of lines a wall's thickness
    apart - a door opening between the two closing lines carried across it."""
    segs = [tuple(l[:4]) for l in lines or ()]
    axes = [_axis(s) for s in segs]
    idx = [k for k, a in enumerate(axes) if a is not None]
    out = []
    for i, j, _d, _ov in _parallel_pairs(segs, axes, idx, dmin or WALL_T_MIN, dmax or WALL_T_MAX):
        q = _strip_between(segs, axes, i, j, min_len=100.0)
        if q is not None:
            out.append(q[0])
    return out


# -- The walls, as drawn in the output (AI_WALL, one layer)
# config.json "wall_output". A wall is never a room and never in an area.
_WO = CONFIG.get("wall_output") or {}
WALL_EDGE_MM = float(_WO.get("external_edge_mm", 300.0))
# Runs are joined into pieces within three groups - along the plate's edge,
# structure (thick, on a structure hatch, a rejected C / L column shape) and
# the rest - as the three wall classes were: a balcony grows over the parapet
# piece along its open side (_grow_balconies), which one piece of every wall
# that touches it would not be. All of them are drawn on AI_WALL, as one.
WALL_CORE_MIN_MM = float(_WO.get("core_min_thickness_mm", 300.0))
# A window or cut-out in a wall is part of the wall (drawn over it on its
# own layer): the corrected walls of BHUB and XA run on through louvres
# and cut-outs. A balcony's parapet it grew over stays a wall too
# (balcony_parapet_not_wall false): the balcony is measured to its outer
# face and the parapet is drawn on AI_WALL as well.
OPENINGS_IN_WALLS = bool(_WO.get("openings_in_walls", True))
# Fix 4: the wall band filled across a window from the wall this far either
# side of it (0 turns it off); a fill larger than this is not a window's
WINDOW_BRIDGE_MM = float(_WO.get("window_bridge_mm", 600.0))
WINDOW_BRIDGE_MAX_SQM = float(_WO.get("window_bridge_max_sqm", 1.5))
WINDOW_BRIDGE_MAX_MM = float(_WO.get("window_bridge_max_mm", 600.0))      # thickest band bridged
BALCONY_PARAPET_NOT_WALL = bool(_WO.get("balcony_parapet_not_wall", False))
DRAW_DOORS = bool(_WO.get("draw_doors", True))
DOOR_ACI = int(_WO.get("door_color", 3))
# Each door is drawn as one line joining the walls either side (Test_2), on
# the face of the room it opens into - a shaft's room run to its door, on
# the outer face; the opening is kept in its XDATA. false: the opening as a
# closed polyline, as before.
DOOR_AS_LINE = bool(_WO.get("door_as_line", True))
# a door line drawn by hand (no opening stored): read as a band this far
# either side of it
DOOR_LINE_BAND_MM = float(_WO.get("door_line_band_mm", 100.0))
# a secondary door (inside one room - a WC cubicle's) on AI_S_DOORS
DOOR_SECONDARY_ACI = int(_WO.get("door_secondary_color", 30))
# A door opening (door_openings): found from a door swing - an arc of this
# radius and sweep on any layer - at the gap in a wall, or with its leaf
# drawn open where there is no wall (a cubicle board).
_DR = _WO.get("door_radius_mm") or [450, 1300]
DOOR_ARC_R_MIN, DOOR_ARC_R_MAX = float(_DR[0]), float(_DR[1])
DOOR_SWEEP = tuple(float(v) for v in (_WO.get("door_sweep_deg") or [10, 100]))
DOOR_JAMB_REACH_MM = float(_WO.get("door_jamb_reach_mm", 350.0))
DOOR_LEAF_MM = float(_WO.get("door_leaf_mm", 12.0))
DOOR_SHUT_DEPTH_MM = float(_WO.get("door_between_ends_depth_mm", 100.0))
# Rule doors_need_evidence (door_openings): an opening whose long side runs
# along a wall face - within DOOR_FACE_TOL_MM of it for DOOR_ALONG_FACE_SHARE
# of its width or more - lies beside a wall, not in a gap in one, and is no
# door; from DOOR_DOUBT_FACE_SHARE it is doubtful and marked on AI_REVIEW
# instead. Two openings in one gap (parallel, overlapping along it for half
# the narrower's width, no more than DOOR_SAME_GAP_MM apart across) are one:
# the narrower is the door's leaf, the wider is the gap measured on past it.
DOOR_FACE_TOL_MM = float(_WO.get("door_face_tolerance_mm", 40.0))
DOOR_ALONG_FACE_SHARE = float(_WO.get("door_along_face_share", 0.6))
DOOR_DOUBT_FACE_SHARE = float(_WO.get("door_doubt_face_share", 0.35))
DOOR_SAME_GAP_MM = float(_WO.get("door_same_gap_mm", 300.0))
# A door drawn by its leaf shut in the opening (a panel: two door-layer
# lines 10-80 mm apart over 90% of their length) with a frame at both
# ends or its leaf drawn open from one (P285 11th floor: the shaft doors
# FD4 / FD8 and D4, whose swings - 325 mm, or 682 mm about a point outside
# the jamb, or 30 degrees inside an unbroken wall - describe no opening).
_LP = _WO.get("door_leaf_panel_mm") or [450, 1300]
DOOR_LEAF_PANEL_MIN, DOOR_LEAF_PANEL_MAX = float(_LP[0]), float(_LP[1])
DOOR_LEAF_DOORS = bool(_WO.get("door_leaf_doors", True))
# Door swings drawn as ELLIPSE (285 FD, the P285 11th floor) read as arcs
# when they are a circle's arc: radii within this share of one another.
# a space labelled as a lift with this many hinged doors opening into it is
# not a lift well (rules: lift_not_with_doors)
LIFT_REFUSE_DOORS = int(_WO.get("lift_refuse_doors", 2))
# rules: join_shaft_strips - X-marked strips up to this far apart
SHAFT_STRIP_GAP_MAX_MM = float((CONFIG.get("x_marks") or {}).get("strip_join_gap_max_mm", 700.0))
DOOR_ELLIPSES = bool(_WO.get("door_ellipses", True))
DOOR_ELLIPSE_ROUND = float(_WO.get("door_ellipse_round", 0.08))
WALL_STRUCTURE_HATCH = set(str(p).upper() for p in _WO.get(
    "structure_hatch_patterns", ["AR-CONC", "CONCRETE", "SOLID", "ANSI31"]))
WALL_MIN_PIECE_SQM = float(_WO.get("min_piece_sqm", 0.05))
WALL_SIMPLIFY_MM = float(_WO.get("simplify_mm", 2.0))
_WALL_OVERLAY_LAYER = re.compile(str(_WO.get(
    "not_wall_layers", r"^\d+(\.\d+)+\s|FINI|FINISH|THRESH|SILL|SKIRT")), re.I)
WALL_DEDUPE_MM = float(_WO.get("dedupe_mm", 2.0))
WALL_ABSORB_SHARE = float(_WO.get("wall_absorb_share", 0.7))
UNHATCHED_NEEDS_EVIDENCE = bool(_WO.get("unhatched_needs_evidence", True))
HATCH_SHARE_FLOOR = float(_WO.get("hatch_share_floor", 0.6))
UNHATCHED_STRIP_SHARE = float(_WO.get("unhatched_strip_share", 0.3))
UNHATCHED_FACE_OVERRUN_MM = float(_WO.get("unhatched_face_overrun_mm", 300.0))
COLUMN_ACI = int(_WO.get("column_color", 1))
COLUMN_HATCH = str(_WO.get("column_hatch", "ANSI31")).upper()
COLUMN_HATCH_SCALE = float(_WO.get("column_hatch_scale", 50.0))
COLUMN_LABEL = str(_WO.get("column_label", "C"))
# Walls are one class, drawn on one layer: a wall is a wall, whether it
# stands on the plate's edge, round the core or between two rooms.
WALL_CLASSES = (LN.WALL,)


def _wall_length_mm(pg):
    """Centre-line length of a wall piece: exact for a rectangle (from its
    area and perimeter), close for an L or a T."""
    P, A = pg.length, pg.area
    h = P / 2.0
    disc = h * h - 4.0 * A
    return (h + math.sqrt(disc)) / 2.0 if disc > 0 else h / 2.0


def wall_polygons(strips, floor_poly, hatches=(), columns=(), windows=(), doors=(), extra_core=(), bridge=(),
                  counts=None, veto=(), locked=None):
    """The walls of a floor as polygons, on the one wall layer (AI_WALL).

      a. duplicate strips (the same four corners within WALL_DEDUPE_MM) go;
      b. a strip WALL_ABSORB_SHARE of whose area lies on a longer strip goes
         - a mirror or backsplash line pairing with a wall face, and its
         overhang past the wall; never a strip the wall hatch fills
         (walltrace "hatched");
      c. the strips left and the wall hatches are one wall mass, with
         columns, windows and door openings cut out;
      d. the mass is split into runs - straight pieces, a junction going to
         the longer run - each of them a wall (AI_WALL); a C / L shape
         rejected as a column (extra_core) is a wall too;
      e. a piece under WALL_MIN_PIECE_SQM goes unless it touches another.
    Runs that touch are one piece within their group: along the plate's
    edge, structure, the rest (as the three wall classes were joined, so a
    balcony's parapet is still a piece of its own). draw_walls merges them.

    strips: _detect_walls()["strips"]; hatches: _collect_wall_hatches
    (outer, holes, layer, pattern); columns, windows, doors, extra_core:
    polygons (shapely or point lists). `counts`, when given, receives how
    many strips (a) and (b) removed. Returns ({AI_WALL: [Polygon]},
    {AI_WALL: {"count", "length_m", "area_sqm"}})."""
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union
    from shapely.strtree import STRtree

    def poly(p):
        if p is None:
            return None
        if hasattr(p, "geom_type"):
            return p
        try:
            g = Polygon(p).buffer(0)
            return g if not g.is_empty else None
        except (ValueError, TypeError):
            return None

    plate = poly(floor_poly)
    empty = ({c: [] for c in WALL_CLASSES}, {c: {"count": 0, "length_m": 0.0, "area_sqm": 0.0} for c in WALL_CLASSES})
    if plate is None:
        return empty
    reach = plate.buffer(WALL_EDGE_MM)
    edge_band = plate.boundary.buffer(WALL_EDGE_MM)

    # the strips: a run each, its direction, offset and thickness
    veto_u = unary_union([g for g in (poly(v) for v in veto or ()) if g is not None]) if veto else None
    # `locked` (the element pipeline): what is decided already - a shaft's
    # slab opening, a lift well, a void, a loose cut-out, a door opening, a
    # column - is never wall: a strip mostly inside it goes, and the wall
    # mass is cut back round it before the runs are formed
    locked_u = None
    if locked is not None:
        locked_u = locked if hasattr(locked, "geom_type") else \
            unary_union([g for g in (poly(v) for v in locked or ()) if g is not None])
        if locked_u is not None and locked_u.is_empty:
            locked_u = None
    items = []
    for st in strips or ():
        lays = st.get("layers") or ()
        if any(_WALL_OVERLAY_LAYER.search(l or "") for l in lays):
            continue                  # the area-plan overlay or a finish line, not a wall
        pts = st["pts"]
        g = poly(pts)
        if g is None or g.area <= 0 or not g.intersects(reach):
            continue
        if veto_u is not None and g.intersects(veto_u) and g.intersection(veto_u).area >= 0.5 * g.area:
            if counts is not None:
                counts["vetoed"] = counts.get("vetoed", 0) + 1
            continue                  # inside a room grown to its stated size
        if locked_u is not None and g.intersects(locked_u) and g.intersection(locked_u).area >= 0.5 * g.area:
            if counts is not None:
                counts["in_locked"] = counts.get("in_locked", 0) + 1
            continue                  # inside an element decided already
        (ax, ay), (bx, by) = pts[0], pts[1]
        L = math.hypot(bx - ax, by - ay)
        if L <= 0:
            continue
        items.append({"g": g, "pts": pts, "L": L, "t": float(st.get("t") or 0.0),
                      "ang": math.degrees(math.atan2(by - ay, bx - ax)) % 180.0,
                      "ux": (bx - ax) / L, "uy": (by - ay) / L, "hatched": bool(st.get("hatched"))})
    n0 = len(items)

    # (a) duplicates
    seen, uniq = set(), []
    for it in items:
        key = tuple(sorted((int(round(x / WALL_DEDUPE_MM)), int(round(y / WALL_DEDUPE_MM))) for x, y in it["pts"]))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    n_dup = n0 - len(uniq)

    # (b) absorbed by a longer strip
    kept = []
    if uniq:
        tree = STRtree([it["g"] for it in uniq])
        for k, it in enumerate(uniq):
            gone = False
            if it["hatched"]:
                kept.append(it)               # the hatch says it is wall (a column's 50 mm foot past the junction)
                continue
            for j in tree.query(it["g"]):
                j = int(j)
                o = uniq[j]
                if j == k or o["L"] <= it["L"]:
                    continue
                if it["g"].intersection(o["g"]).area >= WALL_ABSORB_SHARE * it["g"].area:
                    gone = True
                    break
            if not gone:
                kept.append(it)
    n_abs = len(uniq) - len(kept)
    if counts is not None:
        counts["strips"] = counts.get("strips", 0) + n0
        counts["duplicates_removed"] = counts.get("duplicates_removed", 0) + n_dup
        counts["absorbed_removed"] = counts.get("absorbed_removed", 0) + n_abs

    # (c) one wall mass
    hatch_pgs, struct = [], []
    for h in hatches or ():
        try:
            pg = Polygon(h[0], [r for r in (h[1] if len(h) > 1 else []) if len(r) >= 3]).buffer(0)
        except (ValueError, TypeError, IndexError):
            continue
        if pg.is_empty or pg.area <= 0 or not pg.intersects(reach):
            continue
        hatch_pgs.append(pg)
        if len(h) > 3 and str(h[3] or "").upper() in WALL_STRUCTURE_HATCH:
            struct.append(pg)
    core_shapes = [g for g in (poly(p) for p in extra_core or ()) if g is not None and g.intersects(reach)]
    parts = [it["g"] for it in kept] + hatch_pgs + core_shapes
    if not parts:
        return empty
    mass = unary_union(parts).intersection(reach)
    # (Fix 4) a window never cuts a wall: the wall band runs on through it.
    # Where the window's own lines broke the wall faces, the strips either
    # side pair with the window and the band is left thin there (XA lift
    # bank: 109 mm of a 300 mm wall). The band is filled across the window
    # from the wall either side of it; the window is drawn over it.
    # `bridge`: the windows (openings) alone, given by the floor pass; a
    # shear wall or a column among the core shapes is never run on into
    # the wall beside it, and a caller that passes none bridges nothing
    bridge_shapes = [g for g in (poly(p) for p in (bridge or ())) if g is not None and g.intersects(reach)]
    if WINDOW_BRIDGE_MM > 0 and bridge_shapes:
        fills = []
        for g in bridge_shapes:
            try:
                # the window's own rectangle, run on along its length into
                # the wall either side (never a hull: that fills corners in
                # with slanted edges and eats into the rooms)
                mr_ = g.minimum_rotated_rectangle
                if mr_.geom_type != "Polygon":
                    continue
                cs_ = list(mr_.exterior.coords)[:4]
                e0 = (cs_[1][0] - cs_[0][0], cs_[1][1] - cs_[0][1])
                e1 = (cs_[2][0] - cs_[1][0], cs_[2][1] - cs_[1][1])
                l0, l1 = math.hypot(*e0), math.hypot(*e1)
                if min(l0, l1) <= 0:
                    continue
                u_ = (e0[0] / l0, e0[1] / l0) if l0 >= l1 else (e1[0] / l1, e1[1] / l1)
                ends = (cs_[0], cs_[1], cs_[2], cs_[3])
                # the two end faces of the rectangle (across its length)
                if l0 >= l1:
                    faces_ = ((ends[0], ends[3]), (ends[1], ends[2]))
                    dirs_ = ((-u_[0], -u_[1]), u_)
                else:
                    faces_ = ((ends[0], ends[1]), (ends[3], ends[2]))
                    dirs_ = ((-u_[0], -u_[1]), u_)
                v_ = (-u_[1], u_[0])                  # across the wall
                c_ = mr_.centroid
                pieces, lo_, hi_, runs_ = [], 0.0, 0.0, []
                for (a_, b_), d_ in zip(faces_, dirs_):
                    ext = Polygon([a_, b_, (b_[0] + d_[0] * WINDOW_BRIDGE_MM, b_[1] + d_[1] * WINDOW_BRIDGE_MM),
                                   (a_[0] + d_[0] * WINDOW_BRIDGE_MM, a_[1] + d_[1] * WINDOW_BRIDGE_MM)]).buffer(0)
                    if ext.is_empty:
                        continue
                    # only where the wall goes on beyond the window (its band,
                    # thin or not, is there): never out past a corner, and
                    # never across a wall meeting it at right angles - the
                    # far half of the run must still be wall (H10 T3: a
                    # band run 400 mm into the shaft behind the window's jamb)
                    far_ = Polygon([(a_[0] + d_[0] * WINDOW_BRIDGE_MM / 2.0, a_[1] + d_[1] * WINDOW_BRIDGE_MM / 2.0),
                                    (b_[0] + d_[0] * WINDOW_BRIDGE_MM / 2.0, b_[1] + d_[1] * WINDOW_BRIDGE_MM / 2.0),
                                    (b_[0] + d_[0] * WINDOW_BRIDGE_MM, b_[1] + d_[1] * WINDOW_BRIDGE_MM),
                                    (a_[0] + d_[0] * WINDOW_BRIDGE_MM, a_[1] + d_[1] * WINDOW_BRIDGE_MM)]).buffer(0)
                    if mass.intersection(ext).area < 0.25 * ext.area or far_.is_empty \
                            or mass.intersection(far_).area < 0.5 * far_.area:
                        continue
                    pieces.append(ext)
                    # how thick the wall is there: slices across the wall
                    # beside the window's end; the run of mass through the
                    # window's centre line, the median of three slices (a
                    # wall meeting it there at right angles is not its thickness)
                    m_ = ((a_[0] + b_[0]) / 2.0, (a_[1] + b_[1]) / 2.0)
                    for k_ in (0.25, 0.5, 0.75):
                        q_ = (m_[0] + d_[0] * WINDOW_BRIDGE_MM * k_, m_[1] + d_[1] * WINDOW_BRIDGE_MM * k_)
                        sl_ = LineString([(q_[0] - v_[0] * WINDOW_BRIDGE_MAX_MM, q_[1] - v_[1] * WINDOW_BRIDGE_MAX_MM),
                                          (q_[0] + v_[0] * WINDOW_BRIDGE_MAX_MM, q_[1] + v_[1] * WINDOW_BRIDGE_MAX_MM)])
                        cut_ = mass.intersection(sl_)
                        for ln_ in (cut_.geoms if hasattr(cut_, "geoms") else [cut_]):
                            if ln_.geom_type != "LineString" or ln_.is_empty:
                                continue
                            o_ = [(x_ - c_.x) * v_[0] + (y_ - c_.y) * v_[1] for x_, y_ in ln_.coords]
                            if min(o_) <= 0.0 <= max(o_):
                                runs_.append((min(o_), max(o_)))
                                break
                if not pieces:
                    continue
                # the band at the wall's thickness: the shortest run found
                # (a wall meeting it at a corner gives a long run, never its
                # thickness); none short enough - the window's own
                half_ = min(l0, l1) / 2.0
                runs_ = [r_ for r_ in runs_ if (r_[1] - r_[0]) <= WINDOW_BRIDGE_MAX_MM]
                if runs_:
                    r_ = min(runs_, key=lambda r_: r_[1] - r_[0])
                    lo_, hi_ = r_
                lo_, hi_ = min(lo_, -half_), max(hi_, half_)
                band_ = unary_union([mr_] + pieces).buffer(0)
                bc_ = list(band_.minimum_rotated_rectangle.exterior.coords)[:4] if band_.geom_type == "Polygon" else None
                if not bc_:
                    continue
                along_ = [(x_ - c_.x) * u_[0] + (y_ - c_.y) * u_[1] for x_, y_ in bc_]
                a0_, a1_ = min(along_), max(along_)
                fill = Polygon([(c_.x + u_[0] * a0_ + v_[0] * lo_, c_.y + u_[1] * a0_ + v_[1] * lo_),
                                (c_.x + u_[0] * a1_ + v_[0] * lo_, c_.y + u_[1] * a1_ + v_[1] * lo_),
                                (c_.x + u_[0] * a1_ + v_[0] * hi_, c_.y + u_[1] * a1_ + v_[1] * hi_),
                                (c_.x + u_[0] * a0_ + v_[0] * hi_, c_.y + u_[1] * a0_ + v_[1] * hi_)]).buffer(0)
                add = fill.difference(mass)
                if not add.is_empty and add.area <= WINDOW_BRIDGE_MAX_SQM * 1e6 and add.area <= 0.6 * fill.area:
                    fills.append(fill)
            except Exception:                         # noqa: BLE001
                continue
        if fills:
            if os.environ.get("BRIDGE_DEBUG"):
                for f_ in fills:
                    a_ = f_.difference(mass)
                    print("bridge", [round(v) for v in f_.bounds], "new", round(a_.area / 1e6, 3), [round(v) for v in a_.bounds])
            mass = unary_union([mass] + fills).intersection(reach)
            if counts is not None:
                counts["window_bridged"] = counts.get("window_bridged", 0) + len(fills)
    cuts = [g for g in (poly(p) for p in list(columns or ()) + list(windows or ()) + list(doors or ())) if g is not None]
    if cuts:
        mass = mass.difference(unary_union(cuts))
    if locked_u is not None and mass.intersects(locked_u):
        # cut back round what is decided already, before the runs are
        # formed: a reclaimed piece never comes back as wall
        cut_ = mass.intersection(locked_u).area
        mass = mass.difference(locked_u)
        if counts is not None and cut_ > 0:
            counts["locked_cut_sqm"] = round(counts.get("locked_cut_sqm", 0.0) + cut_ / 1e6, 3)
    mass = mass.buffer(0)
    if mass.is_empty:
        return empty
    struct_u = unary_union(struct) if struct else None
    core_u = unary_union(core_shapes) if core_shapes else None

    # (d) runs: strips along one line, of one thickness, joined; the longest
    # first, each taking what of the mass is still free
    groups = collections.defaultdict(list)
    for it in kept:
        nx, ny = -it["uy"], it["ux"]
        c = it["g"].centroid
        a = int(round(it["ang"])) % 180
        if a >= 90:                                   # one normal per direction
            nx, ny = -nx, -ny
        off = int(round((c.x * nx + c.y * ny) / 20.0))
        groups[(a, off, int(round(it["t"] / 20.0)))].append(it)
    runs = []
    for key, lst in groups.items():
        u = unary_union([it["g"] for it in lst])
        for comp in getattr(u, "geoms", [u]):
            if comp.geom_type != "Polygon" or comp.is_empty:
                continue
            runs.append((_wall_length_mm(comp), key[2] * 20.0, comp))
    runs.sort(key=lambda r: -r[0])
    pieces, taken = [], None
    for _L, t, comp in runs:
        region = comp.intersection(mass)
        if taken is not None and not region.is_empty:
            region = region.difference(taken)
        for g in getattr(region, "geoms", [region]):
            if g.geom_type == "Polygon" and g.area > 1.0:
                pieces.append((g, t))
        taken = comp if taken is None else taken.union(comp)
    rest = mass.difference(taken) if taken is not None else mass
    for g in getattr(rest, "geoms", [rest]):
        if g.geom_type == "Polygon" and g.area > 1.0:
            pieces.append((g, 2.0 * g.area / g.length if g.length else 0.0))

    def share(g, other):
        return 0.0 if other is None or not g.intersects(other) else g.intersection(other).area / g.area

    # the group a run joins pieces in (not a layer: every wall is AI_WALL)
    classed = []
    for g, t in pieces:
        if share(g, edge_band) >= 0.5:
            grp = "edge"
        elif t > WALL_CORE_MIN_MM or share(g, struct_u) >= 0.5 or share(g, core_u) >= 0.5:
            grp = "structure"
        else:
            grp = "other"
        classed.append((g, grp))

    # (e) slivers: dropped unless they touch another piece
    if classed:
        gtree = STRtree([g for g, _c in classed])
        keep = []
        for k, (g, cls) in enumerate(classed):
            if g.area / 1e6 >= WALL_MIN_PIECE_SQM or any(
                    int(j) != k and classed[int(j)][0].distance(g) <= 1.0 for j in gtree.query(g.buffer(1.0))):
                keep.append((g, cls))
        classed = keep

    pieces_all, n, length_m, area_sqm = [], 0, 0.0, 0.0
    for grp in ("edge", "structure", "other"):
        mine = [g for g, c in classed if c == grp]
        # runs of a group that touch (within half a millimetre) are one piece
        u = unary_union([g.buffer(0.5, join_style=2) for g in mine]).buffer(-0.5, join_style=2) if mine else Polygon()
        if not u.is_empty:
            u = u.simplify(WALL_SIMPLIFY_MM, preserve_topology=True)
        lst = [g for g in getattr(u, "geoms", [u]) if g.geom_type == "Polygon" and not g.is_empty]
        pieces_all.extend(lst)
        n += len(lst)
        length_m += round(sum(_wall_length_mm(g) for g in lst) / 1000.0, 1)
        area_sqm += round(sum(g.area for g in lst) / 1e6, 2)
    return ({LN.WALL: pieces_all},
            {LN.WALL: {"count": n, "length_m": round(length_m, 1), "area_sqm": round(area_sqm, 2)}})


def wall_report(pieces):
    """{"count", "length_m", "area_sqm"} of wall pieces."""
    return {"count": len(pieces),
            "length_m": round(sum(_wall_length_mm(g) for g in pieces) / 1000.0, 1),
            "area_sqm": round(sum(g.area for g in pieces) / 1e6, 2)}


def merge_wall_pieces(pieces):
    """The wall pieces as drawn: pieces that touch are one outline, on
    AI_WALL (wall_polygons keeps a balcony's parapet a piece of its own; the
    drawing does not need that)."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    if not pieces:
        return []
    u = unary_union([g.buffer(0.5, join_style=2) for g in pieces]).buffer(-0.5, join_style=2)
    if u.is_empty:
        return []
    u = u.simplify(WALL_SIMPLIFY_MM, preserve_topology=True)
    return [g for g in getattr(u, "geoms", [u]) if g.geom_type == "Polygon" and not g.is_empty]


def _balcony_to_grow(merged, fam_of):
    """Whether _grow_balconies has a room to look at: a traced balcony or
    refuge area."""
    for d in merged:
        if d.get("source") not in ("P", "S"):
            continue
        fam = d.get("family_forced") or fam_of(d["label"], _FAMILY_RULES)["name"]
        if fam == "Balcony / Open" or re.search(r"\bREFUGE\b", (d.get("label") or "").upper()):
            return True
    return False


def _grow_balconies(merged, pieces, columns, floor_poly, shape_of, set_shape, fam_of,
                    touch_mm=40.0, edge_mm=600.0):
    """Join to every balcony / refuge boundary the wall and column pieces
    along its outer side: pieces touching it, touching no other room, and
    within edge_mm of the plate's edge. Returns how many grew."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    try:
        plate = Polygon(floor_poly).buffer(0)
    except (ValueError, TypeError):
        return 0
    cand = []
    for cls_pieces in (pieces or {}).values():
        cand.extend(g for g in cls_pieces if g is not None and not g.is_empty)
    for c in columns or ():
        try:
            g = Polygon(c).buffer(0) if not hasattr(c, "geom_type") else c
            if g is not None and not g.is_empty:
                cand.append(g)
        except (ValueError, TypeError):
            continue
    if not cand:
        return 0
    shapes = {}
    for d in merged:
        if d.get("source") not in ("P", "S", "G"):
            continue
        g = shape_of(d)
        if g is not None and not g.is_empty:
            shapes[id(d)] = g
    grown = 0
    from shapely.geometry import LineString as _GL
    for d in merged:
        if d.get("source") not in ("P", "S"):
            continue
        fam = d.get("family_forced") or fam_of(d["label"], _FAMILY_RULES)["name"]
        if fam != "Balcony / Open" and not re.search(r"\bREFUGE\b", (d.get("label") or "").upper()):
            continue
        g = shapes.get(id(d))
        if g is None:
            continue
        others = [o for k, o in shapes.items() if k != id(d)]
        # its open side: the longest side of it along the plate's edge. A
        # parapet runs along that side; the walls at its ends run in from
        # it - they are walls, not the parapet (P285: the balconies' end
        # walls, where the plate's outline steps round them).
        open_side = None
        mr = g.minimum_rotated_rectangle
        if mr.geom_type == "Polygon":
            mc = list(mr.exterior.coords)
            for k_ in range(4):
                ln = _GL([mc[k_], mc[k_ + 1]])
                if ln.length <= 0:
                    continue
                share = ln.intersection(plate.exterior.buffer(edge_mm)).length / ln.length
                if share >= 0.6 and (open_side is None or ln.length > open_side.length):
                    open_side = ln
        keep = []
        for w in cand:
            if w.distance(g) > touch_mm or w.area > 0.5 * g.area:
                continue
            if plate.exterior.distance(w) > edge_mm:
                continue
            # on the outward side: the parapet, not an end wall running in
            # from the edge, whose far end stands as deep as the room
            if plate.exterior.distance(w.centroid) > 0.6 * plate.exterior.distance(g.centroid):
                continue
            if any(o.distance(w) <= touch_mm for o in others):
                continue
            if open_side is not None:
                if w.distance(open_side) > edge_mm:
                    continue                          # along another side: an end wall
                wr = w.minimum_rotated_rectangle
                if wr.geom_type == "Polygon":
                    wc = list(wr.exterior.coords)
                    e0 = (wc[1][0] - wc[0][0], wc[1][1] - wc[0][1])
                    e1 = (wc[2][0] - wc[1][0], wc[2][1] - wc[1][1])
                    l0, l1 = math.hypot(*e0), math.hypot(*e1)
                    e, le = (e0, l0) if l0 >= l1 else (e1, l1)
                    oc = list(open_side.coords)
                    ox_, oy_ = oc[1][0] - oc[0][0], oc[1][1] - oc[0][1]
                    lo = math.hypot(ox_, oy_)
                    if le > 1.5 * min(l0, l1) and le > 0 and lo > 0 and \
                            abs(e[0] / le * oy_ / lo - e[1] / le * ox_ / lo) > 0.26:
                        continue                      # across the open side: an end wall
            keep.append(w)
        if not keep:
            continue
        # the pieces stand up to touch_mm off the room: close that gap
        new = (unary_union([g] + keep).buffer(touch_mm, join_style=2)
               .buffer(-touch_mm, join_style=2))
        if new.geom_type == "MultiPolygon":
            new = max(new.geoms, key=lambda q: q.area)
        if new.geom_type != "Polygon" or new.area <= g.area * 1.001:
            continue
        if set_shape(d, new, (d["cx"], d["cy"])):
            d["_balcony_grown_sqm"] = (new.area - g.area) / 1e6
            d.setdefault("evidence", []).append("grown to the outer face of its parapet (+{0:.2f} sq.m)".format((new.area - g.area) / 1e6))
            grown += 1
    return grown


def _absorb_columns(merged, columns, shape_of, set_shape, touch_mm=40.0, min_share=0.45):
    """Join to a traced room every column it wraps round: the column's
    outline lies along the room for at least min_share of its length
    (two sides of a corner column, three of one standing out of a wall),
    and no other room touches it. A take-off polyline runs round the
    walls and over the column; the trace stepped round it. Returns how
    many columns joined a room."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    cols = []
    for c in columns or ():
        try:
            g = Polygon(c).buffer(0) if not hasattr(c, "geom_type") else c
        except (ValueError, TypeError):
            continue
        if g is not None and not g.is_empty and g.geom_type == "Polygon" and g.area <= 4e6:
            cols.append(g)
    if not cols:
        return 0
    shapes = []
    for d in merged:
        g = shape_of(d)
        if g is not None and not g.is_empty:
            shapes.append((d, g))
    if not shapes:
        return 0
    from shapely.strtree import STRtree
    tree = STRtree([g for _d, g in shapes])
    joins = {}
    for c in cols:
        ring = c.exterior
        hits = []
        for i in tree.query(c.buffer(touch_mm)):
            d, g = shapes[int(i)]
            if g.distance(c) > touch_mm:
                continue
            share = ring.intersection(g.buffer(touch_mm, join_style=2)).length / max(ring.length, 1.0)
            hits.append((share, d, g))
        if not hits:
            continue
        hits.sort(key=lambda h: -h[0])
        share, d, g = hits[0]
        if d.get("source") not in ("P", "S") or share < min_share:
            continue
        if any(h[0] > 0.05 for h in hits[1:]):
            continue                                  # between two rooms: neither's
        if g.intersection(c).area > 0.5 * c.area:
            continue                                  # already inside it
        joins.setdefault(id(d), (d, []))[1].append(c)
    n = 0
    for d, cs in joins.values():
        g = shape_of(d)
        if g is None:
            continue
        new = (unary_union([g] + cs).buffer(touch_mm, join_style=2)
               .buffer(-touch_mm, join_style=2))
        if new.geom_type == "MultiPolygon":
            new = max(new.geoms, key=lambda q: q.area)
        if new.geom_type != "Polygon" or new.area <= g.area * 1.0005:
            continue
        # the room keeps its own holes (a shaft inside it)
        if set_shape(d, new, (d["cx"], d["cy"])):
            d.setdefault("evidence", []).append(
                "{0} column{1} it wraps round measured with it (+{2:.2f} sq.m)".format(
                    len(cs), "" if len(cs) == 1 else "s", (new.area - g.area) / 1e6))
            n += len(cs)
    return n


def wall_rings(pieces):
    """A wall polygon as point lists to draw: its outline, then its holes."""
    out = []
    for p in pieces:
        out.append([[list(map(float, c)) for c in list(p.exterior.coords)[:-1]]] +
                   [[list(map(float, c)) for c in list(r.coords)[:-1]] for r in p.interiors])
    return out


WALL_ACI = {LN.WALL: int(_WO.get("color", 30))}


def draw_cutout(doc, msp, units, pts, name=None, x_tag=None):
    """A cut-out on AI_CUTOUT: its outline and its X (pts in mm); its name
    inside when it says more than CUTOUT. x_tag: (class, step, why) of the X
    sequence, stored on the outline. Returns the outline."""
    if len(pts) < 3:
        return None
    if CUTOUT_LAYER not in doc.layers:
        doc.layers.add(CUTOUT_LAYER, color=CUTOUT_COLOR)
    f = units.from_mm
    ring = [(f(x), f(y)) for x, y in pts]
    at = {"layer": CUTOUT_LAYER, "color": 256}
    outline = msp.add_lwpolyline(ring, close=True, dxfattribs=at)
    if x_tag:
        tag_x(doc, outline, *x_tag)
    if len(ring) == 4:
        msp.add_line(ring[0], ring[2], dxfattribs=at)
        msp.add_line(ring[1], ring[3], dxfattribs=at)
    if name and name != CUTOUT_NAME:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ht = max(50.0, min(300.0, 0.3 * min(max(xs) - min(xs), max(ys) - min(ys))))
        msp.add_mtext(name, dxfattribs={"layer": CUTOUT_LAYER, "char_height": f(ht), "attachment_point": 5,
                                        "insert": (f((min(xs) + max(xs)) / 2), f((min(ys) + max(ys)) / 2))})
    return outline


# The X sequence's answer for an X mark (xplace, 2026-10-16), stored on what
# is drawn for it: its class, the step that claimed it and why. Every later
# step reads it - the element engine, the geometry comparison, the final
# drawing - and none decides the X again.
X_APPID = "DRAWING_ANALYZER_X"


def tag_x(doc, entity, cls, step=None, why=""):
    try:
        if X_APPID not in doc.appids:
            doc.appids.new(X_APPID)
        entity.set_xdata(X_APPID, [(1000, str(cls)), (1070, int(step or 0)), (1000, str(why or "")[:250])])
    except (AttributeError, TypeError, ValueError):
        pass


def x_tag(entity):
    """(class, step, why) the X sequence stored on an entity, or None."""
    try:
        if not entity.has_xdata(X_APPID):
            return None
        vals = [v for _c, v in entity.get_xdata(X_APPID)]
    except Exception:                                 # noqa: BLE001
        return None
    if not vals:
        return None
    return (str(vals[0]), int(vals[1]) if len(vals) > 1 else 0, str(vals[2]) if len(vals) > 2 else "")


def draw_column(doc, msp, units, pts, x_tag=None):
    """A column on AI_COLUMN: its outline, a hatch and the label "C"
    (config.json wall_output column_*). pts in mm. x_tag: (class, step, why)
    when the X sequence found it (a column's cross)."""
    if len(pts) < 3:
        return None
    if LN.COLUMN not in doc.layers:
        doc.layers.add(LN.COLUMN, color=COLUMN_ACI)
    f = units.from_mm
    ring = [(f(x), f(y)) for x, y in pts]
    outline = msp.add_lwpolyline(ring, close=True, dxfattribs={"layer": LN.COLUMN, "color": 256})
    if x_tag:
        tag_x(doc, outline, *x_tag)
    try:
        h = msp.add_hatch(color=256, dxfattribs={"layer": LN.COLUMN})
        if COLUMN_HATCH == "SOLID":
            h.set_solid_fill(color=256)
        else:
            h.set_pattern_fill(COLUMN_HATCH, color=256, scale=f(COLUMN_HATCH_SCALE))
        h.paths.add_polyline_path(ring, is_closed=True)
    except Exception:                                 # noqa: BLE001
        pass
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    side = min(max(xs) - min(xs), max(ys) - min(ys))
    msp.add_mtext(COLUMN_LABEL, dxfattribs={
        "layer": LN.COLUMN, "char_height": f(max(80.0, min(400.0, 0.4 * side))), "attachment_point": 5,
        "insert": (f((min(xs) + max(xs)) / 2.0), f((min(ys) + max(ys)) / 2.0))})
    return outline


def draw_door_review(doc, msp, units, items):
    """The doubtful door openings (rule doors_need_evidence) on AI_REVIEW:
    the opening's outline and a note why. items: [{"pts", "why"}], mm.
    Returns how many."""
    if not items:
        return 0
    if LN.REVIEW not in doc.layers:
        doc.layers.add(LN.REVIEW, color=REVIEW_DOOR_ACI)
    f = units.from_mm
    n = 0
    for it in items:
        pts = it.get("pts") or []
        if len(pts) < 3:
            continue
        msp.add_lwpolyline([(f(x), f(y)) for x, y in pts], close=True,
                           dxfattribs={"layer": LN.REVIEW, "color": 256})
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        msp.add_mtext(str(it.get("why") or "door?"), dxfattribs={
            "layer": LN.REVIEW, "char_height": f(80.0), "attachment_point": 7,
            "insert": (f(min(xs)), f(max(ys) + 40.0))})
        n += 1
    return n


REVIEW_DOOR_ACI = 6

# Rule boundary_on_evidence: a stretch of a room's side standing on nothing
# no longer than this, whose two ends stand on evidence, is the line across
# a doorless opening - an opening line, counted as a door (the door
# definition), not marked.
EVIDENCE_OPENING_MAX_MM = float(_BD.get("evidence_opening_max_mm", 1000.0))


def boundary_evidence_check(entities, units, plate, tol=None, min_run=None, opening_max=None,
                            skip=(), extra_lines=()):
    """What every room's side stands on, read from what was drawn for one
    floor (rule boundary_on_evidence, 2026-10-17). A room boundary may lie
    only on a wall (AI_WALL), the facade (the plate's edge, the glass and
    windows on AI_GLASS / AI_WINDOW), another room (any room layer, AI_VOID,
    AI_CUTOUT), a primary door (AI_P_DOORS: its line, and the opening it
    closes across the wall's thickness - a room stopping at its own face
    of the opening, the door drawn on the other room's face) or a column
    (AI_COLUMN) - within `tol` (EVIDENCE_TOL_MM). A secondary door
    (AI_S_DOORS) and any other line are none.

    entities: the floor's entities as drawn; plate: its outline, mm;
    skip: handles of outlines not checked - the X sequence's shafts, lifts
    and voids, identified by their X (they are evidence to the others);
    extra_lines: more evidence, (x1, y1, x2, y2) mm - the lines closing a
    lift lobby at its bank (rule lift_lobby_to_lift_bank).
    Returns (summary, items): summary {"checked_m", "evidence_m",
    "opening_m", "no_evidence_m", "rooms", "rooms_off"}; items: one per
    stretch of `min_run` (EVIDENCE_MIN_RUN_MM) or more standing on nothing
    and not an opening line - {"pts", "why", "layer", "len_mm"}, mm, as
    draw_door_review takes them."""
    from shapely.geometry import Polygon, LineString, Point
    from shapely.ops import unary_union
    from shapely.prepared import prep
    from shapely.strtree import STRtree
    tol = EVIDENCE_TOL_MM if tol is None else float(tol)
    min_run = EVIDENCE_MIN_RUN_MM if min_run is None else float(min_run)
    opening_max = EVIDENCE_OPENING_MAX_MM if opening_max is None else float(opening_max)
    walls, cols, lines, rooms, openings = [], [], [], [], []
    skip = set(skip or ())
    for s_ in extra_lines or ():
        try:
            lines.append(LineString([(float(s_[0]), float(s_[1])), (float(s_[2]), float(s_[3]))]))
        except (ValueError, TypeError, IndexError):
            continue
    for e in entities or ():
        try:
            if e.dxftype() not in ("LWPOLYLINE", "POLYLINE", "LINE"):
                continue
            layer = (e.dxf.layer or "").strip().upper()
            if e.dxftype() == "LINE":
                pts = [units.point_to_mm(e.dxf.start.x, e.dxf.start.y), units.point_to_mm(e.dxf.end.x, e.dxf.end.y)]
                closed = False
            else:
                pts = [tuple(p[:2]) for p in _polyline_points(e, units)]
                closed = _is_closed(e)
        except Exception:                             # noqa: BLE001
            continue
        if len(pts) < 2:
            continue
        if layer == LN.WALL and closed and len(pts) >= 3:
            g = _safe_poly(pts)
            if g is not None:
                walls.append(g)
        elif layer == LN.COLUMN and closed and len(pts) >= 3:
            g = _safe_poly(pts)
            if g is not None:
                cols.append(g)
        elif layer in (LN.GLASS, LN.WINDOW, LN.DOOR_PRIMARY, LN.OLD_DOOR_PRIMARY, LN.CUTOUT, LN.VOID):
            lines.append(LineString(pts + ([pts[0]] if closed else [])))
            if layer in (LN.WINDOW, LN.CUTOUT) and closed and len(pts) >= 3:
                g = _safe_poly(pts)                   # set in the wall: as the wall
                if g is not None:
                    walls.append(g)
            if layer in (LN.DOOR_PRIMARY, LN.OLD_DOOR_PRIMARY):
                try:
                    q = door_quad(e, units)
                except Exception:                     # noqa: BLE001
                    q = None
                g = _safe_poly([tuple(p_[:2]) for p_ in q]) if q and len(q) >= 3 else None
                if g is not None:
                    openings.append(g)
        elif layer != LN.FLOOR and LN.is_room_layer(layer) and closed and len(pts) >= 3:
            g = _safe_poly(pts)
            if g is not None:
                rooms.append((layer, g, e.dxf.handle not in skip))
    try:
        lines.append(Polygon([tuple(p[:2]) for p in plate]).buffer(0).exterior)   # the facade: the plate's edge
    except (ValueError, TypeError, IndexError, AttributeError):
        pass
    # (a door's opening is part of the wall it is set in: its depth stored
    # is often the frame's only, the wall's other face a finish further on)
    parts = ([g.buffer(tol + EVIDENCE_WALL_FINISH_MM) for g in walls + openings]
             + [g.buffer(tol + EVIDENCE_COLUMN_CASING_MM) for g in cols]
             + [ln.buffer(tol) for ln in lines if ln.length > 0])
    ev_ = prep(unary_union(parts)) if parts else None
    rings = [g.exterior.buffer(tol) for _l, g, _c in rooms]
    tree = STRtree(rings) if rings else None
    summ = {"checked_m": 0.0, "evidence_m": 0.0, "opening_m": 0.0, "no_evidence_m": 0.0,
            "rooms": sum(1 for r_ in rooms if r_[2]), "rooms_off": 0}
    items = []
    STEP = 50.0

    def on_evidence(P, k, reach):
        if ev_ is not None and (ev_.contains(P) if reach <= 0 else ev_.intersects(P.buffer(reach))):
            return True
        if tree is None:
            return False
        Q = P if reach <= 0 else P.buffer(reach)
        return any(int(q) != k and rings[int(q)].intersects(Q) for q in tree.query(Q))
    for k, (layer, g, check) in enumerate(rooms):
        if not check:
            continue
        ring = list(g.exterior.coords)
        off_here = False
        for i in range(len(ring) - 1):
            (ax, ay), (bx, by) = ring[i][:2], ring[i + 1][:2]
            L = math.hypot(bx - ax, by - ay)
            if L < 1e-6:
                continue
            n = max(1, int(L // STEP))
            seg = L / n
            run = None
            runs = []
            for j in range(n):
                t = (j + 0.5) / n
                P = Point(ax + (bx - ax) * t, ay + (by - ay) * t)
                if on_evidence(P, k, 0.0):
                    summ["evidence_m"] += seg / 1000.0
                    run = None
                else:
                    if run is None:
                        run = [t, t]
                        runs.append(run)
                    run[1] = t
            for r0, r1 in runs:
                t0, t1 = max(0.0, r0 - 0.5 / n), min(1.0, r1 + 0.5 / n)
                ln_mm = (t1 - t0) * L
                a = (ax + (bx - ax) * t0, ay + (by - ay) * t0)
                b = (ax + (bx - ax) * t1, ay + (by - ay) * t1)
                # an opening line: short, both ends on evidence - a jamb, a
                # wall's end (the door definition counts it as a door)
                if (ln_mm <= opening_max and on_evidence(Point(a), k, STEP + tol)
                        and on_evidence(Point(b), k, STEP + tol)):
                    summ["opening_m"] += ln_mm / 1000.0
                    continue
                summ["no_evidence_m"] += ln_mm / 1000.0
                if ln_mm < min_run:
                    continue
                box = LineString([a, b]).buffer(60.0, cap_style=2)
                if box.is_empty or box.geom_type != "Polygon":
                    continue
                items.append({"pts": [(float(x), float(y)) for x, y in list(box.exterior.coords)[:-1]],
                              "why": "boundary on no evidence ({0}, {1:.0f} mm)".format(layer, ln_mm),
                              "layer": layer, "len_mm": round(ln_mm)})
                off_here = True
        if off_here:
            summ["rooms_off"] += 1
    summ["checked_m"] = summ["evidence_m"] + summ["opening_m"] + summ["no_evidence_m"]
    for key in ("checked_m", "evidence_m", "opening_m", "no_evidence_m"):
        summ[key] = round(summ[key], 2)
    return summ, items


def veto_walls(walls_by_class, veto):
    """The walls less the room-size veto areas (a strip a room grew over to
    reach its stated size - inside the room, not a wall). walls_by_class:
    {class layer: [rings]}; veto: [points]. Returns the same shape and the
    number of wall pieces cut."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    vs = []
    for v in veto or ():
        try:
            g = Polygon(v).buffer(0)
            if not g.is_empty:
                vs.append(g)
        except (ValueError, TypeError):
            pass
    if not vs:
        return walls_by_class, 0
    cutter = unary_union(vs).buffer(1.0, join_style=2)
    out, cut = {}, 0
    for cls, items in (walls_by_class or {}).items():
        keep = []
        for rings in items:
            try:
                g = Polygon(rings[0], [r for r in rings[1:] if len(r) >= 3]).buffer(0)
            except (ValueError, TypeError, IndexError):
                keep.append(rings)
                continue
            if g.is_empty or not g.intersects(cutter) or g.intersection(cutter).area < 0.01e6:
                keep.append(rings)
                continue
            cut += 1
            rest = g.difference(cutter)
            keep.extend(wall_rings([p for p in getattr(rest, "geoms", [rest])
                                    if p.geom_type == "Polygon" and p.area >= 0.01e6]))
        out[cls] = keep
    return out, cut


def draw_walls(doc, msp, units, walls_by_class):
    """Walls on their layer (AI_WALL): each ring a closed polyline.
    walls_by_class: {layer: [rings, as wall_rings gives]}. A result saved
    before walls were one layer (AI_WALL_EXTERNAL / _CORE / _INTERNAL) is
    drawn on AI_WALL too."""
    for cls, items in (walls_by_class or {}).items():
        if not items:
            continue
        if cls in LN.OLD_WALLS:
            cls = LN.WALL
        if cls not in doc.layers:
            doc.layers.add(cls, color=WALL_ACI.get(cls, 8))
        for rings in items:
            for ring in rings:
                if len(ring) >= 3:
                    msp.add_lwpolyline([(units.from_mm(x), units.from_mm(y)) for x, y in ring], close=True,
                                       dxfattribs={"layer": cls, "color": 256})


def _side(segs, axes, i, j):
    """Signed distance of segment j's middle from segment i's line."""
    s, a = segs[i], axes[i]
    mx, my = (segs[j][0] + segs[j][2]) / 2.0, (segs[j][1] + segs[j][3]) / 2.0
    return (mx - s[0]) * -a[2] + (my - s[1]) * a[1]


def _ledges(segs, axes, wall, always, partners, fixture_segs, idx):
    """Wall candidates that are ledges in front of a wall (see _detect_walls)."""
    if LEDGE_MAX_DEPTH_MM <= 0:
        return set()

    def confirmed(j, i):
        # a face of a wall in its own right: a structure layer, or paired
        # with a face on its far side from i
        if always[j]:
            return True
        si = _side(segs, axes, j, i)
        return any(k != i and _side(segs, axes, j, k) * si < 0 for k in partners.get(j, ()))

    found = []
    for i in sorted(wall):
        if always[i]:
            continue                                 # a structure line is never a ledge
        ps = partners.get(i) or []
        if not ps:
            continue
        sides = [_side(segs, axes, i, j) for j in ps]
        if not (all(d > 0 for d in sides) or all(d < 0 for d in sides)):
            continue                                 # partners both sides: a wall's middle
        # the nearest partner is the wall's face it stands in front of;
        # partners further on (its far face) are the same wall
        near = min(range(len(ps)), key=lambda k: abs(sides[k]))
        if abs(sides[near]) > LEDGE_MAX_DEPTH_MM:
            continue
        if confirmed(ps[near], i):
            found.append((i, sides[near]))
    if not found or not LEDGE_NEEDS_EVIDENCE:
        return set(i for i, _w in found)

    # Evidence: sanitary fixtures touching it, or cubicle partitions
    # stopping at it - lines across it, not walls, a cubicle deep, standing
    # on its room side (away from the wall it is in front of). The outer
    # face of the same wall has neither; spacing alone cannot tell them
    # apart.
    import shapely
    from shapely.strtree import STRtree
    fx = [shapely.LineString([(s[0], s[1]), (s[2], s[3])]) for s in fixture_segs]
    fx_tree = STRtree(fx) if fx else None
    others = [k for k in idx if k not in wall
              and LEDGE_PARTITION_MM[0] <= axes[k][3] <= LEDGE_PARTITION_MM[1]]
    ot = [shapely.LineString([(segs[k][0], segs[k][1]), (segs[k][2], segs[k][3])]) for k in others]
    ot_tree = STRtree(ot) if ot else None
    out = set()
    for i, wall_side in found:
        s = segs[i]
        ln = shapely.LineString([(s[0], s[1]), (s[2], s[3])])
        zone = ln.buffer(LEDGE_TOUCH_MM)
        if fx_tree is not None and any(fx[k].intersects(zone) for k in fx_tree.query(zone)):
            out.add(i)
            continue
        stops = 0
        if ot_tree is not None:
            for k in ot_tree.query(zone):
                j = others[k]
                if abs(axes[i][1] * axes[j][1] + axes[i][2] * axes[j][2]) > 0.2:
                    continue                         # not across it
                if _side(segs, axes, i, j) * wall_side >= 0:
                    continue                         # on the wall's side, not the room's
                p = segs[j]
                if (zone.contains(shapely.Point(p[0], p[1])) or
                        zone.contains(shapely.Point(p[2], p[3]))):
                    stops += 1
        if stops >= LEDGE_PARTITION_MIN:
            out.add(i)
    return out


def _overrun_caps(segs, axes, idx, wall, panel, faces, caps, excluded,
                  reach=25.0, max_len=1200.0):
    """Lines crossing the ends of two wall faces a wall's thickness apart."""
    import numpy as np
    try:
        from scipy.spatial import cKDTree
    except ImportError:
        return []
    ends = np.array([(f[k], f[k + 1]) for f in faces for k in (0, 2)], dtype=np.float64)
    if not len(ends):
        return []
    tree = cKDTree(ends)
    out = []
    for i in idx:
        if i in wall or i in panel or excluded[i]:
            continue
        s = segs[i]
        if tuple(s) in caps:
            continue
        L = axes[i][3]
        if not (WALL_T_MIN <= L <= max_len):
            continue
        mx, my = (s[0] + s[2]) / 2.0, (s[1] + s[3]) / 2.0
        near = tree.query_ball_point((mx, my), L / 2.0 + reach)
        if len(near) < 2:
            continue
        ax, ay, dx, dy = s[0], s[1], s[2] - s[0], s[3] - s[1]
        ll = dx * dx + dy * dy
        on = []
        for k in near:
            px, py = ends[k]
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll))
            if math.hypot(ax + t * dx - px, ay + t * dy - py) <= reach:
                on.append((t * math.sqrt(ll), px, py))
        if len(on) < 2:
            continue
        on.sort()
        gaps = [b[0] - a[0] for a, b in zip(on, on[1:])]
        if any(WALL_T_MIN <= g <= WALL_T_MAX + 50.0 for g in gaps):
            out.append(s)
    return out


# -- The hard guardrail: a boundary never crosses a wall -------------------
# Hatches that are walls or structure: on a wall / structure layer, or in a
# concrete, brick or blockwork pattern. Floor finishes, landscape and the
# area-plan overlay are hatched too, over whole rooms, and are not walls.
# (config.json walls: wall_hatch_layer, wall_hatch_pattern, wall_hatch_not_layer)
_WALL_HATCH_LAYER = re.compile(str(_WD.get(
    "wall_hatch_layer",
    r"WALL|\bWL|RCC|CONC|COL|STR|SHEAR|BLOCK|BRICK|MASON|PARTITION|CORE|FACADE|GLAZ|GLASS")))
_WALL_HATCH_PATTERN = re.compile(str(_WD.get(
    "wall_hatch_pattern",
    r"^(AR-CONC|ANSI31|ANSI32|ANSI33|ANSI37|AR-B816|AR-B88|AR-BRSTD|AR-BRELM|"
    r"AR-SAND|BRICK|BRSTONE|CONCRETE|EARTH|STEEL|SOLID)$")))
_NOT_WALL_HATCH_LAYER = re.compile(str(_WD.get(
    "wall_hatch_not_layer",
    r"FLOOR|FIN|TILE|LAND|GRASS|PAV|AREA|FURN|ANNO|TEXT|DIM|^\d+(\.\d+)+\s")))
# A wall pattern alone does not make a wall of a hatch on a sign, symbol,
# tag, level marker or legend (a north arrow, a section bubble).
_PATTERN_ONLY_NOT_WALL = re.compile(r"SIGN|SYMB|TAG|BUBBLE|ELEV|LEVEL|LEGEND|NORTH", re.I)
WALL_HATCH_MAX_SQM = 60.0
# Curved wall faces (_collect_wall_arcs): arcs of these radii (mm).
WALL_ARC_R_MIN = float((CONFIG.get("wall_trace") or {}).get("arc_radius_min_mm", 300.0))
WALL_ARC_R_MAX = float((CONFIG.get("wall_trace") or {}).get("arc_radius_max_mm", 60000.0))


def _hatch_islands(rings):
    """A hatch's loops as the pieces it fills: [(outer, [holes])].

    A hatch is filled between its loops by nesting: a loop inside no other
    is filled, a loop inside one is a hole in it, a loop inside two is
    filled again, and so on. One hatch often holds many separate pieces -
    on BHUB 1st Floor one structure hatch holds nine concrete walls as nine
    loops side by side - and reading its largest loop as the hatch and the
    rest as holes dropped eight of them (50 sq.m of wall on that floor).
    rings: point lists, any order."""
    from shapely.geometry import Polygon
    items = []
    for r in rings:
        try:
            g = Polygon(r).buffer(0)
        except (ValueError, TypeError):
            continue
        if not g.is_empty and g.area > 0:
            items.append((r, g))
    items.sort(key=lambda t: -t[1].area)
    n = len(items)
    parent, depth = [-1] * n, [0] * n
    for i in range(n):
        gi = items[i][1]
        pi = gi.representative_point()
        for j in range(i - 1, -1, -1):           # the smallest larger loop holding it
            gj = items[j][1]
            if gj.area > gi.area and gj.contains(pi) and gj.buffer(1.0).contains(gi):
                parent[i], depth[i] = j, depth[j] + 1
                break
    out = []
    for i in range(n):
        if depth[i] % 2 == 0:
            out.append((items[i][0], [items[k][0] for k in range(n) if parent[k] == i]))
    return out


def _collect_all_hatches(doc, msp, units):
    """Every hatch up to WALL_HATCH_MAX_SQM as polygons in mm:
    [(outer, [holes], layer, pattern, solid)], one entry per piece the
    hatch fills (_hatch_islands). The wall tracer (walltrace) finds which of
    them are wall material by where they lie."""
    from ezdxf import path as ezpath
    out = []
    for e, xf, _d in iter_entities(doc, msp, "HATCH"):
        try:
            lu = (effective_layer(e, xf) or "").upper()
            pat = str(e.dxf.get("pattern_name", "") or "").upper()
            solid = bool(e.dxf.get("solid_fill", 0))
            rings = []
            for pth in ezpath.from_hatch(e):
                pts = [units.point_to_mm(*xf.apply(v.x, v.y)) for v in pth.flattening(5.0)]
                if len(pts) >= 3:
                    rings.append(pts)
            if not rings:
                continue
            rings.sort(key=lambda r: abs(shoelace_area_sqm(r)), reverse=True)
            if abs(shoelace_area_sqm(rings[0])) > WALL_HATCH_MAX_SQM:
                continue
            for outer, holes in (_hatch_islands(rings) if len(rings) > 1 else [(rings[0], [])]):
                out.append((outer, holes, lu, pat, solid))
        except Exception:                             # noqa: BLE001
            continue
    return out


def _collect_wall_arcs(doc, msp, units):
    """Curved wall faces as arcs in mm: [(cx, cy, r, a0, a1, layer)], a0 < a1
    in degrees, from ARCs and the bulged spans of polylines on any layer (the
    wall tracer keeps those on wall layers). A curved wall's two faces are
    concentric arcs; flattened into short chords they pair poorly, and the
    curved corner walls of the XA tower were mostly missed."""
    import numpy as _np
    out = []

    def add(pts_local, centre_local, xf, layer):
        try:
            cw = units.point_to_mm(*xf.apply(*centre_local))
            pw = [units.point_to_mm(*xf.apply(x, y)) for x, y in pts_local]
        except Exception:                             # noqa: BLE001
            return
        r = sum(math.hypot(x - cw[0], y - cw[1]) for x, y in pw) / len(pw)
        if not (WALL_ARC_R_MIN <= r <= WALL_ARC_R_MAX):
            return
        ang = _np.degrees(_np.unwrap(_np.radians([math.degrees(math.atan2(y - cw[1], x - cw[0])) for x, y in pw])))
        a0, a1 = float(ang.min()), float(ang.max())
        if a1 - a0 < 1.0:
            return
        out.append((cw[0], cw[1], r, a0, a1, layer))

    for e, xf, _d in iter_entities(doc, msp, "ARC LWPOLYLINE"):
        try:
            layer = effective_layer(e, xf)
            if e.dxftype() == "ARC":
                c, r = e.dxf.center, float(e.dxf.radius)
                sa, ea = float(e.dxf.start_angle), float(e.dxf.end_angle)
                if ea <= sa:
                    ea += 360.0
                ocs = e.ocs()
                pts = []
                for k in range(9):
                    a = math.radians(sa + (ea - sa) * k / 8.0)
                    p = ocs.to_wcs((c.x + r * math.cos(a), c.y + r * math.sin(a), c.z))
                    pts.append((p.x, p.y))
                cc = ocs.to_wcs(c)
                add(pts, (cc.x, cc.y), xf, layer)
            else:
                pl = list(e.get_points("xyb"))
                n = len(pl)
                last = n if e.closed else n - 1
                for i in range(max(last, 0)):
                    a, b = pl[i], pl[(i + 1) % n]
                    if not a[2]:
                        continue
                    c, sa, ea, r = bulge_to_arc((a[0], a[1]), (b[0], b[1]), a[2])
                    sa, ea = math.degrees(sa), math.degrees(ea)
                    if ea <= sa:
                        ea += 360.0
                    pts = [(c.x + r * math.cos(math.radians(sa + (ea - sa) * k / 8.0)),
                            c.y + r * math.sin(math.radians(sa + (ea - sa) * k / 8.0))) for k in range(9)]
                    add(pts, (c.x, c.y), xf, layer)
        except Exception:                             # noqa: BLE001
            continue
    return out


def _collect_wall_hatches(doc, msp, units, islands=False):
    """Wall and structure hatches as polygons in mm: [(outer, [holes], layer, pattern)].
    islands: every piece a hatch fills (_hatch_islands) - a hatch drawn as
    several separate pieces (P285: the pillars between the ELE SHAFT doors)
    - rather than its largest piece only."""
    from ezdxf import path as ezpath
    out = []
    for e, xf, _d in iter_entities(doc, msp, "HATCH"):
        try:
            lu = (effective_layer(e, xf) or "").upper()
            pat = str(e.dxf.get("pattern_name", "") or "").upper()
            if _NOT_WALL_HATCH_LAYER.search(lu):
                continue
            if not _WALL_HATCH_LAYER.search(lu):
                if not _WALL_HATCH_PATTERN.match(pat):
                    continue
                if _PATTERN_ONLY_NOT_WALL.search(lu) or any(
                        _PATTERN_ONLY_NOT_WALL.search(b or "") for b in xf.blocks):
                    continue
            rings = []
            for pth in ezpath.from_hatch(e):
                pts = [units.point_to_mm(*xf.apply(v.x, v.y)) for v in pth.flattening(5.0)]
                if len(pts) >= 3:
                    rings.append(pts)
            if not rings:
                continue
            rings.sort(key=lambda r: abs(shoelace_area_sqm(r)), reverse=True)
            if abs(shoelace_area_sqm(rings[0])) > WALL_HATCH_MAX_SQM:
                continue
            if islands and len(rings) > 1:
                for outer, holes in _hatch_islands(rings):
                    out.append((outer, holes, lu, pat))
                continue
            out.append((rings[0], rings[1:], lu, pat))
        except Exception:                             # noqa: BLE001
            continue
    return out


# -- Hanging walls: a wall drawn above floor level --------------------------
# An object drawn under a wall - a door swinging through it, a WC or a
# basin standing in it - says the wall does not reach the floor: a beam, a
# bulkhead, a drop wall. On BHUB 1st Floor a 300 mm C-shaped element of the
# structure xref crossed both toilets: three cubicle doors swing under it and
# the cubicle partitions run under it. Taken as a wall it cut 1.19 sq.m out
# of the female toilet and 1.11 out of the male one (typical floors, without
# it: 21.62 and 21.94 - the area sheet's figure on every floor).
# The whole element, from where it starts to where it ends, is a hanging
# wall: not a wall, its area counted in the room around it, drawn on
# AI_HANGING_WALL for checking. Where part of it stands on a real wall (another
# wall hatch, the faces of a wall layer) that part stays wall.
# What counts as an object under it: a door swing (a door-size arc on a door
# layer or in a door block) entering it, a sanitary fixture body (one fixture
# block placed) mostly inside it. Never lift or elevator graphics (a lift's
# entrance detail sits in the wall of a lift that does not stop at the floor),
# never a loose line (a drain on a sanitary layer, a section line, solar
# panels): 1,033 lines cross real walls on the seven test sites.
# config.json "hanging_walls"; rule "hanging_walls".
_HW = CONFIG.get("hanging_walls") or {}
HANGING_WALLS_ON = bool(_HW.get("enabled", True))
HANGING_INSET_MM = float(_HW.get("inset_mm", 40.0))
HANGING_DOOR_ARC_MIN_MM = float(_HW.get("door_arc_min_in_mm", 150.0))
HANGING_DOOR_RADIUS_MM = tuple(float(v) for v in (_HW.get("door_radius_mm") or (450.0, 1300.0)))
HANGING_FIXTURE_SHARE = float(_HW.get("fixture_min_share", 0.4))
HANGING_FIXTURE_SQM = tuple(float(v) for v in (_HW.get("fixture_area_sqm") or (0.05, 3.0)))
HANGING_MAX_THICK_MM = float(_HW.get("max_thickness_mm", 600.0))
HANGING_MAX_SQM = float(_HW.get("max_area_sqm", 15.0))
HANGING_OUTLINE_TOL_MM = float(_HW.get("outline_tol_mm", 20.0))
HANGING_OWN_LAYER_SHARE = float(_HW.get("own_layer_share", 0.9))
HANGING_COLOR = int(_HW.get("color", 6))
_NOT_UNDER = re.compile(str(_HW.get("never_objects", r"LIFT|ELEV|EVTR|\bELV\b")), re.I)
_HANGING_MEMO = {}


def _under_objects(doc, msp, units):
    """What may stand under a wall, in mm: door swings as lines
    [(LineString, layer)] and fixture bodies as polygons
    [(Polygon, block name)]."""
    from ezdxf import path as ezpath
    from shapely.geometry import LineString, MultiPoint
    lo, hi = HANGING_DOOR_RADIUS_MM
    swings, groups = [], {}
    for e, xf, _d in iter_entities(doc, msp, "LINE ARC LWPOLYLINE POLYLINE CIRCLE ELLIPSE SPLINE"):
        try:
            lay = effective_layer(e, xf) or ""
            blocks = [b or "" for b in xf.blocks]
            if _NOT_UNDER.search(lay) or any(_NOT_UNDER.search(b) for b in blocks):
                continue
            lu = lay.upper()
            scale = units.mm_per_unit * (xf.scale_avg or 1.0)
            kind = e.dxftype()
            swing = False
            if _is_door_leaf(lay, blocks) and kind in ("ARC", "LWPOLYLINE"):
                if kind == "ARC":
                    radii = [float(e.dxf.radius)]
                else:
                    pts_ = list(e.get_points("xyb"))
                    radii = []
                    for i in range(len(pts_) - 1 + (1 if e.closed else 0)):
                        a, b = pts_[i], pts_[(i + 1) % len(pts_)]
                        if a[2]:
                            radii.append(float(bulge_to_arc((a[0], a[1]), (b[0], b[1]), a[2])[3]))
                swing = any(lo <= r * scale <= hi for r in radii)
            fixture = not swing and bool(blocks) and (
                _FIXTURE_NAME_RE.search(blocks[-1].upper()) is not None
                or _FIXTURE_NAME_RE.search(lu) is not None or _is_fixture_layer(lu))
            if not (swing or fixture):
                continue
            pth = ezpath.make_path(e)
            pts = [units.point_to_mm(*xf.apply(v.x, v.y)) for v in pth.flattening(max(5.0 / scale, 1e-6))]
            if len(pts) < 2:
                continue
            if fixture:
                key = (tuple(blocks), round(xf.ix, 1), round(xf.iy, 1))
                groups.setdefault(key, []).extend(pts)
            else:
                swings.append((LineString(pts), lay))
        except Exception:                             # noqa: BLE001
            continue
    bodies = []
    a0, a1 = HANGING_FIXTURE_SQM
    for key, pts in groups.items():
        if len(pts) < 3:
            continue
        hull = MultiPoint(pts).convex_hull
        if hull.geom_type == "Polygon" and a0 * 1e6 <= hull.area <= a1 * 1e6:
            bodies.append((hull, key[0][-1] if key[0] else ""))
    return swings, bodies


def find_hanging_walls(doc, msp, units, wall_hatches):
    """The wall hatches that are hanging walls: an object drawn under them.
    [{"poly": Polygon (mm), "rings": [outline rings], "layer", "hatch": the
    wall_hatches entry, "evidence": "3 door swings", "area_sqm"}]. Worked
    out once per drawing (the text and the geometry engine both ask)."""
    from shapely.geometry import Polygon
    from shapely.strtree import STRtree
    if not HANGING_WALLS_ON or not wall_hatches:
        return []
    key = (id(doc), len(wall_hatches),
           hash(tuple((round(h[0][0][0], 1), round(h[0][0][1], 1), len(h[0])) for h in wall_hatches)))
    got = _HANGING_MEMO.get(key)
    if got is not None and got[0] is doc:
        return got[1]
    cands = []
    for h in wall_hatches:
        try:
            outer = Polygon(h[0]).buffer(0)
            if outer.is_empty or outer.area < 0.02e6 or outer.area > HANGING_MAX_SQM * 1e6:
                continue
            pg, rings = outer, [list(h[0])]
            for r in (h[1] if len(h) > 1 else []) or []:
                if len(r) < 3:
                    continue
                hp = Polygon(r).buffer(0)
                if hp.area > 0.85 * outer.area:
                    rings.append(list(r))     # the outline drawn again a few mm in: an outline ring, not a hole
                else:
                    pg = pg.difference(hp)
            if pg.is_empty or 2.0 * pg.area / max(pg.length, 1.0) > HANGING_MAX_THICK_MM:
                continue
            core = pg.buffer(-HANGING_INSET_MM)
            if core.is_empty:
                continue
            cands.append((h, pg, rings, core))
        except Exception:                             # noqa: BLE001
            continue
    out = []
    if cands:
        swings, bodies = _under_objects(doc, msp, units)
        st = STRtree([g for g, _l in swings]) if swings else None
        bt = STRtree([g for g, _b in bodies]) if bodies else None
        for h, pg, rings, core in cands:
            n_sw = n_fx = 0
            if st is not None:
                for i in st.query(core):
                    if swings[int(i)][0].intersection(core).length >= HANGING_DOOR_ARC_MIN_MM:
                        n_sw += 1
            if bt is not None:
                for i in bt.query(core):
                    body = bodies[int(i)][0]
                    if body.intersection(core).area >= HANGING_FIXTURE_SHARE * body.area:
                        n_fx += 1
            if not (n_sw or n_fx):
                continue
            ev = ", ".join(x for x in ("%d door swing%s" % (n_sw, "s" if n_sw > 1 else "") if n_sw else "",
                                       "%d fixture%s" % (n_fx, "s" if n_fx > 1 else "") if n_fx else "") if x)
            out.append({"poly": pg, "rings": rings, "layer": h[2] if len(h) > 2 else "", "hatch": h,
                        "evidence": ev, "area_sqm": round(pg.area / 1e6, 3)})
    if len(_HANGING_MEMO) > 4:
        _HANGING_MEMO.clear()
    _HANGING_MEMO[key] = (doc, out)
    return out


def drop_hanging_walls(found, raw_segs=None, wall_hatches=None, all_hatches=None, columns=None):
    """Take the hanging walls out of what the walls are made of: their hatch
    (from wall_hatches and all_hatches), the lines drawn as their outline
    (on the element's own layers - a layer whose lines run along nearly all
    of its outline; a real wall's face along part of it stays), and a C / L
    shape on the column layer that is the same element. Returns the new
    (raw_segs, wall_hatches, all_hatches, columns); None stays None."""
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union
    if not found:
        return raw_segs, wall_hatches, all_hatches, columns
    polys = [f["poly"] for f in found]

    def same(ring):
        try:
            g = Polygon(ring).buffer(0)
        except (ValueError, TypeError):
            return False
        if g.is_empty:
            return False
        for p in polys:
            if g.intersects(p):
                i = g.intersection(p).area
                if i >= 0.9 * g.area and i >= 0.9 * p.area:
                    return True
        return False

    gone = set(id(f["hatch"]) for f in found)
    if wall_hatches is not None:
        wall_hatches = [h for h in wall_hatches if id(h) not in gone and not same(h[0])]
    if all_hatches is not None:
        all_hatches = [h for h in all_hatches if not same(h[0])]
    if raw_segs is not None:
        tol = HANGING_OUTLINE_TOL_MM
        drop = set()
        for f in found:
            band = unary_union([LineString(list(r) + [r[0]]) for r in f["rings"]]).buffer(tol)
            per = LineString(list(f["rings"][0]) + [f["rings"][0][0]]).length
            b = band.bounds
            along = collections.defaultdict(list)
            for k, s in enumerate(raw_segs):
                if max(s[0], s[2]) < b[0] or min(s[0], s[2]) > b[2] or max(s[1], s[3]) < b[1] or min(s[1], s[3]) > b[3]:
                    continue
                ln = LineString([(s[0], s[1]), (s[2], s[3])])
                if ln.length > 0 and ln.intersection(band).length >= 0.8 * ln.length:
                    along[s[4]].append(k)
            for lay, ks in along.items():
                if sum(raw_segs[k][5] for k in ks) >= HANGING_OWN_LAYER_SHARE * per:
                    drop.update(ks)
        if drop:
            raw_segs = [s for k, s in enumerate(raw_segs) if k not in drop]
    if columns is not None:
        def keep(pts):
            try:
                g = Polygon(pts).buffer(0)
            except (ValueError, TypeError):
                return True
            return not any(g.intersects(p) and g.intersection(p).area >= 0.5 * g.area for p in polys)
        if isinstance(columns, dict):
            columns = {k: ({f: [c for c in v if keep(c if not hasattr(c, "exterior") else
                                                     list(c.exterior.coords)[:-1])]
                               for f, v in vv.items()} if isinstance(vv, dict) else vv)
                       for k, vv in columns.items()}
    return raw_segs, wall_hatches, all_hatches, columns


DOOR_APPID = "DRAWING_ANALYZER_DOOR"


def _tag_door(doc, entity, kind, level, sides=("", ""), quad=None, room_dir=None):
    """Store on a door what it opens into ("exit", "shaft", "cubicle"), its
    level ("primary", "secondary"), the spaces on its two sides - the room
    it belongs to first, where that is settled (elements.door_owners) - and,
    a door drawn as a line, the opening it closes (mm), as XDATA: invisible
    in CAD, read back by door_tag and door_quad."""
    try:
        if DOOR_APPID not in doc.appids:
            doc.appids.new(DOOR_APPID)
        vals = [(1000, str(kind or "exit")), (1000, str(level or "primary")),
                (1000, str(sides[0] or "")[:120]), (1000, str(sides[1] or "")[:120])]
        if quad is not None and len(quad) >= 3:
            vals.append((1000, ("mm:" + ";".join("%.1f,%.1f" % (float(p[0]), float(p[1])) for p in quad))[:250]))
        if room_dir is not None:
            vals.append((1000, "dir:%.4f,%.4f" % (float(room_dir[0]), float(room_dir[1]))))
        entity.set_xdata(DOOR_APPID, vals)
    except (AttributeError, TypeError, ValueError):
        pass


def door_quad(entity, units=None):
    """The opening a door entity closes, as points in mm: the opening kept
    in its XDATA (a door drawn as a line), else its own outline (a closed
    polyline - an older drawing, or door_as_line off), else - a line drawn
    by hand - a band DOOR_LINE_BAND_MM either side of it. None if neither."""
    units = units or MM_UNITS
    try:
        if entity.has_xdata(DOOR_APPID):
            vals = [v for code, v in entity.get_xdata(DOOR_APPID) if code == 1000]
            for v in vals[4:]:
                if str(v).startswith("mm:"):
                    pts = [tuple(float(c) for c in p.split(",")) for p in str(v)[3:].split(";") if "," in p]
                    if len(pts) >= 3:
                        return pts
    except (AttributeError, TypeError, ValueError):
        pass
    t = entity.dxftype()
    try:
        if t in ("LWPOLYLINE", "POLYLINE"):
            pts = _polyline_points(entity, units)
            if _is_closed(entity) and len(pts) >= 3:
                return pts
            if len(pts) >= 2:
                a, b = pts[0], pts[-1]
            else:
                return None
        elif t == "LINE":
            a = units.point_to_mm(entity.dxf.start[0], entity.dxf.start[1])
            b = units.point_to_mm(entity.dxf.end[0], entity.dxf.end[1])
        else:
            return None
    except Exception:                                 # noqa: BLE001
        return None
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    if L < 1.0:
        return None
    nx, ny = -(b[1] - a[1]) / L * DOOR_LINE_BAND_MM, (b[0] - a[0]) / L * DOOR_LINE_BAND_MM
    return [(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny), (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)]


def door_tag(entity):
    """(kind, level) _tag_door stored on a door, else what its layer says
    (an older drawing's AI_DOOR / AI_DOOR_SHAFT / AI_DOOR_CUBICLE), with
    "exit" and "primary" where nothing says."""
    kind = level = None
    try:
        if entity.has_xdata(DOOR_APPID):
            vals = [v for code, v in entity.get_xdata(DOOR_APPID) if code == 1000]
            kind = vals[0] if vals else None
            level = vals[1] if len(vals) > 1 else None
    except (AttributeError, TypeError, ValueError):
        pass
    layer = entity.dxf.layer if hasattr(entity, "dxf") else ""
    kind = kind or LN.door_layer_kind(layer) or "exit"
    level = level or LN.door_layer_level(layer) or ("secondary" if kind == "cubicle" else "primary")
    return kind, level


def draw_doors(doc, msp, units, quads, kinds=None, levels=None, sides=None, rooms=None, owners=None, lines=None,
               dirs=None):
    """Door openings (the gap in the wall a door closes): never a room,
    never a wall, never in an area. A primary door (between two different
    spaces: it stops a room) on AI_P_DOORS, a secondary one (inside one
    room - a WC cubicle's: it never stops a room) on AI_S_DOORS
    (elements.door_levels); what it opens into and its sides in its XDATA -
    the room it belongs to first.

    Each is drawn as a single line joining the walls either side: lines, one
    per quad, where given - the room's inner wall trace across the opening
    (elements.door_trace, 2026-10-17); else on the inner face of the room
    the door belongs to (owners: elements.door_owners' side, 0 or 1); where
    neither is settled, elements.door_line's face - door_as_line, config
    wall_output. The opening itself is kept in the XDATA (door_quad reads it
    back), and the way into the door's room (dirs: a unit vector per quad,
    door_room_dir reads it back). Off: the opening as a closed polyline, as
    before. kinds / levels / sides: one per quad (default exit / primary);
    rooms: the rooms drawn (shapely polygons), for door_line. Returns
    {level: count}."""
    n = collections.Counter()
    tree = None
    room_list = [r for r in (rooms or ()) if r is not None and not r.is_empty] if DOOR_AS_LINE else []
    if room_list:
        from shapely.strtree import STRtree
        tree = STRtree(room_list)
    from . import elements as _el
    for k, q in enumerate(quads or ()):
        if len(q) < 3:
            continue
        kind = kinds[k] if kinds and k < len(kinds) else "exit"
        level = levels[k] if levels and k < len(levels) else "primary"
        layer, aci = ((LN.DOOR_SECONDARY, DOOR_SECONDARY_ACI) if level == "secondary"
                      else (LN.DOOR_PRIMARY, DOOR_ACI))
        if layer not in doc.layers:
            doc.layers.add(layer, color=aci)
        ln = None
        if DOOR_AS_LINE:
            ln = lines[k] if lines and k < len(lines) else None
            side = owners[k] if owners and k < len(owners) else None
            if ln is None and side is not None:
                ln = _el.owner_face(q, side)
            if ln is None:
                ln = _el.door_line(q, room_list, tree)
        if ln is not None:
            e = msp.add_lwpolyline([(units.from_mm(x), units.from_mm(y)) for x, y in ln], close=False,
                                   dxfattribs={"layer": layer, "color": 256})
        else:
            e = msp.add_lwpolyline([(units.from_mm(x), units.from_mm(y)) for x, y in q], close=True,
                                   dxfattribs={"layer": layer, "color": 256})
        _tag_door(doc, e, kind, level, (sides[k] if sides and k < len(sides) else ("", "")), quad=q,
                  room_dir=(dirs[k] if dirs and k < len(dirs) else None))
        n[level] += 1
    return dict(n)


def _door_room_items(rooms):
    """The rooms draw_doors_by_level takes, read: (polygons, is_shaft,
    is_main, is_lift, rank) lists, one entry per usable room."""
    from . import elements as _el
    from shapely.geometry import Polygon
    gs, sh, mn, lf, rk = [], [], [], [], []
    for item in rooms or ():
        pg, is_shaft = item[0], item[1]
        is_main = item[2] if len(item) > 2 else False
        is_lift = item[3] if len(item) > 3 else False
        rank = item[4] if len(item) > 4 else None
        try:
            g = pg if hasattr(pg, "geom_type") else Polygon([tuple(p[:2]) for p in pg]).buffer(0)
        except (ValueError, TypeError, IndexError):
            continue
        if g is None or g.is_empty or g.area <= 0:
            continue
        gs.append(g)
        sh.append(bool(is_shaft))
        mn.append(bool(is_main))
        lf.append(bool(is_lift))
        if rank is None:
            rank = (_el.OWNER_SHAFT if (is_shaft or is_lift) else
                    _el.OWNER_LOBBY if is_main else _el.OWNER_ROOM)
        rk.append(int(rank))
    return gs, sh, mn, lf, rk


def door_plan(quads, gs, rk, swings=(), step3=(), trace_solid=None):
    """For each door opening, (side, owner, t, how): the face of the room it
    belongs to (0 / 1, as elements.door_owners; None not settled), that room
    (an index into gs, or None), the line's offset t across the opening (on
    the room's inner wall trace: elements.door_trace; None - the room's
    face) and what settled it. An opening that is one of Step 3's doors
    (step3, door_hints) takes Step 3's side and line; any other the same
    rule here (door_owners, then door_trace on trace_solid).
    gs / rk: the rooms (shapely) and their ranks (_door_room_items)."""
    from . import elements as _el
    quads = list(quads or ())
    own = _el.door_owners(quads, gs, rk, swings) if _el.DOOR_OWNER else [(None, None, "off")] * len(quads)
    hints = door_hints(quads, step3)
    sides = None
    if gs and any(h is not None for h in hints):
        from shapely.strtree import STRtree
        tree = STRtree(gs)
        sides = [_el.door_sides(q, gs, tree) if h is not None else (None, None) for q, h in zip(quads, hints)]
    out = []
    for k, q in enumerate(quads):
        h = hints[k]
        if h is not None:
            sd, t, owned = h
            who = (sides[k][0 if sd == 0 else 1] if sides else None) if owned else None
            out.append((sd, who, t, "step 3" if owned else "step 3 line"))
            continue
        sd, who, how = own[k]
        t = None
        if sd is not None and trace_solid is not None and _el.DOOR_TRACE:
            ax = _el.door_axes(q)
            if ax is not None and ax[7] > _el.DOOR_CUBICLE_BOARD_MAX_MM:
                t, how_t, _jog = _el.door_trace(q, sd, trace_solid)
                if t is not None:
                    how = how_t
        out.append((sd, who, t, how))
    return out


def draw_doors_by_level(doc, msp, units, quads, rooms, labels=(), plate=None, solid=None, swings=(), step3=(),
                        trace_solid=None, plan=None):
    """Door openings drawn primary / secondary (elements.door_kinds and
    door_levels) with the rooms of the drawing they are drawn in: rooms
    [(polygon points or shapely polygon, is_shaft, is_main[, is_lift[,
    rank]])]; plate: the floor outline and solid: its walls, columns, glass
    and windows (shapely), for the untraced common floor.

    Each door belongs to one room (elements.door_owners, rank: elements.
    door_room_rank - from is_shaft / is_lift / is_main where not given) and
    its line is that room's inner wall trace across the opening (elements.
    door_trace on trace_solid; without it, the room's face of the opening):
    the same rule as Step 3. step3: Step 3's doors [(opening, shapely
    polygon; its line's middle (x, y); the way into its room (dx, dy) or
    None)] - an opening here that is one of them takes Step 3's side and
    line, so the final drawing draws every such door where Step 3 did
    (door_hints). swings: dxf_reader._collect_door_arcs, for two like rooms.
    plan: door_plan, when the caller has it already. Returns {level: count}."""
    from . import elements as _el
    quads = [q for q in quads or () if len(q) >= 3]
    if not quads:
        return {}
    gs, sh, mn, lf, rk = _door_room_items(rooms)
    kinds = (_el.door_kinds(quads, gs, sh, labels, lift=lf) if _el.DOOR_KINDS
             else [(_el.EXIT, None, None)] * len(quads))
    opens = _el.open_floor(plate, gs, solid, quads) if plate is not None else []
    levels = _el.door_levels(quads, gs, mn, sh, kinds, opens)
    if plan is None or len(plan) != len(quads):
        plan = door_plan(quads, gs, rk, swings, step3, trace_solid)
    owners = [p[0] for p in plan]
    lines = [(_el.trace_line(q, p[2]) if p[2] is not None else None) for q, p in zip(quads, plan)]
    dirs = [door_room_vector(q, p[0]) for q, p in zip(quads, plan)]
    return draw_doors(doc, msp, units, quads, kinds=[k[0] for k in kinds], levels=[v[0] for v in levels],
                      rooms=gs, owners=owners, lines=lines, dirs=dirs)


def door_room_vector(quad, side):
    """The way into the room a door belongs to, as a unit vector (the
    opening's + normal for side 0, - for side 1), or None."""
    from . import elements as _el
    ax = _el.door_axes(quad)
    if ax is None or side not in (0, 1):
        return None
    sg = 1.0 if side == 0 else -1.0
    return (sg * ax[4], sg * ax[5])


def door_room_dir(entity):
    """The way into the room a door belongs to that draw_doors stored on it
    ((dx, dy), a unit vector), or None."""
    try:
        if entity.has_xdata(DOOR_APPID):
            for code, v in entity.get_xdata(DOOR_APPID):
                if code == 1000 and str(v).startswith("dir:"):
                    dx, dy = (float(c) for c in str(v)[4:].split(","))
                    return (dx, dy)
    except (AttributeError, TypeError, ValueError):
        pass
    return None


def door_hints(quads, step3=()):
    """For each door opening, (side, t, owned) of the Step 3 door at the same
    place - an opening of step3 overlapping it by half the smaller: the
    room's face it belongs to (0 or 1, as elements.door_owners) from the way
    into its room Step 3 stored (owned True; else from the side its line
    stands on, owned False - Step 3 settled no room, as for a room traced
    through its own door), and where Step 3 drew its line, as an offset t
    across this opening; None
    where there is none (or, with no way stored, its line stands in the
    middle of the wall). step3: [(shapely polygon, (x, y) the line's
    middle[, (dx, dy) the way into its room or None])]."""
    from . import elements as _el
    out = [None] * len(quads or ())
    if not step3:
        return out
    from shapely.geometry import Polygon
    from shapely.strtree import STRtree
    polys = [it[0] for it in step3]
    tree = STRtree(polys)
    for k, q in enumerate(quads or ()):
        ax = _el.door_axes(q)
        if ax is None:
            continue
        cx, cy, ux, uy, nx, ny, W, D = ax
        try:
            Q = Polygon([(float(p[0]), float(p[1])) for p in q]).buffer(0)
        except (ValueError, TypeError, IndexError):
            continue
        best = None
        for m in tree.query(Q):
            g = polys[int(m)]
            a = g.intersection(Q).area
            if a >= 0.5 * min(g.area, Q.area) and (best is None or a > best[0]):
                best = (a, step3[int(m)])
        if best is None:
            continue
        it = best[1]
        mid = it[1]
        way = it[2] if len(it) > 2 else None
        t = (mid[0] - cx) * nx + (mid[1] - cy) * ny
        if way is not None:
            d = way[0] * nx + way[1] * ny
            if abs(d) > 0.5:
                out[k] = (0 if d > 0 else 1, t, True)
                continue
        if abs(t) >= D / 4.0:
            out[k] = (0 if t > 0 else 1, t, False)       # Step 3 settled no room: its line only
    return out


GLASS_ACI = int((CONFIG.get("glass") or {}).get("color", 140))


def draw_glass(doc, msp, units, rings):
    """Glass partitions and glazing (elements.glass_elements) on AI_GLASS,
    each a closed polyline: never a wall, never a room, never in an area."""
    n = 0
    for pts in rings or ():
        if len(pts) < 3:
            continue
        if LN.GLASS not in doc.layers:
            doc.layers.add(LN.GLASS, color=GLASS_ACI)
        msp.add_lwpolyline([(units.from_mm(x), units.from_mm(y)) for x, y in pts], close=True,
                           dxfattribs={"layer": LN.GLASS, "color": 256})
        n += 1
    return n


def draw_window_quads(doc, msp, units, quads):
    """Windows found in the wall gaps (window_openings) on AI_WINDOW, each a
    closed polyline: part of the wall, never a room."""
    n = 0
    for q in quads or ():
        if len(q) < 3:
            continue
        if WINDOW_LAYER not in doc.layers:
            doc.layers.new(WINDOW_LAYER, dxfattribs={"color": WINDOW_COLOR})
        msp.add_lwpolyline([(units.from_mm(x), units.from_mm(y)) for x, y in q], close=True,
                           dxfattribs={"layer": WINDOW_LAYER, "color": 256})
        n += 1
    return n


def draw_hanging_walls(doc, msp, units, found):
    """The hanging walls on AI_HANGING_WALL: each outline a closed polyline,
    dashed where the drawing has a DASHED linetype. Not walls, not rooms,
    not in any area - shown so they can be checked."""
    if not found:
        return 0
    if LN.HANGING_WALL not in doc.layers:
        doc.layers.add(LN.HANGING_WALL, color=HANGING_COLOR)
    lt = "DASHED" if "DASHED" in doc.linetypes else "CONTINUOUS"
    n = 0
    for f in found:
        pts = f["pts"] if "pts" in f else list(f["poly"].exterior.coords)[:-1]
        if len(pts) >= 3:
            msp.add_lwpolyline([(units.from_mm(x), units.from_mm(y)) for x, y in pts], close=True,
                               dxfattribs={"layer": LN.HANGING_WALL, "color": 256, "linetype": lt})
            n += 1
    return n


# -- Windows: an opening drawn inside a wall --------------------------------
# A closed outline - X-marked or not - no thicker than a wall, lying in one
# wall (its long sides on that wall's two faces, or inside a wall hatch),
# is a window or an opening in the wall, not a shaft: on BHUB 1st Floor 54
# of the 81 X-marks were 200-380 mm deep, in walls, tagged C-/W-/SD-. A
# window takes no room label, is never a room, and is a wall to the rooms
# traced beside it. config.json "windows".
_WIN = CONFIG.get("windows") or {}
WINDOW_LAYER = str(_WIN.get("layer", "AI_WINDOW"))
WINDOW_COLOR = int(_WIN.get("color", 4))
# What is drawn inside an X-marked opening, and the layers it is drawn on,
# tell a window from a shaft strip or a cut-out (OpeningEvidence).
GLAZING_MIN_LINES = int(_WIN.get("glazing_min_lines", 2))
GLAZING_MIN_SHARE = float(_WIN.get("glazing_min_share", 0.6))
GLAZING_EDGE_TOL_MM = float(_WIN.get("glazing_edge_tol_mm", 10.0))
GLAZING_ANGLE_DEG = float(_WIN.get("glazing_angle_deg", 3.0))
OPENING_MATCH_MM = float(_WIN.get("outline_match_mm", 20.0))
WINDOW_LAYER_WORDS = list(_WIN.get("window_layer_words") or
                          ["LOUVER", "LOUVRE", "WINDOW", "WIN", "GLAZ", "GLASS", r"W-\d"])
SHAFT_LAYER_WORDS = list(_WIN.get("shaft_layer_words") or
                         ["SHAFT", "SH", "RISER", "DUCT", "PRESS", "PREZ", "PHE", "FP", "RW", "RWDP", "FHC",
                          "TEN", "TENANT", "COMM", "COMMUNICATION", "ELV", "BMS", "SPARE", "FUTURE"])
CUTOUT_LAYER_WORDS = list(_WIN.get("cutout_layer_words") or
                          ["CUTOUT", "CUT OUT", "CUT-OUT", "OPENING"])
CUTOUT_LAYER = str(_WIN.get("cutout_layer", LN.CUTOUT))
CUTOUT_COLOR = int(_WIN.get("cutout_color", 6))
CUTOUT_NAME = "CUTOUT"
WINDOW_MAX_THICKNESS_MM = float(_WIN.get("max_thickness_mm", 400.0))
WINDOW_FACE_TOL_MM = float(_WIN.get("face_tol_mm", 30.0))
WINDOW_SIDE_SHARE = float(_WIN.get("side_share", 0.5))
WINDOW_WALL_RUN_MM = float(_WIN.get("wall_run_mm", 300.0))
WINDOW_OPENING_MIN_ASPECT = float(_WIN.get("opening_min_aspect", 1.2))


def _short_side(pg):
    """The short side of a polygon's minimum rotated rectangle, in mm."""
    try:
        cs = list(pg.minimum_rotated_rectangle.exterior.coords)[:4]
        return min(math.hypot(cs[k + 1][0] - cs[k][0], cs[k + 1][1] - cs[k][1]) for k in range(3))
    except Exception:                                 # noqa: BLE001
        return float("inf")


class WindowFinder:
    """Tells whether a closed outline is a window in a wall.

    faces: wall lines (x1, y1, x2, y2) in mm; hatches: [(outer, holes)] in
    mm, the wall hatches (_collect_wall_hatches)."""

    def __init__(self, faces, hatches=None, evidence=None):
        from shapely.geometry import LineString, Polygon
        from shapely.strtree import STRtree
        self.evidence = evidence                      # OpeningEvidence, or None
        self.lines = [LineString([(f[0], f[1]), (f[2], f[3])]) for f in faces or ()
                      if math.hypot(f[2] - f[0], f[3] - f[1]) > 1.0]
        self.tree = STRtree(self.lines) if self.lines else None
        # (the hatch, its outer ring, its holes): a window sits in the wall
        # hatch, or in a hole cut in it for the window
        hp = []
        for h in hatches or ():
            try:
                holes = [r for r in (h[1] if len(h) > 1 else []) if len(r) >= 3]
                outer = Polygon(h[0])
                outer = outer if outer.is_valid else outer.buffer(0)
                full = Polygon(h[0], holes)
                full = full if full.is_valid else full.buffer(0)
                if outer.is_empty:
                    continue
                hole_pgs = []
                for r in holes:
                    hpg = Polygon(r)
                    hole_pgs.append(hpg if hpg.is_valid else hpg.buffer(0))
                hp.append((full.buffer(1.0), outer.buffer(1.0), hole_pgs))
            except (ValueError, TypeError, IndexError):
                continue
        self.hatches = hp
        self.htree = STRtree([o for _f, o, _h in hp]) if hp else None

    def _in_hatch(self, r):
        """The rectangle r lies in a wall hatch, or fills a hole in one."""
        if self.htree is None:
            return False
        # its outline may lie on the wall's faces, a hatch's edge just inside
        inner = r.buffer(-min(WINDOW_FACE_TOL_MM, 0.25 * _short_side(r)))
        if inner.is_empty:
            return False
        for k in self.htree.query(inner):
            full, outer, holes = self.hatches[k]
            if not outer.contains(inner):
                continue
            if full.contains(inner):
                return True
            # a hole that is a slot in the wall (no wider than a wall), not
            # a room or shaft the hatched wall goes round
            if any(hpg.buffer(1.0).contains(inner) and _short_side(hpg) <= WINDOW_MAX_THICKNESS_MM
                   for hpg in holes):
                return True
        return False

    def _cover(self, a, b, band, own=None):
        """Share of the segment a-b matched by wall lines parallel to it
        lying within `band` (a polygon beside it). Lines lying within `own`
        (the outline's own edges) do not count."""
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        if L <= 0 or self.tree is None:
            return 0.0
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        cos_tol = math.cos(math.radians(5.0))
        spans = []
        for k in self.tree.query(band):
            f = self.lines[k]
            if own is not None and own.contains(f):
                continue
            (fx0, fy0), (fx1, fy1) = f.coords[0], f.coords[-1]
            fl = f.length
            if abs(ux * (fx1 - fx0) / fl + uy * (fy1 - fy0) / fl) < cos_tol:
                continue
            part = f.intersection(band)
            for g in getattr(part, "geoms", [part]):
                if g.is_empty or g.geom_type != "LineString" or g.length <= 0:
                    continue
                c0, c1 = g.coords[0], g.coords[-1]
                t0 = (c0[0] - a[0]) * ux + (c0[1] - a[1]) * uy
                t1 = (c1[0] - a[0]) * ux + (c1[1] - a[1]) * uy
                lo, hi = max(0.0, min(t0, t1)), min(L, max(t0, t1))
                if hi > lo:
                    spans.append((lo, hi))
        spans.sort()
        tot, cur = 0.0, 0.0
        for lo, hi in spans:
            if hi <= cur:
                continue
            tot += hi - max(lo, cur)
            cur = hi
        return tot / L

    @staticmethod
    def _band(a, b, centre, d0, d1):
        """The strip beside a-b from d0 to d1 outward (away from centre)."""
        from shapely.geometry import Polygon
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        nx, ny = -(b[1] - a[1]) / L, (b[0] - a[0]) / L
        mx, my = (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
        if (centre[0] - mx) * nx + (centre[1] - my) * ny > 0:
            nx, ny = -nx, -ny
        return Polygon([(a[0] + nx * d0, a[1] + ny * d0), (b[0] + nx * d0, b[1] + ny * d0),
                        (b[0] + nx * d1, b[1] + ny * d1), (a[0] + nx * d1, a[1] + ny * d1)])

    def _face_runs_on(self, a, b, c, tol, own):
        """Wall faces in line with the side a-b beyond both its ends."""
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        run, d0 = WINDOW_WALL_RUN_MM, 5.0
        for p, s in ((a, -1.0), (b, 1.0)):
            p0 = (p[0] + s * ux * d0, p[1] + s * uy * d0)
            p1 = (p[0] + s * ux * run, p[1] + s * uy * run)
            if self._cover(p0, p1, self._band(p0, p1, c, -tol, tol), own) < WINDOW_SIDE_SHARE:
                return False
        return True

    def _wall_beyond(self, rect, end, longs, tol):
        """A wall line (not the outline's own) in the strip beyond one end
        of the outline, inside its thickness, WINDOW_WALL_RUN_MM long."""
        from shapely.geometry import Polygon
        (a1, b1), (a2, b2) = longs
        # the two long sides run in opposite directions round the rectangle
        p1 = a1 if end[0] == 0 else b1
        p2 = b2 if end[0] == 0 else a2
        L = math.hypot(b1[0] - a1[0], b1[1] - a1[1])
        ux, uy = (b1[0] - a1[0]) / L, (b1[1] - a1[1]) / L
        if end[0] == 0:
            ux, uy = -ux, -uy                         # outward from this end
        d0, d1 = 5.0, WINDOW_WALL_RUN_MM
        c = (rect.centroid.x, rect.centroid.y)
        # the wall runs on: faces in line with a long side beyond this end
        for p in (p1, p2):
            a = (p[0] + ux * d0, p[1] + uy * d0)
            b = (p[0] + ux * d1, p[1] + uy * d1)
            if self._cover(a, b, self._band(a, b, c, -tol, tol)) >= WINDOW_SIDE_SHARE:
                return True
        # or turns: a wall across its thickness there
        T = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        if T <= 2 * tol:
            return False
        vx, vy = (p2[0] - p1[0]) / T, (p2[1] - p1[1]) / T
        q1 = (p1[0] + vx * tol, p1[1] + vy * tol)     # inside the thickness only:
        q2 = (p2[0] - vx * tol, p2[1] - vy * tol)     # a corner touching does not count
        d0, d1 = 5.0, WINDOW_WALL_RUN_MM
        strip = Polygon([(q1[0] + ux * d0, q1[1] + uy * d0), (q2[0] + ux * d0, q2[1] + uy * d0),
                         (q2[0] + ux * d1, q2[1] + uy * d1), (q1[0] + ux * d1, q1[1] + uy * d1)])
        own = rect.buffer(10.0)
        if self.tree is None:
            return False
        return any(not own.contains(self.lines[k]) and self.lines[k].intersects(strip)
                   for k in self.tree.query(strip))

    def reason(self, pg):
        """Why pg is a window (a sentence), or None when it is not one."""
        try:
            r = pg.minimum_rotated_rectangle
            cs = list(r.exterior.coords)[:4]
        except Exception:                             # noqa: BLE001
            return None
        if len(cs) < 4:
            return None
        ed = [(cs[k], cs[(k + 1) % 4]) for k in range(4)]
        ln = [math.hypot(e[1][0] - e[0][0], e[1][1] - e[0][1]) for e in ed]
        short, long_ = min(ln), max(ln)
        if short <= 0 or short > WINDOW_MAX_THICKNESS_MM:
            return None
        size = "{0:.0f} x {1:.0f} mm".format(short, long_)
        longs = [ed[0], ed[2]] if ln[0] >= ln[1] else [ed[1], ed[3]]
        c = (r.centroid.x, r.centroid.y)
        tol = WINDOW_FACE_TOL_MM
        # A slot between two walls - a riser between their inner faces -
        # has a further wall face beyond each long side (the walls' outer
        # faces): a shaft, not a window, whatever is hatched round it.
        beyond = [self._cover(a, b, self._band(a, b, c, WALL_T_MIN, WALL_T_MAX)) for a, b in longs]
        if min(beyond) >= WINDOW_SIDE_SHARE:
            return None
        if self._in_hatch(r):
            return "{0}, inside a wall hatch".format(size)
        # a face of the wall, not the outline's own edge paired with a
        # wall beside it (a riser strip against a wall has only its own
        # edge on its far side)
        # Both faces of a wall: a line along each long side (its own outline
        # edge will do here). An X drawn without an outline, inside a
        # walled shaft against one of its walls, has nothing along its
        # other side.
        if min(self._cover(a, b, self._band(a, b, c, -tol, tol)) for a, b in longs) < WINDOW_SIDE_SHARE:
            return None
        own = r.buffer(tol)
        # What lies along each long side: a wall face running past it (not
        # its own edge), a wall face that stops at it and carries on beyond
        # both its ends (an opening cut in the wall), or only its own edge.
        along = []
        for a, b in longs:
            if self._cover(a, b, self._band(a, b, c, -tol, tol), own) >= WINDOW_SIDE_SHARE:
                along.append("face")
            elif self._face_runs_on(a, b, c, tol, own):
                along.append("opening")
            else:
                along.append("own")
        if "own" in along and along != ["own", "own"]:
            # a wall face along one side and nothing along the other: a
            # strip lying against a wall - a riser (BHUB: the 300 mm tenant
            # risers), not an opening in the wall
            return None
        if along == ["own", "own"]:
            if long_ < WINDOW_OPENING_MIN_ASPECT * short:
                return None                           # a square mark: no long sides in a wall
            # in a wall's line with its faces stopping at it: an opening only
            # where the wall runs on or turns beyond one of its ends - a
            # strip standing on its own (a cut-out beside a shaft) has
            # nothing there but its own outline
            if not any(self._wall_beyond(r, e, longs, tol) for e in ((0, 0), (1, 1))):
                return None
            return "{0}, an opening in the line of a wall (between wall ends)".format(size)
        return "{0}, its long sides on the two faces of one wall".format(size)

    def classify(self, pg):
        """(kind, why, evidence, conflict word) for an X-marked outline:
        kind "window", "shaft", "cutout", or None (not an opening in a wall).

        Only an outline the wall tests (reason) take for an opening in a
        wall is sorted; then what is drawn in it decides - glazing lines
        make it a window, no glazing and a shaft layer word a shaft strip,
        no glazing and a cut-out word a cut-out. With no telling evidence
        it stays a window, as the wall tests found."""
        why = self.reason(pg)
        if why is None:
            return None, None, None, None
        if self.evidence is None:
            return "window", why, None, None
        ev = self.evidence.gather(pg)
        kind, clash = classify_opening(ev)
        if kind == "window":
            return "window", "{0}; {1} glazing line{2} inside".format(
                why, ev["glazing"], "" if ev["glazing"] == 1 else "s"), ev, clash
        if kind == "shaft":
            return "shaft", "{0}; no glazing, layer says {1}".format(why, ev["shaft"][0]), ev, None
        if kind == "cutout":
            return "cutout", "{0}; no glazing, layer says {1}".format(why, ev["cutout"][0]), ev, None
        return "window", why, ev, None


def layer_text(layer):
    """A layer's name as words: the XREF prefix gone (the text after the
    last "$" or "|"), upper case, punctuation as spaces."""
    t = re.split(r"[$|]", str(layer or ""))[-1]
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", t.upper()).split())


def _word_res(words):
    out = []
    for w in words:
        pat = re.sub(r"[-_.]+", " ", str(w)).strip()
        try:
            out.append((str(w).upper(), re.compile(r"(?<![A-Z0-9])" + pat + r"S?(?![A-Z0-9])", re.I)))
        except re.error:
            continue
    return out


_WINDOW_WORD_RES = _word_res(WINDOW_LAYER_WORDS)
_SHAFT_WORD_RES = _word_res(SHAFT_LAYER_WORDS)
_CUTOUT_WORD_RES = _word_res(CUTOUT_LAYER_WORDS)


def _words_in(text, res):
    return [w for w, rx in res if rx.search(text)]


def opening_name(layer):
    """A shaft or cut-out named from its area-plan overlay or layer:
    "3.12 FP SHAFT" -> "FP SHAFT" (XREF prefix and leading number gone)."""
    t = re.split(r"[$|]", str(layer or ""))[-1].strip().upper()
    t = re.sub(r"^\d+(\.\d+)*\s+", "", t)
    return " ".join(t.split())


class OpeningEvidence:
    """What an X-marked rectangle holds: its glazing lines (lines inside it
    along its long side, at least GLAZING_MIN_SHARE of it, clear of its
    own long edges by GLAZING_EDGE_TOL_MM) and the words of the layers of
    everything drawn in it or on its outline - its X, its edges, the lines
    inside, an area-plan overlay matching it within OPENING_MATCH_MM.

    segs: the floor's segments, (x1, y1, x2, y2, layer, ...) in mm."""

    def __init__(self, segs):
        from shapely.geometry import LineString
        from shapely.strtree import STRtree
        self.segs = [sg for sg in segs or () if math.hypot(sg[2] - sg[0], sg[3] - sg[1]) > 1.0]
        try:
            # all at once (the same lines, made in one call)
            import numpy as np
            import shapely
            self.lines = list(shapely.linestrings(
                np.array([(sg[0], sg[1], sg[2], sg[3]) for sg in self.segs], dtype=np.float64).reshape(-1, 2, 2))) \
                if self.segs else []
        except (ImportError, AttributeError, ValueError, TypeError):
            self.lines = [LineString([(sg[0], sg[1]), (sg[2], sg[3])]) for sg in self.segs]
        self.tree = STRtree(self.lines) if self.lines else None

    def gather(self, pg):
        ev = {"glazing": 0, "layers": [], "window": [], "shaft": [], "cutout": [], "name": None}
        try:
            r = pg.minimum_rotated_rectangle
            cs = list(r.exterior.coords)[:4]
        except Exception:                             # noqa: BLE001
            return ev
        if self.tree is None or len(cs) < 4:
            return ev
        ed = [(cs[k], cs[(k + 1) % 4]) for k in range(4)]
        ln = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in ed]
        k = 0 if ln[0] >= ln[1] else 1
        (ax, ay), (bx, by) = ed[k]
        L, T = ln[k], ln[1 - k]
        if L <= 0 or T <= 0:
            return ev
        ux, uy = (bx - ax) / L, (by - ay) / L
        nx, ny = -uy, ux
        cos_tol = math.cos(math.radians(GLAZING_ANGLE_DEG))
        near = r.buffer(OPENING_MATCH_MM)
        inside = r.buffer(1.0)
        offsets, layers = [], []
        for i in self.tree.query(near):
            f = self.lines[int(i)]
            if not near.contains(f):
                continue
            lay = self.segs[int(i)][4]
            if lay not in layers:
                layers.append(lay)
            if f.length < GLAZING_MIN_SHARE * L or not inside.contains(f):
                continue
            (x0, y0), (x1, y1) = f.coords[0], f.coords[-1]
            if abs(ux * (x1 - x0) + uy * (y1 - y0)) / f.length < cos_tol:
                continue
            d0 = abs((x0 - ax) * nx + (y0 - ay) * ny)
            d1 = abs((x1 - ax) * nx + (y1 - ay) * ny)
            if min(d0, d1) > GLAZING_EDGE_TOL_MM and max(d0, d1) < T - GLAZING_EDGE_TOL_MM:
                d = (d0 + d1) / 2.0
                if all(abs(d - o) > 5.0 for o in offsets):
                    offsets.append(d)                 # a line drawn twice is one line
        ev["glazing"] = len(offsets)
        ev["layers"] = layers
        for lay in layers:
            t = layer_text(lay)
            for key, res in (("window", _WINDOW_WORD_RES), ("shaft", _SHAFT_WORD_RES), ("cutout", _CUTOUT_WORD_RES)):
                for w in _words_in(t, res):
                    if w not in ev[key]:
                        ev[key].append(w)
            if ev["name"] is None and _words_in(t, _SHAFT_WORD_RES):
                ev["name"] = opening_name(lay)
        # an overlay named "3.12 FP SHAFT" names the shaft, before a layer
        # that only says SHAFT somewhere
        for lay in layers:
            raw = re.split(r"[$|]", str(lay or ""))[-1].strip()
            if re.match(r"^\d+(\.\d+)*\s+", raw) and _words_in(layer_text(lay), _SHAFT_WORD_RES):
                ev["name"] = opening_name(lay)
                break
        return ev


def classify_opening(ev):
    """window / shaft / cutout from what is drawn (glazing lines first,
    then layer words), or None when there is no telling evidence. The
    glazing always outranks the words. Returns (kind, conflict word)."""
    g = ev.get("glazing", 0)
    if g >= GLAZING_MIN_LINES or (g >= 1 and ev.get("window")):
        clash = None
        if g >= GLAZING_MIN_LINES and not ev.get("window"):
            clash = (ev.get("shaft") or ev.get("cutout") or [None])[0]
        return "window", clash
    if g == 0 and ev.get("shaft"):
        return "shaft", None
    if g == 0 and ev.get("cutout"):
        return "cutout", None
    return None, None


def cutout_name(ev):
    """A cut-out's name: CUTOUT, or the overlay/layer text when it says
    what kind (DOUBLE HEIGHT ... CUTOUT)."""
    for lay in ev.get("layers") or ():
        t = layer_text(lay)
        if re.search(r"\b(DOUBLE|TRIPLE) HEIGHT\b", t):
            return opening_name(lay)
    return CUTOUT_NAME


def guard_against_walls(poly, holes, bbox, label_xy, wall_lines, hatches, cut_mm=1.0):
    """A boundary never crosses a wall, a wall hatch or the facade. Where it
    does, it is cut there and the piece holding its label (else the
    largest) is kept; a column standing inside it becomes a hole.

    wall_lines: [(x1, y1, x2, y2)]; hatches: [(outer, holes)] in mm.
    Returns (poly, holes, bbox, changed) - unchanged when nothing crosses."""
    from shapely.geometry import Polygon, LineString, Point, box as sbox
    from shapely.ops import unary_union
    try:
        g = Polygon(poly, [h for h in (holes or []) if len(h) >= 3]) if poly else sbox(*bbox)
        if not g.is_valid:
            g = g.buffer(0)
    except (ValueError, TypeError):
        return poly, holes, bbox, False
    if g.is_empty or g.area <= 0:
        return poly, holes, bbox, False
    inner = g.buffer(-max(cut_mm * 3.0, 3.0))
    if inner.is_empty:
        return poly, holes, bbox, False
    b = g.bounds
    cuts = []
    for w in wall_lines:
        if (max(w[0], w[2]) < b[0] or min(w[0], w[2]) > b[2]
                or max(w[1], w[3]) < b[1] or min(w[1], w[3]) > b[3]):
            continue
        if (w[0], w[1]) == (w[2], w[3]):
            continue
        ln = LineString([(w[0], w[1]), (w[2], w[3])])
        if ln.intersects(inner):
            cuts.append(ln.buffer(cut_mm, cap_style=2))
    for hatch in hatches or ():
        outer, hh = hatch[0], hatch[1]
        xs = [p[0] for p in outer]
        ys = [p[1] for p in outer]
        if max(xs) < b[0] or min(xs) > b[2] or max(ys) < b[1] or min(ys) > b[3]:
            continue
        try:
            hp = Polygon(outer, [h for h in hh if len(h) >= 3])
            if not hp.is_valid:
                hp = hp.buffer(0)
        except (ValueError, TypeError):
            continue
        if hp.intersects(inner) and hp.intersection(inner).area > 1e4:
            cuts.append(hp)
    if not cuts:
        return poly, holes, bbox, False
    left = g.difference(unary_union(cuts))
    parts = [q for q in getattr(left, "geoms", [left]) if q.geom_type == "Polygon" and q.area > 1e4]
    if not parts:
        return poly, holes, bbox, False
    pt = Point(*label_xy) if label_xy else None
    own = [q for q in parts if pt is not None and q.buffer(5.0).contains(pt)]
    keep = own[0] if own else max(parts, key=lambda q: q.area)
    keep = keep.simplify(2.0, preserve_topology=True)
    if abs(keep.area - g.area) < 0.002 * g.area:
        return poly, holes, bbox, False          # only the 1 mm of a wall it runs along
    return ([(float(x), float(y)) for x, y in list(keep.exterior.coords)[:-1]],
            [[(float(x), float(y)) for x, y in list(r.coords)[:-1]] for r in keep.interiors],
            tuple(float(v) for v in keep.bounds), True)


# -- Shafts: what an X has to stand in ------------------------------------
def _collect_stair_marks(doc, msp, units, words=None):
    """The UP / DOWN / DN marks of the flights, blocks included: [(x, y)]
    in mm. Evidence for a staircase (rule rooms_from_spaces), never a
    label - the extraction leaves them out of the texts. words: the
    Staircase family's evidence words (family_rules.json)."""
    if words is None:
        words = []
        for fam in _FAMILY_RULES:
            if fam.get("name") == "Staircase":
                words = list(fam.get("evidence") or [])
    words = [w for w in (words or ("UP", "DOWN", "DN")) if w]
    if not words:
        return []
    rx = re.compile(r"^\s*(%s)\b" % "|".join(re.escape(w) for w in words), re.IGNORECASE)
    out = []
    for e, xf, _d in iter_entities(doc, msp, "TEXT MTEXT"):
        try:
            t = e.dxf.text if e.dxftype() == "TEXT" else e.plain_text()
            if not rx.match(str(t or "")):
                continue
            p = e.dxf.insert
            x, y = units.point_to_mm(*xf.apply(p[0], p[1]))
            out.append((float(x), float(y)))
        except Exception:                             # noqa: BLE001
            continue
    return out


def _collect_circles(doc, msp, units):
    """Every circle in the drawing, blocks included: (cx, cy, r) in mm."""
    out = []
    for e, xf, _d in iter_entities(doc, msp, "CIRCLE"):
        try:
            c = e.dxf.center
            cx, cy = units.point_to_mm(*xf.apply(c[0], c[1]))
            out.append((cx, cy, float(e.dxf.radius) * units.mm_per_unit * (xf.scale_avg or 1.0)))
        except Exception:                             # noqa: BLE001
            continue
    return out


def x_in_circle(bbox, circles):
    """A cross drawn inside a circle of about its own size - a round
    cut-out or sleeve (a 400 mm circle with its two 400 mm diameters at
    45 degrees). A shaft is never round."""
    if not circles:
        return False
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    m = max(w, h)
    if m <= 0:
        return False
    cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
    for x, y, r in circles:
        if (abs(x - cx) <= 0.25 * m and abs(y - cy) <= 0.25 * m
                and 0.55 * m <= r <= 0.95 * m):
            return True
    return False


def shaft_side_on_wall(bbox, poly, walls, tol=50.0, share=0.5, angle_deg=5.0):
    """Whether at least one side of a shaft runs along a wall (a wall face,
    structure, glazing/facade or the plate edge) for at least `share` of
    its length, within `tol` mm. A shaft stands against a wall; a cross
    standing loose in a room is a symbol, not a shaft."""
    if not walls:
        return False
    pts = [tuple(p) for p in poly] if poly and len(poly) >= 3 else [
        (bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[2], bbox[3]), (bbox[0], bbox[3])]
    x0, y0, x1, y1 = bbox
    near = [w for w in walls
            if max(w[0], w[2]) >= x0 - tol and min(w[0], w[2]) <= x1 + tol
            and max(w[1], w[3]) >= y0 - tol and min(w[1], w[3]) <= y1 + tol]
    if not near:
        return False
    cos_tol = math.cos(math.radians(angle_deg))
    for i in range(len(pts)):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % len(pts)]
        L = math.hypot(bx - ax, by - ay)
        if L < 100.0:
            continue
        ux, uy = (bx - ax) / L, (by - ay) / L
        covered = []
        for w in near:
            wl = math.hypot(w[2] - w[0], w[3] - w[1])
            if wl <= 0:
                continue
            wx, wy = (w[2] - w[0]) / wl, (w[3] - w[1]) / wl
            if abs(ux * wx + uy * wy) < cos_tol:
                continue
            # both ends of the wall line within tol of the side's line
            d1 = abs((w[0] - ax) * uy - (w[1] - ay) * ux)
            d2 = abs((w[2] - ax) * uy - (w[3] - ay) * ux)
            if d1 > tol or d2 > tol:
                continue
            t1 = (w[0] - ax) * ux + (w[1] - ay) * uy
            t2 = (w[2] - ax) * ux + (w[3] - ay) * uy
            lo, hi = max(0.0, min(t1, t2)), min(L, max(t1, t2))
            if hi > lo:
                covered.append((lo, hi))
        if not covered:
            continue
        covered.sort()
        total, cur_lo, cur_hi = 0.0, covered[0][0], covered[0][1]
        for lo, hi in covered[1:]:
            if lo <= cur_hi:
                cur_hi = max(cur_hi, hi)
            else:
                total += cur_hi - cur_lo
                cur_lo, cur_hi = lo, hi
        total += cur_hi - cur_lo
        if total >= share * L:
            return True
    return False


def _is_diagonal(angle_deg, tol=5.0):
    """Neither horizontal nor vertical, within tol degrees."""
    a = angle_deg % 90.0
    return tol < a < 90.0 - tol


def _free_ended_diagonals(cand, segs, axes, idx, reach=60.0):
    """Of the diagonal wall faces in cand, those with an end that touches
    no other line (a line parallel to it - its own partner - does not
    count)."""
    if not cand:
        return set()
    import numpy as np
    pool = np.array([segs[j][:4] for j in idx], dtype=np.float64)
    ang = np.array([axes[j][0] for j in idx], dtype=np.float64)
    ids = np.array(idx)
    ax, ay = pool[:, 0], pool[:, 1]
    dx, dy = pool[:, 2] - ax, pool[:, 3] - ay
    ll = np.maximum(dx * dx + dy * dy, 1e-9)
    out = set()
    for i in cand:
        diff = np.abs((ang - axes[i][0] + 90.0) % 180.0 - 90.0)
        other = (ids != i) & (diff > 3.0)
        s = segs[i]
        for px, py in ((s[0], s[1]), (s[2], s[3])):
            t = np.clip(((px - ax) * dx + (py - ay) * dy) / ll, 0.0, 1.0)
            dist = np.hypot(ax + t * dx - px, ay + t * dy - py)
            if not np.any(other & (dist <= reach)):
                out.add(i)
                break
    return out


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


# A window: a gap in a wall with glazing or a louvre drawn across it
# (window_openings). Evidence is a line on a glazing / window / louvre layer
# or in a window / louvre block - BHUB's louvres are a "LOVERS-2 DETAIL"
# block on a mechanical layer, XA's windows A-GLAZ lines in an A-GLAZ-FRAM
# frame between A-WALL piers.
_WO_W = CONFIG.get("windows") or {}
WINDOW_EVIDENCE_LAYER_RE = re.compile(str(_WO_W.get(
    "evidence_layer_regex", r"GLAZ|WINDOW|(^|[^A-Z])WIN([^A-Z]|$)|WDW|WNDW|LOUV|LOVER|CURTAIN|MULLION")), re.I)
# not window evidence: glass partitions and mirrors (glass, not windows - see
# GLASS_*), the area-plan overlay ("2.1 TSIIC-BUA (exc glass)")
WINDOW_NOT_EVIDENCE_RE = re.compile(str(_WO_W.get(
    "not_evidence_regex", r"PARTITION|(^|[^A-Z])PART([^A-Z]|$)|MIRROR|^\d+(\.\d+)+\s")), re.I)
WINDOW_EVIDENCE_BLOCK_RE = re.compile(str(_WO_W.get(
    "evidence_block_regex", r"WINDOW|WDW|WNDW|LOUV|LOVER|GLAZ|(^|[^A-Z])W-?\d")), re.I)
WINDOW_GAP_MM = tuple(float(v) for v in (_WO_W.get("gap_mm") or [250, 6000]))
WINDOW_COVER = float(_WO_W.get("evidence_cover_share", 0.6))
WINDOW_GAPS = bool(_WO_W.get("gap_windows", True))


def _collect_window_evidence(doc, msp, units=None):
    """Glazing and louvre lines, in mm: [(x1, y1, x2, y2)] - a line on a
    glazing / window / louvre layer or in such a block, not a door's and not
    drawing furniture (see WINDOW_EVIDENCE_LAYER_RE)."""
    units = units or MM_UNITS
    out = []
    for e, xf, _d in iter_entities(doc, msp, "LINE LWPOLYLINE POLYLINE"):
        try:
            layer = effective_layer(e, xf)
            last = str(layer or "").split("$")[-1]
            blocks = [b or "" for b in xf.blocks]
            if _is_annotation(layer, blocks) or _is_door_leaf(layer, blocks):
                continue
            if WINDOW_NOT_EVIDENCE_RE.search(last) or any(WINDOW_NOT_EVIDENCE_RE.search(b.split("$")[-1]) for b in blocks):
                continue
            if not (WINDOW_EVIDENCE_LAYER_RE.search(last)
                    or any(WINDOW_EVIDENCE_BLOCK_RE.search(b.split("$")[-1]) for b in blocks)):
                continue
            if e.dxftype() == "LINE":
                a = units.point_to_mm(*xf.apply(e.dxf.start.x, e.dxf.start.y, e.dxf.start.z))
                b = units.point_to_mm(*xf.apply(e.dxf.end.x, e.dxf.end.y, e.dxf.end.z))
                pts = [a, b]
            else:
                pts = _polyline_points(e, units, xf)
                if _is_closed(e) and len(pts) > 1:
                    pts = pts + [pts[0]]
            for i in range(len(pts) - 1):
                if math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) >= 5.0:
                    out.append((pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]))
        except Exception:                             # noqa: BLE001
            continue
    return out


class _WallGaps:
    """The ends of the walls and the faces across them, for finding the gaps
    a door or a window fills (door_openings, window_openings).

    caps: short wall-layer lines across a wall (60-450 mm) and the ends of
    traced strips; pool: every wall face (wall / structure layer lines and
    strip edges); W: the traced strips. A filled-in wall hatch ("hatch")
    is not a traced wall here."""

    def __init__(self, segs, strips):
        from shapely.geometry import box, Polygon
        from shapely.strtree import STRtree
        from . import walltrace as _wt
        W, pool, caps = [], [], []
        for st in strips or ():
            if st.get("verdict") == "hatch":
                continue
            try:
                P = [tuple(map(float, p[:2])) for p in st["pts"]]
                g = Polygon(P).buffer(0)
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if g.is_empty or len(P) != 4:
                continue
            W.append(g)
            for a, b in zip(P, P[1:] + P[:1]):
                pool.append((a[0], a[1], b[0], b[1]))
            T = math.hypot(P[2][0] - P[1][0], P[2][1] - P[1][1])
            if 60.0 <= T <= 450.0 and st.get("verdict") != "junction":
                caps.append((P[0][0], P[0][1], P[3][0], P[3][1]))
                caps.append((P[1][0], P[1][1], P[2][0], P[2][1]))
        segs = [s for s in segs or () if math.hypot(s[2] - s[0], s[3] - s[1]) >= 5.0]
        for s in segs:
            if (len(s) > 7 and s[7]) or (len(s) > 8 and s[8]):
                continue
            if _wt.is_wall_layer(s[4]) or _wt.is_structure_layer(s[4]):
                pool.append(tuple(s[:4]))
                if 60.0 <= math.hypot(s[2] - s[0], s[3] - s[1]) <= 450.0:
                    caps.append(tuple(s[:4]))

        def bb(q):
            return box(min(q[0], q[2]) - .01, min(q[1], q[3]) - .01, max(q[0], q[2]) + .01, max(q[1], q[3]) + .01)
        self.W, self.pool, self.caps, self.segs = W, pool, caps, segs
        self.bb = bb
        self.wtree = STRtree(W) if W else None
        self.ptree = STRtree([bb(q) for q in pool]) if pool else None
        self.ctree = STRtree([bb(q) for q in caps]) if caps else None
        self.stree = STRtree([bb(q) for q in segs]) if segs else None

    def wall_area(self, g):
        if self.wtree is None:
            return 0.0
        return sum(g.intersection(self.W[int(k)]).area for k in self.wtree.query(g))

    @staticmethod
    def parallel(s, ux, uy, tol):
        sl = math.hypot(s[2] - s[0], s[3] - s[1])
        return sl >= 1.0 and abs((s[2] - s[0]) / sl * uy - (s[3] - s[1]) / sl * ux) <= tol

    def backed(self, f, c):
        """The wall behind a cap: its pieces, or its two faces running on."""
        zone = _door_frame_rect(f, -60, -5, 0.15 * c, 0.85 * c)
        if self.wall_area(zone) >= 0.5 * zone.area:
            return True
        lo_ok = hi_ok = False
        for k in (self.ptree.query(_door_frame_rect(f, -60, -5, -15, c + 15)) if self.ptree is not None else ()):
            ob = self.pool[int(k)]
            if not self.parallel(ob, f[2], f[3], 0.05):
                continue
            p0, p1 = _door_frame_pt(f, ob[0], ob[1]), _door_frame_pt(f, ob[2], ob[3])
            if min(-5.0, max(p0[0], p1[0])) - max(-60.0, min(p0[0], p1[0])) < 27.5:
                continue
            t = (p0[1] + p1[1]) / 2.0
            lo_ok = lo_ok or abs(t) <= 15
            hi_ok = hi_ok or abs(t - c) <= 15
        return lo_ok and hi_ok

    def gaps_from(self, cp, dmax):
        """The gaps opening off one cap: [(frame, gap length, cap length)] -
        each side of it that is open (no wall in its first 150 mm) and has
        the wall behind it, run on to the first wall face across its band
        (within dmax)."""
        out = []
        c = math.hypot(cp[2] - cp[0], cp[3] - cp[1])
        if c < 1.0 or self.ptree is None:
            return out
        mx, my = (cp[2] - cp[0]) / c, (cp[3] - cp[1]) / c
        for sg in (1.0, -1.0):
            f = (cp[0], cp[1], -my * sg, mx * sg, mx, my)
            first = _door_frame_rect(f, 5, 150, 0.15 * c, 0.85 * c)
            if self.wall_area(first) > 0.3 * first.area or not self.backed(f, c):
                continue
            g = None
            for k in self.ptree.query(_door_frame_rect(f, 20, dmax, 0.15 * c, 0.85 * c)):
                ob = self.pool[int(k)]
                if not self.parallel(ob, mx, my, 0.05):
                    continue
                p0, p1 = _door_frame_pt(f, ob[0], ob[1]), _door_frame_pt(f, ob[2], ob[3])
                sm = (p0[0] + p1[0]) / 2.0
                if sm < 20 or sm > dmax:
                    continue
                if min(c, max(p0[1], p1[1])) - max(0.0, min(p0[1], p1[1])) < 0.6 * c:
                    continue
                g = sm if g is None else min(g, sm)
            if g is not None:
                out.append((f, g, c))
        return out


def window_openings(evidence, segs, strips, avoid=()):
    """The windows in the walls, as quads (point lists, mm), drawn on
    AI_WINDOW - a gap in a wall (from a wall's end to the next face across
    its band, WINDOW_GAP_MM long) with glazing or a louvre drawn across it:
    evidence lines (_collect_window_evidence) along the gap covering
    WINDOW_COVER of it. Across, the window is as deep as the evidence drawn
    in the gap (a frame: XA's windows are their 109 mm frames), else the
    wall. A gap with a door in it (avoid: the door openings) is a door.
    BHUB 1st floor: the louvres in the facade wall; XA: the windows between
    the facade piers."""
    from shapely.geometry import Polygon, box
    from shapely.strtree import STRtree
    if not WINDOW_GAPS or not evidence:
        return []
    ev = [e for e in evidence if math.hypot(e[2] - e[0], e[3] - e[1]) >= 5.0]
    if not ev:
        return []
    idx = _WallGaps(segs, strips)
    if idx.ctree is None:
        return []
    etree = STRtree([idx.bb(e) for e in ev])
    av = [Polygon(q).buffer(0) for q in avoid or () if len(q) >= 3]
    found = []
    seen_caps = set()
    for ci in range(len(idx.caps)):
        cp = idx.caps[ci]
        key = tuple(round(v) for v in cp)
        if key in seen_caps:
            continue
        seen_caps.add(key)
        # only caps with evidence within reach
        cx, cy = (cp[0] + cp[2]) / 2.0, (cp[1] + cp[3]) / 2.0
        if not len(etree.query(box(cx - 400, cy - 400, cx + 400, cy + 400))):
            continue
        for f, g, c in idx.gaps_from(cp, WINDOW_GAP_MM[1]):
            if g < WINDOW_GAP_MM[0]:
                continue
            R = _door_frame_rect(f, 0, g, -5, c + 5)
            if idx.wall_area(R) > 0.3 * R.area:
                continue
            ivs, offs = [], []
            for k in etree.query(R):
                e = ev[int(k)]
                p0, p1 = _door_frame_pt(f, e[0], e[1]), _door_frame_pt(f, e[2], e[3])
                lo, hi = max(0.0, min(p0[0], p1[0])), min(g, max(p0[0], p1[0]))
                if _WallGaps.parallel(e, f[2], f[3], 0.05):
                    v = (p0[1] + p1[1]) / 2.0
                    if -5 <= v <= c + 5 and hi - lo >= 10:
                        ivs.append((lo, hi))
                        offs.append(min(max(v, 0.0), c))
                elif _WallGaps.parallel(e, f[4], f[5], 0.05) and 0 <= (p0[0] + p1[0]) / 2.0 <= g:
                    for v in (p0[1], p1[1]):
                        offs.append(min(max(v, 0.0), c))
            if not ivs:
                continue
            ivs.sort()
            cover, cur = 0.0, None
            for lo, hi in ivs:
                if cur is None or lo > cur[1]:
                    if cur is not None:
                        cover += cur[1] - cur[0]
                    cur = [lo, hi]
                else:
                    cur[1] = max(cur[1], hi)
            cover += cur[1] - cur[0]
            if cover < WINDOW_COVER * g:
                continue
            t0, t1 = (min(offs), max(offs)) if offs and max(offs) - min(offs) >= 20 else (0.0, c)
            q = [_door_frame_xy(f, 0, t0), _door_frame_xy(f, g, t0), _door_frame_xy(f, g, t1), _door_frame_xy(f, 0, t1)]
            pg = Polygon(q).buffer(0)
            if pg.is_empty or any(pg.intersection(a).area > 0.2 * pg.area for a in av):
                continue
            if any(pg.intersection(o).area > 0.5 * min(pg.area, o.area) for _q, o in found):
                continue
            found.append((q, pg))
    return [[(float(x), float(y)) for x, y in q] for q, _pg in found]


def _collect_door_arcs(doc, msp, units=None):
    """Door swings, in mm: [(hx, hy, r, (ax, ay), (bx, by), sweep)] - every
    ARC of a door leaf's radius (DOOR_ARC_R_MIN-MAX) sweeping DOOR_SWEEP
    degrees, on any layer: the shaft doors of BHUB and XA are drawn on the
    shaft's layer (EL-SHAFT, ELEC SPATIALS) in the shaft's block, and their
    swings stop part-open (27-58 degrees). Whether it is a door is decided
    where it stands (door_openings): at the gap in a wall, or with its leaf
    drawn open from the hinge.

    An ELLIPSE drawn as a door swing is read the same way (285 FD and the
    P285 11th floor draw their door blocks' swings as ellipses: 139 room
    exits were missed): one that is a circle's arc - its points within
    DOOR_ELLIPSE_ROUND of one radius from its centre, the hinge."""
    units = units or MM_UNITS
    out = []
    if DOOR_ELLIPSES:
        out.extend(_collect_door_ellipses(doc, msp, units))
    for e, xf, _d in iter_entities(doc, msp, "ARC"):
        try:
            if _is_annotation(effective_layer(e, xf), xf.blocks):
                continue
            r = float(e.dxf.radius) * units.mm_per_unit * (xf.scale_avg or 1.0)
            if not (DOOR_ARC_R_MIN <= r <= DOOR_ARC_R_MAX):
                continue
            sweep = (float(e.dxf.end_angle) - float(e.dxf.start_angle)) % 360.0
            if not (DOOR_SWEEP[0] <= sweep <= DOOR_SWEEP[1]):
                continue
            c, rr = e.dxf.center, float(e.dxf.radius)
            ocs = e.ocs()

            def world(px, py):
                w = ocs.to_wcs((px, py, c.z))
                return units.point_to_mm(*xf.apply(w.x, w.y, w.z))
            sa, ea = math.radians(float(e.dxf.start_angle)), math.radians(float(e.dxf.end_angle))
            h = world(c.x, c.y)
            a = world(c.x + rr * math.cos(sa), c.y + rr * math.sin(sa))
            b = world(c.x + rr * math.cos(ea), c.y + rr * math.sin(ea))
            out.append((h[0], h[1], r, a, b, sweep))
        except Exception:                             # noqa: BLE001
            continue
    return out


def _collect_door_ellipses(doc, msp, units):
    """The door swings drawn as ELLIPSE (see _collect_door_arcs): an
    elliptical arc of a door leaf's radius and sweep whose points all stand
    within DOOR_ELLIPSE_ROUND of one radius from its centre - a circle's
    arc, the leaf turning on its hinge. The same tuples as the arcs."""
    from ezdxf.path import make_path
    out = []
    for e, xf, _d in iter_entities(doc, msp, "ELLIPSE"):
        try:
            if _is_annotation(effective_layer(e, xf), xf.blocks):
                continue
            sweep = math.degrees((float(e.dxf.end_param) - float(e.dxf.start_param)) % (2.0 * math.pi))
            if not (DOOR_SWEEP[0] <= sweep <= DOOR_SWEEP[1]):
                continue
            c = e.dxf.center
            h = units.point_to_mm(*xf.apply(c.x, c.y, c.z))
            pts = [units.point_to_mm(*xf.apply(v.x, v.y, v.z)) for v in make_path(e).flattening(5.0)]
            if len(pts) < 3:
                continue
            rs = [math.hypot(p[0] - h[0], p[1] - h[1]) for p in pts]
            r = sum(rs) / len(rs)
            if not (DOOR_ARC_R_MIN <= r <= DOOR_ARC_R_MAX) or max(rs) - min(rs) > DOOR_ELLIPSE_ROUND * r:
                continue
            out.append((h[0], h[1], r, tuple(pts[0]), tuple(pts[-1]), sweep))
        except Exception:                             # noqa: BLE001
            continue
    return out


def _door_frame_pt(f, x, y):
    """(along, across) of a point in a local frame (ox, oy, ux, uy, nx, ny)."""
    return (x - f[0]) * f[2] + (y - f[1]) * f[3], (x - f[0]) * f[4] + (y - f[1]) * f[5]


def _door_frame_xy(f, s, t):
    return (f[0] + f[2] * s + f[4] * t, f[1] + f[3] * s + f[5] * t)


def _door_frame_rect(f, s0, s1, t0, t1):
    from shapely.geometry import Polygon
    return Polygon([_door_frame_xy(f, s0, t0), _door_frame_xy(f, s1, t0),
                    _door_frame_xy(f, s1, t1), _door_frame_xy(f, s0, t1)])


def door_openings(arcs, segs, strips, evidence=True, rejected=None):
    """The door openings, as quads (point lists, mm), drawn on AI_DOOR.

    evidence (rule doors_need_evidence): what the swings found is checked
    against the walls last (_door_evidence) - an opening along a wall face
    is no door, a doubtful one is left out and listed in `rejected` with
    "review": True (drawn on AI_REVIEW), two in one gap are one.

    A door in a wall: the gap between the end of a wall - a cap, a short
    wall-layer line across it or the end of a traced strip, with the wall
    behind it (its pieces, or its two faces running on) - and the first
    wall face across its band beyond, DOOR_ARC fitting the gap (a single
    leaf 0.55-1.35 of its radius; a pair 1.6-2.5, with a second swing at
    the other jamb). The swing's centre stands within DOOR_JAMB_REACH_MM of
    a jamb and one of its ends reaches the gap (within 200 mm). The gap is
    as long as the wall end to that face; across it is as deep as the
    lines drawn in it along the wall - a frame, a shaft door's 76 mm frame
    on XA, a finish line - lines running on past both jambs (an area or
    floor outline) not counted; with none, the wall's thickness.

    A door with no wall gap - a WC cubicle or a shaft door in a 12 mm board
    - is taken from a full swing (75+ degrees) whose leaf is drawn open: a
    line from the centre to one end. The other end is where it shuts; the
    opening runs from the hinge to there, squared to the line it shuts on,
    as deep as the lines along it (the board's faces on either side, the
    frame).

    A dynamic door block flattened with all its states (XA *U blocks: six
    swings of 652-1101 mm on one hinge) is one door: any of its radii can
    fit the gap, and with no gap it is taken only where it shuts from one
    wall-line end to another (shut_between_ends) - as is a full swing with
    no open leaf drawn. arcs: _collect_door_arcs;
    segs: the floor's raw segments (_collect_raw_segs); strips: the wall
    strips ({"pts": [4 points]}, walltrace or the engine)."""
    from shapely.geometry import Point, LineString, box
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
    from . import walltrace as _wt
    arcs = list(arcs or ())
    if not arcs:
        found = _leaf_door_quads(segs, strips)
        return _door_evidence(found, strips, rejected) if evidence else found
    W, pool, caps = [], [], []
    for st in strips or ():
        if st.get("verdict") == "hatch":
            continue                                  # wall hatch filled in, not a traced wall
        try:
            P = [tuple(map(float, p[:2])) for p in st["pts"]]
            from shapely.geometry import Polygon
            g = Polygon(P).buffer(0)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if g.is_empty or len(P) != 4:
            continue
        W.append(g)
        for a, b in zip(P, P[1:] + P[:1]):
            pool.append((a[0], a[1], b[0], b[1]))
        T = math.hypot(P[2][0] - P[1][0], P[2][1] - P[1][1])
        if 60.0 <= T <= 450.0 and st.get("verdict") != "junction":
            caps.append((P[0][0], P[0][1], P[3][0], P[3][1]))
            caps.append((P[1][0], P[1][1], P[2][0], P[2][1]))
    segs = [s for s in segs or () if math.hypot(s[2] - s[0], s[3] - s[1]) >= 5.0]
    for s in segs:
        if (len(s) > 7 and s[7]) or (len(s) > 8 and s[8]):
            continue
        if _wt.is_wall_layer(s[4]) or _wt.is_structure_layer(s[4]):
            pool.append(tuple(s[:4]))
            if 60.0 <= math.hypot(s[2] - s[0], s[3] - s[1]) <= 450.0:
                caps.append(tuple(s[:4]))

    def bb(s):
        return box(min(s[0], s[2]) - .01, min(s[1], s[3]) - .01, max(s[0], s[2]) + .01, max(s[1], s[3]) + .01)
    wtree = STRtree(W) if W else None
    ptree = STRtree([bb(s) for s in pool]) if pool else None
    ctree = STRtree([bb(s) for s in caps]) if caps else None
    stree = STRtree([bb(s) for s in segs]) if segs else None

    def wall_area(g):
        if wtree is None:
            return 0.0
        return sum(g.intersection(W[int(k)]).area for k in wtree.query(g))

    def parallel(s, ux, uy, tol):
        sl = math.hypot(s[2] - s[0], s[3] - s[1])
        return sl >= 1.0 and abs((s[2] - s[0]) / sl * uy - (s[3] - s[1]) / sl * ux) <= tol

    def backed(f, c):
        """The wall behind a cap: its pieces, or its two faces running on."""
        zone = _door_frame_rect(f, -60, -5, 0.15 * c, 0.85 * c)
        if wall_area(zone) >= 0.5 * zone.area:
            return True
        lo_ok = hi_ok = False
        for k in ptree.query(_door_frame_rect(f, -60, -5, -15, c + 15)):
            ob = pool[int(k)]
            if not parallel(ob, f[2], f[3], 0.05):
                continue
            p0, p1 = _door_frame_pt(f, ob[0], ob[1]), _door_frame_pt(f, ob[2], ob[3])
            if min(-5.0, max(p0[0], p1[0])) - max(-60.0, min(p0[0], p1[0])) < 27.5:
                continue
            t = (p0[1] + p1[1]) / 2.0
            lo_ok = lo_ok or abs(t) <= 15
            hi_ok = hi_ok or abs(t - c) <= 15
        return lo_ok and hi_ok

    reach = DOOR_JAMB_REACH_MM
    quads, later = [], []
    for (hx, hy, r, A, B, sweep) in arcs:
        H = Point(hx, hy)
        fan = sorted({round(q[2]) for q in arcs if math.hypot(q[0] - hx, q[1] - hy) < 100} | {round(r)})
        rmax = float(max(fan))
        best = None
        for ci in (ctree.query(H.buffer(2.5 * rmax + 400)) if ctree is not None else ()):
            cp = caps[int(ci)]
            if LineString([cp[:2], cp[2:4]]).distance(H) > 2.5 * rmax + 400:
                continue
            c = math.hypot(cp[2] - cp[0], cp[3] - cp[1])
            mx, my = (cp[2] - cp[0]) / c, (cp[3] - cp[1]) / c
            for sg in (1.0, -1.0):
                f = (cp[0], cp[1], -my * sg, mx * sg, mx, my)
                first = _door_frame_rect(f, 5, 150, 0.15 * c, 0.85 * c)
                if wall_area(first) > 0.3 * first.area or not backed(f, c):
                    continue                          # into the wall, or not a wall's end
                D = 2.5 * rmax + 300
                g = None
                for k in ptree.query(_door_frame_rect(f, 20, D, 0.15 * c, 0.85 * c)):
                    ob = pool[int(k)]
                    if not parallel(ob, mx, my, 0.05):
                        continue
                    p0, p1 = _door_frame_pt(f, ob[0], ob[1]), _door_frame_pt(f, ob[2], ob[3])
                    sm = (p0[0] + p1[0]) / 2.0
                    if sm < 20 or sm > D:
                        continue
                    if min(c, max(p0[1], p1[1])) - max(0.0, min(p0[1], p1[1])) < 0.6 * c:
                        continue
                    g = sm if g is None else min(g, sm)
                if g is None:
                    continue
                single = [rr for rr in fan if 0.55 * rr <= g <= 1.35 * rr]
                double = [rr for rr in fan if 1.6 * rr <= g <= 2.5 * rr]
                if not single and not double:
                    continue
                hs, ht = _door_frame_pt(f, hx, hy)
                if not (-reach <= ht <= c + reach) or not (-reach <= hs <= g + reach) \
                        or min(abs(hs), abs(hs - g)) > reach:
                    continue
                gq = _door_frame_rect(f, 0, g, 0, c)
                if wall_area(gq) > 0.3 * gq.area:
                    continue
                if not single:
                    other = 0.0 if abs(hs) > abs(hs - g) else g
                    rd = double[0]
                    if not any(math.hypot(q[0] - hx, q[1] - hy) >= 100 and 0.65 * rd <= q[2] <= 1.5 * rd
                               and -reach <= _door_frame_pt(f, q[0], q[1])[1] <= c + reach
                               and abs(_door_frame_pt(f, q[0], q[1])[0] - other) <= reach for q in arcs):
                        continue                      # a pair's width with one leaf
                ends = min(gq.distance(Point(*A)), gq.distance(Point(*B)))
                if ends > 200:
                    continue                          # the swing does not reach the gap
                score = (round(ends / 50.0), round(gq.distance(H) / 50.0),
                         min(abs(g - rr) for rr in single) if single else min(abs(g - 2 * rr) for rr in double))
                if best is None or score < best[0]:
                    best = (score, f, g, c)
        if best is None:
            later.append((hx, hy, r, A, B, sweep, len(fan) > 1))
            continue
        _s, f, g, T = best
        offs = []
        for k in (stree.query(_door_frame_rect(f, 0, g, -5, T + 5)) if stree is not None else ()):
            s = segs[int(k)]
            if not parallel(s, f[2], f[3], 0.05):
                continue
            p0, p1 = _door_frame_pt(f, s[0], s[1]), _door_frame_pt(f, s[2], s[3])
            if min(p0[0], p1[0]) < -100 and max(p0[0], p1[0]) > g + 100:
                continue                              # runs on past both jambs: a face or area line
            lo, hi = max(0.0, min(p0[0], p1[0])), min(g, max(p0[0], p1[0]))
            v = (p0[1] + p1[1]) / 2.0
            if hi - lo >= 10 and -5 <= v <= T + 5:
                offs.append(min(max(v, 0.0), T))
        t0, t1 = (min(offs), max(offs)) if offs and max(offs) - min(offs) >= 20 else (0.0, T)
        quads.append(([_door_frame_xy(f, 0, t0), _door_frame_xy(f, g, t0),
                       _door_frame_xy(f, g, t1), _door_frame_xy(f, 0, t1)], True))
    from shapely.geometry import Polygon
    gaps = [Polygon(q).buffer(0) for q, _g in quads]

    def shut_between_ends(hx, hy, fan_arcs):
        """A door that shuts from one wall-line end to another: the hinge
        within 60 mm of the end of a wall line, one end of a swing within
        100 mm of the end of another, the two that far apart (0.75-1.3 of
        the radius, 400 mm or more) and both lines returns - standing at an
        angle to the opening, not running on along it as a board does. A
        shaft box drawn in single lines with its door block flattened with
        two states (XA typical floor: PHE SH 620 x 500, swings 476 and 570
        mm) has no wall gap and no open leaf to read. The quad is
        DOOR_SHUT_DEPTH_MM deep across the opening, or None."""
        if ptree is None:
            return None
        rm = max(q[2] for q in fan_arcs)
        near = [pool[int(k)] for k in ptree.query(Point(hx, hy).buffer(1.4 * rm + 100))]

        def ends_of(sg):
            return (((sg[0], sg[1]), (sg[2], sg[3])), ((sg[2], sg[3]), (sg[0], sg[1])))
        jambs = [(e, o, sg) for sg in near for e, o in ends_of(sg)
                 if math.hypot(e[0] - hx, e[1] - hy) <= 60.0]
        best = None
        for e1, o1, s1 in jambs:
            for q in fan_arcs:
                for S in (q[3], q[4]):
                    for s2 in near:
                        if s2 is s1:
                            continue
                        for e2, o2 in ends_of(s2):
                            dd = math.hypot(e2[0] - S[0], e2[1] - S[1])
                            if dd > 100.0:
                                continue
                            d = math.hypot(e2[0] - e1[0], e2[1] - e1[1])
                            if d < 400.0 or not (0.75 * q[2] <= d <= 1.3 * q[2]):
                                continue
                            cx, cy = (e2[0] - e1[0]) / d, (e2[1] - e1[1]) / d
                            ok = True
                            for e, o in ((e1, o1), (e2, o2)):
                                L = math.hypot(o[0] - e[0], o[1] - e[1])
                                if L < 100.0 or abs((o[0] - e[0]) / L * cx + (o[1] - e[1]) / L * cy) > 0.5:
                                    ok = False
                            if ok and (best is None or dd < best[0]):
                                best = (dd, e1, e2)
        if best is None:
            return None
        _dd, e1, e2 = best
        d = math.hypot(e2[0] - e1[0], e2[1] - e1[1])
        nx, ny = -(e2[1] - e1[1]) / d, (e2[0] - e1[0]) / d
        h = DOOR_SHUT_DEPTH_MM / 2.0
        return [(e1[0] + nx * h, e1[1] + ny * h), (e2[0] + nx * h, e2[1] + ny * h),
                (e2[0] - nx * h, e2[1] - ny * h), (e1[0] - nx * h, e1[1] - ny * h)]

    def add_shut(hx, hy, fan_arcs):
        qd = shut_between_ends(hx, hy, fan_arcs)
        if qd is None:
            return False
        quads.append((qd, False))
        gaps.append(Polygon(qd).buffer(0))
        return True

    for (hx, hy, r, A, B, sweep, stacked) in later:
        H = Point(hx, hy)
        if sweep < 75 or stree is None:
            continue                                  # part-open
        if any(q.distance(H) < 300 for q in gaps):
            continue                                  # another state of a door found in its wall
        if stacked:                                   # a block's door states: only between two wall ends
            add_shut(hx, hy, [q for q in arcs if math.hypot(q[0] - hx, q[1] - hy) < 100])
            continue
        opens = set()
        for k in stree.query(H.buffer(60)):
            s = segs[int(k)]
            for p, q in (((s[0], s[1]), (s[2], s[3])), ((s[2], s[3]), (s[0], s[1]))):
                if math.hypot(p[0] - hx, p[1] - hy) <= 40:
                    for ei, end in enumerate((A, B)):
                        if math.hypot(q[0] - end[0], q[1] - end[1]) <= 60:
                            opens.add(ei)
        cands = []
        for ei in opens:                              # the leaf drawn open: the other end shuts
            S = (B, A)[ei]
            L = math.hypot(S[0] - hx, S[1] - hy)
            if L < 1:
                continue
            ux, uy = (S[0] - hx) / L, (S[1] - hy) / L
            f = (hx, hy, ux, uy, -uy, ux)
            zone = _door_frame_rect(f, -200, L + 200, -150, 150)
            longest = None                            # square it to the line it shuts on
            for k in stree.query(zone):
                s = segs[int(k)]
                sl = math.hypot(s[2] - s[0], s[3] - s[1])
                cr = abs((s[2] - s[0]) / sl * uy - (s[3] - s[1]) / sl * ux)
                if sl >= 100 and 0.0015 <= cr <= 0.1 and (longest is None or sl > longest[0]):
                    longest = (sl, (s[2] - s[0]) / sl, (s[3] - s[1]) / sl)
            if longest is not None:
                vx, vy = longest[1], longest[2]
                if vx * ux + vy * uy < 0:
                    vx, vy = -vx, -vy
                ux, uy = vx, vy
                L = (S[0] - hx) * ux + (S[1] - hy) * uy
                f = (hx, hy, ux, uy, -uy, ux)
            offs, a0, a1 = [], 0.0, L
            for k in stree.query(zone):
                s = segs[int(k)]
                if not parallel(s, ux, uy, 0.05):
                    continue
                p0, p1 = _door_frame_pt(f, s[0], s[1]), _door_frame_pt(f, s[2], s[3])
                v = (p0[1] + p1[1]) / 2.0
                if abs(v) > 150:
                    continue
                lo, hi = min(p0[0], p1[0]), max(p0[0], p1[0])
                if min(hi, L) - max(lo, 0.0) >= 0.7 * L and lo >= -150 and hi <= L + 150:
                    offs.append(v)                    # across the opening: frame, shut leaf
                    a0, a1 = min(a0, lo), max(a1, hi)
                elif (abs(hi) <= 40 and lo < -20) or (abs(lo - L) <= 40 and hi > L + 20):
                    offs.append(v)                    # the board it stands in, ending at a jamb
            cands.append((len(offs), f, offs, a0, a1))
        if not cands:
            add_shut(hx, hy, [(hx, hy, r, A, B, sweep)])
            continue
        _n, f, offs, a0, a1 = max(cands, key=lambda q: q[0])
        if offs and max(offs) - min(offs) >= 5:
            t0, t1 = min(offs), max(offs)
        else:
            c0 = min(offs) if offs else 0.0
            t0, t1 = c0 - DOOR_LEAF_MM / 2.0, c0 + DOOR_LEAF_MM / 2.0
        quads.append(([_door_frame_xy(f, a0, t0), _door_frame_xy(f, a1, t0),
                       _door_frame_xy(f, a1, t1), _door_frame_xy(f, a0, t1)], False))
    # doors drawn by their leaf, shut in the opening (_leaf_door_quads)
    for q in _leaf_door_quads(segs, strips, [Polygon(q_).buffer(0) for q_, _g in quads]):
        quads.append((q, True))
    # one opening per gap; the two leaves of a pair meeting: one opening
    out = []
    for q, _gap in quads:
        g = Polygon(q).buffer(0)
        if g.is_empty:
            continue
        for m in out:
            if m[1].intersects(g) and m[1].intersection(g).area > 0.5 * min(m[1].area, g.area):
                break
            if m[1].distance(g) < 5:
                u = unary_union([m[1], g]).minimum_rotated_rectangle
                if u.area <= 1.08 * (m[1].area + g.area):
                    m[1] = u
                    m[0] = [tuple(p) for p in list(u.exterior.coords)[:-1]]
                    break
        else:
            out.append([q, g])
    found = [[(float(x), float(y)) for x, y in m[0]] for m in out]
    return _door_evidence(found, strips, rejected) if evidence else found


def _door_evidence(quads, strips, rejected=None):
    """Rule doors_need_evidence, on door openings already found from a
    swing or a leaf: what the walls say about each. A door stands in a gap
    in a wall - its long sides carry on the wall's faces, they do not run
    along one. An opening a wall face runs along (DOOR_ALONG_FACE_SHARE of
    its width, within DOOR_FACE_TOL_MM) lies beside the wall and is no
    door; from DOOR_DOUBT_FACE_SHARE it is doubtful and left for review.
    Two openings in one gap are one door: the narrower (the leaf's width)
    is kept (XA 37th floor, male toilet entrance: a 2.05 m "door" over the
    0.8 m one, a 1.27 m one along the wall's face). quads: point lists, mm;
    strips: the wall strips ({"pts"}). Returns the doors kept; rejected (a
    list) gets {"pts", "why", "review"} for each left out."""
    from shapely.geometry import Polygon, LineString
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
    W = []
    for st in strips or ():
        if st.get("verdict") == "hatch":
            continue
        try:
            g = Polygon([tuple(map(float, p[:2])) for p in st["pts"]]).buffer(0)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if not g.is_empty:
            W.append(g)
    wtree = STRtree(W) if W else None
    recs = []
    for q in quads or ():
        try:
            g = Polygon([tuple(p[:2]) for p in q]).buffer(0)
        except (TypeError, ValueError, IndexError):
            continue
        if g.is_empty or g.area <= 0:
            continue
        R = g.minimum_rotated_rectangle
        if R.geom_type != "Polygon":
            recs.append({"q": q, "g": g, "width": 0.0})
            continue
        P = list(R.exterior.coords)[:4]
        E = [(P[i], P[(i + 1) % 4]) for i in range(4)]
        L = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in E]
        k = 0 if L[0] >= L[1] else 1
        w = L[k]
        u = ((E[k][1][0] - E[k][0][0]) / w, (E[k][1][1] - E[k][0][1]) / w) if w > 0 else (1.0, 0.0)
        recs.append({"q": q, "g": g, "width": w, "depth": L[1 - k], "u": u,
                     "sides": (E[k], E[k + 2]), "c": (R.centroid.x, R.centroid.y)})

    def drop(r, why, review=False):
        r["out"] = True
        if rejected is not None:
            rejected.append({"pts": [list(map(float, p[:2])) for p in r["q"]], "why": why, "review": review})
    # 1. beside a wall, not in a gap in one
    for r in recs:
        if wtree is None or not r["width"]:
            continue
        near = [W[int(i)] for i in wtree.query(r["g"].buffer(DOOR_FACE_TOL_MM + 5.0))]
        if not near:
            continue
        wu = unary_union(near)
        if r["g"].intersection(wu).area >= 0.3 * r["g"].area:
            continue                                  # in the wall's line: a gap the strips ran across
        wb = wu.buffer(DOOR_FACE_TOL_MM)
        share = max(LineString(s).intersection(wb).length / r["width"] for s in r["sides"])
        if share >= DOOR_ALONG_FACE_SHARE:
            drop(r, "runs along a wall face ({0:.0%} of its {1:.0f} mm), not across a gap".format(share, r["width"]))
        elif share >= DOOR_DOUBT_FACE_SHARE:
            drop(r, "door? a wall face runs along {0:.0%} of it".format(share), review=True)
    # 2. one door per gap
    live = [r for r in recs if not r.get("out") and r["width"]]
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            if a.get("out") or b.get("out"):
                continue
            if abs(a["u"][0] * b["u"][0] + a["u"][1] * b["u"][1]) < 0.996:
                continue                              # not parallel (5 degrees)
            ux, uy = a["u"]
            pa = sorted(((p[0] - a["c"][0]) * ux + (p[1] - a["c"][1]) * uy) for p in (a["sides"][0][0], a["sides"][0][1]))
            pb = sorted(((p[0] - a["c"][0]) * ux + (p[1] - a["c"][1]) * uy) for s_ in b["sides"] for p in s_)
            ov = min(pa[1], pb[-1]) - max(pa[0], pb[0])
            if ov < 0.5 * min(a["width"], b["width"]):
                continue
            across = abs((b["c"][0] - a["c"][0]) * -uy + (b["c"][1] - a["c"][1]) * ux)
            if across > (a["depth"] + b["depth"]) / 2.0 + DOOR_SAME_GAP_MM:
                continue
            wide, narrow = (a, b) if a["width"] > b["width"] else (b, a)
            drop(wide, "a second door in the gap of a {0:.0f} mm one".format(narrow["width"]))
    return [r["q"] for r in recs if not r.get("out")]


SHAFT_CELL_MAX_RATIO = float(_BD.get("shaft_cell_max_ratio", 5.0))
SHAFT_CELL_MAX_SQM = float(_BD.get("shaft_cell_max_sqm", 6.0))


def _shaft_cells_to_door(merged, floor_texts, doors, shape_of, family_of, trace, gaps_tracer, trace_with,
                         walls=()):
    """See the call in _floor_pass_rules. For each named X-marked cut-out
    (source "X") on a shaft's name - not a lift's - the space traced from
    just outside each of its sides that touches it along most of that side,
    is no wall (`walls`: the wall pieces), compact (its rectangle no more
    than 1.6 times it) and 300 mm wide or more, holds no other label, overlaps no room, is no
    larger than SHAFT_CELL_MAX_RATIO times the cut-out (and
    SHAFT_CELL_MAX_SQM) and has a door (`doors`, quads) on its outline
    becomes a part of the shaft named as it. Returns how many."""
    from shapely.geometry import Polygon, Point, box as _bx
    from shapely.strtree import STRtree
    from shapely.ops import unary_union as _cu
    from . import elements as _el
    wall_u = _cu([g for g in walls or () if g is not None and not g.is_empty]) if walls else None
    rooms = [(d, shape_of(d)) for d in merged]
    rooms = [(d, g) for d, g in rooms if g is not None and not g.is_empty]
    if not rooms:
        return 0
    rtree = STRtree([g for _d, g in rooms])
    dq = [Polygon(q).buffer(0) for q in doors or () if len(q) >= 3]
    dq = [g for g in dq if not g.is_empty]
    if not dq:
        return 0
    dtree = STRtree(dq)
    named = [(d, g) for d, g in rooms if d.get("source") not in ("X",)
             and _is_shaft_name(d.get("label")) and not str(d.get("label") or "").startswith(X_UNKNOWN_LABEL)]
    n_new = 0
    added = []
    for d, xg in list(rooms):
        if d.get("source") != "X":
            continue
        lab = str(d.get("label") or "")
        if not lab or lab.startswith(X_UNKNOWN_LABEL):
            # an unnamed cut-out at the back of a named shaft's cell takes its name
            mr = xg.minimum_rotated_rectangle
            cs = list(mr.exterior.coords)[:4]
            long_side = max(math.hypot(cs[1][0] - cs[0][0], cs[1][1] - cs[0][1]),
                            math.hypot(cs[2][0] - cs[1][0], cs[2][1] - cs[1][1]))
            own = [o for o, og in named if og.buffer(30.0).intersection(xg.boundary).length >= 0.8 * long_side
                   and og.area <= SHAFT_CELL_MAX_RATIO * xg.area + 1e6]
            if len(own) == 1:
                d["label"] = own[0]["label"]
                d.setdefault("evidence", []).append("named for the shaft whose cell it stands in")
            continue
        fam_x = family_of(d)
        if (not shaft_like_label(lab, fam_x) or not _el.is_shaft_room(lab, "") or fam_x == "Lift"
                or _el.is_lift_room(lab, fam_x) or re.search(r"\bLIFT\b", lab.upper())):
            continue
        b = xg.bounds
        cx, cy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
        _dbg = os.environ.get("SHAFT_DEBUG") and (os.environ.get("SHAFT_DEBUG_LABEL") or "ELE SHAFT") in lab
        for px, py, side in ((b[0] - 80.0, cy, _bx(b[0] - 1, b[1], b[0] + 1, b[3])),
                             (b[2] + 80.0, cy, _bx(b[2] - 1, b[1], b[2] + 1, b[3])),
                             (cx, b[1] - 80.0, _bx(b[0], b[1] - 1, b[2], b[1] + 1)),
                             (cx, b[3] + 80.0, _bx(b[0], b[3] - 1, b[2], b[3] + 1))):
            pt = Point(px, py)
            if any(rooms[int(k)][1].contains(pt) for k in rtree.query(pt)) or any(a_.contains(pt) for a_ in added):
                if _dbg: print('  to_door', repr(lab[:12]), (round(px), round(py)), 'in a room', round(cell.area/1e6,2) if 'cell' in dir() else '')
                continue
            if wall_u is not None and wall_u.contains(pt):
                if _dbg: print('  to_door', repr(lab[:12]), (round(px), round(py)), 'in wall', round(cell.area/1e6,2) if 'cell' in dir() else '')
                continue                              # in the wall beside it
            tr = trace(px, py)
            if tr is None or not tr.ok or not tr.polygon or tr.area_sqm <= 0:
                closed = gaps_tracer()
                tr = trace_with(closed, px, py) if closed is not None else None
            if tr is None or not tr.ok or not tr.polygon or tr.area_sqm <= 0:
                if _dbg: print('  to_door', repr(lab[:12]), (round(px), round(py)), 'no trace', round(cell.area/1e6,2) if 'cell' in dir() else '')
                continue
            cell = Polygon(tr.polygon).buffer(0)
            if d.get("cell") and not cell.is_empty and cell.intersection(xg).area > 0.05 * cell.area:
                # the X is its traced compartment (shaft_compartments): the
                # room tracer does not stop at the partition line, so the
                # shaft's room is what lies beyond that line - the door side
                cell = cell.difference(xg.buffer(1.0)).buffer(0)
                if cell.geom_type == "MultiPolygon":
                    cell = max(cell.geoms, key=lambda g_: g_.area)
                if cell.is_empty or cell.geom_type != "Polygon" or cell.area < 0.3 * 1e6:
                    continue
            if _dbg:
                print('  to_door', repr(lab[:12]), 'cell?', d.get("cell"), 'xrects', str(d.get("xrects"))[:80], 'x_box', d.get("bbox"))
            if d.get("cell") and not cell.is_empty:
                # the part of the X's own box the compartment trace left out
                # (the X drawn over the room beside it - H10 T2 PL-SH-03 over
                # its toilet) is that room's, not the shaft's room
                try:
                    rects_ = d.get("xrects") or ([d["x_box"]] if d.get("x_box") else [])
                    drawn_ = _cu([_bx(*r_[:4]) for r_ in rects_]).buffer(0) if rects_ else None
                    if drawn_ is not None and drawn_.intersection(cell).area >= 0.5 * cell.area:
                        if _dbg:
                            print('  to_door', repr(lab[:12]), 'skip: inside the X as drawn', round(cell.area / 1e6, 2))
                        continue
                except Exception as ex_:              # noqa: BLE001
                    if _dbg:
                        print('  to_door', repr(lab[:12]), 'drawn-box test failed:', ex_)
            if cell.is_empty or cell.area > SHAFT_CELL_MAX_RATIO * xg.area or cell.area > SHAFT_CELL_MAX_SQM * 1e6:
                if _dbg: print('  to_door', repr(lab[:12]), (round(px), round(py)), 'too big', round(cell.area/1e6,2) if 'cell' in dir() else '')
                continue
            if cell.buffer(30.0).intersection(side.buffer(1.0)).area < 0.6 * side.buffer(1.0).area:
                if _dbg: print('  to_door', repr(lab[:12]), (round(px), round(py)), 'not along side', round(cell.area/1e6,2) if 'cell' in dir() else '')
                continue                              # not along that side of the cut-out
            mr_ = cell.minimum_rotated_rectangle
            if mr_.area > 1.6 * cell.area:
                if _dbg: print('  to_door', repr(lab[:12]), (round(px), round(py)), 'not compact', round(cell.area/1e6,2) if 'cell' in dir() else '')
                continue                              # not a cell: it runs on round corners
            mc_ = list(mr_.exterior.coords)
            if min(math.hypot(mc_[1][0] - mc_[0][0], mc_[1][1] - mc_[0][1]),
                   math.hypot(mc_[2][0] - mc_[1][0], mc_[2][1] - mc_[1][1])) < 300.0:
                continue                              # too narrow to enter: a wall drawn as an outline
            if wall_u is not None and cell.intersection(wall_u).area > 0.3 * cell.area:
                continue                              # the inside of a wall drawn as an outline
            if any(rooms[int(k)][0] is not d and cell.intersection(rooms[int(k)][1]).area > 0.05 * cell.area
                   for k in rtree.query(cell)):
                continue
            if any(cell.contains(Point(t["x"], t["y"])) for t in floor_texts
                   if not is_ignorable_label(t["text"]) and not is_device_tag(t["text"])
                   and not is_note_text(t["text"]) and t.get("sr_no") != d.get("sr_no")):
                continue                              # another room's name in it
            if not any(cell.buffer(30.0).intersects(dq[int(k)]) for k in dtree.query(cell.buffer(30.0))):
                if _dbg: print('  to_door', repr(lab[:12]), (round(px), round(py)), 'no door', round(cell.area/1e6,2), 'dq near:', [[round(v) for v in g_.bounds] for g_ in dq if g_.distance(cell) <= 1500], 'cell', [round(v) for v in cell.bounds])
                continue                              # a shaft's room has its door
            if cell.envelope.area <= 1.04 * cell.area:
                cell = cell.envelope                  # square off the trace's grid steps
            if _dbg:
                print('  to_door', repr(lab[:12]), 'OK', round(cell.area / 1e6, 2), 'dq near:',
                      [[round(v) for v in g_.bounds] for g_ in dq if g_.distance(cell) <= 1500], 'cell', [round(v) for v in cell.bounds])
            ip = cell.representative_point()
            merged.append({"bbox": tuple(cell.bounds),
                           "poly": [(float(x), float(y)) for x, y in list(cell.exterior.coords)[:-1]],
                           "holes": [[(float(x), float(y)) for x, y in list(r_.coords)[:-1]] for r_ in cell.interiors],
                           "label": lab, "height": d.get("height", 0.0), "source": "S",
                           "cx": ip.x, "cy": ip.y, "sr_no": None, "absorbed_srs": [],
                           "family_forced": d.get("family_forced"),
                           "evidence": ["the shaft's room up to its door (its cut-out drawn on its own)"]})
            added.append(cell)
            n_new += 1
            break
    return n_new


def _leaf_door_quads(segs, strips, found=()):
    """Door openings from a door leaf drawn shut in its opening, where the
    swing does not describe it (door_openings reads swings first).

    A leaf is a panel: two door-layer lines DOOR_LEAF_PANEL_MIN-MAX long,
    10-80 mm apart, overlapping over 90% of the shorter. It is a door's
    only with a frame at both of its ends (short door-layer lines within
    120 mm of each end) or its leaf drawn open from one end. Then:

      * a leaf lying inside a wall band, parallel to it - the wall drawn on
        through the door (P285: the 200 mm masonry front of the TEN / COMM
        / FAPA shafts) - opens that band across its thickness, over the
        leaf and its frame, out to the end of the band when that is within
        150 mm (the next cell's partition);
      * a leaf in a gap - walls at both of its ends within 250 mm - opens
        the gap from jamb to jamb (0.8-1.6 of the leaf), as deep as the
        lines drawn across the whole gap along it (a frame band), else as
        the walls at its jambs.

    A leaf within 300 mm of an opening already found (`found`, shapely)
    adds nothing. strips: the wall strips ({"pts": 4 points, "t"}). Returns
    quads (point lists, mm)."""
    from shapely.geometry import Polygon, Point, LineString, box
    from shapely.strtree import STRtree
    if not DOOR_LEAF_DOORS:
        return []
    door = [s for s in segs or () if len(s) > 7 and s[7] and s[5] >= 5.0]
    long_ = [s for s in door if DOOR_LEAF_PANEL_MIN <= s[5] <= DOOR_LEAF_PANEL_MAX]
    short = [s for s in door if s[5] <= 160.0]
    if not long_:
        return []
    other = [s for s in segs or () if not (len(s) > 7 and s[7]) and s[5] >= 100.0]
    W = []
    for st in strips or ():
        try:
            P = [tuple(map(float, p[:2])) for p in st["pts"]]
            g = Polygon(P).buffer(0)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if g.is_empty or len(P) < 3:
            continue
        ax = None                                     # a hatch-filled piece: no one direction
        if len(P) == 4:
            a = math.hypot(P[1][0] - P[0][0], P[1][1] - P[0][1])
            b = math.hypot(P[2][0] - P[1][0], P[2][1] - P[1][1])
            if a <= 0 or b <= 0:
                continue
            if a >= b:
                ax = ((P[1][0] - P[0][0]) / a, (P[1][1] - P[0][1]) / a)
            else:
                ax = ((P[2][0] - P[1][0]) / b, (P[2][1] - P[1][1]) / b)
        W.append((g, ax))
    wtree = STRtree([w[0] for w in W]) if W else None
    ltree = STRtree([LineString([s[:2], s[2:4]]) for s in long_])
    shtree = STRtree([LineString([s[:2], s[2:4]]) for s in short]) if short else None
    otree = STRtree([LineString([s[:2], s[2:4]]) for s in other]) if other else None
    dtree = STRtree([LineString([s[:2], s[2:4]]) for s in door])

    # the panels: (origin, ux, uy, length, offset of its middle line)
    panels = []
    for i, a in enumerate(long_):
        L = a[5]
        ux, uy = (a[2] - a[0]) / L, (a[3] - a[1]) / L
        for k in ltree.query(LineString([a[:2], a[2:4]]).buffer(90.0)):
            k = int(k)
            if k <= i:
                continue
            b = long_[k]
            M = b[5]
            vx, vy = (b[2] - b[0]) / M, (b[3] - b[1]) / M
            if abs(ux * vy - uy * vx) > 0.02:
                continue
            off = (b[0] - a[0]) * -uy + (b[1] - a[1]) * ux
            if not (10.0 <= abs(off) <= 80.0):
                continue
            s0, s1 = sorted(((b[0] - a[0]) * ux + (b[1] - a[1]) * uy, (b[2] - a[0]) * ux + (b[3] - a[1]) * uy))
            lo, hi = max(0.0, s0), min(L, s1)
            if hi - lo < 0.9 * min(L, M):
                continue
            ox, oy = a[0] + ux * lo + -uy * off / 2.0, a[1] + uy * lo + ux * off / 2.0
            panels.append((ox, oy, ux, uy, hi - lo, abs(off)))
    # one leaf per panel group (a leaf drawn with inner lines too)
    keep = []
    for p in sorted(panels, key=lambda q: -q[4]):
        mx, my = p[0] + p[2] * p[4] / 2.0, p[1] + p[3] * p[4] / 2.0
        if any(abs(p[2] * q[3] - p[3] * q[2]) <= 0.02
               and math.hypot(mx - (q[0] + q[2] * q[4] / 2.0), my - (q[1] + q[3] * q[4] / 2.0)) < 120.0
               for q in keep):
            continue
        keep.append(p)
    out, out_g = [], []
    found = [g for g in found or () if g is not None and not g.is_empty]
    for ox, oy, ux, uy, L, th in keep:
        f = (ox, oy, ux, uy, -uy, ux)
        mid = Point(ox + ux * L / 2.0, oy + uy * L / 2.0)
        if any(g.distance(mid) < 300.0 for g in found + out_g):
            continue
        ends = [(ox, oy), (ox + ux * L, oy + uy * L)]

        def framed(e):
            if shtree is None:
                return False
            return any(short[int(k)][5] >= 10.0 for k in shtree.query(Point(*e).buffer(120.0)))

        def opened(e):
            for k in dtree.query(Point(*e).buffer(60.0)):
                s = door[int(k)]
                if s[5] < 0.6 * L:
                    continue
                sx, sy = (s[2] - s[0]) / s[5], (s[3] - s[1]) / s[5]
                if 0.17 <= abs(ux * sy - uy * sx):            # 10 degrees or more off the shut leaf
                    return True
            return False
        if not ((framed(ends[0]) and framed(ends[1])) or opened(ends[0]) or opened(ends[1])):
            continue
        line = LineString(ends)
        quad = None
        # a. inside a wall band running with it: the wall is drawn through the door
        host = None
        for k in (wtree.query(line) if wtree is not None else ()):
            g, ax = W[int(k)]
            if ax is not None and abs(ax[0] * uy - ax[1] * ux) <= 0.05 and g.buffer(25.0).contains(line):
                if host is None or g.area < host.area:
                    host = g
        if host is not None:
            cs = [_door_frame_pt(f, x, y) for x, y in list(host.exterior.coords)[:-1]]
            t0, t1 = min(c[1] for c in cs), max(c[1] for c in cs)
            hs0, hs1 = min(c[0] for c in cs), max(c[0] for c in cs)
            s0 = hs0 if -150.0 <= hs0 <= 0.0 else -60.0
            s1 = hs1 if L <= hs1 <= L + 150.0 else L + 60.0
            if t1 - t0 >= 50.0:
                quad = [_door_frame_xy(f, s0, t0), _door_frame_xy(f, s1, t0),
                        _door_frame_xy(f, s1, t1), _door_frame_xy(f, s0, t1)]
        # b. in a gap: walls at both ends of the leaf
        elif wtree is not None:
            probe = _door_frame_rect(f, -250.0, L + 250.0, -200.0, 200.0)
            sl, sr, tw = None, None, []
            for k in wtree.query(probe):
                g, wax = W[int(k)]
                if wax is not None and 0.05 < abs(wax[0] * uy - wax[1] * ux) < 0.95:
                    continue                          # a wall at an angle: the leaf is drawn open
                part = g.intersection(probe)
                if part.is_empty:
                    continue
                cs = [_door_frame_pt(f, x, y) for p_ in getattr(part, "geoms", [part])
                      if p_.geom_type == "Polygon" for x, y in list(p_.exterior.coords)[:-1]]
                if not cs:
                    continue
                smin, smax = min(c[0] for c in cs), max(c[0] for c in cs)
                if smax <= L * 0.15 and smax >= -250.0:
                    sl = smax if sl is None else max(sl, smax)
                    tw += [c[1] for c in cs]
                elif smin >= L * 0.85 and smin <= L + 250.0:
                    sr = smin if sr is None else min(sr, smin)
                    tw += [c[1] for c in cs]
            if sl is not None and sr is not None and 0.8 * L <= sr - sl <= 1.6 * L:
                gap = _door_frame_rect(f, sl, sr, -40.0, 40.0)
                covered = sum(gap.intersection(W[int(k)][0]).area for k in wtree.query(gap))
                if covered <= 0.3 * gap.area:
                    offs = []
                    for k in (otree.query(_door_frame_rect(f, sl, sr, -300.0, 300.0)) if otree is not None else ()):
                        s = other[int(k)]
                        sx, sy = (s[2] - s[0]) / s[5], (s[3] - s[1]) / s[5]
                        if abs(ux * sy - uy * sx) > 0.05:
                            continue
                        p0, p1 = _door_frame_pt(f, s[0], s[1]), _door_frame_pt(f, s[2], s[3])
                        lo, hi = min(p0[0], p1[0]), max(p0[0], p1[0])
                        if min(hi, sr) - max(lo, sl) >= 0.7 * (sr - sl) and lo >= sl - 150.0 and hi <= sr + 150.0:
                            offs.append((p0[1] + p1[1]) / 2.0)
                    # the frame band: the nearest line along the gap on each side of the leaf
                    below = [v for v in offs if v < -th / 2.0 - 5.0]
                    above = [v for v in offs if v > th / 2.0 + 5.0]
                    tws = [v for v in tw if -300.0 <= v <= 300.0]
                    t0 = max(below) if below else (max(min(tws), -300.0) if tws else -100.0)
                    t1 = min(above) if above else (min(max(tws), 300.0) if tws else 100.0)
                    if t1 - t0 >= 50.0:
                        quad = [_door_frame_xy(f, sl, t0), _door_frame_xy(f, sr, t0),
                                _door_frame_xy(f, sr, t1), _door_frame_xy(f, sl, t1)]
        if quad is not None:
            out.append([(float(x), float(y)) for x, y in quad])
            out_g.append(Polygon(quad).buffer(0))
    return out


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
            if not _is_door(effective_layer(e, xform), xform.blocks) or _is_annotation(effective_layer(e, xform), xform.blocks):
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


def _door_bridges(faces, caps, swings, others=None):
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

    `others` (any drawn lines): when the far jamb is no wall face - a door
    frame drawn as loose linework (H10 Tower 2, the ladies' toilet door
    D1: its far jamb is a frame outline, so the toilet's fill ran out
    through the door and the room came back as a four-ray box, 22 of 38
    sq.m) - a door whose hinge stands at this wall's end closes the
    opening at the first drawn line the leaf's width away.
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

    def wall_end(fi, f, ux, uy, L):
        """True when face f is the end of a wall: no longer than
        DOOR_CAP_FACE_MAX, with a face square to it leaving each of its
        ends on the same side and the longer of the two at least twice its
        length. Its ray runs across the wall's thickness: it closes a
        doorway only when it meets the far side at a wall's corner."""
        if L > DOOR_CAP_FACE_MAX:
            return False
        sides = []
        for px, py in ((f[0], f[1]), (f[2], f[3])):
            best = None
            for bi in grid.get((int(math.floor(px / CELL)), int(math.floor(py / CELL))), ()):
                if bi == fi:
                    continue
                s = barrier[bi]
                for qx, qy, rx, ry in ((s[0], s[1], s[2], s[3]), (s[2], s[3], s[0], s[1])):
                    if math.hypot(qx - px, qy - py) > 10.0:
                        continue
                    vx, vy = rx - qx, ry - qy
                    lv = math.hypot(vx, vy)
                    if lv < 1.0 or abs((vx * ux + vy * uy) / lv) > 0.1:
                        continue
                    side = 1 if (vx * -uy + vy * ux) > 0 else -1
                    if best is None or lv > best[1]:
                        best = (side, lv)
            if best is None:
                return False
            sides.append(best)
        return sides[0][0] == sides[1][0] and max(sides[0][1], sides[1][1]) >= 2.0 * L

    out = set()
    for fi, f in enumerate(faces):
        ax = _axis(f)
        if ax is None:
            continue
        _ang, ux, uy, _L = ax
        cap = wall_end(fi, f, ux, uy, _L)
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
            hits_b = sorted((t, bi) for bi in cand if bi != fi
                            for t in [_ray_hit(ox, oy, dx, dy, barrier[bi])]
                            if t is not None and 60.0 < t <= DOOR_BRIDGE_MAX)
            hits = [t for t, _bi in hits_b]
            if cap:
                # A wall's end carried on runs across its own thickness: a
                # doorway only when it reaches the corner of the wall
                # opposite - the opening between two wall ends (shaft doors
                # between partition ends, H10). Ending in the middle of a
                # face it runs into the room behind the door (FHC-3, P285).
                if not hits_b or hits_b[0][0] < DOOR_BRIDGE_MIN:
                    continue
                t0, b0 = hits_b[0]
                hx0, hy0 = ox + dx * t0, oy + dy * t0
                s0 = barrier[b0]
                if min(math.hypot(s0[0] - hx0, s0[1] - hy0), math.hypot(s0[2] - hx0, s0[3] - hy0)) > DOOR_CAP_CORNER_MM:
                    continue

            def frame_close():
                if not others:
                    return
                t_ = _door_frame_hit(ox, oy, dx, dy, hinges_near(ox, oy, DOOR_HINGE_TOL), others)
                if t_ is not None:
                    hx_, hy_ = ox + dx * t_, oy + dy * t_
                    a_, b_ = (round(ox, 1), round(oy, 1)), (round(hx_, 1), round(hy_, 1))
                    out.add(a_ + b_ if a_ <= b_ else b_ + a_)
            if not hits or hits[0] < DOOR_BRIDGE_MIN:
                if not cap:
                    frame_close()
                continue
            t = hits[0]
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
            elif not cap:
                # the first wall beyond is not this door's far jamb (the
                # next door's, a frame on a door layer in between)
                frame_close()
    return sorted(out)


def _join_x_strip_pairs(merged, wall_lines, shape_of, set_shape, gap_max=SHAFT_STRIP_GAP_MAX_MM):
    """Join X-marked strips (source "X") lying face to face - the same
    length within 5%, overlapping 90% along, 50-gap_max mm apart, no wall
    line in the band between and no other room in it - into one shaft:
    both strips and the band. The first keeps the label (a named strip's
    over Unknown Shaft); the other is marked _joined_away. Returns how many
    were joined."""
    from shapely.geometry import box as _bx, LineString
    xs = [d for d in merged if d.get("source") == "X" and d.get("bbox")]
    others = [(d, shape_of(d)) for d in merged if d.get("source") != "X"]
    n = 0
    for i, a in enumerate(xs):
        if a.get("_joined_away"):
            continue
        for b in xs[i + 1:]:
            if b.get("_joined_away"):
                continue
            A, Bb = a["bbox"], b["bbox"]
            for horiz in (True, False):
                k0, k1 = (0, 2) if horiz else (1, 3)      # along
                j0, j1 = (1, 3) if horiz else (0, 2)      # across
                la, lb = A[k1] - A[k0], Bb[k1] - Bb[k0]
                ta, tb = A[j1] - A[j0], Bb[j1] - Bb[j0]
                if min(la, lb) <= 0 or la < 2 * ta or lb < 2 * tb:
                    continue                          # not strips along this axis
                if abs(la - lb) > 0.05 * max(la, lb):
                    continue
                ov = min(A[k1], Bb[k1]) - max(A[k0], Bb[k0])
                if ov < 0.9 * max(la, lb):
                    continue
                lo_, hi_ = (A, Bb) if A[j0] < Bb[j0] else (Bb, A)
                gap = hi_[j0] - lo_[j1]
                if not (50.0 <= gap <= gap_max):
                    continue
                g0, g1 = max(A[k0], Bb[k0]), min(A[k1], Bb[k1])
                band = _bx(g0, lo_[j1], g1, hi_[j0]) if horiz else _bx(lo_[j1], g0, hi_[j0], g1)
                inner = band.buffer(-20.0)
                if inner.is_empty:
                    continue
                if any(LineString([(w[0], w[1]), (w[2], w[3])]).intersects(inner) for w in wall_lines
                       if min(w[0], w[2]) <= band.bounds[2] and max(w[0], w[2]) >= band.bounds[0]
                       and min(w[1], w[3]) <= band.bounds[3] and max(w[1], w[3]) >= band.bounds[1]):
                    continue                          # a wall between: two shafts
                if any(g is not None and g.intersects(inner) and g.intersection(inner).area > 0.1 * inner.area
                       for _d, g in others):
                    continue                          # a room in the band
                ga, gb = shape_of(a), shape_of(b)
                if ga is None or gb is None:
                    continue
                u = ga.union(gb).union(band.buffer(1.0, join_style=2))
                keep, gone = (a, b) if a.get("label") != X_UNKNOWN_LABEL or b.get("label") == X_UNKNOWN_LABEL else (b, a)
                if not set_shape(keep, u, (keep["cx"], keep["cy"])):
                    continue
                keep["xrects"] = list(keep.get("xrects") or []) + list(gone.get("xrects") or [])
                keep.setdefault("evidence", []).append("two X-marked strips and the band between: one shaft")
                gone["_joined_away"] = True
                if gone.get("sr_no") is not None:
                    keep.setdefault("absorbed_srs", []).append(gone["sr_no"])
                n += 1
                break
            if b.get("_joined_away") or a.get("_joined_away"):
                break
    return n


def _door_quad_closures(quads, closing, board_max=None):
    """Lines that close the rooms at door openings (door_openings quads)
    no closure in `closing` already crosses: both long sides of each quad,
    carried a hair past its ends so the fill cannot squeeze round them. A
    door in a board (no deeper than board_max: a WC cubicle's) is left
    open - a cubicle is not a room. [(x1, y1, x2, y2)]."""
    from shapely.geometry import Polygon, LineString
    from shapely.strtree import STRtree
    from . import elements as _el
    board_max = _el.DOOR_CUBICLE_BOARD_MAX_MM if board_max is None else board_max
    lines = [LineString([(c[0], c[1]), (c[2], c[3])]) for c in closing or ()]
    tree = STRtree(lines) if lines else None
    out = []
    for q in quads or ():
        ax = _el.door_axes(q)
        if ax is None:
            continue
        cx, cy, ux, uy, nx, ny, W, D = ax
        if D <= board_max or W < 300.0:
            continue
        g = Polygon([tuple(p[:2]) for p in q]).buffer(0)
        zone = g.buffer(120.0)
        if tree is not None and any(lines[int(k)].intersection(zone).length >= 0.6 * W for k in tree.query(zone)):
            continue                                  # closed by the wall faces carried across
        h = W / 2.0 + 15.0
        for t in (-D / 2.0, D / 2.0):
            out.append((cx - ux * h + nx * t, cy - uy * h + ny * t, cx + ux * h + nx * t, cy + uy * h + ny * t))
    return out


def _door_frame_hit(ox, oy, dx, dy, at_o, others):
    """Distance along the ray from a wall end to the first drawn line a
    door leaf's width away (within 25 %), when a hinge stands at that end;
    None otherwise. `others` is a _LineGrid of every drawn line."""
    if not at_o:
        return None
    for sw in at_o:
        w = sw[2]
        if not (DOOR_BRIDGE_MIN <= w <= DOOR_BRIDGE_MAX):
            continue
        reach = 1.25 * w
        best = None
        # three rays a hair apart: a jamb whose end lies exactly on the
        # wall face line is hit by at least one of them
        for off in (0.0, 8.0, -8.0):
            rx, ry = ox - dy * off, oy + dx * off
            t = others.ray_min(rx, ry, dx, dy, 0.75 * w, reach)
            if t is not None and (best is None or t < best):
                best = t
        if best is not None:
            return best
    return None


class _LineGrid:
    """Lines bucketed on a 1 m grid, for short ray queries."""
    def __init__(self, segs, cell=1000.0):
        self.cell, self.grid = cell, {}
        self.segs = list(segs)
        for i, s in enumerate(self.segs):
            for gx in range(int(math.floor(min(s[0], s[2]) / cell)), int(math.floor(max(s[0], s[2]) / cell)) + 1):
                for gy in range(int(math.floor(min(s[1], s[3]) / cell)), int(math.floor(max(s[1], s[3]) / cell)) + 1):
                    self.grid.setdefault((gx, gy), []).append(i)
        self._xy = None
        self._cells = {}

    def _cell_ids(self, x0, y0, x1, y1):
        c = self.cell
        for gx in range(int(math.floor(min(x0, x1) / c)), int(math.floor(max(x0, x1) / c)) + 1):
            for gy in range(int(math.floor(min(y0, y1) / c)), int(math.floor(max(y0, y1) / c)) + 1):
                ids = self.grid.get((gx, gy))
                if ids:
                    yield (gx, gy), ids

    def near(self, x0, y0, x1, y1):
        seen = set()
        for _key, ids in self._cell_ids(x0, y0, x1, y1):
            for i in ids:
                s = self.segs[i]
                if id(s) not in seen:
                    seen.add(id(s))
                    yield s

    def ray_min(self, rx, ry, dx, dy, lo, hi):
        """The least t in [lo, hi] at which the ray (rx, ry) + t*(dx, dy)
        meets a line near it (_ray_hit's test, over every line at once),
        or None."""
        import numpy as np
        parts = []
        for key, ids in self._cell_ids(rx, ry, rx + dx * hi, ry + dy * hi):
            arr = self._cells.get(key)
            if arr is None:
                arr = self._cells[key] = np.asarray(ids, dtype=np.int64)
            parts.append(arr)
        if not parts:
            return None
        if self._xy is None:
            self._xy = np.array([(s[0], s[1], s[2], s[3]) for s in self.segs], dtype=np.float64).reshape(-1, 4)
        S = self._xy[np.concatenate(parts)]
        ex, ey = S[:, 2] - S[:, 0], S[:, 3] - S[:, 1]
        den = dx * ey - dy * ex
        qx, qy = S[:, 0] - rx, S[:, 1] - ry
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (qx * ey - qy * ex) / den
            v = (qx * dy - qy * dx) / den
        ok = ((np.abs(den) >= 1e-9) & (t > 0) & (v >= -1e-6) & (v <= 1.0 + 1e-6)
              & (t >= lo) & (t <= hi))
        if not ok.any():
            return None
        return float(t[ok].min())


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


def facade_glass_segs(glass_segs, plate, band=None):
    """The facade's glass (rule boundary_on_evidence): the glass lines
    ((x1, y1, x2, y2), mm) standing within `band` (FACADE_GLASS_BAND_MM) of
    the plate's edge - the glazing along the outside, not a glass partition
    inside the floor. A room traced towards the outside stops at the glass,
    not at the slab edge beyond it."""
    band = FACADE_GLASS_BAND_MM if band is None else float(band)
    if not glass_segs or not plate or len(plate) < 3:
        return []
    from shapely.geometry import Polygon, LineString
    try:
        edge = Polygon([tuple(p[:2]) for p in plate]).buffer(0).exterior
    except (ValueError, TypeError, IndexError, AttributeError):
        return []
    out = []
    for s_ in glass_segs:
        try:
            ln = LineString([(float(s_[0]), float(s_[1])), (float(s_[2]), float(s_[3]))])
        except (ValueError, TypeError, IndexError):
            continue
        if ln.length > 0 and edge.distance(ln.interpolate(0.5, normalized=True)) <= band:
            out.append(tuple(float(v) for v in s_[:4]))
    return out


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


def _ray_dists(px, py, wall_segs, index=None, max_search=MAX_HV_SEARCH):
    """The four ray distances _hv_boundary measures, without deciding."""
    best = {"left": max_search, "right": max_search,
            "down": max_search, "up": max_search}
    if index is not None:
        h_cands = index.crossing_horizontal_ray(py)
        v_cands = index.crossing_vertical_ray(px)
    else:
        h_cands = v_cands = wall_segs or ()
    for sx, sy, ex, ey in h_cands:
        if not (min(sy, ey) <= py <= max(sy, ey)):
            continue
        if index is None and abs(ey - sy) <= abs(ex - sx):
            continue
        diff = (sx + ex) / 2 - px
        if diff > 0: best["right"] = min(best["right"], diff)
        elif diff < 0: best["left"] = min(best["left"], -diff)
    for sx, sy, ex, ey in v_cands:
        if not (min(sx, ex) <= px <= max(sx, ex)):
            continue
        if index is None and abs(ex - sx) <= abs(ey - sy):
            continue
        diff = (sy + ey) / 2 - py
        if diff > 0: best["up"] = min(best["up"], diff)
        elif diff < 0: best["down"] = min(best["down"], -diff)
    return best


def _corridor_box(px, py, wall_segs, index=None, max_search=MAX_HV_SEARCH):
    """A space between two parallel walls that is open at its ends.

    A lift lobby between two banks of lifts has walls on both sides and
    corridor at both ends, so the fill escapes and the four rays find no
    end wall. The space is followed along while both side walls stay at
    the same distance (within 300 mm) and no wall crosses it, and ends
    where the side walls do. Returns a box, or None when this is not such
    a space (sides 1.5-8 m apart, at least 1.5 times as long as wide).
    """
    d0 = _ray_dists(px, py, wall_segs, index, max_search)
    step, reach = 250.0, 40000.0
    for across, along in ((("left", "right"), ("down", "up")),
                          (("down", "up"), ("left", "right"))):
        a, b = d0[across[0]], d0[across[1]]
        if a >= max_search or b >= max_search:
            continue
        width = a + b
        if not (1500.0 <= width <= 8000.0):
            continue
        def at(o, sign):
            return (px, py + sign * o) if across[0] == "left" else (px + sign * o, py)

        def sides_hold(dd):
            return abs(dd[across[0]] - a) <= 300.0 and abs(dd[across[1]] - b) <= 300.0

        ends = []
        for sign, wall_key in ((-1, along[0]), (1, along[1])):
            off = 0.0
            while off < reach:
                if d0[wall_key] <= off + step:
                    off = d0[wall_key]
                    break
                nxt = off + step
                dd = _ray_dists(*at(nxt, sign), wall_segs, index, max_search)
                if not sides_hold(dd):
                    # the side walls stop between the last step and this
                    # one: find where (to about 1 mm), not the last step
                    lo_, hi_ = off, nxt
                    for _ in range(8):
                        mid = (lo_ + hi_) / 2.0
                        if sides_hold(_ray_dists(*at(mid, sign), wall_segs, index, max_search)):
                            lo_ = mid
                        else:
                            hi_ = mid
                    off = _snap_to_wall_end(px, py, sign, lo_, across, a, b, wall_segs)
                    break
                if dd[wall_key] < step:
                    # the end wall is closer than a step: run to it
                    off = nxt + dd[wall_key]
                    break
                off = nxt
            ends.append(off)
        length = ends[0] + ends[1]
        if length < 1.5 * width or length * width / 1e6 > MAX_ROOM_SQM:
            continue
        if across[0] == "left":
            return (px - a, py - ends[0], px + b, py + ends[1])
        return (px - ends[0], py - a, px + ends[1], py + b)
    return None


def _snap_to_wall_end(px, py, sign, off, across, a, b, wall_segs, tol=25.0):
    """off moved onto the end of a side wall within tol of it (the side
    walls of a corridor box stop exactly there), else off."""
    vertical = across[0] == "left"                   # the box runs along y
    end = (py if vertical else px) + sign * off
    sides = ((px - a, px + b) if vertical else (py - a, py + b))
    best = None
    for sg in wall_segs:
        for ex, ey in ((sg[0], sg[1]), (sg[2], sg[3])):
            c_across, c_along = (ex, ey) if vertical else (ey, ex)
            if min(abs(c_across - sides[0]), abs(c_across - sides[1])) > 10.0:
                continue
            dlt = abs(c_along - end)
            if dlt <= tol and (best is None or dlt < best[0]):
                best = (dlt, c_along)
    if best is None:
        return off
    return abs(best[1] - (py if vertical else px))


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
            # The walls only move a side OUT onto the wall face. A line
            # crossing the X (285 FD: a 13.8 m slab-edge line on a-details
            # running 125 mm inside the STP and DG shafts) never pulls a
            # side in: the X's own extent stands there.
            tol = X_INSIDE_TOL_MM
            enc = (bb[0] if enc[0] > bb[0] + tol else enc[0],
                   bb[1] if enc[1] > bb[1] + tol else enc[1],
                   bb[2] if enc[2] < bb[2] - tol else enc[2],
                   bb[3] if enc[3] < bb[3] - tol else enc[3])
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


ROOM_APPID = "DRAWING_ANALYZER_ROOM"


def _tag_room(doc, entity, source, label="", conf=None, cell=False):
    """Store how a boundary was made on it, as XDATA: "X" for a slab
    opening found by its X mark, "P"/"S" for a room traced from its
    label, "G" for one the geometry found (a staircase by its treads).
    The area step reads it back: an X-marked opening is a cut-out in the
    slab and is deducted; a walled shaft room without an X is a room."""
    try:
        if ROOM_APPID not in doc.appids:
            doc.appids.new(ROOM_APPID)
        vals = [(1000, str(source or "")), (1000, " ".join(str(label or "").split())[:200])]
        if conf is not None or cell:
            c_ = conf if conf is not None else (0.0, [])
            vals += [(1000, "%.2f" % c_[0]), (1000, "; ".join(c_[1])[:240])]
        if cell:
            vals += [(1000, "cell")]                  # traced to its compartment (shaft_compartments)
        entity.set_xdata(ROOM_APPID, vals)
    except (AttributeError, TypeError, ValueError):
        pass


def _confidence_band(score):
    from . import confidence as _cf
    return _cf.band(score)


def room_confidence_tag(entity):
    """(score, reasons) _tag_room stored on a boundary, or (None, "")."""
    try:
        if entity.has_xdata(ROOM_APPID):
            vals = [v for code, v in entity.get_xdata(ROOM_APPID) if code == 1000]
            if len(vals) > 2 and vals[2]:
                return float(vals[2]), (str(vals[3]) if len(vals) > 3 else "")
    except (AttributeError, TypeError, ValueError):
        pass
    return None, ""


def _boundary_confidence(d, fam_name):
    """confidence.score_room for one drawn boundary."""
    from . import confidence as _cf
    from .labels import declared_area as _da
    try:
        b = d["bbox"]
        if d.get("poly"):
            area = shoelace_area_sqm(d["poly"]) - sum(shoelace_area_sqm(h) for h in (d.get("holes") or []))
        else:
            area = (b[2] - b[0]) * (b[3] - b[1]) / 1e6
        ev = " ".join(str(x) for x in (d.get("evidence") or [])).lower()
        return _cf.score_room(
            area, b[2] - b[0], b[3] - b[1],
            want=declared_size(d.get("label") or ""), want_area=_da(d.get("label") or ""),
            traced=bool(d.get("poly")) and not d.get("box_only"), source=d.get("source") or "P", family=fam_name,
            other_labels=len(d.get("merged_srs") or []),
            grown=bool(d.get("_grown_strip")) or "grown" in ev or "fitted" in ev,
            leak_cut="cut" in ev or "leak" in ev,
            tag_name=bool(is_device_tag(d.get("label") or "")))
    except Exception:                                 # noqa: BLE001
        return None


def room_tag(entity):
    """(source, label) _tag_room stored on a boundary, or (None, None)."""
    try:
        if entity.has_xdata(ROOM_APPID):
            vals = [v for code, v in entity.get_xdata(ROOM_APPID) if code == 1000]
            if vals:
                return (str(vals[0]) or None, str(vals[1]) if len(vals) > 1 else None)
    except (AttributeError, TypeError, ValueError):
        pass
    return None, None


def room_cell_tag(entity):
    """True when the boundary was traced to its compartment from its X
    (shaft_compartments): the texts' outline stands against the geometry's."""
    try:
        if entity.has_xdata(ROOM_APPID):
            vals = [v for code, v in entity.get_xdata(ROOM_APPID) if code == 1000]
            return len(vals) > 4 and str(vals[4]) == "cell"
    except (AttributeError, TypeError, ValueError):
        pass
    return False


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


def _poly_overlap_ratio(pa, pb):
    """Shared area over the smaller outline's area (0..1)."""
    try:
        from shapely.geometry import Polygon as _P
        ga, gb = _P(pa).buffer(0), _P(pb).buffer(0)
        m = min(ga.area, gb.area)
        return ga.intersection(gb).area / m if m > 0 else 0.0
    except Exception:                                 # noqa: BLE001
        return 1.0


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



# -- Sizes, voids, cut-out shafts, geometry types ------------------------------
def _exact_size_err(want, w, h, partial=False):
    """How far (mm, the worse side) a w x h region is from the size a label
    states, when both sides agree within 3% (at least 25 mm) either way
    round; None otherwise. Stricter than _size_match: 750x1050 and
    750x1140 are different shafts."""
    if not want:
        return None
    a, b = want
    best = None
    for p, q in ((a, b), (b, a)):
        ew, eh = abs(w - p), abs(h - q)
        if partial:
            # one side exact, the other well short of the stated side
            if eh <= max(0.03 * q, 25.0) and w < 0.8 * p and min(w, h) >= 150.0:
                best = eh if best is None else min(best, eh)
            continue
        if ew <= max(0.03 * p, 25.0) and eh <= max(0.03 * q, 25.0):
            e = max(ew, eh)
            best = e if best is None else min(best, e)
    return best


def _size_match(want, w, h):
    """How a region of w x h mm fits the size a label states.

    "full"    both sides agree (either way round), within tolerance;
    "partial" one side agrees and the region is narrower on the other -
              the X marks only the slab cut-out inside the shaft;
    None      no agreement.
    """
    if not want:
        return None
    a, b = want

    def near(x, y):
        return abs(x - y) <= max(SIZE_MATCH_TOL * y, SIZE_MATCH_MIN_MM)
    for p, q in ((a, b), (b, a)):
        if near(w, p) and near(h, q):
            return "full"
    for p, q in ((a, b), (b, a)):
        if (near(w, p) and h < q) or (near(h, q) and w < p):
            return "partial"
    return None


def _bbox_iou(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _size_ok(area_sqm, want, bbox=None):
    """Is a boundary of this area (and, given its box, this shape)
    believable for the size a label states? The shape matters as much as
    the area: 22 sq.m spread over 5.7 x 14.3 m is not a 9.5 x 4.35 m
    toilet, though the areas are within reach of each other."""
    if not want or area_sqm is None:
        return True
    wa = want[0] * want[1] / 1e6
    if abs(area_sqm - wa) > 2.0 and not (
            SIZE_MIN_RATIO * wa <= area_sqm <= SIZE_MAX_RATIO * wa):
        return False
    return _shape_penalty(want, bbox) <= math.log(1.8)


def _shape_penalty(want, bbox):
    """How far a box's sides are from the stated sides (log ratio of the
    worse side, either way round); 0 for no box."""
    if not want or bbox is None:
        return 0.0
    w = max(bbox[2] - bbox[0], 1.0)
    h = max(bbox[3] - bbox[1], 1.0)
    a, b = want
    return min(max(abs(math.log(w / a)), abs(math.log(h / b))),
               max(abs(math.log(w / b)), abs(math.log(h / a))))


_TAG_LAYER_RE = re.compile(r"(^|[^A-Z])TAGS?([^A-Z]|$)", re.IGNORECASE)
_SPACED_TAG_RE = re.compile(r"^([A-Z]{1,4}) (\d{1,3}[A-Z]?)$")


def _spaced_tag_on_tag_layer(text, layer):
    """A tag written with a space ("FMD 4") on a tag layer: the same device
    tag as FMD-4 beside it. Tags are otherwise never spaced, so a room name
    such as SHOP 4 on a room-name layer is untouched (BHUB 20th floor: FMD 4
    on A3-Tag took a 39 sq.m boundary of its own)."""
    if not _TAG_LAYER_RE.search(str(layer or "")):
        return False
    m = _SPACED_TAG_RE.match(" ".join(str(text or "").split()).upper())
    return bool(m) and is_device_tag(m.group(1) + "-" + m.group(2))


def _is_plain_tag(text):
    """A door or device tag (FRD-2, D-5) that must never name a space; with
    X_TAGS_NEVER_NAME also every door / window / cut-out tag (FMD, SD-3A,
    W-1, ALW3, C-10a - xplace.tag_kind)."""
    if (_LABEL_RULES.is_tag(text)
            and not _LABEL_RULES.family_keyword(text)
            and not _LABEL_RULES.is_equipment(text)):
        return True
    return bool(X_TAGS_NEVER_NAME and _x_tag_kind(text))


def _x_tag_is_name(text):
    """A shaft's name or a room-family keyword: never a door / window /
    cut-out tag (CHW is chilled water, not a window)."""
    try:
        fam = family_for_label(text, _FAMILY_RULES)["name"]
    except Exception:                                 # noqa: BLE001
        fam = ""
    return bool(shaft_like_label(text, fam) or _LABEL_RULES.family_keyword(text))


def _x_tag_kind(text):
    """'door' / 'window' / 'cutout' for a tag beside an X (xplace.tag_kind)."""
    from . import xplace as _xp
    return _xp.tag_kind(text, _x_tag_is_name)


def _best_outline(bbox, outlines):
    """The closed outline that is this void, when the drawing has one."""
    best, best_iou = None, 0.8
    a = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    for poly in outlines or ():
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        ob = (min(xs), min(ys), max(xs), max(ys))
        w = min(bbox[2], ob[2]) - max(bbox[0], ob[0])
        h = min(bbox[3], ob[3]) - max(bbox[1], ob[1])
        if w <= 0 or h <= 0:
            continue
        inter = w * h
        union = a + (ob[2] - ob[0]) * (ob[3] - ob[1]) - inter
        iou = inter / union if union > 0 else 0.0
        if iou > best_iou:
            best, best_iou = [tuple(p) for p in poly], iou
    return best


_FIXTURE_NAME_RE = re.compile(
    r"(^|[^A-Z])(E?WC|IWC|W\.C|WB|URINALS?|URNL|BASINS?|COMMODE|SINK|LAV\w*|"
    r"TOILET|SANITARY|SANIT\w*|FIXTURES?|PAN)([^A-Z]|$)")


def _collect_fixtures(doc, msp, units):
    """Sanitary fixture positions (mm): inserts whose block name or layer
    reads as a WC, basin or urinal. Each block definition is searched once
    (two levels deep) and placed by its insert, so a sheet of repeated
    toilet blocks costs one read per block, not one per placement."""
    cache = {}

    def local(name, depth):
        if name in cache:
            return cache[name]
        cache[name] = []
        pts = []
        try:
            blk = doc.blocks.get(name)
        except Exception:                             # noqa: BLE001
            blk = None
        if blk is None or depth > 2:
            return pts
        for e in blk.query("INSERT"):
            nm = (e.dxf.get("name") or "").upper()
            ly = (e.dxf.get("layer") or "").upper()
            if _FIXTURE_NAME_RE.search(nm) or _FIXTURE_NAME_RE.search(ly) or \
                    _is_fixture_layer(ly):
                pts.append((e.dxf.insert.x, e.dxf.insert.y))
            else:
                try:
                    m = list(e.matrix44())
                except Exception:                     # noqa: BLE001
                    continue
                for x, y in local(nm and e.dxf.get("name"), depth + 1):
                    pts.append((x * m[0] + y * m[4] + m[12],
                                x * m[1] + y * m[5] + m[13]))
        cache[name] = pts
        return pts

    out = []
    for e in msp.query("INSERT"):
        nm = (e.dxf.get("name") or "").upper()
        ly = (e.dxf.get("layer") or "").upper()
        try:
            if _FIXTURE_NAME_RE.search(nm) or _FIXTURE_NAME_RE.search(ly) or \
                    _is_fixture_layer(ly):
                out.append(units.point_to_mm(e.dxf.insert.x, e.dxf.insert.y))
                continue
            inner = local(e.dxf.get("name"), 1)
            if not inner:
                continue
            m = list(e.matrix44())
            for x, y in inner:
                out.append(units.point_to_mm(x * m[0] + y * m[4] + m[12],
                                             x * m[1] + y * m[5] + m[13]))
        except Exception:                             # noqa: BLE001
            continue
    return out


def _stair_treads(segs_arr, bbox, poly=None):
    """Number of evenly spaced treads inside a region (0 when none).

    A flight is drawn as parallel lines at the going of a step, 250-300
    mm apart; nothing else in a plan repeats like that across a space.
    """
    import numpy as np
    if segs_arr is None or not len(segs_arr):
        return 0
    x1, y1, x2, y2 = bbox
    m = 50.0
    a = segs_arr
    inside = ((np.minimum(a[:, 0], a[:, 2]) >= x1 - m) &
              (np.maximum(a[:, 0], a[:, 2]) <= x2 + m) &
              (np.minimum(a[:, 1], a[:, 3]) >= y1 - m) &
              (np.maximum(a[:, 1], a[:, 3]) <= y2 + m))
    a = a[inside]
    if not len(a):
        return 0
    dx = np.abs(a[:, 2] - a[:, 0])
    dy = np.abs(a[:, 3] - a[:, 1])
    L = np.hypot(dx, dy)
    best = 0
    for sel, pos in (((dy <= 0.03 * L) & (L >= STAIR_TREAD_MIN_LEN),
                      (a[:, 1] + a[:, 3]) / 2.0),
                     ((dx <= 0.03 * L) & (L >= STAIR_TREAD_MIN_LEN),
                      (a[:, 0] + a[:, 2]) / 2.0)):
        p = np.sort(pos[sel])
        if len(p) < STAIR_MIN_TREADS:
            continue
        uniq = [p[0]]
        for v in p[1:]:
            if v - uniq[-1] > 15.0:
                uniq.append(v)
        run, step = 1, None
        for k in range(1, len(uniq)):
            d = uniq[k] - uniq[k - 1]
            if STAIR_RISER_MIN <= d <= STAIR_RISER_MAX and (
                    step is None or abs(d - step) <= 25.0):
                run += 1
                step = d if step is None else step
            else:
                best = max(best, run)
                run, step = 1, None
        best = max(best, run)
    return best if best >= STAIR_MIN_TREADS else 0


def _tread_runs(segs_arr, min_len=None, max_len=3500.0, with_runs=False):
    """Centres of the flights of steps drawn on a floor: [(x, y, n)].

    A flight is a run of STAIR_MIN_TREADS or more parallel lines of a
    tread's length, evenly spaced at a going (STAIR_RISER_MIN..MAX). The
    lines of one flight share their extent along their own direction, so
    they are grouped by that extent first and read for even spacing
    second. Nothing else on a plan repeats like that across a space.
    with_runs: each centre also carries its flight, ((x0, y0, x1, y1),
    treads horizontal, longest tread).
    """
    import numpy as np
    if segs_arr is None or not len(segs_arr):
        return []
    min_len = STAIR_TREAD_MIN_LEN if min_len is None else min_len
    a = segs_arr
    dx = np.abs(a[:, 2] - a[:, 0])
    dy = np.abs(a[:, 3] - a[:, 1])
    L = np.hypot(dx, dy)
    out = []
    for horiz in (True, False):
        sel = ((dy if horiz else dx) <= 0.03 * L) & (L >= min_len) & (L <= max_len)
        b = a[sel]
        if len(b) < STAIR_MIN_TREADS:
            continue
        # along-extent (lo, hi) and across position
        lo = np.minimum(b[:, 0], b[:, 2]) if horiz else np.minimum(b[:, 1], b[:, 3])
        hi = np.maximum(b[:, 0], b[:, 2]) if horiz else np.maximum(b[:, 1], b[:, 3])
        pos = (b[:, 1] + b[:, 3]) / 2.0 if horiz else (b[:, 0] + b[:, 2]) / 2.0
        order = np.argsort(pos)
        lo, hi, pos = lo[order], hi[order], pos[order]
        used = np.zeros(len(b), dtype=bool)
        for i in range(len(b)):
            if used[i]:
                continue
            run = [i]
            step = None
            for j in range(i + 1, len(b)):
                if used[j]:
                    continue
                d = pos[j] - pos[run[-1]]
                if d < 15.0:
                    continue                           # the same line twice
                if d > STAIR_RISER_MAX:
                    break
                # same flight: overlapping extent, within a tread of each other
                if min(hi[j], hi[run[-1]]) - max(lo[j], lo[run[-1]]) < 0.6 * min(hi[j] - lo[j], hi[run[-1]] - lo[run[-1]]):
                    continue
                if d < STAIR_RISER_MIN or (step is not None and abs(d - step) > 25.0):
                    continue
                run.append(j)
                step = d if step is None else step
            if len(run) >= STAIR_MIN_TREADS:
                used[run] = True
                r = np.array(run)
                along = (lo[r].mean() + hi[r].mean()) / 2.0
                across = pos[r].mean()
                a0, a1, c0, c1 = float(lo[r].min()), float(hi[r].max()), float(pos[r].min()), float(pos[r].max())
                rb = (a0, c0, a1, c1) if horiz else (c0, a0, c1, a1)
                out.append(((along, across) if horiz else (across, along)) + (len(run), (rb, horiz, float((hi[r] - lo[r]).max()))))
    # one centre per flight: runs within 1 m of each other are one (the two
    # flights of a dog-leg are two centres; the one inside a staircase
    # traced from the other is skipped where they are traced)
    merged = []
    for x, y, n, run in sorted(out, key=lambda t: -t[2]):
        if any(math.hypot(x - m[0], y - m[1]) < 1000.0 for m in merged):
            continue
        merged.append((x, y, n, run) if with_runs else (x, y, n))
    return merged


def _geometry_family(d, segs_arr, fixtures):
    """Staircase or Toilet from what is drawn inside a boundary, or None."""
    bbox = d["bbox"]
    if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / 1e6 < 3.0:
        return None
    shape = d.get("poly")
    area = shoelace_area_sqm(shape) if shape else (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / 1e6
    # a terrace or open floor holding a flight of stairs is not a staircase
    # (H10 Tower 3 terrace: 834 sq.m renamed STAIRCASE)
    if area <= STAIR_FOUND_MAX_SQM and _stair_treads(segs_arr, bbox, d.get("poly")):
        return "Staircase"
    # A toilet block is never floor-sized: an open office or refuge area
    # traced round the toilets beside it holds their WCs too (H10 Tower 1:
    # a 983 sq.m refuge floor area renamed TOILET).
    if area > TOILET_GEOMETRY_MAX_SQM:
        return None
    n = 0
    for x, y in fixtures or ():
        if bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3] and (
                not shape or _point_in_polygon(x, y, shape)):
            n += 1
            if n >= TOILET_MIN_FIXTURES:
                return "Toilet"
    return None


# -- Per-floor passes (parallel) ---------------------------------------------
# How far past a floor's outline its segments are kept. The ray search
# runs 20 m; a little more keeps every wall a ray from inside can reach.
FLOOR_CLIP_MARGIN_MM = MAX_HV_SEARCH + 5000.0
# Floors run in worker processes when there are at least this many.
PARALLEL_MIN_FLOORS = 2


def _floor_clip_box(polygon):
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    m = FLOOR_CLIP_MARGIN_MM
    return (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)


def _seg_boxes(segs):
    """(N, 4) array of each segment's x1, y1, x2, y2."""
    import numpy as np
    if not segs:
        return np.zeros((0, 4))
    a = np.array([(s[0], s[1], s[2], s[3]) for s in segs], dtype=np.float64)
    return np.column_stack([np.minimum(a[:, 0], a[:, 2]),
                            np.minimum(a[:, 1], a[:, 3]),
                            np.maximum(a[:, 0], a[:, 2]),
                            np.maximum(a[:, 1], a[:, 3])])


def _box_mask(boxes, box):
    return ((boxes[:, 2] >= box[0]) & (boxes[:, 0] <= box[2])
            & (boxes[:, 3] >= box[1]) & (boxes[:, 1] <= box[3]))


def _segs_in_box(segs, boxes, box):
    import numpy as np
    if not len(boxes):
        return []
    return [segs[i] for i in np.nonzero(_box_mask(boxes, box))[0]]


def _segs_in_boxes(segs, boxes, box_list):
    import numpy as np
    if not len(boxes) or not box_list:
        return list(segs)
    keep = np.zeros(len(boxes), dtype=bool)
    for box in box_list:
        keep |= _box_mask(boxes, box)
    return [segs[i] for i in np.nonzero(keep)[0]]


def _move_texts(doc, msp, units, moves):
    """Move original text entities. `moves` is a list of
    {"handles": [...], "dx": mm, "dy": mm, "text": str, "x": mm, "y": mm}
    (x, y the position before the move). A text extracted before handles
    were recorded is found by its words and where it stood."""
    moved = not_movable = 0
    by_pos = None
    for mv in moves:
        dx = units.from_mm(float(mv.get("dx") or 0.0))
        dy = units.from_mm(float(mv.get("dy") or 0.0))
        if not dx and not dy:
            continue
        ents = []
        in_blocks = collections.OrderedDict()
        for h in mv.get("handles") or []:
            h = str(h)
            if h.startswith("B:"):
                _, path, th = h.split(":", 2)
                in_blocks.setdefault(path, []).append(th)
                continue
            e = doc.entitydb.get(h)
            if e is not None and e.dxftype() in ("TEXT", "MTEXT", "ATTRIB"):
                ents.append(e)
        block_moved = False
        for path, ths in in_blocks.items():
            if _move_block_texts(doc, msp, path.split("/"), ths, dx, dy):
                block_moved = True
        if block_moved and not ents:
            moved += 1
            continue
        if not ents and mv.get("text"):
            if by_pos is None:
                by_pos = [(e, units.point_to_mm(*_text_position(e)),
                           " ".join(_text_value(e).split()))
                          for e in msp.query("TEXT MTEXT")]
            want = " ".join(str(mv["text"]).split())
            x0, y0 = float(mv.get("x") or 0), float(mv.get("y") or 0)
            for e, (ex, ey), val in by_pos:
                if val and val in want and math.hypot(ex - x0, ey - y0) < 3000.0:
                    ents.append(e)
        if not ents:
            not_movable += 1
            continue
        for e in ents:
            try:
                e.translate(dx, dy, 0)
            except Exception:                         # noqa: BLE001
                continue
        moved += 1
    return {"moved": moved, "not_movable": not_movable}


def _move_block_texts(doc, msp, path, text_handles, dx, dy):
    """Move texts that live inside a block placed by the INSERTs `path`
    (outermost first) by (dx, dy) drawing units in the world.

    When every block on the path is placed only once, the text is moved
    inside the block definition and the block stays a block. Otherwise
    only this one placement is exploded into modelspace (its other copies
    elsewhere are untouched) and the exploded text is moved."""
    from ezdxf.math import Vec3
    inserts = [doc.entitydb.get(h) for h in path]
    texts = [doc.entitydb.get(h) for h in text_handles]
    texts = [t for t in texts if t is not None and t.dxftype() in ("TEXT", "MTEXT")]
    # An INSERT exploded by an earlier move is gone; the caller then finds
    # its (now modelspace) text by words and position.
    if not texts or any(i is None or not i.is_alive or i.dxftype() != "INSERT"
                        for i in inserts):
        return False
    m = None
    for ins in inserts:
        own = ins.matrix44()
        m = own if m is None else own @ m
    uses = collections.Counter(e.dxf.name for e in doc.entitydb.values()
                               if e.dxftype() == "INSERT" and e.is_alive)
    if all(uses[i.dxf.name] == 1 for i in inserts):
        inv = m.copy()
        inv.inverse()
        local = inv.transform_direction(Vec3(dx, dy, 0))
        for t in texts:
            t.translate(local.x, local.y, local.z)
        return True

    # Explode along the path. Where the text will stand once exploded:
    want = {}
    for t in texts:
        p = m.transform(Vec3(_text_position(t)))
        want[t.dxf.handle] = ((p.x, p.y), " ".join(_text_value(t).split()))
    # Where each nested INSERT lands once its parent is exploded (read
    # before exploding: an exploded INSERT is gone).
    nested = []
    outer = inserts[0].matrix44()
    for ins in inserts[1:]:
        p = outer.transform(Vec3(ins.dxf.insert))
        nested.append((ins.dxf.name, p))
        outer = ins.matrix44() @ outer
    found = list(inserts[0].explode())
    for name, p in nested:
        # the copy of this nested INSERT made by the explode above
        cands = [e for e in found if e.dxftype() == "INSERT" and e.dxf.name == name]
        if not cands:
            return False
        near = min(cands, key=lambda e: math.hypot(e.dxf.insert.x - p.x, e.dxf.insert.y - p.y))
        found = [e for e in found if e is not near] + list(near.explode())
    ok = False
    for (wx, wy), val in want.values():
        best = None
        for e in found:
            if e.dxftype() in ("TEXT", "MTEXT") and " ".join(_text_value(e).split()) == val:
                ex, ey = _text_position(e)
                d = math.hypot(ex - wx, ey - wy)
                if best is None or d < best[0]:
                    best = (d, e)
        if best is not None:
            best[1].translate(dx, dy, 0)
            ok = True
    return ok


LAST_PASS_RUN = {"floors": 0, "recomputed": 0}


def _run_floor_passes(jobs):
    """Run _floor_pass for every floor; several floors run at once.

    Returns (results in job order, number of worker processes used). Any
    failure of the pool itself falls back to running the floors here, one
    after another, so a machine that cannot start processes still works.
    """
    floor_jobs = jobs
    out = [None] * len(floor_jobs)
    # A floor already worked out with exactly these inputs - by a run that
    # was stopped, or by the last Generate when only another floor's
    # choices changed - is not worked out again.
    todo = []
    # each floor's key worked out once, before its pass, for the look-up
    # and the store alike
    keys = [_pass_key(j) if _PASS_CACHE_DIR else None for j in floor_jobs]
    for i, j in enumerate(floor_jobs):
        cached = _pass_cache_get(j, keys[i])
        if cached is not None:
            out[i] = cached
        else:
            todo.append(i)
    # how many floors this run worked out afresh (the rest came from cache)
    LAST_PASS_RUN.update({"floors": len(floor_jobs), "recomputed": len(todo)})
    if not todo:
        return out, 0

    def done(i, res):
        out[i] = res
        _pass_cache_put(floor_jobs[i], res, keys[i])

    if len(todo) < PARALLEL_MIN_FLOORS:
        for i in todo:
            _jobs.check()
            done(i, _floor_pass(floor_jobs[i]))
        return out, 0
    workers = max(1, min(len(todo), (os.cpu_count() or 2) - 1, 8))
    try:
        import concurrent.futures as cf
        pool = cf.ProcessPoolExecutor(max_workers=workers)
    except Exception:                                 # noqa: BLE001
        pool = None
    if pool is None:
        for i in todo:
            _jobs.check()
            done(i, _floor_pass(floor_jobs[i]))
        return out, 0
    try:
        futs = {pool.submit(_floor_pass, floor_jobs[i]): i for i in todo}
        pending = set(futs)
        while pending:
            finished, pending = cf.wait(pending, timeout=0.5,
                                        return_when=cf.FIRST_COMPLETED)
            for f in finished:
                done(futs[f], f.result())
            _jobs.check()
        pool.shutdown(wait=True)
        return out, workers
    except _jobs.Stopped:
        # Stop: drop the floors not started and end the ones running now,
        # rather than waiting for them. Finished floors are cached above.
        _kill_pool(pool)
        raise
    except Exception:                                 # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception(
            "parallel floor pass failed; running the rest one by one")
        _kill_pool(pool)
        for i in todo:
            if out[i] is None:
                _jobs.check()
                done(i, _floor_pass(floor_jobs[i]))
        return out, 0



def _floor_columns(doc, msp, units, floors, segs=None, rcc_walls=True):
    """{"columns": {floor: [points]}, "rejects": {floor: [points]}}: the
    columns the geometry engine finds (one group on a grid), and the C / L
    shapes on their layer that are walls - cut out of / added to the walls
    drawn on AI_WALL_*. segs: the raw segments, so a thin piece lying in a
    wall line is not taken for a column."""
    try:
        from . import geoengine as _ge
        rej = {}
        cols = _ge._column_shapes(doc, msp, units, floors, rejected=rej, segs=segs, rcc_walls=rcc_walls)
    except Exception:                                 # noqa: BLE001
        return {"columns": {}, "rejects": {}}
    pts = lambda pg: [tuple(map(float, c)) for c in list(pg.exterior.coords)[:-1]]
    return {"columns": {f: [pts(pg) for pg, _l, _h in lst] for f, lst in cols.items()},
            "rejects": {f: [pts(pg) for pg in lst] for f, lst in rej.items()}}


# -- Slab-edge lines ----------------------------------------------------------
# A long single line on a detail or section layer that runs through X-marked
# shafts is the edge of the slab (285 FD / P285: a 13.8 m line on a-details
# through the STAIR PREZ and CONDENSER shafts by the east balcony), not a
# wall: a wall never runs through a shaft. It bounds no room and no wall, so
# the balcony beside it runs to the wall face. Detail layers carrying real
# walls elsewhere are untouched (see the note on 'DETAIL' in the annotation
# layers above). config.json "slab_edge_lines".
_SE = CONFIG.get("slab_edge_lines") or {}
SLAB_EDGE_ON = bool(_SE.get("enabled", True))
SLAB_EDGE_LAYER_RE = re.compile(str(_SE.get("layer_regex", r"DETAIL|DETL|SECTION|SLAB\W*EDGE")), re.I)
SLAB_EDGE_MIN_MM = float(_SE.get("min_length_mm", 3000.0))
SLAB_EDGE_THROUGH_MM = float(_SE.get("through_x_mm", 200.0))


def drop_slab_edge_lines(segs, x_boxes):
    """segs less the slab-edge lines: a line on a detail / section layer at
    least SLAB_EDGE_MIN_MM long that runs SLAB_EDGE_THROUGH_MM or more
    through the inside of an X-marked box (x_boxes: (x0, y0, x1, y1), mm).
    Returns (segs, the lines dropped)."""
    if not segs or not x_boxes:
        return segs, []
    from shapely.geometry import LineString, box as _bx
    boxes = []
    for b in x_boxes:
        try:
            x0, y0, x1, y1 = [float(v) for v in b[:4]]
        except (TypeError, ValueError, IndexError):
            continue
        if x1 - x0 > 120.0 and y1 - y0 > 120.0:
            boxes.append((x0, y0, x1, y1))
    if not boxes:
        return segs, []
    dropped = set()
    for i, s in enumerate(segs):
        try:
            L = math.hypot(s[2] - s[0], s[3] - s[1])
        except (TypeError, IndexError):
            continue
        if L < SLAB_EDGE_MIN_MM or not SLAB_EDGE_LAYER_RE.search(str(s[4] or "")):
            continue
        sx0, sx1 = min(s[0], s[2]), max(s[0], s[2])
        sy0, sy1 = min(s[1], s[3]), max(s[1], s[3])
        ln = None
        for x0, y0, x1, y1 in boxes:
            if sx1 < x0 or sx0 > x1 or sy1 < y0 or sy0 > y1:
                continue
            ln = ln or LineString([(s[0], s[1]), (s[2], s[3])])
            inner = _bx(x0 + 50.0, y0 + 50.0, x1 - 50.0, y1 - 50.0)
            if ln.intersection(inner).length >= SLAB_EDGE_THROUGH_MM:
                dropped.add(i)
                break
    if not dropped:
        return segs, []
    kept = type(segs)() if isinstance(segs, _SegList) else []
    for i, s in enumerate(segs):
        if i not in dropped:
            kept.append(s)
    if isinstance(segs, _SegList):
        kept.dashed = segs.dashed
    return kept, [segs[i] for i in sorted(dropped)]


def _x_region_layer(layer):
    """A layer an X region may be read on (step 1 of the X sequence): any,
    but a column, symbol, grid or hatch layer (xregions.NOT_X_LAYER_WORDS)."""
    lu = (layer or "").upper()
    return not any(w in lu for w in xregions.NOT_X_LAYER_WORDS)


def _prepare_floors(doc, msp, units, floors, rule):
    """What every floor's pass needs from the whole drawing: the segments
    round the chosen floors, the X shafts and voids, the fixtures and the
    door swings. The slow part of a Generate before any room is found."""
    raw_segs = _collect_raw_segs(doc, msp, units)
    dashed = frozenset(getattr(raw_segs, "dashed", ()) or ())
    # Only the chosen floors are worked on, so everything else on the sheet
    # is dropped here: the other floors of a multi-floor sheet, title
    # blocks, elevations. The X detection below runs on what is left.
    seg_boxes = _seg_boxes(raw_segs)
    keep_boxes = [_floor_clip_box(f["polygon"]) for f in floors]
    raw_segs = _segs_in_boxes(raw_segs, seg_boxes, keep_boxes)
    wall_hatches = _collect_wall_hatches(doc, msp, units)
    # every piece of them, for the wall left between the rooms (wall_gaps_filled)
    wall_hatch_islands = _collect_wall_hatches(doc, msp, units, islands=True) \
        if rule["wall_gaps_filled"] else []
    all_hatches = _collect_all_hatches(doc, msp, units) if rule["wall_trace"] else []
    wall_arcs = _collect_wall_arcs(doc, msp, units) if rule["wall_trace"] else []
    door_arcs = _collect_door_arcs(doc, msp, units) if DRAW_DOORS else []
    window_evidence = _collect_window_evidence(doc, msp, units) if WINDOW_GAPS else []
    from . import elements as _el
    glass_segs = _el.collect_glass(doc, msp, units) if _el.ENABLED else []
    columns = _floor_columns(doc, msp, units, floors, segs=raw_segs, rcc_walls=rule["rcc_walls_on_column_layer"])
    # A wall with an object drawn under it is above floor level (a beam, a
    # bulkhead): the whole element leaves the walls before anything else
    # reads them - its hatch, its outline, the C / L shape it makes on the
    # column layer - so the room runs under it (find_hanging_walls).
    hanging = []
    if rule["hanging_walls"]:
        try:
            hanging = find_hanging_walls(doc, msp, units, wall_hatches)
            raw_segs, wall_hatches, all_hatches, columns = drop_hanging_walls(
                hanging, raw_segs, wall_hatches, all_hatches, columns)
            if wall_hatch_islands:
                _s, wall_hatch_islands, _a, _c = drop_hanging_walls(hanging, None, wall_hatch_islands)
        except Exception:                             # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("hanging walls")
            hanging = []
    seg_boxes = _seg_boxes(raw_segs)

    # Step 1 of the X sequence (xplace, 2026-10-16, final): every X mark on
    # the floor, found once - two diagonals crossing at their middles, on any
    # layer, whatever their size. The cut-out layers' X marks first (their
    # pairs as found there), then every other pair by its shape. Every X is
    # a void to start with; xplace.resolve_x decides each one, once, in the
    # floor pass.
    diag_segs_v3 = _collect_diagonals_layered(raw_segs)
    x_pairs_v3 = _find_x_pairs(diag_segs_v3)
    diag_segs, x_pairs, detection_mode = list(diag_segs_v3), list(x_pairs_v3), "geometry"
    try:
        any_diags = _collect_diagonals_anywhere(doc, msp, units, segs=raw_segs, any_layer=True)
        have = {_seg_key(d[:4]) for d in diag_segs}
        for i, j in _find_x_pairs(any_diags):
            a, b = any_diags[i], any_diags[j]
            if _seg_key(a[:4]) in have or _seg_key(b[:4]) in have:
                continue
            if math.hypot(a[4] - b[4], a[5] - b[5]) > 0.05 * a[7]:
                continue                              # not crossing at their middles
            diag_segs.extend([a, b])
            x_pairs.append((len(diag_segs) - 2, len(diag_segs) - 1))
    except Exception:                                 # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("X marks by shape")

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

    # Tolerant X regions (xregions): the X marks the pair test above
    # misses - a trimmed or broken diagonal, an L-shaped cut-out, a shaft
    # or void past the 5 m diagonal limit - on any layer (the X sequence's
    # step 1, 2026-10-16: Centaurus's PHE strips, 300 mm x 5-10 m, are on
    # no cut-out layer), as the geometry engine reads them; only layers
    # plainly no building element (columns, symbols, grids, hatches) are
    # left out. Every X line is kept out of the walls; a region is added
    # only where no X was found already.
    if True:
        for rg in xregions.find_x_regions(raw_segs, X_DIAG_MIN_LEN, X_DIAG_MIN_AXIS,
                                          layer_ok=_x_region_layer):
            for k in rg["seg_idx"]:
                x_lines.add(_seg_key(raw_segs[k]))
            b = rg["bbox"]
            ra = (b[2] - b[0]) * (b[3] - b[1])
            dup = False
            for cl in x_clusters:
                cb = cl["bbox"]
                w = min(b[2], cb[2]) - max(b[0], cb[0])
                h = min(b[3], cb[3]) - max(b[1], cb[1])
                ca = (cb[2] - cb[0]) * (cb[3] - cb[1])
                if w > 0 and h > 0 and w * h >= 0.5 * max(ra, ca):
                    dup = True
                    break
            if dup:
                continue
            x_clusters.append({"cx": rg["cx"], "cy": rg["cy"],
                               "mn_x": b[0], "mn_y": b[1],
                               "mx_x": b[2], "mx_y": b[3],
                               "w": b[2] - b[0], "h": b[3] - b[1],
                               "bbox": b, "region": True,
                               "poly": rg["poly"],
                               "area_sqm": rg["area_sqm"],
                               "diag_len": max([raw_segs[k][5] for k in rg["seg_idx"]] or [0.0])})
    # The slab edge running through the shafts is no wall and no room side.
    slab_edges = []
    if rule["slab_edge_not_boundary"] and SLAB_EDGE_ON:
        raw_segs, slab_edges = drop_slab_edge_lines(raw_segs, [cl["bbox"] for cl in x_clusters if cl.get("bbox")])
        if slab_edges:
            seg_boxes = _seg_boxes(raw_segs)
    # A void follows its own closed outline where the drawing has one.
    void_outlines = []
    for e in msp.query("LWPOLYLINE POLYLINE"):
        try:
            if not xregions.is_x_layer(e.dxf.layer) or not _is_closed(e):
                continue
            pts = _polyline_points(e, units)
        except Exception:                             # noqa: BLE001
            continue
        if len(pts) >= 3 and shoelace_area_sqm(pts) > X_VOID_AREA_THRESHOLD:
            void_outlines.append(pts)
    try:
        fixtures = _collect_fixtures(doc, msp, units)
    except Exception:                                 # noqa: BLE001
        fixtures = []

    # Walls, doors and rooms are found floor by floor (_floor_pass),
    # from the segments around each floor. Door swings are collected once;
    # the X-shaft diagonals above were found on the same cropped segments.
    all_swings = (_collect_door_swings(doc, msp, units)
                  if rule["close_doors"] else [])
    return {"raw_segs": raw_segs, "seg_boxes": seg_boxes, "dashed": dashed,
            "diag_segs": diag_segs, "x_pairs": x_pairs,
            "detection_mode": detection_mode, "x_clusters": x_clusters,
            "x_lines": x_lines, "void_outlines": void_outlines,
            "fixtures": fixtures, "all_swings": all_swings,
            "circles": _collect_circles(doc, msp, units),
            "stair_marks": _collect_stair_marks(doc, msp, units),
            "columns": columns,
            "wall_hatches": wall_hatches,
            "wall_hatch_islands": wall_hatch_islands,
            "all_hatches": all_hatches,
            "wall_arcs": wall_arcs,
            "door_arcs": door_arcs,
            "window_evidence": window_evidence,
            "glass_segs": glass_segs,
            "slab_edges": [tuple(sg[:5]) for sg in slab_edges],
            "hanging_walls": [{"pts": [tuple(map(float, c)) for c in list(f["poly"].exterior.coords)[:-1]],
                               "holes": [[tuple(map(float, c)) for c in list(r.coords)[:-1]]
                                         for r in f["poly"].interiors],
                               "layer": f["layer"], "evidence": f["evidence"], "area_sqm": f["area_sqm"]}
                              for f in hanging if f["poly"].geom_type == "Polygon"]}


def _prepared(src_path, unit_override, floors, rule, moved, build):
    """_prepare_floors, kept on disk beside the per-floor cache: the same
    drawing, plates and rules give the same answer, so a second Generate -
    or a Restart after Stop - skips it. Not kept when texts were moved in
    the Geometry tab, as the drawing itself then differs."""
    path = None
    if _PASS_CACHE_DIR and not moved:
        import hashlib, pickle
        try:
            st = os.stat(src_path)
            key = repr((os.path.abspath(src_path), st.st_size, st.st_mtime_ns,
                        unit_override, "x-sequence-2", rule.get("close_doors"),
                        rule.get("hanging_walls", True), rule.get("wall_trace"),
                        rule.get("slab_edge_not_boundary", True), rule.get("wall_gaps_filled", True),
                        rule.get("rcc_walls_on_column_layer", True),
                        sorted(tuple(round(v, 1) for v in _floor_clip_box(f["polygon"]))
                               for f in floors), _code_stamp()))
            path = os.path.join(_PASS_CACHE_DIR, "prep_" +
                                hashlib.sha1(key.encode("utf-8")).hexdigest() + ".pkl")
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    return pickle.load(fh)
        except Exception:                             # noqa: BLE001
            path = None
    _jobs.check()
    out = build()
    _jobs.check()
    if path:
        import pickle
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "wb") as fh:
                pickle.dump(out, fh, protocol=4)
            os.replace(path + ".tmp", path)
        except Exception:                             # noqa: BLE001
            pass
    return out

def _shaft_cutter(shape, centre, wall_mass):
    """What a shaft may cut out of a room (cut_shafts_from_rooms): its
    shape less the walls (wall hatches and strips, wall_mass), the piece
    holding its centre - else the largest; the whole shape when nothing
    is left. Never area across a wall."""
    from shapely.geometry import Point
    own = shape.difference(wall_mass) if wall_mass is not None and not wall_mass.is_empty else shape
    parts = [q for q in getattr(own, "geoms", [own]) if q.geom_type == "Polygon" and q.area > 0]
    if not parts:
        return shape                                  # all wall: the shape as it is
    c = Point(*centre)
    held = [q for q in parts if q.buffer(1.0).contains(c)]
    return held[0] if held else max(parts, key=lambda q: q.area)


def _turned_x_outline(cl, off_mm=60.0, tol=0.05):
    """(polygon points, sq.m) of a single X drawn turned off the axes - its
    four ends off the corners of its box by more than off_mm, its two
    diagonals of one length crossing at their middles: the turned rectangle
    the ends make. None for an X on the axes, or a group of X marks."""
    ends = cl.get("x_ends") or ()
    if len(ends) != 1 or cl.get("region"):
        return None
    p = ends[0]                                   # a1, b1, a2, b2: round the rectangle
    x0, y0, x1, y1 = cl["bbox"]
    corners = ((x0, y0), (x0, y1), (x1, y0), (x1, y1))
    if max(min(math.hypot(px - cx_, py - cy_) for cx_, cy_ in corners) for px, py in p) <= off_mm:
        return None
    d1 = math.hypot(p[2][0] - p[0][0], p[2][1] - p[0][1])
    d2 = math.hypot(p[3][0] - p[1][0], p[3][1] - p[1][1])
    if d1 <= 0 or d2 <= 0 or abs(d1 - d2) > tol * max(d1, d2):
        return None
    m1 = ((p[0][0] + p[2][0]) / 2.0, (p[0][1] + p[2][1]) / 2.0)
    m2 = ((p[1][0] + p[3][0]) / 2.0, (p[1][1] + p[3][1]) / 2.0)
    if math.hypot(m1[0] - m2[0], m1[1] - m2[1]) > tol * d1:
        return None
    pts = [(float(a), float(b)) for a, b in p]
    area = 0.0
    for i in range(4):
        area += pts[i][0] * pts[(i + 1) % 4][1] - pts[(i + 1) % 4][0] * pts[i][1]
    area = abs(area) / 2.0
    if area <= 0:
        return None
    return pts, area / 1e6


def _settle_x_overlaps(validated, stats, share=0.2):
    """validated X records, each overlap between two of them given to the
    one with the longer X symbol (diag_len; a tie to the X that is not a
    region). The loser keeps the rest of its shape when that is a shape
    (a polygon of 0.02 sq.m and up), else it is dropped."""
    from shapely.geometry import Polygon, box as _b

    def shape(v):
        try:
            g = (Polygon(v["poly"]) if v.get("poly") and len(v["poly"]) >= 3 else _b(*v["bbox"])).buffer(0)
            return g if not g.is_empty else None
        except (ValueError, TypeError):
            return None
    sh = [shape(v) for v in validated]
    gone = set()
    for i in range(len(validated)):
        for j in range(i + 1, len(validated)):
            if i in gone or j in gone or sh[i] is None or sh[j] is None:
                continue
            if not sh[i].intersects(sh[j]):
                continue
            ov = sh[i].intersection(sh[j]).area
            if ov < share * min(sh[i].area, sh[j].area):
                continue
            a, b = validated[i], validated[j]
            ka = (round(a.get("diag_len") or 0.0), not a.get("region"))
            kb = (round(b.get("diag_len") or 0.0), not b.get("region"))
            win, lose = (i, j) if ka >= kb else (j, i)
            rest = sh[lose].difference(sh[win].buffer(1.0, join_style=2))
            parts = [q for q in getattr(rest, "geoms", [rest]) if q.geom_type == "Polygon" and q.area >= 0.02e6]
            if not parts:
                gone.add(lose)
                stats["x_overlap_dropped"] += 1
                continue
            q = max(parts, key=lambda x: x.area).simplify(1.0, preserve_topology=True)
            v = validated[lose]
            v["poly"] = [(float(x), float(y)) for x, y in list(q.exterior.coords)[:-1]]
            v["bbox"] = tuple(float(c) for c in q.bounds)
            v["cx"], v["cy"] = (q.bounds[0] + q.bounds[2]) / 2.0, (q.bounds[1] + q.bounds[3]) / 2.0
            if len(v["poly"]) == 4:
                v["poly"] = None                      # a rectangle: its box
            sh[lose] = q
            stats["x_overlap_trimmed"] += 1
    return [v for k, v in enumerate(validated) if k not in gone]


def _x_merge_shape(x, other):
    """The outline an X keeps when a traced room merges into it (Phase D):
    its own marks - (bbox, poly, holes, xrects). When those cover under
    half of the room (an L of two X strips along a shaft's sides, the
    room traced from its PHE-SHAFT label being the shaft between them),
    the X marks strips of the room: the room's outline is the shaft."""
    from shapely.geometry import Polygon, box as _b
    from shapely.ops import unary_union

    def sh(d, marks):
        try:
            if d.get("poly"):
                g = Polygon(d["poly"], [h for h in d.get("holes") or () if len(h) >= 3])
            elif marks and d.get("xrects") and len(d["xrects"]) > 1:
                g = unary_union([_b(*r) for r in d["xrects"]])
            else:
                g = _b(*d["bbox"])
            g = g.buffer(0)
            return g if not g.is_empty else None
        except (ValueError, TypeError):
            return None
    keep = (x["bbox"], x.get("poly"), x.get("holes") or [], x.get("xrects"))
    gx, go = sh(x, True), sh(other, False)
    if gx is None or go is None or go.area <= 0 or not other.get("poly"):
        return keep
    if gx.area >= 0.95 * _b(*x["bbox"]).area:
        return keep                                   # a plain X box
    if gx.intersection(go).area < 0.5 * go.area:
        # within the X's extent: the marks bound the shaft
        g = go.intersection(_b(*x["bbox"]))
        parts = [q for q in getattr(g, "geoms", [g]) if q.geom_type == "Polygon" and q.area > 0]
        if not parts:
            return keep
        g = max(parts, key=lambda q: q.area).simplify(1.0, preserve_topology=True)
        return (tuple(float(v) for v in g.bounds),
                [(float(px), float(py)) for px, py in list(g.exterior.coords)[:-1]],
                [[(float(px), float(py)) for px, py in list(r.coords)[:-1]] for r in g.interiors], None)
    return keep


def _hatch_edges(hatch_pgs, clip=None):
    """The outline of every wall hatch (outer ring and holes) as wall
    segments, within the box clip (x0, y0, x1, y1) when given."""
    out = []
    for g in hatch_pgs or ():
        if clip is not None:
            b = g.bounds
            if b[2] < clip[0] or b[0] > clip[2] or b[3] < clip[1] or b[1] > clip[3]:
                continue
        for part in getattr(g, "geoms", [g]):
            if part.geom_type != "Polygon":
                continue
            for ring in [part.exterior] + list(part.interiors):
                cs = list(ring.coords)
                for k in range(len(cs) - 1):
                    (ax, ay), (bx, by) = cs[k][:2], cs[k + 1][:2]
                    if math.hypot(bx - ax, by - ay) > 1.0:
                        out.append((ax, ay, bx, by))
    return out


def _face_line_closers(raw_segs, wall_lines, skip=None, blockers=()):
    """Doorways closed by a line drawn across them on the wall's face line.

    For every drawn line (any layer): the wall lines in line with it
    (parallel within 3 degrees, within FACE_LINE_TOL_MM of its line) are
    found either side; when none covers its middle, the gap between the
    nearest wall end on each side is an opening. The opening is closed -
    straight across between the two wall ends - when it is a doorway's
    width (FACE_LINE_GAP_MM), the line covers FACE_LINE_COVER of it, and
    the line is no longer than the opening (so a long overlay or finish
    line running past many openings closes none of them).

    blockers: shapely shapes (lifts and shafts) - an opening in front of
    one (across its width, within a wall's thickness) is not closed, so
    closing a doorway never makes a lift or a shaft bigger.
    Returns [(x1, y1, x2, y2)]."""
    from shapely.geometry import LineString
    from shapely.strtree import STRtree
    gmin, gmax = FACE_LINE_GAP_MM
    tol = FACE_LINE_TOL_MM
    walls = [LineString([(w[0], w[1]), (w[2], w[3])]) for w in wall_lines
             if math.hypot(w[2] - w[0], w[3] - w[1]) > 1.0]
    if not walls:
        return []
    tree = STRtree(walls)
    btree = STRtree(list(blockers)) if blockers else None
    cos3 = math.cos(math.radians(3.0))
    out, seen = [], set()
    for sg in raw_segs:
        L = math.hypot(sg[2] - sg[0], sg[3] - sg[1])
        if L < FACE_LINE_COVER * gmin or L > 1.25 * gmax:
            continue
        if skip and _seg_key(sg) in skip:
            continue
        ax, ay = sg[0], sg[1]
        ux, uy = (sg[2] - ax) / L, (sg[3] - ay) / L
        ext = LineString([(ax - ux * gmax, ay - uy * gmax), (sg[2] + ux * gmax, sg[3] + uy * gmax)])
        lo, hi, covered = None, None, False
        for k in tree.query(ext.buffer(tol)):
            w = walls[int(k)]
            (wx0, wy0), (wx1, wy1) = w.coords[0], w.coords[-1]
            wl = w.length
            if abs(ux * (wx1 - wx0) + uy * (wy1 - wy0)) / wl < cos3:
                continue
            if max(abs((wx0 - ax) * -uy + (wy0 - ay) * ux), abs((wx1 - ax) * -uy + (wy1 - ay) * ux)) > tol:
                continue
            t0 = (wx0 - ax) * ux + (wy0 - ay) * uy
            t1 = (wx1 - ax) * ux + (wy1 - ay) * uy
            t0, t1 = min(t0, t1), max(t0, t1)
            if t0 < L / 2.0 < t1:
                covered = True                        # a wall runs here: no opening
                break
            if t1 <= L / 2.0:
                lo = t1 if lo is None else max(lo, t1)
            elif t0 >= L / 2.0:
                hi = t0 if hi is None else min(hi, t0)
        if covered or lo is None or hi is None:
            continue
        G = hi - lo
        if not (gmin <= G <= gmax) or L > 1.25 * G:
            continue
        if min(L, hi) - max(0.0, lo) < FACE_LINE_COVER * G:
            continue
        p0 = (ax + ux * lo, ay + uy * lo)
        p1 = (ax + ux * hi, ay + uy * hi)
        key = (round(p0[0] / 10), round(p0[1] / 10), round(p1[0] / 10), round(p1[1] / 10))
        if key in seen or (key[2], key[3], key[0], key[1]) in seen:
            continue
        blocked = False
        if btree is not None:
            ln = LineString([p0, p1])
            near = ln.buffer(WALL_T_MAX + 50.0, cap_style=2)
            for b in btree.query(near):
                sh = blockers[int(b)]
                part = sh.intersection(near)
                if part.is_empty:
                    continue
                # across the opening: the shape's extent along it covers half of it
                pb = part.bounds
                span = (pb[2] - pb[0]) if abs(ux) >= abs(uy) else (pb[3] - pb[1])
                if span >= 0.5 * G:
                    blocked = True
                    break
        else:
            blocked = False
        seen.add(key)
        out.append(((p0[0], p0[1], p1[0], p1[1]), blocked, G))
    # in front of a lift or shaft a closing line stays only with the line
    # on the wall's other face (both faces closed: the doorway's depth
    # goes to neither side, so the lift or shaft does not grow by it)
    kept = []
    for i, (ln, blocked, G) in enumerate(out):
        if blocked:
            ux, uy = (ln[2] - ln[0]) / G, (ln[3] - ln[1]) / G
            twin = False
            for j, (m, _b, H) in enumerate(out):
                if j == i or abs(H - G) > 0.2 * G:
                    continue
                mx, my = (m[0] + m[2]) / 2.0, (m[1] + m[3]) / 2.0
                off = abs((mx - ln[0]) * -uy + (my - ln[1]) * ux)
                along = (mx - ln[0]) * ux + (my - ln[1]) * uy
                if 20.0 < off <= WALL_T_MAX + 50.0 and 0.0 <= along <= G:
                    twin = True
                    break
            if not twin:
                continue
        kept.append(ln)
    return kept


def _slit_closers(ends_of, targets, max_mm=None, touch_mm=None):
    """Lines closing the slits: an end of a wall face (or cap) standing
    free - nothing within touch_mm of it but its own line - with another
    wall line (a face, a cap, a wall hatch's edge, a settled outline)
    closer than max_mm. A doorway is 300 mm and up, so it is untouched.
    ends_of: [(x1, y1, x2, y2)] whose ends are looked at; targets: every
    wall line. Returns [(x1, y1, x2, y2)]."""
    from shapely.geometry import LineString, Point
    from shapely.strtree import STRtree
    from shapely.ops import nearest_points
    max_mm = SLIT_MAX_MM if max_mm is None else max_mm
    touch_mm = SLIT_TOUCH_MM if touch_mm is None else touch_mm
    if max_mm <= 0 or not ends_of:
        return []
    lines = [LineString([(t[0], t[1]), (t[2], t[3])]) for t in targets
             if math.hypot(t[2] - t[0], t[3] - t[1]) > 1.0]
    if not lines:
        return []
    tree = STRtree(lines)
    bounds = [ln.bounds for ln in lines]
    pad = 0.5 + 1e-6
    out, seen = [], set()
    for sg in ends_of:
        own = LineString([(sg[0], sg[1]), (sg[2], sg[3])])
        if own.length <= 1.0:
            continue
        ob = own.bounds
        for ex, ey in ((sg[0], sg[1]), (sg[2], sg[3])):
            key = (round(ex), round(ey))
            if key in seen:
                continue
            seen.add(key)
            pt = Point(ex, ey)
            best, joined = None, False
            for k in tree.query(pt.buffer(max_mm)):
                ln = lines[int(k)]
                lb = bounds[int(k)]
                # its own line: only a line whose box holds this one's
                # (to the 0.5 mm of the test) can be it
                if (ob[0] >= lb[0] - pad and ob[2] <= lb[2] + pad and ob[1] >= lb[1] - pad
                        and ob[3] <= lb[3] + pad
                        and (ln.equals(own) or ln.buffer(0.5).contains(own))):
                    continue                          # its own line
                d = ln.distance(pt)
                if d <= touch_mm:
                    joined = True                     # the end meets a wall
                    break
                if d < max_mm and (best is None or d < best[0]):
                    best = (d, ln)
            if joined or best is None:
                continue
            q = nearest_points(pt, best[1])[1]
            out.append((ex, ey, q.x, q.y))
    return out


def _outline_edges(poly, bbox):
    """The sides of a settled boundary, as wall segments."""
    if poly and len(poly) >= 3:
        pts = [tuple(p) for p in poly]
    else:
        x0, y0, x1, y1 = bbox
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [(pts[i][0], pts[i][1], pts[(i + 1) % len(pts)][0], pts[(i + 1) % len(pts)][1])
            for i in range(len(pts))]


def _kill_pool(pool):
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:                                 # noqa: BLE001
        pass
    for proc in list((getattr(pool, "_processes", None) or {}).values()):
        try:
            proc.terminate()
        except Exception:                             # noqa: BLE001
            pass


# -- Per-floor cache ---------------------------------------------------------
# One floor's pass is a pure function of its job: the segments round the
# floor, its texts, the user's choices for it and the rules. Keyed by a
# hash of all of that (and of this file, so a new version of the program
# never reads an old result), it is kept beside the outputs; a stopped run
# restarts from the floors it had not finished, and regenerating after a
# change on one floor only works that floor out again.
_PASS_CACHE_DIR = None


def set_pass_cache_dir(path):
    global _PASS_CACHE_DIR
    _PASS_CACHE_DIR = path


# What a floor pass reads: every engine module it imports, the family rules,
# and the config sections those modules use. Hashed by content, not by
# timestamp, so saving a file unchanged - or editing the area statement,
# the screens, the reports - does not throw the cached passes away. The
# tracer, wall trace and confidence modules are in the list: a change to
# them used to leave old passes in the cache.
_PASS_MODULES = ("dxf_reader.py", "roomtrace.py", "walltrace.py", "elements.py", "confidence.py", "labels.py",
                 "rules.py", "geometry.py", "xregions.py", "dxfio.py", "levels.py", "units.py",
                 "layernames.py", "layers.py", "userrules.py", "geoengine.py", "jobs.py", "config.py",
                 "xplace.py", "elementid.py", "locked.py", "spaces.py", "family_rules.json")
# config.json sections only read after the floor passes
_PASS_CONFIG_SKIP = ("area_statement", "facade")
_STAMP = []


def _code_stamp():
    if _STAMP:
        return _STAMP[0]
    import hashlib
    here = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha1()
    for name in _PASS_MODULES:
        try:
            with open(os.path.join(here, name), "rb") as fh:
                h.update(name.encode("utf-8") + b"\0" + fh.read())
        except OSError:
            h.update(name.encode("utf-8") + b"\0missing")
    try:
        with open(os.path.join(here, "config.json"), "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        used = {k: v for k, v in cfg.items() if not k.startswith("_") and k not in _PASS_CONFIG_SKIP}
        h.update(json.dumps(used, sort_keys=True).encode("utf-8"))
    except (OSError, ValueError):
        h.update(b"config.json missing")
    _STAMP.append("v2:" + h.hexdigest())
    return _STAMP[0]


_PLAIN = frozenset((int, float, str, bool, type(None), bytes))


def _canonical(obj):
    """The job in a form that pickles the same every time: a set's order
    depends on how it was built (fresh, or loaded from the prep cache), so
    sets become sorted lists and dicts sorted pairs. Only the cache key is
    made from this; the floor pass gets the job as it is."""
    t = type(obj)
    if t is tuple or t is list:
        # a row of plain values (a segment, a point) is its own form - the
        # floor's 200,000 segments are not rebuilt one value at a time
        for v in obj:
            if type(v) not in _PLAIN:
                break
        else:
            return obj
    if isinstance(obj, dict):
        return ("d", sorted(((repr(k), _canonical(v)) for k, v in obj.items()), key=lambda kv: kv[0]))
    if isinstance(obj, (set, frozenset)):
        items = [_canonical(v) for v in obj]
        return ("s", sorted(items, key=repr))
    if isinstance(obj, (list, tuple)):
        return type(obj)(_canonical(v) for v in obj)
    return obj


def _pass_key(job):
    import hashlib, pickle
    h = hashlib.sha1(_code_stamp().encode("utf-8"))
    try:
        h.update(pickle.dumps(_canonical(job), protocol=4))
    except Exception:                                 # noqa: BLE001
        return None
    return h.hexdigest()


def _pass_cache_path(job, key=None):
    if not _PASS_CACHE_DIR:
        return None
    key = key or _pass_key(job)
    return os.path.join(_PASS_CACHE_DIR, "pass_" + key + ".pkl") if key else None


def _pass_cache_get(job, key=None):
    path = _pass_cache_path(job, key)
    if not path or not os.path.exists(path):
        return None
    import pickle
    try:
        with open(path, "rb") as fh:
            return pickle.load(fh)
    except Exception:                                 # noqa: BLE001
        return None


def _pass_cache_put(job, res, key=None):
    path = _pass_cache_path(job, key)
    if not path:
        return
    import pickle
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            pickle.dump(res, fh, protocol=4)
        os.replace(tmp, path)
    except Exception:                                 # noqa: BLE001
        pass


def _x_log_row(rec):
    """One X mark of the X sequence, for the run report: where it is, what
    it was decided to be (cls; None for one standing where another X is),
    at which step and why, and what every step before made of it."""
    f = rec.get("facts") or {}
    return {"bbox": rec.get("bbox"), "cls": rec.get("cls"), "step": rec.get("step"),
            "why": rec.get("why"), "label": rec.get("label"),
            "trail": [list(t) for t in rec.get("trail") or ()],
            "size_mm": [round(f.get("w") or 0), round(f.get("h") or 0)],
            "area_sqm": round(f.get("area_sqm") or 0.0, 3),
            "gap_mm": f.get("gap"), "sides": f.get("sides"),
            "in_wall": f.get("in_wall"), "column": f.get("column"), "window": f.get("window"),
            "round": f.get("round"), "window_tag": f.get("wtag"), "cutout_tag": f.get("ctag"),
            "door_tag": f.get("dtag"), "cut_back_to": rec.get("cut_back_to")}


def _floor_pass(job):
    """One floor's rooms with that floor's family rules in force (see
    _floor_pass_rules); the module's own rules are restored after."""
    global _FAMILY_RULES, _LABEL_RULES
    saved = (_FAMILY_RULES, _LABEL_RULES)
    try:
        return _floor_pass_rules(job)
    finally:
        _FAMILY_RULES, _LABEL_RULES = saved


def _floor_pass_rules(job):
    """Find one floor's rooms: walls, doors and the room search.

    Runs in a worker process when several floors are drawn at once, so it
    takes everything it needs in `job` and returns plain data; the drawing
    itself is written by annotate_dxf in the parent. The segments and door
    swings are only those around this floor - a floor's rooms are bounded
    by its own walls and its outline, and detecting walls across the
    whole sheet for each floor was most of the run time.
    """
    global _FAMILY_RULES, _LABEL_RULES
    if job.get("family_rules") is not None:
        # A worker process loads the rules file at import; the parent may
        # hold newer rules saved from the UI since.
        _FAMILY_RULES = job["family_rules"]
        _LABEL_RULES = build_rules(CONFIG, _FAMILY_RULES, ignore_words=_FAMILY_IGNORE_WORDS)
    rule = job["rule"]
    raw_segs = job["segs"]
    x_lines = job["x_lines"]
    x_clusters = job["x_clusters"]
    detection_mode = job["detection_mode"]
    family_overrides = job["family_overrides"]
    floor_poly = [tuple(p) for p in job["floor"]["polygon"]]
    floor_name = job["floor"]["name"]
    rows = job["rows"]
    stats = collections.Counter()
    # How long each stage of the pass takes (seconds, stats["timings"] in
    # the run report): the baseline for the element pipeline.
    _tm = {"t": time.perf_counter()}
    timings = {}

    def _tick(name):
        now = time.perf_counter()
        timings[name] = round(timings.get(name, 0.0) + now - _tm["t"], 3)
        _tm["t"] = now

    # (An X-marked trap door is an X like any other: the X sequence decides
    # it, 2026-10-16 - it is no longer taken out of the floor's linework.)
    trap_srs = []

    # Rooms are bounded by real walls only: two faces a wall's thickness
    # apart, or anything on a structure layer. Cubicle partitions, screens,
    # counters and door leaves are not walls, so a room runs past them to
    # the wall behind.
    # Each X's own frame - the lines running corner to corner along its box
    # - is no wall (2026-10-16, rule x_frames_not_walls): a 300 mm X with a
    # rectangle round it is two lines a wall's thickness apart, and as a
    # wall it stopped every trace at the X and was drawn as wall. The frame
    # stays a drawn line to the shaft's own trace (the partition between a
    # slab opening and its shaft room), so what a shaft deducts is unchanged.
    x_frame_segs = []
    wall_skip = x_lines
    # The element pipeline (rule element_pipeline): what is decided is
    # locked (locked.LockedMask), and every wall built after it skips what
    # the mask holds. Before the X marks are decided, their boxes and the
    # columns are the mask: no hatch inside an X's box is wall material.
    from .locked import LockedMask
    LM = LockedMask()
    ep = bool(rule["element_pipeline"])
    if ep:
        from shapely.geometry import box as _lbx
        LM.lock("x_box", [_lbx(*cl["bbox"]) for cl in x_clusters
                          if _point_in_polygon(cl["cx"], cl["cy"], floor_poly)])
        LM.lock("column", job.get("columns") or [])
    if rule["x_frames_not_walls"] and x_clusters:
        from . import xplace as _xp
        bxs_ = [cl["bbox"] for cl in x_clusters]
        if job.get("columns"):
            # a column's cross: its outline is the column's, not a frame (XA:
            # the steel column beside FHC SH - with its outline held out, the
            # shaft's label traced a 0.06 sq.m sliver of the column)
            from shapely.geometry import Polygon as _CPg, box as _cbx
            from shapely.strtree import STRtree as _CST
            cols_ = []
            for c_ in job.get("columns") or ():
                try:
                    g_ = (c_ if hasattr(c_, "geom_type") else _CPg(c_)).buffer(0)
                except Exception:                     # noqa: BLE001
                    continue
                if not g_.is_empty:
                    cols_.append(g_)
            if cols_:
                ctree_ = _CST(cols_)
                bxs_ = [b_ for b_ in bxs_
                        if not any(cols_[k].intersection(_cbx(*b_)).area >= 0.8 * _cbx(*b_).area
                                   for k in ctree_.query(_cbx(*b_)))]
        lays_ = _xp.diagonal_layers(raw_segs, bxs_, lambda sg: _seg_key(sg) in x_lines)
        fidx = _xp.frame_lines(raw_segs, bxs_, layers=lays_, t_min=WALL_T_MIN, t_max=WALL_T_MAX,
                               skip=lambda sg: _seg_key(sg) in x_lines)
        if fidx:
            x_frame_segs = [tuple(raw_segs[i][:4]) for i in sorted(fidx)]
            wall_skip = set(x_lines) | {_seg_key(raw_segs[i]) for i in fidx}
            stats["x_frame_lines"] += len(fidx)
    walls = _detect_walls(raw_segs, skip=wall_skip,
                          other_level=(rule["other_level_structure"] and
                                       not _floor_uses_lower_structure(job["floor"]["name"])),
                          fixtures=rule["fixtures_not_walls"],
                          break_lines=rule["break_lines"],
                          overrun_caps=rule["overrun_wall_ends"],
                          plate=floor_poly, hatches=job.get("wall_hatches"))
    faces, caps = walls["faces"], walls["caps"]
    wall_exclusions = [{"floor": floor_name, "rule": r, "line": list(s)} for r, s in walls.get("excluded") or []]
    for r, _s in walls.get("excluded") or []:
        stats["faces_" + r.replace(" ", "_")] += 1
    _tick("01 walls W1 (detect_walls)")

    # Door openings are closed straight across, along the wall faces, where
    # a door swing stands in them. Openings without a door stay open unless
    # the close_wall_gaps rule asks for them to be closed too.
    swings = job["swings"] if rule["close_doors"] else []
    jamb_lines = None
    if swings and rule["door_frame_jamb"]:
        # every drawn line but the X marks - door frames included: the far
        # jamb is often only a frame on the door layer
        jamb = [sg[:4] for sg in raw_segs if _seg_key(sg) not in x_lines]
        if HATCH_EDGES_AS_WALLS and job.get("wall_hatches"):
            fb0 = _polygon_bbox(floor_poly)
            jamb += [tuple(h[:4]) for h in _hatch_edges(_hatch_polys(job.get("wall_hatches")),
                                                        (fb0[0] - 1000, fb0[1] - 1000, fb0[2] + 1000, fb0[3] + 1000))]
        jamb_lines = _LineGrid(jamb)
    door_closing = _door_bridges(faces, caps, swings, others=jamb_lines) if swings else []
    # The door openings drawn on AI_P_DOORS / AI_S_DOORS (door_openings: a swing - arc or
    # ellipse - at a gap in the traced walls) close a room that leaks
    # through one where the faces carried across did not: a door between
    # two short returns has no wall face running on to it (H10 Tower 1: the
    # G. TOILET door stands between a 300 mm nib and a wall corner, so the
    # toilet ran out into its lobby and came back as a box). They join the
    # doorless openings closed on a leak (retry_gaps), so a room that does
    # not leak keeps its entry lobby (the typical floors' G. TOILET), and
    # an unlabelled lobby shut off is joined again (join_beyond). Worked
    # out once here; the walls drawn below use the same trace and openings.
    early_wt, early_doors, door_leak_closing = None, None, []
    early_review = []                       # doors_need_evidence: what was left out
    if DRAW_DOORS and job.get("door_arcs") and (rule["close_at_found_doors"] or rule["draw_walls"]):
        if rule["wall_trace"]:
            try:
                from . import walltrace as _wt0
                wt_counts = {}
                early_wt = _wt0.trace_floor(job.get("segs"), job.get("all_hatches"), job.get("rows"), floor_poly,
                                            arcs=job.get("wall_arcs"),
                                            skip=((lambda sg, _k=wall_skip: _seg_key(sg) in _k) if ep else None),
                                            locked=(LM.union(("x_box", "column")) if ep else None),
                                            counts=wt_counts)
                for k_, v_ in wt_counts.items():
                    stats[k_] += v_
            except Exception:                         # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception("wall trace for %s", floor_name)
                early_wt = None
        try:
            early_review = []
            early_doors = door_openings(job.get("door_arcs") or [], job.get("segs") or [],
                                        (early_wt["strips"] if early_wt is not None else walls.get("strips")) or [],
                                        evidence=rule["doors_need_evidence"], rejected=early_review)
        except Exception:                             # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("door openings on %s", floor_name)
            early_doors = None
        if early_doors and rule["close_at_found_doors"]:
            door_leak_closing = _door_quad_closures(early_doors, door_closing)
            stats["door_closing_from_openings"] += len(door_leak_closing) // 2
            if not rule["close_gaps_on_leak"]:
                door_closing = door_closing + door_leak_closing   # no leak retry: closed always
                door_leak_closing = []
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
    first_door_gap = len(retry_gaps)                  # retry_gaps[first_door_gap:] close door openings
    retry_gaps = list(retry_gaps) + list(door_leak_closing)
    _tick("02 early doors (walltrace, door_openings)")



    stats["wall_segs_thick"] += len(faces)
    stats["wall_segs_all"] += len(wall_set)
    stats["wall_faces"] += len(faces)
    stats["wall_end_caps"] += len(caps)
    stats["panels_ignored"] += walls["panels"]
    stats["ledge_lines"] += walls.get("ledges", 0)
    stats["door_swings"] += len(swings)
    stats["door_closing_lines"] += len(door_closing)
    stats["gap_closing_lines"] += len(gap_closing)
    stats["threshold_lines"] += len(threshold)
    stats["retry_gap_lines"] += len(retry_gaps)
    stats["centre_marks_ignored"] += walls.get("centre_marks", 0)
    stats["break_lines_ignored"] += walls.get("break_lines", 0)
    stats["door_lines_ignored"] += walls.get("door_lines", 0)

    # Windows: an X-marked outline lying in a wall (see WindowFinder) is a
    # window, not a shaft - taken out before the X shafts are worked out,
    # so it never claims a room's label, and its outline is a wall to the
    # rooms beside it. Drawn on WINDOW_LAYER.
    windows, cutouts, window_conflicts = [], [], []

    def shaft_label_beside(bbox):
        """A shaft-like label standing beside an X in a wall - within
        X_TEXT_GAP of its outline, its stated size (if any) fitting it -
        or None. The nearest one wins.

        The X's own layer says CUTOUT on many plans (A-CUTOUT holds every
        X mark), so an X lying in the line of a wall reads as a wall
        cut-out from its layer alone. One with FHR, FHC-3 or PHE SHAFT
        written beside it is a riser slot - the take-off deducts it as
        that shaft - while one beside a column tag (C-9) or a door tag is
        the cut-out its layer says. A room's name never makes it a shaft."""
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        best = None
        for r in rows:
            text = str(r.get("text") or "").strip()
            if not text or is_ignorable_label(text) or _is_plain_tag(text):
                continue
            gap = _bbox_gap(float(r["x"]), float(r["y"]), bbox)
            if gap > X_TEXT_GAP:
                continue
            fam = family_for_label(text, _FAMILY_RULES)["name"]
            if not shaft_like_label(text, fam):
                continue
            if not stated_fits(declared_size(text), w, h):
                continue
            if best is None or gap < best[0]:
                best = (gap, text)
        return best[1] if best else None

    # The X marks of this floor, each with what the X sequence reads
    # (xplace.x_facts: the walls round it, its tags, a column under it).
    # Nothing is decided here: every X is decided once, by
    # xplace.resolve_x, further down (where its label and the rooms round
    # it are known). x_recs[fid]: one record per X mark.
    x_recs = []
    x_mass = None                      # the walls the X marks were placed against
    if x_clusters:
        from . import xplace as _xp
        from shapely.geometry import Polygon as _WPoly, box as _wbox
        mass = _xp.WallMass(walls.get("strips"), _hatch_polys(job.get("wall_hatches")) or [],
                            list(faces) + list(caps), job.get("columns") or [],
                            soft=[b for r_, b in (walls.get("excluded") or []) if r_ == "unhatched pair inside a room"],
                            lines=[sg[:4] for sg in raw_segs if _seg_key(sg) not in x_lines])
        mass.set_xboxes([cl_["bbox"] for cl_ in x_clusters])
        x_mass = mass
        # the window finder reads the X frames as the lines they are: a
        # window's own outline is what closes the wall across it
        finder = WindowFinder(list(faces) + list(caps) + list(x_frame_segs), job.get("wall_hatches"),
                              evidence=OpeningEvidence(raw_segs))
        texts_ = [(float(r["x"]), float(r["y"]), str(r.get("text") or "")) for r in rows]
        x_evidence = [None]
        keep, drop_strips = [], set()
        # an X drawn turned (XA: PHE SH at 23 degrees in the chamfered
        # corner) is the turned rectangle its four ends make, not the box
        # round it - 0.28 sq.m, not 0.49
        for k_, cl in enumerate(x_clusters):
            q_ = _turned_x_outline(cl)
            if q_ is not None:
                x_clusters[k_] = dict(cl, region=True, poly=q_[0], area_sqm=q_[1], rects=None, turned=True)
                stats["x_turned_outline"] += 1
        for cl in x_clusters:
            if not _point_in_polygon(cl["cx"], cl["cy"], floor_poly):
                keep.append(cl)
                continue
            if cl.get("region") and len(cl.get("poly") or ()) >= 3:
                wpg = _WPoly(cl["poly"])
            else:
                wpg = _wbox(cl["mn_x"], cl["mn_y"], cl["mx_x"], cl["mx_y"])
            kind, why, ev, clash = (finder.classify(wpg) if wpg.is_valid and wpg.area > 0
                                    else (None, None, None, None))
            if kind == "window" and job.get("dashed"):
                # a dashed outline is never a window's (the X sequence's
                # window evidence is a solid outline's)
                if x_evidence[0] is None:
                    from . import elementid as _eid
                    x_evidence[0] = _eid.Evidence(raw_segs, job.get("dashed"))
                if _eid_boundary(wpg, x_evidence[0]) == "dashed":
                    kind = "cutout"
                    stats["windows_dashed_to_cutouts"] += 1
            # a group of X marks (an L of two strips) is not cut back as one box
            rects_ = cl.get("rects") or ()
            one_x = not cl.get("region") and (len(rects_) <= 1 or (
                _wbox(*cl["bbox"]).area > 0 and
                __import__("shapely.ops", fromlist=["unary_union"]).unary_union(
                    [_wbox(*r_[:4]) for r_ in rects_]).area >= 0.95 * _wbox(*cl["bbox"]).area))
            f_ = _xp.x_facts(cl["bbox"], mass, texts_, is_name=_x_tag_is_name, finder_kind=kind,
                             glazing=((ev or {}).get("glazing", 0) if kind == "window" else 0), cut_back=one_x,
                             area_sqm=(cl.get("area_sqm") if cl.get("region") else None),
                             round_=x_in_circle(cl["bbox"], job.get("circles")))
            fid = len(x_recs)
            rect = [tuple(p) for p in list(wpg.minimum_rotated_rectangle.exterior.coords)[:4]]
            x_recs.append({"fid": fid, "facts": f_, "bbox": [round(v) for v in cl["bbox"]], "rect": rect,
                           "cx": cl["cx"], "cy": cl["cy"],
                           "name": (cutout_name(ev) if ev else CUTOUT_NAME), "cls": None})
            if ev is not None and ev.get("glazing", 0) >= 1 and f_["in_wall"]:
                stats["windows_by_glazing"] += 1
            if clash and f_["in_wall"]:
                bx = wpg.bounds
                window_conflicts.append({"floor": floor_name, "bbox": [round(v) for v in bx],
                                         "glazing": ev["glazing"], "word": clash,
                                         "layers": [layer_text(l) for l in ev["layers"]],
                                         "note": "layer says {0}, drawing shows glazing".format(clash)})
                stats["window_layer_conflicts"] += 1
            if f_["in_wall"] or f_["column"]:
                # inside a wall (a window or a cut-out, steps 4 and 5) or on a
                # column (step 6): held out of the spaces traced below; an
                # opening in a wall is wall to the rooms beside it
                x_recs[-1]["held"] = True
                if f_["in_wall"]:
                    wall_set = wall_set + [(rect[k][0], rect[k][1], rect[(k + 1) % 4][0], rect[(k + 1) % 4][1])
                                           for k in range(4)]
                continue
            drop_strips.update(f_["drop_strips"])
            cl2 = dict(cl, x_fid=fid, x_ctag=f_["ctag"])
            if kind == "shaft" and ev is not None and ev.get("name"):
                cl2["name_hint"] = ev.get("name")         # a shaft strip named from its overlay
            elif kind in ("cutout", "window"):
                # the window finder took it for an opening, the walls do not:
                # a shaft label beside it names it (FHR, FHC-3, PHE SHAFT)
                beside = shaft_label_beside(cl["bbox"])
                if beside:
                    cl2["name_hint"] = " ".join(name_part(beside).split("\n"))
                    stats["shafts_named_beside"] += 1
            nb = f_["shaft_bbox"]
            moved = max(abs(float(a_) - float(b_)) for a_, b_ in zip(nb, cl["bbox"])) > 1.0
            if moved and not cl.get("region"):
                cl2.update(bbox=[float(v) for v in nb], mn_x=nb[0], mn_y=nb[1], mx_x=nb[2], mx_y=nb[3],
                           w=nb[2] - nb[0], h=nb[3] - nb[1])
                # the X's own rectangles cut back with it: the shaft's cell is
                # traced from them (XA STR PRE: the 300 mm it spilled into
                # the facade wall came back as a second STR PRE)
                rr_ = [(max(r_[0], nb[0]), max(r_[1], nb[1]), min(r_[2], nb[2]), min(r_[3], nb[3])) + tuple(r_[4:])
                       for r_ in (cl.get("rects") or ())]
                rr_ = [r_ for r_ in rr_ if r_[2] - r_[0] > 1.0 and r_[3] - r_[1] > 1.0]
                cl2["rects"] = rr_ or [tuple(float(v) for v in nb)]
                x_recs[-1]["cut_back_to"] = [round(v) for v in nb]
                stats["x_cut_back_from_walls"] += 1
            keep.append(cl2)
        x_clusters = keep
        if drop_strips and walls.get("strips"):
            walls["strips"] = [st for i, st in enumerate(walls["strips"]) if i not in drop_strips]
            stats["wall_strips_round_x_not_wall"] += len(drop_strips)



    _tick("03 x facts (WallMass, window finder)")
    # The slab edge is a wall: it stops rays that would otherwise run
    # off into nothing, and it confines a leaking flood fill.
    edges = (_polygon_edges(floor_poly)
             if (USE_FLOOR_EDGES and rule["floor_outline_walls"]) else [])
    # Rule boundary_on_evidence (2026-10-17): a room boundary stands only on
    # a wall, the facade, a room already identified, a primary door line or
    # a column. The facade is the glass line where glass is drawn: the glass
    # along the plate's edge stops a room before the slab edge does.
    ev = bool(rule["boundary_on_evidence"])
    ev_rejected = set()                  # sr_no of labels no evidence closes
    lobby_bank_lines = []                # rule lift_lobby_to_lift_bank: the lines closing a lobby
    facade_glass = (facade_glass_segs(job.get("glass_segs"), floor_poly)
                    if ev and job.get("glass_segs") else [])
    if facade_glass:
        stats["facade_glass_lines"] += len(facade_glass)
    f_walls = wall_set + edges + facade_glass
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
    # Built below, once the shafts, voids and chosen shapes are known:
    # their outlines are walls to the rooms traced after them.
    tracer = None

    def build_tracer(segs):
        try:
            t = RoomTracer(segs, cell_mm=TRACE_CELL_MM,
                           max_area_sqm=room_cap,
                           seal_openings_mm=SEAL_OPENINGS_MM)
            return t if t.ok else None
        except (MemoryError, ValueError, OverflowError):
            return None

    def extend_tracer(extra):
        """build_tracer(f_walls + extra), on the walls `tracer` already
        indexed and rasterised (RoomTracer.extended): the same traces, the
        base walls not indexed again for every opening a leak closes."""
        if tracer is None:
            return build_tracer(f_walls + list(extra))
        try:
            t = tracer.extended(extra)
            return t if t.ok else None
        except (MemoryError, ValueError, OverflowError):
            return None

    # The same walls with every doorless opening closed, built the first
    # time a room needs it.
    closed_tracer = []

    # Last of all, the plain lines of the drawing: a side of a room that
    # has no facade, wall, door or settled boundary is closed by whatever
    # line is drawn there (rule 5 - a polyline has the last priority).
    lines_tracer_box = []

    def lines_tracer():
        if not lines_tracer_box:
            t3 = None
            # (never while boundaries stand only on evidence: a plain line
            # is none - rule boundary_on_evidence)
            if tracer is not None and rule["lines_last_resort"] and not ev:
                never = [k.upper() for k in _HARD_EXCLUDE_KEYWORDS]
                skip_words = [k.upper() for k in _WALL_EXCLUDE_KEYWORDS]
                plain = []
                for sg in raw_segs:
                    lu = (sg[4] or "").upper()
                    if (any(k in lu for k in never) or any(k in lu for k in skip_words)
                            or _is_fixture_layer(lu) or (len(sg) > 7 and sg[7])
                            or _other_level_xref(lu)
                            or (x_lines and _seg_key(sg) in x_lines)
                            or sg[5] < 50.0):
                        continue
                    plain.append((sg[0], sg[1], sg[2], sg[3]))
                if plain:
                    t3 = extend_tracer(retry_gaps[:first_door_gap] + plain)
            lines_tracer_box.append(t3)
        return lines_tracer_box[0]

    def gaps_tracer():
        if not closed_tracer:
            t2 = None
            if tracer is not None and retry_gaps:
                t2 = extend_tracer(retry_gaps)
            closed_tracer.append(t2)
        return closed_tracer[0]

    def openings_tracer():
        """Every doorless opening closed, the doors found (retry_gaps from
        first_door_gap on) still open; with no doors found, gaps_tracer."""
        if first_door_gap >= len(retry_gaps):
            return gaps_tracer()
        if first_door_gap == 0 or tracer is None:
            return None
        return tracer_with(list(range(first_door_gap)))

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
                    "want": declared_size(r["text"]),
                    "want_area": declared_area(r["text"]),
                    "family": r.get("family") or "",
                    "layer": r.get("layer") or "",
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
    size_flags = {}                # sr_no -> (stated sqm, drawn sqm)
    for t in floor_texts:
        if t["sr_no"] is not None:
            text_status[t["sr_no"]] = "pending"
    for sr in trap_srs:
        if sr is not None:
            text_status[sr] = "absorbed"            # a trap door: part of its room

    # Validate clusters: keep only those with valid bbox. Regions the
    # tolerant recogniser added (xregions) already have their outline.
    # Anything larger than the void threshold is a void, not a shaft.
    validated = []
    voids = []
    # The walls an X's sides move out onto: with its own frame (held out of
    # the walls) it is as drawn, as before the frames were held out - a
    # framed X 250 mm off a wall (P285: LIFT WELL BELOW) is not stretched
    # to the wall
    snap_walls, snap_idx = f_walls, wall_idx
    if rule["x_wall_snap"] and x_frame_segs:
        snap_walls = f_walls + list(x_frame_segs)
        snap_idx = BandIndex(snap_walls, band=INDEX_BAND, axis_ratio=WALL_AXIS_RATIO)
    for cl in x_clusters:
        if not _point_in_polygon(cl["cx"], cl["cy"], floor_poly):
            continue
        xf_ = x_recs[cl["x_fid"]]["facts"] if cl.get("x_fid") is not None else None
        if cl.get("region"):
            bbox, src = cl["bbox"], "region"
            stats["x_regions_added"] += 1
        elif xf_ is not None and not (xf_.get("gap") is not None and xf_["gap"] <= X_PLACE_GAP_MM):
            # no wall within the X sequence's reach of it: as drawn - a wall
            # further off is no face of it (a sleeve 125 mm off the AHU
            # room's wall is not stretched to it)
            bbox, src = tuple(cl["bbox"]), "x_extent"
            stats["x_as_drawn_no_wall"] += 1
        else:
            bbox, src = _validate_x_cluster(
                cl, snap_walls if rule["x_wall_snap"] else [], [],
                trust_layer=(detection_mode in ("layer", "geometry")),
                idx_thick=snap_idx if rule["x_wall_snap"] else None,
                idx_all=None)
        if bbox is None:
            bbox, src = tuple(cl["bbox"]), "x_extent"     # every X goes on to the sequence
        area_sqm = (cl.get("area_sqm") if cl.get("region") else
                    (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / 1e6)
        poly = cl.get("poly") if cl.get("region") and (len(cl.get("poly") or ()) > 4 or
                                                       (cl.get("turned") and len(cl.get("poly") or ()) == 4)) else None
        if cl.get("x_fid") is not None:
            area_sqm = x_recs[cl["x_fid"]]["facts"]["area_sqm"]
        if area_sqm > X_VOID_AREA_THRESHOLD:
            # larger than a void's size: no step before the last can claim it
            # (xplace.resolve_x) - its texts belong to it, as a void's
            voids.append({"bbox": bbox, "poly": cl.get("poly"),
                          "cx": cl["cx"], "cy": cl["cy"], "x_fid": cl.get("x_fid")})
            continue
        if src == "wall":
            stats["x_validated_wall"] += 1
        else:
            stats["x_validated_fallback"] += 1
        validated.append({"bbox": bbox, "source_kind": src, "poly": poly,
                          "cx": cl["cx"], "cy": cl["cy"], "rects": cl.get("rects"),
                          "name_hint": cl.get("name_hint"), "diag_len": cl.get("diag_len") or 0.0,
                          "region": bool(cl.get("region")),
                          "x_fid": cl.get("x_fid"), "x_ctag": cl.get("x_ctag")})

    # Two X's over the same place - an X-marked strip, and an X region the
    # strip's X is part of (an L of the strip and the narrow strip beside a
    # shaft): the one with the longer X symbol keeps the overlap, the other
    # only what is left of its own shape (or nothing). On a tie the X drawn
    # on its own rectangle wins. BHUB drew the 2760 x 300 strip twice.
    if len(validated) > 1:
        validated = _settle_x_overlaps(validated, stats)
        alive = {v.get("x_fid") for v in validated} | {v.get("x_fid") for v in voids}
        for rec in x_recs:
            if not rec.get("held") and rec["fid"] not in alive:
                rec["absorbed"] = True                # the same place as another X: one X there

    # Shapes decided in the Geometry tab. An X mark is the X sequence's
    # (2026-10-16): no shape picked, mapped or deleted in the tab changes,
    # drops or names an X or a void; the tab's other shapes are drawn as
    # mapped below.
    geo = [g for g in (job.get("geometry") or []) if g.get("kind") != "x"]
    if geo:
        # A shape deleted in the tab: nothing is drawn there - the texts
        # inside do not make a room either.
        for g in [g for g in geo if g.get("deleted")]:
            gshape = [tuple(p) for p in g.get("poly") or []]
            probe = {"bbox": tuple(g["bbox"]), "poly": gshape or None, "holes": []}
            for t in floor_texts:
                if not t["claimed"] and _inside_boundary(probe, t["x"], t["y"]):
                    t["claimed"] = True
                    if t["sr_no"] is not None:
                        text_status[t["sr_no"]] = "deleted"
            stats["geometry_deleted"] += 1

    # "Draw only my selection": nothing but the chosen shapes is drawn - no
    # room found on its own and none grown from a text (the X marks are
    # drawn as the X sequence decides them).
    if job.get("geometry_only"):
        for t in floor_texts:
            if not t["claimed"]:
                t["claimed"] = True
                if t["sr_no"] is not None:
                    text_status[t["sr_no"]] = "not_selected"

    # Voids: one boundary each, on VOID_BOUNDARY, named VOID. It follows
    # the void's own closed outline when the drawing has one; otherwise
    # the region the X marks. Every text inside a void belongs to it -
    # "HEAVILY LANDSCAPE PODIUM" names the void, it does not get a room.
    drawn = []
    void_probes = []
    void_recs = []
    for v in voids:
        shape = _best_outline(v["bbox"], job.get("void_outlines")) or \
            [tuple(p) for p in (v["poly"] or [])] or None
        if shape:
            xs = [p[0] for p in shape]
            ys = [p[1] for p in shape]
            vb = (min(xs), min(ys), max(xs), max(ys))
        else:
            vb = v["bbox"]
        void_probes.append({"bbox": vb, "poly": shape, "holes": []})
        drawn.append({"bbox": vb, "poly": shape, "holes": [],
                      "label": v.get("label") or "VOID", "source": "V", "family_forced": "Void",
                      "cx": (vb[0] + vb[2]) / 2.0, "cy": (vb[1] + vb[3]) / 2.0,
                      "sr_no": None, "absorbed_srs": [], "x_fid": v.get("x_fid")})
        void_recs.append(drawn[-1])
        stats["void_boundaries"] += 1

    # The included shapes are drawn as mapped, and take the texts inside.
    for g in geo:
        if not g.get("include"):
            stats["geometry_excluded"] += 1
            continue
        gb = tuple(g["bbox"])
        gshape = [tuple(p) for p in g.get("poly") or []]
        gpoly = gshape if len(gshape) > 4 else None
        probe = {"bbox": gb, "poly": gshape or None, "holes": []}
        sr = g.get("label_sr")
        for t in floor_texts:
            if t["claimed"]:
                continue
            if (sr is not None and t["sr_no"] == sr) or _inside_boundary(probe, t["x"], t["y"]):
                t["claimed"] = True
                if t["sr_no"] is not None:
                    text_status[t["sr_no"]] = "geometry"
        drawn.append({"bbox": gb, "poly": gpoly, "holes": [],
                      "label": (g.get("label") or g.get("family") or "SPACE").strip(),
                      "source": "G", "family_forced": g.get("family") or None,
                      "cx": (gb[0] + gb[2]) / 2.0, "cy": (gb[1] + gb[3]) / 2.0,
                      "sr_no": sr, "absorbed_srs": []})
        stats["geometry_boundaries"] += 1

    # Boundaries settled before any room is traced - X shafts and lift
    # wells, voids, shapes chosen in the Geometry tab - are walls to every
    # room traced after them, so a room stops at a shaft rather than
    # running into it (rule 5: "the boundary of an existing enclosed
    # geometry"). Rooms then cannot overlap them (rule 9).
    if rule["existing_boundaries_walls"]:
        settled = []
        for v in validated:
            settled.extend(_outline_edges(v.get("poly"), v["bbox"]))
            # an X cluster of several marks: each mark's own rectangle too,
            # not only the box round them
            if not v.get("poly") and len(v.get("rects") or ()) > 1:
                for r in v["rects"]:
                    settled.extend(_outline_edges(None, r))
        for d0 in drawn:
            settled.extend(_outline_edges(d0.get("poly"), d0["bbox"]))
        if settled:
            f_walls = f_walls + settled
            wall_idx = BandIndex(f_walls, band=INDEX_BAND,
                                 axis_ratio=WALL_AXIS_RATIO)
            stats["settled_edges_as_walls"] += len(settled)
    # A wall drawn as a hatch with one face line missing - the L-wall round
    # a PHE shaft, the wall between a toilet's lobby and H.TOILET on
    # 181216 8th floor - let the fill run inside the wall into the rooms
    # beyond it. The hatch's outline is a wall; and an opening too narrow
    # to be a doorway (a slit) between a wall's end and another wall is
    # closed, for every trace.
    hatch_lines = []
    if HATCH_EDGES_AS_WALLS and job.get("wall_hatches"):
        fb = _polygon_bbox(floor_poly)
        hatch_lines = _hatch_edges(_hatch_polys(job.get("wall_hatches")),
                                   (fb[0] - 1000, fb[1] - 1000, fb[2] + 1000, fb[3] + 1000))
    slits = _slit_closers(faces + caps, f_walls + hatch_lines) if SLIT_MAX_MM > 0 else []
    # A doorway with a line drawn across it on the wall's face - a door
    # frame on any layer (SD-3B into the PHE shaft on 181216: an unnamed
    # block on EL-SHAFT, so no door layer and no door block) - is closed.
    # Never in front of a lift or shaft: closing a lift's door recess made
    # the lift larger.
    face_closers = []
    if FACE_LINE_COVER > 0:
        from shapely.geometry import Polygon as _FP, box as _fbox
        blk = []
        for v in validated:
            try:
                blk.append((_FP(v["poly"]) if v.get("poly") else _fbox(*v["bbox"])).buffer(0))
            except (ValueError, TypeError):
                pass
        for d0 in drawn:
            if d0.get("source") in ("X", "V"):
                try:
                    blk.append((_FP(d0["poly"]) if d0.get("poly") else _fbox(*d0["bbox"])).buffer(0))
                except (ValueError, TypeError):
                    pass
        for t in floor_texts:
            if family_for_label(t.get("text") or "", _FAMILY_RULES)["name"] in ("Lift", "Shaft"):
                # a lift or shaft named here: the opening in front of its label
                blk.append(_fbox(t["x"] - 600, t["y"] - 600, t["x"] + 600, t["y"] + 600))
        face_closers = _face_line_closers(raw_segs, f_walls + hatch_lines, skip=x_lines,
                                          blockers=[b for b in blk if not b.is_empty])
    f_walls_base = f_walls
    if hatch_lines or slits or face_closers:
        f_walls = f_walls + hatch_lines + slits + face_closers
        wall_idx = BandIndex(f_walls, band=INDEX_BAND,
                             axis_ratio=WALL_AXIS_RATIO)
        stats["hatch_edges_as_walls"] += len(hatch_lines)
        stats["slits_closed"] += len(slits)
        stats["doorways_closed_by_drawn_line"] += len(face_closers)
    if BOUNDARY_METHOD == "trace" and rule["trace_rooms"]:
        tracer = build_tracer(f_walls)
    _tick("04 tracer built (voids, settled outlines, slits)")
    base_box = []

    def base_tracer():
        if not base_box:
            base_box.append(tracer if f_walls_base is f_walls or tracer is None
                            else build_tracer(f_walls_base))
        return base_box[0]

    # Which labels an X may take (x_may_take): one standing inside it (by
    # LABEL_INSIDE_TOL_MM), or a shaft-like one beside it with no larger
    # room around it - and never one whose stated size the X does not
    # have. ELEC. ROOM 2500x2500, 972 mm from a 600 x 2500 cut-out, was
    # taken by it: the room was never traced and the shaft got its name.
    x_refused = {}                     # sr_no -> (X bbox, why)
    cell_pairs = set()                 # (cluster, text) given the X's whole shaft cell
    trim_pairs = set()                 # (cluster, text): the X cut back to its stated size (band_trim)
    room_at = {}

    room_pg_at = {}

    def room_area_at(t):
        """The enclosed room traced from a label (sq.m), or None."""
        key = (t["x"], t["y"])
        if key not in room_at:
            tr = _trace_with(tracer, t["x"], t["y"]) if tracer is not None else None
            room_at[key] = tr.area_sqm if (tr is not None and tr.ok and tr.area_sqm > 0) else None
            pg_ = None
            if tr is not None and tr.ok and tr.area_sqm > 0 and tr.polygon:
                try:
                    from shapely.geometry import Polygon as _RP
                    pg_ = _RP(tr.polygon).buffer(0)
                except Exception:                     # noqa: BLE001
                    pg_ = None
            room_pg_at[key] = pg_
        return room_at[key]

    col_pgs = []
    if job.get("columns"):
        from shapely.geometry import Polygon as _CP0
        for c_ in job.get("columns") or ():
            try:
                g_ = (c_ if hasattr(c_, "geom_type") else _CP0(c_)).buffer(0)
            except Exception:                         # noqa: BLE001
                continue
            if not g_.is_empty:
                col_pgs.append(g_)

    def own_room_across_wall(t, bbox):
        """A label standing in a room of its own, a wall between that room
        and the X: the room it traces (not the inside of a column - XA's ELE
        SH stands on the steel column beside its shaft's wall) clear of the
        X by more than the wall placement's gap."""
        if x_mass is None or not x_mass.wall_between(t["x"], t["y"], bbox):
            return False
        room_area_at(t)
        pg_ = room_pg_at.get((t["x"], t["y"]))
        if pg_ is None or pg_.is_empty:
            return False
        if any(c_.intersection(pg_).area >= 0.5 * pg_.area for c_ in col_pgs if c_.intersects(pg_)):
            return False
        from shapely.geometry import box as _RB
        return pg_.distance(_RB(*bbox)) > X_PLACE_GAP_MM

    # Raw lines as arrays, for band_trim below (built on first use).
    _band = {}

    def band_trim(bbox, want, keep):
        """bbox cut back to the size a label states: one side fits it
        (STATED_SIDE_TOL), the other runs over by at most X_BAND_MAX_MM,
        and a drawn line (any layer) crosses the whole short side at the
        stated depth from one end - the shaft's own edge, with the shutter
        band beyond it. The end kept is the one `keep` (the X itself)
        stands at. Returns the cut bbox, or None."""
        if not want:
            return None
        if "a" not in _band:
            import numpy as _np
            arr = _np.array([(sg[0], sg[1], sg[2], sg[3]) for sg in raw_segs], dtype=float) \
                if raw_segs else _np.zeros((0, 4))
            _band["a"] = arr
            _band["np"] = _np
        _np, arr = _band["np"], _band["a"]
        x0, y0, x1, y1 = bbox
        w, h = x1 - x0, y1 - y0
        for p, q in ((want[0], want[1]), (want[1], want[0])):
            for axis in ("y", "x"):
                # the side across the cut fits p; the cut axis is over q
                side, over = (w, h) if axis == "y" else (h, w)
                if abs(side - p) > STATED_SIDE_TOL * p or not (0 < over - q <= X_BAND_MAX_MM):
                    continue
                lo, hi = (y0, y1) if axis == "y" else (x0, x1)
                klo, khi = (keep[1], keep[3]) if axis == "y" else (keep[0], keep[2])
                for at, kept in ((lo + q, (lo, lo + q)), (hi - q, (hi - q, hi))):
                    if klo < kept[0] - X_BAND_LINE_TOL_MM or khi > kept[1] + X_BAND_LINE_TOL_MM:
                        continue                      # the X would be cut
                    if axis == "y":
                        m = ((_np.abs(arr[:, 1] - at) <= X_BAND_LINE_TOL_MM) &
                             (_np.abs(arr[:, 3] - at) <= X_BAND_LINE_TOL_MM))
                        a0 = _np.minimum(arr[m, 0], arr[m, 2]); a1 = _np.maximum(arr[m, 0], arr[m, 2])
                        span = (x0, x1)
                    else:
                        m = ((_np.abs(arr[:, 0] - at) <= X_BAND_LINE_TOL_MM) &
                             (_np.abs(arr[:, 2] - at) <= X_BAND_LINE_TOL_MM))
                        a0 = _np.minimum(arr[m, 1], arr[m, 3]); a1 = _np.maximum(arr[m, 1], arr[m, 3])
                        span = (y0, y1)
                    cover = (_np.minimum(a1, span[1]) - _np.maximum(a0, span[0])).clip(min=0).sum() if m.any() else 0.0
                    if cover < 0.8 * (span[1] - span[0]):
                        continue
                    nb = (x0, kept[0], x1, kept[1]) if axis == "y" else (kept[0], y0, kept[1], y1)
                    if stated_fits(want, nb[2] - nb[0], nb[3] - nb[1]):
                        return nb
        return None

    cell_of = {}

    def x_cell(bbox, want):
        """The shaft an X marks only a strip of: the X with the enclosed
        space beside it (traced from just outside each of its sides), when
        that has the stated size - (polygon points, bbox) or None."""
        key = (tuple(round(v) for v in bbox), want)
        if key in cell_of:
            return cell_of[key]
        cell_of[key] = None
        if tracer is None or not want:
            return None
        from shapely.geometry import Polygon as _CP, box as _cbox
        from shapely.ops import unary_union as _cu
        xb = _cbox(*bbox)
        cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
        off = 80.0
        parts = []
        # the walls as drawn (not the hatch outlines and slit closers added
        # for the rooms): which label an X takes is decided as before
        cell_tracer = base_tracer()
        for px, py in ((bbox[0] - off, cy), (bbox[2] + off, cy), (cx, bbox[1] - off), (cx, bbox[3] + off)):
            tr = _trace_with(cell_tracer, px, py)
            if tr is None or not tr.ok or not tr.polygon or tr.area_sqm <= 0:
                continue
            if tr.area_sqm > 3.0 * want[0] * want[1] / 1e6:
                continue                                  # open floor, not the shaft's cell
            pg = _CP(tr.polygon).buffer(0)
            if pg.distance(xb) <= off:
                parts.append(pg)
        for pg in parts:
            u = _cu([pg, xb]).buffer(1.0, join_style=2).buffer(-1.0, join_style=2)
            if u.geom_type != "Polygon":
                continue
            b = u.bounds
            if (stated_fits(want, b[2] - b[0], b[3] - b[1])
                    and abs(u.area / (want[0] * want[1]) - 1.0) <= STATED_CLAIM_TOL):
                cell_of[key] = ([(float(x), float(y)) for x, y in list(u.exterior.coords)[:-1]], tuple(b))
                break
            nb = band_trim(tuple(b), want, tuple(bbox)) if rule["x_band_trim"] else None
            if nb is not None:
                cut = u.intersection(_cbox(*nb))
                if cut.geom_type == "Polygon" and abs(cut.area / (want[0] * want[1]) - 1.0) <= STATED_CLAIM_TOL:
                    stats["x_band_trimmed"] += 1
                    cell_of[key] = ([(float(x), float(y)) for x, y in list(cut.exterior.coords)[:-1]], tuple(cut.bounds))
                    break
        return cell_of[key]

    trim_of = {}

    def x_may_take(t, bbox, keep_of=None):
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if rule["lift_label_fits_its_x"] and min(w, h) < LIFT_X_MIN_SIDE_MM:
            sr_ = t.get("sr_no")
            fam_ = ((floor_family_overrides.get(sr_) if sr_ is not None else None)
                    or family_for_label(t.get("text") or "", _FAMILY_RULES)["name"])
            if fam_ == "Lift":
                # a lift is never a strip: the 2450 x 600 X beside the lift
                # (Centaurus 11th floor) is something else - on every path
                # a label takes an X by (near, schedule, left over)
                stats["lift_label_strip_x_refused"] += 1
                return False, "a lift's label, and the X is too narrow for a lift"
        if not stated_fits(t.get("want"), w, h) and rule["x_band_trim"] and keep_of is not None:
            # the X snapped out to the shutter line: the X itself has the
            # stated size, cut back at the line drawn across at that depth
            nb = band_trim(bbox, t.get("want"), keep_of)
            if nb is not None:
                sr = t.get("sr_no")
                fam = ((floor_family_overrides.get(sr) if sr is not None else None)
                       or family_for_label(t.get("text") or "", _FAMILY_RULES)["name"])
                if shaft_like_label(t["text"], fam):
                    trim_of[(tuple(round(v) for v in bbox), t.get("want"))] = nb
                    return True, "trim"
        if not stated_fits(t.get("want"), w, h):
            # a shaft label whose X marks only a strip of the shaft takes
            # the shaft the X stands in, when that has the stated size
            sr = t.get("sr_no")
            fam = ((floor_family_overrides.get(sr) if sr is not None else None)
                   or family_for_label(t.get("text") or "", _FAMILY_RULES)["name"])
            if shaft_like_label(t["text"], fam) and x_cell(bbox, t.get("want")) is not None:
                return True, "cell"
            return False, "its stated size does not fit the X ({0:.0f} x {1:.0f} mm)".format(w, h)
        if _bbox_gap(t["x"], t["y"], bbox) <= LABEL_INSIDE_TOL_MM:
            return True, ""
        sr = t.get("sr_no")
        fam = ((floor_family_overrides.get(sr) if sr is not None else None)
               or family_for_label(t.get("text") or "", _FAMILY_RULES)["name"])
        if not shaft_like_label(t["text"], fam):
            return False, "a room's name, outside the X"
        if t.get("want"):
            return True, ""                       # its stated size is the X's: it names it
        if _LABEL_RULES.is_equipment(t["text"]):
            return True, ""                       # a tag (TEN, FAPA, RWP) never traces a room of its own
        if rule["x_label_not_across_wall"] and own_room_across_wall(t, bbox):
            # (XA: CA-ELEC SH on the face of the wall beside the ELECTRICAL
            # SH X names the room behind that wall)
            return False, "a wall stands between it and the X"
        a = room_area_at(t)
        if a is not None and a * 1e6 > w * h:
            return False, "it stands in a room larger than the X"
        return True, ""

    # -- Phase A: stable matching X-clusters <-> texts --
    # A shaft label often stands beside its shaft, across a wall or a
    # dimension line (SHAFT 2700x1000 sits 1.3 m above its X). When the
    # label states a size and the X has it, that label may reach a little
    # further, and it outranks a text that merely happens to stand
    # inside. Door and device tags never name a shaft.
    pairs = []
    for ci, v in enumerate(validated):
        if v.get("forced"):
            continue
        vw = v["bbox"][2] - v["bbox"][0]
        vh = v["bbox"][3] - v["bbox"][1]
        for ti, t in enumerate(floor_texts):
            if is_ignorable_label(t["text"]) or t["claimed"]:
                continue
            if _is_plain_tag(t["text"]):
                continue
            gap0 = _bbox_gap(t["x"], t["y"], v["bbox"])
            if gap0 > max(X_TEXT_RADIUS, X_TEXT_GAP):
                # A shaft schedule written in the core, a few metres from
                # the shafts (BHUB: LOBBY PRESS. 750X1140 2.8 m off its
                # X): a shaft-like label whose stated size is this X's
                # exactly may reach X_STATED_REACH_MM. Ranked after a
                # label standing inside the X, before a mere neighbour.
                err = _exact_size_err(t.get("want"), vw, vh)
                rank = 2.5
                if err is None:
                    # the X marks only the cut-out strip of the shaft: one
                    # side exact, the other narrower (FP-SH-02 750X1210
                    # over a 300 x 1210 X)
                    err = _exact_size_err(t.get("want"), vw, vh, partial=True)
                    rank = 2.7
                if err is None or gap0 > X_STATED_REACH_MM:
                    continue
                sr = t.get("sr_no")
                fam = ((floor_family_overrides.get(sr) if sr is not None else None)
                       or family_for_label(t.get("text") or "", _FAMILY_RULES)["name"])
                if not shaft_like_label(t["text"], fam):
                    continue
                if rank == 2.7 and fam == "Lift" and rule["lift_label_fits_its_x"]:
                    # a lift is never marked by a strip: a 2450 x 600 X over
                    # the lift (Centaurus) is something else, and the lift is
                    # traced from its label
                    stats["lift_label_strip_x_refused"] += 1
                    continue
                pairs.append(((rank, err, gap0), ci, ti))
                stats["x_named_from_schedule_candidates"] += 1
                continue
            rr = v.get("rects") or []
            keep = ((min(r[0] for r in rr), min(r[1] for r in rr), max(r[2] for r in rr), max(r[3] for r in rr))
                    if rr else None)
            ok, why = x_may_take(t, v["bbox"], keep_of=keep)
            if not ok:
                x_refused.setdefault(t.get("sr_no"), (v["bbox"], why))
                continue
            if why == "trim":
                pairs.append(((1,) + tuple(label_score(t["text"], t.get("height", 0.0),
                                                      _bbox_gap(t["x"], t["y"], v["bbox"]))), ci, ti))
                trim_pairs.add((ci, ti))
                continue
            if why == "cell":
                # the shaft cell has the stated size: a full fit, ranked
                # with the labels that fit their X
                pairs.append(((1,) + tuple(label_score(t["text"], t.get("height", 0.0),
                                                      _bbox_gap(t["x"], t["y"], v["bbox"]))), ci, ti))
                cell_pairs.add((ci, ti))
                continue
            d = math.hypot(t["x"] - v["cx"], t["y"] - v["cy"])
            inside = text_in_bbox(t["x"], t["y"], v["bbox"])
            fit = _size_match(t.get("want"), vw, vh)
            gap = _bbox_gap(t["x"], t["y"], v["bbox"])
            if fit and not inside and gap <= X_TEXT_RADIUS:
                rank = 1 if fit == "full" else 3
                pairs.append(((rank,) + tuple(label_score(
                    t["text"], t.get("height", 0.0), gap)), ci, ti))
                continue
            if inside and fit == "full":
                pairs.append(((0,) + tuple(label_score(
                    t["text"], t.get("height", 0.0), d)), ci, ti))
                continue
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
            pairs.append(((2 if inside else 4,)
                          + tuple(label_score(t["text"],
                                              t.get("height", 0.0), d)),
                          ci, ti))
    pairs.sort(key=lambda p: p[0])
    cluster_assigned = {}; text_assigned = set()
    for _, ci, ti in pairs:
        if ci in cluster_assigned or ti in text_assigned: continue
        cluster_assigned[ci] = ti
        text_assigned.add(ti)

    for ci, v in enumerate(validated):
        ti = cluster_assigned.get(ci)
        sr_for_this = None
        if ti is not None and (ci, ti) in trim_pairs:
            nb = trim_of.get((tuple(round(c) for c in v["bbox"]), floor_texts[ti].get("want")))
            if nb is not None:
                v = dict(v, bbox=nb, poly=None, cx=(nb[0] + nb[2]) / 2.0, cy=(nb[1] + nb[3]) / 2.0)
                stats["x_band_trimmed"] += 1
        if ti is not None and (ci, ti) in cell_pairs and rule["x_grows_to_cell"]:
            cell = x_cell(v["bbox"], floor_texts[ti].get("want"))
            if cell is not None:
                v = dict(v, poly=cell[0], bbox=cell[1])
                stats["x_grown_to_cell"] += 1
        if ti is not None:
            label = floor_texts[ti]["text"]
            floor_texts[ti]["claimed"] = True
            sr_for_this = floor_texts[ti]["sr_no"]
            if sr_for_this is not None:
                text_status[sr_for_this] = "x"
            stats["x_with_label"] += 1
        else:
            label = v.get("name_hint") or X_UNKNOWN_LABEL
            stats["x_unknown"] += 1
        drawn.append({"bbox": v["bbox"], "poly": v.get("poly"), "holes": [],
                      "label": label, "source": "X", "xrects": v.get("rects"),
                      "cx": v["cx"], "cy": v["cy"],
                      "sr_no": sr_for_this, "absorbed_srs": [],
                      "x_fid": v.get("x_fid"), "x_ctag": v.get("x_ctag"),
                      "want": (floor_texts[ti].get("want")
                               if ti is not None else None)})

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

    def text_family(t):
        if t.get("sr_no") is not None and t["sr_no"] in floor_family_overrides:
            return floor_family_overrides[t["sr_no"]]
        return family_for_label(t.get("text") or "", _FAMILY_RULES)["name"]

    def guessed_box_for_unknown(got, seed):
        """A box the rays guessed, for a text no family knows. The walls
        did not close round it, so nothing says a room is there: a "DW"
        tag in an open corridor came back as a 16 x 3.5 m box, a sleeve
        note as 50 sq.m of open floor. A traced outline - an unnamed room
        the walls do enclose - is still drawn."""
        return (got[0] is not None and not got[1] and seed is not None
                and rule["unknown_needs_walls"]
                and text_family(seed) == "Unknown")

    def foreign_labels(poly, seed, fill_sqm=None, texts=False):
        """The other rooms' names standing inside poly (holds_other_room).
        With fill_sqm: a shaft's name whose stated size is under a tenth of
        the fill is left out - a shaft opening off the room (the PHE
        shaft's access opening in MALE TOILET's lobby), cut out of the room
        afterwards by the shaft's own boundary (rule 9), not a leak."""
        if seed is None or not poly:
            return []
        own = family_for_label(seed["text"], _FAMILY_RULES)["name"]
        out = []
        for o in floor_texts:
            if o is seed or not _point_in_polygon(o["x"], o["y"], poly):
                continue
            t = o["text"]
            if (is_ignorable_label(t) or is_note_text(t)
                    or is_device_tag(t) or not looks_like_primary(t)):
                continue
            fam = family_for_label(t, _FAMILY_RULES)["name"]
            if fam == own:
                continue
            if fill_sqm:
                want = o.get("want") or declared_size(t)
                if (want and shaft_like_label(t, fam)
                        and want[0] * want[1] / 1e6 < 0.1 * fill_sqm):
                    continue
            out.append(o["text"] if texts else (o["x"], o["y"]))
        return out

    def leaks(t, seed):
        """A trace that ran out of its room: none, over the cap, or
        holding another room's name (a small shaft opening off it aside)."""
        if t is None or not t.ok:
            return True
        return bool(foreign_labels(t.polygon, seed, fill_sqm=t.area_sqm))

    def open_to_refuge(t, seed):
        """A balcony's fill that runs on into a refuge area and holds only
        refuge names: the sunk balcony strip open to the refuge is part of
        it (285 FD Tower 2, 5th floor: BALCONY 100MM SUNK beside REFUGE
        AREA-1; the manual take-off counts it as refuge)."""
        if seed is None or t is None or not t.ok or t.area_sqm > REFUGE_UNSIZED_MAX_SQM:
            return False
        if family_for_label(seed["text"], _FAMILY_RULES)["name"] != "Balcony / Open":
            return False
        names = foreign_labels(t.polygon, seed, fill_sqm=t.area_sqm, texts=True)
        return bool(names) and all(REFUGE_NAME_RE.search(str(n).upper()) for n in names)

    leak_tracers = {}

    def tracer_with(extra):
        key = tuple(sorted(extra))
        if key not in leak_tracers:
            leak_tracers[key] = extend_tracer([retry_gaps[k] for k in key])
        return leak_tracers[key]

    def close_leak_path(tx, ty, seed, tr, doors=False):
        """Close only the doorless openings the fill leaked through: the
        retry gap lines crossing the leaked fill, the one nearest the
        other room's name first (or, for a fill over the area cap, the
        farthest from the label first), one at a time, re-tracing after
        each; the first trace holding no other room's name and under the
        cap is kept. Returns (trace, [gap indices closed]) or (None, None)
        when LEAK_CLOSE_MAX re-traces do not stop the leak. With doors,
        the doors found (close_at_found_doors) are candidates too, after
        the openings."""
        if not retry_gaps or LEAK_CLOSE_MAX <= 0 or tracer is None:
            return None, None
        if not doors and first_door_gap == 0:
            return None, None
        from shapely.geometry import Polygon, LineString, Point
        fill, targets = None, []
        if tr is not None and tr.ok and tr.polygon:
            try:
                fill = Polygon(tr.polygon).buffer(0)
            except (ValueError, TypeError):
                fill = None
            targets = foreign_labels(tr.polygon, seed, fill_sqm=tr.area_sqm)
        if fill is None:
            # over the cap: a wider fill shows where it ran
            try:
                big = RoomTracer(f_walls, cell_mm=TRACE_CELL_MM, max_area_sqm=room_cap * 4.0,
                                 seal_openings_mm=SEAL_OPENINGS_MM)
                tb = _trace_with(big, tx, ty)
                if tb is not None and tb.ok and tb.polygon:
                    fill = Polygon(tb.polygon).buffer(0)
            except (MemoryError, ValueError, OverflowError, TypeError):
                fill = None
        # where the fill entered the other room: that room's own space,
        # traced from its name with every doorless opening closed
        spaces = []
        if targets:
            closed_all = gaps_tracer() if doors else openings_tracer()
            for t in targets:
                sp = None
                if closed_all is not None:
                    ts = _trace_with(closed_all, t[0], t[1])
                    if ts is not None and ts.ok and ts.polygon:
                        try:
                            sp = Polygon(ts.polygon).buffer(0)
                        except (ValueError, TypeError):
                            sp = None
                spaces.append(sp if sp is not None and not sp.is_empty else Point(*t))
        cands = []
        for k, g in enumerate(retry_gaps):
            if k >= first_door_gap and not doors:
                continue
            ln = LineString([(g[0], g[1]), (g[2], g[3])])
            if fill is not None:
                if not ln.intersects(fill):
                    continue
            elif ln.distance(Point(tx, ty)) > 9000.0:
                continue
            mid = ln.interpolate(0.5, normalized=True)
            if spaces:
                d = min(sp.distance(mid) for sp in spaces)
            else:
                d = -mid.distance(Point(tx, ty))      # farthest from the label first
            # the doorless openings first, then the doors found: a room the
            # openings alone close keeps its entry lobby behind its door
            cands.append((k >= first_door_gap, d, k))
        if not cands:
            return None, None
        cands.sort()
        # one opening at a time: an opening in a wall has a gap line on
        # each of its faces, closed together
        units, taken_ = [], set()
        for _door, _d, k in cands:
            if k in taken_:
                continue
            unit = [k] + [j for j in opening_twins(k) if j not in taken_
                          and (doors or j < first_door_gap)]
            taken_.update(unit)
            units.append(unit)
        used = []
        for n, unit in enumerate(units[:LEAK_CLOSE_MAX]):
            used.append(unit)
            t2 = _trace_with(tracer_with([k for u in used for k in u]), tx, ty)
            stats["leak_retraces"] += 1
            if t2 is not None and t2.area_sqm > 0 and not leaks(t2, seed):
                # an opening closed but not needed in the end stays open
                need = list(used)
                for u in list(used[:-1]):
                    trial = [x for x in need if x is not u]
                    t3 = _trace_with(tracer_with([k for x in trial for k in x]), tx, ty)
                    stats["leak_retraces"] += 1
                    if t3 is not None and t3.area_sqm > 0 and not leaks(t3, seed):
                        need, t2 = trial, t3
                stats["leak_gaps_closed"] += len(need)
                return t2, [k for u in need for k in u]
        return None, None

    twins_of = {}
    gap_boxes = []

    def twin_candidates(k):
        """The gap lines that can be a twin of gap k, in order - those whose
        box comes within a wall's thickness (plus the tilt a 5 degree turn
        allows) of its box; opening_twins tests each as before. Every twin
        passes: it overlaps gap k along the line, so a point of it lies
        within that distance of gap k."""
        import numpy as np
        if not gap_boxes:
            a = np.array([g[:4] for g in retry_gaps], dtype=np.float64).reshape(-1, 4)
            reach = WALL_T_MAX + 50.0 + 0.05 * np.hypot(a[:, 2] - a[:, 0], a[:, 3] - a[:, 1]) + 1.0
            gap_boxes.append((np.minimum(a[:, 0], a[:, 2]) - reach, np.maximum(a[:, 0], a[:, 2]) + reach,
                              np.minimum(a[:, 1], a[:, 3]) - reach, np.maximum(a[:, 1], a[:, 3]) + reach))
        x0, x1, y0, y1 = gap_boxes[0]
        g = retry_gaps[k]
        gx0, gx1 = min(g[0], g[2]), max(g[0], g[2])
        gy0, gy1 = min(g[1], g[3]), max(g[1], g[3])
        hit = (x0 <= gx1) & (x1 >= gx0) & (y0 <= gy1) & (y1 >= gy0)
        return [int(j) for j in np.nonzero(hit)[0]]

    def opening_twins(k):
        """The other gap lines of the same opening: parallel to gap k, a
        wall's thickness away at most, over the same span."""
        if k in twins_of:
            return twins_of[k]
        g = retry_gaps[k]
        L = math.hypot(g[2] - g[0], g[3] - g[1])
        out = []
        if L > 0:
            ux, uy = (g[2] - g[0]) / L, (g[3] - g[1]) / L
            for j in twin_candidates(k):
                h = retry_gaps[j]
                if j == k:
                    continue
                M = math.hypot(h[2] - h[0], h[3] - h[1])
                if M <= 0 or abs(ux * (h[2] - h[0]) / M + uy * (h[3] - h[1]) / M) < math.cos(math.radians(5.0)):
                    continue
                mx, my = (h[0] + h[2]) / 2.0, (h[1] + h[3]) / 2.0
                off = abs((mx - g[0]) * -uy + (my - g[1]) * ux)
                if off > WALL_T_MAX + 50.0:
                    continue
                t0 = (h[0] - g[0]) * ux + (h[1] - g[1]) * uy
                t1 = (h[2] - g[0]) * ux + (h[3] - g[1]) * uy
                ov = min(L, max(t0, t1)) - max(0.0, min(t0, t1))
                if ov >= 0.5 * min(L, M):
                    out.append(j)
        twins_of[k] = out
        return out

    def join_beyond(tr2, used, seed):
        """An unlabelled enclosed space on the far side of a doorless
        opening the trace closed - no room's name in it, smaller than the
        room - is part of the room (the toilet's entry lobby)."""
        from shapely.geometry import Polygon, LineString
        from shapely.ops import unary_union
        if tr2 is None or not tr2.ok or not tr2.polygon:
            return tr2
        idx = range(len(retry_gaps)) if used is None else used
        closed = gaps_tracer() if used is None else tracer_with(used)
        if closed is None:
            return tr2
        try:
            room = Polygon(tr2.polygon, [h for h in tr2.holes or () if len(h) >= 3]).buffer(0)
        except (ValueError, TypeError):
            return tr2
        add = []
        for k in idx:
            g = retry_gaps[k]
            ln = LineString([(g[0], g[1]), (g[2], g[3])])
            if ln.length <= 0 or room.exterior.distance(ln.interpolate(0.5, normalized=True)) > 2 * TRACE_CELL_MM:
                continue
            # a point just beyond the middle of the opening - past this line
            # and past the opening's line on the wall's other face - away
            # from the room
            nx, ny = -(g[3] - g[1]) / ln.length, (g[2] - g[0]) / ln.length
            probes = []
            for h in [g] + [retry_gaps[j] for j in opening_twins(k)]:
                mx, my = (h[0] + h[2]) / 2.0, (h[1] + h[3]) / 2.0
                for sgn in (1.0, -1.0):
                    probes.append((mx + sgn * nx * 3 * TRACE_CELL_MM, my + sgn * ny * 3 * TRACE_CELL_MM))
            for px, py in probes:
                if room.buffer(1.0).contains(Polygon([(px, py), (px + 1, py), (px, py + 1)])):
                    continue
                t = _trace_with(closed, px, py)
                if t is None or not t.ok or t.area_sqm <= 0 or t.area_sqm > tr2.area_sqm:
                    continue
                # the slot inside the wall between an opening's two closed
                # faces is not a space: a lobby is wider than a wall
                try:
                    mr = Polygon(t.polygon).minimum_rotated_rectangle
                    cs = list(mr.exterior.coords)
                    short = min(math.hypot(cs[i + 1][0] - cs[i][0], cs[i + 1][1] - cs[i][1]) for i in range(2))
                except (ValueError, TypeError, IndexError):
                    continue
                if short <= WALL_T_MAX + 50.0:
                    continue
                if any(_point_in_polygon(o["x"], o["y"], t.polygon) and looks_like_primary(o["text"])
                       and not (is_ignorable_label(o["text"]) or is_note_text(o["text"])
                                or is_device_tag(o["text"]))
                       for o in floor_texts if o is not seed):
                    continue                          # another room's space
                try:
                    add.append(Polygon(t.polygon, [h for h in t.holes or () if len(h) >= 3]).buffer(0))
                    # and the opening itself, between its closing lines
                    from shapely.geometry import MultiPoint
                    pts_ = [(h[0], h[1]) for h in [g] + [retry_gaps[j] for j in opening_twins(k)]] +                            [(h[2], h[3]) for h in [g] + [retry_gaps[j] for j in opening_twins(k)]]
                    hull = MultiPoint(pts_).convex_hull
                    if hull.geom_type == "Polygon":
                        add.append(hull)
                except (ValueError, TypeError):
                    pass
        if not add:
            return tr2
        u = unary_union([room] + add).buffer(TRACE_CELL_MM, join_style=2).buffer(-TRACE_CELL_MM, join_style=2)
        if u.geom_type != "Polygon" or u.area <= room.area:
            return tr2
        from .roomtrace import TraceResult
        stats["leak_spaces_joined"] += len(add)
        b = u.bounds
        return TraceResult(polygon=[(float(x), float(y)) for x, y in list(u.exterior.coords)[:-1]],
                           holes=[[(float(x), float(y)) for x, y in list(r.coords)[:-1]] for r in u.interiors],
                           area_sqm=u.area / 1e6, gross_area_sqm=Polygon(u.exterior).area / 1e6,
                           status="ok", bbox=tuple(float(v) for v in b))

    slot_quads = []

    def opening_slots():
        """The depth of each doorless opening in its wall: the quad between
        the opening's two gap lines (one per face), with both lines."""
        if not slot_quads:
            from shapely.geometry import MultiPoint, LineString
            seen = set()
            out = []
            for k in range(len(retry_gaps)):
                for j in opening_twins(k):
                    if (j, k) in seen:
                        continue
                    seen.add((k, j))
                    a, b = retry_gaps[k], retry_gaps[j]
                    q = MultiPoint([(a[0], a[1]), (a[2], a[3]), (b[0], b[1]), (b[2], b[3])]).convex_hull
                    if q.geom_type == "Polygon" and q.area > 0:
                        out.append((q, LineString([(a[0], a[1]), (a[2], a[3])]),
                                    LineString([(b[0], b[1]), (b[2], b[3])])))
            slot_quads.append(out)
        return slot_quads[0]

    band_box = []

    def hatch_band():
        """The wall hatches with the doorways in them bridged (gaps up to
        DOORWAY_BRIDGE_MM along a wall), less the hatches themselves: the
        doorways' depth in their walls. None without hatches."""
        if not band_box:
            band = None
            hp = _hatch_polys(job.get("wall_hatches"))
            if hp:
                u = _hatch_union(hp)
                r = DOORWAY_BRIDGE_MM / 2.0
                closed_ = u.buffer(r, join_style=2).buffer(-r, join_style=2)
                band = closed_.difference(u)
                if band.is_empty:
                    band = None
            band_box.append(band)
        return band_box[0]

    def trim_opening_slots(got, tx, ty, seed=None):
        """A room whose label states its size, traced holding the depth of
        a doorway in its wall - the opening closed on the wall's far face,
        open on the room's - less that slot: it is wall, and it spoils the
        size (a 2300 x 1270 shaft came out 2300 x 1470). An opening with
        the room on both sides (an entrance the trace went through) is not
        touched; a room with no stated size is measured as before."""
        bbox, poly, holes = got
        if not poly or not retry_gaps or not (seed and seed.get("want")):
            return got
        from shapely.geometry import Polygon, Point
        try:
            g = Polygon(poly, [h for h in holes or () if len(h) >= 3]).buffer(0)
        except (ValueError, TypeError):
            return got
        if g.is_empty or g.geom_type != "Polygon":
            return got
        ext = g.exterior
        tol = 1.5 * TRACE_CELL_MM
        cut = []
        for q, la, lb in opening_slots():
            if not q.intersects(g) or q.intersection(g).area < 0.8 * q.area:
                continue
            if any(ln.length > 0 and ln.buffer(tol).intersection(ext).length >= 0.8 * ln.length
                   for ln in (la, lb)):
                cut.append(q)
        # the same from the wall hatch: a doorway is the gap between two
        # pieces of one hatched wall; bridged along the wall, the part of
        # the room inside the wall's line is the slot (PHE-SHAFT 1420X1270
        # ran 200 mm up into its doorway)
        band = hatch_band()
        if band is not None and g.intersects(band):
            inside = g.intersection(band)
            for q in getattr(inside, "geoms", [inside]):
                if q.geom_type != "Polygon" or q.area < 0.01e6:
                    continue
                try:
                    mr = list(q.minimum_rotated_rectangle.exterior.coords)
                    sides = sorted(math.hypot(mr[k + 1][0] - mr[k][0], mr[k + 1][1] - mr[k][1]) for k in range(2))
                except (ValueError, TypeError, IndexError):
                    continue
                # no deeper than a wall, longer than deep, on the room's edge
                if (sides[0] <= WALL_T_MAX + 50.0 and sides[1] >= 2.0 * sides[0]
                        and q.boundary.intersection(ext.buffer(tol)).length >= 0.5 * q.length):
                    cut.append(q)
        if not cut:
            return got
        from shapely.ops import unary_union
        rest = g.difference(unary_union(cut).buffer(1.0, join_style=2))
        parts = [r for r in getattr(rest, "geoms", [rest]) if r.geom_type == "Polygon" and r.area > 0]
        if not parts:
            return got
        own = [r for r in parts if r.buffer(1.0).contains(Point(tx, ty))]
        r = own[0] if own else max(parts, key=lambda x: x.area)
        if r.area < 0.85 * g.area:
            return got
        r = r.simplify(1.0, preserve_topology=True)
        stats["wall_slots_trimmed"] += len(cut)
        return (tuple(float(v) for v in r.bounds),
                [(float(x), float(y)) for x, y in list(r.exterior.coords)[:-1]],
                [[(float(x), float(y)) for x, y in list(h.coords)[:-1]] for h in r.interiors])

    def resolve_boundary(tx, ty, seed=None):
        got = trim_opening_slots(_resolve_sized(tx, ty, seed), tx, ty, seed)
        if guessed_box_for_unknown(got, seed):
            stats["unknown_box_rejected"] += 1
            return None, None, []
        return got

    def _resolve_sized(tx, ty, seed=None):
        """Traced polygon where possible, ray-cast box otherwise, checked
        against the size its label states (see _checked_size).

        Returns (bbox, polygon_or_None, holes).
        """
        got = _resolve_once(tx, ty, seed)
        want = seed.get("want") if seed else None
        if (want is None and seed is not None and tracer is not None and not seed.get("want_area")
                and REFUGE_NAME_RE.search(str(seed.get("text") or "").upper())
                and (_result_sqm(got) or 0.0) > SERVICE_UNSIZED_MAX_SQM):
            got = _refuge_over_cap(tx, ty, got)
            if got[0] is None:
                return got
        if want is None and seed is not None and tracer is not None and not seed.get("want_area") and (
                (((_result_sqm(got) or 0.0) > SHAFT_UNSIZED_MAX_SQM or _long_strip(got))
                 and _is_shaft_name(seed.get("text")))
                or ((_result_sqm(got) or 0.0) > SERVICE_UNSIZED_MAX_SQM
                    and seed.get("family") in SERVICE_FAMILIES
                    and not SERVICE_BIG_ROOM_RE.search(str(seed.get("text") or "").upper()))):
            # a shaft never fills a room: shut its doorways, or give up
            closed = gaps_tracer()
            tr2 = _trace_with(closed, tx, ty) if closed is not None else None
            if tr2 is not None and tr2.ok and 0 < tr2.area_sqm <= SHAFT_UNSIZED_MAX_SQM:
                stats["shaft_closed_at_door"] += 1
                return (tr2.bbox, tr2.polygon, tr2.holes if rule["deduct_voids"] else [])
            stats["shaft_too_big"] += 1
            return None, None, []
        if (want is None and seed is not None and not seed.get("want_area")
                and (seed.get("family") or "Unknown") in ("Unknown", "Ignore")
                and (_result_sqm(got) or 0.0) > UNKNOWN_UNSIZED_MAX_SQM):
            stats["unknown_name_too_big"] += 1
            return None, None, []
        if want is None and tracer is not None and seed is not None and seed.get("want_area"):
            return _checked_area(tx, ty, seed, seed["want_area"], got)
        if want is None or tracer is None:
            return got
        area = _result_sqm(got)
        if _size_ok(area, want, got[0]):
            return _fit_stated(got, want, tx, ty)
        return _checked_size(tx, ty, seed, want, got)

    def _refuge_over_cap(tx, ty, got):
        """A refuge area traced over the services cap (REFUGE_NAME_RE):
        traced again with every doorway shut, then with the plain lines
        closing it too, and the first that still holds REFUGE_CLOSE_MIN_SHARE
        of it is kept - the balcony beyond a doorway or a step line left out
        (285 FD Tower 1, 16th floor: the sunk balcony's edge). Dropped when
        it is over REFUGE_UNSIZED_MAX_SQM either way."""
        a0 = _result_sqm(got) or 0.0
        for make in (gaps_tracer, lines_tracer):
            closed = make()
            tr2 = _trace_with(closed, tx, ty) if closed is not None else None
            if (tr2 is not None and tr2.ok
                    and REFUGE_CLOSE_MIN_SHARE * a0 <= tr2.area_sqm < a0 - 0.5):
                got = (tr2.bbox, tr2.polygon, tr2.holes if rule["deduct_voids"] else [])
                stats["refuge_closed_at_door"] += 1
                break
        if (_result_sqm(got) or 0.0) > REFUGE_UNSIZED_MAX_SQM:
            stats["refuge_too_big"] += 1
            return None, None, []
        return got

    def _fit_stated(got, want, tx, ty):
        """A traced room a little larger than the size its label states
        ran out through an opening into the space beside it: FIRE TOWER
        3100X4400 came out 4975 x 4600, holding the passage to the shafts
        next to it. When a rectangle of the stated size stands inside the
        outline around the label, the room is that rectangle."""
        bbox, poly, holes = got
        if not poly or not rule["fit_stated_size"] or ev:
            return got                                # (a rectangle of the stated size is no evidence)
        wa = want[0] * want[1] / 1e6
        area = _result_sqm(got) or 0.0
        if area <= wa * (1.0 + STATED_FIT_SLACK):
            return got
        from shapely.geometry import Polygon, Point, box as sbox
        try:
            room = Polygon(poly, [h for h in holes or () if len(h) >= 3])
            if not room.is_valid:
                room = room.buffer(0)
        except (ValueError, TypeError):
            return got
        label = Point(tx, ty)
        best = None
        for w, h in {(want[0], want[1]), (want[1], want[0])}:
            for vx, vy in poly:
                for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                    r = sbox(min(vx, vx + sx * w), min(vy, vy + sy * h),
                             max(vx, vx + sx * w), max(vy, vy + sy * h))
                    if not r.contains(label):
                        continue
                    inside = r.intersection(room).area
                    if inside < 0.97 * r.area:
                        continue
                    if best is None or inside > best[0]:
                        best = (inside, r)
        if best is None:
            return got
        fit = best[1].intersection(room)
        # A hole touching the outline leaves the fit in pieces: keep the
        # one holding the label.
        parts = [q for q in getattr(fit, "geoms", [fit])
                 if q.geom_type == "Polygon" and q.area > 0]
        if not parts:
            return got
        own = [q for q in parts if q.buffer(1.0).contains(label)]
        fit = own[0] if own else max(parts, key=lambda q: q.area)
        if fit.area < 0.9 * best[1].area:
            return got
        if rule["fit_keeps_wide_room"]:
            # The extra beyond the rectangle joins it along most of one
            # side - no doorway between - so it is the same room, larger
            # than its label says (H10 Tower 2: AHU 6000X5500 with its
            # duct and riser strip, 6000 x 6050 to the walls, came out
            # 33.0 of 36.3 sq.m). A passage reached through an opening
            # (the FIRE TOWER above) joins along an opening's width only.
            from shapely.geometry import LineString
            rest = room.difference(best[1])
            bx = best[1].bounds
            sides = [LineString([(bx[0], bx[1]), (bx[2], bx[1])]), LineString([(bx[2], bx[1]), (bx[2], bx[3])]),
                     LineString([(bx[2], bx[3]), (bx[0], bx[3])]), LineString([(bx[0], bx[3]), (bx[0], bx[1])])]
            # (a piece left by the cut can hang on to the rectangle by a
            # hairline along its edge - BHUB's FIRE TOWER 3100X4400: its
            # passage strip "joined" all four sides; hairlines go first)
            rest = rest.buffer(-1.0, join_style=2).buffer(1.0, join_style=2)
            for q in getattr(rest, "geoms", [rest]):
                if q.geom_type != "Polygon" or q.area < 0.02 * best[1].area:
                    continue
                qb = q.buffer(20.0)
                share = max(sd.intersection(qb).length / max(sd.length, 1.0) for sd in sides)
                if share >= FIT_WIDE_JOIN:
                    if q.area > FIT_WIDE_MAX_EXTRA * best[1].area:
                        stats["stated_size_wide_too_big"] += 1
                        continue                      # a space of its own, not a strip
                    stats["stated_size_kept_wide"] += 1
                    return got
        fit = fit.simplify(5.0, preserve_topology=True)
        stats["stated_size_fitted"] += 1
        return (tuple(float(v) for v in fit.bounds),
                [(float(x), float(y)) for x, y in list(fit.exterior.coords)[:-1]],
                [[(float(x), float(y)) for x, y in list(r.coords)[:-1]] for r in fit.interiors])

    def _result_sqm(got):
        bbox, poly, holes = got
        if bbox is None:
            return None
        if poly:
            return shoelace_area_sqm(poly) - sum(shoelace_area_sqm(h) for h in holes or ())
        return (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / 1e6

    def _checked_area(tx, ty, seed, wa, got):
        """The label states its area ("REFUGE AREA 151.05 SQ. MTR.") and
        the boundary found is more than AREA_STATED_TOL off it: try the
        trace with doorways shut, with the plain lines as walls, and from
        start points around the text, and keep the closest. H10 Tower 2:
        each 151.05 sq.m refuge came out 197-216 through its openings."""
        def err(o):
            a = _result_sqm(o)
            return abs(math.log(max(a, 1e-6) / wa)) if a else 99.0
        if err(got) <= math.log(1.0 + AREA_STATED_TOL):
            return got
        options = [got]
        for tr_ in (gaps_tracer(), lines_tracer()):
            if tr_ is None:
                continue
            for px, py in [(tx, ty)] + [(tx + r * math.cos(math.radians(a_)), ty + r * math.sin(math.radians(a_)))
                                        for r in SEED_SEARCH_MM for a_ in range(0, 360, 90)]:
                tr = _trace_with(tr_, px, py)
                if tr is None or not tr.ok or tr.area_sqm <= 0:
                    continue
                if not _point_in_polygon(tx, ty, tr.polygon) and _bbox_gap(tx, ty, tr.bbox) > 400.0:
                    continue
                if holds_other_room(tr.polygon, seed):
                    continue
                options.append((tr.bbox, tr.polygon, tr.holes if rule["deduct_voids"] else []))
        # A strip the trace ran out along - the gap between the facade
        # and the slab edge, up the whole side of the plate (H10 Tower 2
        # refuge) - is cut off: the outline opened by the strip's width,
        # keeping the part that holds the label.
        from shapely.geometry import Polygon as _P, Point as _Pt
        for o in ([] if ev else list(options)):       # (an outline opened by a width is no evidence)
            if not o[1]:
                continue
            try:
                g = _P(o[1]).buffer(0)
                for w in (300.0, 500.0, 800.0):
                    op = g.buffer(-w, join_style=2).buffer(w, join_style=2).intersection(g)
                    parts = [q for q in getattr(op, "geoms", [op]) if q.geom_type == "Polygon" and q.area > 0]
                    own = [q for q in parts if q.buffer(w).contains(_Pt(tx, ty))]
                    if not own:
                        continue
                    q = max(own, key=lambda z: z.area).simplify(1.0, preserve_topology=True)
                    options.append((tuple(float(v) for v in q.bounds),
                                    [(float(x), float(y)) for x, y in list(q.exterior.coords)[:-1]],
                                    [[(float(x), float(y)) for x, y in list(h.coords)[:-1]] for h in q.interiors]))
            except Exception:                         # noqa: BLE001
                continue
        best = min(options, key=err)
        if best is not got and err(best) <= math.log(1.0 + AREA_STATED_TOL):
            stats["area_corrected"] += 1
            return best
        stats["area_mismatch"] += 1
        if seed.get("sr_no") is not None:
            size_flags[seed["sr_no"]] = (round(wa, 2), round(_result_sqm(got) or 0.0, 2))
        return got

    def _checked_size(tx, ty, seed, want, got):
        """The label states a size and the boundary found disagrees - a
        MALE TOILET 9500x4350 came out 0.32 sq.m because its text stands
        inside a cubicle partition, and PL-SH SHAFT 2200x1000 flooded 251
        sq.m of open floor. Try the neighbours - the same point with
        doorless gaps closed, the ray-cast box, and start points moved
        out of the partition - and keep the one nearest the stated size.
        None believable: keep what was found, and say so."""
        wa = want[0] * want[1] / 1e6
        options = [got]
        closed = gaps_tracer()
        if closed is not None:
            tr2 = _trace_with(closed, tx, ty)
            if tr2 is not None and tr2.ok and tr2.area_sqm > 0:
                options.append((tr2.bbox, tr2.polygon,
                                tr2.holes if rule["deduct_voids"] else []))
        box = None if ev else raycast_boundary(tx, ty)       # (a box is no evidence)
        if box is not None:
            options.append((box, None, []))
        for r in SEED_SEARCH_MM:
            for ang in range(0, 360, 45):
                px = tx + r * math.cos(math.radians(ang))
                py = ty + r * math.sin(math.radians(ang))
                rb = None if ev else raycast_boundary(px, py)
                if rb is not None and _bbox_gap(tx, ty, rb) <= 400.0:
                    options.append((rb, None, []))
                tr = _trace_with(tracer, px, py)
                if (tr is None or not tr.ok) and closed is not None:
                    # A room open to the corridor through a doorless
                    # opening only closes with those openings shut.
                    tr = _trace_with(closed, px, py)
                if tr is None or not tr.ok or tr.area_sqm <= 0:
                    continue
                b = tr.bbox
                # The room must still be the label's: it reaches the text,
                # or it stands beside it with exactly the stated size (a
                # shaft label written under its shaft).
                gap = _bbox_gap(tx, ty, b)
                if gap > 400.0 and not (
                        gap <= X_TEXT_RADIUS and
                        _size_match(want, b[2] - b[0], b[3] - b[1]) == "full"):
                    continue
                if holds_other_room(tr.polygon, seed):
                    continue
                options.append((tr.bbox, tr.polygon,
                                tr.holes if rule["deduct_voids"] else []))
            if any(_size_ok(_result_sqm(o), want, o[0]) for o in options[1:]):
                break

        def score(o):
            a = _result_sqm(o)
            if not a:
                return 99.0
            return abs(math.log(max(a, 1e-6) / wa)) + _shape_penalty(want, o[0])
        best = min(options, key=score)
        if best is not got and _size_ok(_result_sqm(best), want, best[0]):
            stats["size_corrected"] += 1
            return best
        stats["size_mismatch"] += 1
        if seed is not None and seed.get("sr_no") is not None:
            size_flags[seed["sr_no"]] = (round(wa, 2), round(_result_sqm(got) or 0.0, 2))
        # Ten times off the stated size, every way it was tried, is not a
        # boundary for this label: a 2.2 sq.m shaft drawn over 265 sq.m of
        # open floor is worse than none. Nothing is drawn and it is
        # reported instead.
        # (the boundary kept is `got`, so it is `got` that is judged: a
        # closer option that still fails the size test does not excuse a
        # 40.8 sq.m lobby traced for LOBBY PRESS. 1200X1000 - H10 Tower 2)
        if score(best) > math.log(10.0) or score(got) > math.log(10.0):
            stats["size_rejected"] += 1
            return None, None, []
        # Three times the stated size and another room's name inside it:
        # the fill left the shaft and filled the room it opens off. "PHE-
        # SHAFT 3260X1270" on BHUB's 20th floor stands inside MALE TOILET
        # and came back as the whole toilet, 30 sq.m, and the toilet's own
        # label was swallowed. The room keeps its label; the shaft is
        # reported as not found.
        shape = got[1] or (got[0] and [(got[0][0], got[0][1]), (got[0][2], got[0][1]),
                                       (got[0][2], got[0][3]), (got[0][0], got[0][3])])
        if (score(best) > math.log(3.0) and shape
                and holds_other_room(shape, seed)):
            stats["size_rejected"] += 1
            stats["size_rejected_other_room"] += 1
            return None, None, []
        return got

    def _resolve_once(tx, ty, seed=None):
        if tracer is None:
            if ev:
                return no_evidence(seed)
            return raycast_boundary(tx, ty), None, []
        box = raycast_boundary(tx, ty)
        box_sqm = (0.0 if box is None else
                   (box[2] - box[0]) * (box[3] - box[1]) / 1e6)
        tr = _trace_with(tracer, tx, ty)
        # Doorless openings are left open, so an alcove belongs to its
        # room. When that lets the fill escape - it leaks, or it reaches
        # a room of another family - the openings on the leak path are
        # closed one at a time (close_leak_path); only when that fails is
        # the room traced again with every doorless opening closed. Either
        # way an unlabelled space shut off by a closed opening (the
        # toilet's entry lobby) joins the room again (join_beyond).
        if leaks(tr, seed):
            tr2, used = close_leak_path(tx, ty, seed, tr)
            if tr2 is None:
                closed = openings_tracer()
                tr2 = _trace_with(closed, tx, ty) if closed else None
                used = None if first_door_gap >= len(retry_gaps) else list(range(first_door_gap))
                if tr2 is not None and tr2.ok:
                    stats["leak_fallback_all"] += 1
            if tr2 is not None and tr2.area_sqm > 0 and not leaks(tr2, seed):
                tr = join_beyond(tr2, used, seed)
                stats["traced_after_closing_gaps"] += 1
        if leaks(tr, seed):
            # No wall, door or settled boundary closes the room on some
            # side: the plain lines drawn there do (lowest priority).
            lt = lines_tracer()
            tr3 = _trace_with(lt, tx, ty) if lt else None
            if (tr3 is not None and tr3.ok and tr3.area_sqm >= LINES_MIN_ROOM_SQM
                    and not leaks(tr3, seed)):
                tr = tr3
                stats["traced_with_plain_lines"] += 1
        if leaks(tr, seed) and first_door_gap < len(retry_gaps):
            # Last: the door openings found (close_at_found_doors) - on the
            # leak path first, then all of them with every opening. A room
            # the openings and plain lines close keeps the entry lobby
            # behind its own door; one they do not (H10 Tower 1 ground
            # floor: G. TOILET, its door between a nib and a corner) stops
            # at its doors, an unlabelled lobby shut off joined again.
            tr2, used = close_leak_path(tx, ty, seed, tr, doors=True)
            if tr2 is None:
                closed = gaps_tracer()
                tr2 = _trace_with(closed, tx, ty) if closed else None
                used = None
            if tr2 is not None and tr2.ok and tr2.area_sqm > 0 and not leaks(tr2, seed):
                tr = join_beyond(tr2, used, seed)
                stats["traced_at_found_doors"] += 1
        if leaks(tr, seed) and rule["lift_lobby_to_lift_bank"] and is_lift_lobby(seed):
            # A lift lobby open at an end (rule lift_lobby_to_lift_bank):
            # closed across it at the outer limit of its lift bank.
            tr4 = lobby_at_lift_bank(tx, ty, seed)
            if tr4 is not None:
                tr = tr4
                stats["lift_lobby_closed_at_bank"] += 1
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
                    and leaks(tr, seed)):
                if rule["balcony_open_to_refuge"] and open_to_refuge(tr, seed):
                    stats["balcony_open_to_refuge"] += 1
                elif ev:
                    return no_evidence(seed)          # a box is no evidence
                else:
                    stats["traced_overspill_boxed"] += 1
                    return box, None, []
            stats["traced_boundaries"] += 1
            holes = tr.holes if rule["deduct_voids"] else []
            if holes:
                stats["traced_holes_deducted"] += len(holes)
            return tr.bbox, tr.polygon, holes
        if ev:
            # No wall, facade, room, primary door or column closes it: the
            # four-ray box and the corridor box are guesses - not drawn,
            # listed as "no evidence" (rule boundary_on_evidence).
            return no_evidence(seed)
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
        if (box is None and seed is not None and looks_like_primary(seed["text"])
                and _LABEL_RULES.family_keyword(name_part(seed["text"]) or seed["text"])):
            cb = _corridor_box(tx, ty, f_walls, index=wall_idx, max_search=search)
            if cb is not None and not holds_other_room(
                    [(cb[0], cb[1]), (cb[2], cb[1]), (cb[2], cb[3]), (cb[0], cb[3])], seed):
                stats["corridor_boxes"] += 1
                return cb, None, []
        return box, None, []

    def no_evidence(seed):
        """Nothing closes this label's room on evidence (rule
        boundary_on_evidence): no boundary, the label reported."""
        stats["no_evidence_boundaries"] += 1
        if seed is not None and seed.get("sr_no") is not None:
            ev_rejected.add(seed["sr_no"])
        return None, None, []

    def is_lift_lobby(seed):
        if seed is None:
            return False
        fam = seed.get("family") or family_for_label(seed.get("text") or "", _FAMILY_RULES)["name"]
        return fam == "Lift Lobby"

    def lobby_at_lift_bank(tx, ty, seed):
        """A lift lobby the walls, doors and lifts do not close: its lift
        bank is the lifts within LIFT_BANK_REACH_MM of its label; the bank's
        outer limit along each axis is the far side of its lifts and the
        walls (and shafts, lifts, columns) running on from them, followed
        out at most LIFT_BANK_WALL_MM. Lines across the bank at both limits
        close the lobby; of the two ways (the bank along x or along y) the
        one that closes it on its own, the smaller, is kept. None when
        neither does."""
        from shapely.geometry import Point
        from shapely.ops import unary_union

        def _is_lift(d):
            # an X lift, a lift forced, or a lift named by its text (a bank
            # traced from its labels or as spaces - Aquila, 2026-10-17)
            if d.get("x_class") == "lift" or d.get("family_forced") == "Lift":
                return True
            return (d.get("family_forced") is None and d.get("source") == "P"
                    and family_for_label(d.get("label") or "", _FAMILY_RULES)["name"] == "Lift")
        lifts = [g for g in (_merged_geom(d) for d in drawn if _is_lift(d))
                 if g is not None and not g.is_empty and g.distance(Point(tx, ty)) <= LIFT_BANK_REACH_MM]
        if not lifts:
            return None
        block = [g for g in (_merged_geom(d) for d in drawn if d.get("x_class") in ("shaft", "lift"))
                 if g is not None and not g.is_empty]
        block += [g for g in (_safe_poly(st["pts"]) for st in walls.get("strips") or ()) if g is not None]
        block += [g for g in ((c if hasattr(c, "geom_type") else _safe_poly(c)) for c in job.get("columns") or ())
                  if g is not None and not g.is_empty]
        solid = unary_union(block).buffer(5.0) if block else None
        step = 25.0

        def reach(x, y, dx, dy):
            # from (x, y), on along (dx, dy) while inside the solid
            t = 0.0
            while solid is not None and t < LIFT_BANK_WALL_MM and solid.contains(Point(x + dx * (t + step),
                                                                                         y + dy * (t + step))):
                t += step
            return t
        best = None
        for along_x in (True, False):
            hi, lo, across = [], [], []
            for g in lifts:
                x0, y0, x1, y1 = g.bounds
                if along_x:
                    cy = (y0 + y1) / 2.0
                    hi.append(x1 + reach(x1, cy, 1.0, 0.0))
                    lo.append(x0 - reach(x0, cy, -1.0, 0.0))
                    across += [y0, y1]
                else:
                    cx = (x0 + x1) / 2.0
                    hi.append(y1 + reach(cx, y1, 0.0, 1.0))
                    lo.append(y0 - reach(cx, y0, 0.0, -1.0))
                    across += [x0, x1]
            a0, a1 = min(across), max(across)
            H, L = max(hi), min(lo)
            lines = ([(H, a0, H, a1), (L, a0, L, a1)] if along_x else [(a0, H, a1, H), (a0, L, a1, L)])
            t2 = extend_tracer(lines)
            tr_ = _trace_with(t2, tx, ty) if t2 is not None else None
            if tr_ is not None and tr_.ok and tr_.area_sqm > 0 and not leaks(tr_, seed):
                if best is None or tr_.area_sqm < best[0].area_sqm:
                    best = (tr_, lines)
        if best is None:
            return None
        lobby_bank_lines.extend(best[1])              # evidence to the check after drawing
        return best[0]

    def raycast_boundary(tx, ty):
        # The same walls the tracer follows, so the box and the traced
        # outline describe the same room.
        return (_hv_boundary(tx, ty, f_walls, index=wall_idx,
                             max_search=search) if f_walls else None)

    _segs_cache = []

    def _segs_np():
        if not _segs_cache:
            import numpy as np
            _segs_cache.append(
                np.array([(s[0], s[1], s[2], s[3]) for s in raw_segs],
                         dtype=np.float64) if raw_segs else np.zeros((0, 4)))
        return _segs_cache[0]

    # Texts inside a void belong to it - after the X shafts inside the void
    # (a rainwater pipe cut-out at a podium corner) have taken their own. A
    # text inside naming a family of its own (BALCONY, TERRACE, DOUBLE
    # HEIGHT) names the void; the X sequence keeps it a void (2026-10-16).
    for probe, rec in zip(void_probes, void_recs):
        inside = []
        for t in floor_texts:
            if not t["claimed"] and _inside_boundary(probe, t["x"], t["y"]):
                t["claimed"] = True
                inside.append(t)
                if t["sr_no"] is not None:
                    text_status[t["sr_no"]] = "void"
        named = [t for t in inside
                 if not is_ignorable_label(t["text"]) and not is_note_text(t["text"])
                 and text_family(t) not in ("Unknown", "Void", "Ignore")]
        if named:
            best = choose_label(named, _LABEL_RULES, centre=(rec["cx"], rec["cy"])) or named[0]
            rec.update({"label": best["text"], "sr_no": best["sr_no"]})
            if best["sr_no"] is not None:
                text_status[best["sr_no"]] = "primary"
            stats["void_named_by_text"] += 1

    # An X is a shaft as drawn, a narrow strip included: there is no
    # separate cut-out boundary (the drawings have none).
    # An unnamed X standing inside a named shaft is part of that shaft (the
    # second strip of an L-shaped opening, a slab opening in a riser): a
    # shaft, under the shaft's name.
    named = [d for d in drawn if d.get("source") == "X"
             and d["label"] != X_UNKNOWN_LABEL]
    for d in drawn:
        if d.get("source") != "X" or d["label"] != X_UNKNOWN_LABEL:
            continue
        b = d["bbox"]
        cx, cy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
        area = (b[2] - b[0]) * (b[3] - b[1])
        host = [h for h in named
                if h["bbox"][0] <= cx <= h["bbox"][2] and h["bbox"][1] <= cy <= h["bbox"][3]
                and (h["bbox"][2] - h["bbox"][0]) * (h["bbox"][3] - h["bbox"][1]) > area * 1.2]
        if host:
            # ... of whatever family the texts gave that host (a lift well,
            # a shaft) - the texts decide, not the X.
            d.update({"label": host[0]["label"],
                      "family_forced": family_for_label(host[0]["label"], _FAMILY_RULES)["name"]
                      if family_for_label(host[0]["label"], _FAMILY_RULES)["name"] != "Unknown" else "Shaft"})
            stats["unnamed_x_named_by_shaft"] += 1

    # A shaft whose X marks only a strip of it: the rest of its cell is
    # still the shaft, so no room runs into it through the shaft's access
    # door (H10 Tower 2: the ladies' toilet took the open half of PL-SH-03
    # 1200x2100). The strip alone stays the opening unless x_grows_to_cell.
    if rule["existing_boundaries_walls"] and not rule["x_grows_to_cell"] and tracer is not None:
        cell_edges = []
        for ci, ti in cluster_assigned.items():
            want = floor_texts[ti].get("want")
            if not want:
                continue
            v = validated[ci]
            if (ci, ti) in cell_pairs:
                cell = x_cell(v["bbox"], want)
                if cell is not None:
                    cell_edges.extend(_outline_edges(cell[0], cell[1]))
                    stats["shaft_cells_as_walls"] += 1
                continue
            # an X region (an L of strips) whose box has the stated size:
            # the box is the shaft, the open corner of it included
            b = v["bbox"]
            if v.get("poly") and len(v["poly"]) >= 3 and stated_fits(want, b[2] - b[0], b[3] - b[1]):
                if shoelace_area_sqm(v["poly"]) < 0.9 * (b[2] - b[0]) * (b[3] - b[1]) / 1e6:
                    cell_edges.extend(_outline_edges(None, b))
                    stats["shaft_cells_as_walls"] += 1
        if cell_edges:
            f_walls = f_walls + cell_edges
            wall_idx = BandIndex(f_walls, band=INDEX_BAND, axis_ratio=WALL_AXIS_RATIO)
            tracer = build_tracer(f_walls)
            del closed_tracer[:]
            del lines_tracer_box[:]
            leak_tracers.clear()

    # -- The X sequence (xplace, 2026-10-16, final) -----------------------
    # Every X mark of the floor decided ONCE: 2 shaft, 3 lift, 4 window,
    # 5 cut-out, 6 column, 7 void (xplace.resolve_x). Nothing after it
    # changes an X's class - not the rooms and doors settled together, the
    # element engine, the geometry engine, the final drawing, a family
    # picked by hand. The element pipeline (rule element_pipeline) decides
    # them here, before any room is traced from a label: an X stands "in a
    # room" when the space round it is enclosed, whatever is written in
    # it; a stair's well has a flight on either side. The text-first pass
    # decides them after the rooms, on the rooms traced (below).
    x_log, x_columns = [], []
    x_decided = False

    def run_x_sequence(items, room_of, stair_well_of, geom_of):
        """Decide every X of the floor. items: the X and void boundaries
        (changed in place: a window, a cut-out or a column leaves the list
        - it is no room); room_of(g) -> truthy when the X stands in a room;
        stair_well_of(g) -> True for a stair's well; geom_of(d) -> the
        item's polygon."""
        from . import xplace as _xp
        item_of = {}
        for d_ in items:
            if d_.get("source") in ("X", "V") and d_.get("x_fid") is not None:
                item_of.setdefault(d_["x_fid"], d_)
        moved_x = []
        for rec in x_recs:
            f_ = rec["facts"]
            d = item_of.get(rec["fid"])
            if d is None and not rec.get("held"):
                if rec.get("absorbed"):
                    rec.update(cls=None, step=None, why="the same place as another X mark: one X there")
                else:
                    rec.update(cls=None, step=None, why="not drawn: its place went to another boundary")
                x_log.append(rec)
                continue
            named = lift_label = shaft_name = in_room = stair_well = False
            if d is not None:
                lab_ = " ".join(str(d.get("label") or "").split())
                named = bool(lab_) and lab_ != X_UNKNOWN_LABEL and d.get("source") != "V"
                if named:
                    fam_ = family_for_label(lab_, _FAMILY_RULES)["name"]
                    lift_label = fam_ == "Lift"
                    shaft_name = fam_ in ("Shaft", "Plumbing", "Fire / Life Safety", "MEP / Services") or \
                        bool(shaft_like_label(lab_, fam_) and fam_ != "Lift")
                g_ = geom_of(d)
                if g_ is not None and not g_.is_empty:
                    # whether the X stands in a room decides only step 5
                    # (a loose cut-out, or one with a cut-out tag): asked
                    # only then - a trace for every X is not needed
                    walled_ = f_.get("gap") is not None and f_["gap"] <= _xp.GAP_MM
                    if not f_.get("in_wall") and not f_.get("column") and \
                            f_.get("area_sqm", 0.0) <= _xp.VOID_MIN_SQM and \
                            (f_.get("ctag") or (not named and not walled_)):
                        in_room = room_of(g_) is not None
                    stair_well = bool(stair_well_of(g_))
            res_ = _xp.resolve_x(f_, named=named, lift_label=lift_label, shaft_name=shaft_name,
                                 in_room=in_room, stair_well=stair_well)
            rec.update(cls=res_["cls"], step=res_["step"], why=res_["why"], trail=res_["trail"],
                       label=(d.get("label") if d is not None else None))
            stats["x_class_" + res_["cls"]] += 1
            x_log.append(rec)
            cls_ = res_["cls"]
            tag_ = (cls_, res_["step"], res_["why"])
            if d is not None:
                d["x_class"], d["x_step"], d["x_why"] = cls_, res_["step"], res_["why"]
            if cls_ in ("window", "cutout", "column"):
                if d is not None:
                    g_ = geom_of(d)
                    rect_ = ([tuple(p) for p in list(g_.minimum_rotated_rectangle.exterior.coords)[:4]]
                             if g_ is not None and not g_.is_empty else rec["rect"])
                    moved_x.append(d)
                    for sr in [d.get("sr_no")] + list(d.get("absorbed_srs") or []):
                        if sr is not None:
                            text_status[sr] = cls_
                else:
                    rect_ = rec["rect"]
                xs_, ys_ = [p[0] for p in rect_], [p[1] for p in rect_]
                item_ = {"poly": rect_, "why": res_["why"], "cx": sum(xs_) / 4.0, "cy": sum(ys_) / 4.0,
                         "bbox": [round(min(xs_)), round(min(ys_)), round(max(xs_)), round(max(ys_))],
                         "x_tag": tag_, "in_wall": bool(f_.get("in_wall"))}
                if cls_ == "window":
                    windows.append(item_)
                elif cls_ == "cutout":
                    item_["name"] = rec.get("name") or CUTOUT_NAME
                    cutouts.append(item_)
                else:
                    x_columns.append(item_)
            elif cls_ == "shaft":
                d["family_forced"] = "Shaft"
            elif cls_ == "lift":
                d["family_forced"] = "Lift"
                if " ".join(str(d.get("label") or "").split()) == X_UNKNOWN_LABEL:
                    d["label"] = X_LIFT_LABEL         # an unnamed X lift: no "Unknown Shaft"
            else:                                     # void
                d["family_forced"] = "Void"
                if " ".join(str(d.get("label") or "").split()) == X_UNKNOWN_LABEL:
                    d["label"] = X_VOID_LABEL         # an unnamed X void: no "Unknown Shaft"
                if stair_well:
                    d["stair_well"] = True
            if d is not None:
                rec["label"] = d.get("label")         # (as named once decided: LIFT, VOID)
        if moved_x:
            items[:] = [d for d in items if not any(d is m for m in moved_x)]
        # what is decided is locked: no wall built after this is derived
        # over a shaft's opening, a lift well, a void or a loose cut-out
        for d_ in items:
            if d_.get("x_class") in ("shaft", "lift", "void"):
                g_ = geom_of(d_)
                if g_ is not None and not g_.is_empty:
                    LM.lock("x_" + d_["x_class"], [g_])
        LM.lock("x_cutout_loose", [c_["poly"] for c_ in cutouts if not c_.get("in_wall")])
        LM.lock("x_cutout_in_wall", [c_["poly"] for c_ in cutouts if c_.get("in_wall")])
        LM.lock("x_window", [w_["poly"] for w_ in windows if w_.get("poly")])
        LM.lock("x_column", [c_["poly"] for c_ in x_columns])
        return moved_x

    if x_recs and ep and tracer is not None:
        # the element pipeline: every X decided now, on the walls and the
        # spaces, before a room is traced from any label
        from . import elementid as _eid_x
        in_space_cache = {}
        # the open floor is no room: a space bigger than a room an X stands
        # in, or most of the plate (the plate's own edge closes every trace)
        plate_sqm_ = abs(shoelace_area_sqm(floor_poly)) if floor_poly else 0.0
        in_space_max_ = min(X_IN_SPACE_MAX_SQM, X_IN_SPACE_PLATE_SHARE * plate_sqm_) if plate_sqm_ > 0 \
            else X_IN_SPACE_MAX_SQM

        def _x_in_space(g_):
            """The X stands in an enclosed space: the trace from a point
            just outside one of its sides closes - a room (labelled or
            not), a staircase - no bigger than a room an X stands in (the
            open floor is not a room), and the X is in that space: inside
            its outline (holes filled - the space was traced round the X),
            or in a notch of it (within X_IN_ROOM_REACH_MM, inside its
            convex hull). A probe that lands in the room beyond the X's
            wall finds that room, not the X's place (BHUB: a sleeve under
            an electrical room's door is outside the room)."""
            from shapely.geometry import Polygon as _SPg
            key_ = tuple(round(v) for v in g_.bounds)
            if key_ in in_space_cache:
                return in_space_cache[key_]
            b_ = g_.bounds
            cx_, cy_ = (b_[0] + b_[2]) / 2.0, (b_[1] + b_[3]) / 2.0
            pt_ = g_.representative_point()
            hit = None
            for sx_, sy_ in ((cx_, b_[1] - X_IN_SPACE_PROBE_MM), (cx_, b_[3] + X_IN_SPACE_PROBE_MM),
                             (b_[0] - X_IN_SPACE_PROBE_MM, cy_), (b_[2] + X_IN_SPACE_PROBE_MM, cy_)):
                if not _point_in_polygon(sx_, sy_, floor_poly):
                    continue
                tr_ = _trace_with(tracer, sx_, sy_)
                if tr_ is None or not tr_.ok or not (X_IN_SPACE_MIN_SQM <= tr_.area_sqm <= in_space_max_):
                    continue
                try:
                    sp_ = _SPg([tuple(p[:2]) for p in tr_.polygon]).buffer(0)
                except (ValueError, TypeError, IndexError):
                    continue
                if sp_.is_empty:
                    continue
                if sp_.buffer(1.0).contains(pt_) or \
                        (sp_.distance(g_) <= X_IN_ROOM_REACH_MM and sp_.convex_hull.contains(pt_)):
                    hit = (None, False)
                    stats["x_in_space_traced"] += 1
                    break
            in_space_cache[key_] = hit
            return hit

        def _x_stair_well(g_):
            """A flight of a staircase on either side of the X."""
            try:
                return _eid_x.between_flights(g_, g_.buffer(X_WELL_REACH_MM), raw_segs)
            except Exception:                         # noqa: BLE001
                return False

        run_x_sequence(drawn, _x_in_space, _x_stair_well, _merged_geom)
        x_decided = True
        stats["x_decided_before_rooms"] += 1
    _tick("05 phase A (labels to X marks), X decided")

    # -- The rooms from the spaces (rule rooms_from_spaces, 2026-10-17) ---
    # The walls, the doors and the X marks are decided. First the shafts'
    # compartments (shaft_compartments: an X's walled cell, its door and
    # the partition lines closed, is the shaft's - rule shaft_compartment,
    # run here before any room; a compartment is a wall to the rooms and
    # never wall material). Then every enclosed space of the floor is
    # traced with no text (spaces.py): the free floor is cut into its
    # pieces on a grid and each piece traced once on the walls and doors,
    # with the doorless openings closed if that leaks, on the plain lines
    # last (not under rule boundary_on_evidence). The staircases are the
    # spaces holding a flight with UP / DOWN or a stair name in them, or a
    # named landing with its flight in the cell beside it; an open lift
    # lobby is closed at its lift banks' ends (rule lift_lobby_to_lift_bank).
    # Only then is each space named by the texts standing inside it - never
    # by one outside. A space of SHAFT_ROOM_MAX_SQM or less along a shaft's
    # X with a door of its own is the shaft's room (AI_MEP) whatever is
    # written in it. A space holding two rooms' names is left to Phase B,
    # whose leak search splits it; so is a label no space took.
    shaft_review = []
    shaft_notes = {}
    spaces_log_out = []                  # what became of every space (stats spaces_by_floor)
    spaces_items_out = []                # (log entry, the item drawn) - the later passes' verdict
    spaces_ready = False                 # the stage ran: its helpers can be used after Phase B

    def _shaft_tracer_on(items):
        """The shaft compartments' tracer (built once, on demand): every
        opening closed - the doors too - and the plain lines of the drawing
        (a partition line between a shaft and its room) as walls. items:
        the X items - a doorway or gap closer never runs through an X."""
        def _build():
            never = [k.upper() for k in _HARD_EXCLUDE_KEYWORDS]
            skip_words = [k.upper() for k in _WALL_EXCLUDE_KEYWORDS]
            plain = []
            for sg in raw_segs:
                lu = (sg[4] or "").upper()
                if (any(k in lu for k in never) or any(k in lu for k in skip_words)
                        or _is_fixture_layer(lu) or (len(sg) > 7 and sg[7])
                        or _other_level_xref(lu)
                        or (x_lines and _seg_key(sg) in x_lines)
                        or sg[5] < 50.0):
                    continue
                plain.append((sg[0], sg[1], sg[2], sg[3]))
            # on the walls as drawn (faces, caps, hatch edges, slit and
            # doorway closers) - not on the X outlines settled as walls
            # for the rooms, which would close every trace at the X
            closers = (list(door_closing) + list(threshold) + list(gap_closing) + list(slits) +
                       list(face_closers) + list(retry_gaps))
            if closers:
                # a doorway or gap closer never runs through an X: a
                # shaft's door is on its side (XA: a lift's door line
                # carried 1.9 m on through SPARE SH cut 85 mm off it)
                from shapely.geometry import box as _xb, LineString as _xl
                from shapely.strtree import STRtree as _xst
                xin = []
                for d0 in items:
                    if d0.get("source") != "X":
                        continue
                    for r0 in (d0.get("xrects") or [d0.get("bbox")]):
                        if r0 and r0[2] - r0[0] > 100.0 and r0[3] - r0[1] > 100.0:
                            xin.append(_xb(r0[0] + 30.0, r0[1] + 30.0, r0[2] - 30.0, r0[3] - 30.0))
                if xin:
                    xt_ = _xst(xin)
                    kept_ = []
                    for c0 in closers:
                        ln = _xl([(c0[0], c0[1]), (c0[2], c0[3])])
                        if any(xin[k].intersection(ln).length > 50.0 for k in xt_.query(ln)):
                            stats["closers_through_x_dropped"] += 1
                            continue
                        kept_.append(c0)
                    closers = kept_
            return build_tracer(list(faces) + list(caps) + closers +
                                list(edges) + list(hatch_lines) +
                                plain + list(x_frame_segs))
        return _build

    if rule["rooms_from_spaces"] and ep and tracer is not None:
        try:
            from . import spaces as _spc
            from . import elements as _el_s
            from shapely.geometry import Polygon as _SPoly, Point as _SPt, box as _SBox
            # the shafts' compartments before any room (pass 10 below skips
            # the X marks seen here); one grown past its X is a wall to the
            # rooms traced now and never wall material (LockedMask)
            if rule["shaft_compartment"]:
                shaft_compartments(drawn, _shaft_tracer_on(drawn), raw_segs, floor_poly, stats, shaft_review,
                                   shaft_notes, floor_family_overrides, family_rules=_FAMILY_RULES)
                grown = []
                for d0 in drawn:
                    if d0.get("source") != "X" or not d0.get("cell") or d0.get("x_class", "shaft") != "shaft":
                        continue
                    g0 = _merged_geom(d0)
                    if g0 is None or g0.is_empty or g0.geom_type != "Polygon":
                        continue
                    xb_ = d0.get("x_box")
                    if xb_ and g0.area <= 1.05 * max(1.0, (xb_[2] - xb_[0]) * (xb_[3] - xb_[1])):
                        continue                      # the X itself, a wall to the rooms already
                    grown.append(g0)
                if grown:
                    LM.lock("x_shaft", grown)
                    cell_edges2 = []
                    for g0 in grown:
                        cell_edges2.extend(_polygon_edges([tuple(p) for p in list(g0.exterior.coords)[:-1]]))
                    f_walls = f_walls + cell_edges2
                    wall_idx = BandIndex(f_walls, band=INDEX_BAND, axis_ratio=WALL_AXIS_RATIO)
                    tracer = build_tracer(f_walls)
                    for cache_ in (closed_tracer, lines_tracer_box, base_box):
                        del cache_[:]
                    leak_tracers.clear()
                    stats["spaces_compartments_as_walls"] += len(grown)
                _tick("05a shaft compartments (before the rooms)")
            # Rule walls_before_spaces (2026-10-17; the sequence 1, 5, 2, W2,
            # 5a, 4, 5b ... W3, 12): the walls built in full before any space
            # is traced - the mass between the paired faces and the wall
            # hatches, columns and door openings cut out, never over an X the
            # sequence decided (LockedMask). Its rings are walls to the room
            # tracer and no space to the grid, so no space is traced inside a
            # wall core; pass 11 builds the walls once more on the rooms (W3,
            # the revalidation). Simulated first on P285 11th, BHUB 1st and
            # Aquila 2nd: statements identical, the grid's pieces halved.
            mass_early = []
            if rule["walls_before_spaces"]:
                try:
                    strips_e = (early_wt["strips"] if early_wt is not None else walls.get("strips")) or []
                    openings_e = [w["poly"] for w in windows + cutouts if w.get("in_wall", True)]
                    doors_e = [[[float(x), float(y)] for x, y in q] for q in (early_doors or [])]
                    wcounts_e = {}
                    pieces_e, _rep_e = wall_polygons(
                        strips_e, floor_poly, ([] if early_wt is not None else job.get("wall_hatches")),
                        job.get("columns") or [], ([] if OPENINGS_IN_WALLS else openings_e), doors_e,
                        list(job.get("column_rejects") or []) + (openings_e if OPENINGS_IN_WALLS else []),
                        bridge=(openings_e if OPENINGS_IN_WALLS else []), counts=wcounts_e,
                        locked=(LM.union(LOCKED_NEVER_WALL) if ep else None))
                    mass_early = [p for p in (pieces_e.get(LN.WALL) or [])
                                  if p is not None and not p.is_empty and p.area > 0]
                    if mass_early:
                        edges_e = []
                        for p in mass_early:
                            for ring in [p.exterior] + list(p.interiors):
                                edges_e.extend(_polygon_edges([tuple(c) for c in list(ring.coords)[:-1]]))
                        f_walls = f_walls + edges_e
                        wall_idx = BandIndex(f_walls, band=INDEX_BAND, axis_ratio=WALL_AXIS_RATIO)
                        tracer = build_tracer(f_walls)
                        for cache_ in (closed_tracer, lines_tracer_box, base_box):
                            del cache_[:]
                        leak_tracers.clear()
                        stats["walls_before_spaces_pieces"] += len(mass_early)
                        stats["walls_before_spaces_sqm"] += round(sum(p.area for p in mass_early) / 1e6, 2)
                        stats["walls_before_spaces_edges"] += len(edges_e)
                except Exception:                     # noqa: BLE001
                    import logging
                    logging.getLogger(__name__).exception("walls before the spaces on %s", floor_name)
                _tick("05w walls W2 built before the spaces (the mass as walls)")
            if tracer is None:
                raise RuntimeError("no tracer once the compartments were added")
            # what no space is: the X cells, the voids, the loose cut-outs,
            # the columns - and the shapes the Geometry tab decided
            blocked, x_shaft_g, x_lift_g, x_label_of = [], [], [], {}
            for d0 in drawn:
                if d0.get("source") in ("X", "V", "G"):
                    g0 = _merged_geom(d0)
                    if g0 is None or g0.is_empty:
                        continue
                    blocked.append(g0)
                    if d0.get("x_class") == "shaft":
                        x_shaft_g.append(g0)
                        x_label_of[id(g0)] = str(d0.get("label") or "")
                    elif d0.get("x_class") == "lift":
                        x_lift_g.append(g0)
            for c0 in cutouts:
                if not c0.get("in_wall") and c0.get("poly"):
                    try:
                        blocked.append(_SPoly(c0["poly"]).buffer(0))
                    except (ValueError, TypeError):
                        pass
            for c0 in job.get("columns") or ():
                try:
                    blocked.append(_SPoly(c0).buffer(0))
                except (ValueError, TypeError):
                    pass
            tab_g = []
            for g0 in geo:
                try:
                    pts0 = [tuple(p) for p in (g0.get("poly") or [])]
                    q0 = _SPoly(pts0).buffer(0) if len(pts0) >= 3 else _SBox(*g0["bbox"])
                    if not q0.is_empty:
                        tab_g.append(q0)
                except (ValueError, TypeError, KeyError):
                    continue
            door_lines = list(retry_gaps[first_door_gap:])       # the doors found, closed
            # the flights' treads are walls to the room search (drawn on a
            # structure layer, H10 Tower 1: on COLUMN), and would cut a
            # flight's cell into strips: they are left out of the grid, and
            # each flight is traced first, from its middle, on the walls
            # less its treads
            segs_arr = _segs_np()
            runs_sp = _tread_runs(segs_arr, with_runs=True) if len(segs_arr) else []

            def _is_tread_line(w):
                L_ = math.hypot(w[2] - w[0], w[3] - w[1])
                if L_ < 0.6 * STAIR_TREAD_MIN_LEN:
                    return False
                hz_ = abs(w[3] - w[1]) <= 0.03 * L_
                vt_ = abs(w[2] - w[0]) <= 0.03 * L_
                mx_, my_ = (w[0] + w[2]) / 2.0, (w[1] + w[3]) / 2.0
                for _x, _y, _n, (rb_, horiz_, longest_) in runs_sp:
                    if ((horiz_ and hz_) or (not horiz_ and vt_)) and L_ <= 1.2 * longest_ \
                            and rb_[0] - 30 <= mx_ <= rb_[2] + 30 and rb_[1] - 30 <= my_ <= rb_[3] + 30:
                        return True
                return False
            grid_walls = [w for w in f_walls if not (runs_sp and _is_tread_line(w))]
            n_treads_out = len(f_walls) - len(grid_walls)
            stats["spaces_tread_lines_out"] += n_treads_out
            pieces = _spc.free_pieces(grid_walls + door_lines, floor_poly, blocked + tab_g + mass_early,
                                      cell_mm=SPACES_GRID_MM, open_floor_sqm=SPACES_OPEN_FLOOR_SQM,
                                      min_half_width_mm=SPACES_MIN_HALF_WIDTH_MM, min_sqm=SPACES_MIN_SQM,
                                      open_floor_share=SPACES_OPEN_FLOOR_SHARE)
            stats["spaces_pieces"] += pieces["pieces"]
            stats["spaces_open_floor_sqm"] += round(pieces["open_floor_sqm"], 1)
            stats["spaces_cavities"] += pieces["cavities"]
            t_doors = (tracer_with(list(range(first_door_gap, len(retry_gaps))))
                       if first_door_gap < len(retry_gaps) else tracer)

            def _trace_on(t_):
                def _f(x_, y_, _t=t_):
                    tr_ = _trace_with(_t, x_, y_) if _t is not None else None
                    return [tuple(p[:2]) for p in tr_.polygon] if (tr_ is not None and tr_.ok) else None
                return _f
            attempts = [(_trace_on(t_doors), "walls and doors"), (_trace_on(gaps_tracer()), "doorless openings closed")]
            if not ev:
                attempts.append((_trace_on(lines_tracer()), "plain lines closed"))
            spaces_ = []
            if runs_sp:
                t_flight = None
                try:
                    t_flight = build_tracer(grid_walls) if n_treads_out else tracer
                    if t_flight is not None and door_lines:
                        t_flight = t_flight.extended(door_lines)
                        t_flight = t_flight if t_flight.ok else None
                except (MemoryError, ValueError, OverflowError):
                    t_flight = None
                if t_flight is not None:
                    run_seeds = [(r_[0], r_[1], 0.0) for r_ in runs_sp if _point_in_polygon(r_[0], r_[1], floor_poly)]
                    spaces_ = _spc.trace_pieces(run_seeds, [(_trace_on(t_flight), "walls and doors, the flight's treads left out")],
                                                min_sqm=SPACES_MIN_SQM, min_half_width_mm=SPACES_MIN_HALF_WIDTH_MM)
                    # a flight's trace that ran out over the floor is no space of its own
                    spaces_ = [s_ for s_ in spaces_ if s_["g"].area <= STAIR_FOUND_MAX_SQM * 1e6]
                    stats["spaces_flights_traced"] += len(spaces_)
            spaces_ += _spc.trace_pieces(pieces["seeds"], attempts, min_sqm=SPACES_MIN_SQM,
                                         min_half_width_mm=SPACES_MIN_HALF_WIDTH_MM, taken=[s_["g"] for s_ in spaces_])
            if tab_g:
                # a shape kept, mapped or deleted in the Geometry tab is the tab's
                keep_ = []
                for s_ in spaces_:
                    if any(s_["g"].intersection(q0).area > 0.5 * s_["g"].area for q0 in tab_g):
                        stats["spaces_in_tab_shapes"] += 1
                        continue
                    keep_.append(s_)
                spaces_ = keep_
            # the doors, for the shaft rooms, the pockets and the lobbies
            door_g, door_w = [], []
            for q_ in early_doors or []:
                try:
                    door_g.append(_SPoly([(p[0], p[1]) for p in q_]).buffer(0))
                    ax_ = _el_s.door_axes(q_)
                    door_w.append(ax_[6] if ax_ is not None else 900.0)
                except Exception:                     # noqa: BLE001
                    continue
            strips_g = []
            for st_ in walls.get("strips") or ():
                try:
                    strips_g.append(_SPoly(st_["pts"]).buffer(0))
                except (ValueError, TypeError, KeyError):
                    pass

            def _free_text(t):
                """A text a space may take: not claimed, or one the labels
                could close no room on (no evidence, no walls) - a lift
                lobby's name whose open end the lift bank closes."""
                return (not t["claimed"]) or text_status.get(t.get("sr_no")) in ("no_evidence", "no_walls")

            def _bank_lobbies(lifts_g, others=None, drawn_only=False):
                """(4) An open lift lobby: the floor between two lift banks
                facing each other, closed at the banks' ends (rule
                lift_lobby_to_lift_bank) - on Phase B's bank lines where a
                lobby's name stands in it (the lines are evidence to the
                check after drawing), else as the floor between the banks
                less the walls, the doors found and the spaces traced.
                Returns the new spaces."""
                out_ = []
                if not rule["lift_lobby_to_lift_bank"] or len(lifts_g) < 2:
                    return out_
                # drawn_only (after the labels): a space left to the labels is no
                # room here - the lobby it held is still open floor
                have_ = ([s_["g"] for s_ in spaces_ if s_.get("drawn") or not drawn_only]
                         + [g0 for g0 in (others or []) if g0 is not None and not g0.is_empty])
                for g_, lines_ in _spc.lift_bank_lobbies(lifts_g, strips_g + door_g, have_, floor_poly,
                                                         gap_mm=SPACES_LOBBY_GAP_MM, min_sqm=SPACES_LOBBY_MIN_SQM):
                    poly_ = [tuple(p) for p in list(g_.exterior.coords)[:-1]]
                    seed_ = next((t for t in floor_texts if _free_text(t)
                                  and _point_in_polygon(t["x"], t["y"], poly_)
                                  and family_for_label(t["text"], _FAMILY_RULES)["name"] == "Lift Lobby"), None)
                    g2_ = None
                    if seed_ is not None:
                        tr4 = lobby_at_lift_bank(seed_["x"], seed_["y"], seed_)
                        if tr4 is not None and tr4.ok and len(tr4.polygon) >= 3:
                            try:
                                g2_ = _SPoly([tuple(p[:2]) for p in tr4.polygon]).buffer(0)
                            except (ValueError, TypeError):
                                g2_ = None
                    how_ = "closed at the lift banks' ends (on the bank's lines)"
                    if g2_ is None or g2_.is_empty or g2_.geom_type != "Polygon":
                        g2_ = g_
                        how_ = "the floor between two lift banks, closed at their ends"
                        lobby_bank_lines.extend(lines_)
                    if any(g2_.intersection(q_).area > 0.5 * min(g2_.area, q_.area) for q_ in have_):
                        continue
                    have_.append(g2_)
                    out_.append({"g": g2_, "how": how_, "lobby": True})
                    stats["spaces_lift_lobbies"] += 1
                    stats["lift_lobby_closed_at_bank"] += 1
                return out_
            spaces_ += _bank_lobbies(x_lift_g)
            marks_sp = [_SPt(p[0], p[1]) for p in (job.get("stair_marks") or [])]
            SHAFT_LIKE_FAMS = ("Shaft", "Plumbing", "Fire / Life Safety")

            def _text_info(t):
                txt = str(t.get("text") or "")
                fam_ = family_for_label(txt, _FAMILY_RULES)["name"]
                skip = None
                if is_ignorable_label(txt):
                    skip = "ignored"
                elif X_TAGS_NEVER_NAME and _x_tag_kind(txt):
                    skip = "tag"
                elif _is_plain_tag(txt):
                    skip = "tag"
                elif rule["skip_notes"] and is_note_text(txt) and not looks_like_primary(txt):
                    skip = "note"
                return {"t": t, "text": txt, "fam": fam_, "skip": skip,
                        "device": bool(rule["equipment_tags"] and is_device_tag(txt)),
                        "primary": bool(looks_like_primary(txt)),
                        "shaft_like": fam_ in SHAFT_LIKE_FAMS or (
                            bool(shaft_like_label(txt, fam_)) and fam_ in ("Unknown", "MEP / Services")),
                        "want": t.get("want")}

            def _fits(ti, g_):
                if not ti.get("want"):
                    return False
                b_ = g_.bounds
                try:
                    return bool(stated_fits(ti["want"], b_[2] - b_[0], b_[3] - b_[1]))
                except Exception:                     # noqa: BLE001
                    return False

            # each space's outline, texts, treads and marks
            def _prep(s_):
                s_["poly"] = [tuple(p) for p in list(s_["g"].exterior.coords)[:-1]]
                s_["bbox"] = tuple(float(v) for v in s_["g"].bounds)
                s_["inside"] = [_text_info(t) for t in floor_texts
                                if _free_text(t) and _point_in_polygon(t["x"], t["y"], s_["poly"])]
                s_["treads"] = _spc.treads_in(s_["g"], runs_sp, segs_arr, _stair_treads)
                s_["stair_txt"] = [ti for ti in s_["inside"] if not ti["skip"] and ti["fam"] == "Staircase"]
                s_["mark"] = any(s_["g"].contains(m_) for m_ in marks_sp)
            for s_ in spaces_:
                _prep(s_)
            # the staircases: a flight (STAIR_MIN_TREADS treads) with UP /
            # DOWN or a stair name in it; a named landing with its flight in
            # the cell beside it (across the stair core's wall), and that
            # flight as the other part of it
            flights = [s_ for s_ in spaces_ if s_["treads"] >= STAIR_MIN_TREADS]
            for s_ in flights:
                if s_["stair_txt"] or s_["mark"]:
                    s_["stair"] = "%d treads and %s" % (s_["treads"], "a stair name" if s_["stair_txt"] else "UP / DOWN in it")
            for s_ in spaces_:
                if s_.get("stair") or not s_["stair_txt"]:
                    continue
                near = [f_ for f_ in flights if f_ is not s_
                        and _spc.shares_side(s_["g"], [f_["g"]], side_mm=500.0, gap_mm=STAIR_PART_REACH_MM)]
                if near:
                    s_["stair"] = "a stair name, its flight of %d treads in the cell beside it" % near[0]["treads"]
            for f_ in flights:
                if f_.get("stair"):
                    continue
                named = [s_ for s_ in spaces_ if s_.get("stair") and s_["stair_txt"] and s_ is not f_
                         and _spc.shares_side(f_["g"], [s_["g"]], side_mm=500.0, gap_mm=STAIR_PART_REACH_MM)]
                if named:
                    f_["stair"] = "%d treads: the other part of %s beside it" % (f_["treads"], named[0]["stair_txt"][0]["text"])
                    f_["part_label"] = named[0]["stair_txt"][0]["text"]

            def _name_space(s_):
                """The space's name, family and why - the text last. Returns
                (the text picked or None, the family forced or None, why,
                options)."""
                g_ = s_["g"]
                a_ = g_.area / 1e6
                inside_ = s_["inside"]
                if s_.get("stair"):
                    pick_ = s_["stair_txt"][0] if s_["stair_txt"] else None
                    return pick_, "Staircase", s_["stair"], {"fallback_label": s_.get("part_label") or "STAIRCASE",
                                                             "part": bool(s_.get("part_label"))}
                shafts_ = _spc.shares_side(g_, x_shaft_g, side_mm=SHAFT_ROOM_SIDE_MM) if a_ <= SHAFT_ROOM_MAX_SQM else []
                door_ = _spc.touches_door(g_, door_g, touch_mm=ROOM_DOOR_TOUCH_MM, widths=door_w) if door_g else False
                s_["has_door"] = door_
                if shafts_ and door_:
                    # the shaft's room, whatever is written in it (2026-10-17)
                    pick_ = next((ti for ti in inside_ if not ti["skip"] and ti["shaft_like"]), None)
                    if pick_ is None:
                        pick_ = next((ti for ti in inside_ if not ti["skip"] and not ti["device"]
                                      and (ti["fam"] != "Unknown" or ti["primary"])), None)
                    fallback_ = next((x_label_of.get(id(xg)) for xg in shafts_ if x_label_of.get(id(xg))
                                      and x_label_of.get(id(xg)) != X_UNKNOWN_LABEL), None)
                    return pick_, SHAFT_ROOM_FAMILY, "%.1f sq.m along a shaft's X, a door of its own: the shaft's room" % a_, \
                        {"fallback_label": fallback_ or "SHAFT ROOM", "shaft_room": True}
                # the texts that can name it: no tag, note or device; a text of
                # no family only when it reads as a room's name (a level note
                # does not); a shaft's name in a room over the shaft-room size
                # is a note about the shaft; a stair name with no flight in or
                # beside the space is no name (a staircase takes its treads)
                usable_ = [ti for ti in inside_ if not ti["skip"] and not ti["device"]
                           and (ti["fam"] != "Unknown" or ti["primary"])]
                if a_ > SHAFT_ROOM_MAX_SQM:
                    usable_ = [ti for ti in usable_ if not ti["shaft_like"]]
                stair_no_flight = [ti for ti in usable_ if ti["fam"] == "Staircase"]
                if stair_no_flight:
                    stats["spaces_stair_name_no_flight"] += 1
                    usable_ = [ti for ti in usable_ if ti["fam"] != "Staircase"]
                if not usable_:
                    devices_ = [ti for ti in inside_ if not ti["skip"] and ti["device"]] if a_ <= SHAFT_ROOM_MAX_SQM else []
                    if devices_ and devices_[0]["shaft_like"]:
                        return devices_[0], "Shaft", "a shaft tag in a %.1f sq.m space, no door or no shaft X beside it" % a_, {}
                    if s_.get("lobby"):
                        return None, "Lift Lobby", "closed at the lift banks' ends, nothing written in it", {"fallback_label": "LIFT LOBBY"}
                    if stair_no_flight:
                        return None, None, "a stair name (%s) but no flight of treads in or beside it: not a staircase; " \
                            "nothing else written in it" % stair_no_flight[0]["text"], {}
                    return None, None, "nothing written in it", {}
                pick_ = max(usable_, key=lambda ti: (ti["primary"], _fits(ti, g_), ti["fam"] != "Unknown",
                                                     label_score(ti["text"], ti["t"].get("height", 0.0), 0.0)))
                # another room's name in it as well: the trace ran through a
                # doorless opening - Phase B's leak search splits it from the labels
                if foreign_labels(s_["poly"], pick_["t"], fill_sqm=a_):
                    return pick_, None, "holds another room's name", {"contested": True}
                if pick_["shaft_like"]:
                    return pick_, "Shaft", "a shaft name in a %.1f sq.m space, %s" % (
                        a_, "no shaft X beside it" if not shafts_ else "no door of its own"), {}
                if pick_["fam"] == "Unknown":
                    return pick_, None, "named by a text of no family", {}
                return pick_, None, "named by its text (%s)" % pick_["fam"], {}

            # name each space, claim its texts, draw it; what became of every
            # space is logged (stats spaces_by_floor)
            space_log = spaces_log_out

            def _log_space(s_, fate_, label_=None, fam_=None, why_=None):
                b_ = s_["g"].bounds
                space_log.append({"fate": fate_, "label": (" ".join(str(label_).split()) if label_ else None),
                                  "family": fam_, "sqm": round(s_["g"].area / 1e6, 2), "how": s_.get("how"),
                                  "why": why_, "at": [round((b_[0] + b_[2]) / 2.0), round((b_[1] + b_[3]) / 2.0)],
                                  "texts": [ti["text"][:40] for ti in (s_.get("inside") or [])][:6]})

            def _draw_space(s_):
                g_ = s_["g"]
                pick_, fam_forced, why_, opts_ = _name_space(s_)
                if opts_.get("contested"):
                    stats["spaces_contested"] += 1
                    _log_space(s_, "left to the labels (holds two rooms' names)", pick_["text"] if pick_ else None, None, why_)
                    return
                inside_ = s_.get("inside") or []
                has_text_ = any(not ti["skip"] for ti in inside_)
                if pick_ is None and fam_forced is None and not opts_.get("fallback_label"):
                    if _spc.is_pocket(g_, floor_poly, has_text_, s_.get("has_door", False),
                                      pocket_max_sqm=SPACES_POCKET_MAX_SQM, edge_pocket_max_sqm=SPACES_EDGE_POCKET_MAX_SQM):
                        stats["spaces_pockets"] += 1
                        _log_space(s_, "pocket, not drawn", None, None, why_)
                        return
                    if not SPACES_DRAW_UNNAMED:
                        _log_space(s_, "no name, not drawn", None, None, why_)
                        return
                    label_, fam_forced = SPACES_UNNAMED_LABEL, "Unknown"
                    stats["spaces_unnamed"] += 1
                    if s_["treads"] >= STAIR_MIN_TREADS:
                        why_ = "a flight of %d treads, no UP / DOWN and no stair name in or beside it" % s_["treads"]
                elif pick_ is None:
                    label_ = opts_.get("fallback_label") or (fam_forced or "").upper()
                else:
                    label_ = pick_["text"]
                    stats["spaces_named"] += 1
                if fam_forced == "Staircase":
                    stats["spaces_staircases"] += 1
                    if opts_.get("part"):
                        stats["geometry_staircase_parts"] += 1
                elif opts_.get("shaft_room"):
                    stats["spaces_shaft_rooms"] += 1
                elif fam_forced == "Shaft":
                    stats["spaces_shaft_by_text"] += 1
                pt_ = g_.representative_point()
                d_ = {"bbox": s_["bbox"], "poly": s_["poly"],
                      "holes": [[tuple(p) for p in list(r_.coords)[:-1]] for r_ in g_.interiors if len(r_.coords) >= 4],
                      "label": label_, "height": (pick_["t"].get("height", 0.0) if pick_ else 0.0),
                      "source": "P", "cx": float(pt_.x), "cy": float(pt_.y),
                      "sr_no": (pick_["t"].get("sr_no") if pick_ else None), "absorbed_srs": [],
                      "want": (pick_.get("want") if pick_ else None), "space": True,
                      "evidence": ["traced as an enclosed space (%s)" % s_["how"], why_]}
                if fam_forced:
                    d_["family_forced"] = fam_forced
                if opts_.get("shaft_room"):
                    d_["shaft_room"] = True
                for ti in inside_:
                    t = ti["t"]
                    t["claimed"] = True
                    if t.get("sr_no") is None:
                        continue
                    if t["sr_no"] in ev_rejected:
                        # a label the labels' trace could close no room on: the space closes it
                        ev_rejected.discard(t["sr_no"])
                        stats["no_evidence_boundaries"] -= 1
                    if pick_ is not None and t is pick_["t"]:
                        text_status[t["sr_no"]] = "primary"
                    else:
                        text_status[t["sr_no"]] = "absorbed"
                        d_["absorbed_srs"].append(t["sr_no"])
                drawn.append(d_)
                s_["drawn"] = True
                stats["spaces_traced"] += 1
                _log_space(s_, "drawn", label_, fam_forced or family_for_label(label_, _FAMILY_RULES)["name"], why_)
                spaces_items_out.append((space_log[-1], d_))
            for s_ in list(spaces_):
                _draw_space(s_)
            # (4) again, with the lifts the texts named (a bank traced from its
            # labels, no X on the lifts - Aquila): the open lobby between two
            # such banks, closed at their ends
            lift_named = [g0 for g0 in (_merged_geom(d0) for d0 in drawn
                                        if d0.get("space") and not d0.get("family_forced")
                                        and family_for_label(d0.get("label") or "", _FAMILY_RULES)["name"] == "Lift")
                          if g0 is not None and not g0.is_empty]
            if lift_named:
                more_ = _bank_lobbies(x_lift_g + lift_named)
                for s_ in more_:
                    _prep(s_)
                    spaces_.append(s_)
                    _draw_space(s_)
            stats["primary_boundaries"] += stats.get("spaces_named", 0)
            spaces_ready = True
        except Exception:                             # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("rooms from spaces on %s", floor_name)
        _tick("05b spaces traced and named (text last)")
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
            # The rays reach 20 m; a space longer than that has no box
            # but can still be traced. It sorts last - the largest spaces
            # are claimed after the rooms inside them.
            if tracer is None:
                if t["sr_no"] is not None:
                    text_status[t["sr_no"]] = "no_walls"
                continue
            primaries.append((float("inf"), t))
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
            if d.get("source") in ("X", "V", "G"):
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
        names = [o for o in [t] + inside
                 if not is_ignorable_label(o["text"])
                 and not is_note_text(o["text"])
                 and not is_device_tag(o["text"])
                 and not _spaced_tag_on_tag_layer(o["text"], o.get("layer"))]
        # A label whose stated size cannot be this room does not title it:
        # ST PR-SH-01 1550x1150 standing in a 6.1 x 8.2 m staircase named
        # the stair a shaft.
        room_sqm = (shoelace_area_sqm(d["poly"]) if d.get("poly") else
                    (d["bbox"][2] - d["bbox"][0]) * (d["bbox"][3] - d["bbox"][1]) / 1e6)
        names = [o for o in names
                 if o is t or not o.get("want")
                 or _size_ok(room_sqm, o["want"], d["bbox"])]
        # Nor does a shaft name with no size title a space bigger than any
        # shaft: "PHE SH" standing in the 67 sq.m VRF yard (XA tower).
        names = [o for o in names
                 if o is t or o.get("want") or room_sqm <= SHAFT_UNSIZED_MAX_SQM
                 or not _is_shaft_name(o["text"])]
        if d.get("poly"):
            best = choose_label(names or [t], _LABEL_RULES,
                                centre=polygon_centroid(d["poly"]))
        else:
            best = t
        # A label that states this room's exact size names it: H. TOILET
        # 2200X2350 over a 2200 x 2350 room, not the JAN. beside it.
        bw, bh = d["bbox"][2] - d["bbox"][0], d["bbox"][3] - d["bbox"][1]
        sized = [o for o in names
                 if _size_match(o.get("want"), bw, bh) == "full"]
        if sized and best not in sized:
            cxy = ((d["bbox"][0] + d["bbox"][2]) / 2.0,
                   (d["bbox"][1] + d["bbox"][3]) / 2.0)
            best = min(sized, key=lambda o: math.hypot(o["x"] - cxy[0],
                                                       o["y"] - cxy[1]))
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
            box = None if ev else raycast_boundary(t["x"], t["y"])     # (a box is no evidence)
            if box is not None and guessed_box_for_unknown((box, None, []), t):
                box = None
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
                text_status[t["sr_no"]] = "no_evidence" if t["sr_no"] in ev_rejected else "no_walls"
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
        if ((rule["equipment_tags"] and (is_device_tag(t["text"])
                                         or _spaced_tag_on_tag_layer(t["text"], t.get("layer"))))
                or (rule["skip_notes"] and is_note_text(t["text"]))
                or (X_TAGS_NEVER_NAME and _x_tag_kind(t["text"]))):
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
                text_status[t["sr_no"]] = "no_evidence" if t["sr_no"] in ev_rejected else "no_walls"
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
    # (4) the lift lobby between two banks the labels traced (rule
    # rooms_from_spaces; no X on the lifts, the lifts' names traced them in
    # Phase B - Aquila): the open lobby between the banks is the floor
    # between them, closed at the banks' ends, and takes a lobby name the
    # labels could close no room on (its open side is the bank's).
    if spaces_ready and rule["lift_lobby_to_lift_bank"]:
        try:
            other_rooms = []
            lifts_all = list(x_lift_g)
            for d0 in drawn:
                if d0.get("source") not in ("P", "S") or d0.get("space"):
                    continue
                g0 = _merged_geom(d0)
                if g0 is None or g0.is_empty:
                    continue
                if not d0.get("family_forced") and family_for_label(d0.get("label") or "", _FAMILY_RULES)["name"] == "Lift":
                    lifts_all.append(g0)
                else:
                    other_rooms.append(g0)
            lifts_all += [g0 for g0 in (_merged_geom(d0) for d0 in drawn if d0.get("space") and not d0.get("family_forced")
                                        and family_for_label(d0.get("label") or "", _FAMILY_RULES)["name"] == "Lift")
                          if g0 is not None and not g0.is_empty]
            if len(lifts_all) >= 2:
                for s_ in _bank_lobbies(lifts_all, others=other_rooms, drawn_only=True):
                    _prep(s_)
                    spaces_.append(s_)
                    _draw_space(s_)
                    stats["spaces_lift_lobbies_after_labels"] += 1
        except Exception:                             # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("lift bank lobbies after the labels on %s", floor_name)
    merged = list(drawn); changed = rule["merge_overlaps"]
    while changed:
        changed = False; i = 0
        while i < len(merged):
            j = i + 1
            while j < len(merged):
                a, b = merged[i], merged[j]
                if a["source"] == "X" and b["source"] == "X":
                    j += 1; continue
                if "V" in (a["source"], b["source"]) or "G" in (a["source"], b["source"]):
                    j += 1; continue
                r = bbox_overlap_ratio(a["bbox"], b["bbox"])
                if r >= OVERLAP_MERGE_RATIO and a.get("poly") and b.get("poly"):
                    # Two traced outlines: their boxes overlap whenever the
                    # rooms are angled; what counts is the outlines (XA
                    # tower: a strip of PHE SH along a slanted facade and
                    # the VRF yard beside it, merged into one rectangle).
                    r = _poly_overlap_ratio(a["poly"], b["poly"])
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
                    if a.get("poly") and b.get("poly"):
                        pa_, pb_ = shoelace_area_sqm(a["poly"]), shoelace_area_sqm(b["poly"])
                    else:
                        pa_, pb_ = aa, ba
                    if min(aa, ba) < MERGE_MIN_SIZE * max(aa, ba) or min(pa_, pb_) < MERGE_MIN_SIZE * max(pa_, pb_):
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
                    kpoly, kholes, kxr = None, [], None
                    # An X keeps its own outline: an L of two X strips
                    # drawn as its box covered a toilet's corner (rule 9).
                    if a["source"] == "X":
                        kb, kpoly, kholes, kxr = _x_merge_shape(a, b)
                        ks = "X"
                        kl = a["label"] if a["label"] != X_UNKNOWN_LABEL else b["label"]
                        kcx, kcy = a["cx"], a["cy"]
                        winner_sr = a.get("sr_no")
                    elif b["source"] == "X":
                        kb, kpoly, kholes, kxr = _x_merge_shape(b, a)
                        ks = "X"
                        kl = b["label"] if b["label"] != X_UNKNOWN_LABEL else a["label"]
                        kcx, kcy = b["cx"], b["cy"]
                        winner_sr = b.get("sr_no")
                    else:
                        ax1, ay1, ax2, ay2 = a["bbox"]; bx1, by1, bx2, by2 = b["bbox"]
                        kb = (min(ax1, bx1), min(ay1, by1),
                              max(ax2, bx2), max(ay2, by2))
                        # Two angled outlines whose boxes overlap are not
                        # one room: their union box covers far more than
                        # both (XA tower: PHE SH 18.5 sq.m and the space
                        # beside it became a 111 sq.m rectangle).
                        def _own(z):
                            return (shoelace_area_sqm(z["poly"]) if z.get("poly") else
                                    (z["bbox"][2] - z["bbox"][0]) * (z["bbox"][3] - z["bbox"][1]) / 1e6)
                        if (_same_room_shape(a, b) is None and
                                (kb[2] - kb[0]) * (kb[3] - kb[1]) / 1e6 > MERGE_UNION_MAX_RATIO * (_own(a) + _own(b))):
                            stats["merge_skipped_union_box"] += 1
                            j += 1
                            continue
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
                        # A label stating the merged room's exact size
                        # wins it (H. TOILET 2200X2350 over JAN.).
                        kw_, kh_ = kb[2] - kb[0], kb[3] - kb[1]
                        fits = [z for z in (a, b)
                                if _size_match(z.get("want") or declared_size(z["label"]),
                                               kw_, kh_) == "full"]
                        if len(fits) == 1:
                            winner = fits[0]
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
                        elif ev:
                            # the box round two rooms is no evidence (rule
                            # boundary_on_evidence): both stay as traced
                            stats["merge_skipped_no_evidence"] += 1
                            j += 1
                            continue
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
                                 "holes": kholes, "xrects": kxr,
                                 "label": kl, "source": ks,
                                 "height": max(a.get("height", 0.0) or 0.0,
                                               b.get("height", 0.0) or 0.0),
                                 "cx": kcx, "cy": kcy,
                                 "sr_no": winner_sr,
                                 "absorbed_srs": absorbed,
                                 "merged_srs": merged_srs}
                    if ks == "X":
                        # the X mark the merged boundary is (its record, for
                        # the X sequence below)
                        xo = a if a["source"] == "X" else b
                        merged[i].update({"x_fid": xo.get("x_fid"), "x_ctag": xo.get("x_ctag")})
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

    # A room whose outline runs along the floor plate's edge, where the
    # drawing has no line of its own - only the plate outline this program
    # uses as a wall - has a side nothing closes. "2000 WD BALCONY" came
    # back as a strip 1.76 x 36 m between the facade and the plate edge,
    # where the drawing has neither a balcony nor walls. Such a room is
    # not drawn and is reported as "open side".
    plate_layer = (job["floor"].get("layer") or "").upper()
    if plate_layer and rule["open_side_guard"]:
        from shapely.geometry import LineString, Point, Polygon as _SPoly, box as _sbox
        from shapely.ops import unary_union as _uu
        try:
            plate_edge = _SPoly(floor_poly).exterior
        except (ValueError, TypeError):
            plate_edge = None
        never = [k.upper() for k in _HARD_EXCLUDE_KEYWORDS]
        kept = []
        for d in (merged if plate_edge is not None else []):
            if d.get("source") not in ("P", "S"):
                kept.append(d)
                continue
            try:
                g = (_SPoly(d["poly"]) if d.get("poly") else _sbox(*d["bbox"])).buffer(0)
                ring = g.exterior
            except (ValueError, TypeError, AttributeError):
                kept.append(d)
                continue
            n = max(12, int(ring.length / 200.0))
            pts = [ring.interpolate(i * ring.length / n) for i in range(n)]
            along = [q for q in pts if plate_edge.distance(q) <= OPEN_SIDE_NEAR_MM]
            if len(along) < OPEN_SIDE_FRACTION * n:
                kept.append(d)
                continue
            b = g.bounds
            m = OPEN_SIDE_NEAR_MM + 50.0
            drawn_lines = [LineString([(sg[0], sg[1]), (sg[2], sg[3])]) for sg in raw_segs
                           if (sg[4] or "").upper() != plate_layer
                           and not any(k in (sg[4] or "").upper() for k in never)
                           and max(sg[0], sg[2]) >= b[0] - m and min(sg[0], sg[2]) <= b[2] + m
                           and max(sg[1], sg[3]) >= b[1] - m and min(sg[1], sg[3]) <= b[3] + m
                           and (sg[0], sg[1]) != (sg[2], sg[3])]
            lines = _uu(drawn_lines) if drawn_lines else None
            open_pts = sum(1 for q in along
                           if lines is None or lines.distance(q) > OPEN_SIDE_NEAR_MM)
            if open_pts < OPEN_SIDE_FRACTION * n:
                kept.append(d)
                continue
            stats["open_side_dropped"] += 1
            for sr in [d.get("sr_no")] + list(d.get("absorbed_srs") or []):
                if sr is not None:
                    text_status[sr] = "open_side"
        if plate_edge is not None:
            merged = kept

    def family_of_drawn(d):
        if d.get("family_forced"):
            return d["family_forced"]
        if d.get("sr_no") is not None and d["sr_no"] in floor_family_overrides:
            return floor_family_overrides[d["sr_no"]]
        return family_for_label(d.get("label") or "", _FAMILY_RULES)["name"]

    def shape_of(d):
        from shapely.geometry import Polygon, box as sbox
        try:
            if d.get("poly"):
                g = Polygon(d["poly"], [h for h in (d.get("holes") or []) if len(h) >= 3])
            elif d.get("xrects") and len(d["xrects"]) > 1:
                # an X shaft without an outline: its X marks, not their box
                from shapely.ops import unary_union
                g = unary_union([sbox(*r) for r in d["xrects"]])
                if g.geom_type != "Polygon":
                    g = sbox(*d["bbox"])
            else:
                g = sbox(*d["bbox"])
            return g if g.is_valid else g.buffer(0)
        except (ValueError, TypeError):
            return None

    def set_shape(d, g, keep_point):
        """Write a shapely shape back into a drawn boundary: the part that
        holds the label (else the largest), its holes, and its box."""
        from shapely.geometry import Point
        if g is None or g.is_empty:
            return False
        parts = list(getattr(g, "geoms", [g]))
        parts = [q for q in parts if q.geom_type == "Polygon" and q.area > 0]
        if not parts:
            return False
        pt = Point(*keep_point)
        own = [q for q in parts if q.buffer(1.0).contains(pt)]
        g = own[0] if own else max(parts, key=lambda q: q.area)
        g = g.simplify(5.0, preserve_topology=True)
        d["poly"] = [(float(x), float(y)) for x, y in list(g.exterior.coords)[:-1]]
        d["holes"] = [[(float(x), float(y)) for x, y in list(r.coords)[:-1]]
                      for r in g.interiors]
        d["bbox"] = tuple(float(v) for v in g.bounds)
        return True

    # A toilet takes the WC, basin and urinal it serves. Cubicles are
    # closed rooms of their own behind their partitions and doors, so the
    # trace from the TOILET label stopped at them and the WCs - the very
    # thing that makes it a toilet - were left out. Every small closed
    # space beside a toilet that holds a sanitary fixture, and no other
    # room's name, joins it.
    fixtures = job.get("fixtures") or []
    if tracer is not None and fixtures and rule["toilet_fixtures"]:
        from shapely.geometry import Polygon, Point
        from shapely.ops import unary_union
        others = [o for o in merged if o.get("source") not in ("X",)]
        toilets = [(d, shape_of(d)) for d in merged
                   if d.get("source") in ("P", "S") and family_of_drawn(d) == "Toilet"]
        toilets = [(d, g) for d, g in toilets if g is not None and not g.is_empty]
        # Each fixture goes to the toilet nearest it, and only when it is
        # not already inside one: a WC belongs to one toilet.
        mine = collections.defaultdict(list)
        for f in fixtures:
            pt = Point(f)
            best = None
            for i, (d, g) in enumerate(toilets):
                dist = g.distance(pt)
                if dist <= 50.0:
                    best = None
                    break
                if dist <= TOILET_FIXTURE_REACH_MM and (best is None or dist < best[0]):
                    best = (dist, i)
            if best is not None:
                mine[best[1]].append(f)
        for i, (d, g) in enumerate(toilets):
            near = mine.get(i)
            if not near:
                continue
            seed = {"text": d.get("label") or "TOILET"}
            cells = []
            for fx, fy in near:
                if any(c.buffer(50.0).contains(Point(fx, fy)) for c in cells):
                    continue
                for ox, oy in ((0, 0), (250, 0), (-250, 0), (0, 250), (0, -250)):
                    tr = _trace_with(tracer, fx + ox, fy + oy)
                    if tr is None or not tr.ok or tr.area_sqm <= 0:
                        continue
                    if tr.area_sqm > TOILET_FIXTURE_CELL_MAX_SQM:
                        continue
                    cell = Polygon(tr.polygon)
                    if not cell.is_valid:
                        cell = cell.buffer(0)
                    # A WC block inserted on a wall line traces the hollow
                    # between the wall's two faces - a strip, not a
                    # cubicle. A cubicle is at least 600 mm across and
                    # holds its fixture.
                    if cell.buffer(-300.0).is_empty or cell.distance(Point(fx, fy)) > 300.0:
                        continue
                    if cell.distance(g) > WALL_T_MAX + 50.0:
                        continue
                    if holds_other_room(tr.polygon, seed):
                        continue
                    # Not a room drawn already (a janitor, a shaft).
                    c = cell.representative_point()
                    if any(o is not d and _inside_boundary(o, c.x, c.y) for o in others):
                        continue
                    cells.append(cell)
                    break
            if not cells:
                continue
            # The partitions between the toilet and its cubicles belong
            # to the toilet too: close the gap, then open back up.
            close = (WALL_T_MAX + 50.0) / 2.0
            joined = unary_union([g] + cells).buffer(close, join_style=2).buffer(-close, join_style=2)
            if set_shape(d, joined, (d["cx"], d["cy"])):
                stats["toilet_fixture_cells"] += len(cells)
                for t in floor_texts:
                    if not t["claimed"] and _inside_boundary(d, t["x"], t["y"]):
                        t["claimed"] = True
                        if t["sr_no"] is not None:
                            text_status[t["sr_no"]] = "absorbed"
                            d["absorbed_srs"].append(t["sr_no"])

    # Shafts and lift wells are not part of the rooms around them. A room
    # traced up to a shaft divided from it by a single line ran into it -
    # the FIRE TOWER 3100x4400 came out 4975 x 4600 holding the shaft
    # beside it - and a lift lobby's outline reached into the lift door
    # openings. Every shaft (X-marked or named) and lift well is cut out
    # of the rooms that overlap it.
    if rule["cut_shafts_from_rooms"]:
        # A cutter is the shaft's own piece: its shape less the wall hatches
        # and wall strips, the part holding its centre. Room area across a
        # wall from the shaft is never taken - the L of two X strips cut
        # 0.84 sq.m out of MALE TOILET's corner across a hatched wall.
        from shapely.ops import unary_union as _cun
        wall_mass = _cun(_hatch_polys(job.get("wall_hatches")) +
                         [g for g in (_safe_poly(st["pts"]) for st in walls.get("strips") or ()) if g is not None])
        cutters = []
        for o in merged:
            if o.get("source") == "V":
                continue
            fam = family_of_drawn(o)
            if o.get("source") == "X" or fam in ("Shaft", "Lift"):
                sh = shape_of(o)
                if sh is None or sh.area <= 0:
                    continue
                own = _shaft_cutter(sh, (o.get("cx", sh.centroid.x), o.get("cy", sh.centroid.y)), wall_mass)
                if own is not None:
                    cutters.append((o, own, sh))
        for d in merged:
            if d.get("source") not in ("P", "S") or family_of_drawn(d) in ("Shaft", "Lift", "Void"):
                continue
            g = shape_of(d)
            if g is None or g.area <= 0:
                continue
            hit = [own for o, own, _sh in cutters if o is not d and own.intersects(g)
                   and own.intersection(g).area > 0.02e6]
            for o, own, sh in cutters:
                if o is d or not sh.intersects(g):
                    continue
                across = g.intersection(sh).difference(own)
                if not across.is_empty and across.area > 0.02e6:
                    stats["shaft_cut_refused_across_wall"] += 1
            if not hit:
                continue
            from shapely.ops import unary_union
            cut = g.difference(unary_union(hit))
            # A shaft that would take most of the room is a mis-read X,
            # not a shaft in the room: keep the room.
            if cut.is_empty or cut.area < 0.5 * g.area:
                stats["shaft_cut_skipped"] += 1
                continue
            if set_shape(d, cut, (d["cx"], d["cy"])):
                stats["shaft_cut_from_room"] += 1

    # A shaft that reached the end still holding the placeholder title
    # takes the best real text standing inside it. The stable matching
    # in Phase A gives each cluster at most one text, so a stair whose
    # label went to a neighbouring cluster stayed "Unknown Shaft" - and
    # since that phrase matches no family keyword, three staircases
    # were drawn at the right size on UNKNOWN_BOUNDARY instead of
    # STAIR_BOUNDARY. Ranking picks the title, so this stays general.
    for d in (merged if rule["retitle_inside"] else []):
        if d.get("source") in ("V", "G"):
            continue
        current = d.get("label") or ""
        d_sqm = (shoelace_area_sqm(d["poly"]) if d.get("poly") else
                 (d["bbox"][2] - d["bbox"][0]) * (d["bbox"][3] - d["bbox"][1]) / 1e6)
        inside = [t for t in floor_texts
                  if _inside_boundary(d, t["x"], t["y"])
                  and not is_ignorable_label(t["text"])
                  and not is_note_text(t["text"])
                  and not (X_TAGS_NEVER_NAME and _x_tag_kind(t["text"]))
                  and (not t.get("want")
                       or _size_ok(d_sqm, t["want"], d["bbox"]))]
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

    # A room that took a sized label late - traced from a door tag, then
    # retitled FIRE TOWER 3100X4400 by the label standing in it - is
    # fitted to that size as one traced from the label is.
    if tracer is not None:
        pos = {t["sr_no"]: (t["x"], t["y"]) for t in floor_texts if t["sr_no"] is not None}
        for d in merged:
            if d.get("source") not in ("P", "S") or not d.get("poly"):
                continue
            want = declared_size(d.get("label") or "")
            if not want:
                continue
            lx, ly = pos.get(d.get("sr_no"), (d["cx"], d["cy"]))
            got = (d["bbox"], d["poly"], d.get("holes") or [])
            fit = _fit_stated(got, want, lx, ly)
            if fit is not got:
                d["bbox"], d["poly"], d["holes"] = fit


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
                 and not is_note_text(t["text"])
                 and not _is_plain_tag(t["text"])]
    if leftovers:
        offers = []
        for di, d in enumerate(merged):
            if d.get("source") != "X" or d.get("label") != X_UNKNOWN_LABEL:
                continue
            for ti, t in enumerate(leftovers):
                gap = _bbox_gap(t["x"], t["y"], d["bbox"])
                if gap <= X_TEXT_RADIUS:
                    ok, why = x_may_take(t, d["bbox"])
                    if not ok:
                        x_refused.setdefault(t.get("sr_no"), (d["bbox"], why))
                        continue
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


    for sr in size_flags:
        if text_status.get(sr) == "no_walls":
            text_status[sr] = "size_mismatch"

    # Rooms the text cannot name are named from what is drawn in them.
    segs_arr = None
    for d in merged:
        if d.get("source") in ("V", "G", "X"):
            continue                                  # (an X mark is the X sequence's)
        fam = None
        if d.get("family_forced"):
            fam = d["family_forced"]
        elif d.get("sr_no") is not None and d["sr_no"] in floor_family_overrides:
            fam = floor_family_overrides[d["sr_no"]]
        else:
            fam = family_for_label(d["label"], _FAMILY_RULES)["name"]
        if fam not in ("Unknown",):
            continue
        if segs_arr is None:
            segs_arr = _segs_np()
        kind = _geometry_family(d, segs_arr, job.get("fixtures"))
        if kind:
            d["family_forced"] = kind
            if d["label"] == X_UNKNOWN_LABEL or not _LABEL_RULES.family_keyword(d["label"]):
                d["label"] = kind.upper()
            stats["geometry_" + kind.lower()] += 1

    tread_lines = set()

    # Staircases no text names at all: a flight of treads with no boundary
    # of its own yet is traced from the middle of the flight. The 20th
    # floor of BHUB draws both staircases without a word in them.
    _tick("06 rooms (phase B, merges, shafts cut from rooms)")
    if rule["stairs_by_treads"] and tracer is not None:
        if segs_arr is None:
            segs_arr = _segs_np()
        deleted_shapes = [g for g in (job.get("geometry") or []) if g.get("deleted") or not g.get("include", True)]
        runs_ = _tread_runs(segs_arr, with_runs=True)
        no_tread_box = []

        def is_tread(w):
            L = math.hypot(w[2] - w[0], w[3] - w[1])
            if L < 0.6 * STAIR_TREAD_MIN_LEN:
                return False
            hz = abs(w[3] - w[1]) <= 0.03 * L
            vt = abs(w[2] - w[0]) <= 0.03 * L
            mx, my = (w[0] + w[2]) / 2.0, (w[1] + w[3]) / 2.0
            for _x, _y, _n, (rb, horiz, longest) in runs_:
                if ((horiz and hz) or (not horiz and vt)) and L <= 1.2 * longest \
                        and rb[0] - 30 <= mx <= rb[2] + 30 and rb[1] - 30 <= my <= rb[3] + 30:
                    return True
            return False
        tread_lines = {tuple(w[:4]) for w in faces + caps if is_tread(w)}

        def no_tread_tracer():
            """The walls without the flights' own tread lines: treads drawn
            on a structure layer (H10 Tower 1: on COLUMN) are walls to the
            room search, and a trace from the middle of the flight stays
            between two of them."""
            if not no_tread_box:
                keep_ = [w for w in f_walls if not is_tread(w)]
                no_tread_box.append(build_tracer(keep_) if len(keep_) < len(f_walls) else None)
            return no_tread_box[0]

        for sx, sy, n_treads, _run in runs_:
            if not _point_in_polygon(sx, sy, floor_poly):
                continue
            # a shape the user deleted or excluded in the Geometry tab stays out
            if any(_inside_boundary({"bbox": tuple(g["bbox"]), "poly": g.get("poly") or None}, sx, sy)
                   for g in deleted_shapes if g.get("bbox")):
                continue
            def _covers(d):
                # a space traced (rule rooms_from_spaces) decided its flight
                # already: a staircase with UP / DOWN or a name in it, else not
                fam = d.get("family_forced") or family_for_label(d["label"], _FAMILY_RULES)["name"]
                return _inside_boundary(d, sx, sy) and (fam == "Staircase" or d.get("source") in ("X", "V")
                                                        or d.get("space"))
            if any(_covers(d) for d in merged):
                continue
            pseudo = {"text": "STAIRCASE", "sr_no": None, "x": sx, "y": sy, "want": None}
            try:
                bbox, poly, holes = resolve_boundary(sx, sy, pseudo)
            except Exception:                         # noqa: BLE001
                continue
            small_ = not poly or (shoelace_area_sqm(poly) < STAIR_PART_MIN_SQM)
            if small_:
                # the trace stayed between two treads: again without them
                t2_ = no_tread_tracer()
                if t2_ is not None:
                    saved_ = tracer
                    for cache_ in (closed_tracer, lines_tracer_box, base_box):
                        del cache_[:]
                    leak_tracers.clear()
                    tracer = t2_
                    try:
                        got_ = resolve_boundary(sx, sy, pseudo)
                    except Exception:                 # noqa: BLE001
                        got_ = (None, None, [])
                    finally:
                        tracer = saved_
                        for cache_ in (closed_tracer, lines_tracer_box, base_box):
                            del cache_[:]
                        leak_tracers.clear()
                    if got_[0] is not None and got_[1]:
                        bbox, poly, holes = got_
                        stats["geometry_staircase_without_treads"] += 1
            if bbox is None or not poly:
                stats["geometry_staircase_rejected"] += 1
                stats["geometry_staircase_rejected_no_trace"] += 1
                continue
            area = shoelace_area_sqm(poly) - sum(shoelace_area_sqm(h) for h in (holes or ()) if len(h) >= 3)
            # the other part of a staircase traced beside it (a flight
            # behind a wall across the stair core) may be smaller, and is
            # named as that staircase
            beside = None
            try:
                from shapely.geometry import Polygon as _SP
                sp_ = _SP(poly).buffer(0)
                for d0 in merged:
                    fam0 = d0.get("family_forced") or family_for_label(d0["label"], _FAMILY_RULES)["name"]
                    if fam0 != "Staircase":
                        continue
                    g0 = shape_of(d0)
                    if g0 is not None and not g0.is_empty and g0.distance(sp_) <= STAIR_PART_REACH_MM \
                            and g0.intersection(sp_).area < 0.1 * sp_.area:
                        beside = d0
                        break
            except Exception:                         # noqa: BLE001
                beside = None
            lo_ = STAIR_PART_MIN_SQM if beside is not None else STAIR_FOUND_MIN_SQM
            if not (lo_ <= area <= STAIR_FOUND_MAX_SQM):
                stats["geometry_staircase_rejected"] += 1
                stats["geometry_staircase_rejected_size"] += 1
                continue
            if _stair_treads(segs_arr, bbox, poly) < STAIR_MIN_TREADS:
                stats["geometry_staircase_rejected"] += 1
                stats["geometry_staircase_rejected_treads"] += 1
                continue
            # the flight's own room only: it must not hold another room's name
            if holds_other_room(poly, pseudo):
                stats["geometry_staircase_rejected"] += 1
                stats["geometry_staircase_rejected_other_room"] += 1
                continue
            name_ = "STAIRCASE"
            ev_ = "found by its {0} treads, no label".format(n_treads)
            if beside is not None:
                name_ = " ".join(str(beside.get("label") or "STAIRCASE").split()) or "STAIRCASE"
                ev_ = "found by its {0} treads: the other part of {1} beside it".format(n_treads, name_)
                stats["geometry_staircase_parts"] += 1
            merged.append({"bbox": bbox, "poly": poly, "holes": holes or [],
                           "label": name_, "source": "P", "family_forced": "Staircase",
                           "cx": sx, "cy": sy, "sr_no": None, "absorbed_srs": [],
                           "evidence": [ev_]})
            for t in floor_texts:
                if not t["claimed"] and _point_in_polygon(t["x"], t["y"], poly):
                    t["claimed"] = True
                    if t["sr_no"] is not None:
                        text_status[t["sr_no"]] = "absorbed"
            stats["geometry_staircase_found"] += 1

    _tick("07 stairs by treads")
    # -- The X sequence (xplace, 2026-10-16, final) -----------------------
    # Every X mark of the floor decided ONCE, here, now its label and the
    # rooms round it are known: 2 shaft, 3 lift, 4 window, 5 cut-out,
    # 6 column, 7 void (xplace.resolve_x). Nothing after this changes an X's
    # class - not the rooms and doors settled together, the element engine,
    # the geometry engine, the final drawing, a family picked by hand.
    if x_recs and not x_decided:
        # the text-first pass: the rooms are known, an X stands "in a room"
        # when a traced room holds it
        from . import elementid as _eid_x
        from shapely.geometry import Polygon as _XRP
        rooms_x = [(d_, shape_of(d_)) for d_ in merged if d_.get("source") in ("P", "S")]
        # a room with its holes filled: an X it was traced round (a hole in
        # it) still stands in it
        rooms_x = [(d_, _XRP(g_.exterior)) for d_, g_ in rooms_x
                   if g_ is not None and not g_.is_empty and g_.geom_type == "Polygon"]
        stairs_x = [(d_, g_) for d_, g_ in rooms_x if family_of_drawn(d_) == "Staircase"]

        def _room_of(g_):
            # the room an X stands in: inside its outline (holes filled) -
            # (room, False); or in a notch of it, the room traced round the
            # X and what is drawn about it (a sleeve's circle): within
            # X_IN_ROOM_REACH_MM of the room, inside its convex hull and no
            # other room's - (room, True); else None
            pt_ = g_.representative_point()
            for r_, rg_ in rooms_x:
                if rg_.buffer(1.0).contains(pt_):
                    return r_, False
            for r_, rg_ in rooms_x:
                if rg_.distance(g_) <= X_IN_ROOM_REACH_MM and rg_.convex_hull.contains(pt_):
                    if not any(o_ is not r_ and og_.convex_hull.contains(pt_) and og_.distance(g_) <= X_IN_ROOM_REACH_MM
                               for o_, og_ in rooms_x):
                        return r_, True
            return None

        def _stair_well_of(g_):
            return any(whole_.intersection(g_).area >= 0.9 * g_.area and
                       _eid_x.between_flights(g_, whole_, raw_segs) for st_, whole_ in stairs_x)

        run_x_sequence(merged, _room_of, _stair_well_of, shape_of)
        x_decided = True
    stats["windows"] += len(windows)
    _tick("08 x decision (resolve_x)")

    # Rule 6 / 8: an outline turns only where something stops it. Steps
    # and spikes narrower than the 100 mm gap tolerance - a fill nosing
    # into a door jamb, a notch where two wall lines miss each other - are
    # straightened out: the outline is opened, then closed, by half that.
    if rule["straighten_outlines"]:
        r = STRAIGHTEN_MM / 2.0
        for d in merged:
            if d.get("source") not in ("P", "S") or not d.get("poly"):
                continue
            g = shape_of(d)
            if g is None or g.is_empty:
                continue
            try:
                g2 = (g.buffer(-r, join_style=2).buffer(r, join_style=2)
                      .buffer(r, join_style=2).buffer(-r, join_style=2))
            except Exception:                         # noqa: BLE001
                continue
            if g2.is_empty or abs(g2.area - g.area) > 0.1 * g.area:
                continue
            if set_shape(d, g2, (d["cx"], d["cy"])):
                stats["outlines_straightened"] += 1

    # Rule 9: no two enclosed areas overlap; they may touch. Shafts, lift
    # wells, voids and shapes chosen in the Geometry tab keep their
    # outline; every room gives way to them and to the smaller rooms
    # already kept - the tightest enclosure is the truest. A room left
    # with almost nothing of its own was the same space twice.
    if rule["no_overlaps"]:
        from shapely.ops import unary_union as _union
        fixed = [d for d in merged if d.get("source") in ("X", "V", "G")]
        rooms = [d for d in merged if d.get("source") in ("P", "S")]
        kept_shapes = [g for g in (shape_of(d) for d in fixed) if g is not None and not g.is_empty]
        rooms.sort(key=lambda d: (shape_of(d).area if shape_of(d) is not None else 0.0))
        dropped = set()
        for d in rooms:
            g = shape_of(d)
            if g is None or g.is_empty:
                continue
            hits = [k for k in kept_shapes if k.intersects(g) and k.intersection(g).area > 1e4]
            if hits:
                left = g.difference(_union(hits))
                if left.is_empty or left.area < OVERLAP_KEEP_SHARE * g.area:
                    dropped.add(id(d))
                    stats["overlapping_room_dropped"] += 1
                    for sr in [d.get("sr_no")] + list(d.get("absorbed_srs") or []):
                        if sr is not None and text_status.get(sr) in (
                                "primary", "secondary", "absorbed", "merged"):
                            text_status[sr] = "absorbed"
                    continue
                if set_shape(d, left, (d["cx"], d["cy"])):
                    stats["overlap_trimmed"] += 1
                    g = shape_of(d)
            kept_shapes.append(g)
        if dropped:
            merged = [d for d in merged if id(d) not in dropped]

    # The hard guardrail: no boundary crosses a wall, a wall or structure
    # hatch, or the facade (glazing lines are walls here). Whatever crosses
    # one - a box the rays guessed, a fitted rectangle, a merged pair, a
    # toilet joined to its cubicles - is cut there and keeps the piece
    # holding its label. Voids follow their own drawn outline.
    if rule["never_cross_walls"]:
        guard_lines = [tuple(w[:4]) for w in (faces + caps)]
        if tread_lines:
            # a flight's treads are not walls, whatever layer draws them
            guard_lines = [w for w in guard_lines if w not in tread_lines]
        for d in merged:
            if d.get("source") == "V":
                continue
            # An X-marked opening is the X: drawn corner to corner of the
            # cut in the slab, it is not cut by a line crossing it (285 FD:
            # a slab-edge detail line read as a wall face trimmed 125 mm off
            # the STP, DG, condenser and stair-press shafts).
            if d.get("source") == "X" and rule["x_is_the_opening"]:
                continue
            lx, ly = d.get("cx"), d.get("cy")
            if not d.get("poly") and d.get("source") in ("P", "S"):
                d["box_only"] = True                  # a four-ray box, whatever the cut makes of it
            np_, nh, nb, changed = guard_against_walls(
                d.get("poly"), d.get("holes"), d["bbox"], (lx, ly) if lx is not None else None,
                guard_lines, job.get("wall_hatches"))
            if changed:
                d["poly"], d["holes"], d["bbox"] = np_, nh, nb
                stats["guardrail_cut"] += 1

    # A shaft whose X marks only strips of it: its label traced the room
    # (the X refused it), and the unnamed X shafts at the room's edge are
    # parts of the same shaft - joined to it when that brings it to the
    # size its label states (PHE-SHAFT 2300X1270: a 1.94 sq.m room and
    # its 0.69 + 0.29 strips, 2.92 together). A room's name (ELEC. ROOM)
    # never takes its X: that stays a shaft of its own.
    if x_refused and rule["x_grows_to_cell"]:
        joined = _join_shaft_strips(merged, floor_family_overrides, stats)
        for sr in joined:
            if sr in x_refused:
                xb, why = x_refused[sr]
                x_refused[sr] = (xb, why + "; X joined to the shaft")

    # A room traced smaller than its stated size grows to it, over a strip
    # along one side up to the wall face beyond (ELE. ROOM 2500x2300 came
    # out 2300 wide: a 200 mm strip to the wall at its side was lost).
    # After the guardrail, which would cut it back at the face it grows
    # over; the strip is checked not to be a wall hatch or another room.
    grown = []
    if tracer is not None and rule["fit_stated_size"]:
        grown = _grow_to_stated(merged, f_walls, job.get("wall_hatches"), stats,
                                grow_x=rule["x_grows_to_cell"])

    # Slits and spikes: a line ending inside a room leaves a 0-2 mm slit
    # in its outline; closed and opened away (clean_outline).
    if rule["clean_outlines"]:
        for d in merged:
            if d.get("source") == "V" or not d.get("poly"):
                continue
            np_, nh, changed = clean_outline(d["poly"], d.get("holes"))
            if changed:
                xs = [p[0] for p in np_]
                ys = [p[1] for p in np_]
                d["poly"], d["holes"], d["bbox"] = np_, nh, (min(xs), min(ys), max(xs), max(ys))
                stats["outlines_cleaned"] += 1

    # A lift well is a rectangle. One whose outline spans exactly the size
    # its label states (PASSENGER LIFT 2.6M X 2.9M over a 2600 x 2900 box)
    # but came out notched - door jambs and car guides cut out, 6.0 of 7.54
    # sq.m on the XA tower - is that rectangle, unless another room stands
    # in the notches.
    try:
        from shapely.geometry import Polygon as _LP, box as _lbox
        _others = [(_d, _LP(_d["poly"]).buffer(0)) for _d in merged if _d.get("poly")]
        for d in merged:
            if not d.get("poly") or not d.get("label"):
                continue
            want = declared_size(d["label"])
            if not want or family_for_label(d["label"], _FAMILY_RULES)["name"] != "Lift":
                continue
            b = d["bbox"]
            if _exact_size_err(want, b[2] - b[0], b[3] - b[1]) is None:
                continue
            g = _LP(d["poly"]).buffer(0)
            rect = _lbox(*b)
            if g.area >= 0.97 * rect.area:
                continue
            if any(o is not d and og.intersection(rect).area > 0.05e6 for o, og in _others):
                continue
            d["poly"] = [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]
            d["holes"] = []
            stats["lift_well_squared"] += 1
    except Exception:                                 # noqa: BLE001
        pass

    # Two X-marked strips of one length face to face with no wall between
    # them are the slab openings at either side of one walled shaft: one
    # shaft, the strips and the band between (rules: join_shaft_strips).
    if rule["join_shaft_strips"]:
        try:
            stats["shaft_strips_joined"] += _join_x_strip_pairs(merged, faces + caps, shape_of, set_shape)
            merged = [d for d in merged if not d.get("_joined_away")]
        except Exception:                             # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("shaft strips on %s", floor_name)

    # A lift's landing doors slide: a space labelled as a lift with two or
    # more hinged doors opening into it is a lobby the label was left in
    # (H10 Tower 1 ground floor: LIFT WELL 3100X2300 standing in the
    # staircase's lobby). Its label is refused; the space stays common floor.
    lift_refused = {}
    if rule["lift_not_with_doors"] and early_doors:
        try:
            from . import elements as _el
            lifts = [d for d in merged if d.get("source") in ("P", "S") and family_of_drawn(d) == "Lift"]
            if lifts:
                idx_, geoms_ = [], []
                for d in merged:
                    g = shape_of(d)
                    if g is not None and not g.is_empty and g.area > 0:
                        idx_.append(d)
                        geoms_.append(g)
                n_doors = collections.Counter()
                from shapely.strtree import STRtree as _LT
                tree_ = _LT(geoms_)
                for q in early_doors:
                    ax_ = _el.door_axes(q)
                    if ax_ is None or ax_[7] <= _el.DOOR_CUBICLE_BOARD_MAX_MM:
                        continue
                    for k in set(_el.door_sides(q, geoms_, tree_)) - {None}:
                        n_doors[id(idx_[k])] += 1
                for d in lifts:
                    if n_doors[id(d)] >= LIFT_REFUSE_DOORS:
                        merged = [m for m in merged if m is not d]
                        stats["lift_label_refused_doors"] += 1
                        for sr in [d.get("sr_no")] + list(d.get("absorbed_srs") or []):
                            if sr is not None:
                                text_status[sr] = "not_lift"
                                lift_refused[sr] = n_doors[id(d)]
        except Exception:                             # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("lift labels on %s", floor_name)

    # A cut-out standing inside a traced room is a hole in that room (on
    # AI_VOID), never part of its area.
    if cutouts:
        from shapely.geometry import Polygon as _CP
        for c in cutouts:
            cg = _CP(c["poly"]).buffer(0)
            if cg.is_empty:
                continue
            for d in merged:
                if d.get("source") not in ("P", "S"):
                    continue
                g = shape_of(d)
                if g is None or not g.intersects(cg) or g.intersection(cg).area < 0.9 * cg.area:
                    continue
                if set_shape(d, g.difference(cg), (d["cx"], d["cy"])):
                    stats["cutout_holes"] += 1
                break

    _tick("09 outlines settled (straighten, overlaps, guardrail, stated size)")
    # A shaft is the compartment its X stands in, traced from the X like a
    # room (rule shaft_compartment): see shaft_compartments below. Under
    # rule rooms_from_spaces the X marks were traced before the rooms (the
    # stage above); an X seen there is skipped here.
    if rule["shaft_compartment"] and tracer is not None:
        try:
            shaft_compartments(merged, _shaft_tracer_on(merged), raw_segs, floor_poly, stats, shaft_review,
                               shaft_notes, floor_family_overrides, family_rules=_FAMILY_RULES)
        except Exception:                             # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("shaft compartments on %s", floor_name)

    _tick("10 shaft compartments")
    # The walls as drawn in the output (AI_WALL): the strips between the
    # face pairs found above and the wall hatches, with columns and door
    # openings cut out. A window or a cut-out in a wall stays part of the
    # wall (OPENINGS_IN_WALLS) - drawn on its own layer over it.
    walls_out = {}
    door_quads, door_out, glass_out = [], [], []
    door_review = []
    try:
        door_quads = pair_quads(door_closing) if door_closing else []
    except Exception:                                 # noqa: BLE001
        door_quads = []
    # (a cut-out the X sequence found standing loose - a cut-out tag beside
    # it, no wall round it - is no part of a wall)
    openings = [w["poly"] for w in windows + cutouts if w.get("in_wall", True)]
    # Room-size veto: a strip a room grew over to reach the size its label
    # states (_grow_to_stated) is inside that room, not a wall.
    room_veto = [d["_grown_strip"] for d in merged if d.get("_grown_strip")]
    for vs in room_veto:
        wall_exclusions.append({"floor": floor_name, "rule": "room-size veto",
                                "line": [round(v) for v in (vs[0][0], vs[0][1], vs[2][0], vs[2][1])]})
    wt, wt_pct, wt_review = None, None, []
    if rule["draw_walls"] and rule["wall_trace"]:
        # What is wall: faces paired nearest-first and confirmed by the
        # hatch (walltrace). Room tracing above keeps its own walls.
        if early_wt is not None:
            wt = early_wt                             # traced above, for the doors
        else:
            try:
                from . import walltrace as _wt
                wt_counts = {}
                wt = _wt.trace_floor(job.get("segs"), job.get("all_hatches"), job.get("rows"), floor_poly,
                                     arcs=job.get("wall_arcs"),
                                     skip=((lambda sg, _k=wall_skip: _seg_key(sg) in _k) if ep else None),
                                     locked=(LM.union(LOCKED_NEVER_WALL + ("column",)) if ep else None),
                                     counts=wt_counts)
                for k_, v_ in wt_counts.items():
                    stats[k_] += v_
            except Exception:                         # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception("wall trace for %s", floor_name)
                wt = None
        if wt is not None:
            stats["wall_trace_floors"] += 1
            stats["wall_trace_footprint_sqm"] += wt["footprint_sqm"]
            stats["wall_trace_run_m"] += wt["run_m"]
            for k_, v_ in (wt.get("verdicts") or {}).items():
                stats["wall_trace_strips_" + k_] += v_
            stats["wall_trace_shafts"] += len(wt["shafts"])
            stats["wall_trace_columns"] += len(wt["columns"])
            stats["wall_trace_review_blocks"] += len(wt["review_blocks"])
            wt_pct = wt.get("hatched_traced_pct")
            wt_review = [{"floor": floor_name, "kind": "strip", "pts": [list(p) for p in q]} for q in wt["review_strips"]]
            wt_review += [{"floor": floor_name, "kind": "block",
                           "pts": [list(p) for p in list(g.exterior.coords)[:-1]]} for g in wt["review_blocks"]]
    if rule["draw_walls"]:
        # Door openings (AI_DOOR): from the door swings, at the gaps in the
        # walls traced above (door_openings); they are cut out of the walls
        # (a wall hatch drawn on across a door is not wall there). The room
        # tracer's door closures are not: paired a wall's thickness apart they
        # also ran along both faces of a wall, and cut it away (P-14 6th
        # floor: 133 such quads, 33 sq.m, a 1.1 m shaft wall among them).
        if DRAW_DOORS:
            try:
                if early_doors is not None and (early_wt is not None) == (wt is not None):
                    door_found, door_review = early_doors, early_review
                else:
                    door_review = []
                    door_found = door_openings(job.get("door_arcs") or [], job.get("segs") or [],
                                               (wt["strips"] if wt is not None else walls.get("strips")) or [],
                                               evidence=rule["doors_need_evidence"], rejected=door_review)
                door_out = [[[float(x), float(y)] for x, y in q] for q in door_found]
                if os.environ.get("SHAFT_DEBUG"):
                    print("  doors:", len(door_out), "early" if door_found is early_doors else "re-run",
                          "wt" if wt is not None else "walls", "early_wt" if early_wt is not None else "-",
                          "n_strips", len(((wt["strips"] if wt is not None else walls.get("strips")) or [])),
                          "review", len(door_review), "segs", len(job.get("segs") or []), "arcs", len(job.get("door_arcs") or []),
                          "hatches", len(job.get("all_hatches") or []), "rows", len(job.get("rows") or []),
                          "swings", len(job.get("all_swings") or []), "plate", [round(v) for v in __import__("shapely.geometry", fromlist=["Polygon"]).Polygon(floor_poly).bounds] if floor_poly else None)
                stats["doors_without_evidence"] += len(door_review)
                stats["doors_marked_for_review"] += sum(1 for r_ in door_review if r_.get("review"))
            except Exception:                         # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception("door openings on %s", floor_name)
                door_out = []
        # Windows: a gap in a wall with glazing or a louvre across it
        # (window_openings), beside the X-marked ones found above - part of
        # the wall (OPENINGS_IN_WALLS), drawn on AI_WINDOW.
        if WINDOW_GAPS and job.get("window_evidence"):
            try:
                from shapely.geometry import Polygon as _WP
                have = [_WP(w["poly"]).buffer(0) for w in windows + cutouts if w.get("poly")]
                for q in window_openings(job.get("window_evidence"), job.get("segs") or [],
                                         (wt["strips"] if wt is not None else walls.get("strips")) or [],
                                         avoid=door_out):
                    g = _WP(q).buffer(0)
                    if g.is_empty or any(g.intersection(h).area > 0.3 * min(g.area, h.area) for h in have):
                        continue
                    xs, ys = [p[0] for p in q], [p[1] for p in q]
                    windows.append({"poly": [tuple(p) for p in q], "why": "glazing or a louvre across a gap in a wall",
                                    "cx": sum(xs) / len(xs), "cy": sum(ys) / len(ys), "gap": True})
                    openings.append(windows[-1]["poly"])
                    stats["windows_in_wall_gaps"] += 1
            except Exception:                         # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception("window openings on %s", floor_name)
        # Glass partitions and glazing (elements.glass_elements): not walls,
        # not rooms - drawn on AI_GLASS; what lies in a window or a door is
        # that window or door.
        if job.get("glass_segs"):
            try:
                from . import elements as _el
                gl = _el.glass_elements(job["glass_segs"], floor_poly,
                                        cut=[w["poly"] for w in windows] + list(door_out))
                glass_out = [[(float(x), float(y)) for x, y in list(g.exterior.coords)[:-1]] for g in gl]
                rep_ = _el.glass_report(gl)
                stats["glass_count"] += rep_["count"]
                stats["glass_length_m"] += rep_["length_m"]
            except Exception:                         # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception("glass on %s", floor_name)
        try:
            wcounts = {}
            strips_drawn = (wt["strips"] if wt is not None else walls.get("strips")) or []
            if rule["label_strip_not_wall"] and strips_drawn:
                # a strip holding a room's or a shaft's name is that space,
                # not wall, unless the wall runs on past it (rule 2-1)
                from . import xplace as _xp
                name_pts = [(float(r["x"]), float(r["y"])) for r in rows
                            if not is_ignorable_label(str(r.get("text") or "")) and not is_note_text(str(r.get("text") or ""))
                            and not _x_tag_kind(str(r.get("text") or ""))
                            and (looks_like_primary(str(r.get("text") or ""))
                                 or shaft_like_label(str(r.get("text") or ""),
                                                     family_for_label(str(r.get("text") or ""), _FAMILY_RULES)["name"]))]
                strips_drawn, n_gone = _xp.drop_label_strips(strips_drawn, name_pts)
                stats["wall_strips_holding_a_name"] += n_gone
            # the element pipeline: the walls are built once more here, on
            # what is not decided yet - never over an X the sequence made a
            # shaft, a lift, a void or a loose cut-out (LockedMask)
            locked_w = LM.union(LOCKED_NEVER_WALL) if ep else None
            pieces, wrep = wall_polygons(
                strips_drawn, floor_poly,
                ([] if wt is not None else job.get("wall_hatches")),
                job.get("columns") or [], ([] if OPENINGS_IN_WALLS else openings),
                door_out,
                list(job.get("column_rejects") or []) + (openings if OPENINGS_IN_WALLS else []),
                bridge=(openings if OPENINGS_IN_WALLS else []),
                counts=wcounts, veto=room_veto, locked=locked_w)
            for k, v in wcounts.items():
                stats["wall_strips_" + k] += v
            if rule["x_box_not_wall"]:
                # An X beside a wall - a shaft, a lift, a machine room's well -
                # is never part of the wall (rule 2-2); a window or a cut-out
                # in the wall still is.
                xs_ = [shape_of(d) for d in merged if d.get("source") == "X"]
                xs_ = [g for g in xs_ if g is not None and not g.is_empty]
                if xs_:
                    from shapely.ops import unary_union as _xu
                    XU = _xu(xs_).buffer(0)
                    cut_ = 0.0
                    for cls_ in list(pieces):
                        kept_ = []
                        for pc in pieces[cls_]:
                            if pc.intersects(XU) and pc.intersection(XU).area > 0.005e6:
                                rest = pc.difference(XU)
                                cut_ += pc.area - rest.area
                                kept_ += [q for q in getattr(rest, "geoms", [rest])
                                          if q.geom_type == "Polygon" and q.area >= 0.01e6]
                            else:
                                kept_.append(pc)
                        pieces[cls_] = kept_
                    if cut_ > 0:
                        wrep = {cls_: wall_report(p_) for cls_, p_ in pieces.items()}
                        stats["walls_x_boxes_cut_sqm"] += round(cut_ / 1e6, 3)
                        for g in xs_:
                            for q in getattr(g, "geoms", [g]):
                                if q.geom_type == "Polygon":
                                    room_veto.append([tuple(map(float, c)) for c in list(q.exterior.coords)[:-1]])
            walls_out = {cls: wall_rings(merge_wall_pieces(p)) for cls, p in pieces.items()}
            # Rooms grow over the walls the room tracing saw (a parapet of
            # glass and kerb lines is still the outer face of a balcony),
            # not over the measured walls drawn on AI_WALL_*.
            room_pieces = pieces
            # (only a balcony or refuge area grows over them: with none on
            # the floor they are not worked out - 9 s on 285 FD)
            if (wt is not None or OPENINGS_IN_WALLS) and rule["balcony_outer_face"] and \
                    _balcony_to_grow(merged, family_for_label):
                room_pieces, _r = wall_polygons(
                    walls.get("strips") or [], floor_poly, job.get("wall_hatches"),
                    job.get("columns") or [], openings,
                    door_quads, job.get("column_rejects") or [], veto=room_veto, locked=locked_w)
            # A balcony or refuge area is measured to the outer face of its
            # parapet: the wall pieces along its open side, which no other
            # room shares, join it (the take-off polylines on BHUB and 285
            # run to that face; we stopped 235 mm short along 36 m).
            if rule["balcony_outer_face"]:
                try:
                    stats["balcony_grown"] += _grow_balconies(
                        merged, room_pieces, job.get("columns") or [], floor_poly,
                        shape_of, set_shape, family_for_label)
                except Exception:                     # noqa: BLE001
                    import logging
                    logging.getLogger(__name__).exception("balcony outer face on %s", floor_name)
            # The parapet a balcony grew over is part of the balcony, not a
            # wall: it leaves the walls drawn here, and the final drawing's
            # walls (wall_veto) - BHUB 1st Floor drew 7.2 sq.m of it twice.
            if BALCONY_PARAPET_NOT_WALL:
                grown_b = [shape_of(d) for d in merged if d.get("_balcony_grown_sqm")]
                grown_b = [g for g in grown_b if g is not None and not g.is_empty]
                if grown_b:
                    from shapely.ops import unary_union as _bu
                    G = _bu(grown_b).buffer(1.0, join_style=2)
                    kept = []
                    for pc in pieces.get(LN.WALL) or []:
                        if pc.intersects(G):
                            rest = pc.difference(G)
                            kept += [q for q in getattr(rest, "geoms", [rest])
                                     if q.geom_type == "Polygon" and q.area >= 0.01e6]
                        else:
                            kept.append(pc)
                    pieces = {LN.WALL: kept}
                    wrep = {LN.WALL: wall_report(kept)}
                    walls_out = {LN.WALL: wall_rings(merge_wall_pieces(kept))}
                    for g in grown_b:
                        for q in getattr(g, "geoms", [g]):
                            if q.geom_type == "Polygon":
                                room_veto.append([tuple(map(float, c)) for c in list(q.exterior.coords)[:-1]])
                    stats["walls_balcony_parapet_vetoed"] += len(grown_b)
            if rule["columns_in_rooms"]:
                try:
                    stats["columns_in_rooms"] += _absorb_columns(
                        merged, job.get("columns") or [], shape_of, set_shape)
                except Exception:                     # noqa: BLE001
                    import logging
                    logging.getLogger(__name__).exception("columns in rooms on %s", floor_name)
            for v in wrep.values():
                stats["walls_count"] += v["count"]
                stats["walls_length_m"] += v["length_m"]
                stats["walls_area_sqm"] += v["area_sqm"]
        except Exception:                             # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("walls for %s", floor_name)

    _tick("11 walls rebuilt on the rooms (W3; doors, windows, glass, balcony, columns)")
    # A shaft's room runs to its door: an X-marked cut-out at the back of a
    # walled shaft cell (P285: TEN, COMM, FAPA, the TEN shafts below the lift
    # lobby) is the shaft's slab opening; the space in front of it, up to
    # the cell's door, is the shaft's room too - drawn as a part of its own
    # with the shaft's name, so the area statement still deducts only the
    # cut-out (a walled shaft room beside its opening is listed, not
    # deducted - area_statement.classify). An unnamed cut-out in a named
    # shaft's cell takes that name.
    if rule["shaft_room_to_door"] and tracer is not None and DRAW_DOORS:
        try:
            try:
                _cell_walls = list(pieces.get(LN.WALL) or [])
            except NameError:
                _cell_walls = []
            stats["shaft_cells_to_door"] += _shaft_cells_to_door(
                merged, floor_texts, door_out or early_doors or [], shape_of, family_of_drawn,
                lambda x, y: _trace_with(tracer, x, y), gaps_tracer, _trace_with, _cell_walls)
        except Exception:                             # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("shaft cells on %s", floor_name)

    # Every door is primary - between two spaces: it stops a room - or
    # secondary - inside one room, a WC cubicle's (elements.door_levels);
    # what it opens into (a room, a shaft, a WC cubicle: door_kinds) is kept
    # with it. All of them were cut out of the walls above.
    door_kinds_out, door_levels_out, door_sides_out = [], [], []
    rooms_k, shaft_k, main_k, names_k, lift_k, solid_parts = [], [], [], [], [], []
    liftfam_k = []                        # in the Lift family (rule lift_without_door_mechanical)
    if DRAW_DOORS and rule["draw_walls"]:
        try:
            from . import elements as _el
            from shapely.geometry import Polygon as _DP
            from shapely.ops import unary_union as _du
            for d in merged:
                fam = family_of_drawn(d)
                if fam == _IGNORE_FAMILY["name"]:
                    continue
                g = shape_of(d)
                if g is None or g.is_empty or g.area <= 0:
                    continue
                rooms_k.append(g)
                shaft_k.append(_el.is_shaft_room(d.get("label") or "", fam))
                main_k.append(_el.is_main_room(d.get("label") or "", fam))
                lift_k.append(_el.is_lift_room(d.get("label") or "", fam))
                liftfam_k.append(fam == "Lift")
                names_k.append(" ".join(str(d.get("label") or "").split()))
            try:
                solid_parts += list(pieces.get(LN.WALL) or [])
            except NameError:
                pass
            # A lift's entrance: a gap in its own wall with no swing - its
            # doors slide (elements.lift_door_quads).
            # Every lift of the Lift family is looked at, a well passing
            # through too (rule lift_without_door_mechanical): a gap in its
            # walls is its door; one with none is no lift stopping here.
            gap_k = [lf or (rule["lift_without_door_mechanical"] and lfam)
                     for lf, lfam in zip(lift_k, liftfam_k)]
            if _el.LIFT_DOORS and any(gap_k) and solid_parts:
                wall_u = _du(solid_parts)
                lift_g = [g for g, lf in zip(rooms_k, gap_k) if lf]
                other_g = [g for g, sh_, lf in zip(rooms_k, shaft_k, gap_k) if sh_ or lf]   # the wells
                for q, _ev in _el.lift_door_quads(lift_g, wall_u, other_g,
                                                  [_DP(q_).buffer(0) for q_ in door_out or ()],
                                                  job.get("segs") or (),
                                                  open_front=rule["lift_without_door_mechanical"]):
                    door_out = list(door_out or []) + [[list(p) for p in q]]
                    stats["doors_lift_entrances"] += 1
                    if _ev.get("open_front"):
                        stats["doors_lift_open_fronts"] += 1
        except Exception:                             # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("lift doors on %s", floor_name)
    if door_out and DRAW_DOORS:
        try:
            from . import elements as _el
            from shapely.geometry import Polygon as _DP
            from shapely.ops import unary_union as _du
            labels_k = [(str(t.get("text") or ""), float(t.get("x") or 0), float(t.get("y") or 0))
                        for t in job.get("rows") or ()]
            kinds = (_el.door_kinds(door_out, rooms_k, shaft_k, labels_k, lift=lift_k) if _el.DOOR_KINDS
                     else [(_el.EXIT, None, None)] * len(door_out))
            for ring in list(job.get("columns") or []) + list(glass_out or []) + [w["poly"] for w in windows + cutouts]:
                try:
                    gg = _DP([tuple(p[:2]) for p in ring]).buffer(0)
                    if not gg.is_empty:
                        solid_parts.append(gg)
                except (ValueError, TypeError, IndexError):
                    continue
            opens = _el.open_floor(floor_poly, rooms_k, _du(solid_parts) if solid_parts else None, door_out)
            levels = _el.door_levels(door_out, rooms_k, main_k, shaft_k, kinds, opens)
            nr = len(rooms_k)
            nm = lambda k: "" if k is None else (names_k[k] if k < nr else "(common floor)")
            for (kind, i_, j_), (lev, a_, b_, within) in zip(kinds, levels):
                door_kinds_out.append(kind)
                door_levels_out.append(lev)
                door_sides_out.append((nm(a_), nm(b_)))
                stats["doors_" + kind] += 1
                stats["doors_" + lev] += 1
                if within:
                    stats["doors_primary_within_main"] += 1
                if kind == _el.EXIT and i_ is not None and i_ == j_:
                    stats["doors_exit_same_room"] += 1   # the room was traced through its own door
            stats["door_open_floor_pieces"] += len(opens)
        except Exception:                             # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("door kinds on %s", floor_name)
            door_kinds_out, door_levels_out, door_sides_out = [], [], []

    _tick("12 shaft rooms, door kinds")
    # A label an X was refused: what became of it, and of the X.
    label_notes = dict(shaft_notes)
    for sr, n in lift_refused.items():
        label_notes[sr] = "not a lift well: {0} hinged doors open into it (a lift's doors slide)".format(n)
    if x_refused:
        from shapely.geometry import Polygon as _NP, Point as _NPt, box as _nbox
        own = {d.get("sr_no"): d for d in merged if d.get("sr_no") is not None and d.get("source") in ("P", "S")}
        for sr, (xb, why) in x_refused.items():
            if sr is None or text_status.get(sr) in ("x", "not_lift"):
                continue                                  # another X took it, or not a lift at all
            d = own.get(sr)
            if d is None or not d.get("poly"):
                label_notes[sr] = "not given to the X beside it ({0})".format(why)
                continue
            try:
                room = _NP(d["poly"], [h for h in d.get("holes") or () if len(h) >= 3]).buffer(0)
                xc = _NPt((xb[0] + xb[2]) / 2.0, (xb[1] + xb[3]) / 2.0)
                hull = _NP(d["poly"]).buffer(0)
                where = ("inside" if hull.buffer(-5).contains(xc) else
                         "at its edge" if hull.buffer(50).intersects(_nbox(*xb)) else "beside it")
            except Exception:                         # noqa: BLE001
                where = "beside it"
            label_notes[sr] = "room traced; X {0} kept as shaft ({1})".format(where, why)

    # what the later passes made of the spaces (merges, overlaps, the rooms
    # in the wall mass, the families set from what is drawn in them)
    alive_ = {id(m_) for m_ in merged}
    for e_, d_ in spaces_items_out:
        if id(d_) not in alive_:
            e_["fate"] = "drawn, then dropped by a later pass (inside the wall mass, or another boundary took its place)"
        elif d_.get("family_forced") and e_.get("family") != d_.get("family_forced"):
            e_["fate"] = "drawn (family set later from what is drawn in it: %s)" % d_["family_forced"]
            e_["family"] = d_["family_forced"]

    return {"merged": merged, "text_status": text_status,
            "label_notes": label_notes,
            "wall_exclusions": wall_exclusions, "grown": grown,
            "wall_trace_pct": wt_pct, "wall_trace_review": wt_review[:200],
            "wall_veto": [[list(p) for p in v] for v in room_veto],
            "x_placement": [_x_log_row(r_) for r_ in x_log],
            "x_columns": x_columns,
            "size_flags": size_flags,
            "floor_family_overrides": floor_family_overrides,
            "stats": dict(stats),
            "windows": windows, "cutouts": cutouts, "window_conflicts": window_conflicts,
            "walls": walls_out,
            "doors": door_out,
            "door_review": [r_ for r_ in door_review if r_.get("review")],
            "shaft_review": shaft_review,
            "door_kinds": door_kinds_out,
            "door_levels": door_levels_out,
            "door_sides": door_sides_out,
            "lobby_bank_lines": [list(map(float, l_)) for l_ in lobby_bank_lines],
            "glass": glass_out,
            "timings": timings,
            "locked": LM.report(),
            "spaces": spaces_log_out,
            "traced": tracer is not None}



# -- What a floor's rooms and doors settle together (Test_2) ----------------
# Once the floor pass has found the rooms and the doors, these are settled
# with both (each a rule in rules.py, but the first):
#   (the stair's open well)    an X the X sequence made a void, between two
#                              flights of the staircase it stands in: cut out
#                              of the staircase as a hole (AI_VOID);
#   shaft_room_mep             a room sharing a side with an X-marked shaft
#                              and with a door of its own is the shaft's room,
#                              not a shaft: AI_MEP, whatever its name;
#   lift_no_door_machine_room  a lift traced from its label (not an X lift)
#                              with no door, its front on a lift machine room,
#                              is part of it: AI_MACHINEROOM;
#   wall_gaps_filled           (wall_gap_pieces) the wall left between them.
# config.json "rooms_and_doors".
_RD = CONFIG.get("rooms_and_doors") or {}
SHAFT_ROOM_FAMILIES = tuple(_RD.get("shaft_room_families") or
                            ("Shaft", "Fire / Life Safety", "Unknown", "Plumbing", "MEP / Services"))
SHAFT_ROOM_FAMILY = str(_RD.get("shaft_room_family", "MEP / Services"))
SHAFT_ROOM_SIDE_MM = float(_RD.get("shaft_room_side_mm", 250.0))
SHAFT_ROOM_MAX_SQM = float(_RD.get("shaft_room_max_sqm", 12.0))
ROOM_DOOR_TOUCH_MM = float(_RD.get("door_touch_mm", 300.0))
LIFT_MACHINE_ROOM_GAP_MM = float(_RD.get("lift_machine_room_gap_mm", 450.0))
WALL_GAP_MAX_SQM = float(_RD.get("wall_gap_max_sqm", 0.5))
WALL_GAP_THIN_MM = float(_RD.get("wall_gap_thin_mm", 200.0))
WALL_GAP_MIN_SQM = float(_RD.get("wall_gap_min_sqm", 0.002))
# A lift's label is never taken by a strip (rule lift_label_fits_its_x),
# and the lift-size X of the X sequence is xplace.LIFT_SIDE_MM.
# Rule lift_label_fits_its_x: an X narrower than this is never a lift's.
LIFT_X_MIN_SIDE_MM = float(_RD.get("lift_x_min_side_mm", 1200.0))

# rule rooms_clear_of_walls: a room left with less than this share of itself
# once the walls are cut out of it was a label in a wall - dropped
ROOM_WALL_KEEP_SHARE = float(_RD.get("room_wall_keep_share", 0.25))
# rule x_cell_never_wall: an X this much covered by a wall is a slab opening
# the wall was drawn over (cut out of the wall); one covered for less than
# X_WALL_CLIP_SHARE is cut back to the wall crossing it
X_IN_WALL_SHARE = float(_RD.get("x_in_wall_share", 0.6))
X_WALL_CLIP_SHARE = float(_RD.get("x_wall_clip_share", 0.3))
# rule wall_junctions_closed: the widest gap closed between two wall pieces,
# and the largest piece of wall that closing may add
WALL_JUNCTION_GAP_MM = float(_RD.get("wall_junction_gap_mm", 250.0))
WALL_JUNCTION_MAX_SQM = float(_RD.get("wall_junction_max_sqm", 0.15))
# rule lift_well_rectangle: a lift outline filling this share of its
# rectangle (no larger than LIFT_RECT_MAX_SQM), with no more than
# LIFT_RECT_WALL_SHARE of the rectangle drawn as wall, is that rectangle
LIFT_RECT_MIN_FILL = float(_RD.get("lift_rect_min_fill", 0.75))
LIFT_RECT_MAX_SQM = float(_RD.get("lift_rect_max_sqm", 16.0))
LIFT_RECT_WALL_SHARE = float(_RD.get("lift_rect_wall_share", 0.06))
# rules stair_one_outline, toilet_with_cubicles, leftover_strip_joins_room:
# the pieces of one staircase are at most this far apart (with no wall
# between); a piece of floor no room took, holding no label, off the floor's
# edge and touching the room along JOIN_TOUCH_MM, joins a staircase up to
# STAIR_JOIN_MAX_SQM, a toilet (its cubicles) up to TOILET_JOIN_MAX_SQM, any
# other room up to LEFTOVER_MAX_SQM (and LEFTOVER_MIN_WIDTH_MM wide - a
# thinner one is wall)
STAIR_PIECE_GAP_MM = float(_RD.get("stair_piece_gap_mm", 150.0))
STAIR_JOIN_MAX_SQM = float(_RD.get("stair_join_max_sqm", 40.0))
TOILET_JOIN_MAX_SQM = float(_RD.get("toilet_join_max_sqm", 6.0))
LEFTOVER_MAX_SQM = float(_RD.get("leftover_max_sqm", 2.0))
LEFTOVER_MIN_WIDTH_MM = float(_RD.get("leftover_min_width_mm", 250.0))
JOIN_TOUCH_MM = float(_RD.get("join_touch_mm", 250.0))
# A piece of floor along a door opening no deeper than this is the strip in
# the doorway (between the door and the room's face): never taken into a
# staircase (P285: 0.1 sq.m on each of the four).
DOORWAY_STRIP_MAX_MM = float(_RD.get("doorway_strip_max_mm", 400.0))
LEFTOVER_NEVER_FAMILIES = tuple(_RD.get("leftover_never_families") or
                                ("Lift", "Staircase", "Lift Lobby", "Service Lobby", "Circulation", "Machine Room",
                                 "Mechanical Room", "Balcony / Open", "Void", "Shaft"))


def _merged_geom(d):
    """A floor pass boundary as a shapely polygon (its outline and holes; an
    X item its rectangles), or None."""
    from shapely.geometry import Polygon, box as _bx
    from shapely.ops import unary_union as _uu
    try:
        if d.get("poly") and len(d["poly"]) >= 3:
            g = Polygon(d["poly"], [h for h in (d.get("holes") or []) if len(h) >= 3]).buffer(0)
        elif d.get("xrects"):
            g = _uu([_bx(*r[:4]) for r in d["xrects"]])
        else:
            g = _bx(*d["bbox"])
    except Exception:                                 # noqa: BLE001
        return None
    if g is None or g.is_empty:
        return None
    if g.geom_type != "Polygon":
        parts = [q for q in getattr(g, "geoms", [g]) if q.geom_type == "Polygon"]
        g = max(parts, key=lambda q: q.area) if parts else None
    return g


def _set_merged_geom(d, g):
    d["poly"] = [(float(x), float(y)) for x, y in list(g.exterior.coords)[:-1]]
    d["holes"] = [[(float(x), float(y)) for x, y in list(r.coords)[:-1]] for r in g.interiors]
    d["bbox"] = tuple(float(v) for v in g.bounds)


# -- A shaft is its compartment (Fix 1) -------------------------------------
# The X marks the shaft; it is not its boundary. A shaft has walls on one
# to four sides, or a plain partition line on one or two sides between it
# and its shaft room, and the X may be drawn badly (partly in the box, an
# edge outside it). So the shaft is traced from the X like a room, on a
# tracer with every opening closed (its door too) and the plain lines of
# the drawing as walls:
#   * the trace must hold at least SHAFT_CELL_MIN_COVER of the X and be no
#     more than SHAFT_CELL_MAX_RATIO times its area (or the stated size,
#     when the label gives one); else the X stays as it was, cut to the
#     walls, and is flagged on AI_REVIEW;
#   * an X spanning two compartments (Centaurus PHE: two strips either side
#     of a walkway) is traced from several seeds and split by the line
#     between them - one shaft per compartment; the walkway, with no X, is
#     left alone;
#   * a room already traced over the same compartment (a label standing in
#     it) IS the shaft: the room keeps its outline and takes the X;
#   * only an X the X sequence made a shaft is traced (a stair's well is a
#     void, cut out of its staircase as a hole by rooms_and_doors).
SHAFT_CELL_MIN_COVER = float(_RD.get("shaft_cell_min_cover", 0.6))
SHAFT_CELL_MAX_RATIO = float(_RD.get("shaft_cell_max_ratio", 4.0))
SHAFT_CELL_STATED_TOL = float(_RD.get("shaft_cell_stated_tol", 1.3))
SHAFT_CELL_PART_MIN = float(_RD.get("shaft_cell_part_min", 0.15))
SHAFT_CELL_SAME_IOU = float(_RD.get("shaft_cell_same_iou", 0.7))
SHAFT_CELL_SKIP_FAMILIES = tuple(_RD.get("shaft_cell_skip_families") or
                                 ("Lift", "Void", "Balcony / Open", "Machine Room", "Mechanical Room", "Cutout", "Window"))


def shaft_compartments(merged, tracer_fn, segs, plate, stats, review, label_notes, overrides, family_rules=None):
    """Every X item of the floor traced to its compartment (in place). See
    the note above. tracer_fn() -> the tracer (built once, on demand)."""
    from shapely.geometry import Polygon as _P, Point as _Pt
    from shapely.ops import unary_union as _uu
    fr = family_rules or _FAMILY_RULES

    def fam_of(d):
        if d.get("family_forced"):
            return d["family_forced"]
        sr = d.get("sr_no")
        if sr is not None and sr in (overrides or {}):
            return overrides[sr]
        return family_for_label(d.get("label"), fr)["name"]
    # every X: with its diagonals' rectangles, or - found in a wall, or by
    # its outline (xregions) - with the outline it holds
    xs = [d for d in merged if d.get("source") == "X" and (d.get("xrects") or d.get("poly") or d.get("bbox"))]
    if not xs:
        return
    rooms = [(d, _merged_geom(d)) for d in merged if d.get("source") in ("P", "S")]
    rooms = [(d, g) for d, g in rooms if g is not None and not g.is_empty]
    tracer = None
    try:
        plate_pg = _P(plate).buffer(0)
    except Exception:                                 # noqa: BLE001
        plate_pg = None
    added = []
    gone = []
    traced_cells = []                                 # [(item, polygon)] compartments given out
    for d in xs:
        if d.get("x_class", "shaft") != "shaft":
            continue                                  # the X sequence made it no shaft
        fam = fam_of(d)
        if fam in SHAFT_CELL_SKIP_FAMILIES:
            continue
        # the X itself (its rectangles), not the cell it may already hold
        try:
            from shapely.geometry import box as _bx
            if not d.get("xrects"):
                raise ValueError("no rects")
            xg = _uu([_bx(*r[:4]) for r in d["xrects"]]).buffer(0)
            if xg.geom_type != "Polygon":
                xg = max(xg.geoms, key=lambda q: q.area)
        except Exception:                             # noqa: BLE001
            xg = _merged_geom(d)
        if xg is None or xg.is_empty or xg.area <= 0:
            continue
        have = _merged_geom(d)                        # what it holds now (a cell from its stated size)
        if d.get("_shaft_cell_seen"):
            continue
        d["_shaft_cell_seen"] = True
        if tracer is None:
            tracer = tracer_fn()
            if tracer is None:
                return
        ax = xg.area
        want = d.get("want")
        cap = SHAFT_CELL_MAX_RATIO * ax
        if want:
            cap = max(cap, SHAFT_CELL_STATED_TOL * float(want[0]) * float(want[1]))
        # seeds: the X's centre, then points along its axes
        b = xg.bounds
        cx, cy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
        w, h = b[2] - b[0], b[3] - b[1]
        seeds = [(cx, cy)]
        for fx, fy in ((-0.3, 0), (0.3, 0), (0, -0.3), (0, 0.3), (-0.3, -0.3), (0.3, 0.3), (-0.3, 0.3), (0.3, -0.3)):
            seeds.append((cx + fx * w, cy + fy * h))
        parts = []
        for sx, sy in seeds:
            if not xg.buffer(1.0).contains(_Pt(sx, sy)):
                continue
            tr = _trace_with(tracer, sx, sy)
            if tr is None or not tr.ok or not tr.polygon or tr.area_sqm <= 0:
                continue
            try:
                pg = _P(tr.polygon).buffer(0)
            except Exception:                         # noqa: BLE001
                continue
            if pg.is_empty or pg.geom_type != "Polygon":
                continue
            if plate_pg is not None and not plate_pg.is_empty:
                pg = pg.intersection(plate_pg)
                if pg.geom_type != "Polygon" or pg.is_empty:
                    continue
            if pg.area > cap:
                continue                              # ran out over the floor
            cover = pg.intersection(xg).area
            if cover < SHAFT_CELL_PART_MIN * ax:
                continue
            if any(pg.intersection(q).area >= SHAFT_CELL_SAME_IOU * min(pg.area, q.area) for q in parts):
                continue
            parts.append(pg)
        covered = sum(p.intersection(xg).area for p in parts)
        if os.environ.get("SHAFT_DEBUG"):
            print("shaft_cell", repr(str(d.get("label") or "")[:30]), "x", round(ax / 1e6, 2),
                  "have", round(have.area / 1e6, 2) if have is not None else None, "want", want,
                  "cap", round(cap / 1e6, 2), "parts", [round(p.area / 1e6, 2) for p in parts],
                  "cover", round(covered / ax, 2) if ax else None, "at", [round(v) for v in xg.bounds])
        # the cell it already holds from its stated size (x_cell) stands
        # when the trace gives less: the stated size is the better witness
        if want and have is not None and parts and have.area >= 0.9 * float(want[0]) * float(want[1]) \
                and sum(p.area for p in parts) < 0.95 * have.area:
            d["cell"] = True
            if not d.get("family_forced"):
                d["family_forced"] = "Shaft"
            stats["shaft_cells_stated"] = stats.get("shaft_cells_stated", 0) + 1
            continue
        if not parts or covered < SHAFT_CELL_MIN_COVER * ax:
            d["cell_fallback"] = "the trace from the X did not close on walls, doors or lines" if not parts \
                else "the traced cell holds only {0:.0%} of the X".format(covered / ax)
            review.append({"pts": [tuple(map(float, c)) for c in list(xg.exterior.coords)[:-1]],
                           "why": "shaft: " + d["cell_fallback"], "review": True})
            if d.get("sr_no") is not None:
                label_notes[d["sr_no"]] = ((label_notes.get(d["sr_no"]) or "") + "; shaft kept as its X: " +
                                           d["cell_fallback"]).strip("; ")
            stats["shaft_cells_fallback"] = stats.get("shaft_cells_fallback", 0) + 1
            continue
        parts.sort(key=lambda p: -p.intersection(xg).area)
        # a room already traced over a part IS the shaft: it takes the X;
        # a compartment another X already traced takes this X too
        first = True
        for pg in parts:
            owner = None
            for prev in traced_cells:
                if pg.intersection(prev[1]).area >= SHAFT_CELL_SAME_IOU * min(pg.area, prev[1].area):
                    owner = prev[0]
                    break
            for r_, g_ in rooms:
                if r_.get("x") or fam_of(r_) in SHAFT_CELL_SKIP_FAMILIES:
                    continue
                if pg.intersection(g_).area >= SHAFT_CELL_SAME_IOU * min(pg.area, g_.area):
                    owner = r_
                    break
            if owner is not None:
                owner["x"] = True
                owner["source"] = "X"                 # an X-marked opening: deducted
                owner["x_class"] = "shaft"            # the X sequence's shaft, in its compartment
                owner["x_step"], owner["x_why"] = d.get("x_step"), d.get("x_why")
                if owner.get("x_fid") is None:
                    owner["x_fid"] = d.get("x_fid")
                traced_cells.append((owner, pg))
                owner["xrects"] = list(owner.get("xrects") or []) + list(d.get("xrects") or [])
                owner["x_label"] = d.get("label")
                owner["cell"] = True
                if not owner.get("family_forced"):
                    owner["family_forced"] = "Shaft"
                if d.get("sr_no") is not None and d["sr_no"] not in (owner.get("absorbed_srs") or []):
                    owner.setdefault("absorbed_srs", []).append(d["sr_no"])
                if first:
                    gone.append(d)
                stats["shaft_cells_to_room"] = stats.get("shaft_cells_to_room", 0) + 1
            else:
                tgt = d if first else dict(d, absorbed_srs=[], sr_no=None)
                _set_merged_geom(tgt, pg)
                tgt["cx"], tgt["cy"] = pg.representative_point().x, pg.representative_point().y
                tgt["cell"] = True
                tgt["x_box"] = tuple(float(v) for v in xg.bounds)     # the X as drawn
                traced_cells.append((tgt, pg))
                if not tgt.get("family_forced"):
                    tgt["family_forced"] = "Shaft"
                if not first:
                    added.append(tgt)
                stats["shaft_cells_traced"] = stats.get("shaft_cells_traced", 0) + 1
            first = False
        if len(parts) > 1:
            stats["shaft_cells_split"] = stats.get("shaft_cells_split", 0) + 1
            if d.get("sr_no") is not None:
                label_notes[d["sr_no"]] = ((label_notes.get(d["sr_no"]) or "") +
                                           "; X across {0} compartments, one shaft each".format(len(parts))).strip("; ")
    if gone:
        merged[:] = [d for d in merged if not any(d is g for g in gone)]
    merged.extend(added)
    # a room traced over a compartment (its label's trace ran past the
    # partition line, which rooms do not close on) is cut back to its side
    if traced_cells:
        for r_, g_ in rooms:
            if r_.get("x") or r_.get("cell") or any(r_ is t_ for t_, _p in traced_cells):
                continue
            g2 = g_
            for _t, cell in traced_cells:
                if g2.intersects(cell) and g2.intersection(cell).area >= 0.1 * cell.area:
                    g2 = g2.difference(cell)
            if g2 is not g_:
                if g2.geom_type == "MultiPolygon":
                    g2 = max(g2.geoms, key=lambda q: q.area)
                if g2.geom_type == "Polygon" and not g2.is_empty and g2.area >= 0.2 * g_.area:
                    _set_merged_geom(r_, g2)
                    stats["rooms_cut_back_from_shafts"] = stats.get("rooms_cut_back_from_shafts", 0) + 1


def rooms_and_doors(merged, doors, fam_name, rule, segs=None, user_set=None, stats=None):
    """The floor's boundaries (merged, changed in place) settled with its
    door openings (doors: quads, mm): see the note above. fam_name(d) -> the
    family it would be drawn in; user_set(d) -> True where the user picked
    its family in Step 3 (never overridden); segs: the floor's segments, for
    the stair flights. Returns stats."""
    from shapely.geometry import Polygon, LineString
    from shapely.ops import unary_union
    from . import elements as _el
    stats = stats if stats is not None else {}
    rule = dict(rule_book.defaults(), **(rule or {}))
    user_set = user_set or (lambda d: False)
    geoms = {id(d): _merged_geom(d) for d in merged}

    def is_x(d):
        return d.get("source") == "X"
    quads, widths = [], []
    for q in doors or ():
        try:
            g = Polygon([(float(p[0]), float(p[1])) for p in q]).buffer(0)
        except (ValueError, TypeError, IndexError):
            g = None
        ax = _el.door_axes(q) if g is not None and not g.is_empty else None
        quads.append(g if ax is not None else None)
        widths.append(ax[6] if ax is not None else 0.0)

    def doors_of(g):
        out = []
        for k, qg in enumerate(quads):
            if qg is None or qg.distance(g) > 30.0:
                continue
            if g.exterior.intersection(qg.buffer(30.0)).length >= min(ROOM_DOOR_TOUCH_MM, 0.5 * widths[k]):
                out.append(k)
        return out

    # 0. an X mark is what the X sequence made it (xplace, 2026-10-16): its
    # family is set there and nothing here changes it.

    # 1. the open well between two flights: an X the X sequence made a void
    # standing between the flights of its staircase is cut out of the
    # staircase as a hole (drawn on AI_VOID)
    stairs = [d for d in merged if not is_x(d) and geoms[id(d)] is not None and fam_name(d) == "Staircase"]
    if stairs:
        gone = []
        for x in merged:
            xg = geoms[id(x)]
            if not is_x(x) or xg is None or not x.get("stair_well") or x.get("x_class") != "void":
                continue
            for st in stairs:
                sg = geoms[id(st)]
                if sg is None:
                    continue
                whole = Polygon(sg.exterior)          # the staircase with its holes filled
                if whole.intersection(xg).area < 0.9 * xg.area:
                    continue
                if sg.intersection(xg).area > 0.1 * xg.area:
                    # not yet a hole in it: cut it out (drawn on AI_VOID)
                    new = sg.difference(xg)
                    if new.is_empty or new.geom_type != "Polygon":
                        continue
                    _set_merged_geom(st, new)
                    geoms[id(st)] = new
                gone.append(x)
                stats["stair_voids_cut"] = stats.get("stair_voids_cut", 0) + 1
                break
        if gone:
            merged[:] = [d for d in merged if not any(d is x for x in gone)]

    # 2. a shaft's room: a door of its own, a side shared with the X - a
    # small room named for a shaft (TEN, ELE SHAFT, FHC ...), or for the X
    # beside it, or not named; never a refuge area or a store beside a shaft
    xs = [(d, geoms[id(d)]) for d in merged if is_x(d) and geoms[id(d)] is not None
          and d.get("x_class", "shaft") == "shaft"]
    shaft_rooms = []
    if rule["shaft_room_mep"] and xs:
        from .area_statement import SHAFT_PATTERNS as _SP, _first as _f1, _norm as _nm

        def shaft_named(d, beside):
            n = _nm(d.get("label"))
            if not n or "UNKNOWN" in n:
                return True
            if _f1(_SP, n) or re.search(r"\bSHAFT\b|\bRISER\b|\bDUCT\b", n):
                return True
            return any(_nm(x.get("label")) == n for x in beside)
        for d in merged:
            g = geoms[id(d)]
            if is_x(d) or g is None or fam_name(d) not in SHAFT_ROOM_FAMILIES:
                continue
            if g.area > SHAFT_ROOM_MAX_SQM * 1e6:
                continue
            beside = [x for x, xg in xs if xg.distance(g) <= 30.0 and
                      xg.exterior.intersection(g.buffer(30.0)).length >= SHAFT_ROOM_SIDE_MM]
            if not beside or not shaft_named(d, beside):
                continue
            dk = doors_of(g)
            if not dk:
                continue
            d["shaft_room"] = True
            shaft_rooms.append((d, dk))
            if rule["x_named_by_its_room"]:
                # an unnamed X at the back of a room named for a shaft is that
                # shaft (XA: the 2500 x 500 X of ELECTRICAL SH)
                own = _nm(d.get("label"))
                if own and "UNKNOWN" not in own and (_f1(_SP, own) or
                                                     re.search(r"\bSHAFT\b|\bRISER\b|\bDUCT\b", own)):
                    for x in beside:
                        xn = _nm(x.get("label"))
                        if (not xn or "UNKNOWN" in xn) and not x.get("named_by_room"):
                            x["label"] = d.get("label")
                            x["named_by_room"] = True
                            stats["x_named_by_room"] = stats.get("x_named_by_room", 0) + 1
            if fam_name(d) != SHAFT_ROOM_FAMILY and not user_set(d):
                d["family_forced"] = SHAFT_ROOM_FAMILY
                stats["shaft_rooms_to_mep"] = stats.get("shaft_rooms_to_mep", 0) + 1

    # 3. (Fix 2) a shaft's room no longer runs to the outer face of its
    # door: every room stops at its own face of the wall, and every door
    # line sits on the inner face of the wall of the room it serves
    # (elements.door_line).

    # 4. a lift with no door, its front on a machine room
    if rule["lift_no_door_machine_room"]:
        mrs = [geoms[id(d)] for d in merged if geoms[id(d)] is not None and fam_name(d) == "Machine Room"]
        if mrs:
            for d in merged:
                g = geoms[id(d)]
                if g is None or is_x(d) or user_set(d) or fam_name(d) != "Lift" or doors_of(g):
                    continue                          # (an X lift is the X sequence's)
                b = g.bounds
                side = min(b[2] - b[0], b[3] - b[1])
                if not any(mg.distance(g) <= LIFT_MACHINE_ROOM_GAP_MM and
                           mg.exterior.intersection(g.buffer(LIFT_MACHINE_ROOM_GAP_MM)).length >= 0.5 * side
                           for mg in mrs):
                    continue
                d["family_forced"] = "Machine Room"
                stats["lifts_to_machine_room"] = stats.get("lifts_to_machine_room", 0) + 1

    # 5. a lift with no door opening (rule lift_without_door_mechanical): an
    # X lift or one traced from its label, with no gap in its walls (the
    # floor pass drew a door across every gap it found) and no door, is no
    # lift that stops on this floor - a mechanical room (P285 11th floor:
    # LIFT WELL BELOW). A family picked by hand is never changed.
    if rule["lift_without_door_mechanical"]:
        for d in merged:
            g = geoms[id(d)]
            if g is None or user_set(d) or fam_name(d) != "Lift" or doors_of(g):
                continue
            d["family_forced"] = "Mechanical Room"
            d["no_lift_door"] = True
            stats["lifts_to_mechanical_room"] = stats.get("lifts_to_mechanical_room", 0) + 1
    return stats


def wall_gap_pieces(plate, wall_items, cover, hatch_islands=(), label_pts=(), columns=(), doors=()):
    """The wall left between the rooms (rule wall_gaps_filled): pieces of the
    floor no room, door, window, column, glass or wall took that are wall -
      - a piece of a wall hatch (P285: the 200 x 200 pillar between the two
        doors of each ELE SHAFT);
      - a thin enclosed strip, no wider than WALL_GAP_THIN_MM, holding no
        label and off the floor's edge (P285: 450 x 150 beside each FHC) -
        not a ring round a column or a strip along one (a column's casing),
        not a strip along a door (the rest of its opening);
    each WALL_GAP_MIN_SQM to WALL_GAP_MAX_SQM. plate: the floor outline;
    wall_items: [[outer, hole, ...]] as wall_rings gives; cover, columns,
    doors: shapely polygons (columns and doors in cover too);
    hatch_islands: [(outer, holes, ...)]. Shapely polygons, mm."""
    from shapely.geometry import Polygon, Point
    from shapely.ops import unary_union
    try:
        P = Polygon([tuple(p[:2]) for p in plate]).buffer(0)
    except (ValueError, TypeError, IndexError):
        return []
    if P.is_empty:
        return []
    polys = []
    for item in wall_items or ():
        try:
            if item and len(item[0]) >= 3:
                polys.append(Polygon(item[0], [h for h in item[1:] if len(h) >= 3]).buffer(0))
        except (ValueError, TypeError, IndexError):
            continue
    C = unary_union(polys + [c for c in cover or () if c is not None and not c.is_empty])
    free = P.difference(C.buffer(1.0, join_style=2))

    def parts(g):
        return [q for q in getattr(g, "geoms", [g]) if q.geom_type == "Polygon" and not q.is_empty]
    lo, hi = WALL_GAP_MIN_SQM * 1e6, WALL_GAP_MAX_SQM * 1e6
    out = []
    hs = []
    for h in hatch_islands or ():
        try:
            if len(h[0]) >= 3:
                hs.append(Polygon(h[0], [r for r in (h[1] or []) if len(r) >= 3]).buffer(0))
        except (ValueError, TypeError, IndexError):
            continue
    hs = [h for h in hs if not h.is_empty and h.intersects(P)]
    if hs:
        for q in parts(free.intersection(unary_union(hs))):
            if lo <= q.area <= hi:
                out.append(q)
        if out:
            free = free.difference(unary_union(out))
    edge = P.exterior.buffer(5.0)
    pts = [Point(float(p[0]), float(p[1])) for p in label_pts or ()]
    cols = unary_union([c for c in columns or () if c is not None and not c.is_empty]).buffer(20.0)
    drs = unary_union([c for c in doors or () if c is not None and not c.is_empty]).buffer(20.0)
    for q in parts(free):
        if not (lo <= q.area <= hi) or q.intersects(edge) or list(q.interiors):
            continue
        if not q.buffer(-WALL_GAP_THIN_MM / 2.0).is_empty:
            continue
        if any(q.contains(pt) for pt in pts):
            continue
        rim = q.exterior
        if not cols.is_empty and rim.intersection(cols).length >= 0.2 * rim.length:
            continue                                  # a column's casing
        if not drs.is_empty and rim.intersection(drs).length >= 0.3 * rim.length:
            continue                                  # the rest of a door's opening
        out.append(q)
    return out


def _wall_union(wall_items):
    """The walls (wall_rings point lists: outline, holes) as one shapely
    geometry, mm."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    polys = []
    for item in wall_items or ():
        try:
            if item and len(item[0]) >= 3:
                g = Polygon([tuple(p[:2]) for p in item[0]],
                            [[tuple(p[:2]) for p in h] for h in item[1:] if len(h) >= 3]).buffer(0)
                if not g.is_empty:
                    polys.append(g)
        except (ValueError, TypeError, IndexError):
            continue
    return unary_union(polys) if polys else None


def _polys_of(g, min_area=1.0):
    return [q for q in getattr(g, "geoms", [g]) if q is not None and q.geom_type == "Polygon" and q.area >= min_area]


def settle_rooms_and_walls(items, wall_items, rule, stats=None, cover=(), plate=None, label_pts=(), doors=()):
    """The floor's rooms, X-marked openings and walls settled against each
    other, last (each a rule in rules.py):
      x_cell_never_wall     an X the wall covers for X_IN_WALL_SHARE or more
                            is a slab opening the wall was drawn over: cut
                            out of the wall (XA: the 300 mm ELE SH strips);
                            one a wall crosses for less than X_WALL_CLIP_SHARE
                            is cut back to it (Centaurus STP EX.);
      wall_junctions_closed a gap no wider than WALL_JUNCTION_GAP_MM between
                            two wall pieces is closed where it holds no room,
                            X, door, window or column, and the walls are one
                            outline (XA: 6-226 mm at the corners);
      lift_well_rectangle   a lift outline notched by its frame lines is its
                            rectangle (XA, lower lifts);
      rooms_clear_of_walls  every room less the walls; a room left with less
                            than ROOM_WALL_KEEP_SHARE of itself was a label in
                            a wall and is dropped (XA COM ELV SH);
      stair_one_outline     the pieces of one staircase joined, and the floor
                            left round its flights taken in (XA FIRE
                            STAIRCASE-1 and -2, Centaurus S5-S8);
      toilet_with_cubicles  the cubicles the toilet's outline left out taken
                            in (XA FEMALE / MALE TOILETS);
      leftover_strip_joins_room  a small unlabelled piece of floor beside one
                            room only, with no wall between, taken in
                            (Centaurus: the strip beside the AHU ROOM's shaft).
    items: [{"geom": shapely polygon, "x": bool, "keep": bool (a room run
    over a wall on purpose: a balcony to its parapet's outer face), "pt":
    (x, y) its label or None, "fam": its family, "lift": bool, "split": bool
    (a room a wall cuts in two may stand as both pieces)}] - "geom" is
    changed in place, "drop" set on a room to drop, "pieces" set to the
    further pieces of a split one. wall_items: [[outer, hole, ...]]
    (wall_rings). cover: other shapely polygons nothing is grown over (doors,
    windows, columns). plate: the floor outline (points) and label_pts the
    texts' positions, for the leftover pieces; doors: the door openings
    (shapely polygons) - a leftover piece in a doorway (a ramp, a threshold:
    A-106 AHU rooms) is not taken in. Returns the walls as
    wall_rings point lists (None when there were none). Shapely polygons,
    mm."""
    from shapely.geometry import Point
    from shapely.ops import unary_union
    stats = stats if stats is not None else {}
    W = _wall_union(wall_items)
    if W is None or W.is_empty:
        return wall_items

    def bump(k, v=1):
        stats[k] = round(stats.get(k, 0) + v, 3) if isinstance(v, float) else stats.get(k, 0) + v
    xs = [it for it in items if it.get("x") and it.get("geom") is not None and not it["geom"].is_empty]
    rooms = [it for it in items if not it.get("x") and it.get("geom") is not None and not it["geom"].is_empty]
    if rule["x_cell_never_wall"] and xs:
        cut = []
        for it in xs:
            xg = it["geom"]
            if xg.area <= 0:
                continue
            ov = xg.intersection(W).area
            if ov >= X_IN_WALL_SHARE * xg.area:
                cut.append(xg)
                bump("x_cut_out_of_walls")
            elif 0.02e6 <= ov < X_WALL_CLIP_SHARE * xg.area:
                rest = _polys_of(xg.difference(W), 0.01e6)
                if rest:
                    c = xg.centroid
                    held = [q for q in rest if q.buffer(1.0).contains(c)]
                    it["geom"] = held[0] if held else max(rest, key=lambda q: q.area)
                    bump("x_cut_back_to_walls")
        if cut:
            W = W.difference(unary_union(cut).buffer(1.0, join_style=2))
    if rule["lift_well_rectangle"]:
        # a lift well is the rectangle between its wall faces: the frame,
        # the jambs and the counterweight lines drawn in it do not notch it,
        # and a strip of them taken for wall is not wall (XA, lower lifts)
        others = [it["geom"] for it in xs + rooms]
        for it in rooms:
            if not it.get("lift"):
                continue
            lg = it["geom"]
            R = lg.minimum_rotated_rectangle
            if R.geom_type != "Polygon" or R.area <= 0 or R.area > LIFT_RECT_MAX_SQM * 1e6:
                continue
            if lg.area < LIFT_RECT_MIN_FILL * R.area or lg.area >= 0.995 * R.area:
                continue
            if any(o is not lg and o.intersects(R) and o.intersection(R).area > 0.02e6 for o in others):
                continue                                # it would take in a neighbour
            win = R.intersection(W).area
            if win > LIFT_RECT_WALL_SHARE * R.area:
                continue
            if win > 0:
                W = W.difference(R.buffer(-1.0, join_style=2))
            it["geom"] = R
            it["keep"] = True
            bump("lift_wells_squared")
    if rule["wall_junctions_closed"] and WALL_JUNCTION_GAP_MM > 0:
        g = WALL_JUNCTION_GAP_MM / 2.0
        closed = W.buffer(g, join_style=2, mitre_limit=2.0).buffer(-g, join_style=2, mitre_limit=2.0)
        add = closed.difference(W)
        blockers = [it["geom"] for it in xs + rooms] + [c for c in cover or () if c is not None and not c.is_empty]
        B = unary_union(blockers) if blockers else None
        fills = []
        parts_w = _polys_of(W)
        for q in _polys_of(add, 100.0):
            if q.area > WALL_JUNCTION_MAX_SQM * 1e6:
                continue
            if B is not None and q.intersects(B) and q.intersection(B).area > max(0.002e6, 0.1 * q.area):
                continue
            # a junction: the gap touches two pieces of wall, or one piece on
            # two sides (an L the corner of which is open)
            near = [p for p in parts_w if p.distance(q) <= 1.0]
            if len(near) < 2 and not (near and near[0].boundary.intersection(q.buffer(1.0)).length >= 2.2 * min(
                    q.minimum_rotated_rectangle.length / 4.0, 400.0)):
                continue
            fills.append(q)
        if fills:
            W = unary_union([W] + [f.buffer(0.5, join_style=2) for f in fills])
            bump("wall_junctions_closed", len(fills))
            bump("wall_junctions_closed_sqm", round(sum(f.area for f in fills) / 1e6, 3))
    if rule["rooms_clear_of_walls"] and rooms:
        Wb = W.buffer(-1.0, join_style=2)               # touching a wall face is not covering it
        for it in rooms:
            if it.get("keep"):
                continue
            rg = it["geom"]
            if not rg.intersects(Wb):
                continue
            ov = rg.intersection(Wb).area
            if ov < 0.01e6:
                continue                                # a sliver: a face drawn twice
            rest = _polys_of(rg.difference(W), 0.01e6)
            total = sum(q.area for q in rest)
            if total < ROOM_WALL_KEEP_SHARE * rg.area:
                it["drop"] = True
                bump("rooms_in_walls_dropped")
                continue
            pt = it.get("pt")
            held = [q for q in rest if pt is not None and q.buffer(1.0).contains(Point(*pt))] if pt else []
            it["geom"] = held[0] if held else max(rest, key=lambda q: q.area)
            if it.get("split"):
                # a room a wall runs through (a staircase box across two
                # stairs): each sizeable piece stands
                it["pieces"] = [q for q in rest if q is not it["geom"] and
                                q.area >= max(0.5e6, ROOM_WALL_KEEP_SHARE * rg.area)]
            bump("rooms_cut_by_walls")
            bump("rooms_cut_by_walls_sqm", round((rg.area - it["geom"].area) / 1e6, 3))
        for it in list(rooms):
            for q in it.pop("pieces", None) or ():
                twin = dict(it, geom=q, pt=None, twin_of=it)
                items.append(twin)
                rooms.append(twin)
                bump("rooms_split_by_walls")
    live = [it for it in rooms if not it.get("drop")]
    if rule["stair_one_outline"]:
        # the pieces of one staircase - touching, or a line apart with no
        # wall between - are one
        stairs = [it for it in live if it.get("fam") == "Staircase"]
        merged_any = True
        while merged_any:
            merged_any = False
            for i, a in enumerate(stairs):
                if a.get("drop"):
                    continue
                for b in stairs[i + 1:]:
                    if b.get("drop") or a["geom"].distance(b["geom"]) > STAIR_PIECE_GAP_MM:
                        continue
                    u = unary_union([a["geom"], b["geom"]])
                    if a["geom"].intersects(b["geom"]) and a["geom"].intersection(b["geom"]).area > 0:
                        j = u
                    else:
                        j = u.buffer(STAIR_PIECE_GAP_MM, join_style=2).buffer(-STAIR_PIECE_GAP_MM, join_style=2)
                        gap = j.difference(u)
                        if gap.intersects(W) and gap.intersection(W).area > 0.01e6:
                            continue                    # a wall between: two staircases
                    jp = _polys_of(j, 0.01e6)
                    if len(jp) != 1:
                        continue
                    a["geom"] = jp[0]
                    b["drop"] = True
                    b["merged_into"] = a
                    merged_any = True
                    bump("stair_pieces_joined")
        live = [it for it in rooms if not it.get("drop")]
    if plate is not None and (rule["stair_one_outline"] or rule["toilet_with_cubicles"]
                              or rule["leftover_strip_joins_room"]):
        from shapely.geometry import Polygon
        try:
            P = Polygon([tuple(p[:2]) for p in plate]).buffer(0)
        except (ValueError, TypeError, IndexError):
            P = None
        if P is not None and not P.is_empty:
            # a room's holes (a stair's open well, a column in it) are not
            # floor left over: rooms count by their outline
            occ = unary_union([W] + [Polygon(it["geom"].exterior) for it in xs + live] +
                              [c for c in cover or () if c is not None and not c.is_empty])
            free = P.difference(occ.buffer(1.0, join_style=2))
            edge = P.exterior.buffer(5.0)
            lbl = [Point(float(p[0]), float(p[1])) for p in label_pts or () if p and p[0] is not None and p[1] is not None]
            dgs = [g for g in doors or () if g is not None and not g.is_empty]
            # the largest piece first: the order shapely hands the pieces in
            # moves with any change elsewhere on the floor (Centaurus S/C-S2:
            # an X turned cut-out reordered them, and the stair's two halves
            # stayed apart)
            for q in sorted(_polys_of(free, 0.05e6), key=lambda g: -g.area):
                if q.area > STAIR_JOIN_MAX_SQM * 1e6 or q.intersects(edge):
                    continue
                if any(q.contains(l) for l in lbl):
                    continue
                qb = q.buffer(2.0)
                touch = [it for it in live if not it.get("drop") and qb.intersects(it["geom"]) and
                         qb.intersection(it["geom"].boundary).length >= JOIN_TOUCH_MM]
                if not touch:
                    continue
                fams = set(it.get("fam") for it in touch)
                to = None
                # in a doorway: the piece runs along a door opening (a ramp,
                # a threshold, the strip between a door and the room's face)
                in_door = any(qb.intersects(dg) and qb.intersection(dg.boundary).length >= JOIN_TOUCH_MM
                              for dg in dgs)
                if in_door:
                    mrr = q.minimum_rotated_rectangle
                    ps_ = list(mrr.exterior.coords) if mrr.geom_type == "Polygon" else []
                    short_ = min(math.hypot(ps_[1][0] - ps_[0][0], ps_[1][1] - ps_[0][1]),
                                 math.hypot(ps_[2][0] - ps_[1][0], ps_[2][1] - ps_[1][1])) if len(ps_) >= 3 else 0.0
                else:
                    short_ = None
                if fams == {"Staircase"} and rule["stair_one_outline"] and not (in_door and short_ <= DOORWAY_STRIP_MAX_MM):
                    # (a stair's landing, not the strip in its doorway: P285)
                    to, key = touch, "stair_floor_joined"
                elif fams == {"Toilet"} and len(touch) == 1 and rule["toilet_with_cubicles"] \
                        and q.area <= TOILET_JOIN_MAX_SQM * 1e6 \
                        and not q.buffer(-LEFTOVER_MIN_WIDTH_MM / 2.0).is_empty:
                    to, key = touch, "toilet_cubicles_joined"
                elif len(touch) == 1 and rule["leftover_strip_joins_room"] and q.area <= LEFTOVER_MAX_SQM * 1e6 \
                        and touch[0].get("fam") not in LEFTOVER_NEVER_FAMILIES and not touch[0].get("keep") \
                        and not q.buffer(-LEFTOVER_MIN_WIDTH_MM / 2.0).is_empty and not in_door:
                    # (not in a doorway: a ramp or a threshold stays out)
                    to, key = touch, "leftover_strips_joined"
                if not to:
                    continue
                a = max(to, key=lambda it: it["geom"].area)
                # closed over the 2 mm the touch test allows
                u = unary_union([q] + [it["geom"] for it in to]).buffer(2.5, join_style=2).buffer(-2.5, join_style=2)
                up = _polys_of(u, 0.01e6)
                if len(up) != 1:
                    continue
                a["geom"] = up[0]
                for it in to:
                    if it is not a:
                        it["drop"] = True
                        it["merged_into"] = a
                bump(key)
                bump(key + "_sqm", round(q.area / 1e6, 3))
    return wall_rings(_polys_of(W))


def _floor_job(prep, floor, rows, rule, geometry=None, geometry_only=False, family_overrides=None):
    """What one floor's pass needs, cut from the prepared drawing."""
    raw_segs, seg_boxes = prep["raw_segs"], prep["seg_boxes"]
    detection_mode, x_clusters = prep["detection_mode"], prep["x_clusters"]
    x_lines, void_outlines = prep["x_lines"], prep["void_outlines"]
    fixtures, all_swings = prep["fixtures"], prep["all_swings"]
    circles = prep.get("circles") or []
    wall_hatches = prep.get("wall_hatches") or []
    box = _floor_clip_box(floor["polygon"])
    return {
            "floor": {"name": floor["name"],
                      "polygon": [tuple(p) for p in floor["polygon"]],
                      "layer": floor.get("layer") or ""},
            "rows": rows,
            "segs": _segs_in_box(raw_segs, seg_boxes, box),
            "swings": [sw for sw in all_swings
                       if box[0] <= sw[0] <= box[2] and box[1] <= sw[1] <= box[3]],
            "x_clusters": [cl for cl in x_clusters
                           if _point_in_polygon(cl["cx"], cl["cy"],
                                                floor["polygon"])],
            "x_lines": x_lines, "rule": rule,
            "dashed": frozenset(k for k in (prep.get("dashed") or ())
                                if box[0] - 1000 <= k[0][0] <= box[2] + 1000
                                and box[1] - 1000 <= k[0][1] <= box[3] + 1000),
            # Shapes decided in the Geometry tab (geomextract), if any.
            "geometry": (geometry or {}).get(floor["name"]) or [],
            "geometry_only": bool(geometry_only),
            "void_outlines": [o for o in void_outlines
                              if _point_in_polygon(sum(p[0] for p in o) / len(o),
                                                   sum(p[1] for p in o) / len(o),
                                                   floor["polygon"])],
            "wall_hatches": [h for h in wall_hatches
                             if box[0] - 2000 <= min(p[0] for p in h[0]) and max(p[0] for p in h[0]) <= box[2] + 2000
                             and box[1] - 2000 <= min(p[1] for p in h[0]) and max(p[1] for p in h[0]) <= box[3] + 2000],
            "all_hatches": [h for h in (prep.get("all_hatches") or [])
                            if box[0] - 2000 <= min(p[0] for p in h[0]) and max(p[0] for p in h[0]) <= box[2] + 2000
                            and box[1] - 2000 <= min(p[1] for p in h[0]) and max(p[1] for p in h[0]) <= box[3] + 2000],
            "circles": [c for c in circles
                        if box[0] - 2000 <= c[0] <= box[2] + 2000 and box[1] - 2000 <= c[1] <= box[3] + 2000],
            "glass_segs": [w for w in (prep.get("glass_segs") or [])
                           if box[0] - 1000 <= w[0] <= box[2] + 1000 and box[1] - 1000 <= w[1] <= box[3] + 1000],
            "window_evidence": [w for w in (prep.get("window_evidence") or [])
                                if box[0] - 1000 <= w[0] <= box[2] + 1000 and box[1] - 1000 <= w[1] <= box[3] + 1000],
            "door_arcs": [a for a in (prep.get("door_arcs") or [])
                          if box[0] - 500 <= a[0] <= box[2] + 500 and box[1] - 500 <= a[1] <= box[3] + 500],
            "wall_arcs": [a for a in (prep.get("wall_arcs") or [])
                          if box[0] - 2000 <= a[0] + a[2] * math.cos(math.radians((a[3] + a[4]) / 2.0)) <= box[2] + 2000
                          and box[1] - 2000 <= a[1] + a[2] * math.sin(math.radians((a[3] + a[4]) / 2.0)) <= box[3] + 2000],
            "columns": ((prep.get("columns") or {}).get("columns") or {}).get(floor["name"]) or [],
            "column_rejects": ((prep.get("columns") or {}).get("rejects") or {}).get(floor["name"]) or [],
            "fixtures": [f for f in fixtures
                         if box[0] <= f[0] <= box[2] and box[1] <= f[1] <= box[3]],
            "stair_marks": [p for p in (prep.get("stair_marks") or [])
                            if box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]],
            "detection_mode": detection_mode,
            "family_overrides": {floor["name"]:
                                 (family_overrides or {}).get(floor["name"]) or {}},
            "family_rules": rules_for_usage(floor.get("usage")),
    }


# -- Rotated cores and wings (Fix 0, rotation.py) ----------------------------
# The floor pass measures along the drawing's axes. A core drawn at an angle
# is worked out in a small drawing of its own, turned back to the axes
# (rotation.rotated_subdocument), by the same pass; what it finds is turned
# back and put in place of what the main pass found there.

def _unrotate_pass(res, theta, origin):
    """A floor pass result turned by theta about origin (in place)."""
    from . import rotation as _rot

    def pts(ps):
        return [tuple(q) for q in _rot.rotate_pts(ps, theta, origin)]

    def rect_pts(r):
        return pts([(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])])

    def bounds(ps):
        xs, ys = [q[0] for q in ps], [q[1] for q in ps]
        return (min(xs), min(ys), max(xs), max(ys))

    for d in res.get("merged") or []:
        if d.get("poly") and len(d["poly"]) >= 3:
            d["poly"] = pts(d["poly"])
            d["holes"] = [pts(h) for h in (d.get("holes") or []) if len(h) >= 3]
            d["bbox"] = bounds(d["poly"])
        elif d.get("xrects"):
            # an X item is its rectangles: turned, they are rotated boxes -
            # the outline carries them, the rectangles keep their bounds
            rings = [rect_pts(r) for r in d["xrects"]]
            try:
                from shapely.geometry import Polygon as _P
                from shapely.ops import unary_union as _u
                g = _u([_P(r) for r in rings]).buffer(0)
                if g.geom_type != "Polygon":
                    g = max(g.geoms, key=lambda q: q.area)
                d["poly"] = [tuple(map(float, c)) for c in list(g.exterior.coords)[:-1]]
                d["holes"] = []
            except Exception:                         # noqa: BLE001
                d["poly"] = rings[0]
            d["xrects"] = [bounds(r) + tuple(r_[4:]) for r, r_ in zip(rings, d["xrects"])]
            d["bbox"] = bounds(d["poly"])
        elif d.get("bbox"):
            d["poly"] = rect_pts(d["bbox"])
            d["bbox"] = bounds(d["poly"])
        if d.get("cx") is not None and d.get("cy") is not None:
            d["cx"], d["cy"] = _rot.rotate_pt(float(d["cx"]), float(d["cy"]), theta, origin)
        if d.get("_grown_strip"):
            d["_grown_strip"] = pts(d["_grown_strip"])
        for key in ("region",):
            v_ = d.get(key)
            if isinstance(v_, (list, tuple)) and len(v_) >= 3:
                d[key] = pts(v_)
    walls = res.get("walls") or {}
    res["walls"] = {cls: [[pts(ring) for ring in piece] for piece in pieces] for cls, pieces in walls.items()}
    res["doors"] = [[list(q_) for q_ in pts(q)] for q in (res.get("doors") or [])]
    for key in ("windows", "cutouts", "x_columns"):
        for w in res.get(key) or []:
            if w.get("poly"):
                w["poly"] = pts(w["poly"])
            if w.get("cx") is not None:
                w["cx"], w["cy"] = _rot.rotate_pt(float(w["cx"]), float(w["cy"]), theta, origin)
    res["glass"] = [pts(g) for g in (res.get("glass") or [])]
    res["wall_veto"] = [pts(v) for v in (res.get("wall_veto") or []) if len(v) >= 3]
    for r_ in list(res.get("door_review") or []) + list(res.get("shaft_review") or []):
        if r_.get("pts"):
            r_["pts"] = pts(r_["pts"])
    for r_ in res.get("wall_exclusions") or []:
        ln = r_.get("line")
        if ln and len(ln) >= 4:
            (x0, y0), (x1, y1) = pts([(ln[0], ln[1]), (ln[2], ln[3])])
            r_["line"] = [x0, y0, x1, y1] + list(ln[4:])
    for c_ in res.get("window_conflicts") or []:
        if c_.get("bbox"):
            c_["bbox"] = [round(v) for v in bounds(rect_pts(c_["bbox"]))]
    for r_ in res.get("x_placement") or []:
        if r_.get("bbox"):
            r_["bbox"] = [round(v) for v in bounds(rect_pts(r_["bbox"]))]
    return res


def _rect_ring(b):
    if not b or len(b) < 4:
        return []
    return [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]


def _merge_rotated_pass(res, sub, core_pts, rows_in):
    """What the turned pass found inside the cluster's core replaces what
    the main pass found there (in place, in res)."""
    from shapely.geometry import Polygon as _P, Point as _Pt
    core = _P(core_pts).buffer(0)

    def inside(d):
        g = _merged_geom(d)
        if g is None or g.is_empty:
            return False
        c = g.representative_point()
        return core.contains(c)

    def ring_inside(ps):
        try:
            g = _P(ps).buffer(0)
            return not g.is_empty and core.contains(g.representative_point())
        except Exception:                             # noqa: BLE001
            return False
    res["merged"] = [d for d in res.get("merged") or [] if not inside(d)] + \
                    [d for d in sub.get("merged") or [] if inside(d)]
    # texts that stand in the core: what the turned pass made of them
    srs = {r.get("sr_no") for r in rows_in if r.get("sr_no") is not None}
    for key in ("text_status", "label_notes", "floor_family_overrides", "size_flags"):
        src = sub.get(key) or {}
        dst = dict(res.get(key) or {})
        for sr in list(dst):
            if sr in srs or str(sr) in {str(v) for v in srs}:
                dst.pop(sr, None)
        for sr, v in src.items():
            if sr in srs or str(sr) in {str(v_) for v_ in srs}:
                dst[sr] = v
        res[key] = dst
    res["grown"] = [g for g in (res.get("grown") or []) if g not in srs] + \
                   [g for g in (sub.get("grown") or []) if g in srs]
    # walls: the main pass's pieces cut back to outside the core, the
    # turned pass's pieces inside it
    walls = {}
    for cls in set(list((res.get("walls") or {}).keys()) + list((sub.get("walls") or {}).keys())):
        out = []
        for piece in (res.get("walls") or {}).get(cls) or []:
            try:
                g = _P(piece[0], [h for h in piece[1:] if len(h) >= 3]).buffer(0).difference(core)
            except Exception:                         # noqa: BLE001
                continue
            for q in getattr(g, "geoms", [g]):
                if q.geom_type == "Polygon" and q.area > 1.0:
                    out.append([[list(map(float, c)) for c in list(q.exterior.coords)[:-1]]] +
                               [[list(map(float, c)) for c in list(r.coords)[:-1]] for r in q.interiors])
        for piece in (sub.get("walls") or {}).get(cls) or []:
            try:
                g = _P(piece[0], [h for h in piece[1:] if len(h) >= 3]).buffer(0).intersection(core)
            except Exception:                         # noqa: BLE001
                continue
            for q in getattr(g, "geoms", [g]):
                if q.geom_type == "Polygon" and q.area > 1.0:
                    out.append([[list(map(float, c)) for c in list(q.exterior.coords)[:-1]]] +
                               [[list(map(float, c)) for c in list(r.coords)[:-1]] for r in q.interiors])
        walls[cls] = out
    res["walls"] = walls
    # doors, with their kinds, levels and sides kept in step
    keep = [i for i, q in enumerate(res.get("doors") or []) if not ring_inside(q)]
    take = [i for i, q in enumerate(sub.get("doors") or []) if ring_inside(q)]

    def pick(key, src, idx):
        v = src.get(key) or []
        return [v[i] for i in idx if i < len(v)]
    for key in ("door_kinds", "door_levels", "door_sides"):
        a, b = pick(key, res, keep), pick(key, sub, take)
        res[key] = (a + b) if (len(a) == len(keep) and len(b) == len(take)) else []
    res["doors"] = pick("doors", res, keep) + pick("doors", sub, take)
    # the lines closing a lift lobby at its bank: evidence for either part
    res["lobby_bank_lines"] = list(res.get("lobby_bank_lines") or []) + list(sub.get("lobby_bank_lines") or [])
    res["spaces"] = list(res.get("spaces") or []) + list(sub.get("spaces") or [])
    for key in ("door_review", "shaft_review"):
        res[key] = [r_ for r_ in (res.get(key) or []) if not ring_inside(r_.get("pts") or [])] + \
                   [r_ for r_ in (sub.get(key) or []) if ring_inside(r_.get("pts") or [])]
    for key in ("windows", "cutouts", "x_columns"):
        res[key] = [w for w in (res.get(key) or []) if not ring_inside(w.get("poly") or [])] + \
                   [w for w in (sub.get(key) or []) if ring_inside(w.get("poly") or [])]
    res["x_placement"] = [r_ for r_ in (res.get("x_placement") or [])
                          if not ring_inside(_rect_ring(r_.get("bbox")))] + \
                         [r_ for r_ in (sub.get("x_placement") or []) if ring_inside(_rect_ring(r_.get("bbox")))]
    res["glass"] = [g for g in (res.get("glass") or []) if not ring_inside(g)] + \
                   [g for g in (sub.get("glass") or []) if ring_inside(g)]
    res["wall_veto"] = [v for v in (res.get("wall_veto") or []) if not ring_inside(v)] + \
                       [v for v in (sub.get("wall_veto") or []) if ring_inside(v)]
    res["wall_exclusions"] = list(res.get("wall_exclusions") or []) + list(sub.get("wall_exclusions") or [])
    res["window_conflicts"] = list(res.get("window_conflicts") or []) + list(sub.get("window_conflicts") or [])
    st = dict(res.get("stats") or {})
    for k, v in (sub.get("stats") or {}).items():
        if isinstance(v, (int, float)) and isinstance(st.get(k, 0), (int, float)):
            st[k] = st.get(k, 0) + v
    res["stats"] = st
    return res


def rotated_cluster_passes(doc, msp, units, floor, job, res, rule, rows, prep, geometry=None,
                           geometry_only=False, family_overrides=None, stats=None, log=None):
    """The floor's rotated cores, each worked out in a drawing of its own
    turned to the axes, merged into res. Returns the clusters handled."""
    from . import rotation as _rot
    from shapely.geometry import Polygon as _P, Point as _Pt
    if not rule["rotated_cores"]:
        return []
    try:
        clusters = _rot.wall_angle_clusters(job.get("segs") or [], floor["polygon"])
    except Exception:                                 # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("rotated clusters on %s", floor.get("name"))
        return []
    done = []
    for cl in clusters:
        theta = cl["theta"]
        region = cl["region"]
        try:
            reg = _P(region).buffer(0)
            origin = (reg.centroid.x, reg.centroid.y)
            mini, mmsp = _rot.rotated_subdocument(doc, msp, units, region, theta, origin)
            mini_floor = {"name": floor["name"], "layer": floor.get("layer") or "", "usage": floor.get("usage"),
                          "polygon": _rot.rotate_pts(region, -theta, origin)}
            rows_in = [r for r in rows if r.get("x") is not None and reg.contains(_Pt(float(r["x"]), float(r["y"])))]
            rows_r = _rot.rotate_rows(rows_in, -theta, origin)
            prep_m = _prepare_floors(mini, mmsp, units, [mini_floor], rule)
            job_m = _floor_job(prep_m, mini_floor, rows_r, rule, geometry=None, geometry_only=False,
                               family_overrides=family_overrides)
            job_m["rotated"] = {"theta": theta, "origin": origin}
            sub = _floor_pass(job_m)
            _unrotate_pass(sub, theta, origin)
            core_rows = [r for r in rows_in if _P(cl["core"]).contains(_Pt(float(r["x"]), float(r["y"])))]
            _merge_rotated_pass(res, sub, cl["core"], core_rows)
            # the columns the turned pass found, for the drawing
            try:
                cols_m = ((prep_m.get("columns") or {}).get("columns") or {}).get(floor["name"]) or []
                cols = ((prep.get("columns") or {}).get("columns") or {})
                core = _P(cl["core"]).buffer(0)
                keep = [c for c in (cols.get(floor["name"]) or [])
                        if not core.contains(_P([tuple(p[:2]) for p in c]).representative_point())]
                add = []
                for c in cols_m:
                    pts_ = _rot.rotate_pts([tuple(p[:2]) for p in c], theta, origin)
                    if core.contains(_P(pts_).representative_point()):
                        add.append([list(p) for p in pts_])
                cols[floor["name"]] = keep + add
                prep.setdefault("columns", {})["columns"] = cols
            except Exception:                         # noqa: BLE001
                pass
            done.append({"theta": theta, "core": cl["core"], "wall_len_mm": cl["wall_len_mm"],
                         "found": len([d for d in sub.get("merged") or []]), "whole": cl["whole"]})
            if stats is not None:
                stats["rotated_clusters"] = stats.get("rotated_clusters", 0) + 1
        except Exception:                             # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("rotated core (%s deg) on %s", theta, floor.get("name"))
    return done


def annotate_dxf(src_path, dst_path, floors, results, family_overrides=None,
                 unit_override=None, family_filter=None, rule_overrides=None,
                 user_rules=None, project_name="", geometry=None,
                 text_moves=None, geometry_only=False):
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

    # Texts moved in the Geometry tab: the original entity moves in the
    # output drawing (its position in `results` was already updated by the
    # caller, so the room search sees it where it now stands).
    if text_moves:
        stats_moves = _move_texts(doc, msp, units, text_moves)
    else:
        stats_moves = {"moved": 0, "not_movable": 0}

    prep = _prepared(src_path, unit_override, floors, rule, bool(text_moves),
                     lambda: _prepare_floors(doc, msp, units, floors, rule))
    raw_segs, seg_boxes = prep["raw_segs"], prep["seg_boxes"]
    diag_segs, x_pairs = prep["diag_segs"], prep["x_pairs"]
    detection_mode, x_clusters = prep["detection_mode"], prep["x_clusters"]
    x_lines, void_outlines = prep["x_lines"], prep["void_outlines"]
    fixtures, all_swings = prep["fixtures"], prep["all_swings"]
    circles = prep.get("circles") or []
    wall_hatches = prep.get("wall_hatches") or []
    hatches = []                   # one fill per drawn boundary, sent to back
    drawn_records = []             # every boundary as written, for validation

    stats = {
        "units": units.to_ui(),
        "boundary_method": "raycast",
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
        "traced_after_closing_gaps": 0,
        "x_diagonals_found": len(diag_segs),
        "x_pairs_found": len(x_pairs),
        "x_clusters_total": len(x_clusters),
        "texts_moved": stats_moves["moved"],
        "texts_not_movable": stats_moves["not_movable"],
    }

    # How long each stage takes (seconds): the floor passes' own stages
    # (timings in each pass's result) and the stages run here.
    _atm = {"t": time.perf_counter()}
    stats["timings"] = {}

    def _atick(name):
        now = time.perf_counter()
        stats["timings"][name] = round(stats["timings"].get(name, 0.0) + now - _atm["t"], 3)
        _atm["t"] = now

    # Each floor's rooms are found on their own - in parallel processes
    # when there are several - from the segments around that floor only.
    jobs = [_floor_job(prep, floor, results.get(floor["name"], []), rule, geometry, geometry_only,
                       family_overrides) for floor in floors]
    _atick("A0 drawing read, floors prepared")

    passes, stats["parallel_workers"] = _run_floor_passes(jobs)
    _atick("A1 floor passes (wall clock)")
    for res_ in passes:
        for k_, v_ in (res_.get("timings") or {}).items():
            stats["timings"]["  pass " + k_] = round(stats["timings"].get("  pass " + k_, 0.0) + v_, 3)
    stats["floors_recomputed"] = LAST_PASS_RUN["recomputed"]
    stats["floors_from_cache"] = LAST_PASS_RUN["floors"] - LAST_PASS_RUN["recomputed"]
    # the rotated cores of each floor (Fix 0): worked out turned to the
    # axes, put in place of what the main pass found there
    if not family_filter:
        rotated_done = []
        for floor, res, job_ in zip(floors, passes, jobs):
            rotated_done += [dict(c, floor=floor["name"]) for c in
                             rotated_cluster_passes(doc, msp, units, floor, job_, res, rule,
                                                    results.get(floor["name"], []), prep,
                                                    family_overrides=family_overrides, stats=stats)]
        if rotated_done:
            stats["rotated_cores"] = rotated_done
    _atick("A2 rotated cores")

    traced_any = False
    n_before = len(msp)                # what is drawn below: the element engine reads it
    for floor, res, job_ in zip(floors, passes, jobs):
        floor_poly = [tuple(p) for p in floor["polygon"]]
        floor_name = floor["name"]
        floor_rules = rules_for_usage(floor.get("usage"))
        merged = res["merged"]
        text_status = res["text_status"]
        floor_family_overrides = res["floor_family_overrides"]

        # What the rooms and the doors settle together (Test_2): the stair's
        # open well, a shaft's room on AI_MEP and up to its door line, a
        # doorless lift on a machine room - before anything is drawn.
        def _fam_name(d, _ov=floor_family_overrides, _fr=floor_rules):
            fam_ = None
            if d.get("family_forced"):
                fam_ = family_by_name(d["family_forced"], _FAMILY_RULES)
            sr_ = d.get("sr_no")
            if fam_ is None and sr_ is not None and sr_ in _ov:
                fam_ = family_by_name(_ov[sr_], _FAMILY_RULES)
            if fam_ is None:
                fam_ = family_for_label(d.get("label"), _fr)
            return fam_["name"]
        if not family_filter:
            try:
                rooms_and_doors(merged, res.get("doors") or [], _fam_name, rule, segs=job_.get("segs"),
                                user_set=lambda d, _ov=floor_family_overrides: d.get("sr_no") in _ov,
                                stats=stats)
            except Exception:                         # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception("rooms and doors on %s", floor_name)
            # the wall left between the rooms, doors and walls
            if rule["wall_gaps_filled"] and res.get("walls") is not None:
                try:
                    from shapely.geometry import Polygon as _GP
                    cover_ = [g for g in (_merged_geom(d) for d in merged) if g is not None]
                    for q in list(res.get("doors") or []) + [w.get("poly") for w in (res.get("windows") or [])] + \
                            [c.get("poly") for c in (res.get("cutouts") or [])] + list(res.get("glass") or []) + \
                            list(((prep.get("columns") or {}).get("columns") or {}).get(floor_name) or []):
                        try:
                            if q is not None and len(q) >= 3:
                                cover_.append(_GP([tuple(p[:2]) for p in q]).buffer(0))
                        except (ValueError, TypeError, IndexError):
                            continue
                    fb_ = _floor_clip_box(floor_poly)
                    isl_ = [h for h in (prep.get("wall_hatch_islands") or [])
                            if fb_[0] - 500 <= min(p[0] for p in h[0]) and max(p[0] for p in h[0]) <= fb_[2] + 500
                            and fb_[1] - 500 <= min(p[1] for p in h[0]) and max(p[1] for p in h[0]) <= fb_[3] + 500]
                    lbl_ = [(r.get("x"), r.get("y")) for r in (job_.get("rows") or [])
                            if r.get("x") is not None and r.get("y") is not None]
                    cols_ = []
                    for q in list(((prep.get("columns") or {}).get("columns") or {}).get(floor_name) or []):
                        try:
                            cols_.append(_GP([tuple(p[:2]) for p in q]).buffer(0))
                        except (ValueError, TypeError, IndexError):
                            continue
                    drs_ = []
                    for q in res.get("doors") or []:
                        try:
                            drs_.append(_GP([tuple(p[:2]) for p in q]).buffer(0))
                        except (ValueError, TypeError, IndexError):
                            continue
                    gaps_ = wall_gap_pieces(floor_poly, (res["walls"] or {}).get(LN.WALL) or [], cover_, isl_, lbl_,
                                            columns=cols_, doors=drs_)
                    if gaps_:
                        res["walls"] = dict(res["walls"])
                        res["walls"][LN.WALL] = list(res["walls"].get(LN.WALL) or []) + wall_rings(gaps_)
                        stats["wall_gaps_filled"] = stats.get("wall_gaps_filled", 0) + len(gaps_)
                        stats["wall_gaps_filled_sqm"] = round(stats.get("wall_gaps_filled_sqm", 0.0) +
                                                              sum(g.area for g in gaps_) / 1e6, 3)
                except Exception:                     # noqa: BLE001
                    import logging
                    logging.getLogger(__name__).exception("wall gaps on %s", floor_name)
            # the rooms, the X-marked openings and the walls settled against
            # each other, last: no room over a wall, no wall over an X, the
            # walls joined at their junctions (settle_rooms_and_walls)
            if res.get("walls") is not None and (rule["rooms_clear_of_walls"] or rule["x_cell_never_wall"]
                                                 or rule["wall_junctions_closed"]):
                try:
                    from shapely.geometry import Polygon as _SP
                    items_ = []
                    for d in merged:
                        g = _merged_geom(d)
                        if g is None:
                            continue
                        fam_ = _fam_name(d)
                        items_.append({"geom": g, "g0": g, "d": d, "x": d.get("source") == "X", "fam": fam_,
                                       "lift": d.get("source") != "X" and fam_ == "Lift", "split": False,
                                       "keep": bool(d.get("_balcony_grown_sqm")) or d.get("source") in ("V", "G"),
                                       "pt": (d["cx"], d["cy"]) if d.get("cx") is not None and d.get("cy") is not None else None})
                    other_ = []
                    for q in list(res.get("doors") or []) + [w.get("poly") for w in (res.get("windows") or [])] + \
                            [c.get("poly") for c in (res.get("cutouts") or [])] + \
                            list(((prep.get("columns") or {}).get("columns") or {}).get(floor_name) or []):
                        try:
                            if q is not None and len(q) >= 3:
                                other_.append(_SP([tuple(p[:2]) for p in q]).buffer(0))
                        except (ValueError, TypeError, IndexError):
                            continue
                    lbl2_ = [(r.get("x"), r.get("y")) for r in (job_.get("rows") or [])
                             if r.get("x") is not None and r.get("y") is not None]
                    doors_g = []
                    for q in res.get("doors") or []:
                        try:
                            if q is not None and len(q) >= 3:
                                doors_g.append(_SP([tuple(p[:2]) for p in q]).buffer(0))
                        except (ValueError, TypeError, IndexError):
                            continue
                    walls_ = settle_rooms_and_walls(items_, (res["walls"] or {}).get(LN.WALL) or [], rule,
                                                    stats=stats, cover=other_, plate=floor_poly, label_pts=lbl2_,
                                                    doors=doors_g)
                    if walls_ is not None:
                        res["walls"] = dict(res["walls"])
                        res["walls"][LN.WALL] = walls_
                    gone_ = set()
                    for it in items_:
                        if it.get("twin_of") is not None:
                            continue                    # (Step 3 keeps the labelled piece only)
                        if it.get("drop"):
                            gone_.add(id(it["d"]))
                            into = it.get("merged_into")
                            sr_ = it["d"].get("sr_no")
                            if into is not None:
                                # joined to another piece of the same room: its
                                # label is absorbed there
                                for s_ in [sr_] + list(it["d"].get("absorbed_srs") or []):
                                    if s_ is not None:
                                        into["d"].setdefault("absorbed_srs", []).append(s_)
                                        if s_ in text_status:
                                            text_status[s_] = "merged"
                            elif sr_ is not None and sr_ in text_status:
                                text_status[sr_] = "in_wall"
                        elif it["geom"] is not it["g0"]:
                            _set_merged_geom(it["d"], it["geom"])
                    if gone_:
                        merged[:] = [d for d in merged if id(d) not in gone_]
                except Exception:                     # noqa: BLE001
                    import logging
                    logging.getLogger(__name__).exception("rooms and walls on %s", floor_name)
        _atick("A3 rooms and doors, wall gaps, settle")
        traced_any = traced_any or res["traced"]
        for k, v in res["stats"].items():
            stats[k] = stats.get(k, 0) + v
        # lines the wall rules left out, and rooms grown to their stated size
        stats.setdefault("wall_exclusions", []).extend(res.get("wall_exclusions") or [])
        if res.get("wall_trace_pct") is not None:
            stats.setdefault("wall_trace_hatched_pct_by_floor", {})[floor_name] = res["wall_trace_pct"]
        stats.setdefault("wall_review_items", []).extend(res.get("wall_trace_review") or [])
        stats.setdefault("window_conflicts", []).extend(res.get("window_conflicts") or [])
        if res.get("wall_veto"):
            stats.setdefault("wall_veto", {})[floor_name] = res["wall_veto"]
        if res.get("x_placement"):
            stats.setdefault("x_placement", {})[floor_name] = res["x_placement"]
        if res.get("locked"):
            stats.setdefault("locked_by_floor", {})[floor_name] = res["locked"]
        if res.get("spaces"):
            stats.setdefault("spaces_by_floor", {})[floor_name] = res["spaces"]
        stats.setdefault("label_notes_by_floor", {})[floor_name] = {
            str(k): v for k, v in (res.get("label_notes") or {}).items()}
        stats.setdefault("stated_grown", []).extend(
            {"floor": floor_name, "label": g[0], "stated": g[1], "before_sqm": g[2], "after_sqm": g[3]}
            for g in res.get("grown") or [])
        if res.get("size_flags"):
            stats.setdefault("size_flags_by_floor", {})[floor_name] = {
                int(k): list(v) for k, v in res["size_flags"].items()}

        # Every door belongs to one room, and its line is on that room's
        # inner face of the wall (2026-10-17): first no room reaches into a
        # door opening (it stops at its own face - an X of the X sequence
        # is left as it is), then each door's room is decided once
        # (elements.door_owners: a lift or shaft, a room, a staircase, a
        # lobby, a corridor, the common floor - the higher wins; two like
        # rooms: its only door, its swing, fewer doors). Its sides are kept
        # with it, the room it belongs to first; its line is that room's
        # inner wall trace across the opening (door_trace, below).
        door_owner_sides = door_line_list = door_dirs_ = None
        if DRAW_DOORS and not family_filter and res.get("doors"):
            try:
                from . import elements as _el
                quads_ = [q for q in res["doors"] if q is not None and len(q) >= 3]
                if len(quads_) == len(res["doors"]):
                    live_ = [d for d in merged if _fam_name(d) != _IGNORE_FAMILY["name"]]
                    geoms_ = [_merged_geom(d) for d in live_]
                    new_, cut_sqm = _el.rooms_out_of_doorways(
                        geoms_, quads_, skip=[d.get("source") in ("X", "V") for d in live_])
                    gone_ = set()
                    for d, g0, g1 in zip(live_, geoms_, new_):
                        if g1 is None and g0 is not None:
                            gone_.add(id(d))
                            for s_ in [d.get("sr_no")] + list(d.get("absorbed_srs") or []):
                                if s_ is not None and s_ in text_status:
                                    text_status[s_] = "in_wall"
                        elif g1 is not None and g1 is not g0:
                            _set_merged_geom(d, g1)
                            stats["rooms_cut_at_doorways"] = stats.get("rooms_cut_at_doorways", 0) + 1
                    if gone_:
                        merged[:] = [d for d in merged if id(d) not in gone_]
                        live_ = [d for d in live_ if id(d) not in gone_]
                        stats["rooms_in_doorways_dropped"] = stats.get("rooms_in_doorways_dropped", 0) + len(gone_)
                    if cut_sqm:
                        stats["rooms_cut_at_doorways_sqm"] = round(stats.get("rooms_cut_at_doorways_sqm", 0.0) + cut_sqm, 3)
                    rooms_o, ranks_o, names_o, items_o = [], [], [], []
                    for d in live_:
                        g = _merged_geom(d)
                        if g is None or g.is_empty or g.area <= 0:
                            continue
                        fam_ = _fam_name(d)
                        lab_ = " ".join(str(d.get("label") or "").split())
                        rooms_o.append(g)
                        ranks_o.append(_el.door_room_rank(lab_, fam_))
                        names_o.append(lab_)
                        items_o.append(d)
                    own_ = _el.door_owners(quads_, rooms_o, ranks_o, job_.get("door_arcs") or ())
                    # The line on the room's inner wall trace (2026-10-17,
                    # later): read from the walls either side of the opening
                    # (elements.door_trace) - the inner edge of the jambs'
                    # walls' ends; the room squared up to it (its own room
                    # out to the line or back to it, no other across it).
                    door_dirs_ = [door_room_vector(q, o[0]) for q, o in zip(quads_, own_)]
                    if _el.DOOR_TRACE:
                        from shapely.geometry import Polygon as _TP
                        wall_g_ = _wall_union((res.get("walls") or {}).get(LN.WALL) or [])
                        tw_ = [wall_g_] if wall_g_ is not None else []
                        for ring in list(((prep.get("columns") or {}).get("columns") or {}).get(floor_name) or []):
                            try:
                                tw_.append(_TP([tuple(p[:2]) for p in ring]).buffer(0))
                            except (ValueError, TypeError, IndexError):
                                continue
                        ts_ = []
                        for ring in [w.get("poly") for w in (res.get("windows") or [])] + \
                                [c.get("poly") for c in (res.get("cutouts") or [])] + list(res.get("glass") or []):
                            try:
                                if ring is not None and len(ring) >= 3:
                                    ts_.append(_TP([tuple(p[:2]) for p in ring]).buffer(0))
                            except (ValueError, TypeError, IndexError):
                                continue
                        solid_ = _el.trace_solid(tw_, ts_, quads_)
                        tr_ = _el.door_traces(quads_, [o[0] for o in own_], solid_)
                        doors_rt, door_lines_ = [], [None] * len(quads_)
                        for k_, ((side_, who_, _how), (t_, why_, jog_)) in enumerate(zip(own_, tr_)):
                            if t_ is None:
                                doors_rt.append(None)
                                if side_ is not None and why_:
                                    key_ = "doors_trace_none_" + why_.replace(" ", "_")
                                    stats[key_] = stats.get(key_, 0) + 1
                                    ax_ = _el.door_axes(quads_[k_])
                                    if ax_ is not None:
                                        stats.setdefault("door_trace_review", []).append(
                                            {"floor": floor_name, "at": [round(ax_[0]), round(ax_[1])],
                                             "why": why_ + ": the line stays on the room's face of the opening"})
                                continue
                            key_ = "doors_trace_" + why_.replace(" ", "_")
                            stats[key_] = stats.get(key_, 0) + 1
                            if jog_ is not None and jog_ > _el.DOOR_TRACE_JOG_MM:
                                ax_ = _el.door_axes(quads_[k_])
                                stats["doors_trace_jambs_apart"] = stats.get("doors_trace_jambs_apart", 0) + 1
                                stats.setdefault("door_trace_review", []).append(
                                    {"floor": floor_name, "at": [round(ax_[0]), round(ax_[1])],
                                     "why": "the jambs' walls end %d mm apart: the line at the nearer end" % round(jog_)})
                            door_lines_[k_] = _el.trace_line(quads_[k_], t_)
                            doors_rt.append((who_, side_, t_))
                        new2_, grown_, cut2_ = _el.rooms_to_door_lines(
                            rooms_o, quads_, doors_rt, skip=[d.get("source") in ("X", "V") for d in items_o])
                        for d, g0, g1 in zip(items_o, rooms_o, new2_):
                            if g1 is not None and g1 is not g0:
                                _set_merged_geom(d, g1)
                        rooms_o = [g1 if g1 is not None else g0 for g0, g1 in zip(rooms_o, new2_)]
                        if grown_:
                            stats["rooms_out_to_door_lines_sqm"] = round(stats.get("rooms_out_to_door_lines_sqm", 0.0) + grown_, 3)
                        if cut2_:
                            stats["rooms_back_to_door_lines_sqm"] = round(stats.get("rooms_back_to_door_lines_sqm", 0.0) + cut2_, 3)
                        door_line_list = door_lines_
                    sides_ = [(None, None)] * len(quads_)
                    if rooms_o:
                        from shapely.strtree import STRtree as _OT
                        t_ = _OT(rooms_o)
                        sides_ = [_el.door_sides(q, rooms_o, t_) for q in quads_]
                    nm_ = lambda i: "(common floor)" if i is None else names_o[i]
                    door_owner_sides = [o[0] for o in own_]
                    old_sides = list(res.get("door_sides") or [])
                    new_sides = []
                    for k_, ((side_, who_, how_), (a_, b_)) in enumerate(zip(own_, sides_)):
                        stats["doors_owner_" + how_.replace(" ", "_")] = \
                            stats.get("doors_owner_" + how_.replace(" ", "_"), 0) + 1
                        if side_ is None:
                            new_sides.append(old_sides[k_] if k_ < len(old_sides) else ("", ""))
                        else:
                            other_ = b_ if side_ == 0 else a_
                            new_sides.append((nm_(who_), nm_(other_)))
                    res["door_sides"] = new_sides
            except Exception:                         # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception("door owners on %s", floor_name)
                door_owner_sides = door_line_list = door_dirs_ = None

        # Outline floor + draw all final boundaries.
        # Floor outline goes on the legacy ROOM_BOUNDARY layer.
        # Each room boundary goes on its family's <FAMILY>_BOUNDARY layer
        # with the family's configured color.
        n_floor0 = len(msp)               # this floor's drawing (rule boundary_on_evidence reads it back)
        x_outlines = set()                # the X sequence's outlines: identified by their X, not checked
        outline = [(units.from_mm(px), units.from_mm(py)) for px, py in floor_poly]
        outline_e = msp.add_lwpolyline(outline + [outline[0]],
                                       dxfattribs={"layer": ANNOTATE_LAYER,
                                                   "color": ANNOTATE_COLOR})
        # Name the outline after the floor chosen in Step 2, so Step 4 can
        # report each floor of a multi-floor sheet under the same name.
        _tag_floor(doc, outline_e, floor_name)
        _atick("A4 door owners")
        # The walls (AI_WALL): never rooms, never in an area.
        if res.get("walls") and not family_filter:
            draw_walls(doc, msp, units, res["walls"])
        # The door openings in the walls: never rooms, never walls (the
        # corrected drawings mark every opening this way) - primary ones
        # (between two spaces) on AI_P_DOORS, secondary ones (inside one
        # room) on AI_S_DOORS.
        if DRAW_DOORS and not family_filter and res.get("doors"):
            n_ = draw_doors(doc, msp, units, res["doors"], kinds=res.get("door_kinds"),
                            levels=res.get("door_levels"), sides=res.get("door_sides"),
                            rooms=[g for g in (_merged_geom(d) for d in merged) if g is not None],
                            owners=door_owner_sides, lines=door_line_list, dirs=door_dirs_)
            stats["doors_drawn"] = stats.get("doors_drawn", 0) + sum(n_.values())
            for lev_, c_ in n_.items():
                stats["doors_drawn_" + lev_] = stats.get("doors_drawn_" + lev_, 0) + c_
        # the doubtful door openings (rule doors_need_evidence): not drawn
        # as doors, outlined on AI_REVIEW with why
        if DRAW_DOORS and not family_filter and res.get("door_review"):
            draw_door_review(doc, msp, units, res["door_review"])
        # the shafts whose compartment could not be traced from their X
        # (rule shaft_compartment): the X as drawn, outlined on AI_REVIEW
        if not family_filter and res.get("shaft_review"):
            stats["shaft_review_drawn"] = stats.get("shaft_review_drawn", 0) + \
                draw_door_review(doc, msp, units, res["shaft_review"])
        # Glass partitions and glazing, on AI_GLASS: never walls, never rooms.
        if res.get("glass") and not family_filter:
            stats["glass_drawn"] = stats.get("glass_drawn", 0) + draw_glass(doc, msp, units, res["glass"])
        # The hanging walls on this floor (an object drawn under them): not
        # walls - the rooms ran under them - shown on AI_HANGING_WALL.
        floor_hanging = []
        if prep.get("hanging_walls"):
            from shapely.geometry import Polygon as _HP
            _fp = _HP(floor_poly).buffer(0)
            for hw in prep["hanging_walls"]:
                hg = _HP(hw["pts"], hw.get("holes") or []).buffer(0)
                if not hg.is_empty and hg.intersects(_fp) and hg.intersection(_fp).area >= 0.5 * hg.area:
                    floor_hanging.append((hw, hg))
        if floor_hanging and not family_filter:
            draw_hanging_walls(doc, msp, units, [hw for hw, _g in floor_hanging])
        for hw, hg in floor_hanging:
            stats["hanging_walls_count"] = stats.get("hanging_walls_count", 0) + 1
            stats["hanging_walls_area_sqm"] = round(stats.get("hanging_walls_area_sqm", 0) + hg.area / 1e6, 3)
            stats.setdefault("hanging_wall_items", []).append(
                {"floor": floor_name, "evidence": hw.get("evidence"), "layer": hw.get("layer"),
                 "area_sqm": round(hg.area / 1e6, 3), "pts": [[round(x), round(y)] for x, y in hw["pts"]]})
        # The columns the geometry engine finds (the same finder), on
        # AI_COLUMN: outline, hatch, "C".
        if not family_filter:
            for pts in ((prep.get("columns") or {}).get("columns") or {}).get(floor_name) or []:
                draw_column(doc, msp, units, pts)
                stats["columns_drawn"] = stats.get("columns_drawn", 0) + 1
        # Windows found in the walls (X marks that are not shafts).
        if res.get("windows") and not family_filter:
            if WINDOW_LAYER not in doc.layers:
                doc.layers.new(WINDOW_LAYER, dxfattribs={"color": WINDOW_COLOR})
            for w in res["windows"]:
                pts = [(units.from_mm(px), units.from_mm(py)) for px, py in w["poly"]]
                e_ = msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": WINDOW_LAYER, "color": 256})
                if w.get("x_tag"):
                    tag_x(doc, e_, *w["x_tag"])
        # Cut-outs (the X sequence's step 5): drawn with their X on
        # AI_CUTOUT - never rooms, never named by a room label.
        if res.get("cutouts") and not family_filter:
            for c in res["cutouts"]:
                draw_cutout(doc, msp, units, c["poly"], c.get("name"), x_tag=c.get("x_tag"))
                stats["cutouts_drawn"] = stats.get("cutouts_drawn", 0) + 1
                stats.setdefault("cutouts", []).append(
                    {"floor": floor_name, "bbox": c.get("bbox"), "why": c.get("why")})
        # A column's cross (the X sequence's step 6) on a column the column
        # finder did not draw above: the column, on AI_COLUMN.
        if res.get("x_columns") and not family_filter:
            from shapely.geometry import Polygon as _XCP
            have_ = []
            for q in ((prep.get("columns") or {}).get("columns") or {}).get(floor_name) or []:
                try:
                    have_.append(_XCP([tuple(p[:2]) for p in q]).buffer(0))
                except (ValueError, TypeError, IndexError):
                    continue
            for c in res["x_columns"]:
                try:
                    cg_ = _XCP(c["poly"]).buffer(0)
                except (ValueError, TypeError):
                    continue
                if any(h_.intersects(cg_) and h_.intersection(cg_).area >= 0.5 * cg_.area for h_ in have_):
                    continue                          # the column is drawn already
                draw_column(doc, msp, units, c["poly"], x_tag=c.get("x_tag"))
                stats["x_columns_drawn"] = stats.get("x_columns_drawn", 0) + 1
        # The user's own rules run last, on the finished boundaries: they
        # reshape, drop or refile what the drawing pass decided, and they
        # are deliberately the last word so their effect is visible rather
        # than tangled into the search.
        _atick("A5 walls, doors, columns, windows drawn")
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

        if floor_hanging:
            # how much of the hanging walls the rooms took in
            try:
                from shapely.geometry import Polygon as _HP
                from shapely.ops import unary_union as _hu
                ru = _hu([_HP(d["poly"], [h for h in d.get("holes") or () if len(h) >= 3]).buffer(0) for d in merged
                          if d.get("poly") and len(d["poly"]) >= 3])
                for _hw, hg in floor_hanging:
                    stats["hanging_walls_to_rooms_sqm"] = round(
                        stats.get("hanging_walls_to_rooms_sqm", 0) + hg.intersection(ru).area / 1e6, 3)
            except Exception:                         # noqa: BLE001
                pass
        for d in merged:
            mn_x, mn_y, mx_x, mx_y = d["bbox"]
            fam = None
            sr = d.get("sr_no")
            if d.get("family_forced"):
                fam = family_by_name(d["family_forced"], _FAMILY_RULES)
            if fam is None and sr is not None and sr in floor_family_overrides:
                fam = family_by_name(floor_family_overrides[sr], _FAMILY_RULES)
            if fam is None:
                fam = family_for_label(d["label"], floor_rules)
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
            if outline_ent is not None:
                conf = _boundary_confidence(d, fam["name"])
                if conf is not None:
                    _ck = "confidence_" + _confidence_band(conf[0]).lower()
                    stats[_ck] = stats.get(_ck, 0) + 1
                _tag_room(doc, outline_ent, d.get("source"), d.get("label"), conf, cell=bool(d.get("cell")))
                if d.get("x_class"):
                    tag_x(doc, outline_ent, d["x_class"], d.get("x_step"), d.get("x_why") or "")
                    x_outlines.add(outline_ent.dxf.handle)
            if ANNOTATE_HATCH:
                hatches.append(_hatch_boundary(msp, outline_ent, hole_ents,
                                               layer=layer_name, color=color))
            # Step 4 names each polygon from the text standing inside it,
            # so the label has to be inside the outline it titles. A label
            # sitting on a wall line traces from a free cell beside it and
            # can end up just outside its own room.
            lx, ly = d["cx"], d["cy"]
            if not _inside_boundary(d, lx, ly):
                if d.get("poly"):
                    lx, ly = _interior_point(d["poly"], d.get("holes"))
                else:
                    # A label beside its box (a shaft or toilet named from
                    # outside) is written at the box centre, so Step 4
                    # still finds it inside.
                    lx, ly = (mn_x + mx_x) / 2.0, (mn_y + mx_y) / 2.0
            _add_label(msp, d["label"], lx, ly,
                        layer=layer_name, color=color, units=units)
            drawn_records.append({
                "floor": floor_name, "layer": layer_name, "family": fam["name"],
                "label": d["label"], "label_xy": (lx, ly),
                "pts": (list(d["poly"]) if d.get("poly") else
                        [(mn_x, mn_y), (mx_x, mn_y), (mx_x, mx_y), (mn_x, mx_y)]),
                "cutout_of": d.get("cutout_of")})
            # every boundary whose label states a size: drawn / stated
            want = declared_size(d["label"] or "")
            if want:
                rec = drawn_records[-1]
                drawn_sqm = max(0.0, shoelace_area_sqm(rec["pts"]) -
                                sum(shoelace_area_sqm(hh) for hh in (d.get("holes") or ()) if len(hh) >= 3))
                ratio = drawn_sqm / (want[0] * want[1] / 1e6)
                stats.setdefault("size_check", []).append({
                    "floor": floor_name, "label": " ".join(str(d["label"]).split()), "layer": layer_name,
                    "family": fam["name"], "stated": "{0:g} x {1:g}".format(*want),
                    "stated_sqm": round(want[0] * want[1] / 1e6, 2), "drawn_sqm": round(drawn_sqm, 2),
                    "ratio": round(ratio, 2),
                    "flag": "" if SIZE_CHECK_RANGE[0] <= ratio <= SIZE_CHECK_RANGE[1] else
                    ("smaller than stated" if ratio < SIZE_CHECK_RANGE[0] else "larger than stated")})

        # Every room's sides, as drawn, on evidence (rule
        # boundary_on_evidence): a stretch standing on no wall, facade,
        # room, primary door or column is marked on AI_REVIEW and counted.
        if rule["boundary_on_evidence"] and not family_filter:
            try:
                ev_summ, ev_items = boundary_evidence_check(list(msp)[n_floor0:], units, floor_poly,
                                                            skip=x_outlines,
                                                            extra_lines=res.get("lobby_bank_lines") or ())
                stats.setdefault("boundary_evidence_by_floor", {})[floor_name] = ev_summ
                for k_ in ("checked_m", "evidence_m", "opening_m", "no_evidence_m"):
                    stats["boundary_" + k_] = round(stats.get("boundary_" + k_, 0.0) + ev_summ[k_], 2)
                stats["rooms_off_evidence"] = stats.get("rooms_off_evidence", 0) + ev_summ["rooms_off"]
                if ev_items:
                    stats["boundary_no_evidence_marked"] = (stats.get("boundary_no_evidence_marked", 0)
                                                           + draw_door_review(doc, msp, units, ev_items))
                    stats.setdefault("boundary_evidence_review", []).extend(
                        {"floor": floor_name, "layer": it_["layer"], "len_mm": it_["len_mm"],
                         "at": [round(sum(p_[0] for p_ in it_["pts"]) / len(it_["pts"])),
                                round(sum(p_[1] for p_ in it_["pts"]) / len(it_["pts"]))]}
                        for it_ in ev_items)
            except Exception:                         # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception("boundary evidence on %s", floor_name)

        # Persist this floor's text statuses into the global stats payload.
        # This runs after the draw loop so a boundary held back by
        # family_filter reports as 'filtered' rather than 'drawn'.
        stats.setdefault("text_status_by_floor", {})[floor_name] = dict(text_status)

    stats["boundary_method"] = "trace" if traced_any else "raycast"

    # The element identification engine (elementid): every door, window and
    # cut-out, every shaft, lift, staircase, void and lobby drawn above is
    # classified again from the evidence round it, with a confidence score;
    # each is tagged (XDATA) and the doubtful ones are marked on AI_REVIEW.
    # It changes no area.
    if rule["element_scores"] and not family_filter:
        try:
            from . import elementid as _eid
            if _eid.ENABLED:
                recs_, summ_ = _eid.run_on_annotation(
                    doc, msp, units, floors, jobs, passes, list(msp)[n_before:],
                    lambda layer_: family_from_layer(layer_, _FAMILY_RULES)["name"])
                stats["elements"] = [_eid.public(r_) for r_ in recs_]
                stats["elements_summary"] = summ_
        except Exception:                             # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("element identification")

    # The project name typed in Step 1 travels in the drawing's own header
    # ($PROJECTNAME), so it is still there when the file comes back for
    # Step 4 after editing in CAD.
    if project_name:
        try:
            doc.header["$PROJECTNAME"] = str(project_name)[:120]
        except Exception:                             # noqa: BLE001
            pass
    _atick("A6 boundaries drawn, user rules, element engine")
    # Check what was written, the way a CAD reviewer would (validate.py).
    try:
        from .validate import validate_boundaries
        stats["validation"] = validate_boundaries(
            drawn_records, stats.get("text_status_by_floor"), results,
            X_VOID_AREA_THRESHOLD, _size_ok)
    except Exception as exc:                          # noqa: BLE001
        stats["validation"] = {"error": str(exc)}
    _send_to_back(msp, hatches)
    stats["hatched_boundaries"] = sum(1 for h in hatches if h is not None)
    set_insunits(doc, units)
    doc.saveas(dst_path)
    # the next steps read this drawing back: they get this one instead of
    # parsing the file again (dxfio.remember_written)
    try:
        from .dxfio import remember_written
        remember_written(doc, dst_path)
    except Exception:                                 # noqa: BLE001
        pass
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
    specific than either. An X mark is the X sequence's (2026-10-16): no
    rule reshapes, drops or refiles it.
    """
    items = []
    fixed = []
    for d in merged:
        if d.get("x_class") or d.get("source") == "V":
            fixed.append(d)
            continue
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
    return fixed + out, report


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
                       return_polygons=False, loaded=None, floors=None):
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
    #    `floors` - the plates picked in Step 2, [{"name", "polygon"}] in
    #    mm - are used as they are, and only they: a drawing generated by
    #    this app needs no floor layer chosen again.
    candidates, tagged = [], []
    for e in ([] if floors else msp.query("LWPOLYLINE POLYLINE")):
        pts = _polyline_points(e, units)
        if len(pts) < 3: continue
        tag = floor_tag(e)
        if tag:
            tagged.append((tag, pts, shoelace_area_sqm(pts)))
        if (e.dxf.layer or "0") != floor_layer_name: continue
        if not (_is_closed(e) or tag): continue
        candidates.append((shoelace_area_sqm(pts), pts, tag))
    candidates.sort(key=lambda x: -x[0])
    given, floors = floors, []
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
    if given:
        floors = [{"pts": [tuple(p) for p in g["polygon"]],
                   "area": shoelace_area_sqm([tuple(p) for p in g["polygon"]]),
                   "name": g.get("name") or "Floor {0}".format(i),
                   "perimeter": polygon_perimeter_m([tuple(p) for p in g["polygon"]]),
                   "picked_levels": list(g.get("levels") or []),
                   "layer": g.get("layer") or floor_layer_name}
                  for i, g in enumerate(given, start=1) if len(g.get("polygon") or []) >= 3]
    # A floor outline on a slab-projection layer runs to the slab edge; the
    # gross "inc glass" runs to the outer face of the facade (facade.py).
    glass_face = {}
    try:
        from .facade import floors_glass_face
        for f in floors:
            f.setdefault("layer", floor_layer_name)
        glass_face = floors_glass_face(doc, msp, units, floors)
    except Exception:                                 # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("gross to the facade")
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
        if not LN.is_void_layer(e.dxf.layer or "0"): continue
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
        if not LN.is_room_layer(e_layer): continue     # AI_<FAMILY> or <FAMILY>_BOUNDARY
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
        src, _lab = room_tag(e)
        conf, conf_why = room_confidence_tag(e)
        room_polys.append({"pts": pts, "area_sqm": max(0.0, a - deducted),
                           "confidence": conf, "confidence_why": conf_why,
                           "gross_area_sqm": a, "void_area_sqm": deducted,
                           "void_count": n_voids, "layer": e_layer,
                           "perimeter_m": polygon_perimeter_m(pts),
                           "source": src, "opening": src == "X"})

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
            "No closed polylines found on any room layer (AI_TOILET, "
            "AI_SHAFT ... or the older TOILET_BOUNDARY, SHAFT_BOUNDARY). Draw "
            "room boundaries in Step 3 first, or put them on such a layer in "
            "CAD."
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
    # The floors ticked in Step 2 decide what a plate stands for; a floor
    # struck out there (taken from another plate) is not counted here.
    for f in floors:
        if f.get("picked_levels"):
            f["levels"] = f["picked_levels"]

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
                               "distance": d,
                               "own": (tr.layer or "").upper() == rp["layer"].upper()})
        # Step 3 writes each boundary's label on the boundary's own layer.
        # When one stands inside, it is the name.
        own = [c for c in candidates if c["own"]]
        if own:
            candidates = own
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
            # how the boundary was made (X-marked opening, traced, geometry)
            "source": rp.get("source"),
            "opening": bool(rp.get("opening")),
            # how far the boundary can be trusted (confidence.py)
            "confidence": rp.get("confidence"),
            "confidence_band": (_confidence_band(rp["confidence"]) if rp.get("confidence") is not None else ""),
            "confidence_why": rp.get("confidence_why") or "",
        })

    # Which boundary each one stands inside, if any: a cut-out drawn inside
    # its shaft, a store inside a larger room. The Area Statement uses it
    # so a cut-out inside a shaft is not deducted twice.
    _mark_within(rooms_out, room_polys)
    _mark_neighbours(rooms_out, room_polys)
    _mark_exits(rooms_out, room_polys, msp, units)

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
        if r.get("within_layer"):
            r["within_family"] = family_from_layer(r.pop("within_layer"),
                                                   _FAMILY_RULES)["name"]

    families_out = _families_of(rooms_out)

    # Cut-outs in the walls (AI_CUTOUT): measured and listed as cut-outs,
    # never rooms, never in a room's area.
    cutouts_out = []
    for e in msp.query("LWPOLYLINE POLYLINE"):
        if not _is_closed(e) or not LN.is_cutout_layer(e.dxf.layer or ""):
            continue
        cpts = _polyline_points(e, units)
        if len(cpts) < 3:
            continue
        ca = shoelace_area_sqm(cpts)
        if ca <= 0:
            continue
        ix, iy = _interior_point(cpts)
        nm = next((" ".join(tr.text.split()) for tr in text_records
                   if (tr.layer or "").upper() == LN.CUTOUT and tr.text.strip()
                   and _point_in_polygon(tr.x, tr.y, cpts)), CUTOUT_NAME)
        cutouts_out.append({"floor": next((f["name"] for f in floors if _point_in_polygon(ix, iy, f["pts"])),
                                          "Outside floor outline"),
                            "name": nm, "area_sqm": round(ca, 3), "layer": LN.CUTOUT})

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
        "cutouts": cutouts_out,
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
            "cutouts_area_sqm": round(sum(c["area_sqm"] for c in cutouts_out
                                          if c["floor"] == f["name"]), 2),
            "title": f.get("title", ""),
            "levels": f.get("levels", []),
            "tower": f.get("tower"),
            "layer": f.get("layer"),
            "glass_face_area_sqm": (round(glass_face[f["name"]]["area_sqm"], 2)
                                    if f["name"] in glass_face else None),
        } for f in floors],
    }
    if return_polygons:
        return result, room_polys
    return result


def _title_blocks(text_records):
    """Plan titles as the drawing shows them, one per title.

    An MTEXT title holds all its lines in one entity:
        "LOWER ZONE TYPICAL FLOOR PLAN / (8th, 12th & 16th FLOORS)".
    The same title in TEXT is two entities, one line each, and neither
    line alone is a floor title - the first names no floor, the second has
    no FLOOR word ("(2ND, 3RD & 5th FLOORS)"). Lines stacked under a title
    line - about the same height, within a line or two below it, roughly
    in line horizontally, and not a "... PLAN" title of their own - are
    joined to it. Returns (text, x, y, height, source) per candidate; the
    single lines stay in as well, so a title in one line is unchanged.
    """
    recs = []
    for r in text_records:
        t = clean_title(r.text)
        if not t:
            continue
        u = t.upper()
        recs.append((r, t, u, bool(TITLE_WORDS.search(u)), bool(parse_levels(t))))
    out = [(t, r.x, r.y, r.height, r.source)
           for r, t, u, words, lv in recs if words and lv]
    heads = [x for x in recs if x[3] and x[0].height > 0]
    tails = [x for x in recs if x[4] and not re.search(r"\bPLAN\b", x[2])
             and x[0].height > 0]
    for r, t, u, _w, _lv in heads:
        h = r.height
        reach = max(len(t) * h * 0.9, 10 * h)
        lines, last = [t], r
        while True:
            nxt = None
            for o, ot, _ou, _ow, _olv in tails:
                if o is r or o is last or ot in lines:
                    continue
                if abs(o.height - h) > 0.25 * h:
                    continue
                dy = last.y - o.y
                if not (0.3 * h < dy <= 2.6 * h) or abs(o.x - r.x) > reach:
                    continue
                if nxt is None or dy < last.y - nxt[0].y:
                    nxt = (o, ot)
            if nxt is None or len(lines) >= 3:
                break
            lines.append(nxt[1])
            last = nxt[0]
        if len(lines) > 1:
            out.append((" ".join(lines), r.x, r.y, h,
                        "{0} ({1} lines)".format(r.source, len(lines))))
    return out


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
    blocks = _title_blocks(text_records)
    for f in floors:
        x0, y0, x1, y1 = _polygon_bbox(f["pts"])
        w, h = x1 - x0, y1 - y0
        best = None
        for side in ("below", "above"):
            for title, tx, ty, th, tsrc in blocks:
                if not (x0 - 0.1 * w <= tx <= x1 + 0.1 * w):
                    continue
                if side == "below" and not (y0 - 0.5 * h <= ty < y0):
                    continue
                if side == "above" and not (y1 < ty <= y1 + 0.5 * h):
                    continue
                gap = (y0 - ty) if side == "below" else (ty - y1)
                # Taller first, then nearer; a title read whole (both its
                # lines) before one of its lines alone.
                score = (th, -gap, len(parse_levels(title)))
                if best is None or score > best[0]:
                    best = (score, title, tsrc)
            if best:
                break
        f["title"] = best[1] if best else ""
        f["title_source"] = best[2] if best else ""
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


def _mark_within(rooms_out, room_polys):
    """Set rooms_out[i]["within"] (name) and ["within_layer"] for every room
    whose centre stands inside another, larger room on the same floor."""
    boxes = []
    for rp in room_polys:
        xs = [p[0] for p in rp["pts"]]
        ys = [p[1] for p in rp["pts"]]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    for i, rp in enumerate(room_polys):
        cx, cy = _polygon_centroid(rp["pts"])
        best = None
        for j, other in enumerate(room_polys):
            if j == i or other.get("floor", "") != rp.get("floor", ""):
                continue
            if other["area_sqm"] <= rp["area_sqm"] * 1.05:
                continue
            b = boxes[j]
            if not (b[0] <= cx <= b[2] and b[1] <= cy <= b[3]):
                continue
            if not _point_in_polygon(cx, cy, other["pts"]):
                continue
            if best is None or other["area_sqm"] < room_polys[best]["area_sqm"]:
                best = j
        if best is not None:
            rooms_out[i]["within"] = room_polys[best].get("name", "")
            rooms_out[i]["within_layer"] = room_polys[best]["layer"]


# How far apart two room outlines may run and still count as along each
# other for shared_near_mm (the neighbours' "touch with a tolerance").
SHARED_NEAR_MM = float(_RD.get("shared_near_mm", 160.0))


def _mark_neighbours(rooms_out, room_polys, reach_mm=300.0):
    """rooms_out[i]["neighbours"]: the rooms on the same floor whose outline
    comes within reach_mm of this one, as [{"name", "layer"}]. The Area
    Statement uses it to tell a service-lift lobby from a passenger one."""
    try:
        from shapely.geometry import Polygon
        from shapely.strtree import STRtree
    except ImportError:
        return
    geoms = []
    for rp in room_polys:
        try:
            g = Polygon(rp["pts"]).buffer(0)
        except Exception:                             # noqa: BLE001
            g = None
        geoms.append(g)
    idx = [i for i, g in enumerate(geoms) if g is not None and not g.is_empty]
    if not idx:
        return
    tree = STRtree([geoms[i] for i in idx])
    # the rooms each X-marked opening runs along (100 mm and more of their
    # outlines within 60 mm): whose opening it is (area_statement.classify)
    along = {}
    for j in idx:
        if not room_polys[j].get("opening"):
            continue
        lst = []
        for k in tree.query(geoms[j].buffer(60.0)):
            m = idx[k]
            if m == j or room_polys[m].get("opening") or room_polys[m].get("floor", "") != room_polys[j].get("floor", ""):
                continue
            try:
                sh = geoms[m].boundary.buffer(60.0).intersection(geoms[j].boundary).length
            except Exception:                         # noqa: BLE001
                sh = 0.0
            if sh >= 100.0:
                lst.append((m, room_polys[m].get("name", "")))
        along[j] = lst
    for i in idx:
        g = geoms[i].buffer(reach_mm)
        near = []
        for k in tree.query(g):
            j = idx[k]
            if j == i or room_polys[j].get("floor", "") != room_polys[i].get("floor", ""):
                continue
            if geoms[j].intersects(g):
                try:
                    inside = geoms[i].contains(geoms[j].representative_point())
                    # how much of the neighbour's outline runs along ours
                    shared = geoms[i].boundary.buffer(60.0).intersection(geoms[j].boundary).length
                    # and with a small tolerance: the two outlines a thin
                    # panel or a drawing gap apart (P285: the FHC cabinet
                    # and its X'd slot behind, shared_mm 0)
                    shared_near = shared if shared > 0 else \
                        geoms[i].boundary.buffer(SHARED_NEAR_MM).intersection(geoms[j].boundary).length
                except Exception:                     # noqa: BLE001
                    inside, shared, shared_near = False, 0.0, 0.0
                near.append({"name": room_polys[j].get("name", ""),
                             "layer": room_polys[j]["layer"],
                             "opening": bool(room_polys[j].get("opening")),
                             "inside": bool(inside),
                             "shared_mm": round(float(shared), 1),
                             "shared_near_mm": round(float(shared_near), 1),
                             "touches": [nm for m, nm in along.get(j, ()) if m != i][:8],
                             "area_sqm": round(float(room_polys[j].get("gross_area_sqm") or room_polys[j].get("area_sqm") or 0.0), 2)})
        rooms_out[i]["neighbours"] = near[:40]


def _mark_exits(rooms_out, room_polys, msp, units):
    """Each room's doors as the drawing has them (door layers: the rooms on
    a door's two sides, elements.door_sides; what it opens into and its
    level from its XDATA, door_tag):
      rooms_out[i]["exits"] / ["exits_to"]: its ways out - the doors that
        open into a room or onto the floor (not a shaft's access door, not a
        WC cubicle's), and what is on the other side of each (a room's name,
        "" where nothing is traced);
      rooms_out[i]["doors_primary"] / ["doors_secondary"]: its doors by level.
    Read from the drawing, so a door the user draws or moves counts."""
    for r in rooms_out:
        r["exits"], r["exits_to"] = 0, []
        r["doors_primary"], r["doors_secondary"] = 0, 0
    try:
        from shapely.geometry import Polygon
        from shapely.strtree import STRtree
        from . import elements as _el
    except ImportError:
        return
    quads = []
    for e in msp.query("LWPOLYLINE POLYLINE LINE"):
        if not LN.is_door_layer(e.dxf.layer or ""):
            continue
        # a door drawn as a line carries its opening (door_quad)
        pts = door_quad(e, units)
        if pts and len(pts) >= 3:
            kind, level = door_tag(e)
            quads.append((pts, kind, level))
    if not quads:
        return
    idx, geoms = [], []
    for i, rp in enumerate(room_polys):
        try:
            g = Polygon(rp["pts"]).buffer(0)
        except Exception:                             # noqa: BLE001
            continue
        if not g.is_empty and g.area > 0:
            idx.append(i)
            geoms.append(g)
    if not geoms:
        return
    tree = STRtree(geoms)
    for q, kind, level in quads:
        a, b = _el.door_sides(q, geoms, tree)
        for me in {a, b} - {None}:
            r = rooms_out[idx[me]]
            key = "doors_secondary" if level == "secondary" else "doors_primary"
            r[key] += 1
        if kind != "exit" or a == b:
            continue
        for me, other in ((a, b), (b, a)):
            if me is None:
                continue
            r = rooms_out[idx[me]]
            r["exits"] += 1
            r["exits_to"].append(room_polys[idx[other]].get("name", "") if other is not None else "")


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


# -- Floor usage -------------------------------------------------------------
# A family can belong to some kinds of floor only: PARKING, SLOPE and RAMP
# name parking bays and ramps on a parking floor, but on an office floor a
# "SLOPE" is a floor finish. Floors have a usage (office, parking, retail,
# residential...), and a family lists the usages it applies to - none
# listed means every floor. Many to many: a family can serve several
# usages, and a usage takes many families.
_DEFAULT_USAGES = [
    {"name": "Office", "keywords": ["OFFICE", "WORKSPACE", "IT FLOOR"]},
    {"name": "Parking", "keywords": ["PARKING", "CAR PARK", "STILT", "BASEMENT", "BSMT"]},
    {"name": "Retail", "keywords": ["RETAIL", "SHOP", "SHOPS", "SHOWROOM", "COMMERCIAL", "MALL"]},
    {"name": "Residential", "keywords": ["RESIDENTIAL", "APARTMENT", "FLAT", "FLATS", "RESIDENCE", "VILLA"]},
    {"name": "Hospitality", "keywords": ["HOTEL", "HOSPITALITY", "SERVICED APARTMENT"]},
    {"name": "Amenity", "keywords": ["AMENITY", "AMENITIES", "CLUB HOUSE", "CLUBHOUSE"]},
]


# Which result wins when the texts and the geometry disagree about an
# object of this family (Step 5): "geometry", "text", or "" - not decided,
# so the user is asked.
# geometry: where both sides say the same family, the geometry's outline.
# geometry_always: where the geometry finds one of these, it wins - outline
# and family - even over a text that names it something else (lifts, lift
# lobbies: the geometry reads them from the wells and their doors).
PRECEDENCE_VALUES = ("geometry", "geometry_always", "text")


def _clean_precedence(raw):
    v = str(raw or "").strip().lower()
    return v if v in PRECEDENCE_VALUES else ""


def family_precedence(name):
    """The precedence set for a family, or "" (ask the user)."""
    for f in _FAMILY_RULES:
        if f["name"] == name:
            return f.get("precedence") or ""
    return ""


def _clean_usages(raw):
    """A family's usage list as given: names, stripped, unique. [] = all."""
    out = []
    for u in raw if isinstance(raw, (list, tuple)) else []:
        u = str(u or "").strip()
        if u and u.lower() not in [x.lower() for x in out]:
            out.append(u)
    return out


def _load_floor_usages(rules_path=None):
    """(usages, default usage name) from family_rules.json."""
    path = rules_path or family_rules_path()
    data = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    usages = []
    for u in data.get("usages") or _DEFAULT_USAGES:
        if isinstance(u, str):
            u = {"name": u, "keywords": []}
        name = str((u or {}).get("name") or "").strip()
        if not name or name.lower() in [x["name"].lower() for x in usages]:
            continue
        usages.append({"name": name,
                       "keywords": [str(k).upper().strip() for k in u.get("keywords") or []
                                    if str(k).strip()]})
    if not usages:
        usages = [dict(u) for u in _DEFAULT_USAGES]
    default = str(data.get("default_usage") or "").strip()
    if default.lower() not in [u["name"].lower() for u in usages]:
        default = usages[0]["name"]
    return usages, default


def list_floor_usages():
    """[{"name", "keywords"}] in order, and the default usage's name."""
    return [dict(u) for u in _FLOOR_USAGES], _DEFAULT_USAGE


def floor_usage(*texts):
    """The usage a floor's title or name reads as: "BASEMENT 2 FLOOR PLAN"
    -> Parking, "RETAIL FLOOR" -> Retail; the default usage otherwise."""
    t = " ".join(str(x or "") for x in texts).upper()
    if t.strip():
        for u in _FLOOR_USAGES:
            if any(keyword_matches(k, t) for k in u["keywords"]):
                return u["name"]
    return _DEFAULT_USAGE


def usage_name(usage):
    """A usage as the rules spell it, or None for none / unknown."""
    u = str(usage or "").strip().lower()
    for x in _FLOOR_USAGES:
        if x["name"].lower() == u:
            return x["name"]
    return None


def rules_for_usage(usage, rules=None):
    """The families that apply on a floor of this usage: those that list it,
    and those that list none. No usage given: every family."""
    rules = _FAMILY_RULES if rules is None else rules
    u = str(usage or "").strip().lower()
    if not u:
        return list(rules)
    return [f for f in rules
            if not f.get("usages") or u in [x.lower() for x in f["usages"]]]


def reload_family_rules():
    """Re-read family_rules.json and rebuild everything derived from it.

    Editing the rules in the UI has to take effect without a restart, and
    the label ranking is built from the same rules, so both globals are
    rebound together.
    """
    global _FAMILY_RULES, _LABEL_RULES, _FLOOR_USAGES, _DEFAULT_USAGE
    _FAMILY_RULES = _load_family_rules()
    _LABEL_RULES = build_rules(CONFIG, _FAMILY_RULES, ignore_words=_FAMILY_IGNORE_WORDS)
    _FLOOR_USAGES, _DEFAULT_USAGE = _load_floor_usages()
    _FAMILY_OF_CACHE.clear()
    return _FAMILY_RULES


def list_family_rules(rules=None):
    """The full editable ruleset: name, layer, colour and keywords."""
    rules = _FAMILY_RULES if rules is None else rules
    return [{"name": fam["name"], "layer": fam["layer"],
             "boundary_layer": _family_layer(fam),
             "color": int(fam.get("color", 7)),
             "keywords": list(fam.get("keywords", [])),
             "usages": list(fam.get("usages") or []),
             "precedence": fam.get("precedence") or ""}
            for fam in rules]


def save_family_rules(families, rules_path=None, usages=None, default_usage=None):
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

        entry = {"name": name, "layer": layer, "color": color,
                 "keywords": keywords}
        fam_usages = _clean_usages(fam.get("usages"))
        if fam_usages:
            entry["usages"] = fam_usages
        prec = _clean_precedence(fam.get("precedence"))
        if prec:
            entry["precedence"] = prec
        cleaned.append(entry)

    payload = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
            payload = {k: v for k, v in existing.items()
                       if k.startswith("_") or k in ("usages", "default_usage")}
            # A family's own notes ("_note") stay with it, and an entry the
            # editor never shows - one with no keywords, such as Ignore,
            # which is built in - is kept as it was.
            old = {str(f.get("name") or ""): f for f in existing.get("families") or []
                   if isinstance(f, dict)}
            for fam in cleaned:
                for k, v in (old.get(fam["name"]) or {}).items():
                    if k.startswith("_"):
                        fam.setdefault(k, v)
            sent = {fam["name"] for fam in cleaned}
            cleaned.extend(f for n, f in old.items()
                           if n and n not in sent and not f.get("keywords"))
        except (OSError, ValueError):
            payload = {}
    if usages is not None:
        clean_u = []
        for u in usages if isinstance(usages, list) else []:
            if isinstance(u, str):
                u = {"name": u}
            name_u = str((u or {}).get("name") or "").strip()
            if not name_u:
                continue
            if name_u.lower() in [x["name"].lower() for x in clean_u]:
                raise ValueError("Floor usage '{0}' is listed twice.".format(name_u))
            clean_u.append({"name": name_u,
                            "keywords": [str(k).upper().strip()
                                         for k in (u.get("keywords") or []) if str(k).strip()]})
        if not clean_u:
            raise ValueError("Keep at least one floor usage.")
        payload["usages"] = clean_u
    if default_usage is not None:
        payload["default_usage"] = str(default_usage).strip()
    known = [u["name"].lower() for u in (payload.get("usages") or _DEFAULT_USAGES)]
    for fam in cleaned:
        bad = [u for u in fam.get("usages", []) if u.lower() not in known]
        if bad:
            raise ValueError("'{0}' lists floor usage '{1}', which is not in "
                             "the usage list.".format(fam["name"], bad[0]))
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

    # the family library's ignore words (2026-10-17): texts that are only
    # such a word never name a room (labels.LabelRules)
    global _FAMILY_IGNORE_WORDS
    _FAMILY_IGNORE_WORDS = [str(w).strip().upper() for w in (data.get("ignore_words") or []) if str(w).strip()]
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
        out.append({"name": name, "layer": layer, "color": color, "keywords": kws,
                    "usages": _clean_usages(fam.get("usages")),
                    "precedence": _clean_precedence(fam.get("precedence")),
                    # words that are evidence for the family, never a name
                    # (Staircase: UP, DOWN, DN - rule rooms_from_spaces)
                    "evidence": [str(w).strip().upper() for w in (fam.get("evidence_words") or []) if str(w).strip()]})

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

# Family the user picks in Step 3 for a text that should get no boundary
# and be used nowhere further. Always offered, like Unknown: a family with
# no keywords is dropped when family_rules.json loads, and Ignore needs
# none. Keywords given to an "Ignore" entry in family_rules.json still
# apply, through the normal rules.
_IGNORE_FAMILY = {
    "name": "Ignore",
    "layer": "IGNORE",
    "color": 8,
    "keywords": [],
}


def _dropdown_families(rules):
    """Rules, then Unknown, then Ignore - each name once."""
    fixed = (_UNKNOWN_FAMILY["name"], _IGNORE_FAMILY["name"])
    return ([fam for fam in rules if fam["name"] not in fixed]
            + [_UNKNOWN_FAMILY, _IGNORE_FAMILY])


def family_for_label(room_name, rules):
    """Return the full family dict for a label (or _UNKNOWN_FAMILY if no
    match). Use this when you need layer + color info, not just the name.
    """
    n = (room_name or "").strip().upper()
    if not n or "UNKNOWN" in n:
        return _UNKNOWN_FAMILY
    # The same label is looked up many times in a run (ranking, claiming,
    # titling); its answer depends only on the text and the families.
    key = (n, tuple(id(f) for f in rules))
    hit = _FAMILY_OF_CACHE.get(key)
    if hit is not None:
        return hit
    form = match_form(n)
    found = _UNKNOWN_FAMILY
    for fam in rules:
        for kw in fam["keywords"]:
            # Whole-word, not substring: plain containment put "FRESH AIR
            # ATTENUATORS" into the TEN family and "8M EXPRESS WAY" into
            # PRESS.
            rx = keyword_regex(kw)
            if rx is not None and rx.search(form):
                found = fam
                break
        if found is not _UNKNOWN_FAMILY:
            break
    if len(_FAMILY_OF_CACHE) > 100000:
        _FAMILY_OF_CACHE.clear()
    _FAMILY_OF_CACHE[key] = found
    return found


_FAMILY_OF_CACHE = {}


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
    if n == _IGNORE_FAMILY["name"]:
        return _IGNORE_FAMILY
    return None


def list_family_names(rules=None):
    """Ordered list of family display names for the Step 3 dropdown and
    Step 4 grouping - 'Unknown' then 'Ignore' always last."""
    rules = _FAMILY_RULES if rules is None else rules
    return [fam["name"] for fam in _dropdown_families(rules)]


def _family_layer(family):
    """A family's DXF layer: AI_<LAYER> (layernames.family_layer)."""
    return LN.family_layer(family.get("layer") or "UNKNOWN")


def list_families(rules=None):
    """Display name, DXF layer and color per family, in dropdown order.

    list_family_names gives only the names; the boundary-type picker also
    shows the layer each family writes to (STAIR_BOUNDARY, MEP_BOUNDARY),
    because that is what the user sees in CAD.
    """
    rules = _FAMILY_RULES if rules is None else rules
    return [{"name": fam["name"],
             "layer": _family_layer(fam),
             "color": int(fam.get("color", 7)),
             "usages": list(fam.get("usages") or []),
             "precedence": fam.get("precedence") or ""}
            for fam in _dropdown_families(rules)]


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
    # either name: AI_SHAFT, or the old SHAFT_BOUNDARY -> "SHAFT"
    suffix = LN.room_key(layer_name)
    if suffix in (None, "UNKNOWN", "ROOM", ""):
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
        "LIFT PRESS", "LIFT PRES", "LIFT PREZ", "LIFT PRE",
    ]},
    {"name": "Toilet", "layer": "TOILET", "color": 4, "keywords": [
        "WOMEN TOILET", "MEN TOILET", "TOILET",
        "P.C TOI", "P.C. TOI", "PC TOI", "TOI ",
        "JAN", "VANITY COUNTER", "WC", "WASHROOM", "REST ROOM", "RESTROOM",
    ]},
    {"name": "Lift", "layer": "LIFT", "color": 5, "keywords": [
        "LIFT ELE.PANELS", "LIFT LOBBY", "LIFT WELL",
        "P.LIFT", "P LIFT", "PASSENGER LIFT", "SERVICE LIFT",
        "FREIGHT LIFT", "PAX", "ELEVATOR", "LIFT",
    ]},
    {"name": "Staircase", "layer": "STAIR", "color": 3, "keywords": [
        "STAIRCASE", "STAIR PR", "STAIR PREZ", "STAIR",
        "ST-", "STC", "FIRE EXIT",
    ]},
    {"name": "AHU Room", "layer": "AHUROOM", "color": 200, "keywords": [
        "AIR HANDLING UNIT", "AIR HANDLING", "AHU ROOM", "AHU RM", "AHU",
    ]},
    {"name": "MEP / Services", "layer": "MEP", "color": 6, "keywords": [
        "CONDENSER", "FHC", "FHR", "PRESS",
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
_FAMILY_IGNORE_WORDS = []          # set by _load_family_rules (family_rules.json "ignore_words")
_FAMILY_RULES = _load_family_rules()
_LABEL_RULES = build_rules(CONFIG, _FAMILY_RULES, ignore_words=_FAMILY_IGNORE_WORDS)
_FLOOR_USAGES, _DEFAULT_USAGE = _load_floor_usages()


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