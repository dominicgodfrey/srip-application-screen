/* Screen 3 — cohort what-if. Two sources, one render path:
   - an active job:        POST /jobs/{id}/cohorts?honors=&intensive=&regular=
   - a saved decisions.jsonl: POST /cohorts (multipart) + same params
   Capacity inputs recompute live (debounced). CSV export uses the same call with format=csv. */
(function () {
  "use strict";

  const S = window.SRIP;
  const DEBOUNCE_MS = 300;
  const app = document.getElementById("cohort-app");

  const els = {
    capHonors: document.getElementById("cap-honors"),
    capIntensive: document.getElementById("cap-intensive"),
    capRegular: document.getElementById("cap-regular"),
    sourceNote: document.getElementById("cohort-source-note"),
    reuploadForm: document.getElementById("reupload-form"),
    decisionsFile: document.getElementById("decisions-file"),
    results: document.getElementById("cohort-results"),
    warnings: document.getElementById("cohort-warnings"),
    counts: document.getElementById("cohort-counts"),
    tierBody: document.querySelector("#tier-table tbody"),
    satisfaction: document.getElementById("choice-satisfaction"),
    csvBtn: document.getElementById("download-cohort-csv"),
    downloadsRow: document.getElementById("cohort-downloads"),
    tierFilter: document.getElementById("assign-tier-filter"),
    assignBody: document.querySelector("#assign-table tbody"),
    waitlistSection: document.getElementById("waitlist-section"),
    waitlistBody: document.querySelector("#waitlist-table tbody"),
    unassignableSection: document.getElementById("unassignable-section"),
    unassignableBody: document.querySelector("#unassignable-table tbody"),
  };

  // Source state: a job id, or an uploaded decisions.jsonl File. Job takes effect until a file
  // is chosen; choosing a file switches the source to the upload.
  const jobId = (app.dataset.job || "").trim() || S.getJobId();
  let sourceFile = null;
  let debounceTimer = null;

  function show(el) { el.classList.remove("hidden"); }
  function hide(el) { el.classList.add("hidden"); }

  function capacityParams(format, tier) {
    const params = new URLSearchParams();
    const caps = [["honors", els.capHonors], ["intensive", els.capIntensive], ["regular", els.capRegular]];
    for (const [name, input] of caps) {
      const v = input.value.trim();
      if (v !== "" && Number(v) >= 0) params.set(name, String(Math.floor(Number(v))));
    }
    if (format) params.set("format", format);
    if (tier) params.set("tier", tier);
    return params;
  }

  async function compute(format, tier) {
    const params = capacityParams(format, tier);
    if (sourceFile) {
      const fd = new FormData();
      fd.append("file", sourceFile, sourceFile.name || "decisions.jsonl");
      return S.api("/cohorts?" + params.toString(), { method: "POST", body: fd });
    }
    if (jobId) {
      return S.api("/jobs/" + encodeURIComponent(jobId) + "/cohorts?" + params.toString(),
        { method: "POST" });
    }
    // v3 default: the LIVE ranking straight from the database (recomputed per call).
    return S.api("/api/cohorts?" + params.toString(), { method: "POST" });
  }

  async function recompute() {
    try {
      const res = await compute("");
      render(await res.json());
    } catch (err) {
      const msg = err.status === 409 ? "Results are not ready yet — grading is still running."
        : err.status === 404 ? "This job has expired or was discarded. Re-upload a saved decisions.jsonl below."
        : (err.detail || "Could not compute the assignment.");
      hide(els.results);
      if (err.status !== 0) S.toast(msg, "danger");
      els.sourceNote.textContent = msg;
    }
  }

  function scheduleRecompute() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(recompute, DEBOUNCE_MS);
  }

  [els.capHonors, els.capIntensive, els.capRegular].forEach((input) =>
    input.addEventListener("input", scheduleRecompute));

  // ----- decisions.jsonl re-upload (the durable entry point) --------------------
  els.reuploadForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const file = els.decisionsFile.files[0];
    if (!file) { S.toast("Choose a decisions.jsonl file first.", "danger"); return; }
    sourceFile = file;
    els.sourceNote.textContent = "Source: " + file.name + " (re-uploaded decisions.jsonl)";
    recompute();
  });

  // ----- CSV export --------------------------------------------------------------
  async function downloadCsv(tier, filename) {
    try {
      const res = await compute("csv", tier);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      S.toast(err.detail || "CSV download failed.", "danger");
    }
  }

  els.csvBtn.addEventListener("click", () => downloadCsv("", "cohort_assignments.csv"));

  // One roster button per tier (rank, name, email, phone for the assigned members).
  function renderRosterButtons(tiers) {
    els.downloadsRow.querySelectorAll(".roster-btn").forEach((b) => b.remove());
    Object.keys(tiers).forEach((tier) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-secondary roster-btn";
      btn.textContent = "Download " + tier + " roster CSV";
      btn.addEventListener("click", () => downloadCsv(tier, "cohort_" + tier + ".csv"));
      els.downloadsRow.appendChild(btn);
    });
  }

  // ----- Render --------------------------------------------------------------------
  function stat(value, label, cls) {
    return '<div class="stat ' + (cls || "") + '"><div class="stat-num">' +
      (value === undefined ? "—" : S.esc(value)) +
      '</div><div class="stat-label">' + S.esc(label) + "</div></div>";
  }

  function rowsFor(list, withTier) {
    return list.map((a) =>
      "<tr>" +
        '<td class="num">' + (a.rank === null || a.rank === undefined ? "—" : S.esc(a.rank)) + "</td>" +
        "<td>" + S.esc(a.name) + "</td>" +
        '<td class="num">' + S.fmtNum(a.final_score) + "</td>" +
        (withTier
          ? "<td>" + S.badge(a.status) + "</td><td>" + S.esc(a.assigned_tier || "—") +
            '</td><td class="num">' + (a.choice_number || "—") + "</td>"
          : "<td>" + S.esc((a.choices || []).join(" → ")) + "</td>") +
        '<td class="small">' + S.esc(a.reason) +
          (a.excluded_by_cost && a.excluded_by_cost.length
            ? ' <span class="muted">(cost-excluded: ' + S.esc(a.excluded_by_cost.join(", ")) + ")</span>"
            : "") +
        "</td>" +
      "</tr>").join("");
  }

  let lastResult = null; // kept so the tier filter re-renders without a server round-trip

  function renderAssignments(result) {
    const filter = els.tierFilter.value;
    const all = result.assignments || [];
    const shown = filter ? all.filter((a) => a.assigned_tier === filter) : all;
    els.assignBody.innerHTML = rowsFor(shown, true);
  }

  els.tierFilter.addEventListener("change", () => {
    if (lastResult) renderAssignments(lastResult);
  });

  function syncTierFilter(tiers) {
    const current = els.tierFilter.value;
    const names = Object.keys(tiers);
    els.tierFilter.innerHTML = '<option value="">All programs</option>' +
      names.map((t) => '<option value="' + S.esc(t) + '">' + S.esc(t) + "</option>").join("");
    if (names.indexOf(current) !== -1) els.tierFilter.value = current;
  }

  function render(result) {
    lastResult = result;
    const sum = result.summary || {};
    syncTierFilter(sum.tiers || {});
    renderRosterButtons(sum.tiers || {});

    // The core's warnings[] already covers NEEDS_REVIEW exclusions — render them verbatim.
    els.warnings.innerHTML = (sum.warnings || []).map((w) =>
      '<div class="alert alert-warning">' + S.esc(w) + "</div>").join("");

    els.counts.innerHTML =
      stat(sum.total_ranked, "Ranked input", "") +
      stat(sum.assigned, "Assigned", "ranked") +
      stat(sum.waitlisted, "Waitlisted", "review") +
      stat(sum.unassignable, "Unassignable", "rejected");

    const tiers = sum.tiers || {};
    els.tierBody.innerHTML = Object.entries(tiers).map(([name, t]) =>
      "<tr><td>" + S.esc(name) + "</td>" +
      '<td class="num">' + (t.capacity === null || t.capacity === undefined ? "∞" : S.esc(t.capacity)) + "</td>" +
      '<td class="num">' + S.esc(t.filled) + "</td>" +
      '<td class="num">' + (t.open_seats === null || t.open_seats === undefined ? "∞" : S.esc(t.open_seats)) + "</td>" +
      '<td class="num">' + S.esc(t.first_choice_demand) + "</td></tr>").join("");

    // Keys are choice_1 / choice_2 / choice_3 (cohort.py emits choice_<n>).
    const cs = sum.choice_satisfaction || {};
    const parts = Object.entries(cs).map(([k, n]) =>
      "#" + k.replace("choice_", "") + ": " + n);
    els.satisfaction.textContent = parts.length
      ? "Assigned choice satisfaction — " + parts.join(", ")
      : "";

    renderAssignments(result);

    const waitlist = result.waitlist || [];
    els.waitlistBody.innerHTML = rowsFor(waitlist, false);
    waitlist.length ? show(els.waitlistSection) : hide(els.waitlistSection);

    const unassignable = result.unassignable || [];
    els.unassignableBody.innerHTML = rowsFor(unassignable, false);
    unassignable.length ? show(els.unassignableSection) : hide(els.unassignableSection);

    show(els.results);
  }

  // ----- Initial load ---------------------------------------------------------------
  if (jobId) {
    els.sourceNote.textContent = "Source: results from the current grading run.";
    // Name the uploaded CSV so it's unambiguous which file these results came from.
    S.api("/jobs/" + encodeURIComponent(jobId)).then((res) => res.json()).then((job) => {
      if (!sourceFile && job.filename) {
        els.sourceNote.textContent =
          "Source: results from the current grading run of “" + job.filename + "”.";
      }
    }).catch(() => {});
    recompute();
  } else {
    // v3 default: live database ranking (always current as applications arrive).
    els.sourceNote.textContent = "Source: the live ranking from the database.";
    recompute();
  }
})();
