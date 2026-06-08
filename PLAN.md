# Project Plan — SRIP Track 2 Application Filtering System

Session-to-session memory. See `CLAUDE.md` for how to build, `SRIP_Application_Filter_PRD.md`
for what to build.

## Current Phase
Phase 1 — Ingest + dedup (Stage 0)

## Active Sub-Task
Phase 0 complete (0.1–0.4). Next action: Phase 1 — `src/srip_filter/ingest.py`: load the CSV
against the §2 data contract and deduplicate (email primary; flag — don't merge — duplicate
name-pairs that lack a shared email).

---

## Phase Map

Phases follow the PRD pipeline (Stages 0–9), front-loaded with scaffolding and back-loaded
with the API. Build in order — fail-fast ordering means later stages depend on earlier ones.

- **Phase 0 — Scaffolding & config**
  - 0.1 `uv` project, `pyproject.toml`, deps, `ruff`, `.gitignore` (covers `data/`, `.env`), `git init`
  - 0.2 `config.yaml` (PRD §10.3 + model IDs) loaded & validated via pydantic-settings (`config.py`)
  - 0.3 `models.py` — pydantic v2 schemas for Task A/B/C/D outputs + `AuditRecord` (PRD §8, §9)
  - 0.4 `llm/client.py` — `AsyncOpenAI` wrapper: structured outputs, in-run cache, bounded
        concurrency, retry→`NEEDS_REVIEW` fallback; fake client for tests
