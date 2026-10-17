# -*- coding: utf-8 -*-
"""Floor levels from a plan title.

A sheet of floor plans titles each plate underneath it, and a typical plate
serves several floors at once:

    1ST FLOOR PLAN                                  -> 1st
    TYPICAL FLOOR PLAN-(2nd,4th,6th & 10th)         -> 2nd, 4th, 6th, 10th
    TYPICAL REFUGE FLOOR PLAN- 5th                  -> 5th (refuge)
    TYPICAL FLOOR PLAN 5TH TO 9TH                   -> 5th .. 9th
    SECOND & THIRD FLOOR PLAN                       -> 2nd, 3rd
    GROUND FLOOR / BASEMENT-2 / TERRACE FLOOR       -> as named

parse_levels() returns them in building order, each with a sort key, so a
statement can list the floors of a tower bottom to top.

Pure Python; no ezdxf.
"""

from __future__ import annotations

import re
from typing import Dict, List

_WORDS = {
    "FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4, "FIFTH": 5, "SIXTH": 6,
    "SEVENTH": 7, "EIGHTH": 8, "NINTH": 9, "TENTH": 10, "ELEVENTH": 11,
    "TWELFTH": 12, "THIRTEENTH": 13, "FOURTEENTH": 14, "FIFTEENTH": 15,
    "SIXTEENTH": 16, "SEVENTEENTH": 17, "EIGHTEENTH": 18, "NINETEENTH": 19,
    "TWENTIETH": 20, "TWENTY FIRST": 21, "TWENTY SECOND": 22,
    "TWENTY THIRD": 23, "TWENTY FOURTH": 24, "TWENTY FIFTH": 25,
}
_ORD = r"(\d{1,3})\s*(?:ST|ND|RD|TH)\b"
# A floor number with or without its suffix: "5TH" or "5".
_NUM = r"(\d{1,3})\s*(?:ST|ND|RD|TH)?"
_RANGE_SEP = r"\s*(?:TO|-|–|—|~|UPTO|UP\s+TO|TILL|THRU|THROUGH)\s*"
# A number that is a floor and not part of something else: not a level
# ("+3615", "3.0"), a date ("12-05-2024"), a size ("110MM", "5M", "2X3")
# or a code ("A-106").
_NOT_BEFORE = r"(?<![\d.+/\-A-Z])"
_NOT_AFTER = r"(?![\d.]|\s*(?:MM|CM|M\b|SQ|%|X\s*\d|-\s*\d))"
# "5TH TO 8TH", "4th - 7th", "5 TO 8": group 1 and 2 are the ends.
_RANGE = _NOT_BEFORE + _NUM + _RANGE_SEP + _NUM + r"\b" + _NOT_AFTER
# Words that make plain numbers in a title floors: "FLOOR 5", "LEVELS 5 TO 8".
_FLOOR_WORD = re.compile(r"\b(FLOORS?|FLRS?|LEVELS?|LVLS?|TYPICAL)\b")
# Numbers listed after a floor word: "FLOORS 2, 4 & 6", "FLOOR 2 TO 4 & 6",
# "FLOOR PLAN (5,6,7)". Each _ITEM is one floor or one range.
_ITEM = _NUM + r"(?:" + _RANGE_SEP + _NUM + r")?"
_ITEM_NC = (r"\d{1,3}\s*(?:ST|ND|RD|TH)?(?:" + _RANGE_SEP +
            r"\d{1,3}\s*(?:ST|ND|RD|TH)?)?")
_PLAIN_LIST = (r"\b(?:FLOORS?|FLRS?|LEVELS?|LVLS?|REFUGE)(?:\s+(?:PLANS?|FLOORS?))?\s*[-_:.(]?\s*" +
               r"((?:" + _ITEM_NC + r"\s*(?:,|&|AND)\s*)*" + _ITEM_NC + r")\b" +
               _NOT_AFTER)
# Numbers listed before the floor word, a word or two between: "1,3,5,7
# TYPICAL FLOORS", "30,32,34 FLOORS(MID ZONE)", "29 SERVICE FLOOR", "41
# FLOOR-AMENITY" (XA tower file names).
_LIST_BEFORE = (r"(?:^|(?<=[\s_(]))(?<!X\s)((?:" + _ITEM_NC + r"\s*(?:,|&|AND)\s*)*" + _ITEM_NC + r")" +
                r"\s*,?\s*(?:(?!(?:MM|CM|SQ|SQM|MTR?S?)\b)[A-Z]{2,}\s+){0,2}?(?:FLOORS?|FLRS?)\b")
# Words that make a text a floor title rather than a note.
TITLE_WORDS = re.compile(
    r"\b(FLOOR|FLR|LVL|LEVEL|TERRACE|BASEMENT|GROUND|PODIUM|STILT|ROOF|"
    r"MEZZANINE|TYPICAL)\b")

# Sort keys: basements below ground, the terrace and roof above everything.
_KEY_GROUND = 0
_KEY_TERRACE = 1000
_KEY_ROOF = 1001


