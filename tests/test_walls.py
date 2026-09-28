# -*- coding: utf-8 -*-
"""Room boundaries follow real walls.

Each case here failed before the v5 wall model:

  * block placement - a rotated block inside a mirrored one, a block
    stored with extrusion (0, 0, -1), a block with a base point, and a
    bulge inside a mirrored block were all placed or bent wrongly;
  * layer 0 inside a block did not take the INSERT's layer, so furniture
    blocks escaped the furniture exclusion;
  * WC cubicle partitions and door leaves were walls, so a toilet was
    measured to the cubicle fronts;
  * an opening was only closed by joining line ends, never along the wall,
    and an opening with no door was closed as if it had one;
  * a traced L-shaped room claimed the label of the room in its corner by
    bounding box, and two labels in one traced room became a rectangle.

The end-to-end cases build a small DXF, run annotate_dxf and read the
result back with compute_room_areas, exactly as Steps 3 and 4 do.

Run:  python -m unittest discover -s tests -v
"""

import math
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ezdxf  # noqa: E402

from Drawing_Analyzer import dxf_reader as dr  # noqa: E402
from Drawing_Analyzer.dxfio import effective_layer, iter_entities  # noqa: E402


def _close(a, b, tol=1e-6):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


# ---------------------------------------------------------------------------
# Block placement
# ---------------------------------------------------------------------------
class BlockPlacementTests(unittest.TestCase):

    def _lines(self, doc):
        out = []
        for e, xf, depth in iter_entities(doc, doc.modelspace(), "LINE"):
            if depth == 0:
                continue
            s, t = e.dxf.start, e.dxf.end
            out.append((xf.apply(s.x, s.y, s.z), xf.apply(t.x, t.y, t.z)))
        return out

    def test_rotated_block_inside_mirrored_block(self):
        doc = ezdxf.new()
        child = doc.blocks.new("CHILD")
        child.add_line((0, 0), (100, 0))
        parent = doc.blocks.new("PARENT")
        parent.add_blockref("CHILD", (0, 0), dxfattribs={"rotation": 90})
        doc.modelspace().add_blockref("PARENT", (1000, 0),
                                      dxfattribs={"xscale": -1})
        (a, b), = self._lines(doc)
        # (100, 0) rotated 90 deg is (0, 100); mirroring x leaves it there.
        # The old composition added the rotations and put it at y = -100.
        self.assertTrue(_close(a, (1000, 0)), a)
        self.assertTrue(_close(b, (1000, 100)), b)

    def test_block_with_flipped_extrusion(self):
        doc = ezdxf.new()
        blk = doc.blocks.new("B")
        blk.add_line((0, 0), (100, 0))
        ins = doc.modelspace().add_blockref(
            "B", (500, 0), dxfattribs={"extrusion": (0, 0, -1)})
        expected = [(v.dxf.start, v.dxf.end) for v in ins.virtual_entities()]
        (a, b), = self._lines(doc)
        (ea, eb), = expected
        # Stored mirrored through its OCS: the insert point is at x = -500.
        self.assertTrue(_close(a, (ea.x, ea.y)), (a, ea))
        self.assertTrue(_close(b, (eb.x, eb.y)), (b, eb))
        self.assertLess(a[0], 0)

    def test_block_base_point(self):
        doc = ezdxf.new()
        blk = doc.blocks.new("B", base_point=(50, 50))
        blk.add_line((50, 50), (150, 50))
        doc.modelspace().add_blockref("B", (0, 0))
        (a, b), = self._lines(doc)
        self.assertTrue(_close(a, (0, 0)), a)
        self.assertTrue(_close(b, (100, 0)), b)

    def test_bulge_in_mirrored_block_bends_the_right_way(self):
        doc = ezdxf.new()
        blk = doc.blocks.new("ARCED")
        # Bulge 1 from (0,0) to (100,0) is a half circle below the chord.
        blk.add_lwpolyline([(0, 0, 1.0), (100, 0, 0.0)], format="xyb")
        doc.modelspace().add_blockref("ARCED", (0, 0),
                                      dxfattribs={"xscale": -1})
        (e, xf, _d), = [t for t in iter_entities(doc, doc.modelspace(),
                                                 "LWPOLYLINE") if t[2] > 0]
        pts = dr._polyline_points(e, None, xf)
        self.assertGreater(len(pts), 3)
        # Mirroring in x keeps the arc below the chord; bending it after
        # placing put it above.
        self.assertTrue(all(p[1] <= 1e-6 for p in pts), pts)
        self.assertAlmostEqual(min(p[1] for p in pts), -50.0, delta=6.0)

    def test_layer_zero_takes_the_insert_layer(self):
        doc = ezdxf.new()
        blk = doc.blocks.new("WC")
        blk.add_line((0, 0), (1500, 0), dxfattribs={"layer": "0"})
        blk.add_line((0, 0), (0, 1500), dxfattribs={"layer": "A-WALL"})
        doc.modelspace().add_blockref("WC", (0, 0),
                                      dxfattribs={"layer": "A-FURN"})
        got = sorted(effective_layer(e, xf) for e, xf, d in
                     iter_entities(doc, doc.modelspace(), "LINE") if d > 0)
        self.assertEqual(got, ["A-FURN", "A-WALL"])


