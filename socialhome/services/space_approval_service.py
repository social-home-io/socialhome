"""Multi-admin approval (quorum) for critical space actions (#114 follow-on).

Dissolving a space and changing its publication tier are too high-stakes
for one admin to do alone, so they become *proposals* that execute only
once a **majority of the space's admins approve** — the owner included.

Flow (host is authoritative):

* A local admin calls :meth:`propose` / :meth:`vote`. If the space is
  hosted on another household, the intent forwards to the host via
  ``SPACE_REMOTE_ADMIN_ACTION`` (``propose`` / ``vote`` verbs); otherwise
  it runs here.
* On the host, :meth:`_evaluate` recomputes the threshold against the
  *current* admin set after every vote: any reject cancels; once
  approvals exceed half the admins the proposal executes (runs the real
  :meth:`SpaceService.dissolve_space` / :meth:`SpaceService.update_config`
  as the owner, so the result federates through the normal outbounds).
* Every change publishes :class:`SpaceProposalUpdated` for the local WS
  fan-out and (host only) the ``SPACE_ADMIN_PROPOSAL_UPDATED`` broadcast
  that mirrors the state onto admin households.

The owner is bound by the same rule: a solo-admin space (owner only)
executes immediately (majority of 1), but the moment a second admin
exists no single person can dissolve or publish the space alone.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from ..domain.child_protection import (
    PROTECTED_OWNER_PUBLISH_DETAIL,
    ProtectedCapability,
)
from ..domain.events import SpaceProposalUpdated
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.space import (
    PUBLIC_SPACE_TIERS,
    SpacePermissionError,
    SETTINGS_AUTHORITY_ROLES,
    SpaceRole,
    SpaceType,
)
from ..domain.space_proposal import (
    ProposalAction,
    ProposalStatus,
    ProposalVote,
    SpaceAdminProposal,
    SpaceAdminProposalVote,
)
from ..infrastructure.event_bus import EventBus
from ..repositories.space_proposal_repo import AbstractSpaceProposalRepo
from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from .protection_gate import ProtectionGateMixin

log = logging.getLogger(__name__)

#: How long a proposal stays open before it lapses unapproved.
PROPOSAL_TTL = timedelta(days=7)

#: Cap on PENDING owner-approval requests (forwarded remote admin actions,
#: delegation off) one household may hold open per space. A household
#: whose admin floods the host is dropped past this; with the
#: ``set_member_role`` supersede (one open request per seat) it bounds
#: what the owner's approval list can grow to.
MAX_PENDING_OWNER_APPROVALS_PER_HOUSEHOLD = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _publishes(action: ProposalAction, params: dict) -> bool:
    """Whether a proposal would make the space public / global (§CP.R)."""
    if action is not ProposalAction.SET_PUBLIC_TIER:
        return False
    try:
        return SpaceType(str(params.get("space_type") or "")) in PUBLIC_SPACE_TIERS
    except ValueError:
        return False


class SpaceApprovalService(ProtectionGateMixin):
    """Quorum approval for ``dissolve`` and ``set_public_tier``."""

    __slots__ = (
        "_proposals",
        "_spaces",
        "_remote_members",
        "_users",
        "_bus",
        "_own_instance_id",
        "_federation",
        "_space_service",
        "_child_protection",
    )

    def __init__(
        self,
        proposal_repo: AbstractSpaceProposalRepo,
        space_repo: AbstractSpaceRepo,
        remote_member_repo: AbstractSpaceRemoteMemberRepo,
        user_repo: AbstractUserRepo,
        bus: EventBus,
        *,
        own_instance_id: str | None = None,
    ) -> None:
        self._proposals = proposal_repo
        self._spaces = space_repo
        self._remote_members = remote_member_repo
        self._users = user_repo
        self._bus = bus
        self._own_instance_id = own_instance_id
        self._federation = None
        self._space_service = None
        self._child_protection = None

    def attach(self, *, federation_service=None, space_service=None) -> None:
        """Wire the federation transport (for cross-household forward +
        broadcast) and the :class:`SpaceService` used to execute an
        approved proposal. Both are set after construction to avoid an
        import cycle; see :mod:`socialhome.app`."""
        if federation_service is not None:
            self._federation = federation_service
        if space_service is not None:
            self._space_service = space_service

    # ── Public entry points (called by routes with a local actor) ────────

    async def propose(
        self,
        space_id: str,
        *,
        actor_username: str,
        action: ProposalAction,
        params: dict | None = None,
    ) -> dict:
        """Open (or join) a proposal for a critical action. Returns the
        SPA-facing proposal view. When the space is hosted elsewhere the
        intent forwards to the host and a ``pending`` placeholder view is
        returned (the real state arrives via the mirror broadcast)."""
        if action is ProposalAction.REMOTE_ADMIN_ACTION:
            # Only the host's forward gate opens one (enqueue_owner_approval),
            # with the actor bound to the signed sender — never a proposer.
            raise ValueError("remote_admin_action proposals are not proposable")
        space = await self._require_space(space_id)
        actor = await self._require_local_admin(space_id, actor_username)
        params = params or {}
        if action is ProposalAction.SET_PUBLIC_TIER:
            # §CP.R: a protected account can't publish a space — neither by
            # asking for it nor by joining an open proposal that does
            # (``_host_propose`` reuses the open row with ITS params and
            # auto-approves for the proposer).
            open_row = await self._proposals.find_open(space_id, action)
            if _publishes(action, params) or (
                open_row is not None and _publishes(action, open_row.params)
            ):
                await self._require_unrestricted(
                    actor.user_id, ProtectedCapability.PUBLIC_SPACES
                )
        if space.owner_instance_id and space.owner_instance_id != self._own_instance_id:
            await self._forward(
                space, "propose", {"action": action.value, "params": params}, actor
            )
            return {
                "action": action.value,
                "status": ProposalStatus.PENDING.value,
                "forwarded": True,
            }
        return await self._host_propose(
            space_id,
            action,
            params,
            proposer_instance=self._own_instance_id or "",
            proposer_user=actor.user_id,
        )

    async def vote(
        self,
        space_id: str,
        proposal_id: str,
        *,
        actor_username: str,
        approve: bool,
    ) -> dict | None:
        space = await self._require_space(space_id)
        actor = await self._require_local_admin(space_id, actor_username)
        if approve:
            # §CP.R: nor approve someone else's proposal to publish it. The
            # host mirrors every open proposal here, so a forwarded vote is
            # checked against the same local row. A proposal not mirrored
            # here yet fails closed for a protected account — the host
            # can't tell the voter is protected.
            proposal = await self._proposals.get(proposal_id)
            if proposal is None or _publishes(proposal.action, proposal.params):
                await self._require_unrestricted(
                    actor.user_id, ProtectedCapability.PUBLIC_SPACES
                )
        if space.owner_instance_id and space.owner_instance_id != self._own_instance_id:
            await self._forward(
                space,
                "vote",
                {"proposal_id": proposal_id, "vote": _vote_str(approve)},
                actor,
            )
            return None
        return await self._host_vote(
            proposal_id,
            voter_instance=self._own_instance_id or "",
            voter_user=actor.user_id,
            approve=approve,
        )

    async def list_for_space(self, space_id: str) -> list[dict]:
        """Open proposals for a space (SPA views), lazily expiring stale
        ones. Reads the local table — authoritative on the host, mirror on
        a member household."""
        out: list[dict] = []
        for p in await self._proposals.list_open(space_id):
            if p.expires_at <= _now():
                # Lazy expiry — only the host mutates state; a mirror just
                # hides it (the host's EXPIRED broadcast will follow).
                if (
                    not (space := await self._spaces.get(space_id))
                    or space.owner_instance_id == self._own_instance_id
                ):
                    await self._proposals.set_status(p.id, ProposalStatus.EXPIRED)
                    await self._emit(p, ProposalStatus.EXPIRED)
                continue
            out.append(await self._view(p))
        return out

    async def expire_due(self) -> int:
        """Mark every lapsed pending proposal EXPIRED + emit. Returns the
        count. Intended for a periodic sweep on the host."""
        due = await self._proposals.list_expired(_now())
        for p in due:
            await self._proposals.set_status(p.id, ProposalStatus.EXPIRED)
            await self._emit(p, ProposalStatus.EXPIRED)
        return len(due)

    # ── Inbound (host-side, called by the federation handler) ────────────

    async def apply_remote_propose(
        self,
        space_id: str,
        *,
        proposer_instance: str,
        proposer_user: str,
        action: str,
        params: dict,
    ) -> None:
        """A remote admin proposed a critical action. The §24.11 pipeline
        verified the signer; we re-validate the proposer is a current admin
        before opening the proposal."""
        if not await self._host_owns(space_id):
            return
        if (proposer_instance, proposer_user) not in await self._admin_keys(space_id):
            log.info(
                "apply_remote_propose: %s@%s not an admin of space=%s — dropping",
                proposer_user,
                proposer_instance,
                space_id,
            )
            return
        try:
            act = ProposalAction(action)
        except ValueError:
            log.info("apply_remote_propose: unknown action=%r — dropping", action)
            return
        if act is ProposalAction.REMOTE_ADMIN_ACTION:
            # SECURITY: an owner-approval request is opened ONLY by the host's
            # own forward gate (enqueue_owner_approval), after it validated
            # the action and bound the actor to the signed sender. A proposer
            # building one here would skip that validation and name any
            # actor it likes in the params.
            log.warning(
                "apply_remote_propose: %s@%s tried to open a remote_admin_action "
                "proposal for space=%s directly — dropping",
                proposer_user,
                proposer_instance,
                space_id,
            )
            return
        await self._host_propose(
            space_id,
            act,
            params if isinstance(params, dict) else {},
            proposer_instance=proposer_instance,
            proposer_user=proposer_user,
        )

    async def apply_remote_vote(
        self,
        space_id: str,
        *,
        voter_instance: str,
        voter_user: str,
        proposal_id: str,
        approve: bool,
    ) -> None:
        if not await self._host_owns(space_id):
            return
        await self._host_vote(
            proposal_id,
            voter_instance=voter_instance,
            voter_user=voter_user,
            approve=approve,
        )

    async def apply_mirror_update(self, space_id: str, view: dict) -> None:
        """Member-household side: mirror the host's proposal state so local
        admins can render + vote. Resolved (executed/rejected/expired)
        proposals are dropped from the mirror."""
        pid = str(view.get("id") or "")
        if not pid:
            return
        status = str(view.get("status") or "")
        if status != ProposalStatus.PENDING.value:
            await self._proposals.delete(pid)
        else:
            await self._proposals.upsert(_proposal_from_view(space_id, view))
        await self._bus.publish(
            SpaceProposalUpdated(space_id=space_id, proposal_id=pid, view=view)
        )

    async def enqueue_owner_approval(
        self,
        space_id: str,
        *,
        actor_instance: str,
        actor_user: str,
        fwd_action: str,
        fwd_params: dict | None = None,
    ) -> None:
        """Record a forwarded remote-admin action as a pending OWNER approval
        (delegation OFF). No auto-approve, no dedup — each request is its own
        proposal. The owner approves via the normal vote route; on approval
        :meth:`_execute` runs it as owner."""
        if not await self._host_owns(space_id):
            return
        now = _now()
        fwd_params = fwd_params or {}
        pending = [
            p
            for p in await self._proposals.list_open(space_id)
            if p.action == ProposalAction.REMOTE_ADMIN_ACTION and p.expires_at > now
        ]
        # Cap first — a household at the cap gets nothing in, not even a
        # replacement (so a flood can't churn the owner's list either).
        mine = [p for p in pending if p.proposed_by_instance == actor_instance]
        if len(mine) >= MAX_PENDING_OWNER_APPROVALS_PER_HOUSEHOLD:
            log.warning(
                "enqueue_owner_approval: %s already holds %d pending requests "
                "for space=%s — dropping %r",
                actor_instance,
                MAX_PENDING_OWNER_APPROVALS_PER_HOUSEHOLD,
                space_id,
                fwd_action,
            )
            return
        if fwd_action == "set_member_role":
            # One open request per seat AND household: a newer one replaces
            # that household's own older one for the same (instance_id,
            # user_id), so the owner sees its latest ask. Another
            # household's request is never evicted — each stays for the
            # owner to decide.
            seat = (fwd_params.get("instance_id"), fwd_params.get("user_id"))
            for old in mine:
                op = old.params
                old_fwd = op.get("fwd_params")
                if (
                    op.get("fwd_action") == "set_member_role"
                    and isinstance(old_fwd, dict)
                    and (old_fwd.get("instance_id"), old_fwd.get("user_id")) == seat
                ):
                    await self._proposals.set_status(old.id, ProposalStatus.EXPIRED)
                    await self._emit(old, ProposalStatus.EXPIRED)
        proposal = SpaceAdminProposal(
            id=str(uuid.uuid4()),
            space_id=space_id,
            action=ProposalAction.REMOTE_ADMIN_ACTION,
            params={
                "fwd_action": fwd_action,
                "fwd_params": fwd_params,
            },
            proposed_by_instance=actor_instance,
            proposed_by_user=actor_user,
            status=ProposalStatus.PENDING,
            created_at=now,
            expires_at=(datetime.now(timezone.utc) + PROPOSAL_TTL).isoformat(),
        )
        await self._proposals.upsert(proposal)
        await self._emit(proposal, ProposalStatus.PENDING)

    # ── Host-side core ───────────────────────────────────────────────────

    async def _host_propose(
        self,
        space_id: str,
        action: ProposalAction,
        params: dict,
        *,
        proposer_instance: str,
        proposer_user: str,
    ) -> dict:
        existing = await self._proposals.find_open(space_id, action)
        # §CP.R: a space a protected account owns never goes public — not by
        # its adult admins, not by a remote admin, not by a quorum.
        if (
            _publishes(action, params)
            or (
                existing is not None
                and existing.expires_at > _now()
                and _publishes(action, existing.params)
            )
        ) and await self._owner_is_protected(space_id):
            raise SpacePermissionError(PROTECTED_OWNER_PUBLISH_DETAIL)
        if existing is not None and existing.expires_at > _now():
            proposal = existing
        else:
            if existing is not None:
                await self._proposals.set_status(existing.id, ProposalStatus.EXPIRED)
            now = _now()
            proposal = SpaceAdminProposal(
                id=str(uuid.uuid4()),
                space_id=space_id,
                action=action,
                params=params,
                proposed_by_instance=proposer_instance,
                proposed_by_user=proposer_user,
                status=ProposalStatus.PENDING,
                created_at=now,
                expires_at=(datetime.now(timezone.utc) + PROPOSAL_TTL).isoformat(),
            )
            await self._proposals.upsert(proposal)
        await self._proposals.record_vote(
            SpaceAdminProposalVote(
                proposal_id=proposal.id,
                voter_instance=proposer_instance,
                voter_user=proposer_user,
                vote=ProposalVote.APPROVE,
                voted_at=_now(),
            )
        )
        return await self._evaluate(proposal)

    async def _host_vote(
        self,
        proposal_id: str,
        *,
        voter_instance: str,
        voter_user: str,
        approve: bool,
    ) -> dict | None:
        proposal = await self._proposals.get(proposal_id)
        if proposal is None:
            return None
        if proposal.status != ProposalStatus.PENDING:
            return await self._view(proposal)
        if proposal.expires_at <= _now():
            await self._proposals.set_status(proposal.id, ProposalStatus.EXPIRED)
            return await self._emit(proposal, ProposalStatus.EXPIRED)
        if proposal.action == ProposalAction.REMOTE_ADMIN_ACTION:
            if (voter_instance, voter_user) != await self._owner_key(proposal.space_id):
                log.info(
                    "vote: %s@%s not the owner of owner-only proposal=%s "
                    "(space=%s) — dropping",
                    voter_user,
                    voter_instance,
                    proposal.id,
                    proposal.space_id,
                )
                return await self._view(proposal)
        elif (voter_instance, voter_user) not in await self._admin_keys(
            proposal.space_id
        ):
            log.info(
                "vote: %s@%s not a current admin of space=%s — dropping",
                voter_user,
                voter_instance,
                proposal.space_id,
            )
            return await self._view(proposal)
        await self._proposals.record_vote(
            SpaceAdminProposalVote(
                proposal_id=proposal_id,
                voter_instance=voter_instance,
                voter_user=voter_user,
                vote=ProposalVote.APPROVE if approve else ProposalVote.REJECT,
                voted_at=_now(),
            )
        )
        return await self._evaluate(proposal)

    async def _evaluate(self, proposal: SpaceAdminProposal) -> dict:
        """Recompute the threshold against the current admin set and, if
        met, execute. Returns the SPA view with the resolved status."""
        if proposal.action == ProposalAction.REMOTE_ADMIN_ACTION:
            return await self._evaluate_owner_only(proposal)
        if _publishes(proposal.action, proposal.params) and (
            await self._owner_is_protected(proposal.space_id)
        ):
            # Opened before the owner was protected: it can't pass now.
            await self._proposals.set_status(proposal.id, ProposalStatus.REJECTED)
            return await self._emit(proposal, ProposalStatus.REJECTED)
        admins = await self._admin_keys(proposal.space_id)
        votes = await self._proposals.list_votes(proposal.id)
        relevant = [v for v in votes if (v.voter_instance, v.voter_user) in admins]
        if any(v.vote == ProposalVote.REJECT for v in relevant):
            await self._proposals.set_status(proposal.id, ProposalStatus.REJECTED)
            return await self._emit(proposal, ProposalStatus.REJECTED)
        approvals = sum(1 for v in relevant if v.vote == ProposalVote.APPROVE)
        total = len(admins)
        if total > 0 and approvals * 2 > total:
            await self._proposals.set_status(proposal.id, ProposalStatus.EXECUTED)
            view = await self._emit(proposal, ProposalStatus.EXECUTED)
            await self._execute(proposal)
            return view
        return await self._emit(proposal, ProposalStatus.PENDING)

    async def _evaluate_owner_only(self, proposal: SpaceAdminProposal) -> dict:
        """Owner is the sole authority: an owner REJECT cancels, an owner
        APPROVE executes; any other vote leaves it pending."""
        owner_key = await self._owner_key(proposal.space_id)
        votes = await self._proposals.list_votes(proposal.id)
        owner_votes = [
            v for v in votes if (v.voter_instance, v.voter_user) == owner_key
        ]
        if any(v.vote == ProposalVote.REJECT for v in owner_votes):
            await self._proposals.set_status(proposal.id, ProposalStatus.REJECTED)
            return await self._emit(proposal, ProposalStatus.REJECTED)
        if any(v.vote == ProposalVote.APPROVE for v in owner_votes):
            await self._proposals.set_status(proposal.id, ProposalStatus.EXECUTED)
            view = await self._emit(proposal, ProposalStatus.EXECUTED)
            await self._execute(proposal)
            return view
        return await self._emit(proposal, ProposalStatus.PENDING)

    async def _execute(self, proposal: SpaceAdminProposal) -> None:
        if self._space_service is None:
            log.warning("approval execute: no space_service wired — dropping")
            return
        space = await self._spaces.get(proposal.space_id)
        if space is None:
            return
        owner = space.owner_username
        try:
            if proposal.action == ProposalAction.DISSOLVE:
                await self._space_service.dissolve_space(
                    proposal.space_id, actor_username=owner
                )
            elif proposal.action == ProposalAction.SET_PUBLIC_TIER:
                st = proposal.params.get("space_type")
                if st:
                    await self._space_service.update_config(
                        proposal.space_id, actor_username=owner, space_type=st
                    )
            elif proposal.action == ProposalAction.REMOTE_ADMIN_ACTION:
                p = proposal.params
                await self._space_service.apply_approved_admin_action(
                    proposal.space_id,
                    action=str(p.get("fwd_action") or ""),
                    params=(
                        p.get("fwd_params")
                        if isinstance(p.get("fwd_params"), dict)
                        else {}
                    ),
                    # The signer-bound proposer — set by enqueue_owner_approval
                    # from the verified envelope sender — never the params.
                    actor_instance_id=proposal.proposed_by_instance,
                    actor_user_id=proposal.proposed_by_user,
                )
        except Exception:
            log.exception(
                "approval execute failed for proposal=%s action=%s",
                proposal.id,
                proposal.action,
            )

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _resolve_fwd_target_label(
        self, space_id: str, fwd_action, fwd_params
    ) -> str | None:
        """Human-readable target of a forwarded admin action for the owner's
        approval card. Best-effort: None when nothing resolvable (the card
        falls back to the generic phrase). Host-side only."""
        if not isinstance(fwd_params, dict):
            return None
        if fwd_action == "set_member_role":
            return await self._seat_label(
                space_id,
                str(fwd_params.get("instance_id") or ""),
                str(fwd_params.get("user_id") or ""),
            )
        if fwd_action in ("ban", "unban"):
            uid = str(fwd_params.get("user_id") or "")
            return await self._user_label(space_id, uid) if uid else None
        if fwd_action == "invite":
            # The invitee isn't a member yet; best-effort from a known
            # remote-user record.
            uid = str(fwd_params.get("invitee_user_id") or "")
            if not uid:
                return None
            ru = await self._users.get_remote(uid)
            return ru.display_name if ru and ru.display_name else None
        return None

    async def _seat_label(
        self, space_id: str, instance_id: str, user_id: str
    ) -> str | None:
        """Display name of the exact seat ``(instance_id, user_id)`` — a
        host-local member when ``instance_id`` is ours."""
        if not instance_id or not user_id:
            return None
        if instance_id == self._own_instance_id:
            u = await self._users.get_by_user_id(user_id)
            return u.display_name if u and u.display_name else None
        rm = await self._remote_members.get(space_id, instance_id, user_id)
        if rm is not None and rm.display_name:
            return rm.display_name
        ru = await self._users.get_remote(user_id)
        return ru.display_name if ru and ru.display_name else None

    async def _user_label(self, space_id: str, user_id: str) -> str | None:
        # A current remote member of this space carries a display_name.
        for rm in await self._remote_members.list_for_space(space_id):
            if rm.user_id == user_id and rm.display_name:
                return rm.display_name
        # A local user.
        u = await self._users.get_by_user_id(user_id)
        if u and u.display_name:
            return u.display_name
        # A cached remote user (no longer / not a member of this space).
        ru = await self._users.get_remote(user_id)
        return ru.display_name if ru and ru.display_name else None

    async def _admin_keys(self, space_id: str) -> set[tuple[str, str]]:
        """The (instance_id, user_id) of every current admin (owner counts),
        local + remote — the electorate the majority is computed against."""
        keys: set[tuple[str, str]] = set()
        own = self._own_instance_id or ""
        for m in await self._spaces.list_members(space_id):
            if m.role in SETTINGS_AUTHORITY_ROLES:
                keys.add((own, m.user_id))
        for rm in await self._remote_members.list_for_space(space_id):
            if rm.role == SpaceRole.ADMIN:
                keys.add((rm.instance_id, rm.user_id))
        return keys

    async def _owner_key(self, space_id: str) -> tuple[str, str] | None:
        """(own_instance_id, owner_user_id) for the space, or None.
        The single authority for an owner-only (REMOTE_ADMIN_ACTION) proposal."""
        space = await self._spaces.get(space_id)
        if space is None:
            return None
        owner = await self._users.get(space.owner_username)
        if owner is None:
            return None
        return (self._own_instance_id or "", owner.user_id)

    async def _owner_is_protected(self, space_id: str) -> bool:
        """Whether the space's (local) owner is a protected account."""
        space = await self._spaces.get(space_id)
        if space is None:
            return False
        owner = await self._users.get(space.owner_username)
        return owner is not None and await self._is_protected(owner.user_id)

    async def _require_space(self, space_id: str):
        space = await self._spaces.get(space_id)
        if space is None:
            raise KeyError(f"space {space_id!r} not found")
        return space

    async def _require_local_admin(self, space_id: str, actor_username: str):
        actor = await self._users.get(actor_username)
        if actor is None:
            raise KeyError(f"actor {actor_username!r} not found")
        member = await self._spaces.get_member(space_id, actor.user_id)
        if member is None or member.role not in SETTINGS_AUTHORITY_ROLES:
            raise SpacePermissionError("only an admin can propose or vote")
        return actor

    async def _host_owns(self, space_id: str) -> bool:
        space = await self._spaces.get(space_id)
        return bool(
            space is not None
            and self._own_instance_id is not None
            and space.owner_instance_id == self._own_instance_id
        )

    async def _forward(self, space, verb: str, params: dict, actor) -> None:
        if self._federation is None:
            raise RuntimeError("remote proposal requires federation to be attached")
        if not await self._federation.peer_supports(
            space.owner_instance_id,
            min_version=FederationCapability.MIN_FOR_ADMIN_PROPOSALS,
        ):
            raise SpacePermissionError(
                "this space's host doesn't support admin approvals yet "
                "— ask the host's operator to upgrade",
            )
        await self._federation.send_with_mesh_fallback(
            to_instance_id=space.owner_instance_id,
            event_type=FederationEventType.SPACE_REMOTE_ADMIN_ACTION,
            payload={
                "space_id": space.id,
                "actor_user_id": actor.user_id,
                "actor_instance_id": self._own_instance_id,
                "action": verb,
                "params": params,
            },
            space_id=space.id,
        )

    async def _view(self, proposal: SpaceAdminProposal) -> dict:
        # On a member-household mirror the host's authoritative view (tally
        # included) is stored verbatim — return it rather than recomputing
        # against this household's possibly-stale roster. Only the current
        # status is taken from the local row.
        if proposal.host_view is not None:
            view = {**proposal.host_view, "status": proposal.status.value}
            if isinstance(view.get("params"), dict):
                view["params"] = _public_params(view["params"])
            return view
        if proposal.action == ProposalAction.REMOTE_ADMIN_ACTION:
            owner_key = await self._owner_key(proposal.space_id)
            votes = await self._proposals.list_votes(proposal.id)
            approvals = (
                1
                if any(
                    (v.voter_instance, v.voter_user) == owner_key
                    and v.vote == ProposalVote.APPROVE
                    for v in votes
                )
                else 0
            )
            p = proposal.params
            return {
                "id": proposal.id,
                "space_id": proposal.space_id,
                "action": proposal.action.value,
                "params": _public_params(proposal.params),
                "status": proposal.status.value,
                "proposed_by_instance": proposal.proposed_by_instance,
                "proposed_by_user": proposal.proposed_by_user,
                # The signer-bound proposer's display name for the owner's
                # "Requested by …" line; None → the SPA's generic fallback.
                "proposed_by_label": await self._seat_label(
                    proposal.space_id,
                    proposal.proposed_by_instance,
                    proposal.proposed_by_user,
                ),
                "owner_only": True,
                "fwd_action": p.get("fwd_action"),
                "fwd_params": p.get("fwd_params"),
                "fwd_target_label": await self._resolve_fwd_target_label(
                    proposal.space_id, p.get("fwd_action"), p.get("fwd_params")
                ),
                "approvals": approvals,
                "total_admins": 1,
                "needed": 1,
                "created_at": proposal.created_at,
                "expires_at": proposal.expires_at,
            }
        admins = await self._admin_keys(proposal.space_id)
        votes = await self._proposals.list_votes(proposal.id)
        relevant = [v for v in votes if (v.voter_instance, v.voter_user) in admins]
        approvals = sum(1 for v in relevant if v.vote == ProposalVote.APPROVE)
        total = len(admins)
        return {
            "id": proposal.id,
            "space_id": proposal.space_id,
            "action": proposal.action.value,
            "params": _public_params(proposal.params),
            "status": proposal.status.value,
            "proposed_by_instance": proposal.proposed_by_instance,
            "proposed_by_user": proposal.proposed_by_user,
            "proposed_by_label": await self._seat_label(
                proposal.space_id,
                proposal.proposed_by_instance,
                proposal.proposed_by_user,
            ),
            "approvals": approvals,
            "total_admins": total,
            "needed": (total // 2) + 1,
            "created_at": proposal.created_at,
            "expires_at": proposal.expires_at,
        }

    async def _emit(self, proposal: SpaceAdminProposal, status: ProposalStatus) -> dict:
        """Publish the updated proposal view (status overridden to reflect
        the just-applied transition, since the frozen row may be stale) and
        return that view."""
        view = await self._view(proposal)
        view["status"] = status.value
        await self._bus.publish(
            SpaceProposalUpdated(
                space_id=proposal.space_id, proposal_id=proposal.id, view=view
            )
        )
        # Host mirrors the authoritative view onto member households so a
        # remote admin's SPA shows the same proposal + tally and can vote.
        if self._federation is not None and await self._host_owns(proposal.space_id):
            try:
                await self._federation.broadcast_to_space_members(
                    proposal.space_id,
                    FederationEventType.SPACE_ADMIN_PROPOSAL_UPDATED,
                    {"space_id": proposal.space_id, "view": view},
                )
            except Exception:
                log.exception(
                    "SPACE_ADMIN_PROPOSAL_UPDATED broadcast failed for space=%s",
                    proposal.space_id,
                )
        return view


#: Proposal ``params`` keys never shown in a view: the proposer copied
#: into params by older builds. Nothing trusts them — the signer-bound
#: ``proposed_by_instance`` / ``proposed_by_user`` are authoritative.
_UNTRUSTED_PARAM_KEYS = frozenset({"actor_instance", "actor_user"})


def _public_params(params: dict) -> dict:
    """``params`` without the untrusted proposer copies."""
    return {k: v for k, v in params.items() if k not in _UNTRUSTED_PARAM_KEYS}


def _vote_str(approve: bool) -> str:
    return ProposalVote.APPROVE.value if approve else ProposalVote.REJECT.value


def _proposal_from_view(space_id: str, view: dict) -> SpaceAdminProposal:
    """Reconstruct a mirror row from a broadcast view (member household)."""
    raw_params = view.get("params")
    params: dict = raw_params if isinstance(raw_params, dict) else {}
    return SpaceAdminProposal(
        id=str(view["id"]),
        space_id=space_id,
        action=ProposalAction(view["action"]),
        params=params,
        proposed_by_instance=str(view.get("proposed_by_instance") or ""),
        proposed_by_user=str(view.get("proposed_by_user") or ""),
        status=ProposalStatus(view.get("status") or "pending"),
        created_at=str(view.get("created_at") or _now()),
        expires_at=str(view.get("expires_at") or _now()),
        # Keep the host's authoritative view verbatim so the member shows
        # the host's exact tally, not a local recomputation.
        host_view=view,
    )
