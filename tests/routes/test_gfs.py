"""HTTP tests for /api/gfs/* routes."""

from __future__ import annotations

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    gfs_connection_service_key,
    gfs_ws_supervisor_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id
from socialhome.domain.federation import GfsConnection
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo

from .conftest import _auth


class _StubResp:
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
    """Minimal aiohttp-session stub for the publish round-trip.

    ``get_body`` answers ``GET`` (the ``/gfs/info`` descriptor at pair
    time); ``posted`` records every JSON body handed to ``post`` so a test
    can assert what left for the GFS.
    """

    def __init__(
        self,
        *,
        status: int = 200,
        body: dict | None = None,
        get_body: dict | None = None,
    ):
        self._status = status
        self._body = body or {}
        self._get_body = get_body or {}
        self.posted: list[dict] = []

    def get(self, url, **kw):
        return _StubResp(200, self._get_body)

    def post(self, url, **kw):
        if "json" in kw:
            self.posted.append(kw["json"])
        return _StubResp(self._status, self._body)

    def delete(self, url, **kw):
        return _StubResp(self._status, self._body)


def _stub_session(client, *, status: int = 200, body: dict | None = None) -> None:
    """Swap the wired GFS service's HTTP client for a stub so publish /
    unpublish round-trips don't hit the network."""
    svc = client.app[gfs_connection_service_key]
    svc._http_client = _StubSession(status=status, body=body)


def _make_conn(
    gfs_id: str = "gfs-1",
    *,
    status: str = "active",
    inbox_url: str = "https://gfs.example.com",
) -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"inst-{gfs_id}",
        display_name=f"GFS {gfs_id}",
        public_key="pubkey-hex",
        inbox_url=inbox_url,
        status=status,
        paired_at="2025-01-01T00:00:00+00:00",
    )


async def _seed_gfs(client, gfs_id: str = "gfs-1", *, status: str = "active"):
    repo = SqliteGfsConnectionRepo(client._db)
    await repo.save(_make_conn(gfs_id, status=status))


async def _seed_space(client, space_id: str = "sp-1") -> None:
    """Create a real local space row so publish can build + sign a body.

    The GFS now mandates a signed publish body, so the HFS refuses to
    publish a space it doesn't hold locally (``GfsConnectionError`` →
    422). A publish test therefore needs the space to actually exist.
    """
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key, space_type) "
        "VALUES(?, 'Space One', 'iid', 'admin', ?, 'household')",
        (space_id, "aa" * 32),
    )


# ─── GET /api/gfs/connections ────────────────────────────────────────


async def test_list_requires_auth(client):
    r = await client.get("/api/gfs/connections")
    assert r.status == 401


async def test_list_empty(client):
    r = await client.get("/api/gfs/connections", headers=_auth(client._tok))
    assert r.status == 200
    assert await r.json() == []


async def test_list_returns_connections_of_every_status(client):
    # The UI list must surface pending/suspended connections too — the
    # SPA distinguishes them by ``status``. Active-only made a freshly
    # connected (still-pending) GFS invisible.
    await _seed_gfs(client, "gfs-1")
    await _seed_gfs(client, "gfs-2", status="suspended")
    await _seed_gfs(client, "gfs-3", status="pending")
    r = await client.get("/api/gfs/connections", headers=_auth(client._tok))
    body = await r.json()
    assert {c["id"] for c in body} == {"gfs-1", "gfs-2", "gfs-3"}
    assert {c["status"] for c in body} == {"active", "suspended", "pending"}
    # public_key should be stripped from the response.
    assert all("public_key" not in c for c in body)


class _StubSupervisor:
    """Reports a fixed liveness per gfs_id for the connection_health probe."""

    def __init__(self, health: dict[str, dict]):
        self._health = health

    def connection_health(self, gfs_id: str) -> dict:
        return self._health.get(gfs_id, {"connected": False, "last_error": None})


def _stub_supervisor(client, health: dict[str, dict]) -> None:
    client.app[gfs_ws_supervisor_key] = _StubSupervisor(health)


