# Project Plan — SRIP ATS v3 (continuous webhook receiver)

Session-to-session memory. See `CLAUDE.md` for how to build, `SRIP_ATS_PRD_v3.md` for what
to build, `SCORING.md` for the scoring model, `WEBSITE_ASKS.md` for external dependencies.

**v2 history:** the complete Fillout-CSV batch system (phases 0–16, all shipped) is frozen
on the **`v2-fillout-batch`** branch together with its PLAN.md history. v3 restarts the
phase numbering as P0–P8.

## Current Phase
P0 — governance docs (v2 freeze + v3 spec suite) — this session.

## Active Sub-Task
P0 complete once the doc suite is committed; next is P1 (persistence layer).

---

## Phase Map (v3)

- **P0 — Governance & freeze** ✔ in progress
  - v2 frozen on `v2-fillout-batch` (README housekeeping committed first).
  - New doc suite: `SRIP_ATS_PRD_v3.md` (authoritative spec), `SCORING.md` (150-pt model),
    `WEBSITE_ASKS.md` (asks 1–7 + discussions 8–14, with status), CLAUDE.md v3 rewrite,
    this PLAN.md; superseded banner on the v2 PRD.
- **P1 — Persistence layer**
  - 1.1 `db/migrations/001_init.sql`: `applications` (submission_id PK, cohort_name,
        identity, per-mode payload JSONB + content hashes, status
        received|grading|graded|error, audit_record JSONB, outcome, final_score,
        timestamps), `llm_cache` (PK (task, input_sha256)), `events` (non-PII ledger);
        indexes (cohort_name, status, updated_at).
  - 1.2 `src/srip_filter/db.py`: asyncpg pool lifecycle, migration applier (tracks
        applied filenames in a `schema_migrations` table), typed store functions
        (upsert_application with per-mode hash short-circuit, claim_for_grading with
        FOR UPDATE SKIP LOCKED, save_audit, cache get/put, event append, list/read for
        the UI, delete_submission).
  - 1.3 Tests vs `DATABASE_URL_TEST` (dev Neon branch): migration idempotence, upsert/
        hash semantics, claim contention, cache round-trip. Skip cleanly when the env var
        is absent (CI-safe).
- **P2 — Webhook receiver**
  - 2.1 HMAC verification (`api/webhook_auth.py`): ts+"."+raw_body, constant-time,
        ±300 s window, current+previous secrets; unit test vectors (valid, unsigned,
        bad sig, stale ts, tampered body, previous-secret).
  - 2.2 Payload contracts in `models.py`: EssaysModePayload / ResumeModePayload /
        TestPing (versioned, extra="ignore" at the edge but required fields strict);
        proposed-contract fixtures under tests/fixtures/webhook/.
  - 2.3 `POST /webhooks/applications`: verify → parse → upsert → 202
        {status: accepted|unchanged}; _test signed → 200 no-row; 401/413/422 paths;
        integration tests assert invariant #7 (bad auth touches nothing).
