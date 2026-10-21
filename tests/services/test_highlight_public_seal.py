"""End-to-end sealed public highlight stream (§highlights_public)."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ec

from socialhome.services import highlight_public_framing as framing
from socialhome.services.highlight_public_seal import (
    MAX_TOKEN_LEN,
    STREAM_SEAL_SUITE_P256_LINK,
    StreamOpener,
    StreamSealer,
    UnsupportedStreamSealSuite,
    link_secret,
    link_with_secret,
    token_digest,
    usable_token,
)

SEED = bytes(range(32))


def _dec(buf):
    f = framing.decode(buf)
    return f.header, f.payload


async def _frames():
    yield framing.highlight_meta({"id": "hl-1"}, [])
    yield framing.frame_chunk(
        frame_id="f1",
        sequence=0,
        chunk_index=0,
        byte_length=5,
        is_last_chunk=True,
        payload=b"hello",
    )
    yield framing.stream_end()


async def _sealed(viewer, secret, highlight_id="hl-1"):
    sealer = StreamSealer(viewer.public_key(), secret=secret, highlight_id=highlight_id)
    return [f async for f in sealer.wrap(_frames())]


def test_link_secret_is_per_link_and_needs_the_seed():
    a = link_secret(SEED, highlight_id="hl-1", token="t1")
    assert len(a) == 32
    assert a == link_secret(SEED, highlight_id="hl-1", token="t1")
    assert a != link_secret(SEED, highlight_id="hl-1", token="t2")
    assert a != link_secret(SEED, highlight_id="hl-2", token="t1")
    assert a != link_secret(bytes(32), highlight_id="hl-1", token="t1")


def test_the_secret_rides_in_the_fragment_only():
    secret = link_secret(SEED, highlight_id="hl-1", token="t1")
    url = link_with_secret("https://gfs.example/highlight/i/hl-1/t1", secret)
    path, fragment = url.split("#", 1)
    assert path == "https://gfs.example/highlight/i/hl-1/t1"
    assert fragment.startswith("k=") and len(fragment) == 2 + 43
    # Re-applying replaces, never stacks.
    assert link_with_secret(url, secret) == url


async def test_every_frame_after_the_opening_one_is_ciphertext():
    viewer = ec.generate_private_key(ec.SECP256R1())
    secret = link_secret(SEED, highlight_id="hl-1", token="t1")
    out = await _sealed(viewer, secret)
    opening, *rest = out
    header, payload = _dec(opening)
    assert header["kind"] == "stream_seal"
    assert header["seal_suite"] == STREAM_SEAL_SUITE_P256_LINK
    assert payload == b""
    assert len(rest) == 3
    for f in rest:
        h, _p = _dec(f)
        assert h == {"kind": "sealed"}
        # The inner header's JSON values, quoted as they would appear in the
        # clear: a bare two-byte b"f1" turns up in random ciphertext about
        # once in a few hundred runs.
        assert b"hello" not in f and b'"hl-1"' not in f and b'"f1"' not in f

    opener = StreamOpener(viewer, header, secret=secret, highlight_id="hl-1")
    inner = [_dec(opener.open(_dec(f)[1])) for f in rest]
    assert [h["kind"] for h, _ in inner] == [
        "highlight_meta",
        "frame_chunk",
        "stream_end",
    ]
    assert inner[1][1] == b"hello"


async def test_the_link_without_its_secret_opens_nothing():
    """The GFS has the path (and could make its own viewer key) but never
    the fragment."""
    viewer = ec.generate_private_key(ec.SECP256R1())
    secret = link_secret(SEED, highlight_id="hl-1", token="t1")
    opening, first, *_ = await _sealed(viewer, secret)
    header, _ = _dec(opening)
    wrong = StreamOpener(viewer, header, secret=bytes(32), highlight_id="hl-1")
    with pytest.raises(InvalidTag):
        wrong.open(_dec(first)[1])


async def test_someone_elses_view_does_not_open_with_the_link():
    viewer = ec.generate_private_key(ec.SECP256R1())
    other = ec.generate_private_key(ec.SECP256R1())
    secret = link_secret(SEED, highlight_id="hl-1", token="t1")
    opening, first, *_ = await _sealed(viewer, secret)
    header, _ = _dec(opening)
    with pytest.raises(InvalidTag):
        StreamOpener(other, header, secret=secret, highlight_id="hl-1").open(
            _dec(first)[1]
        )


async def test_dropped_or_reordered_frames_are_refused():
    viewer = ec.generate_private_key(ec.SECP256R1())
    secret = link_secret(SEED, highlight_id="hl-1", token="t1")
    opening, _first, second, _ = await _sealed(viewer, secret)
    header, _ = _dec(opening)
    opener = StreamOpener(viewer, header, secret=secret, highlight_id="hl-1")
    with pytest.raises(InvalidTag):
        opener.open(_dec(second)[1])


async def test_a_frame_moved_to_another_highlight_is_refused():
    viewer = ec.generate_private_key(ec.SECP256R1())
    secret = link_secret(SEED, highlight_id="hl-1", token="t1")
    opening, first, *_ = await _sealed(viewer, secret)
    header, _ = _dec(opening)
    with pytest.raises(InvalidTag):
        StreamOpener(viewer, header, secret=secret, highlight_id="hl-2").open(
            _dec(first)[1]
        )


def test_an_unknown_suite_is_refused():
    viewer = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(UnsupportedStreamSealSuite):
        StreamOpener(
            viewer,
            {"seal_suite": "none", "epk": ""},
            secret=bytes(32),
            highlight_id="x",
        )


# ─── Share-token bounds ─────────────────────────────────────────────────


def test_a_usable_token_is_a_bounded_non_empty_string():
    assert usable_token("tok-1")
    assert usable_token("x" * MAX_TOKEN_LEN)
    assert not usable_token("")
    assert not usable_token("x" * (MAX_TOKEN_LEN + 1))
    assert not usable_token(None)
    assert not usable_token(123)


def test_no_secret_is_derived_for_an_oversized_token():
    """The GFS forwards the token: it can't make us HKDF a megabyte."""
    with pytest.raises(ValueError):
        link_secret(SEED, highlight_id="hl-1", token="x" * (MAX_TOKEN_LEN + 1))


def test_the_token_digest_is_a_sha256_hex():
    d = token_digest("tok-1")
    assert len(d) == 64 and d == d.lower()
    assert d != token_digest("tok-2")
