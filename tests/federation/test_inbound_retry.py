"""One sender's inbound events are applied strictly in arrival order, and a
handler that failed for a passing reason is run again in place — on every
transport; a replayed envelope is still refused."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sqlite3

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
    RETRY_WINDOW_S,
    InboundRetry,
    is_transient,
)
from socialhome.federation.inbound_validator import TRANSPORT_GFS_RELAY
from socialhome.federation.transport import HttpsInboxTransport
from socialhome.services.call_service import CallAlreadyAnsweredError
from tests.federation.test_federation_service import _make_frame, _paired
from tests.federation.test_routed_envelope import _build_chain, _mint_target_eph
from tests.services.test_gfs_relay_inbound import _multi_frame, _MultiHousehold

DM = FederationEventType.DM_MESSAGE
DM_DELETED = FederationEventType.DM_MESSAGE_DELETED
ROLE = FederationEventType.SPACE_MEMBER_ROLE_CHANGED
POST = FederationEventType.SPACE_POST_CREATED
UNPAIR = FederationEventType.UNPAIR
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


def _locked() -> sqlite3.OperationalError:
    return sqlite3.OperationalError("database is locked")


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
    """A registry with its lanes and ``handler`` bound to ``types``."""
    retry = InboundRetry(**{"delays": FAST, "window": 3600.0, **kw})
    registry = EventDispatchRegistry(retry=retry)
    for event_type in types or (DM,):
        registry.register(event_type, handler)
    return registry, retry


async def _yield(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


# ─── One event: the retry happens in place ────────────────────────────────


async def test_a_handler_that_fails_once_applies_the_event_exactly_once():
    handler = _Flaky(fail=1)
    registry, retry = _registry(handler=handler)
    await registry.dispatch(_event())  # returns once it is applied
    assert len(handler.applied) == 1
    assert handler.calls == 2
    assert len(retry) == 0  # the lane went with its last user


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
    registry, _ = _registry(handler=handler)
    with caplog.at_level(logging.ERROR):
        await registry.dispatch(_event())
    assert handler.calls == 1
    assert "failed for" in caplog.text


@pytest.mark.parametrize("exc", [sqlite3.OperationalError, OSError, TimeoutError])
async def test_a_passing_failure_is_tried_again(exc):
    assert is_transient(exc("x"))
    handler = _Flaky(fail=1, exc=exc)
    registry, _ = _registry(handler=handler)
    await registry.dispatch(_event())
    assert len(handler.applied) == 1


async def test_a_refusal_on_a_later_try_ends_the_tries(caplog):
    calls = 0

    async def handler(event):
        nonlocal calls
        calls += 1
        raise (OSError if calls == 1 else ValueError)("x")

    registry, _ = _registry(handler=handler)
    with caplog.at_level(logging.ERROR):
        await registry.dispatch(_event())
    assert calls == 2
    assert "failed for" in caplog.text


async def test_a_poison_event_stops_after_the_last_try_with_one_warning(caplog):
    handler = _Flaky(fail=99)
    registry, retry = _registry(handler=handler)
    with caplog.at_level(logging.WARNING):
        await registry.dispatch(_event())
    assert handler.calls == 1 + len(RETRY_DELAYS_S) == 4
    assert handler.applied == []
    [warning] = _warnings(caplog)
    assert "after 4 tries" in warning.getMessage()
    assert warning.exc_info is not None
    assert len(retry) == 0


async def test_no_try_starts_after_the_window():
    """The hold is bounded in time, whatever the handler does: slower than
    every sender-side timeout would turn a retry into a transport fault."""
    assert sum(RETRY_DELAYS_S) < RETRY_WINDOW_S < HttpsInboxTransport(None)._timeout_s
    handler = _Flaky(fail=99)
    registry, _ = _registry(handler=handler, delays=(0.02, 0.02, 0.02), window=0.05)
    await registry.dispatch(_event())
    assert handler.calls == 3  # the 4th would start past the window


async def test_only_the_handler_that_failed_runs_again():
    flaky, steady = _Flaky(fail=1), _Flaky(fail=0)
    registry, _ = _registry(handler=flaky)
    registry.register(DM, steady)
    await registry.dispatch(_event())
    assert len(flaky.applied) == 1
    assert steady.calls == 1


async def test_the_gate_runs_before_every_later_try_and_not_before_the_first():
    order: list[str] = []

    async def gate(event):
        order.append("gate")
        return True

    async def handler(event):
        order.append("handler")
        if order.count("handler") < 3:
            raise _locked()

    registry, _ = _registry(handler=handler, gate=gate)
    await registry.dispatch(_event())
    assert order == ["handler", "gate", "handler", "gate", "handler"]


async def test_an_event_the_gate_no_longer_allows_is_dropped():
    """A ban or an unpair that landed between the failure and the next try."""

    async def gate(event):
        return False

    handler = _Flaky(fail=1)
    registry, _ = _registry(handler=handler, gate=gate)
    await registry.dispatch(_event())
    assert handler.calls == 1


async def test_a_failing_gate_drops_the_event(caplog):
    async def gate(event):
        raise _locked()

    handler = _Flaky(fail=1)
    registry, _ = _registry(handler=handler, gate=gate)
    with caplog.at_level(logging.WARNING, logger=RETRY_LOG):
        await registry.dispatch(_event())
    assert handler.calls == 1
    assert "gate failed" in caplog.text


# ─── One sender's events stay in order ────────────────────────────────────


class _State:
    """Handlers shaped like the real ones: last write wins, and a delete of
    something not stored returns silently."""

    def __init__(self, *, fail_first: int = 1) -> None:
        self.fail = fail_first
        self.applied: list[str] = []
        self.messages: dict[str, dict] = {}
        self.role = "member"
        #: Set once a handler is inside its "write"; it then waits for
        #: ``db`` — the database lock an in-flight handler sits on.
        self.in_flight = asyncio.Event()
        self.db = asyncio.Event()
        self.db.set()

    async def _write(self) -> None:
        self.in_flight.set()
        await self.db.wait()
        if self.fail > 0:
            self.fail -= 1
            raise _locked()

    async def write(self, event: FederationEvent) -> None:
        await self._write()
        self.applied.append(event.payload["v"])

    async def dm_create(self, event: FederationEvent) -> None:
        await self._write()
        self.messages[event.payload["id"]] = {"deleted": False}

    async def dm_delete(self, event: FederationEvent) -> None:
        message = self.messages.get(event.payload["id"])
        if message is None:
            return
        message["deleted"] = True

    async def role_changed(self, event: FederationEvent) -> None:
        await self._write()
        self.role = event.payload["role"]


async def test_a_delete_arriving_while_its_create_is_in_flight_ends_deleted():
    """Recheck probe 1: the create waits on the database lock, the delete
    arrives, the create then fails and is retried. On b5d23b750 the delete
    ran inline as a no-op and the message ended undeleted."""
    state = _State()
    registry, _ = _registry(handler=state.dm_create, delays=(0.02,))
    registry.register(DM_DELETED, state.dm_delete)
    state.db.clear()
    create = asyncio.create_task(registry.dispatch(_event(payload={"id": "m-1"})))
    await state.in_flight.wait()
    delete = asyncio.create_task(
        registry.dispatch(_event(DM_DELETED, payload={"id": "m-1"}))
    )
    await _yield()
    state.db.set()
    await asyncio.gather(create, delete)
    await asyncio.sleep(0.06)  # (the old hold applied the create only now)
    assert state.messages == {"m-1": {"deleted": True}}


async def test_events_in_flight_together_are_applied_in_arrival_order():
    """Recheck probe 2: A in flight, C fails, D follows, A fails. On
    b5d23b750 the order was C, D, A."""
    applied: list[str] = []
    to_fail = {"A", "C"}  # each fails its first run
    in_flight, db = asyncio.Event(), asyncio.Event()

    async def write(event):
        v = event.payload["v"]
        in_flight.set()
        await db.wait()  # A sits on the database lock
        if v in to_fail:
            to_fail.discard(v)
            raise _locked()
        applied.append(v)

    registry, _ = _registry(handler=write, delays=(0.02,))
    tasks = [asyncio.create_task(registry.dispatch(_event(payload={"v": "A"})))]
    await in_flight.wait()
    for v in ("C", "D"):
        tasks.append(asyncio.create_task(registry.dispatch(_event(payload={"v": v}))))
        await _yield()
    db.set()
    await asyncio.gather(*tasks)
    await asyncio.sleep(0.06)  # (the old hold applied A only now)
    assert applied == ["A", "C", "D"]


async def test_a_later_event_waits_behind_the_one_being_retried():
    """Old at t=0 (its handler fails once), new right after — applied old,
    then new."""
    state = _State()
    registry, _ = _registry(handler=state.write, delays=(0.02,))
    old = asyncio.create_task(registry.dispatch(_event(payload={"v": "old"})))
    await _yield()
    new = asyncio.create_task(registry.dispatch(_event(payload={"v": "new"})))
    await _yield()
    assert state.applied == []  # the new one did not overtake
    await asyncio.gather(old, new)
    assert state.applied == ["old", "new"]


async def test_a_demote_after_a_promote_being_retried_ends_demoted():
    state = _State()
    registry, _ = _registry(ROLE, handler=state.role_changed, delays=(0.02,))
    await asyncio.gather(
        registry.dispatch(_event(ROLE, payload={"role": "admin"})),
        registry.dispatch(_event(ROLE, payload={"role": "member"})),
    )
    assert state.role == "member"


async def test_nothing_waiting_is_ever_dropped():
    """No queue to overflow: 200 events behind a failing head all run, in
    order (main applies them too)."""
    state = _State()
    registry, retry = _registry(handler=state.write, delays=(0.02,))
    tasks = [
        asyncio.create_task(registry.dispatch(_event(payload={"v": str(i)})))
        for i in range(200)
    ]
    await _yield()
    assert len(retry) == 1  # one lane, however many wait on it
    await asyncio.gather(*tasks)
    assert state.applied == [str(i) for i in range(200)]
    assert len(retry) == 0


async def test_another_sender_is_not_held_up():
    state = _State()
    registry, retry = _registry(handler=state.write, delays=SLOW)
    held = asyncio.create_task(
        registry.dispatch(_event(sender="a", payload={"v": "a-1"}))
    )
    await _yield()
    await registry.dispatch(_event(sender="b", payload={"v": "b-1"}))
    assert state.applied == ["b-1"]
    await retry.stop()
    await held
    assert state.applied == ["b-1", "a-1"]


async def test_lanes_go_when_idle_whatever_the_sender_ids():
    """Routed origins are ids a relay chooses — no entry may outlive its
    event."""
    handler = _Flaky(fail=0)
    registry, retry = _registry(handler=handler)
    for i in range(50):
        await registry.dispatch(_event(sender=f"origin-{i}"))
    assert len(retry) == 0


async def test_a_dispatch_from_inside_a_handler_does_not_wait_for_its_own_lane():
    """``replay_held`` runs inside a handler (a parent landed, a seat went
    live) and dispatches events of any sender, the same one included."""
    applied: list[str] = []
    registry, retry = _registry(POST)

    async def on_post(event):
        applied.append("post")
        await registry.dispatch(_event(DM, sender=event.from_instance))
        await registry.dispatch(_event(DM, sender="someone-else"))

    async def on_dm(event):
        applied.append(f"dm:{event.from_instance}")

    registry.register(POST, on_post)
    registry.register(DM, on_dm)
    await asyncio.wait_for(registry.dispatch(_event(POST)), timeout=1.0)
    assert applied == ["post", "dm:peer", "dm:someone-else"]
    assert len(retry) == 0


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
async def test_signalling_and_sync_streams_take_no_lane_and_no_retry(event_type):
    assert SHORT_LIVED <= NEVER_RETRIED
    held, stream = _Flaky(fail=1), _Flaky(fail=1)
    registry, retry = _registry(handler=held, delays=SLOW)
    registry.register(event_type, stream)
    waiting = asyncio.create_task(registry.dispatch(_event()))
    await _yield()
    await registry.dispatch(_event(event_type))  # fails: not tried again
    assert stream.calls == 1
    await registry.dispatch(_event(event_type))  # and did not wait for the DM
    assert len(stream.applied) == 1
    await retry.stop()
    await waiting


# ─── Stop / restart ───────────────────────────────────────────────────────


async def test_stop_ends_the_wait_and_gives_one_last_try():
    handler = _Flaky(fail=1)
    registry, retry = _registry(handler=handler, delays=SLOW)
    waiting = asyncio.create_task(registry.dispatch(_event()))
    await _yield()
    assert handler.calls == 1
    await retry.stop()  # returns once the last try is over
    assert len(handler.applied) == 1
    assert waiting.done() or await waiting is None


async def test_stop_drops_an_event_that_still_fails_with_one_warning(caplog):
    handler = _Flaky(fail=99)
    registry, retry = _registry(handler=handler, delays=(60.0, 60.0))
    waiting = asyncio.create_task(registry.dispatch(_event()))
    await _yield()
    with caplog.at_level(logging.WARNING, logger=RETRY_LOG):
        await retry.stop()
        await waiting
    assert handler.calls == 2  # the first try and the last one — no more
    assert len(_warnings(caplog)) == 1
    assert len(retry) == 0


async def test_stop_lets_a_handler_in_its_later_try_finish():
    release = asyncio.Event()
    calls: list[str] = []

    async def handler(event):
        calls.append("run")
        if len(calls) == 1:
            raise OSError("first try")
        await release.wait()  # mid-write when stop() comes
        calls.append("finished")

    registry, retry = _registry(handler=handler)
    waiting = asyncio.create_task(registry.dispatch(_event()))
    while len(calls) < 2:
        await asyncio.sleep(0.001)
    stopping = asyncio.create_task(retry.stop())
    await asyncio.sleep(0.01)
    assert not stopping.done()  # waiting for the handler, never cancelling it
    release.set()
    await stopping
    await waiting
    assert calls == ["run", "run", "finished"]


async def test_after_stop_nothing_sleeps_and_a_new_process_holds_nothing():
    """In memory on purpose: an event being retried when the process dies
    is lost (as before this existed) — a restart starts with no lanes."""
    handler = _Flaky(fail=1)
    registry, retry = _registry(handler=handler, delays=SLOW)
    await retry.stop()
    await retry.stop()  # idempotent
    await asyncio.wait_for(registry.dispatch(_event()), timeout=1.0)
    assert len(handler.applied) == 1  # its one last try, at once
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
    assert len(handler.applied) == 1
    # The same envelope again — an attacker's replay or the sender's own
    # re-send after its POST timed out — is refused.
    with pytest.raises(ValueError, match="Replay detected"):
        await p.svc_b.handle_inbound_envelope(inbox, body)
    assert len(handler.applied) == 1


async def test_a_replay_is_refused_while_the_event_is_still_being_retried():
    """The sender's POST timed out and it re-sends: refused, and the event
    is still applied exactly once."""
    p, handler = _flaky_paired()
    p.svc_b._inbound_retry._delays = (0.05,)
    body, _ = _make_frame(p)
    inbox = p.repo_b._instances[p.a_id].local_inbox_id
    first = asyncio.create_task(p.svc_b.handle_inbound_envelope(inbox, body))
    await _yield(20)
    assert handler.calls == 1 and not first.done()
    with pytest.raises(ValueError, match="Replay detected"):
        await p.svc_b.handle_inbound_envelope(inbox, body)
    assert await first == {"status": "ok"}
    assert len(handler.applied) == 1 and handler.calls == 2


async def test_datachannel_a_failed_handler_is_tried_again():
    p, handler = _flaky_paired()
    body, _ = _make_frame(p)
    assert await p.svc_b.handle_inbound_rtc(p.a_id, body) == {"status": "ok"}
    assert len(handler.applied) == 1
    assert handler.calls == 2


async def test_datachannel_media_frame_keeps_its_bytes_for_the_second_try():
    p, handler = _flaky_paired()
    header, chunk = _make_frame(p, raw=b"picture bytes")
    await p.svc_b.handle_inbound_media_frame(p.a_id, header, chunk)
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
    [event] = handler.applied
    assert event.msg_id == env["msg_id"]
    with pytest.raises(ValueError, match="Replay detected"):
        await h.frame(frame)
    assert handler.calls == 2


async def test_mesh_routed_inner_events_take_their_origins_lane():
    """a → b → c over the mesh: c's ``SpaceRoutedHandler`` unwraps each
    inner event and hands it to the dispatch (in production
    ``FederationService._dispatch_event``, which ends in the registry). The
    wrapper came from the relay b; the lane is the origin's, a."""
    redeem = FederationEventType.SPACE_INVITE_TOKEN_REDEEM
    handler = _Flaky()
    registry, retry = _registry(redeem, handler=handler, delays=(0.03,))
    lanes: list[set[str]] = []

    async def dispatch(event):
        task = asyncio.create_task(registry.dispatch(event))
        await _yield()
        lanes.append(set(retry._lanes))
        await task

    nodes = _build_chain(["a", "b", "c"])
    nodes["c"].handler._dispatcher = dispatch
    sends = [
        asyncio.create_task(
            nodes["a"].handler.send_routed(
                path=["a", "b", "c"],
                target_eph_pk_b64=_mint_target_eph(nodes["c"]),
                inner_event_type=redeem,
                inner_payload={"redeem_nonce": nonce},
            )
        )
        for nonce in ("n1", "n2")
    ]
    await asyncio.gather(*sends)
    for _ in range(100):
        if len(handler.applied) == 2:
            break
        await asyncio.sleep(0.005)
    assert [e.payload["redeem_nonce"] for e in handler.applied] == ["n1", "n2"]
    assert handler.applied[0].routed_path == ["a", "b", "c"]
    assert handler.calls == 3
    assert lanes and all(lane == {"a"} for lane in lanes)


