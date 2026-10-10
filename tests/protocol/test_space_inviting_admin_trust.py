"""Release-blocker protocol tests: the delegated admin household that invited
this household is host-equivalent here for content and admin actions — and
for nothing owner-only (owner decision 2026-10-10, migration 0092,
``socialhome/federation/host_equivalence.py``).

A household a delegated admin invited is seated under the REAL owner O. While
the space's ``delegated_admin_authority`` is on and the inviting household
holds a live admin seat, that household's full §25.6 catch-up stream (roster
and bans included) and its admin actions apply like the owner's. Owner-only
settings still come only from O. Another admin household gets the member
rules, and turning delegation off ends the trust.

Real handlers, real SQLite; only the transport is a mock.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import orjson
import pytest

from socialhome.crypto import (
    derive_instance_id,
    ed25519_public_key,
    generate_identity_keypair,
    generate_space_keypair,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.space import SpaceFeatures, SpaceRole
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.private_invite_handler import PrivateSpaceInviteHandler
from socialhome.federation.space_authorship import SpaceAuthorship
from socialhome.federation.sync.space.exporter import serialise_chunk
from socialhome.federation.sync.space.receiver import SpaceSyncReceiver
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteUserRepo,
)
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.services.federation_inbound import SpaceMembershipInboundHandlers
from socialhome.services.federation_inbound_service import FederationInboundService
from socialhome.space_owner_statement import (
    sign_inviter_proof,
    sign_owner_statement,
)
from socialhome.services.space_crypto_service import (
    sign_authority_event,
    strip_authority_sig_fields,
)

pytestmark = pytest.mark.security

SPACE = "sp-trust"
OWNER_SEED = b"\x41" * 32
OWNER = derive_instance_id(ed25519_public_key(OWNER_SEED))
#: A third household with a plain member — what the inviting admin's
#: admin actions are tested on (the owner's own people are protected).
THIRD = derive_instance_id(ed25519_public_key(b"\x44" * 32))
ADMIN_SEED = b"\x42" * 32
ADMIN = derive_instance_id(ed25519_public_key(ADMIN_SEED))
OTHER_SEED = b"\x43" * 32
OTHER_ADMIN = derive_instance_id(ed25519_public_key(OTHER_SEED))
JOINER = "joiner-i"


def _event(event_type, sender: str, payload: dict) -> FederationEvent:
    return FederationEvent(
        msg_id=f"m-{event_type.value}-{sender}",
        event_type=event_type,
        from_instance=sender,
        to_instance=JOINER,
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=SPACE,
    )


def _meta(space_pk_hex: str, **over) -> dict:
    meta = {
        "name": "Shared",
        "owner_instance_id": OWNER,
        "owner_username": "owner",
        "identity_public_key": space_pk_hex,
        "config_sequence": 5,
        "space_type": "private",
        "join_mode": "invite_only",
        "features": {"delegated_admin_authority": True},
        "roster": [
            {"user_id": "u-owner", "instance_id": OWNER, "role": "owner"},
            {"user_id": "u-o2", "instance_id": OWNER, "role": "member"},
            {"user_id": "u-third", "instance_id": THIRD, "role": "member"},
            {"user_id": "u-admin", "instance_id": ADMIN, "role": "admin"},
            {"user_id": "u-other", "instance_id": OTHER_ADMIN, "role": "admin"},
        ],
    }
    meta.update(over)
    return meta


def _proven(space_kp, meta: dict, *, inviter: str | None = None) -> dict:
    """An honest delegated admin's snapshot (migration 0092): the owner's
    signed statement and the inviter's proof that it holds the seed."""
    return {
        **meta,
        "owner_statement": sign_owner_statement(
            space_id=SPACE,
            owner_seed=OWNER_SEED,
            authority_pk_hex=space_kp.public_key.hex(),
            authority_key_epoch=0,
        ),
        "inviter_proof": sign_inviter_proof(
            space_id=SPACE,
            owner_instance_id=OWNER,
            inviter_instance_id=inviter or ADMIN,
            space_seed=space_kp.private_key,
        ),
    }


