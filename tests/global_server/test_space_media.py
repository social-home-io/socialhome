"""GfsSpaceMediaStore — the encrypted space media cache: content-addressed,
claimable by several scopes, short-lived, bounded by an operator byte cap and
a per-scope cap (both evicting least recently fetched first), an upload rate
per scope that eviction and delete can't reset, a fetch mark at most once an
hour, and a sweep that also clears orphan files."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time

import pytest

from socialhome.domain.gfs_space_media import space_media_address
from socialhome.global_server import blob_store as bs_mod
from socialhome.global_server import space_media as sm
from socialhome.global_server.blob_store import (
    BLOB_OVERHEAD,
    FsBlobFiles,
    MediaRefused,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.repositories import (
    SqliteGfsFederationRepo,
    SqliteGfsSpaceMediaRepo,
)
from socialhome.global_server.space_media import (
    FETCH_TOUCH_INTERVAL_S,
    ORPHAN_GRACE_S,
    GfsSpaceMediaStore,
    SpaceMediaLimits,
)
from socialhome.moment_public_media import MAX_BLOB_BYTES, PAD_BUCKETS

NOW = 1_800_000_000  # an hour boundary: NOW % 3600 == 0
SMALL = PAD_BUCKETS[1] + BLOB_OVERHEAD  # 64 KiB bucket
BIG = PAD_BUCKETS[2] + BLOB_OVERHEAD  # 256 KiB bucket


def _blob(size: int = SMALL) -> bytes:
    return os.urandom(size)


def _limits(**over) -> SpaceMediaLimits:
    base = {
        "max_bytes": 10 * SMALL,
        "ttl_s": 3 * 86_400,
        "space_max_bytes": 100 * SMALL,
        "space_uploads_per_hour": 100,
        "ip_bytes_per_hour": 1024 * SMALL,
    }
    base.update(over)
    return SpaceMediaLimits(**base)


@pytest.fixture
async def repo(gfs_db):
    return SqliteGfsSpaceMediaRepo(gfs_db)


@pytest.fixture
def make(repo, tmp_dir):
    def _make(**over) -> GfsSpaceMediaStore:
        return GfsSpaceMediaStore(
            repo, FsBlobFiles(tmp_dir / "spaces"), _limits(**over)
        )

    return _make


#: The scope-bound address each test blob was put under (its last scope).
_ADDR: dict[bytes, str] = {}


def _addr(data: bytes, space: str = "s1", kind: str = "space") -> str:
    """The blob's scope-bound address (security review v2, N1)."""
    _ADDR[data] = space_media_address(kind, space, data)
    return _ADDR[data]


def _bid(data: bytes) -> str:
    """The address *data* was last put under (else its address in s1)."""
    return _ADDR.get(data) or space_media_address("space", "s1", data)


async def _put(store, data, *, space="s1", kind="space", now=NOW, expires=None):
    return await store.put(
        scope_kind=kind,
        scope_id=space,
        blob_id=_addr(data, space, kind),
        data=data,
        expires_at=expires if expires is not None else now + 86_400,
        now=now,
    )


async def _refused(coro) -> tuple[int, str]:
    with pytest.raises(MediaRefused) as exc:
        await coro
    return exc.value.status, exc.value.reason


async def _held(repo) -> set[str]:
    return await repo.known_blob_ids(list(_ADDR.values()))


# ── Limits ───────────────────────────────────────────────────────────────


def test_limits_from_config_and_capability():
    limits = SpaceMediaLimits.from_config(GfsConfig())
    assert limits == SpaceMediaLimits(
        max_bytes=5 * 1024**3,
        ttl_s=3 * 86_400,
        space_max_bytes=256 * 1024**2,
        space_uploads_per_hour=300,
    )
    assert limits.capability() == {
        "max_blob_bytes": MAX_BLOB_BYTES,
        "ttl_s": 259_200,
        "space_max_bytes": 256 * 1024**2,
        "space_uploads_per_hour": 300,
        "scopes": ["channel", "space"],
    }
    small = SpaceMediaLimits.from_config(
        GfsConfig(space_media_max_bytes=1_000_000, space_media_ttl_seconds=3_600)
    )
    assert small.capability()["max_blob_bytes"] == 1_000_000
    off = SpaceMediaLimits.from_config(GfsConfig(space_media_max_bytes=0))
    assert off.capability() is None


# ── Put / read ──────────────────────────────────────────────────────────


