"""The owner household keeps its ownership statement fresh (migration 0093).

An owner statement (:mod:`socialhome.space_owner_statement`) is taken only
while it is younger than ``MAX_STATEMENT_AGE_MS`` and names the authority key
and epoch a member pins — so an admin revoked by a rotation, still holding
the OLD key and the owner's statement for it, stops counting. For the honest
case to keep working the owner must hand members a fresh statement well
inside that window and after every rotation: members relay the newest in
the snapshots they send (a delegated admin's invite), and a member's
inviting-admin trust lapses when the statement it holds goes stale.

:meth:`OwnerStatementRefreshService.refresh` re-signs when the statement
this household last broadcast for a space it OWNS is missing, older than
``STATEMENT_REFRESH_MS``, or names another key / epoch than the space pins
now (a rotation). It stores the new one, raises ``config_sequence`` and
publishes :class:`SpaceConfigChanged` — :class:`SpaceConfigOutbound` then
broadcasts the config, which carries the fresh statement. The background
loop follows the ``asyncio.Event`` scheduler lifecycle
(``infrastructure/replay_cache_scheduler.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from ..domain.events import SpaceConfigChanged
from ..domain.space import SpaceConfigEventType, SpaceType
from ..infrastructure.event_bus import EventBus
from ..repositories.space_repo import AbstractSpaceRepo
from ..space_owner_statement import STATEMENT_REFRESH_MS, sign_owner_statement

log = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class OwnerStatementRefreshService:
    """Re-signs and broadcasts the owner statements of spaces we own."""

    __slots__ = (
        "_spaces",
        "_bus",
        "_own_instance_id",
        "_own_signing_key",
        "_interval",
        "_task",
        "_stop",
    )

    def __init__(
        self,
        *,
        space_repo: AbstractSpaceRepo,
        bus: EventBus,
        interval_seconds: float = 6 * 3600.0,
    ) -> None:
        self._spaces = space_repo
        self._bus = bus
        self._own_instance_id = ""
        self._own_signing_key = b""
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def attach_identity(self, *, own_instance_id: str, own_signing_key: bytes) -> None:
        self._own_instance_id = own_instance_id
        self._own_signing_key = own_signing_key

    async def start(self) -> None:
        """Start the background loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop and wait for the task to exit."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                refreshed = await self.refresh_all()
                if refreshed:
                    log.info("owner statements refreshed for %d space(s)", refreshed)
            except Exception:  # pragma: no cover — defensive
                log.exception("owner statement refresh failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    async def refresh_all(self) -> int:
        """Refresh every space we own that is due; how many were refreshed."""
        if not self._own_instance_id:
            return 0
        done = 0
        for space_type in SpaceType:
            for space in await self._spaces.list_by_type(space_type):
                if space.owner_instance_id != self._own_instance_id:
                    continue
                if await self.refresh(space.id):
                    done += 1
        return done

    async def refresh(self, space_id: str, *, now_ms: int | None = None) -> bool:
        """Re-sign and broadcast the statement for *space_id* when due."""
        if not self._own_instance_id or len(self._own_signing_key) != 32:
            return False
        space = await self._spaces.get(space_id)
        if (
            space is None
            or space.dissolved
            or space.owner_instance_id != self._own_instance_id
        ):
            return False
        now = _now_ms() if now_ms is None else now_ms
        if not self._due(space, now):
            return False
        statement = sign_owner_statement(
            space_id=space_id,
            owner_seed=self._own_signing_key,
            authority_pk_hex=space.identity_public_key,
            authority_key_epoch=space.authority_key_epoch,
            issued_at_ms=now,
            now_ms=now,
        )
        await self._spaces.set_owner_statement(
            space_id, json.dumps(statement, separators=(",", ":"), sort_keys=True)
        )
        sequence = await self._spaces.increment_config_sequence(space_id)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.OWNER_STATEMENT_REFRESHED.value,
                payload={},
                sequence=sequence,
            )
        )
        return True

    @staticmethod
    def _due(space, now: int) -> bool:
        if not space.owner_statement:
            return True
        try:
            held = json.loads(space.owner_statement)
            same_key = (
                str(held["authority_pk"]).lower()
                == str(space.identity_public_key or "").lower()
            )
            same_epoch = int(held["authority_key_epoch"]) == space.authority_key_epoch
            age = now - int(held["issued_at"])
        except ValueError, TypeError, KeyError:
            return True
        return not (same_key and same_epoch) or age >= STATEMENT_REFRESH_MS
