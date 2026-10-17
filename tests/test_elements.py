# -*- coding: utf-8 -*-
"""The architect's order of reading a plan (P25 1st floor, P14 6th floor).

  1. drawing furniture is cleared: grid lines, section marks, tags, title
     blocks, revision clouds, landscape - by layer, and by block for a
     section line or a tree; column-layout blocks named for a grid keep
     their columns;
  2. a level tag (LVL + 555.35FFL) or a grid note (GRID FOR CRITICAL ROOM)
     never names a room; platforms, counters and landscape are never walls;
  3. windows: glazing or a louvre drawn across a gap in a wall; a glass
     partition, a mirror or the area overlay is not window evidence;
  4. glass: glass partitions and glazing on AI_GLASS, never walls or rooms.

Run:  python -m unittest discover -s tests -v
"""

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402
from shapely.geometry import Polygon, box  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import elements as el  # noqa: E402
from Drawing_Analyzer import labels  # noqa: E402
from Drawing_Analyzer import layernames as LN  # noqa: E402
from Drawing_Analyzer.config import CONFIG  # noqa: E402


def seg(x1, y1, x2, y2, layer="A-WALL"):
    return (x1, y1, x2, y2, layer, math.hypot(x2 - x1, y2 - y1), -1, False, False)


class Step1ClearTheDrawing(unittest.TestCase):

    def test_layers(self):
        for layer in ("A-SYMB-SEC", "A3-SYMBEL", "A3-GRID", "XREF-T1_STR$0$A3-Grid-dim", "AR-TAG-DOOR",
                      "A3-Tag", "A-ELEV SYMBOL", "V-SEC.LINE", "revision", "TITLE"):
            self.assertTrue(dr._is_annotation(layer), layer)
        for layer in ("A-WALL", "A3-WL_EXT", "A-STAGE", "AR-CONCRETE WALL", "A-DOOR", "PLANTING"):
            self.assertFalse(dr._is_annotation(layer), layer)

    def test_blocks(self):
        self.assertTrue(dr._is_annotation("A-WALL", ("SECTION LINE",)))
        self.assertTrue(dr._is_annotation("A绿-1", ("A$C5FC924F8", "*E22", "arbol_1p")))      # a tree
        self.assertTrue(dr._is_annotation("A-DOOR", ("TAG-DOOR",)))
        # column-layout blocks named for a grid carry the columns
        self.assertFalse(dr._is_annotation("IEC-STEEL-COL", ("COLUMN-GRID-750",)))
        self.assertFalse(dr._is_annotation("COLUMN", ("FIRST FLOOR COLUMN LAYOUT & GRID 20201228",)))

    def test_a_section_line_never_reaches_the_floor(self):
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        msp.add_line((0, 0), (5000, 0), dxfattribs={"layer": "A-WALL"})
        blk = doc.blocks.new("SECTION LINE")
        blk.add_lwpolyline([(0, 0), (20000, 0)], dxfattribs={"layer": "A-SYMB-SEC"})
        msp.add_blockref("SECTION LINE", (0, 3000), dxfattribs={"layer": "A-SYMB-SEC"})
        msp.add_line((0, 6000), (9000, 6000), dxfattribs={"layer": "AR-GRID"})
        layers = {s[4] for s in dr._collect_raw_segs(doc, msp, dr.MM_UNITS)}
        self.assertEqual(layers, {"A-WALL"})


class Step2NamesAndNonWalls(unittest.TestCase):

    def setUp(self):
        self.rules = labels.build_rules(CONFIG, [])

    def test_level_tags_and_grid_notes_name_no_room(self):
        for t in ("LVL + 555.35FFL", "+555.35 FFL", "LEVEL 12.6", "GRID FOR CRITICAL ROOM"):
            self.assertFalse(labels.looks_like_room_name(t, self.rules), t)
        for t in ("LIFT LOBBY", "LEVEL CONTROL ROOM", "KITCHEN"):
            self.assertTrue(labels.looks_like_room_name(t, self.rules), t)

    def test_platforms_counters_landscape_are_never_walls(self):
        never = [k.upper() for k in dr._HARD_EXCLUDE_KEYWORDS]
        for layer in ("AR-PLATFORM", "A- Platform", "Counter", "KITCHEN EQUIPMENTS", "ADB-LANDSCAPING", "L-PLANTING"):
            self.assertTrue(any(k in layer.upper() for k in never), layer)


