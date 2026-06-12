# PRD — SRIP Track 2 (Software Engineering) Application Filtering System

**Owner:** Dominic
**Consumer of this doc:** Claude Code (implementation agent)
**Input dataset:** `Fillout__Su26__Application__CS__results.csv` (466 rows, 29 columns)

**What this system does (v2 scope):** It does exactly two things — (1) **reject** applications that fail deterministic/hard-gate quality checks, and (2) **score and rank** every surviving application. It does **not** decide acceptances. Acceptance, waitlisting, and cohort assignment are a separate downstream step (§11) that consumes this system's ranked output. The job here is: remove the obviously-weak applications, and produce a defensible ranking of the rest.

**Revision note (v2):** GPA threshold lowered from 3.3 → **3.0 (B average)**; the three-bucket model (deny/accept/waitlist) replaced by **REJECTED / RANKED / NEEDS_REVIEW**; no acceptance threshold; resume parsing explicitly deferred (not yet planned).

---

## 0. Design principles (read first — these govern every ambiguous case)

1. **Deterministic-first, fail-fast.** Cheap deterministic gates run before any LLM call. The moment an application hits a hard-reject gate, stop processing it and spend zero LLM tokens on it.
2. **Hard rules decide rejections. The score only ranks survivors.** Rejection is rule-based and binary. The additive score is computed *only* for applications that survive every hard gate, and is used solely to rank them. No score threshold accepts or rejects anyone at this stage.
3. **Non-required signals can only add, never subtract.** Resume, top-school attendance, and relevant coursework are bonuses. Their *absence* is neutral. Hard invariant — no code path may deduct points for a missing optional signal.
4. **Population reality: this is a high-school program.** 364 of 466 applicants (78%) listed "High School" as their institution. High-school status is *not* a reason to relax GPA or coursework standards. The top-college check is a pure bonus that simply will not fire for most applicants.
5. **One failed essay fails the application** — but only for *disqualifying* failures (profanity/gibberish/off-topic/grossly out-of-bounds length), not for soft penalties (slightly off length, minor grammar). See §4.
6. **Auditability is a feature, not a log.** Each applicant produces a structured decision record (§9) explaining every gate outcome and every subscore. The downstream cohort UI and any human auditor read these.
7. **Never silently reject.** The only path to `REJECTED` is an affirmative hard-gate failure. Anything unscoreable (unresolvable GPA scale, parse failure, unchecked affirmation) goes to `NEEDS_REVIEW`, not rejection.

---

## 1. GPA threshold (settled)

- **3.0 (a B average) is the threshold.** It is both the deny line and the bottom of the positive-signal range. Do **not** raise or lower it for high schoolers.
- **GPA ≥ 3.0** passes the gate and earns points on a **gradient** — higher is strictly better. A 3.2 must score meaningfully lower than a 3.7 (see §8.1).
- **GPA < 3.0** requires an extenuating-circumstances explanation, and **the explanation must scale with how far below 3.0 the GPA is.** A 2.9 needs a modest, realistic reason; a 2.4 needs a strong, concrete one. No explanation → `REJECTED`. Adequate explanation → `RANKED` (scored; the deficit is reflected in a low GPA subscore). Inadequate/unrealistic → `REJECTED`. Adjudicated by **LLM Task B** (§8.2).
- **GPA < 2.0 is a hard floor (owner decision, 2026-06-12):** automatic `REJECTED` regardless of any explanation — Task B is never called below the floor.
- **A blank GPA cell with a blank explanation is an affirmative non-answer → `REJECTED`** (owner decision, 2026-06-12). A blank GPA *with* an explanation present, or any non-blank unresolvable scale, still goes to `NEEDS_REVIEW` — never auto-rejected.
- A B average corresponds to ~83% / 3.0 on the conversion table in §6.1, so "below a B regardless of scale" and "below 3.0" are the same line.

| Normalized GPA (4.0 scale) | No explanation | Explanation present (severity-scaled) |
|---|---|---|
| ≥ 3.0 | Pass → gradient points, higher = better | n/a (already passing) |
| 2.0 ≤ GPA < 3.0 | `REJECTED` | `RANKED` if reason is strong & realistic enough for the deficit; else `REJECTED` |
| < 2.0 (hard floor) | `REJECTED` | `REJECTED` — no explanation can rescue below the floor |
| Unscalable (non-blank) | `NEEDS_REVIEW` (human resolves scale, then re-rank) | `NEEDS_REVIEW` |
| Blank cell | `REJECTED` (non-answer) | `NEEDS_REVIEW` |

