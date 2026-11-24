"""Tests for the household side of member publish — the opaque channel only.

Since v_96 a household's own space items reach a connection server only
through the space's opaque channel (v_51 private, v_96 global): the
space-keyed ``/gfs/member-publish-anon`` and ``/gfs/spaces/{id}/epoch``
senders are gone. These tests drive :class:`GfsMemberPublishService` against
a recording channel service: what it plans, seals and hands over, the
padded plaintext codec, and the follower-switch gate on a reader cert.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
)
from socialhome.domain.federation import GfsConnection
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.domain.space_item import ITEM_SIZE_BUCKETS
from socialhome.domain.writer_cert import WriterCert
from socialhome.services.gfs_member_publish_service import (
    GfsMemberPublishService,
    _cert_lets,
    build_item_plaintext,
    parse_item_plaintext,
)
from socialhome.writer_cert import bind_writer_users, sign_writer_cert

SPACE_ID = "sp-pub"
AUTHOR = "u-me"
SPACE_SEED = os.urandom(32)
SPACE_PK = ed25519_public_key(SPACE_SEED)


class _Household:
    def __init__(self) -> None:
        self.seed = os.urandom(32)
        self.pk = ed25519_public_key(self.seed)
        self.instance_id = derive_instance_id(self.pk)


class _Crypto:
    """A one-key AES-GCM stand-in for ``SpaceContentEncryption``."""

    def __init__(self, epoch: int = 3) -> None:
        self.epoch = epoch
        self.key = AESGCM.generate_key(bit_length=256)

    async def get_current_epoch(self, space_id):
        return self.epoch

    async def encrypt(self, space_id, plaintext: bytes):
        nonce = os.urandom(12)
        return self.epoch, b64url_encode(
            nonce + AESGCM(self.key).encrypt(nonce, plaintext, None)
        )

    def decrypt(self, ct: str) -> bytes:
        raw = b64url_decode(ct)
        return AESGCM(self.key).decrypt(raw[:12], raw[12:], None)


class _Certs:
    def __init__(self, holder: _Household, *, scope: str = "write") -> None:
        self.holder = holder
        self.scope: str | None = scope
        self.users: list[str] = [AUTHOR]

    async def own_cert(self, space_id, epoch) -> WriterCert | None:
        if self.scope is None:
            return None
        cert = sign_writer_cert(
            space_seed=SPACE_SEED,
            space_id=space_id,
            epoch=epoch,
            instance_pk=self.holder.pk,
            scope=self.scope,
        )
        return bind_writer_users(cert, space_seed=SPACE_SEED, user_ids=self.users)

    async def current_own_cert_wire(self, space_id):
        cert = await self.own_cert(space_id, 3)
        return cert.to_wire() if cert is not None else None


@dataclass
class _Spaces:
    spaces: dict[str, Space] = field(default_factory=dict)

    async def get(self, space_id):
        return self.spaces.get(space_id)


class _Conns:
    def __init__(self, conns: list[GfsConnection]):
        self.conns = conns

    async def get(self, gfs_id):
        return next((c for c in self.conns if c.id == gfs_id), None)


class _Channels:
    """Records what the member publisher hands the channel service."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.sealed: list[tuple[str, int, str, list]] = []
        self.rotations: list[str] = []
        self.announced: list[tuple[str, str | None]] = []
        self.healed: list[str] = []
        self.channel = True

    async def plan(self, space_id):
        return [self.conn]

    async def publish_sealed(self, space_id, epoch, ciphertext, targets):
        self.sealed.append((space_id, epoch, ciphertext, targets))
        return targets

    async def on_rotation(self, space_id):
        self.rotations.append(space_id)
        return 1

    async def announce_epoch(self, space_id, *, only=None):
        self.announced.append((space_id, only))
        return 1

    async def on_rekey_sent(self, space_id):
        self.rotations.append(f"rekey-sent:{space_id}")
        return True

    async def heal(self, gfs_id):
        self.healed.append(gfs_id)
        return 0

    async def has_channel(self, space_id):
        return self.channel

    async def retire(self, space_id):
        self.rotations.append(f"retire:{space_id}")

    async def enable(self, space_id):
        self.rotations.append(f"enable:{space_id}")
        return "created"
        return True


