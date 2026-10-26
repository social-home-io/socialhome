"""Tests for socialhome.repositories.moment_repo."""

from __future__ import annotations

import unicodedata
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.moment import (
    Moment,
    MomentReactionRoute,
    MomentReactionSummary,
    MomentTombstone,
)
from socialhome.repositories.moment_repo import SqliteMomentRepo


def _now_iso(offset_minutes: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(minutes=offset_minutes)).isoformat()


def _moment(
    *,
    id: str = "m-1",
    author: str = "u-author",
    content: str = "hello",
    parent_moment_id: str | None = None,
    created_offset_min: int = 0,
    expires_days: int = 7,
) -> Moment:
    return Moment(
        id=id,
        author_user_id=author,
        content=content,
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=parent_moment_id,
        origin_instance_id="self",
        created_at=_now_iso(created_offset_min),
        expires_at=(datetime.now(UTC) + timedelta(days=expires_days)).isoformat(),
    )


@pytest.fixture
async def repo(db):
    await db.enqueue("INSERT OR IGNORE INTO preferences(id) VALUES('household')")
    return SqliteMomentRepo(db)


async def test_save_get_round_trip(db, repo):
    m = _moment()
    await repo.save(m)
    got = await repo.get("m-1")
    assert got is not None
    assert got.author_user_id == "u-author"
    assert got.content == "hello"


async def test_save_is_upsert(db, repo):
    """Inbound federation handlers may receive the same moment twice
    via different relay paths; the second save must not duplicate."""
    await repo.save(_moment(id="m-up", content="v1"))
    await repo.save(_moment(id="m-up", content="v2"))
    got = await repo.get("m-up")
    assert got is not None
    assert got.content == "v2"


async def test_list_visible_filters_blocked_authors(db, repo):
    await repo.save(_moment(id="m-a", author="u-bad"))
    await repo.save(_moment(id="m-b", author="u-good"))
    # Viewer u-me has u-bad on their block list.
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
        ("u-me", "u-bad"),
    )
    visible = await repo.list_visible_to("u-me")
    assert {m.id for m in visible} == {"m-b"}


async def test_list_visible_24h_default_then_7d_for_followers(db, repo):
    """A 36-h-old moment is hidden by default; if the viewer follows
    the author it surfaces (within the 7-day absolute cap)."""
    old = _moment(id="m-old", author="u-bob", created_offset_min=-36 * 60)
    await repo.save(old)
    # u-me does NOT follow u-bob → not visible.
    assert await repo.list_visible_to("u-me") == []
    # After follow → visible.
    await db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id) VALUES(?, ?)",
        ("u-me", "u-bob"),
    )
    visible = await repo.list_visible_to("u-me")
    assert [m.id for m in visible] == ["m-old"]


async def test_list_visible_drops_past_absolute_expiry(db, repo):
    """Even followers can't see moments past the 7-day expiry."""
    expired = Moment(
        id="m-expired",
        author_user_id="u-bob",
        content="ancient",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        origin_instance_id="self",
        created_at=_now_iso(-9 * 24 * 60),
        expires_at=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
    )
    await repo.save(expired)
    await db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id) VALUES(?, ?)",
        ("u-me", "u-bob"),
    )
    assert await repo.list_visible_to("u-me") == []


async def test_list_replies(db, repo):
    await repo.save(_moment(id="m-root", author="u-author"))
    await repo.save(_moment(id="m-r1", author="u-other", parent_moment_id="m-root"))
    await repo.save(_moment(id="m-r2", author="u-other2", parent_moment_id="m-root"))
    replies = await repo.list_replies("m-root")
    assert {r.id for r in replies} == {"m-r1", "m-r2"}


async def test_count_recent_for_author_excludes_replies(db, repo):
    """The 15-min rate-limit ignores replies — they're spam-free."""
    await repo.save(_moment(id="m-top", author="u-bob"))
    await repo.save(_moment(id="m-r", author="u-bob", parent_moment_id="m-top"))
    since = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    n = await repo.count_recent_for_author("u-bob", since_iso=since)
    assert n == 1


async def test_set_and_clear_reaction(db, repo):
    await repo.save(_moment(id="m-r"))
    await repo.set_reaction("m-r", "u-rx", "🔥")
    rs = await repo.list_reactions("m-r")
    assert [r.emoji for r in rs] == ["🔥"]
    # Same reactor changing emoji upserts.
    await repo.set_reaction("m-r", "u-rx", "❤️")
    rs = await repo.list_reactions("m-r")
    assert len(rs) == 1 and rs[0].emoji == "❤️"
    # Clear removes.
    await repo.clear_reaction("m-r", "u-rx")
    assert await repo.list_reactions("m-r") == []


async def test_count_engagement_for(db, repo):
    """Aggregate query returns reaction + reply counts per id."""
    await repo.save(_moment(id="m-a", author="u-a"))
    await repo.save(_moment(id="m-b", author="u-b"))
    await repo.save(_moment(id="m-c", author="u-c"))
    # m-a has 2 reactions, 1 reply; m-b has 1 reaction; m-c has neither.
    await repo.set_reaction("m-a", "u-x", "🔥")
    await repo.set_reaction("m-a", "u-y", "❤️")
    await repo.set_reaction("m-b", "u-x", "👏")
    await repo.save(_moment(id="m-rep", author="u-z", parent_moment_id="m-a"))

    counts = await repo.count_engagement_for(["m-a", "m-b", "m-c"])
    assert counts["m-a"] == {"reaction_count": 2, "reply_count": 1}
    assert counts["m-b"] == {"reaction_count": 1, "reply_count": 0}
    assert counts["m-c"] == {"reaction_count": 0, "reply_count": 0}


async def test_count_engagement_empty_list(db, repo):
    assert await repo.count_engagement_for([]) == {}


async def test_save_extracts_hashtags(db, repo):
    """``save`` materialises ``moment_hashtags`` rows from the content
    so the tag-filter query and the trending list stay in sync."""
    await repo.save(_moment(id="m-tag", content="Trip #Berlin and #berlin again"))
    tags = await repo.list_hashtags_for("m-tag")
    # Lowercased + deduped.
    assert tags == ["berlin"]


async def test_save_rewrites_hashtags_on_edit(db, repo):
    """An upsert with new content drops the old tag rows."""
    await repo.save(_moment(id="m-tag", content="hello #foo"))
    assert await repo.list_hashtags_for("m-tag") == ["foo"]
    # Federated relay update — same id, different content.
    await repo.save(_moment(id="m-tag", content="hello #bar"))
    assert await repo.list_hashtags_for("m-tag") == ["bar"]


