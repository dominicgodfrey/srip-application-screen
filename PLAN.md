# Project Plan — SRIP Track 2 Application Filtering System

Session-to-session memory. See `CLAUDE.md` for how to build, `SRIP_Application_Filter_PRD.md`
for what to build.

## Current Phase
Phase 2 — Essay deterministic gates (Stage 1)

## Active Sub-Task
Phase 1 complete (all of Stage 0 ingest). Phase 2 now broken into 2.1–2.4 (see Phase Map).
Next action: Phase 2.1 — word count + length gate in `src/srip_filter/gates/essays.py`:
`word_count` (`re.findall(r"[\w'-]+")`) and `length_gate(text, cfg)` returning
`(wc, ok, hard_fail, length_penalty)` — hard fail outside [hard_min, hard_max], soft penalty
ramp across the off-target band, zero inside 100–350.

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
- **Phase 1 — Ingest + validation + dedup (Stage 0)**
  - 1.1 data contract: §2 header constants + header validation (graceful) + `ApplicantRow`
  - 1.2 load + normalize: pandas read (encoding-safe); trim whitespace; blank/whitespace -> empty
  - 1.3 identity validation: drop rows missing first name, last name, OR email (unidentifiable);
        record dropped count/ids. GPA/essay blanks are NOT dropped — they flow to the pipeline
        (blank GPA -> NEEDS_REVIEW, empty essay -> REJECTED per PRD)
  - 1.4 dedup: email primary (keep first; mark + drop surplus as is_duplicate_email); name-pairs
        without a shared email -> flag is_duplicate_name (keep, don't merge) -> DedupInfo
  - 1.5 `ingest_csv()` orchestration (kept rows + drop/dup report) + synthetic-CSV tests
- **Phase 2 — Essay deterministic gates (Stage 1)** — `src/srip_filter/gates/essays.py`,
  tests `tests/gates/test_essays.py`. Runs on BOTH essays; either essay failing a *hard* check
  → `REJECTED`. Soft length penalties are computed here but carried forward (applied in Stage 4
  scoring, §8.3), never a rejection. No LLM calls in this stage.
  - 2.1 Word count + length gate (PRD §4.1): `word_count` tokenizer (`re.findall(r"[\w'-]+")`);
        `length_gate(text, cfg)` → `(wc, ok, hard_fail, length_penalty)`. Hard fail when
        `wc < hard_min` or `wc > hard_max` (empty essay → hard fail); soft penalty ramps 0 →
        `len_penalty_max` across the off-target band (100–350 = 0). Pure functions.
  - 2.2 Profanity gate (PRD §4.2): `resources/profanity.txt` scaffold (medical/anatomical
        allowlist + curated-slur placeholder, per openissue #3); `profanity_gate(text)` over
        `better-profanity`, whole-token case-insensitive + light leetspeak normalization, with
        the medical/anatomical allowlist exempting clinical terms. Returns a hit bool.
  - 2.3 Gibberish heuristics (PRD §4.2, no dictionary): cheap deterministic signals
        (long consonant runs, low char-entropy / repeated-char runs, low unique-word ratio);
        fires only when **≥2** signals trip (ESL-safe). Adds a `gibberish` CONFIG section
        (thresholds) to `config.yaml` + `config.py`. Returns a hit bool.
  - 2.4 Stage 1 aggregator: `run_essay_gates(row, cfg) -> Stage1Result` runs 2.1–2.3 on both
        essays, sets the verdict (REJECTED if either essay hard-fails length OR any profanity/
        gibberish hit), carries the two soft length penalties forward, and fills the audit
        `Gates` blocks (`essay_length`, `profanity`, `gibberish`). Integration tests.
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
- [x] Phase 1.1 — ingest data contract: §2 header constants + graceful resolver
      (`resolve_headers`/`validate_headers`) + `ApplicantRow` (commit: d32a52b).
- [x] Phase 1.2 — load + normalize: encoding-safe `read_csv_records` (utf-8-sig→cp1252→latin-1,
      all-string, no NA inference) + `normalize_cell`; from_record normalizes (commit: c140a11).
- [x] Phase 1.3 — identity validation: `validate_identity` drops rows missing first/last/email,
      records index+id+missing fields; blank GPA/essays kept (commit: c79d852).
- [x] Phase 1.4 — dedup: `deduplicate` email-primary removal + name-pair flagging -> DedupInfo
      (commit: ba4c780).
- [x] Phase 1.5 — `ingest_csv()` orchestration (kept rows + IngestReport) + synthetic-CSV
      integration tests (commit: 21992c5).

## In Progress
- (none)

## Next Up
- [ ] Phase 2.1 — word count + length gate (hard fail vs soft-penalty band)
- [ ] Phase 2.2 — profanity gate (better-profanity + medical allowlist + leetspeak)
- [ ] Phase 2.3 — gibberish heuristics (≥2 cheap signals, no dictionary) + gibberish CONFIG
- [ ] Phase 2.4 — Stage 1 aggregator `run_essay_gates` + integration tests
- [ ] Phase 3 — GPA normalization + gate (deterministic + Task A/B)

## How to Verify Completed Work
(Fill in one command per sub-task as it lands.)
- Phase 0.1: `uv sync && uv run pytest -q && uv run ruff check .`
- Phase 0.2: `uv run pytest tests/test_config.py`
- Phase 0.3: `uv run pytest tests/test_models.py`
- Phase 0.4: `uv run pytest tests/llm/test_client.py`
- Phase 1 (all): `uv run pytest tests/test_ingest.py` (header resolution, load/normalize,
  identity, dedup, and the `ingest_csv` synthetic-CSV integration tests)
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
- **Ingest validation (Phase 1.3):** drop a row only when first name, last name, OR email is
  empty (unidentifiable submission). Blank GPA and empty essays are NOT dropped — they flow to
  the pipeline (blank GPA -> NEEDS_REVIEW, empty essay -> REJECTED) per PRD §1/§6, preserving the
  ~43 blank-GPA international applicants. (Owner decision.)
- **Header matching (Phase 1.1):** short, stable headers match exactly; the long Fillout
  question columns (both essays, extenuating-circumstances, affirmation) match by a distinctive
  substring because the PRD only quotes them in part and form copy drifts per cycle. The
  resolver enforces a 1:1 role↔header mapping and *reports* missing/ambiguous/unrecognized
  without raising; only `validate_headers`/`ingest_csv` raise (`HeaderValidationError`) and only
  when the contract is unsatisfiable (missing-required or ambiguous). Required roles = identity
  (first/last/email) + core graded signals (GPA + both essays); everything else optional.
- **CSV reading (Phase 1.2):** `read_csv_records` reads every cell as a string with pandas
  NA-inference OFF, so a literal `N/A`/`4.0` GPA survives verbatim (no float coercion, no NaN).
  Encoding fallback utf-8-sig → cp1252 → latin-1 (last never raises) so a non-UTF-8 byte can't
  500 the upload. Accepts path/bytes/binary-buffer for the future API. Outer-whitespace trim
  only; interior essay newlines preserved.
- **Dedup flagging (Phase 1.4):** `is_duplicate_email` is set True on BOTH the kept canonical
  and the dropped surplus (honest "this applicant submitted more than once"); only `kept`
  differs. Name-pair duplicates are flagged on all members and kept (never merged) — by
  construction they have distinct emails (siblings / re-applications).

## Owner-Supplied Dependencies (full detail in `openissue.md`)
- [x] `resources/schools.json` — Top-20 US + Top-50 International (source: U.S. News), frozen for Summer 2026.
- [~] Profanity list — using `better-profanity` DEFAULT list for now (owner approved). Curated
      slur list + medical/anatomical allowlist still needed (openissue.md #3).
- [ ] `OPENAI_API_KEY` in `.env` (openissue.md #1).
- [ ] OpenAI account set to zero/minimal data retention (openissue.md #2).
- [x] GPA threshold — settled at 3.0 (PRD §1). No decision needed.
- [~] Resume parsing — explicitly deferred; Stage 6 stays an inert stub.
