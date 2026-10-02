"""Content-report repository."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.report import (
    ContentReport,
    ReportCategory,
    ReportStatus,
    ReportTargetType,
)
from .base import row_to_dict, rows_to_dicts


@runtime_checkable
class AbstractReportRepo(Protocol):
    async def save(self, report: ContentReport) -> None: ...
    async def get(self, report_id: str) -> ContentReport | None: ...
    async def list_by_status(
        self,
        status: ReportStatus,
        *,
        space_id: str | None = None,
        limit: int = 200,
    ) -> list[ContentReport]: ...
    async def count_recent_by_reporter(
        self,
        reporter_user_id: str,
        *,
        hours: int = 24,
    ) -> int: ...
    async def resolve(
        self,
        report_id: str,
        *,
        resolved_by: str,
        status: ReportStatus = ReportStatus.RESOLVED,
    ) -> None: ...
    async def has_open_for_target(
        self,
        *,
        target_type: ReportTargetType,
        target_id: str,
    ) -> bool: ...
    async def count_pending_in_space(
        self,
        space_id: str,
        *,
        reporter_user_id: str | None = None,
        reporter_instance_id: str | None = None,
    ) -> int: ...
    async def find_by_key(
        self,
        *,
        space_id: str,
        target_type: ReportTargetType,
        target_id: str,
        reporter_user_id: str,
    ) -> ContentReport | None: ...
    async def delete_for_space(self, space_id: str, *, remote_only: bool) -> int: ...


class SqliteReportRepo:
    """SQLite-backed :class:`AbstractReportRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save(self, report: ContentReport) -> None:
        await self._db.enqueue(
            """
            INSERT INTO content_reports(
                id, target_type, target_id, reporter_user_id,
                reporter_instance_id,
                category, notes, status, created_at, resolved_by, resolved_at,
                space_id, sole_reviewer_user_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.id,
                report.target_type.value,
                report.target_id,
                report.reporter_user_id,
                report.reporter_instance_id,
                report.category.value,
                report.notes,
                report.status.value,
                _iso(report.created_at),
                report.resolved_by,
                _iso(report.resolved_at),
                report.space_id,
                report.sole_reviewer_user_id,
            ),
        )

    async def get(self, report_id: str) -> ContentReport | None:
        row = await self._db.fetchone(
            "SELECT * FROM content_reports WHERE id=?",
            (report_id,),
        )
        return _row_to_report(row_to_dict(row))

    async def list_by_status(
        self,
        status: ReportStatus,
        *,
        space_id: str | None = None,
        limit: int = 200,
    ) -> list[ContentReport]:
        """Reports in one scope: ``space_id=None`` lists the household-level
        reports ONLY (never a space's), otherwise that space's."""
        if space_id is None:
            rows = await self._db.fetchall(
                "SELECT * FROM content_reports WHERE status=? AND space_id IS NULL "
                "ORDER BY created_at DESC LIMIT ?",
                (status.value, int(limit)),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM content_reports WHERE status=? AND space_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (status.value, space_id, int(limit)),
            )
        return [r for r in (_row_to_report(d) for d in rows_to_dicts(rows)) if r]

    async def count_recent_by_reporter(
        self,
        reporter_user_id: str,
        *,
        hours: int = 24,
    ) -> int:
        # ``content_reports.created_at`` is written by the service as
        # tz-aware ISO 8601 while ``datetime('now', ?)`` yields SQLite's
        # naive shape; a raw TEXT compare ("T" 0x54 > " " 0x20) counted
        # reports from *outside* the window whenever the calendar dates
        # matched, falsely tripping the per-day report cap. Normalise both.
        return int(
            await self._db.fetchval(
                "SELECT COUNT(*) FROM content_reports "
                "WHERE reporter_user_id=? "
                "AND datetime(created_at) > datetime('now', ?)",
                (reporter_user_id, f"-{int(hours)} hours"),
                default=0,
            )
        )

    async def resolve(
        self,
        report_id: str,
        *,
        resolved_by: str,
        status: ReportStatus = ReportStatus.RESOLVED,
    ) -> None:
        await self._db.enqueue(
            "UPDATE content_reports SET status=?, resolved_by=?, "
            "resolved_at=datetime('now') WHERE id=?",
            (status.value, resolved_by, report_id),
        )

    async def has_open_for_target(
        self,
        *,
        target_type: ReportTargetType,
        target_id: str,
    ) -> bool:
        """True iff at least one ``status='pending'`` report is open
        against ``(target_type, target_id)``. The §Momentum-relay-policy
        check uses this to short-circuit fan-out for moments / authors
        currently under moderation review.
        """
        if not target_id:
            return False
        # HOUSEHOLD-level reports only: a member's conduct reported inside a
        # space is that space's moderators' business and must not gate the
        # household relay (nobody outside the space could ever clear it).
        row = await self._db.fetchone(
            "SELECT 1 FROM content_reports "
            "WHERE target_type=? AND target_id=? AND status='pending' "
            "AND space_id IS NULL LIMIT 1",
            (target_type.value, target_id),
        )
        return row is not None

    async def count_pending_in_space(
        self,
        space_id: str,
        *,
        reporter_user_id: str | None = None,
        reporter_instance_id: str | None = None,
    ) -> int:
        """Pending reports in a space — optionally only one reporter's, and
        / or only those filed from one household (the inbound caps)."""
        sql = (
            "SELECT COUNT(*) FROM content_reports WHERE space_id=? AND status='pending'"
        )
        args: list = [space_id]
        if reporter_user_id is not None:
            sql += " AND reporter_user_id=?"
            args.append(reporter_user_id)
        if reporter_instance_id is not None:
            sql += " AND reporter_instance_id=?"
            args.append(reporter_instance_id)
        return int(await self._db.fetchval(sql, tuple(args), default=0))

    async def find_by_key(
        self,
        *,
        space_id: str,
        target_type: ReportTargetType,
        target_id: str,
        reporter_user_id: str,
    ) -> ContentReport | None:
        """The one report a reporter filed on a target in a space — the key
        every reviewing household shares (its row ids differ)."""
        row = await self._db.fetchone(
            "SELECT * FROM content_reports WHERE space_id=? AND target_type=? "
            "AND target_id=? AND reporter_user_id=?",
            (space_id, target_type.value, target_id, reporter_user_id),
        )
        return _row_to_report(row_to_dict(row))

    async def delete_for_space(self, space_id: str, *, remote_only: bool) -> int:
        """Drop a space's reports — with ``remote_only`` just those other
        households filed (this household no longer reviews the space)."""
        where = "space_id=?" + (
            " AND reporter_instance_id IS NOT NULL" if remote_only else ""
        )
        n = int(
            await self._db.fetchval(
                f"SELECT COUNT(*) FROM content_reports WHERE {where}",
                (space_id,),
                default=0,
            )
        )
        if n:
            await self._db.enqueue(
                f"DELETE FROM content_reports WHERE {where}", (space_id,)
            )
        return n


# ─── Helpers ──────────────────────────────────────────────────────────────


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _row_to_report(row: dict | None) -> ContentReport | None:
    if row is None:
        return None
    try:
        target_type = ReportTargetType(row["target_type"])
        category = ReportCategory(row["category"])
        status = ReportStatus(row["status"])
    except KeyError, ValueError:
        return None
    return ContentReport(
        id=row["id"],
        target_type=target_type,
        target_id=row["target_id"],
        reporter_user_id=row["reporter_user_id"],
        reporter_instance_id=row.get("reporter_instance_id"),
        category=category,
        notes=row.get("notes"),
        status=status,
        created_at=_parse(row["created_at"]) or datetime.now(timezone.utc),
        resolved_by=row.get("resolved_by"),
        resolved_at=_parse(row.get("resolved_at")),
        space_id=row.get("space_id"),
        sole_reviewer_user_id=row.get("sole_reviewer_user_id"),
    )
