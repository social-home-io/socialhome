"""Tests for the GFS periodic maintenance scheduler."""

from __future__ import annotations

import asyncio

from socialhome.global_server.maintenance import GfsMaintenanceScheduler


class _StubAdminRepo:
    def __init__(self) -> None:
        self.purge_calls: list[int] = []
        self.prune_token_calls: list[int] = []

    async def purge_expired_sessions(self, now: int) -> None:
        self.purge_calls.append(now)

    async def prune_old_pair_tokens(self, cutoff: int) -> int:
        self.prune_token_calls.append(cutoff)
        return 3


class _StubHighlightRepo:
    def __init__(self) -> None:
        self.prune_calls: list[int] = []

    async def prune_expired(self, now: int) -> int:
        self.prune_calls.append(now)
        return 2


async def test_maintain_once_runs_all_three_prunes():
    admin = _StubAdminRepo()
    highlight = _StubHighlightRepo()
    sched = GfsMaintenanceScheduler(admin_repo=admin, highlight_repo=highlight)
    await sched._maintain_once()
    assert len(admin.purge_calls) == 1
    assert len(highlight.prune_calls) == 1
    assert len(admin.prune_token_calls) == 1


async def test_one_prune_failure_does_not_skip_the_others():
    class _Boom(_StubHighlightRepo):
        async def prune_expired(self, now: int) -> int:
            raise RuntimeError("boom")

    admin = _StubAdminRepo()
    highlight = _Boom()
    sched = GfsMaintenanceScheduler(admin_repo=admin, highlight_repo=highlight)
    # Should not raise; admin prunes still run.
    await sched._maintain_once()
    assert len(admin.purge_calls) == 1
    assert len(admin.prune_token_calls) == 1


async def test_start_runs_a_tick_then_stops_clean():
    admin = _StubAdminRepo()
    highlight = _StubHighlightRepo()
    sched = GfsMaintenanceScheduler(
        admin_repo=admin,
        highlight_repo=highlight,
        interval_seconds=0.05,
    )
    await sched.start()
    await sched.start()  # idempotent
    await asyncio.sleep(0.1)
    await sched.stop()
    await sched.stop()  # re-stop is fine
    assert admin.purge_calls  # the loop ran at least once


class _StubEnvelopeQueueRepo:
    def __init__(self, *, boom: bool = False) -> None:
        self.prune_calls: list[int] = []
        self._boom = boom

    async def prune_expired(self, now: int) -> int:
        if self._boom:
            raise RuntimeError("boom")
        self.prune_calls.append(now)
        return 4


async def test_maintain_once_sweeps_expired_relay_envelopes():
    admin = _StubAdminRepo()
    highlight = _StubHighlightRepo()
    envelopes = _StubEnvelopeQueueRepo()
    sched = GfsMaintenanceScheduler(
        admin_repo=admin,
        highlight_repo=highlight,
        envelope_queue_repo=envelopes,
    )
    await sched._maintain_once()
    assert len(envelopes.prune_calls) == 1


async def test_envelope_sweep_failure_does_not_skip_the_others():
    admin = _StubAdminRepo()
    highlight = _StubHighlightRepo()
    sched = GfsMaintenanceScheduler(
        admin_repo=admin,
        highlight_repo=highlight,
        envelope_queue_repo=_StubEnvelopeQueueRepo(boom=True),
    )
    await sched._maintain_once()
    assert len(admin.purge_calls) == 1
    assert len(highlight.prune_calls) == 1
    assert len(admin.prune_token_calls) == 1


class _StubInviteRepo:
    def __init__(self, *, boom: bool = False) -> None:
        self.boom = boom
        self.prune_calls: list[int] = []

    async def prune_expired(self, now: int) -> int:
        if self.boom:
            raise RuntimeError("boom")
        self.prune_calls.append(now)
        return 5


async def test_maintain_once_sweeps_expired_invites():
    invites = _StubInviteRepo()
    sched = GfsMaintenanceScheduler(
        admin_repo=_StubAdminRepo(),
        highlight_repo=_StubHighlightRepo(),
        invite_repo=invites,
    )
    await sched._maintain_once()
    assert len(invites.prune_calls) == 1


async def test_invite_sweep_failure_does_not_skip_the_others():
    admin = _StubAdminRepo()
    highlight = _StubHighlightRepo()
    sched = GfsMaintenanceScheduler(
        admin_repo=admin,
        highlight_repo=highlight,
        invite_repo=_StubInviteRepo(boom=True),
    )
    await sched._maintain_once()
    assert len(admin.purge_calls) == 1
    assert len(highlight.prune_calls) == 1
    assert len(admin.prune_token_calls) == 1
