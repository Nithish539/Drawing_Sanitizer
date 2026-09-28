# -*- coding: utf-8 -*-
"""Drawing-unit detection and normalisation.

The engine works internally in millimetres.

The naive approach - trust $INSUNITS - does not survive contact with real
drawings. The file this was rebuilt against declares $INSUNITS = 6
(Metres) while actually being drawn in millimetres, so trusting the
header inflated every area by a million. Nor can the whole drawing's
bounding box be used to sanity-check it: that file spans 1,224,573 units
because of content parked far from the plan, while the building itself
is only about 72 m across.

What does work is the shape of the size distribution. Across the closed
polylines in a floor plan, the median one is a small element (a fraction
of a metre to a few metres) and the 95th percentile is a room or a wing
(a few metres to a couple of hundred). Those two figures together
identify the unit unambiguously, and are immune both to a wrong header
and to a handful of far-flung outliers:

    that file, read as mm:  median 1.9 m,  p95 24.3 m   -> both sane
                    as cm:  median 19.1 m, p95 243 m    -> neither
                     as m:  median 1912 m, p95 24310 m  -> neither
"""

from __future__ import annotations

# DXF $INSUNITS code -> (label, millimetres per drawing unit)
INSUNITS = {
    0:  ("Unitless", None),
    1:  ("Inches", 25.4),
    2:  ("Feet", 304.8),
    3:  ("Miles", 1_609_344.0),
    4:  ("Millimetres", 1.0),
    5:  ("Centimetres", 10.0),
    6:  ("Metres", 1000.0),
    7:  ("Kilometres", 1_000_000.0),
    8:  ("Microinches", 0.0000254),
    9:  ("Mils", 0.0254),
    10: ("Yards", 914.4),
    11: ("Angstroms", 1e-7),
    12: ("Nanometres", 1e-6),
    13: ("Microns", 0.001),
    14: ("Decimetres", 100.0),
    15: ("Decametres", 10_000.0),
    16: ("Hectometres", 100_000.0),
    17: ("Gigametres", 1e12),
    18: ("Astronomical units", 1.495978707e14),
    19: ("Light years", 9.4607304725808e18),
    20: ("Parsecs", 3.0856775814914e19),
}

DEFAULT_MM_PER_UNIT = 1.0
DEFAULT_LABEL = "Millimetres (assumed)"

# Plausible size band for the median closed polyline, in metres.
MEDIAN_MIN_M, MEDIAN_MAX_M = 0.25, 12.0
# ...and for the 95th percentile, which is a room or a wing.
P95_MIN_M, P95_MAX_M = 4.0, 300.0

# Preference order on ties. Millimetres first: it is what almost every
# architectural DXF in practice uses.
_INFERENCE_ORDER = [4, 6, 5, 2, 1]


class DrawingUnits:
    """Millimetres-per-drawing-unit, plus how we arrived at it."""

    __slots__ = ("mm_per_unit", "label", "code", "assumed", "overridden",
                 "source", "note")

    def __init__(self, mm_per_unit, label, code=None, assumed=False,
                 overridden=False, source="default", note=None):
        self.mm_per_unit = float(mm_per_unit)
        self.label = label
        self.code = code
        self.assumed = bool(assumed)
        self.overridden = bool(overridden)
        self.source = source          # header | geometry | override | default
        self.note = note

    # -- conversions --------------------------------------------------
    def to_mm(self, value):
        return value * self.mm_per_unit

    def from_mm(self, value_mm):
        return value_mm / self.mm_per_unit

    def point_to_mm(self, x, y):
        return (x * self.mm_per_unit, y * self.mm_per_unit)

    @property
    def is_identity(self):
        return abs(self.mm_per_unit - 1.0) < 1e-12

    @property
    def short_label(self):
        return (self.label or "").split(" (")[0]

    def to_ui(self):
        return {
            "label": self.label,
            "short_label": self.short_label,
            "mm_per_unit": self.mm_per_unit,
            "insunits_code": self.code,
            "assumed": self.assumed,
            "overridden": self.overridden,
            "source": self.source,
            "note": self.note,
        }

    def __repr__(self):
        return "DrawingUnits({0!r}, {1}mm/unit, from {2})".format(
            self.label, self.mm_per_unit, self.source)