@pytest.fixture
async def joiner(db):
    """A household the delegated admin ADMIN invited over the mesh."""
    space_kp = generate_space_keypair()
    spaces = SqliteSpaceRepo(db)
    seats = SqliteSpaceRemoteMemberRepo(db)
    handler = PrivateSpaceInviteHandler(
        bus=EventBus(), space_repo=spaces, remote_member_repo=seats
    )
    await handler._on_invite(
        _event(
            FederationEventType.SPACE_PRIVATE_INVITE,
            ADMIN,
            {
                "space_id": SPACE,
                "invite_token": "tkn",
                "invitee_user_id": "u-joiner",
                "inviter_user_id": "u-admin",
                "host_identity_pk": ed25519_public_key(ADMIN_SEED).hex(),
                "space_meta": _proven(space_kp, _meta(space_kp.public_key.hex())),
            },
        )
    )
    # The other admin household is known by key too (e.g. a mesh claim).
    await spaces.add_space_instance(SPACE, OTHER_ADMIN)
    await spaces.set_space_instance_identity_pk(
        SPACE, OTHER_ADMIN, ed25519_public_key(OTHER_SEED).hex()
    )
    inbound = FederationInboundService(
        bus=EventBus(),
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=spaces,
        user_repo=SqliteUserRepo(db),
        space_remote_member_repo=seats,
    )
    inbound._federation_service = SimpleNamespace(own_instance_id=JOINER)
    return SimpleNamespace(
        db=db,
        spaces=spaces,
        seats=seats,
        handler=handler,
        inbound=inbound,
        space_kp=space_kp,
    )


class _B64Crypto:
    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        return 0, base64.urlsafe_b64encode(plaintext).decode("ascii")

    async def decrypt_chunk(self, *, space_id, epoch, sync_id, ciphertext):
        return base64.urlsafe_b64decode(ciphertext)


def _receiver(j) -> SpaceSyncReceiver:
    return SpaceSyncReceiver(
        bus=EventBus(),
        encoder=FederationEncoder(generate_identity_keypair().private_key),
        crypto=_B64Crypto(),  # type: ignore[arg-type]
        federation_repo=SimpleNamespace(get_instance=AsyncMock(return_value=None)),  # type: ignore[arg-type]
        space_repo=j.spaces,
        space_post_repo=SqliteSpacePostRepo(j.db),
        space_task_repo=MagicMock(),
        page_repo=MagicMock(),
        sticky_repo=MagicMock(),
        space_calendar_repo=MagicMock(),
        gallery_repo=MagicMock(),
        authorship=SpaceAuthorship(
            space_repo=j.spaces,
            remote_member_repo=j.seats,
            user_repo=SqliteUserRepo(j.db),
        ),
    )


async def _chunk(seed: bytes, resource: str, records: list[dict]) -> bytes:
    _, ciphertext = await _B64Crypto().encrypt_chunk(
        space_id=SPACE, sync_id="s-1", plaintext=orjson.dumps({"records": records})
    )
    envelope = {
        "sync_id": "s-1",
        "resource": resource,
        "space_id": SPACE,
        "epoch": 0,
        "seq_start": 0,
        "seq_end": 0,
        "is_last": False,
        "encrypted_payload": ciphertext,
    }
    envelope["signatures"] = FederationEncoder(seed).sign_envelope_all(
        orjson.dumps(envelope), suite="ed25519"
    )
    return serialise_chunk(envelope)


_OWNER_POST = {
    "id": "p-owner-side",
    "author": "u-o2",  # a user of the OWNER's household
    "type": "text",
    "content": "written on the owner's household",
    "created_at": "2026-10-01T00:00:00+00:00",
}
_ROSTER = [
    {
        "user_id": "u-joiner",
        "role": "member",
        "joined_at": "2026-10-01T00:00:00+00:00",
    }
]


