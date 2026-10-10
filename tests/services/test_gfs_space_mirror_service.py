"""Tests for socialhome.services.gfs_space_mirror_service."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from dataclasses import replace

import pytest

import socialhome.services.gfs_directory as directory_mod
import socialhome.services.gfs_space_mirror_service as mirror_mod

from socialhome.capabilities_sig import sign_capabilities
from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.gfs_space_seat_repo import SqliteGfsSpaceSeatRepo
from socialhome.domain.gfs_space_seat import GfsSpaceSeat
from socialhome.domain.events import (
    RemoteSpaceMemberBanned,
    RemoteSpaceMemberRemoved,
    SpaceConfigChanged,
    SpaceMemberLeft,
)
from socialhome.domain.space import SpaceConfigEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gfs_connection_service import (
    GfsConnectionError,
    GfsConnectionService,
)
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
        # Consumes, like aiohttp's StreamReader (the reader loops to EOF).
        size = len(self._raw) if n < 0 else n
        out, self._raw = self._raw[:size], self._raw[size:]
        return out


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
    """Per-URL GET responses; anything unmapped 404s.

    An unmapped whole directory (``{base}/gfs/spaces``) answers with the ids
    of every detail URL mapped under that base with a non-404 status — a GFS
    that serves a space's detail lists it. Map the directory explicitly to
    model a server whose directory says otherwise.
    """

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
        entry = self.responses.get(url) or self._derived_directory(url)
        status, body = entry[0], entry[1]
        raw = entry[2] if len(entry) > 2 else None
        return _StubResp(status, body if isinstance(body, dict) else {}, raw=raw)

    def _derived_directory(self, url: str) -> tuple:
        if not url.endswith("/gfs/spaces"):
            return (404, {})
        prefix = f"{url}/"
        listed = [
            {"space_id": u[len(prefix) :]}
            for u, entry in self.responses.items()
            if u.startswith(prefix) and entry[0] != 404
        ]
        return (200, {"spaces": listed})


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

    async def unsubscribe_via(self, conn, space_id: str) -> str:
        return await self.unsubscribe_from_gfs_space(space_id, conn.id)


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
    e.seats = SqliteGfsSpaceSeatRepo(db)
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
        seat_repo=env.seats,
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
    # gfs-1's directory doesn't list sp-1: no per-space probe goes there.
    assert session.calls == [
        "https://a.test/gfs/spaces",
        "https://b.test/gfs/spaces",
        "https://b.test/gfs/spaces/sp-1",
    ]


@pytest.mark.security
async def test_ensure_mirror_never_probes_a_gfs_whose_directory_omits_it(env):
    """The per-space detail GET tells the server which space this household
    cares about — it goes only to a GFS whose WHOLE directory lists it."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    session = _StubSession(
        {
            "https://a.test/gfs/spaces": _directory("sp-other"),
            # Would seat fine — but the directory says it isn't listed here.
            "https://a.test/gfs/spaces/sp-1": (200, _gfs_space_body()),
        }
    )
    assert await _mirror(env, session).ensure_mirror("sp-1") is None
    assert session.calls == ["https://a.test/gfs/spaces"]
    assert await env.spaces.get("sp-1") is None


async def test_ensure_mirror_unreadable_directory_sends_no_probe(env):
    """Fail closed: a directory we can't read proves no listing."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    session = _StubSession(
        {
            "https://a.test/gfs/spaces": (500, {}),
            "https://a.test/gfs/spaces/sp-1": (200, _gfs_space_body()),
        }
    )
    assert await _mirror(env, session).ensure_mirror("sp-1") is None
    assert session.calls == ["https://a.test/gfs/spaces"]


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


async def test_subscribe_to_gfs_takes_and_records_the_seat(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    gfs = _StubGfs()
    await _mirror(env, _StubSession(), gfs).subscribe_to_gfs("sp-1", "gfs-1")
    assert gfs.subscribes == [("sp-1", "gfs-1")]
    # Recorded under the SERVER's id, which survives a re-pair, bound to
    # the key and URL it was taken over.
    assert await env.seats.list_for_space("sp-1") == [
        GfsSpaceSeat(
            space_id="sp-1",
            gfs_instance_id="inst-gfs-1",
            gfs_connection_id="gfs-1",
            gfs_public_key="pk",
            gfs_inbox_url="https://a.test",
        )
    ]


async def test_subscribe_to_gfs_unknown_connection_raises(env):
    with pytest.raises(GfsConnectionError):
        await _mirror(env, _StubSession()).subscribe_to_gfs("sp-1", "gfs-x")


async def test_a_refused_subscribe_records_no_seat(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))

    class _Refusing(_StubGfs):
        async def subscribe_to_gfs_space(self, space_id, gfs_id):
            raise GfsConnectionError("GFS rejected subscribe (HTTP 403): no")

    with pytest.raises(GfsConnectionError):
        await _mirror(env, _StubSession(), _Refusing()).subscribe_to_gfs(
            "sp-1", "gfs-1"
        )
    assert await _seat_ids(env, "sp-1") == []


def _directory(*space_ids: str) -> tuple[int, dict]:
    """A whole ``GET /gfs/spaces`` directory body listing *space_ids*."""
    return (200, {"spaces": [{"space_id": sid} for sid in space_ids]})


async def _mirrored(env, space_id: str = "sp-1", *, gfs: str | None = "gfs-1"):
    """A follower stub seated from connection ``gfs`` (provenance + the
    recorded seat ``take_seat`` writes); ``None`` = a pre-v44 mirror with
    neither."""
    repo = await _seat_subscription(env, space_id)
    if gfs is not None:
        await env.spaces.set_mirror_provenance(space_id, gfs_id=gfs, rotation_seq=0)
        await _seat(env, space_id, gfs)
    return repo


@pytest.mark.security
async def test_unsubscribe_reaches_only_the_gfs_that_seated_the_mirror(env):
    """The unsubscribe is signed and identity-bound: sending it to a GFS that
    never seated our subscription tells that operator we follow the space."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await env.conns.save(_conn("gfs-2", inbox_url="https://b.test"))
    await _mirrored(env, gfs="gfs-2")
    gfs = _StubGfs()
    session = _StubSession()
    await _mirror(env, session, gfs).unsubscribe("sp-1")
    assert gfs.unsubscribes == [("sp-1", "gfs-2")]
    # Provenance is local — no GFS is even asked.
    assert session.calls == []


