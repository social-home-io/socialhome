"""Release-blocker protocol tests: a space is listed only on the GFSs its
OWNER chose, and a public space never touches a GFS.

Owner model (2026-10-10): ``public`` is discoverable only by the households we
are connected with; ``global`` is listed on GFSs the owner picks. Before it,
a global space was published to EVERY active GFS, withdrawing a space
contacted every active GFS (telling servers that never listed it the space's
id), and a directory-setting change re-published to every active GFS. These
tests drive the real :class:`SpaceService`, :class:`GfsConnectionService`
and :class:`GfsServerSelectionService` (real SQLite, wired as ``app.py``
wires them) and record every HTTP request the household makes:

* a public space never contacts any GFS — create, settings edits, tier flips
  to and from private, the reconnect heal, the authority-rotation refresh;
* a global space contacts only the servers its owner chose — on create, on a
  settings edit, on leaving the global tier and on dissolve;
* only the space's owner changes the server set;
* the startup reconcile is idempotent and contacts no GFS.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.errors import TierChangeRequiredError
from socialhome.domain.federation import GfsConnection
from socialhome.domain.space import (
    JoinMode,
    SpaceFeatures,
    SpaceMember,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.gfs_connection_service import GfsConnectionService
from socialhome.services.gfs_server_selection_service import (
    GfsServerSelectionService,
)
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService

pytestmark = pytest.mark.security


class _Resp:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._body


class _RecordingSession:
    """Every request the household makes, by host."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    def _record(self, method: str, url: str) -> _Resp:
        self.requests.append((method, url))
        return _Resp(200, {"status": "active"})

    def post(self, url, **kw):
        return self._record("POST", url)

    def get(self, url, **kw):
        return self._record("GET", url)

    def delete(self, url, **kw):
        return self._record("DELETE", url)

    def hosts(self) -> set[str]:
        return {urlsplit(url).hostname or "" for _m, url in self.requests}


@pytest.fixture
async def household(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "owner-choice.db", batch_timeout_ms=10)
    await db.startup()
    try:
        await db.enqueue(
            "INSERT INTO instance_identity(instance_id, identity_private_key,"
            " identity_public_key, routing_secret) VALUES(?,?,?,?)",
            (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
        )
        bus = EventBus()
        users = SqliteUserRepo(db)
        spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0c" * 32))
        gfs_repo = SqliteGfsConnectionRepo(db)
        user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)
        await user_svc.provision(username="alice", display_name="Alice")
        bob = await user_svc.provision(username="bob", display_name="Bob")
        session = _RecordingSession()
        gfs = GfsConnectionService(
            gfs_repo, http_client=session, publish_client=session
        )
        gfs.attach_publish_context(
            space_repo=spaces, own_instance_id=iid, own_signing_key=kp.private_key
        )
        # Wired exactly as ``app._build_gfs_server_selection`` does.
        selection = GfsServerSelectionService(
            space_repo=spaces,
            gfs_repo=gfs_repo,
            bus=bus,
            user_repo=users,
            gfs_connection_service=gfs,
        )
        gfs.attach_on_listings_changed(selection.sync_snapshot)
        selection.wire()
        selection.attach_identity(own_instance_id=iid, own_signing_key=kp.private_key)
        svc = SpaceService(
            spaces, SqliteSpacePostRepo(db), users, bus, own_instance_id=iid
        )
        svc.attach_gfs_connection_service(gfs)
        svc.attach_gfs_server_selection(selection)
        for gfs_id in ("chosen", "other"):
            await gfs_repo.save(
                GfsConnection(
                    id=gfs_id,
                    gfs_instance_id=f"inst-{gfs_id}",
                    display_name=gfs_id,
                    public_key=(gfs_id.encode().hex() * 32)[:64],
                    inbox_url=f"https://{gfs_id}.example",
                    status="active",
                    paired_at="2026-10-01T00:00:00+00:00",
                )
            )

        class H:
            pass

        h = H()
        h.svc, h.gfs, h.selection, h.session = svc, gfs, selection, session
        h.spaces, h.gfs_repo, h.bob = spaces, gfs_repo, bob
        yield h
    finally:
        await db.shutdown()


async def test_a_public_space_never_contacts_any_gfs(household):
    h = household
    space = await h.svc.create_space(
        owner_username="alice",
        name="Club",
        space_type=SpaceType.PUBLIC,
        join_mode=JoinMode.OPEN,
    )
    # Directory settings, tier flips to private and back.
    await h.svc.update_config(
        space.id,
        actor_username="alice",
        join_mode=JoinMode.INVITE_ONLY,
        features=SpaceFeatures(allow_subscribers=True),
    )
    await h.svc.update_config(
        space.id, actor_username="alice", space_type=SpaceType.PRIVATE
    )
    await h.svc.update_config(
        space.id, actor_username="alice", space_type=SpaceType.PUBLIC
    )
    # Publishing it by hand is a tier change, refused before any request.
    with pytest.raises(TierChangeRequiredError):
        await h.selection.add_server(space.id, actor_username="alice", gfs_id="chosen")
    with pytest.raises(TierChangeRequiredError):
        await h.gfs.publish_space(space.id, "chosen")
    # The reconnect heal and the rotation refresh have nothing to re-list.
    assert await h.gfs.heal_space_pins("chosen") == 0
    assert await h.gfs.republish_space(space.id) == 0
    await h.svc.dissolve_space(space.id, actor_username="alice")
    assert h.session.requests == []


