"""What every per-resource part of :class:`~.receiver.SpaceSyncReceiver`
shares: the slots they read (:class:`ReceiverBase`), who counts as the
host, the live authorship rules a member household's record passes,
the v_80 media names, and the module helpers of more than one resource.
"""

from __future__ import annotations

import logging
import pathlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from ....domain.federation import FederationEvent, FederationEventType
from ....domain.file_hashes import FileHash, parse_file_hashes
from ....domain.space import ContentAction
from ....domain.space_media_ref import MEDIA_REFS_FIELD, SpaceMediaLocalization
from ....infrastructure.event_bus import EventBus
from ....services.space_media_names import SpaceMediaNamer
from ...owner_bound_id import OwnerBinding, check_owner_bound_id
from .stream_health import HeldBack

if TYPE_CHECKING:
    from ....federation.space_authorship import SpaceAuthorship
    from ....repositories.bazaar_repo import AbstractBazaarRepo
    from ....repositories.calendar_repo import AbstractSpaceCalendarRepo
    from ....repositories.gallery_repo import AbstractGalleryRepo
    from ....repositories.media_reference_repo import AbstractMediaReferenceRepo
    from ....repositories.page_repo import AbstractPageRepo
    from ....repositories.profile_picture_repo import (
        AbstractProfilePictureRepo,
    )
    from ....repositories.space_poll_repo import AbstractSpacePollRepo
    from ....repositories.space_post_repo import AbstractSpacePostRepo
    from ....repositories.space_repo import AbstractSpaceRepo
    from ....repositories.space_zone_repo import AbstractSpaceZoneRepo
    from ....repositories.sticky_repo import AbstractStickyRepo
    from ....repositories.task_repo import AbstractSpaceTaskRepo
    from ....repositories.timetable_repo import AbstractSpaceTimetableRepo
    from ....services.legacy_album_deletes import LegacyAlbumDeletes
    from ....services.page_conflict_service import PageConflictService

log = logging.getLogger(__name__)

#: Sync pages Phase 3: ``(space_id, deliverer)`` while
#: :meth:`~.receiver.SpaceSyncReceiver.apply_verified` applies ONE row that
#: was checked against the space authority's signed page — the row is the
#: signer's own, so it passes where a host's record passes
#: (:meth:`ReceiverBase._acts_as_host`), for that space and deliverer and
#: nowhere else. Task-local (a context variable), so a concurrent stream of
#: the same household is never taken as verified.
VERIFIED_ROW: ContextVar[tuple[str, str] | None] = ContextVar(
    "space_sync_verified_row", default=None
)


class _GfsMediaSink(Protocol):
    """Where a streamed record's ``media_refs`` go (v_80) — the GFS media
    fetcher: names the record's files for this household, keeps the rows."""

    async def localize(
        self,
        space_id: str,
        raw_refs: object,
        media_urls: list[str],
        *,
        item_id: str,
        origin: str,
        hashes: Mapping[str, FileHash] | None = None,
    ) -> SpaceMediaLocalization: ...

    def localize_as_shown(
        self,
        space_id: str,
        raw_refs: object,
        media_urls: list[str],
        *,
        item_id: str,
        origin: str,
    ) -> SpaceMediaLocalization: ...

    async def keep(self, loc: SpaceMediaLocalization) -> Any: ...


def _parse_iso(value: Any) -> datetime:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(UTC)


def _sync_event(space_id: str, resource: str, provider: str) -> FederationEvent:
    """The stand-in envelope a §25.6 record is judged under: the live-event
    authorship rules (``SpaceAuthorship``) read its ``from_instance`` — the
    provider household the chunk came from — and its ``space_id``."""
    return FederationEvent(
        msg_id=f"sync:{space_id}:{resource}",
        event_type=FederationEventType.SPACE_SYNC_CHUNK,
        from_instance=provider,
        to_instance="",
        timestamp="",
        payload={},
        space_id=space_id,
    )


