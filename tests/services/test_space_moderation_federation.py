"""Unit tests for socialhome.services.space_moderation_federation (v_43).

The protocol-level proof (four real households) lives in
``tests/protocol/test_space_moderation_federated.py``; these pin the
outbound targeting and the remote-payload sanitiser over stubs.
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from socialhome.crypto import (
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
)
from socialhome.domain.federation import DeliveryResult, FederationEventType
from socialhome.domain.post import Post, PostType
from socialhome.domain.space import (
    ContentAction,
    HostTooOldError,
    ModerationStatus,
    SpaceModerationItem,
    SpaceType,
)
from socialhome.federation.owner_bound_id import SPACE_POST_KIND, mint_owner_bound_id
from socialhome.services.media_file_hasher import MediaFileHasher
from socialhome.services.space_moderation_federation import (
    SpaceModerationFederation,
    _signed_copy,
    moderation_media_urls,
    sanitize_remote_payload,
)
from socialhome.services.space_public_author import (
    build_signed_author_inner,
    verify_signed_author_inner,
)

FET = FederationEventType
NOW = datetime(2026, 6, 1, tzinfo=UTC)


class _Fed:
    """Only the targeted door — no ``broadcast_to_space_members`` at all, so
    a pending item that tried to broadcast would raise."""

    def __init__(self, own: str, versions: dict[str, int]) -> None:
        self.own_instance_id = own
        self.versions = versions
        self.sent: list[tuple[str, FederationEventType, dict]] = []

    async def space_member_supports(self, instance_id, *, min_version):
        # The space-member judge: a mesh-only reviewer household counts by
        # the version it claimed, not by a pairing row. ``peer_supports``
        # is deliberately absent — the queue must not gate on it.
        return self.versions.get(instance_id, 0) >= min_version

    async def send_with_mesh_fallback(
        self, *, to_instance_id, event_type, payload, space_id=None
    ):
        self.sent.append((to_instance_id, event_type, payload))
        return DeliveryResult(instance_id=to_instance_id, ok=True)


class _Seats:
    def __init__(self, rows) -> None:
        self.rows = rows

    async def list_instances_with_roles(self, space_id, roles):
        return sorted(
            {r.instance_id for r in self.rows if r.role in roles and not r.tombstoned}
        )

    async def get_including_tombstones(self, space_id, instance_id, user_id):
        for r in self.rows:
            if r.user_id == user_id:
                return r
        return None


class _Media:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def enqueue_for_post(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _seat(inst, uid, role="member", tombstoned=False):
    return SimpleNamespace(
        instance_id=inst,
        user_id=uid,
        role=role,
        tombstoned=tombstoned,
        display_name=uid.title(),
    )


SPACE = SimpleNamespace(id="sp", owner_instance_id="host")


def _fed(own="house-a", versions=None, rows=None, media=None):
    fed = _Fed(own, versions or {"host": 43, "house-mod": 43, "house-adm": 43})
    out = SpaceModerationFederation(
        federation_service=fed,  # type: ignore[arg-type]
        space_repo=SimpleNamespace(),  # type: ignore[arg-type]
        remote_member_repo=_Seats(  # type: ignore[arg-type]
            rows
            if rows is not None
            else [
                _seat("host", "u-h"),
                _seat("house-a", "u-a"),
                _seat("house-d", "u-d"),
                _seat("house-mod", "u-mod", "moderator"),
                _seat("house-adm", "u-adm", "admin"),
                _seat("house-gone", "u-g", "moderator", tombstoned=True),
            ]
        ),
        authorship=SimpleNamespace(),  # type: ignore[arg-type]
        media_sync=media,
    )
    return out, fed


def _item(feature="stickies", payload=None):
    return SpaceModerationItem(
        id="item-1",
        space_id="sp",
        feature=feature,
        action="create",
        submitted_by="u-a",
        payload=payload or {"entity": "sticky", "target_id": "s-1", "content": "x"},
        current_snapshot=None,
        submitted_at=NOW,
        expires_at=NOW + timedelta(days=7),
        status=ModerationStatus.PENDING,
    )


async def test_targets_are_the_host_and_live_reviewer_households_only():
    out, _ = _fed()
    assert await out.submission_targets(SPACE) == ["host", "house-adm", "house-mod"]


async def test_the_host_sends_to_the_reviewers_but_never_to_itself():
    out, _ = _fed(own="host")
    assert await out.submission_targets(SPACE) == ["house-adm", "house-mod"]


async def test_an_old_reviewer_is_skipped_and_an_old_host_refuses():
    out, _ = _fed(versions={"host": 43, "house-mod": 42, "house-adm": 43})
    assert await out.submission_targets(SPACE) == ["host", "house-adm"]
    old_host, _ = _fed(versions={"host": 42, "house-mod": 43, "house-adm": 43})
    with pytest.raises(HostTooOldError):
        await old_host.submission_targets(SPACE)


class _MirroredRoster:
    """A member household's ``space_members``: our own people plus the
    host's, mirrored by the §25.6 ``members`` sync (no ``users`` row)."""

    def __init__(self, local, mirrored) -> None:
        self._local = local
        self._mirrored = mirrored

    async def list_members(self, space_id):
        return [
            SimpleNamespace(user_id=u, role=r)
            for u, r in (*self._local, *self._mirrored)
        ]

    async def list_local_member_user_ids(self, space_id):
        return [u for u, _r in self._local]