async def test_unsubscribe_skips_an_inactive_seating_gfs(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test", status="pending"))
    await env.conns.save(_conn("gfs-2", inbox_url="https://b.test"))
    await _mirrored(env, gfs="gfs-1")
    gfs = _StubGfs()
    await _mirror(env, _StubSession(), gfs).unsubscribe("sp-1")
    assert gfs.unsubscribes == []


@pytest.mark.security
async def test_unsubscribe_legacy_mirror_uses_the_whole_directory(env):
    """A pre-v44 mirror has no provenance: fall back to the GFSs whose WHOLE
    directory lists the space — never a space-specific probe."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await env.conns.save(_conn("gfs-2", inbox_url="https://b.test"))
    await _mirrored(env, gfs=None)
    session = _StubSession(
        {
            "https://a.test/gfs/spaces": _directory("sp-1", "sp-x"),
            "https://b.test/gfs/spaces": _directory("sp-x"),
        }
    )
    gfs = _StubGfs()
    svc = _mirror(env, session, gfs)
    await svc.unsubscribe("sp-1")
    # Directory reads never run on the unsubscribe request path (L1).
    assert session.calls == [] and gfs.unsubscribes == []
    await svc.wait_idle()
    assert gfs.unsubscribes == [("sp-1", "gfs-1")]
    assert session.calls == ["https://a.test/gfs/spaces", "https://b.test/gfs/spaces"]


@pytest.mark.parametrize(
    "entry",
    [
        (500, {}),
        (200, {"spaces": "nope"}),
        (200, {"spaces": [1, {"space_id": ["sp-1"]}, {}]}),
        (200, {}, b"not json"),
    ],
)
async def test_unsubscribe_legacy_unreadable_directory_contacts_nobody(env, entry):
    """Fail closed: a directory we can't read proves nothing."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _mirrored(env, gfs=None)
    session = _StubSession({"https://a.test/gfs/spaces": entry})
    gfs = _StubGfs()
    svc = _mirror(env, session, gfs)
    await svc.unsubscribe("sp-1")
    await svc.wait_idle()
    assert gfs.unsubscribes == []


async def test_unsubscribe_legacy_directory_transport_error_contacts_nobody(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _mirrored(env, gfs=None)
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(raise_for="a.test"), gfs)
    await svc.unsubscribe("sp-1")
    await svc.wait_idle()
    assert gfs.unsubscribes == []


async def test_unsubscribe_legacy_without_session_contacts_nobody(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _mirrored(env, gfs=None)
    gfs = _StubGfs()
    svc = GfsSpaceMirrorService(
        space_repo=env.spaces,
        gfs_connection_repo=env.conns,
        gfs_connection_service=gfs,
        seat_repo=env.seats,
    )
    await svc.unsubscribe("sp-1")
    assert gfs.unsubscribes == []


async def test_unsubscribe_swallows_gfs_errors(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _mirrored(env, gfs="gfs-1")
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


async def test_ensure_mirror_carries_allow_subscribers(env):
    """The stub carries the owner's readability opt-in off the directory
    body, in the ``features`` block where ``allow_subscribers`` lives on a
    Space. Without it ``SpaceService.subscribe_to_space`` would refuse every
    GFS-discovered subscription."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    session = _StubSession(
        {
            "https://gfs.test/gfs/spaces/sp-1": (
                200,
                # Invite-only AND readable — the broadcast shape.
                _gfs_space_body(join_mode="invite_only", allow_subscribers=True),
            ),
        },
    )
    assert await _mirror(env, session).ensure_mirror("sp-1") is not None
    stored = await env.spaces.get("sp-1")
    assert stored is not None
    assert stored.features.allow_subscribers is True
    assert stored.join_mode is JoinMode.INVITE_ONLY


@pytest.mark.parametrize("hostile", [None, "nope", 0, [], {}])
async def test_ensure_mirror_allow_subscribers_fails_closed(env, hostile):
    """A missing (older GFS) or falsy/hostile flag reads as not-readable —
    never as something more permissive."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    body = _gfs_space_body(join_mode="open")
    if hostile is not None:
        body["allow_subscribers"] = hostile
    session = _StubSession({"https://gfs.test/gfs/spaces/sp-1": (200, body)})
    assert await _mirror(env, session).ensure_mirror("sp-1") is not None
    stored = await env.spaces.get("sp-1")
    assert stored is not None
    assert stored.features.allow_subscribers is False


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


async def test_was_gfs_listed_takes_any_positive_evidence(env):
    """L4: provenance, a recorded seat, or a paired server's WHOLE
    directory (never truncated) prove a GFS listed the space — the
    truncated discovery cache is not the only source."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    session = _StubSession({"https://a.test/gfs/spaces": _directory("sp-dir")})
    svc = _mirror(env, session)
    await _seat_subscription(env, "sp-prov", listed=False)
    await env.spaces.set_mirror_provenance("sp-prov", gfs_id="gfs-1", rotation_seq=0)
    await _seat(env, "sp-seat", "gfs-1")
    for sid in ("sp-prov", "sp-seat"):
        assert await svc.was_gfs_listed(sid) is True
    # L1: the directory is cache-only here — never downloaded on the
    # unsubscribe request path …
    assert await svc.was_gfs_listed("sp-dir") is False
    assert session.calls == []
    # … but a cached copy counts.
    await svc._directories.lists(await env.conns.get("gfs-1"), "sp-dir")
    assert await svc.was_gfs_listed("sp-dir") is True
    assert await svc.was_gfs_listed("sp-none") is False


async def test_directory_evidence_skips_an_owner_the_admin_blocked(env):
    """L1: the directory poll drops listings of a blocked owner; so does
    the mirror's directory evidence (cached and downloaded)."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    session = _StubSession(
        {
            "https://a.test/gfs/spaces": (
                200,
                {
                    "spaces": [
                        {"space_id": "sp-bad", "owning_instance": "evil-host"},
                        {"space_id": "sp-ok", "owning_instance": "good-host"},
                    ]
                },
            )
        }
    )
    repo = SqlitePublicSpaceRepo(env.db)
    await repo.block_instance("evil-host", blocked_by="admin")
    svc = _mirror(env, session, public_space_repo=repo)
    conn = await env.conns.get("gfs-1")
    await svc._directories.lists(conn, "sp-ok")
    assert await svc.was_gfs_listed("sp-bad") is False
    assert await svc.was_gfs_listed("sp-ok") is True
    assert await svc._legacy_listed(conn, "sp-bad") is False
    assert await svc._legacy_listed(conn, "sp-ok") is True


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


# ─── resubscribe_all (F4b — seats restored on GFS reconnect) ─────────────


async def _seat_subscription(env, space_id: str, *, listed: bool = True):
    """A local ``role='subscriber'`` row on a GFS-mirrored global stub."""
    from socialhome.domain.space import SpaceMember, SpaceRole

    # A LOCAL user (``users`` row): only local seats keep a GFS seat wanted.
    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username, user_id, display_name)"
        " VALUES('local','u-local','Local')"
    )
    await env.spaces.save(
        Space(
            id=space_id,
            name="Mirrored",
            owner_instance_id="remote-host",
            owner_username="them",
            identity_public_key="",
            config_sequence=0,
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.OPEN,
            features=SpaceFeatures(allow_subscribers=True),
        )
    )
    await env.spaces.save_member(
        SpaceMember(
            space_id=space_id,
            user_id="u-local",
            role=SpaceRole.SUBSCRIBER,
            joined_at="2025-01-01T00:00:00+00:00",
        )
    )
    repo = SqlitePublicSpaceRepo(env.db)
    if listed:
        await repo.upsert(
            PublicSpaceListing(
                space_id=space_id,
                instance_id="remote-host",
                name="Mirrored",
                description=None,
                emoji=None,
                lat=None,
                lon=None,
                radius_km=None,
                member_count=1,
            )
        )
    return repo


async def test_resubscribe_all_restores_a_purged_seat(env):
    """F4b: the GFS seat is registered only on the FIRST-ever subscribe, so a
    seat the GFS purged (owner turned readability off, then back on) is never
    re-taken — the household shows "subscribed" forever and receives nothing.
    A (re)connect re-POSTs ``/gfs/subscribe`` for every local subscription."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    repo = await _mirrored(env, "sp-sub")
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs, public_space_repo=repo)

    assert await svc.resubscribe_all("gfs-1") == 1
    assert gfs.subscribes == [("sp-sub", "gfs-1")]


async def test_resubscribe_all_skips_spaces_no_gfs_ever_listed(env):
    """A public/global stub learned from a direct peer is nobody's mirror —
    re-registering it would disclose the relationship to a GFS operator who
    never knew about the space."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    repo = await _seat_subscription(env, "sp-peer", listed=False)
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs, public_space_repo=repo)

    assert await svc.resubscribe_all("gfs-1") == 0
    assert gfs.subscribes == []


async def test_resubscribe_all_swallows_a_refusal_per_space(env, caplog):
    """The owner may have turned readability off for good: the GFS answers 403
    and that is an expected outcome, not an incident — DEBUG, fail-soft, and
    the remaining spaces are still attempted."""

    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    repo = await _mirrored(env, "sp-403")
    await _mirrored(env, "sp-ok")

    class _Refusing(_StubGfs):
        async def subscribe_to_gfs_space(self, space_id: str, gfs_id: str) -> str:
            if space_id == "sp-403":
                raise GfsConnectionError(
                    "GFS rejected subscribe (HTTP 403): space is not publicly readable"
                )
            return await super().subscribe_to_gfs_space(space_id, gfs_id)

    gfs = _Refusing()
    svc = _mirror(env, _StubSession(), gfs, public_space_repo=repo)
    with caplog.at_level(logging.WARNING):
        assert await svc.resubscribe_all("gfs-1") == 1
    assert gfs.subscribes == [("sp-ok", "gfs-1")]
    assert caplog.text == ""


@pytest.mark.security
async def test_resubscribe_all_shuffles_writer_spaces_into_the_follower_batch(
    env, monkeypatch
):
    """v_50: the spaces this household WRITES in ride the same reconnect
    batch, merged with the followed ones and shuffled — the server sees one
    run of identical signed subscribes and can't separate writer seats by
    order or timing."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    repo = await _mirrored(env, "sp-follow")
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs, public_space_repo=repo)
    shuffled: list[list[str]] = []

    def _shuffle(self, items):
        shuffled.append(list(items))
        items.reverse()

    monkeypatch.setattr(mirror_mod.secrets.SystemRandom, "shuffle", _shuffle)
    assert await svc.resubscribe_all("gfs-1", also=["sp-write", "sp-follow"]) == 2
    # One batch, de-duplicated, shuffled as a whole.
    assert shuffled == [["sp-follow", "sp-write"]]
    assert gfs.subscribes == [("sp-write", "gfs-1"), ("sp-follow", "gfs-1")]


@pytest.mark.security
async def test_resubscribe_all_never_subscribes_on_a_gfs_that_did_not_seat_it(env):
    """A reconnect to GFS B must not re-POST a subscribe for a space mirrored
    from GFS A: the signed, identity-bound request would tell B's operator
    that this household follows a space B may never have listed."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await env.conns.save(_conn("gfs-2", inbox_url="https://b.test"))
    repo = await _mirrored(env, "sp-a", gfs="gfs-1")
    await _mirrored(env, "sp-b", gfs="gfs-2")
    gfs = _StubGfs()
    session = _StubSession()
    svc = _mirror(env, session, gfs, public_space_repo=repo)

    assert await svc.resubscribe_all("gfs-2") == 1
    assert gfs.subscribes == [("sp-b", "gfs-2")]
    assert session.calls == []


