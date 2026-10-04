"""Open sign-up on the GFS: ``POST /gfs/signup-token`` + the signed capability.

An operator who turns ``[policy] open_signup`` on lets the GFS hand out a
single-use pairing token over JSON, so a household can connect during
onboarding without scanning a QR code. Off by default; when off the endpoint
answers one uniform 404. Per-IP and global rate limits shed floods, and the
token is the same short-lived, single-use one the landing-page QR carries.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.capabilities_sig import verify_capabilities
from socialhome.global_server import public
from socialhome.global_server.app_keys import gfs_admin_repo_key, gfs_db_key
from socialhome.global_server.config import EXAMPLE_TOML, GfsConfig
from socialhome.global_server.server import create_gfs_app


def _cfg(tmp_path, *, open_signup: bool) -> GfsConfig:
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_path),
        instance_id="gfs-signup",
        open_signup=open_signup,
    )


@pytest.fixture
async def open_client(tmp_path):
    app = create_gfs_app(_cfg(tmp_path, open_signup=True))
    async with TestClient(TestServer(app)) as tc:
        yield tc


@pytest.fixture
async def closed_client(tmp_path):
    app = create_gfs_app(_cfg(tmp_path, open_signup=False))
    async with TestClient(TestServer(app)) as tc:
        yield tc


def _ip(n: int) -> dict[str, str]:
    """A distinct client address per call (loopback is a trusted proxy)."""
    return {"X-Forwarded-For": f"198.51.100.{n}"}


def _register_body(token: str, instance_id: str = "inst-signup") -> dict:
    return {
        "token": token,
        "instance_id": instance_id,
        "public_key": "aa" * 32,
        "inbox_url": "https://home.example.com/federation/inbox",
        "display_name": "Home",
    }


def test_open_signup_is_off_by_default():
    assert GfsConfig().open_signup is False


async def test_signup_token_is_404_when_open_signup_is_off(closed_client):
    resp = await closed_client.post("/gfs/signup-token", headers=_ip(1))
    assert resp.status == 404
    assert await resp.json() == {"error": "not_found"}


async def test_signup_token_issues_a_token_that_registers(open_client):
    resp = await open_client.post("/gfs/signup-token", headers=_ip(2))
    assert resp.status == 200
    body = await resp.json()
    assert set(body) == {"token", "expires_in"}
    assert body["expires_in"] == public.PAIR_TOKEN_TTL_SECONDS
    assert len(body["token"]) >= 32
    reg = await open_client.post("/gfs/register", json=_register_body(body["token"]))
    assert reg.status == 200
    assert (await reg.json())["status"] == "registered"


async def test_signup_token_is_single_use(open_client):
    token = (
        await (await open_client.post("/gfs/signup-token", headers=_ip(3))).json()
    )["token"]
    first = await open_client.post("/gfs/register", json=_register_body(token, "a"))
    assert first.status == 200
    again = await open_client.post("/gfs/register", json=_register_body(token, "b"))
    assert again.status == 401
    assert (await again.json())["error"] == "invalid_or_expired_token"


async def test_signup_token_expires(open_client):
    token = (
        await (await open_client.post("/gfs/signup-token", headers=_ip(4))).json()
    )["token"]
    db = open_client.server.app[gfs_db_key]
    old = int(time.time()) - public.PAIR_TOKEN_TTL_SECONDS - 5
    await db.enqueue(
        "UPDATE gfs_pair_tokens SET created_at=? WHERE token=?",
        (old, token),
    )
    resp = await open_client.post("/gfs/register", json=_register_body(token))
    assert resp.status == 401


async def test_signup_registration_is_pending_when_auto_accept_is_off(open_client):
    app = open_client.server.app
    await app[gfs_admin_repo_key].set_config("auto_accept_clients", "0")
    token = (
        await (await open_client.post("/gfs/signup-token", headers=_ip(5))).json()
    )["token"]
    reg = await open_client.post("/gfs/register", json=_register_body(token))
    assert (await reg.json())["status"] == "pending"


async def test_signup_token_one_per_address_window(open_client):
    """The token service keeps its per-address interval: a second request from
    the same address right away is shed with 429 + Retry-After."""
    first = await open_client.post("/gfs/signup-token", headers=_ip(6))
    assert first.status == 200
    second = await open_client.post("/gfs/signup-token", headers=_ip(6))
    assert second.status == 429
    assert second.headers.get("Retry-After") == str(public.PAIR_TOKEN_MIN_INTERVAL)


async def test_signup_token_per_ip_flood_is_rate_limited(open_client):
    """The middleware sheds a per-IP flood before any database work."""
    statuses = [
        (await open_client.post("/gfs/signup-token", headers=_ip(7))).status
        for _ in range(public.SIGNUP_MAX_PER_MINUTE + 1)
    ]
    assert statuses[-1] == 429
    resp = await open_client.post("/gfs/signup-token", headers=_ip(7))
    assert resp.status == 429
    assert resp.headers.get("Retry-After") == "60"
    assert (await resp.json()) == {"error": "rate_limited"}


async def test_signup_token_global_rate_limit(tmp_path, monkeypatch):
    """Many addresses together hit the global budget — the sybil lever that
    does not depend on believing a client address."""
    monkeypatch.setattr(public, "SIGNUP_MAX_PER_MINUTE_GLOBAL", 3)
    app = create_gfs_app(_cfg(tmp_path, open_signup=True))
    async with TestClient(TestServer(app)) as tc:
        ok = [
            (await tc.post("/gfs/signup-token", headers=_ip(20 + i))).status
            for i in range(3)
        ]
        assert ok == [200, 200, 200]
        resp = await tc.post("/gfs/signup-token", headers=_ip(40))
        assert resp.status == 429


async def test_signup_token_rejects_get(open_client):
    resp = await open_client.get("/gfs/signup-token", headers=_ip(8))
    assert resp.status == 405


async def test_info_advertises_open_signup_signed(open_client):
    body = await (await open_client.get("/gfs/info")).json()
    assert body["capabilities"]["open_signup"] is True
    assert verify_capabilities(
        body["public_key"],
        body["gfs_instance_id"],
        body["capabilities"],
        body["capabilities_sig"],
        body["capabilities_sig_suite"],
    )


async def test_info_says_open_signup_false_when_off(closed_client):
    body = await (await closed_client.get("/gfs/info")).json()
    assert body["capabilities"]["open_signup"] is False


async def test_register_path_unchanged_with_open_signup(open_client):
    """Turning open sign-up on does not loosen ``/gfs/register``: no token is
    still a 400, a made-up one still a 401."""
    no_tok = _register_body("")
    no_tok.pop("token")
    assert (await open_client.post("/gfs/register", json=no_tok)).status == 400
    bad = await open_client.post("/gfs/register", json=_register_body("never-minted"))
    assert bad.status == 401


def test_open_signup_loads_from_toml_and_env(tmp_path, monkeypatch):
    p = tmp_path / "global_server.toml"
    p.write_text(
        '[server]\nbase_url = "https://gfs.example.com"\n[policy]\nopen_signup = true\n'
    )
    assert GfsConfig.from_toml(p).open_signup is True
    monkeypatch.setenv("GFS_OPEN_SIGNUP", "false")
    assert GfsConfig.load(p).open_signup is False
    monkeypatch.setenv("GFS_OPEN_SIGNUP", "1")
    assert replace(GfsConfig(), open_signup=False)._with_env_overrides().open_signup


def test_example_toml_documents_open_signup():
    assert "open_signup = false" in EXAMPLE_TOML


async def test_one_signup_token_registers_exactly_one_household(open_client):
    """Regression (review R1): consuming a token was a SELECT then a separate
    UPDATE, so 20 concurrent registers on ONE token all succeeded — one
    sign-up token could mint unboundedly many households. Each request comes
    from its own address so the register rate limit doesn't mask the race."""
    resp = await open_client.post("/gfs/signup-token", headers=_ip(9))
    token = (await resp.json())["token"]
    resps = await asyncio.gather(
        *[
            open_client.post(
                "/gfs/register",
                json=_register_body(token, f"sybil-{i}"),
                headers=_ip(100 + i),
            )
            for i in range(20)
        ]
    )
    statuses = sorted(r.status for r in resps)
    assert statuses.count(200) == 1
    assert statuses.count(401) == 19


async def test_register_is_rate_limited_per_ip(open_client):
    """``/gfs/register`` sheds a per-address flood before any token lookup."""
    statuses = [
        (
            await open_client.post(
                "/gfs/register", json=_register_body("never-minted"), headers=_ip(60)
            )
        ).status
        for _ in range(public.REGISTER_MAX_PER_MINUTE + 1)
    ]
    assert statuses[:-1] == [401] * public.REGISTER_MAX_PER_MINUTE
    assert statuses[-1] == 429
    # Another address is unaffected.
    other = await open_client.post(
        "/gfs/register", json=_register_body("never-minted"), headers=_ip(61)
    )
    assert other.status == 401