async def _stream(j, seed: bytes, sender: str) -> tuple[bool, bool]:
    """Stream a roster chunk and a post written on ANOTHER household; report
    which applied."""
    receiver = _receiver(j)
    await receiver.on_chunk(
        await _chunk(seed, "members", _ROSTER), from_instance=sender
    )
    await receiver.on_chunk(
        await _chunk(seed, "posts", [_OWNER_POST]), from_instance=sender
    )
    roster = await j.spaces.get_member(SPACE, "u-joiner") is not None
    post = await SqliteSpacePostRepo(j.db).get("p-owner-side") is not None
    return roster, post


async def test_the_inviting_admins_full_stream_applies_like_the_hosts(joiner):
    space = await joiner.spaces.get(SPACE)
    assert space.owner_instance_id == OWNER
    assert await joiner.spaces.get_invited_by(SPACE) == ADMIN
    assert await _stream(joiner, ADMIN_SEED, ADMIN) == (True, True)


async def test_another_admin_household_gets_the_member_rules(joiner):
    assert await _stream(joiner, OTHER_SEED, OTHER_ADMIN) == (False, False)


async def test_delegation_off_ends_the_inviting_admins_trust(joiner):
    space = await joiner.spaces.get(SPACE)
    await joiner.spaces.save(replace(space, features=SpaceFeatures()))
    assert await _stream(joiner, ADMIN_SEED, ADMIN) == (False, False)


async def test_a_demoted_inviting_admin_is_no_longer_trusted(joiner):
    await joiner.seats.set_role(SPACE, ADMIN, "u-admin", SpaceRole.MEMBER.value)
    assert await _stream(joiner, ADMIN_SEED, ADMIN) == (False, False)


def _role_change(sender: str) -> FederationEvent:
    return _event(
        FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
        sender,
        {
            "space_id": SPACE,
            "user_id": "u-third",
            "instance_id": THIRD,
            "role": SpaceRole.MODERATOR.value,
        },
    )


@pytest.mark.parametrize(
    "sender, delegation, applied",
    [
        (ADMIN, True, True),
        (OTHER_ADMIN, True, False),
        (ADMIN, False, False),
    ],
)
async def test_role_changes_from_the_inviting_admin(
    joiner, sender, delegation, applied
):
    if not delegation:
        space = await joiner.spaces.get(SPACE)
        await joiner.spaces.save(replace(space, features=SpaceFeatures()))
    await joiner.inbound._on_space_member_role_changed(_role_change(sender))
    seat = await joiner.seats.get(SPACE, THIRD, "u-third")
    assert (seat.role == SpaceRole.MODERATOR.value) is applied


@pytest.mark.parametrize("sender, applied", [(ADMIN, True), (OTHER_ADMIN, False)])
async def test_member_removal_from_the_inviting_admin(joiner, sender, applied):
    await joiner.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("joe", "u-joiner", "Joe"),
    )
    await joiner.db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role, joined_at)"
        " VALUES(?, 'u-joiner', 'member', '2026-10-01')",
        (SPACE,),
    )
    await joiner.handler._on_member_removed(
        _event(
            FederationEventType.SPACE_REMOTE_MEMBER_REMOVED,
            sender,
            {"space_id": SPACE, "user_id": "u-joiner"},
        )
    )
    gone = await joiner.spaces.get_member(SPACE, "u-joiner") is None
    assert gone is applied


async def test_owner_only_settings_from_the_inviting_admin_stay_ignored(joiner):
    """Host-equivalent for content — never for the owner's settings."""
    meta = _meta(
        joiner.space_kp.public_key.hex(),
        config_sequence=9,
        name="RenamedByAdmin",
        features={
            "delegated_admin_authority": True,
            "private_gfs": True,
            "gfs_publish_mode": "strict",
        },
    )
    meta.pop("roster")
    meta["config_author_instance"] = ADMIN
    meta.update(
        sign_authority_event(
            event_type="space_config_changed",
            space_id=SPACE,
            payload=strip_authority_sig_fields(meta),
            space_seed=joiner.space_kp.private_key,
        )
    )
    await joiner.inbound._on_space_config_changed(
        _event(
            FederationEventType.SPACE_CONFIG_CHANGED,
            ADMIN,
            {
                "space_id": SPACE,
                "sequence": 9,
                "event_type": "feature_changed",
                "space_meta": meta,
            },
        )
    )
    space = await joiner.spaces.get(SPACE)
    assert space.name == "RenamedByAdmin"
    assert space.features.private_gfs is False
    assert space.features.gfs_publish_mode == "trusted"
    assert space.owner_instance_id == OWNER
    assert await joiner.spaces.get_host_identity_pk(SPACE) is None


