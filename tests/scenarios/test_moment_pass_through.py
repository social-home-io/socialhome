"""§Momentum-relay-policy pass-through scenario.

When no local user can see an inbound moment (under their
``moments.max_hops`` preference + per-viewer block list), the
recipient instance does NOT save the row, but it still relays
onward to its paired peers. Pure pass-through behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.domain.federation import FederationEventType, RemoteInstance
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.instance_ban_repo import (
    SqliteHouseholdInstanceBanRepo,
)
from socialhome.repositories.moment_repo import SqliteMomentRepo
from socialhome.repositories.report_repo import SqliteReportRepo
from socialhome.services.federation_inbound_service import (
    FederationInboundService,
)
from socialhome.services.moment_federation_outbound import (
    MomentFederationOutbound,
)
from socialhome.services.relay_policy import RelayPolicy


def _peer(iid: str) -> RemoteInstance:
    inst = MagicMock(spec=RemoteInstance)
    inst.id = iid
    return inst


@pytest.fixture
async def env(db):
    """Wire the recipient stack with a single local user whose
    ``max_hops=1``, an inbound author at hop=2, and a real
    :class:`MomentFederationOutbound` so we can assert relay fires."""
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state, preferences_json) "
        "VALUES('u-self','alice','Alice','active','{\"moments\":{\"max_hops\":1}}')"
    )
    moment_repo = SqliteMomentRepo(db)
    bans = SqliteHouseholdInstanceBanRepo(db)
    reports = SqliteReportRepo(db)
    policy = RelayPolicy(ban_repo=bans, report_repo=reports)

    bus = EventBus()
    federation = MagicMock()
    federation.own_instance_id = "self"
    federation.send_event = AsyncMock()
    federation_repo = MagicMock()
    federation_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-onward")]
    )
    user_repo = MagicMock()
    user_repo.get_instance_for_user = AsyncMock(return_value="inst-remote")
    user_repo.get_by_user_id = AsyncMock(return_value=None)  # author is remote

    out = MomentFederationOutbound(
        bus=bus,
        federation_service=federation,
        federation_repo=federation_repo,
        user_repo=user_repo,
        relay_policy=policy,
    )
    inbound = FederationInboundService(
        bus=bus,
        conversation_repo=MagicMock(),
        space_post_repo=MagicMock(),
        space_repo=MagicMock(),
        user_repo=user_repo,
        moment_repo=moment_repo,
        moment_outbound=out,
        relay_policy=policy,
    )
    return {
        "bus": bus,
        "moments": moment_repo,
        "federation": federation,
        "inbound": inbound,
        "outbound": out,
        "bans": bans,
    }


def _moment_event(*, hop_count: int = 2):
    """Mocked :class:`FederationEvent` carrying the moment payload."""
    ev = MagicMock()
    ev.from_instance = "inst-remote"
    ev.event_type = FederationEventType.MOMENT_CREATED
    ev.payload = {
        "moment_id": "m-pass",
        "author_user_id": "u-author-remote",
        "content": "passing through",
        "origin_instance_id": "inst-remote",
        "occurred_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
        "hop_count": hop_count,
    }
    return ev


async def test_no_local_visibility_means_skip_persist_but_relay(env):
    """Local user has ``max_hops=1``; inbound is at hop=2. The
    recipient does NOT save the row, but DOES relay onward."""
    # Authority check passes — the fixture sets ``from_instance`` ==
    # ``origin_instance_id`` so :meth:`_moment_authority_matches`
    # short-circuits to True without consulting user_repo.
    await env["inbound"]._on_moment_created(_moment_event(hop_count=2))
    # No local row — the row's not visible to anyone, so we didn't
    # bother persisting it.
    assert await env["moments"].get("m-pass") is None
    # Relay still fired — federation.send_event called for the onward
    # peer with hop_count bumped to 3.
    assert env["federation"].send_event.called
    sent = [c.kwargs for c in env["federation"].send_event.call_args_list]
    assert all(c["payload"]["hop_count"] == 3 for c in sent)


async def test_within_visibility_persists_and_relays(env):
    """Hop count = 1 sits at-or-below ``max_hops=1`` so the row IS
    saved. Relay still happens too."""
    await env["inbound"]._on_moment_created(_moment_event(hop_count=1))
    saved = await env["moments"].get("m-pass")
    assert saved is not None
    assert saved.hop_count == 1
    # Relay fires (hop=1 → bumped to 2 onward).
    sent = [c.kwargs for c in env["federation"].send_event.call_args_list]
    assert sent and all(c["payload"]["hop_count"] == 2 for c in sent)


async def test_banned_source_drops_envelope_entirely(env):
    """Add the author's home to the household instance-ban list:
    the inbound is dropped at the policy gate before any persist
    OR relay."""
    await env["bans"].add(instance_id="inst-remote", reason="spam")

    await env["inbound"]._on_moment_created(_moment_event(hop_count=1))
    assert await env["moments"].get("m-pass") is None
    env["federation"].send_event.assert_not_called()


# ── §CP.R decision 14: protected accounts don't change the relay ─────────


async def _protected_only(db) -> None:
    """The household's one user is a protected account with the default
    (widest) ``max_hops``."""
    await db.enqueue(
        "UPDATE users SET child_protection_enabled=1, preferences_json='{}'"
        " WHERE user_id='u-self'"
    )


async def test_a_protected_only_household_passes_a_far_moment_through(db, env):
    """Nobody here may see a 2-hop moment (a protected account sees its
    household and paired households only): not stored — still relayed."""
    await _protected_only(db)
    await env["inbound"]._on_moment_created(_moment_event(hop_count=2))
    assert await env["moments"].get("m-pass") is None
    sent = [c.kwargs for c in env["federation"].send_event.call_args_list]
    assert sent and all(c["payload"]["hop_count"] == 3 for c in sent)


async def test_a_protected_only_household_keeps_and_relays_a_paired_moment(db, env):
    await _protected_only(db)
    await env["inbound"]._on_moment_created(_moment_event(hop_count=1))
    assert await env["moments"].get_readable("m-pass", "u-self") is not None
    sent = [c.kwargs for c in env["federation"].send_event.call_args_list]
    assert sent and all(c["payload"]["hop_count"] == 2 for c in sent)


async def test_a_mixed_household_stores_a_far_moment_for_the_adult_only(db, env):
    """An adult housemate may see it: stored and relayed — the protected
    account's reads still leave it out."""
    await _protected_only(db)
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state)"
        " VALUES('u-adult','bob','Bob','active')"
    )
    await env["inbound"]._on_moment_created(_moment_event(hop_count=2))
    assert await env["moments"].get_readable("m-pass", "u-adult") is not None
    assert await env["moments"].get_readable("m-pass", "u-self") is None
    assert env["federation"].send_event.called