async def test_put_once_read_many(make, repo):
    store = make()
    data = _blob()
    assert await _put(store, data) is True
    assert await store.read(_bid(data), now=NOW + 10) == data
    row = await repo.get_blob(_bid(data))
    assert (row.size, row.created_at, row.last_fetched_at) == (SMALL, NOW, NOW)
    assert (row.scope_kind, row.scope_id) == ("space", "s1")


async def test_a_duplicate_upload_is_a_no_op_and_keeps_the_expiry(make, repo):
    """A re-upload by the same scope stores nothing new, is not an error and
    does NOT extend the blob's expiry."""
    store = make()
    data = _blob()
    assert await _put(store, data) is True
    assert await _put(store, data, now=NOW + 60, expires=NOW + 2 * 86_400) is False
    assert (await repo.get_blob(_bid(data))).expires_at == NOW + 86_400
    assert await repo.totals() == (1, SMALL)


async def test_losing_the_insert_race_is_a_coalesced_answer(make, repo):
    """Another node stored the blob (the same scope's upload) between our
    check and our insert: we answer as a coalesced re-upload."""
    store = make()
    data = _blob()
    bid = _bid(data)
    real_get = repo.get_blob
    calls = {"n": 0}

    async def racing_get(blob_id):
        calls["n"] += 1
        if calls["n"] == 1:
            other = make()
            await _put(other, data, space="s1")
            return None
        return await real_get(blob_id)

    repo.get_blob = racing_get  # type: ignore[method-assign]
    try:
        assert await _put(store, data, space="s1") is False
    finally:
        repo.get_blob = real_get  # type: ignore[method-assign]
    assert await repo.totals() == (1, SMALL)
    assert (await repo.get_blob(bid)).scope_id == "s1"


async def test_an_expired_blob_is_replaced_by_a_fresh_upload(make, repo):
    store = make()
    data = _blob()
    await _put(store, data, expires=NOW + 10)
    assert await _put(store, data, now=NOW + 20, expires=NOW + 3600) is True
    assert (await repo.get_blob(_bid(data))).expires_at == NOW + 3600


@pytest.mark.parametrize(
    ("blob_id", "data", "reason"),
    [
        ("nope", _blob(), "bad_blob_id"),
        (None, os.urandom(1000), "not_a_padded_blob"),
        ("a" * 64, _blob(), "blob_id_mismatch"),
        ("plain", _blob(), "blob_id_mismatch"),
    ],
    ids=["id", "bucket", "sha", "unscoped_sha"],
)
async def test_shape_and_address_are_checked(make, blob_id, data, reason):
    store = make()
    status, got = await _refused(
        store.put(
            scope_kind="space",
            scope_id="s1",
            blob_id=(
                hashlib.sha256(data).hexdigest()
                if blob_id == "plain"
                else blob_id or _bid(data)
            ),
            data=data,
            expires_at=NOW + 60,
            now=NOW,
        )
    )
    assert (status, got) == (422, reason)


async def test_an_expiry_in_the_past_is_refused(make):
    assert await _refused(_put(make(), _blob(), expires=NOW)) == (422, "bad_expiry")


async def test_a_longer_expiry_is_clamped_to_the_ttl(make, repo):
    store = make(ttl_s=3_600)
    data = _blob()
    await _put(store, data, expires=NOW + 30 * 86_400)
    assert (await repo.get_blob(_bid(data))).expires_at == NOW + 3_600


async def test_a_blob_bigger_than_the_whole_cache_is_413(make):
    store = make(max_bytes=SMALL)
    assert await _refused(_put(store, _blob(BIG))) == (413, "too_large_for_cache")
    assert (await store.stats())["refused_too_large"] == 1


async def test_a_turned_off_cache_answers_503_uncounted(make):
    store = make(max_bytes=0)
    assert await _refused(_put(store, _blob())) == (503, "space_media_disabled")
    with pytest.raises(MediaRefused) as exc:
        store.admit_size(SMALL)
    assert exc.value.status == 503
    stats = await store.stats()
    assert (stats["refused_too_large"], stats["refused_quota"]) == (0, 0)


async def test_an_unknown_scope_is_refused(make):
    data = _blob()
    status = await _refused(
        make().put(
            scope_kind="room",
            scope_id="s1",
            blob_id=space_media_address("space", "s1", data),
            data=data,
            expires_at=NOW + 60,
            now=NOW,
        )
    )
    assert status == (422, "bad_scope")


