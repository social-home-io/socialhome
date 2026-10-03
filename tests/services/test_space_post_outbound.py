"""Tests for the SpacePostCreated → SPACE_POST_CREATED federation bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from socialhome.crypto import (
    b64url_decode,
    derive_user_id,
    generate_identity_keypair,
    verify_ed25519,
)
from socialhome.domain.events import (
    PostDeleted,
    PostEdited,
    SpacePostCreated,
)
from socialhome.domain.federation import FederationEventType
from socialhome.domain.link_preview import LinkPreview
from socialhome.domain.post import Post, PostType
from socialhome.domain.space import JoinMode, SpaceType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.moderation_release import release_scope
from socialhome.services.space_post_outbound import SpacePostOutbound
from socialhome.services.space_public_author import (
    author_signing_bytes,
    verify_signed_author_inner,
)
from tests.services.test_space_public_author import _v25_author_signing_bytes


@dataclass
class _FakeFeatures:
    #: Mirrors ``SpaceFeatures.allow_subscribers`` — the owner's readability
    #: opt-in. OFF ⇒ the space is listed for discovery but never GFS-relayed,
    #: so it gets no relay hint. Defaults ON here because most tests in this
    #: module are about the hint's CONTENT.
    allow_subscribers: bool = True


@dataclass
class _FakeSpace:
    space_type: SpaceType
    #: A pure membership gate now — it has no say over the relay hint.
    join_mode: JoinMode = JoinMode.OPEN
    features: _FakeFeatures = field(default_factory=_FakeFeatures)


@dataclass
class _FakeUser:
    username: str
    identity_anchor: str | None = None


class _FakeSpaceRepo:
    """Minimal space repo — resolves ``get`` for the relay-hint gate."""

    def __init__(self, spaces: dict[str, _FakeSpace] | None = None) -> None:
        self._spaces = spaces or {}

    async def get(self, space_id: str) -> _FakeSpace | None:
        return self._spaces.get(space_id)


class _FakeUserRepo:
    """Minimal user repo — resolves ``get_by_user_id`` for the relay hint."""

    def __init__(self, users: dict[str, _FakeUser] | None = None) -> None:
        self._users = users or {}

    async def get_by_user_id(self, user_id: str) -> _FakeUser | None:
        return self._users.get(user_id)


def _make_outbound(
    *,
    bus: EventBus,
    federation: AsyncMock,
    space_repo: _FakeSpaceRepo | None = None,
    user_repo: _FakeUserRepo | None = None,
    media_sync=None,
    federation_repo=None,
    identity=None,
) -> SpacePostOutbound:
    """Build a ``SpacePostOutbound`` with the new required repo deps.

    By default no identity is attached, so the ``public_relay`` hint is
    omitted (existing broadcast tests stay unchanged). Pass ``identity`` (an
    ``Ed25519Keypair`` + instance id tuple) to opt in to the relay hint.
    """
    outbound = SpacePostOutbound(
        bus=bus,
        federation_service=federation,
        space_repo=space_repo or _FakeSpaceRepo(),
        user_repo=user_repo or _FakeUserRepo(),
        media_sync=media_sync,
        federation_repo=federation_repo,
    )
    if identity is not None:
        keypair, instance_id = identity
        outbound.attach_identity(
            own_instance_id=instance_id,
            own_instance_public_key=keypair.public_key,
            own_identity_seed=keypair.private_key,
        )
    return outbound


async def test_space_post_created_broadcasts_to_space_members():
    """When a local user creates a space post, the bus event must
    federate via ``broadcast_to_space_members`` so every member
    household (direct + mesh-only) sees it. Without this bridge,
    posts in cross-household spaces stay invisible to remote
    members."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)

    post = Post(
        id="post-xyz",
        author="uid-alice",
        type=PostType.TEXT,
        content="hello space members",
        created_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))

    federation.broadcast_to_space_members.assert_awaited_once()
    call = federation.broadcast_to_space_members.call_args
    assert call.args[0] == "sp-1"
    assert call.args[1] is FederationEventType.SPACE_POST_CREATED
    payload = call.args[2]
    assert payload["id"] == "post-xyz"
    assert payload["space_id"] == "sp-1"
    assert payload["author"] == "uid-alice"
    assert payload["type"] == "text"
    assert payload["content"] == "hello space members"
    # ``broadcast_to_space_members`` already targets members-only
    # via space_instances and routes mesh-fallback for unpaired
    # members — see CLAUDE.md "Encryption-First Rule". The bridge
    # doesn't need to gate; it just publishes.


