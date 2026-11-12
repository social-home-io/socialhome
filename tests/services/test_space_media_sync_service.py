"""Tests for the space-post + gallery media bytes outbox + scheduler."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEventType,
    InstanceSource,
    PairingStatus,
)
from socialhome.domain.media_chunking import (
    FRAME_OVERHEAD_BYTES,
    HTTPS_CHUNK_BYTES,
    ROUTED_CHUNK_BYTES,
)
from socialhome.federation.federation_service import FederationService
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_media_outbox_repo import (
    SqliteSpaceMediaOutboxRepo,
)
from socialhome.services import space_media_sync_service as sms
from socialhome.services.media_file_hasher import MediaFileHasher
from socialhome.services.space_media_sync_service import (
    PIPELINE_WINDOW,
    SpaceMediaSyncService,
)

#: Successful-delivery shape the scheduler treats as a green ship.
_OK = DeliveryResult(instance_id="peer-x", ok=True)


async def _seed_space(db, *, space_id="sp-1"):
    """Insert a parent ``spaces`` row so the outbox FK doesn't trip."""
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (space_id, "S", "peer", "owner", "aa" * 32, "household", "invite_only"),
    )
    # The outbox queues only for a paired or member household of the space
    # (the enqueue guard): seat every peer these tests address.
    for peer in ("peer-a", "peer-b", "peer-e", "peer-x", "peer-tomb", "peer-mesh"):
        await db.enqueue(
            "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
            (space_id, peer),
        )


def _fed(chunk_bytes: int = HTTPS_CHUNK_BYTES) -> AsyncMock:
    """A federation stand-in whose media chunks are ``chunk_bytes``."""
    fed = AsyncMock()
    fed.mesh_media_chunk_bytes = AsyncMock(return_value=chunk_bytes)
    return fed


@pytest.fixture
async def db(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    yield db
    await db.shutdown()


@pytest.fixture
async def outbox(db):
    return SqliteSpaceMediaOutboxRepo(db)


async def test_enqueue_for_blob_writes_one_row_per_peer_per_url(
    db,
    outbox,
    tmp_path,
):
    """One row per (URL, peer) tuple — gallery items and posts both
    use this surface."""
    await _seed_space(db)
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=None,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-a", "peer-b"],
        media_urls=["api/media/img-1.webp", "api/media/img-2.webp"],
    )
    rows = await outbox.list_for_correlation("p-1")
    assert len(rows) == 4
    by_pair = {(r.blob_id, r.target_instance_id) for r in rows}
    assert by_pair == {
        ("img-1.webp", "peer-a"),
        ("img-1.webp", "peer-b"),
        ("img-2.webp", "peer-a"),
        ("img-2.webp", "peer-b"),
    }
    # Every row carries the space scope so a SPACE_DISSOLVED cascades.
    assert all(r.space_id == "sp-1" for r in rows)
    # Every row carries the soft correlation_id.
    assert all(r.correlation_id == "p-1" for r in rows)


async def test_flush_once_ships_small_file_as_single_chunk(
    db,
    outbox,
    tmp_path,
):
    """A file that fits one chunk ships as one chunk."""
    await _seed_space(db)
    body = b"WEBP-small-bytes-" * 8
    (tmp_path / "small.webp").write_bytes(body)
    fed = _fed()
    fed.send_media_chunk = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=fed,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/small.webp"],
    )
    shipped = await svc.flush_once()
    assert shipped == 1
    fed.send_media_chunk.assert_awaited_once()
    call = fed.send_media_chunk.call_args
    assert call.kwargs["to_instance_id"] == "peer-x"
    assert call.kwargs["event_type"] is FederationEventType.SPACE_MEDIA_BLOB
    payload = call.kwargs["payload"]
    assert payload["filename"] == "small.webp"
    assert payload["chunk_count"] == 1
    assert payload["chunk_index"] == 0
    assert payload["final"] is True
    assert payload["correlation_id"] == "p-1"
    assert payload["space_id"] == "sp-1"
    # v_85 — the whole file's hash, so the receiver checks the bytes.
    assert payload["h"] == hashlib.sha256(body).hexdigest()
    assert payload["h_suite"] == "sha256"
    # Bytes ride the dedicated ``raw_chunk`` arg now (binary channel, or
    # base64 on the JSON/SPACE_ROUTED fallback), not a ``bytes_b64``
    # field in the metadata.
    assert "bytes_b64" not in payload
    assert call.kwargs["raw_chunk"] == body
    assert call.kwargs["mesh_fallback"] is True
    assert await outbox.list_for_correlation("p-1") == []


