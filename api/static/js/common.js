/* Shared front-end helpers (Phase 10). Plain ES modules-free globals on `window.SRIP`. */
(function () {
  "use strict";

  const JOB_KEY = "srip_job_id";

  // ----- Toast ----------------------------------------------------------------
  let toastTimer = null;
  function toast(message, kind) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.className = "toast show" + (kind ? " " + kind : "");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = "toast" + (kind ? " " + kind : ""); }, 4200);
  }

  // ----- Job id (URL ?job= first, then sessionStorage) ------------------------
  function getJobId() {
    const fromUrl = new URLSearchParams(window.location.search).get("job");
    if (fromUrl) {
      try { sessionStorage.setItem(JOB_KEY, fromUrl); } catch (e) { /* ignore */ }
      return fromUrl;
    }
    try { return sessionStorage.getItem(JOB_KEY) || ""; } catch (e) { return ""; }
  }
  function setJobId(id) {
    try { id ? sessionStorage.setItem(JOB_KEY, id) : sessionStorage.removeItem(JOB_KEY); }
    catch (e) { /* ignore */ }
  }

  // ----- Fetch wrapper: throws an Error carrying {status, detail} --------------
  async function api(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) {
      let detail = res.statusText;
      try { const body = await res.json(); if (body && body.detail) detail = body.detail; }
      catch (e) { /* non-JSON error body */ }
      const err = new Error(detail);
      err.status = res.status;
      err.detail = detail;
      throw err;
    }
    return res;
  }

  // ----- Formatters -----------------------------------------------------------
  function fmtNum(v, digits) {
    if (v === null || v === undefined || v === "") return "—";
    const n = Number(v);
    if (Number.isNaN(n)) return String(v);
    return n.toFixed(digits === undefined ? 1 : digits);
  }
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function badge(outcome) {
    const o = String(outcome || "").toLowerCase();
    return '<span class="badge badge-' + esc(o) + '">' + esc(outcome) + "</span>";
  }
  function bool(flag, goodWhenTrue) {
    const good = goodWhenTrue === false ? !flag : !!flag;
    return '<span class="' + (good ? "flag-ok" : "flag-bad") + '">' + (flag ? "yes" : "no") + "</span>";
  }

  // ----- Highlight the active nav link ---------------------------------------
  function markActiveNav() {
    const path = window.location.pathname;
    const map = { "/": "upload", "/audit": "audit", "/cohorts": "cohorts" };
    const key = map[path];
    if (!key) return;
    const link = document.querySelector('.nav-links a[data-nav="' + key + '"]');
    if (link) link.classList.add("active");
  }
  document.addEventListener("DOMContentLoaded", markActiveNav);

  window.SRIP = { toast, getJobId, setJobId, api, fmtNum, esc, badge, bool, JOB_KEY };
})();
