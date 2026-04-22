"""Tests for PublicSpaceDiscoveryService + SqlitePublicSpaceRepo."""

from __future__ import annotations

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
    gfs_id: str = "gfs-1", *, endpoint_url: str = "https://gfs.example.com"
) -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"inst-{gfs_id}",
        display_name=f"GFS {gfs_id}",
        public_key="pk-hex",
        endpoint_url=endpoint_url,
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


class _StubResp:
    def __init__(self, status: int, body):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body


class _StubSession:
    def __init__(self, *, status: int = 200, body=None):
        self._status = status
        self._body = body
        self.calls: list[str] = []

    def get(self, url, **kw):
        self.calls.append(url)
        return _StubResp(self._status, self._body)


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
    await gfs_repo.save(_gfs_conn("gfs-1", endpoint_url="https://gfs1.example.com"))
    await gfs_repo.save(_gfs_conn("gfs-2", endpoint_url="https://gfs2.example.com"))
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
