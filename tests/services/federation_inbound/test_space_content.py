"""Tests for :class:`SpaceContentInboundHandlers` (§13)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from socialhome.domain.events import (
    CalendarEventCreated,
    CalendarEventDeleted,
    GalleryAlbumCreated,
    GalleryAlbumDeleted,
    GalleryAlbumUpdated,
    GalleryItemDeleted,
    GalleryItemUploaded,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.post import BazaarStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.federation_inbound import SpaceContentInboundHandlers
from socialhome.domain.user import SYSTEM_AUTHOR
from socialhome.services.gallery_service import (
    ALBUMS_PER_SPACE,
    DESCRIPTION_MAX,
    NAME_MAX,
)
from socialhome.services.gallery_tombstones import GalleryAlbumTombstones


class _FakeRegistry:
    def __init__(self) -> None:
        self.registered = []

    def register(self, t, h):
        self.registered.append((t, h))


class _FakeFederationService:
    def __init__(self) -> None:
        self._event_registry = _FakeRegistry()


class _AllowAuthorship:
    """A :class:`SpaceAuthorship` stand-in that lets every write through.

    These tests are about each handler's own shape (field parsing, space
    scoping, bus events). The authorship rule is covered by
    ``tests/federation/test_space_authorship.py`` and, end to end, by
    ``tests/protocol/test_space_content_authorship.py``; the
    ``_DenyAuthorship`` tests at the bottom prove every handler consults it.
    """

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, str]] = []
        self.refusals: list[str] = []

    async def acts_for(self, event, space_id, user_id, *, any_role=False):
        self.calls.append(("acts_for", space_id, user_id))
        return self.answer

    async def may_author(self, event, space_id, user_id, *, subscriber_comment=False):
        self.calls.append(("may_author", space_id, user_id))
        return self.answer

    async def may_mutate(self, event, space_id, owner_user_id):
        self.calls.append(("may_mutate", space_id, owner_user_id))
        return self.answer

    async def writes_here(self, event, space_id):
        self.calls.append(("writes_here", space_id, ""))
        return self.answer

    async def is_moderator(self, event, space_id):
        self.calls.append(("is_moderator", space_id, ""))
        return self.answer

    def log_refusal(self, event, *, space_id, what, row_id, user_id):
        self.refusals.append(row_id)

    async def hold_or_refuse(self, event, *, space_id, what, row_id, user_id):
        self.refusals.append(row_id)


class _FakePostRepo:
    """``get`` for the wrapper-post lookups (polls, schedules, bazaar).
    Posts live in ``sp-1`` by ``u-seller`` unless told otherwise."""

    def __init__(self) -> None:
        self.post_space: dict[str, str] = {}
        self.author: dict[str, str] = {}
        self.missing: set[str] = set()

    async def get(self, post_id):
        if post_id in self.missing:
            return None
        return (
            self.post_space.get(post_id, "sp-1"),
            SimpleNamespace(id=post_id, author=self.author.get(post_id, "u-seller")),
        )


class _ScopedRows:
    """Mimics the repos' §24.11 scoping: a row id belongs to one space."""

    def __init__(self) -> None:
        self.space_of: dict[str, str | None] = {}

    def row(self, row_id):
        """The stored row's scope + attribution, as ``get`` would return."""
        if row_id not in self.space_of:
            return None
        return SimpleNamespace(
            id=row_id,
            space_id=self.space_of[row_id],
            created_by="u-author",
            author="u-author",
        )

    def claim(self, row_id, space_id) -> bool:
        """Upsert under ``space_id``; False when the id is another space's."""
        owner = self.space_of.get(row_id, space_id)
        if owner != space_id:
            return False
        self.space_of[row_id] = space_id
        return True

    def drop(self, row_id, space_id) -> bool:
        if self.space_of.get(row_id) != space_id:
            return False
        del self.space_of[row_id]
        return True


class _FakePageRepo:
    def __init__(self) -> None:
        self.saved = []
        self.deleted = []
        self.rows = _ScopedRows()

    async def get(self, page_id):
        return self.rows.row(page_id)

    async def save(self, page, *, space_id):
        if not self.rows.claim(page.id, space_id):
            return False
        self.saved.append(page)
        return True

    async def delete(self, page_id, *, space_id):
        if not self.rows.drop(page_id, space_id):
            return False
        self.deleted.append(page_id)
        return True


class _FakeStickyRepo:
    def __init__(self) -> None:
        self.saved = []
        self.deleted = []
        self.rows = _ScopedRows()

    async def get(self, sticky_id):
        return self.rows.row(sticky_id)

    async def save(self, sticky, *, space_id):
        if not self.rows.claim(sticky.id, space_id):
            return False
        self.saved.append(sticky)
        return True

    async def delete(self, sticky_id, *, space_id):
        if not self.rows.drop(sticky_id, space_id):
            return False
        self.deleted.append(sticky_id)
        return True


class _FakeSpaceTaskRepo:
    def __init__(self) -> None:
        self.saved = []
        self.deleted = []
        self.rows = _ScopedRows()

    async def get(self, task_id):
        row = self.rows.row(task_id)
        return None if row is None else (row.space_id, row)

    async def save(self, task, *, space_id):
        if not self.rows.claim(task.id, space_id):
            return False
        self.saved.append((space_id, task))
        return True

    async def delete(self, task_id, *, space_id):
        if not self.rows.drop(task_id, space_id):
            return False
        self.deleted.append(task_id)
        return True


class _FakeSpaceCalendarRepo:
    def __init__(self) -> None:
        self.saved = []
        self.deleted = []
        # Per-event store keyed by event_id → (space_id, event)
        self._events: dict = {}
        # In-memory RSVP store keyed by (event_id, user_id, occurrence_at)
        self.rsvps: dict = {}
        # Buffer keyed the same way; status="removed" means apply-as-delete on flush.
        self.buffer: dict = {}
        self.flush_calls: list[str] = []

    async def save_event(self, event, *, space_id):
        owner = self._events.get(event.id)
        if owner is not None and owner[0] != space_id:
            return False
        self.saved.append((space_id, event))
        self._events[event.id] = (space_id, event)
        return True

    async def get_event(self, event_id):
        return self._events.get(event_id)

    async def delete_event(self, event_id, *, space_id):
        owner = self._events.get(event_id)
        if owner is None or owner[0] != space_id:
            return False
        self.deleted.append(event_id)
        self._events.pop(event_id, None)
        return True

    async def list_rsvps(self, event_id, *, occurrence_at=None):
        return [
            r
            for (ev, _u, occ), r in self.rsvps.items()
            if ev == event_id and (occurrence_at is None or occ == occurrence_at)
        ]

    async def upsert_rsvp(self, rsvp, *, space_id):
        owner = self._events.get(rsvp.event_id)
        if owner is None or owner[0] != space_id:
            return False
        self.rsvps[(rsvp.event_id, rsvp.user_id, rsvp.occurrence_at)] = rsvp
        return True

    async def remove_rsvp(self, event_id, user_id, *, occurrence_at, space_id):
        owner = self._events.get(event_id)
        if owner is None or owner[0] != space_id:
            return False
        self.rsvps.pop((event_id, user_id, occurrence_at), None)
        return True

    async def buffer_pending_rsvp(
        self,
        *,
        event_id,
        user_id,
        occurrence_at,
        status,
        updated_at,
        space_id,
    ):
        self.buffer[(event_id, user_id, occurrence_at)] = {
            "status": status,
            "updated_at": updated_at,
            "space_id": space_id,
        }

    async def flush_pending_rsvps(self, event_id, *, space_id):
        self.flush_calls.append(event_id)
        applied = []
        from socialhome.domain.calendar import CalendarRSVP, RSVPStatus

        keys_to_drop = [
            k
            for k in self.buffer
            if k[0] == event_id and self.buffer[k].get("space_id") == space_id
        ]
        for key in keys_to_drop:
            entry = self.buffer.pop(key)
            _, user_id, occ = key
            status = entry["status"]
            if status == "removed":
                self.rsvps.pop(key, None)
            elif status in RSVPStatus.ALL:
                rsvp = CalendarRSVP(
                    event_id=event_id,
                    user_id=user_id,
                    status=status,
                    updated_at=entry["updated_at"],
                    occurrence_at=occ,
                )
                self.rsvps[key] = rsvp
                applied.append(rsvp)
        return applied


class _FakePollRepo:
    """Space-scoped poll surface. Posts / slots live in ``sp-1`` unless
    ``post_space`` / ``slot_space`` say otherwise."""

    def __init__(self) -> None:
        self.valid_options: set[tuple[str, str]] = set()
        self.post_space: dict[str, str] = {}
        self.slot_space: dict[str, str] = {}
        self.cleared: list[tuple[str, str]] = []
        self.inserted: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.scheduled: list[dict] = []
        self.responses: dict[tuple[str, str], str] = {}
        self.finalized: list[tuple[str, str]] = []
        self.fail_create_schedule = False

    def _post_in(self, post_id, space_id) -> bool:
        return self.post_space.get(post_id, "sp-1") == space_id

    def _slot_in(self, slot_id, space_id) -> bool:
        return self.slot_space.get(slot_id, "sp-1") == space_id

    async def cast_vote_in_space(self, *, space_id, post_id, option_id, voter_user_id):
        if not self._post_in(post_id, space_id):
            return False
        if (post_id, option_id) not in self.valid_options:
            return False
        self.cleared.append((post_id, voter_user_id))
        self.inserted.append((option_id, voter_user_id))
        return True

    async def close_in_space(self, post_id, *, space_id):
        if not self._post_in(post_id, space_id):
            return False
        self.closed.append(post_id)
        return True

    async def create_schedule_poll_in_space(
        self, *, space_id, post_id, title, deadline, slots
    ):
        if self.fail_create_schedule:
            raise RuntimeError("malformed slot simulated")
        if not self._post_in(post_id, space_id):
            return False
        self.scheduled.append(
            {
                "post_id": post_id,
                "title": title,
                "deadline": deadline,
                "slots": list(slots),
            },
        )
        return True

    async def upsert_schedule_response_in_space(
        self, *, space_id, slot_id, user_id, response
    ):
        if not self._slot_in(slot_id, space_id):
            return False
        self.responses[(slot_id, user_id)] = response
        return True

    async def delete_schedule_response_in_space(self, *, space_id, slot_id, user_id):
        if not self._slot_in(slot_id, space_id):
            return False
        return self.responses.pop((slot_id, user_id), None) is not None

    async def finalize_schedule_poll_in_space(self, *, space_id, post_id, slot_id):
        if not self._post_in(post_id, space_id) or not self._slot_in(slot_id, space_id):
            return False
        self.finalized.append((post_id, slot_id))
        return True


def _event(event_type, payload, *, from_instance="peer-a", space_id=None):
    return FederationEvent(
        msg_id="m",
        event_type=event_type,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=space_id,
    )


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def repos():
    return {
        "page": _FakePageRepo(),
        "sticky": _FakeStickyRepo(),
        "task": _FakeSpaceTaskRepo(),
        "calendar": _FakeSpaceCalendarRepo(),
        "poll": _FakePollRepo(),
        "post": _FakePostRepo(),
        "auth": _AllowAuthorship(),
    }


@pytest.fixture
def handlers(bus, repos):
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
        poll_repo=repos["poll"],
    )
    h.attach_to(_FakeFederationService())
    return h


async def test_attach_registers_all_content_event_types(bus, repos):
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
        poll_repo=repos["poll"],
    )
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    # 15 events total: 3 task + 3 page + 3 sticky + 3 calendar + 3 poll.
    for t in (
        FederationEventType.SPACE_TASK_CREATED,
        FederationEventType.SPACE_TASK_UPDATED,
        FederationEventType.SPACE_TASK_DELETED,
        FederationEventType.SPACE_PAGE_CREATED,
        FederationEventType.SPACE_PAGE_UPDATED,
        FederationEventType.SPACE_PAGE_DELETED,
        FederationEventType.SPACE_STICKY_CREATED,
        FederationEventType.SPACE_STICKY_UPDATED,
        FederationEventType.SPACE_STICKY_DELETED,
        FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
        FederationEventType.SPACE_CALENDAR_EVENT_UPDATED,
        FederationEventType.SPACE_CALENDAR_EVENT_DELETED,
        FederationEventType.SPACE_RSVP_UPDATED,
        FederationEventType.SPACE_RSVP_DELETED,
        # SPACE_POLL_CREATED is intentionally not registered — poll
        # creation rides inline on SPACE_POST_CREATED.
        FederationEventType.SPACE_POLL_VOTE_CAST,
        FederationEventType.SPACE_POLL_CLOSED,
    ):
        assert t in types


# ─── Tasks ──────────────────────────────────────────────────────────


async def test_task_saved_happy_path(repos, handlers):
    await handlers._on_task_saved(
        _event(
            FederationEventType.SPACE_TASK_CREATED,
            {
                "id": "t-1",
                "list_id": "list-1",
                "title": "Fix the sink",
                "status": "todo",
                "created_by": "u-1",
                "assignees": ["u-2"],
            },
            space_id="sp-1",
        )
    )
    assert len(repos["task"].saved) == 1
    sp, task = repos["task"].saved[0]
    assert sp == "sp-1"
    assert task.id == "t-1"
    assert task.assignees == ("u-2",)


