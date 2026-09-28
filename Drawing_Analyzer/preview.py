# -*- coding: utf-8 -*-
"""Boundary preview.

Turns the boundary layers of an annotated drawing into a small SVG that the
browser can pan, zoom and interrogate.

Rendering the whole DXF is not worth it: a 31 MB drawing takes about 16
seconds and produces a 16 MB image, and the walls underneath are not what
anyone is checking after Step 3. Only the generated boundaries, the floor
outline and the deducted voids are drawn, which is half a second and a few
hundred kilobytes, and answers the real question - did every room get a
boundary, and did it land on the right family layer.

The geometry is emitted one group per layer so the UI can toggle families,
and each polygon carries its room name and area for the hover readout.
"""

from __future__ import annotations

import os

from .dxf_reader import (ANNOTATE_LAYER, MIN_FLOOR_AREA_SQM, VOID_LAYER,
                         _FAMILY_RULES, floor_tag,
                         _is_closed, _point_in_polygon, _polyline_points,
                         _text_position, _text_value, family_from_layer)
from .dxfio import iter_entities, load_document
from .geometry import shoelace_area_sqm

# ACI index -> a colour that stays legible on a light page. This is
# deliberately not the raw AutoCAD palette: ACI 2 (yellow) and ACI 7
# (white) are close to invisible on white, and the point of the preview is
# to be read on screen, not to match a plot.
_ACI_WEB = {
    1: "#e03131",    # red
    2: "#b8860b",    # yellow -> amber
    3: "#2f9e44",    # green
    4: "#0c8599",    # cyan
    5: "#3b5bdb",    # blue
    6: "#c2255c",    # magenta
    7: "#343a40",    # white/black -> near black
    8: "#868e96",    # dark grey
    9: "#adb5bd",    # light grey
    30: "#e8590c",   # orange
    41: "#8b5a2b",   # brown
    51: "#5c940d",   # light green
    140: "#4dabf7",  # light blue
    150: "#ae3ec9",  # light magenta
    210: "#f06595",  # pink
}
_FALLBACK_COLOR = "#495057"
_FLOOR_COLOR = "#1f3a5f"
_VOID_COLOR = "#868e96"

# Anything smaller than this is a drafting artefact, not a room, and only
# adds noise and file size to the preview.
_MIN_AREA_SQM = 0.05


def _color_for(aci):
    return _ACI_WEB.get(int(aci or 7), _FALLBACK_COLOR)


