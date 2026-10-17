# -*- coding: utf-8 -*-
"""Gross area to the outer face of the facade, when the floor outline is
the slab's.

A floor outline drawn on a slab-projection layer (285 FD Tower 2: one
5,207.90 sq.m outline per floor on A-FLOOR PROJ) runs to the slab edge.
The area statement's gross "inc glass" runs to the outer face of the
glazing instead: 600-700 mm in from the slab edge along most of the
facade on floors 1-12, so 128.92 sq.m less (5,078.98). The glass line is
drawn - thousands of mullion and panel lines - but never as one closed
outline.

So the slab is rasterised with every facade line (glass, walls, columns,
fins, doors, railings) as a barrier, small gaps closed, and filled from
outside the slab: what the fill reaches inside the slab lies outside the
facade. Gross = the slab less that band, the barrier band along it
counted a quarter in (the take-off line runs on the glass, not beyond
it). Checked on 285 FD Tower 1, where take-off outlines exist: 5,078.97
against 5,078.98 (11th floor), 5,079.3 against 5,078.98 (typical floor),
and the slab itself where the glass stands at the slab edge (13th floor
up). A fill that leaks into the building (a facade opening wider than the
gap closing) gives no answer and the slab stays the gross.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .config import CONFIG

_FC = CONFIG.get("facade", {}) if isinstance(CONFIG, dict) else {}

ENABLED = bool(_FC.get("enabled", True))
# Floor outlines on these layers run to the slab edge.
SLAB_LAYER_WORDS = [str(w).upper() for w in _FC.get("slab_layer_words", ["FLOOR PROJ", "SLAB EDGE", "SLAB OUTLINE"])]
# Layers whose lines make the facade.
FACADE_LAYER_WORDS = [str(w).upper() for w in _FC.get("facade_layer_words", [
    "GLASS", "GLAZ", "CURTAIN", "MULLION", "WALL", "COLUMN", "COL", "FIN", "RCC", "SLIP FORM",
    "DOOR", "WINDOW", "RAIL", "UPSTAND", "WL_EXT", "WL-EXT", "LOUV"])]
NOT_FACADE_WORDS = [str(w).upper() for w in _FC.get("not_facade_words", [
    "DIM", "TEXT", "HATCH", "FURN", "GRID", "CENTRE", "CENTER", "FLOOR PROJ", "BOUNDARY", "AI_"])]
CELL_MM = float(_FC.get("cell_mm", 25.0))
CLOSE_MM = float(_FC.get("gap_close_mm", 100.0))
BAND_SHARE = float(_FC.get("band_share", 0.25))
MAX_OUTSIDE_SHARE = float(_FC.get("max_outside_share", 0.30))
SAME_AS_SLAB_SQM = float(_FC.get("same_as_slab_sqm", 0.5))
MAX_CELLS = int(_FC.get("max_cells", 12_000_000))


def is_slab_layer(layer: str) -> bool:
    t = " ".join(re.sub(r"[^A-Z0-9]+", " ", str(layer or "").upper()).split())
    return any(w in t for w in SLAB_LAYER_WORDS)


def is_facade_layer(layer: str) -> bool:
    t = str(layer or "").upper()
    t = re.split(r"[$|]", t)[-1]
    if any(w in t for w in NOT_FACADE_WORDS):
        return False
    return any(w in t for w in FACADE_LAYER_WORDS)


def facade_segments(doc, msp, units, boxes: Sequence[Tuple[float, float, float, float]]):
    """[(x1, y1, x2, y2)] in mm of every line on a facade layer that lies
    within one of the boxes (mm), blocks included."""
    from .dxfio import iter_entities, effective_layer
    from . import dxf_reader as dr
    out = []

    def inside(x, y):
        return any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in boxes)

    for e, xf, _d in iter_entities(doc, msp, "LINE LWPOLYLINE POLYLINE ARC CIRCLE"):
        layer = effective_layer(e, xf) or ""
        if not is_facade_layer(layer):
            continue
        try:
            t = e.dxftype()
            if t == "LINE":
                pts = [units.point_to_mm(*xf.apply(e.dxf.start.x, e.dxf.start.y)),
                       units.point_to_mm(*xf.apply(e.dxf.end.x, e.dxf.end.y))]
            elif t in ("ARC", "CIRCLE"):
                from ezdxf.path import make_path
                pts = [units.point_to_mm(*xf.apply(v.x, v.y)) for v in make_path(e).flattening(20)]
            else:
                pts = dr._polyline_points(e, units, xf)
                if dr._is_closed(e) and len(pts) > 2:
                    pts = list(pts) + [pts[0]]
        except Exception:                             # noqa: BLE001
            continue
        if not pts or not any(inside(x, y) for x, y in pts):
            continue
        for a, b in zip(pts, pts[1:]):
            out.append((a[0], a[1], b[0], b[1]))
    return out


def glass_face_area(slab_pts, segs, cell: float = None, close_mm: float = None) -> Optional[Dict]:
    """{"area_sqm", "outside_sqm", "slab_sqm"} for one slab outline, or None
    when it cannot be told (no facade drawn, or the fill leaks inside)."""
    import numpy as np
    from shapely.geometry import Polygon
    import shapely
    try:
        from scipy import ndimage
    except ImportError:
        return None
    cell = float(cell or CELL_MM)
    close = max(1, int(round(float(close_mm or CLOSE_MM) / cell)))
    slab = Polygon(slab_pts).buffer(0)
    if slab.is_empty or slab.area <= 0:
        return None
    x0, y0, x1, y1 = slab.bounds
    m = max(1000.0, 4 * close * cell)
    W = int((x1 - x0 + 2 * m) / cell) + 1
    H = int((y1 - y0 + 2 * m) / cell) + 1
    if W * H > MAX_CELLS:
        cell *= math.sqrt(W * H / MAX_CELLS)
        close = max(1, int(round(float(close_mm or CLOSE_MM) / cell)))
        W = int((x1 - x0 + 2 * m) / cell) + 1
        H = int((y1 - y0 + 2 * m) / cell) + 1
    bar = np.zeros((H, W), bool)
    ox, oy = x0 - m, y0 - m
    n = 0
    for sx, sy, ex, ey in segs:
        if max(sx, ex) < ox or min(sx, ex) > ox + W * cell or max(sy, ey) < oy or min(sy, ey) > oy + H * cell:
            continue
        L = math.hypot(ex - sx, ey - sy)
        k = max(2, int(L / (cell * 0.5)) + 1)
        t = np.linspace(0.0, 1.0, k)
        cs = ((sx + (ex - sx) * t - ox) / cell).astype(int)
        rs = ((sy + (ey - sy) * t - oy) / cell).astype(int)
        ok = (rs >= 0) & (rs < H) & (cs >= 0) & (cs < W)
        bar[rs[ok], cs[ok]] = True
        n += 1
    if n == 0:
        return None
    grown = ndimage.binary_dilation(bar, iterations=close)
    lab, _ = ndimage.label(~grown)
    out_label = lab[0, 0]
    if out_label == 0:
        return None
    reach = ndimage.binary_dilation(lab == out_label, iterations=close) & ~bar
    yy, xx = np.mgrid[0:H, 0:W]
    ins = shapely.contains_xy(slab, (xx + 0.5) * cell + ox, (yy + 0.5) * cell + oy)
    outside = (reach & ins).sum() * cell * cell / 1e6
    slab_sqm = slab.area / 1e6
    if outside > MAX_OUTSIDE_SHARE * slab_sqm:
        return None                                   # the fill got into the building
    if outside <= SAME_AS_SLAB_SQM:
        return {"area_sqm": slab_sqm, "outside_sqm": 0.0, "slab_sqm": slab_sqm, "cells": int(cell)}
    enc = ~reach & ins
    band = enc & bar & ndimage.binary_dilation(reach, iterations=1)
    area = slab_sqm - outside - BAND_SHARE * band.sum() * cell * cell / 1e6
    return {"area_sqm": area, "outside_sqm": slab_sqm - area, "slab_sqm": slab_sqm, "cells": int(cell)}


def floors_glass_face(doc, msp, units, floors: List[Dict]) -> Dict[str, Dict]:
    """{floor name: glass_face_area result} for the floors whose outline is
    on a slab layer (f["layer"])."""
    todo = [f for f in floors if ENABLED and is_slab_layer(f.get("layer"))]
    if not todo:
        return {}
    boxes = []
    for f in todo:
        xs = [p[0] for p in f["pts"]]
        ys = [p[1] for p in f["pts"]]
        boxes.append((min(xs) - 2000, min(ys) - 2000, max(xs) + 2000, max(ys) + 2000))
    segs = facade_segments(doc, msp, units, boxes)
    out = {}
    for f, b in zip(todo, boxes):
        mine = [s for s in segs if b[0] <= s[0] <= b[2] and b[1] <= s[1] <= b[3]]
        try:
            r = glass_face_area(f["pts"], mine)
        except Exception:                             # noqa: BLE001
            r = None
        if r is not None:
            out[f["name"]] = r
    return out
