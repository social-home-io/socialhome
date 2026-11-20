"""§27.9 release blocker (v_91 to listed GFSs): an author uploads space
content for a member only to a GFS the space OWNER signed onto the space's
list, pinned to the key that list names.

Since this change an author household with no connection to a GFS still
uploads there when a member's card names a mailbox on it — so whoever could
choose that GFS could aim space traffic (mailbox ids, timing, sizes) at a
server of its choosing. The checks, against real GFS apps and the author's
real sending stack (``tests/federation/test_multi_listed_gfs_e2e.py``):

* a list naming an attacker's GFS that is not the owner's — signed by
  another household, or not signed at all — is never stored, so a member
  card naming that GFS gets NO request there (not even ``/gfs/info``); the
  member goes through the host hop;
* a member card naming any GFS the owner did not list gets nothing there;
* a server answering at a listed URL that does not hold the listed key (a
  hijacked name) proves nothing: it gets no upload, the host hop carries.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import aiohttp
import pytest

from socialhome.crypto import derive_instance_id, ed25519_public_key
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import FederationEventType
from socialhome.domain.gfs_server import GfsServer
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.gfs_server_list_sig import sign_gfs_server_list
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_service import apply_gfs_servers_from_metadata
from tests.federation.test_multi_listed_gfs_e2e import (
    CONTENT,
    _Author,
    _gfs,
    _mailboxes,
)
from tests.services.test_gfs_relay_inbound import _SPACE

try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """Re-enable sockets if the HA pytest plugin disabled them."""

except ImportError:  # pragma: no cover - CI path
    pass


pytestmark = pytest.mark.security

POST = FederationEventType.SPACE_POST_CREATED
OWNER_SEED = os.urandom(32)
OWNER = derive_instance_id(ed25519_public_key(OWNER_SEED))
ATTACKER_SEED = os.urandom(32)


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _signed(servers: tuple[GfsServer, ...], *, seed: bytes, issued_at: int) -> dict:
    return sign_gfs_server_list(
        space_id=_SPACE,
        servers=servers,
        issued_at=issued_at,
        owner_seed=seed,
    ).to_wire()


@pytest.fixture
async def world(tmp_dir):
    honest = await _gfs(tmp_dir, "gfs-honest")
    evil = await _gfs(tmp_dir, "gfs-evil")
    db = AsyncDatabase(tmp_dir / "author.db", batch_timeout_ms=10)
    await db.startup()
    spaces = SqliteSpaceRepo(db)
    await spaces.save(
        Space(
            id=_SPACE,
            name="Listed",
            owner_instance_id=OWNER,
            owner_username="o",
            identity_public_key=os.urandom(32).hex(),
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.OPEN,
        )
    )
    http = aiohttp.ClientSession()
    routes = {g.server().route for g in (honest, evil)}

    async def _member_servers(space_id: str):
        """The read side of the STORED signed list. The GFSs here listen on
        loopback, which production drops for a server we never joined (the
        SSRF guard, ``GfsServerSelectionService.member_servers``); they are
        passed as known routes so the list is read as it would be for public
        addresses."""
        space = await spaces.get(space_id)
        if space is None or space.gfs_servers is None:
            return None
        servers, _dropped = space.gfs_servers.servers(own_routes=routes)
        return servers

    yield honest, evil, spaces, http, _member_servers, routes
    await http.close()
    await db.shutdown()
    for g in (honest, evil):
        await g.tc.close()


async def _store(spaces, wire: dict) -> bool:
    return await apply_gfs_servers_from_metadata(
        spaces, _SPACE, meta={"gfs_servers": wire}, owner_instance_id=OWNER
    )


async def test_a_list_not_signed_by_the_owner_never_steers_an_upload(world):
    honest, evil, spaces, http, member_servers, _routes = world
    t0 = _now_ms()
    assert await _store(
        spaces, _signed((honest.server(),), seed=OWNER_SEED, issued_at=t0)
    )
    # Newer lists naming the attacker's GFS: one signed by another household,
    # one whose signature is the owner's over a different list (forged).
    by_attacker = _signed(
        (honest.server(), evil.server()), seed=ATTACKER_SEED, issued_at=t0 + 1
    )
    forged = _signed(
        (honest.server(), evil.server()), seed=OWNER_SEED, issued_at=t0 + 2
    )
    forged["gfs_servers_sig"] = _signed((), seed=OWNER_SEED, issued_at=t0 + 2)[
        "gfs_servers_sig"
    ]
    unsigned = {**forged, "gfs_servers_sig": ""}
    for wire in (by_attacker, forged, unsigned):
        assert not await _store(spaces, wire)

    author = _Author(http, [], member_servers=member_servers)
    on_honest = [await author.member(honest.url) for _ in range(2)]
    on_evil = [await author.member(evil.url) for _ in range(2)]
    await author.svc.broadcast_to_space_members(_SPACE, POST, dict(CONTENT))
    await author.drain()

    [upload] = honest.multi()
    assert _mailboxes(upload) == sorted(rx.mailbox.mailbox_id for rx in on_honest)
    assert evil.requests == []  # not an upload, not even a probe
    assert sorted(c[0] for c in author.host_hop.calls) == sorted(
        rx.own_id for rx in on_evil
    )


async def test_a_server_at_a_listed_url_without_the_listed_key_gets_nothing(world):
    """The owner listed the honest server's KEY at an address the attacker
    now answers on: the capability block and the proof of possession are
    checked against the listed key, so nothing is uploaded there."""
    honest, evil, spaces, http, member_servers, routes = world
    hijacked = GfsServer(
        gfs_instance_id=honest.gid,
        inbox_url=evil.url,
        public_key=honest.info["public_key"],
        gfs_key_suite="ed25519",
        display_name="hijacked",
    )
    routes.add(hijacked.route)
    assert await _store(
        spaces, _signed((hijacked,), seed=OWNER_SEED, issued_at=_now_ms())
    )
    author = _Author(http, [], member_servers=member_servers)
    members = [await author.member(evil.url) for _ in range(2)]
    await author.svc.broadcast_to_space_members(_SPACE, POST, dict(CONTENT))
    await author.drain()

    assert evil.uploads == []
    assert {p for _m, p in evil.requests} <= {"/gfs/info", "/gfs/proof"}
    assert sorted(c[0] for c in author.host_hop.calls) == sorted(
        rx.own_id for rx in members
    )
