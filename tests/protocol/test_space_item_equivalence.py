"""Release-blocker: a v_97 space item authorizes exactly like its event.

A space item (``docs/protocol/space-items.md``) reaches a household from a
channel ring or a ``SPACE_ITEM`` event and is applied by the very handler of
the federation event it replaces. The ONE thing that may differ is how the
origin is proven: by the household's author signature + writer cert instead
of the envelope signature. Everything after that — ban, archived space,
writer seat, follower opt-ins — must be the same code with the same answer.

These tests run every scenario both ways against REAL SQLite repos and the
REAL gate composition of :class:`FederationService`:

* **legacy:** the pipeline's own post-decrypt order — its ban step, then
  :meth:`FederationService.post_decrypt_gate_steps`;
* **item:** :class:`SpaceItemDispatch` (signature, cert, epoch) →
  :meth:`FederationService.dispatch_space_item`.

and assert the verdicts match. Item-only refusals (a forged origin, a cert
for another household, a reader cert on a write) are checked on top: the
item path may refuse MORE, never less.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceRole,
    SpaceType,
)
from socialhome.domain.writer_cert import WRITER_SCOPE_COMMENT, WRITER_SCOPE_WRITE
from socialhome.federation.federation_service import FederationService
from socialhome.federation.inbound_validator import (
    InboundContext,
    make_ban_check,
    run_post_decrypt_gates,
)
from socialhome.federation.space_item_dispatch import SpaceItemDispatch
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_item_author import build_signed_event_item
from socialhome.services.space_writer_cert_service import (
    WRITER_CERT_FIELD,
    SpaceWriterCertService,
)
from socialhome.writer_cert import sign_writer_cert

pytestmark = pytest.mark.security

SPACE_ID = "sp-items"
EPOCH = 3
SPACE_KP = generate_identity_keypair()
HOST = "host-household"
US = "receiver-household"

E = FederationEventType


class _Household:
    def __init__(self, user_id: str) -> None:
        self.kp = generate_identity_keypair()
        self.id = derive_instance_id(self.kp.public_key)
        self.user = user_id


class _Fed:
    """Just enough of :class:`FederationService` to run its REAL gate
    composition and :meth:`dispatch_space_item`; the registry dispatch is
    recorded instead of handled."""

    post_decrypt_gate_steps = FederationService.post_decrypt_gate_steps
    space_write_gates = FederationService.space_write_gates
    dispatch_space_item = FederationService.dispatch_space_item

    def __init__(self, federation_repo, space_repo, members) -> None:
        self._federation_repo = federation_repo
        self._space_repo = space_repo
        self._space_remote_member_repo = members
        self._user_repo = None
        self._pending_seat_buffer = None
        self.dispatched: list[FederationEvent] = []

    async def _notify_archived_write(self, event, space) -> None:
        return None

    async def _dispatch_event(self, event: FederationEvent) -> None:
        self.dispatched.append(event)


class _Certs:
    """The real cert verification; every epoch is fresh here."""

    async def check_item(self, space, raw, **kw) -> bool:
        return SpaceWriterCertService.check_item_cert(space, raw, **kw)


@pytest.fixture
async def world(tmp_dir):
    db = AsyncDatabase(tmp_dir / "items.db", batch_timeout_ms=10)
    await db.startup()
    spaces = SqliteSpaceRepo(db)
    space = Space(
        id=SPACE_ID,
        name="Items",
        owner_instance_id=HOST,
        owner_username="anna",
        identity_public_key=SPACE_KP.public_key.hex(),
        config_sequence=0,
        features=SpaceFeatures(allow_subscriber_comment=True),
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.INVITE_ONLY,
    )
    await spaces.save(space)
    members = SqliteSpaceRemoteMemberRepo(db)
    fed = _Fed(SqliteFederationRepo(db), spaces, members)
    dispatch = SpaceItemDispatch(
        federation=fed,
        writer_certs=_Certs(),  # type: ignore[arg-type]
        own_instance_id=US,
    )
    yield fed, dispatch, spaces, members
    await db.shutdown()


async def _seat(members, who: _Household, role: str) -> None:
    await members.add(
        space_id=SPACE_ID,
        instance_id=who.id,
        user_id=who.user,
        user_pk=None,
        display_name=None,
        role=role,
    )


def _cert(who: _Household, scope: str, *, reader: bool = False) -> dict:
    return sign_writer_cert(
        space_seed=SPACE_KP.private_key,
        space_id=SPACE_ID,
        epoch=EPOCH,
        instance_pk=who.kp.public_key,
        scope=scope,
        reader=reader,
    ).to_wire()


def _body(event_type: E, who: _Household) -> dict:
    if event_type is E.SPACE_COMMENT_CREATED:
        return {"space_id": SPACE_ID, "author": who.user, "comment_id": "c-1"}
    return {"space_id": SPACE_ID, "task_id": "t-1", "created_by": who.user}


_N = iter(range(1, 10**6))


def _item(event_type: E, who: _Household, cert: dict, *, signer=None) -> dict:
    signer = signer or who
    inner = build_signed_event_item(
        item_type=event_type.value,
        item_id=f"{next(_N):032x}",
        space_id=SPACE_ID,
        origin_instance_id=who.id,
        identity_seed=signer.kp.private_key,
        identity_pk=signer.kp.public_key,
        ts=datetime.now(UTC).isoformat(),
        body=_body(event_type, who),
    )
    inner[WRITER_CERT_FIELD] = cert
    return inner


async def _legacy(fed: _Fed, event_type: E, who: _Household) -> bool:
    """The pipeline's verdict on the same event from ``who``."""
    event = FederationEvent(
        msg_id="m",
        event_type=event_type,
        from_instance=who.id,
        to_instance=US,
        timestamp=datetime.now(UTC).isoformat(),
        payload=_body(event_type, who),
        space_id=SPACE_ID,
    )
    ctx = InboundContext(
        envelope={"space_id": SPACE_ID, "from_instance": who.id}, event=event
    )
    steps = [make_ban_check(federation_repo=fed._federation_repo)]
    steps += fed.post_decrypt_gate_steps()
    return await run_post_decrypt_gates(ctx, steps=steps)


