"""Coded refusals over HTTP — status, ``error.code`` and ``error.params``.

Each test drives the refusal through the real route + ``BaseView._iter``
and checks three things: the HTTP status didn't change, the stable code
the SPA translates is there, and the body carries no raw internals (user
ids, post ids, Pillow error text).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiohttp

from socialhome.app_keys import (
    child_protection_service_key,
    space_service_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.domain.space import JoinMode, SpaceFeatures, SpaceType

from .conftest import _auth


async def _add_user(client, username: str) -> tuple[str, str]:
    """Insert a local user + API token; returns ``(user_id, token)``."""
    uid = f"uid-{username}"
    tok = f"tok-{username}"
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        (username, uid, username.title()),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"t-{username}", uid, "t", sha256_token_hash(tok)),
    )
    return uid, tok


async def _error(resp) -> dict:
    body = await resp.json()
    return body["error"]


async def _space(client, **kw) -> str:
    svc = client.app[space_service_key]
    space = await svc.create_space(owner_username="admin", name="S", **kw)
    return space.id


# ─── Spaces: joining ─────────────────────────────────────────────────────


async def test_already_member_join_request_is_422_already_member(client):
    sid = await _space(client, join_mode=JoinMode.REQUEST)
    r = await client.post(
        f"/api/spaces/{sid}/join-requests", json={}, headers=_auth(client._tok)
    )
    assert r.status == 422
    err = await _error(r)
    assert err["code"] == "ALREADY_MEMBER"
    assert "params" not in err


async def test_inviting_an_existing_member_is_403_user_already_member(client):
    sid = await _space(client)
    bob_uid, _ = await _add_user(client, "bob")
    await client.app[space_service_key].add_member(
        sid, actor_username="admin", user_id=bob_uid
    )
    r = await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": bob_uid},
        headers=_auth(client._tok),
    )
    assert r.status == 403
    assert (await _error(r))["code"] == "USER_ALREADY_MEMBER"
    assert bob_uid not in await r.text()


async def test_banned_self_join_request_is_403_banned(client):
    sid = await _space(client, join_mode=JoinMode.REQUEST)
    bob_uid, bob_tok = await _add_user(client, "bob")
    await client.app[space_service_key].ban(
        sid, actor_username="admin", user_id=bob_uid
    )
    r = await client.post(
        f"/api/spaces/{sid}/join-requests", json={}, headers=_auth(bob_tok)
    )
    assert r.status == 403
    assert (await _error(r))["code"] == "BANNED"
    assert bob_uid not in await r.text()


async def test_adding_a_banned_user_is_403_user_banned_without_their_id(client):
    sid = await _space(client)
    bob_uid, _ = await _add_user(client, "bob")
    await client.app[space_service_key].ban(
        sid, actor_username="admin", user_id=bob_uid
    )
    r = await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": bob_uid},
        headers=_auth(client._tok),
    )
    assert r.status == 403
    err = await _error(r)
    assert err["code"] == "USER_BANNED"
    assert bob_uid not in await r.text()


async def test_join_request_to_invite_only_space_is_403_invite_only(client):
    sid = await _space(client, join_mode=JoinMode.INVITE_ONLY)
    _, bob_tok = await _add_user(client, "bob")
    r = await client.post(
        f"/api/spaces/{sid}/join-requests", json={}, headers=_auth(bob_tok)
    )
    assert r.status == 403
    assert (await _error(r))["code"] == "INVITE_ONLY"


async def test_following_a_private_space_is_403_subscribe_not_allowed(client):
    sid = await _space(client, space_type=SpaceType.PRIVATE)
    _, bob_tok = await _add_user(client, "bob")
    r = await client.post(f"/api/spaces/{sid}/subscribe", headers=_auth(bob_tok))
    assert r.status == 403
    assert (await _error(r))["code"] == "SUBSCRIBE_NOT_ALLOWED"


async def test_following_a_space_without_followers_is_403_subscribe_not_allowed(
    client,
):
    sid = await _space(
        client,
        space_type=SpaceType.GLOBAL,
        features=SpaceFeatures(allow_subscribers=False),
    )
    _, bob_tok = await _add_user(client, "bob")
    r = await client.post(f"/api/spaces/{sid}/subscribe", headers=_auth(bob_tok))
    assert r.status == 403
    assert (await _error(r))["code"] == "SUBSCRIBE_NOT_ALLOWED"


async def test_follower_posting_is_403_subscriber_read_only(client):
    sid = await _space(
        client,
        space_type=SpaceType.GLOBAL,
        features=SpaceFeatures(allow_subscribers=True),
    )
    _, bob_tok = await _add_user(client, "bob")
    r = await client.post(f"/api/spaces/{sid}/subscribe", headers=_auth(bob_tok))
    assert r.status in (200, 201, 204)
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "hi"},
        headers=_auth(bob_tok),
    )
    assert r.status == 403
    err = await _error(r)
    assert err["code"] == "SUBSCRIBER_READ_ONLY"
    assert err["params"] == {"action": "post"}


async def test_posting_in_an_archived_space_is_403_space_archived(client):
    sid = await _space(client)
    await client.app[space_service_key].archive_space(sid, actor_username="admin")
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "hi"},
        headers=_auth(client._tok),
    )
    assert r.status == 403
    assert (await _error(r))["code"] == "SPACE_ARCHIVED"


async def test_join_request_to_an_unpaired_host_is_403_not_paired(client):
    r = await client.post(
        "/api/public_spaces/sp-far-away/join-request",
        json={"host_instance_id": "inst-stranger"},
        headers=_auth(client._tok),
    )
    assert r.status == 403
    err = await _error(r)
    assert err["code"] == "NOT_PAIRED"
    assert "inst-stranger" not in await r.text()


async def test_inviting_a_too_young_member_is_403_age_restricted(client):
    sid = await _space(client)
    bob_uid, _ = await _add_user(client, "bob")
    cp = client.app[child_protection_service_key]
    await cp.enable_protection(
        minor_username="bob", declared_age=10, actor_user_id=client._uid
    )
    await cp.update_space_age_gate(sid, min_age=16, actor_user_id=client._uid)
    r = await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": bob_uid},
        headers=_auth(client._tok),
    )
    assert r.status == 403
    err = await _error(r)
    assert err["code"] == "AGE_RESTRICTED"
    assert err["params"] == {"min_age": 16}
    assert bob_uid not in await r.text()


async def test_accepting_an_unknown_remote_invite_is_404_invite_expired(client):
    r = await client.post(
        "/api/remote_invites/tok-gone/accept", json={}, headers=_auth(client._tok)
    )
    assert r.status == 404
    assert (await _error(r))["code"] == "INVITE_EXPIRED"


async def test_redeeming_an_unknown_invite_code_is_404_invite_expired(client):
    r = await client.post(
        "/api/spaces/join", json={"token": "tok-gone"}, headers=_auth(client._tok)
    )
    assert r.status == 404
    assert (await _error(r))["code"] == "INVITE_EXPIRED"


# ─── Conversations ───────────────────────────────────────────────────────


async def test_dm_to_yourself_is_422_dm_self(client):
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "admin"},
        headers=_auth(client._tok),
    )
    assert r.status == 422
    assert (await _error(r))["code"] == "DM_SELF"


async def test_group_of_two_is_422_group_too_small(client):
    await _add_user(client, "bob")
    r = await client.post(
        "/api/conversations/group",
        json={"members": ["bob"]},
        headers=_auth(client._tok),
    )
    assert r.status == 422
    err = await _error(r)
    assert err["code"] == "GROUP_TOO_SMALL"
    assert err["params"] == {"min": 3}


async def test_overlong_message_is_422_dm_too_long(client):
    await _add_user(client, "bob")
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._tok),
    )
    assert r.status in (200, 201)
    conv_id = (await r.json())["id"]
    r = await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "x" * 1001},
        headers=_auth(client._tok),
    )
    assert r.status == 422
    err = await _error(r)
    assert err["code"] == "DM_TOO_LONG"
    assert err["params"] == {"max": 1000}


async def test_dm_to_someone_who_blocked_you_is_403_dm_blocked(client):
    _, bob_tok = await _add_user(client, "bob")
    r = await client.post(
        "/api/blocks", json={"user_id": client._uid}, headers=_auth(bob_tok)
    )
    assert r.status in (200, 201)
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._tok),
    )
    assert r.status == 403
    assert (await _error(r))["code"] == "DM_BLOCKED"


async def test_dm_to_someone_you_blocked_is_403_dm_you_blocked(client):
    bob_uid, _ = await _add_user(client, "bob")
    r = await client.post(
        "/api/blocks", json={"user_id": bob_uid}, headers=_auth(client._tok)
    )
    assert r.status in (200, 201)
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._tok),
    )
    assert r.status == 403
    assert (await _error(r))["code"] == "DM_YOU_BLOCKED"
    assert bob_uid not in await r.text()


# ─── Calendar ────────────────────────────────────────────────────────────


async def test_rsvp_to_an_ended_event_is_422_rsvp_past(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key, space_type, feature_calendar) "
        "VALUES('sp-cal', 'Cal', 'iid', 'admin', ?, 'household', 1)",
        ("aa" * 32,),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role)"
        " VALUES('sp-cal', ?, 'admin')",
        (client._uid,),
    )
    past = datetime.now(timezone.utc) - timedelta(days=2)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Yesterday",
            "start": past.isoformat(),
            "end": (past + timedelta(hours=1)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    eid = (await r.json())["id"]
    r = await client.post(
        f"/api/calendars/events/{eid}/rsvp",
        json={"status": "going"},
        headers=_auth(client._tok),
    )
    assert r.status == 422
    assert (await _error(r))["code"] == "RSVP_PAST"


# ─── Bazaar ──────────────────────────────────────────────────────────────


async def _listing(
    client,
    *,
    listing_id: str,
    seller: str,
    mode: str = "fixed",
    status: str = "active",
    start_price: int | None = None,
    step_price: int | None = None,
) -> None:
    db = client._db
    await db.enqueue(
        "INSERT OR IGNORE INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-baz', 'B', 'iid', ?, ?)",
        (client._uid, "00" * 32),
    )
    await db.enqueue(
        "INSERT OR IGNORE INTO space_members(space_id, user_id, role) "
        "VALUES('sp-baz', ?, 'owner')",
        (client._uid,),
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content) "
        "VALUES(?, 'sp-baz', ?, 'bazaar', '')",
        (listing_id, seller),
    )
    await db.enqueue(
        "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id, title,"
        " mode, end_time, currency, status, start_price, step_price)"
        " VALUES(?, 'sp-baz', ?, 'Bike', ?, '2099-01-01T00:00:00+00:00',"
        " 'EUR', ?, ?, ?)",
        (listing_id, seller, mode, status, start_price, step_price),
    )


async def test_bid_below_the_floor_is_422_bid_too_low_with_the_floor(client):
    await _listing(
        client,
        listing_id="lst-low",
        seller="u-seller",
        mode="auction",
        start_price=1500,
        step_price=100,
    )
    r = await client.post(
        "/api/bazaar/lst-low/bids", json={"amount": 900}, headers=_auth(client._tok)
    )
    assert r.status == 422
    err = await _error(r)
    assert err["code"] == "BID_TOO_LOW"
    assert err["params"] == {"floor_amount": 1500, "currency": "EUR"}


async def test_bidding_on_your_own_listing_is_422_own_listing(client):
    await _listing(client, listing_id="lst-own", seller=client._uid, mode="auction")
    r = await client.post(
        "/api/bazaar/lst-own/bids", json={"amount": 900}, headers=_auth(client._tok)
    )
    assert r.status == 422
    assert (await _error(r))["code"] == "OWN_LISTING"


async def test_offering_on_your_own_listing_is_403_own_listing(client):
    await _listing(client, listing_id="lst-own-o", seller=client._uid)
    r = await client.post(
        "/api/bazaar/lst-own-o/offers",
        json={"amount": 900},
        headers=_auth(client._tok),
    )
    assert r.status == 403
    assert (await _error(r))["code"] == "OWN_LISTING"


async def test_bidding_on_a_sold_listing_is_422_listing_not_active(client):
    await _listing(
        client,
        listing_id="lst-sold",
        seller="u-seller",
        mode="auction",
        status="sold",
    )
    r = await client.post(
        "/api/bazaar/lst-sold/bids", json={"amount": 900}, headers=_auth(client._tok)
    )
    assert r.status == 422
    assert (await _error(r))["code"] == "LISTING_NOT_ACTIVE"
    assert "lst-sold" not in await r.text()


async def test_offering_on_a_sold_listing_is_409_listing_not_active(client):
    await _listing(client, listing_id="lst-sold-o", seller="u-seller", status="sold")
    r = await client.post(
        "/api/bazaar/lst-sold-o/offers",
        json={"amount": 900},
        headers=_auth(client._tok),
    )
    assert r.status == 409
    assert (await _error(r))["code"] == "LISTING_NOT_ACTIVE"
    assert "lst-sold-o" not in await r.text()


# ─── Polls ───────────────────────────────────────────────────────────────


async def test_voting_on_a_closed_poll_is_409_without_the_post_id(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key, space_type) "
        "VALUES('sp-polls', 'Polls', 'iid', 'admin', ?, 'household')",
        ("aa" * 32,),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'admin')",
        ("sp-polls", client._uid),
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content, created_at) "
        "VALUES('post-poll-7', 'sp-polls', ?, 'poll', 'Pizza?', datetime('now'))",
        (client._uid,),
    )
    await db.enqueue(
        "INSERT INTO space_polls(post_id, question, closed) VALUES(?, ?, 1)",
        ("post-poll-7", "Pizza?"),
    )
    await db.enqueue(
        "INSERT INTO space_poll_options(id, post_id, text, position) VALUES(?,?,?,?)",
        ("opt-y", "post-poll-7", "Yes", 0),
    )
    r = await client.post(
        "/api/spaces/sp-polls/posts/post-poll-7/poll/vote",
        json={"option_id": "opt-y"},
        headers=_auth(client._tok),
    )
    assert r.status == 409
    err = await _error(r)
    assert err["code"] == "POLL_CLOSED"
    assert "post-poll-7" not in err["detail"]


# ─── Pictures ────────────────────────────────────────────────────────────


def _picture_form(data: bytes) -> aiohttp.FormData:
    form = aiohttp.FormData()
    form.add_field("file", data, filename="me.png", content_type="image/png")
    return form


async def test_oversized_avatar_is_422_image_too_large(client):
    r = await client.post(
        "/api/me/picture",
        data=_picture_form(b"\0" * (10 * 1024 * 1024 + 1)),
        headers=_auth(client._tok),
    )
    assert r.status == 422
    err = await _error(r)
    assert err["code"] == "IMAGE_TOO_LARGE"
    assert err["params"] == {"max_mb": 10}


async def test_unreadable_avatar_is_422_image_unreadable_without_pillow_text(
    client,
):
    r = await client.post(
        "/api/me/picture",
        data=_picture_form(b"definitely not an image" * 20),
        headers=_auth(client._tok),
    )
    assert r.status == 422
    err = await _error(r)
    assert err["code"] == "IMAGE_UNREADABLE"
    text = (await r.text()).lower()
    assert "cannot identify" not in text
    assert "cannot open image" not in text
    assert "bytesio" not in text
