# -*- coding: utf-8 -*-
"""The X sequence (xplace, 2026-10-16, final): tags, the X's own frame, the
wall-run test, the gap to the wall, the seven steps (1 every X a void,
2 shaft, 3 lift, 4 window, 5 cut-out, 6 column, 7 void), and the whole pass
on small drawings - an X in a wall is a window or a cut-out, an X beside a
wall a shaft that is never wall, a lift-size X walled on three sides a
lift, an X on open floor a cut-out, one in a room a void, a door tag never
a name, an unnamed X with a cut-out tag a cut-out - and nothing after the
sequence changes its answer: not a family picked in Step 3, the Geometry
tab, the user's rules, the element engine, the geometry engine."""
import os
import sys
import tempfile
import unittest

import ezdxf
from shapely.geometry import Polygon, box

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from Drawing_Analyzer import dxf_reader as dr          # noqa: E402
from Drawing_Analyzer import xplace as xp              # noqa: E402
from Drawing_Analyzer import layernames as LN          # noqa: E402


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def strip(x0, y0, x1, y1):
    return {"pts": rect(x0, y0, x1, y1), "t": min(x1 - x0, y1 - y0)}


class TagTests(unittest.TestCase):

    def test_door_window_cutout_tags(self):
        for t in ("FMD", "FMD-2", "FMD 4", "SD-3A", "D-1", "D1", "FD4"):
            self.assertEqual(xp.tag_kind(t), "door", t)
        for t in ("W-1", "W2", "ALW3"):
            self.assertEqual(xp.tag_kind(t), "window", t)
        for t in ("C-1", "C-10a", "C 3"):
            self.assertEqual(xp.tag_kind(t), "cutout", t)

    def test_names_and_words_are_not_tags(self):
        is_name = lambda t: t in ("CHW",)
        self.assertIsNone(xp.tag_kind("CHW", is_name))           # chilled water
        self.assertIsNone(xp.tag_kind("ROAD"))                   # a word, two vowels
        self.assertIsNone(xp.tag_kind("TEN"))
        self.assertIsNone(xp.tag_kind("FHC-3"))
        self.assertIsNone(xp.tag_kind("ST-01"))
        self.assertIsNone(xp.tag_kind("C-5\nLOBBY PRESS. 750X1140"))   # a tag over a name is a name

    def test_the_pass_never_takes_a_tag_for_a_name(self):
        self.assertTrue(dr._is_plain_tag("SD-6"))
        self.assertTrue(dr._is_plain_tag("FMD"))
        self.assertFalse(dr._is_plain_tag("CHW"))
        self.assertFalse(dr._is_plain_tag("BMS SHAFT 480x820"))


class FrameTests(unittest.TestCase):

    def test_frame_lines_but_not_a_wall_face_running_past(self):
        segs = [(0, 0, 300, 0), (300, 0, 300, 600), (0, 600, 300, 600), (0, 0, 0, 600),     # the frame
                (-500, 600, 2000, 600),                                                   # a wall face past its corners
                (0, 0, 300, 600), (0, 600, 300, 0)]                                       # the diagonals
        got = xp.frame_lines([s + ("L", 1.0) for s in segs], [(0, 0, 300, 600)])
        self.assertEqual(got, {0, 1, 2, 3})

    def test_a_wall_face_along_the_x_edge_stays_a_wall(self):
        # BHUB: A3-WL_EXT runs exactly along the cut-out's edge, the X and
        # its frame on A3-CUTOUT
        segs = [(0, 0, 2500, 0, "A3-CUTOUT", 2500.0), (0, 0, 2500, 0, "A3-WL_EXT", 2500.0),
                (0, 600, 2500, 600, "A3-CUTOUT", 2500.0),
                (0, 0, 2500, 600, "A3-CUTOUT", 2571.0), (0, 600, 2500, 0, "A3-CUTOUT", 2571.0),
                (0, -200, 2500, -200, "A3-WL_EXT", 2500.0)]                # the wall's other face
        diag = lambda sg: abs(sg[1] - sg[3]) > 1 and abs(sg[0] - sg[2]) > 1
        lays = xp.diagonal_layers(segs, [(0, 0, 2500, 600)], diag)
        self.assertEqual(lays, [{"A3-CUTOUT"}])
        got = xp.frame_lines(segs, [(0, 0, 2500, 600)], layers=lays, skip=diag)
        self.assertEqual(got, {2})                 # the edge along the wall is the wall's face, whatever its layer
        # an overlay outline along the same edge with no wall behind it is a frame
        segs2 = [s for s in segs if not (s[1] == -200)]
        got = xp.frame_lines(segs2, [(0, 0, 2500, 600)], layers=lays, skip=diag)
        self.assertEqual(got, {0, 1, 2})

    def test_a_line_whose_partner_is_another_walls_face_is_a_frame(self):
        # P285 LIFT PRE: the X's top edge 250 mm below the masonry wall's
        # face (1750), the masonry 200 mm thick (1750-1950)
        segs = [(0, 1500, 1150, 1500, "OTHER", 1150.0),
                (0, 1750, 1150, 1750, "A-WALL-MAS", 1150.0), (0, 1950, 1150, 1950, "A-WALL-MAS", 1150.0),
                (0, 0, 1150, 1500, "A-CUTOUT", 1890.0), (0, 1500, 1150, 0, "A-CUTOUT", 1890.0)]
        diag = lambda sg: abs(sg[1] - sg[3]) > 1 and abs(sg[0] - sg[2]) > 1
        lays = xp.diagonal_layers(segs, [(0, 0, 1150, 1500)], diag)
        self.assertEqual(xp.frame_lines(segs, [(0, 0, 1150, 1500)], layers=lays, skip=diag), {0})


