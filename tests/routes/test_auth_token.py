"""Tests for POST /api/auth/token (standalone-mode login)."""

from __future__ import annotations


from social_home.platform.standalone import StandaloneAdapter


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


async def test_auth_token_rate_limit_returns_429_after_budget(client):
    """§25.7 — a 6th login attempt from the same IP returns 429."""
    from social_home.routes.users import AUTH_TOKEN_RATE_LIMIT

    # Burn the budget with bad credentials — all should 401, then 429.
    for _ in range(AUTH_TOKEN_RATE_LIMIT):
        r = await client.post(
            "/api/auth/token",
            json={"username": "ghost", "password": "wrong"},
        )
        assert r.status == 401
    r = await client.post(
        "/api/auth/token",
        json={"username": "ghost", "password": "wrong"},
    )
    assert r.status == 429
