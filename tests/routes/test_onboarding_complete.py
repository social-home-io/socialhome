"""Tests for POST /api/me/onboarding-complete (first-run wizard hide)."""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_onboarding_complete_clears_is_new_member_flag(client):
    # The fixture creates an admin row WITHOUT is_new_member=False; the
    # column defaults to 1 so the SPA's `currentUser.is_new_member`
    # check fires the wizard. Confirm.
    row = await client._db.fetchone(
        "SELECT is_new_member FROM users WHERE username='admin'",
    )
    assert row is not None and bool(row["is_new_member"]) is True

    r = await client.post(
        "/api/me/onboarding-complete",
        headers=_auth(client._tok),
    )
    assert r.status == 204, await r.text()

    row = await client._db.fetchone(
        "SELECT is_new_member FROM users WHERE username='admin'",
    )
    assert bool(row["is_new_member"]) is False


async def test_onboarding_complete_is_idempotent(client):
    r1 = await client.post(
        "/api/me/onboarding-complete",
        headers=_auth(client._tok),
    )
    r2 = await client.post(
        "/api/me/onboarding-complete",
        headers=_auth(client._tok),
    )
    assert r1.status == 204
    assert r2.status == 204


async def test_onboarding_complete_requires_auth(client):
    r = await client.post("/api/me/onboarding-complete")
    assert r.status == 401
