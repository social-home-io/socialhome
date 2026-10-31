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
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from socialhome.domain.apps import (
    ENDED_SESSIONS_MAX,
    INVITES_OPEN_MAX,
    INVITES_PER_DAY_MAX,
    AppAgeRestrictedError,
    AppCatalogEntry,
    AppChallengeHandledError,
    AppContactNotFoundError,
    AppInviteLimitError,
    AppManifest,
    AppNotEnabledError,
    AppNotFoundError,
    AppPendingSession,
    AppPermissionDeniedError,
    AppSessionTakenError,
    AppSessionUnknownError,
    InstalledApp,
)
from socialhome.domain.events import (
    AppChallengeDeclined,
    AppChallengeEnded,
    AppChallengeReceived,
    AppChallengeWaiting,
    AppEnabledChanged,
    AppGameEnded,
    AppInstalled,
    AppTurn,
    AppTurnsSeen,
)
from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.notification import (
    Notification,
    NotificationActor,
    NotificationHandled,
    NotificationParams,
)
from socialhome.domain.space import SpaceMember, SpaceRole, SpaceType
from socialhome.domain.user import RemoteUser, User
from socialhome.services.app_federation_service import (
    AppFederationService,
    with_household,
)

# ─── Stubs ────────────────────────────────────────────────────────────────────


def _make_installed_app(
    app_id: str = "chess",
    *,
    enabled: bool = True,
    min_age: int = 0,
    capabilities: tuple[str, ...] = ("storage", "federation"),
) -> InstalledApp:
    return InstalledApp(
        app_id=app_id,
        name="Chess",
        version="1.0.0",
        enabled=enabled,
        manifest=AppManifest(entry="index.html", icon=None, capabilities=capabilities),
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

    @property
    def invites(self) -> list:
        """The rows still waiting as invites (not games being played)."""
        return [p for p in self.pending if p.state == "invited"]

    async def add_pending_session(self, s) -> None:
        self.pending.append(s)

    async def get_pending_session(self, app_id, user_id, session_id):
        for p in self.pending:
            if (p.app_id, p.user_id, p.session_id) == (app_id, user_id, session_id):
                return p
        return None

    async def take_pending_session(
        self, app_id, user_id, session_id, *, from_instance=None, from_user=None
    ):
        p = await self.get_pending_session(app_id, user_id, session_id)
        if p is None or p.state != "invited":
            return None
        if from_instance is not None and p.from_instance != from_instance:
            return None
        if from_user is not None and p.from_user != from_user:
            return None
        self.pending.remove(p)
        return p

    def _swap(self, old, new) -> None:
        self.pending[self.pending.index(old)] = new

    async def activate_pending_session(self, app_id, user_id, session_id, *, now):
        p = await self.get_pending_session(app_id, user_id, session_id)
        if p is None or p.state != "invited":
            return None
        self._swap(p, dataclasses.replace(p, state="active", updated_at=now))
        return p

    async def delete_pending_session(self, app_id, user_id, session_id):
        p = await self.get_pending_session(app_id, user_id, session_id)
        if p is not None:
            self.pending.remove(p)

    async def add_active_session(self, s) -> None:
        if await self.get_pending_session(s.app_id, s.user_id, s.session_id) is None:
            self.pending.append(dataclasses.replace(s, state="active"))

    async def touch_session(self, app_id, user_id, session_id, *, now):
        p = await self.get_pending_session(app_id, user_id, session_id)
        if p is not None:
            self._swap(p, dataclasses.replace(p, updated_at=now))
        self.touched = getattr(self, "touched", 0) + 1

    async def hold_message(
        self, app_id, user_id, session_id, *, from_instance, from_user, held, now
    ) -> bool:
        p = await self.get_pending_session(app_id, user_id, session_id)
        if p is None or (p.from_instance, p.from_user) != (from_instance, from_user):
            return False
        payload = dict(p.payload)
        if payload.get("sent") is True:
            payload["answered"] = True
        self._swap(
            p, dataclasses.replace(p, held=held, updated_at=now, payload=payload)
        )
        return True

    async def list_sessions(self, app_id, user_id):
        rows = [p for p in self.pending if (p.app_id, p.user_id) == (app_id, user_id)]
        return sorted(rows, key=lambda p: p.last_change, reverse=True)

    async def withdraw_session(self, app_id, user_id, session_id):
        p = await self.get_pending_session(app_id, user_id, session_id)
        if p is not None:
            self._swap(
                p, dataclasses.replace(p, payload={**p.payload, "withdrawn": True})
            )

    async def list_app_sessions(self, app_id):
        return [p for p in self.pending if p.app_id == app_id]

    async def kv_people(self, app_id):
        return getattr(self, "people", 0)

    async def take_held(self, app_id, user_id):
        out = [
            p
            for p in self.pending
            if (p.app_id, p.user_id) == (app_id, user_id)
            and p.held is not None
            and p.state == "active"
        ]
        for p in out:
            self._swap(p, dataclasses.replace(p, held=None))
        return sorted(out, key=lambda p: p.last_change)


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

    async def get_remote(self, user_id: str) -> RemoteUser | None:
        return next((r for r in self._remote_users if r.user_id == user_id), None)

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
        #: Per-household overrides of :attr:`peer_version`.
        self.peer_versions: dict[str, int] = {}
        #: Households whose send raises (a dead link).
        self.failing: set[str] = set()

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        return self.peer_versions.get(instance_id, self.peer_version) >= min_version

    async def send_event(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str | None = None,
    ):
        if to_instance_id in self.failing:
            raise ConnectionError("peer unreachable")
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
        turn: bool = False,
    ):
        self.sent_app_messages.append(
            {
                "to_instance_id": to_instance_id,
                "app_id": app_id,
                "session_id": session_id,
                "payload": payload,
                "to_user": to_user,
                "from_user": from_user,
                "turn": turn,
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


async def _know(
    svc: AppFederationService,
    user_id: str,
    session_id: str,
    target: dict,
    *,
    app_id: str = "chess",
) -> None:
    """*user_id* sent *session_id* to *target* through the invite sheet: the
    session row :meth:`open_session` keeps (D3 — only known sessions may be
    messaged)."""
    await svc._know_game(
        app_id, user_id, session_id, str(target["instance_id"]), target
    )


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
async def test_open_session_local_loopback_keeps_the_invite_for_the_target_only():
    """Local target → no fed send and no live frame into anyone's app (D3:
    the invite waits in Needs you until Play); the invite is kept for the
    target only. Never an echo to the initiator either."""
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
    assert ws.user_calls == []
    (invite,) = svc._app_repo.invites
    assert (invite.user_id, invite.session_id) == ("u-target", session_id)
    assert (invite.from_instance, invite.from_user) == ("own-inst-id", "u-init")


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
async def test_open_session_refuses_a_household_too_old_to_address_a_person():
    """peer_version=17 → its people aren't in the sheet and an open is
    refused: it could only reach the whole household (D3)."""
    app = _make_installed_app("chess")
    svc, federation, _ = _make_svc(
        apps={"chess": app},
        users=[_make_user("u-init", "alice", "Alice")],
        remote_users=[_make_remote_user(instance_id="peer-1", remote_username="bob")],
        peer_version=17,
    )
    assert await svc.list_invitable(app_id="chess", self_user_id="u-init") == []
    with pytest.raises(AppContactNotFoundError):
        await svc.open_session(
            app_id="chess",
            target=_remote_target("peer-1", "bob"),
            actor_user_id="u-init",
        )
    assert federation.sent_events == []


@pytest.mark.asyncio
async def test_open_session_local_loopback_excludes_protected_minor_target():
    """A local under-age protected minor isn't in the invite sheet, so an
    invite to them is refused — nobody hears it."""
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
    assert await svc.list_invitable(app_id="chess", self_user_id="u-init") == []
    with pytest.raises(AppContactNotFoundError):
        await svc.open_session(
            app_id="chess",
            target=_local_target("u-target"),
            actor_user_id="u-init",
        )
    assert federation.sent_events == []
    assert ws.user_calls == []
    assert svc._app_repo.pending == []


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
    await _know(svc, "user-1", "session-abc", _remote_target("peer-1", "bob"))
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
    await _know(svc, "u-init", "sess-local", _local_target("u-target"))
    # Bob plays it (he pressed Play): his row names Alice.
    await _know(svc, "u-target", "sess-local", _local_target("u-init"))
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
    await _know(svc, "u-init", "s1", _remote_target("peer-1", "bob"))
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
async def test_send_message_refuses_a_household_too_old_to_address_a_person():
    """peer_version=17 → even a known game sends nothing: it would reach
    the whole household."""
    app = _make_installed_app("chess")
    svc, federation, _ = _make_svc(
        apps={"chess": app},
        users=[_make_user("u-init", "alice", "Alice")],
        remote_users=[_make_remote_user(instance_id="peer-1", remote_username="bob")],
        peer_version=17,
    )
    await _know(svc, "u-init", "s1", _remote_target("peer-1", "bob"))
    with pytest.raises(AppContactNotFoundError):
        await svc.send_message(
            app_id="chess",
            session_id="s1",
            target=_remote_target("peer-1", "bob"),
            payload={"move": "e2e4"},
            actor_user_id="u-init",
        )
    assert federation.sent_app_messages == []


@pytest.mark.asyncio
async def test_send_message_rejects_blocked_target():
    """A game with someone the actor blocked since → send_message raises."""
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
        block_pairs={("u-init", "ru-blocked")},
    )
    await _know(svc, "u-init", "s1", _remote_target("peer-1", "bob"))
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
    svc, _, ws = _make_svc(apps={"chess": app}, users=users, peer_version=17)

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
    svc, _, ws = _make_svc(apps={"chess": app}, users=users, peer_version=17)

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
    """Binary-channel path delivers to users just like the JSON event path
    (an older, household-addressed sender)."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users, peer_version=17)

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
    """A non-empty to_user resolves to a local user: the invite is kept for
    them only — no fan-out, and no live frame into their app (D3)."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, ws = _make_svc(
        apps={"chess": app},
        users=users,
        remote_users=[_make_remote_user(remote_username="remote-alice")],
    )

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

    assert ws.calls == []
    assert ws.user_calls == []
    (invite,) = svc._app_repo.invites
    assert (invite.user_id, invite.session_id) == ("u2", "sess-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "body", "kind"),
    [
        (FederationEventType.APP_SESSION, {"verb": "close"}, "session"),
        (FederationEventType.APP_MESSAGE, {"data": {"move": "e2e4"}}, "message"),
    ],
)
async def test_inbound_per_user_frame_names_the_sender(event_type, body, kind):
    """A remote frame carries ``from_user`` like the same-household one.

    The app needs it to tell which friend a move or invite came from (and to
    answer them: it is the ``user_ref`` of that friend's contact). It is the
    sending household's user, proven a user of the signature-checked
    ``from_instance``.
    """
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, ws = _make_svc(
        apps={"chess": app},
        users=users,
        remote_users=[_make_remote_user(remote_username="remote-alice")],
    )
    # Bob plays sess-1 with remote Alice (D3: only then does it reach his app).
    await _know(svc, "u2", "sess-1", _remote_target("peer-inst-id", "remote-alice"))

    await svc.on_inbound_event(
        _make_event(
            event_type,
            {
                "app_id": "chess",
                "session_id": "sess-1",
                "to_user": "bob",
                "from_user": "remote-alice",
                **body,
            },
        )
    )

    [call] = ws.user_calls
    assert call["user_id"] == "u2"
    frame = call["payload"]
    assert frame["kind"] == kind
    assert frame["from_instance"] == "peer-inst-id"
    assert frame["from_user"] == "remote-alice"


@pytest.mark.asyncio
async def test_inbound_household_frame_names_only_a_known_sender():
    """The older-peer fan-out names the sender only when it is a known user.

    A ``from_user`` that is not a user of the sending household is not put
    on the frame; a frame with no sender named has no ``from_user`` at all.
    """
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice")]
    svc, _, ws = _make_svc(
        apps={"chess": app},
        users=users,
        peer_version=17,
        remote_users=[_make_remote_user(remote_username="remote-alice")],
    )

    for from_user in ("remote-alice", "nobody", None):
        payload = {"app_id": "chess", "session_id": "s", "data": {}}
        if from_user is not None:
            payload["from_user"] = from_user
        await svc.on_inbound_event(
            _make_event(FederationEventType.APP_MESSAGE, payload)
        )

    known, unknown, unnamed = (c["payload"] for c in ws.calls)
    assert known["from_user"] == "remote-alice"
    assert "from_user" not in unknown
    assert "from_user" not in unnamed


@pytest.mark.asyncio
async def test_inbound_empty_to_user_falls_back_to_broadcast():
    """An empty-string to_user (legacy household open) falls back to broadcast-all."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users, peer_version=17)

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
    svc, _, ws = _make_svc(apps={"chess": app}, users=users, peer_version=17)

    event = _make_event(
        FederationEventType.APP_MESSAGE,
        {"app_id": "chess", "session_id": "sess-1", "data": {"move": "e4"}},
    )
    await svc.on_inbound_event(event)

    assert ws.user_calls == []
    assert len(ws.calls) == 1
    assert set(ws.calls[0]["user_ids"]) == {"u1", "u2"}


@pytest.mark.asyncio
async def test_inbound_unresolvable_to_user_reaches_nobody():
    """A to_user that resolves to no local user is refused — never widened to
    the whole household."""
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
    assert ws.calls == []


@pytest.mark.asyncio
async def test_inbound_addressing_peer_without_to_user_reaches_nobody():
    """A v_18+ household always names the addressee on the JSON path."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users, peer_version=18)
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_MESSAGE,
            {"app_id": "chess", "session_id": "s", "data": {}},
        )
    )
    assert ws.calls == [] and ws.user_calls == []


@pytest.mark.asyncio
async def test_household_fanout_skips_recipients_who_blocked_the_initiator():
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, ws = _make_svc(
        apps={"chess": app},
        users=users,
        remote_users=[_make_remote_user(remote_username="eve")],
        block_pairs={("u1", "remote-u1")},
        peer_version=17,
    )
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_MESSAGE,
            {"app_id": "chess", "session_id": "s", "from_user": "eve", "data": {}},
        )
    )
    assert ws.calls[0]["user_ids"] == ["u2"]


@pytest.mark.asyncio
async def test_inbound_binary_frame_without_people_broadcasts():
    """A binary frame naming nobody (older sender) → household fan-out."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users, peer_version=17)

    await svc.on_inbound_message("peer-inst-id", "chess", "sess-bin", {"move": "d5"})

    assert ws.user_calls == []
    assert len(ws.calls) == 1
    assert set(ws.calls[0]["user_ids"]) == {"u1", "u2"}


@pytest.mark.asyncio
async def test_inbound_binary_frame_goes_to_the_addressed_person_and_names_the_sender():
    """A binary move naming both people is routed and labelled like JSON."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, ws = _make_svc(
        apps={"chess": app},
        users=users,
        remote_users=[_make_remote_user(remote_username="remote-alice")],
    )
    await _know(svc, "u2", "sess-bin", _remote_target("peer-inst-id", "remote-alice"))

    await svc.on_inbound_message(
        "peer-inst-id",
        "chess",
        "sess-bin",
        {"move": "d5"},
        to_user="bob",
        from_user="remote-alice",
    )

    assert ws.calls == []
    [call] = ws.user_calls
    assert call["user_id"] == "u2"
    assert call["payload"]["kind"] == "message"
    assert call["payload"]["from_user"] == "remote-alice"


@pytest.mark.asyncio
async def test_inbound_binary_frame_from_an_unknown_sender_is_refused():
    """A binary frame addressed to a person must name a known sender."""
    app = _make_installed_app("chess")
    svc, _, ws = _make_svc(
        apps={"chess": app},
        users=[_make_user("u2", "bob")],
        remote_users=[_make_remote_user(remote_username="remote-alice")],
    )

    await svc.on_inbound_message(
        "peer-inst-id", "chess", "s", {}, to_user="bob", from_user="mallory"
    )

    assert ws.calls == [] and ws.user_calls == []


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
async def test_a_household_addressed_target_is_refused():
    """D3: a household-addressed target (user_ref == "") names nobody anyone
    picked in the invite sheet — no open, no message."""
    app = _make_installed_app("chess")
    svc, federation, _ = _make_svc(
        apps={"chess": app},
        users=[_make_user("u-init", "alice", "Alice")],
        peer_version=17,
    )
    target = {"instance_id": "peer-1", "user_ref": "", "is_local": False}
    with pytest.raises(AppContactNotFoundError):
        await svc.open_session(app_id="chess", target=target, actor_user_id="u-init")
    with pytest.raises(AppSessionUnknownError):
        await svc.send_message(
            app_id="chess",
            session_id="s1",
            target=target,
            payload={"x": 1},
            actor_user_id="u-init",
        )
    assert federation.sent_events == []
    assert federation.sent_app_messages == []


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
    await _know(svc, "teen1", "s1", _remote_target("peer-1", "bob"))
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
    svc, _, ws = _make_svc(
        apps={"chess": app}, users=users, cp_repo=cp, peer_version=17
    )

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
    svc, _, ws = _make_svc(
        apps={"chess": app}, users=users, cp_repo=cp, peer_version=17
    )

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
    svc, _, ws = _make_svc(
        apps={"chess": app}, users=users, cp_repo=cp, peer_version=17
    )

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
async def test_list_invitable_excludes_self():
    """The calling user must not appear in their own contact list."""
    alice = _make_user("u-alice", "alice", "Alice")
    bob = _make_user("u-bob", "bob", "Bob")
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app()},
        users=[alice, bob],
        own_instance_id="own-inst",
    )
    contacts = await svc.list_invitable(app_id="chess", self_user_id="u-alice")
    ids = [c["user_ref"] for c in contacts]
    assert "u-alice" not in ids
    assert "u-bob" in ids


