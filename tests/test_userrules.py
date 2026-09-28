# -*- coding: utf-8 -*-
"""Tests for rules the user writes in their own words.

Two things have to hold. The parser must never quietly guess: where a
sentence does not settle something - "small", "regular" - it has to ask
rather than pick, because the alternative is silently reshaping every
boundary on a plate. And the actions have to do what their names say,
since they run last and have the final word on the geometry.

Run:  python -m unittest discover -s tests -v
"""

import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from Drawing_Analyzer import userrules as U  # noqa: E402

FAMILIES = ["Shaft", "Toilet", "Lift", "Staircase", "Circulation",
            "MEP / Services"]


def rule_of(text, families=FAMILIES, layers=()):
    return U.parse(text, families, layers)


class ParseTests(unittest.TestCase):

    def test_regular_shapes(self):
        got = rule_of("draw regular shapes only")
        self.assertTrue(got["ok"])
        self.assertEqual(got["rule"]["action"]["kind"], "rectangle")
        self.assertEqual(got["rule"]["condition"]["kind"], "everything")

    def test_regular_is_ambiguous_and_says_so(self):
        """'Regular' does not choose between upright and angled, so the
        parser must not choose either."""
        got = rule_of("always draw regular shapes")
        self.assertTrue(any("angled" in q for q in got["questions"]))

    def test_area_condition(self):
        got = rule_of("do not draw boundaries under 0.3 sq.m")
        self.assertEqual(got["rule"]["action"]["kind"], "drop")
        self.assertEqual(got["rule"]["condition"],
                         {"kind": "area_below", "value": 0.3})

    def test_area_synonyms(self):
        for text in ("ignore anything smaller than 0.5 sqm",
                     "skip boundaries less than 0.5 sq.m"):
            got = rule_of(text)
            self.assertEqual(got["rule"]["condition"]["value"], 0.5, text)

    def test_family_condition(self):
        got = rule_of("draw boxes for the Shaft family")
        self.assertEqual(got["rule"]["condition"],
                         {"kind": "family", "family": "Shaft"})

    def test_angled_wording(self):
        got = rule_of("shaft marks should follow the angle")
        self.assertEqual(got["rule"]["action"]["mode"], "angled")

    def test_snap_reads_the_distance(self):
        got = rule_of("snap every boundary to 25mm")
        self.assertEqual(got["rule"]["action"], {"kind": "snap", "mm": 25.0})

    def test_snap_without_a_distance_asks(self):
        got = rule_of("snap every boundary")
        self.assertTrue(any("mm" in q for q in got["questions"]))

    def test_grow_and_shrink(self):
        self.assertEqual(rule_of("grow Toilet boundaries by 100 mm")
                         ["rule"]["action"]["mm"], 100.0)
        self.assertEqual(rule_of("shrink Staircase boundaries by 50mm")
                         ["rule"]["action"]["mm"], -50.0)

    def test_move_to_family_keeps_the_label_as_the_subject(self):
        """'put LIFT LOBBY under Staircase' is about a label, even though
        Lift is also a family name."""
        got = rule_of("put LIFT LOBBY under Staircase")
        self.assertEqual(got["rule"]["condition"],
                         {"kind": "label", "label": "LIFT LOBBY"})
        self.assertEqual(got["rule"]["action"],
                         {"kind": "set_family", "family": "Staircase"})

    def test_move_a_whole_family(self):
        got = rule_of("put Toilet under Shaft")
        self.assertEqual(got["rule"]["condition"],
                         {"kind": "family", "family": "Toilet"})

    def test_draw_only_keeps_one_kind(self):
        """'Draw only shaft boundaries' reads backwards from the other
        rules: what it names is what survives."""
        got = rule_of("Draw only shaft boundaries")
        self.assertTrue(got["ok"])
        self.assertEqual(got["rule"]["action"], {"kind": "keep_only"})
        self.assertEqual(got["rule"]["condition"],
                         {"kind": "family", "family": "Shaft"})
        self.assertEqual(U.describe(got["rule"]),
                         "Draw only boundaries in the Shaft family - drop "
                         "everything else.")

    def test_draw_only_wording_variants(self):
        for text in ("draw only Shaft", "show only the toilet boundaries",
                     "only lift boundaries", "keep only x marks"):
            got = rule_of(text)
            self.assertEqual(got["rule"]["action"]["kind"], "keep_only", text)

    def test_draw_only_something_unknown_asks(self):
        got = rule_of("draw only the big ones")
        self.assertFalse(got["ok"])
        self.assertTrue(got["questions"])

    def test_nonsense_is_refused_with_guidance(self):
        got = rule_of("make the toilets correct")
        self.assertFalse(got["ok"])
        self.assertTrue(got["questions"])

    def test_empty_is_refused(self):
        self.assertFalse(rule_of("")["ok"])

    def test_describe_is_readable(self):
        got = rule_of("do not draw boundaries under 0.3 sq.m")
        self.assertEqual(U.describe(got["rule"]),
                         "For boundaries under 0.3 sq.m, do not draw at all.")


