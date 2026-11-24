"""§27.9 release blocker: moderation outcomes reach a global space's readers
through its opaque channel — blind.

Two moderation outcomes travel as the host's AUTHORITY items inside the
space's channel (v_96, ``space_public_authority.channel_authority_plaintext``:
the inner signed by the space authority, binding the content epoch):

* a **removal** — a post or comment removed on the host path (a
  moderator's, an admin's or the author's own delete), with no author
  signature: the authority signature is its only authorizer;
* an **approved post** — a post a seed holder released from the
  moderation queue of a ``MODERATED`` space, relayed under the signature
  its author's household made when it submitted the item.

Driven end to end with real crypto and real SQLite:

    SpacePublicOutbound (host: seal into the channel, authority-signed inner)
        → the channel frame (what a connection server sees and relays)
            → SpacePublicInbound.handle_channel_item (reader: verify, apply)

What is pinned here:

* the reader applies a removal, keeps a tombstone, and a late create does
  not bring the item back;
* an approved post reaches the reader with its real author;
* a forged item — one signed without the space seed, one for another space,
  a member's author-signed inner dressed up as a notice — changes nothing;
* what the server sees is the generic channel frame: one ciphertext in the
  same size bucket as a short post, never the item id, the author, the
  moderator, or the content.

(Before v_96 this rode the space-keyed ``space_post_public`` relay and the
GFS verified the authority signature itself; the channel's server sees only
ciphertext under the channel writer key, so the reader's check is the gate.)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from socialhome.crypto import (
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    generate_space_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    CommentDeleted,
    PostDeleted,
    SpaceModerationRejected,
    SpacePostCreated,
)
from socialhome.domain.gfs_channel import CHANNEL_FRAME_KEYS
from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatureAccess,
    SpaceFeatures,
    SpaceModerationItem,
    SpaceType,
)
from socialhome.domain.space_item import (
    AUTHORITY_KIND_APPROVED_POST,
    AUTHORITY_KIND_FIELD,
    AUTHORITY_KIND_REMOVAL,
    AuthorityRemoval,
)
from socialhome.federation.owner_bound_id import (
    SPACE_COMMENT_KIND,
    SPACE_POST_KIND,
    mint_owner_bound_id,
)
from socialhome.federation.pending_parent_buffer import PendingParentBuffer
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.gfs_member_publish_service import (
    build_item_plaintext,
    parse_item_plaintext,
)
from socialhome.services.space_crypto_service import SpaceContentEncryption
from socialhome.services.space_public_author import build_signed_author_inner
from socialhome.services.space_public_authority import channel_authority_plaintext
from socialhome.services.space_public_inbound import SpacePublicInbound
from socialhome.services.space_public_outbound import SpacePublicOutbound
from socialhome.services.space_writer_cert_service import SpaceWriterCertService

pytestmark = pytest.mark.security

SPACE_ID = "sp-moderated-distinctive"
#: The host household: its instance id is its key's fingerprint, as the
#: relay paths require of every origin.
_HOST_KP = generate_identity_keypair()
HOST = derive_instance_id(_HOST_KP.public_key)
FOLLOWER = "follower-household-distinctive.example"
HOST_USERNAME = "hostuser-distinctive"
MEMBER_USERNAME = "member-distinctive-bob"
MODERATOR_ID = "moderator-user-distinctive"
POST_TEXT = "distinctive-text-under-review"


class _Seats:
    """The host's roster view of remote households: ``iid → [roles]``."""

    def __init__(self, seats: dict[str, list[str]]):
        self.seats = seats

    async def list_for_instance(
        self, space_id, instance_id, *, include_tombstoned=True
    ):
        return [
            SimpleNamespace(role=r, user_id=f"{instance_id}-u{i}")
            for i, r in enumerate(self.seats.get(instance_id, []))
        ]


class _PeerKeys:
    def __init__(self, pks: dict[str, bytes]):
        self.pks = pks

    async def peer_identity_public_key(self, iid):
        return self.pks.get(iid)

    async def peer_supports(self, iid, *, min_version):
        return True


CHANNEL_ID = "c" * 32


