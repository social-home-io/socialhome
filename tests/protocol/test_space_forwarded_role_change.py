"""Release-blocker protocol tests: a role change forwarded from a member
household (v_47, ``SPACE_REMOTE_ADMIN_ACTION`` ``set_member_role``).

Marked ``@pytest.mark.security``.

The host is the only household whose roster everyone trusts, so a stub's
role change is a *request*: the host re-checks it with its own data —

* the actor's live seat on the **signed sender** household (an admin
  naming another household's user as actor is dropped);
* :func:`role_change_allowed` for that seat's role (an admin moves a seat
  only between member and moderator — never makes or unmakes an admin);
* the target seat exists and is not the owner;
* the v_41 moderator floor on the target's home household;
* the owner-approval gate while ``delegated_admin_authority`` is off.

Driven through the real inbound handler, real SQLite repos and the real
services; only the federation transport is a mock.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import SpaceAdminAuthorityRevoked
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.space import SpaceFeatures, SpaceRole
from socialhome.domain.space_proposal import ProposalStatus
from socialhome.federation.private_invite_handler import PrivateSpaceInviteHandler
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_proposal_repo import SqliteSpaceProposalRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_approval_service import (
    MAX_PENDING_OWNER_APPROVALS_PER_HOUSEHOLD,
    SpaceApprovalService,
)
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService

pytestmark = pytest.mark.security

#: The stub household the forwarding actor sits on.
STUB = "house-a"
#: The household the targets live on.
OTHER = "house-c"
#: A household below the v_41 moderator floor.
OLD = "house-old"


@pytest.fixture
async def host(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "fwd-role.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    users = SqliteUserRepo(db)
    spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0d" * 32))
    remote = SqliteSpaceRemoteMemberRepo(db)
    svc = SpaceService(spaces, SqliteSpacePostRepo(db), users, bus, own_instance_id=iid)
    fed = MagicMock()
    fed.send_event = AsyncMock()
    fed.send_with_mesh_fallback = AsyncMock()
    fed.broadcast_to_space_members = AsyncMock(return_value=None)

    async def _supports(instance_id, *, min_version):
        return instance_id != OLD

    fed.peer_supports = AsyncMock(side_effect=_supports)
    fed_repo = MagicMock()
    fed_repo.get_instance = AsyncMock(return_value=None)
    fed_repo.list_instances_in_space = AsyncMock(return_value=[])
    svc.attach_federation(
        federation_service=fed,
        federation_repo=fed_repo,
        remote_member_repo=remote,
    )
    approvals = SpaceApprovalService(
        SqliteSpaceProposalRepo(db),
        spaces,
        remote,
        users,
        bus,
        own_instance_id=iid,
    )
    approvals.attach(space_service=svc)
    handler = PrivateSpaceInviteHandler(
        bus=bus,
        space_repo=spaces,
        remote_member_repo=remote,
        space_service=svc,
    )
    handler.attach_approval_service(approvals)
    user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)
    anna = await user_svc.provision(username="anna", display_name="Anna")
    bob = await user_svc.provision(username="bob", display_name="Bob")
    space = await svc.create_space(
        owner_username="anna",
        name="S",
        features=SpaceFeatures(delegated_admin_authority=True),
    )
    await svc.add_member(space.id, actor_username="anna", user_id=bob.user_id)
    seats = {
        # sender seats on the stub household
        (STUB, "u-admin"): SpaceRole.ADMIN,
        (STUB, "u-mod"): SpaceRole.MODERATOR,
        (STUB, "u-member"): SpaceRole.MEMBER,
        # target seats on another household
        (OTHER, "t-member"): SpaceRole.MEMBER,
        (OTHER, "t-mod"): SpaceRole.MODERATOR,
        (OTHER, "t-admin"): SpaceRole.ADMIN,
        (OTHER, "c-admin"): SpaceRole.ADMIN,
        (OLD, "t-old"): SpaceRole.MEMBER,
    }
    for (inst, uid), role in seats.items():
        await remote.add(
            space_id=space.id,
            instance_id=inst,
            user_id=uid,
            user_pk=None,
            display_name=uid,
            role=role,
        )
    revoked: list[SpaceAdminAuthorityRevoked] = []

    async def _grab(evt):
        revoked.append(evt)

    bus.subscribe(SpaceAdminAuthorityRevoked, _grab)

    class H:
        pass

    h = H()
    h.db, h.svc, h.fed, h.remote, h.spaces = db, svc, fed, remote, spaces
    h.handler, h.approvals, h.space, h.iid = handler, approvals, space, iid
    h.anna, h.bob, h.revoked = anna, bob, revoked
    yield h
    await db.shutdown()


def _event(space_id, *, sender, actor, params, payload_instance=None):
    payload = {
        "space_id": space_id,
        "actor_user_id": actor,
        "action": "set_member_role",
        "params": params,
    }
    if payload_instance is not None:
        # A forged claim the handler must ignore — it binds to the signer.
        payload["actor_instance_id"] = payload_instance
    return FederationEvent(
        msg_id="m-1",
        event_type=FederationEventType.SPACE_REMOTE_ADMIN_ACTION,
        from_instance=sender,
        to_instance="host",
        timestamp="2026-10-02T00:00:00+00:00",
        payload=payload,
        space_id=space_id,
    )


async def _role(h, instance_id, user_id):
    if instance_id == h.iid:
        return (await h.spaces.get_member(h.space.id, user_id)).role
    return (await h.remote.get(h.space.id, instance_id, user_id)).role


async def _forward(
    h, *, sender, actor, target, role, payload_instance=None, from_role=None
):
    inst, uid = target
    if from_role is None:
        # What an honest stub sends: the role its mirror shows (= the host's).
        seat = (
            await h.spaces.get_member(h.space.id, uid)
            if inst == h.iid
            else await h.remote.get(h.space.id, inst, uid)
        )
        from_role = seat.role if seat is not None else "member"
    await h.handler._on_remote_admin_action(
        _event(
            h.space.id,
            sender=sender,
            actor=actor,
            params={
                "instance_id": inst,
                "user_id": uid,
                "from_role": from_role,
                "role": role,
            },
            payload_instance=payload_instance,
        )
    )


#: (target seat, new role, applied when an ADMIN on the stub asks)
TRANSITIONS = [
    ((OTHER, "t-member"), "moderator", True),
    ((OTHER, "t-mod"), "member", True),
    ((OTHER, "t-member"), "admin", False),  # only the owner makes an admin
    ((OTHER, "t-admin"), "member", False),  # only the owner unmakes one
]


@pytest.mark.parametrize(("target", "role", "allowed"), TRANSITIONS)
@pytest.mark.parametrize(
    ("sender", "actor", "sender_ok"),
    [
        (STUB, "u-admin", True),  # stub admin
        (STUB, "u-mod", False),  # stub moderator — content authority only
        (STUB, "u-member", False),  # stub member
        (STUB, "c-admin", False),  # names ANOTHER household's admin as actor
    ],
)
async def test_forwarded_role_change_matrix(
    host, sender, actor, sender_ok, target, role, allowed
):
    before = await _role(host, *target)
    await _forward(
        host,
        sender=sender,
        actor=actor,
        target=target,
        role=role,
        # The impersonation row also forges the payload household claim.
        payload_instance=OTHER if actor == "c-admin" else None,
    )
    after = await _role(host, *target)
    assert after == (role if (sender_ok and allowed) else before)
    # No forwarded path ever makes or unmakes an admin, so no seed
    # rotation is ever triggered from it.
    assert host.revoked == []


async def test_the_owner_is_never_a_forwarded_target(host):
    owner = (host.iid, host.anna.user_id)
    for role in ("member", "moderator", "admin"):
        await _forward(host, sender=STUB, actor="u-admin", target=owner, role=role)
    assert await _role(host, *owner) == SpaceRole.OWNER


async def test_a_host_local_member_can_be_the_target(host):
    """``instance_id`` = the host's own id resolves to ``space_members``."""
    bob = (host.iid, host.bob.user_id)
    await _forward(host, sender=STUB, actor="u-admin", target=bob, role="moderator")
    assert await _role(host, *bob) == SpaceRole.MODERATOR


