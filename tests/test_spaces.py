"""The rooms from the spaces (rule rooms_from_spaces, 2026-10-17): every
enclosed space of a floor found with no text, the text applied last.

  * spaces.free_pieces: the free floor cut into its pieces on a grid - the
    open floor (by size, or by its share of a small plate), the wall
    cavities and the tiny pieces left out, one seed per room at its
    widest point.
  * spaces.trace_pieces: one outline per piece, the tracers tried in
    turn, duplicates and slivers dropped.
  * spaces.lift_bank_lobbies: an open lift lobby closed at its banks' ends,
    with the lines that close it.
  * is_pocket / treads_in / shares_side / touches_door: the geometry the
    naming stands on.
  * the family library: ignore words (SLOPE), the Staircase family's
    evidence words (UP / DOWN / DN), the shaft keywords added, the shaft
    room cap of 10 sq.m; the UP / DOWN marks collected from the drawing.
  * end to end: a labelled room, a room with nothing written in it, a
    landing named STAIRCASE with its flight (and UP) in the cell beside
    it, a level note and SLOPE that name nothing.
"""
import os
import sys
import tempfile
import unittest

import ezdxf
from shapely.geometry import box, Point, Polygon

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Drawing_Analyzer import dxf_reader as dr          # noqa: E402
from Drawing_Analyzer import spaces as sp              # noqa: E402
from Drawing_Analyzer import rules as rb               # noqa: E402
from Drawing_Analyzer.labels import build_rules        # noqa: E402

PLATE = [(0, 0), (20000, 0), (20000, 20000), (0, 20000)]


def rect_lines(x0, y0, x1, y1):
    return [(x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)]


class FreePiecesTests(unittest.TestCase):

    def test_rooms_open_floor_cavity_and_tiny(self):
        walls = rect_lines(0, 0, 4000, 5000) + rect_lines(4000, 0, 8000, 5000)   # two rooms in the corner
        walls += rect_lines(10000, 10000, 15000, 10250)                          # a wall cavity 250 wide
        walls += rect_lines(12000, 12000, 12650, 12650)                          # a closet under 0.3 sq.m
        r = sp.free_pieces(walls, PLATE, [box(1000, 1000, 1300, 1600)], cell_mm=50, open_floor_sqm=100)
        self.assertEqual(len(r["seeds"]), 2, r)
        self.assertGreater(r["open_floor_sqm"], 300)
        self.assertGreaterEqual(r["cavities"], 1)
        self.assertGreaterEqual(r["tiny"], 1)
        A, B = box(0, 0, 4000, 5000), box(4000, 0, 8000, 5000)
        for x, y, a in r["seeds"]:
            self.assertTrue(A.contains(Point(x, y)) or B.contains(Point(x, y)), (x, y))
            self.assertTrue(15 < a < 20, a)

    def test_the_hole_in_a_blocked_polygon_is_a_space(self):
        # a wall mass round a cell (walls_before_spaces): the cell it
        # encloses is free floor, one seed inside it
        block = box(5000, 5000, 11000, 11000).difference(box(6000, 6000, 10000, 10000))
        r = sp.free_pieces([], PLATE, [block], cell_mm=50, open_floor_sqm=100)
        self.assertEqual(len(r["seeds"]), 1, r)
        x, y, a = r["seeds"][0]
        self.assertTrue(box(6000, 6000, 10000, 10000).contains(Point(x, y)), (x, y))
        self.assertTrue(14 < a < 16.5, a)

    def test_a_small_plates_open_floor_by_its_share(self):
        # a 12 x 12 m plate with one 4 x 5 room: the rest is under 1000 sq.m
        # and the open floor all the same (half the free floor)
        plate = [(0, 0), (12000, 0), (12000, 12000), (0, 12000)]
        walls = rect_lines(0, 0, 4000, 5000)
        r = sp.free_pieces(walls, plate, [], cell_mm=50, open_floor_sqm=1000, open_floor_share=0.5)
        self.assertEqual(len(r["seeds"]), 1, r)
        self.assertGreater(r["open_floor_sqm"], 100)
        r0 = sp.free_pieces(walls, plate, [], cell_mm=50, open_floor_sqm=1000, open_floor_share=0)
        self.assertEqual(len(r0["seeds"]), 2, r0)


