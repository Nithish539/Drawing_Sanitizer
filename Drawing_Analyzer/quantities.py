# -*- coding: utf-8 -*-
"""Derived quantities.

Everything here is computed from data the pipeline already produces: the
room/family area result, the boundary polygons, and the annotation stats.
No extra user input is required beyond the DXF and config.json.

Deliberately free of ezdxf so it can be unit-tested directly.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .config import CONFIG
from .geometry import bbox, polygon_perimeter_m, shoelace_area_sqm

R2 = lambda v: round(float(v), 2)   # noqa: E731
R3 = lambda v: round(float(v), 3)   # noqa: E731


# -- 1. Reconciliation ------------------------------------------------------
def reconciliation(area_data: Dict) -> Optional[Dict]:
    """Floor area vs the sum of room areas.

    The residual is essentially circulation plus wall thickness plus
    anything the boundary generator missed. It is the single best sanity
    check on whether a run can be trusted: a residual over roughly 40% of
    the floor plate usually means boundaries failed to generate.
    """
    floor = area_data.get("floor_area_sqm")
    rooms = area_data.get("rooms") or []
    if floor is None:
        return None
    measured = sum(float(r.get("area_sqm") or 0.0) for r in rooms)
    residual = float(floor) - measured
    pct = (measured / float(floor) * 100.0) if floor else 0.0
    if pct >= 60.0:
        verdict = "Good - most of the floor plate is accounted for."
    elif pct >= 40.0:
        verdict = "Fair - a large share of the plate is unmeasured; check for missing boundaries."
    else:
        verdict = "Poor - most of the floor plate has no boundary. Review Step 3 output before using these numbers."
    return {
        "floor_area_sqm": R2(floor),
        "measured_room_area_sqm": R2(measured),
        "unaccounted_sqm": R2(residual),
        "unaccounted_pct": R2(100.0 - pct),
        "coverage_pct": R2(pct),
        "room_count": len(rooms),
        "verdict": verdict,
    }


# -- 2. Efficiency KPIs -----------------------------------------------------
def efficiency_kpis(area_data: Dict, cfg: Dict = None) -> Optional[Dict]:
    """The geometric half of the feasibility-KPI module.

    Parking efficiency, cost per sqft and FAR utilisation are deliberately
    absent: they need inputs the drawing does not carry.
    """
    cfg = cfg or CONFIG
    q = cfg.get("quantities", {})
    floor = area_data.get("floor_area_sqm")
    families = area_data.get("families") or []
    if floor is None or not floor:
        return None
    floor = float(floor)

    by_family = {f["name"]: float(f.get("total_sqm") or 0.0) for f in families}
    core_names = q.get("core_families", [])
    core_area = sum(by_family.get(n, 0.0) for n in core_names)
    measured = sum(by_family.values())
    carpet = max(0.0, measured - core_area)

    split = []
    for f in families:
        total = float(f.get("total_sqm") or 0.0)
        split.append({
            "family": f["name"],
            "count": f.get("count", 0),
            "total_sqm": R2(total),
            "pct_of_floor": R2(total / floor * 100.0),
        })

    return {
        "floor_area_sqm": R2(floor),
        "measured_area_sqm": R2(measured),
        "core_area_sqm": R2(core_area),
        "core_families": list(core_names),
        "carpet_area_sqm": R2(carpet),
        "core_efficiency_pct": R2(core_area / floor * 100.0),
        "carpet_to_floor_pct": R2(carpet / floor * 100.0),
        "measured_to_floor_pct": R2(measured / floor * 100.0),
        "family_split": split,
    }


# -- 3. Deck sheet and studs ------------------------------------------------
def deck_sheet_and_studs(area_data: Dict, cfg: Dict = None) -> Optional[Dict]:
    """Deck sheet area and stud count.

    The module brief defines deck sheet as a function of floor area and
    studs at a fixed 9 per square metre, so both are constants rather than
    user inputs. Shafts, lifts and staircases are deducted because they are
    voids in the deck; that deduction is a config rule, and it is reported
    explicitly so it can be challenged rather than silently trusted.
    """
    cfg = cfg or CONFIG
    q = cfg.get("quantities", {})
    floor = area_data.get("floor_area_sqm")
    if floor is None:
        return None
    floor = float(floor)
    families = area_data.get("families") or []
    by_family = {f["name"]: float(f.get("total_sqm") or 0.0) for f in families}

    deduct_names = q.get("deck_sheet_deduct_families", [])
    deductions = [{"family": n, "area_sqm": R2(by_family.get(n, 0.0))}
                  for n in deduct_names if by_family.get(n)]
    deducted = sum(d["area_sqm"] for d in deductions)
    net = max(0.0, floor - deducted)
    rate = float(q.get("studs_per_sqm", 9.0))

    return {
        "gross_floor_area_sqm": R2(floor),
        "deductions": deductions,
        "total_deducted_sqm": R2(deducted),
        "deck_sheet_area_sqm": R2(net),
        "studs_rate_per_sqm": rate,
        "studs_count_nos": int(round(net * rate)),
        "assumption": ("Deck sheet = floor plate less {0}. Studs at {1:g}/m2 "
                       "as given in the module brief. Adjust the deducted "
                       "families in config.json if the project treats these "
                       "areas differently.").format(
                           ", ".join(deduct_names) or "no families", rate),
    }


# -- 4. Perimeter summary ---------------------------------------------------
def perimeter_summary(area_data: Dict) -> Optional[Dict]:
    """Roll-up of the per-room perimeters computed during area calculation."""
    rooms = area_data.get("rooms") or []
    per = [float(r["perimeter_m"]) for r in rooms if r.get("perimeter_m") is not None]
    if not per:
        return None
    return {
        "floor_perimeter_m": area_data.get("floor_perimeter_m"),
        "total_room_perimeter_m": R2(sum(per)),
        "room_count": len(per),
        "longest_room_perimeter_m": R2(max(per)),
        "shortest_room_perimeter_m": R2(min(per)),
    }


# -- 5. Geometry health / cleanup ------------------------------------------
def geometry_health(room_polys: Sequence[Dict], cfg: Dict = None) -> Dict:
    """Flag suspect boundary geometry.

    Completes the geometry-cleanup module beyond the 70% merge already done
    during annotation. Findings are reported, never auto-deleted: silently
    dropping a polyline the user drew is worse than showing them a list.

    Each entry of `room_polys` is {"pts": [(x, y), ...], "name": str,
    "area_sqm": float, "layer": str}.
    """
    cfg = cfg or CONFIG
    q = cfg.get("quantities", {})
    dup_tol = float(q.get("duplicate_area_tolerance_sqm", 0.02))
    flag_ratio = float(q.get("overlap_flag_ratio", 0.10))
    merge_ratio = float(cfg.get("boundary", {}).get("overlap_merge_ratio", 0.70))
    sliver_max = float(q.get("sliver_max_sqm", 0.5))
    sliver_aspect = float(q.get("sliver_max_aspect", 12.0))

    items = []
    for i, rp in enumerate(room_polys):
        pts = rp.get("pts") or []
        bb = bbox(pts) if len(pts) >= 2 else (0, 0, 0, 0)
        items.append({
            "i": i,
            "name": rp.get("name") or "Unknown",
            "layer": rp.get("layer") or "",
            "area": float(rp.get("area_sqm") or 0.0),
            "bbox": bb,
            "n": len(pts),
        })

    findings = []

    # (a) Degenerate / sliver polygons
    for it in items:
        w = it["bbox"][2] - it["bbox"][0]
        h = it["bbox"][3] - it["bbox"][1]
        short, long_ = (min(w, h), max(w, h))
        if it["n"] < 3:
            findings.append(_finding("Degenerate", it, None,
                                     "Fewer than three vertices."))
        elif it["area"] <= sliver_max and short > 0 and long_ / short >= sliver_aspect:
            findings.append(_finding("Sliver", it, None,
                                     "Very thin polygon ({0:.2f} m2, aspect {1:.0f}:1); "
                                     "likely a stray line, not a room.".format(
                                         it["area"], long_ / short)))

    # (b) Duplicate and contained polygons, and suspect overlaps
    for a in range(len(items)):
        for b in range(a + 1, len(items)):
            ia, ib = items[a], items[b]
            r = _bbox_overlap_ratio(ia["bbox"], ib["bbox"])
            if r <= 0:
                continue
            same_area = abs(ia["area"] - ib["area"]) <= dup_tol
            if r >= 0.98 and same_area:
                findings.append(_finding("Duplicate", ia, ib,
                                         "Two boundaries occupy the same space with "
                                         "the same area - one is a copy."))
            elif r >= 0.98:
                inner, outer = (ia, ib) if ia["area"] < ib["area"] else (ib, ia)
                findings.append(_finding("Contained", inner, outer,
                                         "'{0}' sits entirely inside '{1}'; its area is "
                                         "being counted twice.".format(inner["name"],
                                                                       outer["name"])))
            elif flag_ratio <= r < merge_ratio:
                findings.append(_finding("Partial overlap", ia, ib,
                                         "Boundaries overlap by {0:.0f}%, below the {1:.0f}% "
                                         "auto-merge threshold, so both areas are "
                                         "counted.".format(r * 100, merge_ratio * 100)))

    counts = {}
    for f in findings:
        counts[f["issue"]] = counts.get(f["issue"], 0) + 1
    return {
        "checked": len(items),
        "issue_count": len(findings),
        "by_issue": counts,
        "findings": findings,
        "clean": not findings,
    }


def _finding(issue, a, b, detail):
    return {
        "issue": issue,
        "room_a": a["name"],
        "layer_a": a["layer"],
        "area_a_sqm": R2(a["area"]),
        "room_b": b["name"] if b else "",
        "layer_b": b["layer"] if b else "",
        "area_b_sqm": R2(b["area"]) if b else "",
        "detail": detail,
    }


def _bbox_overlap_ratio(b1, b2):
    ax1, ay1, ax2, ay2 = b1
    bx1, by1, bx2, by2 = b2
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix1 >= ix2 or iy1 >= iy2:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    a1 = (ax2 - ax1) * (ay2 - ay1)
    a2 = (bx2 - bx1) * (by2 - by1)
    denom = min(a1, a2)
    return inter / denom if denom > 0 else 0.0


# -- 6. Extraction QA report ------------------------------------------------
_STATUS_MEANING = {
    "x": "Claimed by an X-marked shaft; the label became the shaft title.",
    "primary": "Got its own boundary as a primary label.",
    "secondary": "Got its own boundary as a secondary label.",
    "absorbed": "Sits inside another room's boundary; no separate boundary drawn.",
    "merged": "Its boundary was merged into an overlapping one.",
    "no_walls": "No enclosing walls found within the search radius - NO BOUNDARY DRAWN.",
    "pending": "Never processed - check whether it was deselected in Step 3.",
    "filtered": "Boundary found, but held back by the boundary-type filter.",
}
# 'filtered' is a deliberate choice by the user, not a defect.
_STATUS_OK = {"x", "primary", "secondary", "absorbed", "merged", "filtered"}


def extraction_qa(stats: Dict, results: Dict) -> Dict:
    """Turn the per-text status that annotate_dxf already computes into a
    reportable defect list.

    This data was being built and then discarded. Surfacing it tells the
    user exactly which labels produced no boundary, which is the main
    reason a run comes out short.
    """
    by_floor = (stats or {}).get("text_status_by_floor") or {}
    rows = []
    totals = {}
    for floor_name, statuses in by_floor.items():
        text_by_sr = {int(r.get("sr_no")): r
                      for r in (results.get(floor_name) or [])
                      if r.get("sr_no") is not None}
        for sr, status in sorted(statuses.items(), key=lambda kv: int(kv[0])):
            sr_i = int(sr)
            rec = text_by_sr.get(sr_i, {})
            totals[status] = totals.get(status, 0) + 1
            rows.append({
                "floor": floor_name,
                "sr_no": sr_i,
                "text": rec.get("text", ""),
                "layer": rec.get("layer", ""),
                "family": rec.get("family", ""),
                "status": status,
                "ok": status in _STATUS_OK,
                "meaning": _STATUS_MEANING.get(status, status),
            })
    problems = [r for r in rows if not r["ok"]]
    return {
        "rows": rows,
        "totals": totals,
        "problem_count": len(problems),
        "problems": problems,
        "boundary_count": sum(totals.get(k, 0) for k in ("primary", "secondary", "x")),
    }


# -- 7. Revision comparison -------------------------------------------------
def compare_revisions(old: Dict, new: Dict, key: str = "name") -> Dict:
    """Quantity delta between two area results.

    Needs no new input: both sides are area JSONs the tool already exports.
    Rooms are matched on name plus family; unmatched rooms on either side
    become added or removed.
    """
    def index(data):
        out = {}
        for r in (data.get("rooms") or []):
            k = (str(r.get(key, "")).strip().upper(), r.get("family", ""))
            out.setdefault(k, []).append(float(r.get("area_sqm") or 0.0))
        return out

    a, b = index(old), index(new)
    added, removed, changed, unchanged = [], [], [], []

    for k in sorted(set(a) | set(b)):
        la, lb = sorted(a.get(k, []), reverse=True), sorted(b.get(k, []), reverse=True)
        name, fam = k
        for i in range(max(len(la), len(lb))):
            va = la[i] if i < len(la) else None
            vb = lb[i] if i < len(lb) else None
            if va is None:
                added.append({"name": name, "family": fam, "new_sqm": R2(vb),
                              "delta_sqm": R2(vb)})
            elif vb is None:
                removed.append({"name": name, "family": fam, "old_sqm": R2(va),
                                "delta_sqm": R2(-va)})
            else:
                d = vb - va
                row = {"name": name, "family": fam, "old_sqm": R2(va),
                       "new_sqm": R2(vb), "delta_sqm": R2(d),
                       "delta_pct": R2(d / va * 100.0) if va else 0.0}
                (changed if abs(d) >= 0.01 else unchanged).append(row)

    old_total = sum(float(r.get("area_sqm") or 0.0) for r in (old.get("rooms") or []))
    new_total = sum(float(r.get("area_sqm") or 0.0) for r in (new.get("rooms") or []))
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged_count": len(unchanged),
        "summary": {
            "old_room_count": len(old.get("rooms") or []),
            "new_room_count": len(new.get("rooms") or []),
            "old_total_sqm": R2(old_total),
            "new_total_sqm": R2(new_total),
            "delta_sqm": R2(new_total - old_total),
            "delta_pct": R2((new_total - old_total) / old_total * 100.0) if old_total else 0.0,
            "added_count": len(added),
            "removed_count": len(removed),
            "changed_count": len(changed),
            "old_floor_area_sqm": old.get("floor_area_sqm"),
            "new_floor_area_sqm": new.get("floor_area_sqm"),
        },
    }


# -- 8. Assembly ------------------------------------------------------------
def floor_area_data(area_data: Dict, floor: Dict) -> Dict:
    """The area result narrowed to one floor: its plate, its rooms and its
    families, shaped like a whole result so every report above runs on it
    unchanged."""
    name = floor.get("name")
    return {
        "units": area_data.get("units"),
        "floor_label": name,
        "floor_area_sqm": floor.get("area_sqm"),
        "floor_perimeter_m": floor.get("perimeter_m"),
        "rooms": [r for r in (area_data.get("rooms") or [])
                  if r.get("floor") == name],
        "families": floor.get("families") or [],
    }


def _sections(area_data: Dict, room_polys, cfg: Dict) -> Dict:
    return {
        "reconciliation": reconciliation(area_data),
        # Only family_split is reported now; the efficiency figures and the
        # deck/stud estimate were dropped from the step-4 output.
        "kpis": efficiency_kpis(area_data, cfg),
        "perimeter": perimeter_summary(area_data),
        "geometry_health": geometry_health(room_polys or [], cfg),
    }


def build_quantity_report(area_data: Dict, room_polys=None, cfg: Dict = None) -> Dict:
    """Everything derivable from an area result with no extra input.

    The top level covers every floor together. A sheet carrying several
    floors also gets the same reports for each floor on its own, under
    "floors", so each plate is reconciled against its own rooms.
    """
    cfg = cfg or CONFIG
    report = {
        "units": area_data.get("units"),
        "floor_label": area_data.get("floor_label"),
    }
    report.update(_sections(area_data, room_polys, cfg))
    floors = area_data.get("floors") or []
    if len(floors) > 1:
        report["floors"] = []
        for f in floors:
            sub = floor_area_data(area_data, f)
            polys = [p for p in (room_polys or []) if p.get("floor") == f.get("name")]
            entry = {"name": f.get("name"), "area_sqm": f.get("area_sqm")}
            entry.update(_sections(sub, polys, cfg))
            report["floors"].append(entry)
    return report
