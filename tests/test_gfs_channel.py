"""Tests for opaque GFS channel keys, statements and grants (v_51)."""

from __future__ import annotations

import dataclasses
import os

import pytest

from socialhome.crypto import b64url_encode, ed25519_public_key
from socialhome.domain.gfs_channel import (
    ChannelPublishAnonRequest,
    GfsChannelGrant,
    UnsupportedChannelSuite,
    valid_channel_id,
)
from socialhome.domain.writer_key import UnsupportedWriterKeySuite
from socialhome.gfs_channel import (
    InvalidChannelSignature,
    bind_grant,
    channel_pk_bytes,
    channel_pk_of,
    derive_channel_epoch_offset,
    derive_channel_seed,
    derive_channel_writer_seed,
    issue_channel_cert,
    issue_channel_pass,
    issue_channel_writer_key,
    issue_repin_cert,
    new_channel_id,
    sign_notice,
    sign_publish_anon,
    sign_register,
    sign_unregister,
    verify_channel_cert,
    verify_channel_pass,
    verify_channel_writer_key_grant,
    verify_grant,
    verify_notice,
    verify_publish_anon,
    verify_register,
    verify_repin_cert,
    verify_unregister,
)
from socialhome.writer_key import derive_writer_seed

SPACE_SEED = os.urandom(32)
SPACE_PK = ed25519_public_key(SPACE_SEED)
SPACE_ID = "space-private-1"
CHANNEL_ID = new_channel_id()
CH_SEED = derive_channel_seed(SPACE_SEED, SPACE_ID, CHANNEL_ID)
CH_PK = channel_pk_bytes(channel_pk_of(CH_SEED))
OWN_SEED = os.urandom(32)
OWN_PK = ed25519_public_key(OWN_SEED)
TS = "2026-10-04T10:00:00+00:00"


def test_channel_id_is_128_random_bits() -> None:
    ids = {new_channel_id() for _ in range(64)}
    assert len(ids) == 64
    for cid in ids:
        assert valid_channel_id(cid) == cid
        assert len(cid) == 32
    assert SPACE_ID not in CHANNEL_ID


def test_channel_key_is_unlinkable_to_space_and_writer_keys() -> None:
    # Deterministic for every seed holder…
    assert derive_channel_seed(SPACE_SEED, SPACE_ID, CHANNEL_ID) == CH_SEED
    # …distinct per channel and per space…
    assert derive_channel_seed(SPACE_SEED, SPACE_ID, new_channel_id()) != CH_SEED
    assert derive_channel_seed(SPACE_SEED, "other", CHANNEL_ID) != CH_SEED
    # …never the space key, nor the space's (public-space) writer key, nor
    # the channel writer key.
    assert CH_SEED != SPACE_SEED
    assert CH_PK != SPACE_PK
    for epoch in (0, 1, 7):
        assert derive_writer_seed(SPACE_SEED, SPACE_ID, epoch) != CH_SEED
        cw = derive_channel_writer_seed(SPACE_SEED, SPACE_ID, CHANNEL_ID, epoch)
        assert cw not in (CH_SEED, derive_writer_seed(SPACE_SEED, SPACE_ID, epoch))
    with pytest.raises(ValueError):
        derive_channel_seed(b"short", SPACE_ID, CHANNEL_ID)
    with pytest.raises(ValueError):
        derive_channel_writer_seed(SPACE_SEED, SPACE_ID, CHANNEL_ID, -1)


def test_cert_round_trip_and_refusals() -> None:
    cert = issue_channel_cert(
        channel_seed=CH_SEED,
        channel_id=CHANNEL_ID,
        epoch=3,
        instance_pk=OWN_PK,
        scope="write",
    )
    wire = cert.to_wire()
    assert "space_id" not in wire
    kw = dict(channel_pk=CH_PK, channel_id=CHANNEL_ID, epoch=3, author_pk=OWN_PK)
    verify_channel_cert(cert, required_scope="comment", **kw)
    # Forged (another key), wrong channel, stale epoch, other household.
    other = derive_channel_seed(os.urandom(32), SPACE_ID, CHANNEL_ID)
    forged = issue_channel_cert(
        channel_seed=other,
        channel_id=CHANNEL_ID,
        epoch=3,
        instance_pk=OWN_PK,
        scope="write",
    )
    with pytest.raises(InvalidChannelSignature):
        verify_channel_cert(forged, required_scope="comment", **kw)
    with pytest.raises(InvalidChannelSignature):
        verify_channel_cert(
            cert, required_scope="comment", **{**kw, "channel_id": new_channel_id()}
        )
    with pytest.raises(InvalidChannelSignature):
        verify_channel_cert(cert, required_scope="comment", **{**kw, "epoch": 4})
    with pytest.raises(InvalidChannelSignature):
        verify_channel_cert(
            cert, required_scope="comment", **{**kw, "author_pk": os.urandom(32)}
        )
    # A tampered field breaks the signature.
    with pytest.raises(InvalidChannelSignature):
        verify_channel_cert(
            dataclasses.replace(cert, epoch=4),
            required_scope="comment",
            **{**kw, "epoch": 4},
        )
    comment = issue_channel_cert(
        channel_seed=CH_SEED,
        channel_id=CHANNEL_ID,
        epoch=3,
        instance_pk=OWN_PK,
        scope="comment",
    )
    with pytest.raises(InvalidChannelSignature):
        verify_channel_cert(comment, required_scope="write", **kw)
    with pytest.raises(UnsupportedChannelSuite):
        verify_channel_cert(
            dataclasses.replace(cert, channel_suite="rot13"),
            required_scope="comment",
            **kw,
        )


