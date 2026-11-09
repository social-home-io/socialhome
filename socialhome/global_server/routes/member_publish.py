"""Member publish routes.

* ``POST /gfs/member-publish-anon`` — a member household publishes its own
  space item WITHOUT naming itself, authorized by the space's per-epoch
  writer group key (the only member-publish route since v_90).
* ``POST /gfs/spaces/{space_id}/epoch`` — a seed holder's content-epoch
  notice (optionally carrying the writer key cert).

The logic lives in :mod:`socialhome.global_server.member_publish`; these views
only parse, call and map refusals to ONE uniform ``403`` body each (the reason
goes to DEBUG), as ``PublishView`` does.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ...domain.gfs_member_publish import (
    InvalidMemberPublish,
    MemberPublishAnonRequest,
)
from ...domain.space_item import PAYLOAD_SUITE_UNSUPPORTED_ERROR, PayloadSuiteRefused
from .. import app_keys as K
from ..member_publish import MemberPublishBusy, MemberPublishRateLimited
from ..public import PUBLISH_MAX_BODY_BYTES
from .base import GfsBaseView

log = logging.getLogger(__name__)

_REFUSED_PUBLISH = {"error": "not authorized to publish to this space"}
_REFUSED_NOTICE = {"error": "not authorized for this space"}


def _suite_refused() -> web.Response:
    """``409`` for an authorized but compressed item a follower of the
    space cannot open — the household re-sends it as JSON."""
    return web.json_response({"error": PAYLOAD_SUITE_UNSUPPORTED_ERROR}, status=409)


class MemberPublishAnonView(GfsBaseView):
    """``POST /gfs/member-publish-anon`` — (anonymous) member publish; see
    :mod:`..member_publish`.

    ``200 {"status": "published"}`` once accepted; ``400`` for a malformed
    body (including one carrying ``instance_id``, a household ``signature``
    or a ``writer_cert``); ``403`` (uniform) for any refusal, a replay
    included; ``409 payload_suite_unsupported`` for a compressed item while a
    follower of the space lacks that suite; ``429`` past the per-(space,
    client address), per-space or per-writer-key limit; ``503`` while the
    fan-out is full. Nothing about the caller is logged — there is nothing
    identifying to log."""

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_member_publish_key)
        body = await self.bounded_json(PUBLISH_MAX_BODY_BYTES)
        try:
            req = MemberPublishAnonRequest.from_wire(body)
        except InvalidMemberPublish as exc:
            raise web.HTTPBadRequest(reason=str(exc)) from exc
        try:
            await svc.publish_anon(req, client_ip=self.client_bucket())
        except MemberPublishRateLimited:
            resp = web.json_response({"error": "rate_limited"}, status=429)
            resp.headers["Retry-After"] = "60"
            return resp
        except MemberPublishBusy:
            resp = web.json_response({"error": "busy"}, status=503)
            resp.headers["Retry-After"] = "5"
            return resp
        except PayloadSuiteRefused:
            return _suite_refused()
        except PermissionError as exc:
            log.debug(
                "GFS anonymous member publish refused for space %s: %s",
                req.target,
                exc,
            )
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

    Either form may carry ``writer_key_cert`` — inside the owner's household
    signature, authority-signed on its own in the seed-only form. See
    ``GfsMemberPublishService``.
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
                    writer_key_cert=body.get("writer_key_cert"),
                    gfs_key=body.get("gfs_key"),
                )
            else:
                await svc.note_epoch(
                    space_id,
                    _field(body, "epoch"),
                    str(_field(body, "authority_sig")),
                    str(_field(body, "authority_sig_suite")),
                    writer_key_cert=body.get("writer_key_cert"),
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
