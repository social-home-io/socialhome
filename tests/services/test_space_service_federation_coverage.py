"""Coverage fill for :class:`SpaceService` federation-facing methods.

Covers remote invites (accept/decline), remote member removal, join
requests (approve/deny local + remote), and ``request_join_remote``.
Each test uses a MagicMock for FederationService so we never require
a real peer connection.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.crypto import (
    derive_instance_id,
    ed25519_public_key,
    generate_identity_keypair,
    generate_space_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    RemoteJoinRequestApproved,
    SpaceInviteAnswered,
    SpaceRemoteSeatEnded,
)
from socialhome.domain.federation import (
    DELIVERY_ERROR_MESH_DEFERRED,
    DELIVERY_ERROR_QUEUED,
    DELIVERY_ERROR_RELAY_THROTTLED,
    DeliveryResult,
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.space import (
    AgeRestrictedError,
    HostUnreachableError,
    InviteExpiredError,
    JoinMode,
    RemoteAdminOutcome,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
)
from socialhome.domain.space_proposal import ProposalAction, ProposalStatus
from socialhome.federation.private_invite_handler import PrivateSpaceInviteHandler
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_proposal_repo import SqliteSpaceProposalRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_approval_service import SpaceApprovalService
from socialhome.services.space_service import SpaceService, seal_join_snapshot
from socialhome.services.user_service import UserService
from socialhome.space_owner_statement import (
    MAX_STATEMENT_AGE_MS,
    sign_inviter_proof,
    sign_owner_statement,
)


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
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    svc = SpaceService(
        space_repo,
        SqliteSpacePostRepo(db),
        user_repo,
        bus,
        own_instance_id=iid,
    )
    fed_svc = MagicMock()
    fed_svc.send_event = AsyncMock()
    # The space-service private-invite family delegates to
    # ``FederationService.send_with_mesh_fallback`` — default to a
    # successful direct-delivery result; tests override per-case.
    fed_svc.send_with_mesh_fallback = AsyncMock(
        return_value=DeliveryResult(instance_id="peer", ok=True),
    )
    # Default: peers advertise the latest protocol version so the
    # cross-household admin-action forward is allowed. Tests that need an
    # older host override this per-case.
    fed_svc.peer_supports = AsyncMock(return_value=True)
    # ``accept_remote_invite`` kicks a mesh catch-up sync via this helper
    # (no-op for a confirmed host); stub it so the ``await`` resolves.
    fed_svc.begin_mesh_catchup_sync = AsyncMock()
    # Real bytes, not a MagicMock attribute: an invite code embeds this
    # household's Ed25519 identity pubkey (the §D2b bootstrap block), and
    # a mock would only prove the mock is JSON-hostile.
    fed_svc.own_identity_pk = kp.public_key
    fed_repo = MagicMock()
    fed_repo.get_instance = AsyncMock(
        return_value=RemoteInstance(
            id="peer",
            display_name="Peer",
            remote_identity_pk="ab" * 32,
            key_self_to_remote="k",
            key_remote_to_self="k",
            remote_inbox_url="https://peer",
            local_inbox_id="l",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        ),
    )
    svc.attach_federation(
        federation_service=fed_svc,
        federation_repo=fed_repo,
        remote_member_repo=SqliteSpaceRemoteMemberRepo(db),
    )

    class S:
        pass

    s = S()
    s.db = db
    s.svc = svc
    s.fed_svc = fed_svc
    s.fed_repo = fed_repo
    s.space_repo = space_repo
    s.user_svc = user_svc
    s.bus = bus
    yield s
    await db.shutdown()


async def _user(stack, username):
    return await stack.user_svc.provision(
        username=username,
        display_name=username,
    )


# ── invite_remote_user ──────────────────────────────────────────────


async def test_invite_remote_user_rejects_unpaired_host(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="Private",
        space_type=SpaceType.PRIVATE,
    )
    # Federation surfaces no path → invite raises permission error.
    stack.fed_svc.send_with_mesh_fallback.return_value = DeliveryResult(
        instance_id="peer",
        ok=False,
        error="not_confirmed",
    )
    with pytest.raises(SpacePermissionError):
        await stack.svc.invite_remote_user(
            space.id,
            actor_username="alicehost",
            invitee_instance_id="peer",
            invitee_user_id="bob",
        )


async def test_invite_remote_user_happy(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="Private",
        space_type=SpaceType.PRIVATE,
    )
    token = await stack.svc.invite_remote_user(
        space.id,
        actor_username="alicehost",
        invitee_instance_id="peer",
        invitee_user_id="bob",
    )
    assert token
    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()
    # The outbound payload must carry ``invitee_user_id`` so
    # :meth:`PrivateSpaceInviteHandler._on_invite` doesn't early-
    # return on the recipient. Regression guard for
    # ``GET /api/remote_invites`` returning empty.
    call = stack.fed_svc.send_with_mesh_fallback.call_args
    payload = call.kwargs["payload"]
    assert payload["invitee_user_id"] == "bob"
    assert payload["invite_token"] == token


async def test_invite_remote_user_requires_federation():
    """Direct SpaceService without attach_federation must raise."""
    svc = SpaceService.__new__(SpaceService)
    svc._federation = None
    svc._federation_repo = None
    with pytest.raises(RuntimeError):
        await svc.invite_remote_user(
            "sp",
            actor_username="alicehost",
            invitee_instance_id="peer",
            invitee_user_id="bob",
        )


# ── accept/decline_remote_invite ───────────────────────────────────


async def test_accept_remote_invite_unknown_token_raises(stack):
    with pytest.raises(KeyError):
        await stack.svc.accept_remote_invite(
            token="bogus",
            user_id="u",
        )


async def test_accept_remote_invite_not_cross_household_raises(stack):
    """A remote-invitation row saved with no remote_instance_id yields ValueError."""
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
    )
    # Directly insert a row without remote_instance_id.
    await stack.db.enqueue(
        """INSERT INTO space_invitations(
               id, space_id, invited_user_id, invited_by, remote_instance_id,
               remote_user_id, invite_token, status, expires_at
           ) VALUES(?, ?, 'u', 'x', '', 'u', 'local-tkn', 'pending',
                    datetime('now', '+1 day'))""",
        ("inv-1", space.id),
    )
    with pytest.raises(ValueError):
        await stack.svc.accept_remote_invite(
            token="local-tkn",
            user_id="u",
        )


async def test_accept_remote_invite_happy(stack):
    bob = await _user(stack, "bobhost")
    # Pre-existing space on the OTHER household — seed a remote-invite
    # row pointing at bob.
    await stack.space_repo.save_remote_invitation(
        space_id="sp-on-the-other-side",
        invited_by="alicehost-id",
        remote_instance_id="peer",
        remote_user_id=bob.user_id,
        invite_token="tok-xyz",
        space_display_hint="S",
    )
    # Seed the local stub so the membership row insert succeeds.
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )

    stub = Space(
        id="sp-on-the-other-side",
        name="Remote space",
        owner_instance_id="peer",
        owner_username="alicehost",
        identity_public_key="",
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
        emoji="🏠",
        description="",
    )
    await stack.space_repo.save(stub)

    answered: list[SpaceInviteAnswered] = []
    stack.bus.subscribe(SpaceInviteAnswered, answered.append)
    await stack.svc.accept_remote_invite(
        token="tok-xyz",
        user_id=bob.user_id,
    )

    stack.fed_svc.send_with_mesh_fallback.assert_awaited()
    # The invite leaves the invitee's "Needs you".
    assert [(a.space_id, a.user_id, a.accepted) for a in answered] == [
        ("sp-on-the-other-side", bob.user_id, True)
    ]
    # Regression guard for "host sees raw user_id instead of display
    # name" — the accept envelope MUST carry the invitee's display name
    # so the host's roster renders the human-readable label rather than
    # the bare ``uid-...`` hash. Earlier, the code looked up
    # ``users_repo.get_by_id`` which doesn't exist on the protocol;
    # ``hasattr`` returned False every time and ``invitee_display_name``
    # was always ``None``.
    call = stack.fed_svc.send_with_mesh_fallback.call_args
    payload = call.kwargs["payload"]
    assert payload["invitee_display_name"] == bob.display_name
    assert payload["invitee_user_id"] == bob.user_id


async def test_decline_remote_invite_unknown_token(stack):
    with pytest.raises(KeyError):
        await stack.svc.decline_remote_invite(
            token="nope",
            user_id="u",
        )


async def test_decline_remote_invite_not_cross_household(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
    )
    await stack.db.enqueue(
        """INSERT INTO space_invitations(
               id, space_id, invited_user_id, invited_by, remote_instance_id,
               remote_user_id, invite_token, status, expires_at
           ) VALUES(?, ?, 'u', 'x', '', 'u', 'loc-dec', 'pending',
                    datetime('now', '+1 day'))""",
        ("inv-dec", space.id),
    )
    with pytest.raises(ValueError):
        await stack.svc.decline_remote_invite(
            token="loc-dec",
            user_id="u",
        )


async def test_decline_remote_invite_happy(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
    )
    await stack.space_repo.save_remote_invitation(
        space_id=space.id,
        invited_by="alicehost-id",
        remote_instance_id="peer",
        remote_user_id="bob",
        invite_token="tok-decline",
        space_display_hint="S",
    )
    answered: list[SpaceInviteAnswered] = []
    stack.bus.subscribe(SpaceInviteAnswered, answered.append)
    await stack.svc.decline_remote_invite(
        token="tok-decline",
        user_id="bob",
    )
    stack.fed_svc.send_with_mesh_fallback.assert_awaited()
    assert [(a.space_id, a.user_id, a.accepted) for a in answered] == [
        (space.id, "bob", False)
    ]


# ── remove_remote_member ───────────────────────────────────────────


async def test_remove_remote_member_happy(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
    )
    await stack.svc.remove_remote_member(
        space.id,
        actor_username="alicehost",
        instance_id="peer",
        user_id="bob",
    )
    stack.fed_svc.send_with_mesh_fallback.assert_awaited()


async def test_remove_remote_member_announces_the_seat_ended(stack):
    """v_77: the game tables this household hosts take the player out."""
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    ended: list = []
    stack.bus.subscribe(SpaceRemoteSeatEnded, ended.append)
    await stack.svc.remove_remote_member(
        space.id, actor_username="alicehost", instance_id="peer", user_id="bob"
    )
    assert [(e.instance_id, e.user_id) for e in ended] == [("peer", "bob")]


# ── request_join_remote ────────────────────────────────────────────


async def test_request_join_remote_requires_confirmed_peer(stack):
    stack.fed_repo.get_instance.return_value = None
    with pytest.raises(SpacePermissionError):
        await stack.svc.request_join_remote(
            "sp-remote",
            applicant_user_id="u",
            host_instance_id="unknown-peer",
        )


async def test_request_join_remote_happy(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        space_type=SpaceType.PUBLIC,
    )
    rid = await stack.svc.request_join_remote(
        space.id,
        applicant_user_id="u-applicant",
        host_instance_id="peer",
        message="join",
    )
    assert rid
    stack.fed_svc.send_event.assert_awaited()


# ── on_remote_join_request_approved ────────────────────────────────


async def test_on_remote_join_request_approved_unknown_noop(stack):
    # No row for this request_id — the handler silently returns.
    await stack.svc.on_remote_join_request_approved(
        "missing",
        invite_token="x",
    )


async def test_on_remote_join_request_approved_routes_cross_instance(stack):
    """§D2 — the host-minted token row lives in the HOST's DB, not ours.

    The applicant's local request row's ``remote_applicant_instance_id``
    holds the HOST instance id. The approval handler MUST route through
    the cross-instance redeem coordinator (which consumes on the host and
    seats locally), passing ``issuer_instance_id=<host>`` — NOT the local
    ``accept_invite_token`` path, which would KeyError on the missing
    local token row and silently drop the join.
    """
    await _user(stack, "alicehost")
    bob = await _user(stack, "bobapp")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        space_type=SpaceType.PUBLIC,
    )
    # Applicant-side remote join request: host = "peer" (a DIFFERENT
    # instance from ours). Seeds the local request row with
    # remote_applicant_instance_id="peer".
    rid = await stack.svc.request_join_remote(
        space.id,
        applicant_user_id=bob.user_id,
        host_instance_id="peer",
    )

    # Stub coordinator records the call and seats the applicant locally
    # (mirrors what the real coordinator does after the host consumes).
    coordinator = MagicMock()

    async def _request_redeem(
        token, *, viewer_user_id, issuer_instance_id, bootstrap=None, join_location=None
    ):
        await stack.space_repo.save_member(
            SpaceMember(
                space_id=space.id,
                user_id=viewer_user_id,
                role=SpaceRole.MEMBER,
                joined_at="2026-01-01T00:00:00+00:00",
            )
        )
        return {"space_id": space.id, "role": SpaceRole.MEMBER}

    coordinator.request_redeem = AsyncMock(side_effect=_request_redeem)
    stack.svc.attach_redeem_coordinator(coordinator)

    await stack.svc.on_remote_join_request_approved(
        rid,
        invite_token="host-minted-token",
    )

    # Routed through the coordinator with the host as issuer + applicant.
    coordinator.request_redeem.assert_awaited_once()
    call = coordinator.request_redeem.call_args
    assert call.args[0] == "host-minted-token"
    assert call.kwargs["issuer_instance_id"] == "peer"
    assert call.kwargs["viewer_user_id"] == bob.user_id
    # And the applicant is actually seated.
    member = await stack.space_repo.get_member(space.id, bob.user_id)
    assert member is not None
    assert member.role == SpaceRole.MEMBER


async def test_on_remote_join_request_approved_local_fallback_seats(stack):
    """A LOCAL join-request approval (no remote host on the row) keeps
    working: redeem_invite_token falls back to accept_invite_token and
    the applicant becomes a real member.
    """
    await _user(stack, "alicehost")
    bob = await _user(stack, "bobapp")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        join_mode=JoinMode.REQUEST,
        space_type=SpaceType.PUBLIC,
    )
    # Local request row → remote_applicant_instance_id is NULL.
    rid = await stack.svc.request_join(
        space.id,
        user_id=bob.user_id,
        message="please",
    )
    # Locally-minted token in OUR DB.
    token = await stack.svc.create_invite_token(
        space.id,
        actor_username="alicehost",
    )
    await stack.svc.on_remote_join_request_approved(rid, invite_token=token)
    member = await stack.space_repo.get_member(space.id, bob.user_id)
    assert member is not None
    assert member.role == SpaceRole.MEMBER


async def test_on_remote_join_request_approved_failure_is_logged(stack, caplog):
    """A genuine redeem failure (host unreachable / denied) must NOT crash
    and must NOT seat the user — but it MUST be observable (WARNING log),
    not a bare ``pass``.
    """
    await _user(stack, "alicehost")
    bob = await _user(stack, "bobapp")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        space_type=SpaceType.PUBLIC,
    )
    rid = await stack.svc.request_join_remote(
        space.id,
        applicant_user_id=bob.user_id,
        host_instance_id="peer",
    )
    coordinator = MagicMock()
    coordinator.request_redeem = AsyncMock(side_effect=TimeoutError("host down"))
    stack.svc.attach_redeem_coordinator(coordinator)

    with caplog.at_level("WARNING"):
        # Does not raise.
        await stack.svc.on_remote_join_request_approved(
            rid,
            invite_token="host-minted-token",
        )

    # Not seated.
    assert (await stack.space_repo.get_member(space.id, bob.user_id)) is None
    # Surfaced at WARNING, not swallowed.
    assert any(r.levelname == "WARNING" for r in caplog.records)


async def test_join_approval_never_blocks_the_inbound_pipeline(stack):
    """Regression (federation-demo ``local-community``): the APPROVED
    envelope is handled inside the inbound pipeline, which reads one
    peer's DataChannel frames one at a time. The redeem it starts waits for
    the host's ACK — on that same channel. Awaited inline, the ACK could
    only be read once the handler gave up: the redeem timed out ("issuer
    did not respond") while the host had already seated the household,
    which never seated itself. The bus handler hands the redeem off and
    returns; the seat lands when the ACK does.
    """
    await _user(stack, "alicehost")
    bob = await _user(stack, "bobapp")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        space_type=SpaceType.PUBLIC,
    )
    rid = await stack.svc.request_join_remote(
        space.id,
        applicant_user_id=bob.user_id,
        host_instance_id="peer",
    )
    started = asyncio.Event()
    ack = asyncio.Event()

    async def _request_redeem(
        token, *, viewer_user_id, issuer_instance_id, bootstrap=None, join_location=None
    ):
        started.set()
        # The ACK arrives only once the inbound pipeline is free again.
        await ack.wait()
        await stack.space_repo.save_member(
            SpaceMember(
                space_id=space.id,
                user_id=viewer_user_id,
                role=SpaceRole.MEMBER,
                joined_at="2026-01-01T00:00:00+00:00",
            )
        )
        return {"space_id": space.id, "role": SpaceRole.MEMBER}

    coordinator = MagicMock()
    coordinator.request_redeem = AsyncMock(side_effect=_request_redeem)
    stack.svc.attach_redeem_coordinator(coordinator)

    # What the inbound SPACE_JOIN_REQUEST_APPROVED handler does: publish and
    # return — it must not wait for the redeem's answer.
    await asyncio.wait_for(
        stack.bus.publish(
            RemoteJoinRequestApproved(
                request_id=rid, space_id=space.id, invite_token="host-minted-token"
            )
        ),
        timeout=1.0,
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert (await stack.space_repo.get_member(space.id, bob.user_id)) is None
    # The pipeline moves on, the ACK is read, the redeem finishes: seated.
    ack.set()
    await asyncio.wait_for(stack.svc.wait_join_approvals(), timeout=1.0)
    member = await stack.space_repo.get_member(space.id, bob.user_id)
    assert member is not None and member.role == SpaceRole.MEMBER


async def test_join_approval_redeem_failure_is_still_logged(stack, caplog):
    """Handed off, a failed redeem is still surfaced at WARNING."""
    await _user(stack, "alicehost")
    bob = await _user(stack, "bobapp")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        space_type=SpaceType.PUBLIC,
    )
    rid = await stack.svc.request_join_remote(
        space.id,
        applicant_user_id=bob.user_id,
        host_instance_id="peer",
    )
    coordinator = MagicMock()
    coordinator.request_redeem = AsyncMock(side_effect=TimeoutError("host down"))
    stack.svc.attach_redeem_coordinator(coordinator)
    with caplog.at_level("WARNING"):
        await stack.bus.publish(
            RemoteJoinRequestApproved(
                request_id=rid, space_id=space.id, invite_token="host-minted-token"
            )
        )
        await stack.svc.wait_join_approvals()
    assert (await stack.space_repo.get_member(space.id, bob.user_id)) is None
    assert any("approval failed to seat" in r.getMessage() for r in caplog.records)


# ── approve_join_request / deny_join_request ──────────────────────


async def test_deny_local_join_request(stack):
    await _user(stack, "alicehost")
    bob = await _user(stack, "bobrequester")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        join_mode=JoinMode.REQUEST,
        space_type=SpaceType.PUBLIC,
    )
    rid = await stack.svc.request_join(
        space.id,
        user_id=bob.user_id,
        message="please",
    )
    await stack.svc.deny_join_request(rid, actor_username="alicehost")
    assert (await stack.space_repo.get_member(space.id, bob.user_id)) is None


async def test_approve_local_join_request(stack):
    await _user(stack, "alicehost")
    bob = await _user(stack, "bobrequester")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        join_mode=JoinMode.REQUEST,
        space_type=SpaceType.PUBLIC,
    )
    rid = await stack.svc.request_join(
        space.id,
        user_id=bob.user_id,
    )
    member = await stack.svc.approve_join_request(
        rid,
        actor_username="alicehost",
    )
    assert member is not None
    assert member.user_id == bob.user_id


async def test_approve_unknown_request_raises(stack):
    await _user(stack, "alicehost")
    await stack.svc.create_space(owner_username="alicehost", name="S")
    with pytest.raises(KeyError):
        await stack.svc.approve_join_request(
            "missing-rid",
            actor_username="alicehost",
        )


# ── mesh-aware delegation for the private-invite family ────────────
#
# Post-refactor, the SpaceService private-invite family delegates to
# ``FederationService.send_with_mesh_fallback`` — the fed service
# decides direct vs mesh. These tests pin the SpaceService contract:
# the right payload reaches the helper, and a failed DeliveryResult
# surfaces as :class:`SpacePermissionError`. The federation-side
# tests cover the direct-vs-mesh branching itself.


async def test_invite_remote_user_uses_mesh_fallback_helper(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="Private",
        space_type=SpaceType.PRIVATE,
    )
    token = await stack.svc.invite_remote_user(
        space.id,
        actor_username="alicehost",
        invitee_instance_id="peer",
        invitee_user_id="bob",
    )
    assert token
    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()
    call = stack.fed_svc.send_with_mesh_fallback.call_args
    assert call.kwargs["to_instance_id"] == "peer"
    assert call.kwargs["event_type"] == FederationEventType.SPACE_PRIVATE_INVITE
    assert call.kwargs["payload"]["invitee_user_id"] == "bob"
    assert call.kwargs["payload"]["invite_token"] == token


async def test_accept_remote_invite_uses_mesh_fallback_helper(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
    )
    await stack.space_repo.save_remote_invitation(
        space_id=space.id,
        invited_by="alicehost-id",
        remote_instance_id="peer",
        remote_user_id="bob",
        invite_token="tok-accept-mesh",
        space_display_hint="S",
    )
    await stack.svc.accept_remote_invite(
        token="tok-accept-mesh",
        user_id="bob",
    )
    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()
    call = stack.fed_svc.send_with_mesh_fallback.call_args
    assert call.kwargs["event_type"] == FederationEventType.SPACE_PRIVATE_INVITE_ACCEPT


async def _accept_payload(stack, token: str) -> dict:
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    await stack.space_repo.save_remote_invitation(
        space_id=space.id,
        invited_by="alicehost-id",
        remote_instance_id="peer",
        remote_user_id="bob",
        invite_token=token,
        space_display_hint="S",
    )
    await stack.svc.accept_remote_invite(token=token, user_id="bob")
    return stack.fed_svc.send_with_mesh_fallback.call_args.kwargs["payload"]


async def test_accept_to_a_mesh_only_host_carries_our_version_claim(stack):
    """The host reaches us only over the mesh (no ``remote_instances`` row
    either way), so the accept tells it our version + identity key — it
    can then deliver our writer cert like a paired member's."""
    from socialhome.domain.federation_capabilities import OURS

    stack.fed_repo.get_instance = AsyncMock(return_value=None)
    payload = await _accept_payload(stack, "tok-accept-claim")
    assert payload["member_proto_version"] == OURS
    assert payload["member_identity_pk"] == stack.fed_svc.own_identity_pk.hex()


async def test_accept_to_a_paired_host_carries_no_claim(stack):
    payload = await _accept_payload(stack, "tok-accept-paired")
    assert "member_proto_version" not in payload
    assert "member_identity_pk" not in payload


async def test_decline_remote_invite_uses_mesh_fallback_helper(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
    )
    await stack.space_repo.save_remote_invitation(
        space_id=space.id,
        invited_by="alicehost-id",
        remote_instance_id="peer",
        remote_user_id="bob",
        invite_token="tok-decline-mesh",
        space_display_hint="S",
    )
    await stack.svc.decline_remote_invite(
        token="tok-decline-mesh",
        user_id="bob",
    )
    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()
    call = stack.fed_svc.send_with_mesh_fallback.call_args
    assert call.kwargs["event_type"] == FederationEventType.SPACE_PRIVATE_INVITE_DECLINE


async def test_remove_remote_member_uses_mesh_fallback_helper(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
    )
    await stack.svc.remove_remote_member(
        space.id,
        actor_username="alicehost",
        instance_id="peer",
        user_id="bob",
    )
    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()
    call = stack.fed_svc.send_with_mesh_fallback.call_args
    assert call.kwargs["event_type"] == FederationEventType.SPACE_REMOTE_MEMBER_REMOVED


async def test_invite_remote_user_raises_when_fed_returns_no_route(stack):
    """Federation helper returning ok=False (no_route) surfaces as a
    SpacePermissionError so the route layer returns 4xx rather than 200.
    """
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="Private",
        space_type=SpaceType.PRIVATE,
    )
    stack.fed_svc.send_with_mesh_fallback.return_value = DeliveryResult(
        instance_id="peer",
        ok=False,
        error="no_route",
    )
    with pytest.raises(SpacePermissionError):
        await stack.svc.invite_remote_user(
            space.id,
            actor_username="alicehost",
            invitee_instance_id="peer",
            invitee_user_id="bob",
        )
    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()


