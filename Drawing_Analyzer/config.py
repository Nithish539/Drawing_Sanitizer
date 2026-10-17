# -*- coding: utf-8 -*-
"""Loader for config.json.

Every tuned threshold used to live as a module constant in dxf_reader.py,
which meant retuning for a new client's drawing conventions required
editing Python. They now live in config.json, in the same spirit as
family_rules.json. Missing keys fall back to the built-in defaults below,
so a partial or absent config file is safe.
"""

from __future__ import annotations

import json
import os

DEFAULTS = {
    "floor_detection": {
        "min_floor_area_sqm": 2000.0,
        "excluded_layers": ["A-ELEV SYMBOL", "ROOM_LABEL"],
    },
    "boundary": {
        "max_hv_search_mm": 20000.0,
        "max_room_sqm": 1000.0,
        "min_boundary_side_mm": 200.0,
        "overlap_merge_ratio": 0.70,
        "method": "trace",
        "trace_cell_mm": 50.0,
        "trace_simplify_cells": 1.2,
        "thick_lineweight_threshold": 40,
        "wall_axis_ratio": 6.0,
        "spatial_index_band_mm": 2000.0,
    },
    "doors": {
        "insert_radius_mm": 1500.0,
        "gap_min_mm": 300.0,
        "gap_max_mm": 2000.0,
    },
    "x_marks": {
        "diag_min_len_mm": 80.0,
        "diag_max_len_mm": 5000.0,
        "diag_min_axis_mm": 60.0,
        "pair_midpoint_tol_mm": 100.0,
        "pair_length_tol": 0.25,
        "cluster_gap_mm": 450.0,
        "text_radius_mm": 2500.0,
        "enclosure_max_sqm": 20.0,
        "enclosure_min_ratio": 0.25,
        "fallback_min_sqm": 0.5,
    },
    "curves": {
        "sagitta_tolerance_mm": 5.0,
        "max_segments_per_arc": 64,
    },
    "annotation": {
        "layer": "AI_FLOOR",
        "color": 2,
        "text_height_mm": 150.0,
        "block_recursion_depth": 3,
        "hatch": True,
        "hatch_transparency": 0.0,
    },
    "label_filters": {
        "device_tag_patterns": None,     # None -> use the built-ins
        "ignore_patterns": None,
    },
    "quantities": {
        "core_families": ["Shaft", "Lift", "Staircase"],
        "deck_sheet_deduct_families": ["Shaft", "Lift", "Staircase"],
        "studs_per_sqm": 9.0,
        "duplicate_area_tolerance_sqm": 0.02,
        "overlap_flag_ratio": 0.10,
        "sliver_max_sqm": 0.5,
        "sliver_max_aspect": 12.0,
    },
    "linear_quantities": {"disciplines": []},
    "block_inventory": {"categories": []},
}


def _merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None):
    if path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "config.json")
    raw = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh) or {}
        except (OSError, ValueError):
            raw = {}
    return _merge(DEFAULTS, raw)


CONFIG = load_config()