async def test_task_saved_missing_fields_drops(repos, handlers):
    await handlers._on_task_saved(
        _event(
            FederationEventType.SPACE_TASK_CREATED,
            {},
            space_id="sp-1",
        )
    )
    assert repos["task"].saved == []


async def test_task_deleted(repos, handlers):
    repos["task"].rows.claim("t-1", "sp-1")
    await handlers._on_task_deleted(
        _event(
            FederationEventType.SPACE_TASK_DELETED,
            {"id": "t-1"},
            space_id="sp-1",
        )
    )
    assert repos["task"].deleted == ["t-1"]


# ─── Pages ──────────────────────────────────────────────────────────


async def test_page_saved_happy_path(repos, handlers):
    await handlers._on_page_saved(
        _event(
            FederationEventType.SPACE_PAGE_CREATED,
            {
                "id": "p-1",
                "title": "Shopping tips",
                "content": "Buy local",
                "created_by": "u-1",
                "created_at": "2026-04-18T00:00:00+00:00",
                "updated_at": "2026-04-18T00:00:00+00:00",
            },
            space_id="sp-1",
        )
    )
    assert len(repos["page"].saved) == 1
    assert repos["page"].saved[0].id == "p-1"
    assert repos["page"].saved[0].space_id == "sp-1"


async def test_page_saved_missing_title_drops(repos, handlers):
    await handlers._on_page_saved(
        _event(
            FederationEventType.SPACE_PAGE_CREATED,
            {"id": "p-1"},
            space_id="sp-1",
        )
    )
    assert repos["page"].saved == []


async def test_page_deleted(repos, handlers):
    repos["page"].rows.claim("p-1", "sp-1")
    await handlers._on_page_deleted(
        _event(
            FederationEventType.SPACE_PAGE_DELETED,
            {"id": "p-1"},
            space_id="sp-1",
        )
    )
    assert repos["page"].deleted == ["p-1"]


# ─── Stickies ───────────────────────────────────────────────────────


async def test_sticky_saved_happy_path(repos, handlers):
    await handlers._on_sticky_saved(
        _event(
            FederationEventType.SPACE_STICKY_CREATED,
            {
                "id": "s-1",
                "author": "u-1",
                "content": "Remember to water plants",
                "color": "pink",
                "position_x": 100.0,
                "position_y": 50.0,
            },
            space_id="sp-1",
        )
    )
    assert len(repos["sticky"].saved) == 1
    assert repos["sticky"].saved[0].id == "s-1"


async def test_sticky_saved_missing_content_drops(repos, handlers):
    await handlers._on_sticky_saved(
        _event(
            FederationEventType.SPACE_STICKY_CREATED,
            {"id": "s-1", "author": "u-1"},
            space_id="sp-1",
        )
    )
    assert repos["sticky"].saved == []


async def test_sticky_deleted(repos, handlers):
    repos["sticky"].rows.claim("s-1", "sp-1")
    await handlers._on_sticky_deleted(
        _event(
            FederationEventType.SPACE_STICKY_DELETED,
            {"id": "s-1"},
            space_id="sp-1",
        )
    )
    assert repos["sticky"].deleted == ["s-1"]


# ─── Calendar events ────────────────────────────────────────────────


async def test_calendar_saved_happy_path(repos, handlers):
    await handlers._on_calendar_saved(
        _event(
            FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
            {
                "id": "e-1",
                "calendar_id": "cal-1",
                "summary": "Weekly sync",
                "created_by": "u-1",
                "start": "2026-04-18T10:00:00+00:00",
                "end": "2026-04-18T11:00:00+00:00",
            },
            space_id="sp-1",
        )
    )
    assert len(repos["calendar"].saved) == 1
    sp, ev = repos["calendar"].saved[0]
    assert sp == "sp-1"
    assert ev.id == "e-1"


async def test_calendar_saved_missing_end_drops(repos, handlers):
    await handlers._on_calendar_saved(
        _event(
            FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
            {
                "id": "e-1",
                "calendar_id": "cal-1",
                "summary": "X",
                "created_by": "u-1",
                "start": "2026-04-18T10:00:00+00:00",
            },
            space_id="sp-1",
        )
    )
    assert repos["calendar"].saved == []


async def test_calendar_deleted(repos, handlers):
    repos["calendar"]._events["e-1"] = ("sp-1", object())
    await handlers._on_calendar_deleted(
        _event(
            FederationEventType.SPACE_CALENDAR_EVENT_DELETED,
            {"id": "e-1"},
            space_id="sp-1",
        )
    )
    assert repos["calendar"].deleted == ["e-1"]


async def test_calendar_inbound_publishes_bus_event(bus, repos, handlers):
    """Inbound SPACE_CALENDAR_EVENT_CREATED publishes CalendarEventCreated
    on the local bus so the calendar→feed bridge fires (Phase B)."""
    from socialhome.domain.events import CalendarEventCreated

    received: list = []

    async def _capture(evt):
        received.append(evt)

    bus.subscribe(CalendarEventCreated, _capture)
    await handlers._on_calendar_saved(
        _event(
            FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
            {
                "id": "e-fed",
                "calendar_id": "cal-1",
                "summary": "Federated event",
                "created_by": "u-remote",
                "start": "2026-09-01T18:00:00+00:00",
                "end": "2026-09-01T20:00:00+00:00",
            },
            space_id="sp-1",
        )
    )
    assert len(received) == 1
    assert received[0].event.id == "e-fed"


# ─── RSVP federation (Phase A) ─────────────────────────────────────────


async def test_rsvp_updated_applies_when_event_present(repos, handlers):
    """RSVP arriving after the event lands → applied directly."""
    from socialhome.domain.calendar import CalendarEvent

    seed = datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc)
    repos["calendar"]._events["e-known"] = (
        "sp-1",
        CalendarEvent(
            id="e-known",
            calendar_id="cal-1",
            summary="x",
            start=seed,
            end=seed,
            created_by="u-1",
        ),
    )
    await handlers._on_rsvp_updated(
        _event(
            FederationEventType.SPACE_RSVP_UPDATED,
            {
                "event_id": "e-known",
                "user_id": "u-2",
                "occurrence_at": seed.isoformat(),
                "status": "going",
                "updated_at": "2026-04-15T00:00:00+00:00",
            },
            space_id="sp-1",
        )
    )
    key = ("e-known", "u-2", seed.isoformat())
    assert key in repos["calendar"].rsvps
    assert repos["calendar"].rsvps[key].status == "going"


async def test_rsvp_updated_buffers_when_event_missing(repos, handlers):
    """RSVP arriving before its event → goes to the pending buffer."""
    occ = "2026-05-01T18:00:00+00:00"
    await handlers._on_rsvp_updated(
        _event(
            FederationEventType.SPACE_RSVP_UPDATED,
            {
                "event_id": "e-future",
                "user_id": "u-2",
                "occurrence_at": occ,
                "status": "going",
                "updated_at": "2026-04-30T00:00:00+00:00",
            },
            space_id="sp-1",
        )
    )
    # No live RSVP row yet
    assert repos["calendar"].rsvps == {}
    # Buffered
    assert ("e-future", "u-2", occ) in repos["calendar"].buffer


async def test_calendar_event_arrival_flushes_buffer(repos, handlers):
    """When the event finally arrives, _on_calendar_saved flushes its buffer."""
    occ = "2026-05-15T18:00:00+00:00"
    # Buffer an orphan RSVP first.
    await repos["calendar"].buffer_pending_rsvp(
        event_id="e-late",
        user_id="u-9",
        occurrence_at=occ,
        status="going",
        updated_at="2026-05-10T00:00:00+00:00",
        space_id="sp-1",
    )
    # Event arrives.
    await handlers._on_calendar_saved(
        _event(
            FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
            {
                "id": "e-late",
                "calendar_id": "cal-1",
                "summary": "Game night",
                "created_by": "u-1",
                "start": "2026-05-15T18:00:00+00:00",
                "end": "2026-05-15T20:00:00+00:00",
            },
            space_id="sp-1",
        )
    )
    # Flush was called and the RSVP applied.
    assert repos["calendar"].flush_calls == ["e-late"]
    assert ("e-late", "u-9", occ) in repos["calendar"].rsvps


async def test_rsvp_deleted_with_event_present(repos, handlers):
    """SPACE_RSVP_DELETED removes the live row when the event is local."""
    from socialhome.domain.calendar import CalendarEvent, CalendarRSVP

    seed = datetime(2026, 5, 20, tzinfo=timezone.utc)
    repos["calendar"]._events["e-rm"] = (
        "sp-1",
        CalendarEvent(
            id="e-rm",
            calendar_id="cal-1",
            summary="Dinner",
            start=seed,
            end=seed,
            created_by="u-1",
        ),
    )
    occ = seed.isoformat()
    repos["calendar"].rsvps[("e-rm", "u-2", occ)] = CalendarRSVP(
        event_id="e-rm",
        user_id="u-2",
        status="going",
        updated_at="2026-05-19T00:00:00+00:00",
        occurrence_at=occ,
    )
    await handlers._on_rsvp_deleted(
        _event(
            FederationEventType.SPACE_RSVP_DELETED,
            {
                "event_id": "e-rm",
                "user_id": "u-2",
                "occurrence_at": occ,
                "updated_at": "2026-05-21T00:00:00+00:00",
            },
            space_id="sp-1",
        )
    )
    assert ("e-rm", "u-2", occ) not in repos["calendar"].rsvps


async def test_rsvp_updated_invalid_status_drops(repos, handlers):
    """Unknown status values are ignored — no buffer, no live row."""
    await handlers._on_rsvp_updated(
        _event(
            FederationEventType.SPACE_RSVP_UPDATED,
            {
                "event_id": "e-1",
                "user_id": "u-1",
                "occurrence_at": "2026-04-01T00:00:00+00:00",
                "status": "tentative",  # not in RSVPStatus.ALL
                "updated_at": "now",
            },
        )
    )
    assert repos["calendar"].rsvps == {}
    assert repos["calendar"].buffer == {}


# ─── Polls ──────────────────────────────────────────────────────────


async def test_poll_vote_clears_and_inserts(repos, handlers):
    """Single-choice invariant — prior vote cleared before new one inserts."""
    repos["poll"].valid_options.add(("p-1", "opt-a"))
    await handlers._on_poll_vote(
        _event(
            FederationEventType.SPACE_POLL_VOTE_CAST,
            {"post_id": "p-1", "option_id": "opt-a", "voter_user_id": "u-1"},
            space_id="sp-1",
        )
    )
    assert repos["poll"].cleared == [("p-1", "u-1")]
    assert repos["poll"].inserted == [("opt-a", "u-1")]


async def test_poll_vote_option_not_on_post_drops(repos, handlers):
    """Can't corrupt a tally with an option id that belongs elsewhere."""
    # No options registered — every lookup returns False.
    await handlers._on_poll_vote(
        _event(
            FederationEventType.SPACE_POLL_VOTE_CAST,
            {"post_id": "p-1", "option_id": "stolen", "voter_user_id": "u-1"},
        )
    )
    assert repos["poll"].inserted == []


async def test_poll_vote_missing_field_drops(repos, handlers):
    await handlers._on_poll_vote(
        _event(
            FederationEventType.SPACE_POLL_VOTE_CAST,
            {"post_id": "p-1"},
        )
    )
    assert repos["poll"].inserted == []


async def test_poll_closed(repos, handlers):
    await handlers._on_poll_closed(
        _event(
            FederationEventType.SPACE_POLL_CLOSED,
            {"post_id": "p-1"},
            space_id="sp-1",
        )
    )
    assert repos["poll"].closed == ["p-1"]


async def test_poll_handlers_not_registered_without_poll_repo(bus, repos):
    """Deployments without polls skip those events cleanly."""
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
    )  # no poll_repo
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    assert FederationEventType.SPACE_POLL_CREATED not in types
    assert FederationEventType.SPACE_POLL_VOTE_CAST not in types
    assert FederationEventType.SPACE_POLL_CLOSED not in types


# ─── Gallery items (§23.119) ────────────────────────────────────────


