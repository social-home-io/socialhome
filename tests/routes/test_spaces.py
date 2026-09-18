"""Tests for space routes — /api/spaces/* endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.app_keys import db_key as _db_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id
from socialhome.domain.space import SpaceFeatureAccess, SpaceFeatures
from socialhome.routes.spaces import _features_from_body


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seat_local_member(client, sid: str, user_token: str, user_uid: str) -> None:
    """Test helper: admin invites + the invitee accepts in one shot.

    The HTTP surface for adding a member now requires the invitee's
    consent (Pascal: "if I invite local member to space — they should
    receive a join request like all others"), so most existing tests
    that wanted bob seated immediately need to drive both legs. This
    helper keeps each test focused on its own assertion rather than
    re-stating the invite + accept dance every time.
    """
    r = await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": user_uid},
        headers=_auth(client._admin_token),
    )
    body = await r.json()
    assert r.status == 202, f"invite failed: {body!r}"
    invitation_id = body["invitation_id"]
    r2 = await client.post(
        f"/api/local_invites/{invitation_id}/accept",
        json={},
        headers=_auth(user_token),
    )
    assert r2.status == 200, f"accept failed: {await r2.text()!r}"


@pytest.fixture
async def client(tmp_dir):
    """App client with admin (pascal) and regular user (bob)."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        db = app[_db_key]
        _row = await db.fetchone(
            "SELECT identity_public_key FROM instance_identity WHERE id='self'"
        )
        _pk = bytes.fromhex(_row["identity_public_key"])

        class _KP:
            public_key = _pk

        kp = _KP()
        uid = derive_user_id(kp.public_key, "pascal")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,1)",
            ("pascal", uid, "Pascal"),
        )
        raw_token = "test-token-raw"
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-1", uid, "test", sha256_token_hash(raw_token)),
        )
        uid2 = derive_user_id(kp.public_key, "bob")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
            ("bob", uid2, "Bob"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-2", uid2, "test", sha256_token_hash("bob-token-raw")),
        )
        tc._admin_token = raw_token
        tc._admin_uid = uid
        tc._bob_token = "bob-token-raw"
        tc._bob_uid = uid2
        yield tc


