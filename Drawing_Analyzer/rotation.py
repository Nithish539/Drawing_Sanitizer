# -*- coding: utf-8 -*-
"""Rotated cores and wings (Fix 0).

Every test in the room search and in the geometry engine measures along
the drawing's axes: a tread is a line within a few degrees of horizontal
or vertical, an X is a pair of diagonals over an axis-aligned rectangle,
a wall band is found by a ray cast along x or y. A core drawn at an angle
(Centaurus 11th floor: the S3 fire tower, 30 degrees off) fails all of
them.

Rather than teach every test about angles, the drawing is turned: the
walls of the floor are read for their directions, every direction that a
good length of wall runs in and that is not the drawing's own axis is a
*rotated cluster*, and for each cluster a small drawing is made holding
only what stands in it, turned back to the axes. The ordinary passes run
on that small drawing unchanged, and their results are turned back and
put in place of what the main pass found there. A whole drawing standing
at an angle is one cluster covering the floor.

    clusters = wall_angle_clusters(segs, plate)
    mini, mmsp = rotated_subdocument(doc, msp, units, region, theta, origin)
    ... run the pass on (mini, mmsp, units) with the floor polygon
        rotate_pts(region, -theta, origin) ...
    ... turn every polygon of the result back with rotate_pts(pts, theta, origin)

Angles are in degrees, counter-clockwise. rotate_pts(pts, theta, o) turns
points by theta about o; the pass runs in a frame turned by -theta, so its
results are turned by +theta to come home.
"""
from __future__ import annotations

import collections
import math

from .config import CONFIG

_RC = CONFIG.get("rotation") or {}
ENABLED = bool(_RC.get("enabled", True))
# a direction counts when this much wall runs along it
MIN_WALL_LEN_MM = float(_RC.get("min_wall_len_mm", 12000.0))
# and it is at least this far off the drawing's axes
MIN_ANGLE_DEG = float(_RC.get("min_angle_deg", 4.0))
ANGLE_BIN_DEG = float(_RC.get("angle_bin_deg", 1.0))
ANGLE_TOL_DEG = float(_RC.get("angle_tol_deg", 2.5))
# walls of one direction closer than this are one cluster
CLUSTER_GAP_MM = float(_RC.get("cluster_gap_mm", 2500.0))
# what is read round the cluster, and what the cluster claims
REGION_PAD_MM = float(_RC.get("region_pad_mm", 1500.0))
CORE_PAD_MM = float(_RC.get("core_pad_mm", 400.0))
# a cluster covering this share of the plate is the whole floor
WHOLE_FLOOR_SHARE = float(_RC.get("whole_floor_share", 0.6))
MIN_SEG_MM = float(_RC.get("min_seg_mm", 400.0))
# the walls across the cluster's direction: at least this share / length
CROSS_MIN_SHARE = float(_RC.get("cross_min_share", 0.3))
CROSS_MIN_MM = float(_RC.get("cross_min_mm", 8000.0))
# inside the cluster, its own walls against the axis-aligned ones
AXIS_RATIO_MIN = float(_RC.get("axis_ratio_min", 1.5))
_DEBUG = False


def rotate_pt(x, y, theta_deg, origin):
    ox, oy = origin
    c, s = math.cos(math.radians(theta_deg)), math.sin(math.radians(theta_deg))
    dx, dy = x - ox, y - oy
    return (ox + c * dx - s * dy, oy + s * dx + c * dy)


def rotate_pts(pts, theta_deg, origin):
    return [rotate_pt(float(p[0]), float(p[1]), theta_deg, origin) for p in pts]


def rotate_poly(g, theta_deg, origin):
    """A shapely polygon turned about origin."""
    from shapely import affinity
    return affinity.rotate(g, theta_deg, origin=origin)


def _angle_mod(seg):
    dx, dy = seg[2] - seg[0], seg[3] - seg[1]
    return math.degrees(math.atan2(dy, dx)) % 90.0


def _is_wall_seg(s):
    from . import walltrace as _wt
    try:
        return _wt.is_wall_layer(s[4]) or _wt.is_structure_layer(s[4])
    except Exception:                                 # noqa: BLE001
        return False


def wall_directions(segs):
    """{angle (0 <= a < 90, degrees): total wall length} of the floor's
    wall-layer segments, binned."""
    hist = collections.Counter()
    for s in segs:
        if len(s) < 6 or not _is_wall_seg(s):
            continue
        L = math.hypot(s[2] - s[0], s[3] - s[1])
        if L < MIN_SEG_MM:
            continue
        a = _angle_mod(s)
        b = round(a / ANGLE_BIN_DEG) * ANGLE_BIN_DEG % 90.0
        hist[b] += L
    return hist