def clean_title(text: str) -> str:
    """Strip CAD control codes (%%U underline and the like) and spacing."""
    t = re.sub(r"%%[A-Za-z]", "", text or "")
    t = re.sub(r"\\[A-Za-z][^;]*;|[{}]", "", t)       # MTEXT formatting
    return " ".join(t.replace("\n", " ").split())


def _ordinal_words(t: str) -> str:
    """'SECOND TO FIFTH' -> '2ND TO 5TH' (upper-case text)."""
    for w in sorted(_WORDS, key=len, reverse=True):
        t = re.sub(r"\b{0}\b".format(w), ordinal(_WORDS[w]).upper(), t)
    return t


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return "{0}{1}".format(n, suf)


def parse_levels(title: str) -> List[Dict]:
    """Every floor a plan title names, bottom to top.

    Each is {"key": sort key, "label": "5th Floor", "refuge": bool}.
    Returns [] when the title names no floor.
    """
    t = _ordinal_words(clean_title(title).upper())       # SECOND -> 2ND
    # an ordinal run into the next word: "15THFLOOR", "12THFLR" (H10 Tower 1)
    t = re.sub(r"(\d{1,3}\s*(?:ST|ND|RD|TH))(?=[A-Z])", r"\1 ", t)
    t = t.replace("_", " ")                                          # file names
    # a date is not a floor: "FLOOR PLAN-_06_05_2022_R2" (285 FD Tower 2)
    t = re.sub(r"(?<!\d)\d{1,2}\s*[-./ ]\s*\d{1,2}\s*[-./ ]\s*(?:19|20)\d{2}(?!\d)", " ", t)
    t = re.sub(r"\bTYPICAL(?=FLOORS?\b)", "TYPICAL ", t)            # "TYPICALFLOORS"
    t = re.sub(r"\bMEZ+AN+INE\b", "MEZZANINE", t)                   # "MEZANINE"
    # a tower, block or wing number is not a floor: "TOWER -02 TERRACE PLAN"
    t = re.sub(r"\b(TOWER|TWR|BLOCK|BLK|WING|ZONE|PHASE|T)\s*[-.#]?\s*\d{1,3}\b", r"\1", t)
    if not t:
        return []
    refuge = "REFUGE" in t

    found = {}

    def add(key, label):
        found.setdefault(key, label)

    def add_floor(n):
        if 0 < n <= 200:
            add(n, "{0} Floor".format(ordinal(n)))

    def add_range(a, b):
        if 0 < a <= b <= 200 and b - a <= 100:
            for n in range(a, b + 1):
                add_floor(n)

    # Ranges first: "5TH TO 9TH", "5TH - 9TH", "4th-7th", "5TH TO 8".
    for m in re.finditer(_RANGE, t):
        if re.search(r"\d\s*(?:ST|ND|RD|TH)\b", m.group(0)):
            add_range(int(m.group(1)), int(m.group(2)))
    for m in re.finditer(_ORD, t):
        add_floor(int(m.group(1)))

    # Plain numbers, only in a floor title that names no floor by its
    # ordinal - "A-106 4th FLOOR PLAN (OFFICE FLOOR 01)" is the 4th floor,
    # the 01 is the sheet's own numbering:
    #   "TYPICAL FLOOR PLAN 5 TO 8", "FLOORS 5-8", "FLOOR 5", "LEVEL-05",
    #   "FLOORS 2, 4 & 6", "FLOOR PLAN (5,6,7)", "FLOOR 2 TO 4 & 6".
    if not any(k > 0 for k in found) and (_FLOOR_WORD.search(t) or re.search(r"\bREFUGE\s*\d", t)):
        for m in re.finditer(_RANGE, t):
            add_range(int(m.group(1)), int(m.group(2)))
        for m in re.finditer(_PLAIN_LIST, t):
            for one in re.finditer(_ITEM, m.group(1)):
                a = int(one.group(1))
                add_range(a, int(one.group(2)) if one.group(2) else a)
        if not any(k > 0 for k in found):
            for m in re.finditer(_LIST_BEFORE, t):
                for one in re.finditer(_ITEM, m.group(1)):
                    a = int(one.group(1))
                    add_range(a, int(one.group(2)) if one.group(2) else a)

    if re.search(r"\bLOWER\s+GROUND\b", t):
        add(-0.5, "Lower Ground Floor")
    if re.search(r"\bUPPER\s+GROUND\b", t):
        add(0.5, "Upper Ground Floor")
    # "GROUND FLOOR MEZZANINE" is the mezzanine over the ground floor, not
    # the ground floor too; "GROUND & MEZZANINE FLOOR PLAN" is both.
    mezz_only = bool(re.search(r"\bGROUND\b[^&,]{0,20}?(?<!\bAND )\bMEZZANINE\b", t)) and not re.search(r"\bGROUND\b.*\b(AND|&)\b.*\bMEZZANINE\b", t)
    if re.search(r"(?<!LOWER )(?<!UPPER )\bGROUND\b", t) and not re.search(
            r"\b(LOWER|UPPER)\s+GROUND\b", t) and not mezz_only:
        add(_KEY_GROUND, "Ground Floor")
    for m in re.finditer(r"\bBASEMENT\s*[-_]?\s*(\d{1,2})\b|\bB[-_ ]?(\d)\b", t):
        n = int(m.group(1) or m.group(2))
        add(-n, "Basement {0}".format(n))
    if re.search(r"\bBASEMENT\b", t) and not any(k < 0 for k in found):
        add(-1, "Basement")
    for m in re.finditer(r"\bPODIUM\s*[-_]?\s*(\d{1,2})?\b", t):
        n = int(m.group(1) or 1)
        add(0.1 * n, "Podium {0}".format(n) if m.group(1) else "Podium")
    if re.search(r"\bSTILT\b", t):
        add(0.05, "Stilt Floor")
    if re.search(r"\bMEZZANINE\b", t):
        add(0.7, "Mezzanine Floor")
    # "Upper Terrace" is a floor of its own above the terrace, not the
    # terrace again (two plates claiming one floor in Step 2).
    if re.search(r"\bUPPER\s+TERRACE\b", t):
        add(_KEY_TERRACE + 0.5, "Upper Terrace Floor")
    # "58 FLOOR (TERRACE) SKY LOUNGE": the 58th floor is the terrace, one floor.
    named_terrace = bool(re.search(r"\(\s*TERRACE\s*\)", t)) and any(isinstance(k, int) and 0 < k < 1000 for k in found)
    if re.search(r"(?<!UPPER )\bTERRACE\b", t) and not re.search(r"\bTERRACE\s+BELOW\b", t) and not named_terrace:
        add(_KEY_TERRACE, "Terrace Floor")
    if re.search(r"\bROOF\b", t):
        add(_KEY_ROOF, "Roof")

    return [{"key": k, "label": found[k], "refuge": refuge}
            for k in sorted(found)]


