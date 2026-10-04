"""Tests for socialhome.services.space_post_moderation (queued posts)."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from socialhome.domain.link_preview import LinkPreview
from socialhome.domain.post import FileMeta, LocationData, Post, PostType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceModerationItem,
    SpaceType,
)
from socialhome.services.space_post_moderation import (
    ATTACHMENT_POST_FIELDS,
    ITEM_POST_FIELDS,
    NOT_AT_CREATE_POST_FIELDS,
    QUEUED_POST_FIELDS,
    PostModerationHandler,
    SpacePostAttachments,
    post_from_queue_payload,
    post_to_queue_payload,
)

_NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _space() -> Space:
    return Space(
        id="sp",
        name="S",
        owner_instance_id="inst",
        owner_username="o",
        identity_public_key="aa" * 32,
        config_sequence=1,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )


def _item(payload: dict) -> SpaceModerationItem:
    return SpaceModerationItem(
        id="i1",
        space_id="sp",
        feature="posts",
        action="create",
        submitted_by="u-mem",
        payload=payload,
        current_snapshot=None,
        submitted_at=_NOW,
        expires_at=_NOW,
    )


def test_every_post_field_is_classified():
    """A new Post field must be classified here before it can ship: either
    the queue carries it, it comes from the item, it rides as an
    attachment, or a brand-new post never has it."""
    classified = (
        QUEUED_POST_FIELDS
        | ITEM_POST_FIELDS
        | ATTACHMENT_POST_FIELDS
        | NOT_AT_CREATE_POST_FIELDS
    )
    names = {f.name for f in dataclasses.fields(Post)}
    assert names - classified == set(), "unclassified Post field(s)"
    assert classified - names == set(), "classified field no longer on Post"
    groups = (
        QUEUED_POST_FIELDS,
        ITEM_POST_FIELDS,
        ATTACHMENT_POST_FIELDS,
        NOT_AT_CREATE_POST_FIELDS,
    )
    assert sum(len(g) for g in groups) == len(classified), "a field in two groups"


def test_queue_payload_round_trips_every_carried_field():
    post = Post(
        id="p-1",
        author="u-mem",
        type=PostType.IMAGE,
        created_at=_NOW,
        content="hello",
        media_url="/api/media/v.mp4",
        image_urls=("/api/media/a.webp", "/api/media/b.webp"),
        file_meta=FileMeta(
            url="/api/media/f", mime_type="text/plain", original_name="f", size_bytes=3
        ),
        location=LocationData(lat=1.2345, lon=2.3456, label="here"),
        linked_event_id="ev-1",
        linked_highlight_id="hl-1",
        hidden_from_feed=True,
        no_link_preview=True,
        link_preview=LinkPreview(url="https://example.com/", title="Card"),
    )
    payload = post_to_queue_payload(post, {"poll": {"question": "Q"}})
    assert payload["attachments"] == {"poll": {"question": "Q"}}
    back = post_from_queue_payload(_item(payload), created_at=_NOW)
    for name in QUEUED_POST_FIELDS | ITEM_POST_FIELDS:
        assert getattr(back, name) == getattr(post, name), name


def test_from_payload_without_an_id_is_refused():
    with pytest.raises(ValueError):
        post_from_queue_payload(_item({"type": "text"}))


def test_from_payload_tolerates_corrupt_optional_parts():
    back = post_from_queue_payload(
        _item(
            {
                "post_id": "p",
                "type": "text",
                "file_meta": {"size_bytes": "x"},
                "location": {"lat": "nope"},
            }
        )
    )
    assert back.file_meta is None and back.location is None


# ── Attachments ────────────────────────────────────────────────────────────


def test_attachments_validate_by_post_type():
    att = SpacePostAttachments()
    assert att.validate(PostType.TEXT, None) == {}
    poll = att.validate(
        PostType.POLL,
        {"poll": {"question": " Q ", "options": ["a", " b ", ""], "allow_multiple": 1}},
    )
    assert poll == {
        "poll": {
            "question": "Q",
            "options": ["a", "b"],
            "allow_multiple": True,
            "closes_at": None,
        }
    }
    sched = att.validate(
        PostType.SCHEDULE,
        {"schedule": {"title": "T", "slots": [{"slot_date": "2026-11-01"}]}},
    )
    assert sched["schedule"]["slots"][0]["position"] == 0
    bazaar = att.validate(PostType.BAZAAR, {"bazaar": {"title": "Bike"}})
    assert bazaar == {"bazaar": {"title": "Bike"}}


@pytest.mark.parametrize(
    ("post_type", "attachments"),
    [
        (PostType.TEXT, {"poll": {"question": "Q", "options": ["a", "b"]}}),
        (PostType.POLL, {"poll": {"question": "Q", "options": ["a"]}}),
        (PostType.POLL, {"poll": {"question": "", "options": ["a", "b"]}}),
        (PostType.TEXT, {"schedule": {"title": "T", "slots": [{"slot_date": "d"}]}}),
        (PostType.SCHEDULE, {"schedule": {"title": "T", "slots": []}}),
        (PostType.SCHEDULE, {"schedule": {"title": "T", "slots": ["x"]}}),
        (PostType.TEXT, {"bazaar": {"title": "x"}}),
        (PostType.POLL, "not a dict"),
    ],
)
def test_attachments_refuse_mismatches(post_type, attachments):
    with pytest.raises(ValueError):
        SpacePostAttachments().validate(post_type, attachments)


class _Polls:
    def __init__(self) -> None:
        self.polls: list[dict] = []
        self.schedules: list[dict] = []

    async def create_poll(self, **kw):
        self.polls.append(kw)
        return {}

    async def create_poll_once(self, **kw):
        if any(p["post_id"] == kw["post_id"] for p in self.polls):
            return False
        self.polls.append(kw)
        return True

    async def create_schedule_poll_once(self, **kw):
        if any(p["post_id"] == kw["post_id"] for p in self.schedules):
            return False
        self.schedules.append(kw)
        return True

    async def create_schedule_poll(self, **kw):
        self.schedules.append(kw)
        return {}


class _Bazaar:
    def __init__(self) -> None:
        self.listings: list[dict] = []

    async def create_listing_once(self, *, space_id, post, fields):
        if any(entry["post"] == post.id for entry in self.listings):
            return False
        self.listings.append({"space_id": space_id, "post": post.id, **fields})
        return True


async def test_attachments_apply_creates_each_kind():
    polls, bazaar = _Polls(), _Bazaar()
    att = SpacePostAttachments(poll_service=polls)
    att.attach_bazaar(bazaar)
    post = Post(id="p", author="u", type=PostType.POLL, created_at=_NOW)
    await att.apply(
        "sp",
        post,
        {
            "poll": {"question": "Q", "options": ["a", "b"]},
            "schedule": {"title": "T", "slots": [{"slot_date": "d"}]},
            "bazaar": {"title": "Bike"},
        },
    )
    assert polls.polls[0]["post_id"] == "p" and polls.polls[0]["space_id"] == "sp"
    assert polls.schedules[0]["title"] == "T"
    assert bazaar.listings == [{"space_id": "sp", "post": "p", "title": "Bike"}]
    # Idempotent: a repeat creates nothing more.
    assert not await att.apply(
        "sp",
        post,
        {
            "poll": {"question": "Q", "options": ["a", "b"]},
            "schedule": {"title": "T", "slots": [{"slot_date": "d"}]},
            "bazaar": {"title": "Bike"},
        },
    )
    assert (len(polls.polls), len(polls.schedules), len(bazaar.listings)) == (1, 1, 1)


async def test_attachments_apply_fails_closed_without_creators():
    post = Post(id="p", author="u", type=PostType.POLL, created_at=_NOW)
    with pytest.raises(RuntimeError):
        await SpacePostAttachments().apply("sp", post, {"poll": {"question": "Q"}})
    with pytest.raises(RuntimeError):
        await SpacePostAttachments().apply("sp", post, {"bazaar": {"title": "x"}})


# ── Handler ────────────────────────────────────────────────────────────────


class _SpaceService:
    def __init__(self) -> None:
        self.attachments = SpacePostAttachments()
        self.posts: dict[str, Post] = {}
        self.published: list[tuple] = []
        self.relays: list = []

    def post_attachments(self):
        return self.attachments

    async def get_space_post(self, space_id, post_id):
        return self.posts.get(post_id)

    async def publish_approved_post(
        self, space_id, post, *, approved_by, attachments, public_relay=None
    ):
        self.posts[post.id] = post
        self.published.append((space_id, post.id, approved_by, attachments))
        self.relays.append(public_relay)
        return post


def _payload(**extra) -> dict:
    post = Post(
        id="p-9", author="u-mem", type=PostType.TEXT, created_at=_NOW, content="c"
    )
    payload = post_to_queue_payload(post)
    payload.update(extra)
    return payload


async def test_handler_validate_snapshot_apply_preview():
    svc = _SpaceService()
    h = PostModerationHandler(svc)  # type: ignore[arg-type]
    clean = h.validate(_space(), _payload())
    assert clean["post_id"] == "p-9" and clean["attachments"] == {}
    with pytest.raises(ValueError):
        h.validate(_space(), {"entity": "page"})
    with pytest.raises(ValueError):
        h.validate(_space(), {"entity": "post", "type": "text"})
    item = _item(clean)
    assert await h.snapshot("sp", "p-9") is None
    result = await h.apply(item, approved_by="u-mod", force=False)
    assert (result.target_id, result.post_id) == ("p-9", "p-9")
    assert svc.published == [("sp", "p-9", "u-mod", {})]
    assert (svc.posts["p-9"].author, svc.posts["p-9"].content) == ("u-mem", "c")
    # Idempotent: an already-published post is not published again.
    await h.apply(item, approved_by="u-mod", force=False)
    assert len(svc.published) == 1
    assert await h.snapshot("sp", "p-9") == {"type": "text", "content": "c"}
    preview = h.preview(_item({**clean, "attachments": {"poll": {"question": "Q"}}}))
    assert preview["content"] == "c" and preview["poll"] == {"question": "Q"}


async def test_handler_apply_hands_the_signed_copy_on():
    """The submitter's author-signed copy the queue kept goes with the
    approved post, so a seed holder can relay it to GFS followers."""
    svc = _SpaceService()
    h = PostModerationHandler(svc)  # type: ignore[arg-type]
    clean = h.validate(_space(), _payload())
    await h.apply(
        _item({**clean, "public_relay": {"post_id": "p-9"}}),
        approved_by="u-mod",
        force=False,
    )
    assert svc.relays == [{"post_id": "p-9"}]
    svc2 = _SpaceService()
    h2 = PostModerationHandler(svc2)  # type: ignore[arg-type]
    await h2.apply(
        _item({**clean, "public_relay": "junk"}), approved_by="u-mod", force=False
    )
    assert svc2.relays == [None]