- **Phase 1 — Ingest + dedup (Stage 0)**
  - pandas load against the §2 data contract; dedup by email (primary) + name-pair (flag, don't merge)
- **Phase 2 — Essay deterministic gates (Stage 1)**
  - Length gate (hard_min/hard_max → REJECTED; soft penalty band); profanity gate
    (`better-profanity` + slur list + medical allowlist); cheap gibberish heuristics
    (entropy / consonant runs / repeated chars — no dictionary)
- **Phase 3 — GPA normalization + gate (Stages 2–3)**
  - Deterministic parsing (4.0 / %, /5, /10, label-strip); Task A for ambiguous; Task B for
    sub-3.0 + explanation; unscalable → `NEEDS_REVIEW`
- **Phase 4 — Essay LLM grading (Stage 4, Task D)**
  - Gibberish check first, then relevance gate (off-topic → REJECTED), then quality score;
    soft length/grammar penalties applied
- **Phase 5 — Coursework bonus (Stage 5, Task C)**
  - Decompose courses, classify cs/math/data/other, normalize grades, <80% ignored, additive cap
- **Phase 6 — School bonus + resume stub (Stages 7, 6)**
  - `rapidfuzz` match against `resources/schools.json`; resume = inert `0` stub (clearly TODO)
- **Phase 7 — Aggregation, ranking, outputs (Stages 8–9)**
  - Compose `final_score`; deterministic tiebreaker; emit `decisions.jsonl`, `ranked.csv`,
    `rejected.csv`, `needs_review.csv`, `summary.json`; all §12 invariant tests
- **Phase 8 — Orchestration (`pipeline.grade_batch`)**
  - Ordered fail-fast runner; per-row error isolation; bounded async; integration test on synthetic CSV
- **Phase 9 — API layer (FastAPI, stateless)**
  - Upload CSV → background job (in-memory registry) → progress poll → downloadable results;
    nothing persisted; input validation + size caps
- **Phase 10 — Frontend SPA (FUTURE, not an immediate concern)**
  - React + Vite: upload, render each application's audit record on open, download results CSV

---

## Completed
- [x] Pre-work — stack decisions captured in CLAUDE.md; PRD reviewed.
- [x] Phase 0.1 — uv project scaffold: pyproject + deps, ruff, pytest, src/tests skeleton,
      .gitignore (data/ + .env), git init + remote, pushed (commit: 8aacb28).
- [x] Phase 0.2 — config.yaml (PRD §10.3 + pinned model IDs) + pydantic-settings loader with
      strict validation and Secrets (OPENAI_API_KEY from .env); tests (commit: 947f24c).
- [x] Phase 0.3 — pydantic v2 schemas: LLM contracts (Task A/B/C/D) + AuditRecord, strict +
      structured-output-ready (additionalProperties:false, all-required); tests (commit: e6867b5).
- [x] Phase 0.4 — LLM client: AsyncOpenAI structured outputs parsed into the contracts, in-run
      cache, bounded-concurrency semaphore, retry-once -> LLMParseFailure; FakeLLMClient + tests
      (commit: 7c9bae1).

## In Progress
- (none)

## Next Up
- [ ] Phase 1 — ingest.py: CSV load (§2 data contract) + dedup (email primary, name-pair flag)
- [ ] Phase 2 — essay deterministic gates (length, profanity, cheap gibberish)
- [ ] Phase 3 — GPA normalization + gate (deterministic + Task A/B)

## How to Verify Completed Work
(Fill in one command per sub-task as it lands.)
- Phase 0.1: `uv sync && uv run pytest -q && uv run ruff check .`
- Phase 0.2: `uv run pytest tests/test_config.py`
- Phase 0.3: `uv run pytest tests/test_models.py`
- Phase 0.4: `uv run pytest tests/llm/test_client.py`
- Phase 2:   `uv run pytest tests/gates/test_essays.py`
- Phase 7:   `uv run pytest tests/scoring/test_aggregate.py` (covers all §12 invariants)
- Phase 8:   `uv run pytest tests/test_pipeline.py` (synthetic CSV end-to-end)

---

## Notes / Decisions Log

Structural facts only — never real applicant content.

- **LLM provider = OpenAI** (cloud, all tasks). PRD text says "Anthropic SDK"; superseded by
  owner decision. Use OpenAI Structured Outputs (strict json_schema → pydantic) as the primary
  JSON mechanism; keep PRD §8 retry-once→`NEEDS_REVIEW` fallback.
- **Models:** `gpt-4.1-mini` for Tasks A & C (extraction); `gpt-4.1` for Tasks B & D
  (judgment that can reject). Pinned in `config.yaml`. No o-series. IDs to be verified against
  OpenAI's current catalog at build time.
- **Gibberish:** primary detection moved into LLM Task D (owner decision); Stage 1 keeps only
  cheap deterministic heuristics (entropy / consonant runs / repeated chars). The PRD's
  dictionary-hit-ratio check is dropped → no English-dictionary dependency, lower ESL
  false-positive risk. Tradeoff: subtly-gibberish essays cost one LLM call instead of a free gate.
- **Stateless:** no persistence between sessions (owner decision). The PRD's persistent
  idempotency cache becomes an **in-run** in-memory cache only. Consequence: re-running the
  same CSV re-bills — accepted. Auditability is delivered via returned/downloadable output,
  not server-side storage.
- **Robustness:** "when grading begins, it finishes" = bounded async + per-row try/except
  (one bad row → `NEEDS_REVIEW`) + SDK retries. No resume-after-refresh; an interrupted run is
  abandoned and nothing is saved.
- **Deployment:** thin FastAPI shell over a transport-agnostic core; long runs use a
  background job + progress polling (free-tier HTTP timeouts can't hold a multi-minute
  request). Target free/cheap hosting (Render / Railway / Fly.io). No DB, no auth initially.
- **Scale target:** up to ~2000 rows in memory; if it ever grows beyond that, revisit a real
  job queue (arq/RQ) — not before.
- **Git:** remote is https://github.com/dominicgodfrey/srip-application-screen.git. Convention
  (CLAUDE.md): push after every atomic change — one self-contained, tested commit then push.
- **School lists:** frozen for Summer 2026 in `resources/schools.json` (Top-20 US, Top-50 Intl).
  Parenthetical abbreviations captured as `aliases` to aid rapidfuzz recall; a school appearing
  on both lists takes the higher bonus. Source: U.S. News (Best National / Best Global).
- **Profanity:** using better-profanity's default list until the owner supplies a curated slur
  list + medical/anatomical allowlist (openissue.md #3).
- **`openissue.md`** added at project root as the owner's running list of inputs to provide.

## Owner-Supplied Dependencies (full detail in `openissue.md`)
- [x] `resources/schools.json` — Top-20 US + Top-50 International (source: U.S. News), frozen for Summer 2026.
- [~] Profanity list — using `better-profanity` DEFAULT list for now (owner approved). Curated
      slur list + medical/anatomical allowlist still needed (openissue.md #3).
- [ ] `OPENAI_API_KEY` in `.env` (openissue.md #1).
- [ ] OpenAI account set to zero/minimal data retention (openissue.md #2).
- [x] GPA threshold — settled at 3.0 (PRD §1). No decision needed.
- [~] Resume parsing — explicitly deferred; Stage 6 stays an inert stub.