# ---------------------------------------------------------------------------
# Wall detection on segments
# ---------------------------------------------------------------------------
def _seg(x0, y0, x1, y1, layer="A-BLOCKWORK"):
    return (x0, y0, x1, y1, layer, math.hypot(x1 - x0, y1 - y0), -1)


def _rect(x0, y0, x1, y1, layer="A-BLOCKWORK"):
    return [_seg(x0, y0, x1, y0, layer), _seg(x1, y0, x1, y1, layer),
            _seg(x1, y1, x0, y1, layer), _seg(x0, y1, x0, y0, layer)]


class WallDetectionTests(unittest.TestCase):

    def _faces(self, raw, **kw):
        return set(tuple(round(v) for v in f)
                   for f in dr._detect_walls(raw, **kw)["faces"])

    def test_two_faces_a_wall_apart_are_a_wall(self):
        faces = self._faces([_seg(0, 0, 5000, 0), _seg(0, 200, 5000, 200)])
        self.assertEqual(len(faces), 2)

    def test_single_line_is_not_a_wall(self):
        self.assertEqual(self._faces([_seg(0, 0, 5000, 0)]), set())

    def test_partition_board_and_door_leaf_are_panels(self):
        raw = (_rect(0, 0, 20, 1500)            # cubicle partition, 20 mm
               + _rect(170, 900, 182, 1500))    # door leaf beside it, 12 mm
        # The leaf stands 150 mm from the partition - a wall's thickness -
        # which is how a row of cubicles used to read as walls.
        self.assertEqual(self._faces(raw), set())

    def test_wall_face_survives_a_short_detail_beside_it(self):
        raw = [_seg(0, 0, 10000, 0), _seg(0, 200, 10000, 200),
               _seg(1000, 25, 2000, 25)]         # WC ledge line 25 mm off
        faces = self._faces(raw)
        self.assertIn((0, 0, 10000, 0), faces)
        self.assertIn((0, 200, 10000, 200), faces)

    def test_structure_layer_is_a_wall_at_any_thickness(self):
        raw = _rect(0, 0, 1000, 1900, layer="XREF-CORE WALLS$0$RCC")
        self.assertEqual(len(self._faces(raw)), 4)

    def test_never_wall_layer_is_ignored(self):
        raw = [_seg(0, 0, 5000, 0, "A-DIM"), _seg(0, 200, 5000, 200, "A-DIM")]
        self.assertEqual(self._faces(raw), set())

    def test_x_mark_diagonals_are_skipped(self):
        raw = (_rect(0, 0, 1000, 6000, layer="A-CUTOUT")
               + [_seg(0, 0, 1000, 6000, "A-CUTOUT"),
                  _seg(1000, 0, 0, 6000, "A-CUTOUT")])
        skip = {dr._seg_key(raw[4]), dr._seg_key(raw[5])}
        faces = self._faces(raw, skip=skip)
        self.assertEqual(len(faces), 4)


