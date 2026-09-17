"""Tests for create_gfs_app() — GFS application factory."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from socialhome.global_server import create_gfs_app, server


@pytest.fixture
async def gfs_client(tmp_path):
    """A running GFS app client backed by a temp SQLite database."""
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    async with TestClient(TestServer(app)) as tc:
        yield tc


async def _fresh_pair_token(app, client_ip: str = "127.0.0.99") -> str:
    """Mint a one-time pair token via the in-process token service.

    The landing page issues these on a per-IP rate limit; in tests we
    bypass the rate limiter by passing a different IP each call.
    """
    token_svc = app["gfs_token_service"]
    token, _wait = await token_svc.generate(client_ip)
    assert token is not None, "token service rate-limited the test"
    return token


async def test_create_gfs_app_returns_application(tmp_path):
    """create_gfs_app() returns an aiohttp.web.Application instance."""
    app = create_gfs_app(db_path=tmp_path / "gfs_check.db")
    assert isinstance(app, web.Application)


def _route_paths(app):
    """Return the set of canonical route paths registered on *app*.

    With the ``BaseView`` subclass refactor (Session 16e), routes are
    registered via ``app.router.add_view`` which uses the ``*`` method
    and dispatches internally; checking paths alone is sufficient for
    smoke tests.
    """
    return {r.resource.canonical for r in app.router.routes()}


async def test_gfs_app_has_register_route(tmp_path):
    """The GFS app exposes /gfs/register."""
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    assert "/gfs/register" in _route_paths(app)


async def test_gfs_app_has_publish_route(tmp_path):
    """The GFS app exposes /gfs/publish."""
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    assert "/gfs/publish" in _route_paths(app)


async def test_gfs_app_has_subscribe_route(tmp_path):
    """The GFS app exposes /gfs/subscribe."""
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    assert "/gfs/subscribe" in _route_paths(app)


async def test_gfs_app_has_spaces_route(tmp_path):
    """The GFS app exposes /gfs/spaces."""
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    assert "/gfs/spaces" in _route_paths(app)


async def test_gfs_app_has_healthz_route(tmp_path):
    """The GFS app exposes /healthz."""
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    assert "/healthz" in _route_paths(app)


async def test_healthz_returns_200(gfs_client):
    """GET /healthz returns HTTP 200."""
    resp = await gfs_client.get("/healthz")
    assert resp.status == 200


async def test_healthz_returns_ok_body(gfs_client):
    """GET /healthz returns JSON body {"status": "ok"}."""
    resp = await gfs_client.get("/healthz")
    body = await resp.json()
    assert body == {"status": "ok"}


async def test_gfs_spaces_returns_empty_list_initially(gfs_client):
    """GET /gfs/spaces returns an empty list when no spaces have been published."""
    resp = await gfs_client.get("/gfs/spaces")
    assert resp.status == 200
    body = await resp.json()
    assert body == {"spaces": []}


async def test_register_instance_returns_registered(gfs_client):
    """POST /gfs/register returns {"status": "registered"} for a valid payload."""
    token = await _fresh_pair_token(gfs_client.server.app, "127.0.0.10")
    resp = await gfs_client.post(
        "/gfs/register",
        json={
            "token": token,
            "instance_id": "inst-abc",
            "public_key": "aa" * 32,
            "inbox_url": "http://example.com/inbox",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "registered"
    assert body["instance_id"] == "inst-abc"


async def test_register_missing_field_returns_400(gfs_client):
    """POST /gfs/register with missing fields returns HTTP 400."""
    token = await _fresh_pair_token(gfs_client.server.app, "127.0.0.11")
    resp = await gfs_client.post(
        "/gfs/register",
        json={"token": token, "instance_id": "inst-abc"},
    )
    assert resp.status == 400


async def test_register_missing_token_returns_400(gfs_client):
    """POST /gfs/register without a token must be rejected — accepting
    anonymous registrations would let anyone show up at the GFS."""
    resp = await gfs_client.post(
        "/gfs/register",
        json={
            "instance_id": "inst-no-tok",
            "public_key": "aa" * 32,
            "inbox_url": "http://example.com/inbox",
        },
    )
    assert resp.status == 400


async def test_register_invalid_token_returns_401(gfs_client):
    resp = await gfs_client.post(
        "/gfs/register",
        json={
            "token": "this-was-never-minted",
            "instance_id": "inst-bad-tok",
            "public_key": "aa" * 32,
            "inbox_url": "http://example.com/inbox",
        },
    )
    assert resp.status == 401
    body = await resp.json()
    assert body["error"] == "invalid_or_expired_token"


async def test_register_token_is_single_use(gfs_client):
    """A token consumed once cannot be replayed."""
    token = await _fresh_pair_token(gfs_client.server.app, "127.0.0.12")
    body = {
        "token": token,
        "instance_id": "inst-replay",
        "public_key": "aa" * 32,
        "inbox_url": "http://example.com/inbox",
    }
    first = await gfs_client.post("/gfs/register", json=body)
    assert first.status == 200
    second = await gfs_client.post("/gfs/register", json=body)
    assert second.status == 401


async def test_register_returns_pending_when_auto_accept_off(gfs_client):
    """When policy has auto_accept_clients=0 the register response is 'pending'."""
    from socialhome.global_server.app_keys import gfs_admin_repo_key

    app = gfs_client.server.app
    await app[gfs_admin_repo_key].set_config("auto_accept_clients", "0")
    token = await _fresh_pair_token(app, "127.0.0.13")
    resp = await gfs_client.post(
        "/gfs/register",
        json={
            "token": token,
            "instance_id": "new-pending.home",
            "public_key": "aa" * 32,
            "inbox_url": "http://p/wh",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "pending"


async def test_gfs_info_returns_public_key(gfs_client):
    """``GET /gfs/info`` exposes the GFS's Ed25519 public key so HFS
    clients can pin it after scanning the QR (which carries only
    ``base_url`` + ``token``).
    """
    resp = await gfs_client.get("/gfs/info")
    assert resp.status == 200
    body = await resp.json()
    assert body["gfs_instance_id"]
    assert body["public_key"]
    assert len(body["public_key"]) == 64  # Ed25519 hex
    assert body["server_name"]


async def test_admin_static_index_served(gfs_client):
    """GET /admin returns the single-page HTML dashboard."""
    resp = await gfs_client.get("/admin")
    assert resp.status == 200
    text = await resp.text()
    assert "<!doctype" in text.lower() or "<html" in text.lower()
    assert "GFS Admin" in text


async def test_healthz_is_public(gfs_client):
    """The admin auth middleware does not gate public endpoints."""
    resp = await gfs_client.get("/healthz")
    assert resp.status == 200


def _make_keypair() -> tuple[bytes, bytes]:
    """Return (private_seed_bytes, public_key_bytes) for an Ed25519 keypair."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    seed = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pk = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return seed, pk


