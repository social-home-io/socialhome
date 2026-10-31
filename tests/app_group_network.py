"""A few households on one event loop, for group-table tests (v_76).

Each :class:`Household` has its own fully-migrated SQLite database, the
real repos, the real :class:`AppFederationService` and
:class:`AppGroupService`, and a fake federation layer: ``send_event``
puts the (already sealed, in production) ``APP_SESSION`` payload on a
shared queue that :meth:`Network.flush` delivers to the addressed
household's app bridge as a :class:`FederationEvent` — what the §24.11
pipeline hands the bridge after verifying the sender. Nothing touches
the network; pairing is seeded rows.

Not a test module itself (no ``test_`` prefix): imported by
``tests/services/test_app_group_service.py`` and
``tests/protocol/test_app_group_federation.py``.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from socialhome.crypto import generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.apps import AppCatalogEntry, AppManifest, InstalledApp
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.federation_capabilities import OURS
from socialhome.domain.user import User
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.app_group_repo import SqliteAppGroupRepo
from socialhome.repositories.app_repo import SqliteAppRepo
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.repositories.notification_repo import SqliteNotificationRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.app_federation_service import AppFederationService
from socialhome.services.app_group_service import AppGroupService
from socialhome.services.notification_service import NotificationService

APP = "quiz"


def quiz_app(
    *, players: tuple[int, int] = (3, 8), late_join: bool = False, tables: bool = True
) -> InstalledApp:
    return InstalledApp(
        app_id=APP,
        name="Quiz",
        version="1.0.0",
        enabled=True,
        manifest=AppManifest(
            entry="index.html",
            icon=None,
            capabilities=("storage", "federation"),
            players_min=players[0],
            players_max=players[1],
            tables=tables,
            late_join=late_join,
        ),
        bundle_path="apps/quiz/1.0.0",
        bundle_sha256="ab" * 32,
        source_url="https://example/quiz.tgz",
        installed_by=None,
        installed_at="2026-10-01T00:00:00+00:00",
    )


class FakeWs:
    def __init__(self) -> None:
        self.frames: list[tuple[str, dict]] = []

    async def broadcast_to_user(self, user_id: str, payload: dict) -> int:
        self.frames.append((user_id, payload))
        return 1

    async def broadcast_to_users(self, user_ids: list[str], payload: dict) -> int:
        for uid in user_ids:
            self.frames.append((uid, payload))
        return len(user_ids)

    def connected_users(self) -> set[str]:
        return set()


class FakeBus:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event) -> None:
        self.events.append(event)

    def of(self, kind: type) -> list:
        return [e for e in self.events if isinstance(e, kind)]


class RecordingBus(EventBus):
    """A real bus (the notification service listens) that also keeps
    every event, like :class:`FakeBus`."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list = []

    async def publish(self, event) -> None:
        self.events.append(event)
        await super().publish(event)

    def of(self, kind: type) -> list:
        return [e for e in self.events if isinstance(e, kind)]


class FakeCatalog:
    """This household's app catalog, listing the quiz (D10: an invite for
    an app the household doesn't have is named from it)."""

    async def browse_catalog_for(self, *, user_id: str, is_admin: bool) -> list:
        return [
            AppCatalogEntry(
                app_id=APP,
                name="Quiz",
                latest_version="1.0.0",
                description="A quiz",
                icon_url=None,
                capabilities=("storage", "federation"),
                bundle_url="https://example/quiz.tgz",
                bundle_sha256="ab" * 32,
            )
        ]


class FakeChildProtection:
    """The §CP reads the services use: who is protected, whom a guardian
    blocked, which households a protected account may message."""

    def __init__(self) -> None:
        self.protected: set[str] = set()
        self.direct_peers: set[str] = set()
        self.blocks: set[frozenset[str]] = set()

    def register_gate(self, _svc) -> None:
        return None

    async def is_protected(self, user_id: str) -> bool:
        return user_id in self.protected

    async def is_guardian_blocked(self, a: str, b: str) -> bool:
        return frozenset({a, b}) in self.blocks

    async def guardian_blocks_household(self, _user_id, _instance_id) -> bool:
        return False

    async def guardian_block_counterparts(self, user_id: str) -> frozenset[str]:
        return frozenset(
            o for pair in self.blocks if user_id in pair for o in pair - {user_id}
        )

    async def is_dm_allowed(self, *, sender_user_id: str, target_instance_id) -> bool:
        if sender_user_id not in self.protected or not target_instance_id:
            return True
        return target_instance_id in self.direct_peers


