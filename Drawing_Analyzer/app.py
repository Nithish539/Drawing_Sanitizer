# -*- coding: utf-8 -*-
"""Drawing_Analyzer - Flask routes.

Run:
    pip install -r requirements.txt
    python runserver.py

Then open http://127.0.0.1:5555
"""

import functools
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
                         list_floor_usages, usage_name, family_precedence,
                         save_family_rules, write_areas_excel, write_excel,
                         write_json)
from .area_statement import write_area_statement
from .geomextract import describe, extract_geometry, merge_polygons, trace_at
from . import cadview
from . import geoengine, geocompare
from . import jobs
from .pipeline import analyse_areas, build_qa, list_layers
from .readback import read_drawn_boundaries
from .preview import build_preview, build_preview_from_shapes
from .quantities import compare_revisions
from .revisions import load_revision
from . import rules as rule_book
from . import userrules
from .reports import (add_size_check_sheet, add_window_checks_sheet, add_wall_rules_sheet, add_walls_sheet, walls_summary, hanging_walls_summary, wall_trace_summary, add_validation_sheet, write_comparison_excel,
                      write_layers_excel, write_quantities_excel)

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
# Each floor's boundary pass is cached by its inputs (dxf_reader), so a
# stopped Generate restarts where it stopped and a re-run after a change
# on one floor only works that floor out again.
from .dxf_reader import set_pass_cache_dir as _set_pass_cache_dir
_PASS_CACHE = os.path.join(OUTPUT_DIR, "_pass_cache")


def _prune_pass_cache(keep_days=7, keep_max=3000):
    try:
        names = [os.path.join(_PASS_CACHE, n) for n in os.listdir(_PASS_CACHE)]
    except OSError:
        return
    import time as _t
    names.sort(key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0, reverse=True)
    for i, f in enumerate(names):
        try:
            if i >= keep_max or _t.time() - os.path.getmtime(f) > keep_days * 86400:
                os.remove(f)
        except OSError:
            pass


_prune_pass_cache()
_set_pass_cache_dir(_PASS_CACHE)

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
        # the element identification engine's list (elementid): every
        # opening and core space, classified with a confidence score
        "elements_xlsx":   os.path.join(OUTPUT_DIR, token + "_elements.xlsx"),
        "elements_json":   os.path.join(OUTPUT_DIR, token + "_elements.json"),
        "preview":         os.path.join(OUTPUT_DIR, token + "_preview.json"),
        "preview_base":    os.path.join(OUTPUT_DIR, token + "_preview_base.json"),
        "rules_dxf":       os.path.join(OUTPUT_DIR, token + "_by_rules.dxf"),
        "layers_xlsx":     os.path.join(OUTPUT_DIR, token + "_layers.xlsx"),
        "layers_json":     os.path.join(OUTPUT_DIR, token + "_layers.json"),
        "compare_xlsx":    os.path.join(OUTPUT_DIR, token + "_comparison.xlsx"),
        "compare_json":    os.path.join(OUTPUT_DIR, token + "_comparison.json"),
        "geometry":        os.path.join(OUTPUT_DIR, token + "_geometry.json"),
        # The geometry engine (geoengine.py) and its comparison with the
        # text engine - separate files, the text outputs are not touched.
        "geo_json":        os.path.join(OUTPUT_DIR, token + "_geoengine.json"),
        "geo_dxf":         os.path.join(OUTPUT_DIR, token + "_geometry_annotated.dxf"),
        "geo_cmp_json":    os.path.join(OUTPUT_DIR, token + "_text_vs_geometry.json"),
        "geo_cmp_xlsx":    os.path.join(OUTPUT_DIR, token + "_text_vs_geometry.xlsx"),
        "geo_cmp_dxf":     os.path.join(OUTPUT_DIR, token + "_text_vs_geometry.dxf"),
        # the text engine alone, for the comparison, when Step 3's Generate
        # was not a plain text run (it used the Geometry tab or one family)
        "geo_text_dxf":    os.path.join(OUTPUT_DIR, token + "_text_engine_only.dxf"),
        # categories and names the user gave geometry findings in Step 7
        "geo_review":      os.path.join(OUTPUT_DIR, token + "_geo_review.json"),
        # the geometry output the user settled on in Step 5 (Generate there)
        "geo_final":       os.path.join(OUTPUT_DIR, token + "_geo_final.json"),
        # Step 5: the one drawing the user settled on from both outputs
        "final_dxf":       os.path.join(OUTPUT_DIR, token + "_final.dxf"),
        "final_json":      os.path.join(OUTPUT_DIR, token + "_final.json"),
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


from . import layernames as _LN  # noqa: E402


def _unit_of(meta):
    """The units every step after upload reads this drawing - and the files
    made from it - in: the user's choice, else what the upload settled on
    (meta["units"]). Never detected again: a generated DXF can read
    differently (the BHUB comparison picked Inches over the upload's
    Millimetres and scaled every text boundary 25.4 times)."""
    meta = meta or {}
    if meta.get("unit_override"):
        return meta["unit_override"]
    code = (meta.get("units") or {}).get("insunits_code")
    return int(code) if code else None


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
                             unit_override=_unit_of(meta),
                             rule_overrides=built_in,
                             user_rules=rules)
    except Exception as exc:                          # noqa: BLE001
        _log_exc("my_rules/generate", exc)
        return _fail("Drawing with these rules failed. See the server log.",
                     500)
    try:
        picture = build_preview(paths["rules_dxf"],
                                _unit_of(meta))
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
            return _LN.family_layer(fam.get("layer") or "UNKNOWN")
    return item.get("layer") or _LN.family_layer("UNKNOWN")


@app.route("/boundary_rules", methods=["GET"])
def boundary_rules():
    """The rules the drawing pass applies, for the Step 3 checklist."""
    return jsonify({"ok": True,
                    "groups": rule_book.catalogue(),
                    "defaults": rule_book.defaults()})


@app.route("/family_rules", methods=["GET"])
def family_rules_get():
    usages, default = list_floor_usages()
    return jsonify({"ok": True, "families": list_family_rules(),
                    "usages": usages, "default_usage": default})


@app.route("/family_rules", methods=["POST"])
def family_rules_post():
    """Save an edited ruleset and apply it without a restart."""
    data = request.get_json(silent=True) or {}
    try:
        saved = save_family_rules(data.get("families"),
                                  usages=data.get("usages"),
                                  default_usage=data.get("default_usage"))
    except ValueError as exc:
        return _fail(str(exc))
    except OSError as exc:
        _log_exc("family_rules/save", exc)
        return _fail("Could not write family_rules.json. Check that the file "
                     "is not open in another program and that the folder is "
                     "writable.", 500)
    usages, default = list_floor_usages()
    return jsonify({"ok": True,
                    "families": list_family_rules(saved),
                    "usages": usages, "default_usage": default,
                    "family_list": list_family_names(),
                    "family_options": list_families()})