@pytest.mark.security
async def test_resubscribe_all_legacy_mirror_needs_the_directory_listing(env):
    """No provenance (pre-v44): re-subscribe only where the WHOLE directory
    lists the space — fetched once per reconnect, never per space."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    repo = await _mirrored(env, "sp-listed", gfs=None)
    await _mirrored(env, "sp-elsewhere", gfs=None)
    session = _StubSession({"https://a.test/gfs/spaces": _directory("sp-listed")})
    gfs = _StubGfs()
    svc = _mirror(env, session, gfs, public_space_repo=repo)

    assert await svc.resubscribe_all("gfs-1") == 1
    assert gfs.subscribes == [("sp-listed", "gfs-1")]
    assert session.calls == ["https://a.test/gfs/spaces"]


async def test_resubscribe_all_unknown_connection_subscribes_nothing(env):
    repo = await _mirrored(env, "sp-1", gfs="gfs-gone")
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs, public_space_repo=repo)
    assert await svc.resubscribe_all("gfs-gone") == 0
    assert gfs.subscribes == []


# ─── v_44: a follower heals its pin from the GFS that seated it ──────────


async def test_ensure_mirror_records_provenance_and_seq(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    body = _gfs_space_body(identity_public_key=PIN_B, authority_rotation_seq=3)
    mirror = _mirror(
        env, _StubSession({"https://gfs.test/gfs/spaces/sp-1": (200, body)})
    )
    await mirror.ensure_mirror("sp-1")
    assert await env.spaces.get_mirror_provenance("sp-1") == ("gfs-1", 3)
    # Never touches the owner-certified epoch.
    assert (await env.spaces.get("sp-1")).authority_key_epoch == 0


async def _follower(
    env,
    *,
    space_type=SpaceType.GLOBAL,
    role="subscriber",
    owner_id="remote-host",
    gfs="gfs-1",
    seq=0,
):
    from socialhome.domain.space import SpaceMember

    await env.spaces.save(
        Space(
            id="sp-1",
            name="Cool",
            owner_instance_id=owner_id,
            owner_username="",
            identity_public_key=PIN_A,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=space_type,
            join_mode=JoinMode.OPEN,
        )
    )
    if role is not None:
        await env.spaces.save_member(
            SpaceMember(space_id="sp-1", user_id="u1", role=role, joined_at="x")
        )
    if gfs is not None:
        await env.spaces.set_mirror_provenance("sp-1", gfs_id=gfs, rotation_seq=seq)
    await env.conns.save(_conn("gfs-1", inbox_url="https://gfs.test"))
    await env.conns.save(_conn("gfs-2", inbox_url="https://other.test"))


def _listing(pk: str, seq: int, url: str = "https://gfs.test") -> _StubSession:
    return _StubSession(
        {
            f"{url}/gfs/spaces/sp-1": (
                200,
                _gfs_space_body(identity_public_key=pk, authority_rotation_seq=seq),
            )
        }
    )


async def test_refresh_heals_a_follower_from_its_own_gfs(env):
    await _follower(env)
    session = _listing(PIN_B, 1)
    mirror = _mirror(env, session)
    mirror.attach_identity(own_instance_id=env.iid)
    assert await mirror.refresh_authority_pin("sp-1") is True
    assert (await env.spaces.get("sp-1")).identity_public_key == PIN_B
    assert await env.spaces.get_mirror_provenance("sp-1") == ("gfs-1", 1)
    # Rate-limited: a second failed-verify burst does not refetch.
    assert await mirror.refresh_authority_pin("sp-1") is False


@pytest.mark.parametrize(
    "setup,session_url",
    [
        ({"space_type": SpaceType.PRIVATE}, "https://gfs.test"),
        ({"role": "member"}, "https://gfs.test"),
        ({"role": None}, "https://gfs.test"),  # no local seat at all
        ({"gfs": None}, "https://gfs.test"),  # unknown provenance
        ({}, "https://other.test"),  # another GFS lists it
        ({"seq": 4}, "https://gfs.test"),  # not a higher seq
        ({"owner_id": "SELF"}, "https://gfs.test"),  # our own space
    ],
)
async def test_refresh_refuses_outside_the_follower_trust_model(
    env, setup, session_url
):
    if setup.get("owner_id") == "SELF":
        setup = {**setup, "owner_id": env.iid}
    await _follower(env, **setup)
    mirror = _mirror(env, _listing(PIN_B, 4, url=session_url))
    mirror.attach_identity(own_instance_id=env.iid)
    assert await mirror.refresh_authority_pin("sp-1") is False
    assert (await env.spaces.get("sp-1")).identity_public_key == PIN_A


@pytest.mark.parametrize(
    "body",
    [
        {"identity_public_key": "zz" * 32, "authority_rotation_seq": 5},
        {"identity_public_key": PIN_B, "authority_rotation_seq": "5"},
        {"identity_public_key": PIN_B, "authority_rotation_seq": 2**63},
        {"identity_public_key": PIN_B[:10], "authority_rotation_seq": 5},
    ],
)
async def test_refresh_refuses_a_malformed_listing(env, body):
    await _follower(env)
    session = _StubSession(
        {"https://gfs.test/gfs/spaces/sp-1": (200, _gfs_space_body(**body))}
    )
    mirror = _mirror(env, session)
    mirror.attach_identity(own_instance_id=env.iid)
    assert await mirror.refresh_authority_pin("sp-1") is False


async def test_refresh_unknown_space_is_false(env):
    mirror = _mirror(env, _StubSession())
    mirror.attach_identity(own_instance_id=env.iid)
    assert await mirror.refresh_authority_pin("sp-none") is False


async def test_refresh_pins_for_gfs_walks_listed_subscriptions(env):
    from unittest.mock import AsyncMock

    await _follower(env)

    class _Listed:
        async def get(self, space_id):
            return object() if space_id == "sp-1" else None

    mirror = _mirror(env, _listing(PIN_B, 2), public_space_repo=_Listed())
    mirror.attach_identity(own_instance_id=env.iid)
    env.spaces.list_subscribed_space_ids = AsyncMock(return_value=["sp-1", "sp-2"])
    assert await mirror.refresh_authority_pins("gfs-1") == 1
    assert (await env.spaces.get("sp-1")).identity_public_key == PIN_B
    assert await mirror.refresh_authority_pins("unknown-gfs") == 0


def test_refresh_bookkeeping_stays_bounded(env):
    from socialhome.services import gfs_space_mirror_service as mod

    mirror = _mirror(env, _StubSession())
    for i in range(mod._PIN_REFRESH_MAX_TRACKED + 10):
        mirror._remember_refresh(f"sp-{i}", 0.0)
    assert len(mirror._last_pin_refresh) <= mod._PIN_REFRESH_MAX_TRACKED


# ─── directory gate (L2 / L5) ────────────────────────────────────────────


async def test_a_just_published_space_is_followable_without_waiting_a_ttl(
    env, monkeypatch
):
    """L2: a cached directory lacking the id is re-read once it is a few
    seconds old — the subscribe doesn't 404 for a whole TTL."""
    clock = [1000.0]
    monkeypatch.setattr(directory_mod, "_now", lambda: clock[0])
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    session = _StubSession({"https://a.test/gfs/spaces": _directory("sp-x")})
    svc = _mirror(env, session)
    assert await svc.ensure_mirror("sp-1") is None
    session.responses["https://a.test/gfs/spaces"] = _directory("sp-x", "sp-1")
    session.responses["https://a.test/gfs/spaces/sp-1"] = (200, _gfs_space_body())
    assert await svc.ensure_mirror("sp-1") is None  # within the window
    clock[0] += directory_mod.MISS_REFRESH_S
    assert await svc.ensure_mirror("sp-1") is not None


async def test_ensure_mirror_reads_the_directories_concurrently(env):
    """L5: one slow server must not serialise the others' directory reads."""

    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await env.conns.save(_conn("gfs-2", inbox_url="https://b.test"))
    gate = asyncio.Event()

    class _GatedResp(_StubResp):
        async def __aenter__(self):
            await gate.wait()
            return self

    class _Gated(_StubSession):
        def get(self, url, **kw):
            resp = super().get(url, **kw)
            if not url.endswith("/gfs/spaces"):
                return resp
            gated = _GatedResp(resp.status, resp._body)
            return gated

    session = _Gated({"https://b.test/gfs/spaces/sp-1": (200, _gfs_space_body())})
    task = asyncio.create_task(_mirror(env, session).ensure_mirror("sp-1"))

    async def _both_in_flight():
        while len(session.calls) < 2:
            await asyncio.sleep(0.01)

    # Both directory reads are in flight before either answered.
    await asyncio.wait_for(_both_in_flight(), timeout=5)
    assert sorted(session.calls) == [
        "https://a.test/gfs/spaces",
        "https://b.test/gfs/spaces",
    ]
    gate.set()
    got = await task
    assert got is not None and got[1] == "gfs-2"


