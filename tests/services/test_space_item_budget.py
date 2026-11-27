"""Sender budgets for a GFS space's channel items (v_97)."""

from __future__ import annotations

import asyncio

from socialhome.domain.space_item import (
    BUDGET_AUTHORITY,
    BUDGET_CHAT,
    BUDGET_CONTENT,
    BUDGET_LOCATION,
)
from socialhome.services import space_item_budget as mod
from socialhome.services.space_item_budget import SenderBudget


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _publisher(log: list, name: str):
    async def publish() -> None:
        log.append(name)

    return publish


async def test_content_and_authority_go_at_once():
    budget, sent = SenderBudget(), []
    for kind in (BUDGET_CONTENT, BUDGET_AUTHORITY):
        assert await budget.submit("sp", kind, "u", _publisher(sent, kind))
    assert sent == [BUDGET_CONTENT, BUDGET_AUTHORITY]


async def test_chat_waits_past_twenty_a_minute_and_keeps_its_order(monkeypatch):
    monkeypatch.setattr(mod, "CHAT_PER_MINUTE", 2)
    clock = _Clock()
    budget, sent = SenderBudget(clock=clock), []
    for n in range(4):
        await budget.submit("sp", BUDGET_CHAT, "u", _publisher(sent, f"m{n}"))
    assert sent == ["m0", "m1"]
    clock.t += 61
    await budget.wait_idle()
    assert sent == ["m0", "m1", "m2", "m3"]


async def test_another_space_has_its_own_chat_budget(monkeypatch):
    monkeypatch.setattr(mod, "CHAT_PER_MINUTE", 1)
    budget, sent = SenderBudget(), []
    assert await budget.submit("a", BUDGET_CHAT, "u", _publisher(sent, "a"))
    assert await budget.submit("b", BUDGET_CHAT, "u", _publisher(sent, "b"))


async def test_one_pin_a_minute_per_person_latest_only(monkeypatch):
    monkeypatch.setattr(mod, "LOCATION_INTERVAL_S", 0.05)
    budget, sent = SenderBudget(), []
    assert await budget.submit("sp", BUDGET_LOCATION, "ann", _publisher(sent, "p1"))
    assert not await budget.submit("sp", BUDGET_LOCATION, "ann", _publisher(sent, "p2"))
    assert not await budget.submit("sp", BUDGET_LOCATION, "ann", _publisher(sent, "p3"))
    # Another person is not held behind her.
    assert await budget.submit("sp", BUDGET_LOCATION, "bob", _publisher(sent, "b1"))
    await asyncio.sleep(0.1)
    await budget.wait_idle()
    assert sent == ["p1", "b1", "p3"]


async def test_a_full_queue_never_drops_a_message(monkeypatch):
    monkeypatch.setattr(mod, "CHAT_PER_MINUTE", 1)
    monkeypatch.setattr(mod, "MAX_WAITING", 1)
    clock = _Clock()
    budget, sent = SenderBudget(clock=clock), []
    await budget.submit("sp", BUDGET_CHAT, "u", _publisher(sent, "m0"))
    await budget.submit("sp", BUDGET_CHAT, "u", _publisher(sent, "m1"))
    assert await budget.submit("sp", BUDGET_CHAT, "u", _publisher(sent, "m2"))
    assert "m2" in sent
    clock.t += 61
    await budget.wait_idle()
    assert sorted(sent) == ["m0", "m1", "m2"]


async def test_close_sends_every_held_item_at_once(monkeypatch):
    """Shutting down never waits out a pacing window: what is held goes."""
    monkeypatch.setattr(mod, "CHAT_PER_MINUTE", 1)
    monkeypatch.setattr(mod, "LOCATION_INTERVAL_S", 3600.0)
    budget, sent = SenderBudget(clock=_Clock()), []
    await budget.submit("sp", BUDGET_CHAT, "u", _publisher(sent, "m0"))
    await budget.submit("sp", BUDGET_CHAT, "u", _publisher(sent, "m1"))
    await budget.submit("sp", BUDGET_LOCATION, "ann", _publisher(sent, "p1"))
    await budget.submit("sp", BUDGET_LOCATION, "ann", _publisher(sent, "p2"))
    await asyncio.wait_for(budget.close(), timeout=2)
    assert sorted(sent) == ["m0", "m1", "p1", "p2"]
