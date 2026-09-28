# -*- coding: utf-8 -*-
"""Tests for the switchable rule catalogue.

The catalogue is data the UI renders and the drawing pass reads, so the
two must agree: every rule the generator asks about has to exist, and a
saved selection from an older build must not break a run.

Run:  python -m unittest discover -s tests -v
"""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from Drawing_Analyzer import rules  # noqa: E402


class CatalogueTests(unittest.TestCase):

    def test_every_rule_is_complete(self):
        for r in rules.RULES:
            for field in ("key", "group", "title", "detail", "default"):
                self.assertIn(field, r, r.get("key"))
                self.assertTrue(str(r[field]) != "", r["key"])
            self.assertIsInstance(r["default"], bool)

    def test_keys_are_unique(self):
        keys = [r["key"] for r in rules.RULES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_every_rule_belongs_to_a_listed_group(self):
        known = {key for key, _title in rules.GROUPS}
        for r in rules.RULES:
            self.assertIn(r["group"], known, r["key"])

    def test_catalogue_covers_every_rule(self):
        listed = [r["key"] for g in rules.catalogue() for r in g["rules"]]
        self.assertCountEqual(listed, [r["key"] for r in rules.RULES])

    def test_shipped_defaults_are_all_on(self):
        """Defaults reproduce the behaviour from before rules could be
        switched, with one deliberate exception: an opening with no door
        now stays open, so a urinal bay or alcove belongs to its room."""
        got = rules.defaults()
        self.assertFalse(got["close_wall_gaps"])
        self.assertTrue(all(v for k, v in got.items()
                            if k != "close_wall_gaps"))


class ResolveTests(unittest.TestCase):

    def test_none_gives_defaults(self):
        self.assertEqual(rules.resolve(None), rules.defaults())

    def test_override_applies(self):
        got = rules.resolve({"x_marks": False})
        self.assertFalse(got["x_marks"])
        self.assertTrue(got["trace_rooms"])

    def test_unknown_keys_are_ignored(self):
        """A selection saved by an older build names rules that have since
        been renamed; the run should proceed, not fail."""
        got = rules.resolve({"no_such_rule": False, "x_marks": False})
        self.assertNotIn("no_such_rule", got)
        self.assertFalse(got["x_marks"])
        self.assertEqual(len(got), len(rules.defaults()))

    def test_values_are_coerced_to_bool(self):
        got = rules.resolve({"x_marks": 0, "trace_rooms": "yes"})
        self.assertIs(got["x_marks"], False)
        self.assertIs(got["trace_rooms"], True)

    def test_changed_from_default_reports_only_moves(self):
        active = rules.resolve({"x_marks": False, "trace_rooms": True})
        self.assertEqual(rules.changed_from_default(active), ["x_marks"])
        self.assertEqual(rules.changed_from_default(rules.defaults()), [])


class WiringTests(unittest.TestCase):
    """The generator reads the resolved rules by key. A typo there would
    raise KeyError mid-run on a real drawing, which is a poor place to
    find out."""

    def test_generator_only_asks_for_rules_that_exist(self):
        path = os.path.join(ROOT, "Drawing_Analyzer", "dxf_reader.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        asked = set(re.findall(r'rule\["([a-z_]+)"\]', source))
        self.assertTrue(asked, "no rule lookups found - did the wiring move?")
        unknown = asked - set(rules.defaults())
        self.assertEqual(unknown, set())

    def test_every_rule_is_actually_wired_in(self):
        """A rule in the list that nothing reads is a switch connected to
        nothing, which is worse than not offering it."""
        path = os.path.join(ROOT, "Drawing_Analyzer", "dxf_reader.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        asked = set(re.findall(r'rule\["([a-z_]+)"\]', source))
        self.assertEqual(set(rules.defaults()) - asked, set())


if __name__ == "__main__":
    unittest.main()
