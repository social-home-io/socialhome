"""Extra coverage for routes/users.py."""

from __future__ import annotations


from .conftest import _auth


async def test_create_token_with_label(client):
    r = await client.post(
        "/api/me/tokens",
        json={"label": "ci-bot", "expires_at": "2099-01-01T00:00:00+00:00"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["token_id"]
    assert body["token"]


async def test_create_token_no_body(client):
    r = await client.post(
        "/api/me/tokens",
        json={},
        headers=_auth(client._tok),
    )
    # Accept 201 (defaulted) or 422 (label required by service).
    assert r.status in (201, 422)


async def test_revoke_token_succeeds(client):
    r = await client.post(
        "/api/me/tokens",
        json={"label": "x"},
        headers=_auth(client._tok),
    )
    tid = (await r.json())["token_id"]
    r = await client.delete(
        f"/api/me/tokens/{tid}",
        headers=_auth(client._tok),
    )
    assert r.status == 204


async def test_patch_me_display_name_strips_whitespace(client):
    r = await client.patch(
        "/api/me",
        json={"display_name": "  Spacey Name  "},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["display_name"] == "Spacey Name"


async def test_patch_me_unknown_field_returns_current_user(client):
    """No supported keys → fallback returns current user without changes."""
    r = await client.patch(
        "/api/me",
        json={"unknown_field": 42},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["username"] == "admin"
