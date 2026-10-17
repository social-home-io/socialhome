"""User move-out federation service (move-out, Task 5).

Lands an inbound move-out redirect and serves the pull backstop:

* :data:`FederationEventType.USER_MOVED` — the *old* home pushes a
  dual-consent :class:`~socialhome.domain.move_link.MoveLink` to every peer
  that supports the feature (proto v_27). The receiver re-verifies both
  signatures and both P-bindings against keys it already pins/stores, then
  records a monotonic forwarding pointer on the moved user's
  ``remote_users`` row so :meth:`AbstractUserRepo.resolve_current_identity`
  follows the redirect.
* :data:`FederationEventType.USER_IDENTITY_RESOLVE` — a pull backstop: a peer
  that missed the push asks the old home (or anyone holding the link) for the
  stored move-link by ``old_user_id``; the holder replies with the same event
  type carrying the link.

Every inbound path is **fail-soft**: a malformed payload, an unknown peer, a
missing stored ``P`` binding, a failed signature/binding check, or a stale
(replayed) link is logged and dropped — the handler never raises out (the
event dispatch registry would only log-and-swallow anyway, but degrading
explicitly keeps the intent local). Replay is defended by the monotonic
``issued_at`` guard in :meth:`AbstractUserRepo.record_user_move`, not by
freshness — a move is a durable fact, so ``verify_move_link`` runs with
``max_age=None``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ..crypto import (
    MoveLinkError,
    UnsupportedMoveLinkSuite,
    verify_move_link,
)
from ..domain.federation import FederationEvent, FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.move_errors import StaleMoveLink
from ..domain.move_link import MoveLink

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


class UserMoveService:
    """Inbound USER_MOVED / USER_IDENTITY_RESOLVE handler + outbound push."""

    __slots__ = ("_federation_service", "_user_repo")

    def __init__(self, *, user_repo: AbstractUserRepo) -> None:
        self._user_repo = user_repo
        self._federation_service: FederationService | None = None

    def attach_to(self, federation_service: FederationService) -> None:
        """Stash the federation service and register both event handlers."""
        self._federation_service = federation_service
        registry = federation_service._event_registry
        registry.register(FederationEventType.USER_MOVED, self._on_user_moved)
        registry.register(
            FederationEventType.USER_IDENTITY_RESOLVE, self._on_resolve_request
        )

    # ─── Inbound: USER_MOVED ────────────────────────────────────────────────

    async def _on_user_moved(self, event: FederationEvent) -> None:
        """Verify and record an inbound move-out redirect (fail-soft)."""
        fed = self._federation_service
        if fed is None:  # pragma: no cover — attach_to always runs first
            return
        raw = event.payload.get("move_link")
        if not isinstance(raw, dict):
            return
        try:
            link = MoveLink.from_wire_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("USER_MOVED: malformed move_link dropped: %s", exc)
            return

        old_home_pk = await fed.peer_identity_public_key(link.old_instance_id)
        if old_home_pk is None:
            log.warning(
                "USER_MOVED: no pinned key for old home %s — dropping",
                link.old_instance_id,
            )
            return

        stored_p = await self._user_repo.get_remote_user_identity_pubkey(
            link.old_user_id
        )
        if stored_p is None:
            log.warning(
                "USER_MOVED: no stored identity key for %s — cannot verify "
                "binding, dropping",
                link.old_user_id,
            )
            return

        try:
            verify_move_link(
                link,
                old_home_pinned_pk=old_home_pk,
                stored_old_user_pubkey=stored_p,
            )
        except (MoveLinkError, UnsupportedMoveLinkSuite, ValueError) as exc:
            log.warning(
                "USER_MOVED: move-link for %s failed verification: %s",
                link.old_user_id,
                exc,
            )
            return

        try:
            await self._user_repo.record_user_move(
                old_user_id=link.old_user_id,
                new_user_id=link.new_user_id,
                new_instance_id=link.new_instance_id,
                issued_at=link.issued_at,
                move_link_json=json.dumps(link.to_wire_dict()),
            )
        except StaleMoveLink as exc:
            log.info(
                "USER_MOVED: ignoring stale move-link for %s: %s", link.old_user_id, exc
            )
            return

    # ─── Inbound: USER_IDENTITY_RESOLVE (pull backstop) ─────────────────────

    async def handle_resolve_request(self, payload: dict) -> dict | None:
        """Return ``{"move_link": <wire dict>}`` for a moved user, else None."""
        old_user_id = payload.get("old_user_id")
        if not old_user_id:
            return None
        link_json = await self._user_repo.get_move_link(old_user_id)
        if link_json is None:
            return None
        return {"move_link": json.loads(link_json)}

    async def _on_resolve_request(self, event: FederationEvent) -> None:
        """Reply to a resolve request with the stored link (if we hold one).

        Gated to CONFIRMED peers only: the §24.11 pipeline authenticates the
        *sender*, but a merely-authenticated (or mid-pairing) peer must not be
        able to enumerate our move destinations. A request from a peer we
        haven't confirmed is logged and dropped without a reply. The
        confirmed-peer check is fail-soft — any error is treated as
        not-confirmed so we never raise out of the handler.
        """
        fed = self._federation_service
        if fed is None:  # pragma: no cover — attach_to always runs first
            return
        try:
            confirmed = await fed.is_confirmed_peer(event.from_instance)
        except Exception:  # pragma: no cover — is_confirmed_peer is fail-soft
            log.debug(
                "UserMoveService._on_resolve_request: fail-soft error ignored",
                exc_info=True,
            )
            confirmed = False
        if not confirmed:
            log.warning(
                "move-resolve: dropping request from non-confirmed peer %s",
                event.from_instance,
            )
            return
        resp = await self.handle_resolve_request(event.payload)
        if resp is None:
            return
        await fed.send_event(
            to_instance_id=event.from_instance,
            event_type=FederationEventType.USER_IDENTITY_RESOLVE,
            payload=resp,
        )

    # ─── Outbound: push the move-link to feature-capable peers ──────────────

    async def push_move_link(
        self, link: MoveLink, *, peer_instance_ids: list[str]
    ) -> list[str]:
        """Push ``USER_MOVED`` to every v_27+ peer; return the peers sent to.

        A peer that doesn't advertise :data:`FederationCapability.MIN_FOR_USER_MOVE`
        is skipped (best-effort — the pull backstop covers it later).
        """
        fed = self._federation_service
        if fed is None:  # pragma: no cover — attach_to always runs first
            return []
        wire = link.to_wire_dict()
        sent_to: list[str] = []
        for peer in peer_instance_ids:
            if not await fed.peer_supports(
                peer, min_version=FederationCapability.MIN_FOR_USER_MOVE
            ):
                continue
            await fed.send_event(
                to_instance_id=peer,
                event_type=FederationEventType.USER_MOVED,
                payload={"move_link": wire},
            )
            sent_to.append(peer)
        return sent_to
