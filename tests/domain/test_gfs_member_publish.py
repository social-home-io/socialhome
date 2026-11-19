"""Tests for the member-publish wire codec (anonymous, the one mode since v_90)."""

from __future__ import annotations

import json
import os

import pytest

import socialhome.domain.gfs_member_publish as codec
from socialhome.domain.gfs_member_publish import (
    MEMBER_PUBLISH_ANON_FRAME_KEYS,
    MEMBER_PUBLISH_ANON_PREFIX,
    MEMBER_PUBLISH_ANON_REQUEST_KEYS,
    MEMBER_PUBLISH_ANON_ROUTE,
    SPACE_ITEM_EVENT_TYPE,
    InvalidMemberPublish,
    MemberPublishAnonRequest,
    SpaceItemFrame,
    owner_epoch_notice_signing_payload,
    parse_writer_key_cert,
)
from socialhome.writer_key import sign_writer_key_cert

SPACE_SEED = os.urandom(32)


def _anon_wire(**over) -> dict:
    body = {
        "gfs_instance_id": "gfs-node-a",
        "ts": "2026-10-03T10:00:00+00:00",
        "nonce": "bm9uY2Vub25jZW5vbmNl",
        "target": "sp-1",
        "event_type": SPACE_ITEM_EVENT_TYPE,
        "epoch": 3,
        "payload": "bm9uY2U:Y2lwaGVy",
        "writer_sig": "c2ln",
        "writer_sig_suite": "ed25519",
    }
    body.update(over)
    return body


def test_there_is_one_publish_mode_and_no_identified_codec():
    # v_90 — no publish-mode setting, no identified request, no route for it.
    for gone in (
        "GFS_PUBLISH_MODES",
        "GFS_PUBLISH_MODE_STRICT",
        "GFS_PUBLISH_MODE_TRUSTED",
        "MemberPublishRequest",
        "MEMBER_PUBLISH_ROUTE",
        "MEMBER_PUBLISH_ACTION",
    ):
        assert not hasattr(codec, gone), gone
    assert MEMBER_PUBLISH_ANON_ROUTE == "/gfs/member-publish-anon"


def test_anon_round_trip():
    req = MemberPublishAnonRequest.from_wire(_anon_wire())
    assert req.to_wire() == _anon_wire()
    assert set(req.to_wire()) == MEMBER_PUBLISH_ANON_REQUEST_KEYS
    assert req.event_type == SPACE_ITEM_EVENT_TYPE


def test_anon_request_names_no_household():
    # Nothing identifying on the wire.
    for forbidden in ("instance_id", "signature", "writer_cert", "from_instance"):
        assert forbidden not in MEMBER_PUBLISH_ANON_REQUEST_KEYS
        with pytest.raises(InvalidMemberPublish):
            MemberPublishAnonRequest.from_wire(_anon_wire(**{forbidden: "x"}))


@pytest.mark.parametrize("field", sorted(MEMBER_PUBLISH_ANON_REQUEST_KEYS))
def test_anon_every_field_is_required(field):
    body = _anon_wire()
    del body[field]
    with pytest.raises(InvalidMemberPublish):
        MemberPublishAnonRequest.from_wire(body)


@pytest.mark.parametrize(
    "over",
    [
        {"event_type": "post"},
        {"nonce": "short"},
        {"nonce": "x" * 65},
        {"nonce": 5},
        {"epoch": -1},
        {"epoch": True},
        {"payload": ""},
        {"payload": "x" * (codec.MEMBER_PUBLISH_MAX_PAYLOAD_CHARS + 1)},
        {"writer_sig": ""},
        {"target": "x" * 129},
    ],
)
def test_anon_refuses_malformed(over):
    with pytest.raises(InvalidMemberPublish):
        MemberPublishAnonRequest.from_wire(_anon_wire(**over))
    with pytest.raises(InvalidMemberPublish):
        MemberPublishAnonRequest.from_wire([])


def test_anon_signing_bytes_cover_everything_but_the_sig():
    req = MemberPublishAnonRequest.from_wire(_anon_wire())
    raw = req.signing_bytes()
    assert raw.startswith(MEMBER_PUBLISH_ANON_PREFIX)
    body = json.loads(raw[len(MEMBER_PUBLISH_ANON_PREFIX) :])
    assert set(body) == MEMBER_PUBLISH_ANON_REQUEST_KEYS - {"writer_sig"}
    other = MemberPublishAnonRequest.from_wire(_anon_wire(epoch=4))
    assert other.signing_bytes() != raw


def test_anon_fan_out_frame_has_no_cert():
    req = MemberPublishAnonRequest.from_wire(_anon_wire())
    frame = req.fan_out_frame()
    assert set(frame) == MEMBER_PUBLISH_ANON_FRAME_KEYS
    parsed = SpaceItemFrame.from_wire({"type": "relay", **frame})
    assert not hasattr(parsed, "writer_cert")
    assert parsed.to_wire() == frame


def test_a_frame_carrying_a_cert_is_read_without_it():
    # An older GFS may still add one: receivers take the cert from inside
    # the ciphertext only, so an outer one is ignored, never trusted.
    frame = MemberPublishAnonRequest.from_wire(_anon_wire()).fan_out_frame()
    parsed = SpaceItemFrame.from_wire({**frame, "writer_cert": {"cert_suite": "x"}})
    assert parsed.to_wire() == frame