async def test_a_demoted_admin_is_refused(host):
    """The stub still thinks it holds an admin seat; the host does not."""
    await host.remote.set_role(host.space.id, STUB, "u-admin", "member")
    target = (OTHER, "t-member")
    await _forward(host, sender=STUB, actor="u-admin", target=target, role="moderator")
    assert await _role(host, *target) == SpaceRole.MEMBER


async def test_the_moderator_floor_holds_on_the_forward_path(host):
    """A target whose household is below v_41 can't store a moderator seat."""
    target = (OLD, "t-old")
    await _forward(host, sender=STUB, actor="u-admin", target=target, role="moderator")
    assert await _role(host, *target) == SpaceRole.MEMBER


async def test_an_unknown_target_or_role_is_dropped(host):
    await _forward(
        host, sender=STUB, actor="u-admin", target=(OTHER, "ghost"), role="moderator"
    )
    target = (OTHER, "t-member")
    for junk in ("owner", "subscriber", "root", ""):
        await _forward(host, sender=STUB, actor="u-admin", target=target, role=junk)
    assert await _role(host, *target) == SpaceRole.MEMBER


async def test_an_applied_change_federates_through_the_host(host):
    """The roster broadcast is what the stub (and everyone) learns from."""
    target = (OTHER, "t-member")
    await _forward(host, sender=STUB, actor="u-admin", target=target, role="moderator")
    types = [c.args[1] for c in host.fed.broadcast_to_space_members.await_args_list]
    assert FederationEventType.SPACE_MEMBER_ROLE_CHANGED in types


