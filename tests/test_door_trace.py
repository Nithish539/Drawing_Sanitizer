# -*- coding: utf-8 -*-
"""A door's line is the room's inner wall trace across the opening, read
from the walls either side of it - the inner edge of the jambs' walls' ends
(2026-10-17, later):

  * a frame (100 mm) set in a 200 mm wall: the line on the room's face of
    the wall, not the frame's (XA, Centaurus);
  * a frame between two nibs: the nibs' room face (Centaurus area 5, the
    RWP compartment's door);
  * side walls that do not run on past the opening on either side (a shaft
    whose whole front is its door, P285 TEN / COMM / FAPA): the line at the
    side walls' ends, where the frame stands - the room runs to it;
  * jambs' walls ending apart: the nearer end, the distance given (flagged);
  * a wall over the opening or straight behind it, or no wall at a jamb: no
    trace (the line stays on the room's face of the opening);
  * the room squared up to the line: out to it, or back to it; no other
    room across it;
  * the final drawing draws Step 3's line (door_hints carries it).

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from shapely.geometry import Polygon, box  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import elements as el  # noqa: E402


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def up(quad):
    """The side (0 / 1) of the opening that faces +y."""
    return 0 if el.door_axes(quad)[5] > 0 else 1


def line_y(quad, t):
    ln = el.trace_line(quad, t)
    return (ln[0][1] + ln[1][1]) / 2.0


def solid(*rects):
    return unary_union([box(*r) for r in rects])


FRAME = rect(-300, -50, 300, 50)          # a 100 mm frame, 600 wide, in the middle of a 200 mm wall
FULL = rect(-300, -100, 300, 100)         # the opening the wall's whole thickness


class Trace(unittest.TestCase):

    def test_a_frame_in_a_wall_the_rooms_face(self):
        walls = solid((-2000, -100, -300, 100), (300, -100, 2000, 100))
        t, how, jog = el.door_trace(FRAME, up(FRAME), walls)
        self.assertEqual(how, "room face")
        self.assertAlmostEqual(line_y(FRAME, t), 100.0, delta=1.0)          # not the frame's 50
        self.assertLessEqual(jog, 1.0)
        t, how, _ = el.door_trace(FRAME, 1 - up(FRAME), walls)               # the room below: its face
        self.assertAlmostEqual(line_y(FRAME, t), -100.0, delta=1.0)

    def test_a_frame_between_nibs_the_nibs_room_face(self):
        walls = solid((-700, -100, -300, 100), (300, -100, 700, 100),        # the nibs
                      (-900, -100, -700, 1500), (700, -100, 900, 1500))      # the compartment's walls
        t, how, _ = el.door_trace(FRAME, up(FRAME), walls)
        self.assertEqual(how, "room face")
        self.assertAlmostEqual(line_y(FRAME, t), 100.0, delta=1.0)

    def test_side_walls_not_running_on_the_line_at_their_ends(self):
        """P285 TEN: the shaft's side walls end at the front; the opening
        found is 200 deep behind it."""
        walls = solid((-500, -100, -300, 1500), (300, -100, 500, 1500), (-500, 1500, 500, 1700))
        t, how, jog = el.door_trace(FULL, up(FULL), walls)
        self.assertEqual(how, "wall ends")
        self.assertAlmostEqual(line_y(FULL, t), -100.0, delta=1.0)          # the front, not +100
        self.assertLessEqual(jog, 1.0)

    def test_jambs_ending_apart_the_nearer_end(self):
        walls = solid((-500, -100, -300, 1500), (300, -60, 500, 1500), (-500, 1500, 500, 1700))
        t, how, jog = el.door_trace(FULL, up(FULL), walls)
        self.assertAlmostEqual(line_y(FULL, t), -60.0, delta=1.0)
        self.assertAlmostEqual(jog, 40.0, delta=3.0)
        self.assertGreater(jog, el.DOOR_TRACE_JOG_MM)

    def test_no_trace(self):
        base = [(-2000, -100, -300, 100), (300, -100, 2000, 100)]
        t, why, _ = el.door_trace(FRAME, up(FRAME), solid(*base, (-300, -100, 300, 100)))
        self.assertEqual((t, why), (None, "wall over the opening"))
        t, why, _ = el.door_trace(FRAME, up(FRAME), solid(*base, (-300, 100, 300, 160)))
        self.assertEqual((t, why), (None, "wall behind the opening"))
        t, why, _ = el.door_trace(FRAME, up(FRAME), solid(base[0]))
        self.assertEqual((t, why), (None, "no wall at a jamb"))

    def test_what_is_set_in_the_wall_is_a_jamb_but_not_over_the_door(self):
        """A cut-out beside the door is the wall line; one over the door is
        not read as wall over it."""
        walls = [box(-2000, -100, -700, 100), box(700, -100, 2000, 100)]
        sets = [box(-700, -100, -250, 100), box(250, -100, 700, 100)]      # sleeves, 50 into the opening
        S = el.trace_solid(walls, sets, [FRAME])
        t, how, _ = el.door_trace(FRAME, up(FRAME), S)
        self.assertEqual(how, "room face")
        self.assertAlmostEqual(line_y(FRAME, t), 100.0, delta=1.0)

    def test_door_traces_leave_board_doors_and_unsettled_ones(self):
        walls = solid((-2000, -100, -300, 100), (300, -100, 2000, 100))
        board = rect(-300, -6, 300, 6)
        out = el.door_traces([FRAME, board, FRAME], [up(FRAME), up(board), None], walls)
        self.assertIsNotNone(out[0][0])
        self.assertEqual(out[1], (None, None, None))
        self.assertEqual(out[2], (None, None, None))


class Rooms(unittest.TestCase):

    def test_a_shaft_runs_out_to_its_line(self):
        walls = solid((-500, -100, -300, 1500), (300, -100, 500, 1500), (-500, 1500, 500, 1700))
        side = up(FULL)
        t, _how, _ = el.door_trace(FULL, side, walls)
        shaft = Polygon(rect(-300, 100, 300, 1500))                          # traced from the opening's back face
        (g,), grown, cut = el.rooms_to_door_lines([shaft], [FULL], [(0, side, t)])
        self.assertAlmostEqual(g.area, 600 * 1600, delta=50)                 # down to the front
        self.assertAlmostEqual(grown, 0.12, places=2)
        self.assertAlmostEqual(g.bounds[1], -100.0, delta=1.0)

    def test_a_room_run_into_the_doorway_goes_back_to_its_line(self):
        walls = solid((-2000, -100, -300, 100), (300, -100, 2000, 100))
        side = up(FRAME)
        t, _how, _ = el.door_trace(FRAME, side, walls)
        room = unary_union([box(-2000, 100, 2000, 3000), box(-300, 50, 300, 100)])   # to the frame line
        (g,), grown, cut = el.rooms_to_door_lines([room], [FRAME], [(0, side, t)])
        self.assertAlmostEqual(g.area, 4000 * 2900, delta=50)
        self.assertAlmostEqual(cut, 0.03, places=3)

    def test_no_other_room_across_the_line(self):
        walls = solid((-2000, -100, -300, 100), (300, -100, 2000, 100))
        side = up(FRAME)
        t, _how, _ = el.door_trace(FRAME, side, walls)
        room = Polygon(rect(-2000, 100, 2000, 3000))
        corridor = unary_union([box(-2000, -2000, 2000, -100), box(-300, -100, 300, 150)])  # into the room's side
        (r, c), _g, _c = el.rooms_to_door_lines([room, corridor], [FRAME], [(0, side, t)])
        self.assertLessEqual(c.bounds[3], 100.5)
        # an X of the X sequence is left as it is
        (r, c), _g, _c = el.rooms_to_door_lines([room, corridor], [FRAME], [(0, side, t)], skip=[False, True])
        self.assertIs(c, corridor)


class Final(unittest.TestCase):

    def test_a_door_step3_settled_no_room_for_squares_no_room(self):
        """Step 3 drew the line but stored no way into a room (a room traced
        through its own door): the final drawing draws the line, and no
        room is cut at it."""
        step3 = [(Polygon(FULL), (0.0, 100.0))]
        gs = [Polygon(rect(-1000, -1500, 1000, 1500))]                       # the same room both sides
        plan = dr.door_plan([FULL], gs, [el.OWNER_ROOM], (), step3, None)
        side, who, t, how = plan[0]
        self.assertEqual((who, how), (None, "step 3 line"))
        (g,), grown, cut = el.rooms_to_door_lines(gs, [FULL], [(who, side, t)])
        self.assertIs(g, gs[0])

    def test_the_final_drawing_draws_step3s_line(self):
        # Step 3: the shaft's line at its front (y -100), the way into it +y
        step3 = [(Polygon(FULL), (0.0, -100.0), (0.0, 1.0))]
        q2 = rect(-295, -95, 295, 95)                                        # the final drawing's opening
        gs = [Polygon(rect(-300, 100, 300, 1500))]
        plan = dr.door_plan([q2], gs, [el.OWNER_SHAFT], (), step3, None)
        side, who, t, how = plan[0]
        self.assertEqual((side, who, how), (up(q2), 0, "step 3"))
        self.assertAlmostEqual(line_y(q2, t), -100.0, delta=0.5)

    def test_a_door_only_the_final_drawing_has_follows_the_rule(self):
        walls = solid((-2000, -100, -300, 100), (300, -100, 2000, 100))
        gs = [Polygon(rect(-2000, 100, 2000, 3000))]
        plan = dr.door_plan([FRAME], gs, [el.OWNER_ROOM], (), (), walls)
        side, who, t, how = plan[0]
        self.assertEqual((who, how), (0, "room face"))
        self.assertAlmostEqual(line_y(FRAME, t), 100.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
