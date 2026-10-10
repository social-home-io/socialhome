"""Release-blocker protocol tests: who a §D1b join stub names as its owner
(migration 0093, ``socialhome/space_owner_statement.py``).

A household invited by somebody other than the owner seats the stub under
the claimed owner ONLY with the owner's signed statement binding this space
to the authority key the snapshot pins, plus the inviter's proof that it
holds that key's seed. Anything less gets the pre-0093 result: the stub
names the inviter itself, with no inviting-admin trust. A stub nobody here
accepted yet gives way to the real owner's invite, dropping everything the
earlier inviter brought; an accepted one stays (trust on first use).

The attack these pin down: a paired non-member MAL pre-seats a stub for
owner R's real space claiming "R owns it, I am its delegated admin", hoping
to become host-equivalent there once R invites us for real.
"""

from __future__ import annotations

import json

import orjson
from datetime import datetime, timezone
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialhome.authority_cert import sign_authority_cert
from socialhome.crypto import (
    derive_instance_id,
    ed25519_public_key,
    generate_space_keypair,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)
from socialhome.federation.host_equivalence import acts_as_host
from socialhome.federation.private_invite_handler import PrivateSpaceInviteHandler
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.sync.space.exporter import (
    SENTINEL_RESOURCE,
    serialise_chunk,
)
from socialhome.federation.sync.space.receiver import SpaceSyncReceiver
from socialhome.federation.sync_manager import (
    SYNC_REJECT_NOT_SEATED,
    SyncSessionManager,
    make_sync_reject_reason,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories import SqliteUserRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_authority_pin import (
    apply_authority_cert,
    owner_statement_for_delegated,
    owner_statement_via,
)
from socialhome.services.federation_inbound import SpaceMembershipInboundHandlers
from socialhome.services.space_service import (
    build_space_snapshot_for_federation,
    store_owner_statement,
)
from socialhome.space_owner_statement import (
    MAX_STATEMENT_AGE_MS,
    sign_inviter_proof,
    sign_owner_statement,
)

pytestmark = pytest.mark.security

SPACE = "sp-real"
R_SEED = b"\x61" * 32
R = derive_instance_id(ed25519_public_key(R_SEED))
MAL_SEED = b"\x62" * 32
MAL = derive_instance_id(ed25519_public_key(MAL_SEED))
ADMIN = derive_instance_id(ed25519_public_key(b"\x63" * 32))
JOINER = derive_instance_id(ed25519_public_key(b"\x64" * 32))


def _meta(space_pk_hex: str, owner: str, **over) -> dict:
    meta = {
        "name": "Real space",
        "owner_instance_id": owner,
        "owner_username": "owner",
        "identity_public_key": space_pk_hex,
        "config_sequence": 3,
        "space_type": "private",
        "join_mode": "invite_only",
        "features": {"delegated_admin_authority": True},
    }
    meta.update(over)
    return meta


def _statement(space_kp, *, seed=R_SEED, space_id=SPACE) -> dict:
    return sign_owner_statement(
        space_id=space_id,
        owner_seed=seed,
        authority_pk_hex=space_kp.public_key.hex(),
        authority_key_epoch=0,
    )


def _proof(space_kp, *, inviter: str, owner: str = R) -> dict:
    return sign_inviter_proof(
        space_id=SPACE,
        owner_instance_id=owner,
        inviter_instance_id=inviter,
        space_seed=space_kp.private_key,
    )


def _invite(sender: str, meta: dict, *, token: str, user: str = "u-joe"):
    return FederationEvent(
        msg_id=f"m-{token}",
        event_type=FederationEventType.SPACE_PRIVATE_INVITE,
        from_instance=sender,
        to_instance=JOINER,
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={
            "space_id": SPACE,
            "invite_token": token,
            "invitee_user_id": user,
            "inviter_user_id": f"u-{sender[:4]}",
            "space_meta": meta,
        },
        space_id=SPACE,
    )


@pytest.fixture
def joiner(db):
    spaces = SqliteSpaceRepo(db)
    seats = SqliteSpaceRemoteMemberRepo(db)
    handler = PrivateSpaceInviteHandler(
        bus=EventBus(), space_repo=spaces, remote_member_repo=seats
    )
    handler._own_instance_id = JOINER
    return SimpleNamespace(db=db, spaces=spaces, seats=seats, handler=handler)


_MAL_ROSTER = [
    {"user_id": "u-mal", "instance_id": MAL, "role": "admin"},
]


async def _mal_preseats(j, mal_kp, **extra) -> None:
    """MAL claims R owns the space and MAL is its delegated admin."""
    await j.handler._on_invite(
        _invite(
            MAL,
            _meta(mal_kp.public_key.hex(), R, roster=_MAL_ROSTER, **extra),
            token="t-mal",
        )
    )


async def _seated(j):
    space = await j.spaces.get(SPACE)
    return (
        space.owner_instance_id if space else None,
        await j.spaces.get_invited_by(SPACE),
        space.identity_public_key if space else None,
    )


async def test_mal_without_the_owners_statement_seats_only_itself(joiner):
    mal_kp = generate_space_keypair()
    await _mal_preseats(joiner, mal_kp)
    # The pre-0093 result: the stub names MAL itself — nothing gained.
    assert await _seated(joiner) == (MAL, None, mal_kp.public_key.hex())


@pytest.mark.parametrize(
    "case", ["relayed_statement_no_seed", "forged", "other_space", "other_key"]
)
async def test_mal_cannot_claim_the_real_owner(joiner, case):
    """Every way MAL could dress up the claim fails closed to "MAL itself"."""
    real_kp = generate_space_keypair()  # R's real space key
    mal_kp = generate_space_keypair()
    if case == "relayed_statement_no_seed":
        # MAL (a plain member of R's space) relays R's genuine statement for
        # R's real key, but cannot prove it holds that key's seed.
        meta = _meta(
            real_kp.public_key.hex(),
            R,
            owner_statement=_statement(real_kp),
            inviter_proof=_proof(mal_kp, inviter=MAL),
        )
    elif case == "forged":
        meta = _meta(
            mal_kp.public_key.hex(),
            R,
            owner_statement={
                **_statement(mal_kp, seed=MAL_SEED),
                "owner_instance_id": R,
            },
            inviter_proof=_proof(mal_kp, inviter=MAL),
        )
    elif case == "other_space":
        meta = _meta(
            mal_kp.public_key.hex(),
            R,
            owner_statement=_statement(mal_kp, space_id="sp-other"),
            inviter_proof=_proof(mal_kp, inviter=MAL),
        )
    else:  # R's statement for R's key, pinned key is MAL's
        meta = _meta(
            mal_kp.public_key.hex(),
            R,
            owner_statement=_statement(real_kp),
            inviter_proof=_proof(mal_kp, inviter=MAL),
        )
    await joiner.handler._on_invite(_invite(MAL, meta, token="t-mal"))
    owner, invited_by, _key = await _seated(joiner)
    assert (owner, invited_by) == (MAL, None)


@pytest.mark.parametrize("claimed", ["JOINER", "not-a-derived-id"])
async def test_impossible_owner_claims_fall_back(joiner, claimed):
    kp = generate_space_keypair()
    owner = JOINER if claimed == "JOINER" else claimed
    await joiner.handler._on_invite(
        _invite(MAL, _meta(kp.public_key.hex(), owner), token="t-x")
    )
    assert (await _seated(joiner))[:2] == (MAL, None)


async def test_a_pending_preseat_is_never_displaced(joiner):
    """First seated wins while its invitation is pending (as before 0093) —
    even against the proven owner, whatever the statements' ages."""
    mal_kp = generate_space_keypair()
    await _mal_preseats(joiner, mal_kp)
    real_kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(
            R,
            _meta(real_kp.public_key.hex(), R, owner_statement=_statement(real_kp)),
            token="t-r",
        )
    )
    assert await _seated(joiner) == (MAL, None, mal_kp.public_key.hex())
    assert (await joiner.spaces.get_invitation_by_token("t-mal"))["status"] == (
        "pending"
    )
    assert await joiner.spaces.get_invitation_by_token("t-r") is None


