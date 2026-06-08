# SRIP Track 2 — Application Filtering System

A stateless service that **rejects** applications failing deterministic hard-gate quality
checks and **scores + ranks** every survivor. It does *not* decide acceptances — that is a
deferred downstream step that consumes this system's ranked output.

Input is a CSV export from Fillout; output is a set of downloadable result files. Nothing is
persisted between sessions.

## Docs
- [`CLAUDE.md`](CLAUDE.md) — how the system is built (stack, conventions, guardrails)
- [`SRIP_Application_Filter_PRD.md`](SRIP_Application_Filter_PRD.md) — functional spec (what it decides)
- [`PLAN.md`](PLAN.md) — phase-by-phase progress tracker
- [`openissue.md`](openissue.md) — owner inputs still required

## Dev quickstart
Requires [uv](https://docs.astral.sh/uv/); the Python version is managed via `.python-version`.

```
uv sync                 # create the venv + install deps (fetches Python if needed)
uv run pytest           # run the test suite
uv run ruff check .     # lint
```

Set `OPENAI_API_KEY` in `.env` (copy from `.env.example`) before running LLM stages.

## Privacy
This system processes minors' PII. Nothing is written to disk or a database; never commit
`data/`, `.env`, results files, or any real applicant content. Test fixtures are synthetic.
