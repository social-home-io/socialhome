"""Tests for :class:`SpaceContentInboundHandlers` (§13)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.events import CalendarEventCreated, CalendarEventDeleted
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.federation_inbound import SpaceContentInboundHandlers


class _FakeRegistry:
    def __init__(self) -> None:
        self.registered = []

    def register(self, t, h):
        self.registered.append((t, h))


class _FakeFederationService:
    def __init__(self) -> None:
        self._event_registry = _FakeRegistry()


class _ScopedRows:
    """Mimics the repos' §24.11 scoping: a row id belongs to one space."""

    def __init__(self) -> None:
        self.space_of: dict[str, str | None] = {}

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
    def __init__(self) -> None:
        self.valid_options: set[tuple[str, str]] = set()
        self.cleared: list[tuple[str, str]] = []
        self.inserted: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.scheduled: list[dict] = []
        self.fail_create_schedule = False

    async def option_belongs_to_post(self, *, option_id, post_id):
        return (post_id, option_id) in self.valid_options

    async def clear_user_votes(self, *, post_id, voter_user_id):
        self.cleared.append((post_id, voter_user_id))

    async def insert_vote(self, *, option_id, voter_user_id):
        self.inserted.append((option_id, voter_user_id))

    async def close(self, post_id):
        self.closed.append(post_id)

    async def create_schedule_poll(self, *, post_id, title, deadline, slots):
        if self.fail_create_schedule:
            raise RuntimeError("FK violation simulated")
        self.scheduled.append(
            {
                "post_id": post_id,
                "title": title,
                "deadline": deadline,
                "slots": list(slots),
            },
        )


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
    }


@pytest.fixture
def handlers(bus, repos):
    h = SpaceContentInboundHandlers(
        bus=bus,
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
        )
    )
    assert repos["poll"].closed == ["p-1"]


async def test_poll_handlers_not_registered_without_poll_repo(bus, repos):
    """Deployments without polls skip those events cleanly."""
    h = SpaceContentInboundHandlers(
        bus=bus,
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
    """Stub matching the slice of ``AbstractGalleryRepo`` the handler uses."""

    def __init__(self) -> None:
        self.created = []
        self.deleted = []
        self.counts: dict[str, int] = {}
        self.items_by_id: dict[str, object] = {}
        self.fail_create = False

    async def create_item(self, item):
        if self.fail_create:
            raise RuntimeError("fk-violation simulated")
        self.created.append(item)
        self.items_by_id[item.id] = item
        return item

    async def increment_item_count(self, album_id, delta):
        self.counts[album_id] = self.counts.get(album_id, 0) + int(delta)

    async def get_item(self, item_id):
        return self.items_by_id.get(item_id)

    async def delete_item(self, item_id):
        self.deleted.append(item_id)
        self.items_by_id.pop(item_id, None)


@pytest.fixture
def gallery_handlers(bus, repos):
    gallery = _FakeGalleryRepo()
    h = SpaceContentInboundHandlers(
        bus=bus,
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
        _event(FederationEventType.SPACE_GALLERY_ITEM_DELETED, {"id": "gi-del"}),
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


async def test_gallery_handlers_not_registered_without_repo(bus, repos):
    """No gallery_repo → events not registered."""
    h = SpaceContentInboundHandlers(
        bus=bus,
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


# ─── Bazaar listings (#PR445) ─────────────────────────────────────────


class _FakeBazaarRepo:
    """Stub matching the slice of ``AbstractBazaarRepo`` the handler uses."""

    def __init__(self) -> None:
        self.saved: list = []
        self.fail = False

    async def save_listing(self, listing):
        if self.fail:
            raise RuntimeError("fk-violation simulated")
        self.saved.append(listing)
        return listing


@pytest.fixture
def bazaar_handlers(bus, repos):
    bazaar = _FakeBazaarRepo()
    h = SpaceContentInboundHandlers(
        bus=bus,
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
    """Stub matching the AbstractBazaarRepo slice the F8 handler uses."""

    def __init__(self) -> None:
        self.sold: list[tuple[str, str, int]] = []
        self.expired: list[str] = []
        self.cancelled: list[str] = []
        self.fail = False

    async def save_listing(self, listing):
        return listing

    async def mark_sold(self, post_id, *, winner_user_id, winning_price):
        if self.fail:
            raise ValueError("not active")
        self.sold.append((post_id, winner_user_id, int(winning_price)))

    async def mark_expired(self, post_id):
        if self.fail:
            raise ValueError("not active")
        self.expired.append(post_id)

    async def mark_cancelled(self, post_id):
        if self.fail:
            raise ValueError("not active")
        self.cancelled.append(post_id)


@pytest.fixture
def bazaar_status_handlers(bus, repos):
    bazaar = _FakeBazaarRepoWithStatus()
    h = SpaceContentInboundHandlers(
        bus=bus,
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
        self.fail_place = False

    async def save_listing(self, listing):
        return listing

    async def get_bid(self, bid_id):
        return self.existing_bids.get(bid_id)

    async def place_bid(self, bid):
        if self.fail_place:
            raise ValueError("listing not active")
        self.placed.append(bid)
        self.existing_bids[bid.id] = bid
        return bid

    async def accept_offer(self, bid_id):
        self.accepted.append(bid_id)


@pytest.fixture
def bazaar_bids_handlers(bus, repos):
    bazaar = _FakeBazaarRepoWithBids()
    h = SpaceContentInboundHandlers(
        bus=bus,
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


# ─── §24.11 cross-space refusals (issue #693) ─────────────────────────────
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
