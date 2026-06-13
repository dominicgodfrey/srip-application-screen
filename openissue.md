# Open Issues — Owner Inputs Still Needed

Things only the owner (Dominic) can provide. Claude Code references this file; update the
status lines as items land. **Do not put real secrets or applicant PII in this file** — it is
committed to the repo. See `CLAUDE.md` → Privacy & Security.

---

## Blocking — LLM stages can't run without these

### 1. OpenAI API key  ·  STATUS: NOT PROVIDED
- **What:** `OPENAI_API_KEY`.
- **Where:** project-root `.env` (gitignored), one line: `OPENAI_API_KEY=sk-...`
- **Why:** every LLM task (A GPA-normalize, B low-GPA adequacy, C coursework, D essay grading)
  needs it. Without it, only the deterministic gates run.
- **Never** hard-code it, commit it, or write it into any output/log.

### 2. OpenAI data-retention setting  ·  STATUS: RESOLVED (owner confirmed, 2026-06-12)
- **What:** set the OpenAI account/project to **zero / minimal data retention**.
- **Resolved:** the owner confirmed the account is already configured for minimal retention.
  No further action; re-verify only if the OpenAI account/project changes.

---

## Blocking — specific stage, has a working stopgap

### 3. Curated profanity / slur list  ·  STATUS: ALLOWLIST CURATED; BLOCK LIST STILL AWAITED
- **Current:** `better-profanity`'s default built-in list **plus a curated ALLOW list** in
  `resources/profanity.txt` (loaded live). The allowlist was populated 2026-06-11 from the
  false positives the default list produced on the reference dataset — 7 good-faith
  applicants were being rejected over clinical/innocuous words (`stroke`, `organ`, `oral`,
  `facial`, `thrust`, `sex-based`, …). A scan after the fix shows 0 profanity flags on the
  reference CSV.
- **Still needed from owner:** the **BLOCK side** — curated slurs and profane exclamations
  the default list may miss. The file format is documented in `resources/profanity.txt`.
- **Needed from owner:** populate `resources/profanity.txt` with —
  - **slurs to block** (the primary concern),
  - **profane exclamations**,
  - a **medical / anatomical ALLOWLIST** — clinical/anatomical terms must NOT trip the gate
    (PRD §4.2; e.g. legitimate medical vocabulary in an extenuating-circumstances explanation).
- **Why it matters:** the default list may miss the specific slurs you want gated and may
  false-positive on clinical terms, which would wrongly reject good-faith applicants.
- **Action:** fill in the placeholder (a plain newline-separated file is fine — a LDNOOBW-style
  base is easy to retrofit); Phase 2.2 will load it and subtract the allowlist.

---

## Non-blocking — housekeeping / defensibility

### 4. Reference CSV for integration testing  ·  STATUS: NOT SUPPLIED
- **What:** the real Fillout export (or a representative **synthetic** copy) to validate the
  §2 data-contract parser end-to-end.
- **Handling:** the real CSV is PII — keep it only in gitignored `data/`, never commit it.
  Automated tests use synthetic fixtures only.

---

## Blocking for Phase 12 (resume parsing)

### 5. Resume URL host allowlist  ·  STATUS: RESOLVED
- **Resolved (Phase 12):** the owner supplied sample resume URLs from the real export; all
  point at one S3 bucket host, now pinned in `config.yaml` →
  `resume.allowed_url_hosts: [prod-fillout-oregon-s3.s3.us-west-2.amazonaws.com]`.
  A live smoke test against five real URLs confirmed public fetchability and extraction.
- **Note from the live sample:** some applicants upload **images** (e.g. a `.png`) in the
  resume slot. These download fine but fail extraction with the typed `not_a_pdf` reason →
  0 bonus + an audit note, never a block. OCR is deliberately out of scope.
- **If Fillout ever changes buckets:** add the new hostname to `resume.allowed_url_hosts`
  (exact host match, https only). The original rationale stands: the allowlist is the SSRF
  guard for URLs arriving in an uploaded CSV.

---

## Open — needs an owner decision

### 6. GPA normalization routes too many applicants to NEEDS_REVIEW  ·  STATUS: SETTLED (owner decision, 2026-06-12)
- **Decision:** current behavior is acceptable. The goal was to reduce reviewer workload, and
  reading through a small number of NEEDS_REVIEW applications is fine. No mitigation will be
  built; promote-from-audit (and now demote) remains the human-resolution workflow.
- Original analysis kept below for reference.
- **Observation (owner, 2026-06-11):** "GPA scale normalization is removing too many
  candidates." (They are not removed — `NEEDS_REVIEW` is never a rejection — but they drop
  out of the ranked list until a human resolves them, which reads as removal.)
- **Measured on the reference CSV (466 rows, deterministic pass only):**
  243 resolved deterministically · 180 routed to LLM Task A (mostly weighted `>4.0` values
  like `4.27`, `4.42`, `weighted: 4.4`; Task A resolves many but returns
  `requires_manual_review` for the genuinely unplaceable) · 43 blank → straight to
  `NEEDS_REVIEW` (no token spent).
- **Why it is conservative by design:** PRD §6.1 — "Do not reject for a missing/unscalable
  scale" and never guess a GPA that gates someone. The blank-GPA cohort (43 = 9.2%) is the
  floor; no normalization change can fix a blank cell.
- **Interim path (shipped):** the audit browser now shows the raw GPA for every candidate
  and a human can **promote** any `NEEDS_REVIEW`/`REJECTED` applicant — the system re-runs
  all scoring on them (unscoreable GPA contributes 0 points) and folds them into the ranking
  as an audited manual override.
- **Candidate mitigations (owner to pick):**
  1. Extend the deterministic parser for the common weighted patterns (`4.0 < x ≤ 5.0`
     unweighted-cap heuristic) instead of routing them to Task A.
  2. Loosen Task A acceptance (treat `confidence: med` + a stated scale as placeable).
  3. Decide a policy for blank GPAs (currently NEEDS_REVIEW; alternatives: score GPA as 0
     points and rank on essays alone, or keep manual review).
- **Action:** owner picks a mitigation; until then promote-from-audit is the workflow.

---

## Settled — no action needed (listed so they aren't re-litigated)
- GPA threshold = **3.3** (PRD §1; owner raised it from 3.0 on 2026-06-12).
- LLM provider = **OpenAI**, cloud for all tasks.
- Resume parsing = **in scope as Phase 12** (owner decision, supersedes the earlier deferral;
  see PLAN.md Phase Map + Notes log). `bonus_max = 10` per PRD §10.1; extraction via `pypdf`;
  Stage 6 stays the inert stub until Phase 12.5 lands, and `resume.bonus_max: 0` remains the
  kill switch thereafter.
- School ranking source = **U.S. News & World Report** (Best National / Best Global), frozen for Summer 2026.
