"""Tests for AppFederationService — cross-household app federation bridge.

All tests use in-memory stubs — no network, no real disk.

Security invariants:
- open_session / send_message raise AppNotFoundError for unknown apps.
- open_session / send_message raise AppNotEnabledError for disabled apps.
- Inbound events for uninstalled / disabled apps are silently dropped.
- Application payload is never exposed in plaintext on the wire; tests
  assert that send_app_message is called (not a raw dict in plaintext),
  and that the JSON fallback nests payload under "data" inside the
  encrypted send_event call.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import pytest

from socialhome.domain.apps import (
    AppAgeRestrictedError,
    AppContactNotFoundError,
    AppManifest,
    AppNotEnabledError,
    AppNotFoundError,
    InstalledApp,
)
from socialhome.domain.federation import (
    InstanceSource,
    FederationEvent,
    FederationEventType,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.user import RemoteUser, User
from socialhome.services.app_federation_service import AppFederationService


# ─── Stubs ────────────────────────────────────────────────────────────────────


def _make_installed_app(
    app_id: str = "chess",
    *,
    enabled: bool = True,
    min_age: int = 0,
) -> InstalledApp:
    return InstalledApp(
        app_id=app_id,
        name="Chess",
        version="1.0.0",
        enabled=enabled,
        manifest=AppManifest(entry="index.html", icon=None, capabilities=()),
        bundle_path="/apps/chess",
        bundle_sha256="abc123",
        source_url="https://example.com/chess.tgz",
        installed_by="admin",
        installed_at="2026-01-01T00:00:00+00:00",
        min_age=min_age,
    )


class _FakeCpRepo:
    """Minimal fake CpRepo for age-gate testing in AppFederationService."""

    def __init__(self) -> None:
        self._records: dict[str, dict] = {}

    def add(self, user_id: str, *, enabled: bool, declared_age: int) -> None:
        self._records[user_id] = {
            "child_protection_enabled": 1 if enabled else 0,
            "declared_age": declared_age,
        }

    async def get_user_protection(self, user_id: str) -> dict | None:
        return self._records.get(user_id)


def _make_remote_instance(
    instance_id: str = "peer-inst-id",
    *,
    display_name: str = "Peer Household",
    status: PairingStatus = PairingStatus.CONFIRMED,
) -> RemoteInstance:
    return RemoteInstance(
        id=instance_id,
        display_name=display_name,
        remote_identity_pk="a" * 64,
        key_self_to_remote="enc-key-1",
        key_remote_to_self="enc-key-2",
        remote_inbox_url="http://peer.example.com/fed/inbox",
        local_inbox_id="local-wh-id",
        status=status,
    )


def _make_user(
    user_id: str = "user-1",
    username: str = "alice",
    display_name: str = "Alice",
) -> User:
    return User(
        user_id=user_id,
        username=username,
        display_name=display_name,
    )


def _make_remote_user(
    user_id: str = "remote-u1",
    instance_id: str = "peer-inst-id",
    remote_username: str = "bob",
    display_name: str = "Bob",
) -> RemoteUser:
    return RemoteUser(
        user_id=user_id,
        instance_id=instance_id,
        remote_username=remote_username,
        display_name=display_name,
    )


class _FakeAppRepo:
    def __init__(self, apps: dict[str, InstalledApp] | None = None) -> None:
        self._apps: dict[str, InstalledApp] = apps or {}
        #: Captured AppPendingSession rows from add_pending_session.
        self.pending: list = []

    async def get(self, app_id: str) -> InstalledApp | None:
        return self._apps.get(app_id)

    async def list_installed(self) -> list[InstalledApp]:
        return list(self._apps.values())

    async def add_pending_session(self, s) -> None:
        self.pending.append(s)

    async def drain_pending_sessions(self, app_id, user_id, **kw):
        drained = [
            p for p in self.pending if p.app_id == app_id and p.user_id == user_id
        ]
        self.pending = [
            p for p in self.pending if not (p.app_id == app_id and p.user_id == user_id)
        ]
        return drained


class _FakeUserRepo:
    def __init__(
        self,
        users: list[User] | None = None,
        remote_users: list[RemoteUser] | None = None,
        blocked_ids: set[str] | None = None,
        block_pairs: set[tuple[str, str]] | None = None,
    ) -> None:
        self._users: list[User] = users or []
        self._remote_users: list[RemoteUser] = remote_users or []
        self._blocked_ids: set[str] = blocked_ids or set()
        #: Directed (blocker_user_id, blocked_user_id) pairs for is_blocked.
        self._block_pairs: set[tuple[str, str]] = block_pairs or set()

    async def list_all(self) -> list[User]:
        return list(self._users)

    async def list_active(self) -> list[User]:
        return [u for u in self._users if u.state == "active" and not u.deleted_at]

    async def get_by_user_id(self, user_id: str) -> User | None:
        for u in self._users:
            if u.user_id == user_id:
                return u
        return None

    async def get(self, username: str) -> User | None:
        for u in self._users:
            if u.username == username:
                return u
        return None

    async def list_all_known_remote(self) -> list[RemoteUser]:
        return list(self._remote_users)

    async def list_blocked(self, blocker_user_id: str) -> list[tuple[str, str]]:
        """Return ``[(blocked_user_id, blocked_at), ...]`` for the blocker."""
        return [(uid, "2026-01-01T00:00:00+00:00") for uid in self._blocked_ids]

    async def is_blocked(self, blocker_user_id: str, blocked_user_id: str) -> bool:
        return (blocker_user_id, blocked_user_id) in self._block_pairs

    async def get_remote_by_member(
        self, instance_id: str, remote_username: str
    ) -> RemoteUser | None:
        for r in self._remote_users:
            if r.instance_id == instance_id and r.remote_username == remote_username:
                return r
        return None


class _FakeFederationRepo:
    def __init__(self, instances: list[RemoteInstance] | None = None) -> None:
        self._instances: list[RemoteInstance] = instances or []

    async def list_social_instances(self):
        return [
            i
            for i in await self.list_instances(status="confirmed")
            if i.source is not InstanceSource.SPACE_SESSION
        ]

    async def list_instances(
        self,
        *,
        source: str | None = None,
        status: str | None = None,
    ) -> list[RemoteInstance]:
        if status is not None:
            return [i for i in self._instances if i.status.value == status]
        return list(self._instances)

    async def get_instance(self, instance_id: str) -> RemoteInstance | None:
        for i in self._instances:
            if i.id == instance_id:
                return i
        return None


class _FakeBus:
    """Records every published domain event for assertions."""

    def __init__(self) -> None:
        self.published: list[object] = []

    async def publish(self, event: object) -> None:
        self.published.append(event)


class _FakeFederation:
    """Fake FederationService that captures outbound calls."""

    def __init__(
        self, own_instance_id: str = "own-inst-id", peer_version: int = 18
    ) -> None:
        self.sent_events: list[dict] = []
        self.sent_app_messages: list[dict] = []
        self.own_instance_id = own_instance_id
        #: Version every peer is assumed to advertise — drives peer_supports.
        self.peer_version = peer_version

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        return self.peer_version >= min_version

    async def send_event(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str | None = None,
    ):
        self.sent_events.append(
            {
                "to_instance_id": to_instance_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        # Return a minimal DeliveryResult-like
        return MagicMock(ok=True)

    async def send_app_message(
        self,
        *,
        to_instance_id: str,
        app_id: str,
        session_id: str,
        payload: dict,
        to_user: str | None = None,
        from_user: str | None = None,
    ):
        self.sent_app_messages.append(
            {
                "to_instance_id": to_instance_id,
                "app_id": app_id,
                "session_id": session_id,
                "payload": payload,
                "to_user": to_user,
                "from_user": from_user,
            }
        )
        return MagicMock(ok=True)


class _FakeWs:
    """Captures broadcast_to_users / broadcast_to_user calls and online ids."""

    def __init__(self, online_user_ids: set[str] | None = None) -> None:
        self.calls: list[dict] = []
        #: Per-user sends recorded as {"user_id": ..., "payload": ...}.
        self.user_calls: list[dict] = []
        self._online: set[str] = online_user_ids or set()

    def connected_users(self) -> set[str]:
        return set(self._online)

    async def broadcast_to_users(self, user_ids: list[str], payload: dict) -> int:
        self.calls.append({"user_ids": list(user_ids), "payload": payload})
        return len(user_ids)

    async def broadcast_to_user(self, user_id: str, payload: dict) -> int:
        self.user_calls.append({"user_id": user_id, "payload": payload})
        return 1


def _make_svc(
    *,
    apps: dict[str, InstalledApp] | None = None,
    users: list[User] | None = None,
    remote_users: list[RemoteUser] | None = None,
    instances: list[RemoteInstance] | None = None,
    cp_repo: _FakeCpRepo | None = None,
    online_user_ids: set[str] | None = None,
    own_instance_id: str = "own-inst-id",
    blocked_ids: set[str] | None = None,
    block_pairs: set[tuple[str, str]] | None = None,
    peer_version: int = 18,
    bus: _FakeBus | None = None,
) -> tuple[AppFederationService, _FakeFederation, _FakeWs]:
    federation = _FakeFederation(
        own_instance_id=own_instance_id, peer_version=peer_version
    )
    ws = _FakeWs(online_user_ids=online_user_ids)
    svc = AppFederationService(
        app_repo=_FakeAppRepo(apps),
        user_repo=_FakeUserRepo(
            users,
            remote_users=remote_users,
            blocked_ids=blocked_ids,
            block_pairs=block_pairs,
        ),
        ws=ws,
        federation=federation,
        federation_repo=_FakeFederationRepo(instances),
        cp_repo=cp_repo,  # type: ignore[arg-type]
        bus=bus,
    )
    return svc, federation, ws


def _make_event(
    event_type: FederationEventType,
    payload: dict,
    from_instance: str = "peer-inst-id",
) -> FederationEvent:
    return FederationEvent(
        msg_id="msg-1",
        event_type=event_type,
        from_instance=from_instance,
        to_instance="local-inst-id",
        timestamp="2026-01-01T00:00:00+00:00",
        payload=payload,
    )


# ─── list_peers ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_peers_returns_confirmed_only():
    """Only CONFIRMED instances appear in list_peers."""
    confirmed = _make_remote_instance("c1", display_name="Home A")
    pending = dataclasses.replace(
        _make_remote_instance("p1", display_name="Home B"),
        status=PairingStatus.PENDING_SENT,
    )
    svc, _, _ = _make_svc(instances=[confirmed, pending])
    peers = await svc.list_peers()
    assert len(peers) == 1
    assert peers[0]["instance_id"] == "c1"
    assert peers[0]["display_name"] == "Home A"


@pytest.mark.asyncio
async def test_list_peers_uses_effective_display_name():
    """effective_display_name (alias > display_name) is used."""
    inst = dataclasses.replace(
        _make_remote_instance("c1", display_name="Boring Name"),
        local_alias="Cool Alias",
    )
    svc, _, _ = _make_svc(instances=[inst])
    peers = await svc.list_peers()
    assert peers[0]["display_name"] == "Cool Alias"


@pytest.mark.asyncio
async def test_list_peers_empty_when_no_confirmed():
    svc, _, _ = _make_svc(instances=[])
    assert await svc.list_peers() == []


# ─── open_session ─────────────────────────────────────────────────────────────


def _remote_target(instance_id: str = "peer-1", user_ref: str = "bob") -> dict:
    return {"instance_id": instance_id, "user_ref": user_ref, "is_local": False}


def _local_target(user_ref: str, *, own: str = "own-inst-id") -> dict:
    return {"instance_id": own, "user_ref": user_ref, "is_local": True}


@pytest.mark.asyncio
async def test_open_session_sends_app_session_event():
    """open_session fires APP_SESSION with app_id, session_id, verb."""
    app = _make_installed_app("chess")
    svc, federation, _ = _make_svc(
        apps={"chess": app},
        users=[_make_user("user-1", "alice")],
        remote_users=[_make_remote_user(instance_id="peer-1", remote_username="bob")],
    )
    session_id = await svc.open_session(
        app_id="chess",
        target=_remote_target(),
        actor_user_id="user-1",
    )
    assert len(federation.sent_events) == 1
    ev = federation.sent_events[0]
    assert ev["event_type"] is FederationEventType.APP_SESSION
    assert ev["to_instance_id"] == "peer-1"
    assert ev["payload"]["app_id"] == "chess"
    assert ev["payload"]["session_id"] == session_id
    assert ev["payload"]["verb"] == "open"
    # session_id is a non-empty hex string
    assert len(session_id) == 32
    assert all(c in "0123456789abcdef" for c in session_id)


@pytest.mark.asyncio
async def test_open_session_raises_for_missing_app():
    svc, federation, _ = _make_svc(apps={})
    with pytest.raises(AppNotFoundError):
        await svc.open_session(
            app_id="nonexistent",
            target=_remote_target(),
            actor_user_id="user-1",
        )
    assert federation.sent_events == []


@pytest.mark.asyncio
async def test_open_session_raises_for_disabled_app():
    app = _make_installed_app("chess", enabled=False)
    svc, federation, _ = _make_svc(apps={"chess": app})
    with pytest.raises(AppNotEnabledError):
        await svc.open_session(
            app_id="chess",
            target=_remote_target(),
            actor_user_id="user-1",
        )
    assert federation.sent_events == []


@pytest.mark.asyncio
async def test_open_session_returns_unique_ids():
    """Each call returns a different session_id."""
    app = _make_installed_app("chess")
    svc, _, _ = _make_svc(
        apps={"chess": app},
        users=[_make_user("u1", "alice")],
        remote_users=[_make_remote_user(instance_id="peer-1", remote_username="bob")],
    )
    ids = {
        await svc.open_session(
            app_id="chess",
            target=_remote_target(),
            actor_user_id="u1",
        )
        for _ in range(5)
    }
    assert len(ids) == 5


@pytest.mark.asyncio
async def test_open_session_local_loopback_delivers_to_target_and_initiator_only():
    """Local target → per-user WS frames to {target, initiator}, no fed send."""
    app = _make_installed_app("chess")
    initiator = _make_user("u-init", "alice", "Alice")
    target = _make_user("u-target", "bob", "Bob")
    svc, federation, ws = _make_svc(
        apps={"chess": app},
        users=[initiator, target],
        own_instance_id="own-inst-id",
    )
    session_id = await svc.open_session(
        app_id="chess",
        target=_local_target("u-target"),
        actor_user_id="u-init",
    )
    # No federation event for a local loopback.
    assert federation.sent_events == []
    # Not a fan-out to all local users.
    assert ws.calls == []
    # Per-user delivery to exactly target + initiator.
    recipients = {c["user_id"] for c in ws.user_calls}
    assert recipients == {"u-target", "u-init"}
    for c in ws.user_calls:
        frame = c["payload"]
        assert frame["type"] == "app.message"
        assert frame["app_id"] == "chess"
        assert frame["kind"] == "session"
        assert frame["session_id"] == session_id
        assert frame["from_instance"] == "own-inst-id"
        assert frame["from_user"] == "u-init"
        assert frame["payload"] == {
            "app_id": "chess",
            "session_id": session_id,
            "verb": "open",
        }


@pytest.mark.asyncio
async def test_open_session_remote_includes_to_user_and_from_user_when_peer_supports():
    """peer_version=18 → APP_SESSION payload carries to_user + from_user."""
    app = _make_installed_app("chess")
    svc, federation, _ = _make_svc(
        apps={"chess": app},
        users=[_make_user("u-init", "alice", "Alice")],
        remote_users=[_make_remote_user(instance_id="peer-1", remote_username="bob")],
        peer_version=18,
    )
    await svc.open_session(
        app_id="chess",
        target=_remote_target("peer-1", "bob"),
        actor_user_id="u-init",
    )
    assert len(federation.sent_events) == 1
    payload = federation.sent_events[0]["payload"]
    assert payload["to_user"] == "bob"
    assert payload["from_user"] == "alice"  # initiator's username


@pytest.mark.asyncio
async def test_open_session_remote_omits_to_user_for_legacy_peer():
    """peer_version=17 → no to_user/from_user on the wire (legacy fan-out)."""
    app = _make_installed_app("chess")
    svc, federation, _ = _make_svc(
        apps={"chess": app},
        users=[_make_user("u-init", "alice", "Alice")],
        remote_users=[_make_remote_user(instance_id="peer-1", remote_username="bob")],
        peer_version=17,
    )
    await svc.open_session(
        app_id="chess",
        target=_remote_target("peer-1", "bob"),
        actor_user_id="u-init",
    )
    assert len(federation.sent_events) == 1
    payload = federation.sent_events[0]["payload"]
    assert "to_user" not in payload
    assert "from_user" not in payload


@pytest.mark.asyncio
async def test_open_session_local_loopback_excludes_protected_minor_target():
    """A local under-age protected-minor target is not delivered to."""
    app = _make_installed_app("chess", min_age=13)
    cp = _FakeCpRepo()
    cp.add("u-target", enabled=True, declared_age=10)  # protected minor
    initiator = _make_user("u-init", "alice", "Alice")
    target = _make_user("u-target", "kid", "Kid")
    svc, federation, ws = _make_svc(
        apps={"chess": app},
        users=[initiator, target],
        cp_repo=cp,
        own_instance_id="own-inst-id",
    )
    await svc.open_session(
        app_id="chess",
        target=_local_target("u-target"),
        actor_user_id="u-init",
    )
    assert federation.sent_events == []
    recipients = {c["user_id"] for c in ws.user_calls}
    # Minor target is filtered; initiator (adult/unprotected) still gets it.
    assert "u-target" not in recipients
    assert "u-init" in recipients


# ─── send_message ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_delegates_to_send_app_message():
    """send_message calls federation.send_app_message — not send_event directly."""
    app = _make_installed_app("chess")
    svc, federation, _ = _make_svc(
        apps={"chess": app},
        users=[_make_user("user-1", "alice")],
        remote_users=[_make_remote_user(instance_id="peer-1", remote_username="bob")],
    )
    payload = {"move": "e2e4", "clock": 90}
    await svc.send_message(
        app_id="chess",
        session_id="session-abc",
        target=_remote_target("peer-1", "bob"),
        payload=payload,
        actor_user_id="user-1",
    )
    assert len(federation.sent_app_messages) == 1
    msg = federation.sent_app_messages[0]
    assert msg["to_instance_id"] == "peer-1"
    assert msg["app_id"] == "chess"
    assert msg["session_id"] == "session-abc"
    assert msg["payload"] == payload


@pytest.mark.asyncio
async def test_send_message_raises_for_missing_app():
    svc, federation, _ = _make_svc(apps={})
    with pytest.raises(AppNotFoundError):
        await svc.send_message(
            app_id="chess",
            session_id="s1",
            target=_remote_target("peer-1", "bob"),
            payload={"x": 1},
            actor_user_id="u1",
        )
    assert federation.sent_app_messages == []


@pytest.mark.asyncio
async def test_send_message_raises_for_disabled_app():
    app = _make_installed_app("chess", enabled=False)
    svc, federation, _ = _make_svc(apps={"chess": app})
    with pytest.raises(AppNotEnabledError):
        await svc.send_message(
            app_id="chess",
            session_id="s1",
            target=_remote_target("peer-1", "bob"),
            payload={"x": 1},
            actor_user_id="u1",
        )
    assert federation.sent_app_messages == []


@pytest.mark.asyncio
async def test_send_message_local_loopback_per_user():
    """A local-target send delivers a kind=message frame to the two parties only.

    No federation send, no fan-out to all local users — exactly the
    initiator and the addressed local user receive the WebSocket frame,
    carrying ``from_user`` = the initiator.
    """
    app = _make_installed_app("chess")
    initiator = _make_user("u-init", "alice", "Alice")
    target = _make_user("u-target", "bob", "Bob")
    bystander = _make_user("u-other", "carol", "Carol")
    svc, federation, ws = _make_svc(
        apps={"chess": app},
        users=[initiator, target, bystander],
        own_instance_id="own-inst-id",
    )
    payload = {"move": "e2e4"}
    await svc.send_message(
        app_id="chess",
        session_id="sess-local",
        target=_local_target("u-target"),
        payload=payload,
        actor_user_id="u-init",
    )
    # No federation send for a local loopback.
    assert federation.sent_app_messages == []
    assert federation.sent_events == []
    # Not a fan-out to all local users.
    assert ws.calls == []
    # Exactly the two parties get the frame.
    recipients = {c["user_id"] for c in ws.user_calls}
    assert recipients == {"u-init", "u-target"}
    for c in ws.user_calls:
        frame = c["payload"]
        assert frame["type"] == "app.message"
        assert frame["app_id"] == "chess"
        assert frame["kind"] == "message"
        assert frame["session_id"] == "sess-local"
        assert frame["from_instance"] == "own-inst-id"
        assert frame["from_user"] == "u-init"
        assert frame["payload"] == payload


@pytest.mark.asyncio
async def test_send_message_remote_includes_to_user_when_peer_supports():
    """peer_version=18 → send_app_message carries to_user + from_user."""
    app = _make_installed_app("chess")
    svc, federation, _ = _make_svc(
        apps={"chess": app},
        users=[_make_user("u-init", "alice", "Alice")],
        remote_users=[_make_remote_user(instance_id="peer-1", remote_username="bob")],
        peer_version=18,
    )
    await svc.send_message(
        app_id="chess",
        session_id="s1",
        target=_remote_target("peer-1", "bob"),
        payload={"move": "e2e4"},
        actor_user_id="u-init",
    )
    assert len(federation.sent_app_messages) == 1
    msg = federation.sent_app_messages[0]
    assert msg["to_user"] == "bob"
    assert msg["from_user"] == "alice"


@pytest.mark.asyncio
async def test_send_message_remote_omits_to_user_for_legacy_peer():
    """peer_version=17 → no to_user/from_user passed to send_app_message."""
    app = _make_installed_app("chess")
    svc, federation, _ = _make_svc(
        apps={"chess": app},
        users=[_make_user("u-init", "alice", "Alice")],
        remote_users=[_make_remote_user(instance_id="peer-1", remote_username="bob")],
        peer_version=17,
    )
    await svc.send_message(
        app_id="chess",
        session_id="s1",
        target=_remote_target("peer-1", "bob"),
        payload={"move": "e2e4"},
        actor_user_id="u-init",
    )
    assert len(federation.sent_app_messages) == 1
    msg = federation.sent_app_messages[0]
    assert msg["to_user"] is None
    assert msg["from_user"] is None


@pytest.mark.asyncio
async def test_send_message_rejects_blocked_target():
    """A blocked remote contact is not in the roster → send_message raises."""
    app = _make_installed_app("chess")
    blocked_remote = _make_remote_user(
        user_id="ru-blocked",
        instance_id="peer-1",
        remote_username="bob",
    )
    svc, federation, _ = _make_svc(
        apps={"chess": app},
        users=[_make_user("u-init", "alice", "Alice")],
        remote_users=[blocked_remote],
        blocked_ids={"ru-blocked"},
    )
    with pytest.raises(AppContactNotFoundError):
        await svc.send_message(
            app_id="chess",
            session_id="s1",
            target=_remote_target("peer-1", "bob"),
            payload={"move": "e2e4"},
            actor_user_id="u-init",
        )
    assert federation.sent_app_messages == []


# ─── on_inbound_event (JSON path) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbound_app_message_delivers_to_local_users():
    """APP_MESSAGE event → ws.broadcast_to_users with app.message frame."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users)

    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {
            "app_id": "chess",
            "session_id": "sess-1",
            "data": {"move": "e2e4"},
        },
    )
    await svc.on_inbound_event(event)

    assert len(ws.calls) == 1
    call = ws.calls[0]
    assert set(call["user_ids"]) == {"u1", "u2"}
    frame = call["payload"]
    assert frame["type"] == "app.message"
    assert frame["app_id"] == "chess"
    assert frame["session_id"] == "sess-1"
    assert frame["from_instance"] == "peer-inst-id"
    assert frame["payload"] == {"move": "e2e4"}
    # kind is "message" for APP_MESSAGE events
    assert frame["kind"] == "message"


