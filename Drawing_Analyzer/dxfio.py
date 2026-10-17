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


def load_document_ex(path, unit_override=None, readonly=False):
    """Open a DXF and settle its units. Returns (doc, msp, units, info).

    The unit decision weighs an explicit override, the $INSUNITS header
    and the geometry, in that order of authority - the header alone is
    not trustworthy, as a file declaring Metres while drawn in
    millimetres demonstrated.
    """
    doc, spans = _read(path, readonly)
    msp = doc.modelspace()
    try:
        header_code = int(doc.header.get("$INSUNITS", 0) or 0)
    except Exception:
        header_code = 0
    if spans is None:
        spans = polyline_spans(msp)
    units, info = resolve_units(header_code, spans, unit_override)
    return doc, msp, units, info


# -- Parsed drawings kept in memory -----------------------------------------
# Every step reads the same drawing again - floors, texts, boundaries, the
# geometry engine, the comparison - and parsing a 65 MB sheet takes 12 s
# each time. The parsed drawing is kept pickled in memory (the two most
# recent files) and every caller gets its own copy back in about 5 s, so a
# step that adds boundaries to it never changes what the next step reads.
import collections as _collections
import os as _os
import pickle as _pickle
import threading as _threading

_DOC_CACHE = _collections.OrderedDict()   # key -> (pickled doc, spans)
_DOC_LOCK = _threading.Lock()
_DOC_DONE = _threading.Condition(_DOC_LOCK)    # a pickled copy finished (or failed)
_DOC_BUSY = set()
DOC_WAIT_S = 120.0
_DOC_KEEP = 2


def _doc_key(path):
    """The drawing's identity: its size and the hash of its bytes - a copy
    of a file (the area step copies the drawing Generate wrote) is the same
    drawing, a file written over is not."""
    import hashlib
    try:
        h = hashlib.sha1()
        size = 0
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 22)
                if not chunk:
                    break
                size += len(chunk)
                h.update(chunk)
    except OSError:
        return None
    return (size, h.hexdigest())


# -- A plan placed twice ------------------------------------------------------
# A drawing sometimes holds its whole plan twice, the second copy shifted by
# a millimetre or so (BHUB's 4th floor: every INSERT, line and text drawn
# again 0.9, 1.3 mm away). Every wall then reads as four faces, every label
# twice, and the floor lost its staircases, a fire tower and part of its
# toilets. When most of modelspace has a partner at one and the same small
# offset, the shifted copy is dropped before anything reads the drawing.
DUPLICATE_MAX_SHIFT = 25.0        # drawing units: a copy further off is a real repeat
DUPLICATE_MIN_SHARE = 0.3         # share of modelspace that must be copied
DUPLICATE_MIN_PAIRS = 30


def _dup_signature(e):
    """(signature, reference point) for an entity, or None."""
    t = e.dxftype()
    try:
        if t == "INSERT":
            p = e.dxf.insert
            return (t, e.dxf.name, round(e.dxf.get("rotation", 0.0), 3),
                    round(e.dxf.get("xscale", 1.0), 4), round(e.dxf.get("yscale", 1.0), 4)), (p.x, p.y)
        if t in ("TEXT", "MTEXT"):
            p = e.dxf.insert
            txt = e.dxf.text if t == "TEXT" else e.text
            return (t, txt, e.dxf.layer), (p.x, p.y)
        if t == "LINE":
            a, b = e.dxf.start, e.dxf.end
            if (b.x, b.y) < (a.x, a.y):
                a, b = b, a
            return (t, e.dxf.layer, round(b.x - a.x, 2), round(b.y - a.y, 2)), (a.x, a.y)
        if t == "LWPOLYLINE":
            pts = list(e.get_points("xy"))
            if not pts:
                return None
            x0, y0 = pts[0]
            return (t, e.dxf.layer, len(pts), tuple(round(x - x0, 1) for x, _ in pts[1:6]),
                    tuple(round(y - y0, 1) for _, y in pts[1:6])), (x0, y0)
        if t in ("CIRCLE", "ARC"):
            c = e.dxf.center
            return (t, e.dxf.layer, round(e.dxf.radius, 2)), (c.x, c.y)
        if t == "ELLIPSE":
            c = e.dxf.center
            m = e.dxf.major_axis
            return (t, e.dxf.layer, round(m.x, 2), round(m.y, 2), round(e.dxf.ratio, 4)), (c.x, c.y)
        if t == "LEADER":
            v = list(e.vertices)
            if not v:
                return None
            return (t, e.dxf.layer, len(v)), (v[0][0], v[0][1])
        if t == "HATCH":
            for path in e.paths:
                pts = [tuple(v)[:2] for v in getattr(path, "vertices", [])]
                if not pts:
                    edges = getattr(path, "edges", [])
                    pts = [(ed.start.x, ed.start.y) for ed in edges if hasattr(ed, "start")]
                if pts:
                    return (t, e.dxf.layer, e.dxf.pattern_name, len(e.paths), len(pts)), pts[0]
            return None
        if t == "DIMENSION":
            p = e.dxf.defpoint
            return (t, e.dxf.layer, e.dxf.get("geometry", "")), (p.x, p.y)
    except Exception:                                 # noqa: BLE001
        return None
    return None


