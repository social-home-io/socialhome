"""The approval scope a moderation release runs in (v_43)."""

from __future__ import annotations

import asyncio

from socialhome.domain.space import MODERATION_BLOCK_KEY, ModerationApproval
from socialhome.services.moderation_release import (
    current_release,
    release_scope,
    with_release,
)


def test_no_scope_adds_nothing():
    assert current_release() is None
    payload = {"id": "t1"}
    assert with_release(payload) == {"id": "t1"}


def test_scope_adds_the_block_and_ends_with_the_block():
    with release_scope("item-1", "u-mod") as rel:
        assert rel == ModerationApproval(item_id="item-1", approved_by="u-mod")
        assert current_release() == rel
        assert with_release({"id": "t1"}) == {
            "id": "t1",
            MODERATION_BLOCK_KEY: {"item_id": "item-1", "approved_by": "u-mod"},
        }
    assert current_release() is None


def test_scope_is_restored_after_an_exception():
    try:
        with release_scope("item-1", "u-mod"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert current_release() is None


async def test_scope_follows_awaits_but_not_unrelated_tasks():
    """The bus awaits its subscribers in the publishing task, so the
    outbound bridges see the release; a task started outside does not."""
    seen: list[ModerationApproval | None] = []

    async def subscriber() -> None:
        await asyncio.sleep(0)
        seen.append(current_release())

    outside = asyncio.Event()

    async def unrelated() -> None:
        await outside.wait()
        seen.append(current_release())

    task = asyncio.create_task(unrelated())
    with release_scope("item-1", "u-mod"):
        await subscriber()
        outside.set()
        await asyncio.sleep(0)
    await task
    assert seen == [ModerationApproval("item-1", "u-mod"), None]


def test_approval_wire_round_trip_and_refusals():
    a = ModerationApproval(item_id="i", approved_by="u")
    assert ModerationApproval.from_wire(a.to_wire()) == a
    for bad in (
        None,
        "x",
        {},
        {"item_id": "i"},
        {"item_id": "", "approved_by": "u"},
        {"item_id": "i", "approved_by": 3},
        {"item_id": "i" * 65, "approved_by": "u"},
    ):
        assert ModerationApproval.from_wire(bad) is None