def _off_axis(a):
    """How far an angle (mod 90) is from the drawing's axes."""
    return min(a, 90.0 - a)


def wall_angle_clusters(segs, plate):
    """The rotated cores and wings of a floor.

    Returns [{"theta": degrees to turn the cluster back to the axes (the
    cluster's own direction, -90 < theta <= 90 as drawn), "region": [pts]
    (what is read: the cluster padded), "core": [pts] (what the cluster
    claims), "whole": bool, "wall_len_mm": float}]."""
    if not ENABLED:
        return []
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import unary_union
    try:
        plate_pg = Polygon([tuple(p[:2]) for p in plate]).buffer(0)
    except Exception:                                 # noqa: BLE001
        plate_pg = None
    hist = wall_directions(segs)
    if not hist:
        return []
    # directions: peaks of the histogram, off the axes
    out = []
    used = set()
    for a, L in sorted(hist.items(), key=lambda kv: -kv[1]):
        if _off_axis(a) < MIN_ANGLE_DEG or a in used:
            continue
        # the peak and its neighbours
        tot = sum(v for b, v in hist.items() if _ang_dist(b, a) <= ANGLE_TOL_DEG)
        if tot < MIN_WALL_LEN_MM:
            continue
        for b in list(hist):
            if _ang_dist(b, a) <= ANGLE_TOL_DEG:
                used.add(b)
        # most of the floor's walls run this way: the whole drawing is
        # turned (one pass over the plate, not one per group of walls)
        all_len = sum(hist.values())
        if plate_pg is not None and not plate_pg.is_empty and all_len > 0 \
                and tot >= WHOLE_FLOOR_SHARE * all_len:
            theta = a if a <= 45.0 else a - 90.0
            pl = [(float(x), float(y)) for x, y in list(plate_pg.exterior.coords)[:-1]]
            return [{"theta": float(theta), "region": pl, "core": pl, "whole": True, "wall_len_mm": float(tot)}]
        # the segments of this direction, clustered by distance
        lines = []
        for s in segs:
            if len(s) < 6 or not _is_wall_seg(s):
                continue
            L1 = math.hypot(s[2] - s[0], s[3] - s[1])
            if L1 < MIN_SEG_MM or _ang_dist(_angle_mod(s), a) > ANGLE_TOL_DEG:
                continue
            lines.append(LineString([(s[0], s[1]), (s[2], s[3])]))
        if not lines:
            continue
        blobs = unary_union([ln.buffer(CLUSTER_GAP_MM / 2.0) for ln in lines])
        for blob in getattr(blobs, "geoms", [blobs]):
            members = [ln for ln in lines if ln.intersects(blob)]
            wl = sum(ln.length for ln in members)
            if wl < MIN_WALL_LEN_MM:
                continue
            # a core has walls both ways (a and a + 90); a curved facade
            # flattened to chords has a run at one angle only
            along = sum(ln.length for ln in members if _ang_dist180(_angle180(ln), a) <= ANGLE_TOL_DEG)
            across = wl - along
            if _DEBUG:
                print("blob", a, "wl", round(wl), "along", round(along), "across", round(across))
            if min(along, across) < max(CROSS_MIN_SHARE * wl, CROSS_MIN_MM):
                continue
            if _DEBUG:
                print("cluster", a, "along", round(along), "across", round(across))
            core = unary_union(members).convex_hull.buffer(CORE_PAD_MM, join_style=2)
            # the cluster's walls must own the place: more wall runs the
            # cluster's way inside it than along the drawing's axes (a
            # slanted facade with square rooms against it is no core)
            axis_len = 0.0
            for s in segs:
                if len(s) < 6 or not _is_wall_seg(s):
                    continue
                L1 = math.hypot(s[2] - s[0], s[3] - s[1])
                if L1 < MIN_SEG_MM or _off_axis(_angle_mod(s)) > ANGLE_TOL_DEG:
                    continue
                # only the part of the wall that lies inside the core counts;
                # a long wall passing through it is not the core's
                try:
                    axis_len += core.intersection(LineString([(s[0], s[1]), (s[2], s[3])])).length
                except Exception:
                    if core.contains(Point((s[0] + s[2]) / 2.0, (s[1] + s[3]) / 2.0)):
                        axis_len += L1
            if _DEBUG:
                print("   wl", round(wl), "axis", round(axis_len))
            if wl < AXIS_RATIO_MIN * axis_len:
                continue
            region = core.buffer(REGION_PAD_MM, join_style=2)
            whole = False
            if plate_pg is not None and not plate_pg.is_empty:
                region = region.intersection(plate_pg)
                if region.is_empty:
                    continue
                if core.intersection(plate_pg).area >= WHOLE_FLOOR_SHARE * plate_pg.area:
                    whole = True
                    core = plate_pg
                    region = plate_pg
            if region.geom_type != "Polygon":
                region = max(getattr(region, "geoms", [region]), key=lambda g: g.area)
            if core.geom_type != "Polygon":
                core = max(getattr(core, "geoms", [core]), key=lambda g: g.area)
            theta = a if a <= 45.0 else a - 90.0
            out.append({"theta": float(theta),
                        "region": [(float(x), float(y)) for x, y in list(region.exterior.coords)[:-1]],
                        "core": [(float(x), float(y)) for x, y in list(core.exterior.coords)[:-1]],
                        "whole": whole, "wall_len_mm": float(wl)})
    # a whole-floor cluster makes the others pointless
    wholes = [c for c in out if c["whole"]]
    if wholes:
        return [max(wholes, key=lambda c: c["wall_len_mm"])]
    return out


