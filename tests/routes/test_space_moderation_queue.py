"""The §4.3 moderation queue on the REST surface, for every feature.

Under ``MODERATED`` a member's new item, and their edit / delete of
somebody else's, answers ``202 {queued, item_id, feature, action}`` and
changes nothing; the moderation routes list / approve / reject it; and the
pending content is visible to nobody but its submitter and the space's
content authority.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from socialhome.app_keys import space_moderation_service_key
from socialhome.services.poll_service import PollService
from socialhome.services.space_moderation_service import MAX_PENDING_PER_SUBMITTER

from . import test_space_content_access as access
from .test_space_content_access import (
    SID,
    _seed_content,
    _send,
    _set_access,
    _snapshot,
    _user,
    _writes,
)

#: The access suite's seeded space (owner, admin, moderator, member).
space = access.space

_FEATURES = ("pages", "tasks", "stickies", "calendar")
#: Writes that are LAYOUT (never queued) — a position-only move.
_LAYOUT = ({"position": 3}, {"position_x": 40})


async def _seed_by_owner(client, space) -> dict:
    """One item per feature, made by the OWNER — so a member's edit /
    delete of it is "somebody else's"."""
    return await _seed_content(client, {"mem": space["owner"]})


async def _second_member(client) -> dict:
    heads = await _user(client, "mem2", "uid-mem2")
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'member')",
        (SID, "uid-mem2"),
    )
    return heads


def _is_layout(path: str, body: dict | None) -> bool:
    return body in _LAYOUT or path.endswith("/reorder")


async def _queue(client, headers: dict, status: str = "pending") -> list[dict]:
    r = await client.get(
        f"/api/spaces/{SID}/moderation?status={status}", headers=headers
    )
    assert r.status == 200, await r.text()
    return await r.json()


@pytest.mark.parametrize("feature", _FEATURES)
async def test_member_writes_on_others_items_queue_with_202(client, space, feature):
    ids = await _seed_by_owner(client, space)
    await _set_access(client, feature, "moderated")
    before = await _snapshot(client)
    queued = 0
    for feat, method, path, body in _writes(ids):
        if feat != feature or "resolve-conflict" in path:
            continue
        r = await _send(client, method, path, body, space["mem"])
        if _is_layout(path, body):
            assert r.status in (200, 201, 204), (method, path, await r.text())
            continue
        assert r.status == 202, (method, path, await r.text())
        got = await r.json()
        assert got["queued"] is True and got["item_id"]
        assert got["feature"] == feature
        assert got["action"] in ("create", "edit", "delete")
        queued += 1
    assert queued >= 3
    after = await _snapshot(client)
    # Only layout moves landed; no content row was added, edited or removed.
    for table in ("space_pages", "space_task_lists", "space_calendar_events"):
        assert after[table] == before[table], table
    assert len(after["space_tasks"]) == len(before["space_tasks"])
    assert len(after["stickies"]) == len(before["stickies"])
    items = await _queue(client, space["mod"])
    assert len(items) == queued
    assert {i["feature"] for i in items} == {feature}
    assert all(i["submitted_by"] == "uid-mem" for i in items)
    assert all(i["submitted_by_display"] == "Mem" for i in items)


@pytest.mark.parametrize("feature", _FEATURES)
async def test_member_own_items_and_moderators_proceed(client, space, feature):
    ids = await _seed_content(client, space)  # the member's own items
    await _set_access(client, feature, "moderated")
    edits = [
        w
        for w in _writes(ids)
        if w[0] == feature and w[1] == "patch" and not _is_layout(w[2], w[3])
    ]
    for _f, method, path, body in edits:
        r = await _send(client, method, path, body, space["mem"])
        assert r.status in (200, 201), (method, path, await r.text())
    creates = [w for w in _writes(ids) if w[0] == feature and w[1] == "post"]
    for _f, method, path, body in creates:
        if "resolve-conflict" in path or path.endswith(("/reorder", "/archive")):
            continue
        r = await _send(client, method, path, body, space["mod"])
        assert r.status in (200, 201), (method, path, await r.text())
    assert await _queue(client, space["mod"]) == []


async def test_queue_routes_are_content_authority_only(client, space):
    await _set_access(client, "stickies", "moderated")
    r = await client.post(
        f"/api/spaces/{SID}/stickies", json={"content": "hi"}, headers=space["mem"]
    )
    assert r.status == 202
    item_id = (await r.json())["item_id"]
    for who in ("owner", "adm", "mod"):
        assert len(await _queue(client, space[who])) == 1, who
    r = await client.get(f"/api/spaces/{SID}/moderation", headers=space["mem"])
    assert r.status == 403
    for verb in ("approve", "reject"):
        r = await client.post(
            f"/api/spaces/{SID}/moderation/{item_id}/{verb}",
            json={},
            headers=space["mem"],
        )
        assert r.status == 403, verb
    r = await client.get(
        f"/api/spaces/{SID}/moderation?status=bogus", headers=space["mod"]
    )
    assert r.status == 422


async def test_mine_lists_only_the_callers_items(client, space):
    mem2 = await _second_member(client)
    await _set_access(client, "stickies", "moderated")
    for heads, content in ((space["mem"], "a"), (mem2, "b")):
        r = await client.post(
            f"/api/spaces/{SID}/stickies", json={"content": content}, headers=heads
        )
        assert r.status == 202
    r = await client.get(f"/api/spaces/{SID}/moderation/mine", headers=space["mem"])
    assert r.status == 200
    mine = await r.json()
    assert [i["preview"]["content"] for i in mine] == ["a"]
    assert mine[0]["status"] == "pending"
    r = await client.get(f"/api/spaces/{SID}/moderation/mine", headers=space["owner"])
    assert await r.json() == []


async def test_pending_content_is_invisible_to_other_members(client, space):
    """Nothing of a pending item reaches another member's lists."""
    ids = await _seed_by_owner(client, space)
    mem2 = await _second_member(client)
    for feature in ("posts", *_FEATURES):
        await _set_access(client, feature, "moderated")
    secret = "SECRET-PENDING"
    start = datetime.now(timezone.utc) + timedelta(days=3)
    submissions = [
        ("post", f"/api/spaces/{SID}/posts", {"type": "text", "content": secret}),
        ("post", f"/api/spaces/{SID}/pages", {"title": secret, "content": secret}),
        ("patch", f"/api/spaces/{SID}/pages/{ids['page']}", {"content": secret}),
        ("post", f"/api/spaces/{SID}/tasks/lists", {"name": secret}),
        (
            "post",
            f"/api/spaces/{SID}/tasks/lists/{ids['list']}/tasks",
            {"title": secret},
        ),
        ("patch", f"/api/spaces/{SID}/tasks/{ids['task']}", {"title": secret}),
        ("post", f"/api/spaces/{SID}/stickies", {"content": secret}),
        ("patch", f"/api/spaces/{SID}/stickies/{ids['sticky']}", {"content": secret}),
        (
            "post",
            f"/api/spaces/{SID}/calendar/events",
            {
                "summary": secret,
                "start": start.isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
            },
        ),
        (
            "patch",
            f"/api/spaces/{SID}/calendar/events/{ids['event']}",
            {"summary": secret},
        ),
    ]
    for method, path, body in submissions:
        r = await _send(client, method, path, body, space["mem"])
        assert r.status == 202, (path, await r.text())
    end = start + timedelta(days=30)
    reads = [
        f"/api/spaces/{SID}/feed",
        f"/api/spaces/{SID}/pages",
        f"/api/spaces/{SID}/pages/{ids['page']}",
        f"/api/spaces/{SID}/tasks/lists",
        f"/api/spaces/{SID}/tasks/lists/{ids['list']}/tasks",
        f"/api/spaces/{SID}/stickies",
        f"/api/spaces/{SID}/calendar/events?start="
        f"{(start - timedelta(days=10)).isoformat().replace('+', '%2B')}"
        f"&end={end.isoformat().replace('+', '%2B')}",
        f"/api/search?q={secret}",
        f"/api/spaces/{SID}/moderation/mine",
        "/api/notifications",
    ]
    for heads in (mem2, space["owner"]):
        for path in reads:
            r = await client.get(path, headers=heads)
            assert r.status == 200, (path, r.status)
            assert secret not in await r.text(), path
    # The submitter sees their own; content authority sees the queue.
    r = await client.get(f"/api/spaces/{SID}/moderation/mine", headers=space["mem"])
    assert secret in await r.text()
    assert secret in str(await _queue(client, space["mod"]))


