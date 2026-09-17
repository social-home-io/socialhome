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
    def __init__(self, raw: bytes):
        self._raw = raw

    async def read(self, n: int = -1) -> bytes:
        return self._raw if n < 0 else self._raw[:n]


class _Resp:
    def __init__(self, raw: bytes, *, content_length: int | None = None):
        self.content = _Content(raw)
        self.content_length = len(raw) if content_length is None else content_length


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
