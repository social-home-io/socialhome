"""Content-report repository."""

from __future__ import annotations

from datetime import datetime
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
                category, notes, status, created_at, resolved_by, resolved_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        limit: int = 200,
    ) -> list[ContentReport]:
        rows = await self._db.fetchall(
            "SELECT * FROM content_reports WHERE status=? "
            "ORDER BY created_at DESC LIMIT ?",
            (status.value, int(limit)),
        )
        return [r for r in (_row_to_report(d) for d in rows_to_dicts(rows)) if r]

    async def count_recent_by_reporter(
        self,
        reporter_user_id: str,
        *,
        hours: int = 24,
    ) -> int:
        return int(
            await self._db.fetchval(
                "SELECT COUNT(*) FROM content_reports "
                "WHERE reporter_user_id=? AND created_at > datetime('now', ?)",
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
    from datetime import timezone

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
    )
