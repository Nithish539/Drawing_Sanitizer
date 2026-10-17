# -*- coding: utf-8 -*-
"""Fixes 0-4 (the XA / Centaurus review, second round):

  0. a core drawn at an angle is worked out turned to the axes (rotation.py)
     - the clusters, the turned drawing, the turned-back results, and a
     whole drawing standing at an angle;
  1. a shaft is the compartment its X stands in, traced from the X: closed
     on walls, doors and the partition line; split across two compartments;
     the room already traced there takes the X; a failed trace falls back
     to the X and is flagged;
  2. every door line on the inner face of the wall of the room it serves;
  3. the stair's well between two flights, however it was drawn; S/C-S3
     is a staircase, STR PRE a shaft;
  4. a window never cuts a wall: the band is filled across it.

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

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import layernames as LN  # noqa: E402
from Drawing_Analyzer import rotation as rot  # noqa: E402
from Drawing_Analyzer import rules  # noqa: E402
from Drawing_Analyzer.dxfio import load_document  # noqa: E402


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def seg(x0, y0, x1, y1, layer="A-WALL"):
    return (x0, y0, x1, y1, layer, math.hypot(x1 - x0, y1 - y0), -1, False, False)


def turned(pts, deg, o=(0, 0)):
    return rot.rotate_pts(pts, deg, o)


def _lw(msp, pts, layer, close=True):
    msp.add_lwpolyline(pts, close=close, dxfattribs={"layer": layer})


def _wall_band(msp, x0, y0, x1, y1, t=200, layer="A-WALL"):
    """The two faces and end caps of a wall x0..x1 (horizontal) or y0..y1."""
    _lw(msp, rect(x0, y0, x1, y1), layer)


def _walled_cell(msp, x0, y0, x1, y1, t=200, layer="A-WALL"):
    """A cell x0..x1, y0..y1 with walls t thick round it, drawn as closed
    outlines (the way the test drawings in this suite draw them)."""
    _lw(msp, rect(x0 - t, y0 - t, x1 + t, y0), layer)
    _lw(msp, rect(x0 - t, y1, x1 + t, y1 + t), layer)
    _lw(msp, rect(x0 - t, y0, x0, y1), layer)
    _lw(msp, rect(x1, y0, x1 + t, y1), layer)


class RotationMathTests(unittest.TestCase):

    def test_turn_and_turn_back(self):
        pts = [(1000, 2000), (3000, 2000), (3000, 5000)]
        back = rot.rotate_pts(rot.rotate_pts(pts, 37.0, (500, 500)), -37.0, (500, 500))
        for a, b in zip(pts, back):
            self.assertAlmostEqual(a[0], b[0], places=6)
            self.assertAlmostEqual(a[1], b[1], places=6)

    def test_wall_directions_fold_perpendiculars(self):
        segs = [seg(0, 0, 10000, 0), seg(0, 0, 0, 8000),
                seg(*turned([(0, 0)], 30)[0], *turned([(10000, 0)], 30)[0])]
        h = rot.wall_directions(segs)
        self.assertAlmostEqual(h[0.0], 18000.0, delta=1)
        self.assertAlmostEqual(h[30.0], 10000.0, delta=1)


class ClusterTests(unittest.TestCase):
    """A 12 x 9 m core of 300 mm walls turned 20 degrees, standing on an
    axis-aligned floor with its own walls."""

    def core(self, deg, at=(40000, 30000)):
        out = []
        o = at
        for x0, y0, x1, y1 in ((0, 0, 12000, 0), (0, 0, 0, 9000), (12000, 0, 12000, 9000), (0, 9000, 12000, 9000),
                               (4000, 0, 4000, 9000), (8000, 0, 8000, 9000), (0, 4500, 12000, 4500)):
            for off in (0, 300):
                a, b = turned([(x0 + off, y0 + off), (x1 + off, y1 + off)], deg, (0, 0))
                out.append(seg(a[0] + o[0], a[1] + o[1], b[0] + o[0], b[1] + o[1]))
        return out

    def floor(self):
        out = []
        for i in range(8):
            out.append(seg(0, i * 10000, 80000, i * 10000))
            out.append(seg(0, i * 10000 + 200, 80000, i * 10000 + 200))
        for i in range(9):
            out.append(seg(i * 10000, 0, i * 10000, 70000))
        return out

    def test_the_turned_core_is_found(self):
        plate = rect(0, 0, 80000, 70000)
        cl = rot.wall_angle_clusters(self.floor() + self.core(20.0), plate)
        self.assertEqual(len(cl), 1)
        self.assertAlmostEqual(cl[0]["theta"], 20.0, delta=1.5)
        self.assertFalse(cl[0]["whole"])
        core = Polygon(cl[0]["core"])
        self.assertTrue(core.contains(Polygon(turned(rect(0, 0, 12000, 9000), 20.0)).centroid.buffer(1)
                                      .translate(40000, 30000) if False else
                                      Polygon([(p[0] + 40000, p[1] + 30000)
                                               for p in turned(rect(0, 0, 12000, 9000), 20.0)]).centroid))

    def test_an_axis_aligned_floor_has_none(self):
        plate = rect(0, 0, 80000, 70000)
        self.assertEqual(rot.wall_angle_clusters(self.floor() + self.core(0.0), plate), [])

    def test_a_slanted_facade_with_square_rooms_is_no_core(self):
        """Centaurus: the 16 degree facade with the toilets' square walls
        against it - walls one way only."""
        plate = rect(0, 0, 80000, 70000)
        segs = list(self.floor())
        a, b = turned([(0, 0), (60000, 0)], 16.0)
        for off in (0, 300):
            segs.append(seg(a[0] + 5000, a[1] + 5000 + off, b[0] + 5000, b[1] + 5000 + off))
        self.assertEqual(rot.wall_angle_clusters(segs, plate), [])

    def test_a_whole_drawing_at_an_angle(self):
        plate = [(p[0] + 40000, p[1] + 35000) for p in turned(rect(-40000, -35000, 40000, 35000), 30.0)]
        segs = []
        for s in self.floor():
            a, b = turned([(s[0] - 40000, s[1] - 35000), (s[2] - 40000, s[3] - 35000)], 30.0)
            segs.append(seg(a[0] + 40000, a[1] + 35000, b[0] + 40000, b[1] + 35000))
        cl = rot.wall_angle_clusters(segs, plate)
        self.assertEqual(len(cl), 1)
        self.assertTrue(cl[0]["whole"])
        self.assertAlmostEqual(cl[0]["theta"], 30.0, delta=1.5)


class SubdocumentTests(unittest.TestCase):

    def test_the_turned_drawing_lies_along_the_axes_and_the_source_is_untouched(self):
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        blk = doc.blocks.new("CORE")
        blk.add_line((0, 0), (12000, 0), dxfattribs={"layer": "A-WALL"})
        blk.add_line((0, 0), (0, 9000), dxfattribs={"layer": "A-WALL"})
        msp.add_blockref("CORE", (40000, 30000), dxfattribs={"rotation": 25.0})
        msp.add_line((0, 0), (80000, 0), dxfattribs={"layer": "A-WALL"})
        n0 = len(msp)
        units = dr.MM_UNITS
        region = [(p[0] + 40000, p[1] + 30000) for p in turned(rect(-2000, -2000, 14000, 11000), 25.0)]
        mini, mmsp = rot.rotated_subdocument(doc, msp, units, region, 25.0, (46000, 34500))
        self.assertEqual(len(msp), n0)
        # the copy keeps its blocks (the passes read it as they read the
        # original); the block reference is turned, its lines then lie
        # along the axes
        ins = list(mmsp.query("INSERT"))
        self.assertEqual(len(ins), 1)
        self.assertAlmostEqual(ins[0].dxf.rotation % 90.0, 0.0, delta=0.01)
        lines = [e for e in ins[0].virtual_entities() if e.dxftype() == "LINE"]
        self.assertGreaterEqual(len(lines), 2)
        angles = sorted(round(math.degrees(math.atan2(e.dxf.end.y - e.dxf.start.y, e.dxf.end.x - e.dxf.start.x)) % 90)
                        for e in lines if e.dxf.layer == "A-WALL" and
                        math.hypot(e.dxf.end.x - e.dxf.start.x, e.dxf.end.y - e.dxf.start.y) > 5000)
        self.assertTrue(all(a in (0, 90) for a in angles), angles)
        # the long wall of the floor far from the region is dropped
        self.assertEqual(len(list(mmsp.query("LINE"))), 0)


class UnrotateTests(unittest.TestCase):

    def test_a_pass_result_is_turned_back(self):
        o = (1000, 1000)
        poly = turned(rect(2000, 2000, 5000, 4000), -20.0, o)
        res = {"merged": [{"poly": poly, "holes": [], "bbox": (0, 0, 1, 1), "cx": 3500, "cy": 3000, "source": "P"},
                          {"poly": None, "holes": [], "bbox": (0, 0, 600, 300), "xrects": [(0, 0, 600, 300)],
                           "cx": 300, "cy": 150, "source": "X", "cell": True}],
               "walls": {"AI_WALL": [[rect(0, 0, 100, 1000)]]}, "doors": [rect(0, 0, 900, 200)],
               "windows": [{"poly": rect(0, 0, 400, 100), "cx": 200, "cy": 50}], "cutouts": [], "glass": []}
        dr._unrotate_pass(res, 20.0, o)
        g = Polygon(res["merged"][0]["poly"])
        self.assertAlmostEqual(g.area, 3000 * 2000, delta=1)
        b = g.bounds
        self.assertAlmostEqual(b[0], 2000, delta=1)
        self.assertAlmostEqual(b[3], 4000, delta=1)
        x = res["merged"][1]
        self.assertEqual(len(x["poly"]), 4)
        self.assertAlmostEqual(Polygon(x["poly"]).area, 600 * 300, delta=1)
        self.assertTrue(x["cell"])                      # a flag is left alone


class MergeTests(unittest.TestCase):

    def test_what_the_turned_pass_found_in_the_core_replaces_the_main_passs(self):
        core = rect(10000, 10000, 20000, 20000)
        res = {"merged": [{"poly": rect(11000, 11000, 12000, 12000), "holes": [], "bbox": (11000, 11000, 12000, 12000),
                           "sr_no": 1, "source": "P"},
                          {"poly": rect(30000, 30000, 31000, 31000), "holes": [], "bbox": (30000, 30000, 31000, 31000),
                           "sr_no": 2, "source": "P"}],
               "text_status": {1: "primary", 2: "primary"}, "label_notes": {}, "floor_family_overrides": {},
               "size_flags": {}, "grown": [], "walls": {"AI_WALL": [[rect(0, 0, 40000, 200)]]},
               "doors": [rect(11000, 10000, 11900, 10200)], "door_kinds": ["room"], "door_levels": ["primary"],
               "door_sides": [(None, None)], "door_review": [], "windows": [], "cutouts": [], "glass": [],
               "wall_veto": [], "wall_exclusions": [], "window_conflicts": [], "stats": {"a": 1}}
        sub = {"merged": [{"poly": rect(11000, 11000, 13000, 13000), "holes": [], "bbox": (11000, 11000, 13000, 13000),
                           "sr_no": 1, "source": "P"}],
               "text_status": {1: "x"}, "label_notes": {1: "turned"}, "walls": {"AI_WALL": [[rect(12000, 9000, 12200, 25000)]]},
               "doors": [rect(15000, 10000, 15900, 10200)], "door_kinds": ["shaft"], "door_levels": ["secondary"],
               "door_sides": [(None, None)], "stats": {"a": 2}}
        dr._merge_rotated_pass(res, sub, core, [{"sr_no": 1, "x": 11500, "y": 11500}])
        names = sorted(Polygon(d["poly"]).area for d in res["merged"])
        self.assertEqual(names, [1000 * 1000, 2000 * 2000])
        self.assertEqual(res["text_status"][1], "x")
        self.assertEqual(res["text_status"][2], "primary")
        self.assertEqual(res["label_notes"][1], "turned")
        self.assertEqual(len(res["doors"]), 1)
        self.assertEqual(res["door_kinds"], ["shaft"])
        self.assertEqual(res["stats"]["a"], 3)
        # the main wall is cut back to outside the core, the turned pass's
        # wall is clipped to it
        W = [Polygon(p[0]) for p in res["walls"]["AI_WALL"]]
        self.assertTrue(all(not w.intersects(Polygon(core).buffer(-10)) or
                            Polygon(core).contains(w.representative_point()) for w in W))


class CompartmentTests(unittest.TestCase):
    """A floor 30 x 20 m: an X-marked shaft 600 x 1200 at the back of a
    walled cell 1200 x 2400 with a door at its front; next to it a cell
    split by a partition line, the X on one side; and a loose X against
    one wall only, open to the floor (the fallback)."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        plate = rect(0, 0, 30000, 20000)
        _lw(msp, plate, "A-FLOOR")
        # cell A: x 5000..6200, y 5000..7400; walls 200; door at the front (y 5000) 900 wide
        _lw(msp, rect(4800, 7400, 6400, 7600), "A-WALL")
        _lw(msp, rect(4800, 5000, 5000, 7400), "A-WALL")
        _lw(msp, rect(6200, 5000, 6400, 7400), "A-WALL")
        _lw(msp, rect(4800, 4800, 5100, 5000), "A-WALL")
        _lw(msp, rect(6100, 4800, 6400, 5000), "A-WALL")
        msp.add_line((5100, 5000), (6100, 5000), dxfattribs={"layer": "A-DOOR"})
        msp.add_arc((5100, 5000), 1000, 0, 90, dxfattribs={"layer": "A-DOOR"})
        # the X: 600 x 1200 at the back, no outline of its own
        msp.add_line((5300, 6200), (5900, 7400), dxfattribs={"layer": "A-CUTOUT"})
        msp.add_line((5300, 7400), (5900, 6200), dxfattribs={"layer": "A-CUTOUT"})
        msp.add_mtext("TEN", dxfattribs={"insert": (5600, 6800), "char_height": 120, "layer": "A-TEXT"})
        # cell B: x 10000..11200, y 5000..7400, a partition line at y 6400, the X behind it
        _lw(msp, rect(9800, 7400, 11400, 7600), "A-WALL")
        _lw(msp, rect(9800, 5000, 10000, 7400), "A-WALL")
        _lw(msp, rect(11200, 5000, 11400, 7400), "A-WALL")
        _lw(msp, rect(9800, 4800, 10100, 5000), "A-WALL")
        _lw(msp, rect(11100, 4800, 11400, 5000), "A-WALL")
        msp.add_line((10100, 5000), (11100, 5000), dxfattribs={"layer": "A-DOOR"})
        msp.add_arc((10100, 5000), 1000, 0, 90, dxfattribs={"layer": "A-DOOR"})
        msp.add_line((10000, 6400), (11200, 6400), dxfattribs={"layer": "A-DETL"})
        msp.add_line((10300, 6600), (10900, 7400), dxfattribs={"layer": "A-CUTOUT"})
        msp.add_line((10300, 7400), (10900, 6600), dxfattribs={"layer": "A-CUTOUT"})
        msp.add_mtext("CHW SHAFT", dxfattribs={"insert": (10600, 7000), "char_height": 120, "layer": "A-TEXT"})
        # a loose X against one wall, open to the floor on its other sides
        _lw(msp, rect(19800, 9000, 20000, 12000), "A-WALL")
        msp.add_line((20000, 10000), (20600, 11200), dxfattribs={"layer": "A-CUTOUT"})
        msp.add_line((20000, 11200), (20600, 10000), dxfattribs={"layer": "A-CUTOUT"})
        msp.add_mtext("RWP", dxfattribs={"insert": (20300, 10600), "char_height": 120, "layer": "A-TEXT"})
        src = os.path.join(tmp, "in.dxf")
        doc.saveas(src)
        floors = [{"name": "F1", "polygon": plate}]
        rows = dr.extract_texts(src, floors)
        cls.out = os.path.join(tmp, "out.dxf")
        dr._PASS_CACHE_DIR = None
        cls.stats = dr.annotate_dxf(src, cls.out, floors, rows)
        cls.areas = dr.compute_room_areas(cls.out, "A-FLOOR")

    def rooms(self, name):
        return [r for r in self.areas["rooms"] if r["name"] == name]

    def test_the_shaft_is_its_compartment(self):
        ten = self.rooms("TEN")
        self.assertEqual(len(ten), 1, ten)
        self.assertTrue(ten[0]["opening"])
        self.assertAlmostEqual(ten[0]["area_sqm"], 1.2 * 2.4, delta=0.06)

    def test_the_partition_line_splits_shaft_and_room(self):
        # the side holding the X is the shaft; the side with the door is
        # the shaft's room (AI_MEP), named as it
        chw = self.rooms("CHW SHAFT")
        self.assertEqual(len(chw), 2, chw)
        shaft = [r for r in chw if r["opening"]]
        room = [r for r in chw if not r["opening"]]
        self.assertEqual(len(shaft), 1, chw)
        self.assertEqual(len(room), 1, chw)
        self.assertAlmostEqual(shaft[0]["area_sqm"], 1.2 * 1.0, delta=0.06)
        self.assertAlmostEqual(room[0]["area_sqm"], 1.2 * 1.4, delta=0.06)
        self.assertEqual(room[0]["layer"], "AI_MEP")

    def test_a_loose_x_falls_back_and_is_flagged(self):
        rwp = self.rooms("RWP")
        self.assertEqual(len(rwp), 1)
        self.assertAlmostEqual(rwp[0]["area_sqm"], 0.6 * 1.2, delta=0.02)
        self.assertGreaterEqual(self.stats.get("shaft_cells_fallback", 0), 1)
        doc = ezdxf.readfile(self.out)
        notes = [e.text for e in doc.modelspace().query("MTEXT") if e.dxf.layer == "AI_REVIEW"]
        self.assertTrue(any("shaft" in n.lower() for n in notes), notes)

    def test_the_door_is_on_the_inner_face(self):
        doc = ezdxf.readfile(self.out)
        ys = []
        for e in doc.modelspace().query("LWPOLYLINE LINE"):
            if LN.is_door_layer(e.dxf.layer):
                pts = ([(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)] if e.dxftype() == "LINE"
                       else list(e.get_points("xy")))
                if all(4900 < p[0] < 6300 for p in pts):
                    ys += [p[1] for p in pts]
        self.assertTrue(ys)
        self.assertTrue(all(abs(y - 5000) < 2 for y in ys), ys)   # the cell's own face, not the outer one

    def test_the_rule_can_be_turned_off(self):
        self.assertIn("shaft_compartment", rules.defaults())
        self.assertNotIn("shaft_room_to_door_line", rules.defaults())


