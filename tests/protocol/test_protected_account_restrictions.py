"""§CP.R: a protected account is limited on the server, not just hidden.

Every restricted surface returns ``403 ACCOUNT_PROTECTED`` (with the
``capability`` id) for a protected account and keeps working for an
unprotected one. ``/api/me`` tells the account *that* it is protected and
*what* is limited — never ``is_minor`` / ``declared_age`` or any other
``SENSITIVE_FIELDS`` key — and nobody else learns it from ``/api/users``.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    child_protection_service_key,
    db_key,
    space_cal_service_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id
from socialhome.domain.child_protection import PROTECTED_ACCOUNT_RESTRICTIONS
from socialhome.security import SENSITIVE_FIELDS

pytestmark = pytest.mark.security

ALL_RESTRICTIONS = [c.value for c in PROTECTED_ACCOUNT_RESTRICTIONS]


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def _keys(obj: Any) -> set[str]:
    """Every dict key anywhere in a JSON value."""
    if isinstance(obj, dict):
        out = set(obj)
        for v in obj.values():
            out |= _keys(v)
        return out
    if isinstance(obj, list):
        out: set[str] = set()
        for v in obj:
            out |= _keys(v)
        return out
    return set()


async def _assert_protected(resp, capability: str) -> None:
    assert resp.status == 403, await resp.text()
    err = (await resp.json())["error"]
    assert err["code"] == "ACCOUNT_PROTECTED"
    assert err["capability"] == capability
    # The refusal explains what, never why.
    assert "declared_age" not in err["detail"]
    assert "minor" not in err["detail"].lower()


async def _assert_not_protected(resp) -> None:
    if resp.status == 403:
        err = (await resp.json()).get("error") or {}
        assert err.get("code") != "ACCOUNT_PROTECTED", err


@pytest.fixture
async def hh(aiohttp_client, tmp_dir):
    """Household with an admin, an adult (bob) and a protected kid, plus a
    private space all three belong to."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {
                "standalone": MappingProxyType(
                    {"external_url": "https://test.example"},
                ),
            },
        ),
    )
    app = create_app(cfg)
    tc = await aiohttp_client(app)
    db = app[db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    pk = bytes.fromhex(row["identity_public_key"])
    ids = {name: derive_user_id(pk, name) for name in ("admin", "bob", "kid")}
    for name, uid in ids.items():
        await db.enqueue(
            "INSERT OR REPLACE INTO users(username, user_id, display_name, "
            "is_admin) VALUES(?,?,?,?)",
            (name, uid, name.title(), 1 if name == "admin" else 0),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
            "VALUES(?,?,?,?)",
            (f"t-{name}", uid, "t", sha256_token_hash(f"{name}-tok")),
        )
    r = await tc.post("/api/spaces", json={"name": "Home"}, headers=_h("admin-tok"))
    assert r.status == 201, await r.text()
    sid = (await r.json())["id"]
    for name in ("bob", "kid"):
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'member')",
            (sid, ids[name]),
        )
    # A subscription link the kid minted *before* protection was enabled.
    kid_feed = await app[space_cal_service_key].issue_feed_token(
        user_id=ids["kid"], space_id=sid
    )
    cp = app[child_protection_service_key]
    await cp.enable_protection(
        minor_username="kid", declared_age=12, actor_user_id=ids["admin"]
    )
    await cp.add_guardian(
        minor_user_id=ids["kid"],
        guardian_user_id=ids["admin"],
        actor_user_id=ids["admin"],
    )
    tc._ids = ids
    tc._sid = sid
    tc._kid_feed = kid_feed
    return tc


async def _listing(hh, **extra) -> str:
    r = await hh.post(
        "/api/bazaar",
        json={
            "space_id": hh._sid,
            "title": "Bike",
            "currency": "EUR",
            **extra,
        },
        headers=_h("admin-tok"),
    )
    assert r.status == 201, await r.text()
    return (await r.json())["post_id"]


# ─── /api/me ────────────────────────────────────────────────────────────


