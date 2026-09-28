# -*- coding: utf-8 -*-
"""Rules the user writes, in their own words.

Three parts, deliberately kept apart:

  parse()    a sentence  ->  a structured rule, or a list of questions
  apply()    structured rules + boundaries  ->  changed boundaries
  storage    the rules live in user_rules.json beside family_rules.json

Only the structured form ever reaches the drawing pass, so a run stays
deterministic and repeatable whatever was typed to produce the rule. The
parser is a controlled vocabulary - synonyms mapped onto the handful of
conditions and actions the geometry can actually carry out - not an
attempt at English. It is built to fail visibly: when it cannot place
part of a sentence it says which part and what it needs, rather than
guessing and quietly reshaping every boundary on the plate.

Swapping the parser for a model later changes nothing downstream: the
contract is the rule dict, and everything from apply() onwards is already
written against that.
"""

from __future__ import annotations

import math
import re
import uuid

# -- vocabulary -------------------------------------------------------------
# Each entry is (phrases, value). Longest phrase wins, so "not a rectangle"
# cannot be read as "rectangle".
_ACTION_WORDS = [
    (["angled rectangle", "rectangle at its own angle", "follow the angle",
      "along the wall angle", "angled box", "rotated rectangle"],
     ("rectangle", {"mode": "angled"})),
    (["upright rectangle", "straight rectangle", "axis rectangle",
      "bounding box", "regular shape", "regular shapes", "rectangle",
      "rectangles", "rectangular", "box", "boxes", "square shape",
      "square shapes", "regular"],
     ("rectangle", {"mode": "upright"})),
    (["do not draw", "dont draw", "don't draw", "never draw", "skip",
      "ignore", "leave out", "remove", "drop", "delete"],
     ("drop", {})),
    (["snap to", "snap"], ("snap", {})),
    (["grow", "expand", "enlarge", "oversize", "offset out"],
     ("grow", {"sign": 1})),
    (["shrink", "reduce", "inset", "offset in"], ("grow", {"sign": -1})),
    (["put under", "move to", "file under", "classify as", "treat as"],
     ("set_family", {})),
]

_COND_WORDS = [
    (["everything", "every boundary", "all boundaries", "all rooms",
      "always", "everywhere", "only", "all"], ("everything", {})),
    (["shaft mark", "shaft marks", "x mark", "x marks", "x shape",
      "x shapes", "x crossing"], ("source", {"source": "shaft"})),
    (["traced", "traced room", "traced rooms", "real shape"],
     ("source", {"source": "traced"})),
    (["ray-cast", "raycast", "ray cast", "boxed"], ("source", {"source": "box"})),
]

# Words that carry no meaning for a rule and would otherwise be read as a
# label to match on.
_STOPWORDS = {
    "a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "as",
    "is", "are", "be", "should", "must", "will", "shall", "please",
    "make", "draw", "drawn", "boundary", "boundaries", "room", "rooms",
    "it", "them", "their", "its", "with", "by", "only", "always", "just",
    "at", "from", "into", "up", "per", "each", "every", "all", "any",
    "give", "me", "my", "i", "want", "need", "use", "using", "keep",
    # units and shape words the action already accounted for, so they do
    # not come back as "I did not use..."
    "sq", "sqm", "sq.m", "m2", "mm", "m", "square", "metre", "metres",
    "meter", "meters", "shape", "shapes", "corner", "corners", "grid",
    "size", "sized", "family", "families", "mark", "marks", "anything",
    "something", "everything", "thing", "things", "type", "types",
}

_UNITS_SQM = ["sq.m", "sqm", "sq m", "square metres", "square meters", "m2"]
_UNITS_MM = ["mm", "millimetres", "millimeters"]
_UNITS_M = ["m", "metres", "meters"]

MAX_RULES = 60


# -- schema -----------------------------------------------------------------
def blank_rule(text=""):
    return {"id": uuid.uuid4().hex[:8], "text": text, "enabled": True,
            "condition": {"kind": "everything"}, "action": {"kind": "rectangle",
                                                            "mode": "upright"}}


