# SRIP ATS — Continuous Application Filtering Service (CS Track)

A **persistent, secured webhook-receiver** ATS. The partner-owned application website POSTs
one JSON payload per application; this service validates, stores, grades asynchronously,
and gives staff a session-gated review UI over the live cohort. It **rejects** applications
failing deterministic hard-gate quality checks and **scores + ranks** every survivor. It
does *not* decide acceptances — that is a downstream step consuming this system's ranked output.

Results persist in a dedicated Neon Postgres database (applications · llm_cache · events).
The service deploys to Vercel as a single function; grading runs from a per-minute cron.

> **v2 note:** the stateless Fillout-CSV batch system described by
> [`SRIP_Application_Filter_PRD.md`](SRIP_Application_Filter_PRD.md) is **superseded** and
> frozen on the `v2-fillout-batch` branch. It stays in the repo because v3 carries over its
> GPA (§6), task-contract (§8), and audit-record (§9) semantics verbatim.

## Docs
- [`CLAUDE.md`](CLAUDE.md) — how the system is built (stack, conventions, guardrails)
- [`SRIP_ATS_PRD_v3.md`](SRIP_ATS_PRD_v3.md) — **current** functional spec (what it decides)
- [`SCORING.md`](SCORING.md) — the 150-point scoring model
- [`SRIP_Application_Filter_PRD.md`](SRIP_Application_Filter_PRD.md) — superseded v2 spec

