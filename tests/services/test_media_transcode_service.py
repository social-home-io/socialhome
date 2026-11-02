"""Tests for the background video-transcode scheduler.

Covers :class:`MediaTranscodeService.flush_once` — the worker that
claims a due ``media_transcode_jobs`` row, reads the uploaded source
bytes off disk, transcodes them to a ``.webm`` + a ``.webp`` poster,
writes both under the media root, deletes the row + temp source, and
publishes a :class:`MediaTranscodeReady` event.

In-memory fakes for the repo + a stub video processor keep the fixture
small — the repo's SQLite implementation + the real PyAV transcode are
covered elsewhere. The scheduler loop is driven via ``flush_once``
directly so the test never waits on the periodic tick.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import aiofiles
import aiofiles.os
import pytest

from socialhome.domain.events import MediaTranscodeFailed, MediaTranscodeReady
from socialhome.domain.media_transcode import MediaTranscodeJob
from socialhome.services.media_transcode_service import (
    BACKOFF_BASE_SECONDS,
    DEFAULT_SPEED,
    MAX_ATTEMPTS,
    MediaTranscodeService,
    keep_seconds,
)

pytestmark = pytest.mark.asyncio


# ── Fakes ──────────────────────────────────────────────────────────────


@dataclass
class _Row:
    """Mutable transcode-job row used by the in-memory fake."""

    output_filename: str
    source_path: str
    thumbnail_filename: str
    owner_user_id: str | None = "user-1"
    kind: str = "video"
    status: str = "pending"
    attempts: int = 0
    next_attempt_at: str = "2000-01-01 00:00:00"
    last_error: str | None = None
    duration_s: float | None = None


class FakeTranscodeRepo:
    """In-memory transcode outbox keyed on ``output_filename``."""

    def __init__(self) -> None:
        self.rows: dict[str, _Row] = {}
        self.reclaim_calls = 0

    def add(self, row: _Row) -> None:
        self.rows[row.output_filename] = row

    async def enqueue(
        self,
        *,
        output_filename,
        source_path,
        thumbnail_filename,
        owner_user_id,
        kind="video",
        duration_s=None,
    ):
        self.rows.setdefault(
            output_filename,
            _Row(
                output_filename=output_filename,
                source_path=source_path,
                thumbnail_filename=thumbnail_filename,
                owner_user_id=owner_user_id,
                kind=kind,
                duration_s=duration_s,
            ),
        )

    @staticmethod
    def _job(r: _Row) -> MediaTranscodeJob:
        return MediaTranscodeJob(
            output_filename=r.output_filename,
            source_path=r.source_path,
            thumbnail_filename=r.thumbnail_filename,
            kind=r.kind,
            owner_user_id=r.owner_user_id,
            status=r.status,
            attempts=r.attempts,
            next_attempt_at=r.next_attempt_at,
            last_error=r.last_error,
            created_at="",
            duration_s=r.duration_s,
        )

    async def list_due(self, *, limit=10):
        now_iso = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        out = [
            self._job(r)
            for r in self.rows.values()
            if r.status == "pending" and r.next_attempt_at <= now_iso
        ]
        return out[:limit]

    async def list_queue(self, *, limit=500):
        rows = [r for r in self.rows.values() if r.status in ("pending", "processing")]
        rows.sort(key=lambda r: (r.status != "processing", r.next_attempt_at))
        return [self._job(r) for r in rows[:limit]]

    async def mark_processing(self, output_filename):
        self.rows[output_filename].status = "processing"

    async def complete(self, output_filename):
        self.rows.pop(output_filename, None)

    async def reschedule(
        self, output_filename, *, attempts, next_attempt_at, last_error
    ):
        r = self.rows[output_filename]
        r.status = "pending"
        r.attempts = attempts
        r.next_attempt_at = next_attempt_at
        r.last_error = last_error

    async def mark_failed(self, output_filename, last_error):
        r = self.rows[output_filename]
        r.status = "failed"
        r.last_error = last_error

    async def reclaim(self):
        self.reclaim_calls += 1
        n = 0
        for r in self.rows.values():
            if r.status == "processing":
                r.status = "pending"
                n += 1
        return n

    async def status_for(self, output_filenames):
        return {}


class StubProcessor:
    """Stub :class:`VideoProcessor` — file-to-file ``transcode_file`` +
    ``thumbnail_file``; records the length each job was told to keep."""

    def __init__(self) -> None:
        self.raise_on_process = False
        self.kept: list[float] = []
        self.progress: list[float] = []

    async def transcode_file(self, src, dst, *, max_duration_s, on_progress=None):
        self.kept.append(max_duration_s)
        if on_progress is not None:
            for done in self.progress:
                on_progress(done)
        async with aiofiles.open(dst, "wb") as f:
            await f.write(b"WEBM")
        if self.raise_on_process:
            raise ValueError("bad video")
        async with aiofiles.open(dst, "ab") as f:
            await f.write(b"DATA")

    async def thumbnail_file(self, path):
        return b"THUMB"


@dataclass
class FakeBus:
    """Records published domain events."""

    published: list = field(default_factory=list)

    async def publish(self, event):
        self.published.append(event)


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_service(tmp_path, repo, processor, bus):
    return MediaTranscodeService(
        repo=repo,
        media_dir=tmp_path,
        processor=processor,
        bus=bus,
        interval_seconds=0.01,
    )


async def _write_source(tmp_path, name="src.tmp", data=b"RAWVIDEO"):
    src = tmp_path / name
    async with aiofiles.open(src, "wb") as f:
        await f.write(data)
    return src


# ── Tests ───────────────────────────────────────────────────────────────


async def test_flush_once_transcodes_due_job(tmp_path):
    repo = FakeTranscodeRepo()
    processor = StubProcessor()
    bus = FakeBus()
    src = await _write_source(tmp_path)
    repo.add(
        _Row(
            output_filename="out.webm",
            source_path=str(src),
            thumbnail_filename="out.webp",
            owner_user_id="user-7",
        )
    )
    svc = _make_service(tmp_path, repo, processor, bus)

    shipped = await svc.flush_once()

    assert shipped == 1
    # Output + thumbnail written with the stub bytes.
    async with aiofiles.open(tmp_path / "out.webm", "rb") as f:
        assert await f.read() == b"WEBMDATA"
    async with aiofiles.open(tmp_path / "out.webp", "rb") as f:
        assert await f.read() == b"THUMB"
    # Row deleted (readiness == absent row).
    assert "out.webm" not in repo.rows
    # Temp source removed.
    assert not await aiofiles.os.path.isfile(src)
    # One MediaTranscodeReady with the right fields.
    assert len(bus.published) == 1
    ev = bus.published[0]
    assert isinstance(ev, MediaTranscodeReady)
    assert ev.output_filename == "out.webm"
    assert ev.thumbnail_filename == "out.webp"
    assert ev.owner_user_id == "user-7"


async def test_missing_source_fails_without_writing_or_crashing(tmp_path):
    repo = FakeTranscodeRepo()
    processor = StubProcessor()
    bus = FakeBus()
    repo.add(
        _Row(
            output_filename="out.webm",
            source_path=str(tmp_path / "does-not-exist.tmp"),
            thumbnail_filename="out.webp",
        )
    )
    svc = _make_service(tmp_path, repo, processor, bus)

    shipped = await svc.flush_once()

    assert shipped == 0
    # First failure → rescheduled, not failed terminally.
    assert repo.rows["out.webm"].status == "pending"
    assert repo.rows["out.webm"].attempts == 1
    # No output written, no event published (a reschedule must NOT emit
    # the terminal MediaTranscodeFailed frame).
    assert not await aiofiles.os.path.isfile(tmp_path / "out.webm")
    assert not await aiofiles.os.path.isfile(tmp_path / "out.webp")
    assert bus.published == []


async def test_processor_error_reschedules_then_fails_after_max_attempts(tmp_path):
    repo = FakeTranscodeRepo()
    processor = StubProcessor()
    processor.raise_on_process = True
    bus = FakeBus()
    src = await _write_source(tmp_path)
    repo.add(
        _Row(
            output_filename="out.webm",
            source_path=str(src),
            thumbnail_filename="out.webp",
        )
    )
    svc = _make_service(tmp_path, repo, processor, bus)

    # First failure → reschedule with attempts=1 and a future next_attempt_at.
    shipped = await svc.flush_once()
    assert shipped == 0
    row = repo.rows["out.webm"]
    assert row.status == "pending"
    assert row.attempts == 1
    now_iso = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    assert row.next_attempt_at > now_iso
    # A reschedule must NOT publish the terminal failed frame.
    assert bus.published == []

    # Drive remaining attempts: reset due time + advance attempts until the cap.
    for _ in range(MAX_ATTEMPTS):
        if "out.webm" not in repo.rows:
            break
        repo.rows["out.webm"].next_attempt_at = "2000-01-01 00:00:00"
        await svc.flush_once()

    # Terminal failure: row flipped to 'failed', never deleted.
    assert repo.rows["out.webm"].status == "failed"
    # Hitting the attempt cap publishes exactly one MediaTranscodeFailed
    # (mirroring the success-path MediaTranscodeReady) so the uploader's
    # SPA can flip the placeholder to the failed state immediately instead
    # of waiting for the next list fetch. No MediaTranscodeReady on failure.
    failed = [e for e in bus.published if isinstance(e, MediaTranscodeFailed)]
    assert len(failed) == 1
    assert failed[0].output_filename == "out.webm"
    assert failed[0].owner_user_id == "user-1"
    assert not any(isinstance(e, MediaTranscodeReady) for e in bus.published)


async def test_reschedule_backs_off_even_on_the_unluckiest_jitter(
    tmp_path, monkeypatch
):
    """A failed transcode is never due again on the next tick.

    Regression: ``_fail`` used full jitter (``random.uniform(0, window)``),
    so ~1 first retry in 30 drew a sub-second delay. ``next_attempt_at``
    is stored at one-second granularity and ``list_due`` picks rows with
    ``<= now``, so that row came straight back — a failing video
    re-transcoded on every tick with no backoff at all. ``random.uniform``
    is pinned to its minimum here, which is exactly that worst case.
    """
    monkeypatch.setattr(random, "uniform", lambda lo, _hi: lo)
    repo = FakeTranscodeRepo()
    processor = StubProcessor()
    processor.raise_on_process = True
    bus = FakeBus()
    src = await _write_source(tmp_path)
    repo.add(
        _Row(
            output_filename="out.webm",
            source_path=str(src),
            thumbnail_filename="out.webp",
        )
    )
    svc = _make_service(tmp_path, repo, processor, bus)

    assert await svc.flush_once() == 0

    row = repo.rows["out.webm"]
    assert row.attempts == 1
    # Half the 30 s first window — pinned, not "some time in the future".
    expected = datetime.now(UTC) + timedelta(seconds=BACKOFF_BASE_SECONDS / 2)
    assert row.next_attempt_at <= expected.strftime("%Y-%m-%d %H:%M:%S")
    assert row.next_attempt_at > datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    # The contract that actually matters: not due on the next tick.
    assert await repo.list_due() == []
    assert await svc.flush_once() == 0
    assert repo.rows["out.webm"].attempts == 1


async def test_start_reclaims_and_stop_is_clean(tmp_path):
    repo = FakeTranscodeRepo()
    processor = StubProcessor()
    bus = FakeBus()
    svc = _make_service(tmp_path, repo, processor, bus)

    await svc.start()
    assert repo.reclaim_calls == 1
    await svc.stop()
    # Idempotent stop.
    await svc.stop()


# ── Length kept, partial output, time estimate (household videos) ──────


async def test_job_keeps_its_checked_length_else_the_protocol_60s(tmp_path):
    """A household video checked at upload (``duration_s``) is converted
    whole — never cut at the protocol's 60 s; a job without one (feed,
    DM, rows from before migration 0140) keeps the 60 s cap."""
    repo = FakeTranscodeRepo()
    processor = StubProcessor()
    for name, dur in (("long.webm", 299.4), ("feed.webm", None)):
        src = await _write_source(tmp_path, name=f"{name}.src")
        repo.add(
            _Row(
                output_filename=name,
                source_path=str(src),
                thumbnail_filename=name.replace(".webm", ".webp"),
                duration_s=dur,
            )
        )
    svc = _make_service(tmp_path, repo, processor, FakeBus())

    assert await svc.flush_once() == 2
    assert processor.kept == [300.0, 60.0]


async def test_a_failed_conversion_leaves_no_partial_webm(tmp_path):
    """The WebM is written beside the source and moved into the media
    root only when complete — a failure leaves neither a served half-file
    nor a ``.part`` behind."""
    repo = FakeTranscodeRepo()
    processor = StubProcessor()
    processor.raise_on_process = True
    srcdir = tmp_path / "transcode_src"
    await aiofiles.os.makedirs(srcdir, exist_ok=True)
    src = await _write_source(srcdir, name="a.bin")
    repo.add(
        _Row(
            output_filename="out.webm",
            source_path=str(src),
            thumbnail_filename="out.webp",
        )
    )
    svc = _make_service(tmp_path, repo, processor, FakeBus())

    assert await svc.flush_once() == 0
    assert not await aiofiles.os.path.isfile(tmp_path / "out.webm")
    assert not await aiofiles.os.path.isfile(srcdir / "out.webm.part")
    # The source stays for the retry.
    assert await aiofiles.os.path.isfile(src)


async def test_estimates_walk_the_queue_in_order(tmp_path):
    """Each waiting video's "ready in" adds up the video ahead of it (its
    own included) at the learned speed; one no longer queued gets no
    entry, and one without a length counts the protocol's 60 s."""
    repo = FakeTranscodeRepo()
    for i, dur in enumerate((120.0, 30.0, None)):
        repo.add(
            _Row(
                output_filename=f"v{i}.webm",
                source_path=f"/nowhere/{i}",
                thumbnail_filename=f"v{i}.webp",
                next_attempt_at=f"2000-01-01 00:00:0{i}",
                duration_s=dur,
            )
        )
    svc = _make_service(tmp_path, repo, StubProcessor(), FakeBus())
    svc._speed = 0.5  # one second of video per two seconds

    etas = await svc.estimates(["v0.webm", "v1.webm", "v2.webm", "gone.webm"])

    assert etas == {"v0.webm": 240, "v1.webm": 300, "v2.webm": 420}
    assert await svc.estimates([]) == {}


