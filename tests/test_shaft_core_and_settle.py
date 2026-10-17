# -*- coding: utf-8 -*-
"""P285 shaft core and the XA / Centaurus review (recommendations 1-10).

  * an X is what the X sequence made it (2026-10-16): a shaft stays on
    AI_SHAFT whatever its label or a family picked by hand, lift wells,
    stair voids and cut-outs keep their own layers;
  * the room with a door in front of it is the shaft's room (AI_MEP);
  * AI_FIRE only for fire rooms, in a colour of its own;
  * the geometry engine: an X slot reports nothing (an X is Step 3's); an
    RCC wall drawn on the column layer is a wall;
  * the comparison: MEP / fire against a geometry room agrees generically;
  * the final drawing: an X mark of Step 3 is tagged as an opening, the
    columns are Step 3's and never filled as wall;
  * the statement: a hose cabinet a thin gap from its slot is its room;
  * rooms, X openings and walls settled together; doors need evidence.

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
from Drawing_Analyzer import geocompare as gc  # noqa: E402
from Drawing_Analyzer import geoengine as ge  # noqa: E402
from Drawing_Analyzer import layernames as LN  # noqa: E402
from Drawing_Analyzer import rules  # noqa: E402


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def room(label, x0, y0, x1, y1, source="P", **kw):
    d = {"label": label, "poly": rect(x0, y0, x1, y1), "holes": [], "bbox": (x0, y0, x1, y1),
         "source": source, "cx": (x0 + x1) / 2.0, "cy": (y0 + y1) / 2.0, "sr_no": None}
    d.update(kw)
    return d


def x_item(label, x0, y0, x1, y1, **kw):
    d = {"label": label, "poly": [], "holes": [], "bbox": (x0, y0, x1, y1), "source": "X",
         "xrects": [(x0, y0, x1, y1)], "cx": (x0 + x1) / 2.0, "cy": (y0 + y1) / 2.0, "sr_no": None}
    d.update(kw)
    return d


def fam_name(d):
    if d.get("family_forced"):
        return d["family_forced"]
    return dr.family_for_label(d.get("label"), dr._FAMILY_RULES)["name"]


def family(name):
    return next(f for f in dr._FAMILY_RULES if f["name"] == name)


class RuleBookTests(unittest.TestCase):

    def test_the_new_rules_exist_and_are_on(self):
        on = rules.defaults()
        for k in ("lift_label_fits_its_x", "x_cell_never_wall",
                  "rcc_walls_on_column_layer", "wall_junctions_closed", "rooms_clear_of_walls",
                  "doors_need_evidence", "stair_one_outline", "lift_well_rectangle",
                  "toilet_with_cubicles", "leftover_strip_joins_room"):
            self.assertTrue(on[k], k)
        self.assertNotIn("x_opening_is_shaft", on)       # the X sequence's (2026-10-16)


class FamilyTests(unittest.TestCase):

    def test_fire_tags_on_shafts_and_cabinets_are_services(self):
        for name in ("FHC", "FHR", "FHC-3", "FHC CABINET"):
            self.assertEqual(dr.family_for_label(name, dr._FAMILY_RULES)["name"], "MEP / Services", name)

    def test_fire_rooms_stay_fire(self):
        for name in ("FIRE COMMAND ROOM", "FIRE CONTROL ROOM", "REFUGE AREA-1", "FIRE PUMP ROOM"):
            self.assertEqual(dr.family_for_label(name, dr._FAMILY_RULES)["name"], "Fire / Life Safety", name)

    def test_fire_and_shaft_are_told_apart_by_colour(self):
        self.assertNotEqual(int(family("Fire / Life Safety")["color"]), int(family("Shaft")["color"]))

    def test_a_named_room_beats_the_geometrys_machine_room(self):
        self.assertEqual(family("Machine Room").get("precedence"), "geometry")
        self.assertEqual(dr.family_precedence("Machine Room"), "geometry")


class XOpeningTests(unittest.TestCase):
    """P285 right core, TEN 600 x 300, FHC 1200 x 300, CHW 850 x 500 X
    boxes: the X sequence made them shafts (family Shaft); what the rooms
    and doors settle after never changes that."""

    def shaft(self, label, *b, **kw):
        return x_item(label, *b, x_class="shaft", family_forced="Shaft", **kw)

    def test_every_x_shaft_stays_a_shaft(self):
        xs = [self.shaft(n, 0, k * 2000, 600, k * 2000 + 300)
              for k, n in enumerate(("TEN", "FHC", "FHR", "CHW SHAFT", "COMM", "FAPA", "PRESS", "RWP", "PHE"))]
        merged = list(xs)
        dr.rooms_and_doors(merged, [], fam_name, rules.defaults())
        for x in xs:
            self.assertEqual(fam_name(x), "Shaft", x["label"])
            self.assertEqual(dr._family_layer(family(fam_name(x))), "AI_SHAFT")

    def test_lifts_voids_and_cutouts_keep_their_layers(self):
        lift = x_item("PASSENGER LIFT", 0, 0, 2500, 2500, x_class="lift", family_forced="Lift")
        void = x_item("VOID", 10000, 0, 16000, 6000, x_class="void", family_forced="Void")
        merged = [lift, void]
        dr.rooms_and_doors(merged, [rect(700, 2500, 1800, 2700)], fam_name, rules.defaults())
        self.assertEqual(fam_name(lift), "Lift")
        self.assertEqual(fam_name(void), "Void")

    def test_a_family_set_by_hand_changes_no_x(self):
        x = self.shaft("FHC", 0, 0, 1200, 300, sr_no=4)
        dr.rooms_and_doors([x], [], fam_name, rules.defaults(), user_set=lambda d: d.get("sr_no") == 4)
        self.assertEqual(x["family_forced"], "Shaft")

    def test_the_cabinet_in_front_with_its_door_is_the_shafts_room(self):
        """FHC: the slot 1200 x 300 at the back, the cabinet 650 deep in front,
        its door across the front."""
        x = self.shaft("FHC", 0, 650, 1200, 950)
        cab = room("FHC", 0, 0, 1200, 650, source="S")
        merged = [x, cab]
        dr.rooms_and_doors(merged, [rect(200, -100, 1000, 0)], fam_name, rules.defaults())
        self.assertEqual(fam_name(x), "Shaft")
        self.assertEqual(fam_name(cab), "MEP / Services")
        self.assertTrue(cab["shaft_room"])
        # a cabinet named otherwise (CHW SHAFT, on AI_SHAFT) goes to AI_MEP
        x2 = self.shaft("CHW SHAFT", 0, 650, 850, 1150)
        rm2 = room("CHW SHAFT", 0, 0, 850, 650, source="S")
        dr.rooms_and_doors([x2, rm2], [rect(100, -100, 750, 0)], fam_name, rules.defaults())
        self.assertEqual(rm2["family_forced"], "MEP / Services")

    def test_no_shaft_room_beside_an_x_that_is_no_shaft(self):
        x = x_item("FHC", 0, 650, 1200, 950, x_class="void", family_forced="Void")
        cab = room("FHC", 0, 0, 1200, 650, source="S")
        dr.rooms_and_doors([x, cab], [rect(200, -100, 1000, 0)], fam_name, rules.defaults())
        self.assertFalse(cab.get("shaft_room"))


class StatementTests(unittest.TestCase):

    def test_a_cabinet_a_thin_gap_from_its_slot_is_its_room(self):
        """P285: the FHC cabinet and its slot 100 mm apart: shared_mm 0."""
        nb = [{"name": "FHC", "layer": "AI_SHAFT", "opening": True, "inside": False,
               "shared_mm": 0.0, "shared_near_mm": 1180.0, "touches": []}]
        self.assertEqual(A.classify("FHC", "MEP / Services", False, nb), "shaft_room")
        far = [dict(nb[0], shared_near_mm=0.0)]
        self.assertNotEqual(A.classify("FHC", "MEP / Services", False, far), "shaft_room")

    def test_the_neighbours_carry_the_near_share(self):
        rooms_out = [{}, {}]
        polys = [{"pts": rect(0, 0, 1200, 650), "name": "FHC", "layer": "AI_MEP", "floor": "F"},
                 {"pts": rect(0, 750, 1200, 1050), "name": "FHC", "layer": "AI_SHAFT", "floor": "F",
                  "opening": True}]
        dr._mark_neighbours(rooms_out, polys)
        nb = rooms_out[0]["neighbours"][0]
        self.assertEqual(nb["shared_mm"], 0.0)
        self.assertGreaterEqual(nb["shared_near_mm"], 1100.0)

    def test_x_openings_keep_their_statement_column_as_shafts(self):
        for name, key in (("FHC", "fhr"), ("TEN", "ten"), ("CHW SHAFT", "chw"), ("RWP", "rwdp")):
            self.assertEqual(A.classify(name, "Shaft", True, []), key, name)


class CompareTests(unittest.TestCase):

    def test_services_against_a_geometry_room_agree_generically(self):
        # the geometry finds no shaft (Step 3's, 2026-10-16): a room there
        for t in ("MEP", "FIRE", "PLUMB", "ELEC"):
            self.assertEqual(gc._category(t, {"cls": "ROOM"}), ("AGREEMENT", "generic"), t)
        self.assertEqual(gc._category("SHAFT", {"cls": "ROOM"}), ("CONFLICT", ""))
        self.assertEqual(gc._category("MEP", {"cls": "ROOM", "category_layer": "MEP"}), ("AGREEMENT", "exact"))

    def test_the_summary_counts_them_apart(self):
        rows = [{"category": "AGREEMENT", "level": "exact", "text": {"cls": "SHAFT"}, "geo": {"cls": "SHAFT"}},
                {"category": "AGREEMENT", "level": "family", "text": {"cls": "MEP"}, "geo": {"cls": "SHAFT"}},
                {"category": "AGREEMENT", "level": "generic", "text": {"cls": "STORE"}, "geo": {"cls": "ROOM"}}]
        s = gc._summary(rows, {"F": [1, 2, 3]}, {"floors": {"F": [1, 2, 3]}})
        self.assertEqual((s["agreement_exact"], s["agreement_family"], s["agreement_generic"]), (1, 1, 1))


class RccWallTests(unittest.TestCase):

    def test_a_long_thin_shape_on_the_column_layer_is_a_wall(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        doc.layers.add("A-COLUMN")
        for i in range(4):
            for j in range(4):
                msp.add_lwpolyline(rect(i * 8000, j * 8000, i * 8000 + 500, j * 8000 + 500), close=True,
                                   dxfattribs={"layer": "A-COLUMN"})
        msp.add_lwpolyline(rect(2000, 2000, 8000, 2300), close=True, dxfattribs={"layer": "A-COLUMN"})
        floors = [{"name": "F", "polygon": rect(-1000, -1000, 30000, 30000)}]
        rej = {}
        cols = ge._column_shapes(doc, msp, dr.MM_UNITS, floors, rejected=rej)
        self.assertTrue(any(abs(g.area - 6000 * 300) < 1 for g in rej.get("F", [])))
        self.assertFalse(any(abs(c[0].area - 6000 * 300) < 1 for c in cols.get("F", [])))
        rej2 = {}
        ge._column_shapes(doc, msp, dr.MM_UNITS, floors, rejected=rej2, rcc_walls=False)
        self.assertFalse(any(abs(g.area - 6000 * 300) < 1 for g in rej2.get("F", [])))


class SettleTests(unittest.TestCase):
    rule = rules.defaults()

    def items(self, *specs):
        out = []
        for fam, g, kw in specs:
            it = {"geom": g, "g0": g, "x": False, "fam": fam, "lift": fam == "Lift", "split": False,
                  "keep": False, "pt": None}
            it.update(kw)
            out.append(it)
        return out

    def test_a_room_never_covers_a_wall(self):
        walls = [[rect(3000, 0, 3200, 3000)]]
        its = self.items(("Room", box(0, 0, 3200, 3000), {}))
        dr.settle_rooms_and_walls(its, walls, self.rule)
        self.assertAlmostEqual(its[0]["geom"].area, 3000 * 3000, delta=1)

    def test_a_label_in_a_wall_draws_no_room(self):
        walls = [[rect(0, 0, 3000, 300)]]
        its = self.items(("Shaft", box(0, 0, 3000, 320), {}))
        dr.settle_rooms_and_walls(its, walls, self.rule)
        self.assertTrue(its[0].get("drop"))

    def test_an_x_cell_is_never_wall(self):
        walls = [[rect(0, 0, 3000, 300)]]
        its = self.items(("Shaft", box(1000, 0, 1600, 300), {"x": True}))
        out = dr.settle_rooms_and_walls(its, walls, self.rule)
        W = dr._wall_union(out)
        self.assertLess(W.intersection(box(1000, 0, 1600, 300)).area, 1000.0)

    def test_an_x_crossed_by_a_wall_is_cut_back_to_it(self):
        walls = [[rect(0, 0, 3000, 200)]]
        its = self.items(("Shaft", box(1000, 100, 2000, 1100), {"x": True}))
        dr.settle_rooms_and_walls(its, walls, self.rule)
        self.assertAlmostEqual(its[0]["geom"].bounds[1], 200, delta=1)

    def test_the_pieces_of_one_staircase_are_one(self):
        walls = [[rect(-500, -500, -300, 5000)]]
        its = self.items(("Staircase", box(0, 0, 2000, 4000), {}), ("Staircase", box(2100, 0, 4000, 4000), {}))
        dr.settle_rooms_and_walls(its, walls, self.rule)
        live = [i for i in its if not i.get("drop")]
        self.assertEqual(len(live), 1)
        self.assertAlmostEqual(live[0]["geom"].area, 4000 * 4000, delta=1000)

    def test_a_lift_notched_by_its_frame_is_its_rectangle(self):
        walls = [[rect(-200, -200, 2700, 0)]]
        g = Polygon([(0, 0), (2500, 0), (2500, 2500), (1500, 2500), (1500, 2350), (1000, 2350), (1000, 2500),
                     (0, 2500)])
        its = self.items(("Lift", g, {}))
        dr.settle_rooms_and_walls(its, walls, self.rule)
        self.assertAlmostEqual(its[0]["geom"].area, 2500 * 2500, delta=1)

    def test_a_toilet_takes_in_its_cubicles(self):
        plate = rect(-1000, -1000, 8000, 6000)
        walls = [[rect(-200, -200, 6200, 0)], [rect(-200, 4000, 6200, 4200)], [rect(-200, 0, 0, 4000)],
                 [rect(6000, 0, 6200, 4000)]]
        its = self.items(("Toilet", box(0, 0, 4500, 4000), {}))
        dr.settle_rooms_and_walls(its, walls, self.rule, plate=plate)
        self.assertAlmostEqual(its[0]["geom"].area, 6000 * 4000, delta=20000)

    def test_a_box_across_two_staircases_is_split_and_not_counted_twice(self):
        """Centaurus S5 / S6: the geometry's staircase box runs across the
        wall into the next stair; the texts drew the first one."""
        walls = [[rect(4000, -200, 4200, 6200)]]
        its = self.items(("Staircase", box(0, 0, 4000, 6000), {}),
                         ("Staircase", box(0, 0, 8200, 6000), {"split": True}))
        dr.settle_rooms_and_walls(its, walls, self.rule)
        live = [i for i in its if not i.get("drop")]
        self.assertEqual(len(live), 2)
        self.assertAlmostEqual(sum(i["geom"].area for i in live), 2 * 4000 * 6000, delta=1000)

    def box_walls(self, gap=None):
        """Walls round 0..4000 x 0..4000, 200 thick; a gap in the top one."""
        top = [[rect(-200, 4000, 4200, 4200)]] if gap is None else \
            [[rect(-200, 4000, gap[0], 4200)], [rect(gap[1], 4000, 4200, 4200)]]
        return [[rect(-200, -200, 4200, 0)], [rect(-200, 0, 0, 4000)], [rect(4000, 0, 4200, 4000)]] + top

    def test_a_staircase_takes_in_its_landing(self):
        plate = rect(-1000, -1000, 6000, 6000)
        door = Polygon(rect(1500, 4000, 2500, 4200))
        its = self.items(("Staircase", box(0, 0, 4000, 3000), {}))
        dr.settle_rooms_and_walls(its, self.box_walls((1500, 2500)), self.rule, plate=plate,
                                  cover=[door], doors=[door])
        self.assertAlmostEqual(its[0]["geom"].area, 4000 * 4000, delta=20000)

    def test_not_the_strip_in_its_doorway(self):
        """P285: the 100 mm between the stair's face and its door line."""
        plate = rect(-1000, -1000, 6000, 6000)
        door = Polygon(rect(1500, 4100, 2500, 4200))      # the door at the wall's outer face
        its = self.items(("Staircase", box(0, 0, 4000, 4000), {}))
        dr.settle_rooms_and_walls(its, self.box_walls((1500, 2500)), self.rule, plate=plate,
                                  cover=[door], doors=[door])
        self.assertAlmostEqual(its[0]["geom"].area, 4000 * 4000, delta=1)

    def test_rules_off(self):
        walls = [[rect(3000, 0, 3200, 3000)]]
        off = dict(self.rule, rooms_clear_of_walls=False)
        its = self.items(("Room", box(0, 0, 3200, 3000), {}))
        dr.settle_rooms_and_walls(its, walls, off)
        self.assertAlmostEqual(its[0]["geom"].area, 3200 * 3000, delta=1)


