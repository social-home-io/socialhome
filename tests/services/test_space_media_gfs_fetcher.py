"""The reader side of the GFS space media cache
(:mod:`socialhome.services.space_media_gfs_fetcher`)."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from socialhome.domain.federation import InstanceSource
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.file_hashes import (
    FileHash,
    sha256_hex,
    space_content_media_name,
)
from socialhome.domain.gfs_space_media import space_media_address
from socialhome.domain.space import SpaceRole
from socialhome.domain.space_media_ref import SpaceMediaRef, space_page_media_name
from socialhome.services import space_media_gfs_fetcher as fetch_mod
from socialhome.services.inbound_media_store import (
    gfs_space_media_name,
    space_direct_media_name,
)
from socialhome.services.space_media_gfs_fetcher import SpaceMediaGfsFetcher
from socialhome.space_media_hint import open_space_media_hint, parse_hint_wire
from socialhome.space_media_seal import (
    SPACE_MEDIA_SUITE_AESGCM256_PADDED,
    derive_space_media_key,
    seal_space_media,
)

SPACE = "sp-1"
KEY = derive_space_media_key(bytes(range(32)))
SUITE = SPACE_MEDIA_SUITE_AESGCM256_PADDED
ITEM = "post-1"
ORIGIN = "peer-a"


def _blob(
    name: str,
    data: bytes,
    *,
    epoch: int = 2,
    key: bytes = KEY,
    item_id: str = ITEM,
    origin: str = ORIGIN,
) -> bytes:
    return seal_space_media(
        media_key=key,
        space_id=SPACE,
        epoch=epoch,
        item_id=item_id,
        origin=origin,
        filename=name,
        data=data,
    )


def _L(
    name: str, origin: str = ORIGIN, space_id: str = SPACE, item_id: str = ITEM
) -> str:
    """This household's name for *name* of *item_id* as delivered by *origin*."""
    return gfs_space_media_name(space_id, origin, item_id, name)


CHANNEL_ID = "c" * 32


def _addr(blob: bytes, scope: str = "space", scope_id: str = SPACE) -> str:
    """The blob's scope-bound GFS address (sm1)."""
    return space_media_address(scope, scope_id, blob)


def _wire(
    name: str,
    blob: bytes,
    *,
    epoch: int = 2,
    suite: str = SUITE,
    scope: str = "space",
    blob_id: str | None = None,
) -> dict:
    scope_id = SPACE if scope == "space" else CHANNEL_ID
    return {
        name: {
            "blob_id": blob_id or _addr(blob, scope, scope_id),
            "suite": suite,
            "epoch": epoch,
            "size": len(blob),
            "scope": scope,
            "addr_suite": "sm1",
        }
    }


async def _recv(
    f,
    space_id,
    raw,
    urls,
    *,
    item_id: str = ITEM,
    origin: str = ORIGIN,
    prefetch: bool = True,
):
    """As an inbound handler does it: localize the item (before it is
    saved), then keep its rows. Returns the kept rows."""
    loc = await f.localize(space_id, raw, urls, item_id=item_id, origin=origin)
    return await f.keep(loc, prefetch=prefetch)


class _Refs:
    """The repo's rules: keyed by local name; only the same item moves a
    row to a newer epoch."""

    def __init__(self):
        self.rows: dict[str, SpaceMediaRef] = {}

    async def save_many(self, refs):
        for r in refs:
            held = self.rows.get(r.local_name)
            if held is None or (
                held.item_id == r.item_id
                and held.space_id == r.space_id
                and r.epoch > held.epoch
            ):
                self.rows[r.local_name] = r

    async def get(self, local_name):
        return self.rows.get(local_name)

    async def list_for_item(self, space_id, item_id):
        return [
            r
            for r in self.rows.values()
            if r.space_id == space_id and r.item_id == item_id
        ]


class _Crypto:
    def __init__(self, epochs=(2,)):
        self.epochs = epochs

    async def media_key(self, space_id, *, epoch=None):
        if space_id != SPACE or (epoch is not None and epoch not in self.epochs):
            return None
        return (epoch if epoch is not None else self.epochs[-1]), KEY


class _Gfs:
    def __init__(self, blobs: dict[str, bytes], *, delay: float = 0.0, known_urls=None):
        self.blobs = blobs
        self.delay = delay
        #: Base URLs this household holds a connection to (``None``: any).
        self.known_urls = known_urls
        self.fetches: list[tuple[str, str]] = []

    async def connection_for_url(self, url):
        if self.known_urls is not None and url not in self.known_urls:
            return None
        return _Conn("relay-" + url, url)

    async def space_media_on_demand_wait(self, conn):
        return None  # a GFS before v_83: proves no on-demand

    async def fetch_space_media(self, conn, blob_id, *, max_bytes, timeout_s):
        base_url = conn.inbox_url
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


@dataclass
class _Seat:
    user_id: str
    role: SpaceRole


V92 = FederationCapability.MIN_FOR_SPACE_MEDIA_WANTED
SPACE_HOST = "host-h"


@dataclass
class _Space:
    id: str
    owner_instance_id: str


class _Spaces:
    """The reader's own channel id of a private space, and its seat: a
    follower household by default (v_92: only a follower asks its GFS)."""

    def __init__(self, channel_id: str | None = None, *, member: bool = False):
        self.channel_id = channel_id
        self.role = SpaceRole.MEMBER if member else SpaceRole.SUBSCRIBER

    async def get(self, space_id):
        return _Space(space_id, SPACE_HOST)

    async def get_gfs_channel(self, space_id):
        return (self.channel_id, "pk") if self.channel_id else None

    async def list_local_member_user_ids(self, space_id):
        return ["u-me"]

    async def list_members(self, space_id):
        return [_Seat("u-me", self.role)]


class _Fed:
    """``space_member_supports`` over fixed household versions."""

    def __init__(self, versions):
        self.versions = dict(versions)

    async def space_member_supports(self, instance_id, *, min_version):
        return self.versions.get(instance_id, 0) >= min_version


def _fetcher(
    tmp_path,
    gfs,
    *,
    conns=(),
    insts=(HOST,),
    channel_id=None,
    member=False,
    host_version=V92,
):
    return SpaceMediaGfsFetcher(
        refs_repo=_Refs(),
        space_crypto=_Crypto(),
        gfs_service=gfs,
        gfs_repo=_GfsRepo(conns),
        federation_repo=_FedRepo(insts),
        space_repo=_Spaces(channel_id, member=member),
        federation=_Fed({SPACE_HOST: host_version}),
        media_dir=tmp_path,
    )


