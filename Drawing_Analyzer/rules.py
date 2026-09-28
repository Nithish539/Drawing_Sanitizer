# -*- coding: utf-8 -*-
"""The rules the boundary generator applies, as data the user can switch.

Everything the drawing pass does beyond "find the label, find the walls"
is a judgement made on the user's behalf: that an X crossing marks a
shaft, that a fill escaping its room should be distrusted, that a fire
hose tag is equipment rather than a room. Each of those is right on most
drawings and wrong on some, and until now the only way to find out was to
read the source.

They are listed here with what they do and what happens when they are
off, so the UI can offer them as a checklist and the run can report which
ones were in force. Defaults reproduce the behaviour of the program
before any of this existed - unticking is always a deliberate act - with
one exception: closing openings that have no door is off by default, so
an alcove or urinal bay belongs to the room it opens off. They are still
closed for a room whose fill leaks (close_gaps_on_leak).

`detail` is written for whoever is looking at a wrong boundary and
wondering which rule produced it, so each says what turning it off costs
as well as what it buys.
"""

from __future__ import annotations

from collections import OrderedDict

GROUPS = [
    ("shafts", "Shaft marks (X crossings)"),
    ("shape", "Room shape"),
    ("walls", "What counts as a wall"),
    ("guards", "Sanity checks"),
    ("labels", "Labels"),
]

