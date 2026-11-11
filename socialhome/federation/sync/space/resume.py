"""``SPACE_SYNC_RESUME`` — long-offline catch-up (spec §4.4 / §11452).

When an instance reconnects after the 7-day outbox window, the
provider's queued events for it have expired. Spec §4.4.1 calls for
the receiver to ask each peer for the missed events via
``SPACE_SYNC_RESUME {space_id, since}``. The provider responds with a
**burst of individual federation events** — not a chunked sync.
Receivers dedup against existing rows by primary key, so re-deliveries
are harmless.

Resource types replayed today:

* ``SPACE_POST_CREATED``         — posts in the space.
* ``SPACE_POLL_CREATED``         — the reply poll of each replayed post
  (question, options with their ids), right after its post. The receiver
  creates it once, so a poll it holds is left alone; votes and closure
  heal on the host's §25.6 stream (v_92).
* ``SPACE_COMMENT_CREATED``      — comments on those posts (joined
  via ``space_post_comments.post_id`` → ``space_posts.space_id``).
* ``SPACE_TASK_LIST_DELETED``    — the space's task lists deleted since
  ``since`` (their tombstones, migration 0069), so a household that
  missed a delete drops the list and its tasks.
* ``SPACE_TASK_LIST_CREATED``    — the space's task lists created *or
  renamed* since ``since`` (v_40), sent before the tasks filed under them.
  A held list's create applies as a rename, so a missed rename heals; a
  list unchanged since ``since`` is not re-sent, so a co-member that
  missed a rename cannot revert it with its old name.
* ``SPACE_TASK_DELETED``         — the space's tasks deleted since
  ``since`` (their tombstones, migration 0071), with the deleter as
  ``actor_user_id``, sent after the lists and before the live tasks.
* ``SPACE_TASK_CREATED``         — live task rows (tombstones excluded).
* ``SPACE_PAGE_DELETED``         — the space's pages deleted since
  ``since`` (their tombstones, migration 0073), with the deleter as
  ``actor_user_id``, sent before the live pages — to the host too.
* ``SPACE_PAGE_CREATED``         — live wiki-style pages (tombstones
  excluded).
* ``SPACE_STICKY_CREATED``       — corkboard notes.
* ``SPACE_CALENDAR_EVENT_CREATED`` — calendar events (RRULEs included).
* ``SPACE_GALLERY_ITEM_CREATED`` — gallery items, joined via
  ``gallery_items.album_id`` → ``gallery_albums.space_id``. Each
  replayed item's album goes out first as ``SPACE_GALLERY_ALBUM_CREATED``
  (v_33, once per album, never the system album): the receiver files an
  item only into an album it already holds, and an album created while it
  was offline is exactly the one it lacks.

The replay payload for every type matches what its corresponding
``federation_inbound_*`` handler reads, so the receiver applies a
re-emitted event with no special-case logic and dedups by primary key.
"""

from __future__ import annotations

import logging
import pathlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

import aiofiles.os

from ....domain.federation import FederationEventType
from ....domain.federation_capabilities import FederationCapability
from ....domain.link_preview import link_preview_to_dict
from ....domain.page import page_tombstone_to_wire_dict
from ....domain.page_version import version_hash
from ....domain.post import PostType, poll_to_wire_dict
from ....domain.space_media_ref import page_media_urls, post_media_urls
from ....domain.task import (
    task_list_to_wire_dict,
    task_list_tombstone_to_wire_dict,
    task_to_wire_dict,
    task_tombstone_to_wire_dict,
)
from ....services.inbound_media_store import media_basename, media_file_path
from ....services.page_conflict_service import side_to_wire
from ....services.space_media_names import space_own_files
from .file_gate import SpaceFileBudget

if TYPE_CHECKING:
    from ....domain.calendar import CalendarEvent
    from ....domain.federation import FederationEvent
    from ....domain.gallery import GalleryAlbum, GalleryItem
    from ....domain.page import Page
    from ....domain.post import Comment, Post
    from ....domain.sticky import Sticky
    from ....repositories.calendar_repo import AbstractSpaceCalendarRepo
    from ....repositories.gallery_repo import AbstractGalleryRepo
    from ....repositories.media_reference_repo import AbstractMediaReferenceRepo
    from ....repositories.page_repo import AbstractPageRepo
    from ....repositories.poll_repo import AbstractPollRepo
    from ....repositories.space_post_repo import AbstractSpacePostRepo
    from ....repositories.space_repo import AbstractSpaceRepo
    from ....repositories.sticky_repo import AbstractStickyRepo
    from ....repositories.task_repo import AbstractSpaceTaskRepo
    from ...federation_service import FederationService


