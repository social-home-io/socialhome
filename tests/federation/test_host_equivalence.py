"""Tests for :mod:`socialhome.federation.host_equivalence` — who may act as
the host of a space on THIS household: the owner, and the delegated admin
household that invited us while delegation is on and its admin seat lives."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceRole,
    SpaceType,
)
from socialhome.federation.host_equivalence import (
    acts_as_host,
    is_trusted_inviter,
    touches_owner_household,
)
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.space_owner_statement import MAX_STATEMENT_AGE_MS

OWNER = "owner-hh"
ADMIN = "admin-hh"
OTHER_ADMIN = "other-admin-hh"


@pytest.fixture
async def env(db):
    spaces = SqliteSpaceRepo(db)
    seats = SqliteSpaceRemoteMemberRepo(db)
    space = Space(
        id="sp-1",
        name="S",
        owner_instance_id=OWNER,
        owner_username="owner",
        identity_public_key="aa" * 32,
        config_sequence=1,
        features=SpaceFeatures(delegated_admin_authority=True),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )
    await spaces.save(space)
    for hh, uid in ((ADMIN, "u-admin"), (OTHER_ADMIN, "u-other")):
        await seats.add(
            space_id="sp-1",
            instance_id=hh,
            user_id=uid,
            user_pk=None,
            display_name=uid,
            role=SpaceRole.ADMIN.value,
        )
    await spaces.set_invited_by("sp-1", ADMIN, 0)
    await spaces.set_owner_statement("sp-1", _current_statement())
    return spaces, seats


def _current_statement(*, age_ms: int = 0, key: str = "aa" * 32, epoch: int = 0):
    """A stored (already verified) owner statement for the pinned key."""
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    return json.dumps(
        {"authority_pk": key, "authority_key_epoch": epoch, "issued_at": now - age_ms}
    )


async def _check(env, sender: str) -> tuple[bool, bool]:
    spaces, seats = env
    space = await spaces.get("sp-1")
    return (
        await acts_as_host(spaces, seats, space, sender),
        await is_trusted_inviter(spaces, seats, space, sender),
    )


async def test_the_owner_always_acts_as_host(env):
    assert await _check(env, OWNER) == (True, False)


async def test_the_inviting_admin_acts_as_host(env):
    assert await _check(env, ADMIN) == (True, True)


async def test_another_admin_household_does_not(env):
    assert await _check(env, OTHER_ADMIN) == (False, False)


async def test_delegation_off_ends_the_trust(env):
    spaces, _ = env
    space = await spaces.get("sp-1")
    await spaces.save(replace(space, features=SpaceFeatures()))
    assert await _check(env, ADMIN) == (False, False)
    assert await _check(env, OWNER) == (True, False)


async def test_a_demoted_or_removed_admin_seat_ends_the_trust(env):
    _, seats = env
    await seats.set_role("sp-1", ADMIN, "u-admin", SpaceRole.MEMBER.value)
    assert await _check(env, ADMIN) == (False, False)


async def test_no_marker_no_trust(env, db):
    spaces, seats = env
    await db.enqueue(
        "UPDATE spaces SET invited_by_instance=NULL, invited_by_epoch=NULL"
        " WHERE id='sp-1'"
    )
    assert await _check(env, ADMIN) == (False, False)


async def test_none_space_or_sender(env):
    spaces, seats = env
    assert await acts_as_host(spaces, seats, None, OWNER) is False
    space = await spaces.get("sp-1")
    assert await acts_as_host(spaces, seats, space, "") is False
    # Without a seat repo only the owner counts.
    assert await acts_as_host(spaces, None, space, ADMIN) is False
    assert await acts_as_host(spaces, None, space, OWNER) is True


async def test_touches_owner_household(env):
    spaces, seats = env
    space = await spaces.get("sp-1")
    await seats.add(
        space_id="sp-1",
        instance_id=OWNER,
        user_id="u-o2",
        user_pk=None,
        display_name="o2",
        role=SpaceRole.MEMBER.value,
    )
    await spaces.set_owner_user_id("sp-1", "u-owner")
    for kwargs, expected in [
        ({"user_id": "u-o2"}, True),  # a seat of the owner household
        ({"user_id": "u-owner"}, True),  # the owner seat
        ({"user_id": "x", "instance_id": OWNER}, True),
        ({"user_id": "u-admin"}, False),
        ({"user_id": "u-admin", "instance_id": ADMIN}, False),
    ]:
        assert (
            await touches_owner_household(spaces, seats, space, **kwargs)
        ) is expected
    assert await touches_owner_household(spaces, None, space, user_id="u-o2") is False


@pytest.mark.parametrize(
    "statement",
    [
        None,
        "stale",
        "other_key",
        "other_epoch",
    ],
)
async def test_the_trust_needs_a_current_owner_statement(env, statement):
    """An admin revoked by a rotation still holds the owner's OLD statement:
    trust lapses when the statement held here is missing, stale, or names
    another key or epoch than the one we pin."""
    spaces, _ = env
    value = {
        None: None,
        "stale": _current_statement(age_ms=MAX_STATEMENT_AGE_MS + 1),
        "other_key": _current_statement(key="cd" * 32),
        "other_epoch": _current_statement(epoch=1),
    }[statement]
    await spaces.set_owner_statement("sp-1", value)
    assert await _check(env, ADMIN) == (False, False)
    assert await _check(env, OWNER) == (True, False)


async def test_the_trust_is_bound_to_the_proven_epoch(env):
    """A moved pin alone never resumes the trust: only a re-proof for the
    new epoch does (``set_invited_by_epoch``)."""
    spaces, _ = env
    await spaces.adopt_authority_key("sp-1", "aa" * 32, 1)
    await spaces.set_owner_statement("sp-1", _current_statement(epoch=1))
    assert await _check(env, ADMIN) == (False, False)
    await spaces.set_invited_by_epoch("sp-1", 1)
    assert await _check(env, ADMIN) == (True, True)
