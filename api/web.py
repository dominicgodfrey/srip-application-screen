"""Server-rendered page routes (Phase 10 UI).

Three thin HTML routes that return Jinja2-rendered shells; **all data fetching happens in the
browser** via ``fetch`` against the existing JSON API (so these templates never contain applicant
PII). Registered onto the app by :func:`register_pages` from ``api.main``, keeping ``main.py`` a
clean JSON-API surface.

* ``GET /``         → live cohort dashboard over the database (v3 screen 1)
* ``GET /audit``    → per-applicant audit-record browser over the live DB
* ``GET /cohorts``  → cohort what-if tool over the live DB

``tags=["pages"]`` keeps them out of the JSON OpenAPI groupings.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# Branding (PRD visual-continuity decision): reuse ThinkNeuro identity, label the app clearly.
BRAND = "ThinkNeuro"
APP_TITLE = "SRIP Track 2 — Application Filter"


def _ctx(**extra: object) -> dict[str, object]:
    """Base template context shared by every page (non-PII only)."""
    return {"brand": BRAND, "app_title": APP_TITLE, **extra}


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
