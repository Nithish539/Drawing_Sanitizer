# -*- coding: utf-8 -*-
"""Door openings (AI_DOOR), checked against the manually corrected
drawings of BHUB 1st Floor and the XA low-zone typical floor.

  * a door in a wall is the gap from the wall's end to the next face across
    its band, found from a swing on any layer - BHUB and XA draw shaft
    doors on the shaft's layer, part-open;
  * across, the gap is as deep as the lines drawn in it along the wall (a
    frame), lines running on past both jambs (an area outline) not
    counted; with none, the wall's thickness;
  * a door at a corner runs from the wall's end to the face of the wall
    across;
  * a pair of leaves is one opening, and a pair's width needs both leaves;
  * a cubicle door in a 12 mm board, with no wall: hinge to where the leaf
    shuts, the board's faces deep;
  * a dynamic door block drawn with all its states is one door;
  * an arc that does not reach the gap, or a part-open swing with no wall,
    is not a door.

Run:  python -m unittest discover -s tests -v
"""

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from shapely.geometry import Polygon, box  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402


def seg(x1, y1, x2, y2, layer="A-WALL"):
    return (x1, y1, x2, y2, layer, math.hypot(x2 - x1, y2 - y1), -1, False, False)


def hwall(x0, x1, y0, y1, caps=True):
    """A horizontal wall: its strip and its lines."""
    strip = {"pts": [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], "t": y1 - y0}
    lines = [seg(x0, y0, x1, y0), seg(x0, y1, x1, y1)]
    if caps:
        lines += [seg(x0, y0, x0, y1), seg(x1, y0, x1, y1)]
    return strip, lines


def vwall(x0, x1, y0, y1):
    strip = {"pts": [(x0, y0), (x0, y1), (x1, y1), (x1, y0)], "t": x1 - x0}
    return strip, [seg(x0, y0, x0, y1), seg(x1, y0, x1, y1), seg(x0, y0, x1, y0), seg(x0, y1, x1, y1)]


def swing(hx, hy, r, shut_deg, open_deg):
    """An arc centred on the hinge from the shut direction to the open one."""
    a = (hx + r * math.cos(math.radians(shut_deg)), hy + r * math.sin(math.radians(shut_deg)))
    b = (hx + r * math.cos(math.radians(open_deg)), hy + r * math.sin(math.radians(open_deg)))
    return (hx, hy, r, a, b, abs(open_deg - shut_deg))


def wall_with_gap(g0=3000, g1=3800):
    s1, l1 = hwall(0, g0, 0, 200)
    s2, l2 = hwall(g1, 6000, 0, 200)
    return [s1, s2], l1 + l2


def bounds(q):
    return tuple(round(v) for v in Polygon(q).bounds)


