# -*- coding: utf-8 -*-
"""Drawing_Analyzer - Flask routes.

Run:
    pip install -r requirements.txt
    python runserver.py

Then open http://127.0.0.1:5555
"""

import json
import os
import re
import uuid

from flask import (Flask, Response, abort, jsonify, render_template, request,
                   send_file)
from werkzeug.utils import secure_filename

from .config import CONFIG
from .dxf_reader import (MIN_FLOOR_AREA_SQM, _FAMILY_RULES, annotate_dxf,
                         extract_texts, family_by_name, get_floor_candidates,
                         list_families, list_family_names, list_family_rules,
                         save_family_rules, write_areas_excel, write_excel,
                         write_json)
from .area_statement import write_area_statement
from . import cadview
from .pipeline import analyse_areas, build_qa, list_layers
from .readback import read_drawn_boundaries
from .preview import build_preview, build_preview_from_shapes
from .quantities import compare_revisions
from .revisions import load_revision
from . import rules as rule_book
from . import userrules
from .reports import (write_comparison_excel, write_layers_excel,
                      write_quantities_excel)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
ALLOWED_EXT = {".dxf"}
REVISION_EXT = {".json", ".xlsx", ".xlsm", ".dxf"}

# A token is only ever a uuid4 hex string. Validating it before it reaches
# os.path.join is what stops a crafted token walking out of these folders.
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024  # 256 MB
# Templates are compiled once per process, but static/main.js is re-read
# from disk on every request. Editing index.html without restarting
# therefore served old HTML against new JS, and the missing element blew
# up mid-request as a bogus "Network error".
app.config["TEMPLATES_AUTO_RELOAD"] = True


def _is_allowed(filename):
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXT


def _valid_token(token):
    return bool(token) and bool(TOKEN_RE.match(str(token)))


def _paths(token):
    if not _valid_token(token):
        abort(400)
    return {
        "dxf":             os.path.join(UPLOAD_DIR, token + ".dxf"),
        "meta":            os.path.join(UPLOAD_DIR, token + ".meta.json"),
        "results":         os.path.join(UPLOAD_DIR, token + ".results.json"),
        "xlsx":            os.path.join(OUTPUT_DIR, token + ".xlsx"),
        "json":            os.path.join(OUTPUT_DIR, token + ".json"),
        "annotated":       os.path.join(OUTPUT_DIR, token + "_annotated.dxf"),
        # Section 4
        "areas_dxf":       os.path.join(UPLOAD_DIR, token + "_corrected.dxf"),
        "areas_xlsx":      os.path.join(OUTPUT_DIR, token + "_areas.xlsx"),
        "area_statement_xlsx": os.path.join(OUTPUT_DIR,
                                            token + "_area_statement.xlsx"),
        "areas_json":      os.path.join(OUTPUT_DIR, token + "_areas.json"),
        # Derived reports
        "quantities_xlsx": os.path.join(OUTPUT_DIR, token + "_quantities.xlsx"),
        "quantities_json": os.path.join(OUTPUT_DIR, token + "_quantities.json"),
        "qa_xlsx":         os.path.join(OUTPUT_DIR, token + "_qa.xlsx"),
        "qa_json":         os.path.join(OUTPUT_DIR, token + "_qa.json"),
        "preview":         os.path.join(OUTPUT_DIR, token + "_preview.json"),
        "preview_base":    os.path.join(OUTPUT_DIR, token + "_preview_base.json"),
        "rules_dxf":       os.path.join(OUTPUT_DIR, token + "_by_rules.dxf"),
        "layers_xlsx":     os.path.join(OUTPUT_DIR, token + "_layers.xlsx"),
        "layers_json":     os.path.join(OUTPUT_DIR, token + "_layers.json"),
        "compare_xlsx":    os.path.join(OUTPUT_DIR, token + "_comparison.xlsx"),
        "compare_json":    os.path.join(OUTPUT_DIR, token + "_comparison.json"),
    }


def _fail(message, code=400):
    """Errors the user can act on. Internal detail is logged, not returned."""
    return jsonify({"ok": False, "error": message}), code


def _log_exc(where, exc):
    app.logger.exception("%s failed: %s", where, exc)


@app.errorhandler(413)
def too_large(e):
    return jsonify({"ok": False,
                    "error": "File too large. Maximum upload size is 256 MB."}), 413


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/config")
def get_config():
    """Expose the tunables the UI needs, so nothing is duplicated in JS."""
    return jsonify({
        "ok": True,
        "min_floor_area_sqm": MIN_FLOOR_AREA_SQM,
        "unit_options": [
            {"value": "", "label": "Auto-detect from drawing"},
            {"value": "4", "label": "Millimetres"},
            {"value": "5", "label": "Centimetres"},
            {"value": "6", "label": "Metres"},
            {"value": "1", "label": "Inches"},
            {"value": "2", "label": "Feet"},
        ],
    })