async def test_an_owner_invite_records_no_inviting_admin(db):
    spaces = SqliteSpaceRepo(db)
    handler = PrivateSpaceInviteHandler(
        bus=EventBus(),
        space_repo=spaces,
        remote_member_repo=SqliteSpaceRemoteMemberRepo(db),
    )
    await handler._on_invite(
        _event(
            FederationEventType.SPACE_PRIVATE_INVITE,
            OWNER,
            {
                "space_id": SPACE,
                "invite_token": "tkn",
                "invitee_user_id": "u-joiner",
                "inviter_user_id": "u-owner",
                "space_meta": _meta(generate_space_keypair().public_key.hex()),
            },
        )
    )
    assert (await spaces.get(SPACE)).owner_instance_id == OWNER
    assert await spaces.get_invited_by(SPACE) is None


def _ban(event_type, sender: str, user: str = "u-third") -> FederationEvent:
    return _event(
        event_type,
        sender,
        {"space_id": SPACE, "user_id": user, "banned_by": "u-admin"},
    )


@pytest.mark.parametrize(
    "sender, delegation, applied",
    [
        (ADMIN, True, True),
        (OTHER_ADMIN, True, False),
        (ADMIN, False, False),
    ],
)
async def test_live_bans_from_the_inviting_admin(joiner, sender, delegation, applied):
    """Live ``SPACE_MEMBER_BANNED`` / ``_UNBANNED`` follow the same rule as
    the bans in the inviting admin's catch-up stream."""
    if not delegation:
        space = await joiner.spaces.get(SPACE)
        await joiner.spaces.save(replace(space, features=SpaceFeatures()))
    handlers = SpaceMembershipInboundHandlers(
        bus=EventBus(), space_repo=joiner.spaces, seat_repo=joiner.seats
    )
    await handlers._on_banned(_ban(FederationEventType.SPACE_MEMBER_BANNED, sender))
    assert await joiner.spaces.is_banned(SPACE, "u-third") is applied
    # The owner's ban always applies; the unban follows the same rule.
    await handlers._on_banned(_ban(FederationEventType.SPACE_MEMBER_BANNED, OWNER))
    await handlers._on_unbanned(_ban(FederationEventType.SPACE_MEMBER_UNBANNED, sender))
    assert await joiner.spaces.is_banned(SPACE, "u-third") is (not applied)


# ── The owner's own people are off limits to the inviting admin ──────────


async def test_the_inviting_admin_cannot_demote_or_ban_the_owners_people(joiner):
    await joiner.inbound._on_space_member_role_changed(
        _event(
            FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
            ADMIN,
            {
                "space_id": SPACE,
                "user_id": "u-o2",
                "instance_id": OWNER,
                "role": SpaceRole.MODERATOR.value,
            },
        )
    )
    seat = await joiner.seats.get(SPACE, OWNER, "u-o2")
    assert seat.role == SpaceRole.MEMBER.value
    handlers = SpaceMembershipInboundHandlers(
        bus=EventBus(), space_repo=joiner.spaces, seat_repo=joiner.seats
    )
    for user in ("u-o2", "u-owner"):
        await handlers._on_banned(
            _ban(FederationEventType.SPACE_MEMBER_BANNED, ADMIN, user)
        )
        assert await joiner.spaces.is_banned(SPACE, user) is False
    # The owner itself still decides about its people.
    await handlers._on_banned(
        _ban(FederationEventType.SPACE_MEMBER_BANNED, OWNER, "u-o2")
    )
    assert await joiner.spaces.is_banned(SPACE, "u-o2") is True
