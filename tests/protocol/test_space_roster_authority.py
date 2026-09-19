"""Release-blocker protocol tests: who may mutate a space's roster.

Marked ``@pytest.mark.security``.

:mod:`test_space_follower_write_gate` pins step 12 of the §24.11
pipeline — a read-only Follower household cannot write space *content*.
This file pins the other half of the same promise: it cannot rewrite the
*roster* either, nor the bans, nor the members, nor the seat it was
given. An adversarial review found six handlers that persisted whatever
the sender asked for after checking nothing but field presence.

The rule these tests encode — **roster authority**:

    A roster mutation is applied only when it comes from the space's own
    host (``spaces.owner_instance_id == event.from_instance``) or carries
    a valid space-authority signature over
    ``spaces.identity_public_key``. Nothing else — not a paired peer, not
    a member household, not a household holding a Follower seat.

Two attacker classes reach these handlers, and both are covered:

* a **stranger seated over the GFS relay** (``InstanceSource.SPACE_SESSION``)
  — its vocabulary is :data:`SPACE_SESSION_ALLOWED_EVENT_TYPES`, which
  contains the roster family, so the envelopes below are ones it can
  really deliver;
* a **QR-paired household** that also holds a Follower seat — no
  vocabulary limit at all.

The tripwire at the end enumerates the REAL application registry.
:meth:`EventDispatchRegistry.dispatch` invokes EVERY handler bound to an
event type, so a guarded handler does not shadow an unguarded sibling:
the duplicate applies the mutation the guarded one refused. That is
exactly how ``SPACE_MEMBER_ROLE_CHANGED`` shipped. Enumerating the
registry means the next duplicate fails here instead of in the wild.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    federation_service_key,
    space_remote_member_repo_key,
    space_repo_key,
)
from socialhome.config import Config
from socialhome.domain.federation import (
    SPACE_SESSION_ALLOWED_EVENT_TYPES,
    FederationEvent,
    FederationEventType,
)
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)

pytestmark = pytest.mark.security

HOSTED = "sp-we-host"
STUB = "sp-they-host"
HOST = "the-real-host"
FOLLOWER = "follower-household"
MEMBER = "member-household"
OUR_USER = "u-ours"
FOLLOWER_USER = "u-follower"

#: Every event type that can add, remove, re-role, ban or unban a seat.
#: The tripwire walks this set against the real registry.
ROSTER_MUTATING_EVENT_TYPES = (
    FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
    FederationEventType.SPACE_MEMBER_BANNED,
    FederationEventType.SPACE_MEMBER_UNBANNED,
    FederationEventType.SPACE_REMOTE_MEMBER_REMOVED,
    FederationEventType.SPACE_MEMBER_JOINED,
    FederationEventType.SPACE_MEMBER_LEFT,
    FederationEventType.SPACE_PRIVATE_INVITE,
    FederationEventType.SPACE_PRIVATE_INVITE_ACCEPT,
    FederationEventType.SPACE_PRIVATE_INVITE_DECLINE,
)


def _space(space_id: str, owner: str) -> Space:
    return Space(
        id=space_id,
        name=space_id,
        owner_instance_id=owner,
        owner_username="anna",
        identity_public_key="00" * 32,
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    """The real application — real repos, the real dispatch registry.

    Two spaces, because the two halves of every handler live on different
    households: ``HOSTED`` is ours (we are the host), ``STUB`` is a mirror
    of a space ``HOST`` runs. ``FOLLOWER`` holds a read-only seat in both;
    ``MEMBER`` holds a real one.
    """
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "roster.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )
    app = create_app(cfg)
    client = await aiohttp_client(app)
    spaces = app[space_repo_key]
    members = app[space_remote_member_repo_key]
    fed = app[federation_service_key]

    await spaces.save(_space(HOSTED, fed.own_instance_id))
    await spaces.save(_space(STUB, HOST))
    await spaces.save_member(
        SpaceMember(
            space_id=HOSTED,
            user_id=OUR_USER,
            role=SpaceRole.OWNER.value,
            joined_at="2026-01-01 00:00:00",
        )
    )
    await spaces.save_member(
        SpaceMember(
            space_id=STUB,
            user_id=OUR_USER,
            role=SpaceRole.MEMBER.value,
            joined_at="2026-01-01 00:00:00",
        )
    )
    for space_id in (HOSTED, STUB):
        await members.add(
            space_id=space_id,
            instance_id=FOLLOWER,
            user_id=FOLLOWER_USER,
            user_pk=None,
            display_name="Follower",
            role=SpaceRole.SUBSCRIBER.value,
        )
        await members.add(
            space_id=space_id,
            instance_id=MEMBER,
            user_id="u-real",
            user_pk=None,
            display_name="Real",
            role=SpaceRole.MEMBER.value,
        )
    yield client, app, spaces, members, fed


def _event(event_type, payload, *, sender=FOLLOWER, space_id=None):
    return FederationEvent(
        msg_id="m-forged",
        event_type=event_type,
        from_instance=sender,
        to_instance="us",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=space_id,
    )


async def _seats(members, space_id):
    return {
        (m.instance_id, m.user_id): (m.role, m.tombstoned)
        for m in await members.list_for_space_including_tombstones(space_id)
    }


# ── The attacker's own vocabulary ────────────────────────────────────


def test_the_roster_family_is_reachable_by_a_relay_seated_stranger():
    """These are not hypothetical envelopes. A household seated from an
    invite link over the GFS holds an ``InstanceSource.SPACE_SESSION``
    row whose whole vocabulary is
    :data:`SPACE_SESSION_ALLOWED_EVENT_TYPES` — and the roster family is
    in it (a Follower must hear about a dissolve, a kick, a role change).
    So every test below is an envelope a stranger can really deliver; a
    QR-paired household has no vocabulary limit at all."""
    reachable = {
        FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
        FederationEventType.SPACE_MEMBER_BANNED,
        FederationEventType.SPACE_MEMBER_UNBANNED,
        FederationEventType.SPACE_REMOTE_MEMBER_REMOVED,
        FederationEventType.SPACE_MEMBER_JOINED,
        FederationEventType.SPACE_MEMBER_LEFT,
        FederationEventType.SPACE_LOCATION_UPDATED,
    }
    assert reachable <= SPACE_SESSION_ALLOWED_EVENT_TYPES


# ── F2: self-promotion ───────────────────────────────────────────────


@pytest.mark.parametrize("space_id", [HOSTED, STUB])
async def test_a_follower_cannot_promote_its_own_seat(env, space_id):
    """F2 — the private-invite family carried a second, unguarded
    ``SPACE_MEMBER_ROLE_CHANGED`` handler next to the host-authority-gated
    one. Since dispatch runs both, the guarded refusal was decorative."""
    _client, _app, _spaces, members, fed = env
    before = await _seats(members, space_id)
    await fed._event_registry.dispatch(
        _event(
            FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
            {
                "space_id": space_id,
                "instance_id": FOLLOWER,
                "user_id": FOLLOWER_USER,
                "role": SpaceRole.ADMIN.value,
            },
            space_id=space_id,
        ),
    )
    assert await _seats(members, space_id) == before


# ── F7: bans ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("space_id", [HOSTED, STUB])
async def test_a_follower_cannot_ban_the_space_owner(env, space_id):
    """``ban_member`` inserts the ban AND deletes the member row."""
    _client, app, spaces, _members, fed = env
    await fed._event_registry.dispatch(
        _event(
            FederationEventType.SPACE_MEMBER_BANNED,
            {"user_id": OUR_USER, "reason": "because"},
            space_id=space_id,
        ),
    )
    db = app[db_key]
    rows = await db.fetchall(
        "SELECT 1 FROM space_bans WHERE space_id=?",
        (space_id,),
    )
    assert list(rows) == []
    assert await spaces.get_member(space_id, OUR_USER) is not None


async def test_a_follower_cannot_lift_a_real_ban(env):
    """The host banned somebody; clearing that is the host's call."""
    _client, app, spaces, _members, fed = env
    await spaces.ban_member(
        space_id=STUB,
        user_id="u-troll",
        banned_by="admin",
        reason="spam",
    )
    await fed._event_registry.dispatch(
        _event(
            FederationEventType.SPACE_MEMBER_UNBANNED,
            {"user_id": "u-troll"},
            space_id=STUB,
        ),
    )
    db = app[db_key]
    rows = await db.fetchall(
        "SELECT 1 FROM space_bans WHERE space_id=? AND user_id=?",
        (STUB, "u-troll"),
    )
    assert len(list(rows)) == 1


