"""Tests for GfsConnectionService + SqliteGfsConnectionRepo."""

from __future__ import annotations

import pytest

from social_home.crypto import derive_instance_id, generate_identity_keypair
from social_home.db.database import AsyncDatabase
from social_home.domain.federation import GfsConnection
from social_home.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from social_home.services.gfs_connection_service import (
    GfsConnectionError,
    GfsConnectionService,
)


# ─── Helpers ────────────────────────────────────────────────────────────


class _StubResp:
    __slots__ = ("status", "_body", "_text")

    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status = status
        self._body = body or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return self._text


class _StubSession:
    __slots__ = ("_status", "_body", "calls")

    def __init__(self, *, status: int = 200, body: dict | None = None):
        self._status = status
        self._body = body or {}
        self.calls: list[tuple[str, str]] = []

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        return _StubResp(self._status, self._body)

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url))
        return _StubResp(self._status, self._body)


# ─── Fixtures ───────────────────────────────────────────────────────────


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
    repo = SqliteGfsConnectionRepo(db)
    yield db, repo
    await db.shutdown()


def _make_conn(
    gfs_id: str = "gfs-1",
    *,
    status: str = "active",
    endpoint_url: str = "https://gfs.example.com",
) -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"inst-{gfs_id}",
        display_name=f"GFS {gfs_id}",
        public_key="pubkey-hex",
        endpoint_url=endpoint_url,
        status=status,
        paired_at="2025-01-01T00:00:00+00:00",
    )


# ─── Repo tests ─────────────────────────────────────────────────────────


async def test_save_and_get(env):
    _, repo = env
    conn = _make_conn("gfs-1")
    await repo.save(conn)
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.id == "gfs-1"
    assert got.gfs_instance_id == "inst-gfs-1"


async def test_get_nonexistent_returns_none(env):
    _, repo = env
    assert await repo.get("nope") is None


async def test_list_active_filters_status(env):
    _, repo = env
    await repo.save(_make_conn("a1", status="active"))
    await repo.save(_make_conn("a2", status="suspended"))
    await repo.save(_make_conn("a3", status="pending"))
    active = await repo.list_active()
    assert len(active) == 1
    assert active[0].id == "a1"


# ── Service: report_fraud ──────────────────────────────────────────────


async def test_report_fraud_signs_and_posts(env):
    _, repo = env
    await repo.save(
        _make_conn("gfs-1", status="active", endpoint_url="https://gfs.test")
    )
    session = _StubSession(status=200, body={"status": "recorded"})
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    ok = await svc.report_fraud(
        "gfs-1",
        target_type="space",
        target_id="s-1",
        category="spam",
        notes="bad",
        reporter_instance_id="me.home",
        reporter_user_id="u-1",
        signing_key=b"\x01" * 32,
    )
    assert ok is True
    assert session.calls and session.calls[0][0] == "POST"
    assert session.calls[0][1].endswith("/gfs/report")


async def test_report_fraud_returns_false_on_http_error(env):
    _, repo = env
    await repo.save(
        _make_conn("gfs-2", status="active", endpoint_url="https://gfs.test")
    )
    session = _StubSession(
        status=500,
        body={},
    )
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    ok = await svc.report_fraud(
        "gfs-2",
        target_type="instance",
        target_id="peer.home",
        category="spam",
        notes=None,
        reporter_instance_id="me.home",
        reporter_user_id=None,
        signing_key=b"\x02" * 32,
    )
    assert ok is False


async def test_report_fraud_returns_false_for_unknown_gfs(env):
    _, repo = env
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    ok = await svc.report_fraud(
        "nope",
        target_type="space",
        target_id="s-1",
        category="spam",
        notes=None,
        reporter_instance_id="me.home",
        reporter_user_id=None,
        signing_key=b"\x03" * 32,
    )
    assert ok is False
    assert session.calls == []


async def test_disconnect_deletes_connection(env):
    _, repo = env
    await repo.save(_make_conn("rm-1"))
    svc = GfsConnectionService(repo, http_client=_StubSession())  # type: ignore[arg-type]
    await svc.disconnect("rm-1")
    assert await repo.get("rm-1") is None


async def test_disconnect_unknown_raises(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())  # type: ignore[arg-type]
    with pytest.raises(GfsConnectionError):
        await svc.disconnect("nope")


