"""Per-viewer max-hops visibility tests (§Momentum-relay-policy)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.moment import Moment
from socialhome.repositories.moment_repo import SqliteMomentRepo


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


async def test_max_hops_clamped_above_three(repo):
    """Out-of-range values are clamped to the wire cap (3)."""
    for i, h in enumerate((1, 2, 3), start=1):
        await repo.save(_moment(moment_id=f"m-{i}", hop_count=h))
    rows = await repo.list_visible_to("u-self", max_hops=99)
    assert len(rows) == 3


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
