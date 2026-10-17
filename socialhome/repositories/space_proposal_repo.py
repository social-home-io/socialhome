"""Repository for multi-admin approval proposals + votes.

Backs :class:`socialhome.services.space_approval_service.SpaceApprovalService`.
The host stores the authoritative rows; member households keep a mirror
(written from ``SPACE_ADMIN_PROPOSAL_UPDATED``) so their admins can render
+ vote. Row-shaped dataclasses live in
:mod:`socialhome.domain.space_proposal`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import orjson

from ..db.database import AsyncDatabase
from ..domain.space_proposal import (
    ProposalAction,
    ProposalStatus,
    ProposalVote,
    SpaceAdminProposal,
    SpaceAdminProposalVote,
)
from .base import rows_to_dicts


@runtime_checkable
class AbstractSpaceProposalRepo(Protocol):
    async def upsert(self, proposal: SpaceAdminProposal) -> None: ...

    async def get(self, proposal_id: str) -> SpaceAdminProposal | None: ...

    async def find_open(
        self,
        space_id: str,
        action: ProposalAction,
    ) -> SpaceAdminProposal | None: ...

    async def list_open(self, space_id: str) -> list[SpaceAdminProposal]: ...

    async def list_expired(self, now_iso: str) -> list[SpaceAdminProposal]: ...

    async def set_status(self, proposal_id: str, status: ProposalStatus) -> None: ...

    async def record_vote(self, vote: SpaceAdminProposalVote) -> None: ...

    async def list_votes(self, proposal_id: str) -> list[SpaceAdminProposalVote]: ...

    async def delete(self, proposal_id: str) -> None: ...


class SqliteSpaceProposalRepo:
    """SQLite-backed :class:`AbstractSpaceProposalRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def upsert(self, proposal: SpaceAdminProposal) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_admin_proposals(
                id, space_id, action, params_json,
                proposed_by_instance, proposed_by_user,
                status, created_at, expires_at, host_view_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                action=excluded.action,
                params_json=excluded.params_json,
                proposed_by_instance=excluded.proposed_by_instance,
                proposed_by_user=excluded.proposed_by_user,
                status=excluded.status,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at,
                host_view_json=excluded.host_view_json
            """,
            (
                proposal.id,
                proposal.space_id,
                proposal.action.value,
                orjson.dumps(proposal.params).decode(),
                proposal.proposed_by_instance,
                proposal.proposed_by_user,
                proposal.status.value,
                proposal.created_at,
                proposal.expires_at,
                orjson.dumps(proposal.host_view).decode()
                if proposal.host_view is not None
                else None,
            ),
        )

    async def get(self, proposal_id: str) -> SpaceAdminProposal | None:
        rows = await self._db.fetchall(
            "SELECT * FROM space_admin_proposals WHERE id=? LIMIT 1",
            (proposal_id,),
        )
        dicts = rows_to_dicts(rows)
        return _proposal(dicts[0]) if dicts else None

    async def find_open(
        self,
        space_id: str,
        action: ProposalAction,
    ) -> SpaceAdminProposal | None:
        rows = await self._db.fetchall(
            "SELECT * FROM space_admin_proposals"
            " WHERE space_id=? AND action=? AND status='pending' LIMIT 1",
            (space_id, action.value),
        )
        dicts = rows_to_dicts(rows)
        return _proposal(dicts[0]) if dicts else None

    async def list_open(self, space_id: str) -> list[SpaceAdminProposal]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_admin_proposals"
            " WHERE space_id=? AND status='pending' ORDER BY created_at",
            (space_id,),
        )
        return [_proposal(r) for r in rows_to_dicts(rows)]

    async def list_expired(self, now_iso: str) -> list[SpaceAdminProposal]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_admin_proposals"
            " WHERE status='pending' AND expires_at <= ?",
            (now_iso,),
        )
        return [_proposal(r) for r in rows_to_dicts(rows)]

    async def set_status(self, proposal_id: str, status: ProposalStatus) -> None:
        await self._db.enqueue(
            "UPDATE space_admin_proposals SET status=? WHERE id=?",
            (status.value, proposal_id),
        )

    async def record_vote(self, vote: SpaceAdminProposalVote) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_admin_proposal_votes(
                proposal_id, voter_instance, voter_user, vote, voted_at
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(proposal_id, voter_instance, voter_user) DO UPDATE SET
                vote=excluded.vote,
                voted_at=excluded.voted_at
            """,
            (
                vote.proposal_id,
                vote.voter_instance,
                vote.voter_user,
                vote.vote.value,
                vote.voted_at,
            ),
        )

    async def list_votes(self, proposal_id: str) -> list[SpaceAdminProposalVote]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_admin_proposal_votes WHERE proposal_id=?",
            (proposal_id,),
        )
        return [_vote(r) for r in rows_to_dicts(rows)]

    async def delete(self, proposal_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_admin_proposals WHERE id=?",
            (proposal_id,),
        )


def _proposal(row: dict) -> SpaceAdminProposal:
    raw = row.get("params_json") or "{}"
    try:
        params = orjson.loads(raw)
    except orjson.JSONDecodeError:
        params = {}
    host_view: dict | None = None
    hv_raw = row.get("host_view_json")
    if hv_raw:
        try:
            parsed = orjson.loads(hv_raw)
            host_view = parsed if isinstance(parsed, dict) else None
        except orjson.JSONDecodeError:
            host_view = None
    return SpaceAdminProposal(
        id=row["id"],
        space_id=row["space_id"],
        action=ProposalAction(row["action"]),
        params=params if isinstance(params, dict) else {},
        proposed_by_instance=row["proposed_by_instance"],
        proposed_by_user=row["proposed_by_user"],
        status=ProposalStatus(row["status"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        host_view=host_view,
    )


def _vote(row: dict) -> SpaceAdminProposalVote:
    return SpaceAdminProposalVote(
        proposal_id=row["proposal_id"],
        voter_instance=row["voter_instance"],
        voter_user=row["voter_user"],
        vote=ProposalVote(row["vote"]),
        voted_at=row["voted_at"],
    )
