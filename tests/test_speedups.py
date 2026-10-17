# -*- coding: utf-8 -*-
"""The speed-ups of 2026-10-12: each one gives exactly what the slow way gave.

Every test here sets the new code against the old way of working the same
thing out (kept below as the reference) on the same input.

Run:  python -m unittest discover -s tests -v
"""

import math
import os
import random
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import dxfio  # noqa: E402
from Drawing_Analyzer.roomtrace import RoomTracer  # noqa: E402


def _box(x0, y0, x1, y1):
    return [(x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)]


def _same_trace(a, b):
    return (a.status == b.status and a.polygon == b.polygon and a.holes == b.holes
            and a.area_sqm == b.area_sqm and a.bbox == b.bbox)


class ExtendedTracerTests(unittest.TestCase):
    """Step 1: a leak re-trace adds its gap lines on top of the floor's
    tracer instead of indexing and rasterising every wall again."""

    def setUp(self):
        # two rooms side by side, a 900 mm doorway between them, and a third
        # room 30 m away
        segs = [(0, 0, 6000, 0), (6000, 0, 12000, 0), (12000, 0, 12000, 5000),
                (12000, 5000, 0, 5000), (0, 5000, 0, 0),
                (6000, 0, 6000, 2000), (6000, 2900, 6000, 5000)]
        segs += _box(40000, 0, 45000, 4000)
        self.segs = segs
        self.gap = [(6000, 2000, 6000, 2900)]

    def test_extended_traces_as_a_fresh_tracer(self):
        base = RoomTracer(self.segs, max_area_sqm=200)
        ext = base.extended(self.gap)
        fresh = RoomTracer(self.segs + self.gap, max_area_sqm=200)
        for x, y in ((3000, 2500), (9000, 2500), (42500, 2000)):
            self.assertTrue(_same_trace(ext.trace(x, y), fresh.trace(x, y)), (x, y))
        # the gap closed: the left room alone
        self.assertAlmostEqual(ext.trace(3000, 2500).area_sqm, 30.0, delta=0.5)

    def test_extended_twice_and_base_untouched(self):
        base = RoomTracer(self.segs, max_area_sqm=200)
        before = base.trace(3000, 2500)
        e1 = base.extended(self.gap[:0] + [(6000, 2000, 6000, 2400)])
        e2 = e1.extended([(6000, 2400, 6000, 2900)])
        fresh = RoomTracer(self.segs + [(6000, 2000, 6000, 2400), (6000, 2400, 6000, 2900)], max_area_sqm=200)
        self.assertTrue(_same_trace(e2.trace(3000, 2500), fresh.trace(3000, 2500)))
        self.assertTrue(_same_trace(base.trace(3000, 2500), before))
        self.assertTrue(_same_trace(base.trace(3000, 2500),
                                    RoomTracer(self.segs, max_area_sqm=200).trace(3000, 2500)))

    def test_results_are_copies(self):
        base = RoomTracer(self.segs, max_area_sqm=200)
        a = base.trace(42500, 2000)
        a.polygon.append((0, 0))
        self.assertNotEqual(a.polygon, base.trace(42500, 2000).polygon)


class RayMinTests(unittest.TestCase):
    """Step 4: the door-frame rays test every line near them at once."""

    def test_same_as_one_line_at_a_time(self):
        rnd = random.Random(7)
        for _ in range(300):
            segs = [(rnd.uniform(-2000, 2000), rnd.uniform(-2000, 2000),
                     rnd.uniform(-2000, 2000), rnd.uniform(-2000, 2000)) for _ in range(rnd.randint(0, 120))]
            segs += [(x, 0.0, x, rnd.uniform(-200, 200)) for x in (rnd.uniform(300, 1500) for _ in range(4))]
            grid = dr._LineGrid(segs)
            a = rnd.uniform(0, 2 * math.pi)
            dx, dy = math.cos(a), math.sin(a)
            lo, hi = 0.75 * 900, 1.25 * 900
            want = None
            for sg in grid.near(0.0, 0.0, dx * hi, dy * hi):
                t = dr._ray_hit(0.0, 0.0, dx, dy, sg)
                if t is not None and lo <= t <= hi and (want is None or t < want):
                    want = t
            self.assertEqual(grid.ray_min(0.0, 0.0, dx, dy, lo, hi), want)


