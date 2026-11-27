"""The durable retry queue of a GFS space's content items (v_97).

Owner 2026-11-27: content in a GFS space (a global space, or a private one
with ``private_gfs`` on) waits in a PERSISTED queue (``space_item_outbox``,
migration 0158) until every planned GFS accepted it — it is never handed to
the legacy per-household fan-out instead (that path is only for households
below v_97 and the 14-day window).

* **Per planned GFS.** One row per (item, GFS). A row goes when that GFS
  accepted the item. A transient answer — the GFS down, a timeout, a 5xx,
  a 429 — moves the row on by :func:`~socialhome.domain.space_item_outbox
  .backoff_s` (a ``Retry-After`` honoured) and holds every other row of
  that GFS until then (they would get the same answer); a reconnect to the
  GFS makes them due at once (:meth:`SpaceItemOutbox.expedite`).
* **Permanent errors reach the author.** A permanent refusal (a 4xx — not
  seated, unknown channel), an item too large for any GFS, a full queue and
  a sealing failure are kept as ``failed`` rows; the author's SPA shows
  "Couldn't send to {space}" with Retry / Discard (``GET / POST / DELETE
  /api/spaces/{id}/unsent``) and is told by a ``space.unsent`` frame.
* **Bounded.** :data:`~socialhome.domain.space_item_outbox
  .SPACE_QUEUE_MAX_ITEMS` rows / :data:`~socialhome.domain.space_item_outbox
  .SPACE_QUEUE_MAX_BYTES` per space; past it a new item is refused (the
  author sees "too many waiting").
* **Budgets.** The sender budget only sets when an item is first due; it
  waits for its slot here, so a restart loses nothing.

The loop follows the scheduler pattern (``_stop: asyncio.Event``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from ..domain.gfs_channel import (
    MEMBER_PUBLISH_MAX_PAYLOAD_CHARS,
    RING_CLASS_EPHEMERAL,
    RING_DURABLE_TTL_S,
    RING_EPHEMERAL_TTL_S,
)
from ..domain.space_item_outbox import (
    MEDIA_UPLOAD_ROW,
    UNPLANNED_GFS,
    UNSENT_QUEUE_FULL,
    UNSENT_REFUSED,
    UNSENT_SEAL_FAILED,
    UNSENT_TOO_LARGE,
    UNSENT_TOO_OLD,
    QueuedSpaceItem,
    UnsentItem,
    backoff_s,
)
from ..repositories.space_item_outbox_repo import (
    GONE_DROPPED,
    GONE_EXHAUSTED,
    AbstractSpaceItemOutboxRepo,
)
from .gfs_publish_retry import PublishOutcome

log = logging.getLogger(__name__)

#: Rows tried per pass (items; file uploads — up to 8 MiB each).
BATCH: int = 100
MEDIA_BATCH: int = 8
#: Longest sleep between passes when nothing is due sooner.
IDLE_WAKE_S: float = 60.0
#: How often an item whose last GFS stopped serving the space is planned
#: afresh before its author is told.
MAX_REPLANS: int = 3
#: Failed items nobody retried or discarded go after this long, checked
#: at most this often.
FAILED_RETENTION: timedelta = timedelta(days=7)
FAILED_SWEEP_INTERVAL: timedelta = timedelta(hours=1)

#: How old an item may be when it is first accepted: receivers drop an item
#: whose signed stamp is older than the ring keeps it (24 h; 15 min for a
#: live location pin), and a household fetching from the ring later reads
#: it older still — so a row waiting past half of that is never sent; its
#: author is told instead (finding 8 of the 10k review).
MAX_QUEUED_AGE: timedelta = timedelta(seconds=RING_DURABLE_TTL_S // 2)
MAX_QUEUED_AGE_EPHEMERAL: timedelta = timedelta(seconds=RING_EPHEMERAL_TTL_S // 2)

#: The author of an item that names nobody: shown to the household's
#: admins.
NO_AUTHOR: str = ""

#: One attempt to put ``row`` on ``row.gfs_id``.
Sender = Callable[[QueuedSpaceItem], Awaitable[PublishOutcome]]
#: The GFS ids a space's item goes to now; ``None`` when the space no longer
#: has a channel here.
Planner = Callable[[str], Awaitable[list[str] | None]]
#: ``(space_id, author_user_id)`` — the author's unsent items changed.
Notify = Callable[[str, str], Awaitable[None]]


def _is_media(row: QueuedSpaceItem) -> bool:
    return row.event_type == MEDIA_UPLOAD_ROW


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SpaceItemOutbox:
    """Queue, retry and surface a GFS space's content items (see the module
    docstring)."""

    __slots__ = (
        "_changed",
        "_clock",
        "_media_sender",
        "_media_task",
        "_media_wake",
        "_notify",
        "_planner",
        "_repo",
        "_sender",
        "_stop",
        "_swept_at",
        "_task",
        "_wake",
    )

    def __init__(
        self,
        repo: AbstractSpaceItemOutboxRepo,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._sender: Sender | None = None
        #: v_97 — one attempt of a space file upload row (the uploader).
        self._media_sender: Sender | None = None
        self._planner: Planner | None = None
        self._notify: Notify | None = None
        self._changed: Callable[[str], None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        #: v_97 — the file-upload lane (its own loop: an 8 MiB upload never
        #: holds an item back, nor a backlog of uploads hides one).
        self._media_wake = asyncio.Event()
        self._media_task: asyncio.Task | None = None
        self._swept_at: datetime | None = None

    def attach(self, *, sender: Sender, planner: Planner) -> None:
        """The channel service's one-attempt send and its planner."""
        self._sender = sender
        self._planner = planner

    def attach_media(self, sender: Sender) -> None:
        """v_97 — the uploader's one-attempt send of a space file blob or
        piece (rows of kind :data:`~socialhome.domain.space_item_outbox
        .MEDIA_UPLOAD_ROW`): the same queue, retries and author notice as
        the items."""
        self._media_sender = sender

    def attach_notify(self, notify: Notify) -> None:
        """Tell the author their unsent items changed (a WS frame)."""
        self._notify = notify

    def attach_changed(self, changed: Callable[[str], None]) -> None:
        """A space's queue changed (the sync status re-counts
        ``waiting_to_send``)."""
        self._changed = changed

    def _now(self) -> str:
        return _iso(self._clock())

    # ── Queueing ─────────────────────────────────────────────────────────

    async def queue(
        self,
        *,
        space_id: str,
        epoch: int,
        payload: str,
        event_type: str,
        ring_class: str,
        author_user_id: str,
        gfs_ids: list[str],
        delay_s: float = 0.0,
        supersede_key: str | None = None,
    ) -> bool:
        """Store the sealed item for each of *gfs_ids* (none planned yet:
        one row planned when due), first due in *delay_s* (its budget
        slot). ``False`` — and the author is told — when it is too large
        for any GFS or the space's queue is full."""
        if len(payload) > MEMBER_PUBLISH_MAX_PAYLOAD_CHARS:
            await self.fail(
                space_id=space_id,
                event_type=event_type,
                author_user_id=author_user_id,
                error=UNSENT_TOO_LARGE,
            )
            return False
        now = self._clock()
        due = _iso(now + timedelta(seconds=max(0.0, delay_s)))
        item_id = uuid.uuid4().hex
        rows = [
            QueuedSpaceItem(
                item_id=item_id,
                gfs_id=gfs_id,
                space_id=space_id,
                epoch=int(epoch),
                ring_class=ring_class,
                event_type=event_type,
                author_user_id=author_user_id,
                supersede_key=supersede_key,
                payload=payload,
                next_attempt_at=due,
                created_at=_iso(now),
            )
            for gfs_id in (list(dict.fromkeys(gfs_ids)) or [UNPLANNED_GFS])
        ]
        if not await self._repo.add(rows):
            log.warning(
                "space items: the queue of space %s is full — a %s was not sent",
                space_id,
                event_type,
            )
            await self.fail(
                space_id=space_id,
                event_type=event_type,
                author_user_id=author_user_id,
                error=UNSENT_QUEUE_FULL,
            )
            return False
        self._space_changed(space_id)
        self._wake_lane(event_type == MEDIA_UPLOAD_ROW)
        return True

    async def queue_media(
        self,
        *,
        space_id: str,
        epoch: int,
        parts: list[tuple[str, list[str]]],
        author_user_id: str,
    ) -> bool:
        """v_97 — one space file's upload rows (*parts*: each blob or
        piece's locator and the GFSs it goes to), stored all or none in one
        transaction (a file is never half queued). ``False`` — and its
        author told — when the space's file lane is full."""
        now = self._clock()
        rows = [
            QueuedSpaceItem(
                item_id=uuid.uuid4().hex,
                gfs_id=gfs_id,
                space_id=space_id,
                epoch=int(epoch),
                ring_class="durable",
                event_type=MEDIA_UPLOAD_ROW,
                author_user_id=author_user_id,
                payload=payload,
                next_attempt_at=_iso(now),
                created_at=_iso(now),
            )
            for payload, gfs_ids in parts
            for gfs_id in dict.fromkeys(gfs_ids)
        ]
        if not rows:
            return True
        if not await self._repo.add(rows):
            log.warning(
                "space items: the file lane of space %s is full — a file was"
                " not uploaded",
                space_id,
            )
            await self.fail(
                space_id=space_id,
                event_type=MEDIA_UPLOAD_ROW,
                author_user_id=author_user_id,
                error=UNSENT_QUEUE_FULL,
            )
            return False
        self._space_changed(space_id)
        self._wake_lane(True)
        return True

    def _wake_lane(self, media: bool) -> None:
        (self._media_wake if media else self._wake).set()

    async def fail(
        self, *, space_id: str, event_type: str, author_user_id: str, error: str
    ) -> None:
        """Record an item that could not be stored at all and tell its
        author."""
        await self._repo.record_failure(
            space_id=space_id,
            event_type=event_type,
            author_user_id=author_user_id,
            error=error,
            now=self._now(),
        )
        await self._tell(space_id, author_user_id)

    # ── The author's view ────────────────────────────────────────────────

    @staticmethod
    def _authors(user_id: str, admin: bool) -> list[str]:
        """Whose failed items *user_id* sees: their own, and — for a
        household admin — the ones that name nobody (an automation's, a
        task delete's): no item is lost unseen."""
        return [user_id, NO_AUTHOR] if admin and user_id else [user_id]

    async def unsent(
        self, space_id: str, user_id: str, *, admin: bool = False
    ) -> list[UnsentItem]:
        out: list[UnsentItem] = []
        for author in self._authors(user_id, admin):
            out.extend(await self._repo.list_failed(space_id, author))
        return out

    async def retry(self, space_id: str, user_id: str, *, admin: bool = False) -> int:
        """Retry the caller's failed items that kept their bytes, now."""
        n = 0
        for author in self._authors(user_id, admin):
            n += await self._repo.retry_failed(space_id, author, now=self._now())
        if n:
            self._space_changed(space_id)
            self._wake.set()
            self._media_wake.set()
        await self._tell(space_id, user_id)
        return n

    async def discard(self, space_id: str, user_id: str, *, admin: bool = False) -> int:
        n = 0
        for author in self._authors(user_id, admin):
            n += await self._repo.discard_failed(space_id, author)
        await self._tell(space_id, user_id)
        return n

    async def waiting(self, space_id: str) -> int:
        """Items of the space still waiting for a GFS."""
        return await self._repo.count_pending(space_id)

    # ── Sending ──────────────────────────────────────────────────────────

    async def expedite(self, gfs_id: str) -> None:
        """*gfs_id* (re)connected: what waits for it goes now."""
        await self._repo.expedite(gfs_id, now=self._now())
        self._wake.set()
        self._media_wake.set()

    async def run_once(self, *, media: bool = False) -> int:
        """One pass over one lane's due rows (the items, or *media*: the
        file uploads); returns how many were due. Rows of one (GFS, space)
        go in order; the pairs in parallel, so one space's failing head
        never holds another space back on the same GFS."""
        if self._sender is None or self._planner is None:
            return 0
        if not media:
            await self._expire_failed()
        rows = await self._repo.list_due(
            self._now(), limit=MEDIA_BATCH if media else BATCH, media=media
        )
        groups: dict[tuple[str, str], list[QueuedSpaceItem]] = {}
        for row in rows:
            groups.setdefault((row.gfs_id, row.space_id), []).append(row)
        await asyncio.gather(*(self._run_group(batch) for batch in groups.values()))
        return len(rows)

    async def _expire_failed(self) -> None:
        now = self._clock()
        if self._swept_at is not None and now - self._swept_at < FAILED_SWEEP_INTERVAL:
            return
        self._swept_at = now
        n = await self._repo.expire_failed(_iso(now - FAILED_RETENTION))
        if n:
            log.info("space items: %d unsent item(s) nobody retried expired", n)

    async def _run_group(self, rows: list[QueuedSpaceItem]) -> None:
        for row in rows:
            try:
                if not await self._attempt(row):
                    return  # the GFS answers the rest the same: they wait
            except Exception:
                log.warning(
                    "space items: an attempt for space %s failed",
                    row.space_id,
                    exc_info=True,
                )
                await self._backoff(row, None)
                return

    async def _attempt(self, row: QueuedSpaceItem) -> bool:
        """Try *row*; ``False`` when its GFS should not be tried again in
        this pass."""
        assert self._sender is not None and self._planner is not None
        if row.gfs_id == UNPLANNED_GFS:
            planned = await self._planner(row.space_id)
            if planned is None:
                log.warning(
                    "space items: space %s no longer has a channel here — a"
                    " waiting %s was not sent; its author is told",
                    row.space_id,
                    row.event_type,
                )
                await self._fail(row, UNSENT_REFUSED)
            elif planned:
                await self._repo.expand(row, planned, now=self._now())
                self._wake_lane(_is_media(row))
            else:
                await self._backoff(row, None, whole_gfs=False)
            return True
        if self._too_old(row) and row.ring_class == RING_CLASS_EPHEMERAL:
            # A live location pin past its life: the 15-minute keepalive
            # sends a fresh one — nothing for the person to act on.
            log.info(
                "space items: a stale %s of space %s dropped (a fresh one follows)",
                row.event_type,
                row.space_id,
            )
            await self._repo.delete(row.item_id, row.gfs_id)
            self._space_changed(row.space_id)
            return True
        if self._too_old(row):
            log.warning(
                "space items: a %s of space %s waited too long to be sent —"
                " receivers would drop it; its author is told",
                row.event_type,
                row.space_id,
            )
            # A file's locator stays (Retry seals it again); an item's
            # stamp is past what receivers take.
            await self._fail(row, UNSENT_TOO_OLD, drop_payload=not _is_media(row))
            return True
        sender = self._media_sender if _is_media(row) else self._sender
        if sender is None:
            await self._backoff(row, None, whole_gfs=False)
            return False
        outcome = await sender(row)
        if outcome.kind == "delivered":
            await self._repo.delivered(row.item_id, row.gfs_id)
            self._space_changed(row.space_id)
            return True
        if outcome.kind == "gone":
            await self._gone(row)
            return True
        if outcome.kind == "stale":
            log.warning(
                "space items: a %s of space %s was sealed at an epoch we can no"
                " longer publish at — not sent; its author is told",
                row.event_type,
                row.space_id,
            )
            await self._fail(row, UNSENT_SEAL_FAILED, drop_payload=True)
            return True
        if outcome.kind == "transient":
            # Holds that GFS's other rows of the same lane (an upload's
            # busy answer never parks the items, nor the reverse).
            await self._backoff(row, outcome.retry_after_s)
            return False
        log.warning(
            "space items: GFS %s refused a %s of space %s for good — the author"
            " is told",
            row.gfs_id,
            row.event_type,
            row.space_id,
        )
        await self._fail(row, UNSENT_REFUSED)
        return True

    def _too_old(self, row: QueuedSpaceItem) -> bool:
        """Whether *row* waited past what receivers still take."""
        try:
            created = datetime.fromisoformat(row.created_at)
        except ValueError:
            return False
        limit = (
            MAX_QUEUED_AGE_EPHEMERAL
            if row.ring_class == RING_CLASS_EPHEMERAL
            else MAX_QUEUED_AGE
        )
        return self._clock() - created > limit

    async def _fail(
        self, row: QueuedSpaceItem, error: str, *, drop_payload: bool = False
    ) -> None:
        await self._repo.mark_failed(
            row, error, drop_payload=drop_payload, now=self._now()
        )
        self._space_changed(row.space_id)
        await self._tell(row.space_id, row.author_user_id)

    async def _gone(self, row: QueuedSpaceItem) -> None:
        """*row*'s GFS no longer serves the space. Another row of the item
        still goes elsewhere, or the item went out already: this copy just
        drops. Its LAST row is planned afresh (the owner's list may name
        another server by then) — a few times, then its author is told:
        never a change that silently reaches nobody. Decided in one repo
        transaction, so two GFSs going away together can't both drop it."""
        outcome = await self._repo.gone(
            row.item_id,
            row.gfs_id,
            max_replans=MAX_REPLANS,
            next_attempt_at=_iso(
                self._clock() + timedelta(seconds=backoff_s(row.replans + 1))
            ),
        )
        if outcome == GONE_DROPPED:
            log.info(
                "space items: GFS %s no longer serves space %s — its copy of a"
                " %s is dropped",
                row.gfs_id,
                row.space_id,
                row.event_type,
            )
            self._space_changed(row.space_id)
        elif outcome == GONE_EXHAUSTED:
            log.warning(
                "space items: no GFS of space %s takes a %s any more — its"
                " author is told",
                row.space_id,
                row.event_type,
            )
            await self._fail(row, UNSENT_REFUSED)

    async def _backoff(
        self,
        row: QueuedSpaceItem,
        retry_after_s: float | None,
        *,
        whole_gfs: bool = True,
    ) -> None:
        attempts = row.attempts + 1
        until = _iso(
            self._clock() + timedelta(seconds=backoff_s(attempts, retry_after_s))
        )
        await self._repo.reschedule(
            row.item_id, row.gfs_id, attempts=attempts, next_attempt_at=until
        )
        if whole_gfs:
            await self._repo.defer_gfs(
                row.gfs_id, row.space_id, until=until, media=_is_media(row)
            )

    # ── Notifications ────────────────────────────────────────────────────

    def _space_changed(self, space_id: str) -> None:
        if self._changed is None:
            return
        try:
            self._changed(space_id)
        except Exception:
            log.debug("space items: status refresh failed", exc_info=True)

    async def _tell(self, space_id: str, author_user_id: str) -> None:
        """Tell the author; an item naming nobody goes to the household's
        admins (the notifier maps :data:`NO_AUTHOR` to them)."""
        if not author_user_id:
            log.warning(
                "space items: an unsent item of space %s names no author — the"
                " household's admins are shown it",
                space_id,
            )
        if self._notify is None:
            return
        try:
            await self._notify(space_id, author_user_id)
        except Exception:
            log.debug("space items: unsent notice failed", exc_info=True)

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="space-item-outbox")
        self._media_task = asyncio.create_task(
            self._loop(media=True), name="space-item-outbox-files"
        )

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._media_wake.set()
        for task in (self._task, self._media_task):
            if task is None:
                continue
            try:
                await asyncio.wait_for(task, timeout=10.0)
            except TimeoutError, asyncio.CancelledError:
                task.cancel()
        self._task = None
        self._media_task = None

    async def _loop(self, *, media: bool = False) -> None:
        wake = self._media_wake if media else self._wake
        while not self._stop.is_set():
            wake.clear()
            due = 0
            try:
                due = await self.run_once(media=media)
            except Exception:
                log.warning("space items: a queue pass failed", exc_info=True)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5.0)
                except TimeoutError:
                    pass
                continue
            if due >= (MEDIA_BATCH if media else BATCH) or self._stop.is_set():
                continue
            await self._sleep(media=media)

    async def _sleep(self, *, media: bool = False) -> None:
        wait = IDLE_WAKE_S
        wake = self._media_wake if media else self._wake
        try:
            nxt = await self._repo.next_due_at(media=media)
        except Exception:
            log.debug("space items: next due time unreadable", exc_info=True)
            nxt = None
        if nxt is not None:
            try:
                ahead = (datetime.fromisoformat(nxt) - self._clock()).total_seconds()
                wait = min(wait, max(0.0, ahead))
            except ValueError:
                pass
        if wait <= 0.0:
            return
        try:
            await asyncio.wait_for(wake.wait(), timeout=wait)
        except TimeoutError:
            pass
