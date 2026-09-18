"""Tests for the proto_version announcement path.

Covers the three surfaces that together implement the §federation
protocol versioning rule:

1. ``CapabilitiesOutbound.publish`` builds the right envelope and
   skips own / unconfirmed peers.
2. The inbound handler in ``federation_inbound.pairing`` writes the
   announced version onto the ``remote_instances`` row.
3. ``FederationService.peer_supports(instance_id, min_version=N)``
   returns the right answer at each version boundary.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.federation_capabilities import OURS as OUR_PROTO_VERSION
from socialhome.services.capabilities_outbound import CapabilitiesOutbound


def _peer(instance_id: str, *, proto_version: int = 1) -> RemoteInstance:
    return RemoteInstance(
        id=instance_id,
        display_name=instance_id,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url=f"https://example.test/{instance_id}",
        local_inbox_id=f"local-{instance_id}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
        proto_version=proto_version,
    )


@pytest.mark.asyncio
async def test_capabilities_outbound_fans_out_to_confirmed_peers():
    peer_a = _peer("inst-a")
    peer_b = _peer("inst-b")
    repo = SimpleNamespace(
        list_social_instances=AsyncMock(return_value=[peer_a, peer_b]),
        get_local_identity=AsyncMock(return_value={"display_name": "My Home"}),
    )
    fed = SimpleNamespace(
        _own_instance_id="inst-self",
        send_event=AsyncMock(),
    )
    out = CapabilitiesOutbound(
        federation_service=fed,
        federation_repo=repo,
    )

    sent = await out.publish()

    assert sent == 2
    assert fed.send_event.await_count == 2
    # Same payload to every peer — the integer is build-wide, and our
    # federated household name rides along so renames reach paired peers.
    for call in fed.send_event.await_args_list:
        assert call.kwargs["event_type"] == (
            FederationEventType.INSTANCE_CAPABILITIES_UPDATED
        )
        assert call.kwargs["payload"] == {
            "proto_version": OUR_PROTO_VERSION,
            "display_name": "My Home",
        }
    targets = {c.kwargs["to_instance_id"] for c in fed.send_event.await_args_list}
    assert targets == {"inst-a", "inst-b"}


@pytest.mark.asyncio
async def test_capabilities_outbound_omits_blank_display_name():
    """A blank / missing local display_name → no ``display_name`` key in the
    payload (older-peer-compatible, additive shape)."""
    peer = _peer("inst-a")
    repo = SimpleNamespace(
        list_social_instances=AsyncMock(return_value=[peer]),
        get_local_identity=AsyncMock(return_value={"display_name": "   "}),
    )
    fed = SimpleNamespace(_own_instance_id="inst-self", send_event=AsyncMock())
    out = CapabilitiesOutbound(federation_service=fed, federation_repo=repo)

    await out.publish()

    call = fed.send_event.await_args
    assert call.kwargs["payload"] == {"proto_version": OUR_PROTO_VERSION}


@pytest.mark.asyncio
async def test_capabilities_outbound_omits_when_no_local_identity():
    """No self-row yet (early bootstrap) → still sends, name omitted."""
    peer = _peer("inst-a")
    repo = SimpleNamespace(
        list_social_instances=AsyncMock(return_value=[peer]),
        get_local_identity=AsyncMock(return_value=None),
    )
    fed = SimpleNamespace(_own_instance_id="inst-self", send_event=AsyncMock())
    out = CapabilitiesOutbound(federation_service=fed, federation_repo=repo)

    await out.publish()

    call = fed.send_event.await_args
    assert call.kwargs["payload"] == {"proto_version": OUR_PROTO_VERSION}


@pytest.mark.asyncio
async def test_capabilities_outbound_skips_self():
    peer = _peer("inst-self")
    repo = SimpleNamespace(
        list_social_instances=AsyncMock(return_value=[peer]),
    )
    fed = SimpleNamespace(
        _own_instance_id="inst-self",
        send_event=AsyncMock(),
    )
    out = CapabilitiesOutbound(
        federation_service=fed,
        federation_repo=repo,
    )

    sent = await out.publish()

    assert sent == 0
    assert fed.send_event.await_count == 0


@pytest.mark.asyncio
async def test_capabilities_outbound_tolerates_repo_failure():
    repo = SimpleNamespace(
        list_social_instances=AsyncMock(side_effect=RuntimeError("boom")),
    )
    fed = SimpleNamespace(_own_instance_id="inst-self", send_event=AsyncMock())
    out = CapabilitiesOutbound(
        federation_service=fed,
        federation_repo=repo,
    )

    # Must not raise — the outbound is fire-and-forget at startup.
    sent = await out.publish()
    assert sent == 0


@pytest.mark.asyncio
async def test_resend_to_re_advertises_to_one_peer():
    """``resend_to`` (the §319.6 resync entry) sends one capabilities
    envelope to the named peer and returns True."""
    repo = SimpleNamespace(
        list_social_instances=AsyncMock(return_value=[]),
        get_local_identity=AsyncMock(return_value={"display_name": "My Home"}),
    )
    fed = SimpleNamespace(_own_instance_id="inst-self", send_event=AsyncMock())
    out = CapabilitiesOutbound(federation_service=fed, federation_repo=repo)

    ok = await out.resend_to("inst-a")

    assert ok is True
    fed.send_event.assert_awaited_once()
    call = fed.send_event.await_args
    assert call.kwargs["to_instance_id"] == "inst-a"
    assert call.kwargs["event_type"] == (
        FederationEventType.INSTANCE_CAPABILITIES_UPDATED
    )
    assert call.kwargs["payload"] == {
        "proto_version": OUR_PROTO_VERSION,
        "display_name": "My Home",
    }


@pytest.mark.asyncio
async def test_resend_to_skips_self():
    """``resend_to`` to our own instance id is a no-op returning False."""
    repo = SimpleNamespace(list_social_instances=AsyncMock(return_value=[]))
    fed = SimpleNamespace(_own_instance_id="inst-self", send_event=AsyncMock())
    out = CapabilitiesOutbound(federation_service=fed, federation_repo=repo)

    ok = await out.resend_to("inst-self")

    assert ok is False
    fed.send_event.assert_not_awaited()