async def test_list_visible_filters_by_tag(db, repo):
    await repo.save(_moment(id="m-a", content="hike #outdoors today"))
    await repo.save(_moment(id="m-b", content="indoor #cooking experiments"))
    await repo.save(_moment(id="m-c", content="no tags here"))
    visible = await repo.list_visible_to("u-me", tag="outdoors")
    assert {m.id for m in visible} == {"m-a"}
    visible = await repo.list_visible_to("u-me", tag="cooking")
    assert {m.id for m in visible} == {"m-b"}
    # Untagged → all three (subject to other filters).
    all_visible = await repo.list_visible_to("u-me")
    assert {m.id for m in all_visible} == {"m-a", "m-b", "m-c"}


async def test_tags_beyond_ascii_are_stored_and_filtered_whole(db, repo):
    """``#Kürbisfest`` and ``#Fußball`` were stored as ``k`` / ``fu`` and
    their filter pulled in every moment starting with those letters."""
    await repo.save(
        _moment(id="m-k", content="Bestes Wochenende!! #Kürbisfest #Fußball")
    )
    await repo.save(_moment(id="m-o", content="#Kino heute"))
    assert await repo.list_hashtags_for("m-k") == ["fußball", "kürbisfest"]
    for query in (
        "Kürbisfest",
        "#kürbisfest",
        unicodedata.normalize("NFD", "Kürbisfest"),
    ):
        visible = await repo.list_visible_to("u-me", tag=query)
        assert {m.id for m in visible} == {"m-k"}, query
    assert await repo.list_visible_to("u-me", tag="k") == []


async def test_list_top_hashtags_counts_and_orders(db, repo):
    """Most-used tag wins; alpha tiebreak keeps the order stable."""
    await repo.save(_moment(id="m-1", content="#alpha #beta"))
    await repo.save(_moment(id="m-2", content="#alpha"))
    await repo.save(_moment(id="m-3", content="#beta"))
    rows = await repo.list_top_hashtags("u-me")
    assert rows == [("alpha", 2), ("beta", 2)]


async def test_list_top_hashtags_respects_blocks(db, repo):
    """A blocked author's tags don't pollute the trending row."""
    await repo.save(_moment(id="m-bad", author="u-bad", content="#spam everywhere"))
    await repo.save(_moment(id="m-good", author="u-ok", content="#fresh tag"))
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
        ("u-me", "u-bad"),
    )
    rows = await repo.list_top_hashtags("u-me")
    assert rows == [("fresh", 1)]


async def test_prune_expired_drops_past_cap(db, repo):
    fresh = _moment(id="m-fresh")
    expired = Moment(
        id="m-expired",
        author_user_id="u-bob",
        content="old",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        origin_instance_id="self",
        created_at=_now_iso(-9 * 24 * 60),
        expires_at=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
        # Received from another household: a moment written here would
        # stay in its author's archive.
        received_via="household",
    )
    await repo.save(fresh)
    await repo.save(expired)
    pruned = await repo.prune_expired()
    assert pruned == 1
    assert await repo.get("m-fresh") is not None
    assert await repo.get("m-expired") is None


# ─── list_public_for (§Momentum-public live index) ──────────────────────


def _public_moment(
    *,
    id: str,
    author: str = "u-author",
    is_public: bool = True,
    created_offset_min: int = 0,
    expires_days: int = 7,
) -> Moment:
    return Moment(
        id=id,
        author_user_id=author,
        content="public",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        origin_instance_id="self",
        created_at=_now_iso(created_offset_min),
        expires_at=(datetime.now(UTC) + timedelta(days=expires_days)).isoformat(),
        is_public=is_public,
    )


async def test_list_public_for_returns_only_public_unexpired(db, repo):
    await repo.save(_public_moment(id="m-pub", is_public=True))
    got = await repo.list_public_for("u-author")
    assert [m.id for m in got] == ["m-pub"]
    assert all(m.is_public for m in got)


async def test_list_public_for_excludes_private(db, repo):
    """PRIVACY INVARIANT: is_public=0 moments are never returned."""
    await repo.save(_public_moment(id="m-pub", is_public=True))
    await repo.save(_public_moment(id="m-priv", is_public=False))
    got = await repo.list_public_for("u-author")
    ids = {m.id for m in got}
    assert "m-pub" in ids
    assert "m-priv" not in ids


async def test_list_public_for_excludes_expired(db, repo):
    await repo.save(_public_moment(id="m-pub", is_public=True))
    expired = Moment(
        id="m-old",
        author_user_id="u-author",
        content="old",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        origin_instance_id="self",
        created_at=_now_iso(-9 * 24 * 60),
        expires_at=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
        is_public=True,
    )
    await repo.save(expired)
    got = await repo.list_public_for("u-author")
    ids = {m.id for m in got}
    assert "m-pub" in ids
    assert "m-old" not in ids


async def test_list_public_for_orders_newest_first(db, repo):
    await repo.save(_public_moment(id="m-old", created_offset_min=-30))
    await repo.save(_public_moment(id="m-new", created_offset_min=-1))
    got = await repo.list_public_for("u-author")
    assert [m.id for m in got] == ["m-new", "m-old"]


async def test_list_public_for_respects_limit(db, repo):
    for i in range(5):
        await repo.save(_public_moment(id=f"m-{i}", created_offset_min=-i))
    got = await repo.list_public_for("u-author", limit=2)
    assert len(got) == 2


async def test_list_public_for_clamps_limit(db, repo):
    """Limit clamps to [1, 100] — a 0 or huge value is coerced."""
    await repo.save(_public_moment(id="m-1"))
    assert len(await repo.list_public_for("u-author", limit=0)) == 1
    assert len(await repo.list_public_for("u-author", limit=10_000)) == 1


async def test_list_public_for_scopes_to_author(db, repo):
    await repo.save(_public_moment(id="m-a", author="u-a"))
    await repo.save(_public_moment(id="m-b", author="u-b"))
    got = await repo.list_public_for("u-a")
    assert [m.id for m in got] == ["m-a"]


# ── Same-day expiry (timestamp-shape regression) ───────────────────────────


def _expired_today_iso() -> str:
    """A tz-aware expiry at the very start of the current UTC day.

    Always in the past, and always on the *same calendar date* as SQLite's
    ``datetime('now')`` — the only window where a raw TEXT compare went
    wrong ("T" 0x54 outranks " " 0x20 only once the date digits tie), so
    the regression fires deterministically rather than only when the wall
    clock happens to cooperate.
    """
    return f"{datetime.now(UTC).date().isoformat()}T00:00:00.000001+00:00"


def _expiring(id: str, *, expired: bool) -> Moment:
    """A moment that has just expired (same UTC day) or expires tomorrow."""
    return replace(
        _moment(id=id),
        expires_at=(
            _expired_today_iso()
            if expired
            else (datetime.now(UTC) + timedelta(days=1)).isoformat()
        ),
    )


