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
    r"^(LVL|LEVEL|EL|FFL|SFL|SSL)\.?\s*[:=]?\s*[+\-]?\s*\d+(\.\d+)?\s*(M|MM)?\s*(FFL|SFL|SSL|FL|LVL)?$",  # LVL + 555.35FFL (P25)
    r"^GRIDS?\b",                                         # a grid note: GRID FOR CRITICAL ROOM (P14)
    r"^(UP|DN|DOWN|NTS|TYP|REF|DIA|THK|CLR|BELOW|ABOVE)\.?$",
    r"^R\d+$",                                          # radii / revisions
    r"^W-?\s*\d{3,4}\s*T-?\s*\d{2,3}\s*R-?\s*\d{2,3}$",  # stair spec W-1500 T-300 R-150
    r"^\d+\s*[xX]\s*\d+$",                              # 2500 X 2500
    r"^\d+\s*MM\b.*\(\s*[WH]\s*\).*$",                 # 1100mm (W) X2400mm (H)
    r"^\d+\s*MM\s+SUNK$",
    r"^\d+\s*MM\.?\s+(DROP|SUNK|SUNKEN|STEP|DEPRESSION)\b",   # 10MM DROP IN FINISH (BHUB): a floor finish note
    r"^.*\bDROP\s+IN\s+(FINISH|FLOOR|FFL|LEVEL|LVL)\b",           # ... DROP IN FINISH - never a room's name
    r"^(NOTE|DATE|SIGN|SCALE|REV|DRAWN|CHECKED|APPROVED)\s*:?$",
    r"^\d{1,2}-\d{1,2}-\d{2,4}$",                       # revision dates
    r"^(TOWER|PHASE|BLOCK)[-\s]?\d+$",
    r"^(LOW|LOWER|MID|MIDDLE|HIGH|UPPER)\s*ZONE\s*(\(?[A-Z0-9]{1,2}\)?)?$",   # lift group tag inside a lift (XA)
]


_WORD_RE_CACHE = {}
_MATCH_FORM_CACHE = {}


def match_form(text) -> str:
    """A label as keywords are matched against it.

    Drawings write one name many ways, and the keyword missed each:
      * "FIRE \\nTOWER" - MTEXT lines, or double spaces, between words;
      * "ELE. ROOM", "P.C.TOI" - abbreviation dots;
      * "SHAFT1380x1250" - the size run into the name.
    Line breaks and runs of spaces become one space, a dot after a letter
    becomes a space, and a word of two or more letters is parted from the
    number run into it. Keywords get the same treatment, so "P.LIFT" and
    "TOI.EX.SHAFT" still match themselves.
    """
    t = _MATCH_FORM_CACHE.get(text)
    if t is None:
        t = str(text or "").upper()
        t = re.sub(r"(?<=[A-Z])\.", " ", t)
        t = re.sub(r"(?<=[A-Z]{2})(?=\d)", " ", t)
        t = " ".join(t.split())
        if len(_MATCH_FORM_CACHE) > 200000:
            _MATCH_FORM_CACHE.clear()
        _MATCH_FORM_CACHE[text] = t
    return t


def keyword_matches(keyword, text_upper) -> bool:
    """Whole-word keyword match.

    Plain substring matching produced real misclassifications on the
    drawing this was tested against: "FRESH AIR ATTENUATORS" matched the
    keyword TEN because of "atTENuators", and "8M EXPRESS WAY" matched
    PRESS because of "exPRESSway". Both went to the wrong family.
    Anchoring on word boundaries fixes it while leaving multi-word
    keywords such as "LIFT WELL" and punctuated ones such as "P.LIFT"
    working. Both sides are compared in match_form, so "FIRE \\nTOWER"
    is FIRE TOWER and "ELE. ROOM" is ELE ROOM.
    """
    rx = keyword_regex(keyword)
    return bool(rx and rx.search(match_form(text_upper)))


_KW_RX_CACHE = {}


def keyword_regex(keyword):
    """The compiled whole-word pattern for a keyword, or None for a blank
    one. Search it against match_form(text): a caller testing one text
    against many keywords cleans the text once (keyword_matches does it
    per call)."""
    got = _KW_RX_CACHE.get(keyword)
    if got is not None or keyword in _KW_RX_CACHE:
        return got
    kw = match_form((keyword or "").strip())
    if not kw:
        _KW_RX_CACHE[keyword] = None
        return None
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
    _KW_RX_CACHE[keyword] = rx
    return rx


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
        form = match_form(t)
        for kw in self.keywords:
            rx = keyword_regex(kw)
            if rx is not None and rx.search(form):
                return kw
        return None


# A size written under a name: "2700x1000", "1500 X 950 MM", "900X1100".
# Metres too, a unit on either number: "2.6M X 2.9M", "3.3MX7.4M" (XA tower).
_SIZE_RE = re.compile(
    r"(?<!\d)(?<!\d\.)(\d{1,6}(?:\.\d+)?)\s*(MM|CM|M(?![A-Z]{2}))?\s*[xX\u00d7*]\s*(\d{1,6}(?:\.\d+)?)"
    r"(?:\s*(MM|M|CM)\b)?")
