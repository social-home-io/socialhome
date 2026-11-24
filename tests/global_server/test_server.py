"""Tests for create_gfs_app() — GFS application factory."""

from __future__ import annotations

import hashlib
import logging
import stat
from datetime import UTC

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from socialhome.capabilities_sig import (
    CAPS_SIG_SUITE_ED25519,
    UnsupportedCapsSigSuite,
    verify_capabilities,
)
from socialhome.crypto import (
    derive_instance_id,
    ed25519_public_key,
)
from socialhome.csp import build_public_page_csp, build_spa_csp
from socialhome.global_server import create_gfs_app, server
from socialhome.global_server.app_keys import (
    gfs_cluster_key,
    gfs_fed_repo_key,
    gfs_token_service_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.moment_public_media import MAX_BLOB_BYTES

#: A household whose id is the one its key derives (the GFS refuses others).
_HOME_PK_HEX = ed25519_public_key(b"\x05" * 32).hex()
_HOME_ID = derive_instance_id(bytes.fromhex(_HOME_PK_HEX))


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
    token_svc = app[gfs_token_service_key]
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


@pytest.mark.parametrize(
    "path",
    [
        "/gfs/publish",
        "/gfs/subscribe",
        "/gfs/member-publish-anon",
        "/gfs/spaces/{space_id}/epoch",
        "/gfs/spaces/{space_id}/subscribers",
        "/gfs/spaces/{space_id}/key_request",
    ],
)
async def test_gfs_app_has_no_space_keyed_route(tmp_path, path):
    """v_96 (P7) — the space-keyed relay is gone: a space's content reaches
    a GFS only through its opaque channel."""
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    assert path not in _route_paths(app)
    assert "/gfs/channels/publish-anon" in _route_paths(app)


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
            "instance_id": _HOME_ID,
            "public_key": _HOME_PK_HEX,
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "registered"
    assert body["instance_id"] == _HOME_ID


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
        },
    )
    assert resp.status == 400


