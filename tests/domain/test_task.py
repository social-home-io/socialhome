"""Tests for socialhome.domain.task."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timezone

from socialhome.domain.task import (
    MAX_TASK_ASSIGNEES,
    MAX_TASK_DESCRIPTION_LENGTH,
    MAX_TASK_LIST_NAME_LENGTH,
    MAX_TASK_TITLE_LENGTH,
    MAX_TASK_LABEL_LENGTH,
    MAX_TASK_LABELS,
    UNSET,
    RecurrenceRule,
    Task,
    TaskList,
    TaskPriority,
    TaskStatus,
    TaskUpdate,
    normalize_labels,
    sanitize_block,
    sanitize_line,
    task_from_wire_dict,
    task_list_from_wire_dict,
    task_list_to_wire_dict,
    task_to_wire_dict,
)


def test_task_lifecycle():
    """Task moves through todo → in_progress → done → todo via status helpers."""
    now = datetime.now(timezone.utc)
    t = Task(
        id="t1",
        list_id="l1",
        title="Buy",
        status=TaskStatus.TODO,
        position=0,
        created_by="u1",
        created_at=now,
        updated_at=now,
    )
    assert t.start().status is TaskStatus.IN_PROGRESS
    assert t.complete().status is TaskStatus.DONE
    assert t.complete().reopen().status is TaskStatus.TODO


def test_task_recurrence():
    """mark_spawned sets last_spawned_at on a RecurrenceRule."""
    r = RecurrenceRule(rrule="FREQ=DAILY")
    r2 = r.mark_spawned()
    assert r2.last_spawned_at is not None


def test_task_update_fields():
    """TaskUpdate carries optional fields for a partial task edit."""
    tu = TaskUpdate(title="New", status=TaskStatus.DONE)
    assert tu.title == "New"
    assert tu.status is TaskStatus.DONE


def test_task_with_assignees_returns_new_instance():
    """with_assignees returns a new Task without mutating the original."""
    now = datetime.now(timezone.utc)
    t = Task(
        id="t",
        list_id="l",
        title="T",
        status=TaskStatus.TODO,
        position=0,
        created_by="u",
        created_at=now,
        updated_at=now,
    )
    t2 = t.with_assignees(("a", "b"))
    assert t2.assignees == ("a", "b")
    assert t.assignees == ()


def test_recurrence_mark_spawned_does_not_mutate_original():
    """mark_spawned returns a new rule, leaving the caller unchanged."""
    r = RecurrenceRule(rrule="FREQ=DAILY")
    r2 = r.mark_spawned()
    assert r2.last_spawned_at is not None
    assert r.last_spawned_at is None


# ─── Priority, labels, UNSET ─────────────────────────────────────────────


def _task(**kw) -> Task:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    base = dict(
        id="t1",
        list_id="l1",
        title="Pack",
        status=TaskStatus.TODO,
        position=3,
        created_by="u1",
        created_at=now,
        updated_at=now,
    )
    base.update(kw)
    return Task(**base)


def test_task_priority_and_labels_default_empty():
    t = _task()
    assert t.priority is None
    assert t.labels == ()


def test_task_priority_values():
    assert [p.value for p in TaskPriority] == ["low", "medium", "high", "urgent"]


def test_unset_sentinel_is_distinct_from_none():
    assert UNSET is not None
    assert repr(UNSET) == "UNSET"


def test_normalize_labels_strips_dedupes_case_insensitively():
    assert normalize_labels(["  Home ", "home", "HOME", "Work"]) == ("Home", "Work")


def test_normalize_labels_drops_empty_non_string_and_control_chars():
    assert normalize_labels(["", "   ", 7, None, "a\x00b", "\u202eevil"]) == (
        "ab",
        "evil",
    )


def test_normalize_labels_keeps_emoji_zwj_sequences():
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert normalize_labels([family]) == (family,)


def test_normalize_labels_caps_length_and_count():
    long = "x" * (MAX_TASK_LABEL_LENGTH + 5)
    assert normalize_labels([long]) == ("x" * MAX_TASK_LABEL_LENGTH,)
    many = [f"l{i}" for i in range(MAX_TASK_LABELS + 5)]
    assert len(normalize_labels(many)) == MAX_TASK_LABELS


# ─── Wire codec ──────────────────────────────────────────────────────────


def test_wire_round_trip_carries_every_field():
    t = _task(
        description="Boxes",
        due_date=date(2026, 10, 3),
        assignees=("u1", "u2"),
        recurrence=RecurrenceRule(
            rrule="FREQ=WEEKLY",
            last_spawned_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        ),
        recurrence_parent_id="p0",
        archived_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        priority=TaskPriority.HIGH,
        labels=("Move", "Garage"),
    )
    wire = task_to_wire_dict(t, "sp1")
    assert wire["space_id"] == "sp1"
    assert wire["priority"] == "high"
    assert wire["labels"] == ["Move", "Garage"]
    assert wire["due_date"] == "2026-10-03"
    assert task_from_wire_dict(wire, existing=None) == t


def test_wire_always_emits_priority_key_even_when_none():
    wire = task_to_wire_dict(_task(), "sp1")
    assert "priority" in wire and wire["priority"] is None
    assert wire["labels"] == []


def test_wire_missing_required_field_returns_none():
    wire = task_to_wire_dict(_task(), "sp1")
    del wire["title"]
    assert task_from_wire_dict(wire, existing=None) is None


def test_wire_absent_key_keeps_existing_value():
    existing = _task(
        description="keep",
        due_date=date(2026, 10, 3),
        priority=TaskPriority.LOW,
        labels=("a",),
    )
    wire = {"id": "t1", "list_id": "l1", "title": "Renamed", "priority": "low"}
    got = task_from_wire_dict(wire, existing=existing)
    assert got is not None
    assert got.title == "Renamed"
    assert got.description == "keep"
    assert got.due_date == date(2026, 10, 3)
    assert got.labels == ("a",)
    assert got.position == existing.position


def test_wire_present_null_clears():
    existing = _task(
        description="x",
        due_date=date(2026, 10, 3),
        priority=TaskPriority.URGENT,
        labels=("a",),
    )
    wire = task_to_wire_dict(
        copy.replace(
            existing, description=None, due_date=None, priority=None, labels=()
        ),
        "sp1",
    )
    got = task_from_wire_dict(wire, existing=existing)
    assert got is not None
    assert got.description is None
    assert got.due_date is None
    assert got.priority is None
    assert got.labels == ()


def test_wire_v39_payload_does_not_wipe_held_fields():
    """A payload without ``priority`` comes from a v39 household whose own
    copy lost the due date / archive / recurrence — its nulls must not
    wipe ours, and it can't touch priority or labels."""
    existing = _task(
        due_date=date(2026, 10, 3),
        archived_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        recurrence=RecurrenceRule(rrule="FREQ=DAILY"),
        priority=TaskPriority.HIGH,
        labels=("keep",),
    )
    wire = task_to_wire_dict(
        copy.replace(
            existing,
            title="Edited on v39",
            status=TaskStatus.DONE,
            due_date=None,
            archived_at=None,
            recurrence=None,
        ),
        "sp1",
    )
    del wire["priority"]
    del wire["labels"]
    got = task_from_wire_dict(wire, existing=existing)
    assert got is not None
    assert got.title == "Edited on v39"
    assert got.status is TaskStatus.DONE
    assert got.due_date == date(2026, 10, 3)
    assert got.archived_at == existing.archived_at
    assert got.recurrence == existing.recurrence
    assert got.priority is TaskPriority.HIGH
    assert got.labels == ("keep",)


