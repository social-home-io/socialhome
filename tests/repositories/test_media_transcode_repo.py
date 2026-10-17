"""Direct SQLite tests for ``SqliteMediaTranscodeRepo``.

Exercises the literal SQL + the ``0026_media_transcode_jobs``
migration against the live (fully-migrated) ``db`` fixture: this is
the implementation the async-transcode scheduler runs in production.
"""

from __future__ import annotations

import pytest

from socialhome.repositories.media_transcode_repo import (
    SqliteMediaTranscodeRepo,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def repo(db):
    return SqliteMediaTranscodeRepo(db)


async def test_enqueue_then_list_due_roundtrip(repo):
    await repo.enqueue(
        output_filename="a.webm",
        source_path="/tmp/a.src",
        thumbnail_filename="a.webp",
        owner_user_id="u-1",
    )
    due = await repo.list_due()
    assert len(due) == 1
    job = due[0]
    assert job.output_filename == "a.webm"
    assert job.source_path == "/tmp/a.src"
    assert job.thumbnail_filename == "a.webp"
    assert job.kind == "video"
    assert job.owner_user_id == "u-1"
    assert job.status == "pending"
    assert job.attempts == 0


async def test_mark_processing_then_reclaim_resets_to_pending(repo):
    await repo.enqueue(
        output_filename="b.webm",
        source_path="/tmp/b.src",
        thumbnail_filename="b.webp",
        owner_user_id="u-1",
    )
    await repo.mark_processing("b.webm")
    # processing rows are not due
    assert await repo.list_due() == []

    reclaimed = await repo.reclaim()
    assert reclaimed == 1

    due = await repo.list_due()
    assert [j.output_filename for j in due] == ["b.webm"]
    assert due[0].status == "pending"


async def test_complete_deletes_row(repo):
    await repo.enqueue(
        output_filename="c.webm",
        source_path="/tmp/c.src",
        thumbnail_filename="c.webp",
        owner_user_id="u-1",
    )
    await repo.complete("c.webm")
    assert await repo.list_due() == []
    # ready = absent from status map
    assert await repo.status_for(["c.webm"]) == {}


async def test_reschedule_bumps_attempts_and_defers(repo):
    await repo.enqueue(
        output_filename="d.webm",
        source_path="/tmp/d.src",
        thumbnail_filename="d.webp",
        owner_user_id="u-1",
    )
    await repo.reschedule(
        "d.webm",
        attempts=2,
        next_attempt_at="2099-01-01 00:00:00",
        last_error="boom",
    )
    # deferred far into the future -> not due
    assert await repo.list_due() == []
    # still tracked as in-progress (pending) for status_for
    assert await repo.status_for(["d.webm"]) == {"d.webm": "processing"}


async def test_mark_failed_then_status_for_returns_failed(repo):
    await repo.enqueue(
        output_filename="e.webm",
        source_path="/tmp/e.src",
        thumbnail_filename="e.webp",
        owner_user_id="u-1",
    )
    await repo.mark_failed("e.webm", "no codec")
    assert await repo.status_for(["e.webm"]) == {"e.webm": "failed"}
    # failed rows are not due for processing
    assert await repo.list_due() == []


async def test_status_for_mixed_filenames(repo):
    await repo.enqueue(
        output_filename="pending.webm",
        source_path="/tmp/p.src",
        thumbnail_filename="p.webp",
        owner_user_id="u-1",
    )
    await repo.enqueue(
        output_filename="processing.webm",
        source_path="/tmp/pr.src",
        thumbnail_filename="pr.webp",
        owner_user_id="u-1",
    )
    await repo.mark_processing("processing.webm")
    await repo.enqueue(
        output_filename="failed.webm",
        source_path="/tmp/f.src",
        thumbnail_filename="f.webp",
        owner_user_id="u-1",
    )
    await repo.mark_failed("failed.webm", "x")

    result = await repo.status_for(
        ["pending.webm", "processing.webm", "failed.webm", "absent.webm"]
    )
    assert result == {
        "pending.webm": "processing",
        "processing.webm": "processing",
        "failed.webm": "failed",
    }


async def test_status_for_empty_returns_empty(repo):
    assert await repo.status_for([]) == {}


async def test_active_source_paths_empty_when_no_rows(repo):
    assert await repo.active_source_paths() == set()


async def test_active_source_paths_returns_all_source_paths(repo):
    await repo.enqueue(
        output_filename="a.webm",
        source_path="/tmp/transcode_src/a.bin",
        thumbnail_filename="a.webp",
        owner_user_id="u-1",
    )
    await repo.enqueue(
        output_filename="b.webm",
        source_path="/tmp/transcode_src/b.bin",
        thumbnail_filename="b.webp",
        owner_user_id="u-1",
    )
    # A processing row's source is still referenced — must be included.
    await repo.mark_processing("b.webm")
    # A failed row's source is still referenced until cleaned up.
    await repo.enqueue(
        output_filename="c.webm",
        source_path="/tmp/transcode_src/c.bin",
        thumbnail_filename="c.webp",
        owner_user_id="u-1",
    )
    await repo.mark_failed("c.webm", "x")

    assert await repo.active_source_paths() == {
        "/tmp/transcode_src/a.bin",
        "/tmp/transcode_src/b.bin",
        "/tmp/transcode_src/c.bin",
    }
