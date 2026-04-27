"""Tests for :class:`PairingRelayQueue` (§11.9)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace


from socialhome.domain.events import PairingIntroRelayReceived
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.pairing_relay_queue import (
    PairingRelayQueue,
    _MAX_PENDING_ROWS,
)


class _FakeFederation:
    """Captures send_event calls for assertion in approve() tests."""

    def __init__(self, *, ok: bool = True) -> None:
        self.calls: list[dict] = []
        self._ok = ok

    async def send_event(self, *, to_instance_id, event_type, payload):
        self.calls.append(
            {
                "to": to_instance_id,
                "type": event_type,
                "payload": payload,
            }
        )
        return SimpleNamespace(ok=self._ok)


class _MemRelayRepo:
    """In-memory :class:`AbstractPairingRelayRepo` used by these tests."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    async def save(
        self,
        *,
        request_id,
        from_instance,
        target_instance_id,
        message,
        received_at,
    ):
        if request_id in self._rows:
            return
        self._rows[request_id] = {
            "id": request_id,
            "from_instance": from_instance,
            "target_instance_id": target_instance_id,
            "message": message,
            "received_at": received_at.isoformat(),
            "status": "pending",
        }

    async def get(self, request_id):
        row = self._rows.get(request_id)
        if row is None or row["status"] != "pending":
            return None
        return dict(row)

    async def list_pending(self):
        return sorted(
            (dict(r) for r in self._rows.values() if r["status"] == "pending"),
            key=lambda r: r["received_at"],
        )

    async def set_status(self, request_id, status):
        if request_id in self._rows:
            self._rows[request_id]["status"] = status

    async def count_pending(self):
        return sum(1 for r in self._rows.values() if r["status"] == "pending")

    async def delete_oldest_pending(self, keep):
        pending = sorted(
            (r for r in self._rows.values() if r["status"] == "pending"),
            key=lambda r: r["received_at"],
        )
        excess = pending[: max(0, len(pending) - int(keep))]
        for r in excess:
            self._rows.pop(r["id"], None)
        return len(excess)

    async def delete_older_than(self, *, status, cutoff_iso):
        targets = [
            rid
            for rid, r in self._rows.items()
            if r["status"] == status and r["received_at"] < cutoff_iso
        ]
        for rid in targets:
            self._rows.pop(rid, None)
        return len(targets)


def _make_queue(*, fed=None, repo=None) -> tuple[PairingRelayQueue, EventBus]:
    bus = EventBus()
    q = PairingRelayQueue(
        bus=bus,
        federation=fed or _FakeFederation(),
        repo=repo or _MemRelayRepo(),
        own_instance_id="self",
    )
    q.wire()
    return q, bus


async def test_queue_collects_relay_events_from_bus():
    q, bus = _make_queue()

    await bus.publish(
        PairingIntroRelayReceived(
            from_instance="peer-a",
            target_instance_id="peer-b",
            message="please intro",
        )
    )
    pending = await q.list_pending()
    assert len(pending) == 1
    assert pending[0].from_instance == "peer-a"
    assert pending[0].target_instance_id == "peer-b"
    assert pending[0].message == "please intro"
    assert isinstance(pending[0].received_at, datetime)


async def test_approve_forwards_pairing_intro_to_target():
    fed = _FakeFederation()
    q, bus = _make_queue(fed=fed)

    await bus.publish(
        PairingIntroRelayReceived(
            from_instance="peer-a",
            target_instance_id="peer-b",
            message="hi",
        )
    )
    [req] = await q.list_pending()

    approved = await q.approve(req.id)
    assert approved is not None
    assert await q.get(req.id) is None  # no longer pending
    assert len(fed.calls) == 1
    call = fed.calls[0]
    assert call["to"] == "peer-b"
    assert call["payload"]["via_instance_id"] == "peer-a"


async def test_decline_marks_declined_without_sending():
    fed = _FakeFederation()
    q, bus = _make_queue(fed=fed)

    await bus.publish(
        PairingIntroRelayReceived(
            from_instance="peer-a",
            target_instance_id="peer-b",
        )
    )
    [req] = await q.list_pending()

    dropped = await q.decline(req.id)
    assert dropped is not None
    assert await q.list_pending() == []
    assert fed.calls == []


async def test_unknown_id_returns_none():
    q, _bus = _make_queue()

    assert await q.approve("no-such-id") is None
    assert await q.decline("no-such-id") is None


async def test_queue_caps_at_max_pending_rows():
    repo = _MemRelayRepo()
    q, bus = _make_queue(repo=repo)

    for i in range(_MAX_PENDING_ROWS + 5):
        await bus.publish(
            PairingIntroRelayReceived(
                from_instance=f"peer-{i}",
                target_instance_id="peer-t",
            )
        )
    assert len(await q.list_pending()) == _MAX_PENDING_ROWS


async def test_queue_survives_restart_via_repo():
    """The repo holds state across PairingRelayQueue instances —
    a "restart" reusing the same repo can still serve list_pending."""
    repo = _MemRelayRepo()
    q1, bus1 = _make_queue(repo=repo)

    await bus1.publish(
        PairingIntroRelayReceived(
            from_instance="peer-a",
            target_instance_id="peer-b",
            message="persisted",
        )
    )
    assert len(await q1.list_pending()) == 1

    q2, _bus2 = _make_queue(repo=repo)
    pending = await q2.list_pending()
    assert len(pending) == 1
    assert pending[0].message == "persisted"
