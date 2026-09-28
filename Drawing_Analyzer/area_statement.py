# -*- coding: utf-8 -*-
"""Area Statement workbook, in the client's reference layout.

Built from the Step 4 area result. Each floor plate is measured once and
listed for every floor its title names - "TYPICAL FLOOR PLAN-(2nd,4th,6th
& 10th)" gives four rows - so a tower reads floor by floor, bottom to top.
Plates are grouped into towers by the TOWER heading on the sheet, else the
file name. Every measured room is filed under one column of the statement
- Toilets, Lobby, AHU room, Staircase, Lift shaft, each shaft type - by its
name first and its family second, and a column no floor uses is left out.

The layout and the formulas follow "Phoenix-14 Tower-01 01A Area Details
reference": Net BUA = gross - shafts - cutouts, plinth area = gross -
staircase - lift - shafts - cutouts, sale area = plinth / 0.745, sq.ft at
x10.764, and the two efficiencies. Everything derived is a live formula,
so a number corrected in Excel carries through.

Columns the drawing cannot fill (elevation area, refuge as the norms
require) are not shown. Shafts that have no column of their own in the
reference (toilet exhaust, fire hose reel risers, spare shafts) go to OTHER
SHAFTS & CUTOUTS, which is counted in the shaft total; leaving them out
would overstate Net BUA.

Free of ezdxf; openpyxl only.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .config import CONFIG
from .levels import parse_levels, tower_from_name

# -- columns ---------------------------------------------------------------
# (key, header, group). Keys are what the classification rules name.
COLUMNS = [
    ("sl", "Sl No", None),
    ("floor", "OFFICE FLOORS", None),
    ("unit", "UNIT", None),
    ("gross", "GROSS AREA\n(Drawing)", None),
    ("elevation", "Elevation Area", None),
    ("gross_pline", "GROSS AREA\nPline", None),
    ("toilets", "TOILETS", "COMMON AREA"),
    ("lobby", "LOBBY", "COMMON AREA"),
    ("ahu_room", "AHU ROOM", "COMMON AREA"),
    ("ele_room", "ELE ROOM", "COMMON AREA"),
    ("refuge_norms", "REFUGE (As per Norms)", "COMMON AREA"),
    ("refuge", "REFUGE (Provided)", "COMMON AREA"),
    ("balcony", "BALCONY", "COMMON AREA"),
    ("staircase", "STAIRCASE", "VERTICAL CONNECTIVITY"),
    ("lift", "LIFT SHAFT", "VERTICAL CONNECTIVITY"),
    ("dg", "DG EXHAUST SHAFT", "SHAFTS & CUTOUTS"),
    ("phe", "PHE LINE SHAFT", "SHAFTS & CUTOUTS"),
    ("fapa", "FAPA SHAFT", "SHAFTS & CUTOUTS"),
    ("stair_press", "STAIRCASE PRESSURE SHAFT", "SHAFTS & CUTOUTS"),
    ("ahu_shaft", "AHU SHAFT", "SHAFTS & CUTOUTS"),
    ("pantry_shaft", "PANTRY SHAFT", "SHAFTS & CUTOUTS"),
    ("bms", "BMS", "SHAFTS & CUTOUTS"),
    ("ten", "TENANT RISER (TEN)", "SHAFTS & CUTOUTS"),
    ("stp", "STP EXHAUST AIR SHAFT", "SHAFTS & CUTOUTS"),
    ("comm", "COMM. SHAFT", "SHAFTS & CUTOUTS"),
    ("elect_shaft", "ELECT. SHAFTS", "SHAFTS & CUTOUTS"),
    ("lobby_prez", "LIFT LOBBY PREZ SHAFT", "SHAFTS & CUTOUTS"),
    ("other_shaft", "OTHER SHAFTS & CUTOUTS", "SHAFTS & CUTOUTS"),
    ("double_cutout", "DOUBLE HEIGHT BALCONY CUTOUT", "SHAFTS & CUTOUTS"),
    ("triple_cutout", "TRIPLE HEIGHT BALCONY CUTOUT", "SHAFTS & CUTOUTS"),
    ("total_shafts", "TOTAL SHAFTS & CUTOUTS", "SHAFTS & CUTOUTS"),
    ("lobby_double", "LOBBY (Double Height)", None),
    ("net_bua", "Net BUA", "BUA, PA, SA AND EFFICIENCY"),
    ("plinth", "PLINTH AREA", "BUA, PA, SA AND EFFICIENCY"),
    ("sale_sqm", "Sale area (sq.m)", "BUA, PA, SA AND EFFICIENCY"),
    ("sale_sft", "Sale area (sq.ft)", "BUA, PA, SA AND EFFICIENCY"),
    ("eff_sa", "BUA TO SA EFFICIENCY", "BUA, PA, SA AND EFFICIENCY"),
    ("eff_pa", "GROSS TO PA EFFICIENCY", "BUA, PA, SA AND EFFICIENCY"),
]
KEYS = [c[0] for c in COLUMNS]
FIRST_COL = 2                                   # column B, as in the reference
# Measured columns: filled from the rooms. Shafts are summed into the total.
MEASURED = ["toilets", "lobby", "ahu_room", "ele_room", "refuge", "balcony",
            "staircase", "lift", "dg", "phe", "fapa", "stair_press",
            "ahu_shaft", "pantry_shaft", "bms", "ten", "stp", "comm",
            "elect_shaft", "lobby_prez", "other_shaft", "double_cutout",
            "triple_cutout", "lobby_double"]
SHAFT_KEYS = ["dg", "phe", "fapa", "stair_press", "ahu_shaft", "pantry_shaft",
              "bms", "ten", "stp", "comm", "elect_shaft", "lobby_prez",
              "other_shaft"]
HEADER_OF = {c[0]: c[1] for c in COLUMNS}

# -- classification rules --------------------------------------------------
# Tried in order, first match wins. `any` are regular expressions tried on
# the room name, upper-cased with punctuation turned into spaces
# ("LOBBY PREZZ." -> "LOBBY PREZZ"); `not` vetoes; `families` matches the
# room's family when no pattern does. The order matters: a pressurisation
# shaft must be caught before STAIRCASE, a lift lobby before LIFT.
DEFAULT_RULES = [
    # Staircase pressurisation: STAIR PREZ, STAIR PR, STAIRCASE PRE.SHAFT,
    # PRESS. SHAFT, a bare PRESS - never one that names a lobby or a lift.
    {"column": "stair_press", "any": [r"\bSTAIR\w* PR\w*\b",
                                      r"\bPRESS\w*\b.*\b(SH|SHAFT)\b",
                                      r"\bSHAFT PRESS", r"^PRESS\w*$"],
     "not": [r"\bLOBBY\b", r"\bLIFT\b"]},
    {"column": "lobby_prez", "any": [r"\bLOBBY PRE[SZ]", r"\bLIFT LOBBY PRES",
                                     r"\bLIFT PRE[SZ]?\w*\b", r"\bPREZZ?\b"]},
    {"column": "double_cutout", "any": [r"DOUBLE HEIGHT.*CUT", r"CUT.*DOUBLE HEIGHT"]},
    {"column": "triple_cutout", "any": [r"TRIPLE HEIGHT.*CUT", r"CUT.*TRIPLE HEIGHT"]},
    {"column": "lobby_double", "any": [r"\bLOBBY\b.*DOUBLE HEIGHT",
                                       r"DOUBLE HEIGHT.*\bLOBBY\b"]},
    {"column": "dg", "any": [r"\bDG\b"]},
    {"column": "phe", "any": [r"\bPHE\b", r"\bPLUMB\w* SHAFT\b", r"\bRWP\b"]},
    {"column": "fapa", "any": [r"\bFAPA\b", r"\bFA PA\b", r"\bPA FA\b"]},
    {"column": "pantry_shaft", "any": [r"\bPANTRY\b.*\bSHAFT\b"]},
    {"column": "bms", "any": [r"\bBMS\b"]},
    {"column": "ten", "any": [r"\bTEN\b", r"\bTENANT\b"]},
    {"column": "stp", "any": [r"\bSTP\b"]},
    {"column": "comm", "any": [r"\bCOMM?\b"]},
    {"column": "elect_shaft", "any": [r"\bELE\w*\b.*\bSHAFT\b", r"\bE SHAFT\b",
                                      r"^ELEC$"]},
    {"column": "ahu_shaft", "any": [r"\bAHU\b.*\b(SHAFT|PIPE|DUCT)\b", r"\bCHW\b",
                                    r"\bTFA\b", r"\bCHILL\w*\b", r"\bCH PIPE\b"]},
    {"column": "other_shaft", "any": [r"\bEXHAUST\b", r"\bT EX\b",
                                      r"\bCUT ?OUT\b"]},
    {"column": "ahu_room", "any": [r"\bAHU\b"]},
    {"column": "ele_room", "any": [r"\bELE\w*\b"], "families": ["Electrical"]},
    {"column": "refuge", "any": [r"\bREFUGE\b"]},
    {"column": "balcony", "any": [r"\bBALCON\w*\b"],
     "families": ["Balcony / Open"]},
    {"column": "toilets", "any": [r"\bTOILET\w*\b", r"\bJANIT\w*\b", r"\bPCT\b",
                                  r"\bWASH ?ROOM\b", r"\bREST ?ROOM\b",
                                  r"\bWC\b", r"\bLADIES\b", r"\bGENTS\b"],
     "families": ["Toilet"]},
    {"column": "staircase", "any": [r"\bSTAIR\w*\b", r"\bST \d+\b",
                                    r"\bFIRE TOWER\b"],
     "families": ["Staircase"]},
    {"column": "lobby", "any": [r"\bLOBBY\b", r"\bCORRIDOR\b", r"\bPASSAGE\b",
                                r"\bFOYER\b"],
     "families": ["Circulation"]},
    {"column": "lift", "any": [r"\bLIFT\b", r"\bELEVATOR\b"],
     "families": ["Lift"]},
    {"column": "other_shaft", "any": [r"\bSHAFT\b", r"\bFHR\b", r"\bSPARE\b",
                                      r"\bDUCT\b", r"\bRISER\b"],
     "families": ["Shaft", "Fire / Life Safety"]},
]


def _settings(cfg=None):
    s = (cfg or CONFIG).get("area_statement") or {}
    return {
        "rules": s.get("rules") or DEFAULT_RULES,
        "sale_factor": float(s.get("sale_area_factor", 0.745)),
        "sqft": float(s.get("sqft_per_sqm", 10.764)),
    }


def _norm(name: str) -> str:
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", (name or "").upper()).split())


def classify(name: str, family: str = "", rules=None) -> Optional[str]:
    """The statement column a room belongs to, or None when it has none
    (an office, a store, a pantry - usable area, not deducted)."""
    n = _norm(name)
    for rule in (rules or DEFAULT_RULES):
        if any(re.search(p, n) for p in rule.get("not", ())):
            continue
        if any(re.search(p, n) for p in rule.get("any", ())):
            return rule["column"]
    for rule in (rules or DEFAULT_RULES):
        if family and family in rule.get("families", ()):
            if any(re.search(p, n) for p in rule.get("not", ())):
                continue
            return rule["column"]
    return None


_LEVEL_PATTERNS = [
    (r"\b(\d{1,3})\s*(ST|ND|RD|TH)\s+FLOOR\b",
     lambda m: "{0}{1} FLOOR".format(m.group(1), m.group(2).lower())),
    (r"\bGROUND\s+FLOOR\b", lambda m: "GROUND FLOOR"),
    (r"\bBASEMENT\s*[-_]?\s*(\d+)\b", lambda m: "BASEMENT {0}".format(m.group(1))),
    (r"\bTERRACE\b", lambda m: "TERRACE"),
    (r"\bLEVEL\s*[-_]?\s*(\d+)\b", lambda m: "LEVEL-{0}".format(m.group(1))),
]


def level_name(drawing_name: str) -> str:
    """The floor a drawing shows, from its file name: 'A-109 7th FLOOR
    (OFFICE FLOOR 04).dxf' -> '7th FLOOR'. Falls back to the file name."""
    raw = re.sub(r"\.dxf$", "", drawing_name or "", flags=re.I)
    text = re.sub(r"[_]+", " ", raw).upper()
    for pat, fmt in _LEVEL_PATTERNS:
        m = re.search(pat, text)
        if m:
            return fmt(m)
    return raw.strip() or "FLOOR"