# ─── The later try's gate, and what follows a success ─────────────────────


async def _dispatch_then(p, call, change) -> None:
    """Start an inbound call whose handler fails once, make ``change``
    while it waits for its second try, then let it finish."""
    p.svc_b._inbound_retry._delays = (0.05,)
    task = asyncio.create_task(call)
    await _yield(20)
    assert not task.done()
    change()
    await task


async def test_a_sender_banned_meanwhile_is_not_applied_on_the_second_try():
    p, handler = _flaky_paired(FederationEventType.SPACE_MEDIA_BLOB)
    header, chunk = _make_frame(
        p, event_type=FederationEventType.SPACE_MEDIA_BLOB, space_id="sp-1"
    )

    async def banned(space_id, instance_id):
        return (space_id, instance_id) == ("sp-1", p.a_id)

    await _dispatch_then(
        p,
        p.svc_b.handle_inbound_media_frame(p.a_id, header, chunk),
        lambda: setattr(p.repo_b, "is_instance_banned_from_space", banned),
    )
    assert handler.calls == 1
    assert handler.applied == []


@pytest.mark.parametrize("how", ["forgotten", "tombstone"])
async def test_a_sender_unpaired_meanwhile_is_not_applied_on_the_second_try(how):
    p, handler = _flaky_paired()
    body, _ = _make_frame(p)

    def unpair() -> None:
        if how == "forgotten":
            del p.repo_b._instances[p.a_id]
        else:
            p.repo_b._instances[p.a_id] = dataclasses.replace(
                p.repo_b._instances[p.a_id], status=PairingStatus.UNPAIRING
            )

    await _dispatch_then(p, p.svc_b.handle_inbound_rtc(p.a_id, body), unpair)
    assert handler.calls == 1
    assert handler.applied == []