class _FakeGalleryRepo:
    """Stub matching the slice of ``AbstractGalleryRepo`` the handler uses.
    Albums live in ``sp-1`` unless ``album_space`` says otherwise."""

    def __init__(self) -> None:
        self.created = []
        self.deleted = []
        self.counts: dict[str, int] = {}
        self.items_by_id: dict[str, object] = {}
        self.album_space: dict[str, str | None] = {}
        self.fail_create = False
        self.albums: dict[str, SimpleNamespace] = {}
        self.albums_deleted: list[str] = []
        self.album_patches: list[tuple[str, dict]] = []
        #: Ids no album is held for here (until created).
        self.missing: set[str] = {"alb-new", "alb-late"}
        self.space_album_count = 0
        self.album_media: dict[str, list[str]] = {}

    def _album_in(self, album_id, space_id) -> bool:
        return self.album_space.get(album_id, "sp-1") == space_id

    async def get_item(self, item_id):
        return self.items_by_id.get(item_id)

    async def get_album(self, album_id):
        if album_id in self.albums:
            return self.albums[album_id]
        if album_id in self.albums_deleted or album_id in self.missing:
            return None
        return SimpleNamespace(
            id=album_id,
            space_id=self.album_space.get(album_id, "sp-1"),
            owner_user_id="u-owner",
            is_system=False,
        )

    async def list_albums(self, space_id, *, limit=30, before=None):
        return [SimpleNamespace(id=f"a{n}") for n in range(self.space_album_count)]

    async def list_album_media(self, album_id):
        return list(self.album_media.get(album_id, []))

    async def create_album_in_space(self, album, *, space_id):
        if album.id in self.album_space and self.album_space[album.id] != space_id:
            return False
        held = self.albums.get(album.id)
        if held is not None and held.owner_user_id != album.owner_user_id:
            return False
        self.albums[album.id] = SimpleNamespace(
            id=album.id,
            space_id=space_id,
            owner_user_id=album.owner_user_id,
            name=album.name,
            description=album.description,
            is_system=False,
        )
        return True

    async def update_album_in_space(self, album_id, patch, *, space_id):
        album = await self.get_album(album_id)
        if album is None or album.space_id != space_id or album.is_system:
            return False
        self.album_patches.append((album_id, dict(patch)))
        return True

    async def delete_album_in_space(self, album_id, *, space_id):
        album = await self.get_album(album_id)
        if album is None or album.space_id != space_id or album.is_system:
            return False
        self.albums.pop(album_id, None)
        self.albums_deleted.append(album_id)
        return True

    async def create_item_in_space(self, item, *, space_id, bump_count=True):
        if self.fail_create:
            raise RuntimeError("fk-violation simulated")
        if not self._album_in(item.album_id, space_id):
            return False
        self.created.append(item)
        self.items_by_id[item.id] = item
        if bump_count:
            self.counts[item.album_id] = self.counts.get(item.album_id, 0) + 1
        return True

    async def delete_item_in_space(self, item_id, *, space_id):
        item = self.items_by_id.get(item_id)
        if item is None or not self._album_in(item.album_id, space_id):
            return False
        self.deleted.append(item_id)
        self.items_by_id.pop(item_id, None)
        self.counts[item.album_id] = self.counts.get(item.album_id, 0) - 1
        return True


@pytest.fixture
def gallery_handlers(bus, repos):
    gallery = _FakeGalleryRepo()
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
        gallery_repo=gallery,
    )
    h.attach_to(_FakeFederationService())
    return h, gallery


async def test_gallery_item_saved_happy_path(gallery_handlers):
    handlers, gallery = gallery_handlers
    await handlers._on_gallery_item_saved(
        _event(
            FederationEventType.SPACE_GALLERY_ITEM_CREATED,
            {
                "id": "gi-1",
                "album_id": "alb-1",
                "uploaded_by": "alice",
                "item_type": "photo",
                "thumbnail_url": "/api/media/t.jpg",
                "width": 800,
                "height": 600,
                "occurred_at": "2026-04-10T12:00:00+00:00",
            },
            space_id="sp-1",
        ),
    )
    assert len(gallery.created) == 1
    assert gallery.created[0].id == "gi-1"
    # Album item count bumped.
    assert gallery.counts == {"alb-1": 1}


async def test_gallery_item_saved_drops_on_repo_error(gallery_handlers):
    """Unknown album / FK failure → log + drop, no count bump."""
    handlers, gallery = gallery_handlers
    gallery.fail_create = True
    await handlers._on_gallery_item_saved(
        _event(
            FederationEventType.SPACE_GALLERY_ITEM_CREATED,
            {
                "id": "gi-fk",
                "album_id": "missing",
                "uploaded_by": "alice",
                "item_type": "photo",
                "thumbnail_url": "/api/media/t.jpg",
                "width": 1,
                "height": 1,
            },
        ),
    )
    assert gallery.created == []
    assert gallery.counts == {}


async def test_gallery_item_saved_missing_required_fields(gallery_handlers):
    handlers, gallery = gallery_handlers
    await handlers._on_gallery_item_saved(
        _event(FederationEventType.SPACE_GALLERY_ITEM_CREATED, {"id": "gi-x"}),
    )
    assert gallery.created == []


async def test_gallery_item_deleted_decrements_count(gallery_handlers):
    handlers, gallery = gallery_handlers
    # Seed an existing item so delete decrements.
    from socialhome.domain.gallery import GalleryItem

    seeded = GalleryItem(
        id="gi-del",
        album_id="alb-1",
        uploaded_by="alice",
        item_type="photo",
        url="/api/media/x",
        thumbnail_url="/api/media/x-thumb",
        width=1,
        height=1,
    )
    gallery.items_by_id["gi-del"] = seeded
    await handlers._on_gallery_item_deleted(
        _event(
            FederationEventType.SPACE_GALLERY_ITEM_DELETED,
            {"id": "gi-del"},
            space_id="sp-1",
        ),
    )
    assert gallery.deleted == ["gi-del"]
    assert gallery.counts == {"alb-1": -1}


async def test_gallery_item_deleted_unknown_is_noop(gallery_handlers):
    """Delete for an item we never had → silent."""
    handlers, gallery = gallery_handlers
    await handlers._on_gallery_item_deleted(
        _event(FederationEventType.SPACE_GALLERY_ITEM_DELETED, {"id": "ghost"}),
    )
    assert gallery.deleted == []
    assert gallery.counts == {}


# ─── Gallery albums (v_33) ────────────────────────────────────────────


def _album_payload(**over):
    return {
        "id": "alb-new",
        "owner_user_id": "u-remote",
        "name": "Holiday",
        "description": "from next door",
        "cover_item_id": None,
        "created_at": "2026-09-28T10:00:00+00:00",
        **over,
    }


def _denying_gallery_handlers(bus, repos):
    gallery = _FakeGalleryRepo()
    deny = _AllowAuthorship(answer=False)
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=deny,
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
        gallery_repo=gallery,
    )
    return h, gallery, deny


async def test_gallery_album_created_files_it_under_the_gated_space(
    gallery_handlers,
):
    handlers, gallery = gallery_handlers
    await handlers._on_gallery_album_created(
        _event(
            FederationEventType.SPACE_GALLERY_ALBUM_CREATED,
            _album_payload(space_id="sp-1"),
            space_id="sp-1",
        )
    )
    got = gallery.albums["alb-new"]
    assert (got.space_id, got.owner_user_id, got.name, got.description) == (
        "sp-1",
        "u-remote",
        "Holiday",
        "from next door",
    )
    assert ("may_author", "sp-1", "u-remote") in handlers._authorship.calls


async def test_an_album_then_an_item_by_a_remote_member_both_land(
    gallery_handlers,
):
    """The bug: an album made after the others joined never reached them,
    so the item uploaded into it had no album to land in."""
    handlers, gallery = gallery_handlers
    gallery.album_space["alb-new"] = None  # not held here yet
    gallery.missing.discard("alb-new")
    item = {"id": "gi-1", "album_id": "alb-new", "uploaded_by": "u-remote"}
    await handlers._on_gallery_item_saved(
        _event(FederationEventType.SPACE_GALLERY_ITEM_CREATED, item, space_id="sp-1")
    )
    assert gallery.created == []  # no album yet → refused (the old failure)
    gallery.album_space.pop("alb-new")
    await handlers._on_gallery_album_created(
        _event(
            FederationEventType.SPACE_GALLERY_ALBUM_CREATED,
            _album_payload(),
            space_id="sp-1",
        )
    )
    gallery.album_space["alb-new"] = "sp-1"
    await handlers._on_gallery_item_saved(
        _event(FederationEventType.SPACE_GALLERY_ITEM_CREATED, item, space_id="sp-1")
    )
    assert [i.uploaded_by for i in gallery.created] == ["u-remote"]


async def test_gallery_album_created_needs_an_owner_and_a_name(gallery_handlers):
    handlers, gallery = gallery_handlers
    for bad in (
        _album_payload(id=""),
        _album_payload(owner_user_id=""),
        _album_payload(name="  "),
    ):
        await handlers._on_gallery_album_created(
            _event(
                FederationEventType.SPACE_GALLERY_ALBUM_CREATED, bad, space_id="sp-1"
            )
        )
    assert gallery.albums == {}


async def test_gallery_album_over_the_local_limits_is_dropped(gallery_handlers):
    """The wire gets the limits ``GalleryService`` puts on a local album."""
    handlers, gallery = gallery_handlers
    for bad in (
        _album_payload(name="x" * (NAME_MAX + 1)),
        _album_payload(description="x" * (DESCRIPTION_MAX + 1)),
        _album_payload(description={"not": "text"}),
    ):
        await handlers._on_gallery_album_created(
            _event(
                FederationEventType.SPACE_GALLERY_ALBUM_CREATED, bad, space_id="sp-1"
            )
        )
        await handlers._on_gallery_album_updated(
            _event(
                FederationEventType.SPACE_GALLERY_ALBUM_UPDATED,
                dict(bad, id="alb-1"),
                space_id="sp-1",
            )
        )
    assert gallery.albums == {}
    assert gallery.album_patches == []


async def test_gallery_album_created_without_a_space_is_dropped(gallery_handlers):
    handlers, gallery = gallery_handlers
    await handlers._on_gallery_album_created(
        _event(FederationEventType.SPACE_GALLERY_ALBUM_CREATED, _album_payload())
    )
    assert gallery.albums == {}


async def test_gallery_album_created_refused_authorship_holds_or_refuses(bus, repos):
    h, gallery, deny = _denying_gallery_handlers(bus, repos)
    await h._on_gallery_album_created(
        _event(
            FederationEventType.SPACE_GALLERY_ALBUM_CREATED,
            _album_payload(),
            space_id="sp-1",
        )
    )
    assert gallery.albums == {}
    assert deny.refusals == ["alb-new"]


async def test_gallery_album_created_cross_space_is_refused(gallery_handlers, caplog):
    handlers, gallery = gallery_handlers
    gallery.album_space["alb-new"] = "sp-b"
    with caplog.at_level("WARNING"):
        await handlers._on_gallery_album_created(
            _event(
                FederationEventType.SPACE_GALLERY_ALBUM_CREATED,
                _album_payload(),
                space_id="sp-a",
            )
        )
    assert gallery.albums == {}
    assert "is not in space sp-a" in caplog.text


async def test_gallery_album_updated_binds_the_stored_owner(gallery_handlers):
    """The payload's ``owner_user_id`` is ignored — the album keeps its
    owner, and the mutate rule is judged against the stored one."""
    handlers, gallery = gallery_handlers
    await handlers._on_gallery_album_updated(
        _event(
            FederationEventType.SPACE_GALLERY_ALBUM_UPDATED,
            _album_payload(id="alb-1", owner_user_id="u-evil", cover_item_id="gi-1"),
            space_id="sp-1",
        )
    )
    assert ("may_mutate", "sp-1", "u-owner") in handlers._authorship.calls
    assert gallery.album_patches == [
        (
            "alb-1",
            {
                "name": "Holiday",
                "description": "from next door",
                "cover_item_id": "gi-1",
            },
        )
    ]


async def test_gallery_album_updated_only_patches_what_it_carries(gallery_handlers):
    handlers, gallery = gallery_handlers
    await handlers._on_gallery_album_updated(
        _event(
            FederationEventType.SPACE_GALLERY_ALBUM_UPDATED,
            {"id": "alb-1", "name": "Renamed"},
            space_id="sp-1",
        )
    )
    assert gallery.album_patches == [("alb-1", {"name": "Renamed"})]


async def test_gallery_album_updated_refuses_the_system_album_and_strangers(
    gallery_handlers, caplog
):
    handlers, gallery = gallery_handlers
    gallery.albums["alb-sys"] = SimpleNamespace(
        id="alb-sys", space_id="sp-1", owner_user_id=None, is_system=True
    )
    gallery.album_space["alb-b"] = "sp-b"
    gallery.albums_deleted.append("alb-gone")
    with caplog.at_level("WARNING"):
        for album_id in ("alb-sys", "alb-b", "alb-gone", ""):
            await handlers._on_gallery_album_updated(
                _event(
                    FederationEventType.SPACE_GALLERY_ALBUM_UPDATED,
                    {"id": album_id, "name": "Evil"},
                    space_id="sp-1",
                )
            )
    assert gallery.album_patches == []
    assert "is not in space sp-1" in caplog.text


async def test_gallery_album_edit_and_delete_refused_authorship(bus, repos):
    h, gallery, deny = _denying_gallery_handlers(bus, repos)
    await h._on_gallery_album_updated(
        _event(
            FederationEventType.SPACE_GALLERY_ALBUM_UPDATED,
            {"id": "alb-1", "name": "Evil"},
            space_id="sp-1",
        )
    )
    await h._on_gallery_album_deleted(
        _event(
            FederationEventType.SPACE_GALLERY_ALBUM_DELETED,
            {"id": "alb-1"},
            space_id="sp-1",
        )
    )
    assert gallery.album_patches == [] and gallery.albums_deleted == []
    assert deny.refusals == ["alb-1", "alb-1"]


async def test_gallery_album_deleted_is_scoped(gallery_handlers, caplog):
    handlers, gallery = gallery_handlers
    gallery.album_space["alb-b"] = "sp-b"
    with caplog.at_level("WARNING"):
        await handlers._on_gallery_album_deleted(
            _event(
                FederationEventType.SPACE_GALLERY_ALBUM_DELETED,
                {"id": "alb-b"},
                space_id="sp-a",
            )
        )
    assert gallery.albums_deleted == []
    assert "is not in space sp-a" in caplog.text
    await handlers._on_gallery_album_deleted(
        _event(
            FederationEventType.SPACE_GALLERY_ALBUM_DELETED,
            {"id": "alb-1"},
            space_id="sp-1",
        )
    )
    assert gallery.albums_deleted == ["alb-1"]
    assert ("may_mutate", "sp-1", "u-owner") in handlers._authorship.calls


