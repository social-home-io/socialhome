"""Integration tests for the admin-issued password-reset flow.

* ``POST /api/admin/users/{username}/issue-password-reset`` (admin)
* ``POST /api/auth/redeem-password-reset`` (public)
"""

from __future__ import annotations

import pytest

from socialhome.login_throttle import (
    LOGIN_USER_MAX_FAILURES,
    RESET_IP_MAX_FAILURES,
)
from socialhome.platform.standalone import StandaloneAdapter


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_platform_user(client, username: str, password: str):
    hashed = StandaloneAdapter.hash_password(password)
    await client._db.enqueue(
        "INSERT INTO platform_users(username, display_name, password_hash) "
        "VALUES(?,?,?)",
        (username, username.title(), hashed),
    )
    # The route uses adapter.users.get(username) which checks the
    # ``users`` table — seed there too.
    await client._db.enqueue(
        "INSERT INTO users(user_id, username, display_name, is_admin) VALUES(?,?,?,?)",
        (username, username, username.title(), 0),
    )


# ── Admin issue endpoint ─────────────────────────────────────────────────


async def test_issue_returns_token_and_expires_at(client):
    await _seed_platform_user(client, "alice", "hunter2")
    r = await client.post(
        "/api/admin/users/alice/issue-password-reset",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert isinstance(body["token"], str) and len(body["token"]) > 32
    assert body["username"] == "alice"
    assert body["expires_at"]


async def test_issue_unknown_user_404(client):
    r = await client.post(
        "/api/admin/users/ghost/issue-password-reset",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_issue_non_admin_403(client):
    await client._db.enqueue(
        "UPDATE users SET is_admin=0 WHERE username='admin'",
        (),
    )
    r = await client.post(
        "/api/admin/users/alice/issue-password-reset",
        headers=_auth(client._tok),
    )
    assert r.status == 403


# ── Public redeem endpoint ───────────────────────────────────────────────


async def test_redeem_rotates_password(client):
    await _seed_platform_user(client, "alice", "old-pass-123")
    issue = await client.post(
        "/api/admin/users/alice/issue-password-reset",
        headers=_auth(client._tok),
    )
    token = (await issue.json())["token"]
    # Redeem with the new password.
    r = await client.post(
        "/api/auth/redeem-password-reset",
        json={"token": token, "new_password": "fresh-pass-9"},
    )
    assert r.status == 204
    # Old password no longer works.
    r1 = await client.post(
        "/api/auth/token",
        json={"username": "alice", "password": "old-pass-123"},
    )
    assert r1.status == 401
    # New password works.
    r2 = await client.post(
        "/api/auth/token",
        json={"username": "alice", "password": "fresh-pass-9"},
    )
    assert r2.status == 200


async def test_redeem_already_used_410(client):
    await _seed_platform_user(client, "alice", "x")
    issue = await client.post(
        "/api/admin/users/alice/issue-password-reset",
        headers=_auth(client._tok),
    )
    token = (await issue.json())["token"]
    await client.post(
        "/api/auth/redeem-password-reset",
        json={"token": token, "new_password": "first-pass-1"},
    )
    r = await client.post(
        "/api/auth/redeem-password-reset",
        json={"token": token, "new_password": "second-pass-2"},
    )
    assert r.status == 410


async def test_redeem_unknown_token_410(client):
    r = await client.post(
        "/api/auth/redeem-password-reset",
        json={"token": "not-a-real-token", "new_password": "abcdefgh"},
    )
    assert r.status == 410


async def test_redeem_short_password_422(client):
    await _seed_platform_user(client, "alice", "x")
    issue = await client.post(
        "/api/admin/users/alice/issue-password-reset",
        headers=_auth(client._tok),
    )
    token = (await issue.json())["token"]
    r = await client.post(
        "/api/auth/redeem-password-reset",
        json={"token": token, "new_password": "short"},
    )
    assert r.status == 422


async def test_redeem_missing_fields_422(client):
    r = await client.post(
        "/api/auth/redeem-password-reset",
        json={},
    )
    assert r.status == 422


# ── Redeem limits: wrong tokens only ─────────────────────────────────────


async def _issue(client, username: str) -> str:
    r = await client.post(
        f"/api/admin/users/{username}/issue-password-reset",
        headers=_auth(client._tok),
    )
    return (await r.json())["token"]


async def _redeem(client, token: str, password: str = "fresh-pass-9"):
    return await client.post(
        "/api/auth/redeem-password-reset",
        json={"token": token, "new_password": password},
    )


@pytest.mark.security
async def test_redeem_wrong_tokens_lock_out_with_retry_after(client):
    for i in range(RESET_IP_MAX_FAILURES):
        r = await _redeem(client, f"not-a-real-token-{i}")
        assert r.status == 410
    r = await _redeem(client, "not-a-real-token-x")
    assert r.status == 429
    retry_after = int(r.headers["Retry-After"])
    assert 0 < retry_after <= 15 * 60
    body = await r.json()
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["retry_after"] == retry_after


@pytest.mark.security
async def test_redeem_successes_do_not_count(client):
    """An admin resetting a room of people from one address: every right
    link works, however many."""
    for i in range(RESET_IP_MAX_FAILURES + 2):
        name = f"user{i}"
        await _seed_platform_user(client, name, "old-pass-123")
        r = await _redeem(client, await _issue(client, name))
        assert r.status == 204, i


async def test_redeem_short_password_does_not_count(client):
    for _ in range(RESET_IP_MAX_FAILURES + 2):
        r = await _redeem(client, "whatever", password="short")
        assert r.status == 422
    r = await _redeem(client, "not-a-real-token")
    assert r.status == 410


@pytest.mark.security
async def test_redeem_lifts_the_users_login_lockout(client):
    """Someone who locked themselves out and got a reset link can sign in
    with the new password straight away."""
    await _seed_platform_user(client, "alice", "old-pass-123")
    for _ in range(LOGIN_USER_MAX_FAILURES):
        r = await client.post(
            "/api/auth/token", json={"username": "alice", "password": "nope"}
        )
        assert r.status == 401
    r = await client.post(
        "/api/auth/token", json={"username": "alice", "password": "old-pass-123"}
    )
    assert r.status == 429
    assert (await _redeem(client, await _issue(client, "alice"))).status == 204
    r = await client.post(
        "/api/auth/token", json={"username": "alice", "password": "fresh-pass-9"}
    )
    assert r.status == 200


@pytest.mark.security
async def test_redeem_and_login_budgets_are_separate(client):
    for i in range(RESET_IP_MAX_FAILURES):
        assert (await _redeem(client, f"bad-{i}")).status == 410
    r = await client.post(
        "/api/auth/token", json={"username": "ghost", "password": "wrong"}
    )
    assert r.status == 401
