# PRD v3 — SRIP ATS: Continuous Webhook-Receiver Application Filter (CS Track)

**Owner:** Dominic
**Consumer of this doc:** Claude Code (implementation agent)
**Supersedes:** `SRIP_Application_Filter_PRD.md` (v2, Fillout CSV batch — frozen on the
`v2-fillout-batch` branch). Where this document is silent, v2 semantics carry over;
where they conflict, v3 wins.
**Approved:** owner grill session, 2026-07-04.

---

## 0. What changed and why

The intake moved off Fillout onto the partner-owned **thinkNeuroWebsite**
(Next.js + Neon Postgres + Auth0 on Vercel). That site dispatches **one JSON POST per
application** to configurable per-cohort ATS webhook URLs and logs every attempt
(`ats_logs`) with per-row re-run. The ATS is therefore no longer a batch tool a human
feeds a CSV — it is a **continuous, persistent, secured receiver**:

- **Stateless → persistent.** v2's "no DB" principle is deliberately overturned:
  applications arrive one at a time over weeks and results must outlive any restart.
  The replacement privacy stance is §9 (retention) — the *spirit* (hold PII no longer
  than needed) survives at cycle timescale.
- **Trigger model:** website admins press "Run ATS on All Applications"
  (`untested_only` re-runs undelivered rows). Assume batch bursts of single-application
  POSTs; the design is trigger-agnostic if the site later adds auto-dispatch.
- **Scope:** CS/Software-Engineering track only. `finaid` mode is **out of scope** (the
  site simply leaves that URL unconfigured). Med track is future work.

Unchanged non-negotiables from v2 §0: deterministic-first fail-fast; hard rules reject,
score only ranks; bonuses only add; never silently reject; three outcomes only
(`REJECTED` / `RANKED` / `NEEDS_REVIEW`); auditability is a feature; GPA threshold 3.3,
hard floor 2.0, blank-GPA+blank-explanation ⇒ REJECTED.

---

## 1. System architecture

```
thinkNeuroWebsite (Vercel) ──signed POST /webhooks/applications (ats_mode: essays|resume)
                                       │ HMAC verify → validate → upsert → 202 (ms)
                                       ▼
        FastAPI service (always-on host) ─── in-process async grading worker
             │ session-gated admin UI            │ DB queue → pipeline → audit record
             ▼                                   ▼
        Review UI (live dashboard,          Neon Postgres (separate DB, ATS-only creds):
        audit detail, promote/demote,       applications · llm_cache · events
        needs-review, cohort what-if,
        exports, close-cycle)               OpenAI (Tasks A,B,C,D,E,F) · R2 (resume GET)
```

- Separate Python/FastAPI service; the transport-agnostic core (`src/srip_filter/`)
  keeps all logic; `api/` is the shell.
- **Separate Neon Postgres database** — never the website's DB; ATS-only credentials;
  thin plain-SQL layer (asyncpg), **no ORM**; payloads and audit records as JSONB.
- Single instance; the queue lives in Postgres; concurrency bounded by the existing
  semaphores. ~2,000 applications/cycle design target.

### 1.1 Persistence schema (migrations in `db/migrations/*.sql`)

- **`applications`** — `submission_id` UUID PK, `cohort_name`, `user_email`,
  `student_name`, `sub_track`, `submitted_at`, `essays_payload` JSONB,
  `resume_payload` JSONB, `essays_hash`, `resume_hash`, `status`
  (`received | grading | graded | error`), `audit_record` JSONB, `outcome`,
  `final_score`, `created_at`, `updated_at`.
  Resume-mode may arrive before essays-mode: a row may exist with only
  `resume_payload`; composition happens when essays grade.
- **`llm_cache`** — PK `(task, input_sha256)`, `output` JSONB, `model`, `created_at`.
  The v2 in-run cache made persistent: re-grades re-bill only changed fields.
- **`events`** — non-PII operational ledger: deliveries, grade completions, manual
  overrides (with `decided_by`), purge tombstones (counts + timestamps). Never essay or
  explanation text.
- **Rank is never stored** — computed at read time per cohort (§7).

---

## 2. Webhook contract

