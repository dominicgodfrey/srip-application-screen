# SRIP ATS — Scoring Model (v3)

One page for any developer to understand how an application is scored. `config.yaml` is the
machine-readable source of truth; this file mirrors it. Owner-approved 2026-07-04.

## The two-layer rule (unchanged from v2)

1. **Hard gates decide rejections.** Rejection is deterministic/rule-based and binary.
   No score threshold accepts or rejects anyone.
2. **The score only ranks gate-survivors.** Bonuses can never manufacture or rescue a
   rejection, and the absence of any optional signal is neutral — it never subtracts.

Outcomes: `REJECTED` (failed a hard gate) · `RANKED` (scored, ranked per cohort) ·
`NEEDS_REVIEW` (unscoreable — human resolves; never auto-rejected).

## Score composition — max 150

| Component | Points | Kind | How |
|---|---|---|---|
| GPA | 0–40 | required | Linear gradient over normalized GPA 3.3 → 4.0 (3.3 ⇒ 0, 4.0 ⇒ 40). Below 3.3 only reachable via an approved Task B explanation, and lands at the gradient bottom. |
| Essay 1 (motivation) | 0–15 | required | Task D quality (0–15) − slight grammar penalty − length penalty. Off-topic/gibberish ⇒ whole application REJECTED. |
| Essay 2 (trajectory) | 0–15 | required | Same as Essay 1. |
| Essay 3 (technical, optional) | 0–20 | bonus | Task F: relevance to its prompt, technical depth/difficulty, real-world impact. Surface-level interest scores low; interest → side project → real impact scores high. Absent ⇒ 0 (neutral). Gibberish/off-topic/over-max ⇒ 0 bonus, never a rejection. |
| Relevant coursework | 0–15 | bonus | Task C decomposition; CS > Math > Data, others ignored; flat weight × unit per counting course; explicit grade < 80% excludes a course. |
| School | 0–20 | bonus | Fuzzy match vs curated lists: Top-20 US = 20, Top-50 Intl = 16. "High School"/unmatched ⇒ 0. |
| Resume | 0–25 | bonus | **In-house rubric** (owner, 2026-07-27 — a third-party hiring agent was rejected): Task E extracts signals, `config.yaml` prices them, same shape as Tasks C/F. Ships disabled (`resume.bonus_max: 0`) until calibrated post-pilot. Any failure ⇒ 0 + audit note, never a block. |

**Required core = 70** (GPA 40 + essays 30). **Bonuses = up to 80.** Theoretical max **150**
(125 while resume is disabled).

## Hard gates (in fail-fast order, zero LLM spend after the first hit)

Most gates reject; two do not, and both exceptions exist because the check cannot be trusted
to be *right* on its own — see each entry. A gate that fires always stops the application.

1. ~~**Profanity in ANY essay**~~ — **⇒ NEEDS_REVIEW, not REJECTED** (owner, 2026-07-29).
   Still evaluated across every essay including the optional Essay 3, and still fail-fast
   (zero LLM spend on a flagged row), but the outcome is a human review. Rationale: the gate
   is `better-profanity`'s word list, which cannot distinguish a slur from ordinary
   vocabulary in context — a curated 1101-term list was tried and reverted for exactly that
   reason. A reviewer promotes a false positive (the row then scores normally, flagged as a
   manual override) or demotes a real violation to REJECTED.
2. **Gibberish in a required essay** (deterministic heuristics, ≥2 signals; ESL-safe) —
   Task D backstops this. Gibberish in Essay 3 ⇒ 0 bonus only.
3. ~~**Word bounds**~~ — **the length gate was deleted** (owner, 2026-07-28). The site
   server-validates essay length at submit and 400s any violation, and it does not send us
   its bounds, so a config-sourced check could only ever fire on a stale local config —
   i.e. produce false positives on good-faith applicants. No word-count rule rejects anyone.
   Essay 3 length is likewise not gated; an over-long optional essay still just scores on
   its merits.
4. **GPA gate:** normalized GPA < 2.0 ⇒ REJECTED regardless of explanation (hard floor).
   GPA < 3.3 with no explanation ⇒ REJECTED; with an explanation ⇒ Task B judges
   (severity-scaled bar) — `rank` or `reject`. Blank GPA + blank explanation ⇒ REJECTED
   (non-answer). Unresolvable scale ⇒ NEEDS_REVIEW, never rejected.
5. **Required essay off-topic** (Task D relevance gate).

## Ranking

`RANKED` applicants are sorted by `final_score` descending **within their cohort**
(`cohort_name`), deterministic tiebreaker: gpa_points → essay total → submission_id.
Rank is computed at read time — it is always live as new applications arrive.

## Invariants (every one has a test)

- No optional-signal absence (essay 3, coursework, school, resume) ever reduces `final_score`.
- No bonus changes a `REJECTED` outcome.
- Every `REJECTED` record names the failing gate in `primary_reason`.
- GPA < 3.3 never yields points without an approved Task B explanation, never above the
  gradient bottom.
- Ranking is stable across reruns; re-delivery of identical content changes nothing and
  re-bills nothing.
- Nothing unscoreable is ever rejected — it goes to `NEEDS_REVIEW`.
