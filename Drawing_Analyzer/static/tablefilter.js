/* Excel-style filter and sort on every data table.
 *
 * Each column header gets a small button. It opens a panel with:
 *   Sort A to Z / Z to A (numbers sort as numbers: 2,400 before 12,000),
 *   a search box, and a checkbox per distinct value with its count,
 *   (Select all), OK and Clear.
 * Filters on several columns combine, as in Excel. A bar above the table
 * says how many rows are showing and clears everything in one click.
 *
 * Tables are found automatically (table.grid, table.lay-table), including
 * ones drawn later. The steps re-draw their rows (Step 3 after every
 * tick); the filter and sort are re-applied each time, so they stay put.
 * A cell's value is its select's chosen option (the Family column), else
 * its text. Rows whose first cell starts with "Total" stay at the bottom
 * and are never hidden; one-cell "nothing here" rows are left alone.
 *
 * Hidden rows carry the class tf-hidden, and the table fires "tf:change",
 * so a step can act on the rows on screen only (Step 3's tick-all does).
 */
(function () {
    "use strict";

    var SELECTOR = "table.grid, table.lay-table";
    var states = new WeakMap();
    var panel = null, panelFor = null;

    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    function cellValue(td) {
        if (!td) { return ""; }
        var sel = td.querySelector("select");
        if (sel) {
            var o = sel.options[sel.selectedIndex];
            return o ? o.text.trim() : "";
        }
        var inp = td.querySelector("input[type=text], input:not([type])");
        if (inp) { return inp.value.trim(); }
        return td.textContent.replace(/\s+/g, " ").trim();
    }

    function asNumber(v) {
        var t = String(v).replace(/,/g, "").replace(/\s*(sq\.?m|m|mm|%)$/i, "").trim();
        if (t === "" || isNaN(t)) { return null; }
        return parseFloat(t);
    }

    function compare(a, b) {
        var na = asNumber(a), nb = asNumber(b);
        if (na !== null && nb !== null) { return na - nb; }
        if (na !== null) { return -1; }
        if (nb !== null) { return 1; }
        return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
    }

    function isPinned(tr) {
        var first = tr.cells[0];
        return !!first && /^\s*(sub)?total\b/i.test(first.textContent);
    }

    function dataRows(state) {
        var tb = state.table.tBodies[0];
        if (!tb) { return []; }
        return Array.prototype.filter.call(tb.rows, function (tr) {
            return tr.cells.length > 1 && !isPinned(tr);
        });
    }

    function headerCells(table) {
        var head = table.tHead;
        if (!head || !head.rows.length) { return []; }
        return Array.prototype.slice.call(head.rows[head.rows.length - 1].cells);
    }

    // ---- applying ---------------------------------------------------------
    function apply(state) {
        var tb = state.table.tBodies[0];
        if (!tb) { return; }
        var rows = dataRows(state);
        var cols = Object.keys(state.filters);
        var shown = 0;
        rows.forEach(function (tr, i) {
            if (tr.dataset.tfIdx === undefined || tr.dataset.tfGen !== state.gen) {
                tr.dataset.tfIdx = i;
                tr.dataset.tfGen = state.gen;
            }
            var ok = cols.every(function (c) {
                return state.filters[c].has(cellValue(tr.cells[c]));
            });
            tr.classList.toggle("tf-hidden", !ok);
            if (ok) { shown++; }
        });
        if (state.sort) {
            var c = state.sort.col, dir = state.sort.dir;
            var sorted = rows.slice().sort(function (a, b) {
                var r = compare(cellValue(a.cells[c]), cellValue(b.cells[c]));
                return r ? r * dir : (+a.dataset.tfIdx) - (+b.dataset.tfIdx);
            });
            reorder(state, tb, sorted);
        } else if (state.wasSorted) {
            reorder(state, tb, rows.slice().sort(function (a, b) {
                return (+a.dataset.tfIdx) - (+b.dataset.tfIdx);
            }));
            state.wasSorted = false;
        }
        if (state.sort) { state.wasSorted = true; }
        paintButtons(state);
        paintBar(state, shown, rows.length);
        state.table.dispatchEvent(new CustomEvent("tf:change", { bubbles: true }));
    }

    function reorder(state, tb, order) {
        state.busy = true;
        var pinned = Array.prototype.filter.call(tb.rows, isPinned);
        var frag = document.createDocumentFragment();
        order.forEach(function (tr) { frag.appendChild(tr); });
        pinned.forEach(function (tr) { frag.appendChild(tr); });
        tb.appendChild(frag);
        // the observer runs after this task; release the guard after it
        setTimeout(function () { state.busy = false; }, 0);
    }

    function active(state) {
        return !!state.sort || Object.keys(state.filters).length > 0;
    }

    function paintButtons(state) {
        headerCells(state.table).forEach(function (th, c) {
            var b = th.querySelector(".tf-btn");
            if (!b) { return; }
            var f = state.filters[c] !== undefined;
            var s = state.sort && state.sort.col === c;
            b.classList.toggle("on", f || !!s);
            b.innerHTML = s ? (state.sort.dir > 0 ? "&#9650;" : "&#9660;") : (f ? "&#9906;" : "&#9662;");
            b.title = f ? "Filtered - click to change" : (s ? "Sorted - click to change" : "Filter and sort");
        });
    }

    function paintBar(state, shown, total) {
        var bar = state.bar;
        if (!active(state)) {
            bar.classList.add("hidden");
            return;
        }
        var bits = [];
        var heads = headerCells(state.table);
        Object.keys(state.filters).forEach(function (c) {
            bits.push(esc((heads[c] && heads[c].dataset.tfName) || ("column " + (+c + 1))));
        });
        var sortTxt = "";
        if (state.sort) {
            var h = heads[state.sort.col];
            sortTxt = " &middot; sorted by " + esc((h && h.dataset.tfName) || "") +
                (state.sort.dir > 0 ? " (A to Z)" : " (Z to A)");
        }
        bar.innerHTML = "<span>Showing <b>" + shown + "</b> of " + total + " rows" +
            (bits.length ? " &middot; filtered on " + bits.join(", ") : "") + sortTxt +
            '</span><button type="button" class="tf-clear-all">Clear filters &amp; sort</button>';
        bar.classList.remove("hidden");
        bar.querySelector(".tf-clear-all").onclick = function () {
            state.filters = {};
            state.sort = null;
            apply(state);
        };
    }

    // ---- the panel --------------------------------------------------------
    function closePanel() {
        if (panel) { panel.remove(); }
        panel = null;
        panelFor = null;
    }

    function openPanel(state, col, btn) {
        if (panelFor && panelFor.state === state && panelFor.col === col) {
            closePanel();
            return;
        }
        closePanel();
        var rows = dataRows(state);
        // Values offered: those on rows that pass the OTHER columns' filters,
        // as Excel does, so the list never offers what cannot show.
        var others = Object.keys(state.filters).filter(function (c) { return +c !== col; });
        var counts = new Map();
        rows.forEach(function (tr) {
            var ok = others.every(function (c) {
                return state.filters[c].has(cellValue(tr.cells[c]));
            });
            if (!ok) { return; }
            var v = cellValue(tr.cells[col]);
            counts.set(v, (counts.get(v) || 0) + 1);
        });
        var values = Array.from(counts.keys()).sort(compare);
        var current = state.filters[col];
        var picked = new Set(current ? values.filter(function (v) { return current.has(v); }) : values);

        panel = document.createElement("div");
        panel.className = "tf-panel";
        panel.innerHTML =
            '<button type="button" class="tf-sort" data-dir="1">&#9650; Sort A to Z</button>' +
            '<button type="button" class="tf-sort" data-dir="-1">&#9660; Sort Z to A</button>' +
            (state.sort && state.sort.col === col
                ? '<button type="button" class="tf-sort" data-dir="0">Clear sort</button>' : "") +
            '<input type="search" class="tf-search" placeholder="Search values..." />' +
            '<label class="tf-all"><input type="checkbox" /> <b>(Select all)</b></label>' +
            '<div class="tf-list"></div>' +
            '<div class="tf-actions"><button type="button" class="tf-clear">Clear filter</button>' +
            '<button type="button" class="tf-ok">OK</button></div>';
        document.body.appendChild(panel);
        panelFor = { state: state, col: col };

        var list = panel.querySelector(".tf-list");
        var search = panel.querySelector(".tf-search");
        var all = panel.querySelector(".tf-all input");

        function visibleValues() {
            var q = search.value.trim().toLowerCase();
            return values.filter(function (v) { return !q || v.toLowerCase().indexOf(q) >= 0; });
        }
        function draw() {
            var vs = visibleValues();
            list.innerHTML = vs.slice(0, 2000).map(function (v, i) {
                return '<label><input type="checkbox" data-i="' + i + '"' +
                    (picked.has(v) ? " checked" : "") + ' /> <span>' +
                    (v === "" ? "<i>(Blanks)</i>" : esc(v)) + '</span><em>' +
                    counts.get(v) + "</em></label>";
            }).join("") + (vs.length > 2000 ? '<div class="tf-more">First 2,000 values shown - search to narrow.</div>' : "") +
                (vs.length ? "" : '<div class="tf-more">No matching values.</div>');
            list.querySelectorAll("input").forEach(function (cb) {
                cb.onchange = function () {
                    var v = vs[+cb.dataset.i];
                    if (cb.checked) { picked.add(v); } else { picked.delete(v); }
                    syncAll();
                };
            });
            syncAll();
        }
        function syncAll() {
            var vs = visibleValues();
            var n = vs.filter(function (v) { return picked.has(v); }).length;
            all.checked = vs.length > 0 && n === vs.length;
            all.indeterminate = n > 0 && n < vs.length;
        }
        all.onchange = function () {
            visibleValues().forEach(function (v) {
                if (all.checked) { picked.add(v); } else { picked.delete(v); }
            });
            draw();
        };
        search.oninput = function () {
            // Typing narrows the list and, as in Excel, the choice to it.
            var vs = visibleValues();
            picked = new Set(search.value.trim() ? vs : values);
            draw();
        };
        panel.querySelectorAll(".tf-sort").forEach(function (b) {
            b.onclick = function () {
                var d = +b.dataset.dir;
                state.sort = d ? { col: col, dir: d } : null;
                closePanel();
                apply(state);
            };
        });
        panel.querySelector(".tf-clear").onclick = function () {
            delete state.filters[col];
            closePanel();
            apply(state);
        };
        panel.querySelector(".tf-ok").onclick = function () {
            if (picked.size === values.length && !search.value.trim()) {
                delete state.filters[col];
            } else {
                state.filters[col] = new Set(picked);
            }
            closePanel();
            apply(state);
        };
        search.onkeydown = function (e) {
            if (e.key === "Enter") { panel.querySelector(".tf-ok").click(); }
            if (e.key === "Escape") { closePanel(); }
        };
        draw();

        var r = btn.getBoundingClientRect();
        var w = panel.offsetWidth;
        var left = Math.min(window.scrollX + r.left, window.scrollX + document.documentElement.clientWidth - w - 8);
        panel.style.left = Math.max(8, left) + "px";
        panel.style.top = (window.scrollY + r.bottom + 4) + "px";
        search.focus();
    }

    document.addEventListener("mousedown", function (e) {
        if (panel && !panel.contains(e.target) && !e.target.closest(".tf-btn")) { closePanel(); }
    });
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") { closePanel(); }
    });

    // ---- attaching --------------------------------------------------------
    function attach(table) {
        if (states.has(table) || table.dataset.noFilter !== undefined) { return; }
        var heads = headerCells(table);
        if (!heads.length) { return; }
        var state = { table: table, filters: {}, sort: null, gen: "0", busy: false,
                      wasSorted: false };
        states.set(table, state);
        var bar = document.createElement("div");
        bar.className = "tf-bar hidden";
        var host = table.closest(".table-wrap") || table;
        host.parentNode.insertBefore(bar, host);
        state.bar = bar;
        heads.forEach(function (th, c) {
            // No filter on a column without a title (a checkbox or buttons).
            if (!th.textContent.trim()) { return; }
            th.dataset.tfName = th.textContent.trim();
            var b = document.createElement("button");
            b.type = "button";
            b.className = "tf-btn";
            b.innerHTML = "&#9662;";
            b.title = "Filter and sort";
            b.addEventListener("click", function (e) {
                e.stopPropagation();
                openPanel(state, c, b);
            });
            th.appendChild(b);
        });
        var tb = table.tBodies[0];
        if (tb) {
            var timer = null;
            new MutationObserver(function () {
                if (state.busy) { return; }
                clearTimeout(timer);
                timer = setTimeout(function () {
                    state.gen = String(+state.gen + 1);
                    if (active(state)) { apply(state); } else { state.wasSorted = false; }
                }, 0);
            }).observe(tb, { childList: true });
        }
        // A Family select changed in place: the row may no longer pass.
        table.addEventListener("change", function (e) {
            if (e.target.tagName === "SELECT" && active(state)) { apply(state); }
        });
    }

    function scan(root) {
        (root.querySelectorAll ? root : document).querySelectorAll(SELECTOR).forEach(attach);
        if (root.matches && root.matches(SELECTOR)) { attach(root); }
    }

    new MutationObserver(function (muts) {
        muts.forEach(function (m) {
            m.addedNodes.forEach(function (n) {
                if (n.nodeType === 1) { scan(n); }
            });
        });
    }).observe(document.body, { childList: true, subtree: true });
    scan(document);

    /* The rows a filter leaves on screen, for a step that acts on them. */
    window.DA_rowShown = function (tr) {
        return !tr.classList.contains("tf-hidden");
    };
})();