- **P3 — Grading worker**
  - 3.1 `worker.py`: loop claim → grade → persist audit/outcome/score → status graded;
        per-row try/except → status error + NEEDS_REVIEW record (invariant #9);
        lifespan-managed task alongside the sweeper pattern from v2.
  - 3.2 Persistent `llm_cache` wired into `llm/client.py` (get before call, put after);
        FakeLLMClient tests: identical re-delivery ⇒ zero new LLM calls (invariant #8).
- **P4 — Pipeline deltas**
  - 4.1 `ingest_webhook.py`: EssaysModePayload → ApplicantRow (+ new fields:
        programming_languages, github_profile, state incl. international flag,
        three ranked choices, sub_track); structured GPA (unweighted primary,
        weighted-only → Task A path).
  - 4.2 Stage 1: strict per-essay exact bounds from payload metadata (required violation
        → REJECTED "contract drift"; no-bounds → no check; essay-3 over-max → bonus
        voided flag); profanity across ALL essays incl. optional; retire soft ramp +
        affirmation gate.
  - 4.3 Task D at quality_max_each 15; config + tests.
  - 4.4 NEW Task F (`llm/prompts/task_f.py`, `scoring/technical_essay.py`): judgment
        tier; output {on_topic, gibberish, technical_depth_0_10, exploration_level_0_10,
        impact_0_10, rationale}; deterministic config-priced 0–20; absent → 0 no call.
  - 4.5 School 20/16; Stage 8 new composition (SCORING.md); per-cohort ranking at read
        time; re-derived full invariant suite (§10 items 1–6).
- **P5 — Admin auth**: login page + session store + throttling + `require_admin`
        everywhere except /health + webhook; `decided_by` on promote/demote.
- **P6 — Review UI re-point**: live cohort dashboard (replaces upload screen), audit
        detail / needs-review / cohort what-if / exports over the DB; per-submission
        delete; close-cycle action stub pending WEBSITE_ASKS #13.
- **P7 — Replay tool + E2E**: `scripts/replay.py` (fixtures or v2 CSV → signed POSTs);
        local end-to-end incl. idempotent re-replay; 466-row v2-vs-v3 calibration run
        (local only; every outcome flip explained by an intended rule change).
- **P8 — Deploy + pilot ladder** *(blocked: WEBSITE_ASKS #12 hosting, #1 secret
        coordination)*: host + secrets; their Test button ✓; pilot `submission_ids`
        slice; ats_logs↔DB reconciliation; go live resume-off.

**Blocked-on-answers map:** payload fields (asks 2/3/5/6) → P2 contract freeze (build on
fixtures meanwhile) · hosting (#12) → P8 · resume engine (#11) → post-P8 enablement ·
retention (#13) → P6 close-cycle UX · flow-back (#9) → post-v3.

---

## Completed
- [x] P0.1 — README housekeeping committed (08e05f2); `v2-fillout-batch` branch created
      and pushed (freeze point).
- [x] P0.2 — v3 doc suite: PRD v3, SCORING.md, WEBSITE_ASKS.md, CLAUDE.md + PLAN.md
      rewrites, v2-PRD superseded banner (1121e55).
- [x] P1 — persistence layer: `db/migrations/001_init.sql` (applications + llm_cache +
      events, status CHECK, indexes), `src/srip_filter/db.py` (asyncpg pool, migration
      applier w/ schema_migrations ledger, per-mode hash upsert, SKIP LOCKED claim,
      finish/error, cache, events, list/get/delete), `DbConfig` + `db:` yaml section,
      `database_url`/`database_url_test` Secrets; `tests/test_db.py` (throwaway-schema
      isolation, 11 tests). **Caveat: db tests are skip-until-provisioned — they need
      `DATABASE_URL_TEST` (dev Neon branch); no local Postgres/Docker on this machine.
      Run them first thing once Neon exists.**

- [x] P2 — webhook receiver: `api/webhook_auth.py` (pure HMAC sign/verify, constant-time,
      ±skew window, multi-secret rotation, log-only reasons), PROPOSED-contract payload
      models in `models.py` (EssaysModePayload/ResumeModePayload/GpaPayload/EssayEntry,
      tolerant-edge + strict essentials, gpa accepts structured or legacy string,
      finaid → UnsupportedModeError), `api/webhooks.py` `POST /webhooks/applications`
      (verify → validate → upsert → 202; `_test` signed ⇒ 200 no-row; 401/413/422 never
      500; validation errors carry field locs only — no echoed PII), `webhook:` config +
      HMAC secrets in Secrets, pool + secrets wired into `create_app`/lifespan
      (migrations at startup). 19 tests incl. the full auth-failure matrix proving
      invariant #7 (no row/event on any 4xx) and #8 groundwork (202 "unchanged").

- [x] P3 — grading worker: `src/srip_filter/worker.py` (`process_one` claim → grade →
      persist; `run_worker` loop with prompt stop + iteration-failure backoff; pluggable
      `GradeFn` — P4 supplies the real pipeline mapping; error notes = exception class
      name only, never messages), durable LLM cache (`CacheBackend` protocol on
      `BaseLLMClient` — in-run dict first, then backend, corrupt row ⇒ honest miss;
      `PgCacheBackend` adapter in db.py over `llm_cache`), `worker:` config
      (poll_seconds). 7 tests: drain/persist, crash isolation (invariant #9), prompt
      stop, claim-failure survival, cache-across-restart zero re-bill (invariant #8),
      corrupt-row degradation, no-backend v2 behavior.

- [x] P4 — pipeline deltas: `ingest_webhook.py` (payload→ApplicantRow mapping, essay
      metadata w/ exact bounds, structured GPA w/ weighted-only→`force_task_a`,
      international derivation from a US-names set, contract-drift notes),
      `run_essay_gates_v3` (strict exact bounds — required violation = REJECTED
      "tampering or contract drift"; profanity across ALL essays incl. optional;
      gibberish on required only; soft ramp + affirmation gate retired), Task D at 15
      (schema+prompt+config), **NEW Task F** (`llm/prompts/task_f.py`,
      `scoring/technical_essay.py` — absent→0 free, over-max→voided free,
      parse-failure→0+note, config-priced 0–20), school 20/16, resume `bonus_max: 0`
      (kill switch until WEBSITE_ASKS #11), composition + `Scores.technical_essay_bonus`
      (150 ceiling), `grade_webhook_applicant` + `make_grade_fn` (worker seam; resume-only
      row → NEEDS_REVIEW "essays not yet received"), worker + durable-cache wiring in the
      API lifespan. v2 test pins rescaled; 32 new tests (mapping, Task F ladder, bounds
      matrix incl. inclusive boundaries, optional-essay gate semantics, weighted-GPA
      routing, grade_fn seam). Per-cohort read-time ranking helper moved to P6 (it's a
      read/UI concern).

- [x] P5 — admin auth: `api/auth.py` (PBKDF2-SHA256 password hashing — generate via
      `uv run python -m api.auth '<password>'`; opaque-token `SessionStore` w/ TTL +
      sweep; global sliding `LoginThrottle`; `OPEN_PREFIXES` allowlist), default-deny
      middleware in `create_app` (browsers → 303 /login, API callers → 401 JSON; webhook
      stays HMAC-governed, never redirected), `/login` + `/logout` routes + `login.html`
      (open-redirect guard on `next`; unconfigured hash fails closed 503/401),
      `auth:` config + `ADMIN_PASSWORD_HASH` secret. Existing API tests bypass the
      barrier via an autouse conftest fixture (`real_auth` marker opts into the real
      thing); 14 new auth tests.

## In Progress
- [ ] P6 — review UI re-point (live dashboard over DB, audit detail, promote/demote,
      needs-review queue, exports, per-submission delete, close-cycle stub).

## Owner inputs needed (v3)
- [ ] **Create the Neon project/database** (separate from the website's) + a dev branch;
      put `DATABASE_URL` and `DATABASE_URL_TEST` in `.env`. Unblocks executing the P1 db
      suite and P3 worker integration tests.
- [ ] Generate `ATS_WEBHOOK_SECRET` (share with website team per WEBSITE_ASKS #1).
- [ ] (carried from v2) `OPENAI_API_KEY`; curated BLOCK slur list.

## How to Verify Completed Work
- P0: `git show v2-fillout-batch --stat`; docs present; `uv run pytest -q` green.
- P1: `uv run pytest tests/test_db.py -q` — 11 skipped without `DATABASE_URL_TEST`,
  11 passed with it. `uv run ruff check .` clean.
- P2: `uv run pytest tests/api/test_webhook.py -q` — 19 passed, no DB needed.
- P3: `uv run pytest tests/test_worker.py -q` — 7 passed, no DB needed.
- P4: `uv run pytest tests/test_pipeline_v3.py tests/test_ingest_webhook.py
  tests/scoring/test_technical_essay.py -q` — 32 passed; full suite 521 passed.
- P5: `uv run pytest tests/api/test_auth.py -q` — 14 passed; full suite 536 passed.

---

## Notes / Decisions Log

- **2026-07-04 — v3 replan approved (owner grill session, 13 forks).** Full decision
  record lives in PRD v3; headlines:
  1. Stateless → **persistent** (separate Neon Postgres, plain SQL, no ORM). The v2
     "no DB" principle was deliberately overturned because the intake became continuous
     per-application webhooks from thinkNeuroWebsite; privacy stance replaced by
     retention design (PRD v3 §9).
  2. **HMAC-SHA256 webhook auth** + fast-202 + async worker; no rate limiting.
  3. **Scoring model changed (owner):** 40 GPA + 15+15 essays + 20 technical-essay bonus
     (NEW Task F) + 15 coursework + 20/16 school + 25 resume = 150. Essay word bounds
     strict-to-exact from payload metadata. Profanity in any essay rejects; optional-essay
     gibberish/off-topic only zeroes its bonus.
  4. **Resume engine undecided** (hiring-agent vs in-house) → pluggable seam, ships
     `bonus_max: 0`.
  5. Scope: CS track only; finaid mode out of scope; email/name dedup retired
     (submission_id + site-level uniqueness); affirmation gate retired.
  6. v2 frozen on `v2-fillout-batch`; CSV upload UI retired (replay tool covers dev use).
  7. Commit convention: `[pN]` prefixes; **no AI co-author trailers** (owner).
- **2026-07-04 — external-dependency protocol:** anything requiring website-repo changes
  or partner decisions goes through WEBSITE_ASKS.md (never edit their repo). Payload
  contract work proceeds on PROPOSED-contract fixtures until asks 2/3/5/6 are answered;
  freeze at P2.
- **(carried from v2) openissue items still live:** OPENAI_API_KEY provisioning; curated
  BLOCK slur list for profanity.txt.
