"""``receive_stickies``: stickies by §25.6 sync."""

from __future__ import annotations

import logging

from socialhome.domain.sticky import DEFAULT_STICKY_COLOR, MAX_STICKY_CONTENT_LENGTH
from socialhome.federation.sync.space.receive_stickies import _sticky_from_record
from tests.federation.sync.space.receiver_fakes import _send


def test_sticky_from_record_never_keeps_a_non_hex_color():
    s = _sticky_from_record(
        {
            "id": "st-1",
            "author": "u",
            "content": "x",
            "color": "url(https://evil.example/t.png)",
        },
        "sp-1",
    )
    assert s is not None and s.color == DEFAULT_STICKY_COLOR
    s = _sticky_from_record({"id": "st-2", "author": "u", "content": "x"}, "sp-1")
    assert s is not None and s.color == DEFAULT_STICKY_COLOR


def test_sticky_from_record_keeps_valid_hex_canonical():
    s = _sticky_from_record(
        {"id": "st-1", "author": "u", "content": "x", "color": "#abc"}, "sp-1"
    )
    assert s is not None and s.color == "#AABBCC"


def test_sticky_from_record_clamps_coords_and_survives_overflow():
    s = _sticky_from_record(
        {
            "id": "st-1",
            "author": "u",
            "content": "x",
            "position_x": 10**400,
            "position_y": -3.0,
        },
        "sp-1",
    )
    assert s is not None and (s.position_x, s.position_y) == (0.0, 0.0)


def test_sticky_from_record_truncates_content_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        s = _sticky_from_record(
            {"id": "st-1", "author": "u", "content": "\u202e" + "y" * 3000},
            "sp-1",
        )
    assert s is not None and s.content == "y" * MAX_STICKY_CONTENT_LENGTH
    assert any("truncat" in r.message for r in caplog.records)


def test_sticky_from_record_invisible_content_is_dropped():
    assert (
        _sticky_from_record(
            {"id": "st-1", "author": "u", "content": "\u200b\x00"}, "sp-1"
        )
        is None
    )


async def test_stickies(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "stickies",
        [
            {
                "id": "s-1",
                "author": "u-1",
                "content": "note",
                "color": "yellow",
                "position_x": 1.0,
                "position_y": 2.0,
                "created_at": "2026-04-18T00:00:00+00:00",
                "updated_at": "2026-04-18T00:00:00+00:00",
            },
        ],
    )
    assert len(c.stickies) == 1
    assert c.stickies[0].id == "s-1"