async def test_expired_today_is_not_visible(db, repo):
    """A moment that expired earlier today is already hidden.

    Regression: ``moments.expires_at`` holds tz-aware ISO 8601 and was
    compared as raw TEXT against SQLite's naive ``datetime('now')``.
    ``"T"`` (0x54) sorts above ``" "`` (0x20), so an expired moment stayed
    visible (and publicly exposed) until the UTC date rolled over.
    """
    await repo.save(_expiring("m-gone", expired=True))
    await repo.save(_expiring("m-live", expired=False))
    ids = {m.id for m in await repo.list_visible_to("u-viewer", limit=50)}
    assert "m-gone" not in ids
    assert "m-live" in ids


async def test_expired_today_is_not_public(db, repo):
    """The public-stream query hides a moment that expired earlier today."""
    await repo.save(replace(_expiring("m-pub-gone", expired=True), is_public=True))
    await repo.save(replace(_expiring("m-pub-live", expired=False), is_public=True))
    ids = {m.id for m in await repo.list_public_for("u-author", limit=50)}
    assert ids == {"m-pub-live"}


async def test_prune_expired_collects_same_day_expiry(db, repo):
    """The retention sweep reclaims a moment that expired earlier today."""
    await repo.save(
        replace(_expiring("m-prune", expired=True), received_via="household")
    )
    await repo.save(_expiring("m-keep", expired=False))
    assert await repo.prune_expired() == 1
    rows = await db.fetchall("SELECT id FROM moments")
    assert {r["id"] for r in rows} == {"m-keep"}


# ── Delete tombstones ─────────────────────────────────────────────────────


async def _raw(db, moment_id: str):
    return await db.fetchone("SELECT * FROM moments WHERE id=?", (moment_id,))


async def test_delete_leaves_a_content_free_tombstone(db, repo):
    await repo.save(replace(_moment(id="m-t", content="#tag hi"), is_public=True))
    await repo.set_reaction("m-t", "u-fan", "x")
    await repo.delete("m-t")
    assert await repo.get("m-t") is None
    tomb = await repo.get("m-t", include_deleted=True)
    assert tomb is not None and tomb.deleted_at is not None
    assert tomb.author_user_id == "u-author"
    assert (tomb.content, tomb.media_url, tomb.is_public) == ("", None, False)
    assert await repo.list_hashtags_for("m-t") == []
    assert await repo.list_reactions("m-t") == []


async def test_tombstones_are_hidden_from_every_read(db, repo):
    await repo.save(replace(_moment(id="m-root", content="#x"), is_public=True))
    await repo.save(_moment(id="m-reply", author="u-o", parent_moment_id="m-root"))
    await repo.save(_moment(id="m-gone", author="u-o", parent_moment_id="m-root"))
    await repo.delete("m-gone")
    assert {m.id for m in await repo.list_visible_to("u-me")} == {"m-root", "m-reply"}
    assert [m.id for m in await repo.list_replies("m-root")] == ["m-reply"]
    engagement = await repo.count_engagement_for(["m-root"])
    assert engagement["m-root"]["reply_count"] == 1
    await repo.delete("m-root")
    assert {m.id for m in await repo.list_visible_to("u-me")} == {"m-reply"}
    assert await repo.list_public_for("u-author") == []
    assert await repo.list_top_hashtags("u-me") == []
    assert await repo.count_recent_for_author("u-author", since_iso=_now_iso(-5)) == 0
    # Replies detach from a deleted parent, as the old hard delete did.
    reply = await repo.get("m-reply")
    assert reply is not None and reply.parent_moment_id is None


async def test_save_never_overwrites_a_tombstone(db, repo):
    await repo.save(_moment(id="m-t", content="#tag original"))
    await repo.delete("m-t")
    await repo.save(_moment(id="m-t", content="#tag resurrected"))
    assert await repo.get("m-t") is None
    assert (await _raw(db, "m-t"))["content"] == ""
    assert await repo.list_hashtags_for("m-t") == []


async def test_tombstone_an_unknown_id_holds_the_delete(db, repo):
    expires = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    removed = await repo.tombstone(
        "m-later",
        author_user_id="u-remote",
        origin_instance_id="peer",
        expires_at=expires,
    )
    assert removed is MomentTombstone.RECORDED
    assert removed.changed
    tomb = await repo.get("m-later", include_deleted=True)
    assert tomb is not None and tomb.deleted_at is not None
    assert (tomb.author_user_id, tomb.origin_instance_id) == ("u-remote", "peer")
    assert tomb.expires_at == expires
    await repo.save(_moment(id="m-later", author="u-remote"))
    assert await repo.get("m-later") is None
    # The same delete again (a replay) changes nothing.
    replay = await repo.tombstone(
        "m-later",
        author_user_id="u-remote",
        origin_instance_id="peer",
        expires_at=expires,
    )
    assert replay is MomentTombstone.ALREADY
    assert not replay.changed


async def test_tombstone_a_stored_moment_keeps_its_expiry(db, repo):
    m = _moment(id="m-t", expires_days=3)
    await repo.save(m)
    removed = await repo.tombstone(
        "m-t",
        author_user_id="u-author",
        origin_instance_id="self",
        expires_at=_now_iso(7 * 24 * 60),
    )
    assert removed is MomentTombstone.REMOVED
    tomb = await repo.get("m-t", include_deleted=True)
    assert tomb is not None and tomb.expires_at == m.expires_at
    # A second delete is a no-op and reports nothing removed.
    again = await repo.tombstone(
        "m-t",
        author_user_id="u-author",
        origin_instance_id="self",
        expires_at=_now_iso(),
    )
    assert again is MomentTombstone.ALREADY


async def test_delete_by_author_tombstones_live_moments(db, repo):
    await repo.save(_moment(id="m-1", author="u-gone"))
    await repo.save(_moment(id="m-2", author="u-gone"))
    await repo.save(_moment(id="m-3", author="u-stays"))
    await repo.delete("m-2")
    assert await repo.delete_by_author("u-gone") == 1
    assert await repo.get("m-1") is None
    assert await repo.get("m-1", include_deleted=True) is not None
    assert await repo.get("m-3") is not None


async def test_prune_expired_sweeps_tombstones(db, repo):
    await repo.tombstone(
        "m-old",
        author_user_id="u-remote",
        origin_instance_id="peer",
        expires_at=_now_iso(-60),
    )
    await repo.tombstone(
        "m-fresh",
        author_user_id="u-remote",
        origin_instance_id="peer",
        expires_at=_now_iso(60),
    )
    assert await repo.prune_expired() == 1
    assert await _raw(db, "m-old") is None
    assert await _raw(db, "m-fresh") is not None


async def _guardian_block(db, *, minor: str, blocked: str) -> None:
    """``minor`` is protected and a guardian blocked ``blocked`` (§CP.F2)."""
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name,"
        " child_protection_enabled) VALUES(?, ?, 'Kid', 1)",
        (minor, minor),
    )
    await db.enqueue(
        "INSERT INTO cp_minor_blocks(minor_user_id, blocked_user_id, blocked_by)"
        " VALUES(?, ?, 'u-guardian')",
        (minor, blocked),
    )


