# Project Plan — SRIP Track 2 Application Filtering System

Session-to-session memory. See `CLAUDE.md` for how to build, `SRIP_Application_Filter_PRD.md`
for what to build.

## Current Phase
Phase 6 — School bonus + resume stub (Stages 7, 6)

## Active Sub-Task
Phase 5 complete (all of Stage 5: Task C coursework bonus — prompt, pure recompute-from-config
bonus math, LLM aggregator). Next action: Phase 6 — `rapidfuzz` school match against
`resources/schools.json` (Stage 7, bonus-only) + the inert `resume_bonus = 0` stub (Stage 6,
clearly TODO). `SchoolConfig`/`ResumeConfig`, `resources/schools.json`, and the `SchoolMatch`
audit model already exist from Phase 0.

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
- **Phase 3 — GPA normalization + gate (Stages 2–3)** — `src/srip_filter/gates/gpa.py`,
  tests `tests/gates/test_gpa.py`. Stage 2 normalizes (deterministic-first, LLM Task A only when
  needed); Stage 3 gates. Hard invariants (PRD §1/§6.2): an unresolvable/blank scale →
  `NEEDS_REVIEW` (never `REJECTED`); GPA ≥ 3.0 → PASS + gradient points; GPA < 3.0 needs a
  severity-scaled explanation (Task B) or it is `REJECTED`. Produces the §9 `gpa` audit block
  (`GpaAssessment`) + the `gpa_gate` block + a verdict. The LLM-touching sub-tasks (3.2 Task A,
  3.4 Task B) are isolated so 3.1/3.3 stay fully testable with zero API spend; LLM tests use
  `FakeLLMClient`.
  - 3.1 Deterministic normalizer (no LLM, PRD §6.1): `normalize_gpa_deterministic(raw, cfg)` →
        a `GpaNormalization` result. Resolves clean `0.0–4.0`, percentages via the §6.1 table,
        clear `/5` and `/10` scales, and trailing-label strip (`3.97 GPA`, `3.8/4.0 unweighted`).
        Fills `{normalized_gpa, original_scale, conversion_method, confidence, below_threshold,
        requires_manual_review, source="deterministic"}`. When it cannot confidently resolve
        (value > route threshold ≈4.5, non-numeric scale, foreign curriculum, unparseable) it
        returns a `needs_llm` routing flag — no decision yet. Centralizes the percentage→4.0
        table + scale/route thresholds in a new `gpa.normalization` CONFIG block (config.py +
        config.yaml). Pure functions; tests over the messy §2 GPA cases.
  - 3.2 Task A fallback + `normalize_gpa` orchestration (LLM, PRD §6.1 / §8): `prompts/` Task-A
        template; async `normalize_gpa(raw, client, cfg)` runs the deterministic path first and
        calls Task A **only** for `needs_llm` values. Caps the LLM result at 4.0, sets
        `source="llm"` + `confidence`, and maps Task A `requires_manual_review` (or low-confidence
        unplaceable, e.g. `N/A` / "no GPA" / blank) → `requires_manual_review=True` (→ `NEEDS_REVIEW`
        at the gate). `LLMParseFailure` → manual-review routing, never a reject. Passes the GPA
        string as `cache_text` so identical values dedup in-run. `FakeLLMClient` tests, no spend.
  - 3.3 GPA points gradient + deterministic gate paths (PRD §8.1, §6.2): pure
        `gpa_points(normalized_gpa, cfg)` = `clamp((g−3.0)/(4.0−3.0),0,1) * gpa_score_max`
        (3.0→0, 3.7→28, 4.0→40); plus the non-LLM branches of the gate — null/`requires_manual_review`
        → `NEEDS_REVIEW`; ≥3.0 → PASS + points; <3.0 with a blank explanation → `REJECTED`.
        Returns a `GpaGateResult` (verdict + points + populated `GpaAssessment`/`GpaGate` audit
        blocks). Deterministic; tests cover the gradient endpoints and each branch.
  - 3.4 Task B low-GPA adequacy + Stage 2–3 aggregator (LLM, PRD §8.2, §6.2): `prompts/` Task-B
        template; wire the `<3.0 + explanation present` branch — call Task B with
        `(normalized_gpa, gap=3.0−g, explanation)`; `recommended_outcome=="rank"` → PASS + (low)
        points with the deficit reflected, else → `REJECTED` (reason = Task B rationale); store the
        `TaskBOutput` in `GpaAssessment.explanation_eval`. Assemble async `assess_gpa(row, client,
        cfg)` tying Stage 2 → Stage 3. PRD §12 invariant tests: GPA < 3.0 never yields points
        without an approved Task B and never scores above the bottom of the gradient; nothing
        unscoreable is `REJECTED`. `FakeLLMClient`, no spend.