async def test_publish_space_records_local(env):
    _, repo = env
    await repo.save(_make_conn("pub-1"))
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    await svc.publish_space("space-x", "pub-1")
    # Post to publish endpoint happened.
    assert session.calls
    assert session.calls[0][0] == "POST"
    assert "/gfs/spaces/space-x/publish" in session.calls[0][1]


async def test_unpublish_space_records_local(env):
    _, repo = env
    await repo.save(_make_conn("up-1"))
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    await svc.unpublish_space("space-y", "up-1")
    assert session.calls and session.calls[0][0] == "DELETE"


async def test_update_status(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.update_status("gfs-1", "suspended")
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.status == "suspended"


async def test_delete_removes_connection_and_publications(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    await repo.delete("gfs-1")
    assert await repo.get("gfs-1") is None
    pubs = await repo.list_publications("gfs-1")
    assert pubs == []


async def test_publish_and_unpublish_space(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert len(pubs) == 1
    assert pubs[0].space_id == "sp-1"

    await repo.unpublish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert pubs == []


async def test_publish_space_idempotent(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    await repo.publish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert len(pubs) == 1


async def test_list_gfs_for_space(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.save(_make_conn("gfs-2"))
    await repo.publish_space("sp-1", "gfs-1")
    await repo.publish_space("sp-1", "gfs-2")
    conns = await repo.list_gfs_for_space("sp-1")
    assert {c.id for c in conns} == {"gfs-1", "gfs-2"}


async def test_count_published_spaces(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    assert await repo.count_published_spaces("gfs-1") == 0
    await repo.publish_space("sp-1", "gfs-1")
    await repo.publish_space("sp-2", "gfs-1")
    assert await repo.count_published_spaces("gfs-1") == 2


# ─── Service tests ──────────────────────────────────────────────────────


async def test_pair_success(env):
    _, repo = env
    session = _StubSession(
        body={"gfs_instance_id": "remote-gfs-id", "display_name": "Test GFS"},
    )
    svc = GfsConnectionService(repo, http_client=session)
    conn = await svc.pair(
        {
            "gfs_url": "https://gfs.example.com",
            "token": "tok-123",
            "public_key": "pk-hex",
        }
    )
    assert conn.status == "active"
    assert conn.gfs_instance_id == "remote-gfs-id"
    assert conn.display_name == "Test GFS"
    assert len(session.calls) == 1
    assert session.calls[0] == ("POST", "https://gfs.example.com/gfs/register")

    # Saved to repo.
    saved = await repo.get(conn.id)
    assert saved is not None


async def test_pair_missing_fields(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="required"):
        await svc.pair({"gfs_url": "https://x.com"})


async def test_pair_gfs_rejects(env):
    _, repo = env
    session = _StubSession(status=403, body={})
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsConnectionError, match="HTTP 403"):
        await svc.pair(
            {
                "gfs_url": "https://gfs.example.com",
                "token": "tok",
                "public_key": "pk",
            }
        )


async def test_pair_no_instance_id_in_response(env):
    _, repo = env
    session = _StubSession(body={"no_id": True})
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsConnectionError, match="gfs_instance_id"):
        await svc.pair(
            {
                "gfs_url": "https://gfs.example.com",
                "token": "tok",
                "public_key": "pk",
            }
        )


async def test_disconnect_success(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    svc = GfsConnectionService(repo, http_client=_StubSession())
    await svc.disconnect("gfs-1")
    assert await repo.get("gfs-1") is None


async def test_disconnect_not_found(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="not found"):
        await svc.disconnect("nonexistent")


async def test_list_connections(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.save(_make_conn("gfs-2", status="suspended"))
    svc = GfsConnectionService(repo, http_client=_StubSession())
    result = await svc.list_connections()
    assert len(result) == 1
    assert result[0].id == "gfs-1"


async def test_publish_space_success(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    session = _StubSession(status=204)
    svc = GfsConnectionService(repo, http_client=session)
    await svc.publish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert len(pubs) == 1
    assert pubs[0].space_id == "sp-1"
    assert len(session.calls) == 1


async def test_publish_space_not_found(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="not found"):
        await svc.publish_space("sp-1", "nonexistent")


async def test_unpublish_space_success(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    session = _StubSession(status=204)
    svc = GfsConnectionService(repo, http_client=session)
    await svc.unpublish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert pubs == []


async def test_unpublish_space_not_found(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="not found"):
        await svc.unpublish_space("sp-1", "nonexistent")