class GeometryTests(unittest.TestCase):

    def test_min_area_rectangle_of_a_square_is_itself(self):
        square = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]
        got = U.min_area_rectangle(square)
        self.assertAlmostEqual(U._area_sqm(got), U._area_sqm(square), places=6)

    def test_min_area_rectangle_follows_an_angle(self):
        """A room turned 30 degrees should come back as a rectangle turned
        30 degrees, not as the much larger upright box around it."""
        ang = math.radians(30)
        w, h = 4000.0, 2000.0
        corners = [(0, 0), (w, 0), (w, h), (0, h)]
        turned = [(x * math.cos(ang) - y * math.sin(ang),
                   x * math.sin(ang) + y * math.cos(ang)) for x, y in corners]
        got = U.min_area_rectangle(turned)
        self.assertAlmostEqual(U._area_sqm(got), w * h / 1e6, places=3)
        upright = U._area_sqm(U._rect_from_bbox(U._bbox_of(turned)))
        self.assertLess(U._area_sqm(got), upright)


def item(label="ROOM", family="Toilet", layer="TOILET_BOUNDARY",
         source="traced", shape=None):
    shape = shape or [(0, 0), (2000, 0), (2000, 1000), (0, 1000)]
    return {"shape": list(shape), "bbox": U._bbox_of(shape), "label": label,
            "family": family, "layer": layer, "source": source,
            "area_sqm": U._area_sqm(shape)}


class ApplyTests(unittest.TestCase):

    def test_drop_removes_and_reports(self):
        rules = [rule_of("do not draw boundaries under 5 sq.m")["rule"]]
        kept, report = U.apply_rules([item()], rules)
        self.assertEqual(kept, [])
        self.assertEqual(report[0]["dropped"], 1)

    def test_condition_limits_what_is_touched(self):
        rules = [rule_of("draw boxes for the Shaft family")["rule"]]
        kept, report = U.apply_rules(
            [item(family="Shaft"), item(family="Toilet")], rules)
        self.assertEqual(len(kept), 2)
        self.assertEqual(report[0]["matched"], 1)

    def test_disabled_rule_does_nothing(self):
        r = rule_of("do not draw boundaries under 5 sq.m")["rule"]
        r["enabled"] = False
        kept, report = U.apply_rules([item()], r and [r])
        self.assertEqual(len(kept), 1)
        self.assertEqual(report, [])

    def test_snap_moves_corners_onto_the_grid(self):
        r = rule_of("snap every boundary to 100mm")["rule"]
        odd = [(3, 7), (2003, 7), (2003, 1007), (3, 1007)]
        kept, _ = U.apply_rules([item(shape=odd)], [r])
        for x, y in kept[0]["shape"]:
            self.assertEqual(x % 100, 0)
            self.assertEqual(y % 100, 0)

    def test_grow_increases_the_area(self):
        r = rule_of("grow every boundary by 100 mm")["rule"]
        before = item()
        kept, _ = U.apply_rules([before], [r])
        self.assertGreater(kept[0]["area_sqm"], before["area_sqm"])

    def test_set_family_marks_the_boundary(self):
        r = rule_of("put ROOM under Staircase")["rule"]
        kept, _ = U.apply_rules([item()], [r])
        self.assertEqual(kept[0]["family_forced"], "Staircase")

    def test_rules_run_in_order(self):
        """A later rule acts on what the earlier one produced - dropping
        first leaves the second nothing to do."""
        drop = rule_of("do not draw boundaries under 5 sq.m")["rule"]
        snap = rule_of("snap every boundary to 100mm")["rule"]
        _kept, report = U.apply_rules([item()], [drop, snap])
        self.assertEqual(report[0]["dropped"], 1)
        self.assertEqual(report[1]["matched"], 0)


class KeepOnlyTests(unittest.TestCase):

    def test_keeps_the_named_family_and_drops_the_rest(self):
        rules = [rule_of("draw only shaft boundaries")["rule"]]
        items = [item(family="Shaft"), item(family="Toilet"),
                 item(family="Lift")]
        kept, report = U.apply_rules(items, rules)
        self.assertEqual([i["family"] for i in kept], ["Shaft"])
        self.assertEqual(report[0]["dropped"], 2)

    def test_keeps_nothing_when_nothing_matches(self):
        rules = [rule_of("draw only shaft boundaries")["rule"]]
        kept, _ = U.apply_rules([item(family="Toilet")], rules)
        self.assertEqual(kept, [])

    def test_shape_is_untouched(self):
        """Restricting the output must not also reshape what survives."""
        rules = [rule_of("draw only shaft boundaries")["rule"]]
        before = item(family="Shaft")
        kept, _ = U.apply_rules([before], rules)
        self.assertEqual(kept[0]["shape"], before["shape"])