def _space(*, owner: str = "host.home", public=True, readable=True, sid=SPACE_ID):
    return Space(
        id=sid,
        name="Public",
        owner_instance_id=owner,
        owner_username="o",
        identity_public_key=SPACE_PK.hex(),
        config_sequence=0,
        features=SpaceFeatures(allow_subscribers=readable),
        space_type=SpaceType.GLOBAL if public else SpaceType.PRIVATE,
        join_mode=JoinMode.OPEN,
    )


@pytest.fixture
async def world():
    """A member household (``me``) of a global space that runs a channel
    here, and the recording channel service."""
    me, owner = _Household(), _Household()
    conn = GfsConnection(
        id="conn-1",
        gfs_instance_id="gfs-node-a",
        display_name="GFS",
        public_key="00" * 32,
        inbox_url="https://gfs.example",
        status="active",
        paired_at="",
    )
    spaces = _Spaces(spaces={SPACE_ID: _space(owner=owner.instance_id)})
    crypto = _Crypto()
    svc = GfsMemberPublishService(
        conn_repo=_Conns([conn]),  # type: ignore[arg-type]
        space_repo=spaces,  # type: ignore[arg-type]
        space_crypto=crypto,  # type: ignore[arg-type]
        writer_certs=_Certs(me),  # type: ignore[arg-type]
        own_instance_id=me.instance_id,
    )
    channels = _Channels(conn)
    svc.attach_channels(channels)  # type: ignore[arg-type]
    await svc.start()
    yield {
        "svc": svc,
        "me": me,
        "owner": owner,
        "conn": conn,
        "spaces": spaces,
        "crypto": crypto,
        "channels": channels,
    }
    await svc.stop()


def _inner(**extra) -> dict:
    return {"post_id": "p-1", "space_id": SPACE_ID, "content": "hello", **extra}


def _sealed(world) -> list[tuple[str, dict]]:
    """What went into the channel, opened: ``(item_type, inner)`` each."""
    out = []
    for _sid, _epoch, ct, _targets in world["channels"].sealed:
        got = parse_item_plaintext(world["crypto"].decrypt(ct))
        assert got is not None
        out.append(got)
    return out


# ── Plaintext codec ──────────────────────────────────────────────────────


def test_item_plaintext_round_trips_and_refuses_the_rest():
    pt = build_item_plaintext("post", {"a": 1})
    assert parse_item_plaintext(pt) == ("post", {"a": 1})
    for bad in (
        b"x",
        b"[]",
        build_item_plaintext("poll", {}),
        b'{"item_type":"post"}',
    ):
        assert parse_item_plaintext(bad) is None


# ── Planning ─────────────────────────────────────────────────────────────


def test_every_member_item_type_round_trips_the_plaintext_codec():
    for item_type in (
        "post_edit",
        "post_delete",
        "comment",
        "comment_edit",
        "comment_delete",
        "reaction_add",
        "reaction_remove",
    ):
        assert parse_item_plaintext(build_item_plaintext(item_type, {"a": 1})) == (
            item_type,
            {"a": 1},
        )


@pytest.mark.parametrize("size", [0, 10, 900, 1100, 5000, 20000, 70000])
def test_the_item_plaintext_is_padded_to_a_size_bucket(size):
    pt = build_item_plaintext("comment", {"content": "x" * size})
    assert len(pt) in ITEM_SIZE_BUCKETS
    assert parse_item_plaintext(pt) == ("comment", {"content": "x" * size})


def test_a_reaction_and_a_short_post_share_one_bucket():
    reaction = build_item_plaintext("reaction_add", {"emoji": "👍"})
    post = build_item_plaintext("post", {"content": "hello there " * 20})
    assert len(reaction) == len(post)


def test_the_padding_rides_a_field_a_pr2_receiver_ignores():
    """A receiver from before this release reads ``item_type`` / ``inner``
    off a JSON object and ignores other keys — a padded post still parses."""
    body = json.loads(build_item_plaintext("post", {"post_id": "p"}))
    assert set(body) == {"item_type", "inner", "_pad"}
    assert (body["item_type"], body["inner"]) == ("post", {"post_id": "p"})