async def test_flush_once_chunks_large_file(db, outbox, tmp_path):
    """A file larger than the target's chunk size splits into chunks of
    that size with strictly increasing index."""
    await _seed_space(db)
    size = HTTPS_CHUNK_BYTES * 5 + 17
    body = bytes(range(256)) * (size // 256 + 1)
    body = body[:size]
    (tmp_path / "big.webm").write_bytes(body)
    fed = _fed()
    fed.send_media_chunk = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=fed,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/big.webm"],
    )
    shipped = await svc.flush_once()
    assert shipped == 1
    assert fed.send_media_chunk.await_count == 6
    calls = fed.send_media_chunk.call_args_list
    payloads = [c.kwargs["payload"] for c in calls]
    assert [p["chunk_index"] for p in payloads] == [0, 1, 2, 3, 4, 5]
    assert all(p["chunk_count"] == 6 for p in payloads)
    assert [p["final"] for p in payloads] == [
        False,
        False,
        False,
        False,
        False,
        True,
    ]
    reassembled = b"".join(c.kwargs["raw_chunk"] for c in calls)
    assert reassembled == body


async def test_flush_once_no_federation_returns_zero(db, outbox, tmp_path):
    """Without federation attached, flush is a no-op."""
    await _seed_space(db)
    (tmp_path / "x.webp").write_bytes(b"x")
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=None,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/x.webp"],
    )
    assert await svc.flush_once() == 0
    assert len(await outbox.list_for_correlation("p-1")) == 1


async def test_flush_once_missing_file_reschedules(db, outbox, tmp_path):
    """File missing → row reschedules with backoff, not a crash."""
    await _seed_space(db)
    fed = _fed()
    fed.send_media_chunk = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=fed,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/ghost.webp"],
    )
    shipped = await svc.flush_once()
    assert shipped == 0
    fed.send_media_chunk.assert_not_awaited()
    rows = await outbox.list_for_correlation("p-1")
    assert len(rows) == 1
    assert rows[0].attempts == 1
    assert rows[0].last_error is not None


async def test_flush_once_reschedules_on_delivery_failure(
    db,
    outbox,
    tmp_path,
):
    """``send_media_chunk`` returns ``ok=False`` (no_route /
    not_confirmed / transport blip) — the scheduler must reschedule
    rather than treat that as a success."""
    await _seed_space(db)
    (tmp_path / "x.webp").write_bytes(b"x")
    fed = _fed()
    fed.send_media_chunk = AsyncMock(
        return_value=DeliveryResult(
            instance_id="peer-x",
            ok=False,
            error="no_route",
        ),
    )
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=fed,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/x.webp"],
    )
    shipped = await svc.flush_once()
    assert shipped == 0
    rows = await outbox.list_for_correlation("p-1")
    assert len(rows) == 1
    assert rows[0].attempts == 1
    assert rows[0].last_error == "no_route"


async def test_dedup_same_blob_to_same_peer(db, outbox, tmp_path):
    """ON CONFLICT(blob_id, target_instance_id) DO NOTHING — same
    blob referenced by two different correlation ids deduplicates."""
    await _seed_space(db)
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=None,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="post-a",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/shared.webp"],
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="gallery-b",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/shared.webp"],
    )
    rows_a = await outbox.list_for_correlation("post-a")
    rows_b = await outbox.list_for_correlation("gallery-b")
    assert len(rows_a) == 1
    assert len(rows_b) == 0


async def test_enqueue_for_post_back_compat_signature(db, outbox, tmp_path):
    """The ``enqueue_for_post`` alias kept for PR #440 call sites
    still works — it's a thin wrapper around ``enqueue_for_blob``."""
    await _seed_space(db)
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=None,
        media_dir=tmp_path,
    )
    await svc.enqueue_for_post(
        post_id="p-back-compat",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/x.webp"],
        space_id="sp-1",
    )
    rows = await outbox.list_for_correlation("p-back-compat")
    assert len(rows) == 1
    assert rows[0].correlation_id == "p-back-compat"
    assert rows[0].space_id == "sp-1"


