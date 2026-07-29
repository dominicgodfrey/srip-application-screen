# Project Plan — SRIP ATS v3 (continuous webhook receiver)

Session-to-session memory. See `CLAUDE.md` for how to build, `SRIP_ATS_PRD_v3.md` for what
to build, `SCORING.md` for the scoring model, `WEBSITE_ASKS.md` for external dependencies.

**v2 history:** the complete Fillout-CSV batch system (phases 0–16, all shipped) is frozen
on the **`v2-fillout-batch`** branch together with its PLAN.md history. v3 restarts the
phase numbering as P0–P8.

## Current Phase
**v3.1 re-architecture — in progress. P9 + P10 shipped; P11–P13 remain.** P0–P7 built the
tool half against a *proposed* contract and an always-on host. The 2026-07-26 repo audit
and the 2026-07-27/28 owner decisions invalidated four built assumptions; **two of the four
are now fixed in code** (contract shape → P9, auth scheme → P10) and two remain (hosting
model → P11–P13, bounds source → resolved by deleting the gate entirely).

**A Neon database now exists and is connected** (2026-07-29). `001_init.sql` is applied and
therefore **frozen** — further schema changes need `002_*.sql`. The 11 P1 persistence tests
execute for the first time and pass.

**Suite state:** `uv run pytest -q` → 556 passed, 5 failed. The 5 are
`tests/api/test_promote.py`, and they are *expected*: see "Known red" below.

## Active Sub-Task
**Start P11.5 — retire the v2 in-memory machinery** (`JobRegistry`, `sweeper_loop`,
`/jobs*` routes, the upload screen, and their tests). Pure deletion, and it is *also* the
fix for the only red in the suite (see "Known red"). Doing it first shrinks the codebase
before the serverless port rather than porting code we are about to delete.

Then the rest of P11 (cron drain, stale-claim reaper, migrations out of the lifespan,
pooled connection), P12 (signed-cookie sessions), and P13.1/13.2 (`vercel.json`, ASGI
entrypoint, `requirements.txt` — the two artifacts the partner needs before he can deploy
anything).

## Known red — expected, do not "fix" by patching
`tests/api/test_promote.py` — 5 failures, and they only appear when `DATABASE_URL` is set.
Bisected: 9 pass with the DB disabled, 5 fail with it enabled. `create_app`'s lifespan now
auto-creates a pool from `.env` and applies migrations on every app construction
(`api/main.py:113`), which shifts timing enough that these tests' 200-iteration poll loop
gives up before grading finishes; the endpoint then correctly answers 409 "results not
ready". Verified benign: no exception logged, and `llm_cache`/`applications` stay empty
because these tests never touch the DB. They cover the v2 `/jobs` + CSV-upload machinery,
which **P11.5 deletes** — the tests go with it. Do not patch the poll loop.

(It is also live evidence for P11.3: migrations running on every app startup is wrong, and
P11.3 is what moves them behind an advisory lock.)

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

- **P11 — Serverless port (Vercel)**
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
  - 11.5 **Retire the in-memory machinery** (already a P6 leftover, now load-bearing):
         `JobRegistry`, `sweeper_loop`, the `/jobs*` routes, the upload screen, and their
         tests. All are single-process state that is meaningless on serverless. `/cohorts`
         what-if is already DB-backed (P6a/b) — keep.
  - 11.6 `run_worker` is kept for local `uvicorn` dev only, started behind
         `SRIP_LOCAL_WORKER=1` so it never runs on Vercel. Its two loop tests survive;
         the two `process_one` tests are untouched by the whole phase.
  - 11.7 **LLM concurrency note:** semaphores become per-invocation rather than global.
         At `drain_max_rows: 50` × ~4 calls/row this is *gentler* than the old unbounded
         loop, and 2 000 applications spread over ~40 one-minute drains. Verify the
         OpenAI client's 429/backoff behavior before the pilot.

- **P12 — Session state off the single process**
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

- **P13 — Vercel deploy + pilot ladder** *(replaces old P8)*
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

## In Progress
- [ ] (none — everything remaining is blocked on owner inputs / website-team answers)

## Blocked (owner / external)
- [ ] P7 (E2E half): local end-to-end + idempotent re-replay + 466-row v2-vs-v3
      calibration — **needs the Neon DB** (owner input: create project + dev branch,
      set DATABASE_URL / DATABASE_URL_TEST; then run `uv run pytest tests/test_db.py`
      first). Run: server with secrets set → `scripts/replay.py --fixtures 20` →
      dashboard shows graded rows → re-replay changes nothing.