async def test_receive_stores_the_items_own_refs(tmp_path):
    blob = _blob("a.webp", b"x")
    f = _fetcher(tmp_path, _Gfs({}))
    refs = await _recv(
        f,
        SPACE,
        {**_wire("a.webp", blob), **_wire("b.webp", blob)},
        ["api/media/a.webp", "api/media/b.webp"],
        prefetch=False,
    )
    assert {r.filename for r in refs} == {"a.webp", "b.webp"}
    assert {r.local_name for r in refs} == {_L("a.webp"), _L("b.webp")}
    assert (await f._refs.get(_L("a.webp"))).blob_id == _addr(blob)


async def test_receive_ignores_refs_for_files_the_item_does_not_show(tmp_path):
    blob = _blob("a.webp", b"x")
    f = _fetcher(tmp_path, _Gfs({}))
    refs = await _recv(
        f, SPACE, _wire("secret.webp", blob), ["api/media/a.webp"], prefetch=False
    )
    assert refs == []
    assert await _recv(f, SPACE, None, ["api/media/a.webp"]) == []


async def test_prefetch_writes_the_picture_byte_identical(tmp_path):
    data = os.urandom(70_000)
    blob = _blob("a.webp", data)
    gfs = _Gfs({_addr(blob): blob})
    f = _fetcher(tmp_path, gfs)
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"])
    await asyncio.wait_for(f.drain(), timeout=5)
    assert (tmp_path / _L("a.webp")).read_bytes() == data
    # From the GFS the link-joined pair relays through.
    assert gfs.fetches[0] == ("https://g1.example", _addr(blob))
    assert not any(p.name.startswith(".") for p in tmp_path.iterdir() if p.is_file())


async def test_prefetch_skips_videos_and_files(tmp_path):
    blob = _blob("v.mp4", b"x")
    gfs = _Gfs({_addr(blob): blob})
    f = _fetcher(tmp_path, gfs)
    await _recv(f, SPACE, _wire("v.mp4", blob), ["api/media/v.mp4"])
    await asyncio.wait_for(f.drain(), timeout=5)
    assert gfs.fetches == []
    assert await f.ensure_local(_L("v.mp4")) is True
    assert (tmp_path / _L("v.mp4")).read_bytes() == b"x"


async def test_ensure_local_without_a_ref_or_with_a_file(tmp_path):
    f = _fetcher(tmp_path, _Gfs({}))
    assert await f.ensure_local("nope.webp") is False
    (tmp_path / _L("here.webp")).write_bytes(b"h")
    blob = _blob("here.webp", b"other")
    await _recv(
        f, SPACE, _wire("here.webp", blob), ["api/media/here.webp"], prefetch=False
    )
    assert await f.ensure_local(_L("here.webp")) is True
    assert (tmp_path / _L("here.webp")).read_bytes() == b"h"  # write-once
    assert await f.ensure_local("../etc") is False


async def test_a_view_of_a_direct_file_still_on_its_way_is_quiet(tmp_path, caplog):
    """A file another household pushes (``sd-`` / a local name) and that has
    not arrived yet is routine for a viewer — never a GFS fetch, never a
    WARNING (the media route asks on every 404)."""
    f = _fetcher(tmp_path, _Gfs({}))
    with caplog.at_level("WARNING"):
        assert await f.ensure_local("sd-" + "a" * 40 + ".webp") is False
        assert await f.ensure_local("nope.webp") is False
    assert caplog.text == ""


async def test_concurrent_views_coalesce_into_one_fetch(tmp_path):
    data = b"pic" * 100
    blob = _blob("a.webp", data)
    gfs = _Gfs({_addr(blob): blob}, delay=0.1)
    f = _fetcher(tmp_path, gfs)
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    results = await asyncio.gather(*(f.ensure_local(_L("a.webp")) for _ in range(5)))
    assert results == [True] * 5
    assert len(gfs.fetches) == 1


async def test_a_slow_gfs_times_out(tmp_path, monkeypatch):
    blob = _blob("a.webp", b"x")
    gfs = _Gfs({_addr(blob): blob}, delay=1.0)
    f = _fetcher(tmp_path, gfs)
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    assert await f.ensure_local(_L("a.webp"), timeout_s=0.05) is False
    await f.stop()


async def test_bad_hash_is_refused(tmp_path, caplog):
    blob = _blob("a.webp", b"x")
    other = _blob("a.webp", b"y")
    gfs = _Gfs({_addr(blob): other})
    f = _fetcher(tmp_path, gfs)
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    with caplog.at_level(logging.WARNING):
        assert await f.ensure_local(_L("a.webp")) is False
    assert "address" in caplog.text
    assert not (tmp_path / _L("a.webp")).exists()


async def test_a_blob_sealed_for_another_file_is_refused(tmp_path, caplog):
    """Right hash, wrong AAD (sealed for another file name): decrypt fails."""
    blob = _blob("other.webp", b"x")
    gfs = _Gfs({_addr(blob): blob})
    f = _fetcher(tmp_path, gfs)
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    with caplog.at_level(logging.WARNING):
        assert await f.ensure_local(_L("a.webp")) is False
    assert "decrypt" in caplog.text
    assert not (tmp_path / _L("a.webp")).exists()


async def test_unknown_suite_is_refused(tmp_path, caplog):
    blob = _blob("a.webp", b"x")
    f = _fetcher(tmp_path, _Gfs({_addr(blob): blob}))
    # A stored row with a suite this build lacks (written by a newer build).
    await f._refs.save_many(
        [
            SpaceMediaRef(
                filename="a.webp",
                local_name=_L("a.webp"),
                item_id=ITEM,
                origin=ORIGIN,
                space_id=SPACE,
                blob_id=_addr(blob),
                suite="future-suite",
                epoch=2,
                size=len(blob),
                scope="space",
                addr_suite="sm1",
            )
        ]
    )
    with caplog.at_level(logging.WARNING):
        assert await f.ensure_local(_L("a.webp")) is False
    assert "suite" in caplog.text


async def test_no_epoch_key_is_refused(tmp_path, caplog):
    blob = _blob("a.webp", b"x", epoch=7)
    f = _fetcher(tmp_path, _Gfs({_addr(blob): blob}))
    await _recv(
        f, SPACE, _wire("a.webp", blob, epoch=7), ["api/media/a.webp"], prefetch=False
    )
    with caplog.at_level(logging.WARNING):
        assert await f.ensure_local(_L("a.webp")) is False
    assert "key" in caplog.text


async def test_falls_back_to_this_households_own_gfs_connections(tmp_path):
    """A follower reads through the GFS it is connected to."""
    blob = _blob("a.webp", b"x")
    gfs = _Gfs({"https://g2.example|" + _addr(blob): blob})
    f = _fetcher(tmp_path, gfs, conns=[_Conn("g2", "https://g2.example")], insts=())
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    assert await f.ensure_local(_L("a.webp")) is True
    assert [u for u, _ in gfs.fetches] == ["https://g2.example"]