async def test_register_invalid_token_returns_401(gfs_client):
    resp = await gfs_client.post(
        "/gfs/register",
        json={
            "token": "this-was-never-minted",
            "instance_id": _HOME_ID,
            "public_key": _HOME_PK_HEX,
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
        "instance_id": _HOME_ID,
        "public_key": _HOME_PK_HEX,
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
            "instance_id": _HOME_ID,
            "public_key": _HOME_PK_HEX,
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "pending"


@pytest.mark.parametrize("legacy_url", ["https://inbox.example/federation/inbox", ""])
async def test_register_ignores_a_legacy_inbox_url(gfs_client, legacy_url):
    """An older household still sends ``inbox_url``. The connection server
    keeps no household address any more, so the field is dropped on the
    floor — never validated, never stored, never echoed — and the
    registration goes through like any other."""
    app = gfs_client.server.app
    token = await _fresh_pair_token(app, "127.0.0.14")
    _seed, pk = _make_keypair()
    legacy_id = derive_instance_id(pk)
    resp = await gfs_client.post(
        "/gfs/register",
        json={
            "token": token,
            "instance_id": legacy_id,
            "public_key": pk.hex(),
            "inbox_url": legacy_url,
        },
    )
    assert resp.status == 200
    assert legacy_url not in await resp.text() or not legacy_url
    stored = await app[gfs_fed_repo_key].get_instance(legacy_id)
    assert stored is not None
    assert not hasattr(stored, "inbox_url")
    columns = {
        r["name"]
        for r in await app[gfs_fed_repo_key]._db.fetchall(
            "PRAGMA table_info(client_instances)"
        )
    }
    assert "inbox_url" not in columns


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


async def test_gfs_info_capability_block_is_signed_by_the_pinned_key(gfs_client):
    """The capability that matters is SIGNED with the GFS's own identity key —
    the one already published as ``public_key`` and pinned by every household
    at pair time. A household trusts a capability only through this block,
    so an on-path attacker cannot strip a flag and push it onto a path that
    reveals more. Since v_96 (P7) the space-keyed relay's flags are gone."""
    resp = await gfs_client.get("/gfs/info")
    body = await resp.json()
    assert "anonymous_publish" not in body
    assert body["capabilities"] == {
        # §D2b — this GFS carries ``POST /gfs/envelope``, so a household only
        # attempts a bootstrap redeem against a server that proved it can
        # relay one: an on-path stripper must not be able to push a
        # household back onto a path that reveals more.
        "envelope_relay": True,
        # §24.8.5 — this GFS hosts owner-minted invite links, so a household
        # only offers to mint one against a server that proved it can serve
        # the ``/join`` page. Signed for the same reason as its siblings.
        "invite_links": True,
        # v_44 — this GFS re-pins a space's authority key from an owner
        # cert on publish. A household warns when its GFS lacks it: there,
        # a revoked admin's old key keeps authorizing relays.
        "authority_rotation": True,
        # This GFS takes ``withdrawn: true`` on a publish (a pin-only re-pin
        # of a withdrawn listing) and answers every publish with the key it
        # pins. A household only sends the field to a server that signs this.
        "withdrawn_repin": True,
        "private_channels": True,
        # Open sign-up is an operator opt-in, off by default.
        "open_signup": False,
        # Takes the optional, signed ``gfs_key`` (its own key) on household
        # requests. No ``replaces``: this server has no instance_id_aliases.
        "addressee_key": True,
        # Verifies and lists a global space's optional place pin.
        "place_pin": True,
        # The public highlight viewer page opens the end-to-end sealed stream.
        "sealed_highlight_stream": True,
        "encrypted_moments": True,
        "encrypted_moment_media": True,
        # v_66 — keeps signed delete certificates 30 days, and a moment blob
        # as long as its moment (≤ 30 days).
        "moment_delete_log": True,
        "moment_media_30d": True,
        "moment_reactions": True,
        "moment_counts": True,
        "join_contact": True,
        # v_96 — serves the contact of a space that takes follows only.
        "subscriber_seats": True,
        # The owner's signed publish may declare member / follower counts.
        "listing_counts": True,
        # The encrypted, short-lived space media cache and the limits a
        # household needs (largest blob, how long one is kept).
        "space_media": {
            "max_blob_bytes": MAX_BLOB_BYTES,
            "ttl_s": 259_200,
            "space_max_bytes": 256 * 1024**2,
            "space_uploads_per_hour": 300,
            "scopes": ["channel"],
            # v_83 — a reader may ask for a blob it missed, waiting this long.
            "on_demand": {"wait_s": 25},
        },
        # v_87 — ``POST /gfs/envelope`` takes binary media frames.
        "envelope_media": True,
        # GFS redesign PR 1 — anonymous per-space mailboxes.
        "envelope_mailbox": {"sig_suites": ["ed25519"], "idle_days": 30},
        # v_91 — ``POST /gfs/envelope/multi``: one frame for many mailboxes.
        "envelope_multi": {"max_recipients": 500},
        # Anonymous registration tickets for those mailboxes (Privacy Pass):
        # the issuer key every household blinds under, signed here.
        "mailbox_tickets": body["capabilities"]["mailbox_tickets"],
        # ``POST /gfs/proof`` needs a real base_url to bind; this fixture's
        # env fallback infers ``http://0.0.0.0:8765`` (a wildcard bind
        # address), so the GFS honestly does not offer it.
        "proof_of_possession": False,
    }
    assert body["capabilities_sig_suite"] == CAPS_SIG_SUITE_ED25519
    assert verify_capabilities(
        body["public_key"],
        body["gfs_instance_id"],
        body["capabilities"],
        body["capabilities_sig"],
        body["capabilities_sig_suite"],
    )


async def test_gfs_info_capability_signature_covers_every_signed_field(gfs_client):
    """Tampering with the capability map, or replaying the block under another
    GFS instance id, breaks verification — and an unknown suite is rejected
    outright rather than defaulted."""
    body = await (await gfs_client.get("/gfs/info")).json()
    assert not verify_capabilities(
        body["public_key"],
        body["gfs_instance_id"],
        {"subscriber_seats": False},
        body["capabilities_sig"],
        body["capabilities_sig_suite"],
    )
    assert not verify_capabilities(
        body["public_key"],
        "some-other-gfs",
        body["capabilities"],
        body["capabilities_sig"],
        body["capabilities_sig_suite"],
    )
    with pytest.raises(UnsupportedCapsSigSuite):
        verify_capabilities(
            body["public_key"],
            body["gfs_instance_id"],
            body["capabilities"],
            body["capabilities_sig"],
            "ed25519+mldsa65",
        )


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
    from datetime import datetime

    return datetime.now(UTC).isoformat()


async def _register_and_publish(
    gfs_client,
    *,
    instance_id,
    space_id,
    client_ip,
    join_mode="open",
    allow_subscribers=True,
):
    """Register a real-keyed instance (auto-accept) + publish a known space.

    Returns the instance's private seed so the caller can sign requests.
    """
    from socialhome.global_server.app_keys import gfs_admin_repo_key

    app = gfs_client.server.app
    await app[gfs_admin_repo_key].set_config("auto_accept_clients", "1")
    seed, pk = _make_keypair()
    # These fixtures name their households (``inst-sub`` …) instead of using
    # the id their key derives: seed the row first, as for a household that
    # registered before the GFS checked the derivation — the route lets a
    # re-register under the same (id, key) through (grandfathered).
    await app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(instance_id=instance_id, display_name="", public_key=pk.hex())
    )
    token = await _fresh_pair_token(app, client_ip)
    reg = await gfs_client.post(
        "/gfs/register",
        json={
            "token": token,
            "instance_id": instance_id,
            "public_key": pk.hex(),
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
        "join_mode": join_mode,
        # Subscribable ⇒ the space must be publicly readable. This flag — not
        # the join mode — is what decides that.
        "allow_subscribers": allow_subscribers,
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


# ── Action domain separation (a subscribe sig can't act as unsubscribe) ──


async def test_publish_space_with_non_hex_public_key_is_403(gfs_client):
    """A malformed stored pubkey fails closed on publish too, never a 500.

    ``register_instance`` never validates that ``public_key`` is hex, so the
    publish handler must treat an unusable stored key as an unverifiable
    signature (403) rather than letting ``bytes.fromhex`` escape as a 500.
    """
    repo = gfs_client.server.app[gfs_fed_repo_key]
    await repo.upsert_instance(
        ClientInstance(
            instance_id="inst-badkey-pub",
            display_name="",
            public_key="not-hex!!",
            status="active",
            auto_accept=True,
        )
    )
    resp = await gfs_client.post(
        "/gfs/spaces/space-badkey-pub/publish",
        json={
            "owning_instance": "inst-badkey-pub",
            "name": "Bad Key",
            "signature": "AAAA",
        },
    )
    assert resp.status == 403


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
        lambda app, host=None, port=None, **_kw: captured.update(host=host, port=port),
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
        lambda app, host=None, port=None, **_kw: captured.update(host=host, port=port),
    )
    server.main()
    assert captured == {"host": "127.0.0.1", "port": 5555}


# ─── GFS identity seed (random, persisted, never derivable) ─────────────


def _identity_pubkey_hex(seed: bytes) -> str:
    return (
        ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def _boot(data_dir, **overrides) -> str:
    """Boot a GFS rooted at *data_dir* and return its served identity key."""
    cfg = GfsConfig(
        base_url="http://127.0.0.1:8765",
        data_dir=str(data_dir),
        **overrides,
    )
    app = create_gfs_app(cfg)
    return app[gfs_cluster_key].own_public_key_hex


def test_identity_seed_is_persisted_and_stable_across_boots(tmp_path):
    """The GFS identity key is minted ONCE per data dir and read back after
    that — restarting must not change the key households pinned at pair."""
    first = _boot(tmp_path)
    seed_file = tmp_path / server.SIGNING_SEED_FILENAME
    assert seed_file.is_file()
    assert _boot(tmp_path) == first


def test_identity_seed_differs_per_data_dir(tmp_path):
    """Two deployments are two identities — nothing about the key is derived
    from public config, so two nodes with the same instance_id differ."""
    a = _boot(tmp_path / "a")
    b = _boot(tmp_path / "b")
    assert a != b


def test_identity_seed_is_not_derivable_from_public_config(tmp_path):
    """Regression for the seed derived as sha256("gfs-cluster-" + instance_id):
    ``instance_id`` is served in the clear by ``/gfs/info``, so anyone could
    recompute the private key and forge a signed capability block."""
    derived = hashlib.sha256(b"gfs-cluster-gfs-node-0").digest()
    assert _boot(tmp_path) != _identity_pubkey_hex(derived)


def test_identity_seed_file_is_owner_only(tmp_path):
    """The seed is the GFS's private key — 0600, never group/world readable."""
    _boot(tmp_path)
    mode = (tmp_path / server.SIGNING_SEED_FILENAME).stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_identity_seed_first_boot_warns_about_re_pairing(tmp_path, caplog):
    """An existing deployment upgrading into this code mints a fresh identity;
    the operator gets exactly one WARNING saying households must re-pair."""
    with caplog.at_level(logging.WARNING, logger="socialhome.global_server.server"):
        _boot(tmp_path)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "re-pair" in warnings[0].getMessage()


def test_signing_seed_override_is_honoured(tmp_path):
    """Operators managing secrets externally pin the seed via config/env; the
    file is then never written (there is nothing to persist)."""
    seed = bytes(range(32))
    served = _boot(tmp_path, signing_seed_hex=seed.hex())
    assert served == _identity_pubkey_hex(seed)
    assert not (tmp_path / server.SIGNING_SEED_FILENAME).exists()


@pytest.mark.parametrize("bad", ["ab" * 16, "zz" * 32, "not-hex"])
def test_bad_signing_seed_override_is_rejected(tmp_path, bad):
    """A wrong-length or non-hex override fails the boot rather than silently
    falling back — and the value itself never reaches the message."""
    with pytest.raises(ValueError) as exc:
        _boot(tmp_path, signing_seed_hex=bad)
    assert bad not in str(exc.value)


async def test_gfs_database_runs_the_configured_write_window(tmp_path):
    """``[server] write_batch_window_ms`` reaches the GFS's AsyncDatabase.

    Before, ``GfsApp`` built its database with no window at all, so every
    GFS node ran the 500 ms production default and every sequential write
    a request made waited out half a second.
    """
    cfg = GfsConfig(data_dir=str(tmp_path), write_batch_window_ms=37)
    gfs = server.GfsApp(cfg, db_path_override=tmp_path / "gfs.db")
    assert gfs.db.batch_window_ms == 37


# ── Security headers ──────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/healthz", "/gfs/spaces", "/"])
async def test_gfs_responses_carry_security_headers(gfs_client, path):
    """The GFS app installs the same ``on_response_prepare`` hardening
    hook as the household app — JSON APIs and public HTML alike."""
    r = await gfs_client.get(path)
    assert r.status == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Permissions-Policy" in r.headers


async def test_gfs_public_page_carries_the_public_page_csp(gfs_client):
    """The landing page carries the strict public-page policy (not the
    SPA's), and its one inline ``<style>`` is admitted by hash."""
    r = await gfs_client.get("/")
    assert r.status == 200
    assert r.headers["Content-Type"].startswith("text/html")
    body = await r.text()
    css = body.split("<style>", 1)[1].split("</style>", 1)[0]
    assert r.headers["Content-Security-Policy"] == build_public_page_csp([css])
    assert r.headers["Content-Security-Policy"] != build_spa_csp()
    assert "<script>" not in body


def test_cluster_service_advertises_the_cluster_self_url(tmp_path):
    """The cluster announces ``[cluster] advertise_url`` (not ``base_url``)
    so sibling frames reach THIS node rather than a random one behind the
    load balancer."""
    cfg = GfsConfig(
        base_url="https://gfs.example.com",
        cluster_advertise_url="http://10.0.0.5:28467",
        data_dir=str(tmp_path),
    )
    app = create_gfs_app(cfg)
    assert app[gfs_cluster_key]._self_url == "http://10.0.0.5:28467"


# ─── Access log: the GFS's own ``run_app`` redacts bearer secrets ─────────


async def test_gfs_run_app_access_log_never_records_secrets(
    tmp_path, monkeypatch, caplog
):
    """``main()`` hands ``run_app`` a redacting access logger, and that
    logger — driving the real GFS app — keeps every bearer secret the GFS
    receives out of the log: the highlight relay's ``?token=``, the
    ``/join/{token}`` invite link, the ``/highlight/…/{token}`` share link
    (also as the ``Referer`` its viewer's static asset sends), and the two
    owner-signed revoke paths that name a token.
    """
    config = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_path),
        instance_id="gfs-test",
    )
    captured: dict = {}

    def _fake_run_app(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(server.GfsConfig, "load", lambda *_a, **_k: config)
    monkeypatch.setattr(server.web, "run_app", _fake_run_app)
    monkeypatch.setattr(server.sys, "argv", ["socialhome-global-server"])
    server.main()

    kwargs = captured["kwargs"]
    assert "access_log_class" in kwargs, "GFS run_app logs secrets verbatim"
    runner = web.AppRunner(captured["app"], access_log_class=kwargs["access_log_class"])
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}"
    caplog.set_level(logging.INFO, logger="aiohttp.access")
    requests = [
        ("GET", "/gfs/highlight_rtc/relay/inst/hl?token=SECRETQUERY", None),
        ("GET", "/join/SECRETJOIN", None),
        ("GET", "/highlight/inst/hl/SECRETSHARE", None),
        (
            "GET",
            "/static/highlight_public_viewer.js",
            f"{base}/highlight/inst/hl/SECRETREFERER",
        ),
        ("POST", "/gfs/highlight_tokens/SECRETREVOKE/revoke", None),
        ("DELETE", "/gfs/spaces/sp-1/invite/SECRETINVITE", None),
    ]
    try:
        async with aiohttp.ClientSession() as session:
            for method, path, referer in requests:
                headers = {"Referer": referer} if referer else {}
                async with session.request(
                    method, f"{base}{path}", headers=headers
                ) as resp:
                    await resp.read()
    finally:
        await runner.cleanup()

    records = [r for r in caplog.records if r.name == "aiohttp.access"]
    assert len(records) == len(requests)
    for record in records:
        blob = record.getMessage() + repr(vars(record))
        for secret in (
            "SECRETQUERY",
            "SECRETJOIN",
            "SECRETSHARE",
            "SECRETREFERER",
            "SECRETREVOKE",
            "SECRETINVITE",
        ):
            assert secret not in blob
    lines = [r.first_request_line for r in records]
    assert "GET /gfs/highlight_rtc/relay/inst/hl?token=*** HTTP/1.1" in lines
    assert "GET /join/*** HTTP/1.1" in lines
    assert "GET /highlight/inst/hl/*** HTTP/1.1" in lines
    assert "POST /gfs/highlight_tokens/***/revoke HTTP/1.1" in lines
    assert "DELETE /gfs/spaces/sp-1/invite/*** HTTP/1.1" in lines
    referers = [r.request_header["Referer"] for r in records]
    assert f"{base}/highlight/inst/hl/***" in referers
