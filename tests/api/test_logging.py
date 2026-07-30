"""Core-logger wiring — ``srip_filter.*`` records must actually reach a handler.

Regression test for a silent-observability bug: uvicorn and Vercel configure only their own
loggers, so our records propagated to a bare root logger and were dropped. The pacing and
rate-limit-retry lines in ``llm/client.py`` exist specifically to make a 429 storm visible,
and a 42-minute paced calibration run (2026-07-29) emitted none of them. ``/health`` reports
a *dead* drain, not a degraded one, so those lines are the only warning of a degraded one.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable

import pytest
from api.main import _wire_core_logging


@pytest.fixture
def wire_onto_bare_root(monkeypatch: pytest.MonkeyPatch) -> Callable[[], io.StringIO]:
    """Return a callable that wires logging as if no host had configured any.

    The clearing must happen inside the test body, not here: pytest's logging plugin adds a
    fresh root handler for each test *phase*, so anything removed during fixture setup is back
    by the time the test runs — which would make ``basicConfig`` a no-op and the assertions
    vacuous. Handlers are restored on teardown either way.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    stream = io.StringIO()

    def wire() -> io.StringIO:
        root.handlers.clear()
        monkeypatch.setattr("sys.stderr", stream)
        _wire_core_logging()
        return stream

    try:
        yield wire
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_core_logger_records_reach_a_handler(
    wire_onto_bare_root: Callable[[], io.StringIO],
) -> None:
    """The real pacing call site emits something a human can see."""
    stream = wire_onto_bare_root()

    logging.getLogger("srip_filter.llm.client").info(
        "LLM task=%s paced %.1fs to stay under TPM", "task_d", 3.5
    )

    out = stream.getvalue()
    assert "paced 3.5s" in out
    assert "srip_filter.llm.client" in out


def test_warning_level_records_reach_a_handler(
    wire_onto_bare_root: Callable[[], io.StringIO],
) -> None:
    """The retry warning — the actual 429-storm signal — is not filtered out."""
    stream = wire_onto_bare_root()

    logging.getLogger("srip_filter.llm.client").warning(
        "LLM task=%s attempt=%d/%d failed (%s): %s",
        "task_d",
        1,
        6,
        "transient",
        "RateLimitError",
    )

    assert "RateLimitError" in stream.getvalue()


def test_wiring_does_not_hijack_logging_the_host_already_configured() -> None:
    """With handlers already installed (a host's dictConfig, or pytest), it is a no-op.

    Guards against double-logging every line and against stomping on a deployment that
    configures logging itself.
    """
    root = logging.getLogger()
    before = root.handlers[:]
    assert before, "precondition: pytest configures root handlers"

    _wire_core_logging()

    assert root.handlers == before
