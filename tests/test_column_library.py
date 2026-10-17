# -*- coding: utf-8 -*-
"""The column size library and the column finder's tiers 2 and 3.

  * column_library.json is read; a size is looked up project, floor, RCC,
    steel, imported, either way round, within its tolerance; a missing or
    broken file gives the built-in lists;
  * a project's sizes apply when one of its match words is in the drawing's
    name (the upload's meta file beside it);
  * tier 1 (the floor's grid group) is unchanged and named by the library;
  * tier 2: a drawn shape on a column layer (COLUMN_PLANTED is not a
    plant), or the floor's own size on a crossing of its grid;
  * tier 3: a hatched library size standing alone on a crossing of the grid;
    off the grid it is only for review; unhatched, on a services layer, a
    block's box alone, or holding a door's lines, it is never a column.

Run:  python -m unittest discover -s tests -v
"""

import json
import math
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402
from shapely.geometry import box  # noqa: E402

from Drawing_Analyzer import column_library as cl  # noqa: E402
from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import geoengine as ge  # noqa: E402


def seg(x1, y1, x2, y2, layer="A-WALL"):
    return (x1, y1, x2, y2, layer, math.hypot(x2 - x1, y2 - y1), -1, False, False)


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def sq(cx, cy, s):
    return rect(cx - s / 2.0, cy - s / 2.0, cx + s / 2.0, cy + s / 2.0)


class Library(unittest.TestCase):

    def test_the_shipped_file_is_read(self):
        lib = cl.load()
        self.assertIsNone(lib.get("error"))
        self.assertTrue(lib["path"].endswith("column_library.json"))
        self.assertEqual(lib["match_order"], ["project", "floor_confirmed", "rcc", "steel", "imported"])
        self.assertIn((600.0, 600.0), lib["rcc_rect"])

    def test_rcc_either_way_round_within_tolerance(self):
        self.assertEqual(cl.match(600, 600)["label"], "RCC 600x600")
        self.assertEqual(cl.match(452, 296)["label"], "RCC 300x450")
        self.assertEqual(cl.match(600, 600)["type"], "RCC")
        self.assertIsNone(cl.match(640, 640))

    def test_steel_and_imported(self):
        m = cl.match(200, 100)
        self.assertEqual((m["label"], m["type"], m["source"]), ("ISMB 200", "Steel", "steel"))
        self.assertIsNone(cl.match(204, 100))              # steel: 3 mm
        self.assertEqual(cl.match(356, 368)["label"], "UC 356")

    def test_round_columns(self):
        self.assertEqual(cl.match(450, 450, round_=True)["label"], "RCC dia 450")
        self.assertEqual(cl.match(219.1, 219.1, round_=True)["label"], "CHS 219.1")
        self.assertIsNone(cl.match(230, 300, round_=True))

    def test_the_floor_size_comes_before_the_lists(self):
        m = cl.match(668, 668, confirmed=[(668.0, 668.0)])
        self.assertEqual((m["source"], m["label"]), ("floor_confirmed", "floor 668x668"))
        self.assertIsNone(cl.match(668, 668))

    def test_a_project_by_its_match_word(self):
        self.assertEqual(cl.match(555, 555, names=["XA_37th_floor.dxf"])["label"], "XA 555x555")
        self.assertIsNone(cl.match(555, 555, names=["MAXAM_floor.dxf"]))
        self.assertEqual(cl.match(668, 668, names=["centaurus11_floor.dxf"])["label"], "Centaurus 668x668")

    def test_a_broken_file_gives_the_built_in_lists(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "column_library.json")
            with open(p, "w") as fh:
                fh.write("{ not json")
            lib = cl.load(p)
            self.assertIn("error", lib)
            self.assertEqual(cl.match(600, 600, lib=lib)["label"], "RCC 600x600")
            self.assertTrue(lib["enabled"])
            lib2 = cl.load(os.path.join(td, "missing.json"))
            self.assertEqual(cl.match(200, 100, lib=lib2)["label"], "ISMB 200")

    def test_a_file_adds_its_own_sizes(self):
        lib = cl.build({"rcc_rect": [[640, 640]], "projects": {"P1": {"sizes": [[555, 555]]}}})
        self.assertEqual(cl.match(640, 640, lib=lib)["label"], "RCC 640x640")
        self.assertIsNone(cl.match(600, 600, lib=lib))
        self.assertEqual(cl.match(555, 555, lib=lib)["label"], "P1 555x555")   # no match words: always

    def test_the_drawing_names_from_the_meta_file(self):
        with tempfile.TemporaryDirectory() as td:
            dxf = os.path.join(td, "abc123.dxf")
            with open(os.path.join(td, "abc123.meta.json"), "w") as fh:
                json.dump({"original_name": "XA_37th.dxf", "project_name": "Tower B"}, fh)
            self.assertEqual(cl.context_names(dxf), ["abc123.dxf", "XA_37th.dxf", "Tower B"])
            self.assertEqual(cl.context_names(None), [])

    def test_planted_is_not_a_plant(self):
        self.assertIsNone(ge._NOT_COLUMN_LAYER.search("COLUMN_PLANTED"))
        self.assertIsNotNone(ge._NOT_COLUMN_LAYER.search("L-PLANTER"))