class ReceiverBase:
    """The slots and cross-resource helpers of
    :class:`~.receiver.SpaceSyncReceiver`. Behaviour only: the receiver
    declares and fills the slots; the per-resource mixins read them
    through the annotations here."""

    __slots__ = ()

    _authorship: SpaceAuthorship | None
    _bazaar_repo: AbstractBazaarRepo | None
    _bus: EventBus
    _foreign_system_albums: OrderedDict[str, None]
    _gallery_repo: AbstractGalleryRepo
    _gfs_media: _GfsMediaSink | None
    _legacy_album_deletes: LegacyAlbumDeletes | None
    _media_dir: pathlib.Path | None
    _media_names: SpaceMediaNamer
    _media_refs: AbstractMediaReferenceRepo | None
    _page_conflicts: PageConflictService | None
    _page_repo: AbstractPageRepo
    _peer_version: Callable[[str], Awaitable[int]] | None
    _poll_repo: AbstractSpacePollRepo | None
    _profile_picture_repo: AbstractProfilePictureRepo | None
    _space_calendar_repo: AbstractSpaceCalendarRepo
    _space_post_repo: AbstractSpacePostRepo
    _space_repo: AbstractSpaceRepo
    _space_task_repo: AbstractSpaceTaskRepo
    _sticky_repo: AbstractStickyRepo
    _timetable_repo: AbstractSpaceTimetableRepo | None
    _zone_repo: AbstractSpaceZoneRepo | None

    async def _localize_record(
        self,
        space_id: str,
        record: dict[str, Any],
        item_id: str,
        urls: list[str],
        *,
        provider: str,
        takes_descriptor: bool,
        held: list[str] | None = None,
        from_host: bool = False,
    ) -> SpaceMediaLocalization:
        """v_80 — this household's names for a streamed item's files kept
        in a GFS media cache, bound to the household that streamed it.

        The record's descriptor counts only when *takes_descriptor*: the
        record creates the item here, or it is the host's stream. A member
        household streaming an item this household already holds can't
        attach its own blobs to it (#1309 re-review 2) — the item keeps the
        names it has here. Apply before the item is saved; keep the rows
        (:meth:`_keep_media`) only once it was.

        Under those, every file takes its direct name as the provider
        delivers it (:class:`SpaceMediaNamer`, *held* — what the stored row
        shows already): the provider's §25.6 catch-up ships the bytes under
        the same names. v_85: a record's ``files`` hash map counts only from
        the host (*from_host*, ``_acts_as_host``) — which vouches for its
        rows whole and may hash content from before v_85 from its own copy
        (owner decision B); another provider's map is ignored (legacy
        names)."""
        hashes = parse_file_hashes(record) if from_host else None
        direct = self._media_names.localize(
            space_id,
            provider,
            urls,
            held=held or (),
            hashes=hashes,
            verified=VERIFIED_ROW.get() == (space_id, provider),
        )
        if self._gfs_media is None:
            return direct
        return SpaceMediaNamer.combine(
            direct,
            await self._gfs_media.localize(
                space_id,
                record.get(MEDIA_REFS_FIELD) if takes_descriptor else None,
                urls,
                item_id=item_id,
                origin=provider,
                # The same map the direct names took: a hashed described
                # file keeps its one content name, verified on fetch.
                hashes=hashes,
            ),
        )

    async def _keep_media(self, media: SpaceMediaLocalization) -> None:
        if self._gfs_media is not None:
            await self._gfs_media.keep(media)

    async def _local_people(self, space_id: str) -> frozenset[str]:
        """This household's own people seated in ``space_id`` — the
        authors of their own votes, answers and RSVPs, which no provider's
        snapshot replaces."""
        return frozenset(await self._space_repo.list_local_member_user_ids(space_id))

    async def _vetted_actor(
        self, r: dict[str, Any], space_id: str, provider: str, from_host: bool
    ) -> str:
        """The ``actor_user_id`` a gallery / zone tombstone may record as its
        ``deleted_by``. Those live delete rules judge the household, not an
        actor, so a record naming a stranger is not refused — but from a
        member household the name is kept only when the provider speaks for
        that user (seated on it, as ``_writer_delete_admits`` requires for
        stickies / events); otherwise the delete is recorded as nobody's.
        The host's stream is taken whole, like every host record."""
        actor = str(r.get("actor_user_id") or "")
        if not actor or from_host:
            return actor
        if self._authorship is None:
            return ""
        event = _sync_event(space_id, "actor", provider)
        if await self._authorship.acts_for(event, space_id, actor, any_role=True):
            return actor
        return ""

    async def _acts_as_host(self, space: Any, provider: str) -> bool:
        """``provider`` hosts the space HERE: its owner, or the delegated
        admin household that invited us while delegation is on
        (:mod:`socialhome.federation.host_equivalence`) — whose chunks are
        then taken whole, like the owner's. Without an authorship binder
        (no seat repo) only the owner counts. A row verified against the
        signed sync page (:data:`VERIFIED_ROW`, Phase 3) passes too."""
        if space is None or not provider:
            return False
        if space.owner_instance_id == provider:
            return True
        verified = VERIFIED_ROW.get()
        if verified is not None and verified == (space.id, provider):
            return True
        if self._authorship is None:
            return False
        return await self._authorship.acts_as_host(space, provider)

    async def _is_host(self, space_id: str, provider: str) -> bool:
        """Is ``provider`` the household hosting ``space_id``?"""
        space = await self._space_repo.get(space_id)
        return await self._acts_as_host(space, provider)

    async def _may_author(
        self,
        held_back: HeldBack,
        event: FederationEvent,
        space_id: str,
        user_id: str,
        **kwargs: Any,
    ) -> bool:
        """:meth:`SpaceAuthorship.may_author` for a record a member household
        adds — and, when it is refused because this space has no record of
        ``user_id`` at all yet (the roster gossip seating them trails the
        stream), the record is held back: the stream must not count as
        clean, or the provider's watermark would skip it until the daily
        full pass."""
        assert self._authorship is not None
        if await self._authorship.may_author(event, space_id, user_id, **kwargs):
            return True
        if await self._authorship.trails_seat(space_id, user_id):
            held_back.add()
        return False

    async def _writer_delete_admits(
        self,
        event: FederationEvent,
        space_id: str,
        feature: str,
        r: dict[str, Any],
        *,
        row_owner: str,
    ) -> bool:
        """The live delete rule of the collaborative features (tasks, task
        lists, pages, stickies, calendar events —
        ``_collaborative_write_allowed`` for ``ContentAction.DELETE``) for a
        streamed tombstone of a row held live here: a **writer** household
        removes it, if the space's ``feature`` level admits the delete for
        the user who made it (``actor_user_id``, the tombstone's
        ``deleted_by``), who must be seated on the provider. Refusals are
        counted in :meth:`_admit`'s one summary line (``quiet``), not logged
        per record on every scheduler tick."""
        auth = self._authorship
        assert auth is not None
        if not await auth.writes_here(event, space_id):
            return False
        actor = str(r.get("actor_user_id") or "") or None
        if actor is not None and not await auth.acts_for(
            event, space_id, actor, any_role=True
        ):
            return False
        return await auth.access_admits(
            event,
            space_id,
            feature,
            ContentAction.DELETE,
            actor=actor,
            row_owner=row_owner,
            quiet=True,
        )

    async def _anchor_author_ok(
        self, event: FederationEvent, space_id: str, post_id: str
    ) -> bool:
        assert self._authorship is not None
        got = await self._space_post_repo.get(post_id)
        if got is None or got[0] != space_id:
            return False
        return await self._authorship.may_author(event, space_id, got[1].author)


