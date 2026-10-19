"""``GET /api/spaces`` — the Spaces page's row per seat (redesign PR 11).

Each row carries, read at request time (no migration):

* ``my_role`` — the caller's seat (a follower's is ``subscriber``);
* ``member_count`` — local + remote members, never a follower, never a
  tombstoned remote seat (what the space's people list shows);
* ``last_activity_at`` — the newest visible feed post, else the space's
  creation time;
* ``icon_url`` — the uploaded icon, signed; ``null`` without one;
* ``needs_you`` — the More badge's total for owners / admins / moderators,
  ``null`` for a member or a follower.

Only the caller's own seats are listed, and nothing about another user.
"""

import pytest

from .test_space_home import _MEMBER, _OWNER, seeded  # noqa: F401 — the fixture

_ROW_KEYS = {
    "id",
    "name",
    "emoji",
    "description",
    "space_type",
    "join_mode",
    "features",
    "archived",
    "archived_reason",
    "owner_instance_id",
    "my_role",
    "member_count",
    "last_activity_at",
    "unread_posts",
    "icon_url",
    "needs_you",
}


async def _rows(client, headers) -> dict[str, dict]:
    r = await client.get("/api/spaces", headers=headers)
    assert r.status == 200, await r.text()
    return {row["id"]: row for row in await r.json()}


async def _row(client, headers, sid) -> dict:
    rows = await _rows(client, headers)
    assert sid in rows
    return rows[sid]


@pytest.mark.parametrize(
    ("who", "role", "needs_you"),
    [
        # 1 held post + 1 join request; olive proposed the tier change.
        ("owner", "owner", 2),
        # …and adam has not voted on it yet.
        ("admin", "admin", 3),
        # Review only — join requests and proposals are settings authority.
        ("moderator", "moderator", 1),
        ("member", "member", None),
        ("follower", "subscriber", None),
    ],
)
async def test_each_seat_gets_its_role_and_needs_you(seeded, who, role, needs_you):  # noqa: F811
    client, hdr, sid = seeded
    row = await _row(client, hdr[who], sid)
    assert row["my_role"] == role
    assert row["needs_you"] == needs_you


async def test_a_row_carries_only_the_list_fields(seeded):  # noqa: F811
    client, hdr, sid = seeded
    row = await _row(client, hdr["member"], sid)
    assert set(row) == _ROW_KEYS


async def test_member_count_skips_followers_and_counts_remote_members(seeded):  # noqa: F811
    client, hdr, sid = seeded
    # olive, adam, mo, mia — fern follows, she is no member.
    assert (await _row(client, hdr["member"], sid))["member_count"] == 4
    await client._db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
        " VALUES(?, 'peer-a', 'rm-1', 'member'), (?, 'peer-a', 'rm-2', 'subscriber')",
        (sid, sid),
    )
    await client._db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role,"
        " tombstoned) VALUES(?, 'peer-a', 'rm-3', 'member', 1)",
        (sid,),
    )
    assert (await _row(client, hdr["member"], sid))["member_count"] == 5


async def test_last_activity_is_the_newest_visible_post(seeded):  # noqa: F811
    client, hdr, sid = seeded
    created = (await _row(client, hdr["member"], sid))["last_activity_at"]
    assert created  # no post yet: when the space was created here
    await client._db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content, created_at)"
        " VALUES('p-old', ?, ?, 'text', 'hi', '2030-01-01 10:00:00')",
        (sid, _OWNER[1]),
    )
    # Newer, but deleted / hidden from the feed: they don't count.
    await client._db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content, created_at,"
        " deleted) VALUES('p-del', ?, ?, 'text', NULL, '2031-01-01 10:00:00', 1)",
        (sid, _OWNER[1]),
    )
    await client._db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content, created_at,"
        " hidden_from_feed) VALUES('p-hid', ?, ?, 'text', 'x',"
        " '2031-01-01 10:00:00', 1)",
        (sid, _OWNER[1]),
    )
    row = await _row(client, hdr["follower"], sid)
    assert row["last_activity_at"] == "2030-01-01 10:00:00"


