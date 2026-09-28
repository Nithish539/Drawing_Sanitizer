# -*- coding: utf-8 -*-
"""Shared DXF loading and block traversal.

Two things previously duplicated across dxf_reader.py and fixed at one
level of nesting now live here:

  * loading a document together with its detected drawing units, so that
    every consumer works in millimetres;
  * walking modelspace and descending INSERTs to a configurable depth,
    composing the transform as it goes.

Production drawings routinely nest blocks three or four deep. The old
depth-1 walk simply could not see those entities.

**Block placement now uses ezdxf's own INSERT matrix.** The hand-rolled
offset/scale/rotation transform this replaces got three things wrong, all
of them measured on a real test plate:

  * an INSERT whose extrusion is (0, 0, -1) - how some CAD programs store
    a mirrored block - was placed as if it were not mirrored, throwing
    doors and glazing 300-850 m away from where they belong;
  * a rotated block inside a mirrored one had its rotation added instead
    of reversed, misplacing the inner geometry by up to 1.4 m;
  * the block definition's base point was ignored.

`Insert.matrix44()` accounts for the insertion point, scale, rotation,
extrusion (OCS) and base point, and nested matrices compose exactly.

**Layer 0 inherits.** Geometry drawn on layer "0" inside a block takes the
layer of the INSERT that places it - that is how CAD displays it and how
the office that drew it thinks of it. It used to be reported as "0", so a
WC cubicle block inserted on a furniture layer escaped the furniture
exclusion and its partitions were treated as walls. `effective_layer()`
returns the inherited name.
"""

from __future__ import annotations

import math
from typing import Iterator, Optional, Tuple

import ezdxf
from ezdxf.math import Matrix44, Vec3

from .config import CONFIG
from .units import resolve_units


class Transform:
    """Block-to-world placement, plus what the block path says about it.

    `m` is an ezdxf Matrix44 (None for modelspace, i.e. identity).
    `ix, iy` is the world position of the INSERT point that created this
    transform - the block inventory and door schedule read it.
    `host_layer` is the layer geometry on layer "0" inherits here, and
    `blocks` the names of the blocks on the path from modelspace.
    """

    __slots__ = ("m", "ix", "iy", "host_layer", "blocks")

    def __init__(self, m: Optional[Matrix44] = None, ix=0.0, iy=0.0,
                 host_layer: Optional[str] = None, blocks=()):
        self.m = m
        self.ix, self.iy = float(ix), float(iy)
        self.host_layer = host_layer
        self.blocks = tuple(blocks)

    def apply(self, x, y, z=0.0) -> Tuple[float, float]:
        """World (x, y) of a point given in block coordinates."""
        if self.m is None:
            return (float(x), float(y))
        v = self.m.transform(Vec3(x, y, z))
        return (v.x, v.y)

    def enter(self, ins) -> "Transform":
        """The transform for the contents of INSERT `ins` placed here."""
        own = ins.matrix44()
        m = own if self.m is None else own @ self.m
        try:
            ip = ins.dxf.insert
            wcs = ins.ocs().to_wcs(ip) if _has_ocs(ins) else Vec3(ip)
            ix, iy = self.apply(wcs.x, wcs.y, wcs.z)
        except (AttributeError, TypeError, ValueError):
            ix, iy = self.apply(0.0, 0.0)
        layer = ins.dxf.get("layer", "0") or "0"
        host = self.host_layer if layer == "0" and self.host_layer else layer
        return Transform(m, ix, iy, host, self.blocks + (ins.dxf.name or "",))

    # Kept for callers written against the old class.
    def compose(self, child: "Transform") -> "Transform":
        if child.m is None:
            return self
        m = child.m if self.m is None else child.m @ self.m
        ix, iy = self.apply(child.ix, child.iy)
        return Transform(m, ix, iy, child.host_layer or self.host_layer,
                         self.blocks + child.blocks)

    def _axes(self):
        if self.m is None:
            return (1.0, 0.0), (0.0, 1.0)
        ux = self.m.transform_direction(Vec3(1, 0, 0))
        uy = self.m.transform_direction(Vec3(0, 1, 0))
        return (ux.x, ux.y), (uy.x, uy.y)

    @property
    def rot_deg(self):
        (ax, ay), _ = self._axes()
        return math.degrees(math.atan2(ay, ax)) % 360.0

    @property
    def scale_avg(self):
        (ax, ay), (bx, by) = self._axes()
        return (math.hypot(ax, ay) + math.hypot(bx, by)) / 2.0 or 1.0

    @property
    def mirrored(self):
        """True when the placement flips handedness (a mirrored block)."""
        (ax, ay), (bx, by) = self._axes()
        return ax * by - ay * bx < 0


IDENTITY = Transform()


