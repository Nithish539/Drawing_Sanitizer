# -*- coding: utf-8 -*-
"""Only the chosen floors are drawn, and several floors run at once.

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

WALL = "A-WALL"


def _wall(msp, x0, y0, x1, y1):
    msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True,
                       dxfattribs={"layer": WALL})


def _room(msp, x0, text):
    """A 5 x 4 m store, walled, with its name in the middle."""
    _wall(msp, x0 + 1000, 1000, x0 + 6200, 1200)
    _wall(msp, x0 + 1000, 5000, x0 + 6200, 5200)
    _wall(msp, x0 + 1000, 1000, x0 + 1200, 5200)
    _wall(msp, x0 + 6000, 1000, x0 + 6200, 5200)
    msp.add_text(text, height=250, dxfattribs={"insert": (x0 + 3500, 3000)})


def _sheet(path):
    """Three floor plates side by side, and a border drawn round them all."""
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for i in range(3):
        x0 = i * 70000.0
        msp.add_lwpolyline([(x0, 0), (x0 + 60000, 0), (x0 + 60000, 40000),
                            (x0, 40000)], close=True, dxfattribs={"layer": "A-FLOOR"})
        _room(msp, x0, "STORE")
    msp.add_lwpolyline([(-5000, -5000), (205000, -5000), (205000, 45000),
                        (-5000, 45000)], close=True, dxfattribs={"layer": "A-BORDER"})
    doc.saveas(path)


from Drawing_Analyzer import layernames as LN  # noqa: E402


def _boundaries(path):
    doc = ezdxf.readfile(path)
    out = []
    for e in doc.modelspace().query("LWPOLYLINE"):
        if LN.is_room_layer(e.dxf.layer):          # a room (not the floor outline, AI_FLOOR)
            xs = [p[0] for p in e.get_points("xy")]
            out.append(round(min(xs)))
    return sorted(out)


class ChosenFloorsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.src = os.path.join(cls.tmp, "sheet.dxf")
        _sheet(cls.src)
        cands, _u, _d = dr.get_floor_candidates(cls.src, min_area_sqm=1000,
                                                max_area_sqm=1e9)
        cls.cands = cands

    def test_border_is_flagged(self):
        border = max(self.cands, key=lambda c: c.area_sqm)
        self.assertEqual(border.layer, "A-BORDER")
        self.assertEqual(border.encloses, 3)
        self.assertTrue(all(c.encloses == 0 for c in self.cands if c is not border))

    def _run(self, picked, name):
        floors = [{"name": "F%d" % i, "polygon": c.polygon}
                  for i, c in enumerate(picked)]
        rows = dr.extract_texts(self.src, floors)
        out = os.path.join(self.tmp, name)
        # Without the per-floor cache, so each run really works the floors
        # out - in parallel or one by one, as the test asks.
        saved_cache = dr._PASS_CACHE_DIR
        dr.set_pass_cache_dir(None)
        try:
            stats = dr.annotate_dxf(self.src, out, floors, rows)
        finally:
            dr.set_pass_cache_dir(saved_cache)
        return stats, _boundaries(out)

    def test_only_chosen_floor_is_drawn(self):
        plates = sorted([c for c in self.cands if c.layer == "A-FLOOR"],
                        key=lambda c: min(p[0] for p in c.polygon))
        stats, xs = self._run([plates[1]], "one.dxf")
        self.assertEqual(stats["parallel_workers"], 0)
        self.assertEqual(len(xs), 1)
        self.assertTrue(70000 < xs[0] < 130000, xs)    # the middle plate only

    def test_parallel_matches_one_by_one(self):
        plates = sorted([c for c in self.cands if c.layer == "A-FLOOR"],
                        key=lambda c: min(p[0] for p in c.polygon))
        stats, xs = self._run([plates[0], plates[2]], "two.dxf")
        import os
        if (os.cpu_count() or 2) >= 3:                  # a 2-core box gets one worker
            self.assertGreaterEqual(stats["parallel_workers"], 2)
        saved = dr.PARALLEL_MIN_FLOORS
        dr.PARALLEL_MIN_FLOORS = 99                     # force one by one
        try:
            stats1, xs1 = self._run([plates[0], plates[2]], "two_seq.dxf")
        finally:
            dr.PARALLEL_MIN_FLOORS = saved
        self.assertEqual(stats1["parallel_workers"], 0)
        self.assertEqual(xs, xs1)
        self.assertEqual(len(xs), 2)                    # first and last plates
        self.assertTrue(xs[0] < 60000 and xs[1] > 140000, xs)
        self.assertEqual(stats["drawn_boundaries"], stats1["drawn_boundaries"])


if __name__ == "__main__":
    unittest.main()
