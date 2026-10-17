# -*- coding: utf-8 -*-
"""The wall hatch fills in where the faces do not pair (P-14 6th floor).

  * the 500 x 300 foot of an L-shaped concrete column - its faces 500 mm
    apart, too far to pair - is wall where the concrete hatch fills it;
  * a block wall whose face a basin niche breaks is carried on behind the
    niche, its face straight, where the hatch fills the gap;
  * a hatched piece wider than a wall, or standing apart from the walls,
    is not filled in;
  * a strip the hatch fills is never absorbed by a longer strip (the 50 mm
    of a column's foot past a junction);
  * the room tracer's door closures do not cut the walls: two of them along
    both faces of a wall paired into a "door" over the wall.

Run:  python -m unittest discover -s tests -v
"""

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from shapely.geometry import Polygon, box  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import geoengine as ge  # noqa: E402
from Drawing_Analyzer import layernames as LN  # noqa: E402
from Drawing_Analyzer import walltrace as wt  # noqa: E402


def seg(x1, y1, x2, y2, layer="A3-WL_EXT"):
    return (x1, y1, x2, y2, layer, math.hypot(x2 - x1, y2 - y1), -1, False, False)


def outline(pts, layer="A3-WL_EXT"):
    return [seg(*pts[i], *pts[(i + 1) % len(pts)], layer=layer) for i in range(len(pts))]


def hatch(pts, pattern="AR-CONC"):
    return (list(pts), [], "A3-HATCH", pattern, False)


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


PLATE = rect(-5000, -5000, 15000, 15000)


def traced(res):
    return unary_union([Polygon(s["pts"]).buffer(0) for s in res["strips"]])


class ColumnFoot(unittest.TestCase):
    """An L: 400 x 1050 up, a 500 x 300 foot - outline and hatch alike."""

    L_SHAPE = [(0, 0), (500, 0), (500, 300), (400, 300), (400, 1350), (0, 1350)]

    def run_trace(self, **cfg):
        segs = outline(self.L_SHAPE) + outline(rect(-3000, 1350, 0, 1550))          # a wall coming in
        hatches = [hatch(self.L_SHAPE), hatch(rect(-3000, 1350, 0, 1550), "ANSI37")]
        return wt.trace_floor(segs, hatches, [], PLATE)

    def test_the_foot_is_wall(self):
        res = self.run_trace()
        T = traced(res)
        self.assertGreater(T.intersection(box(0, 0, 500, 300)).area, 0.99 * 500 * 300)
        self.assertAlmostEqual(res["hatch_fill_sqm"], 0.15, delta=0.03)
        self.assertGreater(res["verdicts"].get("hatch", 0), 0)

    def test_rule_off(self):
        old = wt.HATCH_FILL
        wt.HATCH_FILL = False
        try:
            res = self.run_trace()
        finally:
            wt.HATCH_FILL = old
        self.assertLess(traced(res).intersection(box(400, 0, 500, 300)).area, 0.1 * 100 * 300)


class WallBehindANiche(unittest.TestCase):
    """A 200 mm block wall; a basin niche breaks its top face for 468 mm and
    the hatch follows the niche's outline."""

    def test_the_wall_runs_on_with_its_face(self):
        segs = [seg(0, 0, 6000, 0), seg(0, 200, 2669, 200), seg(3137, 200, 6000, 200),
                seg(0, 0, 0, 200), seg(6000, 0, 6000, 200),
                seg(2669, 123, 3137, 123, "wall tile"), seg(2669, 123, 2669, 254, "wall tile"),
                seg(3137, 123, 3137, 254, "wall tile")]
        h = [(0, 0), (6000, 0), (6000, 200), (3137, 200), (3137, 123), (2669, 123), (2669, 200), (0, 200)]
        res = wt.trace_floor(segs, [hatch(h, "ANSI37")], [], PLATE)
        T = traced(res)
        gap = box(2669, 0, 3137, 200)
        self.assertGreater(T.intersection(gap).area, 0.99 * gap.area)        # face straight across


class NotFilled(unittest.TestCase):

    def test_a_hatched_block_wider_than_a_wall(self):
        segs = outline(rect(0, 0, 3000, 200))
        hatches = [hatch(rect(0, 0, 3000, 200), "ANSI37"), hatch(rect(3000, -300, 3700, 400), "ANSI37")]
        res = wt.trace_floor(segs, hatches, [], PLATE)
        self.assertLess(traced(res).intersection(box(3000, -300, 3700, 400)).area, 1.0)

    def test_a_hatched_piece_apart_from_the_walls(self):
        segs = outline(rect(0, 0, 3000, 200))
        hatches = [hatch(rect(0, 0, 3000, 200), "ANSI37"), hatch(rect(5000, 0, 5200, 200), "ANSI37")]
        res = wt.trace_floor(segs, hatches, [], PLATE)
        self.assertLess(traced(res).intersection(box(5000, 0, 5200, 200)).area, 1.0)


class HatchedStripsStay(unittest.TestCase):

    def test_a_hatched_strip_is_not_absorbed(self):
        """400 x 250 foot strip, 80% on a 400 x 200 junction square: its last
        50 mm is concrete."""
        strips = [{"pts": rect(0, 0, 400, 200), "t": 200},
                  {"pts": [(0, 0), (0, 250), (400, 250), (400, 0)], "t": 400, "hatched": True}]
        pieces, _rep = dr.wall_polygons(strips, PLATE)
        U = unary_union(pieces[LN.WALL])
        self.assertAlmostEqual(U.area, 400 * 250, delta=50)

    def test_an_unhatched_one_still_is(self):
        strips = [{"pts": rect(0, 0, 400, 200), "t": 200},
                  {"pts": [(0, 0), (0, 250), (400, 250), (400, 0)], "t": 400}]
        pieces, _rep = dr.wall_polygons(strips, PLATE)
        U = unary_union(pieces[LN.WALL])
        self.assertAlmostEqual(U.area, 400 * 200, delta=50)


def walls_like_step3(segs, fallback_strips, plate):
    """The walls as Step 3 (and, until 2026-10-17, the geometry engine's
    _add_walls) builds them: walltrace on the segments, the fallback strips
    where the floor has no wall-layer lines, then wall_polygons. Returns
    the report per class."""
    from Drawing_Analyzer import walltrace as wt
    traced = wt.trace_floor(segs, [], [], plate, arcs=[])
    strips = (traced["strips"] if traced is not None else fallback_strips) or []
    _pieces, rep = dr.wall_polygons(strips, plate, [], [], [], [], [])
    return rep


class ClosuresDoNotCutWalls(unittest.TestCase):

    def test_closures_along_both_faces_leave_the_wall(self):
        segs = outline(rect(0, 0, 200, 1100))
        engine_strips = [{"pts": rect(0, 0, 200, 1100), "t": 200, "layers": ("X", "X")}]
        rep = walls_like_step3(segs, engine_strips, PLATE)
        self.assertAlmostEqual(rep[LN.WALL]["area_sqm"], 0.22, places=2)


if __name__ == "__main__":
    unittest.main()
