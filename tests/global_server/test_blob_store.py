"""GFS shared blob handling — the padded-bucket / SHA-256 address checks and
the file store both encrypted-media stores (moments, space media) use."""

from __future__ import annotations

import asyncio
import os

import pytest

from socialhome.global_server import blob_store as bs
from socialhome.moment_public_media import MAX_BLOB_BYTES, PAD_BUCKETS, blob_id_of


def _padded(bucket: int = PAD_BUCKETS[1]) -> bytes:
    return os.urandom(bucket + bs.BLOB_OVERHEAD)


def test_bucket_sizes_are_the_padded_buckets_plus_nonce_and_tag():
    assert bs.BLOB_OVERHEAD == 12 + 16
    assert {b + 28 for b in PAD_BUCKETS} == bs.BUCKET_SIZES
    assert max(bs.BUCKET_SIZES) == MAX_BLOB_BYTES


def test_check_shape_accepts_a_padded_blob():
    data = _padded()
    bs.check_shape(blob_id_of(data), data)


@pytest.mark.parametrize(
    ("blob_id", "data", "reason"),
    [
        ("not-hex", _padded(), "bad_blob_id"),
        ("A" * 64, _padded(), "bad_blob_id"),
        ("a" * 64, os.urandom(1000), "not_a_padded_blob"),
        ("a" * 64, b"\0" * (MAX_BLOB_BYTES + 1), "not_a_padded_blob"),
    ],
    ids=["not-hex", "upper-case", "no-bucket", "too-large"],
)
def test_check_shape_refuses_with_422(blob_id, data, reason):
    with pytest.raises(bs.MediaRefused) as exc:
        bs.check_shape(blob_id, data)
    assert (exc.value.status, exc.value.reason) == (422, reason)


async def test_check_address_needs_the_sha256_of_the_bytes():
    data = _padded()
    await bs.check_address(blob_id_of(data), data)
    with pytest.raises(bs.MediaRefused) as exc:
        await bs.check_address(blob_id_of(b"other"), data)
    assert (exc.value.status, exc.value.reason) == (422, "blob_id_mismatch")


async def test_fs_blob_files_write_read_remove(tmp_path):
    files: bs.BlobFiles = bs.FsBlobFiles(tmp_path / "x")
    data = _padded()
    bid = blob_id_of(data)
    assert await files.read(bid) is None
    await files.write(bid, data)
    assert await files.read(bid) == data
    # No temporary file is left behind.
    assert sorted(p.name for p in (tmp_path / "x").iterdir()) == [bid]
    await files.remove(bid)
    assert await files.read(bid) is None
    # Removing what is gone is not an error.
    await files.remove(bid)


async def test_fs_blob_files_concurrent_writes_of_one_blob_stay_whole(tmp_path):
    """Two cluster nodes may take the same upload at once: each writes its
    own temporary file, so neither truncates the other's half-written one."""
    files = bs.FsBlobFiles(tmp_path)
    data = _padded(PAD_BUCKETS[3])
    bid = blob_id_of(data)
    await asyncio.gather(*(files.write(bid, data) for _ in range(4)))
    assert await files.read(bid) == data
    assert [p.name for p in tmp_path.iterdir()] == [bid]
