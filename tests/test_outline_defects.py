# -*- coding: utf-8 -*-
"""Outline defects: slits and spikes, dead-end lines, break symbols, rooms
grown to their stated size.

Run:  python -m unittest discover -s tests -v
"""

import collections
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from shapely.geometry import Polygon, box  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402


def seg(x1, y1, x2, y2, layer="A-WL"):
    return (x1, y1, x2, y2, layer, math.hypot(x2 - x1, y2 - y1), -1, False)


def wall(x1, y1, x2, y2, t=200, layer="A-WL"):
    """A wall: two faces t apart (to the left of the direction of travel)."""
    L = math.hypot(x2 - x1, y2 - y1)
    nx, ny = -(y2 - y1) / L * t, (x2 - x1) / L * t
    return [seg(x1, y1, x2, y2, layer), seg(x1 + nx, y1 + ny, x2 + nx, y2 + ny, layer)]


def has(faces, s, tol=2.0):
    return any(abs(f[0] - s[0]) < tol and abs(f[1] - s[1]) < tol and abs(f[2] - s[2]) < tol and abs(f[3] - s[3]) < tol
               or abs(f[0] - s[2]) < tol and abs(f[1] - s[3]) < tol and abs(f[2] - s[0]) < tol and abs(f[3] - s[1]) < tol
               for f in faces)


PLATE = [(-5000, -5000), (20000, -5000), (20000, 20000), (-5000, 20000)]


def room_walls():
    """A 9 x 6 m room: four 200 mm walls, corners joined."""
    segs = []
    segs += wall(0, 0, 9000, 0)                      # bottom (faces y 0 and 200)
    segs += wall(9000, 0, 9000, 6000)                # right
    segs += wall(9000, 6000, 0, 6000)                # top
    segs += wall(0, 6000, 0, 0)                      # left
    return segs


# --------------------------------------------------- 1. slits and spikes ---
class CleanOutlineTests(unittest.TestCase):

    def test_slit_closed_notch_kept(self):
        # MALE TOILET: a 2 mm slit 350 deep from the top, and a 101 mm
        # notch 500 deep that is a real wall
        poly = [(0, 0), (6000, 0), (6000, 4000), (4101, 4000), (4101, 3500), (4000, 3500), (4000, 4000),
                (1402, 4000), (1402, 3650), (1400, 3650), (1400, 4000), (0, 4000)]
        np_, nh, changed = dr.clean_outline(poly, [])
        self.assertTrue(changed)
        g = Polygon(np_)
        self.assertFalse(g.intersects(box(4010, 3600, 4090, 3990)))      # the notch stays
        self.assertTrue(g.contains(box(1395, 3700, 1407, 3990)))          # the slit is filled
        self.assertAlmostEqual(g.area, Polygon(poly).area, delta=0.01 * g.area)

    def test_spike_cut_and_column_hole_kept(self):
        poly = [(0, 0), (5000, 0), (5000, 2000), (5800, 2010), (5000, 2030), (5000, 4000), (0, 4000)]
        hole = [(2000, 2000), (2400, 2000), (2400, 2400), (2000, 2400)]
        np_, nh, changed = dr.clean_outline(poly, [hole])
        self.assertTrue(changed)
        self.assertLessEqual(max(p[0] for p in np_), 5001)                 # the spike is gone
        self.assertEqual(len(nh), 1)                                       # the column stays a hole

    def test_clean_rectangle_unchanged(self):
        poly = [(0, 0), (4000, 0), (4000, 3000), (0, 3000)]
        self.assertFalse(dr.clean_outline(poly, [])[2])