async def test_the_real_owners_invite_replaces_a_dead_unproven_preseat(joiner):
    """Once MAL's invitation is no longer pending (declined / expired) and
    nobody accepted it, R's proven invite replaces the dust: MAL loses
    everything it brought — owner, key, its seats and household rows."""
    mal_kp = generate_space_keypair()
    await _mal_preseats(joiner, mal_kp)
    assert await joiner.seats.get(SPACE, MAL, "u-mal") is not None
    await joiner.spaces.update_invitation_status(
        (await joiner.spaces.get_invitation_by_token("t-mal"))["id"], "declined"
    )

    real_kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(
            R,
            _meta(
                real_kp.public_key.hex(),
                R,
                owner_statement=_statement(real_kp),
                roster=[{"user_id": "u-r", "instance_id": R, "role": "owner"}],
            ),
            token="t-r",
        )
    )
    assert await _seated(joiner) == (R, None, real_kp.public_key.hex())
    assert await joiner.seats.get(SPACE, MAL, "u-mal") is None
    assert MAL not in await joiner.spaces.list_member_instances(SPACE)
    assert (await joiner.spaces.get_invitation_by_token("t-mal"))["status"] == (
        "declined"
    )
    assert (await joiner.spaces.get_invitation_by_token("t-r"))["status"] == ("pending")
    space = await joiner.spaces.get(SPACE)
    assert space.owner_statement is not None  # R's, kept for relaying


