"""Released content must match the queue item it claims to release (v_43)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

import pytest

from socialhome.domain.events import StickyCreated
from socialhome.domain.federation import FederationEventType
from socialhome.domain.space import ModerationStatus, SpaceModerationItem
from socialhome.domain.task import (
    Task,
    TaskList,
    TaskPriority,
    TaskStatus,
    task_list_to_wire_dict,
    task_to_wire_dict,
)
from socialhome.federation.moderation_approval import (
    FEATURE_OF_EVENT,
    FREE_KEYS,
    approval_target,
    item_matches_event,
)

FET = FederationEventType
NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _item(feature: str, action: str, payload: dict) -> SpaceModerationItem:
    return SpaceModerationItem(
        id="item-1",
        space_id="sp",
        feature=feature,
        action=action,
        submitted_by="u-a",
        payload=payload,
        current_snapshot=None,
        submitted_at=NOW,
        expires_at=NOW + timedelta(days=7),
        status=ModerationStatus.PENDING,
    )


TASK_CREATE = {
    "entity": "task",
    "target_id": "t-1",
    "list_id": "l-1",
    "title": "Buy milk",
    "description": None,
    "status": "todo",
    "due_date": "2026-06-10",
    "assignees": ["u-a"],
    "priority": None,
    "labels": ["shop"],
}
TASK_WIRE = {
    "id": "t-1",
    "list_id": "l-1",
    "space_id": "sp",
    "title": "Buy milk",
    "status": "todo",
    "position": 4,
    "created_by": "u-a",
    "created_at": "2026-06-01T10:00:00+00:00",
    "description": None,
    "due_date": "2026-06-10",
    "assignees": ["u-a"],
    "archived_at": None,
    "priority": None,
    "labels": ["shop"],
    "actor_user_id": "u-a",
}

HELD_TASK = {
    "list_id": "l-1",
    "title": "Buy milk",
    "description": None,
    "status": "todo",
    "due_date": "2026-06-10",
    "assignees": ["u-a"],
    "position": 4,
    "priority": None,
    "labels": ["shop"],
    "created_by": "u-a",
    "archived": False,
}

POST_CREATE = {
    "entity": "post",
    "target_id": "p-1",
    "post_id": "p-1",
    "type": "text",
    "content": "hello",
    "media_url": None,
    "image_urls": [],
    "file_meta": None,
    "location": {"lat": 1.2345, "lon": 2.3456, "label": None},
    "linked_event_id": None,
    "linked_highlight_id": None,
    "hidden_from_feed": False,
    "no_link_preview": False,
    "link_preview": None,
    "attachments": {
        "schedule": {
            "title": "When?",
            "slots": [
                {
                    "slot_date": "2026-07-01",
                    "start_time": "10:00",
                    "end_time": None,
                    "position": 0,
                }
            ],
            "deadline": None,
        }
    },
}
POST_WIRE = {
    "id": "p-1",
    "space_id": "sp",
    "author": "u-a",
    "actor_user_id": "u-a",
    "type": "text",
    "content": "hello",
    "media_url": None,
    "image_urls": [],
    "occurred_at": "2026-06-02T00:00:00+00:00",
    "hidden_from_feed": False,
    "location": {"lat": 1.2345, "lon": 2.3456, "label": None},
}


@pytest.mark.parametrize(
    ("item", "event_type", "wire", "held"),
    [
        (
            _item("tasks", "create", TASK_CREATE),
            FET.SPACE_TASK_CREATED,
            TASK_WIRE,
            None,
        ),
        (
            _item(
                "tasks",
                "edit",
                {"entity": "task", "target_id": "t-1", "patch": {"title": "Oat"}},
            ),
            FET.SPACE_TASK_UPDATED,
            {**TASK_WIRE, "title": "Oat"},
            HELD_TASK,
        ),
        (
            _item(
                "tasks",
                "delete",
                {"entity": "task", "target_id": "t-1", "op": "archive"},
            ),
            FET.SPACE_TASK_UPDATED,
            {**TASK_WIRE, "archived_at": "2026-06-02T00:00:00+00:00"},
            HELD_TASK,
        ),
        (
            _item(
                "tasks",
                "delete",
                {"entity": "task", "target_id": "t-1", "op": "delete"},
            ),
            FET.SPACE_TASK_DELETED,
            {"id": "t-1", "list_id": "l-1", "space_id": "sp"},
            None,
        ),
        (
            _item(
                "tasks", "create", {"entity": "list", "target_id": "l-9", "name": "N"}
            ),
            FET.SPACE_TASK_LIST_CREATED,
            {"id": "l-9", "space_id": "sp", "name": "N", "created_by": "u-a"},
            None,
        ),
        (
            _item("posts", "create", POST_CREATE),
            FET.SPACE_POST_CREATED,
            POST_WIRE,
            None,
        ),
        (
            _item("posts", "create", POST_CREATE),
            FET.SPACE_SCHEDULE_CREATED,
            {
                "post_id": "p-1",
                "space_id": "sp",
                "title": "When?",
                "deadline": None,
                "slots": [
                    {
                        "id": "s-x",
                        "slot_date": "2026-07-01",
                        "start_time": "10:00",
                        "end_time": None,
                        "position": 0,
                    }
                ],
            },
            None,
        ),
        (
            _item(
                "pages",
                "create",
                {
                    "entity": "page",
                    "target_id": "pg-1",
                    "title": "Wiki",
                    "content": "body",
                    "cover_image_url": None,
                },
            ),
            FET.SPACE_PAGE_CREATED,
            {"id": "pg-1", "title": "Wiki", "content": "body", "created_by": "u-a"},
            None,
        ),
        (
            _item(
                "stickies",
                "edit",
                {"entity": "sticky", "target_id": "s-1", "patch": {"color": "#FFFFFF"}},
            ),
            FET.SPACE_STICKY_UPDATED,
            {"id": "s-1", "content": "x", "color": "#FFFFFF", "position_x": 1.0},
            {"content": "x", "color": "#FFF9B1", "author": "u-h"},
        ),
        (
            _item(
                "calendar",
                "create",
                {
                    "entity": "event",
                    "target_id": "ev-1",
                    "summary": "Picnic",
                    "start": "2026-06-10T18:00:00+00:00",
                    "end": "2026-06-10T19:00:00+00:00",
                    "description": None,
                    "all_day": False,
                    "attendees": [],
                    "rrule": None,
                    "capacity": None,
                    "cover_url": None,
                    "location": "Park",
                    "tz": None,
                    "announce_in_feed": False,
                },
            ),
            FET.SPACE_CALENDAR_EVENT_CREATED,
            {
                "event_id": "ev-1",
                "calendar_id": "sp",
                "summary": "Picnic",
                "start": "2026-06-10T18:00:00+00:00",
                "end": "2026-06-10T19:00:00+00:00",
                "description": None,
                "all_day": False,
                "attendees": [],
                "rrule": None,
                "cover_url": None,
                "location": "Park",
                "tz": "Europe/Zurich",
                "created_by": "u-a",
            },
            None,
        ),
    ],
)
def test_the_release_of_an_item_matches_it(item, event_type, wire, held):
    assert item_matches_event(item, event_type, wire, held=held)


def test_tampered_content_does_not_match():
    item = _item("tasks", "create", TASK_CREATE)
    for key, value in (
        ("title", "Buy beer"),
        ("list_id", "l-2"),
        ("assignees", ["u-x"]),
        ("labels", []),
        ("due_date", "2026-06-11"),
    ):
        assert not item_matches_event(
            item, FET.SPACE_TASK_CREATED, {**TASK_WIRE, key: value}
        ), key
    post = _item("posts", "create", POST_CREATE)
    assert not item_matches_event(
        post, FET.SPACE_POST_CREATED, {**POST_WIRE, "content": "spam"}
    )
    assert not item_matches_event(
        post, FET.SPACE_POST_CREATED, {**POST_WIRE, "image_urls": ["api/media/x"]}
    )


def test_another_target_or_kind_of_write_does_not_match():
    item = _item("tasks", "create", TASK_CREATE)
    assert not item_matches_event(
        item, FET.SPACE_TASK_CREATED, {**TASK_WIRE, "id": "t-other"}
    )
    # A create item never authorises an edit or a delete of the row.
    assert not item_matches_event(item, FET.SPACE_TASK_UPDATED, TASK_WIRE)
    assert not item_matches_event(item, FET.SPACE_TASK_DELETED, {"id": "t-1"})
    # Another feature's event.
    assert not item_matches_event(item, FET.SPACE_PAGE_CREATED, {"id": "t-1"})
    # A delete item never authorises an archive and vice versa.
    delete = _item(
        "tasks", "delete", {"entity": "task", "target_id": "t-1", "op": "delete"}
    )
    assert not item_matches_event(
        delete, FET.SPACE_TASK_UPDATED, {**TASK_WIRE, "archived_at": "x"}
    )
    archive = _item(
        "tasks", "delete", {"entity": "task", "target_id": "t-1", "op": "archive"}
    )
    assert not item_matches_event(archive, FET.SPACE_TASK_DELETED, {"id": "t-1"})
    assert not item_matches_event(
        archive, FET.SPACE_TASK_UPDATED, TASK_WIRE, held=HELD_TASK
    )


def test_a_purged_item_matches_nothing():
    item = _item("tasks", "create", {})
    assert not item_matches_event(item, FET.SPACE_TASK_CREATED, TASK_WIRE)


def test_approval_target_reads_the_row_id_per_event_shape():
    assert approval_target(FET.SPACE_SCHEDULE_CREATED, {"post_id": "p"}) == "p"
    assert approval_target(FET.BAZAAR_LISTING_CREATED, {"post_id": "p"}) == "p"
    assert approval_target(FET.SPACE_CALENDAR_EVENT_DELETED, {"event_id": "e"}) == "e"
    assert approval_target(FET.SPACE_TASK_CREATED, {"id": "t"}) == "t"
    assert approval_target(FET.SPACE_PAGE_DELETED, {"page_id": "g"}) == "g"
    assert approval_target(FET.SPACE_TASK_CREATED, {}) == ""


def test_only_reviewable_content_events_can_carry_a_release():
    assert FET.SPACE_COMMENT_CREATED not in FEATURE_OF_EVENT
    assert FET.SPACE_RSVP_UPDATED not in FEATURE_OF_EVENT
    assert FET.SPACE_POST_UPDATED not in FEATURE_OF_EVENT
    assert FET.SPACE_POST_DELETED not in FEATURE_OF_EVENT


def test_an_edit_may_change_nothing_outside_its_patch():
    """I1: a release of a status change that also rewrites the title, the
    description or the assignees is not that item's release."""
    edit = _item(
        "tasks",
        "edit",
        {"entity": "task", "target_id": "t-1", "patch": {"status": "done"}},
    )
    ok = {**TASK_WIRE, "status": "done"}
    assert item_matches_event(edit, FET.SPACE_TASK_UPDATED, ok, held=HELD_TASK)
    for key, value in (
        ("title", "Anna says: hanna is a thief"),
        ("description", "forged"),
        ("assignees", ["u-x"]),
        ("created_by", "u-x"),
        ("archived_at", "2026-06-02T00:00:00+00:00"),
    ):
        assert not item_matches_event(
            edit, FET.SPACE_TASK_UPDATED, {**ok, key: value}, held=HELD_TASK
        ), key
    # A layout field moved meanwhile is no content change.
    assert item_matches_event(
        edit, FET.SPACE_TASK_UPDATED, {**ok, "position": 9}, held=HELD_TASK
    )
    # Without our copy of the row an edit is never accepted.
    assert not item_matches_event(edit, FET.SPACE_TASK_UPDATED, ok)