async def test_list_visible_hides_guardian_blocked_authors_both_ways(db, repo):
    await _guardian_block(db, minor="u-kid", blocked="u-bad")
    await repo.save(_moment(id="m-bad", author="u-bad", content="#spam"))
    await repo.save(_moment(id="m-kid", author="u-kid", content="#mine"))
    await repo.save(_moment(id="m-ok", author="u-ok", content="#fine"))
    assert {m.id for m in await repo.list_visible_to("u-kid")} == {"m-kid", "m-ok"}
    assert {m.id for m in await repo.list_visible_to("u-bad")} == {"m-bad", "m-ok"}
    assert {m.id for m in await repo.list_visible_to("u-ok")} == {
        "m-bad",
        "m-kid",
        "m-ok",
    }
    assert dict(await repo.list_top_hashtags("u-kid")) == {"mine": 1, "fine": 1}


async def test_get_readable_follows_the_inbox_rule(db):
    """``get_readable`` and ``list_visible_to`` share one rule: a hidden
    moment and an unknown id both read as ``None``."""
    repo = SqliteMomentRepo(db)
    for uid in ("u-me", "u-bob"):
        await db.enqueue(
            "INSERT INTO users(user_id, username, display_name, state)"
            " VALUES(?, ?, ?, 'active')",
            (uid, uid, uid),
        )
    await db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
        " created_at, expires_at) VALUES('m1', 'u-bob', 'x', 'o',"
        " datetime('now'), datetime('now', '+1 day'))"
    )
    assert (await repo.get_readable("m1", "u-me")).id == "m1"
    assert await repo.get_readable("nope", "u-me") is None
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id)"
        " VALUES('u-me', 'u-bob')"
    )
    assert await repo.get_readable("m1", "u-me") is None
    assert [m.id for m in await repo.list_visible_to("u-me")] == []
    # The author always reads their own live moment.
    assert (await repo.get_readable("m1", "u-bob")).id == "m1"


async def _thread(repo) -> None:
    """``m-root`` with a reply each from u-blocked, u-bad (guardian-blocked
    for u-kid), u-far (3 hops away) and u-ok."""
    await repo.save(_moment(id="m-root", author="u-author"))
    for author in ("u-blocked", "u-bad", "u-ok"):
        await repo.save(
            _moment(id=f"r-{author}", author=author, parent_moment_id="m-root")
        )
    await repo.save(
        replace(
            _moment(id="r-u-far", author="u-far", parent_moment_id="m-root"),
            hop_count=3,
        )
    )


async def test_replies_follow_the_inbox_rule(db, repo):
    """A thread never shows a reply the inbox hides: a personal block, a
    guardian block (either way), beyond the viewer's ``max_hops`` — and
    the reply count on the parent agrees with the list."""
    await _guardian_block(db, minor="u-kid", blocked="u-bad")
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
        ("u-me", "u-blocked"),
    )
    await _thread(repo)

    async def seen(viewer: str, max_hops: int = 3) -> set[str]:
        replies = await repo.list_replies(
            "m-root", viewer_user_id=viewer, max_hops=max_hops
        )
        counts = await repo.count_engagement_for(
            ["m-root"], viewer_user_id=viewer, max_hops=max_hops
        )
        assert counts["m-root"]["reply_count"] == len(replies)
        return {r.id for r in replies}

    everyone = {"r-u-blocked", "r-u-bad", "r-u-ok", "r-u-far"}
    assert await seen("u-me") == everyone - {"r-u-blocked"}
    assert await seen("u-kid") == everyone - {"r-u-bad"}
    assert await seen("u-other", max_hops=2) == everyone - {"r-u-far"}
    # The guardian block holds the other way too.
    await repo.save(_moment(id="r-kid", author="u-kid", parent_moment_id="m-root"))
    assert "r-kid" not in await seen("u-bad")
    # Without a viewer (federation / sync): the whole thread.
    assert {r.id for r in await repo.list_replies("m-root")} == everyone | {"r-kid"}
    counts = await repo.count_engagement_for(["m-root"])
    assert counts["m-root"]["reply_count"] == 5


async def test_your_own_reply_shows_whoever_you_blocked(db, repo):
    """A viewer always sees their own live reply, as on the detail read."""
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
        ("u-me", "u-author"),
    )
    await repo.save(_moment(id="m-root", author="u-author"))
    await repo.save(_moment(id="r-me", author="u-me", parent_moment_id="m-root"))
    replies = await repo.list_replies("m-root", viewer_user_id="u-me")
    assert [r.id for r in replies] == ["r-me"]


async def test_an_older_reply_stays_in_a_thread_you_can_still_read(db, repo):
    """Replies drop out for blocks, child protection, hops and expiry —
    not for the inbox's 24-hour window. A reply from someone the viewer
    doesn't follow stays in the thread (and its count) for as long as the
    thread itself is readable, instead of vanishing after a day while the
    followed author's moment is still there."""
    await db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id) VALUES(?, ?)",
        ("u-me", "u-author"),
    )
    await repo.save(
        _moment(id="m-root", author="u-author", created_offset_min=-3 * 24 * 60)
    )
    await repo.save(
        _moment(
            id="r-stranger",
            author="u-stranger",
            parent_moment_id="m-root",
            created_offset_min=-2 * 24 * 60,
        )
    )
    assert (await repo.get_readable("m-root", "u-me")).id == "m-root"
    replies = await repo.list_replies("m-root", viewer_user_id="u-me")
    assert [r.id for r in replies] == ["r-stranger"]
    counts = await repo.count_engagement_for(["m-root"], viewer_user_id="u-me")
    assert counts["m-root"]["reply_count"] == 1


async def test_mark_public_flags_a_received_moment_only(db, repo):
    """``mark_public`` flips only a live moment received from another
    household; a moment authored here and a tombstone keep theirs."""
    await repo.save(replace(_moment(id="m-in"), received_via="household"))
    await repo.save(_moment(id="m-own"))
    await repo.save(replace(_moment(id="m-gone"), received_via="household"))
    await db.enqueue(
        "UPDATE moments SET deleted_at = datetime('now') WHERE id = 'm-gone'"
    )
    for moment_id in ("m-in", "m-own", "m-gone"):
        await repo.mark_public(moment_id)
    assert (await repo.get("m-in")).is_public is True
    assert (await repo.get("m-own")).is_public is False
    gone = await repo.get("m-gone", include_deleted=True)
    assert gone is not None and gone.is_public is False


# ── Your own archive (decision 6a) ────────────────────────────────────────


def _past(id: str, *, author: str = "u-me", via: str = "self", **kw) -> Moment:
    """A moment that expired yesterday, written here or received."""
    return replace(
        _moment(id=id, author=author, created_offset_min=-8 * 24 * 60, **kw),
        expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
        received_via=via,
    )


