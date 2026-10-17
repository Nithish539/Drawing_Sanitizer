# -*- coding: utf-8 -*-
"""The Step 3 result store: what one Generate decided, kept once per upload.

annotate_dxf (dxf_reader) returns it as stats["step3"]; /annotate writes it
to <token>_step3.json. Every later step reads this file instead of working
the drawing out again or reading the Step 3 drawing back:

    the Geometry tab      findings(store)      - the rooms as the tab's records
    the comparison        findings(store)      - the geometry side
    the final drawing     as_geo(store), confidence_index(store), side_shapes(store)
    Trace (a click)       trace_context(store) - the walls to trace against

Per floor the store holds: the plate; the walls by class ({AI_WALL: [rings
of one piece: outline, then holes]}); the door openings with their kind,
level, sides, the room each belongs to (side 0 / 1), the line drawn and the
way into its room; the doubtful openings and shafts (AI_REVIEW); the glass;
the windows and cut-outs; the columns drawn; the hanging walls; every room
written (label, family, layer, outline, holes, source, X class, confidence
and its reasons); the spaces as traced before any text was read; the X
marks decided; the lift-bank lines; the lock report; the text statuses.
Everything in millimetres, plain JSON.

Nothing here finds anything. Until 2026-10-17 a second engine
(geoengine.run) parsed the drawing again in a process of its own and traced
the walls, the doors and the columns a second time for the Geometry tab and
the final drawing; it is gone, and these readers took its place.
"""
from __future__ import annotations

import collections
import json
import os

from shapely.geometry import Point, Polygon

VERSION = 1


# --------------------------------------------------------------- file ---
def read(path):
    """The store at path, or None when there is none (or it cannot be read)."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            store = json.load(fh)
    except (OSError, ValueError):
        return None
    return store if isinstance(store, dict) and isinstance(store.get("floors"), dict) else None


def write(path, store):
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False)
    os.replace(tmp, path)


def floors_of(store):
    return list((store or {}).get("floors") or {})


# ------------------------------------------------------------ helpers ---
def _poly(pts):
    """A shapely polygon (the largest part of a multi-part), or None."""
    try:
        pg = Polygon([tuple(p[:2]) for p in pts])
        if not pg.is_valid:
            pg = pg.buffer(0)
        if pg.geom_type == "MultiPolygon":
            pg = max(pg.geoms, key=lambda g: g.area)
        return pg if not pg.is_empty and pg.area > 0 else None
    except Exception:                                 # noqa: BLE001
        return None


def _rings_of(piece):
    """A wall piece as draw_walls takes it - [outline, hole, ...] - or a
    bare ring: its rings."""
    if piece and isinstance(piece[0], (list, tuple)) and piece[0] and isinstance(piece[0][0], (list, tuple)):
        return [r for r in piece if len(r) >= 3]
    return [piece] if len(piece or ()) >= 3 else []


def _edges(ring):
    out = []
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        if abs(a[0] - b[0]) > 1e-6 or abs(a[1] - b[1]) > 1e-6:
            out.append((float(a[0]), float(a[1]), float(b[0]), float(b[1])))
    return out


def _piece_polygon(piece):
    rings = _rings_of(piece)
    if not rings:
        return None
    outer = _poly(rings[0])
    if outer is None:
        return None
    for h in rings[1:]:
        hg = _poly(h)
        if hg is not None:
            outer = outer.difference(hg)
    return outer if not outer.is_empty else None


def walls_summary(store):
    """{"total": {class: {count, length_m, area_sqm}}, "floors": {floor:
    the same}, "strips": {}} from the store's wall pieces. length_m is half
    the pieces' perimeter - a wall piece's run, near enough for the
    summary; Step 3's own walls_length_m stat is the measured figure."""
    total = {}
    per = {}
    for fl, F in (store.get("floors") or {}).items():
        rep = {}
        for cls, pieces in (F.get("walls") or {}).items():
            c = rep.setdefault(cls, {"count": 0, "length_m": 0.0, "area_sqm": 0.0})
            for piece in pieces or ():
                pg = _piece_polygon(piece)
                if pg is None:
                    continue
                c["count"] += 1
                c["area_sqm"] += pg.area / 1e6
                c["length_m"] += pg.length / 2000.0
            t = total.setdefault(cls, {"count": 0, "length_m": 0.0, "area_sqm": 0.0})
            for k in ("count", "length_m", "area_sqm"):
                t[k] += c[k]
        for c in rep.values():
            c["length_m"], c["area_sqm"] = round(c["length_m"], 2), round(c["area_sqm"], 2)
        per[fl] = rep
    for t in total.values():
        t["length_m"], t["area_sqm"] = round(t["length_m"], 2), round(t["area_sqm"], 2)
    return {"total": total, "floors": per, "strips": {}}


