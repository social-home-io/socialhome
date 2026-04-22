"""Tests for PeerDirectoryHandler (§D1a inbound)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation.peer_directory_handler import PeerDirectoryHandler
from socialhome.repositories.peer_space_directory_repo import (
    SqlitePeerSpaceDirectoryRepo,
)


def _event(from_instance: str, spaces: list[dict]) -> FederationEvent:
    return FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_DIRECTORY_SYNC,
        from_instance=from_instance,
        to_instance="local",
        timestamp="2026-01-01T00:00:00Z",
        payload={"spaces": spaces},
    )


@pytest.fixture
async def repo(tmp_dir):
    db = AsyncDatabase(tmp_dir / "d1a.db", batch_timeout_ms=10)
    await db.startup()
    try:
        yield SqlitePeerSpaceDirectoryRepo(db)
    finally:
        await db.shutdown()


async def test_snapshot_roundtrip(repo):
    handler = PeerDirectoryHandler(repo)
    await handler._on_snapshot(
        _event(
            "peerA",
            [
                {"space_id": "s1", "name": "One", "member_count": 3},
                {
                    "space_id": "s2",
                    "name": "Two",
                    "member_count": 7,
                    "join_mode": "open",
                },
                {"space_id": "s3", "name": "Tri", "member_count": 1},
            ],
        )
    )
    rows = await repo.list_for_instance("peerA")
    assert len(rows) == 3
    names = sorted(r.name for r in rows)
    assert names == ["One", "Tri", "Two"]


async def test_snapshot_replaces_previous_entries(repo):
    handler = PeerDirectoryHandler(repo)
    await handler._on_snapshot(
        _event(
            "peerA",
            [
                {"space_id": "s1", "name": "One"},
                {"space_id": "s2", "name": "Two"},
            ],
        )
    )
    assert len(await repo.list_for_instance("peerA")) == 2
    # Second snapshot drops one space.
    await handler._on_snapshot(
        _event(
            "peerA",
            [
                {"space_id": "s1", "name": "One"},
            ],
        )
    )
    rows = await repo.list_for_instance("peerA")
    assert len(rows) == 1
    assert rows[0].space_id == "s1"


async def test_snapshot_scopes_per_instance(repo):
    handler = PeerDirectoryHandler(repo)
    await handler._on_snapshot(
        _event(
            "peerA",
            [
                {"space_id": "sa", "name": "A"},
            ],
        )
    )
    await handler._on_snapshot(
        _event(
            "peerB",
            [
                {"space_id": "sb", "name": "B"},
            ],
        )
    )
    # Clearing A doesn't touch B.
    await repo.clear_instance("peerA")
    assert await repo.list_for_instance("peerA") == []
    assert len(await repo.list_for_instance("peerB")) == 1


async def test_malformed_payload_ignored(repo):
    handler = PeerDirectoryHandler(repo)
    bad = FederationEvent(
        msg_id="m",
        event_type=FederationEventType.SPACE_DIRECTORY_SYNC,
        from_instance="peerA",
        to_instance="local",
        timestamp="2026-01-01T00:00:00Z",
        payload={},  # no "spaces" key
    )
    await handler._on_snapshot(bad)
    assert await repo.list_for_instance("peerA") == []


async def test_attach_registers_event_type():
    handler = PeerDirectoryHandler(MagicMock())
    federation_service = MagicMock()
    registry = MagicMock()
    federation_service._event_registry = registry
    handler.attach_to(federation_service)
    registry.register.assert_called_once()
    (event_type, fn), _kwargs = registry.register.call_args
    assert event_type == FederationEventType.SPACE_DIRECTORY_SYNC
    assert fn == handler._on_snapshot