async def test_inbound_replay_does_not_loop_back_via_outbound():
    """When ``federation_inbound_service`` receives a SPACE_POST_CREATED
    from peer P and re-publishes ``SpacePostCreated`` to the local
    bus (so realtime / search / HA bridge see the new row), the
    outbound bridge MUST NOT re-broadcast — otherwise we get a
    federation loop. The ``origin_instance_id`` field on the bus
    event is the gate: ``None`` = local origination, set =
    inbound replay → skip."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)

    post = Post(
        id="p-from-peer",
        author="uid-pascal",
        type=PostType.TEXT,
        content="hi",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(
        SpacePostCreated(
            post=post,
            space_id="sp-1",
            origin_instance_id="peer-pascal-instance",
        )
    )
    federation.broadcast_to_space_members.assert_not_awaited()


async def test_calendar_event_post_not_re_federated_via_space_bridge():
    """``CalendarFeedBridge`` mints one PostType.EVENT row per
    federated calendar event on every household — the calendar event
    itself federates via ``SPACE_CALENDAR_EVENT_CREATED``. If the
    outbound bridge here ALSO federated those bridge-published
    ``SpacePostCreated`` events, every peer would receive two posts
    for one event: the bridge's deterministic mint, plus the
    peer-side inbound save (which lacks ``linked_event_id`` because
    the wire payload doesn't carry it). The bridge's update / delete
    paths would then re-federate the peer's row back to the
    originator, multiplying further. Gate is on
    ``post.linked_event_id is not None``."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)

    bridge_post = Post(
        id="p-from-bridge",
        author="uid-pascal",
        type=PostType.EVENT,
        content="Summary",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        linked_event_id="cal-event-1",
    )
    await bus.publish(SpacePostCreated(post=bridge_post, space_id="sp-1"))
    federation.broadcast_to_space_members.assert_not_awaited()


async def test_household_post_does_not_federate_via_space_bridge():
    """SpacePostCreated CAN fire with empty space_id for some legacy
    paths; the bridge must skip those to avoid mis-routing a
    household post as space content."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)

    post = Post(
        id="p",
        author="uid-alice",
        type=PostType.TEXT,
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id=""))
    federation.broadcast_to_space_members.assert_not_awaited()


async def test_broadcast_failure_logged_but_swallowed():
    """A federation failure during broadcast must NOT propagate back
    to the bus — the local DB write already happened and we don't
    want the realtime/HA/search subscribers to receive a partial
    failure exception. We log + drop."""

    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock(
        side_effect=RuntimeError("transport down")
    )
    _make_outbound(bus=bus, federation=federation)

    post = Post(
        id="p",
        author="u",
        type=PostType.TEXT,
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    # Should not raise.
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))
    federation.broadcast_to_space_members.assert_awaited_once()


# ─── PostEdited / PostDeleted federation (PR #431) ─────────────────────


async def test_post_edited_in_space_broadcasts_update():
    """When a local user edits a space post, the bus event must
    federate via ``SPACE_POST_UPDATED`` so remote members see the new
    body. PR #431 plumbed ``space_id`` onto :class:`PostEdited` for
    exactly this — the outbound bridge gates on it."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)

    post = Post(
        id="post-edit-1",
        author="uid-alice",
        type=PostType.TEXT,
        content="updated body",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(PostEdited(post=post, space_id="sp-1"))
    federation.broadcast_to_space_members.assert_awaited_once()
    call = federation.broadcast_to_space_members.call_args
    assert call.args[0] == "sp-1"
    assert call.args[1] is FederationEventType.SPACE_POST_UPDATED
    payload = call.args[2]
    assert payload["post_id"] == "post-edit-1"
    assert payload["content"] == "updated body"


async def test_post_edited_household_only_skipped():
    """``PostEdited`` with no space_id is a household-feed edit — must
    not federate as a space update."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)

    post = Post(
        id="p",
        author="u",
        type=PostType.TEXT,
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(PostEdited(post=post))
    federation.broadcast_to_space_members.assert_not_awaited()


async def test_post_edited_inbound_replay_does_not_loop():
    """Symmetric to the create case — an inbound SPACE_POST_UPDATED
    replays as ``PostEdited`` with ``origin_instance_id`` set; the
    outbound MUST NOT re-broadcast."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)

    post = Post(
        id="p",
        author="u",
        type=PostType.TEXT,
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(
        PostEdited(post=post, space_id="sp-1", origin_instance_id="peer-x"),
    )
    federation.broadcast_to_space_members.assert_not_awaited()


async def test_post_edited_calendar_post_not_re_federated():
    """``CalendarFeedBridge._on_updated`` rewrites the event post's
    body when ``CalendarEventUpdated`` fires on every peer — that
    event arrives via federated ``SPACE_CALENDAR_EVENT_UPDATED``,
    so a parallel ``SPACE_POST_UPDATED`` would race the bridge. Gate
    on ``linked_event_id`` here too."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)

    bridge_post = Post(
        id="p-bridge",
        author="uid-pascal",
        type=PostType.EVENT,
        content="New summary",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        linked_event_id="cal-event-2",
    )
    await bus.publish(PostEdited(post=bridge_post, space_id="sp-1"))
    federation.broadcast_to_space_members.assert_not_awaited()


async def test_post_edited_broadcast_failure_swallowed():
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock(
        side_effect=RuntimeError("transport down"),
    )
    _make_outbound(bus=bus, federation=federation)
    post = Post(
        id="p",
        author="u",
        type=PostType.TEXT,
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(PostEdited(post=post, space_id="sp-1"))


async def test_post_deleted_in_space_broadcasts_delete():
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)

    await bus.publish(PostDeleted(post_id="post-del-1", space_id="sp-1"))
    federation.broadcast_to_space_members.assert_awaited_once()
    call = federation.broadcast_to_space_members.call_args
    assert call.args[1] is FederationEventType.SPACE_POST_DELETED
    payload = call.args[2]
    assert payload["post_id"] == "post-del-1"
    assert payload["space_id"] == "sp-1"


async def test_post_deleted_household_only_skipped():
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)
    await bus.publish(PostDeleted(post_id="p"))
    federation.broadcast_to_space_members.assert_not_awaited()


async def test_post_deleted_inbound_replay_does_not_loop():
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)
    await bus.publish(
        PostDeleted(post_id="p", space_id="sp-1", origin_instance_id="peer-x"),
    )
    federation.broadcast_to_space_members.assert_not_awaited()


async def test_post_deleted_broadcast_failure_swallowed():
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock(
        side_effect=RuntimeError("transport down"),
    )
    _make_outbound(bus=bus, federation=federation)
    await bus.publish(PostDeleted(post_id="p", space_id="sp-1"))


@pytest.mark.parametrize(
    "type_,extra",
    [
        (PostType.IMAGE, {"image_urls": ("/api/media/a.webp", "/api/media/b.webp")}),
        (PostType.VIDEO, {"media_url": "/api/media/clip.webm"}),
    ],
)
async def test_payload_carries_media_fields(type_, extra):
    """The receiver's ``_post_from_payload`` reads ``media_url`` and
    ``image_urls`` — the outbound bridge must include them so
    rendered posts on remote members show the same media as the
    host's local card."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)

    post = Post(
        id="p",
        author="u",
        type=type_,
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        **extra,
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))
    payload = federation.broadcast_to_space_members.call_args.args[2]
    if "image_urls" in extra:
        assert payload["image_urls"] == list(extra["image_urls"])
    if "media_url" in extra:
        assert payload["media_url"] == extra["media_url"]


# ─── SPACE_MEDIA_BLOB — outbox-driven bytes federation ────────────────


async def test_space_post_created_enqueues_outbox_per_peer_per_blob():
    """After SPACE_POST_CREATED broadcasts, the outbound enqueues one
    media-outbox row per (peer, blob) tuple. The scheduler reads
    these lazily, chunks the file, and ships SPACE_MEDIA_BLOB events
    over the federation outbox. Without this, the receiver's
    ``<img src>`` 404s because the bytes never federate."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    federation._own_instance_id = "self-id"
    federation_repo = AsyncMock()
    federation_repo.list_member_instance_ids = AsyncMock(
        return_value=["peer-a", "peer-b", "self-id"],  # self filtered out
    )
    media_sync = AsyncMock()
    media_sync.enqueue_for_post = AsyncMock()
    _make_outbound(
        bus=bus,
        federation=federation,
        media_sync=media_sync,
        federation_repo=federation_repo,
    )
    post = Post(
        id="p-mediated",
        author="u",
        type=PostType.IMAGE,
        image_urls=("api/media/img-a.webp", "api/media/img-b.webp"),
        media_url="api/media/cover.webp",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))

    media_sync.enqueue_for_post.assert_awaited_once()
    call = media_sync.enqueue_for_post.call_args
    assert call.kwargs["post_id"] == "p-mediated"
    # Self instance dropped from the target list.
    assert set(call.kwargs["target_instance_ids"]) == {"peer-a", "peer-b"}
    # All referenced media URLs enqueued.
    assert set(call.kwargs["media_urls"]) == {
        "api/media/img-a.webp",
        "api/media/img-b.webp",
        "api/media/cover.webp",
    }


async def test_space_post_created_no_media_skips_enqueue():
    """Text-only post — no media URLs → no media-outbox enqueue."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    federation._own_instance_id = "self-id"
    media_sync = AsyncMock()
    media_sync.enqueue_for_post = AsyncMock()
    _make_outbound(
        bus=bus,
        federation=federation,
        media_sync=media_sync,
        federation_repo=AsyncMock(),
    )
    post = Post(
        id="p-text",
        author="u",
        type=PostType.TEXT,
        content="hi",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))
    media_sync.enqueue_for_post.assert_not_awaited()


async def test_space_post_created_no_media_sync_wired_is_noop():
    """SpacePostOutbound without ``media_sync`` (test stacks) doesn't
    crash on media posts — just skips the bytes federation."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    # NO media_sync / federation_repo.
    _make_outbound(bus=bus, federation=federation)
    post = Post(
        id="p",
        author="u",
        type=PostType.IMAGE,
        image_urls=("api/media/whatever.webp",),
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    # Should not raise.
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))


# ─── public_relay author hint (Phase 5a remote-author relay) ───────────


@pytest.mark.parametrize("tier", [SpaceType.PUBLIC, SpaceType.GLOBAL])
async def test_public_space_post_attaches_signed_public_relay(tier):
    """A public/global space post by a local author attaches a pre-signed
    ``public_relay`` inner to the member broadcast. The ``author_sig`` must
    verify against the attached ``author_pk`` over the canonical author bytes,
    and the post/author identity fields must match — so a seed-holding member
    can forward it to the GFS without forging attribution."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    keypair = generate_identity_keypair()
    space_repo = _FakeSpaceRepo({"sp-1": _FakeSpace(space_type=tier)})
    # author_user_id derives from the uuid anchor (not the username), so the
    # relay-hint MUST carry the anchor for a seed-holding member's self-cert.
    anchor = "2f3c9d1e4b5a6789abcdef0123456789"
    anchored_uid = derive_user_id(keypair.public_key, anchor)
    user_repo = _FakeUserRepo(
        {anchored_uid: _FakeUser(username="alice", identity_anchor=anchor)}
    )
    _make_outbound(
        bus=bus,
        federation=federation,
        space_repo=space_repo,
        user_repo=user_repo,
        identity=(keypair, "inst-self"),
    )

    post = Post(
        id="post-pub",
        author=anchored_uid,
        type=PostType.TEXT,
        content="public hello",
        created_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))

    payload = federation.broadcast_to_space_members.call_args.args[2]
    relay = payload["public_relay"]
    assert relay["post_id"] == "post-pub"
    assert relay["author_user_id"] == anchored_uid
    assert relay["author_username"] == "alice"
    assert relay["author_pk"] == keypair.public_key.hex()
    assert relay["identity_anchor"] == anchor
    assert relay["origin_instance_id"] == "inst-self"
    # The relay self-certifies via the anchor (username derivation would not).
    assert derive_user_id(keypair.public_key, "alice") != anchored_uid
    assert verify_signed_author_inner(relay)
    # The per-author signature verifies against the attached pubkey over the
    # canonical, domain-separated signing bytes.
    assert verify_ed25519(
        keypair.public_key,
        author_signing_bytes(relay),
        b64url_decode(relay["author_sig"]),
    )


