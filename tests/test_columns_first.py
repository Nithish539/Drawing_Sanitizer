# -*- coding: utf-8 -*-
"""Rule columns_first (2026-10-17): the columns are step 00 of Step 3.

  * xplace.early_column: an X on a column, or inside a solid fill no larger
    than itself, is a column's cross before any wall is known; the X
    sequence (x_facts) reads the same two tests;
  * Step 3: no W1 strip lies in a column (the column's lines still bound
    the room); an X in a solid fill is a column from step 00, and no wall is
    drawn over any column;
  * the rule exists, is on by default, and off gives the pass as before.

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402
from shapely.geometry import Polygon, box  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import rules as rb  # noqa: E402
from Drawing_Analyzer import xplace as xp  # noqa: E402


class EarlyColumn(unittest.TestCase):

    def test_on_a_finder_column(self):
        self.assertEqual(xp.early_column((100, 100, 300, 300), [box(0, 0, 400, 400)]), xp.ON_A_COLUMN)

    def test_inside_a_solid_fill_no_larger_than_itself(self):
        self.assertEqual(xp.early_column((0, 0, 430, 430), [], [box(-5, -5, 435, 435)]), xp.IN_A_FILL)

    def test_not_a_column(self):
        self.assertIsNone(xp.early_column((0, 0, 430, 430), [], [box(-2000, -200, 3000, 630)]))  # a wall's fill
        self.assertIsNone(xp.early_column((0, 0, 400, 1000), [], [box(0, 0, 400, 1000)]))         # not squarish
        self.assertIsNone(xp.early_column((0, 0, 430, 430)))
        self.assertIsNone(xp.early_column((10, 10, 10, 400), [box(0, 0, 400, 400)]))              # no box

    def test_the_x_sequence_reads_the_same_tests(self):
        mass = xp.WallMass([], [box(-5, -5, 435, 435)], [], columns=[box(5000, 0, 5400, 400)])
        self.assertEqual(xp.x_facts((0, 0, 430, 430), mass)["column"], xp.IN_A_FILL)
        self.assertEqual(xp.x_facts((5100, 100, 5300, 300), mass)["column"], xp.ON_A_COLUMN)
        self.assertIsNone(xp.x_facts((2000, 2000, 2400, 2400), mass)["column"])


class RuleTests(unittest.TestCase):

    def test_the_rule_exists_and_is_on(self):
        self.assertTrue(rb.defaults()["columns_first"])
        self.assertFalse(rb.resolve({"columns_first": False})["columns_first"])
        self.assertEqual(rb.describe("columns_first")["group"], "walls")


def build_floor(tmp):
    """A 20 x 10 m office: 200 mm walls on A-WALL; six 380 mm columns on
    S-COLUMN on a grid, each drawn with a second outline 10 mm inside it;
    a 430 mm column drawn only as a solid fill with an X, standing alone."""
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    plate = [(0, 0), (40000, 0), (40000, 30000), (0, 30000)]
    msp.add_lwpolyline(plate, close=True, dxfattribs={"layer": "A-FLOOR"})

    def r(x0, y0, x1, y1, layer="A-WALL"):
        msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": layer})
    t = 200
    r(10000 - t, 10000 - t, 30000 + t, 10000)
    r(10000 - t, 20000, 30000 + t, 20000 + t)
    r(10000 - t, 10000, 10000, 20000)
    r(30000, 10000, 30000 + t, 20000)
    for x in (14000, 20000, 26000):
        for y in (13000, 17000):
            r(x - 190, y - 190, x + 190, y + 190, "S-COLUMN")
            r(x - 180, y - 180, x + 180, y + 180, "S-COLUMN")
    # the column known only by its fill and its cross
    h = msp.add_hatch(dxfattribs={"layer": "A-WALL"})
    h.set_solid_fill()
    h.paths.add_polyline_path([(17000, 11500), (17430, 11500), (17430, 11930), (17000, 11930)], is_closed=True)
    msp.add_line((17000, 11500), (17430, 11930), dxfattribs={"layer": "0"})
    msp.add_line((17000, 11930), (17430, 11500), dxfattribs={"layer": "0"})
    msp.add_mtext("OFFICE", dxfattribs={"insert": (22000, 18500), "char_height": 200, "layer": "A-TEXT"})
    src = os.path.join(tmp, "in.dxf")
    doc.saveas(src)
    return src, [{"name": "F1", "polygon": plate}]


def even_odd(polys):
    """AI_WALL as drawn: a wall's outer ring and its holes are separate
    closed polylines on the one layer, read even-odd."""
    ps = sorted(polys, key=lambda g: -g.area)
    depth = [sum(1 for o in ps if o.area > g.area and o.contains(g.representative_point())) for g in ps]
    solid = unary_union([g for g, d in zip(ps, depth) if d % 2 == 0])
    holes = [g for g, d in zip(ps, depth) if d % 2 == 1]
    return solid.difference(unary_union(holes)) if holes else solid


def ai_polys(path, layer):
    doc = ezdxf.readfile(path)
    out = []
    for e in doc.modelspace().query("LWPOLYLINE"):
        if e.dxf.layer == layer:
            pts = [(p[0], p[1]) for p in e.get_points()]
            if len(pts) >= 3:
                g = Polygon(pts).buffer(0)
                if not g.is_empty:
                    out.append(g)
    return out


class StepZero(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        src, floors = build_floor(tmp)
        rows = dr.extract_texts(src, floors)
        cls.out_on, cls.out_off = os.path.join(tmp, "on.dxf"), os.path.join(tmp, "off.dxf")
        cache = dr._PASS_CACHE_DIR
        dr.set_pass_cache_dir(None)
        try:
            cls.on = dr.annotate_dxf(src, cls.out_on, floors, rows)
            cls.off = dr.annotate_dxf(src, cls.out_off, floors, rows, rule_overrides={"columns_first": False})
        finally:
            dr.set_pass_cache_dir(cache)

    def test_no_w1_strip_in_a_column(self):
        self.assertGreaterEqual(self.on.get("w1_strips_in_columns", 0), 1, self.on)
        self.assertEqual(self.off.get("w1_strips_in_columns", 0), 0)

    def test_the_cross_in_a_fill_is_a_column_from_step_00(self):
        self.assertEqual(self.on.get("columns_first_crosses", 0), 1, self.on)
        self.assertEqual(self.off.get("columns_first_crosses", 0), 0)
        self.assertGreaterEqual(self.on.get("x_class_column", 0), 1, self.on)

    def test_no_wall_over_any_column(self):
        cols = unary_union(ai_polys(self.out_on, "AI_COLUMN") + [box(17000, 11500, 17430, 11930)])
        walls = even_odd(ai_polys(self.out_on, "AI_WALL"))
        self.assertGreater(walls.area, 5e6)                           # the office's walls are there
        self.assertLess(walls.intersection(cols).area, 0.01 * cols.area)

    def test_the_room_still_goes_round_the_columns(self):
        rooms_on = dr.compute_room_areas(self.out_on, "AI_FLOOR")["rooms"]
        rooms_off = dr.compute_room_areas(self.out_off, "AI_FLOOR")["rooms"]
        office_on = [r_ for r_ in rooms_on if "OFFICE" in str(r_["name"])]
        office_off = [r_ for r_ in rooms_off if "OFFICE" in str(r_["name"])]
        self.assertEqual(len(office_on), 1)
        self.assertEqual(len(office_off), 1)
        self.assertAlmostEqual(office_on[0]["area_sqm"], office_off[0]["area_sqm"], delta=0.05)

    def test_the_step_is_timed(self):
        tm = self.on.get("timings") or {}
        if isinstance(tm, dict) and tm:
            keys = [k for v in tm.values() for k in (v.keys() if isinstance(v, dict) else [])] or list(tm)
            self.assertTrue(any("00 columns" in str(k) for k in keys), keys)


if __name__ == "__main__":
    unittest.main()