def test_an_item_past_the_largest_bucket_is_left_unpadded():
    pt = build_item_plaintext("post", {"content": "x" * (ITEM_SIZE_BUCKETS[-1] + 1)})
    assert len(pt) > ITEM_SIZE_BUCKETS[-1]
    assert parse_item_plaintext(pt) is not None


# ── Planning and sealing ─────────────────────────────────────────────────


async def test_a_global_space_with_a_channel_publishes_only_there(world):
    svc = world["svc"]
    assert await svc.channel_space(SPACE_ID)
    targets = await svc.plan_item(SPACE_ID, AUTHOR, "post")
    assert targets == [world["conn"]]
    assert await svc.publish_item(SPACE_ID, "post", _inner(), targets) == targets
    [(item_type, inner)] = _sealed(world)
    # Sealed under the content key with our space writer cert INSIDE.
    assert item_type == "post" and inner["writer_cert"]["space_id"] == SPACE_ID
    assert inner["content"] == "hello"


@pytest.mark.security
async def test_a_global_space_without_a_channel_publishes_nothing(world):
    """v_96 — no channel, no GFS path: the write takes the host path alone
    (no space-keyed member relay any more)."""
    world["channels"].channel = False
    svc = world["svc"]
    assert not await svc.channel_space(SPACE_ID)
    assert await svc.plan_item(SPACE_ID, AUTHOR, "post") == []
    assert await svc.publish_item(SPACE_ID, "post", _inner(), [world["conn"]]) == []
    assert world["channels"].sealed == []


async def test_without_a_channel_service_nothing_is_planned(world):
    svc = GfsMemberPublishService(
        conn_repo=_Conns([world["conn"]]),  # type: ignore[arg-type]
        space_repo=world["spaces"],  # type: ignore[arg-type]
        space_crypto=world["crypto"],  # type: ignore[arg-type]
        writer_certs=_Certs(world["me"]),  # type: ignore[arg-type]
        own_instance_id=world["me"].instance_id,
    )
    assert await svc.plan_item(SPACE_ID, AUTHOR, "post") == []
    assert await svc.publish_item(SPACE_ID, "post", _inner(), [world["conn"]]) == []
    assert await svc.announce_epoch(SPACE_ID) == 0
    assert await svc.announce_held_epochs("conn-1") == 0


async def test_a_public_space_never_touches_the_channel(world):
    """A PUBLIC (not global) space has no channel tier."""
    world["spaces"].spaces[SPACE_ID] = replace(
        world["spaces"].spaces[SPACE_ID], space_type=SpaceType.PUBLIC
    )
    svc = world["svc"]
    assert not await svc.channel_space(SPACE_ID)
    assert await svc.plan_item(SPACE_ID, AUTHOR, "post") == []
    await svc.reconcile_channel(SPACE_ID)
    await svc.enable_channel(SPACE_ID)
    await svc.channel_rekey_sent(SPACE_ID)
    assert await svc.announce_epoch(SPACE_ID) == 0
    assert world["channels"].rotations == []


async def test_the_owner_publishes_its_own_global_post_into_the_channel(world):
    """Q4 — the owner's own post is its household's item in the channel."""
    world["spaces"].spaces[SPACE_ID] = _space(owner=world["me"].instance_id)
    assert await world["svc"].plan_post(SPACE_ID, AUTHOR) == [world["conn"]]


@pytest.mark.parametrize(
    ("item_type", "scope", "planned"),
    [
        ("comment", "comment", True),
        ("comment", "write", True),
        ("reaction_add", "comment", True),
        ("comment_delete", "comment", True),
        # A post edit or delete needs the post's own right.
        ("post_edit", "comment", False),
        ("post_delete", "comment", False),
        ("post_delete", "write", True),
    ],
)
async def test_plan_item_follows_the_scope_the_type_needs(
    world, item_type, scope, planned
):
    svc = world["svc"]
    svc._writer_certs.scope = scope
    targets = await svc.plan_item(SPACE_ID, AUTHOR, item_type)
    assert bool(targets) is planned