async def test_prune_keeps_your_own_live_moments(db, repo):
    """Retention skips a moment written here: it stays in its author's
    archive. Others' expired moments and your own tombstones still go."""
    await repo.save(_past("m-own"))
    await repo.save(_past("m-peer", author="u-peer", via="household"))
    await repo.save(_past("m-gfs", author="u-far", via="gfs"))
    await repo.save(_past("m-own-gone"))
    await repo.delete("m-own-gone")
    assert await repo.prune_expired() == 3
    rows = await db.fetchall("SELECT id FROM moments")
    assert {r["id"] for r in rows} == {"m-own"}


async def test_expired_media_urls_skip_your_own_archive(db, repo):
    await repo.save(replace(_past("m-own"), media_url="api/media/own.webp"))
    await repo.save(
        replace(
            _past("m-peer", author="u-peer", via="household"),
            media_url="api/media/peer.webp",
        )
    )
    assert await repo.list_expired_media_urls() == ["api/media/peer.webp"]


async def test_prune_sweeps_share_keys_of_archived_moments(db, repo):
    """A moment kept in its author's archive no longer keeps the item key
    a public share link needs — sharing ends with the moment."""
    await repo.save(_past("m-own"))
    await repo.save(_moment(id="m-live", author="u-me"))
    for mid in ("m-own", "m-live"):
        await db.enqueue(
            "INSERT INTO moment_public_shares(moment_id, author_user_id,"
            " item_key_sealed) VALUES(?, 'u-me', 'sealed')",
            (mid,),
        )
    await repo.prune_expired()
    rows = await db.fetchall("SELECT moment_id FROM moment_public_shares")
    assert {r["moment_id"] for r in rows} == {"m-live"}
    assert await repo.get("m-own") is not None


async def test_author_reads_their_archived_moment_and_its_own_replies(db, repo):
    await repo.save(_past("m-own"))
    await repo.save(_past("r-own", parent_moment_id="m-own"))
    assert (await repo.get_readable("m-own", "u-me")).id == "m-own"
    assert await repo.get_readable("m-own", "u-other") is None
    replies = await repo.list_replies("m-own", viewer_user_id="u-me")
    assert [r.id for r in replies] == ["r-own"]
    counts = await repo.count_engagement_for(["m-own"], viewer_user_id="u-me")
    assert counts["m-own"]["reply_count"] == 1
    # The inbox still ends at expiry.
    assert [m.id for m in await repo.list_visible_to("u-me")] == []


async def test_an_expired_received_moment_is_not_readable_by_its_author(db, repo):
    """Only rows written here are archived; a copy that arrived from
    elsewhere ends at its expiry, even for its author."""
    await repo.save(_past("m-copy", via="household"))
    assert await repo.get_readable("m-copy", "u-me") is None


async def test_list_own_archive_pages_newest_first(db, repo):
    for i in range(5):
        await repo.save(_moment(id=f"m-{i}", author="u-me", created_offset_min=-i * 60))
    await repo.save(_past("m-old"))
    await repo.save(_moment(id="m-peer", author="u-peer"))
    await repo.save(replace(_moment(id="m-copy", author="u-me"), received_via="gfs"))
    await repo.save(_moment(id="m-gone", author="u-me", created_offset_min=-30))
    await repo.delete("m-gone")
    first = await repo.list_own_archive("u-me", limit=3)
    assert [m.id for m in first] == ["m-0", "m-1", "m-2"]
    rest = await repo.list_own_archive("u-me", before=first[-1].created_at, limit=3)
    assert [m.id for m in rest] == ["m-3", "m-4", "m-old"]


async def test_list_own_archive_includes_replies(db, repo):
    await repo.save(_moment(id="m-root", author="u-peer"))
    await repo.save(_moment(id="r-me", author="u-me", parent_moment_id="m-root"))
    assert [m.id for m in await repo.list_own_archive("u-me")] == ["r-me"]


async def test_list_own_media_oldest_first_per_author(db, repo):
    await repo.save(replace(_past("m-a-old"), media_url="api/media/a1.webp"))
    await repo.save(
        replace(
            _moment(id="m-a-new", author="u-me"),
            media_url="api/media/a2.webp",
            media_type="image",
        )
    )
    await repo.save(_moment(id="m-a-text", author="u-me"))
    await repo.save(replace(_past("m-b", author="u-bea"), media_url="api/media/b.webp"))
    await repo.save(
        replace(
            _past("m-peer", author="u-peer", via="household"),
            media_url="api/media/p.webp",
        )
    )
    rows = await repo.list_own_media()
    assert [(m.author_user_id, m.id) for m in rows] == [
        ("u-bea", "m-b"),
        ("u-me", "m-a-old"),
        ("u-me", "m-a-new"),
    ]
    assert [m.id for m in await repo.list_own_media("u-me")] == ["m-a-old", "m-a-new"]


async def test_clear_media_only_on_an_archived_own_moment(db, repo):
    await repo.save(
        replace(_past("m-old"), media_url="api/media/o.webp", media_type="image")
    )
    await repo.save(
        replace(
            _moment(id="m-live", author="u-me"),
            media_url="api/media/l.webp",
            media_type="image",
        )
    )
    assert await repo.clear_media("m-old") is True
    assert await repo.clear_media("m-live") is False
    old = await repo.get("m-old")
    assert (old.media_url, old.media_type, old.content) == (None, None, "hello")
    assert (await repo.get("m-live")).media_url == "api/media/l.webp"


# ── A follow through a GFS is a follow (decision 6d) ──────────────────────


async def _follow_via_gfs(db, follower: str, followed: str) -> None:
    await db.enqueue(
        "INSERT OR IGNORE INTO users(user_id, username, display_name) VALUES(?, ?, ?)",
        (follower, follower, follower),
    )
    await db.enqueue(
        "INSERT OR IGNORE INTO gfs_connections(id, gfs_instance_id, display_name,"
        " public_key, inbox_url, status, paired_at) VALUES('g1', 'gfs-1',"
        " 'GFS One', ?, 'https://gfs1.example', 'active', datetime('now'))",
        ("aa" * 32,),
    )
    await db.enqueue(
        "INSERT INTO moment_public_follows(follower_user_id, followed_user_id,"
        " gfs_id, followed_instance_pk, followed_username, followed_display_name)"
        " VALUES(?, ?, 'g1', ?, 'noor', 'Noor')",
        (follower, followed, "bb" * 32),
    )