@pytest.mark.parametrize("feature", _FEATURES)
async def test_approve_applies_and_reject_discards(client, space, feature):
    ids = await _seed_by_owner(client, space)
    await _set_access(client, feature, "moderated")
    writes = [
        w
        for w in _writes(ids)
        if w[0] == feature
        and w[1] in ("post", "patch")
        and not _is_layout(w[2], w[3])
        and "resolve-conflict" not in w[2]
        and not w[2].endswith("/archive")
    ]
    for _f, method, path, body in writes:
        r = await _send(client, method, path, body, space["mem"])
        assert r.status == 202, (path, await r.text())
    items = await _queue(client, space["mod"])
    approve, *rest = items
    before = await _snapshot(client)
    r = await client.post(
        f"/api/spaces/{SID}/moderation/{approve['id']}/approve",
        json={},
        headers=space["mod"],
    )
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["status"] == "approved" and body["target_id"] == approve["target_id"]
    assert await _snapshot(client) != before
    r = await client.post(
        f"/api/spaces/{SID}/moderation/{approve['id']}/approve",
        json={},
        headers=space["mod"],
    )
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "ALREADY_DECIDED"
    for item in rest:
        before = await _snapshot(client)
        r = await client.post(
            f"/api/spaces/{SID}/moderation/{item['id']}/reject",
            json={"reason": "not now"},
            headers=space["mod"],
        )
        assert r.status == 200, await r.text()
        assert await _snapshot(client) == before
    everything = await _queue(client, space["mod"], status="all")
    assert {i["status"] for i in everything} <= {"approved", "rejected"}
    rejected = [i for i in everything if i["status"] == "rejected"]
    assert all(i["rejection_reason"] == "not now" for i in rejected)


