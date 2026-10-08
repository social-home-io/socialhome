"""§27.9 release blocker: the GFS relay fallback for PAIRED households.

A paired household (``source = manual``) may be reached over a connection
server only as a last resort, only when this household opted in
(``RemoteInstance.gfs_relay``), and with the same identity-free wire shape
as a link-joined one. Three things are pinned:

* the relay body is ``{to_instance, sealed}`` and nothing else — no route
  URL, no connection-server id, no inbox id, no sender;
* nothing is relayed to a paired peer we did not opt in with, and a 4xx
  from its inbox never moves the envelope onto a connection server;
* a relayed envelope from a paired peer we did not opt in with is refused
  by the §24.11 pipeline before any crypto runs.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from socialhome.crypto import generate_x25519_keypair
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.gfs_relay_transport import GfsRelayTransport
from socialhome.federation.inbound_validator import (
    TRANSPORT_GFS_RELAY,
    InboundContext,
    make_check_relay_opt_in,
)
from socialhome.federation.transport import FederationTransport
from socialhome.global_server.envelope_relay import SEALED_KEYS

pytestmark = pytest.mark.security

ROUTE = "https://route-gfs.example.org"
LOCAL_INBOX_ID = "local-inbox-secret-id"

ENVELOPE = {
    "msg_id": "msg-paired-1",
    "event_type": "dm_message",
    "from_instance": "1234567890abcdef1234567890abcdef",
    "to_instance": "fedcba0987654321fedcba0987654321",
    "timestamp": "2026-10-07T10:00:00+00:00",
    "encrypted_payload": "bm9uY2U:Y2lwaGVy",
    "proto_version": 1,
    "sig_suite": "ed25519",
    "signatures": {"ed25519": "c2ln"},
}


class _RecordingSender:
    """What the production ``GfsEnvelopeSender`` would POST, per server."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def send_sealed_envelope(self, *, to_instance_id, envelope, gfs_url=""):
        self.posts.append(
            (gfs_url, {"to_instance": to_instance_id, "sealed": envelope["sealed"]}),
        )
        return True


class _Inbox:
    def __init__(self, ok: bool, status: int | None) -> None:
        self.result = (ok, status)

    async def send(self, *, instance, envelope_dict):
        return self.result


async def _no_signal(*_a, **_kw):
    return None


def _paired(keywrap_pub: bytes, *, gfs_relay: bool = True) -> RemoteInstance:
    return RemoteInstance(
        id=ENVELOPE["to_instance"],
        display_name="Paired household",
        remote_identity_pk="cc" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="",
        local_inbox_id=LOCAL_INBOX_ID,
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
        relay_via="introducer-instance-id",
        remote_keywrap_pk=keywrap_pub.hex(),
        gfs_relay=gfs_relay,
    )


def _transport(sender, *, inbox=None) -> FederationTransport:
    async def _routes(_iid: str) -> list[str]:
        return [ROUTE]

    t = FederationTransport(
        own_instance_id=ENVELOPE["from_instance"],
        https_inbox=inbox or _Inbox(False, None),
        gfs_relay=GfsRelayTransport(relay_sender=sender),
        gfs_routes=_routes,
        signaling_send=_no_signal,
    )
    t.mark_ice_primed()
    t._rtc_suppressed_until[ENVELOPE["to_instance"]] = float("inf")
    return t


async def test_the_paired_relay_body_names_no_route_server_inbox_or_sender():
    kp = generate_x25519_keypair()
    sender = _RecordingSender()

    result = await _transport(sender).send(
        instance=_paired(kp.public_key),
        envelope_dict=ENVELOPE,
    )

    assert result.ok and result.via == "gfs_relay"
    gfs_url, body = sender.posts[0]
    # The route only picks which of OUR connections carries the blob.
    assert gfs_url == ROUTE
    assert set(body) == {"to_instance", "sealed"}
    assert set(body["sealed"]) == set(SEALED_KEYS)
    on_the_wire = json.dumps(body)
    for leak in (
        ROUTE,
        "route-gfs",
        LOCAL_INBOX_ID,
        "introducer-instance-id",
        ENVELOPE["from_instance"],
        ENVELOPE["event_type"],
        ENVELOPE["msg_id"],
        ENVELOPE["encrypted_payload"],
    ):
        assert leak not in on_the_wire, f"relay body leaks {leak!r}"


async def test_nothing_is_relayed_to_a_paired_peer_we_did_not_opt_in_with():
    kp = generate_x25519_keypair()
    sender = _RecordingSender()

    result = await _transport(sender).send(
        instance=_paired(kp.public_key, gfs_relay=False),
        envelope_dict=ENVELOPE,
    )

    assert result.ok is False
    assert sender.posts == []


@pytest.mark.parametrize("status", [400, 403, 404, 410, 429])
async def test_a_refusal_from_the_peer_never_moves_onto_a_connection_server(status):
    kp = generate_x25519_keypair()
    sender = _RecordingSender()
    peer = dataclasses.replace(
        _paired(kp.public_key),
        remote_inbox_url="https://peer.example.org/inbox",
    )

    result = await _transport(sender, inbox=_Inbox(False, status)).send(
        instance=peer,
        envelope_dict=ENVELOPE,
    )

    assert result.via == "https" and result.ok is False
    assert sender.posts == []


class _Row:
    def __init__(self, gfs_relay: bool) -> None:
        self.source = InstanceSource.MANUAL
        self.gfs_relay = gfs_relay
        self.from_instance = ENVELOPE["from_instance"]


async def test_the_pipeline_refuses_a_relayed_envelope_without_our_opt_in():
    step = make_check_relay_opt_in()

    with pytest.raises(ValueError):
        await step(
            InboundContext(
                envelope=dict(ENVELOPE),
                instance=_Row(gfs_relay=False),
                transport=TRANSPORT_GFS_RELAY,
            ),
        )
    await step(
        InboundContext(
            envelope=dict(ENVELOPE),
            instance=_Row(gfs_relay=True),
            transport=TRANSPORT_GFS_RELAY,
        ),
    )