def test_a_create_sets_no_field_the_item_did_not():
    """I1: a create's release carries nothing beyond the item — no
    recurrence, archive stamp, other creator, mirror origin, settled listing."""
    item = _item("tasks", "create", TASK_CREATE)
    for key, value in (
        ("recurrence", {"rrule": "FREQ=DAILY", "last_spawned_at": None}),
        ("recurrence_parent_id", "t-0"),
        ("archived_at", "2026-06-02T00:00:00+00:00"),
        ("created_by", "u-x"),
    ):
        assert not item_matches_event(
            item, FET.SPACE_TASK_CREATED, {**TASK_WIRE, key: value}
        ), key
    post = _item("posts", "create", POST_CREATE)
    assert not item_matches_event(
        post, FET.SPACE_POST_CREATED, {**POST_WIRE, "author": "u-x"}
    )


# ── Fail closed: every key a release carries is classified (M1) ─────────


def _sweep(item, event_type, wire, held=None):
    """The honest release matches; changing ANY non-free key, or adding an
    unclassified one, refuses it."""
    assert item_matches_event(item, event_type, wire, held=held)
    for key in set(wire) - FREE_KEYS[event_type]:
        value = wire[key]
        mutated = {
            **wire,
            key: (not value) if isinstance(value, bool) else "⚠ tampered",
        }
        assert not item_matches_event(item, event_type, mutated, held=held), key
    assert not item_matches_event(
        item, event_type, {**wire, "brand_new_field": 1}, held=held
    )


