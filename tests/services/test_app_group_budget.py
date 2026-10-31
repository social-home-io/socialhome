"""The per-table send budget (design 2.12)."""

from __future__ import annotations

from socialhome.services.app_group_budget import TableSendBudget


def test_default_budget_is_600_a_minute():
    b = TableSendBudget()
    assert b.take(600, 0.0) is None
    assert b.take(1, 1.0) == 59


def test_a_30_household_table_sends_20_messages_a_minute():
    b = TableSendBudget()
    sent = sum(1 for i in range(25) if b.take(29, float(i)) is None)
    assert sent == 20


def test_the_window_slides():
    b = TableSendBudget(limit=10, window=60.0)
    assert b.take(6, 0.0) is None
    assert b.take(4, 30.0) is None
    assert b.take(5, 40.0) == 20  # the first 6 leave at 60 s
    assert b.take(5, 60.0) is None  # they left; nothing was spent on the refusal


def test_free_messages_and_too_big_ones():
    b = TableSendBudget(limit=10, window=60.0)
    assert b.take(0, 0.0) is None
    assert b.take(11, 0.0) == 60
    assert b.idle(0.0)
    b.take(3, 0.0)
    assert not b.idle(1.0)
    assert b.idle(60.0)