def _plate_levels(f, drawing_name):
    """The floors one plate stands for: its title, else the file name."""
    levels = list(f.get("levels") or [])
    if levels:
        return levels
    levels = parse_levels(level_name(drawing_name))
    if levels:
        return levels
    return [{"key": 0, "label": str(f.get("name") or "Floor"), "refuge": False}]


def build_statement(area_data: Dict, drawing_name: str = "", cfg=None) -> Dict:
    """Towers, each with one row per floor, plus the room list.

    Each floor plate is measured once and repeated for every floor its
    title names, so a typical plate for 2nd, 4th, 6th and 10th gives four
    identical rows. Plates are grouped into towers by the TOWER heading on
    the sheet, else the file name. Two plates standing for the same floor
    with nothing to tell their towers apart are two towers.
    """
    st = _settings(cfg)
    floors = area_data.get("floors") or []
    if not floors:
        floors = [{"name": area_data.get("floor_label") or "Floor",
                   "area_sqm": area_data.get("floor_area_sqm") or 0.0}]
    file_tower = tower_from_name(drawing_name)

    plates = []
    for f in floors:
        sums = {k: 0.0 for k in MEASURED}
        rooms = []
        for r in area_data.get("rooms") or []:
            if len(floors) > 1 and r.get("floor") != f.get("name"):
                continue
            col = classify(r.get("name", ""), r.get("family", ""), st["rules"])
            if col:
                sums[col] += float(r.get("area_sqm") or 0.0)
            rooms.append((r, col))
        plates.append({"floor": f, "levels": _plate_levels(f, drawing_name),
                       "values": {k: round(v, 2) for k, v in sums.items()},
                       "rooms": rooms,
                       "tower": f.get("tower") or file_tower})

    # Group into towers. A plate whose floors are already taken in its
    # tower belongs to another tower of the same name on this sheet.
    towers = []
    for p in plates:
        keys = {lv["key"] for lv in p["levels"]}
        base = p["tower"] or "TOWER"
        for t in towers:
            if t["base"] == base and not (t["keys"] & keys):
                t["plates"].append(p)
                t["keys"] |= keys
                break
        else:
            towers.append({"base": base, "plates": [p], "keys": set(keys)})
    counts = {}
    for t in towers:
        counts[t["base"]] = counts.get(t["base"], 0) + 1
    seen = {}
    for t in towers:
        seen[t["base"]] = seen.get(t["base"], 0) + 1
        if t["base"] == "TOWER":
            t["title"] = "TOWER {0}".format(seen["TOWER"]) \
                if counts["TOWER"] > 1 else "TOWER 1"
        elif counts[t["base"]] > 1:
            t["title"] = "{0} ({1})".format(t["base"], seen[t["base"]])
        else:
            t["title"] = t["base"]

    out_towers, rooms_out = [], []
    for t in towers:
        rows = []
        for p in t["plates"]:
            for lv in p["levels"]:
                label = lv["label"] + (" (Refuge)" if lv.get("refuge") else "")
                rows.append({"key": lv["key"], "level": label,
                             "gross": float(p["floor"].get("area_sqm") or 0.0),
                             "values": dict(p["values"]),
                             "plate": p["floor"].get("title")
                             or p["floor"].get("name", "")})
            floors_txt = ", ".join(lv["label"] for lv in p["levels"])
            for r, col in p["rooms"]:
                rooms_out.append({"tower": t["title"], "level": floors_txt,
                                  "name": r.get("name", ""),
                                  "family": r.get("family", ""),
                                  "area_sqm": float(r.get("area_sqm") or 0.0),
                                  "column": HEADER_OF.get(col) if col else None})
        rows.sort(key=lambda r: r["key"])
        out_towers.append({"title": t["title"], "rows": rows})
    out_towers.sort(key=lambda t: _natural(t["title"]))
    return {"towers": out_towers, "rooms": rooms_out,
            "sale_factor": st["sale_factor"], "sqft": st["sqft"]}


