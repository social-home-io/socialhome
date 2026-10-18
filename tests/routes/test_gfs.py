"""HTTP tests for /api/gfs/* routes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import aiohttp
import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    gfs_connection_service_key,
    gfs_ws_supervisor_key,
    instance_id_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.capabilities_sig import sign_capabilities
from socialhome.config import Config
from socialhome.crypto import derive_user_id, generate_identity_keypair
from socialhome.domain.federation import GfsConnection
from socialhome.gfs_proof import PROOF_CAPABILITY
from socialhome.infrastructure import gfs_reconcile_retry as reconcile_retry_mod
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from tests.app_overrides import REMOVE

from .conftest import _auth


class _StubContent:
    """``resp.content`` — what ``_remote_detail`` reads an error body from."""

    def __init__(self, body: dict):
        self._raw = json.dumps(body).encode()

    async def read(self, n: int = -1) -> bytes:
        return self._raw if n < 0 else self._raw[:n]


class _StubResp:
    def __init__(self, status: int, body: dict | None = None):
        self.status = status
        self._body = body or {}
        self.content = _StubContent(self._body)

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
    time) with ``get_status``, or ``get`` raises ``get_error`` instead (a
    GFS that never answers); ``posted`` records every JSON body handed to
    ``post`` so a test can assert what left for the GFS.
    """

    def __init__(
        self,
        *,
        status: int = 200,
        body: dict | None = None,
        get_body: dict | None = None,
        get_status: int = 200,
        get_error: Exception | None = None,
    ):
        self._status = status
        self._body = body or {}
        self._get_body = get_body or {}
        self._get_status = get_status
        self._get_error = get_error
        self.posted: list[dict] = []

    def get(self, url, **kw):
        if self._get_error is not None:
            raise self._get_error
        return _StubResp(self._get_status, self._get_body)

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


async def _seed_space(
    client,
    space_id: str = "sp-1",
    space_type: str = "global",
    *,
    owner_instance_id: str | None = None,
) -> None:
    """Create a real local space row so publish can build + sign a body,
    owned by this household with the test admin as its OWNER — only the
    space owner chooses where a global space is listed.

    The GFS now mandates a signed publish body, so the HFS refuses to
    publish a space it doesn't hold locally. A publish test therefore needs
    the space to actually exist.
    """
    own = owner_instance_id or client.app[instance_id_key]
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key, space_type, join_mode) "
        "VALUES(?, 'Space One', ?, 'admin', ?, ?, ?)",
        # A household space this household hosts is always open (0101).
        (
            space_id,
            own,
            "aa" * 32,
            space_type,
            "open" if space_type == "household" else "invite_only",
        ),
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,'owner')",
        (space_id, client._uid),
    )


async def _seed_user(client, name: str, *, is_admin: bool = False) -> str:
    """A second local user with a token; returns the raw token."""
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES(?, ?, ?, ?)",
        (name, f"{name}-id", name, int(is_admin)),
    )
    raw = f"{name}-tok"
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES(?, ?, 't', ?)",
        (f"t-{name}", f"{name}-id", sha256_token_hash(raw)),
    )
    return raw


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


async def test_list_enriches_each_connection_with_live_health(make_client):
    # A stored ``status='active'`` pairing whose WS is actually down must
    # NOT read as connected — the row carries the supervisor's live signal.
    supervisor = _StubSupervisor(
        {
            "gfs-up": {"connected": True, "last_error": None},
            "gfs-down": {
                "connected": False,
                "last_error": "unknown-instance",
            },
        },
    )
    client = await make_client({gfs_ws_supervisor_key: supervisor})
    await _seed_gfs(client, "gfs-up")
    await _seed_gfs(client, "gfs-down")
    r = await client.get("/api/gfs/connections", headers=_auth(client._tok))
    assert r.status == 200
    body = {c["id"]: c for c in await r.json()}
    assert body["gfs-up"]["connected"] is True
    assert body["gfs-up"]["last_error"] is None
    assert body["gfs-down"]["connected"] is False
    assert body["gfs-down"]["last_error"] == "unknown-instance"


