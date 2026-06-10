/* Screen 2 — audit-record browser. Fetches decisions.jsonl once (non-evicting), parses the
   NDJSON client-side, and renders a sortable/filterable table with a full-record detail panel.
   No applicant data ever renders server-side. */
(function () {
  "use strict";

  const S = window.SRIP;
  const app = document.getElementById("audit-app");
  const els = {
    empty: document.getElementById("audit-empty"),
    list: document.getElementById("audit-list"),
    search: document.getElementById("audit-search"),
    outcome: document.getElementById("audit-outcome"),
    count: document.getElementById("audit-count"),
    thead: document.querySelector("#audit-table thead"),
    tbody: document.querySelector("#audit-table tbody"),
    detail: document.getElementById("audit-detail"),
    detailTitle: document.getElementById("detail-title"),
    detailBody: document.getElementById("detail-body"),
  };

  let records = [];
  let view = [];
  let sortKey = "rank";
  let sortAsc = true;
  let selectedId = null;

  function show(el) { el.classList.remove("hidden"); }
  function hide(el) { el.classList.add("hidden"); }

  // ----- Load -----------------------------------------------------------------
  const jobId = (app.dataset.job || "").trim() || S.getJobId();
  if (jobId) load(jobId);

  async function load(id) {
    try {
      const res = await S.api("/jobs/" + encodeURIComponent(id) + "/results/decisions");
      const text = await res.text();
      records = text.split("\n").filter(Boolean).map((line) => JSON.parse(line));
      hide(els.empty);
      show(els.list);
      applyView();
    } catch (err) {
      const msg = err.status === 409 ? "Results are not ready yet — grading is still running."
        : err.status === 404 ? "This job has expired or was discarded. Upload the CSV again."
        : (err.detail || "Could not load records.");
      els.empty.querySelector("p").textContent = msg;
      S.toast(msg, "danger");
    }
  }

  // ----- Filter + sort + render -----------------------------------------------
  function applyView() {
    const q = (els.search.value || "").toLowerCase();
    const outcome = els.outcome.value;
    view = records.filter((r) => {
      if (outcome && r.outcome !== outcome) return false;
      if (!q) return true;
      return (r.name || "").toLowerCase().includes(q) ||
             (r.submission_id || "").toLowerCase().includes(q);
    });

    const dir = sortAsc ? 1 : -1;
    const numeric = sortKey === "rank" || sortKey === "final_score";
    view.sort((a, b) => {
      let av = a[sortKey]; let bv = b[sortKey];
      if (numeric) {
        // Nulls (REJECTED/NEEDS_REVIEW have no rank/score) always sink to the bottom.
        const an = av === null || av === undefined; const bn = bv === null || bv === undefined;
        if (an && bn) return 0;
        if (an) return 1;
        if (bn) return -1;
        return (av - bv) * dir;
      }
      return String(av || "").localeCompare(String(bv || "")) * dir;
    });

    els.count.textContent = view.length + " of " + records.length + " applicants";
    els.tbody.innerHTML = view.map((r) =>
      '<tr class="clickable' + (r.submission_id === selectedId ? " selected" : "") +
      '" data-id="' + S.esc(r.submission_id) + '">' +
        '<td class="num">' + (r.rank === null || r.rank === undefined ? "—" : S.esc(r.rank)) + "</td>" +
        "<td>" + S.esc(r.name) + "</td>" +
        "<td>" + S.badge(r.outcome) + "</td>" +
        '<td class="num">' + S.fmtNum(r.final_score) + "</td>" +
        '<td class="small muted">' + S.esc(r.submission_id) + "</td>" +
        '<td class="small">' + S.esc(r.primary_reason) + "</td>" +
      "</tr>").join("");
  }

  els.search.addEventListener("input", applyView);
  els.outcome.addEventListener("change", applyView);

  els.thead.addEventListener("click", (ev) => {
    const th = ev.target.closest("th");
    if (!th || !th.dataset.sort) return;
    if (sortKey === th.dataset.sort) sortAsc = !sortAsc;
    else { sortKey = th.dataset.sort; sortAsc = true; }
    applyView();
  });

  els.tbody.addEventListener("click", (ev) => {
    const tr = ev.target.closest("tr[data-id]");
    if (!tr) return;
    selectedId = tr.dataset.id;
    const record = records.find((r) => r.submission_id === selectedId);
    if (record) renderDetail(record);
    applyView();
  });

  // ----- Detail panel ----------------------------------------------------------
  function kv(pairs) {
    return '<dl class="kv">' + pairs.map(([k, v]) =>
      "<dt>" + S.esc(k) + "</dt><dd>" + (v === "" || v === null || v === undefined ? "—" : v) + "</dd>"
    ).join("") + "</dl>";
  }

  function renderDetail(r) {
    els.detailTitle.textContent = r.name + " — " + r.outcome +
      (r.rank ? " (rank " + r.rank + ")" : "");

    const g = r.gates || {};
    const len = g.essay_length || {};
    const rel = g.essay_relevance || {};
    const gpa = r.gpa || {};
    const sc = r.scores || {};
    const essay = sc.essay || {};
    const school = r.school_match || {};
    const choices = r.program_choices || {};
    const dedup = r.dedup || {};

    const gatesHtml =
      '<h3 class="subhead">Gates</h3>' + kv([
        ["Essay 1 length", S.esc(len.e1_wc) + " words — " + S.bool(len.e1_ok)],
        ["Essay 2 length", S.esc(len.e2_wc) + " words — " + S.bool(len.e2_ok)],
        ["Length hard fail", S.bool(len.hard_fail, false)],
        ["Profanity", S.bool((g.profanity || {}).hit, false)],
        ["Gibberish", S.bool((g.gibberish || {}).hit, false)],
        ["GPA gate", S.bool((g.gpa_gate || {}).passed) +
          ((g.gpa_gate || {}).reason ? " — " + S.esc(g.gpa_gate.reason) : "")],
        ["Essay 1 on-topic", rel.e1_on_topic === null ? "—" : S.bool(rel.e1_on_topic)],
        ["Essay 2 on-topic", rel.e2_on_topic === null ? "—" : S.bool(rel.e2_on_topic)],
      ]);

    let gpaPairs = [
      ["Raw value", S.esc(gpa.raw)],
      ["Normalized", S.fmtNum(gpa.normalized_gpa, 2)],
      ["Scale", S.esc(gpa.original_scale)],
      ["Method", S.esc(gpa.conversion_method)],
      ["Confidence", S.esc(gpa.confidence)],
      ["Below 3.0", gpa.below_threshold === null ? "—" : S.bool(gpa.below_threshold, false)],
      ["Source", S.esc(gpa.source)],
    ];
    if (gpa.explanation_eval) {
      const e = gpa.explanation_eval;
      gpaPairs = gpaPairs.concat([
        ["Explanation adequate", S.bool(e.explanation_adequate)],
        ["Reason strength", S.fmtNum(e.strength_of_reason, 2)],
        ["Recommendation", S.esc(e.recommended_outcome)],
        ["Eval rationale", S.esc(e.rationale)],
      ]);
    }
    const gpaHtml = '<h3 class="subhead">GPA</h3>' + kv(gpaPairs);

    const scoresHtml =
      '<h3 class="subhead">Scores</h3>' + kv([
        ["GPA points", S.fmtNum(sc.gpa_points)],
        ["Essay 1", S.fmtNum(essay.e1)],
        ["Essay 2", S.fmtNum(essay.e2)],
        ["Essay total", S.fmtNum(essay.total)],
        ["Coursework bonus", S.fmtNum(sc.coursework_bonus)],
        ["School bonus", S.fmtNum(sc.school_bonus)],
        ["Resume bonus", S.fmtNum(sc.resume_bonus)],
        ["Final score", "<strong>" + S.fmtNum(r.final_score) + "</strong>"],
      ]);

    const schoolHtml =
      '<h3 class="subhead">School match</h3>' + kv([
        ["Matched", S.esc(school.matched_name)],
        ["List", S.esc(school.list)],
        ["Fuzzy score", S.fmtNum(school.fuzzy_score, 0)],
      ]);

    const courseworkHtml = renderCoursework(r.coursework_breakdown || []);

    const metaHtml =
      '<h3 class="subhead">Application</h3>' + kv([
        ["Submission", S.esc(r.submission_id)],
        ["Email", S.esc(r.email)],
        ["Choices", S.esc([choices.first, choices.second, choices.third].filter(Boolean).join(" → "))],
        ["Duplicate email", S.bool(dedup.is_duplicate_email, false)],
        ["Duplicate name", S.bool(dedup.is_duplicate_name, false)],
        ["Decided at", S.esc(r.decided_at_stage)],
        ["Primary reason", S.esc(r.primary_reason)],
      ]);

    const listsHtml =
      '<h3 class="subhead">Audit trail</h3>' +
      (r.reasons || []).map((x) => '<div class="chip">' + S.esc(x) + "</div>").join("") +
      '<h3 class="subhead">LLM calls</h3>' +
      ((r.llm_calls || []).length
        ? r.llm_calls.map((x) => '<div class="chip">' + S.esc(x) + "</div>").join("")
        : '<span class="muted small">none (rejected before any LLM stage)</span>') +
      ((r.errors || []).length
        ? '<h3 class="subhead">Errors</h3>' +
          r.errors.map((x) => '<div class="chip">' + S.esc(x) + "</div>").join("")
        : "");

    els.detailBody.innerHTML =
      '<div class="detail-grid">' +
        "<div>" + metaHtml + gatesHtml + "</div>" +
        "<div>" + gpaHtml + scoresHtml + schoolHtml + "</div>" +
      "</div>" + courseworkHtml + listsHtml;
    show(els.detail);
    els.detail.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function renderCoursework(courses) {
    if (!courses.length) return "";
    return '<h3 class="subhead">Coursework breakdown</h3>' +
      '<div class="table-wrap"><table class="data"><thead><tr>' +
      '<th class="no-sort">Course</th><th class="no-sort">Grade</th>' +
      '<th class="no-sort num">%</th><th class="no-sort">Category</th>' +
      '<th class="no-sort">Counts</th></tr></thead><tbody>' +
      courses.map((c) =>
        "<tr><td>" + S.esc(c.name) + "</td><td>" + S.esc(c.grade_raw) +
        '</td><td class="num">' + S.esc(c.grade_pct) + "</td><td>" + S.esc(c.category) +
        "</td><td>" + S.bool(c.counts) + "</td></tr>").join("") +
      "</tbody></table></div>";
  }
})();
