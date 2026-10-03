"""Tests for the trusted-mode member-publish wire codec (v_49)."""

from __future__ import annotations

import json
import os

import pytest

from socialhome.crypto import ed25519_public_key
from socialhome.domain.gfs_member_publish import (
    MEMBER_PUBLISH_ACTION,
    MEMBER_PUBLISH_FRAME_KEYS,
    MEMBER_PUBLISH_MAX_PAYLOAD_CHARS,
    MEMBER_PUBLISH_REQUEST_KEYS,
    PLAINTEXT_CERT_KEYS,
    SPACE_ITEM_EVENT_TYPE,
    InvalidMemberPublish,
    MemberPublishRequest,
    SpaceItemFrame,
)
from socialhome.writer_cert import bind_writer_users, sign_writer_cert

SPACE_SEED = os.urandom(32)
AUTHOR_PK = ed25519_public_key(os.urandom(32))


def _cert(epoch: int = 3):
    return sign_writer_cert(
        space_seed=SPACE_SEED,
        space_id="sp-1",
        epoch=epoch,
        instance_pk=AUTHOR_PK,
        scope="write",
    )


def _wire(**over) -> dict:
    body = {
        "instance_id": "a" * 32,
        "gfs_instance_id": "gfs-node-a",
        "ts": "2026-10-03T10:00:00+00:00",
        "signature": "c2ln",
        "target": "sp-1",
        "event_type": SPACE_ITEM_EVENT_TYPE,
        "epoch": 3,
        "writer_cert": _cert().to_wire(),
        "payload": "bm9uY2U:Y2lwaGVy",
    }
    body.update(over)
    return body


def test_round_trip_keeps_every_field():
    req = MemberPublishRequest.from_wire(_wire())
    assert req.to_wire() == _wire()
    assert set(req.to_wire()) == MEMBER_PUBLISH_REQUEST_KEYS


def test_event_type_is_always_the_generic_space_item():
    assert SPACE_ITEM_EVENT_TYPE == "space_item"
    with pytest.raises(InvalidMemberPublish):
        MemberPublishRequest.from_wire(_wire(event_type="space_post_created"))


@pytest.mark.parametrize(
    "field",
    sorted(MEMBER_PUBLISH_REQUEST_KEYS),
)
def test_every_field_is_required(field):
    body = _wire()
    del body[field]
    with pytest.raises(InvalidMemberPublish):
        MemberPublishRequest.from_wire(body)


@pytest.mark.parametrize(
    "extra",
    ["from_instance", "content", "item_type", "type", "author_pk"],
)
def test_an_unknown_field_is_refused(extra):
    """A body can't smuggle a plaintext field (content, the real item type, a
    sender id) alongside the ciphertext — the key set is exact."""
    with pytest.raises(InvalidMemberPublish):
        MemberPublishRequest.from_wire(_wire(**{extra: "x"}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payload", {"content": "hello"}),
        ("payload", ""),
        ("payload", "x" * (MEMBER_PUBLISH_MAX_PAYLOAD_CHARS + 1)),
        ("epoch", "3"),
        ("epoch", True),
        ("epoch", -1),
        ("target", ""),
        ("target", "s" * 129),
        ("target", ["sp-1"]),
        ("instance_id", ""),
        ("instance_id", "x" * 129),
        ("gfs_instance_id", ""),
        ("gfs_instance_id", "g" * 129),
        ("ts", 12345),
        ("signature", ""),
        ("writer_cert", "not-a-dict"),
        ("writer_cert", {"cert_suite": "ed25519"}),
    ],
)
def test_malformed_fields_are_refused(field, value):
    with pytest.raises(InvalidMemberPublish):
        MemberPublishRequest.from_wire(_wire(**{field: value}))


def test_non_object_body_is_refused():
    with pytest.raises(InvalidMemberPublish):
        MemberPublishRequest.from_wire(["not", "an", "object"])


def test_signing_payload_binds_every_field_except_the_signature():
    req = MemberPublishRequest.from_wire(_wire())
    signed = req.signing_payload()
    assert signed["action"] == MEMBER_PUBLISH_ACTION
    assert "signature" not in signed
    assert set(signed) == (MEMBER_PUBLISH_REQUEST_KEYS - {"signature"}) | {"action"}
    assert signed["writer_cert"] == req.writer_cert.to_wire()


