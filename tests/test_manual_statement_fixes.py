# -*- coding: utf-8 -*-
"""Fixes found by checking the engine against manual area statements
(285 FD Tower 1, BHUB Tower 1).

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402

from Drawing_Analyzer.area_statement import classify  # noqa: E402
from Drawing_Analyzer.dxf_reader import _exact_size_err  # noqa: E402
from Drawing_Analyzer.dxfio import drop_shifted_copy  # noqa: E402


class ShiftedCopyTests(unittest.TestCase):
    """A plan drawn twice, the copy a millimetre off, is read once."""

    def _plan(self, doc, dx=0.0, dy=0.0):
        msp = doc.modelspace()
        for i in range(40):
            msp.add_line((i * 1000 + dx, dy), (i * 1000 + dx, 3000 + dy), dxfattribs={"layer": "A-WALL"})
            msp.add_text("ROOM %d" % i, dxfattribs={"layer": "A-TEXT", "insert": (i * 1000 + 500 + dx, 1500 + dy)})

    def test_copy_dropped(self):
        doc = ezdxf.new()
        self._plan(doc)
        self._plan(doc, 0.9, 1.3)
        removed = drop_shifted_copy(doc)
        self.assertEqual(removed, 80)
        self.assertEqual(len(doc.modelspace()), 80)

    def test_single_plan_untouched(self):
        doc = ezdxf.new()
        self._plan(doc)
        self.assertEqual(drop_shifted_copy(doc), 0)
        self.assertEqual(len(doc.modelspace()), 80)

    def test_a_few_repeats_are_not_a_copy(self):
        doc = ezdxf.new()
        self._plan(doc)
        msp = doc.modelspace()
        for i in range(3):                       # three lines drawn twice
            msp.add_line((i * 1000 + 1, 0), (i * 1000 + 1, 3000), dxfattribs={"layer": "A-WALL"})
        self.assertEqual(drop_shifted_copy(doc), 0)


class StatedSizeTests(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(_exact_size_err((750, 1140), 750, 1140), 0)
        self.assertEqual(_exact_size_err((1380, 1250), 1250, 1380), 0)

    def test_close_sizes_are_different_shafts(self):
        self.assertIsNone(_exact_size_err((750, 1050), 750, 1140))

    def test_strip_of_the_shaft(self):
        self.assertIsNone(_exact_size_err((750, 1210), 300, 1210))
        self.assertEqual(_exact_size_err((750, 1210), 300, 1210, partial=True), 0)


class NamesTests(unittest.TestCase):
    def test_coded_shafts(self):
        cases = {
            "300 WD CUTOUT AT FLOOR FT. LOBBY PR. SHAFT 1380x1250": "lobby_press",
            "EL-SH-03 750X1050": "elect_shaft",
            "CO-SH-01 750x930": "comm",
            "TR-SH-02 750x1000": "ten",
            "FP-SH-02 750X1210": "fp",
            "ELV-SH-01 750X1200": "elv",
            "FHC-3": "fhr",
        }
        for name, key in cases.items():
            with self.subTest(name=name):
                self.assertEqual(classify(name, "Shaft", opening=True), key)

    def test_cutout_above_is_not_deducted(self):
        self.assertEqual(classify("CUTOUT 300 WD\nCUTOUT ABOVE AT CEILING", "", opening=True), "cutout_above")
        self.assertEqual(classify("CUTOUT 300 WD AT FLOOR", "", opening=True), "unnamed_opening")

    def test_hose_cabinet_is_not_a_shaft(self):
        # the cabinet traced from its label is not deducted; its X'd slot is
        self.assertNotEqual(classify("FHC-3", "Fire / Life Safety", opening=False), "fhr")
        self.assertEqual(classify("FHC-3", "Fire / Life Safety", opening=True), "fhr")


if __name__ == "__main__":
    unittest.main()