# ── F8: removing other people's members ──────────────────────────────


async def test_a_follower_cannot_remove_a_local_member_of_a_space_we_host(env):
    """F8 — the ``space_remote_members`` half of the handler was scoped to
    the sender's own household; the ``space_members`` half deleted by
    ``(space_id, user_id)`` with no check at all."""
    _client, _app, spaces, _members, fed = env
    await fed._event_registry.dispatch(
        _event(
            FederationEventType.SPACE_REMOTE_MEMBER_REMOVED,
            {"space_id": HOSTED, "user_id": OUR_USER},
            space_id=HOSTED,
        ),
    )
    assert await spaces.get_member(HOSTED, OUR_USER) is not None


# ── F1: self-invite + self-accept ────────────────────────────────────


async def test_a_follower_cannot_invite_itself_into_a_space_we_host(env):
    """``SPACE_PRIVATE_INVITE`` is the HOST inviting one of OUR users. A
    household writing an invitation for a space we host is writing itself
    a ticket — and ``_on_accept`` redeems it into a seat."""
    _client, app, _spaces, _members, fed = env
    await fed._event_registry.dispatch(
        _event(
            FederationEventType.SPACE_PRIVATE_INVITE,
            {
                "space_id": HOSTED,
                "invite_token": "forged-token",
                "invitee_user_id": FOLLOWER_USER,
                "inviter_user_id": FOLLOWER_USER,
            },
            space_id=HOSTED,
        ),
    )
    db = app[db_key]
    rows = await db.fetchall(
        "SELECT 1 FROM space_invitations WHERE invite_token=?",
        ("forged-token",),
    )
    assert list(rows) == []


