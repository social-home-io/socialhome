"""Unit tests for :class:`socialhome.routes.base.BaseView` helpers."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import make_mocked_request

from socialhome.auth import USER_KEY
from socialhome.routes.base import BaseView


def test_require_user_passes_once_the_middleware_attached_a_user():
    request = make_mocked_request("GET", "/api/themes")
    request[USER_KEY] = object()
    BaseView(request).require_user()


def test_require_user_raises_on_a_request_the_middleware_left_anonymous():
    request = make_mocked_request("GET", "/api/themes")
    with pytest.raises(RuntimeError, match="auth middleware"):
        BaseView(request).require_user()
