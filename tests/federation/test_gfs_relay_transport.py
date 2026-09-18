"""Tests for :mod:`socialhome.federation.gfs_relay_transport`.

Real crypto — the seal is the whole point of this transport, so nothing
about it is faked. The only stand-in is the relay sender (the seam the
production :class:`~socialhome.services.gfs_envelope_sender
.GfsEnvelopeSender` fills).
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
    RELAY_STATUS_THROTTLED,
    RELAY_STATUS_TOO_LARGE,
    RELAY_MAX_BODY_BYTES,
    RELAY_MAX_ENVELOPE_BYTES,
    GfsRelayTransport,
    is_relay_envelope_body,
    seal_relay_envelope,
)
from socialhome.federation.invite_bootstrap import EnvelopeRelayThrottled
from socialhome.federation.keywrap_seal import open_keywrap
from socialhome.global_server.envelope_relay import ENVELOPE_MAX_BODY_BYTES


ENVELOPE = {
    "msg_id": "msg-1",
    "event_type": "space.post_created",
    "from_instance": "a" * 32,
    "to_instance": "b" * 32,
    "timestamp": "2026-09-18T10:00:00+00:00",
    "encrypted_payload": "nonce:ciphertext",
    "space_id": "space-xyz",
    "proto_version": 1,
    "sig_suite": "ed25519",
    "signatures": {"ed25519": "deadbeef"},
}


class _FakeRelay:
    """Records what the connection server would have been handed."""

    def __init__(self, *, ok: bool = True, raises: Exception | None = None) -> None:
        self.ok = ok
        self.raises = raises
        self.calls: list[dict] = []

    async def send_sealed_envelope(self, *, to_instance_id, envelope, gfs_url=""):
        if self.raises is not None:
            raise self.raises
        self.calls.append(
            {
                "to_instance_id": to_instance_id,
                "envelope": envelope,
                "gfs_url": gfs_url,
            },
        )
        return self.ok


def _instance(keypair, **over) -> RemoteInstance:
    fields = {
        "id": "b" * 32,
        "display_name": "Link household",
        "remote_identity_pk": "cc" * 32,
        "key_self_to_remote": "enc",
        "key_remote_to_self": "enc",
        "remote_inbox_url": "",
        "local_inbox_id": "wh-1",
        "status": PairingStatus.CONFIRMED,
        "source": InstanceSource.SPACE_SESSION,
        "relay_via": "https://gfs.example.org",
        "remote_keywrap_pk": keypair.public_key.hex(),
    }
    fields.update(over)
    return RemoteInstance(**fields)


# ─── The seal ─────────────────────────────────────────────────────────────


def test_the_sealed_plaintext_carries_the_kind_marker_and_the_envelope():
    """The receiver must not have to guess which family a blob belongs
    to — the marker inside the ciphertext says so."""
    kp = generate_x25519_keypair()

    sealed = seal_relay_envelope(
        envelope_dict=ENVELOPE,
        peer_keywrap_pub=kp.public_key,
    )

    assert set(sealed) == {"kem_suite", "eph_pk", "ciphertext"}
    assert sealed["kem_suite"] == "x25519"
    body = orjson.loads(
        open_keywrap(sealed=sealed, recipient_keywrap_priv=kp.private_key),
    )
    assert body["kind"] == RELAY_KIND_ENVELOPE
    assert body["envelope"] == ENVELOPE
    assert is_relay_envelope_body(body) is True
    assert is_relay_envelope_body({"kind": "space_invite_bootstrap_redeem"}) is False
    assert is_relay_envelope_body("not a dict") is False


async def test_the_relay_sees_a_recipient_and_ciphertext_and_nothing_else():
    """§24.11 routing fields are PLAINTEXT on the envelope — that is why
    the whole envelope is sealed again before the relay touches it."""
    kp = generate_x25519_keypair()
    relay = _FakeRelay()
    transport = GfsRelayTransport(relay_sender=relay)

    ok, status = await transport.send(instance=_instance(kp), envelope_dict=ENVELOPE)

    assert (ok, status) == (True, None)
    body = relay.calls[0]["envelope"]
    assert set(body) == {"to_instance", "sealed"}
    everything = json.dumps(body)
    for marker in (
        "space.post_created",
        "space-xyz",
        ENVELOPE["from_instance"],
        "msg-1",
        "nonce:ciphertext",
    ):
        assert marker not in everything, f"relay saw {marker!r}"


async def test_the_introducing_connection_server_carries_the_envelope():
    """A household paired with several servers must answer on the one the
    peer is listening to — the row remembers which."""
    kp = generate_x25519_keypair()
    relay = _FakeRelay()

    await GfsRelayTransport(relay_sender=relay).send(
        instance=_instance(kp),
        envelope_dict=ENVELOPE,
    )

    assert relay.calls[0]["gfs_url"] == "https://gfs.example.org"
    assert relay.calls[0]["to_instance_id"] == "b" * 32


# ─── Fail-closed behaviour ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "keywrap_pk",
    [None, "", "not-hex"],
    ids=["missing", "empty", "malformed"],
)
async def test_a_row_without_a_usable_keywrap_key_fails_closed(keywrap_pk):
    kp = generate_x25519_keypair()
    relay = _FakeRelay()

    ok, status = await GfsRelayTransport(relay_sender=relay).send(
        instance=_instance(kp, remote_keywrap_pk=keywrap_pk),
        envelope_dict=ENVELOPE,
    )

    assert (ok, status) == (False, None)
    assert relay.calls == []


async def test_a_relay_that_cannot_carry_it_is_a_failure_not_a_raise():
    """``TransportStrategy`` contract: transport-level failure returns
    ``(False, None)`` so the caller queues for retry. A configuration
    refusal from the sender (no connection server) is one of those."""
    kp = generate_x25519_keypair()
    relay = _FakeRelay(raises=RuntimeError("no connection server"))

    ok, status = await GfsRelayTransport(relay_sender=relay).send(
        instance=_instance(kp),
        envelope_dict=ENVELOPE,
    )

    assert (ok, status) == (False, None)


async def test_a_rejected_blob_reports_failure():
    kp = generate_x25519_keypair()
    relay = _FakeRelay(ok=False)

    ok, _status = await GfsRelayTransport(relay_sender=relay).send(
        instance=_instance(kp),
        envelope_dict=ENVELOPE,
    )

    assert ok is False


# ─── The size ceiling (media) ─────────────────────────────────────────────


def test_the_body_cap_matches_the_connection_server_contract():
    """The household's cap is a copy of the server's, and a copy that
    drifts ships envelopes that earn a 413."""
    assert RELAY_MAX_BODY_BYTES == ENVELOPE_MAX_BODY_BYTES
    # The pre-seal bound must leave room for base64 (4/3) plus framing.
    assert RELAY_MAX_ENVELOPE_BYTES * 4 // 3 < RELAY_MAX_BODY_BYTES


async def test_an_oversize_envelope_is_refused_here_not_at_the_relay(caplog):
    """A media chunk (512 KiB — 1 MiB, base64'd) cannot fit the relay.
    It is refused locally, loudly, so the failure names the real reason
    instead of surfacing as "the connection server is unhappy" — and it
    reports the status the relay itself would have answered, because the
    refusal is DETERMINISTIC: the outbox above must drop it permanently
    rather than spend five attempts re-deriving one length compare."""
    kp = generate_x25519_keypair()
    relay = _FakeRelay()
    oversize = {**ENVELOPE, "encrypted_payload": "x" * (RELAY_MAX_ENVELOPE_BYTES + 1)}

    with caplog.at_level("WARNING"):
        ok, status = await GfsRelayTransport(relay_sender=relay).send(
            instance=_instance(kp),
            envelope_dict=oversize,
        )

    assert (ok, status) == (False, RELAY_STATUS_TOO_LARGE)
    assert relay.calls == []
    assert "relay body cap" in caplog.text


async def test_a_throttled_relay_reports_a_waitable_status_not_a_bare_failure():
    """The sender raises on ``429`` so this tier can tell back-pressure
    apart from a broken relay. It must translate that into a *status* the
    facade above can classify — otherwise the throttle arrives upstairs
    as an ordinary ``(False, None)`` and the space-sync provider abandons
    a catch-up over a busy minute."""
    kp = generate_x25519_keypair()
    relay = _FakeRelay(raises=EnvelopeRelayThrottled("busy"))

    ok, status = await GfsRelayTransport(relay_sender=relay).send(
        instance=_instance(kp),
        envelope_dict=ENVELOPE,
    )

    assert (ok, status) == (False, RELAY_STATUS_THROTTLED)


async def test_a_configuration_refusal_is_still_a_plain_failure():
    """The other exception the sender may raise — no reachable relay, or
    one that cannot carry invite envelopes — is NOT waitable, and must
    not be mistaken for a throttle by the catch-all below it."""
    kp = generate_x25519_keypair()
    relay = _FakeRelay(raises=RuntimeError("no connection server"))

    ok, status = await GfsRelayTransport(relay_sender=relay).send(
        instance=_instance(kp),
        envelope_dict=ENVELOPE,
    )

    assert (ok, status) == (False, None)