async def _via_item(fed, dispatch, spaces, event_type, who, cert, **kw) -> bool:
    space = await spaces.get(SPACE_ID)
    before = len(fed.dispatched)
    await dispatch.apply(
        space,
        epoch=EPOCH,
        item_type=event_type.value,
        inner=_item(event_type, who, cert, **kw),
    )
    if len(fed.dispatched) == before:
        return False
    event = fed.dispatched[-1]
    assert event.from_instance == who.id
    assert event.space_id == SPACE_ID
    assert event.event_type is event_type
    return True


async def _both(world, event_type, who, cert) -> tuple[bool, bool]:
    fed, dispatch, spaces, _ = world
    return (
        await _legacy(fed, event_type, who),
        await _via_item(fed, dispatch, spaces, event_type, who, cert),
    )


async def test_a_member_writes_the_same_both_ways(world):
    member = _Household("u-member")
    await _seat(world[3], member, SpaceRole.MEMBER.value)
    assert await _both(
        world, E.SPACE_TASK_CREATED, member, _cert(member, WRITER_SCOPE_WRITE)
    ) == (True, True)


async def test_a_follower_task_is_refused_both_ways(world):
    follower = _Household("u-follower")
    await _seat(world[3], follower, SpaceRole.SUBSCRIBER.value)
    cert = _cert(follower, WRITER_SCOPE_COMMENT, reader=True)
    assert await _both(world, E.SPACE_TASK_CREATED, follower, cert) == (False, False)