log = logging.getLogger(__name__)


class _MediaOutbox(Protocol):
    async def enqueue_for_blob(
        self,
        *,
        space_id: str,
        correlation_id: str,
        target_instance_ids: list[str],
        media_urls: list[str],
    ) -> None: ...


#: A space holds at most ``gallery_service.ALBUMS_PER_SPACE`` albums; the
#: repo caps a listing at the same 200.
MAX_ALBUMS_REPLAYED: int = 200

#: Hard cap on rows replayed per resource type per single
#: ``SPACE_SYNC_RESUME``. Receivers that need older events re-issue the
#: request with the new high-water mark. Matches the DM-history
#: equivalent so a household with many spaces doesn't burst-pin a
#: small HA instance.
MAX_PER_RESOURCE: int = 500

#: The replayed rows' files (posts, covers, gallery items newer than
#: ``since``, in the order replayed) are charged to the ONE (requester,
#: space) budget ``SPACE_MEDIA_WANTED`` shares (:class:`.file_gate
#: .SpaceFileBudget`: 50 files / 32 MiB per 30 minutes). A file that would
#: pass it is skipped (it reaches the household through its author's push,
#: the §25.6 catch-up, or on view).
#: One ``SPACE_SYNC_RESUME`` per (household, space) per this many seconds;
#: a sooner one is dropped (the replay is idempotent — the first one already
#: sent what was missed).
RESUME_COOLDOWN_S: float = 30 * 60.0
#: How many (household, space) cooldowns are remembered (oldest dropped).
RESUME_COOLDOWN_TRACKED: int = 4096
#: A replay reaches back at most this far, whatever ``since`` asks — the
#: catch-up is bounded on every path (older content arrives on view).
RESUME_MAX_AGE_S: int = 30 * 86_400

#: The replayed rows' files: ``(row id, file urls)``.
_Media = list[tuple[str, list[str]]]


def _owner_of(row: Any) -> str | None:
    """The person a replayed row belongs to (a post's / comment's / sticky's
    author, an event's, task's or page's creator, a gallery item's uploader,
    an album's owner)."""
    for field in ("author", "created_by", "uploaded_by", "owner_user_id"):
        value = getattr(row, field, None)
        if value:
            return str(value)
    return None


def _deleter_of(tombstone: Any) -> str | None:
    """Whose delete a tombstone replays: its deleter (the replay's
    ``actor_user_id``, which a receiver binds to the sending household), or
    — none recorded — the row's creator."""
    return str(getattr(tombstone, "deleted_by", "") or "") or _owner_of(tombstone)


def _clamped_since(since: str) -> str:
    """``since``, or :data:`RESUME_MAX_AGE_S` ago when it reaches further
    back — a replay is bounded on every path. An unreadable value is kept
    (``handle_request`` validated it already)."""
    try:
        asked = datetime.fromisoformat(since)
    except ValueError:
        return since
    if asked.tzinfo is None:
        asked = asked.replace(tzinfo=UTC)
    floor = datetime.now(UTC) - timedelta(seconds=RESUME_MAX_AGE_S)
    return floor.isoformat() if asked < floor else since