def tower_of(text: str):
    """'TOWER-1', 'Tower 2A', 'T3 TOWER' -> 'TOWER 1' etc., else None."""
    t = clean_title(text).upper()
    m = re.fullmatch(r"TOWER\s*[-_ ]?\s*([A-Z]?\d+[A-Z]?|[A-Z])", t)
    if m:
        return "TOWER " + m.group(1)
    return None


def tower_from_name(name: str):
    """A tower named in a file name: 'T1_TYPICAL...' or 'Tower-2 plans'."""
    t = (name or "").upper()
    m = re.search(r"\bTOWER\s*[-_ ]?\s*([A-Z]?\d+[A-Z]?)\b", t.replace("_", " "))
    if m:
        return "TOWER " + m.group(1)
    m = re.match(r"^T[-_ ]?(\d{1,2}[A-Z]?)[\s_-]", t)
    if m:
        return "TOWER " + m.group(1)
    return None


def floor_name(title: str) -> str:
    """A short floor name from a plan title, for naming a plate in Step 2.

        1ST FLOOR PLAN                           -> 1st Floor
        TYPICAL FLOOR PLAN-(2nd,4th,6th & 10th)  -> Typical 2nd, 4th, 6th & 10th Floor
        TYPICAL FLOOR PLAN 5TH TO 9TH            -> Typical 5th to 9th Floor
        12TH REFUGE FLOOR PLAN                   -> 12th Floor (Refuge)
        TERRACE FLOOR PLAN                       -> Terrace Floor

    Returns "" when the title names no floor.
    """
    levels = parse_levels(title)
    if not levels:
        return ""
    labels = [lv["label"] for lv in levels]
    floors = all(lab.endswith(" Floor") for lab in labels)
    parts = [lab[:-len(" Floor")] if floors else lab for lab in labels]

    # A title written as a range ("5TH TO 9TH") stays a range; floors the
    # title lists one by one stay listed, as the drawing has them.
    ranged = re.search(_RANGE, _ordinal_words(clean_title(title).upper()))
    keys = [lv["key"] for lv in levels]
    grouped, i = [], 0
    while i < len(parts):
        j = i
        while (ranged and j + 1 < len(parts) and isinstance(keys[j], int)
               and keys[j + 1] == keys[j] + 1):
            j += 1
        if j - i >= 2:
            grouped.append("{0} to {1}".format(parts[i], parts[j]))
        else:
            grouped.extend(parts[i:j + 1])
        i = j + 1

    upper = clean_title(title).upper()
    if re.search(r"\bUPPER\s+TERRACE\b", upper):
        grouped = ["Upper Terrace" if g == "Terrace" else
                   "Upper Terrace Floor" if g == "Terrace Floor" else g
                   for g in grouped]
    text = grouped[0] if len(grouped) == 1 else (
        ", ".join(grouped[:-1]) + " & " + grouped[-1])
    if floors:
        text += " Floor"
    if len(levels) > 1 or re.search(r"\bTYPICAL\b", upper):
        text = "Typical " + text
    if levels[0]["refuge"]:
        text += " (Refuge)"
    # One floor drawn as several wings: "6th FLOOR PLAN (... WING-B)".
    wing = re.search(r"\bWING\s*[-_ ]?\s*([A-Z0-9]{1,3})\b", upper)
    if wing:
        text += " - Wing " + wing.group(1)
    return text
