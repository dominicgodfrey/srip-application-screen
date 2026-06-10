# Project Plan — SRIP Track 2 Application Filtering System

Session-to-session memory. See `CLAUDE.md` for how to build, `SRIP_Application_Filter_PRD.md`
for what to build.

## Current Phase
Phase 12 — Resume bonus (Stage 6, PRD §7.2 — now IN SCOPE) — PLANNED, implementation not started

## Active Sub-Task
Phase 12 planned (owner decision: resume parsing moves from "deferred" into scope). The plan
fills the existing slot — `Scores.resume_bonus` is already in the §10.1 composition,
`scoring/resume.py` is the deliberate stub, and `resume.bonus_max` (0 today) is the kill switch.
Sub-task order: 12.1 config+contracts → 12.2 download layer → 12.3 PDF extraction → 12.4 Task E
prompt + bonus math → 12.5 Stage 6 aggregator + wiring → 12.6 scale verification + docs. See the
Phase Map Phase 12 breakdown and the Notes-log entry (hosting analysis: per-applicant
fetch→extract→discard memory rule, SSRF allowlist, pypdf-over-pdfplumber deviation). Next
action: begin 12.1. Phase 10 (web UI) is complete and demo-ready; other open owner inputs:
`OPENAI_API_KEY` + retention (openissue #1/#2), curated profanity list (#3), resume URL host
allowlist (#5, new).

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
- **Phase 6 — School bonus (Stage 7) + resume stub (Stage 6)** — `src/srip_filter/scoring/school.py`
  + `src/srip_filter/scoring/resume.py`, tests `tests/scoring/test_school.py` +
  `tests/scoring/test_resume.py`. Both stages are **bonus-only** (PRD §0.3/§7): they add to
  `final_score`, never subtract, and can never change a `REJECTED`/`NEEDS_REVIEW` outcome.
  Entirely **deterministic — no LLM**, so no isolate-the-LLM split; instead Phase 6 splits along the
  two stages (match → bonus → stub). "High School" (364/466 applicants), blanks, and unmatched
  schools → 0, **never negative**. No new config — `SchoolConfig` (`bonus_us_top20`,
  `bonus_intl_top50`, `fuzzy_match_threshold`), `ResumeConfig` (`bonus_max=0`), the `SchoolMatch`
  audit model, and `resources/schools.json` (Top-20 US + Top-50 Intl, names + aliases, frozen for
  Summer 2026) all already exist from Phase 0.
  - 6.1 School resource loader + normalize + fuzzy match (PRD §7.1/§13): `match_school(institution,
        cfg) -> SchoolMatch`. Load `resources/schools.json` once (`lru_cache`); normalize the
        institution string (lowercase, strip punctuation/extra whitespace); build the candidate set
        per list = each school's `name` + its `aliases`; score with `rapidfuzz` token-set ratio and
        keep the best match ≥ `fuzzy_match_threshold` (88). Blank / "High School" / no
        ≥-threshold match → empty `SchoolMatch` (`matched_name=None, list=None, fuzzy_score=0`).
        **Both-lists tiebreak:** a school matching in both lists is reported under the list whose
        configured bonus is higher (so `SchoolMatch.list` is authoritative and 6.2 is a pure
        lookup); `match_school` takes `cfg` and owns this resolution. Returns the matched canonical
        name + list + score for the audit + human verification. Pure (given the cached resource);
        tests over exact, alias (`MIT`, `UCLA`), light misspelling, "High School" → no match, blank
        → no match, and a both-lists school → higher-bonus list.
  - 6.2 School bonus + Stage 7 aggregator (PRD §7.1): `score_school(row, cfg) -> Stage7Result`.
        Runs 6.1, then maps the matched list to its bonus (`us_top20` → `bonus_us_top20`,
        `intl_top50` → `bonus_intl_top50`); `list is None` → 0. Fills `Scores.school_bonus` + the
        `SchoolMatch` audit block. Deterministic; tests: US-top20 → its bonus, Intl-top50 → its
        bonus, unmatched/"High School"/blank → 0, bonus is never negative, and (a §12 invariant) a
        school bonus can neither manufacture nor rescue a `REJECTED` outcome.
  - 6.3 Resume inert stub (Stage 6, PRD §7.2 — DEFERRED): `resume_bonus(row, cfg) -> float`
        returning `cfg.resume.bonus_max` (0) for everyone, with a clearly-labeled `TODO` that the
        slot exists but PDF download + parsing is unplanned. Absence of a resume is neutral (148
        blanks). Pure; one test: always 0 regardless of the `Resume (optional)` cell.
- **Phase 7 — Aggregation, ranking, outputs (Stages 8–9)** — `src/srip_filter/scoring/aggregate.py`
  (Stage 8) + `src/srip_filter/outputs.py` (Stage 9), tests `tests/scoring/test_aggregate.py` +
  `tests/test_outputs.py`. Entirely **deterministic — no LLM**. Composes the additive `final_score`
  for gate-survivors *only*, finalizes the three outcomes, ranks `RANKED` applicants with a
  deterministic tiebreaker, and emits the five output artifacts (PRD §9/§10/§12). All PRD §12
  invariants are asserted here. Split: pure composition (7.1) → outcome + ranking (7.2) → output
  emission (7.3) → consolidated §12 invariant suite (7.4).
  - 7.1 Pure score composition (PRD §10.1): `compose_final_score(scores: Scores, cfg) -> float`
        summing the five components (`gpa_points + essay.total + coursework_bonus + school_bonus +
        resume_bonus`), each ≥ 0, none subtracted; plus a thin `finalize_score(record, cfg)` writing
        it into `AuditRecord.final_score`. Pure; tests assert the §10.1 composition and §12 #1 (no
        optional-signal absence — coursework/school/resume = 0 — ever lowers the total).
  - 7.2 Outcome finalization + deterministic ranking (Stage 8 aggregator, PRD §10.2):
        `rank_records(records, cfg) -> list[AuditRecord]`. Each gate-survivor not already
        `REJECTED`/`NEEDS_REVIEW` gets `final_score` (7.1) and `outcome="RANKED"`; sort `RANKED`
        by `final_score` desc with the deterministic tiebreaker (`gpa_points` → `essay.total` →
        `submission_id`, since the §2 contract carries no submission timestamp) and assign `rank`
        1..N. `REJECTED`/`NEEDS_REVIEW` keep `final_score=None`, `rank=None`. **No acceptance
        cutoff** — the full ranked list is the deliverable (§11). Pure; tests cover order, the
        tiebreaker chain, §12 #2 (no bonus changes a `REJECTED` outcome) and #5 (ranking stable
        across reruns).
  - 7.3 Output emission (Stage 9, PRD §10/§12): `outputs.py` — pure serializers returning in-memory
        artifacts (no forced disk write, for the stateless API): `decisions_jsonl(records) -> str`,
        `ranked_csv`, `rejected_csv`, `needs_review_csv`, and `build_summary(records) -> dict`
        (counts per outcome, `RANKED` score histogram, `NEEDS_REVIEW` list + reasons), plus a
        `write_outputs(records, out_dir)` convenience that writes the five files. Tests assert the
        per-file columns/rows (ranked sorted by `rank`; rejected names the failing gate;
        needs_review names the blocker) and that the summary counts reconcile.
  - 7.4 §12 invariant consolidation (`tests/scoring/test_aggregate.py`): a focused suite over
        synthetic `AuditRecord`s spanning all three outcomes asserting the five PRD §12 invariants
        end-to-end — (1) optional-signal absence never reduces `final_score`, (2) no bonus changes
        a `REJECTED` outcome, (3) every `REJECTED` record names the failing gate in `primary_reason`,
        (4) GPA < 3.0 never yields points without an approved Task B and never above the gradient
        bottom, (5) ranking is stable across reruns. Deterministic, no API spend.