async def test_an_accepted_stub_is_never_taken_over(joiner, db):
    """Trust on first use: once a member here joined, a later invite naming
    another owner is refused, whoever sends it."""
    real_kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(
            R,
            _meta(real_kp.public_key.hex(), R, owner_statement=_statement(real_kp)),
            token="t-r",
        )
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('joe','u-joe','Joe')"
    )
    await joiner.spaces.save_member(
        SpaceMember(space_id=SPACE, user_id="u-joe", role="member", joined_at="t")
    )
    mal_kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(
            MAL,
            _meta(
                mal_kp.public_key.hex(),
                MAL,
                owner_statement=_statement(mal_kp, seed=MAL_SEED),
            ),
            token="t-mal",
            user="u-ann",
        )
    )
    assert await _seated(joiner) == (R, None, real_kp.public_key.hex())
    assert await joiner.spaces.get_invitation_by_token("t-mal") is None


async def test_an_honest_admin_with_the_owners_statement_is_trusted(joiner):
    kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(
            ADMIN,
            _meta(
                kp.public_key.hex(),
                R,
                owner_statement=_statement(kp),
                inviter_proof=_proof(kp, inviter=ADMIN),
                roster=[{"user_id": "u-admin", "instance_id": ADMIN, "role": "admin"}],
            ),
            token="t-1",
        )
    )
    assert await _seated(joiner) == (R, ADMIN, kp.public_key.hex())
    space = await joiner.spaces.get(SPACE)
    assert await acts_as_host(joiner.spaces, joiner.seats, space, ADMIN)

    # MEDIUM-1: the same admin can invite another of our users later.
    await joiner.handler._on_invite(
        _invite(
            ADMIN,
            _meta(
                kp.public_key.hex(),
                R,
                owner_statement=_statement(kp),
                inviter_proof=_proof(kp, inviter=ADMIN),
                roster=[{"user_id": "u-admin", "instance_id": ADMIN, "role": "admin"}],
            ),
            token="t-2",
            user="u-ann",
        )
    )
    assert await joiner.spaces.get_invitation_by_token("t-2") is not None
    assert await _seated(joiner) == (R, ADMIN, kp.public_key.hex())

    # Another household — even with a valid proof of its own — may not.
    await joiner.handler._on_invite(
        _invite(
            MAL,
            _meta(
                kp.public_key.hex(),
                R,
                owner_statement=_statement(kp),
                inviter_proof=_proof(kp, inviter=MAL),
            ),
            token="t-3",
            user="u-bob",
        )
    )
    assert await joiner.spaces.get_invitation_by_token("t-3") is None


# ── The owner never answers a joining household with a terminal reject ───


async def test_the_owner_does_not_terminate_a_household_it_never_seated(db):
    spaces = SqliteSpaceRepo(db)
    seats = SqliteSpaceRemoteMemberRepo(db)
    classify = make_sync_reject_reason(spaces, seats)
    assert await classify("gone", JOINER) == "dissolved"
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type) VALUES(?, 'S', ?, 'r', 'ab', 'private')",
        (SPACE, R),
    )
    # Never seated here (an admin invited it; our roster has not learned
    # yet): not terminal.
    assert await classify(SPACE, JOINER) == "not_seated"
    # Held a seat once and lost it.
    await seats.add(
        space_id=SPACE,
        instance_id=JOINER,
        user_id="u-joe",
        user_pk=None,
        display_name="Joe",
        role=SpaceRole.MEMBER.value,
    )
    await seats.remove(SPACE, JOINER, "u-joe")
    reason, extra = await classify(SPACE, JOINER)
    assert reason == "removed" and extra["removed_at"]


async def test_an_honest_admins_real_snapshot_carries_what_the_joiner_needs(
    joiner, tmp_dir
):
    """Sender to receiver: the delegated admin household's real
    ``build_space_snapshot_for_federation`` relays the owner's stored
    statement and adds its seed proof; the joiner seats R + trusts ADMIN."""
    kp = generate_space_keypair()
    admin_db = AsyncDatabase(tmp_dir / "admin.db", batch_timeout_ms=10)
    await admin_db.startup()
    try:
        admin_spaces = SqliteSpaceRepo(admin_db, key_manager=KeyManager(b"\x0f" * 32))
        await admin_spaces.save(
            Space(
                id=SPACE,
                name="Real space",
                owner_instance_id=R,
                owner_username="owner",
                identity_public_key=kp.public_key.hex(),
                config_sequence=3,
                features=SpaceFeatures(delegated_admin_authority=True),
                space_type=SpaceType.PRIVATE,
                join_mode=JoinMode.INVITE_ONLY,
            )
        )
        await admin_spaces.set_space_seed(SPACE, kp.private_key)
        await admin_spaces.set_owner_statement(
            SPACE,
            json.dumps(_statement(kp), separators=(",", ":"), sort_keys=True),
        )
        admin_seats = SqliteSpaceRemoteMemberRepo(admin_db)
        await admin_seats.add(
            space_id=SPACE,
            instance_id=R,
            user_id="u-r",
            user_pk=None,
            display_name="R",
            role=SpaceRole.MEMBER.value,
        )
        space = await admin_spaces.get(SPACE)
        meta = await build_space_snapshot_for_federation(
            space,
            space_repo=admin_spaces,
            remote_member_repo=admin_seats,
            user_repo=SqliteUserRepo(admin_db),
            own_instance_id=ADMIN,
            owner_statement=owner_statement_via(
                SimpleNamespace(own_instance_id=ADMIN), space
            ),
        )
    finally:
        await admin_db.shutdown()
    assert "inviter_proof" in meta and "owner_statement" in meta
    # The admin's own seat, as the admin household's roster would carry it.
    meta["roster"].append({"user_id": "u-admin", "instance_id": ADMIN, "role": "admin"})
    await joiner.handler._on_invite(_invite(ADMIN, meta, token="t-e2e"))
    assert await _seated(joiner) == (R, ADMIN, kp.public_key.hex())


