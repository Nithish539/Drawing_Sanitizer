# -*- coding: utf-8 -*-
"""Every door belongs to one room, and its line is on that room's inner face
of the wall (2026-10-17):

  * the room is decided from what stands on the two sides of the opening:
    a lift or shaft, then a room, a staircase, a lobby, a corridor, the
    common floor - the higher wins, how big a room is never counts; two
    like rooms: its only door, the side its swing is drawn on, fewer doors;
  * a door that swings out into the corridor (a shaft's, an electrical
    room's) still belongs to the room behind it;
  * no room reaches into a door opening (it stops at its own face); a room
    that was only the doorway (a threshold) is dropped; a room on both
    sides of its door, a cubicle's board door and an X of the X sequence
    are left as they are;
  * a floor-finish note (10MM DROP IN FINISH) is never a room's name;
  * the final drawing keeps the face Step 3 gave the same door.

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


def face_y(quad, side):
    """The y of the face `side` of a door opening in a wall along x."""
    ln = el.owner_face(quad, side)
    return round((ln[0][1] + ln[1][1]) / 2.0)


DOOR = rect(1000, 0, 2000, 200)                       # an opening in a 200 mm wall along x


class Ranks(unittest.TestCase):

    def test_the_order(self):
        R = el.door_room_rank
        self.assertEqual(R("ELE SHAFT 750X1200", ""), el.OWNER_SHAFT)
        self.assertEqual(R("LIFT WELL 2650X2650", ""), el.OWNER_SHAFT)
        self.assertEqual(R("PASSENGER LIFT", "Lift"), el.OWNER_SHAFT)
        self.assertEqual(R("LOBBY PRESS. 750X1140", ""), el.OWNER_SHAFT)    # a pressurisation shaft
        self.assertEqual(R("MALE TOILET", "Toilet"), el.OWNER_ROOM)
        self.assertEqual(R("AHU 5000 x 4400", ""), el.OWNER_ROOM)
        self.assertEqual(R("2000 WD BALCONY", ""), el.OWNER_ROOM)
        self.assertEqual(R("STAIRCASE", "Staircase"), el.OWNER_STAIR)
        self.assertEqual(R("FIRE TOWER 3100X4400", ""), el.OWNER_LOBBY)
        self.assertEqual(R("LOWER ZONE 4000 WD LIFT LOBBY", ""), el.OWNER_LOBBY)
        self.assertEqual(R("FIRE LIFT LOBBY 5.0MX2.8M", "Lift Lobby"), el.OWNER_LOBBY)
        self.assertEqual(R("CORRIDOR", ""), el.OWNER_CIRCULATION)
        self.assertEqual(R("VOID", "Void"), el.OWNER_NONE)


class Owners(unittest.TestCase):
    """One opening, y 0..200: a room above (y 200 on) and below (y 0 down)."""

    ABOVE = Polygon(rect(0, 200, 3000, 3000))
    BELOW = Polygon(rect(0, -3000, 3000, 0))

    def owner_y(self, rooms, ranks, swings=(), quads=None):
        quads = quads or [DOOR]
        side, who, how = el.door_owners(quads, rooms, ranks, swings)[0]
        return (None if side is None else face_y(quads[0], side)), who, how

    def test_a_room_and_the_common_floor(self):
        y, who, how = self.owner_y([self.ABOVE], [el.OWNER_ROOM])
        self.assertEqual((y, who, how), (200, 0, "rank"))

    def test_a_shaft_over_a_toilet(self):
        y, who, _ = self.owner_y([self.ABOVE, self.BELOW], [el.OWNER_ROOM, el.OWNER_SHAFT])
        self.assertEqual((y, who), (0, 1))                        # the shaft's face

    def test_a_room_off_a_lobby_however_big(self):
        big = Polygon(rect(-20000, -20000, 20000, 0))             # the balcony, 10 x the lobby
        y, who, _ = self.owner_y([self.ABOVE, big], [el.OWNER_LOBBY, el.OWNER_ROOM])
        self.assertEqual((y, who), (0, 1))

    def test_the_staircase_off_its_fire_tower(self):
        y, who, _ = self.owner_y([self.ABOVE, self.BELOW], [el.OWNER_LOBBY, el.OWNER_STAIR])
        self.assertEqual((y, who), (0, 1))

    def test_two_rooms_the_one_it_is_the_only_door_of(self):
        """A janitor's room off a toilet: the toilet has its own way out."""
        quads = [DOOR, rect(1000, -3200, 2000, -3000)]             # the toilet's door to the corridor
        side, who, how = el.door_owners(quads, [self.ABOVE, self.BELOW], [el.OWNER_ROOM, el.OWNER_ROOM])[0]
        self.assertEqual((face_y(DOOR, side), who, how), (200, 0, "only door"))

    def test_two_like_rooms_by_the_swing(self):
        """Each has another door: the side the door is drawn on decides."""
        quads = [DOOR, rect(1000, 3000, 2000, 3200), rect(1000, -3200, 2000, -3000)]
        # hinged at the lower face's jamb, the leaf open downwards (into the room below)
        swing = (1000.0, 0.0, 1000.0, (2000.0, 0.0), (1000.0, -1000.0), 90.0)
        side, who, how = el.door_owners(quads, [self.ABOVE, self.BELOW], [el.OWNER_ROOM] * 2, [swing])[0]
        self.assertEqual((face_y(DOOR, side), who, how), (0, 1, "swing"))
        # no swing: not settled (door_line's face stands)
        side, who, how = el.door_owners(quads, [self.ABOVE, self.BELOW], [el.OWNER_ROOM] * 2)[0]
        self.assertEqual((side, how), (None, "undecided"))

    def test_the_same_room_on_both_sides_is_not_settled(self):
        room = Polygon(rect(0, -3000, 3000, 3000)).difference(box(-1, 0, 999, 200)).difference(box(2001, 0, 3001, 200))
        side, who, how = el.door_owners([DOOR], [room], [el.OWNER_ROOM])[0]
        self.assertEqual((side, how), (None, "same room both sides"))

    def test_a_swing_out_into_the_corridor_does_not_move_a_shafts_door(self):
        swing = (1000.0, 0.0, 1000.0, (2000.0, 0.0), (1000.0, -1000.0), 90.0)   # opens into the corridor below
        y, who, how = self.owner_y([self.ABOVE], [el.OWNER_SHAFT], [swing])
        self.assertEqual((y, who, how), (200, 0, "rank"))


