"""§27.9 release blocker: moderation outcomes reach GFS followers — blind.

Two moderation outcomes travel on the host's authority-signed
``space_post_public`` relay (``space_public_authority``):

* a **removal** — a post or comment removed on the host path (a
  moderator's, an admin's or the author's own delete), with no author
  signature: the authority signature is its only authorizer;
* an **approved post** — a post a seed holder released from the
  moderation queue of a ``MODERATED`` space, relayed under the signature
  its author's household made when it submitted the item.

Driven end to end with real crypto, real SQLite and the real GFS service:

    SpacePublicOutbound (host: encrypt + pad + authority-sign)
        → GfsFederationService.publish_event (real verify, real fan-out)
            → SpacePublicInbound (follower: verify, decrypt, apply)

What is pinned here:

* the follower applies a removal, keeps a tombstone, and a late create
  does not bring the item back;
* an approved post reaches the follower with its real author;
* a forged notice — a household without the space seed, a notice for
  another space, a member's author-signed inner dressed up as a notice —
  changes nothing, and the GFS itself refuses the unsigned one;
* the GFS sees the SAME event type and the SAME cleartext keys as for any
  post, a ciphertext in the same size bucket as a short post, and never the
  item id, the author, the moderator, or the content.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    sign_authority_event,
    strip_authority_sig_fields,
)
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
    pad_json_object,
)
from socialhome.federation.owner_bound_id import SPACE_POST_KIND, mint_owner_bound_id
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.federation import GfsFederationService
from socialhome.global_server.repositories import SqliteGfsFederationRepo
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
from socialhome.services.space_public_inbound import SpacePublicInbound
from socialhome.services.space_public_author import build_signed_author_inner
from socialhome.services.space_public_outbound import SpacePublicOutbound
from socialhome.services.space_writer_cert_service import SpaceWriterCertService

pytestmark = pytest.mark.security

_GFS_MIGRATIONS = (
    Path(__file__).resolve().parent.parent.parent
    / "socialhome/global_server/migrations"
)

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


class _ToGfs:
    """The host's GFS connection, short-circuited onto the real GFS service
    (the HTTP body itself is pinned by ``test_gfs_payload_minimization``)."""

    def __init__(self) -> None:
        self.svc: GfsFederationService | None = None
        self.payloads: list[tuple[str, dict]] = []

    async def publish_space_event(self, *, space_id, event_type, payload) -> int:
        self.payloads.append((event_type, payload))
        assert self.svc is not None
        return len(await self.svc.publish_event(space_id, event_type, payload))


class _Ws:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(self, instance_id: str, frame: dict) -> bool:
        self.sent.append((instance_id, frame))
        return True


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
    to_gfs = _ToGfs()
    outbound = SpacePublicOutbound(
        bus=hbus,
        space_repo=hspaces,
        space_crypto=hcrypto,
        user_repo=husers,
        gfs_service=to_gfs,
    )
    outbound.attach_identity(
        own_instance_id=HOST,
        own_instance_public_key=host_kp.public_key,
        own_identity_seed=host_kp.private_key,
    )
    outbound.attach_writer_certs(certs)
    outbound.wire()

    # ── GFS: real verify + fan-out ──────────────────────────────────────
    gdb = AsyncDatabase(
        tmp_dir / "gfs.db", migrations_dir=_GFS_MIGRATIONS, batch_timeout_ms=10
    )
    await gdb.startup()
    grepo = SqliteGfsFederationRepo(gdb)
    for iid in (HOST, FOLLOWER):
        await grepo.upsert_instance(
            ClientInstance(
                instance_id=iid,
                display_name=iid,
                public_key=generate_identity_keypair().public_key.hex(),
                inbox_url=f"https://{iid}/federation/inbox/x",
                status="active",
                auto_accept=True,
            )
        )
    await grepo.upsert_space(
        GlobalSpace(
            space_id=SPACE_ID,
            owning_instance=HOST,
            name="Reviewed",
            status="active",
            identity_public_key=space_kp.public_key.hex(),
        )
    )
    await grepo.add_subscriber(space_id=SPACE_ID, instance_id=FOLLOWER)
    ws = _Ws()
    to_gfs.svc = GfsFederationService(grepo, ws_registry=ws)

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
    inbound = SpacePublicInbound(
        bus=EventBus(),
        space_repo=fspaces,
        space_crypto=fcrypto,
        space_post_repo=fposts,
    )
    inbound.attach_identity(own_instance_id=FOLLOWER)

    yield SimpleNamespace(
        space_kp=space_kp,
        host_bus=hbus,
        host_user=host_user,
        member_user=member_user,
        member_iid=member_iid,
        member_kp=member_kp,
        to_gfs=to_gfs,
        ws=ws,
        inbound=inbound,
        posts=fposts,
        crypto=hcrypto,
    )
    for db in (hdb, gdb, fdb):
        await db.shutdown()


async def _deliver_last(world) -> dict:
    """Hand the GFS's last fan-out frame to the follower; return it."""
    target, frame = world.ws.sent[-1]
    assert target == FOLLOWER
    await world.inbound.handle(frame)
    return frame


