"""Tests for the SPACE_ROUTED inner-payload sealing primitives.

Mirrors the property tests we'd write for the pairing-coordinator
key derivation: round-trip, directional-key mismatch detection,
AAD-binding (a ciphertext for one route_id can't decrypt as
another), and forward-vs-reply separation.
"""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from socialhome.crypto import generate_identity_keypair
from socialhome.federation import routed_crypto as rc
from socialhome.federation.route_discovery import _route_found_signing_bytes


# ── Round-trip ─────────────────────────────────────────────────────────


def _ephemeral_pair() -> tuple[str, str, str, str]:
    """Convenience — returns ``(origin_priv, origin_pub, target_priv,
    target_pub)`` for use in tests."""
    o_priv, o_pub = rc.generate_ephemeral_keypair()
    t_priv, t_pub = rc.generate_ephemeral_keypair()
    return o_priv, o_pub, t_priv, t_pub


def test_seal_inner_round_trip():
    """Forward direction: origin seals → target unseals → plaintext."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    sealed = rc.seal_inner_payload(
        inner_payload_json='{"token":"abc","user":"alice"}',
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    decoded = rc.unseal_inner_payload(
        sealed=sealed,
        target_eph_priv_b64=t_priv,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    assert decoded == '{"token":"abc","user":"alice"}'


def test_seal_reply_round_trip():
    """Reply direction: target seals → origin unseals → plaintext."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    sealed = rc.seal_reply_payload(
        inner_payload_json='{"space_id":"s-1","role":"member"}',
        target_eph_priv_b64=t_priv,
        target_eph_pub_b64=t_pub,
        origin_eph_pub_b64=o_pub,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    decoded = rc.unseal_reply_payload(
        sealed=sealed,
        origin_eph_priv_b64=o_priv,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    assert decoded == '{"space_id":"s-1","role":"member"}'


# ── Negative paths ─────────────────────────────────────────────────────


def test_relay_cannot_decrypt():
    """A relay holds neither ephemeral private half → can't decrypt
    even with full knowledge of both ephemeral pubs."""
    o_priv, o_pub, _t_priv, t_pub = _ephemeral_pair()
    sealed = rc.seal_inner_payload(
        inner_payload_json='{"secret":"don\'t look"}',
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    # Relay generates its own keypair, both pubs are public, but it
    # has neither private half — so any DH it computes yields a
    # different shared secret than origin+target derived.
    r_priv, _r_pub = rc.generate_ephemeral_keypair()
    with pytest.raises(InvalidTag):
        rc.unseal_inner_payload(
            sealed=sealed,
            target_eph_priv_b64=r_priv,  # wrong priv
            route_id="r-1",
            inner_event_type="space_invite_token_redeem",
        )


def test_aad_route_id_binding():
    """A ciphertext sealed for one route_id can't be replayed under a
    different route_id — AAD mismatch fails the AEAD tag."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    sealed = rc.seal_inner_payload(
        inner_payload_json='{"token":"t-1"}',
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    with pytest.raises(InvalidTag):
        rc.unseal_inner_payload(
            sealed=sealed,
            target_eph_priv_b64=t_priv,
            route_id="r-2",  # different route_id
            inner_event_type="space_invite_token_redeem",
        )


def test_aad_event_type_binding():
    """A ciphertext sealed for one inner_event_type can't be replayed
    under a different inner_event_type — AAD mismatch fails the AEAD
    tag."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    sealed = rc.seal_inner_payload(
        inner_payload_json='{"token":"t-1"}',
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    with pytest.raises(InvalidTag):
        rc.unseal_inner_payload(
            sealed=sealed,
            target_eph_priv_b64=t_priv,
            route_id="r-1",
            inner_event_type="space_post_created",  # different event
        )


def test_forward_ciphertext_cannot_be_replayed_as_ack():
    """A forward-direction ciphertext can't be passed off as a reply —
    forward uses origin→target key, reply uses target→origin key, and
    the AAD has an ``|ack`` suffix on the reply side."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    sealed = rc.seal_inner_payload(
        inner_payload_json='{"token":"t-1"}',
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="space_invite_token_redeem",
    )
    # Try to decrypt the FORWARD ciphertext as if it were a REPLY —
    # the origin would use ``unseal_reply_payload`` with its own
    # private half. That key is target→origin, not origin→target, so
    # the AEAD tag fails.
    with pytest.raises(InvalidTag):
        rc.unseal_reply_payload(
            sealed=sealed,
            origin_eph_priv_b64=o_priv,
            route_id="r-1",
            inner_event_type="space_invite_token_redeem",
        )


def test_directional_keys_swap_on_role():
    """Origin's send_key equals target's recv_key, and vice versa —
    the invariant the directional-key derivation gates on."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    o_send, o_recv = rc.derive_directional_keys(
        my_priv_b64=o_priv,
        peer_pub_b64=t_pub,
        is_origin=True,
    )
    t_send, t_recv = rc.derive_directional_keys(
        my_priv_b64=t_priv,
        peer_pub_b64=o_pub,
        is_origin=False,
    )
    assert o_send == t_recv
    assert o_recv == t_send


def test_missing_sealed_fields_raise_valueerror():
    """Malformed wire payload (missing fields) raises a clean
    ValueError rather than KeyError."""
    o_priv, _o_pub, _t_priv, _t_pub = _ephemeral_pair()
    with pytest.raises(ValueError):
        rc.unseal_inner_payload(
            sealed={"origin_eph_pk": "x"},  # missing nonce + ciphertext
            target_eph_priv_b64=o_priv,
            route_id="r-1",
            inner_event_type="space_invite_token_redeem",
        )


def test_expired_helper():
    """``expired`` returns True past the TTL window."""
    import time

    now = time.monotonic()
    assert not rc.expired(now, 60.0)
    assert rc.expired(now - 120.0, 60.0)


def test_nonce_is_fresh_per_seal():
    """Two seals of the same plaintext under the same key produce
    different ciphertexts — AES-GCM requires unique nonces and this
    is the regression-pin that we draw a fresh one each call."""
    o_priv, o_pub, _t_priv, t_pub = _ephemeral_pair()
    a = rc.seal_inner_payload(
        inner_payload_json='{"x":1}',
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="t",
    )
    b = rc.seal_inner_payload(
        inner_payload_json='{"x":1}',
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="t",
    )
    assert a["nonce"] != b["nonce"]
    assert a["ciphertext"] != b["ciphertext"]


def test_kem_suite_in_wire_shape():
    """Both seal paths set ``kem_suite`` on the wire — receivers
    check the field on unseal so PQ migration (Phase 2) becomes a
    suite-bump, not a wire-shape break."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    fwd = rc.seal_inner_payload(
        inner_payload_json="{}",
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="t",
    )
    assert fwd["kem_suite"] == rc.KEM_SUITE_X25519
    reply = rc.seal_reply_payload(
        inner_payload_json="{}",
        target_eph_priv_b64=t_priv,
        target_eph_pub_b64=t_pub,
        origin_eph_pub_b64=o_pub,
        route_id="r-1",
        inner_event_type="t",
    )
    assert reply["kem_suite"] == rc.KEM_SUITE_X25519


def test_unknown_kem_suite_rejected():
    """A future ``kem_suite=mlkem768`` envelope arriving at a build
    that only knows ``x25519`` must be rejected, not silently
    downgraded — otherwise a hostile peer could force every receiver
    onto the weakest known suite."""
    o_priv, o_pub, t_priv, t_pub = _ephemeral_pair()
    sealed = rc.seal_inner_payload(
        inner_payload_json="{}",
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=t_pub,
        route_id="r-1",
        inner_event_type="t",
    )
    sealed["kem_suite"] = "mlkem768-future"
    with pytest.raises(rc.UnsupportedKemSuite):
        rc.unseal_inner_payload(
            sealed=sealed,
            target_eph_priv_b64=t_priv,
            route_id="r-1",
            inner_event_type="t",
        )
    with pytest.raises(rc.UnsupportedKemSuite):
        rc.unseal_reply_payload(
            sealed=sealed,
            origin_eph_priv_b64=o_priv,
            route_id="r-1",
            inner_event_type="t",
        )


# ── Route-stale nack signature ─────────────────────────────────────────


def _identity() -> tuple[bytes, bytes]:
    """``(seed, public_key)`` for a fresh Ed25519 identity."""
    kp = generate_identity_keypair()
    return kp.private_key, kp.public_key


def test_route_stale_signing_bytes_exact_shape():
    """The signed bytes are the documented ``space-route-stale:v1:``
    domain tag + route_id + ``:`` + stale pub — nothing else, so an
    independent implementation can reproduce them byte-for-byte."""
    got = rc.route_stale_signing_bytes("r-1", "STALEPK")
    assert got == b"space-route-stale:v1:r-1:STALEPK"


def test_route_stale_signing_bytes_domain_separated_from_route_found():
    """Same (id, pub) inputs must NOT collide with the ROUTE_FOUND
    signing bytes — otherwise a captured ROUTE_FOUND signature could be
    replayed as a route-stale nack (or vice versa)."""
    stale = rc.route_stale_signing_bytes("r-1", "PK")
    found = _route_found_signing_bytes("r-1", "PK")
    assert stale != found
    assert stale.startswith(b"space-route-stale:v1:")
    assert found.startswith(b"space-route-found:v1:")


def test_route_stale_sig_suite_constants():
    assert rc.ROUTE_STALE_SIG_SUITE_ED25519 == "ed25519"
    assert rc.SUPPORTED_ROUTE_STALE_SIG_SUITES == frozenset({"ed25519"})
    assert issubclass(rc.UnsupportedRouteStaleSuite, ValueError)


def test_sign_verify_route_stale_round_trip():
    seed, pk = _identity()
    sig = rc.sign_route_stale(seed=seed, route_id="r-1", stale_eph_pk_b64="PK")
    assert isinstance(sig, str)
    assert rc.verify_route_stale(
        identity_pk=pk,
        route_id="r-1",
        stale_eph_pk_b64="PK",
        sig_b64=sig,
        sig_suite=rc.ROUTE_STALE_SIG_SUITE_ED25519,
    )


def test_verify_route_stale_wrong_key_false():
    seed, _pk = _identity()
    _other_seed, other_pk = _identity()
    sig = rc.sign_route_stale(seed=seed, route_id="r-1", stale_eph_pk_b64="PK")
    assert not rc.verify_route_stale(
        identity_pk=other_pk,
        route_id="r-1",
        stale_eph_pk_b64="PK",
        sig_b64=sig,
        sig_suite="ed25519",
    )


def test_verify_route_stale_tampered_route_id_false():
    seed, pk = _identity()
    sig = rc.sign_route_stale(seed=seed, route_id="r-1", stale_eph_pk_b64="PK")
    assert not rc.verify_route_stale(
        identity_pk=pk,
        route_id="r-2",
        stale_eph_pk_b64="PK",
        sig_b64=sig,
        sig_suite="ed25519",
    )


def test_verify_route_stale_tampered_stale_pk_false():
    seed, pk = _identity()
    sig = rc.sign_route_stale(seed=seed, route_id="r-1", stale_eph_pk_b64="PK")
    assert not rc.verify_route_stale(
        identity_pk=pk,
        route_id="r-1",
        stale_eph_pk_b64="OTHER",
        sig_b64=sig,
        sig_suite="ed25519",
    )


@pytest.mark.parametrize("bad_sig", ["", "!!!not-b64!!!", "AAAA", "x" * 100])
def test_verify_route_stale_malformed_sig_false_not_raise(bad_sig):
    """Garbage / wrong-length signatures return False — matching
    ``crypto.verify_ed25519``'s posture — never raise."""
    _seed, pk = _identity()
    assert (
        rc.verify_route_stale(
            identity_pk=pk,
            route_id="r-1",
            stale_eph_pk_b64="PK",
            sig_b64=bad_sig,
            sig_suite="ed25519",
        )
        is False
    )


def test_verify_route_stale_bad_identity_pk_false_not_raise():
    seed, _pk = _identity()
    sig = rc.sign_route_stale(seed=seed, route_id="r-1", stale_eph_pk_b64="PK")
    assert (
        rc.verify_route_stale(
            identity_pk=b"short",
            route_id="r-1",
            stale_eph_pk_b64="PK",
            sig_b64=sig,
            sig_suite="ed25519",
        )
        is False
    )


@pytest.mark.parametrize("suite", ["", "ED25519", "ed25519+mldsa65", "rsa"])
def test_verify_route_stale_unknown_suite_raises(suite):
    """An unknown ``sig_suite`` is rejected outright — never verified
    under a default algorithm — so a future PQ migration can't be
    downgraded by a peer stripping the tag."""
    seed, pk = _identity()
    sig = rc.sign_route_stale(seed=seed, route_id="r-1", stale_eph_pk_b64="PK")
    with pytest.raises(rc.UnsupportedRouteStaleSuite):
        rc.verify_route_stale(
            identity_pk=pk,
            route_id="r-1",
            stale_eph_pk_b64="PK",
            sig_b64=sig,
            sig_suite=suite,
        )


# ── Routed origin authentication (#692, v_31) ─────────────────────────


def _sealed_fixture() -> dict[str, str]:
    """A sealed blob shaped exactly as the wire carries it."""
    origin_priv, origin_pub = rc.generate_ephemeral_keypair()
    _target_priv, target_pub = rc.generate_ephemeral_keypair()
    return rc.seal_inner_payload(
        inner_payload_json='{"post_id":"p1"}',
        origin_eph_priv_b64=origin_priv,
        origin_eph_pub_b64=origin_pub,
        target_eph_pub_b64=target_pub,
        route_id="r-1",
        inner_event_type="space_post_created",
    )


def test_routed_origin_sig_suite_constants():
    assert rc.ROUTED_ORIGIN_SIG_SUITE_ED25519 == "ed25519"
    assert rc.SUPPORTED_ROUTED_ORIGIN_SIG_SUITES == frozenset({"ed25519"})
    assert issubclass(rc.UnsupportedRoutedOriginSuite, ValueError)


def test_routed_origin_signing_bytes_domain_separated():
    """The tag must not collide with ROUTE_FOUND (binds a pub as live)
    or ROUTE_STALE (binds the same pub as dead) — a signature from one
    surface must never verify on another."""
    got = rc.routed_origin_signing_bytes(
        route_id="r-1",
        direction="forward",
        path=["a", "b"],
        inner_event_type="space_post_created",
        sealed=_sealed_fixture(),
    )
    assert got.startswith(b"space-routed-origin:v1:forward:r-1:a|b:space_post_created:")
    assert not got.startswith(b"space-route-stale:v1:")
    assert not got.startswith(b"space-route-found:v1:")


def test_routed_origin_signing_bytes_cover_every_sealed_field():
    """Each piece of the sealed material is inside the digest — flipping
    any one of them changes the bytes, so a relay can't swap ephemerals
    or ciphertext under a captured signature."""
    sealed = _sealed_fixture()

    def _bytes(mutated: dict[str, str]) -> bytes:
        return rc.routed_origin_signing_bytes(
            route_id="r-1",
            direction="forward",
            path=["a", "b"],
            inner_event_type="space_post_created",
            sealed=mutated,
        )

    base = _bytes(sealed)
    for field in ("kem_suite", "origin_eph_pk", "target_eph_pk", "nonce", "ciphertext"):
        assert _bytes({**sealed, field: "tampered"}) != base, field


def test_sign_verify_routed_origin_round_trip():
    seed, pk = _identity()
    sealed = _sealed_fixture()
    kwargs = {
        "route_id": "r-1",
        "direction": "forward",
        "path": ["a", "b", "c"],
        "inner_event_type": "space_post_created",
        "sealed": sealed,
    }
    sig = rc.sign_routed_origin(seed=seed, **kwargs)
    assert isinstance(sig, str)
    assert rc.verify_routed_origin(
        identity_pk=pk,
        sig_b64=sig,
        sig_suite=rc.ROUTED_ORIGIN_SIG_SUITE_ED25519,
        **kwargs,
    )


def test_verify_routed_origin_wrong_key_false():
    """The whole point: a signature by one household does not verify as
    another's, so ``path[0]`` can't be claimed by a relay."""
    seed, _pk = _identity()
    _other_seed, other_pk = _identity()
    sealed = _sealed_fixture()
    kwargs = {
        "route_id": "r-1",
        "direction": "forward",
        "path": ["a", "b", "c"],
        "inner_event_type": "space_post_created",
        "sealed": sealed,
    }
    sig = rc.sign_routed_origin(seed=seed, **kwargs)
    assert not rc.verify_routed_origin(
        identity_pk=other_pk,
        sig_b64=sig,
        sig_suite=rc.ROUTED_ORIGIN_SIG_SUITE_ED25519,
        **kwargs,
    )


@pytest.mark.parametrize("bad_sig", ["", "!!not-base64!!", "AAAA"])
def test_verify_routed_origin_malformed_sig_false_not_raise(bad_sig):
    """Malformed input is a ``False``, never an exception — the inbound
    path must drop, not crash."""
    _seed, pk = _identity()
    assert (
        rc.verify_routed_origin(
            identity_pk=pk,
            route_id="r-1",
            direction="forward",
            path=["a", "b"],
            inner_event_type="space_post_created",
            sealed=_sealed_fixture(),
            sig_b64=bad_sig,
            sig_suite=rc.ROUTED_ORIGIN_SIG_SUITE_ED25519,
        )
        is False
    )


@pytest.mark.parametrize("suite", ["", "ED25519", "ed25519+mldsa65", "rsa"])
def test_verify_routed_origin_unknown_suite_raises(suite):
    """Unknown suite → reject outright, never verify under a default, so
    a peer stripping the tag can't downgrade the Phase-2 PQ migration."""
    seed, pk = _identity()
    kwargs = {
        "route_id": "r-1",
        "direction": "forward",
        "path": ["a", "b"],
        "inner_event_type": "space_post_created",
        "sealed": _sealed_fixture(),
    }
    sig = rc.sign_routed_origin(seed=seed, **kwargs)
    with pytest.raises(rc.UnsupportedRoutedOriginSuite):
        rc.verify_routed_origin(identity_pk=pk, sig_b64=sig, sig_suite=suite, **kwargs)
