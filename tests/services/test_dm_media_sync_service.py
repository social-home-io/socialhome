"""Tests for the v_3 DM media-sync service.

Covers the two halves of the preview-now-sync-later flow on the
*sender* side:

* :meth:`DmMediaSyncService.build_preview` produces a base64-encoded
  WebP thumbnail for images, ``None`` for video / file kinds.
* :meth:`DmMediaSyncService.enqueue_for_message` writes outbox
  entries via the repo abstraction; :meth:`flush_once` reads them,
  dispatches a ``DM_MEDIA_BLOB`` through the federation service,
  and deletes the row on success.

In-memory fakes for the repos keep the fixture small — the
repo's SQLite implementation is covered by integration tests
elsewhere.
"""

from __future__ import annotations

import base64
import io
import pathlib
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from PIL import Image

from socialhome.domain.conversation import ConversationMessage
from socialhome.domain.federation import DeliveryResult, FederationEventType
from socialhome.repositories.dm_media_outbox_repo import DmMediaOutboxEntry
from socialhome.services.dm_media_sync_service import DmMediaSyncService


pytestmark = pytest.mark.asyncio


# ── Fakes ──────────────────────────────────────────────────────────────


@dataclass
class _Row:
    """Mutable outbox row used by the in-memory fake."""

    blob_id: str
    message_id: str
    target_instance_id: str
    bytes_path: str
    status: str = "pending"
    attempts: int = 0
    next_attempt_at: str = "2000-01-01 00:00:00"
    last_error: str | None = None


class FakeOutboxRepo:
    """In-memory outbox keyed on ``(blob_id, target_instance_id)``."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], _Row] = {}

    async def enqueue(self, *, blob_id, message_id, target_instance_id, bytes_path):
        key = (blob_id, target_instance_id)
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.rows.setdefault(
            key,
            _Row(
                blob_id=blob_id,
                message_id=message_id,
                target_instance_id=target_instance_id,
                bytes_path=bytes_path,
                next_attempt_at=now_iso,
            ),
        )

    async def list_due(self, *, limit: int = 25):
        # Mirror the SQLite implementation's filter: only rows whose
        # ``next_attempt_at`` is in the past are due. Empty / default
        # timestamps read as "way past" so freshly-enqueued rows
        # surface immediately.
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return [
            DmMediaOutboxEntry(
                blob_id=r.blob_id,
                message_id=r.message_id,
                target_instance_id=r.target_instance_id,
                bytes_path=r.bytes_path,
                status=r.status,
                attempts=r.attempts,
                next_attempt_at=r.next_attempt_at,
                last_error=r.last_error,
                created_at="",
            )
            for r in self.rows.values()
            if r.status == "pending" and r.next_attempt_at <= now_iso
        ]

    async def mark_in_flight(self, *, blob_id, target_instance_id):
        self.rows[(blob_id, target_instance_id)].status = "in_flight"

    async def delete(self, *, blob_id, target_instance_id):
        self.rows.pop((blob_id, target_instance_id), None)

    async def reschedule(
        self,
        *,
        blob_id,
        target_instance_id,
        attempts,
        next_attempt_at,
        last_error,
    ):
        r = self.rows[(blob_id, target_instance_id)]
        r.status = "pending"
        r.attempts = attempts
        r.next_attempt_at = next_attempt_at
        r.last_error = last_error

    async def mark_failed(
        self,
        *,
        blob_id,
        target_instance_id,
        last_error,
    ):
        r = self.rows[(blob_id, target_instance_id)]
        r.status = "failed"
        r.last_error = last_error

    async def reclaim_in_flight(self):
        n = 0
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for r in self.rows.values():
            if r.status == "in_flight":
                r.status = "pending"
                r.next_attempt_at = now_iso
                n += 1
        return n

    async def list_for_message(self, message_id):
        return [
            DmMediaOutboxEntry(
                blob_id=r.blob_id,
                message_id=r.message_id,
                target_instance_id=r.target_instance_id,
                bytes_path=r.bytes_path,
                status=r.status,
                attempts=r.attempts,
                next_attempt_at=r.next_attempt_at,
                last_error=r.last_error,
                created_at="",
            )
            for r in self.rows.values()
            if r.message_id == message_id
        ]


class FakeConvosRepo:
    """Just enough conversation_repo for the service's needs."""

    def __init__(self) -> None:
        self.msgs: dict[str, ConversationMessage] = {}
        self.sync_status_updates: list[tuple[str, str | None, str | None]] = []

    async def get_message(self, message_id: str):
        return self.msgs.get(message_id)

    async def update_media_sync_status(self, *, message_id, status, media_url=None):
        self.sync_status_updates.append((message_id, status, media_url))