async def test_a_gfs_follower_keeps_the_moment_past_24_hours(db, repo):
    """Before, only ``user_follows`` counted: someone who followed an author
    through a GFS lost each moment after 24 h whatever the author chose."""
    await repo.save(
        _moment(
            id="m-gfs",
            author="u-noor",
            content="#bees still here",
            created_offset_min=-5 * 24 * 60,
            expires_days=20,
        )
    )
    assert await repo.list_visible_to("u-me") == []
    assert await repo.get_readable("m-gfs", "u-me") is None
    assert await repo.list_top_hashtags("u-me") == []
    await _follow_via_gfs(db, "u-me", "u-noor")
    assert [m.id for m in await repo.list_visible_to("u-me")] == ["m-gfs"]
    assert await repo.get_readable("m-gfs", "u-me") is not None
    assert await repo.list_top_hashtags("u-me") == [("bees", 1)]
    # Someone else in the household who follows nobody still sees 24 h.
    assert await repo.list_visible_to("u-other") == []


async def test_a_follower_sees_an_author_chosen_month(db, repo):
    """The window is the moment's ``expires_at`` — 29 days old and still
    live for a follower when the author chose 30."""
    await repo.save(
        _moment(
            id="m-month",
            author="u-bob",
            created_offset_min=-29 * 24 * 60,
            expires_days=1,
        )
    )
    await db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id) VALUES(?, ?)",
        ("u-me", "u-bob"),
    )
    assert [m.id for m in await repo.list_visible_to("u-me")] == ["m-month"]


# ── Saves (decision 6b) ───────────────────────────────────────────────────


async def _user(db, user_id: str, *, protected: bool = False) -> None:
    await db.enqueue(
        "INSERT OR IGNORE INTO users(user_id, username, display_name,"
        " child_protection_enabled) VALUES(?, ?, ?, ?)",
        (user_id, user_id, user_id, int(protected)),
    )


def _yesterday() -> str:
    return (datetime.now(UTC) - timedelta(days=1)).isoformat()


async def test_a_saved_moment_survives_expiry_with_its_media(db, repo):
    await _user(db, "u-me")
    await repo.save(
        replace(_moment(id="m-keep", author="u-peer"), received_via="household")
    )
    assert await repo.add_save("u-me", "m-keep") is True
    await db.enqueue(
        "UPDATE moments SET expires_at=?, media_url='api/media/k.webp',"
        " media_type='image' WHERE id='m-keep'",
        (_yesterday(),),
    )
    await repo.save(_past("m-other", author="u-peer", via="household"))
    assert await repo.list_expired_media_urls() == []
    assert await repo.prune_expired() == 1
    kept = await repo.get("m-keep")
    assert kept is not None and kept.media_url == "api/media/k.webp"


async def test_saver_reads_a_saved_moment_after_expiry_others_do_not(db, repo):
    await _user(db, "u-me")
    await _user(db, "u-bob")
    await repo.save(
        replace(_moment(id="m-keep", author="u-peer"), received_via="household")
    )
    await repo.add_save("u-me", "m-keep")
    await db.enqueue(
        "UPDATE moments SET expires_at=? WHERE id='m-keep'", (_yesterday(),)
    )
    assert (await repo.get_readable("m-keep", "u-me")).id == "m-keep"
    assert await repo.get_readable("m-keep", "u-bob") is None
    # The inbox still ends at expiry; the Saved list has it.
    assert await repo.list_visible_to("u-me") == []
    saved = await repo.list_saved("u-me")
    assert [s.moment.id for s in saved] == ["m-keep"]
    assert saved[0].user_id == "u-me" and saved[0].saved_at


async def test_a_saved_moment_outlives_the_24h_window_of_a_non_follower(db, repo):
    await _user(db, "u-me")
    await repo.save(
        replace(
            _moment(id="m-day", author="u-peer", created_offset_min=-30 * 60),
            received_via="household",
        )
    )
    assert await repo.get_readable("m-day", "u-me") is None
    await db.enqueue(
        "INSERT INTO moment_saves(user_id, moment_id, saved_at)"
        " VALUES('u-me', 'm-day', ?)",
        (_now_iso(),),
    )
    assert (await repo.get_readable("m-day", "u-me")).id == "m-day"


async def test_a_blocked_authors_saved_moment_is_hidden(db, repo):
    await _user(db, "u-me")
    await repo.save(
        replace(_moment(id="m-keep", author="u-peer"), received_via="household")
    )
    await repo.add_save("u-me", "m-keep")
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
        ("u-me", "u-peer"),
    )
    await db.enqueue(
        "UPDATE moments SET expires_at=? WHERE id='m-keep'", (_yesterday(),)
    )
    assert await repo.get_readable("m-keep", "u-me") is None
    assert await repo.list_saved("u-me") == []


async def test_a_protected_saver_never_reads_a_saved_gfs_moment(db, repo):
    await _user(db, "u-kid")
    await repo.save(replace(_moment(id="m-far", author="u-far"), received_via="gfs"))
    await repo.add_save("u-kid", "m-far")
    await db.enqueue(
        "UPDATE users SET child_protection_enabled=1 WHERE user_id='u-kid'"
    )
    assert await repo.get_readable("m-far", "u-kid") is None
    assert await repo.list_saved("u-kid") == []


async def test_a_save_never_points_at_a_tombstone(db, repo):
    await _user(db, "u-me")
    await repo.save(_moment(id="m-gone", author="u-peer"))
    await repo.delete("m-gone")
    assert await repo.add_save("u-me", "m-gone") is False
    assert await repo.add_save("u-me", "m-unknown") is False
    assert await repo.list_saved("u-me") == []


async def test_every_delete_path_removes_saves(db, repo):
    """The author's delete arriving over the mesh or a GFS certificate
    (``tombstone``), a local delete (``delete``) and ``USER_REMOVED``
    (``delete_by_author``) all drop the saves in the same transaction."""
    await _user(db, "u-me")
    for mid, author in (("m-a", "u-a"), ("m-b", "u-b"), ("m-c", "u-c")):
        await repo.save(
            replace(_moment(id=mid, author=author), received_via="household")
        )
        await repo.add_save("u-me", mid)
    await repo.tombstone(
        "m-a", author_user_id="u-a", origin_instance_id="self", expires_at=_now_iso(60)
    )
    await repo.delete("m-b")
    await repo.delete_by_author("u-c")
    assert await db.fetchall("SELECT moment_id FROM moment_saves") == []


async def test_add_and_remove_save_are_idempotent(db, repo):
    await _user(db, "u-me")
    await repo.save(_moment(id="m-1", author="u-peer"))
    assert await repo.add_save("u-me", "m-1") is True
    first = await repo.list_saved("u-me")
    assert await repo.add_save("u-me", "m-1") is True
    assert (await repo.list_saved("u-me"))[0].saved_at == first[0].saved_at
    assert await repo.saved_ids("u-me", ["m-1", "m-2"]) == {"m-1"}
    assert await repo.saved_ids("u-me", []) == set()
    await repo.remove_save("u-me", "m-1")
    await repo.remove_save("u-me", "m-1")
    assert await repo.saved_ids("u-me", ["m-1"]) == set()


