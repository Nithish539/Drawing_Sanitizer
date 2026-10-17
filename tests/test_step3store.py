# -*- coding: utf-8 -*-
"""The Step 3 result store (step3store.py, 2026-10-17): what one Generate
decided, kept once and read by every later step in place of the geometry
engine's second run.

  * annotate_dxf returns it (stats["step3"]): one entry per floor with the
    walls, the doors and their lines, the columns, every room with its
    confidence, the spaces and the X marks - plain JSON.
  * findings(): the Geometry tab's records from the rooms (not Step 3's
    six), with the shape apply_reviews / build_final / the comparison read.
  * as_geo(), side_shapes(), confidence_index(): what the final drawing
    takes from it instead of reading the Step 3 drawing back.
  * trace_context(): the walls a click is traced against.
  * write_dxf draws the doors as Step 3 drew them (kinds, levels, lines).
"""
import json
import os
import sys
import tempfile
import unittest

import ezdxf
from shapely.geometry import Point, Polygon, box

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Drawing_Analyzer import dxf_reader as dr          # noqa: E402
from Drawing_Analyzer import geoengine as ge           # noqa: E402
from Drawing_Analyzer import step3store as s3          # noqa: E402

PLATE = [[0, 0], [30000, 0], [30000, 20000], [0, 20000]]


