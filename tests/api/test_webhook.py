"""P2 webhook receiver tests — signature vectors, contract validation, idempotent ACK.

No real database: the store boundary (``db.upsert_application`` / ``db.add_event``) is
monkeypatched with spies, which is exactly what proves PRD v3 invariant #7 — on every
4xx path the spies must never fire. HMAC math is exercised for real via ``sign``.
Synthetic data only.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from api.main import create_app
from api.webhook_auth import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookAuthError,
    sign,
    verify_webhook,
)
from fastapi.testclient import TestClient

from api import webhooks as webhooks_mod
from srip_filter.config import AppConfig
from srip_filter.llm.client import FakeLLMClient

SECRET = "test-webhook-secret"
PREVIOUS = "rotated-out-secret"


# ------------------------------------------------------------------------------------------------
# verify_webhook — pure signature vectors
# ------------------------------------------------------------------------------------------------


def _headers(body: bytes, *, secret: str = SECRET, ts: float | None = None) -> dict[str, str]:
    stamp = str(int(time.time() if ts is None else ts))
    return {TIMESTAMP_HEADER: stamp, SIGNATURE_HEADER: sign(secret, stamp, body)}


def test_valid_signature_passes() -> None:
    body = b'{"x":1}'
    h = _headers(body)
    verify_webhook(
        h[TIMESTAMP_HEADER], h[SIGNATURE_HEADER], body, (SECRET,), max_skew_seconds=300
    )


@pytest.mark.parametrize(
    ("ts", "sig", "reason"),
    [
        (None, None, "missing_headers"),
        ("123", None, "missing_headers"),
        ("not-a-number", "deadbeef", "bad_timestamp"),
    ],
)
def test_missing_or_garbled_headers(ts: str | None, sig: str | None, reason: str) -> None:
    with pytest.raises(WebhookAuthError) as err:
        verify_webhook(ts, sig, b"{}", (SECRET,), max_skew_seconds=300)
    assert err.value.reason == reason


def test_stale_and_future_timestamps_rejected() -> None:
    body = b"{}"
    for offset in (-301, 301):
        h = _headers(body, ts=time.time() + offset)
        with pytest.raises(WebhookAuthError) as err:
            verify_webhook(
                h[TIMESTAMP_HEADER], h[SIGNATURE_HEADER], body, (SECRET,), max_skew_seconds=300
            )
        assert err.value.reason == "stale_timestamp"


def test_tampered_body_rejected() -> None:
    h = _headers(b'{"gpa":"4.0"}')
    with pytest.raises(WebhookAuthError) as err:
        verify_webhook(
            h[TIMESTAMP_HEADER], h[SIGNATURE_HEADER], b'{"gpa":"2.0"}', (SECRET,),
            max_skew_seconds=300,
        )
    assert err.value.reason == "bad_signature"


def test_previous_secret_accepted_during_rotation() -> None:
    body = b"{}"
    h = _headers(body, secret=PREVIOUS)
    verify_webhook(
        h[TIMESTAMP_HEADER], h[SIGNATURE_HEADER], body, (SECRET, PREVIOUS), max_skew_seconds=300
    )


def test_no_secrets_configured_never_passes() -> None:
    body = b"{}"
    h = _headers(body)
    with pytest.raises(WebhookAuthError) as err:
        verify_webhook(h[TIMESTAMP_HEADER], h[SIGNATURE_HEADER], body, (), max_skew_seconds=300)
    assert err.value.reason == "no_secrets_configured"


# ------------------------------------------------------------------------------------------------
# Endpoint — spies + TestClient
# ------------------------------------------------------------------------------------------------


class _Spies:
    """Records store-boundary calls; the webhook route must not reach these on any 4xx."""

    def __init__(self) -> None:
        self.upserts: list[dict] = []
        self.events: list[tuple[str, str | None]] = []
        self.upsert_result = "accepted"

    async def upsert_application(self, pool, **kwargs):
        self.upserts.append(kwargs)
        return self.upsert_result

    async def add_event(self, pool, kind, *, submission_id=None, details=None):
        self.events.append((kind, submission_id))


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch) -> _Spies:
    s = _Spies()
    monkeypatch.setattr(webhooks_mod.dbmod, "upsert_application", s.upsert_application)
    monkeypatch.setattr(webhooks_mod.dbmod, "add_event", s.add_event)
    return s


@pytest.fixture
def client() -> TestClient:
    cfg = AppConfig()
    app = create_app(
        config=cfg,
        client=FakeLLMClient(cfg, lambda *a, **k: None),
        db_pool=object(),  # sentinel — store functions are monkeypatched
        webhook_secrets=(SECRET, PREVIOUS),
    )
    return TestClient(app)


def _essays_payload(**overrides) -> dict:
    base = {
        "ats_mode": "essays",
        "submission_id": str(uuid.uuid4()),
        "user_email": "synthetic@example.com",
        "student_name": "Syn Thetic",
        "cohort_name": "su26-cs",
        "gpa": {"unweighted": "3.8 / 4.0", "weighted": None},
        "gpa_explanation": "",
        "required_essays": [
            {"question": "Why?", "answer": "Because.", "min_words": 100, "max_words": 350}
        ],
        "optional_essays": [],
    }
    base.update(overrides)
    return base


def _post(client: TestClient, payload: dict, *, secret: str = SECRET, headers=None):
    body = json.dumps(payload).encode()
    hdrs = headers if headers is not None else _headers(body, secret=secret)
    return client.post(
        "/webhooks/applications",
        content=body,
        headers={**hdrs, "Content-Type": "application/json"},
    )


def test_signed_essays_delivery_accepted(client: TestClient, spies: _Spies) -> None:
    payload = _essays_payload()
    resp = _post(client, payload)
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}
    assert len(spies.upserts) == 1
    stored = spies.upserts[0]
    assert stored["mode"] == "essays"
    assert stored["submission_id"] == payload["submission_id"]
    assert stored["payload"] == payload  # raw delivered dict is what's persisted
    assert spies.events == [("delivery", payload["submission_id"])]


def test_unchanged_redelivery_reports_unchanged(client: TestClient, spies: _Spies) -> None:
    spies.upsert_result = "unchanged"
    resp = _post(client, _essays_payload())
    assert resp.status_code == 202
    assert resp.json() == {"status": "unchanged"}


def test_resume_mode_accepted(client: TestClient, spies: _Spies) -> None:
    resp = _post(
        client,
        {
            "ats_mode": "resume",
            "submission_id": str(uuid.uuid4()),
            "user_email": "synthetic@example.com",
            "resume_url": "https://r2.example.com/resume/x.pdf",
        },
    )
    assert resp.status_code == 202
    assert spies.upserts[0]["mode"] == "resume"


def test_unsigned_tampered_stale_all_401_and_touch_nothing(
    client: TestClient, spies: _Spies
) -> None:
    """PRD v3 invariant #7 — the auth failure matrix writes no row and no event."""
    payload = _essays_payload()
    body = json.dumps(payload).encode()

    cases = [
        {},  # unsigned
        {TIMESTAMP_HEADER: str(int(time.time()))},  # signature missing
        _headers(body, secret="wrong-secret"),  # bad secret
        _headers(body, ts=time.time() - 3600),  # stale
        {  # tampered: signed over different bytes
            **_headers(b'{"other":"bytes"}'),
        },
    ]
    for hdrs in cases:
        resp = _post(client, payload, headers=hdrs)
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Invalid signature."}  # generic, reason not leaked
    assert spies.upserts == []
    assert spies.events == []