async def test_a_follower_seat_survives_a_forged_invite_and_accept(env):
    """End to end: the two events together were the escalation. Even with
    an invitation row somehow present, the accept must not raise a live
    Follower seat to ``member`` — ``add``'s ``ON CONFLICT … DO UPDATE SET
    role`` is what turned a re-seat into a promotion."""
    _client, _app, spaces, members, fed = env
    await spaces.save_remote_invitation(
        HOSTED,
        invited_by=FOLLOWER_USER,
        remote_instance_id=FOLLOWER,
        remote_user_id=FOLLOWER_USER,
        invite_token="planted",
    )
    await fed._event_registry.dispatch(
        _event(
            FederationEventType.SPACE_PRIVATE_INVITE_ACCEPT,
            {"invite_token": "planted", "invitee_user_id": FOLLOWER_USER},
            space_id=HOSTED,
        ),
    )
    seat = await members.get(HOSTED, FOLLOWER, FOLLOWER_USER)
    assert seat is not None
    assert seat.role == SpaceRole.SUBSCRIBER.value


async def test_a_stranger_cannot_redeem_somebody_elses_invitation(env):
    """``get_invitation_by_token`` matches the token alone, so the accept
    has to be bound to the household the invitation was addressed to."""
    _client, _app, spaces, members, fed = env
    await spaces.save_remote_invitation(
        HOSTED,
        invited_by=OUR_USER,
        remote_instance_id="invited-household",
        remote_user_id="u-invited",
        invite_token="theirs",
    )
    await fed._event_registry.dispatch(
        _event(
            FederationEventType.SPACE_PRIVATE_INVITE_ACCEPT,
            {"invite_token": "theirs", "invitee_user_id": "u-invited"},
            space_id=HOSTED,
        ),
    )
    assert await members.get(HOSTED, FOLLOWER, "u-invited") is None
    assert await members.get(HOSTED, "invited-household", "u-invited") is None


# ── F6: location pins ────────────────────────────────────────────────


async def test_a_follower_cannot_pin_itself_on_the_space_map(env):
    _client, app, _spaces, _members, fed = env
    await fed._event_registry.dispatch(
        _event(
            FederationEventType.SPACE_LOCATION_UPDATED,
            {
                "space_id": STUB,
                "user_id": FOLLOWER_USER,
                "mode": "gps",
                "lat": 48.1351,
                "lon": 11.582,
            },
            space_id=STUB,
        ),
    )
    db = app[db_key]
    rows = await db.fetchall("SELECT 1 FROM space_remote_member_locations", ())
    assert list(rows) == []


