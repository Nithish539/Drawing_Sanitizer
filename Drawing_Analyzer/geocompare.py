"""Comparison: the text engine's boundaries against the geometry engine's.

Neither side is taken as right. Objects are paired by where they are -
polygon overlap, containment, centroid distance, area - never by name: a
text TOILET and a geometry TOILET 20 m apart are not a match.

Categories:
    AGREEMENT         same place, compatible classes (exact; a geometry
                      ROOM where the text names a kind of room - "generic";
                      or a geometry SHAFT where the text has a services or
                      fire room / shaft of another family - "family",
                      shown as "agreement, different family")
    CONFLICT          same place, different classes
    TEXT_UNNAMED      same place; the text engine drew it but could not name
                      it (an X with no label), the geometry engine classified it
    GEO_UNCLASSIFIED  same place; the geometry engine could not classify it
    TEXT_ONLY         only the text engine has something here
    GEO_ONLY          only the geometry engine has something here
    MULTIPLE          one object on one side overlaps several on the other

A shaft, lift, window, cut-out, column or void is Step 3's (2026-10-16,
final: every X mark decided once by the X sequence, xplace.py). The
geometry engine finds none of them; such a text boundary is paired like
any other (a geometry finding standing on it gives way to it in the final
drawing, app._final_defaults), and a geometry finding of one of those
classes in a result written before then is not read (STEP3_CLASSES).
"""
from __future__ import annotations

import collections
import json
import os

import shapely
from shapely.geometry import Polygon

from . import dxf_reader as dr

CATEGORIES = ["AGREEMENT", "CONFLICT", "TEXT_UNNAMED", "GEO_UNCLASSIFIED",
              "MULTIPLE", "TEXT_ONLY", "GEO_ONLY", "STRUCTURE"]
CAT_RGB = {"AGREEMENT": "#2f9e44", "CONFLICT": "#e03131", "TEXT_UNNAMED": "#f08c00",
           "GEO_UNCLASSIFIED": "#ae3ec9", "MULTIPLE": "#fab005", "TEXT_ONLY": "#1c7ed6",
           "GEO_ONLY": "#e64980", "STRUCTURE": "#495057"}
CAT_ACI = {"AGREEMENT": 3, "CONFLICT": 1, "TEXT_UNNAMED": 30, "GEO_UNCLASSIFIED": 6,
           "MULTIPLE": 2, "TEXT_ONLY": 5, "GEO_ONLY": 221, "STRUCTURE": 250}

# The text engine's boundary layer (family layer + "_BOUNDARY") -> the
# geometry classes that mean the same thing there.
# (Step 3's six - LIFT, SHAFT, VOID, WINDOW, CUTOUT and the columns - have
# no geometry class: the geometry engine never finds them.)
SPECIFIC = {
    "LIFT": set(),
    "LIFTLOBBY": {"LIFT_LOBBY"},
    "MACHINEROOM": {"MACHINE_ROOM"},
    "SERVICELOBBY": {"SERVICE_LOBBY"},
    "STAIR": {"STAIRCASE"},
    "SHAFT": set(),
    "VOID": set(),
    "TOILET": {"TOILET"},
    "WINDOW": set(),               # the text engine's AI_WINDOW outlines
    "CUTOUT": set(),               # AI_CUTOUT: Step 3's cut-outs
}
# geometry classes of Step 3's six (a result written before 2026-10-16)
STEP3_CLASSES = ("COLUMN", "WINDOW", "CUTOUT", "SHAFT", "LIFT", "VOID", "STAIR_WELL")
# Text classes that are rooms of some kind: a geometry ROOM there agrees,
# generically ("an enclosed room" - the geometry cannot say which kind).
ROOMLIKE = {"TOILET", "CIRC", "LIFTLOBBY", "SERVICELOBBY", "MACHINEROOM", "ROOM", "STORE", "AMENITY", "BALCONY", "PARKING",
            "LANDSCAPE", "MEP", "FIRE", "ELEC", "PLUMB"}
