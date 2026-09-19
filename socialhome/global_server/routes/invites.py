"""Invite-link mint / revoke routes (``/gfs/spaces/{id}/invite``).

The rationale, the constants and the mint/revoke logic live in
:mod:`socialhome.global_server.invites`; this module is the thin aiohttp
surface over them. The public ``GET /join/{gfs_token}`` half is a server-
rendered page and lives with the other public pages in
:mod:`socialhome.global_server.public`.
"""

from __future__ import annotations

import logging

from aiohttp import web

from .. import app_keys as K
from ..invites import InvalidInvite, InviteRateLimited
from .base import GfsBaseView

log = logging.getLogger(__name__)


class SpaceInviteView(GfsBaseView):
    """``POST /gfs/spaces/{space_id}/invite`` — the OWNING household parks an
    opaque invite blob on this server's bulletin board.

    Body: ``{owning_instance, blob, expires_at, ts, signature}``. The Ed25519
    signature covers the canonical ``{action: "mint_invite", owning_instance,
    space_id, ts}`` JSON and is verified against the registered
    ``ClientInstance.public_key`` with the usual ±300 s replay guard; the
    ``action`` is inside the signed bytes, so no other signed request this
    household ever made can be replayed as a mint. Authentication is not
    authorisation: the caller must additionally BE the space's
    ``owning_instance``, and the space must be actively listed.

    ``blob`` is OPAQUE — checked for size and base64url alphabet only, never
    parsed. ``expires_at`` is an absolute unix-seconds expiry, required and
    bounded by ``INVITE_MAX_TTL_SECONDS``.

    ``201 {gfs_token, url}``. A missing body field is a ``400``, as is a bad
    blob / expiry; anything the service refuses on identity or ownership
    grounds is a ``403``; over the per-household mint rate is a ``429``.
    """

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_invite_service_key)
        cfg = self.svc(K.gfs_config_key)
        space_id = self.match("space_id")
        body = await self.body_or_400()
        try:
            owning_instance = body["owning_instance"]
            blob = body["blob"]
            expires_at = body["expires_at"]
            ts = body["ts"]
            signature = body["signature"]
        except KeyError as exc:
            raise web.HTTPBadRequest(reason=f"Missing field: {exc}") from exc
        try:
            row = await svc.mint(
                space_id=space_id,
                owning_instance=str(owning_instance),
                blob=blob,
                expires_at=expires_at,
                ts=str(ts),
                signature=str(signature),
            )
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except InvalidInvite as exc:
            raise web.HTTPBadRequest(reason=str(exc)) from exc
        except InviteRateLimited as exc:
            resp = web.json_response({"error": str(exc)}, status=429)
            resp.headers["Retry-After"] = "60"
            return resp
        return web.json_response(
            {
                "gfs_token": row.gfs_token,
                "url": f"{cfg.base_url}/join/{row.gfs_token}",
            },
            status=201,
        )


class SpaceInviteTokenView(GfsBaseView):
    """``DELETE /gfs/spaces/{space_id}/invite/{gfs_token}`` — the OWNING
    household takes one invite link down.

    Body: ``{owning_instance, ts, signature}`` — the signature covers the
    canonical ``{action: "revoke_invite", gfs_token, owning_instance,
    space_id, ts}`` JSON, so both the action and the exact token are inside
    the signed bytes: a mint signature can't be replayed as a revoke, and a
    revoke for one token can't be redirected at another.

    ``204`` on success and idempotent — an unknown or already-revoked token
    still answers ``204``, but only after the signature verifies and the
    caller is confirmed as the owner, so the endpoint is never an existence
    oracle for invite tokens. ``POST`` is accepted alongside ``DELETE``
    because some proxies strip a DELETE request body, which would turn the
    signed revoke into a permanent ``400``.
    """

    async def delete(self) -> web.Response:
        return await self._handle()

    async def post(self) -> web.Response:
        return await self._handle()

    async def _handle(self) -> web.Response:
        svc = self.svc(K.gfs_invite_service_key)
        space_id = self.match("space_id")
        gfs_token = self.match("gfs_token")
        body = await self.body_or_400()
        try:
            owning_instance = body["owning_instance"]
            ts = body["ts"]
            signature = body["signature"]
        except KeyError as exc:
            raise web.HTTPBadRequest(reason=f"Missing field: {exc}") from exc
        try:
            await svc.revoke(
                space_id=space_id,
                gfs_token=gfs_token,
                owning_instance=str(owning_instance),
                ts=str(ts),
                signature=str(signature),
            )
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        return web.Response(status=204)
