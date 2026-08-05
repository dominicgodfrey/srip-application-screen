/* Shared front-end helpers (Phase 10). Plain ES modules-free globals on `window.SRIP`. */
(function () {
  "use strict";

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
  // ----- Trusted HTML marker --------------------------------------------------
  // Applicant text (names, essays, GPA explanations, model rationales) is rendered by
  // string-building into innerHTML. That works only while every single interpolation
  // remembers esc(), and one forgotten call is stored XSS in a page that can trigger the
  // bulk purge. So the default is "escape", and pre-built markup has to say so out loud:
  // raw(html) is the only way a caller can bypass escaping, which makes the unsafe cases
  // greppable instead of invisible. Helpers that legitimately emit markup return raw().
  function raw(html) {
    return { __html: String(html) };
  }
  function isRaw(v) {
    return v !== null && typeof v === "object" && typeof v.__html === "string";
  }
  // Render any value for HTML insertion: pass raw() markup through, escape everything else.
  function html(v) {
    return isRaw(v) ? v.__html : esc(v);
  }

  // These return markup STRINGS, so they still compose with `+` at the many call sites
  // that build a cell out of several pieces. Wrap the finished string in raw() when handing
  // it to something that escapes by default (see kv() in audit.js).
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
    const map = { "/": "dashboard", "/audit": "audit", "/cohorts": "cohorts" };
    const key = map[path];
    if (!key) return;
    const link = document.querySelector('.nav-links a[data-nav="' + key + '"]');
    if (link) link.classList.add("active");
  }
  document.addEventListener("DOMContentLoaded", markActiveNav);

  window.SRIP = { toast, api, fmtNum, esc, raw, isRaw, html, badge, bool };
})();
