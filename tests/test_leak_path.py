# -*- coding: utf-8 -*-
"""A room's doorless entrance is not closed because its trace leaked
elsewhere: only the leak path is closed; slits (not doorways) are closed;
X strips and hatch-only walls block the fill; an unlabelled lobby beyond a
doorless opening is part of the room; of two overlapping X's the longer X
symbol keeps the overlap.

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402
from shapely.geometry import Polygon, box  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402

PLATE = [(0, 0), (40000, 0), (40000, 30000), (0, 30000)]


def _rect(msp, x0, y0, x1, y1, layer="A-WALL"):
    msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": layer})


def _text(msp, text, x, y):
    msp.add_mtext(text, dxfattribs={"insert": (x, y), "char_height": 150, "layer": "A-TEXT"})


def _run(build, **consts):
    """Draw a plan, run the text engine, read the areas back. consts:
    dxf_reader settings for this run."""
    old = {k: getattr(dr, k) for k in consts}
    cache = dr._PASS_CACHE_DIR
    dr.set_pass_cache_dir(None)                      # these settings are not in the cache key
    try:
        for k, v in consts.items():
            setattr(dr, k, v)
        tmp = tempfile.mkdtemp()
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        msp.add_lwpolyline(PLATE, close=True, dxfattribs={"layer": "A-FLOOR"})
        build(doc, msp)
        src = os.path.join(tmp, "in.dxf")
        doc.saveas(src)
        floors = [{"name": "F1", "polygon": PLATE}]
        rows = dr.extract_texts(src, floors)
        out = os.path.join(tmp, "out.dxf")
        stats = dr.annotate_dxf(src, out, floors, rows)
        return stats, dr.compute_room_areas(out, "AI_FLOOR")
    finally:
        for k, v in old.items():
            setattr(dr, k, v)
        dr.set_pass_cache_dir(cache)


def _named(areas, word):
    return [r for r in areas["rooms"] if word in (r["name"] or "").upper()]


def _rooms_plan(doc, msp):
    """MEETING ROOM 6 x 4 m (x 10000-16000, y 10000-14000) with a 1500 mm
    doorless entrance in its right wall to an unlabelled lobby (16200-18000),
    and a 1200 mm doorless opening in its top wall to a STORE above."""
    t = 200
    # outer walls of meeting room + lobby
    _rect(msp, 10000 - t, 10000 - t, 18000 + t, 10000)            # bottom, both
    _rect(msp, 10000 - t, 10000, 10000, 14000)                      # left
    _rect(msp, 18000, 10000, 18000 + t, 14000)                      # lobby right
    _rect(msp, 16200, 14000, 18000 + t, 14000 + t)                  # lobby top
    # the wall between room and lobby: entrance 11000-12500
    _rect(msp, 16000, 10000, 16200, 11000)
    _rect(msp, 16000, 12500, 16200, 14000 + t)
    # the room's top wall, opening 12000-13200 into the store
    _rect(msp, 10000 - t, 14000, 12000, 14000 + t)
    _rect(msp, 13200, 14000, 16000, 14000 + t)
    # the store 10000-16000 x 14200-17000
    _rect(msp, 10000 - t, 14000 + t, 10000, 17000)
    _rect(msp, 16000, 14000 + t, 16000 + t, 17000)
    _rect(msp, 10000 - t, 17000, 16000 + t, 17000 + t)
    _text(msp, "MEETING ROOM", 12000, 11500)
    _text(msp, "STORE", 13000, 16000)


class LeakPathTests(unittest.TestCase):

    def test_entrance_stays_open_when_the_leak_is_elsewhere(self):
        stats, areas = _run(_rooms_plan)
        room = _named(areas, "MEETING")
        self.assertEqual(len(room), 1, areas["rooms"])
        # the room and its lobby: 24 + 7.2 sq.m (and the entrance's depth)
        self.assertGreater(room[0]["area_sqm"], 30.0)
        self.assertLess(room[0]["area_sqm"], 32.5)
        store = _named(areas, "STORE")
        self.assertEqual(len(store), 1)
        self.assertLess(store[0]["area_sqm"], 18.0)              # the store alone
        self.assertGreater(stats.get("leak_gaps_closed", 0), 0)

    def test_lobby_joins_the_room_when_every_opening_is_closed(self):
        # no leak-path search: every doorless opening is closed, and the
        # unlabelled lobby shut off by the entrance joins the room again
        stats, areas = _run(_rooms_plan, LEAK_CLOSE_MAX=0)
        room = _named(areas, "MEETING")
        self.assertEqual(len(room), 1)
        self.assertGreater(room[0]["area_sqm"], 30.0)
        self.assertGreater(stats.get("leak_spaces_joined", 0), 0)


class SlitTests(unittest.TestCase):

    def test_a_195_mm_gap_is_a_slit_a_900_mm_doorway_is_not(self):
        floor_line = (0, 0, 5000, 0)
        near = (1000, 195, 1000, 3000)                            # ends 195 mm above the floor line
        door = (3000, 900, 3000, 3000)                            # ends 900 mm above it
        lines = dr._slit_closers([near, door], [floor_line, near, door])
        self.assertEqual(len(lines), 1)
        x1, y1, x2, y2 = lines[0]
        self.assertAlmostEqual(x1, 1000, delta=1)
        self.assertAlmostEqual(y1, 195, delta=1)
        self.assertAlmostEqual(y2, 0, delta=1)

    def test_an_end_meeting_a_wall_is_no_slit(self):
        self.assertEqual(dr._slit_closers([(1000, 0, 1000, 3000)], [(0, 0, 5000, 0), (1000, 0, 1000, 3000)]), [])

    def test_hatch_outline_is_wall(self):
        hp = dr._hatch_polys([([(0, 0), (200, 0), (200, 3000), (0, 3000)], [])])
        edges = dr._hatch_edges(hp)
        self.assertEqual(len(edges), 4)


class BlockingTests(unittest.TestCase):

    def test_an_x_strip_blocks_the_fill(self):
        def plan(doc, msp):
            t = 200
            _rect(msp, 10000 - t, 10000 - t, 16000 + t, 10000)
            _rect(msp, 10000 - t, 14000, 16000 + t, 14000 + t)
            _rect(msp, 10000 - t, 10000, 10000, 14000)
            _rect(msp, 16000, 10000, 16000 + t, 14000)
            # a 300 mm X strip along the right wall
            _rect(msp, 15700, 10000, 16000, 14000, "A3-CUTOUTS")
            msp.add_line((15700, 10000), (16000, 14000), dxfattribs={"layer": "A3-CUTOUTS"})
            msp.add_line((15700, 14000), (16000, 10000), dxfattribs={"layer": "A3-CUTOUTS"})
            _text(msp, "MEETING ROOM", 12000, 12000)
        _stats, areas = _run(plan)
        room = _named(areas, "MEETING")
        self.assertEqual(len(room), 1)
        self.assertAlmostEqual(room[0]["area_sqm"], 5.7 * 4.0, delta=0.3)

    def test_a_hatch_only_wall_keeps_two_rooms_apart(self):
        def plan(doc, msp):
            t = 200
            _rect(msp, 10000 - t, 10000 - t, 18000 + t, 10000)
            _rect(msp, 10000 - t, 14000, 18000 + t, 14000 + t)
            _rect(msp, 10000 - t, 10000, 10000, 14000)
            _rect(msp, 18000, 10000, 18000 + t, 14000)
            # the dividing wall is a hatch only: no lines
            h = msp.add_hatch(dxfattribs={"layer": "A-WALL-HATCH"})
            h.paths.add_polyline_path([(13900, 10000), (14100, 10000), (14100, 14000), (13900, 14000)], is_closed=True)
            _text(msp, "MEETING ROOM", 12000, 12000)
            _text(msp, "STORE", 16000, 12000)
        _stats, areas = _run(plan)
        room = _named(areas, "MEETING")
        store = _named(areas, "STORE")
        self.assertEqual(len(room), 1)
        self.assertAlmostEqual(room[0]["area_sqm"], 3.9 * 4.0, delta=0.3)
        self.assertEqual(len(store), 1)
        self.assertAlmostEqual(store[0]["area_sqm"], 3.9 * 4.0, delta=0.3)


class LongerXTests(unittest.TestCase):

    def test_longer_x_keeps_the_overlap(self):
        import collections
        strip = {"bbox": (0, 0, 2760, 300), "poly": None, "diag_len": 2776.0, "region": False, "cx": 1380, "cy": 150}
        ell = {"bbox": (0, 0, 2760, 1270), "region": True, "diag_len": 2776.0, "cx": 1380, "cy": 635,
               "poly": [(0, 0), (2760, 0), (2760, 1270), (2460, 1270), (2460, 300), (0, 300)]}
        out = dr._settle_x_overlaps([strip, ell], collections.Counter())
        self.assertEqual(len(out), 2)
        rest = [v for v in out if v.get("region")][0]
        g = Polygon(rest["poly"]) if rest.get("poly") else box(*rest["bbox"])
        self.assertAlmostEqual(g.area / 1e6, 0.29, delta=0.01)          # the narrow strip is left
        self.assertFalse(g.intersection(box(0, 0, 2760, 300)).area > 1e3)


if __name__ == "__main__":
    unittest.main()


class DoorFrameTests(unittest.TestCase):
    """A doorway closed by a line drawn across it on the wall's face (a door
    frame on a shaft layer, in an unnamed block)."""

    # a 200 mm wall along y 0..200 with a 750 mm opening at x 1000..1750
    WALLS = [(0, 0, 1000, 0), (1750, 0, 3000, 0), (0, 200, 1000, 200), (1750, 200, 3000, 200)]

    def test_frame_lines_close_the_doorway(self):
        frames = [(1000, 0, 1750, 0, "EL-SHAFT"), (1000, 200, 1750, 200, "EL-SHAFT")]
        out = dr._face_line_closers(frames, self.WALLS)
        self.assertEqual(len(out), 2)
        self.assertEqual(sorted((round(o[0]), round(o[2])) for o in out), [(1000, 1750), (1000, 1750)])

    def test_a_long_line_past_the_opening_closes_nothing(self):
        overlay = [(-2000, 0, 5000, 0, "3.5 TOILETS")]
        self.assertEqual(dr._face_line_closers(overlay, self.WALLS), [])

    def test_a_short_line_in_the_opening_closes_nothing(self):
        stub = [(1300, 0, 1450, 0, "A-FURN")]
        self.assertEqual(dr._face_line_closers(stub, self.WALLS), [])

    def test_never_one_face_only_in_front_of_a_lift(self):
        lift = box(1000, 200, 1750, 2850)                         # a lift behind the wall
        one = [(1000, 0, 1750, 0, "A-LIFT-SILL")]
        self.assertEqual(dr._face_line_closers(one, self.WALLS, blockers=[lift]), [])
        both = one + [(1000, 200, 1750, 200, "A-LIFT-SILL")]
        self.assertEqual(len(dr._face_line_closers(both, self.WALLS, blockers=[lift])), 2)


class CorridorBoxTests(unittest.TestCase):
    """The space between two parallel walls runs to its end wall and to
    where its side walls stop - not to the last 250 mm step."""

    def test_runs_to_the_end_wall_and_the_side_wall_ends(self):
        walls = [(0, 0, 10000, 0), (0, 4000, 10000, 4000), (0, 0, 0, 4000)]
        bx = dr._corridor_box(4666, 2000, walls)
        self.assertIsNotNone(bx)
        self.assertAlmostEqual(bx[0], 0, delta=2)                  # the end wall, 4666 mm away
        self.assertAlmostEqual(bx[2], 10000, delta=2)              # where the side walls stop
        self.assertAlmostEqual(bx[1], 0, delta=2)
        self.assertAlmostEqual(bx[3], 4000, delta=2)