def _natural(text):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", text)]


# -- workbook --------------------------------------------------------------
NAVY = "1F3A5F"
_THIN = Side(border_style="thin", color="8EA0B8")
BORDER = Border(top=_THIN, bottom=_THIN, left=_THIN, right=_THIN)
HEAD_FILL = PatternFill("solid", fgColor=NAVY)
GROUP_FILL = PatternFill("solid", fgColor="D9E2F1")
TITLE_FILL = PatternFill("solid", fgColor="E6EEF8")
TOTAL_FILL = PatternFill("solid", fgColor="F2F5FA")
WHITE_BOLD = Font(bold=True, color="FFFFFF", size=10)
BOLD = Font(bold=True)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center")

# Always shown; the measured columns in between appear only when some floor
# of the statement has an area in them.
_FIXED_HEAD = ["sl", "floor", "gross"]
_FIXED_TAIL = ["total_shafts", "net_bua", "plinth", "sale_sqm", "sale_sft",
               "eff_sa", "eff_pa"]
_HEADERS = dict(HEADER_OF)
_HEADERS.update({"floor": "FLOOR", "gross": "GROSS AREA",
                 "total_shafts": "TOTAL SHAFTS & CUTOUTS"})


def _active_keys(data):
    used = set()
    for t in data["towers"]:
        for r in t["rows"]:
            used |= {k for k, v in r["values"].items() if v}
    middle = [k for k in KEYS if k in MEASURED and k in used]
    return _FIXED_HEAD + middle + _FIXED_TAIL


