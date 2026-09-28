/* Section 6: the layers the uploaded drawing is built from.
 *
 * The inventory is read during the Step 1 parse and kept in the upload's
 * meta file, so this only fetches and renders it. main.js calls
 * DA_layersReady once an upload lands.
 */
(function () {
    "use strict";

    var $ = function (id) { return document.getElementById(id); };

    var card = $("step-layers");
    var host = $("layersGroups");
    if (!card || !host) { return; }

    var state = { token: null, groups: [], hideEmpty: false, filter: "" };

    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
                     '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    function num(v) {
        return Number(v || 0).toLocaleString();
    }

    function setStatus(msg, kind) {
        var el = $("layersStatus");
        el.textContent = msg || "";
        el.className = "status " + (kind || "");
    }

    /* ACI 1-9 are the only indices worth a swatch; past that the mapping
     * is a 255-entry table this page has no reason to carry. */
    var ACI = ["#000", "#f00", "#ff0", "#0f0", "#0ff", "#00f", "#f0f",
               "#333", "#808080", "#c0c0c0"];

    function swatch(color) {
        var c = ACI[color] || "#8899aa";
        return '<i class="lay-dot" style="background:' + c + '"></i>';
    }

    function matches(L, group) {
        if (!state.filter) { return true; }
        var f = state.filter;
        return (L.name || "").toLowerCase().indexOf(f) >= 0
            || (L.family || "").toLowerCase().indexOf(f) >= 0
            || (L.discipline || "").toLowerCase().indexOf(f) >= 0
            || (group || "").toLowerCase().indexOf(f) >= 0;
    }

    function rowHtml(L) {
        var total = (L.entities || 0) + (L.in_blocks || 0);
        var wall = L.counts_as_wall || "yes";
        return '<tr' + (total ? "" : ' class="lay-empty"') + ">" +
            "<td>" + swatch(L.color) + "<span>" + esc(L.name) + "</span>" +
            (L.not_in_table
                ? ' <span class="lay-flag">not in the layer table</span>' : "") +
            "</td>" +
            '<td class="num">' + num(L.entities) + "</td>" +
            '<td class="num">' + num(L.in_blocks) + "</td>" +
            '<td class="num">' + num(L.texts) + "</td>" +
            '<td class="num">' + num(L.closed_polylines) + "</td>" +
            '<td><span class="lay-wall lay-wall-' +
            wall.replace(/[^a-z]/g, "") + '">' + esc(wall) + "</span></td>" +
            '<td class="muted small">' + esc(L.kinds || "") + "</td>" +
            "</tr>";
    }

    function groupHtml(g) {
        var rows = g.layers.filter(function (L) {
            if (state.hideEmpty && !(L.entities || L.in_blocks)) {
                return false;
            }
            return matches(L, g.name);
        });
        if (!rows.length) { return ""; }
        return '<section class="lay-group lay-' + esc(g.kind) + '">' +
            '<h3><span class="lay-kind">' +
            (g.kind === "family" ? "family" :
             g.kind === "discipline" ? "discipline" : "no prefix") +
            "</span>" + esc(g.name) +
            "<b>" + rows.length + " layer" + (rows.length === 1 ? "" : "s") +
            "</b>" +
            '<span class="muted small">' + num(g.entities) +
            " entities</span></h3>" +
            '<div class="table-wrap"><table class="lay-table"><thead><tr>' +
            "<th>Layer</th><th>Modelspace</th><th>In blocks</th>" +
            "<th>Texts</th><th>Closed</th><th>Counts as wall</th>" +
            "<th>Mostly</th></tr></thead><tbody>" +
            rows.map(rowHtml).join("") +
            "</tbody></table></div></section>";
    }

    function render() {
        var html = state.groups.map(groupHtml).join("");
        host.innerHTML = html ||
            '<p class="muted">No layer matches that filter.</p>';
    }

    function summaryHtml(s, drawing) {
        var bits = [
            ["Layers in the file", num(s.layers)],
            ["Carrying content", num(s.layers_with_content)],
            ["Empty", num(s.empty_layers)],
            ["Entities (modelspace)", num(s.entities)],
            ["Entities (in blocks)", num(s.entities_in_blocks)],
            ["Texts", num(s.texts)],
            ["Closed polylines", num(s.closed_polylines)],
            ["Read as walls", num(s.wall_layers)]
        ];
        return (drawing ? '<div class="sum-item"><b>' + esc(drawing) +
                "</b><span>drawing</span></div>" : "") +
            bits.map(function (b) {
                return '<div class="sum-item"><b>' + b[1] + "</b><span>" +
                    b[0] + "</span></div>";
            }).join("");
    }

    async function load(token) {
        setStatus("Reading the layer table...", "busy");
        try {
            var res = await fetch("/layers/" + encodeURIComponent(token));
            var text = await res.text();
            var data;
            try {
                data = JSON.parse(text);
            } catch (e) {
                setStatus("The server answered HTTP " + res.status +
                          " with a page instead of JSON. If it is running " +
                          "code from before this section existed, restart it.",
                          "error");
                return;
            }
            if (!data.ok) {
                setStatus(data.error || "Could not read the layers.", "error");
                return;
            }
            state.groups = data.groups || [];
            $("layersSummary").innerHTML = summaryHtml(data.summary || {},
                                                       data.drawing);
            if (data.download) {
                $("layersDlXlsx").href = data.download.layers_xlsx || "#";
                $("layersDlJson").href = data.download.layers_json || "#";
            }
            card.classList.remove("hidden");
            render();
            setStatus("");
        } catch (err) {
            setStatus("Network error: " + err.message, "error");
        }
    }

    $("layersFilter").addEventListener("input", function () {
        state.filter = this.value.trim().toLowerCase();
        render();
    });

    $("layersEmptyBtn").addEventListener("click", function () {
        state.hideEmpty = !state.hideEmpty;
        this.textContent = state.hideEmpty ? "Show empty layers"
                                           : "Hide empty layers";
        this.classList.toggle("off", state.hideEmpty);
        render();
    });

    /* main.js drives these, the same way it drives the preview. */
    window.DA_layersReady = function (token) {
        state.token = token;
        load(token);
    };

    window.DA_layersInvalid = function () {
        state.token = null;
        state.groups = [];
        host.innerHTML = "";
        $("layersSummary").innerHTML = "";
        card.classList.add("hidden");
    };
})();