def _sign(seed: bytes, payload: dict) -> str:
    import base64
    import json

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    sig = Ed25519PrivateKey.from_private_bytes(seed).sign(canonical)
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


async def _register_and_publish(gfs_client, *, instance_id, space_id, client_ip):
    """Register a real-keyed instance (auto-accept) + publish a known space.

    Returns the instance's private seed so the caller can sign requests.
    """
    from socialhome.global_server.app_keys import gfs_admin_repo_key

    app = gfs_client.server.app
    await app[gfs_admin_repo_key].set_config("auto_accept_clients", "1")
    seed, pk = _make_keypair()
    token = await _fresh_pair_token(app, client_ip)
    reg = await gfs_client.post(
        "/gfs/register",
        json={
            "token": token,
            "instance_id": instance_id,
            "public_key": pk.hex(),
            "inbox_url": "http://example.com/wh",
        },
    )
    assert reg.status == 200
    pub_body = {
        "owning_instance": instance_id,
        "name": "Known",
        "description": "",
        "about_markdown": "",
        "cover_url": "",
        "icon_url": "",
        "min_age": 0,
        "category": "general",
        "accent_color": "#D2542A",
        "primary_color": "#D2542A",
    }
    # Phase 5a: the service folds ``identity_public_key`` (default "") into the
    # signed canonical body, so include it here too.
    canonical = {**pub_body, "space_id": space_id, "identity_public_key": ""}
    pub = await gfs_client.post(
        f"/gfs/spaces/{space_id}/publish",
        json={**pub_body, "signature": _sign(seed, canonical)},
    )
    assert pub.status == 200
    return seed


