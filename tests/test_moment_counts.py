"""Signed reaction totals (v_69, :mod:`socialhome.moment_counts`)."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.moment_counts import (
    COUNTS_FIELDS,
    COUNTS_SIG_SUITE_ED25519,
    SUPPORTED_COUNTS_SIG_SUITES,
    UnsupportedCountsSigSuite,
    counts_origin_refusal,
    counts_shape_refusal,
    counts_signing_bytes,
    sign_counts,
    verify_counts,
)

KEY = generate_identity_keypair()
ORIGIN = derive_instance_id(KEY.public_key)


def _signed(**over) -> dict:
    return sign_counts(
        seed=KEY.private_key,
        identity_pk=KEY.public_key,
        moment_id=over.pop("moment_id", "m-1"),
        author_user_id="u-1",
        origin_instance_id=over.pop("origin", ORIGIN),
        rev=over.pop("rev", 1_761_480_000_000),
        counts=over.pop("counts", {"🔥": 2}),
        total=over.pop("total", 3),
        no_relay=over.pop("no_relay", False),
    )


def test_suite_contract():
    assert COUNTS_SIG_SUITE_ED25519 == "ed25519"
    assert frozenset({"ed25519"}) == SUPPORTED_COUNTS_SIG_SUITES


def test_signed_payload_has_exactly_the_fields_and_verifies():
    p = _signed()
    assert set(p) == COUNTS_FIELDS
    assert p["hop_count"] == 1
    assert verify_counts(identity_pk=KEY.public_key, payload=p)
    assert counts_shape_refusal(p) is None
    assert counts_origin_refusal(p, pinned_pk=None) is None
    assert counts_origin_refusal(p, pinned_pk=KEY.public_key) is None


def test_signing_bytes_are_domain_separated_and_skip_hop_and_sig():
    p = _signed()
    raw = counts_signing_bytes(p)
    assert raw.startswith(b"moment-counts:v1:")
    assert b"hop_count" not in raw and b'counts_sig"' not in raw
    assert raw == counts_signing_bytes({**p, "hop_count": 3, "counts_sig": "x"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("counts", {"🔥": 3}),
        ("total", 99),
        ("rev", 1),
        ("moment_id", "m-2"),
        ("author_user_id", "u-2"),
    ],
)
def test_any_signed_field_change_breaks_the_signature(field, value):
    p = {**_signed(), field: value}
    assert not verify_counts(identity_pk=KEY.public_key, payload=p)


def test_no_relay_is_signed_and_cannot_be_stripped():
    p = _signed(no_relay=True)
    assert p["no_relay"] is True
    assert verify_counts(identity_pk=KEY.public_key, payload=p)
    stripped = {k: v for k, v in p.items() if k != "no_relay"}
    assert not verify_counts(identity_pk=KEY.public_key, payload=stripped)
    assert counts_shape_refusal(p) is None
    assert counts_shape_refusal({**p, "no_relay": False}) == "malformed no_relay"


def test_an_unknown_suite_raises_and_is_refused_never_defaulted():
    p = {**_signed(), "counts_sig_suite": "ed448"}
    with pytest.raises(UnsupportedCountsSigSuite):
        verify_counts(identity_pk=KEY.public_key, payload=p)
    assert counts_origin_refusal(p, pinned_pk=None) == "unsupported counts_sig_suite"


def test_origin_refusals():
    other = generate_identity_keypair()
    p = _signed()
    assert counts_origin_refusal(p, pinned_pk=other.public_key) == (
        "origin_identity_pk differs from the pinned key"
    )
    assert counts_origin_refusal({**p, "origin_identity_pk": "zz"}, pinned_pk=None) == (
        "malformed origin_identity_pk"
    )
    assert counts_origin_refusal({**p, "origin_identity_pk": "00"}, pinned_pk=None) == (
        "origin_identity_pk is not 32 bytes"
    )
    assert counts_origin_refusal({**p, "origin_instance_id": "x"}, pinned_pk=None) == (
        "origin_identity_pk does not derive to the origin"
    )
    assert counts_origin_refusal({**p, "counts_sig": "!!"}, pinned_pk=None) == (
        "totals signature does not verify"
    )


@pytest.mark.parametrize(
    "payload,reason",
    [
        ("nope", "not an object"),
        ({"moment_id": "m"}, "unexpected or missing fields"),
        ({**_signed(), "extra": 1}, "unexpected or missing fields"),
        ({**_signed(), "moment_id": ""}, "malformed moment_id"),
        ({**_signed(), "author_user_id": "x" * 300}, "malformed author_user_id"),
        ({**_signed(), "rev": True}, "malformed rev"),
        ({**_signed(), "hop_count": 0}, "malformed hop_count"),
        ({**_signed(), "counts": {"🔥": 5}, "total": 2}, "malformed counts"),
    ],
)
def test_shape_refusals(payload, reason):
    assert counts_shape_refusal(payload) == reason