class _TombstoneFedRepo:
    """``get_instance`` stand-in: every id in ``tombstones`` is an unpair
    tombstone (visible only with ``include_unpairing=True``)."""

    def __init__(self, tombstones: set[str]) -> None:
        self.tombstones = tombstones

    async def get_instance(self, iid, *, include_unpairing=False):
        if iid in self.tombstones:
            if not include_unpairing:
                return None
            return type("_Inst", (), {"status": PairingStatus.UNPAIRING})()
        return None  # a mesh-only member: no row, still gets the bytes


async def test_flush_once_drops_rows_for_an_unpair_tombstone(db, outbox, tmp_path):
    """A household we unpaired while it was offline gets our UNPAIR and
    nothing else — not even over the mesh fallback. Its row is dropped;
    other members (paired or mesh-only) still get theirs."""
    await _seed_space(db)
    (tmp_path / "small.webp").write_bytes(b"WEBP" * 8)
    fed = _fed()
    fed.send_media_chunk = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(
        outbox=outbox,
        federation=fed,
        media_dir=tmp_path,
        federation_repo=_TombstoneFedRepo({"peer-tomb"}),
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-tomb", "peer-mesh"],
        media_urls=["api/media/small.webp"],
    )
    assert await svc.flush_once() == 1
    sent_to = {c.kwargs["to_instance_id"] for c in fed.send_media_chunk.call_args_list}
    assert sent_to == {"peer-mesh"}
    assert await outbox.list_for_correlation("p-1") == []


async def test_start_drops_rows_for_households_that_are_gone(db, outbox, tmp_path):
    """Backstop for a crash mid-teardown: on start, rows whose household is
    neither paired nor seated in the space are deleted."""
    await _seed_space(db)
    svc = SpaceMediaSyncService(outbox=outbox, federation=None, media_dir=tmp_path)
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-a", "peer-b"],
        media_urls=["api/media/img-1.webp"],
    )
    await db.enqueue("DELETE FROM space_instances WHERE instance_id='peer-a'")
    await svc.start()
    try:
        rows = await outbox.list_for_correlation("p-1")
        assert [r.target_instance_id for r in rows] == ["peer-b"]
    finally:
        await svc.stop()


async def test_a_700_kb_picture_ships_in_chunks_the_targets_channel_takes(
    db, outbox, tmp_path
):
    """Sized for the target household: an older household's 256 KiB data
    channel gets chunks that fit it (a 680 KB picture sent whole never
    arrived — "Message size exceeds limit", then 413 from the inbox)."""
    await _seed_space(db)
    body = bytes(range(256)) * (700_000 // 256)
    (tmp_path / "cover.webp").write_bytes(body)
    fed = _fed(256 * 1024 - FRAME_OVERHEAD_BYTES)
    fed.send_media_chunk = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="ev-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/cover.webp"],
    )
    assert await svc.flush_once() == 1
    chunks = [c.kwargs["raw_chunk"] for c in fed.send_media_chunk.call_args_list]
    assert len(chunks) > 1
    assert all(len(c) + FRAME_OVERHEAD_BYTES <= 256 * 1024 for c in chunks)
    fed.mesh_media_chunk_bytes.assert_awaited_with("peer-x", file_size_bytes=len(body))
    assert b"".join(chunks) == body


class _Readers:
    """Stand-in for the GFS media uploader's check (v_80): which households
    take a file from their GFS's media cache."""

    def __init__(self, readers):
        self.readers = set(readers)

    async def reads_from_gfs(self, iid, filename):
        return iid in self.readers


async def test_a_link_joined_v80_reader_gets_no_byte_push(db, outbox, tmp_path):
    """A member seated from an invite link at v_80 reads the space's
    pictures from its GFS's media cache: its row is dropped at send time,
    nothing is pushed to it."""
    await _seed_space(db)
    (tmp_path / "img-1.webp").write_bytes(b"x" * 64)
    fed = _fed()
    fed.send_media_chunk = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)
    svc.attach_relay_readers(_Readers({"peer-e"}))
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-a", "peer-e"],
        media_urls=["api/media/img-1.webp"],
    )
    assert await svc.flush_once() == 1
    sent_to = {c.kwargs["to_instance_id"] for c in fed.send_media_chunk.call_args_list}
    assert sent_to == {"peer-a"}
    assert await outbox.list_for_correlation("p-1") == []


