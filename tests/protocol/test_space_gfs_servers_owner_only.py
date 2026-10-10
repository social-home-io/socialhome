"""Release-blocker protocol tests: a space's GFS list (``gfs_servers``,
migration 0092) is the OWNER household's alone, on every household.

Members will publish to every GFS on that list — including servers their own
household never connected to — so whoever could write it could aim every
member's publishes at a server of its choosing. The list is signed by the
owner household's identity key and taken only when that signature verifies
(``derive_instance_id(owner_pk) == owner_instance_id``) and it is newer than
the stored one. These tests drive every path that carries it:

* the authority-signed ``SPACE_CONFIG_CHANGED`` a seed-holding delegated
  admin broadcasts — its own list is ignored, the owner's list it relays is
  taken, a replayed older owner list is ignored;
* a config edit a remote admin forwards to the host (runs AS the owner);
* the §D1b join snapshots a delegated admin household sends (private invite
  and invite-link redeem ACK): the stub is seated under the REAL owner, the
  admin's planted list is ignored, the owner's relayed list is taken, and the
  owner's later config still reaches the joiner — while the §25.6 catch-up
  stream that admin sends still verifies (against the admin's own key, never
  as the host's) and its members' content applies.

Driven through the real handlers / services with real SQLite repos; only the
federation transport is a mock.
"""

from __future__ import annotations

import base64
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
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.gfs_server import GfsServer, SignedGfsServerList
from socialhome.domain.space import (
    RemoteAdminOutcome,
    SpaceFeatures,
    SpaceRole,
)
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.space_authorship import SpaceAuthorship
from socialhome.federation.sync.space.exporter import serialise_chunk
from socialhome.federation.sync.space.receiver import SpaceSyncReceiver
from socialhome.federation.invite_token_redeem import (
    SpaceInviteTokenRedeemCoordinator,
)
from socialhome.federation.private_invite_handler import PrivateSpaceInviteHandler
from socialhome.gfs_server_list_sig import sign_gfs_server_list
from socialhome.space_owner_statement import (
    sign_inviter_proof,
    sign_owner_statement,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteUserRepo,
)
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.services.federation_inbound_service import FederationInboundService
from socialhome.services.space_crypto_service import (
    sign_authority_event,
    strip_authority_sig_fields,
)
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService

pytestmark = pytest.mark.security

SPACE = "sp-g"
OWNER_SEED = b"\x31" * 32
OWNER = derive_instance_id(ed25519_public_key(OWNER_SEED))
ADMIN_SEED = b"\x32" * 32
ADMIN = derive_instance_id(ed25519_public_key(ADMIN_SEED))
JOINER = "joiner-i"


def _list(gid: str, *, issued_at: int, seed: bytes = OWNER_SEED) -> SignedGfsServerList:
    return sign_gfs_server_list(
        space_id=SPACE,
        servers=(
            GfsServer(
                gfs_instance_id=gid,
                inbox_url=f"https://{gid}.example",
                public_key=(gid.encode().hex() * 32)[:64],
                gfs_key_suite="ed25519",
                display_name=gid,
            ),
        ),
        issued_at=issued_at,
        owner_seed=seed,
    )


#: The owner's list, and a list the ADMIN signed with its own key.
OWNER_LIST = _list("gfs-owner", issued_at=20)
ADMIN_LIST = _list("gfs-evil", issued_at=99, seed=ADMIN_SEED)


def _meta(space_pk_hex: str, *, gfs: dict | None, **over) -> dict:
    meta = {
        "name": "Global",
        "owner_instance_id": OWNER,
        "owner_username": "owner",
        "identity_public_key": space_pk_hex,
        "config_sequence": 5,
        "space_type": "global",
        "join_mode": "invite_only",
        "features": {},
    }
    if gfs is not None:
        meta["gfs_servers"] = gfs
    meta.update(over)
    return meta


def _proven(kp, meta: dict) -> dict:
    """What an honest delegated admin's snapshot carries (migration 0092):
    the owner's signed statement for this space and key, and the admin's
    proof that it holds the space seed."""
    return {
        **meta,
        "owner_statement": sign_owner_statement(
            space_id=SPACE,
            owner_seed=OWNER_SEED,
            authority_pk_hex=kp.public_key.hex(),
            authority_key_epoch=0,
        ),
        "inviter_proof": sign_inviter_proof(
            space_id=SPACE,
            owner_instance_id=OWNER,
            inviter_instance_id=ADMIN,
            space_seed=kp.private_key,
        ),
    }