---

## 2. Data contract (exact field names from the CSV)

Use these column headers verbatim. Quirks discovered in the actual file are noted because they *will* break naive parsing.

| Field (header) | Use | Notes / quirks found in data |
|---|---|---|
| `Submission ID` | Primary key | UUID. Use for idempotency/caching. |
| `Student First Name`, `Student Last Name` | Dedup secondary | 8 duplicate name-pairs exist that do **not** share an email — likely re-applications or siblings. Flag, don't auto-merge. |
| `What is your email address?` | Dedup primary | 6 emails appear >1 time → 6 surplus submissions. |
| `Please list your undergraduate institution of study below.` | School bonus (§7) | **364/466 = "High School".** Free text; misspellings and non-US names expected. 0 blank. |
| `What is your state of residence?` | Metadata | — |
| `First Choice`, `Second Choice (optional)`, `Third Choice (optional)` | Future cohort sizing | Values like `Summer 2026- HONORS / INTENSIVE / REGULAR`. Not used for reject/rank; carried into the audit record for the downstream cohort tool. |
| `GPA` | GPA gate (§6) | **The messiest field.** 394 numeric, 81 of those > 4.5 (weighted / 10-pt / percentage), 43 blank, 19 unparseable free text (`N/A`, `92/100 (Ethiopian National Curriculum)`, `IGCSE grades: A*,A*,A,B...`, `5/5`, `"my school doesn't offer GPAs"`, achievements stuffed into the cell). |
| `If your cumulative GPA is below 3.3, please briefly describe any extenuating circumstances...` | LLM Task B input | Explanation field. (Form copy still says 3.3; our logic uses 3.0 — applicants between 3.0–3.3 simply won't have filled it, which is fine since they pass.) Often blank. |
| `Relevant Coursework` | Coursework bonus (§5 / Task C) | 56 blank. Free text, comma-ish separated, grades in mixed scales. |
| `Resume (optional)` | Resume bonus | S3 URL to a PDF. **148 blank.** Parsing is **not yet planned** — see §7.2. Disabled in current scope. |
| `LinkedIn (optional)` | Optional metadata | 308 blank. Not scored. |
| Essay 1 — `What motivates you to apply to Track 2...(100-350 words)` | Length gate + Task D | min 0 / max 1873 words observed; 23 under 100, 23 over 350, 1 empty. |
| Essay 2 — `Track 2 is designed as a foundation for future research...(100-350 words)` | Length gate + Task D | min 0 / max 2109 words observed; 24 under 100, 14 over 350, 1 empty. |
| Consent/affirmation checkboxes (3) | Validity check | If the truthfulness affirmation is unchecked → `NEEDS_REVIEW`. |
| `Errors`, `Url`, `Network ID` | Ignore | Form-internal. |

Word count rule: tokenize with `re.findall(r"[\w'-]+", text)`. Both essays share the same 100–350 target.

---

## 3. Pipeline (ordered for fail-fast)

```
Stage 0  Ingest + Deduplicate          (deterministic)
Stage 1  Essay deterministic gates     (deterministic)   ── fail → REJECTED, STOP
Stage 2  GPA normalization             (deterministic + LLM Task A only when needed)
Stage 3  GPA gate                       (deterministic + LLM Task B only when needed) ── fail → REJECTED, STOP
Stage 4  Essay LLM grading             (LLM Task D)      ── off-topic → REJECTED, STOP
Stage 5  Coursework bonus               (LLM Task C)      bonus only
Stage 6  Resume bonus                   (DEFERRED — not implemented; contributes 0)
Stage 7  School bonus                   (deterministic match) bonus only
Stage 8  Aggregate score → RANK         (deterministic)
Stage 9  Emit audit record + outputs   (deterministic)
```

LLM calls only happen at Stages 2 (subset), 3 (subset), 4 (all survivors), 5. Everything rejected at Stage 1 or 3 costs zero essay/coursework tokens.

---

## 4. Stage 1 — Essay deterministic gates (no LLM)

Run on **both** essays. If **either** essay fails a *hard* check, the whole application is `REJECTED`.

**4.1 Length.** Target 100–350 words.

| Word count | Action |
|---|---|
| `HARD_MIN` ≤ wc < 100, or 350 < wc ≤ `HARD_MAX` | Soft penalty only (applied in essay score, §8.3). Not a rejection. |
| wc < `HARD_MIN` or wc > `HARD_MAX` | **Hard fail → REJECTED** ("significant margin"). |

Defaults (tunable, §10 CONFIG): `HARD_MIN = 60`, `HARD_MAX = 500`. Rationale: 60 words is well below a good-faith 100-word minimum; 500 is comfortably past the upper bound and into ignoring-instructions territory.

**4.2 Good-faith / profanity / gibberish.** Hard fail → `REJECTED`.
- **Profanity / vulgarity:** maintain a profanity wordlist; allow a *medical/anatomical exception* (clinical terms are fine). Match whole tokens, case-insensitive, with light leetspeak normalization.
- **Gibberish:** deterministic heuristics; require ≥2 signals to fire (this pool is heavily international, so avoid false positives on ESL writing):
  - dictionary-hit ratio below threshold (e.g. <40% real English tokens),
  - abnormally long consonant runs,
  - low character-entropy / repeated-character runs (`asdfasdf`, `aaaaaa`),
  - very low unique-word ratio.
  - **Do not** flag merely-awkward grammar as gibberish — that's a soft penalty in Task D, not a gate. ESL ≠ gibberish.

Stage 1 output: `{essay1_length_ok, essay2_length_ok, length_penalty_e1, length_penalty_e2, profanity_hit, gibberish_hit, verdict}`.

---

## 5. Relevant coursework — definitions (used by LLM Task C, §8.4)

Relevance ranking (most → least): **CS > Math > Data > (everything else = ignored).**
- CS / software / programming: strongest positive.
- Math (calculus, linear algebra, discrete, statistics-as-math): strong positive.
- Data (data science, analytics, ML, databases): slightly weaker positive.
- Anything else: **ignored, weight 0.** Not a penalty.

Grade rules (revised per owner decision, 2026-06-12 — grades are exclusion-only):
- A grade is considered **only when explicitly stated** for that course. A course with no stated
  grade counts at full weight — never guess or default a grade.
- **A course explicitly graded below a B (< 85%) is excluded entirely** (weight 0).
- Any counting course contributes a **flat** amount (`category_weight × COURSE_UNIT`); the grade
  never scales the bonus up or down.

Bonus only. Empty coursework (56 applicants) → 0 bonus, no penalty.

---

## 6. Stage 2 + 3 — GPA normalization and gate

### 6.1 Normalization (Stage 2)

Goal: convert as many GPAs to a 4.0-equivalent as possible *deterministically*; fall back to **LLM Task A** only for ambiguous/non-standard input. Minimizing `NEEDS_REVIEW` volume is an explicit objective.

**Deterministic path (no LLM):**
- Clean 4.0-scale values `0.0–4.0` → use as-is.
- Detectable **percentages** (`85/100`, `92%`, `95.2%`): apply the table below.
- Clear **/5** (`5/5`, `4.5/5`) or **/10** (`8.5/10`, `7.16`) scales: corresponding linear/table conversion.
- Strip trailing labels (`3.97 GPA`, `3.8/4.0 unweighted`) and parse the number.

**Percentage → 4.0 conversion table (default; tunable). The 3.0 row is the threshold (B average):**

| Percentage | 4.0 |
|---|---|
| 93–100 | 4.0 |
| 90–92 | 3.7 |
| 87–89 | 3.3 |
| 83–86 | **3.0 ← threshold (B)** |
| 80–82 | 2.7 |
| 77–79 | 2.3 |
| 73–76 | 2.0 |
| < 73 | scale linearly toward 0 |

**Weighted GPAs > 4.0** (`4.27`, `4.635`, `weighted: 4.4`): genuinely hard — a 4.4 weighted is not a 4.0 unweighted. Route to **LLM Task A** to estimate an unweighted-equivalent with a confidence level. Cap the result at 4.0.

**Route to LLM Task A** when: value > 4.5, non-numeric scales (IGCSE letter strings, "average is 8"), foreign curricula with a stated max, or any string the deterministic parser can't confidently resolve.

**Route to `NEEDS_REVIEW`** when even Task A returns `requires_manual_review = true` or `confidence = low` and the value can't be safely placed — e.g. `N/A`, `"my school doesn't offer GPAs"`, blank. **Do not reject for a missing/unscalable scale** — that would false-reject the large legitimate international contingent.

Normalization output: `{normalized_gpa: float|null, original_scale, conversion_method, confidence: high|med|low, below_threshold: bool, requires_manual_review: bool, source: "deterministic"|"llm"}`. (`below_threshold` ≡ `normalized_gpa < 3.0`.)

### 6.2 GPA gate (Stage 3)

```
if normalized_gpa is null or requires_manual_review:
    if gpa_cell is blank and explanation is blank:
        → REJECTED (reason: "No GPA provided and no explanation given")   # non-answer
    → NEEDS_REVIEW (reason: "GPA scale could not be normalized")     # not a rejection
elif normalized_gpa < 2.0:                # hard floor
    → REJECTED (reason: "GPA below the hard floor of 2.0")           # explanation cannot rescue
elif normalized_gpa >= 3.0:
    → PASS, compute GPA points (§8.1, gradient 3.0 → 4.0)
else:                                   # below 3.0
    explanation = <extenuating-circumstances field>
    if explanation is blank:
        → REJECTED (reason: "GPA below 3.0, no explanation")
    else:
        taskB = LLM Task B (normalized_gpa, gap = 3.0 - normalized_gpa, explanation)
        if taskB.recommended_outcome == "rank":
            → PASS, compute GPA points (will be low; deficit is reflected, not erased)
        else:
            → REJECTED (reason: taskB.rationale)
```

The further below 3.0, the higher the bar Task B applies (§8.2).

---

## 7. Stage 6 + 7 — Bonus signals (additive only)

### 7.1 School bonus (Stage 7, deterministic)
- Maintain two canonical lists in `schools.json`: **Top-20 US** and **Top-50 International** (you must curate these — pick a ranking source and freeze it per cycle; this is a dependency, §13).
- Match the institution string with normalization + fuzzy matching (lowercase, strip punctuation, `rapidfuzz` token-set ratio ≥ threshold). Log the matched name and score into the audit record for human verification.
- Bonus: Top-20 US = `SCHOOL_BONUS_US`, Top-50 Intl = `SCHOOL_BONUS_INTL` (defaults 15 / 12). "High School" and unmatched → 0, **never negative.**
- Effect: a school match **raises the applicant's score and therefore their rank.** Because ranking happens only among gate-survivors, this bonus can never resurrect a `REJECTED` application. The downstream cohort step (§11) is where a higher rank can convert into an acceptance — this is how the original "boost a borderline applicant into acceptance" intent is realized, without this system making the acceptance call.

### 7.2 Resume bonus (Stage 6) — DEFERRED, NOT YET PLANNED
- The resume is a PDF URL, so scoring it requires download + text extraction. **This has not been designed or built and is out of scope for the current version.**
- Current behavior: `resume_bonus = 0` for everyone. Absence of a resume is neutral (148 are blank anyway), and presence currently contributes nothing.
- Future work (unscheduled): download + parse (e.g. `pdfplumber`), then an LLM relevance score (projects, internships, languages, repos) → `RESUME_BONUS` 0–10, relevance-only, never negative. Flag this clearly in code as a TODO stub so it's obvious the slot exists but is inert.

---

## 8. LLM prompt contracts

General rules for all LLM tasks:
- **Return ONLY valid JSON. No markdown fences, no preamble.** Parse defensively (strip stray fences if present) and validate against the schema; on parse failure, retry once, then route the applicant to `NEEDS_REVIEW` with reason `"LLM_PARSE_FAILURE"` (never silently reject).
- Cheap/fast model for **A** and **C** (mechanical extraction). Stronger model for **D** (essay judgment) and **B** (adequacy judgment).
- Temperature ≤ 0.2 for repeatability. Pass `submission_id` through; cache by `(submission_id, sha256(input_text))`.

### 8.1 GPA points (deterministic, no LLM — listed here for completeness)
Linear gradient over `[3.0, 4.0]` → `[0, GPA_SCORE_MAX]` (default max 40), capped at 4.0:
```
gpa_points = clamp((normalized_gpa - 3.0) / (4.0 - 3.0), 0, 1) * GPA_SCORE_MAX
```
So 3.0 → 0, 3.2 → 8, 3.7 → 28, 4.0 → 40. This makes "a 3.2 less impactful than a 3.7" explicit. (Below 3.0 only reaches scoring via an approved Task B explanation, and lands near the bottom of the gradient — the deficit is reflected, never erased.)

### 8.2 LLM Task B — Low-GPA explanation evaluation
Fires only when normalized GPA < 3.0 **and** an explanation is present.

System prompt (essence):
> You evaluate whether a stated extenuating circumstance justifies keeping (and ranking) an applicant whose GPA is below the 3.0 (B average) bar for a selective software-engineering program. The further the GPA falls below 3.0, the stronger, more specific, and more realistic the circumstance must be. Vague, generic, or implausible reasons are not adequate. You are strict but fair. Output only JSON.

User template:
```
NORMALIZED_GPA: {normalized_gpa}
GAP_BELOW_THRESHOLD: {3.0 - normalized_gpa}
EXPLANATION: """{explanation_text}"""
```

Output schema:
```json
{
  "explanation_adequate": true,
  "strength_of_reason": 0.0,            // 0–1
  "realistic": true,
  "severity_vs_reason_balanced": true,  // does reason strength scale with the size of the GPA gap?
  "recommended_outcome": "rank",        // "rank" | "reject"
  "rationale": "1–2 sentence human-readable justification for the audit log"
}
```

### 8.3 LLM Task D — Essay grading (relevance gate + quality)
Run for each essay (or batch both, returning an array). Length/profanity/gibberish already passed deterministically; this stage adds **relevance (a gate)** and **quality (a score)**.

System prompt (essence):
> You grade an application essay for a selective high-school/undergraduate software-engineering program. First decide if the essay actually responds to the given prompt; an off-topic essay is disqualifying. Then score quality: clarity, specificity, coherence, and overall saliency (does it make a compelling, concrete case?). Penalize grammar and spelling only slightly — many applicants are non-native English speakers; penalize genuine errors, never accent-of-writing. Reward concrete detail and genuine motivation over generic filler. Output only JSON.

User template:
```
PROMPT: """{essay_prompt_text}"""
WORD_COUNT: {wc}
TARGET_RANGE: 100-350
ESSAY: """{essay_text}"""
```

Output schema (per essay):
```json
{
  "on_topic": true,                 // false → REJECTED for the whole application
  "relevance_confidence": 0.0,      // 0–1
  "quality_score": 0,               // 0–20 (specificity, coherence, saliency)
  "grammar_spelling_penalty": 0,    // 0–3, subtracted, "slight"
  "saliency_notes": "what made it strong/weak",
  "rationale": "1–2 sentences for audit"
}
```

Post-processing per essay:
```
if not on_topic: → REJECTED (reason: "Essay N off-topic")
length_penalty = soft penalty from Stage 1 (0 if within 100–350; up to LEN_PENALTY_MAX near hard bounds)
essay_score = max(0, quality_score - grammar_spelling_penalty - length_penalty)
```
**Total essay score = essay1_score + essay2_score** (default max 40 = 20 + 20 before penalties).

### 8.4 LLM Task C — Coursework decomposition + relevance
Fires when `Relevant Coursework` is non-empty.

System prompt (essence):
> You extract individual courses and grades from a free-text list and classify each by relevance to software engineering. Categories and weights: CS/programming = highest, Math = high, Data/analytics/ML/databases = moderate, everything else = ignored (weight 0). Normalize each grade to a 0–100 percentage (A=95, A-=92, B+=88, B=85, etc.; convert any stated scale). A course graded below 80% counts for nothing. Output only JSON. Decompose faithfully so a human reviewer can see each course in a UI.

User template:
```
COURSEWORK_RAW: """{coursework_cell}"""
```

Output schema:
```json
{
  "courses": [
    {
      "name": "AP Computer Science A",
      "grade_raw": "A",
      "grade_pct": 95,
      "category": "cs",            // "cs" | "math" | "data" | "other"
      "counts": true,              // false if grade_pct < 80 or category == "other"
      "category_weight": 1.0       // cs=1.0, math=0.8, data=0.6, other=0.0 (tunable)
    }
  ],
  "rationale": "short note"
}
```

Coursework bonus (deterministic, from Task C output; flat per-course — grades exclude, never scale):
```
counts     = category != "other" and (grade_pct is null or grade_pct >= 85)
per_course = category_weight * COURSE_UNIT                        # only if counts == true
coursework_bonus = min(COURSEWORK_BONUS_MAX, sum(per_course))     # default cap 15
```
Empty coursework → 0, no penalty. The `courses[]` array is stored verbatim in the audit record for the future UI.

---

## 9. Audit record schema (one per applicant)

Persist as JSON (one record per applicant, JSONL). Source of truth for audits and the downstream cohort tool.

```json
{
  "submission_id": "d13aea0f-...",
  "name": "Gayatri Veeravarapu",
  "email": "...",
  "program_choices": {"first": "Summer 2026- HONORS", "second": "...", "third": "..."},
  "dedup": {"is_duplicate_email": false, "is_duplicate_name": false, "kept": true, "notes": ""},

  "outcome": "RANKED",               // REJECTED | RANKED | NEEDS_REVIEW
  "final_score": 84.4,                // null if REJECTED or NEEDS_REVIEW
  "rank": 12,                          // integer rank among RANKED; null otherwise
  "decided_at_stage": "stage8",
  "primary_reason": "Survived all gates; GPA 4.0, both essays on-topic/high quality, relevant CS+math coursework",

  "gates": {
    "essay_length": {"e1_wc": 230, "e2_wc": 210, "e1_ok": true, "e2_ok": true, "hard_fail": false},
    "profanity": {"hit": false},
    "gibberish": {"hit": false},
    "gpa_gate": {"passed": true, "reason": ""},
    "essay_relevance": {"e1_on_topic": true, "e2_on_topic": true}
  },

  "gpa": {
    "raw": "4.27",
    "normalized_gpa": 4.0,
    "original_scale": "weighted_gt_4",
    "conversion_method": "llm_task_a",
    "confidence": "med",
    "below_threshold": false,
    "explanation_eval": null            // populated only if Task B ran
  },

  "scores": {
    "gpa_points": 40.0,
    "essay": {"e1": 18, "e2": 17, "total": 35},
    "coursework_bonus": 9.4,
    "school_bonus": 0,
    "resume_bonus": 0                   // always 0 in current scope (deferred)
  },

  "coursework_breakdown": [ { "name": "...", "grade_pct": 95, "category": "cs", "counts": true } ],
  "school_match": {"matched_name": null, "list": null, "fuzzy_score": 0},

  "reasons": [
    "PASS gpa_gate: normalized 4.0 >= 3.0",
    "essay1 on-topic, quality 18",
    "coursework: 3 counting courses (2 cs, 1 math)"
  ],
  "llm_calls": ["task_a", "task_d_e1", "task_d_e2", "task_c"],
  "errors": []
}
```

---

## 10. Stage 8 — Aggregation and ranking, + CONFIG

### 10.1 Score composition (only for apps surviving all hard gates)

```
final_score =
      gpa_points            # 0–40  (required signal, gradient 3.0→4.0)
    + essay_total           # 0–40  (required signal)
    + coursework_bonus      # 0–15  (bonus)
    + school_bonus          # 0–15  (bonus)
    + resume_bonus          # 0      (deferred — inert in current scope)
# theoretical max in current scope ≈ 110 (120 once resume is built)
```

Structure rationale: required signals (GPA + essays) carry the ranking up to ~80 points; bonuses differentiate applicants with otherwise-similar cores and lift strong-bonus candidates up the order. Bonuses are purely additive — they can neither manufacture a rejection nor rescue one (rejections are gated before scoring).

### 10.2 Outcome assignment

```
REJECTED      : assigned by a hard gate (Stages 1, 3, 4). Never scored, never ranked.
NEEDS_REVIEW  : gate-survivor that cannot be fairly scored yet —
                unresolvable GPA scale, unchecked truthfulness affirmation, or LLM_PARSE_FAILURE.
                A human resolves the blocker, after which the applicant is scored and folded into the ranking.
RANKED        : survived all gates and is fully scoreable. Receives final_score and an integer rank.
```

**Ranking:** sort all `RANKED` applicants by `final_score` descending; assign `rank` 1..N. Define a deterministic tiebreaker for equal scores (suggested order: higher GPA points → higher essay total → lower submission timestamp) so reruns are stable. **There is no acceptance cutoff here** — the full ranked list is the deliverable.

### 10.3 CONFIG block (centralize all magic numbers)

```yaml
# Essay length
target_min: 100
target_max: 350
hard_min: 60
hard_max: 500
len_penalty_max: 5

# GPA
gpa_threshold: 3.0          # B average; deny line and bottom of the gradient
gpa_hard_floor: 2.0         # below this no explanation can rescue -> REJECTED
gpa_score_max: 40

# Essay scoring
essay_quality_max_each: 20  # total 40
grammar_penalty_max: 3

# Coursework (bonus)
coursework_bonus_max: 15
course_weight_cs: 1.0
course_weight_math: 0.8
course_weight_data: 0.6
course_weight_other: 0.0
course_min_grade_pct: 85    # B; an explicit grade below this excludes the course entirely
course_unit: 3.0

# School (bonus)
school_bonus_us_top20: 15
school_bonus_intl_top50: 12
fuzzy_match_threshold: 88

# Resume (bonus) — DEFERRED, currently inert
resume_bonus_max: 0         # set >0 only once PDF parsing is built
```

No `accept_threshold` — acceptance is out of scope (§11).

---

## 11. Downstream: cohort sizing & acceptance (separate system, deferred)

This filter **emits a ranked list and a rejection list; it does not accept anyone.** Acceptance is a later step that consumes this output:
- The program tiers (`HONORS` / `INTENSIVE` / `REGULAR`) live in the `First/Second/Third Choice` columns and are already carried into each audit record.
- When run, that step walks the `RANKED` list top-down and fills each program to its chosen capacity, honoring choice order — this is where "automatic acceptance of the strongest applicants" actually happens, by taking the top of the ranking against cohort size.
- Invariant the downstream tool must preserve: changing cohort sizes only moves the accept/waitlist boundary along the ranking; it can never resurface a `REJECTED` applicant, since rejected applicants were never scored or ranked.
- `NEEDS_REVIEW` applicants must be resolved by a human and merged into the ranking before cohort filling runs.

---

## 12. Implementation notes for Claude Code

- **Stack:** Python. `pandas` for ingest, `rapidfuzz` for school matching, Anthropic SDK for LLM tasks, `pydantic` to validate every LLM JSON payload against the schemas above. (`pdfplumber` only if/when resume parsing is built — not now.)
- **Idempotency / cost control:** cache every LLM result by `(submission_id, sha256(input))`; reruns must not re-bill. Expected LLM volume ≈ (466 − Stage-1 rejects − Stage-3 rejects) × {Task D ×2, Task C, sometimes A/B} — fail-fast meaningfully shrinks this.
- **Concurrency:** bounded async/thread pool with retry + backoff; respect rate limits.
- **Determinism:** temperature ≤ 0.2; pin model versions in CONFIG; deterministic tiebreaker for ranking.
- **Outputs:**
  1. `decisions.jsonl` — one audit record per applicant (§9).
  2. `ranked.csv` — `RANKED` applicants only, sorted by rank: rank, id, name, final_score, gpa_points, essay_total, coursework_bonus, school_bonus, primary_reason.
  3. `rejected.csv` — `REJECTED` applicants: id, name, the gate that failed, primary_reason.
  4. `needs_review.csv` — `NEEDS_REVIEW` applicants: id, name, blocker reason.
  5. `summary.json` — counts per outcome, score histogram of `RANKED`, list of `NEEDS_REVIEW` cases and why.
- **Invariant tests to write:**
  - No optional-signal absence ever reduces `final_score`.
  - No bonus changes a `REJECTED` outcome.
  - Every `REJECTED` record names the failing gate in `primary_reason`.
  - Normalized GPA below 3.0 never produces points without an approved Task B explanation, and never scores above the bottom of the gradient band.
  - Ranking is stable across reruns (tiebreaker deterministic; cache hits identical).

---

## 13. Open dependencies / things you must supply

1. **`schools.json`** — curated Top-20 US and Top-50 International lists, frozen per cycle, with a cited ranking source.
2. **Profanity wordlist** with medical/anatomical exceptions.
3. **Resume parsing is explicitly unplanned.** Leave the Stage 6 slot as an inert, clearly-labeled TODO stub (`resume_bonus = 0`) until it's designed.
4. **GPA threshold is settled at 3.0** (§1) — no decision needed; flagged here only so it's not silently re-litigated.
