"""Push subscription routes (§25.3).

* ``GET    /api/push/vapid_public_key``  — public key for ``pushManager.subscribe()``.
* ``POST   /api/push/subscribe``         — register a subscription.
* ``DELETE /api/push/subscribe/{sub_id}``— remove a subscription.
* ``GET    /api/push/subscriptions``     — list subscriptions for the current user.
"""

from __future__ import annotations

import secrets

from aiohttp import web

from .. import app_keys as K
from ..repositories.push_subscription_repo import PushSubscription
from .base import BaseView


class PushVapidKeyView(BaseView):
    """``GET /api/push/vapid_public_key``."""

    async def get(self) -> web.Response:
        self.user
        svc = self.svc(K.push_service_key)
        return self._json({"public_key": svc.vapid_public_key})


class PushSubscribeView(BaseView):
    """``POST /api/push/subscribe`` + ``DELETE /api/push/subscribe/{sub_id}``."""

    async def post(self) -> web.Response:
        ctx = self.user
        data = await self.body()
        endpoint = data.get("endpoint")
        keys = data.get("keys") or {}
        p256dh = keys.get("p256dh")
        auth = keys.get("auth")
        if not endpoint or not p256dh or not auth:
            return web.json_response({"error": "missing fields"}, status=422)
        repo = self.svc(K.push_subscription_repo_key)
        sub_id = data.get("id") or "sub-" + secrets.token_urlsafe(12)
        sub = PushSubscription(
            id=sub_id,
            user_id=ctx.user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth_secret=auth,
            device_label=data.get("device_label"),
        )
        await repo.save(sub)
        return self._json({"id": sub_id}, status=201)

    async def delete(self) -> web.Response:
        ctx = self.user
        sub_id = self.match("sub_id")
        repo = self.svc(K.push_subscription_repo_key)
        ok = await repo.delete(sub_id, user_id=ctx.user_id)
        return web.Response(status=204 if ok else 404)


class PushSubscriptionListView(BaseView):
    """``GET /api/push/subscriptions``."""

    async def get(self) -> web.Response:
        ctx = self.user
        repo = self.svc(K.push_subscription_repo_key)
        subs = await repo.list_for_user(ctx.user_id)
        return self._json(
            [
                {
                    "id": s.id,
                    "device_label": s.device_label,
                    "created_at": s.created_at,
                }
                for s in subs
            ]
        )
