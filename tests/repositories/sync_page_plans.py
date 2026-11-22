"""Shared helpers for the §25.6 sync-page plan tests: capture the queries a
repo read issues and return their ``EXPLAIN QUERY PLAN``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


async def query_plans(db: Any, fetch: Callable[[], Awaitable[Any]]) -> list[str]:
    """The ``EXPLAIN QUERY PLAN`` (steps joined by `` | ``) of every query
    ``fetch`` reads through ``db.fetchall``."""
    seen: list[tuple[str, tuple]] = []
    real = db.fetchall

    async def spy(sql, params=()):
        seen.append((sql, tuple(params)))
        return await real(sql, params)

    db.fetchall = spy
    try:
        await fetch()
    finally:
        db.fetchall = real
    assert seen, "the read issued no query"
    return [
        " | ".join(r[3] for r in await real("EXPLAIN QUERY PLAN " + sql, params))
        for sql, params in seen
    ]


async def set_statistics(db: Any, *, on: bool) -> None:
    """Gather planner statistics (``PRAGMA optimize``) or drop them all."""
    if on:
        await db.optimize()
    else:
        await db.transact(
            lambda conn: conn.execute("DROP TABLE IF EXISTS sqlite_stat1")
        )
