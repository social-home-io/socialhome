"""Unit tests for :mod:`socialhome.federation.moment_origin`."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.federation import FederationEventType
from socialhome.federation.moment_origin import (
    MOMENT_ORIGIN_SIG_SUITE_ED25519,
    SIGNED_MOMENT_EVENT_TYPES,
    SUPPORTED_MOMENT_ORIGIN_SIG_SUITES,
    UnsupportedMomentOriginSuite,
    check_relayed_moment_origin,
    moment_origin_signing_bytes,
    sign_moment_origin,
    verify_moment_origin,
)

FET = FederationEventType
KEY = generate_identity_keypair()
OTHER = generate_identity_keypair()
ORIGIN = derive_instance_id(KEY.public_key)


def _create(**over) -> dict:
    return {
        "moment_id": "m-1",
        "author_user_id": "u-1",
        "origin_instance_id": ORIGIN,
        "content": "hi ✨",
        "media_url": None,
        "media_type": None,
        "duration_ms": None,
        "parent_moment_id": None,
        "expires_at": "2026-10-05T00:00:00+00:00",
        "occurred_at": "2026-09-28T00:00:00+00:00",
        "hop_count": 1,
        **over,
    }


def _sign(event_type=FET.MOMENT_CREATED, payload=None, key=KEY) -> dict:
    return sign_moment_origin(
        seed=key.private_key,
        identity_pk=key.public_key,
        event_type=event_type,
        payload=payload if payload is not None else _create(),
    )


def test_suite_constants():
    assert MOMENT_ORIGIN_SIG_SUITE_ED25519 == "ed25519"
    assert MOMENT_ORIGIN_SIG_SUITE_ED25519 in SUPPORTED_MOMENT_ORIGIN_SIG_SUITES
    assert SIGNED_MOMENT_EVENT_TYPES == {FET.MOMENT_CREATED, FET.MOMENT_DELETED}


def test_sign_adds_fields_without_mutating_input():
    payload = _create()
    signed = _sign(payload=payload)
    assert "origin_sig" not in payload
    assert signed["origin_sig_suite"] == "ed25519"
    assert signed["origin_identity_pk"] == KEY.public_key.hex()
    assert verify_moment_origin(
        identity_pk=KEY.public_key, event_type=FET.MOMENT_CREATED, payload=signed
    )


def test_signing_bytes_are_domain_separated_and_hop_free():
    a = moment_origin_signing_bytes(FET.MOMENT_CREATED, _create(), sig_suite="x")
    b = moment_origin_signing_bytes(
        FET.MOMENT_CREATED, _create(hop_count=3), sig_suite="x"
    )
    assert a.startswith(b"moment-origin:v1:")
    assert a == b
    assert a != moment_origin_signing_bytes(
        FET.MOMENT_DELETED, _create(), sig_suite="x"
    )


def test_signing_bytes_reject_unsigned_event_type():
    with pytest.raises(ValueError):
        moment_origin_signing_bytes(FET.MOMENT_REACTED, _create(), sig_suite="x")


def test_relay_hop_bump_keeps_signature_valid():
    signed = {**_sign(), "hop_count": 3}
    assert verify_moment_origin(
        identity_pk=KEY.public_key, event_type=FET.MOMENT_CREATED, payload=signed
    )


@pytest.mark.parametrize(
    "field", ["content", "author_user_id", "origin_instance_id", "expires_at"]
)
def test_any_signed_field_change_breaks_the_signature(field):
    signed = {**_sign(), field: "tampered"}
    assert not verify_moment_origin(
        identity_pk=KEY.public_key, event_type=FET.MOMENT_CREATED, payload=signed
    )


def test_verify_refuses_wrong_key_bad_encoding_and_other_event_types():
    signed = _sign()
    assert not verify_moment_origin(
        identity_pk=OTHER.public_key, event_type=FET.MOMENT_CREATED, payload=signed
    )
    assert not verify_moment_origin(
        identity_pk=KEY.public_key, event_type=FET.MOMENT_DELETED, payload=signed
    )
    assert not verify_moment_origin(
        identity_pk=KEY.public_key,
        event_type=FET.MOMENT_CREATED,
        payload={**signed, "origin_sig": "a"},
    )
    assert not verify_moment_origin(
        identity_pk=KEY.public_key, event_type=FET.MOMENT_REACTED, payload=signed
    )


@pytest.mark.parametrize("suite", ["rot13", "", None])
def test_verify_raises_on_unknown_or_missing_suite(suite):
    signed = {**_sign(), "origin_sig_suite": suite}
    with pytest.raises(UnsupportedMomentOriginSuite):
        verify_moment_origin(
            identity_pk=KEY.public_key, event_type=FET.MOMENT_CREATED, payload=signed
        )


# ── check_relayed_moment_origin ──────────────────────────────────────────


def _check(payload, *, pinned=None, signs=True, origin=ORIGIN, et=FET.MOMENT_CREATED):
    return check_relayed_moment_origin(
        event_type=et,
        payload=payload,
        origin_instance_id=origin,
        pinned_pk=pinned,
        origin_signs=signs,
    )


def test_check_accepts_pinned_and_derived_keys():
    assert _check(_sign(), pinned=KEY.public_key).accepted
    assert _check(_sign()).accepted  # no row: shipped key derives to ORIGIN


def test_check_legacy_window_only_for_known_older_origin():
    legacy = _check(_create(), pinned=KEY.public_key, signs=False)
    assert legacy.accepted and legacy.legacy
    assert not _check(_create(), pinned=KEY.public_key, signs=True).accepted
    assert not _check(_create(), pinned=None, signs=False).accepted


@pytest.mark.parametrize(
    ("payload", "pinned", "origin"),
    [
        pytest.param(_sign(key=OTHER), KEY.public_key, ORIGIN, id="pinned mismatch"),
        pytest.param(
            {**_sign(), "origin_identity_pk": "zz"}, None, ORIGIN, id="bad hex"
        ),
        pytest.param(
            {**_sign(), "origin_identity_pk": "ab"}, None, ORIGIN, id="short key"
        ),
        pytest.param(_sign(key=OTHER), None, ORIGIN, id="does not derive"),
        pytest.param(
            {k: v for k, v in _sign().items() if k != "origin_identity_pk"},
            None,
            ORIGIN,
            id="no key at all",
        ),
        pytest.param(
            {**_sign(), "origin_sig_suite": "rot13"},
            KEY.public_key,
            ORIGIN,
            id="unknown suite",
        ),
        pytest.param(
            {**_sign(), "content": "x"}, KEY.public_key, ORIGIN, id="tampered"
        ),
    ],
)
def test_check_refusals(payload, pinned, origin):
    verdict = _check(payload, pinned=pinned, origin=origin)
    assert not verdict.accepted
    assert verdict.reason


def test_check_pinned_key_without_shipped_key_verifies():
    signed = {k: v for k, v in _sign().items() if k != "origin_identity_pk"}
    assert _check(signed, pinned=KEY.public_key).accepted


def test_check_refuses_event_types_without_origin_signature():
    assert not _check(_sign(), pinned=KEY.public_key, et=FET.MOMENT_REACTED).accepted