@app.route("/family_rules/precedence", methods=["POST"])
def family_rules_precedence():
    """Step 5: the user settled a conflict and asked for that choice to be
    the rule for the family from now on - "geometry" or "text" - written
    to family_rules.json."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("family") or "").strip()
    prec = str(data.get("precedence") or "").strip().lower()
    if prec not in ("geometry", "geometry_always", "text", ""):
        return _fail("Precedence must be geometry, geometry_always or text.")
    fams = list_family_rules()
    hit = [f for f in fams if f["name"] == name]
    if not hit:
        return _fail("No family called '{0}' in the family rules.".format(name))
    hit[0]["precedence"] = prec
    try:
        save_family_rules(fams)
    except ValueError as exc:
        return _fail(str(exc))
    except OSError as exc:
        _log_exc("family_rules/precedence", exc)
        return _fail("Could not write family_rules.json.", 500)
    return jsonify({"ok": True, "family": name, "precedence": prec,
                    "families": list_families()})


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
            "No floor plate falls in the {0:g} - {1:g} sq.m range entered - neither a "
            "closed polyline nor an outline joined from open lines (gaps up to 2 m). The drawing was read as {2} (typical room about "
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
        "usages": [u["name"] for u in list_floor_usages()[0]],
        "default_usage": list_floor_usages()[1],
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
def _picked_levels(raw):
    """The floors ticked for one plate in Step 2: [{"key", "label",
    "refuge"}]. A floor struck out there (taken from another plate) is not
    in the list, so the area statement does not count it. [] when the page
    sent none - the plate title decides, as before."""
    out = []
    for lv in raw if isinstance(raw, list) else []:
        if not isinstance(lv, dict):
            continue
        try:
            key = float(lv.get("key"))
        except (TypeError, ValueError):
            continue
        if key != key or abs(key) > 10000:             # NaN, nonsense
            continue
        out.append({"key": int(key) if key == int(key) else key,
                    "label": str(lv.get("label") or "")[:80] or "Floor",
                    "refuge": bool(lv.get("refuge"))})
    return out[:300]


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
                         "polygon": cand_by_idx[idx]["polygon"],
                         "layer": cand_by_idx[idx].get("layer") or "",
                         # The floor's usage decides which families apply.
                         "usage": (usage_name(p.get("usage"))
                                   or usage_name(cand_by_idx[idx].get("usage"))
                                   or list_floor_usages()[1]),
                         "levels": _picked_levels(p.get("levels"))})

    unit_override = _unit_of(meta)
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
    # The geometry side starts now, in a process of its own, on the same
    # floors: it reads no texts, so it need not wait for them, and it is
    # ready (or nearly) by the time Step 4 is opened.
    for stale in ("geo_json", "geo_dxf", "geo_final", "geo_cmp_json",
                  "geo_cmp_xlsx", "geo_cmp_dxf"):
        _quiet_remove(paths[stale])
    _geo_bg_start(token, paths, [{"name": f["name"], "polygon": f["polygon"]}
                                 for f in selected], unit_override)

    return jsonify({
        "ok": True,
        "summary": [{"floor": name, "count": len(rows)}
                    for name, rows in results.items()],
        "results": results,
        "family_list": list_family_names(),
        "families": list_families(),
        "floor_usage": {f["name"]: f.get("usage") for f in selected},
        "units": meta.get("units"),
        "download": {"xlsx": "/download/xlsx/" + token,
                     "json": "/download/json/" + token},
    })


# ------------------------------------------------------- Geometry extract
@app.route("/geometry", methods=["POST"])
def geometry():
    """The spaces the drawing's geometry defines on the extracted floors:
    X regions, staircases, toilets and closed outlines, each with its
    layers, the texts in it, and a suggested family and label."""
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    if not _valid_token(token):
        return _fail("Invalid token.")
    paths = _paths(token)
    if not os.path.exists(paths["dxf"]) or not os.path.exists(paths["results"]):
        return _fail("Extract the texts first (Step 2).", 404)
    cache = _read_json(paths["results"])
    meta = _read_json(paths["meta"]) if os.path.exists(paths["meta"]) else {}
    ctx = {}
    try:
        geo = extract_geometry(paths["dxf"], cache.get("selected", []),
                               cache.get("results", {}),
                               unit_override=_unit_of(meta),
                               keep_context=ctx)
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geometry", exc)
        return _fail("Could not read the geometry. See the server log.", 500)
    _write_json(paths["geometry"], geo)
    _GEO_CTX[token] = ctx
    while len(_GEO_CTX) > 3:                          # the last few uploads
        _GEO_CTX.pop(next(iter(_GEO_CTX)))
    return jsonify(dict(geo, ok=True))


# Walls and texts of the floors last read by /geometry, per upload, so a
# click in the drawing is traced without reading the file again.
_GEO_CTX = {}


def _geo_context(token, floor):
    """Read the walls again (after a server restart) for Trace."""
    paths = _paths(token)
    if not os.path.exists(paths["results"]):
        return None
    cache = _read_json(paths["results"])
    meta = _read_json(paths["meta"]) if os.path.exists(paths["meta"]) else {}
    ctx = {}
    try:
        extract_geometry(paths["dxf"], cache.get("selected", []), {},
                         unit_override=_unit_of(meta), keep_context=ctx)
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geometry context", exc)
        return None
    _GEO_CTX[token] = ctx
    return ctx.get(floor)


@app.route("/geometry/trace", methods=["POST"])
def geometry_trace():
    """The space around a point the user clicked (mm)."""
    data = request.get_json(silent=True) or {}
    token, floor = data.get("token"), data.get("floor")
    if not _valid_token(token):
        return _fail("Invalid token.")
    ctx = (_GEO_CTX.get(token) or {}).get(floor)
    if ctx is None:
        ctx = _geo_context(token, floor)
    if ctx is None:
        return _fail("Analyse the geometry again - the drawing could not be read.", 409)
    try:
        got = trace_at(ctx, float(data.get("x")), float(data.get("y")))
    except (TypeError, ValueError):
        return _fail("Bad point.")
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geometry/trace", exc)
        return _fail("Could not trace here.", 500)
    if got is None:
        return _fail("No walls close a space around this point - use the "
                     "draw tool to outline it.")
    return jsonify({"ok": True, "item": describe(got[0], ctx["rows"], "traced",
                                                 "traced from a click")})


@app.route("/geometry/describe", methods=["POST"])
def geometry_describe():
    """A shape the user drew: its size, the texts in it, and a suggested
    label and family, in the same form as a detected one."""
    data = request.get_json(silent=True) or {}
    token, floor = data.get("token"), data.get("floor")
    if not _valid_token(token):
        return _fail("Invalid token.")
    try:
        poly = [(float(x), float(y)) for x, y in data.get("poly") or []]
    except (TypeError, ValueError):
        return _fail("Bad outline.")
    if len(poly) < 3:
        return _fail("An outline needs at least three corners.")
    rows = ((_GEO_CTX.get(token) or {}).get(floor) or {}).get("rows")
    if rows is None:
        paths = _paths(token)
        rows = (_read_json(paths["results"]).get("results") or {}).get(floor, [])             if os.path.exists(paths["results"]) else []
    return jsonify({"ok": True, "item": describe(poly, rows, "drawn", "drawn by hand")})


@app.route("/geometry/merge", methods=["POST"])
def geometry_merge():
    """One outline from several neighbouring shapes."""
    data = request.get_json(silent=True) or {}
    token, floor = data.get("token"), data.get("floor")
    polys = data.get("polys") or []
    if not _valid_token(token) or len(polys) < 2:
        return _fail("Select two or more neighbouring shapes to merge.")
    try:
        merged = merge_polygons([[(float(x), float(y)) for x, y in p] for p in polys])
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geometry/merge", exc)
        return _fail("Could not merge these shapes.", 500)
    if merged is None:
        return _fail("These shapes do not touch (or only across a gap wider "
                     "than a wall), so they cannot become one.")
    rows = ((_GEO_CTX.get(token) or {}).get(floor) or {}).get("rows") or []
    return jsonify({"ok": True, "item": describe(merged, rows, "merged",
                                                 "merged from {0} shapes".format(len(polys)))})


def _geometry_decisions(paths, decisions):
    """The Geometry tab's choices joined to the shapes found, per floor.
    None when the tab was not used, so Generate runs as before."""
    if not isinstance(decisions, dict) or not decisions:
        return None
    if not os.path.exists(paths["geometry"]):
        return None
    found = _read_json(paths["geometry"]).get("floors") or {}
    out = {}
    for floor, chosen in decisions.items():
        if not isinstance(chosen, dict):
            continue
        items = {it["id"]: it for it in found.get(floor) or []}
        rows = []
        for iid, d in chosen.items():
            if not isinstance(d, dict):
                continue
            it = items.get(iid)
            poly = d.get("poly") or (it or {}).get("poly")
            if not poly or len(poly) < 3:
                continue
            try:
                poly = [[float(x), float(y)] for x, y in poly]
            except (TypeError, ValueError):
                continue
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            kind = (it or {}).get("kind") or str(d.get("kind") or "drawn")
            rows.append({"id": iid, "kind": kind,
                         "bbox": [min(xs), min(ys), max(xs), max(ys)],
                         "poly": poly, "include": bool(d.get("include")),
                         "family": str(d.get("family") or (it or {}).get("family") or "Unknown")[:60],
                         "label": str(d.get("label") if d.get("label") is not None
                                      else (it or {}).get("label", ""))[:200],
                         "label_sr": d.get("label_sr", (it or {}).get("label_sr")),
                         "deleted": bool(d.get("deleted"))})
        out[floor] = rows
    return out or None


def _text_moves(results, moves):
    """Apply the Geometry tab's text moves to the extracted rows (so the
    room search sees each text where it now stands) and list the original
    entities to move in the output drawing."""
    out = []
    if not isinstance(moves, dict):
        return out
    for floor, per in moves.items():
        rows = {int(r["sr_no"]): r for r in results.get(floor, []) if r.get("sr_no") is not None}
        for sr, d in (per or {}).items():
            try:
                r = rows[int(sr)]
                dx, dy = float(d[0]), float(d[1])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if not dx and not dy:
                continue
            out.append({"handles": r.get("handles") or [], "dx": dx, "dy": dy,
                        "text": r.get("text", ""), "x": r["x"], "y": r["y"]})
            r["x"] = round(float(r["x"]) + dx, 3)
            r["y"] = round(float(r["y"]) + dy, 3)
    return out


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
    # The plate's own layer (runs extracted before it was stored): its
    # outline is not a wall the drawing has, see _floor_pass.
    layer_of = {c.get("index"): c.get("layer") or "" for c in meta.get("candidates") or []}
    for f in selected:
        if not f.get("layer") and f.get("index") in layer_of:
            f["layer"] = layer_of[f["index"]]

    # A family is the user's pick only when it differs from the one the
    # extraction suggested (an older page sends every row's suggestion;
    # a pick the rules then never refile - a shaft's room, a lift on a
    # machine room - must be a real pick).
    auto_family = {fl: {str(r.get("sr_no")): (r.get("family") or "Unknown") for r in rows}
                   for fl, rows in full_results.items()}
    same = 0
    for fl, over in list(family_overrides.items()):
        if not isinstance(over, dict):
            family_overrides[fl] = {}
            continue
        auto_ = auto_family.get(fl) or {}
        kept_ = {}
        for sr_, fam_ in over.items():
            if fam_ and fam_ != "Ignore" and auto_.get(str(sr_)) == fam_:
                same += 1
                continue
            kept_[sr_] = fam_
        family_overrides[fl] = kept_

    filtered, total_kept, total_dropped = _filter_selections(full_results,
                                                              selections)
    # Texts the user set to Ignore get no boundary and are not used anywhere
    # further - as if they had not been extracted.
    ignored = 0
    for fl, rows in list(filtered.items()):
        over = family_overrides.get(fl) or {}
        keep = [r for r in rows
                if (over.get(str(r.get("sr_no"))) or over.get(r.get("sr_no")) or r.get("family")) != "Ignore"]
        ignored += len(rows) - len(keep)
        filtered[fl] = keep
    moves = _text_moves(filtered, data.get("text_moves"))
    # Ticked "Draw only my selection": the Geometry tab's shapes are the
    # whole output; unticked (no geometry sent), the texts drive it all.
    geo = _geometry_decisions(paths, data.get("geometry"))

    try:
        stats = annotate_dxf(paths["dxf"], paths["annotated"], selected,
                             filtered, family_overrides=family_overrides,
                             unit_override=_unit_of(meta),
                             family_filter=family_filter,
                             rule_overrides=rule_overrides,
                             project_name=meta.get("project_name") or "",
                             geometry=geo,
                             text_moves=moves,
                             geometry_only=bool(geo and data.get("geometry_only")))
    except Exception as exc:                          # noqa: BLE001
        _log_exc("annotate", exc)
        return _fail("Annotation failed. See the server log for detail.", 500)

    # How these boundaries were made, for the text-vs-geometry comparison:
    # it compares against this DXF and must say when it was not the text
    # engine alone that drew it.
    if meta:
        meta["last_generate"] = {"geometry_tab": bool(geo),
                                 "geometry_only": bool(geo and data.get("geometry_only")),
                                 "family_filter": family_filter or ""}
        try:
            _write_json(paths["meta"], meta)
        except OSError:
            pass

    # The per-label outcome was already being computed and discarded. Keep it
    # and hand the user the defect list.
    qa = build_qa(stats, filtered)
    qa["walls"] = walls_summary(stats)
    qa["hanging_walls"] = hanging_walls_summary(stats)
    qa["wall_trace"] = wall_trace_summary(stats)
    qa["confidence"] = {"High": int(stats.get("confidence_high", 0) or 0),
                        "Medium": int(stats.get("confidence_medium", 0) or 0),
                        "Low": int(stats.get("confidence_low", 0) or 0)}
    qa["wall_exclusions"] = stats.get("wall_exclusions") or []
    qa["wall_veto"] = stats.get("wall_veto") or {}
    qa["stated_grown"] = stats.get("stated_grown") or []
    qa["outlines_cleaned"] = int(stats.get("outlines_cleaned", 0) or 0)
    qa["size_check"] = stats.get("size_check") or []
    qa["window_conflicts"] = stats.get("window_conflicts") or []
    try:
        write_quantities_excel({"units": stats.get("units"), "floor_label": "",
                                "reconciliation": None, "kpis": None,
                                "perimeter": None, "geometry_health": None},
                               paths["qa_xlsx"], qa=qa)
        add_validation_sheet(paths["qa_xlsx"], stats.get("validation"))
        add_walls_sheet(paths["qa_xlsx"], qa["walls"], qa.get("wall_trace"), qa.get("hanging_walls"))
        add_wall_rules_sheet(paths["qa_xlsx"], stats)
        add_size_check_sheet(paths["qa_xlsx"], stats.get("size_check") or [])
        add_window_checks_sheet(paths["qa_xlsx"], stats)
        _write_json(paths["qa_json"], dict(qa, validation=stats.get("validation")))
    except Exception as exc:                          # noqa: BLE001
        _log_exc("annotate/qa", exc)

    # The element list (elementid): its records go to their own files, the
    # page gets the counts.
    elements = stats.pop("elements", None)
    elements_dl = None
    if elements is not None:
        try:
            from . import elementid as _eid
            _write_json(paths["elements_json"], {"summary": stats.get("elements_summary"),
                                                 "elements": elements})
            _eid.write_elements_xlsx(elements, paths["elements_xlsx"],
                                     title=(meta.get("project_name") or meta.get("original_name") or "")
                                     + " - element identification")
            elements_dl = {"xlsx": "/download/elements_xlsx/" + token,
                           "json": "/download/elements_json/" + token}
        except Exception as exc:                      # noqa: BLE001
            _log_exc("annotate/elements", exc)

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
        "kept": total_kept - ignored,
        "dropped": total_dropped,
        "ignored": ignored,
        "boundaries_drawn": drew,
        "filtered_out": held_back,
        "family_filter": family_filter,
        "hint": hint,
        "validation": {k: v for k, v in (stats.get("validation") or {}).items()
                       if k != "issues"},
        "qa": {"problem_count": qa["problem_count"],
               "boundary_count": qa["boundary_count"],
               "totals": qa["totals"],
               "confidence": qa.get("confidence"),
               "wall_trace": {k: v for k, v in (qa.get("wall_trace") or {}).items()
                              if k != "review_items"}},
        "download": "/download/dxf/" + token,
        "qa_download": {"xlsx": "/download/qa_xlsx/" + token,
                        "json": "/download/qa_json/" + token},
        "elements": stats.get("elements_summary"),
        "elements_download": elements_dl,
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
                             unit_override=_unit_of(meta),
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
    return _areas_ready(token, paths, safe_name, unit_override)


# A geometry DXF from before the AI_ names (AI_GEO_<CLASS>) -> the layers
# the area step reads (layernames.ai_layer: AI_GEO_STAIRCASE -> AI_STAIR,
# AI_GEO_STAIR_WELL -> AI_SHAFT ...). An unnamed shape is not a room and is
# left out; columns and windows keep their own layers.
_GEO_TO_BOUNDARY = {"AI_GEO_UNKNOWN": None, _LN.UNKNOWN_SHAPE: None}


def _drop_boundaries(msp):
    """Remove what this app drew in a drawing before (AI_* layers, or the
    older *_BOUNDARY / ROOM_VOID / AI_GEO_*), so a drawing generated here
    carries only its own."""
    for e in list(msp):
        if _LN.is_app_layer(e.dxf.get("layer") or ""):
            msp.delete_entity(e)


def _geo_layers_as_boundaries(path, unit_override):
    """The geometry DXF for the area step: it is drawn on the text engine's
    layers already (AI_SHAFT, AI_STAIR ...); an older one's AI_GEO_<X>
    layers are renamed to them. Unknown shapes are not rooms and are left
    out."""
    from .dxfio import load_document, set_insunits
    doc, msp, _units = load_document(path, unit_override)
    for e in list(msp):                               # old boundaries left in the source drawing
        up = (e.dxf.get("layer") or "").upper()
        if up.endswith(_LN.OLD_SUFFIX) or up == _LN.OLD_VOID:
            msp.delete_entity(e)
    for e in list(msp):
        layer = (e.dxf.get("layer") or "")
        up = layer.upper()
        if not (up.startswith("AI_GEO_") or up in _GEO_TO_BOUNDARY):
            continue
        new = _GEO_TO_BOUNDARY[up] if up in _GEO_TO_BOUNDARY else _LN.ai_layer(up)
        if new is None:
            msp.delete_entity(e)
            continue
        if new not in doc.layers:
            colour = doc.layers.get(layer).color if layer in doc.layers else 7
            doc.layers.add(new, color=colour)
        e.dxf.layer = new
    set_insunits(doc, _units)
    doc.saveas(path)


@app.route("/areas/from_output", methods=["POST"])
def areas_from_output():
    """The area step started from a drawing this app generated: the text
    based output (Step 3) or the geometry output (Step 4)."""
    data = request.get_json(silent=True) or {}
    src_token, source = data.get("token"), data.get("source")
    if not _valid_token(src_token) or source not in ("text", "geometry", "final"):
        return _fail("Invalid request.")
    src = _paths(src_token)
    path = {"text": src["annotated"], "geometry": src["geo_dxf"], "final": src["final_dxf"]}[source]
    if not os.path.exists(path):
        return _fail({"text": "Generate the boundaries first (Step 3, Generate Boundaries).",
                      "geometry": "Generate the geometry boundaries first (Step 4).",
                      "final": "Generate the final drawing first (Step 5, Generate final drawing)."}[source], 404)
    meta = _read_json(src["meta"]) if os.path.exists(src["meta"]) else {}
    token = uuid.uuid4().hex
    paths = _paths(token)
    import shutil
    shutil.copyfile(path, paths["areas_dxf"])
    # a drawing this app generated: the units settled at upload (stored
    # with the area step, so every later read uses them too)
    unit_override = _unit_of(meta)
    try:
        if source == "geometry":
            _geo_layers_as_boundaries(paths["areas_dxf"], unit_override)
    except Exception as exc:                          # noqa: BLE001
        _log_exc("areas/from_output", exc)
        _quiet_remove(paths["areas_dxf"])
        return _fail("Could not prepare the geometry output for the area step.", 500)
    base = os.path.splitext(meta.get("original_name") or "drawing")[0]
    name = base + {"text": "_annotated.dxf", "geometry": "_geometry_annotated.dxf", "final": "_final.dxf"}[source]
    # the floor plates picked in Step 2: no floor layer to choose again
    cache = _read_json(src["results"]) if os.path.exists(src["results"]) else {}
    floors = [{"name": f["name"], "polygon": f["polygon"], "levels": f.get("levels") or []} for f in cache.get("selected") or []]
    if not floors:
        return _fail("Pick the floor plates in Step 2 first.", 404)
    sel = (meta.get("selected") or [{}])[0]
    cand = next((c for c in meta.get("candidates") or [] if c.get("index") == sel.get("index")), {})
    _write_json(paths["meta"], {"original_name": name, "kind": "areas",
                                 "unit_override": unit_override, "source": source})
    try:
        cadview.start(paths["areas_dxf"], OUTPUT_DIR, token, "areas", unit_override, boundaries=True)
    except Exception as exc:                          # noqa: BLE001
        _log_exc("areas/from_output/cadview", exc)
    return _areas_compute(token, cand.get("layer") or "", floors=floors,
                          extra={"token": token, "original_name": name, "source": source,
                                 "floors": [f["name"] for f in floors]})


def _areas_ready(token, paths, safe_name, unit_override, floor_layer=None):
    """List the layers of the area step's drawing and start its viewer."""
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
                     "unit_info": diag, "layers": layers, "floor_layer": floor_layer})


