# -*- coding: utf-8 -*-
"""The names of the layers this app writes, and how its readers read them.

Every layer the app writes is "AI_" + a name:

    <FAMILY>_BOUNDARY   -> AI_<FAMILY>           AI_TOILET, AI_SHAFT, AI_CIRC ...
    ROOM_BOUNDARY       -> AI_FLOOR              (the floor outline)
    ROOM_VOID           -> AI_VOID               (holes: columns, ducts in a room)
    AI_GEO_<CLASS>      -> AI_<FAMILY layer>     the same names as the text engine
    CMP_<CATEGORY>      -> AI_CMP_<CATEGORY>
    AI_WINDOW, AI_CUTOUT, AI_COLUMN, AI_WALL, AI_HANGING_WALL,
    AI_P_DOORS, AI_S_DOORS, AI_GLASS, AI_UNKNOWN_SHAPE, AI_REVIEW

Walls are drawn on one layer, AI_WALL. Drawings written before that used
AI_WALL_EXTERNAL / _CORE / _INTERNAL; those names stay reserved, so such a
drawing still reads its walls as walls. AI_HANGING_WALL shows a wall drawn
above floor level (an object under it): not a wall, its area in the room.

A family whose own name is one of the reserved names gets "_AREA" after it:
the Void family (layer VOID) is AI_VOID_AREA, as AI_VOID holds the holes.

Readers accept both forms - the new AI_<NAME> and the old <NAME>_BOUNDARY /
ROOM_VOID / AI_GEO_* - so a corrected DXF in the old names still works.
The reserved layers (AI_FLOOR, AI_VOID, AI_WINDOW, AI_CUTOUT, AI_COLUMN,
AI_WALL, AI_WALL_*, AI_HANGING_WALL, AI_P_DOORS, AI_S_DOORS, AI_GLASS,
AI_CMP_*, AI_REVIEW, and the door layers of older drawings AI_DOOR_PRIMARY,
AI_DOOR_SECONDARY, AI_DOOR, AI_DOOR_SHAFT, AI_DOOR_CUBICLE) are never rooms. AI_CUTOUT
holds the cut-outs in the walls (an X on a cut-out layer, no glazing):
measured as cut-outs, never as rooms. AI_REVIEW holds the element engine's
marks (elementid): an element scored below 50, or classified otherwise than
it is drawn - an outline and a note, never a room, never a label.
"""

PREFIX = "AI_"
FLOOR = "AI_FLOOR"
VOID = "AI_VOID"
WINDOW = "AI_WINDOW"
CUTOUT = "AI_CUTOUT"
COLUMN = "AI_COLUMN"
WALL = "AI_WALL"
# a wall drawn above floor level, with an object under it: shown, not a wall
HANGING_WALL = "AI_HANGING_WALL"
# a door opening in a wall: shown, never a room, never a wall (2026-10-17).
# Every door is primary or secondary:
#   AI_P_DOORS  a door between two different spaces - two rooms, or a room
#               and the common floor (a lift's or a shaft's door included):
#               it stops a room's boundary;
#   AI_S_DOORS  a door inside one room - a WC cubicle's, or a door with the
#               same room on both sides: it never stops a room.
# What it opens into (a room, a shaft, a WC cubicle) is stored on it (XDATA).
# AI_DOOR_PRIMARY / AI_DOOR_SECONDARY (primary: entered from the main
# accessible area; secondary: reached only through another room), AI_DOOR,
# AI_DOOR_SHAFT and AI_DOOR_CUBICLE are the layers of drawings written
# before; they are still read as doors.
DOOR_PRIMARY = "AI_P_DOORS"
DOOR_SECONDARY = "AI_S_DOORS"
OLD_DOOR_PRIMARY = "AI_DOOR_PRIMARY"
OLD_DOOR_SECONDARY = "AI_DOOR_SECONDARY"
DOOR = "AI_DOOR"
DOOR_SHAFT = "AI_DOOR_SHAFT"
DOOR_CUBICLE = "AI_DOOR_CUBICLE"
DOORS = (DOOR_PRIMARY, DOOR_SECONDARY, OLD_DOOR_PRIMARY, OLD_DOOR_SECONDARY, DOOR, DOOR_SHAFT, DOOR_CUBICLE)
# glass partitions and glazing (elements.glass_elements): shown, never a
# room, never a wall
GLASS = "AI_GLASS"
# the wall layers of drawings written before walls were one layer
WALL_EXTERNAL = "AI_WALL_EXTERNAL"
WALL_CORE = "AI_WALL_CORE"
WALL_INTERNAL = "AI_WALL_INTERNAL"
# a shape the geometry engine could not name: shown, never a room
UNKNOWN_SHAPE = "AI_UNKNOWN_SHAPE"
# the element engine's review marks (elementid): never a room, never a label
REVIEW = "AI_REVIEW"
WALLS = (WALL,)
OLD_WALLS = (WALL_EXTERNAL, WALL_CORE, WALL_INTERNAL)
RESERVED = frozenset((FLOOR, VOID, WINDOW, CUTOUT, COLUMN, UNKNOWN_SHAPE, HANGING_WALL, GLASS, REVIEW)
                     + DOORS + WALLS + OLD_WALLS)