def ring(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def store():
    """One floor, by hand: a wall piece with a hole, one door, one column,
    a staircase with its well, an office traced as a space, a shaft (the X
    sequence's - never a finding), a space the office came from."""
    office = ring(10000, 10000, 16000, 14000)
    stair = ring(20000, 10000, 24000, 16000)
    well = ring(21500, 12000, 22500, 14000)
    door = [[12000, 9800], [13000, 9800], [13000, 10000], [12000, 10000]]
    return {"version": 1, "code": "v2:test", "units": {"label": "Millimetres"}, "project": "", "family_filter": "",
            "written": "2026-10-17 12:00:00",
            "floors": {"F1": {
                "plate": PLATE, "layer": "A-FLOOR", "usage": "Office",
                "walls": {"AI_WALL": [[ring(9800, 9800, 16200, 14200), ring(10000, 10000, 16000, 14000)],
                                      ring(19800, 9800, 24200, 10000)]},
                "doors": [door], "door_kinds": ["exit"], "door_levels": ["primary"],
                "door_sides": [["OFFICE", "(common floor)"]], "door_owners": [1],
                "door_lines": [[[12000, 10000], [13000, 10000]]], "door_dirs": [[0.0, 1.0]],
                "door_review": [], "shaft_review": [], "glass": [], "windows": [], "cutouts": [],
                "columns": [ring(5000, 5000, 5600, 5600)],
                "hanging_walls": [],
                "rooms": [
                    {"label": "OFFICE", "family": "Room", "layer": "AI_ROOM", "poly": office, "holes": [],
                     "source": "G", "x_class": None, "cell": False, "sr_no": 3, "absorbed_srs": [],
                     "at": [13000, 12000], "confidence": 0.86, "reasons": ["named by the text inside it", "- no stated size"],
                     "evidence": ["traced on walls and doors"], "cutout_of": None},
                    {"label": "STAIRCASE", "family": "Staircase", "layer": "AI_STAIR", "poly": stair, "holes": [well],
                     "source": "P", "x_class": None, "cell": False, "sr_no": 5, "absorbed_srs": [],
                     "at": [22000, 11000], "confidence": 0.7, "reasons": ["a stair name"], "evidence": [], "cutout_of": None},
                    {"label": "PHE SHAFT", "family": "Shaft", "layer": "AI_SHAFT", "poly": ring(2000, 2000, 3200, 3200),
                     "holes": [], "source": "X", "x_class": "shaft", "x_step": 7, "x_why": "nothing else", "cell": True,
                     "sr_no": 9, "absorbed_srs": [], "at": [2600, 2600], "confidence": 0.9, "reasons": [], "evidence": [],
                     "cutout_of": None},
                ],
                "spaces": [{"fate": "drawn", "label": "OFFICE", "family": "Room", "sqm": 24.0, "how": "walls and doors",
                            "why": None, "at": [13000, 12000], "texts": ["OFFICE"], "poly": office}],
                "x_placement": [{"bbox": [2000, 2000, 3200, 3200], "cls": "shaft", "step": 7, "why": "nothing else"}],
                "lobby_bank_lines": [], "wall_veto": [], "locked": {}, "text_status": {"3": "drawn"}, "timings": {},
            }}}


class FindingsTests(unittest.TestCase):

    def test_the_rooms_become_the_tabs_records_not_the_six(self):
        res = s3.findings(store(), dr.list_families())
        rows = res["floors"]["F1"]
        self.assertEqual([r["cls"] for r in rows], ["ROOM", "STAIRCASE"])
        self.assertEqual([r["id"] for r in rows], ["GEO-0001", "GEO-0002"])
        self.assertEqual(rows[0]["label"], "OFFICE")
        self.assertEqual(rows[0]["rule"], "step3_space")          # it stands in a traced space
        self.assertEqual(rows[1]["rule"], "step3_label")
        self.assertEqual(rows[0]["features"]["centroid"], [13000, 12000])
        self.assertEqual(rows[0]["features"]["source"], "space")
        self.assertAlmostEqual(rows[0]["confidence"], 0.86)
        self.assertEqual([e["ok"] for e in rows[0]["evidence"]], [True, False, True])
        self.assertEqual(rows[1]["holes"], [[[21500, 12000], [22500, 12000], [22500, 14000], [21500, 14000]]])
        self.assertEqual(res["source"], "step3")
        self.assertEqual(res["summary"]["ROOM"], 1)
        self.assertIn("AI_WALL", res["walls"]["F1"])
        self.assertEqual(res["door_kinds"]["F1"], ["exit"])
        json.dumps(res)                                             # plain JSON

    def test_a_family_without_an_engine_class_travels_as_the_category(self):
        st = store()
        st["floors"]["F1"]["rooms"].append(
            {"label": "AHU ROOM", "family": "MEP / Services", "layer": "AI_MEP", "poly": ring(2000, 10000, 6000, 13000),
             "holes": [], "source": "P", "x_class": None, "cell": False, "sr_no": 7, "absorbed_srs": [],
             "at": [4000, 11500], "confidence": 0.75, "reasons": [], "evidence": [], "cutout_of": None})
        res = s3.findings(st, dr.list_families())
        mep = next(r for r in res["floors"]["F1"] if r["label"] == "AHU ROOM")
        self.assertEqual((mep["cls"], mep["category"], mep["category_layer"]), ("ROOM", "MEP / Services", "MEP"))
        office = next(r for r in res["floors"]["F1"] if r["label"] == "OFFICE")
        self.assertEqual((office["category"], office["category_layer"]), ("", None))
        stair = next(r for r in res["floors"]["F1"] if r["cls"] == "STAIRCASE")
        self.assertEqual(stair["category"], "")                      # the class names the family already
        final = ge.build_final(res, None, dr.list_families())
        self.assertIn("MEP / Services", [x["family"] for x in final["floors"]["F1"]])
        # the comparison reads Step 3's MEP room against the drawing's MEP boundary as agreement
        from Drawing_Analyzer import geocompare as gc
        cmp = gc.compare(res, {"F1": [{"cls": "MEP", "name": "AHU ROOM", "pg": Polygon(ring(2000, 10000, 6000, 13000))}]})
        t = next(x for x in cmp["rows"] if x["text"])
        self.assertEqual((t["category"], t["level"]), ("AGREEMENT", "exact"))

    def test_reviews_and_the_final_output_read_it(self):
        res = s3.findings(store(), dr.list_families())
        mr = res["floors"]["F1"][0]
        rev = {mr["id"]: {"category": "Toilet", "name": "GENTS", "centroid": mr["features"]["centroid"]}}
        out = ge.apply_reviews(res, rev, dr.list_families())
        r = out["floors"]["F1"][0]
        self.assertEqual((r["label"], r["cls"], r["machine"]["label"]), ("GENTS", "TOILET", "OFFICE"))
        final = ge.build_final(res, None, dr.list_families())
        self.assertEqual(sorted(x["family"] for x in final["floors"]["F1"]), ["Room", "Staircase"])
        stair = next(x for x in final["floors"]["F1"] if x["family"] == "Staircase")
        self.assertEqual(len(stair["holes"]), 1)
        self.assertEqual(final["door_lines"]["F1"], [[[12000, 10000], [13000, 10000]]])
        self.assertEqual(final["columns"]["F1"], [ring(5000, 5000, 5600, 5600)])

    def test_write_dxf_draws_the_doors_as_step3_did(self):
        res = s3.findings(store(), dr.list_families())
        final = ge.build_final(res, None, dr.list_families())
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        doc.modelspace().add_lwpolyline([tuple(p) for p in PLATE], close=True, dxfattribs={"layer": "A-FLOOR"})
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "geo.dxf")
        ge.write_dxf(doc, dr.MM_UNITS, final, path)
        d2 = ezdxf.readfile(path)
        msp = d2.modelspace()
        doors = [e for e in msp.query("LWPOLYLINE") if e.dxf.layer == "AI_P_DOORS"]
        self.assertEqual(len(doors), 1)
        pts = [(round(x), round(y)) for x, y in doors[0].get_points("xy")]
        self.assertEqual(pts, [(12000, 10000), (13000, 10000)])        # Step 3's line, not worked out again
        self.assertEqual(dr.door_tag(doors[0]), ("exit", "primary"))
        voids = [e for e in msp.query("LWPOLYLINE") if e.dxf.layer == "AI_VOID"]
        self.assertEqual(len(voids), 1)                                  # the stair well
        self.assertTrue(any(e.dxf.layer == "AI_WALL" for e in msp.query("LWPOLYLINE")))