@pytest.mark.asyncio
async def test_list_invitable_local_member_shape():
    """A local household member appears with is_local=True and the own instance_id."""
    alice = _make_user("u-alice", "alice", "Alice")
    bob = _make_user("u-bob", "bob", "Bob")
    svc, _, _ws = _make_svc(
        apps={"chess": _make_installed_app()},
        users=[alice, bob],
        online_user_ids={"u-bob"},
        own_instance_id="own-inst",
    )
    contacts = await svc.list_invitable(app_id="chess", self_user_id="u-alice")
    assert len(contacts) == 1
    c = contacts[0]
    assert c["instance_id"] == "own-inst"
    assert c["user_ref"] == "u-bob"
    assert c["display_name"] == "Bob"
    assert c["is_local"] is True
    assert c["online"] is True  # bob has a live WS session


@pytest.mark.asyncio
async def test_list_invitable_local_member_offline():
    """A local member with no WS session is online=False."""
    alice = _make_user("u-alice", "alice", "Alice")
    bob = _make_user("u-bob", "bob", "Bob")
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app()},
        users=[alice, bob],
        online_user_ids=set(),  # nobody online
        own_instance_id="own-inst",
    )
    contacts = await svc.list_invitable(app_id="chess", self_user_id="u-alice")
    assert len(contacts) == 1
    assert contacts[0]["online"] is False


@pytest.mark.asyncio
async def test_list_invitable_remote_user_shape():
    """A known remote user appears with is_local=False, correct instance_id and user_ref."""
    local = _make_user("u-alice", "alice", "Alice")
    remote = _make_remote_user(
        user_id="ru-1",
        instance_id="peer-inst",
        remote_username="bob",
        display_name="Bob Remote",
    )
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app()},
        users=[local],
        remote_users=[remote],
        own_instance_id="own-inst",
    )
    contacts = await svc.list_invitable(app_id="chess", self_user_id="u-alice")
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
async def test_list_invitable_remote_falls_back_to_username_when_display_name_empty():
    """When a remote user has no display_name, user_ref (remote_username) is used."""
    local = _make_user("u-alice", "alice", "Alice")
    remote = _make_remote_user(
        user_id="ru-2",
        instance_id="peer-inst",
        remote_username="charlie",
        display_name="",  # empty — should fall back to remote_username
    )
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app()},
        users=[local],
        remote_users=[remote],
        own_instance_id="own-inst",
    )
    contacts = await svc.list_invitable(app_id="chess", self_user_id="u-alice")
    remote_contacts = [c for c in contacts if not c["is_local"]]
    assert len(remote_contacts) == 1
    assert remote_contacts[0]["display_name"] == "charlie"


@pytest.mark.asyncio
async def test_list_invitable_empty_when_only_self_and_no_remotes():
    """Single local user with no remotes → empty contact list."""
    alice = _make_user("u-alice", "alice", "Alice")
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app()}, users=[alice], own_instance_id="own-inst"
    )
    assert await svc.list_invitable(app_id="chess", self_user_id="u-alice") == []


@pytest.mark.asyncio
async def test_list_invitable_excludes_blocked_users():
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
        apps={"chess": _make_installed_app()},
        users=[alice, blocked_local, visible_local],
        remote_users=[blocked_remote, visible_remote],
        own_instance_id="own-inst",
        blocked_ids={"u-blocked", "ru-blocked"},
    )
    contacts = await svc.list_invitable(app_id="chess", self_user_id="u-alice")
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
    # Never shown; the bell checks it against blocks and guardian blocks.
    assert ev.from_user_id == "u-alice"
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
    # Kept for Bob (Needs you) — never a live frame into his app (D3).
    assert [p.user_id for p in svc._app_repo.invites] == ["u-bob"]
    assert ws.user_calls == []
    challenges = [e for e in bus.published if isinstance(e, AppChallengeReceived)]
    assert len(challenges) == 1
    ev = challenges[0]
    assert ev.to_user_id == "u-bob"
    assert ev.from_display == "Carol Remote"
    assert ev.from_user_id == "ru-carol"
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
    svc, _, ws = _make_svc(apps={"chess": app}, users=users, bus=bus, peer_version=17)
    event = _make_event(
        FederationEventType.APP_SESSION,
        {"app_id": "chess", "session_id": "sess-1", "verb": "open"},
    )
    await svc.on_inbound_event(event)
    # Legacy fan-out still happens (broadcast_to_users).
    assert ws.calls
    assert not [e for e in bus.published if isinstance(e, AppChallengeReceived)]


@pytest.mark.asyncio
async def test_duplicate_inbound_open_is_kept_and_published_once():
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
    assert len(svc._app_repo.invites) == 1
    assert ws.user_calls == []


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
    # Kept for Bob (Needs you) — never a live frame into his app (D3).
    assert [p.user_id for p in svc._app_repo.invites] == ["u-bob"]
    assert ws.user_calls == []
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
    assert len(repo.invites) == 1
    row = repo.invites[0]
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
    assert svc._app_repo.invites == []  # type: ignore[attr-defined]


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
    assert svc._app_repo.invites == []  # type: ignore[attr-defined]


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
    assert svc._app_repo.invites == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_list_invitable_uses_active_users():
    """A soft-deleted/inactive local user is not surfaced as a contact."""
    alice = _make_user("u-alice", "alice", "Alice")
    inactive = dataclasses.replace(
        _make_user("u-gone", "gone", "Gone"),
        state="inactive",
        deleted_at="2026-01-01T00:00:00+00:00",
    )
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app()},
        users=[alice, inactive],
        own_instance_id="own-inst",
    )
    contacts = await svc.list_invitable(app_id="chess", self_user_id="u-alice")
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


@pytest.mark.asyncio
@pytest.mark.parametrize("from_user", [None, "stranger"], ids=["unnamed", "unknown"])
async def test_per_user_open_needs_an_initiator_of_the_sending_household(from_user):
    """A per-user open always names a user of the sending household, so the
    recipient's block list can be applied; otherwise nothing is delivered."""
    app = _make_installed_app("chess")
    svc, _, ws = _make_svc(apps={"chess": app}, users=[_make_user("u2", "bob")])
    payload = {"app_id": "chess", "session_id": "s", "verb": "open", "to_user": "bob"}
    if from_user is not None:
        payload["from_user"] = from_user
    await svc.on_inbound_event(_make_event(FederationEventType.APP_SESSION, payload))
    assert ws.user_calls == []
    assert svc._app_repo.invites == []


@pytest.mark.asyncio
async def test_open_dedup_is_per_sending_household():
    """Another household reusing a session id never suppresses the real open."""
    app = _make_installed_app("chess")
    svc, _, _ws = _make_svc(
        apps={"chess": app},
        users=[_make_user("u2", "bob")],
        remote_users=[
            _make_remote_user(remote_username="eve", instance_id="other-inst"),
            _make_remote_user(user_id="r2", remote_username="alice"),
        ],
    )
    for sender, who in (("other-inst", "eve"), ("peer-inst-id", "alice")):
        await svc.on_inbound_event(
            _make_event(
                FederationEventType.APP_SESSION,
                {
                    "app_id": "chess",
                    "session_id": "s",
                    "verb": "open",
                    "to_user": "bob",
                    "from_user": who,
                },
                from_instance=sender,
            )
        )
    assert [p.from_instance for p in svc._app_repo.invites] == [
        "other-inst",
        "peer-inst-id",
    ]


@pytest.mark.asyncio
async def test_open_session_refuses_a_local_target_who_blocked_the_actor():
    """§Privacy — the blocked person can't challenge the blocker either, as a
    remote recipient's household already drops a blocked initiator's frames.
    Same answer as a non-contact, so the block isn't revealed."""
    app = _make_installed_app("chess")
    svc, _, ws = _make_svc(
        apps={"chess": app},
        users=[_make_user("u-bob", "bob", "Bob"), _make_user("u-ann", "ann", "Ann")],
        block_pairs={("u-ann", "u-bob")},
    )
    with pytest.raises(AppContactNotFoundError):
        await svc.open_session(
            app_id="chess", target=_local_target("u-ann"), actor_user_id="u-bob"
        )
    # Not even in a game they had before the block.
    await _know(svc, "u-bob", "s1", _local_target("u-ann"))
    with pytest.raises(AppContactNotFoundError):
        await svc.send_message(
            app_id="chess",
            session_id="s1",
            target=_local_target("u-ann"),
            payload={"move": "e4"},
            actor_user_id="u-bob",
        )
    assert ws.user_calls == []


# ─── Game invites: kept, taken in, declined, withdrawn ───────────────────────


def _ended(bus: _FakeBus) -> list:
    return [e for e in bus.published if isinstance(e, AppChallengeEnded)]


async def _remote_invite(svc, session_id: str = "sess-1") -> None:
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_SESSION,
            {
                "app_id": "chess",
                "session_id": session_id,
                "verb": "open",
                "to_user": "bob",
                "from_user": "carol",
            },
        )
    )


def _carol() -> RemoteUser:
    return _make_remote_user(
        user_id="ru-carol", instance_id="peer-inst-id", remote_username="carol"
    )


@pytest.mark.asyncio
async def test_a_local_invite_waits_for_the_invitee_like_a_remote_one():
    """A challenge to someone on this household is kept for them, so it is
    still there when they open the app later (and "Needs you" can check it)."""
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-alice", "alice", "Alice"), _make_user("u-bob", "bob")],
    )
    session_id = await svc.open_session(
        app_id="chess", target=_local_target("u-bob"), actor_user_id="u-alice"
    )
    (row,) = svc._app_repo.invites
    assert (row.user_id, row.session_id) == ("u-bob", session_id)
    assert (row.from_instance, row.from_user) == ("own-inst-id", "u-alice")
    assert row.payload == {"app_id": "chess", "session_id": session_id, "verb": "open"}


@pytest.mark.asyncio
async def test_play_takes_in_only_that_invite():
    """D12: "Play" accepts the one invite it is on; the others keep waiting
    in Needs you."""
    bus = _FakeBus()
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-bob", "bob", "Bob")],
        remote_users=[_carol()],
        bus=bus,
    )
    await _remote_invite(svc, "s1")
    await _remote_invite(svc, "s2")
    invite = await svc.accept_session(
        app_id="chess", session_id="s1", actor_user_id="u-bob"
    )
    assert (invite.session_id, invite.from_instance, invite.from_user) == (
        "s1",
        "peer-inst-id",
        "carol",
    )
    assert invite.payload["verb"] == "open"
    (ended,) = _ended(bus)
    assert (ended.user_id, ended.session_ids, ended.outcome) == (
        "u-bob",
        ("s1",),
        "accepted",
    )
    assert [p.session_id for p in svc._app_repo.invites] == ["s2"]  # type: ignore[attr-defined]
    # A second tap (another device) hears it was handled; nothing else ends.
    with pytest.raises(AppChallengeHandledError):
        await svc.accept_session(app_id="chess", session_id="s1", actor_user_id="u-bob")
    assert len(_ended(bus)) == 1


@pytest.mark.asyncio
async def test_only_the_invitee_can_play_an_invite():
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-alice", "alice", "Alice"), _make_user("u-bob", "bob")],
    )
    session_id = await svc.open_session(
        app_id="chess", target=_local_target("u-bob"), actor_user_id="u-alice"
    )
    with pytest.raises(AppChallengeHandledError):
        await svc.accept_session(
            app_id="chess", session_id=session_id, actor_user_id="u-alice"
        )
    assert len(svc._app_repo.invites) == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_play_needs_the_app_enabled():
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app("chess", enabled=False)},
        users=[_make_user("u-bob", "bob", "Bob")],
    )
    with pytest.raises(AppNotEnabledError):
        await svc.accept_session(app_id="chess", session_id="s1", actor_user_id="u-bob")


@pytest.mark.asyncio
async def test_an_expired_invite_cannot_be_played():
    bus = _FakeBus()
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-bob", "bob", "Bob")],
        remote_users=[_carol()],
        bus=bus,
    )
    await _remote_invite(svc, "s1")
    repo = svc._app_repo  # type: ignore[attr-defined]
    repo.pending = [
        dataclasses.replace(p, created_at="2020-01-01T00:00:00+00:00")
        for p in repo.pending
    ]
    with pytest.raises(AppChallengeHandledError):
        await svc.accept_session(app_id="chess", session_id="s1", actor_user_id="u-bob")
    (ended,) = _ended(bus)
    assert (ended.session_ids, ended.outcome) == (("s1",), "expired")


@pytest.mark.asyncio
async def test_a_move_by_the_invitee_does_not_take_the_invite_in():
    """D3/D12: only Play takes an invite in. A message an app sends in a
    session that still waits in Needs you is refused, and the invite keeps
    waiting."""
    bus = _FakeBus()
    svc, fed, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-bob", "bob", "Bob")],
        remote_users=[_carol()],
        bus=bus,
    )
    await _remote_invite(svc, "s1")
    with pytest.raises(AppSessionUnknownError):
        await svc.send_message(
            app_id="chess",
            session_id="s1",
            target=_remote_target("peer-inst-id", "carol"),
            payload={"move": "e7e5"},
            actor_user_id="u-bob",
        )
    assert _ended(bus) == []
    assert [p.session_id for p in svc._app_repo.invites] == ["s1"]  # type: ignore[attr-defined]
    assert fed.sent_app_messages == []


@pytest.mark.asyncio
async def test_a_move_by_the_challenger_leaves_the_invite_waiting():
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-alice", "alice", "Alice"), _make_user("u-bob", "bob")],
    )
    session_id = await svc.open_session(
        app_id="chess", target=_local_target("u-bob"), actor_user_id="u-alice"
    )
    await svc.send_message(
        app_id="chess",
        session_id=session_id,
        target=_local_target("u-bob"),
        payload={"move": "e2e4"},
        actor_user_id="u-alice",
    )
    assert [p.user_id for p in svc._app_repo.invites] == ["u-bob"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_declining_a_remote_invite_tells_the_challengers_app():
    bus = _FakeBus()
    svc, fed, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-bob", "bob", "Bob")],
        remote_users=[_carol()],
        bus=bus,
    )
    await _remote_invite(svc)
    await svc.decline_session(
        app_id="chess", session_id="sess-1", actor_user_id="u-bob"
    )

    assert svc._app_repo.invites == []
    (ended,) = _ended(bus)
    assert (ended.session_ids, ended.outcome) == (("sess-1",), "declined")
    (sent,) = fed.sent_events
    assert sent["to_instance_id"] == "peer-inst-id"
    assert sent["event_type"] is FederationEventType.APP_SESSION
    assert sent["payload"] == {
        "app_id": "chess",
        "session_id": "sess-1",
        "verb": "close",
        "reason": "declined",
        "to_user": "carol",
        "from_user": "bob",
    }


@pytest.mark.asyncio
async def test_a_second_decline_loses():
    """Declined on two devices at once (or after the app took it in): only
    one wins; the other hears it was handled and nothing more is sent."""
    svc, fed, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-bob", "bob", "Bob")],
        remote_users=[_carol()],
    )
    await _remote_invite(svc)
    await svc.decline_session(
        app_id="chess", session_id="sess-1", actor_user_id="u-bob"
    )
    with pytest.raises(AppChallengeHandledError):
        await svc.decline_session(
            app_id="chess", session_id="sess-1", actor_user_id="u-bob"
        )
    assert len(fed.sent_events) == 1


@pytest.mark.asyncio
async def test_a_decline_that_cant_be_sent_still_stands():
    bus = _FakeBus()
    svc, fed, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-bob", "bob", "Bob")],
        remote_users=[_carol()],
        bus=bus,
    )
    await _remote_invite(svc)

    async def _down(**_kw):
        raise ConnectionError("peer offline")

    fed.send_event = _down  # type: ignore[method-assign]
    await svc.decline_session(
        app_id="chess", session_id="sess-1", actor_user_id="u-bob"
    )
    assert svc._app_repo.invites == []
    assert [e.outcome for e in _ended(bus)] == ["declined"]


@pytest.mark.asyncio
async def test_declining_for_a_disabled_app_sends_nothing():
    svc, fed, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-bob", "bob", "Bob")],
        remote_users=[_carol()],
    )
    await _remote_invite(svc)
    svc._app_repo._apps["chess"] = _make_installed_app("chess", enabled=False)
    await svc.decline_session(
        app_id="chess", session_id="sess-1", actor_user_id="u-bob"
    )
    assert svc._app_repo.invites == []
    assert fed.sent_events == []


@pytest.mark.asyncio
async def test_declining_a_local_invite_tells_the_challenger_over_websocket():
    svc, fed, ws = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-alice", "alice", "Alice"), _make_user("u-bob", "bob")],
    )
    session_id = await svc.open_session(
        app_id="chess", target=_local_target("u-bob"), actor_user_id="u-alice"
    )
    ws.user_calls.clear()
    await svc.decline_session(
        app_id="chess", session_id=session_id, actor_user_id="u-bob"
    )
    (call,) = ws.user_calls
    assert call["user_id"] == "u-alice"
    assert call["payload"]["kind"] == "session"
    assert call["payload"]["payload"]["verb"] == "close"
    assert call["payload"]["payload"]["reason"] == "declined"
    assert fed.sent_events == []


