# Drawing Analyzer — implementation pass

Everything below was built from data the pipeline already produced, or from
the DXF the user already uploads. No new user input is required anywhere.

## How to run

```bash
pip install -r requirements.txt
python runserver.py                 # http://127.0.0.1:5555
python -m unittest discover -s tests -v
```

`FLASK_DEBUG=1` enables the debugger, and only on loopback — the server
refuses to start the debugger on any other host.

---

## 1. Blockers fixed

| Was | Now |
|---|---|
| Two competing Flask apps: `views.py` + `__init__.py` shadowed the real `app.py`, and `runserver.py` imported the dead one | `views.py` deleted; `__init__.py` lazily exports the single real app; `runserver.py` starts it. `.pyproj` startup file corrected |
| `from dxf_reader import ...` only resolved when CWD was the package folder | Package-relative imports throughout |
| `requirements.txt` listed Flask only | Flask, Werkzeug, ezdxf and openpyxl, all bounded |

## 2. Units — the correctness fix

`$INSUNITS` is now read at ingest and everything downstream works in
millimetres, so a drawing authored in metres or inches no longer produces
areas wrong by six orders of magnitude.

- New `units.py`: full $INSUNITS table, override by code or name (`"m"`,
  `"ft"`), and an explicit `assumed` flag when the header is unset.
- New `dxfio.load_document()` is the only way the code opens a DXF, so no
  path can silently re-assume millimetres.
- Geometry written back to the DXF (boundaries, labels, outline) is
  converted from millimetres into the drawing's own units.
- The UI has a unit selector on both upload steps and shows a banner
  stating what was detected, assumed or overridden.

## 3. New modules

| File | Purpose |
|---|---|
| `geometry.py` | Pure primitives: area, perimeter, run length, centroid, arc/bulge flattening, `BandIndex` spatial index. No ezdxf import, so it is directly testable |
| `units.py` | Unit detection and conversion |
| `config.py` / `config.json` | The ~25 tuned thresholds, externalised |
| `dxfio.py` | Document loading and recursive block traversal |
| `quantities.py` | Reconciliation, KPIs, deck sheet, studs, geometry health, extraction QA, revision comparison. Pure, testable |
| `extractors.py` | Linear lengths, wall lengths, block inventory, door schedule |
| `reports.py` | Excel writers for the new reports |
| `pipeline.py` | Single-parse orchestration for step 4 |
| `tests/test_core.py` | 42 tests |

## 4. Modules delivered

**Perimeter calculation** — closes the Core module. Per room and for the
floor outline, in the UI, the areas workbook and the JSON.

**Geometry cleanup** — completes the partial. Beyond the existing 70%
merge, boundaries are now checked for duplicates, fully contained
polygons, slivers and sub-threshold overlaps. Reported, never
auto-deleted.

**Deck sheet quantity** and **studs count** — floor plate less shafts,
lifts and staircases, at the 9/m² given in the brief. The deduction rule
is printed with the number so it can be challenged.

**Efficiency KPIs** — carpet area, core area, core efficiency, carpet-to-
plate ratio and the family split. Parking efficiency, cost per sqft and
FAR are deliberately absent: they need inputs the drawing does not carry.

**Area reconciliation** — floor plate versus the sum of rooms, with a
coverage verdict. The best single check on whether a run is trustworthy.

**Extraction QA report** — `annotate_dxf` was already computing a per-label
outcome and discarding it. It is now a workbook listing every label that
produced no boundary.

**Wall linework length** — by layer and by thick/thin class, using the same
lineweight threshold the ray-caster uses.

**Linear run length by layer and discipline** — the shared primitive behind
cable tray, duct and pipe extraction. Layer-to-discipline rules in
`config.json`.

**Block inventory** — every INSTANCE by block name and category. The
mechanical core of lighting, sprinkler and plumbing fixture counting.

**Door schedule** — the wall-gap width measured inside
`_find_door_closing_lines` was being thrown away after sealing openings.
It is now kept as the door's clear opening, with a block-extents fallback
and the method recorded per door.

**Revision quantity comparison** — new step 5. Two areas JSON exports in,
added/removed/changed table out.

## 5. Accuracy and performance

- **Arc and bulge flattening.** `_polyline_points` dropped the LWPOLYLINE
  bulge, so arc-cornered outlines were measured as chords and came out
  short. Bulges are now expanded at a configurable sagitta tolerance.
  `extractors` also handles ARC, CIRCLE, ELLIPSE and SPLINE lengths.
- **Nested blocks.** Text, line and diagonal collection descended exactly
  one INSERT level. It now recurses to a configurable depth (default 3)
  with composed transforms and cycle guarding.
- **Spatial index.** `_hv_boundary` scanned every wall segment for every
  label and every X-cluster. Segments are now bucketed into bands, so a
  ray only tests segments that could cross it. The result is identical —
  a test asserts it against the linear scan over 200 random rays.
- **One parse per job in step 4.** `pipeline.analyse_areas` opens the
  drawing once and drives areas, quantities and all four extractors from
  it, instead of re-reading a 40 MB file per operation.

## 6. Security and hygiene

- Download tokens validated against `^[0-9a-f]{32}$` before touching the
  filesystem, closing the path-traversal hole.
- `debug=True` removed; env-driven and refused on non-loopback hosts.
- Raw exception text no longer returned to the browser; logged instead.
- Bare `except:` clauses replaced with typed ones.
- Dead code removed: `views.py`, `_extract_insert_transform`,
  `_transform_point`, the duplicate root `family_rules.json`, and the
  discarded first `keep_set` comprehension in `/annotate`.
- `.gitignore` now actually covers `uploads/`, `outputs/`, `.vs/` and
  `__pycache__`.

## 7. Real room tracing (second pass)

The largest remaining accuracy problem is gone. `_hv_boundary` fired four
cardinal rays from a label and took the bounding box, which is only
correct for a rectangular room aligned to the axes. New `roomtrace.py`
measures the region the walls actually enclose:

1. rasterise every wall segment onto a uniform grid as a barrier;
2. flood-fill from the label, so the fill follows whatever shape the
   walls make;
3. walk the filled/unfilled frontier into closed loops;
4. simplify, then project each edge back onto the wall segment it came
   from.

That last step matters: raw grid tracing stops one cell short of every
wall and under-reports by roughly perimeter x cell size, about 2% on a
small room. Snapping edges onto the real wall lines removes the bias and
makes straight-walled rooms **exact**, however they are oriented.

Measured against hand-computed figures:

| Shape | Traced | True | Bounding box would give |
|---|---|---|---|
| 6 x 4 m rectangle | 24.000 | 24.00 | 24.00 |
| L-shape (6x6 less 2x2) | 32.000 | 32.00 | 36.00 |
| 45-degree corner cut | 22.000 | 22.00 | 24.00 |
| 1.5 x 20 m corridor | 30.000 | 30.00 | 30.00 |
| Room with a 400 mm column | 23.840 | 23.84 | 24.00 |
| Semicircular end, 50 mm cell | 30.375 | 30.283 | 40.00 |
| Semicircular end, 10 mm cell | 30.290 | 30.283 | 40.00 |

Curved runs are the only ones left with any error, and it is bounded by
the cell size rather than by the shape: 0.30% at 50 mm, 0.02% at 10 mm.

Other behaviour worth knowing:

- **Leaks are detected, not hidden.** If the fill escapes through an
  unsealed doorway or exceeds the maximum room area, the trace is
  abandoned and that label falls back to the old ray-cast box. Tracing
  can therefore only improve on the previous behaviour, never lose a
  room. The count of fallbacks is reported in the QA panel — a high
  number means door sealing is not catching everything on that drawing.
- **A label sitting on a wall pixel no longer loses its room.** The
  tracer tries the exact cell, then several nearby free cells, and keeps
  the first fill that does not run away.
- **Interior voids are deducted.** A column or duct standing inside a
  room becomes a hole in the trace and comes off the carpet area.
  Voids are written to a `ROOM_VOID` layer, deliberately *not* a
  `*_BOUNDARY` one, because step 4 treats every closed polyline on a
  `*_BOUNDARY` layer as a room — a column drawn there would have been
  counted as a 0.16 sq.m room and its area added back instead of
  subtracted. Step 4 now finds those void polygons and deducts each from
  whichever room contains it. Gross area and deducted void area appear
  as extra columns in the areas workbook, and only when a drawing
  actually has voids.
- **Merged boundaries fall back to the rectangle.** The union of two
  traced outlines is not their bounding box, and stitching them properly
  needs real polygon booleans.
- Cost is about 11 ms per room after a one-off grid build; a 60-room
  floor traces in roughly 0.6 s.

Controlled by `boundary.method` in `config.json` (`"trace"` by default,
`"raycast"` to restore the old behaviour), with `trace_cell_mm` and
`trace_simplify_cells` alongside it.

`tests/test_roomtrace.py` adds 18 tests covering shapes, holes, curves,
leak detection and multi-room tiling. 60 tests in total.

## 8. Fixes from field testing (third pass)

### The unit bug, and the three symptoms it caused

Reported: selecting **Metres** turned a 5,207.90 sq.m floor plate into
5,207,904,569.72; the 2,000 sq.m floor-plate filter stopped filtering;
and step 3 wrote back the original drawing with empty `ROOM_BOUNDARY`,
`BALCONY_BOUNDARY` layers and no polylines in them.

All three were one cause. The drawing is in millimetres, and the
selector was read as *output* units when it means *the units the file
was drawn in*. Choosing Metres multiplied every coordinate by 1,000, so:

- areas came out 1,000,000x too large — the exact ratio reported;
- every stray polyline then cleared the 2,000 sq.m threshold, so the
  floor-plate list filled with unrelated layers;
- a 5 m wall became 5,000,000 mm, past the 20 m ray-cast search radius
  and past `max_room_sqm`, so every label reported `no_walls` and
  nothing was drawn.

Verified by driving the boundary logic directly: a 20 x 12 m six-room
floor in millimetres yields six boundaries summing to exactly 240.00
sq.m by both the ray-cast and the trace path, and zero boundaries when
the same geometry is mis-read as metres.

What changed:

1. **Units are inferred from the geometry** when `$INSUNITS` is absent,
   instead of silently assuming millimetres. A building plate is a few
   metres to a few hundred metres across, which is enough to identify
   the unit; millimetres wins ties, as that is what architectural DXFs
   overwhelmingly use.
2. **A wrong choice is now refused, not reported.** Whatever unit is
   settled on is checked against the drawing's span, and an implausible
   result stops the upload with the actual numbers: *"Read as Metres,
   this drawing would be 60,000 m across, which is not a building. The
   geometry looks like Millimetres, which would make it 60.0 m across."*
   A factor-of-a-million error is not something to warn about and carry
   on from.
3. **The label was the real culprit**, so it now reads *"Units the DXF
   was drawn in"* with *"This describes the file, not the output.
   Results are always reported in sq.m and m."* next to it.
4. The same resolution and check run on the step-4 upload, so a
   corrected DXF cannot be read in the wrong unit either.

### Better diagnosis when nothing is drawn

The old failure mode was silent: you got your drawing back with empty
layers and no explanation. Step 3 now reports how many boundaries were
drawn, and when that is zero it names the likely cause — no usable wall
segments, labels finding no wall inside the search radius, or nothing
selected in step 3.

The "no floor plate found" message now shows the largest polyline areas
it did find and how wide the drawing was read as being, so a threshold
problem is distinguishable from a unit problem at a glance.

### Section 5 accepts DXF and Excel

A revision arrives in whatever form the last person saved. Each side of
the comparison now takes:

- **areas JSON** — as before;
- **areas Excel workbook** — or any sheet with a room-name and an area
  column. Headers are matched by name rather than position and the
  header row is located anywhere in the sheet, so the areas workbook
  (which has a floor-area block above the table), a reordered sheet, or
  a hand-made one all load. Total and subtotal rows are skipped and
  values like `"42.75 sq.m"` or `"1,250.00"` parse.
- **corrected DXF** — measured on the spot, picking the largest
  non-boundary closed polyline as the floor outline automatically. Unit
  and floor-layer overrides appear only when a DXF is selected.

The two sides can be different formats: comparing last month's Excel
against this month's JSON works, and is covered by a test.

19 new tests, 79 in total.

## 9. Field-test fixes (fourth pass)

Diagnosed by reading the two annotated DXFs directly rather than from the
description, which is how the first two causes were found.

### Boundaries were all rectangles because tracing had switched itself off

Every one of the 98 boundaries in the new output had exactly five
vertices - a closed rectangle. The tracer built **one grid over the whole
drawing**, and that file spans 1,224,573 units because of content parked
far from the plan, though the building is only about 72 m across. At a
50 mm cell that is 575 million cells, over the memory guard, so the
tracer returned `None` and every room silently fell back to the ray-cast
box.

Grids are now **local**: a window around each seed, widened only when the
fill reaches its edge (4 m, 9 m, 18 m, 36 m half-widths). A typical room
resolves in a 160 x 160 grid instead of 24,000 x 23,000. Segments are
clipped to the window before rasterising, so a wall crossing from far
away costs nothing.

| Shape | Traced | True | Bounding box |
|---|---|---|---|
| Triangle | 24.08 | 24.00 | 48.00 |
| 30-degree trapezoid | 58.99 | 58.87 | 87.7 |
| L-shape | 32.000 | 32.00 | 36.00 |
| 45-degree corner cut | 22.000 | 22.00 | 24.00 |
| Octagon | within 1% | - | +14% |

Two related fixes: an empty first window now widens instead of aborting
(a 40 x 25 m hall has no wall within 4 m of its centre), and vertices
that snapping brings together at a sharp corner are merged.

### A boundary round PRESS was labelled INID04

`label_score` ranked short strings *containing digits* best, so the
equipment tag INID04 scored 6 against the room name PRESS at 105. The tag
won - and because INID04 matches no family keyword the boundary also
landed in UNKNOWN_BOUNDARY. One bad comparison produced both symptoms and
explains 22 unknown boundaries against the previous run's 6.

New `labels.py` ranks on evidence that actually separates a room name
from a tag: is it a recognised family keyword, is it an equipment tag,
how tall is the text, how far from the centre. Dimensions, levels, UP/DN,
revision dates and title-block text are dropped outright rather than
ranked last.

### Substring keyword matching was misclassifying rooms

Found while checking the above. Keywords were matched by bare
containment, so **"FRESH AIR ATTENUATORS" went to the TEN family**
(at-TEN-uators) and **"8M EXPRESS WAY" to PRESS** (ex-PRESS-way).
Matching is now whole-word, with plural tolerance so the rule
ATTENUATOR still matches ATTENUATORS, and without anchoring the
punctuated ends of keywords like `ST-` and `P.LIFT`.

### The unit header was simply wrong

The drawing declares `$INSUNITS = 6` (Metres) while being drawn in
millimetres, so trusting the header inflated every area by a million;
and the previous sanity check used the drawing-wide extent, which that
file's outlying content made useless.

Detection now uses the **size distribution** of the closed polylines -
median and 95th percentile - which is immune to both:

    as mm:  median 1.9 m,  p95 19.6 m   -> both sane
    as cm:  median 19.1 m, p95 243 m    -> neither
     as m:  median 1912 m, p95 24310 m  -> neither

The header is overruled when the geometry scores better, and the note
says so. An implausible unit is only refused when the user forced it;
auto-detect never blocks an upload.

### Step 4 stops asking for the unit

It inherits the step-1 selection, and failing that whatever the engine
resolved on the step-1 upload.

### family_rules.json rebuilt

15 families and 244 keywords, grounded in the labels actually present in
the drawing. New families: Fire / Life Safety, Electrical, Landscape /
External, Amenity. Added coverage for FIRE TOWER, FACP, PA RACK, FUEL
ROOM, BATTERY, DGCP, HVAC ROOM, SUCTION PIT, PEDESTRIAN WALKWAY, GREEN
STRIP, WATER BODY, VOID SPACE, FOODCOURT, CHANGE ROOM, WAITING LOUNGE,
DROP OFF and more. Door and damper tags (FRD-3, GD1, D1, V1) are demoted
as equipment tags.

19 new tests, 108 in total.

## 10. Known limits, unchanged

- Tracing needs walls that actually close. Where door sealing misses a
  gap the fill leaks and that room falls back to a bounding box, so the
  manual CAD correction step is reduced rather than eliminated. The
  fallback count tells you how much of it is still needed.
- Holes are deducted but not represented as true polygon interiors; each
  void is a separate closed polyline on the `ROOM_VOID` layer.
- Overlapping boundaries are still merged by bounding box.
- Linear lengths are measured on linework, so anything drawn as two
  parallel faces counts twice. Centreline pairing is not implemented.
- External references are still not resolved.
- The end-to-end DXF path could not be executed in the environment these
  changes were written in (no ezdxf available offline). The pure geometry,
  units and derivation layers are covered by tests; the ezdxf-facing code
  is reviewed and import-checked but wants a run against one of your real
  drawings before it is trusted.

## 11. Boundaries follow real walls (fifth pass)

Reported: boundaries drawn from room labels came out wrong across the
board. The men's toilet on the typical-floor plate was drawn at 35.24
sq.m, stopping at the WC cubicle fronts and cutting off the urinal bay
and the right third of the room; it measures 60.71.

### What was wrong

- **Every line was a wall.** Any line of 300 mm or more on a layer not
  in `never_walls` stopped the fill: WC cubicle partitions, door leaves,
  urinal screens, counter edges, centre lines.
- **The gap closer invented walls.** `_find_collinear_wall_gaps` joined
  any two line ends pointing at each other 300-2000 mm apart. Over all
  lines that made 3,684 virtual walls on the test plate (6,435 on A-107);
  three ran straight through the open floor of the men's toilet.
- **Every opening under 900 mm was sealed**, deliberately, including
  each cubicle entrance.
- **Doors were closed corner to corner**, from door INSERTs in modelspace
  only - none at all on A-107, whose doors sit inside XREF blocks.
- **Blocks were placed wrongly.** A block stored with extrusion
  (0, 0, -1) landed 300-850 m away; a rotated block inside a mirrored one
  was off by up to 1.4 m; base points were ignored. Layer-0 geometry did
  not take its block's layer, so furniture blocks escaped the furniture
  exclusion.
- **Labels were claimed by bounding box.** A traced L-shaped room claimed
  the label of the room in its corner, which was then never drawn.

### What changed

| Area | Now |
|---|---|
| Walls | Two parallel lines 80-400 mm apart that run alongside each other, on any layer, or any line on a structure layer (WALL, RCC, COLUMN, UPSTAND, CUTOUT...). Two similar lines a few mm apart are a panel (door leaf, partition board) and ignored. X-mark diagonals never count |
| Doors and windows | An opening is closed straight across both wall faces where a door or window stands in it: an arc hinge, or a straight leaf / threshold / glazing line, at one end of the opening and as wide as it |
| Openings with no door | Stay open, so a urinal bay or alcove belongs to its room. The "Close openings that have no door" rule restores closing, over wall faces only; off by default |
| Opening seal | 900 mm to 100 mm (drafting gaps only) |
| Block placement | `Insert.matrix44()`, composed exactly; OCS honoured for polylines, bulges and text; layer 0 inherits |
| Labels | Claimed by the traced outline; a room holding several labels is titled by the best; two boundaries of one traced room keep the outline; a label is always drawn inside its own boundary so Step 4 can name it |
| Overspill guard | A trace more than 3x its four-ray box is only rejected when it also holds another family's room name. Size alone rejected a 59 sq.m ladies' toilet whose box from its label is 7.8 |
| `never_walls` | HVAC COMMENT removed: on the test plate it carries both women's toilets' outer walls |