class Doorways(unittest.TestCase):

    def test_a_room_grown_into_its_doorway_stops_at_its_face(self):
        room = Polygon(rect(0, 0, 3000, 3000))                     # down to the corridor face, over the opening
        quad = rect(1000, 0, 2000, 200)
        (g,), sqm = el.rooms_out_of_doorways([room], [quad])
        self.assertAlmostEqual(g.area, room.area - 1000 * 200, delta=1)
        self.assertAlmostEqual(sqm, 0.2, places=3)

    def test_a_threshold_is_dropped(self):
        quad = rect(1000, 0, 2000, 200)
        (g,), _ = el.rooms_out_of_doorways([Polygon(quad)], [quad])
        self.assertIsNone(g)

    def test_left_alone(self):
        quad = rect(1000, 0, 2000, 200)
        # a room on both sides of its door (traced through it)
        through = Polygon(rect(0, -3000, 3000, 3000))
        (g,), _ = el.rooms_out_of_doorways([through], [quad])
        self.assertIs(g, through)
        # a cubicle's board door
        board = rect(1000, 0, 1600, 12)
        room = Polygon(rect(0, 0, 3000, 3000))
        (g,), _ = el.rooms_out_of_doorways([room], [board])
        self.assertIs(g, room)
        # an X of the X sequence
        (g,), _ = el.rooms_out_of_doorways([room], [quad], skip=[True])
        self.assertIs(g, room)


