"""Release-blocker protocol test: a paired household always sees the other
household's current members — new, changed and removed.

The bug: a paired household only ever got the roster once, at pair-confirm
(``USERS_SYNC``), and after that only on a member's own profile edit
(``USER_UPDATED``). A member added later — typically a child account a
parent creates, who never edits a profile — was never sent, so the other
household never saw them, however the per-pair "Who can … see?" list was
ticked; a removed member, the other way round, stayed listed there.

Two real households, each a full app over its own SQLite file. Only the
transport is stubbed: each envelope's payload is handed from A's
``send_event`` to B's registered handlers — or dropped while B is "away".
B coming back is B's startup ``INSTANCE_CAPABILITIES_UPDATED`` reaching A's
real handler.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from socialhome.app_keys import (
    child_protection_service_key,
    db_key,
    federation_service_key,
    user_service_key,
    users_sync_outbound_key,
)
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEvent,
    FederationEventType,
)
from socialhome.domain.federation_capabilities import OURS

from .test_users_sync_status_e2e import _add_member, houses  # noqa: F401

pytestmark = pytest.mark.security

FET = FederationEventType


class _Wire:
    """A's sends land on B's real handlers, unless B is away."""

    def __init__(self, monkeypatch, a, b, a_id: str, b_id: str) -> None:
        self.a, self.b, self.a_id, self.b_id = a, b, a_id, b_id
        self.online = True
        self.sent: list[FET] = []
        wire = self
        b_registry = b[federation_service_key]._event_registry

        async def _deliver(self, *, to_instance_id, event_type, payload, **_kw):
            wire.sent.append(event_type)
            if to_instance_id != b_id or not wire.online:
                return DeliveryResult(instance_id=to_instance_id, ok=False)
            for handler in b_registry.handlers_for(event_type):
                await handler(_event(event_type, a_id, b_id, payload))
            return DeliveryResult(instance_id=to_instance_id, ok=True)

        # Transport stub (the service is slotted, so patch the class).
        monkeypatch.setattr(type(a[federation_service_key]), "send_event", _deliver)

    async def b_comes_back(self) -> None:
        """B starts again: its capabilities reach A's real handler."""
        self.online = True
        a_registry = self.a[federation_service_key]._event_registry
        for handler in a_registry.handlers_for(FET.INSTANCE_CAPABILITIES_UPDATED):
            await handler(
                _event(
                    FET.INSTANCE_CAPABILITIES_UPDATED,
                    self.b_id,
                    self.a_id,
                    {"proto_version": OURS},
                )
            )
        # The snapshot answering a fresh connect goes out in the background.
        await self.a[users_sync_outbound_key].drain()

    def syncs(self) -> int:
        return self.sent.count(FET.USERS_SYNC)


def _event(event_type, from_id: str, to_id: str, payload: dict) -> FederationEvent:
    return FederationEvent(
        msg_id=uuid.uuid4().hex,
        event_type=event_type,
        from_instance=from_id,
        to_instance=to_id,
        timestamp=datetime.now(UTC).isoformat(),
        payload=payload,
    )


async def _on_b(b, user_id: str):
    return await b[db_key].fetchone(
        "SELECT display_name, deprovisioned_at FROM remote_users WHERE user_id=?",
        (user_id,),
    )


async def _admin(a, a_pk) -> str:
    admin = await _add_member(a, a_pk, "admin")
    await a[db_key].enqueue("UPDATE users SET is_admin=1 WHERE user_id=?", (admin,))
    return admin


async def test_a_child_created_after_pairing_reaches_the_paired_household(
    houses,  # noqa: F811
    monkeypatch,
):
    a, b, a_id, a_pk, b_id = houses
    admin = await _admin(a, a_pk)
    _Wire(monkeypatch, a, b, a_id, b_id)

    # The parent creates a child account after the pairing, then protects
    # it — the child never edits a profile of their own.
    kid = await a[user_service_key].provision(username="kid", display_name="Kid")
    await a[child_protection_service_key].enable_protection(
        minor_username="kid", declared_age=9, actor_user_id=admin
    )

    row = await _on_b(b, kid.user_id)
    assert row is not None, "the paired household never heard of the child"
    assert row["display_name"] == "Kid"
    assert row["deprovisioned_at"] is None


async def test_a_child_added_while_the_household_was_away_arrives_when_it_is_back(
    houses,  # noqa: F811
    monkeypatch,
):
    a, b, a_id, _a_pk, b_id = houses
    wire = _Wire(monkeypatch, a, b, a_id, b_id)
    wire.online = False
    kid = await a[user_service_key].provision(username="kid", display_name="Kid")
    assert await _on_b(b, kid.user_id) is None

    await wire.b_comes_back()

    assert (await _on_b(b, kid.user_id))["display_name"] == "Kid"


async def test_a_removed_member_disappears_at_the_paired_household(
    houses,  # noqa: F811
    monkeypatch,
):
    a, b, a_id, _a_pk, b_id = houses
    _Wire(monkeypatch, a, b, a_id, b_id)
    sam = await a[user_service_key].provision(username="sam", display_name="Sam")
    assert (await _on_b(b, sam.user_id))["deprovisioned_at"] is None

    await a[user_service_key].deprovision("sam")

    assert (await _on_b(b, sam.user_id))["deprovisioned_at"] is not None

    # Back in the household: back at the paired one too.
    await a[user_service_key].provision(username="sam", display_name="Sam")
    assert (await _on_b(b, sam.user_id))["deprovisioned_at"] is None


async def test_a_removal_missed_while_away_heals_when_the_household_is_back(
    houses,  # noqa: F811
    monkeypatch,
):
    a, b, a_id, _a_pk, b_id = houses
    wire = _Wire(monkeypatch, a, b, a_id, b_id)
    await a[user_service_key].provision(username="anna", display_name="Anna")
    sam = await a[user_service_key].provision(username="sam", display_name="Sam")
    wire.online = False
    await a[user_service_key].deprovision("sam")  # B misses the USER_REMOVED
    assert (await _on_b(b, sam.user_id))["deprovisioned_at"] is None

    await wire.b_comes_back()

    assert (await _on_b(b, sam.user_id))["deprovisioned_at"] is not None


async def test_a_member_hidden_from_the_household_never_reaches_it(
    houses,  # noqa: F811
    monkeypatch,
):
    a, b, a_id, a_pk, b_id = houses
    wire = _Wire(monkeypatch, a, b, a_id, b_id)
    await _add_member(a, a_pk, "anna")
    secret = await _add_member(a, a_pk, "sam")
    await a[db_key].enqueue(
        "INSERT INTO peer_user_visibility(instance_id, user_id, visible) VALUES(?,?,0)",
        (b_id, secret),
    )

    await wire.b_comes_back()

    assert wire.syncs() == 1
    assert await _on_b(b, secret) is None


async def test_a_flapping_household_gets_the_snapshot_once(
    houses,  # noqa: F811
    monkeypatch,
):
    a, b, a_id, a_pk, b_id = houses
    wire = _Wire(monkeypatch, a, b, a_id, b_id)
    await _add_member(a, a_pk, "anna")

    for _ in range(5):
        await wire.b_comes_back()
    assert wire.syncs() == 1

    # A real change goes out at once, and the next reconnect sends the
    # snapshot that now includes it — once.
    await a[user_service_key].provision(username="kid", display_name="Kid")
    for _ in range(3):
        await wire.b_comes_back()
    assert wire.syncs() == 2
