/* CAD viewer: the drawing on a canvas, with each step's data on top.
 *
 *   var v = DA_cad(hostElement);
 *   v.setScene(scene);             // extent + drawing texts, from /cadview
 *   v.setLines(arrayBuffer);       // coloured linework (binary, see cadview.py)
 *   v.setLoader(fn);               // fn(bbox, px) -> Promise<ArrayBuffer>, for
 *                                  // sharper linework when zoomed in
 *   v.setOverlay({polys, marks});  // the step's own data, hoverable/clickable
 *   v.onHover(fn) / v.onClick(fn)  // fn(item or null)
 *   v.fit(bbox) / v.focus(id)
 *
 * Canvas rather than SVG: a typical floor sheet is 3.5 million vertices,
 * which SVG cannot pan smoothly. Linework is held as one Path2D per colour
 * in world millimetres; the canvas transform does the pan and zoom, so a
 * frame is a handful of stroke() calls.
 */
(function () {
    "use strict";

    var DARK = { bg: "#1b2129", grid: "#232a34" };
    var LIGHT = { bg: "#ffffff", grid: "#f1f4f8" };

    function el(tag, cls, html) {
        var e = document.createElement(tag);
        if (cls) { e.className = cls; }
        if (html != null) { e.innerHTML = html; }
        return e;
    }

    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    function hex(rgb) {
        return "#" + ("000000" + (rgb >>> 0).toString(16)).slice(-6);
    }

    /* On white paper AutoCAD plots colour 7 black; do the same, and darken
     * the other very pale colours enough to be seen. */
    function forLight(rgb) {
        var r = (rgb >> 16) & 255, g = (rgb >> 8) & 255, b = rgb & 255;
        var lum = 0.299 * r + 0.587 * g + 0.114 * b;
        if (lum > 200) { return "#2b2f36"; }
        if (lum > 150) {
            return "rgb(" + Math.round(r * 0.7) + "," + Math.round(g * 0.7) + "," + Math.round(b * 0.7) + ")";
        }
        return hex(rgb);
    }

    function parseLines(buf, ox, oy) {
        var dv = new DataView(buf);
        var off = 0;
        var n = dv.getUint32(off, true); off += 4;
        var out = [];
        for (var c = 0; c < n; c++) {
            var rgb = dv.getUint32(off, true); off += 4;
            var nruns = dv.getUint32(off, true); off += 4;
            var npts = dv.getUint32(off, true); off += 4;
            var lens = new Uint32Array(buf.slice(off, off + nruns * 4)); off += nruns * 4;
            var xy = new Int32Array(buf.slice(off, off + npts * 8)); off += npts * 8;
            var path = new Path2D();
            var k = 0;
            for (var r = 0; r < nruns; r++) {
                var len = lens[r];
                path.moveTo(xy[k] - ox, xy[k + 1] - oy);
                k += 2;
                for (var j = 1; j < len; j++, k += 2) {
                    path.lineTo(xy[k] - ox, xy[k + 1] - oy);
                }
            }
            out.push({ rgb: rgb, path: path });
        }
        return out;
    }

    function inPoly(x, y, pts) {
        var inside = false;
        for (var i = 0, j = pts.length - 1; i < pts.length; j = i++) {
            var xi = pts[i][0], yi = pts[i][1], xj = pts[j][0], yj = pts[j][1];
            if (((yi > y) !== (yj > y)) &&
                (x < (xj - xi) * (y - yi) / ((yj - yi) || 1e-12) + xi)) {
                inside = !inside;
            }
        }
        return inside;
    }

    function bboxOf(pts) {
        var b = [Infinity, Infinity, -Infinity, -Infinity];
        pts.forEach(function (p) {
            if (p[0] < b[0]) { b[0] = p[0]; }
            if (p[1] < b[1]) { b[1] = p[1]; }
            if (p[0] > b[2]) { b[2] = p[0]; }
            if (p[1] > b[3]) { b[3] = p[1]; }
        });
        return b;
    }

    function polyArea(pts) {
        var a = 0;
        for (var i = 0, j = pts.length - 1; i < pts.length; j = i++) {
            a += (pts[j][0] + pts[i][0]) * (pts[j][1] - pts[i][1]);
        }
        return Math.abs(a / 2);
    }

    window.DA_cad = function (host, opts) {
        opts = opts || {};
        host.classList.add("cad");
        host.innerHTML = "";
        var bar = el("div", "cad-bar");
        var stage = el("div", "cad-stage");
        var canvas = el("canvas", "cad-canvas");
        var readout = el("div", "cad-readout hidden");
        var status = el("div", "cad-status");
        stage.appendChild(canvas);
        stage.appendChild(readout);
        stage.appendChild(status);
        host.appendChild(bar);
        host.appendChild(stage);

        function button(label, title, fn) {
            var b = el("button", "cad-btn", label);
            b.type = "button";
            b.title = title;
            b.addEventListener("click", fn);
            bar.appendChild(b);
            return b;
        }
        var theme = DARK;
        var showTexts = true, showLabels = opts.labels !== false;
        button("+", "Zoom in", function () { zoomBy(0.7); });
        button("&minus;", "Zoom out", function () { zoomBy(1.4); });
        button("Fit", "Fit the view", function () { fit(); });
        var bTexts = button("Drawing texts", "Show or hide the drawing's own texts", function () {
            showTexts = !showTexts;
            bTexts.classList.toggle("off", !showTexts);
            draw();
        });
        var bLabels = button("Labels", "Show or hide the names of this step's items", function () {
            showLabels = !showLabels;
            bLabels.classList.toggle("off", !showLabels);
            draw();
        });
        bLabels.classList.toggle("off", !showLabels);
        button("&#9681;", "Dark / light background", function () {
            theme = theme === DARK ? LIGHT : DARK;
            host.classList.toggle("cad-light", theme === LIGHT);
            draw();
        });
        var bBig = button("&#x2922;", "Bigger / smaller viewer", function () {
            host.classList.toggle("cad-big");
            bBig.classList.toggle("on", host.classList.contains("cad-big"));
            resize();
        });
        var legend = el("span", "cad-legend");
        bar.appendChild(legend);

        var ctx = canvas.getContext("2d");
        var dpr = window.devicePixelRatio || 1;
        var W = 0, H = 0;
        var origin = [0, 0];
        var extent = null, home = null;
        var base = [], detail = [], detailBox = null, detailPx = null, basePx = null;
        var texts = [];
        var polys = [], marks = [];
        var view = null;           // {cx, cy, s}: centre (world mm) and px per mm
        var hovered = null, pulse = null;
        var hoverFn = null, clickFn = null, loader = null;
        var viewFn = null;            // told the view after each frame (to follow another viewer)
        var frame = 0;
        // Until the user pans or zooms, the view follows the last fit: a
        // viewer set up inside a hidden tab is fitted to a 0-pixel canvas,
        // and must fit again once the tab is opened.
        var autoFit = true, fitBox = null;

        function resize() {
            var r = stage.getBoundingClientRect();
            W = Math.max(50, r.width);
            H = Math.max(50, r.height);
            canvas.width = Math.round(W * dpr);
            canvas.height = Math.round(H * dpr);
            canvas.style.width = W + "px";
            canvas.style.height = H + "px";
            if ((!view || autoFit) && (fitBox || home)) {
                fit(fitBox || home);
                return;
            }
            draw();
        }
        if (window.ResizeObserver) {
            new ResizeObserver(function () { resize(); }).observe(stage);
        } else {
            window.addEventListener("resize", resize);
        }

        function toScreen(x, y) {
            return [(x - view.cx) * view.s + W / 2, (view.cy - y) * view.s + H / 2];
        }

        function toWorld(sx, sy) {
            return [(sx - W / 2) / view.s + view.cx, view.cy - (sy - H / 2) / view.s];
        }

        function viewBox() {
            var a = toWorld(0, H), b = toWorld(W, 0);
            return [a[0], a[1], b[0], b[1]];
        }

        function fit(box) {
            box = box || home || extent;
            if (!box || !W) { return; }
            autoFit = true;
            fitBox = box;
            var w = Math.max(box[2] - box[0], 1), h = Math.max(box[3] - box[1], 1);
            var s = Math.min(W / (w * 1.06), H / (h * 1.06));
            view = { cx: (box[0] + box[2]) / 2, cy: (box[1] + box[3]) / 2, s: s };
            draw();
            wantDetail();
        }

        function zoomBy(f, sx, sy) {
            if (!view) { return; }
            autoFit = false;
            if (sx == null) { sx = W / 2; sy = H / 2; }
            var p = toWorld(sx, sy);
            var ns = view.s / f;
            if (extent) {
                var ext = Math.max(extent[2] - extent[0], extent[3] - extent[1]);
                if (ns * ext < 50 || ns > 20) { return; }
            }
            view.s = ns;
            view.cx = p[0] - (sx - W / 2) / ns;
            view.cy = p[1] + (sy - H / 2) / ns;
            draw();
            wantDetail();
        }

        // ---- sharper linework when zoomed in ------------------------------
        var detailTimer = null, detailSeq = 0;
        function wantDetail() {
            if (!loader || !view || !basePx) { return; }
            clearTimeout(detailTimer);
            detailTimer = setTimeout(function () {
                var px = 1 / view.s;
                if (px > basePx / 2.5) {            // the base is sharp enough
                    return;
                }
                var vb = viewBox();
                var covered = detailBox && detailPx <= px * 1.6 &&
                    vb[0] >= detailBox[0] && vb[1] >= detailBox[1] &&
                    vb[2] <= detailBox[2] && vb[3] <= detailBox[3];
                if (covered) { return; }
                var mx = (vb[2] - vb[0]) * 0.5, my = (vb[3] - vb[1]) * 0.5;
                var box = [vb[0] - mx, vb[1] - my, vb[2] + mx, vb[3] + my];
                var seq = ++detailSeq;
                loader(box, px).then(function (buf) {
                    if (seq !== detailSeq || !buf) { return; }
                    detail = parseLines(buf, origin[0], origin[1]);
                    detailBox = box;
                    detailPx = px;
                    draw();
                }).catch(function () { /* the base still shows */ });
            }, 250);
        }

        // ---- drawing -------------------------------------------------------
        function draw() {
            if (frame) { return; }
            frame = requestAnimationFrame(function () { frame = 0; paint(); });
        }

        function paint() {
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.fillStyle = theme.bg;
            ctx.fillRect(0, 0, W, H);
            if (!view) { return; }
            var s = view.s;
            // World transform: x right, y up, origin-shifted millimetres.
            ctx.setTransform(dpr * s, 0, 0, -dpr * s,
                dpr * ((origin[0] - view.cx) * s + W / 2),
                dpr * ((view.cy - origin[1]) * s + H / 2));
            ctx.lineWidth = 1 / s;
            ctx.lineJoin = "round";
            // The base covers everything; the sharper detail for the area
            // in view is drawn over it only while zoomed in far enough to
            // need it.
            var layers = base;
            if (detail.length && basePx && 1 / s < basePx / 2) {
                layers = base.concat(detail);
            }
            layers.forEach(function (L) {
                ctx.strokeStyle = theme === DARK ? hex(L.rgb) : forLight(L.rgb);
                ctx.stroke(L.path);
            });
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            if (showTexts) { paintTexts(); }
            paintPolys();
            paintMarks();
            paintDraft();
            if (viewFn) { viewFn({ cx: view.cx, cy: view.cy, s: view.s }); }
        }

        function paintTexts() {
            var vb = viewBox();
            var drawn = 0;
            ctx.textBaseline = "alphabetic";
            for (var i = 0; i < texts.length && drawn < 5000; i++) {
                var t = texts[i];
                var hpx = t[2] * view.s;
                if (hpx < 4 || t[0] < vb[0] - t[2] * 40 || t[0] > vb[2] ||
                    t[1] < vb[1] - t[2] || t[1] > vb[3] + t[2] * 2) {
                    continue;
                }
                var p = toScreen(t[0], t[1]);
                ctx.save();
                ctx.translate(p[0], p[1]);
                if (t[3]) { ctx.rotate(-t[3] * Math.PI / 180); }
                ctx.font = Math.min(hpx, 200).toFixed(1) + "px Arial, sans-serif";
                ctx.fillStyle = theme === DARK ? hex(t[5]) : forLight(t[5]);
                ctx.fillText(t[4], 0, 0);
                ctx.restore();
                drawn++;
            }
        }

        function tracePoly(pts) {
            ctx.beginPath();
            pts.forEach(function (q, i) {
                var p = toScreen(q[0], q[1]);
                if (i) { ctx.lineTo(p[0], p[1]); } else { ctx.moveTo(p[0], p[1]); }
            });
            ctx.closePath();
        }

        function paintPolys() {
            polys.forEach(function (P) {
                if (P.hidden) { return; }
                var hot = hovered === P || (pulse && pulse.item === P);
                tracePoly(P.pts);
                if (P.fill) {
                    ctx.fillStyle = P.fill;
                    ctx.fill("evenodd");
                }
                ctx.setLineDash(P.dash ? [8, 5] : []);
                ctx.lineWidth = (P.width || 2) + (hot ? 2 : 0);
                ctx.strokeStyle = hot ? "#ffd43b" : (P.stroke || "#22b8cf");
                ctx.stroke();
                ctx.setLineDash([]);
            });
            if (!showLabels) { return; }
            polys.forEach(function (P) {
                if (P.hidden || !P.label) { return; }
                var b = P.bbox;
                var wpx = (b[2] - b[0]) * view.s, hpx = (b[3] - b[1]) * view.s;
                if (wpx < 40 || hpx < 14) { return; }
                var p = toScreen((b[0] + b[2]) / 2, (b[1] + b[3]) / 2);
                var size = Math.max(10, Math.min(22, hpx / 6, wpx / Math.max(P.label.length * 0.6, 1)));
                ctx.font = "600 " + size.toFixed(0) + "px Segoe UI, Arial, sans-serif";
                // A name wider than its plate would run into the next one;
                // it falls back to the short tag and the full name is on hover.
                var text = P.label;
                if (ctx.measureText(text).width > wpx * 1.05) {
                    text = P.short || "";
                    if (!text || ctx.measureText(text).width > wpx * 1.05) { return; }
                }
                ctx.textAlign = "center";
                ctx.textBaseline = "middle";
                ctx.lineWidth = 3;
                ctx.strokeStyle = theme === DARK ? "rgba(0,0,0,.75)" : "rgba(255,255,255,.85)";
                ctx.strokeText(text, p[0], p[1]);
                ctx.fillStyle = P.labelColor || (theme === DARK ? "#ffffff" : "#1f3a5f");
                ctx.fillText(text, p[0], p[1]);
                if (P.sub && hpx > 40 && text === P.label) {
                    ctx.font = (size * 0.75).toFixed(0) + "px Segoe UI, Arial, sans-serif";
                    ctx.strokeText(P.sub, p[0], p[1] + size);
                    ctx.fillText(P.sub, p[0], p[1] + size);
                }
                ctx.textAlign = "start";
                ctx.textBaseline = "alphabetic";
            });
        }

        function paintMarks() {
            var vb = viewBox();
            var labelsOn = showLabels && view.s * 1000 > 18;   // ~18 px per metre
            marks.forEach(function (M) {
                if (M.hidden || M.x < vb[0] || M.x > vb[2] || M.y < vb[1] || M.y > vb[3]) { return; }
                var p = toScreen(M.x, M.y);
                var hot = hovered === M || (pulse && pulse.item === M);
                var r = hot ? 7 : 4.5;
                ctx.beginPath();
                ctx.arc(p[0], p[1], r, 0, Math.PI * 2);
                ctx.globalAlpha = M.dim ? 0.35 : 1;
                if (M.hollow) {
                    ctx.lineWidth = 2;
                    ctx.strokeStyle = M.color;
                    ctx.stroke();
                } else {
                    ctx.fillStyle = M.color;
                    ctx.fill();
                    ctx.lineWidth = 1.5;
                    ctx.strokeStyle = theme === DARK ? "#000" : "#fff";
                    ctx.stroke();
                }
                if (hot) {
                    ctx.beginPath();
                    ctx.arc(p[0], p[1], r + 5, 0, Math.PI * 2);
                    ctx.lineWidth = 2;
                    ctx.strokeStyle = "#ffd43b";
                    ctx.stroke();
                }
                if ((labelsOn || hot) && M.label) {
                    ctx.font = (hot ? "700 " : "600 ") + "12px Segoe UI, Arial, sans-serif";
                    ctx.lineWidth = 3;
                    ctx.strokeStyle = theme === DARK ? "rgba(0,0,0,.8)" : "rgba(255,255,255,.9)";
                    ctx.strokeText(M.label, p[0] + 8, p[1] - 6);
                    ctx.fillStyle = theme === DARK ? "#fff" : "#1f3a5f";
                    ctx.fillText(M.label, p[0] + 8, p[1] - 6);
                }
                ctx.globalAlpha = 1;
            });
        }

        // ---- interaction ---------------------------------------------------
        function hitTest(sx, sy) {
            var best = null, bestD = 9;
            marks.forEach(function (M) {
                if (M.hidden) { return; }
                var p = toScreen(M.x, M.y);
                var d = Math.hypot(p[0] - sx, p[1] - sy);
                if (d < bestD) { bestD = d; best = M; }
            });
            if (best) { return best; }
            var w = toWorld(sx, sy);
            var hit = null;
            polys.forEach(function (P) {
                if (P.hidden || !P.hit) { return; }
                var b = P.bbox;
                if (w[0] < b[0] || w[0] > b[2] || w[1] < b[1] || w[1] > b[3]) { return; }
                if (inPoly(w[0], w[1], P.pts) && (!hit || P.area < hit.area)) { hit = P; }
            });
            return hit;
        }

        function showReadout(item) {
            if (!item || !item.info) {
                readout.classList.add("hidden");
                return;
            }
            readout.innerHTML = item.info;
            readout.classList.remove("hidden");
        }

        // Editing modes (the Geometry tab): "select" clicks items, "trace"
        // and "draw" click points on the drawing, "move" drags text marks.
        // Panning by dragging empty drawing works in every mode.
        var mode = "select";
        var draft = null;               // {pts: [[x,y]...], cursor: [x,y]}
        var dragMark = null;
        var moveFn = null, dblFn = null, dragEndFn = null;

        function worldAt(e) {
            var r = canvas.getBoundingClientRect();
            return toWorld(e.clientX - r.left, e.clientY - r.top);
        }

        var drag = null, moved = false;
        canvas.addEventListener("wheel", function (e) {
            e.preventDefault();
            var r = canvas.getBoundingClientRect();
            zoomBy(e.deltaY > 0 ? 1.18 : 0.85, e.clientX - r.left, e.clientY - r.top);
        }, { passive: false });
        canvas.addEventListener("pointerdown", function (e) {
            if (mode === "move" && view) {
                var r0 = canvas.getBoundingClientRect();
                var it = hitTest(e.clientX - r0.left, e.clientY - r0.top);
                if (it && it.kind === "mark" && it.draggable) {
                    dragMark = { M: it, x: e.clientX, y: e.clientY, ox: it.x, oy: it.y };
                    canvas.setPointerCapture(e.pointerId);
                    canvas.style.cursor = "grabbing";
                    return;
                }
            }
            drag = { x: e.clientX, y: e.clientY };
            moved = false;
            canvas.setPointerCapture(e.pointerId);
        });
        canvas.addEventListener("pointermove", function (e) {
            var r = canvas.getBoundingClientRect();
            if (dragMark && view) {
                dragMark.M.x = dragMark.ox + (e.clientX - dragMark.x) / view.s;
                dragMark.M.y = dragMark.oy - (e.clientY - dragMark.y) / view.s;
                draw();
                return;
            }
            if (moveFn && view && !drag) { moveFn(worldAt(e)); }
            if (drag && view) {
                var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
                if (Math.abs(dx) + Math.abs(dy) > 2) { moved = true; autoFit = false; }
                view.cx -= dx / view.s;
                view.cy += dy / view.s;
                drag = { x: e.clientX, y: e.clientY };
                canvas.classList.add("dragging");
                draw();
                return;
            }
            if (!view) { return; }
            var item = hitTest(e.clientX - r.left, e.clientY - r.top);
            if (item !== hovered) {
                hovered = item;
                canvas.style.cursor = (mode === "draw" || mode === "trace") ? "crosshair" :
                    (mode === "move" ? (item && item.draggable ? "grab" : "") :
                     (item && item.click !== false ? "pointer" : ""));
                showReadout(item);
                if (hoverFn) { hoverFn(item); }
                draw();
            }
        });
        function endDrag(e) {
            if (dragMark) {
                var dm = dragMark;
                dragMark = null;
                canvas.style.cursor = "";
                if (dragEndFn) { dragEndFn(dm.M, dm.M.x - dm.ox, dm.M.y - dm.oy); }
                draw();
                return;
            }
            if (!drag) { return; }
            var wasMoved = moved;
            drag = null;
            canvas.classList.remove("dragging");
            if (wasMoved) {
                wantDetail();
            } else if (e && e.type === "pointerup" && clickFn && view) {
                var r = canvas.getBoundingClientRect();
                clickFn(mode === "draw" ? null : hitTest(e.clientX - r.left, e.clientY - r.top),
                        worldAt(e), e);
            }
        }
        canvas.addEventListener("pointerup", endDrag);
        canvas.addEventListener("pointercancel", endDrag);
        canvas.addEventListener("pointerleave", function () {
            if (hovered) {
                hovered = null;
                showReadout(null);
                if (hoverFn) { hoverFn(null); }
                draw();
            }
        });
        canvas.addEventListener("dblclick", function (e) {
            if (dblFn && view && dblFn(worldAt(e), e)) { return; }
            fit();
        });

        function paintDraft() {
            if (!draft || !view) { return; }
            var pts = draft.pts.slice();
            if (draft.cursor) { pts.push(draft.cursor); }
            if (!pts.length) { return; }
            ctx.beginPath();
            pts.forEach(function (q, i) {
                var p = toScreen(q[0], q[1]);
                if (i) { ctx.lineTo(p[0], p[1]); } else { ctx.moveTo(p[0], p[1]); }
            });
            if (draft.pts.length >= 2) {
                var p0 = toScreen(draft.pts[0][0], draft.pts[0][1]);
                ctx.lineTo(p0[0], p0[1]);
            }
            ctx.setLineDash([6, 4]);
            ctx.lineWidth = 2;
            ctx.strokeStyle = "#ffd43b";
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillStyle = "rgba(255,212,59,0.15)";
            if (draft.pts.length >= 2) { ctx.fill(); }
            draft.pts.forEach(function (q, i) {
                var p = toScreen(q[0], q[1]);
                ctx.beginPath();
                ctx.arc(p[0], p[1], i === 0 ? 6 : 4, 0, Math.PI * 2);
                ctx.fillStyle = i === 0 ? "#ffd43b" : "#fff";
                ctx.fill();
            });
        }

        function prepare(list, kind) {
            return (list || []).map(function (it) {
                it.kind = kind;
                if (kind === "poly") {
                    it.bbox = bboxOf(it.pts);
                    it.area = polyArea(it.pts);
                    if (it.hit === undefined) { it.hit = true; }
                }
                return it;
            });
        }

        resize();

        return {
            setStatus: function (msg, kind) {
                status.textContent = msg || "";
                status.className = "cad-status" + (msg ? " on " + (kind || "") : "");
            },
            setLegend: function (html) { legend.innerHTML = html || ""; },
            setScene: function (scene) {
                extent = scene.extent;
                origin = [Math.round(extent[0]), Math.round(extent[1])];
                texts = scene.texts || [];
                if (!home) { home = extent; }
                if (!view) { fit(home); }
                draw();
            },
            setLines: function (buf, px) {
                base = parseLines(buf, origin[0], origin[1]);
                basePx = px || null;
                detail = [];
                detailBox = null;
                draw();
                wantDetail();
            },
            setLoader: function (fn) { loader = fn; },
            setOverlay: function (o) {
                polys = prepare(o.polys, "poly");
                marks = prepare(o.marks, "mark");
                hovered = null;
                showReadout(null);
                draw();
            },
            refresh: function () { draw(); },
            setHome: function (box) { home = box; },
            fit: function (box) { fit(box); },
            focus: function (id, zoom) {
                var item = null;
                polys.concat(marks).forEach(function (it) { if (it.id === id) { item = it; } });
                if (!item) { return; }
                if (item.kind === "poly") {
                    fit(item.bbox);
                } else if (view) {
                    autoFit = false;
                    if (zoom && view.s * 1000 < 40) { view.s = 40 / 1000; }
                    view.cx = item.x;
                    view.cy = item.y;
                    draw();
                    wantDetail();
                }
                pulse = { item: item };
                showReadout(item);
                draw();
                var mine = pulse;
                setTimeout(function () {
                    if (pulse === mine) { pulse = null; draw(); }
                }, 1600);
            },
            setHovered: function (id) {
                var item = null;
                if (id != null) {
                    polys.concat(marks).forEach(function (it) { if (it.id === id) { item = it; } });
                }
                hovered = item;
                draw();
            },
            onHover: function (fn) { hoverFn = fn; },
            onClick: function (fn) { clickFn = fn; },
            onMove: function (fn) { moveFn = fn; },
            onDblClick: function (fn) { dblFn = fn; },
            onDragEnd: function (fn) { dragEndFn = fn; },
            setMode: function (m) {
                mode = m || "select";
                canvas.style.cursor = (mode === "draw" || mode === "trace") ? "crosshair" : "";
            },
            setDraft: function (d) { draft = d; draw(); },
            screenScale: function () { return view ? view.s : 1; },
            onView: function (fn) { viewFn = fn; },
            setView: function (v) {
                if (!v || !W) { return; }
                autoFit = false;
                view = { cx: v.cx, cy: v.cy, s: v.s };
                draw();
                wantDetail();
            },
            clear: function () {
                base = []; detail = []; texts = []; polys = []; marks = [];
                extent = null; home = null; view = null;
                autoFit = true; fitBox = null;
                showReadout(null);
                legend.innerHTML = "";
                draw();
            },
            esc: esc
        };
    };

    /* Fetch helpers shared by the steps. */
    window.DA_cadLoad = async function (viewer, scenePath, linesPath, opts) {
        opts = opts || {};
        viewer.setStatus("Reading the drawing for the viewer...", "busy");
        var started = Date.now();
        var scene = null;
        for (;;) {
            var res = await fetch(scenePath);
            var data;
            try { data = await res.json(); } catch (e) { data = { ok: false, error: "The server answered HTTP " + res.status + "." }; }
            if (!data.ok) {
                viewer.setStatus(data.error || "Could not load the drawing.", "error");
                return null;
            }
            if (data.status === "ready") { scene = data.scene; break; }
            if (opts.isStale && opts.isStale()) { return null; }
            var secs = Math.round((Date.now() - started) / 1000);
            viewer.setStatus("Reading the drawing for the viewer... " + secs + " s", "busy");
            await new Promise(function (r) { setTimeout(r, 1200); });
        }
        if (opts.isStale && opts.isStale()) { return null; }
        viewer.setScene(scene);
        var box = opts.box || scene.extent;
        var px = Math.max(box[2] - box[0], box[3] - box[1]) / 1600;
        function url(b, p) {
            return linesPath + (linesPath.indexOf("?") < 0 ? "?" : "&") +
                "bbox=" + b.map(function (v) { return Math.round(v); }).join(",") +
                "&px=" + p.toFixed(2);
        }
        var pad = Math.max(box[2] - box[0], box[3] - box[1]) * 0.25;
        var clip = opts.box ? [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad] : scene.extent;
        var lr = await fetch(url(clip, px));
        if (!lr.ok) {
            viewer.setStatus("Could not load the drawing's linework.", "error");
            return scene;
        }
        if (opts.isStale && opts.isStale()) { return null; }
        viewer.setLines(await lr.arrayBuffer(), px);
        viewer.setLoader(function (b, p) {
            return fetch(url(b, p)).then(function (r) { return r.ok ? r.arrayBuffer() : null; });
        });
        viewer.setStatus("");
        return scene;
    };
})();
