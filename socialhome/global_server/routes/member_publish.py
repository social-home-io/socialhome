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
from ..member_publish import MemberPublishBusy, MemberPublishRateLimited
from ..public import PUBLISH_MAX_BODY_BYTES
from .base import GfsBaseView

log = logging.getLogger(__name__)

_REFUSED_PUBLISH = {"error": "not authorized to publish to this space"}
_REFUSED_NOTICE = {"error": "not authorized for this space"}


class MemberPublishView(GfsBaseView):
    """``POST /gfs/member-publish`` — see :mod:`..member_publish`.

    ``200 {"status": "published"}`` once accepted (a suppressed replay is the
    same answer); ``400`` for a malformed body; ``403`` (uniform) for any
    authorization failure; ``429`` past a per-household or per-space limit;
    ``503`` while the background fan-out is stopped or its backlog is full."""

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
        except MemberPublishBusy:
            resp = web.json_response({"error": "busy"}, status=503)
            resp.headers["Retry-After"] = "5"
            return resp
        except PermissionError as exc:
            log.debug("GFS member publish refused for space %s: %s", req.target, exc)
            return web.json_response(_REFUSED_PUBLISH, status=403)
        return web.json_response({"status": "published"})


class SpaceEpochNoticeView(GfsBaseView):
    """``POST /gfs/spaces/{space_id}/epoch`` — a content-epoch notice, in one
    of two forms:

    * **owner** ``{owning_instance, gfs_instance_id, epoch, ts, signature}``
      — the space owner's household signature (see
      ``owner_epoch_notice_signing_payload``); confirms the epoch, any raise
      up to the plausibility ceiling;
    * **seed-only** ``{epoch, authority_sig, authority_sig_suite}`` — the
      space-authority signature over ``{space_id, epoch}`` under
      ``space_epoch_notice`` (a delegated admin); anonymous like
      ``/gfs/publish``, and it may raise the epoch by +1 at most, once a
      minute, never the owner-confirmed floor.
    """

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_member_publish_key)
        space_id = self.match("space_id")
        body = await self.body_or_400()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(reason="expected a JSON object")
        try:
            if "owning_instance" in body:
                await svc.note_owner_epoch(
                    space_id,
                    owning_instance=str(_field(body, "owning_instance")),
                    gfs_instance_id=str(_field(body, "gfs_instance_id")),
                    epoch=_field(body, "epoch"),
                    ts=str(_field(body, "ts")),
                    signature=str(_field(body, "signature")),
                )
            else:
                await svc.note_epoch(
                    space_id,
                    _field(body, "epoch"),
                    str(_field(body, "authority_sig")),
                    str(_field(body, "authority_sig_suite")),
                )
        except PermissionError as exc:
            log.debug("GFS epoch notice refused for space %s: %s", space_id, exc)
            return web.json_response(_REFUSED_NOTICE, status=403)
        return web.json_response({"status": "ok"})


def _field(body: dict, name: str) -> object:
    try:
        return body[name]
    except KeyError as exc:
        raise web.HTTPBadRequest(reason=f"Missing field: {exc}") from exc
