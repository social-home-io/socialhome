"""Trusted-mode member publish routes (v_49).

* ``POST /gfs/member-publish`` — a member household publishes its own space
  item, identified by its household signature and authorized by a writer cert.
* ``POST /gfs/spaces/{space_id}/epoch`` — a seed holder's authority-signed
  content-epoch notice.

The logic lives in :mod:`socialhome.global_server.member_publish`; these views
only parse, call and map refusals to ONE uniform ``403`` body each (the reason
goes to DEBUG), as ``PublishView`` does.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ...domain.gfs_member_publish import InvalidMemberPublish, MemberPublishRequest
from .. import app_keys as K
from ..member_publish import MemberPublishRateLimited
from ..public import PUBLISH_MAX_BODY_BYTES
from .base import GfsBaseView

log = logging.getLogger(__name__)

_REFUSED_PUBLISH = {"error": "not authorized to publish to this space"}
_REFUSED_NOTICE = {"error": "not authorized for this space"}


class MemberPublishView(GfsBaseView):
    """``POST /gfs/member-publish`` — see :mod:`..member_publish`.

    ``200 {"status": "published"}`` once accepted (a suppressed replay is the
    same answer); ``400`` for a malformed body; ``403`` (uniform) for any
    authorization failure; ``429`` past the per-household limit."""

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_member_publish_key)
        body = await self.bounded_json(PUBLISH_MAX_BODY_BYTES)
        try:
            req = MemberPublishRequest.from_wire(body)
        except InvalidMemberPublish as exc:
            raise web.HTTPBadRequest(reason=str(exc)) from exc
        try:
            await svc.publish(req)
        except MemberPublishRateLimited:
            resp = web.json_response({"error": "rate_limited"}, status=429)
            resp.headers["Retry-After"] = "60"
            return resp
        except PermissionError as exc:
            log.debug("GFS member publish refused for space %s: %s", req.target, exc)
            return web.json_response(_REFUSED_PUBLISH, status=403)
        return web.json_response({"status": "published"})


class SpaceEpochNoticeView(GfsBaseView):
    """``POST /gfs/spaces/{space_id}/epoch`` — body ``{epoch, authority_sig,
    authority_sig_suite}``, the signature over ``{space_id, epoch}`` under
    ``space_epoch_notice``. Anonymous like ``/gfs/publish``: authorized by
    the space-authority signature alone, so it names no household."""

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_member_publish_key)
        space_id = self.match("space_id")
        body = await self.body_or_400()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(reason="expected a JSON object")
        try:
            epoch = body["epoch"]
            authority_sig = body["authority_sig"]
            authority_sig_suite = body["authority_sig_suite"]
        except KeyError as exc:
            raise web.HTTPBadRequest(reason=f"Missing field: {exc}") from exc
        try:
            await svc.note_epoch(
                space_id, epoch, str(authority_sig), str(authority_sig_suite)
            )
        except PermissionError as exc:
            log.debug("GFS epoch notice refused for space %s: %s", space_id, exc)
            return web.json_response(_REFUSED_NOTICE, status=403)
        return web.json_response({"status": "ok"})