# ---------------------------------------------------------------------------
# Door bridges
# ---------------------------------------------------------------------------
class DoorBridgeTests(unittest.TestCase):
    """A 200 mm wall along x = 0..200 with a 900 mm opening at y 2000-2900."""

    def setUp(self):
        raw = _rect(0, 0, 200, 2000) + _rect(0, 2900, 200, 5000)
        w = dr._detect_walls(raw)
        self.faces, self.caps = w["faces"], w["caps"]

    def _bridges(self, swings):
        return [tuple(round(v) for v in b)
                for b in dr._door_bridges(self.faces, self.caps, swings)]

    def test_single_door_closes_straight_along_both_faces(self):
        got = self._bridges([(0.0, 2000.0, 900.0)])
        self.assertEqual(len(got), 2, got)
        for b in got:
            self.assertEqual(b[0], b[2])                 # vertical: along the wall
            self.assertIn(b[0], (0, 200))
            self.assertEqual(sorted((b[1], b[3])), [2000, 2900])

    def test_double_door(self):
        self.assertEqual(len(self._bridges([(0.0, 2000.0, 450.0),
                                            (0.0, 2900.0, 450.0)])), 2)

    def test_no_door_no_bridge(self):
        self.assertEqual(self._bridges([]), [])

    def test_door_that_does_not_fit_the_opening(self):
        self.assertEqual(self._bridges([(0.0, 2000.0, 1400.0)]), [])


# ---------------------------------------------------------------------------
# End to end: annotate_dxf, then compute_room_areas
# ---------------------------------------------------------------------------
WALL = "A-BLOCKWORK"


def _wall(msp, x0, y0, x1, y1, layer=WALL):
    msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True,
                       dxfattribs={"layer": layer})


def _text(msp, text, x, y, h=250):
    msp.add_text(text, dxfattribs={"layer": "ROOMNAME", "height": h,
                                   "insert": (x, y)})


def _new_doc():
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_lwpolyline([(-5000, -5000), (20000, -5000), (20000, 20000),
                        (-5000, 20000)], close=True,
                       dxfattribs={"layer": "FLOOR"})
    return doc, msp


def _l_corridor(msp):
    """L-shaped space [0,10000]x[0,2000] + [8000,10000]x[0,10000]: 36 sq.m."""
    _wall(msp, -200, -200, 10200, 0)
    _wall(msp, 10000, -200, 10200, 10200)
    _wall(msp, 7800, 10000, 10200, 10200)
    _wall(msp, -200, 0, 0, 2000)
    _wall(msp, -200, 2000, 8000, 2200)
    _wall(msp, 7800, 2000, 8000, 10200)


class AnnotateEndToEndTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _run(self, doc):
        src = os.path.join(self.tmp, "in.dxf")
        dst = os.path.join(self.tmp, "out.dxf")
        doc.saveas(src)
        floors = [{"name": "F", "polygon": [(-5000, -5000), (20000, -5000),
                                            (20000, 20000), (-5000, 20000)]}]
        rows = dr.extract_texts(src, floors)
        stats = dr.annotate_dxf(src, dst, floors, rows)
        areas = dr.compute_room_areas(dst, "FLOOR")
        return {r["name"]: r for r in areas["rooms"]}, stats

    def test_toilet_follows_the_walls(self):
        """Cubicles and a doorless alcove included; the door closed along
        the wall; the corridor outside kept out."""
        doc, msp = _new_doc()
        _wall(msp, -200, -200, 8200, 0)                  # bottom
        _wall(msp, -200, 0, 0, 5200)                     # left
        _wall(msp, 8000, 0, 8200, 2000)                  # right, below door
        _wall(msp, 8000, 2900, 8200, 5200)               # right, above door
        _wall(msp, -200, 5000, 5000, 5200)               # top, left of alcove
        _wall(msp, 7000, 5000, 8200, 5200)               # top, right of alcove
        _wall(msp, 4800, 5000, 5000, 6700)               # alcove
        _wall(msp, 7000, 5000, 7200, 6700)
        _wall(msp, 4800, 6500, 7200, 6700)
        # The door: hinge on the room-side jamb, 900 mm swing and leaf.
        msp.add_arc((8000, 2000), 900, 90, 180, dxfattribs={"layer": "A-DOOR"})
        msp.add_line((8000, 2000), (7100, 2000), dxfattribs={"layer": "A-DOOR"})
        # Cubicle partitions (20 mm boards) and cubicle door leaves (12 mm).
        for x in (2000, 3000, 4000):
            _wall(msp, x, 0, x + 20, 1500, layer="0")
            _wall(msp, x + 170, 900, x + 182, 1500, layer="0")
        _text(msp, "MEN TOILET", 3000, 3000, h=250)
        _text(msp, "VANITY COUNTER", 7000, 1000, h=150)

        rooms, _stats = self._run(doc)
        self.assertIn("MEN TOILET", rooms)
        # 8 x 5 m plus the 2 x 1.5 m alcove (its opening strip included).
        self.assertAlmostEqual(rooms["MEN TOILET"]["area_sqm"], 43.0, delta=0.3)
        self.assertEqual(rooms["MEN TOILET"]["family"], "Toilet")
        self.assertNotIn("VANITY COUNTER", rooms)

    def test_room_in_the_corner_of_an_l_keeps_its_own_boundary(self):
        doc, msp = _new_doc()
        _l_corridor(msp)
        _wall(msp, 1800, 3800, 7200, 4000)
        _wall(msp, 1800, 9000, 7200, 9200)
        _wall(msp, 1800, 3800, 2000, 9200)
        _wall(msp, 7000, 3800, 7200, 9200)
        _text(msp, "CORRIDOR", 4000, 1000)
        _text(msp, "STORE", 4500, 6500)

        rooms, _stats = self._run(doc)
        self.assertAlmostEqual(rooms["CORRIDOR"]["area_sqm"], 36.0, delta=0.3)
        self.assertIn("STORE", rooms)
        self.assertAlmostEqual(rooms["STORE"]["area_sqm"], 25.0, delta=0.3)

    def test_two_tags_in_one_traced_room_stay_one_traced_room(self):
        doc, msp = _new_doc()
        _l_corridor(msp)
        _text(msp, "ST-01", 4000, 1000)
        _text(msp, "ST-02", 9000, 7000)

        rooms, stats = self._run(doc)
        self.assertEqual(len(rooms), 1, rooms)
        (room,) = rooms.values()
        # The rectangle around the L would be 100 sq.m.
        self.assertAlmostEqual(room["area_sqm"], 36.0, delta=0.3)
        statuses = sorted(stats["text_status_by_floor"]["F"].values())
        self.assertEqual(statuses, ["absorbed", "secondary"])


    def test_ramp_in_a_doorway_stays_out_of_the_room(self):
        """A-106 AHU rooms: a 2000 x 750 ramp inside a double door."""
        doc, msp = _new_doc()
        _wall(msp, -200, -200, 6200, 0)
        _wall(msp, -200, 0, 0, 5200)
        _wall(msp, 6000, 0, 6200, 5200)
        _wall(msp, -200, 5000, 2000, 5200)               # top, left of door
        _wall(msp, 4000, 5000, 6200, 5200)               # top, right of door
        # Double door, 2 x 1000 mm leaves hinged on the room-side jambs,
        # drawn standing open into the room - the leaves pair with the
        # ramp's sides as a wall would, which is how the jambs used to spike.
        msp.add_arc((2000, 5000), 1000, 270, 360, dxfattribs={"layer": "A-DOOR"})
        msp.add_arc((4000, 5000), 1000, 180, 270, dxfattribs={"layer": "A-DOOR"})
        msp.add_line((2080, 5000), (2080, 4000), dxfattribs={"layer": "A-DOOR"})
        msp.add_line((3920, 5000), (3920, 4000), dxfattribs={"layer": "A-DOOR"})
        # The ramp, on a layer of its own.
        msp.add_lwpolyline([(2000, 4250), (4000, 4250), (4000, 5000),
                            (2000, 5000)], close=True,
                           dxfattribs={"layer": "ELE-5"})
        _text(msp, "AHU ROOM", 3000, 2000)

        rooms, stats = self._run(doc)
        self.assertGreater(stats["threshold_lines"], 0)
        # 6 x 5 m less the 2 x 0.75 m ramp.
        self.assertAlmostEqual(rooms["AHU ROOM"]["area_sqm"], 28.5, delta=0.2)

    def test_doorless_opening_to_another_room_is_closed(self):
        """A store opening onto a corridor with no door: each keeps its own
        room, closed straight across the opening."""
        doc, msp = _new_doc()
        _wall(msp, -200, -200, 10200, 0)                 # bottom
        _wall(msp, -200, 4000, 10200, 4200)              # top
        _wall(msp, -200, 0, 0, 4000)                     # left
        _wall(msp, 10000, 0, 10200, 4000)                # right
        _wall(msp, 4000, 0, 4200, 1500)                  # party wall with a
        _wall(msp, 4000, 2400, 4200, 4000)               # 900 mm opening
        _text(msp, "STORE", 2000, 3000)
        _text(msp, "CORRIDOR", 7000, 3000)

        rooms, stats = self._run(doc)
        self.assertGreaterEqual(stats["traced_after_closing_gaps"], 1)
        self.assertAlmostEqual(rooms["STORE"]["area_sqm"], 16.0, delta=0.3)
        self.assertAlmostEqual(rooms["CORRIDOR"]["area_sqm"], 23.2, delta=0.3)


    def test_two_floors_on_one_sheet_are_read_separately(self):
        """A-107: two floor plates side by side on the same layer."""
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        plates = {"Floor_2": 0.0, "Floor_3": 70000.0}
        floors = []
        for name, x0 in plates.items():
            outline = [(x0, 0), (x0 + 60000, 0), (x0 + 60000, 40000),
                       (x0, 40000)]
            msp.add_lwpolyline(outline, close=True, dxfattribs={"layer": "0"})
            floors.append({"name": name, "polygon": outline})
            _wall(msp, x0 + 1000, 1000, x0 + 6200, 1200)
            _wall(msp, x0 + 1000, 5000, x0 + 6200, 5200)
            _wall(msp, x0 + 1000, 1000, x0 + 1200, 5200)
            _wall(msp, x0 + 6000, 1000, x0 + 6200, 5200)
            _text(msp, "STORE", x0 + 3500, 3000)
        src = os.path.join(self.tmp, "two.dxf")
        dst = os.path.join(self.tmp, "two_out.dxf")
        doc.saveas(src)
        dr.annotate_dxf(src, dst, floors, dr.extract_texts(src, floors),
                        project_name="Phoenix - 14")

        areas = dr.compute_room_areas(dst, "0")
        # The Step 1 project name comes back from the drawing's header.
        self.assertEqual(areas["project_name"], "Phoenix - 14")
        got = {f["name"]: f for f in areas["floors"]}
        self.assertEqual(set(got), {"Floor_2", "Floor_3"})
        self.assertAlmostEqual(areas["floor_area_sqm"], 4800.0, delta=0.1)
        for f in got.values():
            self.assertAlmostEqual(f["area_sqm"], 2400.0, delta=0.1)
            self.assertEqual(f["room_count"], 1)
        # Neither outline comes back as a 2,400 sq.m room.
        self.assertEqual(len(areas["rooms"]), 2)
        self.assertEqual(sorted(r["floor"] for r in areas["rooms"]),
                         ["Floor_2", "Floor_3"])
        # Each floor lists its own families.
        for f in got.values():
            self.assertEqual([(x["name"], x["count"]) for x in f["families"]],
                             [("Storage", 1)])
        # Quantities are reported floor by floor as well as together.
        from Drawing_Analyzer.quantities import build_quantity_report
        q = build_quantity_report(areas)
        self.assertEqual([f["name"] for f in q["floors"]],
                         [f["name"] for f in areas["floors"]])
        for f in q["floors"]:
            self.assertAlmostEqual(f["reconciliation"]["floor_area_sqm"],
                                   2400.0, delta=0.1)
            self.assertEqual(f["reconciliation"]["room_count"], 1)
        self.assertEqual(q["reconciliation"]["room_count"], 2)


    def test_fixture_labels_do_not_take_the_toilet_apart(self):
        """A-109 tower 2: fixture blocks labelled TB / EWC / WB inside a
        toilet. The toilet is traced and titled by its own name."""
        doc, msp = _new_doc()
        _wall(msp, -200, -200, 8200, 0)
        _wall(msp, -200, 5000, 8200, 5200)
        _wall(msp, -200, 0, 0, 5000)
        _wall(msp, 8000, 0, 8200, 5000)
        for x in (2000, 3000, 4000):                     # cubicle partitions
            _wall(msp, x, 0, x + 20, 1500, layer="0")
            _wall(msp, x + 170, 900, x + 182, 1500, layer="0")
        _text(msp, "LADIES TOILET", 4000, 3500, h=350)
        for x in (1500, 2500, 3500):                     # fixture labels
            msp.add_text("EWC", dxfattribs={"layer": "A-Fixtures",
                                            "height": 100, "insert": (x, 700)})
            msp.add_text("TB", dxfattribs={"layer": "A-Fixtures",
                                           "height": 75, "insert": (x, 300)})
        msp.add_text("WB", dxfattribs={"layer": "0", "height": 100,
                                       "insert": (7000, 4500)})

        rooms, _stats = self._run(doc)
        self.assertEqual(list(rooms), ["LADIES TOILET"])
        self.assertAlmostEqual(rooms["LADIES TOILET"]["area_sqm"], 40.0,
                               delta=0.3)


