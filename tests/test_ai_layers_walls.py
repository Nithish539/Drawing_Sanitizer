# -*- coding: utf-8 -*-
"""Columns in the outputs, walls by class, and the AI_ layer names.

Run:  python -m unittest discover -s tests -v
"""

import importlib
import math
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402
from shapely.geometry import box  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import geoengine as ge  # noqa: E402
from Drawing_Analyzer import geocompare as gc  # noqa: E402
from Drawing_Analyzer import layernames as LN  # noqa: E402


def seg(x1, y1, x2, y2, layer):
    return (x1, y1, x2, y2, layer, math.hypot(x2 - x1, y2 - y1), -1, False)


PLATE = [(0, 0), (40000, 0), (40000, 40000), (0, 40000)]


# ------------------------------------------------------------- 3. names ---
class LayerNameTests(unittest.TestCase):

    def test_new_names(self):
        self.assertEqual(LN.ai_layer("TOILET_BOUNDARY"), "AI_TOILET")
        self.assertEqual(LN.ai_layer("ROOM_BOUNDARY"), "AI_FLOOR")
        self.assertEqual(LN.ai_layer("ROOM_VOID"), "AI_VOID")
        self.assertEqual(LN.ai_layer("AI_GEO_STAIRCASE"), "AI_STAIR")
        self.assertEqual(LN.ai_layer("AI_GEO_STAIR_WELL"), "AI_SHAFT")
        self.assertEqual(LN.ai_layer("AI_GEO_LIFT_LOBBY"), "AI_LIFTLOBBY")
        self.assertEqual(LN.ai_layer("AI_GEO_COLUMN"), "AI_COLUMN")
        self.assertEqual(LN.ai_layer("CMP_AGREEMENT"), "AI_CMP_AGREEMENT")
        # the Void family's own name is the holes' layer: it gets _AREA
        self.assertEqual(LN.family_layer("VOID"), "AI_VOID_AREA")
        self.assertEqual(dr._family_layer({"layer": "SHAFT"}), "AI_SHAFT")

    def test_readers_take_both_forms(self):
        for old, new, key in (("TOILET_BOUNDARY", "AI_TOILET", "TOILET"),
                              ("VOID_BOUNDARY", "AI_VOID_AREA", "VOID"),
                              ("AI_GEO_STAIRCASE", "AI_STAIR", "STAIR")):
            self.assertEqual(LN.room_key(old), key)
            self.assertEqual(LN.room_key(new), key)
            self.assertEqual(dr.family_from_layer(old, dr._FAMILY_RULES)["name"],
                             dr.family_from_layer(new, dr._FAMILY_RULES)["name"])
        self.assertTrue(LN.is_void_layer("AI_VOID") and LN.is_void_layer("ROOM_VOID"))
        self.assertFalse(LN.is_void_layer("AI_VOID_AREA"))

    def test_reserved_layers_are_never_rooms(self):
        for L in ("AI_FLOOR", "AI_VOID", "AI_WINDOW", "AI_COLUMN", "AI_WALL", "AI_HANGING_WALL",
                  "AI_WALL_CORE", "AI_WALL_EXTERNAL", "AI_WALL_INTERNAL", "AI_CMP_TEXT", "AI_UNKNOWN_SHAPE",
                  "ROOM_VOID", "AI_GEO_COLUMN", "AI_GEO_UNKNOWN", "A-WALL"):
            self.assertIsNone(LN.room_key(L), L)
            self.assertTrue(LN.is_reserved(L) or L in ("ROOM_VOID", "AI_GEO_COLUMN", "AI_GEO_UNKNOWN", "A-WALL"), L)

    def test_one_wall_layer(self):
        self.assertEqual(LN.WALLS, ("AI_WALL",))
        self.assertEqual(dr.WALL_CLASSES, ("AI_WALL",))
        # a family whose layer would be a wall layer gets _AREA after it
        self.assertEqual(LN.family_layer("WALL"), "AI_WALL_AREA")
        self.assertEqual(LN.family_layer("HANGING_WALL"), "AI_HANGING_WALL_AREA")
        # a drawing written before walls were one layer still reads its walls as walls
        for old in LN.OLD_WALLS:
            self.assertTrue(LN.is_reserved(old))
            self.assertEqual(LN.ai_layer(old), old)

    def test_draw_walls_one_layer_old_results_too(self):
        ring = [[[0.0, 0.0], [1000.0, 0.0], [1000.0, 200.0], [0.0, 200.0]]]
        doc = ezdxf.new()
        msp = doc.modelspace()
        units = dr.MM_UNITS
        dr.draw_walls(doc, msp, units, {"AI_WALL": [ring]})
        dr.draw_walls(doc, msp, units, {"AI_WALL_CORE": [ring], "AI_WALL_INTERNAL": [ring]})   # a result saved before
        self.assertEqual({e.dxf.layer for e in msp.query("LWPOLYLINE")}, {"AI_WALL"})
        self.assertEqual(doc.layers.get("AI_WALL").color, dr.WALL_ACI["AI_WALL"])
        self.assertNotIn(dr.WALL_ACI["AI_WALL"], (7, 250, 8))         # visible on dark and white

    def test_walls_summary_one_row(self):
        from Drawing_Analyzer.reports import walls_summary, hanging_walls_summary
        self.assertEqual(walls_summary({"walls_count": 4, "walls_length_m": 12.34, "walls_area_sqm": 2.5}),
                         {"AI_WALL": {"count": 4, "length_m": 12.3, "area_sqm": 2.5}})
        old = {"walls_external_count": 1, "walls_core_count": 2, "walls_internal_count": 3,
               "walls_external_area_sqm": 1.0, "walls_core_area_sqm": 2.0, "walls_internal_area_sqm": 3.0}
        self.assertEqual(walls_summary(old)["AI_WALL"]["count"], 6)
        self.assertEqual(walls_summary(old)["AI_WALL"]["area_sqm"], 6.0)
        self.assertEqual(hanging_walls_summary({}), {})
        h = hanging_walls_summary({"hanging_walls_count": 1, "hanging_walls_area_sqm": 3.54,
                                   "hanging_walls_to_rooms_sqm": 2.3})
        self.assertEqual((h["count"], h["area_sqm"], h["to_rooms_sqm"]), (1, 3.54, 2.3))

    def _drawing(self, path, old):
        """A plate with a toilet holding a column hole, a wall and a column:
        on the old names or the new ones."""
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        L = (lambda n: n) if old else LN.ai_layer
        msp.add_lwpolyline(PLATE, close=True, dxfattribs={"layer": L("ROOM_BOUNDARY")})
        msp.add_lwpolyline([(1000, 1000), (5000, 1000), (5000, 4000), (1000, 4000)], close=True,
                           dxfattribs={"layer": L("TOILET_BOUNDARY")})
        msp.add_mtext("TOILET", dxfattribs={"layer": L("TOILET_BOUNDARY"), "insert": (3000, 2500)})
        msp.add_lwpolyline([(2000, 2000), (2500, 2000), (2500, 2500), (2000, 2500)], close=True,
                           dxfattribs={"layer": L("ROOM_VOID")})
        msp.add_lwpolyline([(10000, 10000), (10000, 10000 + 6000), (10200, 16000), (10200, 10000)], close=True,
                           dxfattribs={"layer": "AI_WALL_INTERNAL" if old else "AI_WALL"})
        msp.add_lwpolyline([(20000, 20000), (21000, 20000), (21000, 21000), (20000, 21000)], close=True,
                           dxfattribs={"layer": "AI_COLUMN"})
        doc.saveas(path)

    def test_areas_identical_old_and_new_names(self):
        floors = [{"name": "F", "polygon": PLATE}]
        with tempfile.TemporaryDirectory() as tmp:
            res = []
            for old in (True, False):
                p = os.path.join(tmp, "old.dxf" if old else "new.dxf")
                self._drawing(p, old)
                r = dr.compute_room_areas(p, "X", unit_override=4, floors=floors)
                res.append([(x["family"], x["area_sqm"], x["void_area_sqm"]) for x in r["rooms"]])
            self.assertEqual(res[0], res[1])
            # one room: the toilet, its column hole deducted; no wall, no column
            self.assertEqual(res[1], [("Toilet", 11.75, 0.25)])

    def test_geometry_writes_family_layers(self):
        self.assertEqual(ge.LAYER_OF["STAIRCASE"], "AI_STAIR")
        self.assertEqual(ge.LAYER_OF["UNKNOWN_GEOMETRY"], "AI_UNKNOWN_SHAPE")
        # a shaft, lift, window, cut-out, column or void is Step 3's
        # (2026-10-16): the geometry engine has no class for it
        for c in ("SHAFT", "COLUMN", "LIFT", "WINDOW", "CUTOUT", "VOID", "STAIR_WELL"):
            self.assertNotIn(c, ge.LAYER_OF)
            self.assertIn(c, ge.STEP3_CLASSES)