# ── set_remote_member_role (#114) ───────────────────────────────────


async def test_set_remote_member_role_promotes_and_broadcasts(stack):
    """Owner promotes a remote member to admin → SQL row flips +
    SPACE_MEMBER_ROLE_CHANGED federates to every member household."""
    stack.fed_svc.broadcast_to_space_members = AsyncMock()
    _alice = await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    # Seat a remote member directly via the repo.
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="peer-bob",
        user_id="bob",
        user_pk=None,
        display_name="Bob",
    )

    await stack.svc.set_remote_member_role(
        space.id,
        actor_username="alicehost",
        instance_id="peer-bob",
        user_id="bob",
        role="admin",
    )

    member = await stack.svc._remote_members.get(space.id, "peer-bob", "bob")
    assert member is not None
    assert member.role == "admin"
    # A role change now ALSO emits a SPACE_MEMBER_JOINED roster gossip (v_23),
    # so assert the SPACE_MEMBER_ROLE_CHANGED broadcast specifically.
    role_calls = [
        c
        for c in stack.fed_svc.broadcast_to_space_members.await_args_list
        if c.args[1] is FederationEventType.SPACE_MEMBER_ROLE_CHANGED
    ]
    assert len(role_calls) == 1
    args = role_calls[0]
    assert args.args[0] == space.id
    assert args.args[2]["role"] == "admin"
    assert args.args[2]["instance_id"] == "peer-bob"
    assert args.args[2]["user_id"] == "bob"


