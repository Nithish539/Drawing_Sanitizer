/* Step 4 - Geometry (AI): everything found from the shape of the drawing.
 *
 * The geometry engine (geoengine.py, /geoengine/run) classifies every X
 * mark, staircase, toilet, enclosed room and outline without reading the
 * room texts; every enclosed room comes as "Room N" for the user to name.
 * The drawing is the editor:
 *
 *   Select   click a shape (Shift+click adds); the panel on the right sets
 *            its family, its name, and whether it is drawn
 *   Trace    click inside a space the detection missed: its boundary is
 *            traced from the walls (/geometry/trace)
 *   Draw     click corners, double-click / Enter / click the first corner
 *            to close - for spaces with no walls at all (/geometry/describe)
 *   Move     drag a text; the original text moves in the output drawing,
 *            and dropped inside a shape it names that shape
 *   Merge    joins selected neighbours across the wall between them
 *
 * Undo/redo, autosave (this browser, per upload), save/load to a file,
 * a Layers panel (show outlines by layer, map a layer to a family) and a
 * list view. "Generate geometry boundaries" (/geoengine/generate) writes
 * the geometry annotated DXF from what is ticked here; Step 3 (texts) is
 * not affected.
 */
(function () {
    "use strict";

    var $ = function (id) { return document.getElementById(id); };
    var pane = $("geometryPane");
    if (!pane) { return; }

    var KEY_FAMILY = { s: "Shaft", l: "Lift", t: "Toilet", r: "Staircase", v: "Void",
                       e: "Electrical", u: "Unknown" };

    var st = {
        token: null, data: null, floor: null,
        created: {},          // floor -> [items the user traced / drew / merged]
        dec: {},              // floor -> id -> {include, family, label, label_sr}
        moves: {},            // floor -> sr_no -> [dx, dy]
        layerVis: {},         // floor -> layer -> bool
        gone: {},             // floor -> id -> true (merged into another)
        sel: [],              // ids selected on the current floor
        mode: "select", draft: null,
        undo: [], redo: [],
        viewers: {}, colours: {}, gen: 0, seq: 0
    };

    // ---- helpers -------------------------------------------------------------
    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }
    function one(s) { return String(s || "").replace(/\s*\n\s*/g, " / "); }
    function fmt2(v) {
        return Number(v || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    function setStatus(msg, kind) {
        var el = $("geoStatus");
        el.textContent = msg || "";
        el.className = "status " + (kind || "");
    }
    function colour(f) { return st.colours[f] || "#adb5bd"; }
    function hexA(hex, a) {
        var h = String(hex || "#adb5bd").replace("#", "");
        return "rgba(" + parseInt(h.slice(0, 2), 16) + "," + parseInt(h.slice(2, 4), 16) + "," +
            parseInt(h.slice(4, 6), 16) + "," + a + ")";
    }
    function inPoly(x, y, pts) {
        var inside = false;
        for (var i = 0, j = pts.length - 1; i < pts.length; j = i++) {
            var xi = pts[i][0], yi = pts[i][1], xj = pts[j][0], yj = pts[j][1];
            if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / ((yj - yi) || 1e-12) + xi)) {
                inside = !inside;
            }
        }
        return inside;
    }
    async function post(url, body) {
        var res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                     body: JSON.stringify(body) });
        var data;
        try { data = await res.json(); } catch (e) {
            throw new Error("the server answered HTTP " + res.status + ". Restart the server if it runs older code.");
        }
        return data;
    }

    // ---- the model -----------------------------------------------------------
    function items(f) {
        f = f || st.floor;
        if (!st.data) { return []; }
        var gone = st.gone[f] || {};
        return (st.data.floors[f] || []).concat(st.created[f] || []).filter(function (it) {
            return !gone[it.id];
        });
    }
    function byId(id, f) {
        return items(f).find(function (it) { return it.id === id; });
    }
    function dec(it, f) {
        f = f || st.floor;
        var d = st.dec[f][it.id];
        if (!d) {
            d = st.dec[f][it.id] = defaultDecision(it);
        }
        return d;
    }
    // Everything with a family is drawn; a shape of no known family waits
    // for the user to confirm it (see the Unknown prompt in the summary).
    function defaultDecision(it) {
        var include = (it.kind === "outline" && it.hidden_layer) ? false
            : (it.family !== "Unknown" && it.family !== "Column");
        if (it.kind === "traced" || it.kind === "drawn" || it.kind === "merged") { include = true; }
        return { include: include, family: it.family, label: it.label, label_sr: it.label_sr };
    }
    function texts(f) {
        var all = window.DA_step3Results ? window.DA_step3Results() : {};
        return all[f || st.floor] || [];
    }
    function layerVisible(it, f) {
        if (it.kind !== "outline") { return true; }
        var vis = st.layerVis[f || st.floor] || {};
        return (it.layers || []).some(function (l) { return vis[l] !== false; });
    }

    // ---- undo / redo / autosave ---------------------------------------------
    function snapshot() {
        return JSON.stringify({ created: st.created, dec: st.dec, moves: st.moves,
                                layerVis: st.layerVis, gone: st.gone, seq: st.seq,
                                unknownAnswer: st.unknownAnswer || null });
    }
    function restore(json) {
        var o = JSON.parse(json);
        st.created = o.created || {};
        st.dec = o.dec || {};
        st.moves = o.moves || {};
        st.layerVis = o.layerVis || {};
        st.gone = o.gone || {};
        st.seq = o.seq || 0;
        st.unknownAnswer = o.unknownAnswer || null;
        Object.keys(st.data.floors).forEach(function (f) {
            st.dec[f] = st.dec[f] || {};
            st.created[f] = st.created[f] || [];
            st.moves[f] = st.moves[f] || {};
            st.gone[f] = st.gone[f] || {};
            st.layerVis[f] = st.layerVis[f] || {};
        });
    }
    function change(fn) {
        st.undo.push(snapshot());
        if (st.undo.length > 80) { st.undo.shift(); }
        st.redo = [];
        fn();
        afterChange();
    }
    function afterChange() {
        st.sel = st.sel.filter(function (id) { return byId(id); });
        refresh();
        autosave();
    }
    function undo() {
        if (!st.undo.length) { return; }
        st.redo.push(snapshot());
        restore(st.undo.pop());
        afterChange();
    }
    function redo() {
        if (!st.redo.length) { return; }
        st.undo.push(snapshot());
        restore(st.redo.pop());
        afterChange();
    }
    var saveTimer = null;
    function storeKey() { return "da-geo-edit3:" + st.token; }
    function autosave() {
        clearTimeout(saveTimer);
        saveTimer = setTimeout(function () {
            try { localStorage.setItem(storeKey(), snapshot()); } catch (e) { /* full or blocked */ }
        }, 400);
    }

    // ---- the geometry engine's findings -------------------------------------------
    var FAMILY_OF_CLASS = { COLUMN: "Column", LIFT_LOBBY: "Lift Lobby", MACHINE_ROOM: "Machine Room",
                            SERVICE_LOBBY: "Service Lobby", LIFT: "Lift", SHAFT: "Shaft", STAIRCASE: "Staircase", STAIR_WELL: "Shaft",
                            VOID: "Void", TOILET: "Toilet", ROOM: "Room", UNKNOWN_GEOMETRY: "Unknown" };
    var SOURCE = { x: "X mark", stair: "tread lines", toilet: "fixtures", outline: "closed outline",
                   space: "enclosed space" };
    var GROUP_ORDER = ["Room", "Lift", "Lift Lobby", "Service Lobby", "Machine Room", "Shaft", "Staircase", "Toilet", "Void", "Column"];
    function nice(c) {
        return String(c || "").replace(/_/g, " ").toLowerCase().replace(/(^|\s)\S/g, function (m) { return m.toUpperCase(); });
    }
    function famOf(r) { return FAMILY_OF_CLASS[r.cls] || "Unknown"; }
    function toItem(r) {
        var f = r.features || {};
        var b = f.bbox;
        if (!b) {
            var xs = r.poly.map(function (p) { return p[0]; }), ys = r.poly.map(function (p) { return p[1]; });
            b = [Math.min.apply(null, xs), Math.min.apply(null, ys), Math.max.apply(null, xs), Math.max.apply(null, ys)];
        }
        return { id: r.id, kind: f.source || "space", cls: r.cls, row: r, bbox: b, poly: r.poly,
                 layers: f.layers || [], note: r.reason || "", area_sqm: r.area_sqm,
                 size_mm: [Math.round(b[2] - b[0]), Math.round(b[3] - b[1])], texts: [],
                 // a finding read from the Step 3 result store carries Step 3's family as its category
                 label: r.label, label_sr: (r.label_sr == null ? null : r.label_sr), family: r.category || famOf(r), hidden_layer: false,
                 foundAs: nice(r.cls) + (r.sub ? " (" + nice(r.sub) + ")" : "") + " \u00b7 " + (SOURCE[f.source] || f.source || "") };
    }
    function fromEngine(res) {
        var floors = {}, layers = {};
        Object.keys(res.floors).forEach(function (f) {
            floors[f] = res.floors[f].map(toItem);
            var n = {};
            floors[f].forEach(function (it) {
                if (it.kind === "outline" && it.layers[0]) { n[it.layers[0]] = (n[it.layers[0]] || 0) + 1; }
            });
            layers[f] = Object.keys(n).sort(function (a, b) { return n[b] - n[a]; }).map(function (L) {
                return { layer: L, count: n[L], visible: true };
            });
        });
        var fams = (res.families_list || []).map(function (x) { return x.name; });
        if (fams.indexOf("Room") < 0) { fams.unshift("Room"); }
        return { floors: floors, layers: layers, families: fams, group_order: GROUP_ORDER,
                 rules: res.rules || {}, profile_source: res.profile_source || "" };
    }

    // Runs by itself when the step is opened (and again after a new
    // extraction, while it is open); "Analyse again" runs it afresh.
    async function detect(rerun) {
        var s3 = window.DA_step3State ? window.DA_step3State() : {};
        if (!s3.token) { setStatus("Extract the texts first (Steps 1-3).", "error"); return false; }
        if (st.data && st.token === s3.token && !rerun) { return true; }
        // Already running (Fast track, or the step opened meanwhile): the
        // caller waits for that run rather than starting another.
        if (st.busy) { return st.running; }
        st.running = detectRun(rerun, s3);
        return st.running;
    }
    async function detectRun(rerun, s3) {
        st.busy = true;
        setStatus("Analysing the geometry - X marks, tread lines, fixtures, every enclosed room - without " +
                  "reading any room text. Large drawings take 1-3 minutes...", "busy");
        try {
            var res = await post("/geoengine/run", { token: s3.token, rerun: !!rerun });
            if (!res.ok) {
                setStatus(res.error || "Could not analyse the geometry.", "error");
                if (window.DA_flowNote) { window.DA_flowNote(res.error, !!res.stopped); }
                return false;
            }
            var data = fromEngine(res.result);
            clearViewers();
            st.token = s3.token;
            st.data = data;
            $("gaiDlDxf").href = "/download/geo_dxf/" + s3.token;
            $("gaiDlJson").href = "/download/geo_json/" + s3.token;
            paintAbout();
            st.created = {}; st.dec = {}; st.moves = {}; st.layerVis = {}; st.gone = {};
            st.undo = []; st.redo = []; st.sel = []; st.seq = 0; st.unknownAnswer = null;
            Object.keys(data.floors).forEach(function (f) {
                st.created[f] = []; st.dec[f] = {}; st.moves[f] = {}; st.gone[f] = {};
                st.layerVis[f] = {};
                ((data.layers || {})[f] || []).forEach(function (L) { st.layerVis[f][L.layer] = L.visible; });
            });
            var restored = false;
            try {
                var saved = localStorage.getItem(storeKey());
                if (saved) { restore(saved); restored = true; }
            } catch (e) { /* nothing saved */ }
            var sel = $("geoFloor");
            sel.innerHTML = Object.keys(data.floors).map(function (f) {
                return "<option>" + esc(f) + "</option>";
            }).join("");
            st.floor = sel.value;
            $("geoControls").classList.remove("hidden");
            $("geoSplit").classList.remove("hidden");
            await loadColours();
            refresh();
            var n = 0;
            Object.keys(data.floors).forEach(function (f) { n += data.floors[f].length; });
            setStatus("Found " + n + " shapes." + (restored ? " Your earlier edits on this upload were restored (Reset forgets them)." : "") +
                      " Rooms are named Room 1, Room 2 ... - click one to give it a family and a name.", "ok");
            document.dispatchEvent(new CustomEvent("da:stage", { detail: { name: "geometry" } }));
            return true;
        } catch (err) {
            setStatus("Error: " + err.message, "error");
            if (window.DA_flowNote) { window.DA_flowNote("Error: " + err.message); }
            return false;
        } finally {
            st.busy = false;
        }
    }
    var section = $("step-geoai");
    function paneOpen() { return section.classList.contains("pane-on"); }
    new MutationObserver(function () {
        // Fast track runs this step itself, in its own order.
        if (window.DA_flowBusy && window.DA_flowBusy()) { return; }
        if (paneOpen()) { detect(false); }
    }).observe(section, { attributes: true, attributeFilter: ["class"] });
    $("gaiRerunBtn").addEventListener("click", function () { detect(true); });

    async function loadColours() {
        if (Object.keys(st.colours).length) { return; }
        try {
            var r = await (await fetch("/cadview/" + st.token + "/plates")).json();
            (r.families || []).forEach(function (f) { st.colours[f.name] = f.rgb; });
            if (!st.colours.Room) { st.colours.Room = "#fab005"; }
        } catch (e) { /* grey it is */ }
    }

    $("geoFloor").addEventListener("change", function () { selectFloor(this.value); });

    function selectFloor(f, keepDrawing) {
        if (st.floor !== f) { st.sel = []; cancelDraft(); }
        st.floor = f;
        $("geoFloor").value = f;
        refresh();
        if (!keepDrawing) { showFloor(f); }
    }
    function showFloor(f) {
        var target = null;
        document.querySelectorAll("#geoDrawings .geo-floor-view").forEach(function (el) {
            var on = el.dataset.floor === f;
            el.classList.toggle("active", on);
            if (on) { target = el; }
        });
        if (target) {
            var col = target.closest(".geo-left");
            col.scrollTo({ top: target.offsetTop - col.offsetTop - 50, behavior: "smooth" });
        }
    }

    // ---- tools -----------------------------------------------------------------
    var HINTS = {
        select: "Click a shape to select it - Shift+click to add more. Drag the drawing to pan, scroll to zoom.",
        trace: "Click inside a space to trace its boundary from the surrounding walls.",
        draw: "Click each corner. Double-click, press Enter, or click the first corner to finish. Esc cancels.",
        move: "Drag a text to where it belongs. Dropped inside a shape, it becomes that shape's name."
    };
    function setMode(m) {
        cancelAsk();
        st.mode = m;
        // Ctrl+Z right after picking a tool (nothing done with it yet)
        // puts the tool down again.
        st.modeMark = st.undo.length;
        cancelDraft();
        document.querySelectorAll("#geoToolbar [data-mode]").forEach(function (b) {
            b.classList.toggle("on", b.dataset.mode === m);
        });
        Object.keys(st.viewers).forEach(function (f) { st.viewers[f].setMode(m); });
        $("geoHint").textContent = HINTS[m] +
            (m === "select" ? "" : " Click the tool again (or Esc / Ctrl+Z) to go back to Select.");
        paintViewers();
        undoState();
    }
    document.querySelectorAll("#geoToolbar [data-mode]").forEach(function (b) {
        // Clicking the tool that is on again puts it down (back to Select).
        b.addEventListener("click", function () {
            setMode(st.mode === b.dataset.mode ? "select" : b.dataset.mode);
        });
    });
    $("geoMergeBtn").addEventListener("click", mergeSelected);
    $("geoDeleteBtn").addEventListener("click", deleteSelected);
    $("geoUndoBtn").addEventListener("click", undoStep);

    // Undo, one step at a time: a pending question, then the last corner
    // of an outline being drawn, then - when the tool was only just picked
    // - the tool itself, and otherwise the last change.
    function undoStep() {
        if (cancelAsk()) { return; }
        if (st.draft && st.draft.pts.length) {
            st.draft.pts.pop();
            var v = st.viewers[st.draft.floor];
            if (!st.draft.pts.length) { cancelDraft(); } else if (v) { v.setDraft({ pts: st.draft.pts, cursor: null }); }
            return;
        }
        if (st.mode !== "select" && st.undo.length <= (st.modeMark || 0)) {
            setMode("select");
            setStatus("Back to Select.", "ok");
            return;
        }
        undo();
    }
    function undoState() {
        $("geoUndoBtn").disabled = !st.undo.length && st.mode === "select" && !st.draft;
    }
    $("geoRedoBtn").addEventListener("click", redo);

    function cancelDraft() {
        st.draft = null;
        Object.keys(st.viewers).forEach(function (f) { st.viewers[f].setDraft(null); });
    }

    async function traceAt(f, w) {
        setStatus("Tracing the space at that point...", "busy");
        try {
            var r = await post("/geometry/trace", { token: st.token, floor: f, x: w[0], y: w[1] });
            if (!r.ok) { setStatus(r.error || "Nothing to trace here.", "error"); return; }
            addCreated(f, r.item, "Traced a " + fmt2(r.item.area_sqm) + " sq.m space.");
        } catch (e) { setStatus("Error: " + e.message, "error"); }
    }

    function addCreated(f, item, msg) {
        change(function () {
            st.seq += 1;
            item.id = "u-" + st.seq;
            st.created[f].push(item);
            st.dec[f][item.id] = defaultDecision(item);
            if (f !== st.floor) { st.floor = f; $("geoFloor").value = f; }
            st.sel = [item.id];
        });
        setStatus(msg + " It is selected - set its family and name on the right.", "ok");
    }

    function draftClick(f, w) {
        var v = st.viewers[f];
        if (!st.draft || st.draft.floor !== f) { st.draft = { floor: f, pts: [] }; }
        var pts = st.draft.pts;
        if (pts.length >= 3) {
            var s = v.screenScale();
            if (Math.hypot(w[0] - pts[0][0], w[1] - pts[0][1]) * s < 10) { finishDraft(); return; }
        }
        pts.push([w[0], w[1]]);
        v.setDraft({ pts: pts, cursor: null });
        undoState();
    }

    async function finishDraft() {
        if (!st.draft) { return; }
        var f = st.draft.floor;
        var pts = st.draft.pts.slice();
        cancelDraft();
        // A double-click also lands two single clicks on the same spot.
        pts = pts.filter(function (p, i) {
            return i === 0 || Math.hypot(p[0] - pts[i - 1][0], p[1] - pts[i - 1][1]) > 1;
        });
        if (pts.length < 3) { setStatus("An outline needs at least three corners.", "error"); return; }
        try {
            var r = await post("/geometry/describe", { token: st.token, floor: f, poly: pts });
            if (!r.ok) { setStatus(r.error || "Could not add this outline.", "error"); return; }
            addCreated(f, r.item, "Drew a " + fmt2(r.item.area_sqm) + " sq.m outline.");
        } catch (e) { setStatus("Error: " + e.message, "error"); }
    }

    async function mergeSelected() {
        var chosen = st.sel.map(function (id) { return byId(id); }).filter(Boolean);
        if (chosen.length < 2) { setStatus("Select two or more neighbouring shapes (Shift+click), then Merge.", "error"); return; }
        try {
            var r = await post("/geometry/merge", { token: st.token, floor: st.floor,
                                                    polys: chosen.map(function (it) { return it.poly; }) });
            if (!r.ok) { setStatus(r.error, "error"); return; }
            var f = st.floor;
            var fam = dec(chosen[0]).family;
            change(function () {
                chosen.forEach(function (it) { st.gone[f][it.id] = true; });
                st.seq += 1;
                r.item.id = "u-" + st.seq;
                st.created[f].push(r.item);
                var d = defaultDecision(r.item);
                if (d.family === "Unknown" && fam) { d.family = fam; }
                st.dec[f][r.item.id] = d;
                st.sel = [r.item.id];
            });
            setStatus("Merged " + chosen.length + " shapes into one of " + fmt2(r.item.area_sqm) + " sq.m.", "ok");
        } catch (e) { setStatus("Error: " + e.message, "error"); }
    }

    // Deleting asks once, right where it was asked for (the row, or the
    // toolbar): the shape is gone from the list and the drawing, and at
    // Generate its boundary is not drawn - not even from a text inside it.
    var pendingAsk = null;
    function askInline(host, question, onYes) {
        cancelAsk();
        var before = host.innerHTML;
        host.classList.add("geo-asking");
        host.innerHTML = '<span class="geo-ask">' + question +
            ' <button type="button" class="btn btn-mini geo-ask-yes">Yes, delete</button>' +
            ' <button type="button" class="btn btn-ghost btn-mini geo-ask-no">Cancel</button></span>';
        pendingAsk = function () {
            pendingAsk = null;
            host.classList.remove("geo-asking");
            host.innerHTML = before;
        };
        host.querySelector(".geo-ask-yes").addEventListener("click", function (e) {
            e.stopPropagation();
            cancelAsk();
            onYes();
        });
        host.querySelector(".geo-ask-no").addEventListener("click", function (e) {
            e.stopPropagation();
            cancelAsk();
        });
        host.querySelector(".geo-ask-yes").focus();
    }
    function cancelAsk() {
        if (pendingAsk) { pendingAsk(); return true; }
        return false;
    }
    function deleteIds(ids, host) {
        ids = ids.filter(function (id) { return byId(id); });
        if (!ids.length) { return; }
        var what = ids.length === 1 ? "Delete this boundary?" : "Delete " + ids.length + " boundaries?";
        askInline(host || $("geoHint"), what, function () { doDelete(ids); });
    }
    function doDelete(ids) {
        var f = st.floor;
        change(function () {
            ids.forEach(function (id) {
                if (id.indexOf("u-") === 0) {
                    st.created[f] = st.created[f].filter(function (x) { return x.id !== id; });
                } else {
                    st.gone[f][id] = "del";
                }
            });
            st.sel = st.sel.filter(function (id) { return ids.indexOf(id) < 0; });
        });
        setStatus(ids.length + " shape(s) deleted - no boundary will be drawn there (Ctrl+Z to undo).", "ok");
    }
    function deleteSelected() {
        deleteIds(st.sel.slice());
    }

    function setFamily(fam) {
        if (!st.sel.length) { return; }
        change(function () {
            st.sel.forEach(function (id) {
                var it = byId(id);
                if (it) { var d = dec(it); d.family = fam; d.include = true; }
            });
        });
    }

    // ---- viewers -----------------------------------------------------------------
    function clearViewers() {
        st.gen++;
        Object.keys(st.viewers).forEach(function (f) { st.viewers[f].clear(); });
        st.viewers = {};
        $("geoDrawings").innerHTML = "";
    }

    function ensureViewers() {
        var host = $("geoDrawings");
        var floors = Object.keys(st.data.floors);
        host.classList.toggle("multi", floors.length > 1);
        floors.forEach(function (f) {
            if (st.viewers[f]) { return; }
            var box = document.createElement("div");
            box.className = "geo-floor-view";
            box.dataset.floor = f;
            box.innerHTML = '<div class="geo-floor-title" title="Work on this floor">' + esc(f) +
                ' <span class="muted small">' + st.data.floors[f].length + " shapes found</span></div>" +
                '<div class="geo-cad"></div>';
            host.appendChild(box);
            box.querySelector(".geo-floor-title").addEventListener("click", function () { selectFloor(f); });
            var v = st.viewers[f] = window.DA_cad(box.querySelector(".geo-cad"), { labels: false });
            v.setMode(st.mode);
            v.onClick(function (item, w, e) { onCanvasClick(f, item, w, e); });
            v.onDblClick(function () {
                if (st.mode === "draw" && st.draft) { finishDraft(); return true; }
                return false;
            });
            v.onMove(function (w) {
                if (st.mode === "draw" && st.draft && st.draft.floor === f) {
                    v.setDraft({ pts: st.draft.pts, cursor: w });
                }
            });
            v.onDragEnd(function (mark, dx, dy) { onTextMoved(f, mark, dx, dy); });
            loadFloor(f, v, st.gen);
        });
    }

    function onCanvasClick(f, item, w, e) {
        if (st.floor !== f) { selectFloor(f, true); }
        if (st.mode === "trace") { traceAt(f, w); return; }
        if (st.mode === "draw") { draftClick(f, w); return; }
        if (st.mode === "move") { return; }
        var id = item && item.geoId;
        if (!id) {
            if (st.sel.length) { st.sel = []; refresh(); }
            return;
        }
        if (e && (e.shiftKey || e.ctrlKey || e.metaKey)) {
            var k = st.sel.indexOf(id);
            if (k >= 0) { st.sel.splice(k, 1); } else { st.sel.push(id); }
        } else {
            st.sel = [id];
        }
        refresh();
        scrollToRow(id);
    }

    function onTextMoved(f, mark, dx, dy) {
        var sr = mark.sr;
        var named = null;
        change(function () {
            var cur = st.moves[f][sr] || [0, 0];
            st.moves[f][sr] = [cur[0] + dx, cur[1] + dy];
            // Dropped inside a shape: that text now names it.
            var hit = null;
            items(f).forEach(function (it) {
                if (!layerVisible(it, f)) { return; }
                var b = it.bbox;
                if (mark.x < b[0] || mark.x > b[2] || mark.y < b[1] || mark.y > b[3]) { return; }
                if (inPoly(mark.x, mark.y, it.poly) && (!hit || it.area_sqm < hit.area_sqm)) { hit = it; }
            });
            if (hit) {
                var d = dec(hit, f);
                d.label = mark.text;
                d.label_sr = sr;
                d.include = true;
                if (d.family === "Unknown" && mark.family && mark.family !== "Unknown") { d.family = mark.family; }
                st.sel = [hit.id];
                named = d.family;
            }
        });
        setStatus("Moved “" + one(mark.text).slice(0, 40) + "” - the original text moves in the output drawing" +
                  (named ? ", and it now names the " + named + " shape it was dropped in." : "."), "ok");
    }

    async function loadFloor(f, v, gen) {
        var xs = [], ys = [];
        st.data.floors[f].forEach(function (it) {
            if (it.kind === "outline" && it.hidden_layer) { return; }
            it.poly.forEach(function (p) { xs.push(p[0]); ys.push(p[1]); });
        });
        var box = xs.length ? [Math.min.apply(null, xs), Math.min.apply(null, ys),
                               Math.max.apply(null, xs), Math.max.apply(null, ys)] : undefined;
        if (box) { v.setHome(box); }
        await window.DA_cadLoad(v, "/cadview/" + st.token + "/source/scene",
                                "/cadview/" + st.token + "/source/lines",
                                { box: box, isStale: function () { return gen !== st.gen; } });
        if (box && gen === st.gen) { v.fit(box); }
    }

    function paintViewers() {
        if (!st.data) { return; }
        ensureViewers();
        Object.keys(st.data.floors).forEach(function (f) {
            var v = st.viewers[f];
            var sel = f === st.floor ? st.sel : [];
            var polys = items(f).filter(function (it) { return layerVisible(it, f); }).map(function (it) {
                var d = dec(it, f);
                var c = colour(d.family);
                var on = sel.indexOf(it.id) >= 0;
                return {
                    id: "geo-" + it.id, geoId: it.id, pts: it.poly,
                    stroke: on ? "#ffd43b" : (d.include ? c : (it.kind === "space" ? "rgba(173,181,189,0.55)" : "#868e96")),
                    width: on ? 3.5 : (d.include ? 2 : 1),
                    fill: on ? hexA(c, 0.35) : (d.include ? hexA(c, 0.2) : null),
                    dash: !d.include && !on,
                    label: d.include ? (one(d.label) || d.family) : "",
                    info: "<b>" + esc(one(d.label) || "(no name)") + "</b>" +
                        "<span>" + esc(d.family) + " &middot; " + fmt2(it.area_sqm) + " sq.m &middot; " + esc(kindName(it)) + "</span>" +
                        (it.layers && it.layers.length ? "<span>Layers: " + esc(it.layers.join(", ")) + "</span>" : "") +
                        "<span class='cad-muted'>" + (d.include ? "Drawn from your selection" : "Not selected - drawn from its text, if any") + "</span>"
                };
            });
            var moves = st.moves[f] || {};
            var showTexts = st.mode === "move";
            var marks = texts(f).map(function (r) {
                var mv = moves[r.sr_no] || moves[String(r.sr_no)] || [0, 0];
                var movedAlready = !!(mv[0] || mv[1]);
                return {
                    id: "txt-" + r.sr_no, sr: r.sr_no, text: r.text, family: r.family,
                    x: Number(r.x) + mv[0], y: Number(r.y) + mv[1],
                    color: movedAlready ? "#ff922b" : "#e9ecef",
                    hollow: false,
                    draggable: showTexts,
                    hidden: !showTexts && !movedAlready,
                    label: one(r.text).slice(0, 32),
                    info: "<b>" + esc(one(r.text)) + "</b><span>" + esc(r.family || "") + " &middot; " + esc(r.layer || "") +
                        "</span><span class='cad-muted'>" + (movedAlready ? "Moved - " : "") + "Drag to move (Move text tool)</span>"
                };
            });
            v.setOverlay({ polys: polys, marks: marks });
            v.setLegend(legendFor(f));
        });
        document.querySelectorAll("#geoDrawings .geo-floor-view").forEach(function (el) {
            el.classList.toggle("active", el.dataset.floor === st.floor);
        });
    }

    function kindName(it) {
        return it.foundAs || { traced: "traced", drawn: "drawn", merged: "merged" }[it.kind] || it.kind;
    }

    function legendFor(f) {
        var n = {};
        items(f).forEach(function (it) {
            var d = dec(it, f);
            if (d.include) { n[d.family] = (n[d.family] || 0) + 1; }
        });
        return Object.keys(n).sort().map(function (k) {
            return '<span class="cad-key" style="--k:' + colour(k) + '"></span>' + esc(k) + " " + n[k];
        }).join(" &nbsp; ") || '<span class="cad-muted">Nothing selected yet</span>';
    }

    // ---- right-hand panels -----------------------------------------------------
    function refresh() {
        if (!st.data) { return; }
        paintViewers();
        paintSummary();
        paintProps();
        paintLayers();
        paintList();
        undoState();
        $("geoRedoBtn").disabled = !st.redo.length;
        $("geoMergeBtn").disabled = st.sel.length < 2;
        $("geoDeleteBtn").disabled = !st.sel.length;
    }

    function paintSummary() {
        var fams = {}, total = 0, unnamed = 0, moved = 0;
        Object.keys(st.data.floors).forEach(function (f) {
            items(f).forEach(function (it) {
                var d = dec(it, f);
                if (!d.include || !layerVisible(it, f)) { return; }
                total++;
                fams[d.family] = (fams[d.family] || 0) + 1;
                if (!String(d.label || "").trim()) { unnamed++; }
            });
            moved += Object.keys(st.moves[f] || {}).filter(function (k) {
                var m = st.moves[f][k]; return m && (m[0] || m[1]);
            }).length;
        });
        $("geoSummary").innerHTML =
            "<div><b>" + total + "</b> boundaries from your selection" +
            (Object.keys(fams).length ? ": " + Object.keys(fams).sort().map(function (k) {
                return '<span class="geo-chip" style="--k:' + colour(k) + '">' + esc(k) + " " + fams[k] + "</span>";
            }).join(" ") : "") + "</div>" +
            '<div class="muted small">' + (unnamed ? '<b class="geo-warn">' + unnamed + " without a name</b> &middot; " : "") +
            moved + " text(s) moved &middot; " + unknownNote() +
            "<b>Generate geometry boundaries</b> writes these to the geometry DXF.</div>";
    }

    function unknownNote() {
        var n = 0;
        Object.keys(st.data.floors).forEach(function (f) {
            items(f).forEach(function (it) {
                var d = dec(it, f);
                if (d.family === "Unknown" && !d.include) { n++; }
            });
        });
        return n ? n + " Unknown shape(s) not drawn (tick them in the list to draw) &middot; " : "";
    }

    function pct(v) { return v == null ? "-" : Math.round(100 * v) + "%"; }
    function m2(v) { return (v / 1000).toFixed(2); }

    function paintWhy() {
        var host = $("geoWhy");
        var chosen = st.sel.map(function (id) { return byId(id); }).filter(Boolean);
        var it = chosen.length === 1 ? chosen[0] : null;
        if (!it || !it.row) {
            host.innerHTML = '<div class="muted small">' + (it ? "You traced or drew this shape." :
                "Select one shape to see what the engine saw, step by step.") + "</div>";
            return;
        }
        var r = it.row, f = r.features || {}, x = f.x || {};
        var feats = [
            ["Area", fmt2(f.area_sqm) + " sq.m"], ["Perimeter", fmt2(f.perimeter_m) + " m"],
            ["Width x length", m2(f.width_mm) + " &times; " + m2(f.height_mm) + " m"],
            ["Aspect ratio", f.aspect], ["Rectangularity", pct(f.rectangularity)], ["Vertices", f.vertices],
            ["X mark", x.detected ? "yes, " + x.size_mm.join(" &times; ") + " mm, " + x.orientation : "no"],
            ["X marks inside", f.x_inside || 0]
        ];
        if (f.fixtures) { feats.push(["Fixtures", f.fixtures]); }
        if (f.flights) { feats.push(["Stair flights", f.flights]); }
        var ev = (r.evidence || []).map(function (e) {
            return '<li class="' + (e.ok ? "ok" : "no") + '">' + (e.ok ? "&#10003;" : "&#10007;") + " " + esc(e.text) +
                (e.metadata ? ' <span class="gai-meta" title="Supporting metadata, not geometry">metadata</span>' : "") + "</li>";
        }).join("");
        host.innerHTML =
            '<div class="gai-head"><b>' + esc(r.label) + '</b> <span class="muted">' + esc(r.id) + "</span>" +
            (r.confidence == null ? "" : '<span class="gai-conf" title="Measured confidence">' + pct(r.confidence) + "</span>") + "</div>" +
            '<ol class="gai-pipe">' +
            "<li><b>Raw geometry</b>" + esc(SOURCE[f.source] || f.source || "") +
            (f.layers && f.layers.length ? ' <span class="muted small">layer ' + esc(f.layers.join(", ")) + "</span>" : "") + "</li>" +
            '<li><b>Features</b><table class="gai-feat">' + feats.map(function (p) {
                return "<tr><td>" + p[0] + "</td><td>" + p[1] + "</td></tr>";
            }).join("") + "</table></li>" +
            "<li><b>Pattern family</b>" + (r.family ? esc(r.family.title) + " &middot; " + r.family.members + " member(s)" : "-") +
            (r.bank ? " &middot; bank of " + (r.bank + 1) : "") +
            (r.repeats != null ? " &middot; same place on " + r.repeats + " other floor(s)" : "") + "</li>" +
            "<li><b>Spatial context</b>" + (r.near && r.near.length ? "next to " + esc(r.near.join(", ")) : "nothing within 0.5 m") + "</li>" +
            "<li><b>Classification</b>" + esc(nice(r.cls)) + (r.sub ? " &middot; " + esc(nice(r.sub)) : "") +
            ' <span class="muted small">rule ' + esc(r.rule) + "</span>" +
            (r.reason ? '<div class="gai-reason">' + esc(r.reason) + "</div>" : "") +
            '<ul class="gai-ev">' + ev + "</ul></li>" +
            "<li><b>Confidence</b>" + pct(r.confidence) + ' <span class="muted small">' + esc(r.confidence_note || "") + "</span></li>" +
            "<li><b>Engine's label</b>" + esc(r.label) + "</li></ol>";
    }

    function paintAbout() {
        var rules = st.data.rules || {};
        var keys = Object.keys(rules).sort(function (a, b) { return rules[b].n - rules[a].n; });
        $("gaiAbout").innerHTML =
            "<p>Each rule was run on real drawings and its findings paired, by place, with the text engine's " +
            "boundaries; samples of what the text engine had not named were checked by eye. Confidence is the " +
            "<b>95% lower bound</b> of how often the rule was right - right 6 times out of 6 shows about 60%, not 100%. " +
            esc(st.data.profile_source || "") + "</p>" +
            (keys.length ? '<table class="grid geo-layers" data-no-filter><thead><tr><th>Rule</th><th>Agreed</th>' +
                "<th>Conflicts</th><th>No text to compare</th><th>Drawings</th></tr></thead><tbody>" + keys.map(function (k) {
                    var q = rules[k];
                    return "<tr><td>" + esc(k) + "</td><td>" + q.agree + "</td><td>" + (q.conflict || 0) + "</td><td>" +
                        (q.unreferenced || 0) + "</td><td>" + (q.drawings || "") + "</td></tr>";
                }).join("") + "</tbody></table>" : "");
    }

    function paintProps() {
        paintWhy();
        var host = $("geoProps");
        var chosen = st.sel.map(function (id) { return byId(id); }).filter(Boolean);
        if (!chosen.length) {
            host.innerHTML = '<div class="muted small">Nothing selected. Click a shape in the drawing ' +
                "(Shift+click for several) to set its family and name. Use <b>Trace</b> or <b>Draw</b> " +
                "for spaces that are not there.</div>";
            return;
        }
        var fams = st.data.families;
        var ds = chosen.map(function (it) { return dec(it); });
        var famNow = ds.every(function (d) { return d.family === ds[0].family; }) ? ds[0].family : "";
        var incAll = ds.every(function (d) { return d.include; });
        var area = chosen.reduce(function (a, it) { return a + (it.area_sqm || 0); }, 0);
        var html = '<div class="geo-props-head"><b>' + (chosen.length === 1 ? "1 shape" : chosen.length + " shapes") +
            "</b> selected &middot; " + fmt2(area) + " sq.m</div>" +
            '<label class="geo-field">Draw it <input type="checkbox" id="gpInclude"' + (incAll ? " checked" : "") + "></label>" +
            '<label class="geo-field">Family <select id="gpFamily">' + (famNow ? "" : '<option value="">(mixed)</option>') +
            fams.map(function (f) { return "<option" + (f === famNow ? " selected" : "") + ">" + esc(f) + "</option>"; }).join("") +
            "</select></label>";
        if (chosen.length === 1) {
            var it = chosen[0], d = ds[0];
            var cands = it.texts || [];
            html += '<label class="geo-field">Name <input type="text" id="gpName" value="' + esc(one(d.label)) +
                '" placeholder="type a name, or pick a text below"></label>' +
                '<div class="geo-facts muted small">' + esc(kindName(it)) + " &middot; " +
                (it.size_mm[0] / 1000).toFixed(2) + " &times; " + (it.size_mm[1] / 1000).toFixed(2) + " m" +
                (it.layers && it.layers.length ? " &middot; layers: " + esc(it.layers.join(", ")) : "") +
                (it.note ? " &middot; " + esc(it.note) : "") + "</div>" +
                (cands.length ? '<div class="geo-texts"><div class="muted small">Texts inside / beside - click to use as the name:</div>' +
                    cands.map(function (t, i) {
                        return '<button type="button" class="geo-textbtn" data-i="' + i + '">' + esc(one(t.text)) +
                            ' <span class="muted">' + (t.inside ? "inside" : (t.gap_mm / 1000).toFixed(1) + " m") + "</span></button>";
                    }).join("") + "</div>" : '<div class="muted small">No text inside - type a name, or drag one in with <b>Move text</b>.</div>');
        }
        html += '<div class="geo-props-acts">' +
            (chosen.length >= 2 ? '<button type="button" class="btn btn-ghost btn-mini" id="gpMerge">Merge</button>' : "") +
            '<button type="button" class="btn btn-ghost btn-mini" id="gpDelete">Delete</button>' +
            '<button type="button" class="btn btn-ghost btn-mini" id="gpZoom">Zoom to</button></div>';
        host.innerHTML = html;

        $("gpInclude").addEventListener("change", function () {
            var on = this.checked;
            change(function () { chosen.forEach(function (x) { dec(x).include = on; }); });
        });
        $("gpFamily").addEventListener("change", function () {
            if (this.value) { setFamily(this.value); }
        });
        var name = $("gpName");
        if (name) {
            name.addEventListener("change", function () {
                var val = this.value;
                change(function () { var dd = dec(chosen[0]); dd.label = val; dd.label_sr = null; dd.include = true; });
            });
            host.querySelectorAll(".geo-textbtn").forEach(function (b) {
                b.addEventListener("click", function () {
                    var t = chosen[0].texts[+b.dataset.i];
                    change(function () {
                        var dd = dec(chosen[0]);
                        dd.label = t.text; dd.label_sr = t.sr_no; dd.include = true;
                    });
                });
            });
        }
        if ($("gpMerge")) { $("gpMerge").addEventListener("click", mergeSelected); }
        $("gpDelete").addEventListener("click", function () { deleteIds(st.sel.slice(), this.parentNode); });
        $("gpZoom").addEventListener("click", function () {
            showFloor(st.floor);
            var v = st.viewers[st.floor];
            if (v && chosen[0]) { v.focus("geo-" + chosen[0].id); }
        });
    }

    function paintLayers() {
        var host = $("geoLayers");
        var list = ((st.data.layers || {})[st.floor]) || [];
        if (!list.length) { host.innerHTML = '<div class="muted small">No closed outlines on this floor.</div>'; return; }
        var vis = st.layerVis[st.floor];
        host.innerHTML = '<table class="grid geo-layers" data-no-filter><thead><tr><th>Show</th><th>Layer</th><th>Outlines</th><th>Map all to</th></tr></thead><tbody>' +
            list.map(function (L, i) {
                return "<tr><td><input type=\"checkbox\" data-i=\"" + i + "\"" + (vis[L.layer] !== false ? " checked" : "") +
                    "></td><td>" + esc(L.layer) + "</td><td>" + L.count + "</td><td><select data-i=\"" + i +
                    '"><option value="">-</option>' + st.data.families.map(function (f) {
                        return "<option>" + esc(f) + "</option>";
                    }).join("") + "</select></td></tr>";
            }).join("") + "</tbody></table>";
        host.querySelectorAll("input[type=checkbox]").forEach(function (cb) {
            cb.addEventListener("change", function () {
                var L = list[+cb.dataset.i].layer, on = cb.checked;
                change(function () { st.layerVis[st.floor][L] = on; });
            });
        });
        host.querySelectorAll("select").forEach(function (sel) {
            sel.addEventListener("change", function () {
                var L = list[+sel.dataset.i].layer, fam = sel.value;
                if (!fam) { return; }
                var n = 0;
                change(function () {
                    st.layerVis[st.floor][L] = true;
                    items().forEach(function (it) {
                        if ((it.layers || []).indexOf(L) >= 0) {
                            var d = dec(it); d.family = fam; d.include = true; n++;
                        }
                    });
                });
                setStatus(n + " outline(s) on " + L + " mapped to " + fam + ".", "ok");
            });
        });
    }

    // ---- list view -----------------------------------------------------------------
    function paintList() {
        var sec = $("geoListSec");
        if (!sec.open) { return; }          // drawn when opened
        var host = $("geoGroups");
        var groups = {};
        items().forEach(function (it) {
            if (!layerVisible(it)) { return; }
            var f = dec(it).family || "Unknown";
            (groups[f] = groups[f] || []).push(it);
        });
        var order = (st.data.group_order || []).filter(function (f) { return groups[f]; });
        Object.keys(groups).sort().forEach(function (f) {
            if (order.indexOf(f) < 0 && f !== "Unknown") { order.push(f); }
        });
        if (groups.Unknown) { order.push("Unknown"); }
        // Keep the groups the user opened (and where they scrolled) when
        // the list is drawn again after a change.
        var keep = {};
        Array.prototype.forEach.call(host.querySelectorAll("details.geo-group[open]"), function (d) {
            var w = d.querySelector(".table-wrap");
            keep[d.dataset.fam] = w ? w.scrollTop : 0;
        });
        host.innerHTML = order.map(function (f) {
            var rows = groups[f];
            var on = rows.filter(function (it) { return dec(it).include; }).length;
            return '<details class="geo-group" data-fam="' + esc(f) + '"' + (f in keep ? " open" : "") + '><summary><span class="geo-dot" style="--k:' + colour(f) + '"></span><b>' +
                esc(f) + "</b> <span class=\"muted\">" + rows.length + " &middot; " + on + " drawn</span></summary>" +
                '<div class="table-wrap"><table class="grid geo-table"><thead><tr><th>Draw</th><th>Name</th><th>Area</th><th>Found as</th><th>Family</th><th></th></tr></thead><tbody>' +
                rows.map(function (it) {
                    var d = dec(it);
                    return '<tr data-id="' + esc(it.id) + '"' + (st.sel.indexOf(it.id) >= 0 ? ' class="geo-selrow"' : "") +
                        '><td><input type="checkbox" class="geo-inc"' + (d.include ? " checked" : "") + "></td><td>" +
                        esc(one(d.label) || "-") + "</td><td>" + fmt2(it.area_sqm) + "</td><td>" + esc(kindName(it)) +
                        '</td><td><select class="geo-rowfam" title="Move it to another family">' + famOpts(d.family || "Unknown") +
                        '</select></td><td class="geo-acts-cell"><button type="button" class="btn btn-ghost btn-mini geo-preview" title="Show this shape in the drawing">&#128269; Preview</button> ' +
                        '<button type="button" class="btn btn-ghost btn-mini geo-del" title="Delete this boundary">&#128465; Delete</button></td></tr>';
                }).join("") + "</tbody></table></div></details>";
        }).join("");
        Array.prototype.forEach.call(host.querySelectorAll("details.geo-group[open]"), function (d) {
            var w = d.querySelector(".table-wrap");
            if (w) { w.scrollTop = keep[d.dataset.fam] || 0; }
        });
    }
    function famOpts(now) {
        var fams = st.data.families.slice();
        if (fams.indexOf(now) < 0) { fams.push(now); }
        return fams.map(function (f) {
            return "<option" + (f === now ? " selected" : "") + ">" + esc(f) + "</option>";
        }).join("");
    }
    $("geoListSec").addEventListener("toggle", paintList);
    $("geoGroups").addEventListener("change", function (e) {
        var tr = e.target.closest("tr[data-id]");
        if (!tr) { return; }
        var it = byId(tr.dataset.id);
        if (!it) { return; }
        if (e.target.classList.contains("geo-inc")) {
            var on = e.target.checked;
            change(function () { dec(it).include = on; });
        } else if (e.target.classList.contains("geo-rowfam")) {
            // The shape moves to that family's group; open it there.
            var fam = e.target.value;
            change(function () { var d = dec(it); d.family = fam; d.include = true; st.sel = [it.id]; });
            scrollToRow(it.id);
            setStatus("Moved to " + fam + ".", "ok");
        }
    });
    $("geoGroups").addEventListener("click", function (e) {
        var tr = e.target.closest("tr[data-id]");
        if (!tr || e.target.closest("input, select, .geo-ask")) { return; }
        if (e.target.closest(".geo-del")) {
            deleteIds([tr.dataset.id], e.target.closest("td"));
            return;
        }
        st.sel = [tr.dataset.id];
        refresh();
        var v = st.viewers[st.floor];
        showFloor(st.floor);
        if (v) { v.focus("geo-" + tr.dataset.id); }
        if (e.target.closest(".geo-preview")) {
            // bring the drawing on screen when it has scrolled away
            var view = document.querySelector("#geoDrawings .geo-floor-view.active") || $("geoDrawings");
            var r = view.getBoundingClientRect();
            if (r.bottom < 80 || r.top > window.innerHeight - 80) {
                view.scrollIntoView({ block: "start", behavior: "smooth" });
            }
        }
    });
    function scrollToRow(id) {
        var tr = document.querySelector('#geoGroups tr[data-id="' + id + '"]');
        if (!tr) { return; }
        var det = tr.closest("details");
        if (det) { det.open = true; }
        tr.scrollIntoView({ block: "nearest" });
    }

    // ---- keyboard --------------------------------------------------------------
    document.addEventListener("keydown", function (e) {
        if (!st.data || !pane.offsetParent) { return; }             // tab not on screen
        var tag = (e.target.tagName || "").toLowerCase();
        if (tag === "input" || tag === "select" || tag === "textarea") { return; }
        var k = e.key.toLowerCase();
        if ((e.ctrlKey || e.metaKey) && k === "z" && !e.shiftKey) { e.preventDefault(); undoStep(); return; }
        if ((e.ctrlKey || e.metaKey) && (k === "y" || (k === "z" && e.shiftKey))) { e.preventDefault(); redo(); return; }
        if (e.ctrlKey || e.metaKey || e.altKey) { return; }
        if (k === "escape") {
            if (cancelAsk()) { return; }
            if (st.draft) { cancelDraft(); } else if (st.sel.length) { st.sel = []; refresh(); }
            else if (st.mode !== "select") { setMode("select"); }
            return;
        }
        if (k === "enter" && st.draft) { e.preventDefault(); finishDraft(); return; }
        if (k === "delete" || k === "backspace") { e.preventDefault(); deleteSelected(); return; }
        if (k === "m") { mergeSelected(); return; }
        var modes = { "1": "select", "2": "trace", "3": "draw", "4": "move" };
        if (modes[k]) { setMode(modes[k]); return; }
        if (KEY_FAMILY[k] && st.sel.length) {
            var fam = KEY_FAMILY[k];
            if (st.data.families.indexOf(fam) >= 0) { setFamily(fam); }
        }
    });

    // ---- save / load / reset ----------------------------------------------------
    $("geoSaveBtn").addEventListener("click", function () {
        var blob = new Blob([JSON.stringify({ app: "Drawing Analyzer geometry", floors: Object.keys(st.data.floors),
                                              state: JSON.parse(snapshot()) }, null, 1)],
                            { type: "application/json" });
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "geometry-selection.json";
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
    });
    $("geoLoadBtn").addEventListener("click", function () { $("geoLoadFile").click(); });
    $("geoLoadFile").addEventListener("change", async function () {
        var f = this.files && this.files[0];
        this.value = "";
        if (!f) { return; }
        try {
            var o = JSON.parse(await f.text());
            if (!o.state) { throw new Error("not a saved selection"); }
            var missing = (o.floors || []).filter(function (x) { return !st.data.floors[x]; });
            if (missing.length) { throw new Error("it was saved for other floors (" + missing.join(", ") + ")"); }
            st.undo.push(snapshot());
            restore(JSON.stringify(o.state));
            afterChange();
            setStatus("Loaded the saved selection (Ctrl+Z to undo).", "ok");
        } catch (e) { setStatus("Could not load it: " + e.message, "error"); }
    });
    $("geoResetBtn").addEventListener("click", function () {
        if (!st.data) { return; }
        change(function () {
            Object.keys(st.data.floors).forEach(function (f) {
                st.created[f] = []; st.dec[f] = {}; st.moves[f] = {}; st.gone[f] = {};
                st.layerVis[f] = {};
                ((st.data.layers || {})[f] || []).forEach(function (L) { st.layerVis[f][L.layer] = L.visible; });
            });
            st.sel = [];
        });
        setStatus("Back to what was detected (Ctrl+Z to undo).", "ok");
    });

    // ---- Generate geometry boundaries (Step 5's own output) ---------------------
    function decisions() {
        var out = {};
        Object.keys(st.data.floors).forEach(function (f) {
            var per = {};
            var gone = st.gone[f] || {};
            (st.data.floors[f] || []).concat(st.created[f] || []).forEach(function (it) {
                var d = dec(it, f);
                var made = it.id.indexOf("u-") === 0;
                var row = { include: !!d.include && !gone[it.id] && layerVisible(it, f),
                            family: d.family, label: d.label, label_sr: d.label_sr };
                if (gone[it.id] === "del") { row.deleted = true; }
                if (made) { row.poly = it.poly; row.kind = it.kind; }
                per[it.id] = row;
            });
            out[f] = per;
        });
        return out;
    }
    $("geoGenerateBtn").addEventListener("click", function () { generate(); });
    /* Step 4's output: the geometry annotated DXF. True when it is written. */
    async function generate() {
        if (!st.data) { return false; }
        var btn = $("geoGenerateBtn");
        btn.classList.add("is-busy");
        btn.disabled = true;
        setStatus("Writing the geometry boundaries to the geometry annotated DXF...", "busy");
        try {
            var r = await post("/geoengine/generate", { token: st.token, decisions: decisions(), text_moves: st.moves });
            if (!r.ok) {
                setStatus(r.error || "Could not generate.", "error");
                if (window.DA_flowNote) { window.DA_flowNote(r.error, !!r.stopped); }
                return false;
            }
            var fams = Object.keys(r.summary || {}).sort().map(function (k) { return k + " " + r.summary[k]; }).join(", ");
            setStatus("Done: " + r.boundaries + " geometry boundaries (" + fams + ")" +
                      (r.texts_moved ? ", " + r.texts_moved + " text(s) moved" : "") +
                      ". Download the geometry DXF above; Step 5 compares it with the text-based output, Step 6 measures its areas.", "ok");
            if (window.DA_geoCompareStale) { window.DA_geoCompareStale(); }
            document.dispatchEvent(new CustomEvent("da:stage", { detail: { name: "geo-generate" } }));
            return true;
        } catch (e) {
            setStatus("Error: " + e.message, "error");
            if (window.DA_flowNote) { window.DA_flowNote("Error: " + e.message); }
            return false;
        } finally {
            btn.classList.remove("is-busy");
            btn.disabled = false;
        }
    }
    /* Fast track: analyse (unless done for this upload), then write. */
    window.DA_geoFlow = async function () {
        if (!(await detect(false))) { return false; }
        return generate();
    };

    /* Step 5 asks to see one finding here. */
    window.DA_geoFocus = function (floor, id) {
        if (!st.data || !st.data.floors[floor]) { return; }
        if (st.floor !== floor) { selectFloor(floor); }
        st.sel = [id];
        refresh();
        var v = st.viewers[floor];
        showFloor(floor);
        if (v) { v.focus("geo-" + id); }
        var why = $("geoWhySec");
        if (why) { why.open = true; }
    };

    /* A new extraction replaces the floors: the old shapes no longer apply. */
    window.DA_geometryReset = function () {
        clearViewers();
        st.data = null;
        st.sel = [];
        $("geoGroups").innerHTML = "";
        $("geoControls").classList.add("hidden");
        $("geoSplit").classList.add("hidden");
        setStatus("", "");
        // Tab open while the texts were extracted again: read the new floors.
        setTimeout(function () { if (paneOpen()) { detect(); } }, 0);
    };

    $("geoHint").textContent = HINTS.select;
})();
