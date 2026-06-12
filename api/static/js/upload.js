/* Screen 1 — upload a CSV, poll the grading job, render the summary, link the downloads.
   Talks only to the existing JSON API: POST /jobs, GET /jobs/{id},
   GET /jobs/{id}/results/{artifact}, DELETE /jobs/{id}. */
(function () {
  "use strict";

  const S = window.SRIP;
  const POLL_MS = 1500;

  const els = {
    form: document.getElementById("upload-form"),
    file: document.getElementById("csv-file"),
    uploadBtn: document.getElementById("upload-btn"),
    uploadNote: document.getElementById("upload-note"),
    progressSection: document.getElementById("progress-section"),
    progressFill: document.getElementById("progress-fill"),
    progressLabel: document.getElementById("progress-label"),
    summarySection: document.getElementById("summary-section"),
    counts: document.getElementById("summary-counts"),
    hist: document.getElementById("summary-hist"),
    nrBlock: document.getElementById("needs-review-block"),
    nrBody: document.querySelector("#needs-review-table tbody"),
    downloads: document.getElementById("download-links"),
    linkAudit: document.getElementById("link-audit"),
    linkCohorts: document.getElementById("link-cohorts"),
    discardBtn: document.getElementById("discard-btn"),
  };

  let pollTimer = null;

  function stopPolling() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  function show(el) { el.classList.remove("hidden"); }
  function hide(el) { el.classList.add("hidden"); }

  // ----- Upload ---------------------------------------------------------------
  els.form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const file = els.file.files[0];
    if (!file) { S.toast("Choose a CSV file first.", "danger"); return; }

    els.uploadBtn.disabled = true;
    els.uploadNote.textContent = "Uploading…";
    const fd = new FormData();
    fd.append("file", file);

    try {
      const res = await S.api("/jobs", { method: "POST", body: fd });
      const body = await res.json();
      S.setJobId(body.job_id);
      etaBase = null; // fresh run, fresh rate measurement
      els.uploadNote.textContent = "";
      hide(els.summarySection);
      show(els.progressSection);
      setProgress(null, null, "Starting grading…");
      poll(body.job_id);
    } catch (err) {
      els.uploadBtn.disabled = false;
      els.uploadNote.textContent = "";
      S.toast(err.detail || "Upload failed.", "danger");
    }
  });

  // ----- Poll -----------------------------------------------------------------
  // ETA: measured from the first progress sample of this run (not upload time, so ingest /
  // queue delay doesn't skew the rate), shown once a few rows have finished.
  let etaBase = null; // { t: ms timestamp, done: rows finished at that moment }

  function etaText(done, total) {
    if (!done || done >= total) return "";
    if (!etaBase || done < etaBase.done) etaBase = { t: Date.now(), done: done };
    const advanced = done - etaBase.done;
    const elapsedS = (Date.now() - etaBase.t) / 1000;
    if (advanced < 3 || elapsedS < 2) return ""; // not enough signal for a stable estimate yet
    const remainingS = Math.round((total - done) * (elapsedS / advanced));
    return " — about " + fmtDuration(remainingS) + " remaining";
  }

  function fmtDuration(s) {
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60);
    if (m < 60) return m + "m " + (s % 60) + "s";
    return Math.floor(m / 60) + "h " + (m % 60) + "m";
  }

  function setProgress(done, total, label) {
    if (total) {
      const pct = Math.round((done / total) * 100);
      els.progressFill.classList.remove("indeterminate");
      els.progressFill.style.width = pct + "%";
      els.progressLabel.textContent =
        (label || (done + " of " + total + " applicants graded")) + etaText(done, total);
    } else {
      els.progressFill.classList.add("indeterminate");
      els.progressFill.style.width = "";
      els.progressLabel.textContent = label || "Preparing…";
    }
  }

  function poll(jobId) {
    stopPolling();
    pollTimer = setTimeout(async () => {
      try {
        const res = await S.api("/jobs/" + encodeURIComponent(jobId));
        const job = await res.json();
        if (job.state === "succeeded") {
          stopPolling();
          hide(els.progressSection);
          renderSummary(jobId, job.summary || {});
          els.uploadBtn.disabled = false;
        } else if (job.state === "failed") {
          stopPolling();
          hide(els.progressSection);
          els.uploadBtn.disabled = false;
          S.toast(job.error || "Grading failed.", "danger");
        } else {
          setProgress(job.rows_done, job.rows_total);
          poll(jobId);
        }
      } catch (err) {
        stopPolling();
        hide(els.progressSection);
        els.uploadBtn.disabled = false;
        const msg = err.status === 404
          ? "Job expired or was discarded — upload again."
          : (err.detail || "Could not reach the server.");
        S.toast(msg, "danger");
      }
    }, POLL_MS);
  }

  // ----- Summary --------------------------------------------------------------
  function renderSummary(jobId, summary) {
    const counts = summary.counts || {};
    els.counts.innerHTML =
      stat(counts.total, "Total", "") +
      stat(counts.RANKED, "Ranked", "ranked") +
      stat(counts.REJECTED, "Rejected", "rejected") +
      stat(counts.NEEDS_REVIEW, "Needs review", "review");

    renderHistogram(summary.ranked_score_histogram || {});
    renderNeedsReview(summary.needs_review || []);
    renderDownloads(jobId);

    els.linkAudit.href = "/audit?job=" + encodeURIComponent(jobId);
    els.linkCohorts.href = "/cohorts?job=" + encodeURIComponent(jobId);
    show(els.summarySection);
  }

  function stat(value, label, cls) {
    return '<div class="stat ' + cls + '"><div class="stat-num">' +
      (value === undefined ? "—" : S.esc(value)) +
      '</div><div class="stat-label">' + S.esc(label) + "</div></div>";
  }

  function renderHistogram(hist) {
    const entries = Object.entries(hist);
    if (!entries.length) {
      els.hist.innerHTML = '<p class="muted small" style="margin:0;">No ranked applicants.</p>';
      return;
    }
    const max = Math.max(...entries.map(([, n]) => n), 1);
    els.hist.innerHTML = entries.map(([bucket, n]) =>
      '<div class="hist-row">' +
        '<span class="hist-bucket">' + S.esc(bucket) + "</span>" +
        '<div class="hist-bar-wrap"><div class="hist-bar" style="width:' +
          Math.max(2, Math.round((n / max) * 100)) + '%"></div></div>' +
        '<span class="hist-count">' + S.esc(n) + "</span>" +
      "</div>").join("");
  }

  function renderNeedsReview(rows) {
    if (!rows.length) { hide(els.nrBlock); return; }
    els.nrBody.innerHTML = rows.map((r) =>
      "<tr><td>" + S.esc(r.submission_id) + "</td><td>" + S.esc(r.name) +
      "</td><td>" + S.esc(r.reason) + "</td></tr>").join("");
    show(els.nrBlock);
  }

  const ARTIFACTS = [
    ["decisions", "decisions.jsonl"],
    ["ranked", "ranked.csv"],
    ["rejected", "rejected.csv"],
    ["needs_review", "needs_review.csv"],
    ["summary", "summary.json"],
  ];

  function renderDownloads(jobId) {
    els.downloads.innerHTML = ARTIFACTS.map(([name, filename]) =>
      '<a href="/jobs/' + encodeURIComponent(jobId) + "/results/" + name +
      '" download>' + S.esc(filename) + "</a>").join("");
  }

  // ----- Discard --------------------------------------------------------------
  els.discardBtn.addEventListener("click", async () => {
    const jobId = S.getJobId();
    if (!jobId) { S.toast("No job to discard.", "danger"); return; }
    if (!window.confirm("Discard this job and its results? Make sure you downloaded everything you need.")) return;
    try {
      await S.api("/jobs/" + encodeURIComponent(jobId), { method: "DELETE" });
      S.setJobId("");
      hide(els.summarySection);
      S.toast("Job discarded.", "success");
    } catch (err) {
      if (err.status === 404) { S.setJobId(""); hide(els.summarySection); }
      S.toast(err.detail || "Discard failed.", err.status === 404 ? "" : "danger");
    }
  });

  // ----- Resume an in-flight/finished job on page load -------------------------
  const existing = S.getJobId();
  if (existing) {
    show(els.progressSection);
    setProgress(null, null, "Checking previous job…");
    els.uploadBtn.disabled = true;
    poll(existing);
  }
})();