class _Sheet:
    """Column letters for the columns actually shown."""

    def __init__(self, keys):
        self.keys = keys
        self.col = {k: get_column_letter(FIRST_COL + i) for i, k in enumerate(keys)}
        self.last = get_column_letter(FIRST_COL + len(keys) - 1)

    def has(self, k):
        return k in self.col

    def ref(self, k, r):
        return "{0}{1}".format(self.col[k], r)

    def formulas(self, r, sale_factor, sqft):
        """The reference sheet's formulas, over the columns present."""
        shafts = [self.ref(k, r) for k in SHAFT_KEYS if self.has(k)]
        cut = "".join("-" + self.ref(k, r) for k in ("double_cutout",
                                                    "triple_cutout")
                      if self.has(k))
        vc = "".join("-" + self.ref(k, r) for k in ("staircase", "lift")
                     if self.has(k))
        g = self.ref("gross", r)
        return {
            "total_shafts": "=" + ("+".join(shafts) if shafts else "0"),
            "net_bua": "={0}-{1}{2}".format(g, self.ref("total_shafts", r), cut),
            "plinth": "={0}{1}-{2}{3}".format(g, vc, self.ref("total_shafts", r),
                                              cut),
            "sale_sqm": "={0}/{1}".format(self.ref("plinth", r), sale_factor),
            "sale_sft": "={0}*{1}".format(self.ref("sale_sqm", r), sqft),
            "eff_sa": "=IFERROR({0}/{1},0)".format(g, self.ref("sale_sqm", r)),
            "eff_pa": "=IFERROR({0}/{1},0)".format(g, self.ref("plinth", r)),
        }


