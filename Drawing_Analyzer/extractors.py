# -*- coding: utf-8 -*-
"""Drawing-driven extractors that need no user input beyond the DXF.

All of these were possible with data the pipeline already collected and
threw away:

  * linear run length per layer, grouped into disciplines  - the raw
    primitive behind cable tray, duct and pipe length extraction;
  * block instance counts per name and category  - the mechanical core of
    lighting, sprinkler and plumbing fixture counting;
  * a door schedule with widths  - the gap measurement was already being
    computed inside _find_door_closing_lines and discarded after use;
  * wall linework length by layer and thickness class.

Everything is reported in metres, from millimetre-normalised coordinates.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

from .config import CONFIG
from .dxfio import iter_entities, iter_inserts
from .geometry import (MM_PER_M, arc_points, bulge_to_arc_points,
                       circle_points, dist, point_in_polygon)

R2 = lambda v: round(float(v), 2)   # noqa: E731
R3 = lambda v: round(float(v), 3)   # noqa: E731


def _classify(name: str, rules: Sequence[Dict], default="Other") -> str:
    up = (name or "").upper()
    for rule in rules:
        for kw in rule.get("keywords", []):
            if kw.upper() in up:
                return rule["name"]
    return default


# -- entity length ----------------------------------------------------------
def _lwpolyline_points_mm(e, xform, units, sag):
    """Vertices of an LWPOLYLINE in millimetres, with bulges flattened.

    The old reader dropped the bulge value, so arc-cornered polylines were
    measured and areas computed as if they were chorded.
    """
    try:
        raw = e.get_points("xyb")
    except TypeError:
        raw = [(p[0], p[1], 0.0) for p in e.get_points()]
    pts = []
    n = len(raw)
    if n == 0:
        return pts
    closed = bool(getattr(e, "closed", False))
    limit = n if closed else n - 1
    for i in range(n):
        x, y = raw[i][0], raw[i][1]
        wx, wy = xform.apply(x, y)
        pts.append(units.point_to_mm(wx, wy))
        if i < limit:
            b = raw[i][2] if len(raw[i]) > 2 else 0.0
            if b:
                j = (i + 1) % n
                nx, ny = xform.apply(raw[j][0], raw[j][1])
                p1 = pts[-1]
                p2 = units.point_to_mm(nx, ny)
                pts.extend(bulge_to_arc_points(p1, p2, b, sagitta_tol=sag))
    return pts


def _entity_length_mm(e, xform, units, sag) -> float:
    t = e.dxftype()
    scale = units.mm_per_unit * xform.scale_avg
    if t == "LINE":
        a = xform.apply(e.dxf.start.x, e.dxf.start.y)
        b = xform.apply(e.dxf.end.x, e.dxf.end.y)
        return dist(*units.point_to_mm(*a), *units.point_to_mm(*b))
    if t == "LWPOLYLINE":
        pts = _lwpolyline_points_mm(e, xform, units, sag)
        total = sum(dist(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
                    for i in range(len(pts) - 1))
        if getattr(e, "closed", False) and len(pts) > 2:
            total += dist(pts[-1][0], pts[-1][1], pts[0][0], pts[0][1])
        return total
    if t == "POLYLINE":
        pts = []
        for v in e.vertices:
            loc = v.dxf.location
            pts.append(units.point_to_mm(*xform.apply(loc.x, loc.y)))
        total = sum(dist(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
                    for i in range(len(pts) - 1))
        if getattr(e, "is_closed", False) and len(pts) > 2:
            total += dist(pts[-1][0], pts[-1][1], pts[0][0], pts[0][1])
        return total
    if t == "ARC":
        r = float(e.dxf.radius) * scale
        sweep = (float(e.dxf.end_angle) - float(e.dxf.start_angle)) % 360.0
        return math.radians(sweep or 360.0) * r
    if t == "CIRCLE":
        return 2.0 * math.pi * float(e.dxf.radius) * scale
    if t == "ELLIPSE":
        try:
            pts = [units.point_to_mm(*xform.apply(p.x, p.y))
                   for p in e.flattening(sag / max(scale, 1e-9))]
        except Exception:
            return 0.0
        return sum(dist(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
                   for i in range(len(pts) - 1))
    if t in ("SPLINE",):
        try:
            pts = [units.point_to_mm(*xform.apply(p.x, p.y))
                   for p in e.flattening(sag / max(scale, 1e-9))]
        except Exception:
            return 0.0
        return sum(dist(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
                   for i in range(len(pts) - 1))
    return 0.0


LINEAR_TYPES = "LINE LWPOLYLINE POLYLINE ARC CIRCLE ELLIPSE SPLINE"


# -- 1. Linear quantities by layer -----------------------------------------
def linear_lengths_by_layer(doc, msp, units, cfg: Dict = None,
                            floor_polygon=None) -> Dict:
    """Total run length of linework per layer, grouped into disciplines.

    This is the shared primitive behind cable tray length, duct length and
    pipe length extraction. The per-layer table is fully automatic; only
    the mapping of a layer to a discipline is a config rule.

    Note the caveat this cannot resolve on its own: walls and ducts drawn
    as two parallel faces are counted twice, because run length is
    measured on linework, not on centrelines. Reported as such.
    """
    cfg = cfg or CONFIG
    sag = float(cfg["curves"]["sagitta_tolerance_mm"])
    rules = cfg.get("linear_quantities", {}).get("disciplines", [])

    per_layer = {}
    for e, xform, _depth in iter_entities(doc, msp, LINEAR_TYPES):
        layer = (getattr(e.dxf, "layer", None) or "0")
        try:
            length = _entity_length_mm(e, xform, units, sag)
        except Exception:
            continue
        if length <= 0:
            continue
        if floor_polygon is not None:
            try:
                mid = _entity_midpoint_mm(e, xform, units)
                if mid and not point_in_polygon(mid[0], mid[1], floor_polygon):
                    continue
            except Exception:
                pass
        slot = per_layer.setdefault(layer, {"layer": layer, "length_mm": 0.0,
                                            "entities": 0, "types": {}})
        slot["length_mm"] += length
        slot["entities"] += 1
        slot["types"][e.dxftype()] = slot["types"].get(e.dxftype(), 0) + 1

    layers = []
    for slot in per_layer.values():
        layers.append({
            "layer": slot["layer"],
            "discipline": _classify(slot["layer"], rules, "Unclassified"),
            "length_m": R2(slot["length_mm"] / MM_PER_M),
            "entities": slot["entities"],
            "entity_types": ", ".join(sorted(slot["types"])),
        })
    layers.sort(key=lambda r: -r["length_m"])

    grouped = {}
    for row in layers:
        g = grouped.setdefault(row["discipline"],
                               {"discipline": row["discipline"],
                                "length_m": 0.0, "layers": 0, "entities": 0})
        g["length_m"] += row["length_m"]
        g["layers"] += 1
        g["entities"] += row["entities"]
    disciplines = sorted(grouped.values(), key=lambda r: -r["length_m"])
    for d in disciplines:
        d["length_m"] = R2(d["length_m"])

    return {
        "layers": layers,
        "disciplines": disciplines,
        "total_length_m": R2(sum(r["length_m"] for r in layers)),
        "caveat": ("Run length is measured on linework. Anything drawn as two "
                   "parallel faces (most walls, some ducts) is counted twice. "
                   "Use this as a layer inventory until centreline pairing is "
                   "added."),
    }


def _entity_midpoint_mm(e, xform, units):
    t = e.dxftype()
    if t == "LINE":
        a = xform.apply(e.dxf.start.x, e.dxf.start.y)
        b = xform.apply(e.dxf.end.x, e.dxf.end.y)
        return units.point_to_mm((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    if t in ("ARC", "CIRCLE"):
        c = xform.apply(e.dxf.center.x, e.dxf.center.y)
        return units.point_to_mm(*c)
    if t == "LWPOLYLINE":
        pts = e.get_points()
        if not pts:
            return None
        p = pts[len(pts) // 2]
        return units.point_to_mm(*xform.apply(p[0], p[1]))
    if t == "POLYLINE":
        vs = list(e.vertices)
        if not vs:
            return None
        loc = vs[len(vs) // 2].dxf.location
        return units.point_to_mm(*xform.apply(loc.x, loc.y))
    return None


# -- 2. Wall linework by thickness ------------------------------------------
def wall_lengths(doc, msp, units, cfg: Dict = None) -> Dict:
    """Wall linework length split into thick and thin classes.

    Uses the same lineweight threshold and the same layer exclusion lists
    that the boundary generator already applies, so 'thick' here means
    exactly what it means to the ray-caster.
    """
    from .dxf_reader import _HARD_EXCLUDE_KEYWORDS, _WALL_EXCLUDE_KEYWORDS

    cfg = cfg or CONFIG
    sag = float(cfg["curves"]["sagitta_tolerance_mm"])
    thick_at = int(cfg["boundary"]["thick_lineweight_threshold"])

    rows = {}
    for e, xform, _d in iter_entities(doc, msp, "LINE LWPOLYLINE POLYLINE"):
        layer = (getattr(e.dxf, "layer", None) or "0")
        up = layer.upper()
        if any(k in up for k in _HARD_EXCLUDE_KEYWORDS):
            continue
        excluded = any(k in up for k in _WALL_EXCLUDE_KEYWORDS)
        try:
            lw = int(getattr(e.dxf, "lineweight", -1) or -1)
        except (TypeError, ValueError):
            lw = -1
        cls = "Thick (structural)" if lw >= thick_at and not excluded else "Thin / other"
        try:
            length = _entity_length_mm(e, xform, units, sag)
        except Exception:
            continue
        if length <= 0:
            continue
        key = (layer, cls)
        slot = rows.setdefault(key, {"layer": layer, "class": cls,
                                     "length_mm": 0.0, "entities": 0})
        slot["length_mm"] += length
        slot["entities"] += 1

    out = [{"layer": v["layer"], "class": v["class"],
            "length_m": R2(v["length_mm"] / MM_PER_M), "entities": v["entities"]}
           for v in rows.values()]
    out.sort(key=lambda r: -r["length_m"])
    thick = R2(sum(r["length_m"] for r in out if r["class"].startswith("Thick")))
    thin = R2(sum(r["length_m"] for r in out if not r["class"].startswith("Thick")))
    return {
        "rows": out,
        "thick_length_m": thick,
        "thin_length_m": thin,
        "total_length_m": R2(thick + thin),
        "caveat": ("Face lines, not centrelines: a wall drawn as two parallel "
                   "lines contributes roughly twice its true length. Treat as "
                   "linework inventory, not finished wall length."),
    }


# -- 3. Block inventory -----------------------------------------------------
def block_inventory(doc, msp, units, cfg: Dict = None,
                    floors: Sequence[Dict] = None) -> Dict:
    """Count every INSERT by block name, categorised by keyword rules.

    This is the mechanical core of lighting fixture counting, sprinkler
    counting, plumbing fixture counting and door/window scheduling. The
    raw inventory needs no input at all; only the category mapping is a
    config rule.
    """
    cfg = cfg or CONFIG
    rules = cfg.get("block_inventory", {}).get("categories", [])
    floor_polys = [(f["name"], [tuple(p) for p in f["polygon"]])
                   for f in (floors or []) if f.get("polygon")]

    counts = {}
    for ins, xform, _d in iter_inserts(doc, msp):
        name = ins.dxf.name or "(unnamed)"
        pos = units.point_to_mm(xform.ix, xform.iy)
        floor_name = ""
        for fname, poly in floor_polys:
            if point_in_polygon(pos[0], pos[1], poly):
                floor_name = fname
                break
        if floor_polys and not floor_name:
            continue
        key = (name, floor_name)
        slot = counts.setdefault(key, {
            "block": name,
            "floor": floor_name,
            "category": _classify(name, rules, "Unclassified"),
            "layer": getattr(ins.dxf, "layer", "") or "",
            "count": 0,
        })
        slot["count"] += 1

    rows = sorted(counts.values(), key=lambda r: (-r["count"], r["block"]))
    by_cat = {}
    for r in rows:
        c = by_cat.setdefault(r["category"], {"category": r["category"],
                                              "count": 0, "distinct_blocks": 0})
        c["count"] += r["count"]
        c["distinct_blocks"] += 1
    categories = sorted(by_cat.values(), key=lambda r: -r["count"])

    return {
        "rows": rows,
        "categories": categories,
        "total_instances": sum(r["count"] for r in rows),
        "distinct_blocks": len({r["block"] for r in rows}),
    }


# -- 4. Door schedule -------------------------------------------------------
def door_schedule(doc, msp, units, wall_segs_mm, cfg: Dict = None,
                  rooms: Sequence[Dict] = None) -> Dict:
    """Door count and clear width per door block.

    The width measurement already existed inside _find_door_closing_lines,
    where it was used to seal openings before ray-casting and then thrown
    away. Here it is kept: the gap between the two wall endpoints a door
    bridges is the door's clear opening.

    Falls back to the block's own extents when no wall pair is found, and
    marks which method produced each number so the user can see how much
    to trust it.
    """
    cfg = cfg or CONFIG
    d = cfg["doors"]
    radius = float(d["insert_radius_mm"])
    gap_min, gap_max = float(d["gap_min_mm"]), float(d["gap_max_mm"])
    rules = cfg.get("block_inventory", {}).get("categories", [])

    endpoints = []
    for sx, sy, ex, ey in (wall_segs_mm or []):
        endpoints.append((sx, sy))
        endpoints.append((ex, ey))

    room_polys = [(r.get("name") or "Unknown", r["pts"])
                  for r in (rooms or []) if r.get("pts")]

    doors = []
    for ins, xform, _d in iter_inserts(doc, msp):
        name = ins.dxf.name or ""
        if _classify(name, rules, "") != "Door":
            continue
        px, py = units.point_to_mm(xform.ix, xform.iy)

        near = [(dist(px, py, ex, ey), ex, ey) for ex, ey in endpoints
                if abs(ex - px) <= radius and abs(ey - py) <= radius]
        near.sort(key=lambda t: t[0])
        near = near[:12]

        width_mm, method = None, "unmeasured"
        best = None
        for i in range(len(near)):
            for j in range(i + 1, len(near)):
                _, ax, ay = near[i]
                _, bx, by = near[j]
                g = dist(ax, ay, bx, by)
                if not (gap_min <= g <= gap_max):
                    continue
                mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
                off = dist(px, py, mx, my)
                if best is None or off < best[0]:
                    best = (off, g)
        if best:
            width_mm, method = best[1], "wall gap"
        else:
            ext = _block_extents_mm(doc, ins, xform, units)
            if ext:
                width_mm, method = max(ext), "block extents"

        room_name = ""
        for rn, poly in room_polys:
            if point_in_polygon(px, py, poly):
                room_name = rn
                break

        doors.append({
            "block": name,
            "layer": getattr(ins.dxf, "layer", "") or "",
            "room": room_name,
            "width_mm": int(round(width_mm)) if width_mm else None,
            "nominal_width_mm": _nominal(width_mm),
            "method": method,
            "x": R2(px), "y": R2(py),
        })

    by_size = {}
    for dr in doors:
        k = (dr["block"], dr["nominal_width_mm"])
        s = by_size.setdefault(k, {"block": dr["block"],
                                   "nominal_width_mm": dr["nominal_width_mm"],
                                   "count": 0})
        s["count"] += 1
    schedule = sorted(by_size.values(), key=lambda r: (-r["count"], r["block"]))

    measured = sum(1 for dr in doors if dr["method"] == "wall gap")
    return {
        "doors": doors,
        "schedule": schedule,
        "total_doors": len(doors),
        "measured_from_walls": measured,
        "unmeasured": sum(1 for dr in doors if dr["width_mm"] is None),
        "note": ("Width is the clear gap between the wall endpoints the door "
                 "bridges. Where no wall pair was found, the block's own "
                 "extents are used instead - those are less reliable and are "
                 "marked in the method column."),
    }


_STD_WIDTHS = [600, 700, 750, 800, 900, 1000, 1050, 1200, 1500, 1800, 2100, 2400]


def _nominal(width_mm):
    if not width_mm:
        return None
    return min(_STD_WIDTHS, key=lambda w: abs(w - width_mm))


def _block_extents_mm(doc, ins, xform, units):
    name = ins.dxf.name
    if not name or name not in doc.blocks:
        return None
    xs, ys = [], []
    for e in doc.blocks[name].query("LINE LWPOLYLINE POLYLINE ARC CIRCLE"):
        t = e.dxftype()
        try:
            if t == "LINE":
                pts = [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
            elif t == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in e.get_points()]
            elif t == "POLYLINE":
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
            elif t in ("ARC", "CIRCLE"):
                r = float(e.dxf.radius)
                cx, cy = e.dxf.center.x, e.dxf.center.y
                pts = [(cx - r, cy - r), (cx + r, cy + r)]
            else:
                continue
        except AttributeError:
            continue
        for x, y in pts:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    scale = units.mm_per_unit * xform.scale_avg
    return ((max(xs) - min(xs)) * scale, (max(ys) - min(ys)) * scale)