async def test_who_reads_from_the_gfs_is_decided_at_send_time(db, outbox, tmp_path):
    """Round 7: a household that looked like a GFS-cache reader when the
    post was made (a capability view not yet refreshed after its GFS
    restarted without a cache) still gets the push once the send-time
    check says it is not one — the decision is no longer frozen at
    enqueue time."""
    await _seed_space(db)
    (tmp_path / "img-1.webp").write_bytes(b"x" * 64)
    fed = _fed()
    fed.send_media_chunk = AsyncMock(return_value=_OK)
    readers = _Readers({"peer-e"})
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)
    svc.attach_relay_readers(readers)
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-e"],
        media_urls=["api/media/img-1.webp"],
    )
    readers.readers.clear()  # the fresh view: its GFS keeps no cache
    assert await svc.flush_once() == 1
    assert fed.send_media_chunk.call_args.kwargs["to_instance_id"] == "peer-e"


async def test_an_asked_for_file_is_pushed_even_to_a_gfs_reader(db, outbox, tmp_path):
    """v_92: a file a member asked for with ``SPACE_MEDIA_WANTED`` is queued
    with ``push=True`` — the requester already missed its GFS cache, so the
    "reads from its GFS" shortcut never drops it. Other rows to the same
    reader still take the shortcut."""
    await _seed_space(db)
    (tmp_path / "img-1.webp").write_bytes(b"x" * 64)
    (tmp_path / "img-2.webp").write_bytes(b"y" * 64)
    fed = _fed()
    fed.send_media_chunk = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)
    svc.attach_relay_readers(_Readers({"peer-e"}))
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-e"],
        media_urls=["api/media/img-1.webp"],
        push=True,
    )
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-2",
        target_instance_ids=["peer-e"],
        media_urls=["api/media/img-2.webp"],
    )
    assert await svc.flush_once() == 1
    sent = [
        c.kwargs["payload"]["filename"] for c in fed.send_media_chunk.call_args_list
    ]
    assert sent == ["img-1.webp"]
    # Shipped: the mark is gone, so a later ordinary row takes the shortcut.
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-e"],
        media_urls=["api/media/img-1.webp"],
    )
    assert await svc.flush_once() == 0


async def test_a_file_is_sent_under_its_wire_name_with_its_local_bytes(
    db, outbox, tmp_path
):
    """v_92: a helper holding another member's hashed file under its own
    reader name (``sg-``, reserved — a receiver refuses it by name) ships
    it under the asked-for content name, read from its local file."""
    await _seed_space(db)
    body = b"the helper's copy"
    (tmp_path / "sg-local.webp").write_bytes(body)
    fed = _fed()
    fed.send_media_chunk = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-e"],
        media_urls=["sg-local.webp"],
        push=True,
        wire_names={"sg-local.webp": "sc-content.webp"},
    )
    [row] = await outbox.list_for_correlation("p-1")
    assert row.blob_id == "sc-content.webp"
    assert row.bytes_path == str(tmp_path / "sg-local.webp")
    assert await svc.flush_once() == 1
    [call] = fed.send_media_chunk.call_args_list
    assert call.kwargs["payload"]["filename"] == "sc-content.webp"
    assert call.kwargs["raw_chunk"] == body


async def test_an_unsafe_wire_name_is_never_queued(db, outbox, tmp_path):
    await _seed_space(db)
    svc = SpaceMediaSyncService(outbox=outbox, federation=None, media_dir=tmp_path)
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-e"],
        media_urls=["img.webp"],
        wire_names={"img.webp": "../escape.webp"},
    )
    assert await outbox.list_for_correlation("p-1") == []


async def _enqueue_many_chunks(db, outbox, tmp_path, fed, *, chunks: int):
    await _seed_space(db)
    (tmp_path / "big.webp").write_bytes(b"\0" * (chunks * 1024))
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="ev-1",
        target_instance_ids=["peer-x"],
        media_urls=["api/media/big.webp"],
    )
    return svc