# ── Round 3: an unproven claim never replaces; an ex-admin's old key lapses ─


async def test_an_unproven_claimant_never_replaces_the_owners_pending_stub(joiner):
    """MAL naming ITSELF owner (no statement) must not purge R's genuine,
    still-unaccepted invitation, nor expire it."""
    real_kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(
            R,
            _meta(real_kp.public_key.hex(), R, owner_statement=_statement(real_kp)),
            token="t-r",
        )
    )
    mal_kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(MAL, _meta(mal_kp.public_key.hex(), MAL), token="t-mal")
    )
    assert await _seated(joiner) == (R, None, real_kp.public_key.hex())
    assert (await joiner.spaces.get_invitation_by_token("t-r"))["status"] == "pending"
    assert await joiner.spaces.get_invitation_by_token("t-mal") is None


async def test_a_self_proven_claimant_never_replaces_a_statement_backed_stub(joiner):
    """Even with a valid statement of its OWN (naming itself), MAL cannot
    replace a pending stub the real owner's statement backs: two proven
    claims → the first stays."""
    real_kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(
            R,
            _meta(real_kp.public_key.hex(), R, owner_statement=_statement(real_kp)),
            token="t-r",
        )
    )
    mal_kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(
            MAL,
            _meta(
                mal_kp.public_key.hex(),
                MAL,
                owner_statement=_statement(mal_kp, seed=MAL_SEED),
            ),
            token="t-mal",
        )
    )
    assert await _seated(joiner) == (R, None, real_kp.public_key.hex())


async def test_a_stub_somebody_once_accepted_is_never_replaced(joiner, db):
    """Accepted, then everybody left: still not an unaccepted invitation."""
    mal_kp = generate_space_keypair()
    await _mal_preseats(joiner, mal_kp)
    await joiner.spaces.update_invitation_status(
        (await joiner.spaces.get_invitation_by_token("t-mal"))["id"], "accepted"
    )
    real_kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(
            R,
            _meta(real_kp.public_key.hex(), R, owner_statement=_statement(real_kp)),
            token="t-r",
        )
    )
    assert (await _seated(joiner))[0] == MAL


# The review's probe: X was a delegated admin; R demoted it and rotated the
# authority key (epoch 0 → 1). X still holds the OLD seed and R's statement
# for the OLD key, and invites a fresh household H on the old key.

X_SEED = b"\x65" * 32
X = derive_instance_id(ed25519_public_key(X_SEED))


async def _x_invites_on_the_old_key(joiner, old_kp, *, statement_age_ms: int = 0):
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    old_statement = sign_owner_statement(
        space_id=SPACE,
        owner_seed=R_SEED,
        authority_pk_hex=old_kp.public_key.hex(),
        authority_key_epoch=0,
        issued_at_ms=now - statement_age_ms,
    )
    await joiner.handler._on_invite(
        _invite(
            X,
            _meta(
                old_kp.public_key.hex(),
                R,
                owner_statement=old_statement,
                inviter_proof=_proof(old_kp, inviter=X),
                roster=[{"user_id": "u-x", "instance_id": X, "role": "admin"}],
            ),
            token="t-x",
        )
    )


