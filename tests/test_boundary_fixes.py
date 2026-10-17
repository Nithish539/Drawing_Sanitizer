# -*- coding: utf-8 -*-
"""Boundary fixes from the BHUB review: lift lobby family, names split over
lines, notes, other-level structure, fixtures and break lines.

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer.labels import match_form  # noqa: E402


def fam(text):
    return dr.classify_family(text, dr._FAMILY_RULES)


class FamilyTests(unittest.TestCase):

    def test_lift_lobby_is_its_own_family(self):
        self.assertEqual(fam("LOWER ZONE\n4000 WD LIFT LOBBY"), "Lift Lobby")
        self.assertEqual(fam("LIFT WELL\n2650X2650"), "Lift")
        self.assertEqual(fam("LOBBY"), "Circulation")
        layers = {f["name"]: f["layer"] for f in dr.list_families()}
        self.assertEqual(layers["Lift Lobby"], "AI_LIFTLOBBY")

    def test_names_written_over_lines_and_with_dots(self):
        # a fire tower (the fire-fighting lobby) is a service lobby: AI_FIRE
        # is only for the fire rooms - fire command / control, refuge, pump
        self.assertEqual(fam("FIRE \nTOWER\n3100X4400"), "Service Lobby")
        self.assertEqual(fam("ELE. ROOM\n2500x2300"), "Electrical")
        self.assertEqual(fam("FT. LOBBY PR. \nSHAFT1380x1250"), "Shaft")
        # Keywords with dots still match themselves.
        self.assertEqual(fam("P.LIFT"), "Lift")
        self.assertEqual(fam("TOI.EX.SHAFT"), "Shaft")
        self.assertEqual(match_form("FIRE \n TOWER"), "FIRE TOWER")

    def test_service_notes_are_not_rooms(self):
        for t in ("4Nos 19mm CONDUIT RISERS\nFROM B-1 TO TERRACE",
                  "110MM SLEEVE AT\n3rd & 7th FLOOR CEILING\nSLEEVE BOTT.LVL+3615"):
            self.assertTrue(dr.is_note_text(t), t)
            self.assertFalse(dr.looks_like_primary(t), t)
        self.assertTrue(dr.looks_like_primary("2000 WD BALCONY"))
        self.assertFalse(dr.is_note_text("MALE TOILET"))


class WallTests(unittest.TestCase):

    def test_other_level_structure_xref(self):
        self.assertTrue(dr._other_level_xref("XREF-T1_STR$0$BASE._STR-COLUMN"))
        self.assertFalse(dr._other_level_xref("XREF-T1_STR$1$COLUMN"))
        self.assertFalse(dr._other_level_xref("BASE-COLUMN"))      # not an XREF
        self.assertTrue(dr._floor_uses_lower_structure("Basement 2"))
        self.assertFalse(dr._floor_uses_lower_structure("Typical 2nd Floor"))

    def _wall(self, layer, y0=0.0):
        """A 3 m wall, 200 thick, on layer."""
        return [(0.0, y0, 3000.0, y0, layer, 3000.0, -1, False),
                (0.0, y0 + 200.0, 3000.0, y0 + 200.0, layer, 3000.0, -1, False)]

    def test_basement_xref_is_not_a_wall_on_a_typical_floor(self):
        segs = self._wall("XREF-T1_STR$0$BASE._STR-COLUMN")
        self.assertEqual(dr._detect_walls(segs)["faces"], [])
        self.assertEqual(len(dr._detect_walls(segs, other_level=False)["faces"]), 2)

    def test_fixture_lines_are_not_walls(self):
        segs = self._wall("toil-fix")
        self.assertEqual(dr._detect_walls(segs)["faces"], [])
        self.assertEqual(len(dr._detect_walls(segs, fixtures=False)["faces"]), 2)

    def test_break_line_with_a_free_end_is_dropped(self):
        # A room 6 x 4 m drawn with walls; a break line - two diagonals
        # 150 apart - runs from the bottom wall up into the room and stops.
        segs = []
        for a, b in (((0, 0), (6000, 0)), ((6000, 0), (6000, 4000)),
                     ((6000, 4000), (0, 4000)), ((0, 4000), (0, 0))):
            segs.append((a[0], a[1], b[0], b[1], "A-WALL", 0.0, -1, False))
        for off in (0.0, 150.0):
            segs.append((2000.0 + off, 0.0, 2600.0 + off, 2500.0, "A-DETAIL", 0.0, -1, False))
        walls = dr._detect_walls(segs)
        self.assertEqual(walls["break_lines"], 2)
        diagonal = [f for f in walls["faces"] if f[0] != f[2] and f[1] != f[3]]
        self.assertEqual(diagonal, [])
        kept = dr._detect_walls(segs, break_lines=False)["faces"]
        self.assertEqual(len([f for f in kept if f[0] != f[2] and f[1] != f[3]]), 2)

    def test_angled_wall_joined_at_both_ends_stays(self):
        segs = []
        for a, b in (((0, 0), (6000, 0)), ((0, 4000), (0, 0))):
            segs.append((a[0], a[1], b[0], b[1], "A-WALL", 0.0, -1, False))
        # An angled wall from the bottom wall to the left wall, 200 thick.
        segs.append((3000.0, 0.0, 0.0, 3000.0, "A-PARTITION", 0.0, -1, False))
        segs.append((3283.0, 0.0, 0.0, 3283.0, "A-PARTITION", 0.0, -1, False))
        walls = dr._detect_walls(segs)
        self.assertEqual(walls["break_lines"], 0)


class ShaftRuleTests(unittest.TestCase):
    """A shaft is never round, and stands against a wall."""

    def test_cross_in_a_circle_is_not_a_shaft(self):
        box = (0.0, 0.0, 282.0, 282.0)          # two 400 mm lines at 45 degrees
        self.assertTrue(dr.x_in_circle(box, [(141.0, 141.0, 200.0)]))
        self.assertFalse(dr.x_in_circle(box, []))
        # a circle far off, or much bigger, does not count
        self.assertFalse(dr.x_in_circle(box, [(5000.0, 141.0, 200.0)]))
        self.assertFalse(dr.x_in_circle(box, [(141.0, 141.0, 2000.0)]))

    def test_shaft_needs_a_side_on_a_wall(self):
        box = (1000.0, 1000.0, 1900.0, 2000.0)
        wall = [(1000.0, 900.0, 1000.0, 2100.0)]           # along its left side
        far = [(500.0, 900.0, 500.0, 2100.0)]              # 500 mm away
        short = [(1000.0, 1000.0, 1000.0, 1200.0)]         # 20% of the side
        self.assertTrue(dr.shaft_side_on_wall(box, None, wall))
        self.assertFalse(dr.shaft_side_on_wall(box, None, far))
        self.assertFalse(dr.shaft_side_on_wall(box, None, short))
        self.assertFalse(dr.shaft_side_on_wall(box, None, []))

    def test_overrunning_end_line_closes_a_wall(self):
        # a 200 mm wall strip, 680 long; its end closed by a 309 mm line
        # that runs on 109 mm past the strip
        segs = [(0.0, 0.0, 680.0, 0.0, "A3-WL_EXT", 680.0, -1, False),
                (0.0, 200.0, 680.0, 200.0, "A3-WL_EXT", 680.0, -1, False),
                (680.0, 0.0, 680.0, 309.0, "A3-WL_EXT", 309.0, -1, False)]
        on = dr._detect_walls(segs)
        off = dr._detect_walls(segs, overrun_caps=False)
        self.assertEqual(on["overrun_caps"], 1)
        self.assertIn((680.0, 0.0, 680.0, 309.0), [tuple(c) for c in on["caps"]])
        self.assertNotIn((680.0, 0.0, 680.0, 309.0), [tuple(c) for c in off["caps"]])


class TextsFirstTests(unittest.TestCase):
    """Step 5: a family the texts name beats what the geometry's X says."""

    def _row(self, tcls, tname, gcls, category):
        sq = [[0, 0], [2650, 0], [2650, 2650], [0, 2650]]
        return {"floor": "F", "category": category, "level": "exact", "id": "CMP-1",
                "text": {"cls": tcls, "name": tname, "poly": sq, "area_sqm": 7.0},
                "geo": {"cls": gcls, "label": "SHAFT_01", "poly": sq, "area_sqm": 7.0, "id": "GEO-1"}}

    def test_text_family_wins_over_x_shaft(self):
        import importlib
        A = importlib.import_module("Drawing_Analyzer.app")
        saved = A.family_precedence
        A.family_precedence = lambda f: "geometry" if f == "Shaft" else ""
        try:
            for tcls, name, fam in (("LIFT", "LIFT WELL 2650X2650", "Lift"),
                                    ("BALCONY", "2000 WD BALCONY", "Balcony / Open"),
                                    ("VOID", "VOID", "Void")):
                cmp = {"rows": [self._row(tcls, name, "SHAFT", "CONFLICT")]}
                d = A._final_defaults(cmp)["rows"][0]["final_default"]
                self.assertEqual((d["use"], d["family"], d["name"]), ("text", fam, name), tcls)
                self.assertFalse(d["decide"])
            # a shaft is Step 3's (the X sequence, 2026-10-16): the texts'
            # outline, whatever the family's precedence says - and fixed
            cmp = {"rows": [self._row("SHAFT", "PHE SHAFT", "SHAFT", "AGREEMENT")]}
            d = A._final_defaults(cmp)["rows"][0]["final_default"]
            self.assertEqual((d["use"], d["family"], d["name"]), ("text", "Shaft", "PHE SHAFT"))
            self.assertTrue(d["fixed"])
        finally:
            A.family_precedence = saved


