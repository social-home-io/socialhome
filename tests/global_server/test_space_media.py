"""GfsSpaceMediaStore — the encrypted space media cache: content-addressed,
short-lived, bounded by an operator byte cap with least-recently-fetched
eviction, per-space caps, a fetch mark at most once an hour."""

from __future__ import annotations

import os

import pytest

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
    GfsSpaceMediaStore,
    SpaceMediaLimits,
)
from socialhome.moment_public_media import MAX_BLOB_BYTES, PAD_BUCKETS, blob_id_of

NOW = 1_800_000_000
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


async def _put(store, data, *, space="s1", kind="space", now=NOW, expires=None):
    return await store.put(
        scope_kind=kind,
        scope_id=space,
        blob_id=blob_id_of(data),
        data=data,
        expires_at=expires if expires is not None else now + 86_400,
        now=now,
    )


async def _refused(coro) -> tuple[int, str]:
    with pytest.raises(MediaRefused) as exc:
        await coro
    return exc.value.status, exc.value.reason


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
        "scopes": ["channel", "space"],
    }
    small = SpaceMediaLimits.from_config(
        GfsConfig(space_media_max_bytes=1_000_000, space_media_ttl_seconds=3_600)
    )
    assert small.capability() == {
        "max_blob_bytes": 1_000_000,
        "ttl_s": 3_600,
        "scopes": ["channel", "space"],
    }
    assert (
        SpaceMediaLimits.from_config(GfsConfig(space_media_max_bytes=0)).capability()
        is None
    )


# ── Put / read ──────────────────────────────────────────────────────────


async def test_put_once_read_many(make, repo):
    store = make()
    data = _blob()
    assert await _put(store, data) is True
    assert await store.read(blob_id_of(data), now=NOW + 10) == data
    row = await repo.get(blob_id_of(data))
    assert (row.scope_kind, row.scope_id, row.size, row.created_at) == (
        "space",
        "s1",
        SMALL,
        NOW,
    )
    assert row.last_fetched_at == NOW


async def test_a_duplicate_upload_is_a_no_op(make, repo):
    """A re-upload of a held blob (a retry, another GFS replica) stores
    nothing new and is not an error."""
    store = make()
    data = _blob()
    assert await _put(store, data) is True
    assert await _put(store, data, now=NOW + 60, expires=NOW + 7200) is False
    assert (await repo.get(blob_id_of(data))).expires_at == NOW + 86_400
    assert await repo.totals() == (1, SMALL)


async def test_the_same_blob_under_another_space_is_a_conflict(make):
    store = make()
    data = _blob()
    await _put(store, data)
    assert await _refused(_put(store, data, space="s2")) == (409, "blob_id_taken")


async def test_an_expired_row_is_replaced_by_a_fresh_upload(make, repo):
    store = make()
    data = _blob()
    await _put(store, data, expires=NOW + 10)
    assert await _put(store, data, now=NOW + 20, expires=NOW + 3600) is True
    assert (await repo.get(blob_id_of(data))).expires_at == NOW + 3600


