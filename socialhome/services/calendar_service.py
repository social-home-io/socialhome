"""Calendar service — thin orchestration wrapper around calendar repos.

Provides service-layer entry points for personal calendars and space
calendar events. Route handlers call these methods; no SQL in routes.

Raises the usual domain exceptions:

* ``KeyError``   → 404 (calendar or event not found)
* ``ValueError`` → 422 (validation failure)
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..domain.calendar import (
    Calendar,
    CalendarEvent,
    CalendarEventCopy,
    RsvpPastError,
    CalendarEventCreate,
    CalendarRSVP,
    EventReminder,
    RSVPStatus,
)
from ..domain.child_protection import ProtectedCapability
from ..domain.space import (
    HostTooOldError,
    AccessDecision,
    ContentAction,
    ModerationQueueFullError,
    ModerationTargetGoneError,
    Space,
    SpaceModerationItem,
    SpaceArchivedError,
    SpacePermissionError,
)
from ..domain.events import (
    CalendarEventCreated,
    CalendarEventDeleted,
    CalendarEventUpdated,
    SpaceMemberLeft,
    SpaceRsvpChanged,
    UserProvisioned,
)
from ..domain.federation import FederationEventType
from ..federation.owner_bound_id import (
    SPACE_CALENDAR_EVENT_KIND,
    SPACE_POST_KIND,
    mint_owner_bound_id,
)
from ..infrastructure.event_bus import EventBus
from ..media_signer import strip_signature_query
from ..repositories.calendar_repo import AbstractCalendarRepo, AbstractSpaceCalendarRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..utils.rrule import expand_rrule
from ..utils.timezones import is_valid_tz
from .bus_publisher import BusPublisherMixin
from .content_access import ContentAccessMixin
from .moderation_release import with_release
from .protection_gate import ProtectionGateMixin
from .space_moderation_service import ApplyResult, item_payload_snapshot
from .space_service import SpaceService

if TYPE_CHECKING:
    from .space_moderation_service import ModerationSubmitter
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


# Sentinel used by ``update_event(... cover_url=...)`` to distinguish
# "no change" from "explicit clear". Other update fields use
# ``None``/``"no change"`` because they have no meaningful empty
# value at the wire level — cover_url's clear-vs-keep ambiguity
# needs the extra signal.
_UNSET: object = object()

#: Signature of a ``ux_calendar_events_fanout`` violation
#: (``0047_calendar_fanout_dedupe.sql`` — one local fan-out copy per
#: ``(calendar_id, client_event_uuid)``) inside an ``IntegrityError``
#: message. SQLite names the INDEXED COLUMNS, not the index, in a
#: UNIQUE-constraint message ("UNIQUE constraint failed: t.a, t.b"),
#: so the column pair is the only discriminator available — and it is
#: exact, since no other index on ``calendar_events`` covers that pair.
#: ``update_event`` uses it to tell a client-caused uuid collision
#: (→ 422) from any other integrity failure on the same statement
#: (→ propagate unchanged, 500).
_FANOUT_UNIQUE_VIOLATION = (
    "UNIQUE constraint failed: "
    "calendar_events.calendar_id, calendar_events.client_event_uuid"
)


# Public alias for routes that need to express "leave the cover_url
# field as-is" when invoking ``update_event``. Resolves to the same
# sentinel object so identity comparison (``is _UNSET``) works.
UNSET_COVER: object = _UNSET
#: Same shape as ``UNSET_COVER`` for the ``location`` field. The
#: route layer hands this in when the request body omits ``location``
#: so the service knows "leave the existing value alone" — an explicit
#: ``None`` from the client still clears it.
UNSET_LOCATION: object = _UNSET


def _clean_cover_url(value: str | None) -> str | None:
    """Normalise a cover URL coming from the client.

    Empty strings collapse to ``None`` so the column stays NULL when
    the user picked a file then "Removed" it before saving. Signed
    URLs round-trip through ``strip_signature_query`` so the stored
    value is the canonical ``/api/media/{filename}`` form — the
    media-signer re-signs on every read.
    """
    if not value:
        return None
    return strip_signature_query(value)


#: Upper bound on the ``location`` text so a hostile client can't push
#: a 1 MB blob into every event row. Matches the iCal RFC 5545
#: practical-limit guidance (line folding kicks in long before this).
_LOCATION_MAX = 500


def _clean_location(value: str | None) -> str | None:
    """Normalise a location string from the client.

    Trims surrounding whitespace, collapses an empty string to ``None``
    (so blank input doesn't store as a literal empty value), and
    truncates anything past :data:`_LOCATION_MAX` characters.
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    return trimmed[:_LOCATION_MAX]


def _clean_client_event_uuid(value: str | None) -> str | None:
    """Normalise the client-stamped grouping uuid.

    The SPA's composer mints a v4 UUID before a multi-target fan-out
    and ships it as ``client_event_uuid`` on every ``POST`` in the
    batch — same UUID on every resulting row so the agenda's grouper
    can merge by intent (see issue #327).

    We accept lowercase hex with optional dashes, trim whitespace, and
    cap the length so a malicious / mis-wired client can't pump
    arbitrary bytes into the column. ``None`` / empty / overlong /
    garbage shapes collapse to ``None`` — the content-key fallback
    in :func:`groupSharedEvents` picks up the slack.
    """
    if value is None:
        return None
    trimmed = value.strip().lower()
    if not trimmed:
        return None
    # UUID hex with optional dashes — 32 chars without, 36 with the
    # canonical 8-4-4-4-12 shape. Anything else is rejected (the
    # column is wide enough for either shape; we just don't want a
    # client jamming a 1 KiB string in here).
    if len(trimmed) not in (32, 36):
        return None
    allowed = set("0123456789abcdef-")
    if any(c not in allowed for c in trimmed):
        return None
    return trimmed


#: Default colours for freshly-created household calendars. The
#: service walks this list in order and picks the first hue not yet
#: taken by an existing calendar so members get visually distinct
#: chips on the first overlay-on moment. The hex values mirror the
#: SPA's chip palette (``_CAL_HUES`` in
#: ``client/src/features/calendar/CalendarPage.tsx``); the first four
#: are the SH design tokens (``--sh-primary`` / ``--sh-success`` /
#: ``--sh-warning`` / ``--sh-danger``) flattened to literal hex so a
#: backend caller can persist them without a CSS context.
_DEFAULT_CALENDAR_PALETTE: tuple[str, ...] = (
    "#D2542A",  # terracotta — --sh-primary
    "#1F4438",  # moss      — --sh-success
    "#C8902F",  # honey     — --sh-warning
    "#EF4444",  # red       — --sh-danger
    "#7B5BA8",  # plum
    "#3F7B8C",  # dusty teal
    "#A89344",  # ochre
    "#5C7B5A",  # sage
    "#9B5B3F",  # cinnamon
    "#34688D",  # navy
    "#7C9D5F",  # olive
    "#B57E47",  # amber
    "#5B8E8E",  # slate teal
    "#8C5777",  # rose-plum
    "#46735A",  # pine
    "#BC6C68",  # brick rose
)


class CalendarService(BusPublisherMixin):
    """Personal calendar operations."""

    __slots__ = (
        "_repo",
        "_bus",
        "_household",
        "_federation",
        "_fed_repo",
        "_user_repo",
    )

    def __init__(
        self,
        calendar_repo: AbstractCalendarRepo,
        bus: EventBus | None = None,
    ) -> None:
        self._repo = calendar_repo
        self._bus = bus
        self._household = None
        self._federation = None
        self._fed_repo: AbstractFederationRepo | None = None
        self._user_repo: AbstractUserRepo | None = None

    def attach_household_features(self, svc) -> None:
        """Wire :class:`PreferencesService` for toggle enforcement (§18)."""
        self._household = svc

    def attach_federation(
        self,
        federation_service,
        *,
        federation_repo: AbstractFederationRepo,
        user_repo: AbstractUserRepo,
    ) -> None:
        """Wire cross-household invite delivery.

        Personal calendar events that name remote attendees fan out to
        each attendee's home instance via
        :meth:`FederationService.send_event`. Local-only events
        (no attendees, or attendees that resolve to the local instance —
        rejected at validate-time) never produce envelopes. The
        ``federation_repo`` is needed to confirm the peer is paired
        before we route to it; ``user_repo`` resolves user → instance.
        """
        self._federation = federation_service
        self._fed_repo = federation_repo
        self._user_repo = user_repo

    async def _require_calendar_enabled(self) -> None:
        if self._household is not None:
            await self._household.require_enabled("calendar")

    async def _resolve_personal_tz(
        self,
        explicit: str | None,
        *,
        owner_username: str,
    ) -> str:
        """Pick the IANA tz for a new personal calendar event.

        Chain: explicit (from the request) → owner's ``users.tz`` →
        household ``tz`` → ``"UTC"``. Each layer always returns a
        concrete IANA name thanks to ``NOT NULL DEFAULT 'UTC'`` in
        ``0002_calendar_timezone.sql``; once an admin / the user / the
        SPA has touched the relevant row the resolution prefers the
        more specific value over the household fallback.

        Validation is best-effort (:func:`is_valid_tz`): an unknown
        IANA name from the request falls back to the next layer rather
        than 400'ing — the SPA always sends a value detected by
        ``Intl`` so this is a safety belt for hand-written API clients.
        """
        if explicit:
            if is_valid_tz(explicit):
                return explicit
            log.warning(
                "calendar create_event got unknown tz %r — falling "
                "back to owner / household",
                explicit,
            )
        if self._user_repo is not None:
            owner = await self._user_repo.get(owner_username)
            if owner is not None and owner.tz and owner.tz != "UTC":
                return owner.tz
        if self._household is not None:
            household = await self._household.get_household()
            return household.tz
        return "UTC"

    async def _validate_attendees(
        self,
        attendees: list[str] | tuple[str, ...] | None,
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        """Reject local user_ids; return (cleaned, instance_for_user).

        Personal-calendar invites are reserved for confirmed paired-
        instance users. Coordinating with a household member is done by
        writing directly to that member's calendar (handled via the
        dialog's calendar selector), not via the invite list — so a
        local user_id appearing here is a mis-wired client and we
        reject it with 422 rather than silently accept.

        ``instance_for_user`` maps each accepted attendee user_id to its
        home instance so the federation outbound knows where to send
        the envelope without re-querying.
        """
        items = tuple(attendees or ())
        if not items:
            return (), {}
        if self._user_repo is None or self._fed_repo is None:
            # Standalone / unit-test mode: skip federation routing but
            # still reject obvious garbage. Tests that exercise the
            # federation path attach the repos.
            return items, {}
        local_id = await self._fed_repo.get_local_identity()
        local_instance_id = (local_id or {}).get("instance_id")
        # §D2b: social surface — a ``space_session`` row (a household we
        # only share a space with, via an invite link) is not a social
        # peer, so read the social list, not every CONFIRMED row.
        confirmed_ids = {
            inst.id for inst in await self._fed_repo.list_social_instances()
        }
        instance_for_user: dict[str, str] = {}
        for uid in items:
            home = await self._user_repo.get_instance_for_user(uid)
            if home is None:
                raise ValueError(
                    f"unknown attendee {uid!r} — not a household member or "
                    "paired-instance user",
                )
            if home == local_instance_id:
                raise ValueError(
                    f"attendee {uid!r} is a household member; add the event "
                    "to their personal calendar directly instead of inviting "
                    "them",
                )
            if home not in confirmed_ids:
                raise ValueError(
                    f"attendee {uid!r} is not on a confirmed paired instance",
                )
            instance_for_user[uid] = home
        return items, instance_for_user

    # ── Calendars ────────────────────────────────────────────────────────

    async def create_calendar(
        self,
        *,
        name: str,
        owner_username: str,
        color: str | None = None,
    ) -> Calendar:
        await self._require_calendar_enabled()
        name = name.strip()
        if not name:
            raise ValueError("calendar name must not be empty")
        if color is None:
            color = await self._next_default_color()
        calendar = Calendar(
            id=uuid.uuid4().hex,
            name=name,
            owner_username=owner_username,
            color=color,
        )
        return await self._repo.save_calendar(calendar)

    async def _next_default_color(self) -> str:
        """Pick the next default colour for a freshly-created calendar.

        Cycles through :data:`_DEFAULT_CALENDAR_PALETTE` so each
        household member's calendar picks a distinct hue on first
        creation. When a hue is still free, it wins; once the palette
        is exhausted the choice falls back to ``count % palette_len``
        so colours wrap predictably rather than collide with the most
        recent pick. The previous default (``#4A90E2`` blue) painted
        every fresh calendar with the same cold chip, which made the
        first "overlay everyone" moment feel generic — a documented
        family complaint in the UX review.
        """
        existing = await self._repo.list_all_calendars()
        used = {c.color for c in existing}
        for hue in _DEFAULT_CALENDAR_PALETTE:
            if hue not in used:
                return hue
        # Palette wrap — at most one collision per cycle past the first 16.
        return _DEFAULT_CALENDAR_PALETTE[len(existing) % len(_DEFAULT_CALENDAR_PALETTE)]

    async def get_calendar(self, calendar_id: str) -> Calendar:
        result = await self._repo.get_calendar(calendar_id)
        if result is None:
            raise KeyError(f"calendar {calendar_id!r} not found")
        return result

    async def list_calendars(
        self,
        username: str,
        *,
        scope: str = "user",
    ) -> list[Calendar]:
        """List calendars visible to *username*.

        ``scope='user'`` (default) returns only the caller's own
        calendars — the historical behaviour. ``scope='household'``
        returns every calendar on the instance so the SPA's calendar
        picker can let a member peek at another member's calendar.
        """
        if scope == "household":
            return await self._repo.list_all_calendars()
        return await self._repo.list_calendars_for_user(username)

    async def delete_calendar(self, calendar_id: str) -> None:
        result = await self._repo.get_calendar(calendar_id)
        if result is None:
            raise KeyError(f"calendar {calendar_id!r} not found")
        await self._repo.delete_calendar(calendar_id)

    # ── Default-calendar seeding ─────────────────────────────────────────
    #
    # The household-calendar surface in the SPA gates its member filter
    # strip on ``cards.length >= 2`` distinct calendar owners. Calendars
    # were historically lazy-created on the first "+ New event" click, so
    # a freshly-provisioned member showed up with zero calendar rows and
    # the strip stayed hidden until they manually created one. Seeding a
    # default calendar at the moment the user row is born removes that
    # footgun: every household member is represented in the strip as
    # soon as they exist.

    async def seed_default_calendar_for(
        self,
        username: str,
        *,
        name: str = "Calendar",
    ) -> Calendar:
        """Ensure ``username`` has at least one calendar row.

        Idempotent — returns the user's first existing calendar if any,
        otherwise creates a new "Calendar" with the next palette hue.
        Bypasses :meth:`_require_calendar_enabled` deliberately: if the
        household later turns the calendar feature on, every member
        already has a calendar to overlay. A user provisioned while the
        feature is off should not have to wait for someone to flip it
        before their first row exists.
        """
        existing = await self._repo.list_calendars_for_user(username)
        if existing:
            return existing[0]
        calendar = Calendar(
            id=uuid.uuid4().hex,
            name=name.strip() or "Calendar",
            owner_username=username,
            color=await self._next_default_color(),
        )
        return await self._repo.save_calendar(calendar)

    async def backfill_default_calendars(
        self,
        usernames: list[str] | tuple[str, ...],
    ) -> int:
        """Seed a default calendar for each user that doesn't have one.

        Returns the number of NEW calendars created — zero on the steady
        state, non-zero on the boot after an upgrade past this change or
        whenever a user was provisioned through a path that bypasses the
        :class:`UserProvisioned` event (the bootstrap admin paths).
        Called from :func:`app.on_startup` after the platform adapter's
        own ``on_startup`` so headless admin provisioning is covered in
        the same pass.
        """
        created = 0
        for username in usernames:
            existing = await self._repo.list_calendars_for_user(username)
            if existing:
                continue
            await self.seed_default_calendar_for(username)
            created += 1
        return created

    def wire(self) -> None:
        """Subscribe to :class:`UserProvisioned` for live seeding.

        Idempotent in the production wiring (called once at app
        startup). A no-op when the service was constructed without a
        bus — unit tests that don't exercise the seeding path skip
        wiring entirely.
        """
        if self._bus is None:
            return
        self._bus.subscribe(UserProvisioned, self._on_user_provisioned)

    async def _on_user_provisioned(self, event: UserProvisioned) -> None:
        await self.seed_default_calendar_for(event.username)

    # ── Events ────────────────────────────────────────────────────────────

    async def create_event(
        self,
        *,
        calendar_id: str,
        summary: str,
        start: str,
        end: str,
        created_by: str,
        all_day: bool = False,
        description: str | None = None,
        attendees: list[str] | None = None,
        rrule: str | None = None,
        rsvp_enabled: bool = False,
        cover_url: str | None = None,
        location: str | None = None,
        tz: str | None = None,
        client_event_uuid: str | None = None,
    ) -> CalendarEvent:
        event, _created = await self._create_or_update_event(
            calendar_id=calendar_id,
            summary=summary,
            start=start,
            end=end,
            created_by=created_by,
            all_day=all_day,
            description=description,
            attendees=attendees,
            rrule=rrule,
            rsvp_enabled=rsvp_enabled,
            cover_url=cover_url,
            location=location,
            tz=tz,
            client_event_uuid=client_event_uuid,
        )
        return event

    async def import_event(
        self,
        *,
        calendar_id: str,
        created_by: str,
        event: CalendarEventCreate,
    ) -> tuple[CalendarEvent, bool]:
        """Persist one parsed import (ICS / AI) onto ``calendar_id``.

        Same create-or-update path as :meth:`create_event` — and so the
        same authorization as creating or editing an event on that
        calendar. An ``event.client_event_uuid`` already present on the
        calendar (a re-import of the same ICS ``UID``) overwrites that
        row with the file's fields instead of inserting a duplicate;
        fields a file cannot carry (attendees, RSVP toggle, cover) keep
        their stored values.
        Returns ``(event, created)`` so the caller can report
        added / updated counts.
        """
        return await self._create_or_update_event(
            calendar_id=calendar_id,
            summary=event.summary,
            start=event.start.isoformat(),
            end=event.end.isoformat(),
            created_by=created_by,
            all_day=event.all_day,
            description=event.description,
            rrule=event.rrule,
            location=event.location,
            # ``None`` for timed events → the usual creator / household
            # chain. All-day imports pin ``"UTC"`` (``_vevent_to_create``).
            tz=event.tz,
            client_event_uuid=event.client_event_uuid,
            keep_app_fields=True,
        )

    async def _create_or_update_event(
        self,
        *,
        calendar_id: str,
        summary: str,
        start: str,
        end: str,
        created_by: str,
        all_day: bool = False,
        description: str | None = None,
        attendees: list[str] | None = None,
        rrule: str | None = None,
        rsvp_enabled: bool = False,
        cover_url: str | None = None,
        location: str | None = None,
        tz: str | None = None,
        client_event_uuid: str | None = None,
        keep_app_fields: bool = False,
    ) -> tuple[CalendarEvent, bool]:
        """Body of :meth:`create_event`; also reports ``created``
        (``False`` when an existing ``(calendar_id, client_event_uuid)``
        row was updated in place). ``keep_app_fields`` — see
        :meth:`_apply_create_as_update`."""
        await self._require_calendar_enabled()
        summary = summary.strip()
        if not summary:
            raise ValueError("event summary must not be empty")

        # Validate calendar exists
        cal = await self._repo.get_calendar(calendar_id)
        if cal is None:
            raise KeyError(f"calendar {calendar_id!r} not found")

        def _parse_dt(value: str) -> datetime:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"invalid datetime: {value!r}") from exc

        start_dt = _parse_dt(start)
        end_dt = _parse_dt(end)
        if end_dt < start_dt:
            raise ValueError("event end must not be before start")

        attendee_tuple, instance_for_user = await self._validate_attendees(attendees)

        event_tz = await self._resolve_personal_tz(
            tz, owner_username=cal.owner_username
        )

        group_uuid = _clean_client_event_uuid(client_event_uuid)

        fields = dict(
            summary=summary,
            start=start_dt,
            end=end_dt,
            all_day=all_day,
            description=description,
            attendees=attendee_tuple,
            rrule=rrule,
            rsvp_enabled=rsvp_enabled,
            cover_url=_clean_cover_url(cover_url),
            location=_clean_location(location),
            tz=event_tz,
        )

        # Idempotent fan-out: the SPA mints one ``client_event_uuid`` per
        # shared event and POSTs once per target calendar. A re-POST of
        # the same ``(calendar_id, client_event_uuid)`` pair — a retried
        # request, or an edit the SPA sent as a create — must update the
        # existing row rather than mint a second copy of the same event
        # on the same calendar (``ux_calendar_events_fanout``).
        if group_uuid is not None:
            prior = await self._repo.find_by_client_event_uuid(
                calendar_id,
                group_uuid,
            )
            if prior is not None:
                updated = await self._apply_create_as_update(
                    prior=prior,
                    calendar=cal,
                    instance_for_user=instance_for_user,
                    fields=fields,
                    keep_app_fields=keep_app_fields,
                )
                return updated, False

        event = CalendarEvent(
            id=uuid.uuid4().hex,
            calendar_id=calendar_id,
            created_by=created_by,
            client_event_uuid=group_uuid,
            **fields,  # type: ignore[arg-type]
        )
        try:
            saved = await self._repo.save_event(event)
        except sqlite3.IntegrityError:
            # The lookup above and this INSERT are not atomic, so a
            # genuinely concurrent double-submit can land the sibling row
            # in between and trip ``ux_calendar_events_fanout``. Retry
            # once down the resolve-to-update path so the loser of the
            # race still sees its edit applied instead of a 500.
            prior = (
                await self._repo.find_by_client_event_uuid(calendar_id, group_uuid)
                if group_uuid is not None
                else None
            )
            if prior is None:
                raise
            updated = await self._apply_create_as_update(
                prior=prior,
                calendar=cal,
                instance_for_user=instance_for_user,
                fields=fields,
                keep_app_fields=keep_app_fields,
            )
            return updated, False
        await self._emit(CalendarEventCreated(event=saved))
        await self._publish_federation_event(
            event=saved,
            calendar=cal,
            instance_for_user=instance_for_user,
            event_type=FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
        )
        return saved, True

    async def _apply_create_as_update(
        self,
        *,
        prior: CalendarEvent,
        calendar: Calendar,
        instance_for_user: dict[str, str],
        fields: dict,
        keep_app_fields: bool = False,
    ) -> CalendarEvent:
        """Apply a create payload onto an existing fan-out sibling.

        Identity and provenance stay with the stored row (``id``,
        ``created_by``, ``origin``, ``mirrored_from``, ``remote_*``,
        ``client_event_uuid``); everything the caller supplied is
        overwritten verbatim.

        This deliberately does NOT delegate to :meth:`update_event`:
        that method reads ``None`` as "no change", so a create that
        omits ``description`` / ``rrule`` (meaning "this event has
        none") would silently inherit the stored value instead of
        clearing it. Create semantics are full-replacement — hence the
        inline ``replace(...)`` tail.

        ``keep_app_fields`` (the import path) exempts the fields an
        imported file never carries — ``attendees``, ``rsvp_enabled``,
        ``cover_url`` — so a re-import does not silently un-invite a
        remote attendee or drop a cover the household set in the app.
        The stored attendees were validated when they were set, so they
        resolve best-effort like an :meth:`update_event` that leaves
        them alone.
        """
        if keep_app_fields:
            fields = {
                **fields,
                "attendees": prior.attendees,
                "rsvp_enabled": prior.rsvp_enabled,
                "cover_url": prior.cover_url,
            }
            instance_for_user = await self._resolve_attendee_instances(
                prior.attendees,
            )
        updated = replace(prior, **fields)
        await self._repo.save_event(updated)
        await self._emit(CalendarEventUpdated(event=updated))
        await self._publish_federation_event(
            event=updated,
            calendar=calendar,
            instance_for_user=instance_for_user,
            event_type=FederationEventType.PERSONAL_CALENDAR_EVENT_UPDATED,
        )
        return updated

    async def list_event_copies(
        self,
        events: Sequence[CalendarEvent],
    ) -> dict[str, tuple[CalendarEventCopy, ...]]:
        """Resolve the authoritative sibling set of each shared event.

        A household event shared with N members is one row per member's
        personal calendar, all carrying the same client-minted
        ``client_event_uuid``. Callers must never infer that set from
        whichever calendars they happen to have loaded — the server
        owns it, and the answer is independent of the caller's
        visibility.

        Returns ``uuid -> copies``. Events without a
        ``client_event_uuid`` (legacy / ICS-imported rows) contribute
        nothing, and an input with no uuids at all short-circuits
        without touching the repo. The distinct uuids go out in ONE
        batched repo call — never one call per event.
        """
        uuids = list(
            dict.fromkeys(e.client_event_uuid for e in events if e.client_event_uuid),
        )
        if not uuids:
            return {}
        groups = await self._repo.list_copies_for_client_event_uuids(uuids)
        return {key: tuple(copies) for key, copies in groups.items()}

    async def get_event(self, event_id: str) -> CalendarEvent:
        result = await self._repo.get_event(event_id)
        if result is None:
            raise KeyError(f"calendar event {event_id!r} not found")
        return result

    async def list_events_in_range(
        self,
        calendar_id: str,
        *,
        start: str,
        end: str,
    ) -> list[CalendarEvent]:
        def _parse_dt(value: str) -> datetime:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"invalid datetime: {value!r}") from exc

        return await self._repo.list_events_in_range(
            calendar_id,
            start=_parse_dt(start),
            end=_parse_dt(end),
        )

    async def delete_event(self, event_id: str) -> None:
        existing = await self._repo.get_event(event_id)
        if existing is None:
            raise KeyError(f"calendar event {event_id!r} not found")
        cal = await self._repo.get_calendar(existing.calendar_id)
        await self._repo.delete_event(event_id)
        await self._emit(CalendarEventDeleted(event_id=event_id))
        # Federate a deletion to peers that previously received the
        # event. Re-resolves attendee → instance because the event row
        # is gone now; we tolerate stragglers (a user removed from the
        # paired-instance side mid-flight is just a no-op).
        if existing.attendees and cal is not None:
            instance_for_user = await self._resolve_attendee_instances(
                existing.attendees,
            )
            await self._publish_federation_event(
                event=existing,
                calendar=cal,
                instance_for_user=instance_for_user,
                event_type=FederationEventType.PERSONAL_CALENDAR_EVENT_DELETED,
            )

    async def update_event(
        self,
        event_id: str,
        *,
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        all_day: bool | None = None,
        description: str | None = None,
        attendees: list[str] | None = None,
        rrule: str | None = None,
        rsvp_enabled: bool | None = None,
        cover_url: object = _UNSET,
        location: object = _UNSET,
        tz: str | None = None,
        client_event_uuid: str | None = None,
    ) -> CalendarEvent:
        """Partial-update an existing event. Only fields the caller
        supplies are overwritten; the rest retain their current values.

        ``client_event_uuid`` promotes a legacy event into a shared
        group; never clears an existing group id (``None`` = no change).
        Promoting into a group that another local row on the *same*
        calendar already occupies raises :class:`ValueError` (→ 422):
        ``ux_calendar_events_fanout`` allows one local copy per
        ``(calendar_id, client_event_uuid)``, and two indistinguishable
        copies of one shared event on one calendar is a client error.

        ``cover_url`` and ``location`` use the ``_UNSET`` sentinel for
        "no change" so an explicit ``None`` from the client still clears
        the field. The other fields use ``None``/``"no change"`` directly
        because none of them have an ambiguous-clear shape.

        ``tz`` is validated via :func:`is_valid_tz` and only overwrites the
        existing event tz when explicitly passed — leaving it absent
        preserves the wall-clock anchor stamped at create time.
        """
        existing = await self._repo.get_event(event_id)
        if existing is None:
            raise KeyError(f"calendar event {event_id!r} not found")
        new_summary = (summary or existing.summary).strip()
        if not new_summary:
            raise ValueError("event summary must not be empty")
        new_start = _parse_iso(start) if start else existing.start
        new_end = _parse_iso(end) if end else existing.end
        if new_end < new_start:
            raise ValueError("event end must not be before start")
        if tz is not None:
            if not is_valid_tz(tz):
                raise ValueError(f"unknown IANA timezone {tz!r}")
            new_tz = tz
        else:
            new_tz = existing.tz
        if cover_url is _UNSET:
            new_cover = existing.cover_url
        else:
            # mypy can't narrow ``object`` past the ``is _UNSET`` guard,
            # but the route layer only ever passes ``str | None`` when
            # the field is present in the body — assert + cast here.
            assert cover_url is None or isinstance(cover_url, str)
            new_cover = _clean_cover_url(cover_url)
        if location is _UNSET:
            new_location = existing.location
        else:
            assert location is None or isinstance(location, str)
            new_location = _clean_location(location)
        if attendees is not None:
            attendee_tuple, instance_for_user = await self._validate_attendees(
                attendees,
            )
        else:
            attendee_tuple = existing.attendees
            instance_for_user = await self._resolve_attendee_instances(
                existing.attendees,
            )
        updated = replace(
            existing,
            summary=new_summary,
            start=new_start,
            end=new_end,
            all_day=bool(all_day) if all_day is not None else existing.all_day,
            description=description
            if description is not None
            else existing.description,
            attendees=attendee_tuple,
            rrule=rrule if rrule is not None else existing.rrule,
            rsvp_enabled=(
                rsvp_enabled if rsvp_enabled is not None else existing.rsvp_enabled
            ),
            cover_url=new_cover,
            location=new_location,
            tz=new_tz,
            # Only overwrite when a *valid* uuid is supplied — a malformed
            # one (cleans to None) falls through to "no change" rather than
            # silently clearing an existing group, per the docstring.
            client_event_uuid=(
                _clean_client_event_uuid(client_event_uuid)
                or existing.client_event_uuid
            ),
        )
        try:
            await self._repo.save_event(updated)
        except sqlite3.IntegrityError as exc:
            # ``ux_calendar_events_fanout`` (0047) keeps at most one local
            # row per ``(calendar_id, client_event_uuid)``. Landing there
            # means the caller tried to promote this event into a group
            # another row on the same calendar already occupies — the two
            # would be indistinguishable copies of one shared event on one
            # calendar, which is a client-side race or a stale retry, not
            # something to paper over. Surface it as a validation error
            # (422) rather than a 500; a silent no-op would leave the
            # client believing the promotion stuck.
            #
            # Match the specific violation rather than relabelling every
            # integrity failure on this statement. That index is the only
            # one reachable here today, but a future CHECK or FK on
            # ``calendar_events`` would be a genuine server-side fault,
            # and reporting it to the client as "your uuid is taken"
            # (422) would send them chasing a field they never sent.
            if _FANOUT_UNIQUE_VIOLATION not in str(exc):
                raise
            raise ValueError(
                "client_event_uuid is already used by another event on this "
                "calendar (ux_calendar_events_fanout)",
            ) from exc
        await self._emit(CalendarEventUpdated(event=updated))
        cal = await self._repo.get_calendar(updated.calendar_id)
        if cal is not None:
            await self._publish_federation_event(
                event=updated,
                calendar=cal,
                instance_for_user=instance_for_user,
                event_type=FederationEventType.PERSONAL_CALENDAR_EVENT_UPDATED,
            )
        return updated

    # ── RSVP (cross-household personal calendar invites) ────────────────

    async def set_rsvp(
        self,
        *,
        event_id: str,
        user_id: str,
        status: str,
    ) -> None:
        """Set the local user's RSVP on an inbound invite.

        Only valid for events with ``origin='remote_invite'`` — events
        authored on this instance don't carry RSVPs (organisers know
        they're attending). The status propagates back to the
        organising instance via PERSONAL_CALENDAR_RSVP_UPDATED so the
        organiser's UI reflects the response.
        """
        if status not in ("accepted", "declined", "tentative"):
            raise ValueError(
                "RSVP status must be one of accepted/declined/tentative",
            )
        existing = await self._repo.get_event(event_id)
        if existing is None:
            raise KeyError(f"calendar event {event_id!r} not found")
        if existing.origin != "remote_invite":
            raise ValueError(
                "RSVPs only apply to inbound invites — local events have "
                "no organiser to reply to",
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        await self._repo.upsert_rsvp(
            CalendarRSVP(
                event_id=event_id,
                user_id=user_id,
                status=status,
                updated_at=now_iso,
                occurrence_at=existing.start.isoformat(),
            )
        )
        await self._publish_rsvp(
            event=existing,
            user_id=user_id,
            status=status,
            updated_at=now_iso,
        )

    async def clear_rsvp(self, *, event_id: str, user_id: str) -> None:
        existing = await self._repo.get_event(event_id)
        if existing is None:
            return
        if existing.origin != "remote_invite":
            raise ValueError(
                "RSVPs only apply to inbound invites",
            )
        await self._repo.remove_rsvp(
            event_id,
            user_id,
            occurrence_at=existing.start.isoformat(),
        )
        await self._publish_rsvp(
            event=existing,
            user_id=user_id,
            status=None,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    # ── Federation helpers ──────────────────────────────────────────────

    async def _resolve_attendee_instances(
        self,
        attendees: tuple[str, ...] | list[str],
    ) -> dict[str, str]:
        """Best-effort lookup user_id → home instance_id.

        Used on update / delete where the attendees were already
        validated at create-time. Missing rows (user moved off the
        peer between create and now) silently drop out — the outbound
        becomes a no-op for that user.
        """
        if not attendees or self._user_repo is None:
            return {}
        out: dict[str, str] = {}
        for uid in attendees:
            home = await self._user_repo.get_instance_for_user(uid)
            if home is not None:
                out[uid] = home
        return out

    async def _publish_federation_event(
        self,
        *,
        event: CalendarEvent,
        calendar: Calendar,
        instance_for_user: dict[str, str],
        event_type: FederationEventType,
    ) -> None:
        """Send a personal-calendar envelope to each attendee's instance.

        No-op when:
        * federation isn't wired (unit tests, standalone mode); or
        * the event has no attendees (purely-local rows never federate); or
        * the event is itself an inbound mirror (``origin='remote_invite'``)
          — the organiser's instance already knows; we don't echo back.
        """
        if self._federation is None or not instance_for_user:
            return
        if event.origin == "remote_invite":
            return
        # One envelope per receiving instance — multiple attendees on
        # the same peer share a single delivery.
        targets: dict[str, list[str]] = {}
        for uid, inst_id in instance_for_user.items():
            targets.setdefault(inst_id, []).append(uid)
        payload_base = _personal_event_payload(event, calendar)
        for inst_id, uids in targets.items():
            payload = dict(payload_base)
            payload["attendee_user_ids"] = sorted(uids)
            try:
                await self._federation.send_event(
                    to_instance_id=inst_id,
                    event_type=event_type,
                    payload=payload,
                )
            except Exception:  # noqa: BLE001
                # Don't fail the whole create on a transient peer
                # outage — the outbox retry loop will redeliver.
                log.exception(
                    "personal calendar federation outbound failed: %s → %s",
                    event_type,
                    inst_id,
                )

    async def _publish_rsvp(
        self,
        *,
        event: CalendarEvent,
        user_id: str,
        status: str | None,
        updated_at: str,
    ) -> None:
        if self._federation is None:
            return
        if not event.remote_instance_id or not event.remote_event_id:
            return
        evt_type = (
            FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED
            if status is not None
            else FederationEventType.PERSONAL_CALENDAR_RSVP_DELETED
        )
        payload: dict = {
            "event_id": event.remote_event_id,
            "user_id": user_id,
            "occurrence_at": event.start.isoformat(),
            "updated_at": updated_at,
        }
        if status is not None:
            payload["status"] = status
        try:
            await self._federation.send_event(
                to_instance_id=event.remote_instance_id,
                event_type=evt_type,
                payload=payload,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "personal calendar RSVP outbound failed: %s → %s",
                evt_type,
                event.remote_instance_id,
            )


def _personal_event_payload(event: CalendarEvent, calendar: Calendar) -> dict:
    """Wire-shape for PERSONAL_CALENDAR_EVENT_*. Encryption-first
    (§25.8.21): every field rides inside the encrypted payload — only
    the envelope's routing fields stay plaintext."""
    return {
        "event_id": event.id,
        "calendar_id": event.calendar_id,
        "calendar_name": calendar.name,
        "calendar_color": calendar.color,
        "organizer_user_id": event.created_by,
        "organizer_username": calendar.owner_username,
        "summary": event.summary,
        "description": event.description,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "all_day": event.all_day,
        "rrule": event.rrule,
        "rsvp_enabled": event.rsvp_enabled,
        "cover_url": event.cover_url,
        "location": event.location,
        # IANA wall-clock anchor — old peers without this field ignore
        # it on the inbound side (their importer uses ``dict.get("tz")``
        # with a ``"UTC"`` fallback), so the field is additive.
        "tz": event.tz,
        # Client-stamped grouping uuid (issue #327). Optional on the
        # wire: receivers that ignore unknown payload keys just
        # persist ``NULL`` and the SPA's content-key fallback covers
        # the legacy case. When both sides carry it the agenda's
        # grouper merges by intent across the household boundary.
        "client_event_uuid": event.client_event_uuid,
    }


class SpaceCalendarService(BusPublisherMixin, ProtectionGateMixin, ContentAccessMixin):
    """Space calendar event operations.

    Event create / edit / delete pass the space's ``calendar`` access level
    (§4.3, :class:`ContentAccessMixin`) for the acting user; RSVPs and
    reminders are never gated. Under ``MODERATED`` a member's new event,
    and their edit / delete of somebody else's, waits in the space
    moderation queue (:class:`CalendarModerationHandler` replays it on
    approval, gated as the approver via ``approved_by``).
    """

    __slots__ = (
        "_repo",
        "_bus",
        "_federation",
        "_space_repo",
        "_household",
        "_child_protection",
        "_moderation",
    )

    def __init__(
        self,
        space_calendar_repo: AbstractSpaceCalendarRepo,
        bus: EventBus | None = None,
    ) -> None:
        self._repo = space_calendar_repo
        self._bus = bus
        self._federation = None
        # Optional helpers used solely for the tz resolution chain at
        # event create time. Tests that don't touch tz can skip both.
        self._space_repo: AbstractSpaceRepo | None = None
        self._household = None
        self._child_protection = None
        self._moderation: "ModerationSubmitter | None" = None

    # ── Subscription feed tokens (Phase F) ───────────────────────────────

    async def issue_feed_token(self, *, user_id: str, space_id: str) -> str:
        """Mint (or replace) *user_id*'s subscription token for *space_id*.

        Returns the raw token; storage keeps only its hash. The caller has
        already checked space membership. §CP.R: a protected account gets
        no subscription link — the URL is its own credential and exposes
        the account's schedule to whoever holds it.
        """
        await self._require_unrestricted(user_id, ProtectedCapability.CALENDAR_FEEDS)
        token = secrets.token_urlsafe(32)
        await self._repo.upsert_feed_token(
            user_id=user_id, space_id=space_id, token=token
        )
        return token

    async def resolve_feed_token(self, token: str) -> tuple[str, str] | None:
        """``(user_id, space_id)`` for a live feed token, else ``None``.

        A token minted before its account was protected stops serving
        (§CP.R at use time) without being deleted, so lifting protection
        restores the subscription.
        """
        owner = await self._repo.get_user_for_feed_token(token)
        if owner is None:
            return None
        if await self._is_restricted(owner[0], ProtectedCapability.CALENDAR_FEEDS):
            return None
        return owner

    def attach_federation(self, federation_service) -> None:
        """Wire outbound federation for RSVPs.

        ``federation_service`` is the :class:`FederationService`. RSVP
        propagation rides
        :meth:`FederationService.broadcast_to_space_members` so we
        automatically reach every peer co-hosting the space.
        """
        self._federation = federation_service

    def attach_household_features(self, svc) -> None:
        """Wire :class:`PreferencesService` so newly-created
        space events fall back to the household tz when neither the
        request nor the space row carries one."""
        self._household = svc

    def attach_space_repo(self, space_repo: AbstractSpaceRepo) -> None:
        """Wire the space repo so event creation can read ``space.tz``
        as the natural fallback before the household tz."""
        self._space_repo = space_repo

    async def _resolve_space_event_tz(
        self,
        explicit: str | None,
        *,
        space_id: str,
    ) -> str:
        """Pick the IANA tz for a new space calendar event.

        Chain: explicit → ``space.tz`` → household ``tz`` → ``"UTC"``.
        An unknown explicit IANA name falls back to the next layer and
        logs a warning — the SPA always sends a value detected by
        ``Intl`` so a malformed string here is a hand-rolled API
        client's fault, not a normal flow.
        """
        if explicit:
            if is_valid_tz(explicit):
                return explicit
            log.warning(
                "space calendar create_event got unknown tz %r — falling "
                "back to space / household",
                explicit,
            )
        if self._space_repo is not None:
            space = await self._space_repo.get(space_id)
            if space is not None and space.tz and space.tz != "UTC":
                return space.tz
        if self._household is not None:
            household = await self._household.get_household()
            return household.tz
        return "UTC"

    def wire(self) -> None:
        """Phase E: subscribe to SpaceMemberLeft so a user leaving a
        space cleans up their RSVPs on its events.

        Idempotent. Call once at app startup; the bus de-duplicates
        subscribers internally."""
        if self._bus is None:
            return
        self._bus.subscribe(SpaceMemberLeft, self._on_member_left)

    async def _on_member_left(self, event: SpaceMemberLeft) -> None:
        """When a member leaves a space, drop their RSVPs on its events.

        We list the space's events for the next-year window (matches the
        lifetime of typical space content) and remove the user's RSVP
        for each. RSVPs on far-future recurring events outside the
        window are left in place — they expire naturally via
        ``ON DELETE CASCADE`` when the event is eventually deleted.
        """
        events = await self._repo.list_events_in_range(
            event.space_id,
            start=datetime.now(timezone.utc) - timedelta(days=1),
            end=datetime.now(timezone.utc) + timedelta(days=365),
        )
        # `list_events_in_range` may return virtual occurrences (id
        # suffixed with ``@<iso>``) for recurring events; canonical
        # rows only here.
        seen: set[str] = set()
        for ev in events:
            base_id = ev.id.split("@", 1)[0]
            if base_id in seen:
                continue
            seen.add(base_id)
            # Drop every per-occurrence row for this user — fetch their
            # rows for the event and delete each.
            user_rows = [
                r
                for r in await self._repo.list_rsvps(base_id)
                if r.user_id == event.user_id
            ]
            for r in user_rows:
                await self._repo.remove_rsvp(
                    base_id,
                    event.user_id,
                    occurrence_at=r.occurrence_at,
                    space_id=event.space_id,
                )
                await self._publish_rsvp_changed(
                    space_id=event.space_id,
                    event_id=base_id,
                    user_id=event.user_id,
                    occurrence_at=r.occurrence_at,
                    status=None,
                )

    async def list_events_in_range(
        self,
        space_id: str,
        *,
        start: str,
        end: str,
    ) -> list[CalendarEvent]:
        def _parse_dt(value: str) -> datetime:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"invalid datetime: {value!r}") from exc

        return await self._repo.list_events_in_range(
            space_id,
            start=_parse_dt(start),
            end=_parse_dt(end),
        )

    async def create_event(
        self,
        *,
        space_id: str,
        summary: str,
        start: str,
        end: str,
        created_by: str,
        description: str | None = None,
        all_day: bool = False,
        attendees: tuple[str, ...] = (),
        rrule: str | None = None,
        capacity: int | None = None,
        cover_url: str | None = None,
        location: str | None = None,
        tz: str | None = None,
        announce_in_feed: bool = False,
        event_id: str | None = None,
        approved_by: str | None = None,
    ) -> CalendarEvent:
        """Create a space-scoped calendar event.

        ``capacity`` (Phase C): when set, "going" RSVPs require host
        approval and overflow lands on a waitlist. The creator is
        auto-RSVP'd as ``going`` for the first occurrence and counts
        toward capacity.

        ``announce_in_feed`` (§23.15): when False (default) the event
        lives only in the Calendar tab; when True the bridge also mirrors
        it as a ``PostType.EVENT`` post in the feed.
        """
        summary = (summary or "").strip()
        if not summary:
            raise ValueError("summary must not be empty")
        if capacity is not None and capacity < 0:
            raise ValueError("capacity must be >= 0")
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid datetime: {exc}") from exc
        if end_dt < start_dt:
            raise ValueError("end must be at or after start")
        decision = await self._gate(
            space_id,
            approved_by or created_by,
            "calendar",
            ContentAction.CREATE,
            approved_by is None,
        )
        clean_cover = _clean_cover_url(cover_url)
        clean_location = _clean_location(location)
        if decision is AccessDecision.QUEUE:
            await self._submit_for_review(
                space_id,
                created_by,
                "calendar",
                ContentAction.CREATE,
                payload={
                    "entity": "event",
                    "target_id": _mint_event_id(space_id, created_by),
                    "summary": summary,
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "description": description,
                    "all_day": bool(all_day),
                    "attendees": list(attendees),
                    "rrule": rrule,
                    "capacity": capacity,
                    "cover_url": clean_cover,
                    "location": clean_location,
                    "tz": tz,
                    "announce_in_feed": bool(announce_in_feed),
                },
            )
        queue_announce = False
        # A release of a REMOTE member's event (an approver household that is
        # not the creator's, v_43) cannot speak for the creator: no announce
        # card is queued in their name and no "going" RSVP is recorded for
        # them — their own household could accept neither.
        speaks_for_creator = (
            approved_by is None
            or await self._spaces_or_raise().get_member(space_id, created_by)
            is not None
        )
        if announce_in_feed and not speaks_for_creator:
            announce_in_feed = False
        if announce_in_feed:
            refusal = await self.announce_refusal(space_id, created_by)
            if refusal:
                # The feed mirror is a post: a creator the ``posts`` level
                # keeps from posting straight away keeps the event, never
                # the announcement. Dropped at the source so the flag
                # federates as the household meant it; every receiver's
                # ``CalendarFeedBridge`` re-checks it anyway. Under
                # MODERATED the card is queued for review as a post linked
                # to the event instead (below); the route tells the creator.
                announce_in_feed = False
                queue_announce = refusal == "moderated"
        event_tz = await self._resolve_space_event_tz(tz, space_id=space_id)
        event = CalendarEvent(
            # Owner-bound (v_36): no other household can announce this id
            # first as its own user's event.
            id=event_id or _mint_event_id(space_id, created_by),
            calendar_id=space_id,  # space-scoped events use space_id as calendar_id
            summary=summary,
            description=description,
            start=start_dt,
            end=end_dt,
            all_day=all_day,
            attendees=tuple(attendees),
            created_by=created_by,
            rrule=rrule,
            capacity=capacity,
            cover_url=clean_cover,
            location=clean_location,
            tz=event_tz,
            announce_in_feed=announce_in_feed,
        )
        if not await self._repo.save_event(event, space_id=space_id):
            raise KeyError(f"calendar event {event.id!r} exists in another space")
        saved = event
        await self._emit(CalendarEventCreated(event=saved))
        if queue_announce:
            await self._queue_announce_card(space_id, saved)
        await self._publish_federation_event_saved(
            space_id=space_id,
            event=saved,
            evt_type=FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
            actor_user_id=created_by,
        )
        # Phase C: auto-RSVP the creator as going for the first
        # occurrence — they're implicitly going, even on a capped event
        # (skips the approval flow for self). Publish SpaceRsvpChanged
        # too so the personal-calendar mirror appears for the creator
        # without them having to RSVP again.
        if not speaks_for_creator:
            return saved
        await self._repo.upsert_rsvp(
            CalendarRSVP(
                event_id=saved.id,
                user_id=created_by,
                status=RSVPStatus.GOING,
                updated_at=datetime.now(timezone.utc).isoformat(),
                occurrence_at=start_dt.isoformat(),
            ),
            space_id=space_id,
        )
        await self._publish_rsvp_changed(
            space_id=space_id,
            event_id=saved.id,
            user_id=created_by,
            occurrence_at=start_dt.isoformat(),
            status=RSVPStatus.GOING,
        )
        return saved

    # ── Writer gate + scope guard ────────────────────────────────────────

    def _access_space_repo(self) -> AbstractSpaceRepo:
        """The :class:`ContentAccessMixin` gate fails closed without it."""
        return self._spaces_or_raise()

    async def announce_refusal(self, space_id: str, user_id: str) -> str | None:
        """Why ``user_id``'s event can't be announced in the feed: the
        ``posts`` level (``"admin_only"`` / ``"moderated"``) that keeps them
        from posting straight away, else ``None``. The feed mirror of an
        announced event is a post that never queues (``CalendarFeedBridge``
        applies the same rule on every household)."""
        try:
            decision = await self._gate(
                space_id, user_id, "posts", ContentAction.CREATE, True
            )
        except SpacePermissionError:
            decision = AccessDecision.DENY
        if decision is AccessDecision.PROCEED:
            return None
        space = await self._spaces_or_raise().get(space_id)
        return space.features.access_level("posts").value if space else None

    async def _queue_announce_card(self, space_id: str, event: CalendarEvent) -> None:
        """Queue the feed card of an event whose creator's posts are
        reviewed: a ``posts`` CREATE item linked to the event. The event
        itself is already saved; approving the item makes the card appear.
        Where the card can't queue (a household that doesn't hold the
        queue, a full queue) it is dropped, as before."""
        if self._moderation is None:
            return
        space = await self._spaces_or_raise().get(space_id)
        if space is None:
            return
        try:
            await self._moderation.submit(
                space,
                feature="posts",
                action=ContentAction.CREATE,
                submitted_by=event.created_by,
                payload={
                    "entity": "post",
                    # Owner-bound (v_36) to the event's creator, like any
                    # member post: the card stays theirs when approved.
                    "target_id": mint_owner_bound_id(
                        SPACE_POST_KIND,
                        space_id=space_id,
                        owner_user_id=event.created_by,
                    ),
                    "post_id": None,
                    "type": "event",
                    "content": event.summary,
                    "linked_event_id": event.id,
                },
            )
        except (
            HostTooOldError,
            ModerationQueueFullError,
            SpacePermissionError,
        ) as exc:
            log.info("calendar: announce card for %s not queued: %s", event.id, exc)

    async def can_edit_event(
        self, space_id: str, user_id: str, event: CalendarEvent
    ) -> bool:
        """Whether :meth:`update_event` would let ``user_id`` change
        ``event`` — a UI hint (the edit control); the write re-checks."""
        (allowed,) = await self.can_edit_events(space_id, user_id, [event])
        return allowed

    async def can_edit_events(
        self, space_id: str, user_id: str, events: Sequence[CalendarEvent]
    ) -> list[bool]:
        """:meth:`can_edit_event` for each of ``events``, in order — the
        edit / delete hint of a whole listing. One writer check and at most
        two access-level checks (the caller's own events, and everybody
        else's): the decision only depends on ownership. A held-for-review
        change (§4.3 "Reviewed") counts as allowed — the write queues."""
        if not events or not await self.can_write(space_id, user_id):
            return [False] * len(events)
        by_ownership: dict[bool, bool] = {}
        hints: list[bool] = []
        for event in events:
            own = event.created_by == user_id
            if own not in by_ownership:
                try:
                    await self._gate(
                        space_id, user_id, "calendar", ContentAction.EDIT, own
                    )
                    by_ownership[own] = True
                except SpacePermissionError:
                    by_ownership[own] = False
            hints.append(by_ownership[own])
        return hints

    def _spaces_or_raise(self) -> AbstractSpaceRepo:
        # Fail closed: a write gate without the space repo can't decide.
        if self._space_repo is None:
            raise RuntimeError("SpaceCalendarService: space repo not attached")
        return self._space_repo

    async def require_writable_space(self, space_id: str) -> Space:
        """The space, unless it is unknown / dissolved (:class:`KeyError`)
        or archived (read-only → :class:`SpacePermissionError`).

        The space half of :meth:`require_writer`; used alone for the
        legacy remote-invite RSVP path that has no local member row.
        """
        space = await self._spaces_or_raise().get(space_id)
        if space is None or space.dissolved:
            raise KeyError(f"space {space_id!r} not found")
        if space.archived:
            raise SpaceArchivedError()
        return space

    async def require_writer(
        self, space_id: str, user_id: str, *, action: str = "edit the calendar"
    ) -> None:
        """Raise unless ``user_id`` may write this space's calendar.

        ``action`` names the write in the subscriber refusal message
        (e.g. ``"RSVP to events"``).

        * unknown / dissolved space → :class:`KeyError` (404);
        * archived space (read-only) → :class:`SpacePermissionError`;
        * not a member, or a read-only subscriber →
          :class:`SpacePermissionError` (403), via the uniform
          :meth:`SpaceService.assert_writable_member` rule.

        Covers event create / edit / delete, RSVPs and host approvals —
        every write that lands in ``space_calendar_*`` and federates.
        Fails closed when the space repo was never attached.
        """
        space = await self.require_writable_space(space_id)
        member = await self._spaces_or_raise().get_member(space_id, user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
        SpaceService.assert_writable_member(member, action=action, space=space)

    async def can_write(self, space_id: str, user_id: str) -> bool:
        """Whether :meth:`require_writer` would let ``user_id`` write —
        a UI hint (e.g. hide RSVP controls for subscribers / archived
        spaces), never the authority: every write still calls
        :meth:`require_writer`."""
        try:
            await self.require_writer(space_id, user_id)
        except KeyError, SpacePermissionError:
            return False
        return True

    async def get_event_in_space(
        self, event_id: str, *, space_id: str
    ) -> CalendarEvent:
        """The event ``event_id`` of space ``space_id``.

        An event id says nothing about its space, so every by-id
        operation under ``/api/spaces/{id}/calendar`` takes the PATH
        space and refuses a row living in another one (§24.11) —
        :class:`KeyError` (→ 404), never 403, so the response doesn't
        confirm the id exists elsewhere.
        """
        result = await self._repo.get_event(event_id)
        if result is None or result[0] != space_id:
            raise KeyError(f"space calendar event {event_id!r} not found in this space")
        return result[1]

    async def delete_event(
        self,
        event_id: str,
        *,
        space_id: str,
        actor_user_id: str,
        approved_by: str | None = None,
    ) -> None:
        """Delete ``event_id`` from ``space_id`` — :class:`KeyError` when
        the event isn't in that space."""
        existing = await self.get_event_in_space(event_id, space_id=space_id)
        decision = await self._gate(
            space_id,
            approved_by or actor_user_id,
            "calendar",
            ContentAction.DELETE,
            approved_by is None and existing.created_by == actor_user_id,
        )
        if decision is AccessDecision.QUEUE:
            await self._submit_for_review(
                space_id,
                actor_user_id,
                "calendar",
                ContentAction.DELETE,
                payload={"entity": "event", "target_id": existing.id},
                snapshot=_event_dict(existing),
            )
        # Snapshot the event + the cohort of "still attending"-ish RSVPs
        # before deletion so the push handler can produce a meaningful
        # title and reach affected members. The RSVP rows themselves
        # CASCADE-delete with the event.
        rsvps = await self._repo.list_rsvps(event_id)
        notify = tuple(
            {
                r.user_id
                for r in rsvps
                if r.status
                in (
                    RSVPStatus.GOING,
                    RSVPStatus.WAITLIST,
                    RSVPStatus.REQUESTED,
                )
            }
        )
        if not await self._repo.delete_event(event_id, space_id=space_id):
            raise KeyError(f"space calendar event {event_id!r} not found in this space")
        await self._emit(
            CalendarEventDeleted(
                event_id=event_id,
                summary=existing.summary,
                space_id=space_id,
                notify_user_ids=notify,
            )
        )
        await self._publish_federation_event_deleted(
            space_id=space_id,
            event_id=event_id,
            actor_user_id=actor_user_id,
        )

    async def resolve_space_id(self, event_id: str) -> str | None:
        """Return the ``space_id`` that owns ``event_id`` or None.

        Used by the RSVP route to gate voters on space membership and
        to scope WS broadcasts to co-members.
        """
        result = await self._repo.get_event(event_id)
        if result is None:
            return None
        space_id, _event = result
        return space_id

    async def update_event(
        self,
        event_id: str,
        *,
        space_id: str,
        actor_user_id: str,
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        all_day: bool | None = None,
        description: str | None = None,
        attendees: tuple[str, ...] | None = None,
        rrule: str | None = None,
        capacity: int | None = None,
        clear_capacity: bool = False,
        cover_url: object = _UNSET,
        location: object = _UNSET,
        tz: str | None = None,
        approved_by: str | None = None,
    ) -> CalendarEvent:
        """Partial-update a space event. Emits CalendarEventUpdated.

        ``cover_url`` and ``location`` follow the same sentinel
        discipline as the personal calendar's ``update_event``: ``_UNSET``
        = no change, explicit ``None`` clears the field, a string sets it.
        ``tz`` is validated against the IANA database and only overwrites
        the existing event tz when explicitly passed.

        ``space_id`` is the caller's (path) space: an event living in
        another space is :class:`KeyError` (→ 404) and nothing changes.
        """
        existing = await self.get_event_in_space(event_id, space_id=space_id)
        decision = await self._gate(
            space_id,
            approved_by or actor_user_id,
            "calendar",
            ContentAction.EDIT,
            approved_by is None and existing.created_by == actor_user_id,
        )

        new_summary = (summary if summary is not None else existing.summary).strip()
        if not new_summary:
            raise ValueError("event summary must not be empty")
        new_start = _parse_iso(start) if start else existing.start
        new_end = _parse_iso(end) if end else existing.end
        if new_end < new_start:
            raise ValueError("event end must not be before start")

        if capacity is not None and capacity < 0:
            raise ValueError("capacity must be >= 0")
        new_capacity = (
            None
            if clear_capacity
            else (capacity if capacity is not None else existing.capacity)
        )
        if cover_url is _UNSET:
            new_cover = existing.cover_url
        else:
            # mypy can't narrow ``object`` past the ``is _UNSET`` guard,
            # but the route layer only ever passes ``str | None`` when
            # the field is present in the body — assert + cast here.
            assert cover_url is None or isinstance(cover_url, str)
            new_cover = _clean_cover_url(cover_url)
        if location is _UNSET:
            new_location = existing.location
        else:
            assert location is None or isinstance(location, str)
            new_location = _clean_location(location)
        if tz is not None:
            if not is_valid_tz(tz):
                raise ValueError(f"unknown IANA timezone {tz!r}")
            new_tz = tz
        else:
            new_tz = existing.tz
        updated = replace(
            existing,
            summary=new_summary,
            start=new_start,
            end=new_end,
            all_day=bool(all_day) if all_day is not None else existing.all_day,
            description=description
            if description is not None
            else existing.description,
            attendees=tuple(attendees) if attendees is not None else existing.attendees,
            rrule=rrule if rrule is not None else existing.rrule,
            capacity=new_capacity,
            cover_url=new_cover,
            location=new_location,
            tz=new_tz,
        )
        if decision is AccessDecision.QUEUE:
            # Validated above; the patch carries exactly what was sent.
            sent: dict[str, Any] = {
                k: v
                for k, v in (
                    ("summary", summary),
                    ("start", start),
                    ("end", end),
                    ("all_day", all_day),
                    ("description", description),
                    ("attendees", list(attendees) if attendees is not None else None),
                    ("rrule", rrule),
                    ("capacity", capacity),
                    ("tz", tz),
                )
                if v is not None
            }
            if clear_capacity:
                sent["clear_capacity"] = True
            if cover_url is not _UNSET:
                sent["cover_url"] = new_cover
            if location is not _UNSET:
                sent["location"] = new_location
            if not sent:
                raise ValueError("an edit needs at least one field")
            after = _event_dict(updated)
            before = _event_dict(existing)
            shown = [k for k in sent if k in before] + (
                ["capacity"] if clear_capacity else []
            )
            await self._submit_for_review(
                space_id,
                actor_user_id,
                "calendar",
                ContentAction.EDIT,
                payload={
                    "entity": "event",
                    "target_id": existing.id,
                    "patch": sent,
                    "proposed": {k: after[k] for k in shown},
                },
                snapshot={k: before[k] for k in shown},
            )
        if not await self._repo.save_event(updated, space_id=space_id):
            raise KeyError(f"calendar event {updated.id!r} not found in this space")
        await self._publish_federation_event_saved(
            space_id=space_id,
            event=updated,
            evt_type=FederationEventType.SPACE_CALENDAR_EVENT_UPDATED,
            actor_user_id=actor_user_id,
        )
        # Compute *material* field changes — Phase D: only these
        # trigger update push notifications. Cosmetic changes (description,
        # attendees, rrule, all_day) are silent.
        changes: list[str] = []
        if existing.summary != updated.summary:
            changes.append("summary")
        if existing.start != updated.start:
            changes.append("start")
        if existing.end != updated.end:
            changes.append("end")
        # Capacity going *down* is material (it kicks people out / changes
        # waitlist semantics). Capacity going up is silent — promotion
        # happens automatically.
        if (
            existing.capacity is not None
            and new_capacity is not None
            and new_capacity < existing.capacity
        ):
            changes.append("capacity_down")
        await self._emit(
            CalendarEventUpdated(
                event=updated,
                material_changes=tuple(changes),
            )
        )
        # Capacity raised — promote from waitlist to fill new seats.
        if (
            existing.capacity is not None
            and new_capacity is not None
            and new_capacity > existing.capacity
        ):
            await self._auto_promote_waitlist(
                space_id=space_id,
                event=updated,
                occ_iso=updated.start.isoformat(),
            )
        return updated

    async def rsvp(
        self,
        *,
        event_id: str,
        user_id: str,
        status: str,
        occurrence_at: datetime | str | None = None,
    ) -> None:
        """Set ``user_id``'s RSVP for an occurrence of ``event_id``.

        For non-recurring events, ``occurrence_at`` may be omitted —
        defaults to ``event.start``. For recurring events
        (``rrule != None``) it must be provided and must reach a real
        occurrence under the event's rrule (validated via
        :func:`expand_rrule`).
        """
        if status not in RSVPStatus.USER_SETTABLE:
            raise ValueError(
                f"RSVP status must be one of {sorted(RSVPStatus.USER_SETTABLE)}, "
                f"got {status!r}"
            )
        result = await self._repo.get_event(event_id)
        if result is None:
            raise KeyError(f"event {event_id!r} not found")
        space_id, event = result
        occ_dt = self._resolve_occurrence(event, occurrence_at)
        # Phase E: past-event RSVP lock. Once an occurrence has *ended*
        # (its window is fully in the past), responding to it is a
        # write into the past — reject. We compare against
        # ``occurrence_end = occ_dt + (event.end - event.start)`` so
        # recurring events get the correct per-occurrence window.
        # The creator's auto-RSVP at create_event time goes through a
        # different code path so this guard doesn't block it.
        duration = event.end - event.start
        if occ_dt + duration < datetime.now(timezone.utc):
            raise RsvpPastError()
        occ_iso = occ_dt.isoformat()
        now_iso = datetime.now(timezone.utc).isoformat()
        # Phase C: capacity-aware "going" routing. Only "going" is
        # affected — "maybe" never counts toward capacity, "declined"
        # frees a slot (which then auto-promotes the waitlist below).
        effective_status = status
        if (
            event.capacity is not None
            and status == RSVPStatus.GOING
            and user_id != event.created_by
        ):
            existing = await self._existing_rsvp(event_id, user_id, occ_iso)
            if existing != RSVPStatus.GOING:
                effective_status = await self._route_capped_going(
                    event=event,
                    user_id=user_id,
                    occ_iso=occ_iso,
                )
        await self._repo.upsert_rsvp(
            CalendarRSVP(
                event_id=event_id,
                user_id=user_id,
                status=effective_status,
                updated_at=now_iso,
                occurrence_at=occ_iso,
            ),
            space_id=space_id,
        )
        await self._publish_federation_rsvp(
            space_id=space_id,
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_iso,
            status=effective_status,
            updated_at=now_iso,
        )
        await self._publish_rsvp_changed(
            space_id=space_id,
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_iso,
            status=effective_status,
        )
        # If this RSVP frees a seat (declined / maybe replacing a
        # previous "going"), promote the oldest waitlist row.
        if status != RSVPStatus.GOING and event.capacity is not None:
            await self._auto_promote_waitlist(
                space_id=space_id,
                event=event,
                occ_iso=occ_iso,
            )

    async def remove_rsvp(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: datetime | str | None = None,
    ) -> None:
        """Clear ``user_id``'s RSVP for an occurrence of ``event_id``."""
        result = await self._repo.get_event(event_id)
        if result is None:
            return
        space_id, event = result
        occ_dt = self._resolve_occurrence(event, occurrence_at)
        occ_iso = occ_dt.isoformat()
        await self._repo.remove_rsvp(
            event_id,
            user_id,
            occurrence_at=occ_iso,
            space_id=space_id,
        )
        await self._publish_federation_rsvp(
            space_id=space_id,
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_iso,
            status=None,  # signals delete
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._publish_rsvp_changed(
            space_id=space_id,
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_iso,
            status=None,
        )
        # Removing a "going" RSVP frees a seat — promote from waitlist.
        if event.capacity is not None:
            await self._auto_promote_waitlist(
                space_id=space_id,
                event=event,
                occ_iso=occ_iso,
            )

    async def list_rsvps(
        self,
        event_id: str,
        *,
        occurrence_at: datetime | str | None = None,
    ) -> list[CalendarRSVP]:
        """List RSVPs for ``event_id``.

        If ``occurrence_at`` is provided, returns only the RSVPs for
        that single occurrence. Otherwise returns the rows across all
        occurrences (callers wanting per-occurrence aggregates should
        group on ``rsvp.occurrence_at``).
        """
        if occurrence_at is None:
            return await self._repo.list_rsvps(event_id)
        occ_iso = (
            occurrence_at.isoformat()
            if isinstance(occurrence_at, datetime)
            else str(occurrence_at)
        )
        return await self._repo.list_rsvps(event_id, occurrence_at=occ_iso)

    # ── Reminders (Phase D) ──────────────────────────────────────────────

    async def add_reminder(
        self,
        *,
        event_id: str,
        user_id: str,
        minutes_before: int,
        occurrence_at: datetime | str | None = None,
    ) -> EventReminder:
        """Schedule a reminder for ``user_id`` on a specific occurrence.

        ``minutes_before`` is the offset; ``fire_at`` is computed as
        ``occurrence - minutes_before``. The reminder lives in the
        scheduler's queue until either delivered or removed.
        """
        if minutes_before < 0:
            raise ValueError("minutes_before must be >= 0")
        result = await self._repo.get_event(event_id)
        if result is None:
            raise KeyError(f"event {event_id!r} not found")
        _space_id, event = result
        occ_dt = self._resolve_occurrence(event, occurrence_at)
        fire_at_dt = occ_dt - timedelta(minutes=minutes_before)
        reminder = EventReminder(
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_dt.isoformat(),
            minutes_before=int(minutes_before),
            fire_at=fire_at_dt.isoformat(),
        )
        await self._repo.upsert_reminder(reminder)
        return reminder

    async def remove_reminder(
        self,
        *,
        event_id: str,
        user_id: str,
        minutes_before: int,
        occurrence_at: datetime | str | None = None,
    ) -> None:
        result = await self._repo.get_event(event_id)
        if result is None:
            return
        _space_id, event = result
        occ_dt = self._resolve_occurrence(event, occurrence_at)
        await self._repo.remove_reminder(
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_dt.isoformat(),
            minutes_before=int(minutes_before),
        )

    async def list_reminders(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: datetime | str | None = None,
    ) -> list[EventReminder]:
        occ_iso: str | None
        if occurrence_at is None:
            occ_iso = None
        else:
            result = await self._repo.get_event(event_id)
            if result is None:
                return []
            _space_id, event = result
            occ_dt = self._resolve_occurrence(event, occurrence_at)
            occ_iso = occ_dt.isoformat()
        return await self._repo.list_reminders(
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_iso,
        )

    # ── Capacity / request-to-join (Phase C) ─────────────────────────────

    async def list_pending(
        self,
        event_id: str,
        *,
        occurrence_at: datetime | str | None = None,
    ) -> list[CalendarRSVP]:
        """List `requested` RSVPs awaiting host approval for an event.

        Approver-side helper — the route gates this on actor approver-
        status before invoking. With ``occurrence_at`` returns just that
        instance; without, returns all pending across occurrences.
        """
        rsvps = await self.list_rsvps(event_id, occurrence_at=occurrence_at)
        return [r for r in rsvps if r.status == RSVPStatus.REQUESTED]

    async def approve_rsvp(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: datetime | str | None = None,
    ) -> str:
        """Approve a pending request-to-join.

        Promotes the user to ``going`` if a seat is free, otherwise to
        ``waitlist``. Returns the resulting status. Raises
        :class:`KeyError` if the event or RSVP doesn't exist.

        The approver gate (event creator OR space admin) is enforced at
        the route layer because it needs the actor's space-membership
        which the calendar service doesn't depend on.
        """
        result = await self._repo.get_event(event_id)
        if result is None:
            raise KeyError(f"event {event_id!r} not found")
        space_id, event = result
        occ_dt = self._resolve_occurrence(event, occurrence_at)
        occ_iso = occ_dt.isoformat()
        existing = await self._existing_rsvp(event_id, user_id, occ_iso)
        if existing != RSVPStatus.REQUESTED:
            raise KeyError(
                f"no pending request for user {user_id!r} on this occurrence",
            )
        new_status = (
            RSVPStatus.GOING
            if event.capacity is None
            or await self._going_count(event_id, occ_iso) < event.capacity
            else RSVPStatus.WAITLIST
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        await self._repo.upsert_rsvp(
            CalendarRSVP(
                event_id=event_id,
                user_id=user_id,
                status=new_status,
                updated_at=now_iso,
                occurrence_at=occ_iso,
            ),
            space_id=space_id,
        )
        await self._publish_federation_rsvp(
            space_id=space_id,
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_iso,
            status=new_status,
            updated_at=now_iso,
        )
        await self._publish_rsvp_changed(
            space_id=space_id,
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_iso,
            status=new_status,
        )
        return new_status

    async def deny_rsvp(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: datetime | str | None = None,
    ) -> None:
        """Deny a pending request-to-join — removes the row entirely."""
        result = await self._repo.get_event(event_id)
        if result is None:
            raise KeyError(f"event {event_id!r} not found")
        _space_id, event = result
        occ_dt = self._resolve_occurrence(event, occurrence_at)
        occ_iso = occ_dt.isoformat()
        existing = await self._existing_rsvp(event_id, user_id, occ_iso)
        if existing != RSVPStatus.REQUESTED:
            raise KeyError(
                f"no pending request for user {user_id!r} on this occurrence",
            )
        # Reuse remove_rsvp so federation propagation + waitlist
        # promotion (no-op for a denied REQUESTED row) all happen.
        await self.remove_rsvp(
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_dt,
        )

    async def _existing_rsvp(
        self,
        event_id: str,
        user_id: str,
        occ_iso: str,
    ) -> str | None:
        for r in await self._repo.list_rsvps(
            event_id,
            occurrence_at=occ_iso,
        ):
            if r.user_id == user_id:
                return r.status
        return None

    async def _going_count(self, event_id: str, occ_iso: str) -> int:
        return sum(
            1
            for r in await self._repo.list_rsvps(
                event_id,
                occurrence_at=occ_iso,
            )
            if r.status == RSVPStatus.GOING
        )

    async def _route_capped_going(
        self,
        *,
        event: CalendarEvent,
        user_id: str,
        occ_iso: str,
    ) -> str:
        """For a capped event, return the effective status when a member
        attempts to set ``going``: always REQUESTED first (host
        approval). Capacity-vs-waitlist routing happens at approval
        time."""
        return RSVPStatus.REQUESTED

    async def _auto_promote_waitlist(
        self,
        *,
        space_id: str,
        event: CalendarEvent,
        occ_iso: str,
    ) -> None:
        """If an occurrence has free capacity, promote the oldest
        ``waitlist`` row to ``going``."""
        if event.capacity is None:
            return
        going = await self._going_count(event.id, occ_iso)
        if going >= event.capacity:
            return
        rows = await self._repo.list_rsvps(event.id, occurrence_at=occ_iso)
        candidates = [r for r in rows if r.status == RSVPStatus.WAITLIST]
        if not candidates:
            return
        candidates.sort(key=lambda r: r.updated_at)
        promoted = candidates[0]
        now_iso = datetime.now(timezone.utc).isoformat()
        await self._repo.upsert_rsvp(
            CalendarRSVP(
                event_id=promoted.event_id,
                user_id=promoted.user_id,
                status=RSVPStatus.GOING,
                updated_at=now_iso,
                occurrence_at=occ_iso,
            ),
            space_id=space_id,
        )
        await self._publish_federation_rsvp(
            space_id=space_id,
            event_id=promoted.event_id,
            user_id=promoted.user_id,
            occurrence_at=occ_iso,
            status=RSVPStatus.GOING,
            updated_at=now_iso,
        )
        await self._publish_rsvp_changed(
            space_id=space_id,
            event_id=promoted.event_id,
            user_id=promoted.user_id,
            occurrence_at=occ_iso,
            status=RSVPStatus.GOING,
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_occurrence(
        event: CalendarEvent,
        occurrence_at: datetime | str | None,
    ) -> datetime:
        """Validate / default ``occurrence_at`` against ``event``.

        * Non-recurring + omitted → ``event.start``.
        * Non-recurring + given matching ``event.start`` → accepted.
        * Recurring + omitted → :class:`ValueError`.
        * Recurring + given → must match one of the rrule's expanded
          occurrences within a 1-year window from the seed; otherwise
          :class:`ValueError`.
        """
        is_recurring = bool(event.rrule)
        if occurrence_at is None:
            if is_recurring:
                raise ValueError(
                    "recurring events require occurrence_at on RSVP",
                )
            return event.start
        occ_dt = (
            occurrence_at
            if isinstance(occurrence_at, datetime)
            else datetime.fromisoformat(str(occurrence_at).replace("Z", "+00:00"))
        )
        if not is_recurring:
            if occ_dt != event.start:
                raise ValueError(
                    "non-recurring events: occurrence_at must equal event.start",
                )
            return occ_dt
        # Recurring — check the rrule actually emits this occurrence.
        window_end = max(
            occ_dt + timedelta(seconds=1),
            event.start + timedelta(days=365 * 5),
        )
        starts = {
            s
            for s, _ in expand_rrule(
                event.start,
                event.end,
                event.rrule,
                window_start=event.start,
                window_end=window_end,
                tz=event.tz,
            )
        }
        if occ_dt not in starts:
            raise ValueError(
                f"occurrence_at {occ_dt.isoformat()} is not a valid occurrence "
                f"of event {event.id}",
            )
        return occ_dt

    async def _publish_federation_event_saved(
        self,
        *,
        space_id: str,
        event: CalendarEvent,
        evt_type: FederationEventType,
        actor_user_id: str,
    ) -> None:
        """Broadcast a SPACE_CALENDAR_EVENT_CREATED / _UPDATED to peers
        co-hosting this space. No-op when federation isn't wired."""
        if self._federation is None:
            return
        payload: dict = {
            # The space this write belongs to, in the payload as well as
            # the routing field. A mesh-relayed envelope (SPACE_ROUTED)
            # carries no plaintext routing ``space_id`` — a relay must not
            # learn which space is being served — so the payload's copy is
            # the only thing the receiver's §24.11 space-writer gate can
            # read there. Additive; older peers ignore it.
            "space_id": space_id,
            "event_id": event.id,
            "calendar_id": event.calendar_id,
            "summary": event.summary,
            "description": event.description,
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "all_day": event.all_day,
            "attendees": list(event.attendees),
            "created_by": event.created_by,
            "rrule": event.rrule,
            "cover_url": event.cover_url,
            "location": event.location,
            # IANA wall-clock anchor — additive on the wire. Old peers
            # without this field ignore it on the inbound side.
            "tz": event.tz,
            # §23.15 opt-in feed mirror. The receiver's CalendarFeedBridge
            # reads this to decide whether to mint a feed post. Absent on
            # an older sender → the receiver defaults to True (the historic
            # always-mirror behaviour) so events still surface there.
            "announce_in_feed": event.announce_in_feed,
            # v_42: who made the write — receivers check it against the
            # space's ``calendar`` access level. Older peers ignore it.
            "actor_user_id": actor_user_id,
        }
        await self._federation.broadcast_to_space_members(
            space_id,
            evt_type,
            # A write released from the moderation queue names it (v_43).
            with_release(payload),
        )

    async def _publish_federation_event_deleted(
        self,
        *,
        space_id: str,
        event_id: str,
        actor_user_id: str,
    ) -> None:
        if self._federation is None:
            return
        await self._federation.broadcast_to_space_members(
            space_id,
            FederationEventType.SPACE_CALENDAR_EVENT_DELETED,
            # ``space_id`` rides the payload too — see
            # :meth:`_publish_federation_event_saved`.
            with_release(
                {
                    "event_id": event_id,
                    "space_id": space_id,
                    "actor_user_id": actor_user_id,
                }
            ),
        )

    async def _publish_federation_rsvp(
        self,
        *,
        space_id: str,
        event_id: str,
        user_id: str,
        occurrence_at: str,
        status: str | None,
        updated_at: str,
    ) -> None:
        """Broadcast a SPACE_RSVP_UPDATED (or _DELETED) to every peer
        household co-hosting this space. No-op when federation isn't
        wired (unit tests / standalone mode)."""
        if self._federation is None:
            return
        evt_type = (
            FederationEventType.SPACE_RSVP_UPDATED
            if status is not None
            else FederationEventType.SPACE_RSVP_DELETED
        )
        payload: dict = {
            # ``space_id`` rides the payload too — see
            # :meth:`_publish_federation_event_saved`.
            "space_id": space_id,
            "event_id": event_id,
            "user_id": user_id,
            "occurrence_at": occurrence_at,
            "updated_at": updated_at,
        }
        if status is not None:
            payload["status"] = status
        await self._federation.broadcast_to_space_members(
            space_id,
            evt_type,
            payload,
        )

    async def _publish_rsvp_changed(
        self,
        *,
        space_id: str,
        event_id: str,
        user_id: str,
        occurrence_at: str,
        status: str | None,
    ) -> None:
        """Publish a :class:`SpaceRsvpChanged` domain event so the
        personal-mirror bridge (and any future subscriber) can react.
        No-op when the bus isn't wired."""
        if self._bus is None:
            return
        await self._bus.publish(
            SpaceRsvpChanged(
                event_id=event_id,
                space_id=space_id,
                user_id=user_id,
                occurrence_at=occurrence_at,
                status=status,
            )
        )


def _parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 string, tolerating the trailing ``Z`` form."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid datetime: {value!r}") from exc


def _mint_event_id(space_id: str, created_by: str) -> str:
    return mint_owner_bound_id(
        SPACE_CALENDAR_EVENT_KIND, space_id=space_id, owner_user_id=created_by
    )


def _event_dict(event: CalendarEvent) -> dict[str, Any]:
    """A space event's reviewable fields in their API form."""
    return {
        "summary": event.summary,
        "description": event.description,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "all_day": bool(event.all_day),
        "attendees": list(event.attendees),
        "rrule": event.rrule,
        "capacity": event.capacity,
        "cover_url": event.cover_url,
        "location": event.location,
        "tz": event.tz,
        "created_by": event.created_by,
    }


_EVENT_CREATE_FIELDS = (
    "summary",
    "start",
    "end",
    "description",
    "all_day",
    "attendees",
    "rrule",
    "capacity",
    "cover_url",
    "location",
    "tz",
    "announce_in_feed",
)

_EVENT_PATCH_FIELDS = frozenset(
    {
        "summary",
        "start",
        "end",
        "all_day",
        "description",
        "attendees",
        "rrule",
        "capacity",
        "clear_capacity",
        "cover_url",
        "location",
        "tz",
    }
)


class CalendarModerationHandler:
    """Queue items of a space calendar (``calendar`` create / edit /
    delete). Applies through :class:`SpaceCalendarService` gated as the
    approver; an edit re-applies only the fields that were sent."""

    __slots__ = ("_svc",)

    def __init__(self, service: SpaceCalendarService) -> None:
        self._svc = service

    def validate(self, space: Space, payload: dict) -> dict:
        if payload.get("entity") != "event" or not isinstance(
            payload.get("target_id"), str
        ):
            raise ValueError("not a calendar submission")
        out: dict[str, Any] = {"entity": "event", "target_id": payload["target_id"]}
        if "patch" in payload:
            raw = payload["patch"]
            if not isinstance(raw, dict) or not raw or set(raw) - _EVENT_PATCH_FIELDS:
                raise ValueError("invalid event edit")
            out["patch"] = dict(raw)
            proposed = payload.get("proposed")
            out["proposed"] = dict(proposed) if isinstance(proposed, dict) else {}
        elif "summary" in payload:
            out.update({k: payload.get(k) for k in _EVENT_CREATE_FIELDS})
        return out

    async def snapshot(self, space_id: str, target_id: str) -> dict | None:
        try:
            event = await self._svc.get_event_in_space(target_id, space_id=space_id)
        except KeyError:
            return None
        return _event_dict(event)

    async def held(self, space_id: str, target_id: str) -> dict | None:
        """:meth:`snapshot` plus the fields an edit never changes (the feed
        flag, the mirror origin, the calendar) — what a federated release
        is checked against (v_43)."""
        try:
            event = await self._svc.get_event_in_space(target_id, space_id=space_id)
        except KeyError:
            return None
        return {
            **_event_dict(event),
            "calendar_id": event.calendar_id,
            "mirrored_from": event.mirrored_from,
            "announce_in_feed": event.announce_in_feed,
        }

    async def apply(
        self, item: SpaceModerationItem, *, approved_by: str, force: bool
    ) -> ApplyResult:
        p = item.payload
        target = str(p["target_id"])
        live = await self.snapshot(item.space_id, target)
        match item.action:
            case ContentAction.CREATE.value:
                if live is None:
                    await self._svc.create_event(
                        space_id=item.space_id,
                        summary=p["summary"],
                        start=p["start"],
                        end=p["end"],
                        created_by=item.submitted_by,
                        description=p.get("description"),
                        all_day=bool(p.get("all_day")),
                        attendees=tuple(p.get("attendees") or ()),
                        rrule=p.get("rrule"),
                        capacity=p.get("capacity"),
                        cover_url=p.get("cover_url"),
                        location=p.get("location"),
                        tz=p.get("tz"),
                        announce_in_feed=bool(p.get("announce_in_feed")),
                        event_id=target,
                        approved_by=approved_by,
                    )
            case ContentAction.EDIT.value:
                if live is None:
                    raise ModerationTargetGoneError(target)
                patch = dict(p["patch"])
                extra: dict[str, Any] = {}
                if "cover_url" in patch:
                    extra["cover_url"] = patch.pop("cover_url")
                if "location" in patch:
                    extra["location"] = patch.pop("location")
                if "attendees" in patch:
                    patch["attendees"] = tuple(patch["attendees"])
                await self._svc.update_event(
                    target,
                    space_id=item.space_id,
                    actor_user_id=item.submitted_by,
                    approved_by=approved_by,
                    **patch,
                    **extra,
                )
            case ContentAction.DELETE.value:
                if live is not None:
                    await self._svc.delete_event(
                        target,
                        space_id=item.space_id,
                        actor_user_id=item.submitted_by,
                        approved_by=approved_by,
                    )
        return ApplyResult(target_id=target)

    def preview(self, item: SpaceModerationItem) -> dict:
        p = item.payload
        if item.action == ContentAction.EDIT.value:
            return dict(p.get("proposed") or p.get("patch") or {})
        if item.action == ContentAction.DELETE.value:
            return item_payload_snapshot(item) or {}
        return {k: p.get(k) for k in _EVENT_CREATE_FIELDS}