def _assert_blind(frame: dict, *secrets: str) -> None:
    assert set(frame) == {"type", "space_id", "event_type", "payload"}
    # The same event type as any post: the GFS cannot tell a removal or an
    # approval from a post.
    assert frame["event_type"] == AUTHORITY_EVENT_SPACE_POST_PUBLIC
    assert set(frame["payload"]) == {
        "space_id",
        "epoch",
        "encrypted_payload",
        "authority_sig",
        "authority_sig_suite",
    }
    blob = json.dumps(frame)
    for secret in secrets:
        assert secret not in blob, f"leaked to the GFS: {secret!r}"


def _host_post(world, post_id: str = "post-host-1") -> Post:
    return Post(
        id=post_id,
        author=world.host_user,
        type=PostType.TEXT,
        content="short",
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
    )


async def test_a_moderator_removal_reaches_the_follower_blind(world):
    await world.host_bus.publish(
        SpacePostCreated(post=_host_post(world), space_id=SPACE_ID)
    )
    create_frame = await _deliver_last(world)
    assert (await world.posts.get("post-host-1"))[1].deleted is False

    await world.host_bus.publish(
        PostDeleted(
            post_id="post-host-1",
            space_id=SPACE_ID,
            actor_user_id=MODERATOR_ID,
            author_user_id=world.host_user,
        )
    )
    removal_frame = await _deliver_last(world)
    _assert_blind(removal_frame, "post-host-1", MODERATOR_ID, world.host_user)
    # A removal is padded into the same size bucket as a short post.
    assert len(removal_frame["payload"]["encrypted_payload"]) == len(
        create_frame["payload"]["encrypted_payload"]
    )
    _space, row = await world.posts.get("post-host-1")
    assert row.deleted

    # The create arrives again (a second GFS, a retry): still removed.
    await world.inbound.handle(create_frame)
    assert (await world.posts.get("post-host-1"))[1].deleted


async def test_a_removal_that_overtakes_its_create_leaves_a_tombstone(world):
    late = mint_owner_bound_id(
        SPACE_POST_KIND, space_id=SPACE_ID, owner_user_id=world.host_user
    )
    await world.host_bus.publish(
        SpacePostCreated(post=_host_post(world, late), space_id=SPACE_ID)
    )
    create_frame = world.ws.sent[-1][1]
    await world.host_bus.publish(
        PostDeleted(post_id=late, space_id=SPACE_ID, author_user_id=world.host_user)
    )
    await _deliver_last(world)  # removal first
    await world.inbound.handle(create_frame)  # then the create
    _space, row = await world.posts.get(late)
    assert row.deleted


async def test_a_comment_removal_reaches_the_follower(world):
    await world.host_bus.publish(
        SpacePostCreated(post=_host_post(world), space_id=SPACE_ID)
    )
    await _deliver_last(world)
    await world.posts.add_comment(
        Comment(
            id="comment-distinctive-1",
            post_id="post-host-1",
            author=world.member_user,
            type=CommentType.TEXT,
            content="a comment",
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        ),
        space_id=SPACE_ID,
    )
    await world.host_bus.publish(
        CommentDeleted(
            post_id="post-host-1",
            comment_id="comment-distinctive-1",
            space_id=SPACE_ID,
            actor_user_id=MODERATOR_ID,
            author_user_id=world.member_user,
        )
    )
    frame = await _deliver_last(world)
    _assert_blind(frame, "comment-distinctive-1", MODERATOR_ID, world.member_user)
    assert (await world.posts.get_comment("comment-distinctive-1")).deleted


async def _forged_envelope(world, inner: dict, *, seed: bytes) -> dict:
    epoch, ct = await world.crypto.encrypt(SPACE_ID, pad_json_object(inner))
    envelope = {"space_id": SPACE_ID, "epoch": epoch, "encrypted_payload": ct}
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id=SPACE_ID,
            payload=strip_authority_sig_fields(envelope),
            space_seed=seed,
        )
    )
    return envelope


