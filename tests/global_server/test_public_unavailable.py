"""Tests for the uniform public "not available" reply."""

from __future__ import annotations

import asyncio
import json

import pytest

from socialhome.global_server.public_unavailable import (
    UNAVAILABLE_BODY,
    UNAVAILABLE_STATUS,
    unavailable_at,
    unavailable_response,
)


def test_unavailable_response_shape():
    resp = unavailable_response()
    assert resp.status == UNAVAILABLE_STATUS == 503
    assert json.loads(resp.body) == UNAVAILABLE_BODY == {"error": "unavailable"}
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.content_type == "application/json"


def test_unavailable_response_is_byte_identical_each_call():
    a, b = unavailable_response(), unavailable_response()
    assert a.body == b.body
    assert dict(a.headers) == dict(b.headers)


@pytest.mark.security
async def test_unavailable_at_waits_until_deadline():
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    resp = await unavailable_at(t0 + 0.1)
    assert loop.time() - t0 >= 0.1
    assert resp.status == 503


async def test_unavailable_at_past_deadline_returns_immediately():
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    resp = await unavailable_at(t0 - 5)
    assert loop.time() - t0 < 0.05
    assert resp.body == unavailable_response().body
