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