def _signed(meta: dict, *, author: str, space_seed: bytes) -> dict:
    meta = {**meta, "config_author_instance": author}
    meta.update(
        sign_authority_event(
            event_type="space_config_changed",
            space_id=SPACE,
            payload=strip_authority_sig_fields(meta),
            space_seed=space_seed,
        )
    )
    return meta


def _event(event_type, from_instance: str, payload: dict) -> FederationEvent:
    return FederationEvent(
        msg_id=f"m-{event_type.value}-{from_instance}",
        event_type=event_type,
        from_instance=from_instance,
        to_instance=JOINER,
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=SPACE,
    )


def _inbound(db, own: str) -> FederationInboundService:
    inbound = FederationInboundService(
        bus=EventBus(),
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        space_remote_member_repo=SqliteSpaceRemoteMemberRepo(db),
    )
    inbound._federation_service = SimpleNamespace(own_instance_id=own)
    return inbound


async def _config(db, inbound, *, from_instance: str, meta: dict) -> None:
    await inbound._on_space_config_changed(
        _event(
            FederationEventType.SPACE_CONFIG_CHANGED,
            from_instance,
            {
                "space_id": SPACE,
                "sequence": meta["config_sequence"],
                "event_type": "feature_changed",
                "space_meta": meta,
            },
        )
    )


# ── Inbound SPACE_CONFIG_CHANGED ──────────────────────────────────────────


async def _seed_mirror(db, space_pk_hex: str, *, stored: SignedGfsServerList | None):
    repo = SqliteSpaceRepo(db)
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type, join_mode, config_sequence)"
        " VALUES(?,?,?,?,?,'global','invite_only',5)",
        (SPACE, "Global", OWNER, "owner", space_pk_hex),
    )
    if stored is not None:
        await repo.set_gfs_servers(SPACE, stored)
    return repo


@pytest.mark.parametrize("we_host", [True, False])
async def test_a_delegated_admins_own_gfs_list_is_ignored(db, we_host):
    kp = generate_space_keypair()
    stored = _list("gfs-a", issued_at=10)
    repo = await _seed_mirror(db, kp.public_key.hex(), stored=stored)
    meta = _signed(
        _meta(
            kp.public_key.hex(),
            gfs=ADMIN_LIST.to_wire(),
            name="RenamedByAdmin",
            config_sequence=9,
        ),
        author=ADMIN,
        space_seed=kp.private_key,
    )
    await _config(
        db, _inbound(db, OWNER if we_host else JOINER), from_instance=ADMIN, meta=meta
    )
    space = await repo.get(SPACE)
    assert space is not None
    assert space.name == "RenamedByAdmin"  # the rest of the block applied
    assert space.gfs_servers == stored


async def test_the_owners_list_relayed_by_an_admin_is_taken(db):
    kp = generate_space_keypair()
    repo = await _seed_mirror(db, kp.public_key.hex(), stored=_list("a", issued_at=1))
    meta = _signed(
        _meta(kp.public_key.hex(), gfs=OWNER_LIST.to_wire(), config_sequence=9),
        author=ADMIN,
        space_seed=kp.private_key,
    )
    await _config(db, _inbound(db, JOINER), from_instance=ADMIN, meta=meta)
    space = await repo.get(SPACE)
    assert space is not None and space.gfs_servers == OWNER_LIST


async def test_a_replayed_older_owner_list_is_ignored(db):
    kp = generate_space_keypair()
    repo = await _seed_mirror(db, kp.public_key.hex(), stored=OWNER_LIST)
    older = _list("gfs-old", issued_at=OWNER_LIST.issued_at - 1)
    meta = _signed(
        _meta(kp.public_key.hex(), gfs=older.to_wire(), config_sequence=9),
        author=ADMIN,
        space_seed=kp.private_key,
    )
    await _config(db, _inbound(db, JOINER), from_instance=ADMIN, meta=meta)
    space = await repo.get(SPACE)
    assert space is not None and space.gfs_servers == OWNER_LIST


