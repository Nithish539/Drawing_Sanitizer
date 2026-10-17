"""The column size library: standard RCC, steel and imported column sizes
and each project's own, read from column_library.json beside config.json
(or the file config.json "column_library" names). The column finder
(geoengine._column_shapes) looks a closed shape's size up here - its tier
3, the fallback after the floor's grid and the column layers - and names
the columns it finds by it.

Editing the JSON needs no code change; the app reads it when it starts. A
missing or broken file falls back to the lists built in below (the same
as the shipped file), so the finder never stops for it.

match(w, h, ...) -> {"label", "type", "source", "size"} or None.
"""
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
FILE_NAME = "column_library.json"

# The shipped lists (column_library.json holds the same): what is used when
# the file is missing or unreadable, and what a file leaving a key out gets.
BUILT_IN = {
    "enabled": True,
    "limits": {"min_side_mm": 150.0, "max_side_mm": 1500.0, "max_aspect": 4.0},
    "tolerance": {"project": [15.0, 0.02], "floor_confirmed": [15.0, 0.02], "steel": [3.0, 0.0],
                  "imported": [10.0, 0.0], "rcc": [10.0, 0.02], "round": [10.0, 0.02]},
    "match_order": ["project", "floor_confirmed", "rcc", "steel", "imported"],
    # never a column by the library or the floor's size: a shape drawn on one
    # of these layers (a word anywhere in the name) - or on a wall layer
    "not_column_layer_words": ["CUT", "SHAFT", "DUCT", "VOID", "GLASS", "GLAZ", "WIN", "LOUV", "FRAM", "DOOR",
                               "ELEV", "SECT", "SCHED", "PLUMB", "SANIT", "FITMENT", "TOILET", "LIFT", "STAIR",
                               "REV", "CLOUD", "HIDD", "HIDN", "THIN", "CEN", "BLOCK", "HVAC", "MECH", "ELEC",
                               "LIGHT", "LTG", "FIRE", "SPRINK", "DRAIN", "PIPE", "EQUIP", "EQPM", "FURN", "TEXT",
                               "ANNO", "DIM", "TAG", "GRID", "SYMB"],
    "projects": {},
    "rcc_rect": [[200, 200], [230, 230], [230, 300], [230, 380], [230, 400], [230, 450], [230, 600],
                 [250, 250], [300, 300], [300, 450], [300, 600], [350, 350], [400, 400], [450, 450],
                 [450, 600], [500, 500], [600, 600], [600, 900], [750, 750], [900, 900],
                 [1000, 1000], [1200, 1200]],
    "rcc_round": [300, 350, 400, 450, 500, 600, 750, 900],
    "steel": {
        "ISHB": [[150, 150], [200, 200], [225, 225], [250, 250], [300, 250], [350, 250], [400, 250], [450, 250]],
        "ISWB": [[150, 100], [175, 125], [200, 140], [225, 150], [250, 200], [300, 200], [350, 200],
                 [400, 200], [450, 200], [500, 250], [550, 250], [600, 250]],
        "ISMB": [[100, 75], [125, 75], [150, 80], [175, 90], [200, 100], [225, 110], [250, 125], [300, 140],
                 [350, 140], [400, 140], [450, 150], [500, 180], [550, 190], [600, 210]],
        "SHS": [100, 113.5, 132, 150, 180, 200, 250, 300, 350, 400],
        "CHS": [114.3, 139.7, 165.1, 168.3, 219.1, 273, 323.9, 355.6, 406.4, 457, 508],
    },
    "imported": [
        {"name": "HE 200 A", "d": 190, "b": 200}, {"name": "HE 300 A", "d": 290, "b": 300},
        {"name": "HE 400 A", "d": 390, "b": 300}, {"name": "HE 200 B", "d": 200, "b": 200},
        {"name": "HE 240 B", "d": 240, "b": 240}, {"name": "HE 300 B", "d": 300, "b": 300},
        {"name": "HE 340 B", "d": 340, "b": 300}, {"name": "HE 400 B", "d": 400, "b": 300},
        {"name": "UC 152", "d": 152, "b": 152}, {"name": "UC 203", "d": 203, "b": 203},
        {"name": "UC 254", "d": 254, "b": 254}, {"name": "UC 305", "d": 305, "b": 305},
        {"name": "UC 356", "d": 356, "b": 368},
    ],
}