@app.route("/areas/calculate", methods=["POST"])
def areas_calculate():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    floor_layer = data.get("floor_layer")

    if not _valid_token(token):
        return _fail("Invalid token.")
    if not floor_layer:
        return _fail("A floor outline layer is required.")

    return _areas_compute(token, floor_layer)


def _areas_compute(token, floor_layer, floors=None, extra=None):
    """Areas, quantities and the Area Statement for the area step's drawing.
    floors: the Step 2 plates, when the drawing is one this app generated."""
    paths = _paths(token)
    if not os.path.exists(paths["areas_dxf"]):
        return _fail("Upload expired or not found. Please upload again.", 404)

    meta = _read_json(paths["meta"]) if os.path.exists(paths["meta"]) else {}
    try:
        bundle = analyse_areas(paths["areas_dxf"], floor_layer,
                               unit_override=meta.get("unit_override"), floors=floors)
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

    out = {
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
    }
    # the same statement, worked out, to show on the page
    try:
        from .area_statement import statement_view
        out["statement"] = statement_view(result, meta.get("original_name") or "")
    except Exception as exc:                          # noqa: BLE001
        _log_exc("areas/statement view", exc)
    out.update(extra or {})
    return jsonify(out)


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
    # the uploaded drawing: the units settled at upload; the area step's
    # drawing: what that step stored (a generated drawing's resolved units,
    # or nothing - auto - for a corrected DXF the user uploaded)
    return paths[_CAD_SOURCES[which]], (_unit_of(meta) if which == "source" else meta.get("unit_override"))


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
                        "encloses": c.get("encloses") or 0,
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


# ------------------------------------------------------- geometry engine (AI)
# Geometry runs started by Proceed in Step 2: token -> {"pool", "future"}.
_GEO_BG = {}


def _geo_bg_stop(token):
    """End a background geometry run for this upload, if one is going."""
    bg = _GEO_BG.pop(token, None)
    if not bg:
        return
    try:
        bg["pool"].shutdown(wait=False, cancel_futures=True)
    except Exception:                                 # noqa: BLE001
        pass
    for proc in list((getattr(bg["pool"], "_processes", None) or {}).values()):
        try:
            proc.terminate()
        except Exception:                             # noqa: BLE001
            pass


def _geo_bg_start(token, paths, floors, unit_override):
    _geo_bg_stop(token)
    if not floors:
        return
    try:
        from concurrent.futures import ProcessPoolExecutor
        pool = ProcessPoolExecutor(max_workers=1)
        fut = pool.submit(geoengine.run_to_files, paths["dxf"], floors, unit_override,
                          paths["geo_json"], paths["geo_dxf"], list_families())
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geometry in the background", exc)
        return
    _GEO_BG[token] = {"pool": pool, "future": fut}
    fut.add_done_callback(lambda _f: pool.shutdown(wait=False))


def _geo_bg_wait(token):
    """Step 4 opened while Proceed's geometry run is still going: wait for
    it (Stop on Step 4 ends it). True when it finished and wrote its
    findings."""
    bg = _GEO_BG.get(token)
    if not bg:
        return False
    fut = bg["future"]
    import concurrent.futures as cf
    try:
        while not fut.done():
            jobs.check()
            cf.wait([fut], timeout=0.5)
    except jobs.Stopped:
        _geo_bg_stop(token)
        raise
    _GEO_BG.pop(token, None)
    try:
        return bool(fut.result())
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geometry in the background", exc)
        return False


def _geo_floors(paths):
    """The floors picked in Step 2, for the geometry engine."""
    if not os.path.exists(paths["results"]):
        return None, None
    cache = _read_json(paths["results"])
    meta = _read_json(paths["meta"]) if os.path.exists(paths["meta"]) else {}
    floors = [{"name": f["name"], "polygon": f["polygon"]} for f in cache.get("selected") or []]
    return floors, meta


def _geo_reviews(paths):
    return _read_json(paths["geo_review"]) if os.path.exists(paths["geo_review"]) else {}


def _geo_result(paths, raw=None):
    """The engine's findings with the user's reviews (Step 7) on top."""
    raw = raw if raw is not None else _read_json(paths["geo_json"])
    out = geoengine.apply_reviews(raw, _geo_reviews(paths), list_families())
    # (a shaft, lift, window, cut-out, column or void is Step 3's: never a
    # category for a geometry finding)
    out["families_list"] = [{"name": f["name"], "layer": f["layer"]} for f in list_families()
                            if f["name"] not in geoengine.STEP3_FAMILIES]
    return out


def _geo_dxf_current(paths, meta):
    """Rewrite the geometry annotated DXF when reviews are newer than it."""
    if not os.path.exists(paths["geo_review"]):
        return
    if (os.path.exists(paths["geo_dxf"]) and
            os.path.getmtime(paths["geo_dxf"]) >= os.path.getmtime(paths["geo_review"])):
        return
    from .dxfio import load_document, set_insunits
    doc, _msp, units = load_document(paths["dxf"], _unit_of(meta))
    geoengine.write_dxf(doc, units, _geo_result(paths), paths["geo_dxf"])


@app.route("/floors/levels", methods=["POST"])
def floors_levels():
    """The floors each plate name stands for (Step 2, after a rename)."""
    from .dxf_reader import plate_levels
    names = (request.get_json(silent=True) or {}).get("names") or []
    return jsonify({"ok": True, "levels": [plate_levels(str(n)[:200]) for n in names[:500]]})