# ── Join snapshots sent by a delegated admin household ────────────────────


def _invite(meta: dict) -> FederationEvent:
    return _event(
        FederationEventType.SPACE_PRIVATE_INVITE,
        ADMIN,
        {
            "space_id": SPACE,
            "invite_token": "tkn",
            "invitee_user_id": "u-joiner",
            "inviter_user_id": "u-admin",
            "space_meta": meta,
        },
    )


def _invite_handler(db) -> PrivateSpaceInviteHandler:
    return PrivateSpaceInviteHandler(
        bus=EventBus(),
        space_repo=SqliteSpaceRepo(db),
        remote_member_repo=SqliteSpaceRemoteMemberRepo(db),
    )


@pytest.mark.parametrize(
    "gfs, expected",
    [
        (ADMIN_LIST.to_wire(), None),  # planted by the admin → ignored
        (OWNER_LIST.to_wire(), OWNER_LIST),  # the owner's, relayed → taken
    ],
)
async def test_an_admin_invite_seats_the_real_owner_and_only_the_owners_list(
    db, gfs, expected
):
    kp = generate_space_keypair()
    await _invite_handler(db)._on_invite(
        _invite(_proven(kp, _meta(kp.public_key.hex(), gfs=gfs)))
    )
    space = await SqliteSpaceRepo(db).get(SPACE)
    assert space is not None
    assert space.owner_instance_id == OWNER
    assert await SqliteSpaceRepo(db).get_invited_by(SPACE) == ADMIN
    assert space.gfs_servers == expected
    # The admin's identity key is never recorded as the host's.
    assert await SqliteSpaceRepo(db).get_host_identity_pk(SPACE) is None


async def test_the_owners_later_config_reaches_a_household_an_admin_invited(db):
    """Before the fix the stub named the inviting admin as owner, so the
    real owner's configs were dropped forever and the admin's passed every
    owner-only check. Now the owner's word moves ``private_gfs`` and the GFS
    list, and the admin's does not."""
    kp = generate_space_keypair()
    space_pk = kp.public_key.hex()
    await _invite_handler(db)._on_invite(
        _invite(_proven(kp, _meta(space_pk, gfs=None)))
    )
    inbound = _inbound(db, JOINER)
    repo = SqliteSpaceRepo(db)

    # The admin tries owner-only changes: ignored.
    await _config(
        db,
        inbound,
        from_instance=ADMIN,
        meta=_signed(
            _meta(
                space_pk,
                gfs=ADMIN_LIST.to_wire(),
                config_sequence=8,
                features={"private_gfs": True, "gfs_publish_mode": "strict"},
            ),
            author=ADMIN,
            space_seed=kp.private_key,
        ),
    )
    space = await repo.get(SPACE)
    assert space is not None
    assert space.gfs_servers is None
    assert space.features.private_gfs is False
    assert space.features.gfs_publish_mode == "trusted"

    # The owner's own config applies — owner-only fields included.
    await _config(
        db,
        inbound,
        from_instance=OWNER,
        meta=_signed(
            _meta(
                space_pk,
                gfs=OWNER_LIST.to_wire(),
                name="OwnerRenamed",
                config_sequence=9,
                features={"private_gfs": True, "gfs_publish_mode": "strict"},
            ),
            author=OWNER,
            space_seed=kp.private_key,
        ),
    )
    space = await repo.get(SPACE)
    assert space is not None
    assert space.name == "OwnerRenamed"
    assert space.features.private_gfs is True
    assert space.features.gfs_publish_mode == "strict"
    assert space.gfs_servers == OWNER_LIST