# ── Upload rate: counted apart from the rows ────────────────────────────


async def test_the_upload_rate_is_429(make):
    store = make(space_uploads_per_hour=2)
    await _put(store, _blob())
    await _put(store, _blob(), now=NOW + 1)
    assert await _refused(_put(store, _blob(), now=NOW + 2)) == (
        429,
        "space_media_quota",
    )
    assert (await store.stats())["refused_quota"] == 1
    # Two hours on, the window has moved past both.
    assert await _put(store, _blob(), now=NOW + 7200) is True


async def test_self_eviction_does_not_reset_the_upload_rate(make):
    """Probe: a scope whose cap holds one blob evicts its own previous blob on
    every upload — that must not free the rate budget."""
    store = make(space_uploads_per_hour=5, space_max_bytes=SMALL)
    answers = []
    for i in range(50):
        try:
            await _put(store, _blob(), now=NOW + i)
            answers.append(201)
        except MediaRefused as exc:
            answers.append(exc.status)
    assert answers[:5] == [201] * 5
    assert set(answers[5:]) == {429}


async def test_upload_and_delete_does_not_reset_the_upload_rate(make):
    store = make(space_uploads_per_hour=5)
    answers = []
    for i in range(50):
        data = _blob()
        try:
            await _put(store, data, now=NOW + i)
            answers.append(201)
            await store.delete(_bid(data), scope_kind="space", scope_id="s1")
        except MediaRefused as exc:
            answers.append(exc.status)
    assert answers[:5] == [201] * 5
    assert set(answers[5:]) == {429}


async def test_the_rate_window_slides_over_the_hour_boundary(make):
    """The previous hour still weighs on the next one, fading out — a burst
    at the end of one hour can't be doubled at the start of the next."""
    store = make(space_uploads_per_hour=4)
    for i in range(4):
        await _put(store, _blob(), now=NOW + 3600 - 10 + i)
    # 10 s into the next hour, the last hour's 4 still count almost fully.
    assert await _refused(_put(store, _blob(), now=NOW + 3600 + 10)) == (
        429,
        "space_media_quota",
    )
    # Three quarters through it, a quarter of them still counts: 1 < 4.
    assert await _put(store, _blob(), now=NOW + 3600 + 2700) is True


async def test_a_retry_of_the_own_claim_is_not_counted(make):
    store = make(space_uploads_per_hour=1)
    data = _blob()
    await _put(store, data)
    for i in range(3):
        assert await _put(store, data, now=NOW + i) is False


async def test_the_rate_is_per_scope(make):
    store = make(space_uploads_per_hour=1)
    await _put(store, _blob())
    await _put(store, _blob(), space="s2")
    await _put(store, _blob(), kind="channel")


# ── Per-scope cap ───────────────────────────────────────────────────────


async def test_a_full_scope_evicts_its_own_least_recently_fetched(make, repo):
    store = make(space_max_bytes=2 * SMALL)
    a, b, c = _blob(), _blob(), _blob()
    other = _blob()
    await _put(store, other, space="s2", now=NOW)
    await _put(store, a, now=NOW + 1)
    await _put(store, b, now=NOW + 2)
    assert await store.read(_bid(a), now=NOW + FETCH_TOUCH_INTERVAL_S + 1) == a
    assert await _put(store, c, now=NOW + FETCH_TOUCH_INTERVAL_S + 2) is True
    assert await _held(repo) == {_bid(other), _bid(a), _bid(c)}
    stats = await store.stats()
    assert (stats["evicted_blobs"], stats["refused_quota"]) == (1, 0)


async def test_scope_eviction_frees_enough_for_a_big_blob(make, repo):
    store = make(space_max_bytes=BIG + SMALL, max_bytes=100 * BIG)
    for i in range(4):
        await _put(store, _blob(), now=NOW + i)
    big = _blob(BIG)
    assert await _put(store, big, now=NOW + 10) is True
    assert await store.read(_bid(big), now=NOW + 11) == big
    assert await repo.totals() == (2, BIG + SMALL)
    assert (await store.stats())["evicted_blobs"] == 3


async def test_expired_blobs_of_a_scope_go_without_counting_as_evictions(make):
    store = make(space_max_bytes=2 * SMALL)
    old, live = _blob(), _blob()
    await _put(store, old, now=NOW, expires=NOW + 10)
    await _put(store, live, now=NOW + 1)
    await _put(store, _blob(), now=NOW + 20)
    assert await store.read(_bid(live), now=NOW + 21) == live
    assert (await store.stats())["evicted_blobs"] == 0