class FakeFederation:
    """Records every media chunk handed to ``send_media_chunk``.

    The sender now routes media through ``send_media_chunk`` (which picks
    the binary ``fed-media-v1`` channel or the JSON fallback). This stub
    re-attaches the base64 ``bytes_b64`` so the recorded payload matches
    the JSON wire shape the existing assertions expect — the chunking +
    correlation behaviour under test is identical either way.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.should_fail = False

    async def send_media_chunk(
        self,
        *,
        to_instance_id,
        event_type,
        payload,
        raw_chunk,
        space_id=None,
        mesh_fallback=False,
    ):
        if self.should_fail:
            return DeliveryResult(
                instance_id=to_instance_id,
                ok=False,
                error="simulated transport failure",
            )
        rec = {**payload, "bytes_b64": base64.b64encode(raw_chunk).decode("ascii")}
        self.sent.append(
            {"to": to_instance_id, "event": event_type, "payload": rec},
        )
        return DeliveryResult(instance_id=to_instance_id, ok=True)


class _FakeVisibilityRepo:
    """Per-peer hide list — Protocol-shape fake."""

    def __init__(self) -> None:
        self._hidden: dict[str, set[str]] = {}

    def hide(self, peer: str, user_id: str) -> None:
        self._hidden.setdefault(peer, set()).add(user_id)

    async def hidden_user_ids_for_peer(self, peer: str) -> frozenset[str]:
        return frozenset(self._hidden.get(peer, set()))


# ── Helpers ────────────────────────────────────────────────────────────


def _make_test_image(media_dir: pathlib.Path, name: str = "cat.webp") -> pathlib.Path:
    """Write a tiny WebP to ``media_dir`` so the preview path resolves."""
    img = Image.new("RGB", (640, 480), color=(200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=82)
    dest = media_dir / name
    dest.write_bytes(buf.getvalue())
    return dest


@pytest.fixture
def stack(tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    convos = FakeConvosRepo()
    outbox = FakeOutboxRepo()
    fed = FakeFederation()
    svc = DmMediaSyncService(
        convos=convos,
        outbox=outbox,
        federation=fed,
        media_dir=media_dir,
    )
    # Pre-seed one media message so flush_once can read its metadata.
    convos.msgs["m1"] = ConversationMessage(
        id="m1",
        conversation_id="c1",
        sender_user_id="u-alice",
        content="",
        type="image",
        media_url="api/media/cat.webp",
        file_name="cat.jpg",
        mime_type="image/webp",
        file_size_bytes=4321,
        media_blob_id="m1",
        created_at=datetime.now(timezone.utc),
    )
    return {
        "svc": svc,
        "convos": convos,
        "outbox": outbox,
        "fed": fed,
        "media_dir": media_dir,
    }


# ── Preview-build tests ────────────────────────────────────────────────


async def test_build_preview_image_returns_b64(stack):
    """An image source yields a base64-encoded WebP under the cap."""
    _make_test_image(stack["media_dir"])
    out = await stack["svc"].build_preview(
        media_url="api/media/cat.webp",
        kind="image",
        mime_type="image/webp",
    )
    assert out is not None
    decoded = base64.b64decode(out)
    # WebP magic — see ``IMAGE_WEBP_MAGIC`` in media_constraints.
    assert decoded[:4] == b"RIFF"
    assert b"WEBP" in decoded[:16]
    # Preview should be well under 50 KB.
    assert len(decoded) < 50_000


async def test_build_preview_video_extracts_first_frame(stack, tmp_path):
    """Video preview pulls the first frame and downscales to the cap.

    We synthesise a tiny single-frame WebM via PyAV so the test
    doesn't depend on a real video file shipped under tests/. The
    output should be a WebP under the inline-envelope budget so the
    receiver can render a recognisable poster immediately.
    """
    import av
    import io

    # 3 frames of 320×240 solid colour @ 25 fps — that's about 0.12 s
    # of footage, plenty for ``generate_thumbnail`` to land the first
    # frame. VideoProcessor only needs to decode one frame.
    buf = io.BytesIO()
    out_container = av.open(buf, mode="w", format="webm")
    stream = out_container.add_stream("vp9", rate=25)
    stream.width = 320
    stream.height = 240
    stream.pix_fmt = "yuv420p"
    from PIL import Image as _Image

    for _ in range(3):
        frame = av.VideoFrame.from_image(
            _Image.new("RGB", (320, 240), color=(50, 100, 200)),
        )
        for packet in stream.encode(frame):
            out_container.mux(packet)
    for packet in stream.encode():
        out_container.mux(packet)
    out_container.close()
    video_bytes = buf.getvalue()
    assert video_bytes, "synthesised video should produce bytes"
    (stack["media_dir"] / "clip.webm").write_bytes(video_bytes)

    out = await stack["svc"].build_preview(
        media_url="api/media/clip.webm",
        kind="video",
        mime_type="video/webm",
    )
    assert out is not None
    decoded = base64.b64decode(out)
    # Output is a WebP (poster encoded by ImageProcessor's
    # downscale path); under the preview-envelope budget.
    assert decoded[:4] == b"RIFF"
    assert b"WEBP" in decoded[:16]
    assert len(decoded) < 50_000


async def test_build_preview_video_undecodable_returns_none(stack):
    """A corrupt video file falls back to ``None``.

    PyAV raises on unparseable bytes; the build_preview branch
    catches and returns ``None`` so the receiver shows the
    play-glyph placeholder instead of the bubble going empty.
    """
    (stack["media_dir"] / "broken.webm").write_bytes(b"not a video")
    out = await stack["svc"].build_preview(
        media_url="api/media/broken.webm",
        kind="video",
        mime_type="video/webm",
    )
    assert out is None


async def test_build_preview_file_returns_none(stack):
    """Files don't carry an inline thumbnail — receiver shows a glyph."""
    (stack["media_dir"] / "invoice.pdf").write_bytes(b"%PDF-1.4 ...")
    out = await stack["svc"].build_preview(
        media_url="api/media/invoice.pdf",
        kind="file",
        mime_type="application/pdf",
    )
    assert out is None


