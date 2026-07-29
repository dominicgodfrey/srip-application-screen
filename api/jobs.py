"""Upload size capping + the five downloadable result artifacts.

Two things survive here from the v2 upload edge (P11.5 retired the rest with the in-memory
job machinery): the streaming byte cap used by ``POST /cohorts``, and the artifact
serialization used by the v3 ``/api/exports/{artifact}`` route. Every rejection is a
graceful 4xx (413 too-large, 422 unprocessable) — **never a 500**.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum

from fastapi import HTTPException, UploadFile
from fastapi.responses import Response

logger = logging.getLogger(__name__)

_READ_CHUNK = 1 << 20  # 1 MiB streaming chunk — bounds memory to max_bytes + one chunk

# Status codes as plain ints: Starlette renamed its 413/422 constants across versions and the old
# names warn on access, so literals stay correct across the whole supported FastAPI range.
_HTTP_413_TOO_LARGE = 413  # Content Too Large
_HTTP_422_UNPROCESSABLE = 422  # Unprocessable Content


async def read_upload_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """Stream the upload into memory, aborting with 413 the moment it exceeds ``max_bytes``.

    Reads in chunks so an oversize body is rejected without buffering the whole thing — peak
    memory is ``max_bytes`` plus one chunk.
    """
    buffer = bytearray()
    while chunk := await upload.read(_READ_CHUNK):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise HTTPException(
                status_code=_HTTP_413_TOO_LARGE,
                detail=f"Uploaded file exceeds the maximum size of {max_bytes} bytes.",
            )
    return bytes(buffer)


class ArtifactName(StrEnum):
    """The five downloadable Stage-9 outputs (PRD §12). A path param of this type makes FastAPI
    reject an unknown artifact with 422 and self-document the valid names in the OpenAPI schema."""

    DECISIONS = "decisions"
    RANKED = "ranked"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"
    SUMMARY = "summary"


# artifact -> (downloaded filename, media type). The four string artifacts are served verbatim;
# ``summary`` (a dict) is JSON-encoded on the way out.
_ARTIFACTS: dict[ArtifactName, tuple[str, str]] = {
    ArtifactName.DECISIONS: ("decisions.jsonl", "application/x-ndjson"),
    ArtifactName.RANKED: ("ranked.csv", "text/csv"),
    ArtifactName.REJECTED: ("rejected.csv", "text/csv"),
    ArtifactName.NEEDS_REVIEW: ("needs_review.csv", "text/csv"),
    ArtifactName.SUMMARY: ("summary.json", "application/json"),
}


def artifact_response_from_records(records: list, artifact: ArtifactName) -> Response:
    """v3 (P6): the same five artifacts, generated on demand from live DB records.

    The DB is the source of truth in v3, so an export serializes whatever the caller just
    read (already ranked at read time).
    """
    from srip_filter.outputs import (
        build_summary,
        decisions_jsonl,
        needs_review_csv,
        ranked_csv,
        rejected_csv,
    )

    builders = {
        ArtifactName.DECISIONS: decisions_jsonl,
        ArtifactName.RANKED: ranked_csv,
        ArtifactName.REJECTED: rejected_csv,
        ArtifactName.NEEDS_REVIEW: needs_review_csv,
        ArtifactName.SUMMARY: build_summary,
    }
    payload = builders[artifact](records)
    filename, media_type = _ARTIFACTS[artifact]
    content = json.dumps(payload, indent=2) if artifact is ArtifactName.SUMMARY else payload
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