def _band(ws, sh, row, text, size=12, fill=TITLE_FILL, height=None,
          font_color=NAVY):
    ws.merge_cells("{0}{1}:{2}{1}".format(get_column_letter(FIRST_COL), row,
                                          sh.last))
    c = ws.cell(row=row, column=FIRST_COL, value=text)
    c.font = Font(bold=True, size=size, color=font_color)
    c.alignment = CENTER
    c.fill = fill
    if height:
        ws.row_dimensions[row].height = height
    return row + 1


def _headers(ws, sh, row):
    """Two header rows: the groups, then the columns under them."""
    groups = []
    group_of = {c[0]: c[2] for c in COLUMNS}
    for i, key in enumerate(sh.keys):
        col = FIRST_COL + i
        group = group_of.get(key)
        if group and groups and groups[-1][0] == group:
            groups[-1][2] = col
        elif group:
            groups.append([group, col, col])
        else:
            ws.merge_cells(start_row=row, start_column=col,
                           end_row=row + 1, end_column=col)
        c = ws.cell(row=row + (1 if group else 0), column=col,
                    value=_HEADERS.get(key, key))
        c.font = WHITE_BOLD
        c.fill = HEAD_FILL
        c.alignment = CENTER
    for group, c0, c1 in groups:
        if c1 > c0:
            ws.merge_cells(start_row=row, start_column=c0, end_row=row,
                           end_column=c1)
        c = ws.cell(row=row, column=c0, value=group)
        c.font = Font(bold=True, color=NAVY)
        c.fill = GROUP_FILL
        c.alignment = CENTER
    for r in (row, row + 1):
        for i in range(len(sh.keys)):
            ws.cell(row=r, column=FIRST_COL + i).border = BORDER
    ws.row_dimensions[row + 1].height = 48
    return row + 2