UNNAMED = {"UNKNOWN"}
# Text families a geometry SHAFT agrees with only as a place, not as a
# family: an MEP / fire / plumbing / electrical room or shaft where the
# geometry sees a shaft. "agreement, different family" - not counted exact.
FAMILY_ONLY = {"MEP", "FIRE", "PLUMB", "ELEC"}
# What a text class is counted as in the per-class summary.
SUMMARY_CLASS = {"LIFT": "LIFT", "LIFTLOBBY": "LIFT_LOBBY", "STAIR": "STAIRCASE", "SHAFT": "SHAFT", "CUTOUT": "CUTOUT",
                 "VOID": "VOID", "TOILET": "TOILET", "MEP": "SHAFT", "FIRE": "SHAFT",
                 "PLUMB": "SHAFT", "ELEC": "SHAFT", "UNKNOWN": "UNNAMED", "WINDOW": "WINDOW"}


from . import layernames as LN  # noqa: E402


def text_class_of_layer(layer):
    up = (layer or "").upper()
    if up == dr.WINDOW_LAYER.upper():
        return "WINDOW"
    if up == dr.CUTOUT_LAYER.upper():
        return "CUTOUT"
    # AI_TOILET or the older TOILET_BOUNDARY -> TOILET
    return LN.room_key(up) or up


def _is_text_layer(layer):
    """A layer the text engine draws its findings on: a family's
    *_BOUNDARY layer, or the windows in the walls."""
    up = (layer or "").upper()
    return LN.is_room_layer(up) or up in (dr.WINDOW_LAYER.upper(), dr.CUTOUT_LAYER.upper())


def read_text_boundaries(text_dxf, floors, unit_override=None, loaded=None):
    """{floor: [{"cls", "name", "pg"}]} from the text engine's annotated DXF:
    every closed polyline on a *_BOUNDARY layer, named by the label (MTEXT)
    the text engine wrote inside it on the same layer. The floor outline
    itself is left out."""
    doc, msp, units = loaded if loaded else dr.load_document(text_dxf, unit_override)
    labels = collections.defaultdict(list)
    for e in msp.query("MTEXT"):
        if LN.is_room_layer(e.dxf.layer):
            x, y = units.point_to_mm(*dr._text_position(e))
            labels[e.dxf.layer].append((x, y, " ".join(dr._text_value(e).split())))
    fpolys = [(f["name"], Polygon(f["polygon"]).buffer(0)) for f in floors]
    out = {f["name"]: [] for f in floors}
    for e in msp.query("LWPOLYLINE"):
        L = e.dxf.layer
        if not _is_text_layer(L):
            continue
        pts = dr._polyline_points(e, units)
        if len(pts) < 3:
            continue
        pg = Polygon(pts).buffer(0)
        if pg.is_empty or pg.area <= 0:
            continue
        if pg.geom_type == "MultiPolygon":
            pg = max(pg.geoms, key=lambda g: g.area)
        c = pg.representative_point()
        home = next((n for n, fp in fpolys if fp.contains(c)), None)
        if home is None:
            continue
        fp = dict(fpolys)[home]
        if pg.area >= 0.8 * fp.area:
            continue                                  # the floor outline
        name = next((t for x, y, t in labels[L] if pg.contains(shapely.Point(x, y))), "")
        src_, _lab_ = dr.room_tag(e)
        out[home].append({"cls": text_class_of_layer(L), "name": name, "pg": pg,
                          "source": src_ or "", "cell": dr.room_cell_tag(e)})
    return out


def _geo_polys(geo_result):
    out = {}
    for fl, rows in geo_result["floors"].items():
        lst = []
        for r in rows:
            # Step 3's six are never the geometry's (an older result's
            # columns, shafts, lifts, windows, cut-outs, voids: not read)
            if r.get("cls") in STEP3_CLASSES and not r.get("category_layer"):
                continue
            try:
                pg = Polygon(r["poly"]).buffer(0)
            except Exception:                         # noqa: BLE001
                continue
            if pg.is_empty or pg.area <= 0:
                continue
            if pg.geom_type == "MultiPolygon":
                pg = max(pg.geoms, key=lambda g: g.area)
            lst.append((r, pg))
        out[fl] = lst
    return out


