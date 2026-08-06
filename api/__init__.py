"""Thin FastAPI shell over the transport-agnostic core.

Receives the partner's authenticated webhook, upserts it into Postgres, and serves the
session-gated review UI over the same store. Grading is the worker's job, never a handler's.
"""
