"""Helpers for tests that put votes on a space poll.

A received vote carries the stamp its household made it at (the one merge
rule, :mod:`socialhome.domain.item_merge`); most tests only care that a
vote is there, so these stamp it "now".
"""

from __future__ import annotations

from datetime import UTC, datetime

from socialhome.domain.space_item import stamp_to_db


def now_stamp() -> str:
    """A stamp in the stored shape, for "just now"."""
    return stamp_to_db(datetime.now(UTC))


async def cast(repo, *, stamp: str | None = None, **vote) -> bool:
    """``set_votes_in_space`` stamped now (or ``stamp``): did the vote
    become the voter's selection?"""
    outcome = await repo.set_votes_in_space(stamp=stamp or now_stamp(), **vote)
    return outcome == "applied"


async def seed_unstamped(repo, *, option_id: str, voter_user_id: str) -> None:
    """A vote row with no stamp — as every vote was before migration 0146."""
    await repo._db.enqueue(
        "INSERT INTO space_poll_votes(option_id, voter_user_id) VALUES(?, ?)",
        (option_id, voter_user_id),
    )
