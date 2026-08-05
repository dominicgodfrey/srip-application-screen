# SRIP ATS — Functional Specification (CS Track)

**Consumer of this doc:** anyone changing the filtering logic.
This is the authoritative description of what the service decides and why. `config.yaml` is the
machine-readable source of every tunable; `SCORING.md` is the one-page scoring summary;
`src/srip_filter/models.py` is the source of truth for every schema named here.

The service does exactly two things per application: **reject** on deterministic hard-gate
failures, and **score + rank** every survivor within its cohort. It does **not** decide
acceptances — the ranked list is the deliverable, and acceptance happens downstream.

---

## 0. Governing principles

These decide every ambiguous case:

- **Deterministic-first, fail-fast.** Cheap gates run before any LLM call; the first hard-gate
  hit stops the application with zero further token spend.
- **Hard rules decide rejections; the score only ranks survivors.** No score threshold accepts
  or rejects anyone.
- **Bonuses only add.** The absence of any optional signal is neutral. No code path deducts for
  a missing optional signal, and no bonus can create or rescue a rejection.
- **Never silently reject.** Only an affirmative hard-gate failure produces `REJECTED`. Anything
  unscoreable becomes `NEEDS_REVIEW`.
- **Three outcomes only:** `REJECTED`, `RANKED`, `NEEDS_REVIEW`.
- **Auditability is a feature.** Every applicant gets a structured audit record explaining every
  gate and subscore. Manual overrides record `decided_by`.
- **Idempotent ingest.** Same `submission_id` + same content ⇒ no-op. Changed content ⇒ re-grade.
- **Scoring is owner-owned.** Weights, the 3.3 GPA threshold, the 2.0 floor, and gate semantics
  change only by owner decision.

Scope is the CS / Software-Engineering track. Applications arrive one at a time over weeks, and
results outlive any process, so the service is persistent rather than a batch tool.

---

## 1. Architecture

```
Application website (Vercel) ──POST /webhooks/applications  (X-ATS-Secret)
                                       │ verify → validate → upsert → 202 (milliseconds)
                                       ▼
        FastAPI service on Vercel ◄── POST /api/cron/drain (per-minute cron, bearer)
             │ session-gated admin UI      │ claim → pipeline → audit record
             ▼                             ▼
        Review UI (dashboard,         Neon Postgres (its own database, ATS-only creds):
        audit detail, needs-review,   applications · llm_cache · events
        cohort what-if, exports,
        purge)                        OpenAI (Tasks A–F) · R2 (resume GET, disabled)
```

- One Vercel Function. `vercel.json` pins `maxDuration: 800` and the per-minute cron;
  `[tool.vercel] entrypoint` points at `api.main:app`.
- The transport-agnostic core lives in `src/srip_filter/`; `api/` is a thin HTTP shell.
- **A separate Neon Postgres database** — never the website's. ATS-only credentials, thin
  plain-SQL layer over asyncpg, no ORM. Payloads and audit records are JSONB.
- Nothing depends on a single long-lived process: the queue is a Postgres status column,
  sessions are stateless signed cookies, and the login throttle counts `events` rows.
- Design target ~2,000 applications per cycle.

### 1.1 Persistence schema

Migrations are numbered `.sql` files in `db/migrations/`, applied in order under a Postgres
advisory lock and recorded in `schema_migrations` — safe to run concurrently, idempotent to run
twice.

- **`applications`** — `submission_id` UUID PK, `cohort_name`, `user_email`, `student_name`,
  `sub_track`, `submitted_at`, `payload` JSONB, `payload_hash`, `status`, `audit_record` JSONB,
  `outcome`, `final_score`, `created_at`, `updated_at`.
  `status` is the grading queue: `received | grading | graded | error | stored`.
  `stored` is terminal — delivered but essay grading was never requested.
- **`llm_cache`** — PK `(task, input_sha256)`, `output` JSONB, `model`, `created_at`. Keyed per
  field, so a re-grade re-bills only what changed.
- **`events`** — non-PII operational ledger: deliveries, grade completions, manual overrides
  (with `decided_by`), login failures, purge tombstones. Never essay, explanation, or resume
  text.
- **Rank is never stored** — it is computed at read time, per cohort (§7).