def describe(rule):
    """The rule restated in plain words - what the UI shows back before it
    is saved, and what the run report prints."""
    c, a = rule.get("condition") or {}, rule.get("action") or {}
    ck, ak = c.get("kind"), a.get("kind")
    if ck == "everything":
        who = "every boundary"
    elif ck == "family":
        who = "boundaries in the %s family" % c.get("family")
    elif ck == "family_in":
        names = list(c.get("families") or [])
        if len(names) > 1:
            who = "boundaries in %s or %s" % (", ".join(names[:-1]), names[-1])
        else:
            who = "boundaries in %s" % (names[0] if names else "no family")
    elif ck == "label":
        who = "boundaries whose label contains '%s'" % c.get("label")
    elif ck == "layer":
        who = "boundaries on layer %s" % c.get("layer")
    elif ck == "area_below":
        who = "boundaries under %g sq.m" % c.get("value", 0)
    elif ck == "area_above":
        who = "boundaries over %g sq.m" % c.get("value", 0)
    elif ck == "source":
        who = {"shaft": "boundaries that came from an X mark",
               "traced": "boundaries that were traced",
               "box": "boundaries measured by rays"}.get(c.get("source"), "some")
    else:
        who = "some boundaries"

    if ak == "keep_only":
        # Reads backwards from the others: the condition says what to keep,
        # not what to act on.
        return "Draw only %s - drop everything else." % who
    if ak == "rectangle":
        what = ("redraw as a rectangle at the room's own angle"
                if a.get("mode") == "angled"
                else "redraw as an upright rectangle")
    elif ak == "drop":
        what = "do not draw at all"
    elif ak == "snap":
        what = "snap the corners to a %g mm grid" % a.get("mm", 0)
    elif ak == "grow":
        mm = a.get("mm", 0)
        what = ("grow by %g mm" % mm) if mm >= 0 else ("shrink by %g mm" % -mm)
    elif ak == "set_family":
        what = "file under %s" % a.get("family")
    else:
        what = "do something"
    return "For %s, %s." % (who, what)


# -- parsing ----------------------------------------------------------------
def _find_phrase(text, table):
    """Longest matching phrase from a table, with where it sat."""
    best = None
    for phrases, value in table:
        for phrase in phrases:
            idx = text.find(phrase)
            if idx < 0:
                continue
            if best is None or len(phrase) > len(best[0]):
                best = (phrase, value, idx)
    return best


def _number_before(text, units):
    """A number attached to one of `units`, e.g. '0.3 sq.m' or '25mm'."""
    for unit in units:
        m = re.search(r"(\d+(?:\.\d+)?)\s*" + re.escape(unit) + r"\b", text)
        if m:
            return float(m.group(1))
    return None


def _family_aliases(name):
    """What a family answers to in a sentence.

    Nobody types "MEP / Services"; they type "mep". So a family is also
    known by each word of its name that is long enough to stand alone.
    """
    low = name.strip().lower()
    out = {low}
    for token in re.findall(r"[a-z0-9]+", low):
        if len(token) >= 3:
            out.add(token)
    return sorted(out, key=len, reverse=True)


def _families_named(fragment, families):
    """Every family the fragment names, in the order it names them."""
    found = []
    for fam in families:
        for alias in _family_aliases(fam):
            # "draw the shafts" means the Shaft family.
            m = re.search(r"\b" + re.escape(alias) + r"(?:es|s)?\b", fragment)
            if m:
                found.append((m.start(), fam))
                break
    found.sort()
    seen, out = set(), []
    for _at, fam in found:
        if fam not in seen:
            seen.add(fam)
            out.append(fam)
    return out


def _condition_from(fragment, families, layers):
    """Read 'the Shaft family' / 'shaft, staircase and mep' /
    'TOILET_BOUNDARY' / 'x marks' into a condition.

    Every family the fragment names is collected, not just the first:
    "draw only staircase and mep boundaries" kept the staircases and
    dropped the MEP rooms without saying so, which is the one thing this
    parser is not allowed to do.
    """
    named = _families_named(fragment, families)
    if len(named) > 1:
        return {"kind": "family_in", "families": named}
    if len(named) == 1:
        return {"kind": "family", "family": named[0]}
    for lay in sorted(layers, key=len, reverse=True):
        if lay.lower() in fragment:
            return {"kind": "layer", "layer": lay}
    hit = _find_phrase(fragment, _COND_WORDS)
    if hit:
        _phrase, (kind, extra), _pos = hit
        if kind != "everything":
            cond = {"kind": kind}
            cond.update(extra)
            return cond
    return None


