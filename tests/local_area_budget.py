"""An in-memory ``AbstractLocalAreaAttemptRepo`` for unit tests, and a
``LocalAreaBudget`` over it (the SQLite one is tested in
``tests/repositories/test_local_area_attempt_repo.py``)."""

from __future__ import annotations

from collections.abc import Sequence

from socialhome.repositories.local_area_attempt_repo import BudgetLine
from socialhome.services.local_community import LocalAreaBudget


class InMemoryLocalAreaAttemptRepo:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str, str], int] = {}

    async def try_spend(
        self,
        space_id: str,
        day: str,
        lines: Sequence[BudgetLine],
        *,
        prune_before: str,
    ) -> bool:
        self.rows = {k: v for k, v in self.rows.items() if k[3] >= prune_before}
        keys = [(space_id, kind, subject, day) for kind, subject, _ in lines]
        if any(self.rows.get(k, 0) >= limit for k, (_, _, limit) in zip(keys, lines)):
            return False
        for k in keys:
            self.rows[k] = self.rows.get(k, 0) + 1
        return True


def memory_budget(**kw) -> LocalAreaBudget:
    return LocalAreaBudget(InMemoryLocalAreaAttemptRepo(), **kw)