async def test_list_enriches_each_connection_with_live_health(client):
    # A stored ``status='active'`` pairing whose WS is actually down must
    # NOT read as connected — the row carries the supervisor's live signal.
    await _seed_gfs(client, "gfs-up")
    await _seed_gfs(client, "gfs-down")
    _stub_supervisor(
        client,
        {
            "gfs-up": {"connected": True, "last_error": None},
            "gfs-down": {
                "connected": False,
                "last_error": "unknown-instance",
            },
        },
    )
    r = await client.get("/api/gfs/connections", headers=_auth(client._tok))
    assert r.status == 200
    body = {c["id"]: c for c in await r.json()}
    assert body["gfs-up"]["connected"] is True
    assert body["gfs-up"]["last_error"] is None
    assert body["gfs-down"]["connected"] is False
    assert body["gfs-down"]["last_error"] == "unknown-instance"


async def test_list_defaults_health_when_supervisor_unwired(client):
    # Defensive: with no supervisor in the app the row still carries the
    # liveness fields, defaulting to disconnected / no error.
    await _seed_gfs(client, "gfs-1")
    client.app.pop(gfs_ws_supervisor_key, None)
    r = await client.get("/api/gfs/connections", headers=_auth(client._tok))
    assert r.status == 200
    [row] = await r.json()
    assert row["connected"] is False
    assert row["last_error"] is None


# ─── POST /api/gfs/connections (pair) ────────────────────────────────