def test_pass_is_scope_free_and_bound() -> None:
    p = issue_channel_pass(
        channel_seed=CH_SEED, channel_id=CHANNEL_ID, epoch=2, instance_pk=OWN_PK
    )
    assert "scope" not in p.to_wire()
    verify_channel_pass(p, channel_pk=CH_PK, channel_id=CHANNEL_ID, instance_pk=OWN_PK)
    with pytest.raises(InvalidChannelSignature):
        verify_channel_pass(
            p, channel_pk=CH_PK, channel_id=CHANNEL_ID, instance_pk=os.urandom(32)
        )
    with pytest.raises(InvalidChannelSignature):
        verify_channel_pass(
            p, channel_pk=os.urandom(32), channel_id=CHANNEL_ID, instance_pk=OWN_PK
        )


def test_writer_key_grant() -> None:
    grant = issue_channel_writer_key(
        space_seed=SPACE_SEED, space_id=SPACE_ID, channel_id=CHANNEL_ID, epoch=5
    )
    seed = verify_channel_writer_key_grant(
        grant, channel_pk=CH_PK, channel_id=CHANNEL_ID, epoch=5
    )
    assert seed == derive_channel_writer_seed(SPACE_SEED, SPACE_ID, CHANNEL_ID, 5)
    with pytest.raises(InvalidChannelSignature):
        verify_channel_writer_key_grant(
            grant, channel_pk=CH_PK, channel_id=CHANNEL_ID, epoch=6
        )
    bad_seed = dataclasses.replace(grant, writer_seed=b64url_encode(os.urandom(32)))
    with pytest.raises(InvalidChannelSignature):
        verify_channel_writer_key_grant(
            bad_seed, channel_pk=CH_PK, channel_id=CHANNEL_ID, epoch=5
        )
    with pytest.raises(UnsupportedWriterKeySuite):
        verify_channel_writer_key_grant(
            dataclasses.replace(grant, writer_key_suite="x"),
            channel_pk=CH_PK,
            channel_id=CHANNEL_ID,
            epoch=5,
        )


def test_register_notice_unregister_signatures() -> None:
    reg = sign_register(
        channel_seed=CH_SEED, channel_id=CHANNEL_ID, gfs_instance_id="g", ts=TS
    )
    assert verify_register(reg) == CH_PK
    with pytest.raises(InvalidChannelSignature):
        verify_register(dataclasses.replace(reg, gfs_instance_id="other"))
    notice = sign_notice(
        channel_seed=CH_SEED,
        channel_id=CHANNEL_ID,
        gfs_instance_id="g",
        ts=TS,
        epoch=4,
        publish_mode="trusted",
    )
    verify_notice(notice, channel_pk=CH_PK)
    with pytest.raises(InvalidChannelSignature):
        verify_notice(dataclasses.replace(notice, epoch=99), channel_pk=CH_PK)
    unreg = sign_unregister(
        channel_seed=CH_SEED, channel_id=CHANNEL_ID, gfs_instance_id="g", ts=TS
    )
    verify_unregister(unreg, channel_pk=CH_PK)
    # Domain separation: a notice signature is never a registration one.
    with pytest.raises(InvalidChannelSignature):
        verify_unregister(
            dataclasses.replace(unreg, channel_sig=notice.channel_sig),
            channel_pk=CH_PK,
        )


def test_repin_cert_chains_to_the_pinned_key() -> None:
    new_seed = os.urandom(32)
    new_pk = channel_pk_of(new_seed)
    cert = issue_repin_cert(
        pinned_seed=CH_SEED, channel_id=CHANNEL_ID, new_channel_pk=new_pk, key_epoch=1
    )
    verify_repin_cert(
        cert,
        pinned_pk=CH_PK,
        pinned_suite="ed25519",
        channel_id=CHANNEL_ID,
        new_channel_pk=new_pk,
    )
    # Signed by the NEW key instead → refused.
    self_signed = issue_repin_cert(
        pinned_seed=new_seed, channel_id=CHANNEL_ID, new_channel_pk=new_pk, key_epoch=1
    )
    with pytest.raises(InvalidChannelSignature):
        verify_repin_cert(
            self_signed,
            pinned_pk=CH_PK,
            pinned_suite="ed25519",
            channel_id=CHANNEL_ID,
            new_channel_pk=new_pk,
        )
    with pytest.raises(InvalidChannelSignature):
        verify_repin_cert(
            cert,
            pinned_pk=CH_PK,
            pinned_suite="ed25519",
            channel_id=CHANNEL_ID,
            new_channel_pk=channel_pk_of(os.urandom(32)),
        )