@pytest.mark.asyncio
async def test_inbound_app_session_delivers_full_payload():
    """APP_SESSION event → entire payload dict forwarded (verb, …).

    from_user is no longer included in the APP_SESSION wire payload (FIX-I2).
    The inbound handler passes whatever fields arrive through to the SPA —
    that is fine, the SPA is the appropriate place to decide what to display.
    We verify that the mandatory fields (verb, app_id, session_id) are
    forwarded and that a compliant sender does NOT put from_user on the wire.
    """
    app = _make_installed_app("chess")
    users = [_make_user("u1")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users)

    event = _make_event(
        FederationEventType.APP_SESSION,
        {
            "app_id": "chess",
            "session_id": "sess-2",
            "verb": "open",
            # Compliant sender does NOT include from_user.
        },
    )
    await svc.on_inbound_event(event)

    assert len(ws.calls) == 1
    frame = ws.calls[0]["payload"]
    assert frame["type"] == "app.message"
    assert frame["session_id"] == "sess-2"
    # Mandatory fields forwarded.
    assert frame["payload"]["verb"] == "open"
    # from_user must NOT be present in a compliant APP_SESSION payload.
    assert "from_user" not in frame["payload"]
    # kind is "session" for APP_SESSION events
    assert frame["kind"] == "session"