class SequenceTests(unittest.TestCase):
    """The X sequence (xplace, 2026-10-16, final): x_facts reads, resolve_x
    decides - 2 shaft, 3 lift, 4 window, 5 cut-out, 6 column, 7 void."""

    def mass(self, strips, columns=()):
        faces = []
        for s in strips:
            p = s["pts"]
            faces += [(p[0][0], p[0][1], p[1][0], p[1][1]), (p[3][0], p[3][1], p[2][0], p[2][1])]
        return xp.WallMass(strips, (), faces, columns)

    def cls(self, bbox, m, **kw):
        texts = kw.pop("texts", ())
        return xp.resolve_x(xp.x_facts(bbox, m, texts), **kw)

    def test_an_x_in_a_wall_run_is_a_cut_out_or_a_window(self):
        # a 200 mm wall broken by a 1200 x 200 X
        m = self.mass([strip(0, 0, 3000, 200), strip(4200, 0, 7000, 200)])
        self.assertEqual(xp.run_ends(box(3000, 0, 4200, 200), m.W), 2)
        f = xp.x_facts((3000, 0, 4200, 200), m)
        self.assertTrue(f["in_wall"])
        r = xp.resolve_x(f)
        self.assertEqual((r["cls"], r["step"]), ("cutout", 5))     # no window evidence: a cut-out
        r = self.cls((3000, 0, 4200, 200), m, texts=[(3600, 500, "W-1")])
        self.assertEqual((r["cls"], r["step"]), ("window", 4))
        # glazing in it, or the window finder's frame, is window evidence too
        f = xp.x_facts((3000, 0, 4200, 200), m, glazing=2)
        self.assertEqual(xp.resolve_x(f)["cls"], "window")
        f = xp.x_facts((3000, 0, 4200, 200), m, finder_kind="window")
        self.assertEqual(xp.resolve_x(f)["cls"], "window")
        # a cut-out tag nearer than the window tag: a cut-out
        r = self.cls((3000, 0, 4200, 200), m, texts=[(3600, 300, "C-1"), (3600, 700, "W-1")])
        self.assertEqual(r["cls"], "cutout")
        # in a wall it is never a shaft, a lift or a void - named or not
        r = self.cls((3000, 0, 4200, 200), m, named=True, shaft_name=True)
        self.assertEqual(r["cls"], "cutout")

    def test_an_x_beside_a_wall_is_a_shaft(self):
        m = self.mass([strip(0, 0, 3000, 200)])
        self.assertEqual(self.cls((1000, 200, 1600, 800), m)["cls"], "shaft")     # touching the wall's face
        self.assertEqual(self.cls((1000, 240, 1600, 840), m)["cls"], "shaft")     # 40 mm off it
        r = self.cls((1000, 400, 1600, 1000), m)                                  # 200 mm off it
        self.assertEqual((r["cls"], r["step"]), ("cutout", 5))                    # unnamed, loose: a cut-out
        r = self.cls((1000, 400, 1600, 1000), m, named=True)
        self.assertEqual((r["cls"], r["step"]), ("void", 7))                      # named, loose: a void
        r = self.cls((1000, 400, 1600, 1000), m, in_room=True)
        self.assertEqual((r["cls"], r["step"]), ("void", 7))                      # standing in a room: a void
        # an unnamed X with a cut-out tag beside it: a cut-out, wall or not
        r = self.cls((1000, 200, 1600, 800), m, texts=[(1300, 1000, "C-7")])
        self.assertEqual((r["cls"], r["step"]), ("cutout", 5))
        # named, the tag does not take it
        r = self.cls((1000, 200, 1600, 800), m, texts=[(1300, 1000, "C-7")], named=True)
        self.assertEqual(r["cls"], "shaft")

    def test_spill_is_cut_back_to_the_wall_face(self):
        m = self.mass([strip(0, 0, 3000, 200)])
        f = xp.x_facts((1000, 100, 1600, 800), m)                  # 100 mm of it in the wall
        self.assertEqual(xp.resolve_x(f)["cls"], "shaft")
        self.assertAlmostEqual(f["shaft_bbox"][1], 200, delta=2)

    def test_column_and_a_small_x(self):
        m = self.mass([strip(0, 0, 3000, 200)], columns=[rect(5000, 5000, 5600, 5600)])
        f = xp.x_facts((5000, 5000, 5600, 5600), m)
        self.assertTrue(f["column"])
        r = xp.resolve_x(f, named=True, lift_label=True)
        self.assertEqual((r["cls"], r["step"]), ("column", 6))
        # a small X is an X like any other (no size guard): in a room a void
        r = self.cls((100, 300, 180, 450), m, in_room=True)
        self.assertEqual(r["cls"], "void")

    def test_a_steel_column_drawn_as_lines(self):
        # XA: a 454 mm X inside a 500 mm outline and 23 mm plates, against a
        # wall on two sides, open floor on the other two
        m = xp.WallMass([strip(-200, 0, 0, 2000), strip(-200, 500, 2000, 700)], (), [],
                        lines=[(0, 0, 500, 0), (500, 0, 500, 500), (500, 500, 0, 500), (0, 500, 0, 0)])
        self.assertEqual(self.cls((23, 23, 477, 477), m)["cls"], "column")
        # a shaft walled in on four sides is no column
        m2 = xp.WallMass([strip(-200, -200, 700, 0), strip(-200, 500, 700, 700),
                          strip(-200, 0, 0, 500), strip(500, 0, 700, 500)], (), [],
                         lines=[(0, 0, 500, 0), (500, 0, 500, 500), (500, 500, 0, 500), (0, 500, 0, 0)])
        self.assertNotEqual(self.cls((23, 23, 477, 477), m2)["cls"], "column")

    def test_a_slot_the_wall_holds_whole_is_in_the_wall(self):
        # BHUB: a 400 x 110 sleeve slot inside a 200 mm wall - an opening in
        # it (a cut-out), never a shaft, however the strips round it pair
        m = self.mass([strip(0, 0, 3000, 200)])
        f = xp.x_facts((1000, 45, 1400, 155), m)
        self.assertIn("slot", f["in_wall"])
        r = xp.resolve_x(f)
        self.assertEqual((r["cls"], r["step"]), ("cutout", 5))
        # wider than a slot and not in a wall run: the slot rule does not apply
        f2 = xp.x_facts((1000, 0, 1600, 200), self.mass([strip(0, 0, 3000, 200), strip(1000, 0, 1600, 200)]))
        self.assertNotIn("slot", str(f2["in_wall"]))

    def test_a_lift_is_walled_on_three_sides(self):
        # a 2400 x 2400 X: walls W, E and N - a lift (step 3)
        m = self.mass([strip(-200, 0, 0, 2600), strip(2400, 0, 2600, 2600), strip(-200, 2400, 2600, 2600)])
        f = xp.x_facts((0, 0, 2400, 2400), m)
        self.assertTrue(f["lift_size"])
        self.assertEqual(f["walled_sides"], 3)
        r = xp.resolve_x(f)
        self.assertEqual((r["cls"], r["step"]), ("lift", 3))
        self.assertEqual(r["trail"][1][0], 2)                      # the shaft rules left it to the lift rules
        # a label naming a shaft keeps a lift-size X a shaft
        self.assertEqual(xp.resolve_x(f, named=True, shaft_name=True)["cls"], "shaft")
        # walled on two sides only: no lift - a shaft (a wall beside it)
        m2 = self.mass([strip(-200, 0, 0, 2600), strip(2400, 0, 2600, 2600)])
        f2 = xp.x_facts((0, 0, 2400, 2400), m2)
        self.assertEqual(f2["walled_sides"], 2)
        self.assertEqual(xp.resolve_x(f2)["cls"], "shaft")
        self.assertEqual(xp.resolve_x(f2, named=True, lift_label=True)["cls"], "shaft")
        # a LIFT label on an X of another size, walled on three sides: a lift
        m3 = self.mass([strip(-200, 0, 0, 1700), strip(1500, 0, 1700, 1700), strip(-200, 1500, 1700, 1700)])
        f3 = xp.x_facts((0, 0, 1500, 1500), m3)
        self.assertFalse(f3["lift_size"])
        self.assertEqual(xp.resolve_x(f3)["cls"], "shaft")
        self.assertEqual(xp.resolve_x(f3, named=True, lift_label=True)["cls"], "lift")

    def test_a_big_x_a_stair_well_and_a_round_x_are_voids(self):
        m = self.mass([strip(-200, 0, 0, 8000)])
        r = xp.resolve_x(xp.x_facts((0, 0, 6000, 6000), m), named=True, lift_label=True)
        self.assertEqual((r["cls"], r["step"]), ("void", 7))       # over 30 sq.m: nothing before 7 takes it
        r = xp.resolve_x(xp.x_facts((0, 1000, 500, 4000), m), stair_well=True, in_room=True)
        self.assertEqual((r["cls"], r["step"]), ("void", 7))
        self.assertIn("stair", r["why"])
        r = xp.resolve_x(xp.x_facts((0, 1000, 500, 1500), m, round_=True), named=True)
        self.assertEqual(r["cls"], "void")                         # a shaft is never round

    def test_every_step_is_on_the_trail(self):
        m = self.mass([strip(0, 0, 3000, 200)])
        r = self.cls((1000, 400, 1600, 1000), m, named=True)
        self.assertEqual([t[0] for t in r["trail"]], [1, 2, 3, 4, 5, 6, 7])
        self.assertTrue(all(c in xp.CLASSES for c in ("shaft", "lift", "window", "cutout", "column", "void")))

    def test_a_lift_sized_x_is_no_opening(self):
        # a 2600 mm X beside a 2600 mm hatched mass is not an opening in it
        m = self.mass([strip(0, 0, 2600, 3000), strip(0, 3000, 2600, 6000)])
        self.assertFalse(xp.x_facts((0, 6000, 2600, 9000), m)["in_wall"])

    def test_a_chase_read_as_a_wall_is_not_wall(self):
        # RWP: a 350 mm "wall" between a 100 mm wall's face and a column's
        # face, holding the X, and no wall runs on at 350: the chase
        m = self.mass([strip(-255, 0, -155, 450), strip(-155, 0, 195, 350), strip(-2904, -200, 195, 0)])
        f = xp.x_facts((0, 0, 195, 350), m)
        self.assertEqual(xp.resolve_x(f)["cls"], "shaft")
        self.assertTrue(f["drop_strips"])

    def test_a_name_over_a_strip(self):
        strips = [strip(0, 0, 300, 1000), strip(2000, 0, 5000, 200), strip(5000, 0, 8000, 200)]
        kept, n = xp.drop_label_strips(strips, [(150, 500), (5000.5, 100)])
        self.assertEqual(n, 1)                                     # the 300 x 1000 piece names a shaft
        self.assertEqual(len(kept), 2)                             # the wall running on keeps its name over it

    def test_the_old_switches_are_gone(self):
        from Drawing_Analyzer import rules
        d = rules.defaults()
        for k in ("x_marks", "x_by_geometry", "x_placement", "windows_in_walls", "dashed_x_cutouts",
                  "stair_void_hole", "lift_x_any_layer", "x_opening_is_shaft", "no_round_shafts",
                  "shaft_on_wall", "x_unlabelled", "trap_door_in_room"):
            self.assertNotIn(k, d)
        self.assertFalse(hasattr(xp, "place_x"))


