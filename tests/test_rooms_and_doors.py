# -*- coding: utf-8 -*-
"""Test_2: what a floor's rooms and doors settle together, and the rest of
that round.

  * a door is drawn as one line joining the walls either side, on the inner
    face of the wall of the room it opens into; the opening is kept in its
    XDATA and read back;
  * an X the X sequence made a void between two flights of a staircase is
    the stair's open well: a hole in the staircase (AI_VOID);
  * a room with a door that shares a side with an X-marked shaft is the
    shaft's room: AI_MEP, run to its door line;
  * a lift traced from its label with no door, its front on a machine
    room, is the machine room's - and the statement files it under MACHINE
    ROOM; an X lift is the X sequence's and stays a lift;
  * the wall left between the rooms (a hatched pillar, a thin strip);
  * a slab-edge line through the shafts is never a boundary;
  * AHU rooms on AI_AHUROOM.

Run:  python -m unittest discover -s tests -v
"""

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402
from shapely.geometry import Polygon, box  # noqa: E402

from Drawing_Analyzer import area_statement as A  # noqa: E402
from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import elementid as E  # noqa: E402
from Drawing_Analyzer import elements as el  # noqa: E402
from Drawing_Analyzer import layernames as LN  # noqa: E402
from Drawing_Analyzer import rules  # noqa: E402


def seg(x1, y1, x2, y2, layer="A-WALL", door=False):
    return (x1, y1, x2, y2, layer, math.hypot(x2 - x1, y2 - y1), -1, door, False)


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def room(label, x0, y0, x1, y1, source="P", **kw):
    d = {"label": label, "poly": rect(x0, y0, x1, y1), "holes": [], "bbox": (x0, y0, x1, y1),
         "source": source, "cx": (x0 + x1) / 2.0, "cy": (y0 + y1) / 2.0, "sr_no": None}
    d.update(kw)
    return d


def x_item(label, x0, y0, x1, y1):
    return {"label": label, "poly": [], "holes": [], "bbox": (x0, y0, x1, y1), "source": "X",
            "xrects": [(x0, y0, x1, y1)], "cx": (x0 + x1) / 2.0, "cy": (y0 + y1) / 2.0, "sr_no": None}


def fam_name(d):
    if d.get("family_forced"):
        return d["family_forced"]
    return dr.family_for_label(d.get("label"), dr._FAMILY_RULES)["name"]


class RulesTests(unittest.TestCase):

    def test_the_rules_exist_and_are_on(self):
        on = rules.defaults()
        for k in ("shaft_room_mep",
                  "lift_no_door_machine_room", "wall_gaps_filled", "slab_edge_not_boundary",
                  "balcony_open_to_refuge"):
            self.assertTrue(on[k], k)
        self.assertNotIn("stair_void_hole", on)          # the X sequence's (2026-10-16)


