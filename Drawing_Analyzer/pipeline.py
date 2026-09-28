# -*- coding: utf-8 -*-
"""Orchestration.

Each endpoint used to call ezdxf.readfile independently, so a single job
parsed the same 35-40 MB drawing several times over. Step 4 now parses
once and drives every derived output from that one open document.
"""

from __future__ import annotations

from typing import Dict

from .config import CONFIG
from .dxf_reader import compute_room_areas, list_layers_with_closed_polylines
from .dxfio import load_document_ex
from .quantities import build_quantity_report, extraction_qa


def analyse_areas(dxf_path: str, floor_layer: str, unit_override=None) -> Dict:
    """Full step-4 analysis from one parse of the drawing.

    Returns a dict with the area result plus every derived report that
    needs no user input.
    """
    doc, msp, units, info = load_document_ex(dxf_path, unit_override)
    loaded = (doc, msp, units)

    area_data, room_polys = compute_room_areas(
        dxf_path, floor_layer, unit_override=unit_override,
        return_polygons=True, loaded=loaded)

    return {
        "areas": area_data,
        "quantities": build_quantity_report(area_data, room_polys, CONFIG),
        "units": units.to_ui(),
        "unit_info": info,
    }


def list_layers(dxf_path: str, unit_override=None, loaded_out=None):
    """Layers holding closed polylines, plus the resolved units.

    Uses the same unit resolution as the step-1 upload, so a corrected
    DXF dropped straight into step 4 cannot be read differently from the
    file it came from.
    """
    doc, msp, units, info = load_document_ex(dxf_path, unit_override)
    if loaded_out is not None:
        loaded_out["loaded"] = (doc, msp, units)
    layers, _ = list_layers_with_closed_polylines(dxf_path,
                                                  loaded=(doc, msp, units))
    return layers, units, info


def build_qa(stats: Dict, results: Dict) -> Dict:
    return extraction_qa(stats, results)
