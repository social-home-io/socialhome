"""Tests for :class:`PersonalCalendarInboundHandlers` (§23.60).

The inbound handler mirrors a remote organiser's invite into the
recipient's existing personal calendar with ``origin='remote_invite'``.
RSVP responses propagate back to the organiser's local row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from socialhome.domain.calendar import Calendar, CalendarEvent, CalendarRSVP
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.user import User
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.federation_inbound import (
    PersonalCalendarInboundHandlers,
)
from socialhome.services.federation_inbound.personal_calendar import (
    _mint_event_id,
)


class _Registry:
    def __init__(self) -> None:
        self.handlers: dict = {}

    def register(self, t, h):
        self.handlers[t] = h


class _FedSvc:
    def __init__(self) -> None:
        self._event_registry = _Registry()


class _FakeCalendarRepo:
    def __init__(self) -> None:
        self.calendars: dict[str, Calendar] = {}
        self.events: dict[str, CalendarEvent] = {}
        self.rsvps: dict = {}

    async def list_calendars_for_user(self, username):
        return [c for c in self.calendars.values() if c.owner_username == username]

    async def get_event(self, event_id):
        return self.events.get(event_id)

    async def get_event_by_remote(self, *, remote_instance_id, remote_event_id):
        for ev in self.events.values():
            if (
                ev.remote_instance_id == remote_instance_id
                and ev.remote_event_id == remote_event_id
            ):
                return ev
        return None

    async def save_event(self, event):
        self.events[event.id] = event
        return event

    async def delete_event(self, event_id):
        self.events.pop(event_id, None)

    async def upsert_rsvp(self, rsvp):
        self.rsvps[(rsvp.event_id, rsvp.user_id, rsvp.occurrence_at)] = rsvp

    async def remove_rsvp(self, event_id, user_id, *, occurrence_at=None):
        if occurrence_at is None:
            for k in [k for k in self.rsvps if k[0] == event_id and k[1] == user_id]:
                del self.rsvps[k]
        else:
            self.rsvps.pop((event_id, user_id, occurrence_at), None)


class _FakeUserRepo:
    def __init__(self) -> None:
        self.by_uid: dict[str, User] = {}
        #: user_id → home instance_id (local users map to ``"self"``).
        self.home: dict[str, str] = {}

    async def get_by_user_id(self, user_id):
        return self.by_uid.get(user_id)

    async def get_instance_for_user(self, user_id):
        return self.home.get(user_id)


def _envelope(event_type, payload, from_instance="i_remote"):
    return FederationEvent(
        msg_id="m1",
        event_type=event_type,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
    )


@pytest.fixture
def env():
    cal_repo = _FakeCalendarRepo()
    user_repo = _FakeUserRepo()
    bus = EventBus()
    handlers = PersonalCalendarInboundHandlers(
        bus=bus,
        calendar_repo=cal_repo,
        user_repo=user_repo,
    )
    fed = _FedSvc()
    handlers.attach_to(fed)

    # Recipient on this instance — has a personal calendar already.
    user_repo.by_uid["u-anna"] = User(
        username="anna",
        user_id="u-anna",
        display_name="Anna",
    )
    user_repo.home["u-anna"] = "self"
    # Invitees who live on the envelope's sending household.
    for uid in ("u-bob", "u-bob-remote"):
        user_repo.home[uid] = "i_remote"
    cal_repo.calendars["cal-anna"] = Calendar(
        id="cal-anna",
        name="Anna",
        color="#abcdef",
        owner_username="anna",
    )
    return fed, cal_repo, user_repo


async def test_inbound_invite_mirrors_into_recipient_calendar(env):
    fed, cal_repo, _ = env
    handler = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    now = datetime.now(timezone.utc)
    await handler(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
            {
                "event_id": "remote-evt-1",
                "summary": "BBQ at the Smiths'",
                "start": now.isoformat(),
                "end": (now + timedelta(hours=2)).isoformat(),
                "organizer_user_id": "u-bob-remote",
                "attendee_user_ids": ["u-anna"],
                "rsvp_enabled": True,
            },
        )
    )
    assert len(cal_repo.events) == 1
    ev = next(iter(cal_repo.events.values()))
    assert ev.origin == "remote_invite"
    assert ev.remote_event_id == "remote-evt-1"
    assert ev.remote_instance_id == "i_remote"
    assert ev.calendar_id == "cal-anna"
    assert ev.summary == "BBQ at the Smiths'"
    # First-receipt + rsvp_enabled → tentative auto-RSVP.
    assert any(r.status == "tentative" for r in cal_repo.rsvps.values())


async def test_inbound_invite_idempotent_under_redelivery(env):
    """Re-receiving the same envelope (network retry) collapses onto
    the same row instead of creating a duplicate."""
    fed, cal_repo, _ = env
    handler = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    now = datetime.now(timezone.utc)
    payload = {
        "event_id": "remote-evt-1",
        "summary": "BBQ",
        "start": now.isoformat(),
        "end": (now + timedelta(hours=2)).isoformat(),
        "organizer_user_id": "u-bob",
        "attendee_user_ids": ["u-anna"],
    }
    await handler(
        _envelope(FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED, payload)
    )
    await handler(
        _envelope(FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED, payload)
    )
    assert len(cal_repo.events) == 1


async def test_inbound_update_overwrites_in_place(env):
    fed, cal_repo, _ = env
    create = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    update = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_UPDATED
    ]
    now = datetime.now(timezone.utc)
    base = {
        "event_id": "remote-evt-1",
        "start": now.isoformat(),
        "end": (now + timedelta(hours=1)).isoformat(),
        "organizer_user_id": "u-bob",
        "attendee_user_ids": ["u-anna"],
    }
    await create(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
            {**base, "summary": "Original"},
        )
    )
    await update(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_UPDATED,
            {**base, "summary": "Renamed"},
        )
    )
    assert len(cal_repo.events) == 1
    ev = next(iter(cal_repo.events.values()))
    assert ev.summary == "Renamed"


async def test_inbound_delete_removes_mirror(env):
    fed, cal_repo, _ = env
    create = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    delete = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_DELETED
    ]
    now = datetime.now(timezone.utc)
    await create(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
            {
                "event_id": "remote-evt-1",
                "summary": "BBQ",
                "start": now.isoformat(),
                "end": (now + timedelta(hours=1)).isoformat(),
                "organizer_user_id": "u-bob",
                "attendee_user_ids": ["u-anna"],
            },
        )
    )
    await delete(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_DELETED,
            {"event_id": "remote-evt-1"},
        )
    )
    assert cal_repo.events == {}


async def test_inbound_rsvp_writes_to_local_event(env):
    """An RSVP coming back from a paired peer lands on the organiser's
    local event id (not a mirror) — verifies the responder echoes the
    organiser's id back."""
    fed, cal_repo, _ = env
    rsvp = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED
    ]
    now = datetime.now(timezone.utc)
    cal_repo.events["local-evt"] = CalendarEvent(
        id="local-evt",
        calendar_id="cal-anna",
        summary="Picnic",
        start=now,
        end=now + timedelta(hours=1),
        created_by="u-anna",
        attendees=("u-bob", "u-bob-remote"),
    )
    await rsvp(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED,
            {
                "event_id": "local-evt",
                "user_id": "u-bob-remote",
                "status": "accepted",
                "occurrence_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
        )
    )
    assert any(
        r.user_id == "u-bob-remote" and r.status == "accepted"
        for r in cal_repo.rsvps.values()
    )