class DoorLineTests(unittest.TestCase):
    """A door: one line from wall end to wall end, on one face."""
    quad = rect(1000, 0, 1900, 200)                    # a 900 mm door in a 200 mm wall, y 0..200

    def test_on_the_face_of_the_room_it_opens_into(self):
        inside = Polygon(rect(0, 200, 4000, 3000))      # the room above the wall
        ln = el.door_line(self.quad, [inside])
        self.assertTrue(all(abs(p[1] - 200) < 1 for p in ln))
        self.assertAlmostEqual(abs(ln[1][0] - ln[0][0]), 900, delta=1)

    def test_of_two_rooms_the_smaller_ones_face(self):
        big, small = Polygon(rect(0, -5000, 6000, 0)), Polygon(rect(800, 200, 2200, 1700))
        ln = el.door_line(self.quad, [big, small])
        self.assertTrue(all(abs(p[1] - 200) < 1 for p in ln))

    def test_a_room_drawn_over_its_door_gets_the_line_nearer_its_body(self):
        # (Fix 2: rooms stop at their face; a room drawn over the opening
        # itself keeps the line on the face nearer its body)
        shaft_room = Polygon(rect(1000, 0, 1900, 900))
        ln = el.door_line(self.quad, [shaft_room])
        self.assertTrue(all(abs(p[1]) < 1 for p in ln))

    def test_drawn_as_a_line_and_read_back_as_the_opening(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        dr.draw_doors(doc, msp, dr.MM_UNITS, [self.quad], kinds=["shaft"], levels=["secondary"],
                      rooms=[Polygon(rect(0, 200, 4000, 3000))])
        ents = [e for e in msp if LN.is_door_layer(e.dxf.layer)]
        self.assertEqual(len(ents), 1)
        e = ents[0]
        self.assertFalse(e.closed)
        self.assertEqual(len(list(e.get_points("xy"))), 2)
        self.assertEqual(e.dxf.layer, LN.DOOR_SECONDARY)
        self.assertEqual(dr.door_tag(e), ("shaft", "secondary"))
        q = Polygon(dr.door_quad(e, dr.MM_UNITS))
        self.assertAlmostEqual(q.area, 900 * 200, delta=10)

    def test_a_line_drawn_by_hand_is_read_as_a_band(self):
        doc = ezdxf.new()
        e = doc.modelspace().add_lwpolyline([(0, 0), (900, 0)], dxfattribs={"layer": LN.DOOR_PRIMARY})
        q = Polygon(dr.door_quad(e, dr.MM_UNITS))
        self.assertAlmostEqual(q.area, 900 * 2 * dr.DOOR_LINE_BAND_MM, delta=10)

    def test_the_area_step_still_counts_the_doors(self):
        """compute_room_areas reads the opening a door line keeps."""
        import tempfile
        doc = ezdxf.new()
        doc.units = 4
        msp = doc.modelspace()
        msp.add_lwpolyline(rect(-1000, -6000, 8000, 6000), close=True, dxfattribs={"layer": "AI_FLOOR"})
        for name, r in (("STORE", rect(0, 200, 4000, 3000)), ("HALL", rect(0, -3000, 4000, 0))):
            msp.add_lwpolyline(r, close=True, dxfattribs={"layer": "AI_ROOM"})
            msp.add_mtext(name, dxfattribs={"layer": "AI_ROOM", "insert": (2000, (r[0][1] + r[2][1]) / 2)})
        dr.draw_doors(doc, msp, dr.MM_UNITS, [self.quad], rooms=[Polygon(rect(0, 200, 4000, 3000))])
        path = os.path.join(tempfile.mkdtemp(), "d.dxf")
        doc.saveas(path)
        areas = dr.compute_room_areas(path, "AI_FLOOR")
        store = [r for r in areas["rooms"] if "STORE" in r["name"]][0]
        self.assertEqual(store["doors_primary"], 1)


def treads(x0, x1, y0, n, step=300.0, layer="A-STRS"):
    return [seg(x0, y0 + k * step, x1, y0 + k * step, layer) for k in range(n)]


class StairWellTests(unittest.TestCase):
    """The X sequence decides the well (a void, step 7); rooms_and_doors
    only cuts what it decided out of the staircase."""

    def well(self, **kw):
        x = x_item("VOID", 2100, 2100, 2700, 5400)
        x.update(kw)
        return x

    def test_a_void_between_two_flights_is_a_hole_in_the_staircase(self):
        st = room("ST-01", 0, 0, 4800, 7500)
        x = self.well(x_class="void", stair_well=True, family_forced="Void")
        segs = treads(0, 2100, 2100, 12) + treads(2700, 4800, 2100, 12)
        merged = [st, x]
        stats = dr.rooms_and_doors(merged, [], fam_name, rules.defaults(), segs=segs)
        self.assertEqual(stats.get("stair_voids_cut"), 1)
        self.assertEqual(merged, [st])
        self.assertEqual(len(st["holes"]), 1)
        self.assertAlmostEqual(Polygon(st["poly"], st["holes"]).area, 4800 * 7500 - 600 * 3300, delta=100)

    def test_an_x_the_sequence_made_a_shaft_stays_one(self):
        st = room("ST-01", 0, 0, 4800, 7500)
        x = self.well(x_class="shaft", family_forced="Shaft")
        merged = [st, x]
        dr.rooms_and_doors(merged, [], fam_name, rules.defaults(),
                           segs=treads(0, 2100, 2100, 12) + treads(2700, 4800, 2100, 12))
        self.assertEqual(len(merged), 2)
        self.assertEqual(x["family_forced"], "Shaft")


class ShaftRoomTests(unittest.TestCase):
    """P285 ELE SHAFT: X 600 x 1913 at the back, the room 400 deep in
    front, two 750 mm doors in a 200 mm band at its front."""

    def floor(self):
        x = x_item("ELE SHAFT", 0, 0, 600, 1913)
        rm = room("ELE SHAFT", 600, 0, 1000, 1913, source="S")
        doors = [rect(1000, 112, 1200, 862), rect(1000, 1062, 1200, 1812)]
        return x, rm, doors

    def test_on_ai_mep_and_stopping_at_its_own_face(self):
        """Fix 2: the shaft's room stops at its face of the wall; its doors
        are drawn on that face (the inner face of the wall it serves)."""
        x, rm, doors = self.floor()
        merged = [x, rm]
        stats = dr.rooms_and_doors(merged, doors, fam_name, rules.defaults())
        self.assertEqual(rm["family_forced"], "MEP / Services")
        self.assertTrue(rm["shaft_room"])
        self.assertEqual(stats["shaft_rooms_to_mep"], 1)
        self.assertAlmostEqual(Polygon(rm["poly"]).area / 1e6, 0.765, delta=0.005)
        self.assertNotIn("family_forced", x)            # the X is the shaft
        g = Polygon(rm["poly"])
        for q in doors:
            ln = el.door_line(q, [g])
            self.assertTrue(all(abs(p[0] - 1000) < 1 for p in ln))

    def test_no_door_no_shaft_room(self):
        x, rm, _doors = self.floor()
        merged = [x, rm]
        dr.rooms_and_doors(merged, [], fam_name, rules.defaults())
        self.assertNotIn("family_forced", rm)

    def test_a_door_between_two_rooms_is_not_taken_in(self):
        x, rm, doors = self.floor()
        other = room("STORE", 1200, 0, 3000, 1913)
        merged = [x, rm, other]
        dr.rooms_and_doors(merged, doors, fam_name, rules.defaults())
        self.assertEqual(rm["family_forced"], "MEP / Services")
        self.assertAlmostEqual(Polygon(rm["poly"]).area / 1e6, 0.765, delta=0.005)

    def test_the_users_family_stands(self):
        x, rm, doors = self.floor()
        rm["sr_no"] = 7
        merged = [x, rm]
        dr.rooms_and_doors(merged, doors, fam_name, rules.defaults(), user_set=lambda d: d.get("sr_no") == 7)
        self.assertNotIn("family_forced", rm)

    def test_a_refuge_area_beside_a_shaft_is_no_shaft_room(self):
        """H10 Tower 1: REFUGE AREA (Fire / Life Safety, 95 sq.m) shares a side
        with a shaft's X and has a door; it is not that shaft's room."""
        x = x_item("ELE SHAFT", 0, 0, 600, 1913)
        refuge = room("REFUGE AREA 95.50 SQ MTR", 600, 0, 10600, 9500)
        merged = [x, refuge]
        dr.rooms_and_doors(merged, [rect(10600, 500, 10800, 1400)], fam_name, rules.defaults())
        self.assertNotIn("family_forced", refuge)
        self.assertAlmostEqual(Polygon(refuge["poly"]).area, 10000 * 9500, delta=1)

    def test_a_small_room_named_otherwise_is_no_shaft_room(self):
        x = x_item("ELE SHAFT", 0, 0, 600, 1913)
        store = room("STORE", 600, 0, 1000, 1913)
        merged = [x, store]
        dr.rooms_and_doors(merged, [rect(1000, 112, 1200, 862)], fam_name, rules.defaults())
        self.assertNotIn("family_forced", store)

    def test_a_toilet_beside_a_shaft_stays_a_toilet(self):
        x = x_item("PHE SHAFT", 0, 0, 600, 1913)
        wc = room("TOILET", 600, 0, 3000, 1913)
        merged = [x, wc]
        dr.rooms_and_doors(merged, [rect(3000, 500, 3200, 1400)], fam_name, rules.defaults())
        self.assertNotIn("family_forced", wc)

    def test_the_element_list_does_not_score_it(self):
        sp = [{"poly": box(0, 0, 600, 1913), "name": "ELE SHAFT", "family": "Shaft", "current_class": E.SHAFT,
               "x": True},
              {"poly": box(600, 0, 1000, 1913), "name": "ELE SHAFT", "family": "MEP / Services",
               "current_class": None}]
        op = [{"poly": box(1000, 112, 1200, 862), "kind": "door", "door_kind": "shaft", "current_class": E.DOOR}]
        recs = E.identify_floor(sp, op, None, E.Evidence([]), floor="F", prefix="F1")
        b = [r for r in recs if r["group"] == "B"]
        self.assertEqual(len(b), 1)
        self.assertAlmostEqual(b[0]["area_sqm"], 1.148, delta=0.01)


class LiftMachineRoomTests(unittest.TestCase):

    def test_a_doorless_lift_facing_the_machine_room_is_the_machine_rooms(self):
        mr = room("LIFT ELE.PANELS", 2700, 0, 6700, 12000)
        wells = [room("LIFT WELL BELOW", 0, k * 2700, 2500, k * 2700 + 2500) for k in range(4)]
        merged = [mr] + wells
        stats = dr.rooms_and_doors(merged, [], fam_name, rules.defaults())
        self.assertEqual(stats["lifts_to_machine_room"], 4)
        self.assertTrue(all(w["family_forced"] == "Machine Room" for w in wells))

    def test_an_x_lift_is_not_filed_on_the_machine_room(self):
        """2026-10-16: the machine-room rule never takes an X lift."""
        mr = room("LIFT ELE.PANELS", 2700, 0, 6700, 12000)
        wells = [dict(x_item("LIFT WELL BELOW", 0, k * 2700, 2500, k * 2700 + 2500),
                      x_class="lift", family_forced="Lift") for k in range(4)]
        merged = [mr] + wells
        stats = dr.rooms_and_doors(merged, [], fam_name, dict(rules.defaults(), lift_without_door_mechanical=False))
        self.assertFalse(stats.get("lifts_to_machine_room"))
        self.assertTrue(all(w["family_forced"] == "Lift" for w in wells))

    def test_an_x_lift_with_no_door_is_a_mechanical_room(self):
        """2026-10-17 (rule lift_without_door_mechanical): an X lift with no
        gap in its walls and no door - LIFT WELL BELOW - is a mechanical
        room; one with its door stays a lift."""
        wells = [dict(x_item("LIFT WELL BELOW", 0, k * 2700, 2500, k * 2700 + 2500),
                      x_class="lift", family_forced="Lift") for k in range(3)]
        door = [(700, -200), (1800, -200), (1800, 0), (700, 0)]        # on the first well's front
        stats = dr.rooms_and_doors(list(wells), [door], fam_name, rules.defaults())
        self.assertEqual(stats["lifts_to_mechanical_room"], 2)
        self.assertEqual(wells[0]["family_forced"], "Lift")
        self.assertEqual([w["family_forced"] for w in wells[1:]], ["Mechanical Room"] * 2)

    def test_a_lift_picked_by_hand_is_never_changed(self):
        lift = dict(x_item("LIFT WELL BELOW", 0, 0, 2500, 2500), x_class="lift", family_forced="Lift")
        stats = dr.rooms_and_doors([lift], [], fam_name, rules.defaults(), user_set=lambda d: True)
        self.assertFalse(stats.get("lifts_to_mechanical_room"))
        self.assertEqual(lift["family_forced"], "Lift")

    def test_a_lift_with_its_door_stays_a_lift(self):
        mr = room("LIFT ELE.PANELS", 2700, 0, 6700, 12000)
        lift = x_item("PASSENGER LIFT", 0, 0, 2500, 2500)
        merged = [mr, lift]
        dr.rooms_and_doors(merged, [rect(700, 2500, 1800, 2700)], fam_name, rules.defaults())
        self.assertNotIn("family_forced", lift)

    def test_the_statement_files_it_under_machine_room(self):
        self.assertEqual(A.classify("LIFT WELL BELOW", "Machine Room", True, []), "machine_room")
        self.assertEqual(A.classify("LIFT WELL BELOW", "Machine Room", False, []), "machine_room")
        self.assertEqual(A.classify("LIFT ELE.PANELS", "Machine Room", False, []), "machine_room")
        self.assertEqual(A.classify("LIFT WELL BELOW", "Lift", True, []), "lift_shaft")


class StatementTests(unittest.TestCase):

    def test_a_hose_cabinet_behind_its_door_is_its_slots_room(self):
        nb = [{"name": "FHC", "layer": "AI_FIRE", "opening": True, "inside": False, "shared_mm": 749.0,
               "touches": []}]
        self.assertEqual(A.classify("FHC", "MEP / Services", False, nb), "shaft_room")
        self.assertEqual(A.classify("FHC-3", "MEP / Services", False, nb), "shaft_room")
        self.assertIsNone(A.classify("FHC", "Fire / Life Safety", False, []))

    def test_ahu_rooms(self):
        self.assertEqual(A.classify("AHU ROOM", "AHU Room", False, []), "ahu_room")
        self.assertEqual(A.FAMILY_CATEGORY["AHU Room"], "ahu_room")


class FamilyTests(unittest.TestCase):

    def test_ahu_rooms_on_their_own_layer(self):
        for name, fam, layer in (("AHU ROOM", "AHU Room", "AI_AHUROOM"), ("AHU", "AHU Room", "AI_AHUROOM"),
                                 ("AHU PIPE SHAFT", "Shaft", "AI_SHAFT"), ("AHU SHAFT", "Shaft", "AI_SHAFT"),
                                 ("TEN", "MEP / Services", "AI_MEP")):
            f = dr.family_for_label(name, dr._FAMILY_RULES)
            self.assertEqual(f["name"], fam, name)
            self.assertEqual(dr._family_layer(f), layer, name)


class AhuOpeningTests(unittest.TestCase):

    def test_an_x_named_for_an_ahu_stays_what_the_sequence_made_it(self):
        """P25: AHU - FA-1 1000X4150 is an X-marked fresh-air shaft, not an
        AHU room. The X sequence makes it a shaft (a wall beside it); the
        rooms and doors settled after never change that."""
        x = dict(x_item("AHU - FA-1 1000X4150", 0, 0, 1000, 4150), x_class="shaft", family_forced="Shaft")
        room_ = room("AHU ROOM", 2000, 0, 7000, 5000)
        merged = [x, room_]
        dr.rooms_and_doors(merged, [], fam_name, rules.defaults())
        self.assertEqual(x["family_forced"], "Shaft")
        self.assertNotIn("family_forced", room_)
        self.assertEqual(fam_name(room_), "AHU Room")
        self.assertEqual(A.classify("AHU - FA-1 1000X4150", "Shaft", True, []), "other_opening")


class WallGapTests(unittest.TestCase):
    plate = rect(0, 0, 10000, 10000)

    def test_a_hatched_pillar_on_no_layer_is_wall(self):
        """P285: the 200 x 200 pillar between the two ELE SHAFT doors."""
        walls = [[rect(1000, 1000, 1200, 3000)]]
        cover = [Polygon(rect(1200, 1000, 3000, 3000))]
        pillar = rect(1000, 3000, 1200, 3200)
        got = dr.wall_gap_pieces(self.plate, walls, cover, hatch_islands=[(pillar, [])])
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0].area, 200 * 200, delta=1500)

    def test_a_thin_enclosed_strip_is_wall(self):
        """P285: the 450 x 150 strip between the column and the FHC cabinet."""
        walls = [[rect(2000, 2000, 2200, 3000)], [rect(2200, 2000, 3000, 2200)]]
        cover = [Polygon(rect(2650, 2200, 3000, 3000)), Polygon(rect(2200, 2350, 2650, 3000))]
        got = dr.wall_gap_pieces(self.plate, walls, cover)
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0].area / 1e6, 0.45 * 0.15, delta=0.005)

    def test_not_a_column_casing_not_a_doorway_not_a_shafts_remainder(self):
        walls = [[rect(2000, 2000, 2200, 3000)], [rect(2200, 2000, 3000, 2200)]]
        strip = rect(2200, 2200, 2650, 2350)
        cover = [Polygon(rect(2650, 2200, 3000, 3000)), Polygon(rect(2200, 2350, 2650, 3000))]
        # along a door
        door = Polygon(rect(2200, 2200, 2650, 2300))
        got = dr.wall_gap_pieces(self.plate, walls, cover + [door], doors=[door])
        self.assertEqual(got, [])
        # holding a label
        got = dr.wall_gap_pieces(self.plate, walls, cover, label_pts=[(2400, 2300)])
        self.assertEqual(got, [])
        # 250 deep: wider than a wall
        cover2 = [Polygon(rect(2650, 2200, 3000, 3000)), Polygon(rect(2200, 2450, 2650, 3000))]
        got = dr.wall_gap_pieces(self.plate, walls, cover2)
        self.assertEqual(got, [])
        self.assertEqual(len(strip), 4)

    def test_every_island_of_a_wall_hatch(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        h = msp.add_hatch(color=8, dxfattribs={"layer": "A-HATCH"})
        h.set_pattern_fill("ANSI32", scale=10)
        h.paths.add_polyline_path(rect(0, 0, 200, 2000), is_closed=True)
        h.paths.add_polyline_path(rect(0, 2200, 200, 2400), is_closed=True)
        one = dr._collect_wall_hatches(doc, msp, dr.MM_UNITS)
        every = dr._collect_wall_hatches(doc, msp, dr.MM_UNITS, islands=True)
        self.assertEqual(len(one), 1)
        self.assertEqual(len(every), 2)


class SlabEdgeTests(unittest.TestCase):

    def test_a_detail_line_through_the_shafts_is_dropped(self):
        segs = [seg(500, -2000, 500, 12000, "a-details"),       # 14 m through the X
                seg(500, 20000, 500, 30000, "a-details"),       # 10 m, through no X
                seg(800, -2000, 800, 12000, "A-WALL"),          # a wall line through it
                seg(0, 0, 1000, 0, "a-details")]               # short
        kept, dropped = dr.drop_slab_edge_lines(segs, [(0, 1000, 1000, 2500)])
        self.assertEqual(dropped, [segs[0]])
        self.assertEqual(kept, segs[1:])

    def test_the_dashed_keys_are_kept(self):
        segs = dr._SegList([seg(500, -2000, 500, 12000, "A-DETL")])
        segs.dashed = frozenset([((1.0, 2.0), (3.0, 4.0))])
        kept, dropped = dr.drop_slab_edge_lines(segs, [(0, 1000, 1000, 2500)])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(kept.dashed, segs.dashed)


class RefugeSizeTests(unittest.TestCase):
    """A refuge area is a floor area: the 150 sq.m cap on a services label
    with no size never drops it (285 FD Tower 2, REFUGE AREA-1 159.9); it
    has a cap of its own, and is traced again with its doorways shut."""

    def test_refuge_is_not_held_to_the_services_cap(self):
        for t in ("REFUGE AREA-1", "REFUGE AREA", "REFUGE"):
            self.assertTrue(dr.SERVICE_BIG_ROOM_RE.search(t), t)
            self.assertTrue(dr.REFUGE_NAME_RE.search(t), t)

    def test_refuge_has_a_cap_of_its_own(self):
        self.assertGreater(dr.REFUGE_UNSIZED_MAX_SQM, dr.SERVICE_UNSIZED_MAX_SQM)
        self.assertTrue(0.0 < dr.REFUGE_CLOSE_MIN_SHARE < 1.0)
        self.assertFalse(dr.REFUGE_NAME_RE.search("FIRE PANEL"))

    def test_equipment_labels_still_are(self):
        for t in ("FIRE PANEL", "FHC", "ELEC. ROOM", "PUMP ROOM"):
            self.assertFalse(dr.SERVICE_BIG_ROOM_RE.search(t), t)

    def test_ahu_and_plant_stay_exempt(self):
        for t in ("AHU ROOM", "AIR HANDLING UNIT", "PLANT ROOM", "SERVICE FLOOR"):
            self.assertTrue(dr.SERVICE_BIG_ROOM_RE.search(t), t)


if __name__ == "__main__":
    unittest.main()