class GeometryAlwaysTests(unittest.TestCase):
    """Lifts and lift lobbies: what the geometry finds wins in Step 5."""

    def test_geometry_lift_lobby_wins(self):
        import importlib
        A = importlib.import_module("Drawing_Analyzer.app")
        saved = A.family_precedence
        A.family_precedence = lambda f: "geometry_always" if f in ("Lift", "Lift Lobby") else ""
        sq = [[0, 0], [4000, 0], [4000, 12000], [0, 12000]]

        def row(tcls, tname, gcls, glabel):
            return {"floor": "F", "category": "CONFLICT", "level": "exact", "id": "CMP-1",
                    "text": {"cls": tcls, "name": tname, "poly": sq, "area_sqm": 48.0},
                    "geo": {"cls": gcls, "label": glabel, "poly": sq, "area_sqm": 48.0, "id": "GEO-1"}}

        def final(r):
            return A._final_defaults({"rows": [r]})["rows"][0]["final_default"]
        try:
            # the texts call it something else: the geometry's lobby
            d = final(row("CIRC", "PASSAGE", "LIFT_LOBBY", "LIFT_LOBBY_01"))
            self.assertEqual((d["use"], d["family"], d["name"]), ("geometry", "Lift Lobby", "LIFT_LOBBY_01"))
            self.assertFalse(d["decide"])
            # the texts agree: the geometry's outline, the texts' name
            d = final(row("LIFTLOBBY", "LIFT LOBBY", "LIFT_LOBBY", "LIFT_LOBBY_01"))
            self.assertEqual((d["use"], d["family"], d["name"]), ("geometry", "Lift Lobby", "LIFT LOBBY"))
            # a geometry machine room does not beat a text lift lobby
            d = final(row("LIFTLOBBY", "LIFT LOBBY", "MACHINE_ROOM", "MACHINE_ROOM_01"))
            self.assertEqual((d["use"], d["family"]), ("text", "Lift Lobby"))
        finally:
            A.family_precedence = saved


