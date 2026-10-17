# -*- coding: utf-8 -*-
"""Geometry extract: the spaces the drawing's own geometry defines.

Step 3's text list starts from the words; this starts from the shapes, so
the user can see what the drawing marks and map each shape to a family and
a label before Generate:

  X regions       cut-outs, shafts and voids crossed corner to corner
                  (xregions); over the void threshold a region is a Void
  Staircases      runs of evenly spaced tread lines, grown to the walls
  Toilets         clusters of WC / basin / urinal fixtures, grown to the
                  walls
  Closed outlines closed polylines on any layer that is not walls,
                  furniture, text or dimensions, 0.5 - 1000 sq.m

Every item carries its outline, size, the layers it came from, the texts
inside it (and, for an X, beside it), a suggested label and a suggested
family. Items are grouped by family for the UI.
"""

from __future__ import annotations

import math
import re
from typing import Dict

from . import dxf_reader as dr
from . import xregions
from .dxfio import load_document
from .geometry import BandIndex, shoelace_area_sqm
from .labels import declared_size

GROUP_ORDER = ["Void", "Shaft", "Lift", "Staircase", "Toilet"]
# Families a label standing beside an X may give it.
SHAFT_LIKE = {"Shaft", "Lift", "Plumbing", "MEP / Services", "Void"}
_WALL_LAYER = re.compile(r"(^|[^A-Z])WL([^A-Z]|$)")
OUTLINE_MIN_SQM = 0.5
OUTLINE_MAX_SQM = 1000.0
OUTLINE_MIN_SIDE_MM = 500.0      # thinner than this is a wall or a line
NEAR_MM = 2500.0                 # a label beside an X, as in Phase A
_NOT_OUTLINE = ("FURN", "TEXT", "DIM", "HATCH", "GRID", "ANNO", "NOTE", "SYMB",
                "TITLE", "BORDER", "VIEWPORT", "DEFPOINTS", "_BOUNDARY", "_VOID", "AI_",
                "COL", "WALL", "BEAM", "CUBIC", "FIXT", "SANIT", "DOOR", "WIN",
                "GLASS", "RAIL", "PLATE", "SCPL", "SEAT", "PLANTER", "REF")


def _bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _rect(b):
    return [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]


