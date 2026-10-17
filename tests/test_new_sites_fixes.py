# -*- coding: utf-8 -*-
"""Fixes found on H10 Towers 1-3 and 285 FD Tower 2.

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from Drawing_Analyzer.levels import parse_levels  # noqa: E402
from Drawing_Analyzer.dxf_reader import is_ignorable_label, is_note_text  # noqa: E402
from Drawing_Analyzer import facade  # noqa: E402


class LevelTests(unittest.TestCase):
    def test_ordinal_run_into_word(self):
        keys = [l["key"] for l in parse_levels("TYPICAL 11TH, 12TH, 14TH & 15THFLOOR PLAN")]
        self.assertEqual(keys, [11, 12, 14, 15])

    def test_numbers_before_the_floor_word(self):
        # XA tower file names
        cases = {
            "1,3,5,7,9,11,13,15,17,19 TYPICAL FLOORS (low zone)": [1, 3, 5, 7, 9, 11, 13, 15, 17, 19],
            "30,32,34,36,38  FLOORS(MID ZONE)": [30, 32, 34, 36, 38],
            "29 SERVICE FLOOR": [29],
            "41 FLOOR-AMENITY": [41],
            "REFUGE 23,27": [23, 27],
            "REFUGE 42,46,50,54 (TYPICALFLOORS": [42, 46, 50, 54],
            "58 FLOOR (TERRACE) SKY LOUNGE": [58],
            "GROUND FLOOR MEZANINE": [0.7],
            "GROUND FLOOR PLAN- MEZANINE": [0.7],
            "GROUND & MEZZANINE FLOOR PLAN": [0, 0.7],
            "2 X 3 M FLOOR": [],
        }
        for title, want in cases.items():
            with self.subTest(title=title):
                self.assertEqual([l["key"] for l in parse_levels(title)], want)
        self.assertTrue(all(l["refuge"] for l in parse_levels("REFUGE 23,27")))

    def test_date_in_a_file_name_is_not_a_floor(self):
        self.assertEqual([l["key"] for l in parse_levels("TOWER_2_GROUND_AND_AMENITY_FLOOR_PLAN-_06_05_2022_R2")], [0])
        self.assertEqual([l["key"] for l in parse_levels("TOWER 2 GROUND AND AMENITY FLOOR PLAN- 06 05 2022 R2")], [0])


class LabelTests(unittest.TestCase):
    def test_stair_spec_is_not_a_room(self):
        self.assertTrue(is_ignorable_label("W-1500\nT-300\nR-150"))
        self.assertFalse(is_ignorable_label("WC"))

    def test_lift_vendor_note(self):
        self.assertTrue(is_note_text("2nd,4th,6th floor level\nlift openings to be closed\n(as per lift vendor drawing)"))
        self.assertFalse(is_note_text("LOWER ZONE\n4000 WD LIFT LOBBY"))
        self.assertFalse(is_note_text("AHU ROOM-01 (TYPICAL FLOOR)"))

    def test_size_right_after_a_full_stop(self):
        from Drawing_Analyzer.labels import declared_size
        self.assertEqual(declared_size("LOBBY PRESS.1200X1000\n(M-HVAC-SH-04)"), (1200.0, 1000.0))
        self.assertIsNone(declared_size("1.5X300"))

    def test_terrace_copy_number_is_not_a_floor(self):
        from Drawing_Analyzer.dxf_reader import plate_levels
        self.assertNotIn(2, [l["key"] for l in plate_levels("TERRACE FLOOR PLAN (2)")])


class FacadeTests(unittest.TestCase):
    """Gross to the glass: a 20 x 10 m slab with the glass 500 mm in."""

    def test_glass_inset(self):
        slab = [(0, 0), (20000, 0), (20000, 10000), (0, 10000)]
        g = [(500, 500), (19500, 500), (19500, 9500), (500, 9500), (500, 500)]
        segs = [(a[0], a[1], b[0], b[1]) for a, b in zip(g, g[1:])]
        r = facade.glass_face_area(slab, segs)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["area_sqm"], 19.0 * 9.0, delta=0.6)

    def test_glass_at_slab_edge(self):
        slab = [(0, 0), (20000, 0), (20000, 10000), (0, 10000)]
        segs = [(a[0], a[1], b[0], b[1]) for a, b in zip(slab + [slab[0]], slab[1:] + [slab[0]])]
        r = facade.glass_face_area(slab, segs)
        self.assertAlmostEqual(r["area_sqm"], 200.0, delta=0.01)

    def test_open_facade_gives_no_answer(self):
        slab = [(0, 0), (20000, 0), (20000, 10000), (0, 10000)]
        r = facade.glass_face_area(slab, [(500, 500, 19500, 500)])
        self.assertIsNone(r)

    def test_slab_layers(self):
        self.assertTrue(facade.is_slab_layer("A-FLOOR PROJ"))
        self.assertFalse(facade.is_slab_layer("1.1 SALEABLE-GROSS AREA (inc glass)"))


class PlinthLessTests(unittest.TestCase):
    """A project that also takes refuge off the plinth (H10 Tower 3)."""

    def test_refuge_taken_off(self):
        from Drawing_Analyzer.area_statement import _template, derive
        st = {"template": "t3", "templates": {"t3": {"columns": [], "plinth_less": ["refuge"]}}}
        vals = {"staircase": 107.3, "lift_shaft": 56.18, "refuge": 213.1, "dg": 32.74}
        tpl = _template(st)
        d = derive(vals, 3537.07, tpl, 0.70, 10.764)
        self.assertAlmostEqual(d["plinth"], 3537.07 - 32.74 - 107.3 - 56.18 - 213.1, places=2)
        d0 = derive(vals, 3537.07, _template({"template": "default", "templates": {}}), 0.70, 10.764)
        self.assertAlmostEqual(d0["plinth"] - d["plinth"], 213.1, places=2)


class XaTowerTests(unittest.TestCase):
    """A different architect's drawings (XA tower, metres)."""

    def test_metre_sizes(self):
        from Drawing_Analyzer.labels import declared_size
        self.assertEqual(declared_size("PASSENGER LIFT\n2.6M X 2.9M"), (2600.0, 2900.0))
        self.assertEqual(declared_size("LIFT LOBBY\n3.3MX7.4M"), (3300.0, 7400.0))
        self.assertEqual(declared_size("JANITOR\n2.2Mx1.08M"), (2200.0, 1080.0))
        self.assertIsNone(declared_size("2X3"))
        self.assertIsNone(declared_size("1.5X300"))

    def test_stated_area(self):
        from Drawing_Analyzer.labels import declared_area
        self.assertEqual(declared_area("REFUGE AREA\n151.05 SQ. MTR."), 151.05)
        self.assertEqual(declared_area("OFFICE SPACE\n843.61 SQ.MTS"), 843.61)
        self.assertIsNone(declared_area("AHU 6000X5500"))
        self.assertIsNone(declared_area("FFL.+623.950"))

    def test_statement_carries_the_stated_area(self):
        from Drawing_Analyzer.area_statement import build_statement
        ad = {"floors": [{"name": "F", "area_sqm": 5000.0, "levels": [{"key": 13, "label": "13th Floor", "refuge": True}]}],
              "rooms": [{"name": "REFUGE AREA\n151.05 SQ. MTR.", "family": "Balcony / Open", "area_sqm": 213.08,
                         "floor": "F", "source": "P", "opening": False}]}
        st = build_statement(ad, "x.dxf")
        self.assertAlmostEqual(st["towers"][0]["rows"][0]["canon"]["refuge"], 151.05, places=2)

    def test_shaft_abbreviations(self):
        from Drawing_Analyzer.area_statement import classify
        self.assertEqual(classify("LLP \nSH", "Shaft", opening=True), "lobby_press")
        self.assertEqual(classify("STR \nPRE", "Shaft", opening=True), "stair_press")
        self.assertEqual(classify("ELE \nSH (FRM)", "Shaft", opening=True), "elect_shaft")
        self.assertEqual(classify("MACHINE ROOM \n2.6M X 2.9M", "Machine Room", opening=True), "machine_room")

    def test_lift_family_reads_metre_label(self):
        from Drawing_Analyzer.dxf_reader import family_for_label, _FAMILY_RULES, _exact_size_err
        self.assertEqual(family_for_label("PASSENGER LIFT \n2.6M X 2.9M", _FAMILY_RULES)["name"], "Lift")
        self.assertEqual(_exact_size_err((2600.0, 2900.0), 2600.0, 2900.0), 0)

    def test_lift_zone_tag_is_not_a_room(self):
        self.assertTrue(is_ignorable_label("LOW ZONE (A)"))
        self.assertTrue(is_ignorable_label("HIGH ZONE (C)"))
        self.assertFalse(is_ignorable_label("HIGH ZONE (C) PASSENGER LIFT"))

    def test_shaft_names_and_strips(self):
        from Drawing_Analyzer.dxf_reader import _is_shaft_name, _long_strip
        self.assertTrue(_is_shaft_name("PHE\nSH"))
        self.assertFalse(_is_shaft_name("LIFT LOBBY"))
        strip = ((0, 0, 14500, 1600), [(0, 0), (14500, 0), (14500, 1600), (0, 1600)], [])
        self.assertTrue(_long_strip(strip))
        self.assertFalse(_long_strip(((0, 0, 3000, 1200), [(0, 0), (3000, 0), (3000, 1200), (0, 1200)], [])))


class PlatePickTests(unittest.TestCase):
    """The gross outline round its own floor's inner outlines is not a sheet border."""

    def _cands(self, rects):
        import tempfile, ezdxf
        from Drawing_Analyzer.dxf_reader import get_floor_candidates
        doc = ezdxf.new(); doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        for (x0, y0, x1, y1), layer in rects:
            msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": layer})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.dxf"); doc.saveas(path)
            cands, _u, _diag = get_floor_candidates(path, None, {}, min_area_sqm=100, max_area_sqm=100000)
        return sorted(((round(c.area_sqm), c.encloses) for c in cands), reverse=True)

    def test_same_floor_outlines(self):
        got = self._cands([((0, 0, 50000, 49600), "GROSS"), ((500, 500, 49000, 48000), "A3-WALL")])
        self.assertEqual([e for _a, e in got], [0, 0])

    def test_sheet_border_round_two_floors(self):
        got = self._cands([((0, 0, 120000, 60000), "BORDER"),
                           ((5000, 5000, 50000, 50000), "PLATE"), ((60000, 5000, 105000, 50000), "PLATE")])
        self.assertEqual(got[0][1], 2)


if __name__ == "__main__":
    unittest.main()