async def test_a_mirrored_host_owner_does_not_make_this_household_a_reviewer():
    out, _ = _fed()
    out._spaces = _MirroredRoster(  # type: ignore[assignment]
        local=[("u-a", "member")], mirrored=[("u-host-owner", "owner")]
    )
    assert not await out._reviews_here(SPACE)  # type: ignore[arg-type]
    out._spaces = _MirroredRoster(  # type: ignore[assignment]
        local=[("u-a", "moderator")], mirrored=[("u-host-owner", "owner")]
    )
    assert await out._reviews_here(SPACE)  # type: ignore[arg-type]


async def test_a_submission_is_one_sealed_send_per_target_never_a_broadcast():
    out, fed = _fed()
    await out.send_submitted(SPACE, _item(), ["host", "house-mod"])
    assert [(to, et) for to, et, _p in fed.sent] == [
        ("host", FET.SPACE_MODERATION_SUBMITTED),
        ("house-mod", FET.SPACE_MODERATION_SUBMITTED),
    ]
    payload = fed.sent[0][2]
    assert payload["item_id"] == "item-1"
    assert payload["submitted_by"] == "u-a"
    assert payload["payload"]["content"] == "x"
    assert payload["space_id"] == "sp"


async def test_a_posts_media_goes_to_the_targets_only():
    media = _Media()
    out, _ = _fed(media=media)
    post = _item(
        "posts",
        {
            "entity": "post",
            "target_id": "p-1",
            "image_urls": ["api/media/a.webp"],
            "media_url": None,
            "attachments": {"bazaar": {"image_urls": ["api/media/b.webp"]}},
        },
    )
    await out.send_submitted(SPACE, post, ["host"])
    assert media.calls == [
        {
            # Correlated by the queue item: a reviewer checks the files
            # against the item it holds, not the post's current media.
            "post_id": "item-1",
            "target_instance_ids": ["host"],
            "media_urls": ["api/media/a.webp", "api/media/b.webp"],
            "space_id": "sp",
        }
    ]
    await out.send_submitted(SPACE, _item(), ["host"])
    assert len(media.calls) == 1  # a sticky has no media


async def test_a_held_calendar_events_cover_goes_to_the_reviewers():
    """A held event (or an edit setting a new cover) brings its cover file
    to the reviewer households — the host shows it in the queue, and once
    approved its own calendar save sends it on to every member."""
    media = _Media()
    out, _ = _fed(media=media)
    create = _item(
        "calendar",
        {"entity": "event", "target_id": None, "cover_url": "api/media/c.webp"},
    )
    await out.send_submitted(SPACE, create, ["host"])
    edit = _item(
        "calendar",
        {
            "entity": "event",
            "target_id": "ev-1",
            "patch": {"cover_url": "api/media/d.webp"},
        },
    )
    await out.send_submitted(SPACE, edit, ["host"])
    assert [c["media_urls"] for c in media.calls] == [
        ["api/media/c.webp"],
        ["api/media/d.webp"],
    ]
    # Regression: an edit was correlated by the event id, and a reviewer
    # household then refused the new cover (not the event's current one).
    assert [c["post_id"] for c in media.calls] == ["item-1", "item-1"]
    # An event with no cover, and an edit leaving it alone, send no file.
    await out.send_submitted(
        SPACE, _item("calendar", {"entity": "event", "summary": "x"}), ["host"]
    )
    await out.send_submitted(
        SPACE,
        _item(
            "calendar",
            {"entity": "event", "target_id": "ev-1", "patch": {"summary": "y"}},
        ),
        ["host"],
    )
    assert len(media.calls) == 2