def test_signing_bytes_are_the_canonical_json_of_the_signing_payload():
    req = MemberPublishRequest.from_wire(_wire())
    assert req.signing_bytes() == json.dumps(
        req.signing_payload(), separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def test_action_is_domain_separated_from_other_signed_gfs_requests():
    assert MEMBER_PUBLISH_ACTION not in ("subscribe", "unsubscribe", "unpublish")
    assert MEMBER_PUBLISH_ACTION.endswith(":v1")


def test_fan_out_frame_carries_routing_fields_and_no_publisher_id():
    req = MemberPublishRequest.from_wire(_wire())
    frame = req.fan_out_frame()
    assert set(frame) == MEMBER_PUBLISH_FRAME_KEYS
    assert frame == {
        "space_id": "sp-1",
        "event_type": SPACE_ITEM_EVENT_TYPE,
        "epoch": 3,
        "writer_cert": req.writer_cert.to_wire(),
        "payload": "bm9uY2U:Y2lwaGVy",
    }
    assert "from_instance" not in frame
    assert "instance_id" not in frame
    assert "a" * 32 not in json.dumps(frame)


def test_space_item_frame_parses_a_fan_out_frame_and_ignores_the_ws_type():
    req = MemberPublishRequest.from_wire(_wire())
    frame = SpaceItemFrame.from_wire({"type": "relay", **req.fan_out_frame()})
    assert frame.space_id == "sp-1"
    assert frame.epoch == 3
    assert frame.writer_cert == req.writer_cert
    assert frame.payload == "bm9uY2U:Y2lwaGVy"
    assert frame.to_wire() == req.fan_out_frame()


@pytest.mark.parametrize(
    "over",
    [
        {"event_type": "space_post_public"},
        {"payload": 7},
        {"epoch": "1"},
        {"space_id": ""},
        {"writer_cert": None},
    ],
)
def test_space_item_frame_refuses_a_malformed_frame(over):
    req = MemberPublishRequest.from_wire(_wire())
    with pytest.raises(InvalidMemberPublish):
        SpaceItemFrame.from_wire({**req.fan_out_frame(), **over})


def test_space_item_frame_refuses_a_non_object():
    with pytest.raises(InvalidMemberPublish):
        SpaceItemFrame.from_wire("nope")


def test_the_event_type_property_is_the_generic_type():
    assert MemberPublishRequest.from_wire(_wire()).event_type == SPACE_ITEM_EVENT_TYPE


def test_the_signed_bytes_name_the_audience_gfs():
    """M4: a request signed for one connection server can't be replayed to
    another — the GFS's pinned id is inside the signature."""
    req = MemberPublishRequest.from_wire(_wire())
    assert req.signing_payload()["gfs_instance_id"] == "gfs-node-a"
    other = MemberPublishRequest.from_wire(_wire(gfs_instance_id="gfs-node-b"))
    assert other.signing_bytes() != req.signing_bytes()


# ─── The v2 user binding never travels in plaintext ─────────────────────


def _bound_cert():
    return bind_writer_users(_cert(), space_seed=SPACE_SEED, user_ids=["alice", "bob"])


@pytest.mark.security
def test_serialised_requests_and_frames_carry_only_the_v1_cert():
    """U1: a request built from a bound cert must not hand the connection
    server the household's writer user ids — not in the request, not in the
    fan-out frame, not in the signed bytes."""
    req = MemberPublishRequest(
        instance_id="i",
        gfs_instance_id="g",
        ts="t",
        signature="s",
        target="sp-1",
        epoch=3,
        writer_cert=_bound_cert(),
        payload="ct",
    )
    for wire in (
        req.to_wire()["writer_cert"],
        req.fan_out_frame()["writer_cert"],
        req.signing_payload()["writer_cert"],
    ):
        assert set(wire) == PLAINTEXT_CERT_KEYS
    blob = (
        json.dumps([req.to_wire(), req.fan_out_frame()]) + req.signing_bytes().decode()
    )
    for leak in ("alice", "bob", "writer_user_ids", "users_sig"):
        assert leak not in blob


@pytest.mark.security
def test_a_request_carrying_the_binding_is_refused():
    with pytest.raises(InvalidMemberPublish):
        MemberPublishRequest.from_wire(_wire(writer_cert=_bound_cert().to_wire()))


def test_a_frame_carrying_the_binding_is_refused():
    req = MemberPublishRequest.from_wire(_wire())
    with pytest.raises(InvalidMemberPublish):
        SpaceItemFrame.from_wire(
            {**req.fan_out_frame(), "writer_cert": _bound_cert().to_wire()}
        )
