# -*- coding: utf-8 -*-
"""Excel writers for the derived reports.

Follows the same conventions as the existing areas workbook: navy headers,
banded borders, and live SUM formulas rather than baked-in totals, so the
client can edit a number and the sheet stays consistent.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

NAVY = "1F3A5F"
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill("solid", fgColor=NAVY)
TITLE_FONT = Font(bold=True, size=13, color=NAVY)
SUB_FONT = Font(bold=True, italic=True, size=11)
SUB_FILL = PatternFill("solid", fgColor="F2F5FA")
WARN_FILL = PatternFill("solid", fgColor="FDE7E7")
OK_FILL = PatternFill("solid", fgColor="EAF6EC")
CENTER = Alignment(horizontal="center", vertical="center")
_THIN = Side(border_style="thin", color="BFBFBF")
BORDER = Border(top=_THIN, bottom=_THIN, left=_THIN, right=_THIN)


def _title(ws, row, text):
    ws.cell(row=row, column=1, value=text).font = TITLE_FONT
    return row + 1


def _kv(ws, row, label, value, bold_value=False):
    ws.cell(row=row, column=1, value=label).font = Font(bold=True)
    c = ws.cell(row=row, column=2, value=value)
    if bold_value:
        c.font = Font(bold=True)
    return row + 1


def _headers(ws, row, headers, widths=None):
    for col, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=col, value=h)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = CENTER
        c.border = BORDER
    if widths:
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
    return row + 1


def _rows(ws, row, data: Sequence[Sequence], fill_when=None):
    for r in data:
        fill = fill_when(r) if fill_when else None
        for col, v in enumerate(r, start=1):
            c = ws.cell(row=row, column=col, value=v)
            c.border = BORDER
            if fill:
                c.fill = fill
        row += 1
    return row


def _total(ws, row, label, col_letter, first, last, label_col=2, value_col=3):
    if last < first:
        return row
    c = ws.cell(row=row, column=label_col, value=label)
    c.font = SUB_FONT
    c.fill = SUB_FILL
    v = ws.cell(row=row, column=value_col,
                value="=SUM({0}{1}:{0}{2})".format(col_letter, first, last))
    v.font = SUB_FONT
    v.fill = SUB_FILL
    v.number_format = "0.00"
    return row + 1


# -- layer inventory --------------------------------------------------------
def write_layers_excel(inventory: Dict, out_path: str, drawing_name: str = ""):
    """Two sheets: every layer in one flat table, and the groups summarised.

    Flat first, because the usual reason to open this is to search for a
    layer name and see what the program made of it.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Layers"
    summary = inventory.get("summary") or {}
    row = _title(ws, 1, "Drawing layers" + (" - " + drawing_name
                                            if drawing_name else ""))
    row = _kv(ws, row, "Layers in the file", summary.get("layers", 0))
    row = _kv(ws, row, "Layers carrying content",
              summary.get("layers_with_content", 0))
    row = _kv(ws, row, "Entities in modelspace", summary.get("entities", 0))
    row = _kv(ws, row, "Entities inside blocks",
              summary.get("entities_in_blocks", 0))
    row = _kv(ws, row, "Layers read as walls", summary.get("wall_layers", 0))
    row += 1

    row = _headers(ws, row, [
        "Layer", "Group", "Group is", "Family", "Discipline",
        "Modelspace", "In blocks", "Texts", "Closed polylines",
        "Counts as wall", "Colour", "Lineweight", "Linetype", "State",
        "Mostly"],
        [34, 20, 12, 18, 16, 12, 11, 8, 15, 15, 8, 11, 16, 12, 30])
    first = row
    data = []
    for L in inventory.get("layers", []):
        state = ", ".join([w for w, on in (("off", L.get("off")),
                                           ("frozen", L.get("frozen")),
                                           ("locked", L.get("locked"))) if on])
        data.append([
            L["name"], L["group"], L["group_kind"], L.get("family") or "",
            L.get("discipline") or "", L["entities"], L["in_blocks"],
            L["texts"], L["closed_polylines"], L["counts_as_wall"],
            L["color"], L["lineweight"], L.get("linetype", ""),
            state or "on", L.get("kinds", ""),
        ])
    row = _rows(ws, row, data,
                fill_when=lambda r: (WARN_FILL if r[5] + r[6] == 0 else None))
    ws.freeze_panes = ws.cell(row=first, column=1)

    ws2 = wb.create_sheet("Groups")
    row = _title(ws2, 1, "Layers by group")
    row += 1
    row = _headers(ws2, row, ["Group", "Group is", "Layers", "Entities",
                              "Texts", "Layer names"],
                   [24, 14, 9, 12, 9, 90])
    _rows(ws2, row, [[g["name"], g["kind"], g["layer_count"], g["entities"],
                      g["texts"],
                      ", ".join(L["name"] for L in g["layers"])]
                     for g in inventory.get("groups", [])])
    wb.save(out_path)


