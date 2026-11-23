"""Unit tests for :mod:`socialhome.moment_origin`."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.federation import FederationEventType
from socialhome.moment_origin import (
    DELETE_CERT_FIELDS,
    MAX_DELETE_CERT_FIELD_CHARS,
    MOMENT_ORIGIN_SIG_SUITE_ED25519,
    NO_RELAY_FIELD,
    REACH_FIELD,
    SIGNED_MOMENT_EVENT_TYPES,
    SUPPORTED_MOMENT_ORIGIN_SIG_SUITES,
    UnsupportedMomentOriginSuite,
    check_relayed_moment_origin,
    delete_cert_of,
    delete_cert_refusal,
    moment_hop_of,
    moment_origin_signing_bytes,
    moment_reach_of,
    reach_marks_of,
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


# ─── v_38 ``no_relay`` ───────────────────────────────────────────────────


def test_no_relay_is_signed_and_cannot_be_stripped():
    signed = sign_moment_origin(
        seed=KEY.private_key,
        identity_pk=KEY.public_key,
        event_type=FET.MOMENT_CREATED,
        payload=_create(**{NO_RELAY_FIELD: True}),
    )
    assert verify_moment_origin(
        identity_pk=KEY.public_key, event_type=FET.MOMENT_CREATED, payload=signed
    )
    stripped = {k: v for k, v in signed.items() if k != NO_RELAY_FIELD}
    assert not verify_moment_origin(
        identity_pk=KEY.public_key, event_type=FET.MOMENT_CREATED, payload=stripped
    )


def test_a_moment_without_no_relay_signs_the_v1_bytes_unchanged():
    """Older receivers verify the v1 bytes — a moment that isn't
    ``no_relay`` must still sign exactly those."""
    bytes_ = moment_origin_signing_bytes(
        FET.MOMENT_CREATED, _create(), sig_suite=MOMENT_ORIGIN_SIG_SUITE_ED25519
    )
    assert bytes_.startswith(b"moment-origin:v1:")
    assert b"no_relay" not in bytes_
    marked = moment_origin_signing_bytes(
        FET.MOMENT_CREATED,
        _create(**{NO_RELAY_FIELD: True}),
        sig_suite=MOMENT_ORIGIN_SIG_SUITE_ED25519,
    )
    assert marked.startswith(b"moment-origin:v2:")
    assert b'"no_relay":true' in marked


def test_a_forged_no_relay_mark_does_not_verify():
    signed = sign_moment_origin(
        seed=KEY.private_key,
        identity_pk=KEY.public_key,
        event_type=FET.MOMENT_CREATED,
        payload=_create(),
    )
    assert not verify_moment_origin(
        identity_pk=KEY.public_key,
        event_type=FET.MOMENT_CREATED,
        payload={**signed, NO_RELAY_FIELD: True},
    )


# ── v_96 ``reach`` ──────────────────────────────────────────────────────


def test_reach_field_name():
    assert REACH_FIELD == "reach"


@pytest.mark.parametrize("et", [FET.MOMENT_CREATED, FET.MOMENT_DELETED])
@pytest.mark.parametrize("reach", [2, 4, 5])
def test_reach_signs_the_v3_bytes(et, reach):
    """``reach`` sits under the signature in its own ``v3`` domain: the v1
    fields plus ``reach``."""
    payload = _create(**{REACH_FIELD: reach})
    bytes_ = moment_origin_signing_bytes(et, payload, sig_suite="ed25519")
    assert bytes_.startswith(b"moment-origin:v3:")
    assert f'"reach":{reach}'.encode() in bytes_
    assert b"no_relay" not in bytes_
    signed = _sign(et, payload)
    assert verify_moment_origin(
        identity_pk=KEY.public_key, event_type=et, payload=signed
    )


def test_no_reach_keeps_the_v1_bytes_byte_for_byte():
    plain = moment_origin_signing_bytes(
        FET.MOMENT_CREATED, _create(), sig_suite="ed25519"
    )
    assert plain.startswith(b"moment-origin:v1:")
    assert b"reach" not in plain


@pytest.mark.parametrize("et", [FET.MOMENT_CREATED, FET.MOMENT_DELETED])
@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(
            lambda p: {k: v for k, v in p.items() if k != REACH_FIELD}, id="strip"
        ),
        pytest.param(lambda p: {**p, REACH_FIELD: 5}, id="raise"),
        pytest.param(lambda p: {**p, REACH_FIELD: 3}, id="lower to 3"),
        pytest.param(lambda p: {**p, NO_RELAY_FIELD: True}, id="splice no_relay"),
    ],
)
def test_a_relay_cannot_strip_or_change_reach(et, tamper):
    signed = _sign(et, _create(**{REACH_FIELD: 2}))
    assert not verify_moment_origin(
        identity_pk=KEY.public_key, event_type=et, payload=tamper(signed)
    )


@pytest.mark.parametrize("et", [FET.MOMENT_CREATED, FET.MOMENT_DELETED])
def test_a_relay_cannot_add_reach(et):
    signed = _sign(et, _create())
    assert not verify_moment_origin(
        identity_pk=KEY.public_key,
        event_type=et,
        payload={**signed, REACH_FIELD: 5},
    )


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        pytest.param({}, 3, id="none = 3"),
        pytest.param({NO_RELAY_FIELD: True}, 1, id="no_relay = 1"),
        pytest.param({REACH_FIELD: 2}, 2, id="2"),
        pytest.param({REACH_FIELD: 4}, 4, id="4"),
        pytest.param({REACH_FIELD: 5}, 5, id="5"),
        pytest.param({REACH_FIELD: 3}, None, id="3 is never sent"),
        pytest.param({REACH_FIELD: 1}, None, id="1 is no_relay"),
        pytest.param({REACH_FIELD: 0}, None, id="0"),
        pytest.param({REACH_FIELD: 6}, None, id="6"),
        pytest.param({REACH_FIELD: -2}, None, id="negative"),
        pytest.param({REACH_FIELD: True}, None, id="bool"),
        pytest.param({REACH_FIELD: 2.0}, None, id="float"),
        pytest.param({REACH_FIELD: "2"}, None, id="string"),
        pytest.param({REACH_FIELD: None}, None, id="null"),
        pytest.param({REACH_FIELD: 2, NO_RELAY_FIELD: True}, None, id="both"),
    ],
)
def test_moment_reach_of(extra, expected):
    assert moment_reach_of(_create(**extra)) == expected


@pytest.mark.parametrize(
    ("reach", "marks"),
    [
        (1, {NO_RELAY_FIELD: True}),
        (2, {REACH_FIELD: 2}),
        (3, {}),
        (4, {REACH_FIELD: 4}),
        (5, {REACH_FIELD: 5}),
        (0, {NO_RELAY_FIELD: True}),
        (9, {REACH_FIELD: 5}),
    ],
)
def test_reach_marks_of_is_the_inverse_of_moment_reach_of(reach, marks):
    assert reach_marks_of(reach) == marks
    assert moment_reach_of(_create(**marks)) == max(1, min(reach, 5))


@pytest.mark.parametrize(
    ("hop", "from_origin", "expected"),
    [
        (1, True, 1),
        (3, True, 3),
        (1, False, 2),
        (2, False, 2),
        (4, False, 4),
        (0, True, None),
        (-1, False, None),
        (None, True, None),
        ("x", True, None),
        ("2", False, 2),
    ],
)
def test_moment_hop_of_parses_and_clamps(hop, from_origin, expected):
    """``hop_count`` is outside the signature: a copy not sent by its origin
    is at least 2 hops away (the relay clamp)."""
    payload = {"hop_count": hop}
    got = moment_hop_of(
        payload, from_instance=ORIGIN if from_origin else "relay", origin=ORIGIN
    )
    assert got == expected


def test_the_legacy_unsigned_window_refuses_reach():
    """An unsigned relay is taken only from an origin predating signing —
    which predates reach too: a ``reach`` on it is a relay's invention."""
    assert _check(_create(), pinned=KEY.public_key, signs=False).accepted
    verdict = _check(_create(**{REACH_FIELD: 5}), pinned=KEY.public_key, signs=False)
    assert not verdict.accepted
    assert "reach" in verdict.reason


