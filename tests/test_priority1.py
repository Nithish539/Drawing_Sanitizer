# -*- coding: utf-8 -*-
"""Doors as exits, walls by their thickness, columns not taken from walls.

  * a door swing drawn as an ELLIPSE (285 FD, the P285 11th floor) is read
    like an ARC when it is a circle's arc; a flattened one is not;
  * a door opening is an exit, a shaft door or a cubicle door by the rooms
    on its two sides: exits on AI_DOOR, the others on AI_DOOR_SHAFT /
    AI_DOOR_CUBICLE; each room's exits are listed when the areas are read;
  * a piece of wall hatch no wider than a wall anywhere is wall, whatever
    its box (an L-shaped toilet wall), when lines on any layer edge it;
  * solid fill (concrete) left over by a shaft label is wall, not a shaft;
  * a piece 250 mm thick or less lying in a wall line is not a column.

Run:  python -m unittest discover -s tests -v
"""

import math
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402
from shapely.geometry import Polygon, box  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import elements as el  # noqa: E402
from Drawing_Analyzer import geoengine as ge  # noqa: E402
from Drawing_Analyzer import layernames as LN  # noqa: E402
from Drawing_Analyzer import walltrace as wt  # noqa: E402


def seg(x1, y1, x2, y2, layer="A-WALL"):
    return (x1, y1, x2, y2, layer, math.hypot(x2 - x1, y2 - y1), -1, False, False)


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def outline(pts, layer="A-WALL"):
    return [seg(*pts[i], *pts[(i + 1) % len(pts)], layer=layer) for i in range(len(pts))]


PLATE = rect(-5000, -5000, 20000, 20000)