class Helpers(unittest.TestCase):

    def test_a_wall_running_through(self):
        idx = ge._seg_index([seg(0, 0, 5000, 0), seg(0, 300, 5000, 300)])
        self.assertTrue(ge._runs_past(box(2000, 0, 2600, 300), idx))
        # the walls stop at it: a column
        idx2 = ge._seg_index([seg(0, 0, 2000, 0), seg(0, 300, 2000, 300), seg(2600, 0, 5000, 0),
                              seg(2600, 300, 5000, 300)])
        self.assertFalse(ge._runs_past(box(2000, 0, 2600, 300), idx2))
        # one face only: a column on a wall's face
        self.assertFalse(ge._runs_past(box(2000, 0, 2600, 600), ge._seg_index([seg(0, 0, 5000, 0)])))

    def test_touching_walls(self):
        faces = ge._wall_faces([seg(0, 0, 2000, 0), seg(2000, 0, 2000, 500, "A-COLUMN")])
        self.assertTrue(ge._touches_walls(box(2000, -300, 2600, 300), faces))
        self.assertFalse(ge._touches_walls(box(4000, -300, 4600, 300), faces))

    def test_door_lines_inside(self):
        idx = ge._seg_index([seg(100, 150, 1100, 150, "A-DOOR")])
        self.assertTrue(ge._holds_lines(box(0, 0, 1200, 300), idx))
        self.assertFalse(ge._holds_lines(box(2000, 0, 3200, 300), idx))