def parse(text, families=(), layers=()):
    """Turn a sentence into a rule.

    Returns {"ok", "rule", "summary", "questions", "unused"}. `questions`
    is what the sentence did not settle - the UI asks those rather than
    the parser picking for the user, because "small" and "regular" mean
    whatever the person typing them meant.
    """
    raw = (text or "").strip()
    low = " " + re.sub(r"\s+", " ", raw.lower()) + " "
    questions, used = [], []

    rule = blank_rule(raw)

    def _family_named(fragment):
        for fam in sorted(families, key=len, reverse=True):
            if fam.lower() in fragment:
                return fam
        return None

    if not raw:
        return {"ok": False, "rule": rule, "summary": "",
                "questions": ["Type the rule first."], "unused": []}

    # ---- "put X under Y" is its own shape of sentence: the family after
    # the verb is the target, and whatever came before it says which
    # boundaries are meant.
    move = re.match(r"\s*(?:put|move|file|classify|treat|send)\s+(.+?)\s+"
                    r"(?:under|into|to|as)\s+(.+?)\s*$", low)
    if move:
        target = _family_named(" " + move.group(2) + " ")
        subject = move.group(1).strip()
        if target:
            fam_subject = _family_named(" " + subject + " ")
            # "put LIFT LOBBY under Staircase" names a label, not the Lift
            # family: the subject says more than the family name it
            # happens to contain.
            if fam_subject and len(subject.split()) > len(fam_subject.split()):
                fam_subject = None
            if fam_subject and fam_subject != target:
                condition = {"kind": "family", "family": fam_subject}
            elif subject and subject not in ("everything", "all", "them"):
                condition = {"kind": "label", "label": subject.upper()}
            else:
                condition = {"kind": "everything"}
            rule["condition"] = condition
            rule["action"] = {"kind": "set_family", "family": target}
            return {"ok": True, "rule": rule, "summary": describe(rule),
                    "questions": [], "unused": []}
        questions.append("Which family should these go under? I did not "
                         "recognise '%s'." % move.group(2).strip())

    # ---- "draw only the shafts" keeps one kind and drops the rest. The
    # thing named after 'only' is what survives, so it is read as the
    # condition and the action is fixed.
    keep = re.match(r"\s*(?:draw|show|keep|export|generate|give|leave)?\s*"
                    r"only\s+(.+?)\s*$", low)
    if keep:
        what_survives = " " + keep.group(1).strip() + " "
        cond = _condition_from(what_survives, families, layers)
        if cond:
            rule["condition"] = cond
            rule["action"] = {"kind": "keep_only"}
            return {"ok": True, "rule": rule, "summary": describe(rule),
                    "questions": [], "unused": []}
        questions.append(
            "Only what? Name a family (%s), a layer, or a size."
            % ", ".join(list(families)[:4]))

    # ---- "draw the staircases" with no other verb is a restriction too:
    # everything is drawn by default, so the only thing naming a family can
    # add is "and nothing else". The restated sentence says so out loud.
    plain = re.match(r"\s*(?:draw|show|keep|export|generate)\s+(.+?)\s*$", low)
    if plain and not _find_phrase(low, _ACTION_WORDS):
        cond = _condition_from(" " + plain.group(1).strip() + " ",
                               families, layers)
        if cond:
            rule["condition"] = cond
            rule["action"] = {"kind": "keep_only"}
            return {"ok": True, "rule": rule, "summary": describe(rule),
                    "questions": ["Read as a restriction: these are drawn "
                                  "and everything else is dropped. Several "
                                  "of these rules add up, so a second one "
                                  "brings its own families back in."],
                    "unused": []}

    # ---- action
    hit = _find_phrase(low, _ACTION_WORDS)
    if not hit:
        return {
            "ok": False, "rule": rule, "summary": "",
            "questions": ["I could not tell what to do. Say one of: draw as "
                          "a rectangle, do not draw, snap to a grid, grow or "
                          "shrink by a distance, or file under a family."],
            "unused": [raw]}
    phrase, (kind, extra), _pos = hit
    used.append(phrase)
    action = {"kind": kind}
    action.update(extra)

    if kind == "rectangle" and "regular" in phrase:
        questions.append(
            "'Regular' can mean an upright rectangle or a rectangle turned "
            "to the room's own angle. I have assumed upright - switch it "
            "below if you meant angled.")
    if kind == "snap":
        mm = _number_before(low, _UNITS_MM)
        if mm is None:
            m_val = _number_before(low, _UNITS_M)
            mm = m_val * 1000.0 if m_val else None
        if mm is None:
            questions.append("How coarse should the grid be, in mm?")
            mm = 25.0
        action["mm"] = mm
    if kind == "grow":
        mm = _number_before(low, _UNITS_MM)
        if mm is None:
            m_val = _number_before(low, _UNITS_M)
            mm = m_val * 1000.0 if m_val else None
        if mm is None:
            questions.append("By how much, in mm?")
            mm = 100.0
        action["mm"] = mm * action.pop("sign", 1)
    if kind == "set_family":
        target = None
        for fam in families:
            if fam.lower() in low:
                target = fam
                used.append(fam.lower())
                break
        if not target:
            questions.append("Which family should these go under?")
        action["family"] = target or (families[0] if families else "Unknown")

    # ---- condition
    condition = None
    area_below = None
    for word in ("under", "below", "less than", "smaller than", "up to"):
        if word in low:
            area_below = _number_before(low, _UNITS_SQM)
            if area_below is not None:
                condition = {"kind": "area_below", "value": area_below}
                used.append(word)
            break
    if condition is None:
        for word in ("over", "above", "more than", "bigger than", "larger than"):
            if word in low:
                value = _number_before(low, _UNITS_SQM)
                if value is not None:
                    condition = {"kind": "area_above", "value": value}
                    used.append(word)
                break

    if condition is None:
        for fam in sorted(families, key=len, reverse=True):
            if fam.lower() in low:
                condition = {"kind": "family", "family": fam}
                used.append(fam.lower())
                break

    if condition is None:
        for lay in sorted(layers, key=len, reverse=True):
            if lay.lower() in low:
                condition = {"kind": "layer", "layer": lay}
                used.append(lay.lower())
                break

    if condition is None:
        hit_c = _find_phrase(low, _COND_WORDS)
        if hit_c:
            phrase_c, (ckind, cextra), _p = hit_c
            if ckind != "everything":
                condition = {"kind": ckind}
                condition.update(cextra)
                used.append(phrase_c)

    if condition is None:
        # A quoted or capitalised phrase left over is a label to match on.
        quoted = re.search(r"['\"]([^'\"]{2,40})['\"]", raw)
        if quoted:
            condition = {"kind": "label", "label": quoted.group(1).strip()}
            used.append(quoted.group(0).lower())

    if condition is None:
        condition = {"kind": "everything"}

    rule["condition"] = condition
    rule["action"] = action

    # ---- what the sentence said that nothing picked up
    leftovers = re.sub(r"\d+(?:\.\d+)?\s*[a-z.]*", " ", low)
    for token in used:
        leftovers = leftovers.replace(token, " ")
    unused = [w for w in re.findall(r"[a-z][a-z0-9\-/]+", leftovers)
              if w not in _STOPWORDS and not w.isdigit()]
    if unused:
        questions.append(
            "I did not use: %s. If that was meant to narrow the rule down, "
            "set the condition below." % ", ".join(sorted(set(unused))[:6]))

    return {"ok": True, "rule": rule, "summary": describe(rule),
            "questions": questions, "unused": sorted(set(unused))}


