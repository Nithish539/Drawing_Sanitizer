# -*- coding: utf-8 -*-
"""Hanging walls: a wall with an object drawn under it is above floor level.

BHUB 1st Floor: a 300 mm C-shaped element of the structure xref crossed
both toilets; three cubicle doors swing under it. Taken as a wall it cut
1.19 sq.m out of the female toilet and 1.11 out of the male one. The whole
element is a hanging wall: not a wall, its area in the room, drawn on
AI_HANGING_WALL. Lift graphics, loose lines and WC cisterns half in a duct
wall are not objects under a wall.

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
from Drawing_Analyzer import layernames as LN  # noqa: E402

WALL = "A-BLOCKWORK"
BEAM_HATCH = "XREF-T1_STR$1$A3-HATCH"
BEAM_LINE = "XREF-T1_STR$0$BASE._STR-COLUMN"
FLOOR = [(-5000, -5000), (20000, -5000), (20000, 20000), (-5000, 20000)]


def _wall(msp, x0, y0, x1, y1, layer=WALL):
    msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True,
                       dxfattribs={"layer": layer})


def _hatch(msp, pts, layer=BEAM_HATCH, pattern="ANSI31"):
    h = msp.add_hatch(dxfattribs={"layer": layer})
    h.set_pattern_fill(pattern, scale=50)
    h.paths.add_polyline_path(pts, is_closed=True)
    return h


def _room(msp):
    """An 8 x 5 m toilet: block walls all round, its name at (4000, 1500)."""
    _wall(msp, -200, -200, 8200, 0)
    _wall(msp, -200, 5000, 8200, 5200)
    _wall(msp, -200, 0, 0, 5000)
    _wall(msp, 8000, 0, 8200, 5000)
    msp.add_text("FEMALE TOILET", dxfattribs={"layer": "ROOMNAME", "height": 250, "insert": (3000, 1500)})


def _beam(msp, y0=3000, y1=3300, x0=0, x1=8000):
    """A 300 mm structural band right across the room, hatched and outlined
    on the structure xref's layers."""
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    _hatch(msp, pts)
    msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": BEAM_LINE})


def _door_block(doc, name="FRD"):
    """A 600 mm cubicle door: hinge at the origin, leaf up, swing to the right."""
    if name not in doc.blocks:
        b = doc.blocks.new(name)
        b.add_line((0, 0), (0, 600), dxfattribs={"layer": "A3-DOOR"})
        b.add_arc((0, 0), 600, 0, 90, dxfattribs={"layer": "A3-DOOR"})
    return name


def _doors_under(doc, msp, y=2700, xs=(1000, 3000, 5000)):
    """Cubicle doors whose swings pass under the band (y 3000-3300)."""
    name = _door_block(doc)
    for x in xs:
        msp.add_blockref(name, (x, y), dxfattribs={"layer": "A3-DOOR"})


def _new():
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_lwpolyline(FLOOR, close=True, dxfattribs={"layer": "FLOOR"})
    return doc, msp