async def test_estimate_subtracts_the_running_videos_progress(tmp_path):
    repo = FakeTranscodeRepo()
    repo.add(
        _Row(
            output_filename="run.webm",
            source_path="/x",
            thumbnail_filename="run.webp",
            status="processing",
            duration_s=100.0,
        )
    )
    repo.add(
        _Row(
            output_filename="next.webm",
            source_path="/y",
            thumbnail_filename="next.webp",
            duration_s=10.0,
        )
    )
    svc = _make_service(tmp_path, repo, StubProcessor(), FakeBus())
    svc._speed = 1.0
    svc._active = "run.webm"
    svc._active_started = time.monotonic()  # just started: learned speed
    svc._on_progress(60.0)

    assert await svc.estimates(["run.webm", "next.webm"]) == {
        "run.webm": 40,
        "next.webm": 50,
    }


async def test_running_video_pace_replaces_the_learned_speed(tmp_path):
    svc = _make_service(tmp_path, FakeTranscodeRepo(), StubProcessor(), FakeBus())
    svc._speed = 0.25
    svc._active = "run.webm"
    svc._active_started = time.monotonic() - 40.0
    svc._on_progress(80.0)  # 2 s of video per second
    assert svc._current_speed() == pytest.approx(2.0, rel=0.05)


