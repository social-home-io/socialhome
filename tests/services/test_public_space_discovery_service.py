"""Tests for PublicSpaceDiscoveryService + SqlitePublicSpaceRepo."""

from __future__ import annotations

import json

import logging
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.public_space_repo import (
    PublicSpaceListing,
    SqlitePublicSpaceRepo,
)
from socialhome.services.gfs_http import (
    MAX_GFS_DIRECTORY_BODY_BYTES,
    MAX_GFS_DIRECTORY_ITEMS,
)
from socialhome.services.public_space_discovery_service import (
    PublicSpaceDiscoveryService,
)


# ─── DB fixture ──────────────────────────────────────────────────────────


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
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('alice', 'alice-id', 'Alice')",
    )
    yield db, SqlitePublicSpaceRepo(db), SqliteGfsConnectionRepo(db)
    await db.shutdown()


def _listing(space_id: str, *, instance_id: str = "remote-1", member_count: int = 5):
    return PublicSpaceListing(
        space_id=space_id,
        instance_id=instance_id,
        name=f"Space {space_id}",
        description="d",
        emoji="\U0001f310",
        lat=47.0,
        lon=8.0,
        radius_km=10,
        member_count=member_count,
    )


def _gfs_conn(
    gfs_id: str = "gfs-1", *, inbox_url: str = "https://gfs.example.com"
) -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"inst-{gfs_id}",
        display_name=f"GFS {gfs_id}",
        public_key="pk-hex",
        inbox_url=inbox_url,
        status="active",
        paired_at="2025-01-01T00:00:00+00:00",
    )


# ─── Repo ────────────────────────────────────────────────────────────────


async def test_upsert_then_list_active(env):
    _, repo, _ = env
    await repo.upsert(_listing("sp-1"))
    out = await repo.list_active()
    assert len(out) == 1
    assert out[0].space_id == "sp-1"


async def test_upsert_replaces_existing_row(env):
    _, repo, _ = env
    await repo.upsert(_listing("sp-1", member_count=5))
    await repo.upsert(_listing("sp-1", member_count=99))
    out = await repo.list_active()
    assert out[0].member_count == 99


async def test_list_active_orders_by_member_count(env):
    _, repo, _ = env
    await repo.upsert(_listing("sp-small", member_count=3))
    await repo.upsert(_listing("sp-big", member_count=300))
    await repo.upsert(_listing("sp-mid", member_count=30))
    out = await repo.list_active()
    assert [s.space_id for s in out] == ["sp-big", "sp-mid", "sp-small"]


async def test_list_active_excludes_blocked_instance(env):
    _, repo, _ = env
    await repo.upsert(_listing("sp-1", instance_id="bad-inst"))
    await repo.upsert(_listing("sp-2", instance_id="ok-inst"))
    await repo.block_instance("bad-inst", blocked_by="admin", reason="spam")
    out = await repo.list_active()
    assert {s.space_id for s in out} == {"sp-2"}


async def test_hide_for_user_removes_from_visible_list(env):
    _, repo, _ = env
    await repo.upsert(_listing("sp-1"))
    await repo.upsert(_listing("sp-2"))
    await repo.hide_for_user("alice-id", "sp-1")
    out = await repo.list_visible_for_user("alice-id")
    assert {s.space_id for s in out} == {"sp-2"}


async def test_hide_for_user_idempotent(env):
    _, repo, _ = env
    await repo.upsert(_listing("sp-1"))
    await repo.hide_for_user("alice-id", "sp-1")
    await repo.hide_for_user("alice-id", "sp-1")  # no-op
    out = await repo.list_visible_for_user("alice-id")
    assert out == []


async def test_is_instance_blocked(env):
    _, repo, _ = env
    assert await repo.is_instance_blocked("nope") is False
    await repo.block_instance("bad", blocked_by="admin")
    assert await repo.is_instance_blocked("bad") is True


