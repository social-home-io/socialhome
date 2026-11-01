"""App engine routes (v_78) — ``/api/apps/{app_id}/groups/{session_id}/engines/{engine}…``.

One route family for every app engine (:mod:`socialhome.app_engines`),
dispatched through the registry, which lets an app call only an engine its
manifest declares (``403 APP_ENGINE_NOT_DECLARED``) and this Social Home
has (``409 APP_ENGINE_UNSUPPORTED``):

* ``GET …/engines/{engine}`` — the engine's state as the app may know it;
* ``GET|POST …/engines/{engine}/{action}`` — the engine's own actions
  (the gift draw: ``POST configure``, ``POST wish``, ``POST start``, and
  ``GET result`` — whom the caller gives to, read only by Social Home's own
  result sheet: an app's sandboxed frame can't reach the API, and the
  bridge has no call for it).

Every route needs the app's ``people`` permission; the engine checks the
caller is seated at the table. Refusals are coded domain errors mapped by
``BaseView._iter``.
"""

from __future__ import annotations

import re

from aiohttp import web

from ..app_engines import AppEngineActionError, EngineCall
from ..app_keys import app_engine_registry_key
from ..domain.apps import PERMISSION_PEOPLE
from .apps import AppPermissionView

_ACTION_RE = re.compile(r"[a-z][a-z_]{0,31}")


class _EngineView(AppPermissionView):
    async def _call(self, method: str, action: str | None) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        if action is not None and not _ACTION_RE.fullmatch(action):
            raise AppEngineActionError()
        app_id = self.match("app_id")
        engine = await self.svc(app_engine_registry_key).require(
            app_id, self.match("engine")
        )
        body = await self.body() if method == "POST" else {}
        result = await engine.call(
            EngineCall(
                app_id=app_id,
                session_id=self.match("session_id"),
                actor_user_id=self.user.user_id,
                method=method,
                action=action,
                body=body if isinstance(body, dict) else {},
            )
        )
        return self._json(result)


class AppEngineView(_EngineView):
    """``GET …/engines/{engine}`` → ``{state}`` (the gift draw: ``state``
    gathering | drawing | ready | failed, ``reason``, ``gen``, ``again``,
    ``round``, ``of``, ``done``, ``waiting_for``, ``blamed``,
    ``settings``, ``wish``, ``has_result``, ``is_host`` — never a result)."""

    async def get(self) -> web.Response:
        return await self._call("GET", None)


class AppEngineActionView(_EngineView):
    """``GET|POST …/engines/{engine}/{action}`` — the engine's own action
    (404 ``APP_ENGINE_ACTION`` for one it doesn't have)."""

    async def get(self) -> web.Response:
        return await self._call("GET", self.match("action"))

    async def post(self) -> web.Response:
        return await self._call("POST", self.match("action"))