class Tiers(unittest.TestCase):
    """Ten 600 mm columns on an 8 m grid (4 x 3, two crossings empty) on
    A-COLUMN, and around them the shapes tiers 2 and 3 judge."""

    @classmethod
    def setUpClass(cls):
        doc = ezdxf.new()
        msp = doc.modelspace()
        xs, ys = (2000, 10000, 18000, 26000), (2000, 10000, 18000)
        for x in xs:
            for y in ys:
                if (x, y) in ((18000, 18000), (26000, 18000)):
                    continue
                msp.add_lwpolyline(sq(x, y, 600), close=True, dxfattribs={"layer": "A-COLUMN"})

        def hatch(pts, layer):
            h = msp.add_hatch(dxfattribs={"layer": layer})
            h.set_solid_fill()
            h.paths.add_polyline_path(pts, is_closed=True)

        # tier 3: hatched, standing alone, on a crossing of the grid
        msp.add_lwpolyline(sq(18000, 18000, 450), close=True, dxfattribs={"layer": "0"})
        hatch(sq(18000, 18000, 450), "0")
        # not a column: unhatched, on a crossing
        msp.add_lwpolyline(sq(26000, 18000, 450), close=True, dxfattribs={"layer": "0"})
        # review: hatched, standing alone, off the grid
        msp.add_lwpolyline(sq(6000, 6000, 450), close=True, dxfattribs={"layer": "0"})
        hatch(sq(6000, 6000, 450), "0")
        # not a column: a services layer, hatched, off the grid
        msp.add_lwpolyline(sq(6000, 14000, 450), close=True, dxfattribs={"layer": "PLUMB-A"})
        hatch(sq(6000, 14000, 450), "PLUMB-A")
        # tier 2: a planted column, off the grid
        msp.add_lwpolyline(sq(14000, 14000, 400), close=True, dxfattribs={"layer": "COLUMN_PLANTED"})
        # not a column: a door opening drawn on the column layer
        msp.add_lwpolyline(rect(21400, 5850, 22600, 6150), close=True, dxfattribs={"layer": "COLUMNS"})
        # not a column: a block's box on the column layer
        blk = doc.blocks.new("TAGBOX")
        blk.add_line((0, 0), (500, 500))
        msp.add_blockref("TAGBOX", (14000, 4000), dxfattribs={"layer": "S-COLUMN"})
        cls.doc, cls.msp = doc, msp
        cls.floors = [{"name": "F", "polygon": rect(-1000, -1000, 29000, 21000)}]
        cls.segs = [seg(21500, 6000, 22500, 6000, "A-DOOR")]
        cls.rev = {}
        cls.cols = ge._column_shapes(doc, msp, dr.MM_UNITS, cls.floors, segs=cls.segs, review=cls.rev)["F"]

    def at(self, x, y):
        return [(pg, lay, how) for pg, lay, how in self.cols if pg.contains(box(x - 1, y - 1, x + 1, y + 1))]

    def test_tier_1_unchanged_and_named(self):
        t1 = [c for c in self.cols if c[2].get("tier") == 1]
        self.assertEqual(len(t1), 10)
        self.assertTrue(all(h["label"] == "RCC 600x600" and h["grid"] for _p, _l, h in t1))

    def test_tier_2_a_planted_column(self):
        got = self.at(14000, 14000)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][2]["tier"], 2)
        self.assertEqual(got[0][1], "COLUMN_PLANTED")

    def test_tier_3_a_library_size_on_the_grid(self):
        got = self.at(18000, 18000)
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0][2]["tier"], got[0][2]["label"]), (3, "RCC 450x450"))

    def test_never_columns(self):
        for x, y in ((26000, 18000), (6000, 14000), (22000, 6000), (14250, 4250), (6000, 6000)):
            self.assertEqual(self.at(x, y), [], (x, y))
        self.assertEqual(len(self.cols), 12)

    def test_review_off_the_grid(self):
        rv = self.rev.get("F") or []
        self.assertEqual(len(rv), 1)
        self.assertIn("RCC 450x450", rv[0]["why"])
        xs = [p[0] for p in rv[0]["pts"]]
        self.assertAlmostEqual((min(xs) + max(xs)) / 2.0, 6000.0, delta=1.0)

    def test_the_library_switched_off(self):
        lib = dict(cl.library(), enabled=False)
        old = cl._LIB[:]
        cl._LIB[:] = [lib]
        try:
            cols = ge._column_shapes(self.doc, self.msp, dr.MM_UNITS, self.floors, segs=self.segs)["F"]
        finally:
            cl._LIB[:] = old
        self.assertEqual(len(cols), 10)

    def test_step3_gets_the_review_and_the_tiers(self):
        res = dr._floor_columns(self.doc, self.msp, dr.MM_UNITS, self.floors, segs=self.segs)
        self.assertEqual(len(res["columns"]["F"]), 12)
        self.assertEqual(sorted(i.get("tier") for i in res["info"]["F"]).count(1), 10)
        self.assertEqual(len(res["review"]["F"]), 1)


if __name__ == "__main__":
    unittest.main()
