"""Tests for socialhome.services.gfs_space_mirror_service."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gfs_connection_service import GfsConnectionError
from socialhome.services.gfs_space_mirror_service import GfsSpaceMirrorService


PIN_A = "aa" * 32
PIN_B = "bb" * 32


# ─── Stubs ───────────────────────────────────────────────────────────────


class _StubResp:
    __slots__ = ("status", "_body")

    def __init__(self, status: int, body: dict | None = None):
        self.status = status
        self._body = body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return ""


class _StubSession:
    """Per-URL GET responses; anything unmapped 404s."""

    __slots__ = ("responses", "calls", "raise_for")

    def __init__(
        self,
        responses: dict[str, tuple[int, dict]] | None = None,
        *,
        raise_for: str | None = None,
    ):
        self.responses = responses or {}
        self.calls: list[str] = []
        self.raise_for = raise_for

    def get(self, url, **kw):
        self.calls.append(url)
        if self.raise_for is not None and self.raise_for in url:
            raise OSError("boom")
        status, body = self.responses.get(url, (404, {}))
        return _StubResp(status, body)


class _StubGfs:
    """Records subscribe / unsubscribe calls against the GFS service seam."""

    __slots__ = ("subscribes", "unsubscribes", "raise_on_unsubscribe")

    def __init__(self, *, raise_on_unsubscribe: bool = False):
        self.subscribes: list[tuple[str, str]] = []
        self.unsubscribes: list[tuple[str, str]] = []
        self.raise_on_unsubscribe = raise_on_unsubscribe

    async def subscribe_to_gfs_space(self, space_id: str, gfs_id: str) -> str:
        self.subscribes.append((space_id, gfs_id))
        return "subscribed"

    async def unsubscribe_from_gfs_space(self, space_id: str, gfs_id: str) -> str:
        self.unsubscribes.append((space_id, gfs_id))
        if self.raise_on_unsubscribe:
            raise GfsConnectionError("GFS down")
        return "unsubscribed"


def _conn(gfs_id: str, *, inbox_url: str, status: str = "active") -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"inst-{gfs_id}",
        display_name=f"GFS {gfs_id}",
        public_key="pk",
        inbox_url=inbox_url,
        status=status,
        paired_at="2025-01-01T00:00:00+00:00",
    )


def _gfs_space_body(**over) -> dict:
    body = {
        "space_id": "sp-1",
        "owning_instance": "remote-host",
        "name": "Cool Space",
        "description": "a public space",
        "about_markdown": "# hi",
        "category": "gaming",
        "min_age": 13,
        "status": "active",
        "identity_public_key": PIN_A,
        "withdrawn": False,
    }
    body.update(over)
    return body


# ─── Fixture ─────────────────────────────────────────────────────────────


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x09" * 32))
    conn_repo = SqliteGfsConnectionRepo(db)

    class Env:
        pass

    e = Env()
    e.db = db
    e.spaces = space_repo
    e.conns = conn_repo
    e.iid = iid
    yield e
    await db.shutdown()


def _mirror(env, session, gfs: _StubGfs | None = None) -> GfsSpaceMirrorService:
    svc = GfsSpaceMirrorService(
        space_repo=env.spaces,
        gfs_connection_repo=env.conns,
        gfs_connection_service=gfs or _StubGfs(),
    )
    svc.attach_session(session)
    return svc


# ─── ensure_mirror ───────────────────────────────────────────────────────


async def test_ensure_mirror_seats_stub_row(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    session = _StubSession(
        {"https://gfs.test/gfs/spaces/sp-1": (200, _gfs_space_body())},
    )
    got = await _mirror(env, session).ensure_mirror("sp-1")

    assert got is not None
    space, gfs_id = got
    assert gfs_id == "gfs-1"
    assert space.identity_public_key == PIN_A

    stored = await env.spaces.get("sp-1")
    assert stored is not None
    assert stored.name == "Cool Space"
    assert stored.description == "a public space"
    assert stored.category == "gaming"
    assert stored.min_age == 13
    assert stored.space_type == SpaceType.GLOBAL
    assert stored.owner_instance_id == "remote-host"
    assert stored.identity_public_key == PIN_A


async def test_ensure_mirror_refuses_empty_identity_public_key(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    session = _StubSession(
        {
            "https://gfs.test/gfs/spaces/sp-1": (
                200,
                _gfs_space_body(identity_public_key=""),
            ),
        },
    )
    gfs = _StubGfs()
    assert await _mirror(env, session, gfs).ensure_mirror("sp-1") is None
    assert await env.spaces.get("sp-1") is None
    assert gfs.subscribes == []


@pytest.mark.parametrize("bad", ["zz" * 32, "aa" * 16, "aa" * 33, "abc"])
async def test_ensure_mirror_refuses_malformed_pin(env, bad):
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    session = _StubSession(
        {
            "https://gfs.test/gfs/spaces/sp-1": (
                200,
                _gfs_space_body(identity_public_key=bad),
            ),
        },
    )
    assert await _mirror(env, session).ensure_mirror("sp-1") is None
    assert await env.spaces.get("sp-1") is None


async def test_ensure_mirror_skips_non_active_listing(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    session = _StubSession(
        {
            "https://gfs.test/gfs/spaces/sp-1": (
                200,
                _gfs_space_body(status="pending"),
            ),
        },
    )
    assert await _mirror(env, session).ensure_mirror("sp-1") is None
    assert await env.spaces.get("sp-1") is None


async def test_ensure_mirror_falls_through_to_second_connection(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await env.conns.save(_conn("gfs-2", inbox_url="https://b.test"))
    session = _StubSession(
        {
            "https://a.test/gfs/spaces/sp-1": (404, {}),
            "https://b.test/gfs/spaces/sp-1": (200, _gfs_space_body()),
        },
    )
    got = await _mirror(env, session).ensure_mirror("sp-1")
    assert got is not None
    assert got[1] == "gfs-2"
    assert session.calls == [
        "https://a.test/gfs/spaces/sp-1",
        "https://b.test/gfs/spaces/sp-1",
    ]


async def test_ensure_mirror_survives_transport_error(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await env.conns.save(_conn("gfs-2", inbox_url="https://b.test"))
    session = _StubSession(
        {"https://b.test/gfs/spaces/sp-1": (200, _gfs_space_body())},
        raise_for="a.test",
    )
    got = await _mirror(env, session).ensure_mirror("sp-1")
    assert got is not None
    assert got[1] == "gfs-2"


async def test_ensure_mirror_refuses_to_clobber_other_host_row(env):
    """``can_seat_remote_stub`` guard: a row already held under a different
    host is never overwritten by a GFS-served mirror."""
    await env.spaces.save(
        Space(
            id="sp-1",
            name="Mine",
            owner_instance_id="some-other-host",
            owner_username="anna",
            identity_public_key=PIN_B,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    session = _StubSession(
        {"https://gfs.test/gfs/spaces/sp-1": (200, _gfs_space_body())},
    )
    assert await _mirror(env, session).ensure_mirror("sp-1") is None
    stored = await env.spaces.get("sp-1")
    assert stored is not None
    assert stored.name == "Mine"
    assert stored.identity_public_key == PIN_B


async def test_re_mirror_updates_name_but_never_moves_the_pin(env):
    """TOFU regression: a later GFS answer may refresh metadata but the
    pinned space-authority key is immutable after first seating."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    url = "https://gfs.test/gfs/spaces/sp-1"
    mirror = _mirror(env, _StubSession({url: (200, _gfs_space_body())}))
    assert await mirror.ensure_mirror("sp-1") is not None

    mirror2 = _mirror(
        env,
        _StubSession(
            {url: (200, _gfs_space_body(name="Renamed", identity_public_key=PIN_B))},
        ),
    )
    assert await mirror2.ensure_mirror("sp-1") is not None

    stored = await env.spaces.get("sp-1")
    assert stored is not None
    assert stored.name == "Renamed"
    assert stored.identity_public_key == PIN_A


async def test_ensure_mirror_without_connections_returns_none(env):
    assert await _mirror(env, _StubSession()).ensure_mirror("sp-1") is None


# ─── subscribe / unsubscribe seams ───────────────────────────────────────


async def test_subscribe_to_gfs_delegates(env):
    gfs = _StubGfs()
    await _mirror(env, _StubSession(), gfs).subscribe_to_gfs("sp-1", "gfs-1")
    assert gfs.subscribes == [("sp-1", "gfs-1")]


async def test_unsubscribe_hits_every_active_connection(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await env.conns.save(_conn("gfs-2", inbox_url="https://b.test"))
    gfs = _StubGfs()
    await _mirror(env, _StubSession(), gfs).unsubscribe("sp-1")
    assert gfs.unsubscribes == [("sp-1", "gfs-1"), ("sp-1", "gfs-2")]


async def test_unsubscribe_swallows_gfs_errors(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    gfs = _StubGfs(raise_on_unsubscribe=True)
    # Must not propagate — a down GFS never blocks a local unsubscribe.
    await _mirror(env, _StubSession(), gfs).unsubscribe("sp-1")
    assert gfs.unsubscribes == [("sp-1", "gfs-1")]