---

## 2. Webhook contract

### 2.1 Security

- **Static shared secret.** The website sends `X-ATS-Secret`; the service compares it
  constant-time against `ATS_WEBHOOK_SECRET` and `ATS_WEBHOOK_SECRET_PREVIOUS` (both accepted, so
  a rotation needs no downtime). Missing or wrong ⇒ **401**, and **nothing is created or
  touched**. No secrets configured ⇒ fail closed. `api/webhook_auth.py` is the single place this
  rule lives, shared with the replay tool.
- HTTPS only. Body cap 1 MiB ⇒ 413. Strict pydantic validation ⇒ 422 with a safe message.
  **Never a 500 on bad input.** No rate limiting — one authenticated source, and admin-triggered
  bursts are legitimate.
- **202 in milliseconds.** The handler does verify → validate → upsert → respond, nothing more.
  Grading is the cron drain's job (§3). A 4xx tells the website the payload is permanently
  rejected and should not be blind-retried.

### 2.2 `POST /webhooks/applications`

One combined payload per application, carrying every sector, plus an
`ats_run: ("essays"|"resume"|"finaid")[]` selector saying which graders to run. **A delivery
whose `ats_run` omits `"essays"` is stored in the terminal `stored` status and never graded.**

Fields: `submission_id`, `user_email`, `student_name`, `cohort_name`, `cohort_display_name`,
`submitted_at` (US Pacific ISO with offset, not UTC `Z`), `ed`, `is_finaid`, `ats_run`,
`gpa_unweighted` and `gpa_weighted` (separate `"value/max"` strings), `tier_first_choice` /
`tier_second_choice` / `tier_third_choice`, `detected_sub_track`, `resume_url`,
`required_essays[]` and `optional_essays[]` (each `{question, answer}`), `all_answers[]` (the
only place `field_key` appears — institution, state of residence, GPA explanation, relevant
coursework), and a `finaid` block.

**`finaid` is present for every applicant** (empty-ish when not applicable) and is **stored but
never scored** — no gate, no subscore. Non-finaid applicants simply drop `"finaid"` from
`ats_run`.