class _Channel:
    """The host's channel service, recording what goes into the channel as
    the frame a connection server relays (``CHANNEL_FRAME_KEYS``)."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def has_channel(self, space_id: str) -> bool:
        return space_id == SPACE_ID

    async def publish_current(self, space_id: str, epoch: int, ct: str) -> int:
        self.frames.append(
            {
                "channel_id": CHANNEL_ID,
                "event_type": "space_item",
                "epoch": epoch,
                "payload": ct,
            }
        )
        return 1


def _space(space_kp) -> Space:
    return Space(
        id=SPACE_ID,
        name="Reviewed",
        owner_instance_id=HOST,
        owner_username=HOST_USERNAME,
        identity_public_key=space_kp.public_key.hex(),
        config_sequence=0,
        features=SpaceFeatures(
            allow_subscribers=True, posts_access=SpaceFeatureAccess.MODERATED
        ),
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
    )


@pytest.fixture
async def world(tmp_dir):
    space_kp = generate_space_keypair()
    host_kp = _HOST_KP
    member_kp = generate_identity_keypair()
    member_iid = derive_instance_id(member_kp.public_key)
    member_user = derive_user_id(member_kp.public_key, MEMBER_USERNAME)
    host_user = derive_user_id(host_kp.public_key, HOST_USERNAME)

    # ── Host: the seed holder ───────────────────────────────────────────
    (tmp_dir / "host").mkdir()
    hdb = AsyncDatabase(tmp_dir / "host" / "hfs.db", batch_timeout_ms=10)
    await hdb.startup()
    hkek = KeyManager.from_data_dir(tmp_dir / "host")
    hspaces = SqliteSpaceRepo(hdb, key_manager=hkek)
    hkeys = SqliteSpaceKeyRepo(hdb)
    hcrypto = SpaceContentEncryption(hkeys, hkek, own_instance_id=HOST)
    husers = SqliteUserRepo(hdb)
    await hdb.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES(?, ?, 'Host', 'active')",
        (host_user, HOST_USERNAME),
    )
    await hspaces.save(_space(space_kp))
    await hspaces.set_space_seed(SPACE_ID, space_kp.private_key)
    await hcrypto.initialise_for_space(SPACE_ID)
    certs = SpaceWriterCertService(
        space_repo=hspaces,
        remote_member_repo=_Seats({member_iid: ["member"]}),
        space_key_repo=hkeys,
        own_instance_id=HOST,
        own_identity_pk=host_kp.public_key,
    )
    certs.attach_federation(_PeerKeys({member_iid: member_kp.public_key}))
    hbus = EventBus()
    channel = _Channel()
    outbound = SpacePublicOutbound(
        bus=hbus, space_repo=hspaces, space_crypto=hcrypto, user_repo=husers
    )
    outbound.attach_identity(own_instance_id=HOST)
    outbound.attach_writer_certs(certs)
    outbound.attach_channels(channel)  # type: ignore[arg-type]
    outbound.wire()

    # ── Follower: mirrored space, the content key, the real consumer ────
    (tmp_dir / "follower").mkdir()
    fdb = AsyncDatabase(tmp_dir / "follower" / "hfs.db", batch_timeout_ms=10)
    await fdb.startup()
    fkek = KeyManager.from_data_dir(tmp_dir / "follower")
    fspaces = SqliteSpaceRepo(fdb, key_manager=fkek)
    fcrypto = SpaceContentEncryption(
        SqliteSpaceKeyRepo(fdb), fkek, own_instance_id=FOLLOWER
    )
    await fspaces.save(_space(space_kp))
    exported = await hcrypto.export_current_key(SPACE_ID)
    assert exported is not None
    await fcrypto.import_key(SPACE_ID, exported[0], exported[1])
    fposts = SqliteSpacePostRepo(fdb)
    fbus = EventBus()
    inbound = SpacePublicInbound(
        bus=fbus,
        space_repo=fspaces,
        space_crypto=fcrypto,
        space_post_repo=fposts,
    )
    inbound.attach_identity(own_instance_id=FOLLOWER)
    inbound.attach_writer_certs(
        SpaceWriterCertService(
            space_repo=fspaces,
            remote_member_repo=_Seats({}),
            space_key_repo=SqliteSpaceKeyRepo(fdb),
            own_instance_id=FOLLOWER,
            own_identity_pk=generate_identity_keypair().public_key,
        )
    )
    # An item (a removal included) that overtakes its post waits for it.
    pending = PendingParentBuffer()
    pending.attach_bus(fbus)
    inbound.attach_pending_parents(pending)

    yield SimpleNamespace(
        space_kp=space_kp,
        host_bus=hbus,
        host_user=host_user,
        member_user=member_user,
        member_iid=member_iid,
        member_kp=member_kp,
        channel=channel,
        inbound=inbound,
        posts=fposts,
        crypto=hcrypto,
        pending=pending,
    )
    for db in (hdb, fdb):
        await db.shutdown()


async def _deliver(world, frame: dict) -> None:
    await world.inbound.handle_channel_item(
        SPACE_ID, epoch=frame["epoch"], payload=frame["payload"]
    )


async def _deliver_last(world) -> dict:
    """Hand the channel's last frame to the reader; return it."""
    frame = world.channel.frames[-1]
    await _deliver(world, frame)
    return frame


