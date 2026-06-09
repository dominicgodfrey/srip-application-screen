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

### 2. OpenAI data-retention setting  ·  STATUS: NOT CONFIRMED
- **What:** set the OpenAI account/project to **zero / minimal data retention**.
- **Why:** essays and GPAs are minors' PII. Default API retention is ~30 days. (API inputs are
  not used for training by default, but retention should still be turned down.)
- **Action:** confirm in the OpenAI dashboard, then mark this done.

---

## Blocking — specific stage, has a working stopgap

### 3. Curated profanity / slur list  ·  STATUS: PLACEHOLDER FILE AWAITING CONTENT
- **Current:** using `better-profanity`'s **default built-in list** (owner approved "use the
  current list for now"). The Stage 1 profanity gate works today with it.
- **Scaffold in place:** `resources/profanity.txt` now exists as an inert, documented
  placeholder (committed). It is **not loaded yet** and contains no curated terms — it defines
  the format (`BLOCK` terms one per line; `ALLOW:`-prefixed medical/anatomical exemptions) and
  is ready to be filled.
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

## Settled — no action needed (listed so they aren't re-litigated)
- GPA threshold = **3.0** (PRD §1).
- LLM provider = **OpenAI**, cloud for all tasks.
- Resume parsing = **deferred**; Stage 6 stays an inert `resume_bonus = 0` stub.
- School ranking source = **U.S. News & World Report** (Best National / Best Global), frozen for Summer 2026.