def test_signed_test_ping_200_and_no_row(client: TestClient, spies: _Spies) -> None:
    resp = _post(client, {"_test": True, "submission_id": "ats-connectivity-test"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert spies.upserts == []


def test_unsigned_test_ping_is_401(client: TestClient, spies: _Spies) -> None:
    resp = _post(client, {"_test": True}, headers={})
    assert resp.status_code == 401
    assert spies.upserts == []


def test_finaid_mode_rejected_as_unsupported(client: TestClient, spies: _Spies) -> None:
    resp = _post(client, {"ats_mode": "finaid", "submission_id": str(uuid.uuid4())})
    assert resp.status_code == 422
    assert "finaid" in resp.json()["detail"]
    assert spies.upserts == []


def test_malformed_json_and_non_object_422(client: TestClient, spies: _Spies) -> None:
    for raw in (b"not json at all", b'["a","list"]'):
        hdrs = _headers(raw)
        resp = client.post(
            "/webhooks/applications",
            content=raw,
            headers={**hdrs, "Content-Type": "application/json"},
        )
        assert resp.status_code == 422
    assert spies.upserts == []


def test_invalid_submission_id_422_without_echoing_values(
    client: TestClient, spies: _Spies
) -> None:
    resp = _post(client, _essays_payload(submission_id="not-a-uuid"))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any("submission_id" in e["loc"] for e in detail)
    # PII discipline: the response must not echo input values back.
    assert "not-a-uuid" not in json.dumps(detail)
    assert spies.upserts == []


def test_oversize_body_413(client: TestClient, spies: _Spies) -> None:
    huge = _essays_payload(padding="x" * 1_100_000)
    resp = _post(client, huge)
    assert resp.status_code == 413
    assert spies.upserts == []


def test_gpa_tolerates_current_site_string_format(client: TestClient, spies: _Spies) -> None:
    # Until WEBSITE_ASKS #3 lands the site sends a joined string; the edge must accept it.
    resp = _post(client, _essays_payload(gpa="3.8 / 4.0"))
    assert resp.status_code == 202
