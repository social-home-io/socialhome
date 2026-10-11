"""Shared fixtures for the household-dashboard tests (in-memory, no disk)."""

from __future__ import annotations

import asyncio
import os

import pytest

from socialhome.infrastructure.key_manager import KeyManager
from socialhome.services.dashboard_settings_service import DashboardSettingsService

MAIN_PORT = 8080
PASSWORD = "correct horse"


class FakeConfigRepo:
    """In-memory :class:`AbstractInstanceConfigRepo`."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        # Yield like a real DB read, so request interleavings are exercised.
        await asyncio.sleep(0)
        return self.rows.get(key)

    async def get_many(self, prefix: str) -> dict[str, str]:
        return {k: v for k, v in self.rows.items() if k.startswith(prefix)}

    async def set(self, key: str, value: str) -> None:
        self.rows[key] = value

    async def delete(self, key: str) -> None:
        self.rows.pop(key, None)

    async def set_many(self, items) -> None:
        self.rows.update(items)


class FakeClock:
    """A settable ``time.time`` stand-in."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def settings_service() -> DashboardSettingsService:
    svc = DashboardSettingsService(
        FakeConfigRepo(), fixed_port=None, main_port=MAIN_PORT
    )
    svc.attach_key_manager(KeyManager(os.urandom(32)))
    return svc


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