@pytest.mark.parametrize(
    ("blob_id", "data", "reason"),
    [
        ("nope", _blob(), "bad_blob_id"),
        (None, os.urandom(1000), "not_a_padded_blob"),
        ("a" * 64, _blob(), "blob_id_mismatch"),
    ],
    ids=["id", "bucket", "sha"],
)
async def test_shape_and_address_are_checked(make, blob_id, data, reason):
    store = make()
    status, got = await _refused(
        store.put(
            scope_kind="space",
            scope_id="s1",
            blob_id=blob_id or blob_id_of(data),
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
    assert (await repo.get(blob_id_of(data))).expires_at == NOW + 3_600


async def test_a_blob_bigger_than_the_whole_cache_is_413(make):
    store = make(max_bytes=SMALL)
    assert await _refused(_put(store, _blob(BIG))) == (413, "too_large_for_cache")
    assert (await store.stats())["refused_too_large"] == 1


async def test_a_turned_off_cache_refuses_everything(make):
    store = make(max_bytes=0)
    assert await _refused(_put(store, _blob())) == (413, "too_large_for_cache")


# ── Per-scope caps ──────────────────────────────────────────────────────


async def test_a_full_scope_evicts_its_own_least_recently_fetched(make, repo):
    """A busy space keeps its newest pictures: past ``space_max_bytes`` its
    own least recently fetched (then oldest) blobs go — never another
    scope's, never a 429."""
    store = make(space_max_bytes=2 * SMALL)
    a, b, c = _blob(), _blob(), _blob()
    other = _blob()
    await _put(store, other, space="s2", now=NOW)
    await _put(store, a, now=NOW + 1)
    await _put(store, b, now=NOW + 2)
    # ``a`` is fetched an hour later, so ``b`` is the least recently used.
    assert await store.read(blob_id_of(a), now=NOW + FETCH_TOUCH_INTERVAL_S + 1) == a
    assert await _put(store, c, now=NOW + FETCH_TOUCH_INTERVAL_S + 2) is True
    held = {m.blob_id for m in await repo.least_recently_fetched(limit=10)}
    assert held == {blob_id_of(other), blob_id_of(a), blob_id_of(c)}
    stats = await store.stats()
    assert (stats["evicted_blobs"], stats["refused_quota"]) == (1, 0)


async def test_scope_eviction_frees_enough_for_a_big_blob(make, repo):
    store = make(space_max_bytes=BIG + SMALL, max_bytes=100 * BIG)
    for i in range(4):
        await _put(store, _blob(), now=NOW + i)
    big = _blob(BIG)
    assert await _put(store, big, now=NOW + 10) is True
    assert await store.read(blob_id_of(big), now=NOW + 11) == big
    # Three of the four small blobs went; the newest one still fits.
    assert await repo.totals() == (2, BIG + SMALL)
    assert (await store.stats())["evicted_blobs"] == 3


async def test_expired_blobs_of_a_scope_go_before_live_ones(make):
    store = make(space_max_bytes=2 * SMALL)
    old, live = _blob(), _blob()
    await _put(store, old, now=NOW, expires=NOW + 10)
    await _put(store, live, now=NOW + 1)
    await _put(store, _blob(), now=NOW + 20)
    assert await store.read(blob_id_of(live), now=NOW + 21) == live
    assert (await store.stats())["evicted_blobs"] == 0


async def test_a_blob_bigger_than_a_scopes_budget_is_413(make):
    store = make(space_max_bytes=SMALL)
    assert await _refused(_put(store, _blob(BIG))) == (413, "too_large_for_cache")


async def test_a_channel_is_its_own_scope(make, repo):
    """A private space's channel has its own budget, apart from any space
    with the same id string."""
    store = make(space_max_bytes=SMALL)
    ch = "0f" * 16
    a, b = _blob(), _blob()
    await _put(store, a, space=ch, kind="channel", now=NOW)
    await _put(store, b, space=ch, kind="space", now=NOW + 1)
    held = {m.blob_id for m in await repo.least_recently_fetched(limit=10)}
    assert held == {blob_id_of(a), blob_id_of(b)}
    assert await _refused(_put(store, a, space=ch, kind="space")) == (
        409,
        "blob_id_taken",
    )


async def test_an_unknown_scope_is_refused(make):
    assert await _refused(_put(make(), _blob(), kind="room")) == (422, "bad_scope")


async def test_the_per_space_upload_rate_is_429(make):
    store = make(space_uploads_per_hour=2)
    await _put(store, _blob())
    await _put(store, _blob(), now=NOW + 1)
    assert await _refused(_put(store, _blob(), now=NOW + 2)) == (
        429,
        "space_media_quota",
    )
    assert await _put(store, _blob(), now=NOW + 3601) is True


# ── Total cap: eviction ──────────────────────────────────────────────────


async def test_eviction_drops_the_least_recently_fetched_first(make, repo, tmp_dir):
    store = make(max_bytes=3 * SMALL)
    a, b, c = _blob(), _blob(), _blob()
    await _put(store, a, now=NOW)
    await _put(store, b, now=NOW + 1)
    await _put(store, c, now=NOW + 2)
    # ``a`` is fetched an hour later: it is now the most recently used.
    assert await store.read(blob_id_of(a), now=NOW + FETCH_TOUCH_INTERVAL_S) == a
    d = _blob()
    await _put(store, d, now=NOW + FETCH_TOUCH_INTERVAL_S + 1)
    held = {m.blob_id for m in await repo.least_recently_fetched(limit=10)}
    assert held == {blob_id_of(a), blob_id_of(c), blob_id_of(d)}
    # The evicted blob's file is gone too.
    assert not (tmp_dir / "spaces" / blob_id_of(b)).exists()
    stats = await store.stats()
    assert (stats["evicted_blobs"], stats["evicted_bytes"]) == (1, SMALL)


async def test_eviction_frees_as_much_as_a_big_blob_needs(make, repo):
    store = make(max_bytes=BIG + SMALL)
    for i in range(5):
        await _put(store, _blob(), now=NOW + i)
    big = _blob(BIG)
    assert await _put(store, big, now=NOW + 10) is True
    assert await repo.totals() == (2, BIG + SMALL)
    assert await store.read(blob_id_of(big), now=NOW + 11) == big


async def test_expired_blobs_go_before_live_ones_are_evicted(make, repo):
    store = make(max_bytes=2 * SMALL)
    old, live = _blob(), _blob()
    await _put(store, old, now=NOW, expires=NOW + 10)
    await _put(store, live, now=NOW + 1)
    await _put(store, _blob(), now=NOW + 20)
    assert await store.read(blob_id_of(live), now=NOW + 21) == live
    assert (await store.stats())["evicted_blobs"] == 0


# ── Read ────────────────────────────────────────────────────────────────


async def test_read_misses(make, repo, gfs_db):
    store = make()
    assert await store.read("nope") is None
    assert await store.read("a" * 64, now=NOW) is None
    data = _blob()
    await _put(store, data, expires=NOW + 10)
    assert await store.read(blob_id_of(data), now=NOW + 10) is None
    # A banned space's blobs are not served.
    fed = SqliteGfsFederationRepo(gfs_db)
    await fed.upsert_instance(
        ClientInstance(instance_id="o", display_name="O", public_key="aa" * 32)
    )
    await fed.upsert_space(
        GlobalSpace(space_id="bad", owning_instance="o", status="banned")
    )
    other = _blob()
    await _put(store, other, space="bad")
    assert await store.read(blob_id_of(other), now=NOW + 1) is None


async def test_a_missing_file_reads_as_a_miss(make, tmp_dir):
    store = make()
    data = _blob()
    await _put(store, data)
    (tmp_dir / "spaces" / blob_id_of(data)).unlink()
    assert await store.read(blob_id_of(data), now=NOW + 1) is None


async def test_the_fetch_mark_moves_at_most_once_an_hour(make, repo):
    store = make()
    data = _blob()
    bid = blob_id_of(data)
    await _put(store, data)
    for t in (1, 60, FETCH_TOUCH_INTERVAL_S - 1):
        await store.read(bid, now=NOW + t)
    assert (await repo.get(bid)).last_fetched_at == NOW
    await store.read(bid, now=NOW + FETCH_TOUCH_INTERVAL_S)
    assert (await repo.get(bid)).last_fetched_at == NOW + FETCH_TOUCH_INTERVAL_S


# ── Delete / prune / stats ───────────────────────────────────────────────


async def test_delete_needs_the_owning_space(make, tmp_dir):
    store = make()
    data = _blob()
    bid = blob_id_of(data)
    await _put(store, data)
    assert await store.delete(bid, scope_kind="space", scope_id="s2") is False
    assert await store.delete(bid, scope_kind="channel", scope_id="s1") is False
    assert await store.delete("nope", scope_kind="space", scope_id="s1") is False
    assert await store.delete(bid, scope_kind="space", scope_id="s1") is True
    assert await store.read(bid, now=NOW + 1) is None
    assert not (tmp_dir / "spaces" / bid).exists()


async def test_prune_drops_expired_rows_and_files(make, repo, tmp_dir, monkeypatch):
    monkeypatch.setattr(sm, "PRUNE_BATCH", 2)
    store = make()
    olds = [_blob() for _ in range(5)]
    for d in olds:
        await _put(store, d, expires=NOW + 10)
    live = _blob()
    await _put(store, live)
    assert await store.prune_expired(NOW + 10) == 5
    assert await repo.totals() == (1, SMALL)
    assert sorted(p.name for p in (tmp_dir / "spaces").iterdir()) == [blob_id_of(live)]


async def test_stats(make):
    store = make()
    await _put(store, _blob())
    stats = await store.stats()
    assert stats == {
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