# -- geometry ---------------------------------------------------------------
def _hull(points):
    """Convex hull, Andrew's monotone chain."""
    pts = sorted(set((round(x, 6), round(y, 6)) for x, y in points))
    if len(pts) < 3:
        return pts

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2:
                (x1, y1), (x2, y2) = out[-2], out[-1]
                if (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1) > 0:
                    break
                out.pop()
            out.append(p)
        return out

    return half(pts)[:-1] + half(reversed(pts))[:-1]


def min_area_rectangle(points):
    """Smallest rectangle containing the points, at any angle.

    Rotating calipers over the hull: a room whose walls run at 30 degrees
    gets a rectangle at 30 degrees, which is the whole point of offering
    an angled rectangle at all.
    """
    hull = _hull(points)
    if len(hull) < 3:
        return list(points)
    best = None
    for i in range(len(hull)):
        x1, y1 = hull[i]
        x2, y2 = hull[(i + 1) % len(hull)]
        edge = math.hypot(x2 - x1, y2 - y1)
        if edge < 1e-9:
            continue
        ux, uy = (x2 - x1) / edge, (y2 - y1) / edge
        vx, vy = -uy, ux
        us = [(px - x1) * ux + (py - y1) * uy for px, py in hull]
        vs = [(px - x1) * vx + (py - y1) * vy for px, py in hull]
        area = (max(us) - min(us)) * (max(vs) - min(vs))
        if best is None or area < best[0]:
            best = (area, (x1, y1), (ux, uy), (vx, vy),
                    min(us), max(us), min(vs), max(vs))
    if best is None:
        return list(points)
    _a, (ox, oy), (ux, uy), (vx, vy), u0, u1, v0, v1 = best
    return [(ox + ux * u + vx * v, oy + uy * u + vy * v)
            for u, v in ((u0, v0), (u1, v0), (u1, v1), (u0, v1))]