# ------------------------------------------------------------- Family rules
@app.route("/my_rules", methods=["GET"])
def my_rules_get():
    """The user's own rules, plus the vocabulary the parser knows, so the
    UI can offer the names it will actually recognise."""
    return jsonify({
        "ok": True,
        "rules": userrules.load_rules(),
        "families": list_family_names(),
        "actions": [
            {"kind": "keep_only", "label": "Draw only these, drop the rest"},
            {"kind": "rectangle", "label": "Redraw as a rectangle"},
            {"kind": "drop", "label": "Do not draw"},
            {"kind": "snap", "label": "Snap corners to a grid"},
            {"kind": "grow", "label": "Grow or shrink"},
            {"kind": "set_family", "label": "File under a family"},
        ],
        "conditions": [
            {"kind": "everything", "label": "Every boundary"},
            {"kind": "family", "label": "A family"},
            {"kind": "family_in", "label": "Any of several families"},
            {"kind": "label", "label": "Label contains"},
            {"kind": "layer", "label": "On a layer"},
            {"kind": "area_below", "label": "Smaller than (sq.m)"},
            {"kind": "area_above", "label": "Larger than (sq.m)"},
            {"kind": "source", "label": "Came from"},
        ],
        "examples": [
            "draw only shaft boundaries",
            "draw only shaft, staircase and mep boundaries",
            "draw regular shapes only",
            "do not draw boundaries under 0.3 sq.m",
            "shaft marks should follow the angle",
            "snap every boundary to 25 mm",
            "grow Toilet boundaries by 100 mm",
            "put LIFT LOBBY under Staircase",
        ],
    })


@app.route("/my_rules/parse", methods=["POST"])
def my_rules_parse():
    """One sentence in, one rule out - or the questions it left open."""
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "")[:400]
    layers = []
    token = data.get("token")
    if _valid_token(token):
        paths = _paths(token)
        if os.path.exists(paths["meta"]):
            inv = (_read_json(paths["meta"]).get("layers") or {})
            layers = [L["name"] for L in (inv.get("layers") or [])][:400]
    return jsonify(dict(userrules.parse(text, list_family_names(), layers),
                        ok=True))


@app.route("/my_rules", methods=["POST"])
def my_rules_save():
    data = request.get_json(silent=True) or {}
    try:
        saved = userrules.save_rules(data.get("rules") or [])
    except ValueError as exc:
        return _fail(str(exc))
    except OSError as exc:
        _log_exc("my_rules/save", exc)
        return _fail("Could not write user_rules.json.", 500)
    return jsonify({"ok": True, "rules": saved})


@app.route("/my_rules/generate", methods=["POST"])
def my_rules_generate():
    """Draw the whole thing again, this time with the user's rules.

    This is a run of its own, written to its own file: Step 3 keeps the
    unrestricted drawing, and unticking a rule here can never quietly
    change what Step 3 produced.
    """
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    if not _valid_token(token):
        return _fail("Invalid token.")
    paths = _paths(token)
    if not os.path.exists(paths["dxf"]) or not os.path.exists(paths["results"]):
        return _fail("Run Step 1 and Step 2 first.", 404)
    try:
        rules = userrules.validate(data.get("rules") or [])
    except ValueError as exc:
        return _fail(str(exc))
    built_in = data.get("built_in")
    if built_in is not None and not isinstance(built_in, dict):
        return _fail("Invalid rules payload.")

    cache = _read_json(paths["results"])
    meta = _read_json(paths["meta"]) if os.path.exists(paths["meta"]) else {}
    try:
        stats = annotate_dxf(paths["dxf"], paths["rules_dxf"],
                             cache.get("selected", []),
                             cache.get("results", {}),
                             family_overrides={},
                             unit_override=meta.get("unit_override"),
                             rule_overrides=built_in,
                             user_rules=rules)
    except Exception as exc:                          # noqa: BLE001
        _log_exc("my_rules/generate", exc)
        return _fail("Drawing with these rules failed. See the server log.",
                     500)
    try:
        picture = build_preview(paths["rules_dxf"],
                                meta.get("unit_override"))
    except ValueError as exc:
        picture = None
        if not stats.get("drawn_boundaries"):
            return jsonify({"ok": True, "stats": stats, "preview": None,
                            "download": None,
                            "message": str(exc)})
    except Exception as exc:                          # noqa: BLE001
        _log_exc("my_rules/generate/preview", exc)
        picture = None
    return jsonify({"ok": True, "stats": stats, "preview": picture,
                    "download": "/download/rules_dxf/" + token,
                    "rule_report": stats.get("user_rule_report") or {},
                    "rules_off": stats.get("rules_changed") or []})