async def test_a_removal_from_a_household_without_the_seed_is_refused(world):
    """It holds the content key (a member) but not the space seed: the GFS
    refuses to relay it, and a follower handed it anyway ignores it."""
    await world.host_bus.publish(
        SpacePostCreated(post=_host_post(world), space_id=SPACE_ID)
    )
    await _deliver_last(world)
    removal = AuthorityRemoval(
        space_id=SPACE_ID, target="post", item_id="post-host-1", post_id="post-host-1"
    ).to_inner()
    forged = await _forged_envelope(
        world, removal, seed=generate_space_keypair().private_key
    )
    with pytest.raises(PermissionError):
        await world.to_gfs.svc.publish_event(
            SPACE_ID, AUTHORITY_EVENT_SPACE_POST_PUBLIC, forged
        )
    await world.inbound.handle(
        {
            "type": "relay",
            "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            "payload": forged,
        }
    )
    assert not (await world.posts.get("post-host-1"))[1].deleted


@pytest.mark.parametrize("case", ["other_space", "author_signed"])
async def test_a_misdirected_or_author_signed_notice_changes_nothing(world, case):
    """Even under a VALID authority signature: a notice naming another space,
    or an inner carrying an author signature (always read as a post) does
    not remove anything."""
    await world.host_bus.publish(
        SpacePostCreated(post=_host_post(world), space_id=SPACE_ID)
    )
    await _deliver_last(world)
    inner = {
        AUTHORITY_KIND_FIELD: AUTHORITY_KIND_REMOVAL,
        "space_id": SPACE_ID,
        "target": "post",
        "item_id": "post-host-1",
        "post_id": "post-host-1",
    }
    if case == "other_space":
        inner["space_id"] = "sp-elsewhere"
    else:
        inner["author_sig"] = "A" * 86
    envelope = await _forged_envelope(world, inner, seed=world.space_kp.private_key)
    await world.inbound.handle(
        {
            "type": "relay",
            "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            "payload": envelope,
        }
    )
    assert not (await world.posts.get("post-host-1"))[1].deleted


def test_a_member_space_item_can_never_carry_a_removal():
    """The member relay names only member item types: a ``space_item`` whose
    real type is an authority kind is refused before anything else."""
    pt = build_item_plaintext(AUTHORITY_KIND_REMOVAL, {"item_id": "post-host-1"})
    assert parse_item_plaintext(pt) is None


def _queued_post(world) -> Post:
    return Post(
        id=mint_owner_bound_id(
            SPACE_POST_KIND, space_id=SPACE_ID, owner_user_id=world.member_user
        ),
        author=world.member_user,
        type=PostType.TEXT,
        content=POST_TEXT,
        created_at=datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc),
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


async def test_an_approved_moderated_post_reaches_the_follower_blind(world):
    """A plain member of a MODERATED space: the host applied the approved
    queue item (its own copy, as the submitter's, with the signed copy the
    member attached at submission) and relays it. The follower shows it
    under the real author — proven by the author's own signature."""
    post = _queued_post(world)
    await world.host_bus.publish(
        SpacePostCreated(
            post=post,
            space_id=SPACE_ID,
            approved_by=MODERATOR_ID,
            public_relay=_submitters_copy(world, post),
        )
    )
    frame = await _deliver_last(world)
    _assert_blind(
        frame, post.id, world.member_user, MEMBER_USERNAME, MODERATOR_ID, POST_TEXT
    )
    space_id, row = await world.posts.get(post.id)
    assert space_id == SPACE_ID
    assert row.author == world.member_user
    assert row.content == POST_TEXT


async def test_a_seed_holder_cannot_attribute_an_approved_post_on_its_own(world):
    """Without the author's signed copy the host relays nothing — and an
    unsigned 'approved post' a seed holder forges is dropped by followers."""
    post = _queued_post(world)
    await world.host_bus.publish(
        SpacePostCreated(post=post, space_id=SPACE_ID, approved_by=MODERATOR_ID)
    )
    assert world.to_gfs.payloads == []
    forged = {
        k: v for k, v in _submitters_copy(world, post).items() if k != "author_sig"
    }
    forged[AUTHORITY_KIND_FIELD] = AUTHORITY_KIND_APPROVED_POST
    envelope = await _forged_envelope(world, forged, seed=world.space_kp.private_key)
    await world.inbound.handle(
        {
            "type": "relay",
            "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            "payload": envelope,
        }
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
                submitted_at=datetime(2026, 6, 11, tzinfo=timezone.utc),
                expires_at=datetime(2026, 6, 18, tzinfo=timezone.utc),
            )
        )
    )
    assert world.to_gfs.payloads == []
    assert world.ws.sent == []
    assert await world.posts.get(post.id) is None
