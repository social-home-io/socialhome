"""Tests for :class:`OwnerStatementRefreshService` — the owner household keeps
its signed ownership statements fresh (migration 0093)."""

from __future__ import annotations

import json

import pytest

from socialhome.crypto import derive_instance_id, ed25519_public_key
from socialhome.domain.events import SpaceConfigChanged
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceConfigEventType,
    SpaceFeatures,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.owner_statement_refresh_service import (
    OwnerStatementRefreshService,
)
from socialhome.space_owner_statement import (
    STATEMENT_REFRESH_MS,
    verify_owner_statement,
)

SEED = b"\x71" * 32
OWN = derive_instance_id(ed25519_public_key(SEED))
NOW = 1_800_000_000_000


@pytest.fixture
async def env(db):
    spaces = SqliteSpaceRepo(db)
    bus = EventBus()
    events: list[SpaceConfigChanged] = []

    async def _record(event: SpaceConfigChanged) -> None:
        events.append(event)

    bus.subscribe(SpaceConfigChanged, _record)
    svc = OwnerStatementRefreshService(space_repo=spaces, bus=bus)
    svc.attach_identity(own_instance_id=OWN, own_signing_key=SEED)
    for sid, owner in (("sp-own", OWN), ("sp-other", "someone-else")):
        await spaces.save(
            Space(
                id=sid,
                name="S",
                owner_instance_id=owner,
                owner_username="o",
                identity_public_key="aa" * 32,
                config_sequence=1,
                features=SpaceFeatures(),
                space_type=SpaceType.PRIVATE,
                join_mode=JoinMode.INVITE_ONLY,
            )
        )
    return spaces, svc, events


async def test_signs_and_broadcasts_when_missing_then_waits(env):
    spaces, svc, events = env
    assert await svc.refresh("sp-own", now_ms=NOW) is True
    space = await spaces.get("sp-own")
    held = json.loads(space.owner_statement)
    verify_owner_statement(
        held,
        space_id="sp-own",
        owner_instance_id=OWN,
        authority_pk_hex="aa" * 32,
        authority_key_epoch=0,
        now_ms=NOW,
    )
    assert space.config_sequence == 2
    assert events[-1].event_type == (
        SpaceConfigEventType.OWNER_STATEMENT_REFRESHED.value
    )
    # Fresh enough: nothing to do until the refresh interval passed.
    assert await svc.refresh("sp-own", now_ms=NOW + 1000) is False
    assert await svc.refresh("sp-own", now_ms=NOW + STATEMENT_REFRESH_MS) is True
    assert len(events) == 2


async def test_a_rotation_makes_it_due_at_once(env):
    spaces, svc, events = env
    await svc.refresh("sp-own", now_ms=NOW)
    await spaces.adopt_authority_key("sp-own", "cd" * 32, 1)
    assert await svc.refresh("sp-own", now_ms=NOW + 1) is True
    held = json.loads((await spaces.get("sp-own")).owner_statement)
    assert (held["authority_pk"], held["authority_key_epoch"]) == ("cd" * 32, 1)


async def test_only_owned_spaces_and_only_with_an_identity(env, db):
    spaces, svc, events = env
    assert await svc.refresh("sp-other", now_ms=NOW) is False
    assert await svc.refresh("nope", now_ms=NOW) is False
    bare = OwnerStatementRefreshService(space_repo=spaces, bus=EventBus())
    assert await bare.refresh("sp-own", now_ms=NOW) is False
    assert await bare.refresh_all() == 0
    assert events == []


async def test_refresh_all_and_the_loop_lifecycle(env):
    spaces, svc, events = env
    assert await svc.refresh_all() == 1
    await svc.start()
    await svc.start()  # idempotent
    await svc.stop()
    assert len(events) == 1
