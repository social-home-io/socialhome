"""Tests for the admin-set federation inbox base URL helper.

Companion to :mod:`socialhome.platform.federation_base`. The value is
what peers POST federation envelopes to, so the path handling matters:
this key carries *our own* base and gets Social Home's inbox route
appended, unlike the Home Assistant integration's key which carries HA's
URL and gets the HA-hosted forwarder path instead.
"""

from __future__ import annotations

import pytest

from socialhome.platform.federation_base import (
    INBOX_PATH,
    MANUAL_BASE_KEY,
    manual_federation_base,
    public_base_from_federation_base,
    read_manual_base,
)


class _FakeDb:
    """Minimal stand-in exposing just ``fetchone``."""

    def __init__(self, value: str | None, *, row_missing: bool = False) -> None:
        self._value = value
        self._row_missing = row_missing
        self.queried: list[tuple] = []

    async def fetchone(self, sql: str, params: tuple = ()):
        self.queried.append(params)
        if self._row_missing:
            return None
        return {"value": self._value}


async def test_read_manual_base_none_without_a_db():
    """An adapter constructed without a DB must not explode."""
    assert await read_manual_base(None) is None
    assert await manual_federation_base(None) is None


async def test_read_manual_base_reads_the_documented_key():
    db = _FakeDb("https://home.example.com")
    assert await read_manual_base(db) == "https://home.example.com"
    assert db.queried == [(MANUAL_BASE_KEY,)]


async def test_read_manual_base_none_when_row_absent():
    assert await read_manual_base(_FakeDb(None, row_missing=True)) is None


@pytest.mark.parametrize("stored", ["", "   ", None])
async def test_read_manual_base_treats_blank_as_unset(stored):
    """A blank must read as unset, or the adapter would prefer it over a
    perfectly good automatic source and resolve to nothing useful."""
    assert await read_manual_base(_FakeDb(stored)) is None


async def test_read_manual_base_strips_trailing_slash():
    assert await read_manual_base(_FakeDb("https://h.example/")) == "https://h.example"


async def test_manual_federation_base_appends_our_inbox_path():
    assert await manual_federation_base(_FakeDb("https://h.example")) == (
        f"https://h.example{INBOX_PATH}"
    )


async def test_manual_federation_base_is_idempotent():
    """An admin who pastes the full inbox URL must not get it doubled."""
    already = f"https://h.example{INBOX_PATH}"
    assert await manual_federation_base(_FakeDb(already)) == already


async def test_manual_key_is_not_the_integration_key():
    """The two carry different meanings and must not collide — sharing a
    key would let the integration's next push clobber the admin's value
    and would append the wrong path to whichever one won."""
    assert MANUAL_BASE_KEY != "ha_federation_base"


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        (f"https://h.example{INBOX_PATH}", "https://h.example"),
        (f"https://h.example{INBOX_PATH}/", "https://h.example"),
        (f"https://h.example/prefix{INBOX_PATH}", "https://h.example/prefix"),
        # HA's forwarder reaches only the inbox — not a public origin.
        ("https://ha.example/api/socialhome/inbox", None),
        (INBOX_PATH, None),
        ("", None),
        (None, None),
    ],
)
def test_public_base_from_federation_base(base, expected):
    """Only a base that is Social Home's *own* inbox yields an origin that
    external clients (calendar apps, API-token scripts) can use."""
    assert public_base_from_federation_base(base) == expected
