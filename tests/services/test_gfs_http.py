"""Tests for socialhome.services.gfs_http."""

from __future__ import annotations

import json

import pytest

from socialhome.services.gfs_http import (
    MAX_GFS_BODY_BYTES,
    MAX_GFS_DIRECTORY_BODY_BYTES,
    MAX_GFS_DIRECTORY_ITEMS,
    read_json_capped,
)


class _Content:
    """A stream: ``read(n)`` consumes, at most ``chunk`` bytes per call —
    like aiohttp's ``StreamReader``, which returns what has arrived."""

    def __init__(self, raw: bytes, *, chunk: int | None = None):
        self._raw = raw
        self._chunk = chunk
        self.reads = 0

    async def read(self, n: int = -1) -> bytes:
        self.reads += 1
        size = len(self._raw) if n < 0 else n
        if self._chunk is not None:
            size = min(size, self._chunk)
        out, self._raw = self._raw[:size], self._raw[size:]
        return out


class _Resp:
    def __init__(
        self,
        raw: bytes,
        *,
        content_length: int | None = None,
        chunk: int | None = None,
    ):
        self.content = _Content(raw, chunk=chunk)
        self.content_length = len(raw) if content_length is None else content_length


async def test_a_body_split_over_many_chunks_is_read_whole():
    """Regression: one ``read(n)`` returns only the chunk that has arrived —
    a large listing used to be parsed half-read ("Unterminated string")."""
    body = json.dumps({"spaces": [{"icon": "x" * 5000}] * 3}).encode()
    resp = _Resp(body, chunk=1024)
    assert await read_json_capped(resp, url="u", limit=1 << 20) == json.loads(body)
    assert resp.content.reads > 1


async def test_a_chunked_body_over_the_cap_is_refused():
    resp = _Resp(b"[" + b"1," * 600 + b"1]", content_length=None, chunk=100)
    resp.content_length = None
    assert await read_json_capped(resp, url="u", limit=500) is None


async def test_parses_a_normal_body():
    resp = _Resp(json.dumps({"a": 1}).encode())
    assert await read_json_capped(resp, url="u", limit=1024) == {"a": 1}


async def test_rejects_a_body_whose_declared_length_is_over_the_cap():
    """``Content-Length`` is attacker-supplied but still worth short-
    circuiting on: an honest cap breach costs us nothing to refuse."""
    resp = _Resp(b'{"a": 1}', content_length=10_000)
    assert await read_json_capped(resp, url="u", limit=100) is None


async def test_rejects_a_body_that_lies_about_its_length():
    """A hostile GFS can under-declare (or omit) Content-Length, so the
    actual read is bounded too — ``limit + 1`` bytes, rejected when the
    extra byte materialises."""
    raw = b"[" + b"0," * 5000 + b"0]"
    resp = _Resp(raw, content_length=10)
    assert await read_json_capped(resp, url="u", limit=100) is None


async def test_rejects_a_body_with_no_declared_length_over_the_cap():
    raw = json.dumps(["x" * 500]).encode()
    resp = _Resp(raw, content_length=None)
    assert await read_json_capped(resp, url="u", limit=50) is None


async def test_rejects_unparsable_json():
    assert await read_json_capped(_Resp(b"not json"), url="u", limit=1024) is None


async def test_rejects_undecodable_bytes():
    assert await read_json_capped(_Resp(b"\xff\xfe\x00"), url="u", limit=1024) is None


async def test_a_body_exactly_at_the_cap_is_accepted():
    payload = json.dumps({"k": "v" * 80}).encode()
    resp = _Resp(payload)
    assert await read_json_capped(resp, url="u", limit=len(payload)) is not None


@pytest.mark.parametrize(
    "cap",
    [MAX_GFS_BODY_BYTES, MAX_GFS_DIRECTORY_BODY_BYTES, MAX_GFS_DIRECTORY_ITEMS],
)
def test_caps_are_positive_and_ordered(cap):
    assert cap > 0


def test_directory_cap_is_larger_than_the_single_space_cap():
    """A directory carries the same per-space payload many times over —
    including base64 icons — so it needs the more generous budget."""
    assert MAX_GFS_DIRECTORY_BODY_BYTES > MAX_GFS_BODY_BYTES