@app.route("/geoengine/run", methods=["POST"])
def geoengine_run():
    """Run the geometry engine on the Step 2 floors. It reads no texts."""
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    if not _valid_token(token):
        return _fail("Invalid token.")
    paths = _paths(token)
    if not os.path.exists(paths["dxf"]):
        return _fail("Upload expired or not found. Please upload again.", 404)
    floors, meta = _geo_floors(paths)
    if not floors:
        return _fail("Pick the floors in Step 2 and extract the texts first.", 404)
    if data.get("rerun"):
        _geo_bg_stop(token)
    elif _geo_bg_wait(token) and os.path.exists(paths["geo_json"]):
        return jsonify({"ok": True, "cached": True, "background": True,
                        "result": _geo_result(paths)})
    if not data.get("rerun") and os.path.exists(paths["geo_json"]):
        return jsonify({"ok": True, "cached": True, "result": _geo_result(paths)})
    try:
        from .dxfio import load_document, set_insunits
        loaded = load_document(paths["dxf"], _unit_of(meta))
        ctx = {}
        result = geoengine.run(paths["dxf"], floors, _unit_of(meta),
                               loaded=loaded, keep_context=ctx)
        _GEO_CTX[token] = ctx                         # for Trace in Step 5
        while len(_GEO_CTX) > 3:
            _GEO_CTX.pop(next(iter(_GEO_CTX)))
        # the geometry DXF starts as the engine's defaults; Generate in
        # Step 5 rewrites it with the user's choices
        geoengine.write_dxf(loaded[0], loaded[2],
                            geoengine.build_final(result, None, list_families()),
                            paths["geo_dxf"])
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geoengine/run", exc)
        return _fail("The geometry analysis failed. See the server log for detail.", 500)
    _write_json(paths["geo_json"], result)
    # an older output and comparison no longer match these findings
    for k in ("geo_final", "geo_cmp_json", "geo_cmp_xlsx", "geo_cmp_dxf"):
        _quiet_remove(paths[k])
    if os.path.exists(paths["geo_review"]):
        os.utime(paths["geo_review"])         # the DXF was rewritten without them
    return jsonify({"ok": True, "cached": False, "result": _geo_result(paths, result)})


@app.route("/geoengine/generate", methods=["POST"])
def geoengine_generate():
    """Step 5's own Generate: the geometry annotated DXF from the findings
    the user kept, with their families and names, plus the shapes they
    traced or drew, and the texts they moved. Step 3 is not touched."""
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    if not _valid_token(token):
        return _fail("Invalid token.")
    paths = _paths(token)
    if not os.path.exists(paths["geo_json"]):
        return _fail("Run the geometry analysis first.", 404)
    floors, meta = _geo_floors(paths)
    decisions = data.get("decisions") if isinstance(data.get("decisions"), dict) else None
    try:
        result = _read_json(paths["geo_json"])
        final = geoengine.build_final(result, decisions, list_families())
        from .dxfio import load_document, set_insunits
        from .dxf_reader import _move_texts
        doc, msp, units = load_document(paths["dxf"], _unit_of(meta))
        moves = []
        if isinstance(data.get("text_moves"), dict) and os.path.exists(paths["results"]):
            moves = _text_moves(_read_json(paths["results"]).get("results", {}), data["text_moves"])
        moved = _move_texts(doc, msp, units, moves) if moves else {"moved": 0, "not_movable": 0}
        geoengine.write_dxf(doc, units, final, paths["geo_dxf"])
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geoengine/generate", exc)
        return _fail("Could not write the geometry boundaries. See the server log.", 500)
    final["generated"] = True
    _write_json(paths["geo_final"], final)
    n = sum(len(v) for v in final["floors"].values())
    return jsonify({"ok": True, "boundaries": n, "summary": final["summary"],
                    "texts_moved": moved.get("moved", 0),
                    "download": "/download/geo_dxf/" + token})


@app.route("/geoengine/review", methods=["POST"])
def geoengine_review():
    """The user's category and/or name for one geometry finding. Empty
    category and name remove the review. The engine's own answer is kept."""
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    if not _valid_token(token):
        return _fail("Invalid token.")
    paths = _paths(token)
    if not os.path.exists(paths["geo_json"]):
        return _fail("Run the geometry analysis first.", 404)
    ids = data.get("ids") or ([data.get("id")] if data.get("id") else [])
    category = str(data.get("category") or "").strip()
    name = str(data.get("name") or "").strip()[:80]
    if category and category not in list_family_names():
        return _fail("Unknown category '{0}'.".format(category))
    if category in geoengine.STEP3_FAMILIES:
        return _fail("{0} is decided in Step 3 (every X mark by the X sequence) - not in the geometry "
                     "review.".format(category))
    raw = _read_json(paths["geo_json"])
    where = {r["id"]: r["features"]["centroid"] for rows in raw["floors"].values() for r in rows}
    reviews = _geo_reviews(paths)
    for i in ids:
        if i not in where:
            continue
        if not category and not name:
            reviews.pop(i, None)
        else:
            reviews[i] = {"category": category, "name": name, "centroid": where[i]}
    _write_json(paths["geo_review"], reviews)
    return jsonify({"ok": True, "result": _geo_result(paths, raw)})


def _text_family_names():
    """Text engine class (layer prefix) -> family name."""
    out = {"UNKNOWN": "Unknown"}
    for f in list_families():
        prefix = _LN.room_key(f.get("layer") or "") or ""
        if prefix:
            out.setdefault(prefix, f["name"])
    return out


def _row_sig(r):
    """Which object a comparison row is about, stable across re-runs."""
    t, g = r.get("text"), r.get("geo")
    return "{0}@{1}|{2}".format(t["name"] if t else "", t["area_sqm"] if t else "", g["id"] if g else "")


# Where the texts and the geometry name the same family and its precedence
# says geometry, the geometry's outline is used - unless it is this many
# times larger than the texts' and overlaps it less than GEO_OUTLINE_MIN_IOU
# (IoU): then it is not the same room's outline and the texts' stands.
GEO_OUTLINE_OVERSIZE = 1.25
GEO_OUTLINE_MIN_IOU = 0.6


def _final_defaults(cmp):
    """What the final drawing takes from each row until the user changes
    it. Texts first: where both agree, and where they disagree, the texts'
    result (unless the family's precedence says geometry); a boundary the
    texts drew but could not name, the geometry's; an object only one side
    found, that side's - so the geometry fills what the texts missed.

    A shaft, lift, window, cut-out, column or void is Step 3's (2026-10-16,
    final): every X mark decided once by the X sequence (xplace). Such a
    text row is fixed - the texts' boundary, family and name, nothing to
    decide, no precedence and no decision changes it - a geometry finding
    never takes one of those families, and one standing (half or more of
    it) on Step 3's six is left out."""
    from shapely.geometry import Polygon as _FP
    from shapely.ops import unary_union as _fu
    tfam = _text_family_names()
    six = geoengine.STEP3_FAMILIES
    fixed_u = {}
    for r in cmp["rows"]:
        t = r.get("text")
        if t and tfam.get(t["cls"], t["cls"].title()) in six and len(t.get("poly") or []) >= 3:
            try:
                fixed_u.setdefault(r.get("floor"), []).append(_FP(t["poly"]).buffer(0))
            except (ValueError, TypeError):
                pass
    fixed_u = {fl: _fu(v) for fl, v in fixed_u.items() if v}
    for r in cmp["rows"]:
        t, g = r.get("text"), r.get("geo")
        tf = tfam.get(t["cls"], t["cls"].title()) if t else None
        gf = (g.get("category") or geoengine.FAMILY_OF_CLASS.get(g["cls"], "Room")) if g else None
        if gf in six:
            gf = "Unknown"                            # (a result written before 2026-10-16)
        cat = r["category"]
        if tf in six:
            # Step 3's: final
            r["final_default"] = {"use": "text", "family": tf, "name": t["name"] or tf,
                                  "decide": False, "geometry_only": False, "fixed": True,
                                  "step3": True}
            r["sig"] = _row_sig(r)
            continue
        if cat == "STRUCTURE" or (g and not t and g.get("cls") in geoengine.STEP3_CLASSES):
            # a column the geometry found (a result written before
            # 2026-10-16): the columns are Step 3's
            r["final_default"] = {"use": "exclude", "family": "Column", "name": (g or {}).get("label") or "",
                                  "geometry_only": True, "decide": False, "step3_only": True}
            r["sig"] = _row_sig(r)
            continue
        if g and not t and r.get("floor") in fixed_u and len(g.get("poly") or []) >= 3:
            try:
                gp = _FP(g["poly"]).buffer(0)
                on = gp.intersection(fixed_u[r.get("floor")]).area if not gp.is_empty else 0.0
            except (ValueError, TypeError):
                on = 0.0
            if gp.area > 0 and on >= 0.5 * gp.area:
                # standing on Step 3's shaft / lift / void / window / cut-out
                r["final_default"] = {"use": "exclude", "family": gf, "name": g["label"],
                                      "geometry_only": True, "decide": False, "on_step3": True}
                r["sig"] = _row_sig(r)
                continue
        if cat == "TEXT_UNNAMED" and g:
            d = {"use": "geometry", "family": gf, "name": g["label"]}
        elif t:
            d = {"use": "text", "family": tf, "name": t["name"] or tf}
        else:
            d = {"use": "geometry", "family": gf, "name": g["label"]}
        # Texts first: where the texts drew a named boundary it is final -
        # it can still be edited. What the geometry found and the texts did
        # not (or drew without a name) fills the gap, and waits for the user
        # to confirm what it is.
        # A boundary the texts drew without a name (an X marked "Unknown
        # Shaft") that the geometry calls a shaft, a lift, a staircase is
        # simply named by it; only what the texts did not find at all, or
        # what the geometry can only call a room, is put to the user.
        d["geometry_only"] = d["use"] == "geometry"
        d["decide"] = d["use"] == "geometry" and (t is None or gf in ("Room", "Unknown"))
        named = bool(t) and tf not in (None, "Unknown")
        if named and g:
            d["precedence_family"] = tf
        if g and gf not in ("Room", "Unknown") and family_precedence(gf) == "geometry_always":
            # A family the geometry decides (Lift, Lift Lobby): what the
            # geometry found there wins, outline and family, whatever the
            # texts call it. A text naming the same family keeps its name.
            nm = (t["name"] if named and tf == gf and t.get("name") else None) or g["label"] or gf
            d.update({"use": "geometry", "family": gf, "name": nm, "decide": False,
                      "geometry_only": t is None, "by_precedence": gf})
            if named:
                d["precedence_family"] = gf
        elif named and g and gf != tf:
            # The texts name it one thing (LIFT WELL, BALCONY, VOID,
            # TERRACE - any family in family_rules.json) and the geometry
            # another (a shaft, from its X): the texts win, family and
            # boundary. No precedence overrides a name the texts gave - the
            # geometry's family's precedence used to, which is how an AHU
            # room and X-marked lift wells came out as toilets and shafts.
            d.update({"use": "text", "family": tf, "name": t["name"] or tf,
                      "decide": False, "geometry_only": False, "text_named": True})
        elif named and g and tf == "Shaft" and (t.get("cell") or t.get("source") in ("P", "S")):
            # The texts traced this one to its compartment (a shaft from
            # its X, shaft_compartments) or from its label: that outline
            # stands. The geometry's X box only fills in where the texts
            # drew nothing, or drew the X as it was (its fallback).
            d.update({"use": "text", "family": tf, "name": t["name"] or tf, "decide": False,
                      "geometry_only": False, "text_named": True, "text_cell": True})
        elif named and g and family_precedence(tf) in ("geometry", "geometry_always") and \
                (t.get("area_sqm") or 0) > 0 and (g.get("area_sqm") or 0) > GEO_OUTLINE_OVERSIZE * t["area_sqm"] \
                and (r.get("iou") or 0.0) < GEO_OUTLINE_MIN_IOU:
            # The same family, but the geometry's outline runs on well past
            # the one the texts drew (Centaurus: one staircase box across two
            # stairs, 58.3 sq.m over the 27.9 the texts drew): not the same
            # outline of one room - the texts' stands.
            d.update({"use": "text", "family": tf, "name": t["name"] or tf, "decide": False,
                      "geometry_only": False, "text_named": True, "geometry_oversize": True})
        elif named and g and family_precedence(tf) in ("geometry", "geometry_always"):
            # Both say the same family and draw it differently: the family's
            # precedence decides whose outline is used ("Shaft: geometry
            # wins"). The texts' name stays.
            d.update({"use": "geometry", "family": tf, "name": t["name"] or g["label"] or tf,
                      "decide": False, "by_precedence": tf})
        r["final_default"] = d
        r["sig"] = _row_sig(r)
    return cmp


