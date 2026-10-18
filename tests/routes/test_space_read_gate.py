"""The space read gate — a same-household user with no seat in a space
cannot read it.

Regression for the bug where a local user who was not a member of a
private, invite-only space got ``200`` from ``GET /api/spaces/{id}``,
``/feed`` and ``/members`` (full config, every post, the roster) while the
calendar next to them already answered ``403 Not a space member.``

The rule (:meth:`SpaceService.require_reader`): any seat reads — owner,
admin, moderator, member and a read-only follower (``subscriber``).
Nobody else does, and an unknown space id answers the same 403 as a space
the caller isn't in. The one exception is ``GET /api/spaces/{id}`` on a
discoverable (``public`` / ``global``) space: its landing page shows a
caller with no seat the directory-entry fields only.
"""

import pytest

from socialhome.auth import sha256_token_hash

from .conftest import _auth

#: Owner, follower and outsider — all local users of the same household.
_OWNER = ("olive", "uid-olive")
_FOLLOWER = ("fern", "uid-fern")
_GUEST = ("guest", "uid-guest")


async def _add_user(client, username: str, user_id: str) -> dict:
    token = f"{username}-tok"
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES(?, ?, ?, 0)",
        (username, user_id, username.title()),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES(?, ?, 't', ?)",
        (f"t-{username}", user_id, sha256_token_hash(token)),
    )
    return _auth(token)


async def _seed_space(client, sid: str, space_type: str) -> None:
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type, join_mode, allow_subscribers)"
        " VALUES(?, ?, 'inst', 'olive', ?, ?, 'invite_only', ?)",
        (sid, f"Secret {sid}", "ab" * 32, space_type, int(space_type != "private")),
    )


async def _seat(client, sid: str, user_id: str, role: str) -> None:
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
        (sid, user_id, role),
    )


async def _post(client, sid: str) -> None:
    """Post ``p-{sid}`` with one comment, both saying "family secret"."""
    await client._db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES(?, ?, ?, 'text', 'family secret')",
        (f"p-{sid}", sid, _OWNER[1]),
    )
    await client._db.enqueue(
        "INSERT INTO space_post_comments(id, post_id, author, content)"
        " VALUES(?, ?, ?, 'family secret')",
        (f"c-{sid}", f"p-{sid}", _OWNER[1]),
    )


#: Every ``GET /api/spaces/{id}/…`` read this gate covers ({sid} filled in).
_SEAT_READS = (
    "/feed",
    "/members",
    "/proposals",
    "/theme",
    "/posts/p-{sid}/comments",
)


@pytest.fixture
async def seeded(client):
    """A private space ``sp-priv`` and a global space ``sp-glob``, each
    owned by olive with fern following and one post; guest is in neither."""
    hdr = {
        "owner": await _add_user(client, *_OWNER),
        "follower": await _add_user(client, *_FOLLOWER),
        "guest": await _add_user(client, *_GUEST),
    }
    for sid, tier in (("sp-priv", "private"), ("sp-glob", "global")):
        await _seed_space(client, sid, tier)
        await _seat(client, sid, _OWNER[1], "owner")
        await _seat(client, sid, _FOLLOWER[1], "subscriber")
        await _post(client, sid)
    return client, hdr


async def _forbidden(resp) -> None:
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["error"]["code"] == "FORBIDDEN"
    assert body["error"]["detail"] == "Not a space member."


# ── The reported bug ────────────────────────────────────────────────────


async def test_non_member_cannot_read_a_private_space(seeded):
    """The reported repro: guest, in zero spaces, read a private
    invite-only space's detail, feed and roster."""
    client, hdr = seeded
    for path in ("", *_SEAT_READS):
        r = await client.get(
            "/api/spaces/sp-priv" + path.format(sid="sp-priv"), headers=hdr["guest"]
        )
        await _forbidden(r)
        assert "family secret" not in await r.text()


# ── Content + roster: any seat reads, nobody else ───────────────────────


@pytest.mark.parametrize("path", _SEAT_READS)
@pytest.mark.parametrize("sid", ["sp-priv", "sp-glob"])
async def test_space_reads_need_a_seat(seeded, sid, path):
    """Private or public, the outsider is refused; the owner and a
    read-only follower read."""
    client, hdr = seeded
    url = f"/api/spaces/{sid}" + path.format(sid=sid)
    await _forbidden(await client.get(url, headers=hdr["guest"]))
    for who in ("owner", "follower"):
        r = await client.get(url, headers=hdr[who])
        assert r.status == 200, (who, await r.text())


async def test_member_reads_the_comments(seeded):
    client, hdr = seeded
    r = await client.get(
        "/api/spaces/sp-priv/posts/p-sp-priv/comments", headers=hdr["owner"]
    )
    assert [c["content"] for c in await r.json()] == ["family secret"]


async def test_a_seat_in_one_space_does_not_read_another_spaces_comments(seeded):
    """The post id must belong to the path space: a seat in ``sp-other``
    never reads the private space's thread through its own URL."""
    client, hdr = seeded
    await _seed_space(client, "sp-other", "private")
    await _seat(client, "sp-other", _GUEST[1], "member")
    r = await client.get(
        "/api/spaces/sp-other/posts/p-sp-priv/comments", headers=hdr["guest"]
    )
    assert r.status == 404, await r.text()
    assert "family secret" not in await r.text()