RESERVED_PREFIXES = ("AI_CMP_", "AI_WALL_")
AREA_SUFFIX = "_AREA"

# The old names
OLD_SUFFIX = "_BOUNDARY"
OLD_FLOOR = "ROOM_BOUNDARY"
OLD_VOID = "ROOM_VOID"
OLD_GEO = "AI_GEO_"

# Geometry classes -> the family layer they are drawn on (the text engine's
# names). Classes that are not rooms keep a layer of their own.
_GEO_FAMILY_LAYER = {
    "STAIRCASE": "STAIR", "STAIR_WELL": "SHAFT", "SHAFT": "SHAFT", "LIFT": "LIFT",
    "LIFT_LOBBY": "LIFTLOBBY", "MACHINE_ROOM": "MACHINEROOM", "SERVICE_LOBBY": "SERVICELOBBY",
    "VOID": "VOID", "TOILET": "TOILET", "ROOM": "ROOM",
}
_GEO_OWN_LAYER = {"COLUMN": COLUMN, "WINDOW": WINDOW, "CUTOUT": CUTOUT,
                  "UNKNOWN_GEOMETRY": UNKNOWN_SHAPE, "UNKNOWN": UNKNOWN_SHAPE}


def family_layer(suffix):
    """A family's layer: AI_ + its layer suffix (family_rules.json "layer").
    A suffix that would land on a reserved name gets _AREA after it."""
    s = (suffix or "UNKNOWN").strip().upper()
    if s.endswith(OLD_SUFFIX):
        s = s[:-len(OLD_SUFFIX)]
    if s.startswith(PREFIX):
        s = s[len(PREFIX):]
    name = PREFIX + s
    if name in RESERVED or name.startswith(RESERVED_PREFIXES):
        name += AREA_SUFFIX
    return name


def geo_class_layer(cls):
    """The layer a geometry class is drawn on (AI_GEO_<CLASS> before)."""
    c = (cls or "").upper()
    if c in _GEO_OWN_LAYER:
        return _GEO_OWN_LAYER[c]
    return family_layer(_GEO_FAMILY_LAYER.get(c, c))


def ai_layer(name):
    """Any layer name, old or new, in the new scheme."""
    up = (name or "").strip().upper()
    if not up:
        return family_layer("UNKNOWN")
    if up == OLD_VOID:
        return VOID
    if up == OLD_FLOOR:
        return FLOOR
    if up.startswith(OLD_GEO):
        return geo_class_layer(up[len(OLD_GEO):])
    if up in RESERVED or up.startswith(RESERVED_PREFIXES):
        return up
    if up.startswith("CMP_"):
        return PREFIX + up
    if up.startswith(PREFIX):
        return up
    return family_layer(up)