async def test_set_remote_member_role_rejects_owner_role(stack):
    _a = await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="peer",
        user_id="u",
        user_pk=None,
        display_name=None,
    )
    with pytest.raises(ValueError):
        await stack.svc.set_remote_member_role(
            space.id,
            actor_username="alicehost",
            instance_id="peer",
            user_id="u",
            role="owner",
        )


async def test_set_remote_member_role_requires_owner(stack):
    """Non-owners get a permission error — same as the local
    set_role path."""
    _a = await _user(stack, "alicehost")
    bob = await _user(stack, "bobhost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    # Promote bob to admin to verify even admin can't make this call.
    await stack.svc.add_member(
        space.id,
        actor_username="alicehost",
        user_id=bob.user_id,
        role="admin",
    )
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="peer",
        user_id="u",
        user_pk=None,
        display_name=None,
    )
    with pytest.raises(SpacePermissionError):
        await stack.svc.set_remote_member_role(
            space.id,
            actor_username="bobhost",
            instance_id="peer",
            user_id="u",
            role="admin",
        )


async def test_set_remote_member_role_idempotent_skips_broadcast(stack):
    """Setting the role to its current value is a no-op — no
    broadcast, no config-sequence bump."""
    stack.fed_svc.broadcast_to_space_members = AsyncMock()
    _a = await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="peer",
        user_id="u",
        user_pk=None,
        display_name=None,
    )
    # Default role is 'member'; setting again to 'member' is a no-op.
    await stack.svc.set_remote_member_role(
        space.id,
        actor_username="alicehost",
        instance_id="peer",
        user_id="u",
        role="member",
    )
    stack.fed_svc.broadcast_to_space_members.assert_not_awaited()


