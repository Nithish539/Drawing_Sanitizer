# -*- coding: utf-8 -*-
"""The H10 Tower 2 manual audit (2026-10-12): what each point needed.

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from shapely.geometry import Polygon, box  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import rules  # noqa: E402
from Drawing_Analyzer.area_statement import classify  # noqa: E402


class ShaftNamesTests(unittest.TestCase):
    """Point 7: the shaft labels of H10 T2 and where the statement files them."""

    def fam(self, text):
        return dr.family_for_label(text, dr._FAMILY_RULES)["name"]

    def test_electrical_shaft_code(self):
        self.assertEqual(self.fam("EL-ELE-03 1200X1175"), "Shaft")
        self.assertTrue(dr.shaft_like_label("EL-ELE-03 1200X1175", "Shaft"))
        self.assertEqual(classify("EL-ELE-03 1200X1175", "Shaft", opening=True), "elect_shaft")

    def test_plumbing_shaft_code(self):
        self.assertEqual(classify("PL-SH-04 550X900", "Shaft", opening=True), "phe")

    def test_shaft_for_toilet_is_toilet_exhaust_not_toilets(self):
        self.assertEqual(classify("SHAFT FOR TOILET 1200X1175", "Shaft", opening=True), "toilet_exhaust")

    def test_m_room_is_machine_room(self):
        self.assertEqual(self.fam("M. ROOM 3000X4150"), "Machine Room")
        self.assertEqual(classify("M. ROOM 3000X4150", "Machine Room"), "machine_room")

    def test_rooms_not_caught_by_the_new_codes(self):
        self.assertEqual(self.fam("ELEC. ROOM 2400X2500"), "Electrical")
        self.assertEqual(classify("ELEC. ROOM 2400X2500", "Electrical", opening=False), "ele_room")

    def test_trap_door_label(self):
        self.assertTrue(dr.TRAP_DOOR_RE.search("TRAP DOOR"))
        self.assertTrue(dr.TRAP_DOOR_RE.search("trapdoor"))
        self.assertFalse(dr.TRAP_DOOR_RE.search("DOOR"))


class OutlineSliverTests(unittest.TestCase):
    """Points 2-4: a toilet outline along a partition stub, up a wall slot."""

    def test_partition_notch_filled(self):
        # a 95 mm partition stub 1800 deep from the left wall
        poly = [(0, 0), (5000, 0), (5000, 7000), (0, 7000), (0, 4095), (1800, 4095), (1800, 4000), (0, 4000)]
        np_, _nh, changed = dr.clean_outline(poly, [])
        self.assertTrue(changed)
        self.assertTrue(Polygon(np_).buffer(1).contains(box(100, 4010, 1700, 4085)))

    def test_wall_slot_cut(self):
        # a 200 mm slot 800 long up beside the next room
        poly = [(0, 0), (5000, 0), (5000, 7000), (2600, 7000), (2600, 7800), (2400, 7800), (2400, 7000), (0, 7000)]
        np_, _nh, changed = dr.clean_outline(poly, [])
        self.assertTrue(changed)
        self.assertLessEqual(max(p[1] for p in np_), 7001)

    def test_square_alcove_kept(self):
        # a 900 x 900 alcove is part of the room
        poly = [(0, 0), (5000, 0), (5000, 7000), (2900, 7000), (2900, 7900), (2000, 7900), (2000, 7000), (0, 7000)]
        self.assertFalse(dr.clean_outline(poly, [])[2])


class DoorFrameJambTests(unittest.TestCase):
    """The ladies' toilet door whose far jamb is only a frame."""

    def test_frame_at_leaf_width_closes(self):
        grid = dr._LineGrid([(1050, -300, 1050, 300)])
        t = dr._door_frame_hit(0, 0, 1, 0, [(0, 0, 1000)], grid)
        self.assertAlmostEqual(t, 1050, delta=1)

    def test_no_hinge_no_closing(self):
        grid = dr._LineGrid([(1050, -300, 1050, 300)])
        self.assertIsNone(dr._door_frame_hit(0, 0, 1, 0, [], grid))

    def test_line_far_from_leaf_width_ignored(self):
        grid = dr._LineGrid([(1900, -300, 1900, 300)])
        self.assertIsNone(dr._door_frame_hit(0, 0, 1, 0, [(0, 0, 1000)], grid))

    def test_jamb_end_on_the_face_line(self):
        # the jamb ends exactly on the ray's line: one of the offset rays hits
        grid = dr._LineGrid([(1100, 0.5, 1100, 200)])
        self.assertIsNotNone(dr._door_frame_hit(0, 0, 1, 0, [(0, 0, 1025)], grid))


class FloorEdgeInBlockTests(unittest.TestCase):
    """Point 1 of the extra findings: the 5th floor's slab edge as four LINEs in a block."""

    def test_lines_in_a_block_make_a_plate(self):
        import ezdxf
        doc = ezdxf.new()
        blk = doc.blocks.new("FLOOR5")
        for a, b in (((0, 0), (81900, 0)), ((81900, 0), (81900, 59900)),
                     ((81900, 59900), (0, 59900)), ((0, 59900), (0, 0))):
            blk.add_line(a, b, dxfattribs={"layer": "A3-WL_INT"})
        msp = doc.modelspace()
        msp.add_blockref("FLOOR5", (1000, 2000))
        found = dr.reconstruct_floor_outlines(msp, dr.MM_UNITS, 1000, 8000, doc=doc)
        areas = [round(Polygon(p).area / 1e6, 2) for p, _l, _k in found]
        self.assertIn(4905.81, areas)
        self.assertEqual(dr.reconstruct_floor_outlines(msp, dr.MM_UNITS, 1000, 8000), [])


class SpacedTagTests(unittest.TestCase):
    """Found in the full run: BHUB's FMD 4 (FMD-4 written with a space) on
    the tag layer titled a 39 sq.m room once the doors closed round it."""

    def test_spaced_code_on_a_tag_layer_is_a_tag(self):
        self.assertTrue(dr._spaced_tag_on_tag_layer("FMD 4", "A3-Tag"))
        self.assertTrue(dr._spaced_tag_on_tag_layer("SD 3A", "DOOR TAG"))

    def test_room_names_untouched(self):
        self.assertFalse(dr._spaced_tag_on_tag_layer("FMD 4", "A-AREA-IDEN"))   # not a tag layer
        self.assertFalse(dr._spaced_tag_on_tag_layer("LIFT 1", "A3-Tag"))       # a family keyword
        self.assertFalse(dr._spaced_tag_on_tag_layer("AHU 2", "A3-Tag"))
        self.assertFalse(dr._spaced_tag_on_tag_layer("FMD-4", "A3-Tag"))        # no space: the usual test
        self.assertFalse(dr._spaced_tag_on_tag_layer("FMD 4", "STAGE"))         # TAG inside a word


class AuditRulesTests(unittest.TestCase):

    def test_rules_on_by_default(self):
        d = rules.defaults()
        for k in ("x_band_trim", "door_frame_jamb", "fit_keeps_wide_room"):
            self.assertTrue(d.get(k), k)
        # an X-marked trap door is an X like any other: the X sequence
        # decides it (2026-10-16) - no switch takes it out of the linework
        self.assertNotIn("trap_door_in_room", d)

    def test_mhvac_never_a_wall(self):
        self.assertIn("M-HVAC", [k.upper() for k in dr._HARD_EXCLUDE_KEYWORDS])


if __name__ == "__main__":
    unittest.main()
