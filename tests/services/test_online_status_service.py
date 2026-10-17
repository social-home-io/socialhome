"""Tests for :class:`OnlineStatusService`.

Covers the in-memory transitions (online → idle → resumed → offline),
multi-session edge cases, debounced ``last_seen_at`` persistence, and
the scheduler tick. The service has zero direct WebSocket I/O — events
are published on the bus and :class:`RealtimeService` does the
translation; we assert events here, frames in the realtime tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from socialhome.domain.events import (
    UserCameOnline,
    UserResumedActive,
    UserWentIdle,
    UserWentOffline,
)
from socialhome.domain.federation import FederationEventType as _Evt
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.ws_manager import WebSocketManager
from socialhome.services.online_status_service import OnlineStatusService

# ─── Fakes ────────────────────────────────────────────────────────────────


class _FakeUserRepo:
    """Records ``set_last_seen`` calls for assertions."""

    def __init__(self) -> None:
        self.last_seen_calls: list[tuple[str, str]] = []
        #: Local users ``list_active`` returns (``user_id`` only is read).
        self.active: list[str] = []

    async def list_active(self):
        return [type("U", (), {"user_id": uid})() for uid in self.active]

    async def set_last_seen(self, user_id: str, at: str) -> None:
        self.last_seen_calls.append((user_id, at))

    async def get_instance_for_user(self, user_id: str) -> str | None:
        # Every remote user the tests name is homed on ``inst-peer``.
        return "inst-peer"


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
    svc._sessions["alice"][1] = datetime.now(UTC) - timedelta(minutes=10)
    captured.clear()
    await svc._scan_once()
    assert svc.is_idle("alice")
    assert any(isinstance(e, UserWentIdle) and e.user_id == "alice" for e in captured)


async def test_idle_does_not_double_publish():
    svc, _bus, _repo, captured = _make()
    await svc.user_session_opened("alice", ws_id=1)
    svc._sessions["alice"][1] = datetime.now(UTC) - timedelta(minutes=10)
    await svc._scan_once()
    captured.clear()
    await svc._scan_once()
    assert not any(isinstance(e, UserWentIdle) for e in captured)


async def test_touch_resumes_idle_user():
    svc, _bus, _repo, captured = _make()
    await svc.user_session_opened("alice", ws_id=1)
    svc._sessions["alice"][1] = datetime.now(UTC) - timedelta(minutes=10)
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
    svc._sessions["alice"][1] = datetime.now(UTC) - timedelta(minutes=10)
    await svc._scan_once()
    assert not svc.is_idle("alice")


async def test_new_session_resumes_idle_user():
    svc, _bus, _repo, captured = _make()
    await svc.user_session_opened("alice", ws_id=1)
    svc._sessions["alice"][1] = datetime.now(UTC) - timedelta(minutes=10)
    await svc._scan_once()
    captured.clear()
    await svc.user_session_opened("alice", ws_id=2)
    assert not svc.is_idle("alice")
    assert any(isinstance(e, UserResumedActive) for e in captured)


# ─── Persistence debounce ────────────────────────────────────────────────


async def test_persist_debounce_suppresses_close_writes():
    svc, _bus, repo, _captured = _make()
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
    svc._sessions["alice"][1] = datetime.now(UTC) - timedelta(minutes=10)
    await svc._scan_once()
    assert svc.online_user_ids() == {"alice", "bob"}
    assert svc.idle_user_ids() == {"alice"}


async def test_last_seen_returns_most_recent_session_activity():
    svc, _bus, _repo, _captured = _make()
    await svc.user_session_opened("alice", ws_id=1)
    await svc.user_session_opened("alice", ws_id=2)
    older = datetime.now(UTC) - timedelta(minutes=3)
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
        #: ``instance_id`` → how many queued envelopes ``drop_queued`` drops.
        self.queued: dict[str, int] = {}
        self.dropped: list[tuple[str, frozenset]] = []

    async def send_event(self, *, to_instance_id, event_type, payload, **_):
        self.sent.append((to_instance_id, event_type, dict(payload)))

    async def drop_queued(self, instance_id, event_types) -> int:
        self.dropped.append((instance_id, frozenset(event_types)))
        return self.queued.pop(instance_id, 0)


class _FakeFederationRepo:
    def __init__(self, instances) -> None:
        self._instances = instances

    async def list_social_instances(self):
        return await self.list_instances(status="confirmed")

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
    offline_calls = [
        (tid, ev, p)
        for (tid, ev, p) in fed.sent
        if ev is FederationEventType.USER_OFFLINE
    ]
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


# ─── Per-peer visibility filtering ──────────────────────────────────────────


class _FakeVisibilityRepo:
    """Per-peer hide list. Matches the Protocol's
    ``hidden_user_ids_for_peer`` signature exactly so it slots in
    wherever the real repo would."""

    def __init__(self) -> None:
        self._hidden: dict[str, set[str]] = {}

    def hide(self, peer: str, user_id: str) -> None:
        self._hidden.setdefault(peer, set()).add(user_id)

    async def hidden_user_ids_for_peer(self, peer: str) -> frozenset[str]:
        return frozenset(self._hidden.get(peer, set()))


async def test_user_online_skips_peers_who_hid_the_user():
    """A USER_ONLINE transition fans to every confirmed peer EXCEPT
    those who have hidden ``user_id`` via the per-pair toggle."""
    from socialhome.domain.federation import FederationEventType  # noqa: F401

    svc, _bus, _repo, _captured = _make()
    fed = _FakeFederationService()
    fed_repo = _FakeFederationRepo([_peer("inst-open"), _peer("inst-hider")])
    vis = _FakeVisibilityRepo()
    vis.hide("inst-hider", "alice")
    svc.attach_federation(
        federation_service=fed,
        federation_repo=fed_repo,
        own_instance_id="inst-self",
        visibility_repo=vis,
    )
    await svc.user_session_opened("alice", ws_id=1)
    targets = {tid for tid, _t, _p in fed.sent}
    assert "inst-open" in targets
    assert "inst-hider" not in targets


async def test_user_online_no_visibility_repo_fans_to_every_peer():
    """Back-compat: when ``visibility_repo`` isn't passed to
    ``attach_federation``, every peer receives the transition. Protects
    older call sites that haven't wired the repo yet."""
    svc, _bus, _repo, _captured = _make()
    fed = _FakeFederationService()
    fed_repo = _FakeFederationRepo([_peer("inst-A"), _peer("inst-B")])
    svc.attach_federation(
        federation_service=fed,
        federation_repo=fed_repo,
        own_instance_id="inst-self",
        # visibility_repo intentionally omitted
    )
    await svc.user_session_opened("alice", ws_id=1)
    targets = {tid for tid, _t, _p in fed.sent}
    assert targets == {"inst-A", "inst-B"}


