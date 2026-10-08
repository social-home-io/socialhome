"""Tests for the uniform public "not available" reply."""

from __future__ import annotations

import json

import pytest

from socialhome.global_server import public_unavailable
from socialhome.global_server.public_unavailable import (
    UNAVAILABLE_BODY,
    UNAVAILABLE_STATUS,
    unavailable_response,
)


def test_unavailable_response_shape():
    resp = unavailable_response()
    assert resp.status == UNAVAILABLE_STATUS == 503
    assert json.loads(resp.body) == UNAVAILABLE_BODY == {"error": "unavailable"}
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.content_type == "application/json"


@pytest.mark.security
def test_unavailable_response_is_byte_identical_each_call():
    a, b = unavailable_response(), unavailable_response()
    assert a.body == b.body
    assert dict(a.headers) == dict(b.headers)


def test_no_latency_floor_helper():
    """Failures answer at once — there is deliberately no delayed variant
    (it would hide nothing and let anonymous callers pin tasks)."""
    assert not hasattr(public_unavailable, "unavailable_at")
