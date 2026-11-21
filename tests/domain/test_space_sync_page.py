"""``domain/space_sync_page.py`` — the stored page row."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.space_sync_page import StoredSyncPage


def test_row_is_frozen_with_an_optional_delta():
    row = StoredSyncPage("sp", "{}", b"\x00" * 8192)
    assert row.delta is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.page_json = "x"  # type: ignore[misc]
