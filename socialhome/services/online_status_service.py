"""Online-status (session presence) service.

Tracks WS-session activity per user, drives ``user.online`` /
``user.idle`` / ``user.offline`` domain events. Conceptually orthogonal
to :class:`socialhome.services.presence_service.PresenceService` —
*physical* presence (``home``/``away``/``zone``) comes from HA or the
browser geolocation, *session* presence comes from "is there an open
WebSocket?".

Why a separate service:

* :class:`socialhome.infrastructure.ws_manager.WebSocketManager` owns
  the sockets and knows *how many* open connections a user has — but
  not when a session went idle, when the user was last active, or
  whether to fire a transition event.
* :class:`PresenceService` owns physical state and the GPS pipeline —
  not session bookkeeping.

Per-session activity tracking (``_sessions[user_id][ws_id]``) means a
user with one active tab + one idle tab is correctly *online*, not
*idle*. Idle = every session is past :attr:`IDLE_AFTER`.

Persisted state: ``users.last_seen_at`` is updated on the *last*
session disconnecting, debounced by :attr:`PERSIST_DEBOUNCE` so a
flaky-WiFi reconnect storm doesn't trigger one write per drop. The
in-memory dict is the truth; SQLite is just the durability tier so
"Last seen 2 h ago" survives a restart.

Self-frame suppression: when user X's first session opens, the
``UserCameOnline`` event fires but :class:`RealtimeService` filters X
out of the household fan-out — X's own UI hydrates ``is_online: true``
from the ``/api/presence`` payload that loads on page mount, so a
self-frame would be a redundant flicker.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..domain.events import (
    DomainEvent,
    UserCameOnline,
    UserResumedActive,
    UserWentIdle,
    UserWentOffline,
)
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus
from ..infrastructure.ws_manager import WebSocketManager
from ..repositories.user_repo import AbstractUserRepo

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _RemoteEntry:
    """Last-known online state for a remote user."""

    user_id: str
    instance_id: str
    is_online: bool
    is_idle: bool
    last_seen_at: datetime | None


class OnlineStatusService:
    """Track WS-session presence and publish online/idle/offline events."""

    #: A user is *idle* when every open session has been silent for at
    #: least this long. 5 minutes matches the typical "user wandered
    #: away from the keyboard" feel without flapping on short reading
    #: pauses.
    IDLE_AFTER: timedelta = timedelta(minutes=5)

    #: Don't persist ``last_seen_at`` more often than this on a single
    #: user. A flaky network can produce a connect/disconnect every few
    #: seconds; the in-memory state is the source of truth, so missing
    #: a SQLite update has no user-visible cost.
    PERSIST_DEBOUNCE: timedelta = timedelta(seconds=30)

    #: How often the background loop scans for idle/resumed transitions.
    SCHEDULER_INTERVAL: float = 60.0

    __slots__ = (
        "_ws",
        "_user_repo",
        "_bus",
        "_federation",
        "_federation_repo",
        "_own_instance_id",
        "_sessions",
        "_idle_users",
        "_last_persisted",
        "_remote",
        "_lock",
        "_task",
        "_stop",
    )

    def __init__(
        self,
        ws_manager: WebSocketManager,
        user_repo: AbstractUserRepo,
        bus: EventBus | None = None,
    ) -> None:
        self._ws = ws_manager
        self._user_repo = user_repo
        self._bus = bus
        self._federation: FederationService | None = None
        self._federation_repo: AbstractFederationRepo | None = None
        self._own_instance_id: str = ""
        # ``_sessions[user_id]`` = ``{ws_id: last_active_dt}``
        self._sessions: dict[str, dict[int, datetime]] = {}
        self._idle_users: set[str] = set()
        self._last_persisted: dict[str, datetime] = {}
        # Cross-instance state — keyed by ``user_id``. Ephemeral: a peer
        # restart triggers a full re-emit on their side, so we never
        # persist this dict.
        self._remote: dict[str, _RemoteEntry] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def attach_event_bus(self, bus: EventBus) -> None:
        """Attach an :class:`EventBus` after construction."""
        self._bus = bus

    def attach_federation(
        self,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
        own_instance_id: str,
    ) -> None:
        """Attach federation so transitions fan out to paired peers
        and inbound `USER_ONLINE` / `USER_IDLE` / `USER_OFFLINE` events
        update the remote-state cache."""
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._own_instance_id = own_instance_id

    # ─── Inspection (used by route handlers + RealtimeService) ────────────

    def is_online(self, user_id: str) -> bool:
        if user_id in self._sessions and self._sessions[user_id]:
            return True
        remote = self._remote.get(user_id)
        return remote is not None and remote.is_online

    def is_idle(self, user_id: str) -> bool:
        """True iff online AND idle. Considers both local and remote state.

        Returns False for offline users (offline is not idle).
        """
        if user_id in self._idle_users:
            return True
        remote = self._remote.get(user_id)
        return remote is not None and remote.is_idle

    def last_seen(self, user_id: str) -> datetime | None:
        """Most recent activity timestamp across all of ``user_id``'s sessions.

        Falls through to the remote cache for cross-instance users; callers
        that need the persisted value for offline local users should fall
        back to ``users.last_seen_at`` from the repo.
        """
        sessions = self._sessions.get(user_id)
        if sessions:
            return max(sessions.values())
        remote = self._remote.get(user_id)
        if remote is not None:
            return remote.last_seen_at
        return None

    def online_user_ids(self) -> set[str]:
        local = {uid for uid, sess in self._sessions.items() if sess}
        local |= {uid for uid, r in self._remote.items() if r.is_online}
        return local

    def idle_user_ids(self) -> set[str]:
        local = set(self._idle_users)
        local |= {uid for uid, r in self._remote.items() if r.is_idle}
        return local

    # ─── Lifecycle hooks (called by routes/ws.py) ─────────────────────────

    async def user_session_opened(self, user_id: str, ws_id: int) -> None:
        """A new WS session for ``user_id`` just registered."""
        now = _utcnow()
        was_offline = False
        was_idle = False
        async with self._lock:
            sessions = self._sessions.setdefault(user_id, {})
            was_offline = not sessions
            was_idle = user_id in self._idle_users
            sessions[ws_id] = now
            # Opening a session counts as activity → resume from idle if
            # we were idle.
            if was_idle:
                self._idle_users.discard(user_id)
        if was_offline:
            await self._publish(UserCameOnline(user_id=user_id))
            await self._fan_to_peers(
                FederationEventType.USER_ONLINE,
                user_id=user_id,
            )
        elif was_idle:
            await self._publish(UserResumedActive(user_id=user_id))
            await self._fan_to_peers(
                FederationEventType.USER_ONLINE,
                user_id=user_id,
            )

    async def user_session_closed(self, user_id: str, ws_id: int) -> None:
        """A WS session for ``user_id`` just unregistered."""
        now = _utcnow()
        went_offline = False
        async with self._lock:
            sessions = self._sessions.get(user_id)
            if sessions is None:
                return
            sessions.pop(ws_id, None)
            if not sessions:
                self._sessions.pop(user_id, None)
                self._idle_users.discard(user_id)
                went_offline = True
        if not went_offline:
            return
        # Persist the offline timestamp — debounced. The event payload
        # always carries the real timestamp; only the SQLite write is
        # rate-limited.
        await self._persist_last_seen(user_id, now)
        await self._publish(UserWentOffline(user_id=user_id, last_seen_at=now))
        await self._fan_to_peers(
            FederationEventType.USER_OFFLINE,
            user_id=user_id,
            last_seen_at=now,
        )

    async def touch(self, user_id: str, ws_id: int) -> None:
        """Reset the activity timestamp for one of ``user_id``'s sessions.

        Called on every inbound WS frame from this user (text, ping,
        typing). Resumes the user from idle if applicable.
        """
        now = _utcnow()
        was_idle = False
        async with self._lock:
            sessions = self._sessions.get(user_id)
            if sessions is None or ws_id not in sessions:
                # Session was never registered (or already closed) —
                # nothing to touch. Don't auto-register: this is a
                # bookkeeping helper, not a recovery path.
                return
            sessions[ws_id] = now
            if user_id in self._idle_users:
                self._idle_users.discard(user_id)
                was_idle = True
        if was_idle:
            await self._publish(UserResumedActive(user_id=user_id))
            await self._fan_to_peers(
                FederationEventType.USER_ONLINE,
                user_id=user_id,
            )

    # ─── Background loop: idle detection ──────────────────────────────────

    async def start(self) -> None:
        """Start the idle-scanning background loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._scan_once()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("online-status scan failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.SCHEDULER_INTERVAL,
                )
            except asyncio.TimeoutError:
                continue

    async def _scan_once(self) -> None:
        """Single idle-detection pass. Exposed for tests."""
        now = _utcnow()
        cutoff = now - self.IDLE_AFTER
        newly_idle: list[tuple[str, datetime]] = []
        async with self._lock:
            for user_id, sessions in self._sessions.items():
                if not sessions:
                    continue
                last = max(sessions.values())
                if last < cutoff and user_id not in self._idle_users:
                    self._idle_users.add(user_id)
                    newly_idle.append((user_id, last))
        for user_id, last_active in newly_idle:
            await self._publish(
                UserWentIdle(user_id=user_id, last_active_at=last_active),
            )
            await self._fan_to_peers(
                FederationEventType.USER_IDLE,
                user_id=user_id,
                last_seen_at=last_active,
            )

    # ─── Internals ────────────────────────────────────────────────────────

    async def _persist_last_seen(self, user_id: str, ts: datetime) -> None:
        last = self._last_persisted.get(user_id)
        if last is not None and (ts - last) < self.PERSIST_DEBOUNCE:
            return
        self._last_persisted[user_id] = ts
        try:
            await self._user_repo.set_last_seen(user_id, ts.isoformat())
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("set_last_seen failed for %s: %s", user_id, exc)

    async def _publish(self, event: DomainEvent) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(event)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("online-status publish failed: %s", exc)

    # ─── Federation ────────────────────────────────────────────────────────

    async def _fan_to_peers(
        self,
        event_type: FederationEventType,
        *,
        user_id: str,
        last_seen_at: datetime | None = None,
    ) -> None:
        """Fan a session-presence transition to every confirmed peer.

        Cheap-and-dumb fan-out: every paired instance receives the
        transition. The on-disk pairing list bounds the cost; for the
        typical household it's a single-digit number of peers.

        Encryption-first: ``user_id`` and ``last_seen_at`` go inside
        the encrypted payload (per §25.8.21 — only routing fields
        ride in plaintext). The federation service handles the
        encryption + signature.
        """
        if self._federation is None or self._federation_repo is None:
            return
        try:
            instances = await self._federation_repo.list_instances(
                status="confirmed",
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("online-status fan-out: list_confirmed failed: %s", exc)
            return
        payload: dict = {"user_id": user_id}
        if last_seen_at is not None:
            payload["last_seen_at"] = last_seen_at.isoformat()
        for inst in instances:
            inst_id = getattr(inst, "id", None) or getattr(inst, "instance_id", None)
            if not inst_id or inst_id == self._own_instance_id:
                continue
            try:
                await self._federation.send_event(
                    to_instance_id=inst_id,
                    event_type=event_type,
                    payload=payload,
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.debug(
                    "online-status fan-out to %s failed: %s", inst_id, exc,
                )

    async def apply_remote(
        self,
        *,
        from_instance: str,
        event_type: FederationEventType,
        payload: dict,
    ) -> None:
        """Apply an inbound USER_ONLINE / USER_IDLE / USER_OFFLINE event.

        Updates the in-memory remote-state cache and republishes a local
        domain event so :class:`RealtimeService` fans the transition out
        to local viewers' WS sessions — they see the federated change in
        the same render tick a local change would land.
        """
        user_id = str(payload.get("user_id") or "").strip()
        if not user_id:
            return
        last_seen_iso = payload.get("last_seen_at")
        last_seen: datetime | None = None
        if isinstance(last_seen_iso, str):
            try:
                last_seen = datetime.fromisoformat(last_seen_iso)
            except ValueError:
                last_seen = None

        if event_type is FederationEventType.USER_OFFLINE:
            self._remote.pop(user_id, None)
            # Keep one offline marker so callers can render
            # "Last seen X" — store as a non-online entry.
            self._remote[user_id] = _RemoteEntry(
                user_id=user_id,
                instance_id=from_instance,
                is_online=False,
                is_idle=False,
                last_seen_at=last_seen,
            )
            await self._publish(
                UserWentOffline(
                    user_id=user_id,
                    last_seen_at=last_seen or _utcnow(),
                ),
            )
            return

        is_idle = event_type is FederationEventType.USER_IDLE
        prev = self._remote.get(user_id)
        self._remote[user_id] = _RemoteEntry(
            user_id=user_id,
            instance_id=from_instance,
            is_online=True,
            is_idle=is_idle,
            last_seen_at=last_seen,
        )
        if prev is None or not prev.is_online:
            await self._publish(UserCameOnline(user_id=user_id))
        elif is_idle and not prev.is_idle:
            await self._publish(
                UserWentIdle(
                    user_id=user_id,
                    last_active_at=last_seen or _utcnow(),
                ),
            )
        elif not is_idle and prev.is_idle:
            await self._publish(UserResumedActive(user_id=user_id))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