def _category(tcls, g):
    """g: the geometry finding (its class, and the category the user set)."""
    gcls = g["cls"]
    if g.get("category_layer"):
        # the user named the category: compare it with the text's family
        if tcls in UNNAMED:
            return "TEXT_UNNAMED", ""
        if tcls == g["category_layer"]:
            return "AGREEMENT", "exact"
        if gcls in SPECIFIC.get(tcls, ()):
            return "AGREEMENT", ("family" if tcls in FAMILY_ONLY else "exact")
        return "CONFLICT", ""
    if gcls == "UNKNOWN_GEOMETRY":
        return "GEO_UNCLASSIFIED", ""
    if tcls in UNNAMED:
        return "TEXT_UNNAMED", ""
    if gcls in SPECIFIC.get(tcls, ()):
        return "AGREEMENT", ("family" if tcls in FAMILY_ONLY else "exact")
    if gcls == "ROOM" and tcls in ROOMLIKE:
        return "AGREEMENT", "generic"
    if tcls not in SPECIFIC and tcls not in UNNAMED and gcls == "ROOM":
        return "AGREEMENT", "generic"
    return "CONFLICT", ""


def _pair(tp, gp):
    inter = tp.intersection(gp).area
    if inter <= 0:
        return None
    iou = inter / tp.union(gp).area
    cont = inter / min(tp.area, gp.area)
    dist = tp.centroid.distance(gp.centroid)
    adiff = abs(tp.area - gp.area) / max(tp.area, gp.area)
    scale = min(tp.area, gp.area) / max(tp.area, gp.area)
    if iou >= 0.6:
        strength = "Strong"
    elif iou >= 0.3 or (cont >= 0.8 and scale >= 0.25):
        strength = "Moderate"
    else:
        # a small thing inside a big one (a shaft inside a toilet block) is
        # a different object, not the same one seen twice
        strength = "Weak"
    return {"iou": iou, "contain": cont, "dist": dist, "adiff": adiff, "strength": strength,
            "t_cover": inter / tp.area, "g_cover": inter / gp.area}