class SpaceSyncResumeProvider:
    """Receiver- and provider-side helper for ``SPACE_SYNC_RESUME``.

    Construct once per app and register :meth:`handle_request` for
    :data:`FederationEventType.SPACE_SYNC_RESUME`. The receiver-side
    sender is :meth:`send_request` — typically called by a reconnect
    scheduler when the federation link to a peer comes back up after
    the outbox-retention window.
    """

    __slots__ = (
        "_budget",
        "_clock",
        "_federation",
        "_gallery_repo",
        "_last_resume",
        "_media_dir",
        "_media_refs",
        "_media_sync",
        "_member_gate",
        "_own_members",
        "_page_repo",
        "_roster_authority",
        "_space_calendar_repo",
        "_space_poll_repo",
        "_space_post_repo",
        "_space_repo",
        "_space_task_repo",
        "_sticky_repo",
    )

    def __init__(
        self,
        *,
        federation_service: FederationService,
        space_repo: AbstractSpaceRepo,
        space_post_repo: AbstractSpacePostRepo,
        space_task_repo: AbstractSpaceTaskRepo | None = None,
        page_repo: AbstractPageRepo | None = None,
        sticky_repo: AbstractStickyRepo | None = None,
        space_calendar_repo: AbstractSpaceCalendarRepo | None = None,
        gallery_repo: AbstractGalleryRepo | None = None,
        space_poll_repo: AbstractPollRepo | None = None,
        media_sync: _MediaOutbox | None = None,
        media_dir: pathlib.Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        media_refs: AbstractMediaReferenceRepo | None = None,
        budget: SpaceFileBudget | None = None,
    ) -> None:
        self._federation = federation_service
        #: The replayed posts' reply polls. ``None``: posts only.
        self._space_poll_repo = space_poll_repo
        #: The (requester, space) file budget shared with SPACE_MEDIA_WANTED.
        self._budget = budget if budget is not None else SpaceFileBudget()
        #: v_82: only a page's files that are the space's own ship.
        self._media_refs = media_refs
        #: Where the replayed files live — their sizes bound a request.
        self._media_dir = media_dir
        self._clock = clock
        #: ``(household, space) -> when its last resume was served``.
        self._last_resume: OrderedDict[tuple[str, str], float] = OrderedDict()
        #: Ships the files of the replayed posts / covers / gallery items:
        #: the receiver names a file by the household that delivered its
        #: row, so a replayed row's pictures come from us, not its author.
        self._media_sync = media_sync
        self._space_repo = space_repo
        self._space_post_repo = space_post_repo
        self._space_task_repo = space_task_repo
        self._page_repo = page_repo
        self._sticky_repo = sticky_repo
        self._space_calendar_repo = space_calendar_repo
        self._gallery_repo = gallery_repo
        #: v_92 — whose rows we replay, and to whom (:meth:`attach_row_scope`;
        #: ``None``: every row, to every member household).
        self._roster_authority: Callable[[str], Awaitable[bool]] | None = None
        self._own_members: Callable[[str], Awaitable[set[str]]] | None = None
        self._member_gate: Callable[[str, str], Awaitable[bool]] | None = None

    def attach_row_scope(
        self,
        roster_authority: Callable[[str], Awaitable[bool]],
        *,
        own_members: Callable[[str], Awaitable[set[str]]],
        member_gate: Callable[[str, str], Awaitable[bool]],
    ) -> None:
        """Wire whose rows a replay carries, and to whom (v_92): the roster
        authority (``roster_authority(space_id)``) replays every row to any
        household of the space (a follower gets its subset there); any other
        household — a trusted helper asked while the host is offline —
        replays only its own people's rows (``own_members(space_id)``) and
        their files, and only to a household holding a writer seat
        (``member_gate(space_id, household)``,
        ``SpaceWriterHouseholds.may_receive``)."""
        self._roster_authority = roster_authority
        self._own_members = own_members
        self._member_gate = member_gate

    # ── Outbound (requester side) ─────────────────────────────────────

    async def send_request(
        self,
        *,
        space_id: str,
        instance_id: str,
        since: str,
    ) -> bool:
        """Ask ``instance_id`` to replay missed events since ``since``;
        whether the request was handed to a path that reached it (``False``
        for empty fields or a failed send).

        ``since`` is an ISO-8601 timestamp (the provider clamps it to
        :data:`RESUME_MAX_AGE_S`). Returns immediately; responses arrive as
        individual ``SPACE_*_CREATED`` events handled by
        ``federation_inbound_service``. Sent direct to a paired household,
        over the mesh or to a GFS mailbox seat otherwise (space-scoped,
        ``send_with_mesh_fallback``) — the trusted helper the scheduler
        asks while the host is offline may be reached either way (v_92).
        """
        if not space_id or not instance_id or not since:
            return False
        payload: dict[str, str] = {"space_id": space_id, "since": since}
        result = await self._federation.send_with_mesh_fallback(
            to_instance_id=instance_id,
            event_type=FederationEventType.SPACE_SYNC_RESUME,
            payload=payload,
            space_id=space_id,
        )
        if not result.ok:
            log.info(
                "SPACE_SYNC_RESUME for %s to %s not delivered: %s",
                space_id,
                instance_id,
                result.error,
            )
        return bool(result.ok)

    # ── Inbound (provider side) ───────────────────────────────────────

    async def handle_request(self, event: FederationEvent) -> int:
        """Replay missed events for one (space, peer) pair.

        Returns the total number of events sent across every resource
        type (0 if the peer isn't a member, the space is unknown, or
        there's nothing newer than ``since``). Membership is gated by
        ``list_member_instances`` — a peer that isn't in the space gets
        silently dropped, matching the §S-1 sync-begin guard.
        """
        payload = event.payload or {}
        space_id = str(
            event.space_id or payload.get("space_id") or "",
        )
        since = str(payload.get("since") or "")
        if not space_id or not since:
            return 0
        # Validate ISO-8601 — reject malformed input rather than letting
        # the SQL ``> ?`` comparison silently match nothing.
        try:
            datetime.fromisoformat(since)
        except ValueError:
            log.debug(
                "SPACE_SYNC_RESUME from %s: bad 'since' %r — dropping",
                event.from_instance,
                since,
            )
            return 0
        return await self.replay_space_to(
            space_id=space_id,
            instance_id=event.from_instance,
            since=since,
        )

    def _claim_window(self, instance_id: str, space_id: str) -> bool:
        """Start the (household, space) replay window, or ``False`` (logged)
        when one is still open. Synchronous — taken before the replay awaits
        anything, so concurrent requests can't all pass. Shared by every
        entry point (``SPACE_SYNC_RESUME`` and the §319.6 resync scopes), so
        a household can't route around it."""
        key = (str(instance_id or ""), space_id)
        now = self._clock()
        last = self._last_resume.get(key)
        if last is not None and now - last < RESUME_COOLDOWN_S:
            log.info(
                "space replay to %s for %s: served %.0f s ago — dropped",
                instance_id,
                space_id,
                now - last,
            )
            return False
        self._last_resume[key] = now
        self._last_resume.move_to_end(key)
        while len(self._last_resume) > RESUME_COOLDOWN_TRACKED:
            self._last_resume.popitem(last=False)
        return True

    async def _is_member(self, space_id: str, instance_id: str) -> bool:
        """Membership gate — the §S-1 security boundary.

        A peer that isn't a member of ``space_id`` must never receive any
        of the space's content (posts, comments, calendar…). Both the
        ``SPACE_SYNC_RESUME`` path (:meth:`handle_request`) and the
        §319.6 resync paths (:meth:`replay_space_to` /
        :meth:`replay_calendar_to`) run this check before any
        ``_replay_*`` call so a resync request can't bypass it. A household
        that is not the roster authority (a v_92 helper) serves only writer
        seats — the §25.6 provider's gate for member-only data.
        """
        if (
            self._member_gate is not None
            and self._roster_authority is not None
            and not await self._roster_authority(space_id)
        ):
            return await self._member_gate(space_id, instance_id)
        peers = await self._space_repo.list_member_instances(space_id)
        return instance_id in peers

    async def replay_space_to(
        self,
        *,
        space_id: str,
        instance_id: str,
        since: str,
    ) -> int:
        """Replay every space resource newer than ``since`` to one peer.

        Membership-gated: a non-member receives nothing (returns 0). This
        is the entry point for both ``SPACE_SYNC_RESUME`` and the §319.6
        ``space:<id>`` resync scope — the gate runs *before* any
        ``_replay_*`` call so neither path can leak space content to a
        non-member household.
        """
        if not await self._is_member(space_id, instance_id):
            return 0
        if not self._claim_window(instance_id, space_id):
            return 0
        since = _clamped_since(since)
        own = await self._own_rows_only(space_id)
        to = instance_id
        media: _Media = []
        sent = 0
        sent += await self._replay_posts(space_id, since, to=to, own=own, media=media)
        sent += await self._replay_comments(
            space_id, since, to=to, own=own, media=media
        )
        # Lists before tasks: a task is only filed under a list held here.
        sent += await self._replay_task_list_deletes(space_id, since, to=to, own=own)
        sent += await self._replay_task_lists(space_id, since, to=to, own=own)
        sent += await self._replay_task_deletes(space_id, since, to=to, own=own)
        sent += await self._replay_tasks(space_id, since, to=to, own=own)
        sent += await self._replay_page_deletes(space_id, since, to=to, own=own)
        sent += await self._replay_pages(space_id, since, to=to, own=own, media=media)
        sent += await self._replay_stickies(space_id, since, to=to, own=own)
        sent += await self._replay_calendar(
            space_id, since, to=to, own=own, media=media
        )
        sent += await self._replay_gallery_items(
            space_id, since, to=to, own=own, media=media
        )
        await self._ship_media(space_id, instance_id, media)
        return sent

    async def _own_rows_only(self, space_id: str) -> frozenset[str] | None:
        """The people whose rows we replay: everyone (``None``) as the
        space's roster authority (the host, or a seed-holding delegated
        admin), else only our own people — every receiver takes other
        people's rows only from the household acting as host
        (``receiver._acts_as_host``), so sending them is wasted bytes. Not
        wired (``None``): every row, as before."""
        if self._roster_authority is None or self._own_members is None:
            return None
        if await self._roster_authority(space_id):
            return None
        return frozenset(await self._own_members(space_id))

    async def replay_calendar_to(
        self,
        *,
        space_id: str,
        instance_id: str,
        since: str,
    ) -> int:
        """Replay only the space's calendar events newer than ``since``.

        Entry point for the §319.6 ``calendar:<id>`` resync scope.
        Membership-gated identically to :meth:`replay_space_to` — a
        non-member receives nothing (returns 0).
        """
        if not await self._is_member(space_id, instance_id):
            return 0
        if not self._claim_window(instance_id, space_id):
            return 0
        media: _Media = []
        sent = await self._replay_calendar(
            space_id,
            _clamped_since(since),
            to=instance_id,
            own=await self._own_rows_only(space_id),
            media=media,
        )
        await self._ship_media(space_id, instance_id, media)
        return sent

    async def _ship_media(
        self,
        space_id: str,
        to: str,
        media: _Media,
    ) -> None:
        """Queue the replayed rows' files for ``to``: a receiver binds a
        file to the household that delivered its row, so the bytes must
        come from us. Charged to the shared (requester, space) budget; a
        file over it is skipped."""
        if self._media_sync is None or not media:
            return
        skipped = 0
        for correlation_id, urls in media:
            kept: list[str] = []
            for url in urls:
                size = await self._size(url)
                if size is None or not self._budget.take(to, space_id, size):
                    skipped += 1
                    continue
                kept.append(url)
            if kept:
                await self._media_sync.enqueue_for_blob(
                    space_id=space_id,
                    correlation_id=correlation_id,
                    target_instance_ids=[to],
                    media_urls=kept,
                )
        if skipped:
            log.info(
                "SPACE_SYNC_RESUME to %s for %s: %d file(s) over the per-request "
                "budget or missing — not pushed",
                to,
                space_id,
                skipped,
            )

    async def _size(self, url: str) -> int | None:
        """The local file's size; ``None`` when it is not here. Without a
        media dir (a test stack) every file counts as empty."""
        if self._media_dir is None:
            return 0
        path = media_file_path(self._media_dir, media_basename(url))
        if path is None:
            return None
        try:
            return (await aiofiles.os.stat(path)).st_size
        except OSError:
            return None

    # ── Per-resource replay ───────────────────────────────────────────

    async def _replay_posts(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
        own: frozenset[str] | None = None,
        media: _Media | None = None,
    ) -> int:
        posts = await self._space_post_repo.list_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        sent = await self._send_each(
            posts,
            FederationEventType.SPACE_POST_CREATED,
            _post_to_payload,
            space_id=space_id,
            to=to,
            own=own,
            media_of=post_media_urls,
            media=media,
        )
        return sent + await self._replay_polls(posts, space_id=space_id, to=to, own=own)

    async def _replay_polls(
        self,
        posts: list[Post],
        *,
        space_id: str,
        to: str,
        own: frozenset[str] | None,
    ) -> int:
        """The reply poll of each replayed post, as the live
        ``SPACE_POLL_CREATED`` (one small event per poll post)."""
        repo = self._space_poll_repo
        if repo is None:
            return 0
        polls: list[tuple[Post, dict]] = []
        for post in posts:
            if post.type is not PostType.POLL:
                continue
            meta = await repo.get_meta(post.id)
            if meta is None:
                continue
            polls.append(
                (
                    post,
                    poll_to_wire_dict(
                        post_id=post.id,
                        space_id=space_id,
                        question=meta["question"],
                        options=await repo.list_options_with_counts(post.id),
                        allow_multiple=meta["allow_multiple"],
                        closes_at=meta["closes_at"],
                    ),
                )
            )
        payload_of = {post.id: payload for post, payload in polls}
        return await self._send_each(
            [post for post, _payload in polls],
            FederationEventType.SPACE_POLL_CREATED,
            lambda post: payload_of[post.id],
            space_id=space_id,
            to=to,
            own=own,
        )

    async def _replay_comments(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
        own: frozenset[str] | None = None,
        media: _Media | None = None,
    ) -> int:
        rows = await self._space_post_repo.list_comments_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        post_of = {comment.id: post_id for post_id, comment in rows}
        return await self._send_each(
            [comment for _post_id, comment in rows],
            FederationEventType.SPACE_COMMENT_CREATED,
            lambda comment: _comment_to_payload(post_of[comment.id], comment),
            space_id=space_id,
            to=to,
            own=own,
            # A comment's picture (v_92): the same fill as a post's.
            media_of=lambda comment: [comment.media_url] if comment.media_url else [],
            media=media,
        )

    async def _replay_task_list_deletes(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
        own: frozenset[str] | None = None,
    ) -> int:
        if self._space_task_repo is None:
            return 0
        tombstones = await self._space_task_repo.list_list_tombstones(
            space_id,
            since=since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            tombstones,
            FederationEventType.SPACE_TASK_LIST_DELETED,
            lambda tombstone: task_list_tombstone_to_wire_dict(tombstone, space_id),
            space_id=space_id,
            to=to,
            own=own,
            owner_of=_deleter_of,
        )

    async def _replay_task_lists(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
        own: frozenset[str] | None = None,
    ) -> int:
        if self._space_task_repo is None:
            return 0
        lists = await self._space_task_repo.list_lists_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            lists,
            FederationEventType.SPACE_TASK_LIST_CREATED,
            lambda task_list: task_list_to_wire_dict(task_list, space_id),
            space_id=space_id,
            to=to,
            own=own,
        )

    async def _replay_task_deletes(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
        own: frozenset[str] | None = None,
    ) -> int:
        if self._space_task_repo is None:
            return 0
        tombstones = await self._space_task_repo.list_task_tombstones(
            space_id,
            since=since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            tombstones,
            FederationEventType.SPACE_TASK_DELETED,
            lambda tombstone: task_tombstone_to_wire_dict(tombstone, space_id),
            space_id=space_id,
            to=to,
            own=own,
            owner_of=_deleter_of,
        )

    async def _replay_tasks(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
        own: frozenset[str] | None = None,
    ) -> int:
        if self._space_task_repo is None:
            return 0
        tasks = await self._space_task_repo.list_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            tasks,
            FederationEventType.SPACE_TASK_CREATED,
            lambda task: task_to_wire_dict(task, space_id),
            space_id=space_id,
            to=to,
            own=own,
        )

    async def _replay_page_deletes(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
        own: frozenset[str] | None = None,
    ) -> int:
        if self._page_repo is None:
            return 0
        tombstones = await self._page_repo.list_page_tombstones(
            space_id,
            since=since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            tombstones,
            FederationEventType.SPACE_PAGE_DELETED,
            lambda tombstone: page_tombstone_to_wire_dict(tombstone, space_id),
            space_id=space_id,
            to=to,
            own=own,
            owner_of=_deleter_of,
        )

    async def _replay_pages(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
        own: frozenset[str] | None = None,
        media: _Media | None = None,
    ) -> int:
        if self._page_repo is None:
            return 0
        sequenced = await self._federation.peer_supports(
            to, min_version=FederationCapability.MIN_FOR_HOST_SEQUENCED_PAGES
        )
        space = await self._space_repo.get(space_id)
        if sequenced and space is not None and space.owner_instance_id == to:
            # v_48: the host sequences the pages — a replay to it could only
            # roll it back. Our own drafts reach it as proposals (forwarder).
            return 0
        pages = await self._page_repo.list_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        # v_48: each replayed page carries the host's ``seq`` (and, from the
        # host, its version hash + conflict list), so a member mirrors it by
        # sequence; a replay from anyone but the host never updates a page
        # the receiver holds. An unacknowledged local draft is never
        # replayed — the canonical version it was made from is (or nothing,
        # for an unsequenced create). An older peer gets the plain fields.
        payloads: dict[str, dict] = {}
        replayed = []
        for page in pages:
            if page.pending_base_seq is not None:
                base = await self._page_repo.get_draft_base(page.id, space_id=space_id)
                if base is None or not base.title:
                    continue
                page = replace(
                    page,
                    title=base.title,
                    content=base.content,
                    cover_image_url=base.cover_image_url,
                    seq=base.seq,
                )
            payload = _page_to_payload(page)
            # A replay says so: a v_48 host never takes it for a proposal
            # (it could roll the host back, or bring a deleted page back).
            payload["replay"] = True
            if sequenced:
                sides = await self._page_repo.list_conflict_sides(
                    page.id, space_id=space_id
                )
                payload.update(
                    seq=page.seq,
                    version_hash=version_hash(
                        page.title, page.content, page.cover_image_url
                    ),
                    last_editor_user_id=page.last_editor_user_id or page.created_by,
                    conflict=[side_to_wire(s) for s in sides],
                )
            payloads[page.id] = payload
            replayed.append(page)
        pages = replayed
        # v_82: the host's replay ships the pages' files too — on a
        # receiver only the host fills the names a page shows.
        ships_files = (
            media is not None
            and self._media_sync is not None
            and space is not None
            and space.owner_instance_id == self._federation.own_instance_id
            and await self._federation.space_member_supports(
                to, min_version=FederationCapability.MIN_FOR_PAGE_MEDIA
            )
        )
        sent = await self._send_each(
            pages,
            FederationEventType.SPACE_PAGE_CREATED,
            lambda page: payloads[page.id],
            space_id=space_id,
            to=to,
            own=own,
        )
        if ships_files and media is not None:
            for page in pages:
                urls = await space_own_files(
                    self._media_refs,
                    space_id,
                    page_media_urls(page.cover_image_url, page.content),
                )
                if urls:
                    media.append((page.id, urls))
        return sent

    async def _replay_stickies(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
        own: frozenset[str] | None = None,
    ) -> int:
        if self._sticky_repo is None:
            return 0
        stickies = await self._sticky_repo.list_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            stickies,
            FederationEventType.SPACE_STICKY_CREATED,
            _sticky_to_payload,
            space_id=space_id,
            to=to,
            own=own,
        )

    async def _replay_calendar(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
        own: frozenset[str] | None = None,
        media: _Media | None = None,
    ) -> int:
        if self._space_calendar_repo is None:
            return 0
        events = await self._space_calendar_repo.list_events_since(
            space_id,
            since,
            limit=MAX_PER_RESOURCE,
        )
        return await self._send_each(
            events,
            FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
            _calendar_to_payload,
            space_id=space_id,
            to=to,
            own=own,
            media_of=lambda ev: [ev.cover_url] if ev.cover_url else [],
            media=media,
        )

    async def _replay_gallery_items(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
        own: frozenset[str] | None = None,
        media: _Media | None = None,
    ) -> int:
        """Replay the space's gallery: albums first, then missed items.

        The receiver files an item only into an album it already holds for
        the space, and an album made, edited or deleted while it was away is
        state it simply lacks — whether or not anything was uploaded since.
        So, in order:

        * every album delete recorded since ``since`` as
          ``SPACE_GALLERY_ALBUM_DELETED`` (the album tombstones, migration
          0085);
        * every non-system album of the space as
          ``SPACE_GALLERY_ALBUM_CREATED`` — an idempotent no-op for one the
          receiver holds;
        * those edited since ``since`` as ``SPACE_GALLERY_ALBUM_UPDATED``;
        * the items newer than ``since`` as ``SPACE_GALLERY_ITEM_CREATED``.

        The system "Posts" album is skipped: every household rebuilds its
        own from the posts.
        """
        if self._gallery_repo is None:
            return 0
        sent = 0
        sent += await self._send_each(
            [
                # The owner lets a receiver that never held the album bind
                # its owner-bound id before remembering the delete.
                {"id": t.id, "owner_user_id": t.owner}
                for t in await self._gallery_repo.list_album_tombstones_since(
                    space_id, since
                )
            ],
            FederationEventType.SPACE_GALLERY_ALBUM_DELETED,
            dict,
            space_id=space_id,
            to=to,
            # Kept whole: an album tombstone names no deleter, and a
            # settings-authority (admin) household's delete is taken
            # (``may_mutate(settings=True)``) — the replay as before.
        )
        albums = [
            a
            for a in await self._gallery_repo.list_albums(
                space_id, limit=MAX_ALBUMS_REPLAYED
            )
            if a.space_id == space_id and not a.is_system
        ]
        sent += await self._send_each(
            albums,
            FederationEventType.SPACE_GALLERY_ALBUM_CREATED,
            _gallery_album_to_payload,
            space_id=space_id,
            to=to,
            own=own,
        )
        sent += await self._send_each(
            [a for a in albums if _changed_since(a.updated_at, since)],
            FederationEventType.SPACE_GALLERY_ALBUM_UPDATED,
            _gallery_album_to_payload,
            space_id=space_id,
            to=to,
            own=own,
        )
        return sent + await self._replay_gallery_item_rows(
            space_id, since, to=to, own=own, media=media
        )

    async def _replay_gallery_item_rows(
        self,
        space_id: str,
        since: str,
        *,
        to: str,
        own: frozenset[str] | None = None,
        media: _Media | None = None,
    ) -> int:
        """The gallery items newer than ``since`` (no albums)."""
        if self._gallery_repo is None:
            return 0
        items = await self._gallery_repo.list_items_since(
            space_id, since, limit=MAX_PER_RESOURCE
        )
        return await self._send_each(
            items,
            FederationEventType.SPACE_GALLERY_ITEM_CREATED,
            _gallery_item_to_payload,
            space_id=space_id,
            to=to,
            own=own,
            media_of=lambda item: [u for u in (item.thumbnail_url, item.url) if u],
            media=media,
        )

    async def _send_each(
        self,
        rows: list,
        event_type: FederationEventType,
        to_payload,
        *,
        space_id: str,
        to: str,
        own: frozenset[str] | None = None,
        owner_of: Callable[[Any], str | None] | None = None,
        media_of: Callable[[Any], list[str]] | None = None,
        media: _Media | None = None,
    ) -> int:
        """Send each row as ``event_type``, collecting its files for
        :meth:`_ship_media`. With *own* (a household that is not the roster
        authority) only the rows whose person (*owner_of*, default
        :func:`_owner_of`) is one of ours go out, with their files only —
        other people's files come on view (``SPACE_MEDIA_WANTED``)."""
        sent = 0
        for row in rows:
            if own is not None and (owner_of or _owner_of)(row) not in own:
                continue
            if media_of is not None and media is not None:
                urls = media_of(row)
                if urls:
                    media.append((row.id, list(urls)))
            try:
                await self._federation.send_event(
                    to_instance_id=to,
                    event_type=event_type,
                    payload=to_payload(row),
                    space_id=space_id,
                )
                sent += 1
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "SPACE_SYNC_RESUME %s replay to %s failed: %s",
                    event_type,
                    to,
                    exc,
                    exc_info=True,
                )
        return sent


