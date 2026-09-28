# -*- coding: utf-8 -*-
"""Read boundaries back out of an annotated DXF.

The rule preview needs to answer "what would this do to what I am looking
at" in a couple of seconds. Regenerating the drawing to find out takes a
minute and a half on a real plate, so this reads the boundaries the last
run already wrote instead - which is also exactly the geometry on screen,
rather than a fresh run that might differ for unrelated reasons.
"""

from __future__ import annotations

import math

import ezdxf

from .dxf_reader import family_from_layer, _FAMILY_RULES
from .units import DrawingUnits

# ROOM_BOUNDARY carries the floor outline, not a room, and the drawing
# pass never hands it to the rules either.
SKIP_LAYERS = {"ROOM_VOID", "ROOM_BOUNDARY"}


def _area_sqm(points):
    total = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2e6


def read_drawn_boundaries(path, units=None):
    """Every boundary in an annotated DXF, in the shape userrules wants.

    Labels are matched by containment, smallest boundary first, because
    the label of a shaft standing inside a room sits inside both and the
    smaller one owns it.
    """
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    to_mm = (units.to_mm if isinstance(units, DrawingUnits)
             else (lambda v: v))

    labels = []
    for e in msp.query("MTEXT"):
        layer = e.dxf.layer
        if not layer.endswith("_BOUNDARY"):
            continue
        labels.append({"layer": layer, "x": to_mm(e.dxf.insert.x),
                       "y": to_mm(e.dxf.insert.y), "text": e.text,
                       "used": False})

    items = []
    for e in msp.query("LWPOLYLINE POLYLINE"):
        layer = e.dxf.layer
        if not layer.endswith("_BOUNDARY") or layer in SKIP_LAYERS:
            continue
        try:
            pts = [(float(to_mm(p[0])), float(to_mm(p[1])))
                   for p in e.get_points()]
        except (AttributeError, TypeError):
            continue
        if len(pts) < 3:
            continue
        # A closed polyline repeats its first point; the rules work on a
        # ring, so drop the repeat.
        if len(pts) > 3 and abs(pts[0][0] - pts[-1][0]) < 1e-6 \
                and abs(pts[0][1] - pts[-1][1]) < 1e-6:
            pts = pts[:-1]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        fam = family_from_layer(layer, _FAMILY_RULES)
        items.append({
            "shape": pts,
            "bbox": bbox,
            "label": "",
            "family": fam["name"],
            "layer": layer,
            "source": "box" if len(pts) <= 4 else "traced",
            "area_sqm": float(_area_sqm(pts)),
        })

    for item in sorted(items, key=lambda i: i["area_sqm"]):
        best, best_d = None, None
        cx = (item["bbox"][0] + item["bbox"][2]) / 2.0
        cy = (item["bbox"][1] + item["bbox"][3]) / 2.0
        for lab in labels:
            if lab["used"] or lab["layer"] != item["layer"]:
                continue
            if not (item["bbox"][0] - 1 <= lab["x"] <= item["bbox"][2] + 1
                    and item["bbox"][1] - 1 <= lab["y"] <= item["bbox"][3] + 1):
                continue
            d = math.hypot(lab["x"] - cx, lab["y"] - cy)
            if best is None or d < best_d:
                best, best_d = lab, d
        if best is not None:
            best["used"] = True
            item["label"] = best["text"]
    return items
