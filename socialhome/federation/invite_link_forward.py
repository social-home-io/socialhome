"""Forwarded invite-link mint (v_52): an admin on a member household mints a
link for a space hosted elsewhere.

An invite link is a row in the **host's** ``space_invite_tokens`` table and
every redeem path consumes it there, so a member household cannot mint one
locally. Instead its admin's request is forwarded to the host, which checks
the actor's live seat, mints the token in its own table (publishing its blob
to the host's own connection server when asked) and hands the link back:

1. **Member household → host:** the existing
   :data:`~socialhome.domain.federation.FederationEventType
   .SPACE_REMOTE_ADMIN_ACTION` with ``action = "create_invite_link"`` and
   ``params = {mint_nonce, role, uses, ttl_seconds, via, publish_gfs_url}``.
   The §24.11 pipeline authenticates the household; the actor is bound to
   the signed envelope's ``from_instance`` (never a payload field), exactly
   as for every other forwarded admin action.
2. **Host:** :meth:`SpaceService.handle_forwarded_invite_mint` re-decides
   everything with its own data and answers with
   :data:`~socialhome.domain.federation.FederationEventType
   .SPACE_INVITE_LINK_FORWARD_RESULT` ``{space_id, mint_nonce, link}`` or
   ``{space_id, mint_nonce, error[, gfs_status]}``.
3. **Member household:** the waiting request resolves; a reply from any
   household other than the one addressed is ignored. No reply within
   :data:`MINT_TIMEOUT_SECONDS` is ``HostUnreachableError`` (503) — the
   host is offline.

Unlike a role change, a mint is never held for owner approval: any admin may
mint a member / subscriber / moderator link on the host, so a forwarded one
needs no more than the same seat. This module is transport only; the
authorization and the error vocabulary live in ``SpaceService``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from ..domain.federation import (
    DELIVERY_ERROR_QUEUED,
    DELIVERY_ERROR_RELAY_THROTTLED,
    DeliveryResult,
    FederationEventType,
)
from ..domain.space import HostUnreachableError

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from .federation_service import FederationService

log = logging.getLogger(__name__)

#: The ``SPACE_REMOTE_ADMIN_ACTION`` action a forwarded mint rides as.
FORWARDED_INVITE_LINK_ACTION: str = "create_invite_link"

#: How long the member household waits for the host's answer. A mint on the
#: host may publish to a connection server first, so this is longer than a
#: redeem's single hop.
MINT_TIMEOUT_SECONDS: float = 20.0

#: Delivery errors that still arrive (a durable outbox / a parked relay):
#: the request is on its way, so keep waiting for the reply.
_STILL_ARRIVING: frozenset[str] = frozenset(
    {DELIVERY_ERROR_QUEUED, DELIVERY_ERROR_RELAY_THROTTLED}
)


class InviteLinkForwardCoordinator:
    """Both halves of a forwarded mint: the member household's waiting
    request and the host's reply."""

    __slots__ = (
        "_federation",
        "_space_service",
        "_timeout",
        "_pending",
        "_pending_host",
    )

    def __init__(
        self,
        *,
        federation_service: "FederationService",
        timeout: float = MINT_TIMEOUT_SECONDS,
    ) -> None:
        self._federation = federation_service
        self._space_service = None
        self._timeout = timeout
        #: ``mint_nonce -> Future`` of a request in flight.
        self._pending: dict[str, asyncio.Future[dict]] = {}
        #: ``mint_nonce -> host instance id`` it was sent to.
        self._pending_host: dict[str, str] = {}

    def attach_space_service(self, space_service) -> None:
        """The host-side decision (``handle_forwarded_invite_mint``)."""
        self._space_service = space_service

    def attach_to(self, federation_service: "FederationService") -> None:
        registry = federation_service._event_registry  # noqa: SLF001
        # Every handler bound to a type runs; the private-invite handler's
        # SPACE_REMOTE_ADMIN_ACTION handler leaves this action to us.
        registry.register(
            FederationEventType.SPACE_REMOTE_ADMIN_ACTION,
            self._on_forwarded_mint,
        )
        registry.register(
            FederationEventType.SPACE_INVITE_LINK_FORWARD_RESULT,
            self._on_result,
        )

    # ── Member household ───────────────────────────────────────────────

    async def request_mint(
        self,
        *,
        space_id: str,
        host_instance_id: str,
        actor_user_id: str,
        own_instance_id: str,
        params: dict,
    ) -> dict:
        """Forward one mint to ``host_instance_id`` and wait for its answer:
        the host's result payload (``link`` or ``error``). Raises
        :class:`HostUnreachableError` when the request went nowhere or no
        answer came in time."""
        nonce = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[nonce] = fut
        self._pending_host[nonce] = host_instance_id
        try:
            result = await self._federation.send_with_mesh_fallback(
                to_instance_id=host_instance_id,
                event_type=FederationEventType.SPACE_REMOTE_ADMIN_ACTION,
                payload={
                    "space_id": space_id,
                    "actor_user_id": actor_user_id,
                    "actor_instance_id": own_instance_id,
                    "action": FORWARDED_INVITE_LINK_ACTION,
                    "params": {**params, "mint_nonce": nonce},
                },
                space_id=space_id,
            )
            if (
                isinstance(result, DeliveryResult)
                and not result.ok
                and result.error not in _STILL_ARRIVING
            ):
                log.warning(
                    "forwarded invite-link mint for space=%s did not reach host %s: %s",
                    space_id,
                    host_instance_id,
                    result.error,
                )
                raise HostUnreachableError(host_instance_id)
            try:
                return await asyncio.wait_for(fut, timeout=self._timeout)
            except TimeoutError:
                log.warning(
                    "forwarded invite-link mint for space=%s: no answer from "
                    "host %s within %.0f s",
                    space_id,
                    host_instance_id,
                    self._timeout,
                )
                raise HostUnreachableError(host_instance_id) from None
        finally:
            self._pending.pop(nonce, None)
            self._pending_host.pop(nonce, None)

    async def _on_result(self, event: "FederationEvent") -> None:
        p = event.payload if isinstance(event.payload, dict) else {}
        nonce = str(p.get("mint_nonce") or "")
        fut = self._pending.get(nonce)
        if fut is None or fut.done():
            return
        expected = self._pending_host.get(nonce, "")
        if event.from_instance != expected:
            log.warning(
                "forwarded invite-link mint: %s answered a request addressed to "
                "%s — ignoring",
                event.from_instance,
                expected,
            )
            return
        fut.set_result(dict(p))

    # ── Host ───────────────────────────────────────────────────────────

    async def _on_forwarded_mint(self, event: "FederationEvent") -> None:
        p = event.payload if isinstance(event.payload, dict) else {}
        if p.get("action") != FORWARDED_INVITE_LINK_ACTION:
            return
        raw = p.get("params")
        params = raw if isinstance(raw, dict) else {}
        nonce = str(params.get("mint_nonce") or "")
        space_id = str(p.get("space_id") or "") or (event.space_id or "")
        actor_user_id = str(p.get("actor_user_id") or "")
        if not nonce or not space_id or not actor_user_id:
            log.debug(
                "forwarded invite-link mint from %s missing fields — dropping",
                event.from_instance,
            )
            return
        if self._space_service is None:
            log.warning("forwarded invite-link mint: no space service — dropping")
            return
        # SECURITY: the actor's household is the SIGNED sender, never the
        # payload's ``actor_instance_id`` — a household acts only for its
        # own users.
        answer = await self._space_service.handle_forwarded_invite_mint(
            space_id,
            actor_instance_id=event.from_instance,
            actor_user_id=actor_user_id,
            params=params,
        )
        result = await self._federation.send_with_mesh_fallback(
            to_instance_id=event.from_instance,
            event_type=FederationEventType.SPACE_INVITE_LINK_FORWARD_RESULT,
            payload={"space_id": space_id, "mint_nonce": nonce, **answer},
            space_id=space_id,
        )
        if (
            isinstance(result, DeliveryResult)
            and not result.ok
            and result.error not in _STILL_ARRIVING
        ):
            # The link exists here but its minter never learns it: it is
            # listed on the host and expires like any other.
            log.warning(
                "forwarded invite-link mint for space=%s: the answer to %s was "
                "not delivered (%s)",
                space_id,
                event.from_instance,
                result.error,
            )