def _full_task(**over) -> Task:
    base = dict(
        id="t-1",
        list_id="l-1",
        title="Buy milk",
        status=TaskStatus.TODO,
        position=4,
        created_by="u-a",
        created_at=NOW,
        updated_at=NOW,
        description="Oat",
        due_date=date(2026, 6, 10),
        assignees=("u-a",),
        priority=TaskPriority.HIGH,
        labels=("shop",),
    )
    base.update(over)
    return Task(**base)


def test_a_task_codec_release_is_compared_on_every_applied_key():
    """The guard: walks the REAL task wire codec — a field added to it
    fails here until a rule classifies it."""
    task = _full_task()
    wire = {**task_to_wire_dict(task, "sp"), "actor_user_id": "u-a"}
    create = _item(
        "tasks",
        "create",
        {
            "entity": "task",
            "target_id": "t-1",
            "list_id": "l-1",
            "title": "Buy milk",
            "description": "Oat",
            "status": "todo",
            "due_date": "2026-06-10",
            "assignees": ["u-a"],
            "priority": "high",
            "labels": ["shop"],
        },
    )
    _sweep(create, FET.SPACE_TASK_CREATED, wire)
    held = {
        "list_id": "l-1",
        "title": "Buy milk",
        "description": "Oat",
        "status": "todo",
        "due_date": "2026-06-10",
        "assignees": ["u-a"],
        "position": 4,
        "priority": "high",
        "labels": ["shop"],
        "created_by": "u-a",
        "archived": False,
        "recurrence": None,
        "recurrence_parent_id": None,
    }
    edit = _item(
        "tasks",
        "edit",
        {"entity": "task", "target_id": "t-1", "patch": {"status": "done"}},
    )
    done = {**wire, "status": "done"}
    _sweep(edit, FET.SPACE_TASK_UPDATED, done, held=held)


