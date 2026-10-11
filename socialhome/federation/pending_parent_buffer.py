"""Space items held until the post (or parent comment) they reference lands.

A comment, a reaction, an edit or a delete names the row it hangs off — the
post, or for a reply the parent comment. With several delivery paths per
space (direct federation, ``SPACE_ROUTED``, the host's relay of a member's
write, the v_49 member publish over one or more GFSs, a private space's
opaque channel, §25.6 sync) the item routinely overtakes its parent: a
member's comment reaches the host over the direct path before the post
comes back over a GFS, or a household that just came back online hears the
comment live before the catch-up carries the post. Refusing it then loses
it on that household for good when the post later arrives live (nothing
resends a write that was answered ``ok``).

:class:`PendingParentBuffer` holds such an item and replays it when the
parent lands — on :class:`~socialhome.domain.events.SpacePostCreated`,
:class:`~socialhome.domain.events.SpacePostSynced` (a post) or
:class:`~socialhome.domain.events.CommentAdded` (a parent comment). The
replay is the caller's own closure and re-runs the item's door from the top
(the §24.11 post-decrypt gates and the handler for a federated event; the
decrypt, writer-cert, author-signature and roster checks for a
``space_item``), so every authorship and writer check is taken again with
the parent in place.

Only an **absent** parent is waited for. A parent held here as a deleted
row (a tombstone) or in another space is a refusal, never a hold — the
caller checks before it holds. A parent that never arrives drops the item
when its entry expires, logged at DEBUG. Bounded the same way as the seat
buffer (:class:`~socialhome.federation.bounded_hold.BoundedHold`): entries,
entries per sending household, entries per parent, bytes, TTL.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.events import CommentAdded, SpacePostCreated, SpacePostSynced
from .bounded_hold import BoundedHold, HoldKey

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus

log = logging.getLogger(__name__)

#: The parent kinds an item may wait for.
PARENT_POST = "post"
PARENT_COMMENT = "comment"

#: Defaults — long enough for a host coming back online to hear the post
#: over another path, small against a flood.
DEFAULT_TTL_SECONDS = 600.0
DEFAULT_MAX_ENTRIES = 512
DEFAULT_MAX_PER_KEY = 64
DEFAULT_MAX_PER_SENDER = 128
DEFAULT_MAX_BYTES = 4 * 1024 * 1024

#: Re-runs the held item's door from the top. Never expected to raise.
Replay = Callable[[], Awaitable[None]]


class AwaitingParent(Exception):
    """An item's parent row is not held here at all (not deleted, not in
    another space — simply absent). Raised by an apply step that cannot
    replay itself; its caller holds the whole frame in the buffer."""

    def __init__(self, kind: str, parent_id: str) -> None:
        super().__init__(f"{kind} {parent_id} not held here yet")
        self.kind = kind
        self.parent_id = parent_id


@dataclass(slots=True, frozen=True)
class _Waiting:
    """Buffer-local record (never leaves this module)."""

    replay: Replay
    label: str


class PendingParentBuffer:
    """Bounded, expiring hold for space items waiting on their parent row."""

    __slots__ = ("_hold",)

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_per_key: int = DEFAULT_MAX_PER_KEY,
        max_per_sender: int = DEFAULT_MAX_PER_SENDER,
        max_bytes: int = DEFAULT_MAX_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hold: BoundedHold[_Waiting] = BoundedHold(
            ttl_seconds=ttl_seconds,
            max_entries=max_entries,
            max_per_key=max_per_key,
            max_per_sender=max_per_sender,
            max_bytes=max_bytes,
            clock=clock,
            on_expire=_log_expired,
        )

    def __len__(self) -> int:
        return len(self._hold)

    def purge(self) -> None:
        """Drop every expired entry now (also done on each hold / release)."""
        self._hold.purge()

    def attach_bus(self, bus: "EventBus") -> None:
        """Replay held items when their parent lands."""
        bus.subscribe(SpacePostCreated, self._on_post_created)
        bus.subscribe(SpacePostSynced, self._on_post_synced)
        bus.subscribe(CommentAdded, self._on_comment_added)

    def hold(
        self,
        *,
        space_id: str,
        kind: str,
        parent_id: str,
        token: str,
        sender: str,
        size: int,
        replay: Replay,
        label: str,
    ) -> bool:
        """Hold ``replay`` until ``kind`` ``parent_id`` lands in ``space_id``.

        ``token`` identifies the delivery (a second copy of the same item
        over another GFS takes no second slot); ``sender`` is the household
        the per-sender cap charges. ``False`` when it cannot be held — the
        caller then refuses the item as before."""
        if not space_id or not parent_id or kind not in (PARENT_POST, PARENT_COMMENT):
            return False
        if self._hold.hold(
            (space_id, kind, parent_id),
            _Waiting(replay, label),
            token=token,
            sender=sender,
            size=size,
        ):
            log.debug(
                "%s: %s %s not held here yet — holding until it lands",
                label,
                kind,
                parent_id,
            )
            return True
        log.info(
            "%s: parent-wait buffer is full — not holding (waits on %s %s)",
            label,
            kind,
            parent_id,
        )
        return False

    async def release(self, *, space_id: str, kind: str, parent_id: str) -> int:
        """``kind`` ``parent_id`` landed in ``space_id``: replay every item
        held for it, oldest first. Returns how many were replayed."""
        waiting = self._hold.release((space_id, kind, parent_id))
        for w in waiting:
            log.info("replaying %s now that %s %s landed", w.label, kind, parent_id)
            try:
                await w.replay()
            except Exception:
                log.exception("replay of held %s failed", w.label)
        return len(waiting)

    async def _on_post_created(self, event: SpacePostCreated) -> None:
        await self.release(
            space_id=event.space_id, kind=PARENT_POST, parent_id=event.post.id
        )

    async def _on_post_synced(self, event: SpacePostSynced) -> None:
        await self.release(
            space_id=event.space_id, kind=PARENT_POST, parent_id=event.post.id
        )

    async def _on_comment_added(self, event: CommentAdded) -> None:
        if event.space_id:
            await self.release(
                space_id=event.space_id,
                kind=PARENT_COMMENT,
                parent_id=event.comment.id,
            )


def _log_expired(key: HoldKey, waiting: _Waiting) -> None:
    space_id, kind, parent_id = key
    log.debug(
        "%s: %s %s never arrived in space %s — held item dropped",
        waiting.label,
        kind,
        parent_id,
        space_id,
    )
