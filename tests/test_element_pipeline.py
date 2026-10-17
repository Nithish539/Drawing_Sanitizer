"""The element pipeline (2026-10-17): what is decided is locked, and every
wall built after a lock skips what the mask holds.

  * LockedMask: a locked region only grows; a line inside it is no wall
    candidate; a wall mass is cut back round it; a reclaimed piece never
    comes back.
  * walltrace.trace_floor: an X's frame (skip) is never paired as a wall,
    a hatch inside a locked box is not wall material.
  * wall_polygons: a strip mostly inside a locked cell goes, the mass is
    cut round the cells before the runs are formed.
  * the column finder: every outer ring of a hatch is read; a hollow
    shape of a wall's thickness on a column layer is a wall whatever
    layer the floor's columns stand on.
  * rules: element_pipeline exists and is on by default.
"""
import math
import os
import sys
import tempfile
import unittest

import ezdxf
from shapely.geometry import box, Polygon
from shapely.ops import unary_union

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Drawing_Analyzer import dxf_reader as dr          # noqa: E402
from Drawing_Analyzer import walltrace as wt           # noqa: E402
from Drawing_Analyzer import geoengine as ge           # noqa: E402
from Drawing_Analyzer import layernames as LN          # noqa: E402
from Drawing_Analyzer import rules as rb               # noqa: E402
from Drawing_Analyzer.locked import LockedMask         # noqa: E402

PLATE = [(-1000, -1000), (11000, -1000), (11000, 11000), (-1000, 11000)]
BIG_PLATE = [(0, 0), (40000, 0), (40000, 40000), (0, 40000)]


def seg(x1, y1, x2, y2, layer="A-WALL"):
    return (x1, y1, x2, y2, layer, math.hypot(x2 - x1, y2 - y1), -1, False, False)


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


class MaskTests(unittest.TestCase):

    def test_a_locked_region_only_grows(self):
        m = LockedMask()
        self.assertEqual(m.lock("x_shaft", [box(0, 0, 300, 600)]), 1)
        u1 = m.union()
        m.lock("door", [rect(1000, 0, 1900, 200)])
        u2 = m.union()
        self.assertTrue(u2.contains(u1.buffer(-0.1)))
        self.assertEqual(m.count(), 2)
        self.assertEqual(m.report()["x_shaft"]["count"], 1)
        self.assertAlmostEqual(m.report()["x_shaft"]["area_sqm"], 0.18, places=3)

    def test_a_line_inside_a_locked_region_is_no_candidate(self):
        m = LockedMask()
        m.lock("x_shaft", [box(0, 0, 300, 600)])
        inside = seg(10, 10, 290, 590)
        along = seg(0, 0, 300, 0)                     # the cell's own edge: kept
        across = seg(-100, 100, 400, 100)             # runs through and beyond: kept
        counts = {}
        kept = m.filter_segments([inside, along, across], counts=counts)
        self.assertEqual(kept, [along, across])
        self.assertEqual(counts["segments_in_locked"], 1)

    def test_the_mass_is_cut_back_round_the_cell(self):
        m = LockedMask()
        m.lock("x_lift", [box(2000, 0, 4500, 2500)])
        wall = box(0, -200, 10000, 300)                # a wall drawn over the lift's X
        left = m.subtract(wall)
        self.assertFalse(left.intersects(box(2001, 1, 4499, 299)))
        self.assertTrue(m.covers(box(2500, 0, 3000, 300)))
        self.assertFalse(m.covers(box(0, 0, 1000, 300)))

    def test_a_reclaimed_piece_never_comes_back(self):
        """Once a cell is locked, pairing the same faces again gives the
        same strip, and the mask cuts it away again: a later wall build on
        the mask cannot bring the piece back."""
        m = LockedMask()
        m.lock("x_shaft", [box(2000, 0, 2300, 600)])
        strips = [{"pts": rect(0, 0, 5000, 200), "t": 200}]         # a wall through the X
        pieces, _r = dr.wall_polygons(strips, PLATE, locked=m.union(("x_shaft",)))
        U = unary_union(pieces[LN.WALL])
        self.assertLess(U.intersection(box(2000, 0, 2300, 200)).area, 1.0)
        # built again on the same mask, with another lock added: still gone
        m.lock("door", [rect(4000, 0, 4800, 200)])
        pieces2, _r = dr.wall_polygons(strips, PLATE, locked=m.union(("x_shaft", "door")))
        U2 = unary_union(pieces2[LN.WALL])
        self.assertLess(U2.intersection(box(2000, 0, 2300, 200)).area, 1.0)
        self.assertLess(U2.intersection(box(4000, 0, 4800, 200)).area, 1.0)
        self.assertGreater(U2.intersection(box(0, 0, 2000, 200)).area, 0.9 * 2000 * 200)