@app.route("/my_rules/preview", methods=["POST"])
def my_rules_preview():
    """What these rules would do to the boundaries already generated.

    Reads the annotated DXF rather than regenerating, so the answer comes
    back in a couple of seconds instead of a minute and a half, and it is
    measured on exactly the shapes the user is looking at.
    """
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    if not _valid_token(token):
        return _fail("Invalid token.")
    paths = _paths(token)
    if not os.path.exists(paths["annotated"]):
        return _fail("The quick preview measures your rules against the "
                     "drawing Step 3 produced, and that has not been run "
                     "yet. Press Generate with these rules instead - it "
                     "does not need Step 3.", 404)
    try:
        rules = userrules.validate(data.get("rules") or [])
    except ValueError as exc:
        return _fail(str(exc))
    try:
        items = read_drawn_boundaries(paths["annotated"])
    except Exception as exc:                          # noqa: BLE001
        _log_exc("my_rules/preview", exc)
        return _fail("Could not read the annotated DXF.", 500)
    kept, report = userrules.apply_rules(items, rules)
    before = round(sum(i["area_sqm"] for i in items), 2)
    after = round(sum(i["area_sqm"] for i in kept), 2)

    # The same picture the Step 3 viewer draws, but of what these rules
    # would leave behind - geometry that exists only in this request.
    picture, picture_error = None, None
    try:
        picture = build_preview_from_shapes(
            [{"layer": _layer_after_rules(i), "pts": i["shape"],
              "area": i["area_sqm"], "name": i.get("label") or ""}
             for i in kept])
    except ValueError as exc:
        picture_error = str(exc)
    except Exception as exc:                          # noqa: BLE001
        _log_exc("my_rules/preview/svg", exc)
        picture_error = "Could not draw the preview."

    return jsonify({"ok": True, "report": report,
                    "boundaries_before": len(items),
                    "boundaries_after": len(kept),
                    "area_before": before, "area_after": after,
                    "preview": picture, "preview_error": picture_error})


def _layer_after_rules(item):
    """A rule that refiles a boundary moves it onto that family's layer,
    so the preview colours it the way the run would."""
    forced = item.get("family_forced")
    if forced:
        fam = family_by_name(forced, _FAMILY_RULES)
        if fam:
            return (fam.get("layer") or "UNKNOWN").upper() + "_BOUNDARY"
    return item.get("layer") or "UNKNOWN_BOUNDARY"


@app.route("/boundary_rules", methods=["GET"])
def boundary_rules():
    """The rules the drawing pass applies, for the Step 3 checklist."""
    return jsonify({"ok": True,
                    "groups": rule_book.catalogue(),
                    "defaults": rule_book.defaults()})


@app.route("/family_rules", methods=["GET"])
def family_rules_get():
    return jsonify({"ok": True, "families": list_family_rules()})


@app.route("/family_rules", methods=["POST"])
def family_rules_post():
    """Save an edited ruleset and apply it without a restart."""
    data = request.get_json(silent=True) or {}
    try:
        saved = save_family_rules(data.get("families"))
    except ValueError as exc:
        return _fail(str(exc))
    except OSError as exc:
        _log_exc("family_rules/save", exc)
        return _fail("Could not write family_rules.json. Check that the file "
                     "is not open in another program and that the folder is "
                     "writable.", 500)
    return jsonify({"ok": True,
                    "families": list_family_rules(saved),
                    "family_list": list_family_names(),
                    "family_options": list_families()})


# ---------------------------------------------------------------- Section 1
@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return _fail("No file uploaded.")
    f = request.files["file"]
    if not f or f.filename == "":
        return _fail("Empty file.")
    if not _is_allowed(f.filename):
        return _fail("Only .dxf files are supported.")

    unit_override = request.form.get("units") or None
    # Optional. Carried into the annotated DXF so the Area Statement in
    # Step 4 can title itself even when the file comes back from CAD.
    project_name = " ".join((request.form.get("project") or "").split())[:120]
    # The floor-plate area range is required: which closed polylines are
    # floor plates depends on the project, not on a fixed threshold.
    try:
        min_area = float(request.form.get("min_area", ""))
        max_area = float(request.form.get("max_area", ""))
    except (TypeError, ValueError):
        return _fail("Enter the minimum and maximum floor plate area "
                     "(sq.m) before uploading.")
    if not (min_area > 0 and max_area > min_area):
        return _fail("The floor plate area range is not valid: the minimum "
                     "must be above 0 and the maximum above the minimum.")

    token = uuid.uuid4().hex
    paths = _paths(token)
    safe_name = secure_filename(f.filename)
    f.save(paths["dxf"])

    inventory = {}
    parsed = {}
    try:
        candidates, units, diag = get_floor_candidates(
            paths["dxf"], unit_override, inventory,
            min_area_sqm=min_area, max_area_sqm=max_area,
            drawing_name=f.filename or "", loaded_out=parsed)
    except Exception as exc:                          # noqa: BLE001
        _log_exc("upload/parse", exc)
        _quiet_remove(paths["dxf"])
        return _fail("Could not read this DXF. Check that the file is not "
                     "corrupt and was saved as a DXF, not a DWG.")

    # Units are resolved, not interrogated. The upload is only refused
    # when the user forced a unit that makes the drawing absurd AND the
    # geometry cannot suggest a better one; anything else proceeds with
    # the resolved unit and a note.
    if not diag.get("plausible", True) and units.overridden:
        _quiet_remove(paths["dxf"])
        return _fail("{0} Leave the unit on auto-detect, or pick the one "
                     "the file was actually drawn in. This setting "
                     "describes the file, not the output - areas are "
                     "always reported in square metres.".format(
                         diag.get("message") or ""))

    if not candidates:
        _quiet_remove(paths["dxf"])
        largest = diag["largest_areas_sqm"]
        detail = (" The largest closed polylines measure {0} sq.m.".format(
            ", ".join("{0:g}".format(a) for a in largest[:4]))
            if largest else " No closed polylines were found at all.")
        return _fail(
            "No closed polyline falls in the {0:g} - {1:g} sq.m floor-plate "
            "range entered. The drawing was read as {2} (typical room about "
            "{3:g} m).{4} Widen the range, or check that the floor outline "
            "is a closed LWPOLYLINE/POLYLINE.".format(
                min_area, max_area, units.short_label,
                diag.get("p95_m") or 0, detail))

    meta = {
        "original_name": safe_name,
        "project_name": project_name,
        "floor_area_range_sqm": [min_area, max_area],
        "units": units.to_ui(),
        "unit_override": unit_override,
        "diagnostics": diag,
        "candidates": [dict(list(c.to_ui().items()) + [("polygon", c.polygon)])
                       for c in candidates],
        "selected": [],
        "layers": inventory,
    }
    _write_json(paths["meta"], meta)

    # Section 6 reads the layer table that was collected in the parse
    # above. Both exports are written now, while the data is in hand, so
    # the buttons work the moment the section appears.
    try:
        _write_json(paths["layers_json"],
                    dict(inventory, drawing=safe_name,
                         units=units.to_ui()))
        write_layers_excel(inventory, paths["layers_xlsx"], safe_name)
    except Exception as exc:                          # noqa: BLE001
        _log_exc("upload/layers", exc)

    # The CAD viewer in Steps 2 and 3 needs the whole drawing; reading it
    # takes a few seconds, so it starts now and is usually ready by the
    # time the floor plates have been looked at.
    try:
        cadview.start(paths["dxf"], OUTPUT_DIR, token, "source",
                      unit_override, loaded=parsed.get("loaded"))
    except Exception as exc:                          # noqa: BLE001
        _log_exc("upload/cadview", exc)

    return jsonify({
        "ok": True,
        "token": token,
        "original_name": safe_name,
        "units": units.to_ui(),
        "diagnostics": diag,
        "candidates": [c.to_ui() for c in candidates],
        "cad": "/cadview/" + token,
        "layer_summary": inventory.get("summary") or {},
    })