async def test_an_ex_admin_on_the_old_key_loses_us_when_the_owner_answers(joiner):
    """Within the statement window X is trusted at first — until our first
    catch-up reaches R: R never seated us, answers with its CURRENT
    statement (new key, epoch 1), and the owner wins."""
    old_kp = generate_space_keypair()
    await _x_invites_on_the_old_key(joiner, old_kp)
    space = await joiner.spaces.get(SPACE)
    assert (space.owner_instance_id, await joiner.spaces.get_invited_by(SPACE)) == (
        R,
        X,
    )
    assert await acts_as_host(joiner.spaces, joiner.seats, space, X)

    new_kp = generate_space_keypair()
    current = sign_owner_statement(
        space_id=SPACE,
        owner_seed=R_SEED,
        authority_pk_hex=new_kp.public_key.hex(),
        authority_key_epoch=1,
    )
    handlers = SpaceMembershipInboundHandlers(
        bus=EventBus(), space_repo=joiner.spaces, seat_repo=joiner.seats
    )
    answer = FederationEvent(
        msg_id="m-rej",
        event_type=FederationEventType.SPACE_SYNC_REJECTED,
        from_instance=R,
        to_instance=JOINER,
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={
            "sync_id": "s-1",
            "space_id": SPACE,
            "reason": SYNC_REJECT_NOT_SEATED,
            "owner_statement": current,
        },
        space_id=SPACE,
    )
    # Only the owner's answer counts.
    await handlers._on_sync_rejected(replace(answer, from_instance=X))
    assert await joiner.spaces.get_invited_by(SPACE) == X
    await handlers._on_sync_rejected(answer)
    space = await joiner.spaces.get(SPACE)
    assert await joiner.spaces.get_invited_by(SPACE) is None
    assert (space.identity_public_key, space.authority_key_epoch) == (
        new_kp.public_key.hex(),
        1,
    )
    assert not await acts_as_host(joiner.spaces, joiner.seats, space, X)
    assert space.archived is False  # not terminal: we keep our copy


async def test_an_ex_admins_stale_statement_is_refused_outright(joiner):
    old_kp = generate_space_keypair()
    await _x_invites_on_the_old_key(
        joiner, old_kp, statement_age_ms=MAX_STATEMENT_AGE_MS + 60_000
    )
    assert (await _seated(joiner))[:2] == (X, None)


async def test_the_trust_lapses_once_the_held_statement_ages_out(joiner):
    """R never reachable: X's trust ends when the statement we hold for the
    key we pin grows older than the window."""
    old_kp = generate_space_keypair()
    await _x_invites_on_the_old_key(joiner, old_kp)
    space = await joiner.spaces.get(SPACE)
    held = json.loads(space.owner_statement)
    held["issued_at"] -= MAX_STATEMENT_AGE_MS + 1
    await joiner.spaces.set_owner_statement(SPACE, json.dumps(held))
    space = await joiner.spaces.get(SPACE)
    assert not await acts_as_host(joiner.spaces, joiner.seats, space, X)


async def test_the_owner_answers_a_never_seated_household_with_its_statement(db):
    """Owner side of the recovery: a BEGIN from a household this host never
    seated gets a non-terminal reject carrying the owner's statement."""
    spaces = SqliteSpaceRepo(db)
    seats = SqliteSpaceRemoteMemberRepo(db)
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type) VALUES(?, 'S', ?, 'r', 'ab', 'private')",
        (SPACE, R),
    )
    statement = {"owner_statement_sig": "x"}

    async def _statement_for(space_id):
        return statement

    async def _not_a_member(space_id, instance_id):
        return False

    mgr = SyncSessionManager(
        MagicMock(),
        check_member=_not_a_member,
        reject_reason=make_sync_reject_reason(spaces, seats),
        owner_statement_for=_statement_for,
    )
    decision = await mgr.begin_session(
        sync_id="s-9",
        space_id=SPACE,
        requester_instance_id=JOINER,
        provider_instance_id=R,
    )
    assert decision.next_event is FederationEventType.SPACE_SYNC_REJECTED
    assert decision.next_payload["reason"] == SYNC_REJECT_NOT_SEATED
    assert decision.next_payload["owner_statement"] == statement


async def test_a_join_in_flight_is_never_told_removed(db):
    """An accepted invitation, or a live seat whose household row has not
    landed yet (the accept writes the seat first), is a join in flight —
    ``not_seated``, never the terminal ``removed``."""
    spaces = SqliteSpaceRepo(db)
    seats = SqliteSpaceRemoteMemberRepo(db)
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type) VALUES(?, 'S', ?, 'r', 'ab', 'private')",
        (SPACE, R),
    )
    await spaces.save_remote_invitation(
        space_id=SPACE,
        invited_by="u-r",
        remote_instance_id=JOINER,
        remote_user_id="u-joe",
        invite_token="t-acc",
        space_display_hint=None,
    )
    row = await spaces.get_invitation_by_token("t-acc")
    await spaces.update_invitation_status(row["id"], "accepted")
    classify = make_sync_reject_reason(spaces, seats)
    assert await classify(SPACE, JOINER) == SYNC_REJECT_NOT_SEATED
    await seats.add(
        space_id=SPACE,
        instance_id=JOINER,
        user_id="u-joe",
        user_pk=None,
        display_name="Joe",
        role=SpaceRole.MEMBER.value,
    )
    assert await classify(SPACE, JOINER) == SYNC_REJECT_NOT_SEATED


