"""Tests for :class:`SpaceApprovalService` — multi-admin quorum approval.

Real SQLite repos for state; the *executed* SpaceService and the
federation transport are mocked so we assert the approval logic
(threshold math, reject-cancels, expiry, forward, host dispatch) in
isolation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import (
    SpaceMember,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
)
from socialhome.domain.child_protection import AccountProtectedError
from socialhome.domain.events import SpaceProposalUpdated
from socialhome.domain.space_proposal import ProposalAction, ProposalStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_proposal_repo import SqliteSpaceProposalRepo
from socialhome.repositories.space_remote_member_repo import SqliteSpaceRemoteMemberRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_approval_service import SpaceApprovalService
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    from socialhome.infrastructure.key_manager import KeyManager

    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0c" * 32))
    remote_repo = SqliteSpaceRemoteMemberRepo(db)
    proposal_repo = SqliteSpaceProposalRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    # Real SpaceService only to *create* spaces / seat members in setup.
    setup_svc = SpaceService(
        space_repo, SqliteSpacePostRepo(db), user_repo, bus, own_instance_id=iid
    )
    # The approval service executes through a mock so we assert the call
    # without running the real dissolve / update_config machinery.
    exec_svc = MagicMock()
    exec_svc.dissolve_space = AsyncMock()
    exec_svc.update_config = AsyncMock()
    exec_svc.apply_approved_admin_action = AsyncMock()
    fed = MagicMock()
    fed.send_with_mesh_fallback = AsyncMock()
    fed.broadcast_to_space_members = AsyncMock()
    fed.peer_supports = AsyncMock(return_value=True)
    approvals = SpaceApprovalService(
        proposal_repo, space_repo, remote_repo, user_repo, bus, own_instance_id=iid
    )
    approvals.attach(federation_service=fed, space_service=exec_svc)

    class S:
        pass

    s = S()
    s.db = db
    s.iid = iid
    s.bus = bus
    s.user_svc = user_svc
    s.setup = setup_svc
    s.space_repo = space_repo
    s.remote = remote_repo
    s.proposals = proposal_repo
    s.exec = exec_svc
    s.fed = fed
    s.approvals = approvals
    yield s
    await db.shutdown()


async def _user(stack, name):
    return await stack.user_svc.provision(username=name, display_name=name)


async def _space(stack, owner="alice"):
    await _user(stack, owner)
    return await stack.setup.create_space(owner_username=owner, name="S")


async def _add_admin(stack, space_id, name):
    u = await _user(stack, name)
    await stack.space_repo.save_member(
        SpaceMember(
            space_id=space_id,
            user_id=u.user_id,
            role=SpaceRole.ADMIN,
            joined_at="2026-06-01T00:00:00+00:00",
        )
    )
    return u


# ── solo-admin: immediate execution ─────────────────────────────────


async def test_solo_owner_dissolve_executes_immediately(stack):
    space = await _space(stack)
    view = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    assert view["status"] == ProposalStatus.EXECUTED.value
    stack.exec.dissolve_space.assert_awaited_once()


async def test_solo_owner_set_tier_executes_immediately(stack):
    space = await _space(stack)
    view = await stack.approvals.propose(
        space.id,
        actor_username="alice",
        action=ProposalAction.SET_PUBLIC_TIER,
        params={"space_type": "public"},
    )
    assert view["status"] == ProposalStatus.EXECUTED.value
    stack.exec.update_config.assert_awaited_once()
    assert stack.exec.update_config.call_args.kwargs["space_type"] == "public"


# ── two admins: needs both ───────────────────────────────────────────


async def test_two_admins_need_both_to_dissolve(stack):
    space = await _space(stack)
    await _add_admin(stack, space.id, "bob")
    # Alice proposes — pending, not executed (majority of 2 is 2).
    view = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    assert view["status"] == ProposalStatus.PENDING.value
    assert view["approvals"] == 1
    assert view["total_admins"] == 2
    assert view["needed"] == 2
    stack.exec.dissolve_space.assert_not_awaited()
    # Bob approves → majority → executes.
    out = await stack.approvals.vote(
        space.id, view["id"], actor_username="bob", approve=True
    )
    assert out["status"] == ProposalStatus.EXECUTED.value
    stack.exec.dissolve_space.assert_awaited_once()


async def test_reject_cancels_proposal(stack):
    space = await _space(stack)
    await _add_admin(stack, space.id, "bob")
    view = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    out = await stack.approvals.vote(
        space.id, view["id"], actor_username="bob", approve=False
    )
    assert out["status"] == ProposalStatus.REJECTED.value
    stack.exec.dissolve_space.assert_not_awaited()


async def test_three_admins_majority_is_two(stack):
    space = await _space(stack)
    await _add_admin(stack, space.id, "bob")
    await _add_admin(stack, space.id, "carol")
    view = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    assert view["needed"] == 2 and view["total_admins"] == 3
    out = await stack.approvals.vote(
        space.id, view["id"], actor_username="bob", approve=True
    )
    assert out["status"] == ProposalStatus.EXECUTED.value


# ── authorization ────────────────────────────────────────────────────


async def test_non_admin_cannot_propose(stack):
    space = await _space(stack)
    member = await _user(stack, "mallory")
    await stack.space_repo.save_member(
        SpaceMember(
            space_id=space.id,
            user_id=member.user_id,
            role=SpaceRole.MEMBER,
            joined_at="2026-06-01T00:00:00+00:00",
        )
    )
    with pytest.raises(SpacePermissionError):
        await stack.approvals.propose(
            space.id, actor_username="mallory", action=ProposalAction.DISSOLVE
        )


async def test_vote_by_non_admin_dropped(stack):
    space = await _space(stack)
    await _add_admin(stack, space.id, "bob")
    view = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    # A plain member tries to vote — _require_local_admin rejects.
    plain = await _user(stack, "mallory")
    await stack.space_repo.save_member(
        SpaceMember(
            space_id=space.id,
            user_id=plain.user_id,
            role=SpaceRole.MEMBER,
            joined_at="2026-06-01T00:00:00+00:00",
        )
    )
    with pytest.raises(SpacePermissionError):
        await stack.approvals.vote(
            space.id, view["id"], actor_username="mallory", approve=True
        )
    stack.exec.dissolve_space.assert_not_awaited()


# ── dedupe + list + expiry ───────────────────────────────────────────


async def test_proposing_twice_reuses_open_proposal(stack):
    space = await _space(stack)
    await _add_admin(stack, space.id, "bob")
    v1 = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    v2 = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    assert v1["id"] == v2["id"]
    assert len(await stack.approvals.list_for_space(space.id)) == 1


async def test_expire_due_marks_expired(stack):
    space = await _space(stack)
    await _add_admin(stack, space.id, "bob")
    view = await stack.approvals.propose(
        space.id, actor_username="alice", action=ProposalAction.DISSOLVE
    )
    # Force the row's expiry into the past.
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    await stack.db.enqueue(
        "UPDATE space_admin_proposals SET expires_at=? WHERE id=?",
        (past, view["id"]),
    )
    n = await stack.approvals.expire_due()
    assert n == 1
    assert (await stack.proposals.get(view["id"])).status == ProposalStatus.EXPIRED


# ── cross-household forward + host dispatch ──────────────────────────


async def _remote_stub(stack):
    """A space hosted elsewhere where our local user is a (stub) admin."""
    from socialhome.domain.space import JoinMode, Space, SpaceFeatures

    admin = await _user(stack, "localadmin")
    stub = Space(
        id="sp-remote",
        name="S",
        owner_instance_id="host-instance",
        owner_username="hostowner",
        identity_public_key="",
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
        emoji="🏠",
        description="",
    )
    await stack.space_repo.save(stub)
    await stack.space_repo.save_member(
        SpaceMember(
            space_id=stub.id,
            user_id=admin.user_id,
            role=SpaceRole.ADMIN,
            joined_at="2026-06-01T00:00:00+00:00",
        )
    )
    return stub


async def test_propose_on_remote_stub_forwards(stack):
    stub = await _remote_stub(stack)
    await stack.approvals.propose(
        stub.id, actor_username="localadmin", action=ProposalAction.DISSOLVE
    )
    stack.fed.send_with_mesh_fallback.assert_awaited_once()
    payload = stack.fed.send_with_mesh_fallback.call_args.kwargs["payload"]
    assert payload["action"] == "propose"
    assert payload["params"]["action"] == "dissolve"
    # Nothing executed locally.
    stack.exec.dissolve_space.assert_not_awaited()


async def test_propose_on_remote_stub_raises_when_host_too_old(stack):
    stack.fed.peer_supports = AsyncMock(return_value=False)
    stub = await _remote_stub(stack)
    with pytest.raises(SpacePermissionError):
        await stack.approvals.propose(
            stub.id, actor_username="localadmin", action=ProposalAction.DISSOLVE
        )


async def test_apply_remote_propose_then_vote_executes(stack):
    """Host receives a remote admin's propose + a second remote admin's
    approve → majority → executes."""
    space = await _space(stack)
    # Seat two remote admins on different households.
    for inst, uid in (("host-A", "ada"), ("host-B", "ben")):
        await stack.remote.add(
            space_id=space.id,
            instance_id=inst,
            user_id=uid,
            user_pk=None,
            display_name=None,
        )
        await stack.remote.set_role(space.id, inst, uid, "admin")
    # Total admins now: alice (owner) + ada + ben = 3 → majority 2.
    await stack.approvals.apply_remote_propose(
        space.id,
        proposer_instance="host-A",
        proposer_user="ada",
        action="dissolve",
        params={},
    )
    open_props = await stack.approvals.list_for_space(space.id)
    assert len(open_props) == 1
    pid = open_props[0]["id"]
    stack.exec.dissolve_space.assert_not_awaited()
    await stack.approvals.apply_remote_vote(
        space.id,
        voter_instance="host-B",
        voter_user="ben",
        proposal_id=pid,
        approve=True,
    )
    stack.exec.dissolve_space.assert_awaited_once()


async def test_apply_remote_propose_by_non_admin_dropped(stack):
    space = await _space(stack)
    # A household that is NOT a seated admin tries to propose.
    await stack.approvals.apply_remote_propose(
        space.id,
        proposer_instance="rando-instance",
        proposer_user="rando",
        action="dissolve",
        params={},
    )
    assert await stack.approvals.list_for_space(space.id) == []


# ── member-household mirror shows the host's exact tally (fast-follow) ──


async def test_mirror_update_shows_host_tally_not_local_recompute(stack):
    """A member household stores + returns the host's authoritative view
    verbatim, even when its own roster would compute a different tally."""
    from socialhome.domain.space import JoinMode, Space, SpaceFeatures

    # A stub of a space hosted elsewhere — locally we know of NO admins,
    # so a local recompute would say 0 total. The host's view says 2-of-3.
    stub = Space(
        id="sp-remote",
        name="S",
        owner_instance_id="host-instance",
        owner_username="hostowner",
        identity_public_key="",
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
        emoji="🏠",
        description="",
    )
    await stack.space_repo.save(stub)
    host_view = {
        "id": "prop-1",
        "space_id": stub.id,
        "action": "dissolve",
        "params": {},
        "status": "pending",
        "proposed_by_instance": "host-instance",
        "proposed_by_user": "hostadmin",
        "approvals": 2,
        "total_admins": 3,
        "needed": 2,
        # Relative dates: ``list_for_space`` filters out proposals whose
        # ``expires_at`` is in the past (service.py ``_now()`` check), so a
        # hardcoded date silently rots the test the day it lapses. Keep the
        # proposal active by anchoring on the run time.
        "created_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    }
    await stack.approvals.apply_mirror_update(stub.id, host_view)
    listed = await stack.approvals.list_for_space(stub.id)
    assert len(listed) == 1
    assert listed[0]["approvals"] == 2
    assert listed[0]["total_admins"] == 3
    assert listed[0]["needed"] == 2


async def test_mirror_update_drops_resolved_proposal(stack):
    """A resolved (executed/rejected) host view removes the mirror row."""
    from socialhome.domain.space import JoinMode, Space, SpaceFeatures

    stub = Space(
        id="sp-remote2",
        name="S",
        owner_instance_id="host-instance",
        owner_username="hostowner",
        identity_public_key="",
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
        emoji="🏠",
        description="",
    )
    await stack.space_repo.save(stub)
    base = {
        "id": "prop-2",
        "space_id": stub.id,
        "action": "dissolve",
        "params": {},
        "proposed_by_instance": "host-instance",
        "proposed_by_user": "hostadmin",
        "approvals": 1,
        "total_admins": 2,
        "needed": 2,
        # Relative dates: ``list_for_space`` filters out proposals whose
        # ``expires_at`` is in the past (service.py ``_now()`` check), so a
        # hardcoded date silently rots the test the day it lapses. Keep the
        # proposal active by anchoring on the run time.
        "created_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    }
    await stack.approvals.apply_mirror_update(stub.id, {**base, "status": "pending"})
    assert len(await stack.approvals.list_for_space(stub.id)) == 1
    await stack.approvals.apply_mirror_update(stub.id, {**base, "status": "rejected"})
    assert await stack.approvals.list_for_space(stub.id) == []


# ── owner-only approval of a forwarded remote-admin action (Phase 6a) ──


async def test_enqueue_owner_approval_creates_pending_no_vote(stack):
    """A forwarded remote-admin action becomes a PENDING owner-only proposal
    carrying the forwarded action in params, with NO vote recorded."""
    space = await _space(stack)
    seen: list = []

    async def _capture(e):
        seen.append(e)

    stack.bus.subscribe(SpaceProposalUpdated, _capture)
    await stack.approvals.enqueue_owner_approval(
        space.id,
        actor_instance="host-B",
        actor_user="ben",
        fwd_action="ban",
        fwd_params={"user_id": "u-victim"},
    )
    listed = await stack.approvals.list_for_space(space.id)
    assert len(listed) == 1
    v = listed[0]
    assert v["action"] == ProposalAction.REMOTE_ADMIN_ACTION.value
    assert v["status"] == ProposalStatus.PENDING.value
    assert v["params"]["fwd_action"] == "ban"
    assert v["params"]["fwd_params"] == {"user_id": "u-victim"}
    assert v["params"]["actor_instance"] == "host-B"
    assert v["params"]["actor_user"] == "ben"
    # No vote recorded.
    assert await stack.proposals.list_votes(v["id"]) == []
    # An update event was emitted.
    assert any(e.proposal_id == v["id"] for e in seen)


async def test_enqueue_owner_approval_no_dedup(stack):
    """Two successive enqueues create two distinct pending proposals."""
    space = await _space(stack)
    await stack.approvals.enqueue_owner_approval(
        space.id,
        actor_instance="host-B",
        actor_user="ben",
        fwd_action="ban",
        fwd_params={"user_id": "a"},
    )
    await stack.approvals.enqueue_owner_approval(
        space.id,
        actor_instance="host-B",
        actor_user="ben",
        fwd_action="ban",
        fwd_params={"user_id": "b"},
    )
    listed = await stack.approvals.list_for_space(space.id)
    assert len(listed) == 2
    assert {p["id"] for p in listed} == set(p["id"] for p in listed)
    assert len({p["id"] for p in listed}) == 2


async def test_enqueue_owner_approval_noop_when_not_host(stack):
    stub = await _remote_stub(stack)
    await stack.approvals.enqueue_owner_approval(
        stub.id,
        actor_instance="host-B",
        actor_user="ben",
        fwd_action="ban",
        fwd_params={},
    )
    assert await stack.approvals.list_for_space(stub.id) == []


async def test_owner_approve_executes_forwarded_action(stack):
    space = await _space(stack)
    await stack.approvals.enqueue_owner_approval(
        space.id,
        actor_instance="host-B",
        actor_user="ben",
        fwd_action="ban",
        fwd_params={"user_id": "u-victim"},
    )
    pid = (await stack.approvals.list_for_space(space.id))[0]["id"]
    out = await stack.approvals.vote(
        space.id, pid, actor_username="alice", approve=True
    )
    assert out["status"] == ProposalStatus.EXECUTED.value
    stack.exec.apply_approved_admin_action.assert_awaited_once()
    kwargs = stack.exec.apply_approved_admin_action.call_args.kwargs
    assert kwargs["action"] == "ban"
    assert kwargs["params"] == {"user_id": "u-victim"}


async def test_owner_reject_does_not_execute(stack):
    space = await _space(stack)
    await stack.approvals.enqueue_owner_approval(
        space.id,
        actor_instance="host-B",
        actor_user="ben",
        fwd_action="ban",
        fwd_params={},
    )
    pid = (await stack.approvals.list_for_space(space.id))[0]["id"]
    out = await stack.approvals.vote(
        space.id, pid, actor_username="alice", approve=False
    )
    assert out["status"] == ProposalStatus.REJECTED.value
    stack.exec.apply_approved_admin_action.assert_not_awaited()


async def test_non_owner_admin_cannot_vote_owner_only(stack):
    """A non-owner admin's vote on an owner-only proposal is dropped; the
    proposal stays pending and is not executed."""
    space = await _space(stack)
    await _add_admin(stack, space.id, "bob")
    await stack.approvals.enqueue_owner_approval(
        space.id,
        actor_instance="host-B",
        actor_user="ben",
        fwd_action="ban",
        fwd_params={},
    )
    pid = (await stack.approvals.list_for_space(space.id))[0]["id"]
    out = await stack.approvals.vote(space.id, pid, actor_username="bob", approve=True)
    assert out["status"] == ProposalStatus.PENDING.value
    stack.exec.apply_approved_admin_action.assert_not_awaited()
    # No vote recorded for the non-owner.
    assert await stack.proposals.list_votes(pid) == []


async def test_owner_only_view_exposes_owner_only_fields(stack):
    space = await _space(stack)
    await stack.approvals.enqueue_owner_approval(
        space.id,
        actor_instance="host-B",
        actor_user="ben",
        fwd_action="ban",
        fwd_params={"user_id": "u-victim"},
    )
    v = (await stack.approvals.list_for_space(space.id))[0]
    assert v["owner_only"] is True
    assert v["fwd_action"] == "ban"
    assert v["fwd_params"] == {"user_id": "u-victim"}
    assert v["total_admins"] == 1
    assert v["needed"] == 1
    assert v["approvals"] == 0
    # After the owner approves, approvals flips 0 → 1 in the emitted view.
    pid = v["id"]
    out = await stack.approvals.vote(
        space.id, pid, actor_username="alice", approve=True
    )
    assert out["approvals"] == 1


# ── fwd_target_label: resolve the target's name for the owner's card ──


async def test_owner_only_view_labels_ban_target_remote_member(stack):
    """A ban targeting a seated remote member resolves to that member's
    display_name."""
    space = await _space(stack)
    await stack.remote.add(
        space_id=space.id,
        instance_id="host-B",
        user_id="u-victim",
        user_pk=None,
        display_name="Alice",
    )
    await stack.approvals.enqueue_owner_approval(
        space.id,
        actor_instance="host-B",
        actor_user="ben",
        fwd_action="ban",
        fwd_params={"user_id": "u-victim"},
    )
    v = (await stack.approvals.list_for_space(space.id))[0]
    assert v["fwd_target_label"] == "Alice"


async def test_owner_only_view_labels_ban_target_local_user(stack):
    """A ban targeting a local user resolves to that user's display_name."""
    space = await _space(stack)
    victim = await _user(stack, "victimlocal")
    await stack.approvals.enqueue_owner_approval(
        space.id,
        actor_instance="host-B",
        actor_user="ben",
        fwd_action="ban",
        fwd_params={"user_id": victim.user_id},
    )
    v = (await stack.approvals.list_for_space(space.id))[0]
    assert v["fwd_target_label"] == "victimlocal"