def test_wire_v39_payload_for_new_row_takes_its_due_date():
    wire = task_to_wire_dict(_task(due_date=date(2026, 10, 3)), "sp1")
    del wire["priority"]
    wire["labels"] = ["ignored"]
    got = task_from_wire_dict(wire, existing=None)
    assert got is not None
    assert got.due_date == date(2026, 10, 3)
    assert got.priority is None
    assert got.labels == ()


def test_wire_unknown_priority_keeps_existing():
    existing = _task(priority=TaskPriority.MEDIUM)
    wire = task_to_wire_dict(existing, "sp1")
    wire["priority"] = "critical"
    got = task_from_wire_dict(wire, existing=existing)
    assert got is not None and got.priority is TaskPriority.MEDIUM


def test_wire_labels_are_normalized_on_the_way_in():
    wire = task_to_wire_dict(_task(), "sp1")
    wire["labels"] = ["A", "a", "", 3, "b" * 40]
    got = task_from_wire_dict(wire, existing=None)
    assert got is not None
    assert got.labels == ("A", "b" * MAX_TASK_LABEL_LENGTH)


def test_wire_tolerates_garbage_scalars():
    wire = {
        "task_id": "t9",
        "list_id": "l1",
        "title": "T",
        "status": "bogus",
        "position": "x",
        "due_date": "not-a-date",
        "archived_at": 12,
        "recurrence": "nope",
        "priority": None,
        "labels": "not-a-list",
        "assignees": ["u1", "u1", ""],
        "occurred_at": "2026-09-01T00:00:00",
    }
    got = task_from_wire_dict(wire, existing=None)
    assert got is not None
    assert got.id == "t9"
    assert got.status is TaskStatus.TODO
    assert got.position == 0
    assert got.due_date is None
    assert got.archived_at is None
    assert got.recurrence is None
    assert got.labels == ()
    assert got.assignees == ("u1",)
    assert got.updated_at == datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_task_list_wire_round_trip():
    lst = TaskList(id="l1", name="Chores", created_by="u1")
    wire = task_list_to_wire_dict(lst, "sp1")
    assert wire == {"id": "l1", "space_id": "sp1", "name": "Chores", "created_by": "u1"}
    assert task_list_from_wire_dict(wire) == lst