@pytest.mark.asyncio
async def test_inbound_for_uninstalled_app_not_delivered():
    """App not in repo → silently dropped, no WebSocket push."""
    svc, _, ws = _make_svc(apps={}, users=[_make_user("u1")])
    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {"app_id": "unknown-app", "session_id": "s1", "data": {}},
    )
    await svc.on_inbound_event(event)
    assert ws.calls == []


@pytest.mark.asyncio
async def test_inbound_for_disabled_app_not_delivered():
    """Disabled app → silently dropped, no WebSocket push."""
    app = _make_installed_app("chess", enabled=False)
    svc, _, ws = _make_svc(apps={"chess": app}, users=[_make_user("u1")])
    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {"app_id": "chess", "session_id": "s1", "data": {}},
    )
    await svc.on_inbound_event(event)
    assert ws.calls == []


@pytest.mark.asyncio
async def test_inbound_missing_app_id_dropped():
    """Missing app_id in payload → silently dropped."""
    svc, _, ws = _make_svc(users=[_make_user("u1")])
    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {"session_id": "s1", "data": {}},
    )
    await svc.on_inbound_event(event)
    assert ws.calls == []


@pytest.mark.asyncio
async def test_inbound_no_local_users_no_broadcast():
    """No local users → broadcast_to_users is not called."""
    app = _make_installed_app("chess")
    svc, _, ws = _make_svc(apps={"chess": app}, users=[])
    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {"app_id": "chess", "session_id": "s1", "data": {"x": 1}},
    )
    await svc.on_inbound_event(event)
    assert ws.calls == []


