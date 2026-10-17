# -*- coding: utf-8 -*-
"""The six fixes from the BHUB 1st Floor / P285 11th Floor review: ledge
lines, windows in walls, units kept between steps, staircases and the
guardrail, the per-floor cache key, STAIR PREZ labels.

Run:  python -m unittest discover -s tests -v
"""

import collections
import importlib
import math
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402
from shapely.geometry import Polygon, box  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import geoengine as ge  # noqa: E402
from Drawing_Analyzer import geocompare as gc  # noqa: E402


def seg(x1, y1, x2, y2, layer):
    """A raw segment as _collect_raw_segs gives it."""
    return (x1, y1, x2, y2, layer, math.hypot(x2 - x1, y2 - y1), -1, False)


def has_face(faces, y):
    return any(abs(f[1] - y) < 1 and abs(f[3] - y) < 1 for f in faces)


class LedgeTests(unittest.TestCase):
    """1. A WC ledge in front of a wall is not the wall's other face."""

    def wall_with_ledge(self, ledge_layer="A3-WL_EXT", fixtures=True):
        # a 200 mm wall (faces at y -200 and 0) and a ledge 150 mm in front
        # of it (BHUB: the male toilet's ledge at 62756, wall at 62606/62406)
        segs = [seg(0, -200, 4000, -200, "A3-WL_EXT"),
                seg(0, 0, 4000, 0, "A3-WL_EXT"),
                seg(0, 150, 3000, 150, ledge_layer)]
        if fixtures:                                   # basins on the ledge
            segs += [seg(500, 150, 500, 600, "toil-fix"), seg(1500, 150, 1500, 600, "toil-fix")]
        return segs

    def test_ledge_is_skipped(self):
        w = dr._detect_walls(self.wall_with_ledge())
        self.assertFalse(has_face(w["faces"], 150))     # the ledge
        self.assertTrue(has_face(w["faces"], 0))        # the wall's two faces
        self.assertTrue(has_face(w["faces"], -200))
        self.assertEqual(w["ledges"], 1)

    def test_outer_face_is_not_a_ledge(self):
        # the outer face pairs only with the inner face too, but nothing
        # stands on it: spacing alone cannot make it a ledge
        w = dr._detect_walls(self.wall_with_ledge(fixtures=False))
        self.assertTrue(has_face(w["faces"], -200))
        self.assertTrue(has_face(w["faces"], 150))      # no evidence: kept
        self.assertEqual(w["ledges"], 0)

    def test_structure_layer_is_never_a_ledge(self):
        w = dr._detect_walls(self.wall_with_ledge(ledge_layer="A-WALL"))
        self.assertTrue(has_face(w["faces"], 150))

    def test_deeper_than_the_limit_is_a_wall(self):
        segs = [seg(0, -200, 4000, -200, "A3-WL_EXT"), seg(0, 0, 4000, 0, "A3-WL_EXT"),
                seg(0, 300, 3000, 300, "A3-WL_EXT"), seg(500, 300, 500, 800, "toil-fix")]
        w = dr._detect_walls(segs)
        self.assertTrue(has_face(w["faces"], 300))      # 300 mm > ledge_max_depth_mm