@pytest.mark.parametrize(
    "gfs, expected",
    [(ADMIN_LIST.to_wire(), None), (OWNER_LIST.to_wire(), OWNER_LIST)],
)
async def test_an_admin_issued_link_seats_the_real_owner_and_only_the_owners_list(
    db, gfs, expected
):
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("joe", "u-joiner", "Joe"),
    )
    coord = SpaceInviteTokenRedeemCoordinator(
        bus=EventBus(),
        federation_service=SimpleNamespace(own_instance_id=JOINER),  # type: ignore[arg-type]
        space_repo=SqliteSpaceRepo(db),
        space_remote_member_repo=SqliteSpaceRemoteMemberRepo(db),
        user_repo=SqliteUserRepo(db),
        federation_repo=MagicMock(),
    )
    kp = generate_space_keypair()
    await coord._seat_local_after_ack(
        {
            "space_id": SPACE,
            "role": SpaceRole.MEMBER.value,
            "space_meta": _proven(kp, _meta(kp.public_key.hex(), gfs=gfs)),
        },
        viewer_user_id="u-joiner",
        issuer_instance_id=ADMIN,
    )
    space = await SqliteSpaceRepo(db).get(SPACE)
    assert space is not None
    assert space.owner_instance_id == OWNER
    assert await SqliteSpaceRepo(db).get_invited_by(SPACE) == ADMIN
    assert space.gfs_servers == expected


# ── A forwarded config edit, run on the host as the owner ────────────────


ADMIN_HOUSE = "house-admin"


@pytest.fixture
async def host(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "gfs-servers-fwd.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    users = SqliteUserRepo(db)
    spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0e" * 32))
    remote = SqliteSpaceRemoteMemberRepo(db)
    svc = SpaceService(spaces, SqliteSpacePostRepo(db), users, bus, own_instance_id=iid)
    fed = MagicMock()
    fed.send_event = AsyncMock()
    fed.send_with_mesh_fallback = AsyncMock()
    fed.broadcast_to_space_members = AsyncMock(return_value=None)
    fed.peer_supports = AsyncMock(return_value=True)
    fed_repo = MagicMock()
    fed_repo.get_instance = AsyncMock(return_value=None)
    fed_repo.list_instances_in_space = AsyncMock(return_value=[])
    svc.attach_federation(
        federation_service=fed,
        federation_repo=fed_repo,
        remote_member_repo=remote,
    )
    user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)
    await user_svc.provision(username="anna", display_name="Anna")
    space = await svc.create_space(
        owner_username="anna",
        name="S",
        features=SpaceFeatures(delegated_admin_authority=True),
    )
    owner_list = sign_gfs_server_list(
        space_id=space.id, servers=(), issued_at=5, owner_seed=kp.private_key
    )
    await spaces.set_gfs_servers(space.id, owner_list)
    await remote.add(
        space_id=space.id,
        instance_id=ADMIN_HOUSE,
        user_id="u-admin",
        user_pk=None,
        display_name="Admin",
    )
    await remote.set_role(space.id, ADMIN_HOUSE, "u-admin", SpaceRole.ADMIN.value)
    yield SimpleNamespace(
        svc=svc, spaces=spaces, space_id=space.id, owner_list=owner_list
    )
    await db.shutdown()


async def test_a_forwarded_config_edit_cannot_change_gfs_servers(host):
    outcome = await host.svc.apply_remote_admin_action(
        host.space_id,
        actor_instance_id=ADMIN_HOUSE,
        actor_user_id="u-admin",
        action="update_config",
        params={"name": "RenamedByAdmin", "gfs_servers": ADMIN_LIST.to_wire()},
    )
    assert outcome is RemoteAdminOutcome.EXECUTED
    space = await host.spaces.get(host.space_id)
    assert space is not None
    assert space.name == "RenamedByAdmin"
    assert space.gfs_servers == host.owner_list


def test_gfs_servers_is_not_a_remote_config_field() -> None:
    assert "gfs_servers" not in SpaceService._REMOTE_CONFIG_FIELDS


# ── The catch-up stream an inviting admin sends still verifies ───────────


class _B64Crypto:
    """Chunk payload codec standing in for the space content key."""

    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        return 0, base64.urlsafe_b64encode(plaintext).decode("ascii")

    async def decrypt_chunk(self, *, space_id, epoch, sync_id, ciphertext):
        return base64.urlsafe_b64decode(ciphertext)


