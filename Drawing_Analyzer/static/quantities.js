/* Derived quantities, extraction QA, unit selection, the core module
 * checklist, and revision comparison.
 *
 * Kept in its own file rather than growing main.js further. main.js exposes
 * the hooks this module fills in, so neither file needs to know the other's
 * internals.
 */
(function () {
    "use strict";

    var $ = function (id) { return document.getElementById(id); };

    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
                     '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    function num(v, dp) {
        if (v === null || v === undefined || v === "") { return "\u2014"; }
        var n = Number(v);
        if (isNaN(n)) { return esc(v); }
        return n.toLocaleString(undefined, {
            minimumFractionDigits: dp === undefined ? 2 : dp,
            maximumFractionDigits: dp === undefined ? 2 : dp
        });
    }

    function setStatus(el, msg, kind) {
        if (!el) { return; }
        el.textContent = msg;
        el.className = "status " + (kind || "");
    }

    /* Spinner on the button that started the work, matching main.js. */
    function setBusy(btn, on) {
        if (!btn) { return; }
        btn.classList.toggle("is-busy", !!on);
        btn.disabled = !!on;
    }

    /* ---------------------------------------------------------- units --- */
    var UNIT_OPTIONS = [
        { value: "", label: "Auto-detect from drawing" },
        { value: "4", label: "Millimetres" },
        { value: "5", label: "Centimetres" },
        { value: "6", label: "Metres" },
        { value: "1", label: "Inches" },
        { value: "2", label: "Feet" }
    ];

    function fillUnitSelect(sel) {
        if (!sel) { return; }
        sel.innerHTML = "";
        UNIT_OPTIONS.forEach(function (o) {
            var opt = document.createElement("option");
            opt.value = o.value;
            opt.textContent = o.label;
            sel.appendChild(opt);
        });
    }

    fillUnitSelect($("unitSelect"));
    fillUnitSelect($("areasUnitSelect"));

    /* Step 4 inherits whatever was chosen in step 1, and whatever the
     * engine actually resolved on the step-1 upload, so the unit is
     * asked for once per session rather than at every stage. */
    var resolvedUnitCode = "";

    function syncAreasUnit() {
        var src = $("unitSelect"), dst = $("areasUnitSelect");
        if (!src || !dst) { return; }
        var want = src.value || resolvedUnitCode || "";
        for (var i = 0; i < dst.options.length; i++) {
            if (dst.options[i].value === want) { dst.selectedIndex = i; return; }
        }
    }
    if ($("unitSelect")) {
        $("unitSelect").addEventListener("change", function () {
            resolvedUnitCode = "";
            syncAreasUnit();
        });
    }
    fillUnitSelect($("cmpPrevUnits"));
    fillUnitSelect($("cmpCurrUnits"));

    /* Unit and floor-layer options only apply to a DXF revision, so they
     * stay hidden until one is picked. */
    function wireRevisionInput(fileId, optsId) {
        var f = $(fileId), opts = $(optsId);
        if (!f || !opts) { return; }
        f.addEventListener("change", function () {
            var name = f.files[0] ? f.files[0].name.toLowerCase() : "";
            opts.classList.toggle("hidden", !name.endsWith(".dxf"));
        });
    }
    wireRevisionInput("cmpPrev", "cmpPrevOpts");
    wireRevisionInput("cmpCurr", "cmpCurrOpts");

    /* main.js reads this before every upload so the choice travels with the
     * file. Returns "" for auto-detect. */
    window.DA_selectedUnits = function (which) {
        var sel = $(which === "areas" ? "areasUnitSelect" : "unitSelect");
        return sel ? sel.value : "";
    };

    window.DA_showUnits = function (units, bannerId) {
        var banner = $(bannerId || "unitBanner");
        if (!units) { return; }

        /* Remember what the engine settled on so step 4 starts there. */
        if (units.insunits_code) {
            resolvedUnitCode = String(units.insunits_code);
            syncAreasUnit();
        }
        if (!banner) { return; }

        banner.classList.remove("hidden");
        if (units.source === "override") {
            setStatus(banner, "Units set manually to " + units.short_label +
                      ". Output is in sq.m and m.", "ok");
        } else if (units.source === "geometry") {
            setStatus(banner, "Units: " + units.short_label + ". " +
                      (units.note || ""), "ok");
        } else if (units.source === "header") {
            setStatus(banner, "Units read from the DXF header: " +
                      units.short_label + ".", "ok");
        } else {
            setStatus(banner, "This DXF declares no units and the geometry " +
                      "was inconclusive, so millimetres were assumed. If the " +
                      "areas look wrong, pick the unit above and re-upload.",
                      "error");
        }
    };

    /* ------------------------------------------------------------- QA --- */
    window.DA_renderQA = function (data) {
        var panel = $("qaPanel");
        if (!panel || !data || !data.qa) { return; }
        var qa = data.qa;
        var st = data.stats || {};
        var totals = qa.totals || {};
        var chips = [];
        if (st.boundary_method) {
            chips.push(chip("boundary method", st.boundary_method,
                            st.boundary_method === "trace" ? "ok" : ""));
        }
        if (st.traced_boundaries) {
            chips.push(chip("traced as real outlines", st.traced_boundaries, "ok"));
        }
        if (st.traced_fallback_raycast) {
            chips.push(chip("fell back to a box (walls leaked)",
                            st.traced_fallback_raycast, "warn"));
        }
        if (st.traced_holes_deducted) {
            chips.push(chip("interior voids deducted",
                            st.traced_holes_deducted, "ok"));
        }
        chips.push(chip("Boundaries drawn", qa.boundary_count, "ok"));
        chips.push(chip("Labels with a problem", qa.problem_count,
                        qa.problem_count ? "warn" : "ok"));
        Object.keys(totals).sort().forEach(function (k) {
            chips.push(chip(k, totals[k], k === "no_walls" ? "warn" : ""));
        });
        $("qaSummary").innerHTML = chips.join("");
        if (data.qa_download) {
            $("qaDlXlsx").href = data.qa_download.xlsx || "#";
            $("qaDlJson").href = data.qa_download.json || "#";
        }
        panel.classList.remove("hidden");
    };

    function chip(label, value, kind) {
        var colour = kind === "warn" ? "#8a2b2b"
                   : kind === "ok" ? "#1e7b34" : "#1f3a5f";
        return '<div class="sum-item"><b style="color:' + colour + '">' +
               esc(value) + "</b> <span>" + esc(label) + "</span></div>";
    }

    /* --------------------------------------------- derived quantities --- */
    function panel(title, cells, note) {
        var html = '<div class="qpanel"><h4>' + esc(title) + '</h4><div class="qgrid">';
        cells.forEach(function (c) {
            html += '<div class="qcell"><span class="qlabel">' + esc(c[0]) +
                    '</span><span class="qvalue ' + (c[3] || "") + '">' + c[1] +
                    (c[2] ? ' <span class="qunit">' + esc(c[2]) + "</span>" : "") +
                    "</span></div>";
        });
        html += "</div>";
        if (note) { html += '<div class="qnote">' + esc(note) + "</div>"; }
        return html + "</div>";
    }

    function tablePanel(title, headers, rows, note) {
        if (!rows || !rows.length) { return ""; }
        var html = '<div class="qpanel"><h4>' + esc(title) +
                   '</h4><div class="table-wrap"><table class="grid" ' +
                   'style="margin:0;border-radius:0"><thead><tr>';
        headers.forEach(function (h) { html += "<th>" + esc(h) + "</th>"; });
        html += "</tr></thead><tbody>";
        rows.forEach(function (r) {
            html += "<tr>";
            r.forEach(function (c) { html += "<td>" + c + "</td>"; });
            html += "</tr>";
        });
        html += "</tbody></table></div>";
        if (note) { html += '<div class="qnote">' + esc(note) + "</div>"; }
        return html + "</div>";
    }

    /* The reports for one scope - the whole sheet or one floor. */
    function quantitySections(q, scope) {
        var html = "";

        var rec = q.reconciliation;
        if (rec) {
            var cls = rec.verdict.indexOf("Good") === 0 ? "verdict-good"
                    : rec.verdict.indexOf("Fair") === 0 ? "verdict-fair"
                    : "verdict-poor";
            html += panel("Area reconciliation", [
                ["Floor plate", num(rec.floor_area_sqm), "sq.m"],
                ["Measured rooms", num(rec.measured_room_area_sqm), "sq.m"],
                ["Unaccounted", num(rec.unaccounted_sqm), "sq.m"],
                ["Coverage", num(rec.coverage_pct) + "%", "", cls]
            ], rec.verdict);
        }

        var p = q.perimeter;
        if (p) {
            html += panel("Perimeter", [
                ["Floor outline", num(p.floor_perimeter_m), "m"],
                ["All rooms", num(p.total_room_perimeter_m), "m"],
                ["Longest room", num(p.longest_room_perimeter_m), "m"],
                ["Shortest room", num(p.shortest_room_perimeter_m), "m"]
            ]);
        }

        var k = q.kpis;
        if (k) {
            html += tablePanel("Area split by family",
                ["Family", "Count", "Area (sq.m)", "% of floor"],
                (k.family_split || []).map(function (f) {
                    return [esc(f.family), f.count, num(f.total_sqm),
                            num(f.pct_of_floor)];
                }));
        }

        var gh = q.geometry_health;
        if (gh) {
            if (gh.clean) {
                html += panel("Boundary geometry check", [
                    ["Boundaries checked", num(gh.checked, 0), ""],
                    ["Issues found", "0", "", "verdict-good"]
                ], "No duplicate, contained, sliver or partially overlapping " +
                   "boundaries found.");
            } else {
                html += geometryPanel(gh, scope || "all");
            }
        }
        return html;
    }

    /* ----------------------------------- geometry findings: review state --- *
     * Each finding can be marked resolved (it moves to "Boundary checks
     * resolved" under the table) or rejected (struck through where it is).
     * The choice is kept in this browser for this run, so re-opening the
     * results or re-calculating the same upload keeps it. */
    var geoRun = "";

    function geoStoreKey() { return "da-geo-review:" + geoRun; }

    function geoLoad() {
        try {
            var v = JSON.parse(localStorage.getItem(geoStoreKey()) || "{}");
            return (v && typeof v === "object") ? v : {};
        } catch (e) { return {}; }
    }

    function geoSave(state) {
        try { localStorage.setItem(geoStoreKey(), JSON.stringify(state)); }
        catch (e) { /* storage unavailable - the choice lasts for this view */ }
    }

    function geoKey(scope, f) {
        return [scope, f.issue, f.room_a, f.room_b, f.area_a_sqm,
                f.area_b_sqm].join("|");
    }

    function geometryPanel(gh, scope) {
        var head = "<thead><tr><th>Issue</th><th>Boundary A</th>" +
                   "<th>Boundary B</th><th>Detail</th>" +
                   '<th class="geo-status-h">Status</th></tr></thead>';
        var rows = "";
        gh.findings.forEach(function (f, i) {
            rows += '<tr class="geo-row" data-i="' + i + '" data-key="' +
                    esc(encodeURIComponent(geoKey(scope, f))) + '">' +
                    "<td><b>" + esc(f.issue) + "</b></td><td>" + esc(f.room_a) +
                    "</td><td>" + esc(f.room_b) + "</td><td>" + esc(f.detail) +
                    '</td><td class="geo-actions">' +
                    '<button type="button" class="geo-btn geo-ok" ' +
                    'title="Resolved - move to Boundary checks resolved">\u2713</button>' +
                    '<button type="button" class="geo-btn geo-no" ' +
                    'title="Not a problem - strike it out (click again to restore)">\u2717</button>' +
                    '<button type="button" class="geo-btn geo-undo" ' +
                    'title="Move back to the open checks">Undo</button>' +
                    "</td></tr>";
        });
        var table = '<div class="table-wrap"><table class="grid" ' +
                    'style="margin:0;border-radius:0">' + head;
        return '<div class="qpanel geo-panel" data-scope="' + esc(scope) +
               '" data-total="' + gh.findings.length + '">' +
               '<h4 class="geo-title"></h4>' + table +
               '<tbody class="geo-open">' + rows + "</tbody></table></div>" +
               '<div class="qnote">Reported, not deleted. \u2713 marks a finding ' +
               "resolved and moves it below; \u2717 strikes it out as not a real " +
               "problem. Choices are remembered in this browser for this run.</div>" +
               "</div>" +
               '<div class="qpanel geo-resolved" data-scope="' + esc(scope) + '">' +
               '<h4 class="geo-rtitle"></h4>' + table +
               '<tbody class="geo-done"></tbody></table></div>' +
               '<div class="qnote geo-empty">Nothing resolved yet - click ' +
               "\u2713 on a finding above.</div></div>";
    }

    /* Put every row where its saved state says, and refresh the counts. */
    function geoApply(host) {
        var state = geoLoad();
        Array.prototype.forEach.call(host.querySelectorAll(".geo-panel"),
            function (panel) {
                var resolved = panel.nextElementSibling;
                var open = panel.querySelector(".geo-open");
                var done = resolved.querySelector(".geo-done");
                var rows = Array.prototype.slice.call(
                    panel.querySelectorAll(".geo-row"))
                    .concat(Array.prototype.slice.call(
                        resolved.querySelectorAll(".geo-row")));
                // Keep the original order in both tables.
                rows.sort(function (a, b) {
                    return Number(a.dataset.i) - Number(b.dataset.i);
                });
                var nOk = 0, nNo = 0;
                rows.forEach(function (row) {
                    var st = state[decodeURIComponent(row.dataset.key)] || "";
                    row.classList.toggle("geo-struck", st === "no");
                    row.classList.toggle("geo-is-resolved", st === "ok");
                    if (st === "ok") { nOk++; done.appendChild(row); }
                    else { open.appendChild(row); if (st === "no") { nNo++; } }
                });
                var total = Number(panel.dataset.total) || rows.length;
                panel.querySelector(".geo-title").textContent =
                    "Boundary geometry check \u2014 " + total + " issue(s)" +
                    (nOk || nNo ? " \u00b7 " + (total - nOk - nNo) + " open, " +
                     nOk + " resolved, " + nNo + " struck out" : "");
                resolved.querySelector(".geo-rtitle").textContent =
                    "Boundary checks resolved \u2014 " + nOk;
                resolved.querySelector(".geo-empty").style.display =
                    nOk ? "none" : "";
                resolved.querySelector("table").style.display = nOk ? "" : "none";
                // Everything resolved: no empty header row left behind.
                panel.querySelector("table").style.display =
                    (total - nOk) ? "" : "none";
            });
    }

    function geoWire(host) {
        if (host.dataset.geoWired) { return; }
        host.dataset.geoWired = "1";
        host.addEventListener("click", function (ev) {
            var btn = ev.target.closest(".geo-btn");
            if (!btn) { return; }
            var row = btn.closest(".geo-row");
            var key = decodeURIComponent(row.dataset.key);
            var state = geoLoad();
            if (btn.classList.contains("geo-ok")) {
                state[key] = "ok";
            } else if (btn.classList.contains("geo-no")) {
                // Clicking the cross again restores the finding.
                if (state[key] === "no") { delete state[key]; }
                else { state[key] = "no"; }
            } else {
                delete state[key];
            }
            geoSave(state);
            geoApply(host);
        });
    }

    function floorHeading(text, sub) {
        return '<div style="margin:22px 0 10px;padding:12px 16px;background:#1f3a5f;' +
               'color:#fff;border-radius:8px;font-weight:700;font-size:16px">' +
               esc(text) + (sub ? ' &nbsp;<span style="font-weight:400;font-size:13px">' +
               esc(sub) + "</span>" : "") + "</div>";
    }

    window.DA_renderQuantities = function (data) {
        var host = $("quantityPanels");
        if (!host) { return; }
        host.innerHTML = "";
        var q = data.quantities || {};
        var html = "";
        var floors = q.floors || [];

        if (floors.length > 1) {
            // Floor by floor first, then the sheet as a whole.
            html += tablePanel("Floor-wise summary",
                ["Floor", "Plate (sq.m)", "Rooms", "Measured (sq.m)",
                 "Coverage", "Geometry issues"],
                floors.map(function (f) {
                    var rec = f.reconciliation || {};
                    var gh = f.geometry_health || {};
                    return ["<b>" + esc(f.name) + "</b>", num(f.area_sqm),
                            rec.room_count || 0, num(rec.measured_room_area_sqm),
                            num(rec.coverage_pct) + "%", gh.issue_count || 0];
                }));
            floors.forEach(function (f) {
                html += floorHeading(f.name, "plate " + num(f.area_sqm) + " sq.m");
                html += quantitySections(f, "floor:" + f.name);
            });
            html += floorHeading("All floors together",
                                 floors.length + " floors");
        }
        var dl = (data.download && data.download.quantities_xlsx) || "";
        geoRun = dl.split("/").pop() || "current";
        html += quantitySections(q, "all");

        host.innerHTML = html ||
            '<p class="muted small">No derived quantities available.</p>';
        geoWire(host);
        geoApply(host);

        if (data.download) {
            if ($("qtyDlXlsx")) {
                $("qtyDlXlsx").href = data.download.quantities_xlsx || "#";
            }
            if ($("qtyDlJson")) {
                $("qtyDlJson").href = data.download.quantities_json || "#";
            }
        }
    };

    /* ----------------------------------------------- family rules editor --- */
    var rulesEditor = $("rulesEditor");
    var rulesLoaded = null;

    /* `isNew` marks a family that exists only in the browser. Those carry
     * their own Save and Discard, because the pair at the top of the
     * section has scrolled out of sight by the time you are looking at a
     * card appended to the bottom of the grid. */
    function ruleRow(fam, i, isNew) {
        return '<div class="rule-row' + (isNew ? " rule-new" : "") + '">' +
            '<div class="rule-head">' +
            '<span class="rule-order">' + (i + 1) + "</span>" +
            '<input class="rule-name" value="' + esc(fam.name) +
            '" placeholder="Family name" />' +
            '<input class="rule-layer-in" value="' + esc(fam.layer) +
            '" placeholder="LAYER" title="Boundary layer becomes ' +
            'LAYER_BOUNDARY" />' +
            '<input class="rule-color" type="number" min="1" max="255" value="' +
            esc(fam.color) + '" title="AutoCAD colour 1-255" />' +
            (isNew ? "" : ruleDelBtn()) + "</div>" +
            '<textarea class="rule-kw" rows="4" spellcheck="false" ' +
            'placeholder="One keyword per line">' +
            esc((fam.keywords || []).join("\n")) + "</textarea>" +
            (isNew ? ruleActions() : "") + "</div>";
    }

    function ruleDelBtn() {
        return '<button type="button" class="rule-del" ' +
            'title="Delete this family" aria-label="Delete this family">' +
            '<svg viewBox="0 0 16 16" width="13" height="13" ' +
            'aria-hidden="true"><path fill="currentColor" d="M6.5 1h3a.5.5 0 ' +
            '0 1 .5.5V2h3.5a.5.5 0 0 1 0 1h-11a.5.5 0 0 1 0-1H6v-.5a.5.5 0 0 ' +
            '1 .5-.5zM3.6 4h8.8l-.62 9.06A1.5 1.5 0 0 1 10.28 14.5H5.72a1.5 ' +
            '1.5 0 0 1-1.5-1.44L3.6 4zm2.65 1.75a.5.5 0 0 0-.5.5v5.5a.5.5 0 ' +
            '0 0 1 0v-5.5a.5.5 0 0 0-.5-.5zm3.5 0a.5.5 0 0 0-.5.5v5.5a.5.5 0 ' +
            '0 0 1 0v-5.5a.5.5 0 0 0-.5-.5z"/></svg></button>';
    }

    /* Deleting throws away a keyword list that took someone a while to
     * build, so the bin only arms the card; the second click commits. */
    function ruleDeleteBar(name, kwCount) {
        return '<div class="rule-actions rule-confirm">' +
            '<span class="rule-msg">Delete ' + esc(name || "this family") +
            (kwCount ? " and its " + kwCount + " keyword" +
             (kwCount === 1 ? "" : "s") : "") + "?</span>" +
            '<button type="button" class="btn btn-mini btn-ghost ' +
            'rule-del-cancel">Cancel</button>' +
            '<button type="button" class="btn btn-mini btn-danger ' +
            'rule-del-yes">Delete</button></div>';
    }

    function armDelete(row) {
        // One armed card at a time, so the red outline always points at the
        // family the buttons would remove.
        Array.prototype.forEach.call(
            rulesEditor.querySelectorAll(".rule-row.rule-doomed"),
            function (other) { if (other !== row) { disarmDelete(other); } });
        if (row.querySelector(".rule-confirm")) { return; }
        var name = row.querySelector(".rule-name").value.trim();
        var kw = row.querySelector(".rule-kw").value.split("\n").filter(
            function (l) { return l.trim(); }).length;
        row.classList.add("rule-doomed");
        row.insertAdjacentHTML("beforeend", ruleDeleteBar(name, kw));
    }

    function disarmDelete(row) {
        var bar = row.querySelector(".rule-confirm");
        if (bar) { bar.parentNode.removeChild(bar); }
        row.classList.remove("rule-doomed");
    }

    function ruleActions() {
        return '<div class="rule-actions">' +
            '<span class="rule-msg">Not saved yet</span>' +
            '<button type="button" class="btn btn-mini btn-ghost rule-discard"' +
            ' title="Drop this family again. family_rules.json is not ' +
            'touched.">Discard</button>' +
            '<button type="button" class="btn btn-mini btn-primary rule-save"' +
            ' title="Write every family, this one included, to ' +
            'family_rules.json.">Save</button></div>';
    }

    /* The card's own status line. setStatus() would strip its class off. */
    function setCardMsg(el, msg, kind) {
        if (!el) { return; }
        el.textContent = msg;
        el.className = "rule-msg " + (kind || "");
    }

    function renderRules(families) {
        rulesLoaded = families;
        // map() hands the callback a third argument, which would make every
        // row read as new. Pass only the two that mean anything here.
        rulesEditor.innerHTML = families.map(function (fam, i) {
            return ruleRow(fam, i);
        }).join("");
        wireRuleInputs();
        checkDuplicates();
    }

    function renumberRules() {
        Array.prototype.forEach.call(
            rulesEditor.querySelectorAll(".rule-order"),
            function (el, i) { el.textContent = i + 1; });
    }

    function wireRuleInputs() {
        Array.prototype.forEach.call(
            rulesEditor.querySelectorAll(".rule-kw, .rule-name"),
            function (el) {
                el.removeEventListener("input", checkDuplicates);
                el.addEventListener("input", checkDuplicates);
            });
    }

    /* A keyword under two families silently belongs to whichever sits
     * higher, which is the single most confusing thing about this file. */
    function checkDuplicates() {
        var owner = {}, losers = {};
        collectRules().forEach(function (fam) {
            fam.keywords.forEach(function (kw) {
                var key = kw.trim();
                if (!key) { return; }
                if (!owner[key]) { owner[key] = fam.name; return; }
                if (owner[key] === fam.name) { return; }
                losers[key] = losers[key] || [];
                if (losers[key].indexOf(fam.name) < 0) {
                    losers[key].push(fam.name);
                }
            });
        });
        var keys = Object.keys(losers);
        var el = $("rulesWarning");
        if (!keys.length) { setStatus(el, ""); return; }
        var shown = keys.slice(0, 4).map(function (k) {
            return k + ": " + owner[k] + " wins over " + losers[k].join(", ");
        }).join("   |   ");
        setStatus(el, "Claimed by more than one family — " + shown +
                  (keys.length > 4 ? "   (+" + (keys.length - 4) + " more)" : ""),
                  "info");
    }

    /* Read straight from the rows, so a family added in the browser is
     * saved the same way as one that came from the file. */
    function collectRules(skipRow) {
        var rows = Array.prototype.filter.call(
            rulesEditor.querySelectorAll(".rule-row"),
            function (row) { return row !== skipRow; });
        return rows.map(
            function (row) {
                var val = function (sel) {
                    var el = row.querySelector(sel);
                    return el ? el.value : "";
                };
                return {
                    name: val(".rule-name").trim(),
                    layer: val(".rule-layer-in").trim(),
                    color: Number(val(".rule-color")) || 7,
                    keywords: val(".rule-kw").split("\n").filter(
                        function (l) { return l.trim(); })
                };
            });
    }

    async function fetchRules(msg) {
        setStatus($("rulesStatus"), "Loading family rules...", "busy");
        try {
            var res = await fetch("/family_rules");
            var data = await res.json();
            if (!data.ok) {
                setStatus($("rulesStatus"), data.error || "Could not load rules.",
                          "error");
                return;
            }
            renderRules(data.families);
            setStatus($("rulesStatus"), msg || "", msg ? "ok" : "");
        } catch (err) {
            setStatus($("rulesStatus"), "Network error: " + err.message, "error");
        }
    }

    /* `cardMsg` is the status line of the card that started the save, or
     * null when the section's own button did. The section's line is at the
     * top of the grid, too far from a card at the bottom to be read. */
    async function saveRules(btn, cardMsg, opts) {
        opts = opts || {};
        setStatus($("rulesStatus"), opts.busy || "Saving...", "busy");
        setCardMsg(cardMsg, opts.busy || "Saving...", "busy");
        setBusy(btn, true);
        try {
            var res = await fetch("/family_rules", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    families: opts.families || collectRules()
                })
            });
            var data = await res.json();
            if (!data.ok) {
                setStatus($("rulesStatus"), data.error || "Save failed.",
                          "error");
                setCardMsg(cardMsg, data.error || "Save failed.", "error");
                return;
            }
            renderRules(data.families);
            if (window.DA_familiesChanged) {
                window.DA_familiesChanged(data.family_list,
                                          data.family_options);
            }
            setStatus($("rulesStatus"), opts.okMsg ||
                      "Saved. Re-run Step 3 or Step 4 to apply the new " +
                      "rules to a drawing.", "ok");
        } catch (err) {
            setStatus($("rulesStatus"), "Network error: " + err.message,
                      "error");
            setCardMsg(cardMsg, "Network error: " + err.message, "error");
        } finally {
            // Harmless when a successful save has already replaced the row.
            setBusy(btn, false);
        }
    }

    if (rulesEditor) {
        fetchRules();

        $("rulesToggleBtn").addEventListener("click", function () {
            var hidden = rulesEditor.classList.toggle("hidden");
            this.textContent = hidden ? "Show rules" : "Hide rules";
        });

        $("rulesReloadBtn").addEventListener("click", async function () {
            setBusy(this, true);
            try {
                await fetchRules("Reloaded from family_rules.json.");
            } finally {
                setBusy(this, false);
            }
        });

        $("rulesAddBtn").addEventListener("click", function () {
            if (rulesEditor.classList.contains("hidden")) {
                $("rulesToggleBtn").click();
            }
            var n = rulesEditor.querySelectorAll(".rule-row").length;
            rulesEditor.insertAdjacentHTML("beforeend", ruleRow(
                { name: "", layer: "", color: 7, keywords: [] }, n, true));
            wireRuleInputs();
            var row = rulesEditor.lastElementChild;
            row.scrollIntoView({ behavior: "smooth", block: "center" });
            row.querySelector(".rule-name").focus();
            setStatus($("rulesStatus"), "New family added below. Give it a " +
                      "name, a layer and at least one keyword, then Save on " +
                      "the card itself — or Discard to drop it again.",
                      "info");
        });

        $("rulesSaveBtn").addEventListener("click", function () {
            saveRules(this, null);
        });

        /* Delegated, so a card added after page load is wired too. */
        rulesEditor.addEventListener("click", function (ev) {
            var btn = ev.target.closest && ev.target.closest("button");
            if (!btn || !rulesEditor.contains(btn)) { return; }
            var row = btn.closest(".rule-row");
            if (!row) { return; }
            if (btn.classList.contains("rule-save")) {
                saveRules(btn, row.querySelector(".rule-msg"));
                return;
            }
            if (btn.classList.contains("rule-del")) {
                armDelete(row);
                return;
            }
            if (btn.classList.contains("rule-del-cancel")) {
                disarmDelete(row);
                setStatus($("rulesStatus"), "");
                return;
            }
            if (btn.classList.contains("rule-del-yes")) {
                var gone = row.querySelector(".rule-name").value.trim();
                // The card stays on screen until the server has written the
                // file, so a refused save leaves the grid matching the file.
                saveRules(btn, row.querySelector(".rule-msg"), {
                    families: collectRules(row),
                    busy: "Deleting...",
                    okMsg: "Deleted " + (gone ? '"' + gone + '"' : "the family") +
                           ". Re-run Step 3 or Step 4 to apply the change " +
                           "to a drawing."
                });
                return;
            }
            if (btn.classList.contains("rule-discard")) {
                var named = row.querySelector(".rule-name").value.trim();
                row.parentNode.removeChild(row);
                renumberRules();
                checkDuplicates();
                setStatus($("rulesStatus"), "Discarded " +
                          (named ? '"' + named + '"' : "the new family") +
                          ". family_rules.json was not touched.", "info");
            }
        });
    }

    /* ------------------------------------------- core module checklist --- */
    /* The module list is the agreed scope. Ticks live only as long as the
     * page: a refresh or a new tab starts with none ticked. They used to be
     * kept in localStorage, which every tab shares; that copy is removed. */
    var MODULE_KEY = "da-core-modules";
    try { localStorage.removeItem(MODULE_KEY); } catch (e) { /* none */ }

    function loadModuleTicks() {
        return {};
    }

    var moduleList = $("moduleList");
    if (moduleList) {
        var ticks = loadModuleTicks();
        var boxes = moduleList.querySelectorAll(".modchk");

        var refreshModules = function () {
            var done = 0;
            Array.prototype.forEach.call(boxes, function (b) {
                b.parentNode.classList.toggle("done", b.checked);
                if (b.checked) { done += 1; }
            });
            setStatus($("moduleProgress"),
                      done + " of " + boxes.length + " complete.",
                      done === boxes.length ? "ok" : "info");
        };

        Array.prototype.forEach.call(boxes, function (b) {
            b.checked = ticks[b.getAttribute("data-mod")] === true;
            b.addEventListener("change", function () {
                ticks[b.getAttribute("data-mod")] = b.checked;
                refreshModules();
            });
        });
        refreshModules();
    }

    /* ---------------------------------------------- revision compare --- */
    var cmpBtn = $("cmpBtn");
    if (cmpBtn) {
        cmpBtn.addEventListener("click", async function () {
            var st = $("cmpStatus");
            var prev = $("cmpPrev").files[0];
            var curr = $("cmpCurr").files[0];
            if (!prev || !curr) {
                setStatus(st, "Pick a previous and a current file. Each can " +
                              "be an areas JSON, an areas Excel workbook, or " +
                              "a corrected DXF.", "error");
                return;
            }
            setStatus(st, "Comparing the two files...", "busy");
            setBusy(cmpBtn, true);
            var fd = new FormData();
            fd.append("previous", prev);
            fd.append("current", curr);
            fd.append("previous_units", ($("cmpPrevUnits") || {}).value || "");
            fd.append("current_units", ($("cmpCurrUnits") || {}).value || "");
            fd.append("previous_layer", ($("cmpPrevLayer") || {}).value || "");
            fd.append("current_layer", ($("cmpCurrLayer") || {}).value || "");
            try {
                var res = await fetch("/compare", { method: "POST", body: fd });
                var data = await res.json();
                if (!data.ok) {
                    setStatus(st, data.error || "Comparison failed.", "error");
                    return;
                }
                renderComparison(data);
                setStatus(st, "Done.", "ok");
            } catch (err) {
                setStatus(st, "Network error: " + err.message, "error");
            } finally {
                setBusy(cmpBtn, false);
            }
        });
    }

    function describeSource(info) {
        if (!info) { return ""; }
        if (info.source === "dxf") {
            return "DXF, measured on the spot (floor layer '" +
                   esc(info.floor_layer) + "', " + esc(info.units) + ")";
        }
        if (info.source === "xlsx") {
            return "Excel, sheet '" + esc(info.sheet) + "'";
        }
        return "areas JSON";
    }

    function renderComparison(data) {
        var c = data.comparison;
        var s = c.summary;
        var src = data.sources || c.sources;
        var note = $("cmpSourceNote");
        if (!note) {
            note = document.createElement("div");
            note.id = "cmpSourceNote";
            note.className = "src-note";
            $("cmpResults").insertBefore(note, $("cmpResults").firstChild);
        }
        note.innerHTML = src
            ? "Previous: " + describeSource(src.previous) + " &nbsp;|&nbsp; " +
              "Current: " + describeSource(src.current) +
              " &nbsp;|&nbsp; matched " + (s.old_room_count) + " vs " +
              (s.new_room_count) + " rooms by name and family."
            : "";
        var deltaCls = s.delta_sqm >= 0 ? "delta-up" : "delta-down";
        $("cmpSummary").innerHTML =
            chip("previous total (sq.m)", num(s.old_total_sqm)) +
            chip("current total (sq.m)", num(s.new_total_sqm)) +
            chip("delta (sq.m)", num(s.delta_sqm),
                 s.delta_sqm >= 0 ? "ok" : "warn") +
            chip("added", s.added_count, "ok") +
            chip("removed", s.removed_count, s.removed_count ? "warn" : "") +
            chip("changed", s.changed_count);

        var html = tablePanel("Changed",
            ["Room", "Family", "Previous", "Current", "Delta (sq.m)", "Delta (%)"],
            c.changed.map(function (r) {
                var cls = r.delta_sqm >= 0 ? "delta-up" : "delta-down";
                return [esc(r.name), esc(r.family), num(r.old_sqm),
                        num(r.new_sqm),
                        '<b class="' + cls + '">' + num(r.delta_sqm) + "</b>",
                        num(r.delta_pct)];
            }));
        html += tablePanel("Added", ["Room", "Family", "Area (sq.m)"],
            c.added.map(function (r) {
                return [esc(r.name), esc(r.family), num(r.new_sqm)];
            }));
        html += tablePanel("Removed", ["Room", "Family", "Area (sq.m)"],
            c.removed.map(function (r) {
                return [esc(r.name), esc(r.family), num(r.old_sqm)];
            }));
        $("cmpTables").innerHTML = html ||
            '<p class="muted small">No differences found.</p>';

        $("cmpDlXlsx").href = data.download.xlsx;
        $("cmpDlJson").href = data.download.json;
        $("cmpResults").classList.remove("hidden");
    }
})();
