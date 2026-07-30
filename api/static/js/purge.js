/* Bulk purge (PRD v3 §9) — the confirmation dialog behind the bottom-corner control.

   The dialog's whole job is to make the scale concrete before anything is destroyed: how many
   applications, in which cohorts, spanning which dates, and which applicant fields go with
   them. Figures come from GET /api/admin/purge-preview (read-only) and are re-fetched whenever
   the scope changes, so what the operator reads is always what the confirm button would delete.

   The count is passed back as `expected_count` and the server refuses to delete if it has moved
   — webhook deliveries keep arriving while the dialog sits open. */
(function () {
  "use strict";

  const S = window.SRIP;
  const els = {
    open: document.getElementById("purge-open"),
    dialog: document.getElementById("purge-dialog"),
    body: document.getElementById("purge-body"),
    confirm: document.getElementById("purge-confirm"),
  };
  if (!els.open || !els.dialog) return;

  // Cohorts are discovered from the unscoped preview, so the select is built once per open and
  // survives scope changes (a scoped preview only knows about its own cohort).
  let cohorts = [];
  let preview = null;

  function fmtDate(iso) {
    if (!iso) return "—";
    // Date only: the dialog needs the span, and a timestamp implies a precision it lacks.
    return String(iso).slice(0, 10);
  }

  function scopeSelect(selected) {
    const opts = ['<option value="">Every cohort (full wipe)</option>']
      .concat(cohorts.map((c) =>
        '<option value="' + S.esc(c.name) + '"' + (c.name === selected ? " selected" : "") +
        ">" + S.esc(c.name) + " (" + c.n + ")</option>"));
    return '<label class="muted small" for="purge-scope-select">Scope</label> ' +
      '<select id="purge-scope-select">' + opts.join("") + "</select>";
  }

  function render() {
    const p = preview;
    const scoped = p.cohort_name;
    const outcomes = Object.keys(p.by_outcome || {});
    const cohortNames = Object.keys(p.by_cohort || {});

    let html = '<div class="row-actions" style="margin-bottom:0.9rem">' +
      scopeSelect(scoped) + "</div>";

    if (!p.total) {
      html += "<p>Nothing to delete in this scope — there are no applications.</p>";
      els.body.innerHTML = html;
      wireScope();
      els.confirm.disabled = true;
      return;
    }

    html += '<div class="purge-scope">' +
      '<div class="purge-count">' + p.total + "</div>" +
      "<div><strong>application" + (p.total === 1 ? "" : "s") + "</strong> will be permanently deleted from " +
      (scoped ? "cohort <strong>" + S.esc(scoped) + "</strong>" : "<strong>every cohort</strong>") +
      "</div></div>";

    html += "<p>Each of those rows carries, and loses:</p><ul>" +
      "<li>the applicant's <strong>name and email address</strong></li>" +
      "<li>the full submitted <strong>payload</strong> — every essay, the GPA explanation, " +
      "coursework, institution, and program choices</li>" +
      "<li>the <strong>audit record</strong> with all gate results, subscores and grading " +
      "rationales (" + p.with_audit_record + " of " + p.total + " have one)</li></ul>";

    html += "<dl>" +
      "<dt>Submitted between</dt><dd>" + fmtDate(p.earliest) + " and " + fmtDate(p.latest) + "</dd>" +
      "<dt>Cohorts affected</dt><dd>" + (cohortNames.length
        ? cohortNames.map((c) => S.esc(c) + " (" + p.by_cohort[c] + ")").join(", ")
        : "—") + "</dd>" +
      "<dt>By outcome</dt><dd>" + (outcomes.length
        ? outcomes.map((o) => S.esc(o) + " (" + p.by_outcome[o] + ")").join(", ")
        : "—") + "</dd>" +
      "</dl>";

    // The cache holds model-generated commentary derived from essay text, so whether it is
    // cleared is a privacy fact the operator has to be told either way.
    html += p.llm_cache_cleared
      ? "<p>The <strong>LLM cache will also be cleared</strong> (" + p.llm_cache_rows +
        " rows of model output derived from essay text). Re-grading anything later will " +
        "re-bill the OpenAI calls.</p>"
      : '<p class="purge-kept">The LLM cache (' + p.llm_cache_rows + " rows) is <em>not</em> " +
        "touched by a single-cohort purge — it is keyed by content hash with no cohort column. " +
        "Choose a full wipe to clear it.</p>";

    html += '<p class="purge-kept">Kept: a non-PII tombstone in the events ledger recording ' +
      "the counts and timestamp. No export is taken — download anything you need first. " +
      "<strong>This cannot be undone.</strong></p>";

    els.body.innerHTML = html;
    wireScope();
    els.confirm.disabled = false;
  }

  function wireScope() {
    const sel = document.getElementById("purge-scope-select");
    if (sel) sel.addEventListener("change", () => loadPreview(sel.value));
  }

  async function loadPreview(cohort) {
    els.confirm.disabled = true;
    const qs = cohort ? "?cohort=" + encodeURIComponent(cohort) : "";
    try {
      const res = await S.api("/api/admin/purge-preview" + qs);
      preview = await res.json();
      if (!cohort) {
        cohorts = Object.keys(preview.by_cohort || {})
          .map((name) => ({ name: name, n: preview.by_cohort[name] }));
      }
      render();
    } catch (err) {
      const msg = err.status === 503
        ? "Database is not configured on this server."
        : (err.detail || "Could not read the current figures.");
      els.body.textContent = msg + " Nothing has been deleted.";
      S.toast(msg, "danger");
    }
  }

  els.open.addEventListener("click", () => {
    preview = null;
    cohorts = [];
    els.body.textContent = "Loading current figures…";
    els.confirm.disabled = true;
    els.dialog.showModal();
    loadPreview("");
  });

  els.confirm.addEventListener("click", async () => {
    if (!preview || !preview.total) return;
    els.confirm.disabled = true;
    els.confirm.textContent = "Deleting…";
    try {
      const res = await S.api("/api/admin/purge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cohort: preview.cohort_name,
          expected_count: preview.total,
        }),
      });
      const receipt = await res.json();
      els.dialog.close();
      S.toast("Deleted " + receipt.applications_deleted + " application" +
        (receipt.applications_deleted === 1 ? "" : "s") + ".", "success");
      // Every screen is a view over the rows just destroyed, so re-read rather than leave
      // stale records on screen looking live.
      setTimeout(() => window.location.reload(), 900);
    } catch (err) {
      // 409 means the count moved and nothing was deleted: show the reason, then re-preview so
      // the operator confirms against the new figures rather than the stale ones.
      S.toast(err.detail || "Purge failed — nothing was deleted.", "danger");
      if (err.status === 409) loadPreview(preview.cohort_name || "");
    } finally {
      els.confirm.textContent = "Confirm delete";
    }
  });
})();
