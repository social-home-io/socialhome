"""Tests for the SH-side ``/api/moments/public/*`` routes.

Stubs :class:`MomentPublicService` so the auth + routing wiring is
exercised end-to-end without a real GFS round-trip.
"""

from __future__ import annotations

import pytest

from socialhome.app_keys import moment_public_service_key
from socialhome.csp import MEDIA_CSP
from socialhome.domain.moment_public import MomentPublicFollow, MomentPublicRegistration
from socialhome.services.moment_public_service import MomentPublicError

from .conftest import _auth


class _StubService:
    def __init__(self) -> None:
        self.regs: list[MomentPublicRegistration] = []
        self.follows: list[MomentPublicFollow] = []
        self.deregister_calls: list[tuple[str, str]] = []
        self.unfollow_calls: list[tuple[str, str, str]] = []
        self.set_default_calls: list[tuple[str, str, bool]] = []
        self.directory_calls: list = []
        self.directory_users: list[dict] = [
            {"user_id": "u-remote", "display_name": "Bob"}
        ]
        self.picture_calls: list[tuple[str, str]] = []
        self.picture_response: tuple[bytes, str, str] | None = (
            b"\x00",
            "image/webp",
            "abc",
        )
        self.register_raises: Exception | None = None
        self.follow_raises: Exception | None = None
        self.directory_raises: Exception | None = None
        self.picture_raises: Exception | None = None

    async def list_registrations(self, user_id):
        return [r for r in self.regs if r.user_id == user_id]

    async def register(self, *, user_id, gfs_id, default_share=True):
        if self.register_raises:
            raise self.register_raises
        reg = MomentPublicRegistration(
            user_id=user_id,
            gfs_id=gfs_id,
            registered_at="2026-05-06T12:00:00",
            default_share=default_share,
        )
        self.regs.append(reg)
        return reg

    async def deregister(self, *, user_id, gfs_id):
        self.deregister_calls.append((user_id, gfs_id))
        self.regs = [
            r for r in self.regs if not (r.user_id == user_id and r.gfs_id == gfs_id)
        ]

    async def set_default_share(self, *, user_id, gfs_id, default_share):
        self.set_default_calls.append((user_id, gfs_id, default_share))

    async def list_follows(self, follower_user_id):
        return [f for f in self.follows if f.follower_user_id == follower_user_id]

    async def follow(self, *, follower_user_id, gfs_id, followed_user_id):
        if self.follow_raises:
            raise self.follow_raises
        f = MomentPublicFollow(
            follower_user_id=follower_user_id,
            followed_user_id=followed_user_id,
            gfs_id=gfs_id,
            followed_instance_pk="ab" * 32,
            followed_username="bob",
            followed_display_name="Bob",
            created_at="2026-05-06T12:00:00",
        )
        self.follows.append(f)
        return f

    async def unfollow(self, *, follower_user_id, gfs_id, followed_user_id):
        self.unfollow_calls.append((follower_user_id, gfs_id, followed_user_id))

    async def fetch_directory(self, gfs_id, *, q=None):
        self.directory_calls.append((gfs_id, q) if q else gfs_id)
        if self.directory_raises:
            raise self.directory_raises
        return self.directory_users

    async def fetch_picture(self, gfs_id, user_id):
        self.picture_calls.append((gfs_id, user_id))
        if self.picture_raises:
            raise self.picture_raises
        return self.picture_response


@pytest.fixture
def stub(client):
    s = _StubService()
    client.app[moment_public_service_key] = s
    return s


# ── Registrations ───────────────────────────────────────────────────────