async def test_set_remote_member_role_missing_member_raises(stack):
    _a = await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    with pytest.raises(KeyError):
        await stack.svc.set_remote_member_role(
            space.id,
            actor_username="alicehost",
            instance_id="ghost",
            user_id="nobody",
            role="admin",
        )


# ── apply_remote_admin_kick + remote-space kick routing (#114 phase 2) ──


async def _seat_remote_stub(stack, *, space_id, user_id, role):
    """Build a stub space hosted on someone else + seat our local
    user with the given role."""
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceMember,
        SpaceType,
    )

    stub = Space(
        id=space_id,
        name="Hosted Elsewhere",
        owner_instance_id="instance-remote-host",
        owner_username="bob@remotehost",
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
            space_id=space_id,
            user_id=user_id,
            role=role,
            joined_at="2026-05-23T00:00:00+00:00",
        )
    )
    return stub


async def test_remove_member_on_remote_space_routes_to_host(stack):
    """When the local user kicks someone in a space hosted on another
    household, the kick MUST forward to the host as
    SPACE_REMOTE_ADMIN_KICK instead of mutating the local stub."""
    alice = await _user(stack, "alicehost")
    await _seat_remote_stub(
        stack,
        space_id="sp-remote",
        user_id=alice.user_id,
        role="admin",
    )

    await stack.svc.remove_member(
        "sp-remote",
        actor_username="alicehost",
        user_id="u-victim",
    )

    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()
    call = stack.fed_svc.send_with_mesh_fallback.call_args
    assert call.kwargs["to_instance_id"] == "instance-remote-host"
    assert call.kwargs["event_type"] is FederationEventType.SPACE_REMOTE_ADMIN_KICK
    assert call.kwargs["payload"]["actor_user_id"] == alice.user_id
    assert call.kwargs["payload"]["target_user_id"] == "u-victim"
    assert call.kwargs["payload"]["actor_instance_id"] == stack.svc._own_instance_id


async def test_remove_member_self_on_remote_space_stays_local(stack):
    """Self-leave on a remote space still runs the local path —
    user is dropping their own stub membership, not asking the host
    to kick anyone."""
    alice = await _user(stack, "alicehost")
    await _seat_remote_stub(
        stack,
        space_id="sp-remote-2",
        user_id=alice.user_id,
        role="member",
    )

    await stack.svc.remove_member(
        "sp-remote-2",
        actor_username="alicehost",
        user_id=alice.user_id,
    )
    sent_types = [
        c.kwargs.get("event_type")
        for c in stack.fed_svc.send_with_mesh_fallback.call_args_list
    ]
    assert FederationEventType.SPACE_REMOTE_ADMIN_KICK not in sent_types


async def test_apply_remote_admin_kick_admin_dispatches_remote_member_remove(stack):
    """Host receives SPACE_REMOTE_ADMIN_KICK from a legitimate admin
    → dispatches to remove_remote_member (target was on a different
    household than the actor)."""
    _alice = await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")

    # Seat two remote members. Actor is admin on instance-A; target
    # is a regular member on instance-C.
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="instance-A",
        user_id="u-admin-on-A",
        user_pk=None,
        display_name=None,
    )
    await stack.svc._remote_members.set_role(
        space.id,
        "instance-A",
        "u-admin-on-A",
        "admin",
    )
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="instance-C",
        user_id="u-victim",
        user_pk=None,
        display_name=None,
    )

    stack.fed_svc.broadcast_to_space_members = AsyncMock()

    await stack.svc.apply_remote_admin_kick(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin-on-A",
        target_user_id="u-victim",
    )
    # Victim's row is gone.
    assert (
        await stack.svc._remote_members.get(space.id, "instance-C", "u-victim")
    ) is None


async def test_apply_remote_admin_kick_non_admin_silently_drops(stack):
    """Actor with role='member' (not admin) → dropped. No mutation,
    no exception."""
    _a = await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="instance-A",
        user_id="u-not-an-admin",
        user_pk=None,
        display_name=None,
    )
    # Add a victim so we can detect if the kick mistakenly ran.
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="instance-C",
        user_id="u-victim",
        user_pk=None,
        display_name=None,
    )

    await stack.svc.apply_remote_admin_kick(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-not-an-admin",
        target_user_id="u-victim",
    )
    # Victim is still there.
    assert (
        await stack.svc._remote_members.get(space.id, "instance-C", "u-victim")
    ) is not None


async def test_apply_remote_admin_kick_target_owner_rejected(stack):
    """Owner cannot be kicked through this path — same invariant
    as remove_member."""
    alice = await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="instance-A",
        user_id="u-admin",
        user_pk=None,
        display_name=None,
    )
    await stack.svc._remote_members.set_role(
        space.id,
        "instance-A",
        "u-admin",
        "admin",
    )

    await stack.svc.apply_remote_admin_kick(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        target_user_id=alice.user_id,
    )
    # Owner row still present.
    assert (await stack.space_repo.get_member(space.id, alice.user_id)) is not None


async def test_apply_remote_admin_kick_unknown_space_drops(stack):
    """A kick for an unknown space drops silently — common after
    SPACE_DISSOLVED races with a stale outbox."""
    await stack.svc.apply_remote_admin_kick(
        "sp-nonexistent",
        actor_instance_id="instance-A",
        actor_user_id="u",
        target_user_id="t",
    )  # Should not raise.


# ── cross-household admin actions (#114, v_15) ──────────────────────
#
# Two halves to each action: the *forward* (a remote admin on a stub
# ships SPACE_REMOTE_ADMIN_ACTION to the host instead of mutating the
# stub) and the host-side *dispatch* (apply_remote_admin_action
# re-validates the actor's role and runs the real method as owner).


async def _remote_stub_space(stack, *, host="instance-remote-host"):
    """Insert a stub space hosted on another household (owner_instance_id
    != own) and seat the local ``localadmin`` user as a local admin so
    ``_require_admin_or_owner`` passes before the forward fires.

    ``space_repo.save`` does not update ``owner_instance_id`` on conflict,
    so the stub must be a *fresh* id (not a re-homed create_space)."""
    from socialhome.domain.space import Space, SpaceMember

    localadmin = await _user(stack, "localadmin")
    stub = Space(
        id="sp-hosted-elsewhere",
        name="S",
        owner_instance_id=host,
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
            user_id=localadmin.user_id,
            role=SpaceRole.ADMIN,
            joined_at="2026-06-01T00:00:00+00:00",
        )
    )
    return stub


async def test_update_config_remote_forwards_to_host(stack):
    """A config edit on a remote stub forwards the action to the host
    and does NOT mutate the local stub (which isn't authoritative)."""
    stub = await _remote_stub_space(stack)
    await stack.svc.update_config(stub.id, actor_username="localadmin", name="Renamed")
    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()
    call = stack.fed_svc.send_with_mesh_fallback.call_args
    assert call.kwargs["event_type"] is (FederationEventType.SPACE_REMOTE_ADMIN_ACTION)
    assert call.kwargs["to_instance_id"] == "instance-remote-host"
    payload = call.kwargs["payload"]
    assert payload["action"] == "update_config"
    assert payload["params"]["name"] == "Renamed"
    # Local stub untouched — the rename only lands when the host
    # federates SPACE_CONFIG_CHANGED back.
    refreshed = await stack.space_repo.get(stub.id)
    assert refreshed.name == "S"


async def test_update_config_remote_forwards_only_changed_features(stack):
    """The forwarded params carry only the feature keys that differ from the
    stub's copy — a full snapshot would re-send every key the admin didn't
    touch and revert any host change the stub hasn't caught up with yet."""
    stub = await _remote_stub_space(stack)
    feats = SpaceFeatures(calendar=False, bazaar=False)
    await stack.svc.update_config(stub.id, actor_username="localadmin", features=feats)
    payload = stack.fed_svc.send_with_mesh_fallback.call_args.kwargs["payload"]
    assert payload["params"]["features"] == {"calendar": False, "bazaar": False}