async def test_gallery_album_deleted_unknown_is_a_noop(gallery_handlers):
    handlers, gallery = gallery_handlers
    gallery.albums_deleted.append("ghost")
    for payload in ({"id": "ghost"}, {}):
        await handlers._on_gallery_album_deleted(
            _event(
                FederationEventType.SPACE_GALLERY_ALBUM_DELETED,
                payload,
                space_id="sp-1",
            )
        )
    assert gallery.albums_deleted == ["ghost"]


async def test_gallery_album_handlers_are_registered(bus, repos):
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
        gallery_repo=_FakeGalleryRepo(),
    )
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    assert {
        FederationEventType.SPACE_GALLERY_ALBUM_CREATED,
        FederationEventType.SPACE_GALLERY_ALBUM_UPDATED,
        FederationEventType.SPACE_GALLERY_ALBUM_DELETED,
    } <= types


async def test_gallery_handlers_not_registered_without_repo(bus, repos):
    """No gallery_repo → events not registered."""
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
    )
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    assert FederationEventType.SPACE_GALLERY_ITEM_CREATED not in types
    assert FederationEventType.SPACE_GALLERY_ITEM_DELETED not in types
    assert FederationEventType.SPACE_GALLERY_ALBUM_CREATED not in types


# ─── Bazaar listings (#PR445) ─────────────────────────────────────────


class _FakeBazaarRepo:
    """Stub matching the slice of ``AbstractBazaarRepo`` the handler uses.
    Wrapper posts live in ``sp-1`` unless ``post_space`` says otherwise."""

    def __init__(self) -> None:
        self.saved: list = []
        self.post_space: dict[str, str] = {}
        self.fail = False

    async def get_listing(self, post_id):
        return None

    async def save_listing(self, listing, *, space_id):
        if self.fail:
            raise RuntimeError("check-violation simulated")
        if self.post_space.get(listing.post_id, "sp-1") != space_id:
            return False
        self.saved.append(listing)
        return True


@pytest.fixture
def bazaar_handlers(bus, repos):
    bazaar = _FakeBazaarRepo()
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
        bazaar_repo=bazaar,
    )
    h.attach_to(_FakeFederationService())
    return h, bazaar


async def test_bazaar_listing_created_happy_path(bazaar_handlers):
    handlers, bazaar = bazaar_handlers
    await handlers._on_bazaar_listing_created(
        _event(
            FederationEventType.BAZAAR_LISTING_CREATED,
            {
                "post_id": "bzr-1",
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "mode": "fixed",
                "title": "Vintage chair",
                "description": "Nice.",
                "image_urls": ["api/media/chair-1.webp"],
                "end_time": "2026-06-01T00:00:00+00:00",
                "currency": "USD",
                "status": "active",
                "price": 4500,
                "created_at": "2026-05-23T10:00:00+00:00",
            },
        ),
    )
    assert len(bazaar.saved) == 1
    listing = bazaar.saved[0]
    assert listing.post_id == "bzr-1"
    assert listing.title == "Vintage chair"
    assert listing.mode.value == "fixed"
    assert listing.status.value == "active"
    assert listing.price == 4500
    assert listing.image_urls == ("api/media/chair-1.webp",)


async def test_bazaar_listing_created_missing_required_drops(bazaar_handlers):
    """Missing post_id / seller / mode means the payload is unusable — log + drop."""
    handlers, bazaar = bazaar_handlers
    await handlers._on_bazaar_listing_created(
        _event(
            FederationEventType.BAZAAR_LISTING_CREATED,
            {
                # post_id missing
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "mode": "fixed",
                "title": "X",
            },
        ),
    )
    assert bazaar.saved == []


async def test_bazaar_listing_created_unknown_mode_drops(bazaar_handlers):
    """Unknown mode/status from a forward-compatible peer → log + drop."""
    handlers, bazaar = bazaar_handlers
    await handlers._on_bazaar_listing_created(
        _event(
            FederationEventType.BAZAAR_LISTING_CREATED,
            {
                "post_id": "bzr-2",
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "mode": "future_mode_unknown",
                "title": "T",
            },
        ),
    )
    assert bazaar.saved == []


async def test_bazaar_listing_created_fk_violation_drops(bazaar_handlers):
    """FK violation (post hasn't landed yet) → log + drop; catch-up
    retries on the next §25.6 sync."""
    handlers, bazaar = bazaar_handlers
    bazaar.fail = True
    await handlers._on_bazaar_listing_created(
        _event(
            FederationEventType.BAZAAR_LISTING_CREATED,
            {
                "post_id": "bzr-3",
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "mode": "fixed",
                "title": "T",
            },
        ),
    )
    assert bazaar.saved == []


async def test_bazaar_handlers_not_registered_without_repo(bus, repos):
    """No bazaar_repo → BAZAAR_LISTING_CREATED not registered."""
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
    )
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    assert FederationEventType.BAZAAR_LISTING_CREATED not in types


# ─── Bazaar status updates (F8) ──────────────────────────────────────


class _FakeBazaarRepoWithStatus:
    """Stub matching the AbstractBazaarRepo slice the F8 handler uses.
    Listings live in ``sp-1`` unless ``listing_space`` says otherwise."""

    def __init__(self) -> None:
        self.sold: list[tuple[str, str, int]] = []
        self.expired: list[str] = []
        self.cancelled: list[str] = []
        self.listing_space: dict[str, str] = {}
        self.fail = False

    async def get_listing(self, post_id):
        return SimpleNamespace(
            post_id=post_id,
            space_id=self.listing_space.get(post_id, "sp-1"),
            seller_user_id="u-seller",
            status=BazaarStatus.ACTIVE,
        )

    async def mark_sold(self, post_id, *, space_id, winner_user_id, winning_price):
        if self.fail:
            raise ValueError("not active")
        self.sold.append((post_id, winner_user_id, int(winning_price)))

    async def mark_expired(self, post_id, *, space_id):
        if self.fail:
            raise ValueError("not active")
        self.expired.append(post_id)
        return True

    async def mark_cancelled(self, post_id, *, space_id):
        if self.fail:
            raise ValueError("not active")
        self.cancelled.append(post_id)
        return True


@pytest.fixture
def bazaar_status_handlers(bus, repos):
    bazaar = _FakeBazaarRepoWithStatus()
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
        bazaar_repo=bazaar,
    )
    h.attach_to(_FakeFederationService())
    return h, bazaar


async def test_bazaar_listing_updated_sold_routes_to_mark_sold(
    bazaar_status_handlers,
):
    handlers, bazaar = bazaar_status_handlers
    await handlers._on_bazaar_listing_updated(
        _event(
            FederationEventType.BAZAAR_LISTING_UPDATED,
            {
                "post_id": "bzr-1",
                "space_id": "sp-1",
                "status": "sold",
                "winner_user_id": "u-bidder",
                "winning_price": 4200,
            },
        ),
    )
    assert bazaar.sold == [("bzr-1", "u-bidder", 4200)]
    assert bazaar.expired == []
    assert bazaar.cancelled == []


async def test_bazaar_listing_updated_expired_routes_to_mark_expired(
    bazaar_status_handlers,
):
    handlers, bazaar = bazaar_status_handlers
    await handlers._on_bazaar_listing_updated(
        _event(
            FederationEventType.BAZAAR_LISTING_UPDATED,
            {"post_id": "bzr-1", "space_id": "sp-1", "status": "expired"},
        ),
    )
    assert bazaar.expired == ["bzr-1"]


async def test_bazaar_listing_updated_cancelled_routes_to_mark_cancelled(
    bazaar_status_handlers,
):
    handlers, bazaar = bazaar_status_handlers
    await handlers._on_bazaar_listing_updated(
        _event(
            FederationEventType.BAZAAR_LISTING_UPDATED,
            {"post_id": "bzr-1", "space_id": "sp-1", "status": "cancelled"},
        ),
    )
    assert bazaar.cancelled == ["bzr-1"]


async def test_bazaar_listing_updated_sold_missing_winner_drops(
    bazaar_status_handlers,
):
    """A sold update without winner/price is malformed — drop it."""
    handlers, bazaar = bazaar_status_handlers
    await handlers._on_bazaar_listing_updated(
        _event(
            FederationEventType.BAZAAR_LISTING_UPDATED,
            {"post_id": "bzr-1", "space_id": "sp-1", "status": "sold"},
        ),
    )
    assert bazaar.sold == []


async def test_bazaar_listing_updated_replay_against_terminal_state_is_silent(
    bazaar_status_handlers,
):
    """``mark_*`` raises when the row is already in a terminal state
    (gated on ``status='active'``). The handler swallows so an
    out-of-order replay doesn't error."""
    handlers, bazaar = bazaar_status_handlers
    bazaar.fail = True
    await handlers._on_bazaar_listing_updated(
        _event(
            FederationEventType.BAZAAR_LISTING_UPDATED,
            {"post_id": "bzr-1", "space_id": "sp-1", "status": "cancelled"},
        ),
    )
    # No exception bubbled; cancelled list stayed empty.
    assert bazaar.cancelled == []


async def test_bazaar_listing_updated_handler_not_registered_without_repo(
    bus,
    repos,
):
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
    )
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    assert FederationEventType.BAZAAR_LISTING_UPDATED not in types


# ─── Bazaar bids + offer acceptance (F7) ──────────────────────────────


class _FakeBazaarRepoWithBids:
    """Adds bid-handling methods to the F8 stub for F7 coverage."""

    def __init__(self) -> None:
        self.placed: list = []
        self.accepted: list[str] = []
        self.existing_bids: dict[str, object] = {}
        self.listing_space: dict[str, str] = {}
        self.fail_place = False

    async def get_listing(self, post_id):
        return SimpleNamespace(
            post_id=post_id,
            space_id=self.listing_space.get(post_id, "sp-1"),
            seller_user_id="u-seller",
            status=BazaarStatus.ACTIVE,
        )

    async def get_bid(self, bid_id):
        return self.existing_bids.get(bid_id)

    async def place_bid(self, bid, *, space_id):
        if self.fail_place:
            raise ValueError("listing not active")
        self.placed.append(bid)
        self.existing_bids[bid.id] = bid
        return bid

    async def accept_offer(self, bid_id, *, space_id):
        self.accepted.append(bid_id)


@pytest.fixture
def bazaar_bids_handlers(bus, repos):
    bazaar = _FakeBazaarRepoWithBids()
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
        bazaar_repo=bazaar,
    )
    h.attach_to(_FakeFederationService())
    return h, bazaar


async def test_bazaar_bid_placed_persists_bid(bazaar_bids_handlers):
    handlers, bazaar = bazaar_bids_handlers
    await handlers._on_bazaar_bid_placed(
        _event(
            FederationEventType.BAZAAR_BID_PLACED,
            {
                "bid_id": "bid-1",
                "listing_post_id": "bzr-1",
                "bidder_user_id": "u-bidder",
                "amount": 4200,
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "new_end_time": "2026-06-01T00:00:00+00:00",
                "message": "my offer",
            },
        ),
    )
    assert len(bazaar.placed) == 1
    bid = bazaar.placed[0]
    assert bid.id == "bid-1"
    assert bid.amount == 4200
    assert bid.message == "my offer"


async def test_bazaar_bid_placed_ignores_sender_created_at(bazaar_bids_handlers):
    """A sender-chosen timestamp is ignored — it would let a relayed bid
    backdate itself to win a tied amount; the repo stamps arrival time."""
    handlers, bazaar = bazaar_bids_handlers
    await handlers._on_bazaar_bid_placed(
        _event(
            FederationEventType.BAZAAR_BID_PLACED,
            {
                "bid_id": "bid-1",
                "listing_post_id": "bzr-1",
                "bidder_user_id": "u-bidder",
                "amount": 4200,
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        ),
    )
    assert not bazaar.placed[0].created_at


async def test_bazaar_bid_placed_missing_created_at_falls_back_to_empty(
    bazaar_bids_handlers,
):
    """No timestamp anywhere in the payload — the handler must NOT invent
    one; it hands the repo a falsy value so the repo's own default (a
    real "now", not the historical literal ``""``) can kick in."""
    handlers, bazaar = bazaar_bids_handlers
    await handlers._on_bazaar_bid_placed(
        _event(
            FederationEventType.BAZAAR_BID_PLACED,
            {
                "bid_id": "bid-1",
                "listing_post_id": "bzr-1",
                "bidder_user_id": "u-bidder",
                "amount": 4200,
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
            },
        ),
    )
    assert not bazaar.placed[0].created_at


async def test_bazaar_bid_placed_idempotent_on_replay(bazaar_bids_handlers):
    """Replay or out-of-order delivery — drop silently if bid_id already
    landed."""
    handlers, bazaar = bazaar_bids_handlers
    # Seed an existing bid.
    bazaar.existing_bids["bid-1"] = object()
    await handlers._on_bazaar_bid_placed(
        _event(
            FederationEventType.BAZAAR_BID_PLACED,
            {
                "bid_id": "bid-1",
                "listing_post_id": "bzr-1",
                "bidder_user_id": "u-bidder",
                "amount": 4200,
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "new_end_time": "2026-06-01T00:00:00+00:00",
            },
        ),
    )
    assert bazaar.placed == []


async def test_bazaar_bid_placed_missing_required_drops(bazaar_bids_handlers):
    handlers, bazaar = bazaar_bids_handlers
    await handlers._on_bazaar_bid_placed(
        _event(
            FederationEventType.BAZAAR_BID_PLACED,
            {"bid_id": "bid-1"},  # missing listing_post_id, bidder, amount
        ),
    )
    assert bazaar.placed == []


async def test_bazaar_offer_accepted_routes_to_accept_offer(
    bazaar_bids_handlers,
):
    handlers, bazaar = bazaar_bids_handlers
    bazaar.existing_bids["bid-winning"] = SimpleNamespace(
        id="bid-winning", listing_post_id="bzr-1"
    )
    await handlers._on_bazaar_offer_accepted(
        _event(
            FederationEventType.BAZAAR_OFFER_ACCEPTED,
            {
                "bid_id": "bid-winning",
                "listing_post_id": "bzr-1",
                "space_id": "sp-1",
                "buyer_user_id": "u-bidder",
                "price": 4500,
            },
        ),
    )
    assert bazaar.accepted == ["bid-winning"]


async def test_bazaar_bid_handlers_not_registered_without_repo(bus, repos):
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
    )
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    assert FederationEventType.BAZAAR_BID_PLACED not in types
    assert FederationEventType.BAZAAR_OFFER_ACCEPTED not in types


