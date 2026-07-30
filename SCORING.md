# SRIP ATS — Scoring Model

One page for any developer to understand how an application is scored. `config.yaml` is the
machine-readable source of truth; this file mirrors it. Owner-owned: weights, the 3.3 threshold,
and gate semantics change only by owner decision.

## The two-layer rule

1. **Hard gates decide rejections.** Rejection is deterministic, rule-based, and binary.
   No score threshold accepts or rejects anyone.
2. **The score only ranks gate-survivors.** Bonuses can never manufacture or rescue a
   rejection, and the absence of any optional signal is neutral — it never subtracts.

Outcomes: `REJECTED` (failed a hard gate) · `RANKED` (scored, ranked per cohort) ·
`NEEDS_REVIEW` (unscoreable — a human resolves it; never auto-rejected).

## Score composition — max 150

| Component | Points | Kind | How |
|---|---|---|---|
| GPA | 0–40 | required | Linear gradient over normalized GPA 3.3 → 4.0 (3.3 ⇒ 0, 4.0 ⇒ 40). Below 3.3 is reachable only via an approved Task B explanation, and lands at the gradient bottom. |
| Essay 1 (motivation) | 0–15 | required | Task D quality (0–15) − slight grammar penalty. Off-topic or gibberish ⇒ whole application REJECTED. |
| Essay 2 (trajectory) | 0–15 | required | Same as Essay 1. |
| Essay 3 (technical, optional) | 0–20 | bonus | Task F: relevance to its prompt, technical depth/difficulty, real-world impact. Surface-level interest scores low; interest → side project → real impact scores high. Absent ⇒ 0 (neutral). Gibberish or off-topic ⇒ 0 bonus, never a rejection. |
| Relevant coursework | 0–15 | bonus | Task C decomposition; CS > Math > Data, others ignored; flat weight × unit per counting course; a course explicitly graded below 80% is excluded. |
| School | 0–20 | bonus | Fuzzy match vs curated lists: US Top-20 = 20, Intl Top-50 = 16. "High School" or unmatched ⇒ 0. |
| Resume | 0–25 | bonus | Task E extracts signals, `config.yaml` prices them — same shape as Tasks C and F. Weights are priced for the 0–25 band: a typical resume earns ~half, a standout saturates the cap. **Disabled** (`resume.bonus_max: 0`) until the R2 host is pinned. Any failure ⇒ 0 + audit note, never a block. |

**Required core = 70** (GPA 40 + essays 30). **Bonuses = up to 80.** Theoretical max **150**
(125 while the resume stage is disabled).

## Hard gates (fail-fast order, zero LLM spend after the first hit)

A gate that fires always stops the application. Most reject; one deliberately does not.

1. **Profanity in ANY essay ⇒ NEEDS_REVIEW, not REJECTED.** Evaluated across every essay
   including the optional Essay 3, and still fail-fast (zero LLM spend on a flagged row), but the
   outcome is a human review. The gate is `better-profanity`'s word list, which cannot
   distinguish a slur from ordinary vocabulary in context, so a reviewer confirms every flag —
   promoting a false positive (the row then scores normally, marked as a manual override) or
   demoting a real violation to REJECTED. The curated BLOCK list is deliberately empty; the
   ALLOW list is curated.
2. **Gibberish in a required essay ⇒ REJECTED.** Deterministic heuristics, ≥2 signals required
   together, ESL-safe; Task D backstops it. Gibberish in Essay 3 ⇒ 0 bonus only.
3. **GPA gate.** Normalized GPA < 2.0 ⇒ REJECTED regardless of explanation (hard floor).
   GPA < 3.3 with no explanation ⇒ REJECTED; with an explanation ⇒ Task B judges on a
   severity-scaled bar (`rank` or `reject`). Blank GPA + blank explanation ⇒ REJECTED (a
   non-answer). An unresolvable scale ⇒ NEEDS_REVIEW, never rejected.
4. **Required essay off-topic** (Task D relevance gate) ⇒ REJECTED.

**No word-count rule rejects anyone.** The website server-validates essay length at submit and
sends no bounds, so a length check here could only fire on a stale local config. Word counts are
audit data only, and an over-long optional essay still scores on its merits.

## Ranking

`RANKED` applicants sort by `final_score` descending **within their cohort** (`cohort_name`),
with a deterministic tiebreaker: gpa_points → essay total → submission_id. Rank is computed at
read time, so it is always live as new applications arrive.

## Invariants (every one has a test)

- No optional-signal absence (Essay 3, coursework, school, resume) ever reduces `final_score`.
- No bonus changes a `REJECTED` outcome.
- Every `REJECTED` record names the failing gate in `primary_reason`.
- GPA below 3.3 never yields points without an approved Task B explanation, and never above the
  gradient bottom.
- Ranking is stable across reruns; re-delivery of identical content changes nothing and re-bills
  nothing.
- Nothing unscoreable is ever rejected — it goes to `NEEDS_REVIEW`.
