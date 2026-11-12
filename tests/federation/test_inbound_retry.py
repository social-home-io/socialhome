"""An inbound event whose handler failed for a passing reason is tried
again — by this household, a bounded number of times, with everything else
that household sent waiting behind it in order — on every transport; a
replayed envelope is still refused."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sqlite3

import orjson
import pytest

from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    PairingStatus,
)
from socialhome.domain.federation_retention import SHORT_LIVED
from socialhome.domain.space import SpacePermissionError
from socialhome.federation.event_dispatch_registry import EventDispatchRegistry
from socialhome.federation.inbound_retry import (
    NEVER_RETRIED,
    RETRY_DELAYS_S,
    InboundRetry,
    is_transient,
)
from socialhome.federation.inbound_validator import TRANSPORT_GFS_RELAY
from socialhome.services.call_service import CallAlreadyAnsweredError
from tests.federation.test_federation_service import _make_frame, _paired
from tests.federation.test_routed_envelope import _build_chain, _mint_target_eph
from tests.services.test_gfs_relay_inbound import _multi_frame, _MultiHousehold

DM = FederationEventType.DM_MESSAGE
DM_DELETED = FederationEventType.DM_MESSAGE_DELETED
ROLE = FederationEventType.SPACE_MEMBER_ROLE_CHANGED
POST = FederationEventType.SPACE_POST_CREATED
RETRY_LOG = "socialhome.federation.inbound_retry"
FAST = (0.001, 0.001, 0.001)
SLOW = (60.0,)


def _event(
    event_type: FederationEventType = DM,
    *,
    sender: str = "peer",
    payload: dict | None = None,
    space_id: str | None = None,
) -> FederationEvent:
    return FederationEvent(
        msg_id="m1",
        event_type=event_type,
        from_instance=sender,
        to_instance="self",
        timestamp="2026-01-01T00:00:00+00:00",
        payload=payload if payload is not None else {"text": "hi"},
        space_id=space_id,
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


def _registry(*types: FederationEventType, handler=None, **kw):
    """A registry with its retry hold and ``handler`` bound to ``types``."""
    retry = InboundRetry(**{"delays": FAST, **kw})
    registry = EventDispatchRegistry(retry=retry)
    for event_type in types or (DM,):
        registry.register(event_type, handler)
    return registry, retry


async def _settle(retry: InboundRetry) -> None:
    for _ in range(400):
        if not len(retry):
            assert retry.held_bytes == 0
            return
        await asyncio.sleep(0.005)
    raise AssertionError("retry hold never drained")


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


# ─── One event ────────────────────────────────────────────────────────────


async def test_a_handler_that_fails_once_applies_the_event_exactly_once():
    handler = _Flaky(fail=1)
    registry, retry = _registry(handler=handler)
    await registry.dispatch(_event())
    assert len(retry) == 1
    await _settle(retry)
    assert len(handler.applied) == 1
    assert handler.calls == 2


@pytest.mark.parametrize(
    "exc",
    [
        ValueError,  # a refusal
        KeyError,  # what a malformed payload raises …
        TypeError,
        AttributeError,
        sqlite3.IntegrityError,  # already stored
        AssertionError,  # a bug
        RuntimeError,  # mostly "not configured" — permanent
        NotImplementedError,
        CallAlreadyAnsweredError,  # a RuntimeError that is a refusal
        SpacePermissionError,  # a domain refusal
    ],
)
async def test_a_failure_that_would_repeat_is_not_tried_again(exc, caplog):
    assert not is_transient(exc("x"))
    handler = _Flaky(fail=1, exc=exc)
    registry, retry = _registry(handler=handler)
    with caplog.at_level(logging.ERROR):
        await registry.dispatch(_event())
    assert len(retry) == 0
    assert handler.calls == 1
    assert "failed for" in caplog.text


@pytest.mark.parametrize("exc", [sqlite3.OperationalError, OSError, TimeoutError])
async def test_a_passing_failure_is_tried_again(exc):
    assert is_transient(exc("x"))
    handler = _Flaky(fail=1, exc=exc)
    registry, retry = _registry(handler=handler)
    await registry.dispatch(_event())
    await _settle(retry)
    assert len(handler.applied) == 1


async def test_a_refusal_on_a_later_try_ends_the_tries(caplog):
    calls = 0

    async def handler(event):
        nonlocal calls
        calls += 1
        raise (OSError if calls == 1 else ValueError)("x")

    registry, retry = _registry(handler=handler)
    with caplog.at_level(logging.ERROR):
        await registry.dispatch(_event())
        await _settle(retry)
    assert calls == 2
    assert "failed for" in caplog.text


async def test_a_poison_event_stops_after_the_last_try_with_one_warning(caplog):
    handler = _Flaky(fail=99)
    registry, retry = _registry(handler=handler)
    with caplog.at_level(logging.WARNING):
        await registry.dispatch(_event())
        await _settle(retry)
    assert handler.calls == 1 + len(RETRY_DELAYS_S) == 4
    assert handler.applied == []
    [warning] = _warnings(caplog)
    assert "after 4 tries" in warning.getMessage()
    assert warning.exc_info is not None


async def test_a_payload_too_deep_to_size_is_not_held(caplog):
    """orjson reads deeper nesting than it writes: the hold must refuse
    such an event, not raise into the dispatch loop."""
    deep: dict = {}
    for _ in range(300):
        deep = {"a": deep}
    handler = _Flaky(fail=1)
    registry, retry = _registry(handler=handler)
    with caplog.at_level(logging.ERROR):
        await registry.dispatch(_event(payload=deep))
    assert len(retry) == 0
    assert "failed for" in caplog.text


async def test_only_the_handler_that_failed_runs_again():
    flaky, steady = _Flaky(fail=1), _Flaky(fail=0)
    registry, retry = _registry(handler=flaky)
    registry.register(DM, steady)
    await registry.dispatch(_event())
    await _settle(retry)
    assert len(flaky.applied) == 1
    assert steady.calls == 1


async def test_an_event_the_gates_no_longer_allow_is_dropped():
    """A ban that landed between the failure and the second try."""
    asked: list[FederationEvent] = []

    async def gate(event):
        asked.append(event)
        return False

    handler = _Flaky(fail=1)
    registry, retry = _registry(handler=handler, gate=gate)
    event = _event()
    await registry.dispatch(event)
    await _settle(retry)
    assert asked == [event]
    assert handler.calls == 1


async def test_a_done_event_frees_its_slot_and_bytes():
    """A leak of either would refuse the second and third event."""
    size = len(orjson.dumps(_event().payload))
    for _ in range(3):
        handler = _Flaky(fail=1)
        registry, retry = _registry(
            handler=handler, max_entries=1, max_per_sender=1, max_bytes=size
        )
        await registry.dispatch(_event())
        assert (len(retry), retry.held_bytes) == (1, size)
        await _settle(retry)
        await registry.dispatch(_event())
        assert len(handler.applied) == 2


async def test_the_hold_counts_media_bytes(caplog):
    handler = _Flaky(fail=1)
    registry, retry = _registry(handler=handler, max_bytes=100)
    media = dataclasses.replace(_event(payload={}), media_bytes=b"x" * 101)
    with caplog.at_level(logging.WARNING):
        await registry.dispatch(media)
    assert len(retry) == 0
    assert handler.calls == 1
    assert "hold is full" in caplog.text


# ─── One sender's events stay in order ────────────────────────────────────


class _State:
    """Handlers shaped like the real ones: last write wins, and a delete or
    an edit of something not stored returns silently."""

    def __init__(self, *, fail_first: int = 1) -> None:
        self.fail = fail_first
        self.applied: list[str] = []
        self.messages: dict[str, dict] = {}
        self.role = "member"

    def _maybe_fail(self) -> None:
        if self.fail > 0:
            self.fail -= 1
            raise sqlite3.OperationalError("database is locked")

    async def write(self, event: FederationEvent) -> None:
        self._maybe_fail()
        self.applied.append(event.payload["v"])

    async def dm_create(self, event: FederationEvent) -> None:
        self._maybe_fail()
        self.messages[event.payload["id"]] = {"deleted": False}

    async def dm_delete(self, event: FederationEvent) -> None:
        message = self.messages.get(event.payload["id"])
        if message is None:
            return
        message["deleted"] = True

    async def role_changed(self, event: FederationEvent) -> None:
        self._maybe_fail()
        self.role = event.payload["role"]


async def test_a_later_event_waits_behind_the_held_one():
    """The reviewer's case: old at t=0 (handler fails once), new at t=3 —
    applied old, then new."""
    state = _State()
    registry, retry = _registry(handler=state.write, delays=(0.05,))
    await registry.dispatch(_event(payload={"v": "old"}))
    await registry.dispatch(_event(payload={"v": "new"}))
    assert state.applied == []  # the new one did not overtake
    await _settle(retry)
    assert state.applied == ["old", "new"]


async def test_a_dm_delete_after_a_held_create_ends_deleted():
    """A different handler than the held one — it waits all the same."""
    state = _State()
    registry, retry = _registry(handler=state.dm_create, delays=(0.05,))
    registry.register(DM_DELETED, state.dm_delete)
    await registry.dispatch(_event(DM, payload={"id": "m-1"}))
    await registry.dispatch(_event(DM_DELETED, payload={"id": "m-1"}))
    await _settle(retry)
    assert state.messages == {"m-1": {"deleted": True}}


async def test_a_demote_after_a_held_promote_ends_demoted():
    state = _State()
    registry, retry = _registry(ROLE, handler=state.role_changed, delays=(0.05,))
    await registry.dispatch(_event(ROLE, payload={"role": "admin"}))
    await registry.dispatch(_event(ROLE, payload={"role": "member"}))
    await _settle(retry)
    assert state.role == "member"


async def test_another_sender_is_not_held_up():
    state = _State()
    registry, retry = _registry(handler=state.write, delays=SLOW)
    await registry.dispatch(_event(sender="a", payload={"v": "a-1"}))
    await registry.dispatch(_event(sender="b", payload={"v": "b-1"}))
    assert state.applied == ["b-1"]
    await retry.stop()


async def test_a_queued_event_that_fails_is_tried_again_in_its_turn():
    state = _State(fail_first=1)
    registry, retry = _registry(handler=state.write)
    await registry.dispatch(_event(payload={"v": "1"}))
    state.fail = 2  # the retried head, then the queued one's first run
    await registry.dispatch(_event(payload={"v": "2"}))
    await registry.dispatch(_event(payload={"v": "3"}))
    await _settle(retry)
    assert state.applied == ["1", "2", "3"]


async def test_a_full_per_sender_queue_gives_up_the_head_and_flushes_in_order(caplog):
    state = _State(fail_first=99)
    registry, retry = _registry(handler=state.write, delays=SLOW, max_per_sender=3)
    with caplog.at_level(logging.WARNING):
        await registry.dispatch(_event(payload={"v": "head"}))
        state.fail = 0
        await registry.dispatch(_event(payload={"v": "2"}))
        await registry.dispatch(_event(payload={"v": "3"}))
        assert state.applied == [] and len(retry) == 3
        await registry.dispatch(_event(payload={"v": "4"}))  # one too many
        await _settle(retry)
    # The head was given up (its handler never ran again); the rest in order.
    assert state.applied == ["2", "3", "4"]
    messages = [r.getMessage() for r in _warnings(caplog)]
    assert len([m for m in messages if "queue is full" in m]) == 1
    assert len([m for m in messages if "dropped" in m]) == 1


async def test_a_full_hold_gives_up_that_senders_head(caplog):
    """The total bound, hit by a second sender's wait: only the sender
    whose event did not fit is flushed."""
    state = _State(fail_first=2)
    registry, retry = _registry(handler=state.write, delays=SLOW, max_entries=2)
    with caplog.at_level(logging.WARNING):
        await registry.dispatch(_event(sender="a", payload={"v": "a-head"}))
        await registry.dispatch(_event(sender="b", payload={"v": "b-head"}))
        await registry.dispatch(_event(sender="b", payload={"v": "b-2"}))
        await asyncio.sleep(0.02)
    assert state.applied == ["b-2"]
    assert len(retry) == 1  # a's is still waiting
    await retry.stop()


@pytest.mark.parametrize(
    "event_type",
    [
        FederationEventType.CALL_OFFER,
        FederationEventType.DM_USER_TYPING,
        FederationEventType.FEDERATION_RTC_ICE,
        FederationEventType.SPACE_SYNC_CHUNK,
        FederationEventType.DM_HISTORY_CHUNK,
        FederationEventType.SPACE_ROUTED,
    ],
)
async def test_signalling_and_sync_streams_are_never_held_and_never_wait(event_type):
    assert SHORT_LIVED <= NEVER_RETRIED
    held, stream = _Flaky(fail=99), _Flaky(fail=1)
    registry, retry = _registry(handler=held, delays=SLOW)
    registry.register(event_type, stream)
    await registry.dispatch(_event())  # the sender now has a held event
    await registry.dispatch(_event(event_type))
    assert stream.calls == 1 and len(retry) == 1  # failed: not held
    await registry.dispatch(_event(event_type))
    assert len(stream.applied) == 1  # and did not wait behind the DM
    await retry.stop()


# ─── Stop / restart ───────────────────────────────────────────────────────


async def test_stop_lets_the_running_handler_finish_and_drops_the_rest(caplog):
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def handler(event):
        calls.append(event.payload["v"])
        if len(calls) == 1:
            raise OSError("first try")
        started.set()
        await release.wait()  # the second try is mid-write when stop() comes
        calls.append("finished")

    registry, retry = _registry(handler=handler, delays=(0.001,))
    await registry.dispatch(_event(payload={"v": "head"}))
    await registry.dispatch(_event(payload={"v": "queued"}))
    await started.wait()
    with caplog.at_level(logging.WARNING, logger=RETRY_LOG):
        stopping = asyncio.create_task(retry.stop())
        await asyncio.sleep(0.01)
        assert not stopping.done()  # waiting for the handler, not cancelling it
        release.set()
        await stopping
    assert calls == ["head", "head", "finished"]  # the queued one never ran
    assert (len(retry), retry.held_bytes) == (0, 0)
    [warning] = _warnings(caplog)
    assert "1 held inbound event" in warning.getMessage()


async def test_stop_drops_an_event_waiting_for_its_next_try(caplog):
    handler = _Flaky(fail=1)
    registry, retry = _registry(handler=handler, delays=SLOW)
    await registry.dispatch(_event())
    with caplog.at_level(logging.WARNING, logger=RETRY_LOG):
        await retry.stop()
    assert handler.calls == 1
    assert (len(retry), retry.held_bytes) == (0, 0)
    assert len(_warnings(caplog)) == 1


async def test_nothing_is_held_after_stop_and_a_new_process_holds_nothing(caplog):
    """In memory on purpose: a restart starts empty (as before this hold
    existed, the event is then lost unless a sync carries it)."""
    handler = _Flaky(fail=1)
    registry, retry = _registry(handler=handler)
    await retry.stop()
    await retry.stop()  # idempotent
    with caplog.at_level(logging.ERROR):
        await registry.dispatch(_event())
    assert len(retry) == 0
    assert "failed for" in caplog.text
    assert len(InboundRetry()) == 0


# ─── Every transport ends in the same dispatch ────────────────────────────


def _flaky_paired(event_type=FederationEventType.DM_MEDIA_BLOB, **kw):
    p = _paired()
    handler = _Flaky(**kw)
    registry = p.svc_b._event_registry
    registry.clear()
    registry.register(event_type, handler)
    p.svc_b._inbound_retry._delays = FAST
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
    p.svc_b._inbound_retry._delays = SLOW
    body, _ = _make_frame(p)
    inbox = p.repo_b._instances[p.a_id].local_inbox_id
    await p.svc_b.handle_inbound_envelope(inbox, body)
    assert len(p.svc_b._inbound_retry) == 1
    with pytest.raises(ValueError, match="Replay detected"):
        await p.svc_b.handle_inbound_envelope(inbox, body)
    assert handler.calls == 1
    assert len(p.svc_b._inbound_retry) == 1  # the replay is not queued either
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
    h.federation._inbound_retry._delays = FAST
    sender, enc = await h.add_sender()
    frame, env, _ = _multi_frame(h, sender, enc, to=h.audience)
    assert await h.frame(frame) == {"status": "ok"}
    await _settle(h.federation._inbound_retry)
    [event] = handler.applied
    assert event.msg_id == env["msg_id"]
    with pytest.raises(ValueError, match="Replay detected"):
        await h.frame(frame)
    assert handler.calls == 2


async def test_mesh_routed_inner_events_wait_in_their_origins_queue():
    """a → b → c over the mesh: c's ``SpaceRoutedHandler`` unwraps each
    inner event and hands it to the dispatch (in production
    ``FederationService._dispatch_event``, which ends in the registry). The
    wrapper came from the relay b; the inner events queue under a."""
    redeem = FederationEventType.SPACE_INVITE_TOKEN_REDEEM
    handler = _Flaky()
    registry, retry = _registry(redeem, handler=handler, delays=(0.05,))
    nodes = _build_chain(["a", "b", "c"])
    nodes["c"].handler._dispatcher = registry.dispatch
    for nonce in ("n1", "n2"):
        await nodes["a"].handler.send_routed(
            path=["a", "b", "c"],
            target_eph_pk_b64=_mint_target_eph(nodes["c"]),
            inner_event_type=redeem,
            inner_payload={"redeem_nonce": nonce},
        )
        for _ in range(8):  # the chained deliveries
            await asyncio.sleep(0)
    assert handler.applied == [] and len(retry) == 2
    await _settle(retry)
    assert [e.payload["redeem_nonce"] for e in handler.applied] == ["n1", "n2"]
    assert {e.from_instance for e in handler.applied} == {"a"}
    assert handler.applied[0].routed_path == ["a", "b", "c"]
    assert handler.calls == 3


# ─── The second try's gate and what follows a success ─────────────────────


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


@pytest.mark.parametrize("how", ["forgotten", "tombstone"])
async def test_a_sender_unpaired_meanwhile_is_not_applied_on_the_second_try(how):
    p, handler = _flaky_paired()
    p.svc_b._inbound_retry._delays = (0.05,)
    body, _ = _make_frame(p)
    await p.svc_b.handle_inbound_rtc(p.a_id, body)
    assert len(p.svc_b._inbound_retry) == 1
    if how == "forgotten":
        del p.repo_b._instances[p.a_id]
    else:
        p.repo_b._instances[p.a_id] = dataclasses.replace(
            p.repo_b._instances[p.a_id], status=PairingStatus.UNPAIRING
        )
    await _settle(p.svc_b._inbound_retry)
    assert handler.calls == 1
    assert handler.applied == []


async def test_a_routed_origin_needs_no_pairing_for_its_second_try():
    """A mesh member's household is not a paired peer of ours."""
    p, handler = _flaky_paired(DM)
    inner = dataclasses.replace(
        _event(DM, sender="origin-not-paired"), routed_path=["origin", "r", p.b_id]
    )
    await p.svc_b._dispatch_event(inner)
    await _settle(p.svc_b._inbound_retry)
    assert handler.applied == [inner]