async def test_purge_older_than(env):
    db, repo, _ = env
    await repo.upsert(_listing("sp-1"))
    # Manually backdate.
    old_iso = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    await db.enqueue(
        "UPDATE public_space_cache SET cached_at=? WHERE space_id=?",
        (old_iso, "sp-1"),
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    n = await repo.purge_older_than(cutoff)
    assert n == 1


# ─── Service ─────────────────────────────────────────────────────────────


class _Content:
    """Minimal stand-in for ``aiohttp``'s streaming body reader."""

    def __init__(self, raw: bytes):
        self._raw = raw

    async def read(self, n: int = -1) -> bytes:
        return self._raw if n < 0 else self._raw[:n]


class _StubResp:
    def __init__(self, status: int, body, *, raw: bytes | None = None):
        self.status = status
        self._body = body
        payload = json.dumps(body).encode() if raw is None else raw
        self.content = _Content(payload)
        self.content_length = len(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body


class _StubSession:
    def __init__(self, *, status: int = 200, body=None, raw: bytes | None = None):
        self._status = status
        self._body = body
        self._raw = raw
        self.calls: list[str] = []

    def get(self, url, **kw):
        self.calls.append(url)
        return _StubResp(self._status, self._body, raw=self._raw)


async def test_disabled_when_no_gfs_connection_repo(env):
    _, repo, _ = env
    svc = PublicSpaceDiscoveryService(repo)
    assert svc.is_active is False
    assert await svc.poll_once() == 0
    # Start should be a no-op.
    await svc.start()
    await svc.stop()


async def test_poll_once_caches_listings(env):
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    body = {
        "spaces": [
            {
                "space_id": "sp-X",
                "instance_id": "inst-X",
                "name": "X",
                "member_count": 7,
            },
        ]
    }
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body=body),
    )
    n = await svc.poll_once()
    assert n == 1
    out = await repo.list_active()
    assert len(out) == 1
    assert out[0].space_id == "sp-X"


async def test_poll_once_caches_category(env):
    """A GFS directory item's ``category`` round-trips into the cache."""
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    body = {
        "spaces": [
            {
                "space_id": "sp-cat",
                "instance_id": "inst-X",
                "name": "Cat",
                "category": "gaming",
            },
        ]
    }
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body=body),
    )
    assert await svc.poll_once() == 1
    out = await repo.list_active()
    assert len(out) == 1
    assert out[0].category == "gaming"


async def test_poll_once_normalizes_unknown_category(env):
    """An unknown category normalizes to ``general`` on cache."""
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    body = {
        "spaces": [
            {
                "space_id": "sp-weird",
                "instance_id": "inst-X",
                "name": "Weird",
                "category": "weird",
            },
        ]
    }
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body=body),
    )
    assert await svc.poll_once() == 1
    out = await repo.list_active()
    assert len(out) == 1
    assert out[0].category == "general"


async def test_poll_once_caches_join_mode(env):
    """A GFS directory item's ``join_mode`` round-trips into the cache — the
    household needs it to tell a readable space from an invite-only one."""
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    body = {
        "spaces": [
            {
                "space_id": "sp-jm",
                "instance_id": "inst-X",
                "name": "Open",
                "join_mode": "open",
            },
        ]
    }
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body=body),
    )
    assert await svc.poll_once() == 1
    out = await repo.list_active()
    assert out[0].join_mode == "open"


@pytest.mark.parametrize("item_extra", [{}, {"join_mode": "anything"}])
async def test_poll_once_join_mode_fails_closed(env, item_extra):
    """An older GFS sends no join mode and a hostile one may send nonsense —
    both cache as ``invite_only`` (listed, not publicly readable)."""
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    body = {
        "spaces": [
            {
                "space_id": "sp-jm2",
                "instance_id": "inst-X",
                "name": "Unknown",
                **item_extra,
            },
        ]
    }
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body=body),
    )
    assert await svc.poll_once() == 1
    out = await repo.list_active()
    assert out[0].join_mode == "invite_only"


async def test_poll_once_caches_allow_subscribers(env):
    """The readability opt-in round-trips into the cache — it is what
    ``GET /api/public_spaces`` uses to tell the browser whether to offer
    Subscribe, and it is independent of the join mode."""
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    body = {
        "spaces": [
            {
                "space_id": "sp-rd",
                "instance_id": "inst-X",
                "name": "Broadcast",
                "join_mode": "invite_only",
                "allow_subscribers": True,
            },
        ]
    }
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body=body),
    )
    assert await svc.poll_once() == 1
    out = await repo.list_active()
    assert out[0].allow_subscribers is True
    assert out[0].join_mode == "invite_only"


