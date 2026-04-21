"""WebSocketManager — fan-out of in-process events to connected clients (§5.3).

The frontend opens a single WebSocket against ``/api/ws`` with its
bearer token. The manager keeps a per-user set of connections and
exposes:

* :meth:`broadcast_to_user` — fan to all of one user's sockets
  (e.g. their own browser + mobile + desktop tabs).
* :meth:`broadcast_to_users` — fan to a set of users
  (e.g. all members of a space when a post lands).
* :meth:`broadcast_all` — fan to every connected session
  (rare — used by admin maintenance broadcasts).

The manager has no knowledge of *which* events to send — that is the
job of :class:`social_home.services.realtime_service.RealtimeService`,
which subscribes to domain events on the bus and translates them into
JSON frames the frontend understands.

Closing semantics: connections are tracked weakly — when a client
closes, the WS handler removes itself via :meth:`unregister`. Stale
sockets that fail to send are dropped on the next attempt rather than
raising, so a single dead client cannot block the rest of the fan-out.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any

from aiohttp import web

from ..security import sanitise_for_api

log = logging.getLogger(__name__)


class WebSocketManager:
    """Per-user registry of live WebSocket sessions."""

    __slots__ = ("_by_user", "_lock")

    def __init__(self) -> None:
        # ``set`` so duplicate registrations are idempotent.
        self._by_user: dict[str, set[web.WebSocketResponse]] = defaultdict(set)
        self._lock = asyncio.Lock()

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def register(self, user_id: str, ws: web.WebSocketResponse) -> None:
        async with self._lock:
            self._by_user[user_id].add(ws)
        log.debug("ws.register: user=%s total=%d", user_id, self.connection_count())

    async def unregister(self, user_id: str, ws: web.WebSocketResponse) -> None:
        async with self._lock:
            sessions = self._by_user.get(user_id)
            if sessions is not None:
                sessions.discard(ws)
                if not sessions:
                    self._by_user.pop(user_id, None)
        log.debug("ws.unregister: user=%s total=%d", user_id, self.connection_count())

    # ─── Inspection ───────────────────────────────────────────────────────

    def connection_count(self) -> int:
        return sum(len(s) for s in self._by_user.values())

    def session_count_for_user(self, user_id: str) -> int:
        return len(self._by_user.get(user_id, set()))

    def connected_users(self) -> set[str]:
        return set(self._by_user.keys())

    # ─── Fan-out ──────────────────────────────────────────────────────────

    async def broadcast_to_user(self, user_id: str, payload: dict[str, Any]) -> int:
        """Send a JSON frame to every connection for *user_id*.

        Returns the number of sockets that successfully received the
        message. Failed sockets are dropped from the registry so they
        don't block subsequent fan-outs.
        """
        sessions = list(self._by_user.get(user_id, set()))
        if not sessions:
            return 0
        return await self._send_many(user_id, sessions, payload)

    async def broadcast_to_users(
        self,
        user_ids: list[str] | set[str],
        payload: dict[str, Any],
    ) -> int:
        """Fan a frame to many users in parallel."""
        if not user_ids:
            return 0
        results = await asyncio.gather(
            *(self.broadcast_to_user(uid, payload) for uid in user_ids),
            return_exceptions=True,
        )
        return sum(r for r in results if isinstance(r, int))

    async def broadcast_all(self, payload: dict[str, Any]) -> int:
        """Send to every connected session (admin-broadcast)."""
        return await self.broadcast_to_users(list(self._by_user.keys()), payload)

    # ─── Internal ─────────────────────────────────────────────────────────

    async def _send_many(
        self,
        user_id: str,
        sessions: list[web.WebSocketResponse],
        payload: dict[str, Any],
    ) -> int:
        msg = json.dumps(sanitise_for_api(payload), default=str)
        delivered = 0
        dead: list[web.WebSocketResponse] = []
        for ws in sessions:
            if ws.closed:
                dead.append(ws)
                continue
            try:
                await ws.send_str(msg)
                delivered += 1
            except ConnectionResetError, RuntimeError, asyncio.CancelledError:
                dead.append(ws)
            except Exception as exc:  # defensive
                log.debug("ws send failed user=%s: %s", user_id, exc)
                dead.append(ws)
        if dead:
            async with self._lock:
                live = self._by_user.get(user_id, set())
                for ws in dead:
                    live.discard(ws)
                if not live:
                    self._by_user.pop(user_id, None)
        return delivered
