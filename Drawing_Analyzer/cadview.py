# -*- coding: utf-8 -*-
"""CAD viewer data: the whole drawing as coloured linework and text.

Steps 2, 3 and 4 show the drawing itself, the way CAD does, with each step's
own data laid over it (floor plates, extracted texts, room boundaries). The
boundary preview in preview.py cannot serve that: it walks every INSERT's
block contents afresh, and a production sheet places the same furniture and
sanitary blocks thousands of times - 480,000 entity visits and 40 seconds
on the T1 typical-floor sheet.

Here each block definition is read once, into numpy arrays in its own
coordinates, and every insert of it is a single matrix product. Colours are
kept (BYLAYER, BYBLOCK and layer 0 inside blocks resolve as AutoCAD does),
so the picture reads like the drawing on screen in CAD.

The result is cached per upload:
    <token>_cad_<which>.npz   linework, one coordinate array per colour
    <token>_cad_<which>.json  extent, colours, texts (and, for a corrected
                              drawing, its *_BOUNDARY polygons)
and served in pieces: the lines as a compact binary for a canvas, clipped to
the area on view and thinned to what the screen can show.
"""

from __future__ import annotations

import json
import math
import os
import struct
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import ezdxf
from ezdxf.colors import aci2rgb

from .dxfio import load_document

# Curves are flattened to this chord error, in millimetres.
_SAGITTA_MM = 15.0
# Nested blocks deeper than this are left out (and cannot recurse forever).
_MAX_DEPTH = 8
_MAX_TEXTS = 30000
_CURVES = ("ARC", "CIRCLE", "ELLIPSE", "SPLINE")

_BYBLOCK = -1           # colour tokens resolved at insert time
_LAYER0 = -2


# --------------------------------------------------------------- extraction
class _Geo:
    """Runs of points grouped by colour key: {key: ([arrays], [lengths])}."""

    __slots__ = ("runs", "texts")

    def __init__(self):
        self.runs = {}
        self.texts = []           # [x, y, height, rotation_deg, text, key]

    def add(self, key, pts):
        if len(pts) < 2:
            return
        arrs, lens = self.runs.setdefault(key, ([], []))
        arrs.append(np.asarray(pts, dtype=np.float64).reshape(-1, 2))
        lens.append(len(arrs[-1]))