@app.route("/geoengine/final", methods=["POST"])
def geoengine_final():
    """One drawing from both outputs, as the user settled each object in
    Step 5: its boundary from the texts or the geometry (or left out), its
    family and its name. Written like the text-based output - each family
    on its AI_<FAMILY> layer with the name inside - so the area step reads
    it the same way. Columns on AI_COLUMN (a column standing in a room is a
    hole in it, on AI_VOID), windows on AI_WINDOW, and the walls the
    geometry engine found on AI_WALL: none of them rooms or areas.

    Every shaft, lift, window, cut-out, column and void is Step 3's
    (2026-10-16, final - every X mark decided once by the X sequence): its
    row is fixed (_final_defaults), a decision on it is not taken, no other
    row is given one of those families, the columns and the stair wells are
    Step 3's, Step 3's windows and the cut-outs in a wall run are part of
    the walls, and the lifts' entrances are found for Step 3's lifts."""
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    if not _valid_token(token):
        return _fail("Invalid token.")
    paths = _paths(token)
    if not os.path.exists(paths["geo_cmp_json"]):
        return _fail("Open the comparison first.", 404)
    decisions = data.get("decisions") if isinstance(data.get("decisions"), dict) else {}
    cmp = _read_json(paths["geo_cmp_json"])
    if not cmp["rows"] or "final_default" not in cmp["rows"][0]:
        _final_defaults(cmp)
    layer_of = {f["name"]: (f.get("layer") or "ROOM_BOUNDARY") for f in list_families()}
    aci_of = {f["name"]: int(f.get("color") or 7) for f in list_families()}
    # windows in the walls keep their own layer: not a room, not counted
    from .dxf_reader import WINDOW_LAYER, WINDOW_COLOR, CUTOUT_LAYER, CUTOUT_COLOR
    layer_of["Window"], aci_of["Window"] = WINDOW_LAYER, WINDOW_COLOR
    layer_of["Cutout"], aci_of["Cutout"] = CUTOUT_LAYER, CUTOUT_COLOR
    layer_of["Column"], aci_of["Column"] = _LN.COLUMN, 250
    rows, counts = [], {}
    cleaned = 0
    six = geoengine.STEP3_FAMILIES
    not_taken = 0                                     # decisions on Step 3's six, not taken
    for r in cmp["rows"]:
        d = dict(r["final_default"])
        u = {k: v for k, v in (decisions.get(r["sig"]) or {}).items() if k in ("use", "family", "name")}
        if d.get("fixed"):
            not_taken += 1 if u else 0                # Step 3's: final
        else:
            if u.get("family") in six:
                u.pop("family")
                not_taken += 1                        # one of Step 3's six: never from here
            d.update(u)
        if d.get("use") == "exclude" or d.get("family") == "Ignore":
            continue
        side = r.get("text") if d.get("use") == "text" else r.get("geo")
        side = side or r.get("text") or r.get("geo")
        fam = str(d.get("family") or "Room")
        name = str(d.get("name") or "").strip() or fam
        poly = side["poly"]
        if fam not in ("Column", "Window", "Cutout"):
            # slits and spikes cleaned out (dxf_reader.clean_outline)
            from .dxf_reader import clean_outline
            np_, _nh, changed = clean_outline([tuple(p) for p in poly], [])
            if changed:
                poly = [list(p) for p in np_]
                cleaned += 1
        if fam in six and not d.get("fixed"):
            continue                                  # (a geometry boundary is never one of the six)
        rows.append({"floor": r["floor"], "family": fam, "name": name, "poly": poly,
                     "from": d.get("use"), "row": r["id"], "fixed": bool(d.get("fixed")),
                     "geo_id": (r.get("geo") or {}).get("id") if d.get("use") != "text" else None,
                     "user_family": bool(u.get("family")) and not d.get("fixed")})
        counts[fam] = counts.get(fam, 0) + 1
    floors, meta = _geo_floors(paths)
    try:
        from .dxfio import load_document, set_insunits
        doc, msp, units = load_document(paths["dxf"], _unit_of(meta))
        _drop_boundaries(msp)                         # only the settled boundaries count
        f = units.from_mm
        # the text-based drawing the comparison paired with (stored by name)
        text_dxf = cmp.get("text_dxf") or ""
        if text_dxf and not os.path.isabs(text_dxf):
            text_dxf = os.path.join(OUTPUT_DIR, text_dxf)
        conf_index = _text_confidence_index(text_dxf or paths["annotated"], _unit_of(meta))
        # The stair's open well - an X the X sequence made a void, between
        # the flights of its staircase: what Step 3 cut out of the staircase
        # as a hole on AI_VOID is a hole here too, and a geometry boundary
        # over it gives way (P285: the well in ST-01).
        side_ = _text_side_shapes(text_dxf or paths["annotated"], _unit_of(meta))
        wells = side_["wells"]
        well_holes = [[tuple(p) for p in list(w.exterior.coords)[:-1]] for w in wells]
        if wells:
            from shapely.geometry import Polygon as _WP
            kept_rows = []
            for r in rows:
                if r.get("from") == "geometry" and len(r["poly"]) >= 3:
                    pg_ = _WP(r["poly"]).buffer(0)
                    if any(not pg_.is_empty and pg_.intersection(w).area >= 0.7 * max(pg_.area, w.area)
                           for w in wells):
                        counts[r["family"]] = counts.get(r["family"], 1) - 1
                        continue
                kept_rows.append(r)
            rows = kept_rows
        geo = _read_json(paths["geo_json"]) if os.path.exists(paths["geo_json"]) else {}
        qa_ = _read_json(paths["qa_json"]) if os.path.exists(paths.get("qa_json") or "") else {}
        # how each room was made, read before its outline is settled below:
        # the Step 3 tag of a room taken from the texts (an "X" is an X mark
        # the X sequence made a shaft, a lift or a void)
        for r in rows:
            r["tag"] = _final_tag_of(r, conf_index)
            r["x"] = bool(r["tag"] and r["tag"][0] == "X")
        # the columns: Step 3's
        cols_by_floor = _final_columns(rows, geo, side_["columns"], floors)
        # the rooms, X-marked openings and walls settled against each other
        # as Step 3 settles them (dxf_reader.settle_rooms_and_walls): no room
        # over a wall, no wall over an X, the walls joined at their
        # junctions - and the walls drawn once, from what is settled
        walls_by_floor, rows = _settle_final(doc, msp, units, geo, qa_, rows, floors, paths, cols_by_floor)
        # the lifts' entrances: of Step 3's lifts, in the walls settled here
        lift_doors = _final_lift_doors(walls_by_floor, rows, geo)
        # no room reaches into a door opening: each stops at its own face of
        # the wall, the opening is the door's (as in Step 3, 2026-10-17)
        rows = _final_rooms_out_of_doorways(rows, geo, lift_doors)
        # each door's room and its line on that room's inner wall trace -
        # Step 3's where it is Step 3's door - and the rooms squared up to
        # the lines, before they are drawn
        rows, door_plans = _final_door_plans(rows, geo, lift_doors, walls_by_floor, cols_by_floor,
                                             side_.get("doors") or ())
        counts = {}
        for r in rows:
            counts[r["family"]] = counts.get(r["family"], 0) + 1
        for r in rows:
            layer = layer_of.get(r["family"]) or _LN.family_layer(r["family"])
            if layer not in doc.layers:
                doc.layers.add(layer, color=aci_of.get(r["family"], 7))
            if r["family"] == "Column":
                from .dxf_reader import draw_column
                draw_column(doc, msp, units, [tuple(p) for p in r["poly"]])
                continue                              # outline, hatch and "C"
            if r["family"] == "Cutout":
                from .dxf_reader import draw_cutout
                draw_cutout(doc, msp, units, [tuple(p) for p in r["poly"]], r["name"])
                continue                              # outline and X, never a room
            pl = msp.add_lwpolyline([(f(x), f(y)) for x, y in r["poly"]], close=True,
                                    dxfattribs={"layer": layer, "color": 256})
            if r["family"] == "Window":
                continue                              # outline only, no name
            # how far the room can be trusted, carried into this drawing so
            # the area step shows it as for the Step 3 output
            _tag_final_room(doc, pl, r, conf_index)
            xs = [p[0] for p in r["poly"]]
            ys = [p[1] for p in r["poly"]]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            ht = max(100.0, min(400.0, 0.12 * min(w, h)))
            lx, ly = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
            from .dxf_reader import _point_in_polygon, _interior_point
            if not _point_in_polygon(lx, ly, [tuple(p[:2]) for p in r["poly"]]):
                # an L-shaped outline: its name inside it, where the area
                # step reads it (XA CA-ELEC SH, TENENT SH, FHC SH)
                lx, ly = _interior_point([tuple(p[:2]) for p in r["poly"]], r.get("holes"))
            msp.add_mtext(r["name"], dxfattribs={
                "layer": layer, "char_height": f(ht), "attachment_point": 5,
                "insert": (f(lx), f(ly))})
        # a column standing in a room: a hole in it, deducted from its area;
        # and a wall standing in a room (left inside it when it was settled)
        holes = _column_holes(rows) + well_holes + [h for r in rows for h in (r.get("holes") or ())]
        if holes and _LN.VOID not in doc.layers:
            doc.layers.add(_LN.VOID, color=8)
        for h in holes:
            msp.add_lwpolyline([(f(x), f(y)) for x, y in h], close=True,
                               dxfattribs={"layer": _LN.VOID, "color": 256})
        from .dxf_reader import draw_walls, veto_walls, draw_hanging_walls, draw_doors_by_level, DRAW_DOORS, draw_glass, draw_column
        from .geoengine import door_rooms, door_solid
        # the columns on AI_COLUMN (outline, hatch, "C"): Step 3's - never
        # filled as wall
        user_cols = [r for r in rows if r["family"] == "Column"]
        for _fl, cols_ in cols_by_floor.items():
            for c in cols_:
                if any(_same_shape(c, r["poly"]) for r in user_cols if r.get("floor") == _fl):
                    continue                          # drawn above, as a Column row
                draw_column(doc, msp, units, [tuple(p) for p in list(c.exterior.coords)[:-1]])
        # the walls: the geometry engine's, less the strips a room grew over
        # to its stated size in Step 3 (the room-size veto), with the wall
        # left between the rooms, doors and walls (rule wall_gaps_filled),
        # settled with the rooms above - drawn once
        for _fl, by_class in walls_by_floor.items():
            draw_walls(doc, msp, units, by_class)
        # walls drawn above floor level (an object under them): not walls
        draw_hanging_walls(doc, msp, units, geo.get("hanging_walls") or [])
        # the door openings in the walls: primary (entered from the main
        # accessible area) or secondary, by the rooms settled here; each
        # on the inner wall trace of the room it belongs to - where Step 3
        # drew the same door (side_["doors"]), else the same rule here
        # (door_plans, above)
        if DRAW_DOORS:
            plates_ = {f["name"]: f["polygon"] for f in floors or []}
            for _fl in sorted(set(geo.get("doors") or {}) | set(lift_doors)):
                quads = list((geo.get("doors") or {}).get(_fl) or []) + list(lift_doors.get(_fl) or [])
                rows_fl = [r for r in rows if r.get("floor") == _fl]
                by_class, _n = veto_walls((geo.get("walls") or {}).get(_fl) or {}, (qa_.get("wall_veto") or {}).get(_fl))
                draw_doors_by_level(doc, msp, units, quads, door_rooms(rows_fl), plate=plates_.get(_fl),
                                    solid=door_solid(by_class, [r["poly"] for r in rows_fl if r["family"] == "Window"],
                                                     (geo.get("glass") or {}).get(_fl), rows_fl),
                                    step3=side_.get("doors") or (), plan=door_plans.get(_fl))
        # the doubtful door openings (rule doors_need_evidence): on AI_REVIEW
        if DRAW_DOORS:
            from .dxf_reader import draw_door_review
            for _fl, items in (geo.get("door_review") or {}).items():
                draw_door_review(doc, msp, units, items)
        # (the windows are Step 3's: drawn above, as Window rows)
        # glass partitions and glazing - not where a window of Step 3 is
        for _fl, rings in (geo.get("glass") or {}).items():
            draw_glass(doc, msp, units, _glass_less_windows(rings, [r["poly"] for r in rows
                                                                    if r.get("floor") == _fl and r["family"] == "Window"]))
        set_insunits(doc, units)
        doc.saveas(paths["final_dxf"])
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geoengine/final", exc)
        return _fail("Could not write the final drawing. See the server log.", 500)
    _write_json(paths["final_json"], {"rows": rows, "decisions": decisions, "step3_decisions_not_taken": not_taken})
    return jsonify({"ok": True, "boundaries": len(rows), "families": counts,
                    "outlines_cleaned": cleaned, "step3_decisions_not_taken": not_taken,
                    "download": "/download/final_dxf/" + token})