def drop_shifted_copy(doc):
    """Remove the second copy of a plan drawn twice a small shift apart.
    Returns the number of entities removed (0 when nothing is copied)."""
    try:
        from scipy.spatial import cKDTree
        import numpy as _np
    except ImportError:
        return 0
    msp = doc.modelspace()
    ents = list(msp)
    groups = {}
    for e in ents:
        sp = _dup_signature(e)
        if sp is None:
            continue
        groups.setdefault(sp[0], []).append((e, sp[1]))
    counted = sum(len(g) for g in groups.values())
    if counted < DUPLICATE_MIN_PAIRS * 2:
        return 0
    shifts = _collections.Counter()
    pairs = []                                        # (e_a, e_b, dx, dy): b = a + d
    for members in groups.values():
        if len(members) < 2:
            continue
        pts = _np.array([p for _e, p in members], dtype=float)
        for i, j in cKDTree(pts).query_pairs(DUPLICATE_MAX_SHIFT):
            dx, dy = pts[j] - pts[i]
            if (dx, dy) < (0.0, 0.0):
                i, j, dx, dy = j, i, -dx, -dy
            key = (round(dx, 1), round(dy, 1))
            shifts[key] += 1
            pairs.append((members[i][0], members[j][0], key))
    if not shifts:
        return 0
    best, n = shifts.most_common(1)[0]
    if n < DUPLICATE_MIN_PAIRS or n < DUPLICATE_MIN_SHARE * counted:
        return 0
    drop = set()
    kept = set()
    for a, b, key in pairs:
        if abs(key[0] - best[0]) > 0.15 or abs(key[1] - best[1]) > 0.15:
            continue
        if id(a) in drop or id(b) in kept:
            continue
        drop.add(id(b))
        kept.add(id(a))
    removed = 0
    for e in ents:
        if id(e) in drop:
            try:
                msp.delete_entity(e)
                removed += 1
            except Exception:                         # noqa: BLE001
                continue
    if removed:
        import logging
        logging.getLogger(__name__).warning(
            "plan drawn twice, %.2f, %.2f apart: %d copied entities dropped", best[0], best[1], removed)
    return removed


def _read(path, readonly=False):
    """(a fresh parsed copy of the drawing, its polyline spans or None).

    `readonly`: the caller only reads the drawing, so its own copy can be
    pickled for the next step while it works - no second parse."""
    key = _doc_key(path)
    if key is not None:
        with _DOC_LOCK:
            hit = _DOC_CACHE.get(key)
            if hit is None and key in _DOC_BUSY:
                # The step before parsed this drawing a moment ago and its
                # copy is being pickled: wait for it rather than parse the
                # file again (12 s on a 65 MB sheet against a few seconds).
                _DOC_DONE.wait_for(lambda: key not in _DOC_BUSY, timeout=DOC_WAIT_S)
                hit = _DOC_CACHE.get(key)
            if hit is not None:
                _DOC_CACHE.move_to_end(key)
        if hit is not None:
            try:
                return _pickle.loads(hit[0]), hit[1]
            except Exception:                         # noqa: BLE001
                with _DOC_LOCK:
                    _DOC_CACHE.pop(key, None)
    try:
        doc = ezdxf.readfile(path)
    except (ValueError, ezdxf.DXFStructureError):
        # A drawing ezdxf cannot load as written - H10 Tower 3 holds an
        # entity with no handle, so the copy this app saves of it read
        # back as "Invalid handle None" - is read in recover mode, which
        # repairs what it can, rather than failing the whole job.
        from ezdxf import recover
        doc, _auditor = recover.readfile(path)
    drop_shifted_copy(doc)
    if key is not None and readonly:
        _remember_later(doc, key)
    return doc, None


