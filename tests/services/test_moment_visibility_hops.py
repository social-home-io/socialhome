"""Per-viewer max-hops visibility tests (§Momentum-relay-policy)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.moment import Moment
from socialhome.repositories.moment_repo import SqliteMomentRepo
from socialhome.services.user_preferences import parse_moment_preferences


def _moment(*, moment_id: str, hop_count: int, author_id: str = "u-author") -> Moment:
    # ``list_visible_to`` filters non-followed authors' moments to the
    # last 24 h. Hardcoding ``created_at`` made the suite go red exactly
    # 24 h after whichever date was committed, so build the timestamp
    # relative to ``now`` instead. ``expires_at`` stays comfortably in
    # the future on the same relative footing.
    now = datetime.now(UTC).replace(tzinfo=None)
    return Moment(
        id=moment_id,
        author_user_id=author_id,
        content="hi",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        origin_instance_id="peer-x",
        created_at=(now - timedelta(hours=1)).isoformat(timespec="seconds"),
        expires_at=(now + timedelta(days=30)).isoformat(timespec="seconds"),
        hop_count=hop_count,
    )


@pytest.fixture
async def repo(db):
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u-self','alice','Alice','active')"
    )
    return SqliteMomentRepo(db)


async def test_default_max_hops_shows_all_three_hop_levels(repo):
    for i, h in enumerate((1, 2, 3), start=1):
        await repo.save(_moment(moment_id=f"m-{i}", hop_count=h))
    rows = await repo.list_visible_to("u-self", max_hops=3)
    assert {m.id for m in rows} == {"m-1", "m-2", "m-3"}


async def test_max_hops_one_hides_relayed_moments(repo):
    for i, h in enumerate((1, 2, 3), start=1):
        await repo.save(_moment(moment_id=f"m-{i}", hop_count=h))
    rows = await repo.list_visible_to("u-self", max_hops=1)
    assert {m.id for m in rows} == {"m-1"}


async def test_max_hops_two_shows_one_and_two(repo):
    for i, h in enumerate((1, 2, 3), start=1):
        await repo.save(_moment(moment_id=f"m-{i}", hop_count=h))
    rows = await repo.list_visible_to("u-self", max_hops=2)
    assert {m.id for m in rows} == {"m-1", "m-2"}


async def test_max_hops_clamped_to_the_wire_cap(repo):
    """Out-of-range values are clamped to the wire cap (5)."""
    for h in range(1, 6):
        await repo.save(_moment(moment_id=f"m-{h}", hop_count=h))
    rows = await repo.list_visible_to("u-self", max_hops=99)
    assert len(rows) == 5
    rows = await repo.list_visible_to("u-self", max_hops=0)
    assert {m.id for m in rows} == {"m-1"}


@pytest.mark.parametrize(("max_hops", "seen"), [(3, 3), (4, 4), (5, 5)])
async def test_a_viewer_sees_up_to_their_max_hops_everywhere(repo, max_hops, seen):
    """v_96: hops 4–5 are seen only by a viewer who looks that far — in the
    list, a thread, the hashtag counts and a single read alike."""
    for h in range(1, 6):
        m = _moment(moment_id=f"m-{h}", hop_count=h)
        await repo.save(
            Moment(
                **{
                    **{f: getattr(m, f) for f in m.__slots__},
                    "content": "#far",
                }
            )
        )
    listed = await repo.list_visible_to("u-self", max_hops=max_hops)
    assert {m.id for m in listed} == {f"m-{h}" for h in range(1, seen + 1)}
    tags = dict(await repo.list_top_hashtags("u-self", max_hops=max_hops))
    assert tags == {"far": seen}
    for h in range(1, 6):
        got = await repo.get_readable(f"m-{h}", "u-self", max_hops=max_hops)
        assert (got is not None) is (h <= seen)


@pytest.mark.parametrize(("max_hops", "seen"), [(3, {"r-3"}), (5, {"r-3", "r-5"})])
async def test_a_thread_shows_replies_up_to_the_viewers_max_hops(repo, max_hops, seen):
    await repo.save(_moment(moment_id="root", hop_count=1))
    for h in (3, 5):
        m = _moment(moment_id=f"r-{h}", hop_count=h)
        await repo.save(
            Moment(
                **{
                    **{f: getattr(m, f) for f in m.__slots__},
                    "parent_moment_id": "root",
                }
            )
        )
    replies = await repo.list_replies(
        "root", viewer_user_id="u-self", max_hops=max_hops
    )
    assert {m.id for m in replies} == seen


# ── moment_look_hops (v_96, decision C) ──────────────────────────────────


async def _prefs(db, user_id: str, blob: str) -> None:
    await db.enqueue(
        "UPDATE users SET preferences_json=? WHERE user_id=?", (blob, user_id)
    )


async def _add_user(db, user_id: str, *, protected: bool = False) -> None:
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state,"
        " child_protection_enabled) VALUES(?,?,?,'active',?)",
        (user_id, user_id, user_id, int(protected)),
    )


async def test_look_hops_defaults_to_3(repo):
    assert await repo.moment_look_hops() == 3


async def test_look_hops_is_the_widest_non_protected_person(db, repo):
    await _add_user(db, "u-far")
    await _add_user(db, "u-kid", protected=True)
    await _prefs(db, "u-self", '{"moments":{"max_hops":1}}')
    await _prefs(db, "u-far", '{"moments":{"max_hops":4}}')
    await _prefs(db, "u-kid", '{"moments":{"max_hops":5}}')
    assert await repo.moment_look_hops() == 4


async def test_look_hops_is_1_for_a_household_of_protected_people(db, repo):
    """A protected account sees hop 1 only, whatever its ``max_hops``."""
    await db.enqueue("UPDATE users SET child_protection_enabled=1")
    assert await repo.moment_look_hops() == 1


@pytest.mark.parametrize(
    "gone",
    ["deleted_at=datetime('now'), state='inactive'", "state='inactive'"],
)
async def test_look_hops_skips_removed_people(db, repo, gone):
    await _add_user(db, "u-gone")
    await _prefs(db, "u-gone", '{"moments":{"max_hops":5}}')
    assert await repo.moment_look_hops() == 5
    await db.enqueue(f"UPDATE users SET {gone} WHERE user_id='u-gone'")
    assert await repo.moment_look_hops() == 3


@pytest.mark.parametrize(
    ("blob", "look"),
    [
        ('{"moments":{"max_hops":9}}', 5),
        ('{"moments":{"max_hops":0}}', 1),
        ('{"moments":{"max_hops":"far"}}', 3),
        ('{"moments":{"max_hops":true}}', 3),
        ('{"moments":{"max_hops":null}}', 3),
        ('{"moments":"x"}', 3),
        ("not json", 3),
    ],
)
async def test_look_hops_reads_a_bad_preference_safely(db, repo, blob, look):
    await _prefs(db, "u-self", blob)
    assert await repo.moment_look_hops() == look


# ── has_visible_recipient ─────────────────────────────────────────────────


async def test_has_visible_recipient_true_when_default_user_unblocked(db, repo):
    # Default ``preferences_json='{}'`` → max_hops=3 → every hop visible.
    assert (
        await repo.has_visible_recipient(author_user_id="u-author", hop_count=3) is True
    )


async def test_has_visible_recipient_false_when_max_hops_below(db, repo):
    await db.enqueue(
        'UPDATE users SET preferences_json=\'{"moments":{"max_hops":1}}\' '
        "WHERE user_id='u-self'"
    )
    assert (
        await repo.has_visible_recipient(author_user_id="u-author", hop_count=2)
        is False
    )


async def test_has_visible_recipient_false_when_author_blocked(db, repo):
    # Single user, with a block on the inbound author.
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) "
        "VALUES('u-self', 'u-author')"
    )
    assert (
        await repo.has_visible_recipient(author_user_id="u-author", hop_count=1)
        is False
    )


async def test_has_visible_recipient_true_when_other_user_can_see(db, repo):
    """If *any* local user can see, the recipient set is non-empty."""
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u-other','bob','Bob','active')"
    )
    # u-self blocks the author, but u-other doesn't.
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) "
        "VALUES('u-self', 'u-author')"
    )
    assert (
        await repo.has_visible_recipient(author_user_id="u-author", hop_count=1) is True
    )


@pytest.mark.parametrize(
    "raw",
    ["4", '"5"', '"far"', "true", "4.5", "4.0", "null", "0", "9", "2", "[4]"],
)
async def test_the_sql_and_the_parser_read_max_hops_the_same_way(db, repo, raw):
    """Fable #11: the look query and the preference parser agree — only a
    real integer counts (a bool, a string or a fraction is the default)."""
    blob = '{"moments":{"max_hops":' + raw + "}}"
    await _prefs(db, "u-self", blob)
    assert await repo.moment_look_hops() == parse_moment_preferences(blob).max_hops


# ── Followers (the storage rule, v_96) ───────────────────────────────────
#
# A household keeps a moment when someone here looks that far OR follows its
# author (and has not blocked them); a follower then *sees* that author's
# moments whatever their ``max_hops`` — "stored" and "seen" stay one rule.


async def _follow(db, follower: str, followed: str = "u-author") -> None:
    await db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id) VALUES(?,?)",
        (follower, followed),
    )


async def _follow_via_gfs(db, follower: str, followed: str = "u-author") -> None:
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


async def _stored(repo, hop: int) -> bool:
    return await repo.has_visible_recipient(author_user_id="u-author", hop_count=hop)


@pytest.mark.parametrize("follow", [_follow, _follow_via_gfs])
async def test_a_follower_keeps_the_authors_moments_beyond_their_max_hops(
    db, repo, follow
):
    await _prefs(db, "u-self", '{"moments":{"max_hops":1}}')
    assert not await _stored(repo, 3)
    await follow(db, "u-self")
    for hop in range(1, 6):
        assert await _stored(repo, hop)
    # Someone else's moment is still not kept.
    assert not await repo.has_visible_recipient(author_user_id="u-x", hop_count=3)


async def test_a_follower_who_blocked_the_author_keeps_nothing(db, repo):
    await _prefs(db, "u-self", '{"moments":{"max_hops":1}}')
    await _follow(db, "u-self")
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id)"
        " VALUES('u-self', 'u-author')"
    )
    assert not await _stored(repo, 3)


async def test_a_guardian_block_keeps_nothing_for_that_person(db, repo):
    """A protected person whose guardian blocked the author keeps nothing,
    not even at hop 1 (where its ``max_hops`` alone would count)."""
    await db.enqueue("UPDATE users SET child_protection_enabled=1")
    await _follow(db, "u-self")
    assert await _stored(repo, 1)
    await db.enqueue(
        "INSERT INTO cp_minor_blocks(minor_user_id, blocked_user_id, blocked_by)"
        " VALUES('u-self', 'u-author', 'u-parent')"
    )
    assert not await _stored(repo, 1)


async def test_a_protected_follower_never_keeps_a_far_moment(db, repo):
    await db.enqueue("UPDATE users SET child_protection_enabled=1")
    await _follow(db, "u-self")
    assert await _stored(repo, 1)
    for hop in (2, 3, 4, 5):
        assert not await _stored(repo, hop)


async def test_a_follower_sees_the_authors_far_moments_everywhere(db, repo):
    """The read side of the same rule: the list, a tag, a single read, a
    thread and its count, the Saved list and trending tags."""
    await _add_user(db, "u-other")
    await _follow(db, "u-self")
    await repo.save(_moment(moment_id="root", hop_count=1, author_id="u-x"))
    for author, mid in (("u-author", "far"), ("u-x", "far-x")):
        m = _moment(moment_id=mid, hop_count=5, author_id=author)
        await repo.save(
            Moment(
                **{
                    **{f: getattr(m, f) for f in m.__slots__},
                    "content": "#far",
                    "parent_moment_id": "root",
                }
            )
        )
        await db.enqueue(
            "INSERT INTO moment_saves(user_id, moment_id, saved_at)"
            " VALUES('u-self', ?, datetime('now'))",
            (mid,),
        )
    for viewer, seen in (("u-self", {"far"}), ("u-other", set())):
        listed = await repo.list_visible_to(viewer, max_hops=1)
        assert {m.id for m in listed} - {"root"} == seen
        tagged = await repo.list_visible_to(viewer, max_hops=1, tag="far")
        assert {m.id for m in tagged} == seen
        for mid in ("far", "far-x"):
            got = await repo.get_readable(mid, viewer, max_hops=1)
            assert (got is not None) is (mid in seen)
        replies = await repo.list_replies("root", viewer_user_id=viewer, max_hops=1)
        assert {m.id for m in replies} == seen
        counts = await repo.count_engagement_for(
            ["root"], viewer_user_id=viewer, max_hops=1
        )
        assert counts["root"]["reply_count"] == len(seen)
    saved = await repo.list_saved("u-self", max_hops=1)
    assert {s.moment.id for s in saved} == {"far"}
    # Trending counts top-level moments only: make "far" a root.
    await db.enqueue("UPDATE moments SET parent_moment_id=NULL WHERE id='far'")
    assert dict(await repo.list_top_hashtags("u-self", max_hops=1)) == {"far": 1}
    assert dict(await repo.list_top_hashtags("u-other", max_hops=1)) == {}


async def test_a_protected_follower_still_sees_only_its_circle(db, repo):
    await db.enqueue("UPDATE users SET child_protection_enabled=1")
    await _follow(db, "u-self")
    await repo.save(_moment(moment_id="far", hop_count=2))
    assert await repo.list_visible_to("u-self", max_hops=5) == []
    assert await repo.get_readable("far", "u-self", max_hops=5) is None
