"""Send one file's media chunks with a bounded window.

The space and DM media outboxes ship a file as many ``*_MEDIA_BLOB``
chunks. Order is irrelevant — the receiver writes each by
``chunk_index`` — so a few go out at once to hide the per-chunk round
trip. Only ``window`` workers exist, each pulling the next chunk from a
lazy iterator (:func:`~socialhome.domain.media_chunking.split_into_chunks`),
so the sender never holds a coroutine, a task or a slice per chunk of
the whole file.

A failed chunk stops the rest: the outbox reschedules the whole row and
the next attempt resends every chunk (the receiver overwrites the parts
it already has).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable

#: One chunk's metadata dict and raw bytes.
Chunk = tuple[dict, bytes]


async def send_chunks(
    chunks: Iterable[Chunk],
    send: Callable[[dict, bytes], Awaitable[None]],
    *,
    window: int,
) -> tuple[dict, Exception] | None:
    """Await ``send(payload, raw)`` for every chunk, at most ``window`` at
    a time. ``send`` raises on a failed delivery.

    Returns ``None`` when every chunk was sent, else the first failed
    chunk's metadata and its error — no further chunks are pulled after
    a failure.
    """
    pending = iter(chunks)
    failures: list[tuple[dict, Exception]] = []

    async def _worker() -> None:
        while not failures:
            item = next(pending, None)
            if item is None:
                return
            payload, raw = item
            try:
                await send(payload, raw)
            except Exception as exc:  # noqa: BLE001 — reported to the caller
                failures.append((payload, exc))

    await asyncio.gather(*(_worker() for _ in range(max(1, window))))
    return failures[0] if failures else None
