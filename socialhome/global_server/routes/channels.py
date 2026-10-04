"""Opaque channel routes for private spaces (v_51).

Every identifier rides in the JSON body — never the URL path — so the access
log holds no channel ids. The logic lives in
:mod:`socialhome.global_server.channels`; these views parse, call and map
refusals to ONE uniform ``403`` body (the reason goes to DEBUG).

* ``POST /gfs/channels/register`` — ``201 {"status": "registered"}`` /
  ``200 {"status": "refreshed" | "repinned"}``; ``409`` pinned to another key;
  ``429`` past the per-address registration budget.
* ``POST /gfs/channels/epoch`` — ``200``; ``429`` + ``Retry-After`` while the
  epoch is ahead of its time-bounded allowance.
* ``POST /gfs/channels/unregister`` — ``200`` (idempotent).
* ``POST /gfs/channels/subscribe`` / ``unsubscribe`` — ``200``.
* ``POST /gfs/channels/publish`` / ``publish-anon`` — ``200``; ``429``; ``503``
  while the fan-out is full.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from ...domain.gfs_channel import (
    ChannelEpochNotice,
    ChannelPublishAnonRequest,
    ChannelPublishRequest,
    ChannelRegisterRequest,
    ChannelSubscribeRequest,
    ChannelUnregisterRequest,
    ChannelUnsubscribeRequest,
    InvalidChannelWire,
)
from .. import app_keys as K
from ..channels import ChannelEpochTooSoon, ChannelPinned
from ..member_publish import MemberPublishBusy, MemberPublishRateLimited
from ..public import PUBLISH_MAX_BODY_BYTES
from .base import GfsBaseView

log = logging.getLogger(__name__)

_REFUSED = {"error": "not authorized for this channel"}
#: Control requests are small; only publishes carry a payload.
_CONTROL_MAX_BODY_BYTES: int = 16 * 1024


class _ChannelView(GfsBaseView):
    async def _parse(self, cls: Any, max_bytes: int = _CONTROL_MAX_BODY_BYTES) -> Any:
        body = await self.bounded_json(max_bytes)
        try:
            return cls.from_wire(body)
        except InvalidChannelWire as exc:
            raise web.HTTPBadRequest(reason=str(exc)) from exc

    @staticmethod
    def _limited(retry_after: int = 60) -> web.Response:
        resp = web.json_response({"error": "rate_limited"}, status=429)
        resp.headers["Retry-After"] = str(max(1, int(retry_after)))
        return resp

    @staticmethod
    def _refused(what: str, channel_id: str, exc: Exception) -> web.Response:
        log.debug("GFS channel %s refused for channel %s: %s", what, channel_id, exc)
        return web.json_response(_REFUSED, status=403)


class ChannelRegisterView(_ChannelView):
    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_channel_service_key)
        req = await self._parse(ChannelRegisterRequest)
        try:
            status = await svc.register(req, client_ip=self.client_ip())
        except MemberPublishRateLimited:
            return self._limited()
        except ChannelPinned:
            return web.json_response({"error": "channel_pinned"}, status=409)
        except PermissionError as exc:
            return self._refused("register", req.channel_id, exc)
        return web.json_response(
            {"status": status}, status=201 if status == "registered" else 200
        )


class ChannelEpochView(_ChannelView):
    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_channel_service_key)
        req = await self._parse(ChannelEpochNotice)
        try:
            await svc.note_epoch(req)
        except ChannelEpochTooSoon as exc:
            return self._limited(exc.retry_after_s)
        except PermissionError as exc:
            return self._refused("epoch notice", req.channel_id, exc)
        return web.json_response({"status": "ok"})


class ChannelUnregisterView(_ChannelView):
    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_channel_service_key)
        req = await self._parse(ChannelUnregisterRequest)
        try:
            await svc.unregister(req)
        except PermissionError as exc:
            return self._refused("unregister", req.channel_id, exc)
        return web.json_response({"status": "ok"})


class ChannelSubscribeView(_ChannelView):
    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_channel_service_key)
        req = await self._parse(ChannelSubscribeRequest)
        try:
            await svc.subscribe(req)
        except PermissionError as exc:
            return self._refused("subscribe", req.channel_id, exc)
        return web.json_response({"status": "subscribed"})


class ChannelUnsubscribeView(_ChannelView):
    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_channel_service_key)
        req = await self._parse(ChannelUnsubscribeRequest)
        try:
            await svc.unsubscribe(req)
        except PermissionError as exc:
            return self._refused("unsubscribe", req.channel_id, exc)
        return web.json_response({"status": "unsubscribed"})


class ChannelPublishView(_ChannelView):
    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_channel_service_key)
        req = await self._parse(ChannelPublishRequest, PUBLISH_MAX_BODY_BYTES)
        try:
            await svc.publish(req)
        except MemberPublishRateLimited:
            return self._limited()
        except MemberPublishBusy:
            resp = web.json_response({"error": "busy"}, status=503)
            resp.headers["Retry-After"] = "5"
            return resp
        except PermissionError as exc:
            return self._refused("publish", req.channel_id, exc)
        return web.json_response({"status": "published"})


class ChannelPublishAnonView(_ChannelView):
    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_channel_service_key)
        req = await self._parse(ChannelPublishAnonRequest, PUBLISH_MAX_BODY_BYTES)
        try:
            await svc.publish_anon(req, client_ip=self.client_ip())
        except MemberPublishRateLimited:
            return self._limited()
        except MemberPublishBusy:
            resp = web.json_response({"error": "busy"}, status=503)
            resp.headers["Retry-After"] = "5"
            return resp
        except PermissionError as exc:
            return self._refused("anonymous publish", req.channel_id, exc)
        return web.json_response({"status": "published"})