async def test_inbound_rsvp_for_unknown_event_dropped(env):
    """A peer sending an RSVP for an event we don't own shouldn't
    create an RSVP row — would otherwise let an attacker write
    phantom RSVPs."""
    fed, cal_repo, _ = env
    rsvp = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED
    ]
    now = datetime.now(timezone.utc)
    await rsvp(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED,
            {
                "event_id": "ghost-evt",
                "user_id": "u-bob",
                "status": "accepted",
                "occurrence_at": now.isoformat(),
            },
        )
    )
    assert cal_repo.rsvps == {}


async def test_inbound_rsvp_rejects_bad_status(env):
    fed, cal_repo, _ = env
    rsvp = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED
    ]
    now = datetime.now(timezone.utc)
    cal_repo.events["local-evt"] = CalendarEvent(
        id="local-evt",
        calendar_id="cal-anna",
        summary="Picnic",
        start=now,
        end=now + timedelta(hours=1),
        created_by="u-anna",
        attendees=("u-bob", "u-bob-remote"),
    )
    await rsvp(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED,
            {
                "event_id": "local-evt",
                "user_id": "u-bob",
                "status": "going",  # space-RSVP word, not a valid one here
                "occurrence_at": now.isoformat(),
            },
        )
    )
    assert cal_repo.rsvps == {}