class EllipseSwings(unittest.TestCase):

    def swings(self, ratio):
        doc = ezdxf.new()
        msp = doc.modelspace()
        msp.add_ellipse(center=(1000, 1000), major_axis=(900, 0), ratio=ratio,
                        start_param=0.0, end_param=math.pi / 2)
        msp.add_arc(center=(5000, 1000), radius=800, start_angle=0, end_angle=90)
        return dr._collect_door_arcs(doc, msp)

    def test_a_round_ellipse_is_a_swing(self):
        got = self.swings(1.0)
        self.assertEqual(len(got), 2)
        e = [g for g in got if abs(g[0] - 1000) < 1][0]
        self.assertAlmostEqual(e[2], 900, delta=5)
        self.assertAlmostEqual(e[5], 90, delta=0.5)
        ends = sorted((round(e[3][0]), round(e[3][1])) for _ in [0]) + [(round(e[4][0]), round(e[4][1]))]
        self.assertIn((1900, 1000), ends)
        self.assertIn((1000, 1900), ends)

    def test_a_flattened_ellipse_is_not(self):
        got = self.swings(0.6)
        self.assertEqual(len(got), 1)                        # the arc only
        self.assertAlmostEqual(got[0][0], 5000, delta=1)

    def test_the_ellipse_finds_its_door(self):
        """A 900 mm ellipse swing at a 900 mm gap in a 200 mm wall."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        msp.add_ellipse(center=(3000, 200), major_axis=(900, 0), ratio=1.0,
                        start_param=0.0, end_param=math.pi / 2)
        arcs = dr._collect_door_arcs(doc, msp)
        strips = [{"pts": rect(0, 0, 3000, 200), "t": 200}, {"pts": rect(3900, 0, 7000, 200), "t": 200}]
        segs = outline(rect(0, 0, 3000, 200)) + outline(rect(3900, 0, 7000, 200))
        got = dr.door_openings(arcs, segs, strips)
        self.assertEqual(len(got), 1)
        self.assertEqual(tuple(round(v) for v in Polygon(got[0]).bounds), (3000, 0, 3900, 200))

    def test_switched_off(self):
        old = dr.DOOR_ELLIPSES
        dr.DOOR_ELLIPSES = False
        try:
            self.assertEqual(len(self.swings(1.0)), 1)
        finally:
            dr.DOOR_ELLIPSES = old


DOOR = rect(1000, 3000, 1900, 3200)            # in the 200 mm wall between y 3000 and 3200
ROOM_A = box(0, 0, 3000, 3000)
ROOM_B = box(0, 3200, 3000, 6000)


class DoorKinds(unittest.TestCase):

    def kinds(self, rooms, shaft, labels=(), quads=(DOOR,)):
        return el.door_kinds(list(quads), rooms, shaft, labels)

    def test_between_two_rooms_an_exit(self):
        (k, i, j), = self.kinds([ROOM_A, ROOM_B], [False, False])
        self.assertEqual(k, el.EXIT)
        self.assertEqual({i, j}, {0, 1})

    def test_into_a_shaft(self):
        (k, _i, _j), = self.kinds([ROOM_A, ROOM_B], [False, True])
        self.assertEqual(k, el.SHAFT)

    def test_inside_one_room_a_cubicle(self):
        toilet = box(0, 0, 3000, 6000)
        board = rect(1000, 2994, 1800, 3006)                    # a cubicle door in a 12 mm board
        (k, i, j), = self.kinds([toilet], [False], quads=(board,))
        self.assertEqual((k, i, j), (el.CUBICLE, 0, 0))

    def test_the_same_room_through_a_wall_door_an_exit(self):
        """A room traced through its own door (the door in a 200 mm wall,
        the room on both sides): its way out, not a cubicle."""
        leaked = box(0, 0, 3000, 6000)
        (k, i, j), = self.kinds([leaked], [False])
        self.assertEqual((k, i, j), (el.EXIT, 0, 0))

    def test_a_shaft_a_little_further_off(self):
        """An untraced passage of 1 m between the door and the shaft."""
        shaft = box(0, 4200, 3000, 6000)
        (k, _i, _j), = self.kinds([ROOM_A, shaft], [False, True])
        self.assertEqual(k, el.SHAFT)

    def test_a_room_and_nothing_traced_an_exit(self):
        (k, i, j), = self.kinds([ROOM_A], [False])
        self.assertEqual(k, el.EXIT)
        self.assertEqual({i, j}, {0, None})

    def test_a_shaft_label_where_no_room_is_traced(self):
        (k, _i, _j), = self.kinds([ROOM_A], [False], labels=[("ELEC. SHAFT", 1450, 3700)])
        self.assertEqual(k, el.SHAFT)
        (k, _i, _j), = self.kinds([ROOM_A], [False], labels=[("LOBBY", 1450, 3700)])
        self.assertEqual(k, el.EXIT)

    def test_a_store_inside_a_hall(self):
        """The store's outline stands inside the hall's: its side is the
        store (the smallest room holding the point), the other the hall."""
        hall, store = box(0, 0, 8000, 8000), box(0, 0, 2000, 2000)
        door = rect(800, 2000, 1700, 2150)
        (k, i, j), = self.kinds([hall, store], [False, False], quads=(door,))
        self.assertEqual(k, el.EXIT)
        self.assertEqual({i, j}, {0, 1})

    def test_shaft_rooms_by_family_and_name(self):
        self.assertTrue(el.is_shaft_room("PASSENGER LIFT", "Lift"))
        self.assertTrue(el.is_shaft_room("ELEC. SHAFT"))
        self.assertTrue(el.is_shaft_room("FHC"))
        self.assertTrue(el.is_shaft_room("STAIR PRESS"))
        self.assertFalse(el.is_shaft_room("LIFT LOBBY", "Lift Lobby"))
        self.assertFalse(el.is_shaft_room("AHU ROOM", "MEP / Services"))
        self.assertFalse(el.is_shaft_room("PRESSURISATION ROOM"))

    def test_the_layers_are_reserved(self):
        for name in LN.DOORS:
            self.assertTrue(LN.is_reserved(name))
            self.assertFalse(LN.is_room_layer(name))
            self.assertTrue(LN.is_door_layer(name))
        self.assertEqual(LN.door_layer_kind("AI_DOOR_SHAFT"), "shaft")
        self.assertEqual(LN.door_layer_level("AI_DOOR_SECONDARY"), "secondary")


class DrawnByLevel(unittest.TestCase):

    def test_each_level_on_its_layer_with_its_kind(self):
        """2026-10-17: a door between two spaces is primary (AI_P_DOORS) -
        the lobby's door into the room and the room's door into the shaft
        behind it alike; a WC cubicle's door inside the room is secondary
        (AI_S_DOORS)."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        lobby = box(0, 3200, 6000, 6000)
        room = box(0, 0, 3000, 3000)
        shaft = box(3200, 0, 4400, 3000)
        door_room = rect(1000, 3000, 1900, 3200)          # lobby <-> room
        door_shaft = rect(3000, 1000, 3200, 1700)         # room <-> shaft
        door_cubicle = rect(1000, 1494, 1600, 1506)       # a board inside the room
        n = dr.draw_doors_by_level(doc, msp, dr.MM_UNITS, [door_room, door_shaft, door_cubicle],
                                   [(lobby, False, True), (room, False, False), (shaft, True, False)])
        self.assertEqual(n, {"primary": 2, "secondary": 1})
        got = {}
        for e in msp.query("LWPOLYLINE"):
            got.setdefault(e.dxf.layer, set()).add(dr.door_tag(e))
        self.assertEqual(LN.DOOR_PRIMARY, "AI_P_DOORS")
        self.assertEqual(LN.DOOR_SECONDARY, "AI_S_DOORS")
        self.assertEqual(got[LN.DOOR_PRIMARY], {("exit", "primary"), ("shaft", "primary")})
        self.assertEqual(got[LN.DOOR_SECONDARY], {("cubicle", "secondary")})

    def test_older_door_layers_still_read(self):
        for name in ("AI_DOOR_PRIMARY", "AI_DOOR_SECONDARY", "AI_P_DOORS", "AI_S_DOORS"):
            self.assertTrue(LN.is_door_layer(name))
            self.assertTrue(LN.is_reserved(name))
        self.assertEqual(LN.door_layer_level("AI_DOOR_PRIMARY"), "primary")
        self.assertEqual(LN.door_layer_level("AI_S_DOORS"), "secondary")


