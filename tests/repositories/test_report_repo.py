"""Regression: ``count_recent_by_reporter`` must normalise both sides of
its window comparison through SQLite's ``datetime()``.

``content_reports.created_at`` is written by ``report_service`` as
tz-aware ISO 8601 (``2026-09-18T02:00:00+00:00``) while
``datetime('now', '-24 hours')`` yields SQLite's naive
``2026-09-17 16:00:00`` shape. SQLite compares TEXT lexicographically and
``"T"`` (0x54) sorts above ``" "`` (0x20), so a raw compare counted
reports from *outside* the window whenever the two calendar dates
matched — falsely tripping the per-day report cap for reporters who were
well under it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.report import (
    ContentReport,
    ReportCategory,
    ReportStatus,
    ReportTargetType,
)
from socialhome.repositories.report_repo import SqliteReportRepo


@pytest.fixture
async def repo(db):
    return SqliteReportRepo(db)


def _report(rid: str, created_at: datetime) -> ContentReport:
    return ContentReport(
        id=rid,
        target_type=ReportTargetType.POST,
        target_id="p-1",
        reporter_user_id="u-reporter",
        category=ReportCategory.SPAM,
        notes=None,
        status=ReportStatus.PENDING,
        created_at=created_at,
    )


async def test_report_older_than_window_is_not_counted(repo):
    """A report from outside the window never counts toward the cap.

    Pinned to the very start of the cutoff's own UTC day so the stale
    report's *calendar date* always ties with ``datetime('now', '-24
    hours')`` — the only window where the raw TEXT compare went wrong, so
    the regression fires regardless of the wall clock.
    """
    cutoff_day = (datetime.now(timezone.utc) - timedelta(hours=24)).date()
    stale = datetime.fromisoformat(f"{cutoff_day.isoformat()}T00:00:00.000001+00:00")
    await repo.save(_report("r-stale", stale))
    assert await repo.count_recent_by_reporter("u-reporter", hours=24) == 0


async def test_report_inside_window_is_counted(repo):
    """A report filed minutes ago still counts toward the cap."""
    now = datetime.now(timezone.utc)
    await repo.save(_report("r-fresh", now - timedelta(minutes=5)))
    assert await repo.count_recent_by_reporter("u-reporter", hours=24) == 1


async def test_window_counts_only_the_reporters_own_reports(repo):
    """The cap is per-reporter, not global."""
    now = datetime.now(timezone.utc)
    await repo.save(_report("r-mine", now - timedelta(minutes=5)))
    other = _report("r-theirs", now - timedelta(minutes=5))
    await repo.save(
        ContentReport(
            **{
                **{f: getattr(other, f) for f in other.__dataclass_fields__},
                "reporter_user_id": "u-other",
            }
        )
    )
    assert await repo.count_recent_by_reporter("u-reporter", hours=24) == 1
    assert await repo.count_recent_by_reporter("u-other", hours=24) == 1