def _sync_receiver(db) -> SpaceSyncReceiver:
    spaces = SqliteSpaceRepo(db)
    remote = SqliteSpaceRemoteMemberRepo(db)
    users = SqliteUserRepo(db)
    # The joiner is paired with NOBODY: every chunk comes from an unpaired
    # household (the admin reached it over the mesh).
    unpaired = SimpleNamespace(get_instance=AsyncMock(return_value=None))
    return SpaceSyncReceiver(
        bus=EventBus(),
        encoder=FederationEncoder(generate_identity_keypair().private_key),
        crypto=_B64Crypto(),  # type: ignore[arg-type]
        federation_repo=unpaired,  # type: ignore[arg-type]
        space_repo=spaces,
        space_post_repo=SqliteSpacePostRepo(db),
        space_task_repo=MagicMock(),
        page_repo=MagicMock(),
        sticky_repo=MagicMock(),
        space_calendar_repo=MagicMock(),
        gallery_repo=MagicMock(),
        authorship=SpaceAuthorship(
            space_repo=spaces, remote_member_repo=remote, user_repo=users
        ),
    )


async def _admin_posts_chunk(signing_seed: bytes) -> bytes:
    _, ciphertext = await _B64Crypto().encrypt_chunk(
        space_id=SPACE,
        sync_id="sync-1",
        plaintext=orjson.dumps(
            {
                "records": [
                    {
                        "id": "p-admin-1",
                        "author": "u-admin",
                        "type": "text",
                        "content": "hello from the admin household",
                        "created_at": "2026-10-01T00:00:00+00:00",
                    }
                ]
            }
        ),
    )
    envelope = {
        "sync_id": "sync-1",
        "resource": "posts",
        "space_id": SPACE,
        "epoch": 0,
        "seq_start": 0,
        "seq_end": 0,
        "is_last": False,
        "encrypted_payload": ciphertext,
    }
    envelope["signatures"] = FederationEncoder(signing_seed).sign_envelope_all(
        orjson.dumps(envelope), suite="ed25519"
    )
    return serialise_chunk(envelope)


def _admin_invite(kp) -> FederationEvent:
    meta = _meta(
        kp.public_key.hex(),
        gfs=None,
        roster=[
            {
                "user_id": "u-owner",
                "instance_id": OWNER,
                "display_name": "Owner",
                "role": "owner",
            },
            {
                "user_id": "u-admin",
                "instance_id": ADMIN,
                "display_name": "Admin",
                "role": "admin",
            },
        ],
    )
    event = _invite(_proven(kp, meta))
    event.payload["host_identity_pk"] = ed25519_public_key(ADMIN_SEED).hex()
    return event


@pytest.mark.parametrize("signer", [ADMIN_SEED, b"\x33" * 32])
async def test_an_admin_invited_household_applies_the_admins_catch_up_stream(
    db, signer
):
    """Owner offline / unpaired; the joiner reached the space only through
    the delegated admin that invited it. The stub names the REAL owner, yet
    the admin's §25.6 catch-up stream still verifies — against the admin's
    own key, kept on its ``space_instances`` row, never as the host key —
    and its members' content applies. A chunk signed by anyone else is
    dropped."""
    kp = generate_space_keypair()
    await _invite_handler(db)._on_invite(_admin_invite(kp))
    repo = SqliteSpaceRepo(db)
    space = await repo.get(SPACE)
    assert space is not None and space.owner_instance_id == OWNER
    assert await repo.get_host_identity_pk(SPACE) is None
    assert await repo.get_space_instance_identity_pk(SPACE, ADMIN) == (
        ed25519_public_key(ADMIN_SEED).hex()
    )

    await _sync_receiver(db).on_chunk(
        await _admin_posts_chunk(signer),
        from_instance=ADMIN,
        expected_space_id=SPACE,
    )
    held = await SqliteSpacePostRepo(db).get("p-admin-1")
    if signer == ADMIN_SEED:
        assert held is not None and held[0] == SPACE
        assert held[1].author == "u-admin"
    else:
        assert held is None


async def test_an_unrecorded_household_cannot_stream_into_the_space(db):
    """No paired row and no recorded key: an unpaired household that merely
    names the space id is not verified at all."""
    kp = generate_space_keypair()
    await _invite_handler(db)._on_invite(_admin_invite(kp))
    stranger_seed = b"\x34" * 32
    stranger = derive_instance_id(ed25519_public_key(stranger_seed))
    await _sync_receiver(db).on_chunk(
        await _admin_posts_chunk(stranger_seed),
        from_instance=stranger,
        expected_space_id=SPACE,
    )
    assert await SqliteSpacePostRepo(db).get("p-admin-1") is None
