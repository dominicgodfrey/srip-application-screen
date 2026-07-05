/* Screen 1 (v3) — live cohort dashboard over GET /api/applications.
   Replaces the v2 CSV upload screen: the database is the source of truth and this page
   is a read view with per-cohort filtering, counts, and on-demand exports. */
(function () {
  "use strict";

  const S = window.SRIP;
  const els = {
    cohort: document.getElementById("dash-cohort"),
    refresh: document.getElementById("dash-refresh"),
    counts: document.getElementById("dash-counts"),
    search: document.getElementById("dash-search"),
    tbody: document.querySelector("#dash-table tbody"),
    thead: document.querySelector("#dash-table thead"),
    empty: document.getElementById("dash-empty"),
    exports: document.getElementById("dash-exports"),
  };

  let rows = [];
  let sortKey = "rank";
  let sortAsc = true;

  const OUTCOME_CLASS = { RANKED: "ok", REJECTED: "danger", NEEDS_REVIEW: "warn" };

  async function load() {
    const cohort = els.cohort.value;
    const qs = cohort ? "?cohort=" + encodeURIComponent(cohort) : "";
    try {
      const res = await S.api("/api/applications" + qs);
      const body = await res.json();
      rows = body.applications || [];
      renderCohorts(body.cohorts || [], cohort);
      renderCounts(body.counts || {});
      renderTable();
    } catch (err) {
      const msg = err.status === 503
        ? "Database is not configured on this server."
        : (err.detail || "Could not load applications.");
      els.empty.textContent = msg;
      S.toast(msg, "danger");
    }
  }

  function renderCohorts(cohorts, selected) {
    const current = selected || "";
    els.cohort.innerHTML = '<option value="">All cohorts</option>' + cohorts
      .map((c) => '<option value="' + S.esc(c) + '"' + (c === current ? " selected" : "") +
        ">" + S.esc(c) + "</option>")
      .join("");
  }

  function renderCounts(counts) {
    const order = ["RANKED", "NEEDS_REVIEW", "REJECTED", "received", "grading", "error"];
    const keys = order.filter((k) => counts[k]).concat(
      Object.keys(counts).filter((k) => !order.includes(k)));
    els.counts.innerHTML = keys.map((k) =>
      '<span class="chip ' + (OUTCOME_CLASS[k] || "") + '">' + S.esc(k) + ": " +
      counts[k] + "</span>").join(" ");
  }

  function renderTable() {
    const q = (els.search.value || "").toLowerCase();
    let view = rows.filter((r) => !q ||
      (r.name || "").toLowerCase().includes(q) ||
      (r.email || "").toLowerCase().includes(q) ||
      (r.submission_id || "").toLowerCase().includes(q));

    const dir = sortAsc ? 1 : -1;
    const numeric = sortKey === "rank" || sortKey === "final_score";
    view = view.slice().sort((a, b) => {
      const av = a[sortKey]; const bv = b[sortKey];
      const an = av === null || av === undefined; const bn = bv === null || bv === undefined;
      if (an && bn) return 0;
      if (an) return 1;  // nulls sink regardless of direction
      if (bn) return -1;
      if (numeric) return (av - bv) * dir;
      return String(av).localeCompare(String(bv)) * dir;
    });

    els.tbody.innerHTML = view.map((r) => {
      const outcome = r.outcome
        ? '<span class="chip ' + (OUTCOME_CLASS[r.outcome] || "") + '">' + S.esc(r.outcome) +
          (r.manual_override ? " *" : "") + "</span>"
        : '<span class="chip">' + S.esc(r.status || "") + "</span>";
      return "<tr>" +
        "<td>" + (r.rank ?? "—") + "</td>" +
        "<td>" + S.esc(r.name || "") + "<div class='muted small'>" + S.esc(r.email || "") +
          "</div></td>" +
        "<td>" + S.esc(r.cohort_name || "") + (r.international ? " 🌐" : "") + "</td>" +
        "<td>" + S.esc(r.status || "") + "</td>" +
        "<td>" + outcome + "</td>" +
        "<td>" + (r.final_score === null || r.final_score === undefined
          ? "—" : S.fmtNum(r.final_score)) + "</td>" +
        "<td class='muted small'>" + S.esc((r.submitted_at || "").slice(0, 10)) + "</td>" +
        "<td><a href='/audit'>audit</a></td>" +
        "</tr>";
    }).join("");
    els.empty.classList.toggle("hidden", view.length > 0);
  }

  // ----- events -------------------------------------------------------------------
  els.thead.addEventListener("click", (ev) => {
    const th = ev.target.closest("th[data-key]");
    if (!th) return;
    const key = th.dataset.key;
    if (sortKey === key) sortAsc = !sortAsc; else { sortKey = key; sortAsc = true; }
    renderTable();
  });
  els.search.addEventListener("input", renderTable);
  els.cohort.addEventListener("change", load);
  els.refresh.addEventListener("click", load);
  els.exports.addEventListener("click", (ev) => {
    const a = ev.target.closest("a[data-artifact]");
    if (!a) return;
    ev.preventDefault();
    const cohort = els.cohort.value;
    const qs = cohort ? "?cohort=" + encodeURIComponent(cohort) : "";
    window.location.href = "/api/exports/" + a.dataset.artifact + qs;
  });

  load();
})();