async def test_reject_reason_is_capped(client, space):
    await _set_access(client, "stickies", "moderated")
    r = await client.post(
        f"/api/spaces/{SID}/stickies", json={"content": "x"}, headers=space["mem"]
    )
    item_id = (await r.json())["item_id"]
    r = await client.post(
        f"/api/spaces/{SID}/moderation/{item_id}/reject",
        json={"reason": "x" * 501},
        headers=space["mod"],
    )
    assert r.status == 422


async def test_page_edit_stale_409_then_force(client, space):
    ids = await _seed_by_owner(client, space)
    await _set_access(client, "pages", "moderated")
    r = await client.patch(
        f"/api/spaces/{SID}/pages/{ids['page']}",
        json={"content": "mine"},
        headers=space["mem"],
    )
    assert r.status == 202
    item_id = (await r.json())["item_id"]
    r = await client.patch(
        f"/api/spaces/{SID}/pages/{ids['page']}",
        json={"content": "theirs"},
        headers=space["owner"],
    )
    assert r.status == 200
    r = await client.post(
        f"/api/spaces/{SID}/moderation/{item_id}/approve",
        json={},
        headers=space["mod"],
    )
    assert r.status == 409
    err = (await r.json())["error"]
    assert err["code"] == "STALE"
    assert err["current"] == {"content": "theirs"}
    assert err["proposed"] == {"content": "mine"}
    assert err["base"] == {"content": "c"}
    r = await client.post(
        f"/api/spaces/{SID}/moderation/{item_id}/approve",
        json={"force": True},
        headers=space["mod"],
    )
    assert r.status == 200
    r = await client.get(f"/api/spaces/{SID}/pages/{ids['page']}", headers=space["mem"])
    assert (await r.json())["content"] == "mine"


async def test_edit_of_deleted_target_410_and_delete_is_noop(client, space):
    ids = await _seed_by_owner(client, space)
    await _set_access(client, "stickies", "moderated")
    path = f"/api/spaces/{SID}/stickies/{ids['sticky']}"
    edit = await (
        await client.patch(path, json={"content": "n"}, headers=space["mem"])
    ).json()
    delete = await (await client.delete(path, headers=space["mem"])).json()
    assert (await client.delete(path, headers=space["owner"])).status in (200, 204)
    r = await client.post(
        f"/api/spaces/{SID}/moderation/{edit['item_id']}/approve",
        json={},
        headers=space["mod"],
    )
    assert r.status == 410
    assert (await r.json())["error"]["code"] == "TARGET_GONE"
    r = await client.post(
        f"/api/spaces/{SID}/moderation/{delete['item_id']}/approve",
        json={},
        headers=space["mod"],
    )
    assert r.status == 200
    statuses = {i["id"]: i["status"] for i in await _queue(client, space["mod"], "all")}
    assert statuses == {edit["item_id"]: "expired", delete["item_id"]: "approved"}