async def test_a_miss_everywhere_is_false(tmp_path):
    blob = _blob("a.webp", b"x")
    gfs = _Gfs({})
    f = _fetcher(tmp_path, gfs, conns=[_Conn("g2", "https://g2.example")])
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    assert await f.ensure_local(_L("a.webp")) is False
    assert [u for u, _ in gfs.fetches] == ["https://g1.example", "https://g2.example"]


async def test_prefetch_backlog_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, "MAX_PREFETCH_PENDING", 1)
    blob = _blob("a.webp", b"x")
    blob2 = _blob("b.webp", b"y")
    gfs = _Gfs({_addr(blob): blob, _addr(blob2): blob2}, delay=0.05)
    f = _fetcher(tmp_path, gfs)
    await _recv(
        f,
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
    f = _fetcher(tmp_path, _Gfs({_addr(blob): blob}))
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    with caplog.at_level(logging.INFO):
        assert await f.ensure_local(_L("a.webp")) is True
    line = next(r.getMessage() for r in caplog.records if "fetched" in r.getMessage())
    assert "a.webp" in line and _addr(blob)[:12] in line
    assert "https://g1.example" in line


class _LateGfs(_Gfs):
    """Holds the blob only from the *n*-th fetch on — the upload lands
    after the item that names it (federation demo, gfs-space-post)."""

    def __init__(self, blobs, *, from_call: int):
        super().__init__(blobs)
        self.from_call = from_call

    async def fetch_space_media(self, conn, blob_id, *, max_bytes, timeout_s):
        self.fetches.append((conn.inbox_url, blob_id))
        if len(self.fetches) < self.from_call:
            return None
        return self.blobs.get(blob_id)


async def test_a_prefetch_that_misses_retries_with_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, "PREFETCH_RETRY_DELAYS_S", (0.0, 0.0))
    data = b"late picture"
    blob = _blob("a.webp", data)
    gfs = _LateGfs({_addr(blob): blob}, from_call=3)
    f = _fetcher(tmp_path, gfs, insts=())
    f._gfs_repo.conns = [_Conn("g1", "https://g1.example")]
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"])
    await asyncio.wait_for(f.drain(), timeout=5)
    assert (tmp_path / _L("a.webp")).read_bytes() == data
    assert len(gfs.fetches) == 3


async def test_prefetch_retries_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, "PREFETCH_RETRY_DELAYS_S", (0.0, 0.0))
    blob = _blob("a.webp", b"x")
    gfs = _LateGfs({_addr(blob): blob}, from_call=99)
    f = _fetcher(tmp_path, gfs, insts=())
    f._gfs_repo.conns = [_Conn("g1", "https://g1.example")]
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"])
    await asyncio.wait_for(f.drain(), timeout=5)
    assert len(gfs.fetches) == 3  # the first try + two retries
    assert not (tmp_path / _L("a.webp")).exists()


async def test_a_refused_blob_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, "PREFETCH_RETRY_DELAYS_S", (0.0, 0.0))
    blob = _blob("a.webp", b"x")
    gfs = _Gfs({_addr(blob): _blob("a.webp", b"other")})
    f = _fetcher(tmp_path, gfs, insts=())
    f._gfs_repo.conns = [_Conn("g1", "https://g1.example")]
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"])
    await asyncio.wait_for(f.drain(), timeout=5)
    assert len(gfs.fetches) == 1


async def test_a_blob_at_another_scopes_address_is_refused(tmp_path, caplog):
    """The descriptor says ``space``: the reader re-derives the address with
    its own space id. A blob whose bytes hash right but that sits at an
    address of another scope (a channel's copy) is refused."""
    blob = _blob("a.webp", b"x")
    other = _addr(blob, "channel", CHANNEL_ID)
    gfs = _Gfs({other: blob})
    f = _fetcher(tmp_path, gfs)
    await _recv(
        f,
        SPACE,
        _wire("a.webp", blob, blob_id=other),
        ["api/media/a.webp"],
        prefetch=False,
    )
    with caplog.at_level(logging.WARNING):
        assert await f.ensure_local(_L("a.webp")) is False
    assert "address" in caplog.text
    assert not (tmp_path / _L("a.webp")).exists()


async def test_a_channel_blob_is_checked_with_the_readers_own_channel_id(tmp_path):
    data = b"private picture"
    blob = _blob("a.webp", data)
    gfs = _Gfs({_addr(blob, "channel", CHANNEL_ID): blob})
    f = _fetcher(tmp_path, gfs, channel_id=CHANNEL_ID)
    await _recv(
        f,
        SPACE,
        _wire("a.webp", blob, scope="channel"),
        ["api/media/a.webp"],
        prefetch=False,
    )
    assert await f.ensure_local(_L("a.webp")) is True
    assert (tmp_path / _L("a.webp")).read_bytes() == data


async def test_a_channel_blob_without_a_channel_here_is_refused(tmp_path, caplog):
    blob = _blob("a.webp", b"x")
    gfs = _Gfs({_addr(blob, "channel", CHANNEL_ID): blob})
    f = _fetcher(tmp_path, gfs)
    await _recv(
        f,
        SPACE,
        _wire("a.webp", blob, scope="channel"),
        ["api/media/a.webp"],
        prefetch=False,
    )
    with caplog.at_level(logging.WARNING):
        assert await f.ensure_local(_L("a.webp")) is False
    assert gfs.fetches == []


async def test_a_descriptor_never_names_a_reserved_file(tmp_path):
    """#1309 review B: a ``gm-`` (moment media) or ``dm-`` (chat) name is
    another receiving path's; a descriptor naming one is dropped even when
    the item lists it, so a GFS blob can never land there."""
    blob = _blob("gm-x.webp", b"x")
    f = _fetcher(tmp_path, _Gfs({_addr(blob): blob}))
    refs = await _recv(
        f, SPACE, _wire("gm-x.webp", blob), ["api/media/gm-x.webp"], prefetch=False
    )
    assert refs == []
    assert await f._refs.get(_L("gm-x.webp")) is None


async def test_a_stored_row_for_a_reserved_name_is_never_fetched(tmp_path):
    blob = _blob("dm-x.webp", b"x")
    gfs = _Gfs({_addr(blob): blob})
    f = _fetcher(tmp_path, gfs)
    await f._refs.save_many(
        [
            SpaceMediaRef(
                filename="dm-x.webp",
                local_name="dm-x.webp",
                item_id=ITEM,
                origin=ORIGIN,
                space_id=SPACE,
                blob_id=_addr(blob),
                suite=SUITE,
                epoch=2,
                size=len(blob),
                scope="space",
                addr_suite="sm1",
            )
        ]
    )
    assert await f.ensure_local("dm-x.webp") is False
    assert gfs.fetches == []
    assert not (tmp_path / "dm-x.webp").exists()