# ----------------------------------------------------------- 1. columns ---
class ColumnTests(unittest.TestCase):

    def test_hollow_shapes_are_not_columns(self):
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        lay = "S-COLUMN"
        for i in range(4):
            for j in range(4):
                x, y = 6000 + 8000 * i, 6000 + 8000 * j
                msp.add_lwpolyline([(x, y), (x + 1000, y), (x + 1000, y + 1000), (x, y + 1000)], close=True,
                                   dxfattribs={"layer": lay})
        # a C-shaped core wall on the column layer: 3150 x 3450, 300 thick
        c = [(2000, 2000), (5150, 2000), (5150, 2300), (2300, 2300), (2300, 5150), (5150, 5150),
             (5150, 5450), (2000, 5450)]
        msp.add_lwpolyline(c, close=True, dxfattribs={"layer": lay})
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "c.dxf")
            doc.saveas(p)
            d2, m2, u2 = dr.load_document(p, 4)
            rej = {}
            cols = ge._column_shapes(d2, m2, u2, [{"name": "F", "polygon": PLATE}], rejected=rej)
        self.assertEqual(len(cols["F"]), 16)
        self.assertTrue(all(abs(pg.area - 1e6) < 1 for pg, _l, _h in cols["F"]))
        self.assertEqual(len(rej["F"]), 1)                          # the C goes to the walls
        self.assertGreater(rej["F"][0].convex_hull.area, ge.COLUMN_HOLLOW_RATIO * rej["F"][0].area)

    def test_an_older_results_columns_are_not_read(self):
        """A column is Step 3's (2026-10-16): a geometry COLUMN in a result
        written before then is neither compared nor drawn."""
        col = {"id": "GEO-1", "cls": "COLUMN", "label": "COL_01", "poly": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]],
               "features": {}, "confidence": None, "category": "", "reviewed": False}
        cmp = gc.compare({"floors": {"F": [col]}}, {"F": []})
        self.assertEqual(cmp["rows"], [])
        self.assertEqual(cmp["summary"]["categories"]["GEO_ONLY"], 0)

    def test_column_in_a_room_is_a_hole(self):
        A = importlib.import_module("Drawing_Analyzer.app")
        rows = [{"family": "Toilet", "poly": [(0, 0), (6000, 0), (6000, 4000), (0, 4000)]},
                {"family": "Column", "poly": [(2000, 1000), (3000, 1000), (3000, 2000), (2000, 2000)]},
                {"family": "Column", "poly": [(9000, 9000), (10000, 9000), (10000, 10000), (9000, 10000)]}]
        holes = A._column_holes(rows)
        self.assertEqual(len(holes), 1)                             # only the one in the room

    def test_build_final_draws_no_column_of_its_own(self):
        res = {"floors": {"F": [{"id": "GEO-1", "cls": "COLUMN", "label": "COL_01", "poly": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]],
                                 "rule": "column_grid", "confidence": None}]}}
        out = ge.build_final(res, None, dr.list_families())
        self.assertEqual(out["floors"]["F"], [])
        # a shape drawn by the user as a Column (or any of the six) is not taken
        res = {"floors": {"F": []}}
        dec = {"F": {"u1": {"include": True, "family": "Column", "label": "C",
                            "poly": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]]}}}
        out = ge.build_final(res, dec, dr.list_families())
        self.assertEqual(out["floors"]["F"], [])
        self.assertEqual(out["step3_refused"], 1)