async def test_build_preview_missing_file_returns_none(stack):
    """Vanished source files don't crash."""
    out = await stack["svc"].build_preview(
        media_url="api/media/never_existed.webp",
        kind="image",
        mime_type="image/webp",
    )
    assert out is None


async def test_build_preview_rejects_path_traversal(stack):
    """Malicious ``media_url`` can't escape the media root."""
    out = await stack["svc"].build_preview(
        media_url="api/media/../../etc/passwd",
        kind="image",
        mime_type="image/jpeg",
    )
    assert out is None


# ── Outbox flush tests ─────────────────────────────────────────────────


async def test_enqueue_writes_one_row_per_target(stack):
    _make_test_image(stack["media_dir"])
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["inst-bob", "inst-carol"],
    )
    rows = await stack["outbox"].list_due()
    targets = sorted(r.target_instance_id for r in rows)
    assert targets == ["inst-bob", "inst-carol"]


async def test_flush_once_dispatches_and_deletes(stack):
    _make_test_image(stack["media_dir"])
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["inst-bob"],
    )
    shipped = await stack["svc"].flush_once()
    assert shipped == 1
    sent = stack["fed"].sent
    assert len(sent) == 1
    assert sent[0]["to"] == "inst-bob"
    assert sent[0]["event"] == FederationEventType.DM_MEDIA_BLOB
    payload = sent[0]["payload"]
    assert payload["media_blob_id"] == "m1"
    assert payload["message_id"] == "m1"
    assert payload["file_name"] == "cat.jpg"
    assert payload["mime_type"] == "image/webp"
    assert "bytes_b64" in payload
    # Outbox is empty after delete.
    rows = await stack["outbox"].list_due()
    assert rows == []