def _assert_blind(frame: dict, *secrets: str) -> None:
    # The generic channel frame: the server cannot tell a removal or an
    # approval from any member's item.
    assert set(frame) == CHANNEL_FRAME_KEYS
    assert frame["event_type"] == "space_item"
    blob = json.dumps(frame)
    for secret in secrets:
        assert secret not in blob, f"leaked to the GFS: {secret!r}"


def _queued_post(world, post_id: str | None = None) -> Post:
    return Post(
        id=post_id
        or mint_owner_bound_id(
            SPACE_POST_KIND, space_id=SPACE_ID, owner_user_id=world.member_user
        ),
        author=world.member_user,
        type=PostType.TEXT,
        content=POST_TEXT,
        created_at=datetime(2026, 6, 11, 9, 0, tzinfo=UTC),
    )


def _submitters_copy(world, post: Post) -> dict:
    """What the member's household signed when it submitted the item."""
    return build_signed_author_inner(
        post=post,
        space_id=SPACE_ID,
        author_username=MEMBER_USERNAME,
        author_pk=world.member_kp.public_key,
        author_identity_seed=world.member_kp.private_key,
        origin_instance_id=world.member_iid,
    )


async def _create(world, post_id: str | None = None) -> tuple[Post, dict]:
    """A member's post released from the moderation queue on the host: the
    host puts it into the channel as an approved-post authority item.
    Returns the post and its channel frame (not yet delivered)."""
    post = _queued_post(world, post_id)
    await world.host_bus.publish(
        SpacePostCreated(
            post=post,
            space_id=SPACE_ID,
            approved_by=MODERATOR_ID,
            public_relay=_submitters_copy(world, post),
        )
    )
    return post, world.channel.frames[-1]


async def test_a_moderator_removal_reaches_the_follower_blind(world):
    post, create_frame = await _create(world)
    await _deliver(world, create_frame)
    assert (await world.posts.get(post.id))[1].deleted is False

    await world.host_bus.publish(
        PostDeleted(
            post_id=post.id,
            space_id=SPACE_ID,
            actor_user_id=MODERATOR_ID,
            author_user_id=world.member_user,
        )
    )
    removal_frame = await _deliver_last(world)
    _assert_blind(removal_frame, post.id, MODERATOR_ID, world.member_user)
    # A removal is padded into the same size bucket as any short member item
    # on the channel (a reaction, a one-line comment).
    _epoch, short_item = await world.crypto.encrypt(
        SPACE_ID,
        build_item_plaintext("reaction_add", {"emoji": "x", "post_id": post.id}),
    )
    assert len(removal_frame["payload"]) == len(short_item)
    _space, row = await world.posts.get(post.id)
    assert row.deleted

    # The create arrives again (a second server, a retry): still removed.
    await _deliver(world, create_frame)
    assert (await world.posts.get(post.id))[1].deleted


async def test_a_removal_that_overtakes_its_create_leaves_a_tombstone(world):
    post, create_frame = await _create(world)
    await world.host_bus.publish(
        PostDeleted(
            post_id=post.id, space_id=SPACE_ID, author_user_id=world.member_user
        )
    )
    await _deliver_last(world)  # removal first
    await _deliver(world, create_frame)  # then the create
    _space, row = await world.posts.get(post.id)
    assert row.deleted