@pytest.mark.parametrize("tier", [SpaceType.PUBLIC, SpaceType.GLOBAL])
async def test_legacy_username_anchored_author_relay_hint_is_v25_compatible(tier):
    """REGRESSION (live cross-version bug): every real user row carries an
    anchor (0041 backfilled ``identity_anchor = username``), so the producer
    always passes one. A legacy author's ``public_relay`` hint MUST carry NO
    ``identity_anchor`` key and sign the 13-field v_25 layout, so a
    seed-holding relay / subscriber on a pre-anchor build still verifies it."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    keypair = generate_identity_keypair()
    space_repo = _FakeSpaceRepo({"sp-1": _FakeSpace(space_type=tier)})
    legacy_uid = derive_user_id(keypair.public_key, "alice")
    user_repo = _FakeUserRepo(
        {legacy_uid: _FakeUser(username="alice", identity_anchor="alice")}
    )
    _make_outbound(
        bus=bus,
        federation=federation,
        space_repo=space_repo,
        user_repo=user_repo,
        identity=(keypair, "inst-self"),
    )
    post = Post(
        id="post-legacy",
        author=legacy_uid,
        type=PostType.TEXT,
        content="public hello",
        created_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))

    relay = federation.broadcast_to_space_members.call_args.args[2]["public_relay"]
    assert "identity_anchor" not in relay
    assert relay["author_username"] == "alice"
    # v_25 verifier (13-field layout) accepts the signature...
    assert verify_ed25519(
        keypair.public_key,
        _v25_author_signing_bytes(relay),
        b64url_decode(relay["author_sig"]),
    )
    # ...and so does the current verifier.
    assert verify_signed_author_inner(relay)


async def test_private_space_post_omits_public_relay():
    """A PRIVATE space never relays to the GFS, so the member broadcast must
    NOT carry a ``public_relay`` hint."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    keypair = generate_identity_keypair()
    space_repo = _FakeSpaceRepo({"sp-1": _FakeSpace(space_type=SpaceType.PRIVATE)})
    user_repo = _FakeUserRepo({"uid-alice": _FakeUser(username="alice")})
    _make_outbound(
        bus=bus,
        federation=federation,
        space_repo=space_repo,
        user_repo=user_repo,
        identity=(keypair, "inst-self"),
    )

    post = Post(
        id="p",
        author="uid-alice",
        type=PostType.TEXT,
        content="private",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))

    payload = federation.broadcast_to_space_members.call_args.args[2]
    assert "public_relay" not in payload