# ─── seats: re-pair, teardown, reconcile, reactive ───────────────────────


async def _seat(env, space_id: str, gfs: str, *, key=None, url=None) -> None:
    """Record the seat ``take_seat`` would have written for connection
    ``gfs`` (bound to its key + URL when the row exists)."""
    conn = await env.conns.get(gfs)
    await env.seats.record(
        GfsSpaceSeat(
            space_id=space_id,
            gfs_instance_id=f"inst-{gfs}",
            gfs_connection_id=gfs,
            gfs_public_key=key or (conn.public_key if conn else "pk"),
            gfs_inbox_url=url or (conn.inbox_url if conn else f"https://{gfs}.test"),
        )
    )


async def _seat_ids(env, space_id: str) -> list[str]:
    return [s.gfs_instance_id for s in await env.seats.list_for_space(space_id)]


async def _binding(env, space_id: str, gfs_instance_id: str):
    seat = await env.seats.get(space_id, gfs_instance_id)
    return None if seat is None else (seat.gfs_connection_id, seat.gfs_public_key)


async def _repair(
    env, old: str, new: str, url: str, *, key: str = "pk", instance: str | None = None
) -> None:
    """Disconnect + re-pair: a new local connection id for server
    ``inst-{old}`` (or *instance*), at *url* with *key*."""
    await env.conns.delete(old)
    await env.conns.save(
        GfsConnection(
            id=new,
            gfs_instance_id=instance or f"inst-{old}",
            display_name="G",
            public_key=key,
            inbox_url=url,
            status="active",
            paired_at="2025-02-01T00:00:00+00:00",
        )
    )


@pytest.mark.security
async def test_a_re_paired_server_keeps_its_seats(env):
    """H1: the seat is the server's, not the local connection row's — a
    re-pair through the REAL unpair (``GfsConnectionService.disconnect``,
    which sends nothing and keeps the seats) and a new pairing (new
    ``conn.id``, same id / key / address) re-takes it on reconnect and
    tears it down on leave, and no other server is contacted."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await env.conns.save(_conn("gfs-2", inbox_url="https://b.test"))
    repo = await _mirrored(env, "sp-1", gfs="gfs-1")
    unpair_session = _StubSession()
    await GfsConnectionService(env.conns, http_client=unpair_session).disconnect(
        "gfs-1"
    )
    assert unpair_session.calls == []
    assert await _seat_ids(env, "sp-1") == ["inst-gfs-1"]
    await env.conns.save(
        GfsConnection(
            id="gfs-1b",
            gfs_instance_id="inst-gfs-1",
            display_name="G",
            public_key="pk",
            inbox_url="https://A.test:443/",
            status="active",
            paired_at="2025-02-01T00:00:00+00:00",
        )
    )
    gfs = _StubGfs()
    session = _StubSession()
    svc = _mirror(env, session, gfs, public_space_repo=repo)

    assert await svc.resubscribe_all("gfs-1b") == 1
    assert await svc.resubscribe_all("gfs-2") == 0
    assert gfs.subscribes == [("sp-1", "gfs-1b")]

    await env.spaces.delete_member("sp-1", "u-local")
    await svc.unsubscribe("sp-1")
    assert gfs.unsubscribes == [("sp-1", "gfs-1b")]
    assert await _seat_ids(env, "sp-1") == []
    assert session.calls == []


@pytest.mark.security
@pytest.mark.parametrize(
    "impostor",
    [
        {"key": "copied-elsewhere-key"},  # claims the id, another key
        {"url": "https://impostor.test"},  # claims id + copied key, other URL
    ],
)
async def test_an_id_claiming_impostor_gets_no_seat(env, impostor):
    """M2: a connection claiming a seat's server id is that server only when
    its pinned key AND its URL match the seat's — otherwise nothing recorded
    there is re-subscribed, released or re-anchored on it."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    repo = await _mirrored(env, "sp-1", gfs="gfs-1")
    await env.spaces.delete_member("sp-1", "u-local")  # unwanted, too
    await _repair(
        env,
        "gfs-1",
        "evil",
        impostor.get("url", "https://a.test"),
        key=impostor.get("key", "pk"),
    )
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs, public_space_repo=repo)
    assert await svc.resubscribe_all("evil") == 0
    assert await svc.rebind_mirrors(await env.conns.get("evil")) == 0
    await svc.unsubscribe("sp-1")
    await svc.on_relay_frame({"space_id": "sp-1"}, gfs_id="evil")
    await svc.wait_idle()
    assert gfs.subscribes == gfs.unsubscribes == []
    assert await _binding(env, "sp-1", "inst-gfs-1") == ("gfs-1", "pk")
    assert await env.spaces.get_mirror_provenance("sp-1") == ("gfs-1", 0)


async def test_a_seat_on_a_disconnected_server_waits_for_its_return(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _mirrored(env, "sp-1", gfs="gfs-1")
    await env.conns.delete("gfs-1")
    await env.spaces.delete_member("sp-1", "u-local")
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs)
    await svc.unsubscribe("sp-1")
    assert gfs.unsubscribes == []
    assert await _seat_ids(env, "sp-1") == ["inst-gfs-1"]
    # Re-paired: the reconnect self-heal tears the unwanted seat down.
    await _repair(env, "gfs-1", "gfs-1b", "https://a.test")
    assert await svc.resubscribe_all("gfs-1b") == 0
    assert gfs.unsubscribes == [("sp-1", "gfs-1b")]
    assert gfs.subscribes == []
    assert await _seat_ids(env, "sp-1") == []


async def test_a_failed_teardown_keeps_the_seat(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _mirrored(env, "sp-1", gfs="gfs-1")
    await env.spaces.delete_member("sp-1", "u-local")
    gfs = _StubGfs(raise_on_unsubscribe=True)
    assert await _mirror(env, _StubSession(), gfs).release_seats("sp-1") == 0
    assert await _seat_ids(env, "sp-1") == ["inst-gfs-1"]


@pytest.mark.security
async def test_member_seats_on_every_server_go_when_the_last_member_leaves(env):
    """M1 + L8: a member's auto-subscribe seats us on several servers; when
    the last local member leaves (``SpaceMemberLeft``) every one is released
    — in the background, never inside the leave — and only those."""
    for gid, url in (("gfs-1", "https://a.test"), ("gfs-2", "https://b.test")):
        await env.conns.save(_conn(gid, inbox_url=url))
    await env.conns.save(_conn("gfs-3", inbox_url="https://c.test"))
    await _seat_subscription(env, "sp-w")
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs)
    bus = EventBus()
    svc.wire(bus)
    for gid in ("gfs-1", "gfs-2"):
        await svc.take_seat("sp-w", await env.conns.get(gid))

    # Another local user still seated → nothing released.
    await bus.publish(SpaceMemberLeft(space_id="sp-w", user_id="someone"))
    await svc.wait_idle()
    assert gfs.unsubscribes == []

    await env.spaces.delete_member("sp-w", "u-local")
    await bus.publish(SpaceMemberLeft(space_id="sp-w", user_id="u-local"))
    await svc.wait_idle()
    assert sorted(gfs.unsubscribes) == [("sp-w", "gfs-1"), ("sp-w", "gfs-2")]
    assert await _seat_ids(env, "sp-w") == []


@pytest.mark.parametrize(
    "event",
    [
        RemoteSpaceMemberBanned(space_id="sp-w", user_id="u-local"),
        RemoteSpaceMemberRemoved(space_id="sp-w", instance_id="h", user_id="u-local"),
        SpaceConfigChanged(
            space_id="sp-w",
            event_type=SpaceConfigEventType.MEMBER_BANNED.value,
            payload={},
            sequence=1,
        ),
    ],
)
async def test_bans_and_removals_release_seats_too(env, event):
    """L9: the paths that drop a local seat without a ``SpaceMemberLeft``."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _seat_subscription(env, "sp-w")
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs)
    bus = EventBus()
    svc.wire(bus)
    await _seat(env, "sp-w", "gfs-1")
    await env.spaces.delete_member("sp-w", "u-local")
    await bus.publish(event)
    await svc.wait_idle()
    assert gfs.unsubscribes == [("sp-w", "gfs-1")]


async def test_other_config_changes_and_seatless_spaces_cost_nothing(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs)
    bus = EventBus()
    svc.wire(bus)
    await _seat(env, "sp-w", "gfs-1")
    await bus.publish(
        SpaceConfigChanged(space_id="sp-w", event_type="x", payload={}, sequence=1)
    )
    await bus.publish(SpaceMemberLeft(space_id="sp-none", user_id="u"))
    assert svc._tasks == set()
    assert gfs.unsubscribes == []


async def test_a_dissolved_space_wants_no_seat(env):
    """L10: a dissolved space's local rows don't keep its seat."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _mirrored(env, "sp-1", gfs="gfs-1")
    await env.spaces.mark_dissolved("sp-1")
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs)
    assert await svc.resubscribe_all("gfs-1") == 0
    assert gfs.unsubscribes == [("sp-1", "gfs-1")]