async def test_a_comment_removal_reaches_the_follower(world):
    post, create_frame = await _create(world)
    await _deliver(world, create_frame)
    await world.posts.add_comment(
        Comment(
            id="comment-distinctive-1",
            post_id=post.id,
            author=world.member_user,
            type=CommentType.TEXT,
            content="a comment",
            created_at=datetime(2026, 6, 10, tzinfo=UTC),
        ),
        space_id=SPACE_ID,
    )
    await world.host_bus.publish(
        CommentDeleted(
            post_id=post.id,
            comment_id="comment-distinctive-1",
            space_id=SPACE_ID,
            actor_user_id=MODERATOR_ID,
            author_user_id=world.member_user,
        )
    )
    frame = await _deliver_last(world)
    _assert_blind(frame, "comment-distinctive-1", MODERATOR_ID, world.member_user)
    assert (await world.posts.get_comment("comment-distinctive-1")).deleted


async def test_a_comment_removal_that_overtakes_its_post_still_removes(world):
    """The removal is held until the post lands, then leaves the tombstone —
    so the comment, arriving after both, never shows."""
    cid = mint_owner_bound_id(
        SPACE_COMMENT_KIND, space_id=SPACE_ID, owner_user_id=world.member_user
    )
    post, create_frame = await _create(world)
    await world.host_bus.publish(
        CommentDeleted(
            post_id=post.id,
            comment_id=cid,
            space_id=SPACE_ID,
            actor_user_id=MODERATOR_ID,
            author_user_id=world.member_user,
        )
    )
    await _deliver_last(world)  # the comment removal first
    assert len(world.pending) == 1
    assert await world.posts.get_comment(cid) is None
    await _deliver(world, create_frame)  # then the post
    assert len(world.pending) == 0
    tombstone = await world.posts.get_comment(cid)
    assert tombstone is not None and tombstone.deleted
    # The comment itself, arriving last, is a duplicate of the tombstone.
    assert not await world.posts.add_comment(
        Comment(
            id=cid,
            post_id=post.id,
            author=world.member_user,
            type=CommentType.TEXT,
            content="back?",
            created_at=datetime(2026, 6, 10, tzinfo=UTC),
        ),
        space_id=SPACE_ID,
    )
    assert (await world.posts.get_comment(cid)).deleted


async def _forged_item(world, inner: dict, *, seed: bytes) -> dict:
    """A channel authority item sealed under the real content key, signed
    with *seed* (the space seed, or a forger's)."""
    epoch = await world.crypto.get_current_epoch(SPACE_ID)
    plain = channel_authority_plaintext(
        inner, space_id=SPACE_ID, epoch=epoch, space_seed=seed
    )
    sealed_epoch, ct = await world.crypto.encrypt(SPACE_ID, plain)
    return {"epoch": sealed_epoch, "payload": ct}


async def test_a_removal_from_a_household_without_the_seed_is_refused(world):
    """It holds the content key (a member) but not the space seed: a reader
    handed its removal ignores it."""
    post, create_frame = await _create(world)
    await _deliver(world, create_frame)
    removal = AuthorityRemoval(
        space_id=SPACE_ID, target="post", item_id=post.id, post_id=post.id
    ).to_inner()
    forged = await _forged_item(
        world, removal, seed=generate_space_keypair().private_key
    )
    await _deliver(world, forged)
    assert not (await world.posts.get(post.id))[1].deleted


@pytest.mark.parametrize("case", ["other_space", "author_signed"])
async def test_a_misdirected_or_author_signed_notice_changes_nothing(world, case):
    """Even under a VALID authority signature: a notice naming another space,
    or an inner carrying an author signature (always read as a post) does
    not remove anything."""
    post, create_frame = await _create(world)
    await _deliver(world, create_frame)
    inner = {
        AUTHORITY_KIND_FIELD: AUTHORITY_KIND_REMOVAL,
        "space_id": SPACE_ID,
        "target": "post",
        "item_id": post.id,
        "post_id": post.id,
    }
    if case == "other_space":
        inner["space_id"] = "sp-elsewhere"
    else:
        inner["author_sig"] = "A" * 86
    await _deliver(
        world, await _forged_item(world, inner, seed=world.space_kp.private_key)
    )
    assert not (await world.posts.get(post.id))[1].deleted


