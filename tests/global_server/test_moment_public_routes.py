"""End-to-end tests for the GFS ``/gfs/moments/users/*`` and
``/gfs/moments/*`` HTTP routes (§Momentum-public)."""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import b64url_encode, generate_identity_keypair, sign_ed25519
from socialhome.global_server.app_keys import gfs_fed_repo_key
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app


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
            status="active",
        )
    )
    return kp


@pytest.fixture
async def follower(client):
    kp = generate_identity_keypair()
    await client._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id="inst-follower",
            display_name="Follower",
            public_key=kp.public_key.hex(),
            status="active",
        )
    )
    return kp


def _sign(kp, body: dict) -> dict:
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    body = dict(body)
    body["signature"] = b64url_encode(sign_ed25519(kp.private_key, canonical))
    return body


# ── Register / deregister ──────────────────────────────────────────────


async def test_register_creates_directory_entry(client, author):
    body = _sign(
        author,
        {
            "user_id": "u-author",
            "instance_id": "inst-author",
            "username": "alice",
            "display_name": "Alice",
            "home_instance_pk": "ab" * 32,
        },
    )
    resp = await client.post("/gfs/moments/users/register", json=body)
    assert resp.status == 201
    out = await resp.json()
    assert out["user_id"] == "u-author"
    # Directory now lists the user.
    listing = await (await client.get("/gfs/moments/users")).json()
    assert any(u["user_id"] == "u-author" for u in listing["users"])


async def test_register_missing_home_pk_returns_422(client, author):
    body = _sign(
        author,
        {
            "user_id": "u-author",
            "instance_id": "inst-author",
            "username": "alice",
            "display_name": "Alice",
        },
    )
    resp = await client.post("/gfs/moments/users/register", json=body)
    assert resp.status == 422


async def test_register_instance_mismatch_returns_403(client, author):
    body = _sign(
        author,
        {
            "user_id": "u-author",
            "instance_id": "inst-other",
            "username": "alice",
            "display_name": "Alice",
            "home_instance_pk": "ab" * 32,
        },
    )
    resp = await client.post("/gfs/moments/users/register", json=body)
    assert resp.status == 403


async def test_deregister_removes_directory_entry(client, author):
    body = _sign(
        author,
        {
            "user_id": "u-author",
            "instance_id": "inst-author",
            "username": "alice",
            "display_name": "Alice",
            "home_instance_pk": "ab" * 32,
        },
    )
    assert (await client.post("/gfs/moments/users/register", json=body)).status == 201
    resp = await client.post(
        "/gfs/moments/users/u-author/deregister",
        json=_sign(author, {"user_id": "u-author", "instance_id": "inst-author"}),
    )
    assert resp.status == 200
    out = await resp.json()
    assert out == {"deregistered": True}


async def test_deregister_unknown_user_returns_404(client, author):
    resp = await client.post(
        "/gfs/moments/users/u-ghost/deregister",
        json=_sign(author, {"user_id": "u-ghost", "instance_id": "inst-author"}),
    )
    assert resp.status == 404


# ── Follow / unfollow ──────────────────────────────────────────────────


async def test_follow_then_unfollow(client, author, follower):
    # Author registers.
    await client.post(
        "/gfs/moments/users/register",
        json=_sign(
            author,
            {
                "user_id": "u-author",
                "instance_id": "inst-author",
                "username": "alice",
                "display_name": "Alice",
                "home_instance_pk": "ab" * 32,
            },
        ),
    )
    # Follower follows. The auth middleware reads ``instance_id`` from
    # the body, so we send it alongside the role-named field.
    resp = await client.post(
        "/gfs/moments/users/u-author/follow",
        json=_sign(
            follower,
            {
                "instance_id": "inst-follower",
                "follower_user_id": "u-follower",
                "follower_instance_id": "inst-follower",
            },
        ),
    )
    assert resp.status == 201
    out = await resp.json()
    assert out["user"]["user_id"] == "u-author"
    assert out["follow"]["followed_user_id"] == "u-author"
    # Unfollow.
    resp = await client.post(
        "/gfs/moments/users/u-author/unfollow",
        json=_sign(
            follower,
            {
                "instance_id": "inst-follower",
                "follower_user_id": "u-follower",
                "follower_instance_id": "inst-follower",
            },
        ),
    )
    assert resp.status == 200
    out = await resp.json()
    assert out["unfollowed"] is True


async def test_follow_unknown_author_returns_404(client, follower):
    resp = await client.post(
        "/gfs/moments/users/u-ghost/follow",
        json=_sign(
            follower,
            {
                "instance_id": "inst-follower",
                "follower_user_id": "u-follower",
                "follower_instance_id": "inst-follower",
            },
        ),
    )
    assert resp.status == 404


async def test_follow_missing_follower_id_returns_422(client, follower):
    resp = await client.post(
        "/gfs/moments/users/u-author/follow",
        json=_sign(
            follower,
            {
                "instance_id": "inst-follower",
                "follower_instance_id": "inst-follower",
            },
        ),
    )
    assert resp.status == 422


# ── Publish / delete ───────────────────────────────────────────────────


async def test_publish_returns_delivered_count(client, author):
    # Author registers but has no followers — delivered=0.
    await client.post(
        "/gfs/moments/users/register",
        json=_sign(
            author,
            {
                "user_id": "u-author",
                "instance_id": "inst-author",
                "username": "alice",
                "display_name": "Alice",
                "home_instance_pk": "ab" * 32,
            },
        ),
    )
    resp = await client.post(
        "/gfs/moments/publish",
        json=_sign(
            author,
            {
                "instance_id": "inst-author",
                "moment_id": "m-1",
                "author_user_id": "u-author",
                "content": "hi",
            },
        ),
    )
    assert resp.status == 200
    out = await resp.json()
    assert out == {"delivered": 0}


async def test_delete_returns_delivered_count(client, author):
    resp = await client.post(
        "/gfs/moments/delete",
        json=_sign(
            author,
            {
                "instance_id": "inst-author",
                "moment_id": "m-1",
                "author_user_id": "u-author",
            },
        ),
    )
    assert resp.status == 200


# ── Public discovery ───────────────────────────────────────────────────


async def test_users_html_directory_renders(client, author):
    await client.post(
        "/gfs/moments/users/register",
        json=_sign(
            author,
            {
                "user_id": "u-author",
                "instance_id": "inst-author",
                "username": "alice",
                "display_name": "Alice",
                "home_instance_pk": "ab" * 32,
            },
        ),
    )
    resp = await client.get("/moments")
    assert resp.status == 200
    assert resp.content_type == "text/html"
    text = await resp.text()
    # SPA shell: actual rendering happens client-side via the
    # static JS bundle, not in the HTML response.
    assert "Public Momentum" in text
    assert "/static/users_directory.js" in text


async def test_users_html_directory_empty_state(client):
    resp = await client.get("/moments")
    assert resp.status == 200
    text = await resp.text()
    assert "No registered users yet" in text


async def test_users_json_directory_empty(client):
    resp = await client.get("/gfs/moments/users")
    assert resp.status == 200
    out = await resp.json()
    assert out["users"] == []
    assert out["count"] == 0
