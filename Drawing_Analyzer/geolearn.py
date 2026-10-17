"""Measure the geometry engine's rules on real drawings and write the
results into geo_profiles.json.

    python -m Drawing_Analyzer.geolearn TOKEN=TEXT_DXF [TOKEN=TEXT_DXF ...] [--write]

TOKEN is an upload (uploads/TOKEN.dxf with its .meta.json - the floors
picked in Step 2); TEXT_DXF is the text engine's annotated DXF of the same
upload (outputs/TOKEN_annotated.dxf after Generate Boundaries). For every
rule the engine used, it counts how often the finding matched the text
engine at the same place (geocompare's AGREEMENT), and on how many
drawings. Verified counts (disagreements checked by eye) are kept from the
profile's "verified" section. Sizes of the agreed findings per class are
written to "observed" so the ranges can be reviewed against them.

Without --write it only prints the table.
"""
from __future__ import annotations

import collections
import json
import os
import sys
import time

import numpy as np

from . import dxf_reader as dr
from . import geoengine as ge
from . import geocompare as gc

HERE = os.path.dirname(os.path.abspath(__file__))
UP = os.path.join(HERE, "uploads")


def learn(pairs, write=False, log=print):
    profile = ge.load_profile()
    agree = collections.Counter()
    conflict = collections.Counter()
    unref = collections.Counter()
    against = collections.defaultdict(collections.Counter)
    total = collections.Counter()
    drawings = collections.defaultdict(set)
    sizes = collections.defaultdict(list)
    names = []
    for tok, text_dxf in pairs:
        meta = json.load(open(os.path.join(UP, tok + ".meta.json"), encoding="utf-8"))
        floors = meta.get("selected") or []
        names.append(meta.get("original_name") or tok)
        t = time.time()
        res = ge.run(os.path.join(UP, tok + ".dxf"), floors, meta.get("unit_override"), profile=profile)
        text = gc.read_text_boundaries(text_dxf, floors, meta.get("unit_override"))
        cmp = gc.compare(res, text)
        by_id = {}
        for rows in res["floors"].values():
            for r in rows:
                by_id[r["id"]] = r
                total[r["rule"]] += 1
                drawings[r["rule"]].add(tok)
        for row in cmp["rows"]:
            if not row["geo"]:
                continue
            r = by_id[row["geo"]["id"]]
            if row["category"] == "AGREEMENT":
                agree[r["rule"]] += 1
                f = r["features"]
                sizes[r["cls"]].append((f["width_mm"], f["height_mm"], f["area_sqm"]))
            elif row["category"] == "CONFLICT":
                conflict[r["rule"]] += 1
                against[r["rule"]][row["text"]["cls"]] += 1
            else:
                # no named text boundary here: geometry only, an X the text
                # engine could not name, or several candidates
                unref[r["rule"]] += 1
        log("{0}: {1} findings, {2:.0f}s".format(names[-1], len(by_id), time.time() - t))

    ver = profile.get("verified") or {}
    rules = {}
    log("\n{0:24s} {1:>6s} {2:>6s} {3:>6s} {4:>6s} {5:>5s}  {6}".format(
        "rule", "agree", "confl", "unref", "n", "conf", "conflicts with (text class)"))
    for rule in sorted(total, key=lambda r: -total[r]):
        if rule.startswith("unknown_"):
            continue                      # not a classification: nothing to measure
        st = {"agree": agree[rule], "conflict": conflict[rule], "unreferenced": unref[rule],
              "n": total[rule], "drawings": len(drawings[rule]),
              "conflicts_with": dict(against[rule].most_common(5))}
        if rule in ver:
            st["verified"] = ver[rule][:2]
        rules[rule] = st
        c, note = ge.confidence({"rules": rules}, rule)
        log("{0:24s} {1:6d} {2:6d} {3:6d} {4:6d} {5:>5s}  {6}".format(
            rule, st["agree"], st["conflict"], st["unreferenced"], st["n"],
            "-" if c is None else "{0:.0%}".format(c), st["conflicts_with"]))
    observed = {}
    for cls, v in sizes.items():
        a = np.array(v, float)
        if len(a) < 3:
            continue
        observed[cls] = {"n": len(a),
                         "width_mm_p5_p95": [int(np.percentile(a[:, 0], 5)), int(np.percentile(a[:, 0], 95))],
                         "height_mm_p5_p95": [int(np.percentile(a[:, 1], 5)), int(np.percentile(a[:, 1], 95))],
                         "area_sqm_p5_p95": [round(float(np.percentile(a[:, 2], 5)), 2),
                                             round(float(np.percentile(a[:, 2], 95)), 2)]}
    if write:
        profile["rules"] = rules
        profile["observed"] = observed
        profile["_source"] = "Measured {0} on {1} drawing(s): {2}".format(
            time.strftime("%Y-%m-%d"), len(names), ", ".join(names))
        with open(ge.PROFILE_PATH, "w", encoding="utf-8") as fh:
            json.dump(profile, fh, indent=2)
        log("\nwrote " + ge.PROFILE_PATH)
    return rules, observed


def main(argv):
    write = "--write" in argv
    pairs = [a.split("=", 1) for a in argv if "=" in a]
    if not pairs:
        print(__doc__)
        return 1
    learn(pairs, write=write)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