async def test_list_defaults_health_when_supervisor_unwired(make_client):
    # Defensive: with no supervisor in the app the row still carries the
    # liveness fields, defaulting to disconnected / no error.
    client = await make_client({gfs_ws_supervisor_key: REMOVE})
    await _seed_gfs(client, "gfs-1")
    r = await client.get("/api/gfs/connections", headers=_auth(client._tok))
    assert r.status == 200
    [row] = await r.json()
    assert row["connected"] is False
    assert row["last_error"] is None


async def test_list_reports_envelope_relay_from_the_cache_only(client):
    # ``envelope_relay`` is what the pairing screen's reach picker keys on.
    # It comes from the signed-capability cache (never a probe on a list
    # read) and only an ACTIVE connection can carry it.
    await _seed_gfs(client, "gfs-relay")
    await _seed_gfs(client, "gfs-cold")
    await _seed_gfs(client, "gfs-pending", status="pending")
    svc = client.app[gfs_connection_service_key]
    svc._envelope_relay["gfs-relay"] = True
    svc._envelope_relay["gfs-pending"] = True
    r = await client.get("/api/gfs/connections", headers=_auth(client._tok))
    assert r.status == 200
    body = {c["id"]: c for c in await r.json()}
    assert body["gfs-relay"]["envelope_relay"] is True
    assert body["gfs-cold"]["envelope_relay"] is False
    assert body["gfs-pending"]["envelope_relay"] is False


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


async def _pair(client, session: _StubSession, *, gfs_url="https://gfs.example.com"):
    """``POST /api/gfs/connections`` with the GFS's HTTP answered by *session*."""
    client.app[gfs_connection_service_key]._http_client = session
    return await client.post(
        "/api/gfs/connections",
        json={"gfs_url": gfs_url, "token": "tok"},
        headers=_auth(client._tok),
    )


# The pasted / scanned pairing code answers the SAME codes as the open
# sign-up path (``/default``) for the same causes, so the SPA can tell
# "couldn't reach it" from "it said no" instead of blaming the GFS for a
# network problem. The detail is the household's own sentence — never the
# GFS's words.


async def test_pair_answers_502_gfs_unreachable_when_the_gfs_never_answers(
    client,
):
    session = _StubSession(get_error=aiohttp.ClientConnectionError("refused"))
    r = await _pair(client, session)
    assert r.status == 502, await r.text()
    assert (await r.json())["error"]["code"] == "GFS_UNREACHABLE"
    assert session.posted == []


async def test_pair_answers_502_gfs_unreachable_when_the_gfs_fails_after_contact(
    client,
):
    """A 5xx from ``/gfs/register`` is the GFS having trouble, not refusing."""
    session = _StubSession(get_body=_GFS_INFO, status=503, body={"error": "db down"})
    r = await _pair(client, session)
    assert r.status == 502, await r.text()
    body = await r.json()
    assert body["error"]["code"] == "GFS_UNREACHABLE"
    assert "db down" not in json.dumps(body)


async def test_pair_answers_422_gfs_identity_mismatch_when_the_address_is_not_a_gfs(
    client,
):
    """``/gfs/info`` answered, but not with a GFS descriptor: the address in
    the pairing code doesn't point at a Social Home GFS."""
    session = _StubSession(get_body={"hello": "world"})
    r = await _pair(client, session)
    assert r.status == 422, await r.text()
    assert (await r.json())["error"]["code"] == "GFS_IDENTITY_MISMATCH"
    assert session.posted == []


async def test_pair_answers_422_gfs_identity_mismatch_when_info_is_404(client):
    """A web server with no ``/gfs/info`` at all is not a GFS either — and
    retrying later (what UNREACHABLE suggests) won't change that."""
    session = _StubSession(get_status=404, get_body={"error": "no such page"})
    r = await _pair(client, session)
    assert r.status == 422, await r.text()
    body = await r.json()
    assert body["error"]["code"] == "GFS_IDENTITY_MISMATCH"
    assert "no such page" not in json.dumps(body)