async def test_owner_only_view_label_none_for_unknown_ban_target(stack):
    """A ban whose target is neither a member nor a known user → None."""
    space = await _space(stack)
    await stack.approvals.enqueue_owner_approval(
        space.id,
        actor_instance="host-B",
        actor_user="ben",
        fwd_action="ban",
        fwd_params={"user_id": "u-nobody"},
    )
    v = (await stack.approvals.list_for_space(space.id))[0]
    assert v["fwd_target_label"] is None


async def test_owner_only_view_label_none_for_invite_without_remote_record(stack):
    """An invite to an unknown invitee resolves to None (no remote-user row)."""
    space = await _space(stack)
    await stack.approvals.enqueue_owner_approval(
        space.id,
        actor_instance="host-B",
        actor_user="ben",
        fwd_action="invite",
        fwd_params={
            "invitee_instance_id": "host-C",
            "invitee_user_id": "u-newbie",
        },
    )
    v = (await stack.approvals.list_for_space(space.id))[0]
    assert v["fwd_target_label"] is None


async def test_owner_only_view_labels_invite_target_from_remote_user(stack):
    """An invite resolves to a cached remote-user record's display_name."""
    from socialhome.domain.user import RemoteUser

    space = await _space(stack)
    await stack.db.enqueue(
        """
        INSERT INTO remote_instances(
            id, display_name, remote_identity_pk,
            key_self_to_remote, key_remote_to_self,
            remote_inbox_url, local_inbox_id, status, source
        ) VALUES('host-C', 'C', 'aa', 'k', 'k', 'https://x', 'wh',
                 'confirmed', 'manual')
        """,
    )
    await stack.approvals._users.upsert_remote(
        RemoteUser(
            user_id="u-newbie",
            instance_id="host-C",
            remote_username="newbie",
            display_name="Newbie Bob",
        )
    )
    await stack.approvals.enqueue_owner_approval(
        space.id,
        actor_instance="host-B",
        actor_user="ben",
        fwd_action="invite",
        fwd_params={
            "invitee_instance_id": "host-C",
            "invitee_user_id": "u-newbie",
        },
    )
    v = (await stack.approvals.list_for_space(space.id))[0]
    assert v["fwd_target_label"] == "Newbie Bob"