def test_task_list_from_wire_needs_id_and_name():
    assert task_list_from_wire_dict({"id": "l1", "name": "  "}) is None
    assert task_list_from_wire_dict({"name": "x"}) is None
    got = task_list_from_wire_dict({"list_id": "l1", "name": " N "})
    assert got == TaskList(id="l1", name="N", created_by="")


# ─── Adversarial-review regressions ──────────────────────────────────────


def test_wire_position_infinity_or_huge_keeps_the_held_value():
    """M3: ``Infinity`` raised OverflowError and ``1e30`` overflowed the
    SQLite int64 column; both now fall back to the held / default value."""
    held = _task(position=4)
    for raw in ('{"position": Infinity}', '{"position": 1e30}', '{"position": NaN}'):
        wire = {"id": "t1", "list_id": "l1", "title": "x", "priority": None}
        wire.update(json.loads(raw))
        got = task_from_wire_dict(wire, existing=held)
        assert got is not None and got.position == 4, raw
        new = task_from_wire_dict(wire, existing=None)
        assert new is not None and new.position == 0, raw


def test_wire_assignees_are_capped_and_strings_only():
    """M5: inbound assignees are capped and non-strings dropped."""
    wire = {
        "id": "t1",
        "list_id": "l1",
        "title": "x",
        "priority": None,
        "assignees": [1, None, {"a": 1}, *[f"u{i}" for i in range(15)]],
    }
    got = task_from_wire_dict(wire, existing=None)
    assert got is not None
    assert got.assignees == tuple(f"u{i}" for i in range(MAX_TASK_ASSIGNEES))


def test_sanitize_line_strips_spoofing_and_rejects_invisible():
    """M4: control, bidi and invisible characters never reach a title."""
    assert sanitize_line("‮evil‎‏؜ x\n y") == "evilx y"
    assert sanitize_line("​‌⁠﻿  ") == ""
    family = "\U0001f468‍\U0001f469"
    assert sanitize_line(family) == family


def test_sanitize_block_keeps_newlines_and_tabs():
    assert sanitize_block("a\n\tb‮\x00 c") == "a\n\tbc"
    assert sanitize_block("​\n ") == ""


def test_labels_reject_zero_width_only_and_strip_marks():
    assert normalize_labels(["a​b", "‎‏x", "؜" * 3 + "y", "​"]) == (
        "a​b",
        "x",
        "y",
    )


def test_wire_title_description_are_sanitised_and_capped():
    wire = {
        "id": "t1",
        "list_id": "l1",
        "title": "‮" + "T" * 500,
        "description": "d" * 9000,
        "priority": None,
    }
    got = task_from_wire_dict(wire, existing=None)
    assert got is not None
    assert got.title == "T" * MAX_TASK_TITLE_LENGTH
    assert got.description is not None
    assert len(got.description) == MAX_TASK_DESCRIPTION_LENGTH


def test_wire_invisible_title_is_refused():
    wire = {"id": "t1", "list_id": "l1", "title": "​‮ ", "priority": None}
    assert task_from_wire_dict(wire, existing=None) is None


def test_list_wire_name_is_sanitised_and_capped():
    got = task_list_from_wire_dict({"id": "l", "name": "‮" + "N" * 100000})
    assert got is not None and got.name == "N" * MAX_TASK_LIST_NAME_LENGTH
    assert task_list_from_wire_dict({"id": "l", "name": "⁦​"}) is None