async def test_a_relay_via_url_without_a_connection_here_is_never_fetched(tmp_path):
    """#1309 review E: a fetch rides only a connection this household
    holds (the cookie-less, address-pinned session) — never a raw URL a
    peer row names."""
    blob = _blob("a.webp", b"x")
    gfs = _Gfs({_addr(blob): blob}, known_urls=set())
    f = _fetcher(tmp_path, gfs)
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    assert await f.ensure_local(_L("a.webp")) is False
    assert gfs.fetches == []


# ── Bounded work for a reader (#1309 review G) ───────────────────────────


async def test_a_miss_on_view_is_remembered_for_a_while(tmp_path, monkeypatch):
    """A file no GFS holds isn't asked for again on every view (up to four
    GFSs each time) — for MISS_TTL_S."""
    clock = [1000.0]
    monkeypatch.setattr(fetch_mod, "_monotonic", lambda: clock[0])
    blob = _blob("a.webp", b"x")
    gfs = _Gfs({})
    f = _fetcher(tmp_path, gfs)
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    assert await f.ensure_local(_L("a.webp")) is False
    asked = len(gfs.fetches)
    assert asked >= 1
    assert await f.ensure_local(_L("a.webp")) is False
    assert len(gfs.fetches) == asked  # remembered: no GFS asked
    clock[0] += fetch_mod.MISS_TTL_S
    gfs.blobs[_addr(blob)] = blob
    assert await f.ensure_local(_L("a.webp")) is True


async def test_one_space_cannot_fill_the_prefetch_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, "MAX_PREFETCH_PER_SPACE", 1)
    monkeypatch.setattr(fetch_mod, "PREFETCH_RETRY_DELAYS_S", ())
    blobs = {name: _blob(name, name.encode()) for name in ("a.webp", "b.webp")}
    gfs = _Gfs({_addr(b): b for b in blobs.values()}, delay=0.05)
    f = _fetcher(tmp_path, gfs)
    wire = {}
    for name, b in blobs.items():
        wire.update(_wire(name, b))
    await _recv(f, SPACE, wire, ["api/media/a.webp", "api/media/b.webp"])
    await asyncio.wait_for(f.drain(), timeout=5)
    assert len(gfs.fetches) == 1  # the other waits for a view


async def test_fetches_run_under_a_global_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, "MAX_CONCURRENT_FETCHES", 1)
    inflight = 0
    peak = 0
    blobs = {name: _blob(name, name.encode()) for name in ("a.webp", "b.webp")}

    class _Counting(_Gfs):
        async def fetch_space_media(self, conn, blob_id, *, max_bytes, timeout_s):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.05)
            inflight -= 1
            return await super().fetch_space_media(
                conn, blob_id, max_bytes=max_bytes, timeout_s=timeout_s
            )

    f = _fetcher(tmp_path, _Counting({_addr(b): b for b in blobs.values()}))
    wire = {}
    for name, b in blobs.items():
        wire.update(_wire(name, b))
    await _recv(
        f, SPACE, wire, ["api/media/a.webp", "api/media/b.webp"], prefetch=False
    )
    assert await asyncio.gather(
        f.ensure_local(_L("a.webp")), f.ensure_local(_L("b.webp"))
    ) == [
        True,
        True,
    ]
    assert peak == 1


# ── #1309 re-review 1: a file's name is the reader's, never first-claim ──


async def test_a_member_naming_another_items_file_only_gets_its_own_copy(tmp_path):
    """Regression (#1309 re-review 1): member M learned A's file name F and
    sent its own item naming F with a blob M sealed itself. Under the old
    write-once-by-name rule, whichever descriptor came first took F for
    every item, so A's post could show M's picture. Now each household's F
    lands under the reader's own name for (space, that household, F): A's
    post shows A's bytes, M's item shows M's, in either order."""
    a_blob = _blob("a.webp", b"A's picture")
    m_blob = _blob("a.webp", b"M's picture", item_id="post-m", origin="peer-m")
    gfs = _Gfs({_addr(a_blob): a_blob, _addr(m_blob): m_blob})
    f = _fetcher(tmp_path, gfs)
    # M first — the order the old rule lost on.
    m_loc = await f.localize(
        SPACE,
        _wire("a.webp", m_blob),
        ["api/media/a.webp"],
        item_id="post-m",
        origin="peer-m",
    )
    await f.keep(m_loc, prefetch=False)
    a_loc = await f.localize(
        SPACE,
        _wire("a.webp", a_blob),
        ["api/media/a.webp"],
        item_id=ITEM,
        origin=ORIGIN,
    )
    await f.keep(a_loc, prefetch=False)
    assert a_loc.url("api/media/a.webp") == "api/media/" + _L("a.webp")
    assert m_loc.url("api/media/a.webp") == "api/media/" + _L(
        "a.webp", "peer-m", item_id="post-m"
    )
    assert await f.ensure_local(_L("a.webp")) is True
    assert await f.ensure_local(_L("a.webp", "peer-m", item_id="post-m")) is True
    assert (tmp_path / _L("a.webp")).read_bytes() == b"A's picture"
    assert (
        tmp_path / _L("a.webp", "peer-m", item_id="post-m")
    ).read_bytes() == b"M's picture"
    assert not (tmp_path / "a.webp").exists()


async def test_another_household_describing_the_same_item_keeps_its_binding(
    tmp_path,
):
    """A second household's descriptor for a file the SAME item already
    has here (a collaborative edit re-sending the cover) keeps the item's
    first binding: no new row, the reference maps to the name it has."""
    a_blob = _blob("a.webp", b"A")
    m_blob = _blob("a.webp", b"M", origin="peer-m")
    f = _fetcher(tmp_path, _Gfs({}))
    await _recv(f, SPACE, _wire("a.webp", a_blob), ["api/media/a.webp"], prefetch=False)
    loc = await f.localize(
        SPACE,
        _wire("a.webp", m_blob),
        ["api/media/a.webp"],
        item_id=ITEM,
        origin="peer-m",
    )
    assert loc.refs == ()
    assert loc.url("api/media/a.webp") == "api/media/" + _L("a.webp")
    assert await f._refs.get(_L("a.webp", "peer-m")) is None


async def test_an_update_without_a_descriptor_keeps_the_items_names(tmp_path):
    """A calendar update re-sends its cover URL without a descriptor when
    the cover did not change: the reference keeps the reader's name."""
    blob = _blob("a.webp", b"x")
    f = _fetcher(tmp_path, _Gfs({}))
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    loc = await f.localize(
        SPACE, None, ["api/media/a.webp"], item_id=ITEM, origin="peer-m"
    )
    assert loc.refs == ()
    assert loc.url("/api/media/a.webp") == "/api/media/" + _L("a.webp")
    # Another item naming the same file gets nothing renamed.
    other = await f.localize(
        SPACE, None, ["api/media/a.webp"], item_id="post-9", origin=ORIGIN
    )
    assert other.url("api/media/a.webp") == "api/media/a.webp"