@app.route("/layers/<token>")
def layers(token):
    """The layer inventory for Section 6, as read at upload."""
    if not _valid_token(token):
        return _fail("Invalid token.")
    paths = _paths(token)
    if not os.path.exists(paths["meta"]):
        return _fail("Upload expired or not found. Please upload again.", 404)
    meta = _read_json(paths["meta"])
    inv = meta.get("layers") or {}
    if not inv.get("layers"):
        return _fail("This upload carries no layer inventory. It was made "
                     "before the layer reader existed - upload the drawing "
                     "again to get one.", 404)
    return jsonify({
        "ok": True,
        "drawing": meta.get("original_name") or "",
        "summary": inv.get("summary") or {},
        "groups": inv.get("groups") or [],
        "download": {"layers_xlsx": "/download/layers_xlsx/" + token,
                     "layers_json": "/download/layers_json/" + token},
    })


# ---------------------------------------------------------------- Section 2
@app.route("/extract", methods=["POST"])
def extract():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    picks = data.get("floors")

    if not _valid_token(token):
        return _fail("Invalid token.")
    if not isinstance(picks, list) or not picks:
        return _fail("At least one floor is required.")

    paths = _paths(token)
    if not os.path.exists(paths["dxf"]) or not os.path.exists(paths["meta"]):
        return _fail("Upload expired or not found. Please upload again.", 404)

    meta = _read_json(paths["meta"])
    cand_by_idx = {c["index"]: c for c in meta["candidates"]}

    selected = []
    for p in picks:
        idx = p.get("index")
        name = (p.get("name") or ("Floor_" + str(idx + 1))).strip()
        if idx not in cand_by_idx:
            return _fail("Unknown floor index " + str(idx) + ".")
        selected.append({"index": idx, "name": name,
                         "polygon": cand_by_idx[idx]["polygon"]})

    unit_override = meta.get("unit_override")
    try:
        results = extract_texts(paths["dxf"], selected, unit_override)
    except Exception as exc:                          # noqa: BLE001
        _log_exc("extract", exc)
        return _fail("Text extraction failed. See the server log for detail.",
                     500)

    try:
        write_excel(results, paths["xlsx"])
        write_json(results, paths["json"])
    except Exception as exc:                          # noqa: BLE001
        _log_exc("extract/export", exc)
        return _fail("Could not write the export files.", 500)

    _write_json(paths["results"], {"results": results, "selected": selected})
    meta["selected"] = selected
    _write_json(paths["meta"], meta)

    for stale in ("annotated", "qa_xlsx", "qa_json", "preview", "preview_base"):
        _quiet_remove(paths[stale])

    return jsonify({
        "ok": True,
        "summary": [{"floor": name, "count": len(rows)}
                    for name, rows in results.items()],
        "results": results,
        "family_list": list_family_names(),
        "families": list_families(),
        "units": meta.get("units"),
        "download": {"xlsx": "/download/xlsx/" + token,
                     "json": "/download/json/" + token},
    })