@pytest.mark.parametrize(
    "item_extra",
    [{}, {"allow_subscribers": 0}, {"allow_subscribers": "yes"}],
)
async def test_poll_once_allow_subscribers_fails_closed(env, item_extra):
    """An older GFS sends no flag, a falsy one means off, and a hostile one
    may send a truthy non-boolean. All three cache as not-readable, so the
    SPA never offers a Subscribe that 403s."""
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    body = {
        "spaces": [
            {
                "space_id": "sp-rd2",
                "instance_id": "inst-X",
                "name": "Unknown",
                "join_mode": "open",
                **item_extra,
            },
        ]
    }
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body=body),
    )
    assert await svc.poll_once() == 1
    out = await repo.list_active()
    assert out[0].allow_subscribers is False


async def test_poll_once_skips_blocked_instances(env):
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    await repo.block_instance("bad-inst", blocked_by="admin")
    body = {
        "spaces": [
            {"space_id": "sp-1", "instance_id": "bad-inst", "name": "X"},
            {"space_id": "sp-2", "instance_id": "ok-inst", "name": "Y"},
        ]
    }
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body=body),
    )
    await svc.poll_once()
    out = await repo.list_active()
    assert {s.space_id for s in out} == {"sp-2"}


async def test_poll_once_handles_non_200(env):
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(status=503, body={}),
    )
    n = await svc.poll_once()
    assert n == 0


async def test_poll_once_handles_malformed_body(env):
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body={"not_spaces": "x"}),
    )
    n = await svc.poll_once()
    assert n == 0


async def test_poll_once_skips_malformed_items(env):
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    body = {
        "spaces": [
            {"space_id": "good", "instance_id": "i", "name": "X"},
            "not a dict",
            {"missing_required_fields": True},
        ]
    }
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body=body),
    )
    n = await svc.poll_once()
    assert n == 1


async def test_poll_once_purges_stale_cache(env):
    db, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    await repo.upsert(_listing("sp-old"))
    old_iso = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    await db.enqueue(
        "UPDATE public_space_cache SET cached_at=? WHERE space_id=?",
        (old_iso, "sp-old"),
    )
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        cache_ttl_hours=24,
        http_client=_StubSession(body={"spaces": []}),
    )
    await svc.poll_once()
    out = await repo.list_active()
    assert all(s.space_id != "sp-old" for s in out)


async def test_poll_once_no_active_connections_returns_zero(env):
    _, repo, gfs_repo = env
    # No active connections saved.
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body={"spaces": []}),
    )
    n = await svc.poll_once()
    assert n == 0


async def test_poll_once_multiple_gfs(env):
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1", inbox_url="https://gfs1.example.com"))
    await gfs_repo.save(_gfs_conn("gfs-2", inbox_url="https://gfs2.example.com"))
    body = {
        "spaces": [
            {"space_id": "sp-A", "instance_id": "inst-A", "name": "A"},
        ]
    }
    session = _StubSession(body=body)
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=session,
    )
    n = await svc.poll_once()
    # Both GFS were polled, each returned 1 listing.
    assert n == 2
    assert len(session.calls) == 2


async def test_start_then_stop_exits_via_stop_event(env):
    """``stop()`` must drain the loop via ``_stop`` instead of bare cancel."""
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1", inbox_url="https://gfs.example.com"))
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        poll_interval_seconds=30.0,
        http_client=_StubSession(body={"spaces": []}),
    )
    await svc.start()
    assert svc._task is not None and not svc._task.done()
    await svc.stop()
    assert svc._task is None
    # ``_stop`` is set so a follow-up ``start()`` clears it again.
    assert svc._stop.is_set() is True


async def test_start_is_idempotent(env):
    """Calling ``start()`` twice does not spawn a second task."""
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1", inbox_url="https://gfs.example.com"))
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        poll_interval_seconds=30.0,
        http_client=_StubSession(body={"spaces": []}),
    )
    await svc.start()
    first_task = svc._task
    await svc.start()
    assert svc._task is first_task
    await svc.stop()


# ─── GFS directory contract (regression: wrong URL + wrong field names) ──