async def test_flush_once_marks_failed_after_retry_budget(stack):
    """After MAX_ATTEMPTS failed sends the row flips to 'failed'
    AND the matching message row's media_sync_status is updated."""
    from socialhome.services.dm_media_sync_service import MAX_ATTEMPTS

    _make_test_image(stack["media_dir"])
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["inst-bob"],
    )
    stack["fed"].should_fail = True
    # Run flush_once MAX_ATTEMPTS times — the row's attempts
    # increments on each call. Use the fake outbox internals to
    # reset ``next_attempt_at`` so list_due picks it up each
    # iteration without waiting for the real backoff to elapse.
    for _ in range(MAX_ATTEMPTS):
        # Manually reset the timestamp so the next flush picks it
        # up immediately. Without this, the exponential backoff
        # pushes ``next_attempt_at`` minutes into the future.
        for row in stack["outbox"].rows.values():
            row.next_attempt_at = "2000-01-01 00:00:00"
        await stack["svc"].flush_once()
    rows = await stack["outbox"].list_for_message("m1")
    assert len(rows) == 1
    assert rows[0].status == "failed"
    # And the matching ConversationMessage row's media_sync_status
    # was bumped to 'failed' so the SPA renders the footnote.
    assert ("m1", "failed", None) in stack["convos"].sync_status_updates


async def test_flush_once_handles_build_payload_failure(stack):
    """If the source file vanishes between enqueue and flush, the
    row reschedules rather than crashing the loop."""
    _make_test_image(stack["media_dir"])
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["inst-bob"],
    )
    # Yank the file from under the scheduler.
    (stack["media_dir"] / "cat.webp").unlink()
    shipped = await stack["svc"].flush_once()
    assert shipped == 0
    rows = await stack["outbox"].list_for_message("m1")
    assert len(rows) == 1
    assert rows[0].attempts == 1
    assert rows[0].status == "pending"


async def test_flush_once_no_federation_returns_zero(stack):
    """A service without federation wired silently no-ops on flush."""
    stack["svc"]._federation = None  # type: ignore[attr-defined]
    _make_test_image(stack["media_dir"])
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["inst-bob"],
    )
    assert await stack["svc"].flush_once() == 0


async def test_attach_federation_replaces_handle(stack):
    """``attach_federation`` is the post-construction setter used
    by ``app.py`` after the cycle is broken."""

    class _Other:
        async def send_event(self, **kwargs):
            return None

    other = _Other()
    stack["svc"].attach_federation(other)  # type: ignore[arg-type]
    # Sentinel: the slot now points at our new handle.
    assert stack["svc"]._federation is other  # type: ignore[attr-defined]


async def test_start_reclaims_orphaned_in_flight_rows(stack):
    """``start()`` flips stuck in_flight rows back to pending."""
    # Mark a row in_flight without sending it (simulate sender
    # crash mid-dispatch).
    _make_test_image(stack["media_dir"])
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["inst-bob"],
    )
    await stack["outbox"].mark_in_flight(
        blob_id="m1",
        target_instance_id="inst-bob",
    )
    # Confirm the row is excluded from list_due in this state.
    assert await stack["outbox"].list_due() == []
    await stack["svc"].start()
    try:
        rows = await stack["outbox"].list_for_message("m1")
        assert rows[0].status == "pending"
    finally:
        await stack["svc"].stop()