# ─── Payload shapers ─────────────────────────────────────────────────────


def _post_to_payload(post: Post) -> dict:
    payload: dict = {
        "id": post.id,
        "author": post.author,
        "type": post.type.value,
        "content": post.content,
        "media_url": post.media_url,
        "occurred_at": _iso(post.created_at),
    }
    # Location posts ride on the existing SPACE_POST_CREATED event —
    # no new event type. Coords are already 4dp-truncated by
    # feed_service / space_service before the post is persisted, so
    # the payload carries the canonical wire form.
    if post.location is not None:
        loc: dict = {"lat": post.location.lat, "lon": post.location.lon}
        if post.location.label is not None:
            loc["label"] = post.location.label
        payload["location"] = loc
    if post.link_preview is not None:
        payload["link_preview"] = link_preview_to_dict(post.link_preview)
    return payload


def _comment_to_payload(post_id: str, comment: Comment) -> dict:
    return {
        "post_id": post_id,
        "comment_id": comment.id,
        "author": comment.author,
        "type": comment.type.value,
        "content": comment.content,
        "media_url": comment.media_url,
        "parent_id": comment.parent_id,
        "occurred_at": _iso(comment.created_at),
    }


def _page_to_payload(page: Page) -> dict:
    return {
        "id": page.id,
        "title": page.title,
        "content": page.content,
        "created_by": page.created_by,
        "cover_image_url": page.cover_image_url,
        "created_at": page.created_at,
        "updated_at": page.updated_at,
    }