class WindowTests(unittest.TestCase):
    """2. An X-marked outline lying in a wall is a window, not a shaft."""

    def test_window_in_a_wall(self):
        faces = [(0, 0, 10000, 0), (0, 200, 10000, 200)]           # a 200 mm wall
        f = dr.WindowFinder(faces)
        self.assertIsNotNone(f.reason(box(4000, 0, 5500, 200)))

    def test_window_in_a_wall_hatch_slot(self):
        # BHUB facade: the window sits in a hole of the wall hatch, which is
        # a slot in the 300 mm wall
        outer = [(0, 0), (10000, 0), (10000, 300), (0, 300)]
        hole = [(8, 8), (9992, 8), (9992, 292), (8, 292)]
        f = dr.WindowFinder([], [(outer, [hole])])
        self.assertIn("hatch", f.reason(box(4000, 0, 5500, 300)))

    def test_thick_or_between_two_walls_or_alone_is_a_shaft(self):
        faces = [(0, 0, 10000, 0), (0, 200, 10000, 200)]
        self.assertIsNone(dr.WindowFinder(faces).reason(box(4000, 0, 5000, 600)))   # 600 mm deep
        # a riser between two walls: a further face beyond each long side
        two = [(0, -200, 10000, -200), (0, 0, 10000, 0), (0, 300, 10000, 300), (0, 500, 10000, 500)]
        self.assertIsNone(dr.WindowFinder(two).reason(box(4000, 0, 5500, 300)))
        # a strip on its own (a cut-out beside a shaft): only its own outline
        own = [(4000, 0, 5500, 0), (4000, 300, 5500, 300)]
        self.assertIsNone(dr.WindowFinder(own).reason(box(4000, 0, 5500, 300)))

    def test_riser_against_a_wall_stays_a_shaft(self):
        # BHUB tenant risers: a 300 mm X strip lying against a wall's face,
        # only its own edge on the other side - a riser, not a window
        faces = [(0, 0, 0, 6000), (-200, 0, -200, 6000),            # the wall it leans on
                 (300, 2000, 300, 3000),                            # its own far edge
                 (0, 2000, 950, 2000), (0, 3000, 950, 3000)]        # the cell's cross walls
        self.assertIsNone(dr.WindowFinder(faces).reason(box(0, 2000, 300, 3000)))

    def test_opening_in_a_wall_line(self):
        # an opening cut in a 200 mm wall: its faces stop at the X's edges
        # and carry on beyond both ends
        faces = [(800, 0, 800, 3000), (1000, 0, 1000, 3000),            # wall below the X
                 (800, 3450, 800, 6000), (1000, 3450, 1000, 6000),      # and above it
                 (800, 3000, 800, 3450), (1000, 3000, 1000, 3450)]      # the X's own edges
        self.assertIsNotNone(dr.WindowFinder(faces).reason(box(800, 3000, 1000, 3450)))

    def test_window_class_is_wired(self):
        # a window is Step 3's (the X sequence's step 4, and the gaps in the
        # walls with glazing): the geometry engine finds none (2026-10-16)
        self.assertNotIn("WINDOW", ge.CLASSES)
        self.assertNotIn("WINDOW", ge.LAYER_OF)
        self.assertIn("WINDOW", ge.STEP3_CLASSES)
        self.assertNotIn("windows", ge.DEFAULT_SEQUENCE)
        self.assertEqual(gc.SUMMARY_CLASS["WINDOW"], "WINDOW")
        self.assertEqual(gc.text_class_of_layer(dr.WINDOW_LAYER), "WINDOW")
        self.assertEqual(gc.SPECIFIC["WINDOW"], set())


class UnitsTests(unittest.TestCase):
    """3. Every step reads the drawing in the units settled at upload."""

    def test_units_of_the_upload_are_used(self):
        A = importlib.import_module("Drawing_Analyzer.app")
        meta = {"unit_override": None, "units": {"insunits_code": 4, "label": "Millimetres"}}
        self.assertEqual(A._unit_of(meta), 4)
        self.assertEqual(A._unit_of({"unit_override": "1", "units": {"insunits_code": 4}}), "1")
        self.assertIsNone(A._unit_of({}))

    def test_generated_file_reads_back_in_the_same_units(self):
        # a drawing whose header says Inches but which is drawn in mm: read
        # with the upload's units it is not scaled 25.4 times
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 1
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (40000, 0), (40000, 30000), (0, 30000)], close=True)
        msp.add_lwpolyline([(1000, 1000), (4000, 1000), (4000, 3000), (1000, 3000)], close=True,
                           dxfattribs={"layer": "TOILET_BOUNDARY"})
        msp.add_mtext("TOILET", dxfattribs={"layer": "TOILET_BOUNDARY", "insert": (2500, 2000)})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.dxf")
            doc.saveas(path)
            floors = [{"name": "F", "polygon": [(0, 0), (40000, 0), (40000, 30000), (0, 30000)]}]
            got = gc.read_text_boundaries(path, floors, 4)
            self.assertEqual(len(got["F"]), 1)
            self.assertAlmostEqual(got["F"][0]["pg"].area / 1e6, 6.0, places=2)
            # and a file the app writes records the units it was read in
            d2, _m, units = dr.load_document(path, 4)
            dr.set_insunits(d2, units)
            self.assertEqual(d2.header["$INSUNITS"], 4)