def _bound_here(kind: str, row_id: str, space_id: str, owner: str) -> bool:
    """Is ``row_id`` owner-bound to ``owner`` in ``space_id``? The condition
    for a host stub of an id never held here: ids are global, so a stub for
    another space's id (or a legacy, unbound one) would block a real row."""
    return (
        check_owner_bound_id(kind, row_id, space_id=space_id, owner_user_id=owner)
        is OwnerBinding.VALID
    )


def _log_tombstone_refusals(
    what: str, provider: str, space_id: str, cross_space: list[str], unbound: int
) -> None:
    """One summary per chunk of the tombstone records that were refused."""
    if cross_space:
        log.warning(
            "space sync: %d %s tombstone(s) from %s for %s name a %s held in "
            "another space — refused: %s",
            len(cross_space),
            what,
            provider,
            space_id,
            what,
            ", ".join(cross_space[:5]),
        )
    if unbound:
        log.info(
            "space sync: %d %s tombstone(s) from %s for %s name a %s never "
            "held here whose id is not bound to this space (or, for a "
            "comment / gallery item, whose post / album is not held here) — "
            "no stub recorded",
            unbound,
            what,
            provider,
            space_id,
            what,
        )


def _log_sync_refusal(what: str, row_id: str, space_id: str) -> None:
    """WARNING for a sync record the scoped repo refused: the id belongs
    to another space (or, for a task, its list is not in this space)."""
    log.warning(
        "space sync: %s %s is not writable in space %s — refusing the write",
        what,
        row_id,
        space_id,
    )