async def test_chunk_sends_never_exceed_the_pipeline_window(db, outbox, tmp_path):
    """Regression: every chunk became a coroutine (and a gather task) up
    front — a file in a million chunks meant a million objects at once.
    Only ``PIPELINE_WINDOW`` chunks are ever in flight, and no more tasks
    than that exist."""
    fed = _fed(1024)
    inflight = 0
    peak_inflight = 0
    peak_tasks = 0

    async def _send(**kwargs):
        nonlocal inflight, peak_inflight, peak_tasks
        inflight += 1
        peak_inflight = max(peak_inflight, inflight)
        peak_tasks = max(peak_tasks, len(asyncio.all_tasks()))
        await asyncio.sleep(0)
        inflight -= 1
        return _OK

    fed.send_media_chunk = AsyncMock(side_effect=_send)
    svc = await _enqueue_many_chunks(db, outbox, tmp_path, fed, chunks=64)
    baseline = len(asyncio.all_tasks())
    assert await svc.flush_once() == 1
    assert fed.send_media_chunk.await_count == 64
    assert 1 < peak_inflight <= PIPELINE_WINDOW
    # + 1: the recipient's own task (recipients are served in parallel).
    assert peak_tasks <= baseline + PIPELINE_WINDOW + 1


async def test_a_failed_chunk_stops_sending_the_rest(db, outbox, tmp_path):
    """The whole row is resent on a failure, so the remaining chunks of
    this attempt are not sent."""
    fed = _fed(1024)
    fed.send_media_chunk = AsyncMock(
        return_value=DeliveryResult(instance_id="peer-x", ok=False, error="boom")
    )
    svc = await _enqueue_many_chunks(db, outbox, tmp_path, fed, chunks=64)
    assert await svc.flush_once() == 0
    assert fed.send_media_chunk.await_count <= PIPELINE_WINDOW
    rows = await outbox.list_for_correlation("ev-1")
    assert len(rows) == 1 and rows[0].attempts == 1


class _StatusFedRepo:
    """``get_instance`` stand-in: ``peer-a`` is a CONFIRMED direct peer,
    every other id has no row (a member reached over the mesh)."""

    async def get_instance(self, iid, *, include_unpairing=False):
        if iid == "peer-a":
            return SimpleNamespace(
                status=PairingStatus.CONFIRMED, source=InstanceSource.MANUAL
            )
        return None


async def test_a_mesh_only_member_gets_chunks_that_fit_a_routed_envelope(
    db, outbox, tmp_path
):
    """Regression: a member reached only through another household got
    512 KiB chunks; inside ``SPACE_ROUTED`` each was ~1.24 MB, over the
    first hop's 1 MiB inbox, so the picture never arrived. The routed
    size goes to the mesh-only member; the paired one keeps 512 KiB."""
    await _seed_space(db)
    body = bytes(range(256)) * (1_200_000 // 256)
    (tmp_path / "big.webp").write_bytes(body)
    kp = generate_identity_keypair()
    fed = FederationService(
        db=MagicMock(),
        federation_repo=_StatusFedRepo(),
        outbox_repo=MagicMock(),
        key_manager=MagicMock(),
        bus=EventBus(),
        own_instance_id=derive_instance_id(kp.public_key),
        own_identity_seed=kp.private_key,
        own_identity_pk=kp.public_key,
    )
    send = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="ev-1",
        target_instance_ids=["peer-a", "peer-mesh"],
        media_urls=["api/media/big.webp"],
    )
    with patch.object(FederationService, "send_media_chunk", send):
        assert await svc.flush_once() == 2
    by_target: dict[str, list[bytes]] = {}
    for call in send.call_args_list:
        by_target.setdefault(call.kwargs["to_instance_id"], []).append(
            call.kwargs["raw_chunk"]
        )
    assert max(len(c) for c in by_target["peer-a"]) == HTTPS_CHUNK_BYTES
    assert len(by_target["peer-a"]) == 3
    assert max(len(c) for c in by_target["peer-mesh"]) == ROUTED_CHUNK_BYTES
    assert len(by_target["peer-mesh"]) == 4
    for chunks in by_target.values():
        assert b"".join(chunks) == body


