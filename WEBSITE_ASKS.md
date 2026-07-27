# ATS ↔ thinkNeuroWebsite — asks & open discussions

Maintained by the ATS side; owner sends/discusses with the website team. Items 1–7 are
concrete change requests (all small); 8–14 are decisions to make together — tradeoffs
included so they can be settled in one conversation. Status column tracks answers.

## Asks (changes in the website repo)

| # | Ask | Status |
|---|---|---|
| 1 | **Sign all ATS POSTs** (`sendWebhook` in `app/api/apply/admin/ats/run/route.ts` **and** the Test button route `ats/test/route.ts`): add headers `X-ATS-Timestamp` (unix seconds) and `X-ATS-Signature = hex(HMAC_SHA256(secret, timestamp + "." + rawBody))`; secret from a Vercel env var (`ATS_WEBHOOK_SECRET`). ~10 lines; we provide the spec + test vectors. Unsigned requests will get 401. | **CLOSED — we adopt theirs** (owner, 2026-07-26): accept static `X-ATS-Secret`; HMAC deferred as a pre-production hardening option. No website change needed |
| 2 | **Extend the CS essays-mode payload** with fields already in `form_data`: `gpa_explanation`, `relevant_coursework`, `programming_languages`, `institution`, `state_of_residence`, the three ranked program-choice fields, `github_profile`, `sub_track`. Without `gpa_explanation` in particular, applicants below 3.3 who DID write an explanation would be auto-rejected as "no explanation" — the one field we cannot function without. | **RESOLVED** — all fields ship in `all_answers` (repo audit 2026-07-26) |
| 3 | **Structured GPA:** send `gpa: { unweighted: "3.8 / 4.0", weighted: "4.4 / 5.0" \| null }` instead of `[...gpaFields].join(" / ")` — the join is ambiguous when both GPAs are filled ("3.8 / 4.0 / 4.4 / 5.0"). | answered — split into `gpa_unweighted`/`gpa_weighted` (see 2026-07-21) |
| 4 | **Tell us the `R2_PUBLIC_URL` host** so our resume-download allowlist can pin it (we only ever fetch PDFs from that exact host, https-only). | narrowed — `<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`; need the account id / one sample URL. Deferred (resume off) |
| 5 | **Per-essay metadata:** include `field_key`, `min_words`, `max_words` on each entry of `required_essays[]` / `optional_essays[]` (available from the `questions` table at dispatch time). Lets us enforce exact word bounds without hardcoding form copy. | **CLOSED — not asking** (owner, 2026-07-26): bounds go in our `config.yaml` manually (form copy is stable). Deferred as a future ask if the form churns |
| 6 | **Share the live CS question config** (field keys + ats_role tags). The repo seeds (`lib/questions-default.ts`) differ from the live form (e.g. seeds show one `cohort_preference` + `consider_regular`; the live form has three ranked choice dropdowns with regular/intensive/honors). We pin the contract against the live config. | **RESOLVED** — `ats_role` tags + `FALLBACK_ATS_ROLE` + field keys read from repo (2026-07-26) |
| 7 | **Agree on response semantics:** our 202 = accepted-for-grading (not yet graded — `ats_logs.success` means *delivered*); any 4xx = payload permanently rejected (don't blind-retry the same content); re-submissions (same `submission_id`, changed content) are re-graded automatically. | partial — any 2xx = success, body unparsed; **they retry any non-2xx 3×** (contradicts the no-blind-retry ask) |

## Discussions (decide together — tradeoffs)

| # | Question | Tradeoffs | Status |
|---|---|---|---|
| 8 | **Trigger model** — is admin-triggered batch ("Run ATS on All Applications") the long-term plan, or is auto-dispatch (on-submit / cron) intended? *Not a change request — just clarifying the goal; the ATS works either way.* | *Admin-triggered:* human controls when LLM spend happens; zero risk to the applicant-facing path; but grading lags until someone presses Run. *Auto:* results always fresh; costs website-side code and spends tokens unattended. | **RESOLVED (repo audit 2026-07-26)** — both, and continuous: QStash auto-dispatch on submit (15 s debounce) + sequential admin runs. Always **one POST per applicant**; no batch body |
| 9 | **Results flow-back** — `admin_status` / `accepted_cohort_label` on `applications_submitted` suggest decisions may be meant to live in your admin panel. Should the ATS push results back, or is an export handoff enough? | *Flow-back:* one pane of glass; your email/payment flows can key off decisions; but needs a new authenticated API + UI work in your repo, and live rankings are mutable (rank shifts as applications arrive) — only frozen final decisions sync cleanly. *Export handoff (our default until answered):* zero work in your repo; screening happens in the ATS UI; staff use two tools during the cycle. | open |
| 10 | **Cohort allocation ownership** — who runs capacity/assignment simulation? | *ATS (default):* a tested what-if tool already exists where the scores live; output carried into your acceptance flow by export. *Website:* single acceptance→payment system; but you'd rebuild capacity simulation from scratch. | open |
| 11 | **Resume engine (25 pts of the score)** — HackerRank `interviewstreet/hiring-agent` vs an in-house evaluator mimicking its rubric (open-source contributions, relevant experience, technical depth)? | *hiring-agent:* battle-tested, purpose-built; but a third-party agent framework in a minors'-PII path (its own LLM wiring/deps to secure + maintain), scores are a black box vs our every-subscore-explainable audit records, its scale must be mapped to 0–25 by guesswork, and it bypasses our fetch-and-discard guardrails unless wrapped. *In-house mimic:* fully auditable, reuses our tested security guardrails, rubric tunable in config; rubric quality is on us — we'd calibrate by running hiring-agent offline on sample resumes and comparing. Either way the ATS ships with the resume stage disabled until decided. | **RESOLVED — in-house** (owner, 2026-07-27). No website action. Rationale: hiring-agent is calibrated for professional hiring, not high-schoolers; it is a black box against our every-subscore-explainable audit records; it puts a third-party agent framework in a minors'-PII path (contra CLAUDE.md's no-agent-framework rule); and it bypasses our fetch-→extract-→score-→discard guardrails unless wrapped. In-house reuses the tested guardrails and prices signals in `config.yaml` like Tasks C/F. Cost: rubric calibration is on us. Unchanged: the stage still ships disabled (`resume.bonus_max: 0`) and enabling it is post-pilot — this decision fixes *which* engine, not *when*. |
| 12 | **Hosting** — ~~do you have a home for a small always-on Python service?~~ | Superseded — see the resolution note. | **RESOLVED (owner, 2026-07-27): deploy into their Vercel project.** Andrew offered; we accept. The "no serverless" constraint was premised on a 15 s webhook timeout — their dispatcher actually allows 60 s, and Vercel Pro now runs FastAPI on Fluid compute (800 s maxDuration, per-minute cron). Grading moves from an always-on loop to a per-minute cron drain over the same Postgres queue. **Still need from them: confirm Pro plan + who holds the env vars.** |
| 13 | **Data retention** for the ATS store (minors' PII: names, emails, GPAs, explanations, essays; resume files/text are never stored under any option). | *Keep indefinitely:* cross-cycle analytics; but open-ended retention of minors' PII is a liability. *End-of-cycle download + purge (our suggested default):* staff export the final artifacts, then the cohort's rows are deleted (typed confirmation + non-PII tombstone); DB empty between cycles. *Purge PII, keep anonymized analytics:* both — IF de-identification is strict (all free text dropped; column allowlist to avoid small-cohort re-identification). All options include a per-submission delete for individual removal requests. | open |
| 14 | **FYI / your call:** the R2 resume bucket is public — anyone with a URL can fetch a minor's resume. UUID keys make URLs unguessable-ish, but signed URLs would be stronger (we fetch within minutes of delivery, so short expiries are fine on our side). | resolved — adopted presigned URLs (`X-Amz-Expires=600`) |

---

## Answers received — 2026-07-21 (Andrew, Slack)

Andrew sent the finalized payload contract (3 screenshots) + notes. Captured here; the
`models.py` PROPOSED contract gets reconciled + frozen once the open items below land.

**Finalized shape — essays mode.** Top-level: `submission_id`, `user_email`, `student_name`,
`cohort_name`, `cohort_display_name`, `submitted_at`, `ed`, `is_finaid`, `ats_mode`,
`tier_first_choice`/`tier_second_choice`/`tier_third_choice` (values Honors/Intensive/Regular),
`detected_sub_track`, `gpa_unweighted` + `gpa_weighted`, `resume_url`,
`required_essays[]` / `optional_essays[]` as `{question, answer}` (optional = answered only;
inner questions vary by cohort).
- **resume mode:** just `resume_url`.
- **`resume_url`:** presigned Cloudflare R2 GET, `X-Amz-Expires=600` (10-min expiry), or null.
- **`finaid`** (nested, sent only when `is_finaid: true`):
  `{ sat_score: string|null, test_score_scale: {SAT:1600, PSAT:1600, ACT:36}, fin_aid_essays: [{question, answer}] }`.

**Deltas from our PROPOSED contract (`models.py`) — reconcile at freeze:**
- **GPA:** separate `gpa_unweighted` / `gpa_weighted` fields, not our nested
  `gpa: {unweighted, weighted}`. (One screenshot's example still showed a joined
  `"gpa": "3.95/4.0"` string — confirm the split fields are final.) Answers ask #3.
- **Program choices:** `tier_first_choice/...` not `first_choice/...`; `detected_sub_track`
  not `sub_track`. Answers ask #6 (partial) — align our field names/aliases.
- **Essays:** only `{question, answer}` — **no `field_key`/`min_words`/`max_words`** (ask #5
  unmet ⇒ the exact word-bounds Stage-1 gate has nothing to enforce; degrades to no-check).
- **finaid:** nested in the essays payload, **not** a separate `ats_mode`. Our
  `parse_webhook_payload` currently 422s finaid → change to accept + store.
- Example **omits the ask-#2 fields** (`gpa_explanation`, `relevant_coursework`,
  `institution`, `state_of_residence`, `github_profile`) — location unknown.

