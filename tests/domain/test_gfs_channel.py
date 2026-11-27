"""Tests for the opaque GFS channel wire codecs (v_51)."""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from socialhome.crypto import ed25519_public_key
from socialhome.domain import gfs_channel as _gc
from socialhome.domain.gfs_channel import (
    CHANNEL_FRAME_KEYS,
    CHANNEL_ROUTES,
    ChannelEpochNotice,
    ChannelItemFrame,
    ChannelPass,
    ChannelPublishAnonRequest,
    ChannelRegisterRequest,
    ChannelSubscribeRequest,
    ChannelUnregisterRequest,
    ChannelUnsubscribeRequest,
    ChannelWriterKeyGrant,
    GfsChannelGrant,
    InvalidChannelWire,
    channel_frame,
    valid_channel_id,
)
from socialhome.gfs_channel import (
    derive_channel_seed,
    issue_channel_pass,
    issue_channel_writer_key,
    new_channel_id,
    sign_notice,
    sign_register,
    sign_unregister,
)

CID = new_channel_id()
SEED = derive_channel_seed(os.urandom(32), "s", CID)
PK = ed25519_public_key(os.urandom(32))
TS = "2026-10-04T10:00:00+00:00"


@pytest.mark.parametrize(
    "bad", ["", "abc", "A" * 32, "g" * 32, "0" * 31, "0" * 33, 5, None]
)
def test_channel_id_validation(bad) -> None:
    with pytest.raises(InvalidChannelWire):
        valid_channel_id(bad)


def test_statement_round_trips() -> None:
    p = issue_channel_pass(channel_seed=SEED, channel_id=CID, epoch=1, instance_pk=PK)
    assert ChannelPass.from_wire(p.to_wire()) == p
    wk = issue_channel_writer_key(
        space_seed=os.urandom(32), space_id="s", channel_id=CID, epoch=1
    )
    assert ChannelWriterKeyGrant.from_wire(wk.to_wire()) == wk


def test_there_is_no_identified_channel_publish() -> None:
    # v_90 — one mode: the channel cert and ``/gfs/channels/publish`` are gone.
    import socialhome.domain.gfs_channel as codec

    for gone in ("ChannelCert", "ChannelPublishRequest", "CHANNEL_PUBLISH_ROUTE"):
        assert not hasattr(codec, gone), gone
    assert "/gfs/channels/publish" not in CHANNEL_ROUTES
    assert "/gfs/channels/publish-anon" in CHANNEL_ROUTES


def test_requests_round_trip() -> None:
    reg = sign_register(channel_seed=SEED, channel_id=CID, gfs_instance_id="g", ts=TS)
    assert ChannelRegisterRequest.from_wire(reg.to_wire()) == reg
    # There is no re-pin: the field is malformed.
    with pytest.raises(InvalidChannelWire):
        ChannelRegisterRequest.from_wire({**reg.to_wire(), "repin_cert": {}})
    notice = sign_notice(
        channel_seed=SEED,
        channel_id=CID,
        gfs_instance_id="g",
        ts=TS,
        epoch=3,
    )
    assert ChannelEpochNotice.from_wire(notice.to_wire()) == notice
    assert "publish_mode" not in notice.to_wire()
    # A notice still carrying a publish mode (pre-v_90) is malformed.
    with pytest.raises(InvalidChannelWire):
        ChannelEpochNotice.from_wire({**notice.to_wire(), "publish_mode": "trusted"})
    unreg = sign_unregister(
        channel_seed=SEED, channel_id=CID, gfs_instance_id="g", ts=TS
    )
    assert ChannelUnregisterRequest.from_wire(unreg.to_wire()) == unreg
    p = issue_channel_pass(channel_seed=SEED, channel_id=CID, epoch=1, instance_pk=PK)
    sub = ChannelSubscribeRequest(
        instance_id="i",
        gfs_instance_id="g",
        channel_id=CID,
        ts=TS,
        signature="s",
        channel_pass=p,
    )
    assert ChannelSubscribeRequest.from_wire(sub.to_wire()) == sub
    assert sub.signing_payload()["action"] == "gfs-channel-subscribe:v1"
    unsub = ChannelUnsubscribeRequest(
        instance_id="i", gfs_instance_id="g", channel_id=CID, ts=TS, signature="s"
    )
    assert ChannelUnsubscribeRequest.from_wire(unsub.to_wire()) == unsub
    anon = ChannelPublishAnonRequest(
        gfs_instance_id="g",
        channel_id=CID,
        ts=TS,
        nonce="n" * 22,
        epoch=1,
        payload="ct",
        writer_sig="w",
        writer_sig_suite="ed25519",
    )
    assert ChannelPublishAnonRequest.from_wire(anon.to_wire()) == anon
    assert anon.fan_out_frame() == channel_frame(CID, 1, "ct")
    assert set(anon.fan_out_frame()) == CHANNEL_FRAME_KEYS


