# SRIP ATS v3 — Continuous Application Filtering Service (CS Track)

A **continuous, persistent, secured** webhook-receiver ATS. The partner-owned application
website POSTs one authenticated JSON payload per application; this service validates,
stores, grades asynchronously, and gives staff a session-gated review UI over the live
cohort. It does exactly two things per application: **reject** on deterministic hard-gate
failures, and **score + rank** every survivor within its cohort. It does **not** decide
acceptances.

**Full functional spec: @SRIP_ATS_PRD_v3.md** — read the relevant section before any logic
decision not covered here. `SRIP_Application_Filter_PRD.md` is the superseded v2 spec,
still authoritative where v3 says semantics carry over (GPA §6, task contracts §8, audit
record §9). **Scoring model: @SCORING.md** (mirrors `config.yaml`, the machine source of
truth). **Deployment: @README.md** → Deployment.

**History:** the v2 stateless Fillout-CSV batch system is frozen on the
`v2-fillout-batch` branch. v3 (this) is `main`. The stateless→persistent reversal was an
explicit owner decision (2026-07-04).

**Repository:** https://github.com/dominicgodfrey/srip-application-screen.git

> ### The four contract/hosting facts that differ from PRD v3's original text
> PRD v3 was written against a *proposed* payload contract and an always-on host. Both
> changed before launch; the code below is built to the live reality, and PRD v3 carries the
> same correction table in its own banner. In short:
> 1. **Contract:** no `ats_mode` discriminated union — one combined payload carrying every
>    sector plus an `ats_run: ("essays"|"resume"|"finaid")[]` selector saying which graders
>    to run. Deliveries that don't request essays are stored terminally, never graded.
> 2. **Webhook auth:** a static `X-ATS-Secret` header, constant-time compared, with
>    current+previous secrets for rotation. HMAC request signing is deferred as
>    pre-production hardening; `api/webhook_auth.py` is the seam that restores it.
> 3. **Hosting:** Vercel serverless, in the partner team's project and managed by them. The
>    grading worker is a **per-minute cron drain** over the Postgres queue
>    (`POST /api/cron/drain`), not an always-on loop; sessions are stateless signed cookies
>    and the login throttle counts `events` rows, so nothing depends on one process.
> 4. **Essay word bounds:** the length gate was **deleted outright** (owner, 2026-07-28) —
>    the site 400s any violation at submit, so the gate could only ever produce false
>    positives on a stale local config.
>
> Finaid is **stored but never scored** (no SCORING.md change).

---

## Tech Stack

Settled (do not re-litigate without an owner decision):

- **Python 3.11+**, managed with **`uv`**.
- **`pydantic` v2** + **`pydantic-settings`** — all schemas (webhook contracts, LLM
  contracts, audit record), config, env.
- **Neon Postgres** (separate DB, ATS-only credentials) via **`asyncpg`** — thin
  plain-SQL layer, **no ORM**. Migrations are numbered `.sql` files in `db/migrations/`.
- **`openai`** SDK (`AsyncOpenAI`), Structured Outputs into pydantic models.
- **`asyncio`** + bounded semaphores — LLM + download concurrency; in-process grading
  worker (no external queue — the queue is a Postgres status column).
- **FastAPI** + **`uvicorn`** — webhook endpoint + admin UI shell.
- **Jinja2** server-rendered templates + one static CSS + vanilla JS — the review UI.
- **`rapidfuzz`** (school match), **`better-profanity`** (profanity gate),
  **`httpx`** + **`pypdf`** (resume fetch/extract), **`PyYAML`** (config).
- **`pytest`** + **`pytest-asyncio`**, **`ruff`**.
- `pandas` remains only for the replay tool's CSV conversion.

Do not introduce LangChain/orchestration frameworks, an ORM, an external task queue, or a
second datastore. The DB exception to v2's no-database rule is scoped: one Postgres, plain
SQL, three tables (`applications`, `llm_cache`, `events`).

### Model selection (pinned in `config.yaml`, swappable)

| Task | Job | Tier |
|---|---|---|
| A — GPA normalization fallback | mechanical | mini |
| C — Coursework decomposition | mechanical | mini |
| E — Resume signal extraction | mechanical | mini |
| B — Low-GPA explanation adequacy | judgment (can reject) | full |
| D — Required-essay grading | judgment (can reject) | full |
| F — Technical-essay bonus (NEW) | judgment (bonus-only) | full |

No o-series reasoning models. Verify exact IDs against OpenAI's catalog; swap in config only.

---

## Non-Negotiable Principles

- **Deterministic-first, fail-fast.** Cheap gates before any LLM call; first hard-gate hit
  stops the row with zero further token spend.