async def test_plan_item_needs_the_author_in_the_binding(world):
    world["svc"]._writer_certs.users = ["someone-else"]
    assert await world["svc"].plan_item(SPACE_ID, AUTHOR, "comment") == []


async def test_a_v1_cert_without_a_binding_takes_the_host_path(world, monkeypatch):
    svc = world["svc"]

    async def _v1(space_id):
        return sign_writer_cert(
            space_seed=SPACE_SEED,
            space_id=space_id,
            epoch=3,
            instance_pk=world["me"].pk,
            scope="write",
        ).to_wire()

    monkeypatch.setattr(svc._writer_certs, "current_own_cert_wire", _v1)
    assert await svc.plan_post(SPACE_ID, AUTHOR) == []


async def test_publish_item_refuses_a_type_our_cert_cannot_carry(world):
    world["svc"]._writer_certs.scope = "comment"
    got = await world["svc"].publish_item(
        SPACE_ID, "post_delete", {"x": 1}, [world["conn"]]
    )
    assert got == []
    assert world["channels"].sealed == []


async def test_no_cert_for_the_sealing_epoch_publishes_nothing(world):
    world["svc"]._writer_certs.scope = None
    assert (
        await world["svc"].publish_item(SPACE_ID, "post", _inner(), [world["conn"]])
        == []
    )


async def test_a_moving_epoch_publishes_nothing(world, monkeypatch):
    """The cert binds the epoch it is sealed under: when every attempt
    lands under another epoch, nothing goes."""
    crypto = world["crypto"]

    async def _moving(space_id, plaintext):
        crypto.epoch += 1
        return crypto.epoch, "ct"

    monkeypatch.setattr(crypto, "encrypt", _moving)
    assert (
        await world["svc"].publish_item(SPACE_ID, "post", _inner(), [world["conn"]])
        == []
    )


# ── In the background ────────────────────────────────────────────────────


async def test_schedule_post_publishes_in_the_background(world):
    svc = world["svc"]
    assert svc.schedule_post(SPACE_ID, AUTHOR, _inner())
    await svc.wait_idle()
    assert [t for t, _i in _sealed(world)] == ["post"]


async def test_schedule_item_publishes_in_the_background(world):
    svc = world["svc"]
    assert svc.schedule_item(SPACE_ID, AUTHOR, "reaction_add", {"emoji": "x"})
    await svc.wait_idle()
    assert [t for t, _i in _sealed(world)] == ["reaction_add"]


async def test_schedule_post_for_an_author_without_rights_publishes_nothing(world):
    svc = world["svc"]
    assert svc.schedule_post(SPACE_ID, "u-other", _inner())
    await svc.wait_idle()
    assert world["channels"].sealed == []


async def test_schedule_post_refuses_while_stopping_or_saturated(world, monkeypatch):
    svc = world["svc"]
    await svc.stop()
    assert not svc.schedule_post(SPACE_ID, AUTHOR, _inner())
    await svc.start()
    monkeypatch.setattr(
        "socialhome.services.gfs_member_publish_service.MAX_PENDING_PUBLISHES", 0
    )
    assert not svc.schedule_post(SPACE_ID, AUTHOR, _inner())


async def test_a_failing_background_publish_is_logged(world, monkeypatch, caplog):
    svc = world["svc"]

    async def _boom(self, space_id, author_user_id, item_type):
        raise RuntimeError("down")

    monkeypatch.setattr(GfsMemberPublishService, "plan_item", _boom)
    with caplog.at_level("ERROR"):
        svc.schedule_post(SPACE_ID, AUTHOR, _inner())
        await svc.wait_idle()
    assert "background publish failed" in caplog.text


async def test_on_published_runs_once_a_server_took_the_item(world):
    svc = world["svc"]
    done: list[bool] = []

    async def _done() -> None:
        done.append(True)

    assert svc.schedule_item(
        SPACE_ID, AUTHOR, "reaction_add", {"emoji": "x"}, on_published=_done
    )
    await svc.wait_idle()
    assert done == [True]


async def test_schedule_post_hands_its_on_published_through(world):
    svc = world["svc"]
    done: list[bool] = []

    async def _done() -> None:
        done.append(True)

    assert svc.schedule_post(SPACE_ID, AUTHOR, {"post_id": "p"}, on_published=_done)
    await svc.wait_idle()
    assert done == [True]