async def test_inbound_rsvp_deleted_clears_row(env):
    fed, cal_repo, _ = env
    rsvp_upd = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED
    ]
    rsvp_del = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_RSVP_DELETED
    ]
    now = datetime.now(timezone.utc)
    cal_repo.events["local-evt"] = CalendarEvent(
        id="local-evt",
        calendar_id="cal-anna",
        summary="Picnic",
        start=now,
        end=now + timedelta(hours=1),
        created_by="u-anna",
        attendees=("u-bob", "u-bob-remote"),
    )
    await rsvp_upd(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED,
            {
                "event_id": "local-evt",
                "user_id": "u-bob",
                "status": "accepted",
                "occurrence_at": now.isoformat(),
            },
        )
    )
    assert len(cal_repo.rsvps) == 1
    await rsvp_del(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_RSVP_DELETED,
            {
                "event_id": "local-evt",
                "user_id": "u-bob",
                "occurrence_at": now.isoformat(),
            },
        )
    )
    assert cal_repo.rsvps == {}


async def test_inbound_rsvp_deleted_for_unknown_event_noop(env):
    """RSVP-delete for an event we don't own → noop, never raises."""
    fed, cal_repo, _ = env
    rsvp_del = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_RSVP_DELETED
    ]
    await rsvp_del(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_RSVP_DELETED,
            {"event_id": "ghost", "user_id": "u-bob"},
        )
    )
    assert cal_repo.rsvps == {}


async def test_inbound_invite_dropped_when_user_has_no_calendar(env):
    """If the recipient hasn't got a personal calendar yet, the invite
    is logged + skipped (never raises)."""
    fed, cal_repo, user_repo = env
    handler = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    # Seed a recipient user that has no calendar.
    user_repo.by_uid["u-ben"] = User(
        username="ben",
        user_id="u-ben",
        display_name="Ben",
    )
    now = datetime.now(timezone.utc)
    await handler(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
            {
                "event_id": "remote-evt-2",
                "summary": "BBQ",
                "start": now.isoformat(),
                "end": (now + timedelta(hours=1)).isoformat(),
                "organizer_user_id": "u-bob",
                "attendee_user_ids": ["u-ben"],
            },
        )
    )
    # No mirror written for ben — but the existing one for anna is
    # still empty (we didn't include her in the attendee list).
    assert cal_repo.events == {}


async def test_inbound_invite_dropped_when_user_unknown(env):
    """A user_id not in the local users table → no calendar lookup
    is even attempted; handler logs + skips."""
    fed, cal_repo, _ = env
    handler = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    now = datetime.now(timezone.utc)
    await handler(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
            {
                "event_id": "remote-evt-3",
                "summary": "BBQ",
                "start": now.isoformat(),
                "end": (now + timedelta(hours=1)).isoformat(),
                "organizer_user_id": "u-bob",
                "attendee_user_ids": ["u-totally-unknown"],
            },
        )
    )
    assert cal_repo.events == {}


async def test_inbound_invite_missing_required_fields_dropped(env):
    """Lenient handler: malformed payload (no summary, etc.) just logs
    and returns — never raises into the inbound pipeline."""
    fed, cal_repo, _ = env
    handler = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    await handler(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
            {"event_id": "x"},  # missing summary, start, end, attendees, organiser
        )
    )
    assert cal_repo.events == {}


