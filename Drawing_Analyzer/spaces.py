"""The rooms of a floor as its enclosed spaces, found with no text.

The element pipeline (rule rooms_from_spaces, 2026-10-17): once the walls,
the doors and the X marks are decided, the free floor is cut into its
connected pieces on a grid, and every piece that is not the open floor,
not a wall cavity and not a doorway strip is one enclosed space - traced
once, at its widest point, by the pass's own room tracer. The text is
read last: a space is named by the texts standing inside it, never by
one outside (dxf_reader, the stage before Phase B). What this module
holds is the geometry of it:

  free_pieces        the connected pieces of free floor on the grid
  trace_pieces       one traced outline per piece, duplicates dropped
  lift_bank_lobbies  an open lift lobby closed at the lift banks' ends
  is_pocket          a space that is no room: no text, no door, tiny or
                     a bay along the plate's edge
  treads_in          the treads of a flight inside a space
  shares_side        a space along a shaft's X mark
  touches_door       a space with a door of its own
"""
from __future__ import annotations

import math


def _polys(geoms):
    out = []
    for g in geoms or ():
        if g is None or getattr(g, "is_empty", True):
            continue
        for q in getattr(g, "geoms", [g]):
            if q.geom_type == "Polygon" and not q.is_empty:
                out.append(q)
    return out


def free_pieces(wall_lines, plate_pts, blocked, cell_mm=50.0, open_floor_sqm=1000.0,
                min_half_width_mm=140.0, min_sqm=0.3, open_floor_share=0.5):
    """The connected pieces of free floor.

    wall_lines: (x0, y0, x1, y1) segments - the walls the rooms are traced
    against, the door closures, the plate's edge, the decided outlines;
    plate_pts: the plate polygon; blocked: polygons that are no space (the
    X cells, the columns, the loose cut-outs). Every line is drawn into a
    grid of cell_mm cells and sealed by one cell; the plate outside and
    the blocked polygons are occupied; the pieces are labelled.

    Returns ({"seeds": [(x, y, grid_sqm)], "open_floor_sqm", "cavities",
    "tiny", "pieces"}): one seed per piece worth tracing - at the piece's
    widest point - and the counts of what was left out: a piece as big as
    the open floor (open_floor_sqm, or open_floor_share of the free floor
    - a small plate's open floor is under 1000 sq.m), a piece narrower
    than 2 x min_half_width_mm (a wall cavity, a doorway strip), a piece
    under min_sqm."""
    import numpy as np
    from scipy import ndimage
    from matplotlib.path import Path as MPath
    from shapely.geometry import Polygon

    plate = Polygon(plate_pts).buffer(0)
    if plate.is_empty:
        return {"seeds": [], "open_floor_sqm": 0.0, "cavities": 0, "tiny": 0, "pieces": 0}
    b = plate.bounds
    gx0, gy0 = b[0] - 2 * cell_mm, b[1] - 2 * cell_mm
    nx = int((b[2] - gx0) / cell_mm) + 3
    ny = int((b[3] - gy0) / cell_mm) + 3
    if nx * ny > 40_000_000:
        return {"seeds": [], "open_floor_sqm": 0.0, "cavities": 0, "tiny": 0, "pieces": 0}
    occ = np.zeros((ny, nx), dtype=bool)
    for s in wall_lines or ():
        L = math.hypot(s[2] - s[0], s[3] - s[1])
        n = max(2, int(L / (cell_mm * 0.4)) + 1)
        xs = np.linspace(s[0], s[2], n)
        ys = np.linspace(s[1], s[3], n)
        ix = ((xs - gx0) / cell_mm).astype(int)
        iy = ((ys - gy0) / cell_mm).astype(int)
        ok = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
        occ[iy[ok], ix[ok]] = True
    occ = ndimage.binary_dilation(occ, iterations=1)
    cx = gx0 + (np.arange(nx) + 0.5) * cell_mm
    cy = gy0 + (np.arange(ny) + 0.5) * cell_mm
    XX, YY = np.meshgrid(cx, cy)
    inside = MPath(list(plate.exterior.coords)).contains_points(
        np.column_stack([XX.ravel(), YY.ravel()])).reshape(ny, nx)
    free = (~occ) & inside
    for g in _polys(blocked):
        bb = g.bounds
        i0, i1 = max(0, int((bb[0] - gx0) / cell_mm)), min(nx, int((bb[2] - gx0) / cell_mm) + 2)
        j0, j1 = max(0, int((bb[1] - gy0) / cell_mm)), min(ny, int((bb[3] - gy0) / cell_mm) + 2)
        if i1 <= i0 or j1 <= j0:
            continue
        sub = np.column_stack([XX[j0:j1, i0:i1].ravel(), YY[j0:j1, i0:i1].ravel()])
        m = MPath(list(g.exterior.coords)).contains_points(sub).reshape(j1 - j0, i1 - i0)
        free[j0:j1, i0:i1] &= ~m
    lab, ncomp = ndimage.label(free)
    if ncomp == 0:
        return {"seeds": [], "open_floor_sqm": 0.0, "cavities": 0, "tiny": 0, "pieces": 0}
    idx = np.arange(1, ncomp + 1)
    areas = ndimage.sum(free, lab, idx) * cell_mm * cell_mm / 1e6
    dist = ndimage.distance_transform_edt(free) * cell_mm
    maxr = ndimage.maximum(dist, lab, idx)
    free_sqm = float(free.sum()) * cell_mm * cell_mm / 1e6
    open_at = open_floor_sqm
    if open_floor_share and open_floor_share > 0:
        open_at = min(open_floor_sqm, open_floor_share * free_sqm)
    seeds, open_sqm, n_cav, n_tiny = [], 0.0, 0, 0
    for k in range(1, ncomp + 1):
        a, r = float(areas[k - 1]), float(maxr[k - 1])
        if a >= open_at:
            open_sqm += a
            continue
        if r < min_half_width_mm:
            n_cav += 1
            continue
        if a < min_sqm:
            n_tiny += 1
            continue
        ys_k, xs_k = np.where(lab == k)
        j = int(np.argmax(dist[ys_k, xs_k]))
        seeds.append((float(cx[xs_k[j]]), float(cy[ys_k[j]]), a))
    return {"seeds": seeds, "open_floor_sqm": open_sqm, "cavities": n_cav, "tiny": n_tiny, "pieces": int(ncomp)}


