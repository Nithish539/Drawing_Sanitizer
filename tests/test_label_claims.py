# -*- coding: utf-8 -*-
"""Which labels an X-mark (or any shape) may take, the stated-size gate,
and a room never lost because of its label.

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

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import geocompare as gc  # noqa: E402

CUT = "A3-CUTOUTS"


def _rect(msp, x0, y0, x1, y1, layer="A-WALL"):
    msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": layer})


def _walled_room(msp, x0, y0, x1, y1, t=200):
    _rect(msp, x0 - t, y0 - t, x1 + t, y0)
    _rect(msp, x0 - t, y1, x1 + t, y1 + t)
    _rect(msp, x0 - t, y0, x0, y1)
    _rect(msp, x1, y0, x1 + t, y1)


def _x(msp, x0, y0, x1, y1):
    _rect(msp, x0, y0, x1, y1, CUT)
    msp.add_line((x0, y0), (x1, y1), dxfattribs={"layer": CUT})
    msp.add_line((x0, y1), (x1, y0), dxfattribs={"layer": CUT})


def _text(msp, text, x, y):
    msp.add_mtext(text, dxfattribs={"insert": (x, y), "char_height": 150, "layer": "A-TEXT"})


class GateTests(unittest.TestCase):

    def test_stated_size_gate(self):
        self.assertFalse(dr.stated_fits((2500, 2500), 600, 2500))       # ELEC. ROOM on a cut-out
        self.assertTrue(dr.stated_fits((2500, 2500), 2480, 2530))
        self.assertTrue(dr.stated_fits((2300, 1270), 1270, 2300))       # either way round
        self.assertFalse(dr.stated_fits((2300, 1270), 2300, 300))
        self.assertTrue(dr.stated_fits(None, 600, 2500))                # nothing stated

    def test_shaft_like(self):
        self.assertTrue(dr.shaft_like_label("TEN", "MEP / Services"))
        self.assertTrue(dr.shaft_like_label("FA-PA-SH\n900X1000", "Shaft"))
        self.assertTrue(dr.shaft_like_label("RWP", "Plumbing"))
        self.assertFalse(dr.shaft_like_label("ELEC. ROOM 2500x2500", "Electrical"))
        self.assertFalse(dr.shaft_like_label("MALE TOILET", "Toilet"))
        self.assertFalse(dr.shaft_like_label("LIFT LOBBY", "Lift Lobby"))


class ClaimEndToEndTests(unittest.TestCase):
    """A room label 1 m outside a cut-out in its room, a TEN tag beside a
    300 x 600 X standing against a wall."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        plate = [(0, 0), (40000, 0), (40000, 30000), (0, 30000)]
        msp.add_lwpolyline(plate, close=True, dxfattribs={"layer": "A-FLOOR"})
        # ELEC. ROOM 2500x2500 with a 600 x 2500 cut-out along its top; its
        # label 1 m below the cut-out
        _walled_room(msp, 10000, 10000, 12500, 12500)
        _x(msp, 10000, 11900, 12500, 12500)
        _text(msp, "ELEC. ROOM 2500x2500", 11250, 10900)
        # TEN beside a 300 x 600 riser X against a wall (a shaft always
        # has a wall on one side at least - xplace)
        _x(msp, 25000, 10000, 25300, 10600)
        msp.add_lwpolyline([(24800, 9600), (25000, 9600), (25000, 11000), (24800, 11000)], close=True,
                           dxfattribs={"layer": "A-WALL"})
        _text(msp, "TEN", 25700, 10300)
        src = os.path.join(tmp, "in.dxf")
        doc.saveas(src)
        floors = [{"name": "F1", "polygon": plate}]
        rows = dr.extract_texts(src, floors)
        cls.out = os.path.join(tmp, "out.dxf")
        cls.stats = dr.annotate_dxf(src, cls.out, floors, rows)
        cls.areas = dr.compute_room_areas(cls.out, "A-FLOOR")

    def named(self, word):
        return [r for r in self.areas["rooms"] if word in (r["name"] or "").upper()]

    def test_room_label_outside_an_x_is_not_taken(self):
        elec = self.named("ELEC")
        self.assertEqual(len(elec), 1, self.areas["rooms"])
        # the room is traced (not the 1.5 sq.m cut-out), the cut-out a shaft of its own
        self.assertGreater(elec[0]["area_sqm"], 4.0)
        self.assertEqual(elec[0]["family"], "Electrical")
        cut = [r for r in self.areas["rooms"] if abs(r["area_sqm"] - 1.5) < 0.05]
        self.assertEqual(len(cut), 1)
        self.assertNotIn("ELEC", cut[0]["name"].upper())

    def test_ten_label_outside_its_x_is_taken(self):
        ten = self.named("TEN")
        self.assertEqual(len(ten), 1, self.areas["rooms"])
        self.assertAlmostEqual(ten[0]["area_sqm"], 0.18, delta=0.02)

    def test_a_size_label_is_not_given_to_a_shape_of_another_size(self):
        # nothing drawn under the ELEC name is 600 x 2500
        for r in self.named("ELEC"):
            self.assertGreater(r["area_sqm"], 1.6)


class CompareSizeTests(unittest.TestCase):

    def test_stated_size_disagreeing_is_a_conflict(self):
        sq = box(171006, 70611, 173506, 71211)                         # 600 x 2500, 1.5 sq.m
        text = {"F": [{"cls": "ELEC", "name": "ELEC. ROOM 2500x2500", "pg": sq}]}
        geo = {"floors": {"F": [{"id": "GEO-1", "cls": "ROOM", "label": "Room 1",
                                 "poly": [list(p) for p in list(sq.exterior.coords)[:-1]],
                                 "features": {}, "confidence": 0.9, "category": "", "reviewed": False}]}}
        cmp = gc.compare(geo, text)
        r = [x for x in cmp["rows"] if x.get("text")][0]
        self.assertEqual(r["category"], "CONFLICT")
        self.assertIn("stated size 2500 x 2500 = 6.25 sq.m", r["note"])

    def test_fitting_size_still_agrees(self):
        sq = box(0, 0, 2300, 1270)
        # (a shaft is Step 3's: the geometry finds the room it is drawn in)
        text = {"F": [{"cls": "MEP", "name": "PHE-SHAFT 2300X1270", "pg": sq}]}
        geo = {"floors": {"F": [{"id": "GEO-1", "cls": "ROOM", "label": "Room 1",
                                 "poly": [list(p) for p in list(sq.exterior.coords)[:-1]],
                                 "features": {}, "confidence": 0.9, "category": "", "reviewed": False}]}}
        cmp = gc.compare(geo, text)
        self.assertEqual([x["category"] for x in cmp["rows"] if x.get("text")], ["AGREEMENT"])


if __name__ == "__main__":
    unittest.main()