class FloorPlateTests(unittest.TestCase):

    def test_same_plate_drawn_three_times_is_listed_once(self):
        """T1: one slab edge on A-FLOOR PROJ twice, the glass line inside it
        on A-Glass, and a second plate beside them."""
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        slab = [(0, 0), (60000, 0), (60000, 40000), (0, 40000)]
        for layer in ("A-FLOOR PROJ", "A-FLOOR PROJ"):
            msp.add_lwpolyline(slab, close=True, dxfattribs={"layer": layer})
        msp.add_lwpolyline([(100, 100), (59900, 100), (59900, 39900),
                            (100, 39900)], close=True,
                           dxfattribs={"layer": "A-Glass"})
        msp.add_lwpolyline([(x + 80000, y) for x, y in slab], close=True,
                           dxfattribs={"layer": "A-FLOOR PROJ"})
        path = os.path.join(tempfile.mkdtemp(), "plates.dxf")
        doc.saveas(path)
        cands, _units, diag = dr.get_floor_candidates(path)
        self.assertEqual(len(cands), 2)
        self.assertEqual({c.layer for c in cands}, {"A-FLOOR PROJ"})
        self.assertEqual(diag["duplicate_plates_dropped"], 2)


class FloorRangeTests(unittest.TestCase):
    """Step 1's floor plate area range decides what Step 2 offers."""

    def setUp(self):
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        for i, side in enumerate((30000, 60000, 120000)):     # 900 / 3600 / 14400 sq.m
            x0 = i * 200000
            msp.add_lwpolyline([(x0, 0), (x0 + side, 0), (x0 + side, side),
                                (x0, side)], close=True,
                               dxfattribs={"layer": "FLOOR"})
        self.path = os.path.join(tempfile.mkdtemp(), "range.dxf")
        doc.saveas(self.path)

    def areas(self, **kw):
        cands, _u, _d = dr.get_floor_candidates(self.path, **kw)
        return sorted(round(c.area_sqm) for c in cands)

    def test_min_and_max_both_apply(self):
        self.assertEqual(self.areas(min_area_sqm=500, max_area_sqm=5000),
                         [900, 3600])
        self.assertEqual(self.areas(min_area_sqm=1000, max_area_sqm=20000),
                         [3600, 14400])

    def test_upload_requires_the_range(self):
        from Drawing_Analyzer import create_app
        client = create_app().test_client()

        def post(**form):
            with open(self.path, "rb") as fh:
                form["file"] = (fh, "range.dxf")
                return client.post("/upload", data=form,
                                   content_type="multipart/form-data").get_json()

        self.assertFalse(post()["ok"])
        self.assertFalse(post(min_area="5000", max_area="1000")["ok"])
        got = post(min_area="500", max_area="5000")
        self.assertTrue(got["ok"], got)
        self.assertEqual(sorted(round(c["area_sqm"]) for c in got["candidates"]),
                         [900, 3600])