async def test_icon_url_is_signed_when_the_space_has_an_icon(seeded):  # noqa: F811
    client, hdr, sid = seeded
    assert (await _row(client, hdr["member"], sid))["icon_url"] is None
    await client._db.enqueue("UPDATE spaces SET icon_hash='abc123' WHERE id=?", (sid,))
    url = (await _row(client, hdr["member"], sid))["icon_url"]
    assert url.startswith(f"/api/spaces/{sid}/icon?v=abc123")
    assert "sig=" in url


async def test_no_seat_no_row(seeded):  # noqa: F811
    """The guest has a pending join request but no seat: the space is not
    theirs to list (it is Discover's business)."""
    client, hdr, sid = seeded
    assert sid not in await _rows(client, hdr["guest"])


async def test_rows_are_per_caller(seeded):  # noqa: F811
    """Two callers on the same space read their own seat, never the other's."""
    client, hdr, sid = seeded
    assert (await _row(client, hdr["owner"], sid))["my_role"] == "owner"
    assert (await _row(client, hdr["follower"], sid))["my_role"] == "subscriber"


async def test_one_member_count_on_every_surface(seeded):  # noqa: F811
    """The list, the detail, the seatless landing page and the household
    directory all say the same: four members — the follower is no member."""
    client, hdr, sid = seeded
    assert (await _row(client, hdr["member"], sid))["member_count"] == 4
    r = await client.get(f"/api/spaces/{sid}", headers=hdr["follower"])
    assert (await r.json())["member_count"] == 4
    r = await client.get(f"/api/spaces/{sid}", headers=hdr["guest"])
    assert r.status == 200
    landing = await r.json()
    assert "my_role" not in landing
    assert landing["member_count"] == 4
    r = await client.get("/api/household/spaces", headers=hdr["member"])
    (listed,) = [x for x in await r.json() if x["space_id"] == sid]
    assert listed["member_count"] == 4


async def _post_at(client, sid, pid, author, at, **extra):
    cols = ", ".join(
        ["id", "space_id", "author", "type", "content", "created_at", *extra]
    )
    vals = ", ".join(["?"] * (6 + len(extra)))
    await client._db.enqueue(
        f"INSERT INTO space_posts({cols}) VALUES({vals})",
        (pid, sid, author, "text", "x", at, *extra.values()),
    )


async def test_unread_posts_count_from_the_last_feed_visit(seeded):  # noqa: F811
    """Nothing is new before the first visit; after it, posts by others
    count — never the caller's own, never a deleted or hidden one."""
    client, hdr, sid = seeded
    await _post_at(client, sid, "p-old", _OWNER[1], "2000-01-01 00:00:00")
    assert (await _row(client, hdr["member"], sid))["unread_posts"] == 0
    r = await client.post(f"/api/spaces/{sid}/feed/read", headers=hdr["member"])
    assert r.status == 204
    assert (await _row(client, hdr["member"], sid))["unread_posts"] == 0
    await _post_at(client, sid, "p-new", _OWNER[1], "2999-01-01 00:00:00")
    await _post_at(client, sid, "p-new2", _OWNER[1], "2999-01-02T00:00:00+00:00")
    await _post_at(client, sid, "p-mine", _MEMBER[1], "2999-01-01 00:00:00")
    await _post_at(client, sid, "p-del", _OWNER[1], "2999-01-01 00:00:00", deleted=1)
    await _post_at(
        client, sid, "p-hid", _OWNER[1], "2999-01-01 00:00:00", hidden_from_feed=1
    )
    assert (await _row(client, hdr["member"], sid))["unread_posts"] == 2
    # Another member's marker is their own.
    assert (await _row(client, hdr["admin"], sid))["unread_posts"] == 0


async def test_a_follower_marks_too_and_an_outsider_cannot(seeded):  # noqa: F811
    client, hdr, sid = seeded
    r = await client.post(f"/api/spaces/{sid}/feed/read", headers=hdr["follower"])
    assert r.status == 204
    r = await client.post(f"/api/spaces/{sid}/feed/read", headers=hdr["guest"])
    assert r.status == 403
    r = await client.post("/api/spaces/nope/feed/read", headers=hdr["guest"])
    assert r.status == 403
