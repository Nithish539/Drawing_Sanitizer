/* Page layout: one step at a time instead of one long scroll.
 *
 * Every `section.card` becomes a step in the top nav, and each group of
 * `[data-pane]` blocks becomes a row of sub-tabs inside its step.
 *
 * Readiness is not tracked separately here. The rest of the app already
 * says what is ready by adding and removing the `hidden` class on those
 * elements, so this watches that class instead of inventing a second
 * source of truth that could disagree with it.
 */
(function () {
    "use strict";

    var wrap = document.querySelector("main.wrap");
    if (!wrap) { return; }

    var sections = Array.prototype.slice.call(
        wrap.querySelectorAll("section.card"));
    if (sections.length < 2) { return; }

    function isReady(el) { return !el.classList.contains("hidden"); }

    /* ------------------------------------------------------- step nav --- */
    var nav = document.createElement("nav");
    nav.className = "stepnav";
    var buttons = [];

    // Numbered steps read 1-5 across the bar and the settings sections sit
    // together after a divider, whatever order they happen to be in the
    // document.
    var order = sections.map(function (s, i) { return i; }).sort(function (a, b) {
        var na = stepNumber(sections[a]), nb = stepNumber(sections[b]);
        if (na === nb) { return a - b; }
        return na - nb;
    });

    function stepNumber(section) {
        var mark = section.querySelector(".card-head .step-no");
        var n = parseInt(mark ? mark.textContent : "", 10);
        return isNaN(n) ? 999 : n;
    }

    var dividerAdded = false;

    order.forEach(function (i) {
        var section = sections[i];
        var head = section.querySelector(".card-head");
        var mark = head ? head.querySelector(".step-no") : null;
        var title = head ? head.querySelector("h2") : null;

        if (!dividerAdded && stepNumber(section) === 999) {
            var sep = document.createElement("span");
            sep.className = "stepnav-sep";
            nav.appendChild(sep);
            dividerAdded = true;
        }

        var b = document.createElement("button");
        b.type = "button";
        b.className = "stepnav-item";
        b.innerHTML = '<span class="stepnav-no">' +
            (mark ? mark.innerHTML : i + 1) + "</span><span>" +
            (title ? title.textContent : "Step " + (i + 1)) + "</span>";
        b.addEventListener("click", function () {
            if (b.disabled) { return; }
            show(i);
        });
        nav.appendChild(b);
        // Keyed by section index, not by position in the bar, because the
        // bar is reordered above.
        buttons[i] = b;

        section.classList.add("pane");
        // The card head is now duplicated by the nav item above it.
        if (head) { head.classList.add("pane-head"); }
    });

    wrap.insertBefore(nav, wrap.firstChild);

    var current = -1;

    function show(i) {
        current = i;
        sections.forEach(function (s, j) {
            s.classList.toggle("pane-on", i === j);
            buttons[j].classList.toggle("active", i === j);
        });
        if (window.scrollY > nav.offsetTop) {
            nav.scrollIntoView({ behavior: "smooth", block: "start" });
        }
    }

    function refresh(changedIndex) {
        sections.forEach(function (s, j) {
            var ready = isReady(s);
            buttons[j].disabled = !ready;
            buttons[j].title = ready ? "" : "Not available yet";
        });
        // A step that just became available is where the user wants to be:
        // it is the thing the click they just made produced. The exception
        // is a step marked data-quiet, which fills itself in as a side
        // effect of some other step: the layer inventory is read during the
        // upload, and stealing the view for it would drop the user in
        // Step 6 when they were on their way to Step 2.
        if (changedIndex !== undefined && isReady(sections[changedIndex])
                && !sections[changedIndex].hasAttribute("data-quiet")) {
            show(changedIndex);
            return;
        }
        if (current < 0 || !isReady(sections[current])) {
            for (var k = 0; k < sections.length; k++) {
                if (isReady(sections[k])) { show(k); return; }
            }
        }
    }

    // Selecting a step also rewrites `class`, so reacting to every class
    // change would mean this observer answering its own edits - the two
    // steps trading `pane-on` back and forth until the tab locks up.
    // Only a change in readiness is a real event.
    var wasReady = sections.map(isReady);
    sections.forEach(function (section, i) {
        new MutationObserver(function () {
            var ready = isReady(section);
            if (ready === wasReady[i]) { return; }
            wasReady[i] = ready;
            refresh(i);
        }).observe(section, { attributes: true, attributeFilter: ["class"] });
    });
    refresh();

    /* ------------------------------------------------------- sub-tabs --- */
    var groups = {};

    Array.prototype.forEach.call(
        wrap.querySelectorAll("[data-subpanes]"),
        function (host) {
            var key = host.getAttribute("data-subpanes");
            var bar = wrap.querySelector('[data-subtabs="' + key + '"]');
            if (!bar) { return; }

            var panes = Array.prototype.filter.call(
                host.children,
                function (el) { return el.hasAttribute("data-pane"); });
            if (!panes.length) { return; }

            var tabs = panes.map(function (pane) {
                var t = document.createElement("button");
                t.type = "button";
                t.className = "subtab";
                t.textContent = pane.getAttribute("data-pane");
                t.addEventListener("click", function () {
                    if (!t.disabled) { pick(key, panes.indexOf(pane)); }
                });
                bar.appendChild(t);
                pane.classList.add("subpane");
                return t;
            });

            groups[key] = { panes: panes, tabs: tabs, active: -1,
                            ready: panes.map(isReady) };

            panes.forEach(function (pane, i) {
                new MutationObserver(function () {
                    var g = groups[key];
                    var ready = isReady(pane);
                    if (ready === g.ready[i]) { return; }   // our own toggle
                    g.ready[i] = ready;
                    sync(key);
                }).observe(pane, { attributes: true,
                                   attributeFilter: ["class"] });
            });
            sync(key);
        });

    function pick(key, i) {
        var g = groups[key];
        g.active = i;
        g.panes.forEach(function (p, j) {
            p.classList.toggle("subpane-on", i === j);
            g.tabs[j].classList.toggle("active", i === j);
        });
    }

    /* Keep the tab row honest about which panes hold anything, and never
     * leave a hidden pane selected. */
    function sync(key) {
        var g = groups[key];
        var firstReady = -1;
        g.panes.forEach(function (p, j) {
            var ready = isReady(p);
            g.tabs[j].disabled = !ready;
            g.tabs[j].title = ready ? "" : "Nothing here yet";
            if (ready && firstReady < 0) { firstReady = j; }
        });
        if (g.active < 0 || !isReady(g.panes[g.active])) {
            if (firstReady >= 0) { pick(key, firstReady); }
        }
    }

    /* preview.js calls this so "View boundaries" lands on the drawing
     * rather than quietly filling in a tab the user is not looking at. */
    window.DA_showPane = function (paneId) {
        Object.keys(groups).forEach(function (key) {
            var g = groups[key];
            g.panes.forEach(function (p, j) {
                if (p.id === paneId && isReady(p)) { pick(key, j); }
            });
        });
    };
})();