async def test_subscribe_returns_subscribed(gfs_client):
    """POST /gfs/subscribe with a valid self-signed body returns 'subscribed'."""
    seed = await _register_and_publish(
        gfs_client, instance_id="inst-sub", space_id="space-1", client_ip="127.0.0.20"
    )
    ts = _now_iso()
    sig = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-sub",
            "space_id": "space-1",
            "ts": ts,
        },
    )
    resp = await gfs_client.post(
        "/gfs/subscribe",
        json={
            "instance_id": "inst-sub",
            "space_id": "space-1",
            "ts": ts,
            "signature": sig,
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "subscribed"


async def test_subscribe_unsigned_rejected(gfs_client):
    """POST /gfs/subscribe without a signature is rejected with 403."""
    await _register_and_publish(
        gfs_client,
        instance_id="inst-nosig",
        space_id="space-ns",
        client_ip="127.0.0.22",
    )
    resp = await gfs_client.post(
        "/gfs/subscribe",
        json={
            "instance_id": "inst-nosig",
            "space_id": "space-ns",
            "ts": _now_iso(),
            "signature": "",
        },
    )
    assert resp.status == 403


async def test_subscribe_missing_signature_field_400(gfs_client):
    """A subscribe body with no ``signature`` field at all is a 400."""
    await _register_and_publish(
        gfs_client, instance_id="inst-mf", space_id="space-mf", client_ip="127.0.0.23"
    )
    resp = await gfs_client.post(
        "/gfs/subscribe",
        json={"instance_id": "inst-mf", "space_id": "space-mf"},
    )
    assert resp.status == 400


async def test_subscribe_unsubscribe_roundtrip(gfs_client):
    """POST /gfs/subscribe then unsubscribe returns correct statuses."""
    seed = await _register_and_publish(
        gfs_client,
        instance_id="inst-unsub",
        space_id="space-X",
        client_ip="127.0.0.21",
    )
    ts = _now_iso()
    sig = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-unsub",
            "space_id": "space-X",
            "ts": ts,
        },
    )
    await gfs_client.post(
        "/gfs/subscribe",
        json={
            "instance_id": "inst-unsub",
            "space_id": "space-X",
            "ts": ts,
            "signature": sig,
        },
    )
    ts2 = _now_iso()
    sig2 = _sign(
        seed,
        {
            "action": "unsubscribe",
            "instance_id": "inst-unsub",
            "space_id": "space-X",
            "ts": ts2,
        },
    )
    resp = await gfs_client.post(
        "/gfs/subscribe",
        json={
            "instance_id": "inst-unsub",
            "space_id": "space-X",
            "action": "unsubscribe",
            "ts": ts2,
            "signature": sig2,
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "unsubscribed"


async def test_unsubscribe_missing_signature_field_400(gfs_client):
    """An unsubscribe body with no ``ts``/``signature`` at all is a 400."""
    await _register_and_publish(
        gfs_client,
        instance_id="inst-unsub-mf",
        space_id="space-umf",
        client_ip="127.0.0.24",
    )
    resp = await gfs_client.post(
        "/gfs/subscribe",
        json={
            "instance_id": "inst-unsub-mf",
            "space_id": "space-umf",
            "action": "unsubscribe",
        },
    )
    assert resp.status == 400


async def test_unsubscribe_unsigned_is_403_and_row_survives(gfs_client):
    """SECURITY: an empty signature cannot evict a subscriber over HTTP."""
    seed = await _register_and_publish(
        gfs_client,
        instance_id="inst-unsub-ns",
        space_id="space-uns",
        client_ip="127.0.0.25",
    )
    ts = _now_iso()
    sig = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-unsub-ns",
            "space_id": "space-uns",
            "ts": ts,
        },
    )
    resp = await gfs_client.post(
        "/gfs/subscribe",
        json={
            "instance_id": "inst-unsub-ns",
            "space_id": "space-uns",
            "ts": ts,
            "signature": sig,
        },
    )
    assert resp.status == 200
    resp = await gfs_client.post(
        "/gfs/subscribe",
        json={
            "instance_id": "inst-unsub-ns",
            "space_id": "space-uns",
            "action": "unsubscribe",
            "ts": _now_iso(),
            "signature": "",
        },
    )
    assert resp.status == 403
    from socialhome.global_server.app_keys import gfs_fed_repo_key

    repo = gfs_client.server.app[gfs_fed_repo_key]
    subs = await repo.list_subscribers("space-uns")
    assert any(s.instance_id == "inst-unsub-ns" for s in subs)


