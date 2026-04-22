"""Voice / video call domain shapes (spec §26).

* :class:`CallSession` — persisted row in ``call_sessions``; lifecycle is
  RINGING → ACTIVE → ENDED | MISSED | DECLINED per §26.8.
* :class:`CallQualitySample` — persisted row in ``call_quality_samples``;
  one sample per ~10 s per participant for admin-side diagnosis.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class CallSession:
    """Authoritative call state persisted to ``call_sessions``.

    The in-memory :class:`socialhome.services.call_service.CallRecord`
    remains the hot path for SDP routing; this is the cold-path row used
    for history + missed-call detection + federation replay.
    """

    id: str
    conversation_id: str
    initiator_user_id: str
    callee_user_id: str | None  # None for group calls
    call_type: str  # 'audio' | 'video'
    status: str = "ringing"  # ringing|active|ended|declined|missed
    participant_user_ids: tuple[str, ...] = ()
    started_at: str = ""  # ISO-8601 (datetime('now'))
    connected_at: str | None = None
    ended_at: str | None = None
    duration_seconds: int | None = None

    def with_status(self, status: str, **extra) -> "CallSession":
        """Return a copy with the status transitioned + optional extras."""
        return copy.replace(self, status=status, **extra)


@dataclass(slots=True, frozen=True)
class CallQualitySample:
    """One WebRTC quality snapshot for a call (spec §26, CALL_QUALITY)."""

    call_id: str
    reporter_user_id: str
    sampled_at: int  # unix epoch seconds
    rtt_ms: int | None = None
    jitter_ms: int | None = None
    loss_pct: float | None = None
    audio_bitrate: int | None = None
    video_bitrate: int | None = None
