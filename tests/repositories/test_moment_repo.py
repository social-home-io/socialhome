"""Tests for socialhome.repositories.moment_repo."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.moment import Moment
from socialhome.repositories.moment_repo import SqliteMomentRepo


def _now_iso(offset_minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)).isoformat()


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
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=expires_days)
        ).isoformat(),
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
        expires_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
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
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
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
        expires_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
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
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=expires_days)
        ).isoformat(),
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
        expires_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
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
    return f"{datetime.now(timezone.utc).date().isoformat()}T00:00:00.000001+00:00"


def _expiring(id: str, *, expired: bool) -> Moment:
    """A moment that has just expired (same UTC day) or expires tomorrow."""
    return replace(
        _moment(id=id),
        expires_at=(
            _expired_today_iso()
            if expired
            else (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
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