_LIB = []


def library_path():
    """The library file: config.json "column_library" (a path, relative to
    this folder) when it is set, else column_library.json here."""
    try:
        from .config import CONFIG
        p = CONFIG.get("column_library")
        if isinstance(p, str) and p.strip():
            return p if os.path.isabs(p) else os.path.join(_HERE, p)
    except Exception:                                 # noqa: BLE001
        pass
    return os.path.join(_HERE, FILE_NAME)


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _pairs(v):
    """[[a, b], ...] or [d, ...] -> [(a, b)], bad entries left out."""
    out = []
    for e in v or ():
        if isinstance(e, (list, tuple)) and len(e) >= 2:
            a, b = _num(e[0]), _num(e[1])
        else:
            a = b = _num(e)
        if a and b and a > 0 and b > 0:
            out.append((a, b))
    return out


def _tol(v, default):
    if isinstance(v, (list, tuple)) and v:
        mm = _num(v[0], default[0])
        share = _num(v[1], default[1]) if len(v) > 1 else default[1]
        return (mm, share)
    if _num(v) is not None:
        return (_num(v), default[1])
    return tuple(default)


def _words(v):
    if isinstance(v, str):
        v = [v]
    return [str(w).strip() for w in v or () if str(w).strip()]


def build(raw):
    """The library as the matcher reads it, from the JSON's content (a key
    left out takes the built-in value)."""
    raw = raw if isinstance(raw, dict) else {}
    get = lambda k: raw[k] if k in raw else BUILT_IN[k]  # noqa: E731
    lim = dict(BUILT_IN["limits"])
    lim.update({k: _num(v, lim.get(k)) for k, v in (raw.get("limits") or {}).items() if k in lim})
    tol = {k: tuple(v) for k, v in BUILT_IN["tolerance"].items()}
    for k, v in (raw.get("tolerance") or {}).items():
        tol[k] = _tol(v, tol.get(k, (10.0, 0.0)))
    projects = []
    for name, p in (get("projects") or {}).items():
        if not isinstance(p, dict) or str(name).startswith("_"):
            continue
        projects.append({"name": str(name), "match": _words(p.get("match")),
                         "type": str(p.get("type") or "Column"), "sizes": _pairs(p.get("sizes"))})
    steel = get("steel") or {}
    sections = []
    for fam in ("ISHB", "ISWB", "ISMB"):
        sections += [("%s %g" % (fam, d), d, b) for d, b in _pairs(steel.get(fam, BUILT_IN["steel"][fam]))]
    sections += [("SHS %g" % d, d, b) for d, b in _pairs(steel.get("SHS", BUILT_IN["steel"]["SHS"]))]
    imported = []
    for e in get("imported") or ():
        if isinstance(e, dict) and _num(e.get("d")) and _num(e.get("b")):
            imported.append((str(e.get("name") or "%g x %g" % (_num(e["d"]), _num(e["b"]))),
                             _num(e["d"]), _num(e["b"])))
    order = [str(s) for s in (get("match_order") or ()) if str(s) in ("project", "floor_confirmed", "steel",
                                                                      "imported", "rcc")]
    words = _words(get("not_column_layer_words"))
    not_rx = re.compile("|".join(re.escape(w.upper()) for w in words)) if words else None
    return {"enabled": bool(get("enabled")), "limits": lim, "tolerance": tol, "match_order": order,
            "not_column_layer_words": words, "not_column_layer_rx": not_rx,
            "projects": projects, "rcc_rect": _pairs(get("rcc_rect")),
            "rcc_round": [a for a, _b in _pairs(get("rcc_round"))],
            "steel": sections, "chs": [a for a, _b in _pairs(steel.get("CHS", BUILT_IN["steel"]["CHS"]))],
            "imported": imported}


def load(path=None):
    """Read the library file; a missing or broken one gives the built-in
    lists (and says why in "error")."""
    path = path or library_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lib = build(json.load(fh))
        lib["path"] = path
        return lib
    except (OSError, ValueError, TypeError, AttributeError) as e:
        lib = build({})
        lib["path"] = None
        lib["error"] = "%s: %s" % (os.path.basename(path), e)
        return lib


