"""§25.6 sync of a space's timetables and their tombstones."""

from __future__ import annotations

import logging
from datetime import UTC
from typing import TYPE_CHECKING, Any

from ....domain.events import TimetableSaved
from ....domain.federation import FederationEvent
from ....domain.timetable import (
    Timetable,
    drop_invalid_icons,
    from_wire_dict,
    remote_version_refusal,
    validate,
)
from ...owner_bound_id import SPACE_TIMETABLE_KIND, OwnerBinding, check_owner_bound_id
from .receiver_base import (
    ReceiverBase,
    _bound_here,
    _log_tombstone_refusals,
    _parse_iso,
)
from .stream_health import HeldBack

if TYPE_CHECKING:
    from ....federation.space_authorship import SpaceAuthorship

log = logging.getLogger(__name__)


class TimetablesMixin(ReceiverBase):
    """The timetable resources of :class:`~.receiver.SpaceSyncReceiver`."""

    __slots__ = ()

    async def _authored_timetable(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        # Moderator-only, per user, like the live event: the
        # household moderates and the recorded editor is its admin;
        # a timetable new here also names a creator it speaks for.
        if self._timetable_repo is None or not rid:
            return False
        if not await auth.is_admin_household(event, space_id):
            return False
        if not await auth.admin_as(event, space_id, str(r.get("updated_by") or "")):
            return False
        if await self._timetable_repo.get(rid) is not None:
            return True
        return await self._may_author(
            held_back, event, space_id, str(r.get("created_by") or "")
        )

    async def _persist_timetable_tombstones(
        self, records: list[dict[str, Any]], space_id: str, *, provider: str
    ) -> None:
        """Timetable deletes (v_92) — only the host's records get here
        (:meth:`_authored_record` refuses a member household's). A timetable
        held live here in this space is tombstoned, as the live
        ``SPACE_TIMETABLE_DELETED`` does; an id never held gets a stub only
        when it is owner-bound to its ``created_by`` in THIS space (ids are
        global — a stub for another space's id would eat its upserts)."""
        if self._timetable_repo is None:
            return
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            tt_id = str(r.get("id") or "")
            if not tt_id:
                continue
            at = _parse_iso(r.get("deleted_at") or r.get("created_at"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            held = await self._timetable_repo.get(tt_id)
            if held is not None and held[0] != space_id:
                cross_space.append(tt_id)
                continue
            if held is None:
                if await self._timetable_repo.is_tombstoned(tt_id):
                    continue
                if not _bound_here(
                    SPACE_TIMETABLE_KIND,
                    tt_id,
                    space_id,
                    str(r.get("created_by") or ""),
                ):
                    unbound += 1
                    continue
            await self._timetable_repo.tombstone(tt_id, space_id=space_id, at=at)
        _log_tombstone_refusals("timetable", provider, space_id, cross_space, unbound)

    async def _persist_timetables(
        self, records: list[dict[str, Any]], space_id: str, *, provider: str
    ) -> None:
        """Space timetables (v_39): each record is a domain wire dict,
        parsed and validated like the live event; applied last-writer-wins
        (a stale copy, a deleted id or another space's id is a no-op)."""
        if self._timetable_repo is None:
            log.debug(
                "received %d timetable records — no timetable_repo wired, skipping",
                len(records),
            )
            return
        for r in records:
            tt = _timetable_from_record(r, space_id)
            if tt is None:
                continue
            held = await self._timetable_repo.get(tt.id)
            refusal = remote_version_refusal(
                tt.version,
                held[1].version if held is not None and held[0] == space_id else None,
            )
            if refusal is not None:
                log.warning(
                    "space sync: timetable %s for %s — %s; skipped",
                    tt.id,
                    space_id,
                    refusal,
                )
                continue
            if await self._timetable_repo.apply_remote(tt, space_id=space_id):
                await self._bus.publish(
                    TimetableSaved(
                        timetable=tt, space_id=space_id, origin_instance_id=provider
                    )
                )


def _timetable_from_record(r: dict[str, Any], space_id: str) -> Timetable | None:
    """A synced timetable record, or ``None`` (logged at WARNING) when it is
    malformed, out of bounds, carries assignees, or its id is not bound to
    its creator in this space — space timetables are owner-bound from their
    first release, so a legacy-shaped id is refused too."""
    try:
        tt = drop_invalid_icons(from_wire_dict(r))
        validate(tt)
    except Exception as exc:  # hostile input: never raise out of the stream
        log.warning(
            "space sync: unusable timetable record for %s — skipped: %.200s",
            space_id,
            exc,
            exc_info=True,
        )
        return None
    if tt.assignees:
        log.warning(
            "space sync: timetable %s for %s names assignees — skipped",
            tt.id,
            space_id,
        )
        return None
    if (
        check_owner_bound_id(
            SPACE_TIMETABLE_KIND, tt.id, space_id=space_id, owner_user_id=tt.created_by
        )
        is not OwnerBinding.VALID
    ):
        log.warning(
            "space sync: timetable id %s is not bound to %r in space %s — skipped",
            tt.id,
            tt.created_by,
            space_id,
        )
        return None
    return tt
