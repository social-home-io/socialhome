"""The bell a delegated-admin household's admins get while it isn't on
every GFS of a space it stands in for (:mod:`socialhome.services.space_gfs_needed_service`)."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.crypto import (
    derive_instance_id,
    ed25519_public_key,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import SpaceAdminSeedStored, SpaceFeaturesApplied
from socialhome.domain.federation import GfsConnection
from socialhome.domain.gfs_server import GfsServer
from socialhome.domain.notification_kinds import NotifKind, notif_kind
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)
from socialhome.gfs_server_list_sig import sign_gfs_server_list
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.notification_repo import SqliteNotificationRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.gfs_connection_service import GfsConnectionService
from socialhome.services.gfs_server_selection_service import (
    GfsServerSelectionService,
)
from socialhome.services.notification_service import NotificationService
from socialhome.services.space_gfs_needed_service import (
    NOTIF_TYPE,
    SpaceGfsNeededService,
)
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService

GFS_A = "https://gfs-a.example"
GFS_B = "https://gfs-b.example"


class _Push:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def push_to_users(self, user_ids, payload):
        for uid in user_ids:
            self.calls.append((uid, payload))

    async def push_to_user(self, user_id, payload):
        self.calls.append((user_id, payload))


class _World:
    """What the service reads: whether we stand in for the host, and which
    listed GFSs we're connected to."""

    def __init__(self) -> None:
        self.stands_in = True
        self.servers: dict[str, tuple[str, bool]] = {}

    async def stands_in_for_host(self, space) -> bool:
        return self.stands_in

    async def describe(self, space):
        await asyncio.sleep(0)  # a real listing awaits the database
        if not self.servers:
            return None, False
        return [
            {"display_name": name, "inbox_url": url, "connected": connected}
            for url, (name, connected) in self.servers.items()
        ], False