async def test_a_blob_sealed_for_another_item_or_household_is_refused(tmp_path, caplog):
    """The AAD names the item and the household that sealed it: a blob of
    A's item, re-described by M for M's own item, does not open."""
    a_blob = _blob("a.webp", b"A's picture")
    f = _fetcher(tmp_path, _Gfs({_addr(a_blob): a_blob}))
    await _recv(
        f,
        SPACE,
        _wire("a.webp", a_blob),
        ["api/media/a.webp"],
        item_id="post-m",
        origin="peer-m",
        prefetch=False,
    )
    with caplog.at_level(logging.WARNING):
        assert await f.ensure_local(_L("a.webp", "peer-m", item_id="post-m")) is False
    assert "decrypt" in caplog.text
    assert not (tmp_path / _L("a.webp", "peer-m", item_id="post-m")).exists()


async def test_only_a_gfs_space_media_name_is_ever_fetched(tmp_path):
    """The fetcher writes ``sg-`` names only — a row under any other name
    (an older build's, a crafted one) is never fetched."""
    blob = _blob("a.webp", b"x")
    gfs = _Gfs({_addr(blob): blob})
    f = _fetcher(tmp_path, gfs)
    await f._refs.save_many(
        [
            SpaceMediaRef(
                filename="a.webp",
                local_name="a.webp",
                item_id=ITEM,
                origin=ORIGIN,
                space_id=SPACE,
                blob_id=_addr(blob),
                suite=SUITE,
                epoch=2,
                size=len(blob),
                scope="space",
                addr_suite="sm1",
            )
        ]
    )
    assert await f.ensure_local("a.webp") is False
    assert gfs.fetches == []


async def test_without_an_origin_nothing_is_localized(tmp_path):
    blob = _blob("a.webp", b"x")
    f = _fetcher(tmp_path, _Gfs({}))
    loc = await f.localize(
        SPACE, _wire("a.webp", blob), ["api/media/a.webp"], item_id=ITEM, origin=""
    )
    assert (dict(loc.names), loc.refs) == ({}, ())


# ── #1309 re-review 4: the same item follows a key rotation ──────────────


async def test_the_same_item_moves_its_file_to_a_newer_epoch(tmp_path, monkeypatch):
    """After a rotation the author seals the file again under the new epoch;
    the old blob expires at the GFS TTL. The same item's newer descriptor
    moves the row (and forgets a remembered miss), so the file is not a
    permanent miss."""
    clock = [1000.0]
    monkeypatch.setattr(fetch_mod, "_monotonic", lambda: clock[0])
    old = _blob("a.webp", b"pic", epoch=2)
    new = _blob("a.webp", b"pic", epoch=3)
    gfs = _Gfs({})
    f = _fetcher(tmp_path, gfs)
    f._crypto = _Crypto(epochs=(2, 3))
    await _recv(f, SPACE, _wire("a.webp", old), ["api/media/a.webp"], prefetch=False)
    assert await f.ensure_local(_L("a.webp")) is False  # the old blob expired
    gfs.blobs[_addr(new)] = new
    await _recv(
        f, SPACE, _wire("a.webp", new, epoch=3), ["api/media/a.webp"], prefetch=False
    )
    assert (await f._refs.get(_L("a.webp"))).epoch == 3
    assert await f.ensure_local(_L("a.webp")) is True
    assert (tmp_path / _L("a.webp")).read_bytes() == b"pic"


# ── #1309 re-review 5: one space's misses can't starve prefetch ──────────


def test_the_per_space_prefetch_cap_is_small():
    assert fetch_mod.MAX_PREFETCH_PER_SPACE <= 20
    assert fetch_mod.MAX_PREFETCH_PER_SPACE < fetch_mod.MAX_PREFETCH_PENDING


async def test_a_space_that_keeps_missing_stops_retrying(tmp_path, monkeypatch):
    """After MAX_SPACE_PREFETCH_MISSES prefetches of one space found their
    blob nowhere, that space's prefetches try once and stop (views still
    fetch). One that lands resets the count."""
    monkeypatch.setattr(fetch_mod, "PREFETCH_RETRY_DELAYS_S", (0.0, 0.0))
    monkeypatch.setattr(fetch_mod, "MAX_SPACE_PREFETCH_MISSES", 2)
    gfs = _Gfs({})
    f = _fetcher(tmp_path, gfs)
    for i in range(2):
        name = f"m{i}.webp"
        await _recv(
            f,
            SPACE,
            _wire(name, _blob(name, b"x", item_id=f"p{i}"), epoch=2),
            [f"api/media/{name}"],
            item_id=f"p{i}",
        )
        await asyncio.wait_for(f.drain(), timeout=5)
    assert len(gfs.fetches) == 6  # 2 prefetches x (1 try + 2 retries)
    gfs.fetches.clear()
    await _recv(
        f,
        SPACE,
        _wire("late.webp", _blob("late.webp", b"x", item_id="p9")),
        ["api/media/late.webp"],
        item_id="p9",
    )
    await asyncio.wait_for(f.drain(), timeout=5)
    assert len(gfs.fetches) == 1  # one try, no retries
    # A prefetch that lands resets the space.
    ok = _blob("ok.webp", b"ok", item_id="p10")
    gfs.blobs[_addr(ok)] = ok
    await _recv(f, SPACE, _wire("ok.webp", ok), ["api/media/ok.webp"], item_id="p10")
    await asyncio.wait_for(f.drain(), timeout=5)
    assert f._space_misses.get(SPACE, 0) == 0


async def test_two_items_of_one_household_each_follow_a_rotation(tmp_path):
    """#1309 round 3 (item 3): two items X and Y from the same household
    naming one file F shared one row bound to X, so Y's re-description at a
    newer epoch could not move it. The item is in the reader's name: each
    item owns its row."""
    f = _fetcher(tmp_path, _Gfs({}))
    f._crypto = _Crypto(epochs=(2, 3))
    x_old = _blob("a.webp", b"pic", epoch=2, item_id="post-x")
    y_old = _blob("a.webp", b"pic", epoch=2, item_id="post-y")
    y_new = _blob("a.webp", b"pic", epoch=3, item_id="post-y")
    for item, blob, epoch in (
        ("post-x", x_old, 2),
        ("post-y", y_old, 2),
        ("post-y", y_new, 3),
    ):
        await _recv(
            f,
            SPACE,
            _wire("a.webp", blob, epoch=epoch),
            ["api/media/a.webp"],
            item_id=item,
            prefetch=False,
        )
    x = await f._refs.get(_L("a.webp", item_id="post-x"))
    y = await f._refs.get(_L("a.webp", item_id="post-y"))
    assert x.local_name != y.local_name
    assert (x.item_id, x.epoch) == ("post-x", 2)
    assert (y.item_id, y.epoch) == ("post-y", 3)


