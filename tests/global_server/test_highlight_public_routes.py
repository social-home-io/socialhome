"""Tests for the GFS public highlight routes (§highlights_public).

Exercises both surfaces:

* The public landing page (no auth) — 200 / 410 / 503 paths.
* The Ed25519-signed wire endpoints (publish / mint / revoke /
  unpublish) — full sign + verify path, mirroring
  ``test_federation`` so the auth middleware is live.
"""

from __future__ import annotations

import base64
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from socialhome.global_server.app_keys import (
    gfs_fed_repo_key,
    gfs_highlight_pub_service_key,
    gfs_ws_registry_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app

from .conftest import assert_strict_public_page


def _config(tmp_dir):
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-test",
    )


def _make_keypair() -> tuple[bytes, str]:
    """Return ``(private_seed, public_key_hex)`` for a fresh Ed25519 pair."""
    sk = Ed25519PrivateKey.generate()
    seed = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pk_hex = (
        sk.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    return seed, pk_hex


def _sign(seed: bytes, body: dict) -> dict:
    """Return ``body`` with an appended urlsafe-base64 Ed25519 signature."""
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sk = Ed25519PrivateKey.from_private_bytes(seed)
    sig = base64.urlsafe_b64encode(sk.sign(canonical)).rstrip(b"=").decode("ascii")
    return {**body, "signature": sig}


@pytest.fixture
async def keypair():
    return _make_keypair()


@pytest.fixture
async def client(tmp_dir, keypair):
    seed, pk_hex = keypair
    app = create_gfs_app(_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        tc._app = app
        tc._seed = seed
        # Seed the author instance — both the FK on
        # ``gfs_highlight_publications.instance_id`` and the signed-wire
        # auth middleware look it up.
        await app[gfs_fed_repo_key].upsert_instance(
            ClientInstance(
                instance_id="inst-author",
                display_name="Author",
                public_key=pk_hex,
                inbox_url="http://author/wh",
                status="active",
            )
        )
        yield tc


def _mark_author_online(app, instance_id: str = "inst-author") -> None:
    """Stub the WS registry as if the author were connected."""

    class _StubWs:
        closed = False

    app[gfs_ws_registry_key]._by_instance[instance_id] = _StubWs()


# ── Public landing page ─────────────────────────────────────────────────


async def test_landing_returns_200_when_token_active_and_author_online(client):
    app = client._app
    registry = app[gfs_highlight_pub_service_key]
    tok, url = await registry.record_publish(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="",
    )
    _mark_author_online(app)
    # ``url`` points at the configured base_url; we hit the local
    # path-only counterpart against the test client.
    path = f"/highlight/inst-author/s-1/{tok.token}"
    assert path in url

    resp = await client.get(path)
    assert resp.status == 200
    text = await resp.text()
    assert "Highlight coming soon" in text or "<html" in text
    # ``<base href>`` anchors the relative ``static/...`` script src to
    # the GFS root rather than the deep document URL — keeps the page
    # portable to a path-prefixed deployment.
    assert "<base href='/'>" in text
    assert "src='static/highlight_public_viewer.js'" in text
    assert "src='/static/highlight_public_viewer.js'" not in text


async def test_landing_returns_410_when_token_revoked(client):
    app = client._app
    registry = app[gfs_highlight_pub_service_key]
    tok, _url = await registry.record_publish(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="",
    )
    await registry.revoke_token(tok.token, "inst-author")
    _mark_author_online(app)
    resp = await client.get(f"/highlight/inst-author/s-1/{tok.token}")
    assert resp.status == 410


async def test_landing_returns_410_when_unpublished(client):
    app = client._app
    registry = app[gfs_highlight_pub_service_key]
    tok, _url = await registry.record_publish(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="",
    )
    await registry.remove_publish("s-1", "inst-author")
    resp = await client.get(f"/highlight/inst-author/s-1/{tok.token}")
    assert resp.status == 410


async def test_landing_returns_410_when_url_mixes_wrong_highlight_id(client):
    app = client._app
    registry = app[gfs_highlight_pub_service_key]
    tok, _url = await registry.record_publish(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="",
    )
    _mark_author_online(app)
    # Same token, different highlight_id in the path — must not resolve.
    resp = await client.get(f"/highlight/inst-author/s-OTHER/{tok.token}")
    assert resp.status == 410


async def test_landing_returns_503_when_author_offline(client):
    app = client._app
    registry = app[gfs_highlight_pub_service_key]
    tok, _url = await registry.record_publish(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="",
    )
    # Don't mark author online.
    resp = await client.get(f"/highlight/inst-author/s-1/{tok.token}")
    assert resp.status == 503


async def test_landing_returns_410_for_unknown_token(client):
    resp = await client.get("/highlight/inst-author/s-1/never-issued")
    assert resp.status == 410


# ── Signed-wire endpoints ───────────────────────────────────────────────


async def test_publish_endpoint_rejects_missing_signature(client):
    """No signature on the body → 401 from `_rtc_authenticate`."""
    resp = await client.post(
        "/gfs/highlights/s-1/publish",
        json={"instance_id": "inst-author", "highlight_id": "s-1", "expires_at": 1},
    )
    assert resp.status in (401, 403)


async def test_publish_signed_creates_publication_and_returns_url(client):
    body = _sign(
        client._seed,
        {
            "instance_id": "inst-author",
            "highlight_id": "s-1",
            "expires_at": 10_000_000_000,
            "label": "twitter",
        },
    )
    resp = await client.post("/gfs/highlights/s-1/publish", json=body)
    assert resp.status == 201
    data = await resp.json()
    assert data["token"]
    assert data["url"].startswith("http://gfs.test/highlight/inst-author/s-1/")
    assert data["label"] == "twitter"


async def test_publish_rejects_highlight_id_mismatch(client):
    """URL highlight_id and body highlight_id must agree."""
    body = _sign(
        client._seed,
        {
            "instance_id": "inst-author",
            "highlight_id": "s-OTHER",
            "expires_at": 10_000_000_000,
        },
    )
    resp = await client.post("/gfs/highlights/s-1/publish", json=body)
    assert resp.status == 422


async def test_publish_rejects_invalid_expires_at(client):
    body = _sign(
        client._seed,
        {
            "instance_id": "inst-author",
            "highlight_id": "s-1",
            "expires_at": 0,
        },
    )
    resp = await client.post("/gfs/highlights/s-1/publish", json=body)
    assert resp.status == 422


async def test_mint_extra_token_under_existing_pub(client):
    # First publish creates the parent + first token.
    body = _sign(
        client._seed,
        {
            "instance_id": "inst-author",
            "highlight_id": "s-1",
            "expires_at": 10_000_000_000,
        },
    )
    await client.post("/gfs/highlights/s-1/publish", json=body)
    # Then a second token under the same publication.
    extra = _sign(
        client._seed,
        {"instance_id": "inst-author", "label": "email"},
    )
    resp = await client.post("/gfs/highlights/s-1/tokens", json=extra)
    assert resp.status == 201
    data = await resp.json()
    assert data["label"] == "email"


async def test_mint_token_without_publish_returns_404(client):
    extra = _sign(
        client._seed,
        {"instance_id": "inst-author", "label": "email"},
    )
    resp = await client.post("/gfs/highlights/s-no-pub/tokens", json=extra)
    assert resp.status == 404


async def test_revoke_signed_drops_token(client):
    publish = _sign(
        client._seed,
        {
            "instance_id": "inst-author",
            "highlight_id": "s-1",
            "expires_at": 10_000_000_000,
        },
    )
    publish_resp = await client.post("/gfs/highlights/s-1/publish", json=publish)
    token = (await publish_resp.json())["token"]

    revoke = _sign(client._seed, {"instance_id": "inst-author", "token": token})
    resp = await client.post(f"/gfs/highlight_tokens/{token}/revoke", json=revoke)
    assert resp.status == 200


async def test_revoke_unknown_token_returns_404(client):
    revoke = _sign(
        client._seed,
        {"instance_id": "inst-author", "token": "nope"},
    )
    resp = await client.post("/gfs/highlight_tokens/nope/revoke", json=revoke)
    assert resp.status == 404


async def test_unpublish_signed_removes_publication(client):
    publish = _sign(
        client._seed,
        {
            "instance_id": "inst-author",
            "highlight_id": "s-1",
            "expires_at": 10_000_000_000,
        },
    )
    await client.post("/gfs/highlights/s-1/publish", json=publish)

    unpublish = _sign(
        client._seed,
        {"instance_id": "inst-author", "highlight_id": "s-1"},
    )
    resp = await client.post("/gfs/highlights/s-1/unpublish", json=unpublish)
    assert resp.status == 200


async def test_unpublish_unknown_returns_404(client):
    unpublish = _sign(
        client._seed,
        {"instance_id": "inst-author", "highlight_id": "s-missing"},
    )
    resp = await client.post(
        "/gfs/highlights/s-missing/unpublish",
        json=unpublish,
    )
    assert resp.status == 404


# ── Strict CSP on the viewer + its fallback pages ───────────────────────


async def test_viewer_and_fallback_pages_have_strict_csp(client):
    app = client._app
    registry = app[gfs_highlight_pub_service_key]
    tok, _url = await registry.record_publish(
        highlight_id="s-csp",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="",
    )
    path = f"/highlight/inst-author/s-csp/{tok.token}"
    # 503 — author offline.
    resp = await client.get(path)
    assert resp.status == 503
    assert_strict_public_page(resp, await resp.text())
    # 200 — the viewer.
    _mark_author_online(app)
    resp = await client.get(path)
    assert resp.status == 200
    assert_strict_public_page(resp, await resp.text())
    # 410 — unknown token.
    resp = await client.get("/highlight/inst-author/s-csp/nope")
    assert resp.status == 410
    assert_strict_public_page(resp, await resp.text())


async def test_gone_page_escapes_the_url_segments(client):
    """The 410 page echoes ``instance_id`` / ``highlight_id`` from the URL
    path — percent-decoded by the router, so attacker-chosen. They must be
    HTML-escaped (reflected markup on the GFS origin otherwise)."""
    resp = await client.get(
        "/highlight/%3Cimg%20src%3Dx%3E/%3Cb%3Ehl%3C%2Fb%3E/no-such-token"
    )
    assert resp.status == 410
    text = await resp.text()
    assert "<img src=x>" not in text
    assert "<b>hl</b>" not in text
    assert "&lt;img src=x&gt;" in text
    assert_strict_public_page(resp, text)


async def test_unavailable_page_escapes_the_url_segments(client):
    app = client._app
    registry = app[gfs_highlight_pub_service_key]
    # A publication whose ids carry markup reaches the 503 branch (author
    # offline) with those ids echoed into the body.
    tok, _url = await registry.record_publish(
        highlight_id="<b>hl</b>",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="",
    )
    resp = await client.get(f"/highlight/inst-author/%3Cb%3Ehl%3C%2Fb%3E/{tok.token}")
    assert resp.status == 503
    text = await resp.text()
    assert "<b>hl</b>" not in text
    assert "&lt;b&gt;hl&lt;/b&gt;" in text