# ─── on_inbound_message (binary path) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbound_binary_delivers_to_local_users():
    """Binary-channel path delivers to users just like the JSON event path."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users)

    await svc.on_inbound_message(
        "peer-inst-id",
        "chess",
        "sess-bin",
        {"move": "d7d5"},
    )

    assert len(ws.calls) == 1
    frame = ws.calls[0]["payload"]
    assert frame["type"] == "app.message"
    assert frame["app_id"] == "chess"
    assert frame["session_id"] == "sess-bin"
    assert frame["from_instance"] == "peer-inst-id"
    assert frame["payload"] == {"move": "d7d5"}
    # Binary path is always "message" kind
    assert frame["kind"] == "message"


@pytest.mark.asyncio
async def test_inbound_binary_for_disabled_app_not_delivered():
    app = _make_installed_app("chess", enabled=False)
    svc, _, ws = _make_svc(apps={"chess": app}, users=[_make_user("u1")])
    await svc.on_inbound_message("peer", "chess", "s", {})
    assert ws.calls == []


@pytest.mark.asyncio
async def test_inbound_binary_for_uninstalled_app_not_delivered():
    svc, _, ws = _make_svc(apps={}, users=[_make_user("u1")])
    await svc.on_inbound_message("peer", "unknown-app", "s", {})
    assert ws.calls == []


# ─── Inbound per-user routing (Task 6) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_inbound_routes_to_resolved_user_when_to_user_present():
    """A non-empty to_user resolves to a local user → single broadcast_to_user."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users)

    event = _make_event(
        FederationEventType.APP_SESSION,
        {
            "app_id": "chess",
            "session_id": "sess-1",
            "verb": "open",
            "to_user": "bob",  # bob's local username
            "from_user": "remote-alice",
        },
    )
    await svc.on_inbound_event(event)

    # Per-user delivery to exactly the resolved user — no fan-out.
    assert ws.calls == []
    recipients = {c["user_id"] for c in ws.user_calls}
    assert recipients == {"u2"}
    frame = ws.user_calls[0]["payload"]
    assert frame["type"] == "app.message"
    assert frame["session_id"] == "sess-1"
    assert frame["kind"] == "session"


@pytest.mark.asyncio
async def test_inbound_empty_to_user_falls_back_to_broadcast():
    """An empty-string to_user (legacy household open) falls back to broadcast-all."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users)

    event = _make_event(
        FederationEventType.APP_SESSION,
        {
            "app_id": "chess",
            "session_id": "sess-1",
            "verb": "open",
            "to_user": "",  # legacy household fan-out
        },
    )
    await svc.on_inbound_event(event)

    # Empty string must never be looked up — broadcast-all.
    assert ws.user_calls == []
    assert len(ws.calls) == 1
    assert set(ws.calls[0]["user_ids"]) == {"u1", "u2"}


@pytest.mark.asyncio
async def test_inbound_absent_to_user_falls_back_to_broadcast():
    """No to_user key at all → broadcast-all (legacy / binary path)."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users)

    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {"app_id": "chess", "session_id": "sess-1", "data": {"move": "e4"}},
    )
    await svc.on_inbound_event(event)

    assert ws.user_calls == []
    assert len(ws.calls) == 1
    assert set(ws.calls[0]["user_ids"]) == {"u1", "u2"}