async def test_a_decision_reaches_the_reviewers_and_the_submitters_household():
    out, fed = _fed(
        own="house-mod", versions={"host": 43, "house-a": 43, "house-adm": 43}
    )
    await out.send_decided(
        SPACE,
        _item(),
        decision=ModerationStatus.REJECTED,
        decided_by="u-mod",
        reason="spam",
    )
    assert sorted(to for to, _et, _p in fed.sent) == ["host", "house-a", "house-adm"]
    assert all(et is FET.SPACE_MODERATION_DECIDED for _to, et, _p in fed.sent)
    assert fed.sent[0][2]["reason"] == "spam"
    assert "house-d" not in {to for to, _et, _p in fed.sent}


async def test_remote_writer_and_display_name_read_the_roster_mirror():
    out, _ = _fed()
    assert await out.is_remote_writer("sp", "u-d")
    assert not await out.is_remote_writer("sp", "u-g")  # tombstoned
    assert not await out.is_remote_writer("sp", "u-a")  # our own household
    assert not await out.is_remote_writer("sp", "u-nobody")
    assert await out.display_name("sp", "u-d") == "U-D"
    assert await out.display_name("sp", "u-nobody") is None


def test_sanitize_keeps_local_media_and_drops_remote_urls():
    out = sanitize_remote_payload(
        "posts",
        {
            "media_url": "https://tracker.example/x.gif",
            "image_urls": [
                "api/media/a.webp",
                "https://evil/b.png",
                "/api/media/c.webp",
            ],
            "file_meta": {"url": "https://evil/f.pdf"},
            "location": {"lat": 47.123456789, "lon": 8.987654321, "label": None},
            "link_preview": {"url": "https://x", "thumbnail_url": "https://evil/t.png"},
            "attachments": {"bazaar": {"image_urls": ["https://evil/z.png"]}},
        },
    )
    assert out["media_url"] is None
    assert out["image_urls"] == ["api/media/a.webp", "/api/media/c.webp"]
    assert out["file_meta"] is None
    assert out["location"] == {"lat": 47.1235, "lon": 8.9877, "label": None}
    assert out["link_preview"]["thumbnail_url"] is None
    assert out["attachments"]["bazaar"]["image_urls"] == []
    page = sanitize_remote_payload(
        "pages",
        {"cover_image_url": "https://evil/c.png", "patch": {"cover_image_url": "x"}},
    )
    assert page["cover_image_url"] is None
    assert page["patch"]["cover_image_url"] is None
    event = sanitize_remote_payload("calendar", {"cover_url": "/api/media/ok.webp"})
    assert event["cover_url"] == "/api/media/ok.webp"
    assert sanitize_remote_payload("stickies", {"content": "x"}) == {"content": "x"}


async def test_a_reviewer_households_approval_goes_to_the_host_only():
    out, fed = _fed(own="house-mod")
    await out.send_release_request(SPACE, _item(), decided_by="u-mod")
    assert [(to, et, p["decision"]) for to, et, p in fed.sent] == [
        ("host", FET.SPACE_MODERATION_DECIDED, "approved")
    ]
    host_side, host_fed = _fed(own="host")
    await host_side.send_release_request(SPACE, _item(), decided_by="u-h")
    assert host_fed.sent == []


# ─── A queued public post carries its author-signed copy ────────────────

_AUTHOR_KP = generate_identity_keypair()
_AUTHOR = derive_user_id(_AUTHOR_KP.public_key, "alice")
_POST_ID = mint_owner_bound_id(SPACE_POST_KIND, space_id="sp", owner_user_id=_AUTHOR)


