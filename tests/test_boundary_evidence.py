# -*- coding: utf-8 -*-
"""A room boundary stands only on evidence (2026-10-17).

  * every side of a room lies on a wall, the facade (the glass where glass
    is drawn, else the plate's edge), another room, a primary door line
    (AI_P_DOORS) or a column (rule boundary_on_evidence);
  * a plain line never closes a room: the room is not drawn and its label
    reports "no evidence" (with the rule off it is drawn, as before);
  * a lift lobby open at an end is closed at the outer limit of its lift
    bank (rule lift_lobby_to_lift_bank);
  * the sides of the rooms drawn are checked, and a stretch on nothing is
    marked on AI_REVIEW.

Run:  python -m unittest discover -s tests -v
"""

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
from Drawing_Analyzer import layernames as LN  # noqa: E402
from Drawing_Analyzer import rules  # noqa: E402


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


class RulesTests(unittest.TestCase):

    def test_the_rules_exist_and_are_on(self):
        on = rules.defaults()
        for k in ("boundary_on_evidence", "lift_lobby_to_lift_bank", "lift_without_door_mechanical"):
            self.assertTrue(on[k], k)


class FacadeGlassTests(unittest.TestCase):

    def test_glass_along_the_edge_is_the_facade(self):
        plate = rect(0, 0, 40000, 30000)
        glass = [(500, 1000, 500, 20000),          # 500 mm inside the west edge: facade
                 (20000, 15000, 24000, 15000)]     # a glass partition in the middle: not
        self.assertEqual(dr.facade_glass_segs(glass, plate), [(500.0, 1000.0, 500.0, 20000.0)])

    def test_nothing_without_glass(self):
        self.assertEqual(dr.facade_glass_segs([], rect(0, 0, 100, 100)), [])


class EvidenceCheckTests(unittest.TestCase):
    """boundary_evidence_check on a drawn floor: a 4 x 3 m room walled on
    three sides; its fourth side as given."""

    PLATE = rect(-5000, -5000, 15000, 15000)

    def check(self, fourth):
        doc = ezdxf.new()
        msp = doc.modelspace()
        for w in (rect(-200, -200, 4200, 0), rect(-200, 3000, 4200, 3200), rect(-200, 0, 0, 3000)):
            msp.add_lwpolyline(w, close=True, dxfattribs={"layer": LN.WALL})
        msp.add_lwpolyline(rect(0, 0, 4000, 3000), close=True, dxfattribs={"layer": "AI_STORE"})
        fourth(msp)
        return dr.boundary_evidence_check(list(msp), dr.MM_UNITS, self.PLATE)

    def test_a_side_on_nothing_is_marked(self):
        summ, items = self.check(lambda msp: None)
        self.assertEqual(len(items), 1)
        self.assertEqual(summ["rooms_off"], 1)
        band = (dr.EVIDENCE_TOL_MM + dr.EVIDENCE_WALL_FINISH_MM) / 1000.0    # a wall's finish, at each end
        self.assertAlmostEqual(summ["no_evidence_m"], 3.0 - 2 * band, delta=0.1)
        self.assertIn("AI_STORE", items[0]["why"])

    def test_a_primary_door_line_is_evidence(self):
        summ, items = self.check(lambda msp: msp.add_lwpolyline([(4000, 0), (4000, 3000)],
                                                                dxfattribs={"layer": LN.DOOR_PRIMARY}))
        self.assertEqual(items, [])
        self.assertEqual(summ["no_evidence_m"], 0.0)

    def test_a_secondary_door_line_is_not(self):
        summ, items = self.check(lambda msp: msp.add_lwpolyline([(4000, 0), (4000, 3000)],
                                                                dxfattribs={"layer": LN.DOOR_SECONDARY}))
        self.assertEqual(len(items), 1)

    def test_a_wall_and_a_short_opening_line(self):
        """A wall with a 900 mm opening: the line across it is an opening
        line (the door definition's), not marked."""
        def fourth(msp):
            msp.add_lwpolyline(rect(4000, 0, 4200, 1000), close=True, dxfattribs={"layer": LN.WALL})
            msp.add_lwpolyline(rect(4000, 1900, 4200, 3000), close=True, dxfattribs={"layer": LN.WALL})
        summ, items = self.check(fourth)
        self.assertEqual(items, [])
        band = (dr.EVIDENCE_TOL_MM + dr.EVIDENCE_WALL_FINISH_MM) / 1000.0
        self.assertAlmostEqual(summ["opening_m"], 0.9 - 2 * band, delta=0.1)

    def test_another_room_and_a_column_are_evidence(self):
        def fourth(msp):
            msp.add_lwpolyline(rect(4000, 0, 6000, 2000), close=True, dxfattribs={"layer": "AI_SHAFT"})
            msp.add_lwpolyline(rect(4000, 2000, 4600, 3000), close=True, dxfattribs={"layer": LN.COLUMN})
        summ, items = self.check(fourth)
        self.assertEqual([it for it in items if "AI_STORE" in it["why"]], [])