@pytest.mark.asyncio
async def test_the_challenger_withdraws_a_local_invite():
    bus = _FakeBus()
    svc, _, ws = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-alice", "alice", "Alice"), _make_user("u-bob", "bob")],
        bus=bus,
    )
    session_id = await svc.open_session(
        app_id="chess", target=_local_target("u-bob"), actor_user_id="u-alice"
    )
    ws.user_calls.clear()
    await svc.cancel_session(
        app_id="chess",
        session_id=session_id,
        target=_local_target("u-bob"),
        actor_user_id="u-alice",
    )
    assert svc._app_repo.invites == []
    (ended,) = _ended(bus)
    assert (ended.user_id, ended.outcome) == ("u-bob", "withdrawn")
    # Bob never played it: nothing reaches his app (D3), the invite just goes.
    assert ws.user_calls == []


@pytest.mark.asyncio
async def test_only_the_challenger_can_withdraw_an_invite():
    """Someone else naming the same session leaves the invite waiting."""
    bus = _FakeBus()
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[
            _make_user("u-alice", "alice", "Alice"),
            _make_user("u-bob", "bob"),
            _make_user("u-eve", "eve"),
        ],
        bus=bus,
    )
    session_id = await svc.open_session(
        app_id="chess", target=_local_target("u-bob"), actor_user_id="u-alice"
    )
    # Eve doesn't play that game: refused (D3), and nothing changes.
    with pytest.raises(AppSessionUnknownError):
        await svc.cancel_session(
            app_id="chess",
            session_id=session_id,
            target=_local_target("u-bob"),
            actor_user_id="u-eve",
        )
    assert len(svc._app_repo.invites) == 1
    assert _ended(bus) == []


@pytest.mark.asyncio
async def test_the_challenger_withdraws_a_remote_invite():
    svc, fed, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-alice", "alice", "Alice")],
        remote_users=[_carol()],
        instances=[_make_remote_instance()],
    )
    target = {"instance_id": "peer-inst-id", "user_ref": "carol", "is_local": False}
    await _know(svc, "u-alice", "s9", target)
    await svc.cancel_session(
        app_id="chess", session_id="s9", target=target, actor_user_id="u-alice"
    )
    (sent,) = fed.sent_events
    assert sent["payload"]["verb"] == "close"
    assert sent["payload"]["reason"] == "withdrawn"
    assert (sent["payload"]["to_user"], sent["payload"]["from_user"]) == (
        "carol",
        "alice",
    )


@pytest.mark.asyncio
async def test_a_close_is_not_sent_to_a_household_that_cant_address_a_person():
    svc, fed, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-alice", "alice", "Alice")],
        remote_users=[_carol()],
        instances=[_make_remote_instance()],
        peer_version=17,
    )
    target = {"instance_id": "peer-inst-id", "user_ref": "carol", "is_local": False}
    await _know(svc, "u-alice", "s9", target)
    # A pre-v_18 household can't address a person: no game with it at all.
    with pytest.raises(AppContactNotFoundError):
        await svc.cancel_session(
            app_id="chess", session_id="s9", target=target, actor_user_id="u-alice"
        )
    assert fed.sent_events == []


@pytest.mark.asyncio
async def test_an_inbound_close_from_the_challenger_withdraws_the_invite():
    bus = _FakeBus()
    svc, _, ws = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-bob", "bob", "Bob")],
        remote_users=[_carol()],
        bus=bus,
    )
    await _remote_invite(svc)
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_SESSION,
            {
                "app_id": "chess",
                "session_id": "sess-1",
                "verb": "close",
                "reason": "withdrawn",
                "to_user": "bob",
                "from_user": "carol",
            },
        )
    )
    assert svc._app_repo.invites == []
    assert [(e.session_ids, e.outcome) for e in _ended(bus)] == [
        (("sess-1",), "withdrawn")
    ]
    # Bob never played it: his app hears nothing of it (D3).
    assert ws.user_calls == []


# ─── §CP.F2 guardian blocks (audit #10) ──────────────────────────────────────


class _FakeChildProtection:
    """Guardian blocks as ChildProtectionService answers them: symmetric
    pairs of user ids (local or remote)."""

    def __init__(
        self,
        pairs: set[frozenset[str]] | None = None,
        *,
        homes: dict[str, str] | None = None,
    ) -> None:
        self.pairs = pairs or set()
        #: user_id → instance_id for the blocked people (household check).
        self.homes = homes or {}
        self.gates: list[object] = []
        #: (protected user_id, instance_id) pairs the DM rule refuses
        #: (§CP.F3: not a direct pairing).
        self.no_dm: set[tuple[str, str]] = set()

    def register_gate(self, gate: object) -> None:
        self.gates.append(gate)

    async def is_dm_allowed(
        self, *, sender_user_id: str, target_instance_id: str | None
    ) -> bool:
        return (sender_user_id, target_instance_id or "") not in self.no_dm

    async def is_protected(self, user_id: str) -> bool:
        return any(user_id in p for p in self.pairs)

    async def is_guardian_blocked(self, a: str, b: str) -> bool:
        return frozenset({a, b}) in self.pairs

    async def guardian_block_counterparts(self, user_id: str) -> frozenset[str]:
        return frozenset(
            other for p in self.pairs if user_id in p for other in p - {user_id}
        )

    async def guardian_blocks_household(
        self, user_id: str | None, instance_id: str
    ) -> bool:
        for p in self.pairs:
            if user_id is not None and user_id not in p:
                continue
            if any(self.homes.get(u) == instance_id for u in p):
                return True
        return False


_KID = _make_user("u-kid", "kid", "Kid")
_ADULT = _make_user("u-adult", "adult", "Local Adult")
_REMOTE_ADULT = _make_remote_user(
    user_id="ru-mallory",
    instance_id="peer-inst-id",
    remote_username="mallory",
    display_name="Mallory",
)


def _guarded_svc(pairs: set[frozenset[str]], **kw):
    cp = _FakeChildProtection(pairs, homes={"ru-mallory": "peer-inst-id"})
    bus = _FakeBus()
    svc, fed, ws = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_KID, _ADULT],
        remote_users=[_REMOTE_ADULT],
        bus=bus,
        **kw,
    )
    svc.attach_child_protection(cp)  # type: ignore[arg-type]
    return svc, fed, ws, bus, cp


@pytest.mark.asyncio
async def test_guardian_block_registers_app_federation_as_a_gate():
    svc, _, _, _, cp = _guarded_svc(set())
    assert cp.gates == [svc]


@pytest.mark.asyncio
async def test_guardian_blocked_people_are_not_in_each_others_app_contacts():
    """Neither the protected minor nor the blocked adult (local or remote)
    sees the other in the app contact picker."""
    svc, _, _, _, _ = _guarded_svc(
        {frozenset({"u-kid", "u-adult"}), frozenset({"u-kid", "ru-mallory"})}
    )
    kid_refs = {
        c["user_ref"]
        for c in await svc.list_invitable(app_id="chess", self_user_id="u-kid")
    }
    assert kid_refs.isdisjoint({"u-adult", "mallory"})
    adult_refs = {
        c["user_ref"]
        for c in await svc.list_invitable(app_id="chess", self_user_id="u-adult")
    }
    assert "u-kid" not in adult_refs
    assert "mallory" in adult_refs  # no block between the two adults


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "target"),
    [
        ("u-adult", {"instance_id": "own-inst-id", "user_ref": "u-kid"}),
        ("u-kid", {"instance_id": "own-inst-id", "user_ref": "u-adult"}),
    ],
)
async def test_guardian_blocked_local_pair_cannot_challenge_or_message(actor, target):
    """Local loopback, both directions: no invite, no move, no frame."""
    svc, _, ws, bus, _ = _guarded_svc({frozenset({"u-kid", "u-adult"})})
    target = {**target, "is_local": True}
    with pytest.raises(AppContactNotFoundError):
        await svc.open_session(app_id="chess", target=target, actor_user_id=actor)
    # Not even in a game from before the block.
    await _know(svc, actor, "s", target)
    with pytest.raises(AppContactNotFoundError):
        await svc.send_message(
            app_id="chess",
            target=target,
            session_id="s",
            payload={"m": 1},
            actor_user_id=actor,
        )
    assert ws.user_calls == [] and ws.calls == []
    assert bus.published == []


@pytest.mark.asyncio
async def test_protected_minor_cannot_challenge_or_message_a_guardian_blocked_remote():
    """Outbound federated: nothing leaves for the blocked remote adult."""
    svc, fed, _, _, _ = _guarded_svc({frozenset({"u-kid", "ru-mallory"})})
    target = {"instance_id": "peer-inst-id", "user_ref": "mallory", "is_local": False}
    with pytest.raises(AppContactNotFoundError):
        await svc.open_session(app_id="chess", target=target, actor_user_id="u-kid")
    await _know(svc, "u-kid", "s", target)
    with pytest.raises(AppContactNotFoundError):
        await svc.send_message(
            app_id="chess",
            target=target,
            session_id="s",
            payload={"m": 1},
            actor_user_id="u-kid",
        )
    assert fed.sent_events == [] and fed.sent_app_messages == []


@pytest.mark.asyncio
async def test_protected_minor_legacy_household_send_refused_across_guardian_block():
    """A household-addressed (pre-v_18) send would reach everyone there,
    the blocked adult included — refused like any household-addressed one
    (D3: it names nobody anyone picked)."""
    svc, fed, _, _, _ = _guarded_svc({frozenset({"u-kid", "ru-mallory"})})
    for instance in ("peer-inst-id", "other-inst"):
        target = {"instance_id": instance, "user_ref": "", "is_local": False}
        with pytest.raises(AppContactNotFoundError):
            await svc.open_session(app_id="chess", target=target, actor_user_id="u-kid")
    assert fed.sent_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["open", "message"])
async def test_inbound_from_guardian_blocked_remote_never_reaches_protected_minor(
    verb,
):
    """Inbound federated JSON: no frame, no pending invite, no bell row."""
    svc, _, ws, bus, _ = _guarded_svc({frozenset({"u-kid", "ru-mallory"})})
    if verb == "open":
        event = _make_event(
            FederationEventType.APP_SESSION,
            {
                "app_id": "chess",
                "session_id": "sess-g",
                "verb": "open",
                "to_user": "kid",
                "from_user": "mallory",
            },
        )
    else:
        event = _make_event(
            FederationEventType.APP_MESSAGE,
            {
                "app_id": "chess",
                "session_id": "sess-g",
                "data": {"m": 1},
                "to_user": "kid",
                "from_user": "mallory",
            },
        )
    await svc.on_inbound_event(event)
    assert ws.user_calls == [] and ws.calls == []
    assert svc._app_repo.invites == []  # type: ignore[attr-defined]
    assert bus.published == []


@pytest.mark.asyncio
async def test_inbound_binary_frame_from_guardian_blocked_remote_is_dropped():
    svc, _, ws, _, _ = _guarded_svc({frozenset({"u-kid", "ru-mallory"})})
    await svc.on_inbound_message(
        "peer-inst-id",
        "chess",
        "sess-g",
        {"m": 1},
        to_user="kid",
        from_user="mallory",
    )
    assert ws.user_calls == [] and ws.calls == []


@pytest.mark.asyncio
async def test_inbound_household_fanout_skips_guardian_blocked_minor():
    """Legacy fan-out: a named blocked initiator misses the minor; an
    unnamed sender from a household a guardian block names misses them too."""
    svc, _, ws, _, _ = _guarded_svc(
        {frozenset({"u-kid", "ru-mallory"})}, peer_version=17
    )
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_MESSAGE,
            {"app_id": "chess", "session_id": "s", "data": {}, "from_user": "mallory"},
        )
    )
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_MESSAGE,
            {"app_id": "chess", "session_id": "s", "data": {}},
        )
    )
    assert [c["user_ids"] for c in ws.calls] == [["u-adult"], ["u-adult"]]


@pytest.mark.asyncio
async def test_play_refuses_an_invite_from_someone_guardian_blocked_since():
    """An invite that arrived before the guardian block is not handed to
    the protected minor's app: Play refuses it and its row ends ``done``."""
    svc, _, _, bus, cp = _guarded_svc(set())
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_SESSION,
            {
                "app_id": "chess",
                "session_id": "sess-old",
                "verb": "open",
                "to_user": "kid",
                "from_user": "mallory",
            },
        )
    )
    local_sid = await svc.open_session(
        app_id="chess",
        target={"instance_id": "own-inst-id", "user_ref": "u-kid", "is_local": True},
        actor_user_id="u-adult",
    )
    cp.pairs = {frozenset({"u-kid", "ru-mallory"}), frozenset({"u-kid", "u-adult"})}
    bus.published.clear()
    for sid in ("sess-old", local_sid):
        with pytest.raises(AppChallengeHandledError):
            await svc.accept_session(
                app_id="chess", session_id=sid, actor_user_id="u-kid"
            )
    ended = [e for e in bus.published if isinstance(e, AppChallengeEnded)]
    assert [(e.session_ids, e.outcome) for e in ended] == [
        (("sess-old",), "done"),
        ((local_sid,), "done"),
    ]


@pytest.mark.asyncio
async def test_declining_an_invite_from_someone_guardian_blocked_since_tells_them_nothing():
    """The protected minor can still clear the old invite, but the decline
    (a signal from them) never reaches the blocked adult, local or remote."""
    svc, fed, ws, _, cp = _guarded_svc(set())
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_SESSION,
            {
                "app_id": "chess",
                "session_id": "sess-old",
                "verb": "open",
                "to_user": "kid",
                "from_user": "mallory",
            },
        )
    )
    sid = await svc.open_session(
        app_id="chess",
        target={"instance_id": "own-inst-id", "user_ref": "u-kid", "is_local": True},
        actor_user_id="u-adult",
    )
    cp.pairs = {frozenset({"u-kid", "ru-mallory"}), frozenset({"u-kid", "u-adult"})}
    ws.user_calls.clear()
    await svc.decline_session(
        app_id="chess", session_id="sess-old", actor_user_id="u-kid"
    )
    await svc.decline_session(app_id="chess", session_id=sid, actor_user_id="u-kid")
    assert fed.sent_events == []
    assert ws.user_calls == []
    assert svc._app_repo.invites == []  # type: ignore[attr-defined]


# ─── People from other households are named with their household ────────────


def test_with_household_names_the_household():
    assert with_household("Ben", "Familie Okafor") == "Ben · Familie Okafor"
    assert with_household("Ben", None) == "Ben"
    assert with_household("Ben", "") == "Ben"
    # A household that only knows itself by the person's name: no echo.
    assert with_household("Ben", "Ben") == "Ben"


@pytest.mark.asyncio
async def test_list_invitable_names_a_remote_persons_household():
    """Two "Admin"s — one at home, one in a paired household — read apart."""
    alice = _make_user("u-alice", "alice", "Alice")
    admin = _make_user("u-admin", "admin", "Admin")
    remote = _make_remote_user(
        user_id="ru-1",
        instance_id="peer-inst",
        remote_username="admin",
        display_name="Admin",
    )
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app()},
        users=[alice, admin],
        remote_users=[remote],
        instances=[_make_remote_instance("peer-inst", display_name="Familie Okafor")],
        own_instance_id="own-inst",
    )
    contacts = {
        c["instance_id"]: c
        for c in await svc.list_invitable(app_id="chess", self_user_id="u-alice")
    }
    mine, theirs = contacts["own-inst"], contacts["peer-inst"]
    assert mine["display_name"] == "Admin"
    assert mine["household_name"] is None
    assert mine["person_name"] == "Admin"
    assert theirs["display_name"] == "Admin · Familie Okafor"
    assert theirs["household_name"] == "Familie Okafor"
    assert theirs["person_name"] == "Admin"


@pytest.mark.asyncio
async def test_list_invitable_uses_the_households_local_alias():
    inst = _make_remote_instance("peer-inst", display_name="Okafor-HA")
    inst = dataclasses.replace(inst, local_alias="The Okafors")
    remote = _make_remote_user(
        user_id="ru-1",
        instance_id="peer-inst",
        remote_username="ben",
        display_name="Ben",
    )
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app()},
        users=[_make_user("u-alice", "alice", "Alice")],
        remote_users=[remote],
        instances=[inst],
    )
    (c,) = await svc.list_invitable(app_id="chess", self_user_id="u-alice")
    assert c["display_name"] == "Ben · The Okafors"


@pytest.mark.asyncio
async def test_an_invite_from_another_household_names_it():
    """Needs you reads "Carol · Familie Okafor invited you to play Chess"."""
    from socialhome.domain.events import AppChallengeReceived

    bus = _FakeBus()
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-bob", "bob", "Bob")],
        remote_users=[
            _make_remote_user(
                user_id="ru-carol",
                instance_id="peer-inst-id",
                remote_username="carol",
                display_name="Carol",
            )
        ],
        instances=[
            _make_remote_instance("peer-inst-id", display_name="Familie Okafor")
        ],
        bus=bus,
    )
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
    (ev,) = [e for e in bus.published if isinstance(e, AppChallengeReceived)]
    assert ev.from_display == "Carol · Familie Okafor"


# ─── The mailbox (D11): moves and declines wait for a closed / off app ────


def _declined(bus: _FakeBus) -> list:
    return [e for e in bus.published if isinstance(e, AppChallengeDeclined)]


def _mailbox_svc(*, enabled: bool = True, **kw):
    bus = _FakeBus()
    svc, fed, ws = _make_svc(
        apps={"chess": _make_installed_app("chess", enabled=enabled)},
        users=[_make_user("u-bob", "bob", "Bob"), _make_user("u-ann", "ann", "Ann")],
        remote_users=[
            _carol(),
            _make_remote_user(
                user_id="ru-dave", instance_id="peer-inst-id", remote_username="dave"
            ),
        ],
        bus=bus,
        **kw,
    )
    return svc, fed, ws, bus