### Measured

| Drawing | Before | After |
|---|---|---|
| Typical-floor plate: men's toilets | 35.24 / 35.23 | 60.71 / 60.58 |
| Typical-floor plate: women's toilets | 51.75 box / none | 49.90 / 49.91 |
| Typical-floor plate: traced / fallback / no walls | 34 / 6 / 4 | 37 / 3 / 3 |
| Typical-floor plate: run time | 61 s | 56 s |
| A-107: gents' and ladies' toilets | 3.0 - 9.4 | 59.4 - 72.9 |
| A-107: passenger lift wells | 0.4 - 1.1 | 6.76 |
| A-107: overlapping boundaries | 1 | 0 |

`tests/test_walls.py` adds 19 tests: block placement, wall and panel
detection, door bridges, and three end-to-end drawings run through
`annotate_dxf` and `compute_room_areas`, all three of which fail on the
previous code. 188 tests in total.

### Known limits

- Open-ended spaces - a lift lobby running between two banks of lifts
  into the office floor - have no enclosure once doorless openings stay
  open. Their four-ray box is past the leaked-box cap, so they get no
  boundary and are listed as no_walls. Previously they got a rectangle
  (125 sq.m and 8 sq.m on A-107) that was not right either.
- A door or window drawn with no arc and no straight line of its own
  width (a sliding door as a single thin block, say) does not close its
  opening.
- A wall drawn as a single line on a layer that is not a structure
  layer is not a wall.

## 12. Boundaries without spikes, ramps kept out (sixth pass)

Reported with screenshots from P285 (men's toilet, lobby) and A-106 (AHU
rooms). Diagnosed by rebuilding the wall set from the two annotated DXFs
with a note of why each line counted as a wall.

### The screenshots came from an outdated server

The browser was talking to a second copy of the app, in a different
folder, whose `dxf_reader.py` predates the fifth pass. Run on the same
drawing, the fifth-pass code already drew the men's toilet at 61.6 sq.m
(the screenshot showed 35.2). What was still wrong in the current code:

| Cause (confirmed on the drawings) | Effect |
|---|---|
| Column centre lines (`CENTRE`) paired with base-plate lines as a wall | Slits into the toilet: a spur and a self-crossing outline |
| Open door leaves (`…$DOOR`) paired with the ramp edge as a wall | A spike up each jamb, and the leaves then served as jambs for a door closure across the middle of the ramp |
| A ramp standing in a doorway is a single-line rectangle | Rooms ran into their ramps |
| A crosshair on a column layer (`IEC-STEEL-COL`) is always a wall | A spike beside the column |
| The contour walk took either edge at a pinch point | Outline and hole spliced into one self-touching loop |

### What changed

- **Door lines are never walls.** Lines on a door layer or in a door block
  are opening evidence only. `OUTDOOR` is not a door. Windows are left
  alone: their blocks often carry the real wall faces.
- **Centre marks are never walls.** Two perpendicular lines of comparable
  length crossing in the middle half of both - the "+" through a column -
  are dropped, except on layers named WALL. `CENTRE`/`CENTER` join
  `never_walls`.
- **Ramps and thresholds** (`threshold_ramps` rule, on): a line as wide
  as a door opening, 300-1500 mm off it, with a side running back to the
  wall, not structure and not a wall face, with no wall face ending in
  the strip - the room is closed along it, straight and square. On A-106
  it finds all 20 labelled AHU ramps.
- **Doorless openings** (`close_gaps_on_leak` rule, on): a room is traced
  with doorless openings open, so an alcove or urinal bay stays in it. If
  the fill leaks or reaches another family's room name, it is traced
  again with every doorless opening in a real wall closed straight
  across.
- **Contours are always valid.** At a pinch point the walk crosses to the
  other cell, so loops are simple. The outline is the largest-area loop.
  Snapping that crosses itself is untangled, and if the result is still
  not simple, or its area moved more than snapping can account for, the
  grid outline is kept.
- **Speed.** Wall pairing now bins by position along the line as well
  (41.7 s to 6.3 s on A-106, identical faces, caps and panels). A leaking
  room no longer re-floods the same region from each nearby seed, and
  wall thickening uses numpy (a dependency of ezdxf; pure Python is the
  fallback).

15 new tests, 203 in total. Each new end-to-end test fails with its rule
switched off.

## 13. Floor-wise results, and fixture labels are not room names

- **Several floors on one sheet.** Step 4 reads every closed polyline of
  floor-plate size on the chosen layer as a floor of its own, assigns each
  room to the floor it stands in, and reports areas, families and the
  Quantities & Checks floor by floor (page, areas workbook, quantities
  workbook with one sheet per floor). Floor outlines carry the Step 2
  floor name as XDATA so the names survive the round trip through CAD.
- **Fixture labels.** A-109's second tower has about 150 labels inside its
  sanitary fixture blocks (TB, EWC, WB, HF, TPH on A-Fixtures); the first
  tower has none. Read as room names they were traced first, took the
  toilets apart into TB/EWC boxes, and left GENTS/LADIES TOILET rejected
  for engulfing them. Those codes are now equipment tags, and any text on
  a fixture layer (`fixture_layers` in config.json) is never a room name.
  All eight A-106..A-114 drawings checked.

## 14. Multi-floor sheets read in the right unit; each plate listed once

T1_TYPICAL FLOOR PLANS (ten plates on one sheet) came out with about 23
boundaries per plate against 94 for the same floor drawn alone.

- **Units.** The header declares millimetres, correctly. The sheet is full
  of small hatch squares, so its median element is 0.20 m in millimetres -
  just under the plausible band - and centimetres scored better and
  overruled the header. Every wall was read ten times too thick and never
  counted as a wall. The geometry now overrules the header only when the
  header is impossible (scores nothing), as with the file declaring metres
  while drawn in millimetres; a plausible header is kept.
- **Duplicate plates.** The same slab edge was drawn twice on A-FLOOR PROJ
  and again on A-Glass, so Step 2 listed 13 plates for 10 and a plate
  picked twice had every room drawn twice. Outlines within 3% in area with
  coinciding extents are now one plate, kept on the floor layer.

Result: 80-93 boundaries on each of the eight office plates; the roof and
terrace plates carry fewer rooms because they have fewer.

## 15. Area Statement, floor by floor and tower by tower

"Download Area Statement" (Step 4) now reads each plate's title from the
sheet - the largest floor-title text just below the plate - and lists the
tower floor by floor: "TYPICAL FLOOR PLAN-(2nd,4th,6th & 10th)" becomes
four rows with that plate's areas, refuge floors are marked, and ordinals,
words (SECOND), ranges (5TH TO 9TH), Ground, Basement and Terrace are all
understood (levels.py). Towers come from a TOWER heading on the sheet,
else the file name; two plates of one floor with nothing to tell them apart
are two towers. The workbook opens with the project name typed beside the
button, then per tower "Area Details" with a row per floor, a total and a
summary; columns no floor uses are left out and the formulas follow.

Also fixed on the way: two plates carrying the same Step 2 name reported
each other's rooms (names are now made unique), and the statement rules
now file STAIR PR / STAIR PREZ / STAIRCASE PRE.SHAFT / PRESS as staircase
pressure shafts, LIFT PRE as a lift lobby prez shaft, and RWP as PHE.

## 16. Floor plate area range in Step 1; Step 3 table follows the boundary type

- **Floor plate area range.** Step 1 asks for a minimum and maximum floor
  plate area (sq.m); both are required and checked on the page and the
  server. Step 2 lists only closed polylines within that range, and the
  messages quote it. The fixed 2,000 sq.m threshold (min_floor_area_sqm)
  remains only as the default for code paths with no range, and Step 4 now
  always treats the outlines the program drew (tagged with the floor name)
  as floors whatever their size.
- **Boundary type filters the Step 3 table.** Choosing a family in the
  Boundary type dropdown lists only that family's texts (by each row's
  current family, including changes made in its Family dropdown), with a
  "Showing N of M" note; Check all / Uncheck all act on the rows shown.

---

## 2026-10-10 — checked against the manual area statements (285 FD Tower 1, BHUB Tower 1)

Every floor drawing of both towers was run through the app's own pipeline
(plates → texts → boundaries → areas → statement) and compared, column by
column, with `Area_Statement_Sales.xlsx` and with the take-off polylines
inside the first-floor drawings. Result: on every 285 FD floor, total
shafts are within 0.12 sq.m of the sheet and plinth within 0.2 sq.m. On
BHUB, total shafts are within 0.05 sq.m on floors 4, 20, 24 and 28 (floor
1: 0.74 over, floor 19: 1.18 over), and plinth within 2.6 sq.m except the
4th floor (see below).

**Area statement (area_statement.py, rebuilt)**
- Canonical categories (common areas, vertical connectivity, every shaft
  type, info columns) with per-project templates (`config.json →
  area_statement.templates`: default / 285 / bhub) and the derived figures
  of the manual sheet: total shafts, Net BUA = gross − shafts, plinth =
  Net BUA − stairs − lifts, saleable = plinth / 0.70, sq.ft.
- Only slab openings are deducted: an X-marked opening under its name, or
  as an unnamed cut-out. A walled shaft room with X'd openings in or
  against it is not deducted (its openings are); one with none drawn is.
- An unnamed opening along a single named shaft compartment, or along an
  electrical room, is filed under that shaft.
- Not deducted, listed in info columns: cut-outs in walls, walled shaft
  rooms, corridors, and cut-outs marked ABOVE / AT CEILING.
- Names: FHC slots under FHR, LOBBY PR., coded shafts EL-SH / CO-SH /
  TR-SH / FP-SH / ELV-SH; a hose cabinet (FHC-3) traced as a room is not
  a shaft.

**Reading the drawing (dxfio.py, units.py)**
- A plan drawn twice a small shift apart (BHUB 4th floor: everything
  repeated 0.9, 1.3 mm off) is read once. That floor's staircases, a fire
  tower and part of the toilets were missing.
- Units are inferred from the largest elements when the header and the
  median span disagree (285 first floor was read as inches).

**Boundaries (dxf_reader.py, rules.py)**
- An X-marked opening keeps the X's extent: a line crossing it no longer
  trims it (rule `x_is_the_opening`), and a wall line inside it never
  shrinks it.
- An X in a wall line with a shaft label beside it (FHR, PHE SHAFT) is
  that shaft, not a wall cut-out.