class FakeFederation:
    def __init__(self, net: Network, own: str) -> None:
        self.net = net
        self.own_instance_id = own
        self.versions: dict[str, int] = {}
        kp = generate_identity_keypair()
        self.own_identity_seed = kp.private_key
        self.own_identity_pk = kp.public_key

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        return self.versions.get(instance_id, 0) >= min_version

    async def send_event(self, *, to_instance_id, event_type, payload, **_kw):
        assert event_type in (
            FederationEventType.APP_SESSION,
            FederationEventType.APP_MESSAGE,
        )
        self.net.sent.append((self.own_instance_id, to_instance_id, payload))
        self.net.queue.append((self.own_instance_id, to_instance_id, payload))
        self.net.types[id(payload)] = event_type

    async def send_app_message(self, **_kw) -> None:
        raise AssertionError("no 1:1 messages in group tests")


@dataclass
class Household:
    instance: str
    db: AsyncDatabase
    fed: FakeFederation
    ws: FakeWs
    bus: FakeBus
    cp: FakeChildProtection
    app_repo: SqliteAppRepo
    group_repo: SqliteAppGroupRepo
    user_repo: SqliteUserRepo
    app_fed: AppFederationService
    groups: AppGroupService
    users: dict[str, User] = field(default_factory=dict)
    #: The notification rows, when the household keeps them
    #: (``notifications=True``: waiting invites, D10).
    notifs: SqliteNotificationRepo | None = None

    async def add_user(self, username: str, name: str | None = None) -> User:
        user = User(
            user_id=f"u-{self.instance}-{username}",
            username=username,
            display_name=name or username.title(),
        )
        await self.user_repo.save(user)
        self.users[username] = user
        return user

    def uid(self, username: str) -> str:
        return self.users[username].user_id

    def target(self, other: Household, username: str) -> dict:
        """The invite sheet's entry for *username* of *other* as seen here."""
        if other is self:
            return {
                "instance_id": self.instance,
                "user_ref": self.uid(username),
                "is_local": True,
            }
        return {"instance_id": other.instance, "user_ref": username, "is_local": False}