async def test_a_held_statement_for_the_pinned_key_is_only_replaced_by_a_newer(
    joiner,
):
    kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(
            R,
            _meta(kp.public_key.hex(), R, owner_statement=_statement(kp)),
            token="t-r",
        )
    )
    held = (await joiner.spaces.get(SPACE)).owner_statement
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    older = sign_owner_statement(
        space_id=SPACE,
        owner_seed=R_SEED,
        authority_pk_hex=kp.public_key.hex(),
        authority_key_epoch=0,
        issued_at_ms=now - 60_000,
    )
    canonical = json.dumps(older, separators=(",", ":"), sort_keys=True)
    assert await store_owner_statement(joiner.spaces, SPACE, canonical) is False
    assert (await joiner.spaces.get(SPACE)).owner_statement == held
    # A statement for another key (a pin we moved on from) always gives way.
    other = json.loads(held)
    other["authority_pk"] = "cd" * 32
    await joiner.spaces.set_owner_statement(SPACE, json.dumps(other))
    assert await store_owner_statement(joiner.spaces, SPACE, canonical) is True


# ── Round 4: rotated keys keep an honest admin trusted ────────────────────


def _cert(kp, epoch: int) -> dict:
    return sign_authority_cert(
        space_id=SPACE,
        owner_instance_id=R,
        owner_seed=R_SEED,
        owner_pk_hex=ed25519_public_key(R_SEED).hex(),
        authority_pk_hex=kp.public_key.hex(),
        key_epoch=epoch,
    )


async def test_an_admin_invite_into_a_rotated_space_is_trusted(joiner):
    """Epoch >= 1: the relayed cert is applied BEFORE the inviter is
    recorded, and statement + proof name that epoch."""
    kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(
            ADMIN,
            _meta(
                kp.public_key.hex(),
                R,
                authority_cert=_cert(kp, 3),
                authority_key_epoch=3,
                owner_statement=sign_owner_statement(
                    space_id=SPACE,
                    owner_seed=R_SEED,
                    authority_pk_hex=kp.public_key.hex(),
                    authority_key_epoch=3,
                ),
                inviter_proof=sign_inviter_proof(
                    space_id=SPACE,
                    owner_instance_id=R,
                    inviter_instance_id=ADMIN,
                    space_seed=kp.private_key,
                    authority_key_epoch=3,
                ),
                roster=[{"user_id": "u-admin", "instance_id": ADMIN, "role": "admin"}],
            ),
            token="t-ep",
        )
    )
    space = await joiner.spaces.get(SPACE)
    assert (space.owner_instance_id, space.authority_key_epoch) == (R, 3)
    assert await joiner.spaces.get_invited_by(SPACE) == ADMIN
    assert await acts_as_host(joiner.spaces, joiner.seats, space, ADMIN)


def _paired_receiver(joiner, seeds: dict[str, bytes]) -> SpaceSyncReceiver:
    """A §25.6 receiver for which every household in *seeds* is a paired
    peer (its identity key known)."""

    async def _get_instance(instance_id):
        seed = seeds.get(instance_id)
        if seed is None:
            return None
        return SimpleNamespace(
            sig_suite="ed25519",
            remote_identity_pk=ed25519_public_key(seed).hex(),
            remote_pq_identity_pk=None,
        )

    return SpaceSyncReceiver(
        bus=EventBus(),
        encoder=FederationEncoder(b"\x09" * 32),
        crypto=MagicMock(),
        federation_repo=SimpleNamespace(get_instance=_get_instance),  # type: ignore[arg-type]
        space_repo=joiner.spaces,
        space_post_repo=MagicMock(),
        space_task_repo=MagicMock(),
        page_repo=MagicMock(),
        sticky_repo=MagicMock(),
        space_calendar_repo=MagicMock(),
        gallery_repo=MagicMock(),
    )


async def _sentinel(receiver, *, sender: str, seed: bytes, **extra) -> None:
    envelope = {
        "sync_id": f"s-{sender[:4]}",
        "resource": SENTINEL_RESOURCE,
        "space_id": SPACE,
        "is_last": True,
        **extra,
    }
    envelope["signatures"] = FederationEncoder(seed).sign_envelope_all(
        orjson.dumps(envelope), suite="ed25519"
    )
    await receiver.on_chunk(serialise_chunk(envelope), from_instance=sender)


async def _admin_seated_at_epoch_0(joiner, kp, *, inviter=ADMIN) -> None:
    await joiner.handler._on_invite(
        _invite(
            inviter,
            _meta(
                kp.public_key.hex(),
                R,
                owner_statement=_statement(kp),
                inviter_proof=_proof(kp, inviter=inviter),
                roster=[{"user_id": "u-inv", "instance_id": inviter, "role": "admin"}],
            ),
            token="t-1",
        )
    )