def _iou(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _gap(x, y, b):
    return math.hypot(max(b[0] - x, 0, x - b[2]), max(b[1] - y, 0, y - b[3]))


def _texts_for(item, rows, near=0.0):
    """Texts inside the item (and within `near` mm of it), best first:
    inside before beside, a stated size that fits before one that does not,
    a room name before a tag or a note."""
    b = item["bbox"]
    poly = item.get("poly")
    w, h = b[2] - b[0], b[3] - b[1]
    out = []
    for r in rows:
        x, y = float(r["x"]), float(r["y"])
        inside = (b[0] <= x <= b[2] and b[1] <= y <= b[3] and
                  (not poly or dr._point_in_polygon(x, y, poly)))
        gap = 0.0 if inside else _gap(x, y, b)
        if not inside and gap > near:
            continue
        t = r["text"]
        if dr.is_ignorable_label(t):
            continue
        fit = dr._size_match(declared_size(t), w, h)
        rank = (0 if inside else 1,
                0 if fit == "full" else (1 if fit == "partial" else 2),
                1 if dr.is_note_text(t) or dr._is_plain_tag(t) else 0,
                dr.label_score(t, r.get("height", 0.0), gap))
        out.append((rank, {"sr_no": r.get("sr_no"), "text": t,
                           "inside": inside, "gap_mm": round(gap)}))
    out.sort(key=lambda o: o[0])
    return [o[1] for o in out]


def _family_of_text(text):
    if not text:
        return "Unknown"
    return dr.family_for_label(text, dr._FAMILY_RULES)["name"]


def _family_of_layer(layers):
    """A family the layer name itself suggests (A-STAIR -> Staircase)."""
    for lay in layers or ():
        name = str(lay).upper().replace("_", " ").replace("-", " ")
        fam = dr.family_for_label(name, dr._FAMILY_RULES)["name"]
        if fam != "Unknown":
            return fam
    return "Unknown"


# ---------------------------------------------------------------- stairs
def _stair_runs(segs, clip):
    """Groups of parallel tread lines at a step's going, as boxes."""
    lo, hi = dr.STAIR_RISER_MIN, dr.STAIR_RISER_MAX
    buckets = {}
    for s in segs:
        dx, dy = abs(s[2] - s[0]), abs(s[3] - s[1])
        L = math.hypot(dx, dy)
        if L < dr.STAIR_TREAD_MIN_LEN or L > 4000.0:
            continue
        if dy <= 0.03 * L:
            a0, a1 = sorted((s[0], s[2]))
            key = ("h", round(a0 / 150.0), round(a1 / 150.0))
            pos = (s[1] + s[3]) / 2.0
        elif dx <= 0.03 * L:
            a0, a1 = sorted((s[1], s[3]))
            key = ("v", round(a0 / 150.0), round(a1 / 150.0))
            pos = (s[0] + s[2]) / 2.0
        else:
            continue
        buckets.setdefault(key, []).append((pos, a0, a1))
    boxes = []
    for key, items in buckets.items():
        items.sort()
        pos = []
        for p in items:
            if not pos or p[0] - pos[-1][0] > 15.0:
                pos.append(p)
        run = [pos[0]] if pos else []
        step = None

        def close(run):
            if len(run) >= dr.STAIR_MIN_TREADS:
                a0 = min(r[1] for r in run)
                a1 = max(r[2] for r in run)
                p0, p1 = run[0][0], run[-1][0]
                boxes.append((a0, p0, a1, p1) if key[0] == "h" else (p0, a0, p1, a1))
        for k in range(1, len(pos)):
            d = pos[k][0] - pos[k - 1][0]
            if lo <= d <= hi and (step is None or abs(d - step) <= 25.0):
                run.append(pos[k])
                step = d if step is None else step
            else:
                close(run)
                run, step = [pos[k]], None
        close(run)
    # Flights of one stair stand side by side: join boxes within 1.5 m.
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if (a[0] - 1500 <= b[2] and b[0] - 1500 <= a[2] and
                        a[1] - 1500 <= b[3] and b[1] - 1500 <= a[3]):
                    boxes[i] = (min(a[0], b[0]), min(a[1], b[1]),
                                max(a[2], b[2]), max(a[3], b[3]))
                    boxes.pop(j)
                    merged = True
                    break
            if merged:
                break
    return [b for b in boxes if clip[0] <= (b[0] + b[2]) / 2 <= clip[2]
            and clip[1] <= (b[1] + b[3]) / 2 <= clip[3]]


def _grow_to_walls(core, walls, idx, max_sqm):
    """The walled space around a core box: the ray-cast box from its centre
    when it holds the core and stays a sensible size, else the core."""
    cx, cy = (core[0] + core[2]) / 2.0, (core[1] + core[3]) / 2.0
    box = dr._hv_boundary(cx, cy, walls, index=idx)
    if box is not None:
        holds = (box[0] <= core[0] + 50 and box[1] <= core[1] + 50 and
                 box[2] >= core[2] - 50 and box[3] >= core[3] - 50)
        if holds and (box[2] - box[0]) * (box[3] - box[1]) / 1e6 <= max_sqm:
            return box
    return core


# ----------------------------------------------------------------- toilets
def _fixture_clusters(points, gap=2500.0):
    pts = list(points)
    parent = list(range(len(pts)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            if abs(pts[i][0] - pts[j][0]) <= gap and abs(pts[i][1] - pts[j][1]) <= gap:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    groups = {}
    for i in range(len(pts)):
        groups.setdefault(find(i), []).append(pts[i])
    return [g for g in groups.values() if len(g) >= dr.TOILET_MIN_FIXTURES]


# ---------------------------------------------------------------- outlines
MAX_OUTLINES = 5000


def layer_hidden_by_default(layer):
    """Furniture, doors, walls, text and the like: listed in the Layers
    panel but not shown until the user turns them on."""
    lu = (layer or "").upper()
    return any(k in lu for k in _NOT_OUTLINE) or bool(_WALL_LAYER.search(lu))


def _closed_outlines(doc, msp, units):
    """Every closed polyline of a room's size, in modelspace and in blocks,
    on any layer; `hidden` marks the layers shown only on request."""
    from .dxfio import effective_layer, iter_entities
    out = []
    for e, xf, _d in iter_entities(doc, msp, "LWPOLYLINE POLYLINE"):
        try:
            if not dr._is_closed(e):
                continue
            pts = dr._polyline_points(e, units, xf)
            lay = effective_layer(e, xf) or "0"
        except Exception:                             # noqa: BLE001
            continue
        if len(pts) < 3:
            continue
        a = shoelace_area_sqm(pts)
        if not (OUTLINE_MIN_SQM <= a <= OUTLINE_MAX_SQM):
            continue
        b = _bbox(pts)
        if min(b[2] - b[0], b[3] - b[1]) < OUTLINE_MIN_SIDE_MM:
            continue
        out.append({"pts": [tuple(p) for p in pts], "layer": lay, "area": a,
                    "bbox": b, "hidden": layer_hidden_by_default(lay)})
        if len(out) >= MAX_OUTLINES:
            break
    return out


# ------------------------------------------------------------------ spaces
SPACE_MIN_SQM = 0.5
SPACE_MAX_SQM = 1500.0
SPACE_MIN_WIDTH_MM = 300.0       # 2 x area / perimeter: thinner is a wall


def _spaces(wall_lines, floor_poly):
    """Every face the walls enclose, as CAD's region command finds them:
    the wall linework is noded and split into faces. Each segment is
    stretched 40 mm at both ends so drafting gaps still close; faces
    thinner than a room (wall strips, columns) are dropped."""
    from shapely.geometry import LineString, Polygon
    from shapely.ops import polygonize, unary_union
    lines = []
    for sg in wall_lines:
        x1, y1, x2, y2 = sg[0], sg[1], sg[2], sg[3]
        L = math.hypot(x2 - x1, y2 - y1)
        if L < 1.0:
            continue
        ux, uy = (x2 - x1) / L * 40.0, (y2 - y1) / L * 40.0
        lines.append(LineString([(x1 - ux, y1 - uy), (x2 + ux, y2 + uy)]))
    if not lines:
        return []
    fp = Polygon(floor_poly)
    if not fp.is_valid:
        fp = fp.buffer(0)
    out = []
    for pg in polygonize(unary_union(lines)):
        a = pg.area / 1e6
        if a < SPACE_MIN_SQM or a > SPACE_MAX_SQM:
            continue
        if 2.0 * pg.area / max(pg.length, 1.0) < SPACE_MIN_WIDTH_MM:
            continue
        if not fp.contains(pg.representative_point()):
            continue
        pg = pg.simplify(20.0, preserve_topology=True)
        out.append(pg)
    return out


def trace_at(ctx, x, y):
    """The space around a clicked point, traced from the walls cached by
    Detect geometry (with doorless gaps closed if the plain trace leaks).
    Returns (polygon points, area sq.m) or None."""
    from .roomtrace import RoomTracer
    for key, extra in (("tracer", []), ("tracer_closed", ctx.get("gaps") or [])):
        tr = ctx.get(key)
        if tr is None:
            if key == "tracer_closed" and not extra:
                continue
            tr = RoomTracer(ctx["walls"] + extra, cell_mm=dr.TRACE_CELL_MM,
                            max_area_sqm=dr.MAX_ROOM_SQM,
                            seal_openings_mm=dr.SEAL_OPENINGS_MM)
            ctx[key] = tr
        res = dr._trace_with(tr, x, y)
        if res is not None and res.ok and res.area_sqm > 0:
            return [tuple(p) for p in res.polygon], res.area_sqm
    box = dr._hv_boundary(x, y, ctx["walls"], index=ctx.get("idx"))
    if box is not None:
        return _rect(box), (box[2] - box[0]) * (box[3] - box[1]) / 1e6
    return None


def merge_polygons(polys):
    """One outline from several neighbouring spaces, across the walls
    between them (each is grown 150 mm, joined, and shrunk back)."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    shapes = [Polygon(p).buffer(0) for p in polys if len(p) >= 3]
    if not shapes:
        return None
    u = unary_union([sh.buffer(150.0, join_style=2) for sh in shapes]).buffer(-150.0, join_style=2)
    if u.is_empty:
        return None
    if u.geom_type != "Polygon":
        return None
    u = u.simplify(20.0, preserve_topology=True)
    return [tuple(p) for p in list(u.exterior.coords)[:-1]]


def describe(poly, rows, kind="traced", note=""):
    """A user-made shape (traced, drawn or merged) in the same form as a
    detected one: size, area, texts inside, suggested label and family."""
    b = _bbox(poly)
    it = {"kind": kind, "bbox": [round(v, 1) for v in b],
          "poly": [[round(p[0], 1), round(p[1], 1)] for p in poly],
          "layers": [], "note": note}
    it["area_sqm"] = round(shoelace_area_sqm(it["poly"]), 2)
    it["size_mm"] = [round(b[2] - b[0]), round(b[3] - b[1])]
    texts = _texts_for(it, rows, 0.0)
    it["texts"] = texts[:12]
    best = next((t for t in texts if not (dr.is_note_text(t["text"]) or
                                          dr._is_plain_tag(t["text"]))), None)
    it["label"] = best["text"] if best else ""
    it["label_sr"] = best["sr_no"] if best else None
    it["family"] = _family_of_text(it["label"])
    it["covered_by_text"] = ""
    return it


# -------------------------------------------------------------------- main
def extract_geometry(dxf_path, floors, results, unit_override=None,
                     keep_context=None, loaded=None, x_layer_ok=None) -> Dict:
    """{floor name: [item, ...]} for the floors picked in Step 2.

    keep_context, a dict, receives each floor's walls, segments and texts
    so a click in the drawing can be traced later without reading the file
    again. loaded, (doc, msp, units), skips reading the file when the
    caller already has it open. x_layer_ok overrides which layers an X
    mark may be drawn on (default: cut-out layers, see xregions)."""
    doc, msp, units = loaded if loaded else load_document(dxf_path, unit_override, readonly=True)
    raw = dr._collect_raw_segs(doc, msp, units)
    seg_boxes = dr._seg_boxes(raw)
    keep = [dr._floor_clip_box(f["polygon"]) for f in floors]
    raw = dr._segs_in_boxes(raw, seg_boxes, keep)
    try:
        wall_hatches = dr._collect_wall_hatches(doc, msp, units)
    except Exception:                                 # noqa: BLE001
        wall_hatches = []
    # a wall with an object drawn under it is above floor level: the whole
    # element leaves the walls (dxf_reader.find_hanging_walls)
    try:
        hanging = dr.find_hanging_walls(doc, msp, units, wall_hatches)
        raw, wall_hatches, _ah, _cl = dr.drop_hanging_walls(hanging, raw, wall_hatches)
    except Exception:                                 # noqa: BLE001
        hanging = []
    seg_boxes = dr._seg_boxes(raw)
    regions = xregions.find_x_regions(raw, dr.X_DIAG_MIN_LEN, dr.X_DIAG_MIN_AXIS,
                                      layer_ok=x_layer_ok or xregions.is_x_layer)
    # the slab edge running through the shafts is no wall and no room side
    # (dxf_reader.drop_slab_edge_lines)
    if dr.SLAB_EDGE_ON:
        raw, _slab = dr.drop_slab_edge_lines(raw, [rg["bbox"] for rg in regions if rg.get("bbox")])
        if _slab:
            seg_boxes = dr._seg_boxes(raw)
            regions = xregions.find_x_regions(raw, dr.X_DIAG_MIN_LEN, dr.X_DIAG_MIN_AXIS,
                                              layer_ok=x_layer_ok or xregions.is_x_layer)
    x_lines = set()
    for rg in regions:
        for k in rg["seg_idx"]:
            x_lines.add(dr._seg_key(raw[k]))
    try:
        fixtures = dr._collect_fixtures(doc, msp, units)
    except Exception:                                 # noqa: BLE001
        fixtures = []
    outlines = _closed_outlines(doc, msp, units)
    try:
        swings = dr._collect_door_swings(doc, msp, units)
    except Exception:                                 # noqa: BLE001
        swings = []
    layer_counts = {}

    out = {}
    for floor in floors:
        name = floor["name"]
        poly = [tuple(p) for p in floor["polygon"]]
        clip = dr._floor_clip_box(poly)
        floor_sqm = shoelace_area_sqm(poly)
        segs = dr._segs_in_box(raw, seg_boxes, clip)
        walls = dr._detect_walls(segs, skip=x_lines, plate=poly, hatches=wall_hatches)
        faces, caps = walls["faces"], walls["caps"]
        sw = [w for w in swings if clip[0] <= w[0] <= clip[2] and clip[1] <= w[1] <= clip[3]]
        doors = dr._door_bridges(faces, caps, sw) if sw else []
        gaps = dr._find_collinear_wall_gaps(faces)
        wall_set = faces + caps + doors + dr._polygon_edges(poly)
        idx = BandIndex(wall_set, band=dr.INDEX_BAND, axis_ratio=dr.WALL_AXIS_RATIO)
        rows = results.get(name, [])
        if keep_context is not None:
            keep_context[name] = {"walls": wall_set, "gaps": gaps, "idx": idx,
                                  "rows": rows, "segs": segs, "swings": sw,
                                  # for the walls drawn in the output
                                  "strips": walls.get("strips") or [], "doors": doors,
                                  "excluded": walls.get("excluded") or []}

        def inside_floor(b):
            return dr._point_in_polygon((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0, poly)

        items = []

        def add(kind, b, shape, layers, family, near=0.0, note=""):
            it = {"kind": kind, "bbox": [round(v, 1) for v in b],
                  "poly": [[round(p[0], 1), round(p[1], 1)] for p in (shape or _rect(b))],
                  "layers": sorted(set(layers)), "note": note}
            it["area_sqm"] = round(shoelace_area_sqm(it["poly"]), 2)
            it["size_mm"] = [round(b[2] - b[0]), round(b[3] - b[1])]
            texts = _texts_for(it, rows, near)
            it["texts"] = texts[:12]
            area = it["area_sqm"]

            def usable(t):
                """A text that can name this shape: not a note or a tag,
                any size it states fits, and a text beside an X only when
                it names a shaft-type space."""
                if dr.is_note_text(t["text"]) or dr._is_plain_tag(t["text"]):
                    return False
                want = declared_size(t["text"])
                if want and not (dr._size_ok(area, want, b) or
                                 dr._size_match(want, *it["size_mm"]) == "partial"):
                    return False
                fam = _family_of_text(t["text"])
                if not t["inside"] and fam not in SHAFT_LIKE:
                    return False
                if family in ("Staircase", "Toilet") and fam != family:
                    return False
                return True
            best = next((t for t in texts if usable(t)), None)
            it["label"] = best["text"] if best else ""
            it["label_sr"] = best["sr_no"] if best else None
            if family is None:
                family = _family_of_text(it["label"])
                if family == "Unknown":
                    family = _family_of_layer(layers)
            it["family"] = family
            if family == "Void":
                it["label"], it["label_sr"] = "VOID", None   # voids are named VOID
            elif not it["label"] and family in ("Staircase", "Toilet"):
                it["label"] = family.upper()
            # A staircase, toilet or outline already named by a text of its
            # own family is drawn from that text by the usual pass; it is
            # listed, but not drawn from geometry unless the user asks.
            it["covered_by_text"] = ""
            if kind in ("stair", "toilet", "outline"):
                pool = texts
                if kind == "toilet":
                    # Fixtures along a shared wall belong to the named
                    # toilets either side of it: a toilet name within 3 m
                    # already accounts for them.
                    pool = _texts_for(it, rows, 3000.0)
                own = next((t for t in pool if (t["inside"] or kind == "toilet") and
                            _family_of_text(t["text"]) == family and family != "Unknown"), None)
                if own:
                    it["covered_by_text"] = own["text"]
                    named = " ".join(own["text"].split())[:40]
                    it["note"] = ((note + " - ") if note else "") + \
                        "named by text '{0}', drawn from the text".format(named)
            items.append(it)
            return it

        # 1. X regions
        for rg in regions:
            if not inside_floor(rg["bbox"]):
                continue
            lays = {raw[k][4] for k in rg["seg_idx"]}
            if rg["area_sqm"] > dr.X_VOID_AREA_THRESHOLD:
                add("x", rg["bbox"], rg["poly"], lays, "Void",
                    note="X over {0:g} sq.m".format(dr.X_VOID_AREA_THRESHOLD))
                continue
            it = add("x", rg["bbox"], rg["poly"], lays, None, near=NEAR_MM, note="X-marked")
            if it["family"] not in SHAFT_LIKE:
                # An X inside a room is that room's opening, not the room:
                # it keeps no room name.
                it["family"], it["label"], it["label_sr"] = "Shaft", "", None
        # A small X standing inside a bigger one is part of that shaft:
        # a shaft too, under its name.
        xs = [i for i in items if i["kind"] == "x" and i["family"] != "Void"]
        for a in xs:
            for b in xs:
                if a is b or a["area_sqm"] * 1.2 >= b["area_sqm"]:
                    continue
                ab, bb = a["bbox"], b["bbox"]
                cx, cy = (ab[0] + ab[2]) / 2, (ab[1] + ab[3]) / 2
                if bb[0] <= cx <= bb[2] and bb[1] <= cy <= bb[3]:
                    a["family"], a["note"] = "Shaft", "X inside another X"
                    if not a.get("label"):
                        a["label"] = b.get("label") or ""
                    break

        # 2. staircases
        for core in _stair_runs(segs, clip):
            if not inside_floor(core):
                continue
            b = _grow_to_walls(core, wall_set, idx, 80.0)
            if any(i["kind"] == "stair" and _iou(i["bbox"], b) >= 0.6 for i in items):
                continue
            add("stair", b, None, ["(tread lines)"], "Staircase", note="evenly spaced treads")

        # 3. toilets from fixtures
        fx = [f for f in fixtures if clip[0] <= f[0] <= clip[2] and clip[1] <= f[1] <= clip[3]
              and dr._point_in_polygon(f[0], f[1], poly)]
        for g in _fixture_clusters(fx):
            gx = [p[0] for p in g]
            gy = [p[1] for p in g]
            core = (min(gx) - 600, min(gy) - 600, max(gx) + 600, max(gy) + 600)
            b = _grow_to_walls(core, wall_set, idx, 150.0)
            if any(i["kind"] in ("toilet", "stair") and _iou(i["bbox"], b) >= 0.6 for i in items):
                continue
            add("toilet", b, None, ["(fixtures)"], "Toilet",
                note="{0} sanitary fixtures".format(len(g)))

        # 4. closed outlines, unless they are one of the above
        for o in outlines:
            b = o["bbox"]
            if not inside_floor(b) or o["area"] >= 0.9 * floor_sqm:
                continue
            lc = layer_counts.setdefault(name, {})
            lc[o["layer"]] = lc.get(o["layer"], 0) + 1
            if any(k in o["layer"].upper() for k in ("FLOOR",)) and o["area"] > 200:
                continue
            twin = next((i for i in items if _iou(i["bbox"], b) >= 0.8), None)
            if twin is not None:
                if o["layer"] not in twin["layers"]:
                    twin["layers"] = sorted(set(twin["layers"]) | {o["layer"]})
                continue
            it = add("outline", b, o["pts"], [o["layer"]], None, note="closed polyline")
            it["hidden_layer"] = o["hidden"]

        # 5. every space the walls enclose, unless it is one of the above
        # Doorless openings in real walls are closed here: a toilet open to
        # the corridor is still its own space.
        for pg in _spaces(wall_set + gaps, poly):
            pts = [tuple(p) for p in list(pg.exterior.coords)[:-1]]
            b = _bbox(pts)
            if any(_iou(i["bbox"], b) >= 0.8 for i in items):
                continue
            add("space", b, pts, [], None, note="enclosed by walls")

        for n, it in enumerate(items, start=1):
            it["id"] = "{0}-{1}".format(it["kind"], n)
        out[name] = items
    layers = {}
    for name, counts in layer_counts.items():
        layers[name] = [{"layer": k, "count": v, "visible": not layer_hidden_by_default(k)}
                        for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {"floors": out,
            "hanging_walls": [{"pts": [tuple(map(float, c)) for c in list(f["poly"].exterior.coords)[:-1]],
                               "evidence": f["evidence"], "layer": f["layer"], "area_sqm": f["area_sqm"]}
                              for f in hanging if f["poly"].geom_type == "Polygon"],
            "layers": layers,
            "families": [f["name"] for f in dr.list_families()],
            "group_order": GROUP_ORDER}
