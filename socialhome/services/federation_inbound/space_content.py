"""Inbound federation handlers for space-scoped content (§13).

Mirrors tasks, pages, stickies, calendar events, and poll votes from
paired peers into local repos so the UI shows a coherent space view.
Post/comment events are handled elsewhere (federation_inbound_service).

Handlers are lenient: malformed payloads log + return rather than
raise, because §24.11 has already verified the signature + replay
cache, and a peer sending a malformed body shouldn't take the inbound
pipeline down.

Two guards run on every mutation, in this order:

* **Space scope** (``federation/space_scope.py``) — the row must live in
  the space the envelope was gated for; every repo mutator re-checks it
  in SQL.
* **Authorship** (``federation/space_authorship.py``) — the users the
  payload names (author, creator, voter, seller, bidder …) must be members
  seated on the household that signed the envelope. Per family: creates
  bind the claimed author; gallery albums and items change only from the
  owner's / uploader's household or a moderator; tasks / pages / stickies /
  calendar events
  are collaborative (any writer household, attribution kept);
  votes, RSVPs, schedule answers and bids are the voter's own; closing a
  poll, finalising a schedule and settling a listing are the owner's
  alone; zones are moderator-only, and timetables are moderator-only *per
  user* (the named editor is an admin seated on the sender).

A third guard runs on the collaborative families after authorship: the
space's per-feature **access level** (§4.3, v_42) as this household holds
it — an ``ADMIN_ONLY`` feature takes writes only from an admin, the named
``actor_user_id`` bound to the sender (:meth:`SpaceAuthorship.access_admits`).

A refusal is a WARNING; a benign no-op (a replayed delete, a status change
for a listing already settled) is DEBUG — see :func:`log_not_applied`.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ...domain.calendar import CalendarEvent, CalendarRSVP, RSVPStatus
from ...domain.events import (
    CalendarEventCreated,
    CalendarEventDeleted,
    GalleryAlbumCreated,
    GalleryAlbumDeleted,
    GalleryAlbumUpdated,
    GalleryItemDeleted,
    GalleryItemUploaded,
    PageDeleted,
    TaskCreated,
    TaskDeleted,
    TaskListCreated,
    TaskListDeleted,
    TaskListUpdated,
    TaskUpdated,
    TimetableDeleted,
    TimetableSaved,
)
from ...domain.federation import FederationEventType
from ...federation.owner_bound_id import (
    GALLERY_ALBUM_KIND,
    GALLERY_ITEM_KIND,
    SPACE_CALENDAR_EVENT_KIND,
    SPACE_PAGE_KIND,
    SPACE_STICKY_KIND,
    SPACE_TASK_KIND,
    SPACE_TASK_LIST_KIND,
    SPACE_TIMETABLE_KIND,
    OwnerBinding,
    check_owner_bound_id,
    is_owner_bound,
    owner_bound_id_refused,
)
from ...federation.space_authorship import payload_actor
from ...federation.space_scope import (
    log_cross_space_refusal,
    log_not_applied,
    resolve_space_id,
)
from ...domain.gallery import GalleryAlbum, GalleryItem
from ...domain.page import MAX_PAGE_TITLE_LENGTH, Page
from ...domain.page_version import version_hash
from ...domain.post import (
    BAZAAR_MAX_IMAGES,
    BazaarBid,
    BazaarListing,
    BazaarMode,
    BazaarStatus,
    Post,
)
from ...domain.space import (
    MODERATION_BLOCK_KEY,
    ContentAction,
    SpaceZone,
    validate_zone_color,
    validate_zone_coord,
    validate_zone_name,
    validate_zone_radius,
)
from ...domain.sticky import MAX_STICKY_CONTENT_LENGTH, Sticky, coerce_peer_sticky
from ...domain.task import Task, TaskList, task_from_wire_dict, task_list_from_wire_dict
from ...domain.timetable import (
    Timetable,
    TimetableValidationError,
    from_wire_dict,
    remote_version_refusal,
    validate,
)
from ...domain.timetable import parse_datetime as parse_timetable_datetime
from ...domain.user import SYSTEM_AUTHOR
from ...infrastructure.event_bus import EventBus
from ...media.cleanup import unlink_unreferenced
from ...utils.datetime import parse_iso8601_optional
from ...utils.timezones import coerce_tz
from ..gallery_service import ALBUMS_PER_SPACE, DESCRIPTION_MAX, NAME_MAX
from ..inbound_media_store import (
    local_media_ref,
    local_media_refs,
    verbatim_local_media_ref,
)
from ..page_conflict_service import (
    PageMode,
    canonical_from_wire,
    proposal_from_wire,
)

if TYPE_CHECKING:
    from ...domain.federation import FederationEvent
    from ...federation.federation_service import FederationService
    from ...federation.space_authorship import SpaceAuthorship
    from ...repositories.bazaar_repo import AbstractBazaarRepo
    from ...repositories.calendar_repo import AbstractSpaceCalendarRepo
    import pathlib

    from ...repositories.gallery_repo import AbstractGalleryRepo
    from ...repositories.media_reference_repo import AbstractMediaReferenceRepo
    from ...repositories.page_repo import AbstractPageRepo
    from ..page_conflict_service import PageConflictService
    from ...repositories.space_poll_repo import AbstractSpacePollRepo
    from ...repositories.space_post_repo import AbstractSpacePostRepo
    from ...repositories.space_zone_repo import AbstractSpaceZoneRepo
    from ...repositories.sticky_repo import AbstractStickyRepo
    from ...repositories.task_repo import AbstractSpaceTaskRepo
    from ...repositories.timetable_repo import AbstractSpaceTimetableRepo

log = logging.getLogger(__name__)


def _has_ended(end_time: str | None) -> bool:
    """``True`` when a listing's ``end_time`` is in the past."""
    end = parse_iso8601_optional(end_time)
    if end is None:
        return False
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end <= datetime.now(timezone.utc)


def _payload_hash(p: dict) -> str:
    """The version hash a (possibly malformed) proposal stands for."""
    cover = p.get("cover_image_url")
    return version_hash(
        str(p.get("title") or ""),
        str(p.get("content") or ""),
        cover if isinstance(cover, str) else None,
    )


def _page_from_payload(p: dict, page_id: str, space_id: str) -> Page:
    cover = p.get("cover_image_url")
    return Page(
        id=page_id,
        title=str(p.get("title") or ""),
        content=str(p.get("content") or ""),
        created_by=str(p.get("created_by") or ""),
        created_at=str(p.get("created_at") or p.get("occurred_at") or ""),
        updated_at=str(p.get("updated_at") or p.get("occurred_at") or ""),
        space_id=space_id,
        cover_image_url=cover if isinstance(cover, str) else None,
        last_editor_user_id=str(
            p.get("last_editor_user_id") or p.get("actor_user_id") or ""
        )
        or None,
    )


