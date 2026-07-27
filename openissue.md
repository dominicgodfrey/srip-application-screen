# Open Issues — Owner Inputs Still Needed

Things only the owner (Dominic) can provide. Claude Code references this file; update the
status lines as items land. **Do not put real secrets or applicant PII in this file** — it is
committed to the repo. See `CLAUDE.md` → Security.

> **Scope of this file (v3.1):** long-lived owner-supplied *inputs* — API keys, curated
> word lists, account settings. **Design and phase decisions live in `PLAN.md`**
> ("Load-bearing decisions still open", incl. D1–D3), and everything needing the website
> team lives in `WEBSITE_ASKS.md`. Kept separate so the three don't drift.
>
> v2-only items (Fillout CSV reference export, the Fillout S3 resume-host allowlist) were
> removed 2026-07-27 — the CSV intake is retired and the resume host is now tracked as
> `WEBSITE_ASKS.md` ask #4 (Cloudflare R2, deferred while the resume stage is off).

---

## Blocking — LLM stages can't run without these

### 1. OpenAI API key  ·  STATUS: NOT PROVIDED
- **What:** `OPENAI_API_KEY`.
- **Where:** project-root `.env` (gitignored), one line: `OPENAI_API_KEY=sk-...`
- **Why:** every LLM task (A GPA-normalize, B low-GPA adequacy, C coursework, D essay
  grading, F technical-essay bonus) needs it. Without it, only the deterministic gates run.
- **v3.1 note:** once the service is deployed to the partner's Vercel project, this key is
  also set as an env var *there* — meaning their team can read it. See the secrets-governance
  note in PLAN.md's 2026-07-27 hosting entry; a separate key for that deployment is the
  mitigation if that matters.
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
  `facial`, `thrust`, `sex-based`, …). A scan after the fix showed 0 profanity flags.
- **Still needed from owner:** the **BLOCK side** — curated slurs and profane exclamations
  the default list may miss. The file format is documented in `resources/profanity.txt`.
  - **slurs to block** (the primary concern),
  - **profane exclamations**,
  - a **medical / anatomical ALLOWLIST** — clinical/anatomical terms must NOT trip the gate
    (e.g. legitimate medical vocabulary in an extenuating-circumstances explanation).
- **Why it matters:** the default list may miss the specific slurs you want gated and may
  false-positive on clinical terms, which would wrongly reject good-faith applicants.
- **Stakes are higher in v3:** profanity in **any** essay — including the optional technical
  essay — is a hard rejection, and applications now arrive continuously rather than in a
  batch a human reviews before release.

---

## Settled — no action needed (listed so they aren't re-litigated)

- GPA threshold = **3.3**; hard floor **2.0** (owner raised the threshold from 3.0 on
  2026-06-12).
- **GPA normalization routing to NEEDS_REVIEW is acceptable** (owner decision, 2026-06-12).
  The concern was that scale-normalization "removes" candidates; it does not — `NEEDS_REVIEW`
  is never a rejection, and promote-from-audit is the human-resolution workflow. No
  mitigation will be built. *(Measured on the v2 reference CSV, 466 rows: 243 resolved
  deterministically, 180 routed to Task A, 43 blank → NEEDS_REVIEW. Historical figures —
  v3 receives structured `gpa_unweighted`/`gpa_weighted`, so the Task A share should fall.)*
- LLM provider = **OpenAI**, cloud for all tasks.
- School ranking source = **U.S. News & World Report** (Best National / Best Global), frozen
  for Summer 2026.
- Resume parsing ships **disabled** (`resume.bonus_max: 0` — the kill switch). Engine
  **decided in-house** (owner, 2026-07-27, `WEBSITE_ASKS.md` #11 — hiring-agent rejected);
  the v3 value is 25 points once enabled, and enablement is post-pilot.
  Extraction is via `pypdf`; images uploaded into the resume slot fail extraction with a
  typed `not_a_pdf` reason ⇒ 0 bonus + audit note, never a block. OCR is out of scope.