async def test_feature_off_or_archived_blocks_approve_not_reject(client, space):
    await _set_access(client, "stickies", "moderated")
    r = await client.post(
        f"/api/spaces/{SID}/stickies", json={"content": "x"}, headers=space["mem"]
    )
    item_id = (await r.json())["item_id"]
    await client._db.enqueue("UPDATE spaces SET feature_stickies=0 WHERE id=?", (SID,))
    r = await client.post(
        f"/api/spaces/{SID}/moderation/{item_id}/approve",
        json={},
        headers=space["mod"],
    )
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "FEATURE_UNAVAILABLE"
    r = await client.post(
        f"/api/spaces/{SID}/moderation/{item_id}/reject",
        json={},
        headers=space["mod"],
    )
    assert r.status == 200


async def test_queue_full_is_429(client, space):
    await _set_access(client, "stickies", "moderated")
    for i in range(MAX_PENDING_PER_SUBMITTER):
        r = await client.post(
            f"/api/spaces/{SID}/stickies",
            json={"content": f"n{i}"},
            headers=space["mem"],
        )
        assert r.status == 202
    r = await client.post(
        f"/api/spaces/{SID}/stickies", json={"content": "more"}, headers=space["mem"]
    )
    assert r.status == 429
    assert (await r.json())["error"]["code"] == "QUEUE_FULL"


# ── Posts: attachments ride with the post ──────────────────────────────────


async def test_poll_post_queues_whole_and_approves_atomically(client, space):
    await _set_access(client, "posts", "moderated")
    r = await client.post(
        f"/api/spaces/{SID}/posts",
        json={
            "type": "poll",
            "content": "Lunch?",
            "poll": {"question": "Where?", "options": ["Pizza", "Sushi"]},
        },
        headers=space["mem"],
    )
    assert r.status == 202, await r.text()
    item_id = (await r.json())["item_id"]
    [item] = await _queue(client, space["mod"])
    assert item["preview"]["poll"]["options"] == ["Pizza", "Sushi"]
    assert await client._db.fetchall("SELECT * FROM space_posts") == []
    r = await client.post(
        f"/api/spaces/{SID}/moderation/{item_id}/approve",
        json={},
        headers=space["mod"],
    )
    assert r.status == 200
    post_id = (await r.json())["post_id"]
    r = await client.get(
        f"/api/spaces/{SID}/posts/{post_id}/poll", headers=space["mem"]
    )
    assert r.status == 200
    assert (await r.json())["question"] == "Where?"
    # A late attach to the approved post is refused while posts are reviewed.
    r = await client.post(
        f"/api/spaces/{SID}/posts/{post_id}/poll",
        json={"question": "Q", "options": ["a", "b"]},
        headers=space["mem"],
    )
    assert r.status == 403


async def test_open_poll_post_creates_the_poll_in_one_request(client, space):
    r = await client.post(
        f"/api/spaces/{SID}/posts",
        json={
            "type": "poll",
            "content": "Lunch?",
            "poll": {"question": "Where?", "options": ["Pizza", "Sushi"]},
        },
        headers=space["mem"],
    )
    assert r.status == 201, await r.text()
    post_id = (await r.json())["id"]
    r = await client.get(
        f"/api/spaces/{SID}/posts/{post_id}/poll", headers=space["mem"]
    )
    assert (await r.json())["question"] == "Where?"


async def test_bad_poll_attachment_is_422_and_persists_nothing(client, space):
    r = await client.post(
        f"/api/spaces/{SID}/posts",
        json={
            "type": "poll",
            "content": "?",
            "poll": {"question": "Q", "options": ["a"]},
        },
        headers=space["mem"],
    )
    assert r.status == 422
    assert await client._db.fetchall("SELECT * FROM space_posts") == []


async def test_schedule_post_queues_with_its_slots(client, space):
    await _set_access(client, "posts", "moderated")
    r = await client.post(
        f"/api/spaces/{SID}/posts",
        json={
            "type": "schedule",
            "content": "When?",
            "schedule": {"title": "Meetup", "slots": [{"slot_date": "2026-11-01"}]},
        },
        headers=space["mem"],
    )
    assert r.status == 202, await r.text()
    item_id = (await r.json())["item_id"]
    r = await client.post(
        f"/api/spaces/{SID}/moderation/{item_id}/approve",
        json={},
        headers=space["mod"],
    )
    assert r.status == 200, await r.text()
    post_id = (await r.json())["post_id"]
    r = await client.get(
        f"/api/spaces/{SID}/schedule-polls/{post_id}/summary", headers=space["mem"]
    )
    assert r.status == 200
    assert (await r.json())["title"] == "Meetup"


