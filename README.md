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

## Tests

```powershell
python -m unittest discover -s tests -v
```

Settings live in `Drawing_Analyzer/config.json` and `Drawing_Analyzer/family_rules.json`.