RULES = [
    # -- shafts ------------------------------------------------------------
    {
        "key": "x_marks",
        "group": "shafts",
        "title": "An X crossing marks a shaft",
        "detail": "Two diagonals of matching length crossing at their "
                  "midpoints are read as a shaft, lift well or cut-out, and "
                  "the shape gets a boundary of its own. Off: shafts are "
                  "only found the way rooms are, from their label outwards.",
        "default": True,
    },
    {
        "key": "x_wall_snap",
        "group": "shafts",
        "title": "Snap a shaft out to the wall around it",
        "detail": "Where the walls hug the X within a few centimetres, the "
                  "boundary lands on the wall face instead of on the tips of "
                  "the diagonals. Off: the X's own extent is always used.",
        "default": True,
    },
    {
        "key": "x_unlabelled",
        "group": "shafts",
        "title": "Draw shafts that carry no label",
        "detail": "An X with no text inside or beside it is still drawn, on "
                  "UNKNOWN_BOUNDARY. Off: unnamed shafts are left out "
                  "entirely.",
        "default": True,
    },
    {
        "key": "x_adopt_labels",
        "group": "shafts",
        "title": "Let an unnamed shaft take a spare label",
        "detail": "A shaft still unnamed at the end takes the nearest text "
                  "that named nothing else - the case of a label on a long "
                  "leader. A text that already named a room is never taken.",
        "default": True,
    },

    # -- shape -------------------------------------------------------------
    {
        "key": "trace_rooms",
        "group": "shape",
        "title": "Follow the real room shape",
        "detail": "Flood-fill the space the walls enclose, so angled, curved "
                  "and L-shaped rooms come out as their real outline. Off: "
                  "every boundary is the rectangle four rays find, which is "
                  "faster and always a box.",
        "default": True,
    },
    {
        "key": "deduct_voids",
        "group": "shape",
        "title": "Cut columns and voids out of a room",
        "detail": "Anything enclosed inside a traced room - a column, a duct "
                  "- is drawn as a void and its area deducted. Off: the room "
                  "is measured straight across them.",
        "default": True,
    },
    {
        "key": "secondary_boundaries",
        "group": "shape",
        "title": "Draw boundaries for secondary labels too",
        "detail": "A text that does not read like a room name still gets a "
                  "boundary if nothing else claimed it. Off: only labels that "
                  "look like room names are drawn.",
        "default": True,
    },

    # -- walls -------------------------------------------------------------
    {
        "key": "close_doors",
        "group": "walls",
        "title": "Close door openings",
        "detail": "Where a door swing stands in an opening, both wall faces "
                  "are carried straight across it, so the boundary runs "
                  "along the wall past the door instead of reaching in "
                  "through it. Off: doorways stay open and a room's fill "
                  "runs on into the next space.",
        "default": True,
    },
    {
        "key": "close_wall_gaps",
        "group": "walls",
        "title": "Close openings that have no door",
        "detail": "Two wall ends facing each other across a door-sized gap, "
                  "in line with the same wall, are bridged even though no "
                  "door is drawn there. Off (the default): an opening with "
                  "no door belongs to the room it opens off, so a urinal bay "
                  "or an alcove is part of the toilet.",
        "default": False,
    },
    {
        "key": "close_gaps_on_leak",
        "group": "walls",
        "title": "Close doorless openings when a room leaks",
        "detail": "A room is first traced with its doorless openings open, "
                  "so an alcove or urinal bay stays part of it. If that fill "
                  "escapes, or reaches the name of a room of another family, "
                  "the room is traced again with every doorless opening in "
                  "a real wall closed straight across, so the label still "
                  "gets the room its walls enclose. Off: a leaking room "
                  "falls back to its four-ray box.",
        "default": True,
    },
    {
        "key": "threshold_ramps",
        "group": "walls",
        "title": "Keep door ramps and thresholds out of the room",
        "detail": "A line as wide as a door opening, running parallel to it "
                  "300-1500 mm away - the inner edge of a ramp or threshold "
                  "drawn in the doorway - closes the opening instead of the "
                  "wall face, so the room stops at the ramp. Off: the ramp "
                  "is measured as part of the room.",
        "default": True,
    },
    {
        "key": "floor_outline_walls",
        "group": "walls",
        "title": "Treat the floor outline as a wall",
        "detail": "The slab edge stops a ray running off the plate and "
                  "confines a leaking fill to its own floor. Off: rays and "
                  "fills can run past the edge of the plan.",
        "default": True,
    },

    # -- guards ------------------------------------------------------------
    {
        "key": "overspill_guard",
        "group": "guards",
        "title": "Distrust a fill that escapes its room",
        "detail": "A fill that comes back several times larger than the box "
                  "around the same label, and holds the name of a room of "
                  "another family, has leaked through a gap, so the box is "
                  "used instead. Off: the leak is drawn - on the test plate "
                  "that was a 191 sq.m AHU room covering the staircases "
                  "beside it.",
        "default": True,
    },
    {
        "key": "leaked_box_cap",
        "group": "guards",
        "title": "Cap an unverified boundary",
        "detail": "When the fill leaks and cannot confirm the walls enclose "
                  "the label, an oversized four-ray box is dropped rather "
                  "than drawn. Off: a tag standing in open floor can claim a "
                  "slab of the plate.",
        "default": True,
    },
    {
        "key": "engulf_guard",
        "group": "guards",
        "title": "Reject a boundary that swallows other rooms",
        "detail": "A boundary containing several rooms already drawn is a "
                  "leak that came back holding its neighbours, so it is "
                  "discarded. Shafts never count against it.",
        "default": True,
    },
    {
        "key": "merge_overlaps",
        "group": "guards",
        "title": "Merge boundaries that sit on top of each other",
        "detail": "Two labels in one room produce two near-identical "
                  "boundaries; they are merged and the better name kept. A "
                  "room standing inside a larger one is left alone either "
                  "way. Off: both are drawn.",
        "default": True,
    },

    # -- labels ------------------------------------------------------------
    {
        "key": "equipment_tags",
        "group": "labels",
        "title": "Equipment tags are not rooms",
        "detail": "FHC, FHR, FCU and the like name a device on a wall, not a "
                  "room, so they take no boundary of their own - though they "
                  "can still title a shaft. Off: every tag gets a boundary. "
                  "The list lives in config.json.",
        "default": True,
    },
    {
        "key": "skip_notes",
        "group": "labels",
        "title": "Long texts are notes, not room names",
        "detail": "A text longer than the room-name limit is a drawing note "
                  "- 'FOR DETAILS OF STAIRCASE-01 REFER DRG...' - and is not "
                  "given a boundary. Off: notes are treated as room names.",
        "default": True,
    },
    {
        "key": "retitle_inside",
        "group": "labels",
        "title": "Prefer a better name found inside a boundary",
        "detail": "Where a boundary ends up titled with something the family "
                  "rules do not recognise, a text inside it that they do "
                  "recognise takes over. This is what moves ST-01 onto "
                  "STAIR_BOUNDARY instead of leaving it Unknown.",
        "default": True,
    },
]

_BY_KEY = OrderedDict((r["key"], r) for r in RULES)


def defaults():
    """Every rule at its shipped setting."""
    return {r["key"]: bool(r["default"]) for r in RULES}


def resolve(overrides=None):
    """Merge what the user sent over the defaults.

    Unknown keys are ignored rather than rejected: a saved selection from
    an older build should not fail a run, it should just not apply.
    """
    out = defaults()
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if key in out:
                out[key] = bool(value)
    return out


def catalogue():
    """The list the UI renders: groups in order, each with its rules."""
    groups = []
    for key, title in GROUPS:
        members = [dict(r) for r in RULES if r["group"] == key]
        if members:
            groups.append({"key": key, "title": title, "rules": members})
    return groups


def changed_from_default(active):
    """Which rules the user actually moved, for the run report."""
    base = defaults()
    return sorted(k for k, v in (active or {}).items()
                  if k in base and bool(v) != base[k])


def describe(key):
    return _BY_KEY.get(key)
