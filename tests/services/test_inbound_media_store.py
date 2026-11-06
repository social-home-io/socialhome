"""Tests for :mod:`socialhome.services.inbound_media_store`."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

from socialhome.domain.errors import InvalidMediaRefError
from socialhome.domain.media_constraints import (
    AUDIO_MAX_UPLOAD_BYTES,
    FILE_MAX_UPLOAD_BYTES,
    IMAGE_MAX_UPLOAD_BYTES,
    VIDEO_MAX_UPLOAD_BYTES,
)
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


def test_partial_key_binds_sender_transfer_and_chunk_count():
    a = store.partial_key("peer-a", "tx", chunk_count=2)
    assert a == store.partial_key("peer-a", "tx", chunk_count=2)
    assert a != store.partial_key("peer-b", "tx", chunk_count=2)
    assert a != store.partial_key("peer-a", "tx2", chunk_count=2)
    # A retry chunked for another tier never shares a directory.
    assert a != store.partial_key("peer-a", "tx", chunk_count=5)
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("api/media/0f3a9c.webp", "api/media/0f3a9c.webp"),
        ("/api/media/0f3a9c.webp", "api/media/0f3a9c.webp"),
        ("api/media/0f3a9c.webp?exp=1&sig=x", "api/media/0f3a9c.webp"),
        ("0f3a9c.webp", None),
        ("https://example.org/api/media/x.webp", None),
        ("api/media/../secret", None),
        ("api/media/a/b.webp", None),
        ("api/media/.hidden", None),
        ("api/media/", None),
        ("api/other/x.webp", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_local_media_ref(value, expected):
    assert store.local_media_ref(value) == expected


def test_local_media_refs_keeps_only_canonical_strings_capped():
    raw = [
        "api/media/a.webp",
        "https://x/y.webp",
        7,
        "/api/media/b.webp",
        "api/media/c",
    ]
    assert store.local_media_refs(raw, limit=2) == (
        "api/media/a.webp",
        "api/media/b.webp",
    )
    assert store.local_media_refs("api/media/a.webp", limit=4) == ()
    assert store.local_media_refs(None, limit=4) == ()


@pytest.mark.parametrize("name", ["m-1.preview.webp", "abc123.webp", "m:1.bin"])
def test_media_file_path_accepts_a_child_name(tmp_path, name):
    assert store.media_file_path(tmp_path, name) == tmp_path / name


@pytest.mark.parametrize(
    "name",
    [
        "../x",
        "../x.preview.webp",
        "sub/x",
        "/etc/passwd",
        ".hidden",
        "",
        "..",
        "a\x00b",
    ],
)
def test_media_file_path_refuses_escapes(tmp_path, name):
    """F6: a peer-supplied name never resolves outside ``media_dir``."""
    assert store.media_file_path(tmp_path, name) is None


def test_media_file_path_refuses_absolute_path_under_another_root(tmp_path):
    outside = str(tmp_path.parent / "outside.preview.webp")
    assert store.media_file_path(tmp_path, outside) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("api/media/0f3a9c.webp", "api/media/0f3a9c.webp"),
        ("/api/media/0f3a9c.webp", "/api/media/0f3a9c.webp"),
        ("https://tracker.example/pixel.png", None),
        ("//tracker.example/pixel.png", None),
        ("javascript:alert(1)", None),
        ("api/media/../secret", None),
        ("", None),
        (None, None),
        (7, None),
    ],
)
def test_verbatim_local_media_ref(value, expected):
    """F7: a peer value is kept byte-for-byte when it is a local media
    reference (so hashes / signatures over it still match), else ``None``."""
    assert store.verbatim_local_media_ref(value) == expected


@pytest.mark.parametrize(
    "value", ["api/media/0f3a9c.webp", "/api/media/0f3a9c.webp", "api/media/a.webp?x=1"]
)
def test_require_local_media_ref_returns_value_verbatim(value):
    """The local-write twin of :func:`verbatim_local_media_ref`."""
    assert store.require_local_media_ref(value) == value


@pytest.mark.parametrize(
    "value", ["https://tracker.example/pixel.png", "javascript:alert(1)", ""]
)
def test_require_local_media_ref_refuses_with_field_name(value):
    with pytest.raises(InvalidMediaRefError) as ei:
        store.require_local_media_ref(value, field="cover_url")
    assert ei.value.code == "INVALID_MEDIA_URL"
    assert ei.value.detail.startswith("cover_url ")


# ── store_chunk: whichever chunk completes the set assembles ───────────


async def _put(part_dir, target, idx, chunks, *, size=None):
    return await store.store_chunk(
        part_dir,
        chunk_index=idx,
        chunk_count=len(chunks),
        data=chunks[idx],
        target=target,
        expected_size=sum(map(len, chunks)) if size is None else size,
    )


@pytest.mark.parametrize("order", [(2, 0, 1), (0, 2, 1), (1, 2, 0), (0, 1, 2)])
async def test_store_chunk_assembles_whichever_chunk_lands_last(tmp_path, order):
    part_dir = tmp_path / ".partial" / "k"
    target = tmp_path / "out.bin"
    chunks = [b"AAA", b"BB", b"C"]
    results = [await _put(part_dir, target, i, chunks) for i in order]
    assert results == [None, None, True]
    assert target.read_bytes() == b"AAABBC"
    assert not part_dir.exists()


async def test_store_chunk_never_replaces_an_existing_target(tmp_path):
    part_dir = tmp_path / ".partial" / "k"
    target = tmp_path / "out.bin"
    target.write_bytes(b"ORIGINAL")
    chunks = [b"A", b"B"]
    assert await _put(part_dir, target, 0, chunks) is False
    assert target.read_bytes() == b"ORIGINAL"
    assert not part_dir.exists()


async def test_store_chunk_late_duplicate_leaves_no_part(tmp_path):
    part_dir = tmp_path / ".partial" / "k"
    target = tmp_path / "out.bin"
    chunks = [b"A", b"B"]
    for idx in (1, 0):
        await _put(part_dir, target, idx, chunks)
    assert await _put(part_dir, target, 1, chunks) is False
    assert target.read_bytes() == b"AB"
    assert not part_dir.exists()


async def test_store_chunk_size_mismatch_publishes_nothing(tmp_path, caplog):
    """Bytes that don't add up to the declared size (stale parts from
    another attempt) never become the write-once file."""
    part_dir = tmp_path / ".partial" / "k"
    target = tmp_path / "out.bin"
    chunks = [b"A", b"B"]
    await _put(part_dir, target, 0, chunks, size=3)
    with caplog.at_level(logging.WARNING):
        assert await _put(part_dir, target, 1, chunks, size=3) is None
    assert not target.exists()
    assert not part_dir.exists()
    assert "sender declared 3" in caplog.text


async def test_store_chunk_undeclared_size_is_not_checked(tmp_path):
    part_dir = tmp_path / ".partial" / "k"
    target = tmp_path / "out.bin"
    chunks = [b"A", b"B"]
    for idx in (1, 0):
        await _put(part_dir, target, idx, chunks, size=-1)
    assert target.read_bytes() == b"AB"


async def test_store_chunk_concurrent_completions_publish_exactly_once(tmp_path):
    """Force two chunks to both see a complete set: each assembles into a
    temp file of its own and exactly one publishes; bytes never mix."""
    part_dir = tmp_path / ".partial" / "k"
    target = tmp_path / "out.bin"
    chunks = [bytes([i]) * 65536 for i in range(4)]
    for idx in (0, 1):
        await _put(part_dir, target, idx, chunks)
    barrier = asyncio.Barrier(2)
    real = store.present_parts

    async def both_complete(d):
        await asyncio.wait_for(barrier.wait(), 5)
        return await real(d)

    with patch.object(store, "present_parts", both_complete):
        results = await asyncio.gather(
            _put(part_dir, target, 2, chunks), _put(part_dir, target, 3, chunks)
        )
    assert sorted(results) == [False, True]
    assert target.read_bytes() == b"".join(chunks)
    assert not part_dir.exists()


async def test_store_chunk_part_write_is_atomic(tmp_path):
    """A part is renamed into place: a reader never sees it half-written,
    so an assembly racing a re-sent part never reads a truncated one."""
    part_dir = tmp_path / ".partial" / "k"
    target = tmp_path / "out.bin"
    chunks = [b"X" * 1000, b"Y"]
    seen: list[int] = []
    real_open = store.aiofiles.open

    def spying_open(path, mode="r", *a, **kw):
        if mode == "wb" and path.name == "000000":
            seen.append(-1)  # a direct, truncating write of the part
        return real_open(path, mode, *a, **kw)

    with patch.object(store.aiofiles, "open", spying_open):
        await _put(part_dir, target, 0, chunks)
    assert seen == []
    assert (part_dir / "000000").read_bytes() == chunks[0]


async def test_store_chunk_readiness_check_is_linear(tmp_path):
    """Each chunk checks readiness with one directory listing — no
    per-part probe per chunk (that is quadratic: 141 567 probes and 14 s
    for 512 chunks)."""
    n = 64
    part_dir = tmp_path / ".partial" / "k"
    target = tmp_path / "out.bin"
    chunks = [bytes([i % 256]) * 8 for i in range(n)]
    calls = {"probe": 0}

    def counting(fn):
        async def wrapper(*a, **kw):
            calls["probe"] += 1
            return await fn(*a, **kw)

        return wrapper

    os_mod = store.aiofiles.os
    with (
        patch.object(os_mod.path, "isfile", counting(os_mod.path.isfile)),
        patch.object(os_mod.path, "exists", counting(os_mod.path.exists)),
        patch.object(os_mod, "listdir", counting(os_mod.listdir)),
        patch.object(os_mod, "stat", counting(os_mod.stat)),
    ):
        for idx in range(n):
            await _put(part_dir, target, idx, chunks)
    assert target.read_bytes() == b"".join(chunks)
    assert calls["probe"] <= 3 * n


# ── One namespace: DM names, reserved names ─────────────────────────────


def test_dm_media_name_binds_the_sending_household_and_message():
    name = store.dm_media_name("peer-a", "m-1", ".webp")
    assert name.startswith(store.DM_MEDIA_PREFIX) and name.endswith(".webp")
    assert store.is_safe_media_name(name)
    assert name == store.dm_media_name("peer-a", "m-1", ".webp")
    assert name != store.dm_media_name("peer-b", "m-1", ".webp")
    assert name != store.dm_media_name("peer-a", "m-2", ".webp")
    # Fixed length whatever the id: always one safe component.
    long_id = "m" * 200
    assert store.is_safe_media_name(store.dm_media_name("p", long_id, ".webp"))


@pytest.mark.parametrize(
    ("name", "reserved"),
    [
        ("dm-" + "0" * 40 + ".webp", True),
        ("DM-x.webp", True),
        ("gm-" + "a" * 40 + ".webm", True),
        ("m-1.preview.webp", True),
        ("m-1.part0003", True),
        ("x.assembled", True),
        ("0123abcd" * 4 + ".webp", False),
        ("a1.webp", False),
        ("admin.webp", False),
    ],
)
def test_is_reserved_media_name(name, reserved):
    assert store.is_reserved_media_name(name) is reserved


# ── Size caps ──────────────────────────────────────────────────────────


def test_caps_come_from_the_upload_limits():
    assert store.SPACE_MEDIA_MAX_BYTES == VIDEO_MAX_UPLOAD_BYTES
    assert store.dm_media_max_bytes("video/webm") == VIDEO_MAX_UPLOAD_BYTES
    assert store.dm_media_max_bytes("") == VIDEO_MAX_UPLOAD_BYTES
    small = max(IMAGE_MAX_UPLOAD_BYTES, AUDIO_MAX_UPLOAD_BYTES, FILE_MAX_UPLOAD_BYTES)
    assert store.dm_media_max_bytes("image/webp") == small
    assert store.dm_media_max_bytes("Audio/OGG; codecs=opus") == small


@pytest.mark.parametrize(
    ("total", "chunk", "count", "refused"),
    [
        (100, 100, 1, False),  # one chunk = the file
        (1000, 400, 3, False),  # 400 + 400 + 200
        (1000, 200, 3, False),  # the short last chunk
        (1000, 500, 3, True),  # 2 x 500 already the whole file
        (100, 101, 1, True),  # chunk over the file
        (5000, 1, 4096, False),
        (5001, 1, 1, True),  # over the cap
    ],
)
def test_inbound_size_refusal(total, chunk, count, refused):
    reason = store.inbound_size_refusal(
        total_size=total, chunk_len=chunk, chunk_count=count, max_bytes=5000
    )
    assert (reason is not None) is refused


# ── Symlinks are never followed ────────────────────────────────────────


def _outside(tmp_path):
    out = tmp_path / "outside"
    out.mkdir()
    (out / "precious.txt").write_bytes(b"keep")
    return out


async def test_drop_parts_unlinks_a_symlinked_part_dir_without_following(tmp_path):
    out = _outside(tmp_path)
    link = tmp_path / ".partial" / "k"
    link.parent.mkdir()
    link.symlink_to(out, target_is_directory=True)
    await store.drop_parts(link)
    assert not link.is_symlink()
    assert (out / "precious.txt").read_bytes() == b"keep"


async def test_drop_parts_never_descends_into_a_symlink_inside(tmp_path):
    out = _outside(tmp_path)
    part_dir = tmp_path / ".partial" / "k"
    part_dir.mkdir(parents=True)
    (part_dir / "000000").write_bytes(b"A")
    (part_dir / "000001").symlink_to(out, target_is_directory=True)
    await store.drop_parts(part_dir)
    assert not part_dir.exists()
    assert (out / "precious.txt").read_bytes() == b"keep"


async def test_store_chunk_refuses_a_symlinked_part_dir(tmp_path):
    out = _outside(tmp_path)
    link = tmp_path / ".partial" / "k"
    link.parent.mkdir()
    link.symlink_to(out, target_is_directory=True)
    with pytest.raises(OSError):
        await _put(link, tmp_path / "out.bin", 0, [b"A", b"B"])
    assert sorted(p.name for p in out.iterdir()) == ["precious.txt"]
