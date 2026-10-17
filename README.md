# Drawing Sanitizer (Drawing Analyzer)

A Flask web app that reads AutoCAD DXF floor plans, draws room boundaries, and produces
area reports.

## Run

```powershell
pip install -r requirements.txt
python runserver.py
```

Then open http://127.0.0.1:5555.

## Workflow

1. **Upload DXF.** Enter an optional project name and the floor plate area range, then click **Run**.
2. **Select floor boundaries.** Tick the floor plates you want. Names are suggested from the plan
   titles, and a CAD viewer shows every plate on the drawing.
3. **Review extracted texts.** Search the texts, adjust their families, then click **Generate
   Boundaries**. The annotated DXF has solid hatches in each family's colour.
4. **Calculate areas.** Upload the corrected DXF to get room areas, quantities, and the tower-wise
   **Area Statement** workbook.

Also available: compare two revisions, the drawing layer inventory, family rules, and built-in rules.

What the boundaries pass does with the drawing (2026-10-15):

- A core or wing drawn at an angle is worked out in a copy of the drawing turned to the axes
  (`rotation.py`, rule `rotated_cores`) and the results are turned back - rotated elements are drawn rotated.
- A shaft is the compartment its X stands in, traced from the X on the walls, columns, doors and
  partition lines (rule `shaft_compartment`); an X across two compartments gives one shaft each;
  a trace that fails leaves the X as drawn, flagged on AI_REVIEW.
- Doors are drawn as one line on the inner face of the wall of the room they serve.
- An X between two stair flights is the stair's well (a hole), never a shaft.
- A window never cuts a wall: the wall band runs on through it (`window_bridge_mm`).
- The Area Statement workbook is a single formula-driven sheet (statement, quick lookup, area
  register, by-layer table, working data).

## Tests

```powershell
python -m unittest discover -s tests -v
```

Settings live in `Drawing_Analyzer/config.json` and `Drawing_Analyzer/family_rules.json`.