# ---------------------------------------------------------------- Section 3
@app.route("/annotate", methods=["POST"])
def annotate():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    selections = data.get("selections") or {}
    family_overrides = data.get("families") or {}
    # Empty means every family, which is the default.
    family_filter = (data.get("family_filter") or "").strip()
    # An unticked rule is the user saying "do not apply this judgement";
    # anything absent keeps its shipped setting.
    rule_overrides = data.get("rules")
    if rule_overrides is not None and not isinstance(rule_overrides, dict):
        return _fail("Invalid rules payload.")

    if not _valid_token(token):
        return _fail("Invalid token.")
    if not isinstance(selections, dict):
        return _fail("Invalid selections payload.")
    if not isinstance(family_overrides, dict):
        family_overrides = {}
    if family_filter and family_filter not in list_family_names():
        return _fail("Unknown boundary type '{0}'.".format(family_filter))

    paths = _paths(token)
    if not os.path.exists(paths["dxf"]) or not os.path.exists(paths["results"]):
        return _fail("Run extraction first.", 404)

    cache = _read_json(paths["results"])
    full_results = cache.get("results", {})
    selected = cache.get("selected", [])
    meta = _read_json(paths["meta"]) if os.path.exists(paths["meta"]) else {}

    filtered, total_kept, total_dropped = _filter_selections(full_results,
                                                              selections)

    try:
        stats = annotate_dxf(paths["dxf"], paths["annotated"], selected,
                             filtered, family_overrides=family_overrides,
                             unit_override=meta.get("unit_override"),
                             family_filter=family_filter,
                             rule_overrides=rule_overrides,
                             project_name=meta.get("project_name") or "")
    except Exception as exc:                          # noqa: BLE001
        _log_exc("annotate", exc)
        return _fail("Annotation failed. See the server log for detail.", 500)

    # The per-label outcome was already being computed and discarded. Keep it
    # and hand the user the defect list.
    qa = build_qa(stats, filtered)
    try:
        write_quantities_excel({"units": stats.get("units"), "floor_label": "",
                                "reconciliation": None, "kpis": None,
                                "perimeter": None, "geometry_health": None},
                               paths["qa_xlsx"], qa=qa)
        _write_json(paths["qa_json"], qa)
    except Exception as exc:                          # noqa: BLE001
        _log_exc("annotate/qa", exc)

    # A run that draws nothing is the single most confusing outcome: the
    # DXF comes back looking untouched apart from some empty layers. Say
    # why instead of leaving the user to guess.
    drew = stats.get("drawn_boundaries", 0)
    held_back = stats.get("filtered_out_boundaries", 0)
    hint = None
    if drew == 0 and family_filter:
        hint = ("No '{0}' boundaries were found on the selected floor(s). "
                "{1} boundary(ies) belong to other families - pick "
                "'All boundaries', or change the Family column on the rows "
                "you expected to be {0}.".format(family_filter, held_back))
    elif drew == 0:
        reasons = []
        if stats.get("wall_segs_all", 0) == 0:
            reasons.append("no usable wall segments were found - check that "
                           "walls are LINE or LWPOLYLINE entities and that "
                           "their layer names are not caught by the "
                           "exclusion keywords in dxf_reader")
        if qa["totals"].get("no_walls"):
            reasons.append("{0} label(s) found no enclosing wall within the "
                           "{1:g} m search radius, which usually means the "
                           "drawing units were read wrongly or the rooms are "
                           "larger than max_room_sqm".format(
                               qa["totals"]["no_walls"],
                               float(CONFIG["boundary"]["max_hv_search_mm"]) / 1000.0))
        if not total_kept:
            reasons.append("no texts were selected in step 3")
        hint = ("No boundaries were drawn, so the exported DXF is your "
                "original plus the empty family layers. "
                + ("; ".join(reasons) if reasons else
                   "check the Extraction QA table for the per-label outcome")
                + ".")

    return jsonify({
        "ok": True,
        "stats": stats,
        "kept": total_kept,
        "dropped": total_dropped,
        "boundaries_drawn": drew,
        "filtered_out": held_back,
        "family_filter": family_filter,
        "hint": hint,
        "qa": {"problem_count": qa["problem_count"],
               "boundary_count": qa["boundary_count"],
               "totals": qa["totals"]},
        "download": "/download/dxf/" + token,
        "qa_download": {"xlsx": "/download/qa_xlsx/" + token,
                        "json": "/download/qa_json/" + token},
    })


