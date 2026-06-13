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
    const numeric = sortKey === "rank" || sortKey === "final_score" || sortKey === "gpa";
    view.sort((a, b) => {
      let av = sortKey === "gpa" ? gpaSortVal(a) : a[sortKey];
      let bv = sortKey === "gpa" ? gpaSortVal(b) : b[sortKey];
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
        '<td class="num">' + gpaCell(r) + "</td>" +
        '<td class="small muted">' + S.esc(r.submission_id) + "</td>" +
        '<td class="small">' + S.esc(r.primary_reason) + "</td>" +
      "</tr>").join("");
  }

  // GPA shown for every candidate (esp. NEEDS_REVIEW): the normalized 4.0-scale value when the
  // pipeline resolved one, otherwise the raw cell verbatim so the reviewer still sees what the
  // applicant entered.
  function gpaSortVal(r) {
    const gpa = r.gpa || {};
    if (typeof gpa.normalized_gpa === "number") return gpa.normalized_gpa;
    const parsed = parseFloat(gpa.raw);
    return Number.isNaN(parsed) ? null : parsed;
  }

  function gpaCell(r) {
    const gpa = r.gpa || {};
    if (typeof gpa.normalized_gpa === "number") return S.fmtNum(gpa.normalized_gpa, 2);
    const raw = (gpa.raw || "").trim();
    if (!raw) return "—";
    const shown = raw.length > 14 ? raw.slice(0, 13) + "…" : raw;
    return '<span class="muted" title="Raw value (not normalized): ' + S.esc(raw) + '">' +
      S.esc(shown) + "</span>";
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
  // Internal pipeline stage ids -> plain-language labels for reviewers.
  var STAGE_LABELS = {
    stage1: "essay quality checks",
    stage2: "GPA normalization",
    stage3: "GPA gate",
    stage4: "essay grading",
    stage8: "final scoring & ranking",
    manual_override: "manual override",
  };
  function stageLabel(stage) { return STAGE_LABELS[stage] || stage; }

  function kv(pairs) {
    return '<dl class="kv">' + pairs.map(([k, v]) =>
      "<dt>" + S.esc(k) + "</dt><dd>" + (v === "" || v === null || v === undefined ? "—" : v) + "</dd>"
    ).join("") + "</dl>";
  }

  function renderDetail(r) {
    els.detailTitle.textContent = r.name + " — " + r.outcome +
      (r.rank ? " (rank " + r.rank + ")" : "") +
      (r.manual_override ? " — manual override" : "");

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
      ["Below 3.3", gpa.below_threshold === null ? "—" : S.bool(gpa.below_threshold, false)],
      ["Source", S.esc(gpa.source)],
    ];
    if (gpa.explanation_text) {
      gpaPairs.push(["Applicant's explanation", S.esc(gpa.explanation_text)]);
    }
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
    const resumeHtml = renderResume(r.resume || {});

    const metaPairs = [
      ["Submission", S.esc(r.submission_id)],
      ["Email", S.esc(r.email)],
      ["Choices", S.esc([choices.first, choices.second, choices.third].filter(Boolean).join(" → "))],
      ["Duplicate email", S.bool(dedup.is_duplicate_email, false)],
      ["Duplicate name", S.bool(dedup.is_duplicate_name, false)],
      ["Decided at", S.esc(stageLabel(r.decided_at_stage))],
      ["Primary reason", S.esc(r.primary_reason)],
    ];
    if (r.manual_override) {
      const overrideNote = r.outcome === "REJECTED"
        ? "yes — removed from the ranking by a reviewer"
        : "yes — promoted by a reviewer";
      metaPairs.push(["Manual override", '<span class="flag-bad">' + overrideNote + "</span>"]);
    }
    const metaHtml = '<h3 class="subhead">Application</h3>' + kv(metaPairs);

    const listsHtml =
      '<h3 class="subhead">Audit trail</h3>' +
      (r.reasons || []).map((x) => '<div class="chip">' + S.esc(x) + "</div>").join("") +
      '<h3 class="subhead">LLM calls</h3>' +
      ((r.llm_calls || []).length
        ? r.llm_calls.map(llmChip).join("")
        : '<span class="muted small">none (rejected before any LLM stage)</span>') +
      ((r.errors || []).length
        ? '<h3 class="subhead">Errors</h3>' +
          r.errors.map((x) => '<div class="chip">' + S.esc(x) + "</div>").join("")
        : "");

    els.detailBody.innerHTML =
      '<div class="detail-grid">' +
        "<div>" + metaHtml + gatesHtml + "</div>" +
        "<div>" + gpaHtml + scoresHtml + schoolHtml + resumeHtml + "</div>" +
      "</div>" + renderEssays(r) + courseworkHtml + listsHtml + promoteHtml(r);
    show(els.detail);
    els.detail.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ----- Essays (collapsible; auto-open + highlight on profanity/gibberish rejections) ----------

  function escapeRegex(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

  // Wrap the offending profanity tokens in <mark>. Runs on already-HTML-escaped text; the
  // terms are plain word tokens, unaffected by escaping.
  function highlightTerms(escaped, terms) {
    let out = escaped;
    terms.forEach((term) => {
      const re = new RegExp("(^|[^\\w'-])(" + escapeRegex(S.esc(term)) + ")(?=$|[^\\w'-])", "gi");
      out = out.replace(re, '$1<mark class="hl-bad">$2</mark>');
    });
    return out;
  }

  // Mark the spans the deterministic gibberish signals point at: long identical-character runs
  // and long consonant runs. Entropy/unique-ratio signals have no single span — the essay is
  // simply opened so the auditor can read it.
  function highlightGibberish(escaped) {
    return escaped
      .replace(/([^\s&])\1{4,}/g, '<mark class="hl-bad">$&</mark>')
      .replace(/[bcdfghjklmnpqrstvwxz]{8,}/gi, '<mark class="hl-bad">$&</mark>');
  }

  function renderEssays(r) {
    const essays = r.essays;
    if (!essays || (essays.e1 === undefined && essays.e2 === undefined)) return "";
    const g = r.gates || {};
    const profTerms = ((g.profanity || {}).terms || []);
    const gibTerms = ((g.gibberish || {}).terms || []);
    const rel = g.essay_relevance || {};
    const len = g.essay_length || {};
    const profHit = (g.profanity || {}).hit;
    const gibHit = (g.gibberish || {}).hit;

    function one(n, text, wc) {
      if (text === undefined) return "";
      let escaped = S.esc(text || "");
      const flags = [];
      // A profanity hit is recorded per application; highlight + open the essay(s) that
      // actually contain an offending term.
      const hasProfane = profHit && profTerms.some((t) =>
        new RegExp("(^|[^\\w'-])" + escapeRegex(t) + "($|[^\\w'-])", "i").test(text));
      const gibHere = gibHit && (
        gibTerms.some((t) => t.indexOf("e" + n + ":") === 0) ||
        (gibTerms.indexOf("task_d") !== -1) || gibTerms.length === 0);
      const offTopic = rel["e" + n + "_on_topic"] === false;
      if (hasProfane) { escaped = highlightTerms(escaped, profTerms); flags.push("profanity"); }
      if (gibHere) { escaped = highlightGibberish(escaped); flags.push("gibberish"); }
      if (offTopic) flags.push("off-topic");
      const open = flags.length ? " open" : "";
      const flagHtml = flags.map((f) =>
        ' <span class="badge badge-rejected">' + S.esc(f) + "</span>").join("");
      return '<details class="essay"' + open + "><summary>Essay " + n +
        ' <span class="muted small">(' + S.esc(wc === undefined ? "?" : wc) + " words)</span>" +
        flagHtml + "</summary>" +
        '<div class="essay-text">' + (escaped || '<span class="muted">— empty —</span>') +
        "</div></details>";
    }

    return '<h3 class="subhead">Essays</h3>' +
      one(1, essays.e1, len.e1_wc) + one(2, essays.e2, len.e2_wc);
  }

  // ----- LLM-call chips with plain-language explanations ----------------------------------------

  var TASK_INFO = {
    task_a: "Task A — GPA normalization. Converts a non-standard or ambiguous GPA (weighted, " +
      "percentage, foreign scale) into a 4.0-scale equivalent. Runs only when the deterministic " +
      "parser could not resolve the value.",
    task_b: "Task B — low-GPA explanation review. Judges whether the applicant's extenuating-" +
      "circumstances explanation justifies ranking them despite a GPA below 3.3. The further " +
      "below 3.3, the stronger the reason must be.",
    task_c: "Task C — coursework extraction. Splits the free-text coursework list into individual " +
      "courses, classifies each as CS / math / data / other, and normalizes the grades. Feeds the " +
      "coursework bonus (additive only).",
    task_d_e1: "Task D (essay 1) — essay grading. Checks the first essay is on-topic and not " +
      "gibberish (either fails the application), then scores quality 0–20 with a slight " +
      "grammar penalty (ESL-safe).",
    task_d_e2: "Task D (essay 2) — essay grading. Checks the second essay is on-topic and not " +
      "gibberish (either fails the application), then scores quality 0–20 with a slight " +
      "grammar penalty (ESL-safe).",
    task_e: "Task E — resume signal extraction. Counts software-relevant projects, experience, " +
      "and awards in the fetched resume PDF. Feeds the resume bonus (additive only; any " +
      "failure is neutral).",
  };

  function llmChip(name) {
    const info = TASK_INFO[name];
    if (!info) return '<div class="chip">' + S.esc(name) + "</div>";
    return '<div class="chip">' + S.esc(name) +
      ' <span class="chip-help" tabindex="0" role="button" data-info="' + S.esc(info) +
      '" title="' + S.esc(info) + '">?</span></div>';
  }

  // ----- Manual overrides: promote into / demote out of the ranking -----------------------------

  function promoteHtml(r) {
    if (r.outcome === "RANKED") {
      return '<h3 class="subhead">Manual override</h3>' +
        '<div class="btn-row"><button class="btn btn-danger btn-sm" id="demote-btn" data-id="' +
        S.esc(r.submission_id) + '">Remove from ranking…</button>' +
        '<span class="muted small">Marks this applicant rejected as a manual override (no LLM ' +
        "cost), keeps every gate verdict and subscore visible for the audit trail, and " +
        "re-ranks everyone else. Reversible — a removed applicant can be promoted back.</span></div>";
    }
    return '<h3 class="subhead">Manual override</h3>' +
      '<div class="btn-row"><button class="btn btn-primary btn-sm" id="promote-btn" data-id="' +
      S.esc(r.submission_id) + '">Promote into ranking…</button>' +
      '<span class="muted small">Re-runs every scoring stage on this applicant (spends LLM ' +
      "tokens), records the bypassed gates as a manual override, and folds them into the " +
      "ranked list.</span></div>";
  }

  els.detailBody.addEventListener("click", async (ev) => {
    const help = ev.target.closest(".chip-help");
    if (help) { S.toast(help.dataset.info, ""); return; }
    const demoteBtn = ev.target.closest("#demote-btn");
    if (demoteBtn) {
      const sid = demoteBtn.dataset.id;
      if (!window.confirm(
        "Remove this applicant from the ranking?\n\nThey become REJECTED as a manual override " +
        "and everyone below them moves up. You can promote them back later.")) return;
      demoteBtn.disabled = true;
      demoteBtn.textContent = "Removing…";
      try {
        const res = await S.api(
          "/jobs/" + encodeURIComponent(jobId) + "/records/" + encodeURIComponent(sid) + "/demote",
          { method: "POST" });
        await res.json();
        await load(jobId); // ranks shifted for everyone — refetch the whole set
        const demoted = records.find((x) => x.submission_id === sid);
        if (demoted) renderDetail(demoted);
        S.toast("Removed from the ranking (manual override).", "success");
      } catch (err) {
        demoteBtn.disabled = false;
        demoteBtn.textContent = "Remove from ranking…";
        S.toast(err.detail || "Removal failed.", "danger");
      }
      return;
    }
    const btn = ev.target.closest("#promote-btn");
    if (!btn) return;
    const sid = btn.dataset.id;
    if (!window.confirm(
      "Promote this applicant into the ranking?\n\nThis re-runs scoring (LLM cost applies), " +
      "marks the record as a manual override, and re-ranks everyone.")) return;
    btn.disabled = true;
    btn.textContent = "Scoring…";
    try {
      const res = await S.api(
        "/jobs/" + encodeURIComponent(jobId) + "/records/" + encodeURIComponent(sid) + "/promote",
        { method: "POST" });
      const body = await res.json();
      await load(jobId); // ranks shifted for everyone — refetch the whole set
      const promoted = records.find((x) => x.submission_id === sid);
      if (promoted) renderDetail(promoted);
      const rec = body.record || {};
      S.toast("Promoted — rank " + rec.rank + ", score " + S.fmtNum(rec.final_score), "success");
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "Promote into ranking…";
      S.toast(err.detail || "Promotion failed.", "danger");
    }
  });

  function renderResume(res) {
    // Stage 6 (Phase 12). Older decisions.jsonl files have no resume block — show nothing.
    if (res.url_present === undefined) return "";
    let pairs = [
      ["URL present", S.bool(res.url_present, false)],
      ["Fetch attempted", S.bool(res.attempted, false)],
    ];
    if (res.url) {
      pairs.splice(1, 0, ["Resume link",
        '<a href="' + S.esc(res.url) + '" target="_blank" rel="noopener noreferrer">open resume</a>']);
    }
    if (res.attempted) {
      pairs = pairs.concat([
        ["Downloaded", S.bool(res.fetched, false)],
        ["Extracted chars", res.extracted_chars ? S.esc(res.extracted_chars) : "—"],
      ]);
    }
    if (res.failure) pairs.push(["Failure (bonus neutral)", S.esc(res.failure)]);
    if (res.signals) {
      const sig = res.signals;
      pairs = pairs.concat([
        ["Is a resume", S.bool(sig.is_resume)],
        ["Relevant projects", S.esc(sig.relevant_projects)],
        ["Relevant experience", S.esc(sig.relevant_experience)],
        ["Relevant awards", S.esc(sig.relevant_awards)],
        ["Skills relevance", S.fmtNum(sig.skills_relevance, 2)],
        ["Highlights", S.esc(sig.highlights)],
      ]);
    }
    return '<h3 class="subhead">Resume</h3>' + kv(pairs);
  }

  function renderCoursework(courses) {
    if (!courses.length) return "";
    return '<h3 class="subhead">Coursework breakdown</h3>' +
      '<div class="table-wrap"><table class="data"><thead><tr>' +
      '<th class="no-sort">Course</th><th class="no-sort">Grade</th>' +
      '<th class="no-sort num">%</th><th class="no-sort">Category</th>' +
      '<th class="no-sort">Counts</th></tr></thead><tbody>' +
      courses.map((c) =>
        "<tr><td>" + S.esc(c.name) + "</td><td>" + (c.grade_raw ? S.esc(c.grade_raw) : "—") +
        '</td><td class="num">' +
        (c.grade_pct === null || c.grade_pct === undefined ? "—" : S.esc(c.grade_pct)) +
        "</td><td>" + S.esc(c.category) +
        "</td><td>" + S.bool(c.counts) + "</td></tr>").join("") +
      "</tbody></table></div>";
  }
})();
