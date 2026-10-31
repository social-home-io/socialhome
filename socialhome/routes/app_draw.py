"""Gift draw routes (v_78) — ``/api/apps/{app_id}/groups/{session_id}/draw…``.

For the app bridge (``sh.draw.*``) and Social Home's own result sheet:

* ``GET …/draw`` — the draw's state as the app may know it (never a
  result); ``PATCH …/draw`` — the organiser's settings.
* ``PUT …/draw/wish`` — the caller's wish list.
* ``POST …/draw/start`` — Draw names (``{again: true}`` draws again after
  a reveal).
* ``GET …/draw/result`` — whom the caller gives to. Only Social Home's own
  sheet reads it: an app's sandboxed frame has no route to the API (opaque
  origin, ``connect-src 'none'``) and the bridge has no call for it.

Every route needs the app's ``people`` permission; the caller must be
seated at the table. Refusals are coded domain errors (``APP_DRAW_*``)
mapped by ``BaseView._iter``.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import app_draw_service_key
from ..domain.apps import PERMISSION_PEOPLE
from ..security import error_response
from .apps import AppPermissionView


class _DrawView(AppPermissionView):
    def _ids(self) -> dict[str, str]:
        return {
            "app_id": self.match("app_id"),
            "session_id": self.match("session_id"),
            "actor_user_id": self.user.user_id,
        }


class AppDrawView(_DrawView):
    """``GET …/draw`` → ``{draw}``: ``state`` (``gathering`` | ``drawing``
    | ``ready`` | ``failed``), ``reason``, ``gen``, ``again``, ``round``,
    ``of``, ``done`` (organiser), ``waiting_for`` (organiser, after an
    hour), ``false_complaints`` (organiser), ``settings``, ``wish`` (the
    caller's own), ``has_result``, ``is_host``.

    ``PATCH …/draw`` ``{name?, budget?, day?, pairs?: [[seat, seat]]}`` —
    the organiser's settings, before the draw → ``{draw}``."""

    async def get(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        draw = await self.svc(app_draw_service_key).status(**self._ids())
        return self._json({"draw": draw})

    async def patch(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        body = await self.body()
        draw = await self.svc(app_draw_service_key).configure(
            **self._ids(), settings=body
        )
        return self._json({"draw": draw})


class AppDrawWishView(_DrawView):
    """``PUT …/draw/wish`` ``{text}`` (≤ 500 characters, fitting the 1 KiB
    card) → ``{draw}``. After the reveal it reaches the giver (≤ 5 times;
    429 ``APP_DRAW_WISH_UPDATES``)."""

    async def put(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        text = (await self.body()).get("text")
        if not isinstance(text, str):
            return error_response(400, "UNPROCESSABLE", "text must be a string.")
        draw = await self.svc(app_draw_service_key).set_wish(**self._ids(), text=text)
        return self._json({"draw": draw})


class AppDrawStartView(_DrawView):
    """``POST …/draw/start`` ``{again?: bool}`` — the organiser draws
    names (starting the table if it waits for players) or, after a
    reveal, draws again → ``{draw}``. 409 ``APP_DRAW_TOO_FEW`` /
    ``APP_DRAW_STATE`` / ``APP_DRAW_HOUSEHOLD_TOO_OLD`` /
    ``APP_DRAW_PAST_DAY``, 422 ``APP_DRAW_PAIRS``."""

    async def post(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        again = (await self.body()).get("again", False)
        if not isinstance(again, bool):
            return error_response(400, "UNPROCESSABLE", "again must be a boolean.")
        draw = await self.svc(app_draw_service_key).start(**self._ids(), again=again)
        return self._json({"draw": draw})


class AppDrawResultView(_DrawView):
    """``GET …/draw/result`` → ``{result: {name, household, wish,
    wish_updated, draw_name, budget, day, again, handed_over_by}}`` — the
    caller's own recipient, for Social Home's result sheet. 404
    ``APP_DRAW_NO_RESULT`` before the reveal."""

    async def get(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        result = await self.svc(app_draw_service_key).result(**self._ids())
        return self._json({"result": result})