class StairAndNamingTests(unittest.TestCase):

    def test_stair_abbreviations(self):
        for name in ("S/C-S3", "S.C.-S1", "SC-2", "STR-1"):
            self.assertEqual(dr.family_for_label(name, dr._FAMILY_RULES)["name"], "Staircase", name)
        for name in ("STR PRE", "STR \nPRE", "STR PRESS SHAFT", "STAIR PREZ"):
            self.assertEqual(dr.family_for_label(name, dr._FAMILY_RULES)["name"], "Shaft", name)

    def test_the_well_range_takes_the_xa_well(self):
        from Drawing_Analyzer import geoengine as ge
        R = ge.load_profile()["ranges"]
        self.assertLessEqual(R["stair_well_short_mm"][0], 446)

    def test_any_layer_x_marks(self):
        """The X sequence's step 1 (2026-10-16): every X mark on the floor,
        on any layer and of any size - a lift-size X and a stair-well X off
        the cut-out layers, a 120 mm X on a plain layer - is found, once."""
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        plate = rect(0, 0, 40000, 30000)
        _lw(msp, plate, "A-FLOOR")
        for k in range(3):                               # three cut-out X marks
            x0 = 2000 + k * 2000
            msp.add_line((x0, 2000), (x0 + 600, 3000), dxfattribs={"layer": "A-CUTOUT"})
            msp.add_line((x0, 3000), (x0 + 600, 2000), dxfattribs={"layer": "A-CUTOUT"})
        msp.add_line((20000, 20000), (22500, 22500), dxfattribs={"layer": "A-HIDDEN"})   # a lift
        msp.add_line((20000, 22500), (22500, 20000), dxfattribs={"layer": "A-HIDDEN"})
        msp.add_lwpolyline([(30000, 10000), (30450, 12600)], dxfattribs={"layer": "A-ELEV-HIDN"})  # a well
        msp.add_lwpolyline([(30000, 12600), (30450, 10000)], dxfattribs={"layer": "A-ELEV-HIDN"})
        msp.add_line((10000, 15000), (10120, 15120), dxfattribs={"layer": "A-FLOR"})     # a small X
        msp.add_line((10000, 15120), (10120, 15000), dxfattribs={"layer": "A-FLOR"})
        units = dr.MM_UNITS
        prep = dr._prepare_floors(doc, msp, units, [{"name": "F", "polygon": plate}], rules.defaults())
        self.assertEqual(prep["detection_mode"], "geometry")
        self.assertNotIn("x_any_centres", prep)
        centres = sorted((round(cl["cx"]), round(cl["cy"])) for cl in prep["x_clusters"])
        for c in ((2300, 2500), (4300, 2500), (6300, 2500), (21250, 21250), (30225, 11300), (10060, 15060)):
            self.assertIn(c, centres)