async def test_owner_only_view_label_none_for_targetless_action(stack):
    """An update_config/archive action has no target → None."""
    space = await _space(stack)
    await stack.approvals.enqueue_owner_approval(
        space.id,
        actor_instance="host-B",
        actor_user="ben",
        fwd_action="archive",
        fwd_params={},
    )
    v = (await stack.approvals.list_for_space(space.id))[0]
    assert v["fwd_target_label"] is None


# ── §CP.R: a protected admin can't publish a space ───────────────────


class _Cp:
    def __init__(self, protected: set[str]):
        self._protected = protected

    def register_gate(self, gate):
        pass

    async def is_protected(self, user_id):
        return user_id in self._protected

    async def require_unrestricted(self, user_id, capability):
        if user_id in self._protected:
            raise AccountProtectedError(capability)


async def test_protected_admin_cannot_propose_public_tier(stack):
    space = await _space(stack)
    kid = await _add_admin(stack, space.id, "kid")
    stack.approvals.attach_child_protection(_Cp({kid.user_id}))
    with pytest.raises(AccountProtectedError):
        await stack.approvals.propose(
            space.id,
            actor_username="kid",
            action=ProposalAction.SET_PUBLIC_TIER,
            params={"space_type": "global"},
        )
    # Dissolving isn't a publication — the gate stays out of it.
    view = await stack.approvals.propose(
        space.id, actor_username="kid", action=ProposalAction.DISSOLVE
    )
    assert view["status"] == ProposalStatus.PENDING.value


