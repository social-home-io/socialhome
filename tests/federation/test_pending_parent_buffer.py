"""Space items held until the post (or parent comment) they reference lands."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from socialhome.domain.events import CommentAdded, SpacePostCreated, SpacePostSynced
from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.federation.pending_parent_buffer import (
    PARENT_COMMENT,
    PARENT_POST,
    AwaitingParent,
    PendingParentBuffer,
)
from socialhome.infrastructure.event_bus import EventBus


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def replay(self, name: str):
        async def _run() -> None:
            self.calls.append(name)

        return _run


def _post(pid: str) -> Post:
    return Post(
        id=pid, author="u", type=PostType.TEXT, created_at=datetime.now(timezone.utc)
    )


def _comment(cid: str) -> Comment:
    return Comment(
        id=cid,
        post_id="p",
        author="u",
        type=CommentType.TEXT,
        created_at=datetime.now(timezone.utc),
        content="x",
    )


def _hold(buf, rec, name, *, kind=PARENT_POST, parent="p", sender="s", space="sp"):
    return buf.hold(
        space_id=space,
        kind=kind,
        parent_id=parent,
        token=name,
        sender=sender,
        size=10,
        replay=rec.replay(name),
        label=name,
    )


async def test_a_post_landing_replays_what_waited_for_it() -> None:
    bus = EventBus()
    buf = PendingParentBuffer()
    buf.attach_bus(bus)
    rec = _Recorder()
    assert _hold(buf, rec, "c1")
    assert _hold(buf, rec, "c2")
    assert _hold(buf, rec, "other", parent="q")
    await bus.publish(SpacePostCreated(post=_post("p"), space_id="sp"))
    assert rec.calls == ["c1", "c2"]
    assert len(buf) == 1


async def test_a_synced_post_also_releases() -> None:
    bus = EventBus()
    buf = PendingParentBuffer()
    buf.attach_bus(bus)
    rec = _Recorder()
    _hold(buf, rec, "c1")
    await bus.publish(SpacePostSynced(post=_post("p"), space_id="sp"))
    assert rec.calls == ["c1"]


async def test_a_parent_comment_landing_releases_its_replies() -> None:
    bus = EventBus()
    buf = PendingParentBuffer()
    buf.attach_bus(bus)
    rec = _Recorder()
    _hold(buf, rec, "reply", kind=PARENT_COMMENT, parent="c")
    # A household-feed comment (no space) releases nothing.
    await bus.publish(CommentAdded(post_id="p", comment=_comment("c")))
    assert rec.calls == []
    await bus.publish(CommentAdded(post_id="p", comment=_comment("c"), space_id="sp"))
    assert rec.calls == ["reply"]


async def test_another_space_does_not_release() -> None:
    buf = PendingParentBuffer()
    rec = _Recorder()
    _hold(buf, rec, "c1")
    assert await buf.release(space_id="sp-2", kind=PARENT_POST, parent_id="p") == 0
    assert await buf.release(space_id="sp", kind=PARENT_COMMENT, parent_id="p") == 0
    assert await buf.release(space_id="sp", kind=PARENT_POST, parent_id="p") == 1


async def test_a_parent_that_never_lands_drops_the_item_quietly(caplog) -> None:
    clock = _Clock()
    buf = PendingParentBuffer(ttl_seconds=60, clock=clock)
    rec = _Recorder()
    _hold(buf, rec, "c1")
    clock.now += 61
    with caplog.at_level(logging.DEBUG, logger="socialhome.federation"):
        assert await buf.release(space_id="sp", kind=PARENT_POST, parent_id="p") == 0
    assert rec.calls == []
    assert len(buf) == 0
    dropped = [r for r in caplog.records if "never arrived" in r.getMessage()]
    assert dropped and all(r.levelno == logging.DEBUG for r in dropped)


async def test_a_flood_from_one_household_cannot_fill_the_buffer() -> None:
    buf = PendingParentBuffer(max_per_sender=8, max_entries=20)
    rec = _Recorder()
    held = sum(_hold(buf, rec, f"f{n}", parent=f"p{n}") for n in range(100))
    assert held == 8
    assert _hold(buf, rec, "honest", sender="other")
    assert len(buf) == 9


async def test_unknown_kinds_and_empty_ids_are_not_held() -> None:
    buf = PendingParentBuffer()
    rec = _Recorder()
    assert not _hold(buf, rec, "x", kind="task")
    assert not _hold(buf, rec, "x", parent="")
    assert not _hold(buf, rec, "x", space="")


async def test_a_failing_replay_does_not_stop_the_others() -> None:
    buf = PendingParentBuffer()
    rec = _Recorder()

    async def _boom() -> None:
        raise RuntimeError("boom")

    buf.hold(
        space_id="sp",
        kind=PARENT_POST,
        parent_id="p",
        token="boom",
        sender="s",
        size=1,
        replay=_boom,
        label="boom",
    )
    _hold(buf, rec, "ok")
    assert await buf.release(space_id="sp", kind=PARENT_POST, parent_id="p") == 2
    assert rec.calls == ["ok"]


def test_awaiting_parent_names_the_parent() -> None:
    wait = AwaitingParent(PARENT_POST, "p")
    assert (wait.kind, wait.parent_id) == (PARENT_POST, "p")
    assert "p" in str(wait)
