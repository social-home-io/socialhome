"""The space media cache's upload authorization wire shape — one header, one
scope since v_96 (a space's opaque channel), the kinds it takes."""

from __future__ import annotations

import base64
import hashlib

import orjson
import pytest

from socialhome.canonical_json import canonical_bytes
from socialhome.domain import gfs_space_media
from socialhome.domain.gfs_space_media import (
    ADDR_SUITE_SM1,
    AUTH_CHANNEL,
    AUTH_KINDS_BY_SCOPE,
    AUTH_WRITER_KEY,
    CHANNEL_MEDIA_PUT_PREFIX,
    MEDIA_SCOPES,
    SCOPE_CHANNEL,
    SPACE_MEDIA_AUTH_MAX_CHARS,
    SPACE_MEDIA_WRITER_PREFIX,
    SUPPORTED_ADDR_SUITES,
    InvalidSpaceMediaAuth,
    SpaceMediaClaim,
    SpaceMediaUploadAuth,
    UnsupportedSpaceMediaAddrSuite,
    media_delete_payload,
    space_media_address,
    valid_scope_id,
)
from socialhome.moment_public_media import MAX_BLOB_BYTES

BID = "ab" * 32
CHANNEL = "0f" * 16
OTHER_CHANNEL = "1e" * 16

CLAIM = SpaceMediaClaim(
    scope=SCOPE_CHANNEL,
    scope_id=CHANNEL,
    blob_id=BID,
    size=65_564,
    expires_at=2_000_000,
    ts=1_000_000,
)

CERT_WIRE = {
    "cert_suite": "ed25519",
    "space_id": "sp-1",
    "epoch": 3,
    "instance_pk": "A" * 43,
    "scope": "write",
    "issued_at": 1,
    "cert_sig": "S" * 86,
}


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(orjson.dumps(obj)).rstrip(b"=").decode()