async def test_reconnect_tears_down_a_seat_nobody_wants(env):
    """A leave missed while the server was unreachable is reconciled on the
    next reconnect — but a seat taken moments ago (the member row is written
    after the subscribe) is left alone."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _seat(env, "sp-gone", "gfs-1")
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs)
    await svc.subscribe_to_gfs("sp-fresh", "gfs-1")
    gfs.subscribes.clear()

    assert await svc.resubscribe_all("gfs-1") == 1
    assert gfs.unsubscribes == [("sp-gone", "gfs-1")]
    assert gfs.subscribes == [("sp-fresh", "gfs-1")]
    assert [s.space_id for s in await env.seats.list_for_gfs("inst-gfs-1")] == [
        "sp-fresh"
    ]


async def test_reconnect_rechecks_right_before_each_release(env, monkeypatch):
    """L4: a seat judged stale at the start of the batch but wanted again
    by the time its release comes up (a subscribe landed meanwhile) stays."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _seat(env, "sp-x", "gfs-1")
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs)
    calls = {"n": 0}
    real = GfsSpaceMirrorService._releasable

    async def _flip(self, space_id, gfs_instance_id):
        calls["n"] += 1
        if calls["n"] > 1:
            return False
        return await real(self, space_id, gfs_instance_id)

    monkeypatch.setattr(GfsSpaceMirrorService, "_releasable", _flip)
    assert await svc.resubscribe_all("gfs-1") == 0
    assert gfs.unsubscribes == []
    assert await _seat_ids(env, "sp-x") == ["inst-gfs-1"]


async def test_release_unused_seats_spares_a_seat_in_grace(env):
    """L5: a seat taken moments ago (its member row not written yet) is
    never released, and ``seat_in_grace`` reports it."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs)
    await svc.subscribe_to_gfs("sp-new", "gfs-1")
    assert await svc.seat_in_grace("sp-new") is True
    assert await svc.release_unused_seats("sp-new") == 0
    assert gfs.unsubscribes == []
    svc._seated_at.clear()
    assert await svc.seat_in_grace("sp-new") is False
    assert await svc.release_unused_seats("sp-new") == 1


async def test_a_legacy_mirror_with_a_seat_anywhere_takes_no_new_one(env):
    """L6: the directory fallback is for a space with NO recorded seat on
    any server — never a second seat next to a known one."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await env.conns.save(_conn("gfs-2", inbox_url="https://b.test"))
    repo = await _mirrored(env, "sp-1", gfs=None)
    await _seat(env, "sp-1", "gfs-1")
    session = _StubSession({"https://b.test/gfs/spaces": _directory("sp-1")})
    gfs = _StubGfs()
    svc = _mirror(env, session, gfs, public_space_repo=repo)
    assert await svc.resubscribe_all("gfs-2") == 0
    assert gfs.subscribes == []


async def test_a_legacy_re_subscribe_records_the_seat(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    repo = await _mirrored(env, "sp-1", gfs=None)
    session = _StubSession({"https://a.test/gfs/spaces": _directory("sp-1")})
    svc = _mirror(env, session, _StubGfs(), public_space_repo=repo)
    assert await svc.resubscribe_all("gfs-1") == 1
    assert await _seat_ids(env, "sp-1") == ["inst-gfs-1"]


async def test_resubscribe_without_the_connection_still_takes_member_seats(env):
    """L4 (round 1): an unknown connection id never drops the caller's
    ``also``."""
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs)
    assert await svc.resubscribe_all("gfs-x", also=["sp-w"]) == 1
    assert gfs.subscribes == [("sp-w", "gfs-x")]


def _unpairer(env, mirror) -> GfsConnectionService:
    """The REAL unpair path, wired to the seat keeper as ``app`` does."""
    svc = GfsConnectionService(env.conns, http_client=_StubSession())
    svc.attach_on_disconnect(mirror.on_disconnect)
    return svc


async def _re_pair(env, old: str, new: str, url: str) -> None:
    await env.conns.save(
        GfsConnection(
            id=new,
            gfs_instance_id=f"inst-{old}",
            display_name="G",
            public_key="pk",
            inbox_url=url,
            status="active",
            paired_at="2025-02-01T00:00:00+00:00",
        )
    )


@pytest.mark.security
async def test_unpair_unsubscribes_in_the_background_and_re_pair_reconciles(env):
    """M1: an unpair (real disconnect path) keeps the rows detached and sends
    the unsubscribes in the background — the GFS doesn't keep our seat. A
    re-pair of the same server re-takes the seat a local user still wants
    and releases the one nobody wants."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    repo = await _mirrored(env, "sp-wanted", gfs="gfs-1")
    await _seat(env, "sp-unwanted", "gfs-1")
    gfs = _StubGfs()
    mirror = _mirror(env, _StubSession(), gfs, public_space_repo=repo)

    await _unpairer(env, mirror).disconnect("gfs-1")
    seats = {s.space_id: s for s in await env.seats.list_all()}
    assert all(s.detached for s in seats.values())
    await mirror.wait_idle()
    assert sorted(gfs.unsubscribes) == [
        ("sp-unwanted", "gfs-1"),
        ("sp-wanted", "gfs-1"),
    ]
    assert all(s.released for s in await env.seats.list_all())

    await _re_pair(env, "gfs-1", "gfs-1b", "https://a.test")
    gfs.unsubscribes.clear()
    assert await mirror.resubscribe_all("gfs-1b") == 1
    assert gfs.subscribes == [("sp-wanted", "gfs-1b")]
    assert gfs.unsubscribes == [("sp-unwanted", "gfs-1b")]
    rows = await env.seats.list_all()
    assert [(s.space_id, s.detached) for s in rows] == [("sp-wanted", False)]
    # The pin anchor followed the re-pair through the same row.
    assert await env.spaces.get_mirror_provenance("sp-wanted") == ("gfs-1b", 0)


@pytest.mark.security
async def test_a_failed_unpair_unsubscribe_stays_a_tombstone_and_is_retried(
    env, monkeypatch
):
    """M1: an unsubscribe the server never confirmed keeps its row — the
    sweep never drops it, however old — and the next matching connection
    takes the unsubscribe."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _seat(env, "sp-x", "gfs-1")
    gfs = _StubGfs(raise_on_unsubscribe=True)
    mirror = _mirror(env, _StubSession(), gfs)
    await _unpairer(env, mirror).disconnect("gfs-1")
    await mirror.wait_idle()
    seat = await env.seats.get("sp-x", "inst-gfs-1")
    assert seat.detached and not seat.released

    later = datetime.now(timezone.utc) + timedelta(days=400)
    monkeypatch.setattr(mirror_mod, "_utcnow", lambda: later)
    for _ in range(3):
        later += timedelta(days=2)
        assert await mirror.sweep_orphan_seats() == 0
    assert await _seat_ids(env, "sp-x") == ["inst-gfs-1"]

    gfs.raise_on_unsubscribe = False
    await _re_pair(env, "gfs-1", "gfs-1b", "https://a.test")
    await mirror.resubscribe_all("gfs-1b")
    assert ("sp-x", "gfs-1b") in gfs.unsubscribes
    assert await _seat_ids(env, "sp-x") == []


async def test_a_released_detached_seat_ages_out_after_a_confirmed_sweep(
    env, monkeypatch
):
    """Only released, detached rows age out — 90 days after the unpair, on a
    second sweep at least a day after the first that saw it expired."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _seat(env, "sp-x", "gfs-1")
    gfs = _StubGfs()
    mirror = _mirror(env, _StubSession(), gfs)
    t0 = datetime(2026, 10, 10, tzinfo=timezone.utc)
    clock = [t0]
    monkeypatch.setattr(mirror_mod, "_utcnow", lambda: clock[0])
    await _unpairer(env, mirror).disconnect("gfs-1")
    await mirror.wait_idle()

    clock[0] = t0 + timedelta(days=89)
    assert await mirror.sweep_orphan_seats() == 0
    clock[0] = t0 + timedelta(days=91)
    assert await mirror.sweep_orphan_seats() == 0  # first sighting
    clock[0] += timedelta(hours=1)
    assert await mirror.sweep_orphan_seats() == 0  # too soon to confirm
    clock[0] += timedelta(days=1)
    assert await mirror.sweep_orphan_seats() == 1
    assert await env.seats.list_all() == []


async def test_the_sweep_distrusts_an_insane_or_backwards_clock(env, monkeypatch):
    """L3: before ``CLOCK_SANE_AFTER`` nothing is stamped or aged; a stamp in
    the future (the clock went back) is reset rather than trusted."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _seat(env, "sp-x", "gfs-1")
    mirror = _mirror(env, _StubSession(), _StubGfs())
    clock = [datetime(1970, 1, 2, tzinfo=timezone.utc)]
    monkeypatch.setattr(mirror_mod, "_utcnow", lambda: clock[0])
    await env.conns.delete("gfs-1")  # gone without an unpair

    assert await mirror.sweep_orphan_seats() == 0
    seat = await env.seats.get("sp-x", "inst-gfs-1")
    assert seat.detached and seat.detached_at is None  # no stamp from 1970
    await env.seats.mark_released("sp-x", "inst-gfs-1")

    clock[0] = datetime(2026, 10, 10, tzinfo=timezone.utc)
    await mirror.sweep_orphan_seats()
    assert (
        await env.seats.get("sp-x", "inst-gfs-1")
    ).detached_at == "2026-10-10 00:00:00"

    clock[0] = datetime(2026, 1, 5, tzinfo=timezone.utc)  # went back
    await mirror.sweep_orphan_seats()
    assert (
        await env.seats.get("sp-x", "inst-gfs-1")
    ).detached_at == "2026-01-05 00:00:00"
    # A forward jump past the age only marks it; a later sweep confirms.
    clock[0] = datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert await mirror.sweep_orphan_seats() == 0
    clock[0] = datetime(2026, 12, 31, tzinfo=timezone.utc)  # back again
    assert await mirror.sweep_orphan_seats() == 0
    assert await _seat_ids(env, "sp-x") == ["inst-gfs-1"]