# ------------------------------------------------- the final drawing ---
def as_geo(store):
    """The store in the shape the final drawing reads (what the geometry
    engine's result used to carry): walls, doors, door_review, glass,
    hanging_walls, plates, window_openings (always empty: the windows are
    Step 3's rows) - and, new, the doors' kinds, levels, sides, owners,
    lines and directions, the shaft reviews and the columns, per floor."""
    geo = {"source": "step3", "walls": {}, "doors": {}, "door_review": {}, "shaft_review": {}, "glass": {},
           "plates": {}, "window_openings": {}, "columns": {}, "hanging_walls": [],
           "door_kinds": {}, "door_levels": {}, "door_sides": {}, "door_owners": {}, "door_lines": {}, "door_dirs": {}}
    for fl, F in (store.get("floors") or {}).items():
        geo["walls"][fl] = F.get("walls") or {}
        geo["doors"][fl] = F.get("doors") or []
        geo["door_review"][fl] = F.get("door_review") or []
        geo["shaft_review"][fl] = F.get("shaft_review") or []
        geo["glass"][fl] = F.get("glass") or []
        geo["plates"][fl] = F.get("plate") or []
        geo["window_openings"][fl] = []
        geo["columns"][fl] = F.get("columns") or []
        geo["hanging_walls"] += list(F.get("hanging_walls") or [])
        for k in ("door_kinds", "door_levels", "door_sides", "door_owners", "door_lines", "door_dirs"):
            geo[k][fl] = F.get(k)
    geo["walls_summary"] = walls_summary(store)
    return geo


def confidence_index(store):
    """[(centroid x, y mm, area sq.m, source tag, label, score, reasons)]
    for every room Step 3 scored - what the final drawing used to read back
    from the Step 3 drawing's XDATA (app._text_confidence_index). The area
    and centroid are the outline's, holes left in, as that read made them."""
    out = []
    for F in (store.get("floors") or {}).values():
        for r in F.get("rooms") or ():
            if r.get("confidence") is None:
                continue
            pg = _poly(r.get("poly") or [])
            if pg is None:
                continue
            c = pg.centroid
            out.append((c.x, c.y, pg.area / 1e6, r.get("source") or "", r.get("label") or "",
                        float(r["confidence"]), "; ".join(r.get("reasons") or [])))
    return out


def side_shapes(store, floor=None):
    """What the final drawing takes from Step 3 besides the rooms: "wells" -
    the voids standing in a staircase (the stair's open well), "columns" -
    the columns drawn on AI_COLUMN, "doors" - every door as (its opening,
    the middle of its line, the way into its room). Shapely polygons, mm;
    every floor, or one."""
    out = {"wells": [], "columns": [], "doors": []}
    for fl, F in (store.get("floors") or {}).items():
        if floor is not None and fl != floor:
            continue
        stairs, voids = [], []
        for r in F.get("rooms") or ():
            pg = _poly(r.get("poly") or [])
            if pg is None:
                continue
            if r.get("family") == "Staircase":
                stairs.append(pg)
            if str(r.get("layer") or "").upper() == "AI_VOID":
                voids.append(pg)
            for h in r.get("holes") or ():
                hg = _poly(h)
                if hg is not None:
                    voids.append(hg)
        for v in voids:
            if any(s.intersection(v).area >= 0.9 * v.area for s in stairs):
                out["wells"].append(v)
        for pts in F.get("columns") or ():
            pg = _poly(pts)
            if pg is not None:
                out["columns"].append(pg)
        lines = F.get("door_lines") or []
        dirs = F.get("door_dirs") or []
        for k, q in enumerate(F.get("doors") or ()):
            g = _poly(q)
            if g is None:
                continue
            ln = lines[k] if k < len(lines) else None
            if ln and len(ln) >= 2:
                mid = (sum(p[0] for p in ln) / len(ln), sum(p[1] for p in ln) / len(ln))
            else:
                mid = (sum(p[0] for p in q) / len(q), sum(p[1] for p in q) / len(q))
            d = dirs[k] if k < len(dirs) else None
            out["doors"].append((g, mid, tuple(d) if d else None))
    return out


# ------------------------------------------------------ the Geometry tab ---
def _how_traced(room, space_polys):
    """"space" when the room stands where a space was traced with no text
    (rule rooms_from_spaces), else "label" (traced from its text, Phase B)."""
    at = room.get("at")
    if at and space_polys:
        p = Point(float(at[0]), float(at[1]))
        for pg in space_polys:
            if pg.contains(p):
                return "space"
    src = str(room.get("source") or "")
    return "space" if src == "G" else "label"