class WallPolygonsTests(unittest.TestCase):

    def test_a_strip_mostly_in_a_locked_cell_goes(self):
        strips = [{"pts": rect(0, 0, 5000, 200), "t": 200},
                  {"pts": rect(2000, 200, 2300, 800), "t": 300}]   # the X's own box, paired as a wall
        counts = {}
        pieces, _r = dr.wall_polygons(strips, PLATE, counts=counts, locked=[rect(2000, 200, 2300, 800)])
        self.assertEqual(counts.get("in_locked"), 1)
        U = unary_union(pieces[LN.WALL])
        self.assertLess(U.intersection(box(2000, 200, 2300, 800)).area, 1.0)

    def test_without_a_mask_nothing_changes(self):
        strips = [{"pts": rect(0, 0, 5000, 200), "t": 200}]
        a, _ = dr.wall_polygons(strips, PLATE)
        b, _ = dr.wall_polygons(strips, PLATE, locked=None)
        self.assertAlmostEqual(unary_union(a[LN.WALL]).area, unary_union(b[LN.WALL]).area)


class WalltraceTests(unittest.TestCase):

    def test_an_x_frame_is_not_paired_as_a_wall(self):
        wall = [seg(0, 0, 10000, 0), seg(0, 200, 10000, 200)]
        # a 300 x 600 X box standing in the room: its frame is two lines 300
        # apart (drawn on the wall layer: walltrace reads wall-layer lines
        # only), read as a 300 mm wall unless held out
        frame = [seg(2000, 2000, 2600, 2000), seg(2000, 2300, 2600, 2300)]
        frame_keys = {dr._seg_key(s) for s in frame}
        r0 = wt.trace_floor(wall + frame, [], [], PLATE)
        r1 = wt.trace_floor(wall + frame, [], [], PLATE, skip=lambda s: dr._seg_key(s) in frame_keys)
        self.assertIsNotNone(r0)
        self.assertIsNotNone(r1)
        strips0 = [Polygon(s["pts"]) for s in r0["strips"]]
        strips1 = [Polygon(s["pts"]) for s in r1["strips"]]
        X = box(2000, 2000, 2600, 2300)
        self.assertTrue(any(p.intersection(X).area > 0.5 * X.area for p in strips0))
        self.assertFalse(any(p.intersection(X).area > 0.5 * X.area for p in strips1))

    def test_a_hatch_inside_a_locked_box_is_not_material(self):
        wall = [seg(0, 0, 10000, 0), seg(0, 200, 10000, 200)]
        wall_hatch = ([(0, 0), (10000, 0), (10000, 200), (0, 200)], [], "WHATEVER", "ANSI37", False)
        x_hatch = ([(2000, 200), (2600, 200), (2600, 500), (2000, 500)], [], "WHATEVER", "SOLID", True)
        counts = {}
        r = wt.trace_floor(wall, [wall_hatch, x_hatch], [], PLATE, locked=box(2000, 200, 2600, 500), counts=counts)
        self.assertIsNotNone(r)
        self.assertLess(r["material"].intersection(box(2001, 201, 2599, 499)).area, 1.0)
        self.assertEqual(counts.get("walltrace_in_locked"), 1)


class ColumnFinderTests(unittest.TestCase):

    def _doc(self):
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        return doc, doc.modelspace()

    def _run(self, doc):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "c.dxf")
            doc.saveas(p)
            d2, m2, u2 = dr.load_document(p, 4)
            rej = {}
            cols = ge._column_shapes(d2, m2, u2, [{"name": "F", "polygon": BIG_PLATE}], rejected=rej)
        return cols, rej

    def test_every_outer_ring_of_a_hatch_is_read(self):
        doc, msp = self._doc()
        # one hatch entity with every column of the grid: 16 rings
        h = msp.add_hatch(dxfattribs={"layer": "A-COL HATCH"})
        for i in range(4):
            for j in range(4):
                x, y = 6000 + 8000 * i, 6000 + 8000 * j
                h.paths.add_polyline_path([(x, y), (x + 800, y), (x + 800, y + 800), (x, y + 800)], is_closed=True)
        cols, _rej = self._run(doc)
        self.assertEqual(len(cols["F"]), 16)

    def test_a_hollow_wall_on_a_column_layer_is_a_wall_whatever_the_grid_layer(self):
        doc, msp = self._doc()
        for i in range(4):                                # the grid on S-COLUMN
            for j in range(4):
                x, y = 6000 + 8000 * i, 6000 + 8000 * j
                msp.add_lwpolyline([(x, y), (x + 800, y), (x + 800, y + 800), (x, y + 800)], close=True,
                                   dxfattribs={"layer": "S-COLUMN"})
        # a U-shaped shear wall 280 thick, 3000 x 2500, on another column layer
        u = [(1000, 1000), (4000, 1000), (4000, 3500), (3720, 3500), (3720, 1280),
             (1280, 1280), (1280, 3500), (1000, 3500)]
        msp.add_lwpolyline(u, close=True, dxfattribs={"layer": "A-COLUMN"})
        cols, rej = self._run(doc)
        self.assertEqual(len(cols["F"]), 16)
        self.assertEqual(len(rej["F"]), 1)
        self.assertGreater(rej["F"][0].area, 1.5e6)


class RuleTests(unittest.TestCase):

    def test_the_rule_exists_and_is_on(self):
        self.assertTrue(rb.defaults()["element_pipeline"])
        self.assertFalse(rb.resolve({"element_pipeline": False})["element_pipeline"])
        self.assertEqual(rb.describe("element_pipeline")["group"], "walls")


if __name__ == "__main__":
    unittest.main()
