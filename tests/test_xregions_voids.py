# -*- coding: utf-8 -*-
"""X regions, voids, cut-out shafts, label sizes, geometry types, validation.

The cases come from the P14 1st floor sheet.

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer.area_statement import build_statement  # noqa: E402
from Drawing_Analyzer.labels import declared_size, name_part  # noqa: E402
from Drawing_Analyzer.validate import validate_boundaries  # noqa: E402
from Drawing_Analyzer.xregions import find_x_regions  # noqa: E402

CUT = "A3-CUTOUTS"


def seg(x1, y1, x2, y2, layer=CUT):
    return (x1, y1, x2, y2, layer, ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)


def boxes(regions):
    return sorted(tuple(round(v) for v in r["bbox"]) for r in regions)


class LabelTests(unittest.TestCase):

    def test_size_line_is_not_part_of_the_name(self):
        self.assertFalse(dr.is_note_text("LOBBY PRESSURE SHAFT\n1500 X 950 MM"))
        self.assertFalse(dr.is_note_text(
            "TR-SH-02 1200x1200\n(1000 X 300 & 300 X 900 MM SLAB CUT-OUT)"))
        self.assertEqual(name_part("TR-SH-02 1200x1200\n(1000 X 300 MM SLAB CUT-OUT)"),
                         "TR-SH-02")

    def test_name_over_short_lines_is_a_name(self):
        self.assertFalse(dr.is_note_text("TOWER-01\nZONE-01\nLIFT LOBBY-01"))
        self.assertTrue(dr.is_note_text("FOR DETAILS OF STAIRCASE-01 REFER DRG. NO.T1-A-302"))

    def test_declared_size(self):
        self.assertEqual(declared_size("MALE TOILET\n9500x4350"), (9500.0, 4350.0))
        self.assertEqual(declared_size("LOBBY PRESSURE SHAFT 1500 X 950 MM"), (1500.0, 950.0))
        # the bracket describes the cut-out, not the shaft
        self.assertEqual(declared_size("TR-SH-02 1200x1200 (1000 X 300 MM)"), (1200.0, 1200.0))
        self.assertIsNone(declared_size("HEAVILY LANDSCAPE PODIUM"))

    def test_shaft_codes_are_shafts(self):
        for t in ("CO-SH-04", "FA-PA-SH", "ST PR-SH-01", "ST. PRESS. SH", "EXHAUST SH."):
            self.assertEqual(dr.family_for_label(t, dr._FAMILY_RULES)["name"], "Shaft", t)


class XRegionTests(unittest.TestCase):

    def test_complete_x(self):
        r = find_x_regions([seg(0, 0, 900, 600), seg(0, 600, 900, 0)])
        self.assertEqual(boxes(r), [(0, 0, 900, 600)])

    def test_trimmed_diagonal(self):
        # CUT-OUT 2500 x 700: one diagonal stops at a column plate
        r = find_x_regions([seg(296418, 1061215, 298918, 1060515),
                            seg(298468, 1061215, 296418, 1060515)])
        self.assertEqual(boxes(r), [(296418, 1060515, 298918, 1061215)])

    def test_broken_diagonal(self):
        r = find_x_regions([seg(0, 0, 450, 500), seg(460, 511, 900, 1000),
                            seg(0, 1000, 900, 0)])
        self.assertEqual(boxes(r), [(0, 0, 900, 1000)])

    def test_l_shaped_cutout(self):
        # TR-SH-03: two strips, each with one full and one part diagonal
        r = find_x_regions([seg(318218, 1078965, 317918, 1077865),
                            seg(318218, 1078965, 317318, 1078665),
                            seg(317318, 1078965, 317918, 1078665),
                            seg(317918, 1078665, 318218, 1077865)])
        self.assertEqual(len(r), 1)
        self.assertEqual(tuple(round(v) for v in r[0]["bbox"]), (317318, 1077865, 318218, 1078965))
        self.assertAlmostEqual(r[0]["area_sqm"], 0.51, places=2)
        self.assertEqual(len(r[0]["poly"]), 6)

    def test_long_void_x(self):
        r = find_x_regions([seg(279968, 1016965, 367718, 1027515),
                            seg(279818, 1027365, 366368, 1016965)])
        self.assertEqual(len(r), 1)
        self.assertGreater(r[0]["area_sqm"], 900)

    def test_touching_shafts_stay_apart(self):
        r = find_x_regions([seg(0, 0, 900, 1050), seg(0, 1050, 900, 0),
                            seg(900, 0, 1300, 700), seg(1300, 0, 900, 700)])
        self.assertEqual(len(r), 2)

    def test_column_crosses_are_not_x(self):
        r = find_x_regions([seg(0, 0, 600, 700, "SCPL_COL. CROSS"),
                            seg(0, 700, 600, 0, "SCPL_COL. CROSS")])
        self.assertEqual(r, [])


def _rect(msp, x0, y0, x1, y1, layer="A-WALL"):
    msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True,
                       dxfattribs={"layer": layer})


def _walled_room(msp, x0, y0, x1, y1, t=200):
    """A room x0..x1, y0..y1 with 200 mm walls outside it."""
    _rect(msp, x0 - t, y0 - t, x1 + t, y0)
    _rect(msp, x0 - t, y1, x1 + t, y1 + t)
    _rect(msp, x0 - t, y0, x0, y1)
    _rect(msp, x1, y0, x1 + t, y1)


def _text(msp, text, x, y):
    msp.add_mtext(text, dxfattribs={"insert": (x, y), "char_height": 150,
                                    "layer": "A-TEXT"})


class EndToEndTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        plate = [(0, 0), (60000, 0), (60000, 40000), (0, 40000)]
        msp.add_lwpolyline(plate, close=True, dxfattribs={"layer": "A-FLOOR"})
        # 1. a void: one X over 12 x 6 m, with a note inside
        msp.add_line((5000, 30000), (17000, 36000), dxfattribs={"layer": CUT})
        msp.add_line((5000, 36000), (17000, 30000), dxfattribs={"layer": CUT})
        _text(msp, "HEAVILY LANDSCAPE PODIUM", 11000, 33000)
        # 2. a 900 x 1000 shaft whose X marks only its 300 mm cut-out strip,
        #    labelled from 800 mm outside
        _walled_room(msp, 20000, 10000, 20900, 11000)
        msp.add_line((20600, 10000), (20900, 11000), dxfattribs={"layer": CUT})
        msp.add_line((20600, 11000), (20900, 10000), dxfattribs={"layer": CUT})
        _text(msp, "FA-PA-SH\n900X1000", 18900, 10500)
        # 3. a stair: 3 x 6 m, treads 280 mm apart, named by a stair note
        _walled_room(msp, 30000, 10000, 33000, 16000)
        for k in range(12):
            y = 11000 + 280 * k
            msp.add_line((30100, y), (32900, y), dxfattribs={"layer": "A-STAIRS"})
        _text(msp, "W-2600\nT-300\nR-150", 31500, 15600)
        src = os.path.join(tmp, "in.dxf")
        doc.saveas(src)
        floors = [{"name": "F1", "polygon": plate}]
        rows = dr.extract_texts(src, floors)
        cls.out = os.path.join(tmp, "out.dxf")
        # the strip-to-shaft growth is off by default (a take-off deducts
        # the X'd strip only); this drawing checks it when it is on
        cls.stats = dr.annotate_dxf(src, cls.out, floors, rows,
                                    rule_overrides={"x_grows_to_cell": True})
        cls.areas = dr.compute_room_areas(cls.out, "A-FLOOR")

    def rooms(self, family):
        return [r for r in self.areas["rooms"] if r["family"] == family]

    def test_void_named_by_its_text(self):
        # The X marks a 72 sq.m opening; the text inside, HEAVILY LANDSCAPE
        # PODIUM, names it - and it stays a void: over 30 sq.m, nothing
        # before the X sequence's last step claims it, and no text changes
        # what the sequence decided (2026-10-16).
        self.assertEqual(self.rooms("Landscape / External"), [])
        void = self.rooms("Void")
        self.assertEqual(len(void), 1)
        self.assertAlmostEqual(void[0]["area_sqm"], 72.0, delta=0.5)
        self.assertIn("LANDSCAPE", void[0]["name"])
        self.assertEqual(self.stats.get("void_named_by_text"), 1)

    def test_x_strip_is_the_shaft_as_drawn(self):
        # The X marks a 300 x 1000 strip of the walled 900 x 1000 shaft
        # under "FA-PA-SH 900X1000": the strip does not have the stated
        # size, the shaft it stands in does - the label takes the whole
        # shaft (its cut-out inside), one shaft, no separate cut-out.
        shafts = self.rooms("Shaft")
        self.assertEqual(len(shafts), 1, shafts)
        self.assertAlmostEqual(shafts[0]["area_sqm"], 0.90, delta=0.02)
        self.assertIn("FA-PA-SH", shafts[0]["name"])
        self.assertEqual(self.rooms("Cut-out"), [])

    def test_stair_from_its_treads(self):
        stairs = self.rooms("Staircase")
        self.assertEqual(len(stairs), 1)
        self.assertAlmostEqual(stairs[0]["area_sqm"], 18.0, delta=0.5)

    def test_area_statement_void_and_shaft(self):
        st = build_statement(self.areas, "F1 1st floor.dxf")
        v = st["towers"][0]["rows"][0]["values"]
        # the opening is a void, whatever its text says
        self.assertAlmostEqual(v["void"], 72.0, delta=0.5)
        # the whole shaft, counted once
        self.assertAlmostEqual(v["fapa"], 0.90, delta=0.02)
        self.assertEqual(v.get("other_opening", 0.0), 0.0)

    def test_validation_ran(self):
        val = self.stats["validation"]
        self.assertEqual(val["error_count"], 0, val["issues"])
        self.assertEqual(val["voids"], 1)          # a void, named by its text


class ValidationTests(unittest.TestCase):

    def test_flags(self):
        sq = [(10000, 0), (12000, 0), (12000, 2000), (10000, 2000)]
        recs = [
            {"floor": "F", "layer": "ROOM_BOUNDARY", "family": "Habitable Room",
             "label": "OFFICE", "pts": sq, "label_xy": (11000, 1000)},
            {"floor": "F", "layer": "ROOM_BOUNDARY", "family": "Habitable Room",
             "label": "OFFICE", "pts": list(sq), "label_xy": (5000, 5000)},
            {"floor": "F", "layer": "X", "family": "Unknown", "label": "A",
             "pts": [(0, 0), (10, 0)], "label_xy": None},
            {"floor": "F", "layer": "SHAFT_BOUNDARY", "family": "Shaft",
             "label": "SHAFT 900x1000", "pts": [(0, 0), (5000, 0), (5000, 5000), (0, 5000)],
             "label_xy": (100, 100)},
        ]
        v = validate_boundaries(recs, size_ok=dr._size_ok)
        kinds = {i["kind"] for i in v["issues"]}
        self.assertIn("duplicate", kinds)
        self.assertIn("label_out", kinds)
        self.assertIn("open", kinds)
        self.assertIn("size", kinds)


if __name__ == "__main__":
    unittest.main()


class GeometryTabTests(unittest.TestCase):
    """The Geometry tab: shapes found, then drawn as the user mapped them."""

    @classmethod
    def setUpClass(cls):
        from Drawing_Analyzer.geomextract import extract_geometry
        e = EndToEndTests
        if not hasattr(e, "out"):
            e.setUpClass()
        cls.src = e.out.replace("out.dxf", "in.dxf")
        floors = [{"name": "F1", "polygon": [(0, 0), (60000, 0), (60000, 40000), (0, 40000)]}]
        cls.floors = floors
        cls.rows = dr.extract_texts(cls.src, floors)
        cls.geo = extract_geometry(cls.src, floors, cls.rows)
        cls.items = cls.geo["floors"]["F1"]

    def fam(self, f):
        return [i for i in self.items if i["family"] == f]

    def test_groups_found(self):
        self.assertEqual(len(self.fam("Void")), 1)
        self.assertEqual(len(self.fam("Staircase")), 1)
        self.assertTrue(self.fam("Shaft"))
        self.assertEqual(self.fam("Cut-out"), [])
        void = self.fam("Void")[0]
        self.assertIn("A3-CUTOUTS", void["layers"])
        self.assertTrue(any("LANDSCAPE" in t["text"] for t in void["texts"]))

    def test_generate_follows_the_mapping(self):
        decisions = {}
        for it in self.items:
            d = {"id": it["id"], "kind": it["kind"], "bbox": it["bbox"], "poly": it["poly"],
                 "include": True, "family": it["family"], "label": it["label"],
                 "label_sr": it["label_sr"]}
            if it["family"] == "Void":
                d["include"] = False                  # not drawn
            if it["family"] == "Staircase":
                d["label"] = "STAIRCASE-A"            # relabelled
            decisions.setdefault("F1", []).append(d)
        out = self.src.replace("in.dxf", "geo.dxf")
        st = dr.annotate_dxf(self.src, out, self.floors, self.rows, geometry=decisions)
        areas = dr.compute_room_areas(out, "A-FLOOR")
        fams = [r["family"] for r in areas["rooms"]]
        # the void is the X sequence's: unticked in the tab, still drawn
        self.assertIn("Void", fams)
        stairs = [r for r in areas["rooms"] if r["family"] == "Staircase"]
        self.assertEqual(len(stairs), 1)
        self.assertIn("STAIRCASE-A", stairs[0]["name"])

    def test_deleted_shape_is_not_drawn_even_from_its_text(self):
        stair = self.fam("Staircase")[0]
        decisions = {"F1": [{"id": stair["id"], "kind": stair["kind"], "bbox": stair["bbox"],
                             "poly": stair["poly"], "include": False, "deleted": True,
                             "family": "Staircase", "label": stair["label"],
                             "label_sr": stair["label_sr"]}]}
        out = self.src.replace("in.dxf", "geo_del.dxf")
        st = dr.annotate_dxf(self.src, out, self.floors, self.rows, geometry=decisions)
        fams = [r["family"] for r in dr.compute_room_areas(out, "A-FLOOR")["rooms"]]
        self.assertNotIn("Staircase", fams)
        self.assertEqual(st.get("geometry_deleted"), 1)
        # without the delete the staircase is drawn from its text
        out2 = self.src.replace("in.dxf", "geo_keep.dxf")
        dr.annotate_dxf(self.src, out2, self.floors, self.rows)
        fams2 = [r["family"] for r in dr.compute_room_areas(out2, "A-FLOOR")["rooms"]]
        self.assertIn("Staircase", fams2)


class MoveTextTests(unittest.TestCase):
    """Texts dragged in the Geometry tab move in the output drawing, also
    when they live inside blocks."""

    def _find(self, recs, text):
        return [r for r in recs if r.text == text]

    def test_modelspace_text(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        msp.add_text("LOBBY", dxfattribs={"insert": (100, 100)})
        r = dr._collect_texts_in_modelspace(msp)[0]
        st = dr._move_texts(doc, msp, dr.MM_UNITS, [
            {"handles": list(r.handles), "dx": 50, "dy": -20,
             "text": r.text, "x": r.x, "y": r.y}])
        self.assertEqual(st, {"moved": 1, "not_movable": 0})
        r = dr._collect_texts_in_modelspace(msp)[0]
        self.assertAlmostEqual(r.x, 150)
        self.assertAlmostEqual(r.y, 80)

    def test_block_used_once_moves_inside_the_block(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        doc.blocks.new("S").add_text("X1", dxfattribs={"insert": (10, 0)})
        msp.add_blockref("S", (0, 0), dxfattribs={"rotation": 90, "xscale": 2, "yscale": 2})
        r = dr._collect_texts_from_blocks(doc, msp)[0]
        dr._move_texts(doc, msp, dr.MM_UNITS, [
            {"handles": list(r.handles), "dx": 7, "dy": 3, "text": "X1", "x": r.x, "y": r.y}])
        self.assertEqual(len(msp.query("INSERT")), 1)          # still a block
        r = dr._collect_texts_from_blocks(doc, msp)[0]
        self.assertAlmostEqual(r.x, 7.0)
        self.assertAlmostEqual(r.y, 23.0)

    def test_shared_nested_block_explodes_only_that_copy(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        doc.blocks.new("IN").add_text("ROOM A", dxfattribs={"insert": (10, 0)})
        outer = doc.blocks.new("OUT")
        outer.add_blockref("IN", (100, 0))
        outer.add_text("LOBBY", dxfattribs={"insert": (0, 50)})
        msp.add_blockref("OUT", (1000, 1000), dxfattribs={"rotation": 90, "xscale": 2, "yscale": 2})
        msp.add_blockref("OUT", (5000, 1000))
        recs = dr._collect_texts_from_blocks(doc, msp)
        a = [r for r in recs if r.text == "ROOM A" and r.x < 3000][0]
        lob = [r for r in recs if r.text == "LOBBY" and r.x < 3000][0]
        st = dr._move_texts(doc, msp, dr.MM_UNITS, [
            {"handles": list(a.handles), "dx": 7, "dy": 3, "text": a.text, "x": a.x, "y": a.y},
            {"handles": list(lob.handles), "dx": -4, "dy": 0, "text": lob.text, "x": lob.x, "y": lob.y}])
        self.assertEqual(st, {"moved": 2, "not_movable": 0})
        flat = dr._collect_texts_in_modelspace(msp)
        self.assertEqual([(round(r.x), round(r.y)) for r in self._find(flat, "ROOM A")], [(1007, 1223)])
        self.assertEqual([(round(r.x), round(r.y)) for r in self._find(flat, "LOBBY")], [(896, 1000)])
        # the other copy of the block is untouched
        kept = dr._collect_texts_from_blocks(doc, msp)
        self.assertEqual(sorted((r.text, round(r.x), round(r.y)) for r in kept),
                         [("LOBBY", 5000, 1050), ("ROOM A", 5110, 1000)])


class GeometryOnlyTests(unittest.TestCase):
    """Ticked "Draw only my selection": the chosen shapes and nothing else."""

    def test_only_the_selection_is_drawn(self):
        g = GeometryTabTests
        if not hasattr(g, "items"):
            g.setUpClass()
        stair = [i for i in g.items if i["family"] == "Staircase"][0]
        decisions = {"F1": [{"id": stair["id"], "kind": stair["kind"], "bbox": stair["bbox"],
                             "poly": stair["poly"], "include": True, "family": "Staircase",
                             "label": "MY STAIR", "label_sr": None}]}
        out = g.src.replace("in.dxf", "geo_only.dxf")
        dr.annotate_dxf(g.src, out, g.floors, g.rows, geometry=decisions, geometry_only=True)
        rooms = dr.compute_room_areas(out, "A-FLOOR")["rooms"]
        # the selection, and the X marks as the X sequence decided them
        self.assertEqual(sorted(r["family"] for r in rooms), ["Shaft", "Staircase", "Void"])
        self.assertIn("MY STAIR", [r for r in rooms if r["family"] == "Staircase"][0]["name"])
        # the same selection without "only" also draws from the texts
        out2 = g.src.replace("in.dxf", "geo_mix.dxf")
        dr.annotate_dxf(g.src, out2, g.floors, g.rows, geometry=decisions)
        self.assertGreater(len(dr.compute_room_areas(out2, "A-FLOOR")["rooms"]), 1)