class ExitsListed(unittest.TestCase):
    """compute_room_areas lists each room's ways out from AI_DOOR."""

    @classmethod
    def setUpClass(cls):
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        msp.add_lwpolyline(rect(0, 0, 10000, 8000), close=True, dxfattribs={"layer": "FLOOR"})
        for pts, layer, name in ((rect(200, 200, 4800, 7800), "AI_ROOM", "OFFICE"),
                                 (rect(5000, 200, 9800, 7800), "AI_CIRC", "LOBBY"),
                                 (rect(5000, 8000, 6000, 9000), "AI_SHAFT", "SHAFT")):
            msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            msp.add_text(name, dxfattribs={"insert": ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2), "height": 200})
        msp.add_lwpolyline(rect(4800, 3000, 5000, 3900), close=True, dxfattribs={"layer": LN.DOOR})
        msp.add_lwpolyline(rect(6000, 7800, 6900, 8000), close=True, dxfattribs={"layer": LN.DOOR})   # lobby to outside
        msp.add_lwpolyline(rect(1000, 4000, 1800, 4012), close=True, dxfattribs={"layer": LN.DOOR_CUBICLE})
        cls.path = os.path.join(tempfile.mkdtemp(), "exits.dxf")
        doc.saveas(cls.path)
        cls.rooms = {r["name"]: r for r in dr.compute_room_areas(cls.path, "FLOOR")["rooms"]}

    def test_the_office_has_one_exit_into_the_lobby(self):
        self.assertEqual(self.rooms["OFFICE"]["exits"], 1)
        self.assertEqual(self.rooms["OFFICE"]["exits_to"], ["LOBBY"])

    def test_the_lobby_has_two(self):
        self.assertEqual(self.rooms["LOBBY"]["exits"], 2)
        self.assertEqual(sorted(self.rooms["LOBBY"]["exits_to"]), ["", "OFFICE"])

    def test_cubicle_doors_are_not_exits(self):
        self.assertEqual(self.rooms["SHAFT"]["exits"], 0)


