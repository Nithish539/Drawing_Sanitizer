# -*- coding: utf-8 -*-
"""Geometry engine (geoengine.py) and its comparison with the text engine.

Run:  python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402
from shapely.geometry import Polygon, box  # noqa: E402

from Drawing_Analyzer import geoengine as ge  # noqa: E402
from Drawing_Analyzer import geocompare as gc  # noqa: E402

FLOOR = [(0, 0), (60000, 0), (60000, 40000), (0, 40000)]


def x_box(msp, x0, y0, w, h, layer="A-SHAFT"):
    """A closed box with both diagonals: an X mark."""
    msp.add_lwpolyline([(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)], close=True,
                       dxfattribs={"layer": layer})
    msp.add_line((x0, y0), (x0 + w, y0 + h), dxfattribs={"layer": layer})
    msp.add_line((x0 + w, y0), (x0, y0 + h), dxfattribs={"layer": layer})


def build(path, with_texts=False):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_lwpolyline(FLOOR, close=True, dxfattribs={"layer": "A-FLOOR"})
    # two banks of three lift-size X marks, an outline between them
    for i in range(3):
        x_box(msp, 5000 + i * 2900, 5000, 2600, 2600)
        x_box(msp, 5000 + i * 2900, 10000, 2600, 2600)
    msp.add_lwpolyline([(5000, 7700), (13400, 7700), (13400, 9900), (5000, 9900)], close=True,
                       dxfattribs={"layer": "A-AREA-LOBBY"})
    # a single small shaft and its 300 mm cut-out strip right below it
    x_box(msp, 20000, 5000, 1200, 1200)
    x_box(msp, 20000, 4700, 1200, 300)
    # a large void
    x_box(msp, 30000, 20000, 9000, 9000, layer="A-CUTOUTS")
    # a staircase: an open well (X) with treads on both sides
    x_box(msp, 10000, 20000, 900, 3000, layer="A-STAIR")
    for k in range(11):
        y = 20100 + k * 280
        msp.add_line((8700, y), (9950, y), dxfattribs={"layer": "A-STAIR"})
        msp.add_line((10950, y), (12200, y), dxfattribs={"layer": "A-STAIR"})
    msp.add_lwpolyline([(8500, 19500), (12400, 19500), (12400, 24600), (8500, 24600)], close=True,
                       dxfattribs={"layer": "A-WALL"})
    if with_texts:
        # misleading room names - the geometry engine must not care
        msp.add_text("TOILET", dxfattribs={"insert": (5800, 6200), "height": 200, "layer": "A-TEXT"})
        msp.add_text("LIFT", dxfattribs={"insert": (33000, 24000), "height": 200, "layer": "A-TEXT"})
    doc.saveas(path)
    return doc


def floors():
    return [{"name": "F1", "polygon": [list(p) for p in FLOOR]}]


class GeometryEngineTests(unittest.TestCase):
    """A shaft, lift, window, cut-out, column or void is Step 3's (the X
    sequence, 2026-10-16): the engine reports none, and reads the X marks
    only as evidence - lift wells along both sides of a space, a long X
    between two flights."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.src = os.path.join(cls.tmp, "in.dxf")
        build(cls.src)
        cls.out_dxf = os.path.join(cls.tmp, "geo.dxf")
        # The classifier itself, every kind it can find: the profile's
        # "identify" list (what is reported) is tested on its own below.
        cls.full_profile = ge.load_profile()
        cls.full_profile.pop("identify", None)
        cls.res = ge.run(cls.src, floors(), out_dxf=cls.out_dxf, profile=cls.full_profile)
        cls.rows = cls.res["floors"]["F1"]

    def of(self, cls):
        return [r for r in self.rows if r["cls"] == cls]

    def test_none_of_step3s_six(self):
        classes = {r["cls"] for r in self.rows}
        self.assertFalse(classes & set(ge.STEP3_CLASSES), classes)
        self.assertFalse(classes & set(ge.EVIDENCE_CLASSES), classes)
        self.assertEqual(self.res["window_openings"], {"F1": []})

    def test_lift_wells_are_evidence_for_the_space_between(self):
        # six lift-size X marks, none reported: the space between the two
        # banks is reported, from them (no door onto it: their machine room)
        mr = self.of("MACHINE_ROOM")
        self.assertEqual(len(mr), 1)
        self.assertAlmostEqual(mr[0]["area_sqm"], 18.48, places=1)
        self.assertTrue(any("lift wells" in e["text"] for e in mr[0]["evidence"]))

    def test_no_finding_where_an_x_stands(self):
        for x0, y0, x1, y1 in ((5000, 5000, 7600, 7600), (20000, 5000, 21200, 6200), (30000, 20000, 39000, 29000)):
            B = box(x0, y0, x1, y1)
            self.assertFalse(any(Polygon(r["poly"]).intersection(B).area > 0.5 * B.area for r in self.rows))

    def test_the_staircase_round_its_well(self):
        stairs = self.of("STAIRCASE")
        self.assertEqual(len(stairs), 1)
        # the staircase is the enclosure around the well, not the well's box
        self.assertGreater(stairs[0]["area_sqm"], 3 * 2.7)
        self.assertTrue(any("well" in e["text"] for e in stairs[0]["evidence"]))

    def test_every_finding_explains_itself(self):
        for r in self.rows:
            self.assertTrue(r["evidence"], r["label"])
            self.assertIn("width_mm", r["features"])
            self.assertTrue(r["label"].split("_")[0])
            self.assertGreaterEqual(len(r["poly"]), 3)
            if r["confidence"] is not None:
                self.assertTrue(0.0 <= r["confidence"] <= 1.0)

    def test_texts_do_not_change_the_result(self):
        other = os.path.join(self.tmp, "with_texts.dxf")
        build(other, with_texts=True)
        res2 = ge.run(other, floors(), profile=self.full_profile)
        a = sorted((r["cls"], r["label"], r["area_sqm"]) for r in self.rows)
        b = sorted((r["cls"], r["label"], r["area_sqm"]) for r in res2["floors"]["F1"])
        self.assertEqual(a, b)

    def test_annotated_dxf_layers(self):
        doc = ezdxf.readfile(self.out_dxf)
        msp = doc.modelspace()
        for c in ("MACHINE_ROOM", "STAIRCASE"):
            layer = ge.LAYER_OF[c]
            self.assertIn(layer, doc.layers)
            self.assertTrue(len(msp.query('LWPOLYLINE[layer=="%s"]' % layer)))
            self.assertTrue(len(msp.query('MTEXT[layer=="%s"]' % layer)))
        for layer in ("AI_SHAFT", "AI_LIFT", "AI_VOID_AREA", "AI_COLUMN", "AI_WINDOW", "AI_CUTOUT"):
            self.assertFalse(len(msp.query('LWPOLYLINE[layer=="%s"]' % layer)), layer)
        # the original drawing is still there, untouched
        src = ezdxf.readfile(self.src).modelspace()
        for layer in ("A-SHAFT", "A-CUTOUTS", "A-STAIR", "A-FLOOR", "A-WALL"):
            self.assertEqual(len(msp.query('*[layer=="%s"]' % layer)),
                             len(src.query('*[layer=="%s"]' % layer)))

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

    def test_confidence_is_a_lower_bound(self):
        prof = {"rules": {"r": {"agree": 6, "n": 6, "drawings": 2}}}
        c, note = ge.confidence(prof, "r")
        self.assertLess(c, 0.7)                       # 6 of 6 is not certainty
        self.assertIn("6 of 6", note)
        # unreferenced findings (no named text there) count by the rate seen
        # when a sample of them was checked by eye
        prof = {"rules": {"r": {"agree": 300, "conflict": 5, "unreferenced": 35, "n": 340,
                                "drawings": 9, "verified": [9, 9]}}}
        c2, _ = ge.confidence(prof, "r")
        self.assertGreater(c2, 0.95)
        # without a check, unreferenced findings are left out, not counted right
        prof = {"rules": {"r": {"agree": 30, "conflict": 10, "unreferenced": 500, "n": 540}}}
        c3, note3 = ge.confidence(prof, "r")
        self.assertLess(c3, 0.75)
        self.assertIn("30 of 40", note3)
        self.assertEqual(ge.confidence({"rules": {}}, "x"), (None, "not measured yet"))


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        t = GeometryEngineTests
        if not hasattr(t, "res"):
            t.setUpClass()
        self.geo = t.res

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
        from Drawing_Analyzer import dxf_reader as dr
        rows = self.geo["floors"]["F1"]
        mr = next(r for r in rows if r["cls"] == "MACHINE_ROOM")
        rev = {mr["id"]: {"category": "MEP / Services", "name": "AHU",
                          "centroid": mr["features"]["centroid"]}}
        res = ge.apply_reviews(self.geo, rev, dr.list_families())
        r = next(x for x in res["floors"]["F1"] if x["id"] == mr["id"])
        self.assertEqual((r["label"], r["category"], r["category_layer"]), ("AHU", "MEP / Services", "MEP"))
        self.assertEqual(r["machine"]["cls"], "MACHINE_ROOM")    # the engine's answer is kept
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
        d = os.path.join(tmp, "c.dxf")
        gc.write_dxf(GeometryEngineTests.out_dxf, cmp, d)
        doc = ezdxf.readfile(d)
        self.assertIn("AI_CMP_TEXT", doc.layers)
        self.assertIn("AI_CMP_AGREEMENT", doc.layers)
        self.assertIn(ge.LAYER_OF["STAIRCASE"], doc.layers)


class IdentifyTests(unittest.TestCase):
    """geo_profiles.json "identify": staircases, lift lobbies, machine rooms
    and service lobbies - never one of Step 3's six."""

    def test_only_what_is_listed(self):
        t = GeometryEngineTests
        if not hasattr(t, "src"):
            t.setUpClass()
        res = ge.run(t.src, floors())
        classes = {r["cls"] for rows in res["floors"].values() for r in rows}
        self.assertTrue(classes, "nothing reported")
        self.assertLessEqual(classes, {"STAIRCASE", "LIFT_LOBBY", "MACHINE_ROOM", "SERVICE_LOBBY"})

    def test_identify_lists_known_rules(self):
        rules = ge.identified_rules(ge.load_profile())
        self.assertEqual(set(rules.values()), {"STAIRCASE", "LIFT_LOBBY", "MACHINE_ROOM", "SERVICE_LOBBY"})
        self.assertNotIn("shaft_small", rules)
        self.assertNotIn("column_grid", rules)
        self.assertIn("stair_treads", rules)


if __name__ == "__main__":
    unittest.main()


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