# The figures a reader looks for first each keep one tint down the table.
KEY_FILLS = {
    "gross": PatternFill("solid", fgColor="DDEBF7"),         # blue
    "total_shafts": PatternFill("solid", fgColor="FCE4E4"),  # rose
    "net_bua": PatternFill("solid", fgColor="E2EFDA"),       # green
    "plinth": PatternFill("solid", fgColor="E2EFDA"),
    "sale_sqm": PatternFill("solid", fgColor="FFF2CC"),      # gold
    "sale_sft": PatternFill("solid", fgColor="FFF2CC"),
    "eff_sa": PatternFill("solid", fgColor="EDE7F6"),        # lavender
    "eff_pa": PatternFill("solid", fgColor="EDE7F6"),
}
TOTAL_BAND = PatternFill("solid", fgColor="FFD966")          # strong gold
REFUGE_RED = "C00000"


def _style_row(ws, sh, r, fill=None, bold=False, refuge=False, total=False):
    for i, k in enumerate(sh.keys):
        c = ws.cell(row=r, column=FIRST_COL + i)
        c.border = BORDER
        if total:
            c.fill = TOTAL_BAND
        elif fill:
            c.fill = fill
        elif k in KEY_FILLS:
            c.fill = KEY_FILLS[k]
        if total or bold:
            c.font = Font(bold=True, color=NAVY)
        if refuge:
            c.font = Font(bold=(k == "floor"), color=REFUGE_RED)
        if k in ("eff_sa", "eff_pa"):
            c.number_format = "0.00%"
        elif k not in ("sl", "floor"):
            c.number_format = "#,##0.00"


