"""Tests for :mod:`socialhome.services.inbound_media_store`."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from socialhome.services import inbound_media_store as store


@pytest.mark.parametrize(
    "name",
    ["0f3a9c.webp", "a1b2-c3d4", "m-1.part00001", "blob:instance", "x_y.webm"],
)
def test_safe_names_accepted(name):
    assert store.is_safe_media_name(name)


@pytest.mark.parametrize(
    "name",
    ["", "../x", "a/b", "a\\b", ".hidden", "..", "a\x00b", "a b", "a" * 201, "é.webp"],
)
def test_unsafe_names_rejected(name):
    assert not store.is_safe_media_name(name)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("api/media/x.webp", "x.webp"),
        ("/api/media/x.webp?sig=abc", "x.webp"),
        ("x.webp", "x.webp"),
        (None, ""),
        ("", ""),
    ],
)
def test_media_basename(url, expected):
    assert store.media_basename(url) == expected


def test_partial_key_binds_sender():
    a = store.partial_key("peer-a", "tx")
    assert a == store.partial_key("peer-a", "tx")
    assert a != store.partial_key("peer-b", "tx")
    assert a != store.partial_key("peer-a", "tx2")
    assert store.is_safe_media_name(a)


@pytest.mark.parametrize(
    ("index", "count", "expected"),
    [
        (0, 1, (0, 1)),
        ("3", "4", (3, 4)),
        (4, 4, None),
        (-1, 4, None),
        (0, 0, None),
        (0, store.MAX_MEDIA_CHUNKS + 1, None),
        ("x", 1, None),
        (None, 1, None),
    ],
)
def test_parse_chunk_meta(index, count, expected):
    assert store.parse_chunk_meta(index, count) == expected


async def test_publish_once_moves_into_empty_slot(tmp_path):
    tmp = tmp_path / "tmp"
    tmp.write_bytes(b"new")
    assert await store.publish_once(tmp, tmp_path / "target")
    assert (tmp_path / "target").read_bytes() == b"new"
    assert not tmp.exists()


async def test_publish_once_never_replaces(tmp_path):
    tmp = tmp_path / "tmp"
    tmp.write_bytes(b"new")
    (tmp_path / "target").write_bytes(b"old")
    assert not await store.publish_once(tmp, tmp_path / "target")
    assert (tmp_path / "target").read_bytes() == b"old"
    assert not tmp.exists()


async def test_publish_once_without_hard_links(tmp_path):
    """Filesystems that refuse hard links fall back to check-then-replace."""
    tmp = tmp_path / "tmp"
    tmp.write_bytes(b"new")
    with patch.object(store.aiofiles.os, "link", side_effect=PermissionError):
        assert await store.publish_once(tmp, tmp_path / "target")
        assert (tmp_path / "target").read_bytes() == b"new"
        tmp.write_bytes(b"newer")
        assert not await store.publish_once(tmp, tmp_path / "target")
    assert (tmp_path / "target").read_bytes() == b"new"
    assert not tmp.exists()


async def test_remove_quietly_tolerates_missing(tmp_path):
    await store.remove_quietly(tmp_path / "nope")


async def test_note_existing_levels(tmp_path, caplog):
    target = tmp_path / "f"
    target.write_bytes(b"1234")
    with caplog.at_level(logging.DEBUG, logger=store.__name__):
        await store.note_existing(
            target, incoming_size=4, what="X", from_instance="peer"
        )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=store.__name__):
        await store.note_existing(
            target, incoming_size=5, what="X", from_instance="peer"
        )
        await store.note_existing(
            tmp_path / "missing", incoming_size=5, what="X", from_instance="peer"
        )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "not replacing" in warnings[0].getMessage()