class DoorInAWall(unittest.TestCase):

    def test_a_part_open_shaft_door_on_a_shaft_layer(self):
        """BHUB core shafts: an 800 mm arc on EL-SHAFT, open 27 degrees,
        its centre in the jamb: the 750 mm gap, the wall's 200 deep."""
        strips, segs = wall_with_gap()
        got = dr.door_openings([swing(3750, 100, 800, 180, 153)], segs, strips)
        self.assertEqual(len(got), 1)
        self.assertEqual(bounds(got[0]), (3000, 0, 3800, 200))

    def test_the_frame_drawn_in_the_gap_is_its_depth(self):
        """XA shaft doors: the frame lines 76 mm apart across the gap are the
        door; an area outline running through it is not."""
        strips, segs = wall_with_gap()
        segs += [seg(3000, 0, 3770, 0, "ELEC SPATIALS"), seg(3027, 76, 3773, 76, "ELEC SPATIALS"),
                 seg(2000, 200, 5000, 200, "A-ANNO-AREA")]
        got = dr.door_openings([swing(3760, 60, 700, 180, 135)], segs, strips)
        self.assertEqual(len(got), 1)
        self.assertEqual(bounds(got[0]), (3000, 0, 3800, 76))

    def test_an_area_line_through_the_gap_leaves_the_wall_depth(self):
        strips, segs = wall_with_gap()
        segs += [seg(2000, 200, 5000, 200, "A-ANNO-AREA")]
        got = dr.door_openings([swing(3750, 100, 800, 180, 153)], segs, strips)
        self.assertEqual(bounds(got[0]), (3000, 0, 3800, 200))

    def test_a_door_at_a_corner_runs_to_the_wall_across(self):
        """BHUB: a wall ends 1000 mm short of the wall across; the door's
        hinge is at the far side, by the wall across."""
        s1, l1 = vwall(0, 200, -3000, 0)
        s2, l2 = hwall(-2000, 2000, 1000, 1200)
        got = dr.door_openings([swing(170, 970, 930, -90, -178)], l1 + l2, [s1, s2])
        self.assertEqual(len(got), 1)
        self.assertEqual(bounds(got[0]), (0, 0, 200, 1000))

    def test_a_pair_of_leaves_is_one_opening(self):
        strips, segs = wall_with_gap(3000, 4800)
        arcs = [swing(3030, 100, 880, 0, 58), swing(4770, 100, 880, 180, 122)]
        got = dr.door_openings(arcs, segs, strips)
        self.assertEqual(len(got), 1)
        self.assertEqual(bounds(got[0]), (3000, 0, 4800, 200))

    def test_a_pairs_width_needs_both_leaves(self):
        strips, segs = wall_with_gap(3000, 4800)
        self.assertEqual(dr.door_openings([swing(3030, 100, 880, 0, 58)], segs, strips), [])

    def test_a_door_blocks_states_are_one_door(self):
        """XA *U blocks: six swings of 652-1101 mm on one hinge."""
        strips, segs = wall_with_gap(3000, 4200)
        arcs = [swing(4170, 100, r, 180, 92) for r in (652, 701, 752, 801, 920, 1101)]
        got = dr.door_openings(arcs, segs, strips)
        self.assertEqual(len(got), 1)
        self.assertEqual(bounds(got[0]), (3000, 0, 4200, 200))

    def test_an_arc_that_does_not_reach_the_gap(self):
        strips, segs = wall_with_gap()
        # a 900 mm arc by the jamb, swinging away from the gap
        self.assertEqual(dr.door_openings([swing(3750, 300, 900, 0, 80)], segs, strips), [])

    def test_a_gap_the_swing_does_not_fit(self):
        strips, segs = wall_with_gap(3000, 6000 - 1)          # 3 m: no single leaf
        self.assertEqual(dr.door_openings([swing(5970, 100, 800, 180, 153)], segs, strips), [])


class DoorWithNoWall(unittest.TestCase):

    def board(self):
        """A 12 mm board with a 600 mm opening (BHUB's ELEC cubicles)."""
        return [seg(0, 1000, 1000, 1000, "A3-HATCH"), seg(0, 1012, 1000, 1012, "A3-HATCH"),
                seg(1600, 1000, 2600, 1000, "A3-HATCH"), seg(1600, 1012, 2600, 1012, "A3-HATCH")]

    def test_a_cubicle_door_in_a_board(self):
        segs = self.board() + [seg(1600, 1012, 1600, 412, "A3-HATCH")]    # the leaf, open
        got = dr.door_openings([swing(1600, 1012, 600, 180, 270)], segs, [])
        self.assertEqual(len(got), 1)
        self.assertEqual(bounds(got[0]), (1000, 1000, 1600, 1012))

    def test_with_no_leaf_drawn_it_is_not_a_door(self):
        self.assertEqual(dr.door_openings([swing(1600, 1012, 600, 180, 270)], self.board(), []), [])

    def test_a_part_open_swing_with_no_wall_is_not_a_door(self):
        segs = self.board() + [seg(1600, 1012, 1067, 1284, "A3-HATCH")]
        self.assertEqual(dr.door_openings([swing(1600, 1012, 600, 180, 153)], segs, []), [])


class DoorArcsRead(unittest.TestCase):

    def test_any_layer_door_size_and_sweep(self):
        import ezdxf
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        msp.add_arc((0, 0), 800, 90, 117, dxfattribs={"layer": "EL-SHAFT"})        # a shaft door, 27 degrees
        msp.add_arc((5000, 0), 600, 0, 90, dxfattribs={"layer": "A-DOOR"})
        msp.add_arc((9000, 0), 300, 0, 90, dxfattribs={"layer": "A-DOOR"})         # too small
        msp.add_arc((12000, 0), 900, 0, 180, dxfattribs={"layer": "A-DOOR"})       # half a circle
        got = dr._collect_door_arcs(doc, msp, dr.MM_UNITS)
        self.assertEqual(sorted(round(a[2]) for a in got), [600, 800])


if __name__ == "__main__":
    unittest.main()