async def test_speed_is_learned_from_finished_jobs(tmp_path):
    """A finished job folds its pace into the speed the next estimate
    uses (bounded, so a glitch can't promise "ready now")."""
    repo = FakeTranscodeRepo()
    processor = StubProcessor()
    processor.progress = [10.0, 20.0]
    src = await _write_source(tmp_path)
    repo.add(
        _Row(
            output_filename="out.webm",
            source_path=str(src),
            thumbnail_filename="out.webp",
            duration_s=20.0,
        )
    )
    svc = _make_service(tmp_path, repo, processor, FakeBus())
    assert svc._speed == DEFAULT_SPEED

    await svc.flush_once()

    # 20 s of video in (almost) no time: the sample hits the upper bound,
    # smoothed with the starting speed.
    assert svc._speed > DEFAULT_SPEED
    assert svc._speed <= 50.0
    assert svc._active is None


async def test_keep_seconds_rounds_up_to_whole_seconds():
    job = MediaTranscodeJob(
        output_filename="a.webm",
        source_path="/a",
        thumbnail_filename="a.webp",
        kind="video",
        owner_user_id=None,
        status="pending",
        attempts=0,
        next_attempt_at="",
        last_error=None,
        created_at=None,
        duration_s=59.2,
    )
    assert keep_seconds(job) == 60.0
    assert keep_seconds(replace(job, duration_s=None)) == 60.0
    assert keep_seconds(replace(job, duration_s=300.0)) == 300.0