async def test_a_blob_bigger_than_a_scopes_budget_is_413(make):
    store = make(space_max_bytes=SMALL)
    assert await _refused(_put(store, _blob(BIG))) == (413, "too_large_for_cache")


# ── Total cap ───────────────────────────────────────────────────────────


async def test_eviction_drops_the_least_recently_fetched_first(make, repo, tmp_dir):
    store = make(max_bytes=3 * SMALL)
    a, b, c = _blob(), _blob(), _blob()
    await _put(store, a, now=NOW)
    await _put(store, b, now=NOW + 1)
    await _put(store, c, now=NOW + 2)
    assert await store.read(_bid(a), now=NOW + FETCH_TOUCH_INTERVAL_S) == a
    d = _blob()
    await _put(store, d, now=NOW + FETCH_TOUCH_INTERVAL_S + 1)
    assert await _held(repo) == {_bid(a), _bid(c), _bid(d)}
    assert not (tmp_dir / "spaces" / _bid(b)).exists()
    stats = await store.stats()
    assert (stats["evicted_blobs"], stats["evicted_bytes"]) == (1, SMALL)


async def test_upload_drops_only_what_it_needs_not_every_expired_blob(make, repo):
    """Bulk expiry is the sweep's job: an upload over the cap drops just
    enough (expired blobs first), never the whole expired backlog."""
    store = make(max_bytes=4 * SMALL)
    olds = [_blob() for _ in range(3)]
    for i, d in enumerate(olds):
        await _put(store, d, space=f"o{i}", now=NOW, expires=NOW + 10)
    await _put(store, _blob(), now=NOW + 1)
    await _put(store, _blob(), now=NOW + 20)
    # One expired blob made room; the other two wait for the sweep.
    assert await repo.totals() == (4, 4 * SMALL)
    # Making room from expired blobs is not an eviction.
    assert (await store.stats())["evicted_blobs"] == 0


async def test_expired_blobs_go_before_live_ones_are_evicted(make):
    store = make(max_bytes=2 * SMALL)
    old, live = _blob(), _blob()
    await _put(store, old, space="x", now=NOW, expires=NOW + 10)
    await _put(store, live, now=NOW + 1)
    await _put(store, _blob(), space="y", now=NOW + 20)
    assert await store.read(_bid(live), now=NOW + 21) == live


# ── Read ────────────────────────────────────────────────────────────────


async def test_read_misses(make, repo, gfs_db):
    store = make()
    assert await store.read("nope") is None
    assert await store.read("a" * 64, now=NOW) is None
    data = _blob()
    await _put(store, data, expires=NOW + 10)
    assert await store.read(_bid(data), now=NOW + 10) is None
    fed = SqliteGfsFederationRepo(gfs_db)
    await fed.upsert_instance(
        ClientInstance(instance_id="o", display_name="O", public_key="aa" * 32)
    )
    await fed.upsert_space(
        GlobalSpace(space_id="bad", owning_instance="o", status="active")
    )
    other = _blob()
    await _put(store, other, space="bad")
    assert await store.read(_bid(other), now=NOW + 1) == other
    await fed.set_space_status("bad", "banned")
    # A ban hides the space's blobs at once (the auth refuses its uploads).
    assert await store.read(_bid(other), now=NOW + 1) is None


async def test_a_missing_file_reads_as_a_miss(make, tmp_dir):
    store = make()
    data = _blob()
    await _put(store, data)
    (tmp_dir / "spaces" / _bid(data)).unlink()
    assert await store.read(_bid(data), now=NOW + 1) is None


async def test_the_fetch_mark_moves_at_most_once_an_hour(make, repo):
    store = make()
    data = _blob()
    bid = _bid(data)
    await _put(store, data)
    for t in (1, 60, FETCH_TOUCH_INTERVAL_S - 1):
        await store.read(bid, now=NOW + t)
    assert (await repo.get_blob(bid)).last_fetched_at == NOW
    await store.read(bid, now=NOW + FETCH_TOUCH_INTERVAL_S)
    assert (await repo.get_blob(bid)).last_fetched_at == NOW + FETCH_TOUCH_INTERVAL_S


# ── Delete / sweep / stats ──────────────────────────────────────────────