async def test_public_space_without_identity_omits_public_relay():
    """If identity isn't attached (empty seed), the producer cannot sign the
    author hint — it degrades to omitting ``public_relay`` while the normal
    broadcast still fires."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    space_repo = _FakeSpaceRepo({"sp-1": _FakeSpace(space_type=SpaceType.PUBLIC)})
    user_repo = _FakeUserRepo({"uid-alice": _FakeUser(username="alice")})
    # No identity attached.
    _make_outbound(
        bus=bus,
        federation=federation,
        space_repo=space_repo,
        user_repo=user_repo,
    )

    post = Post(
        id="p",
        author="uid-alice",
        type=PostType.TEXT,
        content="public",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))

    federation.broadcast_to_space_members.assert_awaited_once()
    payload = federation.broadcast_to_space_members.call_args.args[2]
    assert "public_relay" not in payload


# ─── allow_subscribers OFF: listed for discovery, never publicly readable ──


@pytest.mark.parametrize("tier", [SpaceType.PUBLIC, SpaceType.GLOBAL])
async def test_space_without_subscribers_omits_public_relay(tier):
    """A public/global space whose owner has not opted into subscribers is
    listed for discovery but never publicly readable: its content is never
    relayed to the GFS. The pre-signed ``public_relay`` hint exists solely so
    a seed-holding member can run that relay, so it must be neither signed nor
    shipped."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    keypair = generate_identity_keypair()
    space_repo = _FakeSpaceRepo(
        {
            "sp-1": _FakeSpace(
                space_type=tier,
                features=_FakeFeatures(allow_subscribers=False),
            )
        }
    )
    uid = derive_user_id(keypair.public_key, "alice")
    user_repo = _FakeUserRepo({uid: _FakeUser(username="alice")})
    _make_outbound(
        bus=bus,
        federation=federation,
        space_repo=space_repo,
        user_repo=user_repo,
        identity=(keypair, "inst-self"),
    )

    post = Post(
        id="post-invite",
        author=uid,
        type=PostType.TEXT,
        content="members only",
        created_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))

    payload = federation.broadcast_to_space_members.call_args.args[2]
    assert "public_relay" not in payload