async def _played_remote_game(svc, session_id: str = "sess-1") -> None:
    """Carol (peer-inst-id) invited Bob, and Bob pressed Play."""
    await _remote_invite(svc, session_id)
    await svc.accept_session(
        app_id="chess", session_id=session_id, actor_user_id="u-bob"
    )


async def _inbound_move(
    svc, data: dict, *, session_id: str = "sess-1", from_user: str = "carol"
) -> None:
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_MESSAGE,
            {
                "app_id": "chess",
                "session_id": session_id,
                "data": data,
                "to_user": "bob",
                "from_user": from_user,
            },
        )
    )


async def _inbound_close(
    svc, reason: str, *, session_id: str = "sess-1", to_user: str = "bob"
) -> None:
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_SESSION,
            {
                "app_id": "chess",
                "session_id": session_id,
                "verb": "close",
                "reason": reason,
                "to_user": to_user,
                "from_user": "carol",
            },
        )
    )


def _turn(svc, *, enabled: bool) -> None:
    svc._app_repo._apps["chess"] = _make_installed_app("chess", enabled=enabled)


@pytest.mark.asyncio
async def test_a_move_for_a_closed_app_waits_and_only_the_latest_is_kept():
    """L#1: Bob's app isn't open — the household keeps Carol's latest move
    of that game and hands it over (once) when his app opens."""
    svc, _, _, _ = _mailbox_svc()
    await _played_remote_game(svc)
    await _inbound_move(svc, {"n": 1})
    await _inbound_move(svc, {"n": 2})
    (held,) = await svc.take_held(app_id="chess", actor_user_id="u-bob")
    assert held.session_id == "sess-1"
    assert (held.from_instance, held.from_user) == ("peer-inst-id", "carol")
    assert held.held == {"kind": "message", "payload": {"n": 2}}
    assert await svc.take_held(app_id="chess", actor_user_id="u-bob") == []


@pytest.mark.asyncio
async def test_a_move_while_the_app_is_turned_off_is_held_and_arrives_on():
    """L#1: Chess is turned off on Bob's household — the move is not live
    (no frame), but waits and arrives once Chess is back on and opened."""
    svc, _, ws, _ = _mailbox_svc()
    await _played_remote_game(svc)
    _turn(svc, enabled=False)
    ws.user_calls.clear()
    await _inbound_move(svc, {"san": "Nf3"})
    assert ws.user_calls == []
    with pytest.raises(AppNotEnabledError):
        await svc.take_held(app_id="chess", actor_user_id="u-bob")
    _turn(svc, enabled=True)
    (held,) = await svc.take_held(app_id="chess", actor_user_id="u-bob")
    assert held.held == {"kind": "message", "payload": {"san": "Nf3"}}


@pytest.mark.asyncio
async def test_an_invite_to_a_turned_off_app_is_still_dropped():
    svc, _, ws, bus = _mailbox_svc(enabled=False)
    await _remote_invite(svc)
    assert svc._app_repo.pending == []
    assert ws.user_calls == [] and bus.published == []


@pytest.mark.asyncio
async def test_a_move_in_a_game_this_household_does_not_know_is_not_kept():
    """Only a known session is kept — and (D3) only a known session reaches
    the app live: a message for any other session id goes nowhere."""
    svc, _, ws, _ = _mailbox_svc()
    await _inbound_move(svc, {"n": 1}, session_id="unknown")
    assert svc._app_repo.pending == []
    assert ws.user_calls == []


@pytest.mark.asyncio
async def test_a_move_from_anyone_but_the_other_side_is_not_kept():
    svc, _, _, _ = _mailbox_svc()
    await _played_remote_game(svc)
    await _inbound_move(svc, {"n": 1}, from_user="dave")
    assert await svc.take_held(app_id="chess", actor_user_id="u-bob") == []


@pytest.mark.asyncio
async def test_a_message_too_large_to_hold_still_goes_out_live():
    svc, _, ws, _ = _mailbox_svc()
    await _played_remote_game(svc)
    ws.user_calls.clear()
    await _inbound_move(svc, {"blob": "x" * (64 * 1024)})
    assert len(ws.user_calls) == 1
    assert await svc.take_held(app_id="chess", actor_user_id="u-bob") == []


@pytest.mark.asyncio
async def test_a_decline_waits_for_the_challenger_and_rings_the_bell():
    """L#2: Bob invited Carol; she declines while his app is closed — the
    close waits in his mailbox and he gets a bell row."""
    svc, _, _, bus = _mailbox_svc()
    sid = await svc.open_session(
        app_id="chess",
        target=_remote_target("peer-inst-id", "carol"),
        actor_user_id="u-bob",
    )
    await _inbound_close(svc, "declined", session_id=sid)
    (declined,) = _declined(bus)
    assert declined.to_user_id == "u-bob"
    assert declined.from_user_id == "ru-carol"
    assert declined.session_id == sid and declined.app_id == "chess"
    (held,) = await svc.take_held(app_id="chess", actor_user_id="u-bob")
    assert held.held is not None and held.held["kind"] == "session"
    assert held.held["payload"]["reason"] == "declined"


@pytest.mark.asyncio
async def test_a_decline_for_a_game_never_sent_rings_nothing():
    """A "decline" naming a session Bob never opened with Carol is not
    kept and raises no bell row."""
    svc, _, _, bus = _mailbox_svc()
    await _inbound_close(svc, "declined", session_id="made-up")
    assert _declined(bus) == []
    assert svc._app_repo.pending == []


@pytest.mark.asyncio
async def test_a_withdrawal_after_play_waits_for_the_invitee():
    """L#3: Bob already pressed Play; Carol cancels — the close waits for
    Bob's app (his game clears on open), and no bell row."""
    svc, _, _, bus = _mailbox_svc()
    await _played_remote_game(svc)
    bus.published.clear()
    await _inbound_close(svc, "withdrawn")
    assert _declined(bus) == [] and _ended(bus) == []
    (held,) = await svc.take_held(app_id="chess", actor_user_id="u-bob")
    assert held.held is not None
    assert held.held["payload"]["reason"] == "withdrawn"


@pytest.mark.asyncio
async def test_a_withdrawal_before_play_still_just_ends_the_invite():
    svc, _, _, bus = _mailbox_svc()
    await _remote_invite(svc)
    await _inbound_close(svc, "withdrawn")
    assert [e.outcome for e in _ended(bus)] == ["withdrawn"]
    assert svc._app_repo.pending == []


@pytest.mark.asyncio
async def test_a_move_before_play_waits_with_the_invite():
    """The challenger may move before the invitee plays: the move waits
    on the invite's row and the invite still waits in Needs you."""
    svc, _, _, _ = _mailbox_svc()
    await _remote_invite(svc)
    await _inbound_move(svc, {"san": "e4"})
    assert [p.session_id for p in svc._app_repo.invites] == ["sess-1"]
    invite = await svc.accept_session(
        app_id="chess", session_id="sess-1", actor_user_id="u-bob"
    )
    assert invite.payload["verb"] == "open"
    (held,) = await svc.take_held(app_id="chess", actor_user_id="u-bob")
    assert held.held == {"kind": "message", "payload": {"san": "e4"}}


@pytest.mark.asyncio
async def test_a_local_game_holds_moves_and_a_decline_rings_the_challenger():
    """Same household: Ann invites Bob; his decline waits in her mailbox
    with a bell row; in a played game each side's move waits for the other."""
    svc, _, _, bus = _mailbox_svc()
    sid = await svc.open_session(
        app_id="chess", target=_local_target("u-bob"), actor_user_id="u-ann"
    )
    await svc.decline_session(app_id="chess", session_id=sid, actor_user_id="u-bob")
    (declined,) = _declined(bus)
    assert (declined.to_user_id, declined.from_user_id) == ("u-ann", "u-bob")
    assert declined.from_display == "Bob"
    (held,) = await svc.take_held(app_id="chess", actor_user_id="u-ann")
    assert held.held is not None and held.held["payload"]["reason"] == "declined"

    sid2 = await svc.open_session(
        app_id="chess", target=_local_target("u-bob"), actor_user_id="u-ann"
    )
    await svc.accept_session(app_id="chess", session_id=sid2, actor_user_id="u-bob")
    await svc.send_message(
        app_id="chess",
        target=_local_target("u-bob"),
        session_id=sid2,
        payload={"san": "e4"},
        actor_user_id="u-ann",
    )
    await svc.send_message(
        app_id="chess",
        target=_local_target("u-ann"),
        session_id=sid2,
        payload={"san": "e5"},
        actor_user_id="u-bob",
    )
    (to_bob,) = await svc.take_held(app_id="chess", actor_user_id="u-bob")
    assert to_bob.held == {"kind": "message", "payload": {"san": "e4"}}
    (to_ann,) = await svc.take_held(app_id="chess", actor_user_id="u-ann")
    assert to_ann.held == {"kind": "message", "payload": {"san": "e5"}}


@pytest.mark.asyncio
async def test_a_local_withdrawal_after_play_waits_for_the_invitee():
    svc, _, _, _ = _mailbox_svc()
    sid = await svc.open_session(
        app_id="chess", target=_local_target("u-bob"), actor_user_id="u-ann"
    )
    await svc.accept_session(app_id="chess", session_id=sid, actor_user_id="u-bob")
    await svc.cancel_session(
        app_id="chess",
        session_id=sid,
        target=_local_target("u-bob"),
        actor_user_id="u-ann",
    )
    (held,) = await svc.take_held(app_id="chess", actor_user_id="u-bob")
    assert held.held is not None and held.held["payload"]["reason"] == "withdrawn"


# ─── "Your turn" (owner decision 2026-10-30): the app flags the move ─────────


def _turns(bus: _FakeBus) -> list[AppTurn]:
    return [e for e in bus.published if isinstance(e, AppTurn)]


async def _inbound_turn(svc, data: dict, *, turn: object = True, **kw) -> None:
    payload = {
        "app_id": "chess",
        "session_id": kw.get("session_id", "sess-1"),
        "data": data,
        "to_user": "bob",
        "from_user": kw.get("from_user", "carol"),
        "turn": turn,
    }
    await svc.on_inbound_event(_make_event(FederationEventType.APP_MESSAGE, payload))


@pytest.mark.asyncio
async def test_a_move_flagged_turn_tells_the_player_once_it_is_kept():
    """Carol's move hands Bob the turn: kept in his mailbox, and Social
    Home raises "Your turn" for him — the app said so, Social Home read
    nothing of the move."""
    svc, _, _, bus = _mailbox_svc()
    await _played_remote_game(svc)
    bus.published.clear()
    await _inbound_turn(svc, {"shot": "B4"})
    (turn,) = _turns(bus)
    assert (turn.app_id, turn.session_id) == ("chess", "sess-1")
    assert (turn.to_user_id, turn.from_user_id) == ("u-bob", "ru-carol")
    # The sender as this household names them (the fixture's "Bob").
    assert turn.from_display == "Bob"


@pytest.mark.asyncio
async def test_a_move_without_the_turn_flag_raises_nothing():
    """An informational message (a chat line, "Ben is thinking"), or a
    flag that isn't exactly ``true``, makes no row — older apps and
    households send none."""
    svc, _, _, bus = _mailbox_svc()
    await _played_remote_game(svc)
    bus.published.clear()
    await _inbound_move(svc, {"n": 1})
    await _inbound_turn(svc, {"n": 2}, turn="yes")
    await _inbound_turn(svc, {"n": 3}, turn=1)
    assert _turns(bus) == []
    # The messages were kept all the same.
    (held,) = await svc.take_held(app_id="chess", actor_user_id="u-bob")
    assert held.held == {"kind": "message", "payload": {"n": 3}}


@pytest.mark.asyncio
async def test_a_turn_that_is_not_kept_raises_nothing():
    """Only a move the mailbox keeps: a made-up session, someone other
    than the game's other side, a message too large to hold, an invite
    not played yet (it waits in Needs you) or an app turned off."""
    svc, _, _, bus = _mailbox_svc()
    await _inbound_turn(svc, {"n": 1}, session_id="made-up")
    await _remote_invite(svc)
    await _inbound_turn(svc, {"n": 1})  # before Play: Needs you has it
    await svc.accept_session(app_id="chess", session_id="sess-1", actor_user_id="u-bob")
    await _inbound_turn(svc, {"n": 2}, from_user="dave")
    await _inbound_turn(svc, {"blob": "x" * (64 * 1024)})
    _turn(svc, enabled=False)
    await _inbound_turn(svc, {"n": 3})
    assert _turns(bus) == []


@pytest.mark.asyncio
async def test_a_turn_from_someone_blocked_raises_nothing():
    svc, _, _, bus = _mailbox_svc()
    await _played_remote_game(svc)
    bus.published.clear()
    # Bob blocked Carol after the game started.
    svc._user_repo._block_pairs.add(("u-bob", "ru-carol"))
    await _inbound_turn(svc, {"n": 1})
    assert _turns(bus) == []


@pytest.mark.asyncio
async def test_a_turn_from_someone_guardian_blocked_raises_nothing():
    """§CP.F2: a guardian set a block between Bob and Carol after their
    game started — her moves never become a "Your turn" row for him."""
    svc, _, _, bus = _mailbox_svc()
    await _played_remote_game(svc)
    svc.attach_child_protection(  # type: ignore[arg-type]
        _FakeChildProtection(
            {frozenset({"u-bob", "ru-carol"})}, homes={"ru-carol": "peer-inst-id"}
        )
    )
    bus.published.clear()
    await _inbound_turn(svc, {"n": 1})
    await svc.on_inbound_message(
        "peer-inst-id",
        "chess",
        "sess-1",
        {"n": 2},
        to_user="bob",
        from_user="carol",
        turn=True,
    )
    assert _turns(bus) == []


@pytest.mark.asyncio
async def test_a_turn_for_a_protected_account_under_the_age_gate_raises_nothing():
    """The app's age gate went up after Bob (protected, 10) started
    playing: a move flagged ``turn`` makes no row for him."""
    cp = _FakeCpRepo()
    cp.add("u-bob", enabled=True, declared_age=10)
    svc, _, _, bus = _mailbox_svc(cp_repo=cp)
    await _played_remote_game(svc)
    svc._app_repo._apps["chess"] = _make_installed_app("chess", min_age=13)  # type: ignore[attr-defined]
    bus.published.clear()
    await _inbound_turn(svc, {"n": 1})
    assert _turns(bus) == []
    # At the gate's age the same move tells him.
    cp.add("u-bob", enabled=True, declared_age=13)
    await _inbound_turn(svc, {"n": 2})
    assert len(_turns(bus)) == 1


@pytest.mark.asyncio
async def test_a_turn_for_a_protected_account_from_a_household_it_cant_message():
    """§CP.F3: a protected account plays only with people it could already
    message — a turn from any other household makes no row."""
    svc, _, _, bus = _mailbox_svc()
    await _played_remote_game(svc)
    cp = _FakeChildProtection()
    cp.no_dm.add(("u-bob", "peer-inst-id"))
    svc.attach_child_protection(cp)  # type: ignore[arg-type]
    bus.published.clear()
    await _inbound_turn(svc, {"n": 1})
    assert _turns(bus) == []


@pytest.mark.asyncio
async def test_a_binary_frame_carries_the_turn_flag_too():
    svc, _, _, bus = _mailbox_svc()
    await _played_remote_game(svc)
    bus.published.clear()
    await svc.on_inbound_message(
        "peer-inst-id", "chess", "sess-1", {"n": 1}, to_user="bob", from_user="carol"
    )
    assert _turns(bus) == []
    await svc.on_inbound_message(
        "peer-inst-id",
        "chess",
        "sess-1",
        {"n": 2},
        to_user="bob",
        from_user="carol",
        turn=True,
    )
    (turn,) = _turns(bus)
    assert turn.to_user_id == "u-bob"


@pytest.mark.asyncio
async def test_sending_a_turn_flags_it_for_the_other_household():
    """``turn`` rides the sealed message (beside ``to_user``) — only when
    the app set it."""
    svc, fed, _, _ = _mailbox_svc()
    await _played_remote_game(svc)
    target = _remote_target("peer-inst-id", "carol")
    for turn in (True, False):
        await svc.send_message(
            app_id="chess",
            target=target,
            session_id="sess-1",
            payload={"n": 1},
            actor_user_id="u-bob",
            turn=turn,
        )
    assert [m["turn"] for m in fed.sent_app_messages] == [True, False]


@pytest.mark.asyncio
async def test_a_local_turn_tells_the_other_player():
    svc, _, _, bus = _mailbox_svc()
    sid = await svc.open_session(
        app_id="chess", target=_local_target("u-bob"), actor_user_id="u-ann"
    )
    await svc.accept_session(app_id="chess", session_id=sid, actor_user_id="u-bob")
    bus.published.clear()
    for turn in (False, True):
        await svc.send_message(
            app_id="chess",
            target=_local_target("u-bob"),
            session_id=sid,
            payload={"san": "e4"},
            actor_user_id="u-ann",
            turn=turn,
        )
    (turn,) = _turns(bus)
    assert (turn.to_user_id, turn.from_user_id) == ("u-bob", "u-ann")
    assert turn.session_id == sid and turn.from_display is None


@pytest.mark.asyncio
async def test_opening_the_app_tells_which_games_it_took():
    """The app took its held messages: those games' "Your turn" rows read
    as read. Nothing taken, nothing said."""
    svc, _, _, bus = _mailbox_svc()
    await _played_remote_game(svc)
    await _inbound_turn(svc, {"n": 1})
    bus.published.clear()
    await svc.take_held(app_id="chess", actor_user_id="u-bob")
    (seen,) = [e for e in bus.published if isinstance(e, AppTurnsSeen)]
    assert seen == AppTurnsSeen(
        app_id="chess", user_id="u-bob", session_ids=("sess-1",)
    )
    bus.published.clear()
    await svc.take_held(app_id="chess", actor_user_id="u-bob")
    assert bus.published == []