class SpaceContentInboundHandlers:
    """Register space-content inbound handlers."""

    __slots__ = (
        "_bus",
        "_authorship",
        "_post_repo",
        "_page_repo",
        "_sticky_repo",
        "_task_repo",
        "_calendar_repo",
        "_poll_repo",
        "_gallery_repo",
        "_zone_repo",
        "_bazaar_repo",
        "_timetable_repo",
        "_media_dir",
        "_media_refs",
        "_page_conflicts",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        authorship: "SpaceAuthorship",
        post_repo: "AbstractSpacePostRepo",
        page_repo: "AbstractPageRepo",
        sticky_repo: "AbstractStickyRepo",
        task_repo: "AbstractSpaceTaskRepo",
        calendar_repo: "AbstractSpaceCalendarRepo",
        poll_repo: "AbstractSpacePollRepo | None" = None,
        gallery_repo: "AbstractGalleryRepo | None" = None,
        zone_repo: "AbstractSpaceZoneRepo | None" = None,
        bazaar_repo: "AbstractBazaarRepo | None" = None,
        timetable_repo: "AbstractSpaceTimetableRepo | None" = None,
        media_dir: "pathlib.Path | None" = None,
        media_refs: "AbstractMediaReferenceRepo | None" = None,
        page_conflicts: "PageConflictService | None" = None,
    ) -> None:
        self._bus = bus
        #: v_48 — applies another household's version of an existing page
        #: (fast-forward / merge / conflict). ``None``: last write wins.
        self._page_conflicts = page_conflicts
        self._authorship = authorship
        self._post_repo = post_repo
        self._page_repo = page_repo
        self._sticky_repo = sticky_repo
        self._task_repo = task_repo
        self._calendar_repo = calendar_repo
        self._poll_repo = poll_repo
        self._gallery_repo = gallery_repo
        self._zone_repo = zone_repo
        self._bazaar_repo = bazaar_repo
        self._timetable_repo = timetable_repo
        #: Where a federated gallery delete removes the files it leaves
        #: unreferenced. Without both, files are kept for the orphan sweep.
        self._media_dir = media_dir
        self._media_refs = media_refs

    def attach_to(self, federation_service: "FederationService") -> None:
        registry = federation_service._event_registry

        # Tasks
        registry.register(FederationEventType.SPACE_TASK_CREATED, self._on_task_saved)
        registry.register(FederationEventType.SPACE_TASK_UPDATED, self._on_task_saved)
        registry.register(FederationEventType.SPACE_TASK_DELETED, self._on_task_deleted)
        # Task lists (v_40)
        registry.register(
            FederationEventType.SPACE_TASK_LIST_CREATED, self._on_task_list_created
        )
        registry.register(
            FederationEventType.SPACE_TASK_LIST_UPDATED, self._on_task_list_updated
        )
        registry.register(
            FederationEventType.SPACE_TASK_LIST_DELETED, self._on_task_list_deleted
        )

        # Pages
        if self._page_conflicts is not None:
            # A member's page proposal into a space archived here is
            # answered ``refused/archived`` (the archived gate drops it
            # before any handler runs).
            federation_service.add_archived_write_listener(self._on_archived_page_write)
        registry.register(FederationEventType.SPACE_PAGE_CREATED, self._on_page_saved)
        registry.register(FederationEventType.SPACE_PAGE_UPDATED, self._on_page_saved)
        registry.register(FederationEventType.SPACE_PAGE_DELETED, self._on_page_deleted)

        # Stickies
        registry.register(
            FederationEventType.SPACE_STICKY_CREATED, self._on_sticky_saved
        )
        registry.register(
            FederationEventType.SPACE_STICKY_UPDATED, self._on_sticky_saved
        )
        registry.register(
            FederationEventType.SPACE_STICKY_DELETED, self._on_sticky_deleted
        )

        # Calendar events
        registry.register(
            FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
            self._on_calendar_saved,
        )
        registry.register(
            FederationEventType.SPACE_CALENDAR_EVENT_UPDATED,
            self._on_calendar_saved,
        )
        registry.register(
            FederationEventType.SPACE_CALENDAR_EVENT_DELETED,
            self._on_calendar_deleted,
        )
        # Per-(event, user, occurrence) RSVPs.
        registry.register(
            FederationEventType.SPACE_RSVP_UPDATED,
            self._on_rsvp_updated,
        )
        registry.register(
            FederationEventType.SPACE_RSVP_DELETED,
            self._on_rsvp_deleted,
        )

        # Polls — only registered when a poll_repo is attached (deployments
        # without polls skip it entirely, classical behaviour). Poll
        # creation rides inline on ``SPACE_POST_CREATED`` (posts with
        # ``type=poll`` carry the poll body), so there is no inbound
        # handler for the bare ``SPACE_POLL_CREATED`` event — the
        # dispatch registry no-ops on unknown events.
        if self._poll_repo is not None:
            registry.register(
                FederationEventType.SPACE_POLL_VOTE_CAST,
                self._on_poll_vote,
            )
            registry.register(
                FederationEventType.SPACE_POLL_CLOSED,
                self._on_poll_closed,
            )
            # Schedule polls piggy-back on the poll repo — the
            # response / finalized rows live in the same SQLite module.
            registry.register(
                FederationEventType.SPACE_SCHEDULE_CREATED,
                self._on_schedule_created,
            )
            registry.register(
                FederationEventType.SPACE_SCHEDULE_RESPONSE_UPDATED,
                self._on_schedule_response_updated,
            )
            registry.register(
                FederationEventType.SPACE_SCHEDULE_FINALIZED,
                self._on_schedule_finalized,
            )

        # Gallery — only registered when a gallery_repo is wired. Albums
        # federate their own lifecycle (v_33): an item lands only in an
        # album this household already holds for the space, so an album
        # made after the members joined must reach them first.
        if self._gallery_repo is not None:
            registry.register(
                FederationEventType.SPACE_GALLERY_ALBUM_CREATED,
                self._on_gallery_album_created,
            )
            registry.register(
                FederationEventType.SPACE_GALLERY_ALBUM_UPDATED,
                self._on_gallery_album_updated,
            )
            registry.register(
                FederationEventType.SPACE_GALLERY_ALBUM_DELETED,
                self._on_gallery_album_deleted,
            )
            registry.register(
                FederationEventType.SPACE_GALLERY_ITEM_CREATED,
                self._on_gallery_item_saved,
            )
            registry.register(
                FederationEventType.SPACE_GALLERY_ITEM_DELETED,
                self._on_gallery_item_deleted,
            )

        # Per-space zones (§23.8.7). Only registered when a zone_repo
        # is wired — keeps the handler optional so older deployments
        # without the catalogue don't choke on inbound zone events.
        if self._zone_repo is not None:
            registry.register(
                FederationEventType.SPACE_ZONE_UPSERTED,
                self._on_zone_upserted,
            )
            registry.register(
                FederationEventType.SPACE_ZONE_DELETED,
                self._on_zone_deleted,
            )

        # Space timetables (v_39) — moderator-only, like zones.
        if self._timetable_repo is not None:
            registry.register(
                FederationEventType.SPACE_TIMETABLE_UPSERTED,
                self._on_timetable_upserted,
            )
            registry.register(
                FederationEventType.SPACE_TIMETABLE_DELETED,
                self._on_timetable_deleted,
            )

        # Bazaar listings. Only registered when a bazaar_repo is wired
        # so deployments without the bazaar feature don't choke on
        # inbound BAZAAR_LISTING_CREATED from a peer that does have it.
        # The wrapper post lands via SPACE_POST_CREATED first; this
        # handler fills in the BazaarListing row keyed on the same
        # ``post_id``.
        if self._bazaar_repo is not None:
            registry.register(
                FederationEventType.BAZAAR_LISTING_CREATED,
                self._on_bazaar_listing_created,
            )
            registry.register(
                FederationEventType.BAZAAR_LISTING_UPDATED,
                self._on_bazaar_listing_updated,
            )
            # F7: cross-household bids + offer acceptance.
            registry.register(
                FederationEventType.BAZAAR_BID_PLACED,
                self._on_bazaar_bid_placed,
            )
            registry.register(
                FederationEventType.BAZAAR_OFFER_ACCEPTED,
                self._on_bazaar_offer_accepted,
            )

    # ─── Tasks ───────────────────────────────────────────────────────────

    async def _on_task_saved(self, event: "FederationEvent") -> None:
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        task_id = str(p.get("id") or p.get("task_id") or "")
        # Collaborative, like the local rule (``SpaceTaskService`` lets any
        # space member update, archive or delete any task): an edit needs a
        # writer household, and the upsert keeps the row's own
        # ``created_by``, so the claim is only bound for a new task.
        held = await self._task_repo.get(task_id) if task_id else None
        if held is not None and held[0] != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="task", row_id=task_id
            )
            return
        if (
            held is None
            and task_id
            and await self._task_repo.is_task_deleted(task_id, space_id=space_id)
        ):
            # A household that missed the delete re-announcing the task
            # (or a replay of its create): the tombstone wins.
            log_not_applied(event, what="task", row_id=task_id, reason="deleted here")
            return
        existing = held[1] if held is not None else None
        # The shared wire codec merges onto the held row: an absent key
        # keeps our value, and a v39 sender's lossy fields never wipe it.
        task = task_from_wire_dict(p, existing=existing)
        if task is None:
            log.warning(
                "%s from %s: task without an id, list id or visible title — dropping",
                event.event_type,
                event.from_instance,
            )
            return
        if existing is None and self._bound_id_refused(
            event, SPACE_TASK_KIND, task.id, space_id, task.created_by
        ):
            return
        if not await self._collaborative_write_allowed(
            event,
            space_id,
            what="task",
            row_id=task.id,
            claimed_author=task.created_by if existing is None else "",
            feature="tasks",
            action=(
                ContentAction.CREATE
                if existing is None
                # A move within its column (only the position changed) is
                # LAYOUT, as on the local path — never held for review.
                else ContentAction.LAYOUT
                if replace(
                    task, position=existing.position, updated_at=existing.updated_at
                )
                == existing
                else ContentAction.EDIT
            ),
            # The held row's creator for an edit — never the payload's. As
            # locally, a task's assignee owns its status (and position): a
            # change of only those, named by an assignee of the HELD row, is
            # judged as their own row's edit.
            row_owner=(
                task.created_by
                if existing is None
                else _assignee_owner(existing, task, payload_actor(event))
            ),
        ):
            return
        if not await self._task_repo.save(task, space_id=space_id):
            if existing is None:
                # Most often a task that overtook its list's create. The
                # list's own event or the ``task_lists`` sync heals it — a
                # task never creates its list.
                log.warning(
                    "%s from %s: task %s names list %s, which is not held in "
                    "space %s — refusing the write",
                    event.event_type,
                    event.from_instance,
                    task.id,
                    task.list_id,
                    space_id,
                )
            else:
                log_cross_space_refusal(
                    event, space_id=space_id, what="task", row_id=task.id
                )
            return
        # Live refresh for local members, with the row as STORED (the
        # upsert keeps columns a payload can't change, like ``created_by``);
        # ``origin_instance_id`` stops the outbound bridge from echoing the
        # peer's own edit back.
        stored = await self._task_repo.get(task.id)
        if stored is not None:
            task = stored[1]
        if existing is None:
            await self._bus.publish(
                TaskCreated(
                    task=task,
                    space_id=space_id,
                    origin_instance_id=event.from_instance,
                )
            )
        else:
            await self._bus.publish(
                TaskUpdated(
                    task=task,
                    space_id=space_id,
                    origin_instance_id=event.from_instance,
                )
            )

    async def _on_task_deleted(self, event: "FederationEvent") -> None:
        space_id = resolve_space_id(event)
        if not space_id:
            return
        task_id = str(event.payload.get("id") or event.payload.get("task_id") or "")
        if not task_id:
            return
        existing = await self._task_repo.get(task_id)
        if existing is None:
            log_not_applied(
                event, what="task", row_id=task_id, reason="no such task here"
            )
            return
        if existing[0] != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="task", row_id=task_id
            )
            return
        if not await self._collaborative_write_allowed(
            event,
            space_id,
            what="task",
            row_id=task_id,
            feature="tasks",
            action=ContentAction.DELETE,
            row_owner=existing[1].created_by,
        ):
            return
        # Tombstoned, not removed (migration 0071): sync / resume tell a
        # household that missed it, and a stale copy cannot come back.
        if not await self._task_repo.delete(
            task_id, space_id=space_id, deleted_by=_deleter(event)
        ):
            log_cross_space_refusal(
                event, space_id=space_id, what="task", row_id=task_id
            )
            return
        await self._bus.publish(
            TaskDeleted(
                task_id=task_id,
                list_id=existing[1].list_id,
                space_id=space_id,
                origin_instance_id=event.from_instance,
            )
        )

    # ─── Task lists (v_40) ───────────────────────────────────────────────
    #
    # Collaborative like the local rule (any writable member may create,
    # rename or delete a space list): a write needs a writer household; a
    # new list's id is owner-bound to its ``created_by``.

    async def _on_task_list_created(self, event: "FederationEvent") -> None:
        space_id = resolve_space_id(event)
        if not space_id:
            return
        lst = task_list_from_wire_dict(event.payload)
        if lst is None:
            log.warning(
                "%s from %s: task list without an id or visible name — dropping",
                event.event_type,
                event.from_instance,
            )
            return
        held = await self._task_repo.get_list(lst.id)
        if held is not None and held[0] != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="task list", row_id=lst.id
            )
            return
        if held is None and await self._task_repo.is_list_deleted(
            lst.id, space_id=space_id
        ):
            # A household that missed the delete re-announcing the list
            # (or a replay of its create): the tombstone wins.
            log_not_applied(
                event, what="task list", row_id=lst.id, reason="deleted here"
            )
            return
        if held is None and not lst.created_by:
            log.warning(
                "%s from %s: new task list %s names no created_by — dropping",
                event.event_type,
                event.from_instance,
                lst.id,
            )
            return
        if held is None and self._bound_id_refused(
            event, SPACE_TASK_LIST_KIND, lst.id, space_id, lst.created_by
        ):
            return
        if held is None and not is_owner_bound(lst.id):
            # Every v_40 sender mints owner-bound list ids, and this event
            # is new in v_40 — a legacy (uuid4) id here can only be a squat
            # on a pre-v_40 list another space holds. Such lists reach us
            # through their host's sync stream instead.
            log.warning(
                "%s from %s: task list %s for space %s has a legacy (unbound) "
                "id — refusing; pre-v40 lists arrive through the host's sync",
                event.event_type,
                event.from_instance,
                lst.id,
                space_id,
            )
            return
        if not await self._collaborative_write_allowed(
            event,
            space_id,
            what="task list",
            row_id=lst.id,
            claimed_author=lst.created_by if held is None else "",
            feature="tasks",
            action=ContentAction.CREATE if held is None else ContentAction.EDIT,
            row_owner=lst.created_by if held is None else held[1].created_by,
        ):
            return
        if not await self._task_repo.save_list(lst, space_id=space_id):
            log_cross_space_refusal(
                event, space_id=space_id, what="task list", row_id=lst.id
            )
            return
        lst = await self._stored_list(lst)
        if held is None:
            await self._bus.publish(
                TaskListCreated(
                    list_id=lst.id,
                    name=lst.name,
                    space_id=space_id,
                    created_by=lst.created_by,
                    origin_instance_id=event.from_instance,
                )
            )
        else:
            await self._bus.publish(
                TaskListUpdated(
                    list_id=lst.id,
                    name=lst.name,
                    space_id=space_id,
                    origin_instance_id=event.from_instance,
                )
            )

    async def _on_task_list_updated(self, event: "FederationEvent") -> None:
        """A rename. Only a list already held here is renamed — an unseen
        list arrives through its create or the ``task_lists`` sync."""
        space_id = resolve_space_id(event)
        if not space_id:
            return
        lst = task_list_from_wire_dict(event.payload)
        if lst is None:
            log.warning(
                "%s from %s: task list without an id or visible name — dropping",
                event.event_type,
                event.from_instance,
            )
            return
        held = await self._task_repo.get_list(lst.id)
        if held is None:
            log_not_applied(
                event, what="task list", row_id=lst.id, reason="no such list here"
            )
            return
        if held[0] != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="task list", row_id=lst.id
            )
            return
        if not await self._collaborative_write_allowed(
            event,
            space_id,
            what="task list",
            row_id=lst.id,
            feature="tasks",
            action=ContentAction.EDIT,
            row_owner=held[1].created_by,
        ):
            return
        renamed = TaskList(id=lst.id, name=lst.name, created_by=held[1].created_by)
        if not await self._task_repo.save_list(renamed, space_id=space_id):
            log_cross_space_refusal(
                event, space_id=space_id, what="task list", row_id=lst.id
            )
            return
        renamed = await self._stored_list(renamed)
        await self._bus.publish(
            TaskListUpdated(
                list_id=renamed.id,
                name=renamed.name,
                space_id=space_id,
                origin_instance_id=event.from_instance,
            )
        )

    async def _stored_list(self, fallback: TaskList) -> TaskList:
        """The list as the repo now holds it (for the live event)."""
        stored = await self._task_repo.get_list(fallback.id)
        return stored[1] if stored is not None else fallback

    async def _on_task_list_deleted(self, event: "FederationEvent") -> None:
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        list_id = str(p.get("id") or p.get("list_id") or "")
        if not list_id:
            return
        held = await self._task_repo.get_list(list_id)
        if held is None:
            log_not_applied(
                event, what="task list", row_id=list_id, reason="no such list here"
            )
            return
        if held[0] != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="task list", row_id=list_id
            )
            return
        if not await self._collaborative_write_allowed(
            event,
            space_id,
            what="task list",
            row_id=list_id,
            feature="tasks",
            action=ContentAction.DELETE,
            row_owner=held[1].created_by,
        ):
            return
        # Tombstoned, not removed (sync / resume tell a household that
        # missed it); the list-tombstone trigger (0071) tombstones its tasks.
        if not await self._task_repo.delete_list(
            list_id, space_id=space_id, deleted_by=_deleter(event)
        ):
            log_cross_space_refusal(
                event, space_id=space_id, what="task list", row_id=list_id
            )
            return
        await self._bus.publish(
            TaskListDeleted(
                list_id=list_id,
                space_id=space_id,
                origin_instance_id=event.from_instance,
            )
        )

    # ─── Pages ───────────────────────────────────────────────────────────

    async def _on_page_saved(self, event: "FederationEvent") -> None:
        """A space page from another household (v_48: host-sequenced).

        * **Under a pre-v_48 host** (or none wired): mirrored as sent, last
          write wins.
        * **On the host**: every member's ``SPACE_PAGE_CREATED`` /
          ``_UPDATED`` is a *proposal* — after the gates (a refusal is
          answered ``refused``/``access``) it goes to
          :meth:`PageConflictService.sequence`.
        * **On a member household**: only the host's versions are taken,
          ordered by ``seq`` (:meth:`PageConflictService.mirror`); another
          member's version of a page held here is ignored, and an unknown
          page another member creates is stored as before, unsequenced.

        Page timestamps are ISO strings (matches the domain type —
        `Page.created_at`/`updated_at` are `str`).
        """
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        page_id = str(p.get("id") or p.get("page_id") or "")
        title = str(p.get("title") or "")
        if not page_id or (not title and "sequenced" not in p):
            log.debug("SPACE_PAGE_* missing required field")
            return
        existing = await self._page_repo.get(page_id)
        if existing is not None and existing.space_id != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="page", row_id=page_id
            )
            return
        engine = self._page_conflicts
        mode, host = (
            await engine.mode(space_id) if engine is not None else (PageMode.LEGACY, "")
        )
        if mode is PageMode.HOST:
            await self._on_page_proposal(event, space_id, page_id, existing)
            return
        tombstoned = existing is None and await self._page_repo.is_page_deleted(
            page_id, space_id=space_id
        )
        if tombstoned and not (host and event.from_instance == host and "seq" in p):
            # Deleted here (migration 0073): a household that missed the
            # delete, or its replay, never brings the page back. Only the
            # host's version may — and only over a tombstone the host never
            # confirmed (:meth:`PageConflictService.mirror`).
            log_not_applied(event, what="page", row_id=page_id, reason="deleted here")
            return
        if (
            mode is PageMode.LEGACY
            and host
            and event.from_instance == host
            and "seq" in p
            and canonical_from_wire(p) is not None
        ):
            # The host sequences (it sent ``seq``) even if we have not seen
            # its v_48 capabilities yet: mirror it as the host's version,
            # never last write wins on our possibly stale view.
            await self._on_host_page_version(
                event, space_id, page_id, existing, tombstoned=tombstoned
            )
            return
        if mode is PageMode.MEMBER and event.from_instance == host:
            await self._on_host_page_version(
                event, space_id, page_id, existing, tombstoned=tombstoned
            )
            return
        if tombstoned:
            return
        if mode is PageMode.MEMBER and existing is not None:
            log.debug(
                "%s from %s: page %s in space %s — only its host sequences a "
                "held page; ignored",
                event.event_type,
                event.from_instance,
                page_id,
                space_id,
            )
            return
        if "sequenced" in p or not title:
            return
        page = _page_from_payload(p, page_id, space_id)
        if mode is PageMode.MEMBER and (
            event.event_type is not FederationEventType.SPACE_PAGE_CREATED
        ):
            return  # an update of a page we never held, not from the host
        if not await self._page_write_gates(event, space_id, page_id, page, existing):
            return
        if existing is not None and engine is not None:
            # A pre-v_48 host: last write wins, the old body kept in history.
            await engine.legacy_apply(existing, page, space_id=space_id)
            return
        if not await self._page_repo.save(page, space_id=space_id):
            log_cross_space_refusal(
                event, space_id=space_id, what="page", row_id=page_id
            )

    async def _page_write_gates(
        self,
        event: "FederationEvent",
        space_id: str,
        page_id: str,
        page: Page,
        existing: Page | None,
    ) -> bool:
        """Collaborative (any member edits a space page locally): an edit
        needs a writer household; a NEW page is attributed to whoever the
        payload names, so that name must be the sender's (or the host's
        relay), and its id must be bound to that creator."""
        if existing is None and self._bound_id_refused(
            event, SPACE_PAGE_KIND, page_id, space_id, page.created_by
        ):
            return False
        return await self._collaborative_write_allowed(
            event,
            space_id,
            what="page",
            row_id=page_id,
            claimed_author=page.created_by if existing is None else "",
            feature="pages",
            action=ContentAction.CREATE if existing is None else ContentAction.EDIT,
            row_owner=page.created_by if existing is None else existing.created_by,
        )

    async def _on_archived_page_write(self, event: "FederationEvent", space) -> None:
        """The archived gate refused a write. Only a household holding a
        live writer seat in the space hears back about a page proposal —
        anyone else gets silence, so the refusal is no oracle — and the
        answer carries no page state."""
        if self._page_conflicts is None or space is None:
            return
        if event.event_type not in (
            FederationEventType.SPACE_PAGE_CREATED,
            FederationEventType.SPACE_PAGE_UPDATED,
        ):
            return
        if not await self._authorship.writes_here(event, str(space.id)):
            return
        await self._page_conflicts.on_archived_write(event, space)

    async def _on_page_proposal(
        self,
        event: "FederationEvent",
        space_id: str,
        page_id: str,
        existing: Page | None,
    ) -> None:
        """The host: a member household proposes a version of a page.

        A replay is not a proposal: a resume replay (``replay: true``), a
        payload carrying ``seq`` (a version shape) or, for a page held here,
        a ``SPACE_PAGE_CREATED`` without ``base_seq`` never changes — or
        creates — a page here. A live update without
        ``base_seq`` is a pre-v_48 (or not-yet-upgraded) sender's — based on
        the current version. The named actor must be seated on the sender
        at EVERY access level (no other household's user is ever a page's
        editor or a side's author); then the space's level must admit them.
        """
        engine = self._page_conflicts
        assert engine is not None
        p = event.payload
        if "sequenced" in p:
            log.debug("a host takes no host versions (page %s) — ignored", page_id)
            return
        if p.get("replay") is True or (
            existing is not None
            and (
                "seq" in p
                or (
                    event.event_type is FederationEventType.SPACE_PAGE_CREATED
                    and "base_seq" not in p
                )
            )
        ):
            log.debug(
                "%s from %s: page %s — a replay, not a proposal; ignored",
                event.event_type,
                event.from_instance,
                page_id,
            )
            return
        proposal = proposal_from_wire(p)
        if (
            proposal is None
            or not proposal.title
            or len(proposal.title) > MAX_PAGE_TITLE_LENGTH
        ):
            log.warning(
                "%s from %s: page %s in space %s — malformed proposal; refused",
                event.event_type,
                event.from_instance,
                page_id,
                space_id,
            )
            await engine.refuse(
                space_id=space_id,
                page_id=page_id,
                proposal_hash=_payload_hash(p),
                proposer_instance=event.from_instance,
                reason="bad_base",
            )
            return
        if existing is None and (
            (proposal.base_seq or 0) > 0
            or await self._page_repo.is_page_deleted(page_id, space_id=space_id)
        ):
            # Gone: never held, or deleted here (a tombstone is never
            # brought back, whatever ``base_seq`` it claims). The member
            # keeps its words and the SPA offers "Save as new page".
            await engine.refuse(
                space_id=space_id,
                page_id=page_id,
                proposal_hash=proposal.hash,
                proposer_instance=event.from_instance,
                reason="gone",
            )
            return
        if existing is None and (
            not proposal.created_by
            or self._bound_id_refused(
                event, SPACE_PAGE_KIND, page_id, space_id, proposal.created_by
            )
        ):
            return
        actor = proposal.actor_user_id
        # A demoted (read-only) author still edits their own page, as locally.
        bound = await self._authorship.acts_for(
            event,
            space_id,
            actor,
            any_role=existing is not None and actor == existing.created_by,
        )
        if bound and existing is None and proposal.created_by != actor:
            bound = False  # a create is its creator's own write
        if not bound:
            held = await self._authorship.hold_or_refuse(
                event, space_id=space_id, what="page", row_id=page_id, user_id=actor
            )
            if not held:
                await engine.refuse(
                    space_id=space_id,
                    page_id=page_id,
                    proposal_hash=proposal.hash,
                    proposer_instance=event.from_instance,
                    reason="access",
                )
            return
        if not await self._authorship.access_admits(
            event,
            space_id,
            "pages",
            ContentAction.CREATE if existing is None else ContentAction.EDIT,
            actor=actor,
            row_owner=proposal.created_by if existing is None else existing.created_by,
        ):
            await engine.refuse(
                space_id=space_id,
                page_id=page_id,
                proposal_hash=proposal.hash,
                proposer_instance=event.from_instance,
                reason="access",
            )
            return
        await engine.sequence(
            space_id=space_id,
            page_id=page_id,
            proposal=proposal,
            proposer_instance=event.from_instance,
        )

    async def _on_host_page_version(
        self,
        event: "FederationEvent",
        space_id: str,
        page_id: str,
        existing: Page | None,
        *,
        tombstoned: bool = False,
    ) -> None:
        """A member household: the host's canonical version (or its answer
        to our proposal), mirrored by ``seq``. ``tombstoned``: we hold the
        page as a tombstone — the engine revives it only if the host never
        confirmed the delete and still holds the page at or above its
        ``seq``; the row (and its creator) is ours, so it is judged as a
        held page."""
        engine = self._page_conflicts
        assert engine is not None
        p = event.payload
        version = canonical_from_wire(p)
        if version is None:
            if "seq" in p or "sequenced" in p:
                log.warning(
                    "%s from host %s: page %s in space %s — malformed version; ignored",
                    event.event_type,
                    event.from_instance,
                    page_id,
                    space_id,
                )
            return
        held = existing is not None or tombstoned
        if not held and (
            event.event_type is not FederationEventType.SPACE_PAGE_CREATED
            or not version.has_state
        ):
            return  # an update of a page we never held
        refusal = (
            version.sequenced is not None and version.sequenced.outcome == "refused"
        )
        if version.has_state and not refusal:
            # The host already ran the access gate on the edit it sequenced
            # (its ``actor_user_id`` may be any household's member, never
            # seated on the host): only the authorship of a new page — an
            # id bound to its creator, whom the host relays — is checked.
            page = _page_from_payload(p, page_id, space_id)
            if not held and self._bound_id_refused(
                event, SPACE_PAGE_KIND, page_id, space_id, page.created_by
            ):
                return
            if not await self._authorship_allows(
                event,
                space_id,
                what="page",
                row_id=page_id,
                claimed_author=page.created_by if not held else "",
            ):
                return
            # A moderation release is still checked against the item this
            # household holds (v_43 defence in depth).
            if MODERATION_BLOCK_KEY in p and not await self._authorship.access_admits(
                event,
                space_id,
                "pages",
                ContentAction.CREATE if not held else ContentAction.EDIT,
                actor=payload_actor(event),
                row_owner=existing.created_by if existing else page.created_by,
            ):
                return
        await engine.mirror(space_id=space_id, page_id=page_id, version=version)

    async def _on_page_deleted(self, event: "FederationEvent") -> None:
        """A space page delete (migration 0073: tombstoned, not removed).

        Deletes are not sequenced, so households may judge one differently;
        as for every page decision, only the **host's** view is final:

        * from the host of a v_48 space the delete is taken as final (the
          host already judged it — its ``actor_user_id`` may be seated on
          any household), confirming a tombstone we made ourselves;
        * from another household it is gated as before and tombstoned
          *unconfirmed*: a later host version at or above its ``seq``
          brings the page back (:meth:`PageConflictService.mirror`);
        * on the host, an accepted member delete is final and the host
          re-broadcasts it, so every member converges on the host's view.
        """
        space_id = resolve_space_id(event)
        if not space_id:
            return
        page_id = str(event.payload.get("id") or event.payload.get("page_id") or "")
        if not page_id:
            return
        engine = self._page_conflicts
        mode, host = (
            await engine.mode(space_id) if engine is not None else (PageMode.LEGACY, "")
        )
        from_host = bool(host) and event.from_instance == host
        existing = await self._page_repo.get(page_id)
        if existing is None:
            await self._page_delete_of_unseen(event, space_id, page_id, from_host)
            return
        if existing.space_id != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="page", row_id=page_id
            )
            return
        if not (mode is PageMode.MEMBER and from_host) and (
            not await self._collaborative_write_allowed(
                event,
                space_id,
                what="page",
                row_id=page_id,
                feature="pages",
                action=ContentAction.DELETE,
                row_owner=existing.created_by,
            )
        ):
            return
        deleter = _deleter(event)
        deleted = (
            await engine.delete_page(
                space_id,
                page_id,
                deleted_by=deleter,
                from_instance=event.from_instance,
            )
            if engine is not None
            else await self._page_repo.delete(
                page_id, space_id=space_id, deleted_by=deleter, confirmed=False
            )
        )
        if not deleted:
            log_cross_space_refusal(
                event, space_id=space_id, what="page", row_id=page_id
            )
            return
        if mode is PageMode.HOST:
            # The host decided: tell every member household, so the ones
            # that missed (or refused) the member's own broadcast converge.
            await self._bus.publish(
                PageDeleted(page_id=page_id, space_id=space_id, actor_user_id=deleter)
            )

    async def _page_delete_of_unseen(
        self,
        event: "FederationEvent",
        space_id: str,
        page_id: str,
        from_host: bool,
    ) -> None:
        """A delete of a page not held live here. From the space's **host**
        (live, its re-broadcast, or its resume replay) it confirms our own
        unconfirmed tombstone; a page we never held gets a stub tombstone,
        so a stale copy another household streams or replays later cannot
        create it — but only for an id owner-bound to its ``created_by`` in
        THIS space (page ids are global)."""
        if await self._page_repo.is_page_deleted(page_id, space_id=space_id):
            if from_host:
                await self._page_repo.confirm_delete(page_id, space_id=space_id)
            else:
                log_not_applied(
                    event, what="page", row_id=page_id, reason="deleted here already"
                )
            return
        created_by = str(event.payload.get("created_by") or "")
        if (
            not from_host
            or check_owner_bound_id(
                SPACE_PAGE_KIND, page_id, space_id=space_id, owner_user_id=created_by
            )
            is not OwnerBinding.VALID
        ):
            log_not_applied(
                event, what="page", row_id=page_id, reason="no such page here"
            )
            return
        await self._page_repo.tombstone(
            page_id,
            space_id=space_id,
            created_by=created_by,
            deleted_by=_deleter(event),
        )

    # ─── Stickies ────────────────────────────────────────────────────────

    async def _on_sticky_saved(self, event: "FederationEvent") -> None:
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        sticky_id = str(p.get("id") or p.get("sticky_id") or "")
        author = str(p.get("author") or p.get("created_by") or "")
        # Display fields go through the shared sticky rules: a peer's
        # ``color`` is rendered as CSS, so a non-hex value (``url(...)``
        # tracking beacon, ``red; ...``) is replaced, never stored.
        fields = coerce_peer_sticky(
            content=p.get("content") or p.get("text"),
            color=p.get("color") or p.get("colour"),
            position_x=p.get("position_x"),
            position_y=p.get("position_y"),
        )
        if not sticky_id or not fields.content:
            log.debug("SPACE_STICKY_* missing required field")
            return
        if fields.truncated:
            log.warning(
                "%s from %s: sticky %s in space %s — content over %d "
                "characters, truncated",
                event.event_type,
                event.from_instance,
                sticky_id,
                space_id,
                MAX_STICKY_CONTENT_LENGTH,
            )
        if await self._sticky_repo.is_deleted(sticky_id, space_id=space_id):
            # Migration 0085: a deleted sticky's id never comes back.
            log_not_applied(
                event, what="sticky", row_id=sticky_id, reason="deleted here already"
            )
            return
        existing = await self._sticky_repo.get(sticky_id)
        if existing is not None and existing.space_id != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="sticky", row_id=sticky_id
            )
            return
        if existing is not None:
            # Collaborative edit — ``SPACE_STICKY_UPDATED`` carries no
            # author, and the upsert keeps the row's own anyway.
            author = existing.author
        elif not author:
            log.debug("SPACE_STICKY_CREATED missing author")
            return
        elif self._bound_id_refused(
            event, SPACE_STICKY_KIND, sticky_id, space_id, author
        ):
            return
        if not await self._collaborative_write_allowed(
            event,
            space_id,
            what="sticky",
            row_id=sticky_id,
            claimed_author=author if existing is None else "",
            feature="stickies",
            action=(
                ContentAction.CREATE
                if existing is None
                # Same words and colour: a drag across the board (LAYOUT).
                else ContentAction.LAYOUT
                if fields.content == existing.content and fields.color == existing.color
                else ContentAction.EDIT
            ),
            row_owner=author,
        ):
            return
        now_iso = str(
            p.get("updated_at") or p.get("created_at") or p.get("occurred_at") or "",
        )
        sticky = Sticky(
            id=sticky_id,
            author=author,
            content=fields.content,
            color=fields.color,
            position_x=fields.position_x,
            position_y=fields.position_y,
            created_at=str(p.get("created_at") or p.get("occurred_at") or ""),
            updated_at=now_iso,
            space_id=space_id,
        )
        if not await self._sticky_repo.save(sticky, space_id=space_id):
            log_cross_space_refusal(
                event, space_id=space_id, what="sticky", row_id=sticky_id
            )

    async def _on_sticky_deleted(self, event: "FederationEvent") -> None:
        space_id = resolve_space_id(event)
        if not space_id:
            return
        sticky_id = str(event.payload.get("id") or event.payload.get("sticky_id") or "")
        if not sticky_id:
            return
        existing = await self._sticky_repo.get(sticky_id)
        if existing is None:
            log_not_applied(
                event, what="sticky", row_id=sticky_id, reason="no such sticky here"
            )
            return
        if existing.space_id != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="sticky", row_id=sticky_id
            )
            return
        if not await self._collaborative_write_allowed(
            event,
            space_id,
            what="sticky",
            row_id=sticky_id,
            feature="stickies",
            action=ContentAction.DELETE,
            row_owner=existing.author,
        ):
            return
        if not await self._sticky_repo.delete(
            sticky_id, space_id=space_id, deleted_by=payload_actor(event) or ""
        ):
            log_cross_space_refusal(
                event, space_id=space_id, what="sticky", row_id=sticky_id
            )

    # ─── Calendar events ─────────────────────────────────────────────────

    async def _on_calendar_saved(self, event: "FederationEvent") -> None:
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        event_id = str(p.get("id") or p.get("event_id") or "")
        calendar_id = str(p.get("calendar_id") or "")
        summary = str(p.get("summary") or p.get("title") or "")
        created_by = str(p.get("created_by") or "")
        start = parse_iso8601_optional(p.get("start"))
        end = parse_iso8601_optional(p.get("end"))
        if (
            not event_id
            or not calendar_id
            or not summary
            or not created_by
            or start is None
            or end is None
        ):
            log.debug("SPACE_CALENDAR_EVENT_* missing required field")
            return
        # Rendered as ``<img src>`` for every member: only a local media
        # reference — a third-party URL would leak their IPs.
        cover = verbatim_local_media_ref(p.get("cover_url"))
        location = p.get("location")
        ev = CalendarEvent(
            id=event_id,
            calendar_id=calendar_id,
            summary=summary,
            start=start,
            end=end,
            created_by=created_by,
            description=p.get("description"),
            all_day=bool(p.get("all_day", False)),
            attendees=tuple(str(a) for a in (p.get("attendees") or ())),
            mirrored_from=p.get("mirrored_from"),
            rrule=p.get("rrule"),
            cover_url=cover,
            location=location if isinstance(location, str) and location else None,
            # IANA wall-clock anchor. Old peers omit; default ``"UTC"``.
            # Peer-supplied, so validated here: an unknown zone name
            # makes ``Intl`` throw in the SPA and one bad row breaks
            # the whole space calendar tab. Fail closed on the value,
            # not the event — the row still lands, anchored to UTC.
            tz=coerce_tz(
                p.get("tz"),
                context=(f"{event.event_type} from instance {event.from_instance}"),
            ),
            # §23.15 opt-in feed mirror. Absent on an older sender →
            # default True so the bridge keeps the historic always-mirror
            # behaviour for events from un-upgraded peers.
            announce_in_feed=bool(p.get("announce_in_feed", True)),
        )
        if await self._calendar_repo.is_event_deleted(event_id, space_id=space_id):
            # Migration 0085: a deleted event's id never comes back.
            log_not_applied(
                event,
                what="calendar event",
                row_id=event_id,
                reason="deleted here already",
            )
            return
        existing = await self._calendar_repo.get_event(event_id)
        is_new = existing is None
        if existing is not None and existing[0] != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="calendar event", row_id=event_id
            )
            return
        if is_new and self._bound_id_refused(
            event, SPACE_CALENDAR_EVENT_KIND, event_id, space_id, created_by
        ):
            return
        # Collaborative (any member edits a space event locally); the
        # upsert keeps the row's own ``created_by``, so the claim is only
        # bound for a new event.
        if not await self._collaborative_write_allowed(
            event,
            space_id,
            what="calendar event",
            row_id=event_id,
            claimed_author=created_by if is_new else "",
            feature="calendar",
            action=ContentAction.CREATE if is_new else ContentAction.EDIT,
            row_owner=created_by if existing is None else existing[1].created_by,
        ):
            return
        if not await self._calendar_repo.save_event(ev, space_id=space_id):
            log_cross_space_refusal(
                event, space_id=space_id, what="calendar event", row_id=event_id
            )
            return
        # Publish on the local bus so the calendar→feed bridge (Phase B)
        # can mirror the event into space_posts on inbound federation
        # arrivals too. The bridge guards against duplicates by linked_event_id.
        if is_new:
            await self._bus.publish(CalendarEventCreated(event=ev))
        # Drain any RSVPs that arrived ahead of this event.
        try:
            await self._calendar_repo.flush_pending_rsvps(event_id, space_id=space_id)
        except AttributeError:
            # In-memory test fakes may not implement the buffer.
            pass

    async def _on_calendar_deleted(self, event: "FederationEvent") -> None:
        space_id = resolve_space_id(event)
        if not space_id:
            return
        event_id = str(event.payload.get("id") or event.payload.get("event_id") or "")
        if not event_id:
            return
        existing = await self._calendar_repo.get_event(event_id)
        if existing is None:
            log_not_applied(
                event,
                what="calendar event",
                row_id=event_id,
                reason="no such event here",
            )
            return
        if existing[0] != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="calendar event", row_id=event_id
            )
            return
        if not await self._collaborative_write_allowed(
            event,
            space_id,
            what="calendar event",
            row_id=event_id,
            feature="calendar",
            action=ContentAction.DELETE,
            row_owner=existing[1].created_by,
        ):
            return
        if not await self._calendar_repo.delete_event(
            event_id, space_id=space_id, deleted_by=payload_actor(event) or ""
        ):
            log_cross_space_refusal(
                event, space_id=space_id, what="calendar event", row_id=event_id
            )
            return
        # Mirror to the feed bridge so the linked post soft-deletes. The
        # space id scopes the ``calendar.deleted`` WS frame to the
        # space's members (without it RealtimeService fans it out to
        # the whole household).
        await self._bus.publish(
            CalendarEventDeleted(event_id=event_id, space_id=space_id)
        )

    # ─── RSVPs (per-occurrence) ──────────────────────────────────────────

    async def _on_rsvp_updated(self, event: "FederationEvent") -> None:
        """Apply a peer's RSVP. If the underlying event hasn't propagated
        yet, buffer the RSVP and let it flush on event arrival."""
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        event_id = str(p.get("event_id") or "")
        user_id = str(p.get("user_id") or "")
        occurrence_at = str(p.get("occurrence_at") or "")
        status = str(p.get("status") or "")
        updated_at = str(p.get("updated_at") or "")
        if (
            not event_id
            or not user_id
            or not occurrence_at
            or status not in RSVPStatus.ALL
        ):
            log.debug("SPACE_RSVP_UPDATED missing or invalid field")
            return
        # An RSVP is the user's own answer: only their household sends it
        # (or the event's organiser settling a request — see
        # :meth:`_rsvp_allowed`). Checked before the buffer too, so an early
        # RSVP cannot squat the (event, user, occurrence) buffer key for
        # somebody else's member.
        if not await self._rsvp_allowed(
            event, space_id, event_id, user_id, occurrence_at, status
        ):
            return
        # The parent event decides whether this is an out-of-order
        # arrival (buffer it) or a cross-space write (refuse it). The
        # write itself is scoped by the repo regardless of what this
        # read said — the read only picks between the two outcomes.
        result = await self._calendar_repo.get_event(event_id)
        if result is None:
            # Out-of-order: event hasn't arrived yet — buffer for flush.
            # The gated space rides along so the buffer can't launder a
            # write into a space this sender was never gated on.
            try:
                await self._calendar_repo.buffer_pending_rsvp(
                    event_id=event_id,
                    user_id=user_id,
                    occurrence_at=occurrence_at,
                    status=status,
                    updated_at=updated_at,
                    space_id=space_id,
                )
            except AttributeError:
                log.debug("calendar_repo lacks buffer_pending_rsvp")
            return
        if not await self._calendar_repo.upsert_rsvp(
            CalendarRSVP(
                event_id=event_id,
                user_id=user_id,
                status=status,
                updated_at=updated_at,
                occurrence_at=occurrence_at,
            ),
            space_id=space_id,
        ):
            log_cross_space_refusal(
                event, space_id=space_id, what="RSVP for event", row_id=event_id
            )

    async def _on_rsvp_deleted(self, event: "FederationEvent") -> None:
        """Apply a peer's RSVP removal. Like _on_rsvp_updated, buffers
        with status='removed' if the event hasn't propagated yet — so a
        later flush honours the deletion rather than resurrecting the
        RSVP."""
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        event_id = str(p.get("event_id") or "")
        user_id = str(p.get("user_id") or "")
        occurrence_at = str(p.get("occurrence_at") or "")
        updated_at = str(p.get("updated_at") or "")
        if not event_id or not user_id or not occurrence_at:
            log.debug("SPACE_RSVP_DELETED missing required field")
            return
        if not await self._rsvp_allowed(
            event, space_id, event_id, user_id, occurrence_at, "removed"
        ):
            return
        result = await self._calendar_repo.get_event(event_id)
        if result is None:
            try:
                await self._calendar_repo.buffer_pending_rsvp(
                    event_id=event_id,
                    user_id=user_id,
                    occurrence_at=occurrence_at,
                    status="removed",
                    updated_at=updated_at,
                    space_id=space_id,
                )
            except AttributeError:
                log.debug("calendar_repo lacks buffer_pending_rsvp")
            return
        if not await self._calendar_repo.remove_rsvp(
            event_id,
            user_id,
            occurrence_at=occurrence_at,
            space_id=space_id,
        ):
            log_cross_space_refusal(
                event, space_id=space_id, what="RSVP for event", row_id=event_id
            )

    # ─── Polls ──────────────────────────────────────────────────────────

    async def _on_poll_vote(self, event: "FederationEvent") -> None:
        """Mirror a remote poll vote. Enforces the single-choice invariant
        (the old vote is cleared first) matching the local poll service."""
        if self._poll_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        post_id = str(p.get("post_id") or "")
        option_id = str(p.get("option_id") or "")
        voter = str(p.get("voter_user_id") or p.get("user_id") or "")
        if not post_id or not option_id or not voter:
            log.debug("SPACE_POLL_VOTE_CAST missing required field")
            return
        if not await self._acts_for(event, space_id, voter, "poll vote", post_id):
            return
        # The repo proves, in the same transaction as the write, that the
        # option belongs to this post (a mismatched pair would corrupt the
        # tally) and that the post lives in the gated space.
        if not await self._poll_repo.cast_vote_in_space(
            space_id=space_id,
            post_id=post_id,
            option_id=option_id,
            voter_user_id=voter,
        ):
            log_cross_space_refusal(
                event, space_id=space_id, what="poll option", row_id=option_id
            )

    async def _on_poll_closed(self, event: "FederationEvent") -> None:
        if self._poll_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        post_id = str(event.payload.get("post_id") or "")
        if not post_id:
            return
        # Only the poll's author closes it (``PollService.close_poll``).
        if not await self._post_owner_acts(event, space_id, post_id, what="poll"):
            return
        if not await self._poll_repo.close_in_space(post_id, space_id=space_id):
            log_cross_space_refusal(
                event, space_id=space_id, what="poll", row_id=post_id
            )

    async def _on_schedule_created(
        self,
        event: "FederationEvent",
    ) -> None:
        """F5: persist a peer's schedule-poll slot defs locally so the
        remote member's SPA renders the slot picker.

        The wrapper ``PostType.SCHEDULE`` post arrived first via
        ``SPACE_POST_CREATED``; this fills in the
        ``space_schedule_poll_meta`` row + ``space_schedule_slots``
        children keyed on the same ``post_id``. Idempotent — repeat
        deliveries from a chunked-sync replay UPSERT cleanly.
        """
        if self._poll_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        post_id = str(p.get("post_id") or "")
        title = str(p.get("title") or "")
        slots_raw = p.get("slots") or []
        if not post_id or not title or not slots_raw:
            log.debug("SPACE_SCHEDULE_CREATED missing required field: %s", p)
            return
        # The slots belong to the wrapper post: its author's household
        # creates them.
        anchor = await self._post_in_space(
            event, space_id, post_id, what="schedule poll"
        )
        if anchor is None:
            return
        if not await self._authorship.may_author(event, space_id, anchor.author):
            await self._authorship.hold_or_refuse(
                event,
                space_id=space_id,
                what="schedule poll",
                row_id=post_id,
                user_id=anchor.author,
            )
            return
        try:
            if not await self._poll_repo.create_schedule_poll_in_space(
                space_id=space_id,
                post_id=post_id,
                title=title,
                deadline=p.get("deadline"),
                slots=list(slots_raw),
            ):
                log_cross_space_refusal(
                    event, space_id=space_id, what="schedule poll", row_id=post_id
                )
        except Exception as exc:
            # Malformed slot row etc. — log + drop; catch-up retries. (A
            # wrapper post that has not landed yet is a refusal above.)
            log.debug(
                "SPACE_SCHEDULE_CREATED apply failed for post=%s: %s",
                post_id,
                exc,
            )

    async def _on_schedule_response_updated(
        self,
        event: "FederationEvent",
    ) -> None:
        """Mirror a peer's schedule-poll vote / retraction locally."""
        if self._poll_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        slot_id = str(p.get("slot_id") or "")
        user_id = str(p.get("user_id") or "")
        response = str(p.get("response") or "")
        if not slot_id or not user_id:
            log.debug("SPACE_SCHEDULE_RESPONSE_UPDATED missing field")
            return
        if not await self._acts_for(
            event, space_id, user_id, "schedule answer", slot_id
        ):
            return
        if response == "retracted" or not response:
            applied = await self._poll_repo.delete_schedule_response_in_space(
                space_id=space_id,
                slot_id=slot_id,
                user_id=user_id,
            )
        else:
            applied = await self._poll_repo.upsert_schedule_response_in_space(
                space_id=space_id,
                slot_id=slot_id,
                user_id=user_id,
                response=response,
            )
        if not applied:
            log_cross_space_refusal(
                event, space_id=space_id, what="schedule slot", row_id=slot_id
            )

    async def _on_schedule_finalized(
        self,
        event: "FederationEvent",
    ) -> None:
        """Mirror a peer's schedule-poll finalisation. The matching
        local calendar entry is produced by
        :class:`ScheduleCalendarBridge` if the household enables it.
        """
        if self._poll_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        post_id = str(event.payload.get("post_id") or "")
        slot_id = str(event.payload.get("slot_id") or "")
        if not post_id or not slot_id:
            return
        # Only the poll's author finalises it (``finalize_schedule_poll``).
        if not await self._post_owner_acts(
            event, space_id, post_id, what="schedule poll"
        ):
            return
        if not await self._poll_repo.finalize_schedule_poll_in_space(
            space_id=space_id,
            post_id=post_id,
            slot_id=slot_id,
        ):
            log_cross_space_refusal(
                event, space_id=space_id, what="schedule poll", row_id=post_id
            )

    # ─── Gallery albums (§23.119, v_33) ──────────────────────────────────

    async def _on_gallery_album_created(self, event: "FederationEvent") -> None:
        """Mirror a member's new album into the gated space.

        The album is filed under the space the envelope was gated for and
        starts empty and non-system (``create_album_in_space``); its owner
        must be a member seated on the sending household, the same rule as
        any other create — never the shared bot identity. From v_34 the id
        itself commits to its creator (``federation/owner_bound_id.py``):
        an owner-bound id claimed for anyone else, or for another space, is
        refused on sight, so nobody can announce another household's new
        album first. A legacy (uuid4) id keeps the first-come rule below. An album id held
        here already is only ever a redelivery of that same album: for its
        owner a quiet no-op, for anybody else a refusal. An album deleted
        here (or whose delete overtook this create) is not brought back,
        and the local per-space album limit holds for federated albums too.
        """
        if self._gallery_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        album_id = str(p.get("id") or "")
        owner = str(p.get("owner_user_id") or "")
        name = str(p.get("name") or "").strip()
        if not album_id or not owner or not name or owner == SYSTEM_AUTHOR:
            log.debug("SPACE_GALLERY_ALBUM_CREATED missing or invalid owner / name")
            return
        if not _album_text_ok(name, p.get("description")):
            log.debug("SPACE_GALLERY_ALBUM_CREATED %s over the size limits", album_id)
            return
        binding = check_owner_bound_id(
            GALLERY_ALBUM_KIND, album_id, space_id=space_id, owner_user_id=owner
        )
        if binding is OwnerBinding.MISMATCH:
            log.warning(
                "%s from %s: gallery album id %s is not bound to %r in space "
                "%s — refusing the write",
                event.event_type,
                event.from_instance,
                album_id,
                owner,
                space_id,
            )
            return
        held = await self._gallery_repo.get_album(album_id)
        if held is not None:
            if held.space_id == space_id and held.owner_user_id == owner:
                return  # a redelivery of the album we hold
            log.warning(
                "%s from %s: gallery album %s is already held for another "
                "owner or space here — refusing the write",
                event.event_type,
                event.from_instance,
                album_id,
            )
            return
        if await self._gallery_repo.is_album_deleted(album_id, space_id=space_id):
            log_not_applied(
                event,
                what="gallery album",
                row_id=album_id,
                reason="deleted here already",
            )
            return
        if not await self._authorship.may_author(event, space_id, owner):
            await self._authorship.hold_or_refuse(
                event,
                space_id=space_id,
                what="gallery album",
                row_id=album_id,
                user_id=owner,
            )
            return
        existing = await self._gallery_repo.list_albums(
            space_id, limit=ALBUMS_PER_SPACE + 1
        )
        if len(existing) >= ALBUMS_PER_SPACE:
            log.warning(
                "%s from %s: space %s already holds %d albums — refusing album %s",
                event.event_type,
                event.from_instance,
                space_id,
                ALBUMS_PER_SPACE,
                album_id,
            )
            return
        album = GalleryAlbum(
            id=album_id,
            space_id=space_id,
            owner_user_id=owner,
            name=name,
            description=p.get("description"),
            created_at=p.get("created_at"),
            updated_at=p.get("updated_at"),
        )
        if not await self._gallery_repo.create_album_in_space(album, space_id=space_id):
            log_cross_space_refusal(
                event, space_id=space_id, what="gallery album", row_id=album_id
            )
            return
        if binding is OwnerBinding.LEGACY:
            # The legacy window (v_34): an id minted before the binding,
            # or by a household that does not bind yet, carries no proof
            # of its creator — accepted first-come, as before.
            log.info(
                "%s from %s: gallery album %s has a legacy (unbound) id — "
                "accepted under the first-come rule",
                event.event_type,
                event.from_instance,
                album_id,
            )
        await self._bus.publish(
            GalleryAlbumCreated(
                album_id=album_id,
                space_id=space_id,
                owner_id=owner,
                origin_instance_id=event.from_instance,
            )
        )

    async def _gallery_album_mutable(
        self, event: "FederationEvent", space_id: str, album_id: str
    ) -> bool:
        """The album is a user album of ``space_id`` the sender may change.

        The owner is read from the stored row — never the payload — and the
        rule is the local "owner or space admin" one
        (``GalleryService._require_album_owner_or_admin``) — settings
        authority, so a moderator seat does not rename / delete a whole
        album that is not its own.
        """
        assert self._gallery_repo is not None
        album = await self._gallery_repo.get_album(album_id)
        if album is None:
            log_not_applied(
                event,
                what="gallery album",
                row_id=album_id,
                reason="no such album here",
            )
            return False
        if album.space_id != space_id or album.is_system:
            log_cross_space_refusal(
                event, space_id=space_id, what="gallery album", row_id=album_id
            )
            return False
        owner = album.owner_user_id or ""
        if not await self._authorship.may_mutate(event, space_id, owner, settings=True):
            await self._authorship.hold_or_refuse(
                event,
                space_id=space_id,
                what="gallery album",
                row_id=album_id,
                user_id=owner,
            )
            return False
        return True

    async def _on_gallery_album_updated(self, event: "FederationEvent") -> None:
        """Apply a member's rename / description / cover edit.

        A ``cover_item_id`` of ``None`` clears the cover; a key that is
        absent leaves it as it is.
        """
        if self._gallery_repo is None:
            return
        space_id = resolve_space_id(event)
        p = event.payload
        album_id = str(p.get("id") or "")
        if not space_id or not album_id:
            return
        if not await self._gallery_album_mutable(event, space_id, album_id):
            return
        patch: dict = {}
        name = str(p.get("name") or "").strip()
        if name:
            patch["name"] = name
        if "description" in p:
            patch["description"] = p.get("description")
        if "cover_item_id" in p:
            cover = p.get("cover_item_id")
            patch["cover_item_id"] = str(cover) if cover else None
        if not patch:
            return
        if not _album_text_ok(patch.get("name", ""), patch.get("description")):
            log.debug("SPACE_GALLERY_ALBUM_UPDATED %s over the size limits", album_id)
            return
        if not await self._gallery_repo.update_album_in_space(
            album_id, patch, space_id=space_id
        ):
            log_cross_space_refusal(
                event, space_id=space_id, what="gallery album", row_id=album_id
            )
            return
        await self._bus.publish(
            GalleryAlbumUpdated(
                album_id=album_id,
                space_id=space_id,
                origin_instance_id=event.from_instance,
            )
        )

    async def _may_tombstone(
        self, event: "FederationEvent", space_id: str, album_id: str
    ) -> bool:
        """May this delete of an album not held here yet be remembered?

        Remembering it writes the album's tombstone (migration 0085), which
        refuses its create later — for good, so only for an id owner-bound
        (v_34) to the payload's ``owner_user_id`` in THIS space (album ids
        are global: a tombstone for another space's id, or a legacy unbound
        one, would block a real album here), and only from a household that
        could delete the album once it lands: an admin household (settings
        authority — not a moderator seat), or the owner's own household.
        """
        owner = str(event.payload.get("owner_user_id") or "")
        binding = check_owner_bound_id(
            GALLERY_ALBUM_KIND, album_id, space_id=space_id, owner_user_id=owner
        )
        if binding is not OwnerBinding.VALID:
            log.info(
                "%s from %s: delete of gallery album %s, not held here, names "
                "an id not bound to its owner in space %s — not remembered",
                event.event_type,
                event.from_instance,
                album_id,
                space_id,
            )
            return False
        if await self._authorship.is_admin_household(event, space_id):
            return True
        if await self._authorship.acts_for(event, space_id, owner, any_role=True):
            return True
        log.warning(
            "%s from %s: delete of gallery album %s, not held here, comes "
            "from neither its owner's household nor a moderator — not "
            "remembered",
            event.event_type,
            event.from_instance,
            album_id,
        )
        return False

    async def _on_gallery_album_deleted(self, event: "FederationEvent") -> None:
        """Remove a member's album — and, by cascade, the items in it, with
        their files unless another row still names them.

        A delete for an album not held here yet is remembered, so the
        create it overtook does not bring the album back.
        """
        if self._gallery_repo is None:
            return
        space_id = resolve_space_id(event)
        album_id = str(event.payload.get("id") or "")
        if not space_id or not album_id:
            return
        if (
            await self._gallery_repo.get_album(album_id) is None
            and not await self._gallery_repo.is_album_deleted(
                album_id, space_id=space_id
            )
            and await self._may_tombstone(event, space_id, album_id)
        ):
            await self._gallery_repo.tombstone_album(
                album_id,
                space_id=space_id,
                owner_user_id=str(event.payload.get("owner_user_id") or ""),
                deleted_by=payload_actor(event) or "",
            )
        if not await self._gallery_album_mutable(event, space_id, album_id):
            return
        media = await self._gallery_repo.list_album_media(album_id)
        if not await self._gallery_repo.delete_album_in_space(
            album_id, space_id=space_id, deleted_by=payload_actor(event) or ""
        ):
            log_cross_space_refusal(
                event, space_id=space_id, what="gallery album", row_id=album_id
            )
            return
        await unlink_unreferenced(self._media_dir, self._media_refs, media)
        await self._bus.publish(
            GalleryAlbumDeleted(
                album_id=album_id,
                space_id=space_id,
                origin_instance_id=event.from_instance,
            )
        )

    # ─── Gallery items (§23.119) ─────────────────────────────────────────

    async def _on_gallery_item_saved(self, event: "FederationEvent") -> None:
        """Mirror a remote upload into the local ``gallery_items`` table.

        Carries the thumbnail and the full ``url``
        (``GalleryItem.to_federation_dict``); both files follow over the
        media outbox, and the ``SPACE_MEDIA_BLOB`` scope check accepts only
        files this row names. Only the canonical local ``api/media/<name>``
        shape is stored (``local_media_ref``); anything else stores nothing.
        The item's ``album_id`` must reference a local album row of the
        gated space already (the initial sync, or a
        ``SPACE_GALLERY_ALBUM_CREATED`` ahead of the item, seeds those);
        if it doesn't — unknown album, another space's album, or a
        household album — the write is refused rather than
        auto-creating a stub.
        """
        if self._gallery_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        item_id = str(p.get("id") or p.get("item_id") or "")
        album_id = str(p.get("album_id") or "")
        uploaded_by = str(p.get("uploaded_by") or p.get("uploader") or "")
        item_type = str(p.get("item_type") or "photo")
        if not item_id or not album_id or not uploaded_by:
            log.debug("SPACE_GALLERY_ITEM_* missing required field")
            return
        item = GalleryItem(
            id=item_id,
            album_id=album_id,
            uploaded_by=uploaded_by,
            item_type=item_type,
            url=local_media_ref(p.get("url")) or "",
            thumbnail_url=local_media_ref(p.get("thumbnail_url")) or "",
            width=int(p.get("width") or 0),
            height=int(p.get("height") or 0),
            duration_s=p.get("duration_s"),
            caption=p.get("caption"),
            taken_at=p.get("taken_at"),
            sort_order=int(p.get("sort_order") or 0),
            created_at=p.get("created_at") or p.get("occurred_at"),
        )
        if self._bound_id_refused(
            event, GALLERY_ITEM_KIND, item_id, space_id, uploaded_by
        ):
            return
        if not await self._authorship.may_author(event, space_id, uploaded_by):
            await self._authorship.hold_or_refuse(
                event,
                space_id=space_id,
                what="gallery item",
                row_id=item_id,
                user_id=uploaded_by,
            )
            return
        if await self._gallery_repo.is_item_deleted(item_id, space_id=space_id):
            # Migration 0085: a deleted item's id never comes back (and is
            # not announced as a new upload).
            log_not_applied(
                event,
                what="gallery item",
                row_id=item_id,
                reason="deleted here already",
            )
            return
        is_new = await self._gallery_repo.get_item(item_id) is None
        try:
            if not await self._gallery_repo.create_item_in_space(
                item, space_id=space_id
            ):
                log_cross_space_refusal(
                    event, space_id=space_id, what="gallery album", row_id=album_id
                )
                return
        except Exception as exc:
            # A malformed record — log and drop. (The uploader is never an
            # FK: they may live on another household, migration 0046.)
            # Matches the chunked-sync receiver's tolerance.
            log.debug(
                "SPACE_GALLERY_ITEM_CREATED apply failed item=%s: %s",
                item_id,
                exc,
            )
            return
        if is_new:
            await self._bus.publish(
                GalleryItemUploaded(
                    item_id=item_id,
                    album_id=album_id,
                    item_type=item_type,
                    uploader=uploaded_by,
                    space_id=space_id,
                    origin_instance_id=event.from_instance,
                )
            )

    async def _on_gallery_item_deleted(self, event: "FederationEvent") -> None:
        if self._gallery_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        item_id = str(event.payload.get("id") or event.payload.get("item_id") or "")
        if not item_id:
            return
        item = await self._gallery_repo.get_item(item_id)
        if item is None:
            log_not_applied(
                event, what="gallery item", row_id=item_id, reason="no such item here"
            )
            return
        album = await self._gallery_repo.get_album(item.album_id)
        if album is None or album.space_id != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="gallery item", row_id=item_id
            )
            return
        # Uploader or a space admin (``GalleryService.delete_item``).
        if not await self._authorship.may_mutate(event, space_id, item.uploaded_by):
            await self._authorship.hold_or_refuse(
                event,
                space_id=space_id,
                what="gallery item",
                row_id=item_id,
                user_id=item.uploaded_by,
            )
            return
        # Deletes the item and decrements its album's count in one
        # transaction — only when the item's album lives in the gated
        # space. A duplicate delete from the chunked path finds nothing.
        if not await self._gallery_repo.delete_item_in_space(
            item_id, space_id=space_id, deleted_by=payload_actor(event) or ""
        ):
            log_cross_space_refusal(
                event, space_id=space_id, what="gallery item", row_id=item_id
            )
            return
        # Same rule as ``GalleryService.delete_item``: the files go unless
        # another row still names them.
        await unlink_unreferenced(
            self._media_dir, self._media_refs, [item.url, item.thumbnail_url]
        )
        await self._bus.publish(
            GalleryItemDeleted(
                item_id=item_id,
                album_id=item.album_id,
                space_id=space_id,
                origin_instance_id=event.from_instance,
            )
        )

    # ─── Space zones (§23.8.7) ─────────────────────────────────────────

    async def _on_zone_upserted(self, event: "FederationEvent") -> None:
        """Mirror a remote ``SPACE_ZONE_UPSERTED`` into ``space_zones``.

        Inbound is lenient: a malformed or partial payload is logged
        and dropped rather than raising — by the time we get here the
        envelope signature + replay cache have already passed (§24.11).
        """
        if self._zone_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        zone_id = str(p.get("zone_id") or p.get("id") or "")
        if not zone_id or not p.get("name"):
            log.debug("SPACE_ZONE_UPSERTED missing required field")
            return
        # Same display-data rules as the local API (§23.8.7): the name and
        # colour reach every member's map. Never log the name itself.
        # Coordinates and radius pass the same rules too: finite, in
        # range, truncated to 4 dp (CLAUDE.md GPS rule), 25 m – 50 km.
        try:
            name = validate_zone_name(p.get("name"))
            color = validate_zone_color(p.get("color"))
            latitude = validate_zone_coord(p.get("latitude"), name="latitude", limit=90)
            longitude = validate_zone_coord(
                p.get("longitude"), name="longitude", limit=180
            )
            radius_m = validate_zone_radius(p.get("radius_m"))
        except ValueError as exc:
            log.warning(
                "SPACE_ZONE_UPSERTED from %s: zone %s in space %s refused — %s",
                event.from_instance,
                zone_id[:64],
                space_id,
                exc,
            )
            return
        if not await self._zone_write_allowed(event, space_id, zone_id):
            return
        created_by = str(p.get("created_by") or "")
        if (
            created_by
            and await self._zone_repo.get(zone_id) is None
            and not await self._authorship.may_author(event, space_id, created_by)
        ):
            await self._authorship.hold_or_refuse(
                event,
                space_id=space_id,
                what="zone",
                row_id=zone_id,
                user_id=created_by,
            )
            return
        zone = SpaceZone(
            id=zone_id,
            space_id=space_id,
            name=name,
            latitude=latitude,
            longitude=longitude,
            radius_m=radius_m,
            color=color,
            created_by=created_by,
            created_at=str(
                p.get("created_at") or p.get("updated_at") or "",
            ),
            updated_at=str(
                p.get("updated_at") or p.get("occurred_at") or "",
            ),
        )
        if not await self._zone_repo.upsert(zone, space_id=space_id):
            log_cross_space_refusal(
                event, space_id=space_id, what="zone", row_id=zone_id
            )

    async def _on_zone_deleted(self, event: "FederationEvent") -> None:
        if self._zone_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        zone_id = str(
            event.payload.get("zone_id") or event.payload.get("id") or "",
        )
        if not zone_id:
            return
        if not await self._zone_write_allowed(event, space_id, zone_id):
            return
        actor = payload_actor(event) or str(event.payload.get("deleted_by") or "")
        if not await self._zone_repo.delete(
            zone_id, space_id=space_id, deleted_by=actor
        ):
            log_cross_space_refusal(
                event, space_id=space_id, what="zone", row_id=zone_id
            )

    # ─── Space timetables (v_39) ─────────────────────────────────────────

    async def _on_timetable_upserted(self, event: "FederationEvent") -> None:
        """Apply a remote ``SPACE_TIMETABLE_UPSERTED`` (the whole timetable).

        Checks, in order — each refusal is a WARNING (a replay is DEBUG),
        never an exception out of the handler:

        1. the gated space (:func:`resolve_space_id`);
        2. the timetable parses and passes the domain :func:`validate`
           (schema, bounds, timestamps in 1970–2199, the 96 KiB wire cap);
        3. no assignees — space timetables have none;
        4. an id this household holds lives in this space;
        5. the id is owner-bound to its ``created_by`` in this space
           (timetables are bound from their first release, so any other
           shape is refused) and, for an id new here, that creator is the
           sender's to name;
        6. the editor (``updated_by``) moderates the space from the
           sending household (:meth:`_timetable_write_allowed`);
        7. a deleted id stays deleted;
        8. the version stays clear of the cap and moves at most
           ``MAX_REMOTE_VERSION_JUMP`` past the held copy (no freezing);
        9. last-writer-wins in the repo — an older or replayed version is a
           no-op; a write that lands is published for the realtime layer.
        """
        if self._timetable_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        tt = self._parse_timetable(event, space_id)
        if tt is None:
            return
        if tt.assignees:
            log.warning(
                "%s from %s: timetable %s in space %s names assignees — space "
                "timetables have none; dropping",
                event.event_type,
                event.from_instance,
                tt.id,
                space_id,
            )
            return
        existing = await self._timetable_repo.get(tt.id)
        if existing is not None and existing[0] != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="timetable", row_id=tt.id
            )
            return
        if not self._timetable_id_bound(event, space_id, tt):
            return
        if existing is None and not await self._creator_named_by_sender(
            event, space_id, tt
        ):
            return
        if not await self._timetable_write_allowed(
            event, space_id, tt.id, tt.updated_by or ""
        ):
            return
        if await self._timetable_repo.is_tombstoned(tt.id):
            log_not_applied(
                event, what="timetable", row_id=tt.id, reason="deleted here"
            )
            return
        refusal = remote_version_refusal(
            tt.version, existing[1].version if existing is not None else None
        )
        if refusal is not None:
            log.warning(
                "%s from %s: timetable %s in space %s — %s; dropping",
                event.event_type,
                event.from_instance,
                tt.id,
                space_id,
                refusal,
            )
            return
        if not await self._timetable_repo.apply_remote(tt, space_id=space_id):
            log_not_applied(
                event,
                what="timetable",
                row_id=tt.id,
                reason="not newer than the copy held here",
            )
            return
        await self._bus.publish(
            TimetableSaved(
                timetable=tt,
                space_id=space_id,
                origin_instance_id=event.from_instance,
            )
        )

    async def _on_timetable_deleted(self, event: "FederationEvent") -> None:
        """Tombstone a timetable a space moderator deleted.

        An id this household never saw is tombstoned too, so a create the
        delete overtook can't land afterwards — but only when the id commits
        to the payload's ``created_by`` in THIS space. Otherwise a moderator
        of any space we share could pre-tombstone another space's timetable
        id under its own space and silently eat every later upsert of it
        (the tombstone owns the id), and junk ids would grow the table. A
        replay is a DEBUG no-op; a bad ``deleted_at`` is dropped.
        """
        if self._timetable_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        tt_id = p.get("timetable_id")
        if not isinstance(tt_id, str) or not 0 < len(tt_id) <= _MAX_ROW_ID:
            log.warning(
                "%s from %s: malformed timetable id in space %s — dropping",
                event.event_type,
                event.from_instance,
                space_id,
            )
            return
        existing = await self._timetable_repo.get(tt_id)
        if existing is not None and existing[0] != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="timetable", row_id=tt_id
            )
            return
        deleted_by = p.get("deleted_by")
        if not await self._timetable_write_allowed(
            event,
            space_id,
            tt_id,
            deleted_by if isinstance(deleted_by, str) else "",
        ):
            return
        if await self._timetable_repo.is_tombstoned(tt_id):
            log_not_applied(
                event, what="timetable", row_id=tt_id, reason="already deleted"
            )
            return
        try:
            at = parse_timetable_datetime(p.get("deleted_at"), "deleted_at")
        except TimetableValidationError as exc:
            log.warning(
                "%s from %s: timetable %s in space %s — %s; dropping",
                event.event_type,
                event.from_instance,
                tt_id,
                space_id,
                exc,
            )
            return
        if existing is not None:
            created_by = existing[1].created_by
        else:
            claimed = p.get("created_by")
            created_by = claimed if isinstance(claimed, str) else ""
            if (
                check_owner_bound_id(
                    SPACE_TIMETABLE_KIND,
                    tt_id,
                    space_id=space_id,
                    owner_user_id=created_by,
                )
                is not OwnerBinding.VALID
            ):
                log.warning(
                    "%s from %s: timetable id %s is not bound to %r in space %s "
                    "— no tombstone for an id this space does not own",
                    event.event_type,
                    event.from_instance,
                    tt_id,
                    created_by,
                    space_id,
                )
                return
        if not await self._timetable_repo.tombstone(tt_id, space_id=space_id, at=at):
            log_cross_space_refusal(
                event, space_id=space_id, what="timetable", row_id=tt_id
            )
            return
        await self._bus.publish(
            TimetableDeleted(
                timetable_id=tt_id,
                space_id=space_id,
                origin_instance_id=event.from_instance,
                deleted_by=str(deleted_by),
                created_by=created_by,
            )
        )

    @staticmethod
    def _parse_timetable(event: "FederationEvent", space_id: str) -> Timetable | None:
        """The payload's timetable, parsed and validated — or ``None``
        (logged at WARNING) for anything malformed or out of bounds."""
        raw = event.payload.get("timetable")
        try:
            if not isinstance(raw, dict):
                raise TypeError("timetable must be an object")
            tt = from_wire_dict(raw)
            validate(tt)
        except Exception as exc:  # hostile input: never raise out of here
            log.warning(
                "%s from %s: unusable timetable in space %s — dropping: %.200s",
                event.event_type,
                event.from_instance,
                space_id,
                exc,
            )
            return None
        return tt

    @staticmethod
    def _timetable_id_bound(
        event: "FederationEvent", space_id: str, tt: Timetable
    ) -> bool:
        """Every space timetable id commits to its creator in its space
        (they are owner-bound from their first release): any other shape,
        or a commitment to anyone / anywhere else, is refused."""
        binding = check_owner_bound_id(
            SPACE_TIMETABLE_KIND, tt.id, space_id=space_id, owner_user_id=tt.created_by
        )
        if binding is OwnerBinding.VALID:
            return True
        log.warning(
            "%s from %s: timetable id %s is not bound to %r in space %s "
            "— refusing the write",
            event.event_type,
            event.from_instance,
            tt.id,
            tt.created_by,
            space_id,
        )
        return False

    async def _creator_named_by_sender(
        self, event: "FederationEvent", space_id: str, tt: Timetable
    ) -> bool:
        """A timetable new to this household names a creator the sender
        speaks for (its own seated user, or the host relaying a remote one)."""
        if await self._authorship.may_author(event, space_id, tt.created_by):
            return True
        await self._authorship.hold_or_refuse(
            event,
            space_id=space_id,
            what="timetable",
            row_id=tt.id,
            user_id=tt.created_by,
        )
        return False

    # ─── Bazaar listings ─────────────────────────────────────────────────

    async def _on_bazaar_listing_created(self, event: "FederationEvent") -> None:
        """Mirror a remote bazaar listing into the local ``bazaar_listings``
        table.

        The wrapper ``PostType.BAZAAR`` post must already exist on the
        receiver — ``SPACE_POST_CREATED`` ships before this event in
        the federation outbox queue. If the post hasn't landed yet
        (out-of-order delivery or chunked-sync race), the FK to
        ``space_posts.id`` fails and we log + drop; a subsequent
        catch-up sync will retry the enqueue.

        Idempotent: a repeat delivery for the same listing simply
        overwrites the row via the repo's UPSERT path. Reactions /
        comments are stored separately so no data is lost on replay.
        """
        if self._bazaar_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        post_id = str(p.get("post_id") or "")
        seller_user_id = str(p.get("seller_user_id") or "")
        mode_raw = str(p.get("mode") or "")
        status_raw = str(p.get("status") or "active")
        title = str(p.get("title") or "")
        if not post_id or not seller_user_id or not mode_raw:
            log.debug(
                "BAZAAR_LISTING_CREATED missing required field: %s",
                p,
            )
            return
        try:
            mode = BazaarMode(mode_raw)
            status = BazaarStatus(status_raw)
        except ValueError:
            log.debug(
                "BAZAAR_LISTING_CREATED unknown mode/status: mode=%r status=%r",
                mode_raw,
                status_raw,
            )
            return
        listing = BazaarListing(
            post_id=post_id,
            space_id=space_id,
            seller_user_id=seller_user_id,
            mode=mode,
            title=title,
            end_time=str(p.get("end_time") or ""),
            currency=str(p.get("currency") or "USD"),
            status=status,
            created_at=str(
                p.get("created_at") or p.get("occurred_at") or "",
            ),
            description=p.get("description"),
            image_urls=local_media_refs(p.get("image_urls"), limit=BAZAAR_MAX_IMAGES),
            price=p.get("price"),
            start_price=p.get("start_price"),
            step_price=p.get("step_price"),
            winner_user_id=p.get("winner_user_id"),
            winning_price=p.get("winning_price"),
            sold_at=p.get("sold_at"),
        )
        # The listing hangs off the seller's own wrapper post.
        anchor = await self._post_in_space(
            event, space_id, post_id, what="bazaar listing"
        )
        if anchor is None:
            return
        if await self._bazaar_repo.get_listing(post_id) is not None:
            # A listing is created once; its status then only moves through
            # ``BAZAAR_LISTING_UPDATED`` / ``BAZAAR_OFFER_ACCEPTED``. A re-send
            # would reset the status (a sold listing back to active) and
            # could hand the listing to another seller.
            log_not_applied(
                event,
                what="bazaar listing",
                row_id=post_id,
                reason="listing already here",
            )
            return
        if seller_user_id != anchor.author or not await self._authorship.may_author(
            event, space_id, seller_user_id
        ):
            await self._authorship.hold_or_refuse(
                event,
                space_id=space_id,
                what="bazaar listing",
                row_id=post_id,
                user_id=seller_user_id,
            )
            return
        try:
            if not await self._bazaar_repo.save_listing(listing, space_id=space_id):
                # The wrapper post is not in this space (or has not landed
                # yet), or the listing id belongs to another space.
                log_cross_space_refusal(
                    event, space_id=space_id, what="bazaar listing", row_id=post_id
                )
        except Exception as exc:
            # CHECK failure (unknown mode/status from a future peer), or
            # any other repo error. Log + drop — the catch-up enqueue at
            # the next §25.6 sync picks this up again.
            log.debug(
                "BAZAAR_LISTING_CREATED apply failed listing=%s: %s",
                post_id,
                exc,
            )

    async def _on_bazaar_listing_updated(self, event: "FederationEvent") -> None:
        """Apply a status-only mutation (SOLD / EXPIRED / CANCELLED) to an
        existing ``bazaar_listings`` row.

        Routes by ``status``:

        * ``sold``      → :meth:`AbstractBazaarRepo.mark_sold` with
          winner_user_id + winning_price from the payload.
        * ``expired``   → :meth:`mark_expired`.
        * ``cancelled`` → :meth:`mark_cancelled`.

        All three repo methods are gated on the row's CURRENT status
        being ``active`` — replayed events or out-of-order deliveries
        for a row already in a terminal state are silent no-ops.
        """
        if self._bazaar_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        post_id = str(p.get("post_id") or "")
        status_raw = str(p.get("status") or "")
        if not post_id or not status_raw:
            log.debug("BAZAAR_LISTING_UPDATED missing required field: %s", p)
            return
        # The mutators below are scoped to ``space_id`` in SQL regardless;
        # this read only separates a cross-space attempt (WARNING) from a
        # benign replay against a terminal state (silent). It also hands
        # the follow-up seller-ownership check the row it needs.
        listing = await self._listing_in_space(event, post_id, space_id)
        if listing is None:
            return
        if listing.status is not BazaarStatus.ACTIVE:
            log_not_applied(
                event,
                what="bazaar listing",
                row_id=post_id,
                reason=f"already {listing.status.value}",
            )
            return
        # Only the seller settles their listing (sold / expired / cancelled
        # are all driven from the seller's household).
        if not await self._authorship.acts_for(event, space_id, listing.seller_user_id):
            if status_raw in ("expired", "sold") and _has_ended(listing.end_time):
                # Every household runs the expiry sweep over the listings it
                # mirrors; an older one still announces its own result. Each
                # applies the expiry locally anyway — this is noise, not an
                # attack on the listing.
                log_not_applied(
                    event,
                    what="bazaar listing",
                    row_id=post_id,
                    reason="expiry is announced by the seller's household",
                )
                return
            await self._authorship.hold_or_refuse(
                event,
                space_id=space_id,
                what="bazaar listing",
                row_id=post_id,
                user_id=listing.seller_user_id,
            )
            return
        try:
            if status_raw == "sold":
                winner = str(p.get("winner_user_id") or "")
                price_raw = p.get("winning_price")
                if not winner or price_raw is None:
                    log.debug("BAZAAR_LISTING_UPDATED sold missing winner/price: %s", p)
                    return
                await self._bazaar_repo.mark_sold(
                    post_id,
                    space_id=space_id,
                    winner_user_id=winner,
                    winning_price=int(price_raw),
                )
            elif status_raw == "expired":
                await self._bazaar_repo.mark_expired(post_id, space_id=space_id)
            elif status_raw == "cancelled":
                await self._bazaar_repo.mark_cancelled(post_id, space_id=space_id)
            else:
                log.debug(
                    "BAZAAR_LISTING_UPDATED unknown status %r — skipping",
                    status_raw,
                )
        except Exception as exc:
            # ``mark_sold`` raises ValueError when the listing isn't
            # in the active state (replay / out-of-order); log + drop.
            log.debug(
                "BAZAAR_LISTING_UPDATED apply failed listing=%s: %s",
                post_id,
                exc,
            )

    async def _on_bazaar_bid_placed(self, event: "FederationEvent") -> None:
        """F7: persist a bid placed on a remote bidder's instance.

        The bid lives canonically on the seller's host but every member
        household mirrors the row so the SPA can render "current high
        bid" consistently. Idempotent — a replay matching an existing
        bid_id is dropped.
        """
        if self._bazaar_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        p = event.payload
        bid_id = str(p.get("bid_id") or "")
        listing_post_id = str(p.get("listing_post_id") or "")
        bidder = str(p.get("bidder_user_id") or "")
        amount_raw = p.get("amount")
        if not bid_id or not listing_post_id or not bidder or amount_raw is None:
            log.debug("BAZAAR_BID_PLACED missing required field: %s", p)
            return
        # Already-present? Drop silently.
        try:
            existing = await self._bazaar_repo.get_bid(bid_id)
        except Exception:
            existing = None
        if existing is not None:
            return
        if await self._listing_in_space(event, listing_post_id, space_id) is None:
            return
        if not await self._acts_for(event, space_id, bidder, "bid", bid_id):
            return
        try:
            await self._bazaar_repo.place_bid(
                BazaarBid(
                    id=bid_id,
                    listing_post_id=listing_post_id,
                    bidder_user_id=bidder,
                    amount=int(amount_raw),
                    # Never the sender's clock: created_at is the
                    # highest_bid tie-break, so a payload timestamp would
                    # let a household backdate its bid. The repo stamps
                    # arrival time for a falsy value (bazaar_repo.place_bid).
                    created_at="",
                    message=p.get("message"),
                ),
                space_id=space_id,
            )
        except Exception as exc:
            # Listing not yet persisted (race against F4 catch-up) or
            # the listing is no longer active — drop; the seller's
            # state of truth wins. The catch-up enqueue will eventually
            # re-converge if a sync runs.
            log.debug(
                "BAZAAR_BID_PLACED apply failed bid=%s listing=%s: %s",
                bid_id,
                listing_post_id,
                exc,
            )

    async def _on_bazaar_offer_accepted(
        self,
        event: "FederationEvent",
    ) -> None:
        """F7: mirror an offer acceptance on the bidder + every other
        member's local row.

        ``accept_offer`` flips ``accepted=1`` for the row; the matching
        F8 BAZAAR_LISTING_UPDATED handler will fire separately and
        mark the listing sold via ``mark_sold``. Both are idempotent
        so the order doesn't matter.
        """
        if self._bazaar_repo is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        bid_id = str(event.payload.get("bid_id") or "")
        if not bid_id:
            log.debug("BAZAAR_OFFER_ACCEPTED missing bid_id: %s", event.payload)
            return
        bid = await self._bazaar_repo.get_bid(bid_id)
        if bid is None:
            log.debug("BAZAAR_OFFER_ACCEPTED unknown bid %s", bid_id)
            return
        listing = await self._listing_in_space(event, bid.listing_post_id, space_id)
        if listing is None:
            return
        # Only the seller accepts an offer (``BazaarService.accept_offer``).
        if not await self._acts_for(
            event, space_id, listing.seller_user_id, "bazaar offer", bid_id
        ):
            return
        try:
            await self._bazaar_repo.accept_offer(bid_id, space_id=space_id)
        except Exception as exc:
            log.debug(
                "BAZAAR_OFFER_ACCEPTED apply failed bid=%s: %s",
                bid_id,
                exc,
            )

    async def _listing_in_space(
        self,
        event: "FederationEvent",
        post_id: str,
        space_id: str,
    ) -> BazaarListing | None:
        """The bazaar listing ``post_id`` when it lives in ``space_id``.

        ``None`` (logged) otherwise: unknown here is DEBUG — out-of-order
        delivery — another space is the cross-space WARNING. Not the space
        boundary itself (every bazaar mutator is scoped in SQL); it hands
        the seller to the ownership checks and separates the log levels.
        """
        assert self._bazaar_repo is not None
        listing = await self._bazaar_repo.get_listing(post_id)
        if listing is None:
            log_not_applied(
                event,
                what="bazaar listing",
                row_id=post_id,
                reason="no such listing here",
            )
            return None
        if listing.space_id != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="bazaar listing", row_id=post_id
            )
            return None
        return listing

    # ─── Authorship helpers ──────────────────────────────────────────────

    async def _acts_for(
        self,
        event: "FederationEvent",
        space_id: str,
        user_id: str,
        what: str,
        row_id: str,
    ) -> bool:
        """Strict rule — ``user_id`` is seated on the sender (logged if not)."""
        if await self._authorship.acts_for(event, space_id, user_id):
            return True
        await self._authorship.hold_or_refuse(
            event, space_id=space_id, what=what, row_id=row_id, user_id=user_id
        )
        return False

    async def _rsvp_allowed(
        self,
        event: "FederationEvent",
        space_id: str,
        event_id: str,
        user_id: str,
        occurrence_at: str,
        new_status: str,
    ) -> bool:
        """May the sender set ``user_id``'s RSVP to ``new_status``?

        The user's own household always may. Two transitions of somebody
        ELSE's RSVP are legitimate too, because the calendar service makes
        them on another household's behalf (``SpaceCalendarService``):

        * **settling a request** on a capacity-limited event —
          ``requested`` → ``going`` / ``waitlist`` (approve) or → removed
          (deny) — by the event creator's household or a moderator, the
          federated form of the route's "event creator or space admin"
          gate;
        * **waitlist promotion** — ``waitlist`` → ``going`` when a seat is
          free here — by any writer household, since the household whose
          member dropped out is the one that promotes
          (``_auto_promote_waitlist``).

        Anything else naming somebody else's member is refused.
        """
        if await self._authorship.acts_for(event, space_id, user_id):
            return True
        got = await self._calendar_repo.get_event(event_id)
        if got is not None and got[0] == space_id:
            cal = got[1]
            current = next(
                (
                    r.status
                    for r in await self._calendar_repo.list_rsvps(
                        event_id, occurrence_at=occurrence_at
                    )
                    if r.user_id == user_id
                ),
                None,
            )
            if current == RSVPStatus.REQUESTED and new_status in (
                RSVPStatus.GOING,
                RSVPStatus.WAITLIST,
                "removed",
            ):
                if await self._authorship.acts_for(
                    event, space_id, cal.created_by
                ) or await self._authorship.has_content_authority(event, space_id):
                    return True
            if (
                current == RSVPStatus.WAITLIST
                and new_status == RSVPStatus.GOING
                and cal.capacity is not None
            ):
                going = sum(
                    1
                    for r in await self._calendar_repo.list_rsvps(
                        event_id, occurrence_at=occurrence_at
                    )
                    if r.status == RSVPStatus.GOING
                )
                if going < cal.capacity and await self._authorship.writes_here(
                    event, space_id
                ):
                    return True
        await self._authorship.hold_or_refuse(
            event, space_id=space_id, what="RSVP", row_id=event_id, user_id=user_id
        )
        return False

    async def _post_in_space(
        self,
        event: "FederationEvent",
        space_id: str,
        post_id: str,
        *,
        what: str,
    ) -> Post | None:
        """The space post ``post_id`` when it lives in ``space_id`` (logged
        otherwise — unknown is DEBUG, another space is WARNING)."""
        got = await self._post_repo.get(post_id)
        if got is None:
            log_not_applied(
                event, what=what, row_id=post_id, reason="no such post here"
            )
            return None
        row_space, post = got
        if row_space != space_id:
            log_cross_space_refusal(event, space_id=space_id, what=what, row_id=post_id)
            return None
        return post

    async def _post_owner_acts(
        self,
        event: "FederationEvent",
        space_id: str,
        post_id: str,
        *,
        what: str,
    ) -> bool:
        """Owner-only state change on a post (close / finalise a poll)."""
        post = await self._post_in_space(event, space_id, post_id, what=what)
        if post is None:
            return False
        return await self._acts_for(event, space_id, post.author, what, post_id)

    @staticmethod
    def _bound_id_refused(
        event: "FederationEvent",
        kind: str,
        row_id: str,
        space_id: str,
        owner_user_id: str,
    ) -> bool:
        """A new row's owner-bound id (v_36) names somebody else — refuse.

        Only a row we do not hold yet is checked: an edit of a collaborative
        row keeps the stored attribution whatever the payload claims.
        """
        return owner_bound_id_refused(
            kind,
            row_id,
            space_id=space_id,
            owner_user_id=owner_user_id,
            context=f"{event.event_type} from {event.from_instance}",
        )

    async def _collaborative_write_allowed(
        self,
        event: "FederationEvent",
        space_id: str,
        *,
        what: str,
        row_id: str,
        feature: str,
        action: ContentAction,
        claimed_author: str = "",
        row_owner: str = "",
    ) -> bool:
        """Tasks / task lists / pages / stickies / calendar events: any
        writer household edits; a new row's claimed author must be the
        sender's (or relayed by the host). Then the space's ``feature``
        access level must admit the payload's ``actor_user_id`` (v_42,
        :meth:`SpaceAuthorship.access_admits`)."""
        if not await self._authorship_allows(
            event, space_id, what=what, row_id=row_id, claimed_author=claimed_author
        ):
            return False
        return await self._authorship.access_admits(
            event,
            space_id,
            feature,
            action,
            actor=payload_actor(event),
            row_owner=row_owner,
        )

    async def _authorship_allows(
        self,
        event: "FederationEvent",
        space_id: str,
        *,
        what: str,
        row_id: str,
        claimed_author: str,
    ) -> bool:
        if claimed_author:
            if await self._authorship.may_author(event, space_id, claimed_author):
                return True
            await self._authorship.hold_or_refuse(
                event,
                space_id=space_id,
                what=what,
                row_id=row_id,
                user_id=claimed_author,
            )
            return False
        if await self._authorship.writes_here(event, space_id):
            return True
        log.warning(
            "%s from %s: %s %s in space %s — the sending household holds no "
            "writer seat here; refusing the write",
            event.event_type,
            event.from_instance,
            what,
            row_id,
            space_id,
        )
        return False

    async def _zone_write_allowed(
        self,
        event: "FederationEvent",
        space_id: str,
        zone_id: str,
    ) -> bool:
        """Zones are admin-only locally (``SpaceZoneService``): the host or
        a household holding a live admin seat."""
        if await self._authorship.is_admin_household(event, space_id):
            return True
        log.warning(
            "%s from %s: zone %s in space %s — the sending household does not "
            "moderate this space; refusing the write",
            event.event_type,
            event.from_instance,
            zone_id,
            space_id,
        )
        return False

    async def _timetable_write_allowed(
        self,
        event: "FederationEvent",
        space_id: str,
        timetable_id: str,
        user_id: str,
    ) -> bool:
        """Timetables are owner / admin-only locally
        (``SpaceTimetableService``): the sending household must moderate the
        space (the host, or a live admin seat), and the user the write is
        recorded as must be a moderator seated on it — never a plain member
        of an admin household, another household's admin, or a banned user
        (:meth:`SpaceAuthorship.admin_as`)."""
        if not await self._authorship.is_admin_household(event, space_id):
            log.warning(
                "%s from %s: timetable %s in space %s — the sending household "
                "does not moderate this space; refusing the write",
                event.event_type,
                event.from_instance,
                timetable_id,
                space_id,
            )
            return False
        if await self._authorship.admin_as(event, space_id, user_id):
            return True
        log.warning(
            "%s from %s: timetable %s in space %s is recorded as %r, who is "
            "not a moderator seated on the sending household — refusing the "
            "write",
            event.event_type,
            event.from_instance,
            timetable_id,
            space_id,
            user_id,
        )
        return False