async def test_bazaar_listing_queues_with_its_post_no_orphan(client, space):
    await _set_access(client, "posts", "moderated")
    await client._db.enqueue("UPDATE spaces SET feature_bazaar=1 WHERE id=?", (SID,))
    r = await client.post(
        "/api/bazaar",
        json={
            "space_id": SID,
            "title": "Bike",
            "mode": "fixed",
            "currency": "EUR",
            "price": 5000,
        },
        headers=space["mem"],
    )
    assert r.status == 202, await r.text()
    item_id = (await r.json())["item_id"]
    assert await client._db.fetchall("SELECT * FROM bazaar_listings") == []
    assert await client._db.fetchall("SELECT * FROM space_posts") == []
    r = await client.get(f"/api/spaces/{SID}/bazaar", headers=space["mem"])
    assert await r.json() == []
    r = await client.post(
        f"/api/spaces/{SID}/moderation/{item_id}/approve",
        json={},
        headers=space["mod"],
    )
    assert r.status == 200, await r.text()
    r = await client.get(f"/api/spaces/{SID}/bazaar", headers=space["mem"])
    [listing] = await r.json()
    assert listing["title"] == "Bike"
    assert listing["seller_user_id"] == "uid-mem"


async def test_highlight_share_queues_with_item_id(client, space):
    await _set_access(client, "posts", "moderated")
    r = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=space["mem"],
    )
    assert r.status in (200, 201), await r.text()
    highlight_id = (await r.json())["highlight"]["id"]
    r = await client.post(
        f"/api/highlights/{highlight_id}/share",
        json={"scope": "space", "space_id": SID, "note": "look"},
        headers=space["mem"],
    )
    assert r.status == 202, await r.text()
    body = await r.json()
    assert body["queued"] is True and body["item_id"]


async def test_calendar_announce_queues_its_card(client, space):
    await _set_access(client, "posts", "moderated")
    start = datetime.now(timezone.utc) + timedelta(days=1)
    r = await client.post(
        f"/api/spaces/{SID}/calendar/events",
        json={
            "summary": "Picnic",
            "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(),
            "announce_in_feed": True,
        },
        headers=space["mem"],
    )
    assert r.status == 201, await r.text()
    body = await r.json()
    assert body["announce_queued"] is True and body["announce_item_id"]
    assert "announce_suppressed" not in body
    r = await client.get(f"/api/spaces/{SID}/feed", headers=space["mem"])
    assert "Picnic" not in await r.text()
    r = await client.post(
        f"/api/spaces/{SID}/moderation/{body['announce_item_id']}/approve",
        json={},
        headers=space["mod"],
    )
    assert r.status == 200, await r.text()
    rows = await client._db.fetchall(
        "SELECT linked_event_id, author FROM space_posts WHERE type='event'"
    )
    assert [(r["linked_event_id"], r["author"]) for r in rows] == [
        (body["id"], "uid-mem")
    ]


# ── Where the queue may live (local-only until federated moderation) ──────


async def _remote_household(client, instance_id: str, version: int) -> None:
    """A paired member household that advertised ``version``."""
    await client._db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source, proto_version, capabilities_seen_at)"
        " VALUES(?, ?, ?, '00', '00', ?, ?, 'confirmed', 'manual', ?,"
        " '2026-06-01T00:00:00+00:00')",
        (
            instance_id,
            instance_id,
            "ab" * 32,
            f"https://{instance_id}/inbox/x",
            f"{instance_id}_local",
            version,
        ),
    )
    await client._db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES(?, ?)",
        (SID, instance_id),
    )