async def test_pair_requires_admin(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('bob', 'bob-id', 'Bob', 0)",
    )
    raw = "bob-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('tb', 'bob-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    r = await client.post(
        "/api/gfs/connections",
        json={"gfs_url": "https://x.com", "token": "t", "public_key": "pk"},
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_pair_missing_fields_returns_422(client):
    r = await client.post(
        "/api/gfs/connections",
        json={"gfs_url": "https://x.com"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


_GFS_INFO = {
    "gfs_instance_id": "inst-remote",
    "public_key": "bb" * 32,
    "server_name": "Test GFS",
}


async def _client_without_external_url(aiohttp_client, tmp_dir):
    """An admin-authenticated household with NO ``[standalone].external_url``
    — the shape of a Home Assistant add-on at onboarding time, where the
    household's address isn't known yet."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
        instance_name="Alpha House",
    )
    tc = await aiohttp_client(create_app(cfg))
    db = tc.app[db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    uid = derive_user_id(bytes.fromhex(row["identity_public_key"]), "admin")
    await db.enqueue(
        "INSERT OR REPLACE INTO users(username, user_id, display_name, is_admin) "
        "VALUES(?,?,?,1)",
        ("admin", uid, "Admin"),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t1", uid, "t", sha256_token_hash("admin-tok")),
    )
    return tc


async def test_pair_succeeds_without_an_external_url(aiohttp_client, tmp_dir):
    """The regression: QR pairing with a GFS needs no household address, so
    a household without an External URL pairs — no ``422 NOT_CONFIGURED``."""
    tc = await _client_without_external_url(aiohttp_client, tmp_dir)
    session = _StubSession(get_body=_GFS_INFO, body={"status": "registered"})
    tc.app[gfs_connection_service_key]._http_client = session
    r = await tc.post(
        "/api/gfs/connections",
        json={"gfs_url": "https://gfs.example.com", "token": "tok"},
        headers=_auth("admin-tok"),
    )
    assert r.status == 201, await r.text()
    body = await r.json()
    assert body["status"] == "active"
    assert body["gfs_instance_id"] == "inst-remote"
    assert "public_key" not in body


async def test_pair_registration_body_carries_no_inbox_url(client):
    """Even with an External URL configured, the household's address is not
    the GFS's business: the register body has no ``inbox_url`` key at all."""
    session = _StubSession(get_body=_GFS_INFO, body={"status": "registered"})
    client.app[gfs_connection_service_key]._http_client = session
    r = await client.post(
        "/api/gfs/connections",
        json={"gfs_url": "https://gfs.example.com", "token": "tok"},
        headers=_auth(client._tok),
    )
    assert r.status == 201, await r.text()
    assert len(session.posted) == 1
    register_body = session.posted[0]
    assert "inbox_url" not in register_body
    assert register_body["token"] == "tok"
    assert register_body["display_name"]


# ─── GET /api/gfs/connections/{id} ──────────────────────────────────


async def test_detail_returns_connection(client):
    await _seed_gfs(client, "gfs-1")
    r = await client.get(
        "/api/gfs/connections/gfs-1",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["id"] == "gfs-1"
    assert "public_key" not in body


async def test_detail_not_found(client):
    r = await client.get(
        "/api/gfs/connections/nonexistent",
        headers=_auth(client._tok),
    )
    assert r.status == 404


# ─── DELETE /api/gfs/connections/{id} ────────────────────────────────


async def test_disconnect_requires_admin(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('bob2', 'bob2-id', 'Bob2', 0)",
    )
    raw = "bob2-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('tb2', 'bob2-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    await _seed_gfs(client, "gfs-1")
    r = await client.delete(
        "/api/gfs/connections/gfs-1",
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_disconnect_success(client):
    await _seed_gfs(client, "gfs-1")
    r = await client.delete(
        "/api/gfs/connections/gfs-1",
        headers=_auth(client._tok),
    )
    assert r.status == 204
    # Verify it's gone.
    r = await client.get(
        "/api/gfs/connections/gfs-1",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_disconnect_not_found(client):
    r = await client.delete(
        "/api/gfs/connections/nonexistent",
        headers=_auth(client._tok),
    )
    assert r.status == 404


# ─── POST /api/spaces/{id}/publish/{gfs_id} ─────────────────────────


async def test_publish_requires_admin(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('bob3', 'bob3-id', 'Bob3', 0)",
    )
    raw = "bob3-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('tb3', 'bob3-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    r = await client.post(
        "/api/spaces/sp-1/publish/gfs-1",
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_publish_gfs_not_found(client):
    r = await client.post(
        "/api/spaces/sp-1/publish/nonexistent",
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_publish_returns_publication_with_status(client):
    await _seed_gfs(client, "gfs-1")
    await _seed_space(client, "sp-1")
    _stub_session(client, status=200, body={"status": "pending"})
    r = await client.post(
        "/api/spaces/sp-1/publish/gfs-1",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["space_id"] == "sp-1"
    assert body["gfs_connection_id"] == "gfs-1"
    assert body["status"] == "pending"
    assert "published_at" in body


async def test_publish_unknown_space_is_rejected(client):
    """Fail-closed: the GFS mandates a signed publish body describing a real
    space, so publishing a space the HFS doesn't hold locally is a 422 — it
    must NOT fall back to an unsigned, metadata-less ``{space_id}`` body."""
    await _seed_gfs(client, "gfs-1")
    _stub_session(client, status=200, body={"status": "active"})
    r = await client.post(
        "/api/spaces/sp-nonexistent/publish/gfs-1",
        headers=_auth(client._tok),
    )
    assert r.status == 422
    body = await r.json()
    assert body["error"]["code"] == "GFS_PUBLISH_FAILED"


async def test_publish_returns_422_when_gfs_rejects(client):
    await _seed_gfs(client, "gfs-1")
    await _seed_space(client, "sp-1")
    _stub_session(client, status=500)
    r = await client.post(
        "/api/spaces/sp-1/publish/gfs-1",
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ─── GET /api/spaces/{id}/publications ───────────────────────────────


async def test_space_publications_requires_auth(client):
    r = await client.get("/api/spaces/sp-1/publications")
    assert r.status == 401


async def test_space_publications_requires_admin(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('bob4', 'bob4-id', 'Bob4', 0)",
    )
    raw = "bob4-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('tb4', 'bob4-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    r = await client.get(
        "/api/spaces/sp-1/publications",
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_space_publications_returns_array(client):
    await _seed_gfs(client, "gfs-1")
    await _seed_space(client, "sp-1")
    _stub_session(client, status=200, body={"status": "active"})
    # Publish so there's a row to list.
    pr = await client.post(
        "/api/spaces/sp-1/publish/gfs-1",
        headers=_auth(client._tok),
    )
    assert pr.status == 200
    r = await client.get(
        "/api/spaces/sp-1/publications",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["space_id"] == "sp-1"
    assert body[0]["gfs_connection_id"] == "gfs-1"
    assert body[0]["status"] == "active"


async def test_space_publications_empty_array(client):
    r = await client.get(
        "/api/spaces/sp-no-pubs/publications",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert await r.json() == []


# ─── DELETE /api/spaces/{id}/publish/{gfs_id} ────────────────────────


async def test_unpublish_gfs_not_found(client):
    r = await client.delete(
        "/api/spaces/sp-1/publish/nonexistent",
        headers=_auth(client._tok),
    )
    assert r.status == 422
