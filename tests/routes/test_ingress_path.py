"""Unit tests for :mod:`socialhome.routes.ingress_path`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from socialhome.app_keys import platform_adapter_key
from socialhome.platform.adapter import Capability
from socialhome.routes.ingress_path import INGRESS_PATH_RE, trusted_ingress_path


def _request(caps: frozenset[Capability] | None, header: str | None) -> web.Request:
    app = web.Application()
    if caps is not None:
        app[platform_adapter_key] = SimpleNamespace(capabilities=caps)  # type: ignore[assignment]
    headers = {"X-Ingress-Path": header} if header is not None else {}
    return make_mocked_request("GET", "/", headers=headers, app=app)


_INGRESS = frozenset({Capability.INGRESS})


def test_regex_rejects_trailing_newline():
    """``fullmatch`` keeps the regex from trusting a trailing ``\\n``
    (``$`` alone would match before it)."""
    assert INGRESS_PATH_RE.fullmatch("/api/hassio_ingress/abc\n") is None
    assert INGRESS_PATH_RE.fullmatch("/api/hassio_ingress/abc/")


def test_ingress_valid_header_is_trusted_without_trailing_slash():
    req = _request(_INGRESS, "/api/hassio_ingress/Xy-9_kQ2/")
    assert trusted_ingress_path(req) == "/api/hassio_ingress/Xy-9_kQ2"


def test_without_ingress_capability_header_is_ignored():
    req = _request(frozenset(), "/api/hassio_ingress/abc")
    assert trusted_ingress_path(req) == ""


def test_without_adapter_header_is_ignored():
    req = _request(None, "/api/hassio_ingress/abc")
    assert trusted_ingress_path(req) == ""


@pytest.mark.parametrize(
    "header",
    [
        "",
        "/evil",
        "/api/hassio_ingress/",
        "/api/hassio_ingress/a;b",
        "/api/hassio_ingress/a/b",
    ],
)
def test_ingress_invalid_header_is_ignored(header):
    assert trusted_ingress_path(_request(_INGRESS, header)) == ""


def test_ingress_missing_header():
    assert trusted_ingress_path(_request(_INGRESS, None)) == ""
