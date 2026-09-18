"""Background supervisor that keeps one ``GfsWebSocketClient`` per pairing.

Spec §24.12. The set of paired GFSes can change at any time (admin
pairs a new one via the UI, or disconnects an existing one). The
supervisor periodically reconciles the live client set against
``gfs_connection_repo.list_active()`` and starts / stops clients to
match.

Lifecycle follows the project-standard ``_stop: asyncio.Event`` pattern
(reference: :class:`socialhome.infrastructure.replay_cache_scheduler.ReplayCachePruneScheduler`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiohttp

from ..domain.federation import GfsConnection
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..services.gfs_ws_client import GfsWebSocketClient

log = logging.getLogger(__name__)


DEFAULT_RECONCILE_INTERVAL_SECONDS = 30.0


class GfsWebSocketSupervisor:
    """Owns the per-pairing :class:`GfsWebSocketClient` set.

    The supervisor never blocks a public method on a network call —
    starts and stops all run on the background loop.
    """

    __slots__ = (
        "_repo",
        "_instance_id",
        "_signing_key",
        "_session_factory",
        "_on_relay",
        "_on_highlight_signal",
        "_on_moment_signal",
        "_on_moment_public",
        "_on_follow_changed",
        "_on_new_subscriber",
        "_on_envelope",
        "_on_connected",
        "_interval",
        "_clients",
        "_client_urls",
        "_lock",
        "_stop",
        "_task",
    )

    def __init__(
        self,
        *,
        repo: AbstractGfsConnectionRepo,
        instance_id: str,
        signing_key: bytes,
        session_factory: Callable[[], aiohttp.ClientSession],
        on_relay: Callable[[dict], Awaitable[None]],
        on_highlight_signal: Callable[[dict], Awaitable[None]] | None = None,
        on_moment_signal: Callable[[dict], Awaitable[None]] | None = None,
        on_moment_public: Callable[..., Awaitable[None]] | None = None,
        on_follow_changed: Callable[[dict], Awaitable[None]] | None = None,
        on_new_subscriber: Callable[[dict], Awaitable[None]] | None = None,
        on_envelope: Callable[..., Awaitable[None]] | None = None,
        on_connected: Callable[[str], Awaitable[None]] | None = None,
        reconcile_interval_seconds: float = DEFAULT_RECONCILE_INTERVAL_SECONDS,
    ) -> None:
        self._repo = repo
        self._instance_id = instance_id
        self._signing_key = signing_key
        self._session_factory = session_factory
        self._on_relay = on_relay
        self._on_highlight_signal = on_highlight_signal
        self._on_moment_signal = on_moment_signal
        self._on_moment_public = on_moment_public
        self._on_follow_changed = on_follow_changed
        self._on_new_subscriber = on_new_subscriber
        self._on_envelope = on_envelope
        self._on_connected = on_connected
        self._interval = reconcile_interval_seconds
        self._clients: dict[str, GfsWebSocketClient] = {}
        #: ``gfs_id`` → that client's base URL, so a late-bound handler can
        #: be re-bound per connection server (see
        #: :meth:`attach_envelope_handler`).
        self._client_urls: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def attach_highlight_signal_handler(
        self,
        handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Late-bound — attaches the §highlights_public answerer to every
        running client and to clients started later by the reconciler."""
        self._on_highlight_signal = handler
        for client in list(self._clients.values()):
            client.attach_highlight_signal_handler(handler)

    def attach_moment_public_handler(
        self,
        handler: Callable[..., Awaitable[None]],
    ) -> None:
        """Late-bound — attaches the §Momentum-public inbound dispatcher
        to every running client and to clients started later. The
        handler must accept ``(frame, *, gfs_id)``; this method wraps
        it per-client so the right ``gfs_id`` is bound for each
        connection.
        """
        self._on_moment_public = handler
        for gfs_id, client in list(self._clients.items()):
            client.attach_moment_public_handler(_bind_gfs_id(handler, gfs_id))

    def attach_follow_changed_handler(
        self,
        handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        self._on_follow_changed = handler
        for client in list(self._clients.values()):
            client.attach_follow_changed_handler(handler)

    def attach_new_subscriber_handler(
        self,
        handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Late-bound — attaches the Phase-5b subscriber key-handoff producer
        to every running client and to clients started later by the
        reconciler."""
        self._on_new_subscriber = handler
        for client in list(self._clients.values()):
            client.attach_new_subscriber_handler(handler)

    def attach_envelope_handler(
        self,
        handler: Callable[..., Awaitable[None]],
    ) -> None:
        """Late-bound — attaches the §D2b invite-bootstrap inbound leg to
        every running client and to clients started later.

        The handler must accept ``(frame, *, gfs_url)``; this method binds
        the right URL per client, so the issuer's sealed reply goes back out
        the same connection server the request arrived on rather than
        whichever one happens to be first.
        """
        self._on_envelope = handler
        for gfs_id, client in list(self._clients.items()):
            client.attach_envelope_handler(
                _bind_gfs_url(handler, self._client_urls.get(gfs_id, "")),
            )

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Run an immediate reconcile and spawn the background loop."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        await self._reconcile_once()
        self._task = asyncio.create_task(self._loop(), name="gfs-ws-supervisor")

    async def stop(self) -> None:
        """Stop every client and the reconcile loop."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            await client.stop()

    # ─── Inspection ───────────────────────────────────────────────────────

    def client_count(self) -> int:
        return len(self._clients)

    def is_running(self, gfs_id: str) -> bool:
        return gfs_id in self._clients

    def connection_health(self, gfs_id: str) -> dict:
        """Live WS liveness for one pairing, for the connections UI.

        Distinct from the stored ``gfs_connections.status`` (the pairing
        state): this reflects whether a WebSocket is actually open right
        now and, if not, the last auth/close reason the GFS gave. A
        ``status='active'`` pairing whose socket is rejected/down reads
        as ``connected=False`` here so the SPA can stop showing it as
        "connected".
        """
        client = self._clients.get(gfs_id)
        if client is None:
            return {"connected": False, "last_error": None}
        return {"connected": client.connected, "last_error": client.last_auth_error}

    # ─── Reconciliation ───────────────────────────────────────────────────

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
                return  # _stop fired
            except asyncio.TimeoutError:
                pass
            try:
                await self._reconcile_once()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("gfs.ws.supervisor: reconcile failed: %s", exc)

    async def _reconcile_once(self) -> None:
        """Sync the running-client set with the repo's active pairings."""
        active = await self._repo.list_active()
        active_ids = {c.id for c in active}
        async with self._lock:
            current_ids = set(self._clients.keys())
            # A client present in the map but whose background loop task has
            # died (uncaught error / cancellation) is NOT running — restart it
            # even though its pairing is still active. Without this, a dead
            # loop would be treated as "running" forever (defense-in-depth).
            dead_ids = {gid for gid, c in self._clients.items() if not c.is_alive()}
        # Start = newly-active OR present-but-dead (``_start_client`` stops the
        # stale instance before replacing it, so a dead client is restarted).
        to_start = [c for c in active if c.id not in current_ids or c.id in dead_ids]
        to_stop_ids = current_ids - active_ids

        for conn in to_start:
            if conn.id in dead_ids:
                log.warning(
                    "gfs.ws.supervisor: restarting dead client gfs_id=%s",
                    conn.id,
                )
            await self._start_client(conn)
        for gfs_id in to_stop_ids:
            await self._stop_client(gfs_id)

    async def _start_client(self, conn: GfsConnection) -> None:
        # The §Momentum-public inbound handler needs to know *which*
        # GFS pushed the frame so it can look up the right verifier
        # key. Wrap the supervisor-level handler in a per-client
        # closure that injects ``conn.id``.
        wrapped_moment_public = (
            _bind_gfs_id(self._on_moment_public, conn.id)
            if self._on_moment_public is not None
            else None
        )
        # The display-name refresh hook needs to know *which* GFS just
        # (re)connected so it can re-fetch that pairing's /gfs/info. Bind
        # ``conn.id`` per client (gfs rename → reconnect → refresh).
        wrapped_on_connected = (
            _bind_on_connected(self._on_connected, conn.id)
            if self._on_connected is not None
            else None
        )
        client = GfsWebSocketClient(
            gfs_url=conn.inbox_url,
            instance_id=self._instance_id,
            signing_key=self._signing_key,
            session_factory=self._session_factory,
            on_relay=self._on_relay,
            on_highlight_signal=self._on_highlight_signal,
            on_moment_signal=self._on_moment_signal,
            on_moment_public=wrapped_moment_public,
            on_follow_changed=self._on_follow_changed,
            on_new_subscriber=self._on_new_subscriber,
            on_envelope=(
                _bind_gfs_url(self._on_envelope, conn.inbox_url)
                if self._on_envelope is not None
                else None
            ),
            on_connected=wrapped_on_connected,
        )
        async with self._lock:
            existing = self._clients.get(conn.id)
            self._clients[conn.id] = client
            self._client_urls[conn.id] = conn.inbox_url
        if existing is not None:
            await existing.stop()
        await client.start()
        log.info(
            "gfs.ws.supervisor: started client gfs_id=%s url=%s",
            conn.id,
            conn.inbox_url,
        )

    async def _stop_client(self, gfs_id: str) -> None:
        async with self._lock:
            client = self._clients.pop(gfs_id, None)
            self._client_urls.pop(gfs_id, None)
        if client is not None:
            await client.stop()
            log.info("gfs.ws.supervisor: stopped client gfs_id=%s", gfs_id)


def _bind_gfs_url(
    handler: Callable[..., Awaitable[None]],
    gfs_url: str,
) -> Callable[[dict], Awaitable[None]]:
    """Return a per-frame wrapper that injects ``gfs_url`` into the call.

    The §D2b inbound leg replies through the same connection server the
    blob arrived on — a household paired with several servers must not
    answer on one the requester isn't listening to.
    """

    async def _wrapped(frame: dict) -> None:
        await handler(frame, gfs_url=gfs_url)

    return _wrapped


def _bind_gfs_id(
    handler: Callable[..., Awaitable[None]],
    gfs_id: str,
) -> Callable[[dict], Awaitable[None]]:
    """Return a per-frame wrapper that injects ``gfs_id`` into the call.

    The §Momentum-public inbound handler needs to know which GFS
    pushed each frame so it can look up the right verifier key. Each
    WS client is bound to one GFS pairing, so we wrap the handler
    once at client-construction time and pass the wrapped version to
    the underlying :class:`GfsWebSocketClient`.
    """

    async def _wrapped(frame: dict) -> None:
        await handler(frame, gfs_id=gfs_id)

    return _wrapped


def _bind_on_connected(
    handler: Callable[[str], Awaitable[None]],
    gfs_id: str,
) -> Callable[[], Awaitable[None]]:
    """Return a zero-arg WS ``on_connected`` callback bound to ``gfs_id``.

    The :class:`GfsWebSocketClient` invokes ``on_connected()`` with no
    args on each (re)connect; the supervisor's hook needs the pairing's
    id to refresh the right connection, so we close over ``conn.id``.
    """

    async def _wrapped() -> None:
        await handler(gfs_id)

    return _wrapped
