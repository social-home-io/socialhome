"""Tests for socialhome.services.gfs_space_mirror_service."""

from __future__ import annotations

import json

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gfs_connection_service import GfsConnectionError
from socialhome.domain.public_space import PublicSpaceListing
from socialhome.repositories.public_space_repo import SqlitePublicSpaceRepo
from socialhome.services.gfs_http import MAX_GFS_BODY_BYTES
from socialhome.services.gfs_space_mirror_service import (
    _MIRROR_FETCH_TIMEOUT_S,
    GfsSpaceMirrorService,
)


PIN_A = "aa" * 32
PIN_B = "bb" * 32


# ─── Stubs ───────────────────────────────────────────────────────────────


class _Content:
    """Minimal stand-in for ``aiohttp``'s streaming body reader."""

    __slots__ = ("_raw",)

    def __init__(self, raw: bytes):
        self._raw = raw

    async def read(self, n: int = -1) -> bytes:
        return self._raw if n < 0 else self._raw[:n]


class _StubResp:
    __slots__ = ("status", "_body", "content", "content_length")

    def __init__(
        self,
        status: int,
        body: dict | None = None,
        *,
        raw: bytes | None = None,
        content_length: int | None = None,
    ):
        self.status = status
        self._body = body or {}
        payload = json.dumps(self._body).encode() if raw is None else raw
        self.content = _Content(payload)
        self.content_length = len(payload) if content_length is None else content_length

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
        entry = self.responses.get(url, (404, {}))
        status, body = entry[0], entry[1]
        raw = entry[2] if len(entry) > 2 else None
        return _StubResp(status, body if isinstance(body, dict) else {}, raw=raw)


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


def _mirror(
    env,
    session,
    gfs: _StubGfs | None = None,
    *,
    public_space_repo=None,
) -> GfsSpaceMirrorService:
    svc = GfsSpaceMirrorService(
        space_repo=env.spaces,
        gfs_connection_repo=env.conns,
        gfs_connection_service=gfs or _StubGfs(),
        public_space_repo=public_space_repo,
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


# ─── space-id validation (URL-injection guard) ───────────────────────────


@pytest.mark.parametrize(
    "unsafe",
    [
        "../../admin/api/clients",  # yarl normalises the prefix away entirely
        "../..",
        "a/b",  # a bare slash adds a path segment
        "..",
        ".",
        "",
        "sp 1",
        "sp?x=1",  # would graft a query string onto the GFS request
        "x" * 129,
    ],
)
async def test_ensure_mirror_refuses_unsafe_space_id(env, unsafe):
    """A crafted space id must never reach the GFS URL.

    ``space_id`` comes from ``POST /api/spaces/{space_id}/subscribe`` and
    aiohttp percent-DECODES the path before filling ``match_info`` — so
    ``..%2F..%2Fadmin`` arrives as the literal ``../../admin``. Interpolated
    into ``{inbox_url}/gfs/spaces/{space_id}``, yarl normalises the
    ``/gfs/spaces/`` prefix away and the metadata fetch becomes a GET against
    an arbitrary path on the paired GFS, driveable by any authenticated local
    user. Fail closed before the URL is ever built.
    """
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    session = _StubSession()

    assert await _mirror(env, session).ensure_mirror(unsafe) is None
    assert session.calls == []  # no request was issued at all


# ─── hostile-body coercion (FIX 4) ───────────────────────────────────────


@pytest.mark.parametrize("hostile", [[1, 2, 3], {"a": 1}, 42])
@pytest.mark.parametrize("field", ["name", "description", "about_markdown"])
async def test_ensure_mirror_coerces_non_string_text_fields(env, hostile, field):
    """A GFS answering ``{"about_markdown": [1, 2, 3]}`` must not reach
    SQLite: the driver raises ``ProgrammingError``, which escapes
    ``ensure_mirror`` → ``subscribe_to_space`` unmapped → HTTP 500.
    """
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    session = _StubSession(
        {
            "https://gfs.test/gfs/spaces/sp-1": (
                200,
                _gfs_space_body(**{field: hostile}),
            ),
        },
    )

    got = await _mirror(env, session).ensure_mirror("sp-1")

    assert got is not None
    stored = await env.spaces.get("sp-1")
    assert stored is not None
    assert isinstance(getattr(stored, field), str)


@pytest.mark.parametrize("hostile", [[1, 2, 3], {"a": 1}, 42])
@pytest.mark.parametrize("field", ["owning_instance", "status", "identity_public_key"])
async def test_ensure_mirror_refuses_non_string_identity_fields(env, hostile, field):
    """Identifier-shaped fields are validated, never stringified — a
    ``["x"]`` owning instance is a malformed listing, not a host id."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    session = _StubSession(
        {
            "https://gfs.test/gfs/spaces/sp-1": (
                200,
                _gfs_space_body(**{field: hostile}),
            ),
        },
    )

    assert await _mirror(env, session).ensure_mirror("sp-1") is None
    assert await env.spaces.get("sp-1") is None


async def test_ensure_mirror_carries_the_real_join_mode(env):
    """The stub reflects what the GFS directory says, instead of the
    hardcoded ``invite_only`` that used to stand in for a field that never
    arrived — a household could not tell an open space from a closed one."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    session = _StubSession(
        {
            "https://gfs.test/gfs/spaces/sp-1": (
                200,
                _gfs_space_body(join_mode="open"),
            ),
        },
    )
    assert await _mirror(env, session).ensure_mirror("sp-1") is not None
    stored = await env.spaces.get("sp-1")
    assert stored is not None
    assert stored.join_mode is JoinMode.OPEN