def _has_ocs(entity) -> bool:
    """Whether the entity's coordinates are in a non-trivial OCS."""
    try:
        ex = entity.dxf.get("extrusion", None)
    except AttributeError:
        return False
    if ex is None:
        return False
    return not (abs(ex[0]) < 1e-12 and abs(ex[1]) < 1e-12 and ex[2] > 0)


def ocs_to_wcs(entity, x, y, z=0.0) -> Tuple[float, float, float]:
    """Container coordinates of a point stored in the entity's OCS."""
    if not _has_ocs(entity):
        return (float(x), float(y), float(z))
    v = entity.ocs().to_wcs(Vec3(x, y, z))
    return (v.x, v.y, v.z)


def effective_layer(entity, xform: Transform = None) -> str:
    """The layer CAD shows this entity on: layer 0 inherits the INSERT's."""
    layer = (entity.dxf.get("layer", "0") or "0")
    if layer == "0" and xform is not None and xform.host_layer:
        return xform.host_layer
    return layer


def insert_transform(ins) -> Transform:
    """Placement of a modelspace INSERT's contents."""
    return IDENTITY.enter(ins)


def polyline_spans(msp):
    """Bounding-box span of every closed polyline, in raw drawing units.

    Feeds the statistical unit inference. Per-entity rather than a single
    drawing-wide extent, because a real file has content parked far from
    the plan: one drawing here spans 1.2 million units while the building
    itself is 72 m across.
    """
    spans = []
    for e in msp.query("LWPOLYLINE POLYLINE"):
        try:
            if e.dxftype() == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in e.get_points()]
            else:
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
        except (AttributeError, TypeError, ValueError):
            continue
        if len(pts) < 3:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        spans.append(max(max(xs) - min(xs), max(ys) - min(ys)))
    return spans


def load_document_ex(path, unit_override=None):
    """Open a DXF and settle its units. Returns (doc, msp, units, info).

    The unit decision weighs an explicit override, the $INSUNITS header
    and the geometry, in that order of authority - the header alone is
    not trustworthy, as a file declaring Metres while drawn in
    millimetres demonstrated.
    """
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    try:
        header_code = int(doc.header.get("$INSUNITS", 0) or 0)
    except Exception:
        header_code = 0
    units, info = resolve_units(header_code, polyline_spans(msp),
                                unit_override)
    return doc, msp, units, info


def load_document(path, unit_override=None):
    """As load_document_ex, without the diagnostics."""
    doc, msp, units, _info = load_document_ex(path, unit_override)
    return doc, msp, units


def drawing_span(msp) -> float:
    """Largest single closed-polyline span, in raw drawing units."""
    spans = polyline_spans(msp)
    return max(spans) if spans else 0.0


def iter_entities(doc, msp, dxftypes: str, max_depth: int = None
                  ) -> Iterator[Tuple[object, Transform, int]]:
    """Yield (entity, world_transform, depth) for modelspace and nested blocks.

    `dxftypes` is an ezdxf query string, e.g. "LINE LWPOLYLINE".
    Cyclic block references are guarded against by tracking the block names
    on the current path. Read the entity's layer through effective_layer()
    so that layer-0 geometry reports the layer its block was placed on.
    """
    if max_depth is None:
        max_depth = int(CONFIG["annotation"].get("block_recursion_depth", 3))

    for e in msp.query(dxftypes):
        yield e, IDENTITY, 0

    def descend(container, xform, depth, seen):
        if depth > max_depth:
            return
        for ins in container.query("INSERT"):
            name = ins.dxf.name
            if not name or name in seen or name not in doc.blocks:
                continue
            try:
                child = xform.enter(ins)
            except (ValueError, ZeroDivisionError, AttributeError):
                continue
            block = doc.blocks[name]
            for e in block.query(dxftypes):
                yield e, child, depth
            for item in descend(block, child, depth + 1, seen | {name}):
                yield item

    for item in descend(msp, IDENTITY, 1, frozenset()):
        yield item


def iter_inserts(doc, msp, max_depth: int = None
                 ) -> Iterator[Tuple[object, Transform, int]]:
    """Yield every INSERT, including those nested inside other blocks.

    The transform is the one for the INSERT's own contents; its ix, iy is
    the INSERT point in world coordinates.
    """
    if max_depth is None:
        max_depth = int(CONFIG["annotation"].get("block_recursion_depth", 3))

    def walk(container, xform, depth, seen):
        for ins in container.query("INSERT"):
            name = ins.dxf.name
            try:
                child = xform.enter(ins)
            except (ValueError, ZeroDivisionError, AttributeError):
                continue
            yield ins, child, depth
            if depth >= max_depth or not name or name in seen \
               or name not in doc.blocks:
                continue
            for item in walk(doc.blocks[name], child, depth + 1, seen | {name}):
                yield item

    for item in walk(msp, IDENTITY, 0, frozenset()):
        yield item
