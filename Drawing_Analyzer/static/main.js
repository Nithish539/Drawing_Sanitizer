(() => {
    "use strict";

    const $ = (sel) => document.querySelector(sel);

    const drop = $("#drop");
    const fileInput = $("#file");
    const pickBtn = $("#pickBtn");
    const uploadStat = $("#uploadStatus");

    const floorsCard = $("#step-floors");
    const floorsTbody = $("#floorsTable tbody");
    const selAll = $("#selAll");
    const extractBtn = $("#extractBtn");
    const resetBtn = $("#resetBtn");
    const extractStat = $("#extractStatus");

    const resultsCard = $("#step-results");
    const summaryEl = $("#summary");
    const tabsEl = $("#tabs");
    const resultsTbody = $("#resultsTable tbody");
    const rowsSelAll = $("#rowsSelAll");
    const textSearch = $("#textSearch");
    const checkAllBtn = $("#checkAllBtn");
    const uncheckAllBtn = $("#uncheckAllBtn");
    const generateBtn = $("#generateBtn");
    const boundaryFamilySel = $("#boundaryFamily");
    const annotateStat = $("#annotateStatus");
    const dlXlsx = $("#dlXlsx");
    const dlJson = $("#dlJson");
    const dlDxf = $("#dlDxf");

    // ─────────────────────────────────────────────────────────────────────
    // State
    // ─────────────────────────────────────────────────────────────────────
    let state = {
        token: null,
        candidates: [],
        results: null,         // { floor_name: [row, ...], ... }
        activeFloor: null,
        /** unchecked sr_nos per floor: { floor_name: Set<number> } */
        unchecked: {},
        /** boundary status per floor per sr_no: { floor_name: { sr_no: status } } */
        statusByFloor: {},
        /** list of valid family names for the dropdown, e.g. ["Shaft", "Toilet", ...] */
        familyList: [],
        /** user-chosen family overrides per floor: { floor_name: { sr_no: familyName } } */
        family: {},
    };

    // localStorage helpers — keyed by token + floor name
    function lsKey(token, floor) { return `dxf-uncheck:${token}:${floor}`; }
    function lsKeyFamily(token, floor) { return `dxf-family:${token}:${floor}`; }

    function loadUnchecked(token, floor) {
        try {
            const raw = localStorage.getItem(lsKey(token, floor));
            if (!raw) return new Set();
            const arr = JSON.parse(raw);
            return new Set(Array.isArray(arr) ? arr.map(Number) : []);
        } catch (e) { return new Set(); }
    }

    function saveUnchecked(token, floor, set) {
        try {
            localStorage.setItem(lsKey(token, floor), JSON.stringify(Array.from(set)));
        } catch (e) { /* quota exceeded — ignore */ }
    }

    function loadFamilyOverrides(token, floor) {
        try {
            const raw = localStorage.getItem(lsKeyFamily(token, floor));
            if (!raw) return {};
            const obj = JSON.parse(raw);
            return (obj && typeof obj === "object") ? obj : {};
        } catch (e) { return {}; }
    }

    function saveFamilyOverrides(token, floor, obj) {
        try {
            localStorage.setItem(lsKeyFamily(token, floor), JSON.stringify(obj));
        } catch (e) { /* quota exceeded — ignore */ }
    }

    /* Step 1 inputs - project name and the floor plate area range - are
     * kept for this tab only (sessionStorage): a refresh keeps them, a new
     * tab starts empty. They used to go to localStorage, which every tab
     * shares and which never clears, so an old range turned up in any new
     * tab; those leftovers are removed here. */
    ["da-project-name", "da-min-area", "da-max-area"].forEach((k) => {
        try { localStorage.removeItem(k); } catch (e) { /* storage unavailable */ }
    });
    function keepForThisTab(box, key, onInput) {
        if (!box) return;
        try { box.value = sessionStorage.getItem(key) || ""; } catch (e) { /* none */ }
        box.addEventListener("input", () => {
            if (onInput) onInput();
            try { sessionStorage.setItem(key, box.value.trim()); } catch (e) { /* none */ }
        });
    }

    /* The project name from Step 1. Optional. */
    const projectBox = $("#projectName");
    function projectNameValue() {
        return projectBox ? projectBox.value.trim() : "";
    }
    keepForThisTab(projectBox, "da-project-name");

    /* Floor plate area range (Step 1). Required. */
    const minAreaBox = $("#minArea");
    const maxAreaBox = $("#maxArea");
    [[minAreaBox, "da-min-area"], [maxAreaBox, "da-max-area"]].forEach(([box, key]) => {
        keepForThisTab(box, key, () => { box.style.borderColor = ""; });
    });
    function areaRange() {
        const lo = parseFloat(minAreaBox ? minAreaBox.value : "");
        const hi = parseFloat(maxAreaBox ? maxAreaBox.value : "");
        if (!(lo > 0) || !(hi > lo)) return null;
        return { lo, hi };
    }
    function fmtArea(v) {
        return Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
    }

    /* Every stage that finishes says so: Step 6 (flow.js) keeps its
     * progress and download cards from these, whichever way the stage was
     * started - its own button, or Fast track. */
    function stageDone(name, detail) {
        document.dispatchEvent(new CustomEvent("da:stage",
            { detail: Object.assign({ name: name }, detail || {}) }));
    }
    /* A failed request, for the flow's own status line in Step 6. */
    function flowError(data) {
        if (window.DA_flowNote) window.DA_flowNote(data && data.error, !!(data && data.stopped));
    }

    function setStatus(el, msg, kind = "info") {
        el.className = "status " + kind;
        el.textContent = msg || "";
    }

    /* A spinner on the button that started the work. Several of these
     * steps run for the best part of a minute, and a disabled button on
     * its own reads as a click that did nothing. */
    function setBusy(btn, on) {
        if (!btn) return;
        btn.classList.toggle("is-busy", !!on);
        btn.disabled = !!on;
    }

    /* fetch() cannot report upload progress, and on a 50 MB drawing the
     * upload is most of the wait - so these two go through XHR to get a
     * percentage out of the browser. */
    function postFile(url, fd, onProgress) {
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open("POST", url);
            xhr.upload.addEventListener("progress", (e) => {
                if (e.lengthComputable && onProgress) {
                    onProgress(e.loaded / e.total);
                }
            });
            xhr.addEventListener("load", () => {
                if (onProgress) onProgress(1);
                try {
                    resolve(JSON.parse(xhr.responseText));
                } catch (err) {
                    reject(new Error("the server answered HTTP " + xhr.status +
                                     " with a page instead of JSON."));
                }
            });
            xhr.addEventListener("error",
                () => reject(new Error("the connection dropped.")));
            xhr.addEventListener("abort",
                () => reject(new Error("the upload was cancelled.")));
            xhr.send(fd);
        });
    }

    function setDxfDisabled(disabled, title) {
        if (disabled) {
            dlDxf.style.opacity = "0.55";
            dlDxf.style.pointerEvents = "none";
            dlDxf.style.cursor = "not-allowed";
            dlDxf.setAttribute("aria-disabled", "true");
            dlDxf.dataset.disabled = "1";
            if (title) dlDxf.title = title;
        } else {
            dlDxf.style.opacity = "";
            dlDxf.style.pointerEvents = "";
            dlDxf.style.cursor = "";
            dlDxf.removeAttribute("aria-disabled");
            delete dlDxf.dataset.disabled;
            if (title) dlDxf.title = title;
        }
    }

    // ─────────────────────────────────────────────────────────────────────
    // Upload
    // ─────────────────────────────────────────────────────────────────────
    pickBtn.addEventListener("click", () => fileInput.click());
    drop.addEventListener("click", (e) => {
        if (e.target === pickBtn) return;
        fileInput.click();
    });

    ["dragenter", "dragover"].forEach((ev) =>
        drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) =>
        drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("drag"); }));

    // Choosing or dropping a file only selects it; Run starts the work, so
    // the project name, area range and units can be set first.
    const runBtn = $("#runBtn");
    const fileChosen = $("#fileChosen");
    let pendingFile = null;

    function describeFile(file) {
        const mb = file.size / (1024 * 1024);
        return `Selected: ${file.name} (${mb >= 1 ? mb.toFixed(1) + " MB" : Math.max(1, Math.round(file.size / 1024)) + " KB"})`;
    }

    function chooseFile(file) {
        if (!file.name.toLowerCase().endsWith(".dxf")) {
            setStatus(uploadStat, "Please select a .dxf file.", "error");
            return;
        }
        pendingFile = file;
        fileChosen.textContent = describeFile(file);
        fileChosen.classList.add("is-set");
        drop.classList.add("has-file");
        runBtn.disabled = false;
        runBtn.title = "Upload the file and look for floor plates";
        setStatus(uploadStat, "Ready - click Run to upload and find the floor plates.", "info");
    }

    drop.addEventListener("drop", (e) => {
        if (e.dataTransfer.files && e.dataTransfer.files[0]) {
            chooseFile(e.dataTransfer.files[0]);
        }
    });

    fileInput.addEventListener("change", () => {
        if (fileInput.files && fileInput.files[0]) {
            chooseFile(fileInput.files[0]);
        }
    });

    runBtn.addEventListener("click", () => {
        if (pendingFile) uploadFile(pendingFile);
    });

    async function uploadFile(file) {
        if (!file.name.toLowerCase().endsWith(".dxf")) {
            setStatus(uploadStat, "Please select a .dxf file.", "error");
            return;
        }
        const range = areaRange();
        if (!range) {
            [minAreaBox, maxAreaBox].forEach((b) => {
                if (b) b.style.borderColor = "#b00020";
            });
            setStatus(uploadStat, "Enter the minimum and maximum floor plate area " +
                "(sq.m) first - the maximum must be larger than the minimum, " +
                "then click Run again.", "error");
            return;
        }
        setStatus(uploadStat, `Uploading ${file.name} ...`, "busy");
        if (window.DA_layersInvalid) window.DA_layersInvalid();
        if (window.DA_myRulesToken) window.DA_myRulesToken(null);
        setBusy(pickBtn, true);
        setBusy(runBtn, true);
        drop.classList.add("is-busy");
        floorsCard.classList.add("hidden");
        resultsCard.classList.add("hidden");

        const fd = new FormData();
        fd.append("file", file);
        fd.append("project", projectNameValue());
        fd.append("min_area", String(range.lo));
        fd.append("max_area", String(range.hi));
        // The unit choice has to travel with the file: reading areas in the
        // wrong unit is wrong by a factor of a million, not a rounding error.
        if (window.DA_selectedUnits) fd.append("units", window.DA_selectedUnits("main"));

        try {
            const data = await postFile("/upload", fd, (done) => {
                setStatus(uploadStat, done < 1
                    ? `Uploading ${file.name} - ${Math.round(done * 100)}%`
                    : `Reading ${file.name} - looking for floor outlines...`,
                    "busy");
            });
            if (!data.ok) {
                setStatus(uploadStat, data.error || "Upload failed.", "error");
                return;
            }
            state.token = data.token;
            stageDone("reset");
            if (window.DA_showUnits) window.DA_showUnits(data.units);
            state.candidates = data.candidates;
            state.unchecked = {};
            state.statusByFloor = {};
            state.family = {};
            state.familyList = [];
            setStatus(uploadStat,
                `Uploaded ${data.original_name} - found ${data.candidates.length} candidate polyline(s) between ${fmtArea(range.lo)} and ${fmtArea(range.hi)} sq.m.`,
                "ok");
            const rt = document.getElementById("floorRangeText");
            if (rt) rt.textContent = `Floor plates between ${fmtArea(range.lo)} and ${fmtArea(range.hi)} sq.m`;
            state.usages = data.usages || [];
            state.defaultUsage = data.default_usage || (state.usages[0] || "");
            renderFloors(data.candidates);
            if (window.DA_cadStep2) window.DA_cadStep2(state.token);
            // The layer table was read in the same parse, so Section 6 has
            // something to show before a single floor is picked.
            if (window.DA_layersReady) window.DA_layersReady(state.token);
        } catch (err) {
            setStatus(uploadStat, "Upload failed: " + err.message, "error");
        } finally {
            setBusy(pickBtn, false);
            setBusy(runBtn, false);
            drop.classList.remove("is-busy");
        }
    }

    // ─────────────────────────────────────────────────────────────────────
    // Floor picker (Step 2)
    // ─────────────────────────────────────────────────────────────────────
    function renderFloors(cands) {
        floorsTbody.innerHTML = "";

        if (!cands.length) {
            floorsTbody.innerHTML =
                `<tr><td colspan="11" class="muted" style="text-align:center;padding:20px">
          No closed polylines within the floor plate area range were found in this drawing.
        </td></tr>`;
            floorsCard.classList.remove("hidden");
            extractBtn.disabled = true;
            return;
        }
        extractBtn.disabled = false;

        // One plate is ticked for you - the largest - but never an outline
        // that encloses other plates: that is a sheet border or site
        // outline, and as a "floor" it would take every text on the sheet,
        // so boundaries came out on every floor. The rest is your choice.
        // Nor an outline on DEFPOINTS: AutoCAD's non-plotting layer holds
        // construction copies, not the drawn floor.
        const plain = cands.filter((c) => !c.encloses && !/^DEFPOINTS$/i.test((c.layer || "").split("$").pop()));
        const preset = new Set(plain.slice(0, 1).map((c) => c.index));
        state.candidates = cands;
        // The plate's usage (Office, Parking...) decides which families
        // apply on it; read from its title, changeable here.
        state.plateUsage = {};
        cands.forEach((c) => { state.plateUsage[c.index] = c.usage || state.defaultUsage || ""; });
        // One row per floor a plate stands for ("Typical 2nd, 4th & 6th
        // Floor" -> 2nd, 4th, 6th), each ticked and named on its own.
        state.floorRows = [];
        cands.forEach((c, i) => {
            const tower = towerOf(c.name);
            const levels = (c.levels && c.levels.length) ? c.levels : [null];
            levels.forEach((lv) => {
                const plateName = c.name || `Floor_${i + 1}`;
                state.floorRows.push({
                    id: state.floorRows.length, idx: c.index, tower,
                    key: lv ? lv.key : null,
                    label: lv ? lv.label : plateName,
                    refuge: !!(lv && lv.refuge),
                    name: lv ? rowName(tower, lv) : plateName,
                    checked: preset.has(c.index),
                });
            });
        });
        drawFloorRows();
        floorsCard.classList.remove("hidden");
        setStatus(extractStat, "", "info");
    }

    // How each plate was found: a closed polyline, or joined from open
    // lines and polylines - or both.
    function sourceBadge(c) {
        const srcs = (c.sources && c.sources.length) ? c.sources : [c.source || "Closed Polyline"];
        return srcs.map((s) => `<span class="src-badge ${s === "Closed Polyline" ? "src-closed" : "src-lines"}"` +
            ` title="${s === "Closed Polyline" ? "A closed polyline in the drawing" :
                "Joined from separate lines / open polylines whose ends meet (gaps up to 2 m)"}">${escapeHtml(s)}</span>`).join(" ");
    }

    // "Tower 2 - Typical 2nd & 4th Floor" -> "Tower 2" (sheets with towers).
    function towerOf(name) {
        const m = /^(Tower\s+\S+)\s+-\s+/i.exec(name || "");
        return m ? m[1] : "";
    }
    function rowName(tower, lv) {
        return (tower ? tower + " - " : "") + lv.label + (lv.refuge ? " (Refuge)" : "");
    }
    // The floor a row stands for; rows of one floor are grouped together,
    // and only one of them can be ticked. null: a plate with no floor named.
    function floorId(r) {
        return r.key == null ? null : r.tower + "|" + r.key;
    }
    // {floor: the ticked row that holds it}
    function takenFloors() {
        const m = {};
        state.floorRows.forEach((r) => {
            const f = floorId(r);
            if (f !== null && r.checked && !(f in m)) m[f] = r;
        });
        return m;
    }
    function isStruck(r, taken) {
        const f = floorId(r);
        return f !== null && !!taken[f] && taken[f] !== r;
    }
    // Bottom to top, so every copy of a floor sits together.
    function sortedRows() {
        return state.floorRows.slice().sort((a, b) =>
            a.tower.localeCompare(b.tower) ||
            (a.key == null) - (b.key == null) ||
            (a.key == null ? 0 : a.key - b.key) ||
            a.idx - b.idx || a.id - b.id);
    }
    function rowOf(el) {
        const tr = el && el.closest("tr");
        return tr && tr.dataset.row != null ? state.floorRows[Number(tr.dataset.row)] : null;
    }

    // The plan title as read from the drawing, with what it was written in:
    // MTEXT (all lines in one entity), TEXT, or TEXT lines joined into one.
    function titleCell(c) {
        if (!c.title) return '<span class="muted small" title="No floor title found under or above this plate">none found</span>';
        const src = c.title_source || "";
        const kind = /^MTEXT/.test(src) ? "mtext" : /^TEXT/.test(src) ? "text" : "other";
        return `<span class="title-text">${escapeHtml(c.title)}</span>` +
            (src ? ` <span class="title-src title-src-${kind}" title="Read from ${escapeHtml(src)}">${escapeHtml(src)}</span>` : "");
    }

    // One row per floor, sorted by floor. Ticking a floor strikes out the
    // same floor on every other plate: a struck-out row is not used for
    // anything - texts, boundaries, areas or the statement.
    // The usage of a floor row's plate, as a picker. All rows of a plate
    // share it.
    function usageSelect(r) {
        const cur = (state.plateUsage || {})[r.idx] || "";
        const opts = (state.usages || []).map((u) =>
            `<option value="${escapeHtml(u)}"${u === cur ? " selected" : ""}>${escapeHtml(u)}</option>`).join("");
        return `<select class="rowusage" data-idx="${r.idx}" title="What the floor is used for - only the families that apply to this usage are used on it (see Family rules)">${opts}</select>`;
    }

    function drawFloorRows() {
        floorsTbody.innerHTML = "";
        const byIdx = {};
        (state.candidates || []).forEach((c) => { byIdx[c.index] = c; });
        const taken = takenFloors();
        const rows = sortedRows();
        const copies = {}, perPlate = {};
        rows.forEach((r) => {
            const f = floorId(r);
            if (f !== null) copies[f] = (copies[f] || 0) + 1;
            perPlate[r.idx] = (perPlate[r.idx] || 0) + 1;
        });
        let prev;
        rows.forEach((r) => {
            const c = byIdx[r.idx];
            if (!c) return;
            const f = floorId(r);
            const struck = isStruck(r, taken);
            const tr = document.createElement("tr");
            tr.dataset.idx = r.idx;
            tr.dataset.row = r.id;
            if (c.encloses) tr.classList.add("row-encloses");
            if (struck) tr.classList.add("row-struck");
            if (f !== null && copies[f] > 1) tr.classList.add("row-floorgroup");
            if (f !== null && f === prev) tr.classList.add("row-samefloor");
            prev = f;
            const warn = c.encloses
                ? `<div class="enclose-warn" title="This outline surrounds ${c.encloses} other floor plate(s). It is probably a sheet border or site outline, not a floor.">&#9888; encloses ${c.encloses} other plate${c.encloses === 1 ? "" : "s"}</div>`
                : "";
            const note = struck
                ? `<div class="floor-taken" title="This floor is taken from plate ${taken[f].idx}; untick that row to use this one instead">taken from plate ${taken[f].idx}</div>`
                : (f !== null && copies[f] > 1 && !taken[f]
                    ? `<div class="floor-alt" title="The same floor is on ${copies[f]} plates; tick the one to use">on ${copies[f]} plates - tick one</div>` : "");
            const many = perPlate[r.idx] > 1
                ? ` <span class="muted small" title="Plate ${r.idx} stands for ${perPlate[r.idx]} floors">plate of ${perPlate[r.idx]} floors</span>` : "";
            tr.innerHTML = `
        <td><input type="checkbox" class="rowsel" data-idx="${r.idx}" data-row="${r.id}" ${r.checked && !struck ? "checked" : ""} ${struck ? "disabled" : ""} /></td>
        <td>${r.idx}</td>
        <td class="floor-cell"><b>${escapeHtml(r.label)}</b>${r.refuge ? ' <span class="muted small">(Refuge)</span>' : ""}${many}${note}</td>
        <td>${c.area_sqm.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
        <td>${escapeHtml(c.layer)}${warn}</td>
        <td>${c.num_points}</td>
        <td>${escapeHtml(c.entity_type)}</td>
        <td>${sourceBadge(c)}</td>
        <td class="title-cell">${titleCell(c)}</td>
        <td>${usageSelect(r)}</td>
        <td><input type="text" class="rowname" data-idx="${r.idx}" data-row="${r.id}" value="${escapeHtml(r.name)}" ${struck ? "disabled" : ""}
                   title="${c.title ? "Plan title: " + escapeHtml(c.title) : "No plan title found for this plate"} - type another floor (e.g. 8th Floor) to change the floor this row stands for" /></td>`;
            floorsTbody.appendChild(tr);
        });
        const box = document.getElementById("floorDupWarn");
        if (box) { box.innerHTML = ""; box.classList.add("hidden"); }
    }

    floorsTbody.addEventListener("change", (e) => {
        const us = e.target.closest(".rowusage");
        if (us) {
            const idx = Number(us.dataset.idx);
            state.plateUsage[idx] = us.value;
            floorsTbody.querySelectorAll(`.rowusage[data-idx="${idx}"]`).forEach((x) => { x.value = us.value; });
            return;
        }
        const cb = e.target.closest(".rowsel");
        const r = cb && rowOf(cb);
        if (!r) return;
        r.checked = cb.checked;
        drawFloorRows();
    });
    floorsTbody.addEventListener("input", (e) => {
        const nm = e.target.closest(".rowname");
        const r = nm && rowOf(nm);
        if (r) r.name = nm.value;
    });
    // A new name may stand for another floor: "8th Floor" moves the row to
    // the 8th floor (its place in the list, and what it strikes out).
    floorsTbody.addEventListener("focusout", async (e) => {
        const nm = e.target.closest(".rowname");
        const r = nm && rowOf(nm);
        if (!r) return;
        try {
            const res = await fetch("/floors/levels", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ names: [r.name] }),
            });
            const data = await res.json();
            if (!data.ok) return;
            const lv = (data.levels[0] || [])[0];
            const before = [r.key, r.label, r.refuge].join("|");
            if (lv) { r.key = lv.key; r.label = lv.label; r.refuge = !!lv.refuge; }
            else { r.key = null; r.label = r.name; r.refuge = false; }
            if ([r.key, r.label, r.refuge].join("|") === before) return;
            // Renamed onto a floor another ticked row holds: that one keeps it.
            const t = takenFloors()[floorId(r)];
            if (r.checked && t && t !== r) r.checked = false;
            drawFloorRows();
            floorsTbody.dispatchEvent(new Event("change"));
        } catch (err) { /* the row keeps its floor */ }
    });

    // Tick all: every shown floor once, lowest plate first.
    selAll.addEventListener("change", () => {
        const shown = new Set(shownBoxes(floorsTbody, ".rowsel").map((cb) => Number(cb.dataset.row)));
        if (selAll.checked) {
            const taken = takenFloors();
            sortedRows().forEach((r) => {
                if (!shown.has(r.id) || r.checked) return;
                const f = floorId(r);
                if (f !== null && taken[f]) return;
                r.checked = true;
                if (f !== null) taken[f] = r;
            });
        } else {
            state.floorRows.forEach((r) => { if (shown.has(r.id)) r.checked = false; });
        }
        drawFloorRows();
    });

    // The drawing in Step 2: a click on a plate ticks all its floors that
    // are free, or unticks them all.
    window.DA_togglePlate = function (idx) {
        if (!state.floorRows) return;
        const rows = state.floorRows.filter((r) => r.idx === idx);
        const on = rows.some((r) => r.checked);
        const taken = takenFloors();
        rows.forEach((r) => {
            if (on) { r.checked = false; return; }
            if (!isStruck(r, taken)) r.checked = true;
        });
        drawFloorRows();
    };

    // The plates to process, with the floors ticked for each. A plate
    // with several ticked floors is named for them all; a struck-out
    // floor is in none.
    function floorPicks() {
        const by = new Map();
        sortedRows().forEach((r) => {
            if (!r.checked) return;
            if (!by.has(r.idx)) by.set(r.idx, []);
            by.get(r.idx).push(r);
        });
        return Array.from(by, ([idx, rows]) => ({
            index: idx,
            usage: (state.plateUsage || {})[idx] || "",
            name: plateNameOf(rows),
            levels: rows.map((r) => ({
                key: r.key == null ? 5000 + r.id : r.key,
                label: r.key == null ? r.name : r.label,
                refuge: r.refuge,
            })),
        }));
    }
    function plateNameOf(rows) {
        if (rows.length === 1) return (rows[0].name || "").trim() || rows[0].label;
        if (rows.some((r) => r.key == null || !/ Floor$/.test(r.label))) {
            return rows.map((r) => (r.name || r.label).trim()).join(", ");
        }
        const parts = rows.map((r) => r.label.replace(/ Floor$/, ""));
        const list = parts.slice(0, -1).join(", ") + " & " + parts[parts.length - 1];
        const tower = rows[0].tower;
        return (tower ? tower + " - " : "") + "Typical " + list + " Floor" +
            (rows.every((r) => r.refuge) ? " (Refuge)" : "");
    }
    document.getElementById("resultsTable")
        .addEventListener("tf:change", () => refreshHeaderCheckbox());

    resetBtn.addEventListener("click", () => {
        state = {
            token: null, candidates: [], results: null, activeFloor: null,
            unchecked: {}, statusByFloor: {}, familyList: [], family: {}
        };
        fileInput.value = "";
        uploadStat.textContent = "";
        floorsCard.classList.add("hidden");
        resultsCard.classList.add("hidden");
    });

    // ─────────────────────────────────────────────────────────────────────
    // Extract texts (Step 3)
    // ─────────────────────────────────────────────────────────────────────
    /* Problems with the Step 2 picks that stop a run, or "" if none.
     * Fast track asks no questions, so an outline that encloses other
     * plates stops it here instead of asking to continue. */
    function checkPicks(fast) {
        const picks = floorPicks();
        if (!picks.length) return "Please tick at least one floor.";
        const names = picks.map((p) => p.name);
        if (new Set(names).size !== names.length) return "Floor names must be unique.";
        if (fast) {
            const byIdx = {};
            (state.candidates || []).forEach((c) => { byIdx[c.index] = c; });
            const wide = picks.filter((p) => byIdx[p.index] && byIdx[p.index].encloses);
            if (wide.length) {
                return wide.map((p) => `"${p.name}"`).join(", ") + " surrounds other floor plates. " +
                    "Untick it to work on the chosen floors only, or run Step by step to be asked.";
            }
        }
        return "";
    }

    extractBtn.addEventListener("click", () => {
        if (window.DA_runMode && window.DA_runMode() === "fast" && window.DA_fastTrack) {
            window.DA_fastTrack();
            return;
        }
        runExtract({});
    });

    /* Step 2's Proceed: read the texts of the ticked floors. True when the
     * texts are in; the step's status line says why when not. */
    async function runExtract(opts) {
        opts = opts || {};
        const picks = floorPicks();
        const problem = checkPicks(!!opts.fast);
        if (problem) {
            setStatus(extractStat, problem, "error");
            return false;
        }
        const byIdx = {};
        (state.candidates || []).forEach((c) => { byIdx[c.index] = c; });
        const wide = picks.filter((p) => byIdx[p.index] && byIdx[p.index].encloses);
        if (wide.length && !opts.fast) {
            const list = wide.map((p) => `"${p.name}" (encloses ${byIdx[p.index].encloses} other plates)`).join(", ");
            const ok = window.confirm(
                `${list} surrounds other floor plates, so it would take every text inside them ` +
                `and boundaries would be drawn on all of those floors too.\n\n` +
                `Untick it to work on the chosen floors only. Continue anyway?`);
            if (!ok) {
                setStatus(extractStat, "Untick the outline that encloses other plates, then Extract again.", "error");
                return false;
            }
        }

        setStatus(extractStat,
                  "Extracting texts (may take a minute for large files)...",
                  "busy");
        setBusy(extractBtn, true);

        try {
            const res = await fetch("/extract", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ token: state.token, floors: picks }),
            });
            const data = await res.json();
            if (!data.ok) {
                setStatus(extractStat, data.error || "Extraction failed.", "error");
                flowError(data);
                return false;
            }
            state.results = data.results;
            state.familyList = data.family_list || [];
            state.familyUsages = {};
            (data.families || []).forEach((f) => { state.familyUsages[f.name] = f.usages || []; });
            state.floorUsage = data.floor_usage || {};
            renderBoundaryFamilyOptions(data.families || []);

            // Initialize per-floor unchecked sets and family overrides from
            // localStorage (or empty).
            state.unchecked = {};
            state.family = {};
            Object.keys(state.results).forEach((floor) => {
                state.unchecked[floor] = loadUnchecked(state.token, floor);
                state.family[floor] = loadFamilyOverrides(state.token, floor);
            });

            setStatus(extractStat, "Done. Review texts below and click Generate Boundaries.", "ok");
            renderResults(data);
            // My rules draws from the uploaded DXF and these texts, not
            // from Step 3's output, so it can run from here on.
            if (window.DA_myRulesToken) window.DA_myRulesToken(state.token);
            stageDone("extract");
            return true;
        } catch (err) {
            setStatus(extractStat, "Network error: " + err.message, "error");
            flowError({ error: "Network error: " + err.message });
            return false;
        } finally {
            setBusy(extractBtn, false);
        }
    }

    // ─────────────────────────────────────────────────────────────────────
    // Render results (Step 3)
    // ─────────────────────────────────────────────────────────────────────
    function renderResults(data) {
        textSearch.value = "";
        if (window.DA_geometryReset) window.DA_geometryReset();
        if (window.DA_geoaiReset) window.DA_geoaiReset();
        summaryEl.innerHTML = "";
        data.summary.forEach((s) => {
            const span = document.createElement("span");
            span.className = "pill";
            span.innerHTML = `${escapeHtml(s.floor)} <b>${s.count}</b>`;
            summaryEl.appendChild(span);
        });

        dlXlsx.href = data.download.xlsx;
        dlJson.href = data.download.json;
        dlDxf.href = "#";
        setDxfDisabled(true, "Generate Boundaries first");

        tabsEl.innerHTML = "";
        const floorNames = Object.keys(state.results);
        floorNames.forEach((name, i) => {
            const b = document.createElement("button");
            b.className = "tab" + (i === 0 ? " active" : "");
            b.textContent = name;
            b.addEventListener("click", () => {
                tabsEl.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
                b.classList.add("active");
                state.activeFloor = name;
                renderRows();
            });
            tabsEl.appendChild(b);
        });

        if (floorNames.length) {
            state.activeFloor = floorNames[0];
            renderRows();
        } else {
            resultsTbody.innerHTML = "";
        }

        resultsCard.classList.remove("hidden");
        resultsCard.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function statusBadge(status) {
        // Returns HTML for the status cell. Color & icon depend on status.
        switch (status) {
            case "x": return '<span title="Claimed by X-shaft" style="color:#1f3a5f;font-weight:600">X-shaft</span>';
            case "primary": return '<span title="Drawn (primary)"     style="color:#0a8f3a;font-weight:600">✓ drawn</span>';
            case "secondary": return '<span title="Drawn (secondary)"   style="color:#0a8f3a;font-weight:600">✓ drawn</span>';
            case "absorbed": return '<span title="Inside a primary boundary" style="color:#666">⊂ inside</span>';
            case "merged": return '<span title="Merged into another rectangle" style="color:#a86200">⊕ merged</span>';
            case "no_walls": return '<span title="No walls found around this text" style="color:#b00020">✗ no walls</span>';
            case "no_evidence": return '<span title="No wall, facade, room, primary door or column closes this room - not drawn (rule: a room boundary stands only on evidence)" style="color:#b00020">✗ no evidence</span>';
            case "filtered": return '<span title="Belongs to another boundary type, so it was not exported this run" style="color:#666">⊘ other type</span>';
            case "pending": return '<span style="color:#999">—</span>';
            case "open_side": return '<span title="Its room runs along the floor plate edge where the drawing has no line - open on that side, so not drawn" style="color:#e8590c;font-weight:600">&#9888; open side</span>';
            case "ignored": return '<span title="Family Ignore: no boundary is drawn and the text is not used further" style="color:#868e96">⊘ ignored</span>';
            case "not_lift": return '<span title="A lift label in a space with hinged doors opening into it - a lift\'s doors slide, so it is a lobby; the label is refused and the space left as common floor" style="color:#e8590c;font-weight:600">&#9888; not a lift</span>';
            default: return '<span style="color:#999">—</span>';
        }
    }

    // Families that apply on the active floor: those listing its usage, and
    // those listing none. The row's own family is always offered.
    function familiesForFloor(currentFamily) {
        const usage = ((state.floorUsage || {})[state.activeFloor] || "").toLowerCase();
        const fu = state.familyUsages || {};
        return (state.familyList || []).filter((name) => {
            const us = fu[name] || [];
            return name === currentFamily || !usage || !us.length ||
                us.some((u) => u.toLowerCase() === usage);
        });
    }

    function familySelectHtml(row, currentFamily) {
        const opts = familiesForFloor(currentFamily).map((name) => {
            const sel = name === currentFamily ? " selected" : "";
            return `<option value="${escapeHtml(name)}"${sel}>${escapeHtml(name)}</option>`;
        }).join("");
        return `<select class="familysel" data-sr="${row.sr_no}">${opts}</select>`;
    }

    // Boundary-type picker: "All boundaries" plus one entry per family,
    // labelled with the DXF layer the family writes to.
    function renderBoundaryFamilyOptions(families) {
        const previous = boundaryFamilySel.value;
        boundaryFamilySel.innerHTML = '<option value="">All boundaries</option>';
        families.forEach((fam) => {
            if (fam.name === "Ignore") return;            // nothing to export
            const opt = document.createElement("option");
            opt.value = fam.name;
            opt.textContent = `${fam.name} (${fam.layer})`;
            boundaryFamilySel.appendChild(opt);
        });
        if (previous && families.some((f) => f.name === previous)) {
            boundaryFamilySel.value = previous;
        }
    }

    // The table follows the boundary type: pick AI_SHAFT and only
    // the texts of the Shaft family are listed.
    boundaryFamilySel.addEventListener("change", () => {
        invalidateAnnotation();
        if (state.results && state.activeFloor) renderRows();
    });

    function visibleRows(floor) {
        const rows = state.results[floor] || [];
        const want = boundaryFamilySel.value || "";
        const query = (textSearch.value || "").trim().toLowerCase();
        const overrides = state.family[floor] || {};
        const show = (document.getElementById("textShow") || {}).value || "all";
        return rows.filter((r) => {
            const fam = overrides[r.sr_no] || r.family || "Unknown";
            if (want && fam !== want) return false;
            // Show: the Unknown texts (what still needs a family), the
            // others, or all of them - the drawing follows the same choice.
            if (show === "unknown" && fam !== "Unknown") return false;
            if (show === "others" && fam === "Unknown") return false;
            if (!query) return true;
            return String(r.text || "").toLowerCase().includes(query);
        });
    }

    // Search narrows what is shown; it never changes what is ticked.
    textSearch.addEventListener("input", () => {
        if (state.results && state.activeFloor) renderRows();
    });
    document.getElementById("textShow").addEventListener("change", () => {
        if (state.results && state.activeFloor) renderRows();
    });

    // The family-rules editor calls this after a save so the Family column
    // and the boundary-type picker follow the new ruleset without a reload.
    window.DA_familiesChanged = function (familyList, familyOptions) {
        if (Array.isArray(familyList) && familyList.length) {
            state.familyList = familyList;
        }
        if (Array.isArray(familyOptions) && familyOptions.length) {
            renderBoundaryFamilyOptions(familyOptions);
        }
        if (state.results && state.activeFloor) renderRows();
    };

    // The CAD viewer (cadsteps.js) reads the table's state through this,
    // so it always shows exactly what the table shows.
    // Every floor's extracted texts, for the Geometry tab's drawing.
    window.DA_step3Results = function () { return state.results || {}; };

    window.DA_step3State = function () {
        const floor = state.activeFloor;
        return {
            token: state.token,
            floor,
            rows: (state.results && state.results[floor]) || [],
            visible: state.results && floor ? visibleRows(floor) : [],
            unchecked: state.unchecked[floor] || new Set(),
            family: state.family[floor] || {},
        };
    };

    function renderRows() {
        if (window.DA_cadStep3) window.DA_cadStep3();
        const floor = state.activeFloor;
        const all = state.results[floor] || [];
        const rows = visibleRows(floor);
        const unchecked = state.unchecked[floor] || new Set();
        const statuses = state.statusByFloor[floor] || {};
        const familyOverrides = state.family[floor] || {};
        const want = boundaryFamilySel.value || "";

        const query = (textSearch.value || "").trim();
        const note = document.getElementById("rowsFilterNote");
        if (note) {
            const bits = [];
            const show = document.getElementById("textShow").value;
            if (show === "unknown") bits.push("Unknown only (Show: All to see the rest)");
            if (show === "others") bits.push("texts with a family");
            if (want) bits.push(`${want} only`);
            if (query) bits.push(`matching "${query}"`);
            note.textContent = bits.length
                ? `Showing ${rows.length} of ${all.length} texts on this floor - ${bits.join(", ")}.`
                : "";
        }

        resultsTbody.innerHTML = "";
        if (!rows.length) {
            resultsTbody.innerHTML =
                `<tr><td colspan="7" class="muted" style="text-align:center;padding:20px">
          ${query ? "No texts on this floor match “" + escapeHtml(query) + "”."
            : want ? "No " + escapeHtml(want) + " texts on this floor." : "No texts found inside this floor."}
        </td></tr>`;
            rowsSelAll.checked = false;
            return;
        }

        const frag = document.createDocumentFragment();
        rows.forEach((r) => {
            const isChecked = !unchecked.has(r.sr_no);
            // Status keys may come back as strings (JSON serialization) — try both
            const currentFamily = familyOverrides[r.sr_no] || r.family || "Unknown";
            // Ignore: no boundary, nothing further.
            const st = currentFamily === "Ignore" ? "ignored"
                : (statuses[r.sr_no] || statuses[String(r.sr_no)] || "");
            const tr = document.createElement("tr");
            tr.innerHTML = `
        <td><input type="checkbox" class="textsel" data-sr="${r.sr_no}" ${isChecked ? "checked" : ""} /></td>
        <td>${r.sr_no}</td>
        <td>${escapeHtml(r.text)}</td>
        <td>${escapeHtml(r.source)}</td>
        <td>${escapeHtml(r.layer)}</td>
        <td>${familySelectHtml(r, currentFamily)}</td>
        <td>${statusBadge(st)}</td>`;
            frag.appendChild(tr);
        });
        resultsTbody.appendChild(frag);

        // Update header checkbox state
        refreshHeaderCheckbox();
    }

    // The rows on screen: a column filter (tablefilter.js) hides the rest,
    // and ticking all acts on what is showing, as in Excel.
    function shownBoxes(tbody, cls) {
        return Array.prototype.filter.call(tbody.querySelectorAll(cls),
            (cb) => !cb.closest("tr").classList.contains("tf-hidden"));
    }

    function refreshHeaderCheckbox() {
        const boxes = shownBoxes(resultsTbody, ".textsel");
        if (!boxes.length) { rowsSelAll.checked = false; return; }
        let allOn = true, anyOn = false;
        boxes.forEach((cb) => { if (cb.checked) anyOn = true; else allOn = false; });
        rowsSelAll.checked = allOn;
        rowsSelAll.indeterminate = !allOn && anyOn;
    }

    // Per-row checkbox change → update state.unchecked + persist
    resultsTbody.addEventListener("change", (e) => {
        if (e.target.classList.contains("familysel")) {
            const sr = Number(e.target.dataset.sr);
            const floor = state.activeFloor;
            if (!floor) return;
            if (!state.family[floor]) state.family[floor] = {};
            state.family[floor][sr] = e.target.value;
            saveFamilyOverrides(state.token, floor, state.family[floor]);
            invalidateAnnotation();
            const cell = e.target.closest("tr") && e.target.closest("tr").lastElementChild;
            if (cell) cell.innerHTML = statusBadge(e.target.value === "Ignore" ? "ignored" : "");
            if (window.DA_cadStep3) window.DA_cadStep3();
            return;
        }
        if (!e.target.classList.contains("textsel")) return;
        const sr = Number(e.target.dataset.sr);
        const floor = state.activeFloor;
        if (!floor) return;
        if (!state.unchecked[floor]) state.unchecked[floor] = new Set();
        if (e.target.checked) state.unchecked[floor].delete(sr);
        else state.unchecked[floor].add(sr);
        saveUnchecked(state.token, floor, state.unchecked[floor]);
        refreshHeaderCheckbox();
        invalidateAnnotation();
    });

    // Header checkbox → check/uncheck all rows on active floor
    rowsSelAll.addEventListener("change", () => {
        const floor = state.activeFloor;
        if (!floor) return;
        const boxes = shownBoxes(resultsTbody, ".textsel");
        // Only the rows on screen: with a boundary type chosen or a column
        // filter set, the other rows keep whatever they had.
        const shown = boxes.map((cb) => Number(cb.dataset.sr));
        if (!state.unchecked[floor]) state.unchecked[floor] = new Set();
        if (rowsSelAll.checked) {
            boxes.forEach((cb) => { cb.checked = true; });
            shown.forEach((sr) => state.unchecked[floor].delete(sr));
        } else {
            boxes.forEach((cb) => { cb.checked = false; });
            shown.forEach((sr) => state.unchecked[floor].add(sr));
        }
        saveUnchecked(state.token, floor, state.unchecked[floor]);
        refreshHeaderCheckbox();
        invalidateAnnotation();
    });

    // Bulk: check all rows on active floor
    checkAllBtn.addEventListener("click", () => {
        rowsSelAll.checked = true;
        rowsSelAll.dispatchEvent(new Event("change"));
    });

    // Bulk: uncheck all rows on active floor
    uncheckAllBtn.addEventListener("click", () => {
        rowsSelAll.checked = false;
        rowsSelAll.dispatchEvent(new Event("change"));
    });

    function invalidateAnnotation() {
        setDxfDisabled(true, "Selections changed - click Generate Boundaries again");
        dlDxf.href = "#";
        if (window.DA_previewInvalid) window.DA_previewInvalid();
        setStatus(annotateStat, "", "info");
        // Status badges are no longer accurate — clear them so user re-Generates.
        state.statusByFloor = {};
        renderRows();
        const legend = document.querySelector("#statusLegend");
        if (legend) legend.style.display = "none";
    }

    // ─────────────────────────────────────────────────────────────────────
    // Generate Boundaries (Step 3 → annotated DXF)
    // ─────────────────────────────────────────────────────────────────────
    generateBtn.addEventListener("click", () => { runGenerate(); });

    /* Step 3's Generate Boundaries. True when the annotated DXF is written. */
    async function runGenerate() {
        if (!state.results) return false;

        // Build selections payload: per floor, the list of sr_nos that are CHECKED
        const selections = {};
        // Build families payload: per floor, the resolved family (override or
        // auto-suggested) for every sr_no — used to pick which AI_<FAMILY>
        // layer each drawn boundary lands on.
        const families = {};
        Object.keys(state.results).forEach((floor) => {
            const rows = state.results[floor];
            const unchecked = state.unchecked[floor] || new Set();
            selections[floor] = rows
                .filter((r) => !unchecked.has(r.sr_no))
                .map((r) => r.sr_no);

            // Only the families the user changed are sent: a family the
            // server treats as picked by hand is never refiled by the
            // rules that settle rooms and doors (a shaft's room, a lift on
            // a machine room), so sending every row's suggestion switched
            // those rules off for every labelled boundary.
            const overrides = state.family[floor] || {};
            const fmap = {};
            rows.forEach((r) => {
                const o = overrides[r.sr_no];
                if (o && o !== (r.family || "Unknown")) fmap[r.sr_no] = o;
            });
            families[floor] = fmap;
        });

        const familyFilter = boundaryFamilySel.value || "";

        setStatus(annotateStat,
            familyFilter
                ? `Generating ${familyFilter} boundaries...`
                : "Generating boundaries...",
            "busy");
        setBusy(generateBtn, true);
        setDxfDisabled(true, "Generating ...");

        try {
            const res = await fetch("/annotate", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    token: state.token, selections, families,
                    // The built-in rules unticked on the Built-in rules
                    // page this session; none unticked means all apply,
                    // which is the default and what a fresh page sends.
                    rules: window.DA_ruleChoices ? window.DA_ruleChoices() : {},
                    // Step 3 is text-based only: geometry lives in Step 5.
                    family_filter: familyFilter,
                }),
            });
            const data = await res.json();
            if (!data.ok) {
                setStatus(annotateStat, data.error || "Annotation failed.", "error");
                flowError(data);
                return false;
            }

            const s = data.stats || {};
            const xLabel = s.x_with_label || 0;
            const xUnknown = s.x_unknown || 0;
            const prim = s.primary_boundaries || 0;
            const sec = s.secondary_boundaries || 0;
            const merged = s.merged_overlapping || 0;
            const skipped = s.skipped_no_boundary || 0;

            // Capture per-floor per-sr_no status and re-render the active table.
            state.statusByFloor = s.text_status_by_floor || {};
            renderRows();

            // Show legend now that statuses are populated.
            const legend = document.querySelector("#statusLegend");
            if (legend) legend.style.display = "";

            // The per-label outcome used to be computed and thrown away.
            if (window.DA_renderQA) window.DA_renderQA(data);

            const scope = familyFilter
                ? `Exported ${data.boundaries_drawn} ${familyFilter} boundary(ies); ` +
                  `${data.filtered_out} of other types left out. `
                : "";
            const ruleNote = "";
            // What the validation pass found in the written boundaries.
            const v = data.validation || {};
            const flagged = (v.checks || []).filter((c) => c.count);
            const myNote = v.checks
                ? (flagged.length
                    ? " Validation: " + flagged.map((c) => `${c.count} ${c.check.toLowerCase()}`).join(", ") +
                      " - see the Validation sheet in Extraction QA (Excel)."
                    : " Validation: all checks passed.") +
                  (v.voids ? ` ${v.voids} void(s) drawn.` : "")
                : "";
            setStatus(annotateStat,
                `Done. ${scope}${data.kept} text(s) used, ${data.dropped} skipped` +
                (data.ignored ? `, ${data.ignored} ignored` : "") + ". " +
                `Found ${xLabel} X-shafts (+ ${xUnknown} Unknown), ` +
                `${prim} primary, ${sec} secondary; ${merged} merged, ${skipped} no boundary.` +
                ruleNote + myNote +
                (data.hint ? ` ${data.hint}` : ""),
                data.boundaries_drawn ? "ok" : "error");

            // Activate the DXF download
            dlDxf.href = data.download;
            setDxfDisabled(false, "Download the annotated DXF");
            // The rule preview measures against boundaries that exist.
            if (window.DA_myRulesToken && data.boundaries_drawn) {
                window.DA_myRulesToken(state.token);
            }
            if (window.DA_previewReady && data.boundaries_drawn) {
                window.DA_previewReady(state.token);
            }
            stageDone("annotate");
            return true;
        } catch (err) {
            setStatus(annotateStat, "Network error: " + err.message, "error");
            flowError({ error: "Network error: " + err.message });
            return false;
        } finally {
            setBusy(generateBtn, false);
        }
    }

    // (Disabled state handled via pointer-events:none in setDxfDisabled.)


    function escapeHtml(s) {
        return String(s == null ? "" : s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    // ─────────────────────────────────────────────────────────────────────
    // Section 4: Calculate areas from corrected DXF
    //   Independent of the upper sections — user can come here directly
    //   with any annotated DXF, in any session.
    // ─────────────────────────────────────────────────────────────────────
    const areasDrop = $("#areasDrop");
    const areasFile = $("#areasFile");
    const areasPickBtn = $("#areasPickBtn");
    const areasUploadSt = $("#areasUploadStatus");
    const areasLayerPick = $("#areasLayerPicker");
    const areasLayerSel = $("#areasFloorLayer");
    const areasCalcBtn = $("#areasCalcBtn");
    const areasCalcSt = $("#areasCalcStatus");
    const areasResults = $("#areasResults");
    const floorAreaTbody = $("#floorAreaTable tbody");
    const roomAreasTbody = $("#roomAreasTable tbody");
    const areasDlXlsx = $("#areasDlXlsx");
    const areasDlJson = $("#areasDlJson");

    let areasState = { token: null, layers: [] };

    areasPickBtn.addEventListener("click", () => areasFile.click());
    areasDrop.addEventListener("click", (e) => {
        if (e.target === areasPickBtn) return;
        areasFile.click();
    });
    ["dragenter", "dragover"].forEach((ev) =>
        areasDrop.addEventListener(ev, (e) => {
            e.preventDefault();
            areasDrop.classList.add("drag");
        })
    );
    ["dragleave", "drop"].forEach((ev) =>
        areasDrop.addEventListener(ev, (e) => {
            e.preventDefault();
            areasDrop.classList.remove("drag");
        })
    );
    const areasRunBtn = $("#areasRunBtn");
    const areasFileChosen = $("#areasFileChosen");
    let areasPending = null;

    function chooseCorrectedDxf(file) {
        if (!file.name.toLowerCase().endsWith(".dxf")) {
            setStatus(areasUploadSt, "Please select a .dxf file.", "error");
            return;
        }
        areasPending = file;
        areasFileChosen.textContent = describeFile(file);
        areasFileChosen.classList.add("is-set");
        areasDrop.classList.add("has-file");
        areasRunBtn.disabled = false;
        areasRunBtn.title = "Upload the corrected DXF and list its layers";
        setStatus(areasUploadSt, "Ready - click Run to upload the corrected DXF.", "info");
    }

    areasDrop.addEventListener("drop", (e) => {
        if (e.dataTransfer.files && e.dataTransfer.files[0]) {
            chooseCorrectedDxf(e.dataTransfer.files[0]);
        }
    });
    areasFile.addEventListener("change", () => {
        if (areasFile.files && areasFile.files[0]) {
            chooseCorrectedDxf(areasFile.files[0]);
        }
    });
    areasRunBtn.addEventListener("click", () => {
        if (areasPending) uploadCorrectedDxf(areasPending);
    });

    /* The area step's drawing is ready (uploaded, or taken from a generated
     * output): list its layers and preselect the floor outline layer. */
    function areasLoaded(data, what) {
        areasState.token = data.token;
        areasState.layers = data.layers || [];
        if (window.DA_showUnits) window.DA_showUnits(data.units);

        // Populate layer dropdown — try to default to the most likely floor layer
        areasLayerSel.innerHTML = "";
        const preferredOrder = ["A-FLOOR PROJ", "A-FLOOR", "A-FLOR", "FLOOR", "0"];
        let defaultIdx = -1;
        areasState.layers.forEach((L, i) => {
            const opt = document.createElement("option");
            opt.value = L; opt.textContent = L;
            areasLayerSel.appendChild(opt);
            if (data.floor_layer && L === data.floor_layer) defaultIdx = i;
        });
        if (defaultIdx < 0) {
            areasState.layers.forEach((L, i) => {
                if (defaultIdx >= 0) return;
                for (const pref of preferredOrder) {
                    if (L.toUpperCase().includes(pref.toUpperCase())) { defaultIdx = i; break; }
                }
            });
        }
        if (defaultIdx >= 0) areasLayerSel.selectedIndex = defaultIdx;

        setStatus(areasUploadSt,
            `${what} - found ${areasState.layers.length} layer(s) with closed polylines. ` +
            "Check the floor outline layer and press Calculate areas.", "ok");
        areasLayerPick.classList.remove("hidden");
    }

    /* Take the drawing straight from a generated output (Step 3 or 4). */
    async function areasFromOutput(source, btn) {
        if (!state.token) {
            setStatus(areasUploadSt, "Upload a drawing and extract its texts first (Steps 1-3).", "error");
            flowError({ error: "Upload a drawing and extract its texts first (Steps 1-3)." });
            return false;
        }
        const label = { text: "the text-based output (Step 3)", geometry: "the geometry output (Step 4)",
                        final: "the final drawing (Step 5)" }[source];
        setStatus(areasUploadSt, `Calculating the areas of ${label} on the floor plates picked in Step 2 ...`, "busy");
        if (btn) setBusy(btn, true);
        areasLayerPick.classList.add("hidden");
        areasResults.classList.add("hidden");
        try {
            const res = await fetch("/areas/from_output", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ token: state.token, source }),
            });
            const data = await res.json();
            if (!data.ok) {
                setStatus(areasUploadSt, data.error || "Could not take that drawing.", "error");
                flowError(data);
                return false;
            }
            // The floor plates come from Step 2: no layer to pick, straight to the result.
            areasState.token = data.token;
            if (window.DA_showUnits) window.DA_showUnits(data.units);
            renderAreas(data);
            if (window.DA_cadStep4) window.DA_cadStep4(areasState.token);
            setStatus(areasUploadSt, `Done: ${data.result.rooms.length} room boundaries from ${label}, on ` +
                      `${(data.floors || []).length} floor plate(s) picked in Step 2 (${(data.floors || []).join(", ")}).`, "ok");
            stageDone("areas", { source: label.replace(/^the /, ""), statement: !!(data.download && data.download.area_statement_xlsx) });
            return true;
        } catch (err) {
            setStatus(areasUploadSt, "Error: " + err.message, "error");
            flowError({ error: "Error: " + err.message });
            return false;
        } finally {
            if (btn) setBusy(btn, false);
        }
    }
    document.getElementById("areasFromText").addEventListener("click", function () { areasFromOutput("text", this); });
    document.getElementById("areasFromGeo").addEventListener("click", function () { areasFromOutput("geometry", this); });
    document.getElementById("areasFromFinal").addEventListener("click", function () { areasFromOutput("final", this); });

    /* What flow.js chains for Fast track and for Generate final outputs. */
    window.DA_flowApi = {
        checkPicks: (fast) => checkPicks(fast),
        showPickProblem: (msg) => setStatus(extractStat, msg, "error"),
        extract: () => runExtract({ fast: true }),
        generate: () => runGenerate(),
        areasFrom: (source) => areasFromOutput(source, document.getElementById("areasFromFinal")),
        projectName: () => projectNameValue(),
    };

    async function uploadCorrectedDxf(file) {
        if (!file.name.toLowerCase().endsWith(".dxf")) {
            setStatus(areasUploadSt, "Please select a .dxf file.", "error");
            return;
        }
        setStatus(areasUploadSt, `Uploading ${file.name} ...`, "busy");
        setBusy(areasPickBtn, true);
        setBusy(areasRunBtn, true);
        areasDrop.classList.add("is-busy");
        areasLayerPick.classList.add("hidden");
        areasResults.classList.add("hidden");

        const fd = new FormData();
        fd.append("file", file);
        if (window.DA_selectedUnits) fd.append("units", window.DA_selectedUnits("areas"));
        try {
            const data = await postFile("/areas/upload", fd, (done) => {
                setStatus(areasUploadSt, done < 1
                    ? `Uploading ${file.name} - ${Math.round(done * 100)}%`
                    : `Reading ${file.name} - listing layers with closed ` +
                      "polylines...", "busy");
            });
            if (!data.ok) {
                setStatus(areasUploadSt, data.error || "Upload failed.", "error");
                return;
            }
            areasLoaded(data, `Uploaded ${data.original_name}`);
        } catch (err) {
            setStatus(areasUploadSt, "Upload failed: " + err.message, "error");
        } finally {
            setBusy(areasPickBtn, false);
            setBusy(areasRunBtn, false);
            areasDrop.classList.remove("is-busy");
        }
    }

    areasCalcBtn.addEventListener("click", async () => {
        if (!areasState.token) return;
        const layer = areasLayerSel.value;
        if (!layer) {
            setStatus(areasCalcSt, "Please pick a floor outline layer.", "error");
            return;
        }
        setStatus(areasCalcSt, "Calculating areas...", "busy");
        setBusy(areasCalcBtn, true);
        areasResults.classList.add("hidden");

        try {
            const res = await fetch("/areas/calculate", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ token: areasState.token, floor_layer: layer }),
            });
            const data = await res.json();
            if (!data.ok) {
                setStatus(areasCalcSt, data.error || "Calculation failed.", "error");
                return;
            }
            renderAreas(data);
            if (window.DA_cadStep4) window.DA_cadStep4(areasState.token);
            setStatus(areasCalcSt,
                `Done. ${data.result.rooms.length} room boundaries processed.`, "ok");
            stageDone("areas", { source: "corrected DXF (layer " + layer + ")",
                                 statement: !!(data.download && data.download.area_statement_xlsx) });
        } catch (err) {
            setStatus(areasCalcSt, "Network error: " + err.message, "error");
        } finally {
            setBusy(areasCalcBtn, false);
        }
    });

    /* The Area Statement on the page: the workbook's towers, columns and
     * totals (worked out on the server, area_statement.statement_view). */
    function renderStatement(st) {
        const host = document.getElementById("areasStatementView");
        if (!host) return;
        if (!st || !st.towers || !st.towers.length) {
            host.innerHTML = '<div class="muted small">No area statement for this drawing.</div>';
            return;
        }
        const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
            ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
        const num = (v, k) => (v == null || v === "") ? "" : (k === "eff_sa" || k === "eff_pa")
            ? Number(v).toFixed(2)
            : Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const cols = st.columns;
        // group header: consecutive columns of one group share a cell
        let groups = "";
        for (let i = 0; i < cols.length;) {
            let j = i;
            while (j + 1 < cols.length && cols[j + 1].group === cols[i].group) j++;
            groups += `<th colspan="${j - i + 1}" class="${cols[i].group ? "ast-group" : "ast-nogroup"}">${esc(cols[i].group || "")}</th>`;
            i = j + 1;
        }
        const head = cols.map((c) => `<th class="ast-k-${c.key}">${esc(c.label).replace(/\n/g, "<br>")}</th>`).join("");
        const cell = (row, c) => {
            if (c.key === "sl") return `<td>${row.sl == null ? "" : row.sl}</td>`;
            if (c.key === "floor") return `<td class="ast-floor">${esc(row.floor)}</td>`;
            return `<td class="ast-num ast-k-${c.key}">${num(row[c.key], c.key)}</td>`;
        };
        let html = `<div class="ast-head"><b>Area Statement</b> <span class="muted small">- from ${esc(st.drawing || "the drawing")}` +
            ` - all areas in sq.m unless stated; sale area = plinth / ${st.sale_factor}, 1 sq.m = ${st.sqft} sq.ft</span></div>`;
        st.towers.forEach((t) => {
            html += `<div class="ast-tower">${esc(t.title)}</div><div class="table-wrap ast-wrap"><table class="grid ast-table" data-no-filter>` +
                `<thead><tr>${groups}</tr><tr>${head}</tr></thead><tbody>` +
                t.rows.map((r) => `<tr class="${r.refuge ? "ast-refuge" : ""}" title="${esc(r.plate || "")}">` +
                    cols.map((c) => cell(r, c)).join("") + "</tr>").join("") +
                `<tr class="ast-total">${cols.map((c) => cell(Object.assign({ sl: "" }, t.total), c)).join("")}</tr>` +
                `</tbody></table></div>` +
                `<div class="muted small ast-sft">Gross area: <b>${num(t.gross_sft)}</b> sq.ft</div>`;
        });
        html += '<div class="ast-tower">SUMMARY</div><div class="table-wrap"><table class="grid ast-table ast-summary" data-no-filter>' +
            "<thead><tr><th>S.No</th><th>Tower</th><th>GBUA (sq.m)</th><th>GBUA (sq.ft)</th><th>Sale area (sq.ft)</th></tr></thead><tbody>" +
            st.summary.map((s, i) => `<tr><td>${i + 1}</td><td>${esc(s.tower)}</td><td class="ast-num">${num(s.gbua_sqm)}</td>` +
                `<td class="ast-num">${num(s.gbua_sft)}</td><td class="ast-num">${num(s.sale_sft)}</td></tr>`).join("") +
            `<tr class="ast-total"><td></td><td>Total</td><td class="ast-num">${num(st.summary_total.gbua_sqm)}</td>` +
            `<td class="ast-num">${num(st.summary_total.gbua_sft)}</td><td class="ast-num">${num(st.summary_total.sale_sft)}</td></tr>` +
            "</tbody></table></div>";
        const assigned = (st.rooms || []).filter((x) => x.column);
        html += `<details class="geo-sec ast-rooms"><summary>Which room went to which column ` +
            `<span class="muted small">- ${assigned.length} of ${(st.rooms || []).length} rooms counted</span></summary>` +
            '<div class="table-wrap"><table class="grid"><thead><tr><th>Tower</th><th>Floors</th><th>Room</th><th>Family</th>' +
            "<th>Area (sq.m)</th><th>Column</th></tr></thead><tbody>" +
            (st.rooms || []).map((x) => `<tr><td>${esc(x.tower)}</td><td>${esc(x.level)}</td><td>${esc(x.name)}</td>` +
                `<td>${esc(x.family)}</td><td class="ast-num">${num(x.area_sqm)}</td><td>${esc(x.column || "- not counted")}</td></tr>`).join("") +
            "</tbody></table></div></details>";
        host.innerHTML = html;
    }
    document.getElementById("areasViewStatement").addEventListener("click", () => {
        const tab = Array.from(document.querySelectorAll('[data-subtabs="step4"] .subtab'))
            .find((b) => b.textContent.trim() === "Area statement");
        if (tab) tab.click();
        const pane = document.getElementById("areasStatementPane");
        if (pane) pane.scrollIntoView({ behavior: "smooth", block: "start" });
    });

    // Confidence: a pill per room (High / Medium / Low and the score), the
    // reasons on hover, and a summary that filters the table to one band.
    function confCell(room) {
        if (room.confidence == null) return '<span class="muted small">-</span>';
        const b = room.confidence_band || "";
        const why = (room.confidence_why || "").split("; ").join("\n");
        return `<span class="conf-pill conf-${b.toLowerCase()}" title="${escapeHtml(why)}">` +
            `${escapeHtml(b)} <span class="conf-score">${Math.round(room.confidence * 100)}%</span></span>`;
    }
    function renderConfSummary(rooms) {
        const box = document.getElementById("confSummary");
        if (!box) return;
        const n = { High: 0, Medium: 0, Low: 0 };
        rooms.forEach((r) => { if (n[r.confidence_band] != null) n[r.confidence_band] += 1; });
        const total = n.High + n.Medium + n.Low;
        if (!total) { box.hidden = true; return; }
        box.hidden = false;
        box.innerHTML = '<span class="conf-title">Confidence</span>' +
            ["All", "High", "Medium", "Low"].map((b) =>
                `<button type="button" class="conf-filter${b === "All" ? " active" : ""}" data-band="${b}">` +
                (b === "All" ? `All <b>${total}</b>` :
                    `<span class="conf-dot conf-${b.toLowerCase()}"></span>${b} <b>${n[b]}</b>`) + "</button>").join("") +
            '<span class="muted small conf-hint">Check the Low rows first; hover a score for the reasons.</span>';
        box.querySelectorAll(".conf-filter").forEach((btn) => btn.addEventListener("click", () => {
            box.querySelectorAll(".conf-filter").forEach((x) => x.classList.toggle("active", x === btn));
            const want = btn.dataset.band;
            roomAreasTbody.querySelectorAll("tr").forEach((tr) => {
                tr.style.display = (want === "All" || tr.dataset.band === want) ? "" : "none";
            });
        }));
    }

    function renderAreas(data) {
        renderStatement(data.statement);
        const r = data.result || {};

        // Floor Area rows - one per floor, plus a total when there are several
        floorAreaTbody.innerHTML = "";
        const fmt2 = (v) => (v == null) ? "—" :
            Number(v).toLocaleString(undefined, {
                minimumFractionDigits: 2, maximumFractionDigits: 2
            });
        const floors = (r.floors && r.floors.length) ? r.floors :
            [{ name: r.floor_label, area_sqm: r.floor_area_sqm,
               perimeter_m: r.floor_perimeter_m, room_count: null }];
        floors.forEach((f) => {
            const tr = document.createElement("tr");
            const rooms = (f.room_count == null) ? "" :
                ` <span class="muted small">(${f.room_count} rooms)</span>`;
            tr.innerHTML = `
      <td><b>${escapeHtml(f.name || "—")}</b>${rooms}</td>
      <td>${fmt2(f.area_sqm)}</td>
      <td>${fmt2(f.perimeter_m)}</td>`;
            floorAreaTbody.appendChild(tr);
        });
        if (floors.length > 1) {
            const tr = document.createElement("tr");
            tr.innerHTML = `
      <td><b>Total (${floors.length} floors, layer ${escapeHtml(r.floor_label || "—")})</b></td>
      <td><b>${fmt2(r.floor_area_sqm)}</b></td>
      <td><b>${fmt2(r.floor_perimeter_m)}</b></td>`;
            floorAreaTbody.appendChild(tr);
        }

        // Room Areas rows
        roomAreasTbody.innerHTML = "";
        const rooms = r.rooms || [];
        if (!rooms.length) {
            roomAreasTbody.innerHTML =
                `<tr><td colspan="7" class="muted" style="text-align:center;padding:20px">
          No closed polylines found on a room layer (AI_&lt;FAMILY&gt;, or the older *_BOUNDARY).
        </td></tr>`;
        } else {
            const frag = document.createDocumentFragment();
            rooms.forEach((room) => {
                const r2 = document.createElement("tr");
                const areaStr = Number(room.area_sqm).toLocaleString(undefined, {
                    minimumFractionDigits: 2, maximumFractionDigits: 2
                });
                const perimStr = (room.perimeter_m == null) ? "—" :
                    Number(room.perimeter_m).toLocaleString(undefined, {
                        minimumFractionDigits: 2, maximumFractionDigits: 2
                    });
                r2.innerHTML = `
          <td>${room.sr_no}</td>
          <td>${escapeHtml(room.name)}</td>
          <td>${areaStr}</td>
          <td>${perimStr}</td>
          <td>${escapeHtml(room.family || "Unknown")}</td>
          <td>${escapeHtml(room.floor || "")}</td>
          <td>${confCell(room)}</td>`;
                r2.dataset.band = room.confidence_band || "";
                frag.appendChild(r2);
            });
            roomAreasTbody.appendChild(frag);
        }
        renderConfSummary(rooms);

        // By-Family grouped view
        const groupsContainer = document.getElementById("familyGroups");
        groupsContainer.innerHTML = "";
        const fams = r.families || [];
        if (!fams.length) {
            groupsContainer.innerHTML =
                '<p class="muted small" style="text-align:center;padding:14px">' +
                'No rooms to group.</p>';
        } else {
            let grandTotal = 0, grandCount = 0;
            // Several floors: each floor gets its own heading with its own
            // families underneath. One floor keeps the plain list.
            const multi = (r.floors || []).length > 1;
            const sections = multi ?
                r.floors.map((f) => ({ floor: f, families: f.families || [] })) :
                [{ floor: null, families: fams }];
            sections.forEach((sec) => {
            if (sec.floor) {
                const f = sec.floor;
                const fTotal = (sec.families || []).reduce(
                    (a, x) => a + (Number(x.total_sqm) || 0), 0);
                const head = document.createElement("div");
                head.style.cssText = "margin:22px 0 10px; padding:12px 16px; background:#1f3a5f; color:#fff; border-radius:8px; display:flex; justify-content:space-between; font-weight:700; font-size:16px";
                head.innerHTML = `
        <span>${escapeHtml(f.name)} &nbsp;<span style="font-weight:400;font-size:13px">plate ${fmt2(f.area_sqm)} sq.m &middot; ${f.room_count} rooms in ${sec.families.length} famil${sec.families.length === 1 ? 'y' : 'ies'}</span></span>
        <span>${fmt2(fTotal)} sq.m</span>`;
                groupsContainer.appendChild(head);
                if (!sec.families.length) {
                    const none = document.createElement("p");
                    none.className = "muted small";
                    none.textContent = "No rooms on this floor.";
                    groupsContainer.appendChild(none);
                }
            }
            sec.families.forEach((fam) => {
                grandTotal += Number(fam.total_sqm) || 0;
                grandCount += fam.count || 0;
                const totalStr = Number(fam.total_sqm).toLocaleString(undefined, {
                    minimumFractionDigits: 2, maximumFractionDigits: 2
                });
                const itemPlural = (fam.count === 1) ? "item" : "items";
                // One collapsible section per family: the header carries the
                // count and the total, the room list opens on a click.
                const block = document.createElement("details");
                block.className = "fam-block";
                let rowsHtml = "";
                fam.rooms.forEach((room, idx) => {
                    const aStr = Number(room.area_sqm).toLocaleString(undefined, {
                        minimumFractionDigits: 2, maximumFractionDigits: 2
                    });
                    rowsHtml += `<tr>
            <td style="width:70px">${idx + 1}</td>
            <td>${escapeHtml(room.name)}</td>
            <td style="width:160px">${escapeHtml(room.floor || "")}</td>
            <td style="width:160px">${aStr}</td></tr>`;
                });
                block.innerHTML = `
          <summary class="fam-head">
            <div style="font-weight:700; color:#1f3a5f; font-size:15px">
              ${escapeHtml(fam.name)}
              <span style="font-weight:400; font-size:13px; color:#465a7a">
                &nbsp;(${fam.count} ${itemPlural})
              </span>
            </div>
            <div style="font-weight:700; color:#1f3a5f">
              ${totalStr} sq.m
            </div>
          </summary>
          <div class="table-wrap" style="margin:0; border-radius:0">
          <table class="grid" style="margin:0; border-radius:0">
            <thead>
              <tr>
                <th style="width:70px">Sr.No</th>
                <th>Room Name</th>
                <th style="width:160px">Floor</th>
                <th style="width:160px">Area (sq.m)</th>
              </tr>
            </thead>
            <tbody>${rowsHtml}</tbody>
          </table></div>`;
                groupsContainer.appendChild(block);
            });
            });
            // Grand total summary
            const gt = document.createElement("div");
            gt.style.cssText = "margin-top:8px; padding:12px 16px; background:#f2f5fa; border:1px solid #cbd5e1; border-radius:8px; display:flex; justify-content:space-between; font-weight:700";
            gt.innerHTML = `
        <span>Grand Total (${grandCount} rooms across ${fams.length} famil${fams.length === 1 ? 'y' : 'ies'}${multi ? ", " + r.floors.length + " floors" : ""})</span>
        <span>${grandTotal.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} sq.m</span>`;
            groupsContainer.appendChild(gt);
        }

        // Wire downloads
        if (data.download) {
            areasDlXlsx.href = data.download.xlsx || "#";
            areasDlJson.href = data.download.json || "#";
            const st = document.getElementById("areasDlStatement");
            if (st) {
                const url = data.download.area_statement_xlsx;
                // The Step 1 project name, read at click time so an edit
                // made after Calculate still counts. Left empty, the server
                // uses the name the annotated DXF carries, if any.
                st.dataset.baseUrl = url || "";
                st.href = url || "#";
                if (!st.dataset.wired) {
                    st.dataset.wired = "1";
                    st.addEventListener("click", () => {
                        const base = st.dataset.baseUrl || "";
                        const name = projectNameValue();
                        if (base) {
                            st.href = base + (name ? "?project=" +
                                encodeURIComponent(name) : "");
                        }
                    });
                }
                st.classList.toggle("disabled", !url);
                st.style.pointerEvents = url ? "" : "none";
                st.style.opacity = url ? "" : "0.55";
                st.title = url ? "Tower-wise area statement in the client's reference layout"
                               : "The area statement could not be written - see the server log.";
            }
        }
        if (window.DA_showUnits) window.DA_showUnits(data.units);
        if (window.DA_renderQuantities) window.DA_renderQuantities(data);
        areasResults.classList.remove("hidden");
        areasResults.scrollIntoView({ behavior: "smooth", block: "start" });
    }
})();