async def test_delete_needs_the_claiming_scope(make, tmp_dir):
    store = make()
    data = _blob()
    bid = _bid(data)
    await _put(store, data)
    assert await store.delete(bid, scope_kind="space", scope_id="s2") is False
    assert await store.delete(bid, scope_kind="channel", scope_id="s1") is False
    assert await store.delete("nope", scope_kind="space", scope_id="s1") is False
    assert await store.delete(bid, scope_kind="space", scope_id="s1") is True
    assert await store.read(bid, now=NOW + 1) is None
    assert not (tmp_dir / "spaces" / bid).exists()


async def test_the_sweep_drops_expired_blobs_claims_and_counts(
    make, repo, tmp_dir, monkeypatch
):
    monkeypatch.setattr(sm, "PRUNE_BATCH", 2)
    store = make()
    olds = [_blob() for _ in range(5)]
    for d in olds:
        await _put(store, d, expires=NOW + 10)
    live = _blob()
    await _put(store, live, space="a")
    assert await store.sweep(NOW + 7300) == 5
    assert await repo.totals() == (1, SMALL)
    assert sorted(p.name for p in (tmp_dir / "spaces").iterdir()) == [_bid(live)]
    # Upload counts older than the previous hour are gone.
    assert await repo.upload_counts("space", "s1", bucket=(NOW + 7300) // 3600) == (
        0,
        0,
    )


async def test_the_sweep_removes_orphan_files_after_a_grace(make, repo, tmp_dir):
    """A file with no row (a crash between write and insert, a dropped row)
    and a stale temporary file go — but not one younger than the grace, which
    may be an upload still being written."""
    store = make()
    kept = _blob()
    await _put(store, kept)
    d = tmp_dir / "spaces"
    orphan = d / ("e" * 64)
    orphan.write_bytes(b"x")
    part = d / f".{'f' * 64}.0123456789abcdef.part"
    part.write_bytes(b"x")
    fresh = d / ("9" * 64)
    fresh.write_bytes(b"x")
    old = time.time() - ORPHAN_GRACE_S - 60
    os.utime(orphan, (old, old))
    os.utime(part, (old, old))
    removed = await store.sweep_orphans(now=time.time())
    assert removed == 2
    assert sorted(p.name for p in d.iterdir()) == sorted([_bid(kept), "9" * 64])


async def test_stats(make):
    store = make()
    await _put(store, _blob())
    assert await store.stats() == {
        "enabled": True,
        "stored_blobs": 1,
        "stored_bytes": SMALL,
        "max_bytes": 10 * SMALL,
        "ttl_seconds": 3 * 86_400,
        "evicted_blobs": 0,
        "evicted_bytes": 0,
        "refused_quota": 0,
        "refused_too_large": 0,
    }


# ── Round 2: concurrency, races, the sweep at scale ─────────────────────


async def _attempt(store, data, **kw) -> int:
    try:
        return 201 if await _put(store, data, **kw) else 200
    except MediaRefused as exc:
        return exc.status


async def test_concurrent_uploads_cannot_pass_the_upload_rate(make):
    """The slot is reserved atomically before the work, so 20 uploads at
    once can't all pass a read-then-write check."""
    store = make(space_uploads_per_hour=2)
    answers = await asyncio.gather(*(_attempt(store, _blob()) for _ in range(20)))
    assert sorted(answers).count(201) == 2
    assert set(answers) == {201, 429}


async def test_concurrent_uploads_cannot_pass_the_byte_caps(make, repo):
    store = make(max_bytes=3 * SMALL, space_max_bytes=2 * SMALL)
    await asyncio.gather(
        *(_attempt(store, _blob(), space=f"s{i % 2}") for i in range(20))
    )
    _, total = await repo.totals()
    assert total <= 3 * SMALL
    for scope in ("s0", "s1"):
        assert await repo.scope_bytes("space", scope) <= 2 * SMALL


