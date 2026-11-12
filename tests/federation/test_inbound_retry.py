"""An inbound event whose handler failed for a passing reason is tried
again — by this household, once per failed handler, a bounded number of
times — on every transport; a replayed envelope is still refused."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sqlite3

import pytest

from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.federation_retention import SHORT_LIVED
from socialhome.federation.inbound_retry import (
    NEVER_RETRIED,
    PERMANENT_ERRORS,
    RETRY_DELAYS_S,
    InboundRetry,
)
from socialhome.federation.inbound_validator import TRANSPORT_GFS_RELAY
from tests.federation.test_federation_service import _make_frame, _paired
from tests.services.test_gfs_relay_inbound import _multi_frame, _MultiHousehold

DM = FederationEventType.DM_MESSAGE
POST = FederationEventType.SPACE_POST_CREATED
RETRY_LOG = "socialhome.federation.inbound_retry"


def _event(
    event_type: FederationEventType = DM,
    *,
    sender: str = "peer",
    payload: dict | None = None,
) -> FederationEvent:
    return FederationEvent(
        msg_id="m1",
        event_type=event_type,
        from_instance=sender,
        to_instance="self",
        timestamp="2026-01-01T00:00:00+00:00",
        payload=payload if payload is not None else {"text": "hi"},
    )


class _Flaky:
    """A handler that raises ``fail`` times, then applies the event."""

    def __init__(
        self, fail: int = 1, exc: type[Exception] = sqlite3.OperationalError
    ) -> None:
        self.fail = fail
        self.exc = exc
        self.calls = 0
        self.applied: list[FederationEvent] = []

    async def __call__(self, event: FederationEvent) -> None:
        self.calls += 1
        if self.calls <= self.fail:
            raise self.exc("database is locked")
        self.applied.append(event)


async def _first_try(retry: InboundRetry, handler: _Flaky, event: FederationEvent):
    """What the registry does: run the handler, hand a failure to the hold."""
    try:
        await handler(event)
    except Exception as exc:  # noqa: BLE001 — the registry catches the same
        return retry.hold(event, handler, exc)
    return None


async def _settle(retry: InboundRetry) -> None:
    for _ in range(200):
        if not len(retry):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("retry hold never drained")


def _fast(**kw) -> InboundRetry:
    return InboundRetry(delays=(0.001, 0.001, 0.001), **kw)


# ─── The hold ─────────────────────────────────────────────────────────────


async def test_a_handler_that_fails_once_applies_the_event_exactly_once():
    retry, handler = _fast(), _Flaky(fail=1)
    assert await _first_try(retry, handler, _event()) is True
    await _settle(retry)
    assert len(handler.applied) == 1
    assert handler.calls == 2


@pytest.mark.parametrize("exc", PERMANENT_ERRORS)
async def test_a_failure_that_would_repeat_is_not_tried_again(exc):
    retry, handler = _fast(), _Flaky(fail=1, exc=exc)
    assert await _first_try(retry, handler, _event()) is False
    assert len(retry) == 0
    assert handler.calls == 1


async def test_a_refusal_on_a_later_try_ends_the_tries(caplog):
    """Transient first, then a refusal: no third try."""
    retry = _fast()
    calls = 0

    async def handler(event):
        nonlocal calls
        calls += 1
        raise ValueError("refused")

    with caplog.at_level(logging.WARNING, logger=RETRY_LOG):
        assert retry.hold(_event(), handler, RuntimeError("not attached"))
        await _settle(retry)
    assert calls == 1


async def test_a_poison_event_stops_after_the_last_try_with_one_warning(caplog):
    retry, handler = _fast(), _Flaky(fail=99)
    with caplog.at_level(logging.WARNING, logger=RETRY_LOG):
        assert await _first_try(retry, handler, _event()) is True
        await _settle(retry)
    assert handler.calls == 1 + len(RETRY_DELAYS_S) == 4
    assert handler.applied == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "after 4 tries" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None


@pytest.mark.parametrize(
    "event_type",
    [
        FederationEventType.CALL_OFFER,
        FederationEventType.DM_USER_TYPING,
        FederationEventType.FEDERATION_RTC_ICE,
        FederationEventType.SPACE_SYNC_CHUNK,
        FederationEventType.DM_HISTORY_CHUNK,
    ],
)
async def test_signalling_and_sync_streams_are_never_tried_again(event_type):
    assert SHORT_LIVED <= NEVER_RETRIED
    retry, handler = _fast(), _Flaky(fail=1)
    assert await _first_try(retry, handler, _event(event_type)) is False
    assert handler.calls == 1


async def test_an_event_the_gates_no_longer_allow_is_dropped():
    """A ban that landed between the failure and the second try."""
    asked: list[FederationEvent] = []

    async def gate(event):
        asked.append(event)
        return False

    retry, handler = _fast(gate=gate), _Flaky(fail=1)
    event = _event()
    assert await _first_try(retry, handler, event) is True
    await _settle(retry)
    assert asked == [event]
    assert handler.calls == 1


async def test_the_hold_is_bounded_in_entries_per_sender_and_bytes(caplog):
    slow = (60.0,)

    async def boom(event):
        raise OSError

    by_count = InboundRetry(delays=slow, max_entries=2)
    assert by_count.hold(_event(sender="a"), boom, OSError())
    assert by_count.hold(_event(sender="b"), boom, OSError())
    with caplog.at_level(logging.WARNING, logger=RETRY_LOG):
        assert not by_count.hold(_event(sender="c"), boom, OSError())
    assert "retry hold is full" in caplog.text

    by_sender = InboundRetry(delays=slow, max_per_sender=1)
    assert by_sender.hold(_event(sender="a"), boom, OSError())
    assert not by_sender.hold(_event(sender="a"), boom, OSError())
    assert by_sender.hold(_event(sender="b"), boom, OSError())

    by_bytes = InboundRetry(delays=slow, max_bytes=100)
    assert by_bytes.hold(_event(payload={"t": "x" * 50}), boom, OSError())
    assert not by_bytes.hold(_event(payload={"t": "x" * 50}), boom, OSError())
    media = dataclasses.replace(_event(payload={}), media_bytes=b"x" * 101)
    assert not InboundRetry(delays=slow, max_bytes=100).hold(media, boom, OSError())

    for retry in (by_count, by_sender, by_bytes):
        await retry.stop()
        assert len(retry) == 0


async def test_a_finished_try_frees_its_slot_and_bytes():
    retry = _fast(max_entries=1, max_per_sender=1, max_bytes=64)
    for _ in range(3):
        handler = _Flaky(fail=1)
        assert await _first_try(retry, handler, _event()) is True
        await _settle(retry)
        assert len(handler.applied) == 1


# ─── Stop / restart ───────────────────────────────────────────────────────


async def test_stop_gives_a_held_event_one_last_try_now():
    retry, handler = InboundRetry(delays=(60.0, 60.0)), _Flaky(fail=1)
    assert await _first_try(retry, handler, _event()) is True
    await retry.stop()
    assert len(handler.applied) == 1
    assert len(retry) == 0


async def test_stop_drops_an_event_that_still_fails_with_one_warning(caplog):
    retry, handler = InboundRetry(delays=(60.0, 60.0)), _Flaky(fail=99)
    assert await _first_try(retry, handler, _event()) is True
    with caplog.at_level(logging.WARNING, logger=RETRY_LOG):
        await retry.stop()
    assert handler.calls == 2  # the first try and the last one — no more
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


async def test_nothing_is_held_after_stop_and_a_new_process_holds_nothing():
    """In memory on purpose: a restart starts empty (as before this hold
    existed, the event is then lost unless a sync carries it)."""
    retry = _fast()
    await retry.stop()
    await retry.stop()  # idempotent
    handler = _Flaky(fail=1)
    assert await _first_try(retry, handler, _event()) is False
    assert len(InboundRetry()) == 0


# ─── Every transport ends in the same dispatch ────────────────────────────


def _fast_service_retry(svc) -> None:
    svc._inbound_retry._delays = (0.001, 0.001, 0.001)


def _flaky_paired(event_type=FederationEventType.DM_MEDIA_BLOB, **kw):
    p = _paired()
    handler = _Flaky(**kw)
    registry = p.svc_b._event_registry
    registry.clear()
    registry.register(event_type, handler)
    _fast_service_retry(p.svc_b)
    return p, handler


async def test_https_inbox_a_failed_handler_is_tried_again_and_a_replay_refused():
    p, handler = _flaky_paired()
    body, _ = _make_frame(p)
    inbox = p.repo_b._instances[p.a_id].local_inbox_id
    assert await p.svc_b.handle_inbound_envelope(inbox, body) == {"status": "ok"}
    await _settle(p.svc_b._inbound_retry)
    assert len(handler.applied) == 1
    # The same envelope again — an attacker's replay or the sender's own
    # re-send — is refused, held event or not.
    with pytest.raises(ValueError, match="Replay detected"):
        await p.svc_b.handle_inbound_envelope(inbox, body)
    assert len(handler.applied) == 1


async def test_a_replay_is_refused_while_the_event_is_still_held():
    p, handler = _flaky_paired(fail=99)
    p.svc_b._inbound_retry._delays = (60.0,)
    body, _ = _make_frame(p)
    inbox = p.repo_b._instances[p.a_id].local_inbox_id
    await p.svc_b.handle_inbound_envelope(inbox, body)
    assert len(p.svc_b._inbound_retry) == 1
    with pytest.raises(ValueError, match="Replay detected"):
        await p.svc_b.handle_inbound_envelope(inbox, body)
    assert handler.calls == 1
    await p.svc_b.stop()


async def test_datachannel_a_failed_handler_is_tried_again():
    p, handler = _flaky_paired()
    body, _ = _make_frame(p)
    assert await p.svc_b.handle_inbound_rtc(p.a_id, body) == {"status": "ok"}
    await _settle(p.svc_b._inbound_retry)
    assert len(handler.applied) == 1
    assert handler.calls == 2


async def test_datachannel_media_frame_keeps_its_bytes_for_the_second_try():
    p, handler = _flaky_paired()
    header, chunk = _make_frame(p, raw=b"picture bytes")
    await p.svc_b.handle_inbound_media_frame(p.a_id, header, chunk)
    await _settle(p.svc_b._inbound_retry)
    [event] = handler.applied
    assert event.media_bytes == b"picture bytes"


async def test_gfs_relay_a_failed_handler_is_tried_again():
    p, handler = _flaky_paired()
    p.repo_b._instances[p.a_id] = dataclasses.replace(
        p.repo_b._instances[p.a_id], gfs_relay=True
    )
    body, _ = _make_frame(p)
    res = await p.svc_b.handle_inbound_rtc(p.a_id, body, transport=TRANSPORT_GFS_RELAY)
    assert res == {"status": "ok"}
    await _settle(p.svc_b._inbound_retry)
    assert len(handler.applied) == 1


async def test_gfs_mailbox_multi_recipient_frame_a_failed_handler_is_tried_again():
    h = _MultiHousehold()
    handler = _Flaky()
    h.federation._event_registry.clear()
    h.federation._event_registry.register(POST, handler)
    _fast_service_retry(h.federation)
    sender, enc = await h.add_sender()
    frame, env, _ = _multi_frame(h, sender, enc, to=h.audience)
    assert await h.frame(frame) == {"status": "ok"}
    await _settle(h.federation._inbound_retry)
    [event] = handler.applied
    assert event.msg_id == env["msg_id"]
    with pytest.raises(ValueError, match="Replay detected"):
        await h.frame(frame)
    assert handler.calls == 2


async def test_mesh_routed_inner_event_a_failed_handler_is_tried_again():
    """``SpaceRoutedHandler`` hands the unwrapped inner event to
    ``FederationService._dispatch_event`` — the same dispatch."""
    p, handler = _flaky_paired(DM)
    inner = dataclasses.replace(
        _event(DM, sender=p.a_id), routed_path=[p.a_id, "relay", p.b_id]
    )
    await p.svc_b._dispatch_event(inner)
    await _settle(p.svc_b._inbound_retry)
    assert handler.applied == [inner]


async def test_only_the_handler_that_failed_runs_again():
    p, flaky = _flaky_paired()
    steady = _Flaky(fail=0)
    p.svc_b._event_registry.register(FederationEventType.DM_MEDIA_BLOB, steady)
    body, _ = _make_frame(p)
    await p.svc_b.handle_inbound_rtc(p.a_id, body)
    await _settle(p.svc_b._inbound_retry)
    assert len(flaky.applied) == 1
    assert steady.calls == 1


async def test_a_refusal_raised_by_a_handler_is_logged_and_not_retried(caplog):
    p, handler = _flaky_paired(exc=ValueError)
    body, _ = _make_frame(p)
    with caplog.at_level(logging.ERROR):
        await p.svc_b.handle_inbound_rtc(p.a_id, body)
    assert len(p.svc_b._inbound_retry) == 0
    assert handler.calls == 1
    assert "failed for" in caplog.text


async def test_a_sender_banned_meanwhile_is_not_applied_on_the_second_try():
    p, handler = _flaky_paired(FederationEventType.SPACE_MEDIA_BLOB)
    header, chunk = _make_frame(
        p, event_type=FederationEventType.SPACE_MEDIA_BLOB, space_id="sp-1"
    )
    p.svc_b._inbound_retry._delays = (0.05,)
    await p.svc_b.handle_inbound_media_frame(p.a_id, header, chunk)
    assert len(p.svc_b._inbound_retry) == 1

    async def banned(space_id, instance_id):
        return (space_id, instance_id) == ("sp-1", p.a_id)

    p.repo_b.is_instance_banned_from_space = banned
    await _settle(p.svc_b._inbound_retry)
    assert handler.calls == 1
    assert handler.applied == []


async def test_service_stop_gives_held_events_their_last_try():
    p, handler = _flaky_paired()
    p.svc_b._inbound_retry._delays = (60.0,)
    body, _ = _make_frame(p)
    await p.svc_b.handle_inbound_rtc(p.a_id, body)
    await p.svc_b.stop()
    assert len(handler.applied) == 1