async def test_an_honest_rotation_suspends_then_restores_the_trust(joiner):
    """The pin moves (0 → 1): the trust pauses. Neither the pin move nor the
    owner's fresh statement resumes it on their own — only the inviting
    admin's re-proof with the NEW seed (its sync sentinel carries one)."""
    kp = generate_space_keypair()
    await _admin_seated_at_epoch_0(joiner, kp)
    new_kp = generate_space_keypair()
    assert await joiner.spaces.adopt_authority_key(SPACE, new_kp.public_key.hex(), 1)
    space = await joiner.spaces.get(SPACE)
    assert not await acts_as_host(joiner.spaces, joiner.seats, space, ADMIN)

    admin_seed = b"\x63" * 32  # ADMIN's identity seed (see ADMIN above)
    receiver = _paired_receiver(joiner, {R: R_SEED, ADMIN: admin_seed})
    fresh = sign_owner_statement(
        space_id=SPACE,
        owner_seed=R_SEED,
        authority_pk_hex=new_kp.public_key.hex(),
        authority_key_epoch=1,
    )
    await _sentinel(receiver, sender=R, seed=R_SEED, owner_statement=fresh)
    space = await joiner.spaces.get(SPACE)
    assert not await acts_as_host(joiner.spaces, joiner.seats, space, ADMIN)

    reproof = sign_inviter_proof(
        space_id=SPACE,
        owner_instance_id=R,
        inviter_instance_id=ADMIN,
        space_seed=new_kp.private_key,
        authority_key_epoch=1,
    )
    await _sentinel(receiver, sender=ADMIN, seed=admin_seed, inviter_proof=reproof)
    space = await joiner.spaces.get(SPACE)
    assert await joiner.spaces.get_invited_by_epoch(SPACE) == 1
    assert await acts_as_host(joiner.spaces, joiner.seats, space, ADMIN)


async def test_a_demoted_inviter_never_regains_trust(joiner):
    """The review's probe: X invited us at epoch 0, then R demoted X and
    rotated. X relays R's epoch-1 cert itself (the pin moves), streams us
    R's fresh epoch-1 statement (it holds it as a member), keeps its own
    roster saying "admin" — and still is not trusted: it cannot re-prove
    without the new seed, and a proof with the OLD seed, or one replayed
    from another household, does not count."""
    x_seed = b"\x65" * 32
    old_kp = generate_space_keypair()
    await _admin_seated_at_epoch_0(joiner, old_kp, inviter=X)
    new_kp = generate_space_keypair()
    # X relays R's genuine cert for the new key (any sender may).
    space = await joiner.spaces.get(SPACE)
    await apply_authority_cert(
        joiner.spaces, space, _cert(new_kp, 1), own_instance_id=JOINER
    )
    space = await joiner.spaces.get(SPACE)
    assert (space.identity_public_key, space.authority_key_epoch) == (
        new_kp.public_key.hex(),
        1,
    )
    receiver = _paired_receiver(joiner, {X: x_seed, ADMIN: b"\x63" * 32})
    fresh = sign_owner_statement(
        space_id=SPACE,
        owner_seed=R_SEED,
        authority_pk_hex=new_kp.public_key.hex(),
        authority_key_epoch=1,
    )
    old_seed_proof = sign_inviter_proof(
        space_id=SPACE,
        owner_instance_id=R,
        inviter_instance_id=X,
        space_seed=old_kp.private_key,
        authority_key_epoch=1,
    )
    await _sentinel(
        receiver,
        sender=X,
        seed=x_seed,
        owner_statement=fresh,
        inviter_proof=old_seed_proof,
    )
    # A real admin's valid proof, replayed by X, names the other admin.
    admins_proof = sign_inviter_proof(
        space_id=SPACE,
        owner_instance_id=R,
        inviter_instance_id=ADMIN,
        space_seed=new_kp.private_key,
        authority_key_epoch=1,
    )
    await _sentinel(receiver, sender=X, seed=x_seed, inviter_proof=admins_proof)
    space = await joiner.spaces.get(SPACE)
    assert await joiner.spaces.get_invited_by(SPACE) == X
    assert await joiner.spaces.get_invited_by_epoch(SPACE) == 0
    assert not await acts_as_host(joiner.spaces, joiner.seats, space, X)


async def test_not_seated_answers_only_for_owned_delegated_spaces() -> None:
    seed = b"\x66" * 32
    own = derive_instance_id(ed25519_public_key(seed))
    fed = SimpleNamespace(own_instance_id=own, own_identity_seed=seed)
    base = Space(
        id=SPACE,
        name="S",
        owner_instance_id=own,
        owner_username="o",
        identity_public_key="aa" * 32,
        config_sequence=1,
        features=SpaceFeatures(delegated_admin_authority=True),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )
    assert owner_statement_for_delegated(fed, base, owned_only=True) is not None
    assert (
        owner_statement_for_delegated(
            fed, replace(base, features=SpaceFeatures()), owned_only=True
        )
        is None
    )
    assert (
        owner_statement_for_delegated(
            fed, replace(base, owner_instance_id="someone"), owned_only=True
        )
        is None
    )


# ── Round 5: a re-invited household is not archived by an older removal ───


def _rejected(reason: str, **extra) -> FederationEvent:
    return FederationEvent(
        msg_id="m-rej-5",
        event_type=FederationEventType.SPACE_SYNC_REJECTED,
        from_instance=R,
        to_instance=JOINER,
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={"sync_id": "s-5", "space_id": SPACE, "reason": reason, **extra},
        space_id=SPACE,
    )


