"""Author-side fan-out for public Momentum (§Momentum-public).

Bus subscriber on :class:`MomentCreated` and :class:`MomentDeleted`.
For each event whose author is local on this instance AND has at
least one :class:`MomentPublicRegistration`, build a signed envelope
and POST it to every registered GFS so the broker can fan it out to
each follower's instance.

Runs alongside :class:`MomentFederationOutbound`: the original
moment also still goes to paired households via the existing relay,
so a follower that is *both* household-paired and a GFS-follower
receives the same moment twice. Receivers dedupe by ``moment.id``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import aiohttp

from ..crypto import b64url_encode, sign_ed25519
from ..domain.events import MomentCreated, MomentDeleted
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.moment_public_repo import (
        AbstractMomentPublicRegistrationRepo,
    )
    from ..repositories.moment_repo import AbstractMomentRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


class MomentPublicOutbound:
    __slots__ = (
        "_bus",
        "_moments",
        "_regs",
        "_users",
        "_gfs",
        "_http_client",
        "_signing_key",
        "_own_instance_id",
        "_own_users",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        moment_repo: "AbstractMomentRepo",
        registration_repo: "AbstractMomentPublicRegistrationRepo",
        user_repo: "AbstractUserRepo",
        gfs_repo: "AbstractGfsConnectionRepo",
        http_client: aiohttp.ClientSession | None = None,
    ) -> None:
        self._bus = bus
        self._moments = moment_repo
        self._regs = registration_repo
        self._users = user_repo
        self._gfs = gfs_repo
        self._http_client = http_client
        self._signing_key: bytes | None = None
        self._own_instance_id: str = ""
        # Cached set of locally-hosted user_ids; refreshed on every
        # subscribe + after each successful event so we don't fan out
        # for events whose author lives on another instance.
        self._own_users: set[str] | None = None

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        if self._http_client is None:
            self._http_client = session

    def attach_identity(self, *, own_instance_id: str, signing_key: bytes) -> None:
        self._own_instance_id = own_instance_id
        self._signing_key = signing_key

    def wire(self) -> None:
        self._bus.subscribe(MomentCreated, self._on_created)
        self._bus.subscribe(MomentDeleted, self._on_deleted)

    # ── Subscribers ─────────────────────────────────────────────────────

    async def _on_created(self, event: MomentCreated) -> None:
        if not await self._is_local(event.author_user_id):
            return
        moment = await self._moments.get(event.moment_id)
        if moment is None or not moment.is_public:
            return
        # GFS-received moments are NEVER re-fanned (no-redistribute rule).
        if moment.received_via == "gfs":
            return
        regs = await self._regs.list_for_user(event.author_user_id)
        if not regs:
            return
        author = await self._users.get_by_user_id(event.author_user_id)
        if author is None:
            return
        envelope = {
            "moment_id": moment.id,
            "author_user_id": moment.author_user_id,
            "author_username": author.username,
            "author_display_name": author.display_name,
            "content": moment.content,
            "media_url": moment.media_url,
            "media_type": moment.media_type,
            "duration_ms": moment.duration_ms,
            "parent_moment_id": moment.parent_moment_id,
            "origin_instance_id": moment.origin_instance_id,
            # ``instance_id`` is the field the GFS-side
            # ``_rtc_authenticate`` middleware reads to look up the
            # sender's pubkey. Carries the same value as
            # ``origin_instance_id`` for outbound author posts.
            "instance_id": self._own_instance_id,
            "created_at": moment.created_at,
            "expires_at": moment.expires_at,
        }
        signed = self._sign_envelope(envelope)
        for reg in regs:
            await self._post_to_gfs(reg.gfs_id, signed, kind="publish")

    async def _on_deleted(self, event: MomentDeleted) -> None:
        if not await self._is_local(event.author_user_id):
            return
        regs = await self._regs.list_for_user(event.author_user_id)
        if not regs:
            return
        envelope = {
            "moment_id": event.moment_id,
            "author_user_id": event.author_user_id,
            "instance_id": self._own_instance_id,
        }
        signed = self._sign_envelope(envelope)
        for reg in regs:
            await self._post_to_gfs(reg.gfs_id, signed, kind="delete")

    # ── Helpers ─────────────────────────────────────────────────────────

    async def _is_local(self, user_id: str) -> bool:
        if self._own_users is None:
            users = await self._users.list_all()
            self._own_users = {u.user_id for u in users}
        return user_id in self._own_users

    async def _post_to_gfs(self, gfs_id: str, signed: dict, *, kind: str) -> None:
        if self._http_client is None or self._signing_key is None:
            return
        conn = await self._gfs.get(gfs_id)
        if conn is None or conn.status != "active":
            return
        path = "/gfs/moments/publish" if kind == "publish" else "/gfs/moments/delete"
        try:
            async with self._http_client.post(
                f"{conn.inbox_url}{path}",
                allow_redirects=False,
                json=signed,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300:
                    log.warning(
                        "moment_public.outbound: GFS %s rejected %s — HTTP %d",
                        gfs_id,
                        kind,
                        resp.status,
                    )
        except aiohttp.ClientError as exc:
            log.warning(
                "moment_public.outbound: GFS %s %s failed: %s",
                gfs_id,
                kind,
                exc,
            )

    def _sign_envelope(self, envelope: dict) -> dict:
        if self._signing_key is None:
            raise RuntimeError("MomentPublicOutbound used before attach_identity")
        canonical = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        signed = dict(envelope)
        signed["signature"] = b64url_encode(sign_ed25519(self._signing_key, canonical))
        return signed
