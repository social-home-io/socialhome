"""Tests for ``socialhome.services.timetable_federation_outbound``."""

from __future__ import annotations

import os
from datetime import datetime, time, timezone
from typing import Any

import orjson
import pytest

from socialhome.domain.events import TimetableDeleted, TimetableSaved
from socialhome.domain.federation import FederationEventType
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.timetable import (
    MAX_WIRE_BYTES,
    Timetable,
    TimetableEntry,
    TimetableValidationError,
    from_wire_dict,
    to_wire_dict,
    validate,
)
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.gfs_relay_transport import RELAY_MAX_ENVELOPE_BYTES
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.timetable_federation_outbound import (
    TimetableFederationOutbound,
    upsert_payload,
)

FET = FederationEventType
_NOW = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


class _FakeFederation:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail = fail

    async def broadcast_to_space_members(self, space_id, event_type, payload, **kw):
        if self._fail:
            raise RuntimeError("transport down")
        self.calls.append(
            {"space_id": space_id, "event_type": event_type, "payload": payload, **kw}
        )


def _tt(**kw) -> Timetable:
    kw.setdefault("id", "tt-1")
    return Timetable(
        name="5b",
        created_by="u-admin",
        created_at=_NOW,
        updated_at=_NOW,
        updated_by="u-admin",
        **kw,
    )


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFederation()
    TimetableFederationOutbound(bus=bus, federation_service=fed).wire()
    return bus, fed


async def test_space_upsert_ships_the_whole_timetable_to_members_only(env):
    bus, fed = env
    tt = _tt()
    await bus.publish(TimetableSaved(timetable=tt, space_id="sp"))
    [call] = fed.calls
    assert call["space_id"] == "sp"
    assert call["event_type"] is FET.SPACE_TIMETABLE_UPSERTED
    assert call["min_proto_version"] == FederationCapability.MIN_FOR_SPACE_TIMETABLE
    assert call["payload"] == {"space_id": "sp", "timetable": to_wire_dict(tt)}
    assert call["payload"]["timetable"]["updated_by"] == "u-admin"
    assert from_wire_dict(call["payload"]["timetable"]) == tt


async def test_space_delete_carries_id_time_and_admin(env):
    bus, fed = env
    ev = TimetableDeleted(
        timetable_id="tt-1", space_id="sp", deleted_by="u-admin", created_by="u-c"
    )
    await bus.publish(ev)
    [call] = fed.calls
    assert call["event_type"] is FET.SPACE_TIMETABLE_DELETED
    assert call["min_proto_version"] == FederationCapability.MIN_FOR_SPACE_TIMETABLE
    assert call["payload"] == {
        "space_id": "sp",
        "timetable_id": "tt-1",
        "deleted_at": ev.occurred_at.isoformat(),
        "deleted_by": "u-admin",
        "created_by": "u-c",
    }


async def test_household_timetables_and_inbound_echoes_stay_put(env):
    bus, fed = env
    await bus.publish(TimetableSaved(timetable=_tt()))
    await bus.publish(TimetableDeleted(timetable_id="tt-1"))
    await bus.publish(
        TimetableSaved(timetable=_tt(), space_id="sp", origin_instance_id="peer")
    )
    await bus.publish(
        TimetableDeleted(timetable_id="tt-1", space_id="sp", origin_instance_id="peer")
    )
    assert fed.calls == []


async def test_a_failed_fan_out_is_logged_not_raised(caplog):
    bus = EventBus()
    TimetableFederationOutbound(
        bus=bus, federation_service=_FakeFederation(fail=True)
    ).wire()
    with caplog.at_level("WARNING"):
        await bus.publish(TimetableSaved(timetable=_tt(), space_id="sp"))
    assert "broadcast failed" in caplog.text


def _max_size_timetable() -> Timetable:
    """The largest timetable ``validate`` still admits (just under 128 KiB)."""
    days = tuple(range(7))
    entries = [
        TimetableEntry(
            id=f"e{wd}-{i}",
            weekday=wd,
            start=time(i // 2, (i % 2) * 30),
            end=time(i // 2, (i % 2) * 30 + 29),
            title="ü" * 60,
            room="ü" * 30,
            teacher="ü" * 60,
            note="ü" * 200,
            label="ü" * 8,
        )
        for wd in days
        for i in range(24)
    ][:200]
    while True:
        tt = _tt(days=days, entries=tuple(entries))
        try:
            validate(tt)
            return tt
        except TimetableValidationError:
            entries.pop()


def test_a_max_size_timetable_fits_the_relay_envelope_cap():
    """Link-joined members are reached through the connection-server relay,
    whose envelope cap is far below a paired peer's. The domain's 128 KiB
    wire cap must leave a sealed upsert inside it."""
    tt = _max_size_timetable()
    wire = orjson.dumps(to_wire_dict(tt))
    assert MAX_WIRE_BYTES - 2048 < len(wire) <= MAX_WIRE_BYTES
    payload = orjson.dumps(upsert_payload(tt, "s" * 32)).decode()
    sealed = FederationEncoder(os.urandom(32)).encrypt_payload(payload, os.urandom(32))
    envelope = {
        "msg_id": "0" * 36,
        "event_type": FET.SPACE_TIMETABLE_UPSERTED.value,
        "from_instance": "i" * 64,
        "to_instance": "j" * 64,
        "timestamp": _NOW.isoformat(),
        "encrypted_payload": sealed,
        "space_id": "s" * 32,
        "proto_version": 1,
        "sig_suite": "ed25519+mldsa65",
        # An Ed25519 + ML-DSA-65 pair, base64 — the largest signature set.
        "signatures": {"ed25519": "x" * 88, "mldsa65": "y" * 4412},
    }
    assert len(orjson.dumps(envelope)) < RELAY_MAX_ENVELOPE_BYTES
