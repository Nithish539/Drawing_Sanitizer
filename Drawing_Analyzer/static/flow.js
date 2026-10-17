/* One workflow, two ways to run it, and Step 6 Final outputs.
 *
 *   Step by step  Proceed opens Step 3; the user moves through 3, 4 and 5.
 *                 "Generate final outputs" on Step 5 writes the final
 *                 drawing and opens Step 6, which builds its areas and area
 *                 statement.
 *   Fast track    Proceed opens Step 6 at once. Steps 3-5 run here, one
 *                 after the other, with the system defaults and no
 *                 questions; Step 6 shows the progress and unlocks each
 *                 download as it is ready. Steps 3-5 fill in as they go and
 *                 can be opened to review.
 *
 * Nothing here talks to the server itself. Each stage is the same function
 * the step's own button runs (main.js, geometry.js, geoai.js expose them),
 * and every stage that finishes - by its button or from here - announces it
 * with a "da:stage" event. Step 6's progress and download cards are kept
 * from those events, so they are right whichever way the work was done.
 *
 * Stop / Restart on Step 6 (stepctl.js) end this run and carry it on from
 * the stage it stopped at. A change made in Steps 3-4 after the outputs
 * were built marks them out of date, with a Rebuild button.
 */
(function () {
    "use strict";

    function $(id) { return document.getElementById(id); }
    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }
    function api() { return window.DA_flowApi || {}; }

    /* ------------------------------------------------------------ run mode */
    var MODE_KEY = "da-run-mode";
    function radios() { return document.querySelectorAll('input[name="runMode"]'); }
    function mode() {
        var m = "step";
        Array.prototype.forEach.call(radios(), function (r) { if (r.checked) { m = r.value; } });
        return m;
    }
    function paintMode() {
        var fast = mode() === "fast";
        var note = $("proceedNote");
        if (note) { note.innerHTML = fast ? "&rarr; 6 Final outputs" : "&rarr; 3 Texts &amp; boundaries"; }
        Array.prototype.forEach.call(document.querySelectorAll(".run-mode-opt"), function (opt) {
            var r = opt.querySelector("input");
            opt.classList.toggle("on", !!(r && r.checked));
        });
    }
    function saveMode() {
        var keep = $("runModeRemember");
        try {
            if (keep && keep.checked) { localStorage.setItem(MODE_KEY, mode()); }
            else { localStorage.removeItem(MODE_KEY); }
        } catch (e) { /* storage blocked: the choice lasts this page */ }
    }
    (function loadMode() {
        var saved = null;
        try { saved = localStorage.getItem(MODE_KEY); } catch (e) { /* none */ }
        if (saved) {
            Array.prototype.forEach.call(radios(), function (r) { r.checked = r.value === saved; });
        }
        Array.prototype.forEach.call(radios(), function (r) {
            r.addEventListener("change", function () { paintMode(); saveMode(); });
        });
        var keep = $("runModeRemember");
        if (keep) { keep.addEventListener("change", saveMode); }
        paintMode();
    })();
    window.DA_runMode = mode;

    /* -------------------------------------------------------------- stages */
    // [key, label, the step that does the same work]
    var STAGES = [
        ["extract", "Extract texts", 2],
        ["annotate", "Generate boundaries", 3],
        ["geometry", "Geometry (AI)", 4],
        ["compare", "Compare (defaults)", 5],
        ["final", "Final drawing", 5],
        ["areas", "Calculate areas", 6],
        ["statement", "Area statement", 6]
    ];
    var ORDER = STAGES.map(function (s) { return s[0]; });
    var STEP_OF = {};
    STAGES.forEach(function (s) { STEP_OF[s[0]] = s[2]; });

    // waiting | running | done | failed | stopped
    var stage = {};
    var extra = { geoWritten: false, source: "", touched: false };
    var stale = false;
    function resetFrom(key) {
        var i = ORDER.indexOf(key);
        ORDER.forEach(function (k, j) { if (j >= i) { stage[k] = "waiting"; } });
        if (i <= ORDER.indexOf("geometry")) { extra.geoWritten = false; }
    }
    resetFrom("extract");

    /* ---------------------------------------------------------------- run */
    var flow = { busy: false, kind: "", seq: null, at: -1, stopped: false, note: "", failed: "" };

    window.DA_flowBusy = function () { return flow.busy; };
    // While Fast track runs, Steps 3-5 appearing must not pull the user
    // away from Step 6 (layout.js asks).
    window.DA_navHold = function () { return flow.busy && flow.kind === "fast"; };
    // The last error a stage reported, for this step's own status line.
    window.DA_flowNote = function (msg, stopped) {
        if (!flow.busy) { return; }
        flow.note = msg || "";
        if (stopped) { flow.stopped = true; }
    };
    window.DA_flowStop = function () { if (flow.busy) { flow.stopped = true; } };

    var RUNNERS = {
        extract: function () { return api().extract(); },
        annotate: function () { return api().generate(); },
        geometry: function () { return window.DA_geoFlow ? window.DA_geoFlow() : false; },
        compare: function () { return window.DA_compareFlow ? window.DA_compareFlow() : false; },
        final: function () { return window.DA_finalFlow ? window.DA_finalFlow() : false; },
        areas: function () { return api().areasFrom("final"); }
    };

    async function run(kind, seq, from) {
        if (flow.busy) { return; }
        flow.busy = true;
        flow.kind = kind;
        flow.seq = seq;
        flow.stopped = false;
        flow.failed = "";
        extra.touched = true;
        paintStep6Bar();
        for (var i = from || 0; i < seq.length; i++) {
            var key = seq[i];
            flow.at = i;
            flow.note = "";
            stage[key] = "running";
            if (key === "areas") { stage.statement = "running"; }
            render();
            var ok = false;
            try { ok = await RUNNERS[key](); } catch (e) { flow.note = e.message; ok = false; }
            if (flow.stopped || !ok) {
                stage[key] = flow.stopped ? "stopped" : "failed";
                if (key === "areas") { stage.statement = "waiting"; }
                flow.failed = key;
                break;
            }
            if (stage[key] === "running") { stage[key] = "done"; }   // no event came
        }
        if (!flow.failed) { flow.seq = null; flow.at = -1; }
        flow.busy = false;
        paintStep6Bar();
        render();
    }

    /* Step 2's Proceed in Fast track. */
    window.DA_fastTrack = function () {
        if (flow.busy) { return; }
        var problem = api().checkPicks ? api().checkPicks(true) : "";
        if (problem) { if (api().showPickProblem) { api().showPickProblem(problem); } return; }
        saveMode();
        stale = false;
        resetFrom("extract");
        if (window.DA_showStep) { window.DA_showStep(6); }
        window.scrollTo({ top: 0, behavior: "smooth" });
        run("fast", ["extract", "annotate", "geometry", "compare", "final", "areas"], 0);
    };

    /* Step 5's "Generate final outputs" wrote the final drawing: Step 6
     * builds the rest from it. */
    window.DA_afterFinal = function () {
        if (flow.busy) { return; }
        stale = false;
        if (window.DA_showStep) { window.DA_showStep(6); }
        window.scrollTo({ top: 0, behavior: "smooth" });
        run("final", ["areas"], 0);
    };

    /* Restart on Step 6: carry a stopped or failed run on from its stage. */
    window.DA_flowResume = function () {
        if (flow.busy || !flow.seq || flow.at < 0) { return false; }
        run(flow.kind, flow.seq, flow.at);
        return true;
    };

    $("foRebuildBtn").addEventListener("click", function () {
        if (flow.busy) { return; }
        stale = false;
        run("rebuild", ["compare", "final", "areas"], 0);
    });

    function paintStep6Bar() { if (window.DA_stepPaint) { window.DA_stepPaint(6); } }

    /* ---------------------------------------------- what the steps report */
    document.addEventListener("da:stage", function (e) {
        var d = e.detail || {};
        var built = stage.final === "done" || stage.areas === "done";
        switch (d.name) {
        case "reset":
            resetFrom("extract");
            stale = false;
            extra.touched = false;
            flow.seq = null; flow.at = -1; flow.failed = "";
            break;
        case "extract":
            resetFrom("extract");
            stale = false;
            stage.extract = "done";
            break;
        case "annotate":
            stage.annotate = "done";
            if (built && !flow.busy) { stale = true; }
            break;
        case "geometry":
            stage.geometry = "done";
            break;
        case "geo-generate":
            stage.geometry = "done";
            extra.geoWritten = true;
            if (built && !flow.busy) { stale = true; }
            break;
        case "compare":
            stage.compare = "done";
            // The comparison runs the geometry engine itself when Step 4
            // was skipped, so the geometry is in either way.
            if (stage.geometry === "waiting") { stage.geometry = "done"; }
            break;
        case "final":
            stage.final = "done";
            stage.areas = stage.statement = "waiting";
            break;
        case "areas":
            stage.areas = "done";
            stage.statement = d.statement ? "done" : "failed";
            extra.source = d.source || "";
            extra.touched = true;
            if (/final/i.test(extra.source)) { stale = false; }
            break;
        default:
            return;
        }
        render();
    });

    /* -------------------------------------------------------------- paint */
    var CHIP = {
        done: ["Ready", "ready"], running: ["Building", "running"], waiting: ["Waiting", "waiting"],
        failed: ["Not built", "failed"], stopped: ["Stopped", "failed"], partial: ["Partly ready", "partial"]
    };
    var STATE_TEXT = { done: "DONE", running: "RUNNING", waiting: "WAITING", failed: "FAILED", stopped: "STOPPED" };

    function anchorStage(a, cardStage) {
        return a.getAttribute("data-stage") || cardStage;
    }
    function stageOk(key) {
        if (key === "geo-generate") { return extra.geoWritten; }
        return stage[key] === "done";
    }
    function stageRunning(key) {
        if (key === "geo-generate") { return stage.geometry === "running"; }
        return stage[key] === "running";
    }

    var CARD_STAGE = { final: "final", statement: "statement", areas: "areas", quantities: "areas" };

    function paintCards() {
        Array.prototype.forEach.call(document.querySelectorAll("#foCards .fo-card"), function (card) {
            var name = card.getAttribute("data-card");
            if (name === "later") { return; }
            var files = Array.prototype.slice.call(card.querySelectorAll(".fo-files a"));
            var okN = 0, runN = 0, failN = 0;
            files.forEach(function (a) {
                var key = anchorStage(a, CARD_STAGE[name]);
                var ok = stageOk(key);
                var src = a.getAttribute("data-src");
                if (src && ok) {
                    var from = $(src);
                    var href = from ? from.getAttribute("href") : "";
                    ok = !!(href && href !== "#");
                    if (ok) { a.setAttribute("href", href); }
                }
                a.classList.toggle("is-off", !ok);
                a.setAttribute("aria-disabled", ok ? "false" : "true");
                if (ok) { a.removeAttribute("tabindex"); } else { a.setAttribute("tabindex", "-1"); }
                if (ok) { okN++; }
                else if (stageRunning(key)) { runN++; }
                else if (stage[key] === "failed" || stage[key] === "stopped") { failN++; }
            });
            var view = card.querySelector("#areasViewStatement");
            if (view) { view.disabled = !stageOk("statement"); }
            var st = okN === files.length ? "done" : runN ? "running" : okN ? "partial" : failN ? "failed" : "waiting";
            var chip = card.querySelector(".fo-chip");
            chip.textContent = CHIP[st][0];
            chip.className = "fo-chip is-" + CHIP[st][1];
            card.classList.toggle("is-ready", st === "done");
        });
    }

    function paintProgress() {
        var box = $("foProgress");
        var any = extra.touched || ORDER.some(function (k) { return stage[k] !== "waiting"; });
        box.classList.toggle("hidden", !any);
        if (!any) { return; }
        var n = ORDER.filter(function (k) { return stage[k] === "done"; }).length;
        $("foCount").textContent = n + " of " + ORDER.length + " · one drawing at a time";
        $("foBar").style.width = Math.round(n / ORDER.length * 100) + "%";
        $("foStages").innerHTML = STAGES.map(function (s) {
            var k = stage[s[0]];
            return '<div class="fo-stage is-' + k + '"><span class="fo-stage-state">' + STATE_TEXT[k] +
                '</span><span class="fo-stage-label">' + esc(s[1]) + '</span>' +
                '<span class="fo-stage-where">' + (s[2] === 6 ? "this step" : "same work as Step " + s[2]) + "</span></div>";
        }).join("");
        var status = $("foStatus");
        if (flow.busy) {
            var cur = flow.seq && flow.seq[flow.at];
            setStatus(status, "Working on: " + (cur ? labelOf(cur) : "the outputs") +
                      ". The downloads unlock as they finish." +
                      (flow.kind === "fast" ? " Steps 3-5 fill in as this runs; open any to review." : ""), "busy");
        } else if (flow.failed) {
            status.innerHTML = "";
            status.className = "status error";
            status.appendChild(document.createTextNode(stage[flow.failed] === "stopped"
                ? "Stopped at " + labelOf(flow.failed) + ". Press Restart to carry on from there; what finished is kept. "
                : labelOf(flow.failed) + " did not finish" + (flow.note ? ": " + flow.note.replace(/\.?$/, ".") : ".") +
                  " Press Restart to try again from there. "));
            var step = STEP_OF[flow.failed];
            if (step !== 6 && step !== 2) {
                var go = document.createElement("button");
                go.type = "button";
                go.className = "btn btn-ghost btn-mini";
                go.textContent = "Open Step " + step;
                go.addEventListener("click", function () { if (window.DA_showStep) { window.DA_showStep(step); } });
                status.appendChild(go);
            } else if (step === 2 && window.DA_showStep) {
                var back = document.createElement("button");
                back.type = "button";
                back.className = "btn btn-ghost btn-mini";
                back.textContent = "Back to Step 2";
                back.addEventListener("click", function () { window.DA_showStep(2); });
                status.appendChild(back);
            }
        } else if (n === ORDER.length) {
            setStatus(status, "All outputs are ready.", "ok");
        } else {
            setStatus(status, "", "");
        }
    }
    function labelOf(key) {
        for (var i = 0; i < STAGES.length; i++) { if (STAGES[i][0] === key) { return STAGES[i][1]; } }
        return key;
    }
    function setStatus(el, msg, kind) {
        el.textContent = msg || "";
        el.className = "status" + (kind ? " " + kind : "");
    }

    function paintHead() {
        var h = $("foHeadline");
        var all = stage.areas === "done" && stage.final === "done";
        var some = stage.areas === "done";
        h.textContent = flow.busy ? "Final outputs — processing, downloads unlock as they finish"
            : flow.failed ? "Final outputs — stopped before the end"
            : all ? "Final outputs — ready to download"
            : some ? "Final outputs — areas ready" : "Final outputs";
        $("foStale").classList.toggle("hidden", !stale || flow.busy);
        var name = api().projectName ? api().projectName() : "";
        $("foProject").innerHTML = name
            ? "Project name for the statement: <b>" + esc(name) + "</b> (from Step 1)"
            : "No project name in Step 1 - the statement takes the drawing's own.";
        var src = $("foSource");
        if (src) { src.textContent = extra.source || "-"; }
    }

    function paintBadges() {
        if (!window.DA_stepBadge) { return; }
        var fast = flow.kind === "fast" && (flow.busy || flow.failed || stage.final === "done");
        var per = { 3: ["annotate"], 4: ["geometry"], 5: ["compare", "final"] };
        Object.keys(per).forEach(function (n) {
            if (!fast) { window.DA_stepBadge(+n, "", ""); return; }
            var ks = per[n];
            var done = ks.every(function (k) { return stage[k] === "done"; });
            var bad = ks.some(function (k) { return stage[k] === "failed" || stage[k] === "stopped"; });
            window.DA_stepBadge(+n, done ? "ready" : bad ? "stopped" : flow.busy ? "running" : "",
                                done ? "ready" : bad ? "failed" : "running");
        });
        window.DA_stepBadge(6, flow.busy ? "running" : "", flow.busy ? "running" : "");
    }

    function render() {
        paintHead();
        paintProgress();
        paintCards();
        paintBadges();
    }
    // The project name is read live, so an edit in Step 1 shows here.
    var pn = $("projectName");
    if (pn) { pn.addEventListener("input", paintHead); }
    render();
})();
