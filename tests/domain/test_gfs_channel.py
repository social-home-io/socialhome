"""Tests for the opaque GFS channel wire codecs (v_51)."""

from __future__ import annotations

import os

import pytest

from socialhome.crypto import ed25519_public_key
from socialhome.domain.gfs_channel import (
    CHANNEL_FRAME_KEYS,
    ChannelCert,
    ChannelEpochNotice,
    ChannelItemFrame,
    ChannelPass,
    ChannelPublishAnonRequest,
    ChannelPublishRequest,
    ChannelRegisterRequest,
    ChannelRepinCert,
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
    issue_channel_cert,
    issue_channel_pass,
    issue_channel_writer_key,
    issue_repin_cert,
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
    cert = issue_channel_cert(
        channel_seed=SEED, channel_id=CID, epoch=1, instance_pk=PK, scope="comment"
    )
    assert ChannelCert.from_wire(cert.to_wire()) == cert
    p = issue_channel_pass(channel_seed=SEED, channel_id=CID, epoch=1, instance_pk=PK)
    assert ChannelPass.from_wire(p.to_wire()) == p
    wk = issue_channel_writer_key(
        space_seed=os.urandom(32), space_id="s", channel_id=CID, epoch=1
    )
    assert ChannelWriterKeyGrant.from_wire(wk.to_wire()) == wk
    repin = issue_repin_cert(
        pinned_seed=SEED, channel_id=CID, new_channel_pk="x" * 43, key_epoch=2
    )
    assert ChannelRepinCert.from_wire(repin.to_wire()) == repin


def test_statements_refuse_extra_or_missing_keys() -> None:
    cert = issue_channel_cert(
        channel_seed=SEED, channel_id=CID, epoch=1, instance_pk=PK, scope="write"
    ).to_wire()
    with pytest.raises(InvalidChannelWire):
        ChannelCert.from_wire({**cert, "space_id": "leak"})
    with pytest.raises(InvalidChannelWire):
        ChannelCert.from_wire({k: v for k, v in cert.items() if k != "epoch"})
    with pytest.raises(InvalidChannelWire):
        ChannelCert.from_wire({**cert, "scope": "admin"})
    with pytest.raises(InvalidChannelWire):
        ChannelCert.from_wire({**cert, "epoch": True})
    repin = issue_repin_cert(
        pinned_seed=SEED, channel_id=CID, new_channel_pk="x" * 43, key_epoch=1
    ).to_wire()
    with pytest.raises(InvalidChannelWire):
        ChannelRepinCert.from_wire({**repin, "key_epoch": 0})


def test_requests_round_trip() -> None:
    reg = sign_register(channel_seed=SEED, channel_id=CID, gfs_instance_id="g", ts=TS)
    assert ChannelRegisterRequest.from_wire(reg.to_wire()) == reg
    notice = sign_notice(
        channel_seed=SEED,
        channel_id=CID,
        gfs_instance_id="g",
        ts=TS,
        epoch=3,
        publish_mode="strict",
    )
    assert ChannelEpochNotice.from_wire(notice.to_wire()) == notice
    with pytest.raises(InvalidChannelWire):
        ChannelEpochNotice.from_wire({**notice.to_wire(), "publish_mode": "loud"})
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
    cert = issue_channel_cert(
        channel_seed=SEED, channel_id=CID, epoch=1, instance_pk=PK, scope="write"
    )
    pub = ChannelPublishRequest(
        instance_id="i",
        gfs_instance_id="g",
        channel_id=CID,
        ts=TS,
        signature="s",
        epoch=1,
        channel_cert=cert,
        payload="ct",
    )
    assert ChannelPublishRequest.from_wire(pub.to_wire()) == pub
    assert set(pub.fan_out_frame()) == CHANNEL_FRAME_KEYS
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
    assert anon.fan_out_frame() == pub.fan_out_frame()


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
