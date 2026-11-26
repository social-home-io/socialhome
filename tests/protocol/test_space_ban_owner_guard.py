"""P7b Fable review (2026-11-26): a delegated admin's signed ban / unban
never reaches over the owner, on real SQLite.

* C1 — on the OWNER household, an admin's ``SPACE_MEMBER_BANNED`` naming the
  owner or one of the owner household's people (local users) is refused.
  Before, the owner-household check missed on the host itself: the owner's
  user id is NULL there (``users`` answers it) and the host keeps no remote
  seat rows for its own people.
* I1 — an admin can't lift the owner's ban by banning the same target
  first (the re-ban used to overwrite ``banned_by``).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from socialhome.crypto import (
    derive_instance_id,
    ed25519_public_key,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import FederationEvent
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.federation_inbound import SpaceMembershipInboundHandlers
from socialhome.services.space_service import signed_ban_event

pytestmark = pytest.mark.security

_SEED = b"\x07" * 32
_SID = "sp-1"
_ADMIN_HOUSE = "admin-house"


class _Registry:
    def register(self, *_a, **_k) -> None:
        return None


class _Federation:
    def __init__(self, own_instance_id: str) -> None:
        self.own_instance_id = own_instance_id
        self._event_registry = _Registry()


def _event(event_type, payload: dict) -> FederationEvent:
    return FederationEvent(
        msg_id="m",
        event_type=event_type,
        from_instance=_ADMIN_HOUSE,
        to_instance="self",
        timestamp=datetime.now(UTC).isoformat(),
        payload=payload,
        space_id=_SID,
    )


def _ban(*, banned: bool = True, **target) -> FederationEvent:
    event_type, payload = signed_ban_event(_SID, banned=banned, seed=_SEED, **target)
    return _event(event_type, payload)


@pytest.fixture
async def host(tmp_path):
    """The OWNER household: alice owns ``sp-1``, bob (same household) is a
    member, carol (same household) holds no seat; ``admin-house`` holds a
    live admin seat with delegation on (a seed holder)."""
    kp = generate_identity_keypair()
    own = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_path / "t.db", batch_timeout_ms=10)
    await db.startup()
    for username, uid in (
        ("alice", "uid-alice"),
        ("bob", "uid-bob"),
        ("carol", "uid-carol"),
    ):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?, ?, ?)",
            (username, uid, username),
        )
    repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x07" * 32))
    seats = SqliteSpaceRemoteMemberRepo(db)
    await repo.save(
        Space(
            id=_SID,
            name="S",
            owner_instance_id=own,
            owner_username="alice",
            identity_public_key=ed25519_public_key(_SEED).hex(),
            config_sequence=0,
            features=dataclasses.replace(
                SpaceFeatures(), delegated_admin_authority=True
            ),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    for uid, role in (("uid-alice", "owner"), ("uid-bob", "member")):
        await repo.save_member(
            SpaceMember(space_id=_SID, user_id=uid, role=role, joined_at="2025")
        )
    await seats.add(
        space_id=_SID,
        instance_id=_ADMIN_HOUSE,
        user_id="u-adm",
        user_pk=None,
        display_name="A",
        role="admin",
    )
    handlers = SpaceMembershipInboundHandlers(
        bus=EventBus(),
        space_repo=repo,
        seat_repo=seats,
        users=SqliteUserRepo(db),
    )
    handlers.attach_to(_Federation(own))
    yield repo, handlers
    await db.shutdown()


@pytest.mark.parametrize("uid", ["uid-alice", "uid-bob", "uid-carol"])
async def test_an_admin_never_bans_the_owner_households_people_on_the_owner(host, uid):
    repo, handlers = host
    seated_before = await repo.get_member(_SID, uid)
    await handlers._on_banned(_ban(user_id=uid, banned_by="u-adm"))
    assert await repo.is_banned(_SID, uid) is False
    assert await repo.get_member(_SID, uid) == seated_before


async def test_an_admin_still_bans_another_households_person_on_the_owner(host):
    repo, handlers = host
    await handlers._on_banned(_ban(user_id="u-remote", banned_by="u-adm"))
    assert await repo.is_banned(_SID, "u-remote") is True


async def test_an_admin_never_unbans_for_the_owner_household(host):
    repo, handlers = host
    await repo.ban_member(_SID, "uid-carol", banned_by="uid-alice")
    await handlers._on_unbanned(_ban(banned=False, user_id="uid-carol"))
    assert await repo.is_banned(_SID, "uid-carol") is True


async def test_an_admin_cannot_lift_the_owners_ban_by_re_banning(host):
    repo, handlers = host
    await repo.ban_member(_SID, "u-x", banned_by="uid-alice")
    await repo.ban_household(_SID, "bad-house", banned_by="uid-alice")
    await handlers._on_banned(_ban(user_id="u-x", banned_by="u-adm"))
    await handlers._on_banned(_ban(instance_id="bad-house", banned_by=None))
    await handlers._on_unbanned(_ban(banned=False, user_id="u-x"))
    await handlers._on_unbanned(_ban(banned=False, instance_id="bad-house"))
    assert await repo.is_banned(_SID, "u-x") is True
    assert await repo.is_household_banned(_SID, "bad-house") is True


async def test_an_admin_never_claims_the_owner_as_the_banner(host):
    """Minor M2: ``banned_by`` is the sender's word — an admin that names
    the owner as the banner would make a ban no admin can lift and blame
    the owner for it. Refused."""
    repo, handlers = host
    await handlers._on_banned(_ban(user_id="u-y", banned_by="uid-alice"))
    assert await repo.is_banned(_SID, "u-y") is False