@pytest.mark.parametrize("hostile", [None, "wide-open", 7, [1], {"a": 1}])
async def test_ensure_mirror_join_mode_fails_closed(env, hostile):
    """A missing (older GFS) or hostile join mode reads as invite-only —
    never as something more permissive."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    body = _gfs_space_body()
    if hostile is not None:
        body["join_mode"] = hostile
    session = _StubSession({"https://gfs.test/gfs/spaces/sp-1": (200, body)})
    assert await _mirror(env, session).ensure_mirror("sp-1") is not None
    stored = await env.spaces.get("sp-1")
    assert stored is not None
    assert stored.join_mode is JoinMode.INVITE_ONLY


@pytest.mark.parametrize("hostile", [[1, 2, 3], {"a": 1}, "nonsense"])
@pytest.mark.parametrize("field", ["min_age", "category"])
async def test_ensure_mirror_normalises_hostile_enum_fields(env, hostile, field):
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    session = _StubSession(
        {
            "https://gfs.test/gfs/spaces/sp-1": (
                200,
                _gfs_space_body(**{field: hostile}),
            ),
        },
    )

    assert await _mirror(env, session).ensure_mirror("sp-1") is not None
    stored = await env.spaces.get("sp-1")
    assert stored is not None
    assert stored.min_age in {0, 13, 16, 18}
    assert isinstance(stored.category, str)


# ─── response-size bound (FIX 5) ─────────────────────────────────────────


async def test_ensure_mirror_refuses_an_oversized_body(env):
    """aiohttp caps nothing by default — a hostile GFS returning a
    multi-gigabyte body would exhaust household memory. Fail closed."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    giant = b'{"pad": "' + b"x" * (MAX_GFS_BODY_BYTES + 10) + b'"}'
    session = _StubSession(
        {"https://gfs.test/gfs/spaces/sp-1": (200, {}, giant)},
    )

    assert await _mirror(env, session).ensure_mirror("sp-1") is None
    assert await env.spaces.get("sp-1") is None


async def test_ensure_mirror_refuses_an_unparsable_body(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    session = _StubSession(
        {"https://gfs.test/gfs/spaces/sp-1": (200, {}, b"<html>nope</html>")},
    )

    assert await _mirror(env, session).ensure_mirror("sp-1") is None


async def test_mirror_fetch_timeout_is_short(env):
    """``ensure_mirror`` walks every paired GFS serially, and any local user
    can drive it against an arbitrary unknown id — so the per-connection
    budget bounds the held request slot."""
    assert _MIRROR_FETCH_TIMEOUT_S <= 5.0


# ─── was_gfs_listed (mirror-provenance evidence, FIX 1) ──────────────────


async def test_was_gfs_listed_is_false_without_a_directory_repo(env):
    """No evidence available is not evidence of a mirror — fail safe."""
    assert await _mirror(env, _StubSession()).was_gfs_listed("sp-1") is False


async def test_was_gfs_listed_follows_the_public_space_cache(env):
    repo = SqlitePublicSpaceRepo(env.db)
    await repo.upsert(
        PublicSpaceListing(
            space_id="sp-1",
            instance_id="remote-host",
            name="Cool Space",
            description=None,
            emoji=None,
            lat=None,
            lon=None,
            radius_km=None,
            member_count=3,
        )
    )
    svc = _mirror(env, _StubSession(), public_space_repo=repo)

    assert await svc.was_gfs_listed("sp-1") is True
    assert await svc.was_gfs_listed("sp-other") is False