async def test_unfollowing_a_released_detached_seat_just_forgets_it(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _mirrored(env, "sp-1", gfs="gfs-1")
    gfs = _StubGfs()
    mirror = _mirror(env, _StubSession(), gfs)
    await _unpairer(env, mirror).disconnect("gfs-1")
    await mirror.wait_idle()
    gfs.unsubscribes.clear()
    await env.spaces.delete_member("sp-1", "u-local")
    await mirror.unsubscribe("sp-1")
    assert gfs.unsubscribes == []
    assert await _seat_ids(env, "sp-1") == []


async def test_disconnect_never_waits_on_the_network(env):
    """M1/M3: the unpair request only records local state."""

    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _seat(env, "sp-x", "gfs-1")
    gate = asyncio.Event()

    class _Slow(_StubGfs):
        async def unsubscribe_via(self, conn, space_id):
            await gate.wait()
            return await super().unsubscribe_via(conn, space_id)

    gfs = _Slow()
    mirror = _mirror(env, _StubSession(), gfs)
    await asyncio.wait_for(_unpairer(env, mirror).disconnect("gfs-1"), timeout=2)
    assert await env.conns.get("gfs-1") is None
    gate.set()
    await mirror.wait_idle()
    assert gfs.unsubscribes == [("sp-x", "gfs-1")]


async def test_a_moved_server_address_is_logged_once_and_not_rebound(env, caplog):
    """M1: same id + key at another address is a genuine move or an
    impostor — not re-bound until the proof-of-possession follow-up; one
    WARNING per seat asks for a re-follow."""

    await env.conns.save(_conn("gfs-1", inbox_url="https://old.test"))
    await _seat(env, "sp-1", "gfs-1")
    await _repair(env, "gfs-1", "gfs-1b", "https://new.test")
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs)
    with caplog.at_level(logging.WARNING):
        await svc.sweep_orphan_seats()
        await svc.sweep_orphan_seats()
        assert await svc.resubscribe_all("gfs-1b") == 0
    assert caplog.text.count("needs re-follow: server address changed") == 1
    assert gfs.subscribes == []


@pytest.mark.parametrize(
    "a, b, same",
    [
        ("https://gfs.example", "https://GFS.example:443/", True),
        ("http://gfs.example/", "http://gfs.example:80", True),
        ("https://gfs.example/inbox", "https://gfs.example", True),
        ("https://gfs.example", "https://gfs.example:8443", False),
        ("http://gfs.example", "https://gfs.example", False),
        ("https://gfs.example", "https://other.example", False),
        ("https://gfs.example:notaport", "https://gfs.example", False),
    ],
)
def test_server_addresses_compare_normalized(a, b, same):
    assert (mirror_mod._address(a) == mirror_mod._address(b)) is same


@pytest.mark.security
async def test_reactive_teardown_acts_only_on_a_seat_this_server_holds(env):
    """M3: a relay frame from a server holding a recorded, unwanted seat
    of ours → unsubscribe there (background, rate-limited). Any other frame
    — no seat on THIS server, whatever other servers hold, wanted or not —
    gets the same silence, so frames can't probe what we follow."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await env.conns.save(_conn("gfs-2", inbox_url="https://b.test"))
    await _seat(env, "sp-held", "gfs-1")  # unwanted: no local member
    await _seat(env, "sp-elsewhere", "gfs-2")  # held on ANOTHER server
    await _seat_subscription(env, "sp-followed")  # wanted, seated nowhere
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs)
    for sid in ("sp-elsewhere", "sp-followed", "sp-never-heard-of"):
        await svc.on_relay_frame({"space_id": sid}, gfs_id="gfs-1")
    await svc.wait_idle()
    assert gfs.unsubscribes == []

    frame = {"type": "relay", "space_id": "sp-held", "event_type": "x"}
    await svc.on_relay_frame(frame, gfs_id="gfs-1")
    await svc.wait_idle()
    assert gfs.unsubscribes == [("sp-held", "gfs-1")]
    await _seat(env, "sp-held", "gfs-1")  # say the server kept it anyway
    await svc.on_relay_frame(frame, gfs_id="gfs-1")  # rate-limited
    await svc.wait_idle()
    assert gfs.unsubscribes == [("sp-held", "gfs-1")]


async def test_reactive_teardown_spares_a_wanted_or_fresh_seat(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _seat_subscription(env, "sp-wanted")
    await _seat(env, "sp-wanted", "gfs-1")
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs)
    await svc.subscribe_to_gfs("sp-fresh", "gfs-1")
    for sid in ("sp-wanted", "sp-fresh"):
        await svc.on_relay_frame({"space_id": sid}, gfs_id="gfs-1")
    await svc.wait_idle()
    assert gfs.unsubscribes == []


@pytest.mark.parametrize(
    "frame, gfs_id",
    [
        ({"type": "relay", "channel_id": "ch", "space_id": "sp-1"}, "gfs-1"),
        ({"type": "relay", "space_id": 7}, "gfs-1"),
        ({"type": "relay", "space_id": "../x"}, "gfs-1"),
        ({"type": "relay"}, "gfs-1"),
        ({"space_id": "sp-1"}, "gfs-gone"),
        ({"space_id": "sp-1"}, "gfs-p"),
    ],
)
async def test_reactive_teardown_ignores_frames_it_cannot_attribute(env, frame, gfs_id):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await env.conns.save(_conn("gfs-p", inbox_url="https://p.test", status="pending"))
    await _seat(env, "sp-1", "gfs-1")
    await _seat(env, "sp-1", "gfs-p")
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs)
    await svc.on_relay_frame(frame, gfs_id=gfs_id)
    await svc.wait_idle()
    assert gfs.unsubscribes == []


async def test_background_tasks_are_bounded_and_cancelled_on_stop(env, monkeypatch):
    """L8: releases run as tracked tasks — capped, cancelled on stop, and
    nothing new is spawned once stopping."""

    svc = _mirror(env, _StubSession())
    gate = asyncio.Event()

    async def _blocked():
        await gate.wait()

    monkeypatch.setattr(mirror_mod, "MAX_PENDING_SEAT_TASKS", 1)
    svc._spawn(_blocked(), "a")
    svc._spawn(_blocked(), "b")  # over the cap: skipped, coroutine closed
    assert len(svc._tasks) == 1
    await svc.stop()
    assert svc._tasks == set()
    svc._spawn(_blocked(), "c")  # stopping: skipped
    assert svc._tasks == set()


async def test_a_failing_background_task_is_logged(env, caplog):
    svc = _mirror(env, _StubSession())

    async def _boom():
        raise RuntimeError("bug")

    svc._spawn(_boom(), "x")
    await svc.wait_idle()
    assert "background seat task failed" in caplog.text


def test_seat_bookkeeping_stays_bounded():
    store: dict = {}
    for i in range(mirror_mod._MAX_TRACKED_SEATS + 10):
        mirror_mod._remember(store, ("g", f"sp-{i}"), now=0.0)
    # Full of fresh keys: the oldest go.
    assert len(store) == mirror_mod._MAX_TRACKED_SEATS
    # Full again later: every key past the longest window is pruned first.
    late = mirror_mod.UNWANTED_RELAY_RETRY_S
    mirror_mod._remember(store, ("g", "new"), now=late)
    assert store == {("g", "new"): late}


# ─── pin-heal anchor across a re-pair ────────────────────────────────────


@pytest.mark.security
async def test_rebind_moves_the_anchor_only_for_the_same_server_key_and_url(env):
    await _follower(env)  # provenance gfs-1, conns gfs-1 + gfs-2 (key "pk")
    await _seat(env, "sp-1", "gfs-1")
    await _repair(env, "gfs-1", "gfs-1b", "https://gfs.test")
    svc = _mirror(env, _StubSession())
    assert await svc.rebind_mirrors(await env.conns.get("gfs-1b")) == 1
    assert await env.spaces.get_mirror_provenance("sp-1") == ("gfs-1b", 0)
    assert await _binding(env, "sp-1", "inst-gfs-1") == ("gfs-1b", "pk")
    # Idempotent.
    assert await svc.rebind_mirrors(await env.conns.get("gfs-1b")) == 0


@pytest.mark.security
@pytest.mark.parametrize(
    "repair",
    [
        {"key": "another-key", "url": "https://gfs.test"},
        {"key": "pk", "url": "https://impostor.test"},  # id + copied key
    ],
)
async def test_rebind_refuses_another_key_or_url(env, repair):
    """M1: pairing reads id and key off an unauthenticated ``/gfs/info`` —
    a different key, or the same copied key at another URL, inherits no
    pin-heal trust."""
    await _follower(env)
    await _seat(env, "sp-1", "gfs-1")
    await _repair(env, "gfs-1", "gfs-1b", repair["url"], key=repair["key"])
    svc = _mirror(env, _StubSession())
    assert await svc.rebind_mirrors(await env.conns.get("gfs-1b")) == 0
    assert await env.spaces.get_mirror_provenance("sp-1") == ("gfs-1", 0)
    assert await _binding(env, "sp-1", "inst-gfs-1") == ("gfs-1", "pk")


async def test_rebind_leaves_a_mirror_seated_from_another_server(env):
    """A member seat on gfs-2 must not pull the anchor of a mirror gfs-1
    seated; the seat itself follows its server's genuine re-pair."""
    await _follower(env)
    await _seat(env, "sp-1", "gfs-2")
    await _repair(env, "gfs-2", "gfs-2b", "https://other.test")
    svc = _mirror(env, _StubSession())
    assert await svc.rebind_mirrors(await env.conns.get("gfs-2b")) == 0
    assert await env.spaces.get_mirror_provenance("sp-1") == ("gfs-1", 0)
    assert await _binding(env, "sp-1", "inst-gfs-2") == ("gfs-2b", "pk")