@pytest.mark.parametrize("tier", [SpaceType.PUBLIC, SpaceType.GLOBAL])
async def test_space_without_subscribers_still_broadcasts_to_members(tier):
    """Suppressing the relay hint is NOT "such spaces don't federate":
    genuine members are fanned out over ``space_instances`` by
    ``broadcast_to_space_members``, independently of the hint, so a remote
    MEMBER of a non-readable global space still receives the post in full."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    keypair = generate_identity_keypair()
    space_repo = _FakeSpaceRepo(
        {
            "sp-1": _FakeSpace(
                space_type=tier,
                features=_FakeFeatures(allow_subscribers=False),
            )
        }
    )
    uid = derive_user_id(keypair.public_key, "alice")
    user_repo = _FakeUserRepo({uid: _FakeUser(username="alice")})
    _make_outbound(
        bus=bus,
        federation=federation,
        space_repo=space_repo,
        user_repo=user_repo,
        identity=(keypair, "inst-self"),
    )

    post = Post(
        id="post-invite",
        author=uid,
        type=PostType.TEXT,
        content="members only",
        created_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))

    federation.broadcast_to_space_members.assert_awaited_once()
    call = federation.broadcast_to_space_members.call_args
    assert call.args[0] == "sp-1"
    assert call.args[1] is FederationEventType.SPACE_POST_CREATED
    payload = call.args[2]
    assert payload["id"] == "post-invite"
    assert payload["content"] == "members only"
    assert payload["author"] == uid


@pytest.mark.parametrize(
    "join_mode", [JoinMode.INVITE_ONLY, JoinMode.REQUEST, JoinMode.OPEN]
)
async def test_join_mode_never_gates_the_relay_hint(join_mode):
    """Readability and membership are independent dials: with subscribers ON
    the hint is shipped under EVERY join mode, including ``invite_only``
    (a broadcast space) — the old model wrongly gated on the join mode."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    keypair = generate_identity_keypair()
    space_repo = _FakeSpaceRepo(
        {
            "sp-1": _FakeSpace(
                space_type=SpaceType.GLOBAL,
                join_mode=join_mode,
                features=_FakeFeatures(allow_subscribers=True),
            )
        }
    )
    uid = derive_user_id(keypair.public_key, "alice")
    user_repo = _FakeUserRepo({uid: _FakeUser(username="alice")})
    _make_outbound(
        bus=bus,
        federation=federation,
        space_repo=space_repo,
        user_repo=user_repo,
        identity=(keypair, "inst-self"),
    )

    post = Post(
        id="post-req",
        author=uid,
        type=PostType.TEXT,
        content="broadcast",
        created_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))

    payload = federation.broadcast_to_space_members.call_args.args[2]
    assert "public_relay" in payload
    assert payload["id"] == "post-req"