async def test_pair_answers_409_already_connected(client):
    """The pairing code names a GFS this household already has: say so
    before anything leaves the household (no second registration)."""
    await _seed_gfs(client, "gfs-1")  # inbox_url https://gfs.example.com
    session = _StubSession(get_body=_GFS_INFO, body={"status": "registered"})
    r = await _pair(client, session, gfs_url="https://gfs.example.com/")
    assert r.status == 409, await r.text()
    assert (await r.json())["error"]["code"] == "ALREADY_CONNECTED"
    assert session.posted == []


async def test_pair_answers_422_gfs_pairing_failed_when_the_gfs_refuses_the_token(
    client,
):
    """The GFS answered ``/gfs/register`` with a 4xx (stale / used token):
    that IS "the GFS didn't accept this household" — in our words."""
    session = _StubSession(
        get_body=_GFS_INFO, status=401, body={"error": "token already used"}
    )
    r = await _pair(client, session)
    assert r.status == 422, await r.text()
    body = await r.json()
    assert body["error"]["code"] == "GFS_PAIRING_FAILED"
    assert "token already used" not in json.dumps(body)


async def test_pair_answers_422_gfs_pairing_failed_for_an_insecure_url(client):
    """Plain ``http://`` on the public internet never leaves the household."""
    session = _StubSession(get_body=_GFS_INFO, body={"status": "registered"})
    r = await _pair(client, session, gfs_url="http://gfs.example.com")
    assert r.status == 422, await r.text()
    body = await r.json()
    assert body["error"]["code"] == "GFS_PAIRING_FAILED"
    assert "https://" in body["error"]["detail"]
    assert session.posted == []


def _info_offering_proof() -> dict:
    """A descriptor whose SIGNED block offers ``proof_of_possession``."""
    kp = generate_identity_keypair()
    caps = {PROOF_CAPABILITY: True}
    sig, suite = sign_capabilities(kp.private_key, "inst-remote", caps)
    return {
        **_GFS_INFO,
        "public_key": kp.public_key.hex(),
        "capabilities": caps,
        "capabilities_sig": sig,
        "capabilities_sig_suite": suite,
    }


async def test_pair_answers_422_gfs_proof_failed_when_the_proof_fails(client):
    """The GFS offers proof of possession and can't give one (here: the
    challenge answer carries no signature) — refused before registering."""
    session = _StubSession(get_body=_info_offering_proof(), body={"nope": True})
    r = await _pair(client, session)
    assert r.status == 422, await r.text()
    body = await r.json()
    assert body["error"]["code"] == "GFS_PROOF_FAILED"
    # Only the challenge left the household — never the registration.
    assert [set(b) for b in session.posted] == [{"nonce"}]


async def test_pair_reports_possession_unproven_for_an_older_gfs(client):
    session = _StubSession(get_body=_GFS_INFO, body={"status": "registered"})
    r = await _pair(client, session)
    assert r.status == 201, await r.text()
    assert (await r.json())["possession_proven"] is False


async def test_list_and_detail_report_possession_from_the_cache(client):
    await _seed_gfs(client, "gfs-1")
    await _seed_gfs(client, "gfs-2")
    svc = client.app[gfs_connection_service_key]
    svc._possession[("gfs-1", "https://gfs.example.com")] = True
    r = await client.get("/api/gfs/connections", headers=_auth(client._tok))
    body = {c["id"]: c for c in await r.json()}
    assert body["gfs-1"]["possession_proven"] is True
    assert body["gfs-2"]["possession_proven"] is None
    r = await client.get("/api/gfs/connections/gfs-1", headers=_auth(client._tok))
    assert (await r.json())["possession_proven"] is True


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


@pytest.mark.security
@pytest.mark.parametrize("is_admin", [False, True])
async def test_publish_requires_the_space_owner(client, is_admin):
    """Choosing where a global space is listed is its OWNER's call — not a
    household admin's, not anyone else's."""
    await _seed_gfs(client, "gfs-1")
    await _seed_space(client, "sp-1")
    _stub_session(client, status=200)
    raw = await _seed_user(client, "bob3", is_admin=is_admin)
    r = await client.post(
        "/api/spaces/sp-1/publish/gfs-1",
        headers=_auth(raw),
    )
    assert r.status == 403
    assert client.app[gfs_connection_service_key]._http_client.posted == []