class StaircaseGuardTests(unittest.TestCase):
    """4. Treads inside a staircase do not cut it to its well."""

    def stair(self):
        e = ge._E("STAIRCASE", "stair_flights_space", box(0, 0, 4800, 7500), {}, "F")
        e.features = {}
        return e

    def test_treads_inside_do_not_cut(self):
        e = self.stair()
        treads = [(200, y, 2100, y) for y in range(1000, 6000, 300)] + \
                 [(2700, y, 4600, y) for y in range(1000, 6000, 300)] + \
                 [(2100, 1000, 2100, 6000), (2700, 1000, 2700, 6000)]       # the well's sides
        diag = collections.Counter()
        ge._guard_finding(e, treads, [], diag)
        self.assertAlmostEqual(e.pg.area / 1e6, 36.0, places=1)

    def test_cut_leaving_a_sliver_is_skipped(self):
        e = self.stair()
        # walls crossing in from outside either side of its centre: the
        # piece holding the centre would be a 0.5 m strip
        walls = [(-500, 3500, 5300, 3500), (-500, 4000, 5300, 4000)]
        diag = collections.Counter()
        ge._guard_finding(e, walls, [], diag)
        self.assertAlmostEqual(e.pg.area / 1e6, 36.0, places=1)
        self.assertTrue(any("guardrail cut skipped" in x["text"] for x in e.evidence))

    def test_a_room_is_still_cut(self):
        e = ge._E("ROOM", "room_space", box(0, 0, 6000, 4000), {}, "F")
        e.features = {}
        ge._guard_finding(e, [(3000, -100, 3000, 4100)], [], collections.Counter())
        self.assertLess(e.pg.area, 24e6 * 0.6)


class PassCacheKeyTests(unittest.TestCase):
    """5. The same job hashes the same however its sets were built."""

    def test_set_order_does_not_change_the_key(self):
        keys = [(float(i) * 1.37, float(i) * 2.11, "L%d" % (i % 7)) for i in range(400)]
        a, b = set(), set()
        for k in keys:
            a.add(k)
        for k in reversed(keys):
            b.add(k)
        ja = {"floor": {"name": "F"}, "x_lines": a, "rule": {"x": True}}
        jb = {"rule": {"x": True}, "x_lines": b, "floor": {"name": "F"}}
        self.assertEqual(dr._pass_key(ja), dr._pass_key(jb))
        self.assertIsInstance(ja["x_lines"], set)                 # the job itself is untouched
        jc = dict(ja, x_lines=a | {(0.5, 0.5, "Z")})
        self.assertNotEqual(dr._pass_key(ja), dr._pass_key(jc))


class StairPrezTests(unittest.TestCase):
    """6. STAIR PREZ / STAIR PR are shafts, not staircases."""

    def test_prez_is_a_shaft(self):
        for t in ("STAIR PREZ", "STAIR PR", "STAIR\nPREZ", "PRESSURISATION SHAFT"):
            self.assertEqual(dr.family_for_label(t, dr._FAMILY_RULES)["name"], "Shaft", t)
        self.assertEqual(dr.family_for_label("STAIRCASE", dr._FAMILY_RULES)["name"], "Staircase")


if __name__ == "__main__":
    unittest.main()