async def test_build_preview_unparseable_image_returns_none(stack):
    """When the on-disk bytes aren't a parseable image, Pillow
    raises and we return ``None`` rather than crashing."""
    # Drop a file with a real .webp extension but bogus content.
    (stack["media_dir"] / "broken.webp").write_bytes(b"not a webp")
    out = await stack["svc"].build_preview(
        media_url="api/media/broken.webp",
        kind="image",
        mime_type="image/webp",
    )
    assert out is None


async def test_enqueue_skips_when_media_url_unresolvable(stack):
    """An external / malformed media_url doesn't write any outbox row."""
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="https://external.example/foo.jpg",  # not under media_dir
        target_instance_ids=["inst-bob"],
    )
    rows = await stack["outbox"].list_for_message("m1")
    assert rows == []


async def test_enqueue_skips_when_file_missing(stack):
    """A correctly-shaped media_url whose file is gone is skipped."""
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/never_uploaded.webp",
        target_instance_ids=["inst-bob"],
    )
    rows = await stack["outbox"].list_for_message("m1")
    assert rows == []


async def test_loop_runs_at_least_one_flush(stack):
    """Drive the scheduler's ``_loop`` for a single tick.

    ``start()`` schedules an asyncio task running ``_loop``; without
    blocking on a real interval, we instead set the interval to a
    very small value, run one tick worth of asyncio time, then
    stop the loop and assert the side effect (the queued blob shipped).
    """
    _make_test_image(stack["media_dir"])
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["inst-bob"],
    )
    # Shorten the tick so the loop's wait_for completes quickly.
    stack["svc"]._interval = 0.05  # type: ignore[attr-defined]
    await stack["svc"].start()
    try:
        # Wait a moment for the loop to flush.
        import asyncio

        for _ in range(20):
            if stack["fed"].sent:
                break
            await asyncio.sleep(0.05)
    finally:
        await stack["svc"].stop()
    assert len(stack["fed"].sent) >= 1
    assert stack["fed"].sent[0]["event"] == FederationEventType.DM_MEDIA_BLOB


async def test_start_is_idempotent(stack):
    """Calling ``start()`` twice doesn't double-schedule the loop."""
    await stack["svc"].start()
    try:
        first_task = stack["svc"]._task  # type: ignore[attr-defined]
        await stack["svc"].start()
        second_task = stack["svc"]._task  # type: ignore[attr-defined]
        assert first_task is second_task
    finally:
        await stack["svc"].stop()


async def test_stop_when_not_started_is_safe(stack):
    """``stop()`` with no task running is a no-op."""
    # No prior start — should not raise.
    await stack["svc"].stop()


async def test_flush_once_reschedules_on_failure(stack, monkeypatch):
    _make_test_image(stack["media_dir"])
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["inst-bob"],
    )
    stack["fed"].should_fail = True
    # Pin the jitter to its *minimum* — the worst case for the
    # "no rows due-now" assertion below. Equal jitter guarantees at
    # least half the 30 s first window, so even this roll keeps the
    # row out of the due set. (It used to be full jitter, which rolled
    # below ~1 s often enough to make this test flake; the fix is in
    # ``socialhome.services.backoff``, not a pinned mid-range value.)
    monkeypatch.setattr(
        "socialhome.services.backoff.random.uniform",
        lambda lo, _hi: lo,
    )
    shipped = await stack["svc"].flush_once()
    assert shipped == 0
    # No rows due-now (reschedule pushed the timestamp ≥ 15 s out).
    immediate = await stack["outbox"].list_due()
    assert immediate == []
    all_rows = await stack["outbox"].list_for_message("m1")
    assert len(all_rows) == 1
    assert all_rows[0].attempts == 1
    assert all_rows[0].status == "pending"
    assert all_rows[0].last_error is not None