async def test_publish_gfs_not_found(client):
    await _seed_space(client, "sp-1")
    r = await client.post(
        "/api/spaces/sp-1/publish/nonexistent",
        headers=_auth(client._tok),
    )
    assert r.status == 422
    assert (await r.json())["error"]["code"] == "GFS_PUBLISH_FAILED"


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


@pytest.mark.security
async def test_publish_refuses_a_public_space_with_tier_change_required(client):
    """Public is visible only to connected households: publishing it to a
    GFS would make it global — a tier change. 409, nothing sent."""
    await _seed_gfs(client, "gfs-1")
    await _seed_space(client, "sp-pub", space_type="public")
    _stub_session(client, status=200, body={"status": "active"})
    r = await client.post(
        "/api/spaces/sp-pub/publish/gfs-1",
        headers=_auth(client._tok),
    )
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "TIER_CHANGE_REQUIRED"
    assert client.app[gfs_connection_service_key]._http_client.posted == []


@pytest.mark.security
@pytest.mark.parametrize("space_type", ["private", "household"])
async def test_publish_refuses_a_space_outside_the_public_tiers(client, space_type):
    """Only global spaces may be listed on a GFS; private / household is a
    409 SPACE_NOT_PUBLIC and nothing is sent to the GFS."""
    await _seed_gfs(client, "gfs-1")
    await _seed_space(client, "sp-priv", space_type=space_type)
    _stub_session(client, status=200, body={"status": "active"})
    r = await client.post(
        "/api/spaces/sp-priv/publish/gfs-1",
        headers=_auth(client._tok),
    )
    assert r.status == 409
    body = await r.json()
    assert body["error"]["code"] == "SPACE_NOT_PUBLIC"


async def test_publish_unknown_space_is_rejected(client):
    """Fail-closed: a space the HFS doesn't hold locally is a 404 — never an
    unsigned, metadata-less ``{space_id}`` publish."""
    await _seed_gfs(client, "gfs-1")
    _stub_session(client, status=200, body={"status": "active"})
    r = await client.post(
        "/api/spaces/sp-nonexistent/publish/gfs-1",
        headers=_auth(client._tok),
    )
    assert r.status == 404
    assert client.app[gfs_connection_service_key]._http_client.posted == []


@pytest.mark.security
async def test_publish_refuses_a_space_hosted_elsewhere(client):
    await _seed_gfs(client, "gfs-1")
    await _seed_space(client, "sp-remote", owner_instance_id="other-household")
    _stub_session(client, status=200)
    r = await client.post(
        "/api/spaces/sp-remote/publish/gfs-1",
        headers=_auth(client._tok),
    )
    assert r.status == 403
    assert client.app[gfs_connection_service_key]._http_client.posted == []


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


@pytest.mark.security
async def test_unpublish_without_a_listing_contacts_no_gfs(client):
    """A GFS that holds no listing of the space would learn its id from the
    withdrawal: 404, nothing sent."""
    await _seed_gfs(client, "gfs-1")
    await _seed_space(client, "sp-1")
    _stub_session(client, status=204)
    for gfs_id in ("gfs-1", "nonexistent"):
        r = await client.delete(
            f"/api/spaces/sp-1/publish/{gfs_id}",
            headers=_auth(client._tok),
        )
        assert r.status == 404
    assert client.app[gfs_connection_service_key]._http_client.posted == []


async def test_household_admin_withdraws_a_dissolved_spaces_leftover_listing(client):
    """Keyed on the publication row, not on a live space."""
    await _seed_gfs(client, "gfs-1")
    await SqliteGfsConnectionRepo(client._db).publish_space("sp-gone", "gfs-1")
    _stub_session(client, status=204)
    r = await client.delete(
        "/api/spaces/sp-gone/publish/gfs-1", headers=_auth(client._tok)
    )
    assert r.status == 204
    repo = SqliteGfsConnectionRepo(client._db)
    assert await repo.list_publications_for_space("sp-gone") == []


# ─── Reconnect reconcile behind the proof-of-possession gate ─────────


