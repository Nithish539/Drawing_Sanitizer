# -*- coding: utf-8 -*-
"""Step 2, method 2: floor outlines drawn as separate lines / open polylines.

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402

W, H = 60000.0, 50000.0          # a 3000 sq.m plate, in mm


def _dxf(build):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    build(msp)
    path = os.path.join(tempfile.mkdtemp(), "plan.dxf")
    doc.saveas(path)
    return path


def _sides_with_gaps(msp, gap, layer="A-FLOOR"):
    """The plate's four sides as LINEs, each stopping `gap` short of the
    next corner."""
    corners = [(0, 0), (W, 0), (W, H), (0, H)]
    for i in range(4):
        (x0, y0), (x1, y1) = corners[i], corners[(i + 1) % 4]
        dx, dy = x1 - x0, y1 - y0
        n = (dx * dx + dy * dy) ** 0.5
        msp.add_line((x0, y0), (x1 - dx / n * gap, y1 - dy / n * gap), dxfattribs={"layer": layer})


def _plates(path, lo=2000, hi=10000):
    cands, _u, diag = dr.get_floor_candidates(path, min_area_sqm=lo, max_area_sqm=hi)
    return cands, diag


class ConnectedLinesTests(unittest.TestCase):

    def test_gaps_within_two_metres_are_joined(self):
        cands, diag = _plates(_dxf(lambda m: _sides_with_gaps(m, 1500.0)))
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c.source, dr.SOURCE_LINES)
        self.assertAlmostEqual(c.area_sqm, 3000.0, delta=60)
        self.assertEqual(c.layer, "A-FLOOR")
        self.assertEqual(diag["joined_from_open_lines"], 1)
        self.assertEqual(c.to_ui()["source"], "Connected Open Lines")

    def test_gaps_over_two_metres_are_not(self):
        cands, _d = _plates(_dxf(lambda m: _sides_with_gaps(m, 2500.0)))
        self.assertEqual(cands, [])

    def test_lines_on_unrelated_layers_are_not_joined(self):
        def build(m):
            corners = [(0, 0), (W, 0), (W, H), (0, H)]
            for i in range(4):
                m.add_line(corners[i], corners[(i + 1) % 4], dxfattribs={"layer": "LAYER%d" % i})
        cands, _d = _plates(_dxf(build))
        self.assertEqual(cands, [])

    def test_an_inner_wall_does_not_spoil_the_outline(self):
        def build(m):
            _sides_with_gaps(m, 0.0)
            m.add_line((0, 20000), (W, 20000), dxfattribs={"layer": "A-FLOOR"})   # splits the plate
        cands, _d = _plates(_dxf(build))
        areas = sorted(round(c.area_sqm) for c in cands)
        self.assertIn(3000, areas)                       # the whole outline is offered

    def test_open_polyline_coming_back_to_its_start(self):
        def build(m):
            m.add_lwpolyline([(0, 0), (W, 0), (W, H), (0, H), (0, 1000)], dxfattribs={"layer": "A-FLOOR PROJ"})
        cands, _d = _plates(_dxf(build))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].source, dr.SOURCE_LINES)

    def test_area_range_still_applies(self):
        cands, _d = _plates(_dxf(lambda m: _sides_with_gaps(m, 1000.0)), lo=4000, hi=10000)
        self.assertEqual(cands, [])

    def test_same_plate_by_both_methods_is_one_plate(self):
        def build(m):
            m.add_lwpolyline([(0, 0), (W, 0), (W, H), (0, H)], close=True, dxfattribs={"layer": "A-FLOOR"})
            _sides_with_gaps(m, 800.0, layer="A-SLAB EDGE")
        cands, diag = _plates(_dxf(build))
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c.source, dr.SOURCE_CLOSED)            # the closed polyline is kept
        self.assertEqual(c.sources, [dr.SOURCE_CLOSED, dr.SOURCE_LINES])
        self.assertEqual(diag["joined_same_as_closed"], 1)

    def test_closed_polylines_work_as_before(self):
        def build(m):
            m.add_lwpolyline([(0, 0), (W, 0), (W, H), (0, H)], close=True, dxfattribs={"layer": "A-FLOOR"})
        cands, diag = _plates(_dxf(build))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].source, dr.SOURCE_CLOSED)
        self.assertEqual(cands[0].to_ui()["sources"], ["Closed Polyline"])
        self.assertEqual(diag["joined_from_open_lines"], 0)


if __name__ == "__main__":
    unittest.main()