# ─── Schedule poll create (F5) ───────────────────────────────────────


@pytest.fixture
def schedule_handlers(bus, repos):
    poll = _FakePollRepo()
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
        poll_repo=poll,
    )
    h.attach_to(_FakeFederationService())
    return h, poll


async def test_schedule_created_persists_meta_and_slots(schedule_handlers):
    handlers, poll = schedule_handlers
    await handlers._on_schedule_created(
        _event(
            FederationEventType.SPACE_SCHEDULE_CREATED,
            {
                "post_id": "p-sched",
                "title": "Picnic?",
                "deadline": "2026-08-01",
                "space_id": "sp-1",
                "slots": [
                    {
                        "id": "s1",
                        "slot_date": "2026-07-01",
                        "start_time": "14:00",
                        "end_time": "16:00",
                        "position": 0,
                    },
                ],
            },
        ),
    )
    assert len(poll.scheduled) == 1
    assert poll.scheduled[0]["post_id"] == "p-sched"
    assert poll.scheduled[0]["title"] == "Picnic?"


async def test_schedule_created_missing_field_drops(schedule_handlers):
    handlers, poll = schedule_handlers
    await handlers._on_schedule_created(
        _event(
            FederationEventType.SPACE_SCHEDULE_CREATED,
            {"post_id": "p-sched", "title": "Picnic?"},  # missing slots
        ),
    )
    assert poll.scheduled == []


async def test_schedule_created_repo_failure_swallowed(schedule_handlers):
    """FK race (wrapper post not yet landed) — log + drop."""
    handlers, poll = schedule_handlers
    poll.fail_create_schedule = True
    await handlers._on_schedule_created(
        _event(
            FederationEventType.SPACE_SCHEDULE_CREATED,
            {
                "post_id": "p-orphan",
                "title": "X",
                "slots": [{"id": "s1", "slot_date": "2026-07-01"}],
            },
        ),
    )
    assert poll.scheduled == []


async def test_schedule_created_not_registered_without_poll_repo(bus, repos):
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
    )
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    assert FederationEventType.SPACE_SCHEDULE_CREATED not in types


# ─── peer-supplied tz is validated at the trust boundary ───────────────


@pytest.mark.parametrize(
    ("wire_tz", "expected"),
    [
        ("Foo/Bar", "UTC"),
        ("Europe/Zurich", "Europe/Zurich"),
    ],
)
async def test_calendar_saved_validates_peer_tz(repos, handlers, wire_tz, expected):
    """An unknown IANA name from a peer must not reach the SPA (``Intl``
    raises ``RangeError`` on it, breaking the whole space calendar tab).
    Fail closed on the value, not the event — the row still lands,
    anchored to ``"UTC"``."""
    await handlers._on_calendar_saved(
        _event(
            FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
            {
                "id": "e-tz",
                "calendar_id": "cal-1",
                "summary": "Zone test",
                "created_by": "u-remote",
                "start": "2026-09-10T18:00:00+00:00",
                "end": "2026-09-10T20:00:00+00:00",
                "tz": wire_tz,
            },
            space_id="sp-1",
        )
    )
    _space_id, ev = repos["calendar"]._events["e-tz"]
    assert ev.tz == expected


# ─── §24.11 cross-space refusals ─────────────────────────────
#
# Every handler below is fed an envelope the pipeline gated on space A
# that names a row of space B. The write must not land, a WARNING must
# say so, and no bus event may fire.


def _seed_other_space(repos):
    """Give space ``sp-b`` one row of every kind the handlers mutate."""
    repos["task"].rows.claim("t-b", "sp-b")
    repos["page"].rows.claim("p-b", "sp-b")
    repos["page"].rows.claim("hh-page", None)  # a household (personal) page
    repos["sticky"].rows.claim("s-b", "sp-b")
    repos["calendar"]._events["e-b"] = ("sp-b", object())


@pytest.fixture
def other_space(repos):
    _seed_other_space(repos)
    return repos


async def test_task_saved_cross_space_is_refused(other_space, handlers, caplog):
    with caplog.at_level("WARNING"):
        await handlers._on_task_saved(
            _event(
                FederationEventType.SPACE_TASK_UPDATED,
                {"id": "t-b", "list_id": "l-b", "title": "stolen"},
                space_id="sp-a",
            )
        )
    assert other_space["task"].saved == []
    assert other_space["task"].rows.space_of["t-b"] == "sp-b"
    assert "is not in space sp-a" in caplog.text


async def test_task_deleted_cross_space_is_refused(other_space, handlers, caplog):
    with caplog.at_level("WARNING"):
        await handlers._on_task_deleted(
            _event(
                FederationEventType.SPACE_TASK_DELETED,
                {"id": "t-b"},
                space_id="sp-a",
            )
        )
    assert other_space["task"].deleted == []
    assert other_space["task"].rows.space_of["t-b"] == "sp-b"
    assert "is not in space sp-a" in caplog.text


async def test_page_saved_cross_space_is_refused(other_space, handlers, caplog):
    with caplog.at_level("WARNING"):
        await handlers._on_page_saved(
            _event(
                FederationEventType.SPACE_PAGE_UPDATED,
                {"id": "p-b", "title": "stolen", "content": "x"},
                space_id="sp-a",
            )
        )
    assert other_space["page"].saved == []
    assert "is not in space sp-a" in caplog.text


async def test_page_deleted_cross_space_is_refused(other_space, handlers, caplog):
    with caplog.at_level("WARNING"):
        await handlers._on_page_deleted(
            _event(
                FederationEventType.SPACE_PAGE_DELETED,
                {"id": "p-b"},
                space_id="sp-a",
            )
        )
    assert other_space["page"].deleted == []
    assert other_space["page"].rows.space_of["p-b"] == "sp-b"
    assert "is not in space sp-a" in caplog.text


async def test_space_page_deleted_cannot_reach_the_personal_pages_table(
    other_space, handlers, caplog
):
    """A SPACE_PAGE_DELETED routed for a space never touches ``pages``.

    The household page id lives in the personal ``pages`` table
    (``space_id IS NULL``). The repo's delete now picks its table from
    the gated space, so a space-routed delete can't reach it — this
    asserts the handler passes that scope through.
    """
    with caplog.at_level("WARNING"):
        await handlers._on_page_deleted(
            _event(
                FederationEventType.SPACE_PAGE_DELETED,
                {"id": "hh-page"},
                space_id="sp-a",
            )
        )
    assert other_space["page"].deleted == []
    assert other_space["page"].rows.space_of["hh-page"] is None
    assert "is not in space sp-a" in caplog.text


async def test_sticky_saved_cross_space_is_refused(other_space, handlers, caplog):
    with caplog.at_level("WARNING"):
        await handlers._on_sticky_saved(
            _event(
                FederationEventType.SPACE_STICKY_UPDATED,
                {"id": "s-b", "author": "u-1", "content": "stolen"},
                space_id="sp-a",
            )
        )
    assert other_space["sticky"].saved == []
    assert "is not in space sp-a" in caplog.text


async def test_sticky_deleted_cross_space_is_refused(other_space, handlers, caplog):
    with caplog.at_level("WARNING"):
        await handlers._on_sticky_deleted(
            _event(
                FederationEventType.SPACE_STICKY_DELETED,
                {"id": "s-b"},
                space_id="sp-a",
            )
        )
    assert other_space["sticky"].deleted == []
    assert other_space["sticky"].rows.space_of["s-b"] == "sp-b"
    assert "is not in space sp-a" in caplog.text


async def test_calendar_saved_cross_space_is_refused(
    bus, other_space, handlers, caplog
):
    """No row change, no CalendarEventCreated, no buffer flush."""
    seen = []
    bus.subscribe(CalendarEventCreated, lambda e: seen.append(e))
    with caplog.at_level("WARNING"):
        await handlers._on_calendar_saved(
            _event(
                FederationEventType.SPACE_CALENDAR_EVENT_UPDATED,
                {
                    "id": "e-b",
                    "calendar_id": "cal-a",
                    "summary": "stolen",
                    "created_by": "u-1",
                    "start": "2026-05-15T18:00:00+00:00",
                    "end": "2026-05-15T20:00:00+00:00",
                },
                space_id="sp-a",
            )
        )
    assert other_space["calendar"].saved == []
    assert other_space["calendar"].flush_calls == []
    assert seen == []
    assert "is not in space sp-a" in caplog.text


async def test_calendar_deleted_cross_space_is_refused(
    bus, other_space, handlers, caplog
):
    """No row change and no CalendarEventDeleted on the bus."""
    seen = []
    bus.subscribe(CalendarEventDeleted, lambda e: seen.append(e))
    with caplog.at_level("WARNING"):
        await handlers._on_calendar_deleted(
            _event(
                FederationEventType.SPACE_CALENDAR_EVENT_DELETED,
                {"id": "e-b"},
                space_id="sp-a",
            )
        )
    assert other_space["calendar"].deleted == []
    assert "e-b" in other_space["calendar"]._events
    assert seen == []
    assert "is not in space sp-a" in caplog.text


async def test_rsvp_updated_cross_space_is_refused(other_space, handlers, caplog):
    occ = "2026-05-15T18:00:00+00:00"
    with caplog.at_level("WARNING"):
        await handlers._on_rsvp_updated(
            _event(
                FederationEventType.SPACE_RSVP_UPDATED,
                {
                    "event_id": "e-b",
                    "user_id": "u-attacker",
                    "occurrence_at": occ,
                    "status": "going",
                    "updated_at": "2026-05-10T00:00:00+00:00",
                },
                space_id="sp-a",
            )
        )
    assert other_space["calendar"].rsvps == {}
    assert other_space["calendar"].buffer == {}
    assert "is not in space sp-a" in caplog.text


async def test_rsvp_deleted_cross_space_is_refused(other_space, handlers, caplog):
    occ = "2026-05-15T18:00:00+00:00"
    other_space["calendar"].rsvps[("e-b", "u-9", occ)] = object()
    with caplog.at_level("WARNING"):
        await handlers._on_rsvp_deleted(
            _event(
                FederationEventType.SPACE_RSVP_DELETED,
                {
                    "event_id": "e-b",
                    "user_id": "u-9",
                    "occurrence_at": occ,
                    "updated_at": "2026-05-10T00:00:00+00:00",
                },
                space_id="sp-a",
            )
        )
    assert ("e-b", "u-9", occ) in other_space["calendar"].rsvps
    assert other_space["calendar"].buffer == {}
    assert "is not in space sp-a" in caplog.text


async def test_buffered_rsvp_carries_the_gated_space(repos, handlers):
    """An out-of-order RSVP is buffered under the space it was gated on."""
    occ = "2026-05-15T18:00:00+00:00"
    await handlers._on_rsvp_updated(
        _event(
            FederationEventType.SPACE_RSVP_UPDATED,
            {
                "event_id": "e-later",
                "user_id": "u-9",
                "occurrence_at": occ,
                "status": "going",
                "updated_at": "2026-05-10T00:00:00+00:00",
            },
            space_id="sp-a",
        )
    )
    assert repos["calendar"].buffer[("e-later", "u-9", occ)]["space_id"] == "sp-a"