async def test_a_successful_second_try_marks_the_space_applied():
    p, handler = _flaky_paired(POST)
    marked: list[str] = []
    p.svc_b.attach_space_applied(marked.append)
    p.svc_b._inbound_retry._delays = (0.05,)
    await p.svc_b._dispatch_event(_event(POST, sender=p.a_id, space_id="sp-1"))
    before = len(marked)
    await _settle(p.svc_b._inbound_retry)
    assert len(handler.applied) == 1
    assert marked[before:] == ["sp-1"]


async def test_a_dropped_event_does_not_mark_the_space_applied():
    p, _handler = _flaky_paired(POST, fail=99)
    marked: list[str] = []
    p.svc_b.attach_space_applied(marked.append)
    await p.svc_b._dispatch_event(_event(POST, sender=p.a_id, space_id="sp-1"))
    before = len(marked)
    await _settle(p.svc_b._inbound_retry)
    assert marked[before:] == []


async def test_the_second_try_gate_refuses_instead_of_holding_for_a_seat(monkeypatch):
    """Held for a seat, the event would later be replayed to the handlers
    that already ran — the second try's gate must only refuse."""
    p, _ = _flaky_paired()
    svc = p.svc_b
    svc._space_repo = svc._space_remote_member_repo = object()
    svc._pending_seat_buffer = object()
    seen: list = []

    def _writer_gate(*, pending, **_kw):
        seen.append(pending)

        async def step(ctx):
            return None

        return step

    monkeypatch.setattr(
        "socialhome.federation.federation_service.make_check_space_writer",
        _writer_gate,
    )
    monkeypatch.setattr(
        "socialhome.federation.federation_service.make_check_space_archived",
        lambda **_kw: _writer_gate(pending=None),
    )
    assert await svc._retry_allowed(_event(sender=p.a_id)) is True
    assert seen[-1] is None
    assert await svc._held_event_allowed(_event(sender=p.a_id)) is True
    assert seen[-1] is svc._pending_seat_buffer


async def test_service_stop_drops_what_is_held():
    p, handler = _flaky_paired()
    p.svc_b._inbound_retry._delays = SLOW
    body, _ = _make_frame(p)
    await p.svc_b.handle_inbound_rtc(p.a_id, body)
    await p.svc_b.stop()
    assert len(p.svc_b._inbound_retry) == 0
    assert handler.calls == 1
