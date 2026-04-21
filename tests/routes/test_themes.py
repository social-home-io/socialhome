"""HTTP tests for /api/theme + /api/spaces/{id}/theme."""

from __future__ import annotations


from .conftest import _auth


# ─── Household theme ─────────────────────────────────────────────────────


async def test_get_household_theme_returns_defaults_first_call(client):
    r = await client.get("/api/theme", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body["primary_color"] == "#4A90E2"


async def test_update_household_theme_admin_succeeds(client):
    r = await client.put(
        "/api/theme",
        json={"primary_color": "#112233", "accent_color": "#445566"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["primary_color"] == "#112233"
    # Re-read confirms persistence.
    r = await client.get("/api/theme", headers=_auth(client._tok))
    assert (await r.json())["primary_color"] == "#112233"


async def test_update_household_theme_rejects_invalid_color(client):
    r = await client.put(
        "/api/theme",
        json={"primary_color": "red", "accent_color": "#445566"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_update_household_theme_accepts_partial_patch(client):
    """§23.125: PATCH-style body updates only the supplied fields."""
    r = await client.put(
        "/api/theme",
        json={"primary_color": "#000000"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["primary_color"] == "#000000"
    assert body["accent_color"].startswith("#")


async def test_update_household_theme_rejects_invalid_mode(client):
    r = await client.put(
        "/api/theme",
        json={"mode": "chartreuse"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_update_household_theme_accepts_extended_fields(client):
    r = await client.put(
        "/api/theme",
        json={
            "primary_color": "#112233",
            "accent_color": "#445566",
            "mode": "dark",
            "font_family": "rounded",
            "density": "compact",
            "corner_radius": 8,
            "surface_color": "#ffffff",
            "surface_dark": "#000011",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["mode"] == "dark"
    assert body["font_family"] == "rounded"
    assert body["density"] == "compact"
    assert body["corner_radius"] == 8
    assert body["surface_color"] == "#ffffff"


async def test_update_household_theme_rejects_non_admin(client):
    db = client._db
    from social_home.auth import sha256_token_hash

    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("bob", "bob-uid", "Bob"),
    )
    raw = "bob-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-bob", "bob-uid", "t", sha256_token_hash(raw)),
    )
    r = await client.put(
        "/api/theme",
        json={"primary_color": "#112233", "accent_color": "#445566"},
        headers=_auth(raw),
    )
    assert r.status == 403


# ─── Space theme ─────────────────────────────────────────────────────────


async def test_get_space_theme_falls_back_to_household(client):
    """When a space has no theme, GET returns household defaults flagged as default."""
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-1', 'X', 'inst-x', 'pascal', ?)",
        ("aa" * 32,),
    )
    r = await client.get("/api/spaces/sp-1/theme", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body["is_default"] is True
    assert "primary_color" in body


async def test_update_space_theme_owner_succeeds(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-2', 'Y', 'inst-x', 'pascal', ?)",
        ("aa" * 32,),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('sp-2', ?, 'owner')",
        (client._uid,),
    )
    r = await client.put(
        "/api/spaces/sp-2/theme",
        json={"primary_color": "#abcdef", "accent_color": "#fedcba"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["primary_color"] == "#abcdef"
    assert body["is_default"] is False


async def test_update_space_theme_non_member_403(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-3', 'Z', 'inst-x', 'somebody-else', ?)",
        ("aa" * 32,),
    )
    r = await client.put(
        "/api/spaces/sp-3/theme",
        json={"primary_color": "#abcdef", "accent_color": "#fedcba"},
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_update_space_theme_accepts_partial_patch(client):
    """§23.123: space theme supports partial updates (post_layout only, etc.)."""
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-X', 'X', 'inst-x', 'pascal', ?)",
        ("aa" * 32,),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('sp-X', ?, 'owner')",
        (client._uid,),
    )
    r = await client.put(
        "/api/spaces/sp-X/theme",
        json={"post_layout": "magazine"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["post_layout"] == "magazine"


async def test_update_space_theme_invalid_color_422(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-Y', 'Y', 'inst-x', 'pascal', ?)",
        ("aa" * 32,),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('sp-Y', ?, 'owner')",
        (client._uid,),
    )
    r = await client.put(
        "/api/spaces/sp-Y/theme",
        json={"primary_color": "blue", "accent_color": "#000000"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_update_household_theme_bad_json_400(client):
    r = await client.put(
        "/api/theme",
        data="not json",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_update_space_theme_bad_json_400(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-Z', 'Z', 'inst-x', 'pascal', ?)",
        ("aa" * 32,),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('sp-Z', ?, 'owner')",
        (client._uid,),
    )
    r = await client.put(
        "/api/spaces/sp-Z/theme",
        data="not-json",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_update_household_theme_unauthenticated_401(client):
    r = await client.put(
        "/api/theme",
        json={"primary_color": "#000000", "accent_color": "#ffffff"},
    )
    assert r.status == 401


async def test_update_space_theme_unauthenticated_401(client):
    r = await client.put(
        "/api/spaces/sp-anon/theme",
        json={"primary_color": "#000000", "accent_color": "#ffffff"},
    )
    assert r.status == 401