def _global_space_item(space_id: str = "s1", **over) -> dict:
    """A realistic ``asdict(GlobalSpace)`` row as the GFS actually emits it."""
    item = {
        "space_id": space_id,
        "owning_instance": "inst-a",
        "name": "N",
        "description": "D",
        "subscriber_count": 7,
        "icon_url": "data:image/webp;base64,AA",
        "identity_public_key": "ab" * 32,
        "category": "tech",
        "min_age": 13,
        "status": "active",
    }
    item.update(over)
    return item


async def test_fetch_directory_hits_gfs_spaces_path(env):
    """REGRESSION: the directory lives at ``/gfs/spaces`` on the GFS.

    ``/api/public_spaces`` is *this household's own* API route, so polling
    it 404'd forever and the Global tab never populated.
    """
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1", inbox_url="https://gfs.example.com"))
    session = _StubSession(body={"spaces": []})
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=session,
    )
    await svc.poll_once()
    assert session.calls == ["https://gfs.example.com/gfs/spaces"]


async def test_poll_once_maps_global_space_shape(env):
    """A ``GlobalSpace``-shaped row maps onto the cache columns."""
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body={"spaces": [_global_space_item()]}),
    )
    assert await svc.poll_once() == 1
    out = await repo.list_active()
    assert len(out) == 1
    got = out[0]
    assert got.space_id == "s1"
    assert got.instance_id == "inst-a"
    assert got.member_count == 7
    assert got.category == "tech"
    assert got.min_age == 13
    # The GFS directory is geo-less — never invent coordinates.
    assert got.emoji is None
    assert got.lat is None
    assert got.lon is None
    assert got.radius_km is None


async def test_poll_once_clamps_min_age_without_losing_siblings(env):
    """REGRESSION: a GFS row with a non-conforming ``min_age`` (e.g. 15)
    violates the ``public_space_cache`` CHECK — before the clamp it raised
    and aborted the whole tick, dropping every other listing too."""
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    body = {
        "spaces": [
            _global_space_item("s-bad", min_age=15),
            _global_space_item("s-good", min_age=18),
        ]
    }
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body=body),
    )
    assert await svc.poll_once() == 2
    out = {row.space_id: row for row in await repo.list_active()}
    assert set(out) == {"s-bad", "s-good"}
    assert out["s-bad"].min_age == 0
    assert out["s-good"].min_age == 18


async def test_fetch_directory_logs_non_200_above_debug(env, caplog):
    """A 404/5xx from the GFS is visible at INFO — the debug-level silence
    is exactly why the wrong-URL bug survived."""
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1", inbox_url="https://gfs.example.com"))
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(status=404, body={}),
    )
    with caplog.at_level(
        logging.INFO,
        logger="socialhome.services.public_space_discovery_service",
    ):
        assert await svc.poll_once() == 0
    records = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert records, "non-200 GFS directory response must log above debug"
    joined = " ".join(r.getMessage() for r in records)
    assert "404" in joined
    assert "https://gfs.example.com/gfs/spaces" in joined


# ─── response bounds (FIX 5) ─────────────────────────────────────────────


async def test_poll_once_refuses_an_oversized_directory_body(env):
    """``aiohttp`` caps nothing by default: a hostile/compromised GFS could
    return a multi-gigabyte directory and OOM the household. Fail soft —
    the tick imports nothing and the next one retries."""
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    giant = b'{"pad": "' + b"x" * (MAX_GFS_DIRECTORY_BODY_BYTES + 10) + b'"}'
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body={}, raw=giant),
    )

    assert await svc.poll_once() == 0
    assert await repo.list_active() == []


async def test_poll_once_caps_the_number_of_imported_items(env):
    """A body well under the byte cap can still carry an enormous number of
    tiny rows — each of which would become a cache write."""
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    body = {
        "spaces": [
            {"space_id": f"sp-{i}", "instance_id": "inst-X", "name": "n"}
            for i in range(MAX_GFS_DIRECTORY_ITEMS + 25)
        ]
    }
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body=body),
    )

    assert await svc.poll_once() == MAX_GFS_DIRECTORY_ITEMS


async def test_poll_once_ignores_an_unparsable_directory_body(env):
    _, repo, gfs_repo = env
    await gfs_repo.save(_gfs_conn("gfs-1"))
    svc = PublicSpaceDiscoveryService(
        repo,
        gfs_connection_repo=gfs_repo,
        http_client=_StubSession(body={}, raw=b"<html>nope</html>"),
    )

    assert await svc.poll_once() == 0
