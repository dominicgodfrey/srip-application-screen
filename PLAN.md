# Project Plan — SRIP ATS v3 (continuous webhook receiver)

Session-to-session memory. See `CLAUDE.md` for how to build, `SRIP_ATS_PRD_v3.md` for what
to build, `SCORING.md` for the scoring model, `WEBSITE_ASKS.md` for external dependencies.

**v2 history:** the complete Fillout-CSV batch system (phases 0–16, all shipped) is frozen
on the **`v2-fillout-batch`** branch together with its PLAN.md history. v3 restarts the
phase numbering as P0–P8.

## Current Phase
**v3.1 re-architecture — P9–P13.2 shipped. Only the deploy itself (P13.3–13.5) is left,
and it needs Andrew.** All four invalidated assumptions are now fixed in code: contract
shape (P9), auth scheme (P10), hosting model (P11–P13.2), bounds source (gate deleted).

The code is deploy-ready. What remains before the pilot is (a) the ~9-call live OpenAI
smoke, (b) the profanity BLOCK list, (c) owner-only secret generation, (d) Andrew.

**A Neon database now exists and is connected** (2026-07-29). `001_init.sql` is applied and
therefore **frozen** — further schema changes need `002_*.sql`. The 11 P1 persistence tests
execute for the first time and pass.

**Suite state:** `uv run pytest -q` → **534 passed, 0 failed.** Fully green (P11.5 deleted
the 5 known-red tests along with the machinery they covered; P11–P13 added 16).

**Stage 1 E2E is DONE** (2026-07-29, zero tokens — see the Notes log). The full path
webhook → DB → worker → audit record → dashboard → exports ran on 12 synthetic
applications under `SRIP_DEV_FAKE_LLM=1`, including the idempotency invariant.