@pytest.mark.parametrize("joined_after", [True, False])
async def test_removed_older_than_our_current_join_does_not_archive(
    joiner, db, joined_after
):
    """We left (the owner holds our tombstone), a delegated admin re-invited
    us, and our catch-up BEGIN reached the owner before the re-join gossip:
    the owner says ``removed`` — but our current join is newer, so we keep
    the copy. A removal newer than our join still archives."""
    kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(R, _meta(kp.public_key.hex(), R), token="t-r")
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('joe','u-joe','Joe')"
    )
    await joiner.spaces.save_member(
        SpaceMember(
            space_id=SPACE,
            user_id="u-joe",
            role="member",
            joined_at="2026-10-10T12:00:00+00:00",
        )
    )
    removed_at = "2026-10-10 11:00:00" if joined_after else "2026-10-10 13:00:00"
    handlers = SpaceMembershipInboundHandlers(
        bus=EventBus(), space_repo=joiner.spaces, seat_repo=joiner.seats
    )
    await handlers._on_sync_rejected(_rejected("removed", removed_at=removed_at))
    space = await joiner.spaces.get(SPACE)
    assert space.archived is (not joined_after)


async def test_a_removed_without_a_time_still_archives(joiner, db):
    """Pre-0093 tombstones carry no time: ``removed`` is honoured as before."""
    kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(R, _meta(kp.public_key.hex(), R), token="t-r")
    )
    handlers = SpaceMembershipInboundHandlers(
        bus=EventBus(), space_repo=joiner.spaces, seat_repo=joiner.seats
    )
    await handlers._on_sync_rejected(_rejected("removed"))
    assert (await joiner.spaces.get(SPACE)).archived is True


async def test_a_pending_invitation_past_its_ttl_still_protects_the_stub(joiner, db):
    """The invitee-side row's short ttl does not end it: it stays listed and
    acceptable, so it still protects its stub — status decides, never
    ``expires_at`` (else the user's real invitation would be displaced)."""
    mal_kp = generate_space_keypair()
    await _mal_preseats(joiner, mal_kp)
    await db.enqueue(
        "UPDATE space_invitations SET expires_at='2020-01-01T00:00:00+00:00'"
        " WHERE invite_token='t-mal'"
    )
    assert await joiner.spaces.has_pending_remote_invitation(SPACE) is True
    real_kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(
            R,
            _meta(real_kp.public_key.hex(), R, owner_statement=_statement(real_kp)),
            token="t-r",
        )
    )
    assert (await _seated(joiner))[0] != R
    row = await db.fetchone(
        "SELECT status FROM space_invitations WHERE invite_token='t-mal'"
    )
    assert row["status"] == "pending"


async def test_an_expired_by_status_invitation_protects_nothing(joiner, db):
    mal_kp = generate_space_keypair()
    await _mal_preseats(joiner, mal_kp)
    await db.enqueue(
        "UPDATE space_invitations SET status='expired' WHERE invite_token='t-mal'"
    )
    assert await joiner.spaces.has_pending_remote_invitation(SPACE) is False
    real_kp = generate_space_keypair()
    await joiner.handler._on_invite(
        _invite(
            R,
            _meta(real_kp.public_key.hex(), R, owner_statement=_statement(real_kp)),
            token="t-r",
        )
    )
    assert (await _seated(joiner))[0] == R


async def test_the_stream_that_carried_a_reproof_is_not_recorded_clean(joiner):
    """Its chunks were applied under the member rules (the re-proof lands
    only at the sentinel), so the stream must not count as a clean full
    apply — the next session re-streams under the restored trust."""
    kp = generate_space_keypair()
    await _admin_seated_at_epoch_0(joiner, kp)
    new_kp = generate_space_keypair()
    assert await joiner.spaces.adopt_authority_key(SPACE, new_kp.public_key.hex(), 1)
    admin_seed = b"\x63" * 32
    receiver = _paired_receiver(joiner, {ADMIN: admin_seed})
    reproof = sign_inviter_proof(
        space_id=SPACE,
        owner_instance_id=R,
        inviter_instance_id=ADMIN,
        space_seed=new_kp.private_key,
        authority_key_epoch=1,
    )
    with patch.object(SpaceSyncReceiver, "_record_applied", AsyncMock()) as recorded:
        await _sentinel(
            receiver,
            sender=ADMIN,
            seed=admin_seed,
            inviter_proof=reproof,
            chunk_count=0,
        )
        assert await joiner.spaces.get_invited_by_epoch(SPACE) == 1
        recorded.assert_not_awaited()
        # The next stream, under the restored trust, records as usual.
        await _sentinel(
            receiver,
            sender=ADMIN,
            seed=admin_seed,
            inviter_proof=reproof,
            chunk_count=0,
        )
        recorded.assert_awaited_once()