class _Extractor:

    def __init__(self, doc, units):
        self.doc = doc
        self.units = units
        self.sagitta = _SAGITTA_MM / max(units.mm_per_unit, 1e-9)
        self.layer_rgb = {}
        self.hidden = set()
        for layer in doc.layers:
            name = layer.dxf.name.upper()
            try:
                if layer.is_off() or layer.is_frozen():
                    self.hidden.add(name)
            except Exception:                         # noqa: BLE001
                pass
            self.layer_rgb[name] = self._aci_rgb(abs(layer.dxf.get("color", 7)),
                                                 layer.dxf.get("true_color"))
        self.block_cache = {}

    # colours -------------------------------------------------------------
    @staticmethod
    def _aci_rgb(aci, true_color=None):
        if true_color is not None:
            return int(true_color) & 0xFFFFFF
        if aci is None or aci <= 0 or aci > 255:
            aci = 7
        r, g, b = aci2rgb(int(aci))
        return (r << 16) | (g << 8) | b

    def _key(self, e):
        """Colour key for an entity: an RGB int, or a token that the insert
        placing it resolves (BYBLOCK, or BYLAYER on layer 0)."""
        tc = e.dxf.get("true_color")
        if tc is not None:
            return int(tc) & 0xFFFFFF
        c = e.dxf.get("color", 256)
        layer = (e.dxf.get("layer") or "0").upper()
        if c == 0:
            return _BYBLOCK
        if c == 256 or c is None:
            if layer == "0":
                return _LAYER0
            return self.layer_rgb.get(layer, 0xFFFFFF)
        return self._aci_rgb(c)

    # geometry --------------------------------------------------------------
    def _entity_points(self, e):
        """Local (untransformed) point runs for one drawable entity."""
        kind = e.dxftype()
        if kind == "LINE":
            s, t = e.dxf.start, e.dxf.end
            return [[(s.x, s.y), (t.x, t.y)]]
        if kind == "LWPOLYLINE":
            vals = np.asarray(e.lwpoints.values, dtype=np.float64).reshape(-1, 5)
            if len(vals) < 2:
                return []
            ext = e.dxf.get("extrusion")
            plain = ext is None or (abs(ext[0]) < 1e-9 and abs(ext[1]) < 1e-9)
            if plain and not np.any(vals[:, 4]):
                pts = vals[:, :2]
                if ext is not None and ext[2] < 0:
                    pts = pts * np.array([-1.0, 1.0])
                if e.closed:
                    pts = np.vstack([pts, pts[:1]])
                return [pts]
            return self._path_points(e)
        if kind == "POLYLINE":
            if e.is_poly_face_mesh or e.is_polygon_mesh:
                return []
            return self._path_points(e)
        if kind in _CURVES:
            try:
                return [[(v.x, v.y) for v in e.flattening(self.sagitta)]]
            except Exception:                         # noqa: BLE001
                return []
        return []

    def _path_points(self, e):
        try:
            p = ezdxf.path.make_path(e)
            return [[(v.x, v.y) for v in p.flattening(self.sagitta)]]
        except Exception:                             # noqa: BLE001
            return []

    @staticmethod
    def _text(e):
        kind = e.dxftype()
        try:
            if kind == "MTEXT":
                value = e.plain_text()
                ins = e.dxf.insert
                height = e.dxf.get("char_height", 1.0) or 1.0
                rot = e.get_rotation()
            else:
                value = e.plain_text() if hasattr(e, "plain_text") else e.dxf.text
                ins = e.dxf.insert
                align = e.dxf.get("halign", 0) or e.dxf.get("valign", 0)
                if align and e.dxf.hasattr("align_point"):
                    ins = e.dxf.align_point
                height = e.dxf.get("height", 1.0) or 1.0
                rot = e.dxf.get("rotation", 0.0) or 0.0
        except Exception:                             # noqa: BLE001
            return None
        value = " ".join(str(value or "").split())
        if not value:
            return None
        return [ins.x, ins.y, float(height), float(rot), value[:200]]

    def block(self, name, depth):
        """A block's geometry in its own coordinates, read once."""
        if name in self.block_cache:
            return self.block_cache[name]
        self.block_cache[name] = None                 # guards recursion
        geo = _Geo()
        try:
            blk = self.doc.blocks.get(name)
        except Exception:                             # noqa: BLE001
            blk = None
        if blk is not None:
            base = blk.block.dxf.get("base_point", (0, 0, 0))
            self._collect(blk, geo, depth, skip_hidden=False)
            packed = _pack_runs(geo)
            if base[0] or base[1]:
                shift = np.array([base[0], base[1]])
                packed = {k: (a - shift, n) for k, (a, n) in packed.items()}
                for t in geo.texts:
                    t[0] -= base[0]
                    t[1] -= base[1]
            self.block_cache[name] = (packed, geo.texts)
        else:
            self.block_cache[name] = ({}, [])
        return self.block_cache[name]

    def _collect(self, layout, geo, depth, skip_hidden=True):
        for e in layout:
            kind = e.dxftype()
            layer = (e.dxf.get("layer") or "0").upper()
            if skip_hidden and layer in self.hidden:
                continue
            if kind == "INSERT":
                if depth < _MAX_DEPTH:
                    self._insert(e, geo, depth + 1)
                continue
            if kind in ("TEXT", "MTEXT"):
                t = self._text(e)
                if t:
                    geo.texts.append(t + [self._key(e)])
                continue
            try:
                runs = self._entity_points(e)
            except Exception:                         # noqa: BLE001
                continue
            if runs:
                key = self._key(e)
                for r in runs:
                    geo.add(key, r)

    def _insert(self, ins, geo, depth):
        name = ins.dxf.get("name")
        cached = self.block(name, depth)
        if not cached:
            return
        packed, texts = cached
        try:
            m = list(ins.matrix44())
        except Exception:                             # noqa: BLE001
            return
        a = np.array([[m[0], m[1]], [m[4], m[5]]])
        t = np.array([m[12], m[13]])
        # What the block's tokens become under this insert.
        own = self._key(ins)
        ins_layer = (ins.dxf.get("layer") or "0").upper()
        layer_key = _LAYER0 if ins_layer == "0" else \
            self.layer_rgb.get(ins_layer, 0xFFFFFF)
        byblock = own          # an RGB, or a token the next insert up resolves

        def resolve(k):
            if k == _BYBLOCK:
                return byblock
            if k == _LAYER0:
                return layer_key
            return k

        for k, (arr, lens) in packed.items():
            out = arr @ a + t
            arrs, ls = geo.runs.setdefault(resolve(k), ([], []))
            # Stored as one block of runs: split lengths kept alongside.
            arrs.append(out)
            ls.append(lens)
        if texts and len(geo.texts) < _MAX_TEXTS:
            scale = math.sqrt(abs(a[0, 0] * a[1, 1] - a[0, 1] * a[1, 0])) or 1.0
            turn = math.degrees(math.atan2(a[0, 1], a[0, 0]))
            for x, y, h, rot, value, k in texts:
                p = np.array([x, y]) @ a + t
                geo.texts.append([p[0], p[1], h * scale, rot + turn, value,
                                  resolve(k)])
        # Attribute values sit on the insert itself, already in place.
        for att in getattr(ins, "attribs", []) or []:
            if att.dxf.get("invisible", 0):
                continue
            tt = self._text(att)
            if tt:
                geo.texts.append(tt + [self._key(att)])


