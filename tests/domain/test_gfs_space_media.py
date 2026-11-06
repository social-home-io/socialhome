"""The space media cache's upload authorization wire shape — one header, two
scopes (a listed space, a private space's opaque channel), the kinds each
scope takes."""

from __future__ import annotations

import base64

import orjson
import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_MEDIA_PUT,
    AUTHORITY_SIG_SUITE_ED25519,
    authority_signing_bytes,
)
from socialhome.canonical_json import canonical_bytes
from socialhome.domain.gfs_channel import ChannelCert
from socialhome.domain.gfs_space_media import (
    ADDR_SUITE_SM1,
    AUTH_AUTHORITY,
    AUTH_CHANNEL,
    AUTH_CHANNEL_CERT,
    AUTH_WRITER_CERT,
    AUTH_WRITER_KEY,
    CHANNEL_MEDIA_PUT_PREFIX,
    SCOPE_CHANNEL,
    SCOPE_SPACE,
    SPACE_MEDIA_AUTH_MAX_CHARS,
    SPACE_MEDIA_SIG_SUITE_ED25519,
    SPACE_MEDIA_WRITER_PREFIX,
    SUPPORTED_ADDR_SUITES,
    SUPPORTED_SPACE_MEDIA_SIG_SUITES,
    InvalidSpaceMediaAuth,
    SpaceMediaClaim,
    SpaceMediaUploadAuth,
    UnsupportedSpaceMediaAddrSuite,
    UnsupportedSpaceMediaSigSuite,
    media_delete_payload,
    require_space_media_sig_suite,
    space_media_address,
)
from socialhome.domain.writer_cert import WriterCert
from socialhome.moment_public_media import MAX_BLOB_BYTES

BID = "ab" * 32
CHANNEL = "0f" * 16

CLAIM = SpaceMediaClaim(
    scope=SCOPE_SPACE,
    scope_id="sp-1",
    blob_id=BID,
    size=65_564,
    expires_at=2_000_000,
    ts=1_000_000,
)
CH_CLAIM = SpaceMediaClaim(
    scope=SCOPE_CHANNEL,
    scope_id=CHANNEL,
    blob_id=BID,
    size=65_564,
    expires_at=2_000_000,
    ts=1_000_000,
)

CERT = WriterCert(
    cert_suite="ed25519",
    space_id="sp-1",
    epoch=3,
    instance_pk="A" * 43,
    scope="write",
    issued_at=1,
    cert_sig="S" * 86,
)
CH_CERT = ChannelCert(
    channel_suite="ed25519",
    channel_id=CHANNEL,
    epoch=3,
    instance_pk="A" * 43,
    scope="comment",
    issued_at=1,
    cert_sig="S" * 86,
)


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(orjson.dumps(obj)).rstrip(b"=").decode()


def test_claim_signing_body_is_exactly_the_seven_fields():
    assert CLAIM.signing_body() == {
        "scope": "space",
        "scope_id": "sp-1",
        "blob_id": BID,
        "addr_suite": "sm1",
        "size": 65_564,
        "expires_at": 2_000_000,
        "ts": 1_000_000,
    }


# ── The scope-bound address (security review v2, N1) ─────────────────────

_VECTOR_DATA = bytes(range(256)) * 4


def test_the_address_binds_the_scope_test_vector():
    """``sm1``: sha256(b"sm1|" + scope_kind + b"|" + scope_id + b"|" +
    ciphertext), lowercase hex — fixed vectors so the household side and
    every GFS compute the same address."""
    assert ADDR_SUITE_SM1 in SUPPORTED_ADDR_SUITES
    assert (
        space_media_address(SCOPE_SPACE, "sp-1", _VECTOR_DATA)
        == "fe35c777105b74f679173afe164a669ef5695c2e52429f0800ba17e68ed008ef"
    )
    assert (
        space_media_address(SCOPE_CHANNEL, CHANNEL, _VECTOR_DATA)
        == "c6a7b3c167834828c3723e3cbc13baee245cc6cc558ae369e0f274fe3621e2bf"
    )