- [ ] P13 deploy: needs the Vercel Pro confirm + the shared secret value from the partner
      (WEBSITE_ASKS #12/#1), and the Neon DB from the owner.
- [ ] Contract freeze: **unblocked for implementation.** The 2026-07-26 repo audit plus
      the 2026-07-27 decisions settled the shape; P9 executes it. One residual accuracy
      risk (not a blocker): the `all_answers` `field_key` strings were read from their
      repo *seeds* (`lib/questions-default.ts`), and WEBSITE_ASKS #6 records that seeds
      differ from the live form. **One real sample payload would retire this risk** — it
      is the single highest-value remaining ask.

## Load-bearing decisions still open

Ordered by how expensive they are to reverse once P9–P13 start.

**Owner (blocking implementation) — D1–D3 all DECIDED 2026-07-27; see Notes log:**
- [x] **D1 — NEEDS_REVIEW** (not REJECTED) for a config-sourced bounds violation.
- [x] **D2 — stateless signed cookies** (P12.1 as written).
- [x] **D3 — absent `gpa_explanation` = no explanation ⇒ REJECTED.** Owner overrode the
      NEEDS_REVIEW recommendation; treat missing exactly like blank.
- [ ] Confirm no Neon DB has ever run `001_init.sql` (decides amend-in-place vs `002_`).
- [ ] Confirm retiring `/jobs` + the upload screen now (P11.5). Rec: yes.

**Owner (blocking verification, not authorship):**
- [ ] **Neon project + `DATABASE_URL`/`DATABASE_URL_TEST`** — still the top blocker; the
      P1 db suite has never executed. Everything DB-backed in P9–P12 is unverifiable until
      this exists.
- [ ] `OPENAI_API_KEY`; the live form's actual essay word bounds (P13/D1); curated BLOCK
      slur list (carried from v2).

**Partner (Andrew) — blocking the pilot, not the code:**
- [ ] One real sample payload (synthetic values, real shape) — validates every `field_key`,
      the GPA string format, tier values, `submitted_at` offset, and the finaid block at once.
- [ ] Vercel Pro confirmed + willingness to host a Python service in the project (and who
      holds the env vars — see the secrets-governance note in the Notes log).
- [ ] The `ATS_WEBHOOK_SECRET` value, and confirmation they will actually set it (their
      dispatcher omits the header entirely when the env var is unset ⇒ we 401).

**Deferred (explicitly not blocking):** HMAC re-hardening before production (ask #1);
R2 account id (#4, resume off); per-essay bounds in the payload (#5); retention (#13);
results flow-back (#9); cohort allocation ownership (#10). **Resume engine (#11) is no
longer here — decided in-house 2026-07-27; only the *build* is deferred to post-pilot.**

## P6 leftovers (do during/after P7)
- [ ] Retire the v2 `/jobs` routes + registry + upload screen + their tests once the
      replay tool covers the dev/demo flow end-to-end.
- [ ] Close-cycle action (export → typed confirmation → purge + tombstone) once
      WEBSITE_ASKS #13 settles the retention policy.
- [ ] Audit browser: surface the new v3 blocks (technical_essay, international,
      cohort_name) in the detail panel — currently rendered fields are the v2 set.

## Owner inputs needed (v3)
- [x] **Neon project created and connected** (2026-07-29). `DATABASE_URL` +
      `DATABASE_URL_TEST` in `.env`; `001_init.sql` applied to `public`. **Currently both
      DSNs point at the same branch** — safe, because the db suite isolates into a
      throwaway `srip_test_<pid>` schema, but a separate `dev` branch is still the better
      end state (the branch existed; its compute endpoint was not provisioned, so it had
      no connection string). **Both use the DIRECT host, not `-pooler`** — see P11.4.
- [x] `OPENAI_API_KEY` set.
- [ ] Generate `ATS_WEBHOOK_SECRET` (a random UUID is fine) and send it to the website
      team — now a static shared secret, not an HMAC key.
- [ ] Curated BLOCK slur list (carried from v2) — feeds the profanity gate, a rejection
      path, and is still empty.
- [ ] `ADMIN_PASSWORD_HASH` — decided the partner generates and holds it
      (`uv run python -m api.auth '<password>'`); he already holds the OpenAI key, so the
      secrets-governance tradeoff is accepted.
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
- P9/P10: `uv run pytest -q` — 550 passed, 11 skipped (the skips are the DB suite,
  which needs `DATABASE_URL_TEST`). `uv run ruff check .` clean. Live-shaped fixtures
  live in `tests/live_payload.py` — one builder, used by every suite.

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
