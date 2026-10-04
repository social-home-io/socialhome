"""Forwarded invite links (v_52): an admin on a member household mints,
lists and revokes the links of a space hosted elsewhere.

An invite link is a row in the **host's** ``space_invite_tokens`` table and
every redeem path consumes it there, so a member household holds none of a
remote space's links. Its admin's request is forwarded to the host instead,
which checks the actor's live seat, acts on its own table (a mint publishes
to the host's own connection server when asked; a revoke takes a parked blob
down there) and answers:

1. **Member household → host:** the existing
   :data:`~socialhome.domain.federation.FederationEventType
   .SPACE_REMOTE_ADMIN_ACTION` with ``action`` one of
   :data:`FORWARDED_INVITE_LINK_ACTIONS` and ``params`` carrying
   ``request_nonce`` plus the action's own fields — ``create_invite_link``:
   ``{role, uses, ttl_seconds, via, publish_gfs_url}``;
   ``list_invite_links``: nothing; ``revoke_invite_link``: ``{token}``. The
   §24.11 pipeline authenticates the household; the actor is bound to the
   signed envelope's ``from_instance`` (never a payload field), exactly as
   for every other forwarded admin action.
2. **Host:** :meth:`SpaceService.handle_forwarded_invite_action` re-decides
   everything with its own data and answers with
   :data:`~socialhome.domain.federation.FederationEventType
   .SPACE_INVITE_LINK_FORWARD_RESULT` ``{space_id, request_nonce}`` plus
   ``link`` / ``links`` / ``revoked``, or ``error[, gfs_status]``.
3. **Member household:** the waiting request resolves; a reply from any
   household other than the one addressed is ignored. No reply within
   :data:`REQUEST_TIMEOUT_SECONDS` is ``HostUnreachableError`` (503) — the
   host is offline.

Unlike a role change, none of these is held for owner approval: any admin
may mint, list and revoke links on the host, so a forwarded request needs no
more than the same seat. This module is transport only; the authorization
and the error vocabulary live in ``SpaceService``.
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

#: The ``SPACE_REMOTE_ADMIN_ACTION`` actions of this module.
FORWARDED_INVITE_LINK_ACTION: str = "create_invite_link"
LIST_INVITE_LINKS_ACTION: str = "list_invite_links"
REVOKE_INVITE_LINK_ACTION: str = "revoke_invite_link"
FORWARDED_INVITE_LINK_ACTIONS: frozenset[str] = frozenset(
    {
        FORWARDED_INVITE_LINK_ACTION,
        LIST_INVITE_LINKS_ACTION,
        REVOKE_INVITE_LINK_ACTION,
    }
)

#: How long the member household waits for the host's answer. A mint or a
#: revoke on the host may talk to a connection server first, so this is
#: longer than a redeem's single hop.
REQUEST_TIMEOUT_SECONDS: float = 20.0

#: Delivery errors that still arrive (a durable outbox / a parked relay):
#: the request is on its way, so keep waiting for the reply.
_STILL_ARRIVING: frozenset[str] = frozenset(
    {DELIVERY_ERROR_QUEUED, DELIVERY_ERROR_RELAY_THROTTLED}
)


class InviteLinkForwardCoordinator:
    """Both halves of a forwarded invite-link request: the member
    household's waiting request and the host's reply."""

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
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._federation = federation_service
        self._space_service = None
        self._timeout = timeout
        #: ``request_nonce -> Future`` of a request in flight.
        self._pending: dict[str, asyncio.Future[dict]] = {}
        #: ``request_nonce -> host instance id`` it was sent to.
        self._pending_host: dict[str, str] = {}

    def attach_space_service(self, space_service) -> None:
        """The host-side decision (``handle_forwarded_invite_action``)."""
        self._space_service = space_service

    def attach_to(self, federation_service: "FederationService") -> None:
        registry = federation_service._event_registry  # noqa: SLF001
        # Every handler bound to a type runs; the private-invite handler's
        # SPACE_REMOTE_ADMIN_ACTION handler leaves these actions to us.
        registry.register(
            FederationEventType.SPACE_REMOTE_ADMIN_ACTION,
            self._on_forwarded_request,
        )
        registry.register(
            FederationEventType.SPACE_INVITE_LINK_FORWARD_RESULT,
            self._on_result,
        )

    # ── Member household ───────────────────────────────────────────────

    async def request(
        self,
        action: str,
        *,
        space_id: str,
        host_instance_id: str,
        actor_user_id: str,
        own_instance_id: str,
        params: dict,
    ) -> dict:
        """Forward one ``action`` to ``host_instance_id`` and wait for its
        answer: the host's result payload. Raises :class:`HostUnreachableError`
        when the request went nowhere or no answer came in time, and
        ``ValueError`` for an action this module does not forward."""
        if action not in FORWARDED_INVITE_LINK_ACTIONS:
            raise ValueError(f"not a forwarded invite-link action: {action!r}")
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
                    "action": action,
                    "params": {**params, "request_nonce": nonce},
                },
                space_id=space_id,
            )
            if (
                isinstance(result, DeliveryResult)
                and not result.ok
                and result.error not in _STILL_ARRIVING
            ):
                log.warning(
                    "forwarded %s for space=%s did not reach host %s: %s",
                    action,
                    space_id,
                    host_instance_id,
                    result.error,
                )
                raise HostUnreachableError(host_instance_id)
            try:
                return await asyncio.wait_for(fut, timeout=self._timeout)
            except TimeoutError:
                log.warning(
                    "forwarded %s for space=%s: no answer from host %s within %.0f s",
                    action,
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
        nonce = str(p.get("request_nonce") or "")
        fut = self._pending.get(nonce)
        if fut is None or fut.done():
            return
        expected = self._pending_host.get(nonce, "")
        if event.from_instance != expected:
            log.warning(
                "forwarded invite-link request: %s answered one addressed to "
                "%s — ignoring",
                event.from_instance,
                expected,
            )
            return
        fut.set_result(dict(p))

    # ── Host ───────────────────────────────────────────────────────────

    async def _on_forwarded_request(self, event: "FederationEvent") -> None:
        p = event.payload if isinstance(event.payload, dict) else {}
        action = p.get("action")
        if action not in FORWARDED_INVITE_LINK_ACTIONS:
            return
        raw = p.get("params")
        params = {
            k: v
            for k, v in (raw if isinstance(raw, dict) else {}).items()
            if k != "request_nonce"
        }
        nonce = str((raw if isinstance(raw, dict) else {}).get("request_nonce") or "")
        space_id = str(p.get("space_id") or "") or (event.space_id or "")
        actor_user_id = str(p.get("actor_user_id") or "")
        if not nonce or not space_id or not actor_user_id:
            log.debug(
                "forwarded %s from %s missing fields — dropping",
                action,
                event.from_instance,
            )
            return
        if self._space_service is None:
            log.warning("forwarded %s: no space service — dropping", action)
            return
        # SECURITY: the actor's household is the SIGNED sender, never the
        # payload's ``actor_instance_id`` — a household acts only for its
        # own users.
        answer = await self._space_service.handle_forwarded_invite_action(
            space_id,
            action=str(action),
            actor_instance_id=event.from_instance,
            actor_user_id=actor_user_id,
            params=params,
        )
        result = await self._federation.send_with_mesh_fallback(
            to_instance_id=event.from_instance,
            event_type=FederationEventType.SPACE_INVITE_LINK_FORWARD_RESULT,
            payload={"space_id": space_id, "request_nonce": nonce, **answer},
            space_id=space_id,
        )
        if (
            isinstance(result, DeliveryResult)
            and not result.ok
            and result.error not in _STILL_ARRIVING
        ):
            # The change (if any) stands here; the asker just never learns
            # it. A minted link is listed on the host and expires as usual.
            log.warning(
                "forwarded %s for space=%s: the answer to %s was not delivered (%s)",
                action,
                space_id,
                event.from_instance,
                result.error,
            )
