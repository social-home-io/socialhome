"""Space items held until the post (or parent comment) they reference lands."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from socialhome.domain.events import (
    CommentAdded,
    SpaceCommentSynced,
    SpacePostCreated,
    SpacePostSynced,
)
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


async def test_a_reply_chain_far_deeper_than_the_stack_drains_iteratively() -> None:
    """Each replayed comment lands and publishes ``CommentAdded``, which
    releases the next reply from inside the outer replay. A recursive drain
    nests one replay per level and overflows the stack; the queue doesn't."""
    bus = EventBus()
    depth = 3000
    buf = PendingParentBuffer(
        max_entries=depth,
        max_per_key=1,
        max_per_sender=depth,
        max_bytes=depth * 1024,
        max_bytes_per_sender=depth * 1024,
    )
    buf.attach_bus(bus)
    landed: list[int] = []

    def _replay(n: int):
        async def _run() -> None:
            landed.append(n)
            await bus.publish(
                CommentAdded(post_id="p", comment=_comment(f"c{n}"), space_id="sp")
            )

        return _run

    for n in range(1, depth + 1):
        assert buf.hold(
            space_id="sp",
            kind=PARENT_COMMENT,
            parent_id=f"c{n - 1}",
            token=f"c{n}",
            sender="s",
            size=1,
            replay=_replay(n),
            label=f"c{n}",
        )
    replayed = await buf.release(space_id="sp", kind=PARENT_COMMENT, parent_id="c0")
    assert replayed == depth
    assert landed == list(range(1, depth + 1))
    assert len(buf) == 0


async def test_a_release_inside_a_drain_only_enqueues() -> None:
    buf = PendingParentBuffer()
    rec = _Recorder()
    inner: list[int] = []

    async def _outer() -> None:
        inner.append(await buf.release(space_id="sp", kind=PARENT_POST, parent_id="q"))

    buf.hold(
        space_id="sp",
        kind=PARENT_POST,
        parent_id="p",
        token="outer",
        sender="s",
        size=1,
        replay=_outer,
        label="outer",
    )
    _hold(buf, rec, "nested", parent="q")
    assert await buf.release(space_id="sp", kind=PARENT_POST, parent_id="p") == 2
    assert inner == [0]
    assert rec.calls == ["nested"]


async def test_a_comment_stored_by_sync_releases_its_replies() -> None:
    bus = EventBus()
    buf = PendingParentBuffer()
    buf.attach_bus(bus)
    rec = _Recorder()
    _hold(buf, rec, "reply", kind=PARENT_COMMENT, parent="c")
    await bus.publish(SpaceCommentSynced(comment=_comment("c"), space_id="sp"))
    assert rec.calls == ["reply"]


async def test_one_household_cannot_take_the_shared_bytes() -> None:
    buf = PendingParentBuffer(max_bytes=100_000, max_bytes_per_sender=10_000)
    rec = _Recorder()

    def _big(name: str, sender: str) -> bool:
        return buf.hold(
            space_id="sp",
            kind=PARENT_POST,
            parent_id=name,
            token=name,
            sender=sender,
            size=4_000,
            replay=rec.replay(name),
            label=name,
        )

    # 4 000 B + the per-entry overhead: two fit in 10 000, the third doesn't.
    assert _big("a", "s") and _big("b", "s")
    assert not _big("c", "s")
    assert _big("d", "other")


async def test_an_oversized_item_is_not_held() -> None:
    buf = PendingParentBuffer(max_item_bytes=1_000)
    rec = _Recorder()
    assert not buf.hold(
        space_id="sp",
        kind=PARENT_POST,
        parent_id="p",
        token="big",
        sender="s",
        size=1_001,
        replay=rec.replay("big"),
        label="big",
    )
    assert len(buf) == 0