# ── Action domain separation (a subscribe sig can't act as unsubscribe) ──


async def _subscriber_rows(gfs_client, space_id):
    from socialhome.global_server.app_keys import gfs_fed_repo_key

    repo = gfs_client.server.app[gfs_fed_repo_key]
    return await repo.list_subscribers(space_id)


async def test_subscribe_signature_replayed_as_unsubscribe_is_403(gfs_client):
    """SECURITY: the signed payload binds ``action``, so a captured
    subscribe body re-POSTed with ``action=unsubscribe`` is rejected and
    the subscriber row survives."""
    seed = await _register_and_publish(
        gfs_client,
        instance_id="inst-ds1",
        space_id="space-ds1",
        client_ip="127.0.0.30",
    )
    ts = _now_iso()
    sig = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-ds1",
            "space_id": "space-ds1",
            "ts": ts,
        },
    )
    body = {
        "instance_id": "inst-ds1",
        "space_id": "space-ds1",
        "ts": ts,
        "signature": sig,
    }
    resp = await gfs_client.post("/gfs/subscribe", json=body)
    assert resp.status == 200

    resp = await gfs_client.post(
        "/gfs/subscribe",
        json={**body, "action": "unsubscribe"},
    )
    assert resp.status == 403
    subs = await _subscriber_rows(gfs_client, "space-ds1")
    assert any(s.instance_id == "inst-ds1" for s in subs)


async def test_unsubscribe_signature_replayed_as_subscribe_is_403(gfs_client):
    """SECURITY (mirror): an unsubscribe signature cannot re-enrol a
    household that deliberately left."""
    seed = await _register_and_publish(
        gfs_client,
        instance_id="inst-ds2",
        space_id="space-ds2",
        client_ip="127.0.0.31",
    )
    ts = _now_iso()
    sub_sig = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-ds2",
            "space_id": "space-ds2",
            "ts": ts,
        },
    )
    resp = await gfs_client.post(
        "/gfs/subscribe",
        json={
            "instance_id": "inst-ds2",
            "space_id": "space-ds2",
            "ts": ts,
            "signature": sub_sig,
        },
    )
    assert resp.status == 200

    ts2 = _now_iso()
    unsub_sig = _sign(
        seed,
        {
            "action": "unsubscribe",
            "instance_id": "inst-ds2",
            "space_id": "space-ds2",
            "ts": ts2,
        },
    )
    unsub_body = {
        "instance_id": "inst-ds2",
        "space_id": "space-ds2",
        "ts": ts2,
        "signature": unsub_sig,
    }
    resp = await gfs_client.post(
        "/gfs/subscribe",
        json={**unsub_body, "action": "unsubscribe"},
    )
    assert resp.status == 200
    subs = await _subscriber_rows(gfs_client, "space-ds2")
    assert not any(s.instance_id == "inst-ds2" for s in subs)

    # Replaying the unsubscribe signature as a subscribe must not re-enrol.
    resp = await gfs_client.post("/gfs/subscribe", json=unsub_body)
    assert resp.status == 403
    subs = await _subscriber_rows(gfs_client, "space-ds2")
    assert not any(s.instance_id == "inst-ds2" for s in subs)