async def test_update_config_remote_drops_unchanged_features(stack):
    """A features block identical to the stub's is not forwarded at all."""
    stub = await _remote_stub_space(stack)
    await stack.svc.update_config(
        stub.id,
        actor_username="localadmin",
        name="Renamed",
        features=SpaceFeatures(),
    )
    params = stack.fed_svc.send_with_mesh_fallback.call_args.kwargs["payload"]["params"]
    assert params == {"name": "Renamed"}


async def test_update_config_remote_nothing_to_forward_sends_nothing(stack):
    """An edit that changes nothing (e.g. the route's always-present features
    block equal to the stub's) doesn't ship an empty action to the host."""
    stub = await _remote_stub_space(stack)
    await stack.svc.update_config(
        stub.id, actor_username="localadmin", features=SpaceFeatures()
    )
    stack.fed_svc.send_with_mesh_fallback.assert_not_awaited()


async def test_ban_remote_forwards_to_host(stack):
    stub = await _remote_stub_space(stack)
    await stack.svc.ban(
        stub.id, actor_username="localadmin", user_id="victim", reason="spam"
    )
    payload = stack.fed_svc.send_with_mesh_fallback.call_args.kwargs["payload"]
    assert payload["action"] == "ban"
    assert payload["params"] == {"user_id": "victim", "reason": "spam"}


async def test_archive_remote_forwards_to_host(stack):
    stub = await _remote_stub_space(stack)
    await stack.svc.archive_space(stub.id, actor_username="localadmin")
    payload = stack.fed_svc.send_with_mesh_fallback.call_args.kwargs["payload"]
    assert payload["action"] == "archive"


async def test_unarchive_remote_forwards_to_host(stack):
    stub = await _remote_stub_space(stack)
    await stack.svc.unarchive_space(stub.id, actor_username="localadmin")
    payload = stack.fed_svc.send_with_mesh_fallback.call_args.kwargs["payload"]
    assert payload["action"] == "unarchive"


async def test_remote_admin_action_raises_when_host_too_old(stack):
    """Sub-v_15 host → SpacePermissionError, NOT a silent stub mutation."""
    stack.fed_svc.peer_supports = AsyncMock(return_value=False)
    stub = await _remote_stub_space(stack)
    with pytest.raises(SpacePermissionError):
        await stack.svc.archive_space(stub.id, actor_username="localadmin")
    stack.fed_svc.send_with_mesh_fallback.assert_not_awaited()


# ── apply_remote_admin_action (host side) ───────────────────────────


async def _host_space_with_remote_admin(stack, *, delegation=True):
    """Host-owned space + a remote admin seated on instance-A.

    ``delegation`` flips ``delegated_admin_authority`` so the host-side gate
    auto-executes a forwarded action (Phase 6a). Defaults ON because these
    coverage tests exercise the execute path.
    """
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    if delegation:
        await stack.svc.update_config(
            space.id,
            actor_username="alicehost",
            features=SpaceFeatures(delegated_admin_authority=True),
        )
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="instance-A",
        user_id="u-admin",
        user_pk=None,
        display_name=None,
    )
    await stack.svc._remote_members.set_role(space.id, "instance-A", "u-admin", "admin")
    return space


async def test_apply_remote_admin_action_update_config(stack):
    space = await _host_space_with_remote_admin(stack)
    await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="update_config",
        params={"name": "From Remote Admin"},
    )
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.name == "From Remote Admin"


async def test_apply_remote_admin_action_sanitises_exempt_types(stack):
    """A forwarded config edit drops exempt values we don't know (a newer
    peer's post type, or garbage) instead of failing the whole edit."""
    space = await _host_space_with_remote_admin(stack)
    await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="update_config",
        params={
            "name": "Renamed",
            "retention_exempt_types": ["poll", "pages", "future_type", 3],
        },
    )
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.name == "Renamed"
    assert refreshed.retention_exempt_types == ("poll",)


async def test_forwarded_rename_leaves_host_retention_intact(stack):
    """A remote admin's forwarded save that doesn't name the retention fields
    (the SPA now sends only what the admin changed) must not touch the host's
    retention — absent means "no change", never "reset to Forever"."""
    space = await _host_space_with_remote_admin(stack)
    await stack.svc.update_config(
        space.id,
        actor_username="alicehost",
        retention_days=30,
        retention_exempt_types=["poll"],
    )
    outcome = await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="update_config",
        params={"name": "Renamed by co-admin"},
    )
    assert outcome is RemoteAdminOutcome.EXECUTED
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.name == "Renamed by co-admin"
    assert refreshed.retention_days == 30
    assert refreshed.retention_exempt_types == ("poll",)


async def test_forwarded_config_ignores_fields_a_remote_admin_may_not_set(stack):
    """``space_type`` (quorum-gated), unknown keys like ``join_code`` and the
    owner-only feature flags are dropped / pinned on the host even when a
    forwarded edit carries them."""
    space = await _host_space_with_remote_admin(stack)
    await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="update_config",
        params={
            "space_type": "public",
            "join_code": "HIJACK",
            "features": {"allow_subscribers": True, "calendar": False},
        },
    )
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.space_type is SpaceType.PRIVATE
    assert refreshed.join_code != "HIJACK"
    assert refreshed.features.allow_subscribers is False
    assert refreshed.features.delegated_admin_authority is True
    assert refreshed.features.calendar is False


async def test_apply_remote_admin_action_update_config_rebuilds_features(stack):
    space = await _host_space_with_remote_admin(stack)
    feats = SpaceFeatures(bazaar=False, calendar=False)
    await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="update_config",
        params={"features": feats.to_wire_dict()},
    )
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.features.bazaar is False
    assert refreshed.features.calendar is False


async def test_apply_remote_admin_action_drops_unknown_config_kwargs(stack):
    """An unexpected wire key never reaches update_config as a kwarg."""
    space = await _host_space_with_remote_admin(stack)
    # Should not raise (would be a TypeError if injected as a kwarg).
    await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="update_config",
        params={"name": "Ok", "actor_username": "evil", "bogus": 1},
    )
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.name == "Ok"


async def test_apply_remote_admin_action_archive(stack):
    space = await _host_space_with_remote_admin(stack)
    await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="archive",
    )
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.archived is True


async def test_apply_remote_admin_action_ban(stack):
    space = await _host_space_with_remote_admin(stack)
    victim = await _user(stack, "victimlocal")
    await stack.svc.add_member(
        space.id, actor_username="alicehost", user_id=victim.user_id, role="member"
    )
    await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="ban",
        params={"user_id": victim.user_id, "reason": "nope"},
    )
    assert await stack.space_repo.get_member(space.id, victim.user_id) is None


async def test_apply_remote_admin_action_rejects_non_admin(stack):
    """A remote *member* (not admin) is silently dropped."""
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="instance-A",
        user_id="u-member",
        user_pk=None,
        display_name=None,
    )  # role defaults to member
    await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-member",
        action="update_config",
        params={"name": "Hacked"},
    )
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.name == "S"


async def test_apply_remote_admin_action_unknown_action_noop(stack):
    space = await _host_space_with_remote_admin(stack)
    await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="dissolve",  # owner-only; never forwardable
        params={},
    )  # Should not raise.
    assert await stack.space_repo.get(space.id) is not None


async def test_apply_remote_admin_action_not_hosted_here_drops(stack):
    """If the space is hosted elsewhere, the host-side dispatch drops."""
    stub = await _remote_stub_space(stack)
    await stack.svc.apply_remote_admin_action(
        stub.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="archive",
        params={},
    )  # Should not raise / not mutate.
    refreshed = await stack.space_repo.get(stub.id)
    assert refreshed.archived is False


async def test_update_config_remote_forwards_all_fields(stack):
    """Every provided field is serialised into the forwarded params."""
    stub = await _remote_stub_space(stack)
    await stack.svc.update_config(
        stub.id,
        actor_username="localadmin",
        name="N",
        description="D",
        emoji="🌟",
        features=SpaceFeatures(),
        join_mode=JoinMode.OPEN,
        space_type=SpaceType.PRIVATE,
        retention_days=30,
        retention_exempt_types=["text"],
        about_markdown="hello",
        bot_enabled=True,
        allow_here_mention=True,
    )
    params = stack.fed_svc.send_with_mesh_fallback.call_args.kwargs["payload"]["params"]
    assert params["name"] == "N"
    assert params["description"] == "D"
    assert params["emoji"] == "🌟"
    # Identical to the stub's features → nothing to forward for them.
    assert "features" not in params
    assert params["join_mode"] == JoinMode.OPEN.value
    assert params["space_type"] == SpaceType.PRIVATE.value
    assert params["retention_days"] == 30
    assert params["retention_exempt_types"] == ["text"]
    assert params["about_markdown"] == "hello"
    assert params["bot_enabled"] is True
    assert params["allow_here_mention"] is True