async def test_inbound_delete_with_attendee_list_drops_per_recipient(env):
    """When the DELETE envelope carries the attendee list (typical
    case), the handler drops the per-recipient rows directly via the
    minted id."""
    fed, cal_repo, _ = env
    create = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    delete = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_DELETED
    ]
    now = datetime.now(timezone.utc)
    payload = {
        "event_id": "remote-evt-1",
        "summary": "BBQ",
        "start": now.isoformat(),
        "end": (now + timedelta(hours=1)).isoformat(),
        "organizer_user_id": "u-bob",
        "attendee_user_ids": ["u-anna"],
    }
    await create(
        _envelope(FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED, payload)
    )
    assert len(cal_repo.events) == 1
    await delete(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_DELETED,
            {"event_id": "remote-evt-1", "attendee_user_ids": ["u-anna"]},
        )
    )
    assert cal_repo.events == {}


async def test_inbound_delete_unknown_event_noop(env):
    fed, cal_repo, _ = env
    delete = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_DELETED
    ]
    await delete(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_DELETED,
            {"event_id": "ghost"},
        )
    )
    assert cal_repo.events == {}


async def test_inbound_delete_with_no_event_id_noop(env):
    """Lenient guard — empty/missing event_id just returns."""
    fed, cal_repo, _ = env
    delete = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_DELETED
    ]
    await delete(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_DELETED,
            {},  # no event_id at all
        )
    )
    assert cal_repo.events == {}


async def test_inbound_rsvp_updated_missing_fields_noop(env):
    """Empty user_id / status → handler returns without touching db."""
    fed, cal_repo, _ = env
    rsvp = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED
    ]
    await rsvp(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED,
            {"event_id": "x"},  # missing user_id, status
        )
    )
    assert cal_repo.rsvps == {}


async def test_inbound_rsvp_updated_defaults_occurrence_to_event_start(env):
    """When the envelope omits occurrence_at, the handler falls back
    to the local event's ``start.isoformat()`` — covers the default
    branch."""
    fed, cal_repo, _ = env
    rsvp = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED
    ]
    now = datetime.now(timezone.utc)
    cal_repo.events["evt"] = CalendarEvent(
        id="evt",
        calendar_id="cal-anna",
        summary="P",
        start=now,
        end=now + timedelta(hours=1),
        created_by="u-anna",
        attendees=("u-bob", "u-bob-remote"),
    )
    await rsvp(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED,
            {
                "event_id": "evt",
                "user_id": "u-bob",
                "status": "accepted",
                # NB: no occurrence_at
            },
        )
    )
    keys = list(cal_repo.rsvps.keys())
    assert len(keys) == 1
    assert keys[0][2] == now.isoformat()


async def test_inbound_rsvp_deleted_missing_fields_noop(env):
    fed, cal_repo, _ = env
    rsvp_del = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_RSVP_DELETED
    ]
    await rsvp_del(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_RSVP_DELETED,
            {"event_id": "x"},  # no user_id
        )
    )
    assert cal_repo.rsvps == {}


# ── client_event_uuid (issue #327) ─────────────────────────────────────


async def test_inbound_invite_persists_client_event_uuid(env):
    """When the sender's envelope carries ``client_event_uuid``, the
    mirror row on the recipient side persists it so cross-household
    grouping in the agenda merges the host's row with the local
    mirror by intent."""
    fed, cal_repo, _ = env
    handler = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    now = datetime.now(timezone.utc)
    await handler(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
            {
                "event_id": "remote-evt-uuid-1",
                "summary": "Cross-house dinner",
                "start": now.isoformat(),
                "end": (now + timedelta(hours=2)).isoformat(),
                "organizer_user_id": "u-bob-remote",
                "attendee_user_ids": ["u-anna"],
                "client_event_uuid": "abcdef0123456789abcdef0123456789",
            },
        )
    )
    ev = next(iter(cal_repo.events.values()))
    assert ev.client_event_uuid == "abcdef0123456789abcdef0123456789"


async def test_inbound_invite_without_uuid_lands_as_null(env):
    """Sub-version peers (no ``client_event_uuid`` in their envelope)
    land mirror rows with ``NULL`` — the SPA's content-key fallback
    in :func:`groupSharedEvents` covers them."""
    fed, cal_repo, _ = env
    handler = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    now = datetime.now(timezone.utc)
    await handler(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
            {
                "event_id": "remote-evt-legacy",
                "summary": "Legacy invite",
                "start": now.isoformat(),
                "end": (now + timedelta(hours=1)).isoformat(),
                "organizer_user_id": "u-bob-remote",
                "attendee_user_ids": ["u-anna"],
            },
        )
    )
    ev = next(iter(cal_repo.events.values()))
    assert ev.client_event_uuid is None