def _text_confidence_index(path, unit_override):
    """The score every room of the text-based drawing carries (XDATA put
    there by Step 3), with where it is: [(centroid x, y mm, area sq.m,
    tag source, label, score, reasons)]."""
    out = []
    if not path or not os.path.exists(path):
        return out
    try:
        from .dxfio import load_document
        from .dxf_reader import room_confidence_tag, room_tag
        from shapely.geometry import Polygon
        doc, msp, units = load_document(path, unit_override, readonly=True)
        for e in msp.query("LWPOLYLINE"):
            score, why = room_confidence_tag(e)
            if score is None:
                continue
            pts = [(units.to_mm(x), units.to_mm(y)) for x, y in e.get_points("xy")]
            if len(pts) < 3:
                continue
            pg = Polygon(pts).buffer(0)
            if pg.is_empty:
                continue
            src, label = room_tag(e)
            c = pg.centroid
            out.append((c.x, c.y, pg.area / 1e6, src, label, score, why))
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geoengine/final confidence index", exc)
    return out


def _final_tag_of(row, index):
    """The tag a final-drawing room taken from the texts carried in Step 3
    (same place, same area within 3 %): (source, label, score, reasons), or
    None."""
    if row.get("from") != "text":
        return None
    from shapely.geometry import Polygon
    try:
        pg = Polygon(row["poly"]).buffer(0)
    except (ValueError, TypeError, IndexError):
        return None
    if pg.is_empty:
        return None
    c, area = pg.centroid, pg.area / 1e6
    best = None
    for cx, cy, a, src, label, score, why in index:
        d = ((cx - c.x) ** 2 + (cy - c.y) ** 2) ** 0.5
        if d <= 300 and a > 0 and abs(a - area) / a <= 0.03 and (best is None or d < best[0]):
            best = (d, src, label, score, why)
    return best[1:] if best else None


def _tag_final_room(doc, entity, row, index):
    """Put a confidence on a final-drawing room: the score and the source
    tag its text-based boundary carried when the room was taken from the
    texts (same place, same area within 3 %; row["tag"] when it was read
    before the outline was settled), else a score worked out from the
    boundary itself - its name's stated size or area, its family - with the
    source tag "X" for an X mark of Step 3 (row["x"]), none otherwise, so the area step decides an opening by name and family
    as it always did for a boundary that does not say."""
    from .dxf_reader import _tag_room
    from shapely.geometry import Polygon
    try:
        pg = Polygon(row["poly"]).buffer(0)
        if pg.is_empty:
            return
        area = pg.area / 1e6
        best = row["tag"] if "tag" in row else _final_tag_of(row, index)
        if best:
            # the text side's own tag too: it tells the area step how the
            # boundary was made - an "X" is a slab opening, a shaft traced
            # from its walls is a room - so the statement reads it as it
            # reads the Step 3 output
            _tag_room(doc, entity, best[0] or "", best[1] or row["name"],
                      (best[2], [w for w in str(best[3]).split("; ") if w]))
            return
        from . import confidence as _cf
        from .labels import declared_size, declared_area
        minx, miny, maxx, maxy = pg.bounds
        name = row.get("name") or ""
        score, why = _cf.score_room(area, maxx - minx, maxy - miny,
                                    want=declared_size(name), want_area=declared_area(name),
                                    traced=True, source="G", family=row.get("family") or "")
        if row.get("from") != "text":
            why = ["- found by the geometry, not by a room label"] + why
            score = max(0.0, score - 0.05)
        # an X mark of Step 3 is a slab opening
        _tag_room(doc, entity, "X" if row.get("x") else "", name, (score, why))
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geoengine/final confidence", exc)


def _text_stair_wells(path, unit_override):
    """The holes on AI_VOID standing in a staircase of the text-based
    drawing (the open well between its flights: an X the X sequence made a
    void),
    as shapely polygons, mm."""
    return _text_side_shapes(path, unit_override)["wells"]


def _text_side_shapes(path, unit_override):
    """From the text-based drawing, in one read: "wells" - the holes on
    AI_VOID standing in a staircase (the stair's open well, a void of the X
    sequence) - "columns" - the columns on AI_COLUMN (Step 3's: its column
    finder's and the X sequence's) - and "doors" - Step 3's doors, each
    (its opening, the middle of its line, the way into the room it belongs
    to): the final drawing draws each where Step 3 did (dxf_reader.
    door_hints). Shapely polygons, mm."""
    out = {"wells": [], "columns": [], "doors": []}
    if not path or not os.path.exists(path):
        return out
    try:
        from .dxfio import load_document
        from .dxf_reader import _polyline_points, _is_closed, family_from_layer, _FAMILY_RULES, door_quad, door_room_dir
        from shapely.geometry import Polygon
        doc, msp, units = load_document(path, unit_override, readonly=True)
        voids, stairs, cols = [], [], []
        door_layers = {_LN.DOOR_PRIMARY, _LN.DOOR_SECONDARY, _LN.OLD_DOOR_PRIMARY, _LN.OLD_DOOR_SECONDARY}   # a Step 3 drawing of before 2026-10-17 too
        for e in msp.query("LWPOLYLINE POLYLINE"):
            layer = (e.dxf.layer or "").upper()
            if layer in door_layers and not _is_closed(e):
                q = door_quad(e, units)
                ln = _polyline_points(e, units)
                if q and len(q) >= 3 and ln:
                    g = Polygon([tuple(p[:2]) for p in q]).buffer(0)
                    if not g.is_empty:
                        out["doors"].append((g, (sum(p[0] for p in ln) / len(ln), sum(p[1] for p in ln) / len(ln)),
                                             door_room_dir(e)))
                continue
            if not _is_closed(e):
                continue
            if layer == _LN.VOID:
                bucket = voids
            elif layer == _LN.COLUMN:
                bucket = cols
            elif _LN.is_room_layer(layer) and family_from_layer(e.dxf.layer, _FAMILY_RULES)["name"] == "Staircase":
                bucket = stairs
            else:
                continue
            pts = _polyline_points(e, units)
            if len(pts) >= 3:
                g = Polygon(pts).buffer(0)
                if not g.is_empty and g.geom_type == "Polygon":
                    bucket.append(g)
        for v in voids:
            if any(s_.intersection(v).area >= 0.9 * v.area for s_ in stairs):
                out["wells"].append(v)
        out["columns"] = cols
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geoengine/final text shapes", exc)
    return out


def _same_shape(pg, pts, share=0.8):
    from shapely.geometry import Polygon
    try:
        q = Polygon(pts).buffer(0)
    except (ValueError, TypeError, IndexError):
        return False
    if q.is_empty or not q.intersects(pg):
        return False
    return q.intersection(pg).area >= share * max(q.area, pg.area)


def _final_columns(rows, geo, text_cols, floors):
    """{floor: [shapely polygon]}: the columns the final drawing stands on -
    Step 3's AI_COLUMN columns (its column finder's, and the crosses the X
    sequence made columns), one of each. A column is Step 3's (2026-10-16):
    the geometry finds none, and no row is made one (rows, geo: not read
    for columns any more; kept for the callers)."""
    from shapely.geometry import Polygon
    plates = []
    for f_ in floors or []:
        try:
            plates.append((f_["name"], Polygon(f_["polygon"]).buffer(0)))
        except (ValueError, TypeError, IndexError, KeyError):
            continue
    out = {}

    def add(fl, g):
        if g is None or g.is_empty or g.geom_type != "Polygon":
            return
        lst = out.setdefault(fl, [])
        if any(c.intersects(g) and c.intersection(g).area >= 0.5 * min(c.area, g.area) for c in lst):
            return
        lst.append(g)
    for c in text_cols or ():
        pt = c.representative_point()
        fl = next((n for n, P in plates if P.contains(pt)), None)
        if fl is not None:
            add(fl, c)
    return out


def _glass_less_windows(rings, wins):
    """Glass rings (point lists) less what stands in a window of Step 3
    (point lists): a window's glazing is the window's."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    ws = []
    for q in wins or ():
        try:
            g = Polygon([tuple(p[:2]) for p in q]).buffer(0)
        except (ValueError, TypeError, IndexError):
            continue
        if not g.is_empty:
            ws.append(g)
    if not ws:
        return rings
    W = unary_union(ws).buffer(20.0, join_style=2)
    out = []
    for ring in rings or ():
        try:
            g = Polygon([tuple(p[:2]) for p in ring]).buffer(0)
        except (ValueError, TypeError, IndexError):
            continue
        if g.is_empty:
            continue
        if not g.intersects(W):
            out.append(ring)
            continue
        rest = g.difference(W)
        for q in getattr(rest, "geoms", [rest]):
            if q.geom_type == "Polygon" and q.area >= 0.01e6:
                out.append([[round(x, 1), round(y, 1)] for x, y in list(q.exterior.coords)[:-1]])
    return out


def _openings_in_walls(wall_items, rows_fl):
    """The final drawing's walls (wall_rings point lists) with Step 3's
    openings in them: its windows and its cut-outs in a wall run
    (elements.cutout_in_wall_run) are part of the wall (OPENINGS_IN_WALLS,
    as in Step 3); a cut-out lining a wall's face is cut out of it. rows_fl:
    the floor's final rows (its Window and Cutout rows are read). Returns
    wall_rings point lists."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    from .dxf_reader import OPENINGS_IN_WALLS, merge_wall_pieces, wall_rings, _wall_union
    from . import elements as _el
    wins, cuts = [], []
    for r in rows_fl or ():
        if r.get("family") not in ("Window", "Cutout") or len(r.get("poly") or []) < 3:
            continue
        try:
            g = Polygon([tuple(p[:2]) for p in r["poly"]]).buffer(0)
        except (ValueError, TypeError, IndexError):
            continue
        if g.is_empty:
            continue
        (wins if r["family"] == "Window" else cuts).append(g)
    if not wins and not cuts:
        return wall_items
    W = _wall_union(wall_items)
    if W is None or W.is_empty:
        return wall_items
    lining = [c for c in cuts if not _el.cutout_in_wall_run(c, W)]
    in_run = [c for c in cuts if not any(c is l_ for l_ in lining)]
    # only an opening touching the walls is a piece of them
    fill = [g for g in (wins + in_run if OPENINGS_IN_WALLS else []) if g.buffer(5.0).intersects(W)]
    cut_ = list(lining) + ([] if OPENINGS_IN_WALLS else wins + in_run)
    if not fill and not cut_:
        return wall_items
    pieces = [g for g in getattr(W, "geoms", [W]) if g.geom_type == "Polygon"] + fill
    merged = merge_wall_pieces(pieces)
    if cut_:
        C = unary_union(cut_)
        out = []
        for g in merged:
            rest = g.difference(C)
            out += [q for q in getattr(rest, "geoms", [rest]) if q.geom_type == "Polygon" and q.area > 1.0]
        merged = out
    return wall_rings(merged)