# ----------------------------------------------- 2. short dead-end lines ---
class DeadEndTests(unittest.TestCase):

    def test_screen_pair_is_not_a_wall(self):
        # a glass screen in the room: two 350 mm lines 18 mm apart, standing
        # on the top wall, open at the bottom, on a wall layer (so each is
        # a wall face on its own, with no partner)
        segs = room_walls() + [seg(4400, 6000, 4400, 5650, "A-WALL-SCREEN"), seg(4418, 5650, 4418, 6000, "A-WALL-SCREEN")]
        w = dr._detect_walls(segs, plate=PLATE)
        self.assertFalse(has(w["faces"], (4400, 6000, 4400, 5650)))
        self.assertFalse(has(w["faces"], (4418, 5650, 4418, 6000)))
        self.assertIn("dead end", [r for r, _s in w["excluded"]])
        # the room's own walls are all still there
        self.assertTrue(has(w["faces"], (0, 0, 9000, 0)) and has(w["faces"], (9000, 0, 9000, 6000)))

    def test_short_wall_capped_at_its_end_stays(self):
        # a 400 mm nib, its end closed by a cap: a door jamb, not a dead end
        segs = room_walls() + wall(3000, 0, 3000, 400) + [seg(3000, 400, 2800, 400)]
        w = dr._detect_walls(segs, plate=PLATE)
        self.assertTrue(has(w["faces"], (3000, 0, 3000, 400)))


# ------------------------------------------------------ 3. break symbols ---
class BreakSymbolTests(unittest.TestCase):

    def stair_with_break(self):
        """A stair room with an oblique zig-zag break mark across it: two
        pieces 16 degrees off vertical, 108 mm apart, joined by three
        strokes stepping to both sides."""
        segs = room_walls()
        a = math.radians(106.3)
        ux, uy = math.cos(a), math.sin(a)
        E = (5454, 1072)
        A0 = (E[0] - ux * 897, E[1] - uy * 897)
        F = (E[0] + ux * 108, E[1] + uy * 108)
        B1 = (F[0] + ux * 1131, F[1] + uy * 1131)
        P, Q = (5210, 1128), (5613, 1128)
        piece_a, piece_b = seg(A0[0], A0[1], E[0], E[1], "A3-SYMBEL"), seg(F[0], F[1], B1[0], B1[1], "A3-SYMBEL")
        # a parallel pair of each (so the pieces read as wall faces)
        twin = [seg(x1 + 300, y1, x2 + 300, y2, "A3-SYMBEL") for x1, y1, x2, y2 in (piece_a[:4], piece_b[:4])]
        strokes = [seg(E[0], E[1], P[0], P[1], "A3-SYMBEL"), seg(P[0], P[1], Q[0], Q[1], "A3-SYMBEL"),
                   seg(Q[0], Q[1], F[0], F[1], "A3-SYMBEL")]
        return segs + [piece_a, piece_b] + twin + strokes, piece_a, piece_b

    def test_break_mark_is_not_a_wall(self):
        segs, pa, pb = self.stair_with_break()
        w = dr._detect_walls(segs, plate=PLATE)
        self.assertFalse(has(w["faces"], pa[:4]))
        self.assertFalse(has(w["faces"], pb[:4]))
        # the axis-aligned walls are untouched
        n_axis = sum(1 for f in w["faces"] if abs(f[0] - f[2]) < 1 or abs(f[1] - f[3]) < 1)
        self.assertEqual(n_axis, 8)

    def test_zigzag_found_whatever_the_angle(self):
        # the same mark found by its shape alone (rule B), with no wall run
        # to judge directions against
        segs, pa, pb = self.stair_with_break()
        segs = [s for s in segs if s[4] == "A3-SYMBEL"]
        axes = [dr._axis(s) for s in segs]
        marks = dr._break_marks(segs, axes, list(range(len(segs))))
        self.assertIn(segs.index(pa), marks)
        self.assertIn(segs.index(pb), marks)

    def test_genuine_angled_wall_stays(self):
        # a 30-degree wall 2.5 m long, from the left wall to a cross wall:
        # a direction with a long face in it
        a = math.radians(30)
        x2, y2 = 200 + 2500 * math.cos(a), 1000 + 2500 * math.sin(a)
        segs = room_walls() + wall(200, 1000, x2, y2) + wall(x2, 200, x2, 5800, t=-200)
        w = dr._detect_walls(segs, plate=PLATE)
        self.assertTrue(has(w["faces"], (200, 1000, x2, y2)))
        self.assertNotIn("oblique unsupported", [r for r, _s in w["excluded"]])

    def test_short_oblique_faces_are_not_walls(self):
        # 16-degree faces 936 mm long between two walls (rule A): no face
        # of the floor runs that way for 2 m
        a = math.radians(106)
        x2, y2 = 5000 + 936 * math.cos(a), 200 + 936 * math.sin(a)
        segs = room_walls() + wall(3000, 1100, 7000, 1100) + [seg(5000, 200, x2, y2), seg(5300, 200, x2 + 300, y2)]
        w = dr._detect_walls(segs, plate=PLATE)
        self.assertFalse(has(w["faces"], (5000, 200, x2, y2)))
        self.assertIn("oblique unsupported", [r for r, _s in w["excluded"]])

    def test_curved_wall_of_short_pieces_stays(self):
        # a curved wall: 20 short faces, each 4 degrees on from the last
        segs = room_walls()
        x, y, ang = 1000.0, 1000.0, 20.0
        for _k in range(20):
            a = math.radians(ang)
            nx, ny = x + 300 * math.cos(a), y + 300 * math.sin(a)
            segs += wall(x, y, nx, ny)
            x, y, ang = nx, ny, ang + 4
        w = dr._detect_walls(segs, plate=PLATE)
        self.assertNotIn("oblique unsupported", [r for r, _s in w["excluded"]])


