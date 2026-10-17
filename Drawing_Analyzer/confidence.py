# -*- coding: utf-8 -*-
"""How far a traced room can be trusted: a score from the evidence.

Every boundary is scored when it is drawn, from what the engine knows
about how it was found - never from a fixed cut-off. The score starts at
BASE and moves with each piece of evidence:

  for                                         against
  - the label states a size and the outline   - the label states a size the
    matches it (within 5%: strongly)            outline does not match
  - the label states its area and the         - it states an area the outline
    outline matches it                          does not match (the statement
                                                uses the stated figure)
  - traced along the walls (a closed fill)    - only a box the rays guessed
  - an X-marked opening (an X is explicit)    - no family for the name
  - a named family (toilet, lift, shaft...)   - other rooms' labels inside it
                                              - titled by a tag (SD-6, RWP)
                                              - grown or fitted after tracing
                                              - a leak caught and cut back

Bands: High at HIGH or above, Medium at MEDIUM or above, Low below.
The bands were checked on the benchmark towers (285 FD T1, BHUB T1, H10
T1-T3, 285 FD T2): see tests/test_confidence.py and the CHANGELOG.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .config import CONFIG

_CF = CONFIG.get("confidence", {}) if isinstance(CONFIG, dict) else {}
BASE = float(_CF.get("base", 0.55))
HIGH = float(_CF.get("high", 0.80))
MEDIUM = float(_CF.get("medium", 0.60))
W = {
    "size_exact": 0.30, "size_close": 0.15, "size_off": -0.35,
    "area_exact": 0.25, "area_off": -0.25,
    "traced": 0.12, "box_only": -0.20,
    "x_marked": 0.25,
    "family": 0.08, "no_family": -0.15,
    "other_labels": -0.10,
    "grown": -0.05, "leak_cut": -0.10,
    "tag_name": -0.25,
}
W.update({k: float(v) for k, v in (_CF.get("weights") or {}).items()})


def band(score: float) -> str:
    return "High" if score >= HIGH else ("Medium" if score >= MEDIUM else "Low")


def score_room(area_sqm: float, w_mm: float, h_mm: float, *, want=None, want_area=None,
               traced=True, source="P", family="", other_labels=0, grown=False,
               leak_cut=False, tag_name=False) -> Tuple[float, List[str]]:
    """(score 0-1, [reasons]) for one boundary."""
    s, why = BASE, []

    def add(key, text):
        nonlocal s
        s += W[key]
        why.append(("+" if W[key] >= 0 else "-") + " " + text)

    if want:
        a, b = want
        stated = a * b / 1e6
        err = min(max(abs(w_mm - p) / p, abs(h_mm - q) / q) for p, q in ((a, b), (b, a)))
        aerr = abs(area_sqm - stated) / stated if stated else 1.0
        if err <= 0.05 or aerr <= 0.05:
            add("size_exact", "matches the stated size %gx%g" % (a, b))
        elif err <= 0.15 or aerr <= 0.15:
            add("size_close", "close to the stated size %gx%g" % (a, b))
        else:
            add("size_off", "stated %gx%g = %.2f sq.m, drawn %.2f" % (a, b, stated, area_sqm))
    elif want_area:
        aerr = abs(area_sqm - want_area) / want_area if want_area else 1.0
        if aerr <= 0.06:
            add("area_exact", "matches the stated %.2f sq.m" % want_area)
        else:
            add("area_off", "label states %.2f sq.m, drawn %.2f" % (want_area, area_sqm))
    if source == "X":
        add("x_marked", "X-marked opening")
    elif traced:
        add("traced", "traced along the walls")
    else:
        add("box_only", "a box from the walls around, not a closed trace")
    if tag_name:
        add("tag_name", "titled by a tag (door, column, equipment), not a room name")
    if family and family not in ("Unknown", "Ignore"):
        add("family", "named " + family)
    else:
        add("no_family", "the name matches no family")
    if other_labels:
        add("other_labels", "%d other room name(s) inside" % other_labels)
    if grown:
        add("grown", "grown or fitted after tracing")
    if leak_cut:
        add("leak_cut", "a leak was caught and cut back")
    return max(0.0, min(1.0, s)), why