class WallFilterTests(unittest.TestCase):
    """Door lines and centre marks never become wall faces."""

    def _faces(self, raw):
        return set(tuple(round(v) for v in f)
                   for f in dr._detect_walls(raw)["faces"])

    def test_door_leaf_does_not_pair_into_a_wall(self):
        raw = [_seg(0, 0, 0, 1000, "A-DOOR"), _seg(150, 0, 150, 1000, "ELE-5")]
        self.assertEqual(self._faces(raw), set())

    def test_line_in_a_door_block_is_not_a_wall(self):
        leaf = _seg(0, 0, 0, 1000, "0") + (True,)
        self.assertEqual(self._faces([leaf, _seg(150, 0, 150, 1000)]), set())

    def test_outdoor_is_not_a_door(self):
        raw = [_seg(0, 0, 5000, 0, "A-OUTDOOR-WALL"),
               _seg(0, 200, 5000, 200, "A-OUTDOOR-WALL")]
        self.assertEqual(len(self._faces(raw)), 2)

    def test_centre_mark_through_a_column_is_not_a_wall(self):
        col = "IEC-STEEL-COL"
        raw = (_rect(0, 0, 600, 600, col)
               + [_seg(300, -400, 300, 1000, col),       # the "+" through it,
                  _seg(-400, 300, 1000, 300, col)])      # 400 mm past each side
        faces = self._faces(raw)
        self.assertEqual(len(faces), 4)
        self.assertNotIn((300, -400, 300, 1000), faces)

    def test_centre_layer_is_never_a_wall(self):
        raw = [_seg(0, 0, 5000, 0, "CENTRE"), _seg(0, 200, 5000, 200, "CENTRE")]
        self.assertEqual(self._faces(raw), set())

    def test_line_ending_on_a_wall_stub_is_no_centre_mark(self):
        # An equipment outline whose end meets the middle of a 400 mm stub.
        raw = (_rect(0, 0, 400, 120, "a-wall")
               + [_seg(200, 120, 200, 336, "A-EQPM")])
        self.assertEqual(len(self._faces(raw)), 4)


