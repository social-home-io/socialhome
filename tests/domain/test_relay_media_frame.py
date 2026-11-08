"""The outer bytes of a binary relay media body (v_87)."""

from __future__ import annotations

import pytest

from socialhome.domain import relay_media_frame as rf
from socialhome.federation.media_framing import HTTPS_MEDIA_MAX_BODY_BYTES

TO = "a" * 32


def test_round_trip_outer_body():
    body = rf.build_relay_media_body(TO, b"sealed-bytes")
    assert body.startswith(rf.RELAY_MEDIA_MAGIC)
    assert rf.is_relay_media_body(body)
    to, sealed = rf.parse_relay_media_body(body)
    assert to == TO
    assert bytes(sealed) == b"sealed-bytes"


def test_json_is_never_a_relay_media_body():
    assert not rf.is_relay_media_body(b'{"to_instance":"x"}')


def test_magic_differs_from_inbox_media_and_ws_binary1():
    # The household tells a relay frame from a ws_binary1 frame (version 1)
    # by its first byte, and from the inbox media magic by the fourth.
    assert rf.RELAY_MEDIA_MAGIC[0] == 0
    assert rf.RELAY_MEDIA_MAGIC != b"\x00SHM"


@pytest.mark.parametrize(
    "body",
    [
        b"\x00SHM\x01" + b"a" * 32 + b"x",  # wrong magic
        rf.RELAY_MEDIA_MAGIC + b"\x02" + b"a" * 32 + b"x",  # unknown version
        rf.RELAY_MEDIA_MAGIC + b"\x01" + b"A" * 32 + b"x",  # not an instance id
        rf.RELAY_MEDIA_MAGIC + b"\x01" + b"a\n" + b"a" * 30 + b"x",  # log forgery
        rf.RELAY_MEDIA_MAGIC + b"\x01" + b"a" * 32,  # empty seal
        rf.RELAY_MEDIA_MAGIC + b"\x01" + b"a" * 10,  # short
    ],
)
def test_malformed_bodies_are_refused(body):
    with pytest.raises(rf.InvalidRelayMediaBody):
        rf.parse_relay_media_body(body)


def test_oversize_body_is_refused():
    body = rf.build_relay_media_body(TO, b"\x00" * rf.RELAY_MEDIA_MAX_BODY_BYTES)
    with pytest.raises(rf.InvalidRelayMediaBody, match="too large"):
        rf.parse_relay_media_body(body)


def test_build_refuses_a_bad_recipient():
    with pytest.raises(rf.InvalidRelayMediaBody):
        rf.build_relay_media_body("not-an-id", b"x")


def test_cap_matches_the_inbox_media_cap():
    assert rf.RELAY_MEDIA_MAX_BODY_BYTES == HTTPS_MEDIA_MAX_BODY_BYTES