async def test_a_tombstones_own_unpair_is_still_tried_again():
    """A household we unpaired while it was offline may send its ``UNPAIR``
    (the pipeline lets exactly that through) — so may its second try."""
    p, handler = _flaky_paired(UNPAIR)
    p.repo_b._instances[p.a_id] = dataclasses.replace(
        p.repo_b._instances[p.a_id], status=PairingStatus.UNPAIRING
    )
    await p.svc_b._dispatch_event(_event(UNPAIR, sender=p.a_id))
    assert len(handler.applied) == 1
    # … and nothing else from it.
    other = _Flaky()
    p.svc_b._event_registry.register(DM, other)
    await p.svc_b._dispatch_event(_event(DM, sender=p.a_id))
    assert other.calls == 1


async def test_a_routed_origin_needs_no_pairing_for_its_second_try():
    """A mesh member's household is not a paired peer of ours."""
    p, handler = _flaky_paired(DM)
    inner = dataclasses.replace(
        _event(DM, sender="origin-not-paired"), routed_path=["origin", "r", p.b_id]
    )
    await p.svc_b._dispatch_event(inner)
    assert handler.applied == [inner]


async def test_a_successful_second_try_marks_the_space_applied():
    p, handler = _flaky_paired(POST)
    marked: list[tuple[str, int]] = []
    p.svc_b.attach_space_applied(lambda s: marked.append((s, len(handler.applied))))
    await p.svc_b._dispatch_event(_event(POST, sender=p.a_id, space_id="sp-1"))
    assert marked == [("sp-1", 1)]  # once, after the row was stored


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


async def test_service_stop_gives_an_event_being_retried_its_last_try():
    p, handler = _flaky_paired()
    p.svc_b._inbound_retry._delays = SLOW
    p.svc_b._inbound_retry._window = 3600.0
    body, _ = _make_frame(p)
    waiting = asyncio.create_task(p.svc_b.handle_inbound_rtc(p.a_id, body))
    await _yield(20)
    assert handler.calls == 1
    await p.svc_b.stop()
    assert await waiting == {"status": "ok"}
    assert len(handler.applied) == 1