async def test_a_location_pin_is_written_to_the_routing_space_not_the_payloads(env):
    """The §24.11 writer gate reads ``event.space_id or payload`` — the
    handler read the inverse, so routing ``A`` + payload ``B`` passed the
    gate on the space the sender may write and landed in the one it may
    not."""
    _client, app, _spaces, _members, fed = env
    await fed._event_registry.dispatch(
        _event(
            FederationEventType.SPACE_LOCATION_UPDATED,
            {
                "space_id": HOSTED,
                "user_id": "u-real",
                "mode": "gps",
                "lat": 1.0,
                "lon": 2.0,
            },
            sender=MEMBER,
            space_id=STUB,
        ),
    )
    db = app[db_key]
    rows = await db.fetchall(
        "SELECT space_id FROM space_remote_member_locations",
        (),
    )
    assert list(rows) == []


# ── The tripwire ─────────────────────────────────────────────────────


@pytest.mark.parametrize("event_type", ROSTER_MUTATING_EVENT_TYPES)
@pytest.mark.parametrize("space_id", [HOSTED, STUB])
async def test_every_registered_roster_handler_refuses_an_unauthorised_sender(
    env,
    event_type,
    space_id,
):
    """Walk the REAL registry: every handler bound to a roster-mutating
    event type is invoked with a forged envelope from a household that is
    neither the host nor a space-authority signer, and the roster, the
    bans and the members must come out identical.

    ``dispatch`` runs every handler, so this is the test that catches a
    future duplicate registered next to a guarded one."""
    _client, app, spaces, members, fed = env
    db = app[db_key]
    # The target is ANOTHER household's seat on purpose. A household
    # dropping its OWN seat (``SPACE_REMOTE_MEMBER_REMOVED`` naming its
    # own user) is legitimate — "our user left your space" — so aiming
    # the forged payload at itself would test the wrong thing.
    payload = {
        "space_id": space_id,
        "instance_id": MEMBER,
        "user_id": "u-real",
        "role": SpaceRole.ADMIN.value,
        "invite_token": f"forged-{event_type.value}",
        "invitee_user_id": "u-real",
        "inviter_user_id": FOLLOWER_USER,
        "member_version": 9999,
    }
    handlers = fed._event_registry.handlers_for(event_type)
    assert handlers, f"no handler registered for {event_type.value}"

    before_seats = await _seats(members, space_id)
    before_members = [m.user_id for m in await spaces.list_members(space_id)]
    for handler in handlers:
        await handler(_event(event_type, dict(payload), space_id=space_id))

    assert await _seats(members, space_id) == before_seats
    assert [m.user_id for m in await spaces.list_members(space_id)] == before_members
    assert list(await db.fetchall("SELECT 1 FROM space_bans", ())) == []
    assert (
        list(
            await db.fetchall(
                "SELECT 1 FROM space_invitations WHERE invite_token LIKE 'forged-%'",
                (),
            )
        )
        == []
    )


# ── F9: a re-seated household is not permanently write-refused ───────


async def test_a_kicked_household_that_is_re_seated_can_write_again(env):
    """The regression the other fixes could have shipped: ``add`` never
    cleared ``tombstoned``, and the §24.11 writer gate reads tombstones.
    A household kicked and then legitimately re-invited stayed refused on
    every write — silently, because the refusal answers
    ``{"status": "ok"}`` and the sender's outbox never retries."""
    from socialhome.federation.inbound_validator import (
        InboundContext,
        make_check_space_writer,
    )

    _client, _app, spaces, members, _fed = env
    step = make_check_space_writer(space_repo=spaces, remote_member_repo=members)

    async def _refused(sender):
        ctx = InboundContext()
        ctx.event = _event(
            FederationEventType.SPACE_POST_CREATED,
            {"author": "u-real"},
            sender=sender,
            space_id=HOSTED,
        )
        await step(ctx)
        return ctx.early_response

    assert await _refused(MEMBER) is None
    await members.remove(HOSTED, MEMBER, "u-real")
    assert await _refused(MEMBER) is not None
    await members.add(
        space_id=HOSTED,
        instance_id=MEMBER,
        user_id="u-real",
        user_pk=None,
        display_name="Real",
        role=SpaceRole.MEMBER.value,
    )
    assert await _refused(MEMBER) is None