async def test_a_follower_comment_passes_both_ways_while_allowed(world):
    follower = _Household("u-follower")
    await _seat(world[3], follower, SpaceRole.SUBSCRIBER.value)
    cert = _cert(follower, WRITER_SCOPE_COMMENT, reader=True)
    assert await _both(world, E.SPACE_COMMENT_CREATED, follower, cert) == (
        True,
        True,
    )


async def test_a_banned_household_is_refused_both_ways(world):
    member = _Household("u-banned")
    await _seat(world[3], member, SpaceRole.MEMBER.value)
    await world[2].ban_household(SPACE_ID, member.id, reason="spam")
    cert = _cert(member, WRITER_SCOPE_WRITE)
    assert await _both(world, E.SPACE_TASK_CREATED, member, cert) == (False, False)


async def test_a_kicked_household_with_a_live_cert_is_refused_both_ways(world):
    member = _Household("u-kicked")
    await _seat(world[3], member, SpaceRole.MEMBER.value)
    await world[3].remove(SPACE_ID, member.id, member.user)
    cert = _cert(member, WRITER_SCOPE_WRITE)
    assert await _both(world, E.SPACE_TASK_CREATED, member, cert) == (False, False)


async def test_a_household_with_no_seat_is_refused_both_ways(world):
    stranger = _Household("u-stranger")
    cert = _cert(stranger, WRITER_SCOPE_WRITE)
    assert await _both(world, E.SPACE_TASK_CREATED, stranger, cert) == (
        False,
        False,
    )


async def test_an_archived_space_refuses_both_ways(world):
    member = _Household("u-member")
    await _seat(world[3], member, SpaceRole.MEMBER.value)
    space = await world[2].get(SPACE_ID)
    await world[2].save(replace(space, archived=True))
    cert = _cert(member, WRITER_SCOPE_WRITE)
    assert await _both(world, E.SPACE_TASK_CREATED, member, cert) == (False, False)
    # A removal still lands in an archived space — both ways.
    assert await _both(world, E.SPACE_TASK_DELETED, member, cert) == (True, True)


# ── Item-only refusals: the item path may refuse more, never less ────────


async def test_a_forged_origin_is_refused(world):
    fed, dispatch, spaces, members = world
    victim = _Household("u-victim")
    forger = _Household("u-forger")
    await _seat(members, victim, SpaceRole.MEMBER.value)
    assert await _legacy(fed, E.SPACE_TASK_CREATED, victim)
    assert not await _via_item(
        fed,
        dispatch,
        spaces,
        E.SPACE_TASK_CREATED,
        victim,
        _cert(victim, WRITER_SCOPE_WRITE),
        signer=forger,
    )


async def test_a_cert_for_another_household_is_refused(world):
    fed, dispatch, spaces, members = world
    member = _Household("u-member")
    other = _Household("u-other")
    await _seat(members, member, SpaceRole.MEMBER.value)
    assert not await _via_item(
        fed, dispatch, spaces, E.SPACE_TASK_CREATED, member, _cert(other, "write")
    )


async def test_a_comment_scope_cert_cannot_carry_a_write(world):
    fed, dispatch, spaces, members = world
    member = _Household("u-member")
    await _seat(members, member, SpaceRole.MEMBER.value)
    assert not await _via_item(
        fed,
        dispatch,
        spaces,
        E.SPACE_TASK_CREATED,
        member,
        _cert(member, WRITER_SCOPE_COMMENT),
    )


async def test_an_item_under_another_space_key_is_refused(world):
    fed, dispatch, spaces, members = world
    member = _Household("u-member")
    await _seat(members, member, SpaceRole.MEMBER.value)
    rogue = generate_identity_keypair()
    cert = sign_writer_cert(
        space_seed=rogue.private_key,
        space_id=SPACE_ID,
        epoch=EPOCH,
        instance_pk=member.kp.public_key,
        scope=WRITER_SCOPE_WRITE,
    ).to_wire()
    assert not await _via_item(
        fed, dispatch, spaces, E.SPACE_TASK_CREATED, member, cert
    )
