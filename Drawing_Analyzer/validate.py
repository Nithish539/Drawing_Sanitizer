# -*- coding: utf-8 -*-
"""Checks on the boundaries a Generate run wrote.

annotate_dxf hands over every boundary exactly as it went into the DXF
(floor, layer, family, label, outline). Nothing here changes the drawing;
it reports what a CAD checker would flag:

  open        fewer than three distinct corners - not a closed shape
  self_cross  an outline that crosses itself
  tiny        under 0.05 sq.m, or zero
  duplicate   the same shape twice on one layer
  overlap     two boundaries sharing a real part of their area (a cut-out
              inside its shaft is expected and not reported)
  oversize    a shaft over the void threshold, a room over 1000 sq.m
  size        a label stating a size the boundary does not have
  label_out   a label written outside its own boundary
  missing     a room name among the extracted texts that got no boundary
  unknown     boundaries no family recognises (reported as a count)

Pure Python.
"""

from __future__ import annotations

import math
from typing import Dict, List

from .labels import declared_size

TINY_SQM = 0.05
ROOM_MAX_SQM = 1000.0


def _area(pts):
    a = 0.0
    n = len(pts)
    for k in range(n):
        x1, y1 = pts[k]
        x2, y2 = pts[(k + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0 / 1e6


def _bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _pip(x, y, pts):
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _self_crosses(pts, limit=400):
    """True when two non-adjacent edges cross. Outlines with more corners
    than `limit` are not checked (returns None)."""
    n = len(pts)
    if n < 4:
        return False
    if n > limit:
        return None

    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-6 else (1 if v > 0 else -1)
    edges = [(pts[k], pts[(k + 1) % n]) for k in range(n)]
    boxes = [(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
             for a, b in edges]
    for i in range(n):
        a, b = edges[i]
        bi = boxes[i]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            bj = boxes[j]
            if bj[0] > bi[2] or bj[2] < bi[0] or bj[1] > bi[3] or bj[3] < bi[1]:
                continue
            c, d = edges[j]
            o1, o2 = orient(a, b, c), orient(a, b, d)
            o3, o4 = orient(c, d, a), orient(c, d, b)
            if o1 * o2 < 0 and o3 * o4 < 0:
                return True
    return False


def _shared(p, q, bp, bq, samples=9):
    """Fraction of the smaller shape's sample points inside the other."""
    small, big, bs = (p, q, bp) if _area(p) <= _area(q) else (q, p, bq)
    hits = total = 0
    for i in range(samples):
        for j in range(samples):
            x = bs[0] + (i + 0.5) * (bs[2] - bs[0]) / samples
            y = bs[1] + (j + 0.5) * (bs[3] - bs[1]) / samples
            if not _pip(x, y, small):
                continue
            total += 1
            if _pip(x, y, big):
                hits += 1
    return hits / total if total else 0.0


def validate_boundaries(records: List[Dict], text_status_by_floor: Dict = None,
                        results: Dict = None, void_threshold: float = 30.0,
                        size_ok=None) -> Dict:
    """Check the boundaries written by a run. See the module docstring.

    records: [{floor, layer, family, label, pts, label_xy, cutout_of}]
    size_ok: callable(area_sqm, (a, b), bbox) -> bool; the rule the
             drawing pass used, so both judge a size the same way.
    """
    issues = []

    def add(kind, rec, detail, severity="warning"):
        issues.append({"kind": kind, "severity": severity,
                       "floor": rec.get("floor", "") if rec else "",
                       "layer": rec.get("layer", "") if rec else "",
                       "label": (rec.get("label", "") if rec else "").replace("\n", " / "),
                       "area_sqm": round(rec.get("_area", 0.0), 2) if rec else None,
                       "detail": detail})

    recs = []
    for r in records:
        pts = [tuple(p) for p in (r.get("pts") or [])]
        # Consecutive duplicates and a repeated first point do not count.
        clean = []
        for p in pts:
            if not clean or abs(p[0] - clean[-1][0]) + abs(p[1] - clean[-1][1]) > 0.5:
                clean.append(p)
        if len(clean) > 1 and abs(clean[0][0] - clean[-1][0]) + abs(clean[0][1] - clean[-1][1]) <= 0.5:
            clean.pop()
        r = dict(r, pts=clean)
        r["_area"] = _area(clean) if len(clean) >= 3 else 0.0
        r["_bbox"] = _bbox(clean) if clean else (0, 0, 0, 0)
        recs.append(r)

    for r in recs:
        pts, area = r["pts"], r["_area"]
        if len(pts) < 3:
            add("open", r, "fewer than three distinct corners", "error")
            continue
        if area < TINY_SQM:
            add("tiny", r, "{0:.3f} sq.m".format(area), "error" if area <= 0 else "warning")
        crossing = _self_crosses(pts)
        if crossing:
            add("self_cross", r, "the outline crosses itself", "error")
        fam = r.get("family", "")
        if fam == "Shaft" and area > void_threshold:
            add("oversize", r, "a shaft of {0:.1f} sq.m - over the {1:g} sq.m void "
                "threshold".format(area, void_threshold))
        elif fam not in ("Void",) and area > ROOM_MAX_SQM:
            add("oversize", r, "{0:.0f} sq.m".format(area))
        want = declared_size(r.get("label", ""))
        if want and size_ok is not None and not size_ok(area, want, r["_bbox"]):
            add("size", r, "label states {0:g} x {1:g} mm ({2:.2f} sq.m); drawn {3:.2f} "
                "sq.m".format(want[0], want[1], want[0] * want[1] / 1e6, area))
        lx, ly = r.get("label_xy") or (None, None)
        if lx is not None and not _pip(lx, ly, pts):
            add("label_out", r, "the label is written outside its boundary")

    # Pairs: duplicates and overlaps, per floor, bbox first.
    by_floor = {}
    for i, r in enumerate(recs):
        if len(r["pts"]) >= 3 and r["_area"] >= TINY_SQM:
            by_floor.setdefault(r.get("floor", ""), []).append(i)
    for idxs in by_floor.values():
        idxs.sort(key=lambda i: recs[i]["_bbox"][0])
        for a in range(len(idxs)):
            p = recs[idxs[a]]
            bp = p["_bbox"]
            for b in range(a + 1, len(idxs)):
                q = recs[idxs[b]]
                bq = q["_bbox"]
                if bq[0] >= bp[2]:
                    break
                if bq[1] >= bp[3] or bq[3] <= bp[1]:
                    continue
                if (p["layer"] == q["layer"]
                        and all(abs(u - v) <= 20.0 for u, v in zip(bp, bq))
                        and abs(p["_area"] - q["_area"]) <= 0.01 * max(p["_area"], q["_area"])):
                    add("duplicate", q, "same shape as another {0} boundary".format(p["layer"]))
                    continue
                # A cut-out inside its shaft, and anything inside a void,
                # are expected nestings.
                fams = {p.get("family"), q.get("family")}
                if "Void" in fams:
                    continue
                share = _shared(p["pts"], q["pts"], bp, bq)
                if share >= 0.2:
                    small, big = (p, q) if p["_area"] <= q["_area"] else (q, p)
                    add("overlap", small, "{0:.0f}% of it lies inside '{1}' ({2})".format(
                        share * 100, big.get("label", "").replace("\n", " / ")[:40],
                        big.get("layer")))

    # Room names among the texts that got no boundary.
    missing = 0
    for floor, statuses in (text_status_by_floor or {}).items():
        rows = {int(r.get("sr_no")): r for r in (results or {}).get(floor, [])
                if r.get("sr_no") is not None}
        for sr, st in statuses.items():
            if st not in ("no_walls", "no_evidence", "size_mismatch", "engulfing"):
                continue
            row = rows.get(int(sr), {})
            fam = row.get("family", "")
            if fam in ("", "Unknown"):
                continue
            missing += 1
            add("missing", {"floor": floor, "layer": "", "label": row.get("text", ""),
                            "_area": 0.0},
                "a {0} name with no boundary ({1})".format(fam, st.replace("_", " ")))

    unknown = sum(1 for r in recs if r.get("family") == "Unknown")
    unnamed_shafts = sum(1 for r in recs if (r.get("label") or "") == "Unknown Shaft")
    counts = {}
    for i in issues:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    checks = [
        ("Closed polylines", "open"), ("Self-intersecting outlines", "self_cross"),
        ("Zero-area / tiny polygons", "tiny"), ("Duplicate boundaries", "duplicate"),
        ("Overlapping boundaries", "overlap"), ("Unreasonable sizes", "oversize"),
        ("Size against the label", "size"), ("Labels outside their boundary", "label_out"),
        ("Room names with no boundary", "missing"),
    ]
    return {
        "boundaries": len(recs),
        "checks": [{"check": name, "kind": k, "count": counts.get(k, 0),
                    "ok": counts.get(k, 0) == 0} for name, k in checks],
        "unknown_boundaries": unknown,
        "unnamed_shafts": unnamed_shafts,
        "voids": sum(1 for r in recs if r.get("family") == "Void"),
        "issues": issues,
        "issue_count": len(issues),
        "error_count": sum(1 for i in issues if i["severity"] == "error"),
    }
