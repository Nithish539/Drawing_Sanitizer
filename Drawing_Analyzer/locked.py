"""What a floor pass has already decided, and no later pass derives again.

The element pipeline (rule element_pipeline, 2026-10-17) identifies each
kind of element once, in a fixed order - the walls first, then the X marks
(shaft, lift, window, cut-out, column, void), the doors, the staircases,
the rooms - and locks it. A LockedMask holds everything locked so far, by
kind, as polygons. Every wall computation that runs after a lock reads
the mask and skips what it holds:

  * a drawn line lying inside a locked region is no candidate for a wall
    face any more (filter_segments);
  * a wall strip mostly inside a locked region is dropped, and the wall
    mass is cut back round the locked cells (subtract) - a shaft's slab
    opening, a lift well, a void, a door opening, a column is never wall;
  * a locked outline is a fixed obstacle to a later trace (edges).

A piece reclaimed from the walls this way can never come back: the mask
only grows, and nothing after a lock pairs faces over it again.
"""
from __future__ import annotations

import math


class LockedMask:
    """Locked elements by kind: {kind: [shapely Polygon]}."""

    def __init__(self):
        self.items = {}
        self._union = {}

    # -- building it ---------------------------------------------------
    def lock(self, kind, shapes, min_area=1.0):
        """Add polygons (shapely, or point lists) under `kind`. Returns how
        many were taken."""
        from shapely.geometry import Polygon
        n = 0
        for s in shapes or ():
            try:
                g = s if hasattr(s, "geom_type") else Polygon([tuple(p[:2]) for p in s])
                g = g.buffer(0)
            except (ValueError, TypeError, IndexError):
                continue
            if g is None or g.is_empty or g.area < min_area:
                continue
            for q in getattr(g, "geoms", [g]):
                if q.geom_type == "Polygon" and q.area >= min_area:
                    self.items.setdefault(kind, []).append(q)
                    n += 1
        self._union.clear()
        return n

    def kinds(self):
        return sorted(self.items)

    def count(self, kinds=None):
        return sum(len(v) for k, v in self.items.items() if kinds is None or k in kinds)

    def union(self, kinds=None):
        """One geometry of every locked polygon of these kinds (all kinds
        when None), or None when nothing is locked."""
        from shapely.ops import unary_union
        key = tuple(sorted(kinds)) if kinds is not None else None
        if key in self._union:
            return self._union[key]
        polys = [p for k, v in self.items.items() if kinds is None or k in kinds for p in v]
        u = unary_union(polys).buffer(0) if polys else None
        if u is not None and u.is_empty:
            u = None
        self._union[key] = u
        return u

    # -- reading it from a later pass ----------------------------------
    def filter_segments(self, segs, kinds=None, inside_mm=2.0, counts=None):
        """The segments (sx, sy, ex, ey, ...) not lying inside a locked
        region: a line wholly inside one (both ends and its middle, with
        `inside_mm` of play) is dropped. A line along the locked outline
        is kept: it is the outline's own edge, the wall face beside it."""
        u = self.union(kinds)
        if u is None:
            return list(segs or ())
        from shapely.geometry import Point
        from shapely.strtree import STRtree
        polys = [p for k, v in self.items.items() if kinds is None or k in kinds for p in v]
        tree = STRtree(polys)
        kept, dropped = [], 0
        for sg in segs or ():
            mx, my = (sg[0] + sg[2]) / 2.0, (sg[1] + sg[3]) / 2.0
            gone = False
            for k in tree.query(Point(mx, my)):
                p = polys[int(k)]
                inner = p.buffer(-inside_mm) if inside_mm > 0 else p
                if inner.is_empty:
                    continue
                if inner.contains(Point(mx, my)) and inner.contains(Point(sg[0], sg[1])) \
                        and inner.contains(Point(sg[2], sg[3])):
                    gone = True
                    break
            if gone:
                dropped += 1
            else:
                kept.append(sg)
        if counts is not None:
            counts["segments_in_locked"] = counts.get("segments_in_locked", 0) + dropped
        return kept

    def subtract(self, geom, kinds=None, grow_mm=0.0):
        """`geom` less every locked polygon of these kinds."""
        u = self.union(kinds)
        if u is None or geom is None or geom.is_empty:
            return geom
        if grow_mm > 0:
            u = u.buffer(grow_mm, join_style=2)
        try:
            return geom.difference(u)
        except Exception:                             # noqa: BLE001
            return geom.buffer(0).difference(u.buffer(0))

    def covers(self, geom, kinds=None, share=0.5):
        """True when at least `share` of `geom` lies in a locked region."""
        u = self.union(kinds)
        if u is None or geom is None or geom.is_empty or geom.area <= 0:
            return False
        return geom.intersection(u).area >= share * geom.area

    def edges(self, kinds=None):
        """The outlines of the locked polygons as segments (x0, y0, x1, y1):
        fixed obstacles to a trace."""
        out = []
        for k, v in self.items.items():
            if kinds is not None and k not in kinds:
                continue
            for p in v:
                cs = list(p.exterior.coords)
                for a, b in zip(cs, cs[1:]):
                    if math.hypot(b[0] - a[0], b[1] - a[1]) > 0.5:
                        out.append((float(a[0]), float(a[1]), float(b[0]), float(b[1])))
        return out

    def report(self):
        """{kind: {"count", "area_sqm"}} for the run report."""
        return {k: {"count": len(v), "area_sqm": round(sum(p.area for p in v) / 1e6, 3)}
                for k, v in sorted(self.items.items())}