async def test_has_item_refs(tmp_path):
    """#1309 round 4: the follower heal runs only for an item with no rows
    yet — it never replaces refs or names an item already has."""
    f = _fetcher(tmp_path, _Gfs({}))
    assert await f.has_item_refs(SPACE, ITEM) is False
    blob = _blob("a.webp", b"x")
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    assert await f.has_item_refs(SPACE, ITEM) is True
    assert await f.has_item_refs(SPACE, "post-9") is False


# ── v_82: a page's files, page-scoped on a reader ───────────────────────

PAGE = "pg-1"
SHOWN = "sd-" + "1" * 40 + ".webp"


def _sp(name: str) -> str:
    return space_page_media_name(SPACE, name)


@dataclass
class _Page:
    content: str
    cover_image_url: str | None = None


class _Pages:
    def __init__(self) -> None:
        self.pages: dict[tuple[str, str], _Page] = {}
        self.sides: dict[tuple[str, str], list[_Page]] = {}

    async def get_space_page(self, page_id, *, space_id):
        return self.pages.get((space_id, page_id))

    async def list_conflict_sides(self, page_id, *, space_id):
        return list(self.sides.get((space_id, page_id), ()))


def _page_fetcher(tmp_path, gfs):
    f = _fetcher(tmp_path, gfs)
    pages = _Pages()
    f.attach_pages(pages)
    return f, pages


async def _keep_page(f, raw, urls):
    loc = f.localize_as_shown(SPACE, raw, urls, item_id=PAGE, origin=ORIGIN)
    return await f.keep(loc, prefetch=False)


async def test_a_pages_files_are_kept_page_scoped_and_fetched(tmp_path):
    """A page is the host's version byte for byte; its files are kept under
    page-scoped names (never the shared name the page shows) and fetched
    while the page — or an open side — shows them."""
    data = b"page picture"
    blob = _blob(SHOWN, data, item_id=PAGE)
    side = _blob("s.webp", b"side picture", item_id=PAGE)
    gfs = _Gfs({_addr(blob): blob, _addr(side): side})
    f, pages = _page_fetcher(tmp_path, gfs)
    pages.pages[(SPACE, PAGE)] = _Page(content="", cover_image_url=f"api/media/{SHOWN}")
    pages.sides[(SPACE, PAGE)] = [_Page(content="![s](api/media/s.webp)")]
    rows = await _keep_page(
        f,
        {**_wire(SHOWN, blob), **_wire("s.webp", side)},
        [f"api/media/{SHOWN}", "api/media/s.webp"],
    )
    assert {(r.local_name, r.filename, r.item_id) for r in rows} == {
        (_sp(SHOWN), SHOWN, PAGE),
        (_sp("s.webp"), "s.webp", PAGE),
    }
    assert await f.ensure_local(_sp(SHOWN)) is True
    assert await f.ensure_local(_sp("s.webp")) is True
    assert (tmp_path / _sp(SHOWN)).read_bytes() == data
    assert not (tmp_path / SHOWN).exists()  # never the shared name
    assert await f.ensure_local(SHOWN) is False


async def test_a_page_file_its_page_no_longer_shows_is_never_fetched(tmp_path):
    blob = _blob(SHOWN, b"x", item_id=PAGE)
    gfs = _Gfs({_addr(blob): blob})
    f, pages = _page_fetcher(tmp_path, gfs)
    await _keep_page(f, _wire(SHOWN, blob), [f"api/media/{SHOWN}"])
    assert await f.ensure_local(_sp(SHOWN)) is False
    pages.pages[(SPACE, PAGE)] = _Page(content="no pictures")
    assert await f.ensure_local(_sp(SHOWN)) is False
    assert gfs.fetches == []


async def test_a_page_descriptor_never_names_a_reserved_or_unshown_file(tmp_path):
    blob = _blob("dm-x.webp", b"x", item_id=PAGE)
    f, _pages = _page_fetcher(tmp_path, _Gfs({_addr(blob): blob}))
    rows = await _keep_page(
        f,
        {**_wire("dm-x.webp", blob), **_wire("other.webp", blob)},
        ["api/media/dm-x.webp"],
    )
    assert rows == []


async def test_without_pages_attached_nothing_page_scoped_is_kept(tmp_path):
    blob = _blob(SHOWN, b"x", item_id=PAGE)
    f = _fetcher(tmp_path, _Gfs({_addr(blob): blob}))
    loc = f.localize_as_shown(
        SPACE, _wire(SHOWN, blob), [f"api/media/{SHOWN}"], item_id=PAGE, origin=ORIGIN
    )
    assert loc.refs == ()
    assert await f.ensure_local(_sp(SHOWN)) is False


# ── v_82: a link-joined proposer's page files, fetched by the host ───────

PROPOSER = "hh-e"


def _sd(name: str, origin: str = PROPOSER) -> str:
    return space_direct_media_name(SPACE, origin, name)


async def test_the_host_fetches_a_proposers_file_under_its_sender_bound_name(
    tmp_path,
):
    """A link-joined proposer can't push bytes to the host: it describes
    its new files, and the host fetches each under the proposer's own
    ``sd-`` name — the one the renamed page shows, which nobody else can
    fill. The blob's AAD names the page and the proposer."""
    data = b"new picture"
    blob = _blob("n.webp", data, item_id=PAGE, origin=PROPOSER)
    gfs = _Gfs({_addr(blob): blob})
    f = _fetcher(tmp_path, gfs)
    loc = f.localize_proposal(
        SPACE,
        _wire("n.webp", blob),
        {"n.webp": _sd("n.webp")},
        item_id=PAGE,
        origin=PROPOSER,
    )
    rows = await f.keep(loc, prefetch=False)
    assert [(r.local_name, r.filename, r.origin) for r in rows] == [
        (_sd("n.webp"), "n.webp", PROPOSER)
    ]
    assert await f.ensure_local(_sd("n.webp")) is True
    assert (tmp_path / _sd("n.webp")).read_bytes() == data


