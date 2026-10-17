# -*- coding: utf-8 -*-
"""The wall tracing, checked against the manually corrected drawings of
BHUB 1st Floor, the XA low-zone typical floor and H10 Tower 1.

  * every piece of a hatch is read, not only its largest loop (one BHUB
    structure hatch holds nine concrete walls as nine loops);
  * structure on a structure layer (XREF-T1_STR$1$COLUMN, 400-450 mm) is a
    wall where the structure hatch fills it - a column stays a column;
  * the slab edge outside a wall (one face on the plate's edge, a wall on
    the other) is not wall (XA facade band);
  * curved walls are paired as arcs;
  * a window or cut-out in a wall stays part of the wall; door openings are
    drawn on AI_DOOR; a balcony's parapet is the balcony's, not a wall;
  * the final drawing traces its walls as Step 3 does (glazing pairs, lift
    graphics and bracing are not walls).

Run:  python -m unittest discover -s tests -v
"""

import math
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402
from shapely.geometry import Polygon, box  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import walltrace as wt  # noqa: E402
from Drawing_Analyzer import geoengine as ge  # noqa: E402
from Drawing_Analyzer import layernames as LN  # noqa: E402


def seg(x1, y1, x2, y2, layer="A-WALL"):
    return (x1, y1, x2, y2, layer, math.hypot(x2 - x1, y2 - y1), -1, False, False)


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


PLATE = rect(-1000, -1000, 21000, 21000)


class HatchIslandTests(unittest.TestCase):

    def test_separate_loops_are_separate_pieces(self):
        rings = [rect(0, 0, 4000, 450), rect(5000, 0, 9000, 450), rect(0, 1000, 450, 5000)]
        got = dr._hatch_islands(rings)
        self.assertEqual(len(got), 3)
        self.assertTrue(all(h == [] for _o, h in got))

    def test_nesting_decides_holes_and_islands(self):
        outer, hole, island = rect(0, 0, 10000, 10000), rect(2000, 2000, 8000, 8000), rect(4000, 4000, 6000, 6000)
        got = dr._hatch_islands([island, hole, outer])
        self.assertEqual(len(got), 2)
        areas = sorted(Polygon(o, h).area / 1e6 for o, h in got)
        self.assertAlmostEqual(areas[0], 4.0, places=3)                  # the island in the hole
        self.assertAlmostEqual(areas[1], 100.0 - 36.0, places=3)         # the frame round the hole

    def test_all_hatches_reads_every_piece(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        h = msp.add_hatch(dxfattribs={"layer": "XREF-T1_STR$1$A3-HATCH"})
        h.set_pattern_fill("AR-CONC", scale=1)
        for r in (rect(0, 0, 4000, 450), rect(5000, 0, 9000, 450), rect(0, 1000, 450, 5000)):
            h.paths.add_polyline_path(r, is_closed=True)
        got = dr._collect_all_hatches(doc, msp, dr.MM_UNITS)
        self.assertEqual(len(got), 3)
        self.assertAlmostEqual(sum(Polygon(o).area for o, *_r in got) / 1e6, 1.8 + 1.8 + 1.8, places=2)


class StructureWallTests(unittest.TestCase):
    STR = "XREF-T1_STR$1$COLUMN"

    def hatch(self, pts):
        return (pts, [], "XREF-T1_STR$1$A3-HATCH", "AR-CONC", False)

    def test_layers(self):
        self.assertTrue(wt.is_structure_layer(self.STR))
        self.assertFalse(wt.is_structure_layer("A-WALL"))                # a wall layer already
        self.assertFalse(wt.is_structure_layer("XREF$0$A3-HATCH"))
        self.assertFalse(wt.is_structure_layer("A-DOOR"))

    def test_hatched_structure_wall_is_wall(self):
        s = [seg(0, 0, 4900, 0, self.STR), seg(0, 450, 4900, 450, self.STR)]
        r = wt.trace_floor(s, [self.hatch(rect(0, 0, 4900, 450))], [], PLATE)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["footprint_sqm"], 4.9 * 0.45, places=2)
        self.assertEqual(r["verdicts"].get("structure"), 1)

    def test_unhatched_structure_lines_are_not_wall(self):
        s = [seg(0, 0, 4900, 0, self.STR), seg(0, 450, 4900, 450, self.STR)]
        self.assertIsNone(wt.trace_floor(s, [], [], PLATE))

    def test_a_column_stays_a_column(self):
        s = [seg(0, 0, 600, 0, self.STR), seg(0, 600, 600, 600, self.STR),
             seg(0, 0, 0, 600, self.STR), seg(600, 0, 600, 600, self.STR)]
        r = wt.trace_floor(s, [self.hatch(rect(0, 0, 600, 600))], [], PLATE)
        self.assertTrue(r is None or r["footprint_sqm"] == 0)

    def test_thicker_than_600_is_not_paired(self):
        s = [seg(0, 0, 4900, 0, self.STR), seg(0, 700, 4900, 700, self.STR)]
        self.assertIsNone(wt.trace_floor(s, [self.hatch(rect(0, 0, 4900, 700))], [], PLATE))