@pytest.mark.asyncio
async def test_inbound_unresolvable_to_user_falls_back_to_broadcast():
    """A to_user that resolves to no local user → broadcast-all (best-effort)."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users)

    event = _make_event(
        FederationEventType.APP_SESSION,
        {
            "app_id": "chess",
            "session_id": "sess-1",
            "verb": "open",
            "to_user": "nobody-here",
        },
    )
    await svc.on_inbound_event(event)

    assert ws.user_calls == []
    assert len(ws.calls) == 1
    assert set(ws.calls[0]["user_ids"]) == {"u1", "u2"}


@pytest.mark.asyncio
async def test_inbound_binary_always_broadcasts():
    """Binary path carries no to_user → broadcast-all even with multiple users."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users)

    await svc.on_inbound_message("peer-inst-id", "chess", "sess-bin", {"move": "d5"})

    assert ws.user_calls == []
    assert len(ws.calls) == 1
    assert set(ws.calls[0]["user_ids"]) == {"u1", "u2"}


# ─── Target authorization (Task 6 — item C) ──────────────────────────────────


@pytest.mark.asyncio
async def test_open_session_rejects_target_not_in_contacts():
    """A target who is not a contact of the actor → AppContactNotFoundError."""
    app = _make_installed_app("chess")
    svc, federation, ws = _make_svc(
        apps={"chess": app},
        users=[_make_user("u-init", "alice", "Alice")],
        # No remote users → the remote target is not a contact.
    )
    with pytest.raises(AppContactNotFoundError):
        await svc.open_session(
            app_id="chess",
            target=_remote_target("peer-1", "stranger"),
            actor_user_id="u-init",
        )
    assert federation.sent_events == []
    assert ws.user_calls == []


@pytest.mark.asyncio
async def test_open_session_rejects_blocked_target():
    """A blocked contact is not in the roster → AppContactNotFoundError."""
    app = _make_installed_app("chess")
    blocked_remote = _make_remote_user(
        user_id="ru-blocked",
        instance_id="peer-1",
        remote_username="bob",
    )
    svc, federation, _ = _make_svc(
        apps={"chess": app},
        users=[_make_user("u-init", "alice", "Alice")],
        remote_users=[blocked_remote],
        blocked_ids={"ru-blocked"},
    )
    with pytest.raises(AppContactNotFoundError):
        await svc.open_session(
            app_id="chess",
            target=_remote_target("peer-1", "bob"),
            actor_user_id="u-init",
        )
    assert federation.sent_events == []


@pytest.mark.asyncio
async def test_remote_open_session_does_not_self_echo_locally():
    """A remote open_session sends exactly one federation event and zero local WS
    deliveries — the initiator's own household must never self-echo the outbound
    challenge back to local users.

    This locks the no-self-echo invariant: open_session for a remote target
    takes only the federation send path and never calls broadcast_to_user or
    broadcast_to_users for the initiating household.
    """
    app = _make_installed_app("chess")
    actor = _make_user("u-self", "alice", "Alice")
    bob_remote = _make_remote_user(
        user_id="ru-bob",
        instance_id="instanceB",
        remote_username="bob",
        display_name="Bob",
    )
    svc, federation, ws = _make_svc(
        apps={"chess": app},
        users=[actor],
        remote_users=[bob_remote],
        own_instance_id="own-inst-id",
    )
    sid = await svc.open_session(
        app_id="chess",
        target={"instance_id": "instanceB", "user_ref": "bob", "is_local": False},
        actor_user_id="u-self",
    )
    assert sid  # session_id was allocated
    # No local WS delivery of any kind — no self-echo.
    assert ws.calls == [], (
        "broadcast_to_users must not be called on the initiating household"
    )
    assert ws.user_calls == [], (
        "broadcast_to_user must not be called on the initiating household"
    )
    # Exactly one outbound federation event was sent.
    assert len(federation.sent_events) == 1
    assert federation.sent_events[0]["event_type"] is FederationEventType.APP_SESSION
    assert federation.sent_events[0]["to_instance_id"] == "instanceB"


@pytest.mark.asyncio
async def test_legacy_household_target_is_exempt_from_contact_check():
    """A legacy household-addressed target (user_ref == "") is allowed through."""
    app = _make_installed_app("chess")
    svc, federation, _ = _make_svc(
        apps={"chess": app},
        users=[_make_user("u-init", "alice", "Alice")],
        peer_version=17,
    )
    # No remote contacts, but user_ref="" is the legacy back-compat path.
    await svc.open_session(
        app_id="chess",
        target={"instance_id": "peer-1", "user_ref": "", "is_local": False},
        actor_user_id="u-init",
    )
    assert len(federation.sent_events) == 1


# ─── Age gate tests (§CP) ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_session_blocked_for_minor():
    """open_session raises AppAgeRestrictedError for a protected minor below min_age."""
    app = _make_installed_app("chess", min_age=13)
    cp = _FakeCpRepo()
    cp.add("minor1", enabled=True, declared_age=12)
    svc, federation, _ = _make_svc(apps={"chess": app}, cp_repo=cp)
    with pytest.raises(AppAgeRestrictedError):
        await svc.open_session(
            app_id="chess",
            target=_remote_target(),
            actor_user_id="minor1",
        )
    # Must not send anything
    assert federation.sent_events == []


@pytest.mark.asyncio
async def test_open_session_allowed_for_unprotected_user():
    """open_session is allowed for an unprotected adult account."""
    app = _make_installed_app("chess", min_age=18)
    cp = _FakeCpRepo()
    cp.add("adult1", enabled=False, declared_age=15)  # cp disabled → unprotected
    svc, federation, _ = _make_svc(
        apps={"chess": app},
        users=[_make_user("adult1", "adult", "Adult")],
        remote_users=[
            _make_remote_user(instance_id="peer-inst-id", remote_username="bob")
        ],
        cp_repo=cp,
    )
    await svc.open_session(
        app_id="chess",
        target=_remote_target("peer-inst-id", "bob"),
        actor_user_id="adult1",
    )
    assert len(federation.sent_events) == 1


@pytest.mark.asyncio
async def test_send_message_blocked_for_minor():
    """send_message raises AppAgeRestrictedError for a protected minor below min_age."""
    app = _make_installed_app("chess", min_age=16)
    cp = _FakeCpRepo()
    cp.add("minor1", enabled=True, declared_age=14)
    svc, federation, _ = _make_svc(apps={"chess": app}, cp_repo=cp)
    with pytest.raises(AppAgeRestrictedError):
        await svc.send_message(
            app_id="chess",
            session_id="s1",
            target=_remote_target("peer-1", "bob"),
            payload={"move": "e2e4"},
            actor_user_id="minor1",
        )
    assert federation.sent_app_messages == []


@pytest.mark.asyncio
async def test_send_message_allowed_for_adult_meeting_min_age():
    """send_message is allowed for a protected user at or above min_age."""
    app = _make_installed_app("chess", min_age=16)
    cp = _FakeCpRepo()
    cp.add("teen1", enabled=True, declared_age=16)  # exactly at threshold
    svc, federation, _ = _make_svc(
        apps={"chess": app},
        users=[_make_user("teen1", "teen", "Teen")],
        remote_users=[_make_remote_user(instance_id="peer-1", remote_username="bob")],
        cp_repo=cp,
    )
    await svc.send_message(
        app_id="chess",
        session_id="s1",
        target=_remote_target("peer-1", "bob"),
        payload={"move": "e2e4"},
        actor_user_id="teen1",
    )
    assert len(federation.sent_app_messages) == 1