# ------------------------------------------------------------- 2. walls ---
class WallTests(unittest.TestCase):

    def strip(self, x0, y0, x1, y1, t, *layers):
        """A strip along (x0, y0)-(x1, y1), t thick: its first edge runs
        along the wall, as _detect_walls gives it."""
        if y0 == y1:
            pts = [(x0, y0), (x1, y0), (x1, y0 + t), (x0, y0 + t)]
        else:
            pts = [(x0, y0), (x0, y1), (x0 + t, y1), (x0 + t, y0)]
        return {"pts": pts, "t": t, "layers": layers}

    def test_every_wall_on_one_layer(self):
        strips = [
            self.strip(0, 0, 40000, 0, 300, "A-WALL", "A-WALL"),                       # along the edge
            self.strip(10000, 10000, 10000, 16000, 200, "XREF-T1_STR$1$COLUMN", "XREF-T1_STR$1$COLUMN"),
            self.strip(15000, 10000, 15000, 16000, 150, "A3-WL_EXT", "A3-WL_EXT"),     # block work
            self.strip(20000, 10000, 20000, 16000, 400, "A3-WL_EXT", "A3-WL_EXT"),     # thick: core
            self.strip(25000, 10000, 25000, 16000, 100, "A3-GLASS", "A3-GLASS"),       # glazing inside
            self.strip(30000, 10000, 30000, 16000, 150, "3.3 BALCONY", "A3-WL_EXT"),   # area plan: no wall
        ]
        # a concrete hatch under the 35000 wall: structure
        hatch = ([(35000, 10000), (35200, 10000), (35200, 16000), (35000, 16000)], [], "ANY-LAYER", "AR-CONC")
        strips.append(self.strip(35000, 10000, 35000, 16000, 200, "A3-WL_EXT", "A3-WL_EXT"))
        pieces, rep = dr.wall_polygons(strips, PLATE, hatches=[hatch])

        self.assertEqual(set(pieces), {LN.WALL})
        self.assertEqual(set(rep), {LN.WALL})

        def at(x):
            return any(p.intersects(box(x + 20, 12000, x + 21, 12001)) for p in pieces[LN.WALL])
        # along the plate's edge, on a structure layer, block work, thick, glazing inside,
        # on a structure hatch: all walls, all on AI_WALL
        self.assertTrue(any(p.intersects(box(20000, 100, 20001, 101)) for p in pieces[LN.WALL]))
        for x in (10000, 15000, 20000, 25000, 35000):
            self.assertTrue(at(x), x)
        self.assertFalse(at(30000))                                    # the area-plan overlay: no wall

    def test_mirror_lines_give_one_piece(self):
        # a 200 mm block wall (y 95566-95766, 3.55 m) with 600 mm mirror
        # lines 44 mm in front of it: each mirror line pairs with the far
        # face of the wall, so 600 mm strips 244 thick lie on the wall
        wall = self.strip(164956, 95566, 168506, 95566, 200, "A3-WL_EXT", "A3-WL_EXT")
        mirrors = [{"pts": [(x, 95766), (x + 600, 95766), (x + 600, 95522), (x, 95522)], "t": 244,
                    "layers": ("A3-WL_EXT", "A3-GLASS")} for x in (165120, 165920, 166720)]
        twin = dict(wall)                                              # a duplicate of the wall strip
        plate = [(164756, 90000), (175000, 90000), (175000, 100000), (164756, 100000)]
        counts = {}
        pieces, rep = dr.wall_polygons([wall] + mirrors + [twin], plate, counts=counts)
        self.assertEqual(rep[LN.WALL]["count"], 1)
        p = pieces[LN.WALL][0]
        self.assertAlmostEqual(p.bounds[1], 95566, delta=1)
        self.assertAlmostEqual(p.bounds[3], 95766, delta=1)            # nothing past the wall
        self.assertEqual(counts["duplicates_removed"], 1)
        self.assertEqual(counts["absorbed_removed"], 3)
        self.assertEqual(set(pieces), {LN.WALL})

    def test_small_piece_between_strips_is_kept(self):
        # a 200 x 240 piece of wall between two runs (where they meet, of
        # another thickness) is under min_piece_sqm, but touches them: kept
        a = self.strip(10000, 10000, 13000, 10000, 200, "A-W", "A-W")
        gap = {"pts": [(13000, 10000), (13200, 10000), (13200, 10240), (13000, 10240)], "t": 240, "layers": ("A-W", "A-W")}
        b = self.strip(13200, 10000, 16000, 10000, 200, "A-W", "A-W")
        pieces, rep = dr.wall_polygons([a, gap, b], PLATE)
        total = sum(p.area for p in pieces[LN.WALL]) / 1e6
        self.assertAlmostEqual(total, 0.6 + 0.048 + 0.56, places=3)
        self.assertEqual(rep[LN.WALL]["count"], 1)                     # one wall, no gap
        # on its own, the same small piece is a sliver and goes
        alone, _r = dr.wall_polygons([gap], PLATE)
        self.assertEqual(alone[LN.WALL], [])

    def test_a_wall_meeting_the_facade_is_one_piece(self):
        strips = [self.strip(10000, 0, 10000, 8000, 150, "A3-WL_EXT", "A3-WL_EXT")]   # runs in from the edge
        pieces, _r = dr.wall_polygons(strips, PLATE)
        self.assertEqual(len(pieces[LN.WALL]), 1)

    def test_cut_outs_and_core_shapes(self):
        strips = [self.strip(10000, 10000, 10000, 20000, 200, "A3-WL_EXT", "A3-WL_EXT")]
        col = box(9900, 14000, 10300, 14400)
        win = box(10000, 17000, 10200, 18000)
        c_shape = [(2000, 2000), (5150, 2000), (5150, 2300), (2300, 2300), (2300, 5150), (5150, 5150),
                   (5150, 5450), (2000, 5450)]
        pieces, rep = dr.wall_polygons(strips, PLATE, columns=[col], windows=[win], extra_core=[c_shape])
        self.assertEqual(rep[LN.WALL]["count"], 4)                         # cut in three, and the C / L shape
        self.assertFalse(any(p.intersects(col.buffer(-5)) for p in pieces[LN.WALL]))
        self.assertTrue(any(p.intersects(box(2100, 3000, 2101, 3001)) for p in pieces[LN.WALL]))

    def test_detect_walls_gives_the_strip(self):
        w = dr._detect_walls([seg(0, 0, 4000, 0, "A-WALL"), seg(0, 200, 4000, 200, "A-WALL")])
        self.assertEqual(len(w["strips"]), 1)
        self.assertAlmostEqual(w["strips"][0]["t"], 200.0)

    def test_door_openings_are_quads(self):
        q = dr.pair_quads([(0, 0, 900, 0), (0, 200, 900, 200)])
        self.assertEqual(len(q), 1)


if __name__ == "__main__":
    unittest.main()
