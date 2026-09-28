# -*- coding: utf-8 -*-
"""Pure geometry primitives.

Deliberately free of any ezdxf / Flask import so it can be unit-tested
without a CAD file and reused by every future quantity module.

All coordinates handled here are in MILLIMETRES. Conversion from raw
drawing units happens once, at ingest, in units.py.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

Point = Tuple[float, float]
Segment = Tuple[float, float, float, float]

MM2_PER_M2 = 1_000_000.0
MM_PER_M = 1_000.0


# -- basic ------------------------------------------------------------------
def dist(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(bx - ax, by - ay)


def bbox(points: Sequence[Point]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def point_in_polygon(x: float, y: float, poly: Sequence[Point]) -> bool:
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


# -- measurement ------------------------------------------------------------
def shoelace_area_sqm(points: Sequence[Point]) -> float:
    """Absolute polygon area in square metres, from millimetre coordinates."""
    n = len(points)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0 / MM2_PER_M2


def polyline_length_m(points: Sequence[Point], closed: bool = False) -> float:
    """Total run length of an open or closed polyline, in metres."""
    n = len(points)
    if n < 2:
        return 0.0
    total = 0.0
    for i in range(n - 1):
        total += dist(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
    if closed and n > 2:
        total += dist(points[-1][0], points[-1][1], points[0][0], points[0][1])
    return total / MM_PER_M


def polygon_perimeter_m(points: Sequence[Point]) -> float:
    """Perimeter of a closed polygon in metres.

    Tolerates a duplicated closing vertex (first point repeated at the end),
    which is how ezdxf hands back some closed polylines.
    """
    pts = list(points)
    if len(pts) >= 2 and dist(pts[0][0], pts[0][1], pts[-1][0], pts[-1][1]) < 1e-9:
        pts = pts[:-1]
    return polyline_length_m(pts, closed=True)


def segment_length_m(seg: Segment) -> float:
    sx, sy, ex, ey = seg
    return dist(sx, sy, ex, ey) / MM_PER_M


# -- curved geometry --------------------------------------------------------
def bulge_to_arc_points(p1: Point, p2: Point, bulge: float,
                        sagitta_tol: float = 5.0,
                        max_segments: int = 64) -> List[Point]:
    """Flatten a bulged (arc) polyline span into intermediate points.

    `bulge` is the DXF convention: tan(included_angle / 4). Returns the
    intermediate vertices only, excluding p1 and p2, so the caller can splice
    them in. `sagitta_tol` is the maximum chord deviation in millimetres.

    Dropping bulges is what made arc-cornered outlines under-report their
    area; this restores it.
    """
    if not bulge:
        return []
    x1, y1 = p1
    x2, y2 = p2
    chord = dist(x1, y1, x2, y2)
    if chord < 1e-9:
        return []

    theta = 4.0 * math.atan(abs(bulge))          # included angle, positive
    if theta < 1e-9:
        return []
    radius = chord / (2.0 * math.sin(theta / 2.0))
    if not math.isfinite(radius) or radius <= 0:
        return []

    # Number of segments needed to keep the sagitta under tolerance.
    ratio = max(0.0, min(1.0, 1.0 - sagitta_tol / radius))
    per_seg = 2.0 * math.acos(ratio) if ratio < 1.0 else theta
    n = 1 if per_seg <= 0 else int(math.ceil(theta / per_seg))
    n = max(1, min(max_segments, n))
    if n == 1:
        return []

    # Arc centre: offset from the chord midpoint along the chord's LEFT
    # normal. The signed offset d = (chord/2) / tan(theta/2) is negative
    # for a major arc (theta > pi), which flips the centre to the other
    # side automatically. `sign` carries the DXF convention: a positive
    # bulge is counter-clockwise from p1 to p2, which puts the centre on
    # the left of the chord; a negative bulge mirrors it.
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    ux, uy = (x2 - x1) / chord, (y2 - y1) / chord
    nx, ny = -uy, ux
    sign = 1.0 if bulge > 0 else -1.0
    half = theta / 2.0
    tan_half = math.tan(half)
    d = 0.0 if abs(tan_half) > 1e12 else (chord / 2.0) / tan_half
    cx, cy = mx + sign * d * nx, my + sign * d * ny

    a1 = math.atan2(y1 - cy, x1 - cx)
    sweep = theta * sign

    out: List[Point] = []
    for i in range(1, n):
        a = a1 + sweep * (i / float(n))
        out.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    return out


def arc_points(cx: float, cy: float, radius: float,
               start_deg: float, end_deg: float,
               sagitta_tol: float = 5.0, max_segments: int = 128) -> List[Point]:
    """Flatten a standalone ARC entity into a point list (inclusive of ends)."""
    if radius <= 0:
        return []
    sweep = (end_deg - start_deg) % 360.0
    if sweep == 0:
        sweep = 360.0
    theta = math.radians(sweep)
    ratio = max(0.0, min(1.0, 1.0 - sagitta_tol / radius))
    per_seg = 2.0 * math.acos(ratio) if ratio < 1.0 else theta
    n = 1 if per_seg <= 0 else int(math.ceil(theta / per_seg))
    n = max(2, min(max_segments, n))
    a0 = math.radians(start_deg)
    return [(cx + radius * math.cos(a0 + theta * i / n),
             cy + radius * math.sin(a0 + theta * i / n)) for i in range(n + 1)]


def circle_points(cx: float, cy: float, radius: float,
                  sagitta_tol: float = 5.0) -> List[Point]:
    pts = arc_points(cx, cy, radius, 0.0, 360.0, sagitta_tol)
    return pts[:-1] if len(pts) > 1 else pts


def polygon_centroid(points: Sequence[Point]) -> Point:
    """Shoelace centroid, falling back to the vertex mean when degenerate."""
    n = len(points)
    if n == 0:
        return (0.0, 0.0)
    if n < 3:
        return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n)
    A = cx = cy = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        A += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    A *= 0.5
    if abs(A) < 1e-9:
        return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n)
    return (cx / (6.0 * A), cy / (6.0 * A))


# -- spatial index ----------------------------------------------------------
class BandIndex:
    """Exact 1-D interval index over wall segments.

    _hv_boundary() previously scanned every segment for every text label and
    every X-cluster, which is the dominant cost on large drawings. Segments
    are bucketed into fixed-width bands on the axis their span covers, so a
    ray only tests the segments that could possibly cross it.

    The result is identical to the linear scan; only the candidate set shrinks.
    """

    __slots__ = ("band", "vertical", "horizontal")

    def __init__(self, segments: Iterable[Segment], band: float = 2000.0,
                 axis_ratio: float = 6.0):
        self.band = float(band) if band and band > 0 else 2000.0
        self.vertical = {}      # keyed by y-band  -> segments blocking horizontal rays
        self.horizontal = {}    # keyed by x-band  -> segments blocking vertical rays
        for seg in segments:
            sx, sy, ex, ey = seg
            dx, dy = abs(ex - sx), abs(ey - sy)
            # Skip strongly slanted segments: arrowheads, hatching, leaders.
            if dx > 0 and dy > 0 and max(dx, dy) / min(dx, dy) < axis_ratio:
                continue
            if dy > dx:
                self._add(self.vertical, min(sy, ey), max(sy, ey), seg)
            elif dx > dy:
                self._add(self.horizontal, min(sx, ex), max(sx, ex), seg)

    def _add(self, table, lo, hi, seg):
        b = self.band
        i0, i1 = int(math.floor(lo / b)), int(math.floor(hi / b))
        # Guard against a pathological segment spanning the whole drawing.
        if i1 - i0 > 10000:
            i1 = i0 + 10000
        for i in range(i0, i1 + 1):
            table.setdefault(i, []).append(seg)

    def crossing_horizontal_ray(self, py: float) -> List[Segment]:
        """Vertical segments whose y-span may contain py."""
        return self.vertical.get(int(math.floor(py / self.band)), ())

    def crossing_vertical_ray(self, px: float) -> List[Segment]:
        """Horizontal segments whose x-span may contain px."""
        return self.horizontal.get(int(math.floor(px / self.band)), ())

    def __len__(self):
        return sum(len(v) for v in self.vertical.values()) + \
               sum(len(v) for v in self.horizontal.values())