@pytest.mark.parametrize(
    "handler_name,payload",
    [
        ("_on_task_saved", {"id": "t-1", "list_id": "l-1", "title": "x"}),
        ("_on_task_deleted", {"id": "t-1"}),
        ("_on_page_saved", {"id": "p-1", "title": "x"}),
        ("_on_page_deleted", {"id": "p-1"}),
        ("_on_sticky_saved", {"id": "s-1", "author": "u", "content": "x"}),
        ("_on_sticky_deleted", {"id": "s-1"}),
        (
            "_on_calendar_saved",
            {
                "id": "e-1",
                "calendar_id": "c-1",
                "summary": "x",
                "created_by": "u",
                "start": "2026-05-15T18:00:00+00:00",
                "end": "2026-05-15T19:00:00+00:00",
            },
        ),
        ("_on_calendar_deleted", {"id": "e-1"}),
        (
            "_on_rsvp_updated",
            {
                "event_id": "e-1",
                "user_id": "u",
                "occurrence_at": "2026-05-15T18:00:00+00:00",
                "status": "going",
                "updated_at": "2026-05-10T00:00:00+00:00",
            },
        ),
        (
            "_on_rsvp_deleted",
            {
                "event_id": "e-1",
                "user_id": "u",
                "occurrence_at": "2026-05-15T18:00:00+00:00",
                "updated_at": "2026-05-10T00:00:00+00:00",
            },
        ),
    ],
)
async def test_routing_payload_space_mismatch_is_dropped(
    repos, handlers, caplog, handler_name, payload
):
    """Routing field and payload copy disagree → nothing is written."""
    evt = _event(
        FederationEventType.SPACE_TASK_UPDATED,
        dict(payload, space_id="sp-b"),
        space_id="sp-a",
    )
    with caplog.at_level("WARNING"):
        await getattr(handlers, handler_name)(evt)
    assert repos["task"].saved == [] and repos["task"].deleted == []
    assert repos["page"].saved == [] and repos["page"].deleted == []
    assert repos["sticky"].saved == [] and repos["sticky"].deleted == []
    assert repos["calendar"].saved == [] and repos["calendar"].deleted == []
    assert repos["calendar"].rsvps == {} and repos["calendar"].buffer == {}
    assert "does not match payload space" in caplog.text


# ─── Cross-space refusal: polls, schedules, gallery, zones, bazaar ──────
#
# The fakes put every unknown row in ``sp-1``; these tests pin a row to
# ``sp-b`` and send the event gated for ``sp-a``.


async def test_poll_vote_cross_space_is_refused(repos, handlers, caplog):
    repos["poll"].valid_options.add(("p-b", "o-b"))
    repos["poll"].post_space["p-b"] = "sp-b"
    with caplog.at_level("WARNING"):
        await handlers._on_poll_vote(
            _event(
                FederationEventType.SPACE_POLL_VOTE_CAST,
                {"post_id": "p-b", "option_id": "o-b", "voter_user_id": "u"},
                space_id="sp-a",
            )
        )
    assert repos["poll"].inserted == [] and repos["poll"].cleared == []
    assert "is not in space sp-a" in caplog.text


async def test_poll_closed_cross_space_is_refused(repos, handlers, caplog):
    repos["poll"].post_space["p-b"] = "sp-b"
    with caplog.at_level("WARNING"):
        await handlers._on_poll_closed(
            _event(
                FederationEventType.SPACE_POLL_CLOSED,
                {"post_id": "p-b"},
                space_id="sp-a",
            )
        )
    assert repos["poll"].closed == []
    assert "is not in space sp-a" in caplog.text


async def test_schedule_created_cross_space_is_refused(repos, handlers, caplog):
    repos["poll"].post_space["p-b"] = "sp-b"
    with caplog.at_level("WARNING"):
        await handlers._on_schedule_created(
            _event(
                FederationEventType.SPACE_SCHEDULE_CREATED,
                {
                    "post_id": "p-b",
                    "title": "x",
                    "slots": [{"id": "s", "slot_date": "2026-07-01"}],
                },
                space_id="sp-a",
            )
        )
    assert repos["poll"].scheduled == []
    assert "is not in space sp-a" in caplog.text


@pytest.mark.parametrize("response", ["yes", "retracted"])
async def test_schedule_response_cross_space_is_refused(
    repos, handlers, caplog, response
):
    repos["poll"].slot_space["s-b"] = "sp-b"
    repos["poll"].responses[("s-b", "u")] = "no"
    with caplog.at_level("WARNING"):
        await handlers._on_schedule_response_updated(
            _event(
                FederationEventType.SPACE_SCHEDULE_RESPONSE_UPDATED,
                {"slot_id": "s-b", "user_id": "u", "response": response},
                space_id="sp-a",
            )
        )
    assert repos["poll"].responses == {("s-b", "u"): "no"}
    assert "is not in space sp-a" in caplog.text


async def test_schedule_response_applies_in_space(repos, handlers):
    await handlers._on_schedule_response_updated(
        _event(
            FederationEventType.SPACE_SCHEDULE_RESPONSE_UPDATED,
            {"slot_id": "s-1", "user_id": "u", "response": "maybe"},
            space_id="sp-1",
        )
    )
    assert repos["poll"].responses == {("s-1", "u"): "maybe"}
    await handlers._on_schedule_response_updated(
        _event(
            FederationEventType.SPACE_SCHEDULE_RESPONSE_UPDATED,
            {"slot_id": "s-1", "user_id": "u", "response": "retracted"},
            space_id="sp-1",
        )
    )
    assert repos["poll"].responses == {}


async def test_schedule_finalized_is_scoped(repos, handlers, caplog):
    repos["poll"].post_space["p-b"] = "sp-b"
    with caplog.at_level("WARNING"):
        await handlers._on_schedule_finalized(
            _event(
                FederationEventType.SPACE_SCHEDULE_FINALIZED,
                {"post_id": "p-b", "slot_id": "s-1"},
                space_id="sp-a",
            )
        )
    assert repos["poll"].finalized == []
    assert "is not in space sp-a" in caplog.text
    await handlers._on_schedule_finalized(
        _event(
            FederationEventType.SPACE_SCHEDULE_FINALIZED,
            {"post_id": "p-1", "slot_id": "s-1"},
            space_id="sp-1",
        )
    )
    assert repos["poll"].finalized == [("p-1", "s-1")]


async def test_gallery_item_saved_cross_space_is_refused(gallery_handlers, caplog):
    handlers, gallery = gallery_handlers
    gallery.album_space["alb-b"] = "sp-b"
    with caplog.at_level("WARNING"):
        await handlers._on_gallery_item_saved(
            _event(
                FederationEventType.SPACE_GALLERY_ITEM_CREATED,
                {"id": "gi-x", "album_id": "alb-b", "uploaded_by": "u"},
                space_id="sp-a",
            ),
        )
    assert gallery.created == [] and gallery.counts == {}
    assert "is not in space sp-a" in caplog.text


async def test_gallery_item_deleted_cross_space_is_refused(gallery_handlers, caplog):
    handlers, gallery = gallery_handlers
    gallery.album_space["alb-b"] = "sp-b"
    gallery.items_by_id["gi-b"] = SimpleNamespace(id="gi-b", album_id="alb-b")
    with caplog.at_level("WARNING"):
        await handlers._on_gallery_item_deleted(
            _event(
                FederationEventType.SPACE_GALLERY_ITEM_DELETED,
                {"id": "gi-b"},
                space_id="sp-a",
            ),
        )
    assert gallery.deleted == [] and "gi-b" in gallery.items_by_id
    assert "is not in space sp-a" in caplog.text


class _FakeZoneRepo:
    def __init__(self) -> None:
        self.rows = _ScopedRows()
        self.upserted: list = []
        self.deleted: list[str] = []

    async def get(self, zone_id):
        return self.rows.row(zone_id)

    async def upsert(self, zone, *, space_id):
        if not self.rows.claim(zone.id, space_id):
            return False
        self.upserted.append(zone)
        return True

    async def delete(self, zone_id, *, space_id):
        if not self.rows.drop(zone_id, space_id):
            return False
        self.deleted.append(zone_id)
        return True


@pytest.fixture
def zone_handlers(bus, repos):
    zones = _FakeZoneRepo()
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
        zone_repo=zones,
    )
    h.attach_to(_FakeFederationService())
    return h, zones


_ZONE = {
    "zone_id": "z-1",
    "name": "Home",
    "latitude": 47.1,
    "longitude": 8.5,
    "radius_m": 100,
}


async def test_zone_upserted_and_deleted_in_space(zone_handlers):
    handlers, zones = zone_handlers
    await handlers._on_zone_upserted(
        _event(FederationEventType.SPACE_ZONE_UPSERTED, dict(_ZONE), space_id="sp-1")
    )
    assert [z.id for z in zones.upserted] == ["z-1"]
    assert zones.upserted[0].space_id == "sp-1"
    await handlers._on_zone_deleted(
        _event(
            FederationEventType.SPACE_ZONE_DELETED, {"zone_id": "z-1"}, space_id="sp-1"
        )
    )
    assert zones.deleted == ["z-1"]


async def test_zone_upserted_malformed_or_missing_drops(zone_handlers):
    handlers, zones = zone_handlers
    await handlers._on_zone_upserted(
        _event(
            FederationEventType.SPACE_ZONE_UPSERTED, {"zone_id": "z"}, space_id="sp-1"
        )
    )
    await handlers._on_zone_upserted(
        _event(
            FederationEventType.SPACE_ZONE_UPSERTED,
            dict(_ZONE, latitude="nope"),
            space_id="sp-1",
        )
    )
    await handlers._on_zone_upserted(
        _event(FederationEventType.SPACE_ZONE_UPSERTED, dict(_ZONE))
    )
    await handlers._on_zone_deleted(
        _event(FederationEventType.SPACE_ZONE_DELETED, {}, space_id="sp-1")
    )
    assert zones.upserted == [] and zones.deleted == []


async def test_zone_cross_space_is_refused(zone_handlers, caplog):
    handlers, zones = zone_handlers
    zones.rows.claim("z-1", "sp-b")
    with caplog.at_level("WARNING"):
        await handlers._on_zone_upserted(
            _event(
                FederationEventType.SPACE_ZONE_UPSERTED, dict(_ZONE), space_id="sp-a"
            )
        )
        await handlers._on_zone_deleted(
            _event(
                FederationEventType.SPACE_ZONE_DELETED,
                {"zone_id": "z-1"},
                space_id="sp-a",
            )
        )
    assert zones.upserted == [] and zones.deleted == []
    assert zones.rows.space_of["z-1"] == "sp-b"
    assert caplog.text.count("is not in space sp-a") == 2


async def test_bazaar_listing_created_cross_space_is_refused(bazaar_handlers, caplog):
    handlers, bazaar = bazaar_handlers
    bazaar.post_space["bzr-b"] = "sp-b"
    with caplog.at_level("WARNING"):
        await handlers._on_bazaar_listing_created(
            _event(
                FederationEventType.BAZAAR_LISTING_CREATED,
                {
                    "post_id": "bzr-b",
                    "seller_user_id": "u",
                    "mode": "fixed",
                    "title": "T",
                },
                space_id="sp-a",
            ),
        )
    assert bazaar.saved == []
    assert "is not in space sp-a" in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "sold", "winner_user_id": "evil", "winning_price": 1},
        {"status": "expired"},
        {"status": "cancelled"},
    ],
)
async def test_bazaar_listing_updated_cross_space_is_refused(
    bazaar_status_handlers, caplog, payload
):
    handlers, bazaar = bazaar_status_handlers
    bazaar.listing_space["bzr-b"] = "sp-b"
    with caplog.at_level("WARNING"):
        await handlers._on_bazaar_listing_updated(
            _event(
                FederationEventType.BAZAAR_LISTING_UPDATED,
                dict(payload, post_id="bzr-b"),
                space_id="sp-a",
            ),
        )
    assert bazaar.sold == [] and bazaar.expired == [] and bazaar.cancelled == []
    assert "is not in space sp-a" in caplog.text


async def test_bazaar_bid_placed_cross_space_is_refused(bazaar_bids_handlers, caplog):
    handlers, bazaar = bazaar_bids_handlers
    bazaar.listing_space["bzr-b"] = "sp-b"
    with caplog.at_level("WARNING"):
        await handlers._on_bazaar_bid_placed(
            _event(
                FederationEventType.BAZAAR_BID_PLACED,
                {
                    "bid_id": "bid-x",
                    "listing_post_id": "bzr-b",
                    "bidder_user_id": "u",
                    "amount": 1,
                },
                space_id="sp-a",
            ),
        )
    assert bazaar.placed == []
    assert "is not in space sp-a" in caplog.text


async def test_bazaar_offer_accepted_cross_space_is_refused(
    bazaar_bids_handlers, caplog
):
    handlers, bazaar = bazaar_bids_handlers
    bazaar.listing_space["bzr-b"] = "sp-b"
    bazaar.existing_bids["bid-b"] = SimpleNamespace(id="bid-b", listing_post_id="bzr-b")
    with caplog.at_level("WARNING"):
        await handlers._on_bazaar_offer_accepted(
            _event(
                FederationEventType.BAZAAR_OFFER_ACCEPTED,
                {"bid_id": "bid-b"},
                space_id="sp-a",
            ),
        )
    assert bazaar.accepted == []
    assert "is not in space sp-a" in caplog.text


async def test_bazaar_offer_accepted_unknown_bid_is_silent(bazaar_bids_handlers):
    handlers, bazaar = bazaar_bids_handlers
    await handlers._on_bazaar_offer_accepted(
        _event(
            FederationEventType.BAZAAR_OFFER_ACCEPTED,
            {"bid_id": "ghost"},
            space_id="sp-1",
        ),
    )
    assert bazaar.accepted == []