def _final_lift_doors(walls_by_floor, rows, geo):
    """{floor: [quad point lists]}: the entrances of Step 3's lifts (the
    final Lift rows) - a gap in the lift's own wall, opening onto the floor
    (elements.lift_door_quads) - where no door is already."""
    from shapely.geometry import Polygon
    from . import elements as _el
    from .dxf_reader import _wall_union
    out = {}
    if not _el.LIFT_DOORS:
        return out
    for fl, by_class in (walls_by_floor or {}).items():
        W = _wall_union(list((by_class or {}).get(_LN.WALL) or []))
        if W is None or W.is_empty:
            continue
        lifts, wells = [], []
        for r in rows or ():
            if r.get("floor") != fl or len(r.get("poly") or []) < 3:
                continue
            try:
                g = Polygon([tuple(p[:2]) for p in r["poly"]]).buffer(0)
            except (ValueError, TypeError, IndexError):
                continue
            if g.is_empty:
                continue
            if r["family"] == "Lift":
                lifts.append(g)
                wells.append(g)
            elif r["family"] == "Shaft" or r.get("x"):
                wells.append(g)
        if not lifts:
            continue
        avoid = []
        for q in (geo.get("doors") or {}).get(fl) or []:
            try:
                g = Polygon([tuple(p[:2]) for p in q]).buffer(0)
            except (ValueError, TypeError, IndexError):
                continue
            if not g.is_empty:
                avoid.append(g)
        try:
            out[fl] = [[list(map(float, p)) for p in q] for q, _ev in _el.lift_door_quads(lifts, W, wells, avoid, ())]
        except Exception as exc:                      # noqa: BLE001
            _log_exc("geoengine/final lift doors", exc)
    return out


def _settle_final(doc, msp, units, geo, qa_, rows, floors, paths, cols_by_floor):
    """The final drawing's walls, per floor, and its rooms settled against
    them as Step 3 settles its own (dxf_reader.settle_rooms_and_walls, each
    step a rule in rules.py): the geometry engine's walls less the room-size
    veto, with the wall gaps filled (rule wall_gaps_filled) - the columns
    left out of them - then no room over a wall, no wall over an X-marked
    opening, the walls joined at their junctions, one outline per
    staircase, toilets with their cubicles, small leftover strips joined
    to their room. A family the user set by hand stays; only outlines
    change. Returns ({floor: walls by class}, rows)."""
    from shapely.geometry import Polygon
    from . import rules as _rules
    from .dxf_reader import (wall_gap_pieces, wall_rings, veto_walls, settle_rooms_and_walls,
                             _collect_wall_hatches)
    rule = _rules.resolve(None)
    plates = {f_["name"]: f_["polygon"] for f_ in floors or []}
    walls_by_floor = {}
    if not geo.get("walls"):
        return walls_by_floor, rows
    try:
        islands = _collect_wall_hatches(doc, msp, units, islands=True) if rule["wall_gaps_filled"] else []
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geoengine/final wall hatches", exc)
        islands = []
    cache = _read_json(paths["results"]) if os.path.exists(paths["results"]) else {}
    texts = cache.get("results") or {}

    def poly(q):
        try:
            g = Polygon([tuple(p[:2]) for p in q]).buffer(0)
        except (ValueError, TypeError, IndexError):
            return None
        if g.is_empty:
            return None
        if g.geom_type != "Polygon":
            g = max(getattr(g, "geoms", [g]), key=lambda h: h.area)
        return g
    out_rows = list(rows)
    for fl, by_class in (geo.get("walls") or {}).items():
        by_class, _n = veto_walls(by_class, (qa_.get("wall_veto") or {}).get(fl))
        by_class = dict(by_class or {})
        # one wall layer (older results: AI_WALL_EXTERNAL / _CORE / _INTERNAL)
        wall_items = []
        for k in list(by_class):
            if k == _LN.WALL or k in _LN.OLD_WALLS:
                wall_items += list(by_class.pop(k) or [])
        if fl not in plates:
            by_class[_LN.WALL] = wall_items
            walls_by_floor[fl] = by_class
            continue
        cols = list(cols_by_floor.get(fl) or [])
        cover, drs = [], []
        for r in out_rows:
            if r.get("floor") != fl or len(r.get("poly") or []) < 3:
                continue
            g = poly(r["poly"])
            if g is not None:
                cover.append(g)
        for q in (geo.get("doors") or {}).get(fl) or []:
            g = poly(q)
            if g is not None:
                cover.append(g)
                drs.append(g)
        others = list(drs)
        for q in (geo.get("glass") or {}).get(fl) or []:
            g = poly(q)
            if g is not None:
                cover.append(g)
                others.append(g)
        cover += cols
        # Step 3's windows and the cut-outs in a wall run are part of the
        # walls; a cut-out lining a wall's face is cut out of them
        try:
            wall_items = _openings_in_walls(wall_items, [r for r in out_rows if r.get("floor") == fl])
        except Exception as exc:                      # noqa: BLE001
            _log_exc("geoengine/final openings in walls", exc)
        lbl = [(t.get("x"), t.get("y")) for t in texts.get(fl) or [] if t.get("x") is not None and t.get("y") is not None]
        try:
            if rule["wall_gaps_filled"]:
                gaps = wall_gap_pieces(plates[fl], wall_items, cover, islands, lbl, columns=cols, doors=drs)
                if gaps:
                    wall_items = wall_items + wall_rings(gaps)
        except Exception as exc:                      # noqa: BLE001
            _log_exc("geoengine/final wall gaps", exc)
        if rule["rooms_clear_of_walls"] or rule["x_cell_never_wall"] or rule["wall_junctions_closed"]:
            try:
                items = []
                for r in out_rows:
                    if r.get("floor") != fl or r["family"] in ("Column", "Window", "Cutout"):
                        if r.get("floor") == fl and r["family"] in ("Window", "Cutout"):
                            g = poly(r["poly"])
                            if g is not None:
                                others.append(g)
                        continue
                    g = poly(r["poly"])
                    if g is None:
                        continue
                    items.append({"geom": g, "g0": g, "row": r, "x": bool(r.get("x")), "fam": r["family"],
                                  "lift": not r.get("x") and r["family"] == "Lift",
                                  # a geometry outline a wall runs through (a
                                  # staircase box across two stairs): both pieces
                                  "split": r.get("from") != "text",
                                  "keep": r["family"] == "Balcony / Open", "pt": None})
                settled = settle_rooms_and_walls(items, wall_items, rule, cover=others + cols,
                                                 plate=plates[fl], label_pts=lbl, doors=drs)
                if settled is not None:
                    wall_items = settled
                gone, add = set(), []
                for it in items:
                    r = it["row"]
                    if it.get("twin_of") is not None:
                        if it.get("drop"):
                            continue                  # joined to another piece (the staircase it doubled)
                        g = it["geom"]
                        if g is not None and not g.is_empty:
                            add.append(dict(r, poly=[[round(x, 1), round(y, 1)] for x, y in list(g.exterior.coords)[:-1]],
                                            holes=_hole_pts(g), twin=True, tag=None))
                        continue
                    if it.get("drop"):
                        gone.add(id(r))
                        continue
                    if it["geom"] is not it["g0"]:
                        g = it["geom"]
                        r["poly"] = [[round(x, 1), round(y, 1)] for x, y in list(g.exterior.coords)[:-1]]
                        r["holes"] = _hole_pts(g)
                        r["settled"] = True
                if gone or add:
                    out_rows = [r for r in out_rows if id(r) not in gone] + add
            except Exception as exc:                  # noqa: BLE001
                _log_exc("geoengine/final rooms and walls", exc)
        by_class[_LN.WALL] = wall_items
        walls_by_floor[fl] = by_class
    return walls_by_floor, out_rows


def _hole_pts(g):
    return [[(round(x, 1), round(y, 1)) for x, y in list(i.coords)[:-1]]
            for i in getattr(g, "interiors", ()) if len(i.coords) >= 4]


def _final_door_plans(rows, geo, lift_doors, walls_by_floor, cols_by_floor, step3):
    """Per floor, each door of the final drawing (geometry openings and the
    lifts' entrances, in the order they are drawn): the room it belongs to
    and its line on that room's inner wall trace (dxf_reader.door_plan) -
    Step 3's side and line for a door Step 3 drew (step3), the same rule on
    this drawing's walls, columns, windows, cut-outs and glass for any other
    - and the rooms squared up to those lines (elements.rooms_to_door_lines:
    a room runs to its door's line, no other crosses it; Step 3's six and X
    marks left as they are). Returns (rows, {floor: plan})."""
    from . import elements as _el
    from .dxf_reader import door_plan, _door_room_items
    from .geoengine import door_rooms, door_solid
    from shapely.geometry import Polygon
    plans = {}
    out = list(rows)
    try:
        floors_ = sorted(set((geo.get("doors") or {})) | set(lift_doors or {}), key=str)
        for fl in floors_:
            quads = [q for q in list((geo.get("doors") or {}).get(fl) or []) + list((lift_doors or {}).get(fl) or [])
                     if q is not None and len(q) >= 3]
            if not quads:
                continue
            rows_fl = [r for r in out if r.get("floor") == fl]
            items, idx = [], []
            for i, r in enumerate(rows_fl):
                it = door_rooms([r])
                if not it:
                    continue
                gs_, _s, _m, _l, rk_ = _door_room_items(it)
                if gs_:
                    items.append(it[0])
                    idx.append(i)
            gs, _sh, _mn, _lf, rk = _door_room_items(items)
            walls = []
            w = door_solid((walls_by_floor or {}).get(fl) or {})
            if w is not None and not w.is_empty:
                walls.append(w)
            walls += [c for c in (cols_by_floor or {}).get(fl) or [] if c is not None]
            sets = []
            for r in rows_fl:
                if r["family"] in ("Window", "Cutout") and len(r.get("poly") or ()) >= 3:
                    try:
                        sets.append(Polygon([tuple(p[:2]) for p in r["poly"]]).buffer(0))
                    except (ValueError, TypeError, IndexError):
                        continue
            for ring in (geo.get("glass") or {}).get(fl) or []:
                try:
                    sets.append(Polygon([tuple(p[:2]) for p in ring]).buffer(0))
                except (ValueError, TypeError, IndexError):
                    continue
            solid = _el.trace_solid(walls, sets, quads)
            plan = door_plan(quads, gs, rk, (), step3, solid)
            plans[fl] = plan
            skip = [bool(rows_fl[i].get("fixed") or rows_fl[i].get("x") or
                         rows_fl[i]["family"] in _DOORWAY_KEEP_FAMILIES) for i in idx]
            new, _g, _c = _el.rooms_to_door_lines(gs, quads, [(p[1], p[0], p[2]) if p[2] is not None else None
                                                               for p in plan], skip=skip)
            for i, g0, g1 in zip(idx, gs, new):
                if g1 is not None and g1 is not g0 and g1.geom_type == "Polygon":
                    r = rows_fl[i]
                    r["poly"] = [[round(x, 1), round(y, 1)] for x, y in list(g1.exterior.coords)[:-1]]
                    r["holes"] = _hole_pts(g1)
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geoengine/final door lines", exc)
    return out, plans