async def test_register_returns_201(client, stub):
    r = await client.post(
        "/api/moments/public/registrations",
        json={"gfs_id": "g1", "default_share": True},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["gfs_id"] == "g1" and body["default_share"] is True


async def test_register_missing_gfs_id_returns_400(client, stub):
    r = await client.post(
        "/api/moments/public/registrations",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 400


async def test_register_gfs_failure_maps_to_502(client, stub):
    stub.register_raises = MomentPublicError("GFS down")
    r = await client.post(
        "/api/moments/public/registrations",
        json={"gfs_id": "g1"},
        headers=_auth(client._tok),
    )
    assert r.status == 502


async def test_list_registrations_returns_caller_only(client, stub):
    await stub.register(user_id=client._uid, gfs_id="g1")
    r = await client.get(
        "/api/moments/public/registrations", headers=_auth(client._tok)
    )
    assert r.status == 200
    rows = await r.json()
    assert len(rows) == 1 and rows[0]["gfs_id"] == "g1"


async def test_deregister_returns_204(client, stub):
    r = await client.delete(
        "/api/moments/public/registrations/g1", headers=_auth(client._tok)
    )
    assert r.status == 204
    assert stub.deregister_calls == [(client._uid, "g1")]


async def test_patch_default_share_returns_204(client, stub):
    r = await client.patch(
        "/api/moments/public/registrations/g1",
        json={"default_share": False},
        headers=_auth(client._tok),
    )
    assert r.status == 204
    assert stub.set_default_calls == [(client._uid, "g1", False)]


async def test_patch_missing_default_share_returns_400(client, stub):
    r = await client.patch(
        "/api/moments/public/registrations/g1",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 400


async def test_register_unauthenticated_returns_401(client):
    r = await client.post("/api/moments/public/registrations", json={"gfs_id": "g1"})
    assert r.status == 401


# ── Follows ─────────────────────────────────────────────────────────────


async def test_follow_returns_201(client, stub):
    r = await client.post(
        "/api/moments/public/follows",
        json={"gfs_id": "g1", "followed_user_id": "u-remote"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["followed_user_id"] == "u-remote"
    assert body["followed_username"] == "bob"


async def test_follow_missing_args_returns_400(client, stub):
    r = await client.post(
        "/api/moments/public/follows",
        json={"gfs_id": "g1"},
        headers=_auth(client._tok),
    )
    assert r.status == 400


async def test_follow_gfs_failure_maps_to_502(client, stub):
    stub.follow_raises = MomentPublicError("GFS down")
    r = await client.post(
        "/api/moments/public/follows",
        json={"gfs_id": "g1", "followed_user_id": "u-remote"},
        headers=_auth(client._tok),
    )
    assert r.status == 502


async def test_list_follows_returns_caller_only(client, stub):
    await stub.follow(
        follower_user_id=client._uid, gfs_id="g1", followed_user_id="u-remote"
    )
    r = await client.get("/api/moments/public/follows", headers=_auth(client._tok))
    assert r.status == 200
    rows = await r.json()
    assert len(rows) == 1 and rows[0]["followed_user_id"] == "u-remote"


async def test_unfollow_returns_204(client, stub):
    r = await client.delete(
        "/api/moments/public/follows/g1/u-remote",
        headers=_auth(client._tok),
    )
    assert r.status == 204
    assert stub.unfollow_calls == [(client._uid, "g1", "u-remote")]


async def test_follow_unauthenticated_returns_401(client):
    r = await client.post(
        "/api/moments/public/follows",
        json={"gfs_id": "g1", "followed_user_id": "u-remote"},
    )
    assert r.status == 401


# ── Directory proxy ─────────────────────────────────────────────────────


async def test_directory_proxy_returns_users(client, stub):
    r = await client.get("/api/gfs/g1/moments/users", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body == {"users": [{"user_id": "u-remote", "display_name": "Bob"}]}
    assert stub.directory_calls == ["g1"]


async def test_directory_proxy_failure_maps_to_502(client, stub):
    stub.directory_raises = MomentPublicError("GFS down")
    r = await client.get("/api/gfs/g1/moments/users", headers=_auth(client._tok))
    assert r.status == 502


async def test_directory_proxy_passes_search_query(client, stub):
    r = await client.get("/api/gfs/g1/moments/users?q=bern", headers=_auth(client._tok))
    assert r.status == 200
    assert stub.directory_calls == [("g1", "bern")]


# ── Picture proxy ────────────────────────────────────────────────────────


async def test_picture_proxy_returns_bytes(client, stub):
    r = await client.get(
        "/api/gfs/g1/moments/users/u-remote/picture", headers=_auth(client._tok)
    )
    assert r.status == 200
    assert r.headers["ETag"] == '"abc"'
    assert r.headers["Cache-Control"].startswith("public")
    assert r.content_type == "image/webp"
    assert r.headers["Content-Security-Policy"] == MEDIA_CSP
    assert (await r.read()) == b"\x00"
    assert stub.picture_calls == [("g1", "u-remote")]


async def test_picture_proxy_never_reflects_a_script_capable_type(client, stub):
    """The ``Content-Type`` comes from the GFS's response — a hostile or
    broken GFS must not get ``text/html`` rendered on our origin."""
    stub.picture_response = (b"<script>alert(1)</script>", "text/html", "abc")
    r = await client.get(
        "/api/gfs/g1/moments/users/u-remote/picture", headers=_auth(client._tok)
    )
    assert r.status == 200
    assert r.content_type == "application/octet-stream"
    assert r.headers["Content-Disposition"].startswith("attachment;")
    assert r.headers["Content-Security-Policy"] == MEDIA_CSP


async def test_picture_proxy_404_when_unknown(client, stub):
    stub.picture_response = None
    r = await client.get(
        "/api/gfs/g1/moments/users/u-nope/picture", headers=_auth(client._tok)
    )
    assert r.status == 404


async def test_picture_proxy_failure_maps_to_502(client, stub):
    stub.picture_raises = MomentPublicError("GFS down")
    r = await client.get(
        "/api/gfs/g1/moments/users/u-remote/picture", headers=_auth(client._tok)
    )
    assert r.status == 502
