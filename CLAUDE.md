# SRIP ATS — Continuous Application Filtering Service (CS Track)

A **continuous, persistent, secured** webhook-receiver ATS. The partner-owned application
website POSTs one authenticated JSON payload per application; this service validates,
stores, grades asynchronously, and gives staff a session-gated review UI over the live
cohort. It does exactly two things per application: **reject** on deterministic hard-gate
failures, and **score + rank** every survivor within its cohort. It does **not** decide
acceptances.

**Full functional spec: @SRIP_ATS_PRD_v3.md** — read the relevant section before any logic
decision not covered here. **Scoring model: @SCORING.md** (mirrors `config.yaml`, the machine
source of truth). **Deployment: @README.md** → Deployment.

**Repository:** https://github.com/dominicgodfrey/srip-application-screen.git

> ### The four facts most often got wrong
> 1. **Payload:** one combined payload carrying every sector, plus an
>    `ats_run: ("essays"|"resume"|"finaid")[]` selector saying which graders to run. A delivery
>    that doesn't request essays is stored terminally and never graded.
> 2. **Webhook auth:** a static `X-ATS-Secret` header, constant-time compared, with
>    current+previous secrets for rotation. `api/webhook_auth.py` is the only place that rule
>    lives.
> 3. **Hosting:** Vercel serverless, in the partner team's project and managed by them. Grading
>    is a **per-minute cron drain** over the Postgres queue (`POST /api/cron/drain`), never an
>    always-on loop; sessions are stateless signed cookies and the login throttle counts
>    `events` rows, so nothing depends on one process.
> 4. **No essay word-count gate exists.** The site rejects length violations at submit and sends
>    no bounds, so a check here could only fire on a stale local config. Word counts are audit
>    data only.
>
> Finaid is **stored but never scored**.

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
- `pandas` is a **dev dependency**, used only by `srip_filter/ingest.py` (the CSV reader) for
  `scripts/replay.py`. Nothing the deployed function imports touches it — keep it that way:
  `ApplicantRow` lives in `applicant.py` precisely so the scoring layer never reaches `ingest`.

Do not introduce LangChain/orchestration frameworks, an ORM, an external task queue, or a
second datastore. The persistence footprint is deliberately minimal and stays that way: one
Postgres, plain SQL, three tables (`applications`, `llm_cache`, `events`).

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
  input. No rate limiting (single authenticated source). `webhook_auth.py` is the only place
  the verification rule lives — the replay tool imports the same module.
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
- **Resume guardrails (absolute):** https-only exact-host allowlist, no redirects, streaming
  size cap, fetch → extract → score → **discard** — resume bytes/text never reach the DB, an
  artifact, or a log. Scoring is in-house (Task E extracts, `config.yaml` prices); no
  third-party agent framework belongs in this path. **`resume.bonus_max: 0` is the current
  setting and disables the stage entirely.** The weights are priced for the 0–25 band; the one
  thing outstanding before `bonus_max` can be raised to 25 is re-pinning
  `resume.allowed_url_hosts` to the website's R2 host — the entry there is the retired Fillout
  value, and the allowlist is exact-host, so an unpinned host simply never fetches.
- **Logging & events:** `submission_id` only — never essay/explanation/resume text.
- **Retention:** per-submission delete and **bulk purge** are built — the latter is a
  session-gated bottom-corner control (`GET /api/admin/purge-preview` →
  `POST /api/admin/purge`), scoped to one cohort or every cohort, guarded by an
  `expected_count` that 409s if the live count moved while the dialog was open, and tombstoned
  in `events` with counts only. A full wipe also truncates `llm_cache`, whose `output` holds
  model commentary derived from essay text. The **export** half of PRD §9's close-cycle is
  still not built: purge takes no backup, which is why the dialog says so. Never commit real
  applicant data; `data/` and `.env` stay gitignored; test fixtures are synthetic only.

---

## Project Structure

```
SRIP Application Filter/
├── CLAUDE.md                    # this file
├── SRIP_ATS_PRD_v3.md           # functional spec (authoritative)
├── SCORING.md                   # scoring model one-pager (mirrors config.yaml)
├── config.yaml                  # all tunables + model IDs (spec / SCORING.md)
├── vercel.json                  # function limits + the per-minute drain cron
├── db/migrations/*.sql          # numbered plain-SQL migrations
├── src/srip_filter/             # transport-agnostic core
│   ├── config.py · models.py    # + webhook payload contracts (versioned)
│   ├── db.py                    # asyncpg pool, plain-SQL store, content hashes
│   ├── applicant.py             # ApplicantRow — what every stage grades (no deps)
│   ├── ingest_webhook.py        # payload → ApplicantRow mapping
│   ├── ingest.py                # CSV reader; replay tool ONLY, never imported by api/
│   ├── gates/ · scoring/ · llm/ # pipeline stages
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

- All tasks through `llm/client.py`; Structured Outputs first. **Failures split by kind:** a
  transient one (429 / timeout / connection / 5xx) is retried to `llm.max_attempts` with
  exponential backoff — a rate limit is our problem, not the applicant's, and must never become
  a review item. A terminal one (unparseable output) gets one retry, then `NEEDS_REVIEW` for a
  required signal / 0 bonus for an optional one. Never silently reject on an LLM error.
- Requests are paced against `llm.tokens_per_minute` by a token bucket, so a burst is slow
  rather than lossy. **That value must match the deploying account's real TPM limit.**
- Temperature ≤ 0.2; model IDs pinned in config; `llm_cache` (Postgres) is keyed
  `(task, sha256(input))`, so re-grades re-bill only changed fields.
- Bounded concurrency via semaphores; per-row try/except: one failure = one
  `NEEDS_REVIEW`/`error` row, never a stuck queue.
- Applicant text is always fenced data in prompts, never instructions.

## Testing Requirements

Every invariant in the spec's §10 has an explicit deterministic test, with no API spend
(`FakeLLMClient`) — including that wrong-secret requests never create or mutate rows, that
identical re-delivery changes nothing and re-bills nothing, and that a per-row crash never
blocks the queue. DB tests run against `DATABASE_URL_TEST` and skip cleanly when it is unset.
**No test ever calls a real model.** The OpenAI boundary is exercised by hand via
`scripts/replay.py` against a locally-run server (see @README.md → Dev quickstart).

## What NOT to Do

- Don't deduct for missing optional signals; don't let bonuses touch rejections; don't
  add an acceptance threshold; don't silently reject.
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
- Format: `[pN] <what changed>` for feature work (e.g. `[p12] signed-cookie sessions`),
  `[infra]` / `[docs]` otherwise.
- No AI co-author trailers in commit messages (owner preference).
- Check `.gitignore` before every commit — never commit PII.

## Session Notes

The owner keeps planning notes, the decisions log, and partner-coordination records in
working files that are **deliberately unpublished** (see `.gitignore`). When they are present
locally, read them at session start and update them before ending a session with meaningful
work — structural facts only, never applicant content. When they are absent, this file plus
PRD v3 are the whole picture.