async def test_the_whole_files_hash_comes_from_the_shared_cache(
    db, outbox, tmp_path, monkeypatch
):
    """#1322 review: a file shipped to many households is hashed once (the
    household hasher's cache), not once per outbox row."""
    await _seed_space(db)
    body = b"WEBP-cached-" * 8
    (tmp_path / "c.webp").write_bytes(body)
    fed = _fed()
    fed.send_media_chunk = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)
    reads: list[str] = []
    real = MediaFileHasher.hash_name

    async def _counting(self, name):
        reads.append(name)
        return await real(self, name)

    monkeypatch.setattr(MediaFileHasher, "hash_name", _counting)
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-x", "peer-b"],
        media_urls=["api/media/c.webp"],
    )
    assert await svc.flush_once() == 2
    assert reads == ["c.webp", "c.webp"]
    hashes = {c.kwargs["payload"]["h"] for c in fed.send_media_chunk.call_args_list}
    assert hashes == {hashlib.sha256(body).hexdigest()}


async def test_one_stuck_recipient_never_starves_the_others(
    db, outbox, tmp_path, monkeypatch
):
    """Round 7: rows go out one at a time, so a send that never returns
    (a mesh path that hangs) held every later recipient back. Each row now
    has its own time limit: the stuck one is rescheduled, the next one
    ships."""
    monkeypatch.setattr(sms, "CHUNK_IDLE_TIMEOUT_S", 0.05)
    await _seed_space(db)
    (tmp_path / "img.webp").write_bytes(b"x" * 64)
    hang = asyncio.Event()

    async def send(*, to_instance_id, **_kw):
        if to_instance_id == "peer-a":
            await hang.wait()  # never set
        return _OK

    fed = _fed()
    fed.send_media_chunk = AsyncMock(side_effect=send)
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-a", "peer-b"],
        media_urls=["api/media/img.webp"],
    )
    assert await asyncio.wait_for(svc.flush_once(), timeout=5) == 1
    left = await outbox.list_for_correlation("p-1")
    assert [r.target_instance_id for r in left] == ["peer-a"]
    assert left[0].status == "pending" and "timeout" in (left[0].last_error or "")


async def test_recipients_are_sent_in_parallel_one_row_each_at_a_time(
    db, outbox, tmp_path
):
    """Owner (round 7): like the GFS — one copy of the bytes, one row per
    recipient, and the recipients served in parallel (a small cap), never
    two sends to the same recipient at once."""
    await _seed_space(db)
    for name in ("one.webp", "two.webp"):
        (tmp_path / name).write_bytes(b"x" * 64)
    in_flight: dict[str, int] = {}
    peak: dict[str, int] = {"all": 0}
    both = asyncio.Event()

    async def send(*, to_instance_id, **_kw):
        in_flight[to_instance_id] = in_flight.get(to_instance_id, 0) + 1
        assert in_flight[to_instance_id] == 1  # one per recipient
        peak["all"] = max(peak["all"], sum(in_flight.values()))
        if peak["all"] >= 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=2)
        in_flight[to_instance_id] -= 1
        return _OK

    fed = _fed()
    fed.send_media_chunk = AsyncMock(side_effect=send)
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-a", "peer-b"],
        media_urls=["api/media/one.webp", "api/media/two.webp"],
    )
    assert await svc.flush_once() == 4
    assert peak["all"] == 2  # two recipients at once


async def test_a_slow_but_steady_send_completes(db, outbox, tmp_path, monkeypatch):
    """Round 8: the limit is per chunk (no progress for so long), not per
    file — a big file over a slow path that keeps moving is delivered."""
    monkeypatch.setattr(sms, "CHUNK_IDLE_TIMEOUT_S", 0.2)
    fed = _fed(1024)

    async def send(**_kw):
        await asyncio.sleep(0.02)  # each chunk well inside the limit
        return _OK

    fed.send_media_chunk = AsyncMock(side_effect=send)
    svc = await _enqueue_many_chunks(db, outbox, tmp_path, fed, chunks=64)
    # 64 chunks / window × 0.02 s > 0.2 s in total: a whole-file deadline
    # of the same size would have cut it.
    assert await svc.flush_once() == 1
    assert fed.send_media_chunk.await_count == 64


async def test_a_stalled_chunk_times_out(db, outbox, tmp_path, monkeypatch):
    monkeypatch.setattr(sms, "CHUNK_IDLE_TIMEOUT_S", 0.05)
    fed = _fed(1024)
    calls = 0

    async def send(**_kw):
        nonlocal calls
        calls += 1
        if calls == 3:
            await asyncio.Event().wait()  # stalls for good
        return _OK

    fed.send_media_chunk = AsyncMock(side_effect=send)
    svc = await _enqueue_many_chunks(db, outbox, tmp_path, fed, chunks=8)
    assert await asyncio.wait_for(svc.flush_once(), timeout=5) == 0
    (row,) = await outbox.list_for_correlation("ev-1")
    assert row.status == "pending" and "timeout" in (row.last_error or "")


