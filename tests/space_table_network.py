"""A few households sharing a space, for space-table tests (v_77).

Builds on :mod:`tests.app_group_network`: each household has its own SQLite
database, the real repos, :class:`AppGroupService` and the real
:class:`SpaceTableService`. Instance ids are derived from each household's
identity key (as in production), so seat keys check against the key a
household id IS the fingerprint of. The fake federation layer adds
``broadcast_to_space_members`` — it delivers only to member households of
the space the network holds (``only_instances`` narrows it, never adds),
skips a household the sender doesn't know at ``min_proto_version``, and
records what went where (event type, space) so tests can assert that no
follower-only, non-member or GFS household ever hears of a table.

Not a test module itself (no ``test_`` prefix).
"""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.federation_capabilities import OURS
from socialhome.domain.space import (
    CONTENT_AUTHORITY_ROLES,
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_app_table_service import SpaceTableService
from tests.app_group_network import APP, Household, Network, quiz_app

SPACE = "sp-lakeside"
_CONTENT = frozenset(r.value for r in CONTENT_AUTHORITY_ROLES)


@dataclass
class Result:
    attempted: int
    terminal_failures: list = field(default_factory=list)


class FakeAuthorship:
    """``moderates_as``: the named user holds a live content-authority seat
    on the sending household (this household's roster mirror)."""

    def __init__(self, seats: SqliteSpaceRemoteMemberRepo) -> None:
        self._seats = seats

    async def moderates_as(self, event, space_id: str, user_id: str) -> bool:
        seat = await self._seats.get(space_id, event.from_instance, user_id)
        return seat is not None and not seat.tombstoned and seat.role in _CONTENT


@dataclass(eq=False)
class SpaceHome:
    home: Household
    spaces: SqliteSpaceRepo
    seats: SqliteSpaceRemoteMemberRepo
    tables: SpaceTableService
    name: str
    roles: dict[str, str] = field(default_factory=dict)

    @property
    def instance(self) -> str:
        return self.home.instance

    def uid(self, username: str) -> str:
        return self.home.uid(username)


class SpaceNetwork(Network):
    """A :class:`Network` whose households share spaces."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.space_homes: dict[str, SpaceHome] = {}
        #: Which households the space delivers to (``space_instances``).
        self.members: dict[str, set[str]] = {}
        #: The space each queued payload went over (by ``id``).
        self.space_of: dict[int, str] = {}

    async def space_home(
        self, name: str, *, version: int = OURS, card_every: float = 0.0
    ) -> SpaceHome:
        kp = generate_identity_keypair()
        instance = derive_instance_id(kp.public_key)
        home = await self.household(instance, version=version)
        home.fed.own_identity_seed = kp.private_key
        home.fed.own_identity_pk = kp.public_key
        home.fed.broadcast_to_space_members = self._broadcaster(home)  # type: ignore[attr-defined]
        home.fed.space_member_supports = home.fed.peer_supports  # type: ignore[attr-defined]
        spaces = SqliteSpaceRepo(home.db)
        seats = SqliteSpaceRemoteMemberRepo(home.db)
        tables = SpaceTableService(
            groups=home.groups,
            group_repo=home.group_repo,
            app_repo=home.app_repo,
            app_federation=home.app_fed,
            space_repo=spaces,
            remote_member_repo=seats,
            user_repo=home.user_repo,
            federation=home.fed,
            authorship=FakeAuthorship(seats),  # type: ignore[arg-type]
            ws=home.ws,  # type: ignore[arg-type]
            bus=home.bus,  # type: ignore[arg-type]
            card_every=card_every,
        )
        home.groups.attach_space_tables(tables)
        sh = SpaceHome(home=home, spaces=spaces, seats=seats, tables=tables, name=name)
        self.space_homes[instance] = sh
        return sh

    def knows(self, a: SpaceHome, b: SpaceHome) -> None:
        """*a* knows *b*'s version (a pairing, or the mesh claim)."""
        a.home.fed.versions[b.instance] = b.home.fed.versions[b.instance]

    def _broadcaster(self, home: Household):
        async def broadcast_to_space_members(
            space_id,
            event_type,
            payload,
            *,
            only_instances=None,
            min_proto_version=None,
            **_kw,
        ):
            targets = sorted(self.members.get(space_id, set()) - {home.instance})
            if only_instances is not None:
                targets = [t for t in targets if t in only_instances]
            sent = 0
            for to in targets:
                if min_proto_version is not None and not await home.fed.peer_supports(
                    to, min_version=min_proto_version
                ):
                    continue
                copy = dict(payload)
                self.queue.append((home.instance, to, copy))
                self.sent.append((home.instance, to, copy))
                self.types[id(copy)] = event_type
                self.space_of[id(copy)] = space_id
                sent += 1
            return Result(attempted=sent)

        return broadcast_to_space_members

    async def make_space(
        self,
        host: SpaceHome,
        *,
        members: dict[SpaceHome, dict[str, str]],
        allowed: tuple[str, ...] = (APP,),
        space_type: SpaceType = SpaceType.PRIVATE,
        space_id: str = SPACE,
    ) -> None:
        """A space hosted by *host*; *members* maps each household to its
        people's roles. Every household holds the space row and the same
        roster mirror; the space delivers to every member household."""
        self.members[space_id] = {h.instance for h in members}
        for sh, own_roles in members.items():
            space = Space(
                id=space_id,
                name="Lakeside",
                owner_instance_id=host.instance,
                owner_username="owner",
                identity_public_key="aa" * 32,
                config_sequence=1,
                features=SpaceFeatures(allowed_apps=allowed),
                space_type=space_type,
                join_mode=JoinMode.INVITE_ONLY,
            )
            await sh.spaces.save(space)
            for other, roles in members.items():
                for username, role in roles.items():
                    if other is sh:
                        await sh.home.db.enqueue(
                            "INSERT INTO space_members(space_id, user_id, role)"
                            " VALUES(?, ?, ?)",
                            (space_id, other.uid(username), role),
                        )
                    else:
                        await sh.home.db.enqueue(
                            "INSERT INTO space_remote_members(space_id,"
                            " instance_id, user_id, display_name, role)"
                            " VALUES(?, ?, ?, ?, ?)",
                            (
                                space_id,
                                other.instance,
                                other.uid(username),
                                username.title(),
                                "member" if role == "owner" else role,
                            ),
                        )
            sh.roles.update(own_roles)

    async def install(self, *homes: SpaceHome, **kw) -> None:
        for sh in homes:
            await sh.home.app_repo.install(quiz_app(**kw))

    async def flush(self, rounds: int = 80) -> None:
        """Deliver everything queued — app events to the app bridge, space
        events to the space-table handlers — until quiet."""
        for _ in range(rounds):
            tasks = [t for h in self.homes.values() for t in h.groups._tasks]
            tasks += [t for sh in self.space_homes.values() for t in sh.tables._tasks]
            if tasks:
                await asyncio.wait(tasks, timeout=0.05)
            if not self.queue:
                await asyncio.sleep(0)
                if not self.queue:
                    return
                continue
            sender, to, payload = self.queue.pop(0)
            event_type = self.types.get(id(payload), FederationEventType.APP_SESSION)
            event = FederationEvent(
                msg_id=uuid4().hex,
                event_type=event_type,
                from_instance=sender,
                to_instance=to,
                timestamp="2026-10-31T12:00:00+00:00",
                payload=dict(payload),
                space_id=self.space_of.get(id(payload)),
            )
            sh = self.space_homes.get(to)
            if sh is None:
                continue
            if event_type is FederationEventType.SPACE_APP_TABLE:
                await sh.tables._on_card(event)
            elif event_type is FederationEventType.SPACE_APP_SESSION:
                await sh.tables._on_session(event)
            else:
                await sh.home.app_fed.on_inbound_event(event)
        raise AssertionError("the network never went quiet")

    def space_events(self, *, to: str | None = None) -> list[tuple[str, str, dict]]:
        """(sender, event type, payload) of every space event sent (to *to*)."""
        return [
            (s, self.types[id(p)].value, p)
            for s, t, p in self.sent
            if id(p) in self.space_of and (to is None or t == to)
        ]

    async def close(self) -> None:
        for sh in self.space_homes.values():
            await sh.tables.stop()
        await super().close()


def with_card(sh: SpaceHome, session_id: str):
    """The table *sh* keeps (``None`` when it knows none)."""
    return sh.home.group_repo.get(session_id)


__all__ = [
    "APP",
    "SPACE",
    "SpaceHome",
    "SpaceNetwork",
    "dataclasses",
    "quiz_app",
    "with_card",
]