class _CountingUserRepo(_FakeUserRepo):
    """Counts the roster reads a send would need without a known session."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.roster_reads = 0

    async def list_all_known_remote(self) -> list[RemoteUser]:
        self.roster_reads += 1
        return await super().list_all_known_remote()


def _counting_svc(**kw):
    svc, fed, ws, bus = _mailbox_svc(**kw)
    old = svc._user_repo
    svc._user_repo = _CountingUserRepo(
        old._users,
        remote_users=old._remote_users,
        blocked_ids=old._blocked_ids,
        block_pairs=old._block_pairs,
    )
    return svc, fed, ws, bus


async def _move(svc, sid: str, target: dict | None = None) -> None:
    await svc.send_message(
        app_id="chess",
        target=target or _remote_target("peer-inst-id", "carol"),
        session_id=sid,
        payload={"san": "e4"},
        actor_user_id="u-bob",
    )


@pytest.mark.asyncio
async def test_a_move_in_a_known_game_skips_the_roster_scan():
    """C#31: per move, one session lookup — not a scan of every contact."""
    svc, fed, _, _ = _counting_svc()
    sid = await svc.open_session(
        app_id="chess",
        target=_remote_target("peer-inst-id", "carol"),
        actor_user_id="u-bob",
    )
    before = svc._user_repo.roster_reads
    for _ in range(3):
        await _move(svc, sid)
    assert svc._user_repo.roster_reads == before
    assert len(fed.sent_app_messages) == 3


@pytest.mark.asyncio
async def test_a_move_to_someone_else_in_a_known_game_is_refused():
    """D3: a game is with the person it was opened with — never someone
    else on the same session id."""
    svc, fed, _, _ = _counting_svc()
    sid = await svc.open_session(
        app_id="chess",
        target=_remote_target("peer-inst-id", "carol"),
        actor_user_id="u-bob",
    )
    with pytest.raises(AppSessionUnknownError):
        await _move(svc, sid, _remote_target("peer-inst-id", "dave"))
    assert fed.sent_app_messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "blocks"),
    [
        ({"instance_id": "peer-inst-id", "user_ref": "carol"}, {("u-bob", "ru-carol")}),
        ({"instance_id": "own-inst-id", "user_ref": "u-ann"}, {("u-bob", "u-ann")}),
        ({"instance_id": "own-inst-id", "user_ref": "u-ann"}, {("u-ann", "u-bob")}),
    ],
)
async def test_a_known_game_still_refuses_a_blocked_person(target, blocks):
    svc, fed, ws, _ = _mailbox_svc()
    is_local = target["instance_id"] == "own-inst-id"
    target = {**target, "is_local": is_local}
    sid = await svc.open_session(app_id="chess", target=target, actor_user_id="u-bob")
    svc._user_repo._block_pairs = blocks
    ws.user_calls.clear()
    with pytest.raises(AppContactNotFoundError):
        await _move(svc, sid, target)
    assert fed.sent_app_messages == [] and ws.user_calls == []


@pytest.mark.asyncio
async def test_a_known_game_refuses_someone_no_longer_known():
    svc, _, _, _ = _mailbox_svc()
    sid = await svc.open_session(
        app_id="chess",
        target=_remote_target("peer-inst-id", "carol"),
        actor_user_id="u-bob",
    )
    svc._user_repo._remote_users = [
        dataclasses.replace(_carol(), deprovisioned_at="2026-10-01T00:00:00+00:00")
    ]
    with pytest.raises(AppContactNotFoundError):
        await _move(svc, sid)


@pytest.mark.asyncio
async def test_a_move_in_a_session_nobody_opened_is_refused():
    """D3 — real hole 2: an app can't message an old opponent (Carol is a
    contact) on a session id the household never saw — not opened through
    the invite sheet, not taken in with Play. Nothing goes out and nothing
    becomes known."""
    svc, fed, _, _ = _counting_svc()
    with pytest.raises(AppSessionUnknownError):
        await _move(svc, "made-up-session")
    assert fed.sent_app_messages == []
    assert svc._app_repo.pending == []


@pytest.mark.asyncio
async def test_own_moves_move_the_game_ttl_on_at_most_hourly():
    svc, _, _, _ = _mailbox_svc()
    sid = await svc.open_session(
        app_id="chess",
        target=_remote_target("peer-inst-id", "carol"),
        actor_user_id="u-bob",
    )
    await _move(svc, sid)
    assert getattr(svc._app_repo, "touched", 0) == 0
    (row,) = svc._app_repo.pending
    svc._app_repo._swap(
        row, dataclasses.replace(row, updated_at="2026-01-01T00:00:00+00:00")
    )
    await _move(svc, sid)
    assert svc._app_repo.touched == 1


@pytest.mark.asyncio
async def test_play_keeps_the_game_known_as_active():
    svc, _, _, _ = _mailbox_svc()
    await _played_remote_game(svc)
    (row,) = svc._app_repo.pending
    assert row.state == "active" and svc._app_repo.invites == []


@pytest.mark.asyncio
async def test_a_guardian_block_keeps_moves_out_of_the_mailbox():
    """§CP.F2: nothing from someone a guardian block separates is kept —
    neither on receipt, nor handed over if the block came after."""
    svc, _, _, _, cp = _guarded_svc(set())
    now = "2026-10-29T10:00:00+00:00"
    await svc._app_repo.add_active_session(
        AppPendingSession(
            app_id="chess",
            user_id="u-kid",
            session_id="g1",
            from_instance="peer-inst-id",
            from_user="mallory",
            payload={},
            created_at=now,
        )
    )

    async def move(n: int) -> None:
        await svc.on_inbound_event(
            _make_event(
                FederationEventType.APP_MESSAGE,
                {
                    "app_id": "chess",
                    "session_id": "g1",
                    "data": {"n": n},
                    "to_user": "kid",
                    "from_user": "mallory",
                },
            )
        )

    cp.pairs = {frozenset({"u-kid", "ru-mallory"})}
    await move(1)
    assert svc._app_repo.pending[0].held is None
    cp.pairs = set()
    await move(2)
    assert svc._app_repo.pending[0].held is not None
    cp.pairs = {frozenset({"u-kid", "ru-mallory"})}
    assert await svc.take_held(app_id="chess", actor_user_id="u-kid") == []


@pytest.mark.asyncio
async def test_a_blocked_sender_is_not_handed_over():
    svc, _, _, _ = _mailbox_svc()
    await _played_remote_game(svc)
    await _inbound_move(svc, {"n": 1})
    svc._user_repo._block_pairs = {("u-bob", "ru-carol")}
    assert await svc.take_held(app_id="chess", actor_user_id="u-bob") == []


# ─── Permissions: "federation" = sends to (and hears from) other households ──

_LOCAL_ONLY = ("storage", "people")


@pytest.mark.asyncio
async def test_an_app_without_federation_cannot_open_a_remote_session():
    svc, fed, _ = _make_svc(
        apps={"chess": _make_installed_app("chess", capabilities=_LOCAL_ONLY)},
        users=[_make_user("u-init", "alice", "Alice")],
        remote_users=[_make_remote_user(instance_id="peer-1", remote_username="bob")],
    )
    with pytest.raises(AppPermissionDeniedError) as ei:
        await svc.open_session(
            app_id="chess",
            target=_remote_target("peer-1", "bob"),
            actor_user_id="u-init",
        )
    assert ei.value.permission == "federation"
    assert fed.sent_events == []


@pytest.mark.asyncio
async def test_an_app_without_federation_cannot_send_to_another_household():
    svc, fed, _ = _make_svc(
        apps={"chess": _make_installed_app("chess", capabilities=_LOCAL_ONLY)},
        users=[_make_user("u-init", "alice", "Alice")],
        remote_users=[_make_remote_user(instance_id="peer-1", remote_username="bob")],
    )
    with pytest.raises(AppPermissionDeniedError):
        await svc.send_message(
            app_id="chess",
            target=_remote_target("peer-1", "bob"),
            session_id="s1",
            payload={"move": "e2e4"},
            actor_user_id="u-init",
        )
    assert fed.sent_app_messages == [] and fed.sent_events == []


@pytest.mark.asyncio
async def test_an_app_without_federation_cannot_cancel_toward_another_household():
    svc, fed, _ = _make_svc(
        apps={"chess": _make_installed_app("chess", capabilities=_LOCAL_ONLY)},
        users=[_make_user("u-init", "alice", "Alice")],
        remote_users=[_make_remote_user(instance_id="peer-1", remote_username="bob")],
    )
    with pytest.raises(AppPermissionDeniedError):
        await svc.cancel_session(
            app_id="chess",
            session_id="s1",
            target=_remote_target("peer-1", "bob"),
            actor_user_id="u-init",
        )
    assert fed.sent_events == []


@pytest.mark.asyncio
async def test_a_local_only_app_still_plays_inside_the_household():
    """``people`` without ``federation``: games with people at home work."""
    svc, fed, ws = _make_svc(
        apps={"chess": _make_installed_app("chess", capabilities=_LOCAL_ONLY)},
        users=[_make_user("u-alice", "alice", "Alice"), _make_user("u-bob", "bob")],
    )
    session_id = await svc.open_session(
        app_id="chess", target=_local_target("u-bob"), actor_user_id="u-alice"
    )
    await svc.send_message(
        app_id="chess",
        session_id=session_id,
        target=_local_target("u-bob"),
        payload={"move": "e2e4"},
        actor_user_id="u-alice",
    )
    assert fed.sent_events == [] and fed.sent_app_messages == []
    assert ws.calls or ws.user_calls


@pytest.mark.asyncio
async def test_an_app_without_people_cannot_open_even_a_local_session():
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app("chess", capabilities=("storage",))},
        users=[_make_user("u-alice", "alice", "Alice"), _make_user("u-bob", "bob")],
    )
    with pytest.raises(AppPermissionDeniedError) as ei:
        await svc.open_session(
            app_id="chess", target=_local_target("u-bob"), actor_user_id="u-alice"
        )
    assert ei.value.permission == "people"


@pytest.mark.asyncio
async def test_inbound_for_an_app_without_federation_is_dropped():
    """Another household's copy of the app can't reach an install that
    never agreed to talk to other households."""
    svc, _, ws = _make_svc(
        apps={"chess": _make_installed_app("chess", capabilities=_LOCAL_ONLY)},
        users=[_make_user("u1", "alice")],
        peer_version=17,
    )
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_SESSION,
            {"app_id": "chess", "session_id": "s1", "verb": "open"},
        )
    )
    await svc.on_inbound_message("peer-inst-id", "chess", "s1", {"move": "e2e4"})
    assert ws.calls == [] and ws.user_calls == []
    assert svc._app_repo.pending == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_declining_after_federation_was_taken_away_sends_nothing():
    svc, fed, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-bob", "bob", "Bob")],
        remote_users=[_carol()],
    )
    await _remote_invite(svc)
    svc._app_repo._apps["chess"] = _make_installed_app(  # type: ignore[attr-defined]
        "chess", capabilities=_LOCAL_ONLY
    )
    await svc.decline_session(
        app_id="chess", session_id="sess-1", actor_user_id="u-bob"
    )
    assert svc._app_repo.invites == []  # type: ignore[attr-defined]
    assert fed.sent_events == []


# ─── D3: invites go through Social Home (R3 PR 9) ───────────────────────────


@pytest.mark.asyncio
async def test_an_apps_contacts_are_only_who_was_picked_and_who_they_play_with():
    """The app learns who the person sent an invite to (through the sheet)
    and who they play with after Play — never the roster, and never an
    inviter whose invite still waits in Needs you."""
    svc, _, _, _ = _mailbox_svc()
    # Nothing yet: the roster (Ann, Carol, Dave) is not the app's to see.
    assert await svc.list_contacts(app_id="chess", self_user_id="u-bob") == []
    await svc.open_session(
        app_id="chess", target=_local_target("u-ann"), actor_user_id="u-bob"
    )
    await _remote_invite(svc, "sess-carol")  # Carol's invite, still waiting
    refs = [
        c["user_ref"]
        for c in await svc.list_contacts(app_id="chess", self_user_id="u-bob")
    ]
    assert refs == ["u-ann"]
    await svc.accept_session(
        app_id="chess", session_id="sess-carol", actor_user_id="u-bob"
    )
    contacts = await svc.list_contacts(app_id="chess", self_user_id="u-bob")
    assert {(c["user_ref"], c["is_local"]) for c in contacts} == {
        ("u-ann", True),
        ("carol", False),
    }
    carol = next(c for c in contacts if c["user_ref"] == "carol")
    assert carol["instance_id"] == "peer-inst-id"


@pytest.mark.asyncio
async def test_contacts_leave_out_a_blocked_person_and_an_old_game():
    svc, _, _, _ = _mailbox_svc(block_pairs={("u-bob", "u-ann")})
    await _know(svc, "u-bob", "s-ann", _local_target("u-ann"))
    await _know(svc, "u-bob", "s-carol", _remote_target("peer-inst-id", "carol"))
    old = (datetime.now(UTC) - timedelta(days=91)).isoformat()
    row = await svc._app_repo.get_pending_session("chess", "u-bob", "s-carol")
    svc._app_repo._swap(row, dataclasses.replace(row, created_at=old, updated_at=old))
    assert await svc.list_contacts(app_id="chess", self_user_id="u-bob") == []


@pytest.mark.asyncio
async def test_a_local_only_app_sees_and_invites_only_this_household():
    """Without ``federation`` the sheet lists only people here, and the
    app's contacts leave out a remote game from before."""
    svc, _, _, _ = _mailbox_svc()
    svc._app_repo._apps["chess"] = _make_installed_app(
        "chess", capabilities=("storage", "people")
    )
    people = await svc.list_invitable(app_id="chess", self_user_id="u-bob")
    assert [(p["user_ref"], p["is_local"]) for p in people] == [("u-ann", True)]
    await _know(svc, "u-bob", "s-carol", _remote_target("peer-inst-id", "carol"))
    assert await svc.list_contacts(app_id="chess", self_user_id="u-bob") == []


@pytest.mark.asyncio
async def test_a_protected_childs_picker_lists_only_people_it_can_message():
    """D5 (§CP.F3): a protected account's sheet lists only households it
    could already DM — and it can't open, message or receive outside them."""
    svc, fed, ws, _, cp = _guarded_svc(set())
    cp.no_dm.add(("u-kid", "peer-inst-id"))
    kid = {
        c["user_ref"]
        for c in await svc.list_invitable(app_id="chess", self_user_id="u-kid")
    }
    assert kid == {"u-adult"}
    adult = {
        c["user_ref"]
        for c in await svc.list_invitable(app_id="chess", self_user_id="u-adult")
    }
    assert adult == {"u-kid", "mallory"}  # the rule is the child's, not theirs
    target = {"instance_id": "peer-inst-id", "user_ref": "mallory", "is_local": False}
    with pytest.raises(AppContactNotFoundError):
        await svc.open_session(app_id="chess", target=target, actor_user_id="u-kid")
    await _know(svc, "u-kid", "s", target)
    with pytest.raises(AppContactNotFoundError):
        await svc.send_message(
            app_id="chess",
            target=target,
            session_id="s",
            payload={"m": 1},
            actor_user_id="u-kid",
        )
    assert await svc.list_contacts(app_id="chess", self_user_id="u-kid") == []
    # Mallory's invite to the child is dropped on receipt.
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_SESSION,
            {
                "app_id": "chess",
                "session_id": "from-mallory",
                "verb": "open",
                "to_user": "kid",
                "from_user": "mallory",
            },
        )
    )
    assert svc._app_repo.invites == []
    assert ws.user_calls == []
    assert fed.sent_events == [] and fed.sent_app_messages == []


async def _invite_carol(svc) -> str:
    return await svc.open_session(
        app_id="chess",
        target=_remote_target("peer-inst-id", "carol"),
        actor_user_id="u-bob",
    )


async def _withdraw(svc, sid: str) -> None:
    await svc.cancel_session(
        app_id="chess",
        session_id=sid,
        target=_remote_target("peer-inst-id", "carol"),
        actor_user_id="u-bob",
    )


@pytest.mark.asyncio
async def test_at_most_ten_unanswered_invites_per_person_and_app():
    svc, fed, _, _ = _mailbox_svc()
    sids = [await _invite_carol(svc) for _ in range(INVITES_OPEN_MAX)]
    counts = await svc.invite_counts(app_id="chess", user_id="u-bob")
    assert counts == {"open": 10, "max_open": 10, "today": 10, "max_per_day": 30}
    with pytest.raises(AppInviteLimitError) as exc:
        await _invite_carol(svc)
    assert exc.value.code == "APP_INVITES_OPEN_LIMIT"
    assert exc.value.params == {"limit": 10}
    assert len(fed.sent_events) == 10
    # Carol answers one (a move): a slot frees up.
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_MESSAGE,
            {
                "app_id": "chess",
                "session_id": sids[0],
                "to_user": "bob",
                "from_user": "carol",
                "data": {"san": "e5"},
            },
        )
    )
    await _invite_carol(svc)
    # Withdrawing one frees another.
    await _withdraw(svc, sids[1])
    await _invite_carol(svc)
    assert (await svc.invite_counts(app_id="chess", user_id="u-bob"))["open"] == 10


