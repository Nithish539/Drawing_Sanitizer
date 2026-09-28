/* Boundary preview viewer.
 *
 * The server sends one small SVG with a group per family. Everything here
 * is view state: pan and zoom by rewriting the viewBox (so strokes stay
 * crisp at any magnification), a legend that toggles families, and a
 * readout for whatever the pointer is over.
 */
(function () {
    "use strict";

    var $ = function (id) { return document.getElementById(id); };

    var panel = $("previewPanel");
    var canvas = $("previewCanvas");
    var stage = $("previewStage");
    var btn = $("previewBtn");
    if (!panel || !canvas || !btn) { return; }

    var svg = null;
    var view = null;          // current viewBox {x, y, w, h}
    var homeRooms = null;     // framing for the boundaries alone
    var homeAll = null;       // framing including the floor outline
    var loadedToken = null;
    var cache = {};           // mode -> payload, so toggling is instant

    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
                     '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    function num(v, dp) {
        var n = Number(v);
        if (isNaN(n)) { return "—"; }
        return n.toLocaleString(undefined, {
            minimumFractionDigits: dp === undefined ? 2 : dp,
            maximumFractionDigits: dp === undefined ? 2 : dp
        });
    }

    /* ------------------------------------------------------------ view --- */
    function applyView() {
        svg.setAttribute("viewBox",
            view.x + " " + view.y + " " + view.w + " " + view.h);
    }

    function setView(box) {
        view = { x: box.x, y: box.y, w: box.w, h: box.h };
        applyView();
    }

    /* Zoom about a fixed point so the drawing does not slide away from
     * the cursor as it scales. */
    function zoomAt(factor, clientX, clientY) {
        var rect = svg.getBoundingClientRect();
        var fx = (clientX - rect.left) / rect.width;
        var fy = (clientY - rect.top) / rect.height;
        var nw = view.w * factor;
        var nh = view.h * factor;
        // Stop runaway zoom in either direction.
        if (nw < homeRooms.w / 4000 || nw > homeAll.w * 8) { return; }
        view.x += (view.w - nw) * fx;
        view.y += (view.h - nh) * fy;
        view.w = nw;
        view.h = nh;
        applyView();
    }

    function wireGestures() {
        svg.addEventListener("wheel", function (e) {
            e.preventDefault();
            zoomAt(e.deltaY > 0 ? 1.15 : 0.87, e.clientX, e.clientY);
        }, { passive: false });

        var dragging = false, lastX = 0, lastY = 0;

        svg.addEventListener("pointerdown", function (e) {
            dragging = true;
            lastX = e.clientX;
            lastY = e.clientY;
            svg.setPointerCapture(e.pointerId);
            svg.classList.add("pv-dragging");
        });

        svg.addEventListener("pointermove", function (e) {
            if (!dragging) { return; }
            var rect = svg.getBoundingClientRect();
            view.x -= (e.clientX - lastX) * (view.w / rect.width);
            view.y -= (e.clientY - lastY) * (view.h / rect.height);
            lastX = e.clientX;
            lastY = e.clientY;
            applyView();
        });

        ["pointerup", "pointercancel", "pointerleave"].forEach(function (ev) {
            svg.addEventListener(ev, function () {
                dragging = false;
                svg.classList.remove("pv-dragging");
            });
        });

        svg.addEventListener("mouseover", function (e) {
            if (e.target.tagName.toLowerCase() !== "polygon") { return; }
            showReadout(e.target);
        });
        svg.addEventListener("mouseout", function (e) {
            if (e.target.tagName.toLowerCase() !== "polygon") { return; }
            e.target.classList.remove("pv-hot");
        });
    }

    /* ----------------------------------------------------------- busy --- */
    function setBusy(msg) {
        // The button that started this sits in the download row above the
        // tabs, which is where the eye is when it is clicked.
        btn.classList.add("is-busy");
        var box = $("previewLoading");
        box.querySelector(".pv-loading-text").textContent = msg;
        box.classList.remove("hidden");
        stage.classList.add("pv-busy");
        // Whatever the readout is showing belongs to the render being
        // replaced, so it would be describing a shape that is on its way out.
        resetReadout();
    }

    function clearBusy() {
        btn.classList.remove("is-busy");
        $("previewLoading").classList.add("hidden");
        stage.classList.remove("pv-busy");
    }

    function resetReadout() {
        $("previewReadout").innerHTML =
            '<span class="muted small">Hover a boundary</span>';
    }

    function showReadout(poly) {
        poly.classList.add("pv-hot");
        var name = poly.getAttribute("data-name") || "(no label)";
        var fam = poly.getAttribute("data-family") || "";
        var area = poly.getAttribute("data-area");
        $("previewReadout").innerHTML =
            '<b>' + esc(name) + "</b>" +
            '<span class="muted small">' + esc(fam) + "</span>" +
            "<span>" + num(area) + " sq.m</span>";
    }

    /* ---------------------------------------------------------- legend --- */
    function renderLegend(layers, hasBase) {
        if (hasBase) {
            layers = [{ layer: "__base__", family: "Drawing (CAD linework)",
                        color: "#b9c2cf", count: "" }].concat(layers);
        }
        $("previewLegend").innerHTML = layers.map(function (L) {
            return '<button type="button" class="pv-chip" data-layer="' +
                esc(L.layer) + '" title="' + esc(L.layer) +
                ' — click to hide">' +
                '<i style="background:' + esc(L.color) + '"></i>' +
                "<span>" + esc(L.family) + "</span>" +
                "<b>" + L.count + "</b></button>";
        }).join("");

        Array.prototype.forEach.call(
            $("previewLegend").querySelectorAll(".pv-chip"),
            function (chip) {
                chip.addEventListener("click", function () {
                    var layer = chip.getAttribute("data-layer");
                    var off = chip.classList.toggle("off");
                    var g = svg.querySelector('g[data-layer="' +
                        layer.replace(/"/g, '\\"') + '"]');
                    if (g) { g.style.display = off ? "none" : ""; }
                });
            });
    }

    /* A server running older code answers an unknown route with its HTML
     * 404 page, and "Unexpected token '<'" tells nobody anything. */
    async function readJson(res) {
        var text = await res.text();
        try {
            return JSON.parse(text);
        } catch (e) {
            if (res.status === 404) {
                return { ok: false, error: "This server has no /preview route. " +
                    "It is running Python code from before the preview was " +
                    "added - restart it and try again." };
            }
            return { ok: false, error: "The server returned HTTP " + res.status +
                " with an HTML page instead of JSON. Check the server log." };
        }
    }

    /* ------------------------------------------------------------ load --- */
    async function load(token, mode, keepView) {
        mode = mode || "boundaries";
        panel.classList.remove("hidden");
        // Switch to the preview tab now, so the wait is visible rather than
        // happening on a tab the user cannot see.
        if (window.DA_showPane) { window.DA_showPane("previewPanel"); }
        var held = keepView && view ? { x: view.x, y: view.y,
                                        w: view.w, h: view.h } : null;
        try {
            var data = cache[mode];
            if (!data) {
                // A cached mode redraws instantly; only a real wait is
                // worth an overlay.
                setBusy(mode === "both"
                    ? "Reading the drawing… this one takes a few seconds."
                    : "Rendering the boundaries…");
                var res = await fetch("/preview/" + encodeURIComponent(token) +
                                      (mode === "both" ? "?base=1" : ""));
                data = await readJson(res);
                if (data.ok) { cache[mode] = data; }
            }
            if (!data.ok) {
                canvas.innerHTML = '<p class="status error" ' +
                    'style="padding:20px">' +
                    esc(data.error || "Preview failed.") + "</p>";
                svg = null;
                return;
            }
            canvas.innerHTML = data.svg;
            svg = canvas.querySelector("svg");
            homeRooms = {
                x: +svg.dataset.x, y: +svg.dataset.y,
                w: +svg.dataset.w, h: +svg.dataset.h
            };
            homeAll = {
                x: +svg.dataset.ax, y: +svg.dataset.ay,
                w: +svg.dataset.aw, h: +svg.dataset.ah
            };
            // Switching mode should not throw away where the user was
            // looking - both modes share one coordinate system.
            setView(held || homeRooms);
            renderLegend(data.layers || [], data.has_base);
            wireGestures();

            var bits = [data.boundary_count + " boundaries"];
            if (data.void_count) { bits.push(data.void_count + " voids"); }
            if (data.floor_area_sqm) {
                bits.push("floor outline " + num(data.floor_area_sqm) + " sq.m");
            }
            if (data.extent_m) {
                bits.push("extent " + data.extent_m[0] + " × " +
                          data.extent_m[1] + " m");
            }
            $("previewStats").textContent = bits.join("  ·  ");
            $("previewModeNote").textContent = data.base_truncated
                ? "Drawing is very large - only part of the linework is shown."
                : "";
            loadedToken = token;
            if (window.DA_showPane) { window.DA_showPane("previewPanel"); }
        } catch (err) {
            canvas.innerHTML = '<p class="status error" style="padding:20px">' +
                "Network error: " + esc(err.message) + "</p>";
            svg = null;
        } finally {
            clearBusy();
        }
    }

    $("pvZoomIn").addEventListener("click", function () {
        if (!svg) { return; }
        var r = svg.getBoundingClientRect();
        zoomAt(0.8, r.left + r.width / 2, r.top + r.height / 2);
    });
    $("pvZoomOut").addEventListener("click", function () {
        if (!svg) { return; }
        var r = svg.getBoundingClientRect();
        zoomAt(1.25, r.left + r.width / 2, r.top + r.height / 2);
    });
    $("pvFitRooms").addEventListener("click", function () {
        if (homeRooms) { setView(homeRooms); }
    });
    $("pvFitAll").addEventListener("click", function () {
        if (homeAll) { setView(homeAll); }
    });
    $("pvLabels").addEventListener("click", function () {
        var off = canvas.classList.toggle("pv-nolabels");
        this.classList.toggle("off", off);
    });

    btn.addEventListener("click", function () {
        if (btn.disabled) { return; }
        load(btn.dataset.token, $("previewMode").value);
    });

    $("previewMode").addEventListener("change", function () {
        if (!loadedToken) { return; }
        load(loadedToken, this.value, true);
    });

    /* main.js drives these: the preview only exists once boundaries have
     * been generated, and it goes stale the moment selections change. */
    window.DA_previewReady = function (token) {
        btn.disabled = false;
        btn.dataset.token = token;
        btn.title = "Render the boundaries in the browser";
        cache = {};
        if (loadedToken === token) {          // refresh what's open
            load(token, $("previewMode").value);
        }
    };

    window.DA_previewInvalid = function () {
        btn.disabled = true;
        btn.title = "Generate Boundaries first";
        loadedToken = null;
        cache = {};
        panel.classList.add("hidden");
    };
})();