async def test_files_in_flight_stay_within_a_byte_budget(
    db, outbox, tmp_path, monkeypatch
):
    """Round 8: recipients go in parallel, but the files held in memory
    for them at once stay within ``MAX_INFLIGHT_FILE_BYTES``."""
    monkeypatch.setattr(sms, "MAX_INFLIGHT_FILE_BYTES", 3000)
    await _seed_space(db)
    for name in ("one.webp", "two.webp", "three.webp"):
        (tmp_path / name).write_bytes(b"x" * 2000)
    held = 0
    peak = 0

    async def send(*, raw_chunk, **_kw):
        nonlocal held, peak
        held += 2000
        peak = max(peak, held)
        await asyncio.sleep(0.01)
        held -= 2000
        return _OK

    fed = _fed()
    fed.send_media_chunk = AsyncMock(side_effect=send)
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-a", "peer-b", "peer-x"],
        media_urls=["api/media/one.webp"],
    )
    assert await svc.flush_once() == 3
    assert peak <= 3000  # one 2000-byte file at a time under a 3000 budget


async def test_a_row_left_in_flight_is_sent_on_the_next_pass(db, outbox, tmp_path):
    """Round 8: a send that died past ``Exception`` (an inner cancellation,
    a DB error while rescheduling) left its row ``in_flight`` until the
    next restart. Every pass now takes such rows back first."""
    await _seed_space(db)
    (tmp_path / "img.webp").write_bytes(b"x" * 64)
    fed = _fed()
    fed.send_media_chunk = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-a"],
        media_urls=["api/media/img.webp"],
    )
    await outbox.mark_in_flight(blob_id="img.webp", target_instance_id="peer-a")
    await svc.flush_once()
    (row,) = await outbox.list_for_correlation("p-1")
    assert row.status == "pending"  # taken back: due again in a few seconds


# ── A mesh chunk the target could not open (SPACE_ROUTE_STALE) ────────────


async def _sent_over_the_mesh(db, outbox, tmp_path):
    await _seed_space(db)
    (tmp_path / "img.webp").write_bytes(b"picture bytes")
    fed = _fed(ROUTED_CHUNK_BYTES)
    fed.send_media_chunk = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-mesh"],
        media_urls=["api/media/img.webp"],
    )
    assert await svc.flush_once() == 1
    assert await outbox.list_for_correlation("p-1") == []
    return svc, fed


async def test_a_file_whose_mesh_chunk_was_lost_is_sent_again_once(
    db, outbox, tmp_path
):
    """A member posts a picture while the host is down: the relay takes the
    chunk (the row is done), the restarted host cannot open it and nacks —
    and the mesh never retransmits a chunk. The whole file is queued again,
    once: every chunk of the first send may be nacked, and a nack for the
    second send gives up (the receiver asks on view)."""
    svc, fed = await _sent_over_the_mesh(db, outbox, tmp_path)
    lost = {"blob_id": "img.webp", "space_id": "sp-1", "chunk_index": 0}
    await svc.on_mesh_chunk_lost("peer-mesh", lost)
    await svc.on_mesh_chunk_lost("peer-mesh", {**lost, "chunk_index": 1})
    [row] = await outbox.list_for_correlation("p-1")
    assert (row.blob_id, row.target_instance_id, row.space_id) == (
        "img.webp",
        "peer-mesh",
        "sp-1",
    )
    assert row.bytes_path == str(tmp_path / "img.webp")
    assert await svc.flush_once() == 1
    assert fed.send_media_chunk.await_count == 2
    # A late nack for the first send, or one for the second: not again.
    await svc.on_mesh_chunk_lost("peer-mesh", lost)
    assert await outbox.list_for_correlation("p-1") == []
    assert await svc.flush_once() == 0