# A parenthesised note: "(1000 X 300 & 300 X 900 MM SLAB CUT-OUT)".
_PAREN_RE = re.compile(r"\([^)]*\)?")
# Words that make a text a note or a cross-reference, not a name.
_NOTE_WORDS_RE = re.compile(
    r"\b(REFER|REF|DRG|DWG|DETAILS?|NOTES?|SEE|FOR|SHOWN|TOWARDS|PROVIDED|"
    r"AS PER|RAISING|TYP|TYPICAL|SLEEVES?|CEILING|BOTT|BOTTOM|NOS|"
    r"CONDUITS?|RISERS?|FROM|TO BE|VENDOR|OPENINGS? TO|FLOOR LEVEL)\b")
# A quantity: "4Nos 19mm CONDUIT RISERS FROM B-1 TO TERRACE" is a services
# note, never the name of a space - though it says TERRACE.
_QUANTITY_RE = re.compile(r"^\d+\s*NOS\b", re.IGNORECASE)
_DETAIL_RE = re.compile(r"\bDETAILS?\b|\bREFER\b|\bSEE DRG\b")


def name_part(text) -> str:
    """The name in a label, without its size line or bracketed note.

    "TR-SH-02 1200x1200 (1000 X 300 & 300 X 900 MM SLAB CUT-OUT)" names
    TR-SH-02; its sizes are not part of the name and must not make it
    read as a sentence.
    """
    t = _PAREN_RE.sub(" ", str(text or ""))
    t = _SIZE_RE.sub(" ", t)
    t = re.sub(r"\b(MM|SQ\.?M|SQM)\b", " ", t, flags=re.I)
    lines = [" ".join(ln.split()) for ln in t.replace("\r", "").split("\n")]
    return "\n".join(ln for ln in lines if ln)


def declared_size(text):
    """The size a label states for its space, as (a_mm, b_mm), or None.

    Only the first size outside brackets counts: in "TR-SH-02 1200x1200
    (1000 X 300 ...)" the shaft is 1200 x 1200 and the bracket describes
    the slab cut-out inside it. Sizes given in metres are converted.
    """
    t = _PAREN_RE.sub(" ", str(text or ""))
    m = None
    for m_ in _SIZE_RE.finditer(t):
        g1, g3 = m_.group(1), m_.group(3)
        # "2 X 3" with no unit and no decimal is a count, not a size
        if len(g1.split(".")[0]) < 2 and len(g3.split(".")[0]) < 2 and "." not in g1 + g3 \
                and not (m_.group(2) or m_.group(4)):
            continue
        m = m_
        break
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(3))
    unit = (m.group(4) or m.group(2) or "").upper()
    if unit == "M" or (not unit and a < 100 and b < 100):
        a, b = a * 1000.0, b * 1000.0
    elif unit == "CM":
        a, b = a * 10.0, b * 10.0
    if a < 100 or b < 100 or a > 200000 or b > 200000:
        return None
    return a, b


# An area written under a name: "REFUGE AREA / 151.05 SQ. MTR.",
# "OFFICE SPACE / 843.61 SQ.MTS", "12.5 SQM", "1200 SQ.FT".
_AREA_RE = re.compile(
    r"(?<![\d.])(\d{1,6}(?:\.\d+)?)\s*(?:(SQ\.?\s*(?:FT|FEET)\.?)|"
    r"SQ\.?\s*M(?:TRS?|ETRES?|ETERS?|TS)?\.?|SQM|M2|M\u00b2)(?![A-Z])", re.I)


def declared_area(text):
    """The floor area a label states for its space, in sq.m, or None."""
    m = _AREA_RE.search(_PAREN_RE.sub(" ", str(text or "")))
    if not m:
        return None
    v = float(m.group(1))
    if m.group(2):
        v *= 0.09290304
    return v if 0.5 <= v <= 100000 else None


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
    # The name is measured without its size line: "LOBBY PRESSURE SHAFT
    # 1500 X 950 MM" is a name, and dropping it as a note left the shaft
    # unnamed. A name spread over short lines ("TOWER-01 / ZONE-01 / LIFT
    # LOBBY-01") is still a name; a note gives itself away by its words.
    if _QUANTITY_RE.match(t):
        return TIER_LONG
    # a pointer to a detail drawing ("TOILET DETAILS", "STAIR DETAIL-2") is
    # not the room it names (H10 Tower 2: TOILET DETAILS traced 130 sq.m)
    if _DETAIL_RE.search(t.upper()):
        return TIER_LONG
    name = name_part(t) or t
    if len(name.replace("\n", " ")) > rules.max_name_chars:
        lines = name.split("\n")
        if (len(lines) > 4 or _NOTE_WORDS_RE.search(name.upper()) or _NOTE_WORDS_RE.search(t.upper())
                or any(len(ln) > rules.max_name_chars for ln in lines)):
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