def _bbox_of(shape):
    xs = [p[0] for p in shape]
    ys = [p[1] for p in shape]
    return (min(xs), min(ys), max(xs), max(ys))


def _rect_from_bbox(bbox):
    x0, y0, x1, y1 = bbox
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _area_sqm(shape):
    total = 0.0
    for i in range(len(shape)):
        x1, y1 = shape[i]
        x2, y2 = shape[(i + 1) % len(shape)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2e6


# -- applying ---------------------------------------------------------------
def _matches(rule, item):
    c = rule.get("condition") or {}
    kind = c.get("kind")
    if kind == "everything":
        return True
    if kind == "family":
        return (item.get("family") or "").strip().lower() == \
            (c.get("family") or "").strip().lower()
    if kind == "family_in":
        have = (item.get("family") or "").strip().lower()
        return any(have == str(f).strip().lower()
                   for f in (c.get("families") or []))
    if kind == "label":
        return (c.get("label") or "").strip().lower() in \
            (item.get("label") or "").lower()
    if kind == "layer":
        return (item.get("layer") or "").strip().upper() == \
            (c.get("layer") or "").strip().upper()
    if kind == "area_below":
        return item.get("area_sqm", 0.0) < float(c.get("value", 0))
    if kind == "area_above":
        return item.get("area_sqm", 0.0) > float(c.get("value", 0))
    if kind == "source":
        return item.get("source") == c.get("source")
    return False


def _restriction_groups(rules):
    """Walk the list, collecting consecutive enabled keep_only rules into
    one group. Anything else is yielded on its own, in order."""
    out, run = [], []
    for rule in rules:
        is_keep = (rule.get("enabled", True)
                   and (rule.get("action") or {}).get("kind") == "keep_only")
        if is_keep:
            run.append(rule)
            continue
        if run:
            out.append(run)
            run = []
        out.append(rule)
    if run:
        out.append(run)
    return out


def apply_rules(items, rules):
    """Run the rules over the boundaries, in order, first match per rule.

    `items` are dicts carrying at least shape (list of points), bbox,
    label, family, layer, source and area_sqm. Returns (items, report):
    the list with dropped entries removed, and a per-rule count of what
    each one touched, which is what the preview and the run report show.
    """
    report = []
    out = list(items)

    # A run of "draw only X" rules is one restriction naming several
    # things, not a sequence of them. Applied one after another they
    # would whittle the drawing down to nothing - the second rule only
    # ever sees what the first kept - and nobody writing "draw only the
    # shafts" then "draw only the stairs" means "draw neither".
    for group in _restriction_groups(rules or []):
        if isinstance(group, list):
            kept, dropped, before = [], 0, 0.0
            for item in out:
                if any(_matches(r, item) for r in group):
                    kept.append(item)
                else:
                    dropped += 1
                    before += _area_sqm(item.get("shape")
                                        or _rect_from_bbox(item["bbox"]))
            names = []
            for r in group:
                c = r.get("condition") or {}
                for name in (c.get("families") or
                             ([c.get("family")] if c.get("family") else [])):
                    # Two rules naming the same family are one family in
                    # the sentence that reports them.
                    if name not in names:
                        names.append(name)
            report.append({
                "id": group[0].get("id"),
                "text": " + ".join(r.get("text") or "" for r in group),
                "summary": (describe(group[0]) if len(group) == 1 else
                            "Draw only %s - drop everything else."
                            % (", ".join(names[:-1]) + " or " + names[-1]
                               if len(names) > 1 else (names or ["nothing"])[0])),
                "matched": dropped, "dropped": dropped,
                "area_before": round(before, 2), "area_after": 0.0})
            out = kept
            continue
        rule = group
        if not rule.get("enabled", True):
            continue
        action = rule.get("action") or {}
        kind = action.get("kind")
        touched, dropped, before, after = 0, 0, 0.0, 0.0
        keep = []
        for item in out:
            if kind == "keep_only":
                # The condition says what survives, so the rule acts on
                # everything it does NOT match.
                if _matches(rule, item):
                    keep.append(item)
                else:
                    dropped += 1
                    touched += 1
                    before += _area_sqm(item.get("shape")
                                        or _rect_from_bbox(item["bbox"]))
                continue
            if not _matches(rule, item):
                keep.append(item)
                continue
            shape = item.get("shape") or _rect_from_bbox(item["bbox"])
            before += _area_sqm(shape)
            if kind == "drop":
                dropped += 1
                touched += 1
                continue
            if kind == "rectangle":
                shape = (min_area_rectangle(shape)
                         if action.get("mode") == "angled"
                         else _rect_from_bbox(_bbox_of(shape)))
            elif kind == "snap":
                step = float(action.get("mm") or 0) or 1.0
                shape = [(round(x / step) * step, round(y / step) * step)
                         for x, y in shape]
            elif kind == "grow":
                mm = float(action.get("mm") or 0)
                cx = sum(p[0] for p in shape) / len(shape)
                cy = sum(p[1] for p in shape) / len(shape)
                moved = []
                for x, y in shape:
                    dx, dy = x - cx, y - cy
                    length = math.hypot(dx, dy) or 1.0
                    moved.append((x + dx / length * mm, y + dy / length * mm))
                shape = moved
            elif kind == "set_family":
                item = dict(item, family=action.get("family"),
                            family_forced=action.get("family"))
            item = dict(item, shape=shape, bbox=_bbox_of(shape),
                        area_sqm=_area_sqm(shape))
            after += item["area_sqm"]
            touched += 1
            keep.append(item)
        out = keep
        report.append({"id": rule.get("id"), "text": rule.get("text"),
                       "summary": describe(rule), "matched": touched,
                       "dropped": dropped,
                       "area_before": round(before, 2),
                       "area_after": round(after, 2)})
    return out, report


# -- storage ----------------------------------------------------------------
def validate(rules):
    """Raise ValueError with a message meant for the user."""
    if not isinstance(rules, list):
        raise ValueError("Send a list of rules.")
    if len(rules) > MAX_RULES:
        raise ValueError("That is more than %d rules." % MAX_RULES)
    cleaned = []
    for r in rules:
        if not isinstance(r, dict):
            raise ValueError("Each rule must be an object.")
        action = r.get("action") or {}
        condition = r.get("condition") or {}
        if action.get("kind") not in ("rectangle", "drop", "snap", "grow",
                                      "set_family", "keep_only"):
            raise ValueError("Unknown action '%s'." % action.get("kind"))
        if condition.get("kind") not in ("everything", "family", "label",
                                         "family_in", "layer", "area_below",
                                         "area_above", "source"):
            raise ValueError("Unknown condition '%s'." % condition.get("kind"))
        cleaned.append({
            "id": str(r.get("id") or uuid.uuid4().hex[:8])[:32],
            "text": str(r.get("text") or "")[:400],
            "enabled": bool(r.get("enabled", True)),
            "condition": condition,
            "action": action,
        })
    return cleaned


# -- the file the rules live in ---------------------------------------------
import json                                                   # noqa: E402
import os                                                     # noqa: E402


def rules_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "user_rules.json")


def load_rules(path=None):
    """Whatever is on disk, or nothing. A broken file is not a reason to
    refuse a drawing run - it is a reason to draw without the rules."""
    path = path or rules_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return validate(data.get("rules") if isinstance(data, dict) else data)
    except (OSError, ValueError, TypeError):
        return []


def save_rules(rules, path=None):
    path = path or rules_path()
    cleaned = validate(rules)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"_comment": "Rules written in the UI. Each is a condition "
                               "and an action; the sentence that produced it "
                               "is kept in 'text' so it can be read back.",
                   "rules": cleaned}, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return cleaned