A `_test` ping (`{"_test": true, …}`, sent by the website's Test button) with a valid secret
returns **200 `{ok: true}` and creates no row**. Without the secret it 401s — which is itself the
connectivity answer.

The contract is pinned against the live question config, and `tests/live_payload.py` is the
single builder every suite uses, so a contract change happens in exactly one place.

### 2.3 Idempotency

Upsert by `submission_id` against one `payload_hash` over the whole canonicalized payload:

- hash unchanged ⇒ 202 `{status: "unchanged"}`, nothing re-graded, nothing re-billed;
- hash changed (re-submissions are legal on the website) ⇒ the payload replaces the stored one,
  `status → received`, re-grade — and `llm_cache` makes unchanged fields free.

Duplicate deliveries and admin re-runs are therefore harmless. `submission_id` is the identity;
the website enforces one application per user per cohort.

---

## 3. Grading

`POST /api/cron/drain` is the only path that grades. It authenticates with
`Authorization: Bearer $CRON_SECRET` and fails closed when that is unset, so a misconfigured
deploy never exposes an open grading trigger.

Each invocation does: apply pending migrations (advisory-locked) → reap stale claims older than
`worker.stale_grading_seconds` → `process_one` until the queue is empty, `worker.drain_max_rows`
is reached, or `worker.drain_budget_seconds` expires.

Rows are claimed with `SELECT … FOR UPDATE SKIP LOCKED` where `status='received'`, so
overlapping invocations are safe by construction and never double-grade a row. Each row runs
inside its own try/except: an unexpected error marks **that** row `error` with a `NEEDS_REVIEW`
audit record and the drain moves on. One poisoned application can never stall the queue.

For local development `SRIP_LOCAL_WORKER=1` runs the same `process_one` in an in-process polling
loop instead; `SRIP_DEV_FAKE_LLM=1` swaps in a zero-spend fake model client. Neither is ever set
in production.

**Health.** `GET /health` is unauthenticated and carries no PII or counts. It returns 200
`{"status":"ok"}` normally, and **503** `{"status":"degraded"}` when the oldest ungraded
application is older than `worker.queue_alert_seconds` or the database is unreachable. This is
what makes a silently-stopped drain visible.

---

## 4. Pipeline (per application, fail-fast order)

```
Gate 0   Payload validation        at the edge; malformed ⇒ 422, never stored
Stage 1  Essay deterministic gates profanity (ANY essay ⇒ NEEDS_REVIEW)
                                   gibberish (required essay ⇒ REJECTED)
Stage 2  GPA normalization         structured input; Task A only for odd/weighted-only values
Stage 3  GPA gate                  3.3 threshold / 2.0 floor / Task B      ⇒ REJECTED?
Stage 4  Required essays (Task D)   off-topic or gibberish ⇒ REJECTED; quality 0–15 each
Stage 4b Technical essay (Task F)   bonus 0–20; any failure ⇒ 0 bonus, never a rejection
Stage 5  Coursework (Task C)        bonus 0–15
Stage 6  Resume (Task E)            bonus 0–25, currently disabled
Stage 7  School match               bonus 20 / 16
Stage 8  Compose score              ranking computed at read time, per cohort
Stage 9  Audit record               → applications.audit_record (JSONB)
```

### Stage 1 — essay gates (no LLM)

- **Profanity in ANY essay, including the optional technical essay ⇒ NEEDS_REVIEW.** It stops the
  application, but a human confirms the flag. The gate is `better-profanity`'s word list plus a
  curated ALLOW list in `resources/profanity.txt`, and a word list cannot tell a slur from
  ordinary vocabulary in context ("the transatlantic slave trade", "flange coupling", a
  surname). A false positive must cost a review, not an application. Still fail-fast: zero token
  spend on a flagged row. The BLOCK side of that file is deliberately empty.
- **Gibberish in a required essay ⇒ REJECTED.** Deterministic heuristics needing at least
  `gibberish.min_signals` signals together, so one alone never fires; ESL-safe by construction.
  Task D backstops it. Gibberish in the optional essay only zeroes that bonus.
- Where both fire, rejection wins and `primary_reason` names gibberish — a `REJECTED` record must
  cite the gate that decided it.
- **There is no word-count gate.** The website server-validates essay length at submit and
  rejects violations there, and it sends no bounds, so any length check here could only fire on
  a stale local config — that is, produce false positives on good-faith applicants. Word counts
  are recorded as audit data only, and an over-long optional essay still scores on its merits.

### Stage 2 — GPA normalization

Input is `gpa_unweighted` (format `"3.8 / 4.0"`). The goal is to resolve as many values as
possible **deterministically**; minimizing `NEEDS_REVIEW` volume is an explicit objective.

Deterministic path (no LLM):
- clean 4.0-scale values `0.0–4.0` — used as-is;
- detectable percentages (`85/100`, `92%`, `95.2%`) — via the table below;
- clear `/5` or `/10` scales — linear/table conversion;
- trailing labels stripped (`3.97 GPA`, `3.8/4.0 unweighted`) and the number parsed.

Percentage → 4.0 conversion (`gpa.normalization.percentage_table`; the 87 row is the threshold):

| Percentage | 4.0 |
|---|---|
| 93–100 | 4.0 |
| 90–92 | 3.7 |
| 87–89 | **3.3 ← threshold** |
| 83–86 | 3.0 |
| 80–82 | 2.7 |
| 77–79 | 2.3 |
| 73–76 | 2.0 |
| < 73 | scales linearly toward 0 |

**Task A** handles what the parser cannot: weighted-only submissions, values above the scale
maximum (a 4.4 weighted is not a 4.0 unweighted), non-numeric or foreign scales. Its result is
capped at 4.0.

**`NEEDS_REVIEW`** when even Task A cannot safely place the value — `N/A`, "my school doesn't
offer GPAs", blank. **A missing or unresolvable scale is never a rejection**; doing so would
false-reject the large legitimate international contingent.

### Stage 3 — GPA gate

```
if normalized_gpa is null or requires_manual_review:
    if the GPA field is blank AND the explanation is blank:
        → REJECTED  ("No GPA provided and no explanation given")     # a non-answer
    → NEEDS_REVIEW  ("GPA scale could not be normalized")            # not a rejection
elif normalized_gpa < 2.0:                                           # hard floor
    → REJECTED                                    # no explanation can rescue; no Task B call
elif normalized_gpa >= 3.3:
    → PASS, award GPA points on the 3.3 → 4.0 gradient
else:                                                                # below 3.3
    if the explanation is blank:
        → REJECTED  ("GPA below 3.3, no explanation")
    else:
        Task B judges (severity-scaled bar) → "rank" or "reject"
```

GPA points are a linear gradient over normalized GPA: 3.3 ⇒ 0, 4.0 ⇒ `gpa.score_max` (40). Below
3.3 is reachable only via an approved Task B explanation and lands at the gradient bottom — the
deficit is reflected, never erased. The further below 3.3, the higher the bar Task B applies.

### Stage 4 — required essays

Task D adds relevance (a gate) and quality (a score) per essay. `on_topic=false` or
`is_gibberish=true` ⇒ the whole application is `REJECTED`. Otherwise
`essay_score = max(0, quality_score − grammar_spelling_penalty)`, with quality 0–15 each
(30 total) and the grammar penalty capped at `essay_scoring.grammar_penalty_max` (3) — slight by
design, because many applicants are non-native English speakers.

### Stage 4b — technical essay (optional, bonus-only)

Task F grades the optional technical essay on **relevance to its prompt, technical depth and
difficulty, and real-world impact**. Calibration: generic interest or surface-level online
reading scores low; sustained exploration scores mid; interest turned side project turned real
impact scores high.

The model judges three 0–10 signals and `config.yaml` prices them:

```
bonus = bonus_max × (w_depth·d + w_expl·e + w_impact·i) / (10 × (w_depth + w_expl + w_impact))
```

`on_topic=false` or `gibberish=true` ⇒ 0 bonus. An absent essay ⇒ 0 with no LLM call. Nothing
here can ever reject.

### Stage 5 — coursework (bonus)

Relevance ranking, most to least: **CS > Math > Data > everything else (ignored)**.

- CS / software / programming — strongest positive.
- Math (calculus, linear algebra, discrete, statistics-as-math) — strong.
- Data (data science, analytics, ML, databases) — moderate.
- Anything else — **ignored at weight 0. Not a penalty.**

Grades are **exclusion-only**: a grade counts only when explicitly stated for that course; a
course with no stated grade counts at full weight (never guess or default one); a course
explicitly graded below `coursework.min_grade_pct` (80%) is **excluded entirely**. Any counting
course contributes a flat `category_weight × unit` — the grade never scales the bonus up or down.
Empty coursework ⇒ 0 bonus, no penalty.

### Stage 6 — resume (bonus, disabled)

Task E extracts signals and `config.yaml` prices them, the same shape as Tasks C and F. The
stage is a seam: `payload → {score_0_25, signals, audit_notes}`.

**`resume.bonus_max: 0` disables it entirely — zero fetches, zero tokens — and that is the
current setting.** The `weight_*` values are priced for the 0–25 band (a typical resume earns
about half of it, a standout resume saturates the cap). Enabling it takes two config changes,
not one: raise `bonus_max` to 25, **and** re-pin `resume.allowed_url_hosts` to the website's R2
host — the entry there is the retired Fillout value, and because the allowlist is exact-host, an
unpinned host simply never fetches. `bonus_max` is what the drain reads to decide whether to
build a downloader at all, so raising it is what actually turns the stage on.

Note that resume URLs from the website are presigned with a short expiry, so a resume is
fetchable only within minutes of delivery. The per-minute drain is comfortably inside that
window, but it does mean the stage cannot be calibrated against a stored corpus — and storing
one would violate the discard rule anyway.

When enabled, the guardrails are absolute: https-only exact-host allowlist, no redirects,
streaming size cap, and **fetch → extract → score → discard**. Resume bytes and text never reach
the database, an artifact, or a log. Any failure ⇒ 0 bonus plus an audit note, never a block.

### Stage 7 — school match (bonus)

Fuzzy match (`rapidfuzz`, threshold `school.fuzzy_match_threshold`) against curated lists:
US Top-20 ⇒ 20, International Top-50 ⇒ 16. "High School" or no match ⇒ 0, which is neutral.

### Stage 8 — composition

Per `SCORING.md`: 40 + 15 + 15 + 20 + 15 + 20 + 25 = **150 maximum** (125 while the resume stage
is disabled). Ranking is scoped per `cohort_name`.

Program choices arrive as three ranked dropdown values (regular / intensive / honors) and feed
the cohort what-if tool: a strict first-choice cost ceiling, rank-filled caps, and a waitlist.
Per-tier capacities are a staff input on the cohort endpoints, not config.

---

## 5. LLM tasks

| Task | Job | Tier | Can reject? |
|---|---|---|---|
| A | GPA normalization fallback | mini | no |
| B | Low-GPA explanation adequacy | full | **yes** |
| C | Coursework decomposition | mini | no |
| D | Required-essay grading | full | **yes** |
| E | Resume signal extraction | mini | no |
| F | Technical-essay bonus | full | no (bonus only) |

Model IDs are pinned in `config.yaml` and swappable; verify against OpenAI's current catalog. No
o-series reasoning models.

Rules for every task:

- All calls go through `llm/client.py`. Structured Outputs parsed straight into the pydantic
  models in `models.py` (`TaskAOutput` … `TaskFOutput`), which are the authoritative schemas.
  Prompts live in `llm/prompts/`, never inline.
- Temperature ≤ 0.2 for repeatability. Bounded concurrency via a semaphore.
- **Applicant text is always fenced data in prompts, never instructions.**
- **Requests are paced against `llm.tokens_per_minute`** by a continuously-refilling token
  bucket, so a burst is slowed rather than made lossy. ⚠️ This must be set to the deploying
  account's real TPM limit for the full-tier model.
- **Two distinct failure policies.** A *transient* failure (429, timeout, connection, 5xx) is
  retried up to `llm.max_attempts` with exponential backoff — a rate limit is the service's
  problem, not the applicant's, and must never become a human review item. A *terminal* failure
  (unparseable or invalid output) gets the initial attempt plus one retry, then raises. Only
  terminal failures become `NEEDS_REVIEW` on a required signal, or 0 bonus on an optional one.
- **`llm_cache` (Postgres)** is keyed `(task, sha256(input))`, so a re-grade re-bills only
  changed fields.
- Per-row try/except: one failure is one `NEEDS_REVIEW`/`error` row, never a stuck queue.

---

## 6. Admin surface

Auth is a **shared strong admin password** → a stateless HMAC-signed session cookie
(Secure / HttpOnly / SameSite=Lax), with throttled login attempts counted in `events` so the
throttle holds across instances. The cookie is signed with `ADMIN_PASSWORD_HASH`, so **changing
the password invalidates every live session** — that is the revocation lever. One middleware
default-denies everything outside `auth.OPEN_PREFIXES` (health, the secret-verified webhook, the
bearer-verified cron drain, login/logout, static assets). Manual overrides record `decided_by`,
which under a shared credential is the literal `"admin"`.

Screens:

1. **Live cohort dashboard** — applicants by cohort, outcome counts, grading status,
   filter/sort/search.
2. **Audit detail** — per applicant: gates, the GPA block including explanation text, subscores,
   coursework breakdown, technical-essay bonus, essays with highlight-on-reject, and
   promote/demote buttons.
3. **Needs-review queue** — `NEEDS_REVIEW` rows with their blocker reasons; resolved by promoting
   (which re-scores) or demoting to `REJECTED`.
4. **Cohort what-if** — live capacity allocation over the current per-cohort ranking.
5. **Exports** — `decisions.jsonl`, ranked / rejected / needs-review CSVs, cohort rosters,
   generated on demand from the database.
6. **Lifecycle** — per-submission delete and bulk purge (§9).

---

## 7. Ranking

`RANKED` applicants sort by `final_score` descending **within `cohort_name`**, with a
deterministic tiebreaker: `gpa_points` → essay total → `submission_id`. Rank 1..N is assigned at
read time, so it is always live — a new application can shift ranks until the cycle closes.

There is no acceptance cutoff. The ranked list is the deliverable, and the cohort what-if tool
simulates capacities over it. Results move downstream by export handoff.

---

## 8. Security summary

- **Webhook:** static secret, constant-time compared, current+previous rotation, fail-closed when
  unset. A 401 touches nothing.
- **Cron drain:** bearer `CRON_SECRET`, fail-closed when unset. The only path that grades.
- **Admin:** signed-cookie sessions, throttled login, default-deny middleware, HTTPS.
- **Secrets** live in the environment or a gitignored `.env`, never in code, config, output, or
  logs: `OPENAI_API_KEY`, `DATABASE_URL`, `ATS_WEBHOOK_SECRET[_PREVIOUS]`,
  `ADMIN_PASSWORD_HASH`, `CRON_SECRET`. Every one fails closed.
- **SSRF:** resume fetches are https-only against an exact-host allowlist, with no redirects and
  a streaming size cap.
- **Logs and `events` carry `submission_id` only** — never essay, explanation, or resume text.
  Exception messages are reduced to their class name before logging, because a traceback can
  quote applicant content.
- **Prompt injection:** applicant text is always fenced data, never instructions.

This service holds minors' PII. Every change is judged accordingly.

---

## 9. Data retention

- **Per-submission delete** — hard delete plus a tombstone, for individual removal requests.
- **Bulk purge** — a session-gated control that previews exactly what would be destroyed (row
  count, per-cohort and per-outcome splits, submission-date span, and the applicant fields
  involved), then deletes on confirmation. Scope is one cohort or every cohort. The confirmation
  carries the row count the operator was shown and is refused if the live count has moved, so a
  purge can never destroy a different set than the one displayed. A full wipe also truncates
  `llm_cache`, whose `output` holds model commentary derived from essay text; a scoped purge
  cannot (the cache has no cohort column). Delete and tombstone share one transaction.
- **A purge takes no export.** Artifacts must be downloaded first if they are wanted.
- Both actions leave a non-PII tombstone in `events` — counts, scope, and timestamp.
- Resume bytes and text are never stored under any policy.

The retention *policy* — how long applications live after a cycle closes — is an owner decision
and is not encoded anywhere in the service.

---

## 10. Invariants (each has an explicit test)

1. No optional-signal absence (technical essay, coursework, school, resume) ever reduces
   `final_score`.
2. No bonus changes a `REJECTED` outcome.
3. Every `REJECTED` record names the failing gate in `primary_reason`.
4. GPA below 3.3 never yields points without an approved Task B explanation, and never above the
   gradient bottom.
5. Ranking is deterministic and stable across reruns.
6. Nothing unscoreable is ever `REJECTED` — it becomes `NEEDS_REVIEW`.
7. Unauthenticated, tampered, or wrong-secret webhook requests never create or mutate any row.
8. Re-delivery of identical content changes no outcome and re-bills nothing.
9. A grading crash on one row never blocks the queue.

Tests run with no API spend against a `FakeLLMClient`. Database tests run against
`DATABASE_URL_TEST` and skip cleanly when it is unset.

---

## 11. Audit record

One per applicant, stored as JSONB in `applications.audit_record`. `AuditRecord` in
`src/srip_filter/models.py` is the authoritative schema — validated on read, so a malformed
record degrades visibly rather than silently.

Identity and metadata: `submission_id`, `name`, `email`, `phone`, `cohort_name`,
`state_of_residence`, `international` (derived, not scored), `programming_languages`,
`github_profile`, `sub_track`, `program_choices`, `dedup`.

Decision: `outcome`, `final_score` (null unless `RANKED`), `rank` (read-time), `decided_at_stage`,
`primary_reason`.

Evidence: `gates` (profanity, gibberish, GPA gate, essay relevance, word counts as data),
`gpa` (raw, normalized, original scale, conversion method, confidence, and the Task B
`explanation_eval` when it ran), `scores` (gpa_points, per-essay and total, coursework, school,
resume, technical-essay bonuses), `essays` (texts, for highlight-on-reject),
`coursework_breakdown` (per course: name, raw grade, percentage, category, whether it counts),
`school_match`, `resume`, `technical_essay`.

Provenance: `reasons` (human-readable trail), `llm_calls` (which tasks ran), `errors`, and
`manual_override` — true when a human pushed a `REJECTED`/`NEEDS_REVIEW` applicant into the
ranking from the audit UI. The original gate verdicts stay visible in `gates` and `reasons`, so
the override is honest in the trail.
