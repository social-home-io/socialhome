"""§27.9 release blocker: the relay body for a SPACE event names a
recipient and carries ciphertext — nothing else.

A §24.11 envelope is encrypted and signed, but its *routing* fields are
plaintext by construction: ``from_instance``, ``to_instance``,
``event_type``, ``space_id``, ``msg_id``, ``timestamp``. Households
seated from an invite link reach each other only through the connection
server (``federation/gfs_relay_transport.py``), so handing that envelope
to the relay as-is would hand a third party the social graph the whole
§D2b design exists to withhold: who talks to whom, about which space,
how often.

This pins the wire shape that prevents it. Every assertion fails against
an implementation that ships the envelope unsealed, or that grows a
third field on the relay body.
"""

from __future__ import annotations

import json

import orjson
import pytest

from socialhome.crypto import generate_x25519_keypair
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.gfs_relay_transport import (
    RELAY_KIND_ENVELOPE,
    RELAY_MAX_BODY_BYTES,
    GfsRelayTransport,
)
from socialhome.federation.keywrap_seal import open_keywrap
from socialhome.global_server.envelope_relay import (
    ENVELOPE_MAX_BODY_BYTES,
    SEALED_KEYS,
)


pytestmark = pytest.mark.security


SPACE_ENVELOPE = {
    "msg_id": "msg-e2e-1",
    "event_type": "space_post_created",
    "from_instance": "1234567890abcdef1234567890abcdef",
    "to_instance": "fedcba0987654321fedcba0987654321",
    "timestamp": "2026-09-18T10:00:00+00:00",
    "encrypted_payload": "bm9uY2U:Y2lwaGVy",
    "space_id": "space-book-club",
    "proto_version": 1,
    "sig_suite": "ed25519",
    "signatures": {"ed25519": "c2ln"},
}


class _RecordingRelay:
    def __init__(self) -> None:
        self.bodies: list[dict] = []

    async def send_sealed_envelope(self, *, to_instance_id, envelope, gfs_url=""):
        # Mirror what the production sender POSTs: the recipient plus the
        # seal, rebuilt rather than forwarded.
        self.bodies.append(
            {"to_instance": to_instance_id, "sealed": envelope["sealed"]}
        )
        return True


def _instance(keywrap_pub: bytes) -> RemoteInstance:
    return RemoteInstance(
        id=SPACE_ENVELOPE["to_instance"],
        display_name="Link household",
        remote_identity_pk="cc" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="",
        local_inbox_id="wh-1",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.SPACE_SESSION,
        relay_via="https://gfs.example.org",
        remote_keywrap_pk=keywrap_pub.hex(),
    )


async def test_the_relay_body_for_a_space_event_holds_no_routing_field():
    """Fails against an implementation that relays the §24.11 envelope
    unsealed — its plaintext routing fields would all appear here."""
    kp = generate_x25519_keypair()
    relay = _RecordingRelay()

    await GfsRelayTransport(relay_sender=relay).send(
        instance=_instance(kp.public_key),
        envelope_dict=SPACE_ENVELOPE,
    )

    body = relay.bodies[0]
    assert set(body) == {"to_instance", "sealed"}
    assert set(body["sealed"]) == set(SEALED_KEYS)
    assert body["to_instance"] == SPACE_ENVELOPE["to_instance"]

    on_the_wire = json.dumps(body)
    for leak in (
        SPACE_ENVELOPE["from_instance"],
        SPACE_ENVELOPE["event_type"],
        SPACE_ENVELOPE["space_id"],
        SPACE_ENVELOPE["msg_id"],
        SPACE_ENVELOPE["timestamp"],
        SPACE_ENVELOPE["encrypted_payload"],
        "from_instance",
        "event_type",
        "space_id",
    ):
        assert leak not in on_the_wire, f"relay body leaks {leak!r}"


async def test_only_the_addressed_household_can_open_the_relay_body():
    """The seal is to the peer's key-wrap key; the relay holds no key
    that opens it, and the marker inside says which family it is."""
    kp = generate_x25519_keypair()
    other = generate_x25519_keypair()
    relay = _RecordingRelay()

    await GfsRelayTransport(relay_sender=relay).send(
        instance=_instance(kp.public_key),
        envelope_dict=SPACE_ENVELOPE,
    )
    sealed = relay.bodies[0]["sealed"]

    with pytest.raises(Exception):
        open_keywrap(sealed=sealed, recipient_keywrap_priv=other.private_key)

    plain = orjson.loads(
        open_keywrap(sealed=sealed, recipient_keywrap_priv=kp.private_key),
    )
    assert plain == {"kind": RELAY_KIND_ENVELOPE, "envelope": SPACE_ENVELOPE}


async def test_the_sealed_body_fits_the_connection_server_contract():
    """A body over the server's cap is refused at the door — so the
    household's ceiling must be the server's, and the sealed body must
    stay under it."""
    assert RELAY_MAX_BODY_BYTES == ENVELOPE_MAX_BODY_BYTES
    kp = generate_x25519_keypair()
    relay = _RecordingRelay()

    await GfsRelayTransport(relay_sender=relay).send(
        instance=_instance(kp.public_key),
        envelope_dict=SPACE_ENVELOPE,
    )

    assert len(orjson.dumps(relay.bodies[0])) <= ENVELOPE_MAX_BODY_BYTES


async def test_every_seal_is_fresh_so_two_identical_events_do_not_match():
    """A relay that could match two identical ciphertexts would learn
    when a household re-sends the same event."""
    kp = generate_x25519_keypair()
    relay = _RecordingRelay()
    transport = GfsRelayTransport(relay_sender=relay)

    await transport.send(
        instance=_instance(kp.public_key), envelope_dict=SPACE_ENVELOPE
    )
    await transport.send(
        instance=_instance(kp.public_key), envelope_dict=SPACE_ENVELOPE
    )

    first, second = relay.bodies
    assert first["sealed"]["ciphertext"] != second["sealed"]["ciphertext"]
    assert first["sealed"]["eph_pk"] != second["sealed"]["eph_pk"]
