"""§CP.R applies to what an account set up *before* it became protected.

When a household admin turns protection on, the server closes what the
account had already opened to the outside — not just new attempts:

* personal API tokens are revoked (password sign-ins keep working);
* public highlight links are unpublished;
* public-moment directory registrations and public follows are removed;
* active bazaar listings are closed (``cancelled``) and no offer on the
  account's listings can be accepted any more.

And a space a protected account owns never goes public, whoever asks —
an adult admin, a quorum, an open proposal from before — nor is a public
space handed to a protected account. The refusal says nothing about the
owner.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import child_protection_service_key, db_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config

pytestmark = pytest.mark.security

USERS = ("admin", "kid", "bob", "carol")


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "retro.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _protect(tc) -> None:
    await tc._app[child_protection_service_key].enable_protection(
        minor_username="kid", declared_age=12, actor_user_id=tc._ids["admin"]
    )


@pytest.fixture
async def hh(aiohttp_client, tmp_dir):
    """Before protection: kid holds a personal API token, a published
    highlight, a public-moment registration + follow, an active listing
    with an offer from bob, and owns a private space bob + carol admin."""
    app = create_app(_config(tmp_dir))
    tc = await aiohttp_client(app)
    db = app[db_key]
    ids = {name: f"u-{name}" for name in USERS}
    for name, uid in ids.items():
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin)"
            " VALUES(?,?,?,?)",
            (name, uid, name.title(), 1 if name == "admin" else 0),
        )
        # A password sign-in session, as the platform login mints it.
        await db.enqueue(
            "INSERT INTO platform_users(username, display_name) VALUES(?, ?)",
            (name, name.title()),
        )
        token_hash = sha256_token_hash(f"{name}-tok")
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
            " VALUES(?,?,?,?)",
            (f"t-{name}", uid, "web", token_hash),
        )
        await db.enqueue(
            "INSERT INTO platform_tokens(token_id, username, token_hash) VALUES(?,?,?)",
            (f"t-{name}", name, token_hash),
        )
    r = await tc.post("/api/me/tokens", json={"label": "script"}, headers=_h("kid-tok"))
    assert r.status == 201, await r.text()
    tc._kid_personal = (await r.json())["token"]

    r = await tc.post("/api/spaces", json={"name": "Den"}, headers=_h("kid-tok"))
    assert r.status == 201, await r.text()
    tc._den = (await r.json())["id"]
    for name in ("bob", "carol"):
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'admin')",
            (tc._den, ids[name]),
        )
    r = await tc.post(
        "/api/spaces",
        json={"name": "Open Den", "space_type": "public"},
        headers=_h("kid-tok"),
    )
    assert r.status == 201, await r.text()
    tc._open_den = (await r.json())["id"]

    r = await tc.post(
        "/api/bazaar",
        json={
            "space_id": tc._den,
            "title": "Lego",
            "mode": "negotiable",
            "currency": "EUR",
            "price": 500,
        },
        headers=_h("kid-tok"),
    )
    assert r.status == 201, await r.text()
    tc._listing = (await r.json())["post_id"]
    r = await tc.post(
        f"/api/bazaar/{tc._listing}/offers", json={"amount": 400}, headers=_h("bob-tok")
    )
    assert r.status == 201, await r.text()
    tc._offer = (await r.json())["id"]

    # A connection server that is gone — every retroactive step must still
    # close the local side.
    await db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, public_key,"
        " inbox_url, status, paired_at) VALUES('gfs-1','gi-1','GFS','00',"
        " 'https://gfs.invalid','suspended', datetime('now'))"
    )
    expires = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    await db.enqueue(
        "INSERT INTO highlights(id, author_user_id, highlight_date, audience_kind,"
        " audience_json, created_at, expires_at, public_gfs_id, public_published_at)"
        " VALUES('h-kid', ?, '2026-09-01', 'all_paired', '[]', datetime('now'), ?,"
        " 'gfs-1', datetime('now'))",
        (ids["kid"], expires),
    )
    await db.enqueue(
        "INSERT INTO moment_public_registrations(user_id, gfs_id) VALUES(?, 'gfs-1')",
        (ids["kid"],),
    )
    await db.enqueue(
        "INSERT INTO moment_public_follows(follower_user_id, followed_user_id,"
        " gfs_id, followed_instance_pk, followed_username, followed_display_name)"
        " VALUES(?, 'stranger', 'gfs-1', '00', 'stranger', 'Stranger')",
        (ids["kid"],),
    )
    tc._app = app
    tc._db = db
    tc._ids = ids
    return tc


# ── Retroactive revocation ──────────────────────────────────────────────


async def test_personal_token_is_revoked_but_sign_in_keeps_working(hh):
    assert (await hh.get("/api/me", headers=_h(hh._kid_personal))).status == 200
    await _protect(hh)
    assert (await hh.get("/api/me", headers=_h(hh._kid_personal))).status == 401
    r = await hh.get("/api/me", headers=_h("kid-tok"))
    assert r.status == 200
    assert (await r.json())["protected"] is True
    # Nobody else's tokens are touched.
    assert (await hh.get("/api/me", headers=_h("bob-tok"))).status == 200


async def test_public_highlight_link_is_unpublished(hh):
    await _protect(hh)
    row = await hh._db.fetchone("SELECT public_gfs_id FROM highlights WHERE id='h-kid'")
    assert row["public_gfs_id"] is None


async def test_public_moment_registration_and_follows_are_removed(hh):
    await _protect(hh)
    uid = hh._ids["kid"]
    assert (
        await hh._db.fetchone(
            "SELECT 1 FROM moment_public_registrations WHERE user_id=?", (uid,)
        )
        is None
    )
    assert (
        await hh._db.fetchone(
            "SELECT 1 FROM moment_public_follows WHERE follower_user_id=?", (uid,)
        )
        is None
    )


async def test_active_listing_is_closed_and_offers_cannot_be_accepted(hh):
    await _protect(hh)
    row = await hh._db.fetchone(
        "SELECT status FROM bazaar_listings WHERE post_id=?", (hh._listing,)
    )
    assert row["status"] == "cancelled"
    r = await hh.post(
        f"/api/bazaar/{hh._listing}/offers/{hh._offer}/accept",
        headers=_h("kid-tok"),
    )
    assert r.status >= 400, await r.text()
    row = await hh._db.fetchone(
        "SELECT status FROM bazaar_listings WHERE post_id=?", (hh._listing,)
    )
    assert row["status"] == "cancelled"


async def test_enabling_protection_is_idempotent(hh):
    await _protect(hh)
    await _protect(hh)
    assert (await hh.get("/api/me", headers=_h("kid-tok"))).status == 200


# ── A protected owner's space stays off the public tiers ────────────────


async def _space_type(hh, sid: str) -> str:
    row = await hh._db.fetchone("SELECT space_type FROM spaces WHERE id=?", (sid,))
    return row["space_type"]


@pytest.mark.parametrize("tier", ["public", "global"])
async def test_adult_admin_cannot_publish_a_protected_owners_space(hh, tier):
    await _protect(hh)
    r = await hh.post(
        f"/api/spaces/{hh._den}/proposals",
        json={"action": "set_public_tier", "space_type": tier},
        headers=_h("bob-tok"),
    )
    assert r.status == 403, await r.text()
    detail = (await r.json())["error"]["detail"]
    assert detail == "This space can't be made public."
    assert await _space_type(hh, hh._den) == "private"


async def test_an_open_proposal_from_before_cannot_pass(hh):
    r = await hh.post(
        f"/api/spaces/{hh._den}/proposals",
        json={"action": "set_public_tier", "space_type": "public"},
        headers=_h("bob-tok"),
    )
    assert r.status in (200, 201), await r.text()
    proposal = (await r.json())["proposal"]
    assert proposal["status"] == "pending"
    pid = proposal["id"]
    await _protect(hh)
    r = await hh.post(
        f"/api/spaces/{hh._den}/proposals/{pid}/vote",
        json={"approve": True},
        headers=_h("carol-tok"),
    )
    assert r.status < 500, await r.text()
    assert await _space_type(hh, hh._den) == "private"
    row = await hh._db.fetchone(
        "SELECT status FROM space_admin_proposals WHERE id=?", (pid,)
    )
    assert row["status"] == "rejected"


async def test_protected_owner_space_can_still_change_otherwise(hh):
    await _protect(hh)
    r = await hh.post(
        f"/api/spaces/{hh._den}/proposals",
        json={"action": "set_public_tier", "space_type": "household"},
        headers=_h("bob-tok"),
    )
    assert r.status == 200, await r.text()


async def test_a_public_space_is_not_handed_to_a_protected_account(hh):
    await _protect(hh)
    r = await hh.post(
        "/api/spaces",
        json={"name": "Open", "space_type": "public"},
        headers=_h("admin-tok"),
    )
    assert r.status == 201, await r.text()
    sid = (await r.json())["id"]
    await hh._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'member')",
        (sid, hh._ids["kid"]),
    )
    r = await hh.post(
        f"/api/spaces/{sid}/ownership",
        json={"to_user_id": hh._ids["kid"]},
        headers=_h("admin-tok"),
    )
    assert r.status == 403, await r.text()
    detail = (await r.json())["error"]["detail"]
    assert "protect" not in detail.lower() and "minor" not in detail.lower()
    row = await hh._db.fetchone("SELECT owner_username FROM spaces WHERE id=?", (sid,))
    assert row["owner_username"] == "admin"
    # A private space can still be handed over.
    r = await hh.post("/api/spaces", json={"name": "Den2"}, headers=_h("admin-tok"))
    sid2 = (await r.json())["id"]
    await hh._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'member')",
        (sid2, hh._ids["kid"]),
    )
    r = await hh.post(
        f"/api/spaces/{sid2}/ownership",
        json={"to_user_id": hh._ids["kid"]},
        headers=_h("admin-tok"),
    )
    assert r.status < 300, await r.text()


async def test_a_public_space_the_account_owns_goes_private(hh):
    assert await _space_type(hh, hh._open_den) == "public"
    await _protect(hh)
    assert await _space_type(hh, hh._open_den) == "private"
    # Its other spaces are left as they were.
    assert await _space_type(hh, hh._den) == "private"