class GuardrailTests(unittest.TestCase):
    """No boundary crosses a wall, a wall hatch or the facade."""

    def test_room_crossing_a_wall_is_cut_at_it(self):
        room = [(0, 0), (6000, 0), (6000, 4000), (0, 4000)]
        wall = [(3000, -100, 3000, 4100)]                 # straight through it
        p, h, b, changed = dr.guard_against_walls(room, [], (0, 0, 6000, 4000), (1000, 2000), wall, [])
        self.assertTrue(changed)
        self.assertLessEqual(b[2], 3001)                  # the label's half only
        self.assertGreater(b[2], 2990)

    def test_wall_along_the_edge_changes_nothing(self):
        room = [(0, 0), (6000, 0), (6000, 4000), (0, 4000)]
        wall = [(0, -100, 0, 4100), (-200, 0, 6200, 0)]   # its own sides
        self.assertFalse(dr.guard_against_walls(room, [], (0, 0, 6000, 4000), (1000, 2000), wall, [])[3])

    def test_column_hatch_inside_becomes_a_hole(self):
        room = [(0, 0), (6000, 0), (6000, 4000), (0, 4000)]
        column = ([(2500, 1500), (3400, 1500), (3400, 2400), (2500, 2400)], [])
        p, h, b, changed = dr.guard_against_walls(room, [], (0, 0, 6000, 4000), (1000, 1000), [], [column])
        self.assertTrue(changed)
        self.assertEqual(len(h), 1)
        self.assertAlmostEqual(dr.shoelace_area_sqm(p) - dr.shoelace_area_sqm(h[0]), 24.0 - 0.81, delta=0.05)


