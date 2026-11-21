"""Tests for POST /api/auth/token (standalone-mode login)."""

from __future__ import annotations

import pytest
from aiohttp.web_request import BaseRequest

from socialhome.login_throttle import (
    LOGIN_IP_MAX_FAILURES,
    LOGIN_USER_MAX_FAILURES,
)
from socialhome.platform.standalone import StandaloneAdapter


async def _seed_platform_user(client, username: str, password: str):
    hashed = StandaloneAdapter.hash_password(password)
    await client._db.enqueue(
        "INSERT INTO platform_users(username, display_name, password_hash) "
        "VALUES(?,?,?)",
        (username, username.title(), hashed),
    )


async def test_token_missing_credentials_422(client):
    r = await client.post("/api/auth/token", json={})
    assert r.status == 422


async def test_token_bad_json_400(client):
    r = await client.post(
        "/api/auth/token",
        data="oops",
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_token_unknown_user_401(client):
    r = await client.post(
        "/api/auth/token",
        json={"username": "ghost", "password": "x"},
    )
    assert r.status == 401


async def test_token_wrong_password_401(client):
    await _seed_platform_user(client, "alice", "correct")
    r = await client.post(
        "/api/auth/token",
        json={"username": "alice", "password": "wrong"},
    )
    assert r.status == 401


async def test_token_correct_password_returns_token(client):
    await _seed_platform_user(client, "alice", "hunter2")
    r = await client.post(
        "/api/auth/token",
        json={"username": "alice", "password": "hunter2"},
    )
    assert r.status == 200
    data = await r.json()
    token = data["token"]
    assert isinstance(token, str) and len(token) > 20


async def test_token_is_stored_as_sha256(client):
    await _seed_platform_user(client, "alice", "hunter2")
    r = await client.post(
        "/api/auth/token",
        json={"username": "alice", "password": "hunter2"},
    )
    data = await r.json()
    row = await client._db.fetchone(
        "SELECT token_hash FROM platform_tokens WHERE username=?",
        ("alice",),
    )
    import hashlib

    assert (
        row["token_hash"]
        == hashlib.sha256(
            data["token"].encode("utf-8"),
        ).hexdigest()
    )


def test_hash_password_produces_scrypt_envelope():
    h = StandaloneAdapter.hash_password("x")
    assert h.startswith("scrypt$")
    assert h.count("$") == 5


def test_verify_password_roundtrip():
    h = StandaloneAdapter.hash_password("secret")
    assert StandaloneAdapter._verify_password("secret", h) is True
    assert StandaloneAdapter._verify_password("nope", h) is False


def test_verify_password_rejects_non_scrypt_stored():
    assert StandaloneAdapter._verify_password("x", "bcrypt$whatever") is False


def _peer_from_header(monkeypatch) -> None:
    """Let a test pick the socket peer per request (``X-Test-Peer``)."""
    monkeypatch.setattr(
        BaseRequest,
        "remote",
        property(lambda self: self.headers.get("X-Test-Peer", "127.0.0.1")),
    )


async def _login(client, username: str, password: str, peer: str = "127.0.0.1"):
    return await client.post(
        "/api/auth/token",
        json={"username": username, "password": password},
        headers={"X-Test-Peer": peer},
    )


@pytest.mark.security
async def test_successful_logins_do_not_count(client, monkeypatch):
    """A family behind one proxy address setting up many phones: right
    passwords never use up the login budget."""
    _peer_from_header(monkeypatch)
    for name in ("anna", "ben", "cleo"):
        await _seed_platform_user(client, name, "right-password")
    for i in range(LOGIN_IP_MAX_FAILURES * 2):
        r = await _login(client, ("anna", "ben", "cleo")[i % 3], "right-password")
        assert r.status == 200, i


@pytest.mark.security
async def test_ten_failures_per_ip_lock_out_with_retry_after(client, monkeypatch):
    _peer_from_header(monkeypatch)
    for i in range(LOGIN_IP_MAX_FAILURES):
        r = await _login(client, f"ghost{i}", "wrong")
        assert r.status == 401
    r = await _login(client, "someone", "wrong")
    assert r.status == 429
    retry_after = int(r.headers["Retry-After"])
    assert 0 < retry_after <= 15 * 60
    body = await r.json()
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["retry_after"] == retry_after
    # Another client address is unaffected.
    r = await _login(client, "someone", "wrong", peer="198.51.100.7")
    assert r.status == 401


@pytest.mark.security
async def test_five_failures_per_username_lock_out_across_addresses(
    client, monkeypatch
):
    """Rotating client addresses doesn't buy more guesses at one name."""
    _peer_from_header(monkeypatch)
    await _seed_platform_user(client, "anna", "right-password")
    for i in range(LOGIN_USER_MAX_FAILURES):
        r = await _login(client, "anna", "wrong", peer=f"198.51.100.{i + 1}")
        assert r.status == 401
    r = await _login(client, "Anna", "wrong", peer="203.0.113.9")
    assert r.status == 429
    assert int(r.headers["Retry-After"]) > 0
    # Locked out means locked out: the right password waits too.
    r = await _login(client, "anna", "right-password", peer="203.0.113.10")
    assert r.status == 429


@pytest.mark.security
async def test_success_clears_that_usernames_failures(client, monkeypatch):
    _peer_from_header(monkeypatch)
    await _seed_platform_user(client, "anna", "right-password")
    for i in range(LOGIN_USER_MAX_FAILURES - 1):
        r = await _login(client, "anna", "wrong", peer=f"198.51.100.{i + 1}")
        assert r.status == 401
    r = await _login(client, "anna", "right-password", peer="198.51.100.50")
    assert r.status == 200
    for i in range(LOGIN_USER_MAX_FAILURES):
        r = await _login(client, "anna", "wrong", peer=f"203.0.113.{i + 1}")
        assert r.status == 401, i
    r = await _login(client, "anna", "wrong", peer="203.0.113.99")
    assert r.status == 429


@pytest.mark.security
async def test_unknown_and_known_usernames_answer_identically(client, monkeypatch):
    """No enumeration: the same answers, in the same order, for a name
    that exists and one that doesn't."""
    _peer_from_header(monkeypatch)
    await _seed_platform_user(client, "anna", "right-password")

    async def run(name: str, net: str) -> list[tuple[int, dict]]:
        seen = []
        for i in range(LOGIN_USER_MAX_FAILURES + 2):
            r = await _login(client, name, "wrong", peer=f"{net}.{i + 1}")
            body = await r.json()
            body["error"].pop("retry_after", None)
            seen.append((r.status, body))
        return seen

    assert await run("anna", "198.51.100") == await run("ghost", "203.0.113")


@pytest.mark.security
async def test_auth_token_rate_limit_buckets_ipv6_by_64(client, monkeypatch):
    """Rotating through addresses in one IPv6 /64 must not buy fresh login
    budgets — the /64 is one subscriber. Another /64 is unaffected."""
    _peer_from_header(monkeypatch)
    statuses = []
    for i in range(LOGIN_IP_MAX_FAILURES + 1):
        r = await _login(client, f"ghost{i}", "wrong", peer=f"2001:db8:1:2::{i + 1:x}")
        statuses.append(r.status)
    assert statuses[:-1] == [401] * LOGIN_IP_MAX_FAILURES
    assert statuses[-1] == 429
    r = await _login(client, "ghost-other", "wrong", peer="2001:db8:1:3::1")
    assert r.status == 401


async def test_missing_fields_do_not_count(client):
    for _ in range(LOGIN_IP_MAX_FAILURES * 2):
        r = await client.post("/api/auth/token", json={"username": "anna"})
        assert r.status == 422
    r = await client.post(
        "/api/auth/token", json={"username": "ghost", "password": "wrong"}
    )
    assert r.status == 401