class DoorEvidenceTests(unittest.TestCase):
    """XA 37th floor, male toilet entrance."""
    strips = [{"pts": rect(0, 0, 2000, 200)}, {"pts": rect(2900, 0, 5000, 200)},     # a wall with a 900 gap
              {"pts": rect(6000, 1000, 6200, 4000)}]                                # a wall on its own

    def test_a_door_in_a_gap_stands(self):
        rej = []
        out = dr._door_evidence([rect(2000, 0, 2900, 200)], self.strips, rej)
        self.assertEqual(len(out), 1)
        self.assertEqual(rej, [])

    def test_an_opening_along_a_wall_face_is_no_door(self):
        rej = []
        out = dr._door_evidence([rect(5800, 1200, 6000, 3200)], self.strips, rej)
        self.assertEqual(out, [])
        self.assertFalse(rej[0]["review"])

    def test_a_second_door_in_the_same_gap(self):
        rej = []
        out = dr._door_evidence([rect(2000, 0, 2900, 200), rect(1900, 200, 3000, 340)], self.strips, rej)
        self.assertEqual([Polygon(q).area for q in out], [900 * 200])
        self.assertIn("second door", rej[0]["why"])

    def test_rule_off(self):
        q = [rect(5800, 1200, 6000, 3200)]
        self.assertEqual(len(dr.door_openings([], [], self.strips, evidence=False) or []), 0)
        self.assertEqual(dr._door_evidence(q, [], None), q)