def compare(geo_result, text_by_floor):
    rows = []
    geo_by_floor = _geo_polys(geo_result)
    for fl in geo_result["floors"]:
        T = text_by_floor.get(fl, [])
        G = geo_by_floor.get(fl, [])
        if not G and not T:
            continue
        tree = shapely.STRtree([g for _r, g in G]) if G else None
        cand = collections.defaultdict(list)          # ti -> [(gi, metrics)]
        gcand = collections.defaultdict(list)
        for ti, t in enumerate(T):
            if tree is None:
                break
            for gi in tree.query(t["pg"]):
                gi = int(gi)
                m = _pair(t["pg"], G[gi][1])
                if m is None:
                    continue
                cand[ti].append((gi, m))
                gcand[gi].append((ti, m))
        # one-to-one: best overlaps first; a containment counts below any
        # real overlap, so a well inside a staircase does not take the
        # staircase's place
        pairs = []
        for ti, lst in cand.items():
            for gi, m in lst:
                if m["strength"] == "Weak":
                    continue
                score = m["iou"] if m["iou"] >= 0.3 else 0.1 + 0.2 * m["contain"]
                pairs.append((score, ti, gi, m))
        pairs.sort(key=lambda p: -p[0])
        t_done, g_done = {}, {}
        for score, ti, gi, m in pairs:
            if ti in t_done or gi in g_done:
                continue
            t_done[ti], g_done[gi] = gi, m
            cat, level = _category(T[ti]["cls"], G[gi][0])
            note = ""
            # A name stating a size neither side has is a conflict, never
            # an agreement - ELEC. ROOM 2500x2500 on a 600 x 2500 cut-out
            # paired as ELEC = SHAFT.
            want = dr.declared_size(T[ti]["name"] or "")
            if want and cat in ("AGREEMENT", "TEXT_UNNAMED", "GEO_UNCLASSIFIED"):
                wa = want[0] * want[1] / 1e6
                ta, ga = T[ti]["pg"].area / 1e6, G[gi][1].area / 1e6
                if abs(ta / wa - 1.0) > dr.STATED_CLAIM_TOL or abs(ga / wa - 1.0) > dr.STATED_CLAIM_TOL:
                    cat, level = "CONFLICT", ""
                    off = ta if abs(ta / wa - 1.0) > abs(ga / wa - 1.0) else ga
                    note = "stated size {0:g} x {1:g} = {2:.2f} sq.m, drawn {3:.2f} sq.m".format(
                        want[0], want[1], wa, off)
            rows.append(_row(fl, cat, level, T[ti], G[gi], m, note=note))
        for ti, t in enumerate(T):
            if ti in t_done:
                continue
            many = [(gi, m) for gi, m in cand.get(ti, []) if m["g_cover"] >= 0.5]
            if len(many) >= 2:
                rows.append(_row(fl, "MULTIPLE", "", t, None, None,
                                 candidates=[_cand_geo(G[gi], m) for gi, m in
                                             sorted(many, key=lambda x: -x[1]["g_cover"])[:6]],
                                 note="This text boundary covers {0} geometry findings".format(len(many))))
            else:
                near = next((G[gi][0]["label"] for gi, m in cand.get(ti, []) if m["t_cover"] >= 0.5), "")
                rows.append(_row(fl, "TEXT_ONLY", "", t, None, None,
                                 note=("Mostly inside geometry finding " + near) if near else ""))
        for gi, g in enumerate(G):
            if gi in g_done:
                continue
            many = [(ti, m) for ti, m in gcand.get(gi, []) if m["t_cover"] >= 0.5]
            if len(many) >= 2:
                rows.append(_row(fl, "MULTIPLE", "", None, g, None,
                                 candidates=[_cand_text(T[ti], m) for ti, m in
                                             sorted(many, key=lambda x: -x[1]["t_cover"])[:6]],
                                 note="This geometry finding covers {0} text boundaries".format(len(many))))
            else:
                inside = next((T[ti] for ti, m in gcand.get(gi, []) if m["g_cover"] >= 0.6), None)
                rows.append(_row(fl, "GEO_ONLY", "", None, g, None,
                                 note=("Inside text boundary {0} ({1})".format(inside["name"] or "unnamed", inside["cls"])
                                       if inside else "")))
    for i, r in enumerate(rows, start=1):
        r["id"] = "CMP-{0:04d}".format(i)
    return {"rows": rows, "summary": _summary(rows, text_by_floor, geo_result)}


def _cand_geo(g, m):
    r, _pg = g
    return {"side": "geometry", "label": r["label"], "cls": r["cls"], "id": r["id"],
            "iou": round(m["iou"], 2), "cover": round(m["g_cover"], 2),
            "reason": "{0:.0%} of it lies inside the text boundary".format(m["g_cover"])}


def _cand_text(t, m):
    return {"side": "text", "label": t["name"] or "(unnamed)", "cls": t["cls"],
            "iou": round(m["iou"], 2), "cover": round(m["t_cover"], 2),
            "reason": "{0:.0%} of it lies inside the geometry finding".format(m["t_cover"])}


def _poly_out(pg):
    return [[round(x), round(y)] for x, y in list(pg.exterior.coords)[:-1]]