async def test_a_chunk_reported_lost_while_the_file_is_still_being_sent(
    db, outbox, tmp_path
):
    """The host is already back and the route cached: the nack for chunk 0
    of a file in several chunks arrives while a later chunk is still being
    sent. The send still "succeeds" (the relay took every chunk) — the row
    must stay queued instead of being deleted, and go out again once."""
    await _seed_space(db)
    (tmp_path / "img.webp").write_bytes(b"x" * 64)
    fed = _fed(16)  # four chunks
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)
    lost = {"blob_id": "img.webp", "space_id": "sp-1", "chunk_index": 0}
    nacked = False

    async def _send(**kwargs):
        nonlocal nacked
        if kwargs["payload"]["chunk_index"] == 1 and not nacked:
            nacked = True
            await svc.on_mesh_chunk_lost("peer-mesh", lost)
        return _OK

    fed.send_media_chunk = AsyncMock(side_effect=_send)
    await svc.enqueue_for_blob(
        space_id="sp-1",
        correlation_id="p-1",
        target_instance_ids=["peer-mesh"],
        media_urls=["api/media/img.webp"],
    )
    await svc.flush_once()
    assert nacked
    [row] = await outbox.list_for_correlation("p-1")
    assert (row.status, row.attempts) == ("pending", 0)
    first = fed.send_media_chunk.await_count
    assert await svc.flush_once() == 1  # the one resend, at once
    assert fed.send_media_chunk.await_count == 2 * first
    assert await outbox.list_for_correlation("p-1") == []
    # A nack for the resend gives up.
    await svc.on_mesh_chunk_lost("peer-mesh", lost)
    assert await outbox.list_for_correlation("p-1") == []


async def test_a_resend_that_fails_and_is_retried_is_still_the_one_resend(
    db, outbox, tmp_path
):
    svc, fed = await _sent_over_the_mesh(db, outbox, tmp_path)
    lost = {"blob_id": "img.webp", "space_id": "sp-1", "chunk_index": 0}
    await svc.on_mesh_chunk_lost("peer-mesh", lost)
    fed.send_media_chunk = AsyncMock(
        return_value=DeliveryResult(instance_id="peer-mesh", ok=False, error="x")
    )
    assert await svc.flush_once() == 0
    await db.enqueue("UPDATE space_media_outbox SET next_attempt_at=datetime('now')")
    fed.send_media_chunk = AsyncMock(return_value=_OK)
    assert await svc.flush_once() == 1
    await svc.on_mesh_chunk_lost("peer-mesh", lost)  # for the resend: gives up
    assert await outbox.list_for_correlation("p-1") == []


async def test_a_lost_chunk_of_a_file_never_sent_from_here_queues_nothing(
    db, outbox, tmp_path
):
    svc, _ = await _sent_over_the_mesh(db, outbox, tmp_path)
    await svc.on_mesh_chunk_lost("peer-mesh", {"blob_id": "other.webp"})
    await svc.on_mesh_chunk_lost("peer-a", {"blob_id": "img.webp"})
    await svc.on_mesh_chunk_lost("peer-mesh", {})
    await svc.on_mesh_chunk_lost("peer-mesh", {"blob_id": 7})
    assert await outbox.list_for_correlation("p-1") == []


async def test_sent_rows_remembered_for_a_nack_stay_bounded(
    db, outbox, tmp_path, monkeypatch
):
    monkeypatch.setattr(sms, "MAX_SENT_MARKS", 1)
    await _seed_space(db)
    fed = _fed()
    fed.send_media_chunk = AsyncMock(return_value=_OK)
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)
    for name in ("one.webp", "two.webp"):
        (tmp_path / name).write_bytes(b"x")
        await svc.enqueue_for_blob(
            space_id="sp-1",
            correlation_id="p-1",
            target_instance_ids=["peer-mesh"],
            media_urls=[name],
        )
        assert await svc.flush_once() == 1
    await svc.on_mesh_chunk_lost("peer-mesh", {"blob_id": "one.webp"})
    assert await outbox.list_for_correlation("p-1") == []
    await svc.on_mesh_chunk_lost("peer-mesh", {"blob_id": "two.webp"})
    assert len(await outbox.list_for_correlation("p-1")) == 1


async def test_held_files_names_only_the_files_that_are_here(tmp_path):
    (tmp_path / "here.webp").write_bytes(b"x")
    svc = SpaceMediaSyncService(outbox=AsyncMock(), federation=None, media_dir=tmp_path)
    assert await svc.held_files(
        ["api/media/here.webp", "/api/media/gone.webp", "here.webp?exp=1", "../x", ""]
    ) == ["api/media/here.webp", "here.webp?exp=1"]