## Dev quickstart
Requires [uv](https://docs.astral.sh/uv/); the Python version is managed via `.python-version`.

```
uv sync                 # create the venv + install deps (fetches Python if needed)
uv run pytest           # run the test suite
uv run ruff check .     # lint
uv run uvicorn api.main:app --port 8321   # local server
```

Set `OPENAI_API_KEY` in `.env` (copy from `.env.example`) before running LLM stages.

Two env flags exist for local runs only, and neither should ever be set in production:
`SRIP_DEV_FAKE_LLM=1` swaps in a zero-spend fake model client, and `SRIP_LOCAL_WORKER=1`
starts the in-process grading loop (deployed, the per-minute cron drain does that job).

## Deployment (Vercel)
Deployed and managed by the partner team in their Vercel project (**Pro plan** — per-minute
cron and a `maxDuration` above 60 s are both Pro-only).

The whole app is one Vercel Function: `vercel.json` pins `maxDuration` and the per-minute
cron, and `[tool.vercel] entrypoint` in `pyproject.toml` points at `api.main:app`. Grading
runs from `POST /api/cron/drain` (bearer `CRON_SECRET`), which also applies pending
migrations under an advisory lock — there is no release phase, so the drain owns them.
`POST /api/admin/migrate` (session-gated) is the manual first run on a fresh database.

**First-deploy checklist**
1. Provision the database (see below) and set every environment variable in the table (an
   unset `ATS_WEBHOOK_SECRET` makes the website's dispatcher omit the auth header entirely,
   so every delivery 401s; an unset `CRON_SECRET` means nothing is ever graded).
2. Deploy, then `curl` `/health` — expect 200.
3. `POST /api/admin/migrate` once, or wait one minute for the cron to do it.
4. Fire the website admin panel's ATS **Test** button — expect 200 and no row created.
5. Send one real application, confirm it appears on the dashboard as `graded`.

### Bringing your own Neon database
The service is not tied to any particular Neon project — it owns whatever database
`DATABASE_URL` points at, and creates its own schema there. To use a fresh one:

1. Create a Neon project (or a branch) and a database. **It must be the ATS's own** — never
   the website's, and ideally with credentials scoped to it alone, since this database holds
   minors' PII.
2. Point `DATABASE_URL` at its **pooled** (`-pooler`) DSN. The pool ships
   `statement_cache_size=0`, which is what makes asyncpg safe against Neon's PgBouncer
   transaction mode; the direct host works too but wastes the pooler serverless needs.
3. Deploy and run step 3 above. `db/migrations/*.sql` is applied in order under a Postgres
   advisory lock and recorded in a `schema_migrations` table, so it is safe to run
   concurrently and idempotent to run twice — an empty database and an up-to-date one both
   end in the same place. Three tables are created: `applications`, `llm_cache`, `events`.
4. Optionally set `DATABASE_URL_TEST` to a **separate** Neon branch (direct host, not pooled)
   to enable the DB test suite. Leave it unset and those tests skip cleanly — `uv run pytest`
   on a fresh clone with no `.env` at all passes 554 and skips 19.

Nothing else is environment-specific: there is no external queue, no second datastore, and
no state outside Postgres, so moving databases is just repointing `DATABASE_URL` (the new one
starts empty — data does not follow).

**Monitoring.** Point an uptime check at `GET /health` (unauthenticated, no PII, no counts).
It returns 200 `{"status":"ok"}` normally and **503** `{"status":"degraded"}` when the oldest
ungraded application is older than `worker.queue_alert_seconds` (1 h) or the database is
unreachable. That covers the failure mode with no other symptom: the cron drain silently
stopping, which otherwise goes unnoticed until someone opens the dashboard. A backlog being
worked through is healthy — the threshold is set above the ~40 min a full cohort burst needs.

Environment variables to set in the Vercel project:

| Name | Purpose |
|---|---|
| `DATABASE_URL` | Neon **pooled** (`-pooler`) DSN — the ATS's own database, never the website's |
| `OPENAI_API_KEY` | LLM tasks |
| `ATS_WEBHOOK_SECRET` | must equal the website's value, or every delivery 401s |
| `ATS_WEBHOOK_SECRET_PREVIOUS` | set only during a rotation |
| `ADMIN_PASSWORD_HASH` | `uv run python -m api.auth '<password>'` — also signs session cookies, so changing it logs everyone out |
| `CRON_SECRET` | Vercel sends it as `Authorization: Bearer …` on cron invocations. **Unset ⇒ the drain fails closed and nothing is ever graded** |

Every one of these fails closed rather than silently degrading: no webhook secret ⇒ 401 on
every delivery; no `CRON_SECRET` ⇒ no grading; no `ADMIN_PASSWORD_HASH` ⇒ `/login` 503s and
the UI is shut; no `DATABASE_URL` ⇒ 503 from the data routes. A deploy missing one looks
broken in a specific way rather than working incorrectly.

## Privacy
This system processes minors' PII. Applicant data **is persisted** — in the ATS's own Neon
Postgres database only (v2's "no database" rule was deliberately overturned; see PRD v3 §9).
The privacy stance is retention-based, so the DB can be emptied between cycles:

- **Per-submission delete** — for individual removal requests.
- **Bulk purge** — the `Purge data…` control in the bottom corner of any signed-in page. It
  shows exactly what is about to be destroyed (row count, cohorts, submission-date span,
  outcome split, and the applicant fields involved) and deletes only on confirmation. Scope is
  one cohort or every cohort; a full wipe also clears `llm_cache`, which holds model output
  derived from essay text. If applications arrive while the dialog is open the confirm is
  rejected rather than deleting a different set than the one displayed.
- **No export is taken by a purge** — download anything you need first. The export-then-purge
  close-cycle flow of PRD v3 §9 is still designed rather than built.

Both actions are session-gated and leave a non-PII tombstone in `events` (counts + timestamp).
Never commit `data/`, `.env`, results files, or any real applicant content. Test fixtures are
synthetic. Logs and the `events` ledger carry `submission_id` only — never essay, explanation,
or resume text.

Resume PDFs (Stage 6) are downloaded only from the https hosts pinned in
`resume.allowed_url_hosts` (`config.yaml`), processed in memory one applicant at a time
(fetch → extract → discard), and never stored or logged. Setting `resume.bonus_max: 0`
disables the stage entirely (zero fetches, zero LLM calls).