def _row(fl, cat, level, t, g, m, candidates=None, note=""):
    r = {"floor": fl, "category": cat, "level": level, "note": note,
         "text": None, "geo": None, "candidates": candidates or []}
    if t is not None:
        r["text"] = {"cls": t["cls"], "name": t["name"], "area_sqm": round(t["pg"].area / 1e6, 2),
                     "poly": _poly_out(t["pg"]), "source": t.get("source") or "",
                     "cell": bool(t.get("cell"))}
    if g is not None:
        gr, gp = g
        r["geo"] = {"id": gr["id"], "label": gr["label"], "cls": gr["cls"], "sub": gr.get("sub", ""),
                    "category": gr.get("category", ""), "reviewed": bool(gr.get("reviewed")),
                    "confidence": gr.get("confidence"), "area_sqm": round(gp.area / 1e6, 2),
                    "poly": gr["poly"]}
    if m is not None:
        r.update(iou=round(m["iou"], 2), area_diff=round(m["adiff"], 3),
                 centroid_mm=round(m["dist"]), spatial=m["strength"])
    else:
        r.update(iou=None, area_diff=None, centroid_mm=None, spatial=None)
    return r


def _summary(rows, text_by_floor, geo_result):
    cats = collections.Counter(r["category"] for r in rows)
    n_text = sum(len(v) for v in text_by_floor.values())
    n_geo = sum(len(v) for v in geo_result["floors"].values())
    per = collections.defaultdict(collections.Counter)

    def tclass(r):
        return SUMMARY_CLASS.get(r["text"]["cls"], "ROOM") if r["text"] else None

    for r in rows:
        tc = tclass(r)
        gc = r["geo"]["cls"] if r["geo"] else None
        if tc:
            per[tc]["text"] += 1
        if gc:
            per[gc]["geometry"] += 1
        key = gc or tc
        if r["category"] == "AGREEMENT":
            per[key]["agreement"] += 1
        elif r["category"] == "CONFLICT":
            per[gc]["conflict"] += 1
            if tc and tc != gc:
                per[tc]["conflict"] += 1
        elif r["category"] == "GEO_ONLY":
            per[gc]["geometry_only"] += 1
        elif r["category"] == "TEXT_ONLY":
            per[tc]["text_only"] += 1
        elif r["category"] in ("TEXT_UNNAMED", "GEO_UNCLASSIFIED", "MULTIPLE"):
            per[key][r["category"].lower()] += 1
    agree_exact = sum(1 for r in rows if r["category"] == "AGREEMENT" and r["level"] == "exact")
    agree_family = sum(1 for r in rows if r["category"] == "AGREEMENT" and r["level"] == "family")
    return {
        "text_detections": n_text, "geometry_detections": n_geo,
        "categories": {c: cats.get(c, 0) for c in CATEGORIES},
        "agreement_exact": agree_exact,
        "agreement_family": agree_family,
        "agreement_generic": cats.get("AGREEMENT", 0) - agree_exact - agree_family,
        "per_class": {k: dict(v) for k, v in sorted(per.items(), key=lambda kv: str(kv[0]))},
    }


# ------------------------------------------------------------------ files ---
LEVEL_NOTE = {"generic": " (generic)", "family": " (different family)"}