- A shaft label stating an X's exact size names it from up to 6 m away
  (shaft lists written inside the core; one side exact for an X'd strip).
- An X marking a strip of a shaft stays the strip (rule `x_grows_to_cell`,
  off by default; on = the previous behaviour).
- Staircases found from their treads when the label is missing
  (`stairs_by_treads`); balconies and refuge areas run to the outer face
  of the parapet (`balcony_outer_face`); a column a room wraps round is
  measured with it (`columns_in_rooms`).
- Size check rejects a leak that holds another room's label; cut-out-layer
  outlines round stairs are not walls.

**Known differences (drawings or sheet, not engine)**
- 285 lift lobby: the sheet has 47.38 on odd and 94.98 on even floors;
  every plan draws both lobbies (94.98).
- 285 refuge floors: the drawings label the STP shaft "STAIR PR", so it
  is counted as stair pressurisation (the total is unchanged). A sunken
  balcony separated only by a line, not a wall, is counted in the refuge
  area.
- 285 floor 11: "Proposed toilet location" spaces are counted as toilets;
  the sheet has a 99.64 machine room instead.
- 285 toilets about 1.5% low: the take-off runs over steel columns drawn as
  lines (IEC-STEEL-COL), which the room trace steps round.
- BHUB floors 18–29 on the sheet repeat the lower-zone figures (lift lobby
  78.28, balcony 71.89); the upper-zone plans draw one lobby (40.76).
- BHUB 4th floor: the drawing's gross polyline is 2480.07; the sheet uses
  2471.10.

New tests: `tests/test_manual_statement_fixes.py`. Suite: 417 passed.

---

## 2026-10-10 (later) — four more towers: H10 Towers 1, 2, 3 and 285 FD Tower 2

The same check, run on the H10 drawings (Tower 1 R4, Tower 2 R5 + terrace,
Tower 3 R3) and 285 FD Tower 2 (ground/amenity R2, 1st to terrace R5),
against their sheets in `Area_Statement_Sales.xlsx`.

**Gross to the glass (facade.py, new)**
- When a floor outline is drawn on a slab layer (FLOOR PROJ, SLAB EDGE),
  the gross is taken to the outer face of the facade: the slab is
  rasterised with every facade line as a barrier and filled from outside;
  what the fill reaches inside the slab is outside the glass. 285 FD
  Tower 2 typical floor: 5,207.90 slab → 5,079.36 (sheet 5,078.98).
  Checked on 285 FD Tower 1, where take-off outlines exist (within 0.3
  sq.m). A fill that leaks into the building gives no answer and the slab
  stays the gross. Settings: `config.json → facade`; on/off:
  `area_statement.gross_to_facade`.

**Reading the drawing (dxfio.py, dxf_reader.py)**
- Drawings ezdxf cannot load as written (H10 Tower 2) are read in recover
  mode instead of failing.
- Floor outlines drawn inside blocks are found (H10).
- Staircases named from their treads are capped at 120 sq.m, and fixture
  toilets at 150 sq.m (a leak through the core no longer becomes a
  staircase or a toilet).
- Section / cut lines are never walls.

**Labels and floor names (labels.py, levels.py)**
- Stair specs (W-1500 T-300 R-150), lift-vendor notes ("lift openings to
  be closed") and detail pointers ("TOILET DETAILS", "REFER DRG") are not
  room names.
- A size written straight after a full stop ("LOBBY PRESS.1200X1000") is
  read; before, the room had no size to check and leaked to 130 sq.m.
- "15THFLOOR", tower / wing numbers in titles, and a copy number such as
  "TERRACE FLOOR PLAN (2)" no longer become floor numbers.

**Known differences (drawings or sheet, not engine)**
- H10: the sheet's gross is slab + an elevation area that is not drawn;
  the Tower 3 sheet has no elevation column (gross − slab is used).
- H10 refuge: Tower 1 never deducts it, Tower 2 only on the 13th floor,
  Tower 3 on its refuge floors. The drawings mark refuge on the 12th, the
  Tower 1 and 3 sheets on the 13th.
- H10 ground floor: the sheet deducts the entrance lobby (266) and fire
  control room (19.6) from plinth.
- H10 Tower 1: AHU room 24.88 (the drawing's own "5000X4975") against 27.08
  on the sheet; fire tower 14.61 ("3100x4750") against 21.85 service lobby.
- H10 Tower 2 floors 5 and 11 have no slab outline, only the elevation
  outline, so their gross cannot be taken from the drawing.
- 285 FD Tower 1 and Tower 2 sheets count the same AHU pipe shafts
  differently (7.48 against 1.82 sq.m per floor); our figure follows the
  drawing (7.48), so Tower 2 plinth reads about 5.7 sq.m under its sheet.
- 285 FD Tower 2 first floor: the glass stands at the slab edge on part of
  the plan, so the gross stays 5,155.

New tests: `tests/test_new_sites_fixes.py`. Suite: 426 passed.

---

## 2026-10-11 — a different architect (XA tower, 23 drawings in metres) and the final re-check

Every tower was run again on this code: 285 FD Tower 1 and 2, BHUB Tower 1,
H10 Towers 1-3, and the XA tower (no manual sheet; checked against the
sizes and areas written on its own labels).

**Labels (labels.py)**
- Sizes in metres are read, a unit on either number: "2.6M X 2.9M",
  "3.3MX7.4M", "2.2Mx1.08M". A bare "2X3" is still not a size.
- A lift group tag standing in a lift ("LOW ZONE (A)") is not a room name; it
  was cutting a triangle out of every lift.
- A stated area is read: "REFUGE AREA 151.05 SQ. MTR.", "843.61 SQ.MTS",
  "12.5 SQM", sq.ft converted.

**Floor names (levels.py)**
- Numbers listed before the floor word: "1,3,5,...,19 TYPICAL FLOORS",
  "30,32,34 FLOORS(MID ZONE)", "29 SERVICE FLOOR", "41 FLOOR-AMENITY";
  "REFUGE 23,27" (refuge floors); "TYPICALFLOORS".
- "GROUND FLOOR MEZANINE" is the mezzanine, one floor; "58 FLOOR
  (TERRACE)" is the 58th floor, not the 58th and the terrace.
- A date in a file name is not a floor ("PLAN-_06_05_2022_R2" gave 285 FD
  Tower 2's ground floor a 6th floor too); underscores read as spaces.

**Boundaries (dxf_reader.py)**
- Treads on A-FLOR-STRS are not walls (staircase traced whole, 22.4 sq.m
  against the architect's 23.4, instead of pieces of 0.4-10.5).
- A label's stated area is checked: off by more than 6%, the trace is
  tried with doorways shut, with plain lines as walls, from points round
  the text, and with thin strips cut off; the closest within 6% is kept.
- A shaft name with no size never traces more than 30 sq.m or a strip
  over 6 m long and six times longer than wide (PHE SH ran 14.5 m along
  a slanted facade); an equipment / services label never more than 150
  sq.m (FIRE PANEL filled 915 sq.m of an open service floor). Doorways
  are shut first; if that fails the label gets no boundary.
- A shaft name with no size never names a room bigger than a shaft
  (PHE SH titled the 67 sq.m VRF yard it stood in).
- Two traced outlines merge only when the outlines overlap, not just
  their boxes (angled rooms on a slanted facade).
- A lift well whose outline spans exactly its stated size but came out
  notched (door jambs, car guides) is that rectangle, unless another room
  stands in the notch (XA: 6.0 -> 7.54 sq.m). No lift on the other towers
  is affected.

**Area statement (area_statement.py)**
- When a label states its area and the traced outline differs by more
  than 3%, the statement carries the stated area and the room note shows
  both ("label states 151.05 sq.m; traced 213.08"). H10 Tower 2 refuge
  302.10 per refuge floor, as the sheet; BHUB refuge 150.52 against 150.54.
  Setting: `area_statement.use_stated_area`, `stated_area_tolerance`.
- Templates can take more columns off the plinth (`plinth_less`);
  template `refuge_off_plinth` for H10 Tower 3, whose sheet deducts refuge.
- XA shaft names: LLP SH (lobby pressurisation), STR PRE (stair
  pressurisation), ELE SH / CA-ELEC SH (electrical shaft). A MACHINE ROOM
  over a lift X is a machine room, not an unnamed opening.

**Step 2 default plate (dxf_reader.py, static/main.js)**
- The gross outline round its own floor's inner outlines is ticked by
  default again. BHUB 4th floor: the 2,480.07 gross polyline enclosed the
  2,301 wall outline and a 2,016 DEFPOINTS copy, so it was taken for a
  sheet border and the DEFPOINTS copy was ticked - the statement's gross
  came out 2,016. An outline now "encloses" another floor only when that
  one is under 60% of its area; a DEFPOINTS outline is never ticked.

New tests in `tests/test_new_sites_fixes.py`. Suite: 438 passed.

---

## 2026-10-11 (later) — wall logic, confidence score, and the screens for both

**Walls (walltrace.py, new; rule `wall_trace`; config `wall_trace`)**
The wall-tracing logic from the separate analysis (WALL_TRACING_LOGIC.md)
now decides what is wall. A wall is two faces a wall's thickness apart on
a wall layer (last part of an xref name only; glazing is not wall), each
face paired with its nearest neighbour only, lines drawn twice counted
once, strips swallowed by the other direction dropped, and confirmed by
the hatch found inside the strips (no pattern names). Back-to-back strips:
hatched = wall; unhatched between two hatched walls = wall; beside a WC /
urinal = ledge, left out; otherwise = review; nothing hatched and closed
at both ends = empty gap. Corners and T's are filled. Hatched blocks left
over are a shaft (SHAFT label within 1.5 m), a column (near-square up to
900 mm) or review.
- Same results as the reference on the two shared drawings: BHUB 1st
  floor 46.38 sq.m / 229.1 m (reference 46.31 / 228.3), 99.1% of the wall
  hatch traced; XA typical 68.3 / 318.2 (70.1 / 326.1). Before: 152 and
  121 sq.m (glazing and lines drawn twice were counted).
- AI_WALL_* and the wall area / length in the QA come from it. Room
  tracing keeps its own walls, and balconies still grow to the parapet
  over them: no room area or statement figure changes.
- Excel QA, Walls sheet: how the walls were decided, % of the wall hatch
  traced, ledges / gaps left out, and the pieces to review with
  coordinates.

**Confidence (confidence.py, new; config `confidence`)**
Every boundary gets a score from the evidence: stated size or area
matched or missed, traced along walls or only a box, X-marked, a known
family, titled by a tag, other rooms' labels inside, grown or cut back.
High >= 0.80, Medium >= 0.60, Low below. Checked against the take-off
polylines in the drawings (285 FD T1 1st floor, BHUB T1 1st and 4th): High
rooms 100% within 5% of the take-off, Medium 88%. Stored on each boundary
in the output DXF (XDATA), read back in Step 6, and listed in the area
statement workbook (Room allocation: Confidence, Score, Why).

**Screens**
- Step 6, Room areas: a Confidence column (High / Medium / Low and the
  score; hover for the reasons) and All / High / Medium / Low filters.
- Step 3, Extraction QA: confidence counts, wall area, wall run, % of the
  wall hatch traced, ledges left out, wall pieces to review.

New tests: `tests/test_walltrace.py`, `tests/test_confidence.py`. Suite: 448 passed.

## 2026-10-12 — one workflow: Step by step or Fast track, and Step 6 Final outputs

The agreed workflow redesign, built into the app. No screen or control is
removed; the order is clearer and the downloads live in one place.

**Step 2 — "How should it run?"** (new card, choice remembered in this browser)
- *Step by step*: as before. Proceed opens Step 3; you move through 3, 4, 5.
- *Fast track*: Proceed opens Step 6 at once. Steps 3-5 run one after the
  other with the system defaults and no confirmations (an outline that
  encloses other plates stops it with a message instead of asking). Steps
  3-5 fill in as it goes and show *running* / *ready* in the step menu;
  any of them can be opened to review.

**Step 5** — "Generate final drawing" is now **Generate final outputs ›**: it
writes the final drawing and opens Step 6, which builds the areas and area
statement from it straight away.

**Step 6 — Final outputs** (was "Calculate areas")
- Progress: the seven stages (extract, boundaries, geometry, compare, final
  drawing, areas, area statement) with done / running / waiting / stopped.
- Download cards that unlock as each output is ready: Final drawing (DXF),
  Area statement (Excel + View), Areas (Excel, JSON), Quantities & checks
  (Excel, JSON), QA & comparison, Source drawings (text-based and geometry
  DXF), and a BuildNow placeholder for later.
- Results: the same Room areas / Area statement / By family / Drawing /
  Quantities tabs, with the source they were measured from.
- "Recalculate areas from another source": the old Step 3 / Step 4 / corrected
  DXF options, unchanged, folded away.
- Stop ends the background run whatever step it is on; Restart carries it on
  from the stage it stopped at (finished work is kept).
- A change made in Steps 3-4 after the outputs were built shows "out of
  date" with **Rebuild final outputs** (compare, final drawing, areas).

**Step menu** — the setup screens sit under a *Tools* heading: Drawing
layers, Family rules, Built-in rules, Core modules.

**Final drawing keeps what Step 3 knew about each room** (app.py
`geoengine/final`). The final drawing used to lose the XDATA Step 3 puts on
every boundary, so (a) Step 6 showed no confidence for it, and (b) the area
step could not tell a shaft traced from its walls (a room, "SHAFT ROOMS
walled, not deducted") from an X-marked opening, and deducted both by name.
Each room taken from the text side now carries its score and source tag
over; a room taken from the geometry gets a score from its own evidence and
no source tag (as before). BHUB T1 1st floor, final drawing route: 81 of 81
rooms scored (was 0); plinth 2,286.34 vs 2,288.25 sq.m from the Step 3
output (was 2,285.25) - the rest is shafts Step 5 takes from the geometry by
default.

Files: `templates/index.html`, `static/flow.js` (new), `static/main.js`,
`static/geometry.js`, `static/geoai.js`, `static/layout.js`,
`static/stepctl.js`, `static/styles.css`, `app.py`.
Checked in the browser: Fast track end to end (BHUB T1 1st floor, XA 22nd
floor), Step by step to Step 6, Stop / Restart mid-run, out of date /
Rebuild; every download link returns its file; no console errors. Suite: 448 passed.

## 2026-10-12 (later) — the H10 Tower 2 manual audit

Every point of the audit (lift lobby, toilets, AHU rooms, unwanted shaft
boundary, labels far from their shafts) traced to its cause and fixed. All
from the drawing as given; nothing new is asked of the user.

**Floor plates** — a slab edge drawn as loose LINEs inside a block is now
joined into a plate like one in model space (`reconstruct_floor_outlines`
reads blocks). H10 T2 5th floor: 4,905.81 sq.m plate found (was only the
4,354.65 inner outline). The 11th floor has no slab-edge line in the drawing
at all; it still takes the inner outline - fix the drawing or pick the plate.

**Shaft labels (point 7)** — rule `x_band_trim`: an X box, or the cell beside
an X'd strip, that fits the label's stated size on one side and runs a band
(<= 350 mm) past it on the other, with a line drawn across at the stated
depth, is cut back to that line and named. H10 T2 17th floor: 12 more shafts
named, Unknown Shaft 29 -> 17; the two full 1200x1175 shafts 1.41/1.38 sq.m
(were 1.64). New names: EL-ELE (electrical shaft), PL-SH (PHE), "SHAFT FOR
TOILET" (toilet exhaust - it was filed under toilets).

**Trap door (point 1)** — rule `trap_door_in_room`: an X-marked box labelled
TRAP DOOR is a hatch in the floor - not an opening, its outline not a wall.
17th floor lift lobby 46.20 sq.m (was 35.56; manual 47.39).

**Toilets (points 2-4)**
- rule `door_frame_jamb`: a door closes its opening when the far jamb is only
  a frame or hatch outline (at the leaf's width, three rays a hair apart).
  5th floor L. TOILET 4300X8750: a full trace (was a four-ray box cut by a
  wall stub, 22.18 sq.m); 17th floor L. TOILET 4300X7100 no longer runs out
  through door D1 into the passage, nor into the PHE shaft's door pocket.
- A shaft whose X marks only a strip (or an L of strips) keeps its whole
  cell as a wall to the rooms round it - the toilet no longer takes the
  open half of PL-SH-03.
- Outline clean-up: a notch <= 130 mm wide and 8x as deep (a partition stub
  the outline ran in and out along) is filled; a protrusion <= 210 mm wide
  and 3x as long (a slot up the thickness of the next room's wall) is cut.
- A four-ray box is scored "a box, not a closed trace" in the confidence
  even after the guardrail reshapes it.

**AHU rooms (point 5)**
- `M-HVAC` layers are never walls (fan casings, chilled-water pipes and their
  supports: parallel pipe pairs passed the wall-thickness test).
- rule `fit_keeps_wide_room`: a room over its stated size is cut to the
  stated rectangle only when the extra lies beyond an opening; joined along
  60%+ of a side it is the same room. AHU per floor 138.4 sq.m (was 130.2;
  manual 136.5).

**Terrace: FP-SH-01 and the machine rooms (point 6)** — the unwanted
0.96 sq.m box beside FP-SH-01's X strip is gone: the label goes to its X,
and the empty space beside it is no shaft. "M. ROOM" is a machine room
(family and statement; it was drawn as TRAP DOOR 11.01). And a room wider
than its label is kept whole (`fit_keeps_wide_room`, above) only when the
extra is a strip - at most a quarter of the stated area
(`fit_wide_max_extra`, 0.25; the AHU duct strips are 9-16 %): the terrace's
M. ROOM 3000X4150 no longer takes the open area beside it, 12.45 sq.m (20.0
with the rule as first written), nor MACHINE ROOM 4000 X 11550 both its
wings, 46.2 (93.8).

**Found in the full run of all seven sites, and corrected**
- The join test of `fit_keeps_wide_room` measured a piece that hung on to
  the stated rectangle by a hairline along its edge as joined to all four
  sides. Hairlines go first now: BHUB's FIRE TOWER 3100X4400 is 13.64 again
  (it had taken its 1.2 sq.m passage, 15.13), and H10 Tower 1's AHU
  5000X4975, whose 1.4 x 1.6 m corner was kept the same way, is 24.88 as
  before the audit (the manual has 27.08: the corner joins the room along
  a third of one side, which the rule reads as beyond an opening).
- A tag written with a space on a tag layer (BHUB "FMD 4" on A3-Tag, beside
  FMD-4) is a tag: with more doors now closed at their frames it had titled
  a 39 sq.m room of its own.

Files: `dxf_reader.py`, `area_statement.py`, `rules.py`, `config.json`,
`family_rules.json`; tests `tests/test_h10t2_audit.py` (18).

## 2026-10-12 (later) — speed: the same rooms, two to seven times faster

Generate had slowed to minutes a drawing (BHUB 19th floor 232 s, 285 FD 1st
floor 282 s). Every change below gives exactly what the slow way gave: on
the twelve test drawings (BHUB 1st, 4th, 19th, 20/24/28; 285 FD all seven;
XA 41st) every room, area, statement row, text status and count is the same
as before, and so are the H10 Tower 2 5th and 17th floor passes.

| drawing | before | after |
|---|---|---|
| BHUB first floor | 89 s | 30 s |
| BHUB 4th floor (refuge) | 132 s | 30 s |
| BHUB 19th floor | 232 s | 31 s |
| BHUB 20, 24 & 28 | 229 s | 30 s |
| 285 FD first floor | 282 s | 101 s |
| 285 FD 2-9 floors | 285 s | 85 s |
| 285 FD 11th floor | 255 s | 80 s |
| 285 FD refuge 5 & 8 | 101 s | 70 s |
| XA 41st floor | 130 s | 59 s |
| H10 T2 5th floor (pass only) | 210 s | 42 s |
| H10 T2 17th floor (pass only) | 215 s | 22 s |

(whole run of the drawing: floors, texts, Generate, areas, statement.)

**Room tracing**
- The walls are indexed and rasterised once per floor. A leak re-trace adds
  its few gap lines on top of that (`RoomTracer.extended`) instead of
  building a new tracer over every wall; a window the gap lines do not reach
  takes the answer already worked out there.
- The free space of a window is labelled once (scipy) and each seed's room
  read off it, not flood-filled cell by cell; straight wall lines are drawn
  into the raster a run at a time.

**Walls and doors**
- Door frames (`door_frame_jamb`): the three rays test every line near them
  in one go (`_LineGrid.ray_min`).
- Break marks: each stroke end's neighbours are worked out once per point,
  not by trying every stroke from every point.
- Slits: a line can only be the end's own line when its box holds it; the
  0.5 mm buffer test runs only then.
- Doorway twins: only gap lines whose box comes within a wall's thickness are
  compared (was every gap line with every other, 15 s on 285 FD).
- The wall hatches are turned into polygons and joined once per floor;
  fixture-layer names are judged once each; the second wall-mass pass (for
  balconies) runs only on a floor that has a balcony or refuge area.

**Reading the drawing**
- A block placed hundreds of times is looked up once per walk.
- A step that needs the drawing while the step before is still keeping its
  parsed copy waits for that copy instead of parsing the file again.
- The drawing Generate writes is kept the same way, so the area step, the
  preview and the final drawing do not parse it again. The kept drawings are
  known by their bytes, so the area step's copy of the file is found too.

**Floor-pass cache** — the key is a hash of what the pass reads (its code
files, the family rules, the config sections it uses), not of every file's
timestamp; worked out once per floor, and fast on the 200,000 segment rows.

Files: `roomtrace.py`, `dxf_reader.py`, `dxfio.py`; tests
`tests/test_speedups.py` (13: each one against the old way of working it out).

## 2026-10-13 — one wall layer, and hanging walls

### Walls are one layer: AI_WALL

A wall is a wall: the output drawings no longer split walls into
AI_WALL_EXTERNAL / AI_WALL_CORE / AI_WALL_INTERNAL. Every wall is drawn on
**AI_WALL** in one colour (`wall_output.color`, ACI 30 orange - the core
walls were ACI 250, near black and invisible on a dark background, H10
Tower 1). Touching pieces are drawn as one outline. The Walls sheet of the
QA workbook has one row.

- No area changes: the classes only ever chose the layer, the colour and the
  Walls-sheet rows. Internally the runs are still joined into pieces in the
  same three groups (along the plate's edge, structure, the rest), so a
  balcony still grows over the parapet piece along its open side - one
  piece of every wall touching it would not let it (BHUB 1st Floor balcony
  71.90 sq.m would have become 63.45).
- Drawings written before keep reading: AI_WALL_EXTERNAL / _CORE /
  _INTERNAL stay reserved layers (walls, never rooms), and a geometry result
  saved with them is drawn on AI_WALL.
- Stats: `walls_count`, `walls_length_m`, `walls_area_sqm` (were
  `walls_external_*`, `walls_core_*`, `walls_internal_*`; old stats are
  added up when read).

### Hanging walls: an object under a wall

A wall with an object drawn under it is above floor level - a beam, a
bulkhead, a drop wall. The whole element, from where it starts to where it
ends, is not a wall: the room runs under it and its area is counted in the
room. It is drawn on **AI_HANGING_WALL** (dashed, ACI 6) for checking, never
a room, never in an area. Where part of it stands on a real wall (another
wall hatch, a wall layer's faces) that part stays wall.

- Objects under a wall: a door swing (a door-size arc, 450-1300 mm, on a
  door layer or in a door block) with 150 mm or more of it inside the
  element (40 mm in from its faces), or a sanitary fixture body (the outline
  of one fixture block placed, 0.05-3 sq.m) 40% or more inside.
- Never objects: lift and elevator graphics (a lift that does not stop at a
  floor keeps its entrance detail in the closed wall - XA mid and high zone
  lifts; the no-door rule needs that wall), loose lines (a drain on a
  sanitary layer, section lines, solar panels, bracing - 1,033 lines cross
  real walls on the seven sites), door-size arcs that are not doors, a
  cistern partly in a duct wall (the WC-ledge rule).
- Only wall-like hatches: up to 600 mm thick, up to 15 sq.m.
- What leaves the walls: the element's hatch (wall hatches and the wall
  tracer's hatches), the lines drawn as its outline (a layer whose lines run
  along 90% of the outline - a real wall's face along part of it stays),
  and the C / L shape it makes on the column layer. Both engines: Step 3
  (`_prepare_floors`) and Step 4 (`geomextract`, `geoengine`).
- Found on the seven sites: one, BHUB 1st Floor - a 300 mm C-shaped
  element of the structure xref (XREF-T1_STR$1$A3-HATCH) across both
  toilets, three cubicle doors swinging under it. FEMALE TOILET 20.43 ->
  21.62 sq.m, MALE TOILET 20.82 -> 21.93, toilets 94.07 -> 96.37 (the
  typical floors: 21.62 and 21.94; the area sheet gives the 1st floor the
  typical floor's toilets, 97.12).
- Rule `hanging_walls` (on); `config.json` "hanging_walls".

Files: `dxf_reader.py`, `layernames.py`, `reports.py`, `app.py`,
`geoengine.py`, `geomextract.py`, `rules.py`, `walltrace.py`,
`config.json`, `templates/index.html`; tests `tests/test_hanging_walls.py`
(11), `tests/test_ai_layers_walls.py` (one wall layer),
`tests/test_geometry_fixes.py`.

## 2026-10-13 (later) — wall tracing checked against the corrected drawings

The walls were checked against three drawings whose walls were corrected by
hand: BHUB 1st Floor, the XA low-zone typical floor and H10 Tower 1 6th
Floor (its hatched walls). "Found" is the share of the corrected walls we
draw; "right" is the share of what we draw that is wall.

| Drawing | Step 3 drawing: before -> now | Final drawing: before -> now |
|---|---|---|
| BHUB 1st Floor | 44% / 83% -> **96% / 98%** | 76% / 45% -> **96% / 97%** |
| XA low-zone typical | 88% / 73% -> **90% / 81%** | 84% / 38% -> **91% / 81%** |
| H10 Tower 1 6th | 95% / 100% -> **95% / 99%** | 94% / 52% -> **96% / 99%** |

(found / right.)

### What was wrong, and what changed

- **Two wall builders.** Step 3 traced walls (faces paired nearest-first,
  confirmed by the hatch); the final drawing paired any two parallel lines
  80-400 mm apart, so facade glazing, mullions, lift-entrance graphics,
  bracing and area lines were drawn as walls (XA: 38% of the final
  drawing's walls were wall). The final drawing now traces as Step 3 does
  (`geoengine._add_walls` calls `walltrace.trace_floor`); a floor with no
  wall-layer lines keeps the engine's strips.
- **A hatch with several loops was read as its largest loop.** One BHUB
  structure hatch holds nine concrete walls as nine loops. Every loop is
  now read, holes and islands by nesting (`_hatch_islands`).
- **Concrete walls on a structure layer were not walls.** BHUB draws its
  400-450 mm core walls on `XREF-T1_STR$1$COLUMN`. Structure on a
  COLUMN / CORE / SHEAR / STRUCT / CONC layer is paired on its own, up to
  600 mm, and is wall where the structure hatch fills it and it runs like a
  wall (900 mm and twice its thickness); a column stays a column. BHUB
  core: 44% -> 96% found.
- **The slab edge outside a wall.** An unhatched strip with one face on the
  floor plate's edge and a wall behind it is the 200 mm band along the XA
  facade, not wall.
- **Curved walls** are paired as arcs (two concentric arcs on wall layers a
  wall's thickness apart): the XA tower's rounded corner was missed.
- **Windows and cut-outs stay in the wall** (drawn over it on AI_WINDOW /
  AI_CUTOUT): the corrected walls run on through louvres and cut-outs.
- **A balcony's parapet is the balcony's.** The parapet a balcony grew over
  leaves the walls of both drawings (BHUB 1st Floor: 7.19 sq.m drawn as
  wall and as balcony).

### Door openings (AI_DOOR)

Doors were the quads between door-closure lines a wall's thickness apart:
BHUB 19 of the 44 corrected doors (9 drawn where there is none), XA 16 of
29 (20). They are now found from the door swings (`door_openings`):

- a swing is an arc of 450-1300 mm sweeping 10-100 degrees, on any layer -
  BHUB and XA draw shaft doors on the shaft's layer (EL-SHAFT,
  ELEC SPATIALS), stopped part-open at 27-58 degrees;
- in a wall: the gap from a wall's end (a short wall-layer line across it,
  or a traced strip's end, with the wall behind it) to the next face across
  its band - the next wall's end or the face of the wall across (a door at
  a corner). The swing's centre is within 350 mm of a jamb and one of its
  ends reaches the gap; one leaf fits 0.55-1.35 of its radius, a pair
  1.6-2.5 with a swing at each jamb. Across, the gap is as deep as the
  lines drawn in it along the wall (XA's shaft doors are their 76 mm
  frame); lines running on past both jambs (an area outline) do not count;
  with none, the wall's thickness;
- with no wall - a WC cubicle or shaft door in a 12 mm board - a full swing
  with its leaf drawn open: from the hinge to where the leaf shuts, squared
  to the line it shuts on, as deep as the board;
- a dynamic door block flattened with all its states (XA `*U` blocks: six
  swings of 652-1101 mm on one hinge) is one door.

BHUB 44 of 44 doors (mean overlap 97%); the 2 more drawn are BMS shaft
doors like the others, not marked in the corrected copy. XA 28 of 29; the 7
more are 3 doors in the angled walls and 4 WC cubicle doors, not marked.
Step 3 and the final drawing alike. `config.json` "wall_output":
`door_radius_mm`, `door_sweep_deg`, `door_jamb_reach_mm`, `door_leaf_mm`.

### Measured on the 39 drawings

Rooms and the area statement: no change on any drawing except BHUB 1st
Floor's two toilets (hanging walls, above). Walls drawn: XA about 4.5 sq.m
less per floor (the facade band), H10 Towers 1-3: 1-3% more.

### Left as it is

- XA lift mouths (groups 2-4): the drawing's A-WALL runs across them and
  they stay wall (3.7 sq.m), as agreed for lifts with no door; the
  corrected copy leaves them open.
- XA: columns drawn inside wall lines (2.4 sq.m missed) and a few A-WALL
  strips the corrected copy leaves out.

Files: `walltrace.py`, `dxf_reader.py`, `geoengine.py`, `config.json`;
tests `tests/test_wall_tracing_fixes.py` (19), `tests/test_door_openings.py`
(13).

## 2026-10-14 — walls, doors, windows and glass, read as an architect reads a plan

Two new drawings: P14 6th floor (four wall mistakes marked by hand) and
P25 1st floor (the file holds the plan twice: as drawn, and a copy the
architect cleaned). Every drawing of all nine sites was run with the
13 Oct code and with this one, and each site checked on its own before the
next (`Drawing_Analyzer_validation_2026-10-14.xlsx` has every figure and
every item still off, with its image).

### The order of reading a plan

1. **Clear the drawing** (`dxf_reader._is_annotation`). Grid lines and
   bubbles, section and elevation marks, tags, title blocks, legends,
   revision marks, north points and break lines (by layer, the xref's last
   part: `XREF-T1_STR$0$A3-GRID` is a grid), and section lines, marks,
   titles, legends, trees and drawing numbers (by block) are cleared before
   anything reads the floor: never a wall, a door, a room's side or an X.
   A column-layout block named for a grid (`COLUMN-GRID-750`) keeps its
   columns. P25: a section line (A-SYMB-SEC in a SECTION LINE block) ran
   across the plate and closed a 530.50 "CAFETERIA" and a 574.80 "GARBAGE
   BINS"; H10 Tower 2: two grid lines cut the ground-floor ZONE-01 lift
   lobby to an 11.40 sliver (now 51.58, wall to wall); BHUB: a revision
   cloud notched the 19th/20th-floor lift lobby (40.60 -> 40.76).
2. **Walls, and what is not a wall or a name.** Level tags
   (`LVL + 555.35FFL`, `+65mm`) and grid notes (`GRID FOR CRITICAL ROOM`)
   never name a room (`labels.py`, `FFL_PATTERN`); kitchen platforms,
   counters, kitchen equipment and landscape are never walls
   (`walls.never_walls`: PLATFORM, COUNTER, KITCHEN EQUIP, LANDSC, PLANTING,
   PLANTS, VEGET). P25: KITCHEN 5500 X 8500 25.92 -> 39.39, MAIN FOOD
   8000 X 7200 32.04 -> 49.19, FIRE TOWER 2650 X 6300 10.53 -> 16.69.
   P14 (the four marked mistakes): a hatched wall whose faces do not pair
   (a face line missing, a niche) is filled from the hatch beside the
   traced wall, and a wall's run is carried across a short gap (<= 1200 mm)
   that is mostly wall hatch (`walltrace` rule 13, `wall_trace.hatch_fill*`);
   a strip the hatch fills is never absorbed into a longer one
   (`wall_polygons` b); door closures no longer cut walls. P14's right
   balcony 12.50 -> 18.96 (its twin's area).
3. **Doors, windows and glass, told apart from the walls.**
   - Doors: as on 13 Oct (the swing at a gap in a wall, closed at the
     frame), found first so neither a window nor glass takes a door gap.
   - Windows (`window_openings`): a gap in a wall with glazing or a louvre
     drawn across it is a window, frame-deep, on AI_WINDOW - beside the
     X-marked windows found before. A glass partition, a mirror or the
     area overlay ("2.1 TSIIC-BUA (exc glass)") is not window evidence
     (`windows.gap_*`). BHUB 1st floor: 4 of 8 -> 8 of 8 windows; XA
     typical: 0 of 11 -> 9 of 11.
   - Glass (`elements.py`, new; layer AI_GLASS): glass partitions and
     glazing - two lines a pane apart are one strip, a single line a 20 mm
     band; what lies in a window or a door is that window or door. Never a
     wall, a room or an area (`glass` in config.json).
4. **Rooms** are traced from the walls with the doors closed, as before.

The final drawing (Step 6) draws the same windows and glass as Step 3
(`geoengine._add_walls`, `write_dxf`, `build_final`, `app.py`).

### Measured

| Check | 13 Oct | Now |
|---|---|---|
| BHUB 1st floor walls found / right (hand-corrected) | 95.5% / 98.1% | 97.3% / 97.9% |
| BHUB doors / windows | 44 of 44 / 4 of 8 | 44 of 44 / 8 of 8 |
| XA typical walls found / right | 90.3% / 81.0% | 91.1% / 80.7% |
| XA doors / windows | 28 of 29 / 0 of 11 | 28 of 29 / 9 of 11 |
| H10 Tower 1 6th floor walls found / right | 95.3% / 98.7% | 98.4% / 98.4% |
| P25 plan as drawn against the cleaned copy: rooms that differ | 27 | 3 |
| P25 walls / glass / columns, as drawn against cleaned | - | 99.8% / 99.7% / 100% |

Against the manual area statements (BHUB, 285 FD Towers 1 and 2, H10
Towers 1-3) every site scores as on 13 Oct (the same cells within 1%).
Closer to the sheet: BHUB 19th-28th floor lift lobby (+0.16), H10 Tower 2
ground floor lift lobby (-33.17 -> +7.01) and staircase. Further: 285 FD
Tower 2 balcony on 7 floors by 0.46 sq.m - one balcony no longer runs
17.5 m down a 200 mm hatched wall cavity; the sheet is still 31 sq.m
above it. The cells still off are the known sheet conventions, shaft
naming, and the items in the workbook's "Unresolved" sheet.

### Tried and not kept

Clearing hidden and projection lines (A-ELEV-HIDN, A-HIDDEN, PROJECTION)
as drawing furniture: on the XA refuge floors the 29.51 refuge did not
change and the 52.28 refuge, the VRF ODU and the condenser room got worse.

### Still off (images in the workbook)

- XA refuge floors: REFUGE AREA 29.51 traced 10.03 and 52.28 traced 45.14
  - a diagonal structure / hidden line cuts them from the facade.
- Final drawing only: BHUB's concrete-hatched precast facade band
  (7.19 sq.m) is drawn as wall (Step 3 drops it); XA's 600 x 200 X-marked
  facade boxes are read as windows. Both as on 13 Oct.
- XA lift mouths (groups 2-4) stay wall, as on 13 Oct.

Files: `dxf_reader.py`, `walltrace.py`, `elements.py` (new),
`geoengine.py`, `app.py`, `labels.py`, `layernames.py`, `config.json`;
tests `tests/test_wall_hatch_fill.py` (8), `tests/test_elements.py` (13).
Suite: 546 passed.

---

## 2026-10-14 (later) — Priority 1: doors are exits, walls by their thickness, no columns in wall lines

Checked on one floor plan per project: BHUB 1st floor, P285 11th floor
(new), H10 Tower 1, XA low-zone typical, P14 6th floor, P25.

### What changed

1. **Door swings drawn as ellipses** are read like arcs
   (`dxf_reader._collect_door_ellipses`): an ELLIPSE that is a circle's
   arc (its points within 8% of one radius from its centre, the hinge),
   of a door leaf's size and sweep. 285 FD and the P285 11th floor draw
   their door blocks this way. (`wall_output.door_ellipses`,
   `door_ellipse_round`.)
2. **A door is there to get out of a room** (`elements.door_kinds`). Each
   opening is told by the rooms on its two sides, probed 150-1400 mm
   beyond its faces:
   - a shaft, riser or lift on either side (Shaft / Lift family, or a name
     such as ELEC SHAFT, FHC, TEN, COMM, STAIR PRESS; with no room traced,
     such a label within 900 mm) - a shaft door, on **AI_DOOR_SHAFT**;
   - the same room on both sides of a door in a board (60 mm or less) -
     a WC cubicle door, on **AI_DOOR_CUBICLE**;
   - anything else - an exit, on **AI_DOOR**. This includes the same room
     on both sides of a door in a wall: that room was traced through its
     own door (`doors_exit_same_room` in the stats counts these).

   All three are cut out of the walls as before. Each room's exits are
   listed when the drawing is read for areas (`exits`, `exits_to` on each
   room; from AI_DOOR, so a door the user draws or moves there counts).
   The Step 6 final drawing sorts the doors by the rooms settled there.
   The Step 4 geometry drawing reads no texts and finds no toilets, so it
   can tell shaft doors but not cubicle doors.
3. **Wall hatch filled by its thickness, not its box** (`walltrace`
   rule 13): a piece of wall hatch no wider than a wall anywhere (eroded
   by 225 mm, nothing or only specks left) is wall - beside a traced wall,
   or anywhere when lines on any layer run along 60% of its outline. A
   piece thin only by its thickness (its box is wider: an L) must be
   edged so; a hatch's own boundary, column outlines, dimensions, grid and
   centre lines do not count as edges.
4. **Solid fill is concrete, never a shaft** (`walltrace` rule 14): a
   solid-filled piece left over by a shaft label is wall where it stands
   against a traced wall or is edged; any solid piece left over against a
   traced wall is wall, label or not (`solid_against_wall_is_wall`), so
   the final drawing - which reads no labels - draws them too.
5. **No column in a wall line** (`geoengine._in_wall_line`): a piece 250 mm
   thick or less with wall-layer faces along both its long sides is the
   wall, not a column (`geometry_engine.column_in_wall_max_mm`). Columns
   are drawn in the final drawing as in Step 3 (checked: the same AI_COLUMN
   count on all six plans).

### Measured

| Plan | Exits on AI_DOOR, before | After | Shaft / cubicle doors moved off AI_DOOR |
|---|---|---|---|
| BHUB 1st floor | 17 of 17 | 17 of 17 | 12 / 13 |
| P285 11th floor | 16 of 26 | 26 of 26 | 13 / 44 |
| H10 Tower 1 | 120 of 120 | 111 of 111 (the check's other 9 are janitors' doors into shafts, now shaft doors) | 21 / 108 |
| XA typical | 8 of 8 | 8 of 8 | 11 / 7 |
| P14 6th floor | 40 of 41 | 40 of 41 | 2 / 0 |
| P25 | 71 of 71 | 71 of 71 | 36 / 0 |

P285 11th floor walls 114.9 -> 127.4 sq.m: the two 200 mm toilet walls
(2 x 2.45), the six solid blocks by the shafts (5.42; no longer shafts),
four RCC blocks at wall ends (1.85; were gaps) and the two lift jambs
(2 x 0.15; no longer columns). The walls of the other five plans, every
room and every statement value of all six are unchanged; against the
hand-corrected drawings BHUB 97.3% / 97.9%, XA 91.1% / 80.7%, H10 98.4% /
98.4% (walls found / right), doors 44 of 44 and 28 of 29, as before.

### Still off

- P14: a 650 mm door where the wall ends with nothing across it is not
  found; eight staircase-lobby doors are drawn as 473 x 12 mm slivers.
- XA: the door into COM ELV SH above the lift core stays an exit - the
  office outline runs through it into the shaft.
- H10 Tower 1 ground floor: the lobby in front of the staircase carries
  the label LIFT WELL 3100X2300, so its three doors are shaft doors.
- H10 Tower 1: G. TOILET is traced through its own door (32.91 against
  20.07 stated); its door is now an exit.
- A shaft door is told only where the shaft is traced or labelled near
  the door (P25: two doors by untraced shafts are uncertain).

Files: `dxf_reader.py`, `elements.py`, `walltrace.py`, `geoengine.py`,
`layernames.py`, `app.py`, `config.json`; tests `tests/test_priority1.py`
(30). Suite: 576 passed.

---

## 2026-10-14 (later) — primary and secondary doors; rooms closed at their doors, staircase parts, lift labels

Checked on the same six plans as Priority 1: BHUB 1st floor, P285 11th
floor, H10 Tower 1, XA low-zone typical, P14 6th floor, P25.

### What changed

1. **Every door is primary or secondary** (`elements.door_levels`).
   - A primary door gives direct entry into a room from the main accessible
     area. A secondary door can only be reached through a primary door or
     another room.
   - The main accessible area is:
     - the rooms of the circulation families: Lift Lobby, Circulation,
       Staircase, Service Lobby;
     - rooms named LOBBY, CORRIDOR, PASSAGE, FOYER, STAIR, LANDING, FIRE
       TOWER, AIRLOCK, WALKWAY, COMMON, OFFICE SPACE or SERVICE AREA;
     - the floor's largest untraced space (the common floor);
     - other untraced floor joined to these by an opening of 600 mm or
       more with no door in it.

     It grows through a door only into another named circulation room.
     Untraced floor behind a door is a room nobody labelled.
   - How each door is graded:
     - a WC cubicle door is always secondary;
     - a shaft door is graded by its side that is not the shaft;
     - any other door is primary with main area on one side, and
       secondary with rooms on both sides. If nothing is traced on a side,
       that side counts as common floor.
   - The direction a door swings is never used. A door is read from the
     spaces on both of its faces (probed 150-1400 mm either side), so it
     comes out the same whether it opens into the room or out of it.
   - Drawn on **AI_DOOR_PRIMARY** (colour 3) and **AI_DOOR_SECONDARY**
     (colour 30). The kind (exit / shaft / cubicle) and the names on the
     two sides go in XDATA (`DRAWING_ANALYZER_DOOR`) on each door.
   - AI_DOOR, AI_DOOR_SHAFT and AI_DOOR_CUBICLE in older outputs are
     still read: AI_DOOR_SHAFT meant "a door into a shaft".
   - Each room lists `doors_primary` and `doors_secondary` when the drawing
     is read for areas.
   - The Step 4 / Step 6 final drawing grades its doors the same way. It
     now reads its walls correctly for this: a wall with holes came in as
     one bad ring, so the floor was one piece and every door came out
     primary.
2. **A room that runs out through a door is closed at that door**
   (rule `close_at_found_doors`).
   - The door openings found (AI_DOOR_*) join the doorless openings that
     are closed when a room leaks. They are tried last: after the
     openings on the leak path, all openings, and the plain lines.
   - So a room that already closed keeps what it had. An unlabelled
     lobby shut off behind the door is joined to the room again.
   - H10 Tower 1 ground floor: G. TOILET was a 32.91 sq.m box at
     confidence 0.08. It is now traced along its walls at 30.97, stopping
     at its door, with the toilet lobby left out.
   - A shaft that has no stated size and would fill a room is closed at
     these doors too.
3. **A door between two wall-line ends with no wall gap** is found
   (`door_openings`, `shut_between_ends`; `door_between_ends_depth_mm`,
   100 mm deep).
   - The case: a shaft box drawn in single lines, whose door block is
     flattened with two swings (XA typical: PHE SH).
   - The door needs its hinge at one line end, its shut end at another,
     and both lines as returns.
4. **The rest of a staircase is traced** (`stairs_by_treads`).
   - A flight of treads drawn on a structure layer behind a wall in the
     stair core is now traced without its tread lines (the treads are not
     walls or guard lines).
   - A part of 3 sq.m or more lying within 1.5 m of a staircase takes
     that staircase's name.
   - H10 Tower 1 ground floor: +9.30 sq.m.
5. **A lift label over a room with hinged doors is not a lift**
   (rule `lift_not_with_doors`, `lift_refuse_doors` = 2). A lift's doors
   slide. H10 Tower 1 ground floor: LIFT WELL 3100X2300 stands in the
   stair lobby. The label is left unplaced, with a note.
6. **One shaft round its X strips** (rule `join_shaft_strips`, **off by
   default**).
   - When on, two X-marked strips face to face, up to 700 mm apart with
     no wall between, become one shaft: the strips and the band between
     them.
   - It is off because the H10 manual area sheets count the strips
     themselves (H10 Tower 1 typical floor: total shafts 25.11 manual,
     24.77 here).
7. **A name that matches no family and states no size titles no space over
   500 sq.m** (`unknown_unsized_max_sqm`). SERVER LOCATION, a note in the
   open office floor of H10 Tower 1's 9th floor, would otherwise have
   taken the whole floor (1000 sq.m) once its doors closed it.

### Measured

| Plan | Doors, Step 3: primary / secondary | Final drawing | Rooms or statement values changed |
|---|---|---|---|
| BHUB 1st floor | 28 / 18 (13 of them cubicles) | 27 / 19 | none |
| P285 11th floor | 39 / 44 (44 cubicles) | 39 / 44 | none |
| H10 Tower 1 (10 floors) | 111 / 138 (108 cubicles) | 131 / 118 | see below |
| XA typical | 20 / 17 (7 cubicles) | 19 / 18 | FIRE LIFT LOBBY 13.13 -> 12.71 (closed at its door) |
| P14 6th floor | 37 / 5 | 38 / 4 | none |
| P25 | 81 / 32 | 103 / 10 | none |

H10 Tower 1, against the manual area statement:

| Floor | Item | Manual | Before | After |
|---|---|---|---|---|
| Ground | lift_shaft | 44.77 | 52.32 | **44.77** |
| Ground | staircase | 45.72 | 30.28 | 39.58 |
| Ground | entrance_lobby | 266.00 | 0 | 256.71 |
| Ground | toilets | 40.60 | 67.24 | 65.30 |
| 17th | toilets | 66.39 | 67.09 | 64.72 |

- Key totals within tolerance went from 71 to 72 of 110, and statement
  cells from 106 to 107 of 330.
- On the 17th floor, G. TOILET is now traced (30.54, outlined 9700 x
  3675) instead of the 32.91 box. The entry passage in front of it is
  left out.
- ODU is now traced on seven floors (29.0 sq.m each; it is not in the
  statement).
- Against the hand-corrected drawings:
  - XA doors 28 -> **29 of 29** (the PHE SH door);
  - BHUB doors 44 of 44;
  - walls unchanged: BHUB 97.3% / 97.9%, XA 91.1% / 80.7%, H10 98.4% /
    98.4%.
- Every exit found by Priority 1 is still found.

### Still off

- H10 Tower 1, typical floors other than the 17th:
  - G. TOILET stays the 32.91 box. Its traced outline (30.5) is further
    from the stated 9700 x 3675 (35.65) than the box, so the size check
    keeps the box.
  - Its entrance from the corridor is not found, so the janitor's own
    door reads secondary there.
- XA:
  - VRF ODU still runs through the fire staircase hall (63.88 sq.m, as
    before). The yard's top door therefore has VRF ODU on both sides and
    reads secondary.
  - FIRE STAIRCASE-2 is still 0.56.
  - The core door above the lift core is unchanged.
- P14: the 650 mm door and the 473 x 12 slivers are unchanged.
- The final drawing reads no labels, so a room it does not find is
  untraced floor. A door is primary there when that floor is the common
  floor, which is why P25 shows 103 / 10 against 81 / 32.

Files: `dxf_reader.py`, `elements.py`, `geoengine.py`, `layernames.py`,
`app.py`, `rules.py`, `config.json`, `static/main.js`; tests
`tests/test_rooms_and_door_levels.py` (24, new), `tests/test_priority1.py`,
`tests/test_rules.py`, `tests/test_wall_tracing_fixes.py`. Suite: 600
passed.

## 2026-10-14 (later) — the two Test_1 analyses fixed, and the element identification engine

Decisions taken on the two analyses:

| Analysis | Point | Decision |
|---|---|---|
| 1 | Shaft rooms | A shaft's room runs to its walls and its door; the statement still deducts only the X-marked cut-out. |
| 1 | PHE shaft | It stays three parts: strip, middle band, strip. |
| 1 | Parapet | The balcony is measured to the outer face of a real parapet, and the parapet is drawn as a wall too. |
| 1 | FHC-3 band | The masonry band is a wall. The cut-out above it is not filled into the wall. |
| 2 | Lift doors | Always secondary. |
| 2 | TEN / COMM / FAPA | The room runs from the back cut-out up to the door. |

### Doors

- **A door drawn by its shut leaf.** A leaf is a panel of two door-layer lines 450–1300 mm long, 10–80 mm apart, framed at both ends or drawn open from one end. It is now a door, even where the wall is drawn straight through it (P285 FD4).
  - In a wall band, the band is opened across its thickness.
  - In a gap, the gap is opened from jamb to jamb.
  - This finds 18 doors on P285 (`door_leaf_doors`, `door_leaf_panel_mm`).
- **A lift's entrance is a door of kind `lift`.** It is a 700–2600 mm gap in the lift well's own wall, with the rest of that side walled, opening onto the floor. It is always secondary.
  - Only a lift that stops on this floor counts: not LIFT PRE / PRESS, not a lift lobby or machine room, not LIFT WELL BELOW (`lift_not_regex`).
  - On P285 this gives 10 doors: 8 passenger and 2 service.
- **A wall's end is never carried across the room behind a doorway.** In `_door_bridges`, the end face of a wall — a face up to 400 mm long, with a longer face leaving each of its ends on the same side — no longer sends a ray. That ray ran through the wall's thickness into the room.
  - FHC-3 on P285 now comes out at 1250 mm wide (0.69 sq.m), as drawn, instead of 900 mm (0.50).
  - The same fix squares off JAN (2.82 → 2.87), the two LOBBY rooms (17.85 → 18.45), FHR and CHW SHAFT (`doors.cap_face_max_mm`).
  - A wall end still closes a doorway when its ray meets the corner of another wall: within 30 mm of that wall's end (`doors.cap_corner_mm`). Without this, H10 Tower 2's FA-PA-SH, FP-SH and similar labels lost their X-marked cut-outs (51 manual-sheet cells).

### Shafts

- **A shaft's room runs to its door** (rule `shaft_room_to_door`, on by default). The space in front of a named X-marked cut-out becomes a room of the same shaft when all of these hold:
  - it is up to its door;
  - it is no larger than 5 times the cut-out (and 6 sq.m);
  - it is at least 300 mm wide, and is neither wall nor another room.
- An unnamed X standing along exactly one named shaft compartment takes that compartment's name.
- On P285: TEN 0.36 ×6, TEN / FAPA / COMM 0.42, ELE SHAFT 0.76, FHC 0.45, CHW 0.3 and FHR. The statement's *shaft_room* column (information only) is 7.24 → 16.44. The deducted shafts are unchanged.
- **An opening behind a shaft compartment's wall, along another shaft compartment, is that other shaft's opening.**
  - XA: the ELE SH strips no longer turn ELECTRICAL SH (2.9) into a "shaft room". ELECTRICAL SH is still deducted.
  - The neighbours now record which rooms each opening runs along (`touches`). In the statement, an opening that shares under 100 mm with a room but runs along a named shaft or riser is left to that shaft (`area_statement.AGAINST_MIN_MM`).
- **A shaft named from the label beside its X keeps the whole name.** For example, COM / ELV SH gives COM ELV SH (ELV), not COM.
- **A label drawn twice, one copy on top of the other, is read once.** On a room layer, two identical texts stacked are no longer joined into one label. Names such as "PHE SHAFT PHE SHAFT" and "ELECTRICAL SH ELECTRICAL SH" are gone from all 42 statements.
- **LIFT PRE, LIFT PRESS and LIFT PRESSURISATION are shafts** (family Shaft, AI_SHAFT), not lifts. The statement still files them under LOBBY PRESS.

### Final drawing (Step 4 / 6)

- **A cut-out is part of the wall only when it lies in a wall run.** That means the wall goes on past both of its ends, in line, or stands on both of its long sides.
- A cut-out lining a wall's face inside a shaft is no longer filled into AI_WALL. On P285 this is the PHE shaft's two strips and the FHC-3 slot (`cutout_in_wall_probe_mm`).
- Lift entrances are drawn there too.

### Balconies

- The parapet stays on AI_WALL (`balcony_parapet_not_wall` false). The balcony grows over it only along its open side: the longest side along the plate's edge.
- End walls running in from the edge are never grown over. P285 balcony total: 120.98 → 117.51.

### X marks: solid or dashed

- The line type of every line is now read: its own, or its layer's (DASHED, HIDDEN, CENTER ...).
- An X in a wall whose outline is dashed is a cut-out, not a window (rule `dashed_x_cutouts`). An X in a solid outline stays a window.
- BHUB: 4 louvre X's became cut-outs. XA: the two ELE SH strips and the COM ELV X were taken for windows and are now that shaft's openings.

### The element identification engine (`elementid.py`)

Every opening and core space is classified again from the evidence drawn round it, as the specification sets out. Each gets a 0–100 confidence score.

**Group A: Door, Window, Cutout**

- Indicators:
  - door: frame / jamb, leaf, opening, swing, sliding, tag;
  - window: glazing, frame, alignment, X in a solid outline, panes, sill, W;
  - cut-out: dashed outline with X, outline, edge interruption, no door / window, C.
- The key combinations, the negative rules and the X rule apply: solid outline in a wall reads as window, dashed as cut-out.
- Tie-breaks follow the specification.

**Group B: spaces**

- Tier 1: Shaft, Lift, Staircase. Tier 1b: Void, only between two flights of the same staircase.
- Treads are found as runs of parallel lines 180–400 mm apart.
- Tier 2: Lift Lobby, Service Lobby, Lift Machine Room. They are gated on an adjacent lift (and staircase) scoring 50 or more.

**Bands and actions**

| Score | Band | Action |
|---|---|---|
| 80–100 | High | Auto-classify |
| 50–79 | Medium | Auto-classify |
| 20–49 | Low | Needs review ("possible …") |
| 0–19 | Unresolved | Hold |

**Output**

- **Records.** One JSON record per element with the specification's fields: element_id, group, tier, classification, confidence_score, confidence_band, indicators_matched / _missing, boundary_type, x_symbol_present / _context, prerequisite_elements / _met, negative_rule_triggered, tie_break_applied, competing_classes, action, notes. Each record also carries floor, name, layer, current_class, agrees, area and position.
- **XDATA.** Each element in the drawing is tagged (`DRAWING_ANALYZER_ELEMENT`: id, class, score, band, action).
- **AI_REVIEW layer (new, reserved).** Elements scored under 50, or classified otherwise than drawn, are outlined here with a note. It is never a room and never a label.
- **Files.** The element list is written as Excel and JSON. In the app it is `/download/elements_xlsx|elements_json/<token>`, with buttons in Step 3 and Final outputs and the counts in the QA chips.
- **Scope.** The engine changes no area.
- **Switches.** Rule `element_scores` (on), and config `element_engine`:
  - `high_min` 80, `auto_min` 50, `review_min` 20: the band limits;
  - `tie_margin` 15: two classes closer than this are a tie and go to the tie-breaks;
  - `near_mm` 400: how far round an element evidence is gathered;
  - `review_layer`, `review_color`, `review_text_mm`, `review_disputed`: the AI_REVIEW marks, and whether elements classified otherwise than drawn are marked.

### Measured

All 42 drawings, run with the code before this entry and after it.

Manual area sheets, cells within tolerance:

| Site | Before | After |
|---|---|---|
| 285 FD Tower 1 | 361 / 422 | 361 / 422 |
| BHUB | 152 / 689 | 152 / 689 |
| H10 Tower 1 | 123 / 384 | 123 / 384 |
| H10 Tower 2 | 255 / 430 | 255 / 430 |
| H10 Tower 3 | 149 / 395 | 148 / 395 |
| 285 FD Tower 2 | 183 / 433 | 183 / 433 |
| **Total** | **1223 / 2753** | **1222 / 2753** |

The one cell lost is H10 Tower 3, 17th floor, total shafts: 32.44 → 32.28 (manual 32.74).

Statement changes:

- 285 FD Tower 1: balcony 1.5–3.4 sq.m less per floor, −35.38 in all (end walls no longer grown over). Shaft room (information column) 7.24 → 17.34 on a typical floor.
- P285 11th Floor: balcony 120.98 → 117.51; service lobby 35.79 → 36.90 (the LOBBY notch is gone); shaft room 7.24 → 16.44.
- XA: ELV 5.68 → 0.30 per floor (only the COM ELV SH cut-out is deducted, by the convention). Electrical shaft moves both ways (3.86 → 4.28 low zone, 2.79 → 1.38 mid / high zone), as the ELE SH strips go to their own shaft.
- BHUB: wall cut-out +1.8 sq.m per floor (4 dashed X's are cut-outs, not windows).
- H10 Tower 2 ground: lift lobby 102.60 → 125.78. ZONE-02 is now 51.56, like ZONE-01. Manual: 95.59, still open.
- P-25: PHE +3.42, balcony +7.6.

Element engine:

- P285 11th floor: 269 elements, all auto-classified, 4 marked (2 stair voids, 2 LOBBY → Lift Lobby).
- H10 Tower 3: 3341 elements, 128 marked.

### Files

`dxf_reader.py`, `elementid.py` (new), `elements.py`, `geoengine.py`, `area_statement.py`, `layernames.py`, `rules.py`, `app.py`, `config.json`, `family_rules.json`, `static/quantities.js`, `templates/index.html`.

Tests: `tests/test_element_engine.py` (new, 46) and `tests/test_wall_tracing_fixes.py` (the parapet stays a wall). Suite: 646 passed.

## 2026-10-15 — element engine: what is drawn on the door layer, and across an opening

Found by running BHUB first floor and H10 Tower 1 1st floor. No area changes.

- **Treads drawn on the door layer are stair flights.** H10 Tower 1 draws its treads on A3-DOOR, so both STAIRCASE rooms (23.04 sq.m) found no flight and scored Shaft 55. They are now Staircase 80 (High).
  - Door-layer lines are read only when no flight is found without them, and only in a run of six or more evenly spaced parallel lines (`element_engine.door_layer_treads_min`).
  - A door's leaf is never a tread.
- **A door-layer line across an opening, no longer than the wall is thick, is its jamb, not a leaf.** H10 Tower 1 draws the staircase wall again on A3-DOOR, so the 700 × 300 X-marked cut-out's ends were door lines. It scored Door 75; it is a Cutout again, 95. An open leaf at the hinge, longer than the wall is thick, is still a leaf.
- **Lines running jamb to jamb on no door layer are glazing or a sill, not sliding-door panels.** A sliding door's panels each run part of the way and overlap. BHUB's four 1200 mm louvres (LUV-3 / W-3, lines on EL-SHAFT) scored Door 85–90; they are windows again.
- **Marked for review:**
  - BHUB first floor: 5 → 1.
  - H10 Tower 1, 1st floor: 9 → 6. Three small X openings just outside a staircase wall are now "possible void" (Low): a flight on one side only, as the specification sets out.
  - P285 11th floor: the same 269 elements, none changed.

Files: `elementid.py`, `config.json`; tests `tests/test_element_engine.py` (+7). Suite: 653 passed.

## 2026-10-15 (later) — Test_2: doors as lines, shaft rooms, the stair's well, machine rooms, AHU rooms

Decisions on the Test_2 analysis:

| Point | Decision |
|---|---|
| Door line | (c) A shaft's room runs to the outer face of its door; every other room stops at its own face of the wall. |
| Shaft rooms | The PHE middle band and the FHC / FHC-3 cabinet rooms go to AI_MEP too. |
| Lift wells with no door | (b) MACHINE ROOM only, not LIFT SHAFT. |
| AHU layer | AI_AHUROOM. |

### Doors

- **A door is drawn as one line** joining the walls on either side, on the face of the room it opens into. It is no longer a rectangle across the wall (config `wall_output.door_as_line`).
  - Of two rooms, the line goes on the smaller one's face: the room entered, such as a lift, a WC or a store.
  - The opening itself is kept in the door's XDATA. The area step (doors per room) and the element list read it back (`door_quad`).
  - A door line drawn by hand is read as a band 100 mm either side of it.
- How doors are found, and primary / secondary, are unchanged.

### Shaft rooms

- **A small room that shares a side with an X-marked shaft and has a door of its own is the shaft's room**, not a shaft (rule `shaft_room_mep`). It goes on AI_MEP.
  - On P285 that is ELE SHAFT ×4, CHW ×2, the PHE middle bands ×2, FHR, FHC ×2 and FHC-3 ×2.
  - It has to be 12 sq.m or less (`rooms_and_doors.shaft_room_max_sqm`) and named as a shaft (a SHAFT, RISER or DUCT name, or one of the statement's shaft names: ELE, CHW, PHE, FHC…), named like its X, or unnamed. A refuge area, a store or a lobby beside a shaft keeps its family (H10 Tower 1's REFUGE AREA).
  - The family a user picked in Step 3 is never overridden.
- **It runs to the outer face of its door** (rule `shaft_room_to_door_line`), where the walls either side end.
  - TEN 0.42 → 0.54; ELE SHAFT room 0.765 → 1.065.
  - +3.66 sq.m on P285, all in SHAFT ROOMS (not deducted).
  - A door between two rooms is left between them.
- The statement files the FHC / FHC-3 cabinet in front of its X-marked slot as a shaft room.
- The element list leaves shaft rooms out: they are not core spaces.

### The stair's open well

- **An X inside a staircase with a flight on each side is the well between the flights** (rule `stair_void_hole`). It is a hole in the staircase on AI_VOID, never a shaft.
  - P285: ST-01 and ST-04. The staircase area is unchanged (33.95).
- In the final drawing, the geometry engine's shaft at the same place (SHAFT_30) becomes that hole too.

### Lift wells with no door

- **A lift with no door whose front faces a lift machine room is part of the machine room** (rule `lift_no_door_machine_room`). It goes on AI_MACHINEROOM.
  - P285 11th floor: the eight LIFT WELL BELOW beside LIFT ELE.PANELS.
- **The statement now fills MACHINE ROOM.** The Machine Room family was not linked to a column, so LIFT ELE.PANELS (47.61) counted as usable area.
- The doorless wells go under MACHINE ROOM only.
  - P285 11th floor: lift shaft 113.98 → 63.98; machine room 0 → 97.61 (manual 99.64).
  - Plinth +50.00, sale area +71.43.

### Walls

- **The wall left between the rooms goes on AI_WALL** (rule `wall_gaps_filled`), in Step 3 and in the final drawing. No room changes. Two kinds of piece go in:
  - a wall-hatched piece still on no layer: the 200 × 200 pillar between the two doors of each ELE SHAFT;
  - a strip no wider than 200 mm, enclosed by rooms, doors and walls, with no label in it: the 450 × 150 strip beside each FHC cabinet.
- Some pieces are left out on purpose:
  - a column's casing;
  - the rest of a door's opening;
  - a shaft compartment's remainder (LIFT PRE: 250 mm, wider than a wall).
- A wall hatch drawn as several separate pieces is read piece by piece for this. Before, only its largest piece was read (`_collect_wall_hatches(islands=True)`).
- **A slab-edge line through the shafts is never a boundary** (rule `slab_edge_not_boundary`, config `slab_edge_lines`). This is a single line on a detail or section layer, 3 m or longer, running through X-marked shafts.
  - It applies in Step 3 and in the geometry engine.
  - P285: the 13.8 m line on a-details. The east balcony now runs to the wall: 40.05 → 44.68.

### AHU rooms

- New family **AHU Room** on **AI_AHUROOM** (colour 200), ahead of MEP / Services, which no longer lists AHU.
- AHU PIPE SHAFT and AHU SHAFT stay shafts. An X-marked opening named for an AHU (P25: AHU - FA-1 1000X4150) is a shaft opening and stays on AI_MEP.
- The statement files AHU rooms by name, as before (117.84).

### Refuge areas

With the slab-edge line gone, 285 FD Tower 2's 5th and 8th floors lost a refuge (312.23 → 155.98): REFUGE AREA-1 traced 159.9 sq.m and was dropped, and the BALCONY 100MM SUNK strip inside it took its place. Three changes put it back without upsetting Tower 1:

- **A refuge area is not held to the 150 sq.m cap on a services label** (FIRE PANEL, FHC) that states no size. AHU and plant rooms were already exempt.
- **A refuge over that cap is traced again,** first with its doorways shut, then with the plain lines closing it too. The first trace that keeps at least half of it is used. Over 450 sq.m it has run out over the floor and is dropped (config `boundary.refuge_unsized_max_sqm`, `refuge_close_min_share`).
  - 285 FD Tower 1, 16th floor: REFUGE AREA-1 stops at the sunk balcony's edge line (202.93 → 158.25), so the balcony beyond keeps its own room. Refuge 322.24 (manual 322).
- **A balcony open to a refuge area is part of it** (rule `balcony_open_to_refuge`). A balcony label whose fill runs into a refuge and holds nothing but refuge names is no longer drawn as a box; the refuge names the room, as the manual take-offs count it.
  - Tower 2, 5th and 8th floors: refuge 320.22 (manual 315), balcony 39.26.
  - The refuge is 8 sq.m larger than before (312.23): the strip between the slab-edge line and the core is in it now.

### Measured

P285 11th floor (the Test_2 drawing):
- 109 doors, all drawn as lines.
- Balcony 40.05 → 44.68.
- Lift shaft 113.98 → 63.98; machine room 0 → 97.61 (manual 99.64).
- Shaft rooms 16.44 → 22.38. Plinth +50.00, sale area +71.43.

All 42 drawings ran. Against the manual sheets, 1219 of 2753 cells are within 1% or 0.3 sq.m (1222 before). Three cells moved out:
- H10 Tower 2, 10th floor, machine room: 90.46 against 46.19. The two LIFT TOP wells (44.26) are now machine room; the manual keeps them under lift shaft.
- 285 FD Tower 2, 5th and 8th floors, refuge: 320.22 against 315 (312.23 before), for the strip beside the core.

Closer to the manual:
- BHUB refuge floors: TEN 0.69 → 0.60, future shaft 0 → 0.09 (manual 0.60 / 0.11).
- 285 FD Tower 2 balconies on the typical floors: 95.08 → 101.05 (manual 126.97).
- 285 FD Tower 1, 16th floor, refuge: 323.89 → 322.24 (manual 322).

Lift wells now under machine room:
- H10 Tower 1 terrace: 6.59.
- H10 Tower 2, 10th floor: 44.26.
- H10 Tower 3: 27.34.
- XA 22nd floor: 6.03.
- P285 11th floor: 50.00.

SHAFT ROOMS goes up on every site. It is an information column, not deducted.

### Files

`dxf_reader.py`, `elements.py`, `elementid.py`, `area_statement.py`, `app.py`, `geomextract.py`, `rules.py`, `config.json`, `family_rules.json`.

The files as they were before are in `_backup_2026-10-15_before_test2`.

Tests:
- `tests/test_rooms_and_doors.py` (new, 35);
- `tests/test_wall_tracing_fixes.py` (reads door lines).

Suite: 688 passed.

## 2026-10-15 (later) — rotated cores, shafts as compartments, doors on the inner face, stair wells, windows in walls; the CLEAN FORMAT statement

The combined prompt (Fix 0 – 4 and Part B). Tested on the four drawings
(P285 11th, XA 37th, BHUB 1st, Centaurus 11th), P285 turned by 30° and
45°, and the 42-drawing bench. 744 tests (20 new).

### Fix 0 — every element rotation-aware (`rotation.py`, new)

- **A core drawn at an angle is worked out in a drawing of its own, turned to the axes.** The walls of a floor are grouped by direction; a group ≥ 4° off the axes with ≥ 12 m of wall, walls both ways (the across run ≥ 30 % / 8 m), and more wall of its own inside its hull than axis-aligned wall, is a *rotated core* (rule `rotated_cores`, config section `rotation`). The drawing is copied (written out and read back: blocks, layers, dimensions and all - nothing is opened out, so the passes read the copy exactly as they read the original), every modelspace entity is turned by −θ in place (coordinates snapped to 0.1 µm so a turned wall lies exactly on the axes) and what lies outside the core's box is dropped; the Step 3 floor pass and the geometry engine run on the copy **unchanged**, and the results are turned back and merged by the core polygon: what the turned pass found inside the core replaces the main pass there. A direction holding ≥ 60 % of all wall is the whole drawing turned: one pass over the plate. (The first cut opened blocks out into a new document; it lost nested block content - 100,000 segments and 41 doors on P285 - and was replaced.)
  - Centaurus 11th: the S3 core (−20°) is found; `S/C-S3` is a staircase on AI_STAIR (was white / AI_UNKNOWN), `FIRE LIFT 2.70X2.50 / SERVICE LIFT 1.35 TON` is paired with its X and drawn (6.87), `BALCONY - 03` (36.16) is traced, FIRE TOWER/SERVICE LOBBY-03 19.29 → 20.99, and PREZ beside it is traced as a compartment.
  - P285 turned by 30° and 45°: the whole drawing is one turned pass; shaft cells, stair voids and room counts match the unrotated run (see the regression note below).
- Sizes: the geometry engine's X (`size_mm`) and columns are measured by their minimum rotated rectangle, not the bounding box. Turned elements are drawn turned.
- The audit of axis-aligned assumptions (15 items: `_find_x_pairs`, `_tread_runs` / `_stair_runs`, `_tread_side` / `_flights`, `_hv_boundary`, `BandIndex`, `_grow_to_stated`, lift-lobby side bands, `_shaft_cells_to_door` probes, …) is in `Final_Outputs_2026-10-15_Rotation/Fix0_audit.md`: each is handled by the turned pass, converted, or safe as it is.
- Known limits: a rotated element that is not part of a wall cluster (one angled X in a square floor; XA 37's FIRE STAIRCASE-2 at the chamfered corner, whose core has 4.2 m of its own cross wall against a 45 m facade run) still goes through the main pass as before. With the cross-wall test relaxed to take that corner, the turned pass traced PHE SH over the stair, so the test was kept.

### Fix 1 — a shaft is the compartment its X stands in (`shaft_compartments`, rule `shaft_compartment`)

- The X marks the shaft; it is not its boundary. Every X-marked shaft is traced from the X like a room, on a tracer with every opening closed (its door too) and the drawing's plain lines (a partition line between a shaft and its room, on any layer) as walls. Seeds: the X's centre, then points along its axes, so an X partly outside its box seeds from the part inside.
  - The trace must hold ≥ 60 % of the X and be no more than 4× its area (or 1.3× the stated size); else the X stays as drawn, cut to the walls, and is flagged on AI_REVIEW (`shaft kept as its X: …`).
  - An X across two compartments is split by the line between them, one shaft each (`shaft_cells_split`; Centaurus PHE: 12 splits). The walkway with no X is left alone.
  - A room already traced over the compartment IS the shaft and takes the X (`shaft_cells_to_room`); a room whose trace ran past a partition line is cut back to its side.
  - A stair's well (an X between two flights) is never traced (Fix 3 runs first).
  - With a partition line, the side holding the X is the shaft and the side with the door is the shaft's room on AI_MEP (`_shaft_cells_to_door` now takes what lies beyond the compartment). The part of the X's own box that the trace left out - an X drawn over the room beside it - stays that room's (H10 T2: PL-SH-03 drawn 750 mm into its toilet; found on the bench, the toilet had lost 1.31 sq.m to a "shaft room").
  - A cell the label's stated size already gave (`x_cell`) stands when the trace gives less: the stated size is the better witness.
  - X marks found in a wall or by their outline (no diagonal pairs) are traced too.
- Final drawing: "shaft: geometry wins" is now "geometry wins only when the texts produced no traced cell for it" (`_final_defaults`: a text row with `cell`, or source P/S, stands against the geometry's X box). Boundaries carry `cell` in their XDATA; geocompare carries `source` and `cell`.
- The Test_2 split where the X box is the shaft and the room runs to the outer door face is removed (rule `shaft_room_to_door_line` gone).
- Per drawing (traced / split / fallback, of the X items): P285 37 / 0 / 6 (5 are 0.04 sq.m `Unknown Shaft` slits with no walls round them, 1 is an X whose trace did not close); BHUB 26 / 4 / 3; Centaurus 77 / 12 / 7; XA 8 / 0 / 0 (its other 15 shafts have no X and are traced from their labels as before). The per-shaft list (area vs the X and the stated size, door on the inner face) is in `Final_Outputs_2026-10-15_Rotation/<site>_shaft_report.txt`.

### Fix 2 — doors sit on the inner face of the wall

- Every door line sits on the inner edge of the wall of the room it serves, at the wall's own angle, shaft doors included (`elements.door_line`). Test_2 decision (c) — a shaft's room running to the outer face of its door — is removed: shaft rooms stop at their own face (P285: SHAFT ROOMS 22.39 → 18.74, not deducted; nothing deducted changes).
- XA `COM ELV SH`, Centaurus `EL.TEN` and `PHE SHAFT`: on face.

### Fix 3 — an X between two stair flights is a void

- `stair_void_hole` runs before the shaft trace. P285: ST-01 and ST-04 unchanged (2 voids cut). Stair abbreviations `S/C`, `S.C`, `SC-`, `STR-` are staircases; `STR PRE / STR PRESS / STAIR PRESS` are stair-pressurisation shafts (XA: `STR PRE` 0.84 now under STAIR PRESS, was unnamed). The stair-well range takes XA's 446 mm well (`stair_well_short_mm` 400–1400).
- XA 37 `FIRE STAIRCASE-1`: the label stands in the well strip between the flights (a 2.04 × 0.28 m cell, no X drawn in it on this floor), so there is no X to cut; the strip is traced from the label as a 0.56 sq.m staircase part and the flights (`FLIGHTS-02 …`, 23.14) are the staircase. Reported, not hidden: it still fails as a void because the drawing has no X there.

### Fix 4 — a window never cuts a wall (`wall_polygons`, `window_bridge_mm`)

- Where the wall faces broke at a window and the strips either side paired with the window's lines (XA lift bank: 109 mm of a 300 mm wall), the band is filled across the window from the window's own rectangle, run on 600 mm into the wall either side at the wall's thickness there (the shortest run across the wall beside the window; a wall meeting it at a corner never widens it; ≤ 600 mm; only where the wall goes on through the whole 600 mm - never across a jamb into the shaft behind it, which cost H10 T3 0.12 sq.m on six shafts a floor before the bench caught it). Only the windows the floor pass hands over are bridged — never a shear wall or a column among the core shapes, and never in the geometry engine's own wall mass (the first cut filled corners in with slanted hulls and ate 0.2–1.0 sq.m out of BHUB's rooms, then its shear walls widened the engine's staircases; both found on the bench and fixed).

### Part B — the Area Statement workbook (`write_area_statement`, rewritten)

- One sheet, formula-driven, in the CLEAN FORMAT layout: title formulas; the statement per tower (tower band, group titles, header row, grey 8-pt key row, one row per floor, TOTAL, GROSS AREA sq.ft, SUMMARY), every figure an `INDEX/MATCH`, `SUMIFS` or `SUMPRODUCT` over defined names; QUICK LOOKUP (`LkFloor`, `LkElement`, `LkCategory`); AREA REGISTER with Excel outline levels (plate › group › column › element rows, each plate once; Nos, Area, Key the only inputs); BY LAYER with `LayPlate`; WORKING DATA (Settings, Floor list + 5 spare rows, Category map, Group titles) grouped and hidden. No XLOOKUP / FILTER / structured references; no reconciliation zone. The old "Room allocation" sheet is no longer written.
- Verified by full recalculation (LibreOffice headless here, no Excel): zero error cells; every statement cell equals `statement_view` on P285, H10 T2 (19 plates) and P285 T2 (5 plates incl. a typical). The sample's "DONE WHEN" figures (plinth 4,765.49 / sale 6,807.84) are from the 2026-10-14 data; the 11th-floor figures now differ by the machine-room decision of Test_2 (machine room 97.58), which the sample predates.

### Bench (42 drawings)

Statement cells within 1 % / 0.3 sq.m of the manual sheets: 1411 of 2310. On the same six sites the previous round's code gives 1032 of 1840 and this round 1031 (BHUB 116 → 118, 285 FD T2 209 → 208, H10 T1 187 → 184, H10 T2 336 → 335, H10 T3 184 → 182; P285 T1 384 both). Rooms at IoU ≥ 0.95 against the take-off polylines: 347 of 397. Three regressions were found on the bench during this round and fixed before delivery: the window bridge over shear walls and columns, the bridge run through a jamb into the shaft behind it, and an X drawn over its toilet becoming a shaft room. The cell-by-cell diff is in `Final_Outputs_2026-10-15_Rotation/bench_r7_vs_f6.txt`.

### Files

`rotation.py`, `tests/test_rotation_and_compartments.py` (new); `dxf_reader.py`, `geoengine.py`, `geocompare.py`, `app.py`, `rules.py`, `elements.py`, `area_statement.py`, `family_rules.json`, `geo_profiles.json`, `tests/test_rooms_and_doors.py`, `tests/test_area_statement.py`. Backup: `_backup_2026-10-15_before_rotation_fixes`.

## 2026-10-16 — every X placed against the walls (shafts not found by layer), windows and cut-outs by evidence, tags never names

A shaft is no longer recognised from the layer its X is drawn on. Every X on the floor is found by its shape, the walls are traced
with the X's own frame held out of them, and each X is then placed against those walls: an opening in a wall (a window or a
cut-out, never a shaft), a column's cross, a symbol, a shaft (a wall within 50 mm of a side), or an X with no wall beside it (no
shaft). Tested on BHUB 1st, Centaurus 11th, P285 11th and XA 37th (one floor each). 772 tests (28 new in
`tests/test_x_placement.py`).

### X marks by shape (rule `x_by_geometry`)

- Every pair of diagonals crossing at their middles is an X, whatever its layer. Smaller than 150 mm a side or 0.03 sq.m is a
  symbol and left out (`x_placement.min_side_mm`, `min_area_sqm`). The X marks found by the cut-out layers stay as they were.
- An X found by its shape that has the size of a lift or of a stair's well is no longer dropped when it turns out to be neither:
  it is placed like any other X (XA: the 2500 x 500 slab opening of ELECTRICAL SH was dropped as "not a well").
- An X drawn turned off the axes (its four ends off the corners of its box) is the turned rectangle its ends make
  (`_turned_x_outline`; XA PHE SH at 23°: 0.28 sq.m, was its 0.49 box).

### The X's own frame is not a wall (rule `x_frames_not_walls`)

- A line along an edge of an X, within the edge, is the X's frame and is held out of the wall finder - unless it is a real wall
  face: paired outside the X with a line 80-400 mm away. That partner is taken for another wall's face (P285 LIFT PRE: the
  masonry 250 mm above the X) only when it pairs at least as well with the line beyond it; XA SPARE SH: the 200 mm wall's far
  face (803 mm) against a 503 mm slab edge beyond is the wall, so the X stays 0.72 (was stretched over the wall, then cut 85 mm).
- Paired both ways, a line of another layer than the one drawing the X's closed outline is the wall's face (XA STR PRE: A-WALL
  along the FLOR-OTLN outline, the stair's landing line 100 mm beyond - the 600 x 200 X in that wall is a cut-out again).
- A column's cross keeps its outline (the column's, not a frame): with it held out, XA FHC SH traced a 0.06 sq.m sliver of the
  steel column instead of its 0.45.
- The frames still close the shaft's own trace, so what a shaft deducts is unchanged; an X's sides move out onto a wall only past
  its own frame (P285 LIFT WELL BELOW stays 6.25, was stretched 250 mm to the wall).

### Placement (rule `x_placement`, new `xplace.py`)

In order:
1. **Symbol** - under the size guard.
2. **Column** - on a column, ringed by a steel column's outline (lines 5-60 mm outside it on all four sides, open floor beyond
   two of them), or inside a solid fill no larger than itself.
3. **Opening in a wall** (a window or a cut-out, never a shaft) - the wall carries on past an end of the X at the X's own
   thickness (within 40 mm, over 400 mm, covering 70 %), or the X lies inside a wall that runs on past it; the X is no thicker
   than 600 mm. A wall found only inside another X standing across the band (Centaurus FHC: an L of two X marks in one cut-out
   outline) is that opening's outline, not a wall running on; a cut-out in line in the same wall still is (BHUB: C-3 / C-4 / C-6
   side by side). A wall strip "runs on" only when the next strip carries on 100 mm past it (`run_on_mm`; the same strip found
   twice is no run).
   - **Window or cut-out**: a window only on evidence - glazing, the window finder's window, or a window tag (W-1, ALW3) nearer
     than any cut-out tag; otherwise a **cut-out** ("if it cannot be determined clearly, a cut-out").
4. **Shaft** - what spilled into the wall is cut back (the X's own rectangles with it: XA STR PRE's 300 mm in the facade wall
   came back as a second STR PRE), and a wall stands within 50 mm of a side (`wall_gap_mm`; on these four floors every real shaft
   is within 20 mm, the nearest X that is not a shaft 75 mm).
5. **No wall beside it** - not a shaft: an unnamed one, or one with a cut-out tag (C-7) beside it, is a cut-out unless it stands
   in a staircase or a room.
- Every X is logged with where it went and why (`stats["x_placement"]`, the `<site>_x_placement.txt` reports).

### Tags (`tags_never_name`)

- `…D`, `D-n`, `FMD`, `SD-3A` are door tags, `…W`, `W-n`, `ALW3` window tags, `C-n` cut-out tags. None of them names a shaft or
  a room (BHUB SD-6 named the BMS shaft; C-10 named a cut-out). A code with no digit and two vowels is a word, and a shaft name
  (CHW, TEN, FHC-3) is never a tag.

### Walls

- Rule `label_strip_not_wall` (2-1): a wall strip holding a room's or a shaft's name, and not carrying on in line at its
  thickness, is the space it names (a wall does not change its thickness except at a junction or a column).
- Rule `x_box_not_wall` (2-2): a shaft's X beside a wall is never drawn as wall; a window or a cut-out in a wall still is.

### Labels

- Rule `x_label_not_across_wall`: a shaft label outside an X, with no stated size, standing in a room of its own with a wall
  between that room and the X, names its room, not the X (XA: CA-ELEC SH on the face of the wall beside the ELECTRICAL SH X took
  that X, and the CA-ELEC SH shaft was never drawn). Tags (TEN, FAPA, RWP) and labels stating the X's size still reach across;
  a label standing on a column beside its shaft still names it.
- Rule `x_named_by_its_room`: an unnamed X at the back of a small room named for a shaft takes the room's name; the room is the
  shaft's room (AI_MEP) as before (XA ELECTRICAL SH; Centaurus COMM/OFC, CCTV/ELV, LCS, FHC: 11 X marks named).

### Shaft tracer, statement, settle

- A doorway or gap closer never runs through an X in the shaft's own trace (XA: a lift's door line carried 1.9 m through SPARE
  SH; PHE SH was split in two by another).
- Statement: a lift, staircase or void next to a walled shaft is not that shaft's opening. A lift's X (found by its shape, or
  marked) beside a walled shaft made it a "shaft room", not deducted (XA: CA-ELEC SH, ELE SH (FRM), LLP SH 1.27, PHE SH 0.22);
  they are deducted again (this was the cause of the LLP SH 1.27 item left open on 2026-10-15).
- Element engine (`element_engine.x_window_needs_evidence`): an X in a solid outline in a wall is a window only with glazing, a
  window frame or a window tag; otherwise a cut-out - the same rule as the placement, so the drawing and the element list agree.
  A window tag with glazing wins over a dashed outline.
- Settle: the leftover pieces of floor are joined largest first and closed over the 2 mm the touch test allows. The order shapely
  hands them in changed with an unrelated X, and Centaurus S/C-S2 came apart into two staircases (60.78 → 33.58 + 25.28).
- The pass cache stamp includes `xplace.py` and `elementid.py`.

### Files

- New: `Drawing_Analyzer/xplace.py`, `tests/test_x_placement.py`.
- Changed: `Drawing_Analyzer/dxf_reader.py`, `rules.py` (7 rules), `config.json` (`x_placement` section,
  `element_engine.x_window_needs_evidence`), `elementid.py`, `area_statement.py`; tests `test_element_engine.py`,
  `test_label_claims.py`, `test_rotation_and_compartments.py`.

## 2026-10-16 (later) — the X sequence: every X decided once, final, in one order

Every X mark of a floor is decided ONCE, in Step 3, in this order, and nothing after it changes the answer (`xplace.py`: `x_facts` reads, `resolve_x` decides):

1. every X mark on the floor - two diagonals crossing at their middles, on any layer, of any size - is a **void** to start with;
2. **shaft** - a wall within 50 mm of a side (what spilled into the wall cut back first); never one inside a wall, round, on a column, over 30 sq.m, a stair's well, an unnamed X with a cut-out tag, or one the lift rules take;
3. **lift** - a LIFT label, or lift size (1800-3600 mm a side, aspect at most 1.5) not named for a shaft, walled on at least 3 sides;
4. **window** - inside a wall, with glazing, the window finder's frame, or a window tag nearer than any cut-out tag;
5. **cut-out** - inside a wall with no window evidence (a slot the wall holds whole included); or a cut-out tag beside it; or unnamed with no wall within 50 mm - never one standing in a room or a staircase (in a notch the room was traced round counts), a column's cross, or one over 30 sq.m;
6. **column** - on a column, ringed by a steel column's outline, or in a solid fill no larger than itself;
7. what is left stays a **void**.

The decision is made at one point of the floor pass, once the labels, the rooms and the staircases (by their treads) are known. An X in a wall or on a column is held out of the label matching from the start; one over 30 sq.m is a void item from the start (its texts are its own). The class, the step and the reason are stored on what is drawn (XDATA `DRAWING_ANALYZER_X`) and listed per X in the run report (`x_placement`: class, step, why, and what every step before made of it). An unnamed X lift is named `LIFT`, an unnamed X void `VOID`.

### Nothing overrides it

- **Step 3**: no family picked in the dropdown, no shape picked, remapped or deleted in the Geometry tab ("draw only my selection" included), and no user rule changes, drops or reshapes an X. The rules that used to change an X's class after the decision are gone: `x_not_shaft_to_cutout`, the round / shaft-on-wall validation, `x_opening_is_shaft`, the any-layer lift / well dropping, the stair-well test inside `shaft_compartments`, the void text re-filing a void under the text's family, the trap-door removal (an X-marked trap door is an X like any other), `lift_no_door_machine_room` on an X lift (it still runs on a lift traced from its label), the geometry family guess on X items. `shaft_compartments` and the strip joining trace only an X the sequence made a shaft; a shaft's room is found only beside one; the stair's well is cut out of its staircase only when the sequence made it a void between the flights.
- **Element engine** (`elementid.py`): shafts, lifts, windows, cut-outs, columns and voids are no longer scored - each is a fixed record (score 100, action "Fixed - Step 3", the step and reason in its notes), never reviewed or disputed. Doors are scored as doors only; tier 1 scores staircases only; tier 2 (lift lobby, service lobby, machine room) stands on the Step 3 lifts and staircases. Columns from the X sequence are listed (group C).
- **Geometry engine** (`geoengine.py`): finds and reports no shaft, lift, window, cut-out, column or void (`STEP3_CLASSES`). It reads an X mark or a lift-size square only as evidence - the place of an X is no room; lift wells along both sides make a lift lobby or machine room; flights either side of a long X make a staircase. Its walls are cut round the shapes of the one column finder Step 3 reads too; no gap windows, no lift entrances of its own. A finding of one of the six in a result written before today is dropped where a result is read (`step3_free`), and none of the six can be a Step 4 category.
- **Step 5 and the final drawing** (`app.py`, `geocompare.py`, `static/geoai.js`): a text row of one of the six is fixed (the texts' boundary, family and name; no precedence, no decision changes it - the table shows it as "Step 3, final"); no other row can be given one of those families; a geometry finding standing (half or more) on Step 3's six is left out. Columns are Step 3's AI_COLUMN only; the stair wells are always Step 3's holes; Step 3's windows and the cut-outs in a wall run are put into the walls (a cut-out lining a wall's face cut out of them); the lifts' entrances are found for Step 3's lifts in the final walls; glass in a window is the window's.
- **Area statement**: a Void-family boundary is a void (or a double / triple height cut-out, or a cut-out above, by its name), whatever the text in it says. Cut-outs on AI_CUTOUT stay "cut-outs in walls, not deducted", as before.

### Details settled on the four test floors

- **Any layer**: the X regions Step 3 traces (a strip of X marks inside one outline) are read on any layer except the layers that are never an opening (furniture, hatch, dimensions, text...), not only on cut-out layers (Centaurus PHE strips, 300 mm x 5-10 m, were missed).
- **A slot the wall holds whole**: an X lying 99 % or more inside a wall, with a side under 150 mm or under 0.03 sq.m, is an opening in the wall (cut-out or window), never a shaft (BHUB 400 x 110 sleeve).
- **In a room**: an X counts as standing in a room when it is inside the room's outline (the room's holes filled), or in a notch the room was traced round (within 150 mm of the room, inside its convex hull, and in no other room) - BHUB AHU sleeves inside 500 mm circles.
- **Drawn as it is**: an X with no wall within 50 mm keeps its own extent; it is not grown to a wall.
- **Loose cut-outs are no wall**: only Step 3's windows and the cut-outs in a wall run are cut out of the walls; a cut-out on open floor is not filled into the walls.
- **Names**: an unnamed X lift is named `LIFT`, an unnamed X void `VOID`; in the final drawing every name is put inside its outline (an L-shaped room's middle can lie outside it, and the area statement read the name as "Unknown").

### What changed on the four test floors (final drawing, sq.m)

- **BHUB 1st** - matches the sequence confirmed on the step-by-step images, X by X (81 X: 23 shaft, 14 lift, 4 window, 36 cut-out, 4 void). AHU rooms -0.83 (the four sleeves in the AHU rooms are voids, the rooms traced round them), void +0.48, wall cut-out +0.09.
- **Centaurus 11th** - FHR +1.97 (three FHC X now named shafts), service lobby -3.37 (a geometry lobby inside the Step 3 fire lift is gone - it was counted twice), shaft room +0.93, unnamed opening -2.78, PHE -0.03, void +0.12 (six A-DRAIN floor-drain X symbols, 0.02 each - "irrespective of size"), AHU -0.05.
- **P285 11th** - lift shaft +50.00, machine room -50.00: the eight LIFT WELL BELOW X are lifts (walled on 3 sides, lift size); the machine-room rule no longer overrides an X lift.
- **XA 37th** - nine more X regions found on any layer, named: electrical shaft +1.09, FHR +0.60, other opening +0.78 (TENENT SH), PHE +0.38, shaft room -1.35, unnamed -1.35, wall cut-out +0.05, lobby pressurisation +0.01.

### Switches removed (rules.py) — a saved selection naming them is ignored

`x_marks`, `x_by_geometry`, `x_placement`, `windows_in_walls`, `dashed_x_cutouts`, `stair_void_hole`, `lift_x_any_layer`, `x_opening_is_shaft`, `no_round_shafts`, `shaft_on_wall`, `x_unlabelled`, `trap_door_in_room`. config.json `x_placement`: `min_side_mm` / `min_area_sqm` removed (no size guard); `lift_side_mm`, `lift_aspect_max`, `lift_walled_sides`, `slot_side_mm`, `slot_area_sqm`, `slot_in_wall_share`, `in_room_reach_mm` added; `element_engine.x_window_needs_evidence` removed; `geometry_sequence` is now columns, x_marks, staircases, toilets, lift_wells, lift_lobbies, service_lobbies, rooms (an older list's `shafts` is `x_marks`, `windows` is gone). geo_profiles.json `identify`: WINDOW, CUTOUT, SHAFT, LIFT, COLUMN removed.

### Files

`xplace.py`, `dxf_reader.py`, `elementid.py`, `geoengine.py`, `geocompare.py`, `app.py`, `area_statement.py`, `rules.py`, `config.json`, `geo_profiles.json`, `static/geoai.js`; tests: `test_x_placement.py`, `test_element_engine.py`, `test_geoengine.py`, `test_ai_layers_walls.py`, `test_boundary_fixes.py`, `test_h10t2_audit.py`, `test_label_claims.py`, `test_rules.py`, `test_six_fixes.py`, `test_rooms_and_doors.py`, `test_rotation_and_compartments.py`, `test_shaft_core_and_settle.py`, `test_xregions_voids.py`. Backup: `_backup_2026-10-16_before_x_sequence`.

## 2026-10-17 — every door on the inner face of the room it belongs to

Every door belongs to one room, and its line is drawn on that room's inner face of the wall - in Step 3, in the geometry drawing and in the final drawing (`elements.door_owners`, `elements.door_room_rank`).

### The room a door belongs to

Decided once from what stands on the two sides of the opening; the higher rank wins and how big a room is never counts (the old "smaller room" tie-break is gone):

| Rank | Space |
|---|---|
| 5 | a lift well or a shaft (riser, duct, FHC cabinet, a shaft's room) |
| 4 | any other room (toilet, AHU room, store, electrical room, balcony) |
| 3 | a staircase (and its landing) |
| 2 | a lobby, foyer, fire tower, air lock, vestibule |
| 1 | a corridor, passage, walkway, common or open office floor |
| 0 | nothing traced (the common floor), or a void |

Two rooms of one rank: the one this is the only door of; else the side the door is drawn on (its swing); else the one with fewer doors; else not settled and `door_line`'s face stands, as before. A door with the same room on both sides (a WC cubicle's board door, a room traced through its own door) keeps `door_line`'s face. The swing is used only between two like rooms: a shaft, electrical room or AHU room whose door opens out into the corridor keeps its door on its own face.

### No room in a doorway

- Every room is cut back at the door openings it reaches into (`elements.rooms_out_of_doorways`), in Step 3 and in the final drawing: a room stops at its own face of the wall, the opening is the door's. A room that was only the doorway is dropped (under 0.05 sq.m left). Not a cubicle's board door, not a room on both sides of its door, not an X of the X sequence (nor, in the final drawing, Step 3's shafts, lifts, windows, cut-outs, columns and voids).
- A floor-finish note is never a room's name: `10MM DROP IN FINISH`, `nnMM DROP / SUNK / SUNKEN / STEP / DEPRESSION`, `... DROP IN FINISH / FLOOR / FFL / LEVEL / LVL` (config `label_filters.ignore_patterns`, and the defaults in `labels.py`).

### One decision

The final drawing takes the face Step 3 gave the same door (`dxf_reader.door_hints`; `app._text_side_shapes` now also reads Step 3's doors); a door only it has follows the same rule. Before, 2 final doors (Centaurus, XA) stood on the other face from Step 3; now none. A door's XDATA sides are stored with the room it belongs to first. The run report counts `doors_owner_rank / only_door / swing / fewer_doors / same_room_both_sides / no_room_either_side / undecided`, `rooms_cut_at_doorways(_sqm)`, `rooms_in_doorways_dropped`. Optional config keys (`wall_output`): `door_owner` (on), `door_owner_stair_regex`, `door_owner_lobby_regex`, `door_owner_circulation_regex`, `doorway_room_keep_sqm` (0.05), `doorway_cut_min_mm2` (1000).

### What changed on the four test floors

- **BHUB 1st** - 7 door lines moved: the two staircase doors off the fire towers (now on the staircase face), the two balcony doors off the lift lobbies (balcony face), the two female toilet entrances (toilet face, not the corridor), FP-SH-02's door (the shaft's face). The seven `10MM DROP IN FINISH` thresholds are no longer rooms. FP-SH-02's room 0.72 -> 0.54 (shaft room -0.18).
- **Centaurus 11th** - 17 moved: 4 AHU room doors, 4 staircase doors (2 from the fire tower lobbies), 3 PHE shaft doors inside janitor rooms (the shaft's face), STP EX., 2 FHC cabinets and a shaft compartment, a fire tower lobby, a balcony. Statement unchanged.
- **P285 11th** - 6 moved: the lift lobby, ST-02 and LIFT ELE. PANELS off the common floor, ST-01 off its lobby, 2 PHE shafts inside JAN rooms. Statement unchanged.
- **XA 37th** - 8 moved: VRF ODU (2), WATER COOLED VRF ODU, MALE TOILETS, the staircase off the fire lift lobby, the two fire lift lobbies off the office floor, ELECTRICAL SH. ELECTRICAL SH's room 1.65 -> 1.39 (shaft room -0.26), WATER COOLED VRF ODU -0.10, LIFT LOBBY -0.01.

### Files

`elements.py`, `dxf_reader.py`, `app.py`, `geoengine.py`, `labels.py`, `config.json`; tests: `test_door_owner.py` (new, 20 tests). Backup: `_backup_2026-10-17_before_door_faces`.

## 2026-10-17 (later) — the element pipeline: walls first, every X decided before the rooms, what is decided is locked

Step 3 identifies each kind of element once, in a fixed order, and locks it (rule `element_pipeline`, on by default; `locked.py`). Every wall built after a lock skips what the mask holds. Validated on BHUB 1st and Aquila 2nd against the text-first order: the same 81 / 43 X marks class for class, the same doors, walls, rooms and area statement figures; 813 tests (16 new in `tests/test_element_pipeline.py`).

### The order (rule `element_pipeline`)

1. **Clean linework, never-wall evidence** - as before (annotation, other-level XREF, hanging walls, door leaves, fixtures), plus the X marks' own frames held out of the face pairing (`x_frames_not_walls`).
2. **Walls, first pass** - `_detect_walls` (the walls rooms are traced against) and walltrace (the walls drawn), with the mask of what is already known: the X marks' boxes and the columns. `walltrace.trace_floor(skip=, locked=)`: a line that is an X's diagonal or frame is never a face; a hatch inside an X's box or a column is that element's, not wall material; a line drawn wholly inside a locked region is no candidate (BHUB: 509 frame lines skipped, 267 lines and hatch pieces inside locked boxes left out).
3. **The X marks** - decided right after the labels are paired with them (Phase A) and **before any room is traced from a label** (`run_x_sequence`, the same `xplace.resolve_x`). An X stands "in a room" when the space traced from a point `in_space_probe_mm` outside one of its sides closes - between `in_space_min_sqm` and `in_space_max_sqm`, no more than `in_space_plate_share` of the plate (the plate's edge closes every trace; the open floor is no room) - and holds the X (inside its outline, or in a notch within `in_room_reach_mm` inside its convex hull). A probe that lands in the room beyond the X's wall finds that room, not the X's place (BHUB: the sleeve under the electrical room's door is outside the room, a cut-out as before). A stair's well has a flight on either side (`elementid.between_flights` on the X buffered `well_reach_mm`). The trace is asked only where it decides anything (step 5: a loose cut-out, or one with a cut-out tag). Each X locks its class: `x_shaft`, `x_lift`, `x_void`, `x_cutout_loose`, `x_cutout_in_wall`, `x_window`, `x_column`.
4. **Rooms** - Phase B as before, with the X marks' outlines as walls to every trace (unchanged) and the windows, cut-outs and columns already out of the room list.
5. **Walls, second pass (AI_WALL)** - `wall_polygons(locked=)`: a strip mostly inside a locked cell goes, and the mass is cut back round every shaft opening, lift well, void and loose cut-out *before* the runs are formed - a reclaimed piece never comes back as wall (the old `x_box_not_wall` cut after the runs still runs and now finds nothing to cut). The balcony pieces take the same mask; walltrace, when it runs here, takes the decided mask.
6. Everything after (shaft compartments, shaft rooms, door kinds, rooms and doors, wall gaps, settle, drawing) as before.

Off: the text-first order - the rooms traced first and every X decided on them, the walls drawn and cut back after. The two orders give the same answer on the test floors; the pipeline is the order new work builds on.

### Also

- **Column finder** (`geoengine._column_shapes`, Step 3 reads the same): every outer ring of a hatch is a shape of its own (one hatch entity draws every column of a core, or several shear walls; only its largest ring was read - Centaurus S3 lost the 1.70 sq.m corner block and the 1.25 sq.m band under the lift). A hollow shape (C / L / U) on a column layer, a wall's thickness (<= `rcc_wall_max_mm`) and length (>= `rcc_wall_min_len_mm`), is a wall whatever layer the floor's columns stand on - tested before its size (the 3.79 sq.m U round Centaurus' fire lift was dropped as a hollow "column" because its layer was not the grid's).
- **The geometry engine** locks the X marks' boxes and the columns before its own walltrace, as Step 3 does (`geoengine.LOCK_WALLS`: None reads the rule, True / False for an A/B). BHUB and Aquila: walls and doors identical with the lock on and off.
- **The Step 3 page sends only the families the user changed** (`static/main.js` runGenerate), and the server drops any family equal to the extraction's suggestion (`app.annotate`). Before, every row's suggestion was sent and read as a hand pick, so the rules that refile a boundary after the rooms and doors are settled (a shaft's room to AI_MEP, a label-traced lift on a machine room) never ran on a labelled boundary from the page: Centaurus from the page traced 39 shaft compartments and 0 shaft rooms where the same code with no payload traced 99 and 14. The page now gives what the bench gives.
- **Timings**: every stage of the floor pass and of `annotate_dxf` is timed (`stats["timings"]`, seconds; the QA report's run data). BHUB 1st whole Generate 78.8 s (old code) -> 59.0 s (pipeline) / 62.1 s (rule off); Aquila 2nd 183 s -> 120 s / 128 s. The runs shared the machine, so take the ranking, not the figures; the early X decision costs 1-2 s on these floors.
- `locked.py` is in the floor-pass cache stamp; a run's locks are in the run report (`locked_by_floor`: count and area per kind).

### What is still text-first (not in this round)

Phase B still traces rooms from their labels (no "trace every space, then name it"); the context classes (lobby kind, machine room, refuge, balcony face) are unchanged; stairs by treads still run after Phase B (the early well test reads the treads directly); the final drawing still re-settles the geometry engine's walls with the shipped rules. The X decision no longer depends on any of them.

### Files

New: `Drawing_Analyzer/locked.py`, `tests/test_element_pipeline.py`. Changed: `dxf_reader.py`, `walltrace.py`, `geoengine.py`, `rules.py`, `config.json` (`x_placement.in_space_*`, `well_reach_mm`), `app.py`, `static/main.js`. Backup: `_backup_2026-10-17_before_element_pipeline`. Outputs: `Final_Outputs_2026-10-17_ElementPipeline`.

## 2026-10-17 (later) — every door line on its room's inner wall trace

A door's line is now the room's inner wall trace across the opening, read from the walls either side of it (the jambs) - not a face of the opening found, which is often only the door frame (100 mm in a 200 mm wall: XA, Centaurus) or drawn behind a shaft's front (P285 TEN / COMM / FAPA). The room a door belongs to is decided as before (`door_owners`); only where its line stands changes (`elements.door_trace`).

### The rule

- At each jamb, the end of the wall there and its **inner edge** - the corner on the room's side; the line joins the two. Read across the opening, from its middle into the room: the free width between the jambs' walls (walls, columns, and cut-outs / windows / glass set in the wall line, never what lies over the door itself) until it first opens wider than the opening - where a jamb's wall ends and the room begins: the room's face of the wall the door is set in, or of the nibs its frame stands between.
- **Side walls that do not run on past the opening on either side** (a shaft whose whole front is its door): the jambs' walls run on into the room, so the line is at the inner edge of their ends, where the frame stands, and the room runs to it.
- The two jambs' walls ending more than 20 mm apart: the line at the nearer end, square to the opening, listed for review (`door_trace_review`).
- A wall drawn over the opening or straight behind it, or no wall at a jamb: no trace - the line stays on the room's face of the opening, listed for review.

### The rooms squared up to the line

`elements.rooms_to_door_lines`, in Step 3 and the final drawing: the room a door belongs to runs out to its line (a shaft traced to the opening's back face is taken out to its front) or back to it (a room that ran on into the doorway up to the frame); no other room crosses the line. Step 3's X marks and, in the final drawing, its shafts, lifts, windows, cut-outs, columns and voids are left as they are.

### One line everywhere

Step 3 stores the way into each door's room on the door (XDATA `dir:`). The final drawing draws every door that is one of Step 3's on Step 3's line and side (`dxf_reader.door_hints` / `door_plan`), and squares its rooms up to those lines before drawing them (`app._final_door_plans`); a door only it has follows the same rule on its own walls. The geometry drawing uses the same rule (`draw_doors_by_level(trace_solid=...)`).

Run report: `doors_trace_room_face`, `doors_trace_wall_ends`, `doors_trace_jambs_apart`, `doors_trace_none_<why>`, `rooms_out_to_door_lines_sqm`, `rooms_back_to_door_lines_sqm`, `door_trace_review` (floor, place, why). Optional config keys (`wall_output`): `door_trace` (on), `door_trace_reach_mm` (400), `door_trace_widen_mm` (30), `door_trace_along_mm` (1500), `door_trace_step_mm` (2), `door_trace_behind_mm` (100), `door_trace_jog_mm` (20).

### What changed on the four test floors

- **BHUB 1st** - 4 of 60 door lines moved (15 mm, onto the staircase and fire tower faces: the opening found was 185 deep in a 200 mm wall). Statement unchanged. 3 doors left on their face (a wall drawn straight behind the opening: the AHU rooms' sleeved wall, H.TOILET).
- **Centaurus 11th** - 41 of 196 moved: 31 into the room (frames set in the wall: AHU rooms, toilets, lift lobbies, staircases, balconies, the RWP compartment's door between its nibs - 50 mm), 10 out to the inner edge of the walls' ends (LCS, CCTV.ELV, COMM/OFC, ELE.CA, FAPA, RWP shafts between side walls; the FHC compartment's door back 73 mm to its wall). Statement: shaft room +1.08 (the side-wall shafts out to their fronts).
- **P285 11th** - 36 of 111 moved: 16 out to the side walls' ends (TEN / COMM / FAPA / FHC shafts: +0.12-0.15 each), 20 into the room. Statement: shaft room +1.98.
- **XA 37th** - 11 of 41 moved: 9 into the room (frames 100-106 mm in 200 mm walls: VRF ODU, fire lift lobbies, toilets, the staircase, ELECTRICAL SH), PHE SH and SPARE SH out to their walls' ends. Statement: shaft room -0.14 (ELECTRICAL SH no longer runs into its doorway), service lobby -0.04, lift lobby +0.01. An X-marked shaft keeps its X outline (the X sequence's); only its door line moves.
- Every door the final drawing shares with Step 3 is drawn on Step 3's line; a door Step 3 settled no room for (a room traced through its own door) draws Step 3's line and squares no room (Centaurus GENTS TOILET 01 kept its 0.38 sq.m).

### Files

`elements.py`, `dxf_reader.py`, `app.py`, `geoengine.py`; tests: `test_door_trace.py` (new, 13 tests), `test_door_owner.py`. Merged onto the element pipeline change of the same day (no conflicts). Backup: `_backup_2026-10-17_before_door_trace`.

## 2026-10-17 (later) — a room boundary stands only on evidence; doors on AI_P_DOORS / AI_S_DOORS

Every side of a room lies on a **wall**, the **facade**, **another room already identified**, a **primary door line** or a **column** - nothing else (rule `boundary_on_evidence`, on by default). Decided with the user from the BHUB / P285 simulation of 2026-10-17.

### Doors: primary and secondary, new meaning and new layers

| Layer | Meaning | Stops a room |
|---|---|---|
| `AI_P_DOORS` | a door between two different spaces - two rooms, or a room and the common floor; a lift's and a shaft's door included | yes |
| `AI_S_DOORS` | a door inside one room - a WC cubicle's, or a door with the same room on both sides | no |

Before, primary meant "entered from the main accessible area" and secondary "reached only through another room" (a lift's doors were secondary). `elements.door_levels` now decides by the two sides only; the main area still marks `within`. A drawing written before keeps `AI_DOOR_PRIMARY` / `AI_DOOR_SECONDARY`; both are still read as doors (`layernames.OLD_DOOR_PRIMARY` / `OLD_DOOR_SECONDARY`, `is_door_layer`, `door_layer_level`, the Step 3 readback in `app._text_side_shapes`).

### The rule (`boundary_on_evidence`)

- **Facade = the glass where glass is drawn**: the glass lines within `boundary.facade_glass_band_mm` (1500) of the plate's edge are walls to every room trace (`facade_glass_segs`); the plate's edge stays the last stop.
- **Never used while it is on**: the plain lines (`lines_last_resort`), the four-ray box and the corridor box, the overspill box, a box after an engulfing trace, the stated-size rectangle (`_fit_stated`), an outline opened by a width (`_checked_area`), the box round two merged rooms. A label no evidence closes is not drawn: text status **`no_evidence`** (Step 3 table, Extraction QA, validation), stat `no_evidence_boundaries`.
- **The check after drawing** (`boundary_evidence_check`): every side of every room drawn is read back against AI_WALL (and windows / cut-outs set in the wall, and a wall finish up to `boundary.evidence_wall_finish_mm` = 120 off its face), AI_COLUMN (and a casing up to `evidence_column_casing_mm` = 150), the plate's edge, AI_GLASS / AI_WINDOW, AI_P_DOORS (the line and its opening across the wall), the other rooms, AI_VOID / AI_CUTOUT and the lines closing a lobby at its bank - within `evidence_tol_mm` (30). A stretch on none of these, of `evidence_min_run_mm` (100) or more, is marked on **AI_REVIEW** ("boundary on no evidence (layer, length)"); one no longer than `evidence_opening_max_mm` (1000) with both ends on evidence is an opening line (counted as a door - the door definition's), not marked. The X sequence's shafts, lifts and voids are not checked: an X is the evidence. Run report: `boundary_evidence_by_floor` (checked / evidence / opening / no evidence, metres; rooms off), `boundary_evidence_review` (floor, layer, length, place), `boundary_no_evidence_marked`.

### Lifts (`lift_without_door_mechanical`)

Every room of the Lift family - an X lift or one traced from its label, a well passing through included - is looked at for a gap in its walls (`elements.lift_door_quads`); a gap is its door and a door line is drawn across it (AI_P_DOORS). With `open_front`, a lift with no such gap whose whole front is open (no wall along it, at least two other sides walled, the floor beyond - not another lift or shaft) has that front as its door. A lift with no gap and no door is no lift stopping on this floor: family **Mechanical Room**, layer **AI_MECHANICALROOM**, filed under MACHINE ROOM in the statement (`area_statement`). A family picked in Step 3 is never changed. New family in `family_rules.json` (keywords MECHANICAL ROOM, MECH. ROOM, MECH ROOM - labels that were Unknown before).

### Lift lobbies (`lift_lobby_to_lift_bank`)

A lift lobby that walls, doors and lifts do not close (an end open to the corridor) is closed across its open end at the outer limit of its lift bank - the lifts within `boundary.lift_bank_reach_mm` (9000) of its label, and the walls, shafts, lifts and columns running on from them up to `lift_bank_wall_mm` (3000); of the bank along x or along y, the one that closes the lobby on its own, the smaller. Stat `lift_lobby_closed_at_bank`; the lines are evidence to the check.

### What changed on the four test floors (Step 3, previous code -> now)

- **BHUB 1st** - doors P/S 29/31 -> 47/13. The upper zone lift lobby, open at its east end, ends at its lift bank (38.98 -> 38.80, lift lobby -0.18). 50 marks, all on the two SLOPE rooms (the facade fins - the SLOPE sub-rule is to come).
- **P285 11th** - doors 55/56 -> 67/44. The eight LIFT WELL BELOW have no gap and no door: AI_MECHANICALROOM (lift shaft -50.00, machine room +50.00). No marks.
- **Centaurus 11th** - doors 53/144 -> 109/89. FIRE TOWER/SERVICE LOBBY-03 -0.57. 30 marks: toilets stopping on cubicle boards and finish lines, AHU rooms at their double doors, balconies on a facade line that is not the glass, service lobbies.
- **XA 37th** - doors 21/20 -> 35/8. Three PASSENGER LIFT with no opening on this floor -> mechanical room (lift shaft -22.62, machine room +22.62). OFFICE SPACE runs to the glass and takes PROPOSED SERVER AREA, drawn only with plain lines (724.83 -> 912.81). The staircase and VRF ODU are one space in the drawing (the band between them is not a wall and stops short): the staircase label now takes it, where VRF ODU's did (staircase +66.71) - a case to handle. 23 marks.
- Labels with no boundary are the same labels as before on all four (now "no evidence").
- Run time unchanged (P285 180 s against 183 s); the check costs about 4 s a floor.

### Files

`layernames.py`, `elements.py` (door_levels, lift_door_quads open_front), `dxf_reader.py` (facade_glass_segs, boundary_evidence_check, the evidence gates in the leak ladder, lobby_at_lift_bank, rooms_and_doors step 5, the check after drawing), `rules.py`, `area_statement.py`, `family_rules.json` (Mechanical Room), `app.py` (Step 3 readback of both door layer names), `config.json` (notes), `quantities.py`, `validate.py`, `static/main.js`, `static/quantities.js`, `templates/index.html`; tests: `test_boundary_evidence.py` (new, 13 tests), `test_rooms_and_door_levels.py`, `test_priority1.py`, `test_element_engine.py`, `test_rooms_and_doors.py`, `test_rotation_and_compartments.py`. 845 tests pass. Backup: `_backup_2026-10-17_before_boundary_rule`. Outputs: `Final_Outputs_2026-10-17_BoundaryEvidence`.

## 2026-10-17 (later still) — the rooms are the enclosed spaces, found with no text; the text is read last

Rule **`rooms_from_spaces`** (group shape, on by default; needs `element_pipeline`). The user's order, implemented in Step 3's floor pass between Phase A (labels to X marks, the X decision) and Phase B: walls → X marks → doors → **every enclosed space traced with no text** → staircases → lift-bank closure → **text applied last** → shaft rooms → the rest. Phase B stays as the fallback: a label no space took, and a space holding two rooms' names (its trace ran through a doorless opening - Phase B's leak search splits it from the labels, as before).

### The stage (`dxf_reader._floor_pass_rules`, `spaces.py` new)

1. **Shaft compartments first.** `shaft_compartments` runs on the X items before any room (`_shaft_tracer_on(items)`, shared with pass 10, which then skips the X marks seen). A compartment grown past its X is a wall to the rooms and never wall material (`LockedMask` "x_shaft").
2. **The free floor cut into its pieces** (`spaces.free_pieces`): every wall line, door closure and decided outline drawn into a 50 mm grid and sealed by one cell; the plate outside, the X cells, voids, loose cut-outs, columns and the Geometry tab's shapes occupied; the pieces labelled. A piece of `open_floor_sqm` (1000) or more, or holding `open_floor_share` (0.5) of the free floor, is the open floor; one narrower than 2 x `min_half_width_mm` (140) is a wall cavity or a doorway strip; one under `min_sqm` (0.3) is nothing. One seed per piece, at its widest point. The flights' tread lines (walls to the room search when drawn on a structure layer) are left out of the grid, and each flight is traced first from its middle on the walls less its treads.
3. **One trace per piece** (`spaces.trace_pieces`): on the walls and the doors found; if that leaks, with the doorless openings closed; last on the plain lines - never under rule `boundary_on_evidence`. Duplicates and slivers dropped.
4. **Lift-bank lobbies** (`spaces.lift_bank_lobbies`, rule `lift_lobby_to_lift_bank`): two banks (lifts within 300 mm of each other) facing across 2-8 m, their extents overlapping by half, hold the floor between them less the walls, the doors found and the spaces traced; a lobby name standing in it is traced on Phase B's bank lines (`lobby_at_lift_bank`, which now also knows lifts named by their text), else the floor between the banks is the lobby and its end lines are evidence to the check. Run once with the X lifts, and again after Phase B with the lifts the labels traced (a bank with no X on its lifts - Aquila).
5. **Names, last.** Only the texts standing inside a space are read - never one outside, no threshold. Tags, notes, ignore words (library `ignore_words`: SLOPE) and a text of no family that does not read as a room's name (a level note: "FFL +590.59 SSL +589.94") name nothing. **Staircase** = a flight of `stair_min_treads` (7) or more with UP / DOWN / DN (library `evidence_words`, collected from the drawing as marks - they are not labels) or a stair name in it; a named landing with its flight in the cell beside it (within `stair_part_reach_mm`), and that flight as the other part of it. A stair name with no flight in or beside its space is no name (the space is drawn unnamed). **Shaft room**: a space of `shaft_room_max_sqm` (now **10**) or less along a shaft's X with a door of its own is the shaft's room - **always AI_MEP** (`shaft_room_family`), whatever is written in it, named by the shaft-like text inside or by the X's label. A shaft-like text in a space over 10 sq.m is a note about the shaft; in a small space with no door or no X beside it, it names a Shaft. A space holding two rooms' names is **left to Phase B**. Otherwise the best text (a room's name first, then one whose stated size fits, then a known family, then `label_score`) names it. **Nothing written in it**: a pocket (under `pocket_max_sqm` 1.5 with no door, or under `edge_pocket_max_sqm` 6 along the plate's edge) is not drawn; else the space is drawn as `unnamed_label` ("ROOM") on the Unknown family's layer (the later geometry pass still names it from what is drawn in it; one inside the wall mass W2 draws is dropped by the rooms-in-walls rule).
6. **Area** is the traced polygon's (holes deducted), never a stated size or a box.

A text wrapped in parentheses - "(EMERGENCY EXIT)" - is now a remark (`labels.label_tier`, tier LONG), not a room's name: it had counted as another room's name standing in the lift lobby between two banks.

Run report: `spaces_pieces`, `spaces_open_floor_sqm`, `spaces_cavities`, `spaces_flights_traced`, `spaces_traced`, `spaces_named`, `spaces_unnamed`, `spaces_pockets`, `spaces_contested`, `spaces_staircases`, `spaces_shaft_rooms`, `spaces_shaft_by_text`, `spaces_lift_lobbies` (+ `_after_labels`), `spaces_compartments_as_walls`, `spaces_tread_lines_out`, `spaces_stair_name_no_flight`, `spaces_in_tab_shapes`; **`spaces_by_floor`**: every space with its fate (drawn / left to the labels / pocket / dropped by a later pass), label, family, sq.m, how it was traced, why it was named so and the texts in it. Timings: `pass 05a shaft compartments (before the rooms)`, `pass 05b spaces traced and named (text last)`.

Library and config: `family_rules.json` `ignore_words` ["SLOPE"], Staircase `evidence_words` ["UP", "DOWN", "DN"], Shaft keywords + KITCHEN EXHAUST, LCS, STAIR PRE, STAIR PRES; `config.json` `spaces` (grid_mm, open_floor_sqm, open_floor_share, min_half_width_mm, min_sqm, pocket_max_sqm, edge_pocket_max_sqm, lobby_gap_mm, lobby_min_sqm, draw_unnamed, unnamed_label), `rooms_and_doors.shaft_room_max_sqm` 12 → 10; `labels.build_rules(..., ignore_words=)`.

### What changed on the two test floors (today's code, rule off → rule on; `Final_Outputs_2026-10-17_RoomsFromSpaces`)

- **BHUB 1st** - 30 spaces: 15 named by their texts, 6 shaft rooms → AI_MEP (FP-SH-01/02/03, ELV-SH-01, PHE-SHAFT x2), 2 staircases (24 treads and UP / DOWN in each), the upper-zone lift lobby closed at its bank (38.80), 3 left to the labels (the lower-zone lift lobby, FEMALE TOILET, ELE. ROOM - two names in one trace), 7 pockets, 9 with nothing written in them (the two 16 sq.m fin bays the plain lines used to call SLOPE are drawn as ROOM; 7 lay inside the wall mass or under an X cell and were dropped). Statement identical to the old order (AHU +0.08, toilets +0.07); the old order's room named "FFL +590.59 SSL +589.94" is ROOM. 81/81 X marks, 60 doors, walls identical.
- **Aquila 2nd** - 30 spaces: 21 named, 6 shaft rooms → AI_MEP (FHR x3, LCS, P.H.E SHAFT x2 - the second was AI_SHAFT before: shaft room +1.53, and two Unknown Shaft X marks in it take its name: elect shaft +1.09, unnamed opening -1.09), 3 staircases (ST-01/02/03: 24 treads and a stair name; ST-01 33.76 → 33.53), 3 left to the labels (AHU ROOM x2 holding STAIR PRES, the left lift bank with its lobby), 12 pockets, 5 unnamed (ROOM 1.72 beside each FIRE/SER. LIFT - probably its door zone). A 24PAX LIFT the old order made a machine room is a lift again (lift shaft 47.03 → 54.05, machine room 13.85 → 7.26). 43/43 X marks, 47 doors, walls identical. **PASSENGER LIFT LOBBY is back (50.20, rule on or off)**: the two rounds merged since this morning had lost it (`no_evidence`) because "(EMERGENCY EXIT)" counted as another room's name in its trace; as a remark it no longer does, and the labels' trace closes the lobby on its walls (the morning's 25.14 stopped on a plain line that is no evidence).
- Against the ElementPipeline delivery of this morning, Aquila also shows what the two rounds merged since (door trace, boundary on evidence) did, with or without this rule: AHU ROOM 24.91 → 29.53 (the STAIR PRES duct strip beside it is separated only by a plain line), the two FIRE/SER. LIFTs → machine room (no door found), the left bank's 24PAX LIFTs 5.98 → 7.02 (to the lobby face of their front wall; the plain car line no longer closes them), STAIR PRES 1.21 staircase → shaft room (library). `before_vs_now.txt` in the output folder lists A (that delivery), B (today, rule off) and C (today, rule on) side by side.
- Open: a shaft-like name in a room cut off only by a plain line (STAIR PRES 4.34 inside AHU ROOM); the FIRE/SER lift's door (its 1.72 sq.m door strip is an unnamed space); the left bank's lifts to the inner face of their front wall; lifts without an X merging with their lobby (to solve later, as agreed).
- Run time: the stage costs 3-4 s a floor (pass 05a 0.9-1.0 s, pass 05b 2.3-2.7 s) and takes 1-3 s off Phase B and the later compartments pass - unchanged within the machine's noise (Aquila's floor passes 61 → 50 s; BHUB's 33 → 62 s in runs where W1, which this change does not touch, went 4.5 → 24.2 s: load, not the change).

### Files

`spaces.py` (new), `dxf_reader.py` (the stage, `_collect_stair_marks`, `_shaft_tracer_on`, `lobby_at_lift_bank` lifts by text, pass 07 skips flights in a traced space, `spaces_by_floor`), `labels.py` (ignore words, the remark tier), `rules.py` (`rooms_from_spaces`), `family_rules.json`, `config.json`; tests: `test_spaces.py` (new, 16 tests), `test_walls.py` (two stair tags with no flight: both absorbed). 861 tests pass. Outputs: `Final_Outputs_2026-10-17_RoomsFromSpaces`. Backup: `_backup_2026-10-17_rooms_from_spaces_delivered` - the code as delivered; no copy was taken before this change was edited in (the previous rollback point is `_backup_2026-10-17_before_boundary_rule`; the rule off gives the old order on today's code).
