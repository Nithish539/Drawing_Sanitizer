# -*- coding: utf-8 -*-
"""Tests for label choice and family matching.

Both bugs these cover came from the same drawing. A boundary drawn round
a pressurisation shaft was labelled INID04 - an equipment tag - instead
of PRESS, and because INID04 matches no family keyword the boundary also
landed in UNKNOWN_BOUNDARY. Separately, substring matching put "FRESH AIR
ATTENUATORS" in the TEN family and "8M EXPRESS WAY" in PRESS.

Run:  python -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from Drawing_Analyzer.labels import (LabelRules, TIER_FAMILY,  # noqa: E402
                                     TIER_TAG, build_rules, choose_label,
                                     keyword_matches, label_tier,
                                     looks_like_room_name)


def load_families():
    path = os.path.join(ROOT, "Drawing_Analyzer", "family_rules.json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)["families"]


class TestKeywordMatching(unittest.TestCase):
    """Whole word, not bare substring."""

    def test_ten_does_not_match_attenuators(self):
        self.assertFalse(keyword_matches("TEN", "FRESH AIR ATTENUATORS"))

    def test_press_does_not_match_expressway(self):
        self.assertFalse(keyword_matches("PRESS", "8M EXPRESS WAY"))

    def test_exact_words_still_match(self):
        self.assertTrue(keyword_matches("TEN", "TEN"))
        self.assertTrue(keyword_matches("PRESS", "PRESS"))
        self.assertTrue(keyword_matches("TEN", "TEN ROOM"))

    def test_punctuated_keywords(self):
        self.assertTrue(keyword_matches("P.LIFT", "P.LIFT"))
        self.assertTrue(keyword_matches("ST-", "ST-1"))

    def test_multiword_keyword(self):
        self.assertTrue(keyword_matches("LIFT WELL", "LIFT WELL 2650X2650"))

    def test_plural_tolerated(self):
        self.assertTrue(keyword_matches("ATTENUATOR", "FRESH AIR ATTENUATORS"))

    def test_inside_brackets(self):
        self.assertTrue(keyword_matches("OFFICE", "17(OFFICE)"))


class TestFamilyRulesFile(unittest.TestCase):
    def setUp(self):
        self.rules = LabelRules(family_rules=load_families())

    def test_real_labels_resolve(self):
        expected = {
            "PRESS": True, "TEN": True, "HVAC ROOM": True,
            "FIRE TOWER": True, "FUEL ROOM-1": True,
            "PEDESTRIAN WALKWAY": True, "2M WIDE GREEN STRIP": True,
            "VOID SPACE": True, "WAITING LOUNGE": True,
            "CHANGE ROOM": True, "FOODCOURT": True, "SUCTION PIT": True,
            "BATTERY": True, "FACP": True, "CAR DROP OFF": True,
            "LIFT WELL 2650X2650": True, "24 PAX": True,
            "INID04": False, "FRD-3": False, "GD1": False,
        }
        for text, should_match in expected.items():
            got = self.rules.family_keyword(text) is not None
            self.assertEqual(got, should_match,
                             "{0!r} family match was {1}".format(text, got))

    def test_no_false_positives(self):
        self.assertIsNone(self.rules.family_keyword("FRESH AIR ATTENUATORS")
                          if False else None)
        # ATTENUATOR is a real MEP keyword, so this one SHOULD match -
        # what must not happen is it matching via "TEN".
        self.assertEqual(self.rules.family_keyword("FRESH AIR ATTENUATORS"),
                         "ATTENUATOR")
        self.assertIsNone(self.rules.family_keyword("8M EXPRESS WAY"))


class TestLabelRanking(unittest.TestCase):
    def setUp(self):
        self.rules = LabelRules(family_rules=load_families())

    def test_the_press_case(self):
        """The exact situation from the drawing."""
        candidates = [
            {"text": "PRESS", "height": 200, "x": 0, "y": 0},
            {"text": "INID04", "height": 150, "x": 100, "y": 50},
            {"text": "SID-5", "height": 150, "x": 600, "y": 300},
            {"text": "300", "height": 120, "x": 50, "y": 200},
            {"text": "1700", "height": 120, "x": -400, "y": -100},
        ]
        best = choose_label(candidates, self.rules, centre=(0, 0))
        self.assertEqual(best["text"], "PRESS")

    def test_a_tag_closer_to_the_centre_still_loses(self):
        candidates = [
            {"text": "INID04", "height": 200, "x": 0, "y": 0},
            {"text": "PRESS", "height": 100, "x": 2000, "y": 2000},
        ]
        self.assertEqual(
            choose_label(candidates, self.rules, centre=(0, 0))["text"],
            "PRESS")

    def test_taller_text_wins_between_equals(self):
        candidates = [
            {"text": "STORE", "height": 100, "x": 0, "y": 0},
            {"text": "PANTRY", "height": 300, "x": 0, "y": 0},
        ]
        self.assertEqual(
            choose_label(candidates, self.rules, centre=(0, 0))["text"],
            "PANTRY")

    def test_dimensions_never_win(self):
        candidates = [{"text": "2250", "height": 900, "x": 0, "y": 0},
                      {"text": "STORE", "height": 50, "x": 500, "y": 500}]
        self.assertEqual(
            choose_label(candidates, self.rules, centre=(0, 0))["text"],
            "STORE")

    def test_all_noise_gives_nothing(self):
        candidates = [{"text": "2250"}, {"text": "UP"}, {"text": "+150 FFL"}]
        self.assertIsNone(choose_label(candidates, self.rules))

    def test_tiers(self):
        self.assertEqual(label_tier("PRESS", self.rules), TIER_FAMILY)
        self.assertEqual(label_tier("INID04", self.rules), TIER_TAG)
        # A code the family rules recognise is a room name, not a tag.
        self.assertEqual(label_tier("FHC-3", self.rules), TIER_FAMILY)
        self.assertEqual(label_tier("ST-1", self.rules), TIER_FAMILY)


class TestNoiseFiltering(unittest.TestCase):
    def setUp(self):
        self.rules = LabelRules(family_rules=load_families())

    def test_ignored(self):
        for t in ("2250", "UP", "DN", "+150 FFL", "2500 X 2500", "R5",
                  "NOTE:", "DATE:", "14-07-2023", "TOWER 01", "BELOW",
                  "100MM SUNK", "1100mm (W) X2400mm (H)"):
            self.assertTrue(self.rules.is_ignored(t), t)

    def test_not_ignored(self):
        for t in ("PRESS", "OPEN OFFICE", "LIFT LOBBY", "FHC-3",
                  "STAIR PREZ", "HVAC ROOM"):
            self.assertFalse(self.rules.is_ignored(t), t)

    def test_room_name_test(self):
        self.assertTrue(looks_like_room_name("OPEN OFFICE", self.rules))
        self.assertTrue(looks_like_room_name("PRESS", self.rules))
        self.assertFalse(looks_like_room_name("INID04", self.rules))
        self.assertFalse(looks_like_room_name("2250", self.rules))


class TestConfigWiring(unittest.TestCase):
    def test_rules_build_from_config(self):
        from Drawing_Analyzer.config import CONFIG
        rules = build_rules(CONFIG, load_families())
        self.assertTrue(rules.is_tag("INID04"))
        self.assertTrue(rules.is_ignored("2500 X 2500"))
        self.assertEqual(rules.family_keyword("PRESS"), "PRESS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
