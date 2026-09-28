/* A pan-and-zoom SVG viewer, as a factory.
 *
 * preview.js grew its own copy of this for the Step 3 drawing, tied to
 * that panel's elements. The rule preview needs the same behaviour in a
 * second place, so the gestures live here instead of being written twice.
 * Each call gets its own state - two viewers on one page do not interfere.
 *
 * Zoom rewrites the viewBox rather than scaling a bitmap, which is what
 * keeps the strokes one pixel wide at any magnification.
 */
(function () {
    "use strict";

    window.DA_viewer = function (canvas) {
        var svg = null;
        var view = null;
        var home = null;

        function applyView() {
            if (!svg || !view) { return; }
            svg.setAttribute("viewBox",
                view.x + " " + view.y + " " + view.w + " " + view.h);
        }

        function setView(box) {
            view = { x: box.x, y: box.y, w: box.w, h: box.h };
            applyView();
        }

        function zoomAt(factor, clientX, clientY) {
            if (!svg || !home) { return; }
            var rect = svg.getBoundingClientRect();
            var fx = (clientX - rect.left) / rect.width;
            var fy = (clientY - rect.top) / rect.height;
            var nw = view.w * factor;
            var nh = view.h * factor;
            if (nw < home.w / 2000 || nw > home.w * 40) { return; }
            view.x += (view.w - nw) * fx;
            view.y += (view.h - nh) * fy;
            view.w = nw;
            view.h = nh;
            applyView();
        }

        function wire() {
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
        }

        return {
            /* Put an SVG in and take control of it. Returns false when the
             * markup carried no <svg>, so the caller can say so rather
             * than leaving an empty frame. */
            load: function (markup) {
                canvas.innerHTML = markup || "";
                svg = canvas.querySelector("svg");
                if (!svg) { return false; }
                home = {
                    x: +svg.dataset.x, y: +svg.dataset.y,
                    w: +svg.dataset.w, h: +svg.dataset.h
                };
                if (!home.w || !home.h) {
                    var vb = (svg.getAttribute("viewBox") || "").split(/\s+/);
                    home = { x: +vb[0] || 0, y: +vb[1] || 0,
                             w: +vb[2] || 100, h: +vb[3] || 100 };
                }
                setView(home);
                wire();
                return true;
            },
            fit: function () { if (home) { setView(home); } },
            zoomIn: function () {
                if (!svg) { return; }
                var r = svg.getBoundingClientRect();
                zoomAt(0.8, r.left + r.width / 2, r.top + r.height / 2);
            },
            zoomOut: function () {
                if (!svg) { return; }
                var r = svg.getBoundingClientRect();
                zoomAt(1.25, r.left + r.width / 2, r.top + r.height / 2);
            },
            clear: function () {
                canvas.innerHTML = "";
                svg = null;
                view = null;
                home = null;
            },
            svg: function () { return svg; }
        };
    };
})();
