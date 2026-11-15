"""Unit tests for :class:`socialhome.routes.base.BaseView` helpers."""

from __future__ import annotations

import orjson
import pytest
from aiohttp.test_utils import make_mocked_request

from socialhome.auth import USER_KEY
from socialhome.domain.errors import NotFoundError
from socialhome.routes.base import BaseView


def test_require_user_passes_once_the_middleware_attached_a_user():
    request = make_mocked_request("GET", "/api/themes")
    request[USER_KEY] = object()
    BaseView(request).require_user()


def test_require_user_raises_on_a_request_the_middleware_left_anonymous():
    request = make_mocked_request("GET", "/api/themes")
    with pytest.raises(RuntimeError, match="auth middleware"):
        BaseView(request).require_user()


class _BareKeyErrorView(BaseView):
    async def get(self):
        raise KeyError("x")


class _NotFoundView(BaseView):
    async def get(self):
        raise NotFoundError("space post 'p1' not found")


async def _error_of(view_cls) -> tuple[int, dict]:
    resp = await view_cls(make_mocked_request("GET", "/api/x"))
    return resp.status, orjson.loads(resp.body)["error"]


async def test_a_bare_key_error_is_still_a_logged_404(caplog):
    """A bare ``KeyError`` reaching the view is a programming error (a
    missing dict key): still answered 404 with the fixed detail, never
    the key, and still logged at WARNING so it gets fixed."""
    caplog.set_level("WARNING", logger="socialhome.routes.base")
    assert await _error_of(_BareKeyErrorView) == (
        404,
        {"code": "NOT_FOUND", "detail": "Resource not found."},
    )
    assert "_BareKeyErrorView: KeyError surfaced from handler: 'x'" in caplog.text


async def test_not_found_is_the_same_404_without_a_log_line(caplog):
    """A missing resource (``NotFoundError``) is the same 404 body, and
    quiet."""
    caplog.set_level("DEBUG", logger="socialhome.routes.base")
    assert await _error_of(_NotFoundView) == (
        404,
        {"code": "NOT_FOUND", "detail": "Resource not found."},
    )
    assert caplog.text == ""