async def test_apply_remote_admin_action_unarchive(stack):
    space = await _host_space_with_remote_admin(stack)
    await stack.svc.archive_space(space.id, actor_username="alicehost")
    await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="unarchive",
    )
    refreshed = await stack.space_repo.get(space.id)
    assert refreshed.archived is False


async def test_apply_remote_admin_action_unban(stack):
    space = await _host_space_with_remote_admin(stack)
    victim = await _user(stack, "victimlocal")
    await stack.svc.add_member(
        space.id, actor_username="alicehost", user_id=victim.user_id, role="member"
    )
    await stack.svc.ban(space.id, actor_username="alicehost", user_id=victim.user_id)
    await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="unban",
        params={"user_id": victim.user_id},
    )
    # The ban row is cleared (unban succeeded as the owner).
    assert await stack.space_repo.is_banned(space.id, victim.user_id) is False


async def test_apply_remote_admin_action_ban_missing_user_noop(stack):
    space = await _host_space_with_remote_admin(stack)
    # No user_id in params → drop without raising.
    await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="ban",
        params={},
    )
    await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="unban",
        params={},
    )  # Should not raise.


async def test_apply_remote_admin_action_unknown_space_drops(stack):
    await stack.svc.apply_remote_admin_action(
        "sp-nonexistent",
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="archive",
        params={},
    )  # Should not raise.


# ── owner-approval gate end-to-end (Phase 6a) ──────────────────────
#
# The seams above are exercised in isolation; this drives the WHOLE
# chain with REAL services and no execute-path mocks: the real
# ``PrivateSpaceInviteHandler._on_remote_admin_action`` receives a
# forwarded ``SPACE_REMOTE_ADMIN_ACTION``, the real
# ``SpaceService.apply_remote_admin_action`` returns
# ``NEEDS_OWNER_APPROVAL`` (delegation OFF), the real
# ``SpaceApprovalService.enqueue_owner_approval`` records a pending
# owner-only proposal, and the OWNER's real ``vote`` runs it through
# ``apply_approved_admin_action`` → ``_run_admin_action`` for real.


def _remote_admin_action_event(
    *, space_id, from_instance, actor_user_id, action, params
):
    """A fully-validated inbound SPACE_REMOTE_ADMIN_ACTION as the §24.11
    pipeline would hand it to the handler. ``from_instance`` is the signed
    sender; the handler binds the actor's household to it (never trusts a
    payload-supplied instance id)."""
    return FederationEvent(
        msg_id="m-1",
        event_type=FederationEventType.SPACE_REMOTE_ADMIN_ACTION,
        from_instance=from_instance,
        to_instance="host-self",
        timestamp="2026-06-10T00:00:00+00:00",
        payload={
            "space_id": space_id,
            "actor_user_id": actor_user_id,
            "action": action,
            "params": params,
        },
        space_id=space_id,
    )


async def test_owner_approval_gate_end_to_end_real_services(stack):
    """delegation OFF: a forwarded ban is held for the owner, then runs
    for real only after the OWNER approves — REAL handler + approval
    service + space service, no execute-path mocks."""
    # 1. Host-owned space (delegation OFF by default) + a remote ADMIN on
    #    household B, plus a local target member to ban.
    alice = await _user(stack, "alicehost")
    space = await stack.svc.create_space(owner_username="alicehost", name="S")
    assert space.features.delegated_admin_authority is False
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="household-B",
        user_id="bob-admin",
        user_pk=None,
        display_name="Bob",
    )
    await stack.svc._remote_members.set_role(
        space.id, "household-B", "bob-admin", "admin"
    )
    victim = await _user(stack, "victimlocal")
    await stack.svc.add_member(
        space.id, actor_username="alicehost", user_id=victim.user_id, role="member"
    )

    # 2. REAL approval service wired to the REAL space service, sharing the
    #    same repos/bus/instance id (mirrors app.py `_build_services`).
    approvals = SpaceApprovalService(
        SqliteSpaceProposalRepo(stack.db),
        stack.svc._spaces,
        stack.svc._remote_members,
        stack.svc._users,
        stack.svc._bus,
        own_instance_id=stack.svc._own_instance_id,
    )
    approvals.attach(space_service=stack.svc)

    # 3. REAL inbound handler — drive the forwarded action through it.
    handler = PrivateSpaceInviteHandler(
        bus=stack.svc._bus,
        space_repo=stack.svc._spaces,
        remote_member_repo=stack.svc._remote_members,
        space_service=stack.svc,
    )
    handler.attach_approval_service(approvals)
    event = _remote_admin_action_event(
        space_id=space.id,
        from_instance="household-B",
        actor_user_id="bob-admin",
        action="ban",
        params={"user_id": victim.user_id, "reason": "spam"},
    )
    await handler._on_remote_admin_action(event)

    # 4. A pending OWNER-ONLY proposal now exists; the ban has NOT run.
    listed = await approvals.list_for_space(space.id, viewer_user_id=alice.user_id)
    assert len(listed) == 1
    proposal = listed[0]
    assert proposal["action"] == ProposalAction.REMOTE_ADMIN_ACTION.value
    assert proposal["status"] == ProposalStatus.PENDING.value
    assert proposal["owner_only"] is True
    assert proposal["fwd_action"] == "ban"
    assert proposal["fwd_params"] == {"user_id": victim.user_id, "reason": "spam"}
    assert await stack.space_repo.get_member(space.id, victim.user_id) is not None

    # 5. NEGATIVE leg: a non-owner admin's vote must NOT execute; the
    #    proposal stays pending and the victim stays a member.
    nonowner = await _user(stack, "bobadmin")
    await stack.svc.add_member(
        space.id, actor_username="alicehost", user_id=nonowner.user_id, role="admin"
    )
    out = await approvals.vote(
        space.id, proposal["id"], actor_username="bobadmin", approve=True
    )
    assert out["status"] == ProposalStatus.PENDING.value
    assert await stack.space_repo.get_member(space.id, victim.user_id) is not None

    # 6. OWNER approves → executes for real through
    #    apply_approved_admin_action → _run_admin_action.
    out = await approvals.vote(
        space.id, proposal["id"], actor_username="alicehost", approve=True
    )
    assert out["status"] == ProposalStatus.EXECUTED.value

    # 7. The ban actually ran as the owner: the target is no longer a member.
    assert await stack.space_repo.get_member(space.id, victim.user_id) is None


# ── a forward that reached nobody is never reported sent (503) ──────


_NO_ROUTE = DeliveryResult(
    instance_id="instance-remote-host", ok=False, error="no_route"
)


@pytest.mark.parametrize(
    "act",
    [
        lambda svc, sid: svc.ban(sid, actor_username="localadmin", user_id="victim"),
        lambda svc, sid: svc.unban(sid, actor_username="localadmin", user_id="victim"),
        lambda svc, sid: svc.archive_space(sid, actor_username="localadmin"),
        lambda svc, sid: svc.unarchive_space(sid, actor_username="localadmin"),
        lambda svc, sid: svc.invite_remote_user(
            sid,
            actor_username="localadmin",
            invitee_instance_id="peer",
            invitee_user_id="u-new",
        ),
    ],
    ids=["ban", "unban", "archive", "unarchive", "invite"],
)
async def test_forwarded_admin_action_to_an_unreachable_host_raises(stack, act):
    """A stub can't do any of these itself; when the forward went nowhere
    (not even queued) the caller learns it instead of a false "sent"."""
    stub = await _remote_stub_space(stack)
    stack.fed_svc.send_with_mesh_fallback = AsyncMock(return_value=_NO_ROUTE)
    with pytest.raises(HostUnreachableError):
        await act(stack.svc, stub.id)
    stack.fed_svc.send_with_mesh_fallback.assert_awaited_once()


async def test_a_queued_forward_still_counts_as_sent(stack):
    stub = await _remote_stub_space(stack)
    stack.fed_svc.send_with_mesh_fallback = AsyncMock(
        return_value=DeliveryResult(
            instance_id="instance-remote-host", ok=False, error=DELIVERY_ERROR_QUEUED
        )
    )
    await stack.svc.archive_space(stub.id, actor_username="localadmin")


# ── Migration 0093: the accept seats the stub from the invitation ─────────


async def _snapshot_invite(stack, user_id: str, *, token: str, issuer: str, **meta):
    await stack.space_repo.save_remote_invitation(
        space_id="sp-snap",
        invited_by="u-inviter",
        remote_instance_id=issuer,
        remote_user_id=user_id,
        invite_token=token,
        join_snapshot=seal_join_snapshot(
            {
                "name": "Snap",
                "owner_instance_id": issuer,
                "owner_username": "o",
                "identity_public_key": "ab" * 32,
                **meta,
            },
            space_id="sp-snap",
            invite_token=token,
            host_identity_pk=None,
            space_crypto_service=None,
        ),
    )


