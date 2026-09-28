# -*- coding: utf-8 -*-
"""Tests for real room-boundary tracing.

The point of the tracer is that it measures the room the walls actually
describe, not that room's bounding box. Most of these compare a traced
area against a figure worked out by hand, and several compare it against
what the old four-ray method would have returned for the same shape.

Run:  python -m unittest discover -s tests -v
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Drawing_Analyzer.roomtrace import RoomTracer, trace_rooms  # noqa: E402


def box(x0, y0, x1, y1):
    """Four wall segments enclosing an axis-aligned rectangle."""
    return [(x0, y0, x1, y0), (x1, y0, x1, y1),
            (x1, y1, x0, y1), (x0, y1, x0, y0)]


class TestBasicShapes(unittest.TestCase):
    def test_rectangle_is_exact(self):
        # Snapping puts edges back on the real wall lines, so a straight-
        # walled room is exact rather than merely close to the grid.
        r = RoomTracer(box(0, 0, 6000, 4000), cell_mm=50).trace(3000, 2000)
        self.assertEqual(r.status, "ok")
        self.assertAlmostEqual(r.area_sqm, 24.0, places=6)
        self.assertEqual(len(r.polygon), 4)

    def test_l_shape(self):
        # 6x6 m with a 2x2 m bite: 32 sq.m. Its bounding box is 36, which
        # is what the old four-ray method would have produced.
        walls = [(0, 0, 6000, 0), (6000, 0, 6000, 4000),
                 (6000, 4000, 4000, 4000), (4000, 4000, 4000, 6000),
                 (4000, 6000, 0, 6000), (0, 6000, 0, 0)]
        r = RoomTracer(walls, cell_mm=50).trace(1000, 1000)
        self.assertAlmostEqual(r.area_sqm, 32.0, places=6)
        self.assertEqual(len(r.polygon), 6)
        bbox_area = ((r.bbox[2] - r.bbox[0]) * (r.bbox[3] - r.bbox[1])) / 1e6
        self.assertAlmostEqual(bbox_area, 36.0, places=6)
        self.assertLess(r.area_sqm, bbox_area)

    def test_angled_wall(self):
        # Square with a 45-degree corner cut: 24 - 2 = 22 sq.m.
        walls = [(0, 0, 6000, 0), (6000, 0, 6000, 4000),
                 (6000, 4000, 2000, 4000), (2000, 4000, 0, 2000),
                 (0, 2000, 0, 0)]
        # seal_openings_mm=0: this measures how closely the fill follows
        # an angled wall, which is exactly what sealing trades away.
        r = RoomTracer(walls, cell_mm=25, seal_openings_mm=0).trace(3000, 1500)
        self.assertAlmostEqual(r.area_sqm, 22.0, places=6)

    def test_narrow_corridor(self):
        r = RoomTracer(box(0, 0, 20000, 1500), cell_mm=50).trace(10000, 750)
        self.assertAlmostEqual(r.area_sqm, 30.0, places=6)

    def test_seed_sitting_on_a_wall_still_finds_the_room(self):
        # A label snapped onto a wall used to seed outside the enclosure.
        r = RoomTracer(box(0, 0, 6000, 4000), cell_mm=50).trace(0, 2000)
        self.assertEqual(r.status, "ok")
        self.assertAlmostEqual(r.area_sqm, 24.0, places=6)


class TestHoles(unittest.TestCase):
    def test_interior_column_is_deducted(self):
        # 6x4 m room with a 400x400 column: 24.00 gross, 23.84 net.
        walls = box(0, 0, 6000, 4000) + box(2800, 1800, 3200, 2200)
        r = RoomTracer(walls, cell_mm=25).trace(500, 500)
        self.assertEqual(len(r.holes), 1)
        self.assertAlmostEqual(r.gross_area_sqm, 24.0, places=6)
        self.assertAlmostEqual(r.area_sqm, 23.84, places=6)

    def test_two_columns(self):
        walls = (box(0, 0, 8000, 6000) + box(2000, 2000, 2400, 2400)
                 + box(5000, 3000, 5400, 3400))
        r = RoomTracer(walls, cell_mm=25).trace(500, 500)
        self.assertEqual(len(r.holes), 2)
        self.assertAlmostEqual(r.area_sqm, 48.0 - 0.16 - 0.16, places=6)


class TestCurves(unittest.TestCase):
    @staticmethod
    def _semicircular_end(steps=180):
        walls = [(0, 0, 6000, 0), (6000, 4000, 0, 4000), (0, 4000, 0, 0)]
        cx, cy, rad = 6000.0, 2000.0, 2000.0
        prev = None
        for i in range(steps + 1):
            a = math.radians(-90 + 180.0 * i / steps)
            p = (cx + rad * math.cos(a), cy + rad * math.sin(a))
            if prev:
                walls.append((prev[0], prev[1], p[0], p[1]))
            prev = p
        return walls, (6000 * 4000 + math.pi * rad * rad / 2) / 1e6

    def test_curved_end_within_half_a_percent(self):
        walls, expected = self._semicircular_end()
        r = RoomTracer(walls, cell_mm=50).trace(3000, 2000)
        self.assertEqual(r.status, "ok")
        self.assertLess(abs(r.area_sqm - expected) / expected, 0.005)

    def test_finer_grid_converges(self):
        walls, expected = self._semicircular_end()
        coarse = RoomTracer(walls, cell_mm=50, seal_openings_mm=0).trace(
            3000, 2000)
        fine = RoomTracer(walls, cell_mm=10, seal_openings_mm=0).trace(
            3000, 2000)
        self.assertLess(abs(fine.area_sqm - expected),
                        abs(coarse.area_sqm - expected))
        self.assertLess(abs(fine.area_sqm - expected) / expected, 0.001)


class TestSealedOpenings(unittest.TestCase):
    """Openings narrower than the seal are bridged, so the outline runs
    along the wall past them rather than reaching in."""

    def _room_with_bays(self):
        """A 6 x 4 m room with three 700 mm bays hanging off the top wall -
        the shape a row of WC cubicles makes."""
        walls = [(0, 0, 6000, 0), (6000, 0, 6000, 4000),
                 (0, 4000, 0, 0)]
        # top wall in pieces, leaving three 700 mm openings
        xs = [(0, 1000), (1700, 2650), (3350, 4300), (5000, 6000)]
        for x0, x1 in xs:
            walls.append((x0, 4000, x1, 4000))
        # the bays themselves, 1 m deep
        for x in (1000, 1700, 2650, 3350, 4300, 5000):
            walls.append((x, 4000, x, 5000))
        walls.append((1000, 5000, 1700, 5000))
        walls.append((2650, 5000, 3350, 5000))
        walls.append((4300, 5000, 5000, 5000))
        return walls

    def test_bays_are_left_out(self):
        walls = self._room_with_bays()
        sealed = RoomTracer(walls, cell_mm=50).trace(3000, 2000)
        self.assertEqual(sealed.status, "ok")
        self.assertAlmostEqual(sealed.area_sqm, 24.0, delta=0.6)

    def test_without_sealing_the_bays_are_followed(self):
        walls = self._room_with_bays()
        raw = RoomTracer(walls, cell_mm=50, seal_openings_mm=0).trace(
            3000, 2000)
        self.assertEqual(raw.status, "ok")
        # three 700 x 1000 bays on top of the 24 sq.m room
        self.assertGreater(raw.area_sqm, 25.5)

    def test_sealing_simplifies_the_outline(self):
        walls = self._room_with_bays()
        sealed = RoomTracer(walls, cell_mm=50).trace(3000, 2000)
        raw = RoomTracer(walls, cell_mm=50, seal_openings_mm=0).trace(
            3000, 2000)
        self.assertLess(len(sealed.polygon), len(raw.polygon))

    def test_a_plain_room_is_unaffected(self):
        """Sealing must cost nothing where there is nothing to seal."""
        r = RoomTracer(box(0, 0, 6000, 4000), cell_mm=50).trace(3000, 2000)
        self.assertAlmostEqual(r.area_sqm, 24.0, places=6)

    def test_a_corridor_narrower_than_the_seal_still_traces(self):
        """A 700 mm space would vanish under the thickening, so the fill
        falls back to the walls as drawn rather than losing the room."""
        r = RoomTracer(box(0, 0, 8000, 700), cell_mm=50).trace(4000, 350)
        self.assertEqual(r.status, "ok")
        self.assertAlmostEqual(r.area_sqm, 5.6, delta=0.3)


class TestFailureModes(unittest.TestCase):
    def test_gap_in_wall_is_reported_as_a_leak(self):
        # An unsealed doorway must be detected, not silently swallow the
        # rest of the floor. The caller falls back to the ray-cast box.
        walls = [(0, 0, 6000, 0), (6000, 0, 6000, 4000),
                 (6000, 4000, 0, 4000), (0, 4000, 0, 2500), (0, 1500, 0, 0)]
        r = RoomTracer(walls, cell_mm=50, max_area_sqm=100).trace(3000, 2000)
        self.assertEqual(r.status, "leaked")
        self.assertFalse(r.ok)

    def test_sealed_doorway_traces_normally(self):
        walls = [(0, 0, 6000, 0), (6000, 0, 6000, 4000),
                 (6000, 4000, 0, 4000), (0, 4000, 0, 2500), (0, 1500, 0, 0),
                 (0, 1500, 0, 2500)]           # the virtual closing line
        r = RoomTracer(walls, cell_mm=50).trace(3000, 2000)
        self.assertEqual(r.status, "ok")
        self.assertAlmostEqual(r.area_sqm, 24.0, places=6)

    def test_area_cap_stops_a_runaway_fill(self):
        walls = box(0, 0, 60000, 40000)        # 2400 sq.m
        r = RoomTracer(walls, cell_mm=100, max_area_sqm=50).trace(30000, 20000)
        self.assertEqual(r.status, "leaked")

    def test_no_walls_at_all(self):
        self.assertEqual(RoomTracer([], cell_mm=50).trace(0, 0).status,
                         "no_walls")

    def test_seed_far_outside_the_drawing(self):
        r = RoomTracer(box(0, 0, 6000, 4000), cell_mm=50).trace(9e6, 9e6)
        self.assertFalse(r.ok)

    def test_far_away_geometry_does_not_disable_tracing(self):
        """The regression that made every room a rectangle.

        One grid over the whole drawing came to 575 million cells on a
        real file and blew the memory guard, so tracing silently turned
        itself off. Windows are local now.
        """
        walls = box(0, 0, 6000, 4000) + [(1200000, 1080000, 1224000, 1089000)]
        r = RoomTracer(walls, cell_mm=50).trace(3000, 2000)
        self.assertEqual(r.status, "ok")
        self.assertAlmostEqual(r.area_sqm, 24.0, places=6)

    def test_large_hall_widens_the_window(self):
        """Nothing within 4 m of the centre of a 40 x 25 m hall."""
        r = RoomTracer(box(0, 0, 40000, 25000), cell_mm=50,
                       max_area_sqm=1500).trace(20000, 12500)
        self.assertEqual(r.status, "ok")
        self.assertAlmostEqual(r.area_sqm, 1000.0, places=4)


class TestMultiRoom(unittest.TestCase):
    def test_grid_of_rooms_sums_to_the_floor_plate(self):
        # 4 x 3 rooms inside a 24 x 12 m plate, walls with no thickness,
        # so the rooms should tile the plate exactly.
        walls = box(0, 0, 24000, 12000)
        for i in range(1, 4):
            walls.append((i * 6000, 0, i * 6000, 12000))
        for j in range(1, 3):
            walls.append((0, j * 4000, 24000, j * 4000))
        seeds = [(i * 6000 + 3000, j * 4000 + 2000)
                 for i in range(4) for j in range(3)]
        results = trace_rooms(walls, seeds, cell_mm=50)
        self.assertTrue(all(r.status == "ok" for r in results))
        self.assertEqual(len(results), 12)
        for r in results:
            self.assertAlmostEqual(r.area_sqm, 24.0, places=6)
        self.assertAlmostEqual(sum(r.area_sqm for r in results), 288.0,
                               places=4)

    def test_one_tracer_serves_many_seeds(self):
        walls = box(0, 0, 12000, 4000) + [(6000, 0, 6000, 4000)]
        tracer = RoomTracer(walls, cell_mm=50)
        left = tracer.trace(3000, 2000)
        right = tracer.trace(9000, 2000)
        self.assertAlmostEqual(left.area_sqm, 24.0, places=6)
        self.assertAlmostEqual(right.area_sqm, 24.0, places=6)


class TestIrregularShapes(unittest.TestCase):
    """The shapes a bounding box cannot represent."""

    def test_triangle(self):
        walls = [(0, 0, 8000, 0), (8000, 0, 4000, 6000), (4000, 6000, 0, 0)]
        r = RoomTracer(walls, cell_mm=25, seal_openings_mm=0).trace(4000, 1500)
        self.assertEqual(r.status, "ok")
        # True area 24 sq.m; its bounding box is 48, so the old ray-cast
        # boundary was 100% out.
        self.assertLess(abs(r.area_sqm - 24.0) / 24.0, 0.01)

    def test_triangle_apex_is_trimmed_by_sealing(self):
        """The default seals openings, and an apex narrower than the seal
        is one. It costs a few percent here and buys a toilet outline that
        runs along the wall instead of into every cubicle."""
        walls = [(0, 0, 8000, 0), (8000, 0, 4000, 6000), (4000, 6000, 0, 0)]
        r = RoomTracer(walls, cell_mm=25).trace(4000, 1500)
        self.assertEqual(r.status, "ok")
        self.assertLess(abs(r.area_sqm - 24.0) / 24.0, 0.05)

    def test_trapezoid_with_a_30_degree_wall(self):
        rise = 10000 * math.tan(math.radians(30))
        walls = [(0, 0, 10000, 0), (10000, 0, 10000, 3000),
                 (10000, 3000, 0, 3000 + rise), (0, 3000 + rise, 0, 0)]
        expected = (3000 + (3000 + rise)) / 2 * 10000 / 1e6
        r = RoomTracer(walls, cell_mm=25).trace(3000, 2000)
        self.assertEqual(r.status, "ok")
        self.assertLess(abs(r.area_sqm - expected) / expected, 0.01)

    def test_octagonal_room(self):
        c = 1500.0
        pts = [(c, 0), (6000 - c, 0), (6000, c), (6000, 6000 - c),
               (6000 - c, 6000), (c, 6000), (0, 6000 - c), (0, c)]
        walls = [(pts[i][0], pts[i][1], pts[(i + 1) % 8][0],
                  pts[(i + 1) % 8][1]) for i in range(8)]
        expected = (36.0 - 4 * 0.5 * (c / 1000.0) ** 2)
        r = RoomTracer(walls, cell_mm=25).trace(3000, 3000)
        self.assertLess(abs(r.area_sqm - expected) / expected, 0.01)
        self.assertGreaterEqual(len(r.polygon), 8)


class TestSnapping(unittest.TestCase):
    def test_snapping_off_still_close(self):
        """Without snapping the grid bias shows, and it is always low."""
        tracer = RoomTracer(box(0, 0, 6000, 4000), cell_mm=50)
        raw = tracer.trace(3000, 2000, snap=False)
        snapped = tracer.trace(3000, 2000, snap=True)
        self.assertLess(raw.area_sqm, 24.0)
        self.assertGreater(raw.area_sqm, 22.5)
        self.assertAlmostEqual(snapped.area_sqm, 24.0, places=6)

    def test_snap_does_not_move_vertices_wildly(self):
        # A room with no nearby parallel wall for one edge must not have
        # that edge dragged onto a distant one.
        walls = box(0, 0, 6000, 4000)
        r = RoomTracer(walls, cell_mm=50).trace(3000, 2000)
        for x, y in r.polygon:
            self.assertTrue(-200 <= x <= 6200)
            self.assertTrue(-200 <= y <= 4200)


class TestOutlineIsAlwaysValid(unittest.TestCase):
    """The outline written to the DXF is a simple polygon."""

    def test_pinch_point_keeps_outline_and_hole_apart(self):
        from Drawing_Analyzer.roomtrace import _boundary_loops
        # A 4 x 4 block of cells less its corner cell and the cell
        # diagonally inside it: the hole touches the outline at one node.
        nx = 10
        filled = ({y * nx + x for x in range(4) for y in range(4)}
                  - {0, 1 * nx + 1})
        loops = _boundary_loops(filled, nx)
        self.assertEqual(sorted(len(lp) for lp in loops), [4, 16])
        for lp in loops:
            self.assertEqual(len(lp), len(set(lp)))     # no node twice

    def test_bowtie_is_cut_off(self):
        from Drawing_Analyzer.roomtrace import _self_intersects, _untangle
        # A square whose top edge doubles back across itself near a corner.
        poly = [(0, 0), (1000, 0), (1000, 1000), (520, 1000),
                (540, 1020), (500, 980), (0, 1000)]
        self.assertTrue(_self_intersects(poly))
        fixed = _untangle(poly)
        self.assertFalse(_self_intersects(fixed))
        self.assertGreater(len(fixed), 3)

    def test_column_centre_line_leaves_no_spike(self):
        # A room with a column in its top wall and the column's centre
        # line poking 500 mm into the room.
        walls = [(0, 0, 6000, 0), (6000, 0, 6000, 4000), (6000, 4000, 0, 4000),
                 (0, 4000, 0, 0),
                 (2800, 3700, 3200, 3700), (3200, 3700, 3200, 4000),
                 (2800, 4000, 2800, 3700),
                 (3000, 3200, 3000, 3700)]         # the stray line
        r = RoomTracer(walls, cell_mm=50).trace(1500, 1500)
        self.assertEqual(r.status, "ok")
        from Drawing_Analyzer.roomtrace import _self_intersects
        self.assertFalse(_self_intersects(r.polygon))


if __name__ == "__main__":
    unittest.main(verbosity=2)
