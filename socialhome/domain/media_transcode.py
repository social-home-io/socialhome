"""Row-shaped DTO for the async video-transcode outbox.

Pure dataclass — no I/O, no service/repo imports. Mirrors a row of
``media_transcode_jobs`` (migration ``0026``). The transcode scheduler
consumes these and turns each into a background transcode job.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class MediaTranscodeJob:
    """One pending / in-progress / failed video transcode."""

    output_filename: str
    source_path: str
    thumbnail_filename: str
    kind: str
    owner_user_id: str | None
    status: str
    attempts: int
    next_attempt_at: str
    last_error: str | None
    created_at: str | None
    #: The source's length, checked against its use's limit at upload
    #: (migration 0140). The transcode keeps exactly this much; ``None``
    #: (feed, DM, older rows) keeps the protocol's
    #: ``VIDEO_MAX_DURATION_SECONDS``. Also what the queue's time
    #: estimate counts.
    duration_s: float | None = None


@dataclass(slots=True, frozen=True)
class VideoProbe:
    """What a video file says about itself, read before it is converted.

    ``width`` / ``height`` are the stored WebM's — the picture as it is
    shown (a portrait phone video's rotation applied) scaled to
    ``VIDEO_MAX_DIMENSION``. ``taken_on`` is the day the container says
    it was recorded (``YYYY-MM-DD``) — the day only; no time of day, and
    never any place the file carries.
    """

    duration_s: float | None
    width: int
    height: int
    rotation: int = 0
    taken_on: str | None = None