@pytest.mark.parametrize(
    "handler_name,payload",
    [
        ("_on_poll_vote", {"post_id": "p", "option_id": "o", "voter_user_id": "u"}),
        ("_on_poll_closed", {"post_id": "p"}),
        ("_on_schedule_created", {"post_id": "p", "title": "t", "slots": [{}]}),
        ("_on_schedule_response_updated", {"slot_id": "s", "user_id": "u"}),
        ("_on_schedule_finalized", {"post_id": "p", "slot_id": "s"}),
    ],
)
async def test_poll_handlers_need_a_space(repos, handlers, handler_name, payload):
    """No space at all → nothing is written."""
    await getattr(handlers, handler_name)(
        _event(FederationEventType.SPACE_POLL_CLOSED, payload)
    )
    poll = repos["poll"]
    assert not (poll.inserted or poll.closed or poll.scheduled or poll.finalized)
    assert poll.responses == {}


# ─── §24.11 authorship ──────────────────────────────────────────────
#
# Every handler consults the authorship binder with the RIGHT user: the
# claimed author on a create, the stored owner on an edit / delete, the
# voter / bidder / seller for the personal and owner-only actions. With a
# binder that says "no", nothing is written.


class _FullBazaarRepo(_FakeBazaarRepoWithBids):
    def __init__(self) -> None:
        super().__init__()
        self.saved: list = []
        self.cancelled: list[str] = []
        self.status = BazaarStatus.ACTIVE
        self.no_listing = False

    async def get_listing(self, post_id):
        # ``p-new*`` is a wrapper post with no listing yet.
        if self.no_listing or post_id.startswith("p-new"):
            return None
        return SimpleNamespace(
            post_id=post_id,
            space_id=self.listing_space.get(post_id, "sp-1"),
            seller_user_id="u-seller",
            status=self.status,
        )

    async def save_listing(self, listing, *, space_id):
        self.saved.append(listing)
        return True

    async def mark_cancelled(self, post_id, *, space_id):
        self.cancelled.append(post_id)
        return True


@pytest.fixture
def full(bus, repos):
    gallery = _FakeGalleryRepo()
    zones = _FakeZoneRepo()
    bazaar = _FullBazaarRepo()
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
        poll_repo=repos["poll"],
        gallery_repo=gallery,
        zone_repo=zones,
        bazaar_repo=bazaar,
    )
    h.attach_to(_FakeFederationService())
    repos.update(gallery=gallery, zones=zones, bazaar=bazaar)
    return h, repos


def _cal(**extra):
    return {
        "id": "e-new",
        "calendar_id": "sp-1",
        "summary": "x",
        "created_by": "u-claimed",
        "start": "2026-06-10T18:00:00+00:00",
        "end": "2026-06-10T19:00:00+00:00",
        **extra,
    }


_OCC = "2026-06-10T18:00:00+00:00"

#: ``(handler, payload, rule, user the rule must be asked about, seed)``
_BOUND = [
    (
        "_on_task_saved",
        {"id": "t-new", "list_id": "l", "title": "x", "created_by": "u-claimed"},
        "may_author",
        "u-claimed",
        None,
    ),
    (
        "_on_task_saved",
        {"id": "t-1", "list_id": "l", "title": "x", "created_by": "u-claimed"},
        "writes_here",
        "",
        ("task", "t-1"),
    ),
    ("_on_task_deleted", {"id": "t-1"}, "writes_here", "", ("task", "t-1")),
    (
        "_on_page_saved",
        {"id": "pg-new", "title": "x", "created_by": "u-claimed"},
        "may_author",
        "u-claimed",
        None,
    ),
    (
        "_on_page_saved",
        {"id": "pg-1", "title": "x", "created_by": "u-claimed"},
        "writes_here",
        "",
        ("page", "pg-1"),
    ),
    ("_on_page_deleted", {"id": "pg-1"}, "writes_here", "", ("page", "pg-1")),
    (
        "_on_sticky_saved",
        {"id": "st-new", "author": "u-claimed", "content": "x"},
        "may_author",
        "u-claimed",
        None,
    ),
    (
        "_on_sticky_saved",
        {"id": "st-1", "content": "x"},
        "writes_here",
        "",
        ("sticky", "st-1"),
    ),
    ("_on_sticky_deleted", {"id": "st-1"}, "writes_here", "", ("sticky", "st-1")),
    ("_on_calendar_saved", _cal(), "may_author", "u-claimed", None),
    (
        "_on_rsvp_updated",
        {
            "event_id": "e-1",
            "user_id": "u-rsvp",
            "status": "going",
            "occurrence_at": _OCC,
        },
        "acts_for",
        "u-rsvp",
        None,
    ),
    (
        "_on_rsvp_deleted",
        {"event_id": "e-1", "user_id": "u-rsvp", "occurrence_at": _OCC},
        "acts_for",
        "u-rsvp",
        "rsvp",
    ),
    (
        "_on_poll_vote",
        {"post_id": "p-1", "option_id": "o-1", "voter_user_id": "u-voter"},
        "acts_for",
        "u-voter",
        None,
    ),
    ("_on_poll_closed", {"post_id": "p-1"}, "acts_for", "u-seller", None),
    (
        "_on_schedule_created",
        {"post_id": "p-1", "title": "t", "slots": [{"id": "s"}]},
        "may_author",
        "u-seller",
        None,
    ),
    (
        "_on_schedule_response_updated",
        {"slot_id": "s-1", "user_id": "u-voter", "response": "yes"},
        "acts_for",
        "u-voter",
        None,
    ),
    (
        "_on_schedule_finalized",
        {"post_id": "p-1", "slot_id": "s-1"},
        "acts_for",
        "u-seller",
        None,
    ),
    (
        "_on_gallery_item_saved",
        {"id": "gi-new", "album_id": "alb", "uploaded_by": "u-claimed"},
        "may_author",
        "u-claimed",
        None,
    ),
    ("_on_gallery_item_deleted", {"id": "gi-1"}, "may_mutate", "u-up", "gallery"),
    (
        "_on_zone_upserted",
        {"zone_id": "z", "name": "Z", "latitude": 1, "longitude": 1, "radius_m": 9},
        "is_moderator",
        "",
        None,
    ),
    ("_on_zone_deleted", {"zone_id": "z"}, "is_moderator", "", ("zones", "z")),
    (
        "_on_bazaar_listing_created",
        {
            "post_id": "p-new",
            "seller_user_id": "u-seller",
            "mode": "offer",
            "title": "x",
            "currency": "EUR",
        },
        "may_author",
        "u-seller",
        None,
    ),
    (
        "_on_bazaar_listing_updated",
        {"post_id": "p-1", "status": "cancelled"},
        "acts_for",
        "u-seller",
        None,
    ),
    (
        "_on_bazaar_bid_placed",
        {
            "bid_id": "b-new",
            "listing_post_id": "p-1",
            "bidder_user_id": "u-bidder",
            "amount": 5,
        },
        "acts_for",
        "u-bidder",
        None,
    ),
    ("_on_bazaar_offer_accepted", {"bid_id": "b-1"}, "acts_for", "u-seller", "bid"),
]


def _seed(repos, seed):
    if seed is None:
        return
    if seed == "gallery":
        repos["gallery"].items_by_id["gi-1"] = SimpleNamespace(
            id="gi-1", album_id="alb", uploaded_by="u-up", url="", thumbnail_url=""
        )
        return
    if seed == "rsvp":
        repos["calendar"].rsvps[("e-1", "u-rsvp", _OCC)] = SimpleNamespace(
            user_id="u-rsvp", status="going"
        )
        return
    if seed == "bid":
        repos["bazaar"].existing_bids["b-1"] = SimpleNamespace(
            id="b-1", listing_post_id="p-1"
        )
        return
    kind, row_id = seed
    repos[kind].rows.claim(row_id, "sp-1")


def _writes(repos) -> tuple:
    """Everything a handler could have written, in one comparable value."""
    poll = repos["poll"]
    return (
        list(repos["task"].saved),
        list(repos["task"].deleted),
        list(repos["page"].saved),
        list(repos["page"].deleted),
        list(repos["sticky"].saved),
        list(repos["sticky"].deleted),
        list(repos["calendar"].saved),
        dict(repos["calendar"].rsvps),
        dict(repos["calendar"].buffer),
        list(poll.inserted),
        list(poll.closed),
        list(poll.scheduled),
        dict(poll.responses),
        list(poll.finalized),
        list(repos["gallery"].created),
        list(repos["gallery"].deleted),
        list(repos["zones"].upserted),
        list(repos["zones"].deleted),
        list(repos["bazaar"].saved),
        list(repos["bazaar"].cancelled),
        list(repos["bazaar"].placed),
        list(repos["bazaar"].accepted),
    )


def _prime(repos, seed):
    _seed(repos, seed)
    repos["calendar"]._events["e-1"] = ("sp-1", object())
    repos["poll"].valid_options.add(("p-1", "o-1"))


_BOUND_IDS = [f"{h}:{rule}:{user or '-'}" for h, _p, rule, user, _s in _BOUND]


@pytest.mark.parametrize(
    ("handler", "payload", "rule", "user", "seed"), _BOUND, ids=_BOUND_IDS
)
async def test_each_handler_asks_the_right_rule_about_the_right_user(
    full, handler, payload, rule, user, seed
):
    h, repos = full
    _prime(repos, seed)
    before = _writes(repos)
    await getattr(h, handler)(
        _event(FederationEventType.SPACE_POST_CREATED, dict(payload), space_id="sp-1")
    )
    assert (rule, "sp-1", user) in repos["auth"].calls, repos["auth"].calls
    # …and with a "yes", the write really lands (the control for the
    # refusal test below).
    assert _writes(repos) != before


@pytest.mark.parametrize(
    ("handler", "payload", "rule", "user", "seed"), _BOUND, ids=_BOUND_IDS
)
async def test_a_refused_author_writes_nothing(
    full, handler, payload, rule, user, seed
):
    h, repos = full
    _prime(repos, seed)
    repos["auth"].answer = False
    before = _writes(repos)
    await getattr(h, handler)(
        _event(FederationEventType.SPACE_POST_CREATED, dict(payload), space_id="sp-1")
    )
    assert _writes(repos) == before


async def test_a_collaborative_sticky_edit_keeps_the_stored_author(full):
    """``SPACE_STICKY_UPDATED`` carries no author; the edit applies under
    the row's own."""
    h, repos = full
    repos["sticky"].rows.claim("st-1", "sp-1")
    await h._on_sticky_saved(
        _event(
            FederationEventType.SPACE_STICKY_UPDATED,
            {"id": "st-1", "content": "edited"},
            space_id="sp-1",
        )
    )
    assert [s.author for s in repos["sticky"].saved] == ["u-author"]


async def test_a_new_sticky_without_an_author_is_dropped(full):
    h, repos = full
    await h._on_sticky_saved(
        _event(
            FederationEventType.SPACE_STICKY_CREATED,
            {"id": "st-new", "content": "x"},
            space_id="sp-1",
        )
    )
    assert repos["sticky"].saved == []


async def test_a_new_page_naming_nobody_needs_a_writer_household(full):
    h, repos = full
    await h._on_page_saved(
        _event(
            FederationEventType.SPACE_PAGE_CREATED,
            {"id": "pg-new", "title": "x"},
            space_id="sp-1",
        )
    )
    assert ("writes_here", "sp-1", "") in repos["auth"].calls
    assert len(repos["page"].saved) == 1


async def test_a_listing_must_hang_off_the_sellers_own_post(full):
    h, repos = full
    repos["post"].author["p-new"] = "u-other"
    await h._on_bazaar_listing_created(
        _event(
            FederationEventType.BAZAAR_LISTING_CREATED,
            {
                "post_id": "p-new",
                "seller_user_id": "u-seller",
                "mode": "offer",
                "title": "x",
                "currency": "EUR",
            },
            space_id="sp-1",
        )
    )
    assert repos["bazaar"].saved == []
    assert repos["auth"].refusals == ["p-new"]


async def test_a_listing_for_a_post_not_here_is_a_quiet_no_op(full, caplog):
    h, repos = full
    repos["post"].missing.add("p-1")
    with caplog.at_level("DEBUG"):
        await h._on_bazaar_listing_created(
            _event(
                FederationEventType.BAZAAR_LISTING_CREATED,
                {
                    "post_id": "p-1",
                    "seller_user_id": "u-seller",
                    "mode": "offer",
                    "title": "x",
                    "currency": "EUR",
                },
                space_id="sp-1",
            )
        )
    assert repos["bazaar"].saved == []
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_a_status_change_on_a_settled_listing_is_debug_not_warning(full, caplog):
    """A replayed ``BAZAAR_LISTING_UPDATED`` against a listing that is
    already terminal is delivery noise, not a security event."""
    h, repos = full
    repos["bazaar"].status = BazaarStatus.CANCELLED
    with caplog.at_level("DEBUG"):
        await h._on_bazaar_listing_updated(
            _event(
                FederationEventType.BAZAAR_LISTING_UPDATED,
                {"post_id": "p-1", "status": "cancelled"},
                space_id="sp-1",
            )
        )
    assert repos["bazaar"].cancelled == []
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert "already cancelled" in caplog.text