def _pack_runs(geo):
    """_Geo whose run lists mix single lengths and length arrays."""
    out = {}
    for key, (arrs, lens) in geo.runs.items():
        if not arrs:
            continue
        flat = []
        for n in lens:
            if isinstance(n, np.ndarray):
                flat.append(n)
            else:
                flat.append(np.array([n], dtype=np.int64))
        out[key] = (np.vstack(arrs), np.concatenate(flat))
    return out


def extract(dxf_path, unit_override=None, loaded=None):
    """Read the drawing: {rgb: (points_mm (N,2) float32, run lengths)},
    texts, extent. Unresolved colour tokens at the top level fall back to
    white, as AutoCAD draws them. `loaded` is (doc, msp, units) when the
    caller has already parsed the file."""
    doc, msp, units = loaded or load_document(dxf_path, unit_override)
    ex = _Extractor(doc, units)
    geo = _Geo()
    ex._collect(msp, geo, 0)
    runs = _pack_runs(geo)
    k = units.mm_per_unit
    merged = {}
    for key, (pts, lens) in runs.items():
        rgb = 0xFFFFFF if key < 0 else int(key)
        a, n = merged.get(rgb, ([], []))
        a.append(pts * k)
        n.append(lens)
        merged[rgb] = (a, n)
    lines = {}
    lo = np.array([np.inf, np.inf])
    hi = -lo
    for rgb, (a, n) in merged.items():
        pts = np.vstack(a).astype(np.float64)
        lens = np.concatenate(n).astype(np.int64)
        good = np.isfinite(pts).all(axis=1)
        if not good.all():
            pts, lens = _drop_bad(pts, lens, good)
        if not len(pts):
            continue
        lines[rgb] = (pts, lens)
    texts = []
    for x, y, h, rot, value, key in geo.texts[:_MAX_TEXTS]:
        rgb = 0xFFFFFF if key < 0 else int(key)
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        texts.append([round(x * k, 1), round(y * k, 1),
                      round(max(h * k, 1.0), 1), round(rot % 360.0, 1),
                      value, rgb])
    extent = _extent(lines, texts)
    return {"lines": lines, "texts": texts, "extent": extent,
            "units": units.to_ui(), "doc": doc, "msp": msp,
            "units_obj": units}


def _drop_bad(pts, lens, good):
    starts = np.concatenate([[0], np.cumsum(lens)[:-1]])
    run_id = np.repeat(np.arange(len(lens)), lens)
    bad_runs = np.unique(run_id[~good])
    keep_run = np.ones(len(lens), dtype=bool)
    keep_run[bad_runs] = False
    mask = keep_run[run_id]
    del starts
    return pts[mask], lens[keep_run]


def _extent(lines, texts):
    """The drawing's extent, ignoring the stray far-off entity a CAD
    sheet often carries: 0.2%..99.8% of all vertices, padded."""
    xs, ys = [], []
    for pts, _ in lines.values():
        if len(pts) > 200000:
            step = len(pts) // 200000 + 1
            pts = pts[::step]
        xs.append(pts[:, 0])
        ys.append(pts[:, 1])
    if texts:
        xs.append(np.array([t[0] for t in texts]))
        ys.append(np.array([t[1] for t in texts]))
    if not xs:
        return [0.0, 0.0, 1000.0, 1000.0]
    x = np.concatenate(xs)
    y = np.concatenate(ys)
    x1, x2 = np.percentile(x, [0.2, 99.8])
    y1, y2 = np.percentile(y, [0.2, 99.8])
    return [float(x1), float(y1), float(x2), float(y2)]