class TracePiecesTests(unittest.TestCase):

    def test_one_outline_per_piece_duplicates_and_slivers_dropped(self):
        A = [(0, 0), (4000, 0), (4000, 5000), (0, 5000)]
        B = [(4000, 0), (8000, 0), (8000, 5000), (4000, 5000)]
        sliver = [(9000, 0), (9200, 0), (9200, 5000), (9000, 5000)]

        def first(x, y):
            return A if Polygon(A).contains(Point(x, y)) else None            # leaks for B

        def second(x, y):
            if Polygon(B).contains(Point(x, y)):
                return B
            if Polygon(sliver).contains(Point(x, y)):
                return sliver
            return None
        seeds = [(2000, 2500, 19.0), (6000, 2500, 19.0), (2500, 2500, 19.0), (9100, 2500, 0.9)]
        out = sp.trace_pieces(seeds, [(first, "walls and doors"), (second, "openings closed")])
        self.assertEqual([s["how"] for s in out], ["walls and doors", "openings closed"])
        self.assertAlmostEqual(out[0]["g"].area / 1e6, 20.0)


class LobbyTests(unittest.TestCase):

    LIFTS = [box(0, 0, 2500, 2500), box(2800, 0, 5300, 2500), box(0, 6000, 2500, 8500), box(2800, 6000, 5300, 8500)]

    def test_an_open_lobby_is_closed_at_the_banks_ends(self):
        out = sp.lift_bank_lobbies(self.LIFTS, [], [], PLATE)
        self.assertEqual(len(out), 1)
        g, lines = out[0]
        self.assertAlmostEqual(g.area / 1e6, 5.3 * 3.5, delta=0.01)
        self.assertEqual(sorted(round(ln[0]) for ln in lines), [0, 5300])
        # a space traced there already is left out
        out2 = sp.lift_bank_lobbies(self.LIFTS, [], [box(0, 2500, 5300, 4000)], PLATE)
        self.assertAlmostEqual(out2[0][0].area / 1e6, 5.3 * 2.0, delta=0.01)

    def test_banks_too_far_apart_hold_no_lobby(self):
        far = [box(0, 0, 2500, 2500), box(2800, 0, 5300, 2500), box(0, 12000, 2500, 14500), box(2800, 12000, 5300, 14500)]
        self.assertEqual(sp.lift_bank_lobbies(far, [], [], PLATE), [])


class GeometryTests(unittest.TestCase):

    def test_pockets(self):
        self.assertTrue(sp.is_pocket(box(5000, 5000, 6000, 6000), PLATE, False, False))
        self.assertFalse(sp.is_pocket(box(5000, 5000, 6000, 6000), PLATE, True, False))
        self.assertFalse(sp.is_pocket(box(5000, 5000, 6000, 6000), PLATE, False, True))
        self.assertTrue(sp.is_pocket(box(0, 5000, 2000, 7000), PLATE, False, False))      # a bay on the plate's edge
        self.assertFalse(sp.is_pocket(box(5000, 5000, 7000, 7000), PLATE, False, False))  # 4 sq.m in the middle

    def test_treads_in(self):
        g = box(0, 0, 3000, 5000)
        self.assertEqual(sp.treads_in(g, [(1000, 1000, 9, None), (9000, 9000, 12, None)]), 9)
        self.assertEqual(sp.treads_in(g, [(9000, 9000, 12, None)]), 0)
        self.assertEqual(sp.treads_in(g, [], segs_arr=object(), stair_treads=lambda s, b, p: 11), 11)

    def test_shares_side_and_touches_door(self):
        g = box(0, 0, 3000, 3000)
        self.assertEqual(len(sp.shares_side(g, [box(3000, 0, 3300, 1000)])), 1)
        self.assertEqual(sp.shares_side(g, [box(3100, 0, 3400, 1000)]), [])         # 100 mm off
        self.assertEqual(sp.shares_side(g, [box(3000, 0, 3300, 100)]), [])          # 100 mm of side only
        self.assertTrue(sp.touches_door(g, [box(1000, -100, 1900, 100)], widths=[900]))
        self.assertFalse(sp.touches_door(g, [box(1000, -500, 1900, -300)]))