@pytest.mark.asyncio
async def test_at_most_thirty_new_invites_a_day():
    """Answered and withdrawn invites still count for the day."""
    svc, _, _, _ = _mailbox_svc()
    for _ in range(INVITES_PER_DAY_MAX):
        await _withdraw(svc, await _invite_carol(svc))
    with pytest.raises(AppInviteLimitError) as exc:
        await _invite_carol(svc)
    assert exc.value.code == "APP_INVITES_DAILY_LIMIT"
    assert exc.value.params == {"limit": 30}
    # Yesterday's invites don't count.
    day_ago = (datetime.now(UTC) - timedelta(days=1, minutes=1)).isoformat()
    for row in list(svc._app_repo.pending):
        svc._app_repo._swap(row, dataclasses.replace(row, created_at=day_ago))
    await _invite_carol(svc)


@pytest.mark.asyncio
async def test_the_caps_are_per_person():
    svc, _, _, _ = _mailbox_svc()
    for _ in range(INVITES_OPEN_MAX):
        await _invite_carol(svc)
    # Ann still can; and an invite Bob *received* never counts for him.
    await svc.open_session(
        app_id="chess", target=_local_target("u-bob"), actor_user_id="u-ann"
    )
    assert (await svc.invite_counts(app_id="chess", user_id="u-ann"))["open"] == 1
    assert (await svc.invite_counts(app_id="chess", user_id="u-bob"))["open"] == 10


@pytest.mark.asyncio
async def test_nothing_goes_out_in_a_withdrawn_invite():
    svc, fed, _, _ = _mailbox_svc()
    sid = await _invite_carol(svc)
    await _withdraw(svc, sid)
    with pytest.raises(AppSessionUnknownError):
        await _move(svc, sid)
    assert fed.sent_app_messages == []


@pytest.mark.asyncio
async def test_a_target_mislabelled_local_or_remote_is_refused():
    """``is_local`` must match where the person lives: a game can't be
    steered between the WebSocket and the federation path."""
    svc, fed, ws, _ = _mailbox_svc()
    sid = await svc.open_session(
        app_id="chess", target=_local_target("u-ann"), actor_user_id="u-bob"
    )
    ws.user_calls.clear()
    with pytest.raises(AppSessionUnknownError):
        await _move(svc, sid, {**_local_target("u-ann"), "is_local": False})
    with pytest.raises(AppSessionUnknownError):
        await _move(
            svc,
            sid,
            {"instance_id": "peer-inst-id", "user_ref": "u-ann", "is_local": True},
        )
    assert fed.sent_app_messages == [] and ws.user_calls == []


@pytest.mark.asyncio
async def test_an_inbound_invite_cant_pose_as_one_this_person_sent():
    """Our own marks (sent / answered / withdrawn) are stripped from an
    inbound open, so another household can't eat this person's invite caps
    or stop their game."""
    svc, _, _, _ = _mailbox_svc()
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_SESSION,
            {
                "app_id": "chess",
                "session_id": "sess-x",
                "verb": "open",
                "to_user": "bob",
                "from_user": "carol",
                "sent": True,
                "withdrawn": True,
            },
        )
    )
    (row,) = svc._app_repo.invites
    assert "sent" not in row.payload and "withdrawn" not in row.payload
    await svc.accept_session(app_id="chess", session_id="sess-x", actor_user_id="u-bob")
    assert (await svc.invite_counts(app_id="chess", user_id="u-bob"))["open"] == 0
    await _move(svc, "sess-x")  # not withdrawn: the game goes on


@pytest.mark.asyncio
async def test_a_v18_household_cannot_fan_a_binary_move_out_to_everyone():
    """A household that addresses people must name one on the binary path
    too — never the whole household (D3, D5)."""
    app = _make_installed_app("chess")
    users = [_make_user("u1", "alice"), _make_user("u2", "bob")]
    svc, _, ws = _make_svc(apps={"chess": app}, users=users)
    await svc.on_inbound_message("peer-inst-id", "chess", "sess-bin", {"move": "d5"})
    assert ws.calls == [] and ws.user_calls == []


@pytest.mark.asyncio
async def test_a_move_for_a_session_the_person_does_not_play_never_reaches_their_app():
    """D3: a made-up session id, a game with someone else, or an invite
    still waiting in Needs you — none reaches the app live. The waiting
    invite's message is kept for after Play."""
    svc, _, ws, _ = _mailbox_svc()
    await _remote_invite(svc, "waiting")
    await _know(svc, "u-bob", "with-dave", _remote_target("peer-inst-id", "dave"))
    for sid in ("made-up", "with-dave", "waiting"):
        await svc.on_inbound_event(
            _make_event(
                FederationEventType.APP_MESSAGE,
                {
                    "app_id": "chess",
                    "session_id": sid,
                    "to_user": "bob",
                    "from_user": "carol",
                    "data": {"san": "e5"},
                },
            )
        )
    assert ws.user_calls == []
    # Nothing is handed over for the waiting invite before Play…
    assert await svc.take_held(app_id="chess", actor_user_id="u-bob") == []
    await svc.accept_session(
        app_id="chess", session_id="waiting", actor_user_id="u-bob"
    )
    # …and after Play the move made meanwhile is there.
    (held,) = await svc.take_held(app_id="chess", actor_user_id="u-bob")
    assert held.session_id == "waiting"


@pytest.mark.asyncio
async def test_an_app_cannot_take_an_invite_in_by_messaging_it():
    """D3/D12: only Play takes an invite in — never a message the app sends
    in a session that still waits in Needs you."""
    svc, fed, _, _ = _mailbox_svc()
    await _remote_invite(svc, "sess-1")
    with pytest.raises(AppSessionUnknownError):
        await _move(svc, "sess-1")
    assert fed.sent_app_messages == []
    assert len(svc._app_repo.invites) == 1


@pytest.mark.asyncio
async def test_the_sheet_leaves_out_someone_here_who_blocked_you():
    """Offering them and then failing Send would reveal the block."""
    svc, _, _, _ = _mailbox_svc(block_pairs={("u-ann", "u-bob")})
    refs = {
        p["user_ref"]
        for p in await svc.list_invitable(app_id="chess", self_user_id="u-bob")
    }
    assert "u-ann" not in refs and "carol" in refs


@pytest.mark.asyncio
async def test_a_protected_childs_old_invite_is_not_played_or_handed_over():
    """D5: an invite (or a held move) from a household the child can't
    message — kept from before the account was protected — is not handed
    to the app."""
    svc, _, _, _, cp = _guarded_svc(set())
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_SESSION,
            {
                "app_id": "chess",
                "session_id": "old",
                "verb": "open",
                "to_user": "kid",
                "from_user": "mallory",
            },
        )
    )
    cp.no_dm.add(("u-kid", "peer-inst-id"))
    with pytest.raises(AppChallengeHandledError):
        await svc.accept_session(
            app_id="chess", session_id="old", actor_user_id="u-kid"
        )
    await _know(svc, "u-kid", "g", _remote_target("peer-inst-id", "mallory"))
    await svc._app_repo.hold_message(
        "chess",
        "u-kid",
        "g",
        from_instance="peer-inst-id",
        from_user="mallory",
        held={"kind": "message", "payload": {}},
        now="2026-10-29T12:00:00+00:00",
    )
    assert await svc.take_held(app_id="chess", actor_user_id="u-kid") == []


@pytest.mark.asyncio
async def test_an_older_households_fan_out_skips_a_protected_child_it_cant_reach():
    """D5 on the household-wide path (a pre-v_18 sender names nobody)."""
    svc, _, ws, _, cp = _guarded_svc(set(), peer_version=17)
    cp.no_dm.add(("u-kid", "peer-inst-id"))
    await svc.on_inbound_message("peer-inst-id", "chess", "s", {"m": 1})
    (call,) = ws.calls
    assert call["user_ids"] == ["u-adult"]


# ─── Continue game (owner decision 2026-10-29) ───────────────────────────────


async def _continue(svc, sid: str, target: dict | None = None, actor: str = "u-bob"):
    return await svc.continue_session(
        app_id="chess",
        session_id=sid,
        target=target or _remote_target("peer-inst-id", "carol"),
        actor_user_id=actor,
    )


async def _inbound_continue(
    svc, sid: str, from_user: str = "carol", inst: str = "peer-inst-id"
):
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_SESSION,
            {
                "app_id": "chess",
                "session_id": sid,
                "verb": "continue",
                "to_user": "bob",
                "from_user": from_user,
            },
            from_instance=inst,
        )
    )


@pytest.mark.asyncio
async def test_an_active_game_stays_known_for_90_days():
    svc, _, _, _ = _mailbox_svc()
    await _know(svc, "u-bob", "g", _remote_target("peer-inst-id", "carol"))
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    row = await svc._app_repo.get_pending_session("chess", "u-bob", "g")
    svc._app_repo._swap(row, dataclasses.replace(row, created_at=old, updated_at=old))
    contacts = await svc.list_contacts(app_id="chess", self_user_id="u-bob")
    assert [c["user_ref"] for c in contacts] == ["carol"]
    await _move(svc, "g")  # still playable after 60 quiet days


@pytest.mark.asyncio
async def test_continue_registers_the_game_and_asks_the_other_side():
    svc, fed, _, _ = _mailbox_svc()
    with pytest.raises(AppSessionUnknownError):
        await _move(svc, "old-game")
    assert await _continue(svc, "old-game") == "old-game"
    (sent,) = fed.sent_events
    assert sent["payload"] == {
        "app_id": "chess",
        "session_id": "old-game",
        "verb": "continue",
        "to_user": "carol",
        "from_user": "bob",
    }
    row = await svc._app_repo.get_pending_session("chess", "u-bob", "old-game")
    assert row.state == "active" and row.sent and row.is_with("peer-inst-id", "carol")
    await _move(svc, "old-game")  # now known
    # The same again is a no-op: nothing more goes out.
    await _continue(svc, "old-game")
    assert len(fed.sent_events) == 1


@pytest.mark.asyncio
async def test_continue_never_moves_a_session_id_to_someone_else():
    svc, fed, _, _ = _mailbox_svc()
    await _know(svc, "u-bob", "g", _remote_target("peer-inst-id", "carol"))
    with pytest.raises(AppSessionTakenError):
        await _continue(svc, "g", _remote_target("peer-inst-id", "dave"))
    await _remote_invite(svc, "waiting")  # Carol's invite, not yet played
    with pytest.raises(AppSessionTakenError):
        await _continue(svc, "waiting")
    assert fed.sent_events == []


@pytest.mark.asyncio
async def test_continue_needs_someone_the_sheet_lists_and_counts_as_an_invite():
    svc, fed, _, _ = _mailbox_svc(blocked_ids={"ru-carol"})
    with pytest.raises(AppContactNotFoundError):
        await _continue(svc, "g")
    svc2, _, _, _ = _mailbox_svc()
    await _continue(svc2, "g1")
    assert (await svc2.invite_counts(app_id="chess", user_id="u-bob"))["today"] == 1
    assert fed.sent_events == []


@pytest.mark.asyncio
async def test_an_inbound_continue_waits_in_needs_you_and_holds_moves_until_play():
    svc, _, ws, bus = _mailbox_svc()
    await _inbound_continue(svc, "old-game")
    (invite,) = svc._app_repo.invites
    assert invite.payload["verb"] == "continue"
    (ev,) = [e for e in bus.published if isinstance(e, AppChallengeReceived)]
    assert ev.continued is True
    await _inbound_move(svc, {"n": 1}, session_id="old-game")
    assert ws.user_calls == []  # nothing reaches the app before Play
    await svc.accept_session(
        app_id="chess", session_id="old-game", actor_user_id="u-bob"
    )
    (held,) = await svc.take_held(app_id="chess", actor_user_id="u-bob")
    assert held.held["payload"] == {"n": 1}
    await _move(svc, "old-game")  # Bob answers in the game


@pytest.mark.asyncio
async def test_an_inbound_continue_cannot_hijack_a_known_session():
    """A continue for a session id the person already has a row for — a
    game with someone, or an invite waiting from someone — is dropped:
    nobody (another household, or another person of the same one) can
    point a session id at themselves."""
    svc, _, _, bus = _mailbox_svc()
    await _know(svc, "u-bob", "g", _remote_target("peer-inst-id", "carol"))
    await _inbound_continue(svc, "g", from_user="dave")
    await _inbound_continue(svc, "g", from_user="carol")
    row = await svc._app_repo.get_pending_session("chess", "u-bob", "g")
    assert row.state == "active" and row.is_with("peer-inst-id", "carol")
    await _remote_invite(svc, "w")  # Carol's open
    await _inbound_continue(svc, "w", from_user="dave")
    invite = await svc._app_repo.get_pending_session("chess", "u-bob", "w")
    assert invite.from_user == "carol"
    assert not any(
        isinstance(e, AppChallengeReceived) and e.continued for e in bus.published
    )


@pytest.mark.asyncio
async def test_a_local_continue_asks_the_person_here():
    svc, fed, _, bus = _mailbox_svc()
    await _continue(svc, "g", _local_target("u-ann"))
    assert fed.sent_events == []
    (invite,) = svc._app_repo.invites
    assert (invite.user_id, invite.from_user, invite.payload["verb"]) == (
        "u-ann",
        "u-bob",
        "continue",
    )
    (ev,) = [e for e in bus.published if isinstance(e, AppChallengeReceived)]
    assert ev.continued and ev.to_user_id == "u-ann"


# ─── D10: an invite for an app this household doesn't have waits ──────────────
#
# Not installed / turned off / not allowed for the person: the invite still
# reaches the person (a waiting "Needs you" row, named from this household's
# catalog), never anything back to the sender; it becomes a normal invite
# once the app is added or turned on.


class _FakeNotifRepo:
    """The notification rows the service reads (``find`` on equality)."""

    def __init__(self, rows: list | None = None) -> None:
        self.rows: list = rows or []

    async def find(self, spec) -> list:
        out = list(self.rows)
        for col, op, value in spec.where:
            assert op == "="
            out = [n for n in out if getattr(n, col) == value]
        return out


class _FakeCatalog:
    """The catalog as AppService.browse_catalog_for shows it to a person —
    with the D5 age filter (the catalog's rating)."""

    def __init__(self, entries: list, cp: _FakeCpRepo | None = None) -> None:
        self.entries = entries
        self.cp = cp
        self.fail = False

    async def browse_catalog_for(self, *, user_id: str, is_admin: bool) -> list:
        if self.fail:
            raise RuntimeError("catalog unreachable")
        p = self.cp._records.get(user_id) if self.cp else None
        if p and p["child_protection_enabled"]:
            return [e for e in self.entries if (e.min_age or 0) <= p["declared_age"]]
        return list(self.entries)


def _catalog_chess(**kw) -> AppCatalogEntry:
    return AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="2026.10.29",
        description="Play chess",
        icon_url=None,
        capabilities=("storage", "federation"),
        bundle_url="https://example.com/chess.tgz",
        bundle_sha256="x",
        **kw,
    )


_BEN = _make_remote_user(
    user_id="ru-ben",
    instance_id="peer-inst-id",
    remote_username="ben",
    display_name="Ben",
)
_MILO = _make_user("u-milo", "milo", "Milo")


def _waiting_svc(
    *,
    apps: dict | None = None,
    entries: list | None = None,
    cp_repo: _FakeCpRepo | None = None,
    block_pairs: set | None = None,
    rows: list | None = None,
):
    bus = _FakeBus()
    notifs = _FakeNotifRepo(rows)
    catalog = _FakeCatalog(
        [_catalog_chess()] if entries is None else entries, cp=cp_repo
    )
    svc, fed, ws = _make_svc(
        apps=apps or {},
        users=[_MILO],
        remote_users=[_BEN],
        instances=[_make_remote_instance(display_name="Lakeside")],
        cp_repo=cp_repo,
        block_pairs=block_pairs,
        bus=bus,
    )
    svc._notifs = notifs  # type: ignore[assignment]
    svc._catalog = catalog  # type: ignore[assignment]
    return svc, fed, ws, bus, notifs, catalog


def _open_event(session_id: str = "s1", **extra) -> FederationEvent:
    return _make_event(
        FederationEventType.APP_SESSION,
        {
            "app_id": "chess",
            "session_id": session_id,
            "verb": "open",
            "to_user": "milo",
            "from_user": "ben",
            **extra,
        },
    )


def _waits(bus: _FakeBus) -> list[AppChallengeWaiting]:
    return [e for e in bus.published if isinstance(e, AppChallengeWaiting)]


def _nothing_sent(fed: _FakeFederation, ws: _FakeWs) -> None:
    """The sender learns nothing: no envelope, no frame, of any kind."""
    assert fed.sent_events == []
    assert fed.sent_app_messages == []
    assert ws.calls == []
    assert ws.user_calls == []


@pytest.mark.asyncio
async def test_invite_for_an_app_not_installed_waits_for_the_person():
    svc, fed, ws, bus, _, _ = _waiting_svc()
    await svc.on_inbound_event(_open_event())
    [w] = _waits(bus)
    assert (w.app_id, w.session_id, w.to_user_id) == ("chess", "s1", "u-milo")
    assert w.reason == "app_missing"
    assert w.app_name == "Chess"  # the household's catalog, not the invite
    assert w.from_user_id == "ru-ben"
    assert w.from_display == "Ben · Lakeside"
    # No session row (the app isn't here); nothing to the sender.
    assert svc._app_repo.pending == []
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_invite_names_the_app_from_the_catalog_never_the_payload():
    svc, _, _, bus, _, _ = _waiting_svc()
    await svc.on_inbound_event(_open_event(name="Free Prizes", app_name="Free"))
    [w] = _waits(bus)
    assert w.app_name == "Chess"