class MultiFamilyTests(unittest.TestCase):
    """Naming several families has to work both ways round: one sentence
    listing them, or one rule each."""

    def test_one_sentence_names_several(self):
        got = rule_of("draw only shaft, staircase and mep boundaries")
        self.assertEqual(got["rule"]["condition"]["kind"], "family_in")
        self.assertEqual(got["rule"]["condition"]["families"],
                         ["Shaft", "Staircase", "MEP / Services"])

    def test_a_short_name_finds_its_family(self):
        """Nobody types 'MEP / Services'."""
        got = rule_of("draw only mep boundaries")
        self.assertEqual(got["rule"]["condition"],
                         {"kind": "family", "family": "MEP / Services"})

    def test_plurals(self):
        got = rule_of("draw the shafts")
        self.assertEqual(got["rule"]["condition"],
                         {"kind": "family", "family": "Shaft"})

    def test_draw_without_only_is_still_a_restriction_and_says_so(self):
        got = rule_of("draw staircase boundaries")
        self.assertEqual(got["rule"]["action"]["kind"], "keep_only")
        self.assertTrue(any("restriction" in q for q in got["questions"]))

    def test_several_rules_add_up_instead_of_cancelling(self):
        """Applied one after another these would leave nothing: the second
        rule only ever sees what the first kept."""
        rules = [rule_of("draw only shaft boundaries")["rule"],
                 rule_of("draw staircase boundaries")["rule"],
                 rule_of("draw mep boundaries")["rule"]]
        items = [item(family=f) for f in
                 ("Shaft", "Staircase", "MEP / Services", "Toilet", "Lift")]
        kept, report = U.apply_rules(items, rules)
        self.assertEqual([i["family"] for i in kept],
                         ["Shaft", "Staircase", "MEP / Services"])
        self.assertEqual(len(report), 1, "the run should report as one")
        self.assertEqual(report[0]["dropped"], 2)

    def test_the_union_is_reported_as_one_line(self):
        rules = [rule_of("draw only shaft boundaries")["rule"],
                 rule_of("draw staircase boundaries")["rule"]]
        _kept, report = U.apply_rules([item(family="Shaft")], rules)
        self.assertEqual(report[0]["summary"],
                         "Draw only Shaft or Staircase - drop everything "
                         "else.")

    def test_the_same_family_twice_is_said_once(self):
        rules = [rule_of("draw only shaft boundaries")["rule"],
                 rule_of("draw shaft boundaries")["rule"],
                 rule_of("draw staircase boundaries")["rule"]]
        _kept, report = U.apply_rules([item(family="Shaft")], rules)
        self.assertEqual(report[0]["summary"],
                         "Draw only Shaft or Staircase - drop everything "
                         "else.")

    def test_a_restriction_still_composes_with_other_actions(self):
        """A drop rule after a restriction must still act on what is left,
        not be swallowed into the group."""
        rules = [rule_of("draw only shaft boundaries")["rule"],
                 rule_of("do not draw boundaries under 5 sq.m")["rule"]]
        kept, report = U.apply_rules(
            [item(family="Shaft"), item(family="Toilet")], rules)
        self.assertEqual(len(report), 2)
        self.assertEqual(kept, [])

    def test_family_in_matches_only_its_members(self):
        rule = {"id": "x", "enabled": True, "text": "",
                "condition": {"kind": "family_in",
                              "families": ["Shaft", "Lift"]},
                "action": {"kind": "keep_only"}}
        kept, _ = U.apply_rules(
            [item(family="Shaft"), item(family="Lift"), item(family="Toilet")],
            [rule])
        self.assertEqual([i["family"] for i in kept], ["Shaft", "Lift"])

    def test_validate_accepts_the_new_condition(self):
        got = U.validate([{"text": "t", "action": {"kind": "keep_only"},
                           "condition": {"kind": "family_in",
                                         "families": ["Shaft"]}}])
        self.assertEqual(got[0]["condition"]["families"], ["Shaft"])


class StorageTests(unittest.TestCase):

    def test_validate_rejects_unknown_action(self):
        with self.assertRaises(ValueError):
            U.validate([{"action": {"kind": "explode"},
                         "condition": {"kind": "everything"}}])

    def test_validate_rejects_unknown_condition(self):
        with self.assertRaises(ValueError):
            U.validate([{"action": {"kind": "drop"},
                         "condition": {"kind": "on a tuesday"}}])

    def test_validate_keeps_the_sentence(self):
        got = U.validate([{"text": "do not draw small ones",
                           "action": {"kind": "drop"},
                           "condition": {"kind": "everything"}}])
        self.assertEqual(got[0]["text"], "do not draw small ones")
        self.assertTrue(got[0]["id"])

    def test_missing_file_is_no_rules_not_an_error(self):
        self.assertEqual(U.load_rules(os.path.join(ROOT, "no_such.json")), [])


if __name__ == "__main__":
    unittest.main()
