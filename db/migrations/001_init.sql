-- 001_init.sql — v3 persistence baseline (PRD v3 §1.1).
-- Three tables, plain SQL, applied by srip_filter.db.apply_migrations (which tracks
-- applied filenames in schema_migrations and runs each file once, in a transaction).

CREATE TABLE IF NOT EXISTS applications (
  submission_id   UUID PRIMARY KEY,
  cohort_name     TEXT NOT NULL DEFAULT '',
  user_email      TEXT NOT NULL DEFAULT '',
  student_name    TEXT NOT NULL DEFAULT '',
  sub_track       TEXT NOT NULL DEFAULT '',
  submitted_at    TIMESTAMPTZ,

  -- Per-mode webhook payloads (PRD v3 §2.2/§2.3). A row may hold either or both;
  -- resume-before-essays arrival is legal.
  essays_payload  JSONB,
  essays_hash     TEXT,
  resume_payload  JSONB,
  resume_hash     TEXT,

  -- Grading lifecycle (the queue IS this column; workers claim with SKIP LOCKED).
  status          TEXT NOT NULL DEFAULT 'received'
                  CHECK (status IN ('received', 'grading', 'graded', 'error')),

  -- Grading result. Rank is NEVER stored — computed at read time per cohort.
  audit_record    JSONB,
  outcome         TEXT CHECK (outcome IN ('REJECTED', 'RANKED', 'NEEDS_REVIEW')),
  final_score     DOUBLE PRECISION,

  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_applications_cohort  ON applications (cohort_name);
CREATE INDEX IF NOT EXISTS idx_applications_status  ON applications (status);
CREATE INDEX IF NOT EXISTS idx_applications_updated ON applications (updated_at DESC);

-- Persistent LLM cache (PRD v3 §5): the v2 in-run cache made durable, so re-grades
-- re-bill only changed fields. Key matches the v2 client: (task, sha256(input_text)).
CREATE TABLE IF NOT EXISTS llm_cache (
  task          TEXT NOT NULL,
  input_sha256  TEXT NOT NULL,
  output        JSONB NOT NULL,
  model         TEXT NOT NULL DEFAULT '',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (task, input_sha256)
);

-- Non-PII operational ledger (PRD v3 §1.1): deliveries, grade completions, manual
-- overrides (decided_by), purge tombstones. details MUST NOT contain essay/explanation/
-- resume text — submission_id and structural facts only (enforced by code review + the
-- db.add_event docstring, not by the schema).
CREATE TABLE IF NOT EXISTS events (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind           TEXT NOT NULL,
  submission_id  UUID,
  details        JSONB,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_kind    ON events (kind);
CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_at DESC);
