# -*- coding: utf-8 -*-
"""Reference tests for the pure-geometry and derivation layers.

The engine depends on roughly 25 tuned thresholds, and until now nothing
checked that a change to one of them left the arithmetic intact. These
cover the parts that can be verified by hand, without a CAD file.

Run:  python -m unittest discover -s tests -v
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Drawing_Analyzer.geometry import (BandIndex, arc_points,  # noqa: E402
                                       bulge_to_arc_points, polygon_centroid,
                                       polygon_perimeter_m, polyline_length_m,
                                       shoelace_area_sqm)
from Drawing_Analyzer.quantities import (compare_revisions,  # noqa: E402
                                         deck_sheet_and_studs, efficiency_kpis,
                                         extraction_qa, geometry_health,
                                         reconciliation)
from Drawing_Analyzer.units import detect_units, units_from_code  # noqa: E402


class FakeHeader(dict):
    def get(self, k, default=None):
        return dict.get(self, k, default)


class FakeDoc:
    def __init__(self, insunits=None):
        self.header = FakeHeader()
        if insunits is not None:
            self.header["$INSUNITS"] = insunits


# -- geometry ---------------------------------------------------------------
class TestArea(unittest.TestCase):
    def test_rectangle_10m_by_5m(self):
        # 10000 x 5000 mm = 50 sq.m
        pts = [(0, 0), (10000, 0), (10000, 5000), (0, 5000)]
        self.assertAlmostEqual(shoelace_area_sqm(pts), 50.0, places=6)

    def test_area_is_orientation_independent(self):
        pts = [(0, 0), (10000, 0), (10000, 5000), (0, 5000)]
        self.assertAlmostEqual(shoelace_area_sqm(pts),
                               shoelace_area_sqm(list(reversed(pts))))

    def test_l_shaped_room(self):
        # 6x6 m square with a 2x2 m bite out of one corner = 36 - 4 = 32
        pts = [(0, 0), (6000, 0), (6000, 4000), (4000, 4000),
               (4000, 6000), (0, 6000)]
        self.assertAlmostEqual(shoelace_area_sqm(pts), 32.0, places=6)

    def test_degenerate(self):
        self.assertEqual(shoelace_area_sqm([(0, 0), (1, 1)]), 0.0)


class TestPerimeter(unittest.TestCase):
    def test_rectangle(self):
        pts = [(0, 0), (10000, 0), (10000, 5000), (0, 5000)]
        self.assertAlmostEqual(polygon_perimeter_m(pts), 30.0, places=6)

    def test_duplicated_closing_vertex_not_double_counted(self):
        pts = [(0, 0), (10000, 0), (10000, 5000), (0, 5000), (0, 0)]
        self.assertAlmostEqual(polygon_perimeter_m(pts), 30.0, places=6)

    def test_open_polyline_length(self):
        pts = [(0, 0), (3000, 0), (3000, 4000)]
        self.assertAlmostEqual(polyline_length_m(pts), 7.0, places=6)

    def test_closed_adds_return_leg(self):
        pts = [(0, 0), (3000, 0), (3000, 4000)]
        self.assertAlmostEqual(polyline_length_m(pts, closed=True), 12.0,
                               places=6)


class TestCurves(unittest.TestCase):
    def test_semicircle_bay_adds_area(self):
        # DXF convention: bulge = tan(theta/4), positive = counter-clockwise
        # from p1 to p2. Walking p1(0,0) -> p2(0,4000), a NEGATIVE bulge
        # bows west, i.e. outside a square occupying x in [0, 4000].
        # 4x4 m square + semicircular bay of radius 2 m = 16 + pi*2^2/2.
        p1, p2 = (0.0, 0.0), (0.0, 4000.0)
        mid = bulge_to_arc_points(p1, p2, -1.0, sagitta_tol=1.0)
        poly = [p2, (4000.0, 4000.0), (4000.0, 0.0), p1] + mid
        self.assertAlmostEqual(shoelace_area_sqm(poly),
                               16.0 + math.pi * 4.0 / 2.0, delta=0.01)

    def test_semicircle_bite_removes_area(self):
        # The same edge with the opposite bulge bows into the square.
        p1, p2 = (0.0, 0.0), (0.0, 4000.0)
        mid = bulge_to_arc_points(p1, p2, 1.0, sagitta_tol=1.0)
        poly = [p2, (4000.0, 4000.0), (4000.0, 0.0), p1] + mid
        self.assertAlmostEqual(shoelace_area_sqm(poly),
                               16.0 - math.pi * 4.0 / 2.0, delta=0.01)

    def test_bulge_direction_flips(self):
        # p1(0,0) -> p2(1000,0) with bulge +1 is a CCW semicircle about
        # (500, 0). Counter-clockwise from the west point of a circle runs
        # to the south point, so the arc sits below the chord.
        p1, p2 = (0.0, 0.0), (1000.0, 0.0)
        ccw = bulge_to_arc_points(p1, p2, 1.0, sagitta_tol=0.5)
        cw = bulge_to_arc_points(p1, p2, -1.0, sagitta_tol=0.5)
        self.assertAlmostEqual(min(p[1] for p in ccw), -500.0, delta=1.0)
        self.assertAlmostEqual(max(p[1] for p in cw), 500.0, delta=1.0)
        for p in ccw:
            self.assertAlmostEqual(math.hypot(p[0] - 500.0, p[1]), 500.0,
                                   places=6)

    def test_quarter_arc_radius_is_exact(self):
        b = math.tan(math.radians(90) / 4)
        pts = bulge_to_arc_points((0.0, 0.0), (1000.0, 1000.0), b,
                                  sagitta_tol=0.05)
        radii = [math.hypot(p[0] - 0.0, p[1] - 1000.0) for p in pts]
        for r in radii:
            self.assertAlmostEqual(r, 1000.0, places=6)

    def test_major_arc_270_degrees(self):
        # Arc plus its closing chord = 3/4 sector + the centre triangle.
        b = math.tan(math.radians(270) / 4)
        mid = bulge_to_arc_points((0.0, 0.0), (1000.0, 0.0), b,
                                  sagitta_tol=0.25)
        r = 1000.0 / (2 * math.sin(math.radians(135)))
        expected = (0.75 * math.pi * r * r + 0.5 * r * r) / 1e6
        self.assertAlmostEqual(shoelace_area_sqm([(0.0, 0.0)] + mid +
                                                 [(1000.0, 0.0)]),
                               expected, delta=0.005)

    def test_zero_bulge_is_straight(self):
        self.assertEqual(bulge_to_arc_points((0, 0), (100, 0), 0.0), [])

    def test_full_circle_from_arc_points(self):
        pts = arc_points(0, 0, 1000.0, 0.0, 360.0, sagitta_tol=0.5)
        area = shoelace_area_sqm(pts[:-1])
        self.assertAlmostEqual(area, math.pi * 1.0 ** 2, delta=0.005)


class TestCentroid(unittest.TestCase):
    def test_rectangle_centre(self):
        cx, cy = polygon_centroid([(0, 0), (10, 0), (10, 4), (0, 4)])
        self.assertAlmostEqual(cx, 5.0)
        self.assertAlmostEqual(cy, 2.0)


class TestBandIndex(unittest.TestCase):
    """The index must return the same walls a linear scan would."""

    def setUp(self):
        # A 10m x 10m box: two vertical walls, two horizontal walls.
        self.segs = [
            (0, 0, 0, 10000),          # left  (vertical)
            (10000, 0, 10000, 10000),  # right (vertical)
            (0, 0, 10000, 0),          # bottom (horizontal)
            (0, 10000, 10000, 10000),  # top (horizontal)
            (2000, 2000, 2100, 2100),  # 45-degree stray: must be ignored
        ]
        self.idx = BandIndex(self.segs, band=2000.0)

    def test_slanted_segment_excluded(self):
        all_indexed = set()
        for bucket in list(self.idx.vertical.values()) + \
                      list(self.idx.horizontal.values()):
            all_indexed.update(bucket)
        self.assertNotIn((2000, 2000, 2100, 2100), all_indexed)

    def test_finds_both_vertical_walls_for_a_mid_ray(self):
        found = self.idx.crossing_horizontal_ray(5000)
        self.assertEqual(len(found), 2)

    def test_finds_both_horizontal_walls(self):
        found = self.idx.crossing_vertical_ray(5000)
        self.assertEqual(len(found), 2)

    def test_matches_linear_scan_on_random_rays(self):
        import random
        random.seed(7)
        for _ in range(200):
            py = random.uniform(-1000, 11000)
            fast = {s for s in self.idx.crossing_horizontal_ray(py)
                    if min(s[1], s[3]) <= py <= max(s[1], s[3])}
            slow = set()
            for s in self.segs:
                dx, dy = abs(s[2] - s[0]), abs(s[3] - s[1])
                if dx > 0 and dy > 0 and max(dx, dy) / min(dx, dy) < 6.0:
                    continue
                if dy > dx and min(s[1], s[3]) <= py <= max(s[1], s[3]):
                    slow.add(s)
            self.assertEqual(fast, slow)


# -- units ------------------------------------------------------------------
class TestUnits(unittest.TestCase):
    def test_millimetres(self):
        u = units_from_code(4)
        self.assertEqual(u.mm_per_unit, 1.0)
        self.assertTrue(u.is_identity)

    def test_metres_scale(self):
        u = units_from_code(6)
        self.assertEqual(u.mm_per_unit, 1000.0)
        self.assertEqual(u.to_mm(2.5), 2500.0)
        self.assertEqual(u.from_mm(2500.0), 2.5)

    def test_metre_drawing_gives_correct_area(self):
        """A 10x5 m room drawn in metres must still measure 50 sq.m."""
        u = units_from_code(6)
        raw = [(0, 0), (10, 0), (10, 5), (0, 5)]
        mm = [u.point_to_mm(x, y) for x, y in raw]
        self.assertAlmostEqual(shoelace_area_sqm(mm), 50.0, places=6)

    def test_unset_header_falls_back_and_flags_assumption(self):
        u = detect_units(FakeDoc())
        self.assertEqual(u.mm_per_unit, 1.0)
        self.assertTrue(u.assumed)

    def test_header_detection(self):
        self.assertEqual(detect_units(FakeDoc(6)).mm_per_unit, 1000.0)

    def test_override_beats_header(self):
        u = detect_units(FakeDoc(4), override="m")
        self.assertEqual(u.mm_per_unit, 1000.0)
        self.assertTrue(u.overridden)

    def test_inches(self):
        self.assertAlmostEqual(units_from_code(1).mm_per_unit, 25.4)


# -- derived quantities -----------------------------------------------------
def sample_areas():
    return {
        "floor_label": "A-FLOOR",
        "floor_area_sqm": 1000.0,
        "floor_perimeter_m": 130.0,
        "rooms": [
            {"sr_no": 1, "name": "OFFICE 1", "area_sqm": 400.0,
             "perimeter_m": 82.0, "family": "Office", "layer": "OFFICE_BOUNDARY"},
            {"sr_no": 2, "name": "TOILET", "area_sqm": 60.0,
             "perimeter_m": 32.0, "family": "Toilet", "layer": "TOILET_BOUNDARY"},
            {"sr_no": 3, "name": "LIFT 1", "area_sqm": 40.0,
             "perimeter_m": 26.0, "family": "Lift", "layer": "LIFT_BOUNDARY"},
            {"sr_no": 4, "name": "SHAFT A", "area_sqm": 20.0,
             "perimeter_m": 18.0, "family": "Shaft", "layer": "SHAFT_BOUNDARY"},
        ],
        "families": [
            {"name": "Shaft", "count": 1, "total_sqm": 20.0, "rooms": []},
            {"name": "Lift", "count": 1, "total_sqm": 40.0, "rooms": []},
            {"name": "Toilet", "count": 1, "total_sqm": 60.0, "rooms": []},
            {"name": "Office", "count": 1, "total_sqm": 400.0, "rooms": []},
        ],
    }


class TestReconciliation(unittest.TestCase):
    def test_residual(self):
        r = reconciliation(sample_areas())
        self.assertEqual(r["measured_room_area_sqm"], 520.0)
        self.assertEqual(r["unaccounted_sqm"], 480.0)
        self.assertEqual(r["coverage_pct"], 52.0)
        self.assertTrue(r["verdict"].startswith("Fair"))

    def test_poor_coverage_flagged(self):
        d = sample_areas()
        d["rooms"] = d["rooms"][:1]
        d["rooms"][0]["area_sqm"] = 100.0
        self.assertTrue(reconciliation(d)["verdict"].startswith("Poor"))

    def test_no_floor_area(self):
        d = sample_areas()
        d["floor_area_sqm"] = None
        self.assertIsNone(reconciliation(d))


class TestKPIs(unittest.TestCase):
    def test_core_and_carpet(self):
        k = efficiency_kpis(sample_areas())
        self.assertEqual(k["core_area_sqm"], 60.0)      # lift 40 + shaft 20
        self.assertEqual(k["carpet_area_sqm"], 460.0)   # 520 - 60
        self.assertEqual(k["core_efficiency_pct"], 6.0)
        self.assertEqual(k["carpet_to_floor_pct"], 46.0)

    def test_family_split_percentages_sum_to_measured(self):
        k = efficiency_kpis(sample_areas())
        total = sum(f["pct_of_floor"] for f in k["family_split"])
        self.assertAlmostEqual(total, 52.0, places=6)


class TestDeckAndStuds(unittest.TestCase):
    def test_deductions_and_rate(self):
        d = deck_sheet_and_studs(sample_areas())
        self.assertEqual(d["total_deducted_sqm"], 60.0)
        self.assertEqual(d["deck_sheet_area_sqm"], 940.0)
        self.assertEqual(d["studs_count_nos"], round(940.0 * 9))

    def test_rate_is_nine_per_sqm(self):
        self.assertEqual(deck_sheet_and_studs(sample_areas())["studs_rate_per_sqm"],
                         9.0)


class TestGeometryHealth(unittest.TestCase):
    def rect(self, x, y, w, h, name, area=None):
        pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        return {"pts": pts, "name": name, "layer": "ROOM_BOUNDARY",
                "area_sqm": area if area is not None else shoelace_area_sqm(pts)}

    def test_clean_set(self):
        polys = [self.rect(0, 0, 5000, 5000, "A"),
                 self.rect(6000, 0, 5000, 5000, "B")]
        self.assertTrue(geometry_health(polys)["clean"])

    def test_duplicate_detected(self):
        polys = [self.rect(0, 0, 5000, 5000, "A"),
                 self.rect(0, 0, 5000, 5000, "A copy")]
        issues = geometry_health(polys)["by_issue"]
        self.assertEqual(issues.get("Duplicate"), 1)

    def test_contained_detected(self):
        polys = [self.rect(0, 0, 10000, 10000, "Big"),
                 self.rect(1000, 1000, 2000, 2000, "Small")]
        self.assertEqual(geometry_health(polys)["by_issue"].get("Contained"), 1)

    def test_sliver_detected(self):
        polys = [self.rect(0, 0, 20000, 20, "Stray line")]
        self.assertEqual(geometry_health(polys)["by_issue"].get("Sliver"), 1)

    def test_partial_overlap_flagged(self):
        polys = [self.rect(0, 0, 10000, 10000, "A"),
                 self.rect(7000, 0, 10000, 10000, "B")]
        self.assertEqual(geometry_health(polys)["by_issue"].get("Partial overlap"),
                         1)


class TestExtractionQA(unittest.TestCase):
    def test_problem_rows_identified(self):
        stats = {"text_status_by_floor": {
            "L1": {1: "primary", 2: "no_walls", 3: "absorbed", 4: "pending"}}}
        results = {"L1": [
            {"sr_no": 1, "text": "OFFICE", "layer": "TX", "family": "Office"},
            {"sr_no": 2, "text": "STORE", "layer": "TX", "family": "Unknown"},
            {"sr_no": 3, "text": "NOTE", "layer": "TX", "family": "Unknown"},
            {"sr_no": 4, "text": "SPARE", "layer": "TX", "family": "Unknown"},
        ]}
        qa = extraction_qa(stats, results)
        self.assertEqual(qa["problem_count"], 2)
        self.assertEqual(qa["boundary_count"], 1)
        self.assertEqual({p["text"] for p in qa["problems"]}, {"STORE", "SPARE"})


class TestRevisionCompare(unittest.TestCase):
    def test_added_removed_changed(self):
        old = sample_areas()
        new = sample_areas()
        new["rooms"][0]["area_sqm"] = 450.0            # changed
        new["rooms"].pop()                              # SHAFT A removed
        new["rooms"].append({"sr_no": 9, "name": "STORE", "area_sqm": 25.0,
                             "family": "Storage"})      # added
        c = compare_revisions(old, new)
        self.assertEqual(c["summary"]["changed_count"], 1)
        self.assertEqual(c["summary"]["removed_count"], 1)
        self.assertEqual(c["summary"]["added_count"], 1)
        self.assertEqual(c["changed"][0]["delta_sqm"], 50.0)
        self.assertEqual(c["summary"]["delta_sqm"], 55.0)  # +50 -20 +25

    def test_identical_revisions_have_no_delta(self):
        c = compare_revisions(sample_areas(), sample_areas())
        self.assertEqual(c["summary"]["delta_sqm"], 0.0)
        self.assertEqual(c["summary"]["changed_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
