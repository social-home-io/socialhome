"""Public-space discovery routes — /api/public_spaces.

Returns the cached directory of public spaces (populated by the
:class:`PublicSpaceDiscoveryService` background poll).
"""

from __future__ import annotations

from aiohttp import web

from .. import app_keys as K
from ..domain.federation import PairingStatus
from .base import BaseView


class PublicSpaceCollectionView(BaseView):
    """``GET /api/public_spaces`` — list visible public spaces."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)
        try:
            limit = int(self.request.query.get("limit", 50))
        except ValueError:
            limit = 50
        limit = max(1, min(limit, 200))

        # §CP.F1 discovery filter — hide age-gated listings from protected
        # minors whose declared_age is below the space's min_age. Enforcement
        # on join lives in ChildProtectionService.check_space_age_gate; this
        # just keeps the listings out of their browser.
        cp_svc = self.svc(K.child_protection_service_key)
        max_min_age: int | None = None
        protection = await cp_svc._repo.get_user_protection(  # noqa: SLF001
            ctx.user_id,
        )
        if protection and protection.get("child_protection_enabled"):
            max_min_age = int(protection.get("declared_age") or 0)

        discovery = self.svc(K.public_space_discovery_key)
        listings = await discovery._repo.list_visible_for_user(  # noqa: SLF001
            ctx.user_id,
            limit=limit,
            max_min_age=max_min_age,
        )
        federation_repo = self.svc(K.federation_repo_key)
        unique_instances = {lst.instance_id for lst in listings}
        host_info: dict[str, tuple[str, bool]] = {}
        for iid in unique_instances:
            inst = await federation_repo.get_instance(iid)
            if inst is None:
                host_info[iid] = (iid, False)
            else:
                host_info[iid] = (
                    inst.display_name or iid,
                    inst.status is PairingStatus.CONFIRMED,
                )
        return web.json_response(
            [
                {
                    "space_id": lst.space_id,
                    "instance_id": lst.instance_id,
                    "host_instance_id": lst.instance_id,
                    "host_display_name": host_info.get(
                        lst.instance_id, (lst.instance_id, False)
                    )[0],
                    "host_is_paired": host_info.get(
                        lst.instance_id, (lst.instance_id, False)
                    )[1],
                    "name": lst.name,
                    "description": lst.description,
                    "emoji": lst.emoji,
                    "lat": lst.lat,
                    "lon": lst.lon,
                    "radius_km": lst.radius_km,
                    "member_count": lst.member_count,
                    "min_age": lst.min_age,
                    "target_audience": lst.target_audience,
                }
                for lst in listings
            ]
        )


class PublicSpaceJoinRequestView(BaseView):
    """``POST /api/public_spaces/{space_id}/join-request`` — §D2.

    Entry point for "I want to join this global-directory space".
    Body: ``{host_instance_id, message?}``. Target household must be a
    CONFIRMED peer of ours — if not, returns 412 and the client falls
    back to the "pair first" flow.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)
        space_id = self.match("space_id")
        try:
            body = await self.request.json()
        except Exception:
            body = {}
        host_instance_id = body.get("host_instance_id")
        if not host_instance_id:
            return web.json_response(
                {"error": "host_instance_id is required"},
                status=422,
            )
        svc = self.svc(K.space_service_key)
        request_id = await svc.request_join_remote(
            space_id,
            applicant_user_id=ctx.user_id,
            host_instance_id=str(host_instance_id),
            message=body.get("message"),
        )
        return web.json_response(
            {"request_id": request_id},
            status=202,
        )


class PublicSpaceHideView(BaseView):
    """``POST /api/public_spaces/{space_id}/hide`` — hide a space for the user."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)
        space_id = self.match("space_id")
        discovery = self.svc(K.public_space_discovery_key)
        await discovery._repo.hide_for_user(ctx.user_id, space_id)  # noqa: SLF001
        return web.Response(status=204)


class PublicSpacesRefreshView(BaseView):
    """``POST /api/public_spaces/refresh`` — admin-only, force a
    re-poll of every paired GFS directory instead of waiting for the
    next scheduled tick (§D1).
    """

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)
        if not ctx.is_admin:
            return web.json_response({"error": "admin_only"}, status=403)
        discovery = self.svc(K.public_space_discovery_key)
        await discovery.refresh_now()
        return web.Response(status=202)


class PublicSpaceBlockInstanceView(BaseView):
    """``POST /api/public_spaces/blocked_instances/{instance_id}`` — block an instance (admin)."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)
        if not ctx.is_admin:
            return web.json_response({"error": "admin_only"}, status=403)
        instance_id = self.match("instance_id")
        try:
            data = await self.request.json()
        except Exception:
            data = {}
        discovery = self.svc(K.public_space_discovery_key)
        await discovery._repo.block_instance(  # noqa: SLF001
            instance_id,
            blocked_by=ctx.user_id,
            reason=data.get("reason"),
        )
        return web.Response(status=204)
