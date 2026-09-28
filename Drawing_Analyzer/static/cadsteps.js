/* The CAD viewer in each step, kept in step with that step's table.
 *
 *   Step 2  floor plate candidates: outlined, filled when ticked; clicking
 *           a plate ticks it, hovering a row lights its plate.
 *   Step 3  extracted texts on the active floor: a dot per text in its
 *           family colour; the table's ticks, search and boundary type
 *           show on the dots, and a dot and its row find each other.
 *   Step 4  the corrected drawing with its room boundaries.
 *
 * The drawing itself is read once per upload on the server (cadview.py)
 * and shared by Steps 2 and 3.
 */
(function () {
    "use strict";

    var $ = function (id) { return document.getElementById(id); };
    var fmt2 = function (v) {
        return Number(v || 0).toLocaleString(undefined, {
            minimumFractionDigits: 2, maximumFractionDigits: 2 });
    };
    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }
    function bboxOf(polys) {
        var b = [Infinity, Infinity, -Infinity, -Infinity];
        polys.forEach(function (pts) {
            pts.forEach(function (p) {
                b[0] = Math.min(b[0], p[0]); b[1] = Math.min(b[1], p[1]);
                b[2] = Math.max(b[2], p[0]); b[3] = Math.max(b[3], p[1]);
            });
        });
        return isFinite(b[0]) ? b : null;
    }
    function flash(tr) {
        if (!tr) { return; }
        tr.scrollIntoView({ behavior: "smooth", block: "center" });
        tr.classList.remove("cad-flash");
        void tr.offsetWidth;
        tr.classList.add("cad-flash");
    }

    /* ------------------------------------------------------ Step 2 --- */
    var s2 = { viewer: null, token: null, plates: [], seq: 0 };

    function plateRows() {
        var out = {};
        var tbody = document.querySelector("#floorsTable tbody");
        if (!tbody) { return out; }
        tbody.querySelectorAll(".rowsel").forEach(function (cb) {
            var idx = Number(cb.dataset.idx);
            var name = tbody.querySelector('.rowname[data-idx="' + idx + '"]');
            out[idx] = { checked: cb.checked, name: name ? name.value.trim() : "",
                         tr: cb.closest("tr"), cb: cb };
        });
        return out;
    }

    function paintPlates() {
        if (!s2.viewer) { return; }
        var rows = plateRows();
        var picked = 0;
        var polys = s2.plates.map(function (c) {
            var r = rows[c.index] || {};
            if (r.checked) { picked++; }
            return {
                id: "plate-" + c.index,
                index: c.index,
                pts: c.polygon,
                stroke: r.checked ? "#40c057" : "#22b8cf",
                fill: r.checked ? "rgba(64,192,87,0.22)" : "rgba(34,184,207,0.06)",
                dash: !r.checked,
                width: r.checked ? 2.5 : 1.5,
                label: (r.name || c.name || ("Plate " + c.index)),
                short: "#" + c.index,
                sub: fmt2(c.area_sqm) + " sq.m",
                info: "<b>" + esc(r.name || c.name || ("Plate " + c.index)) + "</b>" +
                    "<span>Index " + c.index + " &middot; " + fmt2(c.area_sqm) + " sq.m</span>" +
                    "<span>Layer " + esc(c.layer) + "</span>" +
                    (c.title ? "<span>Title: " + esc(c.title) + "</span>" : "") +
                    "<span class='cad-muted'>" + (r.checked ? "Ticked - click to untick" : "Click to tick") + "</span>"
            };
        });
        s2.viewer.setOverlay({ polys: polys });
        s2.viewer.setLegend(
            '<span class="cad-key" style="--k:#40c057"></span>ticked ' + picked +
            ' &nbsp; <span class="cad-key dash" style="--k:#22b8cf"></span>not ticked ' +
            (s2.plates.length - picked));
    }

    window.DA_cadStep2 = async function (token) {
        var host = $("cadPlates");
        if (!host || !window.DA_cad) { return; }
        var seq = ++s2.seq;
        s2.token = token;
        if (!s2.viewer) {
            s2.viewer = window.DA_cad(host);
            s2.viewer.onClick(togglePlate);
        }
        s2.viewer.clear();
        s2.plates = [];
        var stale = function () { return seq !== s2.seq; };
        try {
            var res = await fetch("/cadview/" + token + "/plates");
            var data = await res.json();
            if (stale()) { return; }
            if (!data.ok) { s2.viewer.setStatus(data.error, "error"); return; }
            s2.plates = data.candidates || [];
            s3.families = {};
            (data.families || []).forEach(function (f) { s3.families[f.name] = f.rgb; });
            var box = bboxOf(s2.plates.map(function (c) { return c.polygon; }));
            if (box) { s2.viewer.setHome(box); }
            paintPlates();
            await window.DA_cadLoad(s2.viewer, "/cadview/" + token + "/source/scene",
                                    "/cadview/" + token + "/source/lines",
                                    { isStale: stale });
            if (!stale() && box) { s2.viewer.fit(box); }
        } catch (err) {
            if (!stale()) { s2.viewer.setStatus("Could not load the drawing: " + err.message, "error"); }
        }
    };

    (function wireStep2() {
        var tbody = document.querySelector("#floorsTable tbody");
        var selAll = $("selAll");
        if (!tbody) { return; }
        tbody.addEventListener("change", paintPlates);
        tbody.addEventListener("input", paintPlates);
        if (selAll) { selAll.addEventListener("change", function () { setTimeout(paintPlates, 0); }); }
        tbody.addEventListener("mouseover", function (e) {
            var tr = e.target.closest("tr");
            var cb = tr && tr.querySelector(".rowsel");
            if (s2.viewer) { s2.viewer.setHovered(cb ? "plate-" + cb.dataset.idx : null); }
        });
        tbody.addEventListener("mouseleave", function () {
            if (s2.viewer) { s2.viewer.setHovered(null); }
        });
        // A click on the row itself (not its checkbox or name box) shows
        // the plate in the drawing.
        tbody.addEventListener("click", function (e) {
            if (e.target.closest("input, select, button, a")) { return; }
            var tr = e.target.closest("tr");
            var cb = tr && tr.querySelector(".rowsel");
            if (cb && s2.viewer) { s2.viewer.focus("plate-" + cb.dataset.idx); }
        });
    })();

    /* Clicking a plate in the drawing ticks or unticks its row. */
    function togglePlate(item) {
        if (!item || item.index == null) { return; }
        var r = plateRows()[item.index];
        if (!r) { return; }
        r.cb.checked = !r.cb.checked;
        r.cb.dispatchEvent(new Event("change", { bubbles: true }));
        flash(r.tr);
    }

    /* ------------------------------------------------------ Step 3 --- */
    var s3 = { viewer: null, token: null, floors: {}, families: {},
               loadedFor: null, seq: 0, floor: null };

    function familyColour(name) {
        return s3.families[name] || "#adb5bd";
    }

    async function ensureStep3(token) {
        var host = $("cadTexts");
        if (!host || !window.DA_cad) { return false; }
        // Off by default: each dot sits on the drawing's own copy of its text.
        s3.viewer = s3.viewer || window.DA_cad(host, { labels: false });
        if (s3.loadedFor === token) { return true; }
        var seq = ++s3.seq;
        s3.loadedFor = token;
        s3.floor = null;
        s3.viewer.clear();
        var stale = function () { return seq !== s3.seq; };
        try {
            var res = await fetch("/cadview/" + token + "/plates");
            var data = await res.json();
            if (stale() || !data.ok) { return false; }
            s3.floors = {};
            (data.selected || []).forEach(function (f) { s3.floors[f.name] = f.polygon; });
            (data.families || []).forEach(function (f) { s3.families[f.name] = f.rgb; });
            var all = bboxOf(Object.keys(s3.floors).map(function (k) { return s3.floors[k]; }));
            paintTexts();
            await window.DA_cadLoad(s3.viewer, "/cadview/" + token + "/source/scene",
                                    "/cadview/" + token + "/source/lines",
                                    { isStale: stale, box: all || undefined });
            if (stale()) { return false; }
            s3.floor = null;
            paintTexts();
            s3.viewer.onClick(function (item) {
                if (!item || item.sr == null) { return; }
                var cb = document.querySelector('#resultsTable .textsel[data-sr="' + item.sr + '"]');
                if (cb) { flash(cb.closest("tr")); }
            });
            return true;
        } catch (err) {
            if (!stale()) { s3.viewer.setStatus("Could not load the drawing: " + err.message, "error"); }
            return false;
        }
    }

    function paintTexts() {
        if (!s3.viewer || !window.DA_step3State) { return; }
        var st = window.DA_step3State();
        if (!st.floor) { return; }
        var shown = new Set(st.visible.map(function (r) { return r.sr_no; }));
        var marks = st.rows.map(function (r) {
            var fam = st.family[r.sr_no] || r.family || "Unknown";
            var on = !st.unchecked.has(r.sr_no);
            return {
                id: "text-" + r.sr_no, sr: r.sr_no,
                x: Number(r.x), y: Number(r.y),
                color: familyColour(fam),
                hollow: !on,
                dim: !shown.has(r.sr_no),
                label: r.text.length > 28 ? r.text.slice(0, 27) + "…" : r.text,
                info: "<b>" + esc(r.text) + "</b>" +
                    "<span>Sr.No " + r.sr_no + " &middot; " + esc(fam) + "</span>" +
                    "<span>Layer " + esc(r.layer) + " &middot; " + esc(r.source) + "</span>" +
                    "<span class='cad-muted'>" + (on ? "Ticked" : "Unticked - no boundary") + "</span>"
            };
        });
        var floorPoly = s3.floors[st.floor];
        var polys = floorPoly ? [{ id: "floor", pts: floorPoly, stroke: "#4dabf7",
                                   width: 2, hit: false, label: "" }] : [];
        s3.viewer.setOverlay({ polys: polys, marks: marks });
        if (s3.floor !== st.floor && floorPoly) {
            s3.floor = st.floor;
            var b = bboxOf([floorPoly]);
            s3.viewer.setHome(b);
            s3.viewer.fit(b);
        }
        // Legend: the families present on this floor.
        var counts = {};
        st.rows.forEach(function (r) {
            var fam = st.family[r.sr_no] || r.family || "Unknown";
            counts[fam] = (counts[fam] || 0) + 1;
        });
        s3.viewer.setLegend(Object.keys(counts).sort().map(function (f) {
            return '<span class="cad-key dot" style="--k:' + familyColour(f) + '"></span>' +
                esc(f) + " " + counts[f];
        }).join(" &nbsp; "));
    }

    window.DA_cadStep3 = function () {
        if (!window.DA_step3State) { return; }
        var st = window.DA_step3State();
        if (!st.token) { return; }
        if (s3.loadedFor !== st.token) {
            ensureStep3(st.token);
            return;
        }
        // renderRows calls this before it rebuilds the table; paint after.
        setTimeout(paintTexts, 0);
    };

    (function wireStep3() {
        var tbody = document.querySelector("#resultsTable tbody");
        if (!tbody) { return; }
        tbody.addEventListener("click", function (e) {
            if (e.target.closest("input, select, button, a")) { return; }
            var tr = e.target.closest("tr");
            var cb = tr && tr.querySelector(".textsel");
            if (cb && s3.viewer) { s3.viewer.focus("text-" + cb.dataset.sr, true); }
        });
        tbody.addEventListener("mouseover", function (e) {
            var tr = e.target.closest("tr");
            var cb = tr && tr.querySelector(".textsel");
            if (s3.viewer) { s3.viewer.setHovered(cb ? "text-" + cb.dataset.sr : null); }
        });
        tbody.addEventListener("mouseleave", function () {
            if (s3.viewer) { s3.viewer.setHovered(null); }
        });
    })();

    /* ------------------------------------------------------ Step 4 --- */
    var s4 = { viewer: null, seq: 0 };

    window.DA_cadStep4 = async function (token) {
        var host = $("cadAreas");
        if (!host || !window.DA_cad) { return; }
        var seq = ++s4.seq;
        var stale = function () { return seq !== s4.seq; };
        s4.viewer = s4.viewer || window.DA_cad(host);
        s4.viewer.clear();
        try {
            var scene = await window.DA_cadLoad(s4.viewer, "/cadview/" + token + "/areas/scene",
                                                "/cadview/" + token + "/areas/lines",
                                                { isStale: stale });
            if (!scene || stale()) { return; }
            var rooms = scene.boundaries || [];
            var counts = {};
            var plates = 0;
            var polys = rooms.map(function (b, i) {
                if (b.floor) { plates++; } else { counts[b.family] = (counts[b.family] || 0) + 1; }
                return {
                    id: "room-" + i, pts: b.pts, stroke: "rgba(0,0,0,0)", width: 1,
                    name: b.name, areaSqm: b.area, floor: !!b.floor,
                    info: "<b>" + esc(b.name || "(no label)") + "</b>" +
                        "<span>" + esc(b.family) + " &middot; " + fmt2(b.area) + " sq.m</span>" +
                        "<span>Layer " + esc(b.layer) + "</span>"
                };
            });
            s4.viewer.setOverlay({ polys: polys });
            var box = bboxOf(rooms.map(function (b) { return b.pts; }));
            if (box) { s4.viewer.setHome(box); s4.viewer.fit(box); }
            s4.viewer.setLegend((rooms.length - plates) + " room boundaries" +
                (plates ? " &middot; " + plates + " floor plates" : "") +
                " &middot; hover a room for its details");
            s4.viewer.onClick(function (item) {
                if (!item || !item.name || item.floor) { return; }
                var want = fmt2(item.areaSqm);
                var rows = document.querySelectorAll("#roomAreasTable tbody tr");
                for (var i = 0; i < rows.length; i++) {
                    var td = rows[i].children;
                    if (td.length > 2 && td[1].textContent.trim() === item.name &&
                        td[2].textContent.trim() === want) {
                        if (window.DA_showPane) { window.DA_showPane("areasRoomsPane"); }
                        flash(rows[i]);
                        return;
                    }
                }
            });
            s4.rooms = polys;
        } catch (err) {
            if (!stale()) { s4.viewer.setStatus("Could not load the drawing: " + err.message, "error"); }
        }
    };

    /* A room row in the Room areas table opens the drawing on that room. */
    (function wireStep4() {
        var tbody = document.querySelector("#roomAreasTable tbody");
        if (!tbody) { return; }
        tbody.addEventListener("click", function (e) {
            if (!s4.viewer || !s4.rooms) { return; }
            var tr = e.target.closest("tr");
            if (!tr || tr.children.length < 3) { return; }
            var name = tr.children[1].textContent.trim();
            var area = tr.children[2].textContent.trim();
            var hit = null;
            s4.rooms.forEach(function (P) {
                if (!hit && !P.floor && P.name === name && fmt2(P.areaSqm) === area) { hit = P; }
            });
            if (!hit) { return; }
            if (window.DA_showPane) { window.DA_showPane("areasCadPane"); }
            setTimeout(function () { s4.viewer.focus(hit.id); }, 60);
        });
    })();
})();