# -- quantity report --------------------------------------------------------
def _sheet_title(name, used):
    """A worksheet name Excel accepts: at most 31 characters, none of the
    characters Excel forbids, and unique in the workbook."""
    base = "".join("_" if ch in "[]:*?/\\" else ch for ch in str(name))[:31]
    base = base or "Sheet"
    title, n = base, 2
    while title.lower() in used:
        suffix = " ({0})".format(n)
        title = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(title.lower())
    return title


def _write_sections(ws, row, rep: Dict, plate_label: str = "Floor plate area"):
    """Reconciliation, perimeter and family split for one report - the whole
    sheet or a single floor. Returns the next free row."""
    rec = rep.get("reconciliation")
    if rec:
        row = _title(ws, row, "Area reconciliation")
        row = _headers(ws, row, ["Measure", "Value", "Unit"],
                       [34, 18, 12, 20, 18, 20])
        row = _rows(ws, row, [
            [plate_label, rec["floor_area_sqm"], "sq.m"],
            ["Measured room area", rec["measured_room_area_sqm"], "sq.m"],
            ["Unaccounted (circulation + walls)", rec["unaccounted_sqm"], "sq.m"],
            ["Coverage", rec["coverage_pct"], "%"],
            ["Rooms measured", rec["room_count"], "nos"],
        ])
        ws.cell(row=row, column=1, value=rec["verdict"]).font = Font(italic=True)
        row += 2

    per = rep.get("perimeter")
    if per:
        row = _title(ws, row, "Perimeter")
        row = _headers(ws, row, ["Measure", "Value", "Unit"])
        row = _rows(ws, row, [
            ["Floor outline perimeter", per.get("floor_perimeter_m") or "-", "m"],
            ["Total room perimeter", per["total_room_perimeter_m"], "m"],
            ["Longest room perimeter", per["longest_room_perimeter_m"], "m"],
            ["Shortest room perimeter", per["shortest_room_perimeter_m"], "m"],
        ])
        row += 1

    k = rep.get("kpis")
    if k:
        row = _title(ws, row, "Area split by family")
        row = _headers(ws, row, ["Family", "Count", "Area (sq.m)", "% of floor"])
        first = row
        row = _rows(ws, row, [[f["family"], f["count"], f["total_sqm"],
                               f["pct_of_floor"]] for f in k["family_split"]])
        row = _total(ws, row, "Total", "C", first, row - 1)
        row += 1
    return row


def _write_geometry(ws, row, gh: Dict):
    """The boundary geometry check for one report. Returns the next row."""
    row = _title(ws, row, "Boundary geometry check")
    row = _kv(ws, row, "Boundaries checked", gh["checked"])
    row = _kv(ws, row, "Issues found", gh["issue_count"], bold_value=True)
    row += 1
    if gh["findings"]:
        row = _headers(ws, row, ["Issue", "Boundary A", "Area A", "Boundary B",
                                 "Area B", "Detail"],
                       [18, 30, 12, 30, 12, 70])
        row = _rows(ws, row, [[f["issue"], f["room_a"], f["area_a_sqm"],
                               f["room_b"], f["area_b_sqm"], f["detail"]]
                              for f in gh["findings"]],
                    fill_when=lambda _r: WARN_FILL)
    else:
        ws.cell(row=row, column=1,
                value="No duplicate, contained, sliver or partially "
                      "overlapping boundaries found.").font = Font(italic=True)
        row += 1
    return row + 1


