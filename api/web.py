"""Server-rendered page routes: the dashboard, the audit browser, and the cohort what-if tool.

Thin Jinja2 shells — **all data fetching happens in the browser** against the JSON API, so
these templates never contain applicant PII.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

BRAND = "ThinkNeuro"
APP_TITLE = "SRIP Track 2 — Application Filter"


def _ctx(**extra: object) -> dict[str, object]:
    """Base template context shared by every page, non-PII only.

    ``show_purge`` gates the bulk-purge control, and only these session-gated routes set it —
    ``login.html`` extends the same base but builds its context in ``api.main``, so the
    destructive control never renders to an unauthenticated visitor.
    """
    return {"brand": BRAND, "app_title": APP_TITLE, "show_purge": True, **extra}


def register_pages(app: FastAPI, templates: Jinja2Templates) -> None:
    """Attach the three server-rendered page routes to ``app``."""

    @app.get("/", response_class=HTMLResponse, tags=["pages"])
    async def index(request: Request) -> HTMLResponse:
        """Screen 1 (v3) — live cohort dashboard over the database."""
        return templates.TemplateResponse(request, "dashboard.html", _ctx())

    @app.get("/audit", response_class=HTMLResponse, tags=["pages"])
    async def audit_page(request: Request) -> HTMLResponse:
        """Screen 2 — browse every applicant's audit record in the live cohort."""
        return templates.TemplateResponse(request, "audit.html", _ctx())

    @app.get("/cohorts", response_class=HTMLResponse, tags=["pages"])
    async def cohort_page(request: Request) -> HTMLResponse:
        """Screen 3 — cohort what-if over the live ranking (or a re-uploaded decisions.jsonl)."""
        return templates.TemplateResponse(request, "cohort.html", _ctx())


__all__ = ["register_pages", "BRAND", "APP_TITLE"]