def _esc(text):
    return (str(text)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# Underlay tuning. The drawing is context, not the subject: it is one flat
# grey, clipped to the area the boundaries occupy, and capped so a pathological
# drawing cannot push tens of megabytes at the browser.
_BASE_COLOR = "#b9c2cf"
_BASE_MARGIN = 0.06          # widen the clip past the rooms by this fraction
_BASE_MAX_POINTS = 350000
_ARC_SAGITTA_MM = 12.0       # arc flattening tolerance
# Detail below roughly a third of a screen pixel cannot be seen, so the
# threshold follows the view instead of being a fixed number of
# millimetres: a single floor keeps its detail, while a whole-site sheet
# spanning 400 m drops the furniture scribble that would otherwise cost
# four times the payload for nothing.
_BASE_DETAIL_DIVISOR = 3000
_BASE_MIN_SEG_FLOOR_MM = 2.0


def _collect_base(doc, msp, units, clip):
    """Drawing linework under the boundaries, as SVG path data.

    Polylines stay whole rather than being exploded into segments: one
    "M x y L x y L x y" costs about half of the same run written as
    separate two-point moves, and these drawings carry ~430,000 segments.
    Everything is one colour, so it needs no per-layer grouping either.
    """
    minx, miny, maxx, maxy = clip
    mx = (maxx - minx) * _BASE_MARGIN
    my = (maxy - miny) * _BASE_MARGIN
    minx, miny, maxx, maxy = minx - mx, miny - my, maxx + mx, maxy + my
    min_seg = max(_BASE_MIN_SEG_FLOOR_MM,
                  max(maxx - minx, maxy - miny) / _BASE_DETAIL_DIVISOR)

    chunks = []
    points = 0
    truncated = False

    def emit(pts):
        """Write one run of points, dropping anything outside the clip."""
        nonlocal points, truncated
        if points >= _BASE_MAX_POINTS:
            truncated = True
            return
        run = []
        last = None
        for x, y in pts:
            if not (minx <= x <= maxx and miny <= y <= maxy):
                # Break the run rather than drawing a line across the clip.
                if len(run) > 1:
                    _write(chunks, run)
                    points += len(run)
                run = []
                last = None
                continue
            p = (int(round(x)), int(round(y)))
            if last is not None and abs(p[0] - last[0]) + abs(p[1] - last[1]) \
                    < min_seg:
                continue
            run.append(p)
            last = p
        if len(run) > 1:
            _write(chunks, run)
            points += len(run)

    for e, xform, _depth in iter_entities(
            doc, msp, "LINE LWPOLYLINE POLYLINE ARC CIRCLE"):
        layer = (e.dxf.layer or "0").upper()
        # The generated boundaries are drawn separately and in colour.
        if layer.endswith("_BOUNDARY") or layer.endswith("_VOID"):
            continue
        try:
            kind = e.dxftype()
            if kind == "LINE":
                emit([units.point_to_mm(*xform.apply(e.dxf.start.x,
                                                     e.dxf.start.y)),
                      units.point_to_mm(*xform.apply(e.dxf.end.x,
                                                     e.dxf.end.y))])
            elif kind in ("ARC", "CIRCLE"):
                emit([units.point_to_mm(*xform.apply(v.x, v.y))
                      for v in e.flattening(_ARC_SAGITTA_MM / 1000.0)])
            else:
                pts = _polyline_points(e, units, xform)
                if _is_closed(e) and len(pts) > 1:
                    pts = pts + [pts[0]]
                emit(pts)
        except (AttributeError, TypeError, ValueError, ZeroDivisionError):
            continue
        if points >= _BASE_MAX_POINTS:
            truncated = True
            break

    return "".join(chunks), points, truncated


def _write(chunks, run):
    out = ["M{0} {1}".format(run[0][0], run[0][1])]
    for x, y in run[1:]:
        out.append("L{0} {1}".format(x, y))
    chunks.append("".join(out))


def _ends_meet(pts, tol=1.0):
    """True when the last vertex lands back on the first (within 1 mm)."""
    return (abs(pts[0][0] - pts[-1][0]) <= tol
            and abs(pts[0][1] - pts[-1][1]) <= tol)


def _points_attr(pts):
    """Integer millimetres, with consecutive duplicates dropped.

    Traced outlines carry a vertex per wall cell; at preview scale the
    sub-millimetre detail is invisible and just inflates the payload.
    """
    out = []
    last = None
    for x, y in pts:
        p = (int(round(x)), int(round(y)))
        if p != last:
            out.append(p)
            last = p
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return " ".join("{0},{1}".format(x, y) for x, y in out), out


def build_preview(dxf_path, unit_override=None, with_base=False):
    """Read an annotated DXF and return the SVG plus its legend.

    with_base also draws the original drawing underneath, so the
    boundaries can be checked against the walls they were traced from.
    It is off by default because it is the expensive half.
    """
    doc, msp, units = load_document(dxf_path, unit_override)

    shapes = []          # every boundary polygon, grouped later by layer
    voids = []
    texts = []           # (layer, x, y, value)

    for e in msp.query("LWPOLYLINE POLYLINE"):
        layer = (e.dxf.layer or "0")
        upper = layer.upper()
        is_void = upper == VOID_LAYER.upper() or upper.endswith("_VOID")
        if not (is_void or upper.endswith("_BOUNDARY")):
            continue
        pts = _polyline_points(e, units)
        if len(pts) < 3:
            continue
        # The floor outline is written with its first point repeated rather
        # than the closed flag set, so testing the flag alone loses it.
        if not (_is_closed(e) or _ends_meet(pts)):
            continue
        area = shoelace_area_sqm(pts)
        if area < _MIN_AREA_SQM:
            continue
        (voids if is_void else shapes).append(
            {"layer": layer, "pts": pts, "area": float(area),
             "floor": floor_tag(e)})

    for e in msp.query("TEXT MTEXT"):
        layer = (e.dxf.layer or "0")
        if not layer.upper().endswith("_BOUNDARY"):
            continue
        value = _text_value(e)
        if not value:
            continue
        # _text_position gives raw drawing units; the polygons are in mm.
        x, y = units.point_to_mm(*_text_position(e))
        texts.append((layer.upper(), x, y, value))

    if not shapes:
        raise ValueError(
            "This drawing has no closed polylines on a '*_BOUNDARY' layer, so "
            "there is nothing to preview. Run Generate Boundaries first.")

    # The floor outlines are written to the same legacy layer as the
    # Habitable Room family. Each carries its Step 2 floor name, so a sheet
    # with several floors shows every one of them as a floor; a drawing
    # made before the name was stored falls back to the largest outline.
    outline_layer = ANNOTATE_LAYER.upper()
    floors = [s for s in shapes if s.get("floor")]
    if not floors:
        on_outline = [s for s in shapes if s["layer"].upper() == outline_layer]
        floors = [s for s in on_outline if s["area"] >= MIN_FLOOR_AREA_SQM]
        if not floors and on_outline:
            floors = [max(on_outline, key=lambda s: s["area"])]
    for f in floors:
        shapes.remove(f)
    floor = floors

    _name_shapes(shapes, texts)

    # The site outline can dwarf the rooms, so the opening view frames the
    # boundaries and "Fit all" reaches the outline.
    groups, full, focus = _build_groups(shapes, voids, floor)

    base, base_points, base_truncated = "", 0, False
    if with_base:
        base, base_points, base_truncated = _collect_base(doc, msp, units, focus)

    svg = _svg(groups, focus, full, base)
    minx, miny, maxx, maxy = full

    return {
        "svg": svg,
        "layers": [
            {"layer": g["layer"], "family": g["family"], "color": g["color"],
             "count": len(g["shapes"]),
             "area_sqm": round(float(sum(s["area"] for s in g["shapes"])), 2),
             "kind": g["kind"]}
            for g in groups
        ],
        "boundary_count": len(shapes),
        "void_count": len(voids),
        "has_base": bool(base),
        "base_points": base_points,
        "base_truncated": base_truncated,
        "floor_area_sqm": (round(float(sum(f["area"] for f in floors)), 2)
                           if floors else None),
        "floor_count": len(floors),
        "units": units.to_ui(),
        "extent_m": [round(float(maxx - minx) / 1000.0, 1),
                     round(float(maxy - miny) / 1000.0, 1)],
    }


def build_preview_from_shapes(shapes, floor=None, voids=()):
    """The same picture, from shapes held in memory rather than a file.

    The rule preview needs to show what the rules would leave behind, and
    that geometry only exists in the request - it is never written to a
    DXF. Everything from grouping onwards is shared with build_preview,
    so the two pictures are drawn by the same code and cannot drift.

    `shapes` are dicts of {layer, pts, area, name}.
    """
    if not shapes:
        raise ValueError("These rules leave nothing to draw.")
    groups, full, focus = _build_groups(list(shapes), list(voids), floor)
    svg = _svg(groups, focus, full, "")
    minx, miny, maxx, maxy = full
    return {
        "svg": svg,
        "layers": [
            {"layer": g["layer"], "family": g["family"], "color": g["color"],
             "count": len(g["shapes"]),
             "area_sqm": round(float(sum(s["area"] for s in g["shapes"])), 2),
             "kind": g["kind"]}
            for g in groups
        ],
        "boundary_count": len(shapes),
        "void_count": len(voids),
        "has_base": False,
        "floor_area_sqm": (round(float(sum(f["area"] for f in floor)), 2)
                           if isinstance(floor, list) and floor else
                           round(float(floor["area"]), 2) if floor else None),
        "extent_m": [round(float(maxx - minx) / 1000.0, 1),
                     round(float(maxy - miny) / 1000.0, 1)],
    }


def _name_shapes(shapes, texts):
    """Attach the room label that sits inside each boundary.

    annotate_dxf writes the label onto the same layer as the boundary at
    the label's own position, so matching by layer first keeps this cheap
    and stops a neighbouring room's title leaking in.
    """
    by_layer = {}
    for layer, x, y, value in texts:
        by_layer.setdefault(layer, []).append((x, y, value))

    for shape in sorted(shapes, key=lambda s: s["area"]):
        candidates = by_layer.get(shape["layer"].upper())
        if not candidates:
            continue
        for i, (x, y, value) in enumerate(candidates):
            if _point_in_polygon(x, y, shape["pts"]):
                shape["name"] = value
                candidates.pop(i)   # one label can only name one room
                break


def _build_groups(shapes, voids, floor):
    """One SVG group per layer, ordered largest-area-last so small rooms
    stay clickable on top of the big ones.

    `floor` is one floor outline or a list of them."""
    floors = [f for f in (floor if isinstance(floor, list) else [floor]) if f]
    by_layer = {}
    for s in shapes:
        by_layer.setdefault(s["layer"].upper(), []).append(s)

    groups = []
    for layer, items in by_layer.items():
        fam = family_from_layer(layer, _FAMILY_RULES)
        items.sort(key=lambda s: -s["area"])
        groups.append({"layer": layer, "family": fam["name"],
                       "color": _color_for(fam.get("color")),
                       "kind": "family", "shapes": items})
    groups.sort(key=lambda g: g["family"])

    if voids:
        groups.append({"layer": VOID_LAYER, "family": "Interior voids",
                       "color": _VOID_COLOR, "kind": "void", "shapes": voids})
    if floors:
        groups.insert(0, {"layer": ANNOTATE_LAYER, "family": "Floor outline",
                          "color": _FLOOR_COLOR, "kind": "floor",
                          "shapes": floors})

    # Several layers can map to one family - UNKNOWN_BOUNDARY and the
    # legacy ROOM_BOUNDARY both read as Unknown - and two legend entries
    # with the same word on them are unreadable.
    seen = {}
    for g in groups:
        seen[g["family"]] = seen.get(g["family"], 0) + 1
    for g in groups:
        if seen[g["family"]] > 1:
            g["family"] = "{0} ({1})".format(g["family"], g["layer"])

    full = _bbox(groups)
    rooms = [g for g in groups if g["kind"] != "floor"]

    # A drawing that has been round-tripped through CAD often still carries
    # boundary polylines from an earlier run, sometimes a kilometre away.
    # One stray half-metre shape like that is enough to open the preview on
    # an empty field, so the opening view is framed on the plate.
    focus = None
    if floors:
        boxes = [_shape_bbox(f) for f in floors]
        fx1, fy1 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        fx2, fy2 = max(b[2] for b in boxes), max(b[3] for b in boxes)
        mx, my = (fx2 - fx1) * 0.05, (fy2 - fy1) * 0.05
        near = []
        for g in rooms:
            keep = [s for s in g["shapes"]
                    if _centre_within(s, fx1 - mx, fy1 - my, fx2 + mx, fy2 + my)]
            if keep:
                near.append({"shapes": keep})
        focus = _bbox(near)
        if focus is None:
            focus = (fx1, fy1, fx2, fy2)
    if focus is None:
        focus = _bbox(rooms) or full
    return groups, full, focus


def _shape_bbox(shape):
    xs = [p[0] for p in shape["pts"]]
    ys = [p[1] for p in shape["pts"]]
    return min(xs), min(ys), max(xs), max(ys)


def _centre_within(shape, x1, y1, x2, y2):
    sx1, sy1, sx2, sy2 = _shape_bbox(shape)
    cx, cy = (sx1 + sx2) / 2.0, (sy1 + sy2) / 2.0
    return x1 <= cx <= x2 and y1 <= cy <= y2


def _bbox(groups):
    xs, ys = [], []
    for g in groups:
        for s in g["shapes"]:
            for x, y in s["pts"]:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _view(box):
    """A padded viewBox tuple, with y negated for the flipped axis."""
    minx, miny, maxx, maxy = box
    pad = max(maxx - minx, maxy - miny) * 0.02 or 1000
    return (minx - pad, -(maxy + pad),
            (maxx - minx) + 2 * pad, (maxy - miny) + 2 * pad)


def _svg(groups, focus, full, base=""):
    fx, fy, fw, fh = _view(focus)
    ax, ay, aw, ah = _view(full)

    parts = [
        # y is flipped because DXF counts upward and SVG counts downward.
        '<svg xmlns="http://www.w3.org/2000/svg" id="previewSvg" '
        'viewBox="{0:.0f} {1:.0f} {2:.0f} {3:.0f}" '
        'preserveAspectRatio="xMidYMid meet" '
        'data-x="{0:.0f}" data-y="{1:.0f}" data-w="{2:.0f}" data-h="{3:.0f}" '
        'data-ax="{4:.0f}" data-ay="{5:.0f}" data-aw="{6:.0f}" data-ah="{7:.0f}">'
        .format(fx, fy, fw, fh, ax, ay, aw, ah),
        '<g transform="scale(1,-1)">',
    ]

    # Underneath everything, so the coloured boundaries stay readable.
    if base:
        parts.append('<g class="pv-base" data-layer="__base__" '
                     'fill="none" stroke="{0}"><path d="{1}"/></g>'
                     .format(_BASE_COLOR, base))

    for g in groups:
        parts.append(
            '<g class="pv-layer pv-{0}" data-layer="{1}" data-family="{2}" '
            'stroke="{3}" fill="{3}">'.format(
                g["kind"], _esc(g["layer"]), _esc(g["family"]), g["color"]))
        for s in g["shapes"]:
            attr, pts = _points_attr(s["pts"])
            if len(pts) < 3:
                continue
            parts.append(
                '<polygon points="{0}" data-name="{1}" data-area="{2:.2f}" '
                'data-family="{3}"/>'.format(
                    attr, _esc(s.get("name") or ""), s["area"],
                    _esc(g["family"])))
        parts.append("</g>")

    parts.append("</g>")
    parts.append(_labels(groups))
    parts.append("</svg>")
    return "".join(parts)


def _labels(groups):
    """Room names, drawn in drawing units so they zoom with the plan.

    Outside the flipped group, because text inside it would come out
    mirrored; the viewBox is already in flipped space, so negating y puts
    each name back over its room. Sized to fit the room it belongs to, so
    a name never spills outside its own boundary.
    """
    out = ['<g class="pv-labels">']
    for g in groups:
        if g["kind"] == "void":
            continue
        for s in g["shapes"]:
            name = (s.get("name") or "").strip()
            if not name:
                continue
            x1, y1, x2, y2 = _shape_bbox(s)
            w, h = x2 - x1, y2 - y1
            # Fit to the narrower constraint: the room's height, or its
            # width divided among the characters.
            size = min(h * 0.22, w / max(len(name) * 0.55, 1.0))
            if size < 60.0:            # smaller than this is unreadable
                continue
            out.append(
                '<text x="{0:.0f}" y="{1:.0f}" font-size="{2:.0f}">{3}</text>'
                .format((x1 + x2) / 2.0, -((y1 + y2) / 2.0), size, _esc(name)))
    out.append("</g>")
    return "".join(out)
