# -*- coding: utf-8 -*-
"""Rooms closed at the doors drawn, staircase parts, lift labels, and every
door primary or secondary.

  * the door openings found for the drawing close the rooms too: a door
    between two short returns no longer lets its room run out (H10 Tower 1,
    G. TOILET);
  * a flight of treads drawn on a structure layer behind a wall across the
    stair core is the other part of the staircase beside it, named as it;
  * a space labelled as a lift with two hinged doors opening into it is not
    a lift well;
  * a door with the main accessible area on one side is primary, one
    reached only through another room secondary (a cubicle's always);
  * two X-marked strips face to face with no wall between are one shaft
    (rule join_shaft_strips, off by default);
  * a door with no wall gap that shuts from one wall-line end to another
    (a single-line shaft box, its door block drawn with two swings) is
    found and closes the shaft.

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

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import elements as el  # noqa: E402
from Drawing_Analyzer import layernames as LN  # noqa: E402


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


class DoorLevels(unittest.TestCase):
    """A lobby (main area), a room off it, a shaft behind the room, a WC
    cubicle inside the room."""

    LOBBY = box(0, 3200, 9000, 6000)
    ROOM = box(0, 0, 3000, 3000)
    SHAFT = box(3200, 0, 4400, 3000)
    INNER = box(5000, 0, 8000, 3000)          # reached only through ROOM2
    ROOM2 = box(8200, 0, 9000, 3000)

    def levels(self, quads, kinds=None, opens=()):
        rooms = [self.LOBBY, self.ROOM, self.SHAFT]
        main = [True, False, False]
        shaft = [False, False, True]
        kinds = kinds or el.door_kinds(quads, rooms, shaft)
        return el.door_levels(quads, rooms, main, shaft, kinds, opens)

    def test_entry_from_the_lobby_is_primary(self):
        (lev, a, b, within), = self.levels([rect(1000, 3000, 1900, 3200)])
        self.assertEqual(lev, el.PRIMARY)
        self.assertEqual({a, b}, {0, 1})
        self.assertFalse(within)

    def test_a_shaft_door_from_the_room_is_primary(self):
        """2026-10-17: a door between two spaces is primary, wherever it is
        reached from."""
        (lev, _a, _b, _w), = self.levels([rect(3000, 1000, 3200, 1700)])
        self.assertEqual(lev, el.PRIMARY)

    def test_the_same_room_on_both_sides_is_secondary(self):
        """A door in a wall with the room on both sides (traced through its
        own door): inside one room."""
        (lev, a, b, _w), = self.levels([rect(1000, 1400, 1900, 1600)], kinds=[(el.EXIT, 1, 1)])
        self.assertEqual((a, b), (1, 1))
        self.assertEqual(lev, el.SECONDARY)

    def test_a_shaft_door_from_the_lobby_is_primary(self):
        (lev, _a, _b, _w), = self.levels([rect(3400, 3000, 4200, 3200)])
        self.assertEqual(lev, el.PRIMARY)

    def test_a_cubicle_door_is_secondary(self):
        q = rect(1000, 1494, 1600, 1506)
        (lev, _a, _b, _w), = self.levels([q], kinds=[(el.CUBICLE, 1, 1)])
        self.assertEqual(lev, el.SECONDARY)

    def test_nothing_traced_beyond_is_the_common_floor(self):
        (lev, a, b, _w), = self.levels([rect(-200, 1000, 0, 1900)])
        self.assertEqual(lev, el.PRIMARY)
        self.assertIn(None, (a, b))

    def test_two_rooms_reached_through_each_other(self):
        rooms = [self.LOBBY, self.ROOM2, self.INNER]
        q1, q2 = rect(8300, 3000, 8900, 3200), rect(8000, 1000, 8200, 1900)
        out = el.door_levels([q1, q2], rooms, [True, False, False], [False] * 3)
        self.assertEqual([o[0] for o in out], [el.PRIMARY, el.PRIMARY])   # each between two rooms

    def test_untraced_floor_joined_to_the_lobby_is_main(self):
        """A passage nobody named, open to the lobby: the room off it is
        entered from the main area."""
        lobby, room = box(0, 0, 3000, 3000), box(6200, 0, 9000, 3000)
        plate = box(-100, -100, 9100, 3100)
        opens = el.open_floor(plate, [lobby, room], None, [rect(6000, 1000, 6200, 1900)])
        self.assertEqual(len(opens), 1)                      # the passage 3000..6000
        out = el.door_levels([rect(6000, 1000, 6200, 1900)], [lobby, room], [True, False], [False, False],
                             opens=opens)
        self.assertEqual(out[0][0], el.PRIMARY)

    def test_no_labels_rooms_behind_doors(self):
        """The final drawing reads no labels: the floor's largest untraced
        space is the common floor, a space behind a door off it is a room
        and a space behind that a room beyond - both doors stand between two
        spaces: primary (2026-10-17)."""
        plate = box(0, 0, 9000, 6000)
        wall = box(0, 2900, 9000, 3100).union(box(2900, 0, 3100, 2900))
        d1, d2 = rect(1000, 2900, 1900, 3100), rect(2900, 1000, 3100, 1900)
        opens = el.open_floor(plate, [], wall, [d1, d2])
        self.assertEqual(len(opens), 3)
        out = el.door_levels([d1, d2], [], [], [], opens=opens)
        self.assertEqual([o[0] for o in out], [el.PRIMARY, el.PRIMARY])

    def test_main_rooms_by_family_and_name(self):
        self.assertTrue(el.is_main_room("LIFT LOBBY", "Lift Lobby"))
        self.assertTrue(el.is_main_room("CORRIDOR"))
        self.assertTrue(el.is_main_room("OFFICE SPACE 843.61 SQ.MTS", "Habitable Room"))
        self.assertTrue(el.is_main_room("STAIRCASE", "Staircase"))
        self.assertFalse(el.is_main_room("JANITOR"))
        self.assertFalse(el.is_main_room("PASSENGER LIFT", "Lift"))
        self.assertFalse(el.is_main_room("STAIR PRESS", "Shaft"))


class DoorTags(unittest.TestCase):

    def test_kind_and_level_go_with_the_door(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        n = dr.draw_doors(doc, msp, dr.MM_UNITS, [rect(0, 0, 900, 200), rect(2000, 0, 2600, 12)],
                          kinds=["exit", "cubicle"], levels=["primary", "secondary"],
                          sides=[("LOBBY", "OFFICE"), ("TOILET", "TOILET")])
        self.assertEqual(n, {"primary": 1, "secondary": 1})
        got = sorted((e.dxf.layer, dr.door_tag(e)) for e in msp.query("LWPOLYLINE"))
        self.assertEqual(got, [(LN.DOOR_PRIMARY, ("exit", "primary")), (LN.DOOR_SECONDARY, ("cubicle", "secondary"))])

    def test_older_layers_are_still_read(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        for layer in (LN.DOOR, LN.DOOR_SHAFT, LN.DOOR_CUBICLE):
            msp.add_lwpolyline(rect(0, 0, 900, 200), close=True, dxfattribs={"layer": layer})
        got = [dr.door_tag(e) for e in msp.query("LWPOLYLINE")]
        self.assertEqual(got, [("exit", "primary"), ("shaft", "primary"), ("cubicle", "secondary")])


class DoorClosures(unittest.TestCase):

    def test_an_open_door_is_closed_on_both_faces(self):
        q = rect(7800, 6800, 8000, 7900)                    # 200 deep, 1100 wide
        got = dr._door_quad_closures([q], [])
        self.assertEqual(len(got), 2)
        for x1, y1, x2, y2 in got:
            self.assertAlmostEqual(abs(y2 - y1), 1130, delta=1)      # carried 15 mm past each end
            self.assertIn(round(x1), (7800, 8000))

    def test_a_door_the_wall_faces_already_close_is_left(self):
        q = rect(7800, 6800, 8000, 7900)
        self.assertEqual(dr._door_quad_closures([q], [(7800, 6800, 7800, 7900), (8000, 6800, 8000, 7900)]), [])

    def test_a_cubicle_board_door_is_left_open(self):
        self.assertEqual(dr._door_quad_closures([rect(1000, 1494, 1600, 1506)], []), [])


class FinalDrawingSolid(unittest.TestCase):
    """The final drawing's walls for the common-floor search, in the shape
    draw_walls takes them: {layer: [[outline, hole, ...], ...]}."""

    def test_a_wall_with_a_hole(self):
        from Drawing_Analyzer import geoengine as ge
        walls = {"AI_WALL": [[rect(0, 0, 3000, 3000), rect(200, 200, 2800, 2800)], [rect(5000, 0, 5200, 3000)]]}
        g = ge.door_solid(walls)
        self.assertAlmostEqual(g.area, 3000 * 3000 - 2600 * 2600 + 200 * 3000, delta=1)

    def test_a_bare_ring_is_read_too(self):
        from Drawing_Analyzer import geoengine as ge
        self.assertAlmostEqual(ge.door_solid({"AI_WALL": [rect(0, 0, 200, 3000)]}).area, 600000, delta=1)


class DoorBetweenWallEnds(unittest.TestCase):
    """XA typical floor: a PHE shaft drawn as three single lines, its door
    block flattened with two swings on one hinge - no wall gap, no open
    leaf. The door shuts from one wall-line end to the other."""

    @staticmethod
    def seg(x1, y1, x2, y2, layer="A-WALL"):
        return (x1, y1, x2, y2, layer, math.hypot(x2 - x1, y2 - y1), -1, False, False)

    @staticmethod
    def swing(hx, hy, r, shut_deg, open_deg):
        a = (hx + r * math.cos(math.radians(shut_deg)), hy + r * math.sin(math.radians(shut_deg)))
        b = (hx + r * math.cos(math.radians(open_deg)), hy + r * math.sin(math.radians(open_deg)))
        return (hx, hy, r, a, b, abs(open_deg - shut_deg))

    def box(self):
        return [self.seg(0, 0, 0, 620), self.seg(0, 0, 500, 0), self.seg(0, 620, 500, 620)]

    def test_a_shaft_door_between_two_returns(self):
        arcs = [self.swing(500, 620, r, -90, -2) for r in (476, 570)]
        got = dr.door_openings(arcs, self.box(), [])
        self.assertEqual(len(got), 1)
        x0, y0, x1, y1 = Polygon(got[0]).bounds
        self.assertEqual((round(x0), round(y0), round(x1), round(y1)), (450, 0, 550, 620))
        # deep enough not to be a board: it closes the shaft
        self.assertEqual(len(dr._door_quad_closures(got, [])), 2)

    def test_nothing_where_it_shuts_is_no_door(self):
        segs = [self.seg(0, 0, 0, 620), self.seg(0, 620, 500, 620)]     # no return at the shut end
        arcs = [self.swing(500, 620, r, -90, -2) for r in (476, 570)]
        self.assertEqual(dr.door_openings(arcs, segs, []), [])


class StripPairs(unittest.TestCase):
    """Two 2160 x 300 X-marked strips 400 mm apart (H10's PHE shafts)."""

    def strips(self):
        def d(y0):
            return {"source": "X", "bbox": (0, y0, 2160, y0 + 300), "poly": rect(0, y0, 2160, y0 + 300),
                    "holes": [], "label": dr.X_UNKNOWN_LABEL, "cx": 1080, "cy": y0 + 150, "xrects": [], "sr_no": None,
                    "absorbed_srs": []}
        return [d(0), d(700)]

    def shape_of(self, d):
        return Polygon(d["poly"]).buffer(0)

    def set_shape(self, d, g, _pt):
        d["poly"] = list(g.exterior.coords)[:-1]
        d["bbox"] = g.bounds
        return True

    def test_one_shaft(self):
        m = self.strips()
        self.assertEqual(dr._join_x_strip_pairs(m, [], self.shape_of, self.set_shape), 1)
        keep = [d for d in m if not d.get("_joined_away")]
        self.assertEqual(len(keep), 1)
        self.assertAlmostEqual(Polygon(keep[0]["poly"]).area / 1e6, 2.16, delta=0.01)

    def test_a_wall_between_keeps_two(self):
        m = self.strips()
        wall = [(0, 450, 2160, 450), (0, 650, 2160, 650)]
        self.assertEqual(dr._join_x_strip_pairs(m, wall, self.shape_of, self.set_shape), 0)


class FloorRun(unittest.TestCase):
    """One floor through annotate_dxf:

      * G. TOILET 8 x 6 m; its door in the left wall stands between a 300
        mm nib and the corner of the lobby's wall - the corridor beyond;
      * a staircase labelled STAIRCASE, and behind a 200 mm wall across the
        core a flight of nine treads drawn on COLUMN, no name;
      * a lobby labelled LIFT WELL 3100X2300 with a door each side;
      * WC cubicle doors in 12 mm boards inside the toilet."""

    @classmethod
    def run_floor(cls, overrides=None):
        tmp = tempfile.mkdtemp()
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        plate = rect(0, 0, 40000, 30000)
        msp.add_lwpolyline(plate, close=True, dxfattribs={"layer": "A-FLOOR"})

        def r(x0, y0, x1, y1, layer="A-WALL"):
            msp.add_lwpolyline(rect(x0, y0, x1, y1), close=True, dxfattribs={"layer": layer})

        def door(hx, hy, rad, a0, a1):
            msp.add_arc((hx, hy), rad, a0, a1, dxfattribs={"layer": "A-DOOR"})
            msp.add_line((hx, hy), (hx + rad * math.cos(math.radians(a1)), hy + rad * math.sin(math.radians(a1))),
                         dxfattribs={"layer": "A-DOOR"})
        # G. TOILET: 8000..16000 x 6600..7600 by its cubicles, its wash
        # basins 2000..16000 x 2000..6400 under its lobby (2000..7800 x
        # 6600..8000, open to the corridor on its left). The door's jambs:
        # a nib whose faces are 500 and 100 mm long, and the end of the
        # lobby's floor wall - no wall face runs on to the door.
        r(1800, 8000, 8000, 8200)                   # over the lobby
        r(8000, 7600, 16200, 7800)                  # over the cubicles, 200 lower
        r(1800, 1800, 16200, 2000)                  # bottom
        r(16000, 2000, 16200, 7600)                 # right
        r(1800, 2000, 2000, 6400)                   # left, below the lobby
        r(2000, 6400, 8000, 6600)                   # the lobby's floor wall, ending at the door
        r(7800, 7500, 8000, 8000)                   # the nib above the door
        door(7900, 7500, 900, 270, 360)             # hinge on the nib, leaf open into the toilet
        msp.add_mtext("G. TOILET", dxfattribs={"insert": (12000, 5000), "char_height": 200, "layer": "A-TEXT"})
        for k in range(4):                          # cubicles: 12 mm boards and their doors
            x = 10000 + 1500 * k
            r(x, 6000, x + 12, 7600, "A-TOILET")
            msp.add_arc((x + 100, 6000), 600, 0, 90, dxfattribs={"layer": "A-TOILET"})
            msp.add_line((x + 100, 6000), (x + 100, 6600), dxfattribs={"layer": "A-TOILET"})
        # staircase: 20000..24800 x 10000..11900 labelled, wall 9800..10000, flight below 8200..9800
        r(19800, 11900, 25000, 12100)
        r(19800, 9800, 25000, 10000)
        r(19800, 8000, 25000, 8200)
        r(19800, 8200, 20000, 11900)
        r(24800, 8200, 25000, 11900)
        msp.add_mtext("STAIRCASE", dxfattribs={"insert": (22400, 10900), "char_height": 200, "layer": "A-TEXT"})
        for k in range(9):
            x = 21000 + 300 * k
            msp.add_line((x, 8200), (x, 9800), dxfattribs={"layer": "COLUMN"})
        # a lobby labelled LIFT WELL 3100X2300: 28000..31100 x 2000..4300, a door each side
        r(27800, 4300, 31300, 4500)
        r(27800, 1800, 31300, 2000)
        r(27800, 2000, 28000, 2700)
        r(27800, 3600, 28000, 4300)
        r(31100, 2000, 31300, 2700)
        r(31100, 3600, 31300, 4300)
        door(27900, 2700, 900, 0, 90)
        door(31200, 2700, 900, 90, 180)
        msp.add_mtext("LIFT WELL 3100X2300", dxfattribs={"insert": (29550, 3150), "char_height": 200, "layer": "A-TEXT"})
        src = os.path.join(tmp, "in.dxf")
        doc.saveas(src)
        floors = [{"name": "F1", "polygon": plate}]
        rows = dr.extract_texts(src, floors)
        out = os.path.join(tmp, "out.dxf")
        stats = dr.annotate_dxf(src, out, floors, rows, rule_overrides=overrides)
        areas = dr.compute_room_areas(out, "AI_FLOOR")
        return stats, areas, ezdxf.readfile(out).modelspace()

    @classmethod
    def setUpClass(cls):
        cls.stats, cls.areas, cls.msp = cls.run_floor()

    def rooms(self, word):
        return [r for r in self.areas["rooms"] if word in r["name"]]

    TOILET_SQM = 14.0 * 4.4 + 8.0 * 1.2             # basins + cubicles, not the lobby

    def test_the_toilet_stops_at_its_door(self):
        """Its basins under the lobby are the toilet's; the lobby is not.
        (On H10 Tower 1 the faces carried across missed this door and the
        door openings closed it: door_closing_from_openings there.)"""
        (t,), = [self.rooms("G. TOILET")]
        self.assertAlmostEqual(t["area_sqm"], self.TOILET_SQM, delta=0.8)

    def test_the_flight_behind_the_wall_is_the_staircase(self):
        stairs = sorted(r["area_sqm"] for r in self.rooms("STAIRCASE"))
        self.assertEqual(len(stairs), 2)
        self.assertAlmostEqual(stairs[0], 4.8 * 1.6, delta=0.4)
        self.assertGreaterEqual(self.stats.get("geometry_staircase_parts", 0), 1)

    def test_the_lift_label_with_doors_is_refused(self):
        self.assertEqual(self.rooms("LIFT WELL"), [])
        self.assertEqual(self.stats.get("lift_label_refused_doors"), 1)

    def test_doors_by_level(self):
        layers = {}
        for e in self.msp.query("LWPOLYLINE"):
            if LN.is_door_layer(e.dxf.layer):
                layers.setdefault(e.dxf.layer, []).append(dr.door_tag(e))
        self.assertIn(("exit", "primary"), layers.get(LN.DOOR_PRIMARY, []))
        self.assertTrue(all(t[1] == "secondary" for t in layers.get(LN.DOOR_SECONDARY, [])))
        toilet = self.rooms("G. TOILET")[0]
        self.assertGreaterEqual(toilet["doors_primary"], 1)


if __name__ == "__main__":
    unittest.main()