async def test_list_saved_pages_newest_saved_first(db, repo):
    await _user(db, "u-me")
    for i in range(4):
        await repo.save(_moment(id=f"m-{i}", author="u-peer", created_offset_min=-i))
    for i, mid in enumerate(["m-2", "m-0", "m-3", "m-1"]):
        await db.enqueue(
            "INSERT INTO moment_saves(user_id, moment_id, saved_at) VALUES(?, ?, ?)",
            ("u-me", mid, _now_iso(i)),
        )
    first = await repo.list_saved("u-me", limit=2)
    assert [s.moment.id for s in first] == ["m-1", "m-3"]
    rest = await repo.list_saved("u-me", before=first[-1].saved_at, limit=2)
    assert [s.moment.id for s in rest] == ["m-0", "m-2"]


async def test_unfollow_drops_saves_of_that_authors_moments(db, repo):
    await _user(db, "u-me")
    await repo.save(_moment(id="m-a", author="u-a"))
    await repo.save(_moment(id="m-b", author="u-b"))
    await db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id) VALUES"
        " ('u-me', 'u-a'), ('u-me', 'u-b')"
    )
    await repo.add_save("u-me", "m-a")
    await repo.add_save("u-me", "m-b")
    await db.enqueue(
        "DELETE FROM user_follows WHERE follower_user_id='u-me'"
        " AND followed_user_id='u-a'"
    )
    assert await repo.saved_ids("u-me", ["m-a", "m-b"]) == {"m-b"}


async def test_gfs_unfollow_drops_saves_unless_still_followed(db, repo):
    await _follow_via_gfs(db, "u-me", "u-far")
    await repo.save(replace(_moment(id="m-far", author="u-far"), received_via="gfs"))
    await repo.add_save("u-me", "m-far")
    # Also followed through a paired household: the GFS unfollow keeps it.
    await db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id)"
        " VALUES ('u-me', 'u-far')"
    )
    await db.enqueue("DELETE FROM moment_public_follows")
    assert await repo.saved_ids("u-me", ["m-far"]) == {"m-far"}
    await db.enqueue("DELETE FROM user_follows")
    assert await repo.saved_ids("u-me", ["m-far"]) == set()


async def test_removing_a_gfs_connection_drops_its_followed_saves(db, repo):
    await _follow_via_gfs(db, "u-me", "u-far")
    await repo.save(replace(_moment(id="m-far", author="u-far"), received_via="gfs"))
    await repo.add_save("u-me", "m-far")
    await db.enqueue("DELETE FROM gfs_connections WHERE id='g1'")
    assert await repo.saved_ids("u-me", ["m-far"]) == set()


async def test_drop_saves_from_origin_only_household_copies(db, repo):
    await _user(db, "u-me")
    for mid, origin, via in (
        ("m-b", "inst-b", "household"),
        ("m-c", "inst-c", "household"),
        ("m-g", "inst-b", "gfs"),
    ):
        await repo.save(
            replace(
                _moment(id=mid, author="u-b"),
                origin_instance_id=origin,
                received_via=via,
            )
        )
        await repo.add_save("u-me", mid)
    assert await repo.drop_saves_from_origin("inst-b") == 1
    assert await repo.saved_ids("u-me", ["m-b", "m-c", "m-g"]) == {"m-c", "m-g"}


async def test_list_saved_media_and_clear_saved_media(db, repo):
    """Saved files of moments not written here, per saver, oldest save
    first. A saver's cap clears the file only when nobody else saved it."""
    await _user(db, "u-me")
    await _user(db, "u-bob")
    for mid in ("m-1", "m-2"):
        await repo.save(
            replace(
                _past(mid, author="u-peer", via="household"),
                media_url=f"api/media/{mid}.webp",
                media_type="image",
            )
        )
    await repo.save(
        replace(
            _past("m-own", author="u-mia"),
            media_url="api/media/o.webp",
            media_type="image",
        )
    )
    await repo.save(
        replace(
            _moment(id="m-live", author="u-peer"),
            media_url="api/media/l.webp",
            media_type="image",
            received_via="household",
        )
    )
    await db.enqueue(
        "INSERT INTO moment_saves(user_id, moment_id, saved_at) VALUES"
        " ('u-me', 'm-2', ?), ('u-me', 'm-1', ?), ('u-bob', 'm-2', ?),"
        " ('u-me', 'm-own', ?), ('u-me', 'm-live', ?)",
        (_now_iso(-4), _now_iso(-3), _now_iso(-2), _now_iso(-1), _now_iso(0)),
    )
    rows = await repo.list_saved_media()
    assert [(s.user_id, s.moment.id) for s in rows] == [
        ("u-bob", "m-2"),
        ("u-me", "m-2"),
        ("u-me", "m-1"),
        ("u-me", "m-live"),
    ]
    assert [s.moment.id for s in await repo.list_saved_media("u-me")] == [
        "m-2",
        "m-1",
        "m-live",
    ]
    # m-2 is also Bob's: u-me's cap cannot take it from him.
    assert await repo.clear_saved_media("m-2", "u-me") is False
    assert await repo.clear_saved_media("m-1", "u-bob") is False
    # A live moment keeps its picture for everyone who still sees it.
    assert await repo.clear_saved_media("m-live", "u-me") is False
    assert await repo.clear_saved_media("m-1", "u-me") is True
    one = await repo.get("m-1")
    assert (one.media_url, one.content) == (None, "hello")


# ── v_67: reaction routes and ordered reactions ───────────────────────────


def _route(via: str = "peer-b") -> MomentReactionRoute:
    return MomentReactionRoute(
        keywrap_pk="ab" * 32, via=via, tokens=("t1",), token_suite="aesgcm-256"
    )


async def test_reaction_route_round_trips_and_survives_resaves(db, repo):
    relayed = replace(_moment(id="m-r"), received_via="household", hop_count=2)
    await repo.save(relayed)
    await repo.set_reaction_route("m-r", _route())
    assert await repo.get_reaction_routes(["m-r", "m-none"]) == {"m-r": _route()}
    # A re-save (a closer copy, mark_public, a save) keeps it.
    await repo.save(replace(relayed, hop_count=1))
    await repo.mark_public("m-r")
    assert (await repo.get_reaction_routes(["m-r"]))["m-r"] == _route()
    assert await repo.get_reaction_routes([]) == {}


async def test_reaction_route_only_on_a_relayed_live_moment(db, repo):
    await repo.save(_moment(id="m-own"))  # received_via='self'
    await repo.set_reaction_route("m-own", _route())
    assert await repo.get_reaction_routes(["m-own"]) == {}
    relayed = replace(_moment(id="m-r"), received_via="household")
    await repo.save(relayed)
    await repo.set_reaction_route("m-r", _route())
    await repo.delete("m-r")  # tombstone
    assert await repo.get_reaction_routes(["m-r"]) == {}
    row = await db.fetchone("SELECT reaction_route FROM moments WHERE id='m-r'", ())
    assert row[0] is None


