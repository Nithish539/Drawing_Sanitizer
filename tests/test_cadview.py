# -*- coding: utf-8 -*-
"""CAD viewer data: blocks placed once per insert, colours as CAD shows them.

Run:  python -m unittest discover -s tests -v
"""

import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402
import numpy as np  # noqa: E402

from Drawing_Analyzer import cadview  # noqa: E402

RED, GREEN, BLUE, WHITE = 0xFF0000, 0x00FF00, 0x0000FF, 0xFFFFFF


def _doc():
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4                        # millimetres
    doc.layers.add("WALLS", color=1)                   # red
    doc.layers.add("GLASS", color=5)                   # blue
    doc.layers.add("HIDDEN", color=3).off()
    blk = doc.blocks.new("CHAIR")
    blk.add_line((0, 0), (1000, 0), dxfattribs={"layer": "0"})           # BYLAYER on 0
    blk.add_line((0, 0), (0, 1000), dxfattribs={"layer": "0", "color": 0})  # BYBLOCK
    blk.add_text("SEAT", height=100, dxfattribs={"insert": (100, 100)})
    msp = doc.modelspace()
    msp.add_line((0, 0), (5000, 0), dxfattribs={"layer": "WALLS"})
    msp.add_line((0, 0), (0, 5000), dxfattribs={"layer": "HIDDEN"})
    # Two inserts: one on WALLS (red), one rotated on GLASS with colour green.
    msp.add_blockref("CHAIR", (10000, 0), dxfattribs={"layer": "WALLS"})
    msp.add_blockref("CHAIR", (20000, 0), dxfattribs={
        "layer": "GLASS", "rotation": 90, "color": 3})
    return doc


class ExtractTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.dxf")
        _doc().saveas(self.path)
        self.data = cadview.extract(self.path)

    def runs(self, rgb):
        pts, lens = self.data["lines"].get(rgb, (np.zeros((0, 2)), []))
        out, k = [], 0
        for n in lens:
            out.append([tuple(np.round(p).astype(int)) for p in pts[k:k + n]])
            k += n
        return out

    def test_hidden_layer_left_out(self):
        every = [r for rgb in self.data["lines"] for r in self.runs(rgb)]
        self.assertNotIn([(0, 0), (0, 5000)], every)

    def test_layer_colour_and_block_placement(self):
        red = self.runs(RED)
        self.assertIn([(0, 0), (5000, 0)], red)            # WALLS line
        # First insert on WALLS: layer-0 BYLAYER line takes the insert's
        # layer colour, the BYBLOCK line the insert's (BYLAYER) colour.
        self.assertIn([(10000, 0), (11000, 0)], red)
        self.assertIn([(10000, 0), (10000, 1000)], red)

    def test_rotated_insert_and_byblock(self):
        # Rotated 90 degrees: +x in the block becomes +y on the sheet.
        self.assertIn([(20000, 0), (20000, 1000)], self.runs(BLUE))    # layer 0 -> GLASS
        self.assertIn([(20000, 0), (19000, 0)], self.runs(GREEN))      # BYBLOCK -> insert green

    def test_block_texts_placed(self):
        seats = [t for t in self.data["texts"] if t[4] == "SEAT"]
        self.assertEqual(len(seats), 2)
        rotated = [t for t in seats if abs(t[3] - 90) < 1e-6]
        self.assertEqual(len(rotated), 1)
        self.assertAlmostEqual(rotated[0][0], 19900, delta=1)
        self.assertAlmostEqual(rotated[0][1], 100, delta=1)


class ServeTests(unittest.TestCase):

    def test_build_clip_and_thin(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "t.dxf")
        _doc().saveas(path)
        npz, js = cadview.cache_paths(tmp, "tok", "source")
        cadview._build(path, npz, js, None, False)
        whole = cadview.lines_binary(tmp, "tok", "source")
        clipped = cadview.lines_binary(tmp, "tok", "source",
                                       bbox=(15000, -500, 25000, 2000))
        self.assertLess(len(clipped), len(whole))
        ncol = struct.unpack_from("<I", clipped, 0)[0]
        self.assertEqual(ncol, 2)                      # green + blue only
        # At 1 px = 5 m a 1 m chair is invisible and is dropped entirely.
        coarse = cadview.lines_binary(tmp, "tok", "source", px=5000)
        self.assertLess(len(coarse), len(whole))


if __name__ == "__main__":
    unittest.main()
