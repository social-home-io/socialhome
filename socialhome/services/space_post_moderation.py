"""Space posts in the moderation queue (§4.3 ``MODERATED`` posts).

A queued post must come back out exactly as it went in, attachments and
all, so this module owns the round trip:

* :func:`post_to_queue_payload` / :func:`post_from_queue_payload` — every
  :class:`~socialhome.domain.post.Post` field a create carries, plus the
  pre-minted owner-bound id (so approval is idempotent and the post stays
  the submitter's). ``tests/services/test_space_post_moderation.py`` walks
  ``dataclasses.fields(Post)``: a new field fails until it is classified
  here.
* :class:`SpacePostAttachments` — what rides WITH a post and must be
  created atomically with it: a reply poll, a schedule poll, a Bazaar
  listing. A direct post creates them right after the post; a queued post
  carries them in its payload and creates them on approval — so a pending
  listing never exists without its post, nor a poll without its question
  reviewed.
* :class:`PostModerationHandler` — the ``posts`` create handler of the
  moderation registry.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from ..domain.link_preview import link_preview_to_dict
from ..domain.post import FileMeta, LocationData, Post, PostType
from ..domain.space import Space, SpaceModerationItem
from .link_preview_service import wire_link_preview
from .space_moderation_service import ApplyResult

if TYPE_CHECKING:
    from .poll_service import PollService
    from .space_service import SpaceService

log = logging.getLogger(__name__)

#: Post fields a queued create carries in its payload.
QUEUED_POST_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "type",
        "content",
        "media_url",
        "image_urls",
        "file_meta",
        "location",
        "linked_event_id",
        "linked_highlight_id",
        "hidden_from_feed",
        "no_link_preview",
        "link_preview",
    }
)
#: Post fields set from the queue item itself, not the payload.
ITEM_POST_FIELDS: frozenset[str] = frozenset(
    {
        "author",  # = the item's submitter
        "created_at",  # = the moment of approval: the post appears then
    }
)
#: Post fields that ride as attachments (created with the post).
ATTACHMENT_POST_FIELDS: frozenset[str] = frozenset({"poll", "schedule"})
#: Post fields a brand-new member post never has.
NOT_AT_CREATE_POST_FIELDS: frozenset[str] = frozenset(
    {
        "reactions",
        "comment_count",
        "pinned",
        "deleted",
        "edited_at",
        "moderated",
        "bot_id",
    }
)

#: Attachment caps (the poll / schedule builders' own limits).
MAX_POLL_OPTIONS = 20
MAX_POLL_TEXT = 500
MAX_SCHEDULE_SLOTS = 50


def _file_meta_dict(fm: FileMeta | None) -> dict | None:
    if fm is None:
        return None
    return {
        "url": fm.url,
        "mime_type": fm.mime_type,
        "original_name": fm.original_name,
        "size_bytes": fm.size_bytes,
    }


def _file_meta_from(raw: object) -> FileMeta | None:
    if not isinstance(raw, dict):
        return None
    try:
        return FileMeta(
            url=str(raw.get("url", "")),
            mime_type=str(raw.get("mime_type", "")),
            original_name=str(raw.get("original_name", "")),
            size_bytes=int(raw.get("size_bytes", 0)),
        )
    except TypeError, ValueError:
        return None


def _location_from(raw: object) -> LocationData | None:
    if not isinstance(raw, dict):
        return None
    try:
        return LocationData(
            lat=float(raw["lat"]),
            lon=float(raw["lon"]),
            label=raw.get("label"),
        )
    except KeyError, TypeError, ValueError:
        return None


def post_to_queue_payload(post: Post, attachments: dict | None = None) -> dict:
    """The queue payload of a post create (see the module docstring)."""
    return {
        "entity": "post",
        "target_id": post.id,
        "post_id": post.id,
        "type": post.type.value,
        "content": post.content,
        "media_url": post.media_url,
        "image_urls": list(post.image_urls),
        "file_meta": _file_meta_dict(post.file_meta),
        "location": (
            {
                "lat": post.location.lat,
                "lon": post.location.lon,
                "label": post.location.label,
            }
            if post.location is not None
            else None
        ),
        "linked_event_id": post.linked_event_id,
        "linked_highlight_id": post.linked_highlight_id,
        "hidden_from_feed": bool(post.hidden_from_feed),
        "no_link_preview": bool(post.no_link_preview),
        "link_preview": link_preview_to_dict(post.link_preview),
        "attachments": dict(attachments or {}),
    }


def post_from_queue_payload(
    item: SpaceModerationItem, *, created_at: datetime | None = None
) -> Post:
    """Rebuild the :class:`Post` a queue item describes, authored by its
    submitter, created ``created_at`` (default: now — the post appears in
    the feed when it is approved)."""
    p = item.payload
    post_id = p.get("post_id") or p.get("target_id")
    if not isinstance(post_id, str) or not post_id:
        raise ValueError("queued post has no id")
    return Post(
        id=post_id,
        author=item.submitted_by,
        type=PostType(str(p.get("type") or "text")),
        created_at=created_at or datetime.now(timezone.utc),
        content=p.get("content"),
        media_url=p.get("media_url"),
        image_urls=tuple(str(u) for u in (p.get("image_urls") or ())),
        file_meta=_file_meta_from(p.get("file_meta")),
        location=_location_from(p.get("location")),
        linked_event_id=p.get("linked_event_id"),
        linked_highlight_id=p.get("linked_highlight_id"),
        hidden_from_feed=bool(p.get("hidden_from_feed", False)),
        no_link_preview=bool(p.get("no_link_preview", False)),
        link_preview=wire_link_preview(p.get("link_preview")),
    )


# ── Attachments ──────────────────────────────────────────────────────────


class _ListingCreator(Protocol):
    async def create_listing_once(
        self, *, space_id: str, post: Post, fields: dict
    ) -> bool: ...


def _text(value: object, what: str, *, limit: int = MAX_POLL_TEXT) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{what} must not be empty")
    if len(text) > limit:
        raise ValueError(f"{what} is too long")
    return text


class SpacePostAttachments:
    """Validates and creates the things that ride with a space post."""

    __slots__ = ("_polls", "_bazaar")

    def __init__(
        self,
        *,
        poll_service: "PollService | None" = None,
        bazaar_service: _ListingCreator | None = None,
    ) -> None:
        self._polls = poll_service
        self._bazaar = bazaar_service

    def attach_bazaar(self, bazaar_service: _ListingCreator) -> None:
        self._bazaar = bazaar_service

    def validate(self, post_type: PostType, attachments: dict | None) -> dict:
        """Normalise ``attachments`` for a ``post_type`` post, or raise
        :class:`ValueError` (422). Each kind belongs to its own post type."""
        if not attachments:
            return {}
        if not isinstance(attachments, dict):
            raise ValueError("attachments must be an object")
        out: dict[str, Any] = {}
        poll = attachments.get("poll")
        if poll is not None:
            if post_type is not PostType.POLL or not isinstance(poll, dict):
                raise ValueError("a poll rides only on a poll post")
            options = [
                _text(o, "poll option") for o in (poll.get("options") or []) if o
            ]
            if not 2 <= len(options) <= MAX_POLL_OPTIONS:
                raise ValueError("a poll needs between 2 and 20 options")
            closes_at = poll.get("closes_at")
            out["poll"] = {
                "question": _text(poll.get("question"), "poll question"),
                "options": options,
                "allow_multiple": bool(poll.get("allow_multiple", False)),
                "closes_at": str(closes_at) if closes_at else None,
            }
        schedule = attachments.get("schedule")
        if schedule is not None:
            if post_type is not PostType.SCHEDULE or not isinstance(schedule, dict):
                raise ValueError("a schedule poll rides only on a schedule post")
            raw_slots = schedule.get("slots") or []
            if not isinstance(raw_slots, list) or not (
                1 <= len(raw_slots) <= MAX_SCHEDULE_SLOTS
            ):
                raise ValueError("a schedule poll needs between 1 and 50 slots")
            slots = []
            for i, slot in enumerate(raw_slots):
                if not isinstance(slot, dict):
                    raise ValueError("each slot must be an object")
                slots.append(
                    {
                        "slot_date": _text(
                            slot.get("slot_date"), "slot_date", limit=40
                        ),
                        "start_time": slot.get("start_time") or None,
                        "end_time": slot.get("end_time") or None,
                        "position": i,
                    }
                )
            deadline = schedule.get("deadline")
            out["schedule"] = {
                "title": _text(schedule.get("title"), "schedule title"),
                "slots": slots,
                "deadline": str(deadline) if deadline else None,
            }
        bazaar = attachments.get("bazaar")
        if bazaar is not None:
            if post_type is not PostType.BAZAAR or not isinstance(bazaar, dict):
                raise ValueError("a listing rides only on a bazaar post")
            # Validated by BazaarService before it reached the post path.
            out["bazaar"] = dict(bazaar)
        return out

    async def apply(self, space_id: str, post: Post, attachments: dict) -> bool:
        """Create the attachments of the just-persisted ``post``.

        Create-once per post, atomically in each repo (the poll and its
        options, the schedule and its slots, the listing): a repeat — an
        approval resumed after a failure part-way, even two racing — creates
        only what is still missing, never a duplicate. True when this call
        created anything."""
        created = False
        poll = attachments.get("poll")
        schedule = attachments.get("schedule")
        if (poll or schedule) and self._polls is None:
            raise RuntimeError("space poll service not attached")
        if poll and self._polls is not None:
            created |= await self._polls.create_poll_once(
                post_id=post.id,
                question=poll["question"],
                options=list(poll["options"]),
                allow_multiple=bool(poll.get("allow_multiple")),
                closes_at=poll.get("closes_at"),
                space_id=space_id,
            )
        if schedule and self._polls is not None:
            created |= await self._polls.create_schedule_poll_once(
                post_id=post.id,
                title=schedule["title"],
                deadline=schedule.get("deadline"),
                slots=list(schedule["slots"]),
                space_id=space_id,
            )
        bazaar = attachments.get("bazaar")
        if bazaar:
            if self._bazaar is None:
                raise RuntimeError("bazaar service not attached")
            created |= await self._bazaar.create_listing_once(
                space_id=space_id, post=post, fields=dict(bazaar)
            )
        return created


# ── Moderation handler ───────────────────────────────────────────────────


_PREVIEW_FIELDS = (
    "type",
    "content",
    "media_url",
    "image_urls",
    "file_meta",
    "location",
    "link_preview",
    "linked_highlight_id",
    "linked_event_id",
    "hidden_from_feed",
)


class PostModerationHandler:
    """The ``posts`` create handler: a queued post (with its poll /
    schedule / listing) is published on approval through
    :meth:`SpaceService.publish_approved_post` — the same persist + bus
    path as a direct post, so it federates and mentions exactly once."""

    __slots__ = ("_svc",)

    def __init__(self, service: "SpaceService") -> None:
        self._svc = service

    def validate(self, space: Space, payload: dict) -> dict:
        if payload.get("entity") != "post":
            raise ValueError("not a post submission")
        post_type = PostType(str(payload.get("type") or "text"))
        if not isinstance(payload.get("target_id"), str):
            raise ValueError("queued post has no id")
        out = dict(payload)
        out["post_id"] = payload.get("post_id") or payload["target_id"]
        out["attachments"] = self._svc.post_attachments().validate(
            post_type, payload.get("attachments")
        )
        return out

    async def snapshot(self, space_id: str, target_id: str) -> dict | None:
        post = await self._svc.get_space_post(space_id, target_id)
        if post is None:
            return None
        return {"type": post.type.value, "content": post.content}

    async def apply(
        self, item: SpaceModerationItem, *, approved_by: str, force: bool
    ) -> ApplyResult:
        post = post_from_queue_payload(item)
        if await self._svc.get_space_post(item.space_id, post.id) is None:
            relay = item.payload.get("public_relay")
            await self._svc.publish_approved_post(
                item.space_id,
                post,
                approved_by=approved_by,
                attachments=dict(item.payload.get("attachments") or {}),
                public_relay=relay if isinstance(relay, dict) else None,
            )
        return ApplyResult(target_id=post.id, post_id=post.id)

    async def resume(self, item: SpaceModerationItem, *, approved_by: str) -> bool:
        """Finish an approved post whose attachment failed after the post
        was published: create the missing poll / schedule / listing. False
        when there is nothing to finish."""
        attachments = dict(item.payload.get("attachments") or {})
        if not attachments:
            return False
        post = await self._svc.get_space_post(
            item.space_id, str(item.payload.get("post_id") or item.payload["target_id"])
        )
        if post is None or post.deleted:
            return False
        return await self._svc.post_attachments().apply(
            item.space_id, post, attachments
        )

    def preview(self, item: SpaceModerationItem) -> dict:
        p = item.payload
        out = {k: p.get(k) for k in _PREVIEW_FIELDS}
        for kind, value in (p.get("attachments") or {}).items():
            out[kind] = value
        return out
