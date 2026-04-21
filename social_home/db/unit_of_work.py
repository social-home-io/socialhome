"""Unit of Work pattern (§5.2 transactional writes).

The UoW gives services a single context manager that:

* Opens a ``BEGIN IMMEDIATE`` transaction via :meth:`AsyncDatabase.transact`,
  so all writes inside the block either all commit or all roll back.
* Buffers domain events via :meth:`UnitOfWork.publish` and flushes them
  to the bus only on commit — there is no "wrote rows but the listener
  saw a half-committed state" window.
* Optionally exposes ``uow.repos`` for the most common repos.

Usage::

    async with UnitOfWork(db, bus=bus) as uow:
        await uow.exec(\"\"\"INSERT INTO ...\"\"\", (...))
        uow.publish(SomeDomainEvent(...))

If the ``async with`` body raises, the transaction is rolled back and
**no** events fire. On a clean exit, the transaction commits and queued
events are dispatched in FIFO order.

Why a context manager and not a decorator? Decorators force the wrapped
function to do all writes; the CM lets a service compose many helper
calls inside one transaction without changing their signatures.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractAsyncContextManager
from typing import Any

from .database import AsyncDatabase

log = logging.getLogger(__name__)


class UnitOfWork(AbstractAsyncContextManager["UnitOfWork"]):
    """Single-transaction write batch + buffered event dispatch."""

    __slots__ = (
        "_db",
        "_bus",
        "_pending_events",
        "_writes",
        "_closed",
    )

    def __init__(self, db: AsyncDatabase, *, bus=None) -> None:
        self._db = db
        self._bus = bus
        self._pending_events: list = []
        # Each entry is ``(sql, params)``. They are replayed inside the
        # transact() callback so they share one ``BEGIN IMMEDIATE``.
        self._writes: list[tuple[str, tuple]] = []
        self._closed = False

    # ── Buffer writes + events ──────────────────────────────────────────

    async def exec(self, sql: str, params: tuple = ()) -> None:
        """Buffer a write to run on commit. Reads still go through the DB."""
        if self._closed:
            raise RuntimeError("UnitOfWork is closed")
        self._writes.append((sql, params))

    def publish(self, event: Any) -> None:
        """Buffer a domain event for after-commit dispatch."""
        if self._closed:
            raise RuntimeError("UnitOfWork is closed")
        self._pending_events.append(event)

    # ── Context manager ────────────────────────────────────────────────

    async def __aenter__(self) -> "UnitOfWork":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._closed = True
        if exc_type is not None:
            # Body raised → drop both buffers, no commit, no events.
            self._writes.clear()
            self._pending_events.clear()
            return None

        # Replay buffered writes inside ONE transaction so the whole
        # batch is atomic. Empty batch is a no-op — we still publish
        # any read-only events the caller buffered.
        if self._writes:
            writes = list(self._writes)

            def _run(conn):
                for sql, params in writes:
                    conn.execute(sql, params)

            await self._db.transact(_run)
            self._writes.clear()

        # On-commit event dispatch. Failures are logged but do NOT undo
        # the committed write batch — that would be worse than a missed
        # event. Each handler error is isolated.
        if self._bus is not None and self._pending_events:
            for event in self._pending_events:
                try:
                    coro = self._bus.publish(event)
                    if asyncio.iscoroutine(coro):
                        await coro
                except Exception:
                    log.exception(
                        "UnitOfWork: event dispatch failed for %r",
                        event,
                    )
        self._pending_events.clear()
        return None