@app.route("/preview/<token>")
def preview(token):
    """Boundary preview for the annotated DXF, as inline SVG.

    ?base=1 draws the original drawing underneath the boundaries. It is a
    separate request and a separate cache because it costs several times
    as much to build and to render.
    """
    if not _valid_token(token):
        return _fail("Invalid token.")
    paths = _paths(token)
    if not os.path.exists(paths["annotated"]):
        return _fail("Generate boundaries first.", 404)

    with_base = request.args.get("base") in ("1", "true", "yes")

    # Parsing a 30 MB drawing takes a couple of seconds, and the preview is
    # reopened far more often than it is regenerated.
    cached = paths["preview_base"] if with_base else paths["preview"]
    if (os.path.exists(cached)
            and os.path.getmtime(cached) >= os.path.getmtime(paths["annotated"])):
        try:
            return jsonify(dict(_read_json(cached), ok=True))
        except (OSError, ValueError):
            pass

    meta = _read_json(paths["meta"]) if os.path.exists(paths["meta"]) else {}
    try:
        data = build_preview(paths["annotated"],
                             unit_override=meta.get("unit_override"),
                             with_base=with_base)
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:                          # noqa: BLE001
        _log_exc("preview", exc)
        return _fail("Could not render the preview. See the server log for "
                     "detail.", 500)

    try:
        _write_json(cached, data)
    except OSError as exc:
        _log_exc("preview/cache", exc)

    return jsonify(dict(data, ok=True))


def _filter_selections(full_results, selections):
    """Keep only the sr_nos the user ticked, per floor."""
    filtered, kept, dropped = {}, 0, 0
    for floor_name, rows in full_results.items():
        keep = selections.get(floor_name)
        if keep is None:
            filtered[floor_name] = rows
            kept += len(rows)
            continue
        keep_set = set()
        for s in keep:
            try:
                keep_set.add(int(s))
            except (TypeError, ValueError):
                pass
        kept_rows = [r for r in rows if int(r.get("sr_no", -1)) in keep_set]
        filtered[floor_name] = kept_rows
        kept += len(kept_rows)
        dropped += len(rows) - len(kept_rows)
    return filtered, kept, dropped


# ---------------------------------------------------------------- Section 4
@app.route("/areas/upload", methods=["POST"])
def areas_upload():
    if "file" not in request.files:
        return _fail("No file uploaded.")
    f = request.files["file"]
    if not f or f.filename == "":
        return _fail("Empty file.")
    if not _is_allowed(f.filename):
        return _fail("Only .dxf files are supported.")

    unit_override = request.form.get("units") or None
    token = uuid.uuid4().hex
    paths = _paths(token)
    safe_name = secure_filename(f.filename)
    f.save(paths["areas_dxf"])

    parsed = {}
    try:
        layers, units, diag = list_layers(paths["areas_dxf"], unit_override,
                                          loaded_out=parsed)
    except Exception as exc:                          # noqa: BLE001
        _log_exc("areas/upload", exc)
        _quiet_remove(paths["areas_dxf"])
        return _fail("Could not read this DXF.")

    if not diag.get("plausible", True) and units.overridden:
        _quiet_remove(paths["areas_dxf"])
        return _fail("{0} Leave the unit on auto-detect, or pick the one "
                     "the file was actually drawn in.".format(
                         diag.get("message") or ""))

    _write_json(paths["meta"], {"original_name": safe_name, "kind": "areas",
                                 "units": units.to_ui(),
                                 "unit_override": unit_override})

    try:
        cadview.start(paths["areas_dxf"], OUTPUT_DIR, token, "areas",
                      unit_override, boundaries=True,
                      loaded=parsed.get("loaded"))
    except Exception as exc:                          # noqa: BLE001
        _log_exc("areas/upload/cadview", exc)

    return jsonify({"ok": True, "token": token, "original_name": safe_name,
                     "units": units.to_ui(), "diagnostics": diag,
                     "unit_info": diag, "layers": layers})


@app.route("/areas/calculate", methods=["POST"])
def areas_calculate():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    floor_layer = data.get("floor_layer")

    if not _valid_token(token):
        return _fail("Invalid token.")
    if not floor_layer:
        return _fail("A floor outline layer is required.")

    paths = _paths(token)
    if not os.path.exists(paths["areas_dxf"]):
        return _fail("Upload expired or not found. Please upload again.", 404)

    meta = _read_json(paths["meta"]) if os.path.exists(paths["meta"]) else {}
    try:
        bundle = analyse_areas(paths["areas_dxf"], floor_layer,
                               unit_override=meta.get("unit_override"))
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:                          # noqa: BLE001
        _log_exc("areas/calculate", exc)
        return _fail("Area calculation failed. See the server log for detail.",
                     500)

    result = bundle["areas"]
    try:
        write_areas_excel(result, paths["areas_xlsx"])
        _write_json(paths["areas_json"], result)
        write_quantities_excel(bundle["quantities"], paths["quantities_xlsx"])
        _write_json(paths["quantities_json"],
                    {"quantities": bundle["quantities"]})
    except Exception as exc:                          # noqa: BLE001
        _log_exc("areas/export", exc)
        return _fail("Could not write the export files.", 500)
    # The Area Statement is an extra; if it cannot be written the rest of
    # Step 4 still stands, and the button says so rather than failing.
    statement_ok = True
    try:
        write_area_statement(result, paths["area_statement_xlsx"],
                             drawing_name=meta.get("original_name") or "")
    except Exception as exc:                          # noqa: BLE001
        _log_exc("areas/area_statement", exc)
        statement_ok = False

    return jsonify({
        "ok": True,
        "result": result,
        "units": bundle["units"],
        "unit_info": bundle.get("unit_info"),
        "quantities": bundle["quantities"],
        "download": {
            "xlsx": "/download/areas_xlsx/" + token,
            "json": "/download/areas_json/" + token,
            "quantities_xlsx": "/download/quantities_xlsx/" + token,
            "quantities_json": "/download/quantities_json/" + token,
            "area_statement_xlsx": ("/areas/statement/" + token
                                    if statement_ok else None),
        },
    })


