"""Sender budgets for a GFS space's channel items (v_97): per person, sized
for an active 10 000-seat space; the item waits in the durable queue."""

from __future__ import annotations

from socialhome.domain.space_item import BUDGET_CHAT, BUDGET_CONTENT, BUDGET_LOCATION
from socialhome.services import space_item_budget as mod
from socialhome.services.space_item_budget import SenderBudget


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_content_never_waits():
    budget = SenderBudget(clock=_Clock())
    assert [budget.delay("sp", BUDGET_CONTENT, "u") for _ in range(50)] == [0.0] * 50


def test_chat_bursts_ten_then_one_a_second_per_person():
    clock = _Clock()
    budget = SenderBudget(clock=clock)
    delays = [budget.delay("sp", BUDGET_CHAT, "anna") for _ in range(12)]
    assert delays[:10] == [0.0] * 10
    assert delays[10:] == [1.0, 2.0]
    # Another person — and the same person in another space — is never
    # held behind her.
    assert budget.delay("sp", BUDGET_CHAT, "ben") == 0.0
    assert budget.delay("other", BUDGET_CHAT, "anna") == 0.0
    # The bucket refills at one a second.
    clock.t += 13
    assert budget.delay("sp", BUDGET_CHAT, "anna") == 0.0


def test_one_pin_per_ten_seconds_a_newer_one_takes_the_waiting_slot():
    clock = _Clock()
    budget = SenderBudget(clock=clock)
    assert budget.delay("sp", BUDGET_LOCATION, "ann") == 0.0
    clock.t += 2
    assert budget.delay("sp", BUDGET_LOCATION, "ann") == 8.0
    clock.t += 2
    # Replaces the waiting pin in its slot — never pushed further out.
    assert budget.delay("sp", BUDGET_LOCATION, "ann") == 6.0
    assert budget.delay("sp", BUDGET_LOCATION, "bob") == 0.0
    clock.t += 20
    assert budget.delay("sp", BUDGET_LOCATION, "ann") == 0.0


def test_idle_people_are_forgotten_past_the_cap(monkeypatch):
    monkeypatch.setattr(mod, "MAX_TRACKED", 3)
    clock = _Clock()
    budget = SenderBudget(clock=clock)
    for n in range(3):
        budget.delay("sp", BUDGET_CHAT, f"p{n}")
        budget.delay("sp", BUDGET_LOCATION, f"p{n}")
    clock.t += 3600
    budget.delay("sp", BUDGET_CHAT, "new")
    budget.delay("sp", BUDGET_LOCATION, "new")
    assert len(budget._tokens) == 1 and len(budget._pins) == 1