async def test_member_reads_the_feed(seeded):
    client, hdr = seeded
    r = await client.get("/api/spaces/sp-priv/feed", headers=hdr["owner"])
    assert [p["content"] for p in await r.json()] == ["family secret"]


@pytest.mark.parametrize("path", ["", *_SEAT_READS])
async def test_unknown_space_is_the_same_403(seeded, path):
    """No existence oracle: an id that doesn't exist answers exactly what
    a private space the caller isn't in answers."""
    client, hdr = seeded
    await _forbidden(
        await client.get(
            "/api/spaces/sp-nope" + path.format(sid="sp-nope"), headers=hdr["guest"]
        )
    )


# ── Detail: full config for a seat, landing fields for a public space ──


async def test_member_and_follower_read_the_full_detail(seeded):
    client, hdr = seeded
    for who in ("owner", "follower"):
        r = await client.get("/api/spaces/sp-priv", headers=hdr[who])
        assert r.status == 200, await r.text()
        assert "retention_days" in await r.json()


@pytest.mark.parametrize("tier", ["private", "household"])
async def test_non_member_cannot_read_a_non_discoverable_space(seeded, tier):
    client, hdr = seeded
    await _seed_space(client, f"sp-{tier}-2", tier)
    await _forbidden(await client.get(f"/api/spaces/sp-{tier}-2", headers=hdr["guest"]))


@pytest.mark.parametrize("tier", ["public", "global"])
async def test_non_member_sees_only_the_landing_fields_of_a_public_space(seeded, tier):
    client, hdr = seeded
    await _seed_space(client, f"sp-{tier}-2", tier)
    await _seat(client, f"sp-{tier}-2", _OWNER[1], "owner")
    r = await client.get(f"/api/spaces/sp-{tier}-2", headers=hdr["guest"])
    assert r.status == 200, await r.text()
    assert await r.json() == {
        "id": f"sp-{tier}-2",
        "name": f"Secret sp-{tier}-2",
        "description": None,
        "emoji": None,
        "space_type": tier,
        "join_mode": "invite_only",
        "category": "general",
        "features": {"allow_subscribers": True},
        "member_count": 1,
    }


async def test_landing_is_hidden_from_a_protected_minor_below_its_age_gate(seeded):
    """§CP.F1: the directories hide an age-gated space from a protected
    minor below the gate — its landing page answers the same 403."""
    client, hdr = seeded
    await _seed_space(client, "sp-adult", "public")
    await client._db.enqueue("UPDATE spaces SET min_age=18 WHERE id='sp-adult'")
    r = await client.get("/api/spaces/sp-adult", headers=hdr["guest"])
    assert r.status == 200, await r.text()
    await client._db.enqueue(
        "UPDATE users SET child_protection_enabled=1, declared_age=13 WHERE user_id=?",
        (_GUEST[1],),
    )
    await _forbidden(await client.get("/api/spaces/sp-adult", headers=hdr["guest"]))


# ── Neighbouring holes: reactions and polls by post id ──────────────────


async def test_non_member_cannot_react_to_a_space_post(seeded):
    """Reacting was the write next door with the same gap: no seat was
    checked, and the path space was ignored."""
    client, hdr = seeded
    url = "/api/spaces/sp-priv/posts/p-sp-priv/reactions"
    await _forbidden(await client.post(url, json={"emoji": "x"}, headers=hdr["guest"]))
    await _forbidden(await client.delete(f"{url}/x", headers=hdr["guest"]))
    r = await client.get("/api/spaces/sp-priv/feed", headers=hdr["owner"])
    assert (await r.json())[0]["reactions"] == {}


async def test_a_reaction_binds_the_post_to_the_path_space(seeded):
    """Olive sits in both spaces: the private post is still not reachable
    through the global space's URL."""
    client, hdr = seeded
    r = await client.post(
        "/api/spaces/sp-glob/posts/p-sp-priv/reactions",
        json={"emoji": "x"},
        headers=hdr["owner"],
    )
    assert r.status == 404, await r.text()
    r = await client.post(
        "/api/spaces/sp-priv/posts/p-sp-priv/reactions",
        json={"emoji": "x"},
        headers=hdr["owner"],
    )
    assert r.status == 201, await r.text()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/posts/p-sp-priv/poll", None),
        ("post", "/posts/p-sp-priv/poll/vote", {"option_id": "o1"}),
        ("get", "/schedule-polls/p-sp-priv/summary", None),
    ],
)
async def test_a_seat_elsewhere_does_not_reach_another_spaces_poll(
    seeded, method, path, body
):
    """Polls are keyed by post id alone: a seat in ``sp-other`` must not
    read or vote on the private space's poll through its own URL."""
    client, hdr = seeded
    await client._db.enqueue(
        "INSERT INTO space_polls(post_id, question) VALUES('p-sp-priv', 'family secret')"
    )
    await client._db.enqueue(
        "INSERT INTO space_poll_options(id, post_id, text) VALUES('o1', 'p-sp-priv', 'yes')"
    )
    await _seed_space(client, "sp-other", "private")
    await _seat(client, "sp-other", _GUEST[1], "member")
    r = await getattr(client, method)(
        f"/api/spaces/sp-other{path}", json=body, headers=hdr["guest"]
    )
    assert r.status == 404, await r.text()
    assert "family secret" not in await r.text()
    assert await client._db.fetchall("SELECT * FROM space_poll_votes") == []
