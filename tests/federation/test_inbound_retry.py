"""An inbound handler that failed on a locked database is run once more,
in place, on every transport; every final handler failure is one ERROR
line; a replayed envelope is still refused."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sqlite3
import threading
import time

import pytest

from socialhome.db.database import AsyncDatabase
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
    InboundRetry,
    is_transient,
)
from socialhome.federation.inbound_validator import TRANSPORT_GFS_RELAY
from tests.federation.test_federation_service import _make_frame, _paired
from tests.federation.test_routed_envelope import _build_chain, _mint_target_eph
from tests.services.test_gfs_relay_inbound import _multi_frame, _MultiHousehold

DM = FederationEventType.DM_MESSAGE
POST = FederationEventType.SPACE_POST_CREATED
UNPAIR = FederationEventType.UNPAIR
RETRY_LOG = "socialhome.federation.inbound_retry"
FAST = 0.001
SLOW = 60.0


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

    def __init__(self, fail: int = 1, exc: BaseException | None = None) -> None:
        self.fail = fail
        self.exc = exc if exc is not None else _locked()
        self.calls = 0
        self.applied: list[FederationEvent] = []

    async def __call__(self, event: FederationEvent) -> None:
        self.calls += 1
        if self.calls <= self.fail:
            raise self.exc
        self.applied.append(event)


def _registry(*types: FederationEventType, handler=None, **kw):
    """A registry with its retry and ``handler`` bound to ``types``."""
    retry = InboundRetry(**{"delay": FAST, **kw})
    registry = EventDispatchRegistry(retry=retry)
    for event_type in types or (DM,):
        registry.register(event_type, handler)
    return registry, retry


async def _yield(times: int = 20) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


def _errors(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


# ─── What is retried ──────────────────────────────────────────────────────


async def test_a_handler_that_hit_a_locked_database_applies_the_event_once(caplog):
    handler = _Flaky(fail=1)
    registry, _ = _registry(handler=handler)
    with caplog.at_level(logging.ERROR):
        await registry.dispatch(_event())  # returns once it is applied
    assert len(handler.applied) == 1
    assert handler.calls == 2
    assert _errors(caplog) == []


@pytest.mark.parametrize(
    "message", ["database is locked", "database table is locked", "database is busy"]
)
async def test_locked_and_busy_are_the_failures_that_pass(message):
    assert is_transient(sqlite3.OperationalError(message))
    handler = _Flaky(fail=1, exc=sqlite3.OperationalError(message))
    registry, _ = _registry(handler=handler)
    await registry.dispatch(_event())
    assert len(handler.applied) == 1


@pytest.mark.parametrize(
    "exc",
    [
        sqlite3.OperationalError("no such table: t"),  # a schema bug
        sqlite3.IntegrityError("UNIQUE constraint failed"),
        OSError("disk"),  # handler I/O: not covered
        TimeoutError("an answer timed out"),
        ConnectionError("peer went away"),
        ValueError("refused"),
        KeyError("x"),
        TypeError("x"),
        AttributeError("x"),
        RuntimeError("not configured"),
        SpacePermissionError("x"),
    ],
)
async def test_any_other_failure_is_not_tried_again(exc):
    assert not is_transient(exc)
    handler = _Flaky(fail=1, exc=exc)
    registry, _ = _registry(handler=handler)
    await registry.dispatch(_event())
    assert handler.calls == 1


async def test_a_timeout_after_a_committed_write_does_not_rerun_the_handler():
    """Re-running the whole handler would write its first row twice."""
    rows: list[int] = []

    async def handler(event):
        rows.append(1)  # committed
        raise TimeoutError("its answer to the sender timed out")

    registry, _ = _registry(handler=handler)
    await registry.dispatch(_event())
    assert rows == [1]


async def test_there_is_one_second_try_and_no_third(caplog):
    handler = _Flaky(fail=99)
    registry, _ = _registry(handler=handler)
    with caplog.at_level(logging.ERROR):
        await registry.dispatch(_event())
    assert handler.calls == 2
    assert len(_errors(caplog)) == 1


async def test_the_wait_is_measured_from_the_failure():
    """A real lock surfaces only after SQLite's busy timeout (5 s): however
    long the first run took to fail, the second try still comes."""
    calls: list[float] = []

    async def handler(event):
        calls.append(time.monotonic())
        if len(calls) == 1:
            await asyncio.sleep(0.05)  # the busy timeout
            raise _locked()

    registry, _ = _registry(handler=handler, delay=0.02)
    await registry.dispatch(_event())
    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.07


async def test_only_the_handler_that_failed_runs_again():
    flaky, steady = _Flaky(fail=1), _Flaky(fail=0)
    registry, _ = _registry(handler=flaky)
    registry.register(DM, steady)
    await registry.dispatch(_event())
    assert len(flaky.applied) == 1
    assert steady.calls == 1


async def test_a_failing_handler_never_blocks_the_next_one():
    broken, steady = _Flaky(fail=99, exc=ValueError("x")), _Flaky(fail=0)
    registry, _ = _registry(handler=broken)
    registry.register(DM, steady)
    await registry.dispatch(_event())
    assert steady.calls == 1


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
async def test_signalling_and_sync_streams_are_never_tried_again(event_type):
    assert SHORT_LIVED <= NEVER_RETRIED
    handler = _Flaky(fail=1)
    registry, _ = _registry(event_type, handler=handler)
    await registry.dispatch(_event(event_type))
    assert handler.calls == 1


# ─── The gate before the second try ───────────────────────────────────────


async def test_the_gate_runs_before_the_second_try_and_not_before_the_first():
    order: list[str] = []

    async def gate(event):
        order.append("gate")
        return True

    async def handler(event):
        order.append("handler")
        if order.count("handler") == 1:
            raise _locked()

    registry, _ = _registry(handler=handler, gate=gate)
    await registry.dispatch(_event())
    assert order == ["handler", "gate", "handler"]


async def test_an_event_the_gate_no_longer_allows_is_not_tried_again(caplog):
    """A ban or an unpair that landed between the failure and the retry."""

    async def gate(event):
        return False

    handler = _Flaky(fail=1)
    registry, _ = _registry(handler=handler, gate=gate)
    with caplog.at_level(logging.INFO, logger=RETRY_LOG):
        await registry.dispatch(_event())
    assert handler.calls == 1
    assert "no longer allowed" in caplog.text


async def test_a_failing_gate_drops_the_event(caplog):
    async def gate(event):
        raise _locked()

    handler = _Flaky(fail=1)
    registry, _ = _registry(handler=handler, gate=gate)
    with caplog.at_level(logging.ERROR):
        await registry.dispatch(_event())
    assert handler.calls == 1
    assert len(_errors(caplog)) == 1


# ─── Every final failure is one ERROR line, without content ───────────────


@pytest.mark.parametrize("exc", [ValueError("refused"), _locked(), TimeoutError("x")])
async def test_a_final_failure_is_one_error_with_type_sender_and_traceback(exc, caplog):
    handler = _Flaky(fail=99, exc=exc)
    registry, _ = _registry(handler=handler)
    with caplog.at_level(logging.DEBUG):
        await registry.dispatch(
            _event(sender="household-b", payload={"text": "the secret text"})
        )
    [error] = _errors(caplog)
    line = error.getMessage()
    assert "dm_message" in line and "household-b" in line
    assert type(exc).__name__ in line
    # Diagnosable from a normal log: the traceback rides on the same record.
    assert error.exc_info is not None and error.exc_info[1] is exc
    # The message itself never carries the payload.
    assert all("secret" not in r.getMessage() for r in caplog.records)


async def test_without_a_retry_object_failures_are_logged_the_same(caplog):
    handler = _Flaky(fail=99)
    registry = EventDispatchRegistry()
    registry.register(DM, handler)
    with caplog.at_level(logging.ERROR):
        await registry.dispatch(_event())
    assert handler.calls == 1
    assert len(_errors(caplog)) == 1


# ─── Stop / restart ───────────────────────────────────────────────────────


async def test_stop_ends_the_wait_and_gives_the_last_try():
    handler = _Flaky(fail=1)
    registry, retry = _registry(handler=handler, delay=SLOW)
    waiting = asyncio.create_task(registry.dispatch(_event()))
    await _yield()
    assert handler.calls == 1
    await retry.stop()  # returns once the last try is over
    assert len(handler.applied) == 1
    await waiting


async def test_stop_never_cancels_a_handler_in_its_second_try():
    release = asyncio.Event()
    calls: list[str] = []

    async def handler(event):
        calls.append("run")
        if len(calls) == 1:
            raise _locked()
        await release.wait()  # mid-write when stop() comes
        calls.append("finished")

    registry, retry = _registry(handler=handler)
    waiting = asyncio.create_task(registry.dispatch(_event()))
    while len(calls) < 2:
        await asyncio.sleep(0.001)
    stopping = asyncio.create_task(retry.stop())
    await asyncio.sleep(0.01)
    assert not stopping.done()
    release.set()
    await stopping
    await waiting
    assert calls == ["run", "run", "finished"]


async def test_after_stop_the_second_try_comes_at_once():
    """Nothing is kept across a restart: the retry lives in the handler's
    own task (an event being retried when the process dies is lost)."""
    handler = _Flaky(fail=1)
    registry, retry = _registry(handler=handler, delay=SLOW)
    await retry.stop()
    await retry.stop()  # idempotent
    await asyncio.wait_for(registry.dispatch(_event()), timeout=1.0)
    assert len(handler.applied) == 1


# ─── A real database, a real lock ─────────────────────────────────────────


async def test_a_write_that_met_an_outside_lock_is_applied_by_the_second_try(tmp_path):
    """Production timing: another process holds the file's write lock for
    5.5 s, so the first run fails only after SQLite's 5 s busy timeout. (On
    9110ee714 the retry window was measured from the first run's start and
    had closed by then: one try, the event dropped.)"""
    (tmp_path / "no-migrations").mkdir()
    db = AsyncDatabase(tmp_path / "sh.db", migrations_dir=tmp_path / "no-migrations")
    await db.startup()
    await db.enqueue("CREATE TABLE t (i INTEGER)")

    def hold() -> None:
        conn = sqlite3.connect(str(tmp_path / "sh.db"), isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        time.sleep(5.5)
        conn.execute("COMMIT")
        conn.close()

    holder = threading.Thread(target=hold)
    holder.start()
    await asyncio.sleep(0.2)
    calls = 0

    async def handler(event):
        nonlocal calls
        calls += 1
        await db.enqueue("INSERT INTO t VALUES (?)", (1,))

    registry = EventDispatchRegistry(retry=InboundRetry())
    registry.register(DM, handler)
    try:
        await registry.dispatch(_event())
        assert calls == 2
        assert await db.fetchval("SELECT COUNT(*) FROM t") == 1
    finally:
        await asyncio.to_thread(holder.join)
        await db.shutdown()


# ─── Every transport ends in the same dispatch ────────────────────────────


def _flaky_paired(event_type=FederationEventType.DM_MEDIA_BLOB, **kw):
    p = _paired()
    handler = _Flaky(**kw)
    registry = p.svc_b._event_registry
    registry.clear()
    registry.register(event_type, handler)
    p.svc_b._inbound_retry._delay = FAST
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


async def test_a_replay_is_refused_while_the_event_waits_for_its_second_try():
    p, handler = _flaky_paired()
    p.svc_b._inbound_retry._delay = 0.05
    body, _ = _make_frame(p)
    inbox = p.repo_b._instances[p.a_id].local_inbox_id
    first = asyncio.create_task(p.svc_b.handle_inbound_envelope(inbox, body))
    await _yield()
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
    h.federation._inbound_retry._delay = FAST
    sender, enc = await h.add_sender()
    frame, env, _ = _multi_frame(h, sender, enc, to=h.audience)
    assert await h.frame(frame) == {"status": "ok"}
    [event] = handler.applied
    assert event.msg_id == env["msg_id"]
    with pytest.raises(ValueError, match="Replay detected"):
        await h.frame(frame)
    assert handler.calls == 2


async def test_mesh_routed_inner_event_a_failed_handler_is_tried_again():
    """a → b → c over the mesh: c's ``SpaceRoutedHandler`` unwraps the
    inner event and hands it to the dispatch (in production
    ``FederationService._dispatch_event``, which ends in the registry)."""
    redeem = FederationEventType.SPACE_INVITE_TOKEN_REDEEM
    handler = _Flaky()
    registry, _ = _registry(redeem, handler=handler)
    nodes = _build_chain(["a", "b", "c"])
    nodes["c"].handler._dispatcher = registry.dispatch
    await nodes["a"].handler.send_routed(
        path=["a", "b", "c"],
        target_eph_pk_b64=_mint_target_eph(nodes["c"]),
        inner_event_type=redeem,
        inner_payload={"redeem_nonce": "n1"},
    )
    for _ in range(100):
        if handler.applied:
            break
        await asyncio.sleep(0.005)
    [inner] = handler.applied
    assert inner.from_instance == "a"
    assert inner.routed_path == ["a", "b", "c"]
    assert handler.calls == 2


# ─── The service's gate, and what follows a success ───────────────────────


async def _dispatch_then(p, call, change) -> None:
    """Start an inbound call whose handler fails once, make ``change``
    while it waits for its second try, then let it finish."""
    p.svc_b._inbound_retry._delay = 0.05
    task = asyncio.create_task(call)
    await _yield()
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


async def test_service_stop_gives_an_event_its_second_try_at_once():
    p, handler = _flaky_paired()
    p.svc_b._inbound_retry._delay = SLOW
    body, _ = _make_frame(p)
    waiting = asyncio.create_task(p.svc_b.handle_inbound_rtc(p.a_id, body))
    await _yield()
    assert handler.calls == 1
    await p.svc_b.stop()
    assert await waiting == {"status": "ok"}
    assert len(handler.applied) == 1