# ─── relay-frame cost (M2) and post-grace re-check (L3) ──────────────────


async def test_a_followed_spaces_frames_cost_one_check_per_window(env, monkeypatch):
    """M2: frames of a space a local user still wants spawn nothing and are
    checked once per ``WANTED_CACHE_S``, not once per frame."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _seat_subscription(env, "sp-hot")
    await _seat(env, "sp-hot", "gfs-1")
    svc = _mirror(env, _StubSession())
    checks = {"n": 0}
    real = GfsSpaceMirrorService._releasable

    async def _count(self, space_id, gfs_instance_id):
        checks["n"] += 1
        return await real(self, space_id, gfs_instance_id)

    monkeypatch.setattr(GfsSpaceMirrorService, "_releasable", _count)
    for _ in range(20):
        await svc.on_relay_frame({"space_id": "sp-hot"}, gfs_id="gfs-1")
    assert checks["n"] == 1
    assert svc._tasks == set()


async def test_a_full_task_budget_is_skipped_quietly_on_the_frame_path(
    env, monkeypatch, caplog
):

    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _seat(env, "sp-x", "gfs-1")
    monkeypatch.setattr(mirror_mod, "MAX_PENDING_SEAT_TASKS", 0)
    svc = _mirror(env, _StubSession())
    with caplog.at_level(logging.WARNING):
        await svc.on_relay_frame({"space_id": "sp-x"}, gfs_id="gfs-1")
    assert caplog.text == ""


@pytest.mark.security
async def test_a_subscribe_undone_inside_the_grace_window_is_rechecked(
    env, monkeypatch
):
    """L3: subscribe → quick unsubscribe leaves seat (+ stub) behind while
    the grace window protects it; the re-check after the window releases
    the seat and runs the stub teardown."""
    monkeypatch.setattr(mirror_mod, "SEAT_GRACE_S", 0.05)
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs)
    torn: list[str] = []

    async def _teardown(space_id):
        torn.append(space_id)

    svc.attach_teardown(_teardown)
    await svc.subscribe_to_gfs("sp-q", "gfs-1")
    assert await svc.release_unused_seats("sp-q") == 0  # in grace
    await svc.wait_rechecks()
    assert torn == ["sp-q"]
    assert gfs.unsubscribes == [("sp-q", "gfs-1")]
    assert await _seat_ids(env, "sp-q") == []


async def test_the_recheck_leaves_a_wanted_seat_alone(env, monkeypatch):
    monkeypatch.setattr(mirror_mod, "SEAT_GRACE_S", 0.01)
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _seat_subscription(env, "sp-kept")
    gfs = _StubGfs()
    svc = _mirror(env, _StubSession(), gfs)
    await svc.subscribe_to_gfs("sp-kept", "gfs-1")
    await svc.wait_rechecks()
    assert gfs.unsubscribes == []


async def test_rechecks_are_bounded_and_cancelled_on_stop(env, monkeypatch):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    svc = _mirror(env, _StubSession())
    monkeypatch.setattr(mirror_mod, "MAX_PENDING_RECHECKS", 1)
    await svc.subscribe_to_gfs("sp-1", "gfs-1")
    await svc.subscribe_to_gfs("sp-2", "gfs-1")  # over budget: no re-check
    assert len(svc._rechecks) == 1
    await svc.stop()
    assert svc._rechecks == set()


async def test_a_failing_recheck_is_logged(env, monkeypatch, caplog):
    monkeypatch.setattr(mirror_mod, "SEAT_GRACE_S", 0.0)
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    svc = _mirror(env, _StubSession())

    async def _boom(space_id):
        raise RuntimeError("bug")

    svc.attach_teardown(_boom)
    await svc.subscribe_to_gfs("sp-1", "gfs-1")
    await svc.wait_rechecks()
    assert "post-grace re-check failed" in caplog.text


async def test_stop_lets_a_teardown_already_running_finish(env, monkeypatch):
    """L6: ``stop()`` cancels a sleeping re-check, but a teardown already
    under way (unsubscribe → purge) is let finish, within a bound."""

    monkeypatch.setattr(mirror_mod, "SEAT_GRACE_S", 0.0)
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    svc = _mirror(env, _StubSession())
    started = asyncio.Event()
    gate = asyncio.Event()
    done: list[str] = []

    async def _teardown(space_id):
        started.set()
        await gate.wait()
        done.append(space_id)

    svc.attach_teardown(_teardown)
    await svc.subscribe_to_gfs("sp-1", "gfs-1")
    await asyncio.wait_for(started.wait(), timeout=2)
    stopping = asyncio.create_task(svc.stop())
    await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(stopping, timeout=2)
    assert done == ["sp-1"]


# ─── round 5 ─────────────────────────────────────────────────────────────


@pytest.mark.security
async def test_a_wanted_detached_seat_survives_release_events_and_the_sweep(
    env, monkeypatch
):
    """M1: an unpaired server's seat that a local follower still wants is
    what the re-pair re-takes (and what carries the pin anchor) — another
    member leaving, a ban, or 100 days of sweeps never drop it."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _mirrored(env, "sp-1", gfs="gfs-1")
    gfs = _StubGfs()
    mirror = _mirror(env, _StubSession(), gfs)
    bus = EventBus()
    mirror.wire(bus)
    await _unpairer(env, mirror).disconnect("gfs-1")
    await mirror.wait_idle()
    assert (await env.seats.get("sp-1", "inst-gfs-1")).released

    await bus.publish(SpaceMemberLeft(space_id="sp-1", user_id="someone-else"))
    await mirror.wait_idle()
    assert await mirror.release_unused_seats("sp-1") == 0
    assert await _seat_ids(env, "sp-1") == ["inst-gfs-1"]

    later = datetime.now(timezone.utc) + timedelta(days=100)
    monkeypatch.setattr(mirror_mod, "_utcnow", lambda: later)
    for _ in range(3):
        later += timedelta(days=2)
        assert await mirror.sweep_orphan_seats() == 0
    assert await _seat_ids(env, "sp-1") == ["inst-gfs-1"]

    await _re_pair(env, "gfs-1", "gfs-1b", "https://a.test")
    assert await mirror.resubscribe_all("gfs-1b") == 1
    assert await env.spaces.get_mirror_provenance("sp-1") == ("gfs-1b", 0)