def is_reserved(name):
    """A layer of the app's that is never a room."""
    up = (name or "").strip().upper()
    return up in RESERVED or up.startswith(RESERVED_PREFIXES)


def room_key(name):
    """The family layer suffix ("TOILET", "SHAFT", "VOID", "ROOM" ...) of a
    room boundary layer in either form, or None when the layer holds no
    rooms (a reserved layer, a drawing's own layer).

    Old names read as they always did: ROOM_BOUNDARY is the generic "ROOM"
    (the floor outline among them is told apart by its size, as before);
    AI_GEO_<CLASS> is that class's family layer (columns, windows and
    unknown shapes are not rooms)."""
    up = (name or "").strip().upper()
    if not up or is_reserved(up) or up == OLD_VOID:
        return None
    if up.endswith(OLD_SUFFIX):
        return up[:-len(OLD_SUFFIX)] or None
    if up.startswith(OLD_GEO):
        cls = up[len(OLD_GEO):]
        if cls in _GEO_OWN_LAYER or cls in ("UNKNOWN", "UNKNOWN_GEOMETRY"):
            return None
        return _GEO_FAMILY_LAYER.get(cls, cls)
    if up.startswith(PREFIX):
        key = up[len(PREFIX):]
        if key.endswith(AREA_SUFFIX) and (PREFIX + key[:-len(AREA_SUFFIX)]) in RESERVED:
            key = key[:-len(AREA_SUFFIX)]
        return key or None
    return None


def is_room_layer(name):
    return room_key(name) is not None


def is_void_layer(name):
    """Holes deducted from the room they stand in: AI_VOID, or the old
    ROOM_VOID / any *_VOID."""
    up = (name or "").strip().upper()
    if up == VOID:
        return True
    return not up.startswith(PREFIX) and up.endswith("_VOID")


def is_cutout_layer(name):
    """AI_CUTOUT: a cut-out in a wall, read as a cut-out, never a room."""
    return (name or "").strip().upper() == CUTOUT


def is_floor_layer(name):
    up = (name or "").strip().upper()
    return up == FLOOR


def is_app_layer(name):
    """Any layer this app writes (old or new names) - what a generated
    drawing carries over the source drawing's own layers."""
    up = (name or "").strip().upper()
    return (up.startswith(PREFIX) or up.endswith(OLD_SUFFIX) or up == OLD_VOID
            or up.startswith("CMP_"))


def is_door_layer(name):
    """A door opening: AI_P_DOORS, AI_S_DOORS, or a door layer of an older
    drawing (AI_DOOR_PRIMARY, AI_DOOR_SECONDARY, AI_DOOR, AI_DOOR_SHAFT,
    AI_DOOR_CUBICLE)."""
    return (name or "").strip().upper() in DOORS


def door_layer_kind(name):
    """What an older drawing's door layer says a door opens into: "exit"
    (AI_DOOR), "shaft" (AI_DOOR_SHAFT), "cubicle" (AI_DOOR_CUBICLE); None
    for the primary / secondary layers (the door's XDATA says)."""
    return {DOOR: "exit", DOOR_SHAFT: "shaft", DOOR_CUBICLE: "cubicle"}.get((name or "").strip().upper())


def door_layer_level(name):
    """"primary" / "secondary" by the door layer (AI_P_DOORS / AI_S_DOORS,
    or the older AI_DOOR_PRIMARY / AI_DOOR_SECONDARY), None for the older
    kind layers."""
    return {DOOR_PRIMARY: "primary", DOOR_SECONDARY: "secondary",
            OLD_DOOR_PRIMARY: "primary", OLD_DOOR_SECONDARY: "secondary"}.get((name or "").strip().upper())
