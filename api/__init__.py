"""Thin FastAPI shell over the transport-agnostic core.

The core (``srip_filter``) knows nothing about HTTP; this package receives the partner's
signed per-application webhook, upserts it into Postgres, and serves the session-gated review
UI + export endpoints over the same store. Grading itself is the worker's job, never a request
handler's.
"""