def write_xlsx(cmp, path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    s = cmp["summary"]
    ws.append(["Text detections", s["text_detections"]])
    ws.append(["Geometry detections", s["geometry_detections"]])
    for c in CATEGORIES:
        ws.append([c.replace("_", " ").title(), s["categories"][c]])
    ws.append(["Agreement (exact class)", s["agreement_exact"]])
    ws.append(["Agreement (different family)", s.get("agreement_family", 0)])
    ws.append(["Agreement (generic room)", s["agreement_generic"]])
    ws2 = wb.create_sheet("Per class")
    cols = ["text", "geometry", "agreement", "conflict", "geometry_only", "text_only",
            "text_unnamed", "geo_unclassified", "multiple"]
    ws2.append(["Class"] + [c.replace("_", " ").title() for c in cols])
    for k, v in s["per_class"].items():
        ws2.append([k] + [v.get(c, 0) for c in cols])
    ws3 = wb.create_sheet("Comparison")
    head = ["ID", "Floor", "Category", "Text class", "Text name", "Text area (sq.m)",
            "Geometry label", "Geometry class", "Geometry id", "Confidence", "Geometry area (sq.m)",
            "IoU", "Area difference", "Centroid distance (mm)", "Spatial match", "Note / candidates"]
    ws3.append(head)
    for c in ws3[1]:
        c.font = Font(bold=True)
    for r in cmp["rows"]:
        t, g = r["text"] or {}, r["geo"] or {}
        cands = "; ".join("{0} {1} ({2})".format(c["label"], c["cls"], c["reason"]) for c in r["candidates"])
        ws3.append([r["id"], r["floor"], r["category"] + LEVEL_NOTE.get(r["level"], ""),
                    t.get("cls", ""), t.get("name", ""), t.get("area_sqm"),
                    g.get("label", ""), g.get("cls", ""), g.get("id", ""),
                    None if g.get("confidence") is None else round(100 * g["confidence"]),
                    g.get("area_sqm"), r["iou"],
                    None if r["area_diff"] is None else round(100 * r["area_diff"], 1),
                    r["centroid_mm"], r["spatial"] or "", " ".join(x for x in (r["note"], cands) if x)])
        fill = CAT_RGB.get(r["category"], "#ffffff").lstrip("#")
        ws3.cell(row=ws3.max_row, column=3).fill = PatternFill("solid", fgColor="FF" + fill)
        ws3.cell(row=ws3.max_row, column=3).font = Font(color="FFFFFFFF", bold=True)
    for w, col in zip((10, 14, 22, 12, 34, 12, 16, 16, 11, 11, 12, 7, 12, 14, 12, 60), "ABCDEFGHIJKLMNOP"):
        ws3.column_dimensions[col].width = w
    ws3.freeze_panes = "A2"
    ws3.auto_filter.ref = ws3.dimensions
    wb.save(path)


CMP_TEXT_LAYER = "AI_CMP_TEXT"


def write_dxf(base_dxf, cmp, out_path, unit_override=None):
    """The geometry annotated DXF plus the text engine's boundaries on
    AI_CMP_TEXT and one marker label per comparison row on AI_CMP_<CATEGORY>, so
    each kind of difference can be switched on and off in AutoCAD."""
    doc, msp, units = dr.load_document(base_dxf, unit_override)
    f = units.from_mm
    if CMP_TEXT_LAYER not in doc.layers:
        doc.layers.add(CMP_TEXT_LAYER, color=5, linetype="DASHED" if "DASHED" in doc.linetypes else "CONTINUOUS")
    for c in CATEGORIES:
        if LN.ai_layer("CMP_" + c) not in doc.layers:
            doc.layers.add(LN.ai_layer("CMP_" + c), color=CAT_ACI.get(c, 7))
    for r in cmp["rows"]:
        if r["text"]:
            msp.add_lwpolyline([(f(x), f(y)) for x, y in r["text"]["poly"]], close=True,
                               dxfattribs={"layer": CMP_TEXT_LAYER, "color": 256})
        poly = (r["geo"] or r["text"])["poly"]
        pg = Polygon(poly)
        c = pg.representative_point()
        bits = [r["category"].replace("_", " ")]
        if r["text"]:
            bits.append("text: " + (r["text"]["name"][:30] or r["text"]["cls"]))
        if r["geo"]:
            bits.append("geo: " + r["geo"]["label"])
        b = pg.bounds
        ht = max(100.0, min(350.0, 0.1 * min(b[2] - b[0], b[3] - b[1])))
        msp.add_mtext("\\P".join(bits), dxfattribs={
            "layer": LN.ai_layer("CMP_" + r["category"]), "char_height": f(ht), "attachment_point": 5,
            "insert": (f(c.x), f(c.y))})
    dr.set_insunits(doc, units)
    doc.saveas(out_path)


def save_json(obj, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