def _angle180(ln):
    (x0, y0), (x1, y1) = list(ln.coords)[0], list(ln.coords)[-1]
    return math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180.0


def _ang_dist180(a, b):
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def _ang_dist(a, b):
    d = abs(a - b) % 90.0
    return min(d, 90.0 - d)


def _entity_bbox(e):
    """A quick (minx, miny, maxx, maxy) in drawing units, or None."""
    t = e.dxftype()
    try:
        if t == "LINE":
            a, b = e.dxf.start, e.dxf.end
            return (min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y))
        if t == "LWPOLYLINE":
            pts = list(e.get_points("xy"))
            if not pts:
                return None
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            return (min(xs), min(ys), max(xs), max(ys))
        if t == "POLYLINE":
            pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
            if not pts:
                return None
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            return (min(xs), min(ys), max(xs), max(ys))
        if t in ("CIRCLE", "ARC"):
            c, r = e.dxf.center, e.dxf.radius
            return (c.x - r, c.y - r, c.x + r, c.y + r)
        if t in ("TEXT", "MTEXT", "ATTDEF"):
            p = e.dxf.insert
            return (p.x, p.y, p.x, p.y)
        if t == "POINT":
            p = e.dxf.location
            return (p.x, p.y, p.x, p.y)
    except Exception:                                 # noqa: BLE001
        return None
    try:
        from ezdxf import bbox as _bb
        ext = _bb.extents([e], fast=True)
        if not ext.has_data:
            return None
        return (ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y)
    except Exception:                                 # noqa: BLE001
        return None


_FOREIGN_OK = ("LINE", "CIRCLE", "ARC", "TEXT", "MTEXT", "LWPOLYLINE", "POLYLINE", "ELLIPSE", "SPLINE",
               "POINT", "HATCH", "SOLID", "3DFACE", "TRACE")
MAX_BLOCK_DEPTH = int(_RC.get("block_depth", 3))


def _explode(e, depth, out, host_layer=None):
    """The entity, or what its block holds in place (INSERTs opened out,
    to MAX_BLOCK_DEPTH), appended to out. An entity on layer 0 inside a
    block shows on the INSERT's layer (dxfio.effective_layer): it is
    moved there, so the walls of a block stay walls."""
    t = e.dxftype()
    if t == "INSERT" or (t not in _FOREIGN_OK and hasattr(e, "virtual_entities")):
        # a block reference, a dimension, a leader, a table ...: what it
        # draws, in place
        if depth >= MAX_BLOCK_DEPTH:
            return
        try:
            kids = list(e.virtual_entities())
        except Exception:                             # noqa: BLE001
            return
        lay = (e.dxf.get("layer", "0") or "0")
        if lay == "0" and host_layer:
            lay = host_layer
        for k in kids:
            _explode(k, depth + 1, out, lay)
        return
    if t in _FOREIGN_OK:
        try:
            if host_layer and (e.dxf.get("layer", "0") or "0") == "0":
                e.dxf.layer = host_layer
        except Exception:                             # noqa: BLE001
            pass
        out.append(e)