def test_the_same_ciphertext_has_another_address_in_another_scope():
    a = space_media_address(SCOPE_SPACE, "sp-1", _VECTOR_DATA)
    assert a != space_media_address(SCOPE_SPACE, "sp-2", _VECTOR_DATA)
    assert a != space_media_address(SCOPE_CHANNEL, CHANNEL, _VECTOR_DATA)


@pytest.mark.parametrize(
    ("kind", "sid", "suite"),
    [
        ("room", "sp-1", "sm1"),
        ("space", "a|b", "sm1"),
        ("space", "", "sm1"),
        ("space", "sp-1", "sm2"),
    ],
)
def test_an_address_needs_a_known_suite_and_an_unambiguous_scope(kind, sid, suite):
    with pytest.raises((ValueError, UnsupportedSpaceMediaAddrSuite)):
        space_media_address(kind, sid, b"x", suite=suite)


def test_an_unknown_address_suite_in_a_header_is_refused():
    with pytest.raises(InvalidSpaceMediaAuth):
        SpaceMediaUploadAuth.from_header(_b64(_wire(addr_suite="sm2")))
    with pytest.raises(InvalidSpaceMediaAuth):
        SpaceMediaUploadAuth.from_header(
            _b64({k: v for k, v in _wire().items() if k != "addr_suite"})
        )


def test_authority_kind_signs_the_claim_under_the_put_event_type():
    assert CLAIM.authority_signing_bytes() == authority_signing_bytes(
        event_type=AUTHORITY_EVENT_SPACE_MEDIA_PUT,
        space_id="sp-1",
        payload=CLAIM.signing_body(),
    )


def test_channel_kind_signs_the_claim_after_its_own_prefix():
    assert CH_CLAIM.channel_signing_bytes() == CHANNEL_MEDIA_PUT_PREFIX + (
        canonical_bytes(CH_CLAIM.signing_body())
    )


def test_writer_bytes_bind_the_kind_epoch_and_scope():
    a = CLAIM.writer_signing_bytes(kind=AUTH_WRITER_CERT, epoch=3)
    assert a == SPACE_MEDIA_WRITER_PREFIX + canonical_bytes(
        {**CLAIM.signing_body(), "auth": AUTH_WRITER_CERT, "epoch": 3}
    )
    assert a != CLAIM.writer_signing_bytes(kind=AUTH_WRITER_KEY, epoch=3)
    assert a != CLAIM.writer_signing_bytes(kind=AUTH_WRITER_CERT, epoch=4)
    assert CLAIM.writer_signing_bytes(
        kind=AUTH_WRITER_KEY, epoch=3
    ) != CH_CLAIM.writer_signing_bytes(kind=AUTH_WRITER_KEY, epoch=3)


@pytest.mark.parametrize(
    "auth",
    [
        SpaceMediaUploadAuth(
            claim=CLAIM,
            kind=AUTH_AUTHORITY,
            sig="x" * 86,
            sig_suite=AUTHORITY_SIG_SUITE_ED25519,
        ),
        SpaceMediaUploadAuth(
            claim=CLAIM,
            kind=AUTH_WRITER_CERT,
            sig="x" * 86,
            sig_suite=SPACE_MEDIA_SIG_SUITE_ED25519,
            epoch=3,
            writer_cert=CERT,
        ),
        SpaceMediaUploadAuth(
            claim=CLAIM,
            kind=AUTH_WRITER_KEY,
            sig="x" * 86,
            sig_suite="ed25519",
            epoch=3,
        ),
        SpaceMediaUploadAuth(
            claim=CH_CLAIM, kind=AUTH_CHANNEL, sig="x" * 86, sig_suite="ed25519"
        ),
        SpaceMediaUploadAuth(
            claim=CH_CLAIM,
            kind=AUTH_CHANNEL_CERT,
            sig="x" * 86,
            sig_suite="ed25519",
            epoch=3,
            channel_cert=CH_CERT,
        ),
        SpaceMediaUploadAuth(
            claim=CH_CLAIM,
            kind=AUTH_WRITER_KEY,
            sig="x" * 86,
            sig_suite="ed25519",
            epoch=3,
        ),
    ],
    ids=[
        "authority",
        "writer_cert",
        "writer_key",
        "channel",
        "channel_cert",
        "channel_writer_key",
    ],
)
def test_header_round_trips(auth):
    header = auth.to_header()
    assert len(header) <= SPACE_MEDIA_AUTH_MAX_CHARS
    assert SpaceMediaUploadAuth.from_header(header) == auth


