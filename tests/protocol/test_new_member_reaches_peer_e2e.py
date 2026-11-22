"""Release-blocker protocol test: a household member created after pairing
reaches the paired household — and an existing pair heals on its own.

The bug: a paired household only ever got the roster once, at pair-confirm
(``USERS_SYNC``), and after that only on a member's own profile edit
(``USER_UPDATED``). A member added later — typically a child account a
parent creates, who never edits a profile — was never sent, so the other
household never saw them, however the per-pair "Who can … see?" list was
ticked.

Two real households, each a full app over its own SQLite file. Only the
transport is stubbed: each envelope's payload is handed from A's
``send_event`` to B's registered handlers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from socialhome.app_keys import (
    child_protection_service_key,
    db_key,
    federation_service_key,
    instance_config_repo_key,
    user_service_key,
    users_sync_outbound_key,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.services.users_sync_outbound import ROSTER_RESEND_KEY

from .test_users_sync_status_e2e import _add_member, houses  # noqa: F401

pytestmark = pytest.mark.security

FET = FederationEventType


def _wire(monkeypatch, a, b, a_id: str, b_id: str) -> list[tuple[str, FET]]:
    """A's sends land on B's real handlers; returns the send log."""
    sent: list[tuple[str, FET]] = []
    b_registry = b[federation_service_key]._event_registry

    async def _deliver(self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type))
        if to_instance_id != b_id:
            return
        for handler in b_registry.handlers_for(event_type):
            await handler(
                FederationEvent(
                    msg_id=uuid.uuid4().hex,
                    event_type=event_type,
                    from_instance=a_id,
                    to_instance=b_id,
                    timestamp=datetime.now(UTC).isoformat(),
                    payload=payload,
                )
            )

    # Transport stub (the service is slotted, so patch the class).
    monkeypatch.setattr(type(a[federation_service_key]), "send_event", _deliver)
    return sent


async def _on_b(b, user_id: str):
    return await b[db_key].fetchone(
        "SELECT display_name, deprovisioned_at FROM remote_users WHERE user_id=?",
        (user_id,),
    )


async def test_a_child_created_after_pairing_reaches_the_paired_household(
    houses,  # noqa: F811
    monkeypatch,
):
    a, b, a_id, a_pk, b_id = houses
    admin = await _add_member(a, a_pk, "admin")
    await a[db_key].enqueue("UPDATE users SET is_admin=1 WHERE user_id=?", (admin,))
    _wire(monkeypatch, a, b, a_id, b_id)

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


async def test_an_existing_pair_gets_the_members_it_missed_once(
    houses,  # noqa: F811
    monkeypatch,
):
    a, b, a_id, a_pk, b_id = houses
    # Startup ran with no peer pinned yet (the fixture waits for it); pretend
    # this household was paired under a build without the fix.
    await a[instance_config_repo_key].delete(ROSTER_RESEND_KEY)
    # A child B never got (created before the fix, after the pairing) and a
    # member hidden from B.
    kid = await _add_member(a, a_pk, "kid")
    secret = await _add_member(a, a_pk, "sam")
    await a[db_key].enqueue(
        "INSERT INTO peer_user_visibility(instance_id, user_id, visible) VALUES(?,?,0)",
        (b_id, secret),
    )
    sent = _wire(monkeypatch, a, b, a_id, b_id)

    outbound = a[users_sync_outbound_key]
    assert await outbound.resend_roster_if_due() == 1

    assert (await _on_b(b, kid))["display_name"] == "Kid"
    assert await _on_b(b, secret) is None, "a hidden member must never be sent"

    # Once: the next start sends nothing again.
    sent.clear()
    assert await outbound.resend_roster_if_due() == 0
    assert sent == []