def test_anon_request_refuses_identity_fields() -> None:
    anon = ChannelPublishAnonRequest(
        gfs_instance_id="g",
        channel_id=CID,
        ts=TS,
        nonce="n" * 22,
        epoch=1,
        payload="ct",
        writer_sig="w",
        writer_sig_suite="ed25519",
    ).to_wire()
    for extra in ("instance_id", "signature", "channel_cert", "space_id"):
        with pytest.raises(InvalidChannelWire):
            ChannelPublishAnonRequest.from_wire({**anon, extra: "x"})
    with pytest.raises(InvalidChannelWire):
        ChannelPublishAnonRequest.from_wire({**anon, "event_type": "space_post"})


def test_frame_never_carries_identity() -> None:
    frame = channel_frame(CID, 2, "ct")
    assert set(frame) == CHANNEL_FRAME_KEYS
    parsed = ChannelItemFrame.from_wire({**frame, "type": "relay"})
    assert parsed == ChannelItemFrame(channel_id=CID, epoch=2, payload="ct")
    with pytest.raises(InvalidChannelWire):
        ChannelItemFrame.from_wire({**frame, "channel_id": "nope"})


def test_grant_codec_bounds() -> None:
    p = issue_channel_pass(channel_seed=SEED, channel_id=CID, epoch=1, instance_pk=PK)
    base = {
        "channel_suite": "ed25519",
        "space_id": "s",
        "channel_id": CID,
        "channel_pk": "x" * 43,
        "epoch": 1,
        "epoch_offset": 9,
        "gfs_ids": ["b", "a", "a"],
        "binding_sig_suite": "ed25519",
        "binding_sig": "sig",
        "channel_pass": p.to_wire(),
    }
    grant = GfsChannelGrant.from_wire(base)
    assert grant.gfs_ids == ("a", "b")
    with pytest.raises(InvalidChannelWire):
        GfsChannelGrant.from_wire({**base, "gfs_ids": []})
    with pytest.raises(InvalidChannelWire):
        GfsChannelGrant.from_wire({**base, "gfs_ids": ["g"] * 17})
    with pytest.raises(InvalidChannelWire):
        GfsChannelGrant.from_wire({**base, "extra": 1})
    for bad in (-1, 2**40 + 1, True, "9"):
        with pytest.raises(InvalidChannelWire):
            GfsChannelGrant.from_wire({**base, "epoch_offset": bad})
    assert grant.channel_epoch == 10
    # v_90 — a grant carries no channel cert any more.
    with pytest.raises(InvalidChannelWire):
        GfsChannelGrant.from_wire({**base, "channel_cert": {}})


# ── L2: optional, signed ``gfs_key`` ─────────────────────────────────────


