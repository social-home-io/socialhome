"""Outbound federation for space-scoped pages (§13 / §14).

Subscribes to :class:`PageCreated` / :class:`PageUpdated` /
:class:`PageDeleted` domain events. When the event carries a
``space_id``, broadcasts the matching ``SPACE_PAGE_*`` federation
event to every member household via mesh-routed
``broadcast_to_space_members``. Without this service, page edits
stayed purely local — the inbound handler in
:mod:`federation_inbound.space_content` existed but had no
matching outbound producer, so a remote member saw stale wiki
content until the next §25.6 catch-up sync.

Household-scoped pages (``space_id is None``) stay local — no peer
has a right to know about them.

Every payload carries ``actor_user_id`` (v_42) — who made the write, which
receivers check against the space's ``pages`` access level — and a create
also its ``created_by`` (the same user): the page id is owner-bound to its
creator, so a receiver can only file a new page it can attribute. A write
released from the moderation queue adds the ``moderation`` approval block
(v_43, :mod:`.moderation_release`).

Pages are host-sequenced (v_48): the host's canonical versions carry
``seq``, ``version_hash``, ``conflict`` and ``sequenced`` to member
households at v_48 or above (older ones get the plain fields); a member
household's own drafts are ``proposal`` events this bridge never sends —
:class:`~.page_proposal_forwarder.PageProposalForwarder` proposes them to
the host alone.

A page's files (v_81) — its cover and the pictures its body shows — ride
the host's versions: each version's NEW files (``shown_before`` names the
ones the replaced version showed) are queued on the space media outbox for
every other member household at v_81 (paired or mesh; correlated by the
page), and a link-joined v_81 member gets them as a ``media_refs``
descriptor in its own copy (:class:`SpaceMediaRefsMixin`). The page keeps
the names the host's page shows: a receiver lets only the host fill them.

Sibling of :class:`TaskFederationOutbound` /
:class:`StickyFederationOutbound`; identical shape, different
event types.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from ..domain.events import PageCreated, PageDeleted, PageUpdated
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.space_media_ref import MEDIA_REFS_FIELD, page_media_urls
from ..infrastructure.event_bus import EventBus
from .inbound_media_store import media_basename
from .moderation_release import with_release
from .space_media_refs import SpaceMediaRefsMixin

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.space_repo import AbstractSpaceRepo
    from .space_media_gfs_uploader import SpaceMediaPlan
    from .space_media_sync_service import SpaceMediaSyncService

log = logging.getLogger(__name__)


class PageFederationOutbound(SpaceMediaRefsMixin):
    """Publish space-scoped page mutations to paired peer instances."""

    __slots__ = ("_bus", "_federation", "_media_gfs", "_media_sync", "_spaces")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: FederationService,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._media_sync: SpaceMediaSyncService | None = None
        self._spaces: AbstractSpaceRepo | None = None
        self._media_gfs = None

    def attach_media(
        self, *, media_sync: SpaceMediaSyncService, space_repo: AbstractSpaceRepo
    ) -> None:
        """Wire the space media outbox (v_81: a version's new files)."""
        self._media_sync = media_sync
        self._spaces = space_repo

    def wire(self) -> None:
        self._bus.subscribe(PageCreated, self._on_created)
        self._bus.subscribe(PageUpdated, self._on_updated)
        self._bus.subscribe(PageDeleted, self._on_deleted)

    async def _on_created(self, event: PageCreated) -> None:
        if event.space_id is None or event.proposal:
            return  # household page / a member's draft (the forwarder's)

        def _payload() -> dict:
            return _with_actor(
                {
                    "id": event.page_id,
                    "page_id": event.page_id,
                    "space_id": event.space_id,
                    "title": event.title,
                    "content": event.content,
                },
                event.actor_user_id,
                creates=True,
            )

        def _with_base() -> dict:
            return {**_payload(), **(event.base or {})}

        await self._fan_out_versioned(
            event.space_id,
            FederationEventType.SPACE_PAGE_CREATED,
            _with_base if event.canonical is None else _payload,
            event.canonical,
            page_id=event.page_id,
            content=event.content,
            shown_before=event.shown_before,
        )

    async def _on_updated(self, event: PageUpdated) -> None:
        if event.space_id is None or event.proposal:
            return

        def _payload() -> dict:
            return _with_actor(
                {
                    "id": event.page_id,
                    "page_id": event.page_id,
                    "space_id": event.space_id,
                    "title": event.title,
                    "content": event.content,
                },
                event.actor_user_id,
            )

        def _with_base() -> dict:
            return {**_payload(), **(event.base or {})}

        await self._fan_out_versioned(
            event.space_id,
            FederationEventType.SPACE_PAGE_UPDATED,
            _with_base if event.canonical is None else _payload,
            event.canonical,
            page_id=event.page_id,
            content=event.content,
            shown_before=event.shown_before,
        )

    async def _fan_out_versioned(
        self,
        space_id: str,
        event_type: FederationEventType,
        payload: Callable[[], dict],
        canonical: dict | None,
        *,
        page_id: str,
        content: str,
        shown_before: Sequence[str],
    ) -> None:
        """A host's canonical version (v_48) carries its ``seq``, hash and
        conflict list to v_48 members, and the plain fields to older ones
        (``legacy_payload``: their release check refuses unknown keys).
        Anything else — a write under a pre-v_48 host — goes out as before.
        A canonical version's new files follow it (v_81)."""
        if canonical is None:
            await self._fan_out(space_id, event_type, payload())
            return
        new_files = _new_files(
            page_media_urls(canonical.get("cover_image_url"), content), shown_before
        )
        plan = (
            await self._plan_media_refs(
                space_id, new_files, item_id=page_id, followers=False
            )
            if new_files
            else None
        )
        base = payload()
        await self._fan_out(
            space_id,
            event_type,
            {**base, **canonical},
            legacy_payload=payload(),
            legacy_below=FederationCapability.MIN_FOR_HOST_SEQUENCED_PAGES,
            per_peer=self._page_refs_per_peer(plan),
        )
        self._upload_media(plan)
        await self._ship_files(space_id, page_id, new_files)

    def _page_refs_per_peer(
        self, plan: SpaceMediaPlan | None
    ) -> Callable[[str, dict], Awaitable[dict]] | None:
        """The descriptor for a link-joined member at v_81 only — an older
        one would keep it under a reader name the page never shows."""
        if plan is None or self._media_gfs is None:
            return None
        media_gfs = self._media_gfs
        fed = self._federation
        wire = plan.wire()

        async def _per_peer(instance_id: str, payload: dict) -> dict:
            if await media_gfs.is_relay_reader(
                instance_id
            ) and await fed.space_member_supports(
                instance_id, min_version=FederationCapability.MIN_FOR_PAGE_MEDIA
            ):
                return {**payload, MEDIA_REFS_FIELD: wire}
            return payload

        return _per_peer

    async def _ship_files(
        self, space_id: str, page_id: str, media_urls: list[str]
    ) -> None:
        """Queue a version's new files for every other member household at
        v_81 — paired or mesh-only (the outbox reaches both; a link-joined
        member is left out by the outbox itself). One outbox row per (file,
        household). Fail-soft: the version has gone out already."""
        if not media_urls or self._media_sync is None or self._spaces is None:
            return
        own = str(self._federation.own_instance_id or "")
        try:
            targets = [
                iid
                for iid in await self._spaces.list_member_instances(space_id)
                if iid
                and iid != own
                and await self._federation.space_member_supports(
                    iid, min_version=FederationCapability.MIN_FOR_PAGE_MEDIA
                )
            ]
            if targets:
                await self._media_sync.enqueue_for_blob(
                    space_id=space_id,
                    correlation_id=page_id,
                    target_instance_ids=targets,
                    media_urls=media_urls,
                )
        except Exception:
            log.exception(
                "page-outbound: files of page %s in space %s not queued",
                page_id,
                space_id,
            )

    async def _on_deleted(self, event: PageDeleted) -> None:
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_PAGE_DELETED,
            _with_actor(
                {
                    "id": event.page_id,
                    "page_id": event.page_id,
                    "space_id": event.space_id,
                },
                event.actor_user_id,
            ),
        )

    async def _fan_out(
        self,
        space_id: str,
        event_type: FederationEventType,
        payload: dict,
        *,
        legacy_payload: dict | None = None,
        legacy_below: int | None = None,
        per_peer: Callable[[str, dict], Awaitable[dict]] | None = None,
    ) -> None:
        extra: dict = {"per_peer": per_peer} if per_peer is not None else {}
        try:
            if legacy_payload is None:
                await self._federation.broadcast_to_space_members(
                    space_id, event_type, payload
                )
            else:
                await self._federation.broadcast_to_space_members(
                    space_id,
                    event_type,
                    payload,
                    legacy_payload=legacy_payload,
                    legacy_below=legacy_below,
                    **extra,
                )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "page-outbound: broadcast failed for space=%s: %s",
                space_id,
                exc,
                exc_info=True,
            )


def _new_files(urls: list[str], shown_before: Sequence[str]) -> list[str]:
    """*urls* minus the files *shown_before* names (however spelled)."""
    had = {media_basename(u) for u in shown_before}
    return [u for u in urls if media_basename(u) not in had]


def _with_actor(payload: dict, actor_user_id: str, *, creates: bool = False) -> dict:
    """``payload`` plus the write's actor (v_42) — and, for a create, the
    creator it is attributed to — when known, and the approval block of a
    moderation release (v_43)."""
    if actor_user_id:
        payload["actor_user_id"] = actor_user_id
        if creates:
            payload["created_by"] = actor_user_id
    return with_release(payload)
