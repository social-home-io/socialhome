"""Sealing who shared a public highlight to the viewer's ephemeral key."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from socialhome.crypto import b64url_encode
from socialhome.services.highlight_public_profile import (
    PROFILE_SEAL_SUITE_P256,
    UnsupportedProfileSealSuite,
    open_profile,
    parse_viewer_key,
    seal_profile,
)

PROFILE = {"display_name": "Anna Berger", "bio": "Bread.", "picture": None}


def _viewer():
    sk = ec.generate_private_key(ec.SECP256R1())
    raw = sk.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return sk, b64url_encode(raw)


def test_round_trip_to_the_viewer():
    sk, vk = _viewer()
    header, ct = seal_profile(parse_viewer_key(vk), highlight_id="h1", profile=PROFILE)
    assert header["seal_suite"] == PROFILE_SEAL_SUITE_P256
    assert b"Anna" not in ct
    assert open_profile(sk, header, ct, highlight_id="h1") == PROFILE


def test_bound_to_its_highlight_and_its_viewer():
    sk, vk = _viewer()
    header, ct = seal_profile(parse_viewer_key(vk), highlight_id="h1", profile=PROFILE)
    with pytest.raises(InvalidTag):
        open_profile(sk, header, ct, highlight_id="h2")
    other, _ = _viewer()
    with pytest.raises(InvalidTag):
        open_profile(other, header, ct, highlight_id="h1")


def test_an_unknown_suite_is_refused_never_defaulted():
    sk, vk = _viewer()
    header, ct = seal_profile(parse_viewer_key(vk), highlight_id="h1", profile=PROFILE)
    with pytest.raises(UnsupportedProfileSealSuite):
        open_profile(sk, {**header, "seal_suite": "none"}, ct, highlight_id="h1")


@pytest.mark.parametrize(
    "value",
    [
        None,
        42,
        "",
        "not base64 !!",
        b64url_encode(b"\x04" + b"\x00" * 10),
        b64url_encode(b"\x02" + b"\x01" * 64),  # not an uncompressed point
        b64url_encode(b"\x04" + b"\x01" * 64),  # not on the curve
        "A" * 200,
    ],
)
def test_a_malformed_viewer_key_is_none(value):
    assert parse_viewer_key(value) is None
