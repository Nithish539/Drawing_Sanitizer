# -*- coding: utf-8 -*-
"""Layer inventory - what a drawing is actually built from.

Step 1 already opens the file to look for floor plates, so the layer table
is read in that same pass and costs nothing extra at upload.

Layers are grouped the way rooms are: a layer whose name matches a family
keyword joins that family (A-STAIRS -> Staircase), and everything else
falls back to the discipline letter its name begins with - A for
architectural, S for structural, M for mechanical and so on, per the AIA
layer-naming convention that most consultants follow. On a real drawing
the family keywords catch about one layer in eight, so without that second
tier the whole thing would be one bucket called Unknown.

The wall column matters more than it looks: it says whether the boundary
generator treats that layer's linework as a wall. A vanity counter on
Q-CASE reads as a wall and cuts a toilet in half, and this is where you
can see that before wondering why the boundary came out wrong.
"""

from __future__ import annotations

import collections

from .config import CONFIG

# AIA CAD Layer Guidelines discipline codes. Only a single leading letter
# is recognised - 'ST-SLAB LINE' is stairs, not structural, and guessing
# from two-letter prefixes gets that wrong more often than right.
_DEFAULT_DISCIPLINES = {
    "A": "Architectural", "C": "Civil", "E": "Electrical",
    "F": "Fire protection", "G": "General", "H": "Hazardous materials",
    "I": "Interiors", "L": "Landscape", "M": "Mechanical", "P": "Plumbing",
    "Q": "Equipment", "R": "Resource", "S": "Structural",
    "T": "Telecommunications", "V": "Survey", "W": "Distribution",
    "X": "Other disciplines", "Z": "Contractor",
}

OTHER_GROUP = "Other layers"

_TEXT_TYPES = {"TEXT", "MTEXT", "ATTDEF", "ATTRIB"}


def _disciplines():
    cfg = CONFIG.get("layer_disciplines") or {}
    out = dict(_DEFAULT_DISCIPLINES)
    for k, v in cfg.items():
        if not str(k).startswith("_"):
            out[str(k).strip().upper()] = str(v)
    return out


def discipline_of(layer_name, table=None):
    """The discipline a layer name declares, or None.

    The code is the first token of the name, up to the first separator:
    'A-WALL' and 'A_WALL' and 'A WALL' all read as A.
    """
    table = _disciplines() if table is None else table
    head = ""
    for ch in (layer_name or "").strip():
        if ch in "-_. ":
            break
        head += ch
    head = head.upper()
    return table.get(head) if len(head) == 1 else None


def _is_closed(entity):
    try:
        if entity.dxftype() == "LWPOLYLINE":
            return bool(entity.closed)
        if entity.dxftype() == "POLYLINE":
            return bool(entity.is_closed)
    except (AttributeError, TypeError):
        pass
    return False