**⚠️ Still not exercised for real: the OpenAI boundary.** Every suite and the Stage 1 E2E
ran on `FakeLLMClient`, and until 2026-07-29 `.env` held a 16-char placeholder key, so no
live model call has *ever* been made by this codebase. Pinned model IDs, Structured-Outputs
schemas, and prompt wiring are unvalidated against the real API. A real key is in now
(owner's personal, **temporary, strict budget** — pull it before handover).

---

# ▶ START HERE (handoff, updated 2026-07-29 late)

**The whole "Do now" list from the previous handoff is done and pushed** (P11.1–11.4,
P11.6, P12, P13.1–13.2, plus the audit-panel fix). Commits `0c5ab20 · c286868 · 55337cb ·
0accfd2 · b246978 · 53323f8`. Nothing below is blocked by the profanity list.

### Do now, in this order
1. **Stage 2 LLM smoke — ~9 real calls, the last unproven boundary.** Drop
   `SRIP_DEV_FAKE_LLM`, replay **3** fixtures against the real API. Narrow purpose: confirm
   the pinned model IDs exist, Structured Outputs parse into the pydantic models, and the
   prompts don't error. **Not** a quality check. `llm_cache` means a repeat costs nothing.
   Shipping a system whose OpenAI wiring has never run is a bad trade for a few cents.
2. **Curated profanity BLOCK list** (owner input) — then the 466-row calibration, which is
   only blocked by this.
3. **Owner-only handover steps**, then the three asks to Andrew (see below).

### Left deliberately undone, with reasons
- **P13.2 `requirements.txt` — not created.** Vercel's Python runtime accepts
  `pyproject.toml` (docs verified 2026-07-29), so a second generated manifest would only
  drift. The api extra was folded into the main dependency list instead — Vercel installs
  only default dependencies, so an extra would have deployed with no web framework.
- **P13.5 post-ACK grading kick** — still deliberately out of v1.
- **Finaid block in the audit panel** — the record has no finaid fields to render; it lives
  in `payload`, so surfacing it needs an API change, not a JS one. Task F was the item that
  broke the arithmetic and it is fixed.
- **A live Vercel deploy** cannot be verified from here at all; the whole path was proven
  against the real Neon DB locally instead (see the Notes entry).

### What the profanity list blocks
Only **the 466-row calibration run** — outcomes shift under a changed gate, so calibrating
first measures something about to be replaced. It must also be settled before **real
applicants** hit the system (it is a rejection path), but that is go-live, not handover.
`resources/profanity.txt` is a data file loaded live; adding terms changes no code, no
schema, no tests. Treat it as a parallel track.

### Then, and only then: the Andrew handover
Owner-only steps first — **generate a REAL `ATS_WEBHOOK_SECRET`** (the one in `.env` is a
local throwaway that leaked into a chat transcript), and **remove the owner's personal
`OPENAI_API_KEY`**, deciding whose key production uses. Andrew generates and holds
`ADMIN_PASSWORD_HASH` (note: it now doubles as the session-cookie signing key, so changing
it logs every staff session out — that is the intended revocation lever, P12.1). A
`CRON_SECRET` is new and also needed. Re-read the secrets-governance note in the Notes log
before sending: deployed in his Vercel project, his team can read our env vars and function
logs. The env-var table he needs is in `README.md` → Deployment. Then send the three asks in
"Partner (Andrew)" below.

**Also rotate, because this session printed them into a transcript:** the Neon
`neondb_owner` password (both DSNs in `.env`) and the owner's personal `OPENAI_API_KEY`. The
webhook secret was already a known-leaked throwaway.

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
- **P9 — Live contract rework** ✔ shipped 2026-07-28 (44f9d17): one `ApplicationPayload`
        + `ats_run`, `all_answers` mapping, unified `payload`/`payload_hash` column and a
        terminal `stored` status, finaid accepted-and-stored, word-bounds gate deleted,
        `tests/live_payload.py` shared builder. 001 amended in place (no DB had run it).
- **P10 — Auth swap** ✔ shipped 2026-07-28 (44f9d17): `X-ATS-Secret` constant-time
        compare, fail-closed with no secrets, rotation preserved; HMAC seam kept.
- **P8 — Deploy + pilot ladder** — **SUPERSEDED by P13** (the always-on/Render premise
        died with the 2026-07-27 Vercel decision).

---

## Phase Map (v3.1 — live contract + Vercel re-architecture)

Ordering rationale: P9 and P10 are both "every real request fails today" defects — the
parser 422s every live payload and the verifier 401s every live request. They come first
because nothing can be integration-tested until they land. P11 is the hosting port, P12
the state that port breaks, P13 the deploy.

- **P9 — Live contract rework** *(the `ats_mode` → `ats_run[]` reversal)*
  - 9.1 `models.py`: retire the `EssaysModePayload`/`ResumeModePayload` discriminated
        union and `parse_webhook_payload`'s `ats_mode` dispatch. One
        `ApplicationPayload(_Payload)` carrying every sector: `ats_run: list[str]`,
        `gpa_unweighted`/`gpa_weighted` (top-level strings, `"3.95/4.0"`),
        `tier_first_choice`/`tier_second_choice`/`tier_third_choice`,
        `detected_sub_track`, `all_answers: list[AnswerEntry{field_key,question,answer}]`,
        nested `finaid: FinaidPayload{sat_score, test_score_scale, fin_aid_essays[]}`,
        `required_essays`/`optional_essays` (still `EssayEntry`, but `field_key`/
        `min_words`/`max_words` stay optional — payload wins if ever supplied).
        `AliasChoices` tolerance for the old `first_choice`/`sub_track`/nested-`gpa`
        names (cheap insurance; one screenshot still showed a joined `gpa` string).
        `UnsupportedModeError` and the finaid-422 path retire.
        Unmodelled-but-tolerated (`extra="ignore"`): `referral`, `referral_code`,
        `time_spent_seconds`, legacy `ats_mode`.
  - 9.2 **Payload storage shape.** Amend `001_init.sql` in place: replace
        `essays_payload`/`essays_hash`/`resume_payload`/`resume_hash` with a single
        `payload JSONB` + `payload_hash TEXT`, add `ats_run TEXT[] NOT NULL DEFAULT '{}'`.
        Safe to amend rather than add `002_` **only because no Neon project exists yet and
        001 has never been applied anywhere** — verify that before touching it, else write
        `002_unified_payload.sql` instead. The per-mode split existed solely for
        "resume may arrive before essays", a premise the combined payload kills.
        `db.py`: `upsert_application` loses its `mode` parameter; one hash, one column.
  - 9.3 **`ats_run` semantics.** Store every delivery; enqueue for grading only when
        `"essays" ∈ ats_run`. Deliveries without essays (resume-only / finaid-only) land
        in a new terminal status **`stored`** (extend the `status` CHECK) so they are not
        re-claimed by every drain forever. A later delivery that *does* request essays
        resets `status='received'` through the normal changed-hash path. Rejected
        alternative: grade anyway — it spends tokens the partner explicitly did not
        request and contradicts their "fan out to the requested graders, ignore the rest".
  - 9.4 `ingest_webhook.py`: `map_application_payload(payload)` replacing
        `map_essays_payload`. New `index_answers(all_answers) -> dict[str, str]` pulling
        `gpa_explanation`, `relevant_coursework`, `programming_languages`,
        `github_profile`, `institution`, `state_of_residence` by `field_key`. An
        expected-but-absent key appends a `mapping_notes` entry (the mechanism already
        exists). **Per D3 (2026-07-27): absent and blank collapse to one path** — a missing
        `gpa_explanation` is treated as "no explanation", i.e. the existing sub-3.3 REJECT.
        Keep the `mapping_notes` entry anyway so the audit record shows the key was never
        delivered, and add the batch-level drift check (key absent from *every* row in a
        drain ⇒ contract drift, not unanimous non-answer). GPA: `gpa_unweighted` primary,
        `gpa_weighted`-only keeps the existing `force_task_a` route. Tier values are raw form strings (`Honors`/`Intensive`/
        `Regular`) — normalize into the existing `ProgramChoices`.
  - 9.5 `pipeline.py`: `make_grade_fn` reads `db_row["payload"]`. The resume-only
        NEEDS_REVIEW short-circuit is superseded by 9.3 (such rows are never claimed);
        keep the `missing_required_essays` → NEEDS_REVIEW path, which still fires.
  - 9.6 finaid: persisted inside `payload`, surfaced read-only in the audit UI, **scored
        nowhere**. No SCORING.md change. Add a test asserting a finaid payload grades
        identically to the same payload without it.
  - 9.7 Tests: rewrite the inline `_payload`/`_payload_dict` helpers in
        `tests/test_ingest_webhook.py` + `tests/test_pipeline_v3.py` to the new shape;
        add `all_answers` extraction tests (present / absent / blank); update
        `tests/test_replay.py` + `scripts/replay.py` fixture generation.
  - 9.8 **Essay bounds: config-sourced + D1 semantics.** Two coupled changes, both from
        owner decisions. (a) *Source* (2026-07-26): `ingest_webhook.py` reads
        `min_words`/`max_words` from a new `essay_bounds:` config block keyed by essay
        slot, since the live payload carries no bounds — payload still wins if ever
        supplied. (b) *Severity* (D1, 2026-07-27): a required-essay bounds violation
        becomes **NEEDS_REVIEW, not REJECTED**. Add `essay_bounds.on_violation:
        needs_review` (the `reject` branch stays reachable for the payload-supplied case).
        Retire the "tampering or contract drift" REJECT audit note — that inference was
        only ever defensible when the *site* supplied the bounds. Essay 3 over-max still
        just voids its bonus. Tests: the existing bounds matrix flips its expected outcome
        for required essays; inclusive-boundary cases are unaffected.
        **Owner note (do not action now):** deleting the length gate outright is on the
        table for after the pilot — if the site validates at submit, the gate may only
        ever produce false positives. Decide with real violation counts in hand.

- **P10 — Auth swap to the partner's static secret** *(small, do it with P9)*
  - 10.1 `api/webhook_auth.py`: keep the module (it is the seam for restoring HMAC) but
         reduce to `SECRET_HEADER = "X-ATS-Secret"` +
         `verify_webhook(secret_header, secrets) -> None`. Keep `WebhookAuthError.reason`,
         `hmac.compare_digest`, and the current+previous secrets tuple (rotation still
         works). Delete `sign`, the timestamp header, and the skew window.
  - 10.2 `WebhookConfig.max_skew_seconds` removed from `config.py` **and** `config.yaml`
         (`extra="forbid"` means a leftover key fails the load).
  - 10.3 `api/webhooks.py` call-site update — auth still runs before JSON parse and before
         any `dbmod` call, preserving invariant #7. `scripts/replay.py` header swap.
  - 10.4 Tests: the six pure HMAC vector tests become static-secret vectors (missing
         header, wrong secret, previous-secret rotation, no-secrets-configured); endpoint
         tests keep their shape with `_headers()` swapped. The stale/tampered cases in
         `test_unsigned_tampered_stale_all_401_and_touch_nothing` no longer apply — replace
         with missing/wrong-secret cases, keeping the "touches nothing" assertions.

- **P11 — Serverless port (Vercel)** ✔ shipped 2026-07-29 (0c5ab20, c286868, 55337cb) —
        sub-item text below is the as-designed spec; see Completed for what landed.
  - 11.1 **Driver: cron drain.** New `api/cron.py` → `POST /api/cron/drain`, authorized by
         `Authorization: Bearer $CRON_SECRET` (Vercel sets this on cron invocations);
         path added to `OPEN_PREFIXES` and self-guarded like the webhook. Loops the
         **existing, unmodified `process_one`** under a wall-clock budget and a row cap
         (`worker.drain_budget_seconds: 600`, `worker.drain_max_rows: 50` — inside an
         800 s `maxDuration`). Returns `{claimed, graded, elapsed}`. Overlapping
         invocations are already safe: `claim_next` uses `FOR UPDATE SKIP LOCKED`.
  - 11.2 **Stale-claim reaper (new, and required by serverless).** An always-on process
         drained gracefully on shutdown; a killed invocation cannot. Before claiming, the
         drain runs `UPDATE applications SET status='received' WHERE status='grading' AND
         updated_at < NOW() - INTERVAL '<worker.stale_grading_seconds>'`. Without this a
         row orphaned by a timeout is stuck in `grading` forever.
  - 11.3 **Migrations out of the lifespan.** They currently run on every cold start,
         concurrently across instances. Move into the drain endpoint wrapped in
         `pg_try_advisory_lock` (idempotent and near-free after the first run), plus a
         guarded `POST /api/admin/migrate` for the manual first run. Vercel has no
         release phase, so one of these must own it.
  - 11.4 **Pool.** Module-level cached pool in `db.py` (`get_pool()`), `min_size=0`,
         `max_size=2`, against Neon's **pooled** (`-pooler`) endpoint. **Gotcha:** asyncpg
         against PgBouncer transaction mode requires `statement_cache_size=0` — without it
         prepared-statement reuse fails intermittently under load.
  - 11.5 ✔ **Retire the in-memory machinery** — shipped, see Completed.
  - 11.6 `run_worker` is kept for local `uvicorn` dev only, started behind
         `SRIP_LOCAL_WORKER=1` so it never runs on Vercel. Its two loop tests survive;
         the two `process_one` tests are untouched by the whole phase.
  - 11.7 **LLM concurrency note:** semaphores become per-invocation rather than global.
         At `drain_max_rows: 50` × ~4 calls/row this is *gentler* than the old unbounded
         loop, and 2 000 applications spread over ~40 one-minute drains. Verify the
         OpenAI client's 429/backoff behavior before the pilot.

- **P12 — Session state off the single process** ✔ shipped 2026-07-29 (0accfd2)
  - 12.1 **Recommended: stateless HMAC-signed session cookies.** Cookie =
         `base64(payload).hmac`, payload `{exp, v}`, signed with the existing session-key
         secret; verification is a constant-time HMAC + expiry check. Keeps the
         three-table scope (no CLAUDE.md deviation), adds zero per-request DB round-trips,
         and stays pure-function testable in the existing style.
         **Honest tradeoff: no server-side revocation** — logout clears the cookie, but a
         stolen cookie stays valid until `exp`. Mitigations: short TTL (2 h, down from 8)
         and a `session_key_version` so rotating the key invalidates every session at once.
         For one shared staff password this is proportionate; see decision D2.
  - 12.2 **Throttle from `events`.** `COUNT(*)` of `login_failed` events inside the
         lockout window (no PII — it is a shared credential). Falls back to the existing
         in-memory `LoginThrottle` when no pool is configured (local dev).
  - 12.3 `tests/api/conftest.py:22` monkeypatches `SessionStore.is_valid` — re-point at
         the new verifier. The three pure `SessionStore`/`LoginThrottle` tests are
         replaced by sign/verify/expiry/tamper vectors; the nine TestClient login-flow
         tests should survive unchanged.

- **P13 — Vercel deploy + pilot ladder** *(replaces old P8)* — 13.1 ✔ shipped (b246978),
        13.2 ✔ resolved by *not* adding a manifest, 13.3–13.5 need Andrew
  - 13.1 Entrypoint + `vercel.json`: ASGI `app` at a Vercel-discoverable path, a rewrite
         sending all routes to it, `functions.maxDuration: 800`, and
         `crons: [{path: "/api/cron/drain", schedule: "* * * * *"}]`.
  - 13.2 Dependency manifest: Vercel's Python runtime wants `requirements.txt`; this repo
         is `uv`/`pyproject`. Generate via `uv export` (committed, or a build step).
  - 13.3 Env in their Vercel project: `DATABASE_URL` (Neon pooled), `OPENAI_API_KEY`,
         `ATS_WEBHOOK_SECRET[_PREVIOUS]`, `ADMIN_PASSWORD_HASH`, session key, `CRON_SECRET`.
  - 13.4 Pilot ladder: their Test button → a small `submission_id` slice → reconcile their
         `ats_logs` against our rows → go live with the resume stage off.
  - 13.5 *(optional, post-pilot)* Post-ACK grading kick (Starlette `BackgroundTask` or
         Vercel `waitUntil`) to cut the ≤60 s cron latency. Deliberately **not** in v1 —
         the cron path is the guaranteed-correct one and should be proven first.

**Blocked-on-answers map (v3.1):** live `field_key` list / one sample payload → P9.4
mapping confidence · Vercel Pro confirm + secret value → P13 · Neon DB → any DB-backed
verification (P9.2 onward) · retention (#13) → P6
close-cycle · flow-back (#9) → post-v3.

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

- [x] P6a — DB-backed admin API: `assign_read_time_ranks` (per-cohort, never stored;
      scoring/aggregate.py), `bypass_gates` mode on `grade_webhook_applicant` (the v2
      rescore_one semantics: gates recorded-but-bypassed, unscoreable → 0,
      manual_override=True), `api/admin_api.py` under `/api/*`: applications list
      (+counts+cohorts), detail (rank read-time), promote (full re-score, 409 for
      ranked/ungraded/resume-only), demote (deterministic, reversible), delete (204/404,
      tombstoned), exports (five artifacts from live DB via
      `artifact_response_from_records`, `?cohort=` scoping), live cohort what-if,
      `/api/summary`. Manual overrides append events with `decided_by="admin"`.
      12 endpoint tests over a fake store.

- [x] P6b — UI re-point: `/` = new live dashboard (`dashboard.html`/`dashboard.js` —
      cohort filter, counts chips, sortable table, on-demand export links over
      `/api/*`); audit browser + cohort what-if default to LIVE DB mode (`?job=` keeps
      the legacy job-scoped view during transition; promote/demote hit
      `/api/applications/{sid}/…` in live mode); navbar: Dashboard + Sign out; legacy
      upload screen kept unlinked at `/upload` (dev/demo) until the replay tool replaces
      it. Dev-mode (`SRIP_DEV_FAKE_LLM=1`) drops the Secure cookie flag so the local
      http:// demo can hold a session. **Verified live in the preview browser:**
      login redirect → sign-in → session → dashboard renders; audit/cohorts/upload
      pages 200; `/api/exports` degrades to a clean 503 without a DB; zero console
      errors.

- [x] P7 (tool half) — `scripts/replay.py`: CSV export or deterministic synthetic
      fixtures → signed webhook POSTs (same `api/webhook_auth.sign`, so replays are
      indistinguishable from the website once ask #1 lands); Fillout non-UUID ids map
      via uuid5 (stable across replays ⇒ idempotency exercised end to end); optional
      `_test` ping; `--dry-run`. Fixtures span high/low-with-explanation/below-floor
      GPAs + every 4th row carries a technical essay (Task F). 3 conversion tests
      (contract round-trip through the real edge models, id determinism, fixture
      variety) + CLI dry-run smoke.

- [x] P11.5 — **v2 in-memory machinery retired** (2026-07-29). Deleted `api/registry.py`
      (`Job`/`JobRegistry`/`JobState`), `sweeper_loop`, `run_job`, `validate_csv`, the
      `BatchResult`-based `artifact_response`, all six `/jobs*` routes, `JobCreated`/
      `JobStatus`, the `/upload` page + `upload.html` + `upload.js`, and the `?job=` legacy
      branches in `audit.js`/`cohort.js`/`common.js` (`getJobId`/`setJobId`/`JOB_KEY` are
      gone; both screens are unconditionally live-DB now). `api.jobs` survives as just
      `read_upload_capped` + `ArtifactName` + `artifact_response_from_records` — the two
      things `POST /cohorts` and `/api/exports/{artifact}` still need. `ApiConfig`
      `job_ttl_seconds`/`job_sweep_seconds` removed from `config.py` **and** `config.yaml`
      (`extra="forbid"` couples them). Tests: deleted `test_api.py`, `test_upload.py`,
      `test_status.py`, `test_download.py`, `test_promote.py` (the 5 known-red) and the
      `POST /jobs/{id}/cohorts` half of `test_cohorts.py`; re-pointed `test_auth.py`'s
      closed-path assertions at `/api/*`. **–684/+86 lines; suite 517 passed, 0 failed;
      ruff clean.** Verified live in the preview browser (login → dashboard → audit →
      cohorts, `POST /api/cohorts` 200, zero console errors, navbar has no upload link).

- [x] **Local secrets provisioned + Stage 1 E2E green** (2026-07-29, 560f30e). `.env` now
      carries a real `OPENAI_API_KEY` (owner's personal, temporary), a **local throwaway**
      `ATS_WEBHOOK_SECRET`, and a local `ADMIN_PASSWORD_HASH`. Both local secrets are
      self-generated and unrelated to the production values — they were never partner
      blockers. `.claude/launch.json` no longer hardcodes an admin hash (falls through to
      `.env`). Verified at zero token cost: signed `_test` ping 200 + no row; wrong/missing
      secret 401; login/logout/session round-trip; 12 fixtures graded (8 RANKED /
      4 REJECTED / 0 error); re-delivery all `unchanged` with `llm_cache` unmoved; all five
      exports generate; `events` carries structural data only.
- [x] **P7 replay fix — deterministic `submitted_at`** (2026-07-29, 560f30e). Also fixed
      `test_unconfigured_hash_fails_closed`, which depended on an empty ambient `.env`.
      See the Notes log for the re-billing trap this avoided.

- [x] **P11.6 — in-process worker gated** (2026-07-29, 0c5ab20). `run_worker` starts only
      under `SRIP_LOCAL_WORKER=1`; the durable `llm_cache` backend stays wired
      unconditionally. `.claude/launch.json` sets the flag for the local demo server.

- [x] **P11.1–11.3 — cron drain, reaper, migrations relocated** (2026-07-29, c286868).
      `api/cron.py` → `POST /api/cron/drain`: bearer `CRON_SECRET` (constant-time,
      fail-closed on unset), then migrate → reap → `process_one` under
      `worker.drain_budget_seconds` / `drain_max_rows`. Drives the **unmodified**
      `process_one`, so invariant #9 and the SKIP LOCKED claim are the already-tested ones.
      `db.reap_stale_claims` requeues rows orphaned in `grading` past
      `worker.stale_grading_seconds`. `apply_migrations` now runs under
      `pg_try_advisory_lock` (a loser returns `[]`, never waits) and is **out of the app
      lifespan**; `POST /api/admin/migrate` (session-gated) is the manual first run.
      `/api/cron/` added to `OPEN_PREFIXES`. 8 endpoint tests + 1 db test.

- [x] **P11.4 — pool sized for serverless** (2026-07-29, 55337cb). `create_pool` pins
      `statement_cache_size=0` unconditionally (PgBouncer transaction mode reassigns server
      connections per transaction, so cached prepared statements fail *intermittently*);
      `min_size 0` / `max_size 2`; `.env`'s `DATABASE_URL` now points at Neon's `-pooler`
      host, `DATABASE_URL_TEST` stays direct because the db suite does DDL.

- [x] **P12 — stateless signed-cookie sessions + shared throttle** (2026-07-29, 0accfd2).
      Cookie = `<expiry>.<hmac>`; `SessionStore` deleted. **The signing key is the admin
      password hash** — no separate secret to deploy, and rotating the password is the only
      revocation lever a stateless scheme has. TTL 12 h → 2 h. Throttle counts `login_failed`
      events in the window when a pool exists (`db.count_recent_events`), in-memory
      `LoginThrottle` otherwise. Verified live: a session survived a server restart, which
      the old in-memory store could not.

- [x] **P13.1/13.2 — deploy config** (2026-07-29, b246978). `vercel.json`
      (`maxDuration: 800`, `excludeFiles`, per-minute drain cron) +
      `[tool.vercel] entrypoint = "api.main:app"`. No rewrite needed — a FastAPI app deploys
      as one function. The `api` extra folded into main `dependencies`; **no
      `requirements.txt`** (pyproject is a supported manifest). `project_root()` added
      because `config.yaml` / `db/migrations` / `resources` were resolved off `parents[2]`,
      which breaks the moment the package is installed rather than run from the tree.
      README gained the deploy section + env-var table (13.3).

- [x] **P6 leftover — audit panel shows Task F** (2026-07-29, 53323f8), plus `cohort_name` /
      `international`, and a `task_f` builder for the demo LLM handler (its absence is why
      the zero-spend path never surfaced the defect).

## In Progress
- [ ] (none — see "Do now" in ▶ START HERE: the LLM smoke, then the profanity list)

## Blocked (owner / external)
- [ ] P7 (E2E half): local end-to-end + idempotent re-replay + 466-row v2-vs-v3
      calibration. **No longer blocked on the DB** (Neon connected 2026-07-29) — the
      remaining prerequisites are two self-generated local secrets, not external answers:
      `ATS_WEBHOOK_SECRET` (any string; `replay.py --secret` and the server must match, and
      the verifier fails closed so an unset secret 401s everything) and a local
      `ADMIN_PASSWORD_HASH` (`uv run python -m api.auth '<pw>'`; without it login 503s and
      every UI route is shut). Neither needs the partner — production values are separate.
      Run: server with secrets set → `scripts/replay.py --fixtures N` → dashboard shows
      graded rows → re-replay changes nothing.
- [ ] P13 deploy: needs the Vercel Pro confirm and confirmation the partner will actually
      set `ATS_WEBHOOK_SECRET` (WEBSITE_ASKS #12/#1). The *value* is ours to generate and
      send, not theirs to supply.
- [x] Contract freeze — **the residual `field_key` risk is retired.** An earlier version of
      this entry called one real sample payload "the single highest-value remaining ask" on
      the grounds that the `all_answers` field keys came from their repo *seeds*
      (`lib/questions-default.ts`). That was superseded on 2026-07-28: the live **SP27-CSE**
      question rows were pulled from their own `/api/apply/my-application?track=cs`
      endpoint, so the keys are pinned against the live form, not the seeds (see the
      2026-07-28 Notes entry). A real dispatch body would still confirm serialization
      details — offset format, null-vs-absent, exact tier strings — but it is now a
      nice-to-have, not a blocker.

## Load-bearing decisions still open

Ordered by how expensive they are to reverse once P9–P13 start.

**Owner (blocking implementation) — D1–D3 all DECIDED 2026-07-27; see Notes log:**
- [x] **D1 — NEEDS_REVIEW** (not REJECTED) for a config-sourced bounds violation.
- [x] **D2 — stateless signed cookies** (P12.1 as written).
- [x] **D3 — absent `gpa_explanation` = no explanation ⇒ REJECTED.** Owner overrode the
      NEEDS_REVIEW recommendation; treat missing exactly like blank.
- [x] Confirm no Neon DB has ever run `001_init.sql` (decides amend-in-place vs `002_`).
- [x] **Confirm retiring `/jobs` + the upload screen now (P11.5)** — owner said go
      (2026-07-29); shipped.

**Owner (blocking verification, not authorship):**
- [x] **Neon project + `DATABASE_URL`/`DATABASE_URL_TEST`** — done 2026-07-29; the P1 db
      suite executes and passes (11).
- [x] `OPENAI_API_KEY` — real key in as of 2026-07-29 (owner's personal, temporary; see
      "Owner inputs needed").
- [ ] Two **self-generated local** secrets, the only thing standing between here and the
      first real E2E: a throwaway `ATS_WEBHOOK_SECRET` and a local `ADMIN_PASSWORD_HASH`.
      Neither involves the partner.
- [ ] Curated BLOCK slur list (carried from v2).
- ~~the live form's actual essay word bounds (P13/D1)~~ — **dead item.** The word-bounds
      gate was deleted outright (owner, 2026-07-28) because the site 400s any violation at
      submit, so there are no bounds left to supply.

**Partner (Andrew) — blocking the pilot, not the code:**
- [ ] Vercel Pro confirmed + willingness to host a Python service in the project (and who
      holds the env vars — see the secrets-governance note in the Notes log).
- [ ] Confirmation they will actually set `ATS_WEBHOOK_SECRET` (their dispatcher omits the
      header entirely when the env var is unset ⇒ we 401 everything). **We generate and send
      the value**; it is not theirs to supply.
- [ ] *(nice-to-have, no longer a blocker)* One real sample payload — would confirm
      serialization details (offset format, null-vs-absent, exact tier strings). The
      `field_key` risk this used to carry was retired on 2026-07-28; see "Blocked".

**Deferred (explicitly not blocking):** HMAC re-hardening before production (ask #1);
R2 account id (#4, resume off); per-essay bounds in the payload (#5); retention (#13);
results flow-back (#9); cohort allocation ownership (#10). **Resume engine (#11) is no
longer here — decided in-house 2026-07-27; only the *build* is deferred to post-pilot.**

## P6 leftovers (do during/after P7)
- [x] Retire the v2 `/jobs` routes + registry + upload screen + their tests — done as
      P11.5 (2026-07-29).
- [ ] Close-cycle action (export → typed confirmation → purge + tombstone) once
      WEBSITE_ASKS #13 settles the retention policy.
- [ ] **Audit browser: surface the v3 blocks** (`technical_essay_bonus`, `international`,
      `cohort_name`, finaid) in the detail panel — rendered fields are still the v2 set.
      **Promoted to a before-handover item 2026-07-29**: `technical_essay_bonus` is live and
      worth 0–20, so the panel's breakdown does not sum to `final_score` whenever the
      optional essay was written. See the Notes log entry.

## Owner inputs needed (v3)
- [x] **Neon project created and connected** (2026-07-29). `DATABASE_URL` +
      `DATABASE_URL_TEST` in `.env`; `001_init.sql` applied to `public`. **Currently both
      DSNs point at the same branch** — safe, because the db suite isolates into a
      throwaway `srip_test_<pid>` schema, but a separate `dev` branch is still the better
      end state (the branch existed; its compute endpoint was not provisioned, so it had
      no connection string). **Both use the DIRECT host, not `-pooler`** — see P11.4.
- [x] `OPENAI_API_KEY` — **real key present as of 2026-07-29.** Correcting the record: this
      was marked done earlier while `.env` still held a 16-char `sk-` placeholder, so no
      LLM call could ever have succeeded; every suite to date ran on `FakeLLMClient`. The
      current value is the **owner's personal key, in temporarily for testing under a
      strict budget** — it is not the production key and should come back out before
      handover. Spend discipline: run `SRIP_DEV_FAKE_LLM=1` first (zero tokens), keep real
      runs to a handful of fixtures, and rely on the persistent `llm_cache` (re-running
      identical content re-bills nothing). Per row the live cost is 2× `task_d` (gpt-4.1)
      + optionally 1× `task_f` (gpt-4.1) + 1× `task_c` (gpt-4.1-mini); Task E is off at
      `resume.bonus_max: 0`.
- [ ] Generate `ATS_WEBHOOK_SECRET` (a random UUID is fine) and send it to the website
      team — now a static shared secret, not an HMAC key. **Also needed locally before any
      E2E run**, and it does not have to be the production value — pick a throwaway for
      local testing and generate the real one at handover.
- [ ] Curated BLOCK slur list (carried from v2) — feeds the profanity gate, a rejection
      path, and is still empty (`resources/profanity.txt` has the format header and zero
      BLOCK terms; the `better-profanity` default list is all that is live). Settle it
      **before** the 466-row calibration, or the calibration measures a gate that is about
      to change.
- [ ] `ADMIN_PASSWORD_HASH` — decided the partner generates and holds the **production**
      one (`uv run python -m api.auth '<password>'`); he already holds the OpenAI key, so
      the secrets-governance tradeoff is accepted. **The owner still needs a local hash of
      their own to test** — the admin UI is default-deny, so with no hash configured
      `/login` returns 503 and every page 401s or redirects. Unrelated to the partner's.
- [ ] Optional: provision a compute endpoint on the `dev` Neon branch and point
      `DATABASE_URL_TEST` at it, restoring defence in depth.

## How to Verify Completed Work
- P0: `git show v2-fillout-batch --stat`; docs present; `uv run pytest -q` green.
- P1: `uv run pytest tests/test_db.py -q` — **11 passed** against the live Neon branch
  (skips cleanly to 11 skipped if `DATABASE_URL_TEST` is absent). `uv run ruff check .`
  clean. Schema check:
  `SELECT tablename FROM pg_tables WHERE schemaname='public'` →
  applications · events · llm_cache · schema_migrations.
- P2: `uv run pytest tests/api/test_webhook.py -q` — 19 passed, no DB needed.
- P3: `uv run pytest tests/test_worker.py -q` — 7 passed, no DB needed.
- P4: `uv run pytest tests/test_pipeline_v3.py tests/test_ingest_webhook.py
  tests/scoring/test_technical_essay.py -q` — 32 passed; full suite 521 passed.
- P5: `uv run pytest tests/api/test_auth.py -q` — 14 passed; full suite 536 passed.
- P6a: `uv run pytest tests/api/test_admin_api.py -q` — 12 passed; full suite 547 passed.
- P6b: full suite 550 passed; live preview walkthrough (login → dashboard → pages →
  graceful no-DB 503s) done in-session 2026-07-04.
- P7 tool: `uv run pytest tests/test_replay.py -q` — 3 passed;
  `uv run python scripts/replay.py --fixtures 3 --dry-run` prints 3 payloads.
- **Stage 1 E2E (zero tokens) — repeat any time.** Start the server
  (`.claude/launch.json` → `srip-api-demo`, sets `SRIP_DEV_FAKE_LLM=1`, port 8321), then:
  ```
  SECRET=$(grep '^ATS_WEBHOOK_SECRET=' .env | cut -d= -f2)
  uv run python scripts/replay.py --fixtures 12 --secret "$SECRET" --cohort su27-cs
  uv run python scripts/replay.py --fixtures 12 --secret "$SECRET" --cohort su27-cs --no-test-ping
  ```
  First run → twelve `202 accepted`; second → twelve `202 unchanged` with `llm_cache`
  unchanged (invariant #8). UI at http://localhost:8321, password in `.env`'s hash — the
  local one set 2026-07-29. Expected: 8 RANKED / 4 REJECTED, ranks 1–8, all five exports
  200. `--dry-run` prints payloads and sends nothing.
- P11.5: `uv run pytest -q` — **518 passed, 0 failed** (down from 561 collected: 44 tests
  deleted with the machinery, 1 added). `uv run ruff check .` clean. Surface check:
  `uv run python -c "from api.main import create_app; print([r.path for r in create_app().routes])"`
  → no `/jobs*`, no `/upload`. `grep -rn "JobRegistry\|sweeper_loop\|getJobId" api/ tests/`
  → nothing.
- **P11–P13.2:** `uv run pytest -q` → **534 passed**; `uv run ruff check .` clean.
  Serverless-path smoke against the live Neon DB (zero tokens) — requeue one row, age
  another into `grading` by 2 h, then POST the drain:
  `{"migrated": [], "reaped": 1, "processed": 2, "elapsed": 1.97}`, second call
  `{"reaped": 0, "processed": 0}`, all 12 rows `graded`; wrong/missing bearer → 401.
  Pooled endpoint verified separately (migrations no-op under the advisory lock, 20
  concurrent round-trips clean). UI walkthrough on the audit panel: bonus 12.0 and
  34.3 + 22.0 + 12.0 + 5.4 = 73.7 = displayed `final_score`, zero console errors.
  **Note `uv sync` no longer takes `--extra api`.**
- P9/P10: `uv run pytest -q` — 550 passed, 11 skipped (the skips are the DB suite,
  which needs `DATABASE_URL_TEST`). `uv run ruff check .` clean. Live-shaped fixtures
  live in `tests/live_payload.py` — one builder, used by every suite.

---

## Notes / Decisions Log

- **2026-07-29 (late) — P11–P13.2 shipped; five things worth carrying forward.**
  1. **The Vercel deploy story is simpler than P13 assumed, because the docs moved.** Two
     planned items evaporated on reading the current runtime docs: `pyproject.toml` is a
     supported dependency manifest, so **P13.2's `requirements.txt` was not created** (a
     second generated manifest only drifts); and a FastAPI app becomes **one** function, so
     no rewrite rule is needed. What the plan did *not* anticipate: Vercel installs only the
     **default** dependency set, so the optional `api` extra would have deployed a service
     with no web framework. The extra is gone — `uv sync --extra api` now errors.
  2. **`parents[2]` resource paths were a latent deploy bug.** `config.yaml`,
     `db/migrations`, `resources/schools.json`, and `resources/profanity.txt` were all
     resolved relative to a source file two/three levels up. That is correct when running
     from the tree and wrong the moment the package is *installed* (site-packages), which is
     what a host may do. One `config.project_root()` with a cwd fallback covers both. Nothing
     failed locally, so nothing would have caught this before a deploy.
  3. **The session signing key is the admin password hash, deliberately.** P12.1 called for
     a separate session secret plus a `session_key_version` for bulk invalidation. Deriving
     from the password hash gives the same lever for free (change the password ⇒ every
     session dies), removes an env var from the handover, and cannot drift out of sync
     across instances. Recorded because it looks like an omission and is not.
  4. **The demo LLM handler had no `task_f` builder.** So every zero-spend run produced
     `llm_parse_failure → 0 bonus` for the optional essay — which is *correct* fallback
     behavior and therefore silent. It is why the Stage 1 E2E could not have revealed the
     audit-panel arithmetic gap, and a reminder that the fake handler's coverage is part of
     what the local E2E actually tests. Fixed at mid-range values (deliberately not full
     marks — a demo where every bonus maxes out hides the arithmetic).
  5. **`reap_stale_claims` takes a `timedelta`, not a string.** asyncpg binds `$1::interval`
     to a Python `timedelta`; passing `"900 seconds"` raises `DataError` at runtime, not at
     import. Trivial, but it is the kind of thing only a live DB test catches.
- **2026-07-29 — Stage 1 E2E green, and the two defects it exposed.** First run of the
  full path against a live DB, at zero token cost (`SRIP_DEV_FAKE_LLM=1`). Twelve synthetic
  applications: 12 `graded`, 0 `error`, 0 stuck in `grading`; 8 RANKED / 4 REJECTED;
  read-time per-cohort ranks with stable ties; all five exports 200; `events` carried
  `{"result":"accepted","ats_run":["essays"]}` — structural only, no applicant text.
  Invariants spot-checked in SQL: no REJECTED row carries a score, every REJECTED names its
  gate. **Two real defects surfaced, both fixed in 560f30e:**
  1. **`replay.py` sent `submitted_at: None`** — so replays stored NULL timestamps (the
     dashboard's Submitted column was blank) and the partner's **Pacific-offset** parse
     path, a flagged contract delta, was never exercised end to end.
     **The trap in fixing it, and the reason this is a Notes entry:** the obvious fix is
     `datetime.now()`, and it is *wrong and expensive*. `db.content_hash` canonicalizes and
     hashes the **whole payload**, so any wall-clock field changes `payload_hash` on every
     delivery ⇒ every re-replay re-grades ⇒ **the 466-row calibration re-bills in full every
     run**. Fixed as `_REPLAY_EPOCH + index minutes`, byte-stable across calls, with
     `test_replayed_payloads_are_byte_stable_so_re_replay_never_re_bills` guarding it.
     Verified live: content-changed → `accepted`, identical → `unchanged`, and the
     intervening re-grade re-billed **nothing** because the essay text had not moved (the
     `llm_cache` key is per-field, not per-payload). **Rule for anyone touching the replay
     tool or the payload builders: replayed payloads must be deterministic, or re-runs cost
     real money.**
  2. **`test_unconfigured_hash_fails_closed` depended on an empty ambient `.env`.** It
     passed `admin_hash=None` to mean "unconfigured", but in `create_app` `None` means
     "fall back to the environment" — so it broke the moment `ADMIN_PASSWORD_HASH` was set
     locally. Now passes `""`. **Same class as the db-isolation bug two entries down:
     populating real config keeps exposing latent test coupling to ambient env.** Verified
     green both with `.env` populated and with the vars stripped.
- **2026-07-29 — audit panel hides Task F (open, fix before handover).**
  `api/static/js/audit.js` renders GPA / Essay 1 / Essay 2 / coursework / school / **resume**
  but never `scores.technical_essay_bonus`. So it displays a subscore that is permanently 0
  (resume, stage disabled) while omitting one that is live and worth **0–20** — 13% of the
  150 ceiling. Consequence: whenever an applicant wrote the optional essay, the staff-facing
  breakdown does not sum to `final_score`, against the "every subscore explainable" premise
  of the audit record. Not caught by tests because the UI is verified by hand, and not
  visible in the Stage 1 data because the fake handler scored Task F at 0. `international`,
  `cohort_name` and `finaid` are missing from the panel too (the standing P6 leftover), but
  Task F is the one that breaks the arithmetic.
- **2026-07-29 — local secrets are self-generated; they were never partner blockers.**
  Recorded because it cost time: `ATS_WEBHOOK_SECRET` and `ADMIN_PASSWORD_HASH` were filed
  under partner/handover items, which made local E2E look externally blocked. Neither
  involves Andrew. The webhook secret is a shared static string — `replay.py --secret` and
  the server only have to match *each other*, and the verifier fails closed, so an unset
  secret 401s everything and no application can enter the system. The admin hash gates a
  default-deny UI — unset means `/login` 503s and every page is shut, so results exist but
  cannot be looked at. **The values now in `.env` are local throwaways** (the secret leaked
  into a chat transcript); generate fresh ones at handover.
- **2026-07-29 — PLAN.md accuracy pass; three stale claims corrected.** Found while scoping
  the run-up to handover. Recording them because each one had been sitting in this file
  asserting that work was done or blocked when it was not.
  1. **`OPENAI_API_KEY` was ticked done while `.env` held a placeholder** (16-char `sk-`).
     No live model call has ever been made by this codebase; the "no API spend" testing
     posture quietly became "no API capability". Corrected, and the Current Phase section
     now carries the caveat explicitly so it is not re-lost.
  2. **The "one real sample payload is the highest-value ask" entry was 8 days stale.** It
     rested on the field keys having come from repo *seeds*; the 2026-07-28 live-question-
     config pull retired that. Downgraded to nice-to-have. The two entries had been
     contradicting each other in the same document.
  3. **"the live form's actual essay word bounds" was listed as an owner input** for a gate
     that was deleted outright on 2026-07-28. Struck.
  **Also clarified, because it was costing time:** `ATS_WEBHOOK_SECRET` and
  `ADMIN_PASSWORD_HASH` were both filed under partner/handover items, which made local E2E
  look externally blocked. Both are self-generated, the local values need no relationship
  to the production ones, and neither involves Andrew.
- **2026-07-29 — P11.5: what deletion left behind, and one thing deliberately kept.**
  Two judgement calls worth carrying forward.
  1. **`POST /cohorts` (re-uploaded `decisions.jsonl`) was kept**, though it is v2-era and
     the only surviving upload route. It is not in-memory state — it is a pure function
     over an uploaded artifact, so it survives serverless intact, and it is the *only* way
     to run an allocation after a cohort is closed out and purged (PRD v3 §9 deletes the
     rows; the exported artifacts are the durable record). `ApiConfig.max_upload_bytes` /
     `max_rows` stay alive solely for it.
  2. **`grade_batch` / `promote_record` / `demote_record` in `pipeline.py` now have no
     non-test callers.** `/jobs` was their only production caller; the v3 admin API uses
     `grade_webhook_applicant(bypass_gates=...)` instead. They were *not* deleted: they are
     the spine of `tests/test_pipeline.py`, which is where a large share of the v2 gate and
     invariant coverage still lives, and gutting that suite to delete unused code is a bad
     trade during a re-architecture. **Revisit after P13** — the honest options are to
     delete them together with the CSV-batch tests, or to re-point those tests at the
     webhook path. Same question applies to `srip_filter.ingest`'s CSV reader
     (`read_csv_records`/`validate_headers`), now reachable only via `grade_batch` and
     `scripts/replay.py`'s CSV mode.
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
     `bonus_max: 0`. *(Superseded 2026-07-27 — decided in-house; see the entry below.
     The seam and the `bonus_max: 0` default survive the decision unchanged.)*
  5. Scope: CS track only; finaid mode out of scope; email/name dedup retired
     (submission_id + site-level uniqueness); affirmation gate retired.
  6. v2 frozen on `v2-fillout-batch`; CSV upload UI retired (replay tool covers dev use).
  7. Commit convention: `[pN]` prefixes; **no AI co-author trailers** (owner).
- **2026-07-27 — HOSTING REVERSAL: Vercel serverless, not an always-on host** (owner).
  Supersedes PRD v3 §1 ("always-on host", "no serverless") and WEBSITE_ASKS #12. The
  partner offered to deploy in their existing Vercel project; the owner preferred that over
  asking them to fund/run a second service.
  **Why it is now viable (the original objection was factual, and expired):** PRD v3 §2.1
  banned serverless because "a sleeping instance's cold wake eats your 15 s webhook
  timeout". Their dispatcher actually uses `AbortSignal.timeout(60_000)` — 60 s, plus 3
  retries, plus QStash retrying the job 3× more. Verified 2026-07-27 against Vercel docs:
  FastAPI/ASGI is zero-config supported on the Python runtime (3.12/3.13/3.14) running on
  Fluid compute; `maxDuration` is 800 s GA on Pro (1800 s beta); `waitUntil` is supported
  on Python; Pro cron granularity is **once per minute** (Hobby is once per *day*, which
  would have been disqualifying). The partner's `vercel.json` already runs an hourly cron
  (`0 * * * *`), which fails deployment on Hobby ⇒ they are on Pro (confirm anyway).
  **Why the port is cheap:** the queue is already a Postgres status column drained with
  `FOR UPDATE SKIP LOCKED`. That design never required a long-lived process — it required
  *something* to call `process_one`. A cron invocation satisfies it exactly as well as a
  `while True`, so `process_one`/`claim_next`/`GradeFn` are reused unmodified; only the
  driver changes (P11.1).
  **What the port genuinely costs** (all in P11/P12): a stale-claim reaper (a killed
  invocation cannot drain gracefully); migrations moved out of the lifespan; a
  module-level pooled connection against Neon's `-pooler` host with
  `statement_cache_size=0`; and session/throttle state off-process. The in-memory
  `JobRegistry`/`sweeper_loop`/`/jobs` machinery is retired rather than ported.
  **Secrets governance (accepted, worth re-reading before go-live):** deployed inside the
  partner's Vercel project, their team can read env vars and function logs — i.e. our
  `OPENAI_API_KEY` and the ATS DB credentials. Applicant PII is *not* newly exposed (they
  already hold all of it in their own DB), and "separate DB, ATS-only credentials" still
  holds. Mitigations if wanted: a separate Vercel project under their team, or their own
  OpenAI key.
- **2026-07-29 — Neon connected; the db suite's isolation was never real.** Connecting a
  database immediately exposed a bug that had been latent since P1. The `pool` fixture
  bound its throwaway schema with asyncpg's `init=`, which runs **once per connection at
  creation**. asyncpg runs `RESET ALL` when a connection is *released back to the pool*,
  wiping that `search_path` — so only the very first acquire was isolated and every one
  after it silently operated on `public`. Symptom: migrations built the real tables in
  `public` and 16 synthetic rows accumulated there; no `srip_test_*` schema ever held
  anything. **Fix: `setup=` (runs on every acquire), not `init=`.** Verified directly —
  search_path holds across three consecutive acquires and migrations land in the throwaway
  schema. `server_settings={"search_path": ...}` was tried first and does **not** work; it
  does not survive the reset. Stray rows truncated (all synthetic, `a@example.com` /
  `su26-cs` — no real data has ever existed). Commit 0bf9821.
  **Carry-forward:** `001_init.sql` is now applied to a real database and is therefore
  **frozen** — amending it in place was only defensible while no database had run it. Any
  further schema change needs `002_*.sql`.
- **2026-07-28 — LIVE CONTRACT PINNED from the partner repo + the live question config.**
  Read `thinkNeuroWebsite/lib/ats.ts::buildAtsPayload` (the only payload builder — one call
  site, so there is exactly ONE shape) and pulled the live **SP27-CSE** question rows from
  their own applicant-facing endpoint `/api/apply/my-application?track=cs`, which returns
  `SELECT * FROM questions` unfiltered to any authenticated session — `ats_role` included.
  **Four earlier beliefs were wrong and are corrected here:**
  1. **Andrew's 2026-07-21 Slack example matches no live code path.** It shows `ats_mode`,
     a joined `gpa`, UTC `Z`, finaid-only-when-`is_finaid`, and no `all_answers`. The
     legacy "standard URL" fallback only swaps the *destination URL*, never the body — an
     earlier hypothesis that it sent a different shape is disproven.
  2. **Three tiers, not two.** `cohort.tiers` = Honors/Intensive/Regular and
     `cohort_choice_1/2/3` all exist on SP27-CSE. The `cse_academics_nsb_fa26` migration
     that drops Honors targets **FA26-CSE**, a different (older) cohort. `config.yaml`
     `cohort.tiers: [honors, intensive, regular]` is correct as-is.
  3. **`programming_languages` / `github_profile` are NOT on the live form** — repo seed
     only. This reinstates the owner's original 2026-07-06 note and retracts the
     2026-07-27 retraction of it (that retraction read the seed, not live).
  4. **The third essay is `essay_research`, not `essay_technical`**, and all three essays
     carry explicit `ats_role` tags, so `FALLBACK_ATS_ROLE` never applies. It is tagged
     `optional_essay` — already correct for our Task F bonus path, so no partner ask.
     It is `required: true` on the form (mandatory to submit) but stays **bonus-only,
     never rejecting** (owner, 2026-07-28) and keeps its 0–20 weight unchanged.
  **Word bounds: gate deleted outright** (owner, 2026-07-28), superseding D1's
  NEEDS_REVIEW. `app/api/apply/submit/route.ts` returns 400 on any violation, so a
  violating submission never lands — the check could only ever fire on our own stale
  config. Note their validation skips non-required questions, which is moot here because
  all three essays are `required: true`.
  **Other live facts now pinned:** `gpa_explanation` exists, `required: false`, spelled as
  assumed — and because the question row exists, the key is always present in
  `all_answers` with `answer: null` when unanswered, so D3's "absent key" case is
  near-unreachable. `state_of_residence` is a fixed select whose only non-US value is
  `"Non-U.S. Territory"` (existing `is_international` handles it unchanged). Cohort is
  **SP27-CSE**. `regular_cohort_acknowledgment` has a `depends_on` pointing at
  `cohort_preference`, which does not exist on this cohort — a dead dependency on their
  side (harmless to us; worth telling them).
- **2026-07-27 — D1–D3 ANSWERED (owner).** The proposals below were put to the owner and
  all three are now settled. **D1: NEEDS_REVIEW — accepted as proposed.** Owner's reasoning
  matches the proposal: length is already validated on the input end, so a violation
  reaching us is our problem to look at, not the applicant's to be rejected for.
  **Owner note for the future: it may be worth removing the length gate entirely** —
  if the site validates at submit, the gate arguably earns nothing but false positives.
  Deliberately *not* now; revisit after the pilot has produced real violation counts (if
  the NEEDS_REVIEW queue never sees one, that is the evidence to delete it).
  **D2: stateless signed cookies — accepted as proposed** (P12.1 stands as written; 2 h
  TTL + `session_key_version`, no server-side revocation).
  **D3: REJECTED — owner OVERRODE the NEEDS_REVIEW recommendation.** An absent
  `gpa_explanation` key is to be treated exactly like a blank one: a sufficiently low GPA
  with no explanation, which the existing gate rejects. So `index_answers` returning no
  key and returning `""` collapse to one path — simpler than the proposal, and it removes
  the "unknown vs declined" distinction from the code entirely.
  **Consequence to hold onto:** this makes the `gpa_explanation` *field key* load-bearing
  for a rejection path. If the live key differs from the `lib/questions-default.ts` seed we
  mapped from, or the site stops sending it, every sub-3.3 applicant who wrote an
  explanation is auto-rejected and nothing surfaces it. Two cheap mitigations to build with
  P9.4, neither of which reopens the decision: (a) still append the existing
  `mapping_notes` entry when the key is absent, so the audit record shows *why*; (b) a
  drain-level sanity check — the key absent from **every** row in a batch is contract
  drift, not a cohort that all declined to answer. This is also the strongest argument for
  partner ask #1 (one real sample payload) being the top external item.
- **2026-07-27 — the three gate/scope proposals as originally written (D1–D3).**
  Kept for the reasoning; see the entry above for what was actually decided:
  - **D1 — config-sourced word-bounds violation should become NEEDS_REVIEW, not REJECTED.**
    v3 made a required-essay bounds violation a hard REJECT audited as "tampering or
    contract drift". That inference was only defensible *because the site validated at
    submit and sent us its own bounds*. With bounds now living in our `config.yaml`
    (2026-07-26 decision), a violation may simply mean our config is stale — and
    hard-rejecting a good-faith applicant over our own bookkeeping error is exactly what
    "never silently reject" exists to prevent. Proposal: `essay_bounds.on_violation:
    needs_review` as the shipped default, `reject` retained for when the payload itself
    carries bounds. **Changes gate semantics ⇒ needs an owner decision (CLAUDE.md).**
  - **D2 — session state: stateless signed cookies over a fourth table.** Keeps the
    three-table scope and adds no per-request DB round-trip; the cost is no server-side
    revocation (a stolen cookie lives until `exp`), mitigated by a 2 h TTL and a
    key-version rotation knob. The alternative (a `sessions` table) buys real revocation
    at the price of a CLAUDE.md scope amendment.
  - **D3 — an absent `gpa_explanation` key should route sub-3.3 applicants to
    NEEDS_REVIEW, not REJECTED.** `all_answers` is a dump of *answered* questions, so a
    missing key means "we don't know", while present-but-blank means "declined to answer".
    Today's code cannot tell them apart and would auto-reject the first case. Present-but-
    blank keeps today's REJECTED (non-answer) semantics. **Also gate semantics ⇒ needs a
    decision.**
- **2026-07-27 — RESUME ENGINE DECIDED: in-house** (owner). Closes WEBSITE_ASKS #11 and
  supersedes the 2026-07-04 "undecided" item above. **hiring-agent rejected on four
  counts:** (1) it is calibrated for professional hiring, not high-school applicants;
  (2) it is a black box, against an audit record whose whole premise is that every
  subscore is explainable; (3) it puts a third-party agent framework in a minors'-PII
  path — a direct CLAUDE.md "no agent framework" violation; (4) it bypasses the
  fetch → extract → score → discard guardrails unless wrapped, and the wrapper is most of
  the work anyway. **In-house shape:** Task E extracts signals, `config.yaml` `weight_*`
  knobs price them — identical to the Task C/F "model judges, config prices" pattern
  already shipped. **Cost accepted:** rubric quality is on us; the calibration plan is to
  run hiring-agent offline on sample resumes and compare, *if* it's ever worth the time.
  **Explicitly NOT changed by this:** `resume.bonus_max` stays `0`, the stage stays
  disabled, and enablement stays **post-pilot** — this fixes *which* engine, not *when*.
  Three things gate flipping it on: WEBSITE_ASKS #4 (R2 host for the allowlist), the
  `weight_*` knobs being re-priced for 0–25 (they were tuned for v2's 10), and the
  150-point ceiling only being real once it ships (125 until then).
  *Note: an earlier draft of this rationale argued hiring-agent "has less to read with no
  GitHub/languages fields" — that argument is dead: the 2026-07-26 repo audit confirmed
  `programming_languages` and `github_profile` do ship in `all_answers`.*
- **2026-07-26 — owner decisions on the two contract conflicts.**
  1. **Auth: adopt the website's static `X-ATS-Secret` header; retire our HMAC path for
     now.** Rationale: simpler, no website change, and changeable before full production.
     P2's `api/webhook_auth.py` becomes a constant-time shared-secret compare (keep the
     module + its tests as the seam). **Flagged as a pre-production hardening option** —
     HMAC signing (timestamp + body binding + replay window) is ~10 lines on their side
     and the code already exists in git history. Security note: a static bearer secret over
     HTTPS is replayable and does not bind the body; acceptable for now, revisit before go-live.
  2. **Essay word bounds: hardcode in `config.yaml`, don't ask.** The form copy is stable,
     so per-essay `min_words`/`max_words` become owner-maintained config keyed by essay
     slot, not payload metadata. Requires a small P4 change — `ingest_webhook.py` currently
     sources bounds from the payload `EssayEntry`; it needs a config fallback (payload wins
     if ever supplied). **Flagged as a future ask** if the live form's limits start changing.
- **2026-07-26 — read-only audit of the `thinkneuro_website` repo** (full findings in
  WEBSITE_ASKS.md → "Repo audit — 2026-07-26"). Answered asks #2, #3, #6 and discussion #8
  without needing to ask; narrowed #4; #7 partially. **Two breaking mismatches found:**
  (a) **`ats_mode` is gone** — they POST ONE combined payload to ONE endpoint with
  `ats_run: [...]` selecting graders, so our `parse_webhook_payload` mode dispatch would
  **422 every real payload**; the discriminated-union contract needs rework.
  (b) **Auth is a static `X-ATS-Secret` header, not HMAC** — our P2 verifier would **401
  every request**; this is what "UUID for API key" meant. Owner decision needed (accept
  their static secret vs ask for ask-#1 HMAC).
  Also: `all_answers` (full form dump) carries every ask-#2 field incl. `gpa_explanation`
  — the auto-reject risk is gone; their webhook timeout is 60 s not 15 s; they retry any
  non-2xx 3× (+ QStash 3×); `finaid` ships for everyone; `submitted_at` is Pacific-offset;
  new unmodelled fields (`referral`, `referral_code`, `time_spent_seconds`).
  **Trigger model settled: continuous, one POST per applicant** (QStash on submit +
  sequential admin runs) — the PRD v3 fast-ACK + async-worker design stands; the batch
  worry is moot, but Vercel serverless hosting still cannot run our always-on worker.
- **2026-07-21 — website team sent the finalized payload contract (Andrew, Slack).**
  Captured in WEBSITE_ASKS.md → "Answers received — 2026-07-21". Contract deltas to
  reconcile at freeze: GPA as separate `gpa_unweighted`/`gpa_weighted` fields (ask #3),
  `tier_first_choice/...` + `detected_sub_track` names (ask #6), essays are `{question,
  answer}` only (ask #5 unmet), `finaid` nested in the essays payload (not a separate
  `ats_mode`; we currently 422 it), presigned Cloudflare R2 `resume_url` w/ 10-min expiry
  (asks #4/#14). **Owner decision:** finaid = store-but-don't-score (accept + persist, no
  scoring, no SCORING.md change). Three questions sent back to Andrew (gpa_explanation
  location; trigger model/hosting — his Vercel offer is serverless, conflicts with the
  always-on worker + DB pool; HMAC-vs-"API key" confirm). Contract **not** frozen yet.
- **2026-07-04 — external-dependency protocol:** anything requiring website-repo changes
  or partner decisions goes through WEBSITE_ASKS.md (never edit their repo). Payload
  contract work proceeds on PROPOSED-contract fixtures until asks 2/3/5/6 are answered;
  freeze at P2.
- **(carried from v2) openissue items still live:** OPENAI_API_KEY provisioning; curated
  BLOCK slur list for profanity.txt.