# the rows no door opening is cut out of: Step 3's six (final, the X
# sequence's) and what is not a room
_DOORWAY_KEEP_FAMILIES = ("Column", "Cutout", "Window", "Void", "Shaft", "Lift")


def _final_rooms_out_of_doorways(rows, geo, lift_doors):
    """The final drawing's rooms cut back at the door openings they reach
    into (elements.rooms_out_of_doorways, as Step 3 does): each room stops
    at its own face of the wall; one that was only the doorway is dropped.
    Step 3's shafts, lifts, windows, cut-outs, columns and voids (fixed) and
    X marks are left as they are. Returns the rows."""
    from . import elements as _el
    from shapely.geometry import Polygon
    out = list(rows)
    try:
        by_fl = {}
        for r in out:
            by_fl.setdefault(r.get("floor"), []).append(r)
        gone = set()
        for fl, rs in by_fl.items():
            quads = [q for q in list((geo.get("doors") or {}).get(fl) or []) + list((lift_doors or {}).get(fl) or [])
                     if q is not None and len(q) >= 3]
            if not quads:
                continue
            geoms, skip = [], []
            for r in rs:
                try:
                    g = Polygon([tuple(p[:2]) for p in r["poly"]],
                                [h for h in (r.get("holes") or ()) if len(h) >= 3]).buffer(0) \
                        if len(r.get("poly") or ()) >= 3 else None
                except (ValueError, TypeError, IndexError):
                    g = None
                if g is not None and (g.is_empty or g.geom_type != "Polygon"):
                    g = None
                geoms.append(g)
                skip.append(bool(r.get("fixed") or r.get("x") or r["family"] in _DOORWAY_KEEP_FAMILIES))
            new, _sqm = _el.rooms_out_of_doorways(geoms, quads, skip=skip)
            for r, g0, g1 in zip(rs, geoms, new):
                if g0 is None:
                    continue
                if g1 is None:
                    gone.add(id(r))
                elif g1 is not g0:
                    r["poly"] = [[round(x, 1), round(y, 1)] for x, y in list(g1.exterior.coords)[:-1]]
                    r["holes"] = _hole_pts(g1)
        if gone:
            out = [r for r in out if id(r) not in gone]
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geoengine/final rooms out of doorways", exc)
    return out


def _final_wall_gaps(doc, msp, units, geo, qa_, rows, floors, paths, cols_by_floor=None):
    """The final drawing's share of rule wall_gaps_filled: per floor, the
    wall-hatched pieces and thin enclosed strips its rooms, doors, windows,
    glass, columns and walls left on no layer, drawn on AI_WALL.
    geoengine_final now fills them in _settle_final; kept for callers.
    cols_by_floor: the columns (_final_columns) - never filled as wall."""
    from shapely.geometry import Polygon
    from .dxf_reader import (wall_gap_pieces, wall_rings, draw_walls, veto_walls,
                             _collect_wall_hatches)
    plates = {f["name"]: f["polygon"] for f in floors or []}
    if not plates:
        return 0
    islands = _collect_wall_hatches(doc, msp, units, islands=True)
    cache = _read_json(paths["results"]) if os.path.exists(paths["results"]) else {}
    texts = cache.get("results") or {}

    def poly(q):
        try:
            g = Polygon([tuple(p[:2]) for p in q]).buffer(0)
        except (ValueError, TypeError, IndexError):
            return None
        return None if g.is_empty else g
    n = 0
    for fl, by_class in (geo.get("walls") or {}).items():
        if fl not in plates:
            continue
        by_class, _n = veto_walls(by_class, (qa_.get("wall_veto") or {}).get(fl))
        cover, cols, drs = [], [], []
        for r in rows:
            if r.get("floor") != fl or len(r.get("poly") or []) < 3:
                continue
            g = poly(r["poly"])
            if g is not None:
                cover.append(g)
                if r["family"] == "Column":
                    cols.append(g)
        for g in (cols_by_floor or {}).get(fl) or []:
            if not any(c.equals(g) for c in cols):
                cols.append(g)
                cover.append(g)
        for q in (geo.get("doors") or {}).get(fl) or []:
            g = poly(q)
            if g is not None:
                cover.append(g)
                drs.append(g)
        for key in ("window_openings", "glass"):
            for q in (geo.get(key) or {}).get(fl) or []:
                g = poly(q)
                if g is not None:
                    cover.append(g)
        lbl = [(t.get("x"), t.get("y")) for t in texts.get(fl) or [] if t.get("x") is not None and t.get("y") is not None]
        gaps = wall_gap_pieces(plates[fl], (by_class or {}).get(_LN.WALL) or [], cover, islands, lbl,
                               columns=cols, doors=drs)
        if gaps:
            draw_walls(doc, msp, units, {_LN.WALL: wall_rings(gaps)})
            n += len(gaps)
    return n


def _column_holes(rows):
    """The piece of each column (a Column row) standing in a room row:
    [point list] - drawn on AI_VOID so the area step deducts it."""
    from shapely.geometry import Polygon
    cols = []
    for r in rows:
        if r["family"] in ("Column", "Cutout") and len(r["poly"]) >= 3:
            pg = Polygon(r["poly"]).buffer(0)
            if not pg.is_empty:
                cols.append((pg, r["family"] == "Cutout"))
    out = []
    if not cols:
        return out
    for r in rows:
        if r["family"] in ("Column", "Window", "Cutout") or len(r["poly"]) < 3:
            continue
        room = Polygon(r["poly"]).buffer(0)
        for c, cut in cols:
            # a cut-out is a hole only standing (almost) wholly in the room
            inside = (room.intersects(c) and room.intersection(c).area >= 0.9 * c.area) if cut \
                else room.contains(c.representative_point())
            if inside:
                part = room.intersection(c)
                for g in getattr(part, "geoms", [part]):
                    if g.geom_type == "Polygon" and g.area > 1.0:
                        out.append([tuple(p) for p in list(g.exterior.coords)[:-1]])
    return out


@app.route("/geoengine/compare", methods=["POST"])
def geoengine_compare():
    """Text engine (the last Generate Boundaries) against the geometry
    engine. Neither is taken as right; objects are paired by place."""
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    if not _valid_token(token):
        return _fail("Invalid token.")
    paths = _paths(token)
    floors, meta = _geo_floors(paths)
    if not floors:
        return _fail("Pick the floors in Step 2 and extract the texts first.", 404)
    if not os.path.exists(paths["geo_json"]):
        try:
            result = geoengine.run(paths["dxf"], floors, _unit_of(meta))
        except Exception as exc:                      # noqa: BLE001
            _log_exc("geoengine/compare engine run", exc)
            return _fail("The geometry analysis failed. See the server log for detail.", 500)
        _write_json(paths["geo_json"], result)
    last = (meta or {}).get("last_generate") or {}
    # The text side must be the text engine alone. Step 3's own output is
    # that only when it was a plain Generate (no Geometry tab selection, no
    # single-family export); otherwise the text engine is run here, on every
    # extracted text, into a file of its own.
    plain = (os.path.exists(paths["annotated"]) and last is not None and
             not last.get("geometry_tab") and not last.get("family_filter"))
    if plain:
        text_dxf, source = paths["annotated"], "step3"
    else:
        text_dxf, source = paths["geo_text_dxf"], "text_engine_run"
        stale = (not os.path.exists(text_dxf) or
                 os.path.getmtime(text_dxf) < os.path.getmtime(paths["results"]))
        if stale:
            try:
                cache = _read_json(paths["results"])
                annotate_dxf(paths["dxf"], text_dxf, cache.get("selected", []),
                             cache.get("results", {}),
                             unit_override=_unit_of(meta),
                             project_name=meta.get("project_name") or "")
            except Exception as exc:                  # noqa: BLE001
                _log_exc("geoengine/compare text run", exc)
                return _fail("Could not run the text engine for the comparison.", 500)
    review_mtime = max(os.path.getmtime(paths[k]) if os.path.exists(paths[k]) else 0
                       for k in ("geo_final", "geo_json", "geo_review"))
    if not data.get("rerun") and os.path.exists(paths["geo_cmp_json"]):
        cmp = _read_json(paths["geo_cmp_json"])
        if (cmp.get("text_dxf") == os.path.basename(text_dxf) and
                cmp.get("text_mtime") == os.path.getmtime(text_dxf) and
                cmp.get("review_mtime", 0) == review_mtime):
            # Defaults are worked out afresh: a family's precedence may have
            # been set since (Family rules, or Step 5 itself).
            _final_defaults(cmp)
            return jsonify({"ok": True, "cached": True, "comparison": cmp})
    try:
        # the geometry side is the user's Step 4 output (Generate there),
        # else the engine's findings with the corrections made in Step 4 -
        # a category or name changed there counts here without a Generate
        raw = _read_json(paths["geo_json"])
        if os.path.exists(paths["geo_final"]):
            geo = _read_json(paths["geo_final"])
        else:
            geo = geoengine.build_final(_geo_result(paths, raw), None, list_families())
        text = geocompare.read_text_boundaries(text_dxf, floors, _unit_of(meta))
        cmp = geocompare.compare(geo, text)
        cmp["text_mtime"] = os.path.getmtime(text_dxf)
        cmp["text_dxf"] = os.path.basename(text_dxf)
        cmp["review_mtime"] = review_mtime
        cmp["geo_source"] = "step5" if geo.get("generated") else "engine_defaults"
        cmp["text_source"] = dict(last, source=source)
        _final_defaults(cmp)
        geocompare.write_xlsx(cmp, paths["geo_cmp_xlsx"])
        if os.path.exists(paths["geo_dxf"]):
            geocompare.write_dxf(paths["geo_dxf"], cmp, paths["geo_cmp_dxf"], _unit_of(meta))
    except Exception as exc:                          # noqa: BLE001
        _log_exc("geoengine/compare", exc)
        return _fail("The comparison failed. See the server log for detail.", 500)
    _write_json(paths["geo_cmp_json"], cmp)
    return jsonify({"ok": True, "cached": False, "comparison": cmp})


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
    "elements_xlsx":   ("_elements.xlsx", "sheet"),
    "elements_json":   ("_elements.json", "json"),
    "compare_xlsx":    ("_revision_comparison.xlsx", "sheet"),
    "compare_json":    ("_revision_comparison.json", "json"),
    "geo_dxf":         ("_geometry_annotated.dxf", "dxf"),
    "geo_json":        ("_geometry_analysis.json", "json"),
    "geo_cmp_xlsx":    ("_text_vs_geometry.xlsx", "sheet"),
    "geo_cmp_json":    ("_text_vs_geometry.json", "json"),
    "geo_cmp_dxf":     ("_text_vs_geometry.dxf", "dxf"),
    "final_dxf":       ("_final.dxf", "dxf"),
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


# --------------------------------------------------------------- stopping
@app.route("/cancel", methods=["POST"])
def cancel():
    """Stop on the page: the running request with this job id gives up at
    its next check (see jobs.py). Whatever it finished is kept."""
    data = request.get_json(silent=True) or {}
    jobs.cancel(data.get("job"))
    return jsonify({"ok": True})


def _stoppable(view):
    """Run a view as the job named in its X-DA-Job header, and answer a
    Stop with a plain reply rather than an error page."""
    @functools.wraps(view)
    def run(*args, **kwargs):
        tok = jobs.begin(request.headers.get("X-DA-Job"))
        try:
            return view(*args, **kwargs)
        except jobs.Stopped:
            return jsonify({"ok": False, "stopped": True,
                            "error": "Stopped. Press Restart to carry on from "
                                     "where it stopped."})
        finally:
            jobs.end(tok)
    return run


for _ep, _fn in list(app.view_functions.items()):
    if _ep not in ("static", "cancel"):
        app.view_functions[_ep] = _stoppable(_fn)
