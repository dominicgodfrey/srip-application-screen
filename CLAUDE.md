# SRIP ATS v3 — Continuous Application Filtering Service (CS Track)

A **continuous, persistent, secured** webhook-receiver ATS. The partner-owned
thinkNeuroWebsite POSTs one signed JSON payload per application; this service validates,
stores, grades asynchronously, and gives staff a session-gated review UI over the live
cohort. It does exactly two things per application: **reject** on deterministic hard-gate
failures, and **score + rank** every survivor within its cohort. It does **not** decide
acceptances.

**Full functional spec: @SRIP_ATS_PRD_v3.md** — read the relevant section before any logic
decision not covered here. `SRIP_Application_Filter_PRD.md` is the superseded v2 spec,
still authoritative where v3 says semantics carry over (GPA §6, task contracts §8, audit
record §9). **Scoring model: @SCORING.md** (mirrors `config.yaml`, the machine source of
truth). External dependencies on the website team: **@WEBSITE_ASKS.md**.

**History:** the v2 stateless Fillout-CSV batch system is frozen on the
`v2-fillout-batch` branch. v3 (this) is `main`. The stateless→persistent reversal was an
explicit owner decision (2026-07-04) — see PLAN.md Notes log.

**Repository:** https://github.com/dominicgodfrey/srip-application-screen.git

---

## Tech Stack

Settled (do not re-litigate without a decision in PLAN.md's Notes log):

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
- **Profanity in ANY essay rejects** (incl. the optional technical essay). Gibberish/
  off-topic in the optional essay only zeroes its bonus.
- **Auditability is a feature.** Every applicant has a structured audit record explaining
  every gate and subscore; manual overrides carry `decided_by`.
- **Idempotent ingest.** Same `submission_id` + same content hash ⇒ no-op; changed content
  ⇒ re-grade (re-submissions are legal on the website).
- **Scoring model is owner-owned** (@SCORING.md, 150 max). Don't change weights, the 3.3
  threshold, or gate semantics without an owner decision recorded in PLAN.md.

## Security (this service holds minors' PII — treat every change accordingly)

- **Webhook:** HMAC-SHA256 (`X-ATS-Timestamp` + `X-ATS-Signature` over
  `ts + "." + raw_body`), constant-time compare, ±300 s replay window, current+previous
  secret rotation. Unsigned/stale/tampered ⇒ 401 and **touches nothing**. Body cap ⇒ 413;
  malformed ⇒ 422; never a 500 on bad input. No rate limiting (single authenticated source).
- **Fast ACK:** webhook handlers do verify → validate → upsert → 202 only. Grading is the
  worker's job (the website aborts at 15 s).
- **Admin UI:** shared-password login → server session, secure/HTTP-only cookie, throttled
  attempts; `require_admin` on everything except `/health` and the webhook.
- **Secrets** (env / gitignored `.env` only): `OPENAI_API_KEY`, `DATABASE_URL`,
  `ATS_WEBHOOK_SECRET[_PREVIOUS]`, `ADMIN_PASSWORD_HASH`, session key. Never in code,
  outputs, or logs.
- **Resume guardrails (unchanged law):** https-only exact-host allowlist (the website's R2
  host), no redirects, streaming size cap, fetch → extract → score → **discard** — resume
  bytes/text never reach the DB, an artifact, or a log. `resume.bonus_max: 0` is the kill
  switch and current default (engine decision pending, WEBSITE_ASKS #11).
- **Logging & events:** `submission_id` only — never essay/explanation/resume text.
- **Retention:** per-submission delete + close-cycle export-then-purge exist by design;
  policy finalization pending WEBSITE_ASKS #13. Never commit real applicant data;
  `data/` and `.env` stay gitignored; test fixtures are synthetic only.

---

## Project Structure

```
SRIP Application Filter/
├── CLAUDE.md                    # this file
├── SRIP_ATS_PRD_v3.md           # v3 functional spec (authoritative)
├── SRIP_Application_Filter_PRD.md  # v2 spec (superseded; carried-over sections)
├── SCORING.md                   # scoring model one-pager (mirrors config.yaml)
├── WEBSITE_ASKS.md              # asks/discussions for the website team + status
├── PLAN.md                      # phase tracker + decisions log (session memory)
├── config.yaml                  # all tunables + model IDs (PRD v3 / SCORING.md)
├── db/migrations/*.sql          # numbered plain-SQL migrations
├── src/srip_filter/             # transport-agnostic core
│   ├── config.py · models.py    # + webhook payload contracts (versioned)
│   ├── db.py                    # asyncpg pool, plain-SQL store, content hashes
│   ├── ingest_webhook.py        # payload → ApplicantRow mapping
│   ├── gates/ · scoring/ · llm/ # pipeline stages (v2 lineage, v3 deltas)
│   ├── worker.py                # DB-queue grading worker
│   ├── pipeline.py              # per-row fail-fast runner
│   └── outputs.py               # exports built from DB records
├── api/                         # FastAPI shell: webhook, auth, admin UI
├── scripts/replay.py            # CSV/fixtures → signed POSTs (dev/integration/migration)
└── tests/                       # mirrors src/; synthetic fixtures only
```

Keep the core free of FastAPI/HTTP concerns. The webhook handler and UI are thin shells.

---

## Workflow

- Read `PLAN.md` at session start; confirm the active phase (P0–P8); one phase at a time.
- Tests alongside code, never after. Ambiguous logic → re-read the PRD section.
- Contract-affected work (payload fields) builds against the PRD v3 §2.2 proposed contract
  with fixtures until WEBSITE_ASKS 2/3/5/6 are answered; the contract freezes at P2.
- Update PLAN.md before ending any session with meaningful work (completed items + commit
  SHAs, verification commands, active sub-task, decisions log).

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
- Don't store resume bytes/text, ever. Don't log PII. Don't weaken the HMAC or session
  auth paths. Don't put PII in `events`.
- Don't run LLM calls before the deterministic gates. Don't grade in the webhook handler.
- Don't store rank — it's computed at read time, per cohort.
- Don't change scoring weights / GPA thresholds / gate semantics without an owner
  decision in PLAN.md.
- Don't touch the thinkNeuroWebsite repo — changes there go through WEBSITE_ASKS.md.
- Don't add an ORM, external queue, second datastore, or agent framework.
- Don't commit `data/`, `.env`, results files, or any real applicant content.

## Commit Conventions

- One logical change per commit, tests included; run `uv run pytest` + `uv run ruff check`
  before every commit; push after every atomic change.
- Format: `[pN] <what changed>` for phase work (e.g. `[p2] HMAC verification middleware`),
  `[infra]` / `[plan]` otherwise.
- No AI co-author trailers in commit messages (owner preference, 2026-07-04).
- Check `.gitignore` before every commit — never commit PII.

## PLAN.md Protocol

PLAN.md is the session-to-session memory. At session start: CLAUDE.md → PLAN.md → the
relevant PRD v3 section → confirm active sub-task → begin only that. Before ending a
session: move completed items (with SHAs), add verification commands, update the active
sub-task, record non-obvious decisions in the Notes log (structural facts only — never
applicant content). When in doubt, update PLAN.md.