def remember_written(doc, path):
    """Keep the drawing this app has just written to `path` (Generate's
    boundaries) as the next step would read it back - its shifted copy
    dropped, pickled - so the area step, the preview and the final drawing
    do not parse the file again. The caller is done with `doc`."""
    key = _doc_key(path)
    if key is not None:
        _remember_later(doc, key, drop_copy=True)


def _remember_later(doc, key, drop_copy=False):
    """Keep a pickled copy of a drawing a read-only caller has just parsed,
    made on a helper thread while the caller carries on reading it."""
    with _DOC_LOCK:
        if key in _DOC_CACHE or key in _DOC_BUSY:
            return
        _DOC_BUSY.add(key)

    def work():
        try:
            if drop_copy:
                drop_shifted_copy(doc)        # as _read does to a file it parses
            spans = polyline_spans(doc.modelspace())
            blob = _pickle.dumps(doc, protocol=_pickle.HIGHEST_PROTOCOL)
            with _DOC_LOCK:
                _DOC_CACHE[key] = (blob, spans)
                _DOC_CACHE.move_to_end(key)
                while len(_DOC_CACHE) > _DOC_KEEP:
                    _DOC_CACHE.popitem(last=False)
        except Exception:                             # noqa: BLE001
            pass
        finally:
            with _DOC_LOCK:
                _DOC_BUSY.discard(key)
                _DOC_DONE.notify_all()

    _threading.Thread(target=work, name="dxf-cache", daemon=True).start()


def set_insunits(doc, units):
    """Record the units the drawing was read in as its $INSUNITS, so a file
    this app writes opens - here and in CAD - in the same units."""
    try:
        code = int(getattr(units, "code", 0) or 0)
        if code:
            doc.header["$INSUNITS"] = code
    except Exception:                                 # noqa: BLE001
        pass


def load_document(path, unit_override=None, readonly=False):
    """As load_document_ex, without the diagnostics."""
    doc, msp, units, _info = load_document_ex(path, unit_override, readonly)
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

    # A block placed 400 times was searched 400 times over (27,000 searches,
    # 5 s, on 285 FD): each block's entities are looked up once per walk.
    found = {}

    def query(container, key, types):
        k = (key, types)
        got = found.get(k)
        if got is None:
            got = found[k] = list(container.query(types))
        return got

    def descend(container, ckey, xform, depth, seen):
        if depth > max_depth:
            return
        for ins in query(container, ckey, "INSERT"):
            name = ins.dxf.name
            if not name or name in seen or name not in doc.blocks:
                continue
            try:
                child = xform.enter(ins)
            except (ValueError, ZeroDivisionError, AttributeError):
                continue
            block = doc.blocks[name]
            for e in query(block, ("block", name), dxftypes):
                yield e, child, depth
            for item in descend(block, ("block", name), child, depth + 1, seen | {name}):
                yield item

    for item in descend(msp, ("msp",), IDENTITY, 1, frozenset()):
        yield item


def iter_inserts(doc, msp, max_depth: int = None
                 ) -> Iterator[Tuple[object, Transform, int]]:
    """Yield every INSERT, including those nested inside other blocks.

    The transform is the one for the INSERT's own contents; its ix, iy is
    the INSERT point in world coordinates.
    """
    if max_depth is None:
        max_depth = int(CONFIG["annotation"].get("block_recursion_depth", 3))

    found = {}                        # each block's INSERTs, looked up once per walk

    def walk(container, ckey, xform, depth, seen):
        got = found.get(ckey)
        if got is None:
            got = found[ckey] = list(container.query("INSERT"))
        for ins in got:
            name = ins.dxf.name
            try:
                child = xform.enter(ins)
            except (ValueError, ZeroDivisionError, AttributeError):
                continue
            yield ins, child, depth
            if depth >= max_depth or not name or name in seen \
               or name not in doc.blocks:
                continue
            for item in walk(doc.blocks[name], ("block", name), child, depth + 1, seen | {name}):
                yield item

    for item in walk(msp, ("msp",), IDENTITY, 0, frozenset()):
        yield item