def test_a_task_edit_release_cannot_add_a_recurrence():
    """Probe P4: a status edit whose release also makes the task repeat."""
    edit = _item(
        "tasks",
        "edit",
        {"entity": "task", "target_id": "t-1", "patch": {"status": "done"}},
    )
    wire = {**TASK_WIRE, "status": "done"}
    held = {**HELD_TASK, "recurrence": None, "recurrence_parent_id": None}
    assert item_matches_event(edit, FET.SPACE_TASK_UPDATED, wire, held=held)
    for key, value in (
        ("recurrence", {"rrule": "FREQ=DAILY", "last_spawned_at": None}),
        ("recurrence_parent_id", "t-0"),
    ):
        assert not item_matches_event(
            edit, FET.SPACE_TASK_UPDATED, {**wire, key: value}, held=held
        ), key


def test_a_list_codec_release_is_compared_on_every_applied_key():
    lst = TaskList(id="l-9", name="Groceries", created_by="u-a")
    wire = {**task_list_to_wire_dict(lst, "sp"), "actor_user_id": "u-a"}
    item = _item(
        "tasks", "create", {"entity": "list", "target_id": "l-9", "name": "Groceries"}
    )
    _sweep(item, FET.SPACE_TASK_LIST_CREATED, wire)


def test_a_sticky_release_is_compared_on_every_applied_key():
    created = asdict(
        StickyCreated(
            sticky_id="s-1",
            space_id="sp",
            author="u-a",
            content="Bring cake",
            color="#FFF9B1",
            position_x=1.0,
            position_y=2.0,
            actor_user_id="u-a",
        )
    )
    created.pop("occurred_at")
    created["id"] = created.pop("sticky_id")
    item = _item(
        "stickies",
        "create",
        {
            "entity": "sticky",
            "target_id": "s-1",
            "content": "Bring cake",
            "color": "#FFF9B1",
            "position_x": 1.0,
            "position_y": 2.0,
        },
    )
    _sweep(item, FET.SPACE_STICKY_CREATED, created)


def test_calendar_page_and_post_releases_are_compared_on_every_key():
    event = _item(
        "calendar",
        "create",
        {
            "entity": "event",
            "target_id": "ev-1",
            "summary": "Picnic",
            "start": "2026-06-10T18:00:00+00:00",
            "end": "2026-06-10T19:00:00+00:00",
            "description": "Bring food",
            "all_day": False,
            "attendees": ["u-a"],
            "rrule": "FREQ=WEEKLY",
            "capacity": None,
            "cover_url": "api/media/c.webp",
            "location": "Park",
            "tz": "Europe/Zurich",
            # A dropped announcement is the one change the apply may make;
            # an item that asked for none may never gain one.
            "announce_in_feed": False,
        },
    )
    event_wire = {
        "space_id": "sp",
        "event_id": "ev-1",
        "calendar_id": "sp",
        "summary": "Picnic",
        "description": "Bring food",
        "start": "2026-06-10T18:00:00+00:00",
        "end": "2026-06-10T19:00:00+00:00",
        "all_day": False,
        "attendees": ["u-a"],
        "created_by": "u-a",
        "rrule": "FREQ=WEEKLY",
        "cover_url": "api/media/c.webp",
        "location": "Park",
        "tz": "Europe/Zurich",
        "announce_in_feed": False,
        "actor_user_id": "u-a",
    }
    _sweep(event, FET.SPACE_CALENDAR_EVENT_CREATED, event_wire)
    page = _item(
        "pages",
        "create",
        {"entity": "page", "target_id": "pg-1", "title": "Wiki", "content": "b"},
    )
    page_wire = {
        "id": "pg-1",
        "page_id": "pg-1",
        "space_id": "sp",
        "title": "Wiki",
        "content": "b",
        "actor_user_id": "u-a",
        "created_by": "u-a",
    }
    _sweep(page, FET.SPACE_PAGE_CREATED, page_wire)
    post = _item("posts", "create", POST_CREATE)
    _sweep(post, FET.SPACE_POST_CREATED, POST_WIRE)