@pytest.mark.asyncio
async def test_invite_for_a_turned_off_app_waits_with_its_reason():
    app = _make_installed_app("chess", enabled=False)
    svc, fed, ws, bus, _, _ = _waiting_svc(apps={"chess": app}, entries=[])
    await svc.on_inbound_event(_open_event())
    [w] = _waits(bus)
    assert w.reason == "app_off"
    assert w.app_name == "Chess"
    assert svc._app_repo.pending == []
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_invite_for_an_app_the_catalog_doesnt_list_is_dropped():
    svc, fed, ws, bus, _, _ = _waiting_svc(entries=[])
    await svc.on_inbound_event(_open_event())
    assert _waits(bus) == []
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_invite_is_dropped_when_the_catalog_cant_be_read():
    svc, fed, ws, bus, _, catalog = _waiting_svc()
    catalog.fail = True
    await svc.on_inbound_event(_open_event())
    assert _waits(bus) == []
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_age_gated_child_gets_no_row_for_a_missing_app():
    """D5: an app rated above a protected child's age stays hidden — no
    row for the child, no ask to a guardian, nothing to the sender."""
    cp = _FakeCpRepo()
    cp.add("u-milo", enabled=True, declared_age=9)
    svc, fed, ws, bus, _, _ = _waiting_svc(
        entries=[_catalog_chess(min_age=13)], cp_repo=cp
    )
    await svc.on_inbound_event(_open_event())
    assert bus.published == []
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_age_gated_child_gets_no_row_for_a_turned_off_app():
    cp = _FakeCpRepo()
    cp.add("u-milo", enabled=True, declared_age=9)
    app = _make_installed_app("chess", enabled=False, min_age=13)
    svc, fed, ws, bus, _, _ = _waiting_svc(apps={"chess": app}, cp_repo=cp)
    await svc.on_inbound_event(_open_event())
    assert bus.published == []
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_child_old_enough_gets_the_waiting_row():
    cp = _FakeCpRepo()
    cp.add("u-milo", enabled=True, declared_age=14)
    svc, _, _, bus, _, _ = _waiting_svc(
        entries=[_catalog_chess(min_age=13)], cp_repo=cp
    )
    await svc.on_inbound_event(_open_event())
    assert len(_waits(bus)) == 1


@pytest.mark.asyncio
async def test_invite_from_a_blocked_sender_leaves_no_row():
    svc, fed, ws, bus, _, _ = _waiting_svc(block_pairs={("u-milo", "ru-ben")})
    await svc.on_inbound_event(_open_event())
    assert bus.published == []
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_invite_across_a_guardian_block_leaves_no_row():
    svc, fed, ws, bus, _, _ = _waiting_svc()
    svc.attach_child_protection(  # type: ignore[arg-type]
        _FakeChildProtection({frozenset({"u-milo", "ru-ben"})})
    )
    await svc.on_inbound_event(_open_event())
    assert _waits(bus) == []
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_protected_invitee_only_from_a_household_it_may_message():
    svc, fed, ws, bus, _, _ = _waiting_svc()
    gate = _FakeChildProtection({frozenset({"u-milo", "someone-else"})})
    gate.no_dm.add(("u-milo", "peer-inst-id"))
    svc.attach_child_protection(gate)  # type: ignore[arg-type]
    await svc.on_inbound_event(_open_event())
    assert _waits(bus) == []
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_invite_from_an_unknown_sender_or_to_nobody_leaves_no_row():
    svc, fed, ws, bus, _, _ = _waiting_svc()
    await svc.on_inbound_event(_open_event(from_user="stranger"))
    await svc.on_inbound_event(_open_event("s2", to_user="nobody"))
    assert bus.published == []
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_a_continue_or_a_message_for_a_missing_app_leaves_no_row():
    svc, fed, ws, bus, _, _ = _waiting_svc()
    await svc.on_inbound_event(_open_event(verb="continue"))
    await svc.on_inbound_event(
        _make_event(
            FederationEventType.APP_MESSAGE,
            {
                "app_id": "chess",
                "session_id": "s1",
                "to_user": "milo",
                "from_user": "ben",
                "data": {"move": "e4"},
            },
        )
    )
    assert bus.published == []
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_odd_session_id_leaves_no_row():
    svc, _, _, bus, _, _ = _waiting_svc()
    await svc.on_inbound_event(_open_event("a/b"))
    assert bus.published == []


@pytest.mark.asyncio
async def test_app_without_federation_permission_leaves_no_row():
    off = _make_installed_app("chess", enabled=False, capabilities=("storage",))
    svc, _, _, bus, _, _ = _waiting_svc(apps={"chess": off})
    await svc.on_inbound_event(_open_event())
    svc2, _, _, bus2, _, _ = _waiting_svc(
        entries=[_catalog_chess(permissions=("storage",))]
    )
    await svc2.on_inbound_event(_open_event())
    assert bus.published == []
    assert bus2.published == []


def _waiting_row(
    *,
    user_id: str = "u-milo",
    session_id: str = "s1",
    actor: str = "ru-ben",
    created_at: str | None = None,
    reason: str = "app_missing",
    handled: bool = False,
) -> Notification:
    return Notification(
        id=f"n-{user_id}-{session_id}",
        user_id=user_id,
        type="app_challenge_waiting",
        title="Ben invited you to play Chess",
        created_at=created_at or datetime.now(UTC).isoformat(),
        object_key=f"app_session:chess/{session_id}",
        params=NotificationParams(
            actors=(NotificationActor(actor, "Ben"),),
            actor_count=1,
            space_name="Chess",
            flags=(reason,),
            handled=NotificationHandled("declined") if handled else None,
        ),
    )


@pytest.mark.asyncio
async def test_the_same_invite_again_keeps_one_row():
    svc, _, _, bus, _, _ = _waiting_svc(rows=[_waiting_row()])
    await svc.on_inbound_event(_open_event())
    assert _waits(bus) == []


@pytest.mark.asyncio
async def test_turned_off_app_never_re_points_a_known_session():
    app = _make_installed_app("chess", enabled=False)
    svc, _, _, bus, _, _ = _waiting_svc(apps={"chess": app})
    await svc._app_repo.add_pending_session(
        AppPendingSession(
            app_id="chess",
            user_id="u-milo",
            session_id="s1",
            from_instance="other",
            from_user="eve",
            payload={},
            created_at=datetime.now(UTC).isoformat(),
        )
    )
    await svc.on_inbound_event(_open_event())
    assert _waits(bus) == []


@pytest.mark.asyncio
async def test_sender_withdraws_a_waiting_invite():
    svc, fed, ws, bus, _, _ = _waiting_svc(rows=[_waiting_row()])
    await svc.on_inbound_event(_open_event(verb="close", reason="withdrawn"))
    [ended] = [e for e in bus.published if isinstance(e, AppChallengeEnded)]
    assert ended.outcome == "withdrawn"
    assert (ended.user_id, ended.session_ids) == ("u-milo", ("s1",))
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_only_the_sender_withdraws_a_waiting_invite():
    eve = _make_remote_user("ru-eve", "peer-inst-id", "eve", "Eve")
    svc, _, _, bus, _, _ = _waiting_svc(rows=[_waiting_row()])
    svc._user_repo._remote_users.append(eve)  # type: ignore[attr-defined]
    await svc.on_inbound_event(
        _open_event(verb="close", reason="withdrawn", from_user="eve")
    )
    await svc.on_inbound_event(
        _open_event("s9", verb="close", reason="withdrawn", from_user="ghost")
    )
    assert bus.published == []


@pytest.mark.asyncio
async def test_decline_a_waiting_invite_sends_nothing():
    svc, fed, ws, bus, _, _ = _waiting_svc(rows=[_waiting_row()])
    assert await svc.decline_waiting(
        app_id="chess", session_id="s1", actor_user_id="u-milo"
    )
    [ended] = bus.published
    assert isinstance(ended, AppChallengeEnded)
    assert ended.outcome == "declined"
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_decline_waiting_is_false_without_such_an_invite():
    svc, _, _, bus, _, _ = _waiting_svc(rows=[_waiting_row(handled=True)])
    assert not await svc.decline_waiting(
        app_id="chess", session_id="s1", actor_user_id="u-milo"
    )
    # Someone else's invite isn't theirs to decline.
    assert not await svc.decline_waiting(
        app_id="chess", session_id="s1", actor_user_id="u-other"
    )
    assert bus.published == []


@pytest.mark.asyncio
async def test_without_notifications_wired_nothing_waits():
    svc, fed, ws = _make_svc(users=[_MILO], remote_users=[_BEN], bus=_FakeBus())
    assert not await svc.decline_waiting(
        app_id="chess", session_id="s1", actor_user_id="u-milo"
    )
    await svc.on_inbound_event(_open_event())
    await svc.on_app_usable(AppEnabledChanged(app_id="chess", enabled=True))
    assert svc._bus.published == []  # type: ignore[union-attr]
    _nothing_sent(fed, ws)


def _installed() -> AppInstalled:
    return AppInstalled(app_id="chess", name="Chess", installed_by="anna")


@pytest.mark.asyncio
async def test_installing_the_app_makes_the_waiting_invite_playable():
    created = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    svc, fed, ws, bus, _, _ = _waiting_svc(
        apps={"chess": _make_installed_app("chess")},
        rows=[_waiting_row(created_at=created)],
    )
    await svc.on_app_usable(_installed())
    [invite] = svc._app_repo.invites
    assert (invite.user_id, invite.session_id) == ("u-milo", "s1")
    assert (invite.from_instance, invite.from_user) == ("peer-inst-id", "ben")
    # The TTL counts from when the invite arrived.
    assert invite.created_at == created
    assert invite.payload == {"app_id": "chess", "session_id": "s1", "verb": "open"}
    [received] = [e for e in bus.published if isinstance(e, AppChallengeReceived)]
    assert received.to_user_id == "u-milo"
    assert received.from_user_id == "ru-ben"
    assert received.from_display == "Ben · Lakeside"
    # Becoming playable is local: still nothing to the sender.
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_turning_the_app_on_makes_the_waiting_invite_playable():
    svc, _, _, bus, _, _ = _waiting_svc(
        apps={"chess": _make_installed_app("chess")},
        rows=[_waiting_row(reason="app_off")],
    )
    await svc.on_app_usable(AppEnabledChanged(app_id="chess", enabled=False))
    assert bus.published == []
    await svc.on_app_usable(AppEnabledChanged(app_id="chess", enabled=True))
    assert len(svc._app_repo.invites) == 1


@pytest.mark.asyncio
async def test_app_still_off_or_gone_makes_nothing_playable():
    off = _make_installed_app("chess", enabled=False)
    svc, _, _, bus, _, _ = _waiting_svc(apps={"chess": off}, rows=[_waiting_row()])
    await svc.on_app_usable(_installed())
    svc2, _, _, bus2, _, _ = _waiting_svc(rows=[_waiting_row()])
    await svc2.on_app_usable(_installed())
    assert bus.published == []
    assert bus2.published == []


@pytest.mark.asyncio
async def test_a_waiting_invite_past_its_ttl_expires_on_install():
    old = (datetime.now(UTC) - timedelta(days=15)).isoformat()
    svc, _, _, bus, _, _ = _waiting_svc(
        apps={"chess": _make_installed_app("chess")},
        rows=[_waiting_row(created_at=old)],
    )
    await svc.on_app_usable(_installed())
    [ended] = bus.published
    assert isinstance(ended, AppChallengeEnded)
    assert ended.outcome == "expired"
    assert svc._app_repo.pending == []


@pytest.mark.asyncio
async def test_install_rechecks_the_receiver_rules():
    """A block since, the installed app's age gate, a sender gone, an
    install without ``federation``: the invite ends ``done`` instead."""
    cp = _FakeCpRepo()
    cp.add("u-milo", enabled=True, declared_age=9)
    cases = [
        _waiting_svc(
            apps={"chess": _make_installed_app("chess")},
            rows=[_waiting_row()],
            block_pairs={("u-milo", "ru-ben")},
        ),
        _waiting_svc(
            apps={"chess": _make_installed_app("chess", min_age=13)},
            rows=[_waiting_row()],
            cp_repo=cp,
        ),
        _waiting_svc(
            apps={"chess": _make_installed_app("chess")},
            rows=[_waiting_row(actor="ru-gone")],
        ),
        _waiting_svc(
            apps={"chess": _make_installed_app("chess", capabilities=("storage",))},
            rows=[_waiting_row()],
        ),
    ]
    for svc, fed, ws, bus, _, _ in cases:
        await svc.on_app_usable(_installed())
        [ended] = bus.published
        assert isinstance(ended, AppChallengeEnded)
        assert ended.outcome == "done"
        assert svc._app_repo.pending == []
        _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_install_leaves_other_apps_and_handled_rows_alone():
    other = dataclasses.replace(
        _waiting_row(session_id="s2"), object_key="app_session:checkers/s2"
    )
    svc, _, _, bus, _, _ = _waiting_svc(
        apps={"chess": _make_installed_app("chess")},
        rows=[other, _waiting_row(handled=True)],
    )
    await svc.on_app_usable(_installed())
    assert bus.published == []


# ─── Uninstall tells opponents (D9, v_72) ─────────────────────────────────────


def _row(
    user_id: str,
    session_id: str,
    *,
    instance: str = "peer-inst-id",
    other: str = "carol",
    state: str = "active",
    payload: dict | None = None,
    held: dict | None = None,
    days_ago: int = 0,
) -> AppPendingSession:
    when = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    return AppPendingSession(
        app_id="chess",
        user_id=user_id,
        session_id=session_id,
        from_instance=instance,
        from_user=other,
        payload=payload or {"app_id": "chess", "session_id": session_id},
        created_at=when,
        state=state,
        held=held,
        updated_at=when,
    )


def _uninstall_svc(**kw):
    return _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[
            _make_user("u-alice", "alice", "Alice"),
            _make_user("u-bob", "bob", "Bob"),
        ],
        remote_users=[
            _carol(),
            _make_remote_user("ru-dan", "peer-2", "dan", "Dan"),
        ],
        instances=[
            _make_remote_instance("peer-inst-id", display_name="Familie Okafor"),
            _make_remote_instance("peer-2", display_name="Brookside"),
        ],
        peer_version=72,
        **kw,
    )


@pytest.mark.asyncio
async def test_sessions_ending_counts_games_and_invites_with_other_households():
    svc, fed, _ = _uninstall_svc()
    sent = {"sent": True}
    svc._app_repo.pending += [
        _row("u-alice", "game"),  # a game being played
        _row("u-alice", "waits", state="invited"),  # an invite to Alice
        _row("u-bob", "asked", payload=sent),  # Bob's invite, unanswered
        _row("u-bob", "answered", payload={"sent": True, "answered": True}),
        _row("u-bob", "took-back", payload={"sent": True, "withdrawn": True}),
        _row(
            "u-bob",
            "over",
            held={"kind": "session", "payload": {"verb": "close", "reason": "x"}},
        ),
        _row("u-alice", "local", instance="own-inst-id", other="u-bob"),
        # Quiet for over a month: most likely over — left to the TTL.
        _row("u-alice", "quiet", days_ago=31),
        # An invite that waits counts however old (its own 14-day TTL).
        _row("u-bob", "old-invite", state="invited", days_ago=10),
    ]
    fed.peer_versions["peer-2"] = 71
    svc._app_repo.pending.append(_row("u-alice", "old", instance="peer-2", other="dan"))
    ending = await svc.sessions_ending("chess")
    assert [(r.session_id, kind, modern, tell) for r, kind, modern, tell in ending] == [
        ("game", "game", True, True),
        ("waits", "invite", True, True),
        ("asked", "invite", True, True),
        ("answered", "game", True, True),
        ("old-invite", "invite", True, True),
        ("old", "game", False, True),
    ]


@pytest.mark.asyncio
async def test_preview_names_who_is_told_and_counts_the_untold():
    svc, fed, _ = _uninstall_svc()
    svc._app_repo.people = 4
    fed.peer_versions["peer-2"] = 71
    svc._app_repo.pending += [
        _row("u-alice", "g1"),
        _row("u-bob", "g2"),  # a second game with Carol: named once
        _row("u-alice", "g3", instance="peer-2", other="dan"),  # older household
        _row("u-bob", "i1", state="invited"),
    ]
    preview = await svc.preview_uninstall("chess")
    assert preview.people == 4
    assert (preview.games, preview.invites, preview.untold) == (3, 1, 1)
    assert preview.told == ("Bob · Familie Okafor",)
    assert preview.told_more == 0


@pytest.mark.asyncio
async def test_preview_names_at_most_three():
    remotes = [
        _make_remote_user(f"ru-{n}", "peer-inst-id", n, n.title())
        for n in ("ben", "clara", "omer", "zoe")
    ]
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-alice", "alice", "Alice")],
        remote_users=remotes,
        peer_version=72,
    )
    for r in remotes:
        svc._app_repo.pending.append(
            _row("u-alice", f"s-{r.remote_username}", other=r.remote_username)
        )
    preview = await svc.preview_uninstall("chess")
    assert preview.told == ("Ben", "Clara", "Omer")
    assert preview.told_more == 1


@pytest.mark.asyncio
async def test_uninstall_sends_one_ended_close_per_household():
    svc, fed, _ = _uninstall_svc()
    svc._app_repo.pending += [
        _row("u-alice", "g1"),
        _row("u-bob", "g2"),
        _row("u-bob", "i1", state="invited"),
        _row("u-alice", "g3", instance="peer-2", other="dan"),
    ]
    await svc.tell_opponents("chess", await svc.sessions_ending("chess"))
    by_peer = {e["to_instance_id"]: e for e in fed.sent_events}
    assert set(by_peer) == {"peer-inst-id", "peer-2"}
    assert len(fed.sent_events) == 2
    one = by_peer["peer-inst-id"]
    assert one["event_type"] is FederationEventType.APP_SESSION
    assert one["payload"] == {
        "app_id": "chess",
        "verb": "close",
        "reason": "ended",
        "sessions": [
            {"session_id": "g1", "to_user": "carol", "from_user": "alice"},
            {"session_id": "g2", "to_user": "carol", "from_user": "bob"},
            {"session_id": "i1", "to_user": "carol", "from_user": "bob"},
        ],
    }
    assert by_peer["peer-2"]["payload"]["sessions"] == [
        {"session_id": "g3", "to_user": "dan", "from_user": "alice"}
    ]