@pytest.mark.parametrize(
    "over", [{"event_type": "post"}, {"epoch": "3"}, {"payload": ""}, {"space_id": ""}]
)
def test_space_item_frame_refuses_a_malformed_frame(over):
    frame = MemberPublishAnonRequest.from_wire(_anon_wire()).fan_out_frame()
    with pytest.raises(InvalidMemberPublish):
        SpaceItemFrame.from_wire({**frame, **over})
    with pytest.raises(InvalidMemberPublish):
        SpaceItemFrame.from_wire("not an object")


def test_owner_notice_payload_signs_the_writer_key_cert_only_when_present():
    base = owner_epoch_notice_signing_payload(
        owning_instance="o", gfs_instance_id="g", space_id="s", epoch=1, ts="t"
    )
    assert "writer_key_cert" not in base
    assert "publish_mode" not in base
    full = owner_epoch_notice_signing_payload(
        owning_instance="o",
        gfs_instance_id="g",
        space_id="s",
        epoch=1,
        ts="t",
        writer_key_cert={"k": 1},
    )
    assert full["writer_key_cert"] == {"k": 1}
    assert "publish_mode" not in full


def test_the_owner_notice_takes_no_publish_mode():
    with pytest.raises(TypeError):
        owner_epoch_notice_signing_payload(  # type: ignore[call-arg]
            owning_instance="o",
            gfs_instance_id="g",
            space_id="s",
            epoch=1,
            ts="t",
            publish_mode="strict",
        )


def test_parse_writer_key_cert():
    cert = sign_writer_key_cert(
        space_seed=SPACE_SEED, space_id="sp-1", epoch=1, writer_pk=os.urandom(32)
    )
    assert parse_writer_key_cert(cert.to_wire()) == cert
    with pytest.raises(InvalidMemberPublish):
        parse_writer_key_cert({"bad": 1})


# ── L2: optional, signed ``gfs_key`` ─────────────────────────────────────


def test_gfs_key_is_signed_when_present_and_absent_otherwise():
    plain = MemberPublishAnonRequest.from_wire(_anon_wire())
    assert "gfs_key" not in plain.signing_body()
    bound = MemberPublishAnonRequest.from_wire(_anon_wire(gfs_key="ab" * 32))
    assert bound.signing_body()["gfs_key"] == "ab" * 32
    assert bound.signing_bytes() != plain.signing_bytes()
    assert MemberPublishAnonRequest.from_wire(bound.to_wire()) == bound


@pytest.mark.parametrize("bad", ["", "AB" * 32, "ab" * 31, 7])
def test_a_malformed_gfs_key_is_refused(bad):
    with pytest.raises(InvalidMemberPublish):
        MemberPublishAnonRequest.from_wire(_anon_wire(gfs_key=bad))


def test_the_owner_notice_payload_signs_the_gfs_key_only_when_present():
    kw = {
        "owning_instance": "o",
        "gfs_instance_id": "g",
        "space_id": "s",
        "epoch": 1,
        "ts": "t",
    }
    assert "gfs_key" not in owner_epoch_notice_signing_payload(**kw)
    assert owner_epoch_notice_signing_payload(**kw, gfs_key="ab" * 32)["gfs_key"] == (
        "ab" * 32
    )


# ── payload_suite ────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["zlib2", "", 1, "JSON"])
def test_an_unknown_payload_suite_is_refused_everywhere(bad):
    with pytest.raises(InvalidMemberPublish, match="payload_suite"):
        MemberPublishAnonRequest.from_wire({**_anon_wire(), "payload_suite": bad})
    frame = MemberPublishAnonRequest.from_wire(_anon_wire()).fan_out_frame()
    with pytest.raises(InvalidMemberPublish, match="payload_suite"):
        SpaceItemFrame.from_wire({**frame, "payload_suite": bad})


def test_anon_payload_suite_is_signed_and_framed():
    req = MemberPublishAnonRequest.from_wire({**_anon_wire(), "payload_suite": "zlib1"})
    assert req.suite == "zlib1"
    assert req.signing_body()["payload_suite"] == "zlib1"
    assert req.fan_out_frame()["payload_suite"] == "zlib1"
    plain = MemberPublishAnonRequest.from_wire(_anon_wire())
    assert plain.suite == "json"
    assert "payload_suite" not in plain.signing_body()
    assert "payload_suite" not in plain.fan_out_frame()
    assert plain.signing_bytes() != req.signing_bytes()


def test_space_item_frame_carries_the_suite():
    zipped = MemberPublishAnonRequest.from_wire(
        {**_anon_wire(), "payload_suite": "zlib1"}
    )
    frame = SpaceItemFrame.from_wire({"type": "relay", **zipped.fan_out_frame()})
    assert frame.suite == "zlib1"
    assert frame.to_wire() == zipped.fan_out_frame()
    plain = SpaceItemFrame.from_wire(
        MemberPublishAnonRequest.from_wire(_anon_wire()).fan_out_frame()
    )
    assert plain.suite == "json"
    assert "payload_suite" not in plain.to_wire()
