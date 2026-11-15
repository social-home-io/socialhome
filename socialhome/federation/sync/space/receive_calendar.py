"""§25.6 sync of a space's calendar: events, their RSVPs, tombstones and
schedule polls.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ....domain.calendar import (
    CalendarEvent,
    CalendarRSVP,
    RSVPStatus,
    clean_attendees,
    clean_location,
    coerce_capacity,
    is_occurrence_id,
)
from ....domain.events import CalendarEventDeleted
from ....domain.federation import FederationEvent
from ....domain.space_item import stored_stamp
from ....domain.space_media_fields import item_media_urls
from ....services.calendar_service import (
    calendar_event_too_large,
    log_calendar_event_not_stored,
)
from ....services.inbound_media_store import space_media_ref, verbatim_local_media_ref
from ....utils.rrule import clean_rrule
from ....utils.timezones import coerce_tz
from ...owner_bound_id import SPACE_CALENDAR_EVENT_KIND
from .receiver_base import (
    ReceiverBase,
    _bound_here,
    _log_sync_refusal,
    _log_tombstone_refusals,
    _parse_iso,
)
from .stream_health import HeldBack

if TYPE_CHECKING:
    from ....federation.space_authorship import SpaceAuthorship

log = logging.getLogger(__name__)


class CalendarMixin(ReceiverBase):
    """The calendar and schedule resources of
    :class:`~.receiver.SpaceSyncReceiver`."""

    __slots__ = ()

    async def _persist_calendar(
        self, space_id: str, records: list[dict[str, Any]], *, provider: str
    ) -> None:
        from_host = await self._is_host(space_id, provider)
        for r in records:
            event = _calendar_from_record(r, provider=provider)
            if event is None:
                continue
            if await self._space_calendar_repo.is_event_deleted(
                event.id, space_id=space_id
            ):
                log.debug("sync: event %s was deleted here — skipped", event.id)
                continue
            held_event = await self._space_calendar_repo.get_event(event.id)
            # The one receiver rule: over the limit (measured whole) and
            # larger than our copy → not stored. The stream goes on; the
            # watermark moves.
            held_here = (
                held_event[1]
                if held_event is not None and held_event[0] == space_id
                else None
            )
            if calendar_event_too_large(event, was=held_here):
                log_calendar_event_not_stored("space sync", event, provider)
                # The event we hold still takes the host's RSVPs (they
                # are not part of its measured size).
                if (
                    held_here is not None
                    and from_host
                    and isinstance(r.get("rsvps"), list)
                ):
                    await self._persist_rsvps(held_here, r, space_id)
                continue
            media = await self._localize_record(
                space_id,
                r,
                event.id,
                item_media_urls("calendar", event),
                provider=provider,
                takes_descriptor=held_event is None or from_host,
                held=(
                    item_media_urls("calendar", held_event[1])
                    if held_event is not None and held_event[1].cover_url
                    else None
                ),
                from_host=from_host,
            )
            event = media.item("calendar", event)
            if not await self._space_calendar_repo.save_event(event, space_id=space_id):
                _log_sync_refusal("calendar event", event.id, space_id)
                continue
            await self._keep_media(media)
            if from_host and isinstance(r.get("rsvps"), list):
                await self._persist_rsvps(event, r, space_id)

    async def _persist_schedules(
        self, space_id: str, records: list[dict[str, Any]], *, provider: str
    ) -> None:
        # F5: schedule-poll meta + slot defs catch up so a remote
        # member's slot picker isn't empty after a §25.6 sync. The
        # wrapper post (PostType.SCHEDULE) was already persisted
        # above by the ``posts`` exporter, so the FK to
        # ``space_posts(id)`` is satisfied.
        if self._poll_repo is None:
            log.debug(
                "received %d schedule records — no poll_repo wired, skipping",
                len(records),
            )
            return
        for r in records:
            post_id = str(r.get("post_id") or "")
            title = str(r.get("title") or "")
            slots = r.get("slots") or []
            if not post_id or not title or not slots:
                log.debug("schedule record missing required field: %r", r)
                continue
            try:
                if not await self._poll_repo.create_schedule_poll_in_space(
                    space_id=space_id,
                    post_id=post_id,
                    title=title,
                    deadline=r.get("deadline"),
                    slots=list(slots),
                ):
                    log.warning(
                        "space sync: schedule poll %s is not a post of "
                        "space %s — refusing the write",
                        post_id,
                        space_id,
                    )
            except Exception as exc:  # pragma: no cover
                log.debug(
                    "schedule catch-up create failed for post=%s: %s",
                    post_id,
                    exc,
                    exc_info=True,
                )
        # v_92 — the answers and the pick, from the space's host only
        # (a member household's record only ever adds a poll it lacks).
        if await self._is_host(space_id, provider):
            await self._persist_schedule_state(records, space_id)

    async def _authored_calendar_delete(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        # The live SPACE_CALENDAR_EVENT_DELETED rule (feature
        # ``calendar``) for an event held live here IN THIS SPACE.
        held_event = await self._space_calendar_repo.get_event(rid) if rid else None
        if held_event is None or held_event[0] != space_id:
            return False
        return await self._writer_delete_admits(
            event,
            space_id,
            "calendar",
            r,
            row_owner=held_event[1].created_by,
        )

    async def _authored_calendar(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        if not rid or await self._space_calendar_repo.get_event(rid) is not None:
            return False
        if await self._space_calendar_repo.is_event_deleted(rid, space_id=space_id):
            return False  # deleted here; the tombstone wins
        return await self._may_author(
            held_back, event, space_id, str(r.get("created_by") or "")
        )

    async def _authored_schedule(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        post_id = str(r.get("post_id") or "")
        if self._poll_repo is None or not post_id:
            return False
        if await self._poll_repo.get_schedule_meta(post_id) is not None:
            return False
        return await self._anchor_author_ok(event, space_id, post_id)

    async def _persist_rsvps(
        self, event: CalendarEvent, r: dict[str, Any], space_id: str
    ) -> None:
        """The host's RSVPs of a held event (v_92), last writer wins on
        ``updated_at``: an answer is taken only when newer than the one held
        here, and an RSVP the host no longer holds is dropped only when the
        host's list is whole (no ``rsvps_partial``) and the held one is not
        newer than the host's snapshot (``rsvps_at``, else the newest stamp
        of the list) — one that reached this household live after the host
        read its list stays.

        Our own people's RSVPs are authored here: of theirs, only the
        transitions the live rule lets another household make are taken
        (:meth:`SpaceContentInboundHandlers._rsvp_allowed`) — a request
        approved (→ going / waitlist) or denied (gone), a waitlisted seat
        promoted to going while the occurrence has room."""
        keep = await self._local_people(space_id)
        held = {
            (h.user_id, h.occurrence_at): h
            for h in await self._space_calendar_repo.list_rsvps(event.id)
        }
        wanted: dict[tuple[str, str], CalendarRSVP] = {}
        stamps: dict[tuple[str, str], datetime | None] = {}
        for a in r["rsvps"]:
            if not isinstance(a, dict):
                continue
            user_id = str(a.get("user_id") or "")
            occurrence_at = str(a.get("occurrence_at") or "")
            status = str(a.get("status") or "")
            if not user_id or not occurrence_at or status not in RSVPStatus.ALL:
                continue
            stamp = stored_stamp(a.get("updated_at"))
            key = (user_id, occurrence_at)
            stamps[key] = stamp
            wanted[key] = CalendarRSVP(
                event_id=event.id,
                user_id=user_id,
                status=status,
                updated_at=(stamp or datetime.now(UTC)).isoformat(),
                occurrence_at=occurrence_at,
            )
        newest = [t for t in stamps.values() if t is not None]
        snapshot = stored_stamp(r.get("rsvps_at")) or (max(newest) if newest else None)

        def going(occurrence_at: str) -> int:
            return sum(
                1
                for (_u, occ), h in held.items()
                if occ == occurrence_at and h.status == RSVPStatus.GOING
            )

        for key, rsvp in sorted(wanted.items()):
            have = held.get(key)
            if have is not None:
                if have.status == rsvp.status:
                    continue
                theirs = stamps[key]
                mine = stored_stamp(have.updated_at)
                if theirs is None or (mine is not None and mine >= theirs):
                    continue
            if rsvp.user_id in keep and not (
                have is not None
                and (
                    (
                        have.status == RSVPStatus.REQUESTED
                        and rsvp.status in (RSVPStatus.GOING, RSVPStatus.WAITLIST)
                    )
                    or (
                        have.status == RSVPStatus.WAITLIST
                        and rsvp.status == RSVPStatus.GOING
                        and event.capacity is not None
                        and going(rsvp.occurrence_at) < event.capacity
                    )
                )
            ):
                continue
            if await self._space_calendar_repo.upsert_rsvp(rsvp, space_id=space_id):
                held[key] = rsvp
        if r.get("rsvps_partial") or snapshot is None:
            return
        for key in sorted(held.keys() - wanted.keys()):
            have = held[key]
            if have.user_id in keep and have.status != RSVPStatus.REQUESTED:
                continue
            mine = stored_stamp(have.updated_at)
            if mine is not None and mine > snapshot:
                continue
            await self._space_calendar_repo.remove_rsvp(
                event.id, key[0], occurrence_at=key[1], space_id=space_id
            )

    async def _persist_schedule_state(
        self, records: list[dict[str, Any]], space_id: str
    ) -> None:
        """A schedule poll's answers and pick from the host's v_92 record:
        everybody's answers but our own people's, last writer wins on
        ``responded_at`` (a cut list — ``responses_partial`` — deletes
        nothing, and an answer newer than the host's read, ``responses_at``,
        stays), and the chosen slot (a pick never reverts). A record without
        ``responses`` (an older host) leaves the held answers."""
        assert self._poll_repo is not None
        keep = await self._local_people(space_id)
        for r in records:
            post_id = str(r.get("post_id") or "")
            if not post_id or ("responses" not in r and "finalized_slot_id" not in r):
                continue
            raw = r.get("responses")
            finalized = r.get("finalized_slot_id")
            await self._poll_repo.apply_synced_schedule_state(
                space_id=space_id,
                post_id=post_id,
                finalized_slot_id=str(finalized) if finalized else None,
                responses=(
                    [a for a in raw if isinstance(a, dict)]
                    if isinstance(raw, list)
                    else None
                ),
                keep_users=keep,
                responses_at=(
                    str(r["responses_at"]) if r.get("responses_at") else None
                ),
                responses_partial=r.get("responses_partial") is True,
            )

    async def _persist_calendar_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed calendar-event deletes (``calendar_deleted``,
        migration 0085).

        An event held live here in this space is tombstoned — its RSVPs and
        reminders go with it — and ``CalendarEventDeleted`` is published as
        for a live ``SPACE_CALENDAR_EVENT_DELETED`` (the feed bridge removes
        the event's announcement post). A member household's records were
        admitted only under the live rule. From the **host**, an id never
        held here gets a stub tombstone when the id is owner-bound to its
        ``created_by`` in THIS space.
        """
        from_host = await self._is_host(space_id, provider)
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            event_id = str(r.get("id") or r.get("event_id") or "")
            if not event_id:
                continue
            deleted_by = str(r.get("actor_user_id") or "")
            held = await self._space_calendar_repo.get_event(event_id)
            if held is not None:
                if held[0] != space_id:
                    cross_space.append(event_id)
                elif await self._space_calendar_repo.delete_event(
                    event_id, space_id=space_id, deleted_by=deleted_by
                ):
                    await self._bus.publish(
                        CalendarEventDeleted(event_id=event_id, space_id=space_id)
                    )
                continue
            if not from_host or await self._space_calendar_repo.is_event_deleted(
                event_id, space_id=space_id
            ):
                continue
            created_by = str(r.get("created_by") or "")
            if not _bound_here(
                SPACE_CALENDAR_EVENT_KIND, event_id, space_id, created_by
            ):
                unbound += 1
                continue
            await self._space_calendar_repo.tombstone_event(
                event_id,
                space_id=space_id,
                created_by=created_by,
                created_at=str(r.get("created_at") or ""),
                deleted_by=deleted_by,
            )
        _log_tombstone_refusals(
            "calendar event", provider, space_id, cross_space, unbound
        )