def _patch_gate(monkeypatch, svc, answers: list[str], healed: list[str]) -> None:
    async def _refresh(self, gfs_id):
        return None

    async def _gate(self, gfs_id):
        return answers.pop(0) if answers else "retry"

    async def _heal(self, gfs_id):
        healed.append(gfs_id)
        return 0

    cls = type(svc)
    monkeypatch.setattr(cls, "refresh_connection_metadata", _refresh)
    monkeypatch.setattr(cls, "possession_gate", _gate)
    monkeypatch.setattr(cls, "heal_space_pins", _heal)


@pytest.mark.parametrize(("gate", "runs"), [("pass", True), ("refuse", False)])
async def test_reconnect_reconcile_runs_only_past_the_possession_gate(
    client, monkeypatch, gate, runs
):
    """Once a connection's server proved its key (the ratchet), the bulk
    reconnect reconcile (pin heal, seat re-subscribe, …) runs only after a
    fresh proof; a verified failure skips it."""
    monkeypatch.setattr(reconcile_retry_mod, "RETRY_DELAYS_S", (0.01,))
    healed: list[str] = []
    _patch_gate(monkeypatch, client.app[gfs_connection_service_key], [gate], healed)
    await client.app[gfs_ws_supervisor_key]._on_connected("gfs-1")
    await asyncio.sleep(0.05)
    assert healed == (["gfs-1"] if runs else [])


async def test_a_retryable_gate_answer_runs_the_reconcile_after_a_retry(
    client, monkeypatch
):
    """A ratcheted connection whose proof got a retryable answer (a rolling
    deploy's 404, a 503, a 429) does not lose its reconcile for the life of
    the socket: the bounded retry runs gate + reconcile again."""
    monkeypatch.setattr(reconcile_retry_mod, "RETRY_DELAYS_S", (0.01, 0.01, 0.01))
    healed: list[str] = []
    _patch_gate(
        monkeypatch,
        client.app[gfs_connection_service_key],
        ["retry", "retry", "pass"],
        healed,
    )
    await client.app[gfs_ws_supervisor_key]._on_connected("gfs-1")
    assert healed == []
    for _ in range(100):
        if healed:
            break
        await asyncio.sleep(0.01)
    assert healed == ["gfs-1"]


async def test_unpublish_by_a_household_admin_who_is_not_the_owner(client):
    """The admin Spaces tab withdraws any listing: a withdrawal only narrows
    what a GFS learns (disconnecting a GFS, also admin-only, does it for
    every space)."""
    await _seed_gfs(client, "gfs-1")
    await _seed_space(client, "sp-1")
    await SqliteGfsConnectionRepo(client._db).publish_space("sp-1", "gfs-1")
    _stub_session(client, status=204)
    raw = await _seed_user(client, "carol", is_admin=True)
    r = await client.delete("/api/spaces/sp-1/publish/gfs-1", headers=_auth(raw))
    assert r.status == 204
    repo = SqliteGfsConnectionRepo(client._db)
    assert await repo.list_publications_for_space("sp-1") == []


@pytest.mark.security
async def test_unpublish_by_a_plain_user_is_refused(client):
    await _seed_gfs(client, "gfs-1")
    await _seed_space(client, "sp-1")
    await SqliteGfsConnectionRepo(client._db).publish_space("sp-1", "gfs-1")
    _stub_session(client, status=204)
    raw = await _seed_user(client, "dave")
    r = await client.delete("/api/spaces/sp-1/publish/gfs-1", headers=_auth(raw))
    assert r.status == 403
    repo = SqliteGfsConnectionRepo(client._db)
    assert len(await repo.list_publications_for_space("sp-1")) == 1


# ─── GET / PUT /api/spaces/{id}/gfs-servers ──────────────────────────