async def test_flush_once_chunks_large_files(stack):
    """A file above ``SINGLE_CHUNK_BYTES_THRESHOLD`` ships as N chunks.

    Every chunk lands as its own ``DM_MEDIA_BLOB`` envelope with a
    monotonic ``chunk_index`` and the same ``chunk_count``. Only
    the last chunk carries ``final=true``.
    """
    from socialhome.services.dm_media_sync_service import (
        MAX_BLOB_CHUNK_BYTES,
        SINGLE_CHUNK_BYTES_THRESHOLD,
    )

    # Write a file just over the single-chunk threshold so the
    # split lands at 5 chunks (4 full + 1 short).
    target_size = SINGLE_CHUNK_BYTES_THRESHOLD + 4 * MAX_BLOB_CHUNK_BYTES
    big = stack["media_dir"] / "big.bin"
    big.write_bytes(b"x" * target_size)
    # Seed a matching message row so flush_once can read metadata.
    stack["convos"].msgs["m-big"] = ConversationMessage(
        id="m-big",
        conversation_id="c1",
        sender_user_id="u-alice",
        content="",
        type="file",
        media_url="api/media/big.bin",
        file_name="big.bin",
        mime_type="application/octet-stream",
        file_size_bytes=target_size,
        media_blob_id="m-big",
        created_at=datetime.now(timezone.utc),
    )
    await stack["svc"].enqueue_for_message(
        message_id="m-big",
        media_url="api/media/big.bin",
        target_instance_ids=["inst-bob"],
    )
    shipped = await stack["svc"].flush_once()
    assert shipped == 1

    sent = stack["fed"].sent
    assert len(sent) >= 2, "large file must split into multiple chunks"
    # Every send went to the same peer and shared the same
    # blob_id + chunk_count.
    chunk_count_seen = {s["payload"]["chunk_count"] for s in sent}
    assert chunk_count_seen == {len(sent)}, (
        f"chunk_count mismatch: {chunk_count_seen} vs N={len(sent)}"
    )
    indices = [s["payload"]["chunk_index"] for s in sent]
    assert indices == list(range(len(sent))), "chunks must dispatch in order"
    # Only the last chunk is ``final=true``.
    assert all(s["payload"]["final"] is False for s in sent[:-1])
    assert sent[-1]["payload"]["final"] is True
    # And the row was deleted after the full batch landed.
    rows = await stack["outbox"].list_for_message("m-big")
    assert rows == []


async def test_flush_once_single_chunk_keeps_legacy_shape(stack):
    """Files under the threshold ride one envelope (back-compat).

    The chunk-metadata fields are still present but read as a
    single-chunk transfer — receivers on older builds that don't
    inspect them treat the payload as a complete file.
    """
    _make_test_image(stack["media_dir"])
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["inst-bob"],
    )
    await stack["svc"].flush_once()
    sent = stack["fed"].sent
    assert len(sent) == 1
    assert sent[0]["payload"]["chunk_index"] == 0
    assert sent[0]["payload"]["chunk_count"] == 1
    assert sent[0]["payload"]["final"] is True


# ── Visibility-gate tests ──────────────────────────────────────────────


async def test_dm_media_blob_suppressed_when_sender_hidden(stack):
    """DM_MEDIA_BLOB is dropped (not retried) when the receiving peer
    has hidden the sender."""
    vis = _FakeVisibilityRepo()
    vis.hide("peer-hider", "u-alice")
    _make_test_image(stack["media_dir"])
    # Seed outbox row for peer-hider (sender is u-alice, set in stack fixture).
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["peer-hider"],
    )
    # Rebuild the service with visibility_repo wired.
    svc = DmMediaSyncService(
        convos=stack["convos"],
        outbox=stack["outbox"],
        federation=stack["fed"],
        media_dir=stack["media_dir"],
        visibility_repo=vis,
    )
    shipped = await svc.flush_once()
    # Nothing was sent.
    assert shipped == 0
    assert stack["fed"].sent == []
    # The row was deleted (not rescheduled) — hidden blobs are dropped forever.
    rows = await stack["outbox"].list_for_message("m1")
    assert rows == []