@app.route("/areas/statement/<token>")
def areas_statement(token):
    """The Area Statement, written when asked for so it carries the project
    name typed beside the button (?project=...). Built from the result the
    last Calculate saved, so it needs no second pass over the drawing."""
    if not _valid_token(token):
        abort(404)
    paths = _paths(token)
    if not os.path.exists(paths["areas_json"]):
        abort(404)
    # The name typed in Step 1 of this session; failing that, the one the
    # annotated DXF carries in its header from the run that made it.
    result = _read_json(paths["areas_json"])
    project = ((request.args.get("project") or "").strip()
               or (result.get("project_name") or "").strip())[:120]
    meta = _read_json(paths["meta"]) if os.path.exists(paths["meta"]) else {}
    drawing = meta.get("original_name") or ""
    try:
        write_area_statement(result,
                             paths["area_statement_xlsx"],
                             drawing_name=drawing, project_name=project)
    except Exception as exc:                          # noqa: BLE001
        _log_exc("areas/statement", exc)
        abort(500)
    base = os.path.splitext(secure_filename(drawing or "drawing"))[0] or "drawing"
    if project:
        base = (secure_filename(project) or base) + "_" + base
    return send_file(paths["area_statement_xlsx"], as_attachment=True,
                     download_name=base + "_area_statement.xlsx",
                     mimetype=_MIME["sheet"])


# ------------------------------------------------------------- CAD viewer
_CAD_SOURCES = {"source": "dxf", "areas": "areas_dxf"}


def _cad_source(token, which):
    if not _valid_token(token) or which not in _CAD_SOURCES:
        abort(404)
    paths = _paths(token)
    meta = _read_json(paths["meta"]) if os.path.exists(paths["meta"]) else {}
    return paths[_CAD_SOURCES[which]], meta.get("unit_override")


@app.route("/cadview/<token>/<which>/scene")
def cadview_scene(token, which):
    """The drawing for the viewer: extent, colours and texts, once the
    background read has finished ({"status": "building"} until then)."""
    dxf_path, unit_override = _cad_source(token, which)
    try:
        state, scene = cadview.status(dxf_path, OUTPUT_DIR, token, which,
                                      unit_override,
                                      boundaries=(which == "areas"))
    except Exception as exc:                          # noqa: BLE001
        _log_exc("cadview/scene", exc)
        return _fail("Could not read this drawing for the viewer.", 500)
    if state == "error":
        return _fail(scene, 404)
    return jsonify({"ok": True, "status": state, "scene": scene})


@app.route("/cadview/<token>/plates")
def cadview_plates(token):
    """Floor plate outlines for the viewer: every Step 2 candidate, the
    floors picked for Step 3, and each family's colour."""
    if not _valid_token(token):
        abort(404)
    paths = _paths(token)
    if not os.path.exists(paths["meta"]):
        return _fail("Upload expired or not found. Please upload again.", 404)
    meta = _read_json(paths["meta"])

    def ints(poly):
        return [[int(round(x)), int(round(y))] for x, y in poly or []]

    from ezdxf.colors import aci2rgb
    families = []
    for fam in list_families():
        r, g, b = aci2rgb(max(1, min(255, int(fam.get("color") or 7))))
        families.append({"name": fam["name"],
                         "rgb": "#{0:02x}{1:02x}{2:02x}".format(r, g, b)})
    return jsonify({
        "ok": True,
        "candidates": [{"index": c["index"], "name": c.get("name") or "",
                        "area_sqm": c.get("area_sqm"), "layer": c.get("layer"),
                        "title": c.get("title") or "",
                        "polygon": ints(c.get("polygon"))}
                       for c in meta.get("candidates") or []],
        "selected": [{"name": f["name"], "index": f.get("index"),
                      "polygon": ints(f.get("polygon"))}
                     for f in meta.get("selected") or []],
        "families": families,
    })


@app.route("/cadview/<token>/<which>/lines")
def cadview_lines(token, which):
    """Linework as binary, clipped to ?bbox=x1,y1,x2,y2 (mm) and thinned to
    ?px= (mm per screen pixel)."""
    _cad_source(token, which)
    bbox = None
    raw = request.args.get("bbox")
    if raw:
        try:
            bbox = tuple(float(v) for v in raw.split(","))
            if len(bbox) != 4:
                raise ValueError
        except ValueError:
            return _fail("Bad bbox.")
    try:
        px = float(request.args.get("px") or 0) or None
    except ValueError:
        px = None
    npz_path, _ = cadview.cache_paths(OUTPUT_DIR, token, which)
    if not os.path.exists(npz_path):
        abort(404)
    try:
        body = cadview.lines_binary(OUTPUT_DIR, token, which, bbox, px)
    except Exception as exc:                          # noqa: BLE001
        _log_exc("cadview/lines", exc)
        abort(500)
    return Response(body, mimetype="application/octet-stream",
                    headers={"Cache-Control": "no-store"})