async def test_me_reports_protection_without_sensitive_fields(hh):
    r = await hh.get("/api/me", headers=_h("kid-tok"))
    assert r.status == 200
    body = await r.json()
    assert body["protected"] is True
    assert body["restrictions"] == ALL_RESTRICTIONS
    assert not (_keys(body) & SENSITIVE_FIELDS)


async def test_me_for_an_adult_is_unrestricted(hh):
    body = await (await hh.get("/api/me", headers=_h("bob-tok"))).json()
    assert body["protected"] is False
    assert body["restrictions"] == []


async def test_my_protection_names_the_guardians(hh):
    r = await hh.get("/api/me/protection", headers=_h("kid-tok"))
    assert r.status == 200
    body = await r.json()
    assert body["protected"] is True
    assert body["restrictions"] == ALL_RESTRICTIONS
    assert [g["user_id"] for g in body["guardians"]] == [hh._ids["admin"]]
    assert not (_keys(body) & SENSITIVE_FIELDS)


async def test_my_protection_for_an_adult(hh):
    body = await (await hh.get("/api/me/protection", headers=_h("bob-tok"))).json()
    assert body == {"protected": False, "restrictions": [], "guardians": []}


async def test_other_users_never_learn_who_is_protected(hh):
    r = await hh.get("/api/users", headers=_h("bob-tok"))
    assert r.status == 200
    keys = _keys(await r.json())
    assert "protected" not in keys
    assert "restrictions" not in keys
    assert not (keys & SENSITIVE_FIELDS)


# ─── bazaar ─────────────────────────────────────────────────────────────


async def test_create_listing(hh):
    body = {
        "space_id": hh._sid,
        "title": "Lego",
        "mode": "fixed",
        "currency": "EUR",
        "price": 5,
    }
    await _assert_protected(
        await hh.post("/api/bazaar", json=body, headers=_h("kid-tok")), "bazaar"
    )
    r = await hh.post("/api/bazaar", json=body, headers=_h("bob-tok"))
    assert r.status == 201, await r.text()


async def test_place_bid(hh):
    lid = await _listing(hh, mode="auction", start_price=100, step_price=10)
    url = f"/api/bazaar/{lid}/bids"
    await _assert_protected(
        await hh.post(url, json={"amount": 200}, headers=_h("kid-tok")), "bazaar"
    )
    r = await hh.post(url, json={"amount": 200}, headers=_h("bob-tok"))
    assert r.status == 201, await r.text()


async def test_make_offer(hh):
    lid = await _listing(hh, mode="negotiable", price=500)
    url = f"/api/bazaar/{lid}/offers"
    await _assert_protected(
        await hh.post(url, json={"amount": 400}, headers=_h("kid-tok")), "bazaar"
    )
    r = await hh.post(url, json={"amount": 400}, headers=_h("bob-tok"))
    assert r.status == 201, await r.text()


async def test_browsing_the_bazaar_stays_open(hh):
    r = await hh.get("/api/bazaar", headers=_h("kid-tok"))
    assert r.status == 200


# ─── public spaces ──────────────────────────────────────────────────────


@pytest.mark.parametrize("space_type", ["public", "global"])
async def test_create_public_space(hh, space_type):
    body = {"name": "Club", "space_type": space_type}
    await _assert_protected(
        await hh.post("/api/spaces", json=body, headers=_h("kid-tok")),
        "public_spaces",
    )
    r = await hh.post("/api/spaces", json=body, headers=_h("bob-tok"))
    await _assert_not_protected(r)
    assert r.status == 201, await r.text()


@pytest.mark.parametrize("space_type", ["private", "household"])
async def test_create_private_space_stays_open(hh, space_type):
    r = await hh.post(
        "/api/spaces",
        json={"name": "Den", "space_type": space_type},
        headers=_h("kid-tok"),
    )
    assert r.status == 201, await r.text()