async def test_link_preview_rides_inside_the_member_payload_and_its_image_syncs():
    """The author-built card travels inside the (encrypted) member payload —
    receivers never fetch the URL — and its local image is handed to the
    media sync like any other post media."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    federation._own_instance_id = "self-id"
    federation_repo = AsyncMock()
    federation_repo.list_member_instance_ids = AsyncMock(return_value=["peer-a"])
    media_sync = AsyncMock()
    media_sync.enqueue_for_post = AsyncMock()
    _make_outbound(
        bus=bus,
        federation=federation,
        media_sync=media_sync,
        federation_repo=federation_repo,
    )
    card = LinkPreview(
        url="https://example.com/a",
        title="Card",
        thumbnail_url="api/media/lp.webp",
    )
    post = Post(
        id="p-link",
        author="u",
        type=PostType.TEXT,
        content="see https://example.com/a",
        link_preview=card,
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))
    payload = federation.broadcast_to_space_members.call_args.args[2]
    assert payload["link_preview"] == {
        "url": "https://example.com/a",
        "title": "Card",
        "description": None,
        "site_name": None,
        "thumbnail_url": "api/media/lp.webp",
    }
    call = media_sync.enqueue_for_post.call_args
    assert call.kwargs["media_urls"] == ["api/media/lp.webp"]


async def test_post_without_link_preview_has_no_key():
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)
    post = Post(
        id="p-plain",
        author="u",
        type=PostType.TEXT,
        content="hi",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))
    payload = federation.broadcast_to_space_members.call_args.args[2]
    assert "link_preview" not in payload


async def test_post_writes_carry_their_actor():
    """v_42: receivers judge a post write against the space's ``posts``
    access level by who made it — a create's actor is its author, an edit
    or delete names the editor (a moderator, say, not the author)."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)
    post = Post(
        id="post-a",
        author="uid-alice",
        type=PostType.TEXT,
        content="hi",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))
    await bus.publish(PostEdited(post=post, space_id="sp-1", actor_user_id="uid-mod"))
    await bus.publish(
        PostDeleted(post_id="post-a", space_id="sp-1", actor_user_id="uid-admin")
    )
    actors = [
        c.args[2].get("actor_user_id")
        for c in federation.broadcast_to_space_members.call_args_list
    ]
    assert actors == ["uid-alice", "uid-mod", "uid-admin"]