- **Hard rules decide rejections; the score only ranks survivors.** No score threshold
  accepts or rejects anyone.
- **Bonuses only add.** Essay 3, coursework, school, resume: absence is neutral; no code
  path deducts for a missing optional signal; no bonus rescues or manufactures a rejection.
- **Never silently reject.** Only an affirmative hard-gate failure produces `REJECTED`.
  Unscoreable → `NEEDS_REVIEW`. LLM parse failure on a required signal → `NEEDS_REVIEW`;
  on a bonus signal → 0 bonus + audit note.
- **Three outcomes only:** `REJECTED`, `RANKED`, `NEEDS_REVIEW`.
- **GPA rules (owner-settled):** threshold 3.3; hard floor 2.0 (no Task B below it); blank
  GPA + blank explanation ⇒ REJECTED (non-answer); unresolvable scale ⇒ NEEDS_REVIEW.
- **Profanity in ANY essay ⇒ NEEDS_REVIEW** (incl. the optional technical essay) — owner,
  2026-07-29. It stops the application, but a human confirms the flag: the gate is a word
  list, and a word list cannot tell a slur from ordinary vocabulary in context ("the
  transatlantic slave trade", "flange coupling", a surname). A false positive must cost a
  review, not an application. Gibberish in a required essay still **rejects** — that is a
  positive finding about the text, not a guess about intent. Gibberish/off-topic in the
  optional essay only zeroes its bonus.
- **Auditability is a feature.** Every applicant has a structured audit record explaining
  every gate and subscore; manual overrides carry `decided_by`.
- **Idempotent ingest.** Same `submission_id` + same content hash ⇒ no-op; changed content
  ⇒ re-grade (re-submissions are legal on the website).
- **Scoring model is owner-owned** (@SCORING.md, 150 max). Don't change weights, the 3.3
  threshold, or gate semantics without an owner decision.

## Security (this service holds minors' PII — treat every change accordingly)

- **Webhook:** static `X-ATS-Secret` header, constant-time compared against
  current+previous secrets (rotation). Missing/wrong ⇒ 401 and **touches nothing**; no
  secrets configured ⇒ fail closed. Body cap ⇒ 413; malformed ⇒ 422; never a 500 on bad
  input. No rate limiting (single authenticated source). HMAC signing is the deferred
  hardening step, and `webhook_auth.py` is the seam where it goes back in.
- **Fast ACK:** webhook handlers do verify → validate → upsert → 202 only. Grading belongs
  to the cron drain (their dispatcher aborts at 60 s; the ACK should be milliseconds).
- **Cron drain:** `POST /api/cron/drain` authenticates with `Authorization: Bearer
  $CRON_SECRET`, fails closed when unset, and is the only path that grades.
- **Admin UI:** shared-password login → stateless HMAC-signed session cookie
  (Secure/HttpOnly/SameSite=Lax), throttled attempts, default-deny on everything outside
  `auth.OPEN_PREFIXES`. The cookie is signed with `ADMIN_PASSWORD_HASH`, so changing the
  password invalidates every live session — that is the revocation lever.
- **Secrets** (env / gitignored `.env` only): `OPENAI_API_KEY`, `DATABASE_URL`,
  `ATS_WEBHOOK_SECRET[_PREVIOUS]`, `ADMIN_PASSWORD_HASH`, `CRON_SECRET`. Never in code,
  outputs, or logs.
- **Resume guardrails (unchanged law):** https-only exact-host allowlist (the website's R2
  host), no redirects, streaming size cap, fetch → extract → score → **discard** — resume
  bytes/text never reach the DB, an artifact, or a log. `resume.bonus_max: 0` is the kill
  switch and current default. Engine decided **in-house** (owner, 2026-07-27) — no
  third-party agent framework in this path. Enablement is post-pilot, and the allowlist must
  be re-pinned to the website's R2 host before it is flipped on.
- **Logging & events:** `submission_id` only — never essay/explanation/resume text.
- **Retention:** per-submission delete is built; the close-cycle export-then-purge action is
  designed but not, pending a settled retention policy. Never commit real applicant data;
  `data/` and `.env` stay gitignored; test fixtures are synthetic only.

---

## Project Structure

```
SRIP Application Filter/
├── CLAUDE.md                    # this file
├── SRIP_ATS_PRD_v3.md           # v3 functional spec (authoritative)
├── SRIP_Application_Filter_PRD.md  # v2 spec (superseded; carried-over sections)
├── SCORING.md                   # scoring model one-pager (mirrors config.yaml)
├── config.yaml                  # all tunables + model IDs (PRD v3 / SCORING.md)
├── vercel.json                  # function limits + the per-minute drain cron
├── db/migrations/*.sql          # numbered plain-SQL migrations
├── src/srip_filter/             # transport-agnostic core
│   ├── config.py · models.py    # + webhook payload contracts (versioned)
│   ├── db.py                    # asyncpg pool, plain-SQL store, content hashes
│   ├── ingest_webhook.py        # payload → ApplicantRow mapping
│   ├── gates/ · scoring/ · llm/ # pipeline stages (v2 lineage, v3 deltas)
│   ├── worker.py                # claim → grade → persist (driven by the cron drain)
│   ├── pipeline.py              # per-row fail-fast runner
│   └── outputs.py               # exports built from DB records
├── api/                         # FastAPI shell: webhook, cron drain, auth, admin UI
├── scripts/replay.py            # CSV/fixtures → authenticated POSTs (dev/integration)
└── tests/                       # mirrors src/; synthetic fixtures only
```

Keep the core free of FastAPI/HTTP concerns. The webhook handler and UI are thin shells.

---

## Workflow

- Tests alongside code, never after. Ambiguous logic → re-read the PRD section.
- The payload contract is frozen against the live question config; `tests/live_payload.py` is
  the single builder every suite uses, so a contract change happens in exactly one place.
- Local runs: `SRIP_DEV_FAKE_LLM=1` for zero-spend work, plus `SRIP_LOCAL_WORKER=1` if you
  want the in-process grading loop rather than calling the drain endpoint by hand.

## Code Style

- Type hints on all public signatures; pydantic v2 models for every boundary object.
- Pure functions for scoring/normalization math; side effects (DB, LLM, HTTP) only at
  marked boundaries. Prompts live in `llm/prompts/`, never inline.
- Every magic number in `config.yaml`. SQL lives in `db.py`/migrations, not scattered.
- No premature abstraction. Concrete before generic.

## LLM Usage Rules

- All tasks through `llm/client.py`; Structured Outputs first; retry-once then
  `NEEDS_REVIEW` (required signals) / 0-bonus (optional signals). Never silently reject
  on an LLM error.
- Temperature ≤ 0.2; model IDs pinned in config; `llm_cache` (Postgres) replaces the v2
  in-run cache — keyed `(task, sha256(input))`, so re-grades re-bill only changed fields.
- Bounded concurrency via semaphores; per-row try/except: one failure = one
  `NEEDS_REVIEW`/`error` row, never a stuck queue.
- Applicant text is always fenced data in prompts, never instructions.

## Testing Requirements

Every PRD v3 §10 invariant has an explicit deterministic test (no API spend;
`FakeLLMClient`): the six v2 invariants plus (7) unsigned/tampered/stale/replayed
requests never create or mutate rows, (8) identical re-delivery changes nothing and
re-bills nothing, (9) a per-row crash never blocks the queue. DB tests run against
`DATABASE_URL_TEST` (dev Neon branch). A small live suite stays behind `RUN_LLM_TESTS=1`.

## What NOT to Do

- Don't deduct for missing optional signals; don't let bonuses touch rejections; don't
  add an acceptance threshold; don't silently reject. (v2 law, unchanged.)
- Don't store resume bytes/text, ever. Don't log PII. Don't weaken the webhook-secret,
  cron-bearer, or session auth paths. Don't put PII in `events`.
- Don't run LLM calls before the deterministic gates. Don't grade in the webhook handler.
- Don't store rank — it's computed at read time, per cohort.
- Don't change scoring weights / GPA thresholds / gate semantics without an owner decision.
- Don't touch the partner's website repo, and don't build on assumed changes there — the ATS
  adapts to the contract it actually receives.
- Don't reintroduce per-process state (in-memory sessions, registries, polling loops): the
  service runs as many short-lived serverless instances.
- Don't add an ORM, external queue, second datastore, or agent framework.
- Don't commit `data/`, `.env`, results files, or any real applicant content.

## Commit Conventions

- One logical change per commit, tests included; run `uv run pytest` + `uv run ruff check`
  before every commit; push after every atomic change.
- Format: `[pN] <what changed>` for phase work (e.g. `[p12] signed-cookie sessions`),
  `[infra]` / `[docs]` otherwise.
- No AI co-author trailers in commit messages (owner preference, 2026-07-04).
- Check `.gitignore` before every commit — never commit PII.

## Session Notes

The owner keeps planning notes, the decisions log, and partner-coordination records in
working files that are **deliberately unpublished** (see `.gitignore`). When they are present
locally, read them at session start and update them before ending a session with meaningful
work — structural facts only, never applicant content. When they are absent, this file plus
PRD v3 are the whole picture.