class Network:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.homes: dict[str, Household] = {}
        self.queue: list[tuple[str, str, dict]] = []
        self.sent: list[tuple[str, str, dict]] = []
        #: The event type each sent payload went as (by ``id``).
        self.types: dict[int, FederationEventType] = {}

    async def household(
        self, instance: str, *, version: int = OURS, notifications: bool = False
    ) -> Household:
        """A household; with *notifications* its bus is a real one and the
        notification service keeps rows — what a game invite waiting for
        its app (D10) is."""
        path = self.root / instance
        path.mkdir(parents=True, exist_ok=True)
        db = AsyncDatabase(path / "h.db", batch_timeout_ms=5)
        await db.startup()
        fed = FakeFederation(self, instance)
        ws, cp = FakeWs(), FakeChildProtection()
        bus: FakeBus | RecordingBus = RecordingBus() if notifications else FakeBus()
        app_repo, group_repo = SqliteAppRepo(db), SqliteAppGroupRepo(db)
        user_repo = SqliteUserRepo(db)
        app_fed = AppFederationService(
            app_repo=app_repo,
            user_repo=user_repo,
            ws=ws,  # type: ignore[arg-type]
            federation=fed,
            federation_repo=SqliteFederationRepo(db),
            bus=bus,  # type: ignore[arg-type]
        )
        groups = AppGroupService(
            group_repo=group_repo,
            federation_repo=SqliteFederationRepo(db),
            key_manager=KeyManager(os.urandom(32)),
            coalesce_every=0.0,
            app_repo=app_repo,
            user_repo=user_repo,
            app_federation=app_fed,
            federation=fed,
            ws=ws,  # type: ignore[arg-type]
            bus=bus,  # type: ignore[arg-type]
            roster_every=0.0,
        )
        app_fed.attach_groups(groups)
        app_fed._child_protection = cp  # type: ignore[assignment]
        groups._child_protection = cp  # type: ignore[assignment]
        notifs = None
        if notifications:
            notifs = SqliteNotificationRepo(db)
            svc = NotificationService(
                notifs,
                user_repo,
                SqliteSpaceRepo(db, key_manager=KeyManager(os.urandom(32))),
                bus,  # type: ignore[arg-type]
            )
            svc.attach_app_repo(app_repo)
            svc.wire()
            app_fed._notifs = notifs
            app_fed._catalog = FakeCatalog()  # type: ignore[assignment]
        home = Household(
            instance=instance,
            db=db,
            fed=fed,
            ws=ws,
            bus=bus,
            cp=cp,
            app_repo=app_repo,
            group_repo=group_repo,
            user_repo=user_repo,
            app_fed=app_fed,
            groups=groups,
            notifs=notifs,
        )
        home.fed.versions[instance] = version
        self.homes[instance] = home
        return home

    async def pair(self, a: Household, b: Household) -> None:
        """A confirmed, manual pairing; each knows the other's people."""
        for me, other in ((a, b), (b, a)):
            await me.db.enqueue(
                "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
                " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
                " local_inbox_id, status, source, proto_version)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    other.instance,
                    f"Home {other.instance}",
                    other.fed.own_identity_pk.hex(),
                    "k1",
                    "k2",
                    f"https://{other.instance}/wh",
                    f"wh-{other.instance}-{uuid4().hex[:6]}",
                    "confirmed",
                    "manual",
                    other.fed.versions[other.instance],
                ),
            )
            me.fed.versions[other.instance] = other.fed.versions[other.instance]
            me.cp.direct_peers.add(other.instance)
            for user in other.users.values():
                await me.db.enqueue(
                    "INSERT INTO remote_users(user_id, instance_id, remote_username,"
                    " display_name) VALUES(?,?,?,?)",
                    (
                        f"r-{other.instance}-{user.username}",
                        other.instance,
                        user.username,
                        user.display_name,
                    ),
                )

    async def flush(self, rounds: int = 50) -> None:
        """Deliver everything queued — and what that sends — until quiet."""
        for _ in range(rounds):
            tasks = [t for h in self.homes.values() for t in h.groups._tasks]
            if tasks:
                # A roster due now runs; one coalesced for later keeps waiting.
                await asyncio.wait(tasks, timeout=0.05)
            if not self.queue:
                await asyncio.sleep(0)
                if not self.queue:
                    return
                continue
            sender, to, payload = self.queue.pop(0)
            target = self.homes.get(to)
            if target is None:
                continue
            await target.app_fed.on_inbound_event(
                FederationEvent(
                    msg_id=uuid4().hex,
                    event_type=self.types.get(
                        id(payload), FederationEventType.APP_SESSION
                    ),
                    from_instance=sender,
                    to_instance=to,
                    timestamp="2026-10-31T12:00:00+00:00",
                    payload=dict(payload),
                )
            )
        raise AssertionError("the network never went quiet")

    def forge(
        self,
        sender: str,
        to: str,
        payload: dict,
        event_type: FederationEventType = FederationEventType.APP_SESSION,
    ) -> None:
        """Queue *payload* as if *sender* (the signature-proven household)
        had sent it to *to*."""
        self.queue.append((sender, to, payload))
        self.types[id(payload)] = event_type
        self.sent.append((sender, to, payload))

    def verbs(self, *, to: str | None = None) -> list[str]:
        return [
            p.get("verb", "message") for _s, t, p in self.sent if to is None or t == to
        ]

    def messages(self, *, to: str | None = None) -> list[dict]:
        """The ``APP_MESSAGE`` payloads sent (to *to*)."""
        return [
            p
            for _s, t, p in self.sent
            if self.types.get(id(p)) is FederationEventType.APP_MESSAGE
            and (to is None or t == to)
        ]

    async def close(self) -> None:
        for home in self.homes.values():
            await home.groups.stop()
            await home.db.shutdown()


__all__ = [
    "APP",
    "FakeBus",
    "FakeCatalog",
    "Household",
    "Network",
    "RecordingBus",
    "quiz_app",
]