class _SourceAwareFedRepo:
    """Distinguishes the social list from every CONFIRMED row."""

    async def list_instances(self, *, status=None):
        return [_peer("inst-A"), _peer("inst-space")]

    async def list_social_instances(self):
        return [_peer("inst-A")]


async def test_presence_is_not_fanned_to_space_session_households():
    """§D2b — an invite-link household shares a space, not a presence
    relationship; who is home is not theirs to see."""
    svc, _bus, _repo, _captured = _make()
    fed = _FakeFederationService()
    svc.attach_federation(
        federation_service=fed,
        federation_repo=_SourceAwareFedRepo(),
        own_instance_id="inst-self",
    )

    await svc.user_session_opened("alice", ws_id=1)

    assert {to for to, _evt, _payload in fed.sent} == {"inst-A"}


# ─── §CP.F2 guardian blocks ──────────────────────────────────────────────


class _FakeChildProtection:
    """``kid`` is protected; its guardian blocked someone on inst-B."""

    def register_gate(self, gate) -> None:
        pass

    async def guardian_blocks_household(self, user_id, instance_id) -> bool:
        return user_id == "kid" and instance_id == "inst-B"


def _gated():
    svc, _bus, _repo, _captured = _make()
    fed = _FakeFederationService()
    svc.attach_federation(
        federation_service=fed,
        federation_repo=_FakeFederationRepo([_peer("inst-A"), _peer("inst-B")]),
        own_instance_id="inst-self",
    )
    svc.attach_child_protection(_FakeChildProtection())  # type: ignore[arg-type]
    return svc, fed


async def test_protected_accounts_online_state_skips_a_blocked_household():
    svc, fed = _gated()
    await svc.user_session_opened("kid", ws_id=1)
    await svc.user_session_closed("kid", ws_id=1)
    await svc.user_session_opened("adult", ws_id=2)
    assert {tid for tid, _t, p in fed.sent if p["user_id"] == "kid"} == {"inst-A"}
    assert {tid for tid, _t, p in fed.sent if p["user_id"] == "adult"} == {
        "inst-A",
        "inst-B",
    }


