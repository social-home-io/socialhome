"""Does released content match the queue item it claims to release? (v_43)

Only the space's HOST releases a queue item: it applies the item from its
own stored copy through the feature's normal persist path, and the
resulting ``SPACE_*`` event carries the approval block
``moderation: {item_id, approved_by}``, attributed to the SUBMITTER. A
household that holds the item itself — always the submitter's own
household, usually the other reviewer households too — checks the release
against its own copy before it lets the content land (defence in depth:
the host is already the roster authority).

The check is **complete over what the receiver applies**:

* a create's event must carry exactly the item's content in every field
  the receiver stores — the fields the apply derives itself (a task's
  position, a timestamp, an event's resolved time zone when the item named
  none) are the only ones not compared, and fields a member's create never
  sets (a task's recurrence or archive stamp, an event's mirror origin, a
  listing's settled status) must be at their creation default;
* an edit's event must carry the item's patch, and every other content
  field the receiver would apply must equal the row this household holds
  (``held``, the handler's snapshot) — nothing outside the patch changes.
  Layout fields (a task's or a sticky's position) are free: a layout move
  never needs review.

Pure functions over plain dicts. :func:`approval_target` answers which row
a content event writes, so the receiver can look for an item about it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from ..domain.federation import FederationEventType
from ..domain.link_preview import link_preview_from_dict, link_preview_to_dict
from ..domain.page_version import is_version_hash, version_hash
from ..domain.space import ContentAction, SpaceModerationItem
from ..utils.datetime import parse_iso8601_optional

FET = FederationEventType

#: The content events an approval block may ride on, by the feature whose
#: queue item they release. A block on any other event is refused.
FEATURE_OF_EVENT: Mapping[FederationEventType, str] = {
    FET.SPACE_POST_CREATED: "posts",
    #: A post's attachments are released with it (one queue item).
    FET.SPACE_SCHEDULE_CREATED: "posts",
    FET.BAZAAR_LISTING_CREATED: "posts",
    FET.SPACE_PAGE_CREATED: "pages",
    FET.SPACE_PAGE_UPDATED: "pages",
    FET.SPACE_PAGE_DELETED: "pages",
    FET.SPACE_TASK_CREATED: "tasks",
    FET.SPACE_TASK_UPDATED: "tasks",
    FET.SPACE_TASK_DELETED: "tasks",
    FET.SPACE_TASK_LIST_CREATED: "tasks",
    FET.SPACE_TASK_LIST_UPDATED: "tasks",
    FET.SPACE_TASK_LIST_DELETED: "tasks",
    FET.SPACE_STICKY_CREATED: "stickies",
    FET.SPACE_STICKY_UPDATED: "stickies",
    FET.SPACE_STICKY_DELETED: "stickies",
    FET.SPACE_CALENDAR_EVENT_CREATED: "calendar",
    FET.SPACE_CALENDAR_EVENT_UPDATED: "calendar",
    FET.SPACE_CALENDAR_EVENT_DELETED: "calendar",
}

_ROUTING = frozenset({"space_id", "actor_user_id", "moderation"})
_STAMPS = frozenset({"created_at", "updated_at", "occurred_at"})
_PAGE_SEQUENCING = frozenset(
    {
        "id",
        "page_id",
        "seq",
        "version_hash",
        "conflict",
        "sequenced",
        "last_editor_user_id",
        "cover_image_url",
        "updated_at",
        "base_seq",
        "base_hash",
        "replay",
    }
)

#: Per event type, the keys a release may carry WITHOUT comparison: ids and
#: routing, timestamps the receiver derives, layout. Everything else on the
#: event must be content a rule compares (:func:`item_matches_event`).
FREE_KEYS: Mapping[FederationEventType, frozenset[str]] = {
    FET.SPACE_POST_CREATED: _ROUTING
    | frozenset({"id", "post_id", "occurred_at", "public_relay"}),
    FET.SPACE_SCHEDULE_CREATED: _ROUTING | frozenset({"post_id"}),
    FET.BAZAAR_LISTING_CREATED: _ROUTING | frozenset({"post_id", "created_at"}),
    # v_48 host-sequencing bookkeeping (the host's ``seq``, the version's
    # hash, its conflict list, which proposal it answers, who edited it
    # last) rides freely. ``cover_image_url`` is free too — but the rules
    # still compare it whenever the wire carries it.
    FET.SPACE_PAGE_CREATED: _ROUTING | _STAMPS | _PAGE_SEQUENCING,
    FET.SPACE_PAGE_UPDATED: _ROUTING | _STAMPS | _PAGE_SEQUENCING,
    FET.SPACE_PAGE_DELETED: _ROUTING | frozenset({"id", "page_id"}),
    FET.SPACE_TASK_CREATED: _ROUTING
    | _STAMPS
    | frozenset({"id", "task_id", "position"}),
    FET.SPACE_TASK_UPDATED: _ROUTING
    | _STAMPS
    | frozenset({"id", "task_id", "position"}),
    FET.SPACE_TASK_DELETED: _ROUTING | frozenset({"id", "task_id", "list_id"}),
    FET.SPACE_TASK_LIST_CREATED: _ROUTING | frozenset({"id", "list_id"}),
    FET.SPACE_TASK_LIST_UPDATED: _ROUTING | frozenset({"id", "list_id"}),
    FET.SPACE_TASK_LIST_DELETED: _ROUTING | frozenset({"id", "list_id"}),
    FET.SPACE_STICKY_CREATED: _ROUTING | _STAMPS | frozenset({"id", "sticky_id"}),
    FET.SPACE_STICKY_UPDATED: _ROUTING
    | _STAMPS
    | frozenset({"id", "sticky_id", "position_x", "position_y"}),
    FET.SPACE_STICKY_DELETED: _ROUTING | frozenset({"id", "sticky_id"}),
    FET.SPACE_CALENDAR_EVENT_CREATED: _ROUTING | frozenset({"id", "event_id"}),
    FET.SPACE_CALENDAR_EVENT_UPDATED: _ROUTING | frozenset({"id", "event_id"}),
    FET.SPACE_CALENDAR_EVENT_DELETED: _ROUTING | frozenset({"id", "event_id"}),
}

#: Events whose row id is the wrapper post's (``post_id``).
_POST_ID_EVENTS = frozenset({FET.SPACE_SCHEDULE_CREATED, FET.BAZAAR_LISTING_CREATED})


def approval_target(event_type: FederationEventType, payload: Mapping) -> str:
    """The id of the row a content event writes ("" when it names none)."""
    if event_type in _POST_ID_EVENTS:
        keys: tuple[str, ...] = ("post_id",)
    elif event_type in (
        FET.SPACE_CALENDAR_EVENT_CREATED,
        FET.SPACE_CALENDAR_EVENT_UPDATED,
        FET.SPACE_CALENDAR_EVENT_DELETED,
    ):
        keys = ("event_id", "id")
    else:
        keys = ("id", "post_id", "page_id", "task_id", "list_id", "sticky_id")
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def needs_held_row(item: SpaceModerationItem) -> bool:
    """Does checking a release of ``item`` need the receiver's own copy of
    the target (every edit, and a task archive / unarchive)?"""
    return item.action == ContentAction.EDIT.value or (
        item.action == ContentAction.DELETE.value
        and (item.payload or {}).get("op") in ("archive", "unarchive")
    )


def item_matches_event(
    item: SpaceModerationItem,
    event_type: FederationEventType,
    payload: Mapping,
    *,
    held: Mapping | None = None,
) -> bool:
    """Is ``payload`` (a ``event_type`` content event) exactly the release of
    ``item`` — same feature, same target, the item's kind of write, the
    item's content, and (an edit) nothing else changed against ``held``?
    A purged item matches nothing; an edit without ``held`` matches nothing."""
    p = item.payload or {}
    target = p.get("target_id")
    if not isinstance(target, str) or not target:
        return False
    if FEATURE_OF_EVENT.get(event_type) != item.feature:
        return False
    if approval_target(event_type, payload) != target:
        return False
    if needs_held_row(item) and held is None:
        return False
    rule = _rule(item, event_type)
    if rule is None:
        return False
    try:
        expected, actual = rule(
            {**p, "_space_id": item.space_id}, item.submitted_by, payload, held or {}
        )
    except KeyError, TypeError, ValueError, AttributeError:
        return False
    # Fail closed: every key the event carries is either routing /
    # bookkeeping this event type may carry freely, or content the rule
    # compared. A key nobody classified (a new wire field, an alias the
    # receiver also reads) refuses the release until it is.
    if set(payload) - FREE_KEYS.get(event_type, frozenset()) - set(actual):
        return False
    return _digest(expected) == _digest(actual)


# ── Rules ────────────────────────────────────────────────────────────────

#: ``(queue payload, submitter, wire payload, held row) -> (expected, actual)``.
_Rule = Callable[[Mapping, str, Mapping, Mapping], tuple[dict, dict]]


def _rule(item: SpaceModerationItem, event_type: FederationEventType) -> _Rule | None:
    """The comparison for ``item`` released as ``event_type`` — ``None``
    when that event is not this item's write."""
    p = item.payload or {}
    entity = p.get("entity")
    op = p.get("op")
    match item.feature, item.action, entity, event_type:
        case "posts", "create", "post", FET.SPACE_POST_CREATED:
            return _post_create
        case "posts", "create", "post", FET.SPACE_SCHEDULE_CREATED:
            return _schedule_create
        case "posts", "create", "post", FET.BAZAAR_LISTING_CREATED:
            return _listing_create
        case "pages", "create", "page", FET.SPACE_PAGE_CREATED:
            return _page_create
        case "pages", "edit", "page", FET.SPACE_PAGE_UPDATED:
            return _page_resolution if op == "resolve_conflict" else _page_edit
        case "pages", "delete", "page", FET.SPACE_PAGE_DELETED:
            return _nothing
        case "tasks", "create", "task", FET.SPACE_TASK_CREATED:
            return _task_create
        case "tasks", "create", "list", FET.SPACE_TASK_LIST_CREATED:
            return _list_create
        case "tasks", "edit", "task", FET.SPACE_TASK_UPDATED:
            return _task_edit
        case "tasks", "edit", "list", FET.SPACE_TASK_LIST_UPDATED:
            return _list_edit
        case "tasks", "delete", "list", FET.SPACE_TASK_LIST_DELETED:
            return _nothing
        case "tasks", "delete", "task", FET.SPACE_TASK_DELETED:
            return _nothing if op in (None, "delete") else None
        case "tasks", "delete", "task", FET.SPACE_TASK_UPDATED:
            return _task_archive if op in ("archive", "unarchive") else None
        case "stickies", "create", "sticky", FET.SPACE_STICKY_CREATED:
            return _sticky_create
        case "stickies", "edit", "sticky", FET.SPACE_STICKY_UPDATED:
            return _sticky_edit
        case "stickies", "delete", "sticky", FET.SPACE_STICKY_DELETED:
            return _nothing
        case "calendar", "create", "event", FET.SPACE_CALENDAR_EVENT_CREATED:
            return _event_create
        case "calendar", "edit", "event", FET.SPACE_CALENDAR_EVENT_UPDATED:
            return _event_edit
        case "calendar", "delete", "event", FET.SPACE_CALENDAR_EVENT_DELETED:
            return _nothing
    return None


