"""Release-blocker protocol tests for the read-only Follower write gate.

Marked ``@pytest.mark.security`` — CLAUDE.md requires these to run before
every commit touching federation code.

A household that redeems a ``subscriber`` (Follower) invite link is a
full participant on the transport: it sits in ``space_instances``, so the
content stream and every epoch content key reach it. Its ONLY protection
against writing back is step 12 of the §24.11 pipeline. That makes the
step a protocol invariant rather than a service detail, and this file
pins it against the REAL SQLite repos — the fakes elsewhere cannot catch
a repo that starts filtering rows the gate depends on.

Coverage:
* **A kicked household is not a stranger.** ``get`` / ``list_for_space``
  filter tombstones; the gate reads them, because "removed" must not
  produce the same answer as "never heard of" — the one answer the gate
  is lenient about.
* **The whole write vocabulary is refused**, ``*_UPDATED`` /
  ``*_DELETED`` included, not just the two events that name an author.
* **The author field is not an input.** It is written by the sender.
* **A member household is untouched**, including a household that holds
  a follower seat alongside a real member seat.
* **An unattributable write is refused.** No routing ``space_id`` and
  none in the payload means the write cannot be pinned to a space, and
  several handlers key on a bare row id.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    SPACE_WRITE_EVENT_TYPES,
    FederationEvent,
    FederationEventType,
)
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceRole,
    SpaceType,
)
from socialhome.federation.inbound_validator import (
    InboundContext,
    make_check_space_writer,
)
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo

pytestmark = pytest.mark.security

SPACE_ID = "sp-follower"
FOLLOWER = "follower-household"
MEMBER = "member-household"
REFUSED = {"status": "ok", "dropped": "subscriber-write"}


@pytest.fixture
async def gate(tmp_dir):
    """The real step over real SQLite rows.

    Yields ``(step, remote_members, spaces)`` — a space owned by a THIRD
    household on purpose, so the gate is exercised in its member-household
    shape (space content fans out peer-to-peer; every receiver enforces).
    """
    db = AsyncDatabase(tmp_dir / "gate.db", batch_timeout_ms=10)
    await db.startup()
    spaces = SqliteSpaceRepo(db)
    await spaces.save(
        Space(
            id=SPACE_ID,
            name="Shared",
            owner_instance_id="some-other-host",
            owner_username="anna",
            identity_public_key="00" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    members = SqliteSpaceRemoteMemberRepo(db)
    yield (
        make_check_space_writer(space_repo=spaces, remote_member_repo=members),
        members,
        spaces,
    )
    await db.shutdown()


def _write(event_type, payload=None, *, sender=FOLLOWER, space_id=SPACE_ID):
    return FederationEvent(
        msg_id="m1",
        event_type=event_type,
        from_instance=sender,
        to_instance="us",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload if payload is not None else {"author": "u-follower"},
        space_id=space_id,
    )


async def _run(step, event):
    ctx = InboundContext()
    ctx.event = event
    await step(ctx)
    return ctx.early_response


async def _seat(members, instance_id, user_id, role):
    await members.add(
        space_id=SPACE_ID,
        instance_id=instance_id,
        user_id=user_id,
        user_pk=None,
        display_name=None,
        role=role,
    )


async def test_every_write_type_is_refused_from_a_follower_household(gate):
    """Enumerating the frozenset, not a hand-written list: a content type
    added tomorrow is covered the moment it is classified as a write."""
    step, members, _ = gate
    await _seat(members, FOLLOWER, "u-follower", SpaceRole.SUBSCRIBER.value)
    for event_type in SPACE_WRITE_EVENT_TYPES:
        refused = await _run(step, _write(event_type))
        assert refused == REFUSED, event_type


async def test_a_member_household_writes_every_type_freely(gate):
    step, members, _ = gate
    await _seat(members, MEMBER, "u-real", SpaceRole.MEMBER.value)
    for event_type in SPACE_WRITE_EVENT_TYPES:
        refused = await _run(step, _write(event_type, sender=MEMBER))
        assert refused is None, event_type


async def test_a_kicked_household_does_not_read_as_a_stranger(gate):
    """``remove`` tombstones rather than deletes, and every LIVE-roster
    read filters tombstones — so a gate that used one of those reads
    would turn a kick into a bypass."""
    step, members, _ = gate
    await _seat(members, MEMBER, "u-real", SpaceRole.MEMBER.value)
    assert (
        await _run(step, _write(FederationEventType.SPACE_POST_CREATED, sender=MEMBER))
        is None
    )
    await members.remove(SPACE_ID, MEMBER, "u-real")
    refused = await _run(
        step,
        _write(FederationEventType.SPACE_POST_CREATED, sender=MEMBER),
    )
    assert refused == REFUSED


async def test_the_payload_author_is_never_an_input(gate):
    """The sender writes the author field. Naming a stranger, a real
    member of another household, or nobody at all changes nothing."""
    step, members, _ = gate
    await _seat(members, FOLLOWER, "u-follower", SpaceRole.SUBSCRIBER.value)
    await _seat(members, MEMBER, "u-real", SpaceRole.MEMBER.value)
    for author in ("u-real", "u-nobody", "", None):
        refused = await _run(
            step,
            _write(
                FederationEventType.SPACE_POST_CREATED,
                {"author": author, "content": "hi"},
            ),
        )
        assert refused == REFUSED, author


async def test_a_household_with_one_real_member_may_still_write(gate):
    """The unit is the household: a follower seat next to a member seat
    does not demote the household."""
    step, members, _ = gate
    await _seat(members, FOLLOWER, "u-follower", SpaceRole.SUBSCRIBER.value)
    await _seat(members, FOLLOWER, "u-real", SpaceRole.MEMBER.value)
    assert (
        await _run(step, _write(FederationEventType.SPACE_TASK_CREATED, {"id": "t1"}))
        is None
    )


async def test_an_unattributable_write_is_refused(gate):
    """No routing space_id and none in the payload. Several handlers key
    on a bare row id (a comment, an RSVP, a calendar delete), so a write
    we cannot pin to a space is a write we cannot judge."""
    step, members, _ = gate
    await _seat(members, FOLLOWER, "u-follower", SpaceRole.SUBSCRIBER.value)
    refused = await _run(
        step,
        _write(
            FederationEventType.SPACE_CALENDAR_EVENT_DELETED,
            {"event_id": "e1"},
            space_id="",
        ),
    )
    assert refused == REFUSED


async def test_a_household_with_no_seat_is_not_gated(gate):
    """Roster convergence: a household seated on the host before the
    gossip reached us legitimately has no row here, and refusing would
    drop real members' content whenever a mirror lagged. This is the ONE
    leniency, and the tombstone test above is why it cannot be reached by
    a household we removed."""
    step, _members, _ = gate
    refused = await _run(
        step,
        _write(FederationEventType.SPACE_POST_CREATED, sender="never-met-them"),
    )
    assert refused is None