**Owner decision (2026-07-21):** finaid = **store but do not score** — accept & persist the
nested block (no more 422), no SAT/finaid-essay gate or subscore, no SCORING.md change.

**Open — the 3 questions sent back to Andrew (kept minimal):**
1. Where do `gpa_explanation` (+ coursework/institution/state/github) live — top-level, or
   inside `required_essays`/`optional_essays`? `gpa_explanation` is the field we cannot
   function without (missing ⇒ sub-3.3 applicants with an explanation auto-reject).
2. Trigger model: continuous per-application POSTs (fast-202 + always-on async worker) vs
   **on-demand batches**? Drives hosting — his Vercel offer is serverless, which can't run
   the always-on worker + DB pool — and interacts with the 10-min resume-URL expiry
   (deferred/batched grading may fetch after expiry). Resolve before any deploy.
3. Auth: our built scheme is HMAC-SHA256 request signing (ask #1), not a static API key —
   a random UUID works fine as the HMAC *secret*. Confirm that's what "UUID for API key" meant.

**Deferred (not blocking; ask later):** exact R2 host (ask #4 — resume stage is off at
`bonus_max: 0`), per-essay word bounds (ask #5), joined-vs-split GPA final confirm.

---

## Repo audit — 2026-07-26 (read-only pass over `thinkneuro_website`)

Read their actual dispatch code instead of asking. Sources: `docs/ats-payload.md`,
`lib/ats.ts`, `lib/qstash.ts`, `lib/r2.ts`, `app/api/apply/ats/worker/route.ts`,
`app/api/apply/admin/ats/run/route.ts`, `lib/questions-default.ts`. **This answers most of
the open asks and invalidates two of our built assumptions.**

### Two breaking mismatches (ours vs theirs) — highest priority

1. **`ats_mode` no longer exists — it's `ats_run: ("essays"|"resume"|"finaid")[]`.**
   They send **ONE combined payload to ONE endpoint** containing *all* sector data; the
   `ats_run` array says which grader(s) to actually run ("fans out … ignore the rest").
   Our `parse_webhook_payload` dispatches on `data["ats_mode"]` and raises
   `UnsupportedModeError` when it's missing ⇒ **every real payload would 422**. The whole
   discriminated-union contract (EssaysModePayload / ResumeModePayload) needs rework into
   one payload + an `ats_run` selector.
2. **Auth is a static shared secret, not HMAC.** `sendWebhook` sets a single header
   `X-ATS-Secret: <ATS_WEBHOOK_SECRET>` (`lib/ats.ts`), no timestamp, no signature, no
   body binding. Our P2 HMAC verification (`X-ATS-Timestamp`/`X-ATS-Signature`) would
   **401 every request**. Also note their comment: when the env var is unset **no header
   is sent at all**. This is what "randomly generated UUID for API key" meant — a static
   secret string. Needs an owner decision (accept theirs vs ask for ask-#1 HMAC).

### Answered — no need to ask

- **Ask #2 (missing fields) — SOLVED by `all_answers`.** Every payload carries
  `all_answers: [{field_key, question, answer}]`, a full dump of every form question
  ("filter on your end"). Confirmed field keys in `lib/questions-default.ts`:
  `gpa_explanation`, `relevant_coursework`, `programming_languages`, `github_profile`,
  `institution`, `state_of_residence`. **`gpa_explanation` is available** — the
  auto-reject risk is gone; we just read it out of `all_answers`.
- **Ask #3 (GPA) — answered.** Separate top-level `gpa_unweighted` / `gpa_weighted`,
  format `"value/max"` (e.g. `"3.95/4.0"`, weighted `"4.23/4.0"`), either may be null.
- **Ask #6 (question config) — answered.** `ats_role` tags drive the essay arrays
  (`required_essay`/`optional_essay`/`finaid_essay`/`sat_score`), with a
  `FALLBACK_ATS_ROLE` map by field_key when unset. Tier values are **raw form answers**:
  CS emits `Regular`/`Intensive`/`Honors`; Med emits `Intensive Cohort`/`Regular Cohort`.
- **Discussion #8 (trigger model) — answered: continuous, one POST per applicant.**
  Auto-run on submit via an Upstash **QStash** queue (`enqueueAtsGrading`, 15 s debounce,
  `retries: 3`), sectors from the cohort's "Auto-run on submit" setting; **plus** manual
  admin runs (`admin/ats/run`, incl. `untested_only`) which loop applicants **sequentially,
  one POST each**. There is no batch-body mode — "batch" just means a burst of single
  POSTs, exactly what PRD v3 §0 assumed. Our fast-ACK + async worker design is correct.
- **Their timeout is 60 s, not 15 s** (`AbortSignal.timeout(60_000)`) — more ACK headroom
  than PRD v3 §2.1 assumed (that figure came from an older `sendWebhook`).
- **Ask #7 (response semantics) — partially answered, and it contradicts our ask.** Any
  2xx = success; the response body is **not parsed**. But they **retry any non-2xx up to 3
  times** with backoff, and QStash retries the worker 3× more — so our 401/422 *will* be
  blind-retried. Harmless (ingest is idempotent + a 4xx touches nothing) but worth knowing.
- **Ask #4 (R2 host) — narrowed.** Presigned URLs are S3-style against
  `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com` (`lib/r2.ts`); key is
  `resume/<submission_id>.pdf`, minted fresh at dispatch (`600 s`). Only the account ID is
  unknown — one sample URL would pin the allowlist.
- **Ask #5 (word bounds) — they already have the data.** `questions.min_words` /
  `max_words` exist in their schema (`lib/schema.sql`) and drive the applicant-facing
  `WordCounter`; they're simply not copied into the essay entries. So the ask is small and
  concrete. (Their Associate/Med form hardcodes `ESSAY_MIN_WORDS=250`/`MAX=300`.)
- **Idempotency/dedup on their side:** a Redis `ats-latest:<submission_id>` nonce collapses
  re-submit bursts so only the final version dispatches; `submission_id` is stable per
  applicant per program. Complements our content-hash idempotency.

### Outstanding — the short list to send Andrew (2026-07-27)

Deliberately three items. Everything else was either answered by the repo audit, decided
on our side, or deferred (see the per-ask status column).

1. **One real sample payload** — a genuine dispatch body with synthetic values (fake name/
   email/essays), captured from the live dispatcher rather than written by hand. This is
   the highest-value ask by a distance: it validates every `all_answers` `field_key`, the
   GPA string format, the tier values, the `submitted_at` offset, and the finaid block in
   one shot. **Why it matters:** we read the field keys out of `lib/questions-default.ts`,
   and ask #6 already established that those seeds differ from the live form — so our
   mapping is currently built on a source we know to be approximate. A wrong
   `gpa_explanation` key silently auto-rejects every sub-3.3 applicant who wrote an
   explanation.
2. **Vercel plan + secret handling** — confirm the project is on **Pro** (per-minute cron
   and >60 s function duration are both Pro-only; the ATS grading drain needs them), and
   say who sets/holds the env vars we would need there (`OPENAI_API_KEY`, `DATABASE_URL`,
   `ATS_WEBHOOK_SECRET`, `ADMIN_PASSWORD_HASH`, session key, `CRON_SECRET`).
3. **Set `ATS_WEBHOOK_SECRET`** — we will send the value. Flagging because their
   `sendWebhook` omits the auth header entirely when the env var is unset, so an unset
   secret looks like a working deploy that 401s every delivery.

*Deferred on purpose (do not send):* exact R2 host (#4 — resume stage is off), per-essay
word bounds (#5 — we hardcode them), HMAC signing (#1 — revisit before production),
joined-vs-split GPA confirm (the sample payload answers it).

### Contract deltas to absorb (beyond the two mismatches)

- **New fields we don't model:** `ats_run`, `all_answers`, `referral`, `referral_code`,
  `time_spent_seconds` (= `save_count × 30`, an engagement proxy — not scored by us).
- **`finaid` is present for *every* applicant** (empty-ish when not finaid), not only when
  `is_finaid: true` as Andrew's Slack note said. `ats_run` drops `"finaid"` for non-finaid
  applicants instead.
- `submitted_at` is **U.S. Pacific** ISO with offset (e.g. `-07:00`), not UTC `Z`.
- If their "ATS Endpoint URL" is blank, dispatch falls back to a legacy per-sector URL.