class _Users:
    async def get_by_user_id(self, user_id):
        if user_id != _AUTHOR:
            return None
        return SimpleNamespace(username="alice", identity_anchor="alice")


def _public_space(space_type=SpaceType.GLOBAL, allow_subscribers=True):
    return SimpleNamespace(
        id="sp",
        owner_instance_id="host",
        space_type=space_type,
        features=SimpleNamespace(allow_subscribers=allow_subscribers),
    )


def _post_item(submitted_by=_AUTHOR, feature="posts", entity="post"):
    item = _item(
        feature=feature,
        payload={
            "entity": entity,
            "target_id": _POST_ID,
            "post_id": _POST_ID,
            "type": "text",
            "content": "queued words",
            "image_urls": [],
            "hidden_from_feed": False,
        },
    )
    return dataclasses.replace(item, submitted_by=submitted_by)


def _signing_fed():
    out, fed = _fed(own=derive_instance_id(_AUTHOR_KP.public_key))
    out.attach_identity(
        own_instance_pk=_AUTHOR_KP.public_key,
        own_identity_seed=_AUTHOR_KP.private_key,
        user_repo=_Users(),  # type: ignore[arg-type]
    )
    return out, fed


async def test_a_queued_public_post_carries_its_authors_signed_copy():
    out, fed = _signing_fed()
    await out.send_submitted(_public_space(), _post_item(), ["host"])
    relay = fed.sent[0][2]["public_relay"]
    assert verify_signed_author_inner(relay)
    assert relay["post_id"] == _POST_ID
    assert relay["author_user_id"] == _AUTHOR
    assert relay["content"] == "queued words"
    # Signed over the submission time — the approval time is not known yet.
    assert relay["created_at"] == NOW.isoformat()
    # The queue payload itself is untouched.
    assert "public_relay" not in fed.sent[0][2]["payload"]


@pytest.mark.parametrize(
    "case", ["private", "no_followers", "sticky", "remote_user", "no_identity"]
)
async def test_no_signed_copy_where_nothing_may_reach_followers(case):
    out, fed = _signing_fed()
    space = _public_space()
    item = _post_item()
    if case == "private":
        space = _public_space(space_type=SpaceType.PRIVATE)
    elif case == "no_followers":
        space = _public_space(allow_subscribers=False)
    elif case == "sticky":
        item = _item()
    elif case == "remote_user":
        item = _post_item(submitted_by="someone-else")
    else:
        out, fed = _fed()
    await out.send_submitted(space, item, ["host"])
    assert "public_relay" not in fed.sent[0][2]


def _copy(**over):
    post = Post(
        id=_POST_ID,
        author=_AUTHOR,
        type=PostType.TEXT,
        content="queued words",
        created_at=NOW,
    )
    relay = build_signed_author_inner(
        post=post,
        space_id="sp",
        author_username="alice",
        author_pk=_AUTHOR_KP.public_key,
        author_identity_seed=_AUTHOR_KP.private_key,
        origin_instance_id=derive_instance_id(_AUTHOR_KP.public_key),
    )
    relay.update(over)
    return relay


def _keep(raw, *, space=None, feature="posts", action=ContentAction.CREATE):
    return _signed_copy(
        raw,
        space=space or _public_space(),  # type: ignore[arg-type]
        feature=feature,
        action=action,
        target_id=_POST_ID,
        submitted_by=_AUTHOR,
    )


def test_the_host_keeps_a_signed_copy_that_verifies():
    assert _keep(_copy()) == _copy()


@pytest.mark.parametrize(
    "case",
    [
        "not_a_dict",
        "tampered",
        "other_post",
        "other_author",
        "other_space",
        "private",
        "not_posts",
        "an_edit",
    ],
)
def test_the_host_drops_a_signed_copy_it_cannot_use(case):
    raw: object = _copy()
    kwargs: dict = {}
    if case == "not_a_dict":
        raw = "x"
    elif case == "tampered":
        raw = _copy(content="other words")
    elif case == "other_post":
        raw = _copy(post_id="p-other")
    elif case == "other_author":
        raw = _copy(author_user_id="u-other")
    elif case == "other_space":
        raw = _copy(space_id="sp-other")
    elif case == "private":
        kwargs["space"] = _public_space(space_type=SpaceType.PRIVATE)
    elif case == "not_posts":
        kwargs["feature"] = "stickies"
    else:
        kwargs["action"] = ContentAction.EDIT
    assert _keep(raw, **kwargs) is None