async def test_a_proposal_descriptor_never_names_another_households_file(tmp_path):
    """Only the files the proposal ADDS are described — each under the
    name bound to the proposer. A descriptor entry for a file the page
    already shows (another household's ``sd-`` file) or for a name not
    bound to the proposer is dropped, never fetched."""
    theirs = space_direct_media_name(SPACE, "hh-b", "f.webp")
    blob = _blob(theirs, b"planted", item_id=PAGE, origin=PROPOSER)
    f = _fetcher(tmp_path, _Gfs({_addr(blob): blob}))
    loc = f.localize_proposal(
        SPACE,
        {**_wire(theirs, blob), **_wire("n.webp", blob)},
        {"n.webp": theirs},  # a local name not bound to the proposer
        item_id=PAGE,
        origin=PROPOSER,
    )
    assert loc.refs == ()
    assert await f.ensure_local(theirs) is False
    assert not (tmp_path / theirs).exists()


async def test_a_blob_sealed_by_another_household_is_refused(tmp_path, caplog):
    """A forged descriptor: the blob at the address was sealed for another
    household (or page) — the AAD fails, nothing is written."""
    blob = _blob("n.webp", b"x", item_id=PAGE, origin="hh-mallory")
    f = _fetcher(tmp_path, _Gfs({_addr(blob): blob}))
    loc = f.localize_proposal(
        SPACE,
        _wire("n.webp", blob),
        {"n.webp": _sd("n.webp")},
        item_id=PAGE,
        origin=PROPOSER,
    )
    await f.keep(loc, prefetch=False)
    with caplog.at_level("WARNING"):
        assert await f.ensure_local(_sd("n.webp")) is False
    assert not (tmp_path / _sd("n.webp")).exists()
    assert "cannot decrypt" in caplog.text


# ── On demand (v_83): a view that misses asks the GFS ────────────────────


class _OnDemandGfs(_Gfs):
    """A GFS that proves ``on_demand``: a ``wanted`` gets the blob from a
    holder (``served``), or nothing."""

    def __init__(self, blobs, *, served=None, wait=25, **kw):
        super().__init__(blobs, **kw)
        self.served = served or {}
        self.wait = wait
        self.wanted: list[tuple[str, str, dict]] = []

    async def space_media_on_demand_wait(self, conn):
        return self.wait

    async def want_space_media(self, conn, blob_id, body, *, max_bytes, timeout_s):
        self.wanted.append((conn.inbox_url, blob_id, body))
        blob = self.served.get(blob_id)
        if blob is None or len(blob) > max_bytes:
            return None
        return blob


async def test_a_view_that_misses_asks_once_and_gets_the_bytes(tmp_path):
    """The blob left the cache: one ``wanted`` (to the first GFS that can
    take it), and the reader gets byte-identical bytes."""
    data = os.urandom(5000)
    blob = _blob("a.webp", data)
    gfs = _OnDemandGfs({}, served={_addr(blob): blob})
    f = _fetcher(tmp_path, gfs)
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    assert await f.ensure_local(_L("a.webp")) is True
    assert (tmp_path / _L("a.webp")).read_bytes() == data
    assert len(gfs.wanted) == 1
    url, bid, body = gfs.wanted[0]
    assert (url, bid) == ("https://g1.example", _addr(blob))
    # The body names the scope and a sealed hint — nothing about the reader.
    assert set(body) == {"scope", "scope_id", "hint"}
    assert (body["scope"], body["scope_id"]) == ("space", SPACE)
    hint = parse_hint_wire(body["hint"])
    loc = open_space_media_hint(
        hint, media_key=KEY, scope="space", scope_id=SPACE, blob_id=_addr(blob)
    )
    assert (loc.item_id, loc.origin, loc.filename) == (ITEM, ORIGIN, "a.webp")


async def test_a_member_household_never_asks_its_gfs_for_a_content_named_file(
    tmp_path,
):
    """v_92: a household holding a member seat in a space whose host is at
    v_92 asks its host or a helper with ``SPACE_MEDIA_WANTED`` for an
    ``sc-`` file — never its GFS's on-demand path."""
    data = b"x"
    blob = _blob("a.webp", data)
    gfs = _OnDemandGfs({}, served={_addr(blob): blob})
    f = _fetcher(tmp_path, gfs, member=True)
    await _recv_hashed(f, _wire("a.webp", blob), data)
    assert await f.ensure_local(_sc("a.webp", data)) is False
    assert gfs.wanted == []


async def test_a_member_holding_a_gfs_named_file_still_asks_its_gfs(tmp_path):
    """A member keeps a file under its ``sg-`` name when it took it from the
    public GFS copy (no hash map, or before v_92) — ``SPACE_MEDIA_WANTED``
    asks only for ``sc-`` content names, so its GFS on-demand ask is the
    one way that file heals past the cache."""
    data = os.urandom(300)
    blob = _blob("a.webp", data)
    gfs = _OnDemandGfs({}, served={_addr(blob): blob})
    f = _fetcher(tmp_path, gfs, member=True)
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    assert await f.ensure_local(_L("a.webp")) is True
    assert (tmp_path / _L("a.webp")).read_bytes() == data
    assert len(gfs.wanted) == 1


async def test_a_member_whose_host_is_below_v92_still_asks_its_gfs(tmp_path):
    """Rollout: a host below v_92 answers no ``SPACE_MEDIA_WANTED``, so a
    member of its space keeps the v_83 on-demand ask it had before."""
    data = os.urandom(300)
    blob = _blob("a.webp", data)
    gfs = _OnDemandGfs({}, served={_addr(blob): blob})
    f = _fetcher(tmp_path, gfs, member=True, host_version=V92 - 1)
    await _recv_hashed(f, _wire("a.webp", blob), data)
    assert await f.ensure_local(_sc("a.webp", data)) is True
    assert (tmp_path / _sc("a.webp", data)).read_bytes() == data
    assert len(gfs.wanted) == 1


async def test_a_wanted_answer_is_checked_like_a_fetch(tmp_path, caplog):
    blob = _blob("a.webp", b"x")
    other = _blob("a.webp", b"y")
    gfs = _OnDemandGfs({}, served={_addr(blob): other})
    f = _fetcher(tmp_path, gfs)
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    assert await f.ensure_local(_L("a.webp")) is False
    assert not (tmp_path / _L("a.webp")).exists()


async def test_a_prefetch_never_asks(tmp_path, monkeypatch):
    """Only a view asks: a prefetch storm of many followers never makes a
    holder upload anything."""
    monkeypatch.setattr(fetch_mod, "PREFETCH_RETRY_DELAYS_S", ())
    blob = _blob("a.webp", b"x")
    gfs = _OnDemandGfs({}, served={_addr(blob): blob})
    f = _fetcher(tmp_path, gfs)
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"])
    await asyncio.wait_for(f.drain(), timeout=5)
    assert gfs.wanted == []