- **Phase 4 — Essay LLM grading (Stage 4, Task D)** — `src/srip_filter/scoring/essays.py`,
  tests `tests/scoring/test_essays.py`. Runs only on Stage 1–3 survivors. Per essay, Task D
  applies the gibberish backstop and the relevance gate (either → `REJECTED`) plus a 0–20 quality
  score; the carried Stage-1 soft length penalty and the Task-D grammar penalty are then
  subtracted. Gibberish OR off-topic on *either* essay rejects the whole application (§4/§8.3); a
  Task-D `LLMParseFailure` → `NEEDS_REVIEW`, never a rejection. The two Task-D calls per applicant
  are the only spend in this stage. The LLM-touching sub-task (4.3) is isolated so the §8.3
  post-processing math (4.2) stays fully testable with zero API spend; LLM tests use `FakeLLMClient`.
  - 4.1 Task D prompt (no scoring logic): create `src/srip_filter/scoring/` (+ `__init__.py`) and
        `llm/prompts/task_d.py` with `SYSTEM` (PRD §8.3 essence: gibberish-first, relevance gate,
        quality on clarity/specificity/coherence/saliency, *slight* grammar penalty, ESL-safe —
        never flag accent-of-writing) and `user_prompt(prompt_text, word_count, essay_text)`
        emitting the §8.3 template (`PROMPT` / `WORD_COUNT` / `TARGET_RANGE: 100-350` / `ESSAY`).
        `prompt_text` is the **resolved CSV essay-question header** (exactly what the applicant
        answered), supplied by the orchestrator from `HeaderResolution.role_to_header` (Phase 8) —
        no new config, no owner dependency, no drift. Pure template; tests assert the rendered shape.
  - 4.2 Per-essay post-processing math (pure, no LLM): `score_one_essay(out: TaskDOutput,
        length_penalty: float, cfg) -> EssayScoreResult` implementing §8.3 — gate flags
        (`is_gibberish`, `not on_topic`) and `essay_score = max(0, quality_score -
        grammar_spelling_penalty - length_penalty)`, floored at 0 and capped at
        `essay_scoring.quality_max_each`. Pure function; tests cover the gate flags, the penalty
        arithmetic, the `max(0, …)` floor (a length penalty never drives a score negative), and
        that a gated essay contributes 0.
  - 4.3 Stage 4 aggregator (LLM): async `grade_essays(row, length_penalty_e1, length_penalty_e2,
        prompt_e1, prompt_e2, client, cfg) -> Stage4Result`. Calls Task D for both essays
        (concurrency handled by the client), applies 4.2, and reduces to a verdict — `REJECTED` if
        gibberish OR off-topic on either essay, with `primary_reason` naming the failing essay/gate
        in deterministic fail-fast order (gibberish → relevance). Fills the audit `essay_relevance`
        block and the Task-D `gibberish` finding, and the `EssaySubscores` (e1/e2/total). A Task-D
        `LLMParseFailure` (after the client's retry) → `NEEDS_REVIEW` with reason `LLM_PARSE_FAILURE`,
        never a rejection. `FakeLLMClient` tests, no spend: reject-on-either-essay, parse-failure
        routing, total-score composition, and that an off-topic essay yields no score.
- **Phase 5 — Coursework bonus (Stage 5, Task C)** — `src/srip_filter/scoring/coursework.py`,
  tests `tests/scoring/test_coursework.py`. Runs only on Stage 1–4 survivors and is **bonus-only**:
  it can add to `final_score`, never subtract, and can never change a `REJECTED`/`NEEDS_REVIEW`
  outcome (PRD §0.3/§7). Empty `Relevant Coursework` → 0 bonus, no LLM call (56 applicants have it
  blank). Task C decomposes the free-text cell into courses, classifies each cs/math/data/other,
  and normalizes each grade to a 0–100 percentage; the deterministic layer then applies the config
  weights + the 80% floor and sums a capped bonus. The `courses[]` array is stored verbatim in the
  audit `coursework_breakdown` for the future UI. No new config — `CourseworkConfig` and the
  `CourseItem`/`TaskCOutput` models already exist (Phase 0). Same isolate-the-LLM pattern as Phases
  3–4: the bonus math (5.2) is pure/zero-spend; only 5.3 spends a token. `FakeLLMClient` tests.
  - 5.1 Task C prompt (no scoring logic): `llm/prompts/task_c.py` with `SYSTEM` (§8.4 essence —
        faithful course/grade extraction, classify cs > math > data > other, normalize each grade
        to a 0–100 pct via the §6 scale logic, decompose so a human reviewer sees each course) and
        `user_prompt(coursework_cell)` emitting `COURSEWORK_RAW: """{…}"""`. Pure template; tests
        assert the rendered shape. Uses the mini tier (`task_c` model — mechanical extraction).
  - 5.2 Pure coursework bonus math (no LLM): `coursework_bonus(out: TaskCOutput, cfg) ->
        CourseworkResult` implementing §8.4/§5. **Weights + counts are recomputed from config**, not
        trusted from the LLM: `weight = course_weight_<category>` and
        `counts = category != "other" and grade_pct >= course_min_grade_pct`; then
        `per_course = weight * (grade_pct/100) * course_unit` for counting courses, summed and
        `min(coursework_bonus_max, …)`, floored at 0 (never negative). Returns the bonus + the
        reconciled `courses[]` for the audit. Pure; tests cover weight-by-category, the <80% and
        `other` zero-outs, the cap, never-negative, and empty→0.
  - 5.3 Stage 5 aggregator (LLM): async `score_coursework(row, client, cfg) -> Stage5Result`.
        Empty cell → `(bonus=0, courses=[])` with no token spent. Otherwise call Task C, apply 5.2,
        and fill `Scores.coursework_bonus` + `AuditRecord.coursework_breakdown`. A Task C
        `LLMParseFailure` (after the client's retry) → `bonus=0` + an audit error note, **never**
        `NEEDS_REVIEW`/`REJECTED` — a bonus-only signal that cannot be extracted is neutral, and the
        applicant stays scoreable on the required signals (GPA + essays). `FakeLLMClient` tests, no
        spend: empty→no call, parse-failure→0 bonus, bonus composition, cap.
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
- [x] Phase 2.0 — `resources/profanity.txt` placeholder scaffold (inert; format documented) +
      openissue.md #3 update (commit: a48f6cd).
- [x] Phase 2.1 — essay length gate: `word_count` + `length_gate` → `LengthResult`
      (hard fail outside [hard_min, hard_max]; soft penalty ramp; pure) (commit: 90822d3).
- [x] Phase 2.2 — profanity gate: `profanity_gate` over better-profanity (default list + BLOCK
      − ALLOW from `resources/profanity.txt`); cached matcher; leetspeak/whole-token (commit: 4ed0bc9).
- [x] Phase 2.3 — gibberish heuristics: `gibberish_gate` (4 dictionary-free signals, hit at
      ≥`min_signals`); `GibberishConfig` added to config.py + config.yaml (commit: a6bbffe).
- [x] Phase 2.4 — Stage 1 aggregator `run_essay_gates(row, cfg)` → `Stage1Result` (verdict +
      audit Gates blocks + carried soft penalties); integration tests (commit: d6c429a).
- [x] Phase 3.1 — deterministic GPA normalizer `normalize_gpa_deterministic` (clean 4.0, % /100,
      /5 linear, /10 ×10 table, label-strip; `needs_llm` routing; blank → manual review) +
      `gpa.normalization` CONFIG (percentage table + clean-scale ceiling) (commit: e46b685).
- [x] Phase 3.2 — Task A prompt + async `normalize_gpa` orchestration (deterministic-first, Task A
      only for `needs_llm`; caps at gpa_max; unplaceable/parse-failure → manual review) (commit: db59947).
- [x] Phase 3.3 — `gpa_points` gradient (§8.1) + `gpa_gate_deterministic` (→ `GpaGateResult`;
      needs_review / pass+points / reject branches; Task B branch returns None) (commit: eb713d8).
- [x] Phase 3.4 — Task B prompt + async `assess_gpa` Stage 2–3 aggregator (sub-3.0 + explanation
      → Task B rank/reject; bottom-of-gradient points; §12 GPA invariants) (commit: 98c3bca).
- [x] Phase 4.1 — Task D prompt (`prompts/task_d.py`): §8.3 SYSTEM (gibberish-first, relevance
      gate, ESL-safe slight grammar penalty) + `user_prompt(prompt_text, word_count, essay_text)`;
      `prompt_text` = resolved CSV essay-question header. Pure template (commit: ebb4cd0).
- [x] Phase 4.2 + 4.3 — `scoring/essays.py`: `score_one_essay` post-processing math (gates +
      `max(0, quality − grammar − length)`, capped) and `grade_essays` Stage 4 aggregator (both
      essays via Task D; reject on gibberish/off-topic either essay, fail-fast gibberish→relevance;
      parse-failure → NEEDS_REVIEW; essay_relevance/gibberish audit blocks + subscores). Landed
      together (shared module + test file) (commit: 2b86820).
- [x] Phase 5.1 — Task C prompt (`prompts/task_c.py`): §8.4 SYSTEM (faithful course/grade
      extraction, classify cs/math/data/other, normalize each grade to 0-100 pct, decompose for
      a human reviewer) + `user_prompt(coursework_cell)` emitting `COURSEWORK_RAW: """{…}"""`.
      Pure template (commit: 90a81c5).
- [x] Phase 5.2 + 5.3 — `scoring/coursework.py`: `coursework_bonus` pure math (weights + counts
      recomputed from config, `per_course = weight*(grade_pct/100)*unit`, cap + never-negative,
      reconciled `courses[]`) and `score_coursework` Stage 5 aggregator (empty cell → 0, no token;
      Task C otherwise; parse-failure → 0 bonus + audit error note, never NEEDS_REVIEW). Landed
      together (shared module + test file) (commit: 90a81c5).

## In Progress
- (none)

## Next Up
- [ ] Phase 6 — School bonus + resume stub (Stages 7, 6)
- [ ] Phase 7 — Aggregation, ranking, outputs (Stages 8–9)

## How to Verify Completed Work
(Fill in one command per sub-task as it lands.)
- Phase 0.1: `uv sync && uv run pytest -q && uv run ruff check .`
- Phase 0.2: `uv run pytest tests/test_config.py`
- Phase 0.3: `uv run pytest tests/test_models.py`
- Phase 0.4: `uv run pytest tests/llm/test_client.py`
- Phase 1 (all): `uv run pytest tests/test_ingest.py` (header resolution, load/normalize,
  identity, dedup, and the `ingest_csv` synthetic-CSV integration tests)
- Phase 2:   `uv run pytest tests/gates/test_essays.py`
- Phase 3:   `uv run pytest tests/gates/test_gpa.py` (deterministic normalize, Task A/B mocked
  fallback, gradient endpoints, gate branches, and the §12 GPA invariants)
- Phase 4:   `uv run pytest tests/scoring/test_essays.py` (Task D post-processing math, mocked
  Task D aggregator: reject-on-either-essay, parse-failure → NEEDS_REVIEW, total-score composition)
- Phase 5:   `uv run pytest tests/scoring/test_coursework.py` (Task C prompt shape, the pure bonus
  math — weights, <80%/`other` zero-out, cap, never-negative, empty→0 — and the mocked aggregator:
  empty→no call, parse-failure→0 bonus, bonus composition)
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
- **Profanity matcher (Phase 2.2):** the gate = better-profanity's DEFAULT list + curated BLOCK
  terms − medical/anatomical ALLOW terms from `resources/profanity.txt`. ALLOW exemption is
  applied by filtering `Profanity.CENSOR_WORDSET` (a plain list; `VaryingString == str` powers
  the match) rather than depending on better-profanity's internal wordlist reader — fewer
  internals coupled. The default list already contains clinical-ish entries (e.g. `anal`), so
  the allowlist is genuinely load-bearing. Matcher built once per run (`lru_cache`); a missing
  file → empty BLOCK/ALLOW → behaves exactly as the default list. File format: `#` comments,
  `ALLOW:`-prefixed allow terms, every other line a block term (lowercased).
- **Gibberish signals (Phase 2.3):** four dictionary-free signals — long consonant run (`y`
  counted as a vowel to avoid false runs like "rhythm"), low letter entropy, long identical-char
  run, low unique-word ratio. A hit needs ≥`min_signals` (default 2) so ordinary awkward/ESL
  prose (≤1 signal) passes; text below `min_chars` letters is never flagged. Thresholds live in
  the new `gibberish` CONFIG section. `GibberishResult` keeps per-signal booleans for the audit
  trail; only `.hit` gates.
- **Stage 1 verdict (Phase 2.4):** all three checks are token-free, so `run_essay_gates` computes
  *all* of them (complete audit Gates block) rather than short-circuiting — fail-fast governs the
  LLM stages, not these. Reject if either essay hard-fails length OR profanity/gibberish hits
  either essay; soft length penalties are carried to Stage 4, never a rejection. `primary_reason`
  names the failing gate in fail-fast order (length → profanity → gibberish) so no reject is silent.
- **Phase 3 (implementation):** `GpaNormalization` (frozen dataclass) is the Stage-2 result with a
  three-way disposition — *resolved* / *needs_llm* (route to Task A, no decision) / *manual review*
  (empty cell, no token). Scale routing line: a bare numeric in `[0, gpa_max]` is clean 4.0; a bare
  value **> gpa_max (4.0) routes to Task A** (treated as weighted) — this supersedes the PRD §6.1
  "> 4.5" example, honoring "weighted >4.0 → Task A". Fraction scale is chosen by denominator
  (100→%, 10→×10 table, 5→linear, 4→4-point; other→Task A). A truly empty cell goes straight to
  manual review (no LLM); a non-empty unparseable string (e.g. `N/A`, IGCSE letters) routes to
  Task A, which then returns `requires_manual_review`. The §6.1 percentage→4.0 table is data in
  `config.yaml` (`gpa.normalization`), table-driven incl. the "<73 → linear toward 0" segment
  (anchored on the lowest band). `gpa_points` clamps below threshold to 0, so an approved sub-3.0
  applicant lands at the gradient bottom (0) — deficit reflected, never erased (§8.1). The Stage-3
  verdict is an internal `GpaGateVerdict` (`pass`/`reject`/`needs_review`), distinct from the final
  `Outcome` (a `pass` is not yet RANKED — essays still run). Hard line held throughout: an
  unresolvable/blank scale and every LLM parse failure → `needs_review`, never `REJECTED`.
- **Phase 3 breakdown (plan-time):** split Stage 2–3 into 3.1 deterministic normalize, 3.2 Task A
  fallback, 3.3 points-gradient + deterministic gate paths, 3.4 Task B + aggregator. Rationale:
  isolate the two LLM-touching sub-tasks (A, B) so the deterministic majority (most GPAs resolve
  without a call) is covered by zero-spend tests, mirroring Phase 2. The §6.1 percentage→4.0 table
  and the scale/route thresholds (the ≈4.5 "route to Task A" line, /5 and /10 handling) will live
  in a new `gpa.normalization` CONFIG block — they are magic numbers and belong in config.yaml,
  not logic. Hard line preserved: an unresolvable/blank scale is `NEEDS_REVIEW`, never `REJECTED`.
- **Phase 4 breakdown (plan-time):** split Stage 4 into 4.1 Task D prompt, 4.2 pure per-essay
  post-processing math, 4.3 the LLM aggregator — same pattern as Phases 2–3 (isolate the LLM call
  so the §8.3 scoring math is zero-spend testable). Decision: the Task D **PROMPT is the resolved
  CSV essay-question header** (what the applicant actually answered), plumbed from
  `HeaderResolution.role_to_header` by the orchestrator and passed into `grade_essays` — *not* a
  frozen copy in config. Rationale: most faithful, zero owner dependency, immune to per-cycle form
  drift; avoids storing question content as a "magic string." `essay1`/`essay2` are required roles,
  so the header is always present after a successful ingest. Consequence: no config or
  `openissue.md` change is needed for Phase 4; the only new wiring is the orchestrator reading the
  two resolved headers (Phase 8). Gibberish is detected in *both* Stage 1 (cheap deterministic) and
  Task D (LLM backstop, per the Phase 0.3 model deviation); Stage 4 contributes its finding to the
  audit `gibberish` block and the pipeline reconciles the two.

- **Phase 4 (implementation):** `grade_essays` fires both Task D calls via `asyncio.gather` (the
  client's semaphore bounds real concurrency). Cache key is left at the default (the rendered user
  prompt = PROMPT + WORD_COUNT + ESSAY), so identical (prompt, essay) pairs dedup but two different
  prompts over the same essay text do NOT collide — safer than keying on essay text alone. Caveat:
  the in-run cache is not lock-guarded, so two *concurrent* identical inputs can both miss and
  double-call against the real API; with the sync `FakeLLMClient` no suspension occurs so the
  dedup test is deterministic. Same-applicant identical essays are rare, so this is accepted (matches
  the existing stateless cache design). `Stage4Result` carries the raw `TaskDOutput`s (`e1_grade`/
  `e2_grade`) for the Phase 8 audit `reasons` builder; they are `None` on a parse failure. The
  Task-D `gibberish` HitGate is Stage 4's own finding — Phase 8 reconciles it with the Stage 1
  cheap-heuristic gibberish block (both can independently reject).

- **Phase 5 (implementation):** `score_coursework` short-circuits a blank/whitespace cell with
  zero spend (`bonus=0, courses=[]`). `coursework_bonus` **recomputes** each course's
  `category_weight` (from `CourseworkConfig`) and `counts` (`category != "other" and grade_pct >=
  min_grade_pct`) and returns the courses with those reconciled values via `model_copy(update=…)`,
  so the audit `coursework_breakdown` shows exactly what the system applied (the model's own
  `counts`/`category_weight` are ignored — only its `category` + `grade_pct` are trusted). The cap
  uses `min(bonus_max, …)` and a `max(0, …)` floor (never negative); the floor test is `>=` so a
  course at exactly 80% counts. A Task C `LLMParseFailure` degrades to `bonus=0` + a non-empty
  `Stage5Result.error` note for `AuditRecord.errors` — never `NEEDS_REVIEW`/`REJECTED` (narrows
  §8's general parse-failure→NEEDS_REVIEW to gating tasks B/D; bonus-only C and the future resume
  degrade to 0).

- **Phase 5 breakdown (plan-time):** split Stage 5 into 5.1 Task C prompt, 5.2 pure bonus math,
  5.3 the LLM aggregator — same isolate-the-LLM pattern as Phases 3–4. Two decisions to settle in
  implementation: (a) the deterministic layer **recomputes** each course's `category_weight` and
  `counts` from `CourseworkConfig` (using the LLM's `category` + `grade_pct`) rather than trusting
  the model's own `category_weight`/`counts` fields — keeps the weights and the 80% floor tunable
  in `config.yaml` and authoritative, mirroring how Phase 3 computes `gpa_points` deterministically
  instead of asking the model. (b) A Task C `LLMParseFailure` yields **0 bonus + an audit error
  note, not `NEEDS_REVIEW`** — coursework is bonus-only (§0.3: "non-required signals can only add,
  never subtract"; absence is neutral), so a failed *bonus* extraction must not block an applicant
  who is fully scoreable on the required signals (GPA + essays). This narrows §8's general
  "parse failure → NEEDS_REVIEW" to gating/required tasks (B, D); bonus-only tasks (C, and the
  future resume) degrade to 0. No new config — `CourseworkConfig` and the `CourseItem`/`TaskCOutput`
  models already exist (Phase 0).

## Owner-Supplied Dependencies (full detail in `openissue.md`)
- [x] `resources/schools.json` — Top-20 US + Top-50 International (source: U.S. News), frozen for Summer 2026.
- [~] Profanity list — using `better-profanity` DEFAULT list for now (owner approved).
      `resources/profanity.txt` placeholder scaffold committed (format documented, not yet
      loaded); curated slur list + medical/anatomical allowlist still needed (openissue.md #3).
- [ ] `OPENAI_API_KEY` in `.env` (openissue.md #1).
- [ ] OpenAI account set to zero/minimal data retention (openissue.md #2).
- [x] GPA threshold — settled at 3.0 (PRD §1). No decision needed.
- [~] Resume parsing — explicitly deferred; Stage 6 stays an inert stub.