async def test_a_failed_upload_gives_its_rate_slot_back(make, repo):
    store = make(space_uploads_per_hour=1)
    data = _blob()
    status, reason = await _refused(
        store.put(
            scope_kind="space",
            scope_id="s1",
            blob_id=space_media_address("space", "s1", b"other"),
            data=data,
            expires_at=NOW + 60,
            now=NOW,
        )
    )
    assert (status, reason) == (422, "blob_id_mismatch")
    assert await repo.upload_counts("space", "s1", bucket=NOW // 3600) == (0, 0)
    assert await _put(store, data) is True


async def test_a_retry_of_the_same_scope_rewrites_a_missing_file(make, tmp_dir):
    """The residual cross-node window leaves a row without its file; the
    uploading household's own retry (same scope, blob still live) must put
    the bytes back, not answer already_stored over a 404."""
    store = make()
    data = _blob()
    bid = _bid(data)
    await _put(store, data, space="s1")
    (tmp_dir / "spaces" / bid).unlink()
    assert await store.read(bid, now=NOW + 1) is None
    assert await _put(store, data, space="s1", now=NOW + 1) is False
    assert await store.read(bid, now=NOW + 2) == data


class _Files:
    """A :class:`FsBlobFiles` stand-in whose calls a test can watch."""

    def __init__(self, inner: FsBlobFiles) -> None:
        self.inner = inner
        self.on_remove = None
        self.scan_override = None

    async def write(self, blob_id, data):
        await self.inner.write(blob_id, data)

    async def read(self, blob_id):
        return await self.inner.read(blob_id)

    async def remove(self, name):
        if self.on_remove is not None:
            await self.on_remove(name)
        await self.inner.remove(name)

    async def stat(self, name):
        return await self.inner.stat(name)

    async def exists(self, blob_id):
        return await self.inner.exists(blob_id)

    async def scan(self, *, after, limit):
        if self.scan_override is not None:
            return await self.scan_override(after=after, limit=limit)
        return await self.inner.scan(after=after, limit=limit)


async def test_a_drop_removes_rows_before_files(repo, tmp_dir):
    """A crash between the two leaves an orphan file (the sweep's), never a
    row whose file is gone."""
    files = _Files(FsBlobFiles(tmp_dir / "spaces"))
    store = GfsSpaceMediaStore(repo, files, _limits(max_bytes=SMALL))
    first = _blob()
    await _put(store, first, space="a")
    removed_with_row = {}

    async def note(name):
        removed_with_row[name] = await repo.get_blob(name) is not None

    files.on_remove = note
    await _put(store, _blob(), space="b", now=NOW + 1)
    assert removed_with_row == {_bid(first): False}


async def test_the_orphan_sweep_walks_past_its_batch_and_wraps(
    make, tmp_dir, monkeypatch
):
    monkeypatch.setattr(sm, "ORPHAN_SCAN_BATCH", 2)
    store = make()
    d = tmp_dir / "spaces"
    d.mkdir()
    old = time.time() - ORPHAN_GRACE_S - 60
    names = [f"{i:064x}" for i in range(5)]
    for n in names:
        (d / n).write_bytes(b"x")
        os.utime(d / n, (old, old))
    removed = 0
    for _ in range(3):
        removed += await store.sweep_orphans(now=time.time())
    assert removed == 5
    assert list(d.iterdir()) == []
    # Past the end the cursor wraps to the start.
    assert await store._repo.sweep_cursor() == ""


async def test_only_the_lease_holder_sweeps_orphans(repo, tmp_dir):
    a = GfsSpaceMediaStore(
        repo, FsBlobFiles(tmp_dir / "spaces"), _limits(), node_id="a"
    )
    b = GfsSpaceMediaStore(
        repo, FsBlobFiles(tmp_dir / "spaces"), _limits(), node_id="b"
    )
    d = tmp_dir / "spaces"
    d.mkdir()
    orphan = d / ("e" * 64)
    orphan.write_bytes(b"x")
    old = time.time() - ORPHAN_GRACE_S - 60
    os.utime(orphan, (old, old))
    await a.sweep(NOW)
    assert not orphan.exists()
    orphan.write_bytes(b"x")
    os.utime(orphan, (old, old))
    await b.sweep(NOW + 60)  # a still holds the lease
    assert orphan.exists()
    await b.sweep(NOW + sm.ORPHAN_LEASE_S + 1)  # a's lease ran out
    assert not orphan.exists()


async def test_the_orphan_sweep_rechecks_a_file_before_removing_it(repo, tmp_dir):
    """A file listed as an old orphan, then replaced by a re-upload before
    the unlink, is kept."""
    files = _Files(FsBlobFiles(tmp_dir / "spaces"))
    store = GfsSpaceMediaStore(repo, files, _limits())
    d = tmp_dir / "spaces"
    d.mkdir()
    name = "e" * 64
    (d / name).write_bytes(b"new")  # fresh mtime: just rewritten
    old = time.time() - ORPHAN_GRACE_S - 60

    async def stale_scan(*, after, limit):
        return ([(name, old)], "") if after < name else ([], "")

    files.scan_override = stale_scan
    assert await store.sweep_orphans(now=time.time()) == 0
    assert (d / name).exists()


# ── Stage B: the orphan-sweep cursor survives a vanishing file ───────────


async def test_the_orphan_sweep_cursor_advances_past_a_vanished_file(
    make, tmp_dir, monkeypatch
):
    """One of the listed files vanishes before its stat (a busy cache):
    the cursor still moves to the last name listed, not back to the start —
    else the sweep never gets past its first batch."""
    monkeypatch.setattr(sm, "ORPHAN_SCAN_BATCH", 3)
    store = make()
    d = tmp_dir / "spaces"
    d.mkdir()
    names = sorted(f"{i:064x}" for i in range(10))
    for n in names:
        (d / n).write_bytes(b"x")  # fresh: nothing is removed
    real_stat = bs_mod.os.stat

    def flaky_stat(path, *a, **kw):
        if str(path).endswith(names[2]):
            raise FileNotFoundError(path)
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(bs_mod.os, "stat", flaky_stat)
    await store.sweep_orphans(now=time.time())
    assert await store._repo.sweep_cursor() == names[2]
    await store.sweep_orphans(now=time.time())
    assert await store._repo.sweep_cursor() == names[5]


# ── Security review F1: fair eviction, a per-address byte budget ─────────


async def test_a_flooding_scope_evicts_its_own_blobs_not_others(make, repo):
    """Probe P1: one scope uploading past the global cap must not evict
    another scope's blob in one global LRU order — the scope holding the
    most bytes loses its own least recently fetched first."""
    store = make(max_bytes=4 * SMALL)
    victim = _blob()
    await _put(store, victim, space="victim", now=NOW)
    flood = [_blob() for _ in range(8)]
    for i, d in enumerate(flood):
        await _put(store, d, space="flood", now=NOW + 1 + i)
    assert await store.read(_bid(victim), now=NOW + 20) == victim
    _, total = await repo.totals()
    assert total <= 4 * SMALL
    # The flood kept its newest.
    assert await store.read(_bid(flood[-1]), now=NOW + 20) == flood[-1]
    assert await store.read(_bid(flood[0]), now=NOW + 20) is None


async def test_eviction_takes_from_the_largest_owner_first(make, repo):
    store = make(max_bytes=4 * SMALL)
    a1, a2, a3, b1, c1 = (_blob() for _ in range(5))
    await _put(store, b1, space="b", now=NOW)  # the oldest of all
    await _put(store, a1, space="a", now=NOW + 1)
    await _put(store, a2, space="a", now=NOW + 2)
    await _put(store, a3, space="a", now=NOW + 3)
    await _put(store, c1, space="c", now=NOW + 4)
    assert await store.read(_bid(b1), now=NOW + 5) == b1
    assert await store.read(_bid(a1), now=NOW + 5) is None
    assert await store.read(_bid(a2), now=NOW + 5) == a2


async def test_an_address_has_an_upload_byte_budget(make, repo):
    """Backstop: an address uploads at most ip_bytes_per_hour bytes an hour,
    whatever scopes it uses (shared database, cluster-wide); a refusal and a
    coalesced or failed upload cost nothing."""
    store = make(ip_bytes_per_hour=2 * SMALL)
    a, b, c = _blob(), _blob(), _blob()

    async def up(data, *, space, client="198.51.100.7", now=NOW):
        return await store.put(
            scope_kind="space",
            scope_id=space,
            blob_id=_addr(data, space),
            data=data,
            expires_at=now + 86_400,
            now=now,
            client=client,
        )

    assert await up(a, space="s1") is True
    assert await up(b, space="s2") is True
    assert await _refused(up(c, space="s3")) == (429, "ip_media_quota")
    # Another address is not held back.
    assert await up(c, space="s3", client="203.0.113.9") is True
    # The scope's retry stores no bytes: it costs nothing.
    assert await up(a, space="s1", client="192.0.2.1") is False
    assert await up(b, space="s2", client="192.0.2.1") is False
    assert await up(_blob(), space="s6", client="192.0.2.1") is True
    # Two hours on, the window has moved.
    assert await up(_blob(), space="s1", now=NOW + 7200) is True


# ── Security review v2, N1: the address is bound to the scope ────────────


async def test_another_scopes_identical_ciphertext_gets_another_address(make, repo):
    """R8: a follower who fetched a space's ciphertext and uploads it under
    its own scope gets ANOTHER blob — the space's blob is untouched by
    anything that scope does (its upload, its delete, its eviction)."""
    store = make(max_bytes=4 * SMALL)
    data = _blob()
    await _put(store, data, space="victim")
    victim_id = space_media_address("space", "victim", data)
    # Under the victim's address but the attacker's scope: refused.
    assert await _refused(
        store.put(
            scope_kind="channel",
            scope_id="0f" * 16,
            blob_id=victim_id,
            data=data,
            expires_at=NOW + 60,
            now=NOW + 1,
        )
    ) == (422, "blob_id_mismatch")
    # Under its own address: a separate blob, in its own scope.
    assert await _put(store, data, space="0f" * 16, kind="channel", now=NOW + 1)
    attacker_id = space_media_address("channel", "0f" * 16, data)
    assert attacker_id != victim_id
    assert (
        await store.delete(victim_id, scope_kind="channel", scope_id="0f" * 16) is False
    )
    assert await store.delete(attacker_id, scope_kind="channel", scope_id="0f" * 16)
    held = await repo.get_blob(victim_id)
    assert held is not None
    assert (held.scope_kind, held.scope_id, held.expires_at) == (
        "space",
        "victim",
        NOW + 86_400,
    )
    assert await store.read(victim_id, now=NOW + 2) == data


async def test_a_banned_other_space_cannot_touch_the_blob(make, repo, gfs_db):
    """R9: banning another space that uploaded the same ciphertext hides and
    drops only that space's blob."""
    store = make()
    fed = SqliteGfsFederationRepo(gfs_db)
    await fed.upsert_instance(
        ClientInstance(instance_id="o", display_name="O", public_key="aa" * 32)
    )
    for sid in ("victim", "bad"):
        await fed.upsert_space(
            GlobalSpace(space_id=sid, owning_instance="o", status="active")
        )
    data = _blob()
    await _put(store, data, space="victim")
    await _put(store, data, space="bad")
    victim_id = space_media_address("space", "victim", data)
    bad_id = space_media_address("space", "bad", data)
    await fed.set_space_status("bad", "banned")
    assert await store.read(bad_id, now=NOW + 1) is None
    assert await store.withdraw_scope("space", "bad") == 1
    assert await repo.get_blob(bad_id) is None
    assert await store.read(victim_id, now=NOW + 1) == data


async def test_a_ban_hides_and_drops_the_spaces_own_blobs(make, repo, gfs_db, tmp_dir):
    store = make()
    fed = SqliteGfsFederationRepo(gfs_db)
    await fed.upsert_instance(
        ClientInstance(instance_id="o", display_name="O", public_key="aa" * 32)
    )
    await fed.upsert_space(
        GlobalSpace(space_id="bad", owning_instance="o", status="active")
    )
    a, b = _blob(), _blob()
    await _put(store, a, space="bad")
    await _put(store, b, space="bad")
    await fed.set_space_status("bad", "banned")
    assert await store.read(_bid(a), now=NOW + 1) is None
    assert await store.withdraw_scope("space", "bad") == 2
    assert await repo.totals() == (0, 0)
    assert list((tmp_dir / "spaces").iterdir()) == []


# ── Third review A3: fairness per household ──────────────────────────────


async def test_eviction_is_fair_per_household_not_per_scope(make, repo):
    """Many scopes of one household are ONE share: the household holding
    the most (counting the blob being stored) loses its own first."""
    store = make(max_bytes=6 * SMALL)

    async def up(data, space, household, now):
        return await store.put(
            scope_kind="space",
            scope_id=space,
            blob_id=_addr(data, space),
            data=data,
            expires_at=now + 86_400,
            now=now,
            owner=("household", household),
        )

    victim = [_blob() for _ in range(4)]
    for i, v in enumerate(victim):
        await up(v, "real", "home", NOW + i)
    for i in range(12):
        await up(_blob(), f"evil-{i % 6}", "evil", NOW + 10 + i)
    kept = [await store.read(_bid(v), now=NOW + 30) for v in victim]
    assert sum(k is not None for k in kept) == 3
    held = await repo.get_blob(_bid(victim[-1]))
    assert (held.owner_kind, held.owner_id) == ("household", "home")