def hatch(pts, pattern="ANSI37", solid=False, layer="A-HATCH"):
    return (list(pts), [], layer, pattern, solid)


class ThinByThickness(unittest.TestCase):
    """A 12 m wall traced from its faces, and an L-shaped 200 mm toilet
    wall of the same hatch apart from it, its faces on HVAC COMMENTS: 3 m x
    1.3 m by its box, 200 mm thick."""

    L = [(0, 5000), (3000, 5000), (3000, 5200), (200, 5200), (200, 6300), (0, 6300)]

    def run_trace(self, edges=True):
        segs = outline(rect(0, 0, 12000, 200))
        if edges:
            segs += outline(self.L, layer="HVAC COMMENTS")
        hatches = [hatch(rect(0, 0, 12000, 200)), hatch(self.L)]
        return wt.trace_floor(segs, hatches, [], PLATE)

    def test_the_l_wall_is_wall(self):
        res = self.run_trace()
        T = unary_union([Polygon(s["pts"]).buffer(0) for s in res["strips"]])
        L = Polygon(self.L)
        self.assertGreater(T.intersection(L).area, 0.98 * L.area)
        self.assertFalse(res["review_blocks"])

    def test_with_no_lines_along_it_it_stays_for_review(self):
        res = self.run_trace(edges=False)
        T = unary_union([Polygon(s["pts"]).buffer(0) for s in res["strips"]])
        self.assertLess(T.intersection(Polygon(self.L)).area, 1.0)
        self.assertEqual(len(res["review_blocks"]), 1)

    def test_a_sliver_across_a_room_is_not_a_wall(self):
        """A 70 mm hatch sliver running corner to corner from the wall, no
        lines along it: thin, beside the wall, but not edged."""
        sliver = [(0, 200), (70, 200), (6070, 5200), (6000, 5200)]
        segs = outline(rect(0, 0, 12000, 200))
        res = wt.trace_floor(segs, [hatch(rect(0, 0, 12000, 200)), hatch(sliver)], [], PLATE)
        T = unary_union([Polygon(s["pts"]).buffer(0) for s in res["strips"]])
        self.assertLess(T.intersection(Polygon(sliver)).area, 0.05 * Polygon(sliver).area)

    def test_a_short_square_piece_stays_a_column(self):
        """500 x 600 at the wall's end, its outline drawn: eroded by 225 mm a
        speck is left, but it is short - a column, not a wall."""
        col = rect(12000, -200, 12500, 400)
        segs = outline(rect(0, 0, 12000, 200)) + outline(col, layer="A-COLUMN")
        res = wt.trace_floor(segs, [hatch(rect(0, 0, 12000, 200)), hatch(col)], [], PLATE)
        self.assertEqual(len(res["columns"]), 1)
        T = unary_union([Polygon(s["pts"]).buffer(0) for s in res["strips"]])
        self.assertLess(T.intersection(Polygon(col)).area, 0.05 * Polygon(col).area)