async def test_an_edit_keeping_the_same_cover_sends_no_file():
    """Regression: re-sending the event's current cover in an edit shipped
    the file to every reviewer again."""
    media = _Media()
    out, _ = _fed(media=media)
    same = dataclasses.replace(
        _item(
            "calendar",
            {
                "entity": "event",
                "target_id": "ev-1",
                "patch": {"cover_url": "api/media/d.webp", "summary": "y"},
            },
        ),
        action="edit",
        current_snapshot='{"cover_url": "/api/media/d.webp", "summary": "x"}',
    )
    await out.send_submitted(SPACE, same, ["host"])
    assert media.calls == []
    changed = dataclasses.replace(
        same, current_snapshot='{"cover_url": "api/media/old.webp"}'
    )
    await out.send_submitted(SPACE, changed, ["host"])
    assert [c["media_urls"] for c in media.calls] == [["api/media/d.webp"]]


def test_moderation_media_urls_lists_what_an_item_proposes():
    assert moderation_media_urls(
        _item(
            "calendar", {"entity": "event", "patch": {"cover_url": "api/media/n.webp"}}
        )
    ) == ["api/media/n.webp"]
    assert moderation_media_urls(
        _item("posts", {"entity": "post", "image_urls": ["api/media/p.webp"]})
    ) == ["api/media/p.webp"]
    assert moderation_media_urls(_item()) == []


async def test_a_held_post_edit_sends_only_the_pictures_it_changes():
    """Pictures the post already has (``current_snapshot``) are on every
    reviewer; only the new ones travel."""
    media = _Media()
    out, _ = _fed(media=media)
    item = dataclasses.replace(
        _item(
            "posts",
            {
                "entity": "post",
                "image_urls": ["api/media/kept.webp", "api/media/new.webp"],
            },
        ),
        action="edit",
        current_snapshot='{"image_urls": ["/api/media/kept.webp"]}',
    )
    await out.send_submitted(SPACE, item, ["host"])
    assert [c["media_urls"] for c in media.calls] == [["api/media/new.webp"]]


@pytest.mark.parametrize("reserved", ["dm-x.webp", "gm-x.webp", "sg-x.webp"])
def test_a_page_submission_never_keeps_a_reserved_file(reserved):
    """A page cover or body picture naming a file another receiving path owns
    (a DM attachment, GFS moment media, a GFS reader copy) is dropped from a
    remote submission — it would show, and re-publish, someone else's file."""
    body = f"keep ![a](api/media/ok.webp) drop ![r](api/media/{reserved}) end"
    out = sanitize_remote_payload(
        "pages",
        {
            "title": "T",
            "content": body,
            "cover_image_url": f"api/media/{reserved}",
        },
    )
    assert out["cover_image_url"] is None
    assert out["content"] == "keep ![a](api/media/ok.webp) drop ![r]() end"
    edit = sanitize_remote_payload(
        "pages",
        {
            "patch": {
                "cover_image_url": f"/api/media/{reserved}",
                "content": f"![r](/api/media/{reserved}?exp=1)",
            }
        },
    )
    assert edit["patch"] == {"cover_image_url": None, "content": "![r]()"}
    merged = sanitize_remote_payload(
        "pages",
        {"op": "resolve_conflict", "merged_content": f"![r](api/media/{reserved})"},
    )
    assert merged["merged_content"] == "![r]()"
    # A space file a household delivered (``sd-``) is a space file: kept,
    # and renamed to the submitting household's copy at admission.
    sd = sanitize_remote_payload("pages", {"cover_image_url": "api/media/sd-x.webp"})
    assert sd["cover_image_url"] == "api/media/sd-x.webp"