async def test_a_wanted_miss_is_remembered_by_the_miss_cache(tmp_path):
    blob = _blob("a.webp", b"x")
    gfs = _OnDemandGfs({})
    f = _fetcher(tmp_path, gfs)
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    assert await f.ensure_local(_L("a.webp")) is False
    assert await f.ensure_local(_L("a.webp")) is False
    assert len(gfs.wanted) == 1


async def test_a_gfs_without_on_demand_is_never_asked(tmp_path):
    blob = _blob("a.webp", b"x")
    gfs = _OnDemandGfs({}, served={_addr(blob): blob}, wait=None)
    f = _fetcher(tmp_path, gfs)
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    assert await f.ensure_local(_L("a.webp")) is False
    assert gfs.wanted == []


async def test_a_channel_blob_is_asked_under_the_readers_channel(tmp_path):
    data = os.urandom(100)
    blob = _blob("a.webp", data)
    bid = _addr(blob, "channel", CHANNEL_ID)
    gfs = _OnDemandGfs({}, served={bid: blob})
    f = _fetcher(tmp_path, gfs, channel_id=CHANNEL_ID)
    await _recv(
        f,
        SPACE,
        _wire("a.webp", blob, scope="channel"),
        ["api/media/a.webp"],
        prefetch=False,
    )
    assert await f.ensure_local(_L("a.webp")) is True
    assert gfs.wanted[0][2]["scope_id"] == CHANNEL_ID


async def test_a_follower_asks_through_its_own_gfs_connection(tmp_path):
    """Members-equal and followers alike: a follower (no link-joined pair)
    asks the GFS it holds a connection to and gets the same bytes."""
    data = os.urandom(300)
    blob = _blob("a.webp", data)
    gfs = _OnDemandGfs({}, served={_addr(blob): blob})
    f = _fetcher(tmp_path, gfs, conns=[_Conn("g9", "https://g9.example")], insts=())
    await _recv(f, SPACE, _wire("a.webp", blob), ["api/media/a.webp"], prefetch=False)
    assert await f.ensure_local(_L("a.webp")) is True
    assert (tmp_path / _L("a.webp")).read_bytes() == data
    assert gfs.wanted[0][0] == "https://g9.example"


# ── v_85 hashed files: one content name on every path ───────────────────


def _sc(name: str, data: bytes) -> str:
    return space_content_media_name(SPACE, sha256_hex(data), name)


def _hashes(name: str, data: bytes) -> dict[str, FileHash]:
    return {name: FileHash(sha256_hex(data))}


async def _recv_hashed(f, raw, data, *, origin: str = ORIGIN, names=("a.webp",)):
    loc = await f.localize(
        SPACE,
        raw,
        [f"api/media/{n}" for n in names],
        item_id=ITEM,
        origin=origin,
        hashes=_hashes("a.webp", data),
    )
    await f.keep(loc, prefetch=False)
    return loc


async def test_a_hashed_described_file_takes_its_content_name(tmp_path):
    """A file the item's hash map covers is named ``sc-`` even when a GFS
    describes it: the same name a host's or helper's push lands under."""
    data = b"picture"
    blob = _blob("a.webp", data)
    f = _fetcher(tmp_path, _Gfs({}))
    loc = await _recv_hashed(
        f,
        {**_wire("a.webp", blob), **_wire("b.webp", blob)},
        data,
        names=("a.webp", "b.webp"),
    )
    assert loc.names == {"a.webp": _sc("a.webp", data), "b.webp": _L("b.webp")}
    by_name = {r.filename: r for r in loc.refs}
    assert by_name["a.webp"].local_name == _sc("a.webp", data)
    # The blob's AAD still names the item and the sealing household.
    assert (by_name["a.webp"].item_id, by_name["a.webp"].origin) == (ITEM, ORIGIN)
    assert by_name["b.webp"].local_name == _L("b.webp")  # unhashed: unchanged
    assert (await f._refs.get(_sc("a.webp", data))).blob_id == _addr(blob)


async def test_the_same_hashed_file_described_by_two_households_is_one_name(
    tmp_path, caplog
):
    data = b"picture"
    f = _fetcher(tmp_path, _Gfs({}))
    await _recv_hashed(f, _wire("a.webp", _blob("a.webp", data)), data)
    with caplog.at_level("INFO"):
        second = await _recv_hashed(
            f,
            _wire("a.webp", _blob("a.webp", data, origin="peer-b")),
            data,
            origin="peer-b",
        )
    assert second.names == {"a.webp": _sc("a.webp", data)}
    assert "described by" not in caplog.text
    # The row keeps the household whose blob it names (its AAD).
    assert (await f._refs.get(_sc("a.webp", data))).origin == ORIGIN


async def test_a_content_named_described_file_is_fetched_and_verified(tmp_path):
    data = os.urandom(3000)
    blob = _blob("a.webp", data)
    f = _fetcher(tmp_path, _Gfs({_addr(blob): blob}))
    await _recv_hashed(f, _wire("a.webp", blob), data)
    assert await f.ensure_local(_sc("a.webp", data)) is True
    assert (tmp_path / _sc("a.webp", data)).read_bytes() == data


async def test_a_content_named_file_whose_bytes_do_not_hash_is_refused(
    tmp_path, caplog
):
    """The author's map says h; the sealed bytes hash to something else —
    nothing is written under the content name."""
    data = b"the real picture"
    blob = _blob("a.webp", b"something else")
    f = _fetcher(tmp_path, _Gfs({_addr(blob): blob}))
    await _recv_hashed(f, _wire("a.webp", blob), data)
    with caplog.at_level("WARNING"):
        assert await f.ensure_local(_sc("a.webp", data)) is False
    assert not (tmp_path / _sc("a.webp", data)).exists()
    assert "does not hash" in caplog.text


async def test_a_wanted_content_named_file_is_verified_too(tmp_path, caplog):
    data = b"the real picture"
    wrong = _blob("a.webp", b"another picture")
    gfs = _OnDemandGfs({}, served={_addr(wrong): wrong})
    f = _fetcher(tmp_path, gfs)
    await _recv_hashed(f, _wire("a.webp", wrong), data)
    with caplog.at_level("WARNING"):
        assert await f.ensure_local(_sc("a.webp", data)) is False
    assert len(gfs.wanted) == 1
    assert not (tmp_path / _sc("a.webp", data)).exists()
    assert "does not hash" in caplog.text


async def test_a_content_name_without_a_ref_row_is_never_fetched(tmp_path, caplog):
    """An ``sc-`` file a household pushes (no descriptor) that is not here
    yet stays quiet — never a GFS fetch."""
    gfs = _Gfs({})
    f = _fetcher(tmp_path, gfs)
    with caplog.at_level("WARNING"):
        assert await f.ensure_local(_sc("a.webp", b"x")) is False
    assert gfs.fetches == []
    assert caplog.text == ""