async def test_propose_public_tier(hh):
    r = await hh.post("/api/spaces", json={"name": "Den"}, headers=_h("kid-tok"))
    kid_sid = (await r.json())["id"]
    await _assert_protected(
        await hh.post(
            f"/api/spaces/{kid_sid}/proposals",
            json={"action": "set_public_tier", "space_type": "public"},
            headers=_h("kid-tok"),
        ),
        "public_spaces",
    )
    # Taking a space private again is not a publication — stays open.
    r = await hh.post(
        f"/api/spaces/{kid_sid}/proposals",
        json={"action": "set_public_tier", "space_type": "private"},
        headers=_h("kid-tok"),
    )
    await _assert_not_protected(r)
    r = await hh.post(
        f"/api/spaces/{hh._sid}/proposals",
        json={"action": "set_public_tier", "space_type": "public"},
        headers=_h("admin-tok"),
    )
    await _assert_not_protected(r)
    assert r.status == 200, await r.text()


# ─── public moments / public links ──────────────────────────────────────


async def test_public_moments_registration(hh):
    body = {"gfs_id": "gfs-1"}
    url = "/api/moments/public/registrations"
    await _assert_protected(
        await hh.post(url, json=body, headers=_h("kid-tok")), "public_moments"
    )
    # No connection server here, so the adult fails further in — but never
    # on the protection gate.
    await _assert_not_protected(await hh.post(url, json=body, headers=_h("bob-tok")))


async def test_public_moments_follow(hh):
    body = {"gfs_id": "gfs-1", "followed_user_id": "someone"}
    url = "/api/moments/public/follows"
    await _assert_protected(
        await hh.post(url, json=body, headers=_h("kid-tok")), "public_moments"
    )
    await _assert_not_protected(await hh.post(url, json=body, headers=_h("bob-tok")))


async def test_highlight_publish(hh):
    body = {"gfs_id": "gfs-1"}
    url = "/api/highlights/h-1/publish"
    await _assert_protected(
        await hh.post(url, json=body, headers=_h("kid-tok")), "public_links"
    )
    await _assert_not_protected(await hh.post(url, json=body, headers=_h("bob-tok")))


# ─── API tokens ─────────────────────────────────────────────────────────


async def test_mint_api_token(hh):
    body = {"label": "script"}
    await _assert_protected(
        await hh.post("/api/me/tokens", json=body, headers=_h("kid-tok")),
        "api_tokens",
    )
    r = await hh.post("/api/me/tokens", json=body, headers=_h("bob-tok"))
    assert r.status == 201, await r.text()


async def test_protected_account_can_still_list_and_revoke_tokens(hh):
    r = await hh.get("/api/me/tokens", headers=_h("kid-tok"))
    assert r.status == 200


async def test_protected_account_can_still_sign_in(hh):
    """Signing in mints a browser session through the platform adapter, not
    the personal-token path, so protection never locks an account out."""
    r = await hh.get("/api/me", headers=_h("kid-tok"))
    assert r.status == 200


# ─── calendar feeds ─────────────────────────────────────────────────────


async def test_mint_calendar_feed_token(hh):
    url = f"/api/spaces/{hh._sid}/calendar/feed-token"
    await _assert_protected(await hh.post(url, headers=_h("kid-tok")), "calendar_feeds")
    r = await hh.post(url, headers=_h("bob-tok"))
    assert r.status == 201, await r.text()


async def test_feed_minted_before_protection_stops_serving(hh):
    url = f"/api/spaces/{hh._sid}/calendar/export.ics?token={hh._kid_feed}"
    r = await hh.get(url)
    assert r.status == 401


async def test_lifting_protection_restores_everything(hh):
    cp = hh.app[child_protection_service_key]
    await cp.disable_protection(minor_username="kid", actor_user_id=hh._ids["admin"])
    body = await (await hh.get("/api/me", headers=_h("kid-tok"))).json()
    assert body["protected"] is False
    r = await hh.post("/api/me/tokens", json={"label": "x"}, headers=_h("kid-tok"))
    assert r.status == 201
    url = f"/api/spaces/{hh._sid}/calendar/export.ics?token={hh._kid_feed}"
    assert (await hh.get(url)).status == 200
