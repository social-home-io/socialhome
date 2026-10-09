"""The space-row tombstone value type (migration 0085)."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.tombstone import SpaceRowTombstone


def test_a_tombstone_names_identity_only_and_is_frozen():
    t = SpaceRowTombstone(
        id="st-1", owner="u-a", created_at="c", deleted_at="d", deleted_by="u-b"
    )
    assert {f.name for f in dataclasses.fields(t)} == {
        "id",
        "owner",
        "created_at",
        "deleted_at",
        "deleted_by",
        "parent_id",
    }
    assert t.parent_id == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.id = "x"  # type: ignore[misc]