# --------------------------------------------- 4. grown to stated size ---
class GrowTests(unittest.TestCase):

    def rec(self, label, b):
        return {"label": label, "source": "P", "poly": [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])],
                "holes": [], "bbox": b}

    def test_grows_over_the_strip_to_the_wall(self):
        # ELE. ROOM 2500x2300 traced 2300 wide; the wall face 200 mm on
        ele = self.rec("ELE. ROOM\n2500x2300", (171206, 97816, 173506, 100116))
        cut = self.rec("CUTOUT 600x2300", (170406, 97816, 171006, 100116))
        walls = [(171006, 97816, 171006, 100116), (171206, 97816, 171206, 100116)]
        stats = collections.Counter()
        grown = dr._grow_to_stated([ele, cut], walls, [], stats)
        self.assertEqual(len(grown), 1)
        self.assertAlmostEqual(Polygon(ele["poly"]).area / 1e6, 5.75, places=2)
        self.assertAlmostEqual(min(p[0] for p in ele["poly"]), 171006, delta=1)
        self.assertIn("fitted to stated size (grown)", ele["evidence"])
        self.assertAlmostEqual(min(p[0] for p in cut["poly"]), 170406, delta=1)    # the cut-out stays

    def test_not_over_another_room_or_a_hatch(self):
        ele = self.rec("ELE. ROOM 2500x2300", (171206, 97816, 173506, 100116))
        other = self.rec("STORE", (170900, 97816, 171206, 100116))
        walls = [(171006, 97816, 171006, 100116)]
        self.assertEqual(dr._grow_to_stated([ele, other], walls, [], collections.Counter()), [])
        ele2 = self.rec("ELE. ROOM 2500x2300", (171206, 97816, 173506, 100116))
        hatch = ([(171006, 97816), (171206, 97816), (171206, 100116), (171006, 100116)], [], "A-WALL")
        self.assertEqual(dr._grow_to_stated([ele2], walls, [hatch], collections.Counter()), [])

    def test_no_wall_face_no_growth(self):
        ele = self.rec("ELE. ROOM 2500x2300", (171206, 97816, 173506, 100116))
        self.assertEqual(dr._grow_to_stated([ele], [], [], collections.Counter()), [])


if __name__ == "__main__":
    unittest.main()
