"""In-memory job registry for the stateless API (Phase 9.1).

A long ``grade_batch`` run can't be held in a single HTTP request (multi-minute work, free-tier
timeouts), so an upload schedules a background job and the client polls. This registry is the only
place a job lives: a dict keyed by UUID, holding lifecycle state, progress counts, and — once done
— the in-memory :class:`~srip_filter.pipeline.BatchResult` (which carries the run's PII).

Stateless by design (PRD §0 / Privacy): nothing is written to disk or a database. PII lives only
inside a finished job's ``result`` and is dropped the moment the client downloads it
(:meth:`JobRegistry.evict`) or the TTL sweeper (:meth:`JobRegistry.sweep`) drops it — whichever
comes first. A host restart loses every job; that is the intended robustness trade (no
resume-after-refresh).

Concurrency: all access happens on the single API event loop — the background grading task is an
``asyncio`` task in the same loop, not a thread — so the plain dict needs no lock.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from enum import StrEnum

from srip_filter.pipeline import BatchResult


class JobState(StrEnum):
    """Lifecycle of a grading job. ``succeeded``/``failed`` are terminal."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


_TERMINAL_STATES = frozenset({JobState.SUCCEEDED, JobState.FAILED})


@dataclass
class Job:
    """One grading job's transient state. Mutated in place by the API handlers.

    ``rows_total`` is unknown until ingest finishes (set when grading starts); ``rows_done`` ticks
    up via the Phase 9.3 progress callback. ``result`` holds the in-memory artifacts only while the
    job is ``SUCCEEDED`` and undownloaded; ``error`` carries a *safe* message (never PII, never a
    stack trace) when ``FAILED``.
    """

    job_id: str
    state: JobState
    created_at: float
    filename: str = ""  # uploaded CSV's name, so the UI can say which file results came from
    rows_total: int | None = None
    rows_done: int = 0
    finished_at: float | None = None
    result: BatchResult | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    def _reference_time(self) -> float:
        """Time the TTL clock counts from: when finished, else when created (stuck-job reaping)."""
        return self.finished_at if self.finished_at is not None else self.created_at

    def is_expired(self, ttl_seconds: float, *, now: float) -> bool:
        return now - self._reference_time() >= ttl_seconds


class JobRegistry:
    """A dict of live jobs with TTL eviction and discard-after-download.

    ``ttl_seconds`` defaults from :class:`~srip_filter.config.ApiConfig`. The registry owns only
    storage and lifecycle (create / look up / evict / sweep); the handlers own the state
    transitions by mutating the returned :class:`Job`.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._jobs: dict[str, Job] = {}
        self._ttl_seconds = ttl_seconds

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    def create(self, *, now: float | None = None, filename: str = "") -> Job:
        """Register a fresh ``QUEUED`` job with a new UUID and return it."""
        job = Job(
            job_id=str(uuid.uuid4()),
            state=JobState.QUEUED,
            created_at=time.monotonic() if now is None else now,
            filename=filename,
        )
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        """Return the job, or ``None`` if unknown or already evicted (→ 404 at the edge)."""
        return self._jobs.get(job_id)

    def evict(self, job_id: str) -> None:
        """Drop a job (and its in-memory PII) immediately. Idempotent."""
        self._jobs.pop(job_id, None)

    def sweep(self, *, now: float | None = None) -> int:
        """Evict every expired job; return how many were dropped.

        A finished job expires ``ttl_seconds`` after it finished; an unfinished one expires
        ``ttl_seconds`` after creation (so a wedged run can't pin PII forever). Called periodically
        by the background sweeper (Phase 9.4) and safe to call any time.
        """
        clock = time.monotonic() if now is None else now
        expired = [
            jid for jid, job in self._jobs.items() if job.is_expired(self._ttl_seconds, now=clock)
        ]
        for jid in expired:
            del self._jobs[jid]
        return len(expired)

    def __len__(self) -> int:
        return len(self._jobs)


__all__ = ["Job", "JobRegistry", "JobState"]