def trace_pieces(seeds, tracers, min_sqm=0.3, min_half_width_mm=140.0, taken=None):
    """One outline per piece. tracers: [(trace(x, y) -> polygon points or
    None, how)] tried in turn - the walls and doors, then the doorless
    openings closed, then the plain lines. A trace that leaks on every
    tracer gives nothing (the piece is part of something bigger). A
    duplicate (half of it on an outline already traced, or on one of
    taken) is dropped; so is an outline narrower than 2 x
    min_half_width_mm or under min_sqm.
    Returns [{"g": Polygon, "how", "seed", "grid_sqm"}]."""
    from shapely.geometry import Polygon
    out = []
    prior = [{"g": g} for g in _polys(taken)]
    for sx, sy, grid_sqm in seeds or ():
        g, how = None, ""
        for trace, how_ in tracers:
            pts = trace(sx, sy)
            if pts and len(pts) >= 3:
                try:
                    g = Polygon([tuple(p[:2]) for p in pts]).buffer(0)
                except (ValueError, TypeError):
                    g = None
                if g is not None and not g.is_empty:
                    if g.geom_type != "Polygon":
                        g = max(_polys([g]), key=lambda q: q.area, default=None)
                    if g is not None:
                        how = how_
                        break
        if g is None or g.is_empty:
            continue
        if g.area < min_sqm * 1e6 or g.buffer(-min_half_width_mm).is_empty:
            continue
        if any(g.intersection(s["g"]).area > 0.5 * min(g.area, s["g"].area) for s in prior + out):
            continue
        out.append({"g": g, "how": how, "seed": (sx, sy), "grid_sqm": grid_sqm})
    return out