def findings(store, families=None):
    """The store as the Geometry tab's result (what geoengine.run returned):
    one record per room Step 3 wrote - not the six it decides by the X
    sequence (shaft, lift, window, cut-out, column, void: STEP3_FAMILIES) -
    with the engine's record shape (id GEO-nnnn, cls, label, poly, features
    with its centroid and bbox, confidence, evidence), so apply_reviews,
    build_final, the comparison and the tab's page read it unchanged. The
    walls, doors, glass and the rest come along (as_geo). families: the
    app's family list (unused; kept for the callers' signature)."""
    from . import geoengine as ge
    from . import dxf_reader as dr
    # a family the engine's classes do not name (MEP / Services, AHU Room,
    # Circulation, Balcony / Open ...) travels as the record's category -
    # as a category the user sets in the tab does (apply_reviews) - so the
    # comparison, the geometry drawing and the user's output keep it
    layer_of = {f["name"]: dr.LN.room_key(dr.LN.family_layer(f.get("layer") or "ROOM")) for f in (families or [])}
    floors_out = {}
    counts = collections.Counter()
    seq = 0
    n_spaces = n_x = 0
    for fl, F in (store.get("floors") or {}).items():
        rows = []
        spaces = F.get("spaces") or []
        n_spaces += len(spaces)
        n_x += len(F.get("x_placement") or [])
        space_polys = [pg for pg in (_poly(s.get("poly") or []) for s in spaces if str(s.get("fate") or "").startswith("drawn"))
                       if pg is not None]
        for r in F.get("rooms") or ():
            fam = r.get("family") or "Unknown"
            if r.get("source") == "X" or fam in ge.STEP3_FAMILIES or fam in ("Window", "Cutout", "Column", "Ignore"):
                continue
            pg = _poly(r.get("poly") or [])
            if pg is None:
                continue
            seq += 1
            cls = ge.CLASS_OF_FAMILY.get(fam) or ("UNKNOWN_GEOMETRY" if fam == "Unknown" else "ROOM")
            how = _how_traced(r, space_polys)
            feats = ge._shape(pg)
            feats["source"] = how
            feats["layers"] = [r.get("layer")] if r.get("layer") else []
            ev = []
            for w in r.get("reasons") or ():
                s = str(w).strip()
                ev.append({"ok": not s.startswith("-"), "metadata": False, "text": s.lstrip("-+ ").strip()})
            for e in r.get("evidence") or ():
                ev.append({"ok": True, "metadata": False, "text": str(e)})
            if not ev:
                ev.append({"ok": True, "metadata": False,
                           "text": ("Traced from the walls as an enclosed space, named by the text inside it"
                                    if how == "space" else "Traced outward from its text (the labels' pass)")})
            category = fam if (fam not in ("Room", "Unknown") and ge.FAMILY_OF_CLASS.get(cls) != fam) else ""
            rows.append({
                "id": "GEO-{0:04d}".format(seq), "label": r.get("label") or fam, "cls": cls, "sub": "",
                "floor": fl, "rule": "step3_" + how,
                "category": category, "category_layer": (layer_of.get(fam, "ROOM") if category else None),
                "confidence": r.get("confidence"),
                "confidence_note": "Step 3's own score for this room: its name, its stated size, how it was traced",
                "evidence": ev, "reason": "", "family": None, "features": feats,
                "repeats": None, "bank": None, "near": [],
                "poly": [[round(x), round(y)] for x, y in list(pg.exterior.coords)[:-1]],
                "holes": [[[round(float(x)), round(float(y))] for x, y in h] for h in (r.get("holes") or []) if len(h) >= 3],
                "area_sqm": feats["area_sqm"],
                "label_sr": r.get("sr_no"), "step3_family": fam, "step3_layer": r.get("layer"),
            })
            counts[cls] += 1
        floors_out[fl] = rows
    out = {
        "engine": "step3", "version": 2, "source": "step3",
        "floors": floors_out,
        "summary": {c: counts.get(c, 0) for c in ge.CLASSES},
        "classes": [{"cls": c, "layer": ge.LAYER_OF[c], "rgb": ge.RGB[c]} for c in ge.CLASSES],
        "families": [],
        "diagnostics": {"rooms": seq, "spaces traced with no text": n_spaces, "X marks decided": n_x},
        "profile_source": ("Step 3's one run (walls, X marks, doors, spaces, texts last) - no second engine; "
                           "the confidence shown is Step 3's own score for each room"),
        "rules": {},
        "step3_code": store.get("code"), "step3_written": store.get("written"),
        "step3_family_filter": store.get("family_filter") or "",
    }
    out.update(as_geo(store))
    return out


# -------------------------------------------------------------- Trace ---
def trace_context(store, texts_by_floor=None):
    """{floor: {"walls", "gaps", "idx", "rows"}} for geomextract.trace_at: a
    click inside a space is traced against Step 3's walls (the edges of the
    wall pieces), its door openings (closed) and the plate - no drawing is
    read again."""
    from .geometry import BandIndex
    from . import dxf_reader as dr
    ctx = {}
    for fl, F in (store.get("floors") or {}).items():
        segs = []
        for pieces in (F.get("walls") or {}).values():
            for piece in pieces or ():
                for ring in _rings_of(piece):
                    segs += _edges(ring)
        for q in F.get("doors") or ():
            if len(q) >= 3:
                segs += _edges(q)
        plate = F.get("plate") or []
        if len(plate) >= 3:
            segs += _edges(plate)
        ctx[fl] = {"walls": segs, "gaps": [], "rows": (texts_by_floor or {}).get(fl, []),
                   "idx": BandIndex(segs, band=dr.INDEX_BAND, axis_ratio=dr.WALL_AXIS_RATIO) if segs else None}
    return ctx
