"""Tests for socialhome.domain.page."""

from __future__ import annotations

from socialhome.domain.page import PageTombstone, page_tombstone_to_wire_dict


def test_a_page_tombstone_wires_as_the_delete_payload_plus_its_creator():
    tomb = PageTombstone(
        id="pg1", deleted_at="2026-06-01 10:00:00", created_by="u-c", deleted_by="u-d"
    )
    assert page_tombstone_to_wire_dict(tomb, "sp-1") == {
        "id": "pg1",
        "page_id": "pg1",
        "space_id": "sp-1",
        "created_by": "u-c",
        "actor_user_id": "u-d",
    }


def test_a_tombstone_without_a_deleter_names_nobody():
    tomb = PageTombstone(id="pg1", deleted_at="2026-06-01 10:00:00")
    wire = page_tombstone_to_wire_dict(tomb, "sp-1")
    assert (wire["created_by"], wire["actor_user_id"]) == ("", "")