@pytest.mark.parametrize(
    ("handler", "payload"),
    [
        ("_on_task_deleted", {"id": "gone"}),
        ("_on_page_deleted", {"id": "gone"}),
        ("_on_sticky_deleted", {"id": "gone"}),
        ("_on_calendar_deleted", {"id": "gone"}),
        ("_on_gallery_item_deleted", {"id": "gone"}),
        ("_on_bazaar_listing_updated", {"post_id": "gone", "status": "expired"}),
        ("_on_poll_closed", {"post_id": "gone"}),
    ],
)
async def test_a_replayed_delete_of_a_row_not_here_is_debug_only(
    full, caplog, handler, payload
):
    h, repos = full
    repos["post"].missing.add("gone")
    repos["bazaar"].no_listing = True
    with caplog.at_level("DEBUG"):
        await getattr(h, handler)(
            _event(FederationEventType.SPACE_POST_DELETED, payload, space_id="sp-1")
        )
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert "gone not applied" in caplog.text


async def test_a_zone_from_a_non_moderator_is_refused_at_warning(full, caplog):
    h, repos = full
    repos["auth"].answer = False
    with caplog.at_level("WARNING"):
        await h._on_zone_deleted(
            _event(
                FederationEventType.SPACE_ZONE_DELETED,
                {"zone_id": "z"},
                space_id="sp-1",
            )
        )
    assert "does not moderate this space" in caplog.text


async def test_a_collaborative_write_from_a_seatless_household_warns(full, caplog):
    h, repos = full
    repos["page"].rows.claim("pg-1", "sp-1")
    repos["auth"].answer = False
    with caplog.at_level("WARNING"):
        await h._on_page_deleted(
            _event(
                FederationEventType.SPACE_PAGE_DELETED, {"id": "pg-1"}, space_id="sp-1"
            )
        )
    assert repos["page"].deleted == []
    assert "holds no writer seat here" in caplog.text


async def test_a_new_zone_binds_its_claimed_creator(full):
    h, repos = full

    class _ModeratorButNotAuthor(_AllowAuthorship):
        async def may_author(self, event, space_id, user_id):
            self.calls.append(("may_author", space_id, user_id))
            return False

    h._authorship = _ModeratorButNotAuthor()
    await h._on_zone_upserted(
        _event(
            FederationEventType.SPACE_ZONE_UPSERTED,
            {
                "zone_id": "z-new",
                "name": "Z",
                "latitude": 1,
                "longitude": 1,
                "radius_m": 9,
                "created_by": "u-claimed",
            },
            space_id="sp-1",
        )
    )
    assert repos["zones"].upserted == []


async def test_an_existing_listing_is_never_re_created(full, caplog):
    """A re-sent ``BAZAAR_LISTING_CREATED`` would reset a settled listing's
    status and could hand it to another seller — it is a quiet no-op."""
    h, repos = full
    with caplog.at_level("DEBUG"):
        await h._on_bazaar_listing_created(
            _event(
                FederationEventType.BAZAAR_LISTING_CREATED,
                {
                    "post_id": "p-1",
                    "seller_user_id": "u-seller",
                    "mode": "offer",
                    "title": "x",
                    "currency": "EUR",
                },
                space_id="sp-1",
            )
        )
    assert repos["bazaar"].saved == []
    assert "listing already here" in caplog.text


async def test_an_expiry_from_a_non_seller_household_is_debug_noise(full, caplog):
    """Every household sweeps expiries; an older one still announces its
    result. Once the listing has ended that is noise, not a refusal."""
    h, repos = full

    async def _ended(post_id):
        return SimpleNamespace(
            post_id=post_id,
            space_id="sp-1",
            seller_user_id="u-seller",
            status=BazaarStatus.ACTIVE,
            end_time="2000-01-01T00:00:00+00:00",
        )

    repos["bazaar"].get_listing = _ended
    repos["auth"].answer = False
    with caplog.at_level("DEBUG"):
        await h._on_bazaar_listing_updated(
            _event(
                FederationEventType.BAZAAR_LISTING_UPDATED,
                {"post_id": "p-1", "status": "expired"},
                space_id="sp-1",
            )
        )
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert "expiry is announced by the seller's household" in caplog.text
    assert repos["auth"].refusals == []


# ─── Gallery review follow-ups ───────────────────────────────────────


@pytest.fixture
def gallery_env(bus, repos, tmp_path):
    """Handlers with media cleanup + tombstones wired, and the bus events
    they publish captured."""
    gallery = _FakeGalleryRepo()
    tombstones = GalleryAlbumTombstones()
    tombstones.wire(bus)
    media = tmp_path / "media"
    media.mkdir()
    h = SpaceContentInboundHandlers(
        bus=bus,
        authorship=repos["auth"],
        post_repo=repos["post"],
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
        gallery_repo=gallery,
        media_dir=media,
        media_refs=_NoRefs(),
        gallery_tombstones=tombstones,
    )
    seen: list[object] = []

    async def _capture(event) -> None:
        seen.append(event)

    for cls in (
        GalleryAlbumCreated,
        GalleryAlbumUpdated,
        GalleryAlbumDeleted,
        GalleryItemUploaded,
        GalleryItemDeleted,
    ):
        bus.subscribe(cls, _capture)
    return h, gallery, media, seen


class _NoRefs:
    """Media reference repo stub: nothing else references any file."""

    async def is_referenced(self, basename):
        return False

    async def referenced_basenames(self):
        return set()


def _created(**over):
    return _event(
        FederationEventType.SPACE_GALLERY_ALBUM_CREATED,
        _album_payload(**over),
        space_id="sp-1",
    )


async def test_the_system_identity_cannot_own_an_album(gallery_env):
    handlers, gallery, _media, _seen = gallery_env
    await handlers._on_gallery_album_created(_created(owner_user_id=SYSTEM_AUTHOR))
    assert gallery.albums == {}


async def test_the_per_space_album_limit_holds_for_federated_albums(gallery_env):
    handlers, gallery, _media, _seen = gallery_env
    gallery.space_album_count = ALBUMS_PER_SPACE
    await handlers._on_gallery_album_created(_created())
    assert gallery.albums == {}
    gallery.space_album_count = ALBUMS_PER_SPACE - 1
    await handlers._on_gallery_album_created(_created())
    assert "alb-new" in gallery.albums


async def test_a_held_album_redelivered_for_its_owner_is_a_quiet_no_op(
    gallery_env, caplog
):
    handlers, gallery, _media, seen = gallery_env
    await handlers._on_gallery_album_created(_created())
    calls = len(handlers._authorship.calls)
    with caplog.at_level("WARNING"):
        await handlers._on_gallery_album_created(_created(name="Again"))
    assert gallery.albums["alb-new"].name == "Holiday"
    assert len(handlers._authorship.calls) == calls  # nothing to authorise
    assert not caplog.records
    assert [type(e) for e in seen] == [GalleryAlbumCreated]


async def test_a_held_album_claimed_for_another_owner_is_refused(gallery_env, caplog):
    handlers, gallery, _media, seen = gallery_env
    await handlers._on_gallery_album_created(_created())
    seen.clear()
    with caplog.at_level("WARNING"):
        await handlers._on_gallery_album_created(_created(owner_user_id="u-other"))
    assert gallery.albums["alb-new"].owner_user_id == "u-remote"
    assert "already held for another owner" in caplog.text
    assert seen == []


async def test_a_delete_that_overtakes_its_create_keeps_the_album_away(gallery_env):
    handlers, gallery, _media, _seen = gallery_env
    await handlers._on_gallery_album_deleted(
        _event(
            FederationEventType.SPACE_GALLERY_ALBUM_DELETED,
            {"id": "alb-new"},
            space_id="sp-1",
        )
    )
    await handlers._on_gallery_album_created(_created())
    assert gallery.albums == {}


async def test_an_album_deleted_here_is_not_brought_back_by_a_replay(bus, gallery_env):
    handlers, gallery, _media, _seen = gallery_env
    await bus.publish(GalleryAlbumDeleted(album_id="alb-new", space_id="sp-1"))
    await handlers._on_gallery_album_created(_created())
    assert gallery.albums == {}


async def test_an_album_edit_can_clear_the_cover(gallery_env):
    handlers, gallery, _media, _seen = gallery_env
    await handlers._on_gallery_album_updated(
        _event(
            FederationEventType.SPACE_GALLERY_ALBUM_UPDATED,
            {"id": "alb-1", "name": "Same", "cover_item_id": None},
            space_id="sp-1",
        )
    )
    assert gallery.album_patches == [("alb-1", {"name": "Same", "cover_item_id": None})]


async def test_applied_album_changes_are_published_with_their_origin(gallery_env):
    """This household's own screens refresh; the origin keeps the outbound
    bridge from sending them back."""
    handlers, gallery, _media, seen = gallery_env
    await handlers._on_gallery_album_created(_created())
    await handlers._on_gallery_album_updated(
        _event(
            FederationEventType.SPACE_GALLERY_ALBUM_UPDATED,
            {"id": "alb-new", "name": "Renamed"},
            space_id="sp-1",
        )
    )
    await handlers._on_gallery_album_deleted(
        _event(
            FederationEventType.SPACE_GALLERY_ALBUM_DELETED,
            {"id": "alb-new"},
            space_id="sp-1",
        )
    )
    assert [type(e) for e in seen] == [
        GalleryAlbumCreated,
        GalleryAlbumUpdated,
        GalleryAlbumDeleted,
    ]
    assert {e.origin_instance_id for e in seen} == {"peer-a"}
    assert {e.space_id for e in seen} == {"sp-1"}
    assert seen[0].owner_id == "u-remote"


async def test_applied_item_changes_are_published_with_their_origin(gallery_env):
    handlers, gallery, _media, seen = gallery_env
    payload = {
        "id": "gi-1",
        "album_id": "alb-1",
        "uploaded_by": "u-remote",
        "item_type": "photo",
        "thumbnail_url": "api/media/t.webp",
    }
    for _ in range(2):  # a redelivery publishes nothing new
        await handlers._on_gallery_item_saved(
            _event(
                FederationEventType.SPACE_GALLERY_ITEM_CREATED,
                payload,
                space_id="sp-1",
            )
        )
    await handlers._on_gallery_item_deleted(
        _event(
            FederationEventType.SPACE_GALLERY_ITEM_DELETED,
            {"id": "gi-1"},
            space_id="sp-1",
        )
    )
    assert [type(e) for e in seen] == [GalleryItemUploaded, GalleryItemDeleted]
    up, gone = seen
    assert (up.item_id, up.album_id, up.uploader, up.space_id) == (
        "gi-1",
        "alb-1",
        "u-remote",
        "sp-1",
    )
    assert up.origin_instance_id == gone.origin_instance_id == "peer-a"


async def test_item_media_references_are_normalised(gallery_env):
    """Only the canonical local ``api/media/<name>`` shape is stored — a
    query string is dropped, anything else (a full URL, another path)
    stores nothing."""
    handlers, gallery, _media, _seen = gallery_env
    await handlers._on_gallery_item_saved(
        _event(
            FederationEventType.SPACE_GALLERY_ITEM_CREATED,
            {
                "id": "gi-n",
                "album_id": "alb-1",
                "uploaded_by": "u-remote",
                "url": "https://elsewhere.example/api/media/full.webp",
                "thumbnail_url": "/api/media/t.webp?sig=abc",
            },
            space_id="sp-1",
        )
    )
    item = gallery.created[-1]
    assert (item.url, item.thumbnail_url) == ("", "api/media/t.webp")


async def test_a_federated_item_delete_removes_its_files(gallery_env):
    handlers, gallery, media, _seen = gallery_env
    for name in ("full.webp", "thumb.webp"):
        (media / name).write_bytes(b"x")
    await handlers._on_gallery_item_saved(
        _event(
            FederationEventType.SPACE_GALLERY_ITEM_CREATED,
            {
                "id": "gi-f",
                "album_id": "alb-1",
                "uploaded_by": "u-remote",
                "url": "api/media/full.webp",
                "thumbnail_url": "api/media/thumb.webp",
            },
            space_id="sp-1",
        )
    )
    await handlers._on_gallery_item_deleted(
        _event(
            FederationEventType.SPACE_GALLERY_ITEM_DELETED,
            {"id": "gi-f"},
            space_id="sp-1",
        )
    )
    assert not (media / "full.webp").exists()
    assert not (media / "thumb.webp").exists()


async def test_a_federated_album_delete_removes_its_items_files(gallery_env):
    handlers, gallery, media, _seen = gallery_env
    (media / "a.webp").write_bytes(b"x")
    (media / "keep.webp").write_bytes(b"x")
    gallery.album_media["alb-1"] = ["api/media/a.webp"]
    await handlers._on_gallery_album_deleted(
        _event(
            FederationEventType.SPACE_GALLERY_ALBUM_DELETED,
            {"id": "alb-1"},
            space_id="sp-1",
        )
    )
    assert not (media / "a.webp").exists()
    assert (media / "keep.webp").exists()


async def test_a_refused_album_delete_keeps_the_files(gallery_env):
    handlers, gallery, media, _seen = gallery_env
    (media / "a.webp").write_bytes(b"x")
    gallery.album_media["alb-b"] = ["api/media/a.webp"]
    gallery.album_space["alb-b"] = "sp-b"
    await handlers._on_gallery_album_deleted(
        _event(
            FederationEventType.SPACE_GALLERY_ALBUM_DELETED,
            {"id": "alb-b"},
            space_id="sp-1",
        )
    )
    assert (media / "a.webp").exists()
