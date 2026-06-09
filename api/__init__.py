"""Thin stateless FastAPI shell over the transport-agnostic core (Phase 9).

The core (``srip_filter``) knows nothing about HTTP; this package uploads a CSV, schedules a
background ``grade_batch`` run, polls progress, and streams the in-memory artifacts back —
persisting nothing. An interrupted run is abandoned (no DB, no queue).
"""