# ------------------------------------------------------------------ caching
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cadview")
_JOBS = {}
_LOCK = threading.Lock()


def cache_paths(out_dir, token, which):
    base = os.path.join(out_dir, "{0}_cad_{1}".format(token, which))
    return base + ".npz", base + ".json"


def _build(dxf_path, npz_path, json_path, unit_override, boundaries,
           loaded=None):
    data = extract(dxf_path, unit_override, loaded)
    arrays = {}
    colours = []
    for i, (rgb, (pts, lens)) in enumerate(sorted(data["lines"].items())):
        arrays["p{0}".format(i)] = np.round(pts).astype(np.int32)
        arrays["n{0}".format(i)] = lens.astype(np.int32)
        colours.append(rgb)
    tmp = npz_path + ".tmp.npz"
    np.savez(tmp, **arrays)
    os.replace(tmp, npz_path)
    scene = {"extent": data["extent"], "colours": colours,
             "texts": data["texts"], "units": data["units"],
             "points": int(sum(len(p) for p, _ in data["lines"].values()))}
    if boundaries:
        scene["boundaries"] = _boundaries(data["doc"], data["msp"],
                                          data["units_obj"])
    tmp = json_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(scene, fh, ensure_ascii=False)
    os.replace(tmp, json_path)


def start(dxf_path, out_dir, token, which, unit_override=None,
          boundaries=False, loaded=None):
    """Begin building the view in the background; safe to call twice."""
    npz_path, json_path = cache_paths(out_dir, token, which)
    key = (token, which)
    with _LOCK:
        job = _JOBS.get(key)
        if job is not None:
            return job
        if os.path.exists(npz_path) and os.path.exists(json_path):
            return None
        job = _POOL.submit(_build, dxf_path, npz_path, json_path,
                           unit_override, boundaries, loaded)
        _JOBS[key] = job
        return job


def status(dxf_path, out_dir, token, which, unit_override=None,
           boundaries=False):
    """("ready", scene) / ("building", None) / ("error", message)."""
    npz_path, json_path = cache_paths(out_dir, token, which)
    if os.path.exists(npz_path) and os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as fh:
            return "ready", json.load(fh)
    if not os.path.exists(dxf_path):
        return "error", "Upload expired or not found. Please upload again."
    job = start(dxf_path, out_dir, token, which, unit_override, boundaries)
    if job is not None and job.done():
        with _LOCK:
            _JOBS.pop((token, which), None)
        exc = job.exception()
        if exc is not None:
            return "error", "Could not read this drawing for the viewer."
        return status(dxf_path, out_dir, token, which, unit_override,
                      boundaries)
    return "building", None


def forget(out_dir, token, which):
    for p in cache_paths(out_dir, token, which):
        try:
            os.remove(p)
        except OSError:
            pass
    with _LOCK:
        _JOBS.pop((token, which), None)


# --------------------------------------------------------------- serving
def lines_binary(out_dir, token, which, bbox=None, px=None):
    """The linework as a little-endian binary for the canvas viewer.

    bbox (x1, y1, x2, y2, mm) keeps only runs crossing it; px is the size
    in mm of one screen pixel at the view it is for, and detail finer than
    half of that is thinned out. Layout:
        uint32 ncolours
        per colour: uint32 rgb, uint32 nruns, uint32 npoints,
                    uint32 lengths[nruns], int32 xy[npoints * 2]
    """
    npz_path, _ = cache_paths(out_dir, token, which)
    data = np.load(npz_path)
    ncol = len([k for k in data.files if k.startswith("p")])
    with open(cache_paths(out_dir, token, which)[1], "r",
              encoding="utf-8") as fh:
        colours = json.load(fh)["colours"]
    chunks = []
    used = 0
    for i in range(ncol):
        pts = data["p{0}".format(i)].astype(np.int64)
        lens = data["n{0}".format(i)].astype(np.int64)
        pts, lens = _clip(pts, lens, bbox)
        if px:
            pts, lens = _thin(pts, lens, max(float(px) * 0.5, 1.0))
        if not len(lens):
            continue
        used += 1
        chunks.append(struct.pack("<III", colours[i] & 0xFFFFFF, len(lens),
                                  len(pts)))
        chunks.append(lens.astype("<u4").tobytes())
        chunks.append(pts.astype("<i4").tobytes())
    return struct.pack("<I", used) + b"".join(chunks)


def _run_ids(lens):
    return np.repeat(np.arange(len(lens)), lens)


