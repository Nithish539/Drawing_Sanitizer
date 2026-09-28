# -*- coding: utf-8 -*-
"""Choosing which text names a room.

The old ranking preferred short strings containing digits, on the theory
that room codes look like "SH-1". On a real services drawing that is
backwards. Around one pressurisation shaft sat:

    PRESS      the room name
    INID04     an equipment tag
    SID-5      a smoke-damper tag
    TEN        an adjacent room name

"PRESS" scored 105 and "INID04" scored 6, so the boundary came out
labelled INID04 - and since INID04 matches no family keyword, the room
landed in UNKNOWN_BOUNDARY too. One bad comparison caused both symptoms,
and it is why the newer run had 22 unknown boundaries against the older
run's 6.

The ranking now works from evidence that actually distinguishes a room
name from a tag:

  1. **Does it match a family keyword?** A name the family rules already
     recognise - PRESS, TEN, TOILET, LIFT LOBBY - is a room name by
     definition. This does most of the work, and it means the answer
     improves whenever family_rules.json is extended.
  2. **Does it look like an equipment tag?** Letters followed by digits
     with no spaces, or a configured prefix such as INID or SID.
     Demoted hard.
  3. **Text height.** Room names are drawn larger than device tags.
  4. **Distance to the centre** of the region being labelled.

Nothing here is hard-coded to one project: the tag patterns live in
config.json and the room vocabulary in family_rules.json.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Sequence

# Tiers, best first. Kept as named constants so the ordering is legible
# where it is used.
TIER_FAMILY = 0        # matches a family keyword: definitely a room name
TIER_WORDS = 1         # two or more alphabetic words
TIER_PLAIN = 2         # a single alphabetic word
TIER_CODE = 3          # short code with digits, not a known tag prefix
TIER_LONG = 4          # long descriptive text
TIER_TAG = 5           # looks like an equipment tag

# A tag pattern is only consulted AFTER the family keywords, so a code
# the family rules recognise - FHC-3, ST-1 - is never demoted by these.
# Devices that are stencilled onto a plan and are also family keywords, so
# every other test reads them as room names. Whole strings, not substrings:
# "FHC" is a hose cabinet, "FHC ROOM" would be a room.
_DEFAULT_EQUIPMENT_TAGS = [
    "FHC", "FHR", "FACP", "FAPA", "PA RACK", "PA & FA",
    "TEN", "FCP", "DGCP", "FCU", "RWP", "OWC", "VANITY COUNTER",
]

_DEFAULT_TAG_PATTERNS = [
    r"^[A-Z]{1,4}[-\s]?\d{1,3}[A-Z]?$",   # D1, GD1, FRD-3, SID-5, A-201
    r"^[A-Z]{1,5}\d{2,}[A-Z]?$",           # INID04, X004
    r"^[A-Z]{1,4}[-\s]?\d+[-\s]?\d+$",    # W-2600-300
]

_DEFAULT_IGNORE_PATTERNS = [
    r"^\d+(\.\d+)?\s*(MM|CM|M|MTR|SQM|SQ\.?M)?$",      # bare dimensions
    r"^[+\-]?\d+(\.\d+)?\s*(FFL|SSL|FL|LVL)\b",        # levels
    r"^(UP|DN|DOWN|NTS|TYP|REF|DIA|THK|CLR|BELOW|ABOVE)\.?$",
    r"^R\d+$",                                          # radii / revisions
    r"^\d+\s*[xX]\s*\d+$",                              # 2500 X 2500
    r"^\d+\s*MM\b.*\(\s*[WH]\s*\).*$",                 # 1100mm (W) X2400mm (H)
    r"^\d+\s*MM\s+SUNK$",
    r"^(NOTE|DATE|SIGN|SCALE|REV|DRAWN|CHECKED|APPROVED)\s*:?$",
    r"^\d{1,2}-\d{1,2}-\d{2,4}$",                       # revision dates
    r"^(TOWER|PHASE|BLOCK)[-\s]?\d+$",
]


_WORD_RE_CACHE = {}


def keyword_matches(keyword, text_upper) -> bool:
    """Whole-word keyword match.

    Plain substring matching produced real misclassifications on the
    drawing this was tested against: "FRESH AIR ATTENUATORS" matched the
    keyword TEN because of "atTENuators", and "8M EXPRESS WAY" matched
    PRESS because of "exPRESSway". Both went to the wrong family.
    Anchoring on word boundaries fixes it while leaving multi-word
    keywords such as "LIFT WELL" and punctuated ones such as "P.LIFT"
    working.
    """
    kw = (keyword or "").strip().upper()
    if not kw:
        return False
    rx = _WORD_RE_CACHE.get(kw)
    if rx is None:
        esc = re.escape(kw)
        # \b only works next to a word character, so only anchor the ends
        # that actually have one - "ST-" and "P.LIFT" must still match.
        left = r"\b" if kw[0].isalnum() else ""
        # Tolerate a plural: the drawing says ATTENUATORS where the rule
        # says ATTENUATOR.
        right = r"S?\b" if kw[-1].isalnum() else ""
        rx = re.compile(left + esc + right)
        _WORD_RE_CACHE[kw] = rx
    return bool(rx.search(text_upper))


class LabelRules:
    """Compiled patterns plus the family vocabulary."""

    __slots__ = ("tag_res", "ignore_res", "keywords", "equipment",
                 "max_name_chars")

    def __init__(self, tag_patterns=None, ignore_patterns=None,
                 family_rules=None, equipment_tags=None,
                 max_name_chars=None):
        self.max_name_chars = int(max_name_chars or 28)
        self.equipment = {str(t).strip().upper()
                          for t in (equipment_tags or _DEFAULT_EQUIPMENT_TAGS)
                          if str(t).strip()}
        self.tag_res = [re.compile(p, re.IGNORECASE)
                        for p in (tag_patterns or _DEFAULT_TAG_PATTERNS)]
        self.ignore_res = [re.compile(p, re.IGNORECASE)
                           for p in (ignore_patterns or _DEFAULT_IGNORE_PATTERNS)]
        self.keywords = []
        for fam in (family_rules or []):
            for kw in fam.get("keywords", []):
                kw = (kw or "").strip().upper()
                if kw:
                    self.keywords.append(kw)
        # Longest first so "LIFT LOBBY" is tested before "LIFT".
        self.keywords.sort(key=len, reverse=True)

    def is_ignored(self, text) -> bool:
        t = (text or "").strip()
        return any(r.match(t) for r in self.ignore_res)

    def is_equipment(self, text) -> bool:
        """A device stencilled on the plan, not a space.

        These read as room names to every other test because they are
        family keywords - FHR is how the Fire family is recognised - so
        without this list a fire hose reel hanging on a toilet wall is
        given a boundary of its own across half the toilet.
        """
        return (text or "").strip().upper() in self.equipment

    def is_tag(self, text) -> bool:
        t = (text or "").strip()
        if self.is_equipment(t):
            return True
        if " " in t:
            return False               # tags do not have spaces
        return any(r.match(t) for r in self.tag_res)

    def family_keyword(self, text) -> Optional[str]:
        t = (text or "").strip().upper()
        if not t:
            return None
        for kw in self.keywords:
            if keyword_matches(kw, t):
                return kw
        return None


def label_tier(text, rules: LabelRules) -> int:
    t = (text or "").strip()
    if not t:
        return TIER_LONG
    # Checked before the family keywords, because equipment tags ARE
    # family keywords; they may still title a shaft that has nothing
    # better in it, but they must never outrank a real room name.
    if rules.is_equipment(t):
        return TIER_TAG
    # Length is judged before the family keywords, not after. A cross
    # reference like "FOR DETAILS OF STAIRCASE-01 REFER DRG. NO.T1-A-302"
    # contains STAIRCASE, so testing keywords first ranked a sentence of
    # drawing notes as a room name - and it then took a boundary that the
    # real stairs merged into, which is how four staircases and two lifts
    # disappeared off a plan. Room names are short; sentences are notes.
    if len(t) > rules.max_name_chars:
        return TIER_LONG
    if rules.family_keyword(t):
        return TIER_FAMILY
    if rules.is_tag(t):
        return TIER_TAG
    has_digit = any(c.isdigit() for c in t)
    words = [w for w in re.split(r"[\s/,\-]+", t) if w]
    alpha_words = [w for w in words if any(c.isalpha() for c in w)]
    if not has_digit and len(alpha_words) >= 2:
        return TIER_WORDS
    if not has_digit:
        return TIER_PLAIN
    return TIER_CODE


def label_rank(text, height=0.0, distance=0.0, rules: LabelRules = None):
    """Sort key for candidate labels. Lower is better.

    Tier first, then the taller text, then the nearer one, then the
    shorter string as a final deterministic tie-break.
    """
    rules = rules or LabelRules()
    t = (text or "").strip()
    return (label_tier(t, rules),
            -round(float(height or 0.0), 3),
            round(float(distance or 0.0), 3),
            len(t))


def choose_label(candidates: Sequence[Dict], rules: LabelRules = None,
                 centre=None) -> Optional[Dict]:
    """Pick the best label from candidates.

    Each candidate is a dict with at least "text", optionally "height",
    "x" and "y". Ignored texts (dimensions, levels, UP/DN) are dropped
    outright rather than ranked last, so they can never win by default.
    """
    rules = rules or LabelRules()
    scored = []
    for c in candidates:
        text = (c.get("text") or "").strip()
        if not text or rules.is_ignored(text):
            continue
        d = c.get("distance")
        if d is None and centre is not None:
            d = math.hypot(float(c.get("x", 0.0)) - centre[0],
                           float(c.get("y", 0.0)) - centre[1])
        scored.append((label_rank(text, c.get("height", 0.0), d or 0.0,
                                  rules), c))
    if not scored:
        return None
    scored.sort(key=lambda p: p[0])
    return scored[0][1]


def looks_like_room_name(text, rules: LabelRules = None) -> bool:
    """Whether this text deserves its own boundary before the others.

    Replaces the old PRIMARY_RE test, which matched letter-plus-digit
    codes and therefore promoted exactly the equipment tags that should
    be demoted.
    """
    rules = rules or LabelRules()
    t = (text or "").strip()
    if not t or rules.is_ignored(t) or rules.is_tag(t):
        return False
    return label_tier(t, rules) <= TIER_PLAIN


def build_rules(config: Dict, family_rules: Sequence[Dict]) -> LabelRules:
    lf = (config or {}).get("label_filters") or {}
    return LabelRules(tag_patterns=lf.get("device_tag_patterns"),
                      ignore_patterns=lf.get("ignore_patterns"),
                      equipment_tags=lf.get("equipment_tags"),
                      max_name_chars=lf.get("max_room_name_chars"),
                      family_rules=family_rules)