async def test_a_stale_public_listing_is_never_re_advertised(household):
    """A listing row left from before public stopped meaning "may be on a
    GFS": neither the heal nor a settings edit re-publishes it."""
    h = household
    space = await h.svc.create_space(
        owner_username="alice", name="Club", space_type=SpaceType.PUBLIC
    )
    await h.gfs_repo.publish_space(space.id, "chosen")
    assert await h.gfs.heal_space_pins("chosen") == 0
    await h.svc.update_config(
        space.id, actor_username="alice", join_mode=JoinMode.INVITE_ONLY
    )
    assert h.session.requests == []


async def test_a_global_space_contacts_only_the_chosen_gfs(household):
    h = household
    space = await h.svc.create_space(
        owner_username="alice",
        name="Chess",
        space_type=SpaceType.GLOBAL,
        gfs_ids=["chosen"],
    )
    await h.svc.update_config(
        space.id,
        actor_username="alice",
        join_mode=JoinMode.INVITE_ONLY,
        features=SpaceFeatures(allow_subscribers=True),
    )
    await h.svc.update_config(
        space.id, actor_username="alice", space_type=SpaceType.PUBLIC
    )
    assert h.session.requests, "the chosen GFS was contacted"
    assert h.session.hosts() == {"chosen.example"}
    paths = [urlsplit(url).path for _m, url in h.session.requests]
    assert paths == [
        f"/gfs/spaces/{space.id}/publish",  # create
        f"/gfs/spaces/{space.id}/publish",  # directory settings refresh
        f"/gfs/spaces/{space.id}/unpublish",  # leaving global
    ]
    assert await h.gfs_repo.list_publications_for_space(space.id) == []


async def test_dissolving_contacts_only_the_listing_gfs(household):
    h = household
    space = await h.svc.create_space(
        owner_username="alice",
        name="Chess",
        space_type=SpaceType.GLOBAL,
        gfs_ids=["chosen"],
    )
    h.session.requests.clear()
    await h.svc.dissolve_space(space.id, actor_username="alice")
    assert h.session.hosts() == {"chosen.example"}


async def test_global_not_listed_contacts_no_gfs(household):
    """A move into global with no servers (a remote admin's approved
    proposal) is listed nowhere until the owner picks."""
    h = household
    space = await h.svc.create_space(
        owner_username="alice", name="Club", space_type=SpaceType.HOUSEHOLD
    )
    await h.svc.update_config(
        space.id, actor_username="alice", space_type=SpaceType.GLOBAL
    )
    assert h.session.requests == []
    described = await h.selection.describe(await h.spaces.get(space.id))
    assert described == ([], True)


async def test_only_the_owner_changes_the_server_set(household):
    h = household
    space = await h.svc.create_space(
        owner_username="alice",
        name="Chess",
        space_type=SpaceType.GLOBAL,
        gfs_ids=["chosen"],
    )
    await h.spaces.save_member(
        SpaceMember(
            space_id=space.id,
            user_id=h.bob.user_id,
            role=SpaceRole.ADMIN,
            joined_at="2026-10-01T00:00:00+00:00",
        )
    )
    h.session.requests.clear()
    with pytest.raises(SpacePermissionError):
        await h.selection.set_servers(
            space.id, actor_username="bob", gfs_ids=["chosen", "other"]
        )
    with pytest.raises(SpacePermissionError):
        await h.selection.add_server(space.id, actor_username="bob", gfs_id="other")
    with pytest.raises(SpacePermissionError):
        await h.selection.remove_server(space.id, actor_username="bob", gfs_id="chosen")
    assert h.session.requests == []
    # The owner may: the added server is published, nothing else contacted.
    outcome = await h.selection.set_servers(
        space.id, actor_username="alice", gfs_ids=["other"]
    )
    assert outcome.listed == ("other",)
    assert [
        (urlsplit(u).hostname, urlsplit(u).path) for _m, u in h.session.requests
    ] == [
        ("other.example", f"/gfs/spaces/{space.id}/publish"),
        ("chosen.example", f"/gfs/spaces/{space.id}/unpublish"),
    ]


async def test_startup_reconcile_is_idempotent_and_contacts_no_gfs(household):
    h = household
    public = await h.svc.create_space(
        owner_username="alice", name="Club", space_type=SpaceType.PUBLIC
    )
    await h.gfs_repo.publish_space(public.id, "chosen")
    assert await h.selection.reconcile_on_startup() == 1
    promoted = await h.spaces.get(public.id)
    assert promoted.space_type is SpaceType.GLOBAL
    assert promoted.gfs_servers is not None
    signed_at = promoted.gfs_servers.issued_at
    sequence = promoted.config_sequence
    assert await h.selection.reconcile_on_startup() == 0
    again = await h.spaces.get(public.id)
    assert again.gfs_servers.issued_at == signed_at
    assert again.config_sequence == sequence
    assert h.session.requests == []