async def test_on_published_never_runs_when_no_server_took_it(world):
    svc = world["svc"]
    svc._writer_certs.scope = "comment"  # our cert cannot carry this type
    done: list[bool] = []

    async def _done() -> None:
        done.append(True)

    assert svc.schedule_item(
        SPACE_ID, AUTHOR, "post_delete", {"x": 1}, on_published=_done
    )
    await svc.wait_idle()
    assert done == []


# ── Epochs and the channel lifecycle ─────────────────────────────────────


async def test_epoch_notices_and_heal_go_to_the_channel(world):
    channels = world["channels"]
    svc = world["svc"]
    assert await svc.announce_epoch(SPACE_ID) == 1
    assert channels.rotations == [SPACE_ID]
    assert await svc.announce_epoch(SPACE_ID, only="conn-1") == 1
    assert channels.announced == [(SPACE_ID, "conn-1")]
    await svc.reconcile_channel(SPACE_ID)
    assert channels.rotations == [SPACE_ID, SPACE_ID]
    await svc.announce_held_epochs("conn-1")
    assert channels.healed == ["conn-1"]
    await svc.retire_channel(SPACE_ID)
    assert channels.rotations[-1] == f"retire:{SPACE_ID}"
    await svc.enable_channel(SPACE_ID)
    assert channels.rotations[-1] == f"enable:{SPACE_ID}"
    await svc.channel_rekey_sent(SPACE_ID)
    assert channels.rotations[-1] == f"rekey-sent:{SPACE_ID}"


async def test_a_private_space_publishes_through_its_channel(world):
    world["spaces"].spaces[SPACE_ID] = _space(
        owner=world["owner"].instance_id, public=False, readable=False
    )
    svc = world["svc"]
    targets = await svc.plan_item(SPACE_ID, AUTHOR, "post")
    assert await svc.publish_item(SPACE_ID, "post", _inner(), targets) == targets
    assert [t for t, _i in _sealed(world)] == ["post"]


async def test_a_reconnect_only_heals_the_channels(world):
    """v_96 — a reconnect announces nothing space-keyed (the owned-space
    epoch notice loop is gone): it heals the channels on that server."""
    svc = world["svc"]
    world["spaces"].spaces[SPACE_ID] = _space(owner=world["me"].instance_id)
    await svc.announce_held_epochs("conn-1")
    assert world["channels"].healed == ["conn-1"]
    assert world["channels"].rotations == []
    assert world["channels"].announced == []
    assert await svc.announce_held_epochs("conn-unknown") == 0


async def test_a_failing_heal_is_logged_not_raised(world, monkeypatch, caplog):
    async def _boom(gfs_id):
        raise RuntimeError("down")

    monkeypatch.setattr(world["channels"], "heal", _boom)
    with caplog.at_level("ERROR"):
        assert await world["svc"].announce_held_epochs("conn-1") == 0
    assert "channel heal failed" in caplog.text


@pytest.mark.security
@pytest.mark.parametrize(
    ("item_type", "comment", "react", "ok"),
    [
        ("comment", True, False, True),
        ("comment", False, True, False),
        ("reaction_add", False, True, True),
        ("reaction_add", True, False, False),
        ("post", True, True, False),
    ],
)
def test_a_reader_cert_publishes_only_what_the_follower_switches_allow(
    item_type, comment, react, ok
):
    """v_96 — our follower household's reader cert is not used for an item
    the receivers would refuse under the space's follower switches."""
    seed = os.urandom(32)
    cert = bind_writer_users(
        sign_writer_cert(
            space_seed=seed,
            space_id=SPACE_ID,
            epoch=1,
            instance_pk=ed25519_public_key(os.urandom(32)),
            scope="comment",
            reader=True,
        ),
        space_seed=seed,
        user_ids=[AUTHOR],
    ).to_wire()
    features = SpaceFeatures(
        allow_subscriber_comment=comment, allow_subscriber_react=react
    )
    assert _cert_lets(cert, AUTHOR, item_type, features) is ok
    assert not _cert_lets({"scope": "comment"}, AUTHOR, "comment", features)
