"""P2/P10 webhook receiver tests — auth vectors, contract validation, idempotent ACK.

No real database: the store boundary (``db.upsert_application`` / ``db.add_event``) is
monkeypatched with spies, which is exactly what proves PRD v3 invariant #7 — on every
4xx path the spies must never fire. Synthetic data only.
"""

from __future__ import annotations

import json

import pytest
from api.main import create_app
from api.webhook_auth import SECRET_HEADER, WebhookAuthError, verify_webhook
from fastapi.testclient import TestClient

from api import webhooks as webhooks_mod
from srip_filter.config import AppConfig
from srip_filter.llm.client import FakeLLMClient
from tests.api.conftest import raw_asgi_post
from tests.live_payload import make_payload

SECRET = "test-webhook-secret"
PREVIOUS = "rotated-out-secret"


# ------------------------------------------------------------------------------------------------
# verify_webhook — pure static-secret vectors
# ------------------------------------------------------------------------------------------------


def _headers(body: bytes = b"", *, secret: str = SECRET) -> dict[str, str]:
    """Body is ignored — the live scheme is a static header, not a body-bound signature."""
    return {SECRET_HEADER: secret}


def test_valid_secret_passes() -> None:
    verify_webhook(SECRET, (SECRET,))


def test_missing_header_rejected() -> None:
    for value in (None, ""):
        with pytest.raises(WebhookAuthError) as err:
            verify_webhook(value, (SECRET,))
        assert err.value.reason == "missing_header"


def test_wrong_secret_rejected() -> None:
    with pytest.raises(WebhookAuthError) as err:
        verify_webhook("not-the-secret", (SECRET,))
    assert err.value.reason == "bad_secret"


def test_previous_secret_still_accepted_during_rotation() -> None:
    verify_webhook(PREVIOUS, (SECRET, PREVIOUS))


def test_no_secrets_configured_rejects_everything() -> None:
    """Fail closed: their dispatcher omits the header entirely when its env var is unset."""
    with pytest.raises(WebhookAuthError) as err:
        verify_webhook(SECRET, ())
    assert err.value.reason == "no_secrets_configured"


@pytest.mark.parametrize("hostile", ["caf\xe9", "\xff" * 32, "sécret", "\x80"])
def test_non_ascii_header_is_a_clean_rejection_not_a_crash(hostile: str) -> None:
    """A header byte above 0x7F must reject, never raise.

    ``hmac.compare_digest`` rejects non-ASCII ``str`` with a TypeError, and ASGI servers
    decode header values latin-1 — so before the bytes-first comparison this raised
    straight past the endpoint's ``except WebhookAuthError`` into a 500.
    """
    with pytest.raises(WebhookAuthError) as err:
        verify_webhook(hostile, (SECRET, PREVIOUS))
    assert err.value.reason == "bad_secret"


def test_non_ascii_secret_still_matches_itself() -> None:
    """The encoding fix must not break a configured secret that happens to be non-ASCII.

    latin-1 recovers the exact bytes the server read, which for a utf-8 client is the
    utf-8 encoding of the configured value — so the round trip still matches.
    """
    secret = "sécret-ünicode"
    as_server_reads_it = secret.encode("utf-8").decode("latin-1")
    verify_webhook(as_server_reads_it, (secret,))


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
    return make_payload(**overrides)


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
    assert stored["grade"] is True  # "essays" in ats_run
    assert stored["submission_id"] == payload["submission_id"]
    assert stored["payload"] == payload  # raw delivered dict is what's persisted
    assert spies.events == [("delivery", payload["submission_id"])]


def test_unchanged_redelivery_reports_unchanged(client: TestClient, spies: _Spies) -> None:
    spies.upsert_result = "unchanged"
    resp = _post(client, _essays_payload())
    assert resp.status_code == 202
    assert resp.json() == {"status": "unchanged"}


def test_delivery_without_essays_is_stored_not_queued(
    client: TestClient, spies: _Spies
) -> None:
    """ats_run without "essays" ⇒ stored terminal, so no drain ever spends tokens on it."""
    resp = _post(client, _essays_payload(ats_run=["resume"]))
    assert resp.status_code == 202
    assert spies.upserts[0]["grade"] is False


def test_finaid_payload_is_accepted_and_stored(client: TestClient, spies: _Spies) -> None:
    """Finaid is stored, never scored (owner, 2026-07-21) — it must not 422 any more."""
    resp = _post(client, _essays_payload(
        is_finaid=True,
        ats_run=["essays", "finaid"],
        finaid={"sat_score": "1500/1600",
                "test_score_scale": {"SAT": 1600, "PSAT": 1600, "ACT": 36},
                "fin_aid_essays": [{"question": "Need?", "answer": "text"}]},
    ))
    assert resp.status_code == 202
    assert spies.upserts[0]["payload"]["finaid"]["sat_score"] == "1500/1600"


def test_unsigned_tampered_stale_all_401_and_touch_nothing(
    client: TestClient, spies: _Spies
) -> None:
    """PRD v3 invariant #7 — the auth failure matrix writes no row and no event."""
    payload = _essays_payload()

    cases = [
        {},  # no header at all — what their dispatcher sends when the env var is unset
        {SECRET_HEADER: ""},  # empty header
        {SECRET_HEADER: "wrong-secret"},
        {SECRET_HEADER: SECRET + "x"},  # near-miss (constant-time compare)
    ]
    for hdrs in cases:
        resp = _post(client, payload, headers=hdrs)
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Invalid credentials."}  # generic, reason not leaked
    assert spies.upserts == []
    assert spies.events == []


def test_hostile_header_bytes_401_not_500(client: TestClient, spies: _Spies) -> None:
    """End to end at the ASGI layer: a raw high byte in the secret header is a 401.

    Driven through :func:`raw_asgi_post` because httpx will not send this — see its
    docstring. The endpoint's own ``except WebhookAuthError`` cannot catch a TypeError,
    so before the fix this was an unauthenticated 500 with a stack trace.
    """
    status = raw_asgi_post(
        client.app,
        "/webhooks/applications",
        [(b"x-ats-secret", b"caf\xe9"), (b"content-type", b"application/json")],
        body=json.dumps(_essays_payload()).encode(),
    )
    assert status == 401
    assert (spies.upserts, spies.events) == ([], [])  # invariant #7 holds on this path too


def test_signed_test_ping_200_and_no_row(client: TestClient, spies: _Spies) -> None:
    resp = _post(client, {"_test": True, "submission_id": "ats-connectivity-test"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert spies.upserts == []


def test_unsigned_test_ping_is_401(client: TestClient, spies: _Spies) -> None:
    resp = _post(client, {"_test": True}, headers={})
    assert resp.status_code == 401
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


def test_unmodelled_fields_are_ignored_not_rejected(
    client: TestClient, spies: _Spies
) -> None:
    """referral / time_spent_seconds / a legacy ats_mode must never bounce a real payload."""
    resp = _post(client, _essays_payload(
        referral="Someone", referral_code="ABC", time_spent_seconds=900, ats_mode="essays",
    ))
    assert resp.status_code == 202


def test_test_ping_short_circuits_before_uuid_validation(
    client: TestClient, spies: _Spies
) -> None:
    """Their Test button sends submission_id="ats-connectivity-test" — not a UUID."""
    resp = _post(client, {"_test": True, "submission_id": "ats-connectivity-test",
                          "cohort_name": "TEST", "form_data": {}})
    assert resp.status_code == 200
    assert spies.upserts == []