#: Upper bound on a row id read from a delete payload (the domain's id cap).
_MAX_ROW_ID = 64


def _album_text_ok(name: str, description: object) -> bool:
    """The limits ``GalleryService`` enforces on a local album, for the wire."""
    if len(name) > NAME_MAX:
        return False
    if description is None:
        return True
    return isinstance(description, str) and len(description) <= DESCRIPTION_MAX


def _assignee_owner(existing: Task, incoming: Task, actor: str | None) -> str:
    """The owner an inbound task edit is judged against: the held row's
    creator — or the named actor, when they are one of the HELD row's
    assignees and the edit changes nothing but status / position (the
    local rule, ``SpaceTaskService.update_task``)."""
    if (
        actor
        and actor in (existing.assignees or ())
        and replace(
            incoming,
            status=existing.status,
            position=existing.position,
            updated_at=existing.updated_at,
        )
        == existing
    ):
        return actor
    return existing.created_by


def _deleter(event: "FederationEvent") -> str:
    """Who authorised a delete, for a list, task or page tombstone's
    ``deleted_by``: the approver of a reviewed (v_43) delete, else the
    payload's actor (v_42), else nobody. A replay of the tombstone names this user, so it passes
    the space's level wherever the live delete did."""
    block = event.payload.get(MODERATION_BLOCK_KEY)
    if isinstance(block, dict):
        approver = block.get("approved_by")
        if isinstance(approver, str) and approver:
            return approver
    return payload_actor(event) or ""