async def test_inbound_invite_rejects_non_string_uuid(env):
    """A bad payload type doesn't poison the row — accepted as None."""
    fed, cal_repo, _ = env
    handler = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    now = datetime.now(timezone.utc)
    await handler(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
            {
                "event_id": "remote-evt-junk",
                "summary": "Junk uuid",
                "start": now.isoformat(),
                "end": (now + timedelta(hours=1)).isoformat(),
                "organizer_user_id": "u-bob-remote",
                "attendee_user_ids": ["u-anna"],
                "client_event_uuid": 12345,  # not a string
            },
        )
    )
    ev = next(iter(cal_repo.events.values()))
    assert ev.client_event_uuid is None


# ─── peer-supplied tz is validated at the trust boundary ───────────────


@pytest.mark.parametrize(
    ("wire_tz", "expected"),
    [
        ("Foo/Bar", "UTC"),
        ("Europe/Zurich", "Europe/Zurich"),
    ],
)
async def test_inbound_invite_validates_peer_tz(env, wire_tz, expected):
    """An unknown IANA name from a peer must not reach the SPA (``Intl``
    raises ``RangeError`` on it and the whole calendar fails to render).
    Fail closed on the value, not the event — the row still lands,
    anchored to ``"UTC"``."""
    fed, cal_repo, _ = env
    handler = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    now = datetime.now(UTC)
    await handler(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
            {
                "event_id": "remote-evt-tz",
                "summary": "Zone test",
                "start": now.isoformat(),
                "end": (now + timedelta(hours=1)).isoformat(),
                "organizer_user_id": "u-bob",
                "attendee_user_ids": ["u-anna"],
                "tz": wire_tz,
            },
        )
    )
    assert len(cal_repo.events) == 1
    ev = next(iter(cal_repo.events.values()))
    assert ev.tz == expected


# ── RSVP scope: only the invited household, only its own users ────────


def _organiser_event(cal_repo, *, attendees=("u-bob",), origin="local"):
    now = datetime.now(timezone.utc)
    cal_repo.events["org-evt"] = CalendarEvent(
        id="org-evt",
        calendar_id="cal-anna",
        summary="Picnic",
        start=now,
        end=now + timedelta(hours=1),
        created_by="u-anna",
        attendees=attendees,
        origin=origin,
    )
    return now


def _rsvp_payload(user_id, now, status="accepted"):
    return {
        "event_id": "org-evt",
        "user_id": user_id,
        "status": status,
        "occurrence_at": now.isoformat(),
    }


@pytest.mark.parametrize(
    ("user_id", "from_instance", "attendees"),
    [
        # Event never shared with the sending household.
        ("u-bob", "i_other", ("u-bob",)),
        # Sender's own user, but not invited to this event.
        ("u-carl", "i_remote", ("u-bob",)),
        # A local household member's RSVP.
        ("u-anna", "i_remote", ("u-bob", "u-anna")),
        # Invited user who lives on a third household.
        ("u-dora", "i_remote", ("u-bob", "u-dora")),
        # Unknown user id.
        ("u-ghost", "i_remote", ("u-bob", "u-ghost")),
    ],
)
async def test_inbound_rsvp_updated_refused_outside_invite(
    env, caplog, user_id, from_instance, attendees
):
    fed, cal_repo, user_repo = env
    user_repo.home.update({"u-carl": "i_remote", "u-dora": "i_third"})
    rsvp = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED
    ]
    now = _organiser_event(cal_repo, attendees=attendees)
    with caplog.at_level("WARNING"):
        await rsvp(
            _envelope(
                FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED,
                _rsvp_payload(user_id, now),
                from_instance=from_instance,
            )
        )
    assert cal_repo.rsvps == {}
    assert "refusing" in caplog.text