class WindowBridgeTests(unittest.TestCase):

    def test_the_band_runs_through_the_window(self):
        """A 300 mm wall drawn as two strips either side of a 435 x 109
        window box (XA lift bank): with the window, one continuous band."""
        strips = [{"pts": rect(0, 0, 3000, 300), "hatched": False},
                  {"pts": rect(3435, 0, 7000, 300), "hatched": False},
                  {"pts": rect(3000, 100, 3435, 209), "hatched": False}]     # the pairing under the window
        plate = rect(-1000, -1000, 8000, 3000)
        window = rect(3000, 100, 3435, 209)
        pieces, _rep = dr.wall_polygons(strips, plate, [], [], [], [], [window], bridge=[window], counts={})
        W = Polygon(rect(0, 0, 0, 0))
        from shapely.ops import unary_union
        W = unary_union([p for lst in pieces.values() for p in lst])
        for pt in ((3200, 50), (3200, 150), (3200, 260)):
            self.assertTrue(W.buffer(1).contains(Polygon(rect(pt[0] - 1, pt[1] - 1, pt[0] + 1, pt[1] + 1))), pt)

    def test_turned_off(self):
        saved = dr.WINDOW_BRIDGE_MM
        try:
            dr.WINDOW_BRIDGE_MM = 0.0
            strips = [{"pts": rect(0, 0, 3000, 300), "hatched": False},
                      {"pts": rect(3435, 0, 7000, 300), "hatched": False}]
            plate = rect(-1000, -1000, 8000, 3000)
            pieces, _rep = dr.wall_polygons(strips, plate, [], [], [], [], [rect(3000, 100, 3435, 209)], bridge=[rect(3000, 100, 3435, 209)], counts={})
            from shapely.ops import unary_union
            W = unary_union([p for lst in pieces.values() for p in lst])
            self.assertFalse(W.contains(Polygon(rect(3199, 49, 3201, 51))))
        finally:
            dr.WINDOW_BRIDGE_MM = saved


