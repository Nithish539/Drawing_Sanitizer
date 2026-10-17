# -*- coding: utf-8 -*-
"""Step 2: plate titles written as MTEXT or as TEXT, one or several lines.

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


def _plates(build):
    """Two plates side by side, titles under them from build(msp)."""
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for x0 in (0.0, 100000.0):
        msp.add_lwpolyline([(x0, 0), (x0 + W, 0), (x0 + W, H), (x0, H)],
                           close=True, dxfattribs={"layer": "FLOOR"})
    build(msp)
    path = os.path.join(tempfile.mkdtemp(), "plan.dxf")
    doc.saveas(path)
    cands, _units, _diag = dr.get_floor_candidates(path, min_area_sqm=2000,
                                                   max_area_sqm=4000)
    return sorted(cands, key=lambda c: c.polygon[0][0] if c.polygon else 0)


def _text(msp, s, x, y, h=600):
    msp.add_text(s, dxfattribs={"height": h, "insert": (x, y)})


class FloorTitleTests(unittest.TestCase):

    def test_mtext_and_text_titles(self):
        def build(msp):
            msp.add_mtext("LOWER ZONE TYPICAL FLOOR PLAN\\P(8th, 12th & 16th FLOORS)",
                          dxfattribs={"char_height": 600, "insert": (20000, -3000)})
            _text(msp, "%%U5TH FLOOR PLAN", 120000, -3000)
        a, b = _plates(build)
        self.assertEqual(a.name, "Typical 8th, 12th & 16th Floor")
        self.assertEqual(a.title_source, "MTEXT")
        self.assertEqual(b.name, "5th Floor")
        self.assertEqual(b.title_source, "TEXT")

    def test_title_in_two_text_lines(self):
        # The same title as two TEXT entities: neither line is a floor
        # title on its own.
        def build(msp):
            _text(msp, "%%ULOWER ZONE TYPICAL FLOOR PLAN", 20000, -3000)
            _text(msp, "%%U(2ND, 3RD & 5th,6th, 7th FLOORS)", 20900, -4010)
            _text(msp, "%%U4TH FLOOR PLAN", 120000, -3000)
        a, b = _plates(build)
        self.assertEqual(a.name, "Typical 2nd, 3rd, 5th, 6th & 7th Floor")
        self.assertEqual(a.title_source, "TEXT (2 lines)")
        self.assertEqual([lv["label"] for lv in a.levels],
                         ["2nd Floor", "3rd Floor", "5th Floor", "6th Floor", "7th Floor"])
        self.assertEqual(b.name, "4th Floor")

    def test_small_note_is_not_joined(self):
        # A note under the title, in small text, is not part of it.
        def build(msp):
            _text(msp, "1ST FLOOR PLAN", 20000, -3000)
            _text(msp, "SLEEVE AT 3rd & 7th FLOOR", 20000, -3500, h=200)
        a, _b = _plates(build)
        self.assertEqual(a.name, "1st Floor")
        self.assertEqual(a.title_source, "TEXT")

    def test_next_plan_title_is_not_joined(self):
        # A title of its own just below is another plan, not a second line.
        def build(msp):
            _text(msp, "1ST FLOOR PLAN", 20000, -3000)
            _text(msp, "2ND FLOOR PLAN", 20000, -4000)
        a, _b = _plates(build)
        self.assertEqual(a.name, "1st Floor")


class FloorRangeTests(unittest.TestCase):
    """Floors given as ranges or plain numbers in a title."""

    def _labels(self, title):
        from Drawing_Analyzer.levels import parse_levels
        return [lv["label"].replace(" Floor", "") for lv in parse_levels(title)]

    def test_ranges(self):
        for title, want in [
            ("TYPICAL FLOOR PLAN 4th to 7th", ["4th", "5th", "6th", "7th"]),
            ("TYPICAL FLOOR PLAN 4th - 7th", ["4th", "5th", "6th", "7th"]),
            ("TYPICAL FLOOR PLAN 5 TO 8", ["5th", "6th", "7th", "8th"]),
            ("FLOORS 5-8", ["5th", "6th", "7th", "8th"]),
            ("5TH TO 8 FLOOR", ["5th", "6th", "7th", "8th"]),
            ("SECOND TO FIFTH FLOOR", ["2nd", "3rd", "4th", "5th"]),
            ("FLOOR 2 TO 4 & 6", ["2nd", "3rd", "4th", "6th"]),
            ("FLOOR PLAN (5,6 & 7)", ["5th", "6th", "7th"]),
            ("LEVEL 3", ["3rd"]),
        ]:
            self.assertEqual(self._labels(title), want, title)

    def test_numbers_that_are_not_floors(self):
        for title in ("FLOOR LVL +3.000", "DATE 12-05-2024 FLOOR PLAN",
                      "2 X 3 M FLOOR"):
            self.assertEqual(self._labels(title), [], title)
        # The office numbering beside an ordinal floor is not a floor.
        self.assertEqual(self._labels("A-106 4th FLOOR PLAN(OFFICE FLOOR 01)"), ["4th"])

    def test_range_is_named_as_range(self):
        from Drawing_Analyzer.levels import floor_name
        self.assertEqual(floor_name("TYPICAL FLOOR PLAN 5 TO 8"), "Typical 5th to 8th Floor")


class PickedLevelsTests(unittest.TestCase):
    """The floors ticked in Step 2 travel with the plate to the statement."""

    def test_sanitised(self):
        from Drawing_Analyzer.app import _picked_levels
        got = _picked_levels([{"key": 2, "label": "2nd Floor"},
                              {"key": "x"}, "junk",
                              {"key": 1000.5, "label": "Upper Terrace Floor", "refuge": 1}])
        self.assertEqual(got, [{"key": 2, "label": "2nd Floor", "refuge": False},
                               {"key": 1000.5, "label": "Upper Terrace Floor", "refuge": True}])
        self.assertEqual(_picked_levels(None), [])

    def test_statement_uses_picked_floors(self):
        # A typical plate for 2nd-7th with only 2nd and 3rd ticked counts twice.
        from Drawing_Analyzer.area_statement import build_statement
        data = {"floors": [{"name": "Typical 2nd & 3rd Floor", "area_sqm": 100.0,
                            "levels": [{"key": 2, "label": "2nd Floor", "refuge": False},
                                       {"key": 3, "label": "3rd Floor", "refuge": False}]}],
                "rooms": []}
        rows = build_statement(data, "plan.dxf")["towers"][0]["rows"]
        self.assertEqual([r["level"] for r in rows], ["2nd Floor", "3rd Floor"])


if __name__ == "__main__":
    unittest.main()