async def test_delegation_off_holds_it_for_the_owner_under_the_actors_matrix(host):
    """Delegation off → an owner approval, like every forwarded admin action.
    A refused transition is never queued, and approval runs the ACTOR's
    matrix (re-checked live) — never the owner's."""
    space = await host.spaces.get(host.space.id)
    await host.svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(delegated_admin_authority=False),
    )
    host.revoked.clear()  # the off-flip itself revokes; not ours
    # A forbidden one: never queued.
    await _forward(
        host, sender=STUB, actor="u-admin", target=(OTHER, "t-member"), role="admin"
    )
    assert await host.approvals.list_for_space(space.id) == []
    # An allowed one: queued, not yet applied.
    target = (OTHER, "t-member")
    await _forward(host, sender=STUB, actor="u-admin", target=target, role="moderator")
    [proposal] = await host.approvals.list_for_space(space.id)
    assert proposal["fwd_action"] == "set_member_role"
    assert proposal["owner_only"] is True
    assert await _role(host, *target) == SpaceRole.MEMBER
    out = await host.approvals.vote(
        space.id, proposal["id"], actor_username="anna", approve=True
    )
    assert out["status"] == ProposalStatus.EXECUTED.value
    assert await _role(host, *target) == SpaceRole.MODERATOR


async def test_approval_rechecks_the_actor_seat(host):
    """An admin demoted while the approval was pending can't land it."""
    space = await host.spaces.get(host.space.id)
    await host.svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(delegated_admin_authority=False),
    )
    target = (OTHER, "t-member")
    await _forward(host, sender=STUB, actor="u-admin", target=target, role="moderator")
    [proposal] = await host.approvals.list_for_space(space.id)
    await host.remote.set_role(space.id, STUB, "u-admin", "member")
    await host.approvals.vote(
        space.id, proposal["id"], actor_username="anna", approve=True
    )
    assert await _role(host, *target) == SpaceRole.MEMBER


async def test_a_host_side_admin_demotion_still_rotates(host):
    """The forward path can't demote an admin (the actor is never the owner:
    the owner always sits on the host). The owner's own demotion — the same
    ``_apply_remote_role`` the forward path runs — still publishes the v_44
    revocation that rotates the seed."""
    await host.svc.set_remote_member_role(
        host.space.id,
        actor_username="anna",
        instance_id=OTHER,
        user_id="t-admin",
        role="member",
    )
    assert [e.instance_id for e in host.revoked] == [OTHER]


def test_the_capability_gate_names_v47():
    assert FederationCapability.MIN_FOR_FORWARDED_ROLE_CHANGE == 47


