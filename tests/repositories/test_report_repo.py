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


# ── Scope: household vs space (0067) ─────────────────────────────────────


def _scoped(rid: str, space_id: str | None, **kw) -> ContentReport:
    return ContentReport(
        id=rid,
        target_type=kw.pop("target_type", ReportTargetType.POST),
        target_id=kw.pop("target_id", rid),
        reporter_user_id="u-reporter",
        category=ReportCategory.SPAM,
        notes=None,
        status=ReportStatus.PENDING,
        created_at=datetime.now(timezone.utc),
        space_id=space_id,
    )


async def test_space_id_round_trips(repo):
    await repo.save(_scoped("r1", "sp1", target_type=ReportTargetType.PAGE))
    got = await repo.get("r1")
    assert got is not None
    assert got.space_id == "sp1"
    assert got.target_type is ReportTargetType.PAGE


async def test_household_listing_never_shows_space_reports(repo):
    await repo.save(_scoped("house", None))
    await repo.save(_scoped("in-a", "sp-a"))
    await repo.save(_scoped("in-b", "sp-b"))
    household = await repo.list_by_status(ReportStatus.PENDING)
    assert [r.id for r in household] == ["house"]
    in_a = await repo.list_by_status(ReportStatus.PENDING, space_id="sp-a")
    assert [r.id for r in in_a] == ["in-a"]


async def test_space_report_never_gates_household_relay(repo):
    """A member reported inside a space must not block their household
    relay (``relay_policy``) — only a household-level report does."""
    await repo.save(
        _scoped("in-space", "sp-a", target_type=ReportTargetType.USER, target_id="u9")
    )
    assert not await repo.has_open_for_target(
        target_type=ReportTargetType.USER, target_id="u9"
    )
    await repo.save(
        _scoped("house", None, target_type=ReportTargetType.USER, target_id="u9")
    )
    assert await repo.has_open_for_target(
        target_type=ReportTargetType.USER, target_id="u9"
    )


async def test_count_find_and_delete_in_space(repo):
    import dataclasses

    base = _scoped("a", "sp-a")
    await repo.save(base)
    await repo.save(
        dataclasses.replace(_scoped("b", "sp-a"), reporter_instance_id="peer")
    )
    await repo.save(
        dataclasses.replace(
            _scoped("c", "sp-a"), reporter_user_id="other", reporter_instance_id="peer"
        )
    )
    await repo.save(_scoped("d", "sp-b"))
    assert await repo.count_pending_in_space("sp-a") == 3
    assert await repo.count_pending_in_space("sp-a", reporter_user_id="u-reporter") == 2
    assert await repo.count_pending_in_space("sp-a", reporter_instance_id="peer") == 2
    assert (
        await repo.count_pending_in_space(
            "sp-a", reporter_user_id="other", reporter_instance_id="peer"
        )
        == 1
    )
    got = await repo.find_by_key(
        space_id="sp-a",
        target_type=ReportTargetType.POST,
        target_id="a",
        reporter_user_id="u-reporter",
    )
    assert got is not None and got.id == "a"
    assert await repo.delete_for_space("sp-a", remote_only=True) == 2
    assert await repo.count_pending_in_space("sp-a") == 1
    assert await repo.delete_for_space("sp-a", remote_only=False) == 1
    assert await repo.delete_for_space("sp-a", remote_only=False) == 0
    assert await repo.count_pending_in_space("sp-b") == 1


async def test_sole_reviewer_round_trips(repo):
    import dataclasses

    await repo.save(
        dataclasses.replace(_scoped("s", "sp-a"), sole_reviewer_user_id="u-own")
    )
    got = await repo.get("s")
    assert got is not None and got.sole_reviewer_user_id == "u-own"