class HangingWallEndToEnd(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _run(self, doc, **rules):
        src = os.path.join(self.tmp, "in.dxf")
        dst = os.path.join(self.tmp, "out.dxf")
        doc.saveas(src)
        floors = [{"name": "F", "polygon": FLOOR}]
        rows = dr.extract_texts(src, floors)
        stats = dr.annotate_dxf(src, dst, floors, rows, rule_overrides=rules or None)
        areas = dr.compute_room_areas(dst, "FLOOR")
        out = ezdxf.readfile(dst)
        layers = [e.dxf.layer for e in out.modelspace().query("LWPOLYLINE")]
        return {r["name"]: r for r in areas["rooms"]}, stats, layers

    def test_doors_under_a_band_make_it_a_hanging_wall(self):
        doc, msp = _new()
        _room(msp)
        _beam(msp)
        _doors_under(doc, msp)
        rooms, stats, layers = self._run(doc)
        # the band is not a wall: the toilet is the whole 8 x 5 m
        self.assertAlmostEqual(rooms["FEMALE TOILET"]["area_sqm"], 40.0, delta=0.1)
        self.assertEqual(stats.get("hanging_walls_count"), 1)
        self.assertAlmostEqual(stats.get("hanging_walls_area_sqm"), 2.4, delta=0.01)
        self.assertAlmostEqual(stats.get("hanging_walls_to_rooms_sqm"), 2.4, delta=0.05)
        self.assertIn("3 door swings", stats["hanging_wall_items"][0]["evidence"])
        self.assertIn(LN.HANGING_WALL, layers)

    def test_rule_off_the_band_is_a_wall(self):
        doc, msp = _new()
        _room(msp)
        _beam(msp)
        _doors_under(doc, msp)
        rooms, stats, layers = self._run(doc, hanging_walls=False)
        # the band splits the room: the label's side, 8 x 3 m
        self.assertAlmostEqual(rooms["FEMALE TOILET"]["area_sqm"], 24.0, delta=0.1)
        self.assertFalse(stats.get("hanging_walls_count"))
        self.assertNotIn(LN.HANGING_WALL, layers)

    def test_a_band_with_nothing_under_it_is_a_wall(self):
        doc, msp = _new()
        _room(msp)
        _beam(msp)
        rooms, stats, _layers = self._run(doc)
        self.assertAlmostEqual(rooms["FEMALE TOILET"]["area_sqm"], 24.0, delta=0.1)
        self.assertFalse(stats.get("hanging_walls_count"))


class HangingWallFinder(unittest.TestCase):
    """find_hanging_walls on its own: what is and is not an object under a wall."""

    def _found(self, doc, msp):
        units = dr.MM_UNITS
        hatches = dr._collect_wall_hatches(doc, msp, units)
        return dr.find_hanging_walls(doc, msp, units, hatches), hatches

    def test_door_swings_under_the_band(self):
        doc, msp = _new()
        _beam(msp)
        _doors_under(doc, msp)
        found, _h = self._found(doc, msp)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["evidence"], "3 door swings")

    def test_a_wc_standing_in_the_band(self):
        doc, msp = _new()
        _beam(msp)
        b = doc.blocks.new("WC-2")
        b.add_lwpolyline([(0, 0), (400, 0), (400, 600), (0, 600)], close=True, dxfattribs={"layer": "toil-fix"})
        msp.add_blockref("WC-2", (2000, 2900), dxfattribs={"layer": "toil-fix"})        # 37% of it in the band: not under
        w = doc.blocks.new("WB-1")                                                       # a basin, 400 x 250
        w.add_lwpolyline([(0, 0), (400, 0), (400, 250), (0, 250)], close=True, dxfattribs={"layer": "toil-fix"})
        msp.add_blockref("WB-1", (4000, 3030), dxfattribs={"layer": "toil-fix"})        # standing in the band
        found, _h = self._found(doc, msp)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["evidence"], "1 fixture")

    def test_a_cistern_partly_in_a_wall_is_not_under_it(self):
        doc, msp = _new()
        _beam(msp)
        b = doc.blocks.new("WC-2")
        b.add_lwpolyline([(0, 0), (400, 0), (400, 600), (0, 600)], close=True, dxfattribs={"layer": "toil-fix"})
        msp.add_blockref("WC-2", (2000, 2600), dxfattribs={"layer": "toil-fix"})        # 200 of its 600 mm in the band
        found, _h = self._found(doc, msp)
        self.assertEqual(found, [])

    def test_lift_graphics_are_never_objects(self):
        doc, msp = _new()
        _beam(msp)
        b = doc.blocks.new("LIFT DOOR")
        b.add_arc((0, 0), 900, 0, 90, dxfattribs={"layer": "A-FLOR-EVTR"})
        b.add_line((0, 0), (0, 900), dxfattribs={"layer": "A-FLOR-EVTR"})
        msp.add_blockref("LIFT DOOR", (1000, 2700), dxfattribs={"layer": "A-FLOR-EVTR"})
        msp.add_arc((3000, 2700), 600, 0, 90, dxfattribs={"layer": "A-FLOR-EVTR-DOOR"})
        found, _h = self._found(doc, msp)
        self.assertEqual(found, [])

    def test_loose_lines_are_never_objects(self):
        doc, msp = _new()
        _beam(msp)
        msp.add_line((1000, 2000), (1000, 4500), dxfattribs={"layer": "P-SAN-DRAIN"})        # a drain across it
        msp.add_line((2000, 2000), (2000, 4500), dxfattribs={"layer": "A-SECTION"})          # a section line
        msp.add_arc((4000, 2700), 600, 0, 90, dxfattribs={"layer": "M-HVAC-EQPM"})          # door-size, not a door
        found, _h = self._found(doc, msp)
        self.assertEqual(found, [])

    def test_a_block_too_thick_or_too_big_is_left_alone(self):
        doc, msp = _new()
        _hatch(msp, [(0, 2500), (8000, 2500), (8000, 3300), (0, 3300)])           # 800 mm thick
        _doors_under(doc, msp)
        found, _h = self._found(doc, msp)
        self.assertEqual(found, [])

    def test_the_whole_element_goes_and_a_real_wall_stays(self):
        """A C-shaped element hit on one run goes whole: its hatch, its
        outline lines; the block wall along its back keeps its faces."""
        doc, msp = _new()
        c = [(300, 3000), (3150, 3000), (3150, 3300), (0, 3300), (0, -2800), (3150, -2800),
             (3150, -2500), (300, -2500)]
        _hatch(msp, c)
        msp.add_lwpolyline(c, close=True, dxfattribs={"layer": BEAM_LINE})
        _wall(msp, -200, -2800, 0, 3300)                          # the real wall behind its back
        msp.add_line((0, -2800), (0, 3300), dxfattribs={"layer": WALL})
        _doors_under(doc, msp, xs=(1000,))
        units = dr.MM_UNITS
        hatches = dr._collect_wall_hatches(doc, msp, units)
        found = dr.find_hanging_walls(doc, msp, units, hatches)
        self.assertEqual(len(found), 1)
        segs = dr._collect_raw_segs(doc, msp, units)
        segs2, hatches2, _a, _c = dr.drop_hanging_walls(found, segs, hatches)
        self.assertEqual([s for s in segs2 if s[4] == BEAM_LINE], [])            # its outline gone
        self.assertTrue(any(s[4] == WALL and abs(s[0]) < 1 and abs(s[2]) < 1 for s in segs2))   # the wall's face kept
        self.assertEqual(len(hatches2), len(hatches) - 1)

    def test_a_c_shape_on_the_column_layer_goes_with_it(self):
        found = [{"poly": __import__("shapely.geometry", fromlist=["box"]).box(0, 3000, 8000, 3300),
                  "rings": [[(0, 3000), (8000, 3000), (8000, 3300), (0, 3300)]], "hatch": ()}]
        cols = {"columns": {"F": [[(9000, 0), (9500, 0), (9500, 500), (9000, 500)]]},
                "rejects": {"F": [[(100, 3000), (3000, 3000), (3000, 3300), (100, 3300)]]}}
        _s, _h, _a, cols2 = dr.drop_hanging_walls(found, None, None, None, cols)
        self.assertEqual(cols2["rejects"]["F"], [])
        self.assertEqual(len(cols2["columns"]["F"]), 1)


if __name__ == "__main__":
    unittest.main()