def collect_layers(doc, msp, rules, family_for_label,
                   never_walls=(), thin_only=()):
    """Read the layer table and count what sits on each layer.

    `family_for_label` is passed in rather than imported so this module
    stays free of the boundary code, which imports plenty of its own.

    Counts come from modelspace and, separately, from block definitions:
    a layer can be empty in modelspace and still carry the whole drawing
    through a block, which is worth seeing rather than reporting as unused.
    """
    table = _disciplines()
    never = [k.upper() for k in never_walls]
    thin = [k.upper() for k in thin_only]

    stats = collections.defaultdict(
        lambda: {"entities": 0, "in_blocks": 0, "texts": 0, "closed": 0,
                 "types": collections.Counter()})

    def tally(entity, key):
        try:
            name = entity.dxf.layer
        except AttributeError:
            return
        rec = stats[name or "0"]
        rec[key] += 1
        t = entity.dxftype()
        rec["types"][t] += 1
        if t in _TEXT_TYPES:
            rec["texts"] += 1
        elif _is_closed(entity):
            rec["closed"] += 1

    for e in msp:
        tally(e, "entities")
    for block in doc.blocks:
        # Layout blocks are the paper-space plumbing, not drawing content.
        if block.name.lower().startswith(("*model_space", "*paper_space")):
            continue
        for e in block:
            tally(e, "in_blocks")

    layers = []
    for lay in doc.layers:
        name = lay.dxf.name
        rec = stats.get(name, {"entities": 0, "in_blocks": 0, "texts": 0,
                               "closed": 0, "types": collections.Counter()})
        upper = (name or "").upper()
        fam = family_for_label(name, rules)
        fam_name = fam["name"] if fam and fam["name"] != "Unknown" else None
        disc = discipline_of(name, table)
        if any(k in upper for k in never):
            wall = "no"
        elif any(k in upper for k in thin):
            wall = "thin tier only"
        else:
            wall = "yes"
        try:
            colour = int(lay.dxf.color)
        except (AttributeError, TypeError, ValueError):
            colour = 7
        layers.append({
            "name": name,
            "family": fam_name,
            "group": fam_name or disc or OTHER_GROUP,
            "group_kind": "family" if fam_name else (
                "discipline" if disc else "other"),
            "discipline": disc,
            "entities": rec["entities"],
            "in_blocks": rec["in_blocks"],
            "texts": rec["texts"],
            "closed_polylines": rec["closed"],
            "kinds": ", ".join("%s %d" % (t, n) for t, n
                               in rec["types"].most_common(3)),
            "color": abs(colour),
            "lineweight": int(getattr(lay.dxf, "lineweight", -1) or -1),
            "linetype": getattr(lay.dxf, "linetype", "") or "",
            "frozen": bool(lay.is_frozen()),
            "off": not bool(lay.is_on()),
            "locked": bool(lay.is_locked()),
            "counts_as_wall": wall,
        })

    # A layer used by an entity but missing from the table is legal DXF and
    # does happen; report it rather than dropping its content on the floor.
    known = {L["name"] for L in layers}
    for name, rec in stats.items():
        if name in known:
            continue
        fam = family_for_label(name, rules)
        fam_name = fam["name"] if fam and fam["name"] != "Unknown" else None
        disc = discipline_of(name, table)
        layers.append({
            "name": name, "family": fam_name,
            "group": fam_name or disc or OTHER_GROUP,
            "group_kind": "family" if fam_name else (
                "discipline" if disc else "other"),
            "discipline": disc,
            "entities": rec["entities"], "in_blocks": rec["in_blocks"],
            "texts": rec["texts"], "closed_polylines": rec["closed"],
            "kinds": ", ".join("%s %d" % (t, n) for t, n
                               in rec["types"].most_common(3)),
            "color": 7, "lineweight": -1, "linetype": "",
            "frozen": False, "off": False, "locked": False,
            "counts_as_wall": "yes", "not_in_table": True,
        })

    layers.sort(key=lambda L: (-(L["entities"] + L["in_blocks"]),
                               L["name"].upper()))
    return {"layers": layers, "groups": group_layers(layers, rules),
            "summary": summarise(layers)}


def group_layers(layers, rules):
    """Family groups first, in the order of the rules file, then the
    disciplines, then whatever declares neither."""
    order = {fam["name"]: i for i, fam in enumerate(rules)}
    buckets = collections.OrderedDict()
    for L in layers:
        buckets.setdefault(L["group"], []).append(L)

    def rank(name):
        members = buckets[name]
        kind = members[0]["group_kind"]
        if kind == "family":
            return (0, order.get(name, 999), name)
        if kind == "discipline":
            return (1, 0, name)
        return (2, 0, name)

    out = []
    for name in sorted(buckets, key=rank):
        members = buckets[name]
        out.append({
            "name": name,
            "kind": members[0]["group_kind"],
            "layers": members,
            "layer_count": len(members),
            "entities": sum(L["entities"] + L["in_blocks"] for L in members),
            "texts": sum(L["texts"] for L in members),
        })
    return out


def summarise(layers):
    used = [L for L in layers if L["entities"] or L["in_blocks"]]
    return {
        "layers": len(layers),
        "layers_with_content": len(used),
        "empty_layers": len(layers) - len(used),
        "entities": sum(L["entities"] for L in layers),
        "entities_in_blocks": sum(L["in_blocks"] for L in layers),
        "texts": sum(L["texts"] for L in layers),
        "closed_polylines": sum(L["closed_polylines"] for L in layers),
        "families_seen": len({L["family"] for L in layers if L["family"]}),
        "wall_layers": len([L for L in layers
                            if L["counts_as_wall"] == "yes"
                            and (L["entities"] or L["in_blocks"])]),
    }