# ── review hardening: the approval path ──


async def _delegation_off(h):
    await h.svc.update_config(
        h.space.id,
        actor_username="anna",
        features=SpaceFeatures(delegated_admin_authority=False),
    )
    h.revoked.clear()


def _propose_event(space_id, params):
    """A ``propose`` verb carrying a hand-built owner-approval request."""
    return FederationEvent(
        msg_id="m-propose",
        event_type=FederationEventType.SPACE_REMOTE_ADMIN_ACTION,
        from_instance=STUB,
        to_instance="host",
        timestamp="2026-10-02T00:00:00+00:00",
        payload={
            "space_id": space_id,
            "actor_user_id": "u-admin",
            "action": "propose",
            "params": params,
        },
        space_id=space_id,
    )


async def test_a_proposer_cannot_hand_build_an_owner_approval(host):
    """The review probe's exact payload: a ``propose`` verb opening a
    ``remote_admin_action`` that names ANOTHER household's admin as actor.
    Only the host's forward gate may open one, so it is dropped — and even
    after the real sender is demoted nothing lands."""
    await _delegation_off(host)
    sid = host.space.id
    await host.handler._on_remote_admin_action(
        _propose_event(
            sid,
            {
                "action": "remote_admin_action",
                "params": {
                    "fwd_action": "set_member_role",
                    "fwd_params": {
                        "instance_id": OTHER,
                        "user_id": "t-member",
                        "role": "moderator",
                    },
                    "actor_instance": OTHER,
                    "actor_user": "c-admin",
                },
            },
        )
    )
    assert await host.approvals.list_for_space(sid) == []
    await host.remote.set_role(sid, STUB, "u-admin", "member")
    assert await _role(host, OTHER, "t-member") == SpaceRole.MEMBER


async def test_approval_runs_as_the_signer_bound_proposer(host):
    """``_execute`` takes the actor from ``proposed_by_*`` (set from the
    verified sender), never from the params a row carries."""
    await _delegation_off(host)
    sid = host.space.id
    target = (OTHER, "t-member")
    await _forward(host, sender=STUB, actor="u-admin", target=target, role="moderator")
    [proposal] = await host.approvals.list_for_space(sid)
    assert proposal["proposed_by_instance"] == STUB
    assert proposal["proposed_by_user"] == "u-admin"
    # Tamper with the params' actor claim: point it at a live admin.
    row = await host.approvals._proposals.get(proposal["id"])
    await host.approvals._proposals.upsert(
        replace(
            row,
            params={**row.params, "actor_instance": OTHER, "actor_user": "c-admin"},
        )
    )
    await host.remote.set_role(sid, STUB, "u-admin", "member")  # real one demoted
    await host.approvals.vote(sid, proposal["id"], actor_username="anna", approve=True)
    assert await _role(host, *target) == SpaceRole.MEMBER


async def test_the_owner_sees_who_and_which_role(host):
    await _delegation_off(host)
    target = (OTHER, "t-member")
    await _forward(host, sender=STUB, actor="u-admin", target=target, role="moderator")
    [proposal] = await host.approvals.list_for_space(host.space.id)
    assert proposal["fwd_target_label"] == "t-member"  # the seat's display name
    assert proposal["fwd_params"]["role"] == "moderator"


async def test_a_newer_request_for_the_same_seat_supersedes_the_older(host):
    await _delegation_off(host)
    sid = host.space.id
    target = (OTHER, "t-member")
    await _forward(host, sender=STUB, actor="u-admin", target=target, role="moderator")
    [first] = await host.approvals.list_for_space(sid)
    # A different seat stays queued alongside.
    await _forward(
        host, sender=STUB, actor="u-admin", target=(OTHER, "t-mod"), role="member"
    )
    await _forward(host, sender=STUB, actor="u-admin", target=target, role="moderator")
    open_ = await host.approvals.list_for_space(sid)
    assert len(open_) == 2
    assert first["id"] not in {p["id"] for p in open_}
    old = await host.approvals._proposals.get(first["id"])
    assert old.status is ProposalStatus.EXPIRED