def test_channel_requests_carry_an_optional_signed_gfs_key():
    req = ChannelUnsubscribeRequest(
        instance_id="i",
        gfs_instance_id="g",
        channel_id=new_channel_id(),
        ts="t",
        signature="s",
    )
    assert "gfs_key" not in req.to_wire()
    bound = replace(req, gfs_key="ab" * 32)
    assert bound.signing_payload()["gfs_key"] == "ab" * 32
    assert ChannelUnsubscribeRequest.from_wire(bound.to_wire()) == bound
    with pytest.raises(InvalidChannelWire):
        ChannelUnsubscribeRequest.from_wire({**req.to_wire(), "gfs_key": "zz"})


# ── v_97: the channel ring wire ──────────────────────────────────────────


_RING_CH = "b" * 32


def _publish(**kw) -> _gc.ChannelPublishAnonRequest:
    return _gc.ChannelPublishAnonRequest(
        gfs_instance_id="gfs",
        channel_id=_RING_CH,
        ts="2026-11-27T12:00:00+00:00",
        nonce="n" * 22,
        epoch=3,
        payload="p",
        writer_sig="s",
        writer_sig_suite="ed25519",
        **kw,
    )


def test_a_publish_without_ring_fields_signs_the_v96_bytes():
    body = _publish().signing_body()
    assert "ring_class" not in body and "push_all" not in body


def test_ring_fields_round_trip_inside_the_signed_body():
    req = _publish(ring_class=_gc.RING_CLASS_EPHEMERAL, push_all=True)
    body = req.signing_body()
    assert body["ring_class"] == "ephemeral" and body["push_all"] is True
    assert _gc.ChannelPublishAnonRequest.from_wire(req.to_wire()) == req


def test_watch_and_fetch_parse_and_bound():
    w = _gc.ChannelWatch.from_wire(
        {"type": "channel_watch", "watch": [_RING_CH, _RING_CH], "unwatch": []}
    )
    assert w.watch == (_RING_CH,)
    assert _gc.ChannelWatch.from_wire(w.to_wire()) == w
    with pytest.raises(_gc.InvalidChannelWire):
        _gc.ChannelWatch.from_wire(
            {
                "type": "channel_watch",
                "watch": ["c" * 32] * 0
                + [f"{n:032x}" for n in range(_gc.MAX_WATCHED_CHANNELS + 1)],
            }
        )
    with pytest.raises(_gc.InvalidChannelWire):
        _gc.ChannelWatch.from_wire({"type": "channel_watch", "x": 1})
    f = _gc.ChannelFetch.from_wire(
        {"type": "channel_fetch", "channel_id": _RING_CH, "since_seq": 4, "req": "r"}
    )
    assert f.ring_id is None
    f2 = _gc.ChannelFetch(_RING_CH, 4, "r", ring_id="0123456789abcdef")
    assert _gc.ChannelFetch.from_wire(f2.to_wire()) == f2
    for bad in (
        {"since_seq": -1},
        {"since_seq": True},
        {"ring_id": "XYZ"},
        {"req": "x" * 65},
        {"type": "channel_watch"},
    ):
        raw = {**f2.to_wire(), **bad}
        with pytest.raises(_gc.InvalidChannelWire):
            _gc.ChannelFetch.from_wire(raw)


def test_ring_frame_and_position():
    frame = _gc.channel_frame(_RING_CH, 3, "p")
    ring = _gc.ring_frame(frame, seq=7, ring_id="0123456789abcdef", push_all=False)
    assert _gc.ring_position(ring) == (7, "0123456789abcdef")
    assert "push_all" not in ring
    assert _gc.ring_frame(frame, seq=1, ring_id="0" * 16, push_all=True)["push_all"]
    assert _gc.ring_position(frame) is None
    # A v_96 household still parses the frame (extra keys ignored).
    assert _gc.ChannelItemFrame.from_wire({"type": "relay", **ring}).epoch == 3
    head = _gc.ChannelHead(_RING_CH, 9, "0" * 16)
    assert _gc.ChannelHead.from_wire(head.to_wire()) == head
    with pytest.raises(_gc.InvalidChannelWire):
        _gc.ChannelHead.from_wire("x")
