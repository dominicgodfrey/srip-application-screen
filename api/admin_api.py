"""Admin JSON API over the live database (P6, PRD v3 §6).

The review UI's data source: everything here reads/writes ``applications`` via
``srip_filter.db`` and is session-gated by the P5 middleware (this module adds no auth of
its own). Rank is assigned at read time, per cohort, on every response — never stored.

Routes (all under ``/api``):

* ``GET    /api/applications``               — dashboard listing (+ ``?cohort=`` filter)
* ``GET    /api/applications/{sid}``         — one full record (status + audit record)
* ``POST   /api/applications/{sid}/promote`` — manual re-score with gates bypassed (LLM spend)
* ``POST   /api/applications/{sid}/demote``  — manual removal from the ranking (no spend)
* ``DELETE /api/applications/{sid}``         — hard delete (individual removal requests)
* ``GET    /api/exports/{artifact}``         — decisions.jsonl / CSVs / summary from the DB
* ``POST   /api/cohorts``                    — cohort what-if over the live ranking
* ``POST   /api/admin/migrate``              — apply pending migrations (P11.3 bootstrap)
* ``GET    /api/admin/purge-preview``        — what a purge would destroy (read-only)
* ``POST   /api/admin/purge``                — bulk hard delete, count-guarded (§9)

Manual overrides append a non-PII ``events`` entry with ``decided_by`` (PRD v3 §1.1);
under the shared-password model that is the literal ``"admin"``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import ValidationError

from srip_filter import db as dbmod
from srip_filter.cohort import assign_cohorts
from srip_filter.ingest_webhook import map_application_payload
from srip_filter.models import (
    ApplicationPayload,
    AuditRecord,
    CohortCapacities,
)
from srip_filter.outputs import build_summary
from srip_filter.pipeline import grade_webhook_applicant
from srip_filter.scoring.aggregate import assign_read_time_ranks

from .cohorts import CohortFormat, cohort_response
from .jobs import ArtifactName, artifact_response_from_records
from .schemas import PurgeRequest

logger = logging.getLogger(__name__)

DECIDED_BY = "admin"  # single shared credential (P5) — the only identity available

_Capacity = Annotated[int | None, Query(ge=0, description="Seat cap; omit for unlimited.")]

# `applications.submission_id` is a UUID column, so asyncpg raises on any value it cannot
# encode as one. Typing the path param makes FastAPI reject a malformed id with a 422 at
# the edge, instead of letting it reach the driver and surface as a 500 — the same
# never-a-500-on-bad-input rule the webhook edge follows. Handlers immediately stringify
# it, so the store layer's `str` contract is unchanged.
SubmissionId = uuid.UUID


def _record_from_row(row: dict[str, Any]) -> AuditRecord | None:
    """Rebuild the AuditRecord stored on a graded row; None for not-yet-graded rows."""
    raw = row.get("audit_record")
    if not raw:
        return None
    try:
        return AuditRecord.model_validate(raw)
    except ValidationError:
        # A record written by an older schema version must not 500 the dashboard.
        logger.warning("stored audit_record failed validation sid=%s", row["submission_id"])
        return None


def _ranked_records(rows: list[dict[str, Any]]) -> dict[str, AuditRecord]:
    """All rebuildable records keyed by submission id, with read-time per-cohort ranks."""
    records = {
        str(row["submission_id"]): rec
        for row in rows
        if (rec := _record_from_row(row)) is not None
    }
    assign_read_time_ranks(list(records.values()))
    return records


def _summary_row(row: dict[str, Any], record: AuditRecord | None) -> dict[str, Any]:
    """One dashboard listing entry — identity + status + outcome, no essay text."""
    return {
        "submission_id": str(row["submission_id"]),
        "name": record.name if record else row.get("student_name") or "",
        "email": record.email if record else row.get("user_email") or "",
        "cohort_name": row.get("cohort_name") or "",
        "sub_track": row.get("sub_track") or "",
        "status": row.get("status"),
        "outcome": record.outcome if record else None,
        "final_score": record.final_score if record else None,
        "rank": record.rank if record else None,
        "primary_reason": record.primary_reason if record else "",
        "manual_override": record.manual_override if record else False,
        "international": record.international if record else False,
        "submitted_at": row["submitted_at"].isoformat() if row.get("submitted_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def register_admin_api(app: FastAPI) -> None:
    """Attach the DB-backed admin endpoints. Reads pool/client/config off ``app.state``."""

    def _pool():
        pool = app.state.db_pool
        if pool is None:
            raise HTTPException(status_code=503, detail="Database is not configured.")
        return pool

    async def _row_or_404(pool, submission_id: str) -> dict[str, Any]:
        row = await dbmod.get_application(pool, submission_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No application with that id.")
        return row

    @app.post("/api/admin/migrate", response_model=None, tags=["admin"])
    async def migrate() -> dict:
        """Apply pending migrations (P11.3) — the manual first run on a new database.

        Session-gated like everything else under ``/api``. The cron drain runs the same
        advisory-locked pass every minute, so this is only needed to bootstrap a database
        before any cron has fired.
        """
        return {"applied": await dbmod.apply_migrations(_pool())}

    @app.get("/api/applications", response_model=None, tags=["admin"])
    async def list_applications(cohort: str | None = None) -> dict:
        """Live cohort listing with read-time ranks and outcome counts."""
        pool = _pool()
        rows = await dbmod.list_applications(pool, cohort_name=cohort)
        records = _ranked_records(rows)
        listing = [
            _summary_row(row, records.get(str(row["submission_id"]))) for row in rows
        ]
        counts: dict[str, int] = {}
        for entry in listing:
            key = entry["outcome"] or entry["status"] or "unknown"
            counts[key] = counts.get(key, 0) + 1
        cohorts = sorted({e["cohort_name"] for e in listing if e["cohort_name"]})
        return {"applications": listing, "counts": counts, "cohorts": cohorts}

    @app.get("/api/applications/{submission_id}", response_model=None, tags=["admin"])
    async def get_application(submission_id: SubmissionId) -> dict:
        """One applicant: lifecycle status + the full audit record (rank read-time)."""
        pool = _pool()
        row = await _row_or_404(pool, str(submission_id))
        # Rank must reflect the applicant's place in the live cohort, so rank the cohort.
        cohort_rows = await dbmod.list_applications(
            pool, cohort_name=row.get("cohort_name") or None
        )
        records = _ranked_records(cohort_rows)
        record = records.get(str(row["submission_id"]))
        return {
            "submission_id": str(row["submission_id"]),
            "status": row.get("status"),
            "has_payload": row.get("payload") is not None,
            "audit_record": record.model_dump(mode="json") if record else None,
        }

    @app.post(
        "/api/applications/{submission_id}/promote", response_model=None, tags=["admin"]
    )
    async def promote(submission_id: SubmissionId) -> dict:
        """Manually promote into the ranking: full re-score with gates recorded-but-bypassed.

        Spends LLM tokens (the re-score); the durable cache makes unchanged fields free.
        409 for already-RANKED or not-yet-graded rows; 409 when no payload is stored.
        """
        pool = _pool()
        sid = str(submission_id)
        row = await _row_or_404(pool, sid)
        existing = _record_from_row(row)
        if existing is None:
            raise HTTPException(
                status_code=409, detail="Application has not been graded yet."
            )
        if existing.outcome == "RANKED":
            raise HTTPException(status_code=409, detail="Application is already ranked.")
        if not row.get("payload"):
            raise HTTPException(status_code=409, detail="No stored payload to score.")

        payload = ApplicationPayload.model_validate(row["payload"])
        applicant = map_application_payload(payload)
        record = await grade_webhook_applicant(
            applicant, app.state.llm_client, app.state.config, bypass_gates=True
        )
        await dbmod.finish_graded(
            pool,
            sid,
            audit_record=record.model_dump(mode="json"),
            outcome=record.outcome,
            final_score=record.final_score,
        )
        await dbmod.add_event(
            pool,
            "manual_promote",
            submission_id=sid,
            details={"decided_by": DECIDED_BY, "final_score": record.final_score},
        )
        return {"record": record.model_dump(mode="json")}

    @app.post(
        "/api/applications/{submission_id}/demote", response_model=None, tags=["admin"]
    )
    async def demote(submission_id: SubmissionId) -> dict:
        """Manually remove a RANKED applicant from the ranking (→ REJECTED). No LLM spend.

        Every gate verdict and subscore stays on the record; reversible via promote.
        """
        pool = _pool()
        sid = str(submission_id)
        row = await _row_or_404(pool, sid)
        record = _record_from_row(row)
        if record is None:
            raise HTTPException(
                status_code=409, detail="Application has not been graded yet."
            )
        if record.outcome != "RANKED":
            raise HTTPException(status_code=409, detail="Application is not ranked.")

        record.outcome = "REJECTED"
        record.manual_override = True
        record.decided_at_stage = "manual_override"
        record.primary_reason = "Manually removed from the ranking"
        record.final_score = None
        record.rank = None
        record.reasons.append("OVERRIDE: manually demoted by admin")
        await dbmod.finish_graded(
            pool,
            sid,
            audit_record=record.model_dump(mode="json"),
            outcome=record.outcome,
            final_score=None,
        )
        await dbmod.add_event(
            pool,
            "manual_demote",
            submission_id=sid,
            details={"decided_by": DECIDED_BY},
        )
        return {"record": record.model_dump(mode="json")}

    @app.delete("/api/applications/{submission_id}", response_model=None, tags=["admin"])
    async def delete_application(submission_id: SubmissionId) -> Response:
        """Hard-delete one applicant (individual removal request, PRD v3 §9). Tombstoned."""
        pool = _pool()
        deleted = await dbmod.delete_submission(pool, str(submission_id))
        if not deleted:
            raise HTTPException(status_code=404, detail="No application with that id.")
        return Response(status_code=204)

    @app.get("/api/admin/purge-preview", response_model=None, tags=["admin"])
    async def purge_preview(cohort: str | None = None) -> dict:
        """What a purge of this scope would destroy — the confirmation dialog's source.

        Read-only. ``cohort`` omitted means every cohort (a full wipe, which also clears
        ``llm_cache``). Returns counts and date spans only, never applicant fields.
        """
        return await dbmod.purge_preview(_pool(), cohort_name=cohort)

    @app.post("/api/admin/purge", response_model=None, tags=["admin"])
    async def purge(body: PurgeRequest) -> dict:
        """Hard-delete every application in scope (PRD v3 §9). Irreversible; tombstoned.

        ``expected_count`` must match the live count or this 409s without deleting anything:
        webhook deliveries keep landing while a human reads the dialog, and the operator only
        consented to the number they were shown. The client re-previews and asks again.
        """
        try:
            receipt = await dbmod.purge_applications(
                _pool(), cohort_name=body.cohort, expected_count=body.expected_count
            )
        except dbmod.PurgeCountMismatch as mismatch:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Nothing was deleted — the application count changed from "
                    f"{mismatch.expected} to {mismatch.actual} while the dialog was open. "
                    f"Review the new figures and confirm again."
                ),
            ) from mismatch
        # decided_by mirrors the promote/demote overrides: under the shared-password model the
        # only identity available is the literal "admin" (PRD v3 §1.1).
        logger.warning("purge by %s: %s", DECIDED_BY, receipt)
        return {**receipt, "decided_by": DECIDED_BY}

    @app.get("/api/exports/{artifact}", response_model=None, tags=["admin"])
    async def export_artifact(artifact: ArtifactName, cohort: str | None = None) -> Response:
        """Generate one §6 export on demand from the live DB (optionally one cohort)."""
        pool = _pool()
        rows = await dbmod.list_applications(pool, cohort_name=cohort)
        records = list(_ranked_records(rows).values())
        return artifact_response_from_records(records, artifact)

    @app.post("/api/cohorts", response_model=None, tags=["admin"])
    async def cohorts_whatif(
        cohort: str | None = None,
        honors: _Capacity = None,
        intensive: _Capacity = None,
        regular: _Capacity = None,
        format: CohortFormat = "json",
        tier: str | None = None,
    ) -> Response:
        """Cohort what-if over the LIVE ranking (recomputed per call, nothing stored)."""
        pool = _pool()
        rows = await dbmod.list_applications(pool, cohort_name=cohort)
        records = list(_ranked_records(rows).values())
        capacities = CohortCapacities(honors=honors, intensive=intensive, regular=regular)
        result = assign_cohorts(records, capacities, app.state.config)
        return cohort_response(result, format, tier)

    @app.get("/api/summary", response_model=None, tags=["admin"])
    async def live_summary(cohort: str | None = None) -> dict:
        """Outcome counts + histogram for the dashboard (same shape as the v2 summary)."""
        pool = _pool()
        rows = await dbmod.list_applications(pool, cohort_name=cohort)
        records = list(_ranked_records(rows).values())
        summary = build_summary(records)
        summary["ungraded"] = sum(1 for r in rows if not r.get("audit_record"))
        return summary