async def test_accept_seats_the_stub_from_the_snapshot_and_expires_the_rest(stack):
    bob = await _user(stack, "bobsnap")
    await _snapshot_invite(stack, bob.user_id, token="t-peer", issuer="peer")
    await _snapshot_invite(stack, bob.user_id, token="t-other", issuer="other")
    assert await stack.space_repo.get("sp-snap") is None
    await stack.svc.accept_remote_invite(token="t-peer", user_id=bob.user_id)
    space = await stack.space_repo.get("sp-snap")
    assert space is not None and space.owner_instance_id == "peer"
    assert await stack.space_repo.get_member("sp-snap", bob.user_id) is not None
    assert (await stack.space_repo.get_invitation_by_token("t-other"))[
        "status"
    ] == "expired"


async def test_accept_refuses_a_space_held_under_another_owner(stack):
    bob = await _user(stack, "bobclash")
    await _snapshot_invite(stack, bob.user_id, token="t-peer", issuer="peer")
    await _snapshot_invite(stack, bob.user_id, token="t-other", issuer="other")
    await stack.svc.accept_remote_invite(token="t-other", user_id=bob.user_id)
    stack.fed_svc.send_with_mesh_fallback.reset_mock()
    with pytest.raises(InviteExpiredError):
        # Expired by the first accept.
        await stack.svc.accept_remote_invite(token="t-peer", user_id=bob.user_id)
    # The expired invitation's snapshot is gone (L2); a fresh one from the
    # other household still cannot take the space over.
    assert (await stack.space_repo.get_invitation_by_token("t-peer"))[
        "join_snapshot"
    ] is None
    await _snapshot_invite(stack, bob.user_id, token="t-peer-2", issuer="peer")
    with pytest.raises(SpacePermissionError, match="conflicts"):
        await stack.svc.accept_remote_invite(token="t-peer-2", user_id=bob.user_id)
    stack.fed_svc.send_with_mesh_fallback.assert_not_awaited()
    assert (await stack.space_repo.get("sp-snap")).owner_instance_id == "other"


async def test_accept_age_gate_reads_the_snapshots_min_age(stack):
    """No stub exists before the accept, so §CP.F1 reads ``min_age`` from
    the invitation's snapshot — a minor is refused before anything is sent
    or seated."""
    bob = await _user(stack, "bobminor")
    await _snapshot_invite(stack, bob.user_id, token="t-18", issuer="peer", min_age=18)
    gate = MagicMock()
    gate.is_age_allowed = AsyncMock(return_value=False)
    gate.check_space_age_gate = AsyncMock()
    stack.svc._child_protection = gate
    with pytest.raises(AgeRestrictedError):
        await stack.svc.accept_remote_invite(token="t-18", user_id=bob.user_id)
    gate.is_age_allowed.assert_awaited_once_with(bob.user_id, 18)
    gate.check_space_age_gate.assert_not_awaited()
    stack.fed_svc.send_with_mesh_fallback.assert_not_awaited()
    assert await stack.space_repo.get("sp-snap") is None


async def test_concurrent_accepts_for_one_space_seat_exactly_one(stack):
    """M1 probe: two accepts for the same space id from different households
    racing across the (awaited) ACCEPT send — exactly one seats, the other
    fails cleanly, and no foreign household lands in ``space_instances``."""
    bob = await _user(stack, "bobrace")
    await _snapshot_invite(stack, bob.user_id, token="t-peer", issuer="peer")
    await _snapshot_invite(stack, bob.user_id, token="t-other", issuer="other")
    gate = asyncio.Event()

    async def _slow_send(**kwargs):
        await gate.wait()
        return DeliveryResult(instance_id="x", ok=True)

    stack.fed_svc.send_with_mesh_fallback = AsyncMock(side_effect=_slow_send)
    first = asyncio.create_task(
        stack.svc.accept_remote_invite(token="t-peer", user_id=bob.user_id)
    )
    second = asyncio.create_task(
        stack.svc.accept_remote_invite(token="t-other", user_id=bob.user_id)
    )
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    # Whichever took the lock first wins; the other is refused cleanly.
    assert sorted(type(r).__name__ for r in results) == [
        "InviteExpiredError",
        "NoneType",
    ]
    winner = "peer" if results[0] is None else "other"
    assert (await stack.space_repo.get("sp-snap")).owner_instance_id == winner
    assert await stack.space_repo.list_member_instances("sp-snap") == [winner]


async def test_a_failed_accept_send_rolls_the_seat_back(stack):
    """M3/L5: the stub is seated before the ACCEPT goes out; a send that
    fails leaves nothing behind and the invitation pending."""
    bob = await _user(stack, "bobfail")
    await _snapshot_invite(
        stack,
        bob.user_id,
        token="t-peer",
        issuer="peer",
        roster=[{"user_id": "u-p", "instance_id": "peer", "role": "owner"}],
    )
    seen: list[bool] = []

    async def _failing_send(**kwargs):
        seen.append(await stack.space_repo.get("sp-snap") is not None)
        return DeliveryResult(instance_id="peer", ok=False)

    stack.fed_svc.send_with_mesh_fallback = AsyncMock(side_effect=_failing_send)
    with pytest.raises(SpacePermissionError):
        await stack.svc.accept_remote_invite(token="t-peer", user_id=bob.user_id)
    assert seen == [True]  # seated before the ACCEPT left
    assert await stack.space_repo.get("sp-snap") is None
    assert await stack.space_repo.list_member_instances("sp-snap") == []
    row = await stack.space_repo.get_invitation_by_token("t-peer")
    assert row["status"] == "pending" and row["join_snapshot"]


async def test_a_failed_seat_leaves_nothing_and_sends_nothing(stack, monkeypatch):
    bob = await _user(stack, "bobseat")
    await _snapshot_invite(stack, bob.user_id, token="t-peer", issuer="peer")

    async def _boom(*args, **kwargs):
        raise RuntimeError("cover write failed")

    monkeypatch.setattr(
        "socialhome.services.space_service.apply_space_cover_from_metadata", _boom
    )
    with pytest.raises(RuntimeError):
        await stack.svc.accept_remote_invite(token="t-peer", user_id=bob.user_id)
    stack.fed_svc.send_with_mesh_fallback.assert_not_awaited()
    assert await stack.space_repo.get("sp-snap") is None


async def test_decline_checks_the_user_and_the_status(stack):
    bob = await _user(stack, "bobdecline")
    await _snapshot_invite(stack, bob.user_id, token="t-peer", issuer="peer")
    with pytest.raises(InviteExpiredError):
        await stack.svc.decline_remote_invite(token="t-peer", user_id="someone-else")
    await stack.svc.decline_remote_invite(token="t-peer", user_id=bob.user_id)
    row = await stack.space_repo.get_invitation_by_token("t-peer")
    assert row["status"] == "declined" and row["join_snapshot"] is None
    with pytest.raises(InviteExpiredError):
        await stack.svc.decline_remote_invite(token="t-peer", user_id=bob.user_id)


async def test_startup_sweep_drops_pre_0093_unjoined_stubs_only(stack):
    """L1: a stub an older build pre-seated for a never-accepted invitation
    is dropped with its snapshot-less pending invitation; a joined stub and
    an invite-link stub (no invitation row) stay."""
    bob = await _user(stack, "bobsweep")

    def _stub(space_id: str, owner: str) -> Space:
        return Space(
            id=space_id,
            name=space_id,
            owner_instance_id=owner,
            owner_username="o",
            identity_public_key="ab" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )

    for sid in ("sp-dust", "sp-joined", "sp-link"):
        await stack.space_repo.save(_stub(sid, "mal"))
    await stack.space_repo.add_space_instance("sp-dust", "mal")
    for sid, tok in (("sp-dust", "t-dust"), ("sp-joined", "t-joined")):
        await stack.space_repo.save_remote_invitation(
            space_id=sid,
            invited_by="u-mal",
            remote_instance_id="mal",
            remote_user_id=bob.user_id,
            invite_token=tok,
        )
    await stack.space_repo.update_invitation_status(
        (await stack.space_repo.get_invitation_by_token("t-joined"))["id"], "accepted"
    )
    assert await stack.svc.sweep_unjoined_invitation_stubs() == 1
    assert await stack.space_repo.get("sp-dust") is None
    assert await stack.space_repo.list_member_instances("sp-dust") == []
    assert (await stack.space_repo.get_invitation_by_token("t-dust"))[
        "status"
    ] == "expired"
    assert await stack.space_repo.get("sp-joined") is not None
    assert await stack.space_repo.get("sp-link") is not None
    assert await stack.svc.sweep_unjoined_invitation_stubs() == 0  # idempotent


