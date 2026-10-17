# -*- coding: utf-8 -*-
"""The Geometry tab's result shape (geoengine.py: build_final, apply_reviews,
write_dxf) and its comparison with the text engine (geocompare.py).

The geometry engine's run is gone (2026-10-17): the findings are Step 3's,
read from the Step 3 result store (step3store.py, tests in
test_step3store.py). What is tested here is the record shape every reader
of a result still depends on, on a hand-built result.

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402
from shapely.geometry import Polygon, box  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import geoengine as ge  # noqa: E402
from Drawing_Analyzer import geocompare as gc  # noqa: E402

FLOOR = [(0, 0), (60000, 0), (60000, 40000), (0, 40000)]


def _finding(seq, cls, label, pg, rule="step3_space", confidence=0.8):
    f = ge._shape(pg)
    f["source"] = "space"
    f["layers"] = [ge.LAYER_OF[cls]]
    return {"id": "GEO-{0:04d}".format(seq), "label": label, "cls": cls, "sub": "", "floor": "F1",
            "rule": rule, "confidence": confidence, "confidence_note": "", "reason": "",
            "evidence": [{"ok": True, "metadata": False, "text": "Traced from the walls as an enclosed space"}],
            "family": None, "features": f, "repeats": None, "bank": None, "near": [],
            "poly": [[round(x), round(y)] for x, y in list(pg.exterior.coords)[:-1]], "holes": [],
            "area_sqm": f["area_sqm"]}


def result():
    """Two findings, as Step 3's store gives them: a staircase round its
    well and a machine room between two lift banks."""
    stair = box(8500, 19500, 12400, 24600)
    mr = box(5000, 7700, 13400, 9900)
    return {"engine": "step3", "version": 2, "source": "step3",
            "floors": {"F1": [_finding(1, "STAIRCASE", "ST-01", stair), _finding(2, "MACHINE_ROOM", "LMR", mr)]},
            "summary": {"STAIRCASE": 1, "MACHINE_ROOM": 1},
            "walls": {"F1": {"AI_WALL": [[[[8300, 19300], [8500, 19300], [8500, 24800], [8300, 24800]]]]}},
            "doors": {"F1": []}, "door_review": {"F1": []}, "glass": {"F1": []}, "hanging_walls": [],
            "plates": {"F1": [list(p) for p in FLOOR]}, "window_openings": {"F1": []}}


class ResultShapeTests(unittest.TestCase):

    def test_build_final_keeps_the_findings_and_the_walls(self):
        out = ge.build_final(result(), None, dr.list_families())
        fams = sorted(r["family"] for r in out["floors"]["F1"])
        self.assertEqual(fams, ["Machine Room", "Staircase"])
        self.assertIn("AI_WALL", out["walls"]["F1"])
        self.assertEqual(out["source"], "step3")
        self.assertEqual(out["floors"]["F1"][0]["holes"], [])

    def test_an_older_result_is_read_without_the_six(self):
        old = {"floors": {"F1": [
            {"id": "GEO-1", "cls": "SHAFT", "label": "SHAFT_01", "poly": [[0, 0], [600, 0], [600, 600], [0, 600]],
             "rule": "shaft_small", "confidence": None, "features": {"centroid": [300, 300]}, "evidence": []},
            {"id": "GEO-2", "cls": "STAIRCASE", "label": "STAIRCASE_01",
             "poly": [[0, 1000], [4000, 1000], [4000, 6000], [0, 6000]], "rule": "stair_treads",
             "confidence": None, "features": {"centroid": [2000, 3500]}, "evidence": []}]},
            "window_openings": {"F1": [[[0, 0], [1, 0], [1, 1]]]}}
        out = ge.build_final(old, None, [])
        self.assertEqual([r["family"] for r in out["floors"]["F1"]], ["Staircase"])
        self.assertEqual(out["window_openings"], {})
        res = ge.apply_reviews(old, {}, [])
        self.assertEqual([r["cls"] for r in res["floors"]["F1"]], ["STAIRCASE"])

    def test_layers_of_the_classes(self):
        self.assertEqual(ge.LAYER_OF["STAIRCASE"], "AI_STAIR")
        for c in ge.STEP3_CLASSES:
            self.assertNotIn(c, ge.LAYER_OF)
        self.assertFalse(set(ge.CLASSES) & set(ge.STEP3_CLASSES))

    def test_the_engine_run_is_gone(self):
        for name in ("run", "run_to_files", "_add_walls", "_classify_floor", "door_rooms", "door_solid",
                     "load_profile", "confidence"):
            self.assertFalse(hasattr(ge, name), name)
        # what Step 3 still shares with the tab
        for name in ("_column_shapes", "_wall_faces", "_in_wall_line", "_grid_fit", "_dedupe_shapes",
                     "write_dxf", "build_final", "apply_reviews", "step3_free"):
            self.assertTrue(hasattr(ge, name), name)


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.geo = result()

    def test_categories(self):
        rows = self.geo["floors"]["F1"]
        stair = next(r for r in rows if r["cls"] == "STAIRCASE")
        mr = next(r for r in rows if r["cls"] == "MACHINE_ROOM")
        text = {"F1": [
            {"cls": "STAIR", "name": "ST-01", "pg": Polygon(stair["poly"])},              # agreement
            {"cls": "LANDSCAPE", "name": "PODIUM", "pg": Polygon(mr["poly"])},           # conflict
            {"cls": "ROOM", "name": "OFFICE", "pg": box(45000, 30000, 52000, 36000)},    # text only
            {"cls": "LIFT", "name": "P.LIFT 1", "pg": box(5000, 5000, 7600, 7600)},      # Step 3's: text only
        ]}
        cmp = gc.compare(self.geo, text)
        by = {}
        for r in cmp["rows"]:
            by.setdefault(r["category"], []).append(r)
        self.assertEqual([r["text"]["name"] for r in by["AGREEMENT"]], ["ST-01"])
        self.assertEqual(by["AGREEMENT"][0]["spatial"], "Strong")
        self.assertEqual([r["text"]["name"] for r in by["CONFLICT"]], ["PODIUM"])
        self.assertEqual(sorted(r["text"]["name"] for r in by["TEXT_ONLY"]), ["OFFICE", "P.LIFT 1"])
        s = cmp["summary"]
        self.assertEqual(s["text_detections"], 4)
        self.assertEqual(s["categories"]["AGREEMENT"], 1)

    def test_same_name_far_apart_is_not_a_match(self):
        rows = self.geo["floors"]["F1"]
        stair = next(r for r in rows if r["cls"] == "STAIRCASE")
        far = Polygon([(x + 20000, y) for x, y in stair["poly"]])
        cmp = gc.compare(self.geo, {"F1": [{"cls": "STAIR", "name": "ST-01", "pg": far}]})
        t = next(r for r in cmp["rows"] if r["text"])
        self.assertEqual(t["category"], "TEXT_ONLY")

    def test_text_unnamed(self):
        rows = self.geo["floors"]["F1"]
        stair = next(r for r in rows if r["cls"] == "STAIRCASE")
        cmp = gc.compare(self.geo, {"F1": [{"cls": "UNKNOWN", "name": "", "pg": Polygon(stair["poly"])}]})
        t = next(r for r in cmp["rows"] if r["text"])
        self.assertEqual(t["category"], "TEXT_UNNAMED")
        self.assertEqual(t["geo"]["cls"], "STAIRCASE")

    def test_review_sets_category_and_name(self):
        rows = self.geo["floors"]["F1"]
        mr = next(r for r in rows if r["cls"] == "MACHINE_ROOM")
        rev = {mr["id"]: {"category": "MEP / Services", "name": "AHU",
                          "centroid": mr["features"]["centroid"]}}
        res = ge.apply_reviews(self.geo, rev, dr.list_families())
        r = next(x for x in res["floors"]["F1"] if x["id"] == mr["id"])
        self.assertEqual((r["label"], r["category"], r["category_layer"]), ("AHU", "MEP / Services", "MEP"))
        self.assertEqual(r["machine"]["cls"], "MACHINE_ROOM")    # Step 3's answer is kept
        self.assertTrue(r["reviewed"])
        self.assertEqual(self.geo["floors"]["F1"][rows.index(mr)]["label"], mr["label"])  # original untouched
        # the comparison follows the user's category
        cmp = gc.compare(res, {"F1": [{"cls": "MEP", "name": "AHU", "pg": Polygon(mr["poly"])}]})
        t = next(x for x in cmp["rows"] if x["text"])
        self.assertEqual((t["category"], t["level"]), ("AGREEMENT", "exact"))
        # a review whose finding moved (analysed again, changed) is ignored
        rev[mr["id"]]["centroid"] = [0, 0]
        res2 = ge.apply_reviews(self.geo, rev, dr.list_families())
        self.assertFalse(any(x.get("reviewed") for x in res2["floors"]["F1"]))
        # one of Step 3's six is never a geometry finding's category
        rev = {mr["id"]: {"category": "Shaft", "name": "", "centroid": mr["features"]["centroid"]}}
        res3 = ge.apply_reviews(self.geo, rev, dr.list_families())
        r3 = next(x for x in res3["floors"]["F1"] if x["id"] == mr["id"])
        self.assertEqual(r3["cls"], "MACHINE_ROOM")
        self.assertFalse(r3.get("category"))

    def test_files(self):
        tmp = tempfile.mkdtemp()
        rows = self.geo["floors"]["F1"]
        stair = next(r for r in rows if r["cls"] == "STAIRCASE")
        cmp = gc.compare(self.geo, {"F1": [{"cls": "STAIR", "name": "S", "pg": Polygon(stair["poly"])}]})
        x = os.path.join(tmp, "c.xlsx")
        gc.write_xlsx(cmp, x)
        self.assertTrue(os.path.getsize(x) > 1000)
        # the geometry drawing from the result, then the comparison drawn over it
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        doc.modelspace().add_lwpolyline(FLOOR, close=True, dxfattribs={"layer": "A-FLOOR"})
        geo_dxf = os.path.join(tmp, "geo.dxf")
        ge.write_dxf(doc, dr.MM_UNITS, ge.build_final(self.geo, None, dr.list_families()), geo_dxf)
        d2 = ezdxf.readfile(geo_dxf)
        self.assertIn(ge.LAYER_OF["STAIRCASE"], d2.layers)
        self.assertIn("AI_WALL", d2.layers)
        d = os.path.join(tmp, "c.dxf")
        gc.write_dxf(geo_dxf, cmp, d)
        doc = ezdxf.readfile(d)
        self.assertIn("AI_CMP_TEXT", doc.layers)
        self.assertIn("AI_CMP_AGREEMENT", doc.layers)
        self.assertIn(ge.LAYER_OF["STAIRCASE"], doc.layers)


class AreaStatementViewTests(unittest.TestCase):
    """The statement shown on the page follows the workbook's formulas."""

    def test_formulas(self):
        from Drawing_Analyzer.area_statement import statement_view
        data = {"floors": [{"name": "Typical 2nd & 4th Floor", "area_sqm": 1000.0,
                            "levels": [{"key": 2, "label": "2nd Floor", "refuge": False},
                                       {"key": 4, "label": "4th Floor", "refuge": False}]}],
                "rooms": [{"name": "LIFT WELL", "family": "Lift", "area_sqm": 20.0},
                          {"name": "STAIRCASE", "family": "Staircase", "area_sqm": 30.0},
                          {"name": "PHE SHAFT", "family": "Shaft", "area_sqm": 5.0}]}
        v = statement_view(data, "T1.dxf")
        t = v["towers"][0]
        self.assertEqual(len(t["rows"]), 2)                     # 2nd and 4th
        r = t["rows"][0]
        self.assertAlmostEqual(r["gross"], 1000.0)
        self.assertAlmostEqual(r["total_shafts"], 5.0)
        self.assertAlmostEqual(r["net_bua"], 995.0)
        self.assertAlmostEqual(r["plinth"], 1000.0 - 20 - 30 - 5)
        self.assertAlmostEqual(r["sale_sqm"], round(945.0 / v["sale_factor"], 2))
        self.assertAlmostEqual(t["total"]["gross"], 2000.0)
        self.assertAlmostEqual(v["summary_total"]["gbua_sqm"], 2000.0)
        keys = [c["key"] for c in v["columns"]]
        self.assertIn("lift_shaft", keys)
        self.assertIn("staircase", keys)


if __name__ == "__main__":
    unittest.main()