def rotated_subdocument(doc, msp, units, region_mm, theta_deg, origin_mm):
    """A copy of the drawing turned by -theta about origin so the cluster
    lies along the axes. Returns (doc, modelspace). The copy is the
    drawing written out and read back (blocks, layers, dimensions and all),
    so the passes read it exactly as they read the original - blocks stay
    blocks, nothing is opened out; each modelspace entity is turned in
    place, and what lies outside the region's box is dropped. The source
    drawing is never touched."""
    import io
    import ezdxf
    from ezdxf.math import Matrix44
    from shapely.geometry import Polygon
    k = float(getattr(units, "mm_per_unit", 1.0) or 1.0)
    reg = Polygon(region_mm).buffer(0)
    minx, miny, maxx, maxy = [v / k for v in reg.bounds]
    buf = io.StringIO()
    doc.write(buf)
    buf.seek(0)
    target = ezdxf.read(buf)
    tmsp = target.modelspace()
    ox, oy = origin_mm[0] / k, origin_mm[1] / k
    m = Matrix44.chain(Matrix44.translate(-ox, -oy, 0.0),
                       Matrix44.z_rotate(math.radians(-theta_deg)),
                       Matrix44.translate(ox, oy, 0.0))

    def inside(b):
        return not (b[2] < minx or b[0] > maxx or b[3] < miny or b[1] > maxy)
    for e in list(tmsp):
        try:
            if e.dxftype() in _FOREIGN_OK:
                b = _entity_bbox(e)
                if b is not None and not inside(b):
                    tmsp.delete_entity(e)
                    continue
            e.transform(m)
            _snap(e)
        except Exception:                             # noqa: BLE001
            try:
                tmsp.delete_entity(e)
            except Exception:                         # noqa: BLE001
                pass
    return target, tmsp


SNAP_DECIMALS = int(_RC.get("snap_decimals", 4))


def _snap(e):
    """Coordinates rounded (SNAP_DECIMALS places) after the turn: a wall
    turned to the axes is then exactly on them, as a drawn one is, so
    the passes' coordinate matching sees the same drawing."""
    from ezdxf.math import Vec3
    d = SNAP_DECIMALS

    def r3(v):
        return Vec3(round(v.x, d), round(v.y, d), round(v.z, d))
    t = e.dxftype()
    try:
        if t == "LINE":
            e.dxf.start = r3(e.dxf.start)
            e.dxf.end = r3(e.dxf.end)
        elif t == "LWPOLYLINE":
            pts = [(round(p[0], d), round(p[1], d)) + tuple(p[2:]) for p in e.get_points()]
            e.set_points(pts)
        elif t == "POLYLINE":
            for v in e.vertices:
                v.dxf.location = r3(v.dxf.location)
        elif t in ("CIRCLE", "ARC"):
            e.dxf.center = r3(e.dxf.center)
            e.dxf.radius = round(e.dxf.radius, d)
            if t == "ARC":
                e.dxf.start_angle = round(e.dxf.start_angle, 6)
                e.dxf.end_angle = round(e.dxf.end_angle, 6)
        elif t in ("TEXT", "MTEXT"):
            e.dxf.insert = r3(e.dxf.insert)
        elif t == "INSERT":
            e.dxf.insert = r3(e.dxf.insert)
            e.dxf.rotation = round(e.dxf.rotation, 6)
    except Exception:                                 # noqa: BLE001
        pass


def rotate_rows(rows, theta_deg, origin):
    """Step 1 texts turned with the cluster (their positions only)."""
    out = []
    for r in rows:
        r2 = dict(r)
        try:
            x, y = rotate_pt(float(r["x"]), float(r["y"]), theta_deg, origin)
            r2["x"], r2["y"] = x, y
        except (KeyError, TypeError, ValueError):
            pass
        for key in ("bbox",):
            b = r.get(key)
            if b and len(b) == 4:
                pts = rotate_pts([(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])], theta_deg, origin)
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                r2[key] = (min(xs), min(ys), max(xs), max(ys))
        out.append(r2)
    return out


def centroid_of(pts):
    from shapely.geometry import Polygon
    try:
        g = Polygon(pts)
        if g.is_valid and g.area > 0:
            c = g.centroid
            return (c.x, c.y)
    except Exception:                                 # noqa: BLE001
        pass
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (sum(xs) / len(xs), sum(ys) / len(ys))