# ─── FIX 3: _deliver filters minor recipients from fan-out ───────────────────


@pytest.mark.asyncio
async def test_deliver_excludes_protected_minor_from_fanout():
    """A protected under-age minor must NOT receive inbound app.message frames.

    Setup: chess app with min_age=13.  Two local users — a protected minor
    (declared_age=10) and an adult (declared_age=20, protection enabled).
    Inbound APP_MESSAGE must only reach the adult.
    """
    app = _make_installed_app("chess", min_age=13)
    cp = _FakeCpRepo()
    cp.add("minor1", enabled=True, declared_age=10)
    cp.add("adult1", enabled=True, declared_age=20)
    users = [_make_user("minor1", "minor"), _make_user("adult1", "adult")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users, cp_repo=cp)

    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {"app_id": "chess", "session_id": "s1", "data": {"move": "e2e4"}},
    )
    await svc.on_inbound_event(event)

    assert len(ws.calls) == 1
    recipients = ws.calls[0]["user_ids"]
    assert "minor1" not in recipients, "minor must be excluded from fan-out"
    assert "adult1" in recipients, "adult must receive the frame"


@pytest.mark.asyncio
async def test_deliver_includes_unprotected_user_regardless_of_age():
    """An unprotected user (cp disabled) must receive frames even for min_age apps."""
    app = _make_installed_app("chess", min_age=18)
    cp = _FakeCpRepo()
    cp.add("young1", enabled=False, declared_age=10)  # cp disabled → unprotected
    users = [_make_user("young1", "young")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users, cp_repo=cp)

    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {"app_id": "chess", "session_id": "s1", "data": {}},
    )
    await svc.on_inbound_event(event)

    assert len(ws.calls) == 1
    assert "young1" in ws.calls[0]["user_ids"]


@pytest.mark.asyncio
async def test_deliver_skips_filtering_when_min_age_zero():
    """Fast path: when app.min_age == 0 all users receive the frame."""
    app = _make_installed_app("chess", min_age=0)
    cp = _FakeCpRepo()
    cp.add("minor1", enabled=True, declared_age=5)
    users = [_make_user("minor1", "minor"), _make_user("adult1", "adult")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users, cp_repo=cp)

    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {"app_id": "chess", "session_id": "s1", "data": {}},
    )
    await svc.on_inbound_event(event)

    assert len(ws.calls) == 1
    recipients = ws.calls[0]["user_ids"]
    # Both users receive the frame — no filtering for unrestricted apps.
    assert "minor1" in recipients
    assert "adult1" in recipients


# ─── list_contacts ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_contacts_excludes_self():
    """The calling user must not appear in their own contact list."""
    alice = _make_user("u-alice", "alice", "Alice")
    bob = _make_user("u-bob", "bob", "Bob")
    svc, _, _ = _make_svc(users=[alice, bob], own_instance_id="own-inst")
    contacts = await svc.list_contacts(self_user_id="u-alice")
    ids = [c["user_ref"] for c in contacts]
    assert "u-alice" not in ids
    assert "u-bob" in ids


@pytest.mark.asyncio
async def test_list_contacts_local_member_shape():
    """A local household member appears with is_local=True and the own instance_id."""
    alice = _make_user("u-alice", "alice", "Alice")
    bob = _make_user("u-bob", "bob", "Bob")
    svc, _, ws = _make_svc(
        users=[alice, bob],
        online_user_ids={"u-bob"},
        own_instance_id="own-inst",
    )
    contacts = await svc.list_contacts(self_user_id="u-alice")
    assert len(contacts) == 1
    c = contacts[0]
    assert c["instance_id"] == "own-inst"
    assert c["user_ref"] == "u-bob"
    assert c["display_name"] == "Bob"
    assert c["is_local"] is True
    assert c["online"] is True  # bob has a live WS session


@pytest.mark.asyncio
async def test_list_contacts_local_member_offline():
    """A local member with no WS session is online=False."""
    alice = _make_user("u-alice", "alice", "Alice")
    bob = _make_user("u-bob", "bob", "Bob")
    svc, _, _ = _make_svc(
        users=[alice, bob],
        online_user_ids=set(),  # nobody online
        own_instance_id="own-inst",
    )
    contacts = await svc.list_contacts(self_user_id="u-alice")
    assert len(contacts) == 1
    assert contacts[0]["online"] is False


@pytest.mark.asyncio
async def test_list_contacts_remote_user_shape():
    """A known remote user appears with is_local=False, correct instance_id and user_ref."""
    local = _make_user("u-alice", "alice", "Alice")
    remote = _make_remote_user(
        user_id="ru-1",
        instance_id="peer-inst",
        remote_username="bob",
        display_name="Bob Remote",
    )
    svc, _, _ = _make_svc(
        users=[local],
        remote_users=[remote],
        own_instance_id="own-inst",
    )
    contacts = await svc.list_contacts(self_user_id="u-alice")
    # one remote, self excluded from local
    remote_contacts = [c for c in contacts if not c["is_local"]]
    assert len(remote_contacts) == 1
    c = remote_contacts[0]
    assert c["instance_id"] == "peer-inst"
    assert c["user_ref"] == "bob"
    assert c["display_name"] == "Bob Remote"
    assert c["is_local"] is False
    assert c["online"] is False  # remote presence always False (deferred)


@pytest.mark.asyncio
async def test_list_contacts_remote_falls_back_to_username_when_display_name_empty():
    """When a remote user has no display_name, user_ref (remote_username) is used."""
    local = _make_user("u-alice", "alice", "Alice")
    remote = _make_remote_user(
        user_id="ru-2",
        instance_id="peer-inst",
        remote_username="charlie",
        display_name="",  # empty — should fall back to remote_username
    )
    svc, _, _ = _make_svc(
        users=[local],
        remote_users=[remote],
        own_instance_id="own-inst",
    )
    contacts = await svc.list_contacts(self_user_id="u-alice")
    remote_contacts = [c for c in contacts if not c["is_local"]]
    assert len(remote_contacts) == 1
    assert remote_contacts[0]["display_name"] == "charlie"


@pytest.mark.asyncio
async def test_list_contacts_empty_when_only_self_and_no_remotes():
    """Single local user with no remotes → empty contact list."""
    alice = _make_user("u-alice", "alice", "Alice")
    svc, _, _ = _make_svc(users=[alice], own_instance_id="own-inst")
    assert await svc.list_contacts(self_user_id="u-alice") == []


@pytest.mark.asyncio
async def test_list_contacts_excludes_blocked_users():
    """Blocked contacts (local and remote) are excluded from list_contacts.

    Matches the /friends and DM roster behaviour: list_blocked(self_user_id)
    provides the block set; any user whose user_id is in the set is dropped
    from both local and remote populations.
    """
    alice = _make_user("u-alice", "alice", "Alice")
    blocked_local = _make_user("u-blocked", "blocked", "Blocked Local")
    visible_local = _make_user("u-visible", "visible", "Visible Local")

    blocked_remote = _make_remote_user(
        user_id="ru-blocked",
        instance_id="peer-inst",
        remote_username="blocked-remote",
        display_name="Blocked Remote",
    )
    visible_remote = _make_remote_user(
        user_id="ru-visible",
        instance_id="peer-inst",
        remote_username="visible-remote",
        display_name="Visible Remote",
    )

    svc, _, _ = _make_svc(
        users=[alice, blocked_local, visible_local],
        remote_users=[blocked_remote, visible_remote],
        own_instance_id="own-inst",
        blocked_ids={"u-blocked", "ru-blocked"},
    )
    contacts = await svc.list_contacts(self_user_id="u-alice")
    user_refs = {c["user_ref"] for c in contacts}

    # Blocked contacts must not appear.
    assert "u-blocked" not in user_refs, "blocked local user must be excluded"
    assert "blocked-remote" not in user_refs, "blocked remote user must be excluded"

    # Non-blocked contacts must appear.
    assert "u-visible" in user_refs, "non-blocked local user must be included"
    assert "visible-remote" in user_refs, "non-blocked remote user must be included"