async def test_an_approved_posts_actor_is_its_approver():
    """A post released from the moderation queue was made by its approver
    — receivers check that seat against the space's posts level."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)
    post = Post(
        id="post-q",
        author="uid-bob",
        type=PostType.TEXT,
        content="queued",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    await bus.publish(
        SpacePostCreated(post=post, space_id="sp-1", approved_by="uid-olga")
    )
    payload = federation.broadcast_to_space_members.call_args.args[2]
    assert (payload["author"], payload["actor_user_id"]) == ("uid-bob", "uid-olga")


async def test_a_released_post_names_its_author_and_the_release():
    """v_43: a post released from the queue federates as its AUTHOR's
    (``actor_user_id`` = the submitter) with the approval block naming the
    item and the approver — on any approver household, not just the host."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)
    post = Post(
        id="post-q",
        author="uid-bob",
        type=PostType.TEXT,
        content="reviewed",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    with release_scope("item-1", "uid-mod"):
        await bus.publish(
            SpacePostCreated(post=post, space_id="sp-1", approved_by="uid-mod")
        )
    payload = federation.broadcast_to_space_members.call_args.args[2]
    assert payload["actor_user_id"] == "uid-bob"
    assert payload["moderation"] == {"item_id": "item-1", "approved_by": "uid-mod"}


async def test_an_approved_announce_card_federates_with_its_event_link():
    """The feed card of a reviewed calendar event (#790 concern 4) is a
    queued post linked to the event: once approved it must reach the other
    households — only the bridge's own unreviewed cards stay local."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)
    card = Post(
        id="post-card",
        author="uid-bob",
        type=PostType.EVENT,
        content="Picnic",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        linked_event_id="ev-1",
    )
    with release_scope("item-2", "uid-mod"):
        await bus.publish(
            SpacePostCreated(post=card, space_id="sp-1", approved_by="uid-mod")
        )
    federation.broadcast_to_space_members.assert_awaited_once()
    payload = federation.broadcast_to_space_members.call_args.args[2]
    assert payload["linked_event_id"] == "ev-1"
    assert payload["type"] == "event"
    assert payload["moderation"]["item_id"] == "item-2"


async def test_a_bridge_card_minted_during_an_event_release_stays_local():
    """Approving a calendar EVENT runs the feed bridge on the approving
    household; that card (no ``approved_by``) is the bridge's and must not
    federate — every household's bridge mints its own."""
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    _make_outbound(bus=bus, federation=federation)
    card = Post(
        id="p-bridge",
        author="uid-bob",
        type=PostType.EVENT,
        content="Picnic",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        linked_event_id="ev-1",
    )
    with release_scope("item-3", "uid-mod"):
        await bus.publish(SpacePostCreated(post=card, space_id="sp-1"))
    federation.broadcast_to_space_members.assert_not_awaited()


# ─── v_49: the relay hint carries this household's writer cert ─────────


class _FakeCerts:
    def __init__(self, wire):
        self.wire = wire
        self.asked: list[str] = []

    async def current_own_cert_wire(self, space_id):
        self.asked.append(space_id)
        return self.wire


async def _relay_hint_with(certs):
    bus = EventBus()
    federation = AsyncMock()
    federation.broadcast_to_space_members = AsyncMock()
    keypair = generate_identity_keypair()
    uid = derive_user_id(keypair.public_key, "alice")
    out = _make_outbound(
        bus=bus,
        federation=federation,
        space_repo=_FakeSpaceRepo({"sp-1": _FakeSpace(space_type=SpaceType.PUBLIC)}),
        user_repo=_FakeUserRepo({uid: _FakeUser(username="alice")}),
        identity=(keypair, "inst-self"),
    )
    if certs is not None:
        out.attach_writer_certs(certs)
    post = Post(
        id="post-c",
        author=uid,
        type=PostType.TEXT,
        content="hi",
        created_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc),
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))
    return federation.broadcast_to_space_members.call_args.args[2]["public_relay"]