def lift_bank_lobbies(lifts, walls, spaces, plate_pts, gap_mm=(2000.0, 8000.0), min_sqm=10.0, join_mm=300.0):
    """An open lift lobby closed at the lift banks' ends (2026-10-17 (4)).

    Lifts standing within join_mm of each other are one bank. Two banks
    facing each other across a gap of gap_mm, their extents overlapping
    by half, hold a lobby between them: the floor between the banks' faces
    over their common extent, less the walls, the lifts and the spaces
    already traced. Returns [(lobby polygon, [the two lines closing it at
    the banks' ends])] (>= min_sqm, at least 800 mm wide)."""
    from shapely.geometry import Polygon, box
    from shapely.ops import unary_union
    lifts = _polys(lifts)
    if len(lifts) < 2:
        return []
    plate = Polygon(plate_pts).buffer(0)
    bank_u = unary_union([g.buffer(join_mm, join_style=2) for g in lifts])
    banks = [q.buffer(-join_mm, join_style=2) for q in getattr(bank_u, "geoms", [bank_u])]
    banks = [q for q in banks if not q.is_empty]
    walls_u = unary_union(_polys(walls)) if walls else None
    spaces_u = unary_union(_polys(spaces)) if spaces else None
    lifts_u = unary_union(lifts)
    out = []
    for i in range(len(banks)):
        for j in range(i + 1, len(banks)):
            A, B = banks[i].bounds, banks[j].bounds
            cand, ends = None, []
            ox = min(A[2], B[2]) - max(A[0], B[0])
            oy = min(A[3], B[3]) - max(A[1], B[1])
            if ox > 0.5 * min(A[2] - A[0], B[2] - B[0]):
                lo, hi = (A, B) if A[1] < B[1] else (B, A)
                gap = hi[1] - lo[3]
                if gap_mm[0] <= gap <= gap_mm[1]:
                    x0, x1 = max(A[0], B[0]), min(A[2], B[2])
                    cand = box(x0, lo[3], x1, hi[1])
                    ends = [(x0, lo[3], x0, hi[1]), (x1, lo[3], x1, hi[1])]
            elif oy > 0.5 * min(A[3] - A[1], B[3] - B[1]):
                lo, hi = (A, B) if A[0] < B[0] else (B, A)
                gap = hi[0] - lo[2]
                if gap_mm[0] <= gap <= gap_mm[1]:
                    y0, y1 = max(A[1], B[1]), min(A[3], B[3])
                    cand = box(lo[2], y0, hi[0], y1)
                    ends = [(lo[2], y0, hi[0], y0), (lo[2], y1, hi[0], y1)]
            if cand is None:
                continue
            g = cand.intersection(plate)
            if walls_u is not None:
                g = g.difference(walls_u)
            if spaces_u is not None:
                g = g.difference(spaces_u)
            g = g.difference(lifts_u).buffer(0)
            for q in _polys([g]):
                if q.area >= min_sqm * 1e6 and not q.buffer(-400.0).is_empty:
                    out.append((q, [tuple(float(v) for v in e) for e in ends]))
    return out


def is_pocket(g, plate_pts, has_text, has_door, pocket_max_sqm=1.5, edge_pocket_max_sqm=6.0, edge_mm=50.0):
    """A space that is no room: nothing written in it, no door of its own,
    and either tiny (a door recess, the strip between two lift doors) or a
    bay along the plate's edge (between the facade mullions)."""
    if has_text or has_door:
        return False
    a = g.area / 1e6
    if a < pocket_max_sqm:
        return True
    if a < edge_pocket_max_sqm:
        from shapely.geometry import Polygon
        plate = Polygon(plate_pts).buffer(0)
        if not plate.is_empty and g.exterior.distance(plate.exterior) <= edge_mm:
            return True
    return False


def treads_in(g, tread_runs, segs_arr=None, stair_treads=None):
    """The treads of the flights standing in a space: the runs the pass's
    tread finder gives (dxf_reader._tread_runs(..., with_runs=True)) whose
    centre is inside, else the count inside the space's box
    (dxf_reader._stair_treads; a flight that overhangs the outline counts
    only there)."""
    from shapely.geometry import Point
    n = 0
    for run in tread_runs or ():
        x, y, k = run[0], run[1], run[2]
        if g.contains(Point(x, y)):
            n += int(k)
    if n == 0 and stair_treads is not None and segs_arr is not None:
        b = g.bounds
        try:
            n = int(stair_treads(segs_arr, (b[0], b[1], b[2], b[3]), list(g.exterior.coords)[:-1]))
        except Exception:                             # noqa: BLE001
            n = 0
    return n


def shares_side(g, shafts, side_mm=250.0, gap_mm=30.0):
    """The shaft X marks the space stands along: side_mm or more of a
    shaft's outline within gap_mm of the space. Returns those shafts."""
    out = []
    for xg in _polys(shafts):
        if xg.distance(g) <= gap_mm and xg.exterior.intersection(g.buffer(gap_mm)).length >= side_mm:
            out.append(xg)
    return out


def touches_door(g, doors, touch_mm=300.0, gap_mm=30.0, widths=None):
    """A door opening along the space's outline: touch_mm of it, or half
    the door's width. doors: polygons; widths: the doors' widths (mm)."""
    for k, q in enumerate(_polys(doors)):
        if q.distance(g) > gap_mm:
            continue
        w = (widths[k] if widths and k < len(widths) and widths[k] else 900.0)
        if g.exterior.intersection(q.buffer(gap_mm)).length >= min(touch_mm, 0.5 * w):
            return True
    return False