def _zigzag_reference(E, F, strokes, segs, ux, uy, snap=12.0, max_edges=4):
    """_zigzag_path as it was: every stroke end tried from every point."""
    def side(p):
        return (p[0] - E[0]) * -uy + (p[1] - E[1]) * ux

    def near(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1]) <= snap

    ends = [((segs[s][0], segs[s][1]), (segs[s][2], segs[s][3]), s) for s in strokes]
    stack = [(E, [], [])]
    while stack:
        p, used, pts = stack.pop()
        if used and near(p, F):
            sides = [side(q) for q in pts[:-1]]
            if any(v > snap for v in sides) and any(v < -snap for v in sides):
                return used
            continue
        if len(used) >= max_edges:
            continue
        for a, b, s in ends:
            if s in used:
                continue
            for x, y in ((a, b), (b, a)):
                if near(p, x):
                    stack.append((y, used + [s], pts + [y]))
    return []


class ZigzagTests(unittest.TestCase):
    """The break-mark search: each point's neighbours worked out once."""

    def test_same_chain_as_before(self):
        rnd = random.Random(11)
        found = 0
        for _ in range(4000):
            E, F = (0.0, 0.0), (rnd.choice([100.0, 150.0, 200.0]), 0.0)
            pts = [E, F] + [(rnd.uniform(-20, 220), rnd.uniform(-60, 60)) for _ in range(rnd.randint(1, 6))]

            def jit(p):
                return (p[0] + rnd.choice([0, 0, rnd.uniform(-13, 13)]),
                        p[1] + rnd.choice([0, 0, rnd.uniform(-13, 13)]))
            segs = [jit(rnd.choice(pts)) + jit(rnd.choice(pts)) for _ in range(rnd.randint(1, 12))]
            strokes = list(range(len(segs)))
            rnd.shuffle(strokes)
            want = _zigzag_reference(E, F, strokes, segs, 1.0, 0.0)
            found += bool(want)
            self.assertEqual(dr._zigzag_path(E, F, strokes, segs, 1.0, 0.0), want)
        self.assertGreater(found, 100)


def _slit_reference(ends_of, targets, max_mm, touch_mm):
    """_slit_closers as it was: every nearby line buffered to test it."""
    from shapely.geometry import LineString, Point
    from shapely.strtree import STRtree
    from shapely.ops import nearest_points
    lines = [LineString([(t[0], t[1]), (t[2], t[3])]) for t in targets
             if math.hypot(t[2] - t[0], t[3] - t[1]) > 1.0]
    tree = STRtree(lines)
    out, seen = [], set()
    for sg in ends_of:
        own = LineString([(sg[0], sg[1]), (sg[2], sg[3])])
        if own.length <= 1.0:
            continue
        for ex, ey in ((sg[0], sg[1]), (sg[2], sg[3])):
            key = (round(ex), round(ey))
            if key in seen:
                continue
            seen.add(key)
            pt = Point(ex, ey)
            best, joined = None, False
            for k in tree.query(pt.buffer(max_mm)):
                ln = lines[int(k)]
                if ln.equals(own) or ln.buffer(0.5).contains(own):
                    continue
                d = ln.distance(pt)
                if d <= touch_mm:
                    joined = True
                    break
                if d < max_mm and (best is None or d < best[0]):
                    best = (d, ln)
            if joined or best is None:
                continue
            q = nearest_points(pt, best[1])[1]
            out.append((ex, ey, q.x, q.y))
    return out


class SlitClosersTests(unittest.TestCase):

    def test_same_closers_as_before(self):
        rnd = random.Random(5)
        for _ in range(60):
            segs = []
            for _ in range(rnd.randint(5, 60)):
                x, y = rnd.uniform(0, 3000), rnd.uniform(0, 3000)
                L = rnd.uniform(50, 1500)
                segs.append((x, y, x + L, y) if rnd.random() < 0.5 else (x, y, x, y + L))
            segs += [s for s in segs[:5]]                       # some twice
            segs += [(s[0] + 0.3, s[1], s[2] - 0.2, s[3]) for s in segs[:3]]   # inside another
            self.assertEqual(dr._slit_closers(segs, segs, 120.0, 5.0),
                             _slit_reference(segs, segs, 120.0, 5.0))


class CacheKeyTests(unittest.TestCase):
    """Step 6: the floor-pass key - fast on plain rows, still blind to set order."""

    def test_plain_rows_kept_as_they_are(self):
        row = (1.5, 2.0, "L", True, None)
        self.assertIs(dr._canonical(row), row)
        nested = [(1.0, (2.0, 3.0)), {"a": {3, 1, 2}}]
        self.assertEqual(dr._canonical(nested),
                         [(1.0, (2.0, 3.0)), ("d", [("'a'", ("s", [1, 2, 3]))])])

    def test_key_by_content(self):
        ja = {"segs": [(float(i), 2.0 * i, 3.0, 4.0, "W") for i in range(300)], "x": {(1.0, 2.0), (3.0, 4.0)}}
        jb = {"x": {(3.0, 4.0), (1.0, 2.0)}, "segs": [tuple(s) for s in ja["segs"]]}
        self.assertEqual(dr._pass_key(ja), dr._pass_key(jb))
        jc = dict(ja, segs=ja["segs"][:-1] + [(0.0, 0.0, 0.0, 0.5, "W")])
        self.assertNotEqual(dr._pass_key(ja), dr._pass_key(jc))