async def test_relay_hint_carries_our_writer_cert_outside_the_author_sig():
    certs = _FakeCerts({"cert": "ours"})
    relay = await _relay_hint_with(certs)
    assert relay["writer_cert"] == {"cert": "ours"}
    assert certs.asked == ["sp-1"]
    # Unsigned by the author (the cert is self-authenticating), so the
    # author signature verifies exactly as before.
    assert verify_signed_author_inner(relay)


async def test_relay_hint_without_a_cert_has_no_field():
    assert "writer_cert" not in await _relay_hint_with(_FakeCerts(None))
    assert "writer_cert" not in await _relay_hint_with(None)


# ─── v_49: trusted-mode member publish ─────────────────────────────────


class _MemberGfs:
    def __init__(self, *, raises=False):
        self.raises = raises
        self.order: list[str] = []
        self.scheduled: list[tuple] = []

    def schedule_post(self, space_id, author_user_id, inner):
        self.order.append("schedule")
        if self.raises:
            raise RuntimeError("boom")
        self.scheduled.append((space_id, author_user_id, dict(inner)))
        return True


def _member_setup(member_gfs):
    bus = EventBus()
    federation = AsyncMock()
    keypair = generate_identity_keypair()
    uid = derive_user_id(keypair.public_key, "alice")

    async def _broadcast(*a, **kw):
        member_gfs.order.append("broadcast")

    federation.broadcast_to_space_members = AsyncMock(side_effect=_broadcast)
    outbound = _make_outbound(
        bus=bus,
        federation=federation,
        space_repo=_FakeSpaceRepo({"sp-1": _FakeSpace(space_type=SpaceType.PUBLIC)}),
        user_repo=_FakeUserRepo({uid: _FakeUser(username="alice")}),
        identity=(keypair, "inst-self"),
    )
    outbound.attach_member_gfs(member_gfs)
    post = Post(
        id="post-m",
        author=uid,
        type=PostType.TEXT,
        content="member hello",
        created_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
    )
    return bus, federation, post


async def test_the_member_publish_is_scheduled_after_the_broadcast():
    """The broadcast (and so the host's relay) goes out unchanged; our own
    GFS publish is handed to the background publisher afterwards."""
    member = _MemberGfs()
    bus, federation, post = _member_setup(member)
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))
    relay = federation.broadcast_to_space_members.call_args.args[2]["public_relay"]
    assert "gfs_published" not in relay  # no host-dedupe hint any more
    assert member.order == ["broadcast", "schedule"]
    space_id, author, inner = member.scheduled[0]
    assert (space_id, author, inner["post_id"]) == ("sp-1", post.author, "post-m")
    # The GFS copy binds the real item type and its target in the signature;
    # the member-broadcast hint does not.
    assert (inner["item_type"], inner["item_target"]) == ("post", "post-m")
    assert verify_signed_author_inner(inner)
    assert "item_type" not in relay


async def test_a_scheduling_failure_is_swallowed():
    member = _MemberGfs(raises=True)
    bus, federation, post = _member_setup(member)
    await bus.publish(SpacePostCreated(post=post, space_id="sp-1"))
    federation.broadcast_to_space_members.assert_awaited_once()


async def test_a_private_space_post_schedules_nothing():
    member = _MemberGfs()
    bus = EventBus()
    federation = AsyncMock()
    keypair = generate_identity_keypair()
    uid = derive_user_id(keypair.public_key, "alice")
    outbound = _make_outbound(
        bus=bus,
        federation=federation,
        space_repo=_FakeSpaceRepo({"sp-1": _FakeSpace(space_type=SpaceType.PRIVATE)}),
        user_repo=_FakeUserRepo({uid: _FakeUser(username="alice")}),
        identity=(keypair, "inst-self"),
    )
    outbound.attach_member_gfs(member)
    await bus.publish(
        SpacePostCreated(
            post=Post(
                id="p",
                author=uid,
                type=PostType.TEXT,
                content="x",
                created_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
            ),
            space_id="sp-1",
        )
    )
    assert member.scheduled == []