def _clip(pts, lens, bbox):
    if bbox is None or not len(lens):
        return pts, lens
    x1, y1, x2, y2 = bbox
    starts = np.concatenate([[0], np.cumsum(lens)[:-1]])
    rx1 = np.minimum.reduceat(pts[:, 0], starts)
    rx2 = np.maximum.reduceat(pts[:, 0], starts)
    ry1 = np.minimum.reduceat(pts[:, 1], starts)
    ry2 = np.maximum.reduceat(pts[:, 1], starts)
    keep = (rx2 >= x1) & (rx1 <= x2) & (ry2 >= y1) & (ry1 <= y2)
    return pts[keep[_run_ids(lens)]], lens[keep]


def _thin(pts, lens, grid):
    """Drop vertices that land on the same grid cell as the one before, and
    whole runs smaller than a cell: detail the screen cannot show."""
    if not len(lens):
        return pts, lens
    starts = np.concatenate([[0], np.cumsum(lens)[:-1]])
    ends = starts + lens - 1
    q = np.floor(pts / grid).astype(np.int64)
    keep = np.ones(len(pts), dtype=bool)
    keep[1:] = np.any(q[1:] != q[:-1], axis=1)
    keep[starts] = True
    keep[ends] = True
    # Runs whose whole extent is under one cell are invisible.
    rx = np.maximum.reduceat(pts[:, 0], starts) - np.minimum.reduceat(pts[:, 0], starts)
    ry = np.maximum.reduceat(pts[:, 1], starts) - np.minimum.reduceat(pts[:, 1], starts)
    big = (rx >= grid) | (ry >= grid)
    rid = _run_ids(lens)
    keep &= big[rid]
    new_lens = np.bincount(rid[keep], minlength=len(lens))[big]
    return pts[keep], new_lens


# ------------------------------------------------- corrected-drawing rooms
def _boundaries(doc, msp, units):
    """Closed polylines on *_BOUNDARY layers with their room labels, for the
    Step 4 hover readout. Thin wrapper over what the preview reads."""
    from .dxf_reader import (_FAMILY_RULES, _is_closed, _point_in_polygon,
                             _polyline_points, _text_position, _text_value,
                             family_from_layer, floor_tag)
    from .geometry import shoelace_area_sqm
    shapes, texts = [], []
    for e in msp.query("LWPOLYLINE POLYLINE"):
        layer = e.dxf.layer or "0"
        if not layer.upper().endswith("_BOUNDARY"):
            continue
        try:
            pts = _polyline_points(e, units)
        except Exception:                             # noqa: BLE001
            continue
        if len(pts) < 3:
            continue
        closed = _is_closed(e) or (abs(pts[0][0] - pts[-1][0]) < 1
                                   and abs(pts[0][1] - pts[-1][1]) < 1)
        if not closed:
            continue
        area = shoelace_area_sqm(pts)
        if area < 0.05:
            continue
        shapes.append({"layer": layer, "pts": pts, "area": area,
                       "floor": floor_tag(e)})
    for e in msp.query("TEXT MTEXT"):
        layer = (e.dxf.layer or "0").upper()
        if not layer.endswith("_BOUNDARY"):
            continue
        value = _text_value(e)
        if value:
            x, y = units.point_to_mm(*_text_position(e))
            texts.append((layer, x, y, value))
    by_layer = {}
    for layer, x, y, value in texts:
        by_layer.setdefault(layer, []).append((x, y, value))
    out = []
    for s in sorted(shapes, key=lambda s: s["area"]):
        if s.get("floor"):
            # A floor outline written by Step 3, tagged with its floor name.
            out.append({"layer": s["layer"], "family": "Floor plate", "aci": 7,
                        "name": s["floor"], "area": round(float(s["area"]), 2),
                        "floor": True,
                        "pts": [[int(round(x)), int(round(y))]
                                for x, y in s["pts"]]})
            continue
        name = ""
        cands = by_layer.get(s["layer"].upper()) or []
        for i, (x, y, value) in enumerate(cands):
            if _point_in_polygon(x, y, s["pts"]):
                name = value
                cands.pop(i)
                break
        fam = family_from_layer(s["layer"], _FAMILY_RULES)
        out.append({"layer": s["layer"], "family": fam["name"],
                    "aci": int(fam.get("color", 7)), "name": name,
                    "area": round(float(s["area"]), 2),
                    "pts": [[int(round(x)), int(round(y))] for x, y in s["pts"]]})
    return out