def units_from_code(code, overridden=False, source=None, note=None):
    label, mm = INSUNITS.get(int(code), (None, None))
    if mm is None:
        return DrawingUnits(DEFAULT_MM_PER_UNIT, DEFAULT_LABEL, code=code,
                            assumed=True, overridden=overridden,
                            source=source or "default", note=note)
    return DrawingUnits(mm, label, code=code, overridden=overridden,
                        source=source or ("override" if overridden
                                          else "header"), note=note)


# -- statistical inference --------------------------------------------------
def _percentile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    i = q * (len(sorted_vals) - 1)
    lo = int(i)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = i - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def span_stats(spans):
    """Median and 95th percentile of the polyline spans, in drawing units."""
    vals = sorted(s for s in spans if s and s > 0)
    if not vals:
        return None
    return {
        "count": len(vals),
        "median": _percentile(vals, 0.5),
        "p95": _percentile(vals, 0.95),
        "max": vals[-1],
    }


def score_unit(mm_per_unit, stats):
    """How many of the two size criteria this unit satisfies (0, 1 or 2)."""
    if not stats:
        return 0
    med_m = stats["median"] * mm_per_unit / 1000.0
    p95_m = stats["p95"] * mm_per_unit / 1000.0
    score = 0
    if MEDIAN_MIN_M <= med_m <= MEDIAN_MAX_M:
        score += 1
    if P95_MIN_M <= p95_m <= P95_MAX_M:
        score += 1
    return score


def infer_units_from_spans(spans):
    """Identify the drawing unit from the distribution of polyline sizes.

    Returns a DrawingUnits, or None when nothing scores.
    """
    stats = span_stats(spans)
    if not stats:
        return None
    best_code, best_score = None, 0
    for code in _INFERENCE_ORDER:
        _label, mm = INSUNITS[code]
        s = score_unit(mm, stats)
        if s > best_score:               # strict: preference order breaks ties
            best_code, best_score = code, s
    if best_code is None or best_score == 0:
        return None
    label, mm = INSUNITS[best_code]
    u = units_from_code(best_code, source="geometry")
    u.label = label
    u.note = ("Inferred from the drawing: median element {0:.2f} m, "
              "95th percentile {1:.1f} m.".format(
                  stats["median"] * mm / 1000.0, stats["p95"] * mm / 1000.0))
    return u


def resolve_units(header_code, spans, override=None):
    """Settle on a unit from the override, the header and the geometry.

    Precedence:
      1. an explicit override always wins, but is still reported on;
      2. otherwise the geometry decides when it disagrees with the header
         and scores better - a header claiming Metres for a millimetre
         drawing is common enough that it cannot be trusted over the
         evidence;
      3. otherwise the header;
      4. otherwise millimetres.

    Returns (units, info).
    """
    stats = span_stats(spans)
    inferred = infer_units_from_spans(spans)

    if override is not None:
        chosen = _parse_override(override)
        if chosen is not None:
            chosen.overridden = True
            chosen.source = "override"
            return chosen, _info(chosen, stats, inferred, header_code)

    header_units = None
    if header_code:
        _label, mm = INSUNITS.get(int(header_code), (None, None))
        if mm is not None:
            header_units = units_from_code(int(header_code), source="header")

    header_score = score_unit(header_units.mm_per_unit, stats) \
        if (header_units and stats) else -1
    inferred_score = score_unit(inferred.mm_per_unit, stats) \
        if (inferred and stats) else -1

    # The geometry overrules the header only when the header is impossible
    # (scores nothing), not merely when another unit scores better. A
    # header claiming Metres for a millimetre drawing is off by 1000x on
    # both measures and scores 0. But T1_TYPICAL FLOOR PLANS - thirteen
    # floor plates on one sheet, full of small hatch squares - has a median
    # element of 0.20 m in its declared millimetres, just under the band,
    # so centimetres scored 2 against millimetres' 1 and won: every wall
    # was read ten times too thick, and most rooms lost their boundary.
    if header_units and inferred and header_score <= 0 \
            and inferred_score > header_score:
        inferred.note = (
            "The DXF header declares {0}, but at that scale this drawing "
            "would not be building-sized, so the geometry was believed "
            "instead. {1}".format(header_units.short_label,
                                  inferred.note or ""))
        return inferred, _info(inferred, stats, inferred, header_code)

    if header_units and header_score > 0:
        return header_units, _info(header_units, stats, inferred, header_code)

    if inferred:
        return inferred, _info(inferred, stats, inferred, header_code)

    if header_units:
        return header_units, _info(header_units, stats, inferred, header_code)

    fallback = DrawingUnits(DEFAULT_MM_PER_UNIT, DEFAULT_LABEL, code=0,
                            assumed=True, source="default")
    return fallback, _info(fallback, stats, inferred, header_code)


