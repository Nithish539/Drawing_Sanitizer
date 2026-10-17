"""Geometry helpers shared by Step 3 and the Geometry tab.

Until 2026-10-17 this module was a second engine: it parsed the drawing
again in a process of its own, traced the walls, the doors and the columns
a second time and classified every enclosed space from shape alone, for the
Geometry tab, the comparison and the final drawing. Step 3 finds all of
that in its one pass now (walls W1-W3, the X sequence, the doors, the
spaces traced before any text is read, the columns), and keeps it in the
Step 3 result store (step3store.py). The engine's run is gone; what stays
here is what Step 3 and the tab still share:

    _column_shapes       the one column finder (dxf_reader._floor_columns
                         reads it before the walls are traced; the final
                         drawing and the tab take its columns from the store)
    _wall_faces, _in_wall_line, _dedupe_shapes, _grid_fit - its helpers
    CLASSES / LAYER_OF / STEP3_CLASSES / STEP3_FAMILIES, _poly, _shape
    write_dxf            the geometry drawing from a result
    step3_free, apply_reviews, build_final, family_of
                         the Geometry tab's reviews and the user's output

A shaft, a lift, a window, a cut-out, a column or a void is Step 3's (the
X sequence) and is never a finding of the tab (STEP3_FAMILIES).
"""
from __future__ import annotations

import collections
import math
import re

import shapely
from shapely.geometry import Polygon

from . import dxf_reader as dr

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


def _geo_setting(key, default):
    """A number from config.json "geometry_engine" (see its _notes)."""
    try:
        from .config import CONFIG
        return float((CONFIG.get("geometry_engine") or {}).get(key, default))
    except Exception:                                 # noqa: BLE001
        return float(default)


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


def write_dxf(doc, units, result, out_path):
    """The source drawing plus one layer per class: a closed boundary and a
    label (name, id and confidence) for every finding, its holes on AI_VOID,
    then the walls, hanging walls, doors, doubtful openings and glass the
    result carries. A result read from the Step 3 store (step3store.
    findings: "source" step3) carries each door's kind, level, sides, owner,
    line and direction, and the doors are drawn exactly as Step 3 drew them;
    an older engine result falls back to the door rules on the findings.
    Original layers are not touched."""
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
            conf = "" if r.get("confidence") is None else " {0:.0f}%".format(100 * r["confidence"])
            if r.get("reviewed"):
                conf = " (edited)"
            cx, cy = r["features"]["centroid"]
            msp.add_mtext("{0}\\P{1}{2}".format(r["label"], r["id"], conf), dxfattribs={
                "layer": layer, "char_height": f(ht), "attachment_point": 5,
                "insert": (f(cx), f(cy))})
            # a column or a well standing in a room is a hole in it
            # (deducted from its area), as Step 3 drew it
            for hole in r.get("holes") or ():
                if len(hole) < 3:
                    continue
                if dr.LN.VOID not in doc.layers:
                    doc.layers.add(dr.LN.VOID, color=8)
                msp.add_lwpolyline([(f(x), f(y)) for x, y in hole], close=True,
                                   dxfattribs={"layer": dr.LN.VOID, "color": 256})
    for fl, by_class in (result.get("walls") or {}).items():
        dr.draw_walls(doc, msp, units, by_class)
    dr.draw_hanging_walls(doc, msp, units, result.get("hanging_walls") or [])
    if dr.DRAW_DOORS:
        step3 = result.get("source") == "step3" and isinstance(result.get("door_kinds"), dict)
        for fl, quads in (result.get("doors") or {}).items():
            if step3:
                # Step 3's doors, as Step 3 drew them
                dr.draw_doors(doc, msp, units, quads,
                              kinds=(result.get("door_kinds") or {}).get(fl),
                              levels=(result.get("door_levels") or {}).get(fl),
                              sides=(result.get("door_sides") or {}).get(fl),
                              owners=(result.get("door_owners") or {}).get(fl),
                              lines=(result.get("door_lines") or {}).get(fl),
                              dirs=(result.get("door_dirs") or {}).get(fl))
                continue
            rows_fl = (result.get("floors") or {}).get(fl) or []
            solid_ = dr.door_solid((result.get("walls") or {}).get(fl), (result.get("window_openings") or {}).get(fl),
                                   (result.get("glass") or {}).get(fl), rows_fl)
            dr.draw_doors_by_level(doc, msp, units, quads, dr.door_rooms(rows_fl),
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
                "holes": [list(map(list, h)) for h in ((r or {}).get("holes") or []) if len(h) >= 3],
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
    # a result read from the Step 3 store: its doors' kinds, levels, sides,
    # owners, lines and directions, its columns and shaft reviews come along
    out["source"] = result.get("source") or "engine"
    for k in ("door_kinds", "door_levels", "door_sides", "door_owners", "door_lines", "door_dirs",
              "columns", "shaft_review"):
        if result.get(k) is not None:
            out[k] = result[k]
    return out