async def test_gfs_servers_get_shows_the_owner_picker(client):
    await _seed_gfs(client, "gfs-1")
    await _seed_gfs(client, "gfs-2")
    await _seed_space(client, "sp-1")
    await SqliteGfsConnectionRepo(client._db).publish_space("sp-1", "gfs-1")
    r = await client.get("/api/spaces/sp-1/gfs-servers", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body["space_type"] == "global"
    assert body["gfs_ids"] == ["gfs-1"]
    assert body["max"] == 8
    assert {a["gfs_id"]: a["listed"] for a in body["available"]} == {
        "gfs-1": True,
        "gfs-2": False,
    }


async def test_gfs_servers_put_replaces_the_set(client):
    await _seed_gfs(client, "gfs-1")
    await _seed_gfs(client, "gfs-2")
    await _seed_space(client, "sp-1")
    await SqliteGfsConnectionRepo(client._db).publish_space("sp-1", "gfs-1")
    _stub_session(client, status=200, body={"status": "active"})
    r = await client.put(
        "/api/spaces/sp-1/gfs-servers",
        json={"gfs_ids": ["gfs-2"]},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert await r.json() == {"listed": ["gfs-2"], "failed": []}
    pubs = await SqliteGfsConnectionRepo(client._db).list_publications_for_space("sp-1")
    assert [p.gfs_connection_id for p in pubs] == ["gfs-2"]


async def test_gfs_servers_put_reports_a_failing_server(client):
    await _seed_gfs(client, "gfs-1")
    await _seed_space(client, "sp-1")
    _stub_session(client, status=500)
    r = await client.put(
        "/api/spaces/sp-1/gfs-servers",
        json={"gfs_ids": ["gfs-1"]},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert await r.json() == {
        "listed": [],
        "failed": [{"gfs_id": "gfs-1", "reason": "publish_failed"}],
    }


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({}, 422, "GFS_SELECTION_INVALID"),
        ({"gfs_ids": None}, 422, "GFS_SELECTION_INVALID"),
        ({"gfs_ids": ["nope"]}, 422, "GFS_SELECTION_INVALID"),
        ({"gfs_ids": [f"g{i}" for i in range(9)]}, 422, "GFS_SELECTION_INVALID"),
    ],
)
async def test_gfs_servers_put_refuses_a_bad_body(client, body, status, code):
    await _seed_gfs(client, "gfs-1")
    await _seed_space(client, "sp-1")
    await SqliteGfsConnectionRepo(client._db).publish_space("sp-1", "gfs-1")
    _stub_session(client, status=200)
    r = await client.put(
        "/api/spaces/sp-1/gfs-servers", json=body, headers=_auth(client._tok)
    )
    assert r.status == status
    assert (await r.json())["error"]["code"] == code
    # Nothing was withdrawn: a missing / null list never means "none".
    pubs = await SqliteGfsConnectionRepo(client._db).list_publications_for_space("sp-1")
    assert len(pubs) == 1


@pytest.mark.security
async def test_gfs_servers_put_on_a_public_space_is_a_tier_change(client):
    await _seed_gfs(client, "gfs-1")
    await _seed_space(client, "sp-pub", space_type="public")
    _stub_session(client, status=200)
    r = await client.put(
        "/api/spaces/sp-pub/gfs-servers",
        json={"gfs_ids": ["gfs-1"]},
        headers=_auth(client._tok),
    )
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "TIER_CHANGE_REQUIRED"
    assert client.app[gfs_connection_service_key]._http_client.posted == []


@pytest.mark.security
@pytest.mark.parametrize("is_admin", [False, True])
async def test_gfs_servers_are_the_owners_alone(client, is_admin):
    await _seed_gfs(client, "gfs-1")
    await _seed_space(client, "sp-1")
    _stub_session(client, status=200)
    raw = await _seed_user(client, "erin", is_admin=is_admin)
    r = await client.get("/api/spaces/sp-1/gfs-servers", headers=_auth(raw))
    assert r.status == 403
    r = await client.put(
        "/api/spaces/sp-1/gfs-servers",
        json={"gfs_ids": ["gfs-1"]},
        headers=_auth(raw),
    )
    assert r.status == 403
    assert client.app[gfs_connection_service_key]._http_client.posted == []


async def test_gfs_servers_requires_auth(client):
    r = await client.get("/api/spaces/sp-1/gfs-servers")
    assert r.status == 401


async def _seed_channel_space(client, space_id: str = "sp-priv", *, on: bool = True):
    await _seed_space(client, space_id, space_type="private")
    await client._db.enqueue(
        "UPDATE spaces SET private_gfs=? WHERE id=?", (int(on), space_id)
    )


def _channel_capable(client, *gfs_ids: str) -> None:
    svc = client.app[gfs_connection_service_key]
    for gfs_id in gfs_ids:
        svc._private_channels[gfs_id] = True


async def _seed_keyed_gfs(client, *gfs_ids: str) -> None:
    """Connections with distinct, well-formed keys (the signed list names
    a server by its key)."""
    repo = SqliteGfsConnectionRepo(client._db)
    for n, gfs_id in enumerate(gfs_ids, start=1):
        await repo.save(replace(_make_conn(gfs_id), public_key=f"{n:02x}" * 32))


async def test_gfs_servers_choose_a_private_spaces_channel_servers(client):
    """A private space with ``private_gfs`` on: the owner picks its channel
    servers — stored in the signed list, never a listing."""
    await _seed_keyed_gfs(client, "gfs-1", "gfs-2")
    await _seed_channel_space(client)
    _channel_capable(client, "gfs-1", "gfs-2")
    _stub_session(client, status=200)
    r = await client.put(
        "/api/spaces/sp-priv/gfs-servers",
        json={"gfs_ids": ["gfs-2"]},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert await r.json() == {"listed": ["gfs-2"], "failed": []}
    repo = SqliteGfsConnectionRepo(client._db)
    assert await repo.list_publications_for_space("sp-priv") == []
    assert client.app[gfs_connection_service_key]._http_client.posted == []
    r = await client.get("/api/spaces/sp-priv/gfs-servers", headers=_auth(client._tok))
    body = await r.json()
    assert body["mode"] == "channel"
    assert body["gfs_ids"] == ["gfs-2"]
    assert body["listings"] == []
    assert {a["gfs_id"]: a["channel_capable"] for a in body["available"]} == {
        "gfs-1": True,
        "gfs-2": True,
    }
    # Members read the channel servers on the space detail.
    r = await client.get("/api/spaces/sp-priv", headers=_auth(client._tok))
    detail = await r.json()
    assert [s["display_name"] for s in detail["gfs_servers"]] == ["GFS gfs-2"]
    assert detail["gfs_unlisted"] is False


async def test_gfs_servers_refuse_a_server_without_private_channels(client):
    await _seed_gfs(client, "gfs-1")
    await _seed_channel_space(client)
    client.app[gfs_connection_service_key]._private_channels["gfs-1"] = False
    client.app[gfs_connection_service_key]._info_failed_at["gfs-1"] = 10**12
    r = await client.put(
        "/api/spaces/sp-priv/gfs-servers",
        json={"gfs_ids": ["gfs-1"]},
        headers=_auth(client._tok),
    )
    assert r.status == 422
    err = (await r.json())["error"]
    assert err["code"] == "GFS_SELECTION_INVALID"
    assert err["params"]["reason"] == "unsupported"


@pytest.mark.security
async def test_gfs_servers_private_without_the_option_are_refused(client):
    await _seed_gfs(client, "gfs-1")
    await _seed_channel_space(client, on=False)
    _channel_capable(client, "gfs-1")
    _stub_session(client, status=200)
    r = await client.put(
        "/api/spaces/sp-priv/gfs-servers",
        json={"gfs_ids": ["gfs-1"]},
        headers=_auth(client._tok),
    )
    assert r.status != 200
    assert (await r.json())["error"]["code"] == "SPACE_NOT_PUBLIC"
    assert client.app[gfs_connection_service_key]._http_client.posted == []


@pytest.mark.security
async def test_gfs_channel_servers_are_the_owners_alone(client):
    await _seed_gfs(client, "gfs-1")
    await _seed_channel_space(client)
    _channel_capable(client, "gfs-1")
    raw = await _seed_user(client, "erin", is_admin=True)
    r = await client.get("/api/spaces/sp-priv/gfs-servers", headers=_auth(raw))
    assert r.status == 403
    r = await client.put(
        "/api/spaces/sp-priv/gfs-servers",
        json={"gfs_ids": ["gfs-1"]},
        headers=_auth(raw),
    )
    assert r.status == 403
