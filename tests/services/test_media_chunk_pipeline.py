"""Tests for the bounded media-chunk sender."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.domain.media_chunking import split_into_chunks
from socialhome.services.media_chunk_pipeline import send_chunks


async def test_every_chunk_is_sent_with_at_most_window_in_flight():
    sent: list[int] = []
    inflight = 0
    peak = 0

    async def _send(payload: dict, raw: bytes) -> None:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0)
        inflight -= 1
        sent.append(payload["chunk_index"])

    assert await send_chunks(split_into_chunks(b"x" * 100, 1), _send, window=4) is None
    assert sorted(sent) == list(range(100))
    assert peak == 4


async def test_chunks_are_pulled_lazily():
    """A worker takes the next chunk only once it is free — the iterator
    is never drained ahead of the window."""
    pulled = 0
    pulled_at_first_send: list[int] = []

    def _chunks():
        nonlocal pulled
        for i in range(1000):
            pulled += 1
            yield {"chunk_index": i}, b"x"

    async def _send(payload: dict, raw: bytes) -> None:
        if not pulled_at_first_send:
            pulled_at_first_send.append(pulled)
        await asyncio.sleep(0)

    assert await send_chunks(_chunks(), _send, window=4) is None
    assert pulled == 1000
    assert pulled_at_first_send[0] <= 4


async def test_the_first_failure_is_returned_and_stops_the_rest():
    calls = 0

    async def _send(payload: dict, raw: bytes) -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        if payload["chunk_index"] == 1:
            raise RuntimeError("boom")

    failure = await send_chunks(split_into_chunks(b"x" * 100, 1), _send, window=2)
    assert failure is not None
    payload, exc = failure
    assert payload["chunk_index"] == 1
    assert str(exc) == "boom"
    assert calls < 100


async def test_an_empty_iterable_sends_nothing():
    async def _send(payload: dict, raw: bytes) -> None:  # pragma: no cover
        raise AssertionError("not called")

    assert await send_chunks([], _send, window=4) is None


async def test_a_cancelled_send_stops_the_other_workers():
    """Regression: a ``CancelledError`` (any ``BaseException``) out of one
    send left the other workers sending with nobody awaiting them."""
    started: list[int] = []
    finished: list[int] = []

    async def _send(payload: dict, raw: bytes) -> None:
        started.append(payload["chunk_index"])
        if payload["chunk_index"] == 0:
            await asyncio.sleep(0)
            raise asyncio.CancelledError
        await asyncio.sleep(0.05)
        finished.append(payload["chunk_index"])

    with pytest.raises(asyncio.CancelledError):
        await send_chunks(split_into_chunks(b"x" * 100, 1), _send, window=4)
    await asyncio.sleep(0.2)
    assert finished == []
    assert len(started) <= 4


async def test_cancelling_the_caller_cancels_every_worker():
    started = asyncio.Event()
    finished: list[int] = []

    async def _send(payload: dict, raw: bytes) -> None:
        started.set()
        await asyncio.sleep(0.05)
        finished.append(payload["chunk_index"])

    task = asyncio.create_task(
        send_chunks(split_into_chunks(b"x" * 100, 1), _send, window=4)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert finished == []