class SolidIsNotAShaft(unittest.TestCase):
    """A 1450 x 800 block at the end of a 12 m wall, a SHAFT label beside it."""

    BLOCK = rect(12000, -600, 13450, 200)

    def run_trace(self, solid):
        segs = outline(rect(0, 0, 12000, 200))
        pat = "SOLID" if solid else "ANSI31"
        hatches = [hatch(rect(0, 0, 12000, 200), pat, solid), hatch(self.BLOCK, pat, solid)]
        return wt.trace_floor(segs, hatches, [{"text": "ELEC SHAFT", "x": 12700, "y": 1200}], PLATE)

    def test_solid_fill_is_wall(self):
        res = self.run_trace(True)
        self.assertFalse(res["shafts"])
        T = unary_union([Polygon(s["pts"]).buffer(0) for s in res["strips"]])
        self.assertGreater(T.intersection(Polygon(self.BLOCK)).area, 0.98 * Polygon(self.BLOCK).area)

    def test_with_no_label_against_the_wall_it_is_wall(self):
        segs = outline(rect(0, 0, 12000, 200))
        hatches = [hatch(rect(0, 0, 12000, 200), "SOLID", True), hatch(self.BLOCK, "SOLID", True)]
        res = wt.trace_floor(segs, hatches, [], PLATE)
        T = unary_union([Polygon(s["pts"]).buffer(0) for s in res["strips"]])
        self.assertGreater(T.intersection(Polygon(self.BLOCK)).area, 0.98 * Polygon(self.BLOCK).area)
        old = wt.SOLID_AGAINST_WALL
        wt.SOLID_AGAINST_WALL = False
        try:
            res = wt.trace_floor(segs, hatches, [], PLATE)
        finally:
            wt.SOLID_AGAINST_WALL = old
        self.assertEqual(len(res["review_blocks"]), 1)

    def test_a_cross_hatched_one_is_still_a_shaft(self):
        res = self.run_trace(False)
        self.assertEqual(len(res["shafts"]), 1)


class NotAColumnInAWall(unittest.TestCase):

    def faces(self):
        # a 200 mm wall's two faces running on through a 755 x 200 jamb
        return ge._wall_faces([seg(0, 0, 5000, 0), seg(0, 200, 5000, 200)])

    def test_a_jamb_in_a_wall_line(self):
        self.assertTrue(ge._in_wall_line(box(3000, 0, 3755, 200), self.faces()))

    def test_a_square_column_is_a_column(self):
        self.assertFalse(ge._in_wall_line(box(3000, -200, 3600, 400), self.faces()))

    def test_a_thin_piece_with_no_wall_faces(self):
        self.assertFalse(ge._in_wall_line(box(3000, 1000, 3755, 1200), self.faces()))
        none = ge._wall_faces([seg(0, 0, 5000, 0, "S-BEAM"), seg(0, 200, 5000, 200, "S-BEAM")])
        self.assertFalse(ge._in_wall_line(box(3000, 0, 3755, 200), none))

    def test_one_face_only_is_not_enough(self):
        one = ge._wall_faces([seg(0, 0, 5000, 0)])
        self.assertFalse(ge._in_wall_line(box(3000, 0, 3755, 200), one))

    def test_the_column_group_drops_it(self):
        """Nine 600 mm columns on an 8 m grid on A-COLUMN, and a 755 x 200
        jamb on the same layer standing in a wall line."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        for i in range(3):
            for j in range(3):
                x, y = 2000 + 8000 * i, 2000 + 8000 * j
                msp.add_lwpolyline(rect(x - 300, y - 300, x + 300, y + 300), close=True, dxfattribs={"layer": "A-COLUMN"})
        msp.add_lwpolyline(rect(5000, 6000, 5755, 6200), close=True, dxfattribs={"layer": "A-COLUMN"})
        floors = [{"name": "F", "polygon": rect(-1000, -1000, 21000, 21000)}]
        segs = [seg(3000, 6000, 9000, 6000), seg(3000, 6200, 9000, 6200)]
        without = ge._column_shapes(doc, msp, dr.MM_UNITS, floors)["F"]
        withsegs = ge._column_shapes(doc, msp, dr.MM_UNITS, floors, segs=segs)["F"]
        self.assertEqual(len(without), 10)
        self.assertEqual(len(withsegs), 9)
        self.assertFalse(any(pg.intersects(box(5000, 6000, 5755, 6200)) for pg, _l, _h in withsegs))


if __name__ == "__main__":
    unittest.main()