class PlainLineFloor(unittest.TestCase):
    """A STORE 4 x 3 m walled on three sides; across its open side only a
    plain line on a pattern layer. A lift lobby 3 m wide between two banks
    of two X lifts, walled at its west end, open to the floor at its east
    end."""

    @classmethod
    def run_floor(cls, overrides=None):
        tmp = tempfile.mkdtemp()
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        plate = rect(0, 0, 40000, 30000)
        msp.add_lwpolyline(plate, close=True, dxfattribs={"layer": "A-FLOOR"})

        def r(x0, y0, x1, y1, layer="A-WALL"):
            msp.add_lwpolyline(rect(x0, y0, x1, y1), close=True, dxfattribs={"layer": layer})
        # the STORE: 5000..9000 x 5000..8000, open on its east side
        r(4800, 4800, 9000, 5000)
        r(4800, 8000, 9000, 8200)
        r(4800, 5000, 5000, 8000)
        msp.add_line((9000, 5000), (9000, 8000), dxfattribs={"layer": "A-PATTERN"})
        msp.add_mtext("STORE", dxfattribs={"insert": (7000, 6500), "char_height": 200, "layer": "A-TEXT"})

        # the lift lobby: 20000..25600 x 15000..18000; lifts 2500 x 2500 in
        # 200 mm walls above (18200..20700) and below (12300..14800), each
        # with a 1100 mm entrance onto the lobby; walled at x = 19800..20000
        def lift(x0, y0, up):
            x1, y1 = x0 + 2500, y0 + 2500
            msp.add_line((x0, y0), (x1, y1), dxfattribs={"layer": "A-LIFT"})
            msp.add_line((x0, y1), (x1, y0), dxfattribs={"layer": "A-LIFT"})
            msp.add_lwpolyline(rect(x0, y0, x1, y1), close=True, dxfattribs={"layer": "A-LIFT"})
            msp.add_mtext("LIFT", dxfattribs={"insert": (x0 + 1250, y0 + 1250), "char_height": 150,
                                              "layer": "A-TEXT"})
            fy = y0 - 200 if up else y1                       # the front, onto the lobby
            r(x0 - 200, fy, x0 + 700, fy + 200)
            r(x0 + 1800, fy, x1 + 200, fy + 200)
            by = y1 if up else y0 - 200                       # the back
            r(x0 - 200, by, x1 + 200, by + 200)
            r(x0 - 200, y0, x0, y1)
            r(x1, y0, x1 + 200, y1)
        for k in range(2):
            lift(20200 + 2900 * k, 18200, True)
            lift(20200 + 2900 * k, 12300, False)
        r(19800, 14800, 20000, 18200)                         # the lobby's west wall
        msp.add_mtext("LIFT LOBBY", dxfattribs={"insert": (22800, 16500), "char_height": 200,
                                                "layer": "A-TEXT"})
        src = os.path.join(tmp, "in.dxf")
        doc.saveas(src)
        floors = [{"name": "F1", "polygon": plate}]
        rows = dr.extract_texts(src, floors)
        out = os.path.join(tmp, "out.dxf")
        stats = dr.annotate_dxf(src, out, floors, rows, rule_overrides=overrides)
        areas = dr.compute_room_areas(out, "AI_FLOOR")
        return stats, areas, rows

    @classmethod
    def setUpClass(cls):
        cls.stats, cls.areas, cls.rows = cls.run_floor()
        cls.stats_off, cls.areas_off, _r = cls.run_floor({"boundary_on_evidence": False,
                                                          "lift_lobby_to_lift_bank": False})

    @staticmethod
    def status(stats, rows, word):
        sr = next(r["sr_no"] for r in rows["F1"] if " ".join(str(r["text"]).split()) == word)
        return stats["text_status_by_floor"]["F1"].get(sr)

    def names(self, areas):
        return [" ".join(str(r["name"]).split()) for r in areas["rooms"]]

    def test_a_plain_line_closes_no_room(self):
        self.assertNotIn("STORE", self.names(self.areas))
        self.assertEqual(self.status(self.stats, self.rows, "STORE"), "no_evidence")
        self.assertGreaterEqual(self.stats.get("no_evidence_boundaries", 0), 1)

    def test_with_the_rule_off_the_plain_line_closes_it(self):
        store = [r for r in self.areas_off["rooms"] if " ".join(str(r["name"]).split()) == "STORE"]
        self.assertEqual(len(store), 1)
        self.assertAlmostEqual(store[0]["area_sqm"], 12.0, delta=0.5)

    def test_the_open_lift_lobby_ends_at_its_bank(self):
        lob = [r for r in self.areas["rooms"] if " ".join(str(r["name"]).split()) == "LIFT LOBBY"]
        self.assertEqual(len(lob), 1, self.names(self.areas))
        # 20000 .. the bank's east limit (the lifts' east walls, 25600): 5.6 x 3.0,
        # and the lift entrances' recesses
        self.assertAlmostEqual(lob[0]["area_sqm"], 5.6 * 3.0, delta=1.5)
        self.assertGreaterEqual(self.stats.get("lift_lobby_closed_at_bank", 0), 1)

    def test_the_lifts_keep_their_doors(self):
        lifts = [r for r in self.areas["rooms"] if r.get("family") == "Lift"]
        self.assertEqual(len(lifts), 4, self.names(self.areas))
        self.assertGreaterEqual(self.stats.get("doors_lift_entrances", 0), 4)

    def test_every_side_drawn_stands_on_evidence(self):
        ev = self.stats["boundary_evidence_by_floor"]["F1"]
        self.assertGreater(ev["checked_m"], 0)
        self.assertEqual(self.stats.get("boundary_evidence_review", []), [])


if __name__ == "__main__":
    unittest.main()
