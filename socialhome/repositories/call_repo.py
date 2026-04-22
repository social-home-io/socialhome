"""Call session + call quality repositories (spec §26).

``call_sessions`` is the cold-path history + missed-call surface for DM
calls; the hot path (live SDP routing) is the in-memory ``CallRecord``
in :mod:`socialhome.services.call_service`. Both are updated on each
state transition.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.call import CallQualitySample, CallSession
from .base import dump_json, load_json, row_to_dict, rows_to_dicts


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@runtime_checkable
class AbstractCallRepo(Protocol):
    async def save_call(self, call: CallSession) -> CallSession: ...
    async def get_call(self, call_id: str) -> CallSession | None: ...
    async def list_active(self, *, user_id: str) -> list[CallSession]: ...
    async def list_history_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
    ) -> list[CallSession]: ...
    async def transition(
        self,
        call_id: str,
        *,
        status: str,
        connected_at: str | None = None,
        ended_at: str | None = None,
        duration_seconds: int | None = None,
        participant_user_ids: tuple[str, ...] | None = None,
    ) -> CallSession | None: ...
    async def end_stale_calls(
        self,
        *,
        older_than_seconds: int = 90,
    ) -> list[CallSession]: ...
    async def save_quality_sample(
        self,
        sample: CallQualitySample,
    ) -> None: ...
    async def list_quality_samples(
        self,
        call_id: str,
    ) -> list[CallQualitySample]: ...


class SqliteCallRepo:
    """SQLite-backed :class:`AbstractCallRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Call sessions ──────────────────────────────────────────────────

    async def save_call(self, call: CallSession) -> CallSession:
        """Upsert a call row; inserts or updates by primary key."""
        await self._db.enqueue(
            """
            INSERT INTO call_sessions(
                id, conversation_id, initiator_user_id, callee_user_id,
                call_type, status, participant_user_ids,
                started_at, connected_at, ended_at, duration_seconds
            ) VALUES(?, ?, ?, ?, ?, ?, ?,
                     COALESCE(?, datetime('now')), ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status               = excluded.status,
                participant_user_ids = excluded.participant_user_ids,
                connected_at         = excluded.connected_at,
                ended_at             = excluded.ended_at,
                duration_seconds     = excluded.duration_seconds
            """,
            (
                call.id,
                call.conversation_id,
                call.initiator_user_id,
                call.callee_user_id,
                call.call_type,
                call.status,
                dump_json(list(call.participant_user_ids)),
                call.started_at or None,
                call.connected_at,
                call.ended_at,
                call.duration_seconds,
            ),
        )
        # Re-read so the caller sees DB defaults (started_at).
        fresh = await self.get_call(call.id)
        return fresh if fresh is not None else call

    async def get_call(self, call_id: str) -> CallSession | None:
        row = await self._db.fetchone(
            "SELECT * FROM call_sessions WHERE id=?",
            (call_id,),
        )
        d = row_to_dict(row)
        return _row_to_session(d)

    async def list_active(self, *, user_id: str) -> list[CallSession]:
        """Return active (ringing|active) calls involving *user_id*.

        Either the initiator or a recorded participant counts. The JSON
        LIKE match is coarse but fine — ``participant_user_ids`` is a
        JSON array of ids so the id literal appears with quotes.
        """
        rows = await self._db.fetchall(
            """
            SELECT * FROM call_sessions
             WHERE status IN ('ringing','active')
               AND (initiator_user_id=? OR callee_user_id=?
                    OR participant_user_ids LIKE ?)
             ORDER BY started_at DESC
            """,
            (user_id, user_id, f'%"{user_id}"%'),
        )
        return [_row_to_session(d) for d in rows_to_dicts(rows) if d]  # type: ignore[misc]

    async def list_history_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
    ) -> list[CallSession]:
        rows = await self._db.fetchall(
            """
            SELECT * FROM call_sessions
             WHERE conversation_id=?
             ORDER BY started_at DESC
             LIMIT ?
            """,
            (conversation_id, limit),
        )
        return [_row_to_session(d) for d in rows_to_dicts(rows) if d]  # type: ignore[misc]

    async def transition(
        self,
        call_id: str,
        *,
        status: str,
        connected_at: str | None = None,
        ended_at: str | None = None,
        duration_seconds: int | None = None,
        participant_user_ids: tuple[str, ...] | None = None,
    ) -> CallSession | None:
        """Apply a status transition + optional lifecycle timestamps.

        Returns the post-transition row or ``None`` if the call was
        already deleted (FK cascade from conversation removal).
        """
        existing = await self.get_call(call_id)
        if existing is None:
            return None
        parts_sql = dump_json(
            list(
                participant_user_ids
                if participant_user_ids is not None
                else existing.participant_user_ids
            )
        )
        await self._db.enqueue(
            """
            UPDATE call_sessions
               SET status               = ?,
                   connected_at         = COALESCE(?, connected_at),
                   ended_at             = COALESCE(?, ended_at),
                   duration_seconds     = COALESCE(?, duration_seconds),
                   participant_user_ids = ?
             WHERE id = ?
            """,
            (
                status,
                connected_at,
                ended_at,
                duration_seconds,
                parts_sql,
                call_id,
            ),
        )
        return await self.get_call(call_id)

    async def end_stale_calls(
        self,
        *,
        older_than_seconds: int = 90,
    ) -> list[CallSession]:
        """Mark ringing calls past the TTL as ``missed`` and return them.

        Uses SQLite's ``strftime('%s', …)`` to compute epoch age and
        ``ended_at = datetime('now')`` atomically. The returned rows are
        the freshly-missed calls — callers emit the missed-call system
        message + push on each.
        """
        stale_rows = await self._db.fetchall(
            """
            SELECT * FROM call_sessions
             WHERE status = 'ringing'
               AND (strftime('%s','now') - strftime('%s', started_at)) > ?
            """,
            (older_than_seconds,),
        )
        stale_ids = [r["id"] for r in stale_rows]
        for cid in stale_ids:
            await self._db.enqueue(
                "UPDATE call_sessions SET status='missed', "
                "ended_at=datetime('now') WHERE id=? AND status='ringing'",
                (cid,),
            )
        out: list[CallSession] = []
        for cid in stale_ids:
            sess = await self.get_call(cid)
            if sess is not None:
                out.append(sess)
        return out

    # ── Quality samples ────────────────────────────────────────────────

    async def save_quality_sample(
        self,
        sample: CallQualitySample,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO call_quality_samples(
                call_id, reporter_user_id, sampled_at,
                rtt_ms, jitter_ms, loss_pct,
                audio_bitrate, video_bitrate
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample.call_id,
                sample.reporter_user_id,
                sample.sampled_at,
                sample.rtt_ms,
                sample.jitter_ms,
                sample.loss_pct,
                sample.audio_bitrate,
                sample.video_bitrate,
            ),
        )

    async def list_quality_samples(
        self,
        call_id: str,
    ) -> list[CallQualitySample]:
        rows = await self._db.fetchall(
            """
            SELECT * FROM call_quality_samples
             WHERE call_id=?
             ORDER BY sampled_at ASC
            """,
            (call_id,),
        )
        return [
            CallQualitySample(
                call_id=r["call_id"],
                reporter_user_id=r["reporter_user_id"],
                sampled_at=r["sampled_at"],
                rtt_ms=r["rtt_ms"],
                jitter_ms=r["jitter_ms"],
                loss_pct=r["loss_pct"],
                audio_bitrate=r["audio_bitrate"],
                video_bitrate=r["video_bitrate"],
            )
            for r in rows
        ]


def _row_to_session(d: dict | None) -> CallSession | None:
    if d is None:
        return None
    parts = load_json(d.get("participant_user_ids"), [])
    return CallSession(
        id=d["id"],
        conversation_id=d.get("conversation_id") or "",
        initiator_user_id=d["initiator_user_id"],
        callee_user_id=d.get("callee_user_id"),
        call_type=d["call_type"],
        status=d["status"],
        participant_user_ids=tuple(parts),
        started_at=d.get("started_at") or "",
        connected_at=d.get("connected_at"),
        ended_at=d.get("ended_at"),
        duration_seconds=d.get("duration_seconds"),
    )