async def test_block_shows_an_online_account_offline_on_the_blocked_household():
    from socialhome.domain.federation import FederationEventType

    svc, fed = _gated()
    await svc.user_session_opened("kid", ws_id=1)
    fed.sent.clear()
    await svc.on_guardian_block("kid", "u-blocked")
    await svc.on_account_protected("kid")
    assert [(tid, t) for tid, t, _p in fed.sent] == [
        ("inst-B", FederationEventType.USER_OFFLINE)
    ] * 2
    assert all("last_seen_at" in p for _tid, _t, p in fed.sent)


async def test_block_sends_nothing_for_an_offline_account():
    svc, fed = _gated()
    await svc.on_guardian_block("kid", "u-blocked")
    assert fed.sent == []


async def test_block_drops_queued_online_transitions_and_resends_the_rest():
    """A queued ``USER_ONLINE`` sealed before the block would undo the
    offline; it goes, and everyone else's current state is sent again."""
    from socialhome.domain.federation import FederationEventType

    svc, fed = _gated()
    svc._user_repo.active = ["adult", "away", "kid"]
    await svc.user_session_opened("kid", ws_id=1)
    await svc.user_session_opened("adult", ws_id=2)
    fed.sent.clear()
    fed.queued["inst-B"] = 2
    await svc.on_guardian_block("kid", "u-blocked")
    assert fed.dropped == [
        (
            "inst-B",
            frozenset(
                {
                    FederationEventType.USER_ONLINE,
                    FederationEventType.USER_IDLE,
                    FederationEventType.USER_OFFLINE,
                }
            ),
        )
    ]
    # Everyone's current state again (``away`` is offline), then the kid's
    # one offline.
    assert [(tid, t, p["user_id"]) for tid, t, p in fed.sent] == [
        ("inst-B", FederationEventType.USER_ONLINE, "adult"),
        ("inst-B", FederationEventType.USER_OFFLINE, "away"),
        ("inst-B", FederationEventType.USER_OFFLINE, "kid"),
    ]


async def test_block_hook_without_federation_is_a_noop():
    svc, _bus, _repo, _captured = _make()
    svc.attach_child_protection(_FakeChildProtection())  # type: ignore[arg-type]
    await svc.user_session_opened("kid", ws_id=1)
    await svc.on_guardian_block("kid", "u-blocked")


# ─── §CP.F2 block lifted ─────────────────────────────────────────────────


def _lifted_env():
    """inst-A: lifted. inst-B: still blocked. inst-hider: hid ``kid``."""
    svc, _bus, _repo, _captured = _make()
    fed = _FakeFederationService()
    vis = _FakeVisibilityRepo()
    vis.hide("inst-hider", "kid")
    svc.attach_federation(
        federation_service=fed,
        federation_repo=_FakeFederationRepo(
            [_peer("inst-A"), _peer("inst-B"), _peer("inst-hider"), _peer("inst-self")]
        ),
        own_instance_id="inst-self",
        visibility_repo=vis,
    )
    svc.attach_child_protection(_FakeChildProtection())  # type: ignore[arg-type]
    return svc, fed


_ALL_LIFTED = frozenset({"inst-A", "inst-B", "inst-hider", "inst-self", "inst-Z"})


async def test_block_lifted_shows_an_online_account_online_again():
    svc, fed = _lifted_env()
    await svc.user_session_opened("kid", ws_id=1)
    fed.sent.clear()
    await svc.on_guardian_block_lifted("kid", _ALL_LIFTED)
    # Not inst-B (still blocked), inst-hider (hidden), inst-self (own),
    # inst-Z (not a social peer).
    assert fed.sent == [("inst-A", _Evt.USER_ONLINE, {"user_id": "kid"})]


async def test_block_lifted_sends_idle_for_an_idle_account():
    svc, fed = _lifted_env()
    await svc.user_session_opened("kid", ws_id=1)
    svc._sessions["kid"][1] = datetime.now(UTC) - timedelta(minutes=10)
    await svc._scan_once()
    assert svc.is_idle("kid")
    fed.sent.clear()
    await svc.on_guardian_block_lifted("kid", _ALL_LIFTED)
    assert fed.sent == [("inst-A", _Evt.USER_IDLE, {"user_id": "kid"})]


async def test_block_lifted_sends_nothing_for_an_offline_account():
    svc, fed = _lifted_env()
    await svc.on_guardian_block_lifted("kid", _ALL_LIFTED)
    assert fed.sent == []


async def test_block_lifted_without_federation_is_a_noop():
    svc, _bus, _repo, _captured = _make()
    await svc.user_session_opened("kid", ws_id=1)
    await svc.on_guardian_block_lifted("kid", frozenset({"inst-A"}))