def test_writer_cert_never_carries_the_user_binding():
    bound = WriterCert(
        **{
            **{f: getattr(CERT, f) for f in CERT.__slots__},
            "writer_user_ids": ("u1",),
            "users_sig": "U" * 86,
            "users_sig_suite": "ed25519",
        }
    )
    auth = SpaceMediaUploadAuth(
        claim=CLAIM,
        kind=AUTH_WRITER_CERT,
        sig="x",
        sig_suite="ed25519",
        epoch=3,
        writer_cert=bound,
    )
    wire = auth.to_wire()
    assert "writer_user_ids" not in wire["writer_cert"]
    with pytest.raises(InvalidSpaceMediaAuth):
        SpaceMediaUploadAuth.from_header(_b64({**wire, "writer_cert": bound.to_wire()}))


def _wire(claim=CLAIM, **over) -> dict:
    base = SpaceMediaUploadAuth(
        claim=claim,
        kind=AUTH_AUTHORITY if claim is CLAIM else AUTH_CHANNEL,
        sig="x" * 86,
        sig_suite="ed25519",
    ).to_wire()
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


@pytest.mark.parametrize(
    "raw",
    [
        "%%%not-base64",
        _b64({"not": "a claim"}),
        base64.urlsafe_b64encode(b"[1,2]").decode(),
        _b64(_wire(blob_id="nope")),
        _b64(_wire(scope_id="")),
        _b64(_wire(scope_id="x" * 129)),
        _b64(_wire(scope="room")),
        _b64(_wire(CH_CLAIM, scope_id="not-a-channel-id")),
        # Each scope takes only its own kinds.
        _b64(_wire(auth=AUTH_CHANNEL)),
        _b64(_wire(CH_CLAIM, auth=AUTH_AUTHORITY)),
        _b64(
            _wire(CH_CLAIM, auth=AUTH_WRITER_CERT, epoch=3, writer_cert=CERT.to_wire())
        ),
        _b64(_wire(auth=AUTH_CHANNEL_CERT, epoch=3, channel_cert=CH_CERT.to_wire())),
        _b64(_wire(size=0)),
        _b64(_wire(size=MAX_BLOB_BYTES + 1)),
        _b64(_wire(size=True)),
        _b64(_wire(ts="1")),
        _b64(_wire(expires_at=-1)),
        _b64(_wire(auth="owner")),
        _b64(_wire(sig="")),
        _b64(_wire(sig_suite="")),
        _b64(_wire(epoch=3)),
        _b64(_wire(auth=AUTH_WRITER_KEY)),
        _b64(_wire(auth=AUTH_WRITER_KEY, epoch=3, writer_cert=CERT.to_wire())),
        _b64(_wire(auth=AUTH_WRITER_CERT, epoch=3)),
        _b64(_wire(auth=AUTH_WRITER_CERT, epoch=-1, writer_cert=CERT.to_wire())),
        _b64(_wire(CH_CLAIM, auth=AUTH_CHANNEL_CERT, epoch=3, channel_cert={"x": 1})),
        _b64(_wire(extra=1)),
        "a" * (SPACE_MEDIA_AUTH_MAX_CHARS + 1),
    ],
)
def test_malformed_headers_are_refused(raw):
    with pytest.raises(InvalidSpaceMediaAuth):
        SpaceMediaUploadAuth.from_header(raw)


def test_cert_holder_suite_is_checked_never_defaulted():
    assert SPACE_MEDIA_SIG_SUITE_ED25519 in SUPPORTED_SPACE_MEDIA_SIG_SUITES
    assert require_space_media_sig_suite("ed25519") == "ed25519"
    with pytest.raises(UnsupportedSpaceMediaSigSuite):
        require_space_media_sig_suite("ed25519+mldsa65")


def test_delete_payload_binds_scope_blob_and_time():
    assert media_delete_payload("channel", CHANNEL, BID, 5) == {
        "scope": "channel",
        "scope_id": CHANNEL,
        "blob_id": BID,
        "ts": 5,
    }