async def test_create_space(client):
    """POST /api/spaces creates a space and returns 201."""
    resp = await client.post(
        "/api/spaces",
        json={"name": "Family", "emoji": "🏠"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["name"] == "Family"
    assert "id" in body


async def test_get_space(client):
    """GET /api/spaces/{id} returns the space details."""
    r = await client.post(
        "/api/spaces",
        json={"name": "GetMe"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["name"] == "GetMe"


async def test_create_public_space_without_location(client):
    """A public space can be created without a location pin (201)."""
    resp = await client.post(
        "/api/spaces",
        json={"name": "No pin", "space_type": "public"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201


async def test_create_space_with_category_and_min_age(client):
    """POST accepts ``category`` + ``min_age`` for a discoverable tier.

    GET /api/spaces/{id} does not expose ``min_age``, so the age gate is
    verified via the child-protection age-gate endpoint.
    """
    resp = await client.post(
        "/api/spaces",
        json={
            "name": "Gamers",
            "space_type": "global",
            "category": "gaming",
            "min_age": 13,
        },
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    sid = (await resp.json())["id"]
    detail = await (
        await client.get(f"/api/spaces/{sid}", headers=_auth(client._admin_token))
    ).json()
    assert detail["category"] == "gaming"
    gate = await (
        await client.get(
            f"/api/cp/spaces/{sid}/age-gate", headers=_auth(client._admin_token)
        )
    ).json()
    assert gate["min_age"] == 13


async def test_patch_space_category(client):
    """PATCH /api/spaces/{id} accepts ``category`` and GET reflects it."""
    r = await client.post(
        "/api/spaces",
        json={"name": "Tunes", "space_type": "public"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.patch(
        f"/api/spaces/{sid}",
        json={"category": "music_arts"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    detail = await (
        await client.get(f"/api/spaces/{sid}", headers=_auth(client._admin_token))
    ).json()
    assert detail["category"] == "music_arts"


async def test_update_space(client):
    """PATCH /api/spaces/{id} updates the space name."""
    r = await client.post(
        "/api/spaces",
        json={"name": "Old Name"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.patch(
        f"/api/spaces/{sid}",
        json={"name": "New Name"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    assert (await resp.json())["name"] == "New Name"


async def test_patch_sets_retention_days(client):
    """PATCH /api/spaces/{id} accepts retention_days and persists it."""
    r = await client.post(
        "/api/spaces",
        json={"name": "RetentionSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.patch(
        f"/api/spaces/{sid}",
        json={"retention_days": 30},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    got = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    body = await got.json()
    assert body["retention_days"] == 30


async def test_patch_retention_days_zero_clears(client):
    """PATCH retention_days=0 clears the setting (coerced to null)."""
    r = await client.post(
        "/api/spaces",
        json={"name": "RetClear"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await client.patch(
        f"/api/spaces/{sid}",
        json={"retention_days": 14},
        headers=_auth(client._admin_token),
    )
    resp = await client.patch(
        f"/api/spaces/{sid}",
        json={"retention_days": 0},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    got = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    assert (await got.json())["retention_days"] is None


async def test_patch_sets_retention_exempt_types(client):
    """PATCH retention_exempt_types is persisted and echoed on GET."""
    r = await client.post(
        "/api/spaces",
        json={"name": "ExemptSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.patch(
        f"/api/spaces/{sid}",
        json={"retention_exempt_types": ["list", "poll"]},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    got = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    body = await got.json()
    assert set(body["retention_exempt_types"]) == {"list", "poll"}


async def test_get_includes_retention_fields(client):
    """Fresh spaces report null retention_days + empty exempt list."""
    r = await client.post(
        "/api/spaces",
        json={"name": "FreshSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    got = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    body = await got.json()
    assert body["retention_days"] is None
    assert body["retention_exempt_types"] == []


async def test_patch_sets_delegated_admin_authority(client):
    """PATCH features.delegated_admin_authority=true persists and is echoed
    back on GET.

    Regression: the toggle round-tripped 200 OK but _features_from_body
    dropped the key, so the owner-offline delegation opt-in never flipped and
    the whole epic was unreachable via the real API.
    """
    r = await client.post(
        "/api/spaces",
        json={"name": "DelegSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    # Fresh space defaults the flag OFF.
    got0 = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    assert (await got0.json())["features"]["delegated_admin_authority"] is False

    resp = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"delegated_admin_authority": True}},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    got = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    body = await got.json()
    assert body["features"]["delegated_admin_authority"] is True


async def _promote_bob_to_admin(client, sid: str) -> None:
    """Seat bob on *sid* and promote him to space ADMIN (not owner)."""
    await _seat_local_member(client, sid, client._bob_token, client._bob_uid)
    r = await client.patch(
        f"/api/spaces/{sid}/members/{client._bob_uid}",
        json={"role": "admin"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200, await r.text()


async def test_partial_features_patch_leaves_allow_subscribers_alone(client):
    """F3: a PATCH body carrying only SOME feature keys must merge onto the
    space's current features, not onto the class defaults.

    Before the fix, ``{"features": {"bazaar": false}}`` silently turned
    ``allow_subscribers`` back OFF — withdrawing the space's public readability
    (and purging its connection-server seats) on an edit that never mentioned it.
    """
    r = await client.post(
        "/api/spaces",
        json={"name": "Partial", "space_type": "global"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"allow_subscribers": True}},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200, await r.text()

    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"bazaar": False}},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200, await r.text()
    body = await (
        await client.get(f"/api/spaces/{sid}", headers=_auth(client._admin_token))
    ).json()
    assert body["features"]["allow_subscribers"] is True
    assert body["features"]["bazaar"] is False


async def test_partial_features_patch_by_an_admin_is_not_an_owner_gate(client):
    """F3: a non-owner ADMIN editing an unrelated feature must not trip the
    owner-only ``allow_subscribers`` gate — before the fix the missing key read
    as the class default, so the gate saw a change the admin never made."""
    r = await client.post(
        "/api/spaces",
        json={"name": "AdminPartial", "space_type": "global"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"allow_subscribers": True}},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200, await r.text()
    await _promote_bob_to_admin(client, sid)

    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"bazaar": False}},
        headers=_auth(client._bob_token),
    )
    assert r.status == 200, await r.text()
    body = await (
        await client.get(f"/api/spaces/{sid}", headers=_auth(client._admin_token))
    ).json()
    assert body["features"]["allow_subscribers"] is True
    assert body["features"]["bazaar"] is False


async def test_explicit_allow_subscribers_false_still_turns_it_off_for_the_owner(
    client,
):
    """F3: merging onto the current features must not make the flag
    un-clearable — an EXPLICIT ``false`` from the owner still withdraws
    readability."""
    r = await client.post(
        "/api/spaces",
        json={"name": "OwnerOff", "space_type": "global"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"allow_subscribers": True}},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200, await r.text()
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"allow_subscribers": False}},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200, await r.text()
    body = await (
        await client.get(f"/api/spaces/{sid}", headers=_auth(client._admin_token))
    ).json()
    assert body["features"]["allow_subscribers"] is False


async def test_explicit_allow_subscribers_change_by_an_admin_is_still_forbidden(client):
    """F3: the owner-only gate still bites when the admin actually asks for the
    change — the merge narrows the gate to real edits, it does not remove it."""
    r = await client.post(
        "/api/spaces",
        json={"name": "AdminOff", "space_type": "global"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"allow_subscribers": True}},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200, await r.text()
    await _promote_bob_to_admin(client, sid)

    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"allow_subscribers": False}},
        headers=_auth(client._bob_token),
    )
    assert r.status == 403, await r.text()
    body = await (
        await client.get(f"/api/spaces/{sid}", headers=_auth(client._admin_token))
    ).json()
    assert body["features"]["allow_subscribers"] is True


def test_features_from_body_merges_onto_supplied_defaults():
    """Unit: with ``defaults`` given, an absent key takes the CURRENT space's
    value; a present key still wins (including an explicit ``False``)."""
    current = SpaceFeatures(allow_subscribers=True, bazaar=True, location=True)
    merged = _features_from_body({"bazaar": False}, defaults=current)
    assert merged.allow_subscribers is True
    assert merged.location is True
    assert merged.bazaar is False
    off = _features_from_body({"allow_subscribers": False}, defaults=current)
    assert off.allow_subscribers is False


def test_features_from_body_carries_delegated_admin_authority():
    """Unit: _features_from_body rehydrates delegated_admin_authority from the
    PATCH body (True when set, False when omitted)."""
    assert (
        _features_from_body(
            {"delegated_admin_authority": True}
        ).delegated_admin_authority
        is True
    )
    assert _features_from_body({}).delegated_admin_authority is False


def test_features_from_body_roundtrips_every_wire_field():
    """CI guard for the route parser: a full features wire dict (every field
    non-default) rehydrates with no field dropped.

    The SPA always PATCHes the full features dict, so this is the contract
    the parser must uphold — fails the moment ``_features_from_body`` drops a
    field from the wire shape.
    """
    f = SpaceFeatures(
        calendar=False,
        todo=False,
        location=True,
        location_mode="zone_only",
        stickies=False,
        pages=False,
        gallery=False,
        bazaar=False,
        posts_access=SpaceFeatureAccess.MODERATED,
        pages_access=SpaceFeatureAccess.ADMIN_ONLY,
        stickies_access=SpaceFeatureAccess.MODERATED,
        calendar_access=SpaceFeatureAccess.ADMIN_ONLY,
        tasks_access=SpaceFeatureAccess.MODERATED,
        allow_subscriber_comment=True,
        allow_subscriber_react=True,
        delegated_admin_authority=True,
        allowed_post_types=("image", "text"),
    )
    parsed = _features_from_body(f.to_wire_dict())
    for field_name in f.to_wire_dict():
        assert getattr(parsed, field_name) == getattr(f, field_name), (
            f"_features_from_body dropped feature field {field_name!r}"
        )
    assert parsed == f


async def test_create_space_accepts_retention_days(client):
    """POST /api/spaces with retention_days stores it."""
    r = await client.post(
        "/api/spaces",
        json={"name": "BornWithRetention", "retention_days": 7},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201
    sid = (await r.json())["id"]
    got = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    assert (await got.json())["retention_days"] == 7


async def test_dissolve_space(client):
    """DELETE /api/spaces/{id} dissolves the space."""
    r = await client.post(
        "/api/spaces",
        json={"name": "Temp"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.delete(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200


async def test_archive_and_unarchive_space(client):
    """POST /api/spaces/{id}/archive makes a space read-only (and visible
    via GET with archived=true); DELETE unarchives it."""
    r = await client.post(
        "/api/spaces", json={"name": "Arch"}, headers=_auth(client._admin_token)
    )
    sid = (await r.json())["id"]

    resp = await client.post(
        f"/api/spaces/{sid}/archive", headers=_auth(client._admin_token)
    )
    assert resp.status == 200
    assert (await resp.json())["archived"] is True

    # GET reflects the archived flag; the space is still readable.
    got = await client.get(f"/api/spaces/{sid}", headers=_auth(client._admin_token))
    assert got.status == 200
    assert (await got.json())["archived"] is True

    # Writing to the feed is rejected while archived.
    post = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "nope"},
        headers=_auth(client._admin_token),
    )
    assert post.status == 403

    # Unarchive restores read-write.
    resp = await client.delete(
        f"/api/spaces/{sid}/archive", headers=_auth(client._admin_token)
    )
    assert resp.status == 200
    assert (await resp.json())["archived"] is False
    post = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "ok"},
        headers=_auth(client._admin_token),
    )
    assert post.status in (200, 201)


async def test_space_serialisation_exposes_archived_reason(client):
    """The space detail + list responses surface ``archived_reason`` so the
    SPA can distinguish a normal reversible admin archive (``null``) from a
    remote-terminated read-only archive (``'dissolved'`` / ``'removed'``)."""
    from socialhome.app_keys import space_repo_key

    r = await client.post(
        "/api/spaces", json={"name": "Reasoned"}, headers=_auth(client._admin_token)
    )
    sid = (await r.json())["id"]

    # A normal active space reports archived_reason=None.
    got = await client.get(f"/api/spaces/{sid}", headers=_auth(client._admin_token))
    body = await got.json()
    assert "archived_reason" in body
    assert body["archived_reason"] is None

    lst = await client.get("/api/spaces", headers=_auth(client._admin_token))
    rows = await lst.json()
    row = next(s for s in rows if s["id"] == sid)
    assert "archived_reason" in row
    assert row["archived_reason"] is None

    # Archive it read-only with a 'dissolved' reason, as the inbound
    # SPACE_DISSOLVED handler does on a member's copy.
    repo = client.app[space_repo_key]
    await repo.set_archived(sid, True, reason="dissolved")

    got = await client.get(f"/api/spaces/{sid}", headers=_auth(client._admin_token))
    body = await got.json()
    assert body["archived"] is True
    assert body["archived_reason"] == "dissolved"

    lst = await client.get("/api/spaces", headers=_auth(client._admin_token))
    rows = await lst.json()
    row = next(s for s in rows if s["id"] == sid)
    assert row["archived_reason"] == "dissolved"


async def test_invite_and_accept_local_member(client):
    """``POST /api/spaces/{id}/members`` now creates a pending
    invitation; the invitee accepts via
    ``POST /api/local_invites/{id}/accept`` before they're seated.

    Pascal: "if I invite local member to space — they should receive
    a join request like all others". Before this change the admin's
    POST seated the user immediately; now the user-side acceptance is
    required for parity with the §D1b cross-household flow.
    """
    r = await client.post(
        "/api/spaces",
        json={"name": "InviteSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    # Admin invites bob — response is 202 + pending invitation id.
    r2 = await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    assert r2.status == 202
    body = await r2.json()
    assert body["status"] == "pending"
    invitation_id = body["invitation_id"]
    # Member list still shows only the admin (owner); bob isn't
    # seated yet.
    resp = await client.get(
        f"/api/spaces/{sid}/members",
        headers=_auth(client._admin_token),
    )
    members = await resp.json()
    assert len(members) == 1
    # Bob's inbox surfaces the pending invite.
    r3 = await client.get(
        "/api/local_invites",
        headers=_auth(client._bob_token),
    )
    invites = await r3.json()
    assert any(i["invitation_id"] == invitation_id for i in invites)
    # Bob accepts.
    r4 = await client.post(
        f"/api/local_invites/{invitation_id}/accept",
        json={},
        headers=_auth(client._bob_token),
    )
    assert r4.status == 200
    # Now the member list contains both.
    resp = await client.get(
        f"/api/spaces/{sid}/members",
        headers=_auth(client._admin_token),
    )
    members = await resp.json()
    assert len(members) == 2
    user_ids = {m["user_id"] for m in members}
    assert client._bob_uid in user_ids
    # Online-status + location-share fields still ride on every row.
    for member in members:
        assert "is_online" in member
        assert "is_idle" in member
        assert "last_seen_at" in member
        assert "location_share_enabled" in member
        assert isinstance(member["location_share_enabled"], bool)


async def test_decline_local_invite(client):
    """Bob can decline — no seat, invite marked declined."""
    r = await client.post(
        "/api/spaces",
        json={"name": "DeclineSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    invitation_id = (await r2.json())["invitation_id"]
    r3 = await client.post(
        f"/api/local_invites/{invitation_id}/decline",
        json={},
        headers=_auth(client._bob_token),
    )
    assert r3.status == 204
    # Bob's pending list is empty.
    r4 = await client.get(
        "/api/local_invites",
        headers=_auth(client._bob_token),
    )
    invites = await r4.json()
    assert not any(i["invitation_id"] == invitation_id for i in invites)


async def test_local_invite_accept_rejects_other_user(client):
    """An invite for bob cannot be accepted by the admin (or anyone
    else). Defence-in-depth — the route only surfaces a user's own
    invites, but a hostile API call shouldn't seat the wrong user."""
    r = await client.post(
        "/api/spaces",
        json={"name": "GuardSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    invitation_id = (await r2.json())["invitation_id"]
    # Admin tries to "accept on bob's behalf" — refuse.
    r3 = await client.post(
        f"/api/local_invites/{invitation_id}/accept",
        json={},
        headers=_auth(client._admin_token),
    )
    assert r3.status == 403


async def test_list_members_includes_federated_remote_members(client):
    """§D1b — a peer who joined via the cross-household invite flow is
    seated in ``space_remote_members`` on the inviter's instance, not
    ``space_members``. ``GET /api/spaces/{id}/members`` must surface
    both so the inviter actually sees the person they just invited.
    """
    app = client.app
    db = app[_db_key]
    r = await client.post(
        "/api/spaces",
        json={"name": "RemoteRoster"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    # Drop in a remote_members row directly — the inbound federation
    # event handler that normally writes this is exercised in its own
    # tests; here we want to assert the route surface, not the inbound
    # pipeline.
    await db.enqueue(
        "INSERT INTO space_remote_members"
        "(space_id, instance_id, user_id, user_pk, display_name)"
        " VALUES(?,?,?,?,?)",
        (
            sid,
            "peer-instance-id",
            "uid-friend-of-pascal",
            "fake-public-key",
            "Anna (other household)",
        ),
    )
    resp = await client.get(
        f"/api/spaces/{sid}/members",
        headers=_auth(client._admin_token),
    )
    members = await resp.json()
    # 1 local (admin/owner) + 1 remote (the federated friend) = 2.
    assert len(members) == 2
    remote_rows = [m for m in members if m.get("instance_id")]
    assert len(remote_rows) == 1
    remote = remote_rows[0]
    assert remote["user_id"] == "uid-friend-of-pascal"
    assert remote["display_name"] == "Anna (other household)"
    assert remote["instance_id"] == "peer-instance-id"
    # Remote members can't be promoted; role pinned to member.
    assert remote["role"] == "member"
    # No local picture / presence / location-share to surface.
    assert remote["picture_url"] is None
    assert remote["is_online"] is False
    assert remote["location_share_enabled"] is False


async def test_remote_member_display_name_prefers_fresh_users_table_row(client):
    """When the remote user renames themselves on their household, the
    USER_UPDATED federation event updates ``remote_users.display_name``
    on every paired peer. The members endpoint must prefer that fresh
    value over the §D1b accept-time snapshot in
    ``space_remote_members.display_name`` — otherwise the rendered
    roster freezes at the invite-accept name and a rename only
    propagates through a kick + re-invite cycle."""
    app = client.app
    db = app[_db_key]
    r = await client.post(
        "/api/spaces",
        json={"name": "RenameBackfill"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    # Seed BOTH: the §D1b accept-time snapshot AND a newer entry in
    # remote_users (as USERS_SYNC / USER_UPDATED would populate).
    # remote_users.instance_id FKs to remote_instances; seed the peer
    # row first so the insert doesn't trip the constraint.
    await db.enqueue(
        "INSERT INTO remote_instances"
        "(id, display_name, remote_identity_pk, key_self_to_remote,"
        " key_remote_to_self, remote_inbox_url, local_inbox_id, status,"
        " source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "peer-instance-id",
            "Peer",
            "00" * 32,
            "k1",
            "k2",
            "https://peer/wh",
            "wh-peer",
            "confirmed",
            "manual",
        ),
    )
    await db.enqueue(
        "INSERT INTO space_remote_members"
        "(space_id, instance_id, user_id, user_pk, display_name)"
        " VALUES(?,?,?,?,?)",
        (
            sid,
            "peer-instance-id",
            "uid-jacqueline",
            "fake-public-key",
            "Jacqueline (accept-time name)",
        ),
    )
    await db.enqueue(
        "INSERT INTO remote_users"
        "(user_id, instance_id, remote_username, display_name,"
        " synced_at, deprovisioned_at)"
        " VALUES(?,?,?,?, datetime('now'), NULL)",
        (
            "uid-jacqueline",
            "peer-instance-id",
            "jacqueline",
            "Jacqueline Williams",  # renamed since the invite
        ),
    )

    resp = await client.get(
        f"/api/spaces/{sid}/members",
        headers=_auth(client._admin_token),
    )
    members = await resp.json()
    remote = next(m for m in members if m.get("instance_id"))
    # The roster surfaces the fresh name, not the stale snapshot.
    assert remote["display_name"] == "Jacqueline Williams"


async def test_remote_member_surfaces_picture_url_when_users_sync_delivered_bytes(
    client,
):
    """``USERS_SYNC`` fans the remote user's WebP avatar bytes onto every
    paired peer; they land in the shared ``user_profile_pictures`` table
    keyed by ``user_id`` and the ``remote_users`` row picks up
    ``picture_hash``. The space member list MUST surface the resulting
    ``api/users/{user_id}/picture?v=<hash>`` URL so the SPA shows
    cross-household members with their actual avatar instead of just
    initials. Pascal's report was "all members have no profile
    pictures" — root cause was the route hardcoding ``picture_url:
    None`` on the remote row even when the bytes had arrived."""
    app = client.app
    db = app[_db_key]
    r = await client.post(
        "/api/spaces",
        json={"name": "RemoteAvatar"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    # Pair-instance + remote_user row carrying the synced picture hash.
    await db.enqueue(
        "INSERT INTO remote_instances"
        "(id, display_name, remote_identity_pk, key_self_to_remote,"
        " key_remote_to_self, remote_inbox_url, local_inbox_id, status,"
        " source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "peer-instance-id",
            "Peer",
            "00" * 32,
            "k1",
            "k2",
            "https://peer/wh",
            "wh-peer",
            "confirmed",
            "manual",
        ),
    )
    await db.enqueue(
        "INSERT INTO space_remote_members"
        "(space_id, instance_id, user_id, user_pk, display_name)"
        " VALUES(?,?,?,?,?)",
        (sid, "peer-instance-id", "uid-bob", "pk", "Bob"),
    )
    await db.enqueue(
        "INSERT INTO remote_users"
        "(user_id, instance_id, remote_username, display_name,"
        " picture_hash, synced_at, deprovisioned_at)"
        " VALUES(?,?,?,?,?, datetime('now'), NULL)",
        ("uid-bob", "peer-instance-id", "bob", "Bob", "deadbeef"),
    )

    resp = await client.get(
        f"/api/spaces/{sid}/members",
        headers=_auth(client._admin_token),
    )
    members = await resp.json()
    remote = next(m for m in members if m.get("instance_id"))
    assert remote["picture_hash"] == "deadbeef"
    assert remote["picture_url"] is not None
    assert "api/users/uid-bob/picture" in remote["picture_url"]
    assert "v=deadbeef" in remote["picture_url"]


async def test_remote_member_surfaces_personal_alias(client):
    """``personal_aliases`` keys on (viewer, target_user_id) and the
    ``alias_service`` already accepts remote ``user_id``s. The roster
    route must include remote members in the bulk alias resolve and
    pass the value through to the rendered row — without this, the
    nickname Pascal sets via the friends list never surfaces in the
    space-member list."""
    app = client.app
    db = app[_db_key]
    r = await client.post(
        "/api/spaces",
        json={"name": "RemoteAlias"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await db.enqueue(
        "INSERT INTO remote_instances"
        "(id, display_name, remote_identity_pk, key_self_to_remote,"
        " key_remote_to_self, remote_inbox_url, local_inbox_id, status,"
        " source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "peer-instance-id",
            "Peer",
            "00" * 32,
            "k1",
            "k2",
            "https://peer/wh",
            "wh-peer",
            "confirmed",
            "manual",
        ),
    )
    await db.enqueue(
        "INSERT INTO space_remote_members"
        "(space_id, instance_id, user_id, user_pk, display_name)"
        " VALUES(?,?,?,?,?)",
        (sid, "peer-instance-id", "uid-bob", "pk", "Bob"),
    )
    await db.enqueue(
        "INSERT INTO remote_users"
        "(user_id, instance_id, remote_username, display_name,"
        " synced_at, deprovisioned_at)"
        " VALUES(?,?,?,?, datetime('now'), NULL)",
        ("uid-bob", "peer-instance-id", "bob", "Bob"),
    )
    # Seed the alias as if the admin set it via PUT /api/aliases/users.
    await db.enqueue(
        "INSERT INTO user_aliases (viewer_user_id, target_user_id, alias)"
        " VALUES (?, ?, ?)",
        (client._admin_uid, "uid-bob", "Bob the Carpenter"),
    )

    resp = await client.get(
        f"/api/spaces/{sid}/members",
        headers=_auth(client._admin_token),
    )
    members = await resp.json()
    remote = next(m for m in members if m.get("instance_id"))
    assert remote["personal_alias"] == "Bob the Carpenter"


async def test_remote_member_falls_back_to_snapshot_when_users_table_missing(client):
    """When ``remote_users`` has no row for the user (the §D1b accept
    landed before USERS_SYNC, or the household never paired), the
    route falls back to the ``space_remote_members.display_name``
    snapshot rather than the raw ``user_id``."""
    app = client.app
    db = app[_db_key]
    r = await client.post(
        "/api/spaces",
        json={"name": "FallbackSnapshot"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await db.enqueue(
        "INSERT INTO space_remote_members"
        "(space_id, instance_id, user_id, display_name)"
        " VALUES(?,?,?,?)",
        (sid, "peer-x", "uid-no-remote-row", "Snapshot Name"),
    )
    # Deliberately NO remote_users row.
    resp = await client.get(
        f"/api/spaces/{sid}/members",
        headers=_auth(client._admin_token),
    )
    members = await resp.json()
    remote = next(m for m in members if m.get("instance_id"))
    assert remote["display_name"] == "Snapshot Name"


async def test_member_location_share_enabled_round_trips(client):
    """§23.8.8 — PATCHing /location-sharing flips the bit, and the
    next GET /members must surface that flip. Regression for the
    "map tab reset itself but notification settings still showed
    enabled" UX bug."""
    r = await client.post(
        "/api/spaces",
        json={"name": "LocShareRoundtrip"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    # Admin's own member row — flip the opt-in.
    patch = await client.patch(
        f"/api/spaces/{sid}/members/me/location-sharing",
        json={"enabled": True},
        headers=_auth(client._admin_token),
    )
    assert patch.status == 200
    # Read it back via the list endpoint the SPA uses on cold load.
    resp = await client.get(
        f"/api/spaces/{sid}/members",
        headers=_auth(client._admin_token),
    )
    members = await resp.json()
    me = next(m for m in members if m["user_id"] == client._admin_uid)
    assert me["location_share_enabled"] is True


async def test_remove_member(client):
    """DELETE /api/spaces/{id}/members/{user_id} removes the member."""
    r = await client.post(
        "/api/spaces",
        json={"name": "RemoveMember"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await _seat_local_member(client, sid, client._bob_token, client._bob_uid)
    resp = await client.delete(
        f"/api/spaces/{sid}/members/{client._bob_uid}",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200


async def test_ban_member(client):
    """POST /api/spaces/{id}/ban bans a user from the space."""
    r = await client.post(
        "/api/spaces",
        json={"name": "BanSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await _seat_local_member(client, sid, client._bob_token, client._bob_uid)
    resp = await client.post(
        f"/api/spaces/{sid}/ban",
        json={"user_id": client._bob_uid, "reason": "spam"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200


async def test_create_invite_token(client):
    """POST /api/spaces/{id}/invite-tokens creates an invite token."""
    r = await client.post(
        "/api/spaces",
        json={"name": "InviteSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={"uses": 1},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    body = await resp.json()
    assert "token" in body


async def test_create_invite_token_rejects_bad_ttl(client):
    """``ttl_seconds`` bounds the link's life; garbage is a 422, not a
    silently-immortal token."""
    r = await client.post(
        "/api/spaces",
        json={"name": "TtlSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    for bad in ("soon", -1):
        resp = await client.post(
            f"/api/spaces/{sid}/invite-tokens",
            json={"uses": 1, "ttl_seconds": bad},
            headers=_auth(client._admin_token),
        )
        assert resp.status == 422


async def test_create_invite_token_with_ttl_expires(client):
    """A token minted with a 1-second TTL is dead on arrival a moment
    later — proof the value reaches the DB row, not just the API."""
    import asyncio

    r = await client.post(
        "/api/spaces",
        json={"name": "TtlSpace2"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={"uses": 1, "ttl_seconds": 1},
        headers=_auth(client._admin_token),
    )
    token = (await r2.json())["token"]
    await asyncio.sleep(1.5)
    resp = await client.post(
        "/api/spaces/join",
        json={"token": token},
        headers=_auth(client._bob_token),
    )
    assert resp.status >= 400


async def test_join_via_invite_token(client):
    """POST /api/spaces/join with a valid token adds the user as member."""
    r = await client.post(
        "/api/spaces",
        json={"name": "JoinSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={"uses": 1},
        headers=_auth(client._admin_token),
    )
    token = (await r2.json())["token"]
    resp = await client.post(
        "/api/spaces/join",
        json={"token": token},
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    assert (await resp.json())["role"] == "member"


async def test_join_forwards_issuer_instance_id_to_service(client, monkeypatch):
    """POST /api/spaces/join with a foreign ``issuer_instance_id`` forwards
    the field to ``space_service.redeem_invite_token`` and surfaces the
    service's ``{space_id, role}`` response on success."""
    from socialhome.services.space_service import SpaceService

    captured: dict = {}

    async def _fake_redeem(
        self, token, *, user_id, issuer_instance_id=None, bootstrap=None
    ):
        captured["token"] = token
        captured["user_id"] = user_id
        captured["issuer_instance_id"] = issuer_instance_id
        return {"space_id": "sp-remote", "role": "member"}

    monkeypatch.setattr(SpaceService, "redeem_invite_token", _fake_redeem)
    resp = await client.post(
        "/api/spaces/join",
        json={"token": "tkn", "issuer_instance_id": "issuer-abc"},
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"space_id": "sp-remote", "role": "member"}
    assert captured["issuer_instance_id"] == "issuer-abc"
    assert captured["token"] == "tkn"
    assert captured["user_id"] == client._bob_uid


async def test_join_remote_redeem_denied_maps_to_422(client, monkeypatch):
    """A ``SpacePermissionError`` from the cross-instance path (token
    denied / expired / issuer unpaired) maps to 422 with ``REDEEM_DENIED``
    so the SPA can render the issuer's reason verbatim."""
    from socialhome.domain.space import SpacePermissionError
    from socialhome.services.space_service import SpaceService

    async def _fake_redeem(
        self, token, *, user_id, issuer_instance_id=None, bootstrap=None
    ):
        raise SpacePermissionError(
            "invite token invalid, expired, or exhausted",
        )

    monkeypatch.setattr(SpaceService, "redeem_invite_token", _fake_redeem)
    resp = await client.post(
        "/api/spaces/join",
        json={"token": "tkn", "issuer_instance_id": "issuer-abc"},
        headers=_auth(client._bob_token),
    )
    assert resp.status == 422
    body = await resp.json()
    assert body["error"]["code"] == "REDEEM_DENIED"
    assert "expired" in body["error"]["detail"]


async def test_join_remote_redeem_timeout_maps_to_504(client, monkeypatch):
    """A ``TimeoutError`` (issuer didn't ACK / DENY in time) maps to 504."""
    from socialhome.services.space_service import SpaceService

    async def _fake_redeem(
        self, token, *, user_id, issuer_instance_id=None, bootstrap=None
    ):
        raise TimeoutError("issuer did not respond")

    monkeypatch.setattr(SpaceService, "redeem_invite_token", _fake_redeem)
    resp = await client.post(
        "/api/spaces/join",
        json={"token": "tkn", "issuer_instance_id": "issuer-abc"},
        headers=_auth(client._bob_token),
    )
    assert resp.status == 504
    body = await resp.json()
    assert body["error"]["code"] == "ISSUER_TIMEOUT"


async def test_join_local_ban_still_maps_to_403(client, monkeypatch):
    """A local-only join attempt where the user is banned keeps the
    existing 403 ``FORBIDDEN`` mapping — the 422 remap only applies to
    cross-instance redeems."""
    from socialhome.domain.space import SpacePermissionError
    from socialhome.services.space_service import SpaceService

    async def _fake_redeem(
        self, token, *, user_id, issuer_instance_id=None, bootstrap=None
    ):
        raise SpacePermissionError("banned from this space", banned=True)

    monkeypatch.setattr(SpaceService, "redeem_invite_token", _fake_redeem)
    resp = await client.post(
        "/api/spaces/join",
        json={"token": "tkn"},  # no issuer_instance_id → local path
        headers=_auth(client._bob_token),
    )
    assert resp.status == 403


async def test_create_space_post(client):
    """POST /api/spaces/{id}/posts creates a post in the space."""
    r = await client.post(
        "/api/spaces",
        json={"name": "PostSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "space hello"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201


async def test_get_space_feed(client):
    """GET /api/spaces/{id}/feed returns the space feed."""
    r = await client.post(
        "/api/spaces",
        json={"name": "FeedSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "feed post"},
        headers=_auth(client._admin_token),
    )
    resp = await client.get(
        f"/api/spaces/{sid}/feed",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    feed = await resp.json()
    assert len(feed) == 1


async def _space_with_video_post(client) -> tuple[str, str, str]:
    """Create a space + a video post + a matching transcode row.

    Returns ``(space_id, post_id, output_fn)``. Stops the scheduler so
    the row stays put while the test inspects readiness.
    """
    from socialhome.app_keys import (
        media_transcode_repo_key,
        media_transcode_service_key,
    )

    await client.app[media_transcode_service_key].stop()
    r = await client.post(
        "/api/spaces",
        json={"name": "ClipSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    fn = "spcvid00000000000000000000000.webm"
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "video", "media_url": f"api/media/{fn}"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201, await r.text()
    post_id = (await r.json())["id"]
    await client.app[media_transcode_repo_key].enqueue(
        output_filename=fn,
        source_path="/tmp/src.bin",
        thumbnail_filename="thumb.webp",
        owner_user_id=client._admin_uid,
    )
    return sid, post_id, fn


async def _space_feed(client, sid: str) -> list[dict]:
    r = await client.get(f"/api/spaces/{sid}/feed", headers=_auth(client._admin_token))
    assert r.status == 200
    return await r.json()


async def test_space_feed_video_media_status_processing(client):
    from socialhome.app_keys import media_transcode_repo_key

    sid, post_id, fn = await _space_with_video_post(client)
    await client.app[media_transcode_repo_key].mark_processing(fn)
    p = next(x for x in await _space_feed(client, sid) if x["id"] == post_id)
    assert p["type"] == "video"
    assert p["media_status"] == "processing"


async def test_space_feed_video_media_status_ready_after_complete(client):
    from socialhome.app_keys import media_transcode_repo_key

    sid, post_id, fn = await _space_with_video_post(client)
    await client.app[media_transcode_repo_key].complete(fn)
    p = next(x for x in await _space_feed(client, sid) if x["id"] == post_id)
    assert p["media_status"] == "ready"


async def test_space_feed_video_media_status_failed(client):
    from socialhome.app_keys import media_transcode_repo_key

    sid, post_id, fn = await _space_with_video_post(client)
    await client.app[media_transcode_repo_key].mark_failed(fn, "boom")
    p = next(x for x in await _space_feed(client, sid) if x["id"] == post_id)
    assert p["media_status"] == "failed"


async def test_space_feed_text_post_has_no_processing_status(client):
    sid, _post_id, _fn = await _space_with_video_post(client)
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "words"},
        headers=_auth(client._admin_token),
    )
    text_id = (await r.json())["id"]
    p = next(x for x in await _space_feed(client, sid) if x["id"] == text_id)
    assert p.get("media_status") != "processing"


async def test_space_feed_video_post_has_signed_poster(client):
    sid, post_id, _fn = await _space_with_video_post(client)
    p = next(x for x in await _space_feed(client, sid) if x["id"] == post_id)
    poster = p["media_thumbnail_url"]
    base = poster.split("?", 1)[0]
    assert base == "api/media/spcvid00000000000000000000000.webp"
    media_base = p["media_url"].split("?", 1)[0]
    assert base[: -len(".webp")] == media_base[: -len(".webm")]
    assert "exp=" in poster and "sig=" in poster


async def test_space_feed_text_post_has_no_poster(client):
    sid, _post_id, _fn = await _space_with_video_post(client)
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "words"},
        headers=_auth(client._admin_token),
    )
    text_id = (await r.json())["id"]
    p = next(x for x in await _space_feed(client, sid) if x["id"] == text_id)
    assert "media_thumbnail_url" not in p


async def test_create_space_empty_name_422(client):
    """POST /api/spaces with an empty name returns 422."""
    resp = await client.post(
        "/api/spaces",
        json={"name": ""},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_get_nonexistent_space_404(client):
    """GET /api/spaces/{id} for unknown id returns 404."""
    resp = await client.get(
        "/api/spaces/no-such-space-id",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 404


async def test_non_owner_cannot_dissolve_403(client):
    """DELETE /api/spaces/{id} by a non-owner returns 403."""
    r = await client.post(
        "/api/spaces",
        json={"name": "OwnerOnly"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    resp = await client.delete(
        f"/api/spaces/{sid}",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 403


# ─── New admin wiring: list / role / leave-me / ownership / join-requests / reactions ──


async def test_list_spaces_returns_empty_when_no_memberships(client):
    r = await client.get("/api/spaces", headers=_auth(client._bob_token))
    assert r.status == 200
    assert await r.json() == []


async def test_list_spaces_returns_members_spaces(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "Crew"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.get("/api/spaces", headers=_auth(client._admin_token))
    assert r.status == 200
    rows = await r.json()
    assert any(row["id"] == sid for row in rows)


async def test_leave_via_members_me(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "LeaveMe"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await _seat_local_member(client, sid, client._bob_token, client._bob_uid)
    r = await client.delete(
        f"/api/spaces/{sid}/members/me",
        headers=_auth(client._bob_token),
    )
    assert r.status == 200


async def test_set_role_owner_promotes_admin(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "Promote"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await _seat_local_member(client, sid, client._bob_token, client._bob_uid)
    r = await client.patch(
        f"/api/spaces/{sid}/members/{client._bob_uid}",
        json={"role": "admin"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    assert (await r.json())["role"] == "admin"


async def test_set_role_non_owner_forbidden(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "RoleGuard"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await _seat_local_member(client, sid, client._bob_token, client._bob_uid)
    r = await client.patch(
        f"/api/spaces/{sid}/members/{client._admin_uid}",
        json={"role": "admin"},
        headers=_auth(client._bob_token),
    )
    assert r.status == 403


async def test_set_role_invalid_value_422(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "BadRole"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await _seat_local_member(client, sid, client._bob_token, client._bob_uid)
    r = await client.patch(
        f"/api/spaces/{sid}/members/{client._bob_uid}",
        json={"role": "overlord"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 422


async def test_ownership_transfer_owner_only(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "Transfer"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    await _seat_local_member(client, sid, client._bob_token, client._bob_uid)
    r = await client.post(
        f"/api/spaces/{sid}/ownership",
        json={"to_user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    body = await r.json()
    assert body["new_owner_user_id"] == client._bob_uid


async def test_ownership_transfer_requires_to_user_id(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "TransferBad"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/ownership",
        json={},
        headers=_auth(client._admin_token),
    )
    assert r.status == 422


async def test_join_request_lifecycle(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "OpenJoin", "join_mode": "request"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/join-requests",
        json={"message": "please"},
        headers=_auth(client._bob_token),
    )
    assert r.status == 201
    request_id = (await r.json())["request_id"]
    r = await client.get(
        f"/api/spaces/{sid}/join-requests",
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    assert len(await r.json()) == 1
    r = await client.post(
        f"/api/spaces/{sid}/join-requests/{request_id}/approve",
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    r = await client.get(
        f"/api/spaces/{sid}/members",
        headers=_auth(client._admin_token),
    )
    assert any(m["user_id"] == client._bob_uid for m in await r.json())


async def test_join_request_deny_closes_flow(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "DenyMe", "join_mode": "request"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/join-requests",
        json={},
        headers=_auth(client._bob_token),
    )
    request_id = (await r.json())["request_id"]
    r = await client.post(
        f"/api/spaces/{sid}/join-requests/{request_id}/deny",
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    assert (await r.json())["status"] == "denied"


async def test_join_request_unknown_action_422(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "Bad", "join_mode": "request"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/join-requests",
        json={},
        headers=_auth(client._bob_token),
    )
    request_id = (await r.json())["request_id"]
    r = await client.post(
        f"/api/spaces/{sid}/join-requests/{request_id}/smite",
        headers=_auth(client._admin_token),
    )
    assert r.status == 422


async def test_join_requests_list_requires_admin(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "Guarded"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.get(
        f"/api/spaces/{sid}/join-requests",
        headers=_auth(client._bob_token),
    )
    assert r.status == 403


async def test_space_post_reaction_add_and_remove(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "Reacts"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "Hi"},
        headers=_auth(client._admin_token),
    )
    pid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/posts/{pid}/reactions",
        json={"emoji": "👍"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201
    from urllib.parse import quote

    r = await client.delete(
        f"/api/spaces/{sid}/posts/{pid}/reactions/{quote('👍')}",
        headers=_auth(client._admin_token),
    )
    assert r.status == 200


async def test_space_post_comment_happy_path(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "Chatty"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "Topic"},
        headers=_auth(client._admin_token),
    )
    pid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/posts/{pid}/comments",
        json={"content": "Nice!"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201
    assert (await r.json())["content"] == "Nice!"


async def test_space_post_comment_empty_content_422(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "EmptyComment"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "Topic"},
        headers=_auth(client._admin_token),
    )
    pid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/posts/{pid}/comments",
        json={"content": ""},
        headers=_auth(client._admin_token),
    )
    assert r.status == 422


# ─── About markdown + cover image ───────────────────────────────────────

# Tiny valid PNG so the ImageProcessor validates magic bytes and Pillow
# can decode it into a WebP. 1×1 pixel fully transparent.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
    "426082"
)


async def test_space_about_markdown_roundtrips(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "WithAbout"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    patch = await client.patch(
        f"/api/spaces/{sid}",
        json={"about_markdown": "## Welcome\n\n**bold** text."},
        headers=_auth(client._admin_token),
    )
    assert patch.status == 200
    get = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    body = await get.json()
    assert body["about_markdown"] == "## Welcome\n\n**bold** text."


async def test_space_cover_upload_and_fetch(client, tmp_path):
    r = await client.post(
        "/api/spaces",
        json={"name": "WithCover"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]

    # Upload via multipart.
    form = aiohttp.FormData()
    form.add_field(
        "file",
        _TINY_PNG,
        filename="x.png",
        content_type="image/png",
    )
    up = await client.post(
        f"/api/spaces/{sid}/cover",
        data=form,
        headers=_auth(client._admin_token),
    )
    assert up.status == 200
    body = await up.json()
    assert body["cover_hash"]
    assert body["cover_url"].startswith(f"/api/spaces/{sid}/cover?v=")

    # GET streams WebP bytes.
    fetch = await client.get(
        f"/api/spaces/{sid}/cover",
        headers=_auth(client._admin_token),
    )
    assert fetch.status == 200
    assert fetch.headers["Content-Type"] == "image/webp"
    payload = await fetch.read()
    assert payload[:4] == b"RIFF"  # WebP magic

    # GET space detail shows cover_url.
    detail = await client.get(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    dbody = await detail.json()
    assert dbody["cover_hash"] == body["cover_hash"]
    # Both responses sign the URL fresh (different ``exp`` values), so
    # compare canonical paths + cache-buster, not the full string.
    upload_canonical = body["cover_url"].split("&exp=", 1)[0]
    detail_canonical = dbody["cover_url"].split("&exp=", 1)[0]
    assert upload_canonical == detail_canonical
    assert "sig=" in dbody["cover_url"]

    # DELETE clears it.
    rm = await client.delete(
        f"/api/spaces/{sid}/cover",
        headers=_auth(client._admin_token),
    )
    assert rm.status == 204
    fetch2 = await client.get(
        f"/api/spaces/{sid}/cover",
        headers=_auth(client._admin_token),
    )
    assert fetch2.status == 404


async def test_space_icon_upload_fetch_and_clear(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "WithIcon"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    form = aiohttp.FormData()
    form.add_field("file", _TINY_PNG, filename="x.png", content_type="image/png")
    up = await client.post(
        f"/api/spaces/{sid}/icon", data=form, headers=_auth(client._admin_token)
    )
    assert up.status == 200
    body = await up.json()
    assert body["icon_hash"]
    assert body["icon_url"].startswith(f"/api/spaces/{sid}/icon?v=")

    fetch = await client.get(
        f"/api/spaces/{sid}/icon", headers=_auth(client._admin_token)
    )
    assert fetch.status == 200
    assert fetch.headers["Content-Type"] == "image/webp"
    assert (await fetch.read())[:4] == b"RIFF"

    detail = await client.get(f"/api/spaces/{sid}", headers=_auth(client._admin_token))
    dbody = await detail.json()
    assert dbody["icon_hash"] == body["icon_hash"]
    assert "sig=" in dbody["icon_url"]

    rm = await client.delete(
        f"/api/spaces/{sid}/icon", headers=_auth(client._admin_token)
    )
    assert rm.status == 204
    assert (
        await client.get(f"/api/spaces/{sid}/icon", headers=_auth(client._admin_token))
    ).status == 404


async def test_space_cover_non_admin_forbidden(client):
    r = await client.post(
        "/api/spaces",
        json={"name": "LockedCover"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    # Add bob as a plain member.
    await _seat_local_member(client, sid, client._bob_token, client._bob_uid)
    form = aiohttp.FormData()
    form.add_field(
        "file",
        _TINY_PNG,
        filename="x.png",
        content_type="image/png",
    )
    r2 = await client.post(
        f"/api/spaces/{sid}/cover",
        data=form,
        headers=_auth(client._bob_token),
    )
    assert r2.status == 403


# ── Subscriptions (read-only membership) ─────────────────────────────────


async def _create_subscribable_space(client, name: str = "Global") -> str:
    """Create a space subscribers are allowed to join — ``global`` has no
    lat/lon requirement so it's the cleanest fixture for these tests.

    Readability is an explicit owner opt-in that defaults OFF, so the space
    is PATCHed to turn ``allow_subscribers`` on; without it
    ``POST /api/spaces/{id}/subscribe`` is refused.
    """
    r = await client.post(
        "/api/spaces",
        json={"name": name, "space_type": "global"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201, await r.text()
    sid = (await r.json())["id"]
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"allow_subscribers": True}},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200, await r.text()
    return sid


async def test_subscribe_refused_when_the_space_takes_no_followers(client):
    """A freshly created global space has ``allow_subscribers`` OFF, so its
    content is relayed nowhere and no content key is sealed to a follower —
    a subscribe would seat someone who receives nothing forever."""
    r = await client.post(
        "/api/spaces",
        json={"name": "Quiet Global", "space_type": "global"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/subscribe",
        headers=_auth(client._bob_token),
    )
    assert r.status == 403
    assert "does not allow subscribers" in (await r.text())


async def test_subscribe_allowed_once_the_owner_opts_in(client):
    """…and the same space accepts a subscriber the moment the owner flips
    the switch — no join-mode change involved."""
    sid = await _create_subscribable_space(client, name="Opened Up")
    r = await client.post(
        f"/api/spaces/{sid}/subscribe",
        headers=_auth(client._bob_token),
    )
    assert r.status == 200
    assert (await r.json())["subscribed"] is True


async def test_subscribe_and_unsubscribe_space(client):
    sid = await _create_subscribable_space(client)

    r = await client.post(
        f"/api/spaces/{sid}/subscribe",
        headers=_auth(client._bob_token),
    )
    assert r.status == 200
    assert (await r.json())["subscribed"] is True

    r = await client.get("/api/me/subscriptions", headers=_auth(client._bob_token))
    assert r.status == 200
    body = await r.json()
    assert [row["space_id"] for row in body["subscriptions"]] == [sid]

    r = await client.delete(
        f"/api/spaces/{sid}/subscribe",
        headers=_auth(client._bob_token),
    )
    assert r.status == 200
    assert (await r.json())["subscribed"] is False

    r = await client.get("/api/me/subscriptions", headers=_auth(client._bob_token))
    assert (await r.json())["subscriptions"] == []


async def test_subscribe_is_idempotent(client):
    sid = await _create_subscribable_space(client)
    for _ in range(3):
        r = await client.post(
            f"/api/spaces/{sid}/subscribe",
            headers=_auth(client._bob_token),
        )
        assert r.status == 200
    subs = await (
        await client.get("/api/me/subscriptions", headers=_auth(client._bob_token))
    ).json()
    assert len(subs["subscriptions"]) == 1


async def test_subscriptions_scoped_per_user(client):
    sid = await _create_subscribable_space(client)
    await client.post(
        f"/api/spaces/{sid}/subscribe",
        headers=_auth(client._bob_token),
    )
    # admin owns the space but isn't a subscriber of it.
    r = await client.get("/api/me/subscriptions", headers=_auth(client._admin_token))
    assert (await r.json())["subscriptions"] == []


async def test_subscribe_private_space_rejected(client):
    """Private spaces cannot be subscribed to — 403."""
    r = await client.post(
        "/api/spaces",
        json={"name": "Priv", "space_type": "private"},
        headers=_auth(client._admin_token),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/subscribe",
        headers=_auth(client._bob_token),
    )
    assert r.status == 403


async def test_subscriber_cannot_post_in_space(client):
    """Subscriber hitting the post-create route gets 403 — integration
    proof that the service-level read-only gate surfaces correctly."""
    sid = await _create_subscribable_space(client)
    await client.post(
        f"/api/spaces/{sid}/subscribe",
        headers=_auth(client._bob_token),
    )
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "should block"},
        headers=_auth(client._bob_token),
    )
    assert r.status == 403


async def test_subscribe_requires_auth(client):
    r = await client.post("/api/spaces/any/subscribe")
    assert r.status == 401
    r2 = await client.get("/api/me/subscriptions")
    assert r2.status == 401


# ─── GET /api/me/join-requests (Fix A — pending survives reload) ──────────


async def test_my_join_requests_requires_auth(client):
    r = await client.get("/api/me/join-requests")
    assert r.status == 401


async def test_my_join_requests_lists_only_own_pending(client):
    """A user's own pending join-requests surface; approved rows and
    other users' rows don't."""
    from socialhome.app_keys import space_repo_key
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    repo: SqliteSpaceRepo = client.app[space_repo_key]

    async def _space(sid: str) -> None:
        await repo.save(
            Space(
                id=sid,
                name=sid,
                owner_instance_id="inst-x",
                owner_username="pascal",
                identity_public_key="aabb" * 16,
                config_sequence=0,
                features=SpaceFeatures(),
                space_type=SpaceType.PUBLIC,
                join_mode=JoinMode.REQUEST,
            )
        )

    for sid in ("sp-pend-1", "sp-pend-2", "sp-done", "sp-admin"):
        await _space(sid)

    # Bob has two pending requests + one approved (excluded).
    await repo.save_join_request("sp-pend-1", client._bob_uid)
    await repo.save_join_request("sp-pend-2", client._bob_uid)
    rid = await repo.save_join_request("sp-done", client._bob_uid)
    await repo.update_join_request_status(rid, "approved")
    # Admin has a pending request → must not leak into bob's list.
    await repo.save_join_request("sp-admin", client._admin_uid)

    r = await client.get("/api/me/join-requests", headers=_auth(client._bob_token))
    assert r.status == 200
    body = await r.json()
    assert set(body["pending_space_ids"]) == {"sp-pend-1", "sp-pend-2"}
    assert "sp-done" not in body["pending_space_ids"]
    assert "sp-admin" not in body["pending_space_ids"]


async def test_my_join_requests_empty(client):
    r = await client.get("/api/me/join-requests", headers=_auth(client._bob_token))
    assert r.status == 200
    assert (await r.json())["pending_space_ids"] == []


async def test_subscribe_maps_gfs_outage_to_502(client):
    """A GFS-discovered space whose GFS is unreachable is a 502, not a 500.

    ``GfsConnectionError`` escapes ``subscribe_to_space`` when the paired
    global server refuses or times out. It is a domain exception, so
    ``BaseView._iter`` maps it centrally — a handler-level try/except would
    violate the routing convention. 502 + GFS_UNAVAILABLE matches the
    HighlightPublicationError mapping: an upstream we depend on failed, not
    a bad request from the caller.
    """
    from socialhome.app_keys import space_service_key
    from socialhome.services.gfs_connection_service import GfsConnectionError
    from socialhome.services.space_service import stub_space_from_metadata

    svc = client.server.app[space_service_key]
    # An id with NO local row — that is what makes subscribe consult the
    # mirror at all (an already-local space skips it).
    sid = "f" * 32

    class _OutageMirror:
        """Seats the local mirror, then the GFS subscribe fails."""

        def __init__(self, space_service) -> None:
            self._svc = space_service

        async def ensure_mirror(self, space_id: str):
            space = stub_space_from_metadata(
                space_id,
                host_instance_id="inst-remote",
                meta={
                    "name": "Outage Space",
                    "space_type": "global",
                    "join_mode": "invite_only",
                    # Invite-only AND readable — the broadcast shape a GFS
                    # directory really does list; the mirror carries the
                    # owner's flag through so the local gate agrees.
                    "features": {"allow_subscribers": True},
                    "identity_public_key": "ab" * 32,
                    "owner_username": "",
                },
            )
            await self._svc._spaces.save(space)
            return (space, "gfs-1")

        async def subscribe_to_gfs(self, space_id: str, gfs_id: str) -> None:
            raise GfsConnectionError("global server unreachable")

        async def unsubscribe(self, space_id: str) -> None:
            return None

    svc.attach_gfs_space_mirror(_OutageMirror(svc))

    r = await client.post(
        f"/api/spaces/{sid}/subscribe",
        headers=_auth(client._bob_token),
    )
    assert r.status == 502, await r.text()
    assert (await r.json())["error"]["code"] == "GFS_UNAVAILABLE"


# ── Invite-token TTL ───────────────────────────────────────────────────────


async def _invite_space(client) -> str:
    r = await client.post(
        "/api/spaces",
        json={"name": "TtlSpace"},
        headers=_auth(client._admin_token),
    )
    return (await r.json())["id"]


async def _token_expiry(client, token: str) -> str | None:
    row = await client.app[_db_key].fetchone(
        "SELECT expires_at FROM space_invite_tokens WHERE token=?", (token,)
    )
    return row["expires_at"]


async def test_invite_token_gets_default_ttl(client):
    """An invite token minted with no ``ttl_seconds`` expires by default."""
    sid = await _invite_space(client)
    resp = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={"uses": 1},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    raw = await _token_expiry(client, (await resp.json())["token"])
    assert raw is not None
    delta = datetime.fromisoformat(raw) - datetime.now(timezone.utc)
    assert timedelta(days=6) < delta <= timedelta(days=7)


async def test_invite_token_honours_ttl_seconds(client):
    """An explicit ``ttl_seconds`` is anchored on the server clock."""
    sid = await _invite_space(client)
    resp = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={"ttl_seconds": 60},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    raw = await _token_expiry(client, (await resp.json())["token"])
    delta = datetime.fromisoformat(raw) - datetime.now(timezone.utc)
    assert timedelta(seconds=0) < delta <= timedelta(seconds=60)


async def test_invite_token_explicit_null_ttl_never_expires(client):
    """``ttl_seconds: null`` keeps the historical uses-only behaviour."""
    sid = await _invite_space(client)
    resp = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={"uses": 3, "ttl_seconds": None},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    assert await _token_expiry(client, (await resp.json())["token"]) is None


@pytest.mark.parametrize("bad", [-1, "soon", [5]])
async def test_invite_token_rejects_bad_ttl(client, bad):
    """A negative or non-integer ``ttl_seconds`` is a 422.

    ``0`` is deliberately absent: the SPA's expiry picker sends it for
    "Never", so the route maps it onto the service's ``None`` — see
    :func:`test_ttl_seconds_zero_means_never_expires`."""
    sid = await _invite_space(client)
    resp = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={"ttl_seconds": bad},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_expired_invite_token_cannot_be_redeemed(client):
    """End-to-end: a token whose TTL has passed is refused at join time."""
    sid = await _invite_space(client)
    resp = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={"ttl_seconds": 300},
        headers=_auth(client._admin_token),
    )
    token = (await resp.json())["token"]
    # Pinned to the start of the current UTC day: expired for certain, and
    # on the same calendar date as SQLite's ``datetime('now')`` — the only
    # window where the old raw TEXT compare went wrong.
    expired = f"{datetime.now(timezone.utc).date().isoformat()}T00:00:00.000001+00:00"
    await client.app[_db_key].enqueue(
        "UPDATE space_invite_tokens SET expires_at=? WHERE token=?",
        (expired, token),
    )
    join = await client.post(
        "/api/spaces/join",
        json={"token": token},
        headers=_auth(client._bob_token),
    )
    assert join.status >= 400


async def test_list_spaces_includes_features_block(client):
    """``GET /api/spaces`` ships the same ``features`` dict as the detail.

    The space browser reads ``features.allow_subscribers`` off the list row
    to decide whether a local public space wears the 🔒 "Content is private"
    chip. When the list withheld the block the chip lied on every local
    space, and the SPA papered over it with a per-space detail fetch.
    """
    r = await client.post(
        "/api/spaces",
        json={"name": "Readable", "space_type": "public"},
        headers=_auth(client._admin_token),
    )
    open_sid = (await r.json())["id"]
    r = await client.post(
        "/api/spaces",
        json={"name": "Closed", "space_type": "public"},
        headers=_auth(client._admin_token),
    )
    closed_sid = (await r.json())["id"]
    # Owner opts one of the two into followers; the other keeps the default.
    r = await client.patch(
        f"/api/spaces/{open_sid}",
        json={"features": {"allow_subscribers": True}},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200, await r.text()

    r = await client.get("/api/spaces", headers=_auth(client._admin_token))
    assert r.status == 200
    rows = {row["id"]: row for row in await r.json()}
    assert rows[open_sid]["features"]["allow_subscribers"] is True
    assert rows[closed_sid]["features"]["allow_subscribers"] is False
    # …and the block is the full detail shape, not a one-key special case.
    r = await client.get(f"/api/spaces/{open_sid}", headers=_auth(client._admin_token))
    assert rows[open_sid]["features"] == (await r.json())["features"]


async def test_join_builds_a_bootstrap_hint_from_an_invite_link(client, monkeypatch):
    """§D2b — a code minted for a stranger carries the issuer's public keys
    and the connection server that served it; the view turns those into the
    hint the service falls back to when no pairing and no mesh route
    reaches the issuer."""
    from socialhome.services.space_service import SpaceService

    captured: dict = {}

    async def _fake_redeem(
        self, token, *, user_id, issuer_instance_id=None, bootstrap=None
    ):
        captured["bootstrap"] = bootstrap
        return {"space_id": "sp-remote", "role": "member"}

    monkeypatch.setattr(SpaceService, "redeem_invite_token", _fake_redeem)
    resp = await client.post(
        "/api/spaces/join",
        json={
            "token": "tkn",
            "space_id": "sp-remote",
            "issuer_instance_id": "issuer-abc",
            "issuer_identity_pk": "aa" * 32,
            "issuer_keywrap_pk": "bb" * 32,
            "issuer_keywrap_sig": "c2ln",
            "issuer_proto_version": 29,
            "expires_at": "2026-12-01T00:00:00+00:00",
            "gfs": "https://relay.example.org",
        },
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    hint = captured["bootstrap"]
    assert hint is not None
    assert hint.instance_id == "issuer-abc"
    assert hint.invite_token == "tkn"
    assert hint.space_id == "sp-remote"
    assert hint.identity_pk == "aa" * 32
    assert hint.keywrap_pk == "bb" * 32
    assert hint.keywrap_sig == "c2ln"
    assert hint.proto_version == 29
    assert hint.expires_at == "2026-12-01T00:00:00+00:00"
    assert hint.gfs_url == "https://relay.example.org"


async def test_join_without_the_key_block_sends_no_hint(client, monkeypatch):
    """An older code (or one missing any of the three key fields) redeems
    over the direct / mesh paths exactly as before — there is nothing to
    seal to, so no hint is built."""
    from socialhome.services.space_service import SpaceService

    captured: dict = {}

    async def _fake_redeem(
        self, token, *, user_id, issuer_instance_id=None, bootstrap=None
    ):
        captured["bootstrap"] = bootstrap
        return {"space_id": "sp-remote", "role": "member"}

    monkeypatch.setattr(SpaceService, "redeem_invite_token", _fake_redeem)
    resp = await client.post(
        "/api/spaces/join",
        json={
            "token": "tkn",
            "issuer_instance_id": "issuer-abc",
            # key-wrap signature missing → not enough to seal safely
            "issuer_identity_pk": "aa" * 32,
            "issuer_keywrap_pk": "bb" * 32,
        },
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    assert captured["bootstrap"] is None


async def test_join_relay_unavailable_maps_to_422(client, monkeypatch):
    """No connection server can carry the sealed blob → the user reads the
    reason, not a ten-second timeout."""
    from socialhome.services.gfs_envelope_sender import EnvelopeRelayUnavailable
    from socialhome.services.space_service import SpaceService

    async def _fake_redeem(
        self, token, *, user_id, issuer_instance_id=None, bootstrap=None
    ):
        raise EnvelopeRelayUnavailable(
            "this connection server can't relay invites yet",
        )

    monkeypatch.setattr(SpaceService, "redeem_invite_token", _fake_redeem)
    resp = await client.post(
        "/api/spaces/join",
        json={"token": "tkn", "issuer_instance_id": "issuer-abc"},
        headers=_auth(client._bob_token),
    )
    assert resp.status == 422
    body = await resp.json()
    assert body["error"]["code"] == "REDEEM_DENIED"
    assert "can't relay invites yet" in body["error"]["detail"]


# ── Invite links: role, list, revoke ──────────────────────────────────


async def _a_space(client, name: str) -> str:
    r = await client.post(
        "/api/spaces",
        json={"name": name},
        headers=_auth(client._admin_token),
    )
    return (await r.json())["id"]


async def test_mint_invite_link_returns_the_full_shape(client):
    """201 carries everything the SPA renders: the seat, the counters,
    and the ``socialhome://invite#…`` code."""
    import base64
    import json

    sid = await _a_space(client, "LinkShape")
    resp = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={"role": "subscriber", "uses": 3},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["role"] == "subscriber"
    assert body["uses_remaining"] == 3
    assert body["gfs"] is None
    assert body["created_by"] and body["created_at"] and body["expires_at"]
    blob = body["code"].split("#", 1)[1]
    decoded = json.loads(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)))
    assert decoded["token"] == body["token"]
    # The bootstrap block carries this household's REAL published
    # identity triple — the same one /gfs/info serves — so a stranger
    # can seal a redeem to us off a pasted code.
    db = client.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key, keywrap_public_key, keywrap_sig "
        "FROM instance_identity WHERE id='self'"
    )
    assert decoded["issuer_identity_pk"] == row["identity_public_key"]
    assert decoded["issuer_keywrap_pk"] == row["keywrap_public_key"]
    assert decoded["issuer_keywrap_sig"] == row["keywrap_sig"]


async def test_mint_invite_link_defaults_to_member(client):
    sid = await _a_space(client, "LinkDefault")
    resp = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    assert (await resp.json())["role"] == "member"


async def test_mint_invite_link_rejects_owner_and_unknown_roles(client):
    sid = await _a_space(client, "LinkBadRole")
    for bad in ("owner", "superuser"):
        resp = await client.post(
            f"/api/spaces/{sid}/invite-tokens",
            json={"role": bad},
            headers=_auth(client._admin_token),
        )
        assert resp.status == 422, bad


async def test_list_invite_links_is_admin_only(client):
    sid = await _a_space(client, "LinkList")
    await _seat_local_member(client, sid, client._bob_token, client._bob_uid)
    r = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={},
        headers=_auth(client._admin_token),
    )
    token = (await r.json())["token"]
    listed = await client.get(
        f"/api/spaces/{sid}/invite-tokens",
        headers=_auth(client._admin_token),
    )
    assert listed.status == 200
    assert [t["token"] for t in (await listed.json())["tokens"]] == [token]
    # A plain member cannot read the space's bearer credentials.
    denied = await client.get(
        f"/api/spaces/{sid}/invite-tokens",
        headers=_auth(client._bob_token),
    )
    assert denied.status == 403


async def test_revoke_invite_link_is_204_and_idempotent(client):
    sid = await _a_space(client, "LinkRevoke")
    r = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={},
        headers=_auth(client._admin_token),
    )
    token = (await r.json())["token"]
    first = await client.delete(
        f"/api/spaces/{sid}/invite-tokens/{token}",
        headers=_auth(client._admin_token),
    )
    assert first.status == 204
    second = await client.delete(
        f"/api/spaces/{sid}/invite-tokens/{token}",
        headers=_auth(client._admin_token),
    )
    assert second.status == 204
    listed = await client.get(
        f"/api/spaces/{sid}/invite-tokens",
        headers=_auth(client._admin_token),
    )
    assert (await listed.json())["tokens"] == []


async def test_a_revoked_link_no_longer_joins(client):
    sid = await _a_space(client, "LinkDead")
    r = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={},
        headers=_auth(client._admin_token),
    )
    token = (await r.json())["token"]
    await client.delete(
        f"/api/spaces/{sid}/invite-tokens/{token}",
        headers=_auth(client._admin_token),
    )
    joined = await client.post(
        "/api/spaces/join",
        json={"token": token},
        headers=_auth(client._bob_token),
    )
    assert joined.status == 404


async def test_revoke_invite_link_is_admin_only(client):
    sid = await _a_space(client, "LinkRevokeAuth")
    await _seat_local_member(client, sid, client._bob_token, client._bob_uid)
    r = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={},
        headers=_auth(client._admin_token),
    )
    token = (await r.json())["token"]
    denied = await client.delete(
        f"/api/spaces/{sid}/invite-tokens/{token}",
        headers=_auth(client._bob_token),
    )
    assert denied.status == 403


async def test_mint_invite_link_rejects_an_unknown_connection_server(client):
    sid = await _a_space(client, "LinkNoGfs")
    resp = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={"publish_to_gfs": "gfs-nope"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422
    listed = await client.get(
        f"/api/spaces/{sid}/invite-tokens",
        headers=_auth(client._admin_token),
    )
    assert (await listed.json())["tokens"] == []


async def test_mint_invite_link_reports_the_minted_total(client):
    """ "3 of 10 left" needs the total: ``uses_remaining`` is decremented
    in place, so it cannot be derived after the first redeem."""
    sid = await _a_space(client, "LinkCounts")
    r = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={"uses": 10},
        headers=_auth(client._admin_token),
    )
    body = await r.json()
    assert body["uses"] == 10
    assert body["uses_remaining"] == 10
    token = body["token"]
    joined = await client.post(
        "/api/spaces/join",
        json={"token": token},
        headers=_auth(client._bob_token),
    )
    assert joined.status == 200
    listed = await client.get(
        f"/api/spaces/{sid}/invite-tokens",
        headers=_auth(client._admin_token),
    )
    row = (await listed.json())["tokens"][0]
    assert row["uses"] == 10
    assert row["uses_remaining"] == 9


async def test_ttl_seconds_zero_means_never_expires(client):
    """The SPA sends ``0`` for "Never"; omitting the field takes the
    server default instead."""
    sid = await _a_space(client, "LinkTtl")
    never = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={"ttl_seconds": 0},
        headers=_auth(client._admin_token),
    )
    assert (await never.json())["expires_at"] is None
    default = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={},
        headers=_auth(client._admin_token),
    )
    assert (await default.json())["expires_at"] is not None