class SlabEdgeTests(unittest.TestCase):

    def test_band_outside_a_wall_on_the_plate_edge_is_not_wall(self):
        plate = rect(0, 0, 20000, 15000)
        # the plate's edge line (y 0), the wall's outer face (y 200), its inner face (y 500)
        s = [seg(0, 0, 20000, 0), seg(0, 200, 20000, 200), seg(0, 500, 20000, 500)]
        r = wt.trace_floor(s, [], [], plate)
        self.assertEqual(r["verdicts"].get("edge"), 1)
        self.assertAlmostEqual(r["footprint_sqm"], 20 * 0.3, places=2)   # the 300 mm wall only

    def test_a_wall_on_the_plate_edge_alone_stays(self):
        plate = rect(0, 0, 20000, 15000)
        r = wt.trace_floor([seg(0, 0, 20000, 0), seg(0, 200, 20000, 200)], [], [], plate)
        self.assertAlmostEqual(r["footprint_sqm"], 20 * 0.2, places=2)


class CurvedWallTests(unittest.TestCase):

    def test_concentric_arcs_are_a_curved_wall(self):
        arcs = [(5000, 5000, 1300, 0, 90, "A-WALL"), (5000, 5000, 1500, 0, 90, "A-WALL"),
                (5000, 5000, 1496, 0, 90, "A-WALL"),                    # the same face drawn again
                (5000, 5000, 1800, 0, 90, "A-WALL")]
        r = wt.trace_floor([], [], [], PLATE, arcs=arcs)
        want = math.pi / 4 * ((1.8 ** 2) - (1.3 ** 2))
        self.assertAlmostEqual(r["footprint_sqm"], want, delta=0.03)
        self.assertEqual(r["verdicts"].get("curved"), 2)

    def test_arcs_on_other_layers_are_not_walls(self):
        arcs = [(5000, 5000, 1300, 0, 90, "A-DOOR"), (5000, 5000, 1500, 0, 90, "A-DOOR")]
        self.assertIsNone(wt.trace_floor([], [], [], PLATE, arcs=arcs))

    def test_arcs_read_from_the_drawing(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        msp.add_arc((5000, 5000), 1300, 0, 90, dxfattribs={"layer": "A-WALL"})
        msp.add_lwpolyline([(6500, 5000, 0.41421356), (5000, 6500)], format="xyb", dxfattribs={"layer": "A-WALL"})
        got = dr._collect_wall_arcs(doc, msp, dr.MM_UNITS)
        self.assertEqual(len(got), 2)
        for cx, cy, r, a0, a1, _l in got:
            self.assertAlmostEqual(cx, 5000, delta=1)
            self.assertAlmostEqual(cy, 5000, delta=1)
            self.assertAlmostEqual(a1 - a0, 90, delta=0.5)
        self.assertEqual(sorted(round(g[2]) for g in got), [1300, 1500])


class OutputTests(unittest.TestCase):
    """A room with a cut-out in one wall, a door in another, and a balcony
    with its parapet - through annotate_dxf."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        plate = rect(0, 0, 40000, 30000)
        msp.add_lwpolyline(plate, close=True, dxfattribs={"layer": "A-FLOOR"})

        def r(x0, y0, x1, y1, layer="A-WALL"):
            msp.add_lwpolyline(rect(x0, y0, x1, y1), close=True, dxfattribs={"layer": layer})
        # an 8 x 6 m office: bottom wall open 13000..14500 for a cut-out,
        # right wall open 12000..12900 for a door
        r(9800, 9800, 13000, 10000)
        r(14500, 9800, 18200, 10000)
        r(9800, 16000, 18200, 16200)
        r(9800, 10000, 10000, 16000)
        r(18000, 10000, 18200, 12000)
        r(18000, 12900, 18200, 16000)
        cut = "A3-block work cutout"
        r(13000, 9800, 14500, 10000, cut)
        msp.add_line((13000, 9800), (14500, 10000), dxfattribs={"layer": cut})
        msp.add_line((13000, 10000), (14500, 9800), dxfattribs={"layer": cut})
        msp.add_arc((18000, 12000), 900, 90, 180, dxfattribs={"layer": "A-DOOR"})
        msp.add_line((18000, 12000), (18000, 12900), dxfattribs={"layer": "A-DOOR"})
        msp.add_mtext("OFFICE", dxfattribs={"insert": (14000, 13000), "char_height": 200, "layer": "A-TEXT"})
        # a balcony along the plate's bottom edge behind a 200 mm parapet
        r(20000, 0, 30000, 200)                        # the parapet, on the plate's edge
        r(20000, 200, 20200, 2400)
        r(29800, 200, 30000, 2400)
        r(20000, 2200, 30000, 2400)
        msp.add_mtext("BALCONY", dxfattribs={"insert": (25000, 1200), "char_height": 200, "layer": "A-TEXT"})
        src = os.path.join(tmp, "in.dxf")
        doc.saveas(src)
        floors = [{"name": "F1", "polygon": plate}]
        rows = dr.extract_texts(src, floors)
        cls.out = os.path.join(tmp, "out.dxf")
        cls.stats = dr.annotate_dxf(src, cls.out, floors, rows)
        cls.areas = dr.compute_room_areas(cls.out, "AI_FLOOR")
        cls.msp = ezdxf.readfile(cls.out).modelspace()

        def layer_union(name):
            gs = []
            for e in cls.msp.query('LWPOLYLINE[layer=="%s"]' % name):
                # a door is drawn as a line; the opening it closes is in its XDATA
                pts = (dr.door_quad(e) if LN.is_door_layer(name) else None) or \
                    [(p[0], p[1]) for p in e.get_points("xy")]
                if len(pts) >= 3:
                    gs.append(Polygon(pts).buffer(0))
            u = Polygon()
            for g in sorted(gs, key=lambda g: -g.area):
                u = u.symmetric_difference(g)
            return u
        cls.walls = layer_union(LN.WALL)
        cls.doors = layer_union(LN.DOOR_PRIMARY).union(layer_union(LN.DOOR_SECONDARY))

    def test_the_cut_out_stays_in_the_wall(self):
        self.assertEqual(self.stats.get("cutouts_drawn"), 1)
        cut = box(13000, 9800, 14500, 10000)
        self.assertGreater(self.walls.intersection(cut).area, 0.9 * cut.area)

    def test_areas_do_not_change(self):
        office = [x for x in self.areas["rooms"] if "OFFICE" in x["name"]]
        self.assertAlmostEqual(office[0]["area_sqm"], 48.0, delta=0.5)

    def test_the_door_opening_is_drawn_and_is_no_wall(self):
        self.assertGreaterEqual(self.stats.get("doors_drawn", 0), 1)
        opening = box(18000, 12000, 18200, 12900)
        self.assertGreater(self.doors.intersection(opening).area, 0.8 * opening.area)
        self.assertLess(self.walls.intersection(opening).area, 0.05 * opening.area)
        self.assertTrue(LN.is_reserved(LN.DOOR_PRIMARY))

    def test_the_balcony_runs_over_its_parapet_and_the_parapet_stays_a_wall(self):
        """The balcony is measured to the parapet's outer face, and the
        parapet is drawn as a wall too (balcony_parapet_not_wall false)."""
        bal = [x for x in self.areas["rooms"] if "BALCONY" in x["name"]]
        self.assertEqual(len(bal), 1)
        self.assertAlmostEqual(bal[0]["area_sqm"], 21.2, delta=0.1)     # grown over its parapet
        parapet = box(20200, 0, 29800, 200)
        self.assertGreater(self.walls.intersection(parapet).area, 0.9 * parapet.area)
        self.assertEqual(self.stats.get("walls_balcony_parapet_vetoed", 0), 0)


def walls_like_step3(segs, fallback_strips, plate):
    """The walls as Step 3 builds them (walltrace on the segments, the
    fallback strips where the floor has no wall-layer lines, then
    wall_polygons) - what the geometry engine's _add_walls did for the final
    drawing until 2026-10-17; the final drawing takes Step 3's walls now."""
    from Drawing_Analyzer import walltrace as wt
    traced = wt.trace_floor(segs, [], [], plate, arcs=[])
    strips = (traced["strips"] if traced is not None else fallback_strips) or []
    _pieces, rep = dr.wall_polygons(strips, plate, [], [], [], [], [])
    return rep


class FinalDrawingTracesLikeStep3Tests(unittest.TestCase):

    def test_glazing_pairs_are_not_walls_in_the_final_drawing(self):
        plate = rect(0, 0, 20000, 15000)
        segs = [seg(1000, 5000, 9000, 5000), seg(1000, 5200, 9000, 5200),               # a 200 mm wall
                seg(1000, 8000, 9000, 8000, "A-GLAZ"), seg(1000, 8250, 9000, 8250, "A-GLAZ"),   # glazing frame
                seg(1000, 11000, 9000, 11000, "A-FLOR-EVTR"), seg(1000, 11200, 9000, 11200, "A-FLOR-EVTR")]
        engine_strips = [{"pts": rect(1000, y, 9000, y + t), "t": t, "layers": ("X", "X")}
                         for y, t in ((5000, 200), (8000, 250), (11000, 200))]
        rep = walls_like_step3(segs, engine_strips, plate)
        self.assertAlmostEqual(rep[LN.WALL]["area_sqm"], 8 * 0.2, places=2)      # the wall only

    def test_no_wall_layers_keeps_the_engine_strips(self):
        plate = rect(0, 0, 20000, 15000)
        engine_strips = [{"pts": rect(1000, 5000, 9000, 5200), "t": 200, "layers": ("X", "X")}]
        rep = walls_like_step3([seg(0, 0, 100, 0, "A-FURN")], engine_strips, plate)
        self.assertAlmostEqual(rep[LN.WALL]["area_sqm"], 1.6, places=2)


if __name__ == "__main__":
    unittest.main()
