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

# What an X mark is - a shaft, a lift, a window, a cut-out, a column or a
# void - is not a rule anyone switches (2026-10-16): the X sequence decides
# it, once, in this order, and nothing overrides it (xplace.py):
#   1 every X mark on the floor, on any layer and of any size, is a void
#   2 the shaft rules     (a wall within x_placement.wall_gap_mm of a side)
#   3 the lift rules      (a LIFT label or lift size, walled on 3 sides)
#   4 the window rules    (in a wall, with glazing / a window frame / tag)
#   5 the cut-out rules   (in a wall, or a cut-out tag, or loose)
#   6 the column rules    (the cross of a column)
#   7 what is left stays a void
# The switches that once changed that answer (x_marks, x_by_geometry,
# x_placement, windows_in_walls, dashed_x_cutouts, stair_void_hole,
# lift_x_any_layer, x_opening_is_shaft, no_round_shafts, shaft_on_wall,
# x_unlabelled, trap_door_in_room) are gone; a saved selection naming them
# is ignored (resolve).
RULES = [
    # -- shafts ------------------------------------------------------------
    {
        "key": "shaft_room_to_door",
        "group": "shafts",
        "title": "A shaft's room runs to its door",
        "detail": "An X-marked cut-out at the back of a walled shaft cell is "
                  "the shaft's slab opening; the space in front of it, up to "
                  "the cell's door, is drawn as the shaft's room too, under "
                  "its name (P285: TEN, COMM, FAPA). The area statement still "
                  "deducts only the cut-out, as the manual sheets do. Off: the "
                  "shaft is only its cut-out and the space in front is left "
                  "untraced.",
        "default": True,
    },
    {
        "key": "join_shaft_strips",
        "group": "shafts",
        "title": "One shaft round its X-marked strips",
        "detail": "Two X-marked strips of one length lying face to face, up to "
                  "700 mm apart with no wall between (the slab openings at "
                  "either side of one walled shaft - H10's PHE shafts) are "
                  "one shaft: the strips and the band between them. Off "
                  "(default): each strip is its own opening, as the manual "
                  "area sheets of H10 count them.",
        "default": False,
    },
    {
        "key": "clean_outlines",
        "group": "shape",
        "title": "Clean slits and spikes out of outlines",
        "detail": "A line ending inside a room leaves a 0-2 mm slit in its "
                  "outline. After tracing and the guardrail every outline is "
                  "closed by 30 mm and opened by 25 mm (square corners kept): "
                  "slits and spikes narrower than 50 mm go, notches and holes "
                  "100 mm and wider (columns, real walls) stay.",
        "default": True,
    },
    {
        "key": "draw_walls",
        "group": "walls",
        "title": "Draw the walls",
        "detail": "The walls the boundaries were traced against are drawn on "
                  "one layer, AI_WALL (a wall is a wall: no external / core / "
                  "internal classes), with columns, windows and door openings "
                  "cut out. They are never rooms and never counted in areas. "
                  "Off: not drawn.",
        "default": True,
    },
    {
        "key": "hanging_walls",
        "group": "walls",
        "title": "A wall with an object under it is a hanging wall",
        "detail": "A wall with a door swinging through it, or a WC or basin "
                  "standing in it, is above floor level (a beam, a bulkhead). "
                  "The whole element, start to end, is not a wall: the room "
                  "runs under it and its area is counted in the room; it is "
                  "drawn on AI_HANGING_WALL for checking. Where part of it "
                  "stands on a real wall, that part stays wall. Lift graphics "
                  "and loose lines (drains, section lines) are never objects "
                  "under a wall. Off: such an element is a wall.",
        "default": True,
    },
    {
        "key": "shaft_room_mep",
        "group": "shafts",
        "title": "A shaft's room with a door is not a shaft",
        "detail": "A room that shares a side with an X-marked shaft and has a "
                  "door of its own is the shaft's room, not a shaft: it goes "
                  "on AI_MEP whatever its name (P285: ELE SHAFT, CHW SHAFT, the "
                  "PHE middle band, FHR, FHC, FHC-3). The statement files it "
                  "under SHAFT ROOMS, not deducted. Off: it keeps the layer its "
                  "name gives (AI_SHAFT, AI_FIRE).",
        "default": True,
    },
    {
        "key": "lift_no_door_machine_room",
        "group": "shafts",
        "title": "A lift with no door onto a machine room is the machine room's",
        "detail": "A lift well with no door of its own whose front faces a "
                  "lift machine room (P285 11th floor: the eight LIFT WELL "
                  "BELOW beside LIFT ELE.PANELS) goes on AI_MACHINEROOM, and "
                  "the statement files it under MACHINE ROOM, not LIFT SHAFT. "
                  "Off: it stays a lift.",
        "default": True,
    },
    {
        "key": "lift_without_door_mechanical",
        "group": "shafts",
        "title": "A lift with no door opening is a mechanical room",
        "detail": "A lift - an X lift or one traced from its label - is "
                  "checked for a gap in its walls. A gap is its door: a door "
                  "line is drawn across it (AI_P_DOORS). A lift with no gap "
                  "and no door is not a lift that stops on this floor: it goes "
                  "on AI_MECHANICALROOM (P285 11th floor: the eight LIFT WELL "
                  "BELOW), and the statement files it under MACHINE ROOM. A "
                  "family picked by hand in Step 3 is never changed. Off: it "
                  "stays a lift.",
        "default": True,
    },
    {
        "key": "rotated_cores",
        "group": "shape",
        "title": "A core drawn at an angle is worked out turned to the axes",
        "detail": "Every test measures along the drawing's axes. A core or "
                  "wing whose walls run at an angle (Centaurus: the S3 fire "
                  "tower, 20 degrees off) is cut out into a drawing of its "
                  "own, turned straight, worked out by the same pass, and "
                  "the result turned back; the same in the geometry engine. "
                  "A whole drawing standing at an angle is one such core. "
                  "Off: angled cores are read as drawn (their stairs, lifts "
                  "and X marks are missed).",
        "default": True,
    },
    {
        "key": "shaft_compartment",
        "group": "shafts",
        "title": "A shaft is the compartment its X stands in",
        "detail": "The X marks the shaft; it is not its boundary. The shaft "
                  "is traced from the X like a room, closing on walls, the "
                  "facade, columns, its door and the plain line drawn "
                  "between it and its shaft room. The trace must hold most "
                  "of the X and stay within three times its area (or the "
                  "stated size); else the X stays as drawn and is outlined "
                  "on AI_REVIEW. An X across two compartments gives one "
                  "shaft each; a room already traced over the compartment "
                  "takes the X. In the final drawing a traced compartment "
                  "stands against the geometry's X box. Off: the X box is "
                  "the shaft (cut to the walls).",
        "default": True,
    },
    {
        "key": "x_frames_not_walls",
        "group": "walls",
        "title": "An X's own frame is not a wall",
        "detail": "The lines running corner to corner along an X's box are "
                  "its frame, not wall faces: a 300 mm X with a rectangle "
                  "round it otherwise reads as a 300 mm wall. The frame is "
                  "still a drawn line to the shaft's own trace, so what a "
                  "shaft deducts does not change. Off: a frame on a wall or "
                  "cut-out layer is a wall face.",
        "default": True,
    },
    {
        "key": "element_pipeline",
        "group": "walls",
        "title": "Elements first: walls, then the X marks, then the rooms - each decided once and locked",
        "detail": "The walls are traced first with the X marks' boxes and "
                  "the columns held out of them (no hatch inside an X's box "
                  "is wall material), every X is decided before any room is "
                  "traced from a label (an X stands in a room when the "
                  "space round it is enclosed, whatever is written in it; a "
                  "stair's well has a flight on either side), and the walls "
                  "drawn (AI_WALL) are built again on what is not decided: "
                  "never over a shaft's opening, a lift well, a void or a "
                  "loose cut-out - a piece reclaimed from the walls never "
                  "comes back. Off: the text-first order - the rooms are "
                  "traced from their labels first and the X marks decided "
                  "on them, the walls drawn as before and cut back after.",
        "default": True,
    },
    {
        "key": "x_label_not_across_wall",
        "group": "shafts",
        "title": "A label does not name an X across a wall",
        "detail": "A shaft label standing outside an X, with no stated size, "
                  "does not name the X when a wall stands between them "
                  "(x_placement.label_wall_cross_mm of wall on the line from "
                  "the label to the X): it names the room it stands in (XA: "
                  "CA-ELEC SH on the face of the wall beside the ELECTRICAL "
                  "SH X). A label stating the X's size still reaches across. "
                  "Off: the nearest shaft label names the X.",
        "default": True,
    },
    {
        "key": "x_named_by_its_room",
        "group": "shafts",
        "title": "An unnamed X takes the name of the shaft room it opens in",
        "detail": "An X with no label of its own, inside or along a small "
                  "room named for a shaft (XA: the 2500 x 500 X at the back "
                  "of ELECTRICAL SH), is that shaft: it takes the room's "
                  "name, and the room in front of it is the shaft's room "
                  "(AI_MEP, as the shaft room rule makes it). Off: the X "
                  "stays an Unknown Shaft.",
        "default": True,
    },
    {
        "key": "label_strip_not_wall",
        "group": "walls",
        "title": "A wall piece holding a room's name is that room",
        "detail": "A wall does not change its thickness except where "
                  "another wall or a column meets it: a strip holding a "
                  "room's or a shaft's name, and not carrying on in line at "
                  "its thickness, is the space it names (XA: PHE SH, COM "
                  "ELV SH read as walls). Off: drawn as wall.",
        "default": True,
    },
    {
        "key": "x_box_not_wall",
        "group": "walls",
        "title": "A shaft's X beside a wall is not part of the wall",
        "detail": "A shaft, a lift or a machine room's well marked with an "
                  "X is never drawn as wall (P285: eight lift wells and 14 "
                  "shafts were also on AI_WALL); a window or a cut-out in a "
                  "wall still is. Off: as the wall drawing makes it.",
        "default": True,
    },
    {
        "key": "lift_label_fits_its_x",
        "group": "shafts",
        "title": "A lift's label names only an X that fits it",
        "detail": "A lift label that states its size (20PAX LIFT 2500X2600) "
                  "is not taken by an X beside it of another size (Centaurus: "
                  "a 2450 x 600 X over the lift, which then lost the lift). "
                  "The lift is traced from its label instead. Off: the nearest "
                  "X may take it.",
        "default": True,
    },
    {
        "key": "x_cell_never_wall",
        "group": "shafts",
        "title": "An X-marked cell is never wall",
        "detail": "A thin X-marked shaft whose two sides are as far apart as "
                  "a wall's faces (XA: the 300 mm ELE SH strips) is a slab "
                  "opening, so it is cut out of the wall around it. An X that "
                  "a hatched or doubled wall crosses for a small part of it "
                  "(Centaurus STP EX., drawn as a rectangle over an angled "
                  "wall) is cut back to that wall instead. Off: the wall is "
                  "drawn over the X, and the X over the wall.",
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
        "key": "close_at_found_doors",
        "group": "walls",
        "title": "Close a leaking room at the doors drawn",
        "detail": "A room whose fill runs out through a door opening found for "
                  "the drawing (AI_P_DOORS / AI_S_DOORS: a swing at a gap in a wall, arcs and "
                  "ellipses alike) is closed there, where the faces carried "
                  "across from the wall ends did not close it - a door between "
                  "two short returns (H10 Tower 1: the G. TOILET door between a "
                  "300 mm nib and a wall corner; the toilet ran out into its "
                  "lobby and came back as a box). Closed with the doorless "
                  "openings on the leak path, so a room that does not leak "
                  "keeps its entry lobby, and an unlabelled lobby shut off is "
                  "joined to it again. Off: rooms are closed only where a wall "
                  "face runs on to the door.",
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

    {
        "key": "other_level_structure",
        "group": "walls",
        "title": "Another level's structure XREF is not a wall",
        "detail": "An XREF of the basement or podium structure laid under a "
                  "typical floor (XREF-..$0$BASE._STR-COLUMN) is on a COLUMN "
                  "layer, but its lines are not on this floor. They are left "
                  "out, except on basement, podium and stilt floors. Off: they "
                  "are walls - on BHUB one cut the WC cubicles off a toilet.",
        "default": True,
    },
    {
        "key": "fixtures_not_walls",
        "group": "walls",
        "title": "Sanitary fixtures are not walls",
        "detail": "Lines on a fixture layer (toil-fix, P-SANR-FIXT...) - a WC "
                  "pan's two outline lines read as a wall - are left out. "
                  "Off: they can notch a toilet's outline.",
        "default": True,
    },
    {
        "key": "break_lines",
        "group": "walls",
        "title": "A diagonal with a free end is a break line",
        "detail": "Two parallel diagonals across a stair - a break or cut "
                  "line - read as a wall and notched or split the stair. A "
                  "real angled wall meets other lines at both ends; one that "
                  "stops in the open is dropped. Off: every pair is a wall.",
        "default": True,
    },

    {
        "key": "never_cross_walls",
        "group": "walls",
        "title": "A boundary never crosses a wall, hatch or facade (hard guardrail)",
        "detail": "The last step: any boundary crossing a wall line, a wall or "
                  "structure hatch, or the facade is cut there and keeps the "
                  "piece holding its label; a column inside a room becomes a "
                  "hole. Off: a guessed box or a merged pair can run through "
                  "a wall.",
        "default": True,
    },
    {
        "key": "overrun_wall_ends",
        "group": "walls",
        "title": "A line across a wall's end closes it, even if it runs past",
        "detail": "A wall strip is often closed by a line that runs on beyond "
                  "it (309 mm across a 200 mm strip). It now counts as the "
                  "wall's end, so a room does not run into the strip. Off: "
                  "only a line joining the two face ends exactly closes it.",
        "default": True,
    },
    {
        "key": "existing_boundaries_walls",
        "group": "walls",
        "title": "Shafts, lifts and voids already drawn are walls",
        "detail": "X shafts, lift wells, voids and shapes chosen in the "
                  "Geometry tab are settled before any room is traced, and "
                  "their outlines stop the rooms traced after them. Off: a "
                  "room separated from a shaft by a single line runs into it.",
        "default": True,
    },
    {
        "key": "lines_last_resort",
        "group": "walls",
        "title": "Plain lines close a room as a last resort",
        "detail": "Facades, concrete and block walls, doors and settled "
                  "boundaries come first. Where none closes a side, the plain "
                  "lines drawn there do, before the room falls back to a "
                  "guessed box. Off: such a room gets the box, or nothing. "
                  "Never used while 'A room boundary stands only on "
                  "evidence' is on.",
        "default": True,
    },
    {
        "key": "boundary_on_evidence",
        "group": "walls",
        "title": "A room boundary stands only on evidence",
        "detail": "Every side of a room lies on a wall, the facade (the glass "
                  "line where glass is drawn, else the floor plate's edge), "
                  "another room already identified (a shaft, lift, void, X), "
                  "a primary door line (AI_P_DOORS) or a column. A secondary "
                  "door (a WC cubicle's) never stops a room. Plain lines, the "
                  "four-ray box, a corridor box, a stated-size rectangle and "
                  "the box round two merged rooms are never used: a room that "
                  "cannot close on evidence is not drawn and is listed as "
                  "'no evidence'. After the rooms are drawn every side is "
                  "checked, and a stretch standing on none of these is marked "
                  "on AI_REVIEW. Off: the plain lines and the guessed boxes "
                  "close a room as before.",
        "default": True,
    },
    {
        "key": "lift_lobby_to_lift_bank",
        "group": "walls",
        "title": "An open lift lobby ends at its lift bank",
        "detail": "A lift lobby that the walls, doors and lifts do not close "
                  "(an end open to the corridor, BHUB 1st floor: the upper "
                  "zone lobby) is closed across its open end at the outer "
                  "limit of the lifts and the walls round them. Off: such a "
                  "lobby is not drawn while boundaries stand only on evidence.",
        "default": True,
    },

    # -- shape ---------------------------------------------------------------
    {
        "key": "straighten_outlines",
        "group": "shape",
        "title": "Outlines turn only at an obstacle",
        "detail": "Steps and spikes narrower than 100 mm - a fill nosing into "
                  "a door jamb, two wall lines that miss each other - are "
                  "straightened. Off: the outline keeps every small step.",
        "default": True,
    },
    {
        "key": "no_overlaps",
        "group": "shape",
        "title": "No two areas overlap",
        "detail": "Rooms give way to shafts, lifts and voids and to the "
                  "smaller rooms kept before them; outlines may touch but "
                  "not overlap. A room left with under a quarter of itself "
                  "was the same space twice and is dropped. Off: overlaps "
                  "are drawn as found.",
        "default": True,
    },
    {
        "key": "toilet_fixtures",
        "group": "shape",
        "title": "A toilet takes the WC cubicles beside it",
        "detail": "Every small closed space beside a toilet that holds a WC, "
                  "basin or urinal block - and no other room's name - joins "
                  "the toilet, partitions included. Off: cubicles closed off "
                  "by their partitions are left out of the toilet.",
        "default": True,
    },
    {
        "key": "cut_shafts_from_rooms",
        "group": "shape",
        "title": "Shafts and lift wells are cut out of rooms",
        "detail": "Where a room's outline runs into a shaft or lift well - "
                  "divided from it by a single line, or through a lift door "
                  "opening - the shaft is cut out of the room. Off: the room "
                  "keeps the shaft inside it.",
        "default": True,
    },
    {
        "key": "fit_stated_size",
        "group": "shape",
        "title": "Fit a room to the size its label states",
        "detail": "A room that comes out more than 5% larger than its label "
                  "says (FIRE TOWER 3100X4400 traced 4975 x 4600) ran out "
                  "through an opening; when a rectangle of the stated size "
                  "stands inside the outline around the label, the room is "
                  "that rectangle. Off: the traced outline is kept.",
        "default": True,
    },

    # -- guards ------------------------------------------------------------
    {
        "key": "balcony_outer_face",
        "group": "shape",
        "title": "Balconies and refuge areas run to the outer face of their parapet",
        "detail": "The wall and column pieces along a balcony's open side, "
                  "shared with no other room and within 600 mm of the floor "
                  "plate's edge, join the balcony, as a take-off polyline "
                  "drawn to the outer face does. Off: the balcony stops at "
                  "the inner face of its parapet.",
        "default": True,
    },
    {
        "key": "x_grows_to_cell",
        "group": "shape",
        "title": "An X marking a strip of a shaft is drawn as the whole shaft",
        "detail": "When a shaft label states a size its X does not have, but "
                  "the walled compartment the X stands in does, the opening is "
                  "drawn as that whole compartment. Off (default): the X'd "
                  "strip is the opening, as a manual take-off deducts it "
                  "(BHUB FP-SH-03 1070x1250: 0.375 sq.m deducted, not 1.34).",
        "default": False,
    },
    {
        "key": "fit_keeps_wide_room",
        "group": "shape",
        "title": "A room larger than its label, with no doorway to the extra, stays whole",
        "detail": "A traced room over its stated size is cut to the stated "
                  "rectangle only when the extra lies beyond an opening. When "
                  "the extra joins the rectangle along 60% or more of a side, "
                  "and is a strip - at most a quarter of the stated area - it "
                  "is the same room (H10 Tower 2: AHU rooms with their duct "
                  "strip came out 33.0 of 36.3 sq.m). A block as large as the "
                  "room is the space next door and is cut off.",
        "default": True,
    },
    {
        "key": "door_frame_jamb",
        "group": "shape",
        "title": "A door closes its opening even when the far jamb is only a frame",
        "detail": "A door whose hinge stands at a wall's end closes the opening "
                  "at the first drawn line its leaf's width away, when no wall "
                  "face is there (H10 Tower 2: the ladies' toilet door with a "
                  "frame-outline jamb let the room leak; it came back 22 of 38 "
                  "sq.m).",
        "default": True,
    },
    {
        "key": "x_band_trim",
        "group": "shape",
        "title": "A shaft's X is cut back to its stated size at the line drawn across it",
        "detail": "An X box (or the compartment beside an X'd strip) that fits "
                  "the label's stated size on one side and runs a band of up to "
                  "350 mm past it on the other, with a line drawn across at the "
                  "stated depth, is cut back to that line, so the label names it "
                  "(H10 Tower 2: 1200x1175 shafts closed by a shutter line at "
                  "1400 came out Unknown Shaft).",
        "default": True,
    },
    {
        "key": "x_is_the_opening",
        "group": "shape",
        "title": "An X-marked opening keeps the X's extent",
        "detail": "The X is drawn corner to corner of the cut in the slab, so a "
                  "line crossing it (a slab edge, a detail line) never trims it. "
                  "Off: the never-cross-walls guard cuts X openings like any room.",
        "default": True,
    },
    {
        "key": "wall_trace",
        "group": "walls",
        "title": "Walls measured from paired faces, confirmed by the wall hatch",
        "detail": "The walls drawn on AI_WALL - in the Step 3 drawing and in "
                  "the final drawing alike - and their area and length come "
                  "from two faces a wall's thickness apart on a wall layer, each "
                  "paired with its nearest neighbour only, lines drawn twice "
                  "counted once, and confirmed by the hatch that lies in them. "
                  "Glazing is not wall. Ledges behind WCs are left out; strips "
                  "it cannot decide and hatched blocks that are not walls are "
                  "listed for review. Room tracing is not affected. Off: the "
                  "walls the room tracing found (glazing included).",
        "default": True,
    },
    {
        "key": "wall_gaps_filled",
        "group": "walls",
        "title": "Wall left between the rooms is wall",
        "detail": "After the rooms, doors and walls are drawn, a wall-hatched "
                  "piece that is still on no layer (P285: the pillar between "
                  "the two doors of each ELE SHAFT), and a thin enclosed strip "
                  "no room, door or wall took (the 450 x 150 strip beside each "
                  "FHC cabinet), go on AI_WALL. No room changes. Off: they are "
                  "left blank.",
        "default": True,
    },
    {
        "key": "slab_edge_not_boundary",
        "group": "walls",
        "title": "A slab-edge line through a shaft is never a boundary",
        "detail": "A long single line on a detail or section layer that runs "
                  "through X-marked shafts is the edge of the slab (P285: the "
                  "13.8 m line on a-details by the east balcony), not a wall: "
                  "it bounds no room and no wall, so the balcony runs to the "
                  "wall face (+4.68 sq.m). Detail layers that carry real walls "
                  "elsewhere are not affected - a wall never runs through a "
                  "shaft. Off: the line is read like any other.",
        "default": True,
    },
    {
        "key": "rcc_walls_on_column_layer",
        "group": "walls",
        "title": "Concrete walls drawn on the column layer are walls",
        "detail": "An outline on a column layer that is long and thin - about "
                  "a wall's thickness, longer than any column (Centaurus: the "
                  "stair and lift core walls on A-COLUMN, 38-47 m round and "
                  "300 mm thick) - is a concrete wall and goes on AI_WALL. A "
                  "compact one stays a column. Off: the core has no walls.",
        "default": True,
    },
    {
        "key": "wall_junctions_closed",
        "group": "walls",
        "title": "Walls meet at their corners and junctions",
        "detail": "A gap no wider than a wall between two wall pieces where "
                  "their faces line up (XA: 6-226 mm at the curved corner and "
                  "the T-junctions) is closed, and the pieces are drawn as one "
                  "outline, with no seams. A gap holding a door, a window, a "
                  "column, a shaft or a room is left open. Off: the pieces are "
                  "drawn as found.",
        "default": True,
    },
    {
        "key": "columns_in_rooms",
        "group": "shape",
        "title": "A column standing in a room's corner or edge is part of the room",
        "detail": "A column the room wraps round - touching it along at least "
                  "half its outline and touching no other room - is measured "
                  "with the room, as a take-off polyline drawn round the "
                  "room's walls is (285 FD toilets: 0.3-0.4 sq.m a column). "
                  "Off: the room steps round its columns.",
        "default": True,
    },
    {
        "key": "stair_one_outline",
        "group": "shape",
        "title": "One outline per staircase, round all its flights",
        "detail": "A staircase is the walled space holding its flights, at any "
                  "angle, with its landings: the pieces drawn for it (the "
                  "label's box below the flights, a flight's rectangle) are "
                  "joined into it (XA FIRE STAIRCASE-1 and -2). Two stairs "
                  "side by side are split at the wall between them, never one "
                  "box across both. X-marked shafts inside stay out of it. Off: "
                  "each piece stands as found.",
        "default": True,
    },
    {
        "key": "lift_well_rectangle",
        "group": "shape",
        "title": "A lift well is the rectangle between its walls",
        "detail": "A lift well runs straight across its door opening at the "
                  "wall face; the door frame, jambs and sill drawn in the "
                  "opening do not notch it (XA: the lower lifts, 6.04 -> 7.0 "
                  "sq.m). Off: the outline follows the frame lines.",
        "default": True,
    },
    {
        "key": "toilet_with_cubicles",
        "group": "shape",
        "title": "A toilet is one outline with its cubicles",
        "detail": "A toilet runs to its walls with its WC cubicles inside it: "
                  "the cubicles' partitions and doors are fittings, not walls, "
                  "so a cubicle is not a room of its own (XA FEMALE and MALE "
                  "TOILETS). Off: each cubicle is drawn as a separate toilet.",
        "default": True,
    },
    {
        "key": "leftover_strip_joins_room",
        "group": "shape",
        "title": "A small leftover strip joins the room beside it",
        "detail": "An unlabelled strip of floor next to one room only, parted "
                  "from it by a thin line rather than a wall (Centaurus: the "
                  "strip beside the shaft in the AHU ROOM), is part of that "
                  "room. Off: the strip is left on no layer.",
        "default": True,
    },
    {
        "key": "stairs_by_treads",
        "group": "labels",
        "title": "Find staircases by their treads",
        "detail": "A flight of seven or more evenly spaced tread lines with "
                  "no staircase boundary over it is traced from the middle "
                  "of the flight and drawn as STAIRCASE, even when no text "
                  "names it (BHUB 20th floor draws both staircases without a "
                  "word in them). Off: a staircase gets a boundary only from "
                  "a label standing in it.",
        "default": True,
    },
    {
        "key": "lift_not_with_doors",
        "group": "guards",
        "title": "A space with doors into it is not a lift well",
        "detail": "A lift's landing doors slide; a space labelled as a lift "
                  "with two or more hinged doors opening into it is a lobby "
                  "the label was left in (H10 Tower 1 ground floor: LIFT WELL "
                  "3100X2300 standing in the staircase's lobby, 7.55 sq.m "
                  "counted as lift shaft). Its label is refused and the "
                  "space left as common floor. Off: the label is kept.",
        "default": True,
    },
    {
        "key": "unknown_needs_walls",
        "group": "guards",
        "title": "An unrecognised text needs walls round it",
        "detail": "A text no family knows (DW, a sleeve note, TRAP DOOR) only "
                  "gets a boundary where the walls enclose it; the box four "
                  "rays guess when they do not is dropped. Off: such texts "
                  "get guessed boxes over open floor.",
        "default": True,
    },
    {
        "key": "open_side_guard",
        "group": "guards",
        "title": "A room open along the plate edge is not drawn",
        "detail": "A room whose outline runs mostly along the floor plate's "
                  "edge, where the drawing has no line of its own, is open "
                  "on that side and is reported as 'open side'. Off: it is "
                  "drawn, closed by the plate outline.",
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
        "key": "balcony_open_to_refuge",
        "group": "guards",
        "title": "A balcony open to a refuge area is part of it",
        "detail": "A balcony label whose fill runs on into a refuge area, "
                  "holding nothing but refuge names, is a sunk strip open to "
                  "the refuge: the fill is kept and the refuge names it, as "
                  "the manual take-offs count it. Off: the distrust above "
                  "draws the balcony as a box and the refuge around it is "
                  "cut (285 FD Tower 2, 5th floor: refuge 312.23 -> 237.94).",
        "default": True,
    },
    {
        "key": "rooms_clear_of_walls",
        "group": "guards",
        "title": "No room covers a wall",
        "detail": "Last, every room is cut back to the walls: a wall it runs "
                  "over is taken out, a wall standing inside it becomes a hole "
                  "(Centaurus: the 100 x 1600 wall in LADIES TOILET 01, the "
                  "partition in GENTS TOILET 02). A room that was all wall is "
                  "dropped (XA: COM ELV SH, a label in the wall between the "
                  "lifts). A balcony run to its parapet's outer face keeps it. "
                  "Off: rooms may overlap the walls.",
        "default": True,
    },
    {
        "key": "doors_need_evidence",
        "group": "guards",
        "title": "A door needs a leaf, a swing or a sliding panel",
        "detail": "An opening is drawn as a door only when the drawing shows "
                  "one there: a leaf line, a swing arc (an elliptical one too) "
                  "or a sliding arrangement. A gap at a wall junction, a run "
                  "along a wall face and a second door in the same opening are "
                  "not doors (XA: four at the male toilet entrance); doubtful "
                  "ones are marked on AI_REVIEW instead. Off: every opening "
                  "the rooms meet at is a door.",
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
        "key": "rooms_from_spaces",
        "group": "shape",
        "title": "Rooms are the enclosed spaces, named last",
        "detail": "Once the walls, the doors and the X marks are decided, "
                  "every enclosed space of the floor is traced with no text "
                  "(the open floor, wall cavities, doorway strips and pockets "
                  "left out), the staircases are the spaces holding a flight "
                  "of treads with UP / DOWN or a stair name in them, an open "
                  "lift lobby is closed at the lift banks' ends, and only then "
                  "is each space named by the texts standing inside it - "
                  "never by one outside. A space of 10 sq.m or less along a "
                  "shaft's X with a door of its own is the shaft's room "
                  "(AI_MEP) whatever is written in it; a shaft-like text "
                  "decides only such a space; a text that reads as a note or "
                  "the family library's ignore words never name. A label the "
                  "spaces did not take still traces its room from its label, "
                  "as before. Off: every room is traced outward from its "
                  "label (Phase B), the text first.",
        "default": True,
    },
    {
        "key": "walls_before_spaces",
        "group": "walls",
        "title": "Walls built in full before the spaces are traced (W2 first, W3 on the rooms)",
        "detail": "The sequence 1, 5, 2, W2, 5a, 4, 5b ... W3, 12 (2026-10-17): once the X marks "
                  "are decided and the doors found, the wall mass is built in full (the paired "
                  "faces and the wall hatches, columns and door openings cut out, never over a "
                  "shaft, a lift, a void or a loose cut-out) before any space is traced; the mass "
                  "is a wall to the room tracer and no space to the grid, so no space is ever "
                  "traced inside a wall core. The walls are built once more on the rooms "
                  "afterwards (pass 11, W3: the revalidation - the strips rooms grew over, balcony "
                  "parapets and columns in rooms taken out). Off: the walls are built once, after "
                  "the rooms.",
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
                  "AI_STAIR instead of leaving it Unknown.",
        "default": True,
    },
    {
        "key": "element_scores",
        "group": "guards",
        "title": "Score every opening and core space",
        "detail": "Every door, window and cut-out, and every shaft, lift, "
                  "staircase, void, lift lobby, service lobby and lift machine "
                  "room, is classified again from the evidence drawn round it "
                  "with a 0-100 confidence score (the element identification "
                  "engine). 50 and up is taken as it is; 20-49 is marked "
                  "for review and below 20 held as unresolved, on AI_REVIEW "
                  "and in the element list. The areas are not changed by a "
                  "score. Off: no scores, no review marks.",
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