class LibraryTests(unittest.TestCase):

    def test_ignore_words_and_evidence_words(self):
        self.assertIn("SLOPE", dr._FAMILY_IGNORE_WORDS)
        self.assertTrue(dr.is_ignorable_label("SLOPE"))
        self.assertTrue(dr.is_ignorable_label(" slope "))
        self.assertFalse(dr.is_ignorable_label("SLOPE RAMP"))
        st = [f for f in dr._FAMILY_RULES if f["name"] == "Staircase"][0]
        self.assertIn("UP", st["evidence"])
        self.assertIn("DOWN", st["evidence"])
        rules = build_rules(dr.CONFIG, dr._FAMILY_RULES, ignore_words=["FOO BAR"])
        self.assertTrue(rules.is_ignored("foo bar"))
        self.assertFalse(rules.is_ignored("FOO BAR ROOM"))

    def test_a_text_in_parentheses_is_a_remark(self):
        self.assertTrue(dr.is_note_text("(EMERGENCY EXIT)"))
        self.assertFalse(dr.looks_like_primary("(EMERGENCY EXIT)"))
        self.assertTrue(dr.is_note_text(" ( typ. ) "))
        self.assertTrue(dr.looks_like_primary("LIFT LOBBY"))
        self.assertTrue(dr.looks_like_primary("LIFT LOBBY (PASSENGER)"))   # a remark after the name is fine

    def test_shaft_keywords_and_the_shaft_room_cap(self):
        for t in ("STAIR PRES", "LCS", "KITCHEN EXHAUST"):
            self.assertEqual(dr.family_for_label(t, dr._FAMILY_RULES)["name"], "Shaft", t)
        self.assertEqual(dr.family_for_label("KITCHEN", dr._FAMILY_RULES)["name"], "Habitable Room")
        self.assertEqual(dr.SHAFT_ROOM_MAX_SQM, 10.0)

    def test_the_rule(self):
        self.assertTrue(rb.defaults()["rooms_from_spaces"])
        self.assertEqual(rb.describe("rooms_from_spaces")["group"], "shape")

    def test_stair_marks_are_collected(self):
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        for t, x in (("UP", 1000), ("DN", 2000), ("DOWN", 3000), ("UPPER FLOOR", 4000), ("LIFT", 5000)):
            msp.add_text(t, dxfattribs={"insert": (x, 500), "height": 150, "layer": "A-TEXT"})
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "s.dxf")
            doc.saveas(p)
            d2, m2, u2 = dr.load_document(p, 4)
            marks = dr._collect_stair_marks(d2, m2, u2)
        self.assertEqual(sorted(round(m[0]) for m in marks), [1000, 2000, 3000])


