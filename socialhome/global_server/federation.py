"""GFS federation service — instance registration, event relay, subscriptions.

Business logic only — all SQL lives in :mod:`.repositories`. Crypto
helpers are reused from :mod:`socialhome.crypto` (no duplication).

Fan-out delivery uses the same WebRTC-primary + webhook-fallback pattern
as the main federation transport: if a DataChannel is open to a
subscriber, the event is sent over it; otherwise it falls back to an
HTTPS POST to the subscriber's webhook URL.
"""

from __future__ import annotations

import json
import logging

import aiohttp

from ..crypto import b64url_decode, verify_ed25519
from ..domain.federation import InstanceSource, PairingStatus, RemoteInstance
from ..federation.transport import FederationTransport
from .domain import ClientInstance, GlobalSpace, GfsSubscriber
from .repositories import AbstractGfsFederationRepo

log = logging.getLogger(__name__)


class GfsFederationService:
    """Lightweight federation relay for the GFS process.

    Responsible for:
    * Registering/updating client household instances.
    * Verifying Ed25519 signatures on inbound publish requests.
    * Fanning out events to all subscribers via HTTP POST.
    * Managing space subscription lists.
    * Listing all known global spaces.
    """

    __slots__ = ("_repo", "_transport")

    def __init__(
        self,
        repo: AbstractGfsFederationRepo,
        transport: FederationTransport | None = None,
    ) -> None:
        self._repo = repo
        self._transport = transport

    async def register_instance(
        self,
        instance_id: str,
        public_key: str,
        webhook_url: str,
        *,
        display_name: str = "",
        auto_accept: bool = False,
    ) -> None:
        """Register or update a client household instance."""
        await self._repo.upsert_instance(
            ClientInstance(
                instance_id=instance_id,
                display_name=display_name,
                public_key=public_key,
                endpoint_url=webhook_url,
                status="active" if auto_accept else "pending",
                auto_accept=auto_accept,
            )
        )
        log.debug("GFS: registered instance %s webhook=%s", instance_id, webhook_url)

    async def publish_event(
        self,
        space_id: str,
        event_type: str,
        payload: object,
        from_instance: str,
        signature: str = "",
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> list[str]:
        """Relay an event to all subscribers of *space_id*.

        Validates the Ed25519 *signature* using the public key registered
        for *from_instance*. Returns the list of instance_ids successfully
        notified.

        Raises :class:`PermissionError` when *from_instance* is unknown or
        the signature is invalid.
        """
        inst = await self._repo.get_instance(from_instance)
        if inst is None:
            raise PermissionError(f"Unknown instance: {from_instance}")

        if signature:
            canonical = json.dumps(
                {
                    "space_id": space_id,
                    "event_type": event_type,
                    "payload": payload,
                    "from_instance": from_instance,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            raw_key = bytes.fromhex(inst.public_key)
            raw_sig = b64url_decode(signature)
            if not verify_ed25519(raw_key, canonical, raw_sig):
                raise PermissionError("Invalid Ed25519 signature")

        # Preserve existing row data if present; otherwise create a minimal
        # pending row. Admin portal fleshes the metadata out on accept.
        existing = await self._repo.get_space(space_id)
        if existing is None:
            await self._repo.upsert_space(
                GlobalSpace(
                    space_id=space_id,
                    owning_instance=from_instance,
                )
            )

        subscribers = await self._repo.list_subscribers(
            space_id,
            exclude=from_instance,
        )

        event_body = {
            "space_id": space_id,
            "event_type": event_type,
            "payload": payload,
            "from_instance": from_instance,
        }

        return await self._fan_out(subscribers, event_body, session)

    async def subscribe(self, instance_id: str, space_id: str) -> None:
        """Add *instance_id* as a subscriber of *space_id*."""
        existing = await self._repo.get_space(space_id)
        if existing is None:
            # Subscription precedes publish — create a pending row so the
            # admin can see the demand.
            await self._repo.upsert_space(
                GlobalSpace(
                    space_id=space_id,
                    owning_instance=instance_id,
                )
            )
        await self._repo.add_subscriber(
            space_id=space_id,
            instance_id=instance_id,
        )
        log.debug("GFS: %s subscribed to space %s", instance_id, space_id)

    async def unsubscribe(self, instance_id: str, space_id: str) -> None:
        """Remove *instance_id* from subscribers of *space_id*."""
        await self._repo.remove_subscriber(
            space_id=space_id,
            instance_id=instance_id,
        )
        log.debug("GFS: %s unsubscribed from space %s", instance_id, space_id)

    async def list_spaces(
        self,
        *,
        status: str | None = None,
    ) -> list[GlobalSpace]:
        """Return global/public spaces known to this GFS node.

        The public ``GET /gfs/spaces`` endpoint passes ``status='active'``
        to hide pending + banned rows. Internal callers (admin, tests)
        can pass ``status=None`` to see everything.
        """
        return await self._repo.list_spaces(status=status)

    # ── Fan-out ──────────────────────────────────────────────────────────

    async def _fan_out(
        self,
        subscribers: list[GfsSubscriber],
        event_body: dict,
        session: aiohttp.ClientSession | None,
    ) -> list[str]:
        """Deliver *event_body* to each subscriber.

        Tries the WebRTC DataChannel first (if a transport is attached
        and the channel to that subscriber is open); falls back to an
        HTTPS POST to the subscriber's webhook URL.
        """
        own_session = session is None
        active: aiohttp.ClientSession = (
            session if session is not None else aiohttp.ClientSession()
        )
        try:
            delivered: list[str] = []
            for sub in subscribers:
                # Try DataChannel first.
                if self._transport is not None and self._transport.is_ready(
                    sub.instance_id
                ):
                    try:
                        # Build a minimal RemoteInstance for the transport.
                        inst = RemoteInstance(
                            id=sub.instance_id,
                            display_name=sub.instance_id[:8],
                            remote_identity_pk="",
                            key_self_to_remote="",
                            key_remote_to_self="",
                            remote_webhook_url=sub.endpoint_url,
                            local_webhook_id="",
                            status=PairingStatus.CONFIRMED,
                            source=InstanceSource.MANUAL,
                        )
                        result = await self._transport.send(
                            instance=inst,
                            envelope_dict=event_body,
                        )
                        if result.ok:
                            delivered.append(sub.instance_id)
                            continue
                    except Exception as exc:
                        log.debug(
                            "GFS RTC fan-out failed for %s, falling back to webhook: %s",
                            sub.instance_id,
                            exc,
                        )

                # Webhook fallback.
                try:
                    async with active.post(
                        sub.endpoint_url,
                        json=event_body,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status < 400:
                            delivered.append(sub.instance_id)
                        else:
                            log.warning(
                                "GFS fan-out: %s returned HTTP %s",
                                sub.endpoint_url,
                                resp.status,
                            )
                except Exception as exc:
                    log.warning(
                        "GFS fan-out: failed to deliver to %s: %s",
                        sub.endpoint_url,
                        exc,
                    )
            return delivered
        finally:
            if own_session:
                await active.close()
