"""Space games routes (v_77) — ``/api/spaces/{space_id}/games…``.

For the Games section and the "Open games" strip of a space: list the
space's tables, open one, join one, close one (the host person, or the
owner / an admin / a moderator for any table), and the space's games list
("Games allowed here"). Once seated, the table is played through the group
routes (``/api/apps/{app_id}/groups/{session_id}/…``: start, leave,
remove, messages). Every route needs a writer seat in the space (a
follower is refused); refusals are coded domain errors mapped by
``BaseView._iter``.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import space_app_table_service_key, space_service_key
from ..domain.app_group import GROUP_LOBBY_DEFAULT_DAYS
from ..security import error_response
from .base import BaseView


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class SpaceGamesView(BaseView):
    """``GET /api/spaces/{space_id}/games`` — the space's tables as the
    caller sees them, plus the games list: ``{space_id, allowed_apps,
    games_possible, can_edit_games, host_supports, tables: [{session_id,
    app_id, host_name, host_is_local, note, min, max, seated, state
    (open|full|playing|closed), end_reason, late_join, created_at,
    started_at, expires_at, app: {installed, enabled}, group}]}`` —
    ``group`` is the caller's own view of a table they have a place at.

    ``POST`` — open a table: ``{app_id, max_players?, lobby_days? (1|3|7|14),
    note? (≤ 80, one line), announce? (bool: "Let everyone know")}`` → 201
    ``{table}``. 409 ``SPACE_APP_NOT_ALLOWED`` (not on the list, a global or
    archived space), 429 ``SPACE_TABLE_DAILY_LIMIT`` /
    ``SPACE_TABLE_ANNOUNCE_LIMIT`` / ``APP_GROUP_OPEN_LIMIT`` /
    ``APP_GROUP_DAILY_LIMIT``, 422 ``APP_GROUP_SIZE``, and the app's own
    errors when it isn't here or on."""

    async def get(self) -> web.Response:
        out = await self.svc(space_app_table_service_key).list_tables(
            space_id=self.match("space_id"), actor_user_id=self.user.user_id
        )
        return self._json(out)

    async def post(self) -> web.Response:
        body = await self.body()
        app_id = body.get("app_id")
        max_players = body.get("max_players")
        lobby_days = body.get("lobby_days", GROUP_LOBBY_DEFAULT_DAYS)
        note = body.get("note")
        announce = body.get("announce", False)
        if not isinstance(app_id, str) or not app_id:
            return error_response(400, "UNPROCESSABLE", "app_id is required.")
        if (max_players is not None and not _is_int(max_players)) or not _is_int(
            lobby_days
        ):
            return error_response(
                400, "UNPROCESSABLE", "max_players / lobby_days must be integers."
            )
        if note is not None and not isinstance(note, str):
            return error_response(400, "UNPROCESSABLE", "note must be a string.")
        if not isinstance(announce, bool):
            return error_response(400, "UNPROCESSABLE", "announce must be a boolean.")
        table = await self.svc(space_app_table_service_key).open_table(
            space_id=self.match("space_id"),
            actor_user_id=self.user.user_id,
            app_id=app_id,
            max_players=max_players,
            lobby_days=lobby_days,
            note=note,
            announce=announce,
        )
        return self._json({"table": table}, status=201)


class SpaceGameJoinView(BaseView):
    """``POST /api/spaces/{space_id}/games/{session_id}/join`` — take a
    seat (``{group}``; ``my_state`` reads ``joining`` until the host
    household seats them). 403 ``APP_GROUP_CANT_JOIN`` (no reason given),
    409 ``APP_GROUP_FULL`` / ``APP_GROUP_CLOSED`` /
    ``SPACE_APP_NOT_ALLOWED`` / ``SPACE_TABLE_HOUSEHOLD_TOO_OLD``; the app's
    own errors (not here, turned off, too young) — the SPA then offers "Ask
    an admin"."""

    async def post(self) -> web.Response:
        group = await self.svc(space_app_table_service_key).join(
            space_id=self.match("space_id"),
            session_id=self.match("session_id"),
            actor_user_id=self.user.user_id,
        )
        return self._json({"group": group})


class SpaceGameCloseView(BaseView):
    """``POST /api/spaces/{space_id}/games/{session_id}/close`` — the host
    person closes their table, or the owner / an admin / a moderator closes
    any table of the space. 403 ``APP_GROUP_NOT_HOST`` for anyone else."""

    async def post(self) -> web.Response:
        await self.svc(space_app_table_service_key).close(
            space_id=self.match("space_id"),
            session_id=self.match("session_id"),
            actor_user_id=self.user.user_id,
        )
        return self._json({"ok": True})


class SpaceGamesAllowedView(BaseView):
    """``PUT /api/spaces/{space_id}/games/allowed`` — replace the space's
    games list: ``{allowed_apps: [app_id, …]}`` (≤ 20) → ``{allowed_apps}``.
    The owner, an admin or a moderator (owner decision D9); on a space
    hosted elsewhere the edit goes to the host (and may wait for the
    owner's approval, as any forwarded config edit) — then ``forwarded``
    is true and ``allowed_apps`` is still this household's copy (the SPA
    says "sent to the space's home" and the config broadcast refreshes it).
    403 for anyone else, 409 ``SPACE_APP_NOT_ALLOWED`` on a global space,
    422 for a bad list."""

    async def put(self) -> web.Response:
        body = await self.body()
        svc = self.svc(space_service_key)
        space_id = self.match("space_id")
        forwarded = await svc.config_edits_forward(space_id)
        space = await svc.set_allowed_apps(
            space_id,
            actor_username=self.user.username,
            app_ids=body.get("allowed_apps"),
        )
        return self._json(
            {
                "allowed_apps": list(space.features.allowed_apps),
                "forwarded": forwarded,
            }
        )
