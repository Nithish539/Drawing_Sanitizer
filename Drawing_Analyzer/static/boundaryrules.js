/* The rules the drawing pass applies, as a checklist beside Generate.
 *
 * The catalogue comes from the server rather than being written out here,
 * so the list cannot quietly drift from the code that acts on it. What is
 * kept here is only the user's answer: which rules are off.
 *
 * main.js reads window.DA_ruleChoices() when it posts a run.
 */
(function () {
    "use strict";

    var $ = function (id) { return document.getElementById(id); };

    var panel = $("rulesPanel");
    var body = $("rulesPanelBody");
    // The panel used to live behind a button in Step 3. It is open in the
    // rules section now, so the toggle is optional.
    var toggle = $("rulesPanelBtn");
    if (!panel || !body) { return; }

    var LS_KEY = "da_boundary_rules_v1";
    var groups = [];
    var defaults = {};
    var choice = {};

    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
                     '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    /* The ticks live only as long as the page: a refresh or a new tab
     * starts from the defaults. They used to be kept in localStorage, which
     * every tab shares, so a rule switched off once stayed off everywhere;
     * that saved copy is removed here. */
    try { localStorage.removeItem(LS_KEY); } catch (e) { /* none */ }

    function loadSaved() {
        return {};
    }

    function save() { /* nothing is kept beyond this page */ }

    function offCount() {
        return Object.keys(choice).filter(function (k) {
            return !choice[k];
        }).length;
    }

    function total() {
        return Object.keys(defaults).length;
    }

    function paintCount() {
        var off = offCount();
        var label = off
            ? (total() - off) + " of " + total() + " rules on"
            : "All " + total() + " rules on";
        $("rulesPanelCount").textContent = label +
            (off ? " - " + off + " will not be applied" : "");
        if (toggle) {
            toggle.innerHTML = "⚙ Rules" +
                (off ? ' <b class="rule-off-badge">' + off + " off</b>" : "");
        }
    }

    function render() {
        body.innerHTML = groups.map(function (g) {
            return '<section class="rule-cat"><h3>' + esc(g.title) +
                "</h3>" + g.rules.map(function (r) {
                    var on = choice[r.key];
                    return '<label class="rule-check' +
                        (on ? "" : " is-off") + '" title="' +
                        esc(r.detail) + '">' +
                        '<input type="checkbox" data-rule="' + esc(r.key) +
                        '"' + (on ? " checked" : "") + " />" +
                        "<span><b>" + esc(r.title) + "</b>" +
                        '<i class="muted small">' + esc(r.detail) +
                        "</i></span></label>";
                }).join("") + "</section>";
        }).join("");
        paintCount();
    }

    body.addEventListener("change", function (ev) {
        var box = ev.target;
        if (!box || box.type !== "checkbox") { return; }
        choice[box.getAttribute("data-rule")] = box.checked;
        box.closest(".rule-check").classList.toggle("is-off", !box.checked);
        save();
        paintCount();
    });

    function setAll(value) {
        Object.keys(choice).forEach(function (k) { choice[k] = value; });
        save();
        render();
    }

    $("rulesAllOn").addEventListener("click", function () { setAll(true); });
    $("rulesAllOff").addEventListener("click", function () { setAll(false); });
    $("rulesReset").addEventListener("click", function () {
        choice = Object.assign({}, defaults);
        save();
        render();
    });

    if (toggle) {
        toggle.addEventListener("click", function () {
            panel.classList.toggle("hidden");
        });
    }

    (async function init() {
        try {
            var res = await fetch("/boundary_rules");
            var data = await res.json();
            if (!data.ok) { return; }
            groups = data.groups || [];
            defaults = data.defaults || {};
            var saved = loadSaved();
            choice = {};
            Object.keys(defaults).forEach(function (k) {
                choice[k] = (k in saved) ? !!saved[k] : defaults[k];
            });
            render();
        } catch (err) {
            body.innerHTML = '<p class="status error">Could not read the ' +
                "rule list: " + esc(err.message) + "</p>";
        }
    })();

    /* Only the rules that are off need sending - the server fills the rest
     * in from its own defaults, so a rule added later arrives switched on
     * rather than silently missing. */
    window.DA_ruleChoices = function () {
        var out = {};
        Object.keys(choice).forEach(function (k) {
            if (!choice[k]) { out[k] = false; }
        });
        return out;
    };

    window.DA_rulesOffCount = offCount;
})();