class GeometryChecksTests(unittest.TestCase):
    """Lift doors, the column grid and the service lobby column."""


    def test_column_grid_is_dense_and_fins_are_not(self):
        from shapely.geometry import box
        from Drawing_Analyzer import geoengine as ge
        grid = [box(x, y, x + 600, y + 600) for x in range(0, 40000, 8000) for y in range(0, 32000, 8000)]
        g = ge._grid_fit(grid)
        self.assertEqual((g["xs"], g["ys"]), (5, 4))
        self.assertGreaterEqual(g["density"], 0.9)
        # fins all round the facade: they line up both ways, but sparsely
        fins = [box(x, y, x + 250, y + 300) for x in range(0, 40000, 1000) for y in (0, 32000)] +                [box(x, y, x + 300, y + 250) for y in range(1000, 32000, 1000) for x in (0, 40000)]
        g = ge._grid_fit(fins)
        self.assertGreaterEqual(min(g["xs"], g["ys"]), 2)
        self.assertLess(g["density"], 0.25)

    def test_nested_column_outlines_are_one_column(self):
        from shapely.geometry import box
        from Drawing_Analyzer import geoengine as ge
        shapes = [box(0, 0, 900, 900), box(100, 100, 800, 800), box(5000, 0, 5900, 900)]
        self.assertEqual(len(ge._dedupe_shapes(shapes)), 2)

    def test_service_lobby_has_its_statement_column(self):
        from Drawing_Analyzer.area_statement import classify, HEADER_OF
        self.assertEqual(classify("SERVICE_LOBBY_01", "Service Lobby"), "service_lobby")
        self.assertEqual(classify("SERVICE LOBBY", ""), "service_lobby")
        self.assertEqual(classify("LIFT LOBBY", "Lift Lobby"), "lift_lobby")
        self.assertEqual(HEADER_OF["service_lobby"], "SERVICE LOBBY")
        self.assertEqual(dr.classify_family("SERVICE LOBBY", dr._FAMILY_RULES), "Service Lobby")
        self.assertEqual(dr.classify_family("LIFT MACHINE ROOM", dr._FAMILY_RULES), "Machine Room")


class RuleCatalogueTests(unittest.TestCase):

    def test_new_rules_can_be_switched(self):
        from Drawing_Analyzer import rules
        on = rules.defaults()
        for key in ("other_level_structure", "fixtures_not_walls", "break_lines",
                    "toilet_fixtures", "cut_shafts_from_rooms", "fit_stated_size",
                    "unknown_needs_walls", "open_side_guard", "overrun_wall_ends",
                    "never_cross_walls"):
            self.assertTrue(on[key], key)
            self.assertFalse(rules.resolve({key: False})[key], key)


if __name__ == "__main__":
    unittest.main()