# ── Delete certificates (v_66) ──────────────────────────────────────────


def _cert(key=KEY, **over) -> dict:
    payload = {
        "moment_id": "m-1",
        "author_user_id": "u-1",
        "origin_instance_id": derive_instance_id(key.public_key),
        "occurred_at": "2026-10-25T12:00:00+00:00",
        "hop_count": 2,
        **over,
    }
    return delete_cert_of(_sign(FET.MOMENT_DELETED, payload, key=key))


def test_a_delete_certificate_is_the_mesh_deletes_signature():
    """The certificate is the MOMENT_DELETED's signed fields + its origin
    signature — no hop count, no content — and checks out on its own."""
    cert = _cert()
    assert set(cert) == DELETE_CERT_FIELDS
    assert "hop_count" not in cert
    assert delete_cert_refusal(cert, identity_pk_hex=KEY.public_key.hex()) is None
    # Hex case of the pinned key doesn't matter.
    assert (
        delete_cert_refusal(cert, identity_pk_hex=KEY.public_key.hex().upper()) is None
    )


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (lambda c: {**c, "moment_id": "m-2"}, "origin signature does not verify"),
        (lambda c: {**c, "occurred_at": "2026-10-26T00:00:00+00:00"}, "verify"),
        (lambda c: {**c, "origin_sig_suite": "ed25519+mldsa65"}, "unsupported"),
        (lambda c: {**c, "content": "x"}, "not a delete certificate"),
        (lambda c: {k: v for k, v in c.items() if k != "origin_sig"}, "not a"),
        (lambda c: {**c, "author_user_id": ""}, "malformed field"),
        (
            lambda c: {**c, "moment_id": "m" * (MAX_DELETE_CERT_FIELD_CHARS + 1)},
            "malformed field",
        ),
        (lambda c: {**c, "origin_instance_id": "someone-else"}, "derive"),
        (lambda c: {**c, "origin_identity_pk": "zz" * 32}, "another household"),
    ],
)
def test_a_tampered_delete_certificate_is_refused(mutate, why):
    refusal = delete_cert_refusal(mutate(_cert()), identity_pk_hex=KEY.public_key.hex())
    assert refusal is not None and why in refusal


def test_a_certificate_of_another_household_is_refused():
    """Genuine, but signed by a household other than the one pinned."""
    cert = _cert(key=OTHER)
    assert delete_cert_refusal(cert, identity_pk_hex=OTHER.public_key.hex()) is None
    assert delete_cert_refusal(cert, identity_pk_hex=KEY.public_key.hex()) == (
        "signed by another household"
    )
    assert delete_cert_refusal(cert, identity_pk_hex="") is not None


def test_a_create_signature_never_passes_as_a_delete_certificate():
    """The event type is inside the signed bytes."""
    signed = _sign(FET.MOMENT_CREATED, _create())
    forged = {k: str(signed.get(k) or "") for k in DELETE_CERT_FIELDS}
    assert delete_cert_refusal(forged, identity_pk_hex=KEY.public_key.hex()) == (
        "origin signature does not verify"
    )


def test_a_malformed_key_or_not_a_dict_is_refused_not_raised():
    assert delete_cert_refusal("cert", identity_pk_hex="aa") is not None
    cert = {**_cert(), "origin_identity_pk": "abc"}
    assert delete_cert_refusal(cert, identity_pk_hex="abc") == (
        "malformed origin_identity_pk"
    )
