/* Back, Stop and Restart on every step.
 *
 * Every long request a step makes - floor detection, text extraction,
 * boundaries, the geometry engine, the comparison, the areas - goes
 * through the fetch wrapper below, which gives it a job id (X-DA-Job) and
 * an AbortController.
 *
 *   Stop     aborts the request and tells the server to stop that job
 *            (POST /cancel); the step reads "Stopped" in its own status
 *            line, as for any reply it could not use.
 *   Restart  repeats the step's last action - the button that started it,
 *            else the step's main button. Floors finished before the stop
 *            are kept by the server, so it carries on where it stopped.
 *   Back     goes to the previous step.
 *
 * On Step 6 the three also cover the run flow.js makes in the background
 * (Fast track, or Generate final outputs): Stop ends it, whatever step its
 * request belongs to; Restart carries it on from the stage it stopped at.
 */
(function () {
    "use strict";

    // Which step a request belongs to, by its path.
    var ROUTES = [
        [/^\/upload\b/, 1],
        [/^\/extract\b/, 2],
        [/^\/annotate\b/, 3],
        [/^\/(geoengine\/(run|generate|review)|geometry)\b/, 4],
        [/^\/geoengine\/(compare|final)\b/, 5],
        [/^\/areas\/(upload|from_output|calculate)\b/, 6]
    ];
    // The button a step's Restart presses when nothing else started it.
    var MAIN = { 1: "runBtn", 2: "extractBtn", 3: "generateBtn", 4: "gaiRerunBtn",
                 5: "gcmRerunBtn", 6: "areasFromFinal" };
    var STOP_MSG = "Stopped. Press Restart to carry on from where it stopped.";

    var running = {};            // step -> {ctl, job, n}
    var trigger = {};            // step -> the button that started its last run
    var lastClick = null;        // {btn, at}

    function stepOf(url) {
        var path;
        try { path = new URL(url, location.href).pathname; } catch (e) { return 0; }
        for (var i = 0; i < ROUTES.length; i++) {
            if (ROUTES[i][0].test(path)) { return ROUTES[i][1]; }
        }
        return 0;
    }

    function sectionOf(n) {
        var marks = document.querySelectorAll("section.card .card-head .step-no");
        for (var i = 0; i < marks.length; i++) {
            if (parseInt(marks[i].textContent, 10) === n) { return marks[i].closest("section.card"); }
        }
        return null;
    }

    function newJob() {
        return "j" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    }

    /* The button the user pressed just before a request started is what
     * Restart presses again. */
    document.addEventListener("click", function (e) {
        var b = e.target.closest && e.target.closest("button");
        if (b && !b.closest(".step-ctl")) { lastClick = { btn: b, at: Date.now() }; }
    }, true);

    var realFetch = window.fetch.bind(window);
    window.fetch = function (input, opts) {
        var url = typeof input === "string" ? input : (input && input.url) || "";
        var step = stepOf(url);
        if (!step) { return realFetch(input, opts); }
        opts = Object.assign({}, opts || {});
        var ctl = new AbortController();
        if (opts.signal) {
            if (opts.signal.aborted) { ctl.abort(); }
            else { opts.signal.addEventListener("abort", function () { ctl.abort(); }); }
        }
        opts.signal = ctl.signal;
        var job = newJob();
        var headers = new Headers(opts.headers || (typeof input !== "string" && input.headers) || {});
        headers.set("X-DA-Job", job);
        opts.headers = headers;
        var sec = sectionOf(step);
        if (lastClick && Date.now() - lastClick.at < 3000 && sec && sec.contains(lastClick.btn)) {
            trigger[step] = lastClick.btn;
        }
        var run = running[step] || { n: 0 };
        run.n += 1;
        run.ctl = ctl;
        run.job = job;
        run.jobs = (run.jobs || []).concat([job]);
        running[step] = run;
        paint(step);
        return realFetch(input, opts).catch(function (err) {
            if (err && err.name === "AbortError") {
                // Answer the caller as the server does for a stopped job, so
                // the step shows it in its own status line.
                return new Response(JSON.stringify({ ok: false, stopped: true, error: STOP_MSG }),
                                    { status: 200, headers: { "Content-Type": "application/json" } });
            }
            throw err;
        }).finally(function () {
            var r = running[step];
            if (r) {
                r.n -= 1;
                r.jobs = (r.jobs || []).filter(function (j) { return j !== job; });
                if (r.n <= 0) { delete running[step]; }
            }
            paint(step);
        });
    };

    function flowBusy() { return !!(window.DA_flowBusy && window.DA_flowBusy()); }

    function stop(step) {
        if (step === 6 && flowBusy()) {
            if (window.DA_flowStop) { window.DA_flowStop(); }
            for (var s = 2; s <= 5; s++) { stopOne(s); }
        }
        stopOne(step);
    }
    function stopOne(step) {
        var r = running[step];
        if (!r) { return; }
        (r.jobs || [r.job]).forEach(function (job) {
            realFetch("/cancel", { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify({ job: job }) }).catch(function () {});
        });
        try { r.ctl.abort(); } catch (e) { /* already done */ }
    }

    function restart(step) {
        if (running[step]) { return; }
        if (step === 6 && window.DA_flowResume && window.DA_flowResume()) { return; }
        var b = trigger[step];
        if (!b || !document.body.contains(b) || b.disabled) { b = document.getElementById(MAIN[step]); }
        if (b && !b.disabled) { b.click(); }
    }

    function back(step) {
        // The step menu's own button, so the menu and the page stay in step.
        var items = document.querySelectorAll(".stepnav-item");
        for (var i = 0; i < items.length; i++) {
            var no = items[i].querySelector(".stepnav-no");
            if (no && parseInt(no.textContent, 10) === step - 1 && !items[i].disabled) {
                items[i].click();
                window.scrollTo({ top: 0, behavior: "smooth" });
                return;
            }
        }
    }

    var bars = {};

    function paint(step) {
        var bar = bars[step];
        if (!bar) { return; }
        var busy = !!running[step] || (step === 6 && flowBusy());
        bar.stop.disabled = !busy;
        bar.restart.disabled = busy;
        bar.state.textContent = busy ? "Running..." : "";
        bar.el.classList.toggle("is-running", busy);
    }

    function build(step) {
        var sec = sectionOf(step);
        var body = sec && sec.querySelector(".card-body");
        if (!body || body.querySelector(".step-ctl")) { return; }
        var el = document.createElement("div");
        el.className = "step-ctl";
        el.innerHTML =
            '<button type="button" class="btn btn-ghost btn-mini step-back"' + (step === 1 ? " disabled" : "") +
            ' title="Go to the previous step">&lsaquo; Back</button>' +
            '<span class="step-ctl-state" aria-live="polite"></span>' +
            '<button type="button" class="btn btn-ghost btn-mini step-stop" disabled ' +
            'title="Stop what this step is doing. What it finished is kept.">&#9632; Stop</button>' +
            '<button type="button" class="btn btn-ghost btn-mini step-restart" ' +
            'title="Run this step again - after a Stop it carries on from where it stopped">&#8635; Restart</button>';
        body.insertBefore(el, body.firstChild);
        bars[step] = { el: el, stop: el.querySelector(".step-stop"),
                       restart: el.querySelector(".step-restart"),
                       state: el.querySelector(".step-ctl-state") };
        el.querySelector(".step-back").addEventListener("click", function () { back(step); });
        bars[step].stop.addEventListener("click", function () { stop(step); });
        bars[step].restart.addEventListener("click", function () { restart(step); });
        paint(step);
    }

    for (var s = 1; s <= 6; s++) { build(s); }

    window.DA_stepRunning = function (step) { return !!running[step]; };
    window.DA_stepPaint = function (step) { paint(step); };
})();