class EndToEndTests(unittest.TestCase):
    """A 40 x 30 m plate: OFFICE (6 x 4, a door, SLOPE written in it), a
    4 x 3 room with only a level note in it, a landing named STAIRCASE
    with its flight of 9 treads and UP in the cell behind the wall."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        plate = [(0, 0), (40000, 0), (40000, 30000), (0, 30000)]
        msp.add_lwpolyline(plate, close=True, dxfattribs={"layer": "A-FLOOR"})

        def r(x0, y0, x1, y1, layer="A-WALL"):
            msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": layer})

        def text(t, x, y, h=200):
            msp.add_mtext(t, dxfattribs={"insert": (x, y), "char_height": h, "layer": "A-TEXT"})
        t = 200
        # OFFICE 10000..16000 x 10000..14000, a 1000 door in its bottom wall at 12000..13000
        r(10000 - t, 10000 - t, 12000, 10000)
        r(13000, 10000 - t, 16000 + t, 10000)
        msp.add_arc((12000, 10000), 1000, 0, 90, dxfattribs={"layer": "A-DOOR"})
        msp.add_line((12000, 10000), (12000, 11000), dxfattribs={"layer": "A-DOOR"})
        r(10000 - t, 14000, 16000 + t, 14000 + t)
        r(10000 - t, 10000, 10000, 14000)
        r(16000, 10000, 16000 + t, 14000)
        text("OFFICE", 13000, 12000)
        text("SLOPE", 11000, 10500, 100)
        # a 4 x 3 room with nothing but a level note: 20000..24000 x 10000..13000
        r(20000 - t, 10000 - t, 24000 + t, 10000)
        r(20000 - t, 13000, 24000 + t, 13000 + t)
        r(20000 - t, 10000, 20000, 13000)
        r(24000, 10000, 24000 + t, 13000)
        text("FFL +100.00", 22000, 11500, 100)
        # the staircase: landing 28000..32800 x 20000..21900 named, wall 19800..20000, flight 18200..19800
        r(27800, 21900, 33000, 22100)
        r(27800, 19800, 33000, 20000)
        r(27800, 18000, 33000, 18200)
        r(27800, 18200, 28000, 21900)
        r(32800, 18200, 33000, 21900)
        text("STAIRCASE", 30400, 20900)
        for k in range(9):
            x = 29000 + 300 * k
            msp.add_line((x, 18200), (x, 19800), dxfattribs={"layer": "COLUMN"})
        text("UP", 28400, 19000, 100)
        src = os.path.join(tmp, "in.dxf")
        doc.saveas(src)
        floors = [{"name": "F1", "polygon": plate}]
        rows = dr.extract_texts(src, floors)
        out = os.path.join(tmp, "out.dxf")
        cache = dr._PASS_CACHE_DIR
        dr.set_pass_cache_dir(None)
        try:
            cls.stats = dr.annotate_dxf(src, out, floors, rows)
        finally:
            dr.set_pass_cache_dir(cache)
        cls.areas = dr.compute_room_areas(out, "AI_FLOOR")

    def names(self):
        return [" ".join(str(r["name"]).split()) for r in self.areas["rooms"]]

    def rooms(self, word):
        return [r for r in self.areas["rooms"] if word in " ".join(str(r["name"]).split())]

    def test_the_spaces_are_traced_and_named_last(self):
        self.assertGreaterEqual(self.stats.get("spaces_traced", 0), 3, self.stats)
        office = self.rooms("OFFICE")
        self.assertEqual(len(office), 1, self.names())
        self.assertAlmostEqual(office[0]["area_sqm"], 24.0, delta=0.6)
        self.assertNotIn("SLOPE", self.names())

    def test_a_room_with_only_a_note_in_it_is_a_room_of_no_name(self):
        self.assertFalse(any("FFL" in n for n in self.names()), self.names())
        unnamed = self.rooms("ROOM")
        self.assertEqual(len(unnamed), 1, self.names())
        self.assertAlmostEqual(unnamed[0]["area_sqm"], 12.0, delta=0.4)
        self.assertGreaterEqual(self.stats.get("spaces_unnamed", 0), 1)

    def test_the_landing_and_its_flight_are_the_staircase(self):
        stairs = sorted(r["area_sqm"] for r in self.rooms("STAIRCASE"))
        self.assertEqual(len(stairs), 2, self.names())
        self.assertAlmostEqual(stairs[0], 4.8 * 1.6, delta=0.4)
        self.assertAlmostEqual(stairs[1], 4.8 * 1.9, delta=0.4)
        self.assertEqual(self.stats.get("spaces_staircases", 0), 2, self.stats)


if __name__ == "__main__":
    unittest.main()
