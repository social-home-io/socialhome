"""Tests for PeerDirectoryHandler (§D1a inbound)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.local_community import PeerLocalCommunity
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


async def test_a_local_community_round_trips_revalidated(repo):
    """v_59 — the radius is stored, re-validated; a verdict a sender still
    puts in the entry is ignored (there is no column for it); a malformed
    entry is kept as no community — never guessed."""
    handler = PeerDirectoryHandler(repo)
    await handler._on_snapshot(
        _event(
            "peerA",
            [
                {
                    "space_id": "in",
                    "name": "Quartier",
                    "local_community": True,
                    "radius_km": 2,
                },
                {
                    # An unreleased draft's verdict fields — dropped.
                    "space_id": "draft",
                    "name": "Rim",
                    "local_community": True,
                    "radius_km": 1.0,
                    "in_area": None,
                    "area_edge": True,
                },
                {
                    "space_id": "huge",
                    "name": "Region",
                    "local_community": True,
                    "radius_km": 500,
                },
                {"space_id": "plain", "name": "Chess"},
            ],
        )
    )
    rows = {r.space_id: r for r in await repo.list_for_instance("peerA")}
    assert rows["in"].local_community == PeerLocalCommunity(2.0)
    assert rows["draft"].local_community == PeerLocalCommunity(1.0)
    assert rows["huge"].local_community is None
    assert rows["plain"].local_community is None


async def test_a_centre_a_sender_puts_in_is_never_stored(repo):
    """A pre-rework (or hostile) host sending ``local_area: {lat, lon, …}``
    or bare coordinates: the entry is listed, nothing of the centre lands
    on disk — there is not even a column for it."""
    handler = PeerDirectoryHandler(repo)
    await handler._on_snapshot(
        _event(
            "peerA",
            [
                {
                    "space_id": "old",
                    "name": "Quartier",
                    "local_area": {"lat": 47.3769, "lon": 8.5417, "radius_km": 5},
                    "lat": 47.3769,
                    "lon": 8.5417,
                },
            ],
        )
    )
    (row,) = await repo.list_for_instance("peerA")
    assert row.local_community is None
    raw = await repo._db.fetchall("SELECT * FROM peer_space_directory")
    stored = [str(v) for r in raw for v in tuple(r)]
    assert not any(v.startswith(("47.3769", "8.5417")) for v in stored)


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