class Notes(unittest.TestCase):

    def test_a_finish_note_names_no_room(self):
        for t in ("10MM DROP IN FINISH", "10 MM DROP", "15MM. SUNKEN", "DROP IN FFL"):
            self.assertTrue(dr.is_ignorable_label(t), t)
        for t in ("JANITOR", "DROP OFF", "FEMALE TOILET"):
            self.assertFalse(dr.is_ignorable_label(t), t)


class Drawn(unittest.TestCase):

    def test_draw_doors_puts_the_line_on_the_owners_face(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        n = dr.draw_doors(doc, msp, dr.MM_UNITS, [DOOR, DOOR], kinds=["exit", "exit"], levels=["primary", "primary"],
                          sides=[("TOILET", "(common floor)"), ("STORE", "LOBBY")], owners=[0, 1])
        self.assertEqual(n, {"primary": 2})
        lines = [e for e in msp.query("LWPOLYLINE") if e.dxf.layer == LN.DOOR_PRIMARY]
        ys = [round(sum(p[1] for p in e.get_points()) / 2.0) for e in lines]
        self.assertEqual(sorted(ys), sorted([face_y(DOOR, 0), face_y(DOOR, 1)]))

    def test_the_final_drawing_keeps_step3s_face(self):
        step3 = [(Polygon(DOOR), (1500.0, 200.0))]                 # Step 3 drew it at y 200
        q2 = rect(1010, 0, 1990, 190)                              # the final drawing's opening there
        (side, t, owned), = dr.door_hints([q2], step3)
        self.assertEqual(face_y(q2, side), 190)
        self.assertFalse(owned)                                    # no way into a room stored
        ln = el.trace_line(q2, t)                                  # and its line where Step 3 drew it
        self.assertAlmostEqual((ln[0][1] + ln[1][1]) / 2.0, 200.0, places=3)
        self.assertEqual(dr.door_hints([rect(5000, 0, 6000, 200)], step3), [None])
        # Step 3's line at the shaft's front (its side walls' ends), the way
        # into its room stored: the side is the room's, not the line's
        step3 = [(Polygon(DOOR), (1500.0, 0.0), (0.0, 1.0))]
        (side, t, owned), = dr.door_hints([DOOR], step3)
        self.assertEqual(face_y(DOOR, side), 200)
        self.assertTrue(owned)
        self.assertAlmostEqual(t * el.door_axes(DOOR)[5], -100.0, places=3)


class FloorRun(unittest.TestCase):
    """One floor through annotate_dxf, a corridor along the bottom (the
    common floor, untraced) and off it:

      * a TOILET whose door swings out into the corridor, a 10MM DROP IN
        FINISH note standing in its doorway;
      * an ELEC SHAFT, its door swinging out into the corridor;
      * a LIFT LOBBY and, through a door in its side, a STORE larger than
        the lobby, the door swinging into the store."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        plate = rect(0, 0, 20000, 12000)
        msp.add_lwpolyline(plate, close=True, dxfattribs={"layer": "A-FLOOR"})

        def r(x0, y0, x1, y1, layer="A-WALL"):
            msp.add_lwpolyline(rect(x0, y0, x1, y1), close=True, dxfattribs={"layer": layer})

        def door(hx, hy, rad, a0, a1, leaf_deg):
            msp.add_arc((hx, hy), rad, a0, a1, dxfattribs={"layer": "A-DOOR"})
            msp.add_line((hx, hy), (hx + rad * math.cos(math.radians(leaf_deg)),
                                    hy + rad * math.sin(math.radians(leaf_deg))), dxfattribs={"layer": "A-DOOR"})

        def text(t, x, y, h=200):
            msp.add_mtext(t, dxfattribs={"insert": (x, y), "char_height": h, "layer": "A-TEXT"})
        # the corridor's wall, y 5800..6000, with the four doors' gaps
        for x0, x1 in ((1800, 3000), (4000, 8300), (9100, 12500), (13500, 20000)):
            r(x0, 5800, x1, 6000)
        # TOILET 2000..6000 x 6000..10000; door 3000..4000 opening out (hinge on the corridor face)
        r(1800, 6000, 2000, 10200)
        r(6000, 6000, 6200, 10200)
        r(1800, 10000, 6200, 10200)
        door(3000, 5800, 1000, 270, 360, 270)
        text("TOILET", 4000, 8000)
        text("10MM DROP IN FINISH", 3500, 5900, 60)
        # ELEC SHAFT 8000..9500 x 6000..8000; door 8300..9100 opening out
        r(7800, 6000, 8000, 8200)
        r(9500, 6000, 9700, 8200)
        r(7800, 8000, 9700, 8200)
        door(8300, 5800, 800, 270, 360, 270)
        text("ELEC SHAFT", 8750, 7000)
        # LIFT LOBBY 11000..15000 x 6000..10000; its door 12500..13500 opening in
        r(10800, 6000, 11000, 10200)
        r(10800, 10000, 15000, 10200)
        door(12500, 6000, 1000, 0, 90, 90)
        text("LIFT LOBBY", 13000, 8500)
        # STORE 15200..19800 x 6000..11800, through a door 7000..7900 in the
        # lobby's side wall, swinging into the store
        r(15000, 6000, 15200, 7000)
        r(15000, 7900, 15200, 12000)
        r(15200, 11800, 20000, 12000)
        r(19800, 6000, 20000, 11800)
        door(15200, 7000, 900, 0, 90, 0)
        text("STORE", 17500, 9000)
        src = os.path.join(tmp, "in.dxf")
        doc.saveas(src)
        floors = [{"name": "F1", "polygon": plate}]
        rows = dr.extract_texts(src, floors)
        out = os.path.join(tmp, "out.dxf")
        cls.stats = dr.annotate_dxf(src, out, floors, rows)
        cls.areas = dr.compute_room_areas(out, "AI_FLOOR")
        cls.msp = ezdxf.readfile(out).modelspace()
        cls.doors = []
        for e in cls.msp.query("LWPOLYLINE"):
            if e.dxf.layer in (LN.DOOR_PRIMARY, LN.DOOR_SECONDARY):
                pts = [p[:2] for p in e.get_points()]
                q = dr.door_quad(e)
                vals = [str(v) for c, v in e.get_xdata(dr.DOOR_APPID) if c == 1000]
                cls.doors.append({"mid": (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)),
                                  "quad": Polygon(q), "sides": vals[2:4]})

    def door_at(self, x, y):
        hit = [d for d in self.doors if d["quad"].buffer(50).contains(Polygon(rect(x - 1, y - 1, x + 1, y + 1)))]
        self.assertEqual(len(hit), 1, (x, y, [d["mid"] for d in self.doors]))
        return hit[0]

    def test_the_toilets_door_is_on_the_toilets_face(self):
        d = self.door_at(3500, 5900)
        self.assertAlmostEqual(d["mid"][1], 6000, delta=5)      # not the corridor face, though it opens out
        self.assertIn("TOILET", d["sides"][0])

    def test_the_shafts_door_is_on_the_shafts_face(self):
        d = self.door_at(8700, 5900)
        self.assertAlmostEqual(d["mid"][1], 6000, delta=5)
        self.assertIn("ELEC SHAFT", d["sides"][0])

    def test_the_lobbys_doors(self):
        d = self.door_at(13000, 5900)
        self.assertAlmostEqual(d["mid"][1], 6000, delta=5)      # the lobby's own door from the corridor
        s = self.door_at(15100, 7450)
        self.assertAlmostEqual(s["mid"][0], 15200, delta=5)     # the store's face, the larger room
        self.assertIn("STORE", s["sides"][0])
        self.assertIn("LIFT LOBBY", s["sides"][1])

    def test_no_room_from_the_finish_note(self):
        names = [" ".join(str(r["name"]).split()) for r in self.areas["rooms"]]
        self.assertFalse(any("DROP" in n for n in names), names)
        self.assertIn("TOILET", names)

    def test_counted(self):
        self.assertGreaterEqual(self.stats.get("doors_owner_rank", 0), 4)
        # every door here stands in a 200 mm wall: its line on the room's face (door_trace)
        self.assertGreaterEqual(self.stats.get("doors_trace_room_face", 0), 4)


if __name__ == "__main__":
    unittest.main()