class ThresholdTests(unittest.TestCase):
    """A 2000 mm opening along y = 5000, room below it."""

    OPENING = [(2000.0, 5000.0, 4000.0, 5000.0)]

    def _edges(self, raw, faces=()):
        th = dr._threshold_lines(self.OPENING, raw, faces)
        return [t for t in th if t[1] == t[3] and t[1] != 5000.0]

    def test_ramp_edge_closes_the_opening(self):
        ramp = _rect(2000, 4250, 4000, 5000, "ELE-5")
        edges = self._edges(ramp)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0][1], 4250.0)

    def test_a_line_with_no_side_is_not_a_ramp(self):
        self.assertEqual(self._edges([_seg(2000, 4250, 4000, 4250, "ELE-5")]),
                         [])

    def test_structure_is_not_a_ramp(self):
        col = _rect(2000, 4250, 4000, 5000, "A-COLUMN")
        self.assertEqual(self._edges(col), [])

    def test_something_standing_in_the_strip_is_not_a_ramp(self):
        ramp = _rect(2000, 4250, 4000, 5000, "ELE-5")
        column = [(2800, 4500, 3200, 4500), (3200, 4500, 3200, 4800)]
        self.assertEqual(self._edges(ramp, faces=column), [])


if __name__ == "__main__":
    unittest.main()
