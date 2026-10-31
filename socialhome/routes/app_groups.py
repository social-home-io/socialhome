"""Group-table routes (v_76) — ``/api/apps/{app_id}/groups…``.

For Social Home's own screens (the invite sheet's group mode, the lobby,
Needs you) and the coming SDK bridge (``sh.group.*``): open a table with
people picked in the sheet, list / read the caller's tables, and the
answers and the host's commands. People are seats here — a view never
carries a username (:meth:`AppGroupService.view`).

Every route needs the app's ``people`` permission (and ``federation`` once
anyone of another household is involved — checked by the service, which
knows where people live). Refusals are coded domain errors
(``APP_GROUP_*``) mapped by ``BaseView._iter``.
"""

from __future__ import annotations

import orjson
from aiohttp import web

from ..app_keys import app_group_service_key
from ..domain.app_group import GROUP_MESSAGE_MAX_BYTES
from ..domain.apps import PERMISSION_PEOPLE
from ..security import error_response
from .apps import AppPermissionView, person_target


class AppGroupCollectionView(AppPermissionView):
    """``GET /api/apps/{app_id}/groups`` — the caller's tables in this app
    (``{groups: [...]}``). ``POST`` — open a table with the people picked
    in the invite sheet: ``{targets: [{instance_id, user_ref, is_local}],
    max_players?, lobby_days? (1|3|7|14), note? (≤ 80, one line)}`` → 201
    ``{group}``."""

    async def get(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        groups = await self.svc(app_group_service_key).list_tables(
            app_id=self.match("app_id"), actor_user_id=self.user.user_id
        )
        return self._json({"groups": groups})

    async def post(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        body = await self.body()
        raw = body.get("targets")
        targets = (
            [person_target({"target": t}) for t in raw]
            if isinstance(raw, list)
            else None
        )
        if not targets or any(t is None for t in targets):
            return error_response(
                400,
                "UNPROCESSABLE",
                "targets must be a non-empty list of {instance_id, user_ref, "
                "is_local}.",
            )
        max_players = body.get("max_players")
        lobby_days = body.get("lobby_days", 3)
        note = body.get("note")
        if (max_players is not None and not _is_int(max_players)) or not _is_int(
            lobby_days
        ):
            return error_response(
                400, "UNPROCESSABLE", "max_players / lobby_days must be integers."
            )
        if note is not None and not isinstance(note, str):
            return error_response(400, "UNPROCESSABLE", "note must be a string.")
        group = await self.svc(app_group_service_key).open_table(
            app_id=self.match("app_id"),
            actor_user_id=self.user.user_id,
            targets=[t for t in targets if t is not None],
            max_players=max_players,
            lobby_days=lobby_days,
            note=note,
        )
        return self._json({"group": group}, status=201)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class AppGroupDetailView(AppPermissionView):
    """``GET /api/apps/{app_id}/groups/{session_id}`` — one table the caller
    has a place at (``{group}``); 404 ``APP_GROUP_NOT_FOUND`` otherwise."""

    async def get(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        group = await self.svc(app_group_service_key).get_table(
            app_id=self.match("app_id"),
            session_id=self.match("session_id"),
            actor_user_id=self.user.user_id,
        )
        return self._json({"group": group})


class AppGroupJoinView(AppPermissionView):
    """``POST …/groups/{session_id}/join`` — the invitee joins (``{group}``;
    ``my_state`` reads ``joining`` until the host household seats them).
    403 ``APP_GROUP_CANT_JOIN`` when this household keeps them out (no
    reason given), 409 ``APP_GROUP_FULL`` / ``APP_GROUP_CLOSED``."""

    async def post(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        group = await self.svc(app_group_service_key).join(
            app_id=self.match("app_id"),
            session_id=self.match("session_id"),
            actor_user_id=self.user.user_id,
        )
        return self._json({"group": group})


class AppGroupDeclineView(AppPermissionView):
    """``POST …/groups/{session_id}/decline`` — the invitee says no."""

    async def post(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        await self.svc(app_group_service_key).decline(
            app_id=self.match("app_id"),
            session_id=self.match("session_id"),
            actor_user_id=self.user.user_id,
        )
        return self._json({"ok": True})


class AppGroupLeaveView(AppPermissionView):
    """``POST …/groups/{session_id}/leave`` — leave a table one joined."""

    async def post(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        await self.svc(app_group_service_key).leave(
            app_id=self.match("app_id"),
            session_id=self.match("session_id"),
            actor_user_id=self.user.user_id,
        )
        return self._json({"ok": True})


class AppGroupStartView(AppPermissionView):
    """``POST …/groups/{session_id}/start`` — the host person starts the
    game (``{group}``). 403 ``APP_GROUP_NOT_HOST``, 409
    ``APP_GROUP_TOO_FEW`` (``params.min``) / ``APP_GROUP_CLOSED``."""

    async def post(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        group = await self.svc(app_group_service_key).start(
            app_id=self.match("app_id"),
            session_id=self.match("session_id"),
            actor_user_id=self.user.user_id,
        )
        return self._json({"group": group})


class AppGroupRemoveView(AppPermissionView):
    """``POST …/groups/{session_id}/remove`` — the host person takes the
    player in ``{seat}`` out (``{group}``)."""

    async def post(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        seat = (await self.body()).get("seat")
        if not isinstance(seat, int) or isinstance(seat, bool) or seat <= 0:
            return error_response(
                400, "UNPROCESSABLE", "seat must be a positive integer."
            )
        group = await self.svc(app_group_service_key).remove(
            app_id=self.match("app_id"),
            session_id=self.match("session_id"),
            actor_user_id=self.user.user_id,
            seat=seat,
        )
        return self._json({"group": group})


class AppGroupCloseView(AppPermissionView):
    """``POST …/groups/{session_id}/close`` — the host person closes the
    table for everyone."""

    async def post(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        await self.svc(app_group_service_key).close(
            app_id=self.match("app_id"),
            session_id=self.match("session_id"),
            actor_user_id=self.user.user_id,
        )
        return self._json({"ok": True})


class AppGroupMessagesView(AppPermissionView):
    """``POST …/groups/{session_id}/messages`` — a seated person's app sends
    ``{data, to?: [seat…], turn?: seat}`` to everyone at the table, or to
    the seats in ``to`` (sealed to their households' seat keys — the hub
    passes on ciphertext only). ``turn`` names whose turn it is now. →
    ``{seq}`` (``null`` when it went to the hub's household). 413
    ``PAYLOAD_TOO_LARGE`` past 64 KiB, 429 ``APP_GROUP_BUSY``, 409
    ``APP_GROUP_SEAT`` / ``APP_GROUP_CLOSED``."""

    async def post(self) -> web.Response:
        await self.require_permission(PERMISSION_PEOPLE)
        body = await self.body()
        if "data" not in body:
            return error_response(400, "UNPROCESSABLE", "data is required.")
        to = body.get("to")
        turn = body.get("turn")
        if to is not None and (
            not isinstance(to, list)
            or not to
            or not all(_is_int(s) and s > 0 for s in to)
        ):
            return error_response(
                400, "UNPROCESSABLE", "to must be a non-empty list of seats."
            )
        if turn is not None and not (_is_int(turn) and turn > 0):
            return error_response(400, "UNPROCESSABLE", "turn must be a seat.")
        try:
            size = len(orjson.dumps(body["data"]))
        except TypeError, ValueError:
            return error_response(
                400, "UNPROCESSABLE", "data must be JSON-serialisable."
            )
        if size > GROUP_MESSAGE_MAX_BYTES:
            return error_response(
                413,
                "PAYLOAD_TOO_LARGE",
                f"data exceeds the {GROUP_MESSAGE_MAX_BYTES // 1024} KiB limit.",
            )
        result = await self.svc(app_group_service_key).send_message(
            app_id=self.match("app_id"),
            session_id=self.match("session_id"),
            actor_user_id=self.user.user_id,
            data=body["data"],
            to=to,
            turn=turn,
        )
        return self._json(result)
