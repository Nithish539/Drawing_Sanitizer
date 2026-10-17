# -*- coding: utf-8 -*-
"""What is wall (walltrace.py).

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from Drawing_Analyzer import walltrace as wt  # noqa: E402


def seg(x1, y1, x2, y2, layer="A-WALL"):
    import math
    return (x1, y1, x2, y2, layer, math.hypot(x2 - x1, y2 - y1), -1, False, False)


PLATE = [(-1000, -1000), (11000, -1000), (11000, 11000), (-1000, 11000)]


class LayerTests(unittest.TestCase):
    def test_last_part_of_xref_name(self):
        self.assertTrue(wt.is_wall_layer("A3-WL_INT"))
        self.assertTrue(wt.is_wall_layer("XREF$0$A-WALL"))
        self.assertFalse(wt.is_wall_layer("BLOCKWALLS$0$A-DOOR"))
        self.assertFalse(wt.is_wall_layer("wall tile"))
        self.assertFalse(wt.is_wall_layer("A-GLAZ"))


class TraceTests(unittest.TestCase):
    def test_one_wall_drawn_twice_is_counted_once(self):
        s = [seg(0, 0, 10000, 0), seg(0, 200, 10000, 200),
             seg(0, 0.5, 10000, 0.5)]                     # the same face again
        r = wt.trace_floor(s, [], [], PLATE)
        self.assertAlmostEqual(r["footprint_sqm"], 2.0, places=1)
        self.assertAlmostEqual(r["run_m"], 10.0, places=1)

    def test_nearest_face_only(self):
        # 200 wall, 151 gap, 200 wall: never a 551 mm strip across all three
        s = [seg(0, 0, 3000, 0), seg(0, 200, 3000, 200), seg(0, 351, 3000, 351), seg(0, 551, 3000, 551)]
        strips = wt.nearest_face_strips({0.0: wt.merge_collinear(
            [[0, 3000, 0], [0, 3000, 200], [0, 3000, 351], [0, 3000, 551]])})
        self.assertTrue(all(c["t"] <= 201 for c in strips))
        self.assertEqual(len(strips), 3)
        r = wt.trace_floor(s + [seg(0, 0, 0, 551), seg(3000, 0, 3000, 551)], [], [], PLATE)
        self.assertIsNotNone(r)

    def test_hatch_found_without_its_name(self):
        s = [seg(0, 0, 10000, 0), seg(0, 200, 10000, 200)]
        wall_hatch = ([(0, 0), (10000, 0), (10000, 200), (0, 200)], [], "WHATEVER", "ANSI37", False)
        room_hatch = ([(0, 300), (10000, 300), (10000, 9000), (0, 9000)], [], "AREA", "SOLID", True)
        r = wt.trace_floor(s, [wall_hatch, room_hatch], [], PLATE)
        self.assertAlmostEqual(r["material"].area / 1e6, 2.0, places=1)
        self.assertEqual(r["hatched_traced_pct"], None)          # too little hatch to judge (< 3 sq.m)

    def test_no_wall_layers_falls_back(self):
        self.assertIsNone(wt.trace_floor([seg(0, 0, 100, 0, "A-FURN")], [], [], PLATE))


if __name__ == "__main__":
    unittest.main()
