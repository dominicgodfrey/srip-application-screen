# SRIP Track 2 — Application Filtering System

A stateless service that filters and ranks applications to the SRIP Track 2 (Software
Engineering) program. It does exactly two things: (1) **reject** applications that fail
deterministic hard-gate quality checks, and (2) **score and rank** every survivor. It does
**not** decide acceptances — that is a deferred downstream step that consumes this system's
ranked output.

Input is a CSV export from Fillout (466 rows / 29 columns in the reference dataset; design
for up to ~2000). Output is a set of downloadable result files. **Nothing is persisted
between sessions** — all input comes from the uploaded CSV, and results are returned to the
user to save manually.

**Full functional spec: @SRIP_Application_Filter_PRD.md** — read the relevant section
before making any logic decision not covered here. The PRD governs *what* the system
decides; this file governs *how* it is built.

**Repository:** https://github.com/dominicgodfrey/srip-application-screen.git

---

## Tech Stack

Settled (do not re-litigate without a decision in PLAN.md's Notes log):

- **Python 3.11+**, managed with **`uv`**.
- **`pandas`** — CSV ingest and dedup.
- **`pydantic` v2** + **`pydantic-settings`** — all schemas (LLM contracts, audit record), config, and env.
- **`openai`** SDK (`AsyncOpenAI`) — all LLM tasks, cloud-only. **OpenAI Structured Outputs**
  (strict `json_schema` parsed directly into pydantic models) is the primary JSON mechanism.
- **`asyncio`** + bounded `Semaphore` — concurrency for LLM calls.
- **`rapidfuzz`** — school name fuzzy matching.
- **`better-profanity`** — profanity gate. Default list for now; curated slur list +
  medical/anatomical allowlist tracked in `openissue.md`.
- **`PyYAML`** — load `config.yaml` (validated by pydantic).
- **`pytest`** + **`pytest-asyncio`**, **`ruff`** — tests and lint/format.
- **FastAPI** + **`uvicorn`** — thin stateless API layer (backend deployment).
- Future, not now: **React + Vite** SPA frontend; **`pdfplumber`** for the deferred resume parser.

Do not introduce additional frameworks (LangChain, orchestration tools, a database, a task
queue, an ORM) without recording the reason in PLAN.md first. This is a small batch system,
not a platform.

### Model selection (pinned in `config.yaml`, swappable)

| Task | Job | Default model |
|---|---|---|
| A — GPA normalization | mechanical extraction | `gpt-4.1-mini` |
| C — Coursework decomposition | mechanical extraction | `gpt-4.1-mini` |
| B — Low-GPA explanation adequacy | judgment (can reject) | `gpt-4.1` |
| D — Essay grading / gibberish / relevance | judgment (can reject) | `gpt-4.1` |

- Mechanical tasks (A, C) use the mini tier; judgment tasks that can reject an applicant
  (B, D) use the full tier. **Do not use o-series reasoning models** — overkill and costly here.
- Verify exact model IDs against OpenAI's current catalog when building; swap in CONFIG only.

---

## Non-Negotiable Principles

From PRD §0. If an implementation choice conflicts with any of these, the principle wins.

- **Deterministic-first, fail-fast.** Cheap deterministic gates run before any LLM call. The
  moment an application hits a hard-reject gate, stop and spend zero LLM tokens on it.
- **Hard rules decide rejections; the score only ranks survivors.** Rejection is rule-based
  and binary. The additive score is computed *only* for gate-survivors and used solely to rank.
  No score threshold accepts or rejects anyone.
- **Non-required signals can only add, never subtract.** Resume, top-school, relevant
  coursework are bonuses. Their absence is neutral. No code path may deduct points for a
  missing optional signal.
- **Never silently reject.** The only path to `REJECTED` is an affirmative hard-gate failure.
  Anything unscoreable (unresolvable GPA scale, parse failure, unchecked affirmation) goes to
  `NEEDS_REVIEW`.
- **GPA threshold is 3.0 (B average).** Both the deny line and the bottom of the gradient. Do
  not raise or lower it for high schoolers (78% of applicants are high-schoolers).
- **Three outcomes only: `REJECTED`, `RANKED`, `NEEDS_REVIEW`.** No accept/waitlist here.
- **Auditability is a feature.** Every applicant produces a structured decision record (§9)
  explaining every gate and subscore. It is returned to the user, never stored server-side.

---

## Privacy & Security

This system processes **minors' PII** (names, emails, GPAs, personal-essay content). Treat it
accordingly.

- **Stateless by design.** Uploaded CSVs are processed in memory and discarded after the
  response. No applicant data is written to disk or a database. A new session starts clean.
  In-memory job/results registries are transient (TTL or discard-after-download); if the host
  restarts or the page refreshes mid-run, the job is lost — that is the intended behavior.
- **Secrets.** `OPENAI_API_KEY` comes from env / a gitignored `.env`. Never hard-code it,
  never write it into outputs, never log it.
- **OpenAI data retention.** Confirm the account is set to zero/minimal retention so essays
  and GPAs are not held by the provider. API inputs are not used for training by default.
- **Logging.** LLM-call logging (inputs/outputs/model) is for **local/dev debugging only**.
  In any hosted deployment do not persist PII-bearing logs — log `submission_id`, never essay
  or explanation text.
- **Input validation.** Cap upload size and row count (~2000). Validate CSV headers against
  the §2 data contract. Reject malformed uploads gracefully (not a 500).
- **No auth required initially** — nothing is stored, so there is little at risk from the
  system itself. Serve over HTTPS regardless (results contain PII in transit).
- **Version control.** `data/` and `.env` are gitignored. Never commit a real CSV, a results
  file, `.env`, or anything containing applicant content. Test fixtures use synthetic data only.

---

## Project Structure

```
SRIP Application Filter/
├── CLAUDE.md                       # this file
├── SRIP_Application_Filter_PRD.md  # functional spec
├── PLAN.md                         # phase progress tracker
├── openissue.md                    # owner inputs still needed (API key, curated profanity list, etc.)
├── pyproject.toml                  # uv-managed deps
├── config.yaml                     # tunable CONFIG (PRD §10.3) + model IDs
├── .env                            # OPENAI_API_KEY (gitignored)
├── .gitignore
├── resources/                      # committed, non-PII
│   ├── schools.json                # curated Top-20 US / Top-50 Intl (PRD §13)
│   └── profanity.txt               # slur/profanity list + medical allowlist
├── src/srip_filter/                # transport-agnostic core (all logic lives here)
│   ├── config.py                   # pydantic-settings + yaml load
│   ├── models.py                   # pydantic: LLM contracts (A/B/C/D) + AuditRecord
│   ├── ingest.py                   # Stage 0 — load + dedup
│   ├── gates/
│   │   ├── essays.py               # Stage 1 — length, profanity, cheap gibberish heuristics
│   │   └── gpa.py                  # Stages 2–3 — normalize + gate
│   ├── scoring/
│   │   ├── essays.py               # Stage 4 — Task D (relevance gate + quality + gibberish)
│   │   ├── coursework.py           # Stage 5 — Task C
│   │   ├── school.py               # Stage 7 — rapidfuzz match
│   │   ├── resume.py               # Stage 6 — inert stub (returns 0), clearly TODO
│   │   └── aggregate.py            # Stage 8 — compose score + rank
│   ├── llm/
│   │   ├── client.py               # AsyncOpenAI wrapper: structured outputs, in-run cache, retry
│   │   └── prompts/                # one template per task (A/B/C/D)
│   ├── pipeline.py                 # orchestration: ordered fail-fast batch runner
│   └── outputs.py                  # Stage 9 — decisions.jsonl + ranked/rejected/needs_review.csv + summary.json
├── api/                            # thin FastAPI shell over the core
│   └── main.py
└── tests/                          # mirrors src/ ; synthetic fixtures only
```

Keep the core (`src/srip_filter/`) free of FastAPI/HTTP concerns. The API and the future UI
are thin shells that call `pipeline.grade_batch(...)`.

---

## Workflow

- Read `PLAN.md` at session start to find the active phase/sub-task.
- One phase at a time, in pipeline order. Do not build Stage 4 grading while Stage 1 gates are
  incomplete — fail-fast ordering depends on earlier stages existing.
- Tests alongside code, not after.
- When a logic question is ambiguous, re-read the relevant PRD section rather than guessing.
- Confirm the active sub-task with the user if PLAN.md is ambiguous.

---

## Code Style

- Type hints on all public functions and class signatures.
- **pydantic v2 models** for every node: the four LLM contracts (Task A/B/C/D outputs) and the
  `AuditRecord`. Fields match the PRD schemas (§8, §9) exactly.
- Pure functions for all scoring/normalization math; side effects (LLM I/O, file writes) only
  at clearly marked boundaries.
- Prompt templates live in `src/srip_filter/llm/prompts/`, never inline in business logic.
- Centralize every magic number in `config.yaml` — no hard-coded thresholds, weights, or
  model IDs in logic.
- Docstrings on public interfaces; inline comments only where intent isn't obvious.
- No premature abstraction. Concrete before generic.

---

## LLM Usage Rules

- All tasks go through `llm/client.py`. Treat the LLM as a replaceable I/O boundary.
- **Structured Outputs first:** pass the pydantic model as the response schema and parse
  directly. Keep the §8 fallback — on a refusal/cutoff/validation failure, retry once, then
  route the applicant to `NEEDS_REVIEW` with reason `"LLM_PARSE_FAILURE"`. **Never silently
  reject on an LLM error.**
- **Temperature ≤ 0.2** for repeatability. Pin model IDs in `config.yaml`.
- **In-run cache** keyed by `(task, sha256(input_text))`: dedups identical inputs and makes
  retries free *within a single run*. It does **not** persist across runs (stateless design),
  so re-uploading the same CSV re-bills — this is accepted.
- **Bounded concurrency** via an `asyncio.Semaphore`; rely on the SDK's built-in transport
  retries for 429/5xx, add backoff if needed. Per-row `try/except` so one failure becomes a
  `NEEDS_REVIEW` row rather than aborting the batch ("when grading begins, it finishes").
- Log every LLM call (inputs, outputs, model) **locally for dev only** — see Privacy.

---

## Testing Requirements

Every PRD §12 invariant must have an explicit test (deterministic, no API spend):

1. No optional-signal absence (resume/school/coursework) ever reduces `final_score`.
2. No bonus changes a `REJECTED` outcome.
3. Every `REJECTED` record names the failing gate in `primary_reason`.
4. Normalized GPA below 3.0 never produces points without an approved Task B explanation, and
   never scores above the bottom of the gradient band.
5. Ranking is stable across reruns (deterministic tiebreaker; in-run cache hits identical).
6. Nothing unscoreable is rejected — unresolvable GPA / unchecked affirmation / parse failure
   → `NEEDS_REVIEW`, never `REJECTED`.

Plus: unit tests per module, integration test of the full pipeline on a synthetic CSV.
The LLM client is mocked with a fake in unit tests. A small live suite is gated behind
`RUN_LLM_TESTS=1` and run sparingly against the real API.

---

## What NOT to Do

- Don't deduct points for a missing optional signal (resume, school, coursework). Absence is neutral.
- Don't let any bonus manufacture or rescue a rejection. Rejections are gated before scoring.
- Don't silently reject. Unscoreable → `NEEDS_REVIEW`.
- Don't add an acceptance/waitlist threshold. This system only rejects and ranks (PRD §11).
- Don't raise/lower the 3.0 GPA threshold for high-schoolers.
- Don't flag merely-awkward or ESL grammar as gibberish — that's a soft penalty in Task D, not a gate.
- Don't persist applicant data to disk or a database. Stateless only.
- Don't build the resume parser — Stage 6 stays an inert, clearly-labeled `resume_bonus = 0` stub.
- Don't run LLM calls before the deterministic gates that precede them. Fail-fast ordering is load-bearing.
- Don't commit `data/`, `.env`, results files, or any real applicant content.
- Don't add a database, queue, or orchestration framework without recording the reason in PLAN.md.

---

## Commit Conventions

**Remote:** https://github.com/dominicgodfrey/srip-application-screen.git
(Local `git init` + a `.gitignore` covering `data/` and `.env`, wired to the remote above as
`origin`, is Phase 0.1.)

- One logical change per commit; include the tests with the code they cover.
- Format: `[stage-N] <what changed>` for pipeline-scoped work, `[infra] ...` / `[plan] ...` otherwise.
- **Push after every atomic change.** An atomic change = one self-contained, working commit
  (code + its passing tests). Commit it and `git push` immediately — don't batch local commits.
- Run tests before every commit. Check `.gitignore` before every commit — never commit PII.

---

## PLAN.md Protocol

`PLAN.md` is the session-to-session memory of this project. Claude Code does not remember
previous sessions; PLAN.md is how continuity happens.

**At session start:** read CLAUDE.md → read PLAN.md → read the relevant PRD section → confirm
the active sub-task → begin only that sub-task.

**Before ending any session with meaningful work:**
- Move completed items to "Completed" (with commit short-SHA once git exists).
- Add a verification command to "How to Verify Completed Work."
- Update "Active Sub-Task" / "In Progress."
- Record any non-obvious decision in "Notes / Decisions Log" (structural facts only — never
  real applicant content).
- When in doubt, update PLAN.md rather than not.
