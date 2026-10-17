# -*- coding: utf-8 -*-
"""Real room-boundary tracing.

The ray-cast boundary fires four cardinal rays from a label and takes the
resulting bounding box, which is only correct for a rectangular room
aligned to the axes. Angled walls, curved walls, L-shaped rooms and
anything with a re-entrant corner come out wrong.

This traces the region the walls actually enclose:

  1. rasterise the nearby wall segments onto a grid as barriers;
  2. flood-fill outward from the label, so the fill follows whatever
     shape the walls make;
  3. walk the filled/unfilled frontier into closed loops;
  4. simplify, then project each edge back onto the wall it came from.

**Windowed, not drawing-wide.** The first version built one grid over the
whole drawing. On a real file that spans 1.2 million units - because of
content parked far from the plan - that came to 575 million cells, blew
the memory guard, and silently disabled tracing so every room quietly
fell back to a rectangle. Grids are now local: a window around the seed,
widened only if the fill reaches its edge. A typical room resolves in a
160 x 160 grid instead of 24,000 x 23,000.

Two failure modes are reported rather than papered over:

  * **leak** - the fill escapes through a gap in the walls even at the
    widest window, or exceeds the maximum room area;
  * **no_walls** - nothing to enclose the seed at all.

Pure Python and free of ezdxf, so it is directly testable.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

try:                        # ezdxf depends on numpy, so it is normally here
    import numpy as _np
except ImportError:         # pragma: no cover
    _np = None

Point = Tuple[float, float]
Segment = Tuple[float, float, float, float]

MM2_PER_M2 = 1_000_000.0

DEFAULT_CELL_MM = 50.0
# Openings up to this wide are bridged before the fill runs, so a boundary
# runs along the wall past a doorway or a cubicle entrance instead of
# reaching in through it. Nothing is drawn across; the walls are thickened
# for the fill and thinned again before the outline is taken.
DEFAULT_SEAL_MM = 900.0
DEFAULT_MAX_AREA_SQM = 1000.0
DEFAULT_SIMPLIFY_CELLS = 1.2

# Window half-sizes tried in turn, in millimetres. Most rooms resolve at
# the first or second; the last is a backstop before declaring a leak.
WINDOW_STEPS_MM = (4000.0, 9000.0, 18000.0, 36000.0)
MAX_GRID_CELLS = 4_000_000


class TraceResult:
    """Outcome of one flood fill."""

    __slots__ = ("polygon", "holes", "area_sqm", "gross_area_sqm",
                 "cell_area_sqm", "status", "cells", "bbox", "window_mm")

    def __init__(self, polygon=None, area_sqm=0.0, cell_area_sqm=0.0,
                 status="ok", cells=0, bbox=None, holes=None,
                 gross_area_sqm=0.0, window_mm=0.0):
        self.polygon = polygon or []
        self.holes = holes or []
        self.area_sqm = area_sqm            # net of interior voids
        self.gross_area_sqm = gross_area_sqm
        self.cell_area_sqm = cell_area_sqm
        self.status = status
        self.cells = cells
        self.bbox = bbox
        self.window_mm = window_mm

    @property
    def ok(self):
        return self.status == "ok" and len(self.polygon) >= 3

    def to_ui(self):
        return {"status": self.status, "area_sqm": round(self.area_sqm, 2),
                "gross_area_sqm": round(self.gross_area_sqm, 2),
                "holes": len(self.holes), "vertices": len(self.polygon)}

    def __repr__(self):
        return "TraceResult({0}, {1:.2f} sq.m, {2} pts)".format(
            self.status, self.area_sqm, len(self.polygon))


class _SegmentIndex:
    """Coarse bucket lookup over wall segments."""

    __slots__ = ("cell", "buckets", "count")

    def __init__(self, segments, cell=4000.0):
        self.cell = float(cell)
        self.buckets = {}
        self.count = 0
        for seg in segments:
            sx, sy, ex, ey = seg[0], seg[1], seg[2], seg[3]
            if sx == ex and sy == ey:
                continue
            self.count += 1
            steps = int(max(abs(ex - sx), abs(ey - sy)) / self.cell) + 1
            steps = min(steps, 4000)
            seen = set()
            for i in range(steps + 1):
                t = i / float(steps)
                key = (int((sx + (ex - sx) * t) // self.cell),
                       int((sy + (ey - sy) * t) // self.cell))
                if key not in seen:
                    seen.add(key)
                    self.buckets.setdefault(key, []).append(seg)

    def in_window(self, mn_x, mn_y, mx_x, mx_y):
        c = self.cell
        out = []
        seen = set()
        for gx in range(int(mn_x // c), int(mx_x // c) + 1):
            for gy in range(int(mn_y // c), int(mx_y // c) + 1):
                for seg in self.buckets.get((gx, gy), ()):
                    key = id(seg)
                    if key not in seen:
                        seen.add(key)
                        out.append(seg)
        return out

    def near(self, x, y):
        c = self.cell
        cx, cy = int(x // c), int(y // c)
        out = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                out.extend(self.buckets.get((cx + dx, cy + dy), ()))
        return out

    def any_in_window(self, mn_x, mn_y, mx_x, mx_y):
        c = self.cell
        for gx in range(int(mn_x // c), int(mx_x // c) + 1):
            for gy in range(int(mn_y // c), int(mx_y // c) + 1):
                if self.buckets.get((gx, gy)):
                    return True
        return False


class _OverlayIndex:
    """A base index with a few more segments on top, answering exactly as
    one index built over base + extra would: bucket by bucket the base's
    segments first, then the extra ones - the order the nearest-wall
    choice in _snap_to_walls depends on when two walls tie."""

    __slots__ = ("base", "extra", "cell", "count")

    def __init__(self, base, extra):
        self.base, self.extra = base, extra
        self.cell = base.cell
        self.count = base.count + extra.count

    def in_window(self, mn_x, mn_y, mx_x, mx_y):
        c = self.cell
        out = []
        seen = set()
        bb, eb = self.base.buckets, self.extra.buckets
        for gx in range(int(mn_x // c), int(mx_x // c) + 1):
            for gy in range(int(mn_y // c), int(mx_y // c) + 1):
                for src in (bb, eb):
                    for seg in src.get((gx, gy), ()):
                        key = id(seg)
                        if key not in seen:
                            seen.add(key)
                            out.append(seg)
        return out

    def near(self, x, y):
        c = self.cell
        cx, cy = int(x // c), int(y // c)
        out = []
        bb, eb = self.base.buckets, self.extra.buckets
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                out.extend(bb.get((cx + dx, cy + dy), ()))
                out.extend(eb.get((cx + dx, cy + dy), ()))
        return out


def _copy_result(r):
    """A TraceResult a caller may change without touching the cached one."""
    return TraceResult(polygon=[tuple(p) for p in r.polygon], area_sqm=r.area_sqm,
                       cell_area_sqm=r.cell_area_sqm, status=r.status, cells=r.cells,
                       bbox=r.bbox, holes=[list(h) for h in r.holes],
                       gross_area_sqm=r.gross_area_sqm, window_mm=r.window_mm)


RASTER_CACHE_MAX = 48            # base wall rasters kept per tracer family


class RoomTracer:
    """Wall segments indexed once; each label traced in its own window."""

    def __init__(self, segments: Sequence[Segment],
                 cell_mm: float = DEFAULT_CELL_MM,
                 max_area_sqm: float = DEFAULT_MAX_AREA_SQM,
                 window_steps=WINDOW_STEPS_MM,
                 seal_openings_mm: float = DEFAULT_SEAL_MM):
        self.cell = float(cell_mm) if cell_mm and cell_mm > 0 else DEFAULT_CELL_MM
        # Half the opening, in cells: walls grow by this much from both
        # sides, so a gap up to twice it closes.
        self.seal = max(0, int(round((float(seal_openings_mm or 0.0) / 2.0)
                                     / self.cell)))
        self.max_area_sqm = float(max_area_sqm)
        self.max_cells = max(1, int((self.max_area_sqm * MM2_PER_M2) /
                                    (self.cell * self.cell)))
        self.windows = tuple(window_steps)
        self.segments = list(segments)
        self.index = _SegmentIndex(self.segments,
                                   cell=max(self.cell * 40, 2000.0))
        self.ok = bool(self.segments)
        # A tracer extended with a few more segments (extended()) shares
        # its base's index and wall rasters: the base walls are indexed
        # and rasterised once, the extra lines added on top.
        self._root = self
        self._extra = None               # _SegmentIndex of the extra segments
        self._rasters = {}               # window -> (base wall raster, any base segment)
        self._windows = {}               # (px, py, half, simplify, snap) -> TraceResult

    def extended(self, extra):
        """The same tracer over these segments plus `extra` - exactly what
        RoomTracer(segments + extra, same settings) traces, without
        indexing and rasterising the base walls again."""
        extra = list(extra)
        root = self._root
        t = RoomTracer.__new__(RoomTracer)
        t.cell, t.seal = self.cell, self.seal
        t.max_area_sqm, t.max_cells = self.max_area_sqm, self.max_cells
        t.windows = self.windows
        t.segments = self.segments + extra
        own = (list(self._extra_segs) if self._extra is not None else []) + extra
        t._extra_segs = own
        t._extra = _SegmentIndex(own, cell=root.index.cell)
        t.index = _OverlayIndex(root.index, t._extra)
        t.ok = bool(t.segments)
        t._root = root
        t._rasters = root._rasters
        t._windows = {}
        return t

    # -- public ----------------------------------------------------------
    def trace(self, px: float, py: float,
              simplify_cells: float = DEFAULT_SIMPLIFY_CELLS,
              snap: bool = True) -> TraceResult:
        if not self.ok:
            return TraceResult(status="no_walls")

        last = TraceResult(status="leaked")
        for half in self.windows:
            res = self._window_cached(px, py, half, simplify_cells, snap)
            if res.status == "ok":
                return res
            if res.status == "expand":
                last = TraceResult(status="leaked", window_mm=half)
                continue
            last = res
            if res.status == "too_big":
                break
        return last

    # -- one window ------------------------------------------------------
    def _window_cached(self, px, py, half, simplify_cells, snap):
        """_trace_window, once per window: the same label is traced again
        and again from the same point (a leak closed one opening at a time,
        a stated size checked). An extended tracer whose extra lines come
        nowhere near the window - nor within snapping reach of it - sees
        exactly the base walls there, so it takes the base's answer."""
        key = (px, py, half, simplify_cells, snap)
        got = self._windows.get(key)
        if got is None:
            root = self._root
            if root is not self:
                pad = 4.0 * self.cell        # beyond the snap reach (2.5 cells)
                if not self._extra.any_in_window(px - half - pad, py - half - pad,
                                                 px + half + pad, py + half + pad):
                    got = root._window_cached(px, py, half, simplify_cells, snap)
                    self._windows[key] = got
                    return _copy_result(got)
            got = self._trace_window(px, py, half, simplify_cells, snap)
            self._windows[key] = got
        return _copy_result(got)

    def _wall_raster(self, mn_x, mn_y, mx_x, mx_y, nx, ny):
        """(wall raster, any segment in the window): the base walls,
        rasterised once per window and kept, with this tracer's extra
        segments drawn on top."""
        cell = self.cell
        root = self._root
        key = (mn_x, mn_y, nx, ny)
        got = root._rasters.get(key)
        if got is None:
            segs = root.index.in_window(mn_x, mn_y, mx_x, mx_y)
            base = bytearray(nx * ny)
            for seg in segs:
                clipped = _clip(seg[0], seg[1], seg[2], seg[3],
                                mn_x, mn_y, mx_x, mx_y)
                if clipped is None:
                    continue
                _rasterise(base, nx, ny, mn_x, mn_y, cell, *clipped)
            got = (bytes(base), bool(segs))
            if len(root._rasters) >= RASTER_CACHE_MAX:
                root._rasters.pop(next(iter(root._rasters)))
            root._rasters[key] = got
        wall = bytearray(got[0])
        any_seg = got[1]
        if self._extra is not None:
            for seg in self._extra.in_window(mn_x, mn_y, mx_x, mx_y):
                any_seg = True
                clipped = _clip(seg[0], seg[1], seg[2], seg[3],
                                mn_x, mn_y, mx_x, mx_y)
                if clipped is None:
                    continue
                _rasterise(wall, nx, ny, mn_x, mn_y, cell, *clipped)
        return wall, any_seg

    def _trace_window(self, px, py, half, simplify_cells, snap):
        mn_x, mn_y = px - half, py - half
        mx_x, mx_y = px + half, py + half
        cell = self.cell
        nx = int((mx_x - mn_x) / cell) + 3
        ny = int((my := (mx_y - mn_y)) / cell) + 3
        del my
        if nx * ny > MAX_GRID_CELLS:
            return TraceResult(status="too_big")

        wall, any_seg = self._wall_raster(mn_x, mn_y, mx_x, mx_y, nx, ny)
        if not any_seg:
            # No walls this close: the room is bigger than the window, not
            # unwalled. Widen rather than give up - a 40 x 25 m hall has
            # nothing within 4 m of its centre.
            return TraceResult(status="expand")

        sx = int((px - mn_x) // cell)
        sy = int((py - mn_y) // cell)
        if not (0 <= sx < nx and 0 <= sy < ny):
            return TraceResult(status="expand")

        # Thicken the walls so that doorways and cubicle entrances close.
        # The fill then follows the wall line past them instead of
        # reaching in, and the outline is pushed back onto the real wall
        # afterwards. A space narrower than the thickening would vanish
        # altogether, so anything that leaves the seed with nowhere to go
        # is traced on the walls as drawn.
        grid, sealed = wall, False
        if self.seal:
            grown = _dilate(wall, nx, ny, self.seal)
            if not grown[sy * nx + sx] or _free_neighbours(grown, nx, ny,
                                                           sx, sy, limit=4):
                grid, sealed = grown, True

        # A label can sit on a wall pixel or in a doorway. Try the exact
        # cell first, then nearby free cells, and keep the first fill that
        # does not run away - otherwise one unlucky seed on the outside
        # face of a wall loses the room entirely.
        seeds = []
        if not grid[sy * nx + sx]:
            seeds.append(sy * nx + sx)
        seeds.extend(_free_neighbours(grid, nx, ny, sx, sy, limit=8))
        if not seeds:
            return TraceResult(status="on_wall")

        filled = None
        touched_edge = False
        explored = set()
        explored_mask = None
        labels = None
        masks = _np is not None and _has_scipy()
        for seed in seeds:
            # A seed inside a region already flooded from another seed
            # would only flood the same region again. On a leaking room
            # that repeated a multi-second fill up to nine times.
            if seed in explored or (explored_mask is not None and explored_mask[seed]):
                continue
            if masks:
                # the fill as an array: a leaking window is only counted,
                # never turned into a set of cells; the window's free cells
                # are labelled once for all its seeds
                if labels is None:
                    labels = _label_free(grid, nx, ny)
                got, count, edge, overflow = _flood_mask(grid, nx, ny, seed, self.max_cells, labels)
            else:
                got, edge, overflow = _flood(grid, nx, ny, seed, self.max_cells)
                count = len(got)
            if overflow:
                return TraceResult(status="leaked", cells=count)
            if edge:
                touched_edge = True
                if masks:
                    explored_mask = got if explored_mask is None else (explored_mask | got)
                else:
                    explored |= got
                explored |= _local_region(grid, nx, ny, seed, sx, sy, 8)
                continue
            if count:
                filled = got
                break
        if filled is None:
            # The fill reached the window border: the room is bigger than
            # this window, so widen it rather than calling it a leak.
            return TraceResult(status="expand" if touched_edge else "expand")

        if sealed:
            # Give back what the thickening took: grow the filled region by
            # the same amount, stopping at the walls as they were drawn.
            filled = (_grow_mask(filled, wall, nx, ny, self.seal) if masks
                      else _grow_into(filled, wall, nx, ny, self.seal))
        elif masks:
            filled = set(_np.flatnonzero(filled).tolist())

        return self._contour(filled, nx, mn_x, mn_y, simplify_cells, snap,
                             half)

    def _contour(self, filled, nx, mn_x, mn_y, simplify_cells, snap, half):
        loops = _boundary_loops(filled, nx)
        if not loops:
            return TraceResult(status="no_contour", cells=len(filled))

        # The outline encloses every hole, so it is the loop with the
        # largest area. The longest loop is not always it: a hole with a
        # ragged edge can take more grid steps than the room around it.
        loops.sort(key=_shoelace, reverse=True)
        outer, hole_loops = loops[0], loops[1:]
        tol = simplify_cells * self.cell

        def to_poly(lp):
            p = [(mn_x + gx * self.cell, mn_y + gy * self.cell)
                 for gx, gy in lp]
            p = _drop_collinear(p)
            if tol > 0 and len(p) > 4:
                p = _douglas_peucker(p + [p[0]], tol)[:-1]
                p = _drop_collinear(p)
            return p

        poly = to_poly(outer)
        holes = [h for h in (to_poly(lp) for lp in hole_loops) if len(h) >= 3]

        # Grid tracing stops one cell short of every wall, under-reporting
        # by roughly perimeter x cell size - about 2% on a small room.
        # Projecting each edge back onto the wall it came from removes the
        # bias and makes straight-walled rooms exact at any angle.
        if snap:
            poly = _checked_snap(poly, self.index, 2.5 * self.cell, self.cell)
            holes = [_checked_snap(h, self.index, 2.5 * self.cell, self.cell)
                     for h in holes]

        gross = _shoelace(poly) / MM2_PER_M2
        hole_area = sum(_shoelace(h) for h in holes) / MM2_PER_M2
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        return TraceResult(polygon=poly, holes=holes,
                           area_sqm=gross - hole_area, gross_area_sqm=gross,
                           cell_area_sqm=len(filled) * self.cell * self.cell
                           / MM2_PER_M2,
                           status="ok", cells=len(filled),
                           bbox=(min(xs), min(ys), max(xs), max(ys)),
                           window_mm=half)


# -- raster helpers ---------------------------------------------------------
def _clip(x0, y0, x1, y1, mn_x, mn_y, mx_x, mx_y):
    """Liang-Barsky clip. Without it a wall crossing the window from far
    away would rasterise across millions of out-of-range cells."""
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - mn_x), (dx, mx_x - x0),
                 (-dy, y0 - mn_y), (dy, mx_y - y0)):
        if p == 0:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return None
            if r > t0:
                t0 = r
        else:
            if r < t0:
                return None
            if r < t1:
                t1 = r
    return (x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy)