async def test_a_delayed_accept_judges_freshness_at_receipt(stack):
    """M2: an admin's invitation proven fresh on receipt is still seated
    under the REAL owner when accepted after the statement's window."""
    owner_seed = b"\x51" * 32
    owner = derive_instance_id(ed25519_public_key(owner_seed))
    admin = derive_instance_id(ed25519_public_key(b"\x52" * 32))
    kp = generate_space_keypair()
    received = int(time.time() * 1000) - MAX_STATEMENT_AGE_MS - 3_600_000
    statement = sign_owner_statement(
        space_id="sp-snap",
        owner_seed=owner_seed,
        authority_pk_hex=kp.public_key.hex(),
        authority_key_epoch=0,
        issued_at_ms=received - 60_000,
    )
    bob = await _user(stack, "bobdelay")
    meta = {
        "name": "Snap",
        "owner_instance_id": owner,
        "owner_username": "o",
        "identity_public_key": kp.public_key.hex(),
        "owner_statement": statement,
        "inviter_proof": sign_inviter_proof(
            space_id="sp-snap",
            owner_instance_id=owner,
            inviter_instance_id=admin,
            space_seed=kp.private_key,
        ),
    }
    await stack.space_repo.save_remote_invitation(
        space_id="sp-snap",
        invited_by="u-admin",
        remote_instance_id=admin,
        remote_user_id=bob.user_id,
        invite_token="t-admin",
        join_snapshot=seal_join_snapshot(
            meta,
            space_id="sp-snap",
            invite_token="t-admin",
            host_identity_pk=None,
            space_crypto_service=None,
            received_at_ms=received,
            owner_instance_id=owner,
        ),
    )
    await stack.svc.accept_remote_invite(token="t-admin", user_id=bob.user_id)
    assert (await stack.space_repo.get("sp-snap")).owner_instance_id == owner


async def test_an_accept_never_degrades_to_the_issuer(stack):
    """M2: when the owner resolved on receipt no longer resolves, the accept
    is refused — never seated under the issuer — and other invitations stay
    pending."""
    bob = await _user(stack, "bobdegrade")
    await _snapshot_invite(stack, bob.user_id, token="t-other", issuer="other")
    meta = {
        "name": "Snap",
        "owner_instance_id": "peer",
        "owner_username": "o",
        "identity_public_key": "ab" * 32,
    }
    await stack.space_repo.save_remote_invitation(
        space_id="sp-snap",
        invited_by="u-admin",
        remote_instance_id="admin-hh",
        remote_user_id=bob.user_id,
        invite_token="t-admin",
        join_snapshot=seal_join_snapshot(
            meta,
            space_id="sp-snap",
            invite_token="t-admin",
            host_identity_pk=None,
            space_crypto_service=None,
            received_at_ms=int(time.time() * 1000),
            owner_instance_id="peer",  # what receipt resolved
        ),
    )
    with pytest.raises(SpacePermissionError, match="no longer"):
        await stack.svc.accept_remote_invite(token="t-admin", user_id=bob.user_id)
    stack.fed_svc.send_with_mesh_fallback.assert_not_awaited()
    assert await stack.space_repo.get("sp-snap") is None
    assert (await stack.space_repo.get_invitation_by_token("t-other"))[
        "status"
    ] == "pending"


async def test_an_unopenable_content_key_fails_the_accept(stack):
    """L6: the snapshot carries a key this household cannot open — refuse
    rather than seat a space it can never decrypt."""
    bob = await _user(stack, "bobkey")
    await _snapshot_invite(
        stack,
        bob.user_id,
        token="t-peer",
        issuer="peer",
        space_content_key={"epoch": 0, "key_base64": "AA=="},
    )
    with pytest.raises(SpacePermissionError, match="key"):
        await stack.svc.accept_remote_invite(token="t-peer", user_id=bob.user_id)
    stack.fed_svc.send_with_mesh_fallback.assert_not_awaited()
    assert await stack.space_repo.get("sp-snap") is None


@pytest.mark.parametrize(
    "error",
    [
        DELIVERY_ERROR_QUEUED,
        DELIVERY_ERROR_RELAY_THROTTLED,
        DELIVERY_ERROR_MESH_DEFERRED,
    ],
)
async def test_a_queued_accept_keeps_the_seat(stack, error):
    """Round 8: a parked ACCEPT reaches the host later — the host WILL seat
    us, so the stub and the accept stay (no rollback)."""
    bob = await _user(stack, f"bobq{len(error)}")
    await _snapshot_invite(stack, bob.user_id, token="t-peer", issuer="peer")
    stack.fed_svc.send_with_mesh_fallback = AsyncMock(
        return_value=DeliveryResult(instance_id="peer", ok=False, error=error)
    )
    await stack.svc.accept_remote_invite(token="t-peer", user_id=bob.user_id)
    assert (await stack.space_repo.get("sp-snap")).owner_instance_id == "peer"
    assert await stack.space_repo.get_member("sp-snap", bob.user_id) is not None
    assert (await stack.space_repo.get_invitation_by_token("t-peer"))[
        "status"
    ] == "accepted"


@pytest.mark.parametrize("error", ["not_confirmed", "no_route", "relay_too_large"])
async def test_a_definitive_accept_failure_rolls_back_with_a_clear_message(
    stack, error
):
    bob = await _user(stack, f"bobd{len(error)}")
    await _snapshot_invite(stack, bob.user_id, token="t-peer", issuer="peer")
    stack.fed_svc.send_with_mesh_fallback = AsyncMock(
        return_value=DeliveryResult(instance_id="peer", ok=False, error=error)
    )
    with pytest.raises(SpacePermissionError, match="cannot be reached"):
        await stack.svc.accept_remote_invite(token="t-peer", user_id=bob.user_id)
    assert await stack.space_repo.get("sp-snap") is None
    assert (await stack.space_repo.get_invitation_by_token("t-peer"))[
        "status"
    ] == "pending"


async def test_startup_sweep_spares_gfs_mirror_stubs(stack):
    """Round 8 L2: a public / global stub the household follows through a
    GFS (mirror provenance or a recorded GFS seat) is never swept, even with
    a private invitation for its id — its teardown is the mirror's."""
    bob = await _user(stack, "bobmirror")
    for sid in ("sp-mirror", "sp-seat"):
        await stack.space_repo.save(
            Space(
                id=sid,
                name=sid,
                owner_instance_id="mal",
                owner_username="o",
                identity_public_key="ab" * 32,
                config_sequence=0,
                features=SpaceFeatures(),
                space_type=SpaceType.GLOBAL,
                join_mode=JoinMode.INVITE_ONLY,
            )
        )
        await stack.space_repo.save_remote_invitation(
            space_id=sid,
            invited_by="u-mal",
            remote_instance_id="mal",
            remote_user_id=bob.user_id,
            invite_token=f"t-{sid}",
        )
    await stack.db.enqueue("UPDATE spaces SET mirror_gfs_id='g1' WHERE id='sp-mirror'")
    await stack.db.enqueue(
        "INSERT INTO gfs_space_seats(space_id, gfs_instance_id) VALUES('sp-seat','g1')"
    )
    assert await stack.svc.sweep_unjoined_invitation_stubs() == 0
    assert await stack.space_repo.get("sp-mirror") is not None
    assert await stack.space_repo.get("sp-seat") is not None


# ── v_77: a remote moderator forwards a games-list edit, and only that ──


async def _host_space_with_remote_moderator(stack, *, delegation=True):
    space = await _host_space_with_remote_admin(stack, delegation=delegation)
    await stack.svc._remote_members.add(
        space_id=space.id,
        instance_id="instance-M",
        user_id="u-mod",
        user_pk=None,
        display_name=None,
    )
    await stack.svc._remote_members.set_role(
        space.id, "instance-M", "u-mod", "moderator"
    )
    return space


async def test_a_remote_moderators_games_list_edit_is_applied(stack):
    space = await _host_space_with_remote_moderator(stack)
    outcome = await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-M",
        actor_user_id="u-mod",
        action="update_config",
        params={"features": {"allowed_apps": ["quiz"]}},
    )
    assert outcome is RemoteAdminOutcome.EXECUTED
    assert (await stack.space_repo.get(space.id)).features.allowed_apps == ("quiz",)


async def test_a_remote_moderator_forwards_nothing_else(stack):
    space = await _host_space_with_remote_moderator(stack)
    for params in (
        {"name": "Hacked"},
        {"features": {"allowed_apps": ["quiz"], "bazaar": False}},
        {"features": {"allowed_apps": ["quiz"]}, "name": "Hacked"},
    ):
        outcome = await stack.svc.apply_remote_admin_action(
            space.id,
            actor_instance_id="instance-M",
            actor_user_id="u-mod",
            action="update_config",
            params=params,
        )
        assert outcome is RemoteAdminOutcome.DROPPED
    stored = await stack.space_repo.get(space.id)
    assert (stored.name, stored.features.allowed_apps) == ("S", ())


async def test_a_remote_moderators_edit_waits_for_the_owner_without_delegation(stack):
    space = await _host_space_with_remote_moderator(stack, delegation=False)
    outcome = await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id="instance-M",
        actor_user_id="u-mod",
        action="update_config",
        params={"features": {"allowed_apps": ["quiz"]}},
    )
    assert outcome is RemoteAdminOutcome.NEEDS_OWNER_APPROVAL