async def test_reviewed_with_remote_households_needs_them_on_v43(client, space):
    """v_43: "Reviewed" works with members on other households — a household
    below v_43 is named (409 PEERS_TOO_OLD) until the admin applies anyway."""
    await _remote_household(client, "inst-old", 42)
    r = await client.get(f"/api/spaces/{SID}", headers=space["owner"])
    assert (await r.json())["has_remote_households"] is True
    r = await client.patch(
        f"/api/spaces/{SID}",
        json={"features": {"tasks_access": "moderated"}},
        headers=space["owner"],
    )
    assert r.status == 409, await r.text()
    err = (await r.json())["error"]
    assert err["code"] == "PEERS_TOO_OLD"
    assert [h["instance_id"] for h in err["households"]] == ["inst-old"]
    r = await client.patch(
        f"/api/spaces/{SID}",
        json={"features": {"tasks_access": "moderated"}, "force": True},
        headers=space["owner"],
    )
    assert r.status == 200, await r.text()
    await client._db.enqueue(
        "UPDATE remote_instances SET proto_version=43 WHERE id='inst-old'"
    )
    r = await client.patch(
        f"/api/spaces/{SID}",
        json={"features": {"pages_access": "moderated"}},
        headers=space["owner"],
    )
    assert r.status == 200, await r.text()
    # A member's write now queues on the host (no remote reviewer seats).
    r = await client.post(
        f"/api/spaces/{SID}/pages", json={"title": "x"}, headers=space["mem"]
    )
    assert r.status == 202, await r.text()
    assert await client._db.fetchall("SELECT * FROM space_pages") == []


async def test_a_stub_submit_with_a_host_below_v43_is_409_host_too_old(client, space):
    await _set_access(client, "stickies", "moderated")
    await _remote_household(client, "inst-host-old", 42)
    await client._db.enqueue(
        "UPDATE spaces SET owner_instance_id='inst-host-old' WHERE id=?", (SID,)
    )
    r = await client.post(
        f"/api/spaces/{SID}/stickies", json={"content": "x"}, headers=space["mem"]
    )
    assert r.status == 409, await r.text()
    assert (await r.json())["error"]["code"] == "HOST_TOO_OLD"
    assert await client._db.fetchall("SELECT * FROM space_moderation_queue") == []
    assert await client._db.fetchall("SELECT * FROM stickies") == []


async def test_moderation_service_is_wired(client):
    assert client.app[space_moderation_service_key] is not None


async def _member_bot_token(client, space) -> str:
    await client._db.enqueue("UPDATE spaces SET bot_enabled=1 WHERE id=?", (SID,))
    r = await client.post(
        f"/api/spaces/{SID}/bots",
        json={"scope": "member", "slug": "mybot", "name": "My bot", "icon": "🤖"},
        headers=space["mem"],
    )
    assert r.status in (200, 201), await r.text()
    return (await r.json())["token"]