def _rasterise(wall, nx, ny, mn_x, mn_y, cell, sx, sy, ex, ey):
    """Bresenham. A one-cell-wide wall stops a 4-connected fill, which is
    why the fill never moves diagonally: it would squeeze through the
    corner of a thin diagonal wall."""
    x0 = int((sx - mn_x) // cell)
    y0 = int((sy - mn_y) // cell)
    x1 = int((ex - mn_x) // cell)
    y1 = int((ey - mn_y) // cell)
    # A horizontal or vertical run - most walls - visits every cell from
    # one end to the other and nothing else: set it as one slice, the
    # same cells the step-by-step walk below sets.
    if y0 == y1:
        if 0 <= y0 < ny:
            a, b = (x0, x1) if x0 <= x1 else (x1, x0)
            a, b = max(a, 0), min(b, nx - 1)
            if a <= b:
                row = y0 * nx
                wall[row + a:row + b + 1] = b"\x01" * (b - a + 1)
        return
    if x0 == x1:
        if 0 <= x0 < nx:
            a, b = (y0, y1) if y0 <= y1 else (y1, y0)
            a, b = max(a, 0), min(b, ny - 1)
            if a <= b:
                wall[a * nx + x0:b * nx + x0 + 1:nx] = b"\x01" * (b - a + 1)
        return
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    step_x = 1 if x0 < x1 else -1
    step_y = 1 if y0 < y1 else -1
    err = dx + dy
    guard = dx - dy + 4
    while guard >= 0:
        guard -= 1
        if 0 <= x0 < nx and 0 <= y0 < ny:
            wall[y0 * nx + x0] = 1
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += step_x
        if e2 <= dx:
            err += dx
            y0 += step_y


def _dilate(wall, nx, ny, radius):
    """Thicken the wall raster by `radius` cells, each axis in turn.

    Two one-dimensional passes rather than a square stamp per wall cell:
    the result is the same square structuring element at a fraction of
    the cost, which matters because this runs per label per window.
    """
    if radius <= 0:
        return wall
    if _np is not None:
        return _dilate_np(wall, nx, ny, radius)
    out = bytearray(nx * ny)
    for y in range(ny):
        row = y * nx
        x = 0
        while x < nx:
            if wall[row + x]:
                start = x
                while x < nx and wall[row + x]:
                    x += 1
                lo = max(0, start - radius)
                hi = min(nx, x + radius)
                for i in range(lo, hi):
                    out[row + i] = 1
            else:
                x += 1
    grown = bytearray(out)
    for x in range(nx):
        y = 0
        while y < ny:
            if out[y * nx + x]:
                start = y
                while y < ny and out[y * nx + x]:
                    y += 1
                lo = max(0, start - radius)
                hi = min(ny, y + radius)
                for j in range(lo, hi):
                    grown[j * nx + x] = 1
            else:
                y += 1
    return grown


def _dilate_np(wall, nx, ny, radius):
    """_dilate with numpy: the same square element, a few array shifts
    instead of a Python loop over every cell. On a 36 m window that is two
    million cells, and a leaking room is thickened in every window tried."""
    a = _np.frombuffer(bytes(wall), dtype=_np.uint8).reshape(ny, nx)
    h = a.copy()
    for k in range(1, radius + 1):
        h[:, k:] |= a[:, :-k]
        h[:, :-k] |= a[:, k:]
    v = h.copy()
    for k in range(1, radius + 1):
        v[k:, :] |= h[:-k, :]
        v[:-k, :] |= h[k:, :]
    return bytearray(v.tobytes())


def _grow_into(filled, wall, nx, ny, radius):
    """Push the filled region back out to the walls as they were drawn.

    The fill ran against thickened walls, so it stops short of the real
    face by up to `radius`. Growing it back - never into a wall cell, and
    never further than the thickening took - lands the outline on the
    wall again while the sealed openings stay sealed.
    """
    if radius <= 0 or not filled:
        return filled
    try:
        import numpy as np
        from scipy import ndimage
    except ImportError:
        np = None
    if np is not None:
        # The same growth, a step at a time over the whole grid in C.
        blocked = np.frombuffer(bytes(wall), dtype=np.uint8).reshape(ny, nx) != 0
        cur = np.zeros(ny * nx, dtype=bool)
        cur[np.fromiter(filled, dtype=np.int64, count=len(filled))] = True
        cur = cur.reshape(ny, nx)
        square = np.ones((3, 3), dtype=bool)
        for _ in range(radius):
            grown = ndimage.binary_dilation(cur, structure=square) & ~blocked
            if not (grown & ~cur).any():
                break
            cur |= grown
        return set(np.flatnonzero(cur).tolist())
    current = set(filled)
    frontier = set(filled)
    for _ in range(radius):
        nxt = set()
        for idx in frontier:
            y, x = divmod(idx, nx)
            # Eight directions, because the thickening was a square:
            # growing back in four would leave the corners short and the
            # room would measure a little less than it does.
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                           (1, 1), (1, -1), (-1, 1), (-1, -1)):
                ax, ay = x + dx, y + dy
                if not (0 <= ax < nx and 0 <= ay < ny):
                    continue
                j = ay * nx + ax
                if wall[j] or j in current:
                    continue
                current.add(j)
                nxt.add(j)
        if not nxt:
            break
        frontier = nxt
    return current


def _flood(wall, nx, ny, start, max_cells):
    """Returns (filled_set, touched_window_edge, overflowed).

    The free cells 4-connected to `start`, labelled in one pass by scipy
    (C) rather than walked cell by cell in Python: on a 65 MB sheet the
    walk was most of the boundary run. The plain walk below stays for a
    machine without scipy."""
    try:
        import numpy as np
        from scipy import ndimage
    except ImportError:
        return _flood_walk(wall, nx, ny, start, max_cells)
    if wall[start]:
        return set(), False, False
    free = np.frombuffer(bytes(wall), dtype=np.uint8).reshape(ny, nx) == 0
    labels, _n = ndimage.label(free)
    sy, sx = divmod(start, nx)
    mask = labels == labels[sy, sx]
    count = int(mask.sum())
    edge = bool(mask[0, :].any() or mask[-1, :].any()
                or mask[:, 0].any() or mask[:, -1].any())
    filled = set(np.flatnonzero(mask).tolist())
    if count > max_cells:
        return filled, False, True
    if edge:
        return filled, True, False
    return filled, False, False


_SCIPY = []


def _has_scipy():
    if not _SCIPY:
        try:
            from scipy import ndimage  # noqa: F401
            _SCIPY.append(True)
        except ImportError:
            _SCIPY.append(False)
    return _SCIPY[0]


def _label_free(wall, nx, ny):
    """The 4-connected components of the free cells (scipy's default
    cross structure), as _flood labels them."""
    from scipy import ndimage
    free = _np.frombuffer(bytes(wall), dtype=_np.uint8).reshape(ny, nx) == 0
    labels, _n = ndimage.label(free)
    return labels


def _flood_mask(wall, nx, ny, start, max_cells, labels=None):
    """_flood with the region as a flat boolean array:
    (mask, count, touched_window_edge, overflowed). The same cells, the
    same tests in the same order; no set is built."""
    if wall[start]:
        return None, 0, False, False
    if labels is None:
        labels = _label_free(wall, nx, ny)
    sy, sx = divmod(start, nx)
    mask = labels == labels[sy, sx]
    count = int(mask.sum())
    edge = bool(mask[0, :].any() or mask[-1, :].any()
                or mask[:, 0].any() or mask[:, -1].any())
    flat = mask.reshape(-1)
    if count > max_cells:
        return flat, count, False, True
    if edge:
        return flat, count, True, False
    return flat, count, False, False


def _grow_mask(mask, wall, nx, ny, radius):
    """_grow_into from a flat boolean mask; returns the same set."""
    from scipy import ndimage
    if radius <= 0 or not mask.any():
        return set(_np.flatnonzero(mask).tolist())
    full = mask.reshape(ny, nx)
    # Growth reaches at most `radius` cells past the region: work on the
    # box round it (one cell more), which gives the same cells as the
    # whole window.
    rows = _np.flatnonzero(full.any(axis=1))
    cols = _np.flatnonzero(full.any(axis=0))
    r0, r1 = max(0, rows[0] - radius - 1), min(ny, rows[-1] + radius + 2)
    c0, c1 = max(0, cols[0] - radius - 1), min(nx, cols[-1] + radius + 2)
    blocked = (_np.frombuffer(bytes(wall), dtype=_np.uint8).reshape(ny, nx) != 0)[r0:r1, c0:c1]
    cur = full[r0:r1, c0:c1].copy()
    square = _np.ones((3, 3), dtype=bool)
    for _ in range(radius):
        grown = ndimage.binary_dilation(cur, structure=square) & ~blocked
        if not (grown & ~cur).any():
            break
        cur |= grown
    ys, xs = _np.nonzero(cur)
    return set(((ys + r0) * nx + (xs + c0)).tolist())


def _flood_walk(wall, nx, ny, start, max_cells):
    """Returns (filled_set, touched_window_edge, overflowed)."""
    filled = set()
    stack = [start]
    seen = {start}
    edge = False
    while stack:
        idx = stack.pop()
        if wall[idx]:
            continue
        filled.add(idx)
        if len(filled) > max_cells:
            return filled, edge, True
        cy, cx = divmod(idx, nx)
        if cx == 0 or cx == nx - 1 or cy == 0 or cy == ny - 1:
            return filled, True, False
        for nidx in (idx - 1, idx + 1, idx - nx, idx + nx):
            if nidx not in seen and not wall[nidx]:
                seen.add(nidx)
                stack.append(nidx)
    return filled, edge, False


def _local_region(wall, nx, ny, start, cx, cy, r):
    """Free cells joined to `start` inside the box of radius r round (cx, cy).

    Every seed the tracer tries lies in that box, so a seed found here is
    in the same region as `start` and would flood exactly as it did.
    """
    x0, x1 = max(0, cx - r), min(nx - 1, cx + r)
    y0, y1 = max(0, cy - r), min(ny - 1, cy + r)
    seen = {start}
    stack = [start]
    while stack:
        idx = stack.pop()
        y, x = divmod(idx, nx)
        for ax, ay in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if x0 <= ax <= x1 and y0 <= ay <= y1:
                j = ay * nx + ax
                if j not in seen and not wall[j]:
                    seen.add(j)
                    stack.append(j)
    return seen


def _free_neighbours(wall, nx, ny, sx, sy, limit=8):
    out = []
    for r in range(1, 8):
        ring = []
        for dx in range(-r, r + 1):
            for dy in (-r, r):
                ring.append((sx + dx, sy + dy))
        for dy in range(-r + 1, r):
            for dx in (-r, r):
                ring.append((sx + dx, sy + dy))
        for x, y in ring:
            if 0 <= x < nx and 0 <= y < ny and not wall[y * nx + x]:
                out.append(y * nx + x)
                if len(out) >= limit:
                    return out
    return out


# -- contour ----------------------------------------------------------------
def _boundary_loops(filled, nx):
    """Stitch the filled/unfilled frontier into closed loops of grid nodes.

    Each filled cell (gx, gy) is the unit square [gx, gx+1] x [gy, gy+1].
    An edge is emitted wherever a neighbour is not filled, oriented so the
    filled region lies to the left; matching each edge's end to another
    edge's start walks the loops out.

    Where two filled cells touch only at a corner, that grid node has two
    edges leaving it, one belonging to each cell. The walk crosses over to
    the other cell there. Carrying on round the cell it arrived along - or
    taking either edge, as this used to - can splice the outline and a
    hole into one loop that visits the node twice: a self-touching outline
    with a slit into the hole, which snapping then turned into a
    self-crossing polyline (the men's toilet on P285, next to a column).
    Crossing over always yields simple loops, a hole at most touching the
    outline at a single point.
    """
    edges = {}
    for idx in filled:
        gy, gx = divmod(idx, nx)
        if (idx - nx) not in filled:
            edges.setdefault((gx, gy), []).append(((gx + 1, gy), idx))
        if (idx + 1) not in filled:
            edges.setdefault((gx + 1, gy), []).append(((gx + 1, gy + 1), idx))
        if (idx + nx) not in filled:
            edges.setdefault((gx + 1, gy + 1), []).append(((gx, gy + 1), idx))
        if (idx - 1) not in filled:
            edges.setdefault((gx, gy + 1), []).append(((gx, gy), idx))

    loops = []
    while edges:
        start = next(iter(edges))
        loop = [start]
        node = start
        cell = None
        while True:
            outs = edges.get(node)
            if not outs:
                break
            k = len(outs) - 1
            if cell is not None and len(outs) > 1:
                for i, (_n, c) in enumerate(outs):
                    if c != cell:
                        k = i
                        break
            nxt, cell = outs.pop(k)
            if not outs:
                del edges[node]
            if nxt == start:
                break
            loop.append(nxt)
            node = nxt
            if len(loop) > 2_000_000:
                break
        if len(loop) >= 4:
            loops.append(loop)
    return loops


def _drop_collinear(poly, eps=1e-9):
    n = len(poly)
    if n < 3:
        return list(poly)
    out = []
    for i in range(n):
        a, b, c = poly[i - 1], poly[i], poly[(i + 1) % n]
        cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(cross) > eps:
            out.append(b)
    return out if len(out) >= 3 else list(poly)


def _douglas_peucker(pts, tol):
    if len(pts) < 3:
        return list(pts)
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = pts[i]
        bx, by = pts[j]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best_d, best_k = -1.0, -1
        for k in range(i + 1, j):
            px, py = pts[k]
            if norm < 1e-12:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > best_d:
                best_d, best_k = d, k
        if best_d > tol and best_k > 0:
            keep[best_k] = True
            stack.append((i, best_k))
            stack.append((best_k, j))
    return [p for p, k in zip(pts, keep) if k]


def _shoelace(pts):
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


# -- wall snapping ----------------------------------------------------------
# An unsupported edge shorter than this is left alone: a flattened arc is
# made of many short edges with no single wall behind them, and squaring
# those up would turn a curved wall into a staircase.
CURVE_EDGE_MM = 400.0


def _snap_to_walls(poly, index, tol, angle_tol_deg=6.0):
    """Put every edge of the outline onto a wall line.

    Each edge is first fitted to the wall it came from. What is left over
    is the bridge across a doorway or a cubicle opening, which lies on no
    wall at all: those take the direction of the walls around them, so
    the boundary crosses an opening square to the wall instead of cutting
    the corner at an angle. Consecutive lines are then intersected, which
    is what puts the turn exactly where the wall turns.

    The directions are taken from the room's own walls rather than from
    the drawing axes, so a wing at 30 degrees squares up to its own walls
    and a curved run is left on the grid approximation.
    """
    n = len(poly)
    if n < 3:
        return poly

    cos_tol = math.cos(math.radians(angle_tol_deg))
    lines, edges, supported, unsupported = [], [], [], []
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        ex, ey = bx - ax, by - ay
        elen = math.hypot(ex, ey)
        if elen < 1e-9:
            lines.append(None)
            continue
        ux, uy = ex / elen, ey / elen
        mx, my = (ax + bx) / 2.0, (ay + by) / 2.0

        best = None
        for seg in index.near(mx, my):
            sx, sy, tx, ty = seg[0], seg[1], seg[2], seg[3]
            wx, wy = tx - sx, ty - sy
            wlen = math.hypot(wx, wy)
            if wlen < 1e-9:
                continue
            vx, vy = wx / wlen, wy / wlen
            if abs(ux * vx + uy * vy) < cos_tol:
                continue
            d = abs(vx * (my - sy) - vy * (mx - sx))
            if d > tol:
                continue
            proj = (mx - sx) * vx + (my - sy) * vy
            if proj < -tol or proj > wlen + tol:
                continue
            if best is None or d < best[0]:
                best = (d, (sx, sy, vx, vy))
        if best is not None:
            lines.append(best[1])
            supported.append((elen, ux, uy))
        else:
            lines.append(None)          # settled below, once the wall
            unsupported.append(i)       # directions of this room are known
        edges.append((ax, ay, ux, uy, elen, mx, my))

    # The directions this room's walls actually run in, longest first.
    pool = []
    for elen, ux, uy in sorted(supported, reverse=True):
        if all(abs(ux * px + uy * py) < 0.985 for px, py in pool):
            pool.append((ux, uy))
        if len(pool) >= 4:
            break
    if not pool:
        pool = [(1.0, 0.0), (0.0, 1.0)]
    elif len(pool) == 1:
        pool.append((-pool[0][1], pool[0][0]))

    for i in unsupported:
        ax, ay, ux, uy, elen, mx, my = edges[i]
        if elen < CURVE_EDGE_MM:
            lines[i] = (ax, ay, ux, uy)     # part of a curve; leave it
            continue
        best_dir, best_dot = (ux, uy), -1.0
        for px, py in pool:
            dot = abs(ux * px + uy * py)
            if dot > best_dot:
                best_dot, best_dir = dot, (px, py)
        # Through the middle of the edge, so the bridge sits where the
        # opening is rather than shifting the wall line it joins.
        lines[i] = (mx, my, best_dir[0], best_dir[1])

    # Two near-parallel lines meet a long way off. Such a corner is put
    # where the grid found it, but projected onto the line of the edge
    # leaving it - so the edge still runs along its wall instead of
    # keeping the staircase the grid drew. Falling back to the raw vertex
    # instead, which is what this did first, left exactly the 5-degree
    # slants the drawing has no wall for.
    out = []
    limit = 12.0 * tol
    for i in range(n):
        p = _line_intersection(lines[i - 1], lines[i])
        if p is None or math.hypot(p[0] - poly[i][0],
                                   p[1] - poly[i][1]) > limit:
            p = _project_onto(poly[i], lines[i]) or poly[i]
        out.append(p)
    return _drop_collinear(_merge_close(out, tol * 0.6))


def _checked_snap(poly, index, tol, cell):
    """_snap_to_walls, kept only when it made a sound outline.

    Snapping moves each edge at most a few cells, onto its wall, so the
    snapped outline must still be a simple polygon of about the same area.
    One that crosses itself, or whose area moved by more than the band
    snapping can account for, was pulled onto the wrong line - usually a
    short stray face beside the edge - and the grid outline it came from
    is the better answer. A boundary written to the DXF is never invalid.
    """
    snapped = _untangle(_snap_to_walls(poly, index, tol=tol))
    if len(snapped) < 3 or _self_intersects(snapped):
        return poly
    a0, a1 = _shoelace(poly), _shoelace(snapped)
    perimeter = sum(math.hypot(poly[i][0] - poly[i - 1][0],
                               poly[i][1] - poly[i - 1][1])
                    for i in range(len(poly)))
    if abs(a1 - a0) > perimeter * 2.0 * cell + 0.01 * a0:
        return poly
    return snapped


def _untangle(poly, rounds=50):
    """Cut off the small loops where an outline crosses itself.

    Snapping two short edges of a curve onto neighbouring chords can make
    them cross a few centimetres from the corner, leaving a tiny bowtie.
    Each crossing is removed by joining the two edges at the crossing point
    and dropping the vertices of the smaller side.
    """
    poly = list(poly)
    for _ in range(rounds):
        hit = _first_crossing(poly)
        if hit is None:
            return poly
        i, j = hit
        n = len(poly)
        x = _line_intersection(
            _as_line(poly[i], poly[(i + 1) % n]),
            _as_line(poly[j], poly[(j + 1) % n])) or poly[(i + 1) % n]
        inner = j - i                       # vertices i+1 .. j
        if inner <= n - inner:
            poly = poly[:i + 1] + [x] + poly[j + 1:]
        else:
            poly = poly[i + 1:j + 1] + [x]
        if len(poly) < 3:
            return poly
    return poly


def _as_line(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy)
    if L < 1e-12:
        return None
    return (a[0], a[1], dx / L, dy / L)


def _first_crossing(poly):
    """(i, j) of the first two non-adjacent edges that meet, or None."""
    n = len(poly)
    if n < 4:
        return None
    boxes = []
    for i in range(n):
        (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
        boxes.append((min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)))
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        bi = boxes[i]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue                     # adjacent through the closure
            bj = boxes[j]
            if bi[2] < bj[0] or bj[2] < bi[0] or bi[3] < bj[1] or bj[3] < bi[1]:
                continue
            if _segments_meet(a, b, poly[j], poly[(j + 1) % n]):
                return i, j
    return None


def _self_intersects(poly):
    return _first_crossing(poly) is not None


def _segments_meet(p1, p2, p3, p4, eps=1e-6):
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) <= eps else (1 if v > 0 else -1)

    def on_seg(a, b, c):
        return (min(a[0], b[0]) - eps <= c[0] <= max(a[0], b[0]) + eps and
                min(a[1], b[1]) - eps <= c[1] <= max(a[1], b[1]) + eps)

    o1, o2 = orient(p1, p2, p3), orient(p1, p2, p4)
    o3, o4 = orient(p3, p4, p1), orient(p3, p4, p2)
    if o1 != o2 and o3 != o4:
        return True
    return ((o1 == 0 and on_seg(p1, p2, p3)) or (o2 == 0 and on_seg(p1, p2, p4))
            or (o3 == 0 and on_seg(p3, p4, p1))
            or (o4 == 0 and on_seg(p3, p4, p2)))


def _project_onto(point, line):
    """The point on `line` nearest `point`. Lines are (x, y, ux, uy)."""
    if not line:
        return None
    px, py = point
    ox, oy, ux, uy = line
    t = (px - ox) * ux + (py - oy) * uy
    return (ox + ux * t, oy + uy * t)


def _merge_close(poly, tol):
    """Collapse vertices that snapping brought together.

    A sharp corner - the apex of a triangular or angled room - often
    leaves two vertices a fraction of a cell apart, which blunts the
    corner and costs a few tenths of a percent of area.
    """
    if len(poly) < 4:
        return poly
    out = [poly[0]]
    for p in poly[1:]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > tol:
            out.append(p)
    if len(out) > 3 and math.hypot(out[0][0] - out[-1][0],
                                   out[0][1] - out[-1][1]) <= tol:
        out.pop()
    return out if len(out) >= 3 else poly


def _line_intersection(l1, l2):
    if l1 is None or l2 is None:
        return None
    x1, y1, dx1, dy1 = l1
    x2, y2, dx2, dy2 = l2
    den = dx1 * dy2 - dy1 * dx2
    if abs(den) < 1e-9:
        return None
    t = ((x2 - x1) * dy2 - (y2 - y1) * dx2) / den
    return (x1 + dx1 * t, y1 + dy1 * t)


# -- convenience -------------------------------------------------------------
def trace_rooms(segments: Sequence[Segment], seeds: Sequence[Point],
                cell_mm: float = DEFAULT_CELL_MM,
                max_area_sqm: float = DEFAULT_MAX_AREA_SQM) -> List[TraceResult]:
    tracer = RoomTracer(segments, cell_mm, max_area_sqm)
    return [tracer.trace(x, y) for x, y in seeds]