class DrawingTests(unittest.TestCase):
    """One floor through annotate_dxf: a shaft X beside a wall with its
    frame on A-CUTOUT, an X in a wall with W-1 beside it, an X in a wall
    with nothing, an X on open floor, an X named only by a door tag, and an
    unnamed X with a cut-out tag."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        plate = rect(0, 0, 40000, 30000)
        msp.add_lwpolyline(plate, close=True, dxfattribs={"layer": "A-FLOOR"})

        def lw(pts, layer, close=True):
            msp.add_lwpolyline(pts, close=close, dxfattribs={"layer": layer})

        def x(x0, y0, x1, y1, layer="A-CUTOUT", frame=True):
            msp.add_line((x0, y0), (x1, y1), dxfattribs={"layer": layer})
            msp.add_line((x0, y1), (x1, y0), dxfattribs={"layer": layer})
            if frame:
                lw(rect(x0, y0, x1, y1), layer)

        def text(t, px, py):
            msp.add_mtext(t, dxfattribs={"insert": (px, py), "char_height": 150, "layer": "A-TEXT"})
        # a long 200 mm wall along y 10000-10200, broken by two openings
        lw(rect(2000, 10000, 8000, 10200), "A-WALL")
        lw(rect(9200, 10000, 14000, 10200), "A-WALL")
        lw(rect(15200, 10000, 22000, 10200), "A-WALL")
        x(8000, 10000, 9200, 10200)                               # in the wall, W-1 beside it
        text("W-1", 8400, 10500)
        x(14000, 10000, 15200, 10200)                             # in the wall, nothing beside it
        # a shaft beside the wall (frame on A-CUTOUT), named
        x(3000, 10200, 3300, 10800)
        text("TEN", 3600, 10500)
        # a shaft beside the wall with only a door tag inside it
        x(5000, 10200, 5480, 11020)
        text("SD-6", 5240, 10600)
        # an unnamed X beside the wall with a cut-out tag
        x(18000, 10200, 18501, 10400)
        text("C-10", 18250, 10600)
        # an X on open floor
        x(30000, 20000, 30300, 20600)
        # a lift: a 2400 x 2400 X walled on three sides (W, E, N)
        lw(rect(23800, 10200, 24000, 12800), "A-WALL")
        lw(rect(26400, 10200, 26600, 12800), "A-WALL")
        lw(rect(23800, 12600, 26600, 12800), "A-WALL")
        x(24000, 10200, 26400, 12600, frame=False)
        # a small X (120 mm) in a room, and an X over 30 sq.m
        lw(rect(31000, 4000, 34000, 7000), "A-WALL")
        text("OFFICE", 33000, 6000)
        x(32000, 5000, 32120, 5120, layer="A-FLOR", frame=False)
        x(2000, 20000, 9000, 26000)
        src = os.path.join(tmp, "in.dxf")
        doc.saveas(src)
        floors = [{"name": "F1", "polygon": plate}]
        rows = dr.extract_texts(src, floors)
        cls.src, cls.floors, cls.rows, cls.tmp = src, floors, rows, tmp
        cls.out = os.path.join(tmp, "out.dxf")
        cls.stats = dr.annotate_dxf(src, cls.out, floors, rows)
        cls.doc = ezdxf.readfile(cls.out)
        cls.msp = cls.doc.modelspace()
        cls.areas = dr.compute_room_areas(cls.out, "A-FLOOR")

    def polys(self, layer):
        return [Polygon([(p[0], p[1]) for p in e.get_points("xy")]).buffer(0)
                for e in self.msp.query('LWPOLYLINE[layer=="%s"]' % layer)]

    def on(self, layer, b, share=0.8):
        B = box(*b)
        return any(p.intersection(B).area >= share * B.area for p in self.polys(layer))

    def test_openings_are_a_window_and_a_cutout(self):
        self.assertTrue(self.on(LN.WINDOW, (8000, 10000, 9200, 10200)))
        self.assertTrue(self.on(LN.CUTOUT, (14000, 10000, 15200, 10200)))
        self.assertFalse(self.on("AI_SHAFT", (14000, 10000, 15200, 10200), 0.3))

    def test_the_shaft_beside_the_wall_is_not_wall(self):
        self.assertTrue(self.on("AI_SHAFT", (3000, 10200, 3300, 10800)))
        self.assertFalse(self.on(LN.WALL, (3000, 10200, 3300, 10800), 0.3))

    def test_a_door_tag_names_nothing(self):
        names = [" ".join(str(r["name"]).split()) for r in self.areas["rooms"]]
        self.assertNotIn("SD-6", names)
        self.assertTrue(self.on("AI_SHAFT", (5000, 10200, 5480, 11020)))

    def test_an_unnamed_x_with_a_cutout_tag_is_a_cutout(self):
        self.assertTrue(self.on(LN.CUTOUT, (18000, 10200, 18501, 10400)))
        self.assertFalse(self.on("AI_SHAFT", (18000, 10200, 18501, 10400), 0.3))

    def test_an_x_on_open_floor_is_a_cut_out(self):
        self.assertFalse(self.on("AI_SHAFT", (30000, 20000, 30300, 20600), 0.3))
        self.assertTrue(self.on(LN.CUTOUT, (30000, 20000, 30300, 20600)))

    def test_a_lift_size_x_walled_on_three_sides_is_a_lift(self):
        self.assertTrue(self.on("AI_LIFT", (24000, 10200, 26400, 12600)))
        names = {" ".join(str(r["name"]).split()) for r in self.areas["rooms"] if r["family"] == "Lift"}
        self.assertEqual(names, {"LIFT"})                          # not "Unknown Shaft"

    def test_a_small_x_in_a_room_and_a_big_x_are_voids(self):
        self.assertTrue(self.on("AI_VOID_AREA", (32000, 5000, 32120, 5120)))
        self.assertTrue(self.on("AI_VOID_AREA", (2000, 20000, 9000, 26000)))
        names = {" ".join(str(r["name"]).split()) for r in self.areas["rooms"] if r["family"] == "Void"}
        self.assertIn("VOID", names)                               # an unnamed X void: not "Unknown Shaft"

    def test_every_x_decided_once_and_logged(self):
        log = (self.stats.get("x_placement") or {}).get("F1") or []
        got = sorted((e["cls"], e["step"]) for e in log if e.get("cls"))
        self.assertEqual(got, sorted([("window", 4), ("cutout", 5), ("cutout", 5), ("cutout", 5),
                                      ("shaft", 2), ("shaft", 2), ("lift", 3), ("void", 7), ("void", 7)]))
        for e in log:
            self.assertTrue(e["why"])
            self.assertEqual(e["trail"][0][0], 1)                  # every X a void to start with
            self.assertEqual(e["trail"][-1][0], e["step"])

    def test_what_is_drawn_carries_the_x_sequences_answer(self):
        tags = [dr.x_tag(e) for e in self.msp.query("LWPOLYLINE")]
        tags = [t for t in tags if t]
        classes = sorted(t[0] for t in tags)
        for c in ("window", "cutout", "shaft", "lift", "void"):
            self.assertIn(c, classes)
        win = next(t for t in tags if t[0] == "window")
        self.assertEqual(win[1], 4)
        self.assertIn("W-1", win[2])

    def test_the_element_engine_records_them_as_decided(self):
        from Drawing_Analyzer import elementid as E
        recs = self.stats.get("elements") or []
        six = [r for r in recs if r["classification"] in E.STEP3_CLASSES]
        self.assertTrue(six)
        self.assertTrue(all(r["action"] == E.FIXED_ACTION and not r["review"] for r in six))
        self.assertTrue(any(r["classification"] == E.LIFT and "step 3" in r["notes"] for r in six))

    def test_nothing_overrides_the_x_sequence(self):
        """A family picked in Step 3 for the text in an X, the X picked and
        remapped (or deleted) in the Geometry tab, and a user rule dropping
        small boundaries - none of them changes what the X sequence made of
        an X."""
        from Drawing_Analyzer import userrules as U
        ten = next(r for r in self.rows["F1"] if " ".join(str(r.get("text") or "").split()) == "TEN")
        geo = [{"id": "g1", "kind": "x", "bbox": [3000, 10200, 3300, 10800], "include": True,
                "family": "Toilet", "label": "WC"},
               {"id": "g2", "kind": "x", "bbox": [24000, 10200, 26400, 12600], "include": False,
                "deleted": True}]
        rule = U.parse("do not draw boundaries under 1 sq.m", ["Shaft", "Toilet"], [])["rule"]
        rule["enabled"] = True
        out = os.path.join(self.tmp, "out2.dxf")
        stats = dr.annotate_dxf(self.src, out, self.floors, self.rows,
                                family_overrides={"F1": {str(ten["sr_no"]): "Toilet"}},
                                geometry={"F1": geo}, user_rules=[rule])
        msp = ezdxf.readfile(out).modelspace()

        def on(layer, b, share=0.8):
            B = box(*b)
            return any(Polygon([(p[0], p[1]) for p in e.get_points("xy")]).buffer(0).intersection(B).area
                       >= share * B.area for e in msp.query('LWPOLYLINE[layer=="%s"]' % layer))
        self.assertTrue(on("AI_SHAFT", (3000, 10200, 3300, 10800)))         # TEN: still the shaft
        self.assertFalse(on("AI_TOILET", (3000, 10200, 3300, 10800), 0.3))
        self.assertTrue(on("AI_LIFT", (24000, 10200, 26400, 12600)))         # deleted in the tab: still the lift
        self.assertTrue(on(LN.CUTOUT, (14000, 10000, 15200, 10200)))
        a = (stats.get("x_placement") or {}).get("F1") or []
        b = (self.stats.get("x_placement") or {}).get("F1") or []
        self.assertEqual(sorted((e["bbox"], e["cls"]) for e in a), sorted((e["bbox"], e["cls"]) for e in b))


class SleeveInARoomTests(unittest.TestCase):
    """BHUB AHU 5000 x 4400: 354 mm X marks in 500 mm circles, 125 mm off
    the room's wall - the room is traced round the circles, so the X marks
    stand in a notch of it. Standing in a room, they are voids (step 7),
    not cut-outs."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        plate = rect(0, 0, 20000, 20000)
        msp.add_lwpolyline(plate, close=True, dxfattribs={"layer": "A-FLOOR"})
        for r_ in (rect(4800, 5000, 5000, 10200), rect(10000, 5000, 10200, 10200),
                   rect(4800, 4800, 10200, 5000), rect(4800, 10000, 10200, 10200)):
            msp.add_lwpolyline(r_, close=True, dxfattribs={"layer": "A-WALL"})
        for y0 in (6000, 7000):
            x0 = 5125
            msp.add_line((x0, y0), (x0 + 354, y0 + 354), dxfattribs={"layer": "A3-CUTOUT"})
            msp.add_line((x0, y0 + 354), (x0 + 354, y0), dxfattribs={"layer": "A3-CUTOUT"})
            msp.add_circle((x0 + 177, y0 + 177), 250, dxfattribs={"layer": "A3-CUTOUT"})
        msp.add_mtext("AHU\\P5000 x 4400", dxfattribs={"insert": (7500, 7500), "char_height": 150, "layer": "A-TEXT"})
        src = os.path.join(tmp, "in.dxf")
        doc.saveas(src)
        floors = [{"name": "F1", "polygon": plate}]
        rows = dr.extract_texts(src, floors)
        cls.stats = dr.annotate_dxf(src, os.path.join(tmp, "out.dxf"), floors, rows)

    def test_the_sleeves_are_voids(self):
        log = (self.stats.get("x_placement") or {}).get("F1") or []
        self.assertEqual(len(log), 2)
        self.assertEqual({e["cls"] for e in log}, {"void"})


