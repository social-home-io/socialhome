"""§24.11 — the gated space id is the one a content handler may write."""

from __future__ import annotations

import logging

import pytest

from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation.space_scope import (
    log_cross_space_refusal,
    log_not_applied,
    resolve_space_id,
)


def _event(*, space_id: str | None, payload: dict) -> FederationEvent:
    return FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_POST_DELETED,
        from_instance="inst-a",
        to_instance="inst-b",
        timestamp="2026-01-01T00:00:00+00:00",
        payload=payload,
        space_id=space_id,
    )


def test_routing_field_wins_over_absent_payload_copy() -> None:
    assert resolve_space_id(_event(space_id="space-a", payload={})) == "space-a"


def test_matching_payload_copy_is_accepted() -> None:
    ev = _event(space_id="space-a", payload={"space_id": "space-a"})
    assert resolve_space_id(ev) == "space-a"


def test_payload_is_the_fallback_when_routing_field_absent() -> None:
    ev = _event(space_id=None, payload={"space_id": "space-b"})
    assert resolve_space_id(ev) == "space-b"


def test_mismatch_between_routing_and_payload_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ev = _event(space_id="space-a", payload={"space_id": "space-b"})
    with caplog.at_level(logging.WARNING):
        assert resolve_space_id(ev) is None
    assert "does not match payload space" in caplog.text


def test_no_space_id_anywhere_is_refused() -> None:
    assert resolve_space_id(_event(space_id=None, payload={})) is None
    assert resolve_space_id(_event(space_id="", payload={"space_id": ""})) is None


def test_cross_space_refusal_logs_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ev = _event(space_id="space-a", payload={})
    with caplog.at_level(logging.WARNING):
        log_cross_space_refusal(ev, space_id="space-a", what="post", row_id="p1")
    assert "post p1 is not in space space-a" in caplog.text
    assert caplog.records[0].levelno == logging.WARNING


def test_a_benign_no_op_is_logged_at_debug_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ev = _event(space_id="space-a", payload={})
    with caplog.at_level(logging.DEBUG):
        log_not_applied(ev, what="comment", row_id="c1", reason="already deleted")
    records = [r for r in caplog.records if "c1" in r.getMessage()]
    assert records and all(r.levelno == logging.DEBUG for r in records)
    assert "already deleted" in records[0].getMessage()