def _sticky_to_payload(sticky: Sticky) -> dict:
    return {
        "id": sticky.id,
        "author": sticky.author,
        "content": sticky.content,
        "color": sticky.color,
        "position_x": sticky.position_x,
        "position_y": sticky.position_y,
        "created_at": sticky.created_at,
        "updated_at": sticky.updated_at,
    }


def _calendar_to_payload(event: CalendarEvent) -> dict:
    return {
        "id": event.id,
        "calendar_id": event.calendar_id,
        "summary": event.summary,
        "description": event.description,
        "start": _iso(event.start),
        "end": _iso(event.end),
        "all_day": event.all_day,
        "attendees": list(event.attendees),
        "created_by": event.created_by,
        "rrule": event.rrule,
        "cover_url": event.cover_url,
    }


def _gallery_album_to_payload(album: GalleryAlbum) -> dict:
    """Same shape as the live ``GalleryFederationOutbound`` album push."""
    return album.to_federation_dict()


def _gallery_item_to_payload(item: GalleryItem) -> dict:
    """Same shape as the live ``GalleryFederationOutbound`` item push —
    ``GalleryItem.to_federation_dict`` (thumbnail projection + full ``url``)."""
    return item.to_federation_dict()


def _iso(value) -> str:
    """ISO-format helper that tolerates ``datetime`` and ``str`` inputs."""
    if value is None:
        return datetime.now(UTC).isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _changed_since(value: str | None, since: str) -> bool:
    """``value`` is later than ``since`` — or either is unreadable, where
    re-sending an unchanged album is harmless and skipping a change is not."""
    if not value:
        return False
    try:
        a = datetime.fromisoformat(value)
        b = datetime.fromisoformat(since)
    except ValueError:
        return True
    if a.tzinfo is None:
        a = a.replace(tzinfo=UTC)
    if b.tzinfo is None:
        b = b.replace(tzinfo=UTC)
    return a > b
