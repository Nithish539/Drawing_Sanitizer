# -*- coding: utf-8 -*-
"""X-marked regions, recognised tolerantly.

A cut-out, shaft or void is marked in CAD with a cross drawn corner to
corner. The original detector (dxf_reader._find_x_pairs) needs a textbook
X: two diagonals of nearly equal length crossing at their midpoints, each
at most 5 m. Real drawings break that in several ways, all seen on the P14
1st floor sheet:

  * one diagonal is trimmed where a column plate interrupts it
    (CUT-OUT 2500 x 700: 2,596 mm against 2,166 mm);
  * a diagonal is drawn in two pieces;
  * an L-shaped cut-out is crossed strip by strip, each strip with one full
    diagonal and one covering only its own part (TR-SH-03);
  * a tall shaft's diagonals run past 5 m (DG-SH 900 x 5000: 5,080 mm);
  * a void across a whole podium edge is one X 65-88 m long.

Here a diagonal marks the rectangle it is the diagonal of. Two diagonals
of opposite slope that cross, and whose rectangles are substantially the
same, are one X; the region is the union of its diagonals' rectangles.
Crosses whose rectangles overlap (the two strips of an L) join into one
region; crosses that only touch (two shafts side by side) stay apart.

Pure Python; no ezdxf.
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

# Layers whose crosses are never shafts: steel column crosses, symbols
# (the X over a stair well is a drawing symbol).
NOT_X_LAYER_WORDS = ("COL", "SYMBOL", "SYMB", "GRID", "HATCH")

COLLINEAR_DEG = 1.5          # two pieces of one diagonal
COLLINEAR_OFFSET_MM = 30.0
COLLINEAR_GAP_MM = 200.0
PAIR_MIN_OVERLAP = 0.4       # rectangle overlap / larger rectangle
REGION_TOUCH_MM = 5.0        # overlap thinner than this is touching


def is_x_layer(layer: str) -> bool:
    """A layer on which a diagonal can mark a cut-out, shaft or void."""
    lu = (layer or "").upper()
    if any(w in lu for w in NOT_X_LAYER_WORDS):
        return False
    return "CUTOUT" in lu or "CUT-OUT" in lu or "CUT OUT" in lu or (
        "SHAFT" in lu and lu.startswith("E-"))


def _rect(s):
    return (min(s[0], s[2]), min(s[1], s[3]), max(s[0], s[2]), max(s[1], s[3]))


def _rect_area(r):
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def _overlap(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0, 0.0, 0.0
    return w * h, w, h


def _slope_sign(s):
    return 1 if (s[2] - s[0]) * (s[3] - s[1]) > 0 else -1


def _cross(s, t, tol=1.0):
    """Do the two segments cross, or touch within tol mm?"""
    def side(ax, ay, bx, by, px, py):
        L = math.hypot(bx - ax, by - ay) or 1.0
        return ((bx - ax) * (py - ay) - (by - ay) * (px - ax)) / L
    d1 = side(t[0], t[1], t[2], t[3], s[0], s[1])
    d2 = side(t[0], t[1], t[2], t[3], s[2], s[3])
    d3 = side(s[0], s[1], s[2], s[3], t[0], t[1])
    d4 = side(s[0], s[1], s[2], s[3], t[2], t[3])
    return ((d1 * d2 <= 0 or min(abs(d1), abs(d2)) <= tol) and
            (d3 * d4 <= 0 or min(abs(d3), abs(d4)) <= tol))


def _merge_collinear(segs: List[tuple]) -> List[Tuple[tuple, List[int]]]:
    """Join pieces of one diagonal. Returns [(merged_seg, [source idx])]."""
    n = len(segs)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    info = []
    for s in segs:
        dx, dy = s[2] - s[0], s[3] - s[1]
        L = math.hypot(dx, dy) or 1.0
        info.append((math.degrees(math.atan2(dy, dx)) % 180.0, dx / L, dy / L, L))
    cell = 3000.0
    grid = {}
    for i, s in enumerate(segs):
        r = _rect(s)
        for gx in range(int(r[0] // cell), int(r[2] // cell) + 1):
            for gy in range(int(r[1] // cell), int(r[3] // cell) + 1):
                grid.setdefault((gx, gy), []).append(i)
                if len(grid) > 400000:
                    break
    seen = set()
    for bucket in grid.values():
        for a in range(len(bucket)):
            i = bucket[a]
            for b in range(a + 1, len(bucket)):
                j = bucket[b]
                if (i, j) in seen:
                    continue
                seen.add((i, j))
                ai, uxi, uyi, Li = info[i]
                aj = info[j][0]
                d = abs(ai - aj)
                if min(d, 180 - d) > COLLINEAR_DEG:
                    continue
                s, t = segs[i], segs[j]
                nx, ny = -uyi, uxi
                off1 = abs(nx * (t[0] - s[0]) + ny * (t[1] - s[1]))
                off2 = abs(nx * (t[2] - s[0]) + ny * (t[3] - s[1]))
                if max(off1, off2) > COLLINEAR_OFFSET_MM:
                    continue
                a0, a1 = 0.0, Li
                b0, b1 = sorted((uxi * (t[0] - s[0]) + uyi * (t[1] - s[1]),
                                 uxi * (t[2] - s[0]) + uyi * (t[3] - s[1])))
                gap = max(b0 - a1, a0 - b1)
                if gap > COLLINEAR_GAP_MM:
                    continue
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out = []
    for idxs in groups.values():
        if len(idxs) == 1:
            out.append((segs[idxs[0]], idxs))
            continue
        # The merged segment runs between the two extreme endpoints.
        base = segs[idxs[0]]
        _a, ux, uy, _L = info[idxs[0]]
        pts = []
        for k in idxs:
            s = segs[k]
            pts += [(s[0], s[1]), (s[2], s[3])]
        proj = [ux * (p[0] - base[0]) + uy * (p[1] - base[1]) for p in pts]
        p0 = pts[proj.index(min(proj))]
        p1 = pts[proj.index(max(proj))]
        L = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        out.append(((p0[0], p0[1], p1[0], p1[1], base[4], L), idxs))
    return out


def union_outline(rects: Sequence[tuple]) -> List[Tuple[float, float]]:
    """Outline of a union of axis-aligned rectangles, as one polygon.

    Built on the grid of the rectangles' own coordinates; the boundary is
    the set of cell edges with a covered cell on one side only, chained
    into a loop. Returns the largest loop when the union is not connected.
    """
    xs = sorted({v for r in rects for v in (r[0], r[2])})
    ys = sorted({v for r in rects for v in (r[1], r[3])})
    if len(xs) < 2 or len(ys) < 2:
        return []
    cov = [[False] * (len(ys) - 1) for _ in range(len(xs) - 1)]
    for r in rects:
        i0, i1 = xs.index(r[0]), xs.index(r[2])
        j0, j1 = ys.index(r[1]), ys.index(r[3])
        for i in range(i0, i1):
            for j in range(j0, j1):
                cov[i][j] = True
    nx, ny = len(xs) - 1, len(ys) - 1

    def c(i, j):
        return 0 <= i < nx and 0 <= j < ny and cov[i][j]

    # Directed edges, counter-clockwise round covered cells.
    nxt = {}
    for i in range(nx):
        for j in range(ny):
            if not cov[i][j]:
                continue
            if not c(i, j - 1):
                nxt[(i, j)] = (i + 1, j)
            if not c(i + 1, j):
                nxt[(i + 1, j)] = (i + 1, j + 1)
            if not c(i, j + 1):
                nxt[(i + 1, j + 1)] = (i, j + 1)
            if not c(i - 1, j):
                nxt[(i, j + 1)] = (i, j)
    loops = []
    while nxt:
        start = next(iter(nxt))
        loop = [start]
        cur = nxt.pop(start)
        guard = 0
        while cur != start and cur in nxt and guard < 100000:
            loop.append(cur)
            cur = nxt.pop(cur)
            guard += 1
        loops.append(loop)
    best = max(loops, key=len)
    pts = [(xs[i], ys[j]) for i, j in best]
    # Drop points in the middle of straight runs.
    out = []
    n = len(pts)
    for k in range(n):
        a, b, cpt = pts[k - 1], pts[k], pts[(k + 1) % n]
        if (a[0] == b[0] == cpt[0]) or (a[1] == b[1] == cpt[1]):
            continue
        out.append(b)
    return out


def polygon_area_mm2(pts):
    a = 0.0
    n = len(pts)
    for k in range(n):
        x1, y1 = pts[k]
        x2, y2 = pts[(k + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def find_x_regions(segs: Sequence[tuple], min_len: float = 80.0,
                   min_axis: float = 60.0, layer_ok=is_x_layer) -> List[Dict]:
    """X-marked regions among raw segments.

    `segs` are (sx, sy, ex, ey, layer, length, ...) in millimetres, as
    dxf_reader._collect_raw_segs returns them. Each region is a dict:
        rects     the rectangles its diagonals mark
        poly      its outline (the union of rects)
        bbox      (x1, y1, x2, y2)
        area_sqm  area of the outline
        cx, cy    centre of the bbox
        seg_idx   indexes into `segs` of the diagonals that make it
    """
    cand, src = [], []
    for k, s in enumerate(segs):
        if not layer_ok(s[4]):
            continue
        dx, dy = abs(s[2] - s[0]), abs(s[3] - s[1])
        if dx < min_axis or dy < min_axis:
            continue
        if math.hypot(dx, dy) < min_len:
            continue
        cand.append(s)
        src.append(k)
    if len(cand) < 2:
        return []
    merged = _merge_collinear(cand)
    diags = [m[0] for m in merged]
    owners = [[src[i] for i in m[1]] for m in merged]
    rects = [_rect(d) for d in diags]
    n = len(diags)

    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Pair crossing diagonals of opposite slope over the same rectangle.
    order = sorted(range(n), key=lambda i: rects[i][0])
    paired = [False] * n
    for a in range(n):
        i = order[a]
        ri = rects[i]
        for b in range(a + 1, n):
            j = order[b]
            rj = rects[j]
            if rj[0] > ri[2]:
                break
            if rj[3] < ri[1] or rj[1] > ri[3]:
                continue
            if _slope_sign(diags[i]) == _slope_sign(diags[j]):
                continue
            ov, _w, _h = _overlap(ri, rj)
            if ov <= 0 or ov / max(_rect_area(ri), _rect_area(rj)) < PAIR_MIN_OVERLAP:
                continue
            if not _cross(diags[i], diags[j]):
                continue
            union(i, j)
            paired[i] = paired[j] = True

    groups = {}
    for i in range(n):
        if paired[i]:
            groups.setdefault(find(i), []).append(i)
    regions = [{"members": idxs,
                "rects": [rects[i] for i in idxs]} for idxs in groups.values()]

    # Crosses whose rectangles overlap are one region (the strips of an L).
    changed = True
    while changed:
        changed = False
        for a in range(len(regions)):
            for b in range(a + 1, len(regions)):
                ra, rb = regions[a], regions[b]
                hit = False
                for p in ra["rects"]:
                    for q in rb["rects"]:
                        ov, w, h = _overlap(p, q)
                        if w > REGION_TOUCH_MM and h > REGION_TOUCH_MM:
                            hit = True
                            break
                    if hit:
                        break
                if hit:
                    ra["members"] += rb["members"]
                    ra["rects"] += rb["rects"]
                    regions.pop(b)
                    changed = True
                    break
            if changed:
                break

    out = []
    for r in regions:
        rect_set = list({tuple(round(v, 1) for v in q) for q in r["rects"]})
        poly = union_outline(rect_set)
        if len(poly) < 3:
            continue
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        seg_idx = sorted({k for m in r["members"] for k in owners[m]})
        out.append({"rects": rect_set, "poly": poly, "bbox": bbox,
                    "area_sqm": polygon_area_mm2(poly) / 1e6,
                    "cx": (bbox[0] + bbox[2]) / 2.0,
                    "cy": (bbox[1] + bbox[3]) / 2.0,
                    "seg_idx": seg_idx})
    return out