async def test_inbound_rsvp_updated_refused_on_mirrored_invite(env, caplog):
    """A mirror row (``origin='remote_invite'``) has its organiser elsewhere;
    RSVPs flow organiser-ward only, so a peer can't write one here."""
    fed, cal_repo, _ = env
    rsvp = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED
    ]
    now = _organiser_event(cal_repo, origin="remote_invite")
    with caplog.at_level("WARNING"):
        await rsvp(
            _envelope(
                FederationEventType.PERSONAL_CALENDAR_RSVP_UPDATED,
                _rsvp_payload("u-bob", now),
            )
        )
    assert cal_repo.rsvps == {}
    assert "refusing" in caplog.text


@pytest.mark.parametrize(
    ("victim", "from_instance"),
    [
        ("u-anna", "i_remote"),  # local member's RSVP
        ("u-bob", "i_other"),  # another household's invitee
    ],
)
async def test_inbound_rsvp_deleted_refused_outside_invite(
    env, caplog, victim, from_instance
):
    fed, cal_repo, _ = env
    rsvp_del = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_RSVP_DELETED
    ]
    now = _organiser_event(cal_repo, attendees=("u-bob", "u-anna"))
    key = ("org-evt", victim, now.isoformat())
    cal_repo.rsvps[key] = CalendarRSVP(
        event_id="org-evt",
        user_id=victim,
        status="accepted",
        updated_at=now.isoformat(),
        occurrence_at=now.isoformat(),
    )
    with caplog.at_level("WARNING"):
        await rsvp_del(
            _envelope(
                FederationEventType.PERSONAL_CALENDAR_RSVP_DELETED,
                {
                    "event_id": "org-evt",
                    "user_id": victim,
                    "occurrence_at": now.isoformat(),
                },
                from_instance=from_instance,
            )
        )
    assert key in cal_repo.rsvps
    assert "refusing" in caplog.text


async def test_inbound_rsvp_deleted_by_invited_user_clears_row(env):
    fed, cal_repo, _ = env
    rsvp_del = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_RSVP_DELETED
    ]
    now = _organiser_event(cal_repo)
    key = ("org-evt", "u-bob", now.isoformat())
    cal_repo.rsvps[key] = CalendarRSVP(
        event_id="org-evt",
        user_id="u-bob",
        status="accepted",
        updated_at=now.isoformat(),
        occurrence_at=now.isoformat(),
    )
    await rsvp_del(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_RSVP_DELETED,
            {"event_id": "org-evt", "user_id": "u-bob"},
        )
    )
    assert cal_repo.rsvps == {}


# ── Invite scope: a household edits only its own mirror ───────────────


def _invite_payload(remote_id="remote-evt-1", organiser="u-bob"):
    now = datetime.now(timezone.utc)
    return {
        "event_id": remote_id,
        "summary": "BBQ",
        "start": now.isoformat(),
        "end": (now + timedelta(hours=1)).isoformat(),
        "organizer_user_id": organiser,
        "attendee_user_ids": ["u-anna"],
        "rsvp_enabled": True,
    }


def _seed_row(cal_repo, row_id, **overrides):
    now = datetime.now(timezone.utc)
    fields = {
        "id": row_id,
        "calendar_id": "cal-anna",
        "summary": "Seeded",
        "start": now,
        "end": now + timedelta(hours=1),
        "created_by": "u-anna",
        **overrides,
    }
    cal_repo.events[row_id] = CalendarEvent(**fields)
    return cal_repo.events[row_id]