async def test_an_unreadable_route_is_skipped(db, repo):
    await repo.save(replace(_moment(id="m-r"), received_via="household"))
    await db.enqueue(
        "UPDATE moments SET reaction_route=? WHERE id='m-r'", ('{"kw": "x"}',)
    )
    assert await repo.get_reaction_routes(["m-r"]) == {}


async def test_apply_reaction_at_orders_by_time(db, repo):
    await repo.save(_moment(id="m-1"))
    at = "2026-10-26 {}:00:00".format
    assert await repo.apply_reaction_at("m-1", "u-r", "👏", reacted_at=at(10))
    # Older add loses; newer one wins.
    assert not await repo.apply_reaction_at("m-1", "u-r", "🔥", reacted_at=at("09"))
    assert await repo.apply_reaction_at("m-1", "u-r", "😂", reacted_at=at(11))
    assert [r.emoji for r in await repo.list_reactions("m-1")] == ["😂"]
    # Older removal loses; newer one deletes.
    assert not await repo.apply_reaction_at("m-1", "u-r", None, reacted_at=at(10))
    assert await repo.apply_reaction_at("m-1", "u-r", None, reacted_at=at(12))
    assert await repo.list_reactions("m-1") == []
    assert not await repo.apply_reaction_at("m-1", "u-r", None, reacted_at=at(13))
    # Never on a moment that isn't live.
    assert not await repo.apply_reaction_at("m-ghost", "u-r", "👏", reacted_at=at(13))


async def test_apply_reaction_at_keeps_a_removal_watermark(db, repo):
    """#1178 F3: a removal is remembered on disk, so an older add — late or
    replayed after a restart — never brings the reaction back, and the
    watermark never counts as a reaction."""
    await repo.save(_moment(id="m-1"))
    at = "2026-10-26 10:00:00.{:03d}".format
    assert await repo.apply_reaction_at("m-1", "u-r", "👏", reacted_at=at(100))
    assert await repo.apply_reaction_at("m-1", "u-r", None, reacted_at=at(500))
    # A removal that arrives before its add still wins over that add.
    assert not await repo.apply_reaction_at("m-1", "u-x", None, reacted_at=at(900))
    for reactor in ("u-r", "u-x"):
        assert not await repo.apply_reaction_at(
            "m-1", reactor, "👏", reacted_at=at(100)
        )
    assert await repo.list_reactions("m-1") == []
    counts = await repo.count_engagement_for(["m-1"], viewer_user_id="u-r")
    assert counts["m-1"]["reaction_count"] == 0
    assert counts["m-1"].get("my_reaction") is None
    # Milliseconds order two changes in one second; an identical replay of
    # the newest is a no-op.
    assert await repo.apply_reaction_at("m-1", "u-r", "🔥", reacted_at=at(600))
    assert not await repo.apply_reaction_at("m-1", "u-r", "🔥", reacted_at=at(600))
    assert not await repo.apply_reaction_at("m-1", "u-r", "😂", reacted_at=at(550))
    assert [r.emoji for r in await repo.list_reactions("m-1")] == ["🔥"]
    rows = await db.fetchall("SELECT reactor_user_id FROM moment_reaction_removals", ())
    assert [r[0] for r in rows] == ["u-x"]


# ── Reaction totals (v_69) ────────────────────────────────────────────────


def _summary(rev: int = 1_000) -> MomentReactionSummary:
    return MomentReactionSummary(rev=rev, counts=(("🔥", 2),), total=3)


async def test_reaction_summary_round_trips_newer_only(db, repo):
    await repo.save(replace(_moment(id="m-s"), received_via="household"))
    kw = {"author_user_id": "u-author", "origin_instance_id": "self"}
    assert await repo.set_reaction_summary("m-s", _summary(1_000), **kw)
    assert not await repo.set_reaction_summary("m-s", _summary(999), **kw)
    assert not await repo.set_reaction_summary("m-s", _summary(1_000), **kw)
    assert await repo.set_reaction_summary("m-s", _summary(2_000), **kw)
    got = await repo.get_reaction_summaries(["m-s", "m-none"])
    assert got == {"m-s": _summary(2_000)}
    # A re-save of the content row keeps it.
    await repo.save(replace(_moment(id="m-s", content="v2"), received_via="household"))
    assert (await repo.get_reaction_summaries(["m-s"]))["m-s"].rev == 2_000


async def test_reaction_summary_only_for_its_author_and_origin_live_remote(db, repo):
    await repo.save(_moment(id="m-own"))  # written here
    await repo.save(replace(_moment(id="m-r"), received_via="gfs"))
    ok = {"author_user_id": "u-author", "origin_instance_id": "self"}
    assert not await repo.set_reaction_summary("m-own", _summary(), **ok)
    assert not await repo.set_reaction_summary(
        "m-r", _summary(), author_user_id="u-other", origin_instance_id="self"
    )
    assert not await repo.set_reaction_summary(
        "m-r", _summary(), author_user_id="u-author", origin_instance_id="x"
    )
    assert await repo.set_reaction_summary("m-r", _summary(), **ok)
    await repo.delete("m-r")
    assert await repo.get_reaction_summaries(["m-r"]) == {}
    assert not await repo.set_reaction_summary("m-r", _summary(5_000), **ok)


async def test_an_unreadable_summary_is_skipped(db, repo):
    await repo.save(replace(_moment(id="m-bad"), received_via="household"))
    await db.enqueue(
        "UPDATE moments SET reaction_summary=? WHERE id='m-bad'",
        ('{"rev":1,"counts":{"x":1},"total":1}',),
    )
    assert await repo.get_reaction_summaries(["m-bad"]) == {}


async def test_reaction_tallies_and_my_reactions(db, repo):
    await repo.save(_moment(id="m-t"))
    await repo.set_reaction("m-t", "u-1", "🔥")
    await repo.set_reaction("m-t", "u-2", "🔥")
    await repo.set_reaction("m-t", "u-3", "👏")
    assert await repo.reaction_tallies(["m-t", "m-none"]) == {"m-t": {"🔥": 2, "👏": 1}}
    mine = await repo.my_reactions(["m-t"], "u-3")
    assert mine["m-t"][0] == "👏" and mine["m-t"][1]
    assert await repo.my_reactions(["m-t"], "u-9") == {}


async def test_list_own_live_since(db, repo):
    await repo.save(_moment(id="m-new"))
    await repo.save(_moment(id="m-old", created_offset_min=-5 * 24 * 60))
    await repo.save(replace(_moment(id="m-remote"), received_via="household"))
    await repo.save(_moment(id="m-gone"))
    await repo.delete("m-gone")
    since = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert [m.id for m in await repo.list_own_live_since(since)] == ["m-new"]