@pytest.fixture
async def stack(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0c" * 32))
    notif_repo = SqliteNotificationRepo(db, max_per_user=50)
    notif_svc = NotificationService(notif_repo, user_repo, space_repo, bus)
    push = _Push()
    notif_svc.attach_push_service(push)
    notif_svc.wire()
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    space_svc = SpaceService(
        space_repo, SqliteSpacePostRepo(db), user_repo, bus, own_instance_id="iid"
    )
    world = _World()

    def make_service() -> SpaceGfsNeededService:
        svc = SpaceGfsNeededService(
            space_repo=space_repo,
            notifications=notif_svc,
            bus=bus,
            stands_in=world.stands_in_for_host,
            describe=world.describe,
        )
        svc.wire()
        return svc

    class S:
        pass

    s = S()
    s.db, s.bus, s.push, s.world = db, bus, push, world
    s.notif_repo, s.space_repo, s.space_svc = notif_repo, space_repo, space_svc
    s.user_svc = user_svc
    s.notif_svc = notif_svc
    s.make_service = make_service
    s.svc = make_service()
    yield s
    await db.shutdown()


async def _setup(stack):
    """A household admin (Anna, in the space) and a plain member (Ben)."""
    anna = await stack.user_svc.provision(
        username="anna", display_name="Anna", is_admin=True
    )
    ben = await stack.user_svc.provision(username="ben", display_name="Ben")
    space = await stack.space_svc.create_space(owner_username="anna", name="Lakeside")
    await stack.space_svc.add_member(
        space.id, actor_username="anna", user_id=ben.user_id
    )
    return anna, ben, space


async def _rows(stack, user_id: str):
    return [
        n
        for n in await stack.notif_repo.list(user_id, limit=50)
        if n.type == NOTIF_TYPE
    ]


async def _pending(stack, user_id: str):
    return [n for n in await _rows(stack, user_id) if not n.params.handled]


def test_the_bell_files_under_needs_you():
    # Muting it to "In app" keeps that choice; it can't be switched off,
    # like every request only the household's admins can unblock.
    assert notif_kind(NOTIF_TYPE) is NotifKind.NEEDS_YOU


async def test_admin_seat_grant_with_a_missing_gfs_bells_household_admins(stack):
    anna, ben, space = await _setup(stack)
    stack.world.servers = {GFS_A: ("Alpha GFS", True), GFS_B: ("Beta GFS", False)}

    await stack.bus.publish(SpaceAdminSeedStored(space_id=space.id))

    rows = await _rows(stack, anna.user_id)
    assert len(rows) == 1
    row = rows[0]
    assert "Beta GFS" in row.title and "Lakeside" in row.title
    assert "Alpha GFS" not in row.title
    # Tapping it opens the space, where the notice's Connect button is.
    assert row.link_url == f"/spaces/{space.id}"
    assert row.params.gfs == (GFS_B,)
    assert row.space_id == space.id
    # Ben is no household admin: connecting is not his to do.
    assert await _rows(stack, ben.user_id) == []
    # One push, title only: it names the space, never a GFS.
    assert [uid for uid, _ in stack.push.calls] == [anna.user_id]
    payload = stack.push.calls[0][1]
    assert "Beta GFS" not in str(payload)


async def test_no_bell_when_connected_to_every_gfs(stack):
    anna, _, space = await _setup(stack)
    stack.world.servers = {GFS_A: ("Alpha GFS", True), GFS_B: ("Beta GFS", True)}

    await stack.bus.publish(SpaceAdminSeedStored(space_id=space.id))
    await stack.svc.check_all()

    assert await _rows(stack, anna.user_id) == []
    assert stack.push.calls == []


async def test_no_bell_for_a_household_without_the_admin_seat(stack):
    anna, _, space = await _setup(stack)
    stack.world.stands_in = False
    stack.world.servers = {GFS_A: ("Alpha GFS", False)}

    await stack.bus.publish(SpaceFeaturesApplied(space_id=space.id))
    await stack.svc.check_all()

    assert await _rows(stack, anna.user_id) == []


async def test_a_new_list_adding_a_gfs_bells_again(stack):
    anna, _, space = await _setup(stack)
    stack.world.servers = {GFS_A: ("Alpha GFS", False)}
    await stack.bus.publish(SpaceAdminSeedStored(space_id=space.id))
    # Anna reads it; the owner then adds a second GFS we're not on.
    row = (await _rows(stack, anna.user_id))[0]
    await stack.notif_repo.mark_read(row.id, anna.user_id)
    stack.world.servers[GFS_B] = ("Beta GFS", False)

    await stack.bus.publish(SpaceFeaturesApplied(space_id=space.id))

    rows = await _pending(stack, anna.user_id)
    assert len(rows) == 1  # still one bell per space
    assert rows[0].params.gfs == (GFS_A, GFS_B)
    assert "Alpha GFS" in rows[0].title and "Beta GFS" in rows[0].title
    assert rows[0].read_at is None
    assert len(stack.push.calls) == 2


async def test_an_unrelated_config_change_does_not_bell_again(stack):
    anna, _, space = await _setup(stack)
    stack.world.servers = {GFS_A: ("Alpha GFS", False)}
    await stack.bus.publish(SpaceAdminSeedStored(space_id=space.id))
    row = (await _rows(stack, anna.user_id))[0]
    await stack.notif_repo.mark_read(row.id, anna.user_id)

    await stack.bus.publish(SpaceFeaturesApplied(space_id=space.id))
    await stack.bus.publish(SpaceAdminSeedStored(space_id=space.id))

    rows = await _rows(stack, anna.user_id)
    assert len(rows) == 1
    assert rows[0].read_at is not None  # not re-opened
    assert len(stack.push.calls) == 1


async def test_startup_bells_once_and_not_again_after_a_restart(stack):
    anna, _, _ = await _setup(stack)
    stack.world.servers = {GFS_A: ("Alpha GFS", False)}

    await stack.svc.check_all()
    assert len(await _pending(stack, anna.user_id)) == 1
    # A restart: a fresh service, the same database.
    await stack.make_service().check_all()
    await stack.make_service().check_all()

    assert len(await _rows(stack, anna.user_id)) == 1
    assert len(stack.push.calls) == 1


async def test_connecting_resolves_the_bell_and_it_is_not_raised_again(stack):
    anna, _, _ = await _setup(stack)
    stack.world.servers = {GFS_A: ("Alpha GFS", False)}
    await stack.svc.check_all()

    stack.world.servers[GFS_A] = ("Alpha GFS", True)
    await stack.svc.on_connection_added(object())

    rows = await _rows(stack, anna.user_id)
    assert len(rows) == 1
    assert rows[0].params.handled is not None
    assert rows[0].read_at is not None
    await stack.make_service().check_all()
    assert await _pending(stack, anna.user_id) == []
    assert len(stack.push.calls) == 1


async def test_a_gfs_missing_again_later_bells_again(stack):
    anna, _, _ = await _setup(stack)
    stack.world.servers = {GFS_A: ("Alpha GFS", False)}
    await stack.svc.check_all()
    stack.world.servers[GFS_A] = ("Alpha GFS", True)
    await stack.svc.check_all()
    assert await _pending(stack, anna.user_id) == []

    stack.world.servers[GFS_A] = ("Alpha GFS", False)  # disconnected since
    await stack.make_service().check_all()

    assert len(await _pending(stack, anna.user_id)) == 1
    assert len(stack.push.calls) == 2


async def test_one_of_two_connected_keeps_the_bell_for_the_other(stack):
    anna, _, _ = await _setup(stack)
    stack.world.servers = {GFS_A: ("Alpha GFS", False), GFS_B: ("Beta GFS", False)}
    await stack.svc.check_all()

    stack.world.servers[GFS_A] = ("Alpha GFS", True)
    await stack.svc.check_all()

    rows = await _pending(stack, anna.user_id)
    assert len(rows) == 1
    assert rows[0].params.gfs == (GFS_B,)
    assert "Alpha GFS" not in rows[0].title and "Beta GFS" in rows[0].title
    # Anna reads it; Alpha goes missing again: told of again.
    await stack.notif_repo.mark_read(rows[0].id, anna.user_id)
    stack.world.servers[GFS_A] = ("Alpha GFS", False)
    await stack.svc.check_all()
    assert (await _pending(stack, anna.user_id))[0].params.gfs == (GFS_A, GFS_B)
    assert len(stack.push.calls) == 2


async def test_gfs_leaving_the_list_resolves_the_bell(stack):
    anna, _, space = await _setup(stack)
    stack.world.servers = {GFS_A: ("Alpha GFS", False)}
    await stack.svc.check_all()

    stack.world.servers = {}
    await stack.bus.publish(SpaceFeaturesApplied(space_id=space.id))

    assert await _pending(stack, anna.user_id) == []


async def test_losing_the_admin_seat_resolves_the_bell(stack):
    anna, _, space = await _setup(stack)
    stack.world.servers = {GFS_A: ("Alpha GFS", False)}
    await stack.svc.check_all()

    stack.world.stands_in = False
    await stack.bus.publish(SpaceFeaturesApplied(space_id=space.id))

    assert await _pending(stack, anna.user_id) == []


async def test_a_household_admin_outside_the_space_is_not_told(stack):
    """Which spaces another person here is in is theirs: an admin who isn't
    a member never learns the space's name from a bell."""
    anna, _, _ = await _setup(stack)
    carl = await stack.user_svc.provision(
        username="carl", display_name="Carl", is_admin=True
    )
    stack.world.servers = {GFS_A: ("Alpha GFS", False)}

    await stack.svc.check_all()

    assert len(await _rows(stack, anna.user_id)) == 1
    assert await _rows(stack, carl.user_id) == []


async def test_an_admin_who_stepped_down_has_their_bell_resolved(stack):
    anna, _, _ = await _setup(stack)
    stack.world.servers = {GFS_A: ("Alpha GFS", False)}
    await stack.svc.check_all()

    await stack.user_svc.set_admin("anna", False)
    await stack.svc.check_all()

    assert await _pending(stack, anna.user_id) == []


async def test_concurrent_checks_of_one_space_bell_once(stack):
    """Startup's sweep racing an inbound event for the same space: one
    row, one push."""
    anna, _, space = await _setup(stack)
    stack.world.servers = {GFS_A: ("Alpha GFS", False)}

    await asyncio.gather(
        stack.svc.check_all(),
        stack.svc.check(space.id),
        stack.svc.check(space.id),
    )

    assert len(await _rows(stack, anna.user_id)) == 1
    assert len(stack.push.calls) == 1


async def test_an_unknown_space_is_ignored(stack):
    await stack.bus.publish(SpaceAdminSeedStored(space_id="nope"))
    assert stack.push.calls == []


# ── Wired to the real services ──────────────────────────────────────────

_OWNER_SEED = b"\x31" * 32


def _server(gid: str) -> GfsServer:
    return GfsServer(
        gfs_instance_id=gid,
        inbox_url=f"https://{gid}.example",
        public_key=(gid.encode().hex() * 32)[:64],
        gfs_key_suite="ed25519",
        display_name=gid.upper(),
    )


def _conn(gid: str) -> GfsConnection:
    s = _server(gid)
    return GfsConnection(
        id=f"c-{gid}",
        gfs_instance_id=s.gfs_instance_id,
        display_name=s.display_name,
        public_key=s.public_key,
        inbox_url=s.inbox_url,
        status="active",
        paired_at="2026-11-20T00:00:00+00:00",
    )


async def test_real_wiring_bells_a_delegated_admin_and_resolves_on_connect(stack):
    """With the real stand-in check (a remote host, delegation on, the seed
    held) and the real GFS listing (the owner's signed list against our
    connections), as ``app._build_space_gfs_needed`` wires them."""
    anna = await stack.user_svc.provision(
        username="anna", display_name="Anna", is_admin=True
    )
    owner_id = derive_instance_id(ed25519_public_key(_OWNER_SEED))
    seed = b"\x42" * 32
    await stack.space_repo.save(
        Space(
            id="sp-real",
            name="Harbour",
            owner_instance_id=owner_id,
            owner_username="olga",
            identity_public_key=ed25519_public_key(seed).hex(),
            config_sequence=0,
            features=SpaceFeatures(delegated_admin_authority=True),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    await stack.space_repo.save_member(
        SpaceMember(
            space_id="sp-real",
            user_id=anna.user_id,
            role=SpaceRole.ADMIN,
            joined_at="2026-11-20T00:00:00+00:00",
        )
    )
    await stack.space_repo.set_gfs_servers(
        "sp-real",
        sign_gfs_server_list(
            space_id="sp-real",
            servers=(_server("north"), _server("south")),
            issued_at=10,
            owner_seed=_OWNER_SEED,
        ),
    )
    gfs_repo = SqliteGfsConnectionRepo(stack.db)
    await gfs_repo.save(_conn("north"))
    gfs = GfsConnectionService(gfs_repo)
    selection = GfsServerSelectionService(
        space_repo=stack.space_repo,
        gfs_repo=gfs_repo,
        bus=stack.bus,
        user_repo=SqliteUserRepo(stack.db),
        gfs_connection_service=gfs,
    )
    space_svc = SpaceService(
        stack.space_repo,
        SqliteSpacePostRepo(stack.db),
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="own-i",
    )
    # Its own bus: the fixture's stubbed service listens on ``stack.bus``.
    bus = EventBus()
    needed = SpaceGfsNeededService(
        space_repo=stack.space_repo,
        notifications=stack.notif_svc,
        bus=bus,
        stands_in=space_svc.stands_in_for_host,
        describe=selection.describe,
    )
    needed.wire()

    # Not a seat yet: no seed, no bell.
    await needed.check_all()
    assert [
        n for n in await _rows(stack, anna.user_id) if n.space_id == "sp-real"
    ] == []

    await stack.space_repo.set_space_seed("sp-real", seed)
    await bus.publish(SpaceAdminSeedStored(space_id="sp-real"))
    rows = [n for n in await _rows(stack, anna.user_id) if n.space_id == "sp-real"]
    assert len(rows) == 1
    assert "SOUTH" in rows[0].title and "NORTH" not in rows[0].title
    assert rows[0].link_url == "/spaces/sp-real"
    assert rows[0].params.gfs == ("https://south.example",)

    await gfs_repo.save(_conn("south"))
    await needed.on_connection_added(_conn("south"))
    assert [
        n for n in await _pending(stack, anna.user_id) if n.space_id == "sp-real"
    ] == []