async def test_protected_admin_cannot_approve_public_tier(stack):
    space = await _space(stack)
    kid = await _add_admin(stack, space.id, "kid")
    stack.approvals.attach_child_protection(_Cp({kid.user_id}))
    view = await stack.approvals.propose(
        space.id,
        actor_username="alice",
        action=ProposalAction.SET_PUBLIC_TIER,
        params={"space_type": "public"},
    )
    assert view["status"] == ProposalStatus.PENDING.value
    with pytest.raises(AccountProtectedError):
        await stack.approvals.vote(
            space.id, view["id"], actor_username="kid", approve=True
        )
    # Rejecting is always allowed — it can only keep the space private.
    out = await stack.approvals.vote(
        space.id, view["id"], actor_username="kid", approve=False
    )
    assert out["status"] == ProposalStatus.REJECTED.value
    stack.exec.update_config.assert_not_awaited()


async def test_protected_admin_cannot_join_an_open_publication(stack):
    """Proposing ``set_public_tier`` without a tier (or a private one) while
    a public proposal is open would reuse that row and auto-approve it."""
    space = await _space(stack)
    kid = await _add_admin(stack, space.id, "kid")
    stack.approvals.attach_child_protection(_Cp({kid.user_id}))
    open_view = await stack.approvals.propose(
        space.id,
        actor_username="alice",
        action=ProposalAction.SET_PUBLIC_TIER,
        params={"space_type": "public"},
    )
    assert open_view["status"] == ProposalStatus.PENDING.value
    for params in ({}, {"space_type": "private"}):
        with pytest.raises(AccountProtectedError):
            await stack.approvals.propose(
                space.id,
                actor_username="kid",
                action=ProposalAction.SET_PUBLIC_TIER,
                params=params,
            )
    stack.exec.update_config.assert_not_awaited()


async def test_protected_approval_of_an_unknown_proposal_fails_closed(stack):
    space = await _space(stack)
    kid = await _add_admin(stack, space.id, "kid")
    stack.approvals.attach_child_protection(_Cp({kid.user_id}))
    with pytest.raises(AccountProtectedError):
        await stack.approvals.vote(
            space.id, "not-mirrored-yet", actor_username="kid", approve=True
        )