def test_anon_publish_signature() -> None:
    writer = derive_channel_writer_seed(SPACE_SEED, SPACE_ID, CHANNEL_ID, 1)
    req = sign_publish_anon(
        ChannelPublishAnonRequest(
            gfs_instance_id="g",
            channel_id=CHANNEL_ID,
            ts=TS,
            nonce="n" * 22,
            epoch=1,
            payload="ct",
            writer_sig="",
            writer_sig_suite="ed25519",
        ),
        writer_seed=writer,
    )
    verify_publish_anon(req, writer_pk=ed25519_public_key(writer))
    with pytest.raises(InvalidChannelSignature):
        verify_publish_anon(
            dataclasses.replace(req, payload="other"),
            writer_pk=ed25519_public_key(writer),
        )
    with pytest.raises(UnsupportedWriterKeySuite):
        verify_publish_anon(
            dataclasses.replace(req, writer_sig_suite="x"),
            writer_pk=ed25519_public_key(writer),
        )


OFFSET = 5
WIRE = 2 + OFFSET


def _grant(**over) -> GfsChannelGrant:
    unsigned = GfsChannelGrant(
        channel_suite="ed25519",
        space_id=SPACE_ID,
        channel_id=CHANNEL_ID,
        channel_pk=channel_pk_of(CH_SEED),
        epoch=2,
        epoch_offset=OFFSET,
        gfs_ids=("gfs-1",),
        binding_sig_suite="ed25519",
        binding_sig="",
        channel_pass=issue_channel_pass(
            channel_seed=CH_SEED, channel_id=CHANNEL_ID, epoch=WIRE, instance_pk=OWN_PK
        ),
        channel_cert=issue_channel_cert(
            channel_seed=CH_SEED,
            channel_id=CHANNEL_ID,
            epoch=WIRE,
            instance_pk=OWN_PK,
            scope="write",
        ),
        writer_key=issue_channel_writer_key(
            space_seed=SPACE_SEED, space_id=SPACE_ID, channel_id=CHANNEL_ID, epoch=WIRE
        ),
    )
    return bind_grant(dataclasses.replace(unsigned, **over), space_seed=SPACE_SEED)


def test_grant_round_trip_and_verification() -> None:
    grant = _grant()
    parsed = GfsChannelGrant.from_wire(grant.to_wire())
    assert parsed == grant
    seed = verify_grant(parsed, space_pubkey=SPACE_PK, space_id=SPACE_ID, own_pk=OWN_PK)
    assert seed == derive_channel_writer_seed(SPACE_SEED, SPACE_ID, CHANNEL_ID, WIRE)
    assert parsed.channel_epoch == WIRE
    # The offset is bound: shifting it breaks the binding.
    with pytest.raises(InvalidChannelSignature):
        verify_grant(
            dataclasses.replace(grant, epoch_offset=OFFSET + 1),
            space_pubkey=SPACE_PK,
            space_id=SPACE_ID,
            own_pk=OWN_PK,
        )
    # Bound to the space key: a member re-signing with another key fails.
    fake = bind_grant(grant, space_seed=os.urandom(32))
    with pytest.raises(InvalidChannelSignature):
        verify_grant(fake, space_pubkey=SPACE_PK, space_id=SPACE_ID, own_pk=OWN_PK)
    # Redirecting it to other servers breaks the binding.
    moved = dataclasses.replace(grant, gfs_ids=("evil",))
    with pytest.raises(InvalidChannelSignature):
        verify_grant(moved, space_pubkey=SPACE_PK, space_id=SPACE_ID, own_pk=OWN_PK)
    with pytest.raises(InvalidChannelSignature):
        verify_grant(grant, space_pubkey=SPACE_PK, space_id="other", own_pk=OWN_PK)
    # Someone else's grant (pass / cert name another household).
    with pytest.raises(InvalidChannelSignature):
        verify_grant(
            grant, space_pubkey=SPACE_PK, space_id=SPACE_ID, own_pk=os.urandom(32)
        )
    reader = _grant(channel_cert=None, writer_key=None)
    assert (
        verify_grant(reader, space_pubkey=SPACE_PK, space_id=SPACE_ID, own_pk=OWN_PK)
        is None
    )
    with pytest.raises(UnsupportedChannelSuite):
        verify_grant(
            dataclasses.replace(grant, binding_sig_suite="x"),
            space_pubkey=SPACE_PK,
            space_id=SPACE_ID,
            own_pk=OWN_PK,
        )


def test_epoch_offset_is_secret_per_channel_and_40_bits() -> None:
    a = derive_channel_epoch_offset(SPACE_SEED, SPACE_ID, CHANNEL_ID)
    assert a == derive_channel_epoch_offset(SPACE_SEED, SPACE_ID, CHANNEL_ID)
    assert 0 <= a < 2**40
    assert a != derive_channel_epoch_offset(SPACE_SEED, SPACE_ID, new_channel_id())
    assert a != derive_channel_epoch_offset(os.urandom(32), SPACE_ID, CHANNEL_ID)