### 2.1 Security (all webhook requests)

- **HMAC-SHA256 signing.** Headers `X-ATS-Timestamp` (unix seconds) and
  `X-ATS-Signature = hex(HMAC_SHA256(secret, timestamp + "." + raw_body))`.
  Constant-time comparison; reject if |now − timestamp| > 300 s (replay window).
  Two accepted secrets (`ATS_WEBHOOK_SECRET`, `ATS_WEBHOOK_SECRET_PREVIOUS`) for
  zero-downtime rotation. Unsigned/mis-signed/stale ⇒ **401**, no row created or touched.
- HTTPS only. Body cap ~1 MB ⇒ 413. Strict pydantic validation ⇒ 422 with a safe
  message — **never a 500** on bad input. No rate limiting (single authenticated source;
  admin-run bursts are legitimate).
- **202 in milliseconds:** verify → validate → upsert → respond. Grading is async
  (the website's `sendWebhook` aborts at 15 s; its `ats_logs.success` means *delivered*,
  not *graded*). 4xx tells the website the payload is permanently rejected — don't
  blind-retry.

### 2.2 `POST /webhooks/applications`

Discriminated by `ats_mode`:

- **`_test` ping** (`{"_test": true, ...}` — sent by the site's Test button): if signed,
  ⇒ 200 `{ok: true}`, **no row created**. Unsigned ⇒ 401 (that *is* the connectivity
  answer).
- **`essays` mode** — the primary application record. Expected fields (pinned against
  the PROPOSED contract; final field list = website-team ask list, `WEBSITE_ASKS.md`):
  `submission_id`, `user_email`, `student_name`, `cohort_name`, `cohort_display_name`,
  `submitted_at`, `ed`, `is_finaid`, `ats_mode`,
  `gpa: {unweighted, weighted|null}` (structured — ask #3),
  `gpa_explanation`, `relevant_coursework`, `programming_languages`, `institution`,
  `state_of_residence`, program-choice fields (three ranked), `github_profile`,
  `sub_track`, `resume_url`,
  `required_essays[]` / `optional_essays[]` with per-entry
  `{question, answer, field_key, min_words, max_words}` (ask #5).
- **`resume` mode** — thin payload (`submission_id`, identity, `resume_url`, `gpa`).
  Upserted onto the same row; triggers only the resume stage when enabled.

### 2.3 Idempotency

Upsert by `submission_id` with a per-mode content hash (`essays_hash`, `resume_hash`):

- hash unchanged ⇒ 202 `{status: "unchanged"}`, nothing re-graded, nothing re-billed;
- hash changed (the site allows re-submission overwriting `form_data`) ⇒ payload
  replaces the stored one, `status → received`, re-grade; `llm_cache` makes unchanged
  fields free;
- duplicate deliveries (admin re-runs, `untested_only` races) are therefore harmless.

v2's email/name dedup logic retires: `submission_id` is the identity, and the website
enforces one application per user per cohort (`UNIQUE(user_email, cohort_id)`).

---

## 3. Grading worker

In-process async worker; claims rows with `SELECT … FOR UPDATE SKIP LOCKED` where
`status='received'`, runs the pipeline per row inside try/except (per-row isolation —
"when grading begins, it finishes"; unexpected error ⇒ `status='error'` + NEEDS_REVIEW
audit record, never a stuck queue). Progress and completions go to `events`.

---

## 4. Pipeline (per application, fail-fast order)

```
Gate 0  Payload validation           (at the edge; malformed ⇒ 422, never stored)
Stage 1 Essay deterministic gates    profanity (ANY essay) · gibberish (required essays)
                                     · strict word bounds ── fail ⇒ REJECTED, STOP
Stage 2 GPA normalization            structured input; Task A only for odd/weighted-only
Stage 3 GPA gate                     unchanged v2 logic (3.3 / 2.0 / Task B)  ⇒ REJECTED?
Stage 4 Required essays (Task D)     off-topic/gibberish ⇒ REJECTED; quality 0–15 each
Stage 4b Technical essay (Task F)    bonus 0–20; failures ⇒ 0 bonus, never reject
Stage 5 Coursework (Task C)          bonus 0–15, unchanged
Stage 6 Resume                       bonus 0–25 behind pluggable seam; ships DISABLED
Stage 7 School                       bonus 0–20/16, unchanged mechanics
Stage 8 Compose + rank per cohort    (rank computed at read time)
Stage 9 Audit record → applications.audit_record (JSONB)
```

Stage-by-stage deltas from v2:

- **Stage 1 length:** strict to the exact per-essay `min_words`/`max_words` from the
  payload. The site server-validates required essays at submit, so a violation reaching
  us signals tampering or contract drift — REJECTED with an audit note saying exactly
  that. An essay entry without bounds gets no length check. Essay 3 over-max ⇒ bonus
  voided (0), not rejected. The v2 soft-penalty ramp and HARD_MIN/HARD_MAX retire.
- **Stage 2 GPA:** input is `gpa.unweighted` (format "3.8 / 4.0" — the existing
  `_from_fraction` deterministic path). Task A fires only for weighted-only submissions
  or unparseable values. Everything else in v2 §6 (percentage table, /5, /10,
  NEEDS_REVIEW routing) carries over.
- **Stage 4:** Task D unchanged except `quality_max_each: 15`.
- **Stage 4b — NEW Task F (judgment tier, can only grant bonus):** grades the optional
  technical essay on **relevance to its prompt, technical depth/difficulty, and
  real-world impact**. Calibration: generic interest / surface-level online reading ⇒
  low; sustained exploration ⇒ mid; interest turned side-project turned real impact ⇒
  high. Output schema:
  `{on_topic, gibberish, technical_depth_0_10, exploration_level_0_10, impact_0_10,
  rationale}` — deterministic config-priced math maps signals to 0–20 (model judges,
  config prices — the Task C pattern). `on_topic=false` or `gibberish=true` ⇒ 0 bonus.
  Absent essay ⇒ 0, no LLM call. Profanity in this essay was already a Stage-1 reject.
- **Stage 6 resume:** engine decision pending (`WEBSITE_ASKS.md` #11). The stage is a
  seam: `payload → {score_0_25, signals, audit_notes}`. Ships with
  `resume.bonus_max: 0` (zero fetches, zero tokens). When enabled: fetch from the R2
  host (https-only exact-host allowlist — ask #4), pypdf extract,
  **fetch → extract → score → discard** — resume bytes/text never persist.
- **Stage 7 school:** bonuses become US-Top-20 = 20, Intl-Top-50 = 16.
- **Stage 8:** composition per `SCORING.md` (40+15+15+20+15+20+25 = 150). Ranking is
  **scoped per `cohort_name`**.
- **Affirmation gate retires** — the new form enforces required checkboxes at submit.

Program choices: the live form carries three ranked choice dropdowns
(regular/intensive/honors) — v2's three-tier cohort machinery (strict first-choice cost
ceiling, rank-filled caps, waitlist) survives with structured input replacing free-text
parsing. **Note:** the website repo's `questions-default.ts` seeds differ from the live
form; the payload contract must be pinned against the live question config (ask #6).

---

## 5. LLM tasks

| Task | Job | Tier | Notes |
|---|---|---|---|
| A | GPA normalization fallback | mini | now rare (structured GPA) |
| B | Low-GPA explanation adequacy | full | unchanged, severity-scaled |
| C | Coursework decomposition | mini | unchanged |
| D | Required-essay grading | full | quality max 15 each |
| E | Resume signal extraction | mini | behind the resume seam |
| F | **NEW** technical-essay bonus | full | §4 Stage 4b |

All v2 LLM rules stand: structured outputs into pydantic, temperature ≤ 0.2, retry-once
then NEEDS_REVIEW (`LLM_PARSE_FAILURE`) for required signals / 0-bonus for optional ones,
prompts in `llm/prompts/`, model IDs pinned in config. Cache is now the persistent
`llm_cache` table.

---

## 6. Admin surface (session-gated)

Auth: **shared strong admin password** → server-side session, secure/HTTP-only cookie,
throttled login attempts. One `require_admin` dependency guards every route except
`/health` and the HMAC-verified webhook. Treated as the permanent solution (the
dependency is the seam if SSO is ever wanted). Manual overrides record `decided_by`.

Screens (adapted from v2's Phase 10–16 UI, re-pointed from in-memory jobs to the DB):

1. **Live cohort dashboard** — replaces the upload screen: applicants by cohort, outcome
   counts, score histogram, grading-status column, filter/sort.
2. **Audit detail** — the existing per-applicant panel (gates, GPA block incl.
   explanation text, subscores, coursework breakdown, essays with highlight-on-reject,
   promote/demote buttons) unchanged in spirit.
3. **Needs-review queue** — NEEDS_REVIEW rows + blocker reasons; resolves via
   promote (re-score) as in v2.
4. **Cohort what-if** — live capacity allocation over the current ranking (per cohort).
5. **Exports** — `decisions.jsonl`, ranked/rejected/needs-review CSVs, cohort rosters;
   generated on demand from the DB.
6. **Lifecycle** — per-submission delete (individual removal requests) and the
   close-cycle action (§9), both admin-gated, both tombstoned in `events`.

---

## 7. Ranking & downstream

Sort `RANKED` by `final_score` desc within `cohort_name`; tiebreaker
gpa_points → essay total → submission_id; rank 1..N assigned at read time (always live —
a new application can shift ranks until the cycle closes). No acceptance cutoff — the
ranked list is the deliverable; the cohort what-if tool simulates capacities.
Acceptance/payment/onboarding live on the website side; results move by export handoff
until the flow-back discussion (`WEBSITE_ASKS.md` #9) says otherwise.

---

## 8. Security summary

- Webhook: HMAC + replay window + rotation (§2.1); 401 touches nothing.
- Admin: session auth (§6); throttled login; HTTPS everywhere.
- Secrets (env only, never repo): `OPENAI_API_KEY`, `DATABASE_URL`,
  `ATS_WEBHOOK_SECRET[_PREVIOUS]`, `ADMIN_PASSWORD_HASH`, session signing key.
- SSRF: resume fetches https-only against an exact-host allowlist (R2 public host);
  no redirects; streaming size cap.
- Logs and `events` carry `submission_id` only — never essay/explanation/resume content.
- Prompt-injection posture unchanged: applicant text is always fenced data in prompts,
  never instructions; Task F/E prompts are injection-resistant per the Task E precedent.

## 9. Data retention

Design supports (final policy pending `WEBSITE_ASKS.md` #13):

- **Per-submission delete** — hard delete + tombstone.
- **Close-cycle** — admin action: export final artifacts → typed confirmation → delete
  the cohort's applicant rows → non-PII tombstone (counts + timestamp). DB empty between
  cycles; exported artifacts in staff hands are the durable record.
- Optional anonymized-analytics retention (strict column allowlist, all free text
  dropped) if the owner chooses that variant.
- Resume bytes/text are never stored under any policy.

## 10. Invariants (all tested, extending v2 §12)

1. No optional-signal absence (essay 3, coursework, school, resume) ever reduces
   `final_score`.
2. No bonus changes a `REJECTED` outcome.
3. Every `REJECTED` record names the failing gate in `primary_reason`.
4. GPA < 3.3 never yields points without an approved Task B explanation, never above
   the gradient bottom.
5. Ranking is deterministic and stable across reruns.
6. Nothing unscoreable is ever `REJECTED` — always `NEEDS_REVIEW`.
7. **NEW** Unsigned / tampered / stale / replayed webhook requests never create or
   mutate any row.
8. **NEW** Re-delivery of identical content changes no outcome and re-bills nothing.
9. **NEW** A grading crash on one row never blocks the queue (per-row isolation).

## 11. Out of scope (v3)

Finaid mode · med track · results flow-back into the website · auto-dispatch triggers ·
GitHub-profile fetching for resume eval · manual CSV upload (may return later; the
replay tool covers dev needs).

## 12. External dependencies

Everything the website team must change/answer lives in **`WEBSITE_ASKS.md`** (asks
1–7, discussions 8–14). Build against the §2.2 proposed contract with fixtures until
asks 2/3/5/6 are confirmed; freeze the contract at P2.
