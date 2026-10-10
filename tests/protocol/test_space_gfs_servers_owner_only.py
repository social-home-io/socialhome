"""Release-blocker protocol tests: a space's GFS list (``gfs_servers``,
migration 0089) is the OWNER's alone, on every household.

Members will publish to every GFS on that list — including servers their own
household never connected to — so a delegated admin that could change it
would aim every member's publishes at a server of its choosing. Two paths
could carry such a change, and both must ignore it:

* the authority-signed ``SPACE_CONFIG_CHANGED`` a seed-holding delegated
  admin broadcasts (accepted for the rest of its config block) — on the host
  AND on a member mirror;
* a config edit a remote admin forwards to the host
  (``SPACE_REMOTE_ADMIN_ACTION`` ``update_config``), which the host runs AS
  THE OWNER.

Driven through the real inbound handler / space service with real SQLite
repos; only the federation transport is a mock.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_space_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.gfs_server import GfsServer
from socialhome.domain.space import (
    JoinMode,
    RemoteAdminOutcome,
    SpaceFeatures,
    SpaceRole,
    SpaceType,
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

_OWNER_SERVER = GfsServer(
    gfs_instance_id="gfs-owner",
    inbox_url="https://gfs-owner.example",
    public_key="ab" * 32,
    gfs_key_suite="ed25519",
    display_name="Owner's GFS",
)


def _evil_entry() -> dict:
    return {
        "gfs_instance_id": "gfs-evil",
        "inbox_url": "https://gfs-evil.example",
        "public_key": "cd" * 32,
        "gfs_key_suite": "ed25519",
        "display_name": "Admin's GFS",
    }


# ── Inbound SPACE_CONFIG_CHANGED (authority-signed) ──────────────────────


async def _seed_space(db, *, space_id: str, space_pub_hex: str) -> None:
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type, join_mode, config_sequence,"
        " gfs_servers) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            space_id,
            "Global",
            "owner-i",
            "owner",
            space_pub_hex,
            SpaceType.GLOBAL.value,
            JoinMode.INVITE_ONLY.value,
            5,
            json.dumps([_OWNER_SERVER.to_wire()]),
        ),
    )


def _signed_config(*, space_id: str, from_instance: str, seed: bytes, gfs) -> dict:
    meta = {
        "name": "RenamedByAdmin",
        "owner_instance_id": "owner-i",
        "owner_username": "owner",
        "identity_public_key": "ignored-by-stub",
        "config_sequence": 9,
        "config_author_instance": from_instance,
        "space_type": "global",
        "join_mode": "invite_only",
        "features": {},
        "gfs_servers": gfs,
    }
    meta.update(
        sign_authority_event(
            event_type="space_config_changed",
            space_id=space_id,
            payload=strip_authority_sig_fields(meta),
            space_seed=seed,
        )
    )
    return meta


def _config_event(space_id: str, from_instance: str, meta: dict) -> FederationEvent:
    return FederationEvent(
        msg_id="m-cfg",
        event_type=FederationEventType.SPACE_CONFIG_CHANGED,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={
            "space_id": space_id,
            "sequence": 9,
            "event_type": "feature_changed",
            "space_meta": meta,
        },
        space_id=space_id,
    )


@pytest.mark.parametrize("we_host", [True, False])
@pytest.mark.parametrize("gfs", [[_evil_entry()], []])
async def test_a_delegated_admins_signed_config_cannot_change_gfs_servers(
    db, we_host, gfs
):
    inbound = FederationInboundService(
        bus=EventBus(),
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        space_remote_member_repo=SqliteSpaceRemoteMemberRepo(db),
    )
    inbound._federation_service = SimpleNamespace(
        own_instance_id="owner-i" if we_host else "member-i"
    )
    kp = generate_space_keypair()
    await _seed_space(db, space_id="sp-g", space_pub_hex=kp.public_key.hex())

    meta = _signed_config(
        space_id="sp-g", from_instance="admin-i", seed=kp.private_key, gfs=gfs
    )
    await inbound._on_space_config_changed(_config_event("sp-g", "admin-i", meta))

    space = await SqliteSpaceRepo(db).get("sp-g")
    assert space is not None
    # The rest of the signed block applied…
    assert space.name == "RenamedByAdmin"
    assert space.config_sequence == 9
    # …but the GFS list is still the owner's.
    assert space.gfs_servers == (_OWNER_SERVER,)


async def test_the_owners_signed_config_does_change_gfs_servers(db):
    """Control: the same config from the owner household applies."""
    inbound = FederationInboundService(
        bus=EventBus(),
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        space_remote_member_repo=SqliteSpaceRemoteMemberRepo(db),
    )
    inbound._federation_service = SimpleNamespace(own_instance_id="member-i")
    kp = generate_space_keypair()
    await _seed_space(db, space_id="sp-g", space_pub_hex=kp.public_key.hex())
    meta = _signed_config(
        space_id="sp-g",
        from_instance="owner-i",
        seed=kp.private_key,
        gfs=[_evil_entry()],
    )
    await inbound._on_space_config_changed(_config_event("sp-g", "owner-i", meta))
    space = await SqliteSpaceRepo(db).get("sp-g")
    assert space is not None
    assert [s.gfs_instance_id for s in space.gfs_servers] == ["gfs-evil"]


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
    await spaces.set_gfs_servers(space.id, (_OWNER_SERVER,))
    await remote.add(
        space_id=space.id,
        instance_id=ADMIN_HOUSE,
        user_id="u-admin",
        user_pk=None,
        display_name="Admin",
    )
    await remote.set_role(space.id, ADMIN_HOUSE, "u-admin", SpaceRole.ADMIN.value)
    yield SimpleNamespace(svc=svc, spaces=spaces, space_id=space.id)
    await db.shutdown()


async def test_a_forwarded_config_edit_cannot_change_gfs_servers(host):
    outcome = await host.svc.apply_remote_admin_action(
        host.space_id,
        actor_instance_id=ADMIN_HOUSE,
        actor_user_id="u-admin",
        action="update_config",
        params={"name": "RenamedByAdmin", "gfs_servers": [_evil_entry()]},
    )
    assert outcome is RemoteAdminOutcome.EXECUTED
    space = await host.spaces.get(host.space_id)
    assert space is not None
    assert space.name == "RenamedByAdmin"
    assert space.gfs_servers == (_OWNER_SERVER,)


def test_gfs_servers_is_not_a_remote_config_field() -> None:
    assert "gfs_servers" not in SpaceService._REMOTE_CONFIG_FIELDS