async def test_a_members_personal_bot_cannot_post_around_review(client, space):
    token = await _member_bot_token(client, space)
    await _set_access(client, "posts", "moderated")
    r = await client.post(
        f"/api/bot-bridge/spaces/{SID}",
        json={"title": "t", "message": "UNREVIEWED via bot"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 403, await r.text()
    assert (await r.json())["error"]["code"] == "BOT_POSTS_REVIEWED"
    assert await client._db.fetchall("SELECT * FROM space_posts") == []
    # Open posts: the same bot posts as before.
    await _set_access(client, "posts", "open")
    r = await client.post(
        f"/api/bot-bridge/spaces/{SID}",
        json={"title": "t", "message": "fine"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status in (200, 201), await r.text()


async def test_a_members_personal_bot_under_admin_only_is_403(client, space):
    token = await _member_bot_token(client, space)
    await _set_access(client, "posts", "admin_only")
    r = await client.post(
        f"/api/bot-bridge/spaces/{SID}",
        json={"message": "nope"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "ACCESS_ADMIN_ONLY"


async def test_attachment_failure_after_publish_stays_approved_and_resumes(
    client, space
):
    """Adversarial review I2: the post published, then the poll failed. The
    item must stay APPROVED (never back to pending, never rejectable while
    the post is live); approving again creates the missing poll."""
    await _set_access(client, "posts", "moderated")
    r = await client.post(
        f"/api/spaces/{SID}/posts",
        json={
            "type": "poll",
            "content": "Q?",
            "poll": {"question": "Q?", "options": ["a", "b"]},
        },
        headers=space["mem"],
    )
    item_id = (await r.json())["item_id"]

    async def boom(self, **kw):
        raise RuntimeError("db hiccup")

    url = f"/api/spaces/{SID}/moderation/{item_id}"
    with patch.object(PollService, "create_poll_once", boom):
        r = await client.post(f"{url}/approve", json={}, headers=space["mod"])
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["status"] == "approved" and body["complete"] is False
    post_id = body["post_id"]
    row = await client._db.fetchone(
        "SELECT status FROM space_moderation_queue WHERE id=?", (item_id,)
    )
    assert row["status"] == "approved"
    r = await client.post(f"{url}/reject", json={"reason": "no"}, headers=space["mod"])
    assert r.status == 409
    live = await client._db.fetchall(
        "SELECT id FROM space_posts WHERE id=? AND deleted=0", (post_id,)
    )
    assert len(live) == 1
    # Resume: the poll is created now; a further approve has nothing to do.
    r = await client.post(f"{url}/approve", json={}, headers=space["mod"])
    assert r.status == 200, await r.text()
    assert (await r.json())["complete"] is True
    r = await client.get(
        f"/api/spaces/{SID}/posts/{post_id}/poll", headers=space["mem"]
    )
    assert (await r.json())["question"] == "Q?"
    r = await client.post(f"{url}/approve", json={}, headers=space["mod"])
    assert r.status == 409


async def test_approve_after_expiry_is_410_expired(client, space):
    await _set_access(client, "stickies", "moderated")
    r = await client.post(
        f"/api/spaces/{SID}/stickies", json={"content": "late"}, headers=space["mem"]
    )
    item_id = (await r.json())["item_id"]
    await client._db.enqueue(
        "UPDATE space_moderation_queue SET expires_at='2000-01-01T00:00:00+00:00'"
        " WHERE id=?",
        (item_id,),
    )
    r = await client.post(
        f"/api/spaces/{SID}/moderation/{item_id}/approve", json={}, headers=space["mod"]
    )
    assert r.status == 410
    assert (await r.json())["error"]["code"] == "EXPIRED"
    assert await client._db.fetchall("SELECT * FROM stickies") == []


async def test_a_moderator_on_a_stub_approves_and_the_host_publishes(client, space):
    """v_43: the Moderation tab works off the host (no more 409 NOT_HOST) —
    the approval is handed to the host, which publishes the item; nothing
    is applied here, and the item reads "publishing" meanwhile."""
    await _set_access(client, "stickies", "moderated")
    r = await client.post(
        f"/api/spaces/{SID}/stickies", json={"content": "x"}, headers=space["mem"]
    )
    item_id = (await r.json())["item_id"]
    await client._db.enqueue(
        "UPDATE spaces SET owner_instance_id='elsewhere' WHERE id=?", (SID,)
    )
    r = await client.post(
        f"/api/spaces/{SID}/moderation/{item_id}/approve", json={}, headers=space["mod"]
    )
    assert r.status == 200, await r.text()
    assert (await r.json())["status"] == "publishing"
    assert await client._db.fetchall("SELECT content FROM stickies", ()) == []
    r = await client.get(f"/api/spaces/{SID}/moderation", headers=space["mod"])
    (item,) = await r.json()
    assert item["status"] == "pending" and item["publishing"] is True


# ── Round 2: racing resumes / approves (review N1) and checks (N2) ─────────


async def _partial_poll_item(client, space) -> str:
    await _set_access(client, "posts", "moderated")
    r = await client.post(
        f"/api/spaces/{SID}/posts",
        json={
            "type": "poll",
            "content": "Q?",
            "poll": {"question": "Q?", "options": ["a", "b"]},
        },
        headers=space["mem"],
    )
    item_id = (await r.json())["item_id"]

    async def boom(self, **kw):
        raise RuntimeError("hiccup")

    with patch.object(PollService, "create_poll_once", boom):
        r = await client.post(
            f"/api/spaces/{SID}/moderation/{item_id}/approve",
            json={},
            headers=space["mod"],
        )
    assert (await r.json())["complete"] is False
    return item_id


async def _count(client, sql: str) -> int:
    return (await client._db.fetchone(f"SELECT COUNT(*) AS n FROM {sql}"))["n"]


async def test_two_racing_resumes_create_the_poll_once(client, space):
    item_id = await _partial_poll_item(client, space)
    url = f"/api/spaces/{SID}/moderation/{item_id}/approve"
    rs = await asyncio.gather(
        client.post(url, json={}, headers=space["mod"]),
        client.post(url, json={}, headers=space["adm"]),
    )
    statuses = sorted(r.status for r in rs)
    assert statuses in ([200, 409], [200, 200]), statuses
    assert await _count(client, "space_poll_options") == 2
    assert await _count(client, "space_polls") == 1


async def test_double_click_fresh_approve_publishes_once(client, space):
    await _set_access(client, "posts", "moderated")
    r = await client.post(
        f"/api/spaces/{SID}/posts",
        json={
            "type": "poll",
            "content": "Q?",
            "poll": {"question": "Q?", "options": ["a", "b"]},
        },
        headers=space["mem"],
    )
    item_id = (await r.json())["item_id"]
    url = f"/api/spaces/{SID}/moderation/{item_id}/approve"
    rs = await asyncio.gather(
        client.post(url, json={}, headers=space["mod"]),
        client.post(url, json={}, headers=space["adm"]),
    )
    statuses = sorted(r.status for r in rs)
    assert statuses == [200, 409], statuses
    loser = [r for r in rs if r.status == 409][0]
    assert (await loser.json())["error"]["code"] in ("IN_PROGRESS", "ALREADY_DECIDED")
    assert await _count(client, "space_poll_options") == 2
    assert await _count(client, "space_posts WHERE type='poll'") == 1


async def test_racing_resumes_of_a_schedule_create_its_slots_once(client, space):
    await _set_access(client, "posts", "moderated")
    r = await client.post(
        f"/api/spaces/{SID}/posts",
        json={
            "type": "schedule",
            "content": "When?",
            "schedule": {
                "title": "Meet",
                "slots": [{"slot_date": "2026-11-01"}, {"slot_date": "2026-11-02"}],
            },
        },
        headers=space["mem"],
    )
    item_id = (await r.json())["item_id"]

    async def boom(self, **kw):
        raise RuntimeError("hiccup")

    url = f"/api/spaces/{SID}/moderation/{item_id}/approve"
    with patch.object(PollService, "create_schedule_poll_once", boom):
        await client.post(url, json={}, headers=space["mod"])
    await asyncio.gather(
        client.post(url, json={}, headers=space["mod"]),
        client.post(url, json={}, headers=space["adm"]),
    )
    assert await _count(client, "space_schedule_slots") == 2


async def test_racing_resumes_of_a_bazaar_listing_never_500(client, space):
    await _set_access(client, "posts", "moderated")
    await client._db.enqueue("UPDATE spaces SET feature_bazaar=1 WHERE id=?", (SID,))
    r = await client.post(
        "/api/bazaar",
        json={
            "space_id": SID,
            "title": "Bike",
            "mode": "fixed",
            "currency": "EUR",
            "price": 100,
        },
        headers=space["mem"],
    )
    item_id = (await r.json())["item_id"]

    async def boom(self, **kw):
        raise RuntimeError("hiccup")

    from socialhome.services.bazaar_service import BazaarService

    url = f"/api/spaces/{SID}/moderation/{item_id}/approve"
    with patch.object(BazaarService, "create_listing_once", boom):
        r = await client.post(url, json={}, headers=space["mod"])
        assert (await r.json())["complete"] is False
    rs = await asyncio.gather(
        client.post(url, json={}, headers=space["mod"]),
        client.post(url, json={}, headers=space["adm"]),
    )
    assert all(r.status in (200, 409) for r in rs), [r.status for r in rs]
    assert await _count(client, "bazaar_listings") == 1


async def test_resume_runs_the_approve_checks_first(client, space):
    """N2: a resume is still a release — archived space, admin-only feature,
    a departed author all stop it, and nothing more is created."""
    item_id = await _partial_poll_item(client, space)
    url = f"/api/spaces/{SID}/moderation/{item_id}/approve"
    await _set_access(client, "posts", "admin_only")
    r = await client.post(url, json={}, headers=space["mod"])
    assert r.status == 403
    await _set_access(client, "posts", "moderated")
    await client._db.enqueue("UPDATE spaces SET archived=1 WHERE id=?", (SID,))
    r = await client.post(url, json={}, headers=space["mod"])
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "FEATURE_UNAVAILABLE"
    await client._db.enqueue("UPDATE spaces SET archived=0 WHERE id=?", (SID,))
    await client._db.enqueue("DELETE FROM space_members WHERE user_id='uid-mem'")
    r = await client.post(url, json={}, headers=space["mod"])
    assert r.status == 410
    assert await _count(client, "space_poll_options") == 0
    row = await client._db.fetchone(
        "SELECT status FROM space_moderation_queue WHERE id=?", (item_id,)
    )
    assert row["status"] == "approved"  # published content stays approved