class DocCacheTests(unittest.TestCase):
    """The parsed drawing is known by its bytes: a copy is the same drawing."""

    def setUp(self):
        import ezdxf
        self.dir = tempfile.mkdtemp()
        doc = ezdxf.new()
        doc.modelspace().add_lwpolyline([(0, 0), (1000, 0), (1000, 1000)], close=True)
        self.a = os.path.join(self.dir, "a.dxf")
        doc.saveas(self.a)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_copy_same_key_edit_new_key(self):
        b = os.path.join(self.dir, "b.dxf")
        shutil.copyfile(self.a, b)
        self.assertEqual(dxfio._doc_key(self.a), dxfio._doc_key(b))
        with open(b, "ab") as fh:
            fh.write(b"\n")
        self.assertNotEqual(dxfio._doc_key(self.a), dxfio._doc_key(b))
        self.assertIsNone(dxfio._doc_key(os.path.join(self.dir, "none.dxf")))

    def test_written_drawing_read_back(self):
        import ezdxf
        doc = ezdxf.readfile(self.a)
        doc.modelspace().add_lwpolyline([(0, 0), (500, 0), (500, 500)], close=True, dxfattribs={"layer": "AI_X"})
        out = os.path.join(self.dir, "out.dxf")
        doc.saveas(out)
        dxfio.remember_written(doc, out)
        got, _msp, _u = dxfio.load_document(out)
        fresh = ezdxf.readfile(out)
        pts = lambda d: sorted((e.dxf.layer, tuple(tuple(p) for p in e.get_points("xy")))
                               for e in d.modelspace().query("LWPOLYLINE"))
        self.assertEqual(pts(got), pts(fresh))


class WalkTests(unittest.TestCase):
    """A block placed many times is looked up once per walk, same entities."""

    def test_iter_entities_same_as_before(self):
        import ezdxf
        doc = ezdxf.new()
        inner = doc.blocks.new("IN")
        inner.add_line((0, 0), (10, 0))
        outer = doc.blocks.new("OUT")
        outer.add_line((0, 0), (0, 10))
        for k in range(5):
            outer.add_blockref("IN", (k * 100, 0))
        msp = doc.modelspace()
        for k in range(7):
            msp.add_blockref("OUT", (0, k * 1000))
        msp.add_line((0, 0), (1, 1))
        got = [(e.dxf.handle, x.apply(e.dxf.start.x, e.dxf.start.y), d)
               for e, x, d in dxfio.iter_entities(doc, msp, "LINE")]
        self.assertEqual(len(got), 1 + 7 * (1 + 5))
        self.assertEqual(sum(1 for _h, _p, d in got if d == 2), 7 * 5)
        starts = sorted((round(p[0]), round(p[1])) for _h, p, _d in got)
        self.assertIn((400, 6000), starts)
        self.assertEqual(sum(1 for _ in dxfio.iter_inserts(doc, msp)), 7 + 7 * 5)


class MemoTests(unittest.TestCase):

    def test_fixture_layer_memo(self):
        for name in ("SAN-FIX", "toil-fix", "A-WALL", "", None, "P FIXT 2", "FIXTURE", "A-WALL"):
            self.assertEqual(dr._is_fixture_layer(name), dr._fixture_layer_test(name), name)

    def test_hatch_polys_and_union(self):
        from shapely.ops import unary_union
        hatches = [([(0, 0), (100, 0), (100, 100), (0, 100)], []),
                   ([(50, 50), (150, 50), (150, 150), (50, 150)], [])]
        a = dr._hatch_polys(hatches)
        b = dr._hatch_polys(hatches)
        self.assertEqual([g.wkb for g in a], [g.wkb for g in b])
        self.assertEqual(dr._hatch_union(a).wkb, unary_union(a).wkb)
        other = [([(0, 0), (10, 0), (10, 10)], [])]
        self.assertEqual(len(dr._hatch_polys(other)), 1)
        self.assertEqual(dr._hatch_union(b).wkb, unary_union(b).wkb)


if __name__ == "__main__":
    unittest.main()