# ------------------------------------------------------- Revision comparison
@app.route("/compare", methods=["POST"])
def compare():
    """Quantity delta between two revisions.

    Each side may be the areas JSON, the areas Excel workbook, or a
    corrected DXF - whichever form the revision happens to exist in.
    """
    prev = request.files.get("previous")
    curr = request.files.get("current")
    if not prev or not curr:
        return _fail("Upload a previous and a current file. Each can be an "
                     "areas JSON, an areas Excel workbook, or a corrected "
                     "DXF.")

    units_prev = request.form.get("previous_units") or None
    units_curr = request.form.get("current_units") or None
    layer_prev = request.form.get("previous_layer") or None
    layer_curr = request.form.get("current_layer") or None

    token = uuid.uuid4().hex
    paths = _paths(token)
    staged = []
    try:
        try:
            old_data, old_info = _stage_revision(prev, token, "prev", staged,
                                                  layer_prev, units_prev)
            new_data, new_info = _stage_revision(curr, token, "curr", staged,
                                                  layer_curr, units_curr)
        except ValueError as exc:
            return _fail(str(exc))
        except Exception as exc:                      # noqa: BLE001
            _log_exc("compare/load", exc)
            return _fail("Could not read one of those files. Supported: "
                         "areas JSON, areas Excel workbook, corrected DXF.")

        cmp_data = compare_revisions(old_data, new_data)
        cmp_data["sources"] = {"previous": old_info, "current": new_info}

        try:
            write_comparison_excel(cmp_data, paths["compare_xlsx"])
            _write_json(paths["compare_json"], cmp_data)
            _write_json(paths["meta"], {"original_name": "revision_comparison",
                                         "kind": "compare"})
        except Exception as exc:                      # noqa: BLE001
            _log_exc("compare/export", exc)
            return _fail("Could not write the comparison file.", 500)

        return jsonify({"ok": True, "token": token, "comparison": cmp_data,
                         "sources": cmp_data["sources"],
                         "download": {"xlsx": "/download/compare_xlsx/" + token,
                                      "json": "/download/compare_json/" + token}})
    finally:
        for p in staged:
            _quiet_remove(p)


def _stage_revision(storage, token, side, staged, floor_layer, unit_override):
    """Save an uploaded revision to disk and parse it into an areas dict."""
    name = secure_filename(storage.filename or "")
    ext = os.path.splitext(name)[1].lower()
    if ext not in REVISION_EXT:
        raise ValueError(
            "'{0}' is not a supported revision file. Upload the areas JSON, "
            "the areas Excel workbook, or a corrected DXF.".format(
                name or "file"))
    tmp = os.path.join(UPLOAD_DIR, "{0}_{1}{2}".format(token, side, ext))
    storage.save(tmp)
    staged.append(tmp)
    return load_revision(tmp, original_name=name, floor_layer=floor_layer,
                         unit_override=unit_override)


# ------------------------------------------------------------------ download
_DOWNLOAD_KINDS = {
    "xlsx":            ("_text_extraction.xlsx", "sheet"),
    "json":            ("_text_extraction.json", "json"),
    "dxf":             ("_annotated.dxf", "dxf"),
    "areas_xlsx":      ("_areas.xlsx", "sheet"),
    "area_statement_xlsx": ("_area_statement.xlsx", "sheet"),
    "areas_json":      ("_areas.json", "json"),
    "quantities_xlsx": ("_quantities.xlsx", "sheet"),
    "quantities_json": ("_quantities.json", "json"),
    "rules_dxf":       ("_by_my_rules.dxf", "dxf"),
    "layers_xlsx":     ("_drawing_layers.xlsx", "sheet"),
    "layers_json":     ("_drawing_layers.json", "json"),
    "qa_xlsx":         ("_extraction_qa.xlsx", "sheet"),
    "qa_json":         ("_extraction_qa.json", "json"),
    "compare_xlsx":    ("_revision_comparison.xlsx", "sheet"),
    "compare_json":    ("_revision_comparison.json", "json"),
}
_MIME = {
    "sheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "json": "application/json",
    "dxf": "application/dxf",
}


@app.route("/download/<kind>/<token>")
def download(kind, token):
    if kind not in _DOWNLOAD_KINDS or not _valid_token(token):
        abort(404)

    paths = _paths(token)
    path = paths["annotated"] if kind == "dxf" else paths[kind]
    if kind == "rules_dxf":
        path = paths["rules_dxf"]
    if not os.path.exists(path):
        abort(404)

    base = "drawing_analysis"
    if os.path.exists(paths["meta"]):
        meta = _read_json(paths["meta"])
        base = os.path.splitext(secure_filename(
            meta.get("original_name", base)))[0] or base

    suffix, mime_key = _DOWNLOAD_KINDS[kind]
    return send_file(path, as_attachment=True, download_name=base + suffix,
                     mimetype=_MIME[mime_key])


# ------------------------------------------------------------------- helpers
def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def _quiet_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