async def test_an_authority_item_signed_for_another_epoch_is_refused(world):
    """The authority signature binds the content epoch the item is sealed
    under: a removal signed for another epoch changes nothing."""
    post, create_frame = await _create(world)
    await _deliver(world, create_frame)
    removal = AuthorityRemoval(
        space_id=SPACE_ID, target="post", item_id=post.id, post_id=post.id
    ).to_inner()
    epoch = await world.crypto.get_current_epoch(SPACE_ID)
    plain = channel_authority_plaintext(
        removal,
        space_id=SPACE_ID,
        epoch=epoch + 7,
        space_seed=world.space_kp.private_key,
    )
    sealed_epoch, ct = await world.crypto.encrypt(SPACE_ID, plain)
    await _deliver(world, {"epoch": sealed_epoch, "payload": ct})
    assert not (await world.posts.get(post.id))[1].deleted


def test_a_member_space_item_can_never_carry_a_removal():
    """The member relay names only member item types: a ``space_item`` whose
    real type is an authority kind is refused before anything else."""
    pt = build_item_plaintext(AUTHORITY_KIND_REMOVAL, {"item_id": "post-host-1"})
    assert parse_item_plaintext(pt) is None


async def test_an_approved_moderated_post_reaches_the_follower_blind(world):
    """A plain member of a MODERATED space: the host applied the approved
    queue item (its own copy, as the submitter's, with the signed copy the
    member attached at submission) and puts it into the channel. The reader
    shows it under the real author — proven by the author's own
    signature."""
    post, frame = await _create(world)
    await _deliver(world, frame)
    _assert_blind(
        frame, post.id, world.member_user, MEMBER_USERNAME, MODERATOR_ID, POST_TEXT
    )
    space_id, row = await world.posts.get(post.id)
    assert space_id == SPACE_ID
    assert row.author == world.member_user
    assert row.content == POST_TEXT


async def test_a_seed_holder_cannot_attribute_an_approved_post_on_its_own(world):
    """Without the author's signed copy the host relays nothing — and an
    unsigned 'approved post' a seed holder forges is dropped by readers."""
    post = _queued_post(world)
    await world.host_bus.publish(
        SpacePostCreated(post=post, space_id=SPACE_ID, approved_by=MODERATOR_ID)
    )
    assert world.channel.frames == []
    forged = {
        k: v for k, v in _submitters_copy(world, post).items() if k != "author_sig"
    }
    forged[AUTHORITY_KIND_FIELD] = AUTHORITY_KIND_APPROVED_POST
    await _deliver(
        world, await _forged_item(world, forged, seed=world.space_kp.private_key)
    )
    assert await world.posts.get(post.id) is None


async def test_an_unapproved_or_rejected_post_never_reaches_the_gfs(world):
    post = _queued_post(world)
    # A remote member's post applied here without an approval: never relayed.
    await world.host_bus.publish(SpacePostCreated(post=post, space_id=SPACE_ID))
    # A rejection publishes nothing to the GFS either.
    await world.host_bus.publish(
        SpaceModerationRejected(
            item=SpaceModerationItem(
                id="q-1",
                space_id=SPACE_ID,
                feature="posts",
                action="create",
                submitted_by=world.member_user,
                payload={"post_id": post.id, "content": POST_TEXT},
                current_snapshot=None,
                submitted_at=datetime(2026, 6, 11, tzinfo=UTC),
                expires_at=datetime(2026, 6, 18, tzinfo=UTC),
            )
        )
    )
    assert world.channel.frames == []
    assert await world.posts.get(post.id) is None


async def test_a_space_without_a_channel_sends_nothing(world):
    """v_96 — no channel, no GFS path at all: the host relays nothing (the
    space-keyed relay is gone); members are reached by federation."""

    class _NoChannel(_Channel):
        async def has_channel(self, space_id: str) -> bool:
            return False

    world.channel.__class__ = _NoChannel
    post = _queued_post(world)
    await world.host_bus.publish(
        SpacePostCreated(
            post=post,
            space_id=SPACE_ID,
            approved_by=MODERATOR_ID,
            public_relay=_submitters_copy(world, post),
        )
    )
    await world.host_bus.publish(
        PostDeleted(
            post_id=post.id, space_id=SPACE_ID, author_user_id=world.member_user
        )
    )
    assert world.channel.frames == []
