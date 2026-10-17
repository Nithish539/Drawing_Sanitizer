# -*- coding: utf-8 -*-
"""Two geometry fixes: an unhatched face pair inside a room on a floor that
hatches its walls is not a wall (Fix A, with the room-size veto); X marks
meeting at a corner are not one box (Fix C); a shaft never cuts room area
across a wall (Fix D).

Run:  python -m unittest discover -s tests -v
"""

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from shapely.geometry import Polygon, box  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402


def seg(x1, y1, x2, y2, layer="A-WL"):
    return (x1, y1, x2, y2, layer, math.hypot(x2 - x1, y2 - y1), -1, False)


def pair(x0, y0, x1, y1, layer="A-WL"):
    """A wall as its two long faces: the rectangle x0..x1 x y0..y1, faces
    along its longer side."""
    if x1 - x0 >= y1 - y0:
        return [seg(x0, y0, x1, y0, layer), seg(x0, y1, x1, y1, layer)]
    return [seg(x0, y0, x0, y1, layer), seg(x1, y0, x1, y1, layer)]


def hatch(x0, y0, x1, y1):
    return ([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], [], "A-WALL-HATCH", "SOLID")


def has(faces, s, tol=2.0):
    return any(abs(f[0] - s[0]) < tol and abs(f[1] - s[1]) < tol and abs(f[2] - s[2]) < tol and abs(f[3] - s[3]) < tol
               or abs(f[0] - s[2]) < tol and abs(f[1] - s[3]) < tol and abs(f[2] - s[0]) < tol and abs(f[3] - s[1]) < tol
               for f in faces)


PLATE = [(-5000, -5000), (20000, -5000), (20000, 20000), (-5000, 20000)]
# a 9 x 6 m room: four 200 mm walls, all hatched
WALLS = [(-200, -200, 9200, 0), (-200, 6000, 9200, 6200), (-200, 0, 0, 6000), (9000, 0, 9200, 6000)]


def room():
    segs, hs = [], []
    for w in WALLS:
        segs += pair(*w)
        hs.append(hatch(*w))
    return segs, hs


def strips_at(w, x0, x1):
    return [st for st in w["strips"] if box(x0, 100, x1, 5900).contains(Polygon(st["pts"]).centroid)]


# ------------------------------------------------- A. unhatched pairs ---
class UnhatchedPairTests(unittest.TestCase):

    def test_unhatched_pair_inside_a_hatched_floor_is_dropped(self):
        # two loose lines 200 apart across the room, wall to wall, no hatch
        segs, hs = room()
        segs += pair(4000, 0, 4200, 6000)
        w = dr._detect_walls(segs, plate=PLATE, hatches=hs)
        self.assertEqual(strips_at(w, 3900, 4300), [])
        self.assertFalse(has(w["faces"], (4000, 0, 4000, 6000)))
        self.assertIn("unhatched pair inside a room", [r for r, _s in w["excluded"]])
        self.assertTrue(has(w["faces"], (-200, 0, 9200, 0)))         # the room's own walls stay

    def test_hatched_pair_is_kept(self):
        segs, hs = room()
        segs += pair(4000, 0, 4200, 6000)
        hs.append(hatch(4000, 0, 4200, 6000))
        w = dr._detect_walls(segs, plate=PLATE, hatches=hs)
        self.assertEqual(len(strips_at(w, 3900, 4300)), 1)
        self.assertTrue(has(w["faces"], (4000, 0, 4000, 6000)))

    def test_unhatched_pair_continuing_a_hatched_wall_is_kept(self):
        # a hatched wall from the bottom to y 3000, the same wall on
        # unhatched to the top: one wall, drawn in two pieces
        segs, hs = room()
        segs += pair(4000, 0, 4200, 3000) + pair(4000, 3000, 4200, 6000)
        hs.append(hatch(4000, 0, 4200, 3000))
        w = dr._detect_walls(segs, plate=PLATE, hatches=hs)
        self.assertEqual(len(strips_at(w, 3900, 4300)), 2)
        self.assertTrue(has(w["faces"], (4000, 3000, 4000, 6000)))

    def test_floor_that_does_not_hatch_its_walls_is_untouched(self):
        segs, _hs = room()
        segs += pair(4000, 0, 4200, 6000)
        w = dr._detect_walls(segs, plate=PLATE, hatches=[])
        self.assertEqual(len(strips_at(w, 3900, 4300)), 1)

    def test_room_size_veto(self):
        sq = [(4000, 0), (4200, 0), (4200, 6000), (4000, 6000)]
        strips = [{"pts": sq, "t": 200, "layers": ("A-WL", "A-WL")}]
        counts = {}
        pieces, _rep = dr.wall_polygons(strips, PLATE, counts=counts)
        self.assertGreater(sum(p.area for v in pieces.values() for p in v), 1e6)
        counts = {}
        pieces, _rep = dr.wall_polygons(strips, PLATE, counts=counts, veto=[sq])
        self.assertEqual(sum(p.area for v in pieces.values() for p in v), 0)
        self.assertEqual(counts.get("vetoed"), 1)

    def test_veto_cuts_drawn_walls(self):
        ring = [[(0, 0), (9000, 0), (9000, 200), (0, 200)]]
        out, n = dr.veto_walls({"AI_WALL": [ring]}, [[(4000, -50), (4200, -50), (4200, 250), (4000, 250)]])
        self.assertEqual(n, 1)
        self.assertEqual(len(out["AI_WALL"]), 2)            # the wall either side of the strip


