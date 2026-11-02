"""The :class:`MediaTranscodeJob` row dataclass is a frozen DTO."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.media_transcode import MediaTranscodeJob, VideoProbe


def test_media_transcode_job_is_frozen():
    job = MediaTranscodeJob(
        output_filename="out.webm",
        source_path="/tmp/src",
        thumbnail_filename="poster.webp",
        kind="video",
        owner_user_id="u-1",
        status="pending",
        attempts=0,
        next_attempt_at="2026-06-04 00:00:00",
        last_error=None,
        created_at="2026-06-04 00:00:00",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        job.status = "processing"  # type: ignore[misc]


def test_a_job_has_no_length_unless_its_upload_was_checked():
    job = MediaTranscodeJob(
        output_filename="out.webm",
        source_path="/tmp/src",
        thumbnail_filename="poster.webp",
        kind="video",
        owner_user_id=None,
        status="pending",
        attempts=0,
        next_attempt_at="2026-06-04 00:00:00",
        last_error=None,
        created_at=None,
    )
    assert job.duration_s is None
    assert dataclasses.replace(job, duration_s=299.5).duration_s == 299.5


def test_video_probe_is_frozen_with_day_only_defaults():
    probe = VideoProbe(duration_s=12.5, width=1920, height=1080)
    assert (probe.rotation, probe.taken_on) == (0, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        probe.width = 1  # type: ignore[misc]