async def test_dm_media_blob_no_repo_passes_through(stack):
    """When visibility_repo is None the send proceeds normally."""
    _make_test_image(stack["media_dir"])
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["inst-bob"],
    )
    # stack fixture builds the service with visibility_repo=None (default).
    shipped = await stack["svc"].flush_once()
    assert shipped == 1
    assert len(stack["fed"].sent) >= 1
    assert stack["fed"].sent[0]["event"] == FederationEventType.DM_MEDIA_BLOB


async def test_chunks_pipeline_bounded_by_window(stack):
    """Multi-chunk blobs ship concurrently, capped at PIPELINE_WINDOW."""
    import asyncio

    from socialhome.services.dm_media_sync_service import PIPELINE_WINDOW

    class _TrackingFed(FakeFederation):
        def __init__(self):
            super().__init__()
            self.inflight = 0
            self.max_inflight = 0

        async def send_media_chunk(
            self,
            *,
            to_instance_id,
            event_type,
            payload,
            raw_chunk,
            space_id=None,
            mesh_fallback=False,
        ):
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            await asyncio.sleep(0.01)  # hold the slot so overlap is observable
            self.inflight -= 1
            return await super().send_media_chunk(
                to_instance_id=to_instance_id,
                event_type=event_type,
                payload=payload,
                raw_chunk=raw_chunk,
                space_id=space_id,
                mesh_fallback=mesh_fallback,
            )

    fed = _TrackingFed()
    stack["svc"].attach_federation(fed)
    # 4 MiB file with a 512 KiB chunk size → 8 chunks (well over the window).
    (stack["media_dir"] / "big.bin").write_bytes(b"\0" * (4 * 1024 * 1024))
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/big.bin",
        target_instance_ids=["inst-bob"],
    )
    shipped = await stack["svc"].flush_once()
    assert shipped == 1
    assert len(fed.sent) == 8  # all chunks delivered
    assert 1 < fed.max_inflight <= PIPELINE_WINDOW  # pipelined, but bounded


async def test_one_failed_chunk_reschedules_whole_row(stack):
    """If any chunk fails, the row is rescheduled (not deleted)."""
    fed = stack["fed"]
    fed.should_fail = True
    (stack["media_dir"] / "big.bin").write_bytes(b"\0" * (4 * 1024 * 1024))
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/big.bin",
        target_instance_ids=["inst-bob"],
    )
    shipped = await stack["svc"].flush_once()
    assert shipped == 0
    # Row still present (rescheduled / failed), not deleted.
    assert ("m1", "inst-bob") in stack["outbox"].rows


async def test_enqueue_sets_wake_event(stack):
    """Enqueuing media nudges the loop's wake event."""
    _make_test_image(stack["media_dir"])
    assert not stack["svc"]._wake.is_set()  # type: ignore[attr-defined]
    await stack["svc"].enqueue_for_message(
        message_id="m1",
        media_url="api/media/cat.webp",
        target_instance_ids=["inst-bob"],
    )
    assert stack["svc"]._wake.is_set()  # type: ignore[attr-defined]


async def test_loop_ships_promptly_via_wake_not_poll(stack):
    """A blob enqueued after start ships well before the long fallback
    poll — proving wake-on-enqueue, not the periodic tick, drives it."""
    import asyncio

    _make_test_image(stack["media_dir"])
    stack["svc"]._interval = 30.0  # type: ignore[attr-defined]
    await stack["svc"].start()
    try:
        await stack["svc"].enqueue_for_message(
            message_id="m1",
            media_url="api/media/cat.webp",
            target_instance_ids=["inst-bob"],
        )
        for _ in range(20):
            if stack["fed"].sent:
                break
            await asyncio.sleep(0.05)
    finally:
        await stack["svc"].stop()
    assert len(stack["fed"].sent) >= 1