def _info(chosen, stats, inferred, header_code):
    mm = chosen.mm_per_unit
    header_label = None
    if header_code:
        header_label = INSUNITS.get(int(header_code), (None, None))[0]
    ok, message = check_plausibility(chosen, stats)
    return {
        "units": chosen.to_ui(),
        "header_label": header_label,
        "inferred_label": inferred.short_label if inferred else None,
        "median_m": round(stats["median"] * mm / 1000.0, 3) if stats else None,
        "p95_m": round(stats["p95"] * mm / 1000.0, 2) if stats else None,
        "polylines": stats["count"] if stats else 0,
        "plausible": ok,
        "message": message,
    }


def check_plausibility(units, stats):
    """Sanity-check a unit choice against the size distribution.

    Only an outright absurd result is rejected. Everything short of that
    is allowed through with the inference reported, because blocking a
    legitimate but unusual drawing is worse than showing a note.
    """
    if not stats:
        return True, None
    med_m = stats["median"] * units.mm_per_unit / 1000.0
    p95_m = stats["p95"] * units.mm_per_unit / 1000.0
    if med_m > 200.0 or p95_m > 5000.0:
        return False, ("Read as {0}, a typical element in this drawing would "
                       "be {1:,.0f} m and a typical room {2:,.0f} m, which is "
                       "not a building.".format(units.short_label, med_m,
                                                p95_m))
    if med_m < 0.002 or p95_m < 0.05:
        return False, ("Read as {0}, a typical room in this drawing would be "
                       "{1:.3f} m across, which is not a building.".format(
                           units.short_label, p95_m))
    return True, None


# -- override parsing -------------------------------------------------------
_ALIASES = {
    "mm": 4, "millimetre": 4, "millimeter": 4, "millimetres": 4,
    "millimeters": 4,
    "cm": 5, "centimetre": 5, "centimeter": 5, "centimetres": 5,
    "centimeters": 5,
    "m": 6, "metre": 6, "meter": 6, "metres": 6, "meters": 6,
    "in": 1, "inch": 1, "inches": 1, '"': 1,
    "ft": 2, "foot": 2, "feet": 2, "'": 2,
}


def _parse_override(override):
    if isinstance(override, DrawingUnits):
        return override
    if isinstance(override, bool):
        return None
    if isinstance(override, int):
        return units_from_code(override, overridden=True)
    if isinstance(override, float):
        if override > 0:
            return DrawingUnits(override,
                                "Custom ({0:g} mm/unit)".format(override),
                                overridden=True, source="override")
        return None
    if isinstance(override, str):
        key = override.strip().lower()
        if not key or key in ("auto", "auto-detect", "autodetect"):
            return None
        if key in _ALIASES:
            return units_from_code(_ALIASES[key], overridden=True)
        try:
            return units_from_code(int(key), overridden=True)
        except ValueError:
            pass
        try:
            return _parse_override(float(key))
        except ValueError:
            return None
    return None


# -- backwards-compatible helpers ------------------------------------------
def detect_units(doc, override=None):
    """Header-only detection, for callers that have no geometry yet."""
    if override is not None:
        u = _parse_override(override)
        if u is not None:
            return u
    try:
        code = int(doc.header.get("$INSUNITS", 0) or 0)
    except Exception:
        code = 0
    if code:
        _label, mm = INSUNITS.get(code, (None, None))
        if mm is not None:
            return units_from_code(code, source="header")
    return DrawingUnits(DEFAULT_MM_PER_UNIT, DEFAULT_LABEL, code=0,
                        assumed=True, source="default")


def span_in_metres(units, span_drawing_units):
    return (span_drawing_units or 0.0) * units.mm_per_unit / 1000.0