async def test_owner_approvals_are_capped_per_household(host):
    """A flooding household stops at the cap; others still get through."""
    await _delegation_off(host)
    sid = host.space.id
    for i in range(MAX_PENDING_OWNER_APPROVALS_PER_HOUSEHOLD + 5):
        await host.approvals.enqueue_owner_approval(
            sid,
            actor_instance=STUB,
            actor_user="u-admin",
            fwd_action="ban",
            fwd_params={"user_id": f"victim-{i}"},
        )
    mine = await host.approvals.list_for_space(sid)
    assert len(mine) == MAX_PENDING_OWNER_APPROVALS_PER_HOUSEHOLD
    await host.approvals.enqueue_owner_approval(
        sid,
        actor_instance=OTHER,
        actor_user="c-admin",
        fwd_action="ban",
        fwd_params={"user_id": "someone"},
    )
    assert len(await host.approvals.list_for_space(sid)) == (
        MAX_PENDING_OWNER_APPROVALS_PER_HOUSEHOLD + 1
    )


# ── review hardening: a stale request never undoes a newer decision ──


async def test_a_request_against_a_stale_role_is_dropped(host):
    """The stub's mirror still says ``member`` for a seat the host already
    moved to ``moderator``: its "make them a member" is against a role the
    seat no longer holds, so it must not land."""
    target = (OTHER, "t-mod")  # host: moderator
    await _forward(
        host,
        sender=STUB,
        actor="u-admin",
        target=target,
        role="member",
        from_role="member",  # what a stale mirror would claim
    )
    assert await _role(host, *target) == SpaceRole.MODERATOR


async def test_a_request_without_from_role_is_dropped(host):
    target = (OTHER, "t-member")
    await _forward(
        host,
        sender=STUB,
        actor="u-admin",
        target=target,
        role="moderator",
        from_role="",
    )
    assert await _role(host, *target) == SpaceRole.MEMBER


async def test_approval_rechecks_from_role(host):
    """Queued member→moderator; meanwhile the owner made the seat an admin
    directly. Approving the stale ask must not demote that admin."""
    await _delegation_off(host)
    sid = host.space.id
    target = (OTHER, "t-member")
    await _forward(host, sender=STUB, actor="u-admin", target=target, role="moderator")
    [proposal] = await host.approvals.list_for_space(sid)
    await host.svc.set_remote_member_role(
        sid, actor_username="anna", instance_id=OTHER, user_id="t-member", role="admin"
    )
    await host.approvals.vote(sid, proposal["id"], actor_username="anna", approve=True)
    assert await _role(host, *target) == SpaceRole.ADMIN


async def test_one_household_cannot_evict_anothers_request(host):
    """The supersede is per proposing household: an admin on another
    household asking about the same seat leaves the first request alone."""
    await _delegation_off(host)
    sid = host.space.id
    target = (OTHER, "t-member")
    await _forward(host, sender=STUB, actor="u-admin", target=target, role="moderator")
    await _forward(host, sender=OTHER, actor="c-admin", target=target, role="moderator")
    open_ = await host.approvals.list_for_space(sid)
    assert sorted(p["proposed_by_instance"] for p in open_) == sorted([STUB, OTHER])


async def test_the_cap_is_checked_before_a_supersede(host):
    """A household at the cap can't churn the list with replacements: the
    new request is dropped and its older one for that seat stays."""
    await _delegation_off(host)
    sid = host.space.id
    target = (OTHER, "t-member")
    await _forward(host, sender=STUB, actor="u-admin", target=target, role="moderator")
    [first] = await host.approvals.list_for_space(sid)
    for i in range(MAX_PENDING_OWNER_APPROVALS_PER_HOUSEHOLD - 1):
        await host.approvals.enqueue_owner_approval(
            sid,
            actor_instance=STUB,
            actor_user="u-admin",
            fwd_action="ban",
            fwd_params={"user_id": f"victim-{i}"},
        )
    await _forward(host, sender=STUB, actor="u-admin", target=target, role="moderator")
    ids = {p["id"] for p in await host.approvals.list_for_space(sid)}
    assert first["id"] in ids
    assert len(ids) == MAX_PENDING_OWNER_APPROVALS_PER_HOUSEHOLD