@pytest.mark.asyncio
async def test_an_older_household_hears_only_about_pending_invites():
    """Below v_72: the close it already knows — ``declined`` for an invite
    it sent us (our side said no), ``withdrawn`` for one we sent — and
    nothing for a game."""
    svc, fed, _ = _uninstall_svc()
    fed.peer_versions["peer-inst-id"] = 71
    svc._app_repo.pending += [
        _row("u-alice", "g1"),  # a game: nothing it would understand
        _row("u-bob", "i1", state="invited"),
        _row("u-bob", "i2", payload={"sent": True}),
    ]
    await svc.tell_opponents("chess", await svc.sessions_ending("chess"))
    assert sorted(
        (e["payload"]["session_id"], e["payload"]["reason"], e["payload"]["from_user"])
        for e in fed.sent_events
    ) == [("i1", "declined", "bob"), ("i2", "withdrawn", "bob")]
    assert all("sessions" not in e["payload"] for e in fed.sent_events)


@pytest.mark.asyncio
async def test_ended_closes_are_chunked():
    svc, fed, _ = _uninstall_svc()
    n = ENDED_SESSIONS_MAX + 5
    svc._app_repo.pending += [_row("u-alice", f"g{i}") for i in range(n)]
    await svc.tell_opponents("chess", await svc.sessions_ending("chess"))
    sizes = [len(e["payload"]["sessions"]) for e in fed.sent_events]
    assert sizes == [ENDED_SESSIONS_MAX, 5]


@pytest.mark.asyncio
async def test_a_household_that_cant_be_reached_doesnt_stop_the_others():
    svc, fed, _ = _uninstall_svc()
    fed.failing.add("peer-inst-id")
    svc._app_repo.pending += [
        _row("u-alice", "g1"),
        _row("u-alice", "g3", instance="peer-2", other="dan"),
    ]
    await svc.tell_opponents("chess", await svc.sessions_ending("chess"))
    assert [e["to_instance_id"] for e in fed.sent_events] == ["peer-2"]


@pytest.mark.asyncio
async def test_nothing_crosses_a_guardian_block_on_uninstall():
    svc, fed, _ = _uninstall_svc()
    svc.attach_child_protection(
        _FakeChildProtection({frozenset({"u-alice", "ru-carol"})})
    )
    svc._app_repo.pending += [_row("u-alice", "g1"), _row("u-bob", "g2")]
    await svc.tell_opponents("chess", await svc.sessions_ending("chess"))
    (event,) = fed.sent_events
    assert [e["session_id"] for e in event["payload"]["sessions"]] == ["g2"]
    # The dialog counts it as a game nobody is told about.
    preview = await svc.preview_uninstall("chess")
    assert (preview.games, preview.untold) == (2, 1)


@pytest.mark.asyncio
async def test_one_opponents_entries_stay_together_across_chunks():
    """Sorted by person before chunking: the receiver tells each player
    once per opponent per event."""
    svc, fed, _ = _uninstall_svc()
    svc._app_repo.pending += [
        _row("u-alice" if i % 2 else "u-bob", f"g{i}")
        for i in range(ENDED_SESSIONS_MAX + 4)
    ]
    await svc.tell_opponents("chess", await svc.sessions_ending("chess"))
    firsts = [
        {e["from_user"] for e in ev["payload"]["sessions"]} for ev in fed.sent_events
    ]
    assert firsts[0] == {"alice"} or "alice" not in firsts[1]


def _ended_event(sessions: list[dict], *, from_instance: str = "peer-inst-id"):
    return _make_event(
        FederationEventType.APP_SESSION,
        {"app_id": "chess", "verb": "close", "reason": "ended", "sessions": sessions},
        from_instance=from_instance,
    )


def _game_ended(bus: _FakeBus) -> list[AppGameEnded]:
    return [e for e in bus.published if isinstance(e, AppGameEnded)]


@pytest.mark.asyncio
async def test_an_ended_close_ends_the_games_and_tells_the_player_once():
    bus = _FakeBus()
    svc, _, ws = _uninstall_svc(bus=bus)
    svc._app_repo.pending += [_row("u-bob", "g1"), _row("u-bob", "g2")]
    await svc.on_inbound_event(
        _ended_event(
            [
                {"session_id": "g1", "to_user": "bob", "from_user": "carol"},
                {"session_id": "g2", "to_user": "bob", "from_user": "carol"},
            ]
        )
    )
    close = {"verb": "close", "reason": "ended"}
    for sid in ("g1", "g2"):
        row = await svc._app_repo.get_pending_session("chess", "u-bob", sid)
        assert row.held["kind"] == "session"
        assert row.held["payload"] == {"app_id": "chess", "session_id": sid, **close}
    # Live into an open app too (Bob plays both).
    assert [c["payload"]["session_id"] for c in ws.user_calls] == ["g1", "g2"]
    (told,) = _game_ended(bus)
    assert (told.to_user_id, told.from_display, told.from_user_id) == (
        "u-bob",
        "Bob · Familie Okafor",
        "ru-carol",
    )


@pytest.mark.asyncio
async def test_an_ended_close_takes_a_waiting_invite_down():
    bus = _FakeBus()
    svc, _, _ = _uninstall_svc(bus=bus)
    await _remote_invite(svc)
    await svc.on_inbound_event(
        _ended_event([{"session_id": "sess-1", "to_user": "bob", "from_user": "carol"}])
    )
    assert svc._app_repo.invites == []
    assert [(e.session_ids, e.outcome) for e in _ended(bus)] == [
        (("sess-1",), "withdrawn")
    ]
    assert _game_ended(bus) == []


@pytest.mark.asyncio
async def test_an_ended_close_is_handled_once():
    bus = _FakeBus()
    svc, _, _ = _uninstall_svc(bus=bus)
    svc._app_repo.pending.append(_row("u-bob", "g1"))
    entry = {"session_id": "g1", "to_user": "bob", "from_user": "carol"}
    await svc.on_inbound_event(_ended_event([entry]))
    await svc.on_inbound_event(_ended_event([entry]))
    assert len(_game_ended(bus)) == 1


@pytest.mark.asyncio
async def test_an_ended_close_reads_at_most_the_cap():
    bus = _FakeBus()
    svc, _, _ = _uninstall_svc(bus=bus)
    n = ENDED_SESSIONS_MAX + 3
    svc._app_repo.pending += [_row("u-bob", f"g{i}") for i in range(n)]
    await svc.on_inbound_event(
        _ended_event(
            [
                {"session_id": f"g{i}", "to_user": "bob", "from_user": "carol"}
                for i in range(n)
            ]
        )
    )
    held = [p for p in svc._app_repo.pending if p.held is not None]
    assert len(held) == ENDED_SESSIONS_MAX


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"app_id": "chess", "verb": "close", "reason": "ended", "sessions": "g1"},
        {"verb": "close", "reason": "ended", "sessions": []},
        {
            "app_id": "chess",
            "verb": "close",
            "reason": "ended",
            "sessions": [
                "g1",
                {"session_id": "g1", "to_user": "", "from_user": "carol"},
                {"session_id": "g1", "to_user": "bob"},
                {"session_id": 7, "to_user": "bob", "from_user": "carol"},
            ],
        },
    ],
)
async def test_a_malformed_ended_close_changes_nothing(payload):
    bus = _FakeBus()
    svc, _, ws = _uninstall_svc(bus=bus)
    svc._app_repo.pending.append(_row("u-bob", "g1"))
    await svc.on_inbound_event(_make_event(FederationEventType.APP_SESSION, payload))
    assert svc._app_repo.pending[0].held is None
    assert bus.published == [] and ws.user_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("from_instance", "from_user"),
    [
        ("peer-2", "dan"),  # another household: not the other side of g1
        ("peer-inst-id", "erin"),  # the right household, another person
    ],
)
async def test_only_the_other_side_can_end_a_game(from_instance, from_user):
    """Hijack: a household (or a person of it) that isn't the other side of
    Bob's game can't end it — nothing held, nothing told, the game stays."""
    bus = _FakeBus()
    svc, _, ws = _make_svc(
        apps={"chess": _make_installed_app("chess")},
        users=[_make_user("u-bob", "bob", "Bob")],
        remote_users=[
            _carol(),
            _make_remote_user("ru-dan", "peer-2", "dan", "Dan"),
            _make_remote_user("ru-erin", "peer-inst-id", "erin", "Erin"),
        ],
        peer_version=72,
        bus=bus,
    )
    svc._app_repo.pending.append(_row("u-bob", "g1"))
    await svc.on_inbound_event(
        _ended_event(
            [{"session_id": "g1", "to_user": "bob", "from_user": from_user}],
            from_instance=from_instance,
        )
    )
    (row,) = svc._app_repo.pending
    assert row.held is None and row.state == "active"
    assert _game_ended(bus) == [] and ws.user_calls == []


# ─── D10 bounds: one row per sender person, at most 5 senders ────────────────


@pytest.mark.asyncio
async def test_a_repeat_from_the_same_sender_joins_their_row():
    """The service hands the newer invite on (the notification service
    folds it into the sender's row) — it never counts as a new sender."""
    others = [_waiting_row(session_id=f"o{i}", actor=f"ru-{i}") for i in range(4)]
    svc, fed, ws, bus, _, _ = _waiting_svc(rows=[_waiting_row(), *others])
    await svc.on_inbound_event(_open_event("s2"))
    [w] = _waits(bus)
    assert (w.session_id, w.from_user_id) == ("s2", "ru-ben")
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_a_sixth_sender_is_dropped_silently():
    rows = [_waiting_row(session_id=f"o{i}", actor=f"ru-{i}") for i in range(5)]
    svc, fed, ws, bus, _, _ = _waiting_svc(rows=rows)
    await svc.on_inbound_event(_open_event("s9"))
    assert _waits(bus) == []
    _nothing_sent(fed, ws)


@pytest.mark.asyncio
async def test_install_makes_only_the_newest_invite_per_sender_playable():
    now = datetime.now(UTC)
    old = _waiting_row(
        session_id="s1", created_at=(now - timedelta(hours=2)).isoformat()
    )
    new = _waiting_row(
        session_id="s2", created_at=(now - timedelta(hours=1)).isoformat()
    )
    svc, _, _, bus, _, _ = _waiting_svc(
        apps={"chess": _make_installed_app("chess")}, rows=[old, new]
    )
    await svc.on_app_usable(_installed())
    assert [p.session_id for p in svc._app_repo.invites] == ["s2"]
    [ended] = [e for e in bus.published if isinstance(e, AppChallengeEnded)]
    assert (ended.session_ids, ended.outcome) == (("s1",), "done")


@pytest.mark.asyncio
async def test_install_keeps_the_newest_five_senders():
    """Six senders' rows (only possible from before the bound): the oldest
    sender's ends ``done`` and never becomes playable."""
    now = datetime.now(UTC)
    newer = [
        _waiting_row(
            session_id=f"n{i}",
            actor=f"ru-n{i}",
            created_at=(now - timedelta(minutes=i)).isoformat(),
        )
        for i in range(5)
    ]
    oldest = _waiting_row(
        session_id="s1", created_at=(now - timedelta(hours=3)).isoformat()
    )
    svc, _, _, bus, _, _ = _waiting_svc(
        apps={"chess": _make_installed_app("chess")}, rows=[*newer, oldest]
    )
    await svc.on_app_usable(_installed())
    ended = {
        e.session_ids[0]: e.outcome
        for e in bus.published
        if isinstance(e, AppChallengeEnded)
    }
    assert ended["s1"] == "done"
    assert all(p.session_id != "s1" for p in svc._app_repo.invites)


# ─── The invite sheet's spaces ("pick everyone from a space") ────────────


class _FakeSpaces:
    """The spaces the caller is in (``list_for_user``) and the local seats
    (``list_members``)."""

    def __init__(self, spaces: list, members: dict[str, list[tuple[str, str]]]):
        self.spaces = spaces
        self.members = members

    async def list_for_user(self, user_id: str) -> list:
        return [
            s
            for s in self.spaces
            if any(uid == user_id for uid, _ in self.members.get(s.id, ()))
        ]

    async def list_members(self, space_id: str) -> list[SpaceMember]:
        return [
            SpaceMember(space_id=space_id, user_id=uid, role=role, joined_at="x")
            for uid, role in self.members.get(space_id, ())
        ]


class _FakeRemoteMembers:
    def __init__(self, seats: dict[str, list[tuple[str, str, str]]]):
        self.seats = seats

    async def list_for_space(self, space_id: str) -> list:
        return [
            SimpleNamespace(instance_id=inst, user_id=uid, role=role)
            for inst, uid, role in self.seats.get(space_id, ())
        ]


def _with_spaces(svc, spaces: _FakeSpaces, remote: _FakeRemoteMembers) -> None:
    svc._spaces = spaces
    svc._space_remote_members = remote


def _space(sid: str, name: str, kind=SpaceType.PRIVATE, **kw) -> SimpleNamespace:
    return SimpleNamespace(
        id=sid, name=name, space_type=kind, archived=kw.get("archived", False)
    )


@pytest.mark.asyncio
async def test_the_invite_sheet_offers_spaces_with_invitable_writers():
    alice = _make_user("u-alice", "alice", "Alice")
    bob = _make_user("u-bob", "bob", "Bob")
    cara = _make_user("u-cara", "cara", "Cara")
    ben = _make_remote_user("ru-ben", "peer-inst", "ben", "Ben")
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app()},
        users=[alice, bob, cara],
        remote_users=[ben],
        own_instance_id="own-inst",
    )
    member, sub = SpaceRole.MEMBER.value, SpaceRole.SUBSCRIBER.value
    spaces = _FakeSpaces(
        [
            _space("s-fam", "family"),
            _space("s-club", "Chess club", SpaceType.PUBLIC),
            _space("s-glob", "World", SpaceType.GLOBAL),
            _space("s-old", "Old", archived=True),
            _space("s-follow", "Followed", SpaceType.PUBLIC),
            _space("s-alone", "Alone"),
        ],
        {
            # Bob writes, Cara only follows.
            "s-fam": [("u-alice", "owner"), ("u-bob", member), ("u-cara", sub)],
            "s-club": [("u-alice", "moderator"), ("u-cara", member)],
            "s-glob": [("u-alice", member), ("u-bob", member)],
            "s-old": [("u-alice", member), ("u-bob", member)],
            # The caller only follows this one: not theirs to pick from.
            "s-follow": [("u-alice", sub), ("u-bob", member)],
            "s-alone": [("u-alice", "admin")],
        },
    )
    _with_spaces(
        svc,
        spaces,
        _FakeRemoteMembers(
            {
                "s-fam": [("peer-inst", "ru-ben", "admin")],
                "s-club": [("peer-inst", "ru-ben", sub)],
            }
        ),
    )
    people, picks = await svc.invite_sheet(app_id="chess", self_user_id="u-alice")
    refs = [p["user_ref"] for p in people]
    assert refs == await _refs(svc)
    by_name = {s["name"]: [refs[i] for i in s["people"]] for s in picks}
    # Sorted by name; global, archived, followed and empty spaces left out;
    # followers (local or remote) are never offered from a space.
    assert [s["name"] for s in picks] == ["Chess club", "family"]
    assert by_name == {"Chess club": ["u-cara"], "family": ["u-bob", "ben"]}
    assert all(set(s) == {"space_id", "name", "people"} for s in picks)
    assert picks[1]["space_id"] == "s-fam"


async def _refs(svc) -> list[str]:
    return [
        p["user_ref"]
        for p in await svc.list_invitable(app_id="chess", self_user_id="u-alice")
    ]


@pytest.mark.asyncio
async def test_spaces_only_name_people_the_sheet_lists():
    """A blocked person, or another household's people for an app without
    ``federation``, isn't in ``people`` — so no space names them."""
    alice = _make_user("u-alice", "alice", "Alice")
    bob = _make_user("u-bob", "bob", "Bob")
    ben = _make_remote_user("ru-ben", "peer-inst", "ben", "Ben")
    svc, _, _ = _make_svc(
        apps={"chess": _make_installed_app(capabilities=("storage", "people"))},
        users=[alice, bob],
        remote_users=[ben],
        own_instance_id="own-inst",
        blocked_ids={"u-bob"},
    )
    _with_spaces(
        svc,
        _FakeSpaces(
            [_space("s1", "Family")],
            {"s1": [("u-alice", "owner"), ("u-bob", "member")]},
        ),
        _FakeRemoteMembers({"s1": [("peer-inst", "ru-ben", "member")]}),
    )
    people, picks = await svc.invite_sheet(app_id="chess", self_user_id="u-alice")
    assert people == [] and picks == []


@pytest.mark.asyncio
async def test_without_spaces_wired_the_sheet_has_none():
    alice = _make_user("u-alice", "alice", "Alice")
    bob = _make_user("u-bob", "bob", "Bob")
    svc, _, _ = _make_svc(apps={"chess": _make_installed_app()}, users=[alice, bob])
    people, picks = await svc.invite_sheet(app_id="chess", self_user_id="u-alice")
    assert [p["user_ref"] for p in people] == ["u-bob"] and picks == []