def test_claim_signing_body_is_exactly_the_seven_fields():
    assert CLAIM.signing_body() == {
        "scope": "channel",
        "scope_id": CHANNEL,
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
    ciphertext), lowercase hex — a fixed vector so the household side and
    every GFS compute the same address."""
    assert ADDR_SUITE_SM1 in SUPPORTED_ADDR_SUITES
    assert (
        space_media_address(SCOPE_CHANNEL, CHANNEL, _VECTOR_DATA)
        == "c6a7b3c167834828c3723e3cbc13baee245cc6cc558ae369e0f274fe3621e2bf"
    )


def test_the_same_ciphertext_has_another_address_in_another_channel():
    a = space_media_address(SCOPE_CHANNEL, CHANNEL, _VECTOR_DATA)
    assert a != space_media_address(SCOPE_CHANNEL, OTHER_CHANNEL, _VECTOR_DATA)


@pytest.mark.parametrize(
    ("kind", "sid", "suite"),
    [
        ("room", CHANNEL, "sm1"),
        # v_96 — the space-keyed scope is gone (owner decision Q5).
        ("space", "sp-1", "sm1"),
        ("channel", "a|b", "sm1"),
        ("channel", "", "sm1"),
        ("channel", CHANNEL, "sm2"),
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


def test_channel_kind_signs_the_claim_after_its_own_prefix():
    assert CLAIM.channel_signing_bytes() == CHANNEL_MEDIA_PUT_PREFIX + (
        canonical_bytes(CLAIM.signing_body())
    )


def test_writer_bytes_bind_the_kind_epoch_and_scope():
    a = CLAIM.writer_signing_bytes(kind=AUTH_WRITER_KEY, epoch=3)
    assert a == SPACE_MEDIA_WRITER_PREFIX + canonical_bytes(
        {**CLAIM.signing_body(), "auth": AUTH_WRITER_KEY, "epoch": 3}
    )
    assert a != CLAIM.writer_signing_bytes(kind=AUTH_WRITER_KEY, epoch=4)


@pytest.mark.parametrize(
    "auth",
    [
        SpaceMediaUploadAuth(
            claim=CLAIM, kind=AUTH_CHANNEL, sig="x" * 86, sig_suite="ed25519"
        ),
        SpaceMediaUploadAuth(
            claim=CLAIM,
            kind=AUTH_WRITER_KEY,
            sig="x" * 86,
            sig_suite="ed25519",
            epoch=3,
        ),
    ],
    ids=["channel", "channel_writer_key"],
)
def test_header_round_trips(auth):
    header = auth.to_header()
    assert len(header) <= SPACE_MEDIA_AUTH_MAX_CHARS
    assert SpaceMediaUploadAuth.from_header(header) == auth


def test_only_the_channel_scope_and_no_kind_names_a_household():
    # v_90 — the identified writer-cert / channel-cert kinds are gone; v_96
    # — so is the space scope with its authority and space writer-key kinds.
    assert MEDIA_SCOPES == frozenset({SCOPE_CHANNEL})
    assert AUTH_KINDS_BY_SCOPE == {
        SCOPE_CHANNEL: frozenset({AUTH_CHANNEL, AUTH_WRITER_KEY}),
    }
    assert not hasattr(gfs_space_media, "SCOPE_SPACE")
    assert not hasattr(gfs_space_media, "AUTH_AUTHORITY")
    assert not hasattr(SpaceMediaClaim, "authority_signing_bytes")


def test_a_space_scope_id_is_refused():
    with pytest.raises(InvalidSpaceMediaAuth):
        valid_scope_id("space", "sp-1")
    assert valid_scope_id("channel", CHANNEL) == CHANNEL


def _wire(claim=CLAIM, **over) -> dict:
    base = SpaceMediaUploadAuth(
        claim=claim, kind=AUTH_CHANNEL, sig="x" * 86, sig_suite="ed25519"
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
        _b64(_wire(scope_id="not-a-channel-id")),
        _b64(_wire(scope="room")),
        # v_96 — a space-scope claim, under either of its retired kinds.
        _b64(_wire(scope="space", scope_id="sp-1")),
        _b64(_wire(scope="space", scope_id="sp-1", auth="authority")),
        _b64(_wire(auth="authority")),
        # The identified kinds are gone (v_90).
        _b64(_wire(auth="channel_cert", epoch=3, channel_cert={"x": 1})),
        _b64(_wire(auth="writer_cert", epoch=3, writer_cert=CERT_WIRE)),
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
        _b64(_wire(auth=AUTH_WRITER_KEY, epoch=3, writer_cert=CERT_WIRE)),
        _b64(_wire(auth=AUTH_WRITER_KEY, epoch=-1)),
        _b64(_wire(extra=1)),
        "a" * (SPACE_MEDIA_AUTH_MAX_CHARS + 1),
    ],
)
def test_malformed_headers_are_refused(raw):
    with pytest.raises(InvalidSpaceMediaAuth):
        SpaceMediaUploadAuth.from_header(raw)


def test_delete_payload_binds_scope_blob_and_time():
    assert media_delete_payload("channel", CHANNEL, BID, 5) == {
        "scope": "channel",
        "scope_id": CHANNEL,
        "blob_id": BID,
        "ts": 5,
    }


# ── Pieces (sm1p, v_97): a file over one blob, split into 8 MiB pieces ──


def _hashes(*blobs: bytes) -> tuple[str, ...]:
    return tuple(hashlib.sha256(b).hexdigest() for b in blobs)


def _proof(blobs, index):
    return gfs_space_media.PieceProof(manifest=_hashes(*blobs), index=index)


def _addr(proof, scope_id=CHANNEL):
    return gfs_space_media.piece_address(
        gfs_space_media.pieced_file_address(SCOPE_CHANNEL, scope_id, proof.manifest),
        proof.index,
    )


def test_sm1p_is_a_supported_suite_with_its_own_constant():
    assert gfs_space_media.ADDR_SUITE_SM1P == "sm1p"
    assert gfs_space_media.ADDR_SUITE_SM1P in SUPPORTED_ADDR_SUITES


def test_a_piece_count_follows_the_file_size():
    pc = gfs_space_media.piece_count
    piece = gfs_space_media.PIECE_BYTES
    assert piece == 8 * 1024 * 1024
    assert pc(piece + 1) == 2
    assert pc(3 * piece) == 3
    assert pc(gfs_space_media.MAX_PIECED_FILE_BYTES) == gfs_space_media.MAX_PIECES
    assert gfs_space_media.MAX_PIECES == 25
    for bad in (0, piece, gfs_space_media.MAX_PIECED_FILE_BYTES + 1, True, "9"):
        with pytest.raises(ValueError):
            pc(bad)


def test_piece_addresses_derive_from_the_file_address_and_the_index():
    hs = _hashes(b"a", b"b", b"c")
    file_addr = gfs_space_media.pieced_file_address(SCOPE_CHANNEL, CHANNEL, hs)
    # Bound to the scope and to every piece.
    assert file_addr != gfs_space_media.pieced_file_address(SCOPE_CHANNEL, "d" * 32, hs)
    assert file_addr != gfs_space_media.pieced_file_address(
        SCOPE_CHANNEL, CHANNEL, _hashes(b"a", b"x", b"c")
    )
    addrs = {gfs_space_media.piece_address(file_addr, i) for i in range(3)}
    assert len(addrs) == 3
    # Never the content address of the piece itself.
    assert gfs_space_media.piece_address(file_addr, 0) != space_media_address(
        SCOPE_CHANNEL, CHANNEL, b"a"
    )
    with pytest.raises(ValueError):
        gfs_space_media.piece_address(file_addr, -1)
    with pytest.raises(ValueError):
        gfs_space_media.piece_address(file_addr, gfs_space_media.MAX_PIECES)


def test_space_media_address_refuses_sm1p_content_addressing():
    with pytest.raises(UnsupportedSpaceMediaAddrSuite):
        space_media_address(SCOPE_CHANNEL, CHANNEL, b"x", suite="sm1p")


def test_check_piece_accepts_the_right_piece_at_its_address():
    proof = _proof((b"one", b"two", b"three"), 1)
    assert gfs_space_media.check_piece(
        SCOPE_CHANNEL, CHANNEL, _addr(proof), b"two", proof
    )


def test_check_piece_refuses_a_wrong_piece_at_a_real_address():
    blobs = (b"one", b"two", b"three")
    proof = _proof(blobs, 1)
    addr = _addr(proof)
    check = gfs_space_media.check_piece
    assert not check(SCOPE_CHANNEL, CHANNEL, addr, b"evil", proof)
    # A forged manifest naming the evil piece gives another file address.
    forged = _proof((b"one", b"evil", b"three"), 1)
    assert not check(SCOPE_CHANNEL, CHANNEL, addr, b"evil", forged)
    # The right piece under another index, or another channel, is refused.
    assert not check(SCOPE_CHANNEL, CHANNEL, addr, b"one", _proof(blobs, 0))
    assert not check(SCOPE_CHANNEL, "d" * 32, addr, b"two", proof)


def test_a_valid_piece_replayed_under_another_file_address_lands_elsewhere():
    proof = _proof((b"one", b"two"), 1)
    other = _proof((b"zzz", b"two"), 1)
    assert _addr(other) != _addr(proof)
    assert not gfs_space_media.check_piece(
        SCOPE_CHANNEL, CHANNEL, _addr(proof), b"two", other
    )


def test_check_piece_never_raises_on_a_bad_scope():
    proof = _proof((b"a", b"b"), 0)
    assert not gfs_space_media.check_piece(SCOPE_CHANNEL, "", "0" * 64, b"a", proof)


def test_piece_proof_header_round_trips():
    proof = _proof((b"a", b"b", b"c"), 2)
    assert gfs_space_media.PieceProof.from_header(proof.to_header()) == proof


def _raw_proof(obj) -> str:
    return base64.urlsafe_b64encode(orjson.dumps(obj)).decode().rstrip("=")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "!!!",
        _raw_proof([]),
        _raw_proof({"manifest": ["a" * 64, "b" * 64], "index": 2}),
        _raw_proof({"manifest": ["a" * 64, "b" * 64], "index": -1}),
        _raw_proof({"manifest": ["a" * 64, "b" * 64], "index": True}),
        _raw_proof({"manifest": ["a" * 64], "index": 0}),
        _raw_proof({"manifest": ["a" * 64] * 26, "index": 0}),
        _raw_proof({"manifest": ["A" * 64, "b" * 64], "index": 0}),
        _raw_proof({"manifest": ["a" * 63, "b" * 64], "index": 0}),
        _raw_proof({"manifest": "ab", "index": 0}),
        _raw_proof({"manifest": ["a" * 64, "b" * 64], "index": 0, "x": 1}),
        "a" * 5000,
    ],
)
def test_malformed_piece_proofs_are_refused(raw):
    with pytest.raises(gfs_space_media.InvalidPieceProof):
        gfs_space_media.PieceProof.from_header(raw)
