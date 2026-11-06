"""The reader side of the GFS space media cache
(:mod:`socialhome.services.space_media_gfs_fetcher`)."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from socialhome.domain.federation import InstanceSource
from socialhome.domain.space_media_ref import SpaceMediaRef
from socialhome.moment_public_media import blob_id_of
from socialhome.services import space_media_gfs_fetcher as fetch_mod
from socialhome.services.space_media_gfs_fetcher import SpaceMediaGfsFetcher
from socialhome.space_media_seal import (
    SPACE_MEDIA_SUITE_AESGCM256_PADDED,
    derive_space_media_key,
    seal_space_media,
)

SPACE = "sp-1"
KEY = derive_space_media_key(bytes(range(32)))
SUITE = SPACE_MEDIA_SUITE_AESGCM256_PADDED


def _blob(name: str, data: bytes, *, epoch: int = 2, key: bytes = KEY) -> bytes:
    return seal_space_media(
        media_key=key, space_id=SPACE, epoch=epoch, filename=name, data=data
    )


def _wire(name: str, blob: bytes, *, epoch: int = 2, suite: str = SUITE) -> dict:
    return {
        name: {
            "blob_id": blob_id_of(blob),
            "suite": suite,
            "epoch": epoch,
            "size": len(blob),
        }
    }


class _Refs:
    def __init__(self):
        self.rows: dict[str, SpaceMediaRef] = {}

    async def save_many(self, refs):
        for r in refs:
            self.rows[r.filename] = r

    async def get(self, filename):
        return self.rows.get(filename)

    async def delete(self, filename):
        self.rows.pop(filename, None)


class _Crypto:
    async def media_key(self, space_id, *, epoch=None):
        if space_id != SPACE or epoch not in (None, 2):
            return None
        return 2, KEY


class _Gfs:
    def __init__(self, blobs: dict[str, bytes], *, delay: float = 0.0):
        self.blobs = blobs
        self.delay = delay
        self.fetches: list[tuple[str, str]] = []

    async def fetch_space_media(self, base_url, blob_id, *, max_bytes, timeout_s):
        self.fetches.append((base_url, blob_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        blob = self.blobs.get(base_url + "|" + blob_id) or self.blobs.get(blob_id)
        if blob is None or len(blob) > max_bytes:
            return None
        return blob


@dataclass
class _Conn:
    id: str
    inbox_url: str


class _GfsRepo:
    def __init__(self, conns=()):
        self.conns = list(conns)

    async def list_active(self):
        return self.conns


@dataclass
class _Inst:
    id: str
    source: InstanceSource
    relay_via: str | None = None


class _FedRepo:
    def __init__(self, insts=()):
        self.insts = {i.id: i for i in insts}

    async def list_member_instance_ids(self, space_id):
        return list(self.insts)

    async def get_instance(self, iid):
        return self.insts.get(iid)


HOST = _Inst("a", InstanceSource.SPACE_SESSION, relay_via="https://g1.example")


def _fetcher(tmp_path, gfs, *, conns=(), insts=(HOST,)):
    return SpaceMediaGfsFetcher(
        refs_repo=_Refs(),
        space_crypto=_Crypto(),
        gfs_service=gfs,
        gfs_repo=_GfsRepo(conns),
        federation_repo=_FedRepo(insts),
        media_dir=tmp_path,
    )


async def test_receive_stores_the_items_own_refs(tmp_path):
    blob = _blob("a.webp", b"x")
    f = _fetcher(tmp_path, _Gfs({}))
    refs = await f.receive(
        SPACE,
        {**_wire("a.webp", blob), **_wire("b.webp", blob)},
        ["api/media/a.webp", "api/media/b.webp"],
        prefetch=False,
    )
    assert {r.filename for r in refs} == {"a.webp", "b.webp"}
    assert (await f._refs.get("a.webp")).blob_id == blob_id_of(blob)


async def test_receive_ignores_refs_for_files_the_item_does_not_show(tmp_path):
    blob = _blob("a.webp", b"x")
    f = _fetcher(tmp_path, _Gfs({}))
    refs = await f.receive(
        SPACE, _wire("secret.webp", blob), ["api/media/a.webp"], prefetch=False
    )
    assert refs == []
    assert await f.receive(SPACE, None, ["api/media/a.webp"]) == []


async def test_prefetch_writes_the_picture_byte_identical(tmp_path):
    data = os.urandom(70_000)
    blob = _blob("a.webp", data)
    gfs = _Gfs({blob_id_of(blob): blob})
    f = _fetcher(tmp_path, gfs)
    await f.receive(SPACE, _wire("a.webp", blob), ["api/media/a.webp"])
    await asyncio.wait_for(f.drain(), timeout=5)
    assert (tmp_path / "a.webp").read_bytes() == data
    # From the GFS the link-joined pair relays through.
    assert gfs.fetches[0] == ("https://g1.example", blob_id_of(blob))
    assert not any(p.name.startswith(".") for p in tmp_path.iterdir() if p.is_file())


async def test_prefetch_skips_videos_and_files(tmp_path):
    blob = _blob("v.mp4", b"x")
    gfs = _Gfs({blob_id_of(blob): blob})
    f = _fetcher(tmp_path, gfs)
    await f.receive(SPACE, _wire("v.mp4", blob), ["api/media/v.mp4"])
    await asyncio.wait_for(f.drain(), timeout=5)
    assert gfs.fetches == []
    assert await f.ensure_local("v.mp4") is True
    assert (tmp_path / "v.mp4").read_bytes() == b"x"


async def test_ensure_local_without_a_ref_or_with_a_file(tmp_path):
    f = _fetcher(tmp_path, _Gfs({}))
    assert await f.ensure_local("nope.webp") is False
    (tmp_path / "here.webp").write_bytes(b"h")
    blob = _blob("here.webp", b"other")
    await f.receive(
        SPACE, _wire("here.webp", blob), ["api/media/here.webp"], prefetch=False
    )
    assert await f.ensure_local("here.webp") is True
    assert (tmp_path / "here.webp").read_bytes() == b"h"  # write-once
    assert await f.ensure_local("../etc") is False


async def test_concurrent_views_coalesce_into_one_fetch(tmp_path):
    data = b"pic" * 100
    blob = _blob("a.webp", data)
    gfs = _Gfs({blob_id_of(blob): blob}, delay=0.1)
    f = _fetcher(tmp_path, gfs)
    await f.receive(SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    results = await asyncio.gather(*(f.ensure_local("a.webp") for _ in range(5)))
    assert results == [True] * 5
    assert len(gfs.fetches) == 1


async def test_a_slow_gfs_times_out(tmp_path, monkeypatch):
    blob = _blob("a.webp", b"x")
    gfs = _Gfs({blob_id_of(blob): blob}, delay=1.0)
    f = _fetcher(tmp_path, gfs)
    await f.receive(SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    assert await f.ensure_local("a.webp", timeout_s=0.05) is False
    await f.stop()


async def test_bad_hash_is_refused(tmp_path, caplog):
    blob = _blob("a.webp", b"x")
    other = _blob("a.webp", b"y")
    gfs = _Gfs({blob_id_of(blob): other})
    f = _fetcher(tmp_path, gfs)
    await f.receive(SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    with caplog.at_level(logging.WARNING):
        assert await f.ensure_local("a.webp") is False
    assert "hash" in caplog.text
    assert not (tmp_path / "a.webp").exists()


async def test_a_blob_sealed_for_another_file_is_refused(tmp_path, caplog):
    """Right hash, wrong AAD (sealed for another file name): decrypt fails."""
    blob = _blob("other.webp", b"x")
    gfs = _Gfs({blob_id_of(blob): blob})
    f = _fetcher(tmp_path, gfs)
    await f.receive(SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    with caplog.at_level(logging.WARNING):
        assert await f.ensure_local("a.webp") is False
    assert "decrypt" in caplog.text
    assert not (tmp_path / "a.webp").exists()


async def test_unknown_suite_is_refused(tmp_path, caplog):
    blob = _blob("a.webp", b"x")
    f = _fetcher(tmp_path, _Gfs({blob_id_of(blob): blob}))
    # A stored row with a suite this build lacks (written by a newer build).
    await f._refs.save_many(
        [
            SpaceMediaRef(
                filename="a.webp",
                space_id=SPACE,
                blob_id=blob_id_of(blob),
                suite="future-suite",
                epoch=2,
                size=len(blob),
            )
        ]
    )
    with caplog.at_level(logging.WARNING):
        assert await f.ensure_local("a.webp") is False
    assert "suite" in caplog.text


async def test_no_epoch_key_is_refused(tmp_path, caplog):
    blob = _blob("a.webp", b"x", epoch=7)
    f = _fetcher(tmp_path, _Gfs({blob_id_of(blob): blob}))
    await f.receive(
        SPACE, _wire("a.webp", blob, epoch=7), ["api/media/a.webp"], prefetch=False
    )
    with caplog.at_level(logging.WARNING):
        assert await f.ensure_local("a.webp") is False
    assert "key" in caplog.text


async def test_falls_back_to_this_households_own_gfs_connections(tmp_path):
    """A follower reads through the GFS it is connected to."""
    blob = _blob("a.webp", b"x")
    gfs = _Gfs({"https://g2.example|" + blob_id_of(blob): blob})
    f = _fetcher(tmp_path, gfs, conns=[_Conn("g2", "https://g2.example")], insts=())
    await f.receive(SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    assert await f.ensure_local("a.webp") is True
    assert [u for u, _ in gfs.fetches] == ["https://g2.example"]


async def test_a_miss_everywhere_is_false(tmp_path):
    blob = _blob("a.webp", b"x")
    gfs = _Gfs({})
    f = _fetcher(tmp_path, gfs, conns=[_Conn("g2", "https://g2.example")])
    await f.receive(SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    assert await f.ensure_local("a.webp") is False
    assert [u for u, _ in gfs.fetches] == ["https://g1.example", "https://g2.example"]


async def test_prefetch_backlog_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, "MAX_PREFETCH_PENDING", 1)
    blob = _blob("a.webp", b"x")
    blob2 = _blob("b.webp", b"y")
    gfs = _Gfs({blob_id_of(blob): blob, blob_id_of(blob2): blob2}, delay=0.05)
    f = _fetcher(tmp_path, gfs)
    await f.receive(
        SPACE,
        {**_wire("a.webp", blob), **_wire("b.webp", blob2)},
        ["api/media/a.webp", "api/media/b.webp"],
    )
    await asyncio.wait_for(f.drain(), timeout=5)
    assert len(gfs.fetches) == 1  # the other waits for a view


async def test_a_fetch_is_logged_with_its_gfs(tmp_path, caplog):
    """One INFO line per file fetched from a GFS — what an operator (and the
    federation demo) reads to see the cache served it. Names the file, the
    blob address and the GFS; never a key."""
    blob = _blob("a.webp", b"x")
    f = _fetcher(tmp_path, _Gfs({blob_id_of(blob): blob}))
    await f.receive(SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    with caplog.at_level(logging.INFO):
        assert await f.ensure_local("a.webp") is True
    line = next(r.getMessage() for r in caplog.records if "fetched" in r.getMessage())
    assert "a.webp" in line and blob_id_of(blob)[:12] in line
    assert "https://g1.example" in line


class _LateGfs(_Gfs):
    """Holds the blob only from the *n*-th fetch on — the upload lands
    after the item that names it (federation demo, gfs-space-post)."""

    def __init__(self, blobs, *, from_call: int):
        super().__init__(blobs)
        self.from_call = from_call

    async def fetch_space_media(self, base_url, blob_id, *, max_bytes, timeout_s):
        self.fetches.append((base_url, blob_id))
        if len(self.fetches) < self.from_call:
            return None
        return self.blobs.get(blob_id)


async def test_a_prefetch_that_misses_retries_with_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, "PREFETCH_RETRY_DELAYS_S", (0.0, 0.0))
    data = b"late picture"
    blob = _blob("a.webp", data)
    gfs = _LateGfs({blob_id_of(blob): blob}, from_call=3)
    f = _fetcher(tmp_path, gfs, insts=())
    f._gfs_repo.conns = [_Conn("g1", "https://g1.example")]
    await f.receive(SPACE, _wire("a.webp", blob), ["api/media/a.webp"])
    await asyncio.wait_for(f.drain(), timeout=5)
    assert (tmp_path / "a.webp").read_bytes() == data
    assert len(gfs.fetches) == 3


async def test_prefetch_retries_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, "PREFETCH_RETRY_DELAYS_S", (0.0, 0.0))
    blob = _blob("a.webp", b"x")
    gfs = _LateGfs({blob_id_of(blob): blob}, from_call=99)
    f = _fetcher(tmp_path, gfs, insts=())
    f._gfs_repo.conns = [_Conn("g1", "https://g1.example")]
    await f.receive(SPACE, _wire("a.webp", blob), ["api/media/a.webp"])
    await asyncio.wait_for(f.drain(), timeout=5)
    assert len(gfs.fetches) == 3  # the first try + two retries
    assert not (tmp_path / "a.webp").exists()


async def test_a_refused_blob_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, "PREFETCH_RETRY_DELAYS_S", (0.0, 0.0))
    blob = _blob("a.webp", b"x")
    gfs = _Gfs({blob_id_of(blob): _blob("a.webp", b"other")})
    f = _fetcher(tmp_path, gfs, insts=())
    f._gfs_repo.conns = [_Conn("g1", "https://g1.example")]
    await f.receive(SPACE, _wire("a.webp", blob), ["api/media/a.webp"])
    await asyncio.wait_for(f.drain(), timeout=5)
    assert len(gfs.fetches) == 1
