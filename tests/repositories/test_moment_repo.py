"""Tests for socialhome.repositories.moment_repo."""

from __future__ import annotations

import unicodedata
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.moment import Moment
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
    await repo.save(_expiring("m-prune", expired=True))
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
    assert removed is False
    tomb = await repo.get("m-later", include_deleted=True)
    assert tomb is not None and tomb.deleted_at is not None
    assert (tomb.author_user_id, tomb.origin_instance_id) == ("u-remote", "peer")
    assert tomb.expires_at == expires
    await repo.save(_moment(id="m-later", author="u-remote"))
    assert await repo.get("m-later") is None


async def test_tombstone_a_stored_moment_keeps_its_expiry(db, repo):
    m = _moment(id="m-t", expires_days=3)
    await repo.save(m)
    removed = await repo.tombstone(
        "m-t",
        author_user_id="u-author",
        origin_instance_id="self",
        expires_at=_now_iso(7 * 24 * 60),
    )
    assert removed is True
    tomb = await repo.get("m-t", include_deleted=True)
    assert tomb is not None and tomb.expires_at == m.expires_at
    # A second delete is a no-op and reports nothing removed.
    again = await repo.tombstone(
        "m-t",
        author_user_id="u-author",
        origin_instance_id="self",
        expires_at=_now_iso(),
    )
    assert again is False


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