def _calendar_from_record(r: dict[str, Any], *, provider: str) -> CalendarEvent | None:
    """A ``calendar`` record as the event row it streams — read like the
    live ``SPACE_CALENDAR_EVENT_*`` payload, rule and all: a record that
    dropped ``rrule`` / ``cover_url`` / ``location`` / ``announce_in_feed``
    upserted a member's copy of a series into a one-off event and wiped the
    rest, on every stream.

    An id of the form ``<event id>@<its own start>`` is not a row: it is
    an occurrence an older provider expanded a series into (one record per
    occurrence, ten years either side of its clock). The series streams as
    its own row; storing its occurrences would duplicate it as one-off
    events, so they are skipped."""
    rid = str(r.get("id") or "")
    if (
        not rid
        or is_occurrence_id(rid, str(r.get("start") or ""))
        or not r.get("calendar_id")
        or not r.get("summary")
        or not r.get("created_by")
    ):
        return None
    start = _parse_iso(r.get("start"))
    end = _parse_iso(r.get("end"))
    try:
        # As on a live event: lenient for an event saved before the checks,
        # a rule over its length cap refused.
        attendees = clean_attendees(r.get("attendees"), strict=False)
        rrule = clean_rrule(r.get("rrule"), strict=False)
    except ValueError as exc:
        log.warning(
            "space sync: calendar event %s from %s — %s; skipped", rid, provider, exc
        )
        return None
    return CalendarEvent(
        id=rid,
        calendar_id=str(r["calendar_id"]),
        summary=str(r["summary"]),
        start=start,
        end=end,
        created_by=str(r["created_by"]),
        description=r.get("description"),
        all_day=bool(r.get("all_day", False)),
        attendees=attendees,
        mirrored_from=r.get("mirrored_from"),
        rrule=rrule,
        capacity=coerce_capacity(r.get("capacity")),
        # Rendered as ``<img src>`` for every member: only a local media
        # reference — a third-party URL would leak their IPs.
        cover_url=(
            cover
            if (cover := verbatim_local_media_ref(r.get("cover_url")))
            and space_media_ref(cover)
            else None
        ),
        location=clean_location(r.get("location")),
        # IANA wall-clock anchor — validated as on the live event; an older
        # peer omits it and gets ``"UTC"``.
        tz=coerce_tz(r.get("tz"), context=f"space sync calendar from {provider}"),
        # §23.15 opt-in feed mirror; absent on an older provider → True, the
        # live event's default.
        announce_in_feed=bool(r.get("announce_in_feed", True)),
    )
