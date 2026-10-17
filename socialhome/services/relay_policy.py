"""Federation relay-policy gate (§Momentum-relay-policy).

Determines whether a hop-bearing event is allowed to land or relay.
Two negative signals stop the flow:

* The source instance is on the household-level
  :class:`HouseholdInstanceBan` list (set by the operator from the
  Settings → Federation page).
* There is at least one ``status='pending'`` row in
  ``content_reports`` against the author OR the specific moment id.
  The local moderation queue overrides federation fan-out — neither
  the local persist nor the relay-out runs while the report is open.

Per-user :table:`user_blocks` are intentionally NOT consulted here.
Personal block lists stay private to the social layer; tying them
to relay would couple two trust models and leak block-list shape
to every paired peer.

Hooked into both inbound (federation_inbound_service) and outbound
(moment_federation_outbound) so the rule is enforced at every
egress / ingress point.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.report import ReportTargetType

if TYPE_CHECKING:
    from ..repositories.instance_ban_repo import (
        AbstractHouseholdInstanceBanRepo,
    )
    from ..repositories.report_repo import AbstractReportRepo

log = logging.getLogger(__name__)


class RelayPolicy:
    """Allow / deny verdict for a federation envelope's hop traversal."""

    __slots__ = ("_bans", "_reports")

    def __init__(
        self,
        *,
        ban_repo: AbstractHouseholdInstanceBanRepo,
        report_repo: AbstractReportRepo,
    ) -> None:
        self._bans = ban_repo
        self._reports = report_repo

    async def allow_relay(
        self,
        *,
        source_instance_id: str,
        author_user_id: str | None = None,
        target_id: str | None = None,
    ) -> bool:
        """Return True iff the policy allows the envelope to land /
        relay onward.

        ``source_instance_id`` is the immediate sender (or the
        envelope's ``origin_instance_id`` for relay decisions —
        either is fine as a ban-list key, since the operator banned
        the whole instance).

        ``author_user_id`` + ``target_id`` (typically the moment id)
        are checked against the local ``content_reports`` queue.
        Either match short-circuits.
        """
        if source_instance_id and await self._bans.is_banned(source_instance_id):
            log.info(
                "relay_policy: banned instance %s — dropping envelope",
                source_instance_id,
            )
            return False
        if target_id and await self._reports.has_open_for_target(
            target_type=ReportTargetType.MOMENT, target_id=target_id
        ):
            log.info(
                "relay_policy: open report on moment %s — dropping envelope",
                target_id,
            )
            return False
        if author_user_id and await self._reports.has_open_for_target(
            target_type=ReportTargetType.USER, target_id=author_user_id
        ):
            log.info(
                "relay_policy: open report on user %s — dropping envelope",
                author_user_id,
            )
            return False
        return True
