# SRIP ATS — Continuous Application Filtering Service (CS Track)

A **persistent, secured webhook-receiver** ATS. The partner-owned thinkNeuroWebsite POSTs
one JSON payload per application; this service validates, stores, grades asynchronously,
and gives staff a session-gated review UI over the live cohort. It **rejects** applications
failing deterministic hard-gate quality checks and **scores + ranks** every survivor. It
does *not* decide acceptances — that is a downstream step consuming this system's ranked output.

Results persist in a dedicated Neon Postgres database (applications · llm_cache · events).

> **v2 note:** the stateless Fillout-CSV batch system described by
> [`SRIP_Application_Filter_PRD.md`](SRIP_Application_Filter_PRD.md) is **superseded** and
> frozen on the `v2-fillout-batch` branch.
> **v3.1 in flight:** the contract, webhook auth, and hosting model are mid-reversal —
> see the banner in [`CLAUDE.md`](CLAUDE.md) and PLAN.md → "Phase Map (v3.1)".

## Docs
- [`CLAUDE.md`](CLAUDE.md) — how the system is built (stack, conventions, guardrails)
- [`SRIP_ATS_PRD_v3.md`](SRIP_ATS_PRD_v3.md) — **current** functional spec (what it decides)
- [`SCORING.md`](SCORING.md) — the 150-point scoring model
- [`WEBSITE_ASKS.md`](WEBSITE_ASKS.md) — partner-team asks, answers, and open discussions
- [`SRIP_Application_Filter_PRD.md`](SRIP_Application_Filter_PRD.md) — superseded v2 spec
- [`PLAN.md`](PLAN.md) — phase-by-phase progress tracker
- [`openissue.md`](openissue.md) — owner inputs still required

## Dev quickstart
Requires [uv](https://docs.astral.sh/uv/); the Python version is managed via `.python-version`.

```
uv sync --extra api     # create the venv + install deps incl. the API/UI (fetches Python if needed)
uv run pytest           # run the test suite
uv run ruff check .     # lint
```

The `--extra api` group (FastAPI/uvicorn/Jinja2) is required for the API + UI and for the
`tests/api/` suite; omit it only if you want the transport-agnostic core alone.

Set `OPENAI_API_KEY` in `.env` (copy from `.env.example`) before running LLM stages.

## Privacy
This system processes minors' PII. Applicant data **is persisted** — in the ATS's own Neon
Postgres database only (v2's "no database" rule was deliberately overturned; see PRD v3 §9).
The privacy stance is now retention-based: per-submission delete plus a close-cycle
export-then-purge, so the DB is empty between cycles. Never commit `data/`, `.env`, results
files, or any real applicant content. Test fixtures are synthetic. Logs and the `events`
ledger carry `submission_id` only — never essay, explanation, or resume text.

Resume PDFs (Stage 6) are downloaded only from the https hosts pinned in
`resume.allowed_url_hosts` (`config.yaml`), processed in memory one applicant at a time
(fetch → extract → discard), and never stored or logged. Setting `resume.bonus_max: 0`
disables the stage entirely (zero fetches, zero LLM calls).
