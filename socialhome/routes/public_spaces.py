"""Public-space discovery routes — /api/public_spaces.

Returns the cached directory of public spaces (populated by the
:class:`PublicSpaceDiscoveryService` background poll).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from aiohttp import web

from .. import app_keys as K
from ..domain.federation import InstanceSource, PairingStatus
from ..domain.space import normalize_category, normalize_join_mode
from ..federation.join_contact_pin import usable_contact
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
        protection = await cp_svc._repo.get_user_protection(
            ctx.user_id,
        )
        if protection and protection.get("child_protection_enabled"):
            max_min_age = int(protection.get("declared_age") or 0)

        discovery = self.svc(K.public_space_discovery_key)
        listings = await discovery._repo.list_visible_for_user(
            ctx.user_id,
            limit=limit,
            max_min_age=max_min_age,
        )
        # v_84 — the pinned join contact (its own table, outlives the row),
        # read for the listings that take requests or followers.
        contact = {
            lst.space_id: usable_contact(await discovery._repo.get_pin(lst.space_id))
            is not None
            for lst in listings
            if lst.join_mode == "request" or lst.allow_subscribers
        }
        federation_repo = self.svc(K.federation_repo_key)
        unique_instances = {lst.instance_id for lst in listings}
        # The host's name is only ever the one a pairing told us: a GFS
        # directory carries none (the GFS shields households from each
        # other), so an unknown or unnamed host is ``None`` — never its
        # instance id standing in for a name.
        host_info: dict[str, tuple[str | None, bool]] = {}
        for iid in unique_instances:
            inst = await federation_repo.get_instance(iid)
            if inst is None or inst.source is InstanceSource.SPACE_SESSION:
                # A space-session seat (a space of this host joined by link
                # or through the GFS) is no pairing, and its name is a
                # stand-in, not one a pairing told us.
                host_info[iid] = (None, False)
            else:
                host_info[iid] = (
                    inst.display_name or None,
                    inst.status is PairingStatus.CONFIRMED,
                )
        return web.json_response(
            [
                {
                    "space_id": lst.space_id,
                    "instance_id": lst.instance_id,
                    "host_instance_id": lst.instance_id,
                    "host_display_name": host_info[lst.instance_id][0],
                    "host_is_paired": host_info[lst.instance_id][1],
                    "name": lst.name,
                    "description": lst.description,
                    "emoji": lst.emoji,
                    "lat": lst.lat,
                    "lon": lst.lon,
                    "radius_km": lst.radius_km,
                    # The host's declared counts (signed in its GFS
                    # listing); null = it declared none — no number.
                    "member_count": lst.member_count,
                    "follower_count": lst.follower_count,
                    "min_age": lst.min_age,
                    "category": normalize_category(lst.category),
                    # The host's real join mode — how someone becomes a
                    # posting member. Before this field existed the client
                    # fabricated ``'request'`` for every global listing.
                    "join_mode": normalize_join_mode(lst.join_mode),
                    # The host's readability opt-in. False means the listing
                    # is discoverable but its content is NOT publicly readable
                    # — the SPA must not offer Subscribe for it.
                    "allow_subscribers": bool(lst.allow_subscribers),
                    # v_84 — the listing carries a verified, pinned join
                    # contact for its host: an "Ask to join" can go through
                    # the GFS without a pairing.
                    "gfs_askable": lst.join_mode == "request"
                    and contact.get(lst.space_id, False),
                    # v_96 — a follow (a join for the read-only role) can go
                    # through the GFS the same way: followers on and a
                    # verified, pinned contact, whatever the join mode.
                    "gfs_followable": bool(lst.allow_subscribers)
                    and contact.get(lst.space_id, False),
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
        except ValueError:
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
            # Only when the person agreed in the SPA: a local community's
            # host needs the household's home rounded to ~1 km.
            share_location=body.get("share_location") is True,
        )
        return web.json_response(
            {"request_id": request_id},
            status=202,
        )


class PublicSpaceJoinPreflightView(BaseView):
    """``GET /api/public_spaces/{space_id}/join-preflight`` — v_97. Before
    joining (or following) a space: a GFS space (global, or private with
    ``private_gfs``) carries its content only on its channel at the GFSs it
    is listed on, so this household must hold a ``channel_ring`` connection
    to one of them first. Answers ``{gfs_space, connected, may_connect}``
    (``may_connect``: the asker may connect the household — an admin;
    anyone else asks an admin). The server decides what the space is from
    what it knows (:meth:`SpaceService.gfs_join_readiness`) — a
    ``space_type`` query parameter from an older SPA is ignored. The join
    itself is refused the same way (``409 GFS_CONNECTION_REQUIRED``). Read
    only: nothing is sent anywhere."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)
        ready = await self.svc(K.space_service_key).gfs_join_readiness(
            self.match("space_id")
        )
        default_gfs: str | None = None
        if ctx.is_admin and ready.gfs_space and not ready.connected:
            default_gfs = await self._default_gfs()
        return web.json_response(
            {
                "gfs_space": ready.gfs_space,
                "connected": ready.connected,
                "may_connect": bool(ctx.is_admin),
                # What "Connect and join" connects: the household's default
                # GFS, by its host — ``null`` when none is configured or the
                # household is already connected to it (then the admin picks
                # one that lists the space under Connections).
                "default_gfs": default_gfs,
            }
        )

    async def _default_gfs(self) -> str | None:
        url = await _unconnected_default_gfs(self.request.app)
        return (urlsplit(url).hostname or None) if url else None


async def _unconnected_default_gfs(app: web.Application) -> str:
    """The household's default GFS address while it is not connected to
    it, else ``""``."""
    url = str(app[K.config_key].gfs_default_url or "").rstrip("/")
    if not url:
        return ""
    gfs = app.get(K.gfs_connection_service_key)
    if gfs is not None:
        for conn in await gfs.list_connections():
            if conn.inbox_url.rstrip("/") == url:
                return ""
    return url


class PublicSpaceAskAdminView(BaseView):
    """``POST /api/public_spaces/{space_id}/ask-admin`` — owner 2026-11-28,
    "Ask an admin of your household": joining (or following,
    ``{"action": "follow"}``) a GFS space needs a connection the household
    lacks and only an admin can connect it. Each household admin gets one
    "Needs you" row naming who asked, the space and the GFS to connect (the
    household's default, when it is not connected yet), opening the Connect
    flow. ``202 {"asked": n}``; ``409 NOTHING_TO_ASK`` when the space needs
    no new connection. Local only: nothing leaves the household."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)
        body = await self.body()
        url = await _unconnected_default_gfs(self.request.app)
        asked = await self.svc(K.space_service_key).ask_admins_to_connect(
            self.match("space_id"),
            user_id=ctx.user_id,
            action="follow" if body.get("action") == "follow" else "join",
            gfs=(urlsplit(url).hostname or "") if url else "",
            gfs_url=url,
        )
        return self._json({"asked": asked}, status=202)


class PublicSpaceHideView(BaseView):
    """``POST /api/public_spaces/{space_id}/hide`` — hide a space for the user."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)
        space_id = self.match("space_id")
        discovery = self.svc(K.public_space_discovery_key)
        await discovery._repo.hide_for_user(ctx.user_id, space_id)
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
        except ValueError:
            data = {}
        discovery = self.svc(K.public_space_discovery_key)
        await discovery._repo.block_instance(
            instance_id,
            blocked_by=ctx.user_id,
            reason=data.get("reason"),
        )
        return web.Response(status=204)