class FinalDrawingTests(unittest.TestCase):

    def test_an_x_of_step3_is_an_opening(self):
        import importlib
        app = importlib.import_module("Drawing_Analyzer.app")
        self.assertFalse(hasattr(app, "_geo_x_ids"))         # the geometry finds no X opening
        doc = ezdxf.new()
        msp = doc.modelspace()
        row = {"poly": rect(0, 0, 850, 500), "name": "CHW SHAFT", "family": "Shaft", "from": "text",
               "x": True, "tag": None}
        e = msp.add_lwpolyline(row["poly"], close=True, dxfattribs={"layer": "AI_SHAFT"})
        app._tag_final_room(doc, e, row, [])
        self.assertEqual(dr.room_tag(e)[0], "X")
        row2 = dict(row, x=False)
        e2 = msp.add_lwpolyline(row2["poly"], close=True, dxfattribs={"layer": "AI_SHAFT"})
        app._tag_final_room(doc, e2, row2, [])
        self.assertFalse(dr.room_tag(e2)[0])

    def test_a_geometry_box_across_two_staircases_does_not_replace_one(self):
        """Centaurus S5: the texts drew the staircase (27.88), the geometry a
        box across it and the next one (58.3): the texts' outline stands. A
        shaft is Step 3's: the texts' outline, fixed."""
        import importlib
        app = importlib.import_module("Drawing_Analyzer.app")

        def row(tcls, tname, ta, gcls, glabel, ga, iou):
            return {"id": "CMP-1", "category": "AGREEMENT", "level": "exact", "iou": iou,
                    "text": {"cls": tcls, "name": tname, "area_sqm": ta, "poly": rect(0, 0, 1, 1)},
                    "geo": {"id": "GEO-1", "cls": gcls, "label": glabel, "area_sqm": ga, "category": "",
                            "poly": rect(0, 0, 1, 1)}}
        cmp = {"rows": [row("STAIR", "STAIRCASE", 27.88, "STAIRCASE", "STAIRCASE_04", 58.3, 0.32),
                        row("STAIR", "STAIRCASE", 36.0, "STAIRCASE", "STAIRCASE_01", 37.4, 0.93),
                        row("SHAFT", "SMOKE VENT SHAFT", 4.6, "SHAFT", "SHAFT_44", 2.25, 0.49)]}
        app._final_defaults(cmp)
        self.assertEqual(cmp["rows"][0]["final_default"]["use"], "text")
        self.assertEqual(cmp["rows"][1]["final_default"]["use"], "geometry")
        self.assertEqual(cmp["rows"][2]["final_default"]["use"], "text")
        self.assertTrue(cmp["rows"][2]["final_default"]["fixed"])

    def test_the_columns_are_step3s(self):
        import importlib
        app = importlib.import_module("Drawing_Analyzer.app")
        rows = [{"floor": "F", "family": "Column", "poly": rect(0, 0, 500, 500)}]
        geo = {"floors": {"F": [{"cls": "COLUMN", "poly": rect(0, 0, 500, 500)},
                                {"cls": "COLUMN", "poly": rect(5000, 0, 5500, 500)}]}}
        text_cols = [Polygon(rect(5000, 0, 5500, 500)), Polygon(rect(9000, 0, 9700, 700)),
                     Polygon(rect(9000, 0, 9700, 700))]
        cols = app._final_columns(rows, geo, text_cols, [{"name": "F", "polygon": rect(-100, -100, 20000, 20000)}])
        self.assertEqual(len(cols["F"]), 2)                  # Step 3's two, once each


if __name__ == "__main__":
    unittest.main()