async def test_the_unpair_unsubscribe_skips_a_seat_re_taken_meanwhile(env):
    """L1: a quick re-pair re-took the seat before the background
    unsubscribe ran — it must not unsubscribe the live seat."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _seat(env, "sp-1", "gfs-1")
    gfs = _StubGfs()
    mirror = _mirror(env, _StubSession(), gfs)
    conn = await env.conns.get("gfs-1")
    await env.seats.mark_detached("sp-1", "inst-gfs-1", at=None)
    await env.conns.delete("gfs-1")
    await _re_pair(env, "gfs-1", "gfs-1b", "https://a.test")
    await mirror.take_seat("sp-1", await env.conns.get("gfs-1b"))
    gfs.subscribes.clear()
    await mirror._unsubscribe_detached(conn, ["sp-1", "sp-gone"])
    assert gfs.unsubscribes == []
    assert not (await env.seats.get("sp-1", "inst-gfs-1")).detached


async def test_unpair_keeps_seats_another_pairing_still_serves(env, monkeypatch):
    """L2: a seat a remaining connection still matches is not detached by
    unpairing a duplicate (``gfs_instance_id`` is UNIQUE, so this is the
    belt to that brace)."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _seat(env, "sp-1", "gfs-1")
    twin = GfsConnection(
        id="gfs-twin",
        gfs_instance_id="inst-gfs-1",
        display_name="G",
        public_key="pk",
        inbox_url="https://A.test:443/",
        status="active",
        paired_at="2025-02-01T00:00:00+00:00",
    )
    real = SqliteGfsConnectionRepo.list_all

    async def _with_twin(self):
        return [*await real(self), twin]

    monkeypatch.setattr(SqliteGfsConnectionRepo, "list_all", _with_twin)
    gfs = _StubGfs()
    mirror = _mirror(env, _StubSession(), gfs)
    assert await mirror.on_disconnect(await env.conns.get("gfs-1")) == 0
    await mirror.wait_idle()
    assert not (await env.seats.get("sp-1", "inst-gfs-1")).detached
    assert gfs.unsubscribes == []


async def test_a_re_pair_under_another_key_warns_once_per_seat(env, caplog):
    """L3: same id and address, different key — not re-bound, and the user
    is told once per seat to re-follow."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _seat(env, "sp-1", "gfs-1")
    await _repair(env, "gfs-1", "gfs-1b", "https://a.test", key="new-key")
    gfs = _StubGfs()
    mirror = _mirror(env, _StubSession(), gfs)
    with caplog.at_level(logging.WARNING):
        await mirror.sweep_orphan_seats()
        await mirror.sweep_orphan_seats()
        assert await mirror.resubscribe_all("gfs-1b") == 0
    assert caplog.text.count("needs re-follow: server key changed") == 1
    assert (await env.seats.get("sp-1", "inst-gfs-1")).refollow_warned
    assert gfs.subscribes == []


async def test_the_sweep_retries_a_tombstone_once_its_server_is_back(env):
    """L4: an unpair unsubscribe that failed (or was never sent — budget
    full, shutting down) is sent by the sweep as soon as a matching
    connection is active; with none, the sweep sends nothing."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    await _seat(env, "sp-x", "gfs-1")
    gfs = _StubGfs(raise_on_unsubscribe=True)
    mirror = _mirror(env, _StubSession(), gfs)
    await _unpairer(env, mirror).disconnect("gfs-1")
    await mirror.wait_idle()
    gfs.unsubscribes.clear()
    gfs.raise_on_unsubscribe = False

    await mirror.sweep_orphan_seats()
    assert gfs.unsubscribes == []  # no matching connection: no request
    await _re_pair(env, "gfs-1", "gfs-1b", "https://a.test")
    await mirror.sweep_orphan_seats()
    assert gfs.unsubscribes == [("sp-x", "gfs-1b")]
    assert await _seat_ids(env, "sp-x") == []


# ─── seats follow the server's new public id (C1 rebind) ─────────────────


async def _rename(env, conn_id: str, new_id: str) -> None:
    """What ``GfsConnectionService`` does on a rebind: the connection row
    (same local id, key and URL) adopts the server's new public id."""
    assert await env.conns.update_gfs_instance_id(conn_id, new_id)


@pytest.mark.security
async def test_a_follow_survives_the_servers_rename_via_the_rebind_hook(env):
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    repo = await _mirrored(env, "sp-1", gfs="gfs-1")
    gfs = _StubGfs()
    mirror = _mirror(env, _StubSession(), gfs, public_space_repo=repo)

    await _rename(env, "gfs-1", "gfs-social-home")
    await mirror.on_gfs_rebound("gfs-1")
    assert await _seat_ids(env, "sp-1") == ["gfs-social-home"]
    assert await _binding(env, "sp-1", "gfs-social-home") == ("gfs-1", "pk")

    # The reconnect re-takes (keeps) the seat …
    assert await mirror.resubscribe_all("gfs-1") == 1
    assert gfs.subscribes == [("sp-1", "gfs-1")]
    # … and the unfollow reaches the server.
    await env.spaces.delete_member("sp-1", "u-local")
    await mirror.unsubscribe("sp-1")
    assert gfs.unsubscribes == [("sp-1", "gfs-1")]
    assert await _seat_ids(env, "sp-1") == []


@pytest.mark.security
async def test_a_rename_missed_by_the_hook_is_healed_on_the_next_reconnect(env):
    """A crash between the connection's rebind and the seat move leaves the
    seats under the old id: the reconnect self-heal moves them (same key,
    same address) before reading them."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    repo = await _mirrored(env, "sp-1", gfs="gfs-1")
    gfs = _StubGfs()
    mirror = _mirror(env, _StubSession(), gfs, public_space_repo=repo)
    await _rename(env, "gfs-1", "gfs-social-home")
    assert await mirror.resubscribe_all("gfs-1") == 1
    assert gfs.subscribes == [("sp-1", "gfs-1")]
    assert await _seat_ids(env, "sp-1") == ["gfs-social-home"]


@pytest.mark.security
@pytest.mark.parametrize("bound", ["other-key", "other-url"])
async def test_a_seat_bound_to_another_key_or_address_never_follows_a_rename(
    env, bound
):
    """Only the server the seat was taken on (same key AND address) carries
    it to its new id — never a connection that merely shares the old id."""
    await env.conns.save(_conn("gfs-1", inbox_url="https://a.test"))
    repo = await _mirrored(env, "sp-1", gfs=None)
    await _seat(
        env,
        "sp-1",
        "gfs-1",
        key="pk-evil" if bound == "other-key" else None,
        url="https://evil.test" if bound == "other-url" else None,
    )
    gfs = _StubGfs()
    mirror = _mirror(env, _StubSession(), gfs, public_space_repo=repo)
    await _rename(env, "gfs-1", "gfs-social-home")
    await mirror.on_gfs_rebound("gfs-1")
    await mirror.resubscribe_all("gfs-1")
    assert await _seat_ids(env, "sp-1") == ["inst-gfs-1"]
    assert gfs.subscribes == []


async def test_a_rebind_of_an_unknown_connection_moves_nothing(env):
    mirror = _mirror(env, _StubSession())
    assert await mirror.on_gfs_rebound("nope") == 0


@pytest.mark.security
async def test_a_follow_seated_under_the_old_id_survives_a_real_rebind(env):
    """End to end through the real ``GfsConnectionService`` rebind: a follow
    seated under ``gfs-1``; the server now serves ``gfs-social-home`` under
    the pinned key; the reconnect's metadata refresh rebinds the connection
    and (hook) moves the seat; the reconnect reconcile keeps the seat and
    the unfollow reaches the server."""
    kp = generate_identity_keypair()
    await env.conns.save(
        GfsConnection(
            id="conn-1",
            gfs_instance_id="gfs-1",
            display_name="GFS",
            public_key=kp.public_key.hex(),
            inbox_url="https://gfs.test",
            status="active",
            paired_at="2025-01-01T00:00:00+00:00",
        )
    )
    repo = await _seat_subscription(env, "sp-1")
    await env.spaces.set_mirror_provenance("sp-1", gfs_id="conn-1", rotation_seq=0)
    await _seat(env, "sp-1", "conn-1")
    # ``_seat`` names the server ``inst-{conn}``; this one is ``gfs-1``.
    seat = await env.seats.get("sp-1", "inst-conn-1")
    await env.seats.forget("sp-1", "inst-conn-1")
    await env.seats.record(replace(seat, gfs_instance_id="gfs-1"))

    caps = {"anonymous_publish": True}
    sig, suite = sign_capabilities(kp.private_key, "gfs-social-home", caps)
    info = {
        "gfs_instance_id": "gfs-social-home",
        "public_key": kp.public_key.hex(),
        "server_name": "GFS",
        "capabilities": caps,
        "capabilities_sig": sig,
        "capabilities_sig_suite": suite,
    }
    conns = GfsConnectionService(
        env.conns,
        http_client=_StubSession({"https://gfs.test/gfs/info": (200, info)}),  # type: ignore[arg-type]
    )
    gfs = _StubGfs()
    mirror = _mirror(env, _StubSession(), gfs, public_space_repo=repo)
    conns.attach_on_rebound(mirror.on_gfs_rebound)

    await conns.refresh_connection_metadata("conn-1")
    assert (await env.conns.get("conn-1")).gfs_instance_id == "gfs-social-home"
    assert await _seat_ids(env, "sp-1") == ["gfs-social-home"]

    assert await mirror.resubscribe_all("conn-1") == 1
    assert gfs.subscribes == [("sp-1", "conn-1")]
    await env.spaces.delete_member("sp-1", "u-local")
    await mirror.unsubscribe("sp-1")
    assert gfs.unsubscribes == [("sp-1", "conn-1")]
    assert await _seat_ids(env, "sp-1") == []
