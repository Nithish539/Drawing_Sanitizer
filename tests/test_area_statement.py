# -*- coding: utf-8 -*-
"""Area Statement: column allocation, floor titles, towers and the workbook.

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from openpyxl import load_workbook  # noqa: E402

from Drawing_Analyzer.area_statement import (build_statement,  # noqa: E402
                                             classify, level_name,
                                             write_area_statement)
from Drawing_Analyzer.levels import (floor_name, parse_levels,  # noqa: E402
                                     tower_from_name, tower_of)


class ClassifyTests(unittest.TestCase):
    """Names as they come off the A-106..A-114 and T1 drawings."""

    CASES = [
        ("GENTS TOILET", "Toilet", "toilets"),
        ("LADIES TOILET", "Toilet", "toilets"),
        ("Janit.", "Unknown", "toilets"),
        ("PCT", "Unknown", "toilets"),
        ("Toilet Exhaust", "Toilet", "other_shaft"),
        ("TOI.EX.SHAFT", "Shaft", "other_shaft"),
        ("P.Lift LOBBY {HIGH RISE}", "Lift", "lobby"),
        ("LOBBY PREZZ.", "Circulation", "lobby_prez"),
        ("Lobby Press. shaft", "Shaft", "lobby_prez"),
        ("LIFT PRE", "Lift", "lobby_prez"),
        ("STAIR PREZ", "Staircase", "stair_press"),
        ("STAIR PR", "Staircase", "stair_press"),
        ("STAIRCASE PRE.SHAFT", "Staircase", "stair_press"),
        ("PRESS. SHAFT", "Shaft", "stair_press"),
        ("Press.Sh.", "MEP / Services", "stair_press"),
        ("shaft Press.", "Shaft", "stair_press"),
        ("PRESS", "Unknown", "stair_press"),
        ("AHU ROOM", "MEP / Services", "ahu_room"),
        ("AHU PIPE SHAFT", "Shaft", "ahu_shaft"),
        ("CHW,TFA & RWPs SHAFT", "Shaft", "ahu_shaft"),
        ("ELECTRICAL ROOM", "Electrical", "ele_room"),
        ("Elec.", "Electrical", "elect_shaft"),
        ("ELE SHAFT", "Shaft", "elect_shaft"),
        ("BALCONY 100MM SUNK", "Balcony / Open", "balcony"),
        ("REFUGE AREA-1", "Unknown", "refuge"),
        ("STAIRCASE", "Staircase", "staircase"),
        ("ST-01", "Staircase", "staircase"),
        ("FIRE TOWER-1", "Fire / Life Safety", "staircase"),
        ("P.LIFT/FIRE LIFT-12", "Lift", "lift"),
        ("24 PAX P.LIFT 2500 X 2500", "Lift", "lift"),
        ("DG EXHAUST", "Shaft", "dg"),
        ("PHE SHAFT", "Shaft", "phe"),
        ("RWP", "Unknown", "phe"),
        ("STP EXHAUST", "Plumbing", "stp"),
        ("COMM.", "MEP / Services", "comm"),
        ("FAPA", "Unknown", "fapa"),
        ("TEN", "Unknown", "ten"),
        ("FHR-3", "Fire / Life Safety", "other_shaft"),
        ("Unknown Shaft", "Shaft", "other_shaft"),
        ("Pantry", "Storage", None),
        ("OFFICE AREA", "Habitable Room", None),
    ]

    def test_names_file_into_the_reference_columns(self):
        for name, family, want in self.CASES:
            with self.subTest(name=name):
                self.assertEqual(classify(name, family), want)


class LevelTests(unittest.TestCase):
    """Plan titles as written under the T1 plates."""

    def labels(self, title):
        return [lv["label"] for lv in parse_levels(title)]

    def test_titles(self):
        self.assertEqual(self.labels("%%U1ST FLOOR PLAN"), ["1st Floor"])
        self.assertEqual(self.labels("%%UTYPICAL FLOOR PLAN-(2nd,4th,6th & 10th)"),
                         ["2nd Floor", "4th Floor", "6th Floor", "10th Floor"])
        self.assertEqual(self.labels("TYPICAL FLOOR PLAN 5TH TO 9TH"),
                         ["5th Floor", "6th Floor", "7th Floor", "8th Floor",
                          "9th Floor"])
        self.assertEqual(self.labels("SECOND & THIRD FLOOR PLAN"),
                         ["2nd Floor", "3rd Floor"])
        self.assertEqual(self.labels("%%UTERRACE FLOOR"), ["Terrace Floor"])
        self.assertEqual(self.labels("GROUND FLOOR PLAN"), ["Ground Floor"])
        self.assertEqual(self.labels("BASEMENT-2 PLAN"), ["Basement 2"])
        self.assertEqual(self.labels("TERRACE BELOW"), [])
        self.assertEqual(self.labels("STRUCTURAL GLAZING TYPE-1"), [])
        self.assertTrue(parse_levels("%%UTYPICAL REFUGE FLOOR PLAN- 5th")[0]["refuge"])

    def test_towers(self):
        self.assertEqual(tower_of("%%UTOWER-1"), "TOWER 1")
        self.assertIsNone(tower_of("SCHEDULE OF OPENINGS(TOWERS)"))
        self.assertEqual(tower_from_name("T1_TYPICAL FLOOR PLANS.dxf"), "TOWER 1")
        self.assertIsNone(tower_from_name("A-109 7th FLOOR.dxf"))

    def test_level_from_the_file_name(self):
        self.assertEqual(level_name("A-109 7th FLOOR (OFFICE FLOOR 04).dxf"),
                         "7th FLOOR")


def _room(name, family, area, floor):
    return {"name": name, "family": family, "area_sqm": area, "floor": floor}


class FloorNameTests(unittest.TestCase):
    """Step 2 names each plate after its plan title."""

    CASES = [
        ("1ST FLOOR PLAN", "1st Floor"),
        ("TYPICAL FLOOR PLAN-(2nd,4th,6th & 10th)",
         "Typical 2nd, 4th, 6th & 10th Floor"),
        ("TYPICAL FLOOR PLAN-(13th,14th,15th & 17th )",
         "Typical 13th, 14th, 15th & 17th Floor"),
        ("TYPICAL FLOOR PLAN 5TH TO 9TH", "Typical 5th to 9th Floor"),
        ("TYPICAL REFUGE FLOOR PLAN- 5th", "Typical 5th Floor (Refuge)"),
        ("12TH REFUGE FLOOR PLAN", "12th Floor (Refuge)"),
        ("SECOND & THIRD FLOOR PLAN", "Typical 2nd & 3rd Floor"),
        ("TERRACE FLOOR", "Terrace Floor"),
        ("UPPER TERRACE FLOOR", "Upper Terrace Floor"),
        ("6th FLOOR PLAN (REFUGE / OFFICE - WING-B) SCALE : 1:150",
         "6th Floor (Refuge) - Wing B"),
        ("A-106 4th FLOOR PLAN(OFFICE FLOOR 01)", "4th Floor"),
        ("GENERAL NOTES", ""),
    ]

    def test_titles(self):
        for title, want in self.CASES:
            with self.subTest(title=title):
                self.assertEqual(floor_name(title), want)


class StatementTests(unittest.TestCase):

    def area(self):
        return {
            "floors": [
                {"name": "P1", "area_sqm": 1000.0, "tower": "TOWER 1",
                 "title": "1ST FLOOR PLAN",
                 "levels": parse_levels("1ST FLOOR PLAN")},
                {"name": "P2", "area_sqm": 1000.0, "tower": "TOWER 1",
                 "title": "TYPICAL FLOOR PLAN-(2nd,4th & 3rd)",
                 "levels": parse_levels("TYPICAL FLOOR PLAN-(2nd,4th & 3rd)")},
                {"name": "P3", "area_sqm": 800.0, "tower": "TOWER 2",
                 "title": "1ST FLOOR PLAN",
                 "levels": parse_levels("1ST FLOOR PLAN")},
            ],
            "rooms": [
                _room("GENTS TOILET", "Toilet", 50.0, "P1"),
                _room("STAIRCASE", "Staircase", 20.0, "P1"),
                _room("P.LIFT-1", "Lift", 10.0, "P1"),
                _room("PHE SHAFT", "Shaft", 4.0, "P1"),
                _room("LADIES TOILET", "Toilet", 45.0, "P2"),
                _room("STAIRCASE", "Staircase", 20.0, "P2"),
                _room("LADIES TOILET", "Toilet", 40.0, "P3"),
            ],
        }

    def test_floor_by_floor_per_tower(self):
        data = build_statement(self.area(), "T1 plans.dxf")
        self.assertEqual([t["title"] for t in data["towers"]],
                         ["TOWER 1", "TOWER 2"])
        t1 = data["towers"][0]["rows"]
        # The typical plate is repeated for each floor it names, in order.
        self.assertEqual([r["level"] for r in t1],
                         ["1st Floor", "2nd Floor", "3rd Floor", "4th Floor"])
        self.assertEqual([r["values"]["toilets"] for r in t1],
                         [50.0, 45.0, 45.0, 45.0])

    def test_same_floor_twice_without_a_tower_is_two_towers(self):
        area = self.area()
        for f in area["floors"]:
            f["tower"] = None
        data = build_statement(area, "plans.dxf")
        self.assertEqual([t["title"] for t in data["towers"]],
                         ["TOWER 1", "TOWER 2"])

    def test_workbook(self):
        out = os.path.join(tempfile.mkdtemp(), "st.xlsx")
        data = write_area_statement(self.area(), out, "T1 plans.dxf",
                                    project_name="Phoenix - 14")
        ws = load_workbook(out)["Area Statement"]
        self.assertEqual(ws["B2"].value, "Project Name: Phoenix - 14")
        cells = [c for row in ws.iter_rows() for c in row if c.value]
        text = [str(c.value) for c in cells]
        self.assertIn("TOWER 1", text)
        self.assertIn("TOWER 2", text)
        self.assertEqual(text.count("Area Details"), 2)
        # Unused columns are left out.
        headers = data["columns"]
        for gone in ("DG EXHAUST SHAFT", "Elevation Area", "UNIT",
                     "REFUGE (As per Norms)", "BMS"):
            self.assertNotIn(gone, headers)
        for kept in ("TOILETS", "STAIRCASE", "LIFT SHAFT", "PHE LINE SHAFT",
                     "Net BUA", "PLINTH AREA"):
            self.assertIn(kept, headers)
        # Formulas point at the columns that are there.
        net = next(c for c in cells if isinstance(c.value, str)
                   and c.value.startswith("=") and c.column_letter ==
                   _col(headers, "Net BUA") and c.row > 8)
        gross_col = _col(headers, "GROSS AREA")
        self.assertTrue(net.value.startswith("=" + gross_col))
        self.assertIn("Room allocation", load_workbook(out).sheetnames)


def _col(headers, name):
    from openpyxl.utils import get_column_letter
    return get_column_letter(2 + headers.index(name))


if __name__ == "__main__":
    unittest.main()
