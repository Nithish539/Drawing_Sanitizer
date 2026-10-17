# -*- coding: utf-8 -*-
"""Room confidence (confidence.py).

Checked on the benchmark drawings against the take-off polylines drawn in
them (285 FD T1 first floor, BHUB T1 first and 4th floors): of the rooms
scored High, 100% were within 5% of the take-off; Medium 88-90%.

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from Drawing_Analyzer.confidence import score_room, band  # noqa: E402


class ConfidenceTests(unittest.TestCase):
    def test_stated_size_matched_is_high(self):
        s, why = score_room(6.25, 2500, 2500, want=(2500, 2500), family="Lift")
        self.assertEqual(band(s), "High")
        self.assertTrue(any("stated size" in w for w in why))

    def test_stated_size_missed_is_low(self):
        s, _ = score_room(40.8, 6000, 6800, want=(1200, 1000), family="Shaft")
        self.assertEqual(band(s), "Low")

    def test_box_only_and_no_family_is_low(self):
        s, _ = score_room(12.0, 3000, 4000, traced=False, family="Unknown")
        self.assertEqual(band(s), "Low")

    def test_tag_name_is_not_high(self):
        s, _ = score_room(140.0, 10000, 14000, family="Plumbing", tag_name=True)
        self.assertNotEqual(band(s), "High")

    def test_x_marked_opening_is_high(self):
        s, _ = score_room(0.9, 750, 1200, source="X", family="Shaft", want=(750, 1200))
        self.assertEqual(band(s), "High")


if __name__ == "__main__":
    unittest.main()
