"""Tests for :class:`RelayPolicy` (§Momentum-relay-policy).

Two negative signals — instance ban + open content report — short
circuit ``allow_relay``. The clean path returns True even when the
optional ``author_user_id`` / ``target_id`` are absent.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from socialhome.domain.report import (
    ContentReport,
    ReportCategory,
    ReportStatus,
    ReportTargetType,
)
from socialhome.repositories.instance_ban_repo import (
    SqliteHouseholdInstanceBanRepo,
)
from socialhome.repositories.report_repo import SqliteReportRepo
from socialhome.services.relay_policy import RelayPolicy


@pytest.fixture
async def policy(db):
    bans = SqliteHouseholdInstanceBanRepo(db)
    reports = SqliteReportRepo(db)
    return RelayPolicy(ban_repo=bans, report_repo=reports), bans, reports


async def test_allow_relay_returns_true_on_clean_envelope(policy):
    p, _, _ = policy
    assert (
        await p.allow_relay(
            source_instance_id="peer-x",
            author_user_id="u-1",
            target_id="m-1",
        )
        is True
    )


async def test_banned_instance_short_circuits(policy):
    p, bans, _ = policy
    await bans.add(instance_id="bad-inst", reason="spam")
    assert (
        await p.allow_relay(
            source_instance_id="bad-inst",
            author_user_id="u-1",
            target_id="m-1",
        )
        is False
    )


async def test_open_report_on_moment_short_circuits(policy):
    p, _, reports = policy
    await reports.save(
        ContentReport(
            id="r-1",
            target_type=ReportTargetType.MOMENT,
            target_id="m-1",
            reporter_user_id="u-2",
            reporter_instance_id="self",
            category=ReportCategory.SPAM,
            notes=None,
            status=ReportStatus.PENDING,
            created_at=datetime(2026, 5, 6, 12, tzinfo=UTC),
            resolved_by=None,
            resolved_at=None,
        )
    )
    assert (
        await p.allow_relay(
            source_instance_id="peer-x",
            author_user_id="u-1",
            target_id="m-1",
        )
        is False
    )


async def test_open_report_on_user_short_circuits(policy):
    p, _, reports = policy
    await reports.save(
        ContentReport(
            id="r-2",
            target_type=ReportTargetType.USER,
            target_id="u-bad",
            reporter_user_id="u-2",
            reporter_instance_id="self",
            category=ReportCategory.HARASSMENT,
            notes=None,
            status=ReportStatus.PENDING,
            created_at=datetime(2026, 5, 6, 12, tzinfo=UTC),
            resolved_by=None,
            resolved_at=None,
        )
    )
    assert (
        await p.allow_relay(
            source_instance_id="peer-x",
            author_user_id="u-bad",
            target_id="m-99",
        )
        is False
    )


async def test_resolved_report_does_not_block_relay(policy):
    p, _, reports = policy
    await reports.save(
        ContentReport(
            id="r-3",
            target_type=ReportTargetType.MOMENT,
            target_id="m-3",
            reporter_user_id="u-2",
            reporter_instance_id="self",
            category=ReportCategory.SPAM,
            notes=None,
            status=ReportStatus.RESOLVED,
            created_at=datetime(2026, 5, 6, 12, tzinfo=UTC),
            resolved_by="admin",
            resolved_at=datetime(2026, 5, 6, 13, tzinfo=UTC),
        )
    )
    assert (
        await p.allow_relay(
            source_instance_id="peer-x",
            author_user_id="u-1",
            target_id="m-3",
        )
        is True
    )


async def test_no_target_id_skips_target_report_check(policy):
    p, _, _ = policy
    # No target_id → only the instance ban + author report are checked.
    assert (
        await p.allow_relay(
            source_instance_id="peer-x",
            author_user_id="u-1",
            target_id=None,
        )
        is True
    )