# ----------------------------------------------------- C. X clusters ---
def x_pair(x0, y0, x1, y1, diag):
    """One X: its two diagonals appended to diag, the pair returned."""
    for a, b, c, d in ((x0, y0, x1, y1), (x0, y1, x1, y0)):
        diag.append((a, b, c, d, (a + c) / 2.0, (b + d) / 2.0, "A3-CUTOUTS", math.hypot(c - a, d - b)))
    return (len(diag) - 2, len(diag) - 1)


class XClusterTests(unittest.TestCase):

    def test_two_x_strips_forming_an_l_are_not_one_box(self):
        diag = []
        pairs = [x_pair(0, 0, 900, 300, diag), x_pair(600, 300, 900, 800, diag)]
        cl = dr._build_x_clusters_from_pairs(diag, pairs)
        self.assertEqual(sorted(tuple(round(v) for v in c["bbox"]) for c in cl),
                         [(0, 0, 900, 300), (600, 300, 900, 800)])

    def test_overlapping_x_marks_stay_one(self):
        diag = []
        pairs = [x_pair(0, 0, 900, 600, diag), x_pair(10, 10, 890, 590, diag)]
        cl = dr._build_x_clusters_from_pairs(diag, pairs)
        self.assertEqual(len(cl), 1)

    def test_x_merged_with_its_room_keeps_the_room_inside_its_extent(self):
        # an L of X strips along two sides of a shaft, the shaft traced from
        # its label: the shaft is the room, cut to the X's extent
        x = {"bbox": (0, 0, 2760, 1270), "poly": [(0, 0), (2760, 0), (2760, 1270), (2460, 1270), (2460, 300), (0, 300)],
             "holes": [], "xrects": None}
        room_ = {"bbox": (1340, 300, 2460, 1470), "poly": [(1340, 300), (2460, 300), (2460, 1470), (1340, 1470)]}
        kb, kpoly, _kh, _kx = dr._x_merge_shape(x, room_)
        self.assertEqual(tuple(round(v) for v in kb), (1340, 300, 2460, 1270))
        # a plain X box keeps itself
        xb = {"bbox": (0, 0, 1000, 1000), "poly": None, "holes": [], "xrects": None}
        self.assertEqual(dr._x_merge_shape(xb, room_)[0], (0, 0, 1000, 1000))


# ----------------------------------------------- D. cutters and walls ---
class ShaftCutterTests(unittest.TestCase):

    def test_a_shaft_cutter_never_crosses_a_wall_hatch(self):
        # an X box reaching across a hatched wall into the room beyond
        shaft = box(3800, 3000, 5600, 4000)
        wall = box(5000, 0, 5200, 6000)
        cut = dr._shaft_cutter(shaft, (4400, 3500), unary_union([wall]))
        room_beyond = box(5200, 0, 9000, 6000)
        self.assertLess(cut.intersection(room_beyond).area, 1.0)
        self.assertAlmostEqual(cut.area, 1200 * 1000, delta=1.0)

    def test_cutter_without_walls_is_the_shape(self):
        shaft = box(0, 0, 1000, 1000)
        self.assertEqual(dr._shaft_cutter(shaft, (500, 500), None).area, shaft.area)


if __name__ == "__main__":
    unittest.main()
