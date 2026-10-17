/* Step 5 "Comparison: text vs geometry".
 *
 * Left, what the texts found (the text engine, Step 3); right, what the
 * geometry found (Step 4, the user's output there). Each drawing moves on
 * its own. Below them: the text-based report, the geometry-based report
 * and the comparison table. Objects are paired by place (geocompare.py),
 * never by name. Step 4 itself lives in geometry.js.
 */
(function () {
    "use strict";

    function $(id) { return document.getElementById(id); }
    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }
    function pct(v) { return v == null ? "-" : Math.round(100 * v) + "%"; }
    function fmt2(v) { return v == null ? "-" : Number(v).toFixed(2); }
    function m(v) { return (v / 1000).toFixed(2); }
    function rgba(hex, a) {
        var n = parseInt(hex.slice(1), 16);
        return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
    }
    function token() {
        var s = window.DA_step3State ? window.DA_step3State() : {};
        return s.token || null;
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
    function setStatus(el, msg, kind) {
        el.textContent = msg || "";
        el.className = "status" + (kind ? " " + kind : "");
    }
    function polyBox(polys) {
        var b = [Infinity, Infinity, -Infinity, -Infinity];
        polys.forEach(function (pts) {
            pts.forEach(function (p) {
                if (p[0] < b[0]) { b[0] = p[0]; }
                if (p[1] < b[1]) { b[1] = p[1]; }
                if (p[0] > b[2]) { b[2] = p[0]; }
                if (p[1] > b[3]) { b[3] = p[1]; }
            });
        });
        return isFinite(b[0]) ? b : undefined;
    }
    function nice(c) {
        return String(c || "").replace(/_/g, " ").toLowerCase().replace(/(^|\s)\S/g, function (x) { return x.toUpperCase(); });
    }
    // the text engine's boundary layers, as the family names users know
    var TEXT_FAMILY = { SHAFT: "Shaft", TOILET: "Toilet", LIFT: "Lift", LIFTLOBBY: "Lift Lobby", STAIR: "Staircase", FIRE: "Fire / Life Safety",
                        MEP: "MEP / Services", ELEC: "Electrical", CIRC: "Circulation", VOID: "Void", CUTOUT: "Shaft",
                        BALCONY: "Balcony / Open", LANDSCAPE: "Landscape / External", PLUMB: "Plumbing",
                        AMENITY: "Amenity", ROOM: "Habitable Room", STORE: "Storage", PARKING: "Parking",
                        UNKNOWN: "Unknown" };
    var ROOM_RGB = "#fab005";        // every room the user has not named yet, one colour
    var famRgb = {};                 // family -> colour of the generated output
    function colourOf(fam) {
        if (fam === "Room") { return ROOM_RGB; }
        return famRgb[fam] || "#adb5bd";
    }
    async function loadColours(tok) {
        if (Object.keys(famRgb).length) { return; }
        try {
            var r = await (await fetch("/cadview/" + tok + "/plates")).json();
            (r.families || []).forEach(function (f) { famRgb[f.name] = f.rgb; });
        } catch (e) { /* grey it is */ }
    }
    var GEO_FAMILY = { LIFT_LOBBY: "Lift Lobby", MACHINE_ROOM: "Machine Room", SERVICE_LOBBY: "Service Lobby", COLUMN: "Column", LIFT: "Lift", SHAFT: "Shaft", STAIRCASE: "Staircase", STAIR_WELL: "Shaft", VOID: "Void",
                       TOILET: "Toilet", ROOM: "Room", UNKNOWN_GEOMETRY: "Unknown" };
    function textFam(t) { return TEXT_FAMILY[t.cls] || nice(t.cls); }
    function geoFam(g) { return g.category || GEO_FAMILY[g.cls] || nice(g.cls); }

    /* Steps 4 and 5 appear once Step 3 has floors to work on. */
    var results = $("step-results");
    function syncReady() {
        var ready = results && !results.classList.contains("hidden");
        $("step-geoai").classList.toggle("hidden", !ready);
        $("step-geocmp").classList.toggle("hidden", !ready);
    }
    if (results) {
        new MutationObserver(syncReady).observe(results, { attributes: true, attributeFilter: ["class"] });
        syncReady();
    }

    var CAT = {
        AGREEMENT: { rgb: "#2f9e44", name: "Agreement", match: "YES" },
        CONFLICT: { rgb: "#e03131", name: "Conflict", match: "CONFLICT" },
        TEXT_UNNAMED: { rgb: "#f08c00", name: "Text unnamed", match: "PARTIAL" },
        GEO_UNCLASSIFIED: { rgb: "#ae3ec9", name: "Geometry unclassified", match: "PARTIAL" },
        MULTIPLE: { rgb: "#fab005", name: "Multiple candidates", match: "MULTIPLE" },
        TEXT_ONLY: { rgb: "#1c7ed6", name: "Only from texts", match: "TEXT ONLY" },
        GEO_ONLY: { rgb: "#e64980", name: "Only from geometry", match: "GEO ONLY" },
        STRUCTURE: { rgb: "#495057", name: "Structure (columns)", match: "STRUCTURE" }
    };
    var cm = { token: null, data: null, floor: null, tv: null, gv: null, gen: 0, sel: null, busy: false,
               family: "", dec: {}, onlyDecide: false, on: { AGREEMENT: true, CONFLICT: true, TEXT_UNNAMED: true, GEO_UNCLASSIFIED: true,
                                     MULTIPLE: true, TEXT_ONLY: true, GEO_ONLY: true, STRUCTURE: true } };

    async function cmRun(rerun) {
        var tok = token();
        if (!tok) { setStatus($("gcmStatus"), "Extract the texts first (Steps 1-3).", "error"); return false; }
        if (cm.busy) { return cm.running; }
        cm.busy = true;
        cm.running = cmWork(rerun, tok);
        return cm.running;
    }
    async function cmWork(rerun, tok) {
        setStatus($("gcmStatus"), "Pairing the text-based boundaries with the geometry-based ones by place (if an " +
                  "engine has to run first, this takes a minute or two)...", "busy");
        try {
            var r = await post("/geoengine/compare", { token: tok, rerun: !!rerun });
            if (!r.ok) {
                setStatus($("gcmStatus"), r.error || "The comparison failed.", "error");
                if (window.DA_flowNote) { window.DA_flowNote(r.error, !!r.stopped); }
                return false;
            }
            if (r.cached && cm.data && cm.token === tok && cm.data.text_mtime === r.comparison.text_mtime &&
                    cm.data.review_mtime === r.comparison.review_mtime) {
                setStatus($("gcmStatus"), "Up to date.", "ok");
                stageDone("compare");
                return true;
            }
            cm.token = tok;
            cm.data = r.comparison;
            cm.sel = null;
            cm.family = "";
            await loadColours(tok);
            loadDecisions(tok);
            $("gcmFinalDl").classList.add("hidden");
            var floors = [];
            cm.data.rows.forEach(function (x) { if (floors.indexOf(x.floor) < 0) { floors.push(x.floor); } });
            $("gcmFloor").innerHTML = floors.map(function (f) { return "<option>" + esc(f) + "</option>"; }).join("");
            cm.floor = floors[0];
            $("gcmDlXlsx").href = "/download/geo_cmp_xlsx/" + tok;
            $("gcmDlDxf").href = "/download/geo_cmp_dxf/" + tok;
            $("gcmDlJson").href = "/download/geo_cmp_json/" + tok;
            $("gcmBody").classList.remove("hidden");
            var src = cm.data.text_source || {};
            var how = (src.source === "step3" ? " Texts: your Step 3 Generate." :
                       " Texts: the text engine run on every extracted text (Step 3 not generated yet, or generated for one family).") +
                      (cm.data.geo_source === "step5" ? " Geometry: your Step 4 output." :
                       " Geometry: the engine's findings as detected (Generate in Step 4 to compare your edited version).");
            setStatus($("gcmStatus"), "Compared " + cm.data.summary.text_detections + " text-based boundaries with " +
                      cm.data.summary.geometry_detections + " geometry-based ones." + how, "ok");
            render(true);
            stageDone("compare");
            return true;
        } catch (e) {
            setStatus($("gcmStatus"), "Error: " + e.message, "error");
            if (window.DA_flowNote) { window.DA_flowNote("Error: " + e.message); }
            return false;
        } finally {
            cm.busy = false;
        }
    }
    function stageDone(name) {
        document.dispatchEvent(new CustomEvent("da:stage", { detail: { name: name } }));
    }

    function rows() {
        return ((cm.data && cm.data.rows) || []).filter(function (r) { return r.floor === cm.floor; });
    }
    /* The geometry's boundary is shown only where the texts found nothing
     * (or drew a boundary they could not name): what the texts found is
     * final, and the geometry fills the rest. */
    function textFound(r) {
        return !!(r.text && r.category !== "TEXT_UNNAMED");
    }
    function geoShown(r) {
        return !!(r.geo && !textFound(r));
    }

    function shown() {
        return rows().filter(function (r) {
            if (!cm.on[r.category]) { return false; }
            if (cm.onlyDecide && !(r.final_default && r.final_default.decide && !cm.dec[r.sig])) { return false; }
            if (cm.family && !((r.text && textFam(r.text) === cm.family) || (r.geo && geoFam(r.geo) === cm.family))) {
                return false;
            }
            return true;
        });
    }

    /* The Family list under the drawings: one family in both drawings,
       the reports and the table. */
    function familyList() {
        var seen = {};
        rows().forEach(function (r) {
            if (r.text) { seen[textFam(r.text)] = 1; }
            if (r.geo) { seen[geoFam(r.geo)] = 1; }
        });
        var sel = $("gcmFamily");
        sel.innerHTML = '<option value="">All families</option>' + Object.keys(seen).sort().map(function (f) {
            return "<option" + (f === cm.family ? " selected" : "") + ">" + esc(f) + "</option>";
        }).join("");
    }
    $("gcmFamily").addEventListener("change", function () {
        cm.family = this.value;
        cm.sel = null;
        overlays(); reports(); table(); inspect();
    });

    function render(newViewers) {
        summary();
        cats();
        familyList();
        if (newViewers) { viewers(); } else { overlays(); }
        reports();
        table();
        perClass();
        inspect();
    }

    function summary() {
        var s = cm.data.summary, c = s.categories;
        function card(n, label, k, sub) {
            return '<div class="gcm-card"' + (k ? ' style="--k:' + k + '"' : "") + "><b>" + n + "</b><span>" + label +
                (sub ? '<i class="muted small">' + sub + "</i>" : "") + "</span></div>";
        }
        $("gcmSummary").innerHTML =
            card(s.text_detections, "From texts") + card(s.geometry_detections, "From geometry") +
            card(c.AGREEMENT, "Agreements", CAT.AGREEMENT.rgb, s.agreement_exact + " exact &middot; " + (s.agreement_family || 0) + " different family &middot; " + s.agreement_generic + " generic room") +
            card(c.CONFLICT, "Conflicts", CAT.CONFLICT.rgb) +
            card(c.TEXT_ONLY, "Only from texts", CAT.TEXT_ONLY.rgb) +
            card(c.GEO_ONLY, "Only from geometry", CAT.GEO_ONLY.rgb) +
            card(c.TEXT_UNNAMED, "Text unnamed", CAT.TEXT_UNNAMED.rgb, "texts drew it, could not name it") +
            card(c.GEO_UNCLASSIFIED, "Geometry unclassified", CAT.GEO_UNCLASSIFIED.rgb) +
            card(c.MULTIPLE, "Multiple candidates", CAT.MULTIPLE.rgb);
    }

    function cats() {
        var here = {};
        rows().forEach(function (r) { here[r.category] = (here[r.category] || 0) + 1; });
        $("gcmCats").innerHTML = Object.keys(CAT).map(function (k) {
            return '<label class="gai-chip" style="--k:' + CAT[k].rgb + '"><input type="checkbox" data-cat="' + k + '"' +
                (cm.on[k] ? " checked" : "") + '><span class="gai-dot"></span>' + CAT[k].name + " <b>" + (here[k] || 0) + "</b></label>";
        }).join("") +
            '<button type="button" class="btn btn-ghost btn-mini" data-all="1">Check all</button>' +
            '<button type="button" class="btn btn-ghost btn-mini" data-all="0">Uncheck all</button>';
    }
    $("gcmCats").addEventListener("change", function (e) {
        var cb = e.target.closest("input[data-cat]");
        if (!cb) { return; }
        cm.on[cb.dataset.cat] = cb.checked;
        overlays(); reports(); table();
    });
    $("gcmCats").addEventListener("click", function (e) {
        var b = e.target.closest("button[data-all]");
        if (!b) { return; }
        Object.keys(cm.on).forEach(function (k) { cm.on[k] = b.dataset.all === "1"; });
        cats(); overlays(); reports(); table();
    });
    $("gcmFloor").addEventListener("change", function () {
        cm.floor = this.value;
        cm.sel = null;
        render(true);
    });
    $("gcmRerunBtn").addEventListener("click", function () { cmRun(true); });

    // ---- the two drawings (each moves on its own) -------------------------------------
    function viewers() {
        cm.gen++;
        var gen = cm.gen;
        if (cm.tv) { cm.tv.clear(); }
        if (cm.gv) { cm.gv.clear(); }
        cm.tv = window.DA_cad($("gcmTextViewer"), { labels: true });
        cm.gv = window.DA_cad($("gcmGeoViewer"), { labels: true });
        [cm.tv, cm.gv].forEach(function (v) {
            v.onClick(function (item) { if (item && item.rowId) { select(item.rowId, false); } });
        });
        overlays();
        var all = [];
        rows().forEach(function (r) {
            if (r.text) { all.push(r.text.poly); }
            if (r.geo) { all.push(r.geo.poly); }
        });
        var box = polyBox(all);
        [cm.tv, cm.gv].forEach(function (v) {
            if (box) { v.setHome(box); }
            window.DA_cadLoad(v, "/cadview/" + cm.token + "/source/scene", "/cadview/" + cm.token + "/source/lines",
                              { box: box, isStale: function () { return gen !== cm.gen; } })
                .then(function () { if (box && gen === cm.gen) { v.fit(box); } });
        });
    }

    function info(r) {
        var c = CAT[r.category];
        return "<b>" + esc(c.name) + "</b>" + (r.level === "generic" ? " (generic room)" : r.level === "family" ? ", different family" : "") +
            (r.text ? "<br>texts: " + esc(r.text.name || "(unnamed)") + " &middot; " + esc(textFam(r.text)) : "") +
            (r.geo ? "<br>geometry: " + esc(r.geo.label) + " &middot; " + esc(geoFam(r.geo)) : "") +
            (r.iou != null ? "<br>overlap " + pct(r.iou) : "");
    }
    function overlays() {
        if (!cm.tv || !cm.gv) { return; }
        var tp = [], gp = [], nt = 0, ng = 0;
        var tf = {}, gf = {};
        shown().forEach(function (r) {
            var hot = r.id === cm.sel;
            if (r.text) {
                nt++;
                var f1 = textFam(r.text), k1 = colourOf(f1);
                tf[f1] = (tf[f1] || 0) + 1;
                tp.push({ id: r.id, rowId: r.id, pts: r.text.poly, stroke: hot ? "#ffd43b" : k1, width: hot ? 4 : 2,
                          fill: rgba(k1, hot ? 0.45 : 0.22), label: r.text.name || f1, labelColor: k1,
                          info: info(r) });
            }
            if (geoShown(r)) {
                ng++;
                var f2 = geoFam(r.geo), k2 = colourOf(f2);
                gf[f2] = (gf[f2] || 0) + 1;
                gp.push({ id: r.id, rowId: r.id, pts: r.geo.poly, stroke: hot ? "#ffd43b" : k2, width: hot ? 4 : 2,
                          fill: rgba(k2, hot ? 0.45 : 0.22), label: r.geo.label, labelColor: k2, info: info(r) });
            }
        });
        cm.tv.setOverlay({ polys: tp, marks: [] });
        cm.gv.setOverlay({ polys: gp, marks: [] });
        function legend(n) {
            return Object.keys(n).sort().map(function (f) {
                return '<span class="cad-key" style="--k:' + colourOf(f) + '"></span>' + esc(f) + " " + n[f];
            }).join(" ") || '<span class="cad-muted">nothing shown</span>';
        }
        cm.tv.setLegend(legend(tf));
        cm.gv.setLegend(legend(gf));
        $("gcmTextCount").textContent = nt + " boundaries shown";
        $("gcmGeoCount").textContent = ng + " boundaries shown";
    }

    // ---- the reports below the drawings ----------------------------------------------
    function tag(r) {
        return '<span class="gcm-cat" style="--k:' + CAT[r.category].rgb + '">' + CAT[r.category].name + "</span>";
    }
    function reports() {
        var rs = shown();
        var tr = rs.filter(function (r) { return r.text; });
        var gr = rs.filter(geoShown);
        $("gcmTextReport").innerHTML = tr.length ? '<div class="table-wrap"><table class="grid gai-table"><thead><tr>' +
            "<th>Name (text)</th><th>Family</th><th>Area</th><th>Geometry there</th><th>Result</th></tr></thead><tbody>" +
            tr.map(function (r) {
                return '<tr data-id="' + r.id + '"' + (r.id === cm.sel ? ' class="geo-selrow"' : "") + "><td><b>" +
                    esc(r.text.name || "(unnamed)") + "</b></td><td>" + esc(textFam(r.text)) + "</td><td>" + fmt2(r.text.area_sqm) +
                    "</td><td>" + (r.geo ? esc(r.geo.label) + ' <span class="muted small">' + esc(geoFam(r.geo)) + "</span>" : "&mdash;") +
                    "</td><td>" + tag(r) + "</td></tr>";
            }).join("") + "</tbody></table></div>" : '<div class="muted small">Nothing from texts in the ticked categories.</div>';
        $("gcmGeoReport").innerHTML = gr.length ? '<div class="table-wrap"><table class="grid gai-table"><thead><tr>' +
            "<th>Label (geometry)</th><th>Family</th><th>Area</th><th>Confidence</th><th>Texts there</th><th>Result</th>" +
            "</tr></thead><tbody>" + gr.map(function (r) {
                var pending = r.final_default && r.final_default.decide && !cm.dec[r.sig];
                return '<tr data-id="' + r.id + '"' + (r.id === cm.sel ? ' class="geo-selrow"' : "") + "><td><b>" +
                    esc(r.geo.label) + "</b>" + (pending ? ' <span class="gcm-confirm-tag">to confirm</span>' : "") +
                    "</td><td>" + esc(geoFam(r.geo)) + "</td><td>" + fmt2(r.geo.area_sqm) + "</td><td>" +
                    (r.geo.reviewed ? "edited" : pct(r.geo.confidence)) + "</td><td>" +
                    (r.text ? esc(r.text.name || "(unnamed)") + ' <span class="muted small">' + esc(textFam(r.text)) + "</span>" : "&mdash;") +
                    "</td><td>" + tag(r) + "</td></tr>";
            }).join("") + "</tbody></table></div>" : '<div class="muted small">The texts found everything here - nothing left for the geometry.</div>';
    }

    // ---- the final drawing: the user's choice for each row --------------------------
    function storeKey(tok) { return "da-final:" + tok; }
    function loadDecisions(tok) {
        cm.dec = {};
        try { cm.dec = JSON.parse(localStorage.getItem(storeKey(tok)) || "{}") || {}; } catch (e) { cm.dec = {}; }
    }
    function saveDecisions() {
        try { localStorage.setItem(storeKey(cm.token), JSON.stringify(cm.dec)); } catch (e) { /* full or blocked */ }
    }
    // Shaft, lift, window, cut-out, column and void are Step 3's (the X
    // sequence decides every X mark once): such a row is final - nothing
    // here changes it - and no other row can be given one of these families.
    var STEP3_FAMILIES = ["Shaft", "Lift", "Window", "Cutout", "Column", "Void"];
    function fixedRow(r) { return !!(r.final_default && r.final_default.fixed); }
    function choice(r) {
        var d = Object.assign({}, r.final_default || {}), u = cm.dec[r.sig];
        if (u && !fixedRow(r)) { Object.keys(u).forEach(function (k) { d[k] = u[k]; }); }
        return d;
    }
    function familyNames() {
        var n = Object.keys(famRgb).filter(function (f) { return STEP3_FAMILIES.indexOf(f) < 0; });
        if (n.indexOf("Room") < 0) { n.push("Room"); }
        return n.sort();
    }
    function finalCounts() {
        var all = (cm.data && cm.data.rows) || [], keep = 0, out = 0, open = 0, decided = 0;
        all.forEach(function (r) {
            var d = choice(r);
            if (d.use === "exclude") { out++; } else { keep++; }
            if (r.final_default && r.final_default.decide) {
                if (cm.dec[r.sig]) { decided++; } else { open++; }
            }
        });
        $("gcmFinalCounts").innerHTML = "<b>" + keep + "</b> boundaries in the final drawing &middot; " + out +
            " left out &middot; found only by the geometry: <b>" + decided + "</b> confirmed, " +
            (open ? '<b class="geo-warn">' + open + " to confirm</b>" : "none left to confirm");
    }
    $("gcmOnlyDecide").addEventListener("change", function () {
        cm.onlyDecide = this.checked;
        overlays(); reports(); table();
    });
    $("gcmTable").addEventListener("change", function (e) {
        var el = e.target.closest("[data-final]");
        if (!el) { return; }
        var tr = el.closest("tr[data-id]");
        var r = rows().find(function (x) { return x.id === tr.dataset.id; });
        if (!r || fixedRow(r)) { return; }
        var d = choice(r);
        d[el.dataset.final] = el.value;
        cm.dec[r.sig] = { use: d.use, family: d.family, name: d.name };
        saveDecisions();
        tr.classList.remove("gcm-decide");
        finalCounts();
        if (el.dataset.final === "use") { offerPrecedence(r, tr, d.use); }
    });

    /* A conflict settled by hand, in a family with no precedence yet: ask,
     * in place, whether that choice should be the family's rule. Saved to
     * family_rules.json only when the user confirms. */
    function offerPrecedence(r, tr, use) {
        var old = tr.nextElementSibling;
        if (old && old.classList.contains("gcm-prec")) { old.remove(); }
        var fd = r.final_default || {};
        var fam = fd.precedence_family;
        if (!fam || fd.by_precedence || (use !== "text" && use !== "geometry")) { return; }
        var side = use === "geometry" ? "the geometry's" : "the texts'";
        var row = document.createElement("tr");
        row.className = "gcm-prec";
        row.innerHTML = '<td colspan="10"><span>Always take ' + side + " boundary when texts and geometry " +
            "disagree about a <b>" + esc(fam) + "</b>? This is saved in the family rules and settles " +
            "every such conflict from now on.</span> " +
            '<button type="button" class="btn btn-mini btn-primary gcm-prec-yes">Save to family rules</button> ' +
            '<button type="button" class="btn btn-mini btn-ghost gcm-prec-no">Just this one</button></td>';
        tr.parentNode.insertBefore(row, tr.nextSibling);
        row.querySelector(".gcm-prec-no").addEventListener("click", function () { row.remove(); });
        row.querySelector(".gcm-prec-yes").addEventListener("click", async function () {
            var btn = this;
            btn.disabled = true;
            try {
                var res = await post("/family_rules/precedence", { family: fam, precedence: use });
                if (!res.ok) { row.querySelector("span").textContent = res.error || "Could not save it."; return; }
                // Every open conflict of the family follows the new rule.
                var n = 0;
                ((cm.data && cm.data.rows) || []).forEach(function (x) {
                    var xd = x.final_default || {};
                    if (xd.precedence_family === fam && xd.decide) {
                        xd.decide = false;
                        xd.by_precedence = fam;
                        xd.use = use;
                        if (use === "geometry" && x.geo) { xd.name = x.geo.label || xd.name; }
                        n++;
                    }
                });
                row.querySelector("td").innerHTML = "Saved: <b>" + esc(fam) + "</b> conflicts now take " + side +
                    " boundary" + (n ? " (" + n + " more settled on this drawing)" : "") + ".";
                setTimeout(function () { row.remove(); table(); }, 2500);
            } catch (e) {
                row.querySelector("span").textContent = "Error: " + e.message;
                btn.disabled = false;
            }
        });
    }
    /* "Generate final outputs": the final drawing, then Step 6 builds its
     * areas and area statement (flow.js). */
    $("gcmFinalBtn").addEventListener("click", async function () {
        if (await finalDrawing() && window.DA_afterFinal) { window.DA_afterFinal(); }
    });
    async function finalDrawing() {
        var btn = $("gcmFinalBtn");
        btn.classList.add("is-busy");
        btn.disabled = true;
        setStatus($("gcmFinalStatus"), "Writing the final drawing...", "busy");
        try {
            var r = await post("/geoengine/final", { token: cm.token, decisions: cm.dec });
            if (!r.ok) {
                setStatus($("gcmFinalStatus"), r.error || "Could not write it.", "error");
                if (window.DA_flowNote) { window.DA_flowNote(r.error, !!r.stopped); }
                return false;
            }
            var fams = Object.keys(r.families).sort().map(function (k) { return k + " " + r.families[k]; }).join(", ");
            setStatus($("gcmFinalStatus"), "Done: " + r.boundaries + " boundaries in the final drawing (" + fams +
                      "). Its areas and area statement are built in Step 6, Final outputs.", "ok");
            $("gcmFinalDl").href = r.download;
            $("gcmFinalDl").classList.remove("hidden");
            stageDone("final");
            return true;
        } catch (e) {
            setStatus($("gcmFinalStatus"), "Error: " + e.message, "error");
            if (window.DA_flowNote) { window.DA_flowNote("Error: " + e.message); }
            return false;
        } finally {
            btn.classList.remove("is-busy");
            btn.disabled = false;
        }
    }
    /* Fast track and Rebuild: compare (the server answers from its cache
     * when nothing changed), then the final drawing with the defaults and
     * any decisions saved for this upload. */
    window.DA_compareFlow = function () { return cmRun(false); };
    window.DA_finalFlow = function () { return finalDrawing(); };

    function table() {
        finalCounts();
        var rs = shown();
        if (!rs.length) {
            $("gcmTable").innerHTML = '<div class="muted small">Nothing in the ticked categories on this floor.</div>';
            return;
        }
        var fams = familyNames();
        $("gcmTable").innerHTML = '<div class="table-wrap"><table class="grid gai-table gcm-edit"><thead><tr>' +
            "<th>ID</th><th>Text result</th><th>Geometry result</th><th>Match</th><th>Area diff.</th>" +
            "<th>Spatial match</th><th>Category</th><th>Use</th><th>Family</th><th>Name</th></tr></thead><tbody>" + rs.map(function (r) {
                var d = choice(r);
                var open = r.final_default && r.final_default.decide && !cm.dec[r.sig];
                var use = '<select data-final="use" title="Which boundary goes into the final drawing">' +
                    (r.text ? "<option value=\"text\"" + (d.use === "text" ? " selected" : "") + ">Texts'</option>" : "") +
                    (r.geo ? "<option value=\"geometry\"" + (d.use === "geometry" ? " selected" : "") + ">Geometry's</option>" : "") +
                    "<option value=\"exclude\"" + (d.use === "exclude" ? " selected" : "") + ">Leave out</option></select>";
                var fam = '<select data-final="family">' + fams.concat(fams.indexOf(d.family) < 0 && d.family ? [d.family] : [])
                    .map(function (f) { return "<option" + (f === d.family ? " selected" : "") + ">" + esc(f) + "</option>"; }).join("") + "</select>";
                var name = '<input data-final="name" type="text" maxlength="80" value="' + esc(d.name || "") + '">';
                if (fixedRow(r)) {
                    use = "Texts'" + '<div class="muted small" title="Decided in Step 3 by the X sequence - final">Step 3, final</div>';
                    fam = esc(d.family || "");
                    name = esc(d.name || "");
                }
                return '<tr data-id="' + r.id + '" class="' + (r.id === cm.sel ? "geo-selrow " : "") + (open ? "gcm-decide" : "") + '"><td>' + r.id.replace("CMP-", "") +
                    "</td><td>" + (r.text ? esc(textFam(r.text)) + ' <span class="muted small">' + esc((r.text.name || "").slice(0, 40)) + "</span>" : "&mdash;") +
                    "</td><td>" + (r.geo ? "<b>" + esc(r.geo.label) + '</b> <span class="muted small">' + esc(geoFam(r.geo)) + "</span>" : "&mdash;") +
                    "</td><td>" + CAT[r.category].match + (r.level === "generic" ? " (generic)" : r.level === "family" ? ", different family" : "") +
                    "</td><td>" + (r.area_diff == null ? "&mdash;" : Math.round(100 * r.area_diff) + "%") +
                    "</td><td>" + (r.spatial || "&mdash;") + "</td><td>" + tag(r) + "</td><td>" + use +
                    (r.final_default && r.final_default.by_precedence && !cm.dec[r.sig]
                        ? '<div class="muted small" title="Set under Family rules">by ' + esc(r.final_default.by_precedence) + " rule</div>" : "") +
                    "</td><td>" + fam +
                    "</td><td>" + name + "</td></tr>";
            }).join("") + "</tbody></table></div>";
    }
    ["gcmTable", "gcmTextReport", "gcmGeoReport"].forEach(function (id) {
        $(id).addEventListener("click", function (e) {
            if (e.target.closest("select, input")) { return; }
            var tr = e.target.closest("tr[data-id]");
            if (tr) { select(tr.dataset.id, true); }
        });
    });

    function select(id, zoom) {
        cm.sel = id;
        overlays();
        inspect();
        document.querySelectorAll("#gcmTable tr[data-id], #gcmTextReport tr[data-id], #gcmGeoReport tr[data-id]").forEach(function (tr) {
            tr.classList.toggle("geo-selrow", tr.dataset.id === id);
        });
        if (zoom) {
            var r = rows().find(function (x) { return x.id === id; });
            if (r) {
                // both drawings to the same place
                var polys = [];
                if (r.text) { polys.push(r.text.poly); }
                if (r.geo) { polys.push(r.geo.poly); }
                var box = polyBox(polys);
                if (box) {
                    var pad = Math.max(box[2] - box[0], box[3] - box[1]) * 0.8 + 1500;
                    var b = [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad];
                    cm.tv.fit(b);
                    cm.gv.fit(b);
                }
            }
        }
    }

    function inspect() {
        var host = $("gcmInspect");
        var r = rows().find(function (x) { return x.id === cm.sel; });
        if (!r) {
            host.innerHTML = '<div class="muted small">Click a boundary in either drawing, or a row below, to see both ' +
                "results side by side.</div>";
            return;
        }
        var c = CAT[r.category];
        host.innerHTML =
            '<div class="gai-head"><b>' + esc(r.id) + '</b> <span class="gcm-cat" style="--k:' + c.rgb + '">' + c.name +
            (r.level === "generic" ? " (generic room)" : r.level === "family" ? ", different family" : "") + "</span>" +
            (r.iou != null ? '<span class="muted small" style="margin-left:auto">overlap ' + pct(r.iou) + " &middot; area diff. " +
                Math.round(100 * r.area_diff) + "% &middot; centroids " + m(r.centroid_mm) + " m apart &middot; " + esc(r.spatial) + "</span>" : "") +
            "</div>" +
            '<div class="gcm-sides"><div><h4>From texts</h4>' + (r.text ? "<b>" + esc(r.text.name || "(unnamed)") + "</b><br>" +
                esc(textFam(r.text)) + " &middot; " + fmt2(r.text.area_sqm) + " sq.m" : '<span class="muted">nothing here</span>') + "</div>" +
            "<div><h4>From geometry</h4>" + (r.geo ? "<b>" + esc(r.geo.label) + "</b> " + '<span class="muted small">' + esc(r.geo.id) + "</span><br>" +
                esc(geoFam(r.geo)) + " &middot; " + fmt2(r.geo.area_sqm) + " sq.m &middot; " +
                (r.geo.reviewed ? "edited in Step 4" : "confidence " + pct(r.geo.confidence)) : '<span class="muted">nothing here</span>') + "</div></div>" +
            (r.note ? '<div class="gai-reason">' + esc(r.note) + "</div>" : "") +
            (r.candidates.length ? '<h4>Candidates</h4><ol class="gcm-cands">' + r.candidates.map(function (k) {
                return "<li><b>" + esc(k.label) + "</b> " + esc(nice(k.cls)) + ' <span class="muted small">' + esc(k.reason) +
                    " &middot; overlap " + pct(k.iou) + "</span></li>";
            }).join("") + "</ol>" : "") +
            editPanel(r) +
            (r.geo ? '<button type="button" class="btn btn-ghost btn-mini" id="gcmWhy">Open ' + esc(r.geo.label) +
                " in Step 4 - see why, or name it</button>" : "");
        wireEdit(r);
        var why = $("gcmWhy");
        if (why) {
            why.addEventListener("click", function () {
                Array.prototype.forEach.call(document.querySelectorAll(".stepnav-item"), function (b) {
                    if (/Geometry \(AI\)/.test(b.textContent)) { b.click(); }
                });
                var fl = r.floor, gid = r.geo.id;
                setTimeout(function () { if (window.DA_geoFocus) { window.DA_geoFocus(fl, gid); } }, 400);
            });
        }
    }

    /* Edit the selected object here, beside the drawings: which result goes
     * into the final drawing, its family and its name - the same choice as
     * the row in the table below, which follows it. */
    function editPanel(r) {
        var d = choice(r);
        if (fixedRow(r)) {
            return '<div class="gcm-editbox"><h4>Decided in Step 3 - final</h4>' +
                '<div class="small">' + esc(d.family || "") + (d.name ? " &middot; " + esc(d.name) : "") + "</div>" +
                '<div class="muted small">A shaft, lift, window, cut-out, column or void is taken from Step 3 ' +
                "(every X mark is decided once, by the X sequence). Nothing here changes it.</div></div>";
        }
        var fams = familyNames();
        if (d.family && fams.indexOf(d.family) < 0) { fams = fams.concat([d.family]); }
        var by = r.final_default && r.final_default.by_precedence && !cm.dec[r.sig]
            ? ' <span class="muted small">(by ' + esc(r.final_default.by_precedence) + " rule)</span>" : "";
        var pending = r.final_default && r.final_default.decide && !cm.dec[r.sig];
        var head = textFound(r) ? "Found by the texts - final; edit it here if it is wrong"
            : (pending ? "Found only by the geometry - confirm what it is" : "Found only by the geometry - confirmed");
        return '<div class="gcm-editbox' + (pending ? " is-pending" : "") + '"><h4>' + head + by + "</h4>" +
            '<label>Use <select id="gcmEdUse">' +
            (r.text ? '<option value="text"' + (d.use === "text" ? " selected" : "") + ">the texts' boundary</option>" : "") +
            (r.geo ? '<option value="geometry"' + (d.use === "geometry" ? " selected" : "") + ">the geometry's boundary</option>" : "") +
            '<option value="exclude"' + (d.use === "exclude" ? " selected" : "") + ">leave it out</option></select></label>" +
            '<label>Family <select id="gcmEdFam">' + fams.map(function (f) {
                return "<option" + (f === d.family ? " selected" : "") + ">" + esc(f) + "</option>";
            }).join("") + "</select></label>" +
            '<label>Name <input id="gcmEdName" type="text" maxlength="80" value="' + esc(d.name || "") + '"></label>' +
            (pending ? '<button type="button" class="btn btn-primary btn-mini" id="gcmEdConfirm" ' +
                'title="Keep it in the final drawing as shown">Confirm</button>' : "") +
            (cm.dec[r.sig] ? '<button type="button" class="btn btn-ghost btn-mini" id="gcmEdReset" ' +
                'title="Back to the default: texts first, geometry where the texts found nothing">Undo my change</button>' : "") +
            "</div>";
    }

    function wireEdit(r) {
        var use = $("gcmEdUse"), fam = $("gcmEdFam"), name = $("gcmEdName"), reset = $("gcmEdReset");
        if (!use) { return; }
        function save() {
            var d = choice(r);
            var before = d.use;
            cm.dec[r.sig] = { use: use.value, family: fam.value, name: name.value };
            saveDecisions();
            finalCounts();
            table();
            inspect();
            overlays();
            // a conflict settled by hand may become the family's rule
            if (use.value !== before) {
                var tr = document.querySelector('#gcmTable tr[data-id="' + r.id + '"]');
                if (tr) { offerPrecedence(r, tr, use.value); }
            }
        }
        use.addEventListener("change", function () {
            // taking the other side's boundary takes its family and name too
            var fd = r.final_default || {};
            if (use.value === "geometry" && r.geo) {
                fam.value = geoFam(r.geo);
                name.value = r.geo.label || name.value;
            } else if (use.value === "text" && r.text) {
                fam.value = textFam(r.text);
                name.value = r.text.name || name.value;
            }
            if (fd && !fam.value) { fam.value = fd.family || ""; }
            save();
        });
        fam.addEventListener("change", save);
        name.addEventListener("change", save);
        var confirm = $("gcmEdConfirm");
        if (confirm) { confirm.addEventListener("click", save); }
        if (reset) {
            reset.addEventListener("click", function () {
                delete cm.dec[r.sig];
                saveDecisions();
                finalCounts();
                table();
                inspect();
                overlays();
            });
        }
    }

    function perClass() {
        var pc = cm.data.summary.per_class;
        var cols = [["text", "From texts"], ["geometry", "From geometry"], ["agreement", "Agreement"], ["conflict", "Conflict"],
                    ["geometry_only", "Only geometry"], ["text_only", "Only texts"], ["text_unnamed", "Text unnamed"],
                    ["geo_unclassified", "Unclassified"], ["multiple", "Multiple"]];
        $("gcmPerClass").innerHTML = '<div class="table-wrap"><table class="grid gai-table"><thead><tr><th>Class</th>' +
            cols.map(function (c) { return "<th>" + c[1] + "</th>"; }).join("") + "</tr></thead><tbody>" +
            Object.keys(pc).map(function (k) {
                return "<tr><td><b>" + esc(nice(k)) + "</b></td>" + cols.map(function (c) {
                    return "<td>" + (pc[k][c[0]] || 0) + "</td>";
                }).join("") + "</tr>";
            }).join("") + "</tbody></table></div>";
    }

    // Every time the step is opened: the server answers from its cache
    // unless Step 3 or Step 4 generated something new since.
    var section = $("step-geocmp");
    new MutationObserver(function () {
        if (window.DA_flowBusy && window.DA_flowBusy()) { return; }
        if (section.classList.contains("pane-on")) { cmRun(false); }
    }).observe(section, { attributes: true, attributeFilter: ["class"] });

    window.DA_geoCompareStale = function () { cm.data = null; };

    /* A new extraction: the old comparison no longer applies. */
    window.DA_geoaiReset = function () {
        cm.data = null; cm.token = null; cm.sel = null;
        if (cm.tv) { cm.tv.clear(); }
        if (cm.gv) { cm.gv.clear(); }
        $("gcmBody").classList.add("hidden");
        setStatus($("gcmStatus"), "");
    };
})();