- **Phase 8 — Orchestration (`pipeline.grade_batch`)** — `src/srip_filter/pipeline.py`,
  tests `tests/test_pipeline.py`. Wires Stages 0→9 into the ordered fail-fast batch runner. The
  core stays transport-agnostic (no FastAPI/HTTP here — that is Phase 9). Fail-fast order per row:
  Stage 1 essay gates (→ `REJECTED`) → affirmation validity (→ `NEEDS_REVIEW`) → Stage 2–3 GPA
  (→ `REJECTED`/`NEEDS_REVIEW`) → Stage 4 essay grading (→ `REJECTED`/`NEEDS_REVIEW`) → Stages
  5/6/7 bonuses (additive only) → survivor marked `RANKED` (`final_score` filled by Stage 8).
  `REJECTED` precedence over `NEEDS_REVIEW` is preserved by running the hard gates first. Split:
  deterministic glue (8.1) → per-applicant runner (8.2) → batch runner (8.3) → end-to-end §12 +
  fail-fast spend suite (8.4). No new config (`llm.max_concurrency` already bounds concurrency);
  no owner dependency.
  - 8.1 Base record + affirmation validity (deterministic, pure, no LLM):
        `build_base_record(deduped: DedupedRow, resolution) -> AuditRecord` fills identity
        (`submission_id`/`name`/`email`), `program_choices` (first/second/third from the row), and
        the `dedup` block from `DedupInfo`; outcome starts at a non-terminal placeholder. Plus
        `affirmation_ok(row, resolution) -> bool`: an unchecked truthfulness affirmation →
        `NEEDS_REVIEW`, but the check **only fires when the affirmation role resolved** (in
        `resolution.role_to_header`) — an absent column can't be read as "everyone unchecked"
        (§0.7: never silently reject/route). Pure; zero-spend tests over present-and-checked,
        present-and-blank, and column-absent.
  - 8.2 Per-applicant fail-fast runner (`grade_one`, LLM): async
        `grade_one(deduped, resolution, client, cfg) -> AuditRecord`. Sequences the stages in
        fail-fast order on one `ApplicantRow`, filling every audit block as it goes
        (`gates.*`, `gpa`, `scores`, `coursework_breakdown`, `school_match`, `reasons`,
        `llm_calls`, `errors`) and setting the terminal outcome + `decided_at_stage` +
        `primary_reason` the moment a gate fires (zero LLM spend past a Stage-1/affirmation stop).
        Survivors get `outcome="RANKED"` with `final_score=None` (Stage 8 fills score/rank). Reads
        the two resolved essay-question headers from `resolution.role_to_header` and passes them
        to `grade_essays` (the Phase 4 decision). Reconciles the two gibberish findings (Stage 1
        cheap heuristic + Task D backstop) into `gates.gibberish`. A Task B/D `LLMParseFailure`
        (already surfaced by those stages) → `NEEDS_REVIEW`; coursework parse failure → 0 bonus
        (Phase 5 decision), never a block. Whole body wrapped in `try/except` → on any unexpected
        error, `NEEDS_REVIEW` with an `errors[]` note (per-row isolation: "when grading begins it
        finishes"). `FakeLLMClient` tests over each branch — REJECTED at Stage 1, NEEDS_REVIEW on
        unchecked affirmation, RANKED survivor with full Scores, parse-failure routing, and the
        unexpected-exception → NEEDS_REVIEW path.
  - 8.3 Batch runner (`grade_batch`, LLM): async `grade_batch(source, client, cfg) -> BatchResult`.
        Runs Stage 0 `ingest_csv(source)`, fires `grade_one` for every kept row concurrently
        (`asyncio.gather`; the client's `Semaphore` bounds real concurrency), then Stage 8
        `rank_records(records, cfg)` and Stage 9 `outputs.py` (in-memory artifacts). Returns a
        `BatchResult` bundling the `AuditRecord` list, the five artifacts (`decisions.jsonl` +
        3 CSVs + `summary.json`), and the Stage-0 `IngestReport` (so a shrinking row count is
        explained). Stateless: artifacts are in-memory; `write_outputs` is the opt-in disk path.
        `FakeLLMClient` integration test on a small synthetic CSV.
  - 8.4 End-to-end §12 + fail-fast spend suite (`tests/test_pipeline.py`): a synthetic CSV
        exercising all three outcomes through `grade_batch` with a scripted `FakeLLMClient`.
        Asserts the five PRD §12 invariants hold **end-to-end** (the full pass deferred from Phase
        7) and that fail-fast holds: a row rejected at Stage 1 (or routed by the affirmation check)
        makes **zero** LLM calls (assert against the fake's call log). Deterministic, no real spend.
- **Phase 9 — API layer (FastAPI, stateless)** — `api/main.py` (thin shell over
  `pipeline.grade_batch`), tests `tests/api/test_api.py` (FastAPI `TestClient`, injected
  `FakeLLMClient`, no spend). The core stays HTTP-free; the API only uploads → schedules a
  background job → polls → streams the in-memory artifacts back, persisting nothing. Upload size +
  row caps and §2 header validation are enforced at the edge; malformed input is a graceful 4xx,
  never a 500. In-memory job registry only: an interrupted run is abandoned (matches the stateless
  robustness decision — no DB, no queue). Split:
  - 9.1 App scaffold + `ApiConfig` + schemas + in-memory job registry (no grading yet):
        create the `api/` package and a FastAPI app with a health check; pydantic request/response
        models (`JobCreated`, `JobStatus`, error envelope); a `JobRegistry` (dict keyed by UUID)
        holding lifecycle state (`queued`/`running`/`succeeded`/`failed`), progress counts, the
        `BatchResult` when done, error detail, and created/finished timestamps, with TTL eviction +
        discard-after-download. New `ApiConfig` CONFIG section (`max_upload_bytes`, `max_rows`,
        `job_ttl_seconds`) in `config.yaml` + `config.py` — these are magic numbers and belong in
        config. `TestClient` tests over the registry + health.
  - 9.2 Upload + validation + background kickoff: `POST /jobs` (multipart CSV) enforces the size
        cap (→ 413), the row cap (~2000, → 413/422), and §2 header validation
        (`HeaderValidationError` → 422; unreadable/garbled CSV → 422; **never** 500); on success it
        creates a job, schedules `grade_batch` as an `asyncio` background task, and returns 202 +
        `job_id`. One `OpenAILLMClient` is built at app startup from config/secrets (the client's
        semaphore already bounds LLM concurrency); tests inject a `FakeLLMClient`. Synthetic-CSV
        tests: good CSV → 202; oversize → 413; bad headers → 422; non-CSV → 422.
  - 9.3 Progress polling + status: `GET /jobs/{id}` returns the lifecycle state, progress
        (`rows_done`/`rows_total`), and the run `summary` counts once done; unknown/evicted id →
        404; a failed job reports `failed` + a safe error message (never PII, never a stack trace).
        Wires fine-grained progress via an **optional `progress` callback on `grade_batch`** — the
        single, HTTP-free core touch (signature-compatible default `None`). Tests drive a job to
        completion through a scripted `FakeLLMClient` and assert the state transitions + 404.
  - 9.4 Result download + lifecycle/TTL: `GET /jobs/{id}/results/{artifact}` streams each of the
        five in-memory artifacts (`decisions.jsonl`, the three CSVs, `summary.json`) with correct
        content types/filenames; download before completion → 409; after download (or past TTL) the
        job is evicted → subsequent fetch 404. A background sweeper drops expired jobs so PII is not
        held. Tests: download each artifact; download-before-done → 409; eviction → 404.
- **Phase 10 — Web UI (server-rendered Jinja2 + vanilla JS)** — `api/web.py`, `api/templates/`,
  `api/static/`, tests `tests/api/test_web.py`. FastAPI serves Jinja2 HTML + one static CSS theme +
  vanilla-JS `fetch`/polling against the **existing frozen JSON API** — no React/Vite/Node build,
  same-origin (no CORS). All UI stays in `api/`; the core (`src/srip_filter/`) is untouched. Full
  ThinkNeuro branding (vendored `logo.png` + name) labeled "SRIP Track 2 — Application Filter"; no
  auth. New dep: `jinja2` (api extra). See the Notes-log deviation entry (React+Vite → Jinja2).
  - 10.1 Wiring + shell + theme: add `jinja2`; mount `StaticFiles` + `Jinja2Templates` +
        `register_pages` in `create_app`; env-gated dev `FakeLLMClient` switch (`SRIP_DEV_FAKE_LLM=1`)
        + demo handler for a zero-spend, no-key demo; `api/web.py`, `base.html`, `app.css` (full
        theme), vendored `logo.png`; `GET /` minimal upload page; `tests/api/test_web.py`.
  - 10.2 Screen 1 — upload flow: `upload.html` + `common.js` + `upload.js` (upload → poll progress →
        summary counts/histogram/needs_review → 5 download links → discard → cross-screen links).
  - 10.3 Screen 2 — audit browser: `audit.html` + `audit.js` (fetch `decisions.jsonl`, NDJSON parse,
        sortable/filterable table, row → full `AuditRecord` detail panel); `GET /audit` + test marker.
  - 10.4 Screen 3 — cohort what-if: `cohort.html` + `cohort.js` (capacity inputs → live
        `POST /jobs/{id}/cohorts`; assignment/waitlist/unassignable + tier summary; standalone
        `POST /cohorts` re-upload; CSV export); `GET /cohorts` + test marker.
  - 10.5 Synthetic demo CSV (`resources/demo/sample_applications.csv`) + manual-verification
        checklist + responsive/visual polish.
- **Phase 11 — Cohort assignment (PRD §11; executes before Phase 10)** — `src/srip_filter/cohort.py`
  + two API routes, tests `tests/test_cohort.py` + `tests/api/test_cohorts.py`. The downstream
  layer that turns the ranked output into honors/intensive/regular placements under configurable
  per-tier capacities. Entirely deterministic, pure, LLM-free, and instant (live what-if recompute
  for the future frontend). Owner decisions: both entry points, NEEDS_REVIEW warn-and-proceed,
  manual pinning deferred. (The original "maximize matches via displacement chains" decision was
  SUPERSEDED by the 11.5 tiered cost model below.)
  - 11.1 `normalize_choices` (tier-token containment parse of the messy free-text choice strings,
        order-preserving dedupe) + cohort pydantic models (`CohortCapacities`/`CohortAssignment`/
        `TierSummary`/`CohortSummary`/`CohortResult`) + `cohort.tiers` CONFIG section.
  - 11.2 `assign_cohorts`: rank-greedy walk + displacement chains (augmenting paths) = maximum-
        cardinality matching with rank priority; only `RANKED` assignable; rank-ordered waitlist;
        `unassignable` bucket for unparseable choices; invariant tests (capacity, monotonicity,
        determinism, 2-hop chains, weakest-displaced).
  - 11.3 `cohort_assignments_csv` — one rank-ordered CSV across all statuses (per-tier rosters and
        the waitlist are filters of it); summary embedded in the JSON result.
  - 11.4 API: `POST /jobs/{id}/cohorts` (chained off a completed job, non-evicting so staff can
        iterate capacities) + `POST /cohorts` (re-uploaded `decisions.jsonl`, the durable entry
        point); capacities + `format=json|csv` as query params; graceful 413/422, never 500.
  - 11.5 Policy revision — **tiered cost model** (supersedes the 11.2 displacement design):
        tiers are ordered by competitiveness AND cost (honors > intensive > regular; the
        `cohort.tiers` config order is now load-bearing). Strict first-choice **cost ceiling** —
        a student is never placed above their first-choice tier, even one they listed #2/#3
        (pruned tiers reported in `excluded_by_cost`). Capped tiers fill **strictly by rank**
        among choosers (displacement machinery deleted). **No silent overflow** into regular:
        a student whose eligible choices are all full → waitlist/manual-review bucket with a
        reason naming the chosen program(s) + explicit regular eligibility. Regular cap stays an
        optional knob (uncapped default). API shape unchanged.
- **Phase 12 — Resume bonus (Stage 6, PRD §7.2 — now IN SCOPE; supersedes "deferred")** —
  `src/srip_filter/scoring/resume.py` (stub → real) + a new download module + `llm/prompts/
  task_e.py`, tests mirroring each. Fills the existing slot rather than restructuring:
  `Scores.resume_bonus` is already in the §10.1 composition and the PRD's "≈110, 120 once resume
  is built" line settles `bonus_max = 10`. §0.3 invariants unchanged: **bonus-only** (never
  rejects, never subtracts, can never change a `REJECTED`/`NEEDS_REVIEW` outcome), absence
  neutral (148 blanks), any failure → 0 bonus + audit `errors[]` note (the Task C precedent).
  Stage 6 runs on gate-survivors only → rejected rows cost zero downloads and zero tokens.
  Hosting design rule: **fetch → extract → discard per applicant inside `grade_one`** — resume
  bytes never accumulate, so peak transient memory = `download_concurrency × max_download_bytes`
  (≈40 MB) regardless of batch size, free-tier safe at the 2000-row ceiling. `resume.bonus_max:
  0` restores exact stub behavior with zero fetches (safe rollout, instant rollback).
  - 12.1 Config + contracts: expand `ResumeConfig` (`bonus_max: 10`, `max_download_bytes`,
        `download_timeout_s`, `download_concurrency`, `allowed_url_hosts`, `max_text_chars`,
        signal weights) + `llm.models.task_e` (mini tier); `TaskEOutput` contract +
        `ResumeAssessment` audit block on `AuditRecord`. Pure, tests.
  - 12.2 Download layer (network, no LLM): async `fetch_resume(url, cfg)` via `httpx` (promote
        the existing transitive dep to direct) — **https-only + `allowed_url_hosts` allowlist
        (SSRF guard: URLs come from an uploaded CSV)**, streaming size-cap abort, timeout,
        retry-once, its **own semaphore** (separate from the LLM one); typed failure reasons,
        never an exception out. `httpx.MockTransport` tests, zero real network.
  - 12.3 PDF extraction (pure): `extract_resume_text` via `pypdf` — magic-bytes check, per-page
        text, `max_text_chars` cap (~15k, bounds token spend), empty text (scanned PDF) → typed
        failure, no OCR dependency. (Deviation: PRD §7.2 mentioned `pdfplumber`; `pypdf` is the
        lighter dep tree for text-only extraction on small hosts.)
  - 12.4 Task E prompt + pure bonus math: the model extracts structured signals (projects,
        experience, awards, CS/DS relevance) but **never prices them** — deterministic math
        recomputes weights from config (the Task C "model classifies, config prices" pattern),
        capped at `bonus_max`, never negative. Pure, zero-spend tests.
  - 12.5 Stage 6 aggregator + wiring: `score_resume` replaces the stub — `bonus_max == 0` OR
        blank URL → 0 with no fetch and no token; else fetch → extract → Task E → math; any
        failure at any step → 0 bonus + `errors[]` note, never `NEEDS_REVIEW`/`REJECTED`. Wire
        into `grade_one`, extend the §12 invariant suite, add the demo-handler `task_e` and the
        audit-browser Resume detail block.
  - 12.6 Scale verification + docs: batch test over `MockTransport` at volume asserting the
        memory discipline (no resume bytes retained on records); README/openissue updates.

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
- [x] Phase 6.1 + 6.2 — `scoring/school.py`: `match_school` (lru_cache load of `schools.json`,
      normalize, rapidfuzz token-set match of name+aliases ≥ threshold, both-lists tiebreak →
      higher-bonus list) + `score_school` Stage 7 aggregator (list → bonus; unmatched/"High
      School"/blank → 0, never negative). Landed together (shared module + test file).
- [x] Phase 6.3 — `scoring/resume.py`: `resume_bonus` inert stub → `resume.bonus_max` (0) for
      everyone, clearly-labeled DEFERRED TODO; test always-0.
- [x] Phase 7.1 — `scoring/aggregate.py`: `compose_final_score` (pure §10.1 additive sum of the
      five subscores) + `finalize_score` (writes it onto the record); §10.1 composition + §12 #1
      tests (commit: 8d92993).
- [x] Phase 7.2 — `rank_records` Stage 8 aggregator: gate-survivors → `final_score` + RANKED;
      deterministic tiebreaker (`final_score`→`gpa_points`→`essay.total`→`submission_id`) → rank
      1..N; REJECTED/NEEDS_REVIEW forced to None; tests cover order, tiebreaker, §12 #2/#5
      (commit: 933adc5).
- [x] Phase 7.3 — `outputs.py` Stage 9 emission: pure in-memory serializers (`decisions_jsonl`,
      `ranked_csv`, `rejected_csv`, `needs_review_csv`, `build_summary`) + on-disk `write_outputs`;
      deterministic; tests pin columns/sort/reconciliation (commit: 75e7ee1).
- [x] Phase 7.4 — consolidated §12 invariant suite over a synthetic three-outcome population
      (`tests/scoring/test_aggregate.py`); all five invariants asserted end-to-end (commit: f1ac0b6).
- [x] Phase 8.1 — `pipeline.build_base_record` (identity/dedup/program_choices assembly, RANKED
      placeholder) + `affirmation_ok` (unchecked → NEEDS_REVIEW, only when the column resolved);
      pure zero-spend tests (commit: c8f795a).
- [x] Phase 8.2 — `grade_one` per-applicant fail-fast runner: sequences Stage 1 → affirmation →
      GPA → essays → bonuses, fills every audit block, stamps terminal outcome on the first gate to
      fire, survivor → RANKED/`final_score=None`; per-row try/except → NEEDS_REVIEW; `llm_calls`
      inferred from stage results. `FakeLLMClient` branch tests (commit: db52f73).
- [x] Phase 8.3 — `grade_batch` batch runner + `BatchResult`: ingest → concurrent `grade_one`
      (`asyncio.gather`, client semaphore bounds concurrency) → `rank_records` → in-memory Stage 9
      artifacts + `IngestReport`; synthetic-CSV integration tests (commit: c3dc6bc).
- [x] Phase 8.4 — end-to-end §12 invariant + fail-fast spend suite (`tests/test_pipeline.py`): all
      five PRD §12 invariants over `grade_batch`, plus zero-token assertions for Stage-1/affirmation
      stops (commit: 0557ed3).
- [x] Phase 9.1 — API scaffold: `ApiConfig` (max_upload_bytes/max_rows/job_ttl_seconds) in
      config.py + config.yaml; `api/` package — `JobRegistry` (UUID-keyed lifecycle + progress +
      in-memory `BatchResult`, TTL eviction + discard-after-download, lockless single-loop),
      `schemas.py` (JobCreated/JobStatus/ErrorResponse/HealthResponse), `main.py` `create_app`
      factory + `/health`; tests over registry lifecycle/sweep + JobStatus + health (commit: 6c75924).
- [x] Phase 9.2 — `POST /jobs`: `read_upload_capped` (streaming 413), `validate_csv` (parseability/
      header/row-cap → 422/413, never 500), `run_job` background task awaiting `grade_batch`; one
      `OpenAILLMClient` built at startup via lifespan, `FakeLLMClient` injected in tests; added
      python-multipart to the api extra; status codes as int literals (commit: 7f6d002).
- [x] Phase 9.3 — `GET /jobs/{id}` polling + status; the one HTTP-free core seam: optional
      `progress(rows_done, rows_total)` callback on `grade_batch` (default None) → live poll
      progress; failed job → safe message; 404 unknown/evicted (commit: 586e27a).
- [x] Phase 9.4 — `GET /jobs/{id}/results/{artifact}` (five artifacts, Enum path param → 422 on
      bad name; 409 before-done; 404 unknown), `DELETE /jobs/{id}` discard (→204/404), background
      `sweeper_loop` (lifespan-managed, `api.job_sweep_seconds`) for TTL eviction (commit: bf3b275).
- [x] Phase 11.1 — `normalize_choices` (containment parse, both dash formats, repeats dedupe,
      ambiguous/garbage dropped) + cohort models + `cohort.tiers` CONFIG (commit: 52e504e).
- [x] Phase 11.2 — `assign_cohorts` rank-greedy + displacement chains (max matching); invariant
      tests incl. brute-force capacity/monotonicity sweeps (commit: 52f0c12).
- [x] Phase 11.3 — `cohort_assignments_csv` single rank-ordered artifact (commit: 0a40e31).
- [x] Phase 11.4 — `POST /jobs/{id}/cohorts` (non-evicting what-if) + `POST /cohorts`
      (decisions.jsonl re-upload); capacities/format query params; 404/409/413/422 edges
      (commit: 3943b6c).
- [x] Phase 11.5 — tiered cost model: strict first-choice cost ceiling (`excluded_by_cost`),
      rank-filled caps, displacement removed, waitlist = manual-review bucket naming chosen
      programs + regular eligibility; optional regular cap kept (commit: 0ccbe4f).
- [x] Phase 10.0 — PLAN.md deviation entry (React+Vite → server-rendered Jinja2) + Phase-Map
      rewrite (commit: deeba7a).
- [x] Phase 10.1 — UI wiring + shell + theme: `jinja2` dep; `StaticFiles` + `Jinja2Templates` +
      `register_pages` in `create_app`; `SRIP_DEV_FAKE_LLM=1` dev switch + `api/demo.py` handler;
      `api/web.py`, `base.html`, `app.css` (ThinkNeuro theme), vendored `logo.png`;
      `tests/api/test_web.py` (commit: c32f957).
- [x] Phase 10.2 — upload screen: `upload.js`/`common.js` — multipart upload, progress poll,
      summary (counts/histogram/needs_review), 5 download links, discard, cross-screen links
      (commit: a0a0858).
- [x] Phase 10.3 — audit browser: NDJSON fetch/parse, sortable+filterable table, row → full
      `AuditRecord` detail panel (gates/gpa/scores/coursework/school/trail) (commit: 94f9e54).
- [x] Phase 10.4 — cohort what-if: debounced live recompute over `POST /jobs/{id}/cohorts`,
      tier summary + warnings, standalone `decisions.jsonl` re-upload, CSV export (commit: ba4fe65).
- [x] Phase 10.5 — synthetic demo CSV (all ten outcome paths, narrow gitignore exception) +
      live-browser verification fixes (choice_N satisfaction keys, duplicate alert)
      (commits: d2cb42c, b9a4809).

## In Progress
- (none)

## Next Up
- [ ] Phase 12.1 — `ResumeConfig` expansion + `llm.models.task_e` + `TaskEOutput` contract +
      `ResumeAssessment` audit block
- [ ] Phase 12.2–12.6 — download layer, PDF extraction, Task E + bonus math, Stage 6 wiring,
      scale verification
- [ ] Owner inputs (openissue.md): `OPENAI_API_KEY` + zero-retention confirmation; curated
      profanity list; resume URL host allowlist (#5 — needed by 12.1/12.2).
- [ ] (Unscheduled) deployment to a host.

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
- Phase 6:   `uv run pytest tests/scoring/test_school.py tests/scoring/test_resume.py` (exact/alias/
  fuzzy/both-lists match + normalization; list→bonus mapping; unmatched/"High School"/blank→0;
  never-negative; bonus can't change an outcome; resume stub always 0)
- Phase 7:   `uv run pytest tests/scoring/test_aggregate.py tests/test_outputs.py` (score
  composition, deterministic ranking + tiebreaker, the five output artifacts, and all §12 invariants)
- Phase 8:   `uv run pytest tests/test_pipeline.py` (synthetic CSV end-to-end)
- Phase 9:   `uv sync --extra api && uv run pytest tests/api/` (registry lifecycle/TTL + health
  (test_api), upload validation/caps 413/422/503 (test_upload), polling + run_job failure + the
  core progress callback (test_status), artifact download/409/404 + DELETE discard + sweeper
  (test_download); FastAPI `TestClient`, injected `FakeLLMClient`, no LLM spend)
- Phase 11:  `uv run pytest tests/test_cohort.py` (normalization, assignment invariants, CSV) and
  `uv sync --extra api && uv run pytest tests/api/test_cohorts.py` (both endpoints, lifecycle +
  malformed-upload edges; no LLM spend)
- Phase 10 (automated): `uv sync --extra api && uv run pytest tests/api/test_web.py` (page routes
  200 + markers, static assets, JSON-API regression guard)
- Phase 10 (manual, zero-spend — TestClient can't run browser JS):
  1. `uv sync --extra api`
  2. PowerShell: `$env:SRIP_DEV_FAKE_LLM = "1"; uv run uvicorn api.main:app --port 8000`
     (or set a real `OPENAI_API_KEY` in `.env` and omit the flag for a true, token-spending run)
  3. Open `http://localhost:8000/` → upload `resources/demo/sample_applications.csv` → progress →
     summary shows 4 RANKED / 4 REJECTED / 2 NEEDS_REVIEW → all five downloads work
  4. "Browse audit records" → sort/filter → open a row → gates/GPA/scores/coursework/school/trail
     all render
  5. "Cohort what-if" → set Honors=0 → rank 1 moves to her second choice; re-upload a saved
     `decisions.jsonl`; "Download assignments CSV"
  6. "Discard job & results" → subsequent fetches 404 gracefully

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

- **Phase 6 breakdown (plan-time):** Phase 6 is entirely deterministic (no LLM), so there is no
  isolate-the-LLM split like Phases 3–5; instead split along the two stages — 6.1 `match_school`
  (load + normalize + rapidfuzz), 6.2 `score_school` (list → bonus, Stage 7 aggregator), 6.3 the
  inert resume stub (Stage 6). Decisions to settle in implementation: (a) **`match_school` owns the
  both-lists tiebreak**, not the bonus layer — a school appearing in both `us_top20` and
  `intl_top50` is reported under whichever list has the higher *configured* bonus, so `match_school`
  takes `cfg` and `SchoolMatch.list` is authoritative; 6.2 then becomes a pure `list → bonus`
  lookup. (b) The schools resource is **loaded once via `lru_cache`** (committed, non-PII), matching
  the profanity-matcher pattern from Phase 2.2; a single canonical candidate set = each school's
  `name` + its `aliases`. (c) **"High School", blanks, and any below-threshold match → empty
  `SchoolMatch` + 0 bonus, never negative** — the §0.3 "absence is neutral / can only add" invariant,
  and a §12 invariant test asserts a school bonus can neither manufacture nor rescue a `REJECTED`
  outcome. (d) The resume stub returns `ResumeConfig.bonus_max` (0) for everyone with a clear `TODO`
  — the slot exists but PDF download + parsing stays unplanned (PRD §7.2). No new config or owner
  dependency — `SchoolConfig`, `ResumeConfig`, `SchoolMatch`, and `resources/schools.json` already
  exist (Phase 0).

- **Phase 6 (implementation):** `match_school` flattens `schools.json` once via `lru_cache` into
  per-(name/alias) candidates tagged with the school's canonical `name` + list, then scores the
  normalized institution against every candidate with `rapidfuzz.fuzz.token_set_ratio`, keeping
  the **best score per canonical school** (so a short alias like `MIT`=100 wins over the long
  full-name candidate for the same school). The winning school = `max` by `(score, name)` —
  the canonical-name tiebreak keeps equal-score ties deterministic. Below `fuzzy_match_threshold`
  → empty `SchoolMatch`. Both-lists tiebreak is resolved by canonical-name set membership →
  `max` list by *configured bonus* (so Harvard/MIT/etc. report `us_top20` at 15 > `intl_top50`
  at 12), making `SchoolMatch.list` authoritative and `score_school` a pure list→bonus lookup.
  Normalization = lowercase + non-word→space + whitespace-collapse (matches PRD §7.1). Resume
  stub lives in its own `scoring/resume.py` per the project structure (not folded into school.py).

- **Phase 7 breakdown (plan-time):** Phase 7 is entirely deterministic (no LLM), so it splits by
  concern rather than isolate-the-LLM: 7.1 pure `compose_final_score`, 7.2 `rank_records`
  (outcome finalize + ranking), 7.3 `outputs.py` emission, 7.4 the consolidated §12 invariant
  suite. Decisions to settle in implementation: (a) **`final_score` is computed for `RANKED`
  applicants only** — `REJECTED`/`NEEDS_REVIEW` keep `final_score=None`/`rank=None`; composition
  is the plain §10.1 additive sum of the five existing subscores (no new config, no new weights —
  the per-component caps already live in their own config sections). (b) **Tiebreaker fallback is
  `submission_id`, not a timestamp** — the §2 data contract carries no submission-timestamp column,
  so the deterministic tiebreaker chain is `final_score` desc → `gpa_points` desc → `essay.total`
  desc → `submission_id` asc (a stable UUID), which keeps reruns identical (§12 #5) without
  depending on a field we don't have. (c) **`outputs.py` serializers return in-memory
  strings/dicts**, with a thin `write_outputs(records, out_dir)` convenience on top — the stateless
  API (Phase 9) hands results back to the user as downloadables and never persists server-side, so
  the core must be able to produce the artifacts without touching disk. (d) **No acceptance cutoff**
  — the full ranked list is the deliverable; acceptance/cohort filling is the deferred downstream
  step (§11). The §12 invariants are asserted at this aggregate/output level here in Phase 7; the
  full end-to-end pass over a synthetic CSV is the Phase 8 integration test.

- **Phase 7 (implementation):** `compose_final_score(scores, cfg)` rounds the five-term §10.1 sum
  to 4 dp (matches the subscore rounding elsewhere); `cfg` is unused today but kept in the
  signature for parity with the other scoring entry points and future composition tuning.
  `finalize_score`/`rank_records` **mutate the `AuditRecord`s in place** and return them (pydantic
  models are mutable; the ranking pass is idempotent so reruns are stable — §12 #5). `rank_records`
  treats any record whose `outcome` is **not** already `REJECTED`/`NEEDS_REVIEW` as a gate-survivor
  → sets `outcome="RANKED"` + `final_score`; it force-clears `final_score`/`rank` to `None` on the
  two terminal outcomes (so a bonus can never score/rank a rejection — §12 #2). Tiebreaker negates
  the numeric keys for a single ascending sort: `(-final_score, -gpa_points, -essay.total,
  submission_id)`. `rank_records` returns the list in **input order** with `rank` carrying the
  ordering; `ranked_csv` re-sorts by `rank`. `outputs.py` serializers are pure and return
  in-memory `str`/`dict` (stateless API streams them, never persists); `rejected_csv`/
  `needs_review_csv`/the summary `needs_review` list sort by `submission_id` for byte-identical
  reruns. The summary histogram buckets `RANKED` final_scores in fixed width-10 bins
  (`_HISTOGRAM_BUCKET`), filling empty interior bins so the distribution reads continuously; an
  empty `RANKED` set → `{}`. CSVs use `lineterminator="\n"` (not the csv default `\r\n`) for
  portability. The rejected CSV's "failing_stage" column = `decided_at_stage`; the §12 #3 gate name
  lives in `primary_reason` (the invariant field).

- **Phase 8 breakdown (plan-time):** orchestration lives in `src/srip_filter/pipeline.py` (the
  transport-agnostic core; FastAPI is Phase 9). Split 8.1 deterministic glue → 8.2 per-applicant
  runner → 8.3 batch runner → 8.4 end-to-end suite, isolating the pure record-assembly + affirmation
  logic (zero-spend testable) from the LLM-driven sequencing, mirroring the isolate-the-LLM pattern
  of Phases 3–5. Decisions to settle in implementation:
  (a) **The affirmation-unchecked → `NEEDS_REVIEW` check (PRD §2/§10.2) is implemented here, in
  Phase 8** — it exists in no stage module today. It is deterministic and cheap, so it runs early
  (before any LLM spend), but it **only fires when the affirmation column actually resolved** (is
  in `resolution.role_to_header`); an absent column must not be read as "everyone unchecked" and
  blanket-route the whole batch. The affirmation role is optional in the §2 contract, so this guard
  is load-bearing.
  (b) **Fail-fast order: `REJECTED` precedes `NEEDS_REVIEW`.** Per row: Stage 1 essay gates
  (REJECTED) → affirmation (NEEDS_REVIEW) → Stage 2–3 GPA → Stage 4 essays → bonuses. Running the
  hard reject gates first honors §0.7 ("the only path to REJECTED is an affirmative hard-gate
  failure") — an applicant who both wrote profanity and left the affirmation blank is `REJECTED`,
  not `NEEDS_REVIEW`.
  (c) **Survivors leave `grade_one` as `outcome="RANKED"` with `final_score=None`.** The
  `AuditRecord.outcome` Literal has no "pending" value, so a gate-survivor is marked `RANKED`
  immediately; Stage 8 `rank_records` (which treats any non-terminal outcome as a survivor) then
  fills `final_score` + `rank`. No schema change needed.
  (d) **Per-row isolation:** the whole `grade_one` body is wrapped in `try/except`; an unexpected
  error becomes a `NEEDS_REVIEW` record with an `errors[]` note, never an aborted batch ("when
  grading begins, it finishes"). This is distinct from the *designed* `NEEDS_REVIEW` routes
  (unscalable GPA, unchecked affirmation, Task B/D parse failure). Coursework/resume failures stay
  bonus-neutral (0), per the Phase 5 decision.
  (e) **`grade_batch` returns a `BatchResult`** bundling the records + the five in-memory artifacts
  + the Stage-0 `IngestReport`; nothing is written to disk by default (stateless; the API streams
  it). Concurrency is bounded by the existing `llm.max_concurrency` semaphore inside the client, so
  `grade_batch` can `asyncio.gather` all rows without its own pool. No new config, no owner
  dependency — Phase 8 is pure wiring over stages that already exist.

- **Phase 8 (implementation):** `grade_one` fills the audit Gates blocks for *every* path (Stage 1
  is token-free so all three blocks are always set before the reject check; GPA/essay-relevance
  blocks set as those stages run). Terminal outcomes go through a small `_terminal` helper that also
  force-clears `final_score`/`rank` to `None`, so a REJECTED/NEEDS_REVIEW row is never carried with a
  stale score. **`llm_calls` is inferred from stage *results*, not by instrumenting the client**
  (the stage fns don't report calls): `gpa.assessment.source == "llm"` ⇒ `task_a`, a populated
  `explanation_eval` ⇒ `task_b`, reaching Stage 4 ⇒ `task_d_e1`+`task_d_e2` (both attempted even on a
  parse failure), a non-empty coursework cell ⇒ `task_c`. `decided_at_stage` labels: `stage1` /
  `affirmation` / `stage3` (both GPA reject and the unscalable-scale needs_review) / `stage4` /
  `stage8` (survivor) / `error` (the per-row isolation fallback). The gibberish audit block is the
  reconciliation `HitGate(stage1.gibberish.hit or task_d.gibberish.hit)` (both can independently
  reject; a Stage-1 gibberish hit would already have rejected, so at Stage 4 only Task D can flip it).
  Survivors are stamped `RANKED` + `decided_at_stage="stage8"` + `primary_reason="Survived all gates"`
  with `final_score=None`; `rank_records` then fills score + rank. The outer `try/except` only ever
  catches *unexpected* errors — `LLMParseFailure` is already converted to verdicts inside the GPA/
  essay stages, so it never reaches it. `BatchResult` bundles records + the four string artifacts +
  the summary dict + the `IngestReport`, all in memory (the Phase 9 API streams them; `write_outputs`
  is the opt-in disk path). The 8.4 suite drives Task B end-to-end via a scripted handler, so the
  test CSV harness adds the extenuating-circumstances column.

- **Phase 9 breakdown (plan-time):** the API is a thin stateless shell over `pipeline.grade_batch`;
  split 9.1 scaffold/registry → 9.2 upload+validation+kickoff → 9.3 polling → 9.4 download+TTL.
  Decisions to settle in implementation:
  (a) **New `ApiConfig` CONFIG section** (`max_upload_bytes`, `max_rows`, `job_ttl_seconds`) in
  `config.yaml` + `config.py` — edge caps are magic numbers and belong in config, not the handlers.
  (b) **One core touch, HTTP-free:** an optional `progress: Callable[[int, int], None] | None = None`
  on `grade_batch` so the job can report `rows_done/rows_total` to the poll. Everything else lives in
  `api/`; the core never imports FastAPI. (Deferred to 9.3 so 9.1/9.2 don't change the core.)
  (c) **In-memory job registry only** — a dict keyed by UUID with lifecycle state + TTL eviction +
  discard-after-download. No DB, no queue; an interrupted run (host restart / page refresh) is
  abandoned and nothing is persisted (the §0/Privacy stateless decision). PII (essays, GPAs) lives
  only inside the transient `BatchResult` and is evicted on download or TTL.
  (d) **Graceful 4xx, never 500:** oversize upload → 413; row cap exceeded, `HeaderValidationError`,
  and unreadable/garbled CSV → 422; download-before-done → 409; unknown/evicted job → 404. A failed
  background job is captured on the job (`status="failed"` + a safe message — never a stack trace or
  PII), surfaced via the poll, not raised.
  (e) **One `OpenAILLMClient` at app startup** from config/secrets (its semaphore already bounds LLM
  concurrency, so the background task just awaits `grade_batch`); tests inject a `FakeLLMClient` via
  dependency override so the whole suite is zero-spend. **No auth initially** (nothing is stored);
  serve over HTTPS at deploy. No new framework beyond FastAPI/uvicorn (already settled in CLAUDE.md).

- **Phase 9.1 (implementation):** the `api/` package is split into `registry.py` (`JobRegistry` +
  `Job` + `JobState`), `schemas.py` (response models), and `main.py` (`create_app` factory +
  `/health`) — three small concerns rather than one `main.py`, each independently testable.
  `create_app(*, config=None, client=None)` stashes `config`/`llm_client`/`registry` on
  `app.state`; the module-level `app = create_app()` is the uvicorn entry (`uvicorn api.main:app`).
  **`client` is left `None` in 9.1** (no grading route yet) — the real `OpenAILLMClient` is built at
  startup in 9.2; tests inject a `FakeLLMClient`. **Registry clock = `time.monotonic()`**, not
  wall-clock: TTL math must be immune to wall-clock jumps, and the lifecycle clock is internal (not
  shown to the user). A `Job` is a mutable dataclass the handlers mutate in place; the registry owns
  only storage + eviction (`create`/`get`/`evict`/`sweep`). **TTL reference time** = `finished_at`
  for a terminal job, else `created_at` — so a *wedged* unfinished run is also reaped (can't pin PII
  forever), and `is_expired` is **inclusive** at the boundary (`now - ref >= ttl`). **Lockless:** all
  access is on the single API event loop (the background grading task is an `asyncio` task in the
  same loop, not a thread), so the plain dict needs no lock — revisit only if a thread pool is ever
  introduced. `JobState` is a `StrEnum` (py311+) so it serializes to its string value for free.
  `fastapi`/`uvicorn` is the `api` optional-dependency extra — run `uv sync --extra api` before the
  API suite (CI/deploy installs it; the core suite doesn't need it).

- **Phase 9.2 (implementation):** `read_upload_capped` streams the multipart body in 1 MiB chunks
  and aborts with 413 the moment it passes `max_upload_bytes` (peak memory = cap + one chunk) — no
  reliance on a client-supplied `Content-Length`. `validate_csv` parses the CSV **once at the edge**
  (via the Stage-0 `read_csv_records`/`validate_headers`, so edge and core agree on "valid"); the
  CSV is then parsed **again** inside `grade_batch` (Stage 0). That double-parse is deliberate —
  it keeps the core untouched and a re-parse of a ≤25 MiB blob is cheap next to LLM grading. Order
  of checks = cost order: parseability (`ValueError`/`UnicodeDecodeError`, covers pandas
  `EmptyDataError`/`ParserError` which subclass `ValueError`) → 422; header contract
  (`HeaderValidationError`) → 422; row cap → 413. **Never 500** for a bad upload (PRD Privacy).
  Status codes are **plain int literals** (413/422) not `fastapi.status.*` — Starlette renamed the
  413/422 constants and the old names warn on access; literals stay correct across the supported
  FastAPI range. **`File` is `Annotated[UploadFile, File()]`** (not a default arg) to satisfy ruff
  B008. The real `OpenAILLMClient` is built **once in the lifespan** (not at import, so importing
  `api.main` never needs an API key); if a test injects a client, the lifespan skips the build, so
  the whole suite is zero-spend. Background tasks are held in an `app.state.background_tasks` set
  (strong refs) with a done-callback discard, so a fire-and-forget `asyncio.create_task` isn't GC'd
  mid-run. A missing client at request time → 503 (only reachable if startup was skipped without an
  injected client). `run_job` captures any whole-run failure as a **safe** generic message (never
  PII/stack trace) and always stamps `finished_at` (starts the TTL clock); per-row errors are
  already absorbed inside the pipeline, so reaching the `except` means an unexpected whole-run
  failure (e.g. `grade_batch`'s re-ingest hitting a `HeaderValidationError`).

- **Phase 9.3 (implementation):** the **one HTTP-free core touch** is an optional
  `progress: Callable[[int,int],None] | None = None` on `grade_batch` (default keeps the signature
  compatible; the core never imports the API). It fires `(0, total)` after ingest and `(done,
  total)` after each row, ending at `(total, total)`. Safe under the concurrent `asyncio.gather`
  because the `nonlocal done` increments happen at `await` boundaries on the single event loop — no
  data race, no lock. `run_job` passes a closure writing `rows_done`/`rows_total` onto the `Job`,
  so the poll reflects live progress. (Note: under the sync `FakeLLMClient` a whole batch can finish
  in one loop turn, so HTTP tests usually observe QUEUED→SUCCEEDED; the *core* progress test asserts
  the full tick sequence directly.) `GET /jobs/{id}` returns the lifecycle + progress and, once
  `SUCCEEDED`, the run `summary`; a failed job surfaces `state="failed"` + the safe message; unknown/
  evicted id → 404.

- **Phase 9.4 (implementation):** **discard-after-download is explicit, not per-artifact.** There
  are five artifacts, so auto-evicting on the first download would strand the other four — instead
  `GET …/results/{artifact}` is **non-evicting** (all five retrievable) and the client calls
  `DELETE /jobs/{id}` once it has saved everything (→ 204; a double-discard is an honest 404). The
  background `sweeper_loop` (lifespan-managed task, interval = new `api.job_sweep_seconds`, default
  300 s) is the automatic TTL backstop so PII isn't held even if the client never deletes. The
  artifact name is an **`ArtifactName` StrEnum path param** so FastAPI rejects an unknown name with
  422 and self-documents the valid set in OpenAPI; the `summary` dict is JSON-encoded on the way
  out, the four string artifacts served verbatim, each with its content type +
  `Content-Disposition: attachment; filename=…`. Download before `SUCCEEDED` (queued/running/failed)
  → 409; unknown/evicted job → 404. The `ttl_seconds=0` sweeper test makes every job immediately
  expired so one tick evicts deterministically (no long sleep). **New config key**
  `api.job_sweep_seconds` (the sweep interval is a magic number → config, like the other caps).

- **Phase 11 (owner decisions, this cycle):** (a) **Maximize matches** — when a student's listed
  tiers are all full, an already-seated student may be displaced to another tier *they themselves
  listed* to make room (single- or two-hop chains); the goal is filling cohorts to capacity, with
  constrained (fewer-choice) students beating flexible ones for contested seats. Displacement
  never fires to upgrade anyone's choice — only to seat an otherwise-unmatched student — and the
  weakest (lowest-ranked) movable occupant is the one displaced. (b) **Both entry points**:
  chained `POST /jobs/{id}/cohorts` + standalone `POST /cohorts` over a re-uploaded
  `decisions.jsonl` (survives TTL eviction / restarts / later sessions). (c) **NEEDS_REVIEW →
  warn and proceed** (assignment over `RANKED` only, prominent warning + count in the summary)
  so staff can preview sizing before every review case is resolved. (d) **Manual pinning/
  overrides deferred** to the frontend phase; the result model accommodates a future pinned-
  assignments map non-breaking.
- **Phase 11 (implementation):** rank-greedy + augmenting-path displacement = maximum-cardinality
  matching with rank priority (greedy-with-augmentation processed in rank order); deterministic
  via fixed tiebreaks (rank → submission_id; chain search explores tiers in configured order;
  displaced occupant moves to *their* highest-listed open choice). Tier tokens are parsed by
  case-insensitive **containment** (the form emits `Summer 2026- X` and `Summer 2026 - X`
  inconsistently); a slot with zero or two tokens is dropped; repeated tiers dedupe (28 real
  applicants list one tier three times). Capacities are **per-request query params, not config**
  (`None`/omitted = unlimited) — they are the staff's live what-if knob; only the canonical
  `cohort.tiers` token list is config. Both endpoints are synchronous (pure core, milliseconds —
  no job/registry entry; the response is the whole result, nothing stored) and **non-evicting**
  on the chained route so capacities can be iterated against one job. One CSV artifact
  (`cohort_assignments.csv`, rank-ordered across assigned/waitlisted/unassignable; rosters are
  filters of it) via `?format=csv`; JSON (`CohortResult`) is the default. decisions.jsonl
  re-upload errors echo line numbers only, never applicant content.

- **Phase 11.5 (owner decisions — SUPERSEDES the Phase 11 "maximize matches" model):** new owner
  information: the three programs are ordered by competitiveness AND cost — honors (most
  competitive/expensive) > intensive > regular; staff caps honors/intensive and regular is the
  landing tier for applicants who aren't rejected. Decisions: (a) **Strict first-choice cost
  ceiling** — a student is never placed in a tier above their *first choice*, even one they
  explicitly ranked #2/#3 (the first choice signals what they're prepared to pay; in the real
  data 67 applicants ranked R-I-H → eligible for regular only, 54 ranked I-H-R → honors
  excluded). Pruned tiers are surfaced in a new `excluded_by_cost` field (JSON + CSV column,
  replacing `displaced_from`). (b) **No silent overflow into regular** — a student whose
  eligible choices are all full goes to the **waitlist/manual-review bucket**, with a reason
  naming the chosen program(s) at capacity, any ceiling-excluded tiers, and (when they didn't
  list it) explicit "still eligible for regular — staff decision required"; staff handles these
  by hand. Students who *listed* regular fall there via the normal walk. (c) **Displacement
  chains removed** — with the cost ceiling and regular-as-landing-tier, capped tiers fill
  *strictly by rank* among the students who chose them (simplest, most defensible); the
  augmenting-path machinery and the `displaced`/`displaced_from` fields were deleted.
  (d) **Regular capacity stays an optional knob** (uncapped default; if set and it binds, the
  lowest-ranked regular-choosers waitlist). API shape unchanged — same params, same routes.
  `cohort.tiers` config order is now **load-bearing** (cost order, most expensive first).
  New invariant test: across all capacity combos, no student is ever assigned a tier more
  expensive than their first choice.

- **Phase 10 frontend = server-rendered FastAPI (Jinja2 + one CSS + vanilla JS), NOT React + Vite.**
  The PRD/PLAN named a React+Vite SPA; **superseded by owner decision** for a server-rendered UI —
  no Node/npm build step, no SPA framework — matching the `certificate-automation` project's actual
  stack (Flask + Jinja2 + vanilla JS) for visual continuity. FastAPI serves `Jinja2Templates` + a
  `/static` mount; the browser drives upload→poll→summary/downloads, the audit browser, and the
  cohort what-if via `fetch` against the existing frozen JSON API. **Same-origin → no CORS
  middleware.** All UI lives in `api/` (`templates/`, `static/`, `web.py`); the core
  (`src/srip_filter/`) stays HTTP-free. New dep: **`jinja2`** in the `api` extra; `aiofiles`
  deliberately omitted (Starlette `StaticFiles` has a sync fallback — fine for a handful of small
  assets). `GET /` was unclaimed; new wiring only **adds** routes + `app.state.templates` + the
  `/static` mount, so the two fragile API tests (`test_health_endpoint_ok`,
  `test_create_app_wires_registry_from_config_ttl`) stay green. **Full ThinkNeuro branding** (name +
  vendored `api/static/logo.png`) labeled "SRIP Track 2 — Application Filter". **No auth** (nothing
  persisted). The `job_id` flows screen-to-screen via the URL (`?job=<id>`) + `sessionStorage`
  (tab-scoped, matching the transient design); downloads are **non-evicting** (all five artifacts +
  cohort what-ifs repeat against one job) and `DELETE /jobs/{id}` is the only discard. Optional
  **`SRIP_DEV_FAKE_LLM=1`** launches with a `FakeLLMClient` + a small optimistic demo handler
  (in `api/demo.py`) so the whole UI can be demoed end-to-end with **no API key and zero token
  spend** (openissue #1 still open); default (real `OpenAILLMClient`) is unchanged. Interactive JS
  is verified **manually** (uvicorn + browser + the synthetic demo CSV) since TestClient can't run
  browser JS; `tests/api/test_web.py` covers the GET HTML routes (200 + markers) and static serving.

- **Phase 12 (plan-time) — resume parsing moves IN SCOPE (supersedes PRD §7.2 "deferred";
  owner decision).** The slot already exists (`Scores.resume_bonus` in §10.1, the deliberate
  `scoring/resume.py` stub), so this is a fill-in, not a restructure; the PRD's "≈110, 120 once
  resume is built" line settles `bonus_max = 10`. The §0.3 bonus-only invariants and the Task C
  failure precedent (any failure → 0 bonus + audit note, never a block) carry over unchanged.
  **Hosting analysis (the design driver — the server now fetches external files):**
  (a) **Memory:** naive download-all-then-process would hold ~318 × up to 10 MB on a 512 MB
  free-tier instance; the binding rule is **fetch → extract → discard per applicant inside
  `grade_one`**, making peak transient memory `download_concurrency × max_download_bytes`
  (≈40 MB) independent of batch size — holds at the 2000-row ceiling.
  (b) **Runtime:** downloads add ~2–5 min typical (~318 resumes, bounded by a **download
  semaphore separate from the LLM one**); the background-job + polling architecture absorbs it
  (no HTTP timeout risk), though the longer run window slightly raises the free-tier
  restart/abandonment exposure — the documented stateless trade-off.
  (c) **Token spend:** extracted text capped at `max_text_chars` (~15k) so a 30-page resume
  can't blow up a prompt; Task E uses the mini tier (mechanical extraction, the A/C pattern).
  (d) **SSRF:** resume URLs come from an *uploaded CSV*, so without an **https-only + host
  allowlist** (`allowed_url_hosts`, Fillout/S3 domains) a crafted CSV could make the host probe
  its internal network — this is config, not code complexity.
  (e) **Kill switch:** `resume.bonus_max: 0` restores exact stub behavior with zero fetches —
  safe rollout, instant rollback.
  **Stack deviations:** `pypdf` instead of the PRD's `pdfplumber` (much lighter dependency tree
  for text-only extraction on small hosts); `httpx` promoted from transitive (via `openai`) to a
  direct dependency for the download layer. Owner confirmed Fillout resume URLs are publicly
  fetchable (no auth); the exact host allowlist is an owner input (openissue #5).

## Owner-Supplied Dependencies (full detail in `openissue.md`)
- [x] `resources/schools.json` — Top-20 US + Top-50 International (source: U.S. News), frozen for Summer 2026.
- [~] Profanity list — using `better-profanity` DEFAULT list for now (owner approved).
      `resources/profanity.txt` placeholder scaffold committed (format documented, not yet
      loaded); curated slur list + medical/anatomical allowlist still needed (openissue.md #3).
- [ ] `OPENAI_API_KEY` in `.env` (openissue.md #1).
- [ ] OpenAI account set to zero/minimal data retention (openissue.md #2).
- [x] GPA threshold — settled at 3.0 (PRD §1). No decision needed.
- [~] Resume parsing — **now in scope (Phase 12)**; owner confirmed Fillout resume URLs are
      publicly fetchable. Still needed: the resume URL host allowlist (openissue #5). Stage 6
      stays the inert stub until 12.5 lands.
