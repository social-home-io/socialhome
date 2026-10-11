"""Tests for the dashboard's typed app / request keys."""

from __future__ import annotations

from aiohttp import web

from socialhome.dashboard import keys


def test_keys_are_distinct_typed_keys():
    app_keys = [
        keys.AUTH_KEY,
        keys.SETTINGS_KEY,
        keys.RENDERER_KEY,
        keys.HOUSEHOLD_NAME_KEY,
    ]
    assert all(isinstance(k, web.AppKey) for k in app_keys)
    assert isinstance(keys.CLAIMS_KEY, web.RequestKey)
    names = {str(k) for k in app_keys} | {str(keys.CLAIMS_KEY)}
    assert len(names) == 5