# ─── AppChallengeReceived publish (Task 7) ─────────────────────────────────


@pytest.mark.asyncio
async def test_local_loopback_open_publishes_challenge_for_target_only():
    """A local-loopback open publishes one AppChallengeReceived addressed to
    the target, carrying the initiator's display name — never for the
    initiator."""
    from socialhome.domain.events import AppChallengeReceived

    app = _make_installed_app("chess")
    alice = _make_user("u-alice", "alice", "Alice")
    bob = _make_user("u-bob", "bob", "Bob")
    bus = _FakeBus()
    svc, _, _ = _make_svc(
        apps={"chess": app},
        users=[alice, bob],
        own_instance_id="own-inst",
        bus=bus,
    )
    session_id = await svc.open_session(
        app_id="chess",
        target={"instance_id": "own-inst", "user_ref": "u-bob", "is_local": True},
        actor_user_id="u-alice",
    )
    challenges = [e for e in bus.published if isinstance(e, AppChallengeReceived)]
    assert len(challenges) == 1
    ev = challenges[0]
    assert ev.to_user_id == "u-bob"
    assert ev.from_display == "Alice"
    assert ev.app_id == "chess"
    assert ev.session_id == session_id
    # The initiator is never the recipient of a challenge.
    assert all(e.to_user_id != "u-alice" for e in challenges)


@pytest.mark.asyncio
async def test_inbound_resolved_open_publishes_challenge():
    """An inbound APP_SESSION open routed to a single local user publishes a
    challenge with the remote initiator's display name."""
    from socialhome.domain.events import AppChallengeReceived

    app = _make_installed_app("chess")
    bob = _make_user("u-bob", "bob", "Bob")
    remote = _make_remote_user(
        user_id="ru-carol",
        instance_id="peer-inst-id",
        remote_username="carol",
        display_name="Carol Remote",
    )
    bus = _FakeBus()
    svc, _, ws = _make_svc(
        apps={"chess": app},
        users=[bob],
        remote_users=[remote],
        bus=bus,
    )
    event = _make_event(
        FederationEventType.APP_SESSION,
        {
            "app_id": "chess",
            "session_id": "sess-1",
            "verb": "open",
            "to_user": "bob",
            "from_user": "carol",
        },
    )
    await svc.on_inbound_event(event)
    # Delivered to the resolved local user.
    assert any(c["user_id"] == "u-bob" for c in ws.user_calls)
    challenges = [e for e in bus.published if isinstance(e, AppChallengeReceived)]
    assert len(challenges) == 1
    ev = challenges[0]
    assert ev.to_user_id == "u-bob"
    assert ev.from_display == "Carol Remote"
    assert ev.session_id == "sess-1"


@pytest.mark.asyncio
async def test_inbound_message_does_not_publish_challenge():
    """An inbound APP_MESSAGE (kind=message) never publishes a challenge."""
    from socialhome.domain.events import AppChallengeReceived

    app = _make_installed_app("chess")
    bob = _make_user("u-bob", "bob", "Bob")
    bus = _FakeBus()
    svc, _, _ = _make_svc(apps={"chess": app}, users=[bob], bus=bus)
    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {
            "app_id": "chess",
            "session_id": "sess-1",
            "to_user": "bob",
            "from_user": "carol",
            "data": {"move": "e4"},
        },
    )
    await svc.on_inbound_event(event)
    assert not [e for e in bus.published if isinstance(e, AppChallengeReceived)]


@pytest.mark.asyncio
async def test_legacy_broadcast_open_does_not_publish_challenge():
    """A legacy household-addressed open (no to_user) fans out to everyone
    and publishes no challenge (no specific recipient)."""
    from socialhome.domain.events import AppChallengeReceived

    app = _make_installed_app("chess")
    users = [_make_user("u-bob", "bob", "Bob"), _make_user("u-eve", "eve", "Eve")]
    bus = _FakeBus()
    svc, _, ws = _make_svc(apps={"chess": app}, users=users, bus=bus)
    event = _make_event(
        FederationEventType.APP_SESSION,
        {"app_id": "chess", "session_id": "sess-1", "verb": "open"},
    )
    await svc.on_inbound_event(event)
    # Legacy fan-out still happens (broadcast_to_users).
    assert ws.calls
    assert not [e for e in bus.published if isinstance(e, AppChallengeReceived)]


@pytest.mark.asyncio
async def test_duplicate_inbound_open_delivers_and_publishes_once():
    """A re-delivered inbound open (same session_id) is idempotent: it
    delivers and publishes exactly once."""
    from socialhome.domain.events import AppChallengeReceived

    app = _make_installed_app("chess")
    bob = _make_user("u-bob", "bob", "Bob")
    remote = _make_remote_user(
        user_id="ru-carol",
        instance_id="peer-inst-id",
        remote_username="carol",
        display_name="Carol Remote",
    )
    bus = _FakeBus()
    svc, _, ws = _make_svc(
        apps={"chess": app}, users=[bob], remote_users=[remote], bus=bus
    )
    payload = {
        "app_id": "chess",
        "session_id": "dup-sess",
        "verb": "open",
        "to_user": "bob",
        "from_user": "carol",
    }
    await svc.on_inbound_event(_make_event(FederationEventType.APP_SESSION, payload))
    await svc.on_inbound_event(_make_event(FederationEventType.APP_SESSION, payload))
    challenges = [e for e in bus.published if isinstance(e, AppChallengeReceived)]
    assert len(challenges) == 1
    delivered = [c for c in ws.user_calls if c["user_id"] == "u-bob"]
    assert len(delivered) == 1


# ─── Recipient block enforcement on inbound challenge (Fix 1) ─────────────────


@pytest.mark.asyncio
async def test_inbound_challenge_blocked_by_recipient_is_dropped():
    """A challenge from a remote initiator the recipient has BLOCKED is dropped:
    no WS delivery, no AppChallengeReceived publish (symmetric with DMs)."""
    from socialhome.domain.events import AppChallengeReceived

    app = _make_installed_app("chess")
    bob = _make_user("u-bob", "bob", "Bob")
    remote = _make_remote_user(
        user_id="ru-carol",
        instance_id="peer-inst-id",
        remote_username="carol",
        display_name="Carol Remote",
    )
    bus = _FakeBus()
    svc, _, ws = _make_svc(
        apps={"chess": app},
        users=[bob],
        remote_users=[remote],
        # bob (recipient) has blocked carol (remote initiator).
        block_pairs={("u-bob", "ru-carol")},
        bus=bus,
    )
    event = _make_event(
        FederationEventType.APP_SESSION,
        {
            "app_id": "chess",
            "session_id": "sess-1",
            "verb": "open",
            "to_user": "bob",
            "from_user": "carol",
        },
    )
    await svc.on_inbound_event(event)
    # No delivery and no challenge — the block is honoured.
    assert ws.user_calls == []
    assert ws.calls == []
    assert not [e for e in bus.published if isinstance(e, AppChallengeReceived)]