def write_area_statement(area_data: Dict, out_path: str,
                         drawing_name: str = "", project_name: str = "",
                         cfg=None) -> Dict:
    data = build_statement(area_data, drawing_name, cfg)
    sf, sqft = data["sale_factor"], data["sqft"]
    sh = _Sheet(_active_keys(data))
    wb = Workbook()
    ws = wb.active
    ws.title = "Area Statement"
    ws.sheet_view.showGridLines = False

    project = (project_name or "").strip() or "-"
    c = ws.cell(row=2, column=FIRST_COL, value="Project Name: " + project)
    c.font = Font(bold=True, size=16, color=NAVY)
    c.alignment = LEFT
    c = ws.cell(row=3, column=FIRST_COL,
                value="Area Statement  -  from " + (drawing_name or "the drawing")
                + "  -  all areas in sq.m unless stated")
    c.font = Font(italic=True, size=10, color="465A7A")
    legend = [("Red rows", "Refuge floors", None, REFUGE_RED),
              ("Gross area", "", KEY_FILLS["gross"], NAVY),
              ("Shafts", "", KEY_FILLS["total_shafts"], NAVY),
              ("Net BUA / Plinth", "", KEY_FILLS["net_bua"], NAVY),
              ("Sale area", "", KEY_FILLS["sale_sqm"], NAVY),
              ("Efficiency", "", KEY_FILLS["eff_sa"], NAVY),
              ("Totals", "", TOTAL_BAND, NAVY)]
    col = FIRST_COL + 1                 # the FLOOR column is wide enough
    for label, note, fill, color in legend:
        c = ws.cell(row=4, column=col, value=label + (": " + note if note else ""))
        c.font = Font(bold=True, size=9, color=color)
        c.alignment = CENTER
        c.border = BORDER
        if fill:
            c.fill = fill
        col += 1
    row = 6

    totals = []                                    # (title, total row)
    for t in data["towers"]:
        row = _band(ws, sh, row, t["title"], size=14, fill=HEAD_FILL,
                    height=24, font_color="FFFFFF")
        c = ws.cell(row=row, column=FIRST_COL, value="Area Details")
        c.font = Font(bold=True, size=12, color=NAVY)
        row += 1
        row = _headers(ws, sh, row)
        first = row
        for sl, rec in enumerate(t["rows"], start=1):
            ws[sh.ref("sl", row)] = sl
            ws[sh.ref("floor", row)] = rec["level"]
            ws[sh.ref("gross", row)] = round(rec["gross"], 2)
            for k, v in rec["values"].items():
                if sh.has(k):
                    ws[sh.ref(k, row)] = v
            for k, f in sh.formulas(row, sf, sqft).items():
                ws[sh.ref(k, row)] = f
            _style_row(ws, sh, row, refuge="(Refuge)" in rec["level"])
            row += 1
        last = row - 1
        ws[sh.ref("floor", row)] = "TOTAL ({0} floors)".format(len(t["rows"]))
        for k in sh.keys[2:]:
            if k in ("eff_sa", "eff_pa"):
                continue
            ws[sh.ref(k, row)] = "=SUM({0}{1}:{0}{2})".format(sh.col[k], first,
                                                             last)
        ws[sh.ref("eff_sa", row)] = "=IFERROR({0}/{1},0)".format(
            sh.ref("gross", row), sh.ref("sale_sqm", row))
        ws[sh.ref("eff_pa", row)] = "=IFERROR({0}/{1},0)".format(
            sh.ref("gross", row), sh.ref("plinth", row))
        _style_row(ws, sh, row, total=True)
        ws.row_dimensions[row].height = 20
        totals.append((t["title"], row))
        c = ws[sh.ref("floor", row + 1)]
        c.value = "GROSS AREA (sq.ft)"
        c.font = Font(bold=True, color="1F4E79")
        c = ws[sh.ref("gross", row + 1)]
        c.value = "={0}*{1}".format(sh.ref("gross", row), sqft)
        c.number_format = "#,##0.00"
        c.font = Font(bold=True, color="1F4E79")
        c.fill = KEY_FILLS["gross"]
        row += 4

    # Summary: every tower, then the project.
    row = _band(ws, sh, row, "SUMMARY", size=12, fill=GROUP_FILL)
    heads = ["S.No", "Tower", "GBUA (sq.m)", "GBUA (sq.ft)", "Sale area (sq.ft)"]
    for i, h in enumerate(heads):
        c = ws.cell(row=row, column=FIRST_COL + i, value=h)
        c.font = WHITE_BOLD; c.fill = HEAD_FILL; c.alignment = CENTER
        c.border = BORDER
    row += 1
    first = row
    for i, (title, trow) in enumerate(totals, start=1):
        vals = [i, title, "={0}".format(sh.ref("gross", trow)),
                "={0}*{1}".format(sh.ref("gross", trow), sqft),
                "={0}".format(sh.ref("sale_sft", trow))]
        tints = {2: KEY_FILLS["gross"], 3: KEY_FILLS["gross"],
                 4: KEY_FILLS["sale_sft"]}
        for j, v in enumerate(vals):
            c = ws.cell(row=row, column=FIRST_COL + j, value=v)
            c.border = BORDER
            if j >= 2:
                c.number_format = "#,##0.00"
                c.fill = tints[j]
        row += 1
    c = ws.cell(row=row, column=FIRST_COL + 1, value="Total")
    for j in (2, 3, 4):
        col = get_column_letter(FIRST_COL + j)
        c = ws.cell(row=row, column=FIRST_COL + j,
                    value="=SUM({0}{1}:{0}{2})".format(col, first, row - 1))
        c.number_format = "#,##0.00"
    for j in range(5):
        c = ws.cell(row=row, column=FIRST_COL + j)
        c.border = BORDER
        c.fill = TOTAL_BAND
        c.font = Font(bold=True, color=NAVY)
    ws.row_dimensions[row].height = 20

    ws.column_dimensions["A"].width = 2.6
    for k in sh.keys:
        ws.column_dimensions[sh.col[k]].width = {"sl": 7, "floor": 26,
                                                 "gross": 16}.get(k, 13)
    ws.freeze_panes = "{0}1".format(sh.col["gross"])

    # Where every room went, so the allocation can be checked and the rules
    # in config.json tuned.
    ws2 = wb.create_sheet("Room allocation")
    heads = ["Tower", "Floors (plate)", "Room", "Family", "Area (sq.m)",
             "Statement column"]
    for i, h in enumerate(heads, start=1):
        c = ws2.cell(row=1, column=i, value=h)
        c.font = WHITE_BOLD; c.fill = HEAD_FILL; c.alignment = CENTER
        c.border = BORDER
    for r, rec in enumerate(data["rooms"], start=2):
        vals = [rec["tower"], rec["level"], rec["name"], rec["family"],
                round(rec["area_sqm"], 2),
                rec["column"] or "- not in statement (usable area)"]
        for i, v in enumerate(vals, start=1):
            c = ws2.cell(row=r, column=i, value=v)
            c.border = BORDER
            if not rec["column"]:
                c.font = Font(color="8A94A6", italic=True)
    for i, w in enumerate([16, 40, 34, 20, 13, 34], start=1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "A2"
    ws2.auto_filter.ref = "A1:F{0}".format(max(1, len(data["rooms"]) + 1))

    wb.save(out_path)
    data["columns"] = [_HEADERS.get(k, k) for k in sh.keys]
    return data