class FrameAndLabelTests(unittest.TestCase):
    """The second round on the four test floors (XA 37th): a wall face drawn
    along an X's outline, a wall's far face paired with a slab edge beyond
    it, a label naming the room behind a wall, an X drawn turned."""

    diag = staticmethod(lambda sg: abs(sg[1] - sg[3]) > 1 and abs(sg[0] - sg[2]) > 1)

    def test_a_wall_face_along_the_outline_with_a_landing_line_beyond_stays_a_wall(self):
        # XA STR PRE: the X's outline on A-FLOR-OTLN; A-WALL along its top
        # edge, the wall's other face 200 mm above and the stair's landing
        # line 100 mm beyond that
        segs = [(0, 0, 1800, 0, "A-FLOR-OTLN", 1800.0), (1800, 0, 1800, 800, "A-FLOR-OTLN", 800.0),
                (1800, 800, 0, 800, "A-FLOR-OTLN", 1800.0), (0, 800, 0, 0, "A-FLOR-OTLN", 800.0),
                (0, 800, 600, 800, "A-WALL", 600.0), (1200, 800, 1800, 800, "A-WALL", 600.0),
                (-200, 1000, 600, 1000, "A-WALL", 800.0), (1200, 1000, 2000, 1000, "A-WALL", 800.0),
                (-1600, 1100, 2100, 1100, "A-FLOR-OTLN", 3700.0),
                (-1600, 1000, 2200, 1000, "A-ANNO-AREA", 3800.0),       # the area outline along the wall
                (0, 0, 1800, 800, "A-ELEV-HIDN", 1970.0), (0, 800, 1800, 0, "A-ELEV-HIDN", 1970.0)]
        got = xp.frame_lines(segs, [(0, 0, 1800, 800)], skip=self.diag)
        self.assertNotIn(4, got)
        self.assertNotIn(5, got)                   # the A-WALL face
        self.assertIn(2, got)                      # the outline's own edge

    def test_a_wall_whose_far_face_pairs_weakly_beyond_stays_a_wall(self):
        # XA SPARE SH: a 200 mm wall at the X's edge (both faces 803 mm), a
        # 503 mm slab edge 300 mm beyond its far face
        segs = [(0, 0, 0, 803, "A-WALL", 803.0), (200, 0, 200, 803, "A-WALL", 803.0),
                (500, 300, 500, 803, "A-FLOR-OTLN", 503.0),
                (-900, 0, 0, 803, "X", 1206.0), (-900, 803, 0, 0, "X", 1206.0)]
        got = xp.frame_lines(segs, [(-900, 0, 0, 803)], skip=self.diag)
        self.assertNotIn(0, got)
        # the masonry case still holds: both faces the X's length
        segs[2] = (500, 0, 500, 803, "A-FLOR-OTLN", 803.0)
        self.assertIn(0, xp.frame_lines(segs, [(-900, 0, 0, 803)], skip=self.diag))

    def test_a_wall_stands_between_a_label_and_the_x(self):
        m = xp.WallMass([strip(2500, 0, 2700, 1000)], (), [])
        self.assertTrue(m.wall_between(2700, 785, (0, 0, 2500, 500)))      # CA-ELEC SH on the far face
        self.assertFalse(m.wall_between(0, 750, (0, 0, 2500, 500)))        # in the room in front of the X
        self.assertFalse(m.wall_between(2550, 520, (0, 0, 2500, 500)))     # too near to say

    def test_a_turned_x_is_its_turned_rectangle(self):
        # XA PHE SH at 23 degrees: 500 x 621 turned, its box 658 x 748
        import math
        a = math.radians(23.0)
        c, s_ = math.cos(a), math.sin(a)
        pts = [(x * c - y * s_, x * s_ + y * c) for x, y in ((0, 0), (500, 0), (500, 621), (0, 621))]
        cl = {"bbox": (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)),
              "x_ends": [[pts[0], pts[1], pts[2], pts[3]]]}
        q = dr._turned_x_outline(cl)
        self.assertIsNotNone(q)
        self.assertAlmostEqual(q[1], 0.5 * 0.621, delta=0.002)
        square = {"bbox": (0, 0, 500, 600), "x_ends": [[(0, 0), (500, 0), (500, 600), (0, 600)]]}
        self.assertIsNone(dr._turned_x_outline(square))

    def test_a_lift_beside_a_walled_shaft_is_not_its_opening(self):
        from Drawing_Analyzer.area_statement import classify
        lift = [{"name": "PASSENGER LIFT 2.6M X 2.9M", "layer": "AI_LIFT", "opening": True, "inside": False,
                 "shared_mm": 0.0, "shared_near_mm": 0.0, "touches": []}]
        self.assertEqual(classify("CA-ELEC SH", "Shaft", opening=False, neighbours=lift), "elect_shaft")
        x = [{"name": "Unknown Shaft", "layer": "AI_SHAFT", "opening": True, "inside": False,
              "shared_mm": 600.0, "shared_near_mm": 600.0, "touches": []}]
        self.assertEqual(classify("CA-ELEC SH", "Shaft", opening=False, neighbours=x), "shaft_room")

    def test_an_x_across_the_band_is_no_wall_run_but_one_in_line_is(self):
        # Centaurus FHC: an L of two X marks in one cut-out outline - the
        # outline's edges make a 350 mm "wall" through the first X that
        # "runs on" only into the second X, across it
        strips = [strip(0, 0, 1000, 350), strip(0, 0, 1000, 365), strip(1000, 0, 1350, 350),
                  strip(-200, -800, 0, 400), strip(0, 350, 1400, 550)]
        m = xp.WallMass(strips, (), [])
        x1, x2 = (0, 0, 846, 350), (1000, -435, 1350, 350)
        self.assertTrue(xp.x_facts(x1, m)["in_wall"])                # read alone: in a wall
        m.set_xboxes([x1, x2])
        f = xp.x_facts(x1, m)
        self.assertFalse(f["in_wall"])
        self.assertEqual(xp.resolve_x(f)["cls"], "shaft")
        # BHUB: three cut-outs in one 200 mm wall, 100 mm apart - each still
        # an opening, the next one in line being the wall's run
        m2 = xp.WallMass([strip(-1000, 0, 4000, 200)], (), [])
        boxes = [(0, 0, 1300, 200), (1400, 0, 2900, 200)]
        m2.set_xboxes(boxes)
        for b in boxes:
            self.assertTrue(xp.x_facts(b, m2)["in_wall"], b)

    def test_a_staircase_s_two_halves_join_whatever_the_order(self):
        from Drawing_Analyzer import rules
        # two halves of one staircase 300 mm apart, the floor between them
        # two leftover pieces; each order of the pieces gives one staircase
        def items():
            return [{"geom": box(0, 0, 5000, 4000), "fam": "Staircase", "pt": (100, 100)},
                    {"geom": box(5300, 0, 9000, 6000), "fam": "Staircase", "pt": (8000, 100)},
                    {"geom": box(0, 4300, 5000, 6000), "fam": "Staircase", "pt": (100, 5000)}]
        walls = [[rect(-200, -200, 9200, 6200), rect(0, 0, 9000, 6000)],      # the stair's walls round it
                 [rect(5000, 4000, 5300, 4300)]]                             # a pier between the two gaps
        plate = [(-500, -500), (9500, -500), (9500, 6500), (-500, 6500)]
        for order in (False, True):
            it = items()
            if order:
                it.reverse()
            stats = {}
            dr.settle_rooms_and_walls(it, walls, rules.defaults(), stats, plate=plate)
            live = [i for i in it if not i.get("drop")]
            self.assertEqual(len(live), 1, stats)

    def test_an_unnamed_x_takes_its_shaft_rooms_name(self):
        from Drawing_Analyzer import rules
        x = {"label": "Unknown Shaft", "poly": [], "holes": [], "bbox": (0, 0, 2500, 500), "source": "X",
             "xrects": [(0, 0, 2500, 500)], "cx": 1250.0, "cy": 250.0, "sr_no": None}
        rm = {"label": "ELECTRICAL SH", "poly": rect(0, 500, 2500, 1200), "holes": [], "bbox": (0, 500, 2500, 1200),
              "source": "P", "cx": 1250.0, "cy": 850.0, "sr_no": 25}
        fam = lambda d: d.get("family_forced") or dr.family_for_label(d.get("label"), dr._FAMILY_RULES)["name"]
        stats = dr.rooms_and_doors([x, rm], [rect(200, 1200, 1100, 1400)], fam, rules.defaults())
        self.assertEqual(x["label"], "ELECTRICAL SH")
        self.assertEqual(stats.get("x_named_by_room"), 1)
        self.assertTrue(rm.get("shaft_room"))
        x["label"] = "Unknown Shaft"
        x.pop("named_by_room", None)
        rm.pop("shaft_room", None)
        rm.pop("family_forced", None)
        dr.rooms_and_doors([x, rm], [rect(200, 1200, 1100, 1400)], fam, dict(rules.defaults(), x_named_by_its_room=False))
        self.assertEqual(x["label"], "Unknown Shaft")


if __name__ == "__main__":
    unittest.main()