def library():
    """The library, read once."""
    if not _LIB:
        _LIB.append(load())
    return _LIB[0]


def context_names(dxf_path):
    """What a drawing is called, for the projects' match words: the file
    name it was uploaded as and the project name typed for it (the upload's
    <token>.meta.json beside <token>.dxf). [] when there is none."""
    if not dxf_path:
        return []
    names = [os.path.basename(str(dxf_path))]
    try:
        with open(os.path.splitext(str(dxf_path))[0] + ".meta.json", "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        names += [str(meta.get(k) or "") for k in ("original_name", "project_name")]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return [n for n in names if n.strip()]


def _word_in(word, names):
    """The word in a name, not inside a longer word: XA in XA_37th or XA37,
    not in MAXAM; CENTAURUS in centaurus11_floor."""
    rx = re.compile(r"(?<![A-Z])" + re.escape(word.upper()) + r"(?![A-Z])")
    return any(rx.search(n.upper()) for n in names)


def projects_for(names, lib=None):
    """The projects whose match words are in one of the names (a project
    with no match words: always)."""
    lib = lib or library()
    return [p for p in lib["projects"] if not p["match"] or any(_word_in(w, names or ()) for w in p["match"])]


def _near(x, ref, tol):
    mm, share = tol
    return abs(x - ref) <= max(mm, share * ref)


def _rect_hit(a, b, ra, rb, tol):
    """(a, b) against (ra, rb), either way round."""
    ra, rb = sorted((ra, rb))
    return _near(a, ra, tol) and _near(b, rb, tol)


def match(w, h, round_=False, confirmed=(), names=(), lib=None):
    """The column a closed shape w x h mm is by its size, looked up in the
    library's match_order; None when no size fits. round_: a circle (w = h
    = its diameter) - matched against the round sizes only. confirmed: the
    sizes [(a, b)] of the columns tier 1 found on this floor."""
    lib = lib or library()
    a, b = sorted((float(w), float(h)))
    if a <= 0:
        return None
    T = lib["tolerance"]
    for src in lib["match_order"]:
        if src == "project":
            for p in projects_for(names, lib):
                for ra, rb in p["sizes"]:
                    if (not round_ or abs(ra - rb) < 1.0) and _rect_hit(a, b, ra, rb, T["project"]):
                        return {"label": "%s %gx%g" % (p["name"], ra, rb), "type": p["type"], "source": "project",
                                "size": [ra, rb]}
        elif src == "floor_confirmed":
            for ra, rb in confirmed or ():
                if _rect_hit(a, b, ra, rb, T["floor_confirmed"]):
                    return {"label": "floor %gx%g" % (min(ra, rb), max(ra, rb)), "type": "Column",
                            "source": "floor_confirmed", "size": [min(ra, rb), max(ra, rb)]}
        elif src == "steel":
            if round_:
                for d in lib["chs"]:
                    if _near(a, d, T["steel"]) and _near(b, d, T["steel"]):
                        return {"label": "CHS %g" % d, "type": "Steel", "source": "steel", "size": [d, d]}
                continue
            for name, d, bf in lib["steel"]:
                if _rect_hit(a, b, d, bf, T["steel"]):
                    return {"label": name, "type": "Steel", "source": "steel", "size": [d, bf]}
        elif src == "imported":
            if round_:
                continue
            for name, d, bf in lib["imported"]:
                if _rect_hit(a, b, d, bf, T["imported"]):
                    return {"label": name, "type": "Steel", "source": "imported", "size": [d, bf]}
        elif src == "rcc":
            if round_:
                for d in lib["rcc_round"]:
                    if _near(a, d, T["round"]) and _near(b, d, T["round"]):
                        return {"label": "RCC dia %g" % d, "type": "RCC", "source": "rcc", "size": [d, d]}
                continue
            for ra, rb in lib["rcc_rect"]:
                if _rect_hit(a, b, ra, rb, T["rcc"]):
                    return {"label": "RCC %gx%g" % (min(ra, rb), max(ra, rb)), "type": "RCC", "source": "rcc",
                            "size": [min(ra, rb), max(ra, rb)]}
    return None
