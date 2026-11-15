"""The outer bytes of a binary relay media body (v_87)."""

from __future__ import annotations

import orjson
import pytest

from socialhome.domain import relay_media_frame as rf
from socialhome.domain.calendar import MAX_RSVPS_PER_EVENT, RSVP_RECORD_MAX_BYTES
from socialhome.domain.errors import SpaceItemTooLargeError
from socialhome.federation.media_framing import HTTPS_MEDIA_MAX_BODY_BYTES

TO = "a" * 32


VALID = b"\x01" * (64 * 1024 + rf.RELAY_MEDIA_SEAL_OVERHEAD_BYTES)


def test_round_trip_outer_body():
    body = rf.build_relay_media_body(TO, VALID)
    assert body.startswith(rf.RELAY_MEDIA_MAGIC)
    assert rf.is_relay_media_body(body)
    to, sealed = rf.parse_relay_media_body(body)
    assert to == TO
    assert bytes(sealed) == VALID


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


# ── Only valid sealed lengths (a bucket plus the fixed seal overhead) ────


@pytest.mark.parametrize("bucket", rf.RELAY_MEDIA_SIZE_BUCKETS)
def test_every_bucket_length_is_accepted(bucket):
    sealed = b"\x01" * (bucket + rf.RELAY_MEDIA_SEAL_OVERHEAD_BYTES)
    _to, got = rf.parse_relay_media_body(rf.build_relay_media_body(TO, sealed))
    assert len(got) == len(sealed)
    assert len(rf.build_relay_media_body(TO, sealed)) <= rf.RELAY_MEDIA_MAX_BODY_BYTES


@pytest.mark.parametrize("delta", [-1, 1, 100])
def test_a_sealed_length_off_the_buckets_is_refused(delta):
    sealed = b"\x01" * (64 * 1024 + rf.RELAY_MEDIA_SEAL_OVERHEAD_BYTES + delta)
    with pytest.raises(rf.InvalidRelayMediaBody, match="sealed length"):
        rf.parse_relay_media_body(rf.build_relay_media_body(TO, sealed))


# ── A space item is measured whole, as it travels ─────────────────────────


def test_an_item_is_measured_as_its_json_on_the_wire():
    """Every field counts, as orjson writes it: the separators between list
    entries, empty and falsy values, escapes and multi-byte characters."""
    item = {"attendees": [""] * 1000, "rrule": "", "n": 0, "s": "é\x00"}
    assert rf.space_item_bytes(item) == len(orjson.dumps(item))
    # 1000 empty ids are ~3 kB, not nothing.
    assert rf.space_item_bytes({"attendees": [""] * 1000}) > 3000


def test_an_item_within_its_limit_fits():
    item = {"x": "a" * 90}
    size = rf.space_item_bytes(item)
    rf.require_space_item_fits(item, limit=size)
    assert not rf.space_item_too_large(item, limit=size)


def test_an_item_over_its_limit_is_refused_with_the_limit():
    item = {"x": "a" * 90}
    limit = rf.space_item_bytes(item) - 1
    assert rf.space_item_too_large(item, limit=limit)
    with pytest.raises(SpaceItemTooLargeError) as refused:
        rf.require_space_item_fits(item, limit=limit)
    assert refused.value.code == "SPACE_ITEM_TOO_LARGE"
    assert refused.value.status == 413
    # The limit in kB (1000 bytes), the same unit the SPA shows.
    assert refused.value.params == {"max_kb": limit // 1000}


def test_an_item_stored_over_its_limit_may_be_saved_no_larger():
    """An item saved before the limit stays readable and editable: a save
    the same size or smaller passes, one that grows it does not."""
    held = {"x": "a" * 5000}
    limit = 1000
    assert not rf.space_item_too_large({"x": "b" * 5000}, limit=limit, was=held)
    assert not rf.space_item_too_large({"x": "a" * 10}, limit=limit, was=held)
    assert rf.space_item_too_large({"x": "a" * 5001}, limit=limit, was=held)
    rf.require_space_item_fits({"x": "a" * 4000}, limit=limit, was=held)


def test_the_item_limits_are_derived_from_the_bound():
    bound = rf.RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES
    assert rf.SPACE_PAGE_ITEM_MAX_BYTES == bound - rf.SPACE_PAGE_RESERVE_BYTES
    # Calendar: RSVPs are added by others after the event is saved — held
    # out under their own hard cap, per entry and in count.
    assert rf.SPACE_CALENDAR_RESERVE_BYTES >= MAX_RSVPS_PER_EVENT * (
        RSVP_RECORD_MAX_BYTES + 1
    )
    assert rf.SPACE_CALENDAR_ITEM_MAX_BYTES == bound - rf.SPACE_CALENDAR_RESERVE_BYTES
    # Config: the event carries the item twice (flat + ``space_meta``).
    assert rf.SPACE_CONFIG_ITEM_MAX_BYTES * 2 + rf.SPACE_CONFIG_RESERVE_BYTES <= bound