def write_quantities_excel(report: Dict, out_path: str, qa: Dict = None):
    wb = Workbook()
    used = {"summary", "geometry check", "extraction qa"}
    floors = report.get("floors") or []

    # Sheet 1: summary - every floor together
    ws = wb.active
    ws.title = "Summary"
    row = _title(ws, 1, "Derived Quantities and Checks"
                 + (" - all floors" if floors else ""))
    row += 1
    u = report.get("units") or {}
    row = _kv(ws, row, "Floor outline layer", report.get("floor_label") or "-")
    row = _kv(ws, row, "Drawing units", u.get("label", "-"))
    if u.get("assumed"):
        ws.cell(row=row, column=1,
                value="Units were not declared in the DXF header; millimetres "
                      "assumed. Override in the UI if that is wrong.").font = \
            Font(italic=True, color="B06A00")
        row += 1
    row += 1

    if floors:
        # One line per floor up front, so the plates can be compared at a
        # glance; each floor's own sheet follows.
        row = _title(ws, row, "Floor-wise summary")
        row = _headers(ws, row, ["Floor", "Plate area (sq.m)", "Rooms",
                                 "Measured (sq.m)", "Coverage %",
                                 "Geometry issues"],
                       [34, 18, 12, 20, 18, 20])
        first = row
        data = []
        for f in floors:
            rec = f.get("reconciliation") or {}
            gh = f.get("geometry_health") or {}
            data.append([f.get("name"), f.get("area_sqm"),
                         rec.get("room_count", 0),
                         rec.get("measured_room_area_sqm", 0.0),
                         rec.get("coverage_pct", 0.0),
                         gh.get("issue_count", 0)])
        row = _rows(ws, row, data)
        last = row - 1
        for col in ("B", "C", "D"):
            c = ws["{0}{1}".format(col, row)]
            c.value = "=SUM({0}{1}:{0}{2})".format(col, first, last)
            c.font = SUB_FONT
            c.fill = SUB_FILL
        ws.cell(row=row, column=1, value="All floors").font = SUB_FONT
        ws.cell(row=row, column=1).fill = SUB_FILL
        row += 2

    row = _write_sections(ws, row, report,
                          "Floor plate area (all floors)" if floors
                          else "Floor plate area")

    # Sheet 2: geometry health, all floors
    gh = report.get("geometry_health")
    if gh:
        ws2 = wb.create_sheet("Geometry Check")
        _write_geometry(ws2, 1, gh)

    # One sheet per floor: the same checks on that floor's rooms alone.
    for f in floors:
        wsf = wb.create_sheet(_sheet_title(f.get("name") or "Floor", used))
        r = _title(wsf, 1, "Derived Quantities and Checks - "
                   + str(f.get("name") or ""))
        r += 1
        r = _kv(wsf, r, "Floor", f.get("name") or "-")
        r = _kv(wsf, r, "Plate area (sq.m)", f.get("area_sqm"))
        r = _kv(wsf, r, "Drawing units", u.get("label", "-"))
        r += 1
        r = _write_sections(wsf, r, f)
        if f.get("geometry_health"):
            _write_geometry(wsf, r, f["geometry_health"])

    # Sheet 3: extraction QA
    if qa:
        ws3 = wb.create_sheet("Extraction QA")
        r = _title(ws3, 1, "Label to boundary outcome")
        r = _kv(ws3, r, "Boundaries drawn", qa["boundary_count"])
        r = _kv(ws3, r, "Labels with a problem", qa["problem_count"],
                bold_value=True)
        r += 1
        r = _headers(ws3, r, ["Floor", "Sr.No", "Label", "Layer", "Family",
                              "Outcome", "What it means"],
                     [22, 8, 34, 22, 18, 14, 66])
        _rows(ws3, r, [[x["floor"], x["sr_no"], x["text"], x["layer"],
                        x["family"], x["status"], x["meaning"]]
                       for x in qa["rows"]],
              fill_when=lambda row_: None)

    wb.save(out_path)


# -- revision comparison ----------------------------------------------------
def write_comparison_excel(cmp_data: Dict, out_path: str):
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    s = cmp_data["summary"]
    row = _title(ws, 1, "Revision quantity impact")
    row += 1
    row = _headers(ws, row, ["Measure", "Previous", "Current", "Delta"],
                   [34, 18, 18, 18])
    row = _rows(ws, row, [
        ["Floor plate area (sq.m)", s.get("old_floor_area_sqm"),
         s.get("new_floor_area_sqm"), ""],
        ["Total measured area (sq.m)", s["old_total_sqm"], s["new_total_sqm"],
         s["delta_sqm"]],
        ["Change (%)", "", "", s["delta_pct"]],
        ["Room count", s["old_room_count"], s["new_room_count"],
         s["new_room_count"] - s["old_room_count"]],
        ["Rooms added", "", "", s["added_count"]],
        ["Rooms removed", "", "", s["removed_count"]],
        ["Rooms changed", "", "", s["changed_count"]],
    ])

    ws2 = wb.create_sheet("Changed")
    r = _headers(ws2, 1, ["Room", "Family", "Previous (sq.m)", "Current (sq.m)",
                          "Delta (sq.m)", "Delta (%)"],
                 [34, 20, 18, 18, 16, 14])
    _rows(ws2, r, [[x["name"], x["family"], x["old_sqm"], x["new_sqm"],
                    x["delta_sqm"], x["delta_pct"]] for x in cmp_data["changed"]],
          fill_when=lambda row_: WARN_FILL if row_[4] and row_[4] < 0 else OK_FILL)

    ws3 = wb.create_sheet("Added")
    r = _headers(ws3, 1, ["Room", "Family", "Area (sq.m)"], [34, 20, 18])
    _rows(ws3, r, [[x["name"], x["family"], x["new_sqm"]]
                   for x in cmp_data["added"]], fill_when=lambda _r: OK_FILL)

    ws4 = wb.create_sheet("Removed")
    r = _headers(ws4, 1, ["Room", "Family", "Area (sq.m)"], [34, 20, 18])
    _rows(ws4, r, [[x["name"], x["family"], x["old_sqm"]]
                   for x in cmp_data["removed"]], fill_when=lambda _r: WARN_FILL)

    wb.save(out_path)
