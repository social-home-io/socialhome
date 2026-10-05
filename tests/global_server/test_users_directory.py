"""GFS user-directory + picture upload tests (§Momentum-public)."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.csp import MEDIA_CSP
from socialhome.crypto import b64url_encode, generate_identity_keypair, sign_ed25519
from socialhome.global_server.app_keys import gfs_fed_repo_key
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


@pytest.fixture
async def client(tmp_dir):
    app = create_gfs_app(_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        tc._app = app
        yield tc


@pytest.fixture
async def author(client):
    kp = generate_identity_keypair()
    await client._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id="inst-author",
            display_name="Author",
            public_key=kp.public_key.hex(),
            inbox_url="http://author.example/wh",
            status="active",
        )
    )
    return kp


def _sign(kp, body: dict) -> dict:
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    out = dict(body)
    out["signature"] = b64url_encode(sign_ed25519(kp.private_key, canonical))
    return out


async def _register(client, author, *, bio: str | None = None):
    body = _sign(
        author,
        {
            "user_id": "u-author",
            "instance_id": "inst-author",
            "username": "alice",
            "display_name": "Alice In Bern",
            "bio": bio,
            "home_instance_pk": "ab" * 32,
        },
    )
    resp = await client.post("/gfs/moments/users/register", json=body)
    assert resp.status == 201, await resp.text()


# ── bio + directory enrichment ────────────────────────────────────────


async def test_register_persists_bio(client, author):
    await _register(client, author, bio="Mountain biker")
    listing = await (await client.get("/gfs/moments/users")).json()
    me = next(u for u in listing["users"] if u["user_id"] == "u-author")
    assert me["bio"] == "Mountain biker"
    assert me["display_name"] == "Alice In Bern"


async def test_register_rejects_oversize_bio(client, author):
    body = _sign(
        author,
        {
            "user_id": "u-author",
            "instance_id": "inst-author",
            "username": "alice",
            "display_name": "Alice",
            "bio": "x" * 281,
            "home_instance_pk": "ab" * 32,
        },
    )
    resp = await client.post("/gfs/moments/users/register", json=body)
    assert resp.status == 422


async def test_directory_supports_search_query(client, author):
    await _register(client, author, bio="Mountain biker")
    # A second user — only Alice should match "bern".
    other_kp = generate_identity_keypair()
    await client._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id="inst-bob",
            display_name="Bob",
            public_key=other_kp.public_key.hex(),
            inbox_url="http://bob.example",
            status="active",
        )
    )
    body = _sign(
        other_kp,
        {
            "user_id": "u-bob",
            "instance_id": "inst-bob",
            "username": "bob",
            "display_name": "Bob",
            "home_instance_pk": "cd" * 32,
        },
    )
    await client.post("/gfs/moments/users/register", json=body)

    resp = await client.get("/gfs/moments/users?q=bern")
    listing = await resp.json()
    assert {u["user_id"] for u in listing["users"]} == {"u-author"}


# ── per-user detail ───────────────────────────────────────────────────


async def test_user_detail_returns_follower_count(client, author):
    await _register(client, author)
    resp = await client.get("/gfs/moments/users/u-author")
    out = await resp.json()
    assert resp.status == 200
    assert out["user_id"] == "u-author"
    assert out["follower_count"] == 0


async def test_user_detail_404_for_unknown(client):
    resp = await client.get("/gfs/moments/users/u-nope")
    assert resp.status == 404


async def test_user_detail_html_renders_with_follow_cta(client, author):
    await _register(client, author, bio="Hello world")
    resp = await client.get("/moments/u-author")
    text = await resp.text()
    assert resp.status == 200
    assert resp.content_type == "text/html"
    assert "Alice In Bern" in text
    assert "Hello world" in text
    assert "Follow on your Social Home" in text
    assert "gfs=gfs-test" in text


async def test_user_detail_html_escapes_boot_payload_xss(client):
    """A registration whose instance_id carries ``</script>`` must not
    break out of the inline boot <script> block (stored XSS guard)."""
    evil = "inst</script><script>alert(1)</script>"
    kp = generate_identity_keypair()
    await client._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id=evil,
            display_name="Evil",
            public_key=kp.public_key.hex(),
            inbox_url="http://evil.example/wh",
            status="active",
        )
    )
    body = _sign(
        kp,
        {
            "user_id": "u-evil",
            "instance_id": evil,
            "username": "e",
            "display_name": "Evil",
            "home_instance_pk": "ab" * 32,
        },
    )
    resp = await client.post("/gfs/moments/users/register", json=body)
    assert resp.status == 201, await resp.text()
    page = await (await client.get("/moments/u-evil")).text()
    # The raw breakout must NOT appear; the boot payload carries the
    # escaped unicode form instead.
    assert "</script><script>alert(1)</script>" not in page
    assert "\\u003c/script\\u003e" in page


# ── picture upload + fetch ────────────────────────────────────────────


_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _signed_picture_body(kp, *, bytes_: bytes, mime: str = "image/png"):
    body = {
        "user_id": "u-author",
        "instance_id": "inst-author",
        "mime": mime,
        "bytes_b64": base64.b64encode(bytes_).decode("ascii"),
    }
    return _sign(kp, body)


async def test_picture_upload_and_fetch_round_trip(client, author):
    await _register(client, author)
    raw = _PNG_HEADER + b"abcdef"
    resp = await client.post(
        "/gfs/moments/users/u-author/picture",
        json=_signed_picture_body(author, bytes_=raw),
    )
    out = await resp.json()
    assert resp.status == 201, out
    assert out["digest"] == hashlib.sha256(raw).hexdigest()

    # Detail page now reports a digest.
    detail = await (await client.get("/gfs/moments/users/u-author")).json()
    assert detail["picture_digest"] == out["digest"]

    # Anon fetch streams bytes with strong-cache headers.
    pic = await client.get("/gfs/moments/users/u-author/picture")
    assert pic.status == 200
    assert pic.headers["Cache-Control"].startswith("public")
    assert pic.headers["ETag"] == f'"{out["digest"]}"'
    assert pic.headers["Content-Type"] == "image/png"
    assert pic.headers["Content-Security-Policy"] == MEDIA_CSP
    # The GFS hardening hook pairs the media CSP with ``nosniff``.
    assert pic.headers["X-Content-Type-Options"] == "nosniff"
    assert (await pic.read()) == raw


async def test_picture_upload_rejects_other_instance(client, author):
    """Only the author's home instance may push their avatar."""
    await _register(client, author)
    other_kp = generate_identity_keypair()
    await client._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id="inst-impersonator",
            display_name="X",
            public_key=other_kp.public_key.hex(),
            inbox_url="http://x.example",
            status="active",
        )
    )
    body = _sign(
        other_kp,
        {
            "user_id": "u-author",
            "instance_id": "inst-impersonator",
            "mime": "image/png",
            "bytes_b64": base64.b64encode(b"x").decode("ascii"),
        },
    )
    resp = await client.post("/gfs/moments/users/u-author/picture", json=body)
    assert resp.status == 403


async def test_picture_upload_rejects_oversize(client, author):
    await _register(client, author)
    huge = b"x" * (256 * 1024 + 1)
    resp = await client.post(
        "/gfs/moments/users/u-author/picture",
        json=_signed_picture_body(author, bytes_=huge),
    )
    assert resp.status == 413


async def test_picture_upload_rejects_bad_mime(client, author):
    await _register(client, author)
    resp = await client.post(
        "/gfs/moments/users/u-author/picture",
        json=_signed_picture_body(author, bytes_=b"x", mime="application/octet-stream"),
    )
    assert resp.status == 422


async def test_picture_fetch_404_when_unset(client, author):
    await _register(client, author)
    resp = await client.get("/gfs/moments/users/u-author/picture")
    assert resp.status == 404


# ── Strict CSP on the directory + detail pages ──────────────────────────


async def test_directory_and_detail_pages_have_strict_csp(client, author):
    await _register(client, author, bio="Hi")
    for path in ("/moments", "/moments/u-author"):
        resp = await client.get(path)
        assert resp.status == 200
        assert_strict_public_page(resp, await resp.text())