@pytest.mark.parametrize(
    ("organiser", "home"),
    [
        ("u-anna", None),  # local member (home = "self" from the fixture)
        ("u-dora", "i_third"),  # a third household's user
        ("u-ghost", None),  # unknown
    ],
)
@pytest.mark.parametrize(
    "event_type",
    [
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
        FederationEventType.PERSONAL_CALENDAR_EVENT_UPDATED,
    ],
)
async def test_inbound_invite_refused_when_organiser_not_senders(
    env, caplog, organiser, home, event_type
):
    fed, cal_repo, user_repo = env
    if home is not None:
        user_repo.home[organiser] = home
    handler = fed._event_registry.handlers[event_type]
    with caplog.at_level("WARNING"):
        await handler(_envelope(event_type, _invite_payload(organiser=organiser)))
    assert cal_repo.events == {}
    assert cal_repo.rsvps == {}
    assert "organiser is not a user of the sending household" in caplog.text


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({}, "row is not an inbound invite"),
        (
            {
                "origin": "remote_invite",
                "remote_instance_id": "i_other",
                "remote_event_id": "remote-evt-1",
            },
            "row mirrors another household's invite",
        ),
        (
            {
                "origin": "remote_invite",
                "remote_instance_id": "i_remote",
                "remote_event_id": "remote-evt-1-twin",
            },
            "row mirrors a different remote event",
        ),
        (
            {
                "origin": "remote_invite",
                "remote_instance_id": "i_remote",
                "remote_event_id": "remote-evt-1",
                "calendar_id": "cal-someone-else",
            },
            "row is not on the recipient's calendar",
        ),
    ],
)
async def test_inbound_invite_update_refused_on_foreign_row(
    env, caplog, overrides, reason
):
    """The derived row id is truncated; a row found there that is not this
    household's mirror of this event for this recipient stays untouched."""
    fed, cal_repo, _ = env
    row_id = _mint_event_id("i_remote", "remote-evt-1", "u-anna")
    seeded = _seed_row(cal_repo, row_id, **overrides)
    handler = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_UPDATED
    ]
    with caplog.at_level("WARNING"):
        await handler(
            _envelope(
                FederationEventType.PERSONAL_CALENDAR_EVENT_UPDATED,
                _invite_payload(),
            )
        )
    assert cal_repo.events == {row_id: seeded}
    assert cal_repo.rsvps == {}
    assert reason in caplog.text


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {
            "origin": "remote_invite",
            "remote_instance_id": "i_other",
            "remote_event_id": "remote-evt-1",
        },
        {
            "origin": "remote_invite",
            "remote_instance_id": "i_remote",
            "remote_event_id": "remote-evt-1-twin",
        },
    ],
)
async def test_inbound_delete_refused_on_foreign_row(env, caplog, overrides):
    fed, cal_repo, _ = env
    row_id = _mint_event_id("i_remote", "remote-evt-1", "u-anna")
    seeded = _seed_row(cal_repo, row_id, **overrides)
    delete = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_DELETED
    ]
    with caplog.at_level("WARNING"):
        await delete(
            _envelope(
                FederationEventType.PERSONAL_CALENDAR_EVENT_DELETED,
                {"event_id": "remote-evt-1", "attendee_user_ids": ["u-anna"]},
            )
        )
    assert cal_repo.events == {row_id: seeded}
    assert "refusing" in caplog.text


async def test_inbound_invite_for_remote_attendee_creates_nothing(env):
    """Only local users get a mirror (and the default tentative RSVP)."""
    fed, cal_repo, _ = env
    handler = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    payload = {**_invite_payload(), "attendee_user_ids": ["u-bob"]}
    await handler(
        _envelope(FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED, payload)
    )
    assert cal_repo.events == {}
    assert cal_repo.rsvps == {}


@pytest.mark.parametrize(
    ("cover", "expected"),
    [
        ("https://tracker.example/pixel.png", None),
        ("javascript:alert(1)", None),
        ("/api/media/cover.webp", "/api/media/cover.webp"),
    ],
)
async def test_inbound_invite_keeps_only_a_local_cover(env, cover, expected):
    """F7: a mirrored invite's ``cover_url`` is an ``<img src>`` — a remote
    URL would leak the viewer's IP, so only a local media ref is kept,
    on create and on update."""
    fed, cal_repo, _ = env
    create = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED
    ]
    update = fed._event_registry.handlers[
        FederationEventType.PERSONAL_CALENDAR_EVENT_UPDATED
    ]
    now = datetime.now(timezone.utc)
    base = {
        "event_id": "remote-evt-1",
        "summary": "BBQ",
        "start": now.isoformat(),
        "end": (now + timedelta(hours=1)).isoformat(),
        "organizer_user_id": "u-bob",
        "attendee_user_ids": ["u-anna"],
    }
    await create(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_CREATED,
            {**base, "cover_url": cover},
        )
    )
    assert next(iter(cal_repo.events.values())).cover_url == expected
    await update(
        _envelope(
            FederationEventType.PERSONAL_CALENDAR_EVENT_UPDATED,
            {**base, "cover_url": cover},
        )
    )
    assert next(iter(cal_repo.events.values())).cover_url == expected