@pytest.mark.parametrize("reserved", ["dm-x.webp", "gm-x.webp", "sg-x.webp"])
def test_a_post_or_event_submission_never_keeps_a_reserved_file(reserved):
    """The same rule for a post's files and an event's cover (kept byte for
    byte otherwise: an author's signed copy covers them)."""
    ref = f"api/media/{reserved}"
    post = sanitize_remote_payload(
        "posts",
        {
            "media_url": ref,
            "image_urls": [ref, "/api/media/ok.webp"],
            "file_meta": {"url": ref},
            "link_preview": {"url": "https://x.example", "thumbnail_url": ref},
            "attachments": {"bazaar": {"image_urls": [ref]}},
        },
    )
    assert post["media_url"] is None
    assert post["image_urls"] == ["/api/media/ok.webp"]
    assert post["file_meta"] is None
    assert post["link_preview"]["thumbnail_url"] is None
    assert post["attachments"]["bazaar"]["image_urls"] == []
    event = sanitize_remote_payload("calendar", {"cover_url": ref})
    assert event["cover_url"] is None
    edit = sanitize_remote_payload("calendar", {"patch": {"cover_url": ref}})
    assert edit["patch"]["cover_url"] is None


async def test_a_held_pages_cover_and_body_pictures_go_to_the_reviewers():
    """A held page brings its cover and body pictures to the reviewer
    households; an edit only the files the page does not show yet."""
    media = _Media()
    out, _ = _fed(media=media)
    create = _item(
        "pages",
        {
            "entity": "page",
            "target_id": "pg-1",
            "title": "T",
            "content": "![a](api/media/a.webp) ![b](api/media/b.webp)",
            "cover_image_url": "api/media/c.webp",
        },
    )
    await out.send_submitted(SPACE, create, ["host"])
    edit = dataclasses.replace(
        _item(
            "pages",
            {
                "entity": "page",
                "target_id": "pg-1",
                "patch": {"content": "![a](api/media/a.webp) ![n](api/media/n.webp)"},
            },
        ),
        action="edit",
        current_snapshot='{"content": "![a](api/media/a.webp)"}',
    )
    await out.send_submitted(SPACE, edit, ["host"])
    assert [c["media_urls"] for c in media.calls] == [
        ["api/media/c.webp", "api/media/a.webp", "api/media/b.webp"],
        ["api/media/n.webp"],
    ]


async def test_a_submissions_files_ride_with_their_hashes_to_v85_only(tmp_path):
    """v_85 — a held item's proposed files carry their SHA-256 to a v_85
    reviewer household; an older one gets the item as before (its release
    check would refuse an unknown key). A page's files stay page-scoped:
    no map."""
    (tmp_path / "c.webp").write_bytes(b"cover")
    out, fed = _fed(versions={"host": 85, "house-mod": 84})
    out.attach_file_hasher(MediaFileHasher(tmp_path))
    create = _item(
        "calendar",
        {"entity": "event", "target_id": None, "cover_url": "api/media/c.webp"},
    )
    await out.send_submitted(SPACE, create, ["host", "house-mod"])
    sent = {to: payload for to, _t, payload in fed.sent}
    assert sent["host"]["files"] == {
        "c.webp": {"h": hashlib.sha256(b"cover").hexdigest()}
    }
    assert sent["host"]["files_suite"] == "sha256"
    assert "files" not in sent["house-mod"]
    fed.sent.clear()
    page = _item(
        "pages",
        {"entity": "page", "target_id": None, "cover_image_url": "api/media/c.webp"},
    )
    await out.send_submitted(SPACE, page, ["host"])
    assert "files" not in fed.sent[0][2]


async def test_a_post_with_a_signed_follower_copy_carries_no_hashes(
    tmp_path, monkeypatch
):
    """The host matches the author-signed follower copy's file names against
    the files it holds by their sender-bound names (``approved_relay_for``):
    such a submission stays unhashed."""
    (tmp_path / "p.webp").write_bytes(b"pic")
    out, fed = _fed(versions={"host": 85})
    out.attach_file_hasher(MediaFileHasher(tmp_path))

    async def _relay(_self, _space, _item):
        return {"signed": True}

    monkeypatch.setattr(type(out), "_public_relay_for", _relay)
    post = _item(
        "posts",
        {"entity": "post", "target_id": None, "image_urls": ["api/media/p.webp"]},
    )
    await out.send_submitted(SPACE, post, ["host"])
    assert "files" not in fed.sent[0][2]