class FinalDrawingReadersTests(unittest.TestCase):

    def test_as_geo(self):
        geo = s3.as_geo(store())
        self.assertEqual(list(geo["walls"]), ["F1"])
        self.assertEqual(len(geo["doors"]["F1"]), 1)
        self.assertEqual(geo["plates"]["F1"], PLATE)
        self.assertEqual(geo["window_openings"], {"F1": []})
        self.assertEqual(geo["door_owners"]["F1"], [1])
        ws = geo["walls_summary"]["total"]["AI_WALL"]
        self.assertEqual(ws["count"], 2)
        # 6.4 x 4.4 less 6 x 4, plus 4.4 x 0.2
        self.assertAlmostEqual(ws["area_sqm"], 6.4 * 4.4 - 24.0 + 0.88, places=2)

    def test_side_shapes(self):
        side = s3.side_shapes(store())
        self.assertEqual(len(side["wells"]), 1)
        self.assertAlmostEqual(side["wells"][0].area / 1e6, 2.0, places=3)
        self.assertEqual(len(side["columns"]), 1)
        g, mid, d = side["doors"][0]
        self.assertAlmostEqual(g.area, 1000 * 200, delta=1)
        self.assertEqual((round(mid[0]), round(mid[1])), (12500, 10000))
        self.assertEqual(d, (0.0, 1.0))

    def test_confidence_index(self):
        idx = s3.confidence_index(store())
        self.assertEqual(len(idx), 3)
        cx, cy, a, src, label, score, why = next(t for t in idx if t[4] == "OFFICE")
        self.assertEqual((round(cx), round(cy), round(a, 2), src, score), (13000, 12000, 24.0, "G", 0.86))
        self.assertEqual(why, "named by the text inside it; - no stated size")

    def test_trace_context(self):
        ctx = s3.trace_context(store(), {"F1": [{"text": "OFFICE", "x": 13000, "y": 12000}]})["F1"]
        # two rings of the wall piece (8), the strip (4), the door (4), the plate (4)
        self.assertEqual(len(ctx["walls"]), 20)
        self.assertIsNotNone(ctx["idx"])
        self.assertEqual(ctx["rows"][0]["text"], "OFFICE")
        from Drawing_Analyzer.geomextract import trace_at
        got = trace_at(ctx, 13000, 12000)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got[1], 24.0, delta=0.5)                # the office, from the store's walls