async def test_subscribe_with_non_hex_public_key_is_403(gfs_client):
    """A malformed stored pubkey fails closed as 403, never a 500."""
    from socialhome.global_server.app_keys import gfs_fed_repo_key
    from socialhome.global_server.domain import ClientInstance

    repo = gfs_client.server.app[gfs_fed_repo_key]
    await repo.upsert_instance(
        ClientInstance(
            instance_id="inst-badkey",
            display_name="",
            public_key="not-hex!!",
            inbox_url="http://badkey.example/wh",
            status="active",
            auto_accept=True,
        )
    )
    ts = _now_iso()
    resp = await gfs_client.post(
        "/gfs/subscribe",
        json={
            "instance_id": "inst-badkey",
            "space_id": "space-badkey",
            "ts": ts,
            "signature": "AAAA",
        },
    )
    assert resp.status == 403


async def test_unrecognised_action_is_400(gfs_client):
    """A typo'd action must not silently fall through to subscribe."""
    seed = await _register_and_publish(
        gfs_client,
        instance_id="inst-ds3",
        space_id="space-ds3",
        client_ip="127.0.0.32",
    )
    ts = _now_iso()
    sig = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-ds3",
            "space_id": "space-ds3",
            "ts": ts,
        },
    )
    resp = await gfs_client.post(
        "/gfs/subscribe",
        json={
            "instance_id": "inst-ds3",
            "space_id": "space-ds3",
            "action": "unsubscibe",
            "ts": ts,
            "signature": sig,
        },
    )
    assert resp.status == 400
    subs = await _subscriber_rows(gfs_client, "space-ds3")
    assert not any(s.instance_id == "inst-ds3" for s in subs)


# ── main() entry point: bind address resolution (issue #563) ────────────
# The shipped image runs ``socialhome-global-server --config <toml>``. The
# bug: a baked-in GFS_HOST/GFS_PORT shadowed the file's [server] host/port.
# main() must now bind whatever ``GfsConfig.load`` resolved (env > file).


def _toml(tmp_path, *, host="127.0.0.1", port=7654):
    p = tmp_path / "global_server.toml"
    p.write_text(
        f'[server]\nhost = "{host}"\nport = {port}\nbase_url = "https://cfg.example"\n'
    )
    return p


def test_main_binds_config_host_port_without_env(tmp_path, monkeypatch):
    """With a --config TOML and no GFS_* env set, main() binds the
    file's host/port — the core #563 regression."""
    p = _toml(tmp_path, host="127.0.0.1", port=7654)
    for var in (
        "GFS_HOST",
        "GFS_PORT",
        "GFS_BASE_URL",
        "GFS_DATA_DIR",
        "GFS_DB_PATH",
        "GFS_INSTANCE_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        server.sys, "argv", ["socialhome-global-server", "--config", str(p)]
    )
    captured: dict = {}
    monkeypatch.setattr(server, "create_gfs_app", lambda cfg: object())
    monkeypatch.setattr(
        server.web,
        "run_app",
        lambda app, host=None, port=None: captured.update(host=host, port=port),
    )
    server.main()
    assert captured == {"host": "127.0.0.1", "port": 7654}


def test_main_env_overrides_config_host_port(tmp_path, monkeypatch):
    """GFS_* env stays an opt-in override (orchestrators): a per-instance
    GFS_PORT wins, while host still comes from the file."""
    p = _toml(tmp_path, host="127.0.0.1", port=7654)
    monkeypatch.delenv("GFS_HOST", raising=False)
    monkeypatch.setenv("GFS_PORT", "5555")
    monkeypatch.setattr(
        server.sys, "argv", ["socialhome-global-server", "--config", str(p)]
    )
    captured: dict = {}
    monkeypatch.setattr(server, "create_gfs_app", lambda cfg: object())
    monkeypatch.setattr(
        server.web,
        "run_app",
        lambda app, host=None, port=None: captured.update(host=host, port=port),
    )
    server.main()
    assert captured == {"host": "127.0.0.1", "port": 5555}
