"""Inbound federation handlers for personal calendar invites (§23.60).

Cross-household invites land here. Local household members never appear
on the wire — coordinating with a household member is done by writing
directly to that member's personal calendar (no envelope, no RSVP).
Only confirmed paired-instance users can be invited, and the recipient
side mirrors each invite into the user's existing personal calendar
with ``origin='remote_invite'``. RSVP responses propagate back to the
organiser's instance via ``PERSONAL_CALENDAR_RSVP_UPDATED``.

Handlers are lenient: malformed payloads log + return rather than
raise, because §24.11 has already verified the signature + replay
cache, and a peer sending a malformed body shouldn't take the inbound
pipeline down.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ...domain.calendar import CalendarEvent, CalendarRSVP
from ...domain.federation import FederationEventType
from ...infrastructure.event_bus import EventBus
from ...utils.datetime import parse_iso8601_optional
from ...utils.timezones import coerce_tz

if TYPE_CHECKING:
    from ...domain.federation import FederationEvent
    from ...federation.federation_service import FederationService
    from ...repositories.calendar_repo import AbstractCalendarRepo
    from ...repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


class PersonalCalendarInboundHandlers:
    """Register PERSONAL_CALENDAR_* inbound handlers on the federation registry."""

    __slots__ = ("_bus", "_calendar_repo", "_user_repo")

    def __init__(
        self,
        *,
        bus: EventBus,
        calendar_repo: "AbstractCalendarRepo",
        user_repo: "AbstractUserRepo",
    ) -> None:
        self._bus = bus
        self._calendar_repo = calendar_repo
        self._user_repo = user_repo

    def attach_to(self, federation_service: "FederationService") -> None:
        registry = federation_service._event_registry
        registry.register(
            FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
            self._on_event_saved,
        )
        registry.register(
            FederationEventType.PERSONAL_CALENDAR_EVENT_UPDATED,
            self._on_event_saved,
        )
        registry.register(
            FederationEventType.PERSONAL_CALENDAR_EVENT_DELETED,
            self._on_event_deleted,
        )
        registry.register(
            FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED,
            self._on_rsvp_updated,
        )
        registry.register(
            FederationEventType.PERSONAL_CALENDAR_RSVP_DELETED,
            self._on_rsvp_deleted,
        )

    # ─── Helpers ─────────────────────────────────────────────────────────

    async def _resolve_target_calendar(self, recipient_user_id: str) -> str | None:
        """Pick a personal calendar to mirror an inbound invite into.

        Defaults to the recipient's first calendar (alphabetical by
        name, set by ``list_calendars_for_user``). Returns ``None`` if
        the user has no calendars yet — invite is dropped with a log.
        """
        user = await self._user_repo.get_by_user_id(recipient_user_id)
        if user is None:
            return None
        cals = await self._calendar_repo.list_calendars_for_user(user.username)
        if not cals:
            return None
        return cals[0].id

    # ─── Event lifecycle ─────────────────────────────────────────────────

    async def _on_event_saved(self, event: "FederationEvent") -> None:
        p = event.payload
        remote_event_id = str(p.get("event_id") or p.get("id") or "")
        summary = str(p.get("summary") or "")
        start = parse_iso8601_optional(p.get("start"))
        end = parse_iso8601_optional(p.get("end"))
        attendee_ids = [str(uid) for uid in (p.get("attendee_user_ids") or []) if uid]
        organizer_user_id = str(p.get("organizer_user_id") or "")
        if (
            not remote_event_id
            or not summary
            or start is None
            or end is None
            or not attendee_ids
            or not organizer_user_id
        ):
            log.debug("PERSONAL_CALENDAR_EVENT_* missing required field")
            return
        # IANA wall-clock anchor — additive over the wire. The
        # organiser's tz is what locally-rendered times anchor to; the
        # SPA still annotates "≈ HH:MM your time" for the recipient.
        # Old peers omit the field; default ``"UTC"``.
        #
        # Peer-supplied, so validated here: an unknown zone name makes
        # ``Intl`` throw in the SPA and one bad row breaks the whole
        # household calendar. Fail closed on the value, not the event —
        # the row still lands, anchored to UTC. Resolved once per
        # envelope (it doesn't vary by recipient) so a bogus zone from a
        # misbehaving peer logs one warning, not one per attendee.
        event_tz = coerce_tz(
            p.get("tz"),
            context=f"{event.event_type} from instance {event.from_instance}",
        )
        # Each recipient gets their own mirror row — the row id is
        # deterministic from (remote_instance, remote_event, recipient)
        # so a redelivered envelope (network retry) collapses onto the
        # same row instead of creating a duplicate.
        for recipient_user_id in attendee_ids:
            row_id = _mint_event_id(
                event.from_instance,
                remote_event_id,
                recipient_user_id,
            )
            existing = await self._calendar_repo.get_event(row_id)
            cal_id = (
                existing.calendar_id
                if existing is not None
                else await self._resolve_target_calendar(recipient_user_id)
            )
            if cal_id is None:
                log.info(
                    "personal calendar invite dropped — no calendar for %s",
                    recipient_user_id,
                )
                continue
            # Client-stamped grouping uuid (issue #327). Optional on
            # the wire — sub-version peers omit the field; the SPA's
            # content-key fallback covers those rows. When present
            # the agenda's grouper merges by intent across the
            # household boundary.
            client_uuid_in = p.get("client_event_uuid")
            client_event_uuid = (
                str(client_uuid_in)
                if isinstance(client_uuid_in, str) and client_uuid_in
                else None
            )
            mirrored = CalendarEvent(
                id=row_id,
                calendar_id=cal_id,
                summary=summary,
                start=start,
                end=end,
                created_by=organizer_user_id,
                description=p.get("description"),
                all_day=bool(p.get("all_day", False)),
                attendees=tuple(attendee_ids),
                rrule=p.get("rrule"),
                rsvp_enabled=bool(p.get("rsvp_enabled", False)),
                cover_url=p.get("cover_url"),
                location=p.get("location"),
                origin="remote_invite",
                remote_event_id=remote_event_id,
                remote_instance_id=event.from_instance,
                tz=event_tz,
                client_event_uuid=client_event_uuid,
            )
            if existing is not None:
                # Re-save preserves the row id; ``replace`` on the
                # existing dataclass keeps original origin / linkage.
                mirrored = replace(
                    existing,
                    summary=summary,
                    start=start,
                    end=end,
                    description=p.get("description"),
                    all_day=bool(p.get("all_day", False)),
                    attendees=tuple(attendee_ids),
                    rrule=p.get("rrule"),
                    rsvp_enabled=bool(p.get("rsvp_enabled", False)),
                    cover_url=p.get("cover_url"),
                    location=p.get("location"),
                    tz=event_tz,
                    client_event_uuid=client_event_uuid,
                )
            await self._calendar_repo.save_event(mirrored)
            # On first receipt, default the recipient's RSVP to
            # ``tentative`` so the organiser's view shows the invite
            # was delivered (the user can change it to accepted /
            # declined later — that fires PERSONAL_CALENDAR_RSVP_UPDATED).
            if existing is None and p.get("rsvp_enabled", False):
                await self._calendar_repo.upsert_rsvp(
                    CalendarRSVP(
                        event_id=mirrored.id,
                        user_id=recipient_user_id,
                        status="tentative",
                        updated_at=datetime.now(timezone.utc).isoformat(),
                        occurrence_at=start.isoformat(),
                    )
                )

    async def _on_event_deleted(self, event: "FederationEvent") -> None:
        p = event.payload
        remote_event_id = str(p.get("event_id") or p.get("id") or "")
        attendee_ids = [str(uid) for uid in (p.get("attendee_user_ids") or []) if uid]
        if not remote_event_id:
            return
        if attendee_ids:
            # Mirror was per-recipient; delete the per-recipient rows
            # we minted at create-time.
            for recipient_user_id in attendee_ids:
                row_id = _mint_event_id(
                    event.from_instance,
                    remote_event_id,
                    recipient_user_id,
                )
                existing = await self._calendar_repo.get_event(row_id)
                if existing is not None:
                    await self._calendar_repo.delete_event(existing.id)
            return
        # Fallback for older payloads that omit the attendee list:
        # find any mirror row pointing back to this remote event.
        existing = await self._calendar_repo.get_event_by_remote(
            remote_instance_id=event.from_instance,
            remote_event_id=remote_event_id,
        )
        if existing is None:
            return
        await self._calendar_repo.delete_event(existing.id)

    # ─── RSVP propagation back to the organiser ─────────────────────────

    async def _authorised_rsvp_event(
        self,
        event: "FederationEvent",
        local_event_id: str,
        user_id: str,
    ) -> CalendarEvent | None:
        """Return the organiser's event iff ``from_instance`` may answer for ``user_id``.

        An RSVP is a reply to an invite this household sent, so it is
        honoured only when all of these hold — otherwise the event is
        dropped with a WARNING:

        * the event exists here and is organised here (not an inbound
          ``remote_invite`` mirror — replies flow organiser-ward only);
        * ``user_id`` is on the event's ``attendees`` — the same list the
          outbound fan-out (``CalendarService._publish_federation_event``)
          routed the invite from;
        * ``user_id``'s home household is the sending household. That
          rules out a local member (home = this instance) and any
          invitee of a third household.
        """
        existing = await self._calendar_repo.get_event(local_event_id)
        if existing is None:
            log.debug(
                "%s for unknown event %s",
                event.event_type,
                local_event_id,
            )
            return None
        reason: str | None = None
        if existing.origin == "remote_invite":
            reason = "event is not organised here"
        elif user_id not in existing.attendees:
            reason = "user was not invited"
        elif await self._user_repo.get_instance_for_user(user_id) != (
            event.from_instance
        ):
            reason = "user does not belong to the sending household"
        if reason is not None:
            log.warning(
                "%s from %s: %s (event=%s user=%s) — refusing",
                event.event_type,
                event.from_instance,
                reason,
                local_event_id,
                user_id,
            )
            return None
        return existing

    async def _on_rsvp_updated(self, event: "FederationEvent") -> None:
        p = event.payload
        local_event_id = str(p.get("event_id") or "")
        user_id = str(p.get("user_id") or "")
        status = str(p.get("status") or "")
        occurrence_at = str(p.get("occurrence_at") or "")
        updated_at = str(p.get("updated_at") or datetime.now(timezone.utc).isoformat())
        if not local_event_id or not user_id or not status:
            return
        if status not in ("accepted", "declined", "tentative"):
            log.debug("PERSONAL_CALENDAR_RSVP_UPDATED: bad status %r", status)
            return
        # The organiser's row id is in ``event_id`` (the responder
        # echoes the original event id back).
        existing = await self._authorised_rsvp_event(event, local_event_id, user_id)
        if existing is None:
            return
        if not occurrence_at:
            occurrence_at = existing.start.isoformat()
        await self._calendar_repo.upsert_rsvp(
            CalendarRSVP(
                event_id=local_event_id,
                user_id=user_id,
                status=status,
                updated_at=updated_at,
                occurrence_at=occurrence_at,
            )
        )

    async def _on_rsvp_deleted(self, event: "FederationEvent") -> None:
        p = event.payload
        local_event_id = str(p.get("event_id") or "")
        user_id = str(p.get("user_id") or "")
        occurrence_at = p.get("occurrence_at")
        if not local_event_id or not user_id:
            return
        existing = await self._authorised_rsvp_event(event, local_event_id, user_id)
        if existing is None:
            return
        await self._calendar_repo.remove_rsvp(
            local_event_id,
            user_id,
            occurrence_at=str(occurrence_at) if occurrence_at else None,
        )


def _mint_event_id(
    remote_instance_id: str,
    remote_event_id: str,
    recipient_user_id: str,
) -> str:
    """Deterministic local id for a mirrored remote invite.

    Same envelope re-delivered (network retry) collapses onto the
    same row instead of creating duplicates. Three components keep it
    unique even when the same event invites multiple users on this
    instance — each gets their own mirror row on their own calendar.
    """
    return (
        f"ri_{remote_instance_id[:12]}_{remote_event_id[:24]}_{recipient_user_id[:16]}"
    )