class FinalDrawingTests(unittest.TestCase):

    def test_a_shaft_is_step3s_whatever_the_geometry_drew(self):
        """2026-10-16: a shaft is Step 3's (the X sequence) - its outline,
        traced to its compartment or as its X, stands against anything the
        geometry drew there, and nothing in Step 5 changes it."""
        import importlib
        app = importlib.import_module("Drawing_Analyzer.app")

        def row(cell, source, ta, ga, iou):
            return {"id": "CMP-1", "category": "AGREEMENT", "level": "exact", "iou": iou,
                    "text": {"cls": "SHAFT", "name": "TEN", "area_sqm": ta, "poly": rect(0, 0, 1, 1),
                             "cell": cell, "source": source},
                    "geo": {"id": "GEO-1", "cls": "ROOM", "label": "Room 1", "area_sqm": ga, "category": "",
                            "poly": rect(0, 0, 1, 1)}}
        cmp = {"rows": [row(True, "X", 2.9, 0.7, 0.3), row(False, "X", 0.7, 0.7, 0.95)]}
        app._final_defaults(cmp)
        for r in cmp["rows"]:
            d = r["final_default"]
            self.assertEqual((d["use"], d["family"], d["decide"]), ("text", "Shaft", False))
            self.assertTrue(d["fixed"])


if __name__ == "__main__":
    unittest.main()
