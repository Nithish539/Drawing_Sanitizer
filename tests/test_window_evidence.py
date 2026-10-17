# -*- coding: utf-8 -*-
"""Windows, shaft strips and cut-outs told apart: an X-marked opening in a
wall is sorted by what is drawn inside it (glazing lines) and the layers it
is drawn on (WindowFinder.classify, OpeningEvidence).

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402
from shapely.geometry import box  # noqa: E402

from Drawing_Analyzer import area_statement as AS  # noqa: E402
from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import layernames as LN  # noqa: E402

# a 300 mm wall along y 0..300, an opening 2000..3500 in it
FACES = [(0, 0, 6000, 0), (0, 300, 6000, 300)]
OPEN = box(2000, 0, 3500, 300)


def s(x1, y1, x2, y2, layer):
    return (x1, y1, x2, y2, layer)


def opening(layer, glazing=0, glazing_layer="A3-LOUVER", overlay=None):
    """The opening's X and outline on `layer`, glazing lines inside, and an
    area-plan overlay outline on its own layer."""
    segs = [s(2000, 0, 3500, 300, layer), s(2000, 300, 3500, 0, layer),
            s(2000, 0, 3500, 0, layer), s(3500, 0, 3500, 300, layer),
            s(3500, 300, 2000, 300, layer), s(2000, 300, 2000, 0, layer)]
    for k in range(glazing):
        y = 100 + 50 * k
        segs.append(s(2000, y, 3500, y, glazing_layer))
    if overlay:
        segs += [s(1995, -5, 3505, -5, overlay), s(3505, -5, 3505, 305, overlay),
                 s(3505, 305, 1995, 305, overlay), s(1995, 305, 1995, -5, overlay)]
    return segs


def classify(segs):
    finder = dr.WindowFinder(FACES, None, evidence=dr.OpeningEvidence(segs))
    return finder.classify(OPEN)


class ClassifyTests(unittest.TestCase):

    def test_wall_tests_take_it_for_an_opening(self):
        self.assertIsNotNone(dr.WindowFinder(FACES).reason(OPEN))

    def test_three_glazing_lines_make_a_window(self):
        kind, why, ev, clash = classify(opening("A3-LOUVER", glazing=3))
        self.assertEqual(kind, "window")
        self.assertEqual(ev["glazing"], 3)
        self.assertIsNone(clash)

    def test_no_glazing_on_a_shaft_layer_is_a_shaft(self):
        kind, _why, ev, _c = classify(opening("A3-CUTOUTS", overlay="3.12 FP SHAFT"))
        self.assertEqual(kind, "shaft")
        self.assertEqual(ev["name"], "FP SHAFT")

    def test_no_glazing_on_a_cutout_layer_is_a_cutout(self):
        kind, _why, ev, _c = classify(opening("A3-block work cutout"))
        self.assertEqual(kind, "cutout")
        self.assertIn("CUTOUT", ev["cutout"])

    def test_cutout_layer_with_glazing_is_a_window_and_a_conflict(self):
        kind, _why, ev, clash = classify(opening("A3-CUTOUT", glazing=3, glazing_layer="A3-CUTOUT"))
        self.assertEqual(kind, "window")
        self.assertEqual(clash, "CUTOUT")

    def test_no_lines_no_words_is_decided_as_before(self):
        kind, why, _ev, _c = classify(opening("A-OTHER"))
        self.assertEqual(kind, "window")
        self.assertEqual(why, dr.WindowFinder(FACES).reason(OPEN))
        # and an X the wall tests do not take stays out of it, whatever its layer
        far = dr.WindowFinder(FACES, None, evidence=dr.OpeningEvidence(opening("A3-CUTOUT")))
        self.assertEqual(far.classify(box(2000, 2000, 3500, 2300))[0], None)

    def test_xref_prefix_is_stripped(self):
        self.assertEqual(dr.layer_text("XREF-T1_STR$0$T1_STR-A3-CUTOUTS"), "T1 STR A3 CUTOUTS")
        self.assertEqual(dr.layer_text("PLAN|3.11 TENANT RISER"), "3 11 TENANT RISER")
        kind, _w, ev, _c = classify(opening("XREF-T1$0$3.11 TENANT RISER"))
        self.assertEqual(kind, "shaft")
        self.assertEqual(ev["name"], "TENANT RISER")
        # a word inside an XREF name is not read: only the layer's own name
        kind, _w, _ev, _c = classify(opening("SHAFTS-XREF$0$A-OTHER"))
        self.assertEqual(kind, "window")

    def test_words_and_names(self):
        self.assertEqual(dr.opening_name("3.12 FP SHAFT"), "FP SHAFT")
        self.assertEqual(dr.opening_name("3.11 TENANT RISER"), "TENANT RISER")
        self.assertTrue(LN.is_reserved(LN.CUTOUT))
        self.assertIsNone(LN.room_key(LN.CUTOUT))


class CutoutEndToEndTests(unittest.TestCase):
    """A block-work cut-out in a room's wall: drawn on AI_CUTOUT with its X,
    never a room, listed as a cut-out in the areas and in the statement's
    other shafts and cut-outs."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        plate = [(0, 0), (40000, 0), (40000, 30000), (0, 30000)]
        msp.add_lwpolyline(plate, close=True, dxfattribs={"layer": "A-FLOOR"})

        def rect(x0, y0, x1, y1, layer):
            msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": layer})
        # an 8 x 6 m room, its bottom wall open 13000..14500 for a cut-out
        rect(9800, 9800, 13000, 10000, "A-WALL")
        rect(14500, 9800, 18200, 10000, "A-WALL")
        rect(9800, 16000, 18200, 16200, "A-WALL")
        rect(9800, 10000, 10000, 16000, "A-WALL")
        rect(18000, 10000, 18200, 16000, "A-WALL")
        cut = "A3-block work cutout"
        rect(13000, 9800, 14500, 10000, cut)
        msp.add_line((13000, 9800), (14500, 10000), dxfattribs={"layer": cut})
        msp.add_line((13000, 10000), (14500, 9800), dxfattribs={"layer": cut})
        msp.add_mtext("OFFICE", dxfattribs={"insert": (14000, 13000), "char_height": 200, "layer": "A-TEXT"})
        src = os.path.join(tmp, "in.dxf")
        doc.saveas(src)
        floors = [{"name": "F1", "polygon": plate}]
        rows = dr.extract_texts(src, floors)
        cls.out = os.path.join(tmp, "out.dxf")
        cls.stats = dr.annotate_dxf(src, cls.out, floors, rows)
        cls.areas = dr.compute_room_areas(cls.out, "AI_FLOOR")

    def test_drawn_on_ai_cutout_with_its_x(self):
        self.assertEqual(self.stats.get("cutouts_drawn"), 1)
        self.assertEqual(self.stats.get("windows"), 0)
        msp = ezdxf.readfile(self.out).modelspace()
        self.assertEqual(len(msp.query('LWPOLYLINE[layer=="AI_CUTOUT"]')), 1)
        self.assertEqual(len(msp.query('LINE[layer=="AI_CUTOUT"]')), 2)
        self.assertEqual(len(msp.query('*[layer=="AI_WINDOW"]')), 0)

    def test_measured_as_a_cutout_not_a_room(self):
        cuts = self.areas["cutouts"]
        self.assertEqual(len(cuts), 1)
        self.assertAlmostEqual(cuts[0]["area_sqm"], 0.30, delta=0.01)
        self.assertFalse(any(abs(r["area_sqm"] - 0.30) < 0.01 for r in self.areas["rooms"]))
        office = [r for r in self.areas["rooms"] if "OFFICE" in r["name"]]
        self.assertEqual(len(office), 1)
        self.assertAlmostEqual(office[0]["area_sqm"], 48.0, delta=0.5)   # the cut-out is wall to it

    def test_area_statement_puts_it_among_the_cutouts(self):
        st = AS.build_statement(self.areas, "T")
        v = st["towers"][0]["rows"][0]["values"]
        self.assertAlmostEqual(v["wall_cutout"], 0.30, delta=0.01)   # listed, not deducted
        self.assertIn("Cutout", [r["family"] for r in st["rooms"]])
        dbl = dict(self.areas, cutouts=[{"floor": "F1", "name": "DOUBLE HEIGHT CUTOUT", "area_sqm": 2.0}])
        v2 = AS.build_statement(dbl, "T")["towers"][0]["rows"][0]["values"]
        self.assertAlmostEqual(v2["double_cutout"], 2.0, delta=0.01)


if __name__ == "__main__":
    unittest.main()
