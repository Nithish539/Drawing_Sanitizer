# -*- coding: utf-8 -*-
"""Floor usages for families, stopping a step, and the per-floor cache.

Run:  python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer import jobs  # noqa: E402


class UsageTests(unittest.TestCase):

    def test_floor_usage_from_title(self):
        self.assertEqual(dr.floor_usage("BASEMENT 2 FLOOR PLAN"), "Parking")
        self.assertEqual(dr.floor_usage("STILT FLOOR PLAN"), "Parking")
        self.assertEqual(dr.floor_usage("RETAIL FLOOR PLAN"), "Retail")
        self.assertEqual(dr.floor_usage("LOWER ZONE TYPICAL FLOOR PLAN"),
                         dr.list_floor_usages()[1])

    def test_parking_family_only_on_parking_floors(self):
        office = dr.rules_for_usage("Office")
        parking = dr.rules_for_usage("Parking")
        self.assertNotIn("Parking", [f["name"] for f in office])
        self.assertIn("Parking", [f["name"] for f in parking])
        # A family with no usages is on every floor.
        self.assertIn("Toilet", [f["name"] for f in office])
        self.assertIn("Toilet", [f["name"] for f in parking])
        self.assertEqual(dr.classify_family("CAR PARKING", office), "Unknown")
        self.assertEqual(dr.classify_family("CAR PARKING", parking), "Parking")
        # No usage given: every family, as before.
        self.assertEqual(len(dr.rules_for_usage(None)), len(dr._FAMILY_RULES))

    def test_extraction_uses_the_floor_usage(self):
        import ezdxf
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        msp.add_text("CAR PARKING", dxfattribs={"height": 300, "insert": (5000, 5000)})
        path = os.path.join(tempfile.mkdtemp(), "p.dxf")
        doc.saveas(path)
        poly = [(0, 0), (60000, 0), (60000, 50000), (0, 50000)]
        for usage, want in (("Office", "Unknown"), ("Parking", "Parking")):
            rows = dr.extract_texts(path, [{"name": "F", "polygon": poly, "usage": usage}])
            self.assertEqual(rows["F"][0]["family"], want, usage)

    def test_usages_survive_a_save(self):
        tmp = os.path.join(tempfile.mkdtemp(), "family_rules.json")
        with open(dr.family_rules_path(), encoding="utf-8") as fh:
            original = fh.read()
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(original)
        fams = dr.list_family_rules()
        try:
            dr.save_family_rules(fams, rules_path=tmp,
                                 usages=[{"name": "Office", "keywords": ["OFFICE"]},
                                         {"name": "Parking", "keywords": ["PARKING"]}],
                                 default_usage="Office")
            with open(tmp, encoding="utf-8") as fh:
                saved = json.load(fh)
            park = [f for f in saved["families"] if f["name"] == "Parking"][0]
            self.assertEqual(park["usages"], ["Parking"])
            self.assertEqual([u["name"] for u in saved["usages"]], ["Office", "Parking"])
            bad = [dict(f) for f in fams]
            bad[0]["usages"] = ["Warehouse"]
            with self.assertRaises(ValueError):
                dr.save_family_rules(bad, rules_path=tmp)
        finally:
            # save_family_rules reloads the module's rules from the file it
            # wrote; put the real ones back.
            dr.reload_family_rules()


class StopTests(unittest.TestCase):

    def test_cancel_stops_the_job(self):
        tok = jobs.begin("t-job-1")
        try:
            jobs.check()                      # not stopped yet
            jobs.cancel("t-job-1")
            with self.assertRaises(jobs.Stopped):
                jobs.check()
        finally:
            jobs.end(tok)
        jobs.check()                          # outside the job: no flag

    def test_stopped_is_not_swallowed_by_except_exception(self):
        tok = jobs.begin("t-job-2")
        jobs.cancel("t-job-2")
        try:
            with self.assertRaises(jobs.Stopped):
                try:
                    jobs.check()
                except Exception:             # noqa: BLE001 - what routes do
                    self.fail("Stopped was caught as an ordinary error")
        finally:
            jobs.end(tok)


class PassCacheTests(unittest.TestCase):

    def test_a_floor_is_worked_out_once(self):
        calls = []
        real = dr._floor_pass

        def counting(job):
            calls.append(job["floor"]["name"])
            return {"merged": [], "n": job["floor"]["name"]}

        saved, saved_min = dr._PASS_CACHE_DIR, dr.PARALLEL_MIN_FLOORS
        dr.PARALLEL_MIN_FLOORS = 99            # in this process: the stub
        dr._floor_pass = counting
        dr.set_pass_cache_dir(tempfile.mkdtemp())
        try:
            work = [{"floor": {"name": n, "polygon": [(0, 0), (1, 0), (1, 1)]}, "rows": []}
                    for n in ("A", "B")]
            first, _ = dr._run_floor_passes(work)
            again, _ = dr._run_floor_passes(work)
            self.assertEqual(sorted(calls), ["A", "B"])          # only the first time
            self.assertEqual([r["n"] for r in again], ["A", "B"])
            changed = [dict(work[0], rows=[{"text": "X"}]), work[1]]
            dr._run_floor_passes(changed)
            self.assertEqual(sorted(calls), ["A", "A", "B"])     # only the changed floor
        finally:
            dr._floor_pass = real
            dr.PARALLEL_MIN_FLOORS = saved_min
            dr.set_pass_cache_dir(saved)


if __name__ == "__main__":
    unittest.main()
