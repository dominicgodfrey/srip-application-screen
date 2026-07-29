"""Server-rendered UI tests (Phase 10): page routes + static assets.

TestClient cannot run browser JS, so these cover what the server controls — each page renders
(200, html, expected markers) and the static assets serve with sane content types. The
interactive flows (sort/what-if) are verified manually in a browser.
Zero LLM spend: nothing is graded here.
"""

from __future__ import annotations

import pytest
from api.main import create_app
from fastapi.testclient import TestClient

from srip_filter.config import AppConfig


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app(config=AppConfig()))


# --------------------------------------------------------------------------------------------
# Page routes
# --------------------------------------------------------------------------------------------


def test_index_renders_live_dashboard(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert 'id="dash-app"' in resp.text
    assert 'id="dash-table"' in resp.text
    assert "/static/js/dashboard.js" in resp.text
    assert "/static/css/app.css" in resp.text


def test_retired_upload_page_is_gone(client: TestClient) -> None:
    # P11.5 deleted the v2 CSV upload screen with the rest of the in-memory job machinery.
    assert client.get("/upload").status_code == 404
    assert 'data-nav="upload">' not in client.get("/").text


def test_navbar_has_logout(client: TestClient) -> None:
    resp = client.get("/")
    assert 'action="/logout"' in resp.text


def test_audit_page_renders(client: TestClient) -> None:
    resp = client.get("/audit")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert 'id="audit-app"' in resp.text
    assert 'id="audit-table"' in resp.text
    assert 'id="audit-search"' in resp.text
    assert 'id="audit-detail"' in resp.text


def test_cohort_page_renders(client: TestClient) -> None:
    resp = client.get("/cohorts")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert 'id="cohort-app"' in resp.text
    assert 'id="cap-honors"' in resp.text
    assert 'id="cap-intensive"' in resp.text
    assert 'id="cap-regular"' in resp.text
    assert 'id="reupload-form"' in resp.text


def test_pages_never_contain_pii_placeholders(client: TestClient) -> None:
    # Templates are data-free shells: no template should render applicant-looking content.
    for path in ("/", "/audit", "/cohorts"):
        text = client.get(path).text
        assert "@example.com" not in text  # no baked-in records


# --------------------------------------------------------------------------------------------
# Static assets
# --------------------------------------------------------------------------------------------


def test_static_css_serves(client: TestClient) -> None:
    resp = client.get("/static/css/app.css")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")
    assert "--navy" in resp.text  # the theme variables are present


@pytest.mark.parametrize(
    "path",
    [
        "/static/js/common.js",
        "/static/js/audit.js",
        "/static/js/cohort.js",
        "/static/js/dashboard.js",
    ],
)
def test_static_js_serves(client: TestClient, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 200


def test_static_logo_serves(client: TestClient) -> None:
    resp = client.get("/static/logo.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert len(resp.content) > 1000


def test_static_unknown_is_404_not_500(client: TestClient) -> None:
    resp = client.get("/static/does-not-exist.css")
    assert resp.status_code == 404


# --------------------------------------------------------------------------------------------
# Wiring regression — UI additions must not disturb the JSON API surface
# --------------------------------------------------------------------------------------------


def test_health_still_ok_with_ui_mounted(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"  # queue detail is covered by tests/api/test_health.py