@pytest.mark.asyncio
async def test_inbound_challenge_not_blocked_is_delivered():
    """Control: an unblocked remote initiator's challenge still delivers + publishes."""
    from socialhome.domain.events import AppChallengeReceived

    app = _make_installed_app("chess")
    bob = _make_user("u-bob", "bob", "Bob")
    remote = _make_remote_user(
        user_id="ru-carol",
        instance_id="peer-inst-id",
        remote_username="carol",
        display_name="Carol Remote",
    )
    bus = _FakeBus()
    svc, _, ws = _make_svc(
        apps={"chess": app},
        users=[bob],
        remote_users=[remote],
        bus=bus,
    )
    event = _make_event(
        FederationEventType.APP_SESSION,
        {
            "app_id": "chess",
            "session_id": "sess-1",
            "verb": "open",
            "to_user": "bob",
            "from_user": "carol",
        },
    )
    await svc.on_inbound_event(event)
    assert any(c["user_id"] == "u-bob" for c in ws.user_calls)
    assert len([e for e in bus.published if isinstance(e, AppChallengeReceived)]) == 1


@pytest.mark.asyncio
async def test_inbound_message_blocked_by_recipient_is_dropped():
    """An in-session move from a blocked remote initiator is also dropped."""
    app = _make_installed_app("chess")
    bob = _make_user("u-bob", "bob", "Bob")
    remote = _make_remote_user(
        user_id="ru-carol",
        instance_id="peer-inst-id",
        remote_username="carol",
        display_name="Carol Remote",
    )
    svc, _, ws = _make_svc(
        apps={"chess": app},
        users=[bob],
        remote_users=[remote],
        block_pairs={("u-bob", "ru-carol")},
    )
    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {
            "app_id": "chess",
            "session_id": "sess-1",
            "to_user": "bob",
            "from_user": "carol",
            "data": {"move": "e4"},
        },
    )
    await svc.on_inbound_event(event)
    assert ws.user_calls == []
    assert ws.calls == []


# ─── Pending-session persistence (offline-recipient invite survival) ──────────


@pytest.mark.asyncio
async def test_inbound_open_persists_pending_session_for_resolved_user():
    """A resolved per-user APP_SESSION open persists exactly one pending row
    with the right fields and a tz-aware created_at."""
    from datetime import datetime

    app = _make_installed_app("chess")
    bob = _make_user("u-bob", "bob", "Bob")
    remote = _make_remote_user(
        user_id="ru-carol",
        instance_id="peer-inst-id",
        remote_username="carol",
        display_name="Carol Remote",
    )
    svc, _, _ = _make_svc(apps={"chess": app}, users=[bob], remote_users=[remote])
    payload = {
        "app_id": "chess",
        "session_id": "sess-1",
        "verb": "open",
        "to_user": "bob",
        "from_user": "carol",
    }
    await svc.on_inbound_event(_make_event(FederationEventType.APP_SESSION, payload))

    repo = svc._app_repo  # type: ignore[attr-defined]
    assert len(repo.pending) == 1
    row = repo.pending[0]
    assert row.app_id == "chess"
    assert row.user_id == "u-bob"
    assert row.session_id == "sess-1"
    assert row.from_instance == "peer-inst-id"
    assert row.from_user == "carol"
    assert row.payload == payload
    parsed = datetime.fromisoformat(row.created_at)
    assert parsed.tzinfo is not None


@pytest.mark.asyncio
async def test_inbound_message_does_not_persist_pending_session():
    """An APP_MESSAGE (kind=message) never persists a pending session."""
    app = _make_installed_app("chess")
    bob = _make_user("u-bob", "bob", "Bob")
    remote = _make_remote_user(
        user_id="ru-carol",
        instance_id="peer-inst-id",
        remote_username="carol",
        display_name="Carol Remote",
    )
    svc, _, _ = _make_svc(apps={"chess": app}, users=[bob], remote_users=[remote])
    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {
            "app_id": "chess",
            "session_id": "sess-1",
            "to_user": "bob",
            "from_user": "carol",
            "data": {"move": "e4"},
        },
    )
    await svc.on_inbound_event(event)
    assert svc._app_repo.pending == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_inbound_open_blocked_initiator_does_not_persist():
    """A blocked initiator's open persists nothing (same gate as the bell)."""
    app = _make_installed_app("chess")
    bob = _make_user("u-bob", "bob", "Bob")
    remote = _make_remote_user(
        user_id="ru-carol",
        instance_id="peer-inst-id",
        remote_username="carol",
        display_name="Carol Remote",
    )
    svc, _, _ = _make_svc(
        apps={"chess": app},
        users=[bob],
        remote_users=[remote],
        block_pairs={("u-bob", "ru-carol")},
    )
    event = _make_event(
        FederationEventType.APP_SESSION,
        {
            "app_id": "chess",
            "session_id": "sess-1",
            "verb": "open",
            "to_user": "bob",
            "from_user": "carol",
        },
    )
    await svc.on_inbound_event(event)
    assert svc._app_repo.pending == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_legacy_fanout_open_does_not_persist():
    """A legacy household fan-out open (no to_user) persists nothing."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, _ = _make_svc(apps={"chess": app}, users=users)
    event = _make_event(
        FederationEventType.APP_SESSION,
        {"app_id": "chess", "session_id": "sess-1", "verb": "open"},
    )
    await svc.on_inbound_event(event)
    assert svc._app_repo.pending == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_drain_pending_sessions_returns_and_clears():
    """svc.drain_pending_sessions returns captured rows and clears them."""
    app = _make_installed_app("chess")
    bob = _make_user("u-bob", "bob", "Bob")
    remote = _make_remote_user(
        user_id="ru-carol",
        instance_id="peer-inst-id",
        remote_username="carol",
        display_name="Carol Remote",
    )
    svc, _, _ = _make_svc(apps={"chess": app}, users=[bob], remote_users=[remote])
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_SESSION,
            {
                "app_id": "chess",
                "session_id": "sess-1",
                "verb": "open",
                "to_user": "bob",
                "from_user": "carol",
            },
        )
    )
    drained = await svc.drain_pending_sessions("chess", "u-bob")
    assert len(drained) == 1
    assert drained[0].session_id == "sess-1"
    # A second drain returns nothing — the first cleared it.
    assert await svc.drain_pending_sessions("chess", "u-bob") == []


@pytest.mark.asyncio
async def test_list_contacts_uses_active_users():
    """A soft-deleted/inactive local user is not surfaced as a contact."""
    alice = _make_user("u-alice", "alice", "Alice")
    inactive = dataclasses.replace(
        _make_user("u-gone", "gone", "Gone"),
        state="inactive",
        deleted_at="2026-01-01T00:00:00+00:00",
    )
    svc, _, _ = _make_svc(
        users=[alice, inactive],
        own_instance_id="own-inst",
    )
    contacts = await svc.list_contacts(self_user_id="u-alice")
    refs = {c["user_ref"] for c in contacts}
    assert "u-gone" not in refs


async def test_list_peers_excludes_space_session_households():
    """§D2b — an invite-link household is not offered as an app peer.

    Starting a cross-household app session is a social act; sharing a
    space does not imply consent to it.
    """
    paired = RemoteInstance(
        id="peer-social",
        display_name="Paired",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="k",
        key_remote_to_self="k",
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-1",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    space_only = RemoteInstance(
        id="peer-space",
        display_name="Invite link",
        remote_identity_pk="bb" * 32,
        key_self_to_remote="k",
        key_remote_to_self="k",
        remote_inbox_url="",
        local_inbox_id="wh-2",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.SPACE_SESSION,
    )
    svc, _fed, _ws = _make_svc(instances=[paired, space_only])
    peers = await svc.list_peers()
    assert [p["instance_id"] for p in peers] == ["peer-social"]