def wall(x0, x1, y0=0, y1=200):
    return {"pts": [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], "t": y1 - y0}


def wall_lines(x0, x1, y0=0, y1=200):
    return [seg(x0, y0, x1, y0), seg(x0, y1, x1, y1), seg(x0, y0, x0, y1), seg(x1, y0, x1, y1)]


class Step3Windows(unittest.TestCase):

    def facade(self):
        strips = [wall(0, 3000), wall(4200, 8000)]
        return strips, wall_lines(0, 3000) + wall_lines(4200, 8000)

    def test_glazing_across_a_gap_is_a_window_its_frame_deep(self):
        strips, segs = self.facade()
        ev = [(3000, 60, 4200, 60), (3000, 140, 4200, 140), (3000, 40, 4200, 40), (3000, 160, 4200, 160)]
        got = dr.window_openings(ev, segs, strips)
        self.assertEqual(len(got), 1)
        self.assertEqual(tuple(round(v) for v in Polygon(got[0]).bounds), (3000, 40, 4200, 160))

    def test_a_short_stub_of_glazing_is_not(self):
        strips, segs = self.facade()
        self.assertEqual(dr.window_openings([(3000, 100, 3300, 100)], segs, strips), [])

    def test_a_door_gap_is_a_door(self):
        strips, segs = self.facade()
        door = [[(3000, 0), (4200, 0), (4200, 200), (3000, 200)]]
        self.assertEqual(dr.window_openings([(3000, 100, 4200, 100)], segs, strips, avoid=door), [])

    def test_evidence_words(self):
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        msp.add_line((0, 0), (1000, 0), dxfattribs={"layer": "A-GLAZ"})               # window glazing
        msp.add_line((0, 100), (1000, 100), dxfattribs={"layer": "AR-GLASS PART"})     # a glass partition
        msp.add_line((0, 200), (1000, 200), dxfattribs={"layer": "2.1 TSIIC-BUA (exc glass)"})
        b = doc.blocks.new("LOVERS-2 DETAIL")
        b.add_line((0, 0), (0, 1000), dxfattribs={"layer": "M-HVAC-FRSH"})            # a louvre
        msp.add_blockref("LOVERS-2 DETAIL", (5000, 0))
        ev = dr._collect_window_evidence(doc, msp, dr.MM_UNITS)
        ys = sorted(round(e[1]) for e in ev)
        self.assertEqual(ys, [0, 0])                  # the glazing and the louvre


class Step3Glass(unittest.TestCase):

    def test_a_pair_of_glass_lines_is_one_strip(self):
        segs = [(0, 0, 4000, 0), (0, 12, 4000, 12)]
        got = el.glass_elements(segs)
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0].area, 4000 * 12, delta=200)

    def test_a_single_line_is_a_band_and_a_window_cuts_it(self):
        got = el.glass_elements([(0, 0, 4000, 0)], cut=[box(1000, -100, 2000, 100)])
        self.assertEqual(len(got), 2)
        self.assertTrue(all(abs(g.bounds[3] - g.bounds[1] - el.GLASS_LINE_MM) < 1 for g in got))

    def test_mirrors_and_overlays_are_not_glass(self):
        self.assertFalse(el.is_glass("A3-GLASS", ("A$C72861279", "MIRROR")))
        self.assertFalse(el.is_glass("2.1 TSIIC-BUA (exc glass)"))
        self.assertTrue(el.is_glass("AR-GLASS PART"))
        self.assertTrue(el.is_glass("AR-GLAZZING"))

    def test_glass_is_never_a_room(self):
        self.assertTrue(LN.is_reserved(LN.GLASS))


if __name__ == "__main__":
    unittest.main()
