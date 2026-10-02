"""ScheduleCalendarBridge — finalize → space calendar event."""

from __future__ import annotations


import pytest

from socialhome.domain.events import SchedulePollFinalized
from socialhome.infrastructure.event_bus import EventBus
from socialhome.domain.preferences import (
    FeatureDisabledError,
    HouseholdPreferences,
)
from socialhome.services.schedule_calendar_bridge import (
    ScheduleCalendarBridge,
)


class _FakeSpaceCalendar:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create_event(self, **kw):
        self.created.append(kw)


class _FakeFeaturesService:
    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled

    async def require_enabled(self, section):
        if self._enabled:
            return HouseholdPreferences()
        raise FeatureDisabledError(section)


@pytest.fixture
def env(request):
    enabled = getattr(request, "param", True)
    bus = EventBus()
    cal = _FakeSpaceCalendar()
    features = _FakeFeaturesService(enabled=enabled)
    bridge = ScheduleCalendarBridge(
        bus=bus,
        space_calendar_service=cal,  # type: ignore[arg-type]
        household_features=features,  # type: ignore[arg-type]
    )
    bridge.wire()
    return bus, cal


def _finalized(**kw) -> SchedulePollFinalized:
    base = dict(
        post_id="sp-1",
        slot_id="slot-A",
        slot_date="2026-05-01",
        start_time="18:00",
        end_time="20:00",
        title="Pizza night",
        finalized_by="alice",
        space_id="sp-A",
    )
    base.update(kw)
    return SchedulePollFinalized(**base)


async def test_household_only_poll_is_skipped(env):
    bus, cal = env
    await bus.publish(_finalized(space_id=None))
    assert cal.created == []


async def test_space_poll_creates_event_when_feature_enabled(env):
    bus, cal = env
    await bus.publish(_finalized())
    assert len(cal.created) == 1
    call = cal.created[0]
    assert call["space_id"] == "sp-A"
    assert call["summary"] == "Pizza night"
    assert call["start"].startswith("2026-05-01T18:00")
    assert call["end"].startswith("2026-05-01T20:00")
    assert call["created_by"] == "alice"
    assert call["all_day"] is False


@pytest.mark.parametrize("env", [False], indirect=True)
async def test_feature_disabled_no_event(env):
    bus, cal = env
    await bus.publish(_finalized())
    assert cal.created == []


async def test_all_day_slot_uses_day_window(env):
    bus, cal = env
    await bus.publish(_finalized(start_time=None, end_time=None))
    assert len(cal.created) == 1
    assert cal.created[0]["all_day"] is True
    assert cal.created[0]["start"].startswith("2026-05-01T00:00")


async def test_an_admin_only_calendar_skips_a_members_finalised_slot(caplog):
    """Finalising one's schedule poll is never gated, but the calendar entry
    it would add is a calendar CREATE: under ADMIN_ONLY a member's poll just
    adds no event — quietly, it is policy, not a failure."""
    import logging

    from socialhome.domain.space import AccessAdminOnlyError

    class _AdminOnlyCalendar:
        async def create_event(self, **kw):
            raise AccessAdminOnlyError("calendar")

    bus = EventBus()
    ScheduleCalendarBridge(
        bus=bus,
        space_calendar_service=_AdminOnlyCalendar(),  # type: ignore[arg-type]
        household_features=_FakeFeaturesService(enabled=True),  # type: ignore[arg-type]
    ).wire()
    with caplog.at_level(logging.DEBUG, logger="socialhome.services"):
        await bus.publish(_finalized())
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("admin" in r.getMessage() for r in caplog.records)