class EndToEndTests(unittest.TestCase):
    """annotate_dxf returns the store: a 40 x 30 m plate with an OFFICE
    (6 x 4, a door) and a 4 x 3 room with nothing written in it."""

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
        r(10000 - t, 10000 - t, 12000, 10000)
        r(13000, 10000 - t, 16000 + t, 10000)
        msp.add_arc((12000, 10000), 1000, 0, 90, dxfattribs={"layer": "A-DOOR"})
        msp.add_line((12000, 10000), (12000, 11000), dxfattribs={"layer": "A-DOOR"})
        r(10000 - t, 14000, 16000 + t, 14000 + t)
        r(10000 - t, 10000, 10000, 14000)
        r(16000, 10000, 16000 + t, 14000)
        text("OFFICE", 13000, 12000)
        r(20000 - t, 10000 - t, 24000 + t, 10000)
        r(20000 - t, 13000, 24000 + t, 13000 + t)
        r(20000 - t, 10000, 20000, 13000)
        r(24000, 10000, 24000 + t, 13000)
        src = os.path.join(tmp, "in.dxf")
        doc.saveas(src)
        floors = [{"name": "F1", "polygon": plate, "layer": "A-FLOOR"}]
        rows = dr.extract_texts(src, floors)
        cls.out = os.path.join(tmp, "out.dxf")
        cache = dr._PASS_CACHE_DIR
        dr.set_pass_cache_dir(None)
        try:
            cls.stats = dr.annotate_dxf(src, cls.out, floors, rows)
        finally:
            dr.set_pass_cache_dir(cache)
        cls.store = cls.stats["step3"]

    def test_the_store_is_plain_json_with_the_floor(self):
        text = json.dumps(self.store)
        back = json.loads(text)
        self.assertEqual(list(back["floors"]), ["F1"])
        F = back["floors"]["F1"]
        for key in ("plate", "walls", "doors", "door_kinds", "door_levels", "door_sides", "door_owners",
                    "door_lines", "door_dirs", "rooms", "spaces", "x_placement", "columns", "glass", "text_status"):
            self.assertIn(key, F, key)
        self.assertEqual(back["code"], dr._code_stamp())

    def test_rooms_doors_and_walls_as_drawn(self):
        F = self.store["floors"]["F1"]
        names = sorted(r["label"] for r in F["rooms"])
        self.assertEqual(names, ["OFFICE", "ROOM"], names)
        office = next(r for r in F["rooms"] if r["label"] == "OFFICE")
        self.assertAlmostEqual(Polygon(office["poly"]).area / 1e6, 24.0, delta=0.6)
        self.assertIsNotNone(office["confidence"])
        self.assertTrue(office["reasons"])
        self.assertEqual(len(F["doors"]), 1)
        self.assertEqual(len(F["door_lines"]), 1)
        self.assertEqual(len(F["door_kinds"]), 1)
        self.assertTrue(F["walls"].get("AI_WALL"))
        self.assertGreaterEqual(len(F["spaces"]), 2)
        self.assertTrue(all(len(s["poly"]) >= 3 for s in F["spaces"] if s["fate"].startswith("drawn")))

    def test_the_tab_and_the_final_drawing_read_what_was_drawn(self):
        res = s3.findings(self.store, dr.list_families())
        self.assertEqual(sorted(r["label"] for r in res["floors"]["F1"]), ["OFFICE", "ROOM"])
        side = s3.side_shapes(self.store)
        self.assertEqual(len(side["doors"]), 1)
        # the door's line in the store is the line in the drawing
        d2 = ezdxf.readfile(self.out)
        drawn = [e for e in d2.modelspace().query("LWPOLYLINE") if e.dxf.layer in ("AI_P_DOORS", "AI_S_DOORS")]
        self.assertEqual(len(drawn), 1)
        pts = [(round(x), round(y)) for x, y in drawn[0].get_points("xy")]
        stored = [(round(p[0]), round(p[1])) for p in self.store["floors"]["F1"]["door_lines"][0]]
        self.assertEqual(pts, stored)
        idx = s3.confidence_index(self.store)
        self.assertTrue(any(t[4] == "OFFICE" for t in idx))


class PrepareAheadTests(unittest.TestCase):
    """Step 2 done: the drawing's preparation is worked out on the parsed
    copy into the prep cache, and Generate finds it there."""

    def test_the_prep_is_in_the_cache_before_generate(self):
        tmp = tempfile.mkdtemp()
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        plate = [(0, 0), (20000, 0), (20000, 15000), (0, 15000)]
        msp.add_lwpolyline(plate, close=True, dxfattribs={"layer": "A-FLOOR"})
        for x0, y0, x1, y1 in ((4800, 4800, 10200, 5000), (4800, 9000, 10200, 9200), (4800, 5000, 5000, 9000),
                               (10000, 5000, 10200, 9000)):
            msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": "A-WALL"})
        msp.add_mtext("STORE", dxfattribs={"insert": (7500, 7000), "char_height": 200, "layer": "A-TEXT"})
        src = os.path.join(tmp, "in.dxf")
        doc.saveas(src)
        floors = [{"name": "F1", "polygon": plate, "layer": "A-FLOOR"}]
        rows = dr.extract_texts(src, floors)                      # Step 2's parse (kept by dxfio)
        cache = dr._PASS_CACHE_DIR
        dr.set_pass_cache_dir(os.path.join(tmp, "cache"))
        try:
            prep = dr.prepare_floors_ahead(src, None, floors)
            self.assertIn("raw_segs", prep)
            preps = [n for n in os.listdir(os.path.join(tmp, "cache")) if n.startswith("prep_")]
            self.assertEqual(len(preps), 1)
            before = os.path.getmtime(os.path.join(tmp, "cache", preps[0]))
            stats = dr.annotate_dxf(src, os.path.join(tmp, "out.dxf"), floors, rows)
            # Generate took the prepared drawing: the one file, not rewritten
            preps2 = [n for n in os.listdir(os.path.join(tmp, "cache")) if n.startswith("prep_")]
            self.assertEqual(preps2, preps)
            self.assertEqual(os.path.getmtime(os.path.join(tmp, "cache", preps[0])), before)
            self.assertIn("step3", stats)
            self.assertEqual([r["label"] for r in stats["step3"]["floors"]["F1"]["rooms"]], ["STORE"])
        finally:
            dr.set_pass_cache_dir(cache)


if __name__ == "__main__":
    unittest.main()
