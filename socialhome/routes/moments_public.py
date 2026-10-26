"""SH-side routes for public-Momentum management (§Momentum-public).

Auth-gated REST surface that lets a user manage their own GFS
registrations + follows. The heavy lifting lives in
:class:`MomentPublicService`; this file only translates HTTP to
method calls.

Routes:

* ``GET    /api/moments/public/registrations`` — list GFSes the
  caller is currently registered on, each with ``delivery``:
  ``"encrypted"`` (public moments go through it, encrypted) or
  ``"unsupported_server"`` (the server has not proved v_62
  ``encrypted_moments``, so none are sent through it), and
  ``followers_unable`` — follower households there on an older Social
  Home, sent no public moments (``null`` when unknown).
* ``POST   /api/moments/public/registrations`` — body
  ``{gfs_id, default_share?}`` (absent: off for a new registration, kept
  for an existing one); signs + posts to the GFS.
* ``DELETE /api/moments/public/registrations/{gfs_id}`` — deregister.
* ``PATCH  /api/moments/public/registrations/{gfs_id}`` — body
  ``{default_share}``; flip the per-user default.
* ``GET    /api/moments/public/follows`` — caller's GFS follows.
* ``POST   /api/moments/public/follows`` — body
  ``{gfs_id, followed_user_id}``; signs + posts to the GFS.
* ``DELETE /api/moments/public/follows/{gfs_id}/{user_id}``.
* ``GET    /api/gfs/{gfs_id}/moments/users`` — proxy the GFS directory.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import media_signer_key, moment_public_service_key
from ..csp import media_response_headers
from ..domain.moment_public import gfs_picture_proxy_url
from ..media_signer import sign_media_urls_in
from ..services.moment_public_service import MomentPublicError
from .base import BaseView


async def _delivery(svc, gfs_id: str) -> str:
    """How public moments reach followers through *gfs_id* (v_62)."""
    if await svc.encrypted_delivery(gfs_id):
        return "encrypted"
    return "unsupported_server"


async def _registration(svc, r) -> dict:
    delivery = await _delivery(svc, r.gfs_id)
    return {
        "user_id": r.user_id,
        "gfs_id": r.gfs_id,
        "registered_at": r.registered_at,
        "default_share": r.default_share,
        "delivery": delivery,
        # Follower households on an older Social Home: sent no public
        # moments (they can't open them). ``None`` when unknown.
        "followers_unable": (
            await svc.follower_households_unable(user_id=r.user_id, gfs_id=r.gfs_id)
            if delivery == "encrypted"
            else None
        ),
    }


class MomentPublicRegistrationCollectionView(BaseView):
    async def get(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        regs = await svc.list_registrations(self.user.user_id)
        return self._json([await _registration(svc, r) for r in regs])

    async def post(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        body = await self.body()
        gfs_id = str(body.get("gfs_id") or "")
        if not gfs_id:
            raise web.HTTPBadRequest(text='{"error":"gfs_id_required"}')
        # Absent → the service decides: off for a new registration, kept
        # for an existing one (owner decision 4).
        raw_share = body.get("default_share")
        default_share = None if raw_share is None else bool(raw_share)
        try:
            reg = await svc.register(
                user_id=self.user.user_id,
                gfs_id=gfs_id,
                default_share=default_share,
            )
        except MomentPublicError as exc:
            raise web.HTTPBadGateway(text=f'{{"error":"{exc!s}"}}') from exc
        return self._json(await _registration(svc, reg), status=201)


class MomentPublicRegistrationDetailView(BaseView):
    async def delete(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        await svc.deregister(user_id=self.user.user_id, gfs_id=self.match("gfs_id"))
        return web.Response(status=204)

    async def patch(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        body = await self.body()
        if "default_share" not in body:
            raise web.HTTPBadRequest(text='{"error":"default_share_required"}')
        await svc.set_default_share(
            user_id=self.user.user_id,
            gfs_id=self.match("gfs_id"),
            default_share=bool(body["default_share"]),
        )
        return web.Response(status=204)


class MomentPublicFollowCollectionView(BaseView):
    async def get(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        follows = await svc.list_follows(self.user.user_id)
        return self._json(
            [
                {
                    "follower_user_id": f.follower_user_id,
                    "followed_user_id": f.followed_user_id,
                    "gfs_id": f.gfs_id,
                    "followed_username": f.followed_username,
                    "followed_display_name": f.followed_display_name,
                    "created_at": f.created_at,
                }
                for f in follows
            ]
        )

    async def post(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        body = await self.body()
        gfs_id = str(body.get("gfs_id") or "")
        followed_user_id = str(body.get("followed_user_id") or "")
        if not gfs_id or not followed_user_id:
            raise web.HTTPBadRequest(
                text='{"error":"gfs_id_and_followed_user_id_required"}'
            )
        try:
            follow = await svc.follow(
                follower_user_id=self.user.user_id,
                gfs_id=gfs_id,
                followed_user_id=followed_user_id,
            )
        except MomentPublicError as exc:
            raise web.HTTPBadGateway(text=f'{{"error":"{exc!s}"}}') from exc
        return self._json(
            {
                "follower_user_id": follow.follower_user_id,
                "followed_user_id": follow.followed_user_id,
                "gfs_id": follow.gfs_id,
                "followed_username": follow.followed_username,
                "followed_display_name": follow.followed_display_name,
                "created_at": follow.created_at,
            },
            status=201,
        )


class MomentPublicFollowDetailView(BaseView):
    async def delete(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        await svc.unfollow(
            follower_user_id=self.user.user_id,
            gfs_id=self.match("gfs_id"),
            followed_user_id=self.match("user_id"),
        )
        return web.Response(status=204)


def mirrored_picture_url(gfs_id: str, row: dict) -> str | None:
    """Our own proxy URL for a directory row's GFS-mirrored picture.

    The row's ``picture_url`` is whatever string the household
    registered — a third-party tracker, or a relative path that would
    resolve against *our* origin — so it is never handed to the
    browser. Only a GFS-mirrored picture (``picture_digest``) gets a
    URL, and it points at :class:`GfsUserPictureProxyView`.
    """
    user_id = row.get("user_id")
    digest = row.get("picture_digest")
    if not (isinstance(user_id, str) and isinstance(digest, str)):
        return None
    return gfs_picture_proxy_url(gfs_id, user_id, digest)


class GfsUserDirectoryProxyView(BaseView):
    async def get(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        gfs_id = self.match("gfs_id")
        q = self.request.query.get("q") or None
        try:
            users = await svc.fetch_directory(gfs_id, q=q)
        except MomentPublicError as exc:
            raise web.HTTPBadGateway(text=f'{{"error":"{exc!s}"}}') from exc
        for row in users:
            if isinstance(row, dict):
                row["picture_url"] = mirrored_picture_url(gfs_id, row)
        payload = {"users": users}
        # ``<img src>`` carries no bearer — sign so the avatar loads.
        signer = self.request.app.get(media_signer_key)
        if signer is not None:
            sign_media_urls_in(payload, signer)
        return self._json(payload)


class GfsUserPictureProxyView(BaseView):
    """Proxy GFS-mirrored avatar bytes to the local SH UI.

    The Discover tab can't fetch directly from the GFS host (cross-
    origin + may not be addressable). The SH proxies the bytes from
    its persistent connection and applies the GFS's strong-cache
    headers.
    """

    async def get(self) -> web.Response:
        svc = self.svc(moment_public_service_key)
        try:
            got = await svc.fetch_picture(self.match("gfs_id"), self.match("user_id"))
        except MomentPublicError as exc:
            raise web.HTTPBadGateway(text=f'{{"error":"{exc!s}"}}') from exc
        if got is None:
            return web.Response(status=404)
        raw, mime, digest = got
        # ``mime`` is the GFS's ``Content-Type`` header — untrusted; a
        # script-capable type must never render on our origin.
        return web.Response(
            body=raw,
            headers={
                **media_response_headers(mime, "picture"),
                "Cache-Control": "public, max-age=86400, immutable",
                "ETag": f'"{digest}"',
            },
        )
