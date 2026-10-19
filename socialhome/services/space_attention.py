"""The "needs you" read — what in a space waits for the viewer's decision, for the
``attention`` field of ``GET /api/spaces/{id}`` (the space home's Needs-you
card and the filled badge on More).

Only someone with content authority (owner / admin / moderator) has
anything waiting; everyone else — a plain member, a follower — gets
``None``. Within that:

* items held for review and open reports: content authority;
* join requests and the open multi-admin proposals: settings authority
  (owner / admin) — a moderator gets ``0`` / ``[]``.

Read at request time from the same service reads the Moderation section
uses (no counter columns, no migration), so the numbers match what the
section then lists. The counts are a hint: a read its service refuses
(the seat changed between the two checks) counts as nothing, logged at
WARNING, rather than failing the whole space detail.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

from ..domain.space import (
    CONTENT_AUTHORITY_ROLES,
    SETTINGS_AUTHORITY_ROLES,
    SpaceMember,
    SpacePermissionError,
)

if TYPE_CHECKING:
    from .report_service import ReportService
    from .space_approval_service import SpaceApprovalService
    from .space_moderation_service import SpaceModerationService
    from .space_service import SpaceService

log = logging.getLogger(__name__)


async def _soft(read: Awaitable[list[Any]], what: str, space_id: str) -> list[Any]:
    """``await read``, or ``[]`` when its service refuses the viewer."""
    try:
        return await read
    except SpacePermissionError as exc:
        log.warning("space attention: %s refused for space=%s: %s", what, space_id, exc)
        return []


async def space_attention(
    space_id: str,
    *,
    seat: SpaceMember,
    username: str,
    space_service: SpaceService,
    approvals: SpaceApprovalService,
    reports: ReportService,
    moderation: SpaceModerationService | None,
) -> dict | None:
    """The viewer's "needs you" counts, or ``None`` when nothing in this
    space can wait for them (see the module docstring)."""
    if seat.role not in CONTENT_AUTHORITY_ROLES:
        return None
    pending = (
        await _soft(
            moderation.list_items(space_id, actor_user_id=seat.user_id),
            "moderation queue",
            space_id,
        )
        if moderation is not None
        else []
    )
    open_reports = await _soft(
        reports.review_space(space_id, actor_user_id=seat.user_id),
        "reports",
        space_id,
    )
    join_requests = 0
    proposals: list[dict] = []
    if seat.role in SETTINGS_AUTHORITY_ROLES:
        join_requests = len(
            await _soft(
                space_service.list_pending_join_requests(
                    space_id, actor_username=username
                ),
                "join requests",
                space_id,
            )
        )
        # The whole proposal view (the SPA words the row from it) plus
        # ``required`` — the approvals it needs — and the per-reader
        # ``voted_by_me``, so nobody is asked to vote twice.
        proposals = [
            {**p, "required": p.get("needed", 0)}
            for p in await _soft(
                approvals.list_for_space(space_id, viewer_user_id=seat.user_id),
                "proposals",
                space_id,
            )
        ]
    return {
        "pending_review": len(pending),
        "reports": len(open_reports),
        "join_requests": join_requests,
        "proposals": proposals,
    }