def _nothing(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    """A removal proposes no content: the target and the kind are all."""
    return {}, {}


def _patch(p: Mapping) -> Mapping:
    patch = p.get("patch")
    if not isinstance(patch, Mapping) or not patch:
        raise ValueError("an edit item without a patch")
    return patch


def _guarded_edit(
    keys: tuple[str, ...],
    norm: Callable[[str, Any], Any],
    p: Mapping,
    w: Mapping,
    h: Mapping,
    *,
    proposed: Mapping | None = None,
) -> tuple[dict, dict]:
    """Every ``key`` the wire carries: the patch's value where the item
    edits it, else the held row's — and the wire must say exactly that."""
    patch = _patch(p) if proposed is None else proposed
    expected: dict[str, Any] = {}
    actual: dict[str, Any] = {}
    for key in keys:
        if key not in w and key not in patch:
            continue
        if key in patch:
            expected[key] = norm(key, patch[key])
        else:
            if key not in h:
                raise KeyError(key)
            expected[key] = norm(key, h[key])
        actual[key] = norm(key, w.get(key))
    return expected, actual


# Posts.


def _location(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    return {
        "lat": float(raw["lat"]),
        "lon": float(raw["lon"]),
        "label": raw.get("label"),
    }


def _file_meta(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    return {
        "url": str(raw.get("url") or ""),
        "mime_type": str(raw.get("mime_type") or ""),
        "original_name": str(raw.get("original_name") or ""),
        "size_bytes": int(raw.get("size_bytes") or 0),
    }


def _same_ref(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _link_preview(raw: object) -> dict[str, Any] | None:
    # Both sides through the same parser; the image ref is compared as is.
    return link_preview_to_dict(link_preview_from_dict(raw, image_ref=_same_ref))


def _post_fields(src: Mapping) -> dict[str, Any]:
    return {
        "type": str(src.get("type") or "text"),
        "content": src.get("content"),
        "media_url": src.get("media_url") or None,
        "image_urls": [str(u) for u in (src.get("image_urls") or ())],
        "file_meta": _file_meta(src.get("file_meta")),
        "location": _location(src.get("location")),
        "linked_event_id": src.get("linked_event_id") or None,
        "hidden_from_feed": bool(src.get("hidden_from_feed", False)),
        "link_preview": _link_preview(src.get("link_preview")),
    }


def _post_create(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    return {**_post_fields(p), "author": by}, {
        **_post_fields(w),
        "author": w.get("author"),
    }


def _attachment(p: Mapping, kind: str) -> Mapping:
    attachments = p.get("attachments")
    value = attachments.get(kind) if isinstance(attachments, Mapping) else None
    if not isinstance(value, Mapping):
        raise ValueError(f"the item has no {kind}")
    return value


def _slots(raw: object) -> list[tuple[str, str | None, str | None]]:
    if not isinstance(raw, list):
        raise ValueError("slots must be a list")
    return [
        (str(s["slot_date"]), s.get("start_time") or None, s.get("end_time") or None)
        for s in raw
    ]


def _schedule_fields(src: Mapping) -> dict[str, Any]:
    return {
        "title": src.get("title"),
        "deadline": src.get("deadline") or None,
        "slots": _slots(src.get("slots")),
    }


def _schedule_create(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    return _schedule_fields(_attachment(p, "schedule")), _schedule_fields(w)


_LISTING_KEYS = (
    "mode",
    "title",
    "currency",
    "description",
    "price",
    "start_price",
    "step_price",
)


def _listing_fields(src: Mapping) -> dict[str, Any]:
    out: dict[str, Any] = {k: src.get(k) for k in _LISTING_KEYS}
    out["mode"] = str(out["mode"] or "")
    out["title"] = str(out["title"] or "")
    out["image_urls"] = [str(u) for u in (src.get("image_urls") or ())]
    return out


def _listing_create(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    fields = _attachment(p, "bazaar")
    expected = {
        **_listing_fields(fields),
        "seller_user_id": by,
        "status": "active",
        "end_time": True,
    }
    actual = {
        **_listing_fields(w),
        "seller_user_id": w.get("seller_user_id"),
        "status": str(w.get("status") or "active"),
        # Set by the apply (approval + duration): within the item's run.
        "end_time": _ends_within(w.get("end_time"), fields.get("duration_days")),
    }
    return expected, actual


def _ends_within(raw: object, duration_days: object) -> bool:
    end = parse_iso8601_optional(raw) if isinstance(raw, str) and raw else None
    if end is None:
        return False
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    try:
        days = max(1, int(duration_days) if isinstance(duration_days, int | str) else 7)
    except TypeError, ValueError:
        return False
    return end <= datetime.now(timezone.utc) + timedelta(days=days, hours=1)


# Pages.


def _page_create(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    expected = {
        "title": p.get("title"),
        "content": p.get("content") or "",
        "created_by": by,
    }
    actual = {
        "title": w.get("title"),
        "content": w.get("content") or "",
        "created_by": w.get("created_by"),
    }
    if "cover_image_url" in w:
        expected["cover_image_url"] = p.get("cover_image_url")
        actual["cover_image_url"] = w.get("cover_image_url")
    return expected, actual


def _plain(key: str, value: Any) -> Any:
    return value


def _page_edit(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    return _guarded_edit(
        ("title", "content", "cover_image_url", "created_by"), _plain, p, w, h
    )


def _page_resolution(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    """A conflict resolution. ``side`` (v_48) keeps one published version:
    the wire's title + content + cover must hash to exactly the item's
    ``side``. Merged text is the item's, under the held title and cover;
    the two-way "mine" / "theirs" pick one of two versions already
    published (the content is not compared)."""
    expected: dict[str, Any] = {}
    actual: dict[str, Any] = {}
    if "created_by" in w:
        expected["created_by"] = h["created_by"]
        actual["created_by"] = w.get("created_by")
    if p.get("resolution") == "side":
        side = p.get("side")
        if not is_version_hash(side):
            raise ValueError("a side resolution without the kept version")
        expected["version"] = side
        actual["version"] = version_hash(
            str(w.get("title") or ""),
            str(w.get("content") or ""),
            w.get("cover_image_url")
            if "cover_image_url" in w
            else h.get("cover_image_url"),
        )
        # All three are compared through the version; mark them classified.
        for key in ("title", "content", "cover_image_url"):
            if key in w:
                actual[key] = expected[key] = w.get(key)
        return expected, actual
    if "cover_image_url" in w:
        expected["cover_image_url"] = h["cover_image_url"]
        actual["cover_image_url"] = w.get("cover_image_url")
    expected["title"] = h["title"]
    actual["title"] = w.get("title")
    if p.get("resolution") == "merged_content":
        expected["content"] = p.get("merged_content")
    else:
        # One of the two versions already published (not compared).
        expected["content"] = w.get("content")
    actual["content"] = w.get("content")
    return expected, actual


# Tasks.

_TASK_CONTENT_KEYS = (
    "list_id",
    "title",
    "description",
    "status",
    "due_date",
    "assignees",
    "priority",
    "labels",
)


def _task_value(key: str, value: Any) -> Any:
    if key in ("assignees", "labels"):
        return [str(v) for v in (value or ())]
    if key == "due_date":
        when = parse_iso8601_optional(value) if value else None
        return when.date().isoformat() if when is not None else None
    return value


def _rrule(value: object) -> str | None:
    """A task recurrence as its rule (``last_spawned_at`` is bookkeeping)."""
    if isinstance(value, Mapping):
        rule = value.get("rrule")
        return str(rule) if rule else None
    # Anything else that is set is not a recurrence we recognise: it must
    # not compare equal to "none".
    return None if value is None else f"invalid:{value!r}"


def _task_fixed(w: Mapping) -> dict[str, Any]:
    """The wire's non-patchable task fields, normalised."""
    return {
        "created_by": w.get("created_by"),
        "recurrence": _rrule(w.get("recurrence")),
        "recurrence_parent_id": w.get("recurrence_parent_id") or None,
        "archived_at": bool(w.get("archived_at")),
    }


def _task_held_fixed(h: Mapping, *, archived: bool | None = None) -> dict[str, Any]:
    return {
        "created_by": h["created_by"],
        "recurrence": _rrule(h.get("recurrence")),
        "recurrence_parent_id": h.get("recurrence_parent_id") or None,
        "archived_at": bool(h.get("archived")) if archived is None else archived,
    }


def _task_create(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    expected = {k: _task_value(k, p.get(k)) for k in _TASK_CONTENT_KEYS}
    # The apply defaults an absent status to the board's first column.
    expected["status"] = expected["status"] or "todo"
    expected.update(
        created_by=by, recurrence=None, recurrence_parent_id=None, archived_at=False
    )
    actual = {k: _task_value(k, w.get(k)) for k in _TASK_CONTENT_KEYS}
    actual.update(_task_fixed(w))
    return expected, actual


def _task_edit(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    expected, actual = _guarded_edit(_TASK_CONTENT_KEYS, _task_value, p, w, h)
    # Never another owner, recurrence or archive state by an edit.
    expected.update(_task_held_fixed(h))
    actual.update(_task_fixed(w))
    return expected, actual


def _task_archive(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    expected = {k: _task_value(k, h.get(k)) for k in _TASK_CONTENT_KEYS}
    expected.update(_task_held_fixed(h, archived=p.get("op") == "archive"))
    actual = {k: _task_value(k, w.get(k)) for k in _TASK_CONTENT_KEYS}
    actual.update(_task_fixed(w))
    return expected, actual


def _list_create(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    return (
        {"name": p.get("name"), "created_by": by},
        {"name": w.get("name"), "created_by": w.get("created_by")},
    )


def _list_edit(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    expected = {"name": _patch(p).get("name")}
    actual = {"name": w.get("name")}
    if "created_by" in w:
        expected["created_by"] = h["created_by"]
        actual["created_by"] = w.get("created_by")
    return expected, actual


# Stickies — the position is layout, free.

_STICKY_CONTENT_KEYS = ("content", "color")


def _sticky_create(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    keys = (*_STICKY_CONTENT_KEYS, "position_x", "position_y")
    return (
        {**{k: p.get(k) for k in keys}, "author": by},
        {**{k: w.get(k) for k in keys}, "author": w.get("author")},
    )


def _sticky_edit(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    return _guarded_edit((*_STICKY_CONTENT_KEYS, "author"), _plain, p, w, h)


# Calendar events.

_EVENT_CONTENT_KEYS = (
    "summary",
    "start",
    "end",
    "description",
    "all_day",
    "attendees",
    "rrule",
    "cover_url",
    "location",
)


def _event_value(key: str, value: Any) -> Any:
    if key in ("start", "end"):
        when = parse_iso8601_optional(value) if value else None
        return when.isoformat() if when is not None else None
    if key == "attendees":
        return [str(a) for a in (value or ())]
    if key == "all_day":
        return bool(value)
    return value


def _event_create(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    space_id = str(p.get("_space_id") or "")
    expected = {k: _event_value(k, p.get(k)) for k in _EVENT_CONTENT_KEYS}
    actual = {k: _event_value(k, w.get(k)) for k in _EVENT_CONTENT_KEYS}
    expected.update(created_by=by, mirrored_from=None, calendar_id=space_id)
    actual.update(
        created_by=w.get("created_by"),
        mirrored_from=w.get("mirrored_from") or None,
        calendar_id=w.get("calendar_id"),
    )
    # The item's own zone, when it named one; else the apply resolved it
    # (any value the receiver validates).
    expected["tz"] = p.get("tz") or w.get("tz")
    actual["tz"] = w.get("tz")
    # An announcement can be dropped by the apply, never added.
    actual["announce_in_feed"] = bool(w.get("announce_in_feed"))
    expected["announce_in_feed"] = bool(w.get("announce_in_feed")) and bool(
        p.get("announce_in_feed")
    )
    return expected, actual


def _event_edit(p: Mapping, by: str, w: Mapping, h: Mapping) -> tuple[dict, dict]:
    patch = dict(_patch(p))
    patch.pop("capacity", None)
    patch.pop("clear_capacity", None)
    if not patch:
        patch = {"__capacity_only__": True}
    expected, actual = _guarded_edit(
        (*_EVENT_CONTENT_KEYS, "tz"), _event_value, p, w, h, proposed=patch
    )
    expected.pop("__capacity_only__", None)
    for key in ("created_by", "calendar_id", "mirrored_from", "announce_in_feed"):
        if key in w or key == "created_by":
            expected[key] = h.get(key)
            actual[key] = w.get(key)
    return expected, actual


def _digest(projection: Mapping[str, Any]) -> str:
    """A stable digest of a projection (sorted keys, canonical JSON)."""
    blob = json.dumps(projection, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
