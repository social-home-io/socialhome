"""Tests for :class:`OnlineStatusService`.

Covers the in-memory transitions (online → idle → resumed → offline),
multi-session edge cases, debounced ``last_seen_at`` persistence, and
the scheduler tick. The service has zero direct WebSocket I/O — events
are published on the bus and :class:`RealtimeService` does the
translation; we assert events here, frames in the realtime tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from socialhome.domain.events import (
    UserCameOnline,
    UserResumedActive,
    UserWentIdle,
    UserWentOffline,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.ws_manager import WebSocketManager
from socialhome.services.online_status_service import OnlineStatusService


# ─── Fakes ────────────────────────────────────────────────────────────────


class _FakeUserRepo:
    """Records ``set_last_seen`` calls for assertions."""

    def __init__(self) -> None:
        self.last_seen_calls: list[tuple[str, str]] = []

    async def set_last_seen(self, user_id: str, at: str) -> None:
        self.last_seen_calls.append((user_id, at))


def _make() -> tuple[OnlineStatusService, EventBus, _FakeUserRepo, list]:
    bus = EventBus()
    repo = _FakeUserRepo()
    svc = OnlineStatusService(WebSocketManager(), repo, bus)
    captured: list = []
    for evt_cls in (UserCameOnline, UserResumedActive, UserWentIdle, UserWentOffline):

        async def _h(e, _cap=captured):
            _cap.append(e)

        bus.subscribe(evt_cls, _h)
    return svc, bus, repo, captured


# ─── Online / offline transitions ────────────────────────────────────────


async def test_first_session_publishes_came_online():
    svc, _bus, _repo, captured = _make()
    await svc.user_session_opened("alice", ws_id=1)
    assert any(isinstance(e, UserCameOnline) and e.user_id == "alice" for e in captured)
    assert svc.is_online("alice")
    assert not svc.is_idle("alice")


async def test_second_session_does_not_republish():
    svc, _bus, _repo, captured = _make()
    await svc.user_session_opened("alice", ws_id=1)
    await svc.user_session_opened("alice", ws_id=2)
    online_events = [e for e in captured if isinstance(e, UserCameOnline)]
    assert len(online_events) == 1


async def test_last_session_close_publishes_offline_and_persists():
    svc, _bus, repo, captured = _make()
    await svc.user_session_opened("alice", ws_id=1)
    await svc.user_session_closed("alice", ws_id=1)
    assert any(isinstance(e, UserWentOffline) for e in captured)
    assert not svc.is_online("alice")
    assert repo.last_seen_calls == [("alice", repo.last_seen_calls[0][1])]


async def test_one_session_close_with_others_still_open_keeps_online():
    svc, _bus, _repo, captured = _make()
    await svc.user_session_opened("alice", ws_id=1)
    await svc.user_session_opened("alice", ws_id=2)
    captured.clear()
    await svc.user_session_closed("alice", ws_id=2)
    assert svc.is_online("alice")
    assert not any(isinstance(e, UserWentOffline) for e in captured)


async def test_close_without_open_is_noop():
    svc, _bus, repo, captured = _make()
    await svc.user_session_closed("alice", ws_id=1)
    assert captured == []
    assert repo.last_seen_calls == []


# ─── Idle scanning ───────────────────────────────────────────────────────


async def test_scan_marks_user_idle_after_threshold():
    svc, _bus, _repo, captured = _make()
    await svc.user_session_opened("alice", ws_id=1)
    # Backdate the session so the scan triggers idle.
    svc._sessions["alice"][1] = datetime.now(timezone.utc) - timedelta(minutes=10)
    captured.clear()
    await svc._scan_once()
    assert svc.is_idle("alice")
    assert any(isinstance(e, UserWentIdle) and e.user_id == "alice" for e in captured)


async def test_idle_does_not_double_publish():
    svc, _bus, _repo, captured = _make()
    await svc.user_session_opened("alice", ws_id=1)
    svc._sessions["alice"][1] = datetime.now(timezone.utc) - timedelta(minutes=10)
    await svc._scan_once()
    captured.clear()
    await svc._scan_once()
    assert not any(isinstance(e, UserWentIdle) for e in captured)


async def test_touch_resumes_idle_user():
    svc, _bus, _repo, captured = _make()
    await svc.user_session_opened("alice", ws_id=1)
    svc._sessions["alice"][1] = datetime.now(timezone.utc) - timedelta(minutes=10)
    await svc._scan_once()
    assert svc.is_idle("alice")
    captured.clear()
    await svc.touch("alice", ws_id=1)
    assert not svc.is_idle("alice")
    assert any(isinstance(e, UserResumedActive) for e in captured)


async def test_idle_only_when_all_sessions_idle():
    svc, _bus, _repo, _captured = _make()
    await svc.user_session_opened("alice", ws_id=1)
    await svc.user_session_opened("alice", ws_id=2)
    # Tab 1 idle, tab 2 still active.
    svc._sessions["alice"][1] = datetime.now(timezone.utc) - timedelta(minutes=10)
    await svc._scan_once()
    assert not svc.is_idle("alice")


async def test_new_session_resumes_idle_user():
    svc, _bus, _repo, captured = _make()
    await svc.user_session_opened("alice", ws_id=1)
    svc._sessions["alice"][1] = datetime.now(timezone.utc) - timedelta(minutes=10)
    await svc._scan_once()
    captured.clear()
    await svc.user_session_opened("alice", ws_id=2)
    assert not svc.is_idle("alice")
    assert any(isinstance(e, UserResumedActive) for e in captured)


# ─── Persistence debounce ────────────────────────────────────────────────


async def test_persist_debounce_suppresses_close_writes():
    svc, _bus, repo, _captured = _make()
    svc.PERSIST_DEBOUNCE  # touch class attr to satisfy reader
    await svc.user_session_opened("alice", ws_id=1)
    await svc.user_session_closed("alice", ws_id=1)
    assert len(repo.last_seen_calls) == 1
    # Reconnect + disconnect within the debounce window — second write
    # should be skipped.
    await svc.user_session_opened("alice", ws_id=2)
    await svc.user_session_closed("alice", ws_id=2)
    assert len(repo.last_seen_calls) == 1


# ─── Inspection helpers ──────────────────────────────────────────────────


async def test_online_user_ids_and_idle_user_ids():
    svc, _bus, _repo, _captured = _make()
    await svc.user_session_opened("alice", ws_id=1)
    await svc.user_session_opened("bob", ws_id=2)
    svc._sessions["alice"][1] = datetime.now(timezone.utc) - timedelta(minutes=10)
    await svc._scan_once()
    assert svc.online_user_ids() == {"alice", "bob"}
    assert svc.idle_user_ids() == {"alice"}


async def test_last_seen_returns_most_recent_session_activity():
    svc, _bus, _repo, _captured = _make()
    await svc.user_session_opened("alice", ws_id=1)
    await svc.user_session_opened("alice", ws_id=2)
    older = datetime.now(timezone.utc) - timedelta(minutes=3)
    svc._sessions["alice"][1] = older
    last = svc.last_seen("alice")
    assert last is not None and last > older


# ─── Scheduler lifecycle ─────────────────────────────────────────────────


async def test_start_stop_idempotent():
    svc, _bus, _repo, _captured = _make()
    await svc.start()
    await svc.start()  # second start is a no-op
    await svc.stop()
    await svc.stop()  # second stop is a no-op


# ─── Federation ──────────────────────────────────────────────────────────


class _FakeFederationService:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object, dict]] = []

    async def send_event(self, *, to_instance_id, event_type, payload, **_):
        self.sent.append((to_instance_id, event_type, dict(payload)))


class _FakeFederationRepo:
    def __init__(self, instances) -> None:
        self._instances = instances

    async def list_instances(self, *, status=None):
        return list(self._instances)


def _peer(instance_id: str):
    """Minimal duck-typed `RemoteInstance` — only `.id` is read."""
    return type("Peer", (), {"id": instance_id})()


async def test_user_came_online_fans_to_confirmed_peers():
    from socialhome.domain.federation import FederationEventType

    svc, _bus, _repo, _captured = _make()
    fed = _FakeFederationService()
    fed_repo = _FakeFederationRepo([_peer("inst-A"), _peer("inst-B")])
    svc.attach_federation(
        federation_service=fed,
        federation_repo=fed_repo,
        own_instance_id="inst-self",
    )
    await svc.user_session_opened("alice", ws_id=1)
    sent_types = [t for _id, t, _p in fed.sent]
    assert FederationEventType.USER_ONLINE in sent_types
    targets = {tid for tid, _t, _p in fed.sent}
    assert targets == {"inst-A", "inst-B"}


async def test_remote_user_online_surfaces_in_inspection():
    from socialhome.domain.federation import FederationEventType

    svc, _bus, _repo, _captured = _make()
    await svc.apply_remote(
        from_instance="inst-peer",
        event_type=FederationEventType.USER_ONLINE,
        payload={"user_id": "u-bob"},
    )
    assert svc.is_online("u-bob")
    assert "u-bob" in svc.online_user_ids()
    # Republished as a local domain event so RealtimeService fans it
    # to local viewers' WS sessions.
    assert any(isinstance(e, UserCameOnline) for e in _captured)


async def test_remote_user_offline_clears_online_state():
    from socialhome.domain.federation import FederationEventType

    svc, _bus, _repo, captured = _make()
    await svc.apply_remote(
        from_instance="inst-peer",
        event_type=FederationEventType.USER_ONLINE,
        payload={"user_id": "u-bob"},
    )
    captured.clear()
    await svc.apply_remote(
        from_instance="inst-peer",
        event_type=FederationEventType.USER_OFFLINE,
        payload={"user_id": "u-bob", "last_seen_at": "2026-05-02T10:00:00+00:00"},
    )
    assert not svc.is_online("u-bob")
    assert any(isinstance(e, UserWentOffline) for e in captured)


async def test_remote_offline_last_seen_surfaces():
    from socialhome.domain.federation import FederationEventType

    svc, _bus, _repo, _captured = _make()
    await svc.apply_remote(
        from_instance="inst-peer",
        event_type=FederationEventType.USER_OFFLINE,
        payload={"user_id": "u-bob", "last_seen_at": "2026-05-02T10:00:00+00:00"},
    )
    last = svc.last_seen("u-bob")
    assert last is not None and last.year == 2026


async def test_remote_invalid_payload_dropped():
    from socialhome.domain.federation import FederationEventType

    svc, _bus, _repo, captured = _make()
    await svc.apply_remote(
        from_instance="inst-peer",
        event_type=FederationEventType.USER_ONLINE,
        payload={"user_id": ""},  # empty
    )
    await svc.apply_remote(
        from_instance="inst-peer",
        event_type=FederationEventType.USER_ONLINE,
        payload={"user_id": "u-bob", "last_seen_at": "not-a-date"},
    )
    assert captured  # second call still fires the event despite bad ISO
    assert svc.is_online("u-bob")
    assert svc.last_seen("u-bob") is None  # bad ISO → null


async def test_remote_idle_then_resume_publishes_transitions():
    from socialhome.domain.federation import FederationEventType

    svc, _bus, _repo, captured = _make()
    await svc.apply_remote(
        from_instance="inst-peer",
        event_type=FederationEventType.USER_ONLINE,
        payload={"user_id": "u-bob"},
    )
    captured.clear()
    await svc.apply_remote(
        from_instance="inst-peer",
        event_type=FederationEventType.USER_IDLE,
        payload={"user_id": "u-bob"},
    )
    assert any(isinstance(e, UserWentIdle) for e in captured)
    captured.clear()
    await svc.apply_remote(
        from_instance="inst-peer",
        event_type=FederationEventType.USER_ONLINE,
        payload={"user_id": "u-bob"},
    )
    assert any(isinstance(e, UserResumedActive) for e in captured)


async def test_fan_to_peers_skips_own_instance():
    from socialhome.domain.federation import FederationEventType

    svc, _bus, _repo, _captured = _make()
    fed = _FakeFederationService()
    fed_repo = _FakeFederationRepo([_peer("inst-self"), _peer("inst-A")])
    svc.attach_federation(
        federation_service=fed,
        federation_repo=fed_repo,
        own_instance_id="inst-self",
    )
    await svc.user_session_opened("alice", ws_id=1)
    targets = {tid for tid, _t, _p in fed.sent}
    assert targets == {"inst-A"}  # own instance never receives
    assert FederationEventType.USER_ONLINE in {t for _id, t, _p in fed.sent}


async def test_fan_to_peers_offline_carries_last_seen():
    from socialhome.domain.federation import FederationEventType

    svc, _bus, _repo, _captured = _make()
    fed = _FakeFederationService()
    fed_repo = _FakeFederationRepo([_peer("inst-A")])
    svc.attach_federation(
        federation_service=fed,
        federation_repo=fed_repo,
        own_instance_id="inst-self",
    )
    await svc.user_session_opened("alice", ws_id=1)
    fed.sent.clear()
    await svc.user_session_closed("alice", ws_id=1)
    offline_calls = [(tid, ev, p) for (tid, ev, p) in fed.sent
                     if ev is FederationEventType.USER_OFFLINE]
    assert len(offline_calls) == 1
    _, _, payload = offline_calls[0]
    assert payload["user_id"] == "alice"
    assert "last_seen_at" in payload


async def test_publish_without_bus_is_safe():
    """Service constructed without a bus must not raise on transitions."""
    svc = OnlineStatusService(WebSocketManager(), _FakeUserRepo(), bus=None)
    await svc.user_session_opened("alice", ws_id=1)  # no crash
    assert svc.is_online("alice")
    await svc.user_session_closed("alice", ws_id=1)
    assert not svc.is_online("alice")


async def test_attach_event_bus_late_binding():
    """attach_event_bus accepts a post-construction bus reference."""
    svc = OnlineStatusService(WebSocketManager(), _FakeUserRepo(), bus=None)
    bus = EventBus()
    svc.attach_event_bus(bus)
    captured: list = []

    async def _h(e):
        captured.append(e)

    bus.subscribe(UserCameOnline, _h)
    await svc.user_session_opened("alice", ws_id=1)
    assert any(isinstance(e, UserCameOnline) for e in captured)


async def test_idle_remote_user_surfaces_in_idle_user_ids():
    from socialhome.domain.federation import FederationEventType

    svc, _bus, _repo, _captured = _make()
    await svc.apply_remote(
        from_instance="inst-peer",
        event_type=FederationEventType.USER_IDLE,
        payload={"user_id": "u-bob"},
    )
    assert "u-bob" in svc.idle_user_ids()
    assert svc.is_idle("u-bob")
