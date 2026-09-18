"""GFS periodic maintenance sweep.

The GFS has no recurring cleanup loop of its own — only a boot-time
``purge_expired_sessions`` and the cluster heartbeat. Several retention
prunes therefore never ran on a long-lived process: expired admin
sessions piled up until reboot, expired highlight publications were never
dropped, and ``gfs_pair_tokens`` accumulated one dead row per pairing
attempt forever.

This scheduler runs every ``interval_seconds`` and best-effort runs each
prune in its own ``try``/``except`` so one failure doesn't skip the rest.
Lifecycle matches ``cluster.ClusterService`` (the ``_stop: asyncio.Event``
pattern).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .repositories import PAIR_TOKEN_RETENTION_SECONDS

if TYPE_CHECKING:
    from .repositories import (
        AbstractGfsAdminRepo,
        AbstractGfsEnvelopeQueueRepo,
        AbstractGfsHighlightPublicationRepo,
    )

log = logging.getLogger(__name__)


class GfsMaintenanceScheduler:
    """Background task that runs the dormant GFS retention prunes."""

    __slots__ = (
        "_admin_repo",
        "_highlight_repo",
        "_envelope_queue_repo",
        "_interval",
        "_task",
        "_stop",
    )

    def __init__(
        self,
        *,
        admin_repo: "AbstractGfsAdminRepo",
        highlight_repo: "AbstractGfsHighlightPublicationRepo",
        envelope_queue_repo: "AbstractGfsEnvelopeQueueRepo | None" = None,
        interval_seconds: float = 3600.0,  # once per hour
    ) -> None:
        self._admin_repo = admin_repo
        self._highlight_repo = highlight_repo
        # Optional so an existing test/embedder that builds the scheduler with
        # the two original repos keeps working; production always wires it.
        self._envelope_queue_repo = envelope_queue_repo
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

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
            await self._maintain_once()
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue

    async def _maintain_once(self) -> None:
        """Run one maintenance pass. Exposed for tests.

        Each prune is best-effort and independently guarded so a single
        failure doesn't skip the others.
        """
        now = int(time.time())
        try:
            await self._admin_repo.purge_expired_sessions(now)
        except Exception as exc:
            log.warning("gfs maintenance: purge_expired_sessions failed: %s", exc)
        highlights = 0
        try:
            highlights = await self._highlight_repo.prune_expired(now)
        except Exception as exc:
            log.warning("gfs maintenance: prune_expired highlights failed: %s", exc)
        tokens = 0
        try:
            tokens = await self._admin_repo.prune_old_pair_tokens(
                now - PAIR_TOKEN_RETENTION_SECONDS,
            )
        except Exception as exc:
            log.warning("gfs maintenance: prune_old_pair_tokens failed: %s", exc)
        envelopes = 0
        if self._envelope_queue_repo is not None:
            try:
                envelopes = await self._envelope_queue_repo.prune_expired(now)
            except Exception as exc:
                log.warning(
                    "gfs maintenance: prune_expired envelopes failed: %s",
                    exc,
                )
        if highlights or tokens or envelopes:
            log.debug(
                "gfs maintenance: pruned %d highlight publications, %d pair "
                "tokens, %d expired envelopes",
                highlights,
                tokens,
                envelopes,
            )
