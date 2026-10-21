"""Author profile content for a space's GFS followers (v_60) — pure."""

from __future__ import annotations

import base64
import io
import json
import re

import pytest
from PIL import Image

from socialhome.domain.space_author_profile import (
    MAX_PROFILE_ITEM_BYTES,
    PROFILE_ITEM_KEYS,
    PROFILE_PICTURE_FIELD,
    PROFILE_PICTURE_MAX_BYTES,
    author_picture_url,
    decode_picture_sidecar,
    decode_profile_content,
    encode_picture_sidecar,
    encode_profile_content,
    profile_picture_digest,
    profile_rev,
    stored_profile_inner,
    without_picture_sidecar,
)
from socialhome.services.space_item_author import _SIGNED_FIELDS


def _webp() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (1, 2, 3)).save(buf, "WEBP")
    return buf.getvalue()


def test_round_trip_carries_the_hash_not_the_bytes():
    """The signed content names the picture by its hash only: the bytes
    ride beside it as an unsigned sidecar, so a names-only item stays
    author-verifiable and small."""
    pic = _webp()
    content, rev = encode_profile_content(
        display_name="Noor Haddad",
        bio="Beekeeper.",
        picture_hash=profile_picture_digest(pic),
    )
    assert 'picture"' not in content.replace("picture_hash", "")
    assert len(content) < 256
    p = decode_profile_content(content)
    assert p is not None
    assert (p.display_name, p.bio) == ("Noor Haddad", "Beekeeper.")
    assert p.picture_hash == profile_picture_digest(pic)
    assert p.profile_rev == rev


def test_name_only():
    content, rev = encode_profile_content(
        display_name="Kid", bio=None, picture_hash=None
    )
    p = decode_profile_content(content)
    assert p is not None
    assert (p.bio, p.picture_hash) == (None, None)
    assert p.profile_rev == rev == profile_rev("Kid", None, None)


def test_sidecar_round_trip():
    pic = _webp()
    wire = encode_picture_sidecar(pic)
    assert decode_picture_sidecar(wire, profile_picture_digest(pic)) == pic


@pytest.mark.parametrize(
    "wire",
    [
        None,
        7,
        "",
        "!!!not base64!!!",
        base64.b64encode(b"GIF89a....").decode(),
        base64.b64encode(
            b"RIFF\0\0\0\0WEBP" + bytes(PROFILE_PICTURE_MAX_BYTES)
        ).decode(),
        "A" * (PROFILE_PICTURE_MAX_BYTES * 2 + 4),
    ],
)
def test_a_malformed_sidecar_is_refused(wire):
    assert decode_picture_sidecar(wire, "0123456789abcdef") is None


def test_a_sidecar_that_does_not_match_the_signed_hash_is_refused():
    """The sidecar is outside the author signature: only the signed hash
    makes it the author's picture."""
    pic = _webp()
    assert (
        decode_picture_sidecar(encode_picture_sidecar(pic), "0123456789abcdef") is None
    )
    assert decode_picture_sidecar(encode_picture_sidecar(pic), None) is None


def test_without_picture_sidecar_strips_only_the_sidecar():
    inner = {"content": "x", PROFILE_PICTURE_FIELD: "abc", "writer_cert": {}}
    assert without_picture_sidecar(inner) == {"content": "x", "writer_cert": {}}
    assert PROFILE_PICTURE_FIELD in inner  # not mutated


def test_rev_changes_with_every_part():
    base = profile_rev("A", "b", "c")
    assert (
        len(
            {
                base,
                profile_rev("A2", "b", "c"),
                profile_rev("A", None, "c"),
                profile_rev("A", "b", None),
            }
        )
        == 4
    )


def _raw(**body) -> str:
    return json.dumps(
        {"display_name": "Bob", "bio": None, "picture_hash": None, **body}
    )


@pytest.mark.parametrize(
    "content",
    [
        None,
        42,
        "",
        "[]",
        "not json",
        _raw(display_name=""),
        _raw(display_name="   "),
        _raw(display_name=7),
        _raw(display_name="x" * 65),
        _raw(bio="x" * 301),
        _raw(bio=["list"]),
        _raw(picture_hash=7),
        _raw(picture_hash="ab&sig=evil"),
        _raw(picture_hash="0123456789ABCDEF"),
    ],
)
def test_malformed_content_is_refused(content):
    assert decode_profile_content(content) is None


def test_picture_url_is_relative_and_quoted():
    assert (
        author_picture_url("sp-1", "u-1", "0123456789abcdef")
        == "api/spaces/sp-1/authors/u-1/picture?v=0123456789abcdef"
    )
    assert "/" not in author_picture_url("a/b", "u", "h").split("/authors/")[0][11:]


def test_the_signed_picture_digest_is_a_full_sha256():
    """Review I4: a 64-bit hash prefix lets an author forge a second WebP
    with the same signed hash (~2^32 work) and show different pictures on
    different GFSs. The signed digest is the full SHA-256."""
    pic = _webp()
    digest = profile_picture_digest(pic)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    content, _rev = encode_profile_content(
        display_name="Noor", bio=None, picture_hash=digest
    )
    assert decode_profile_content(content).picture_hash == digest
    short, _ = encode_profile_content(
        display_name="Noor", bio=None, picture_hash=digest[:16]
    )
    assert decode_profile_content(short) is None


def test_a_stored_profile_item_keeps_only_the_signed_shape():
    """Review I5: a member could pad its own item with unsigned junk and
    crowd every other author out of a bundle's names part."""
    inner = {
        "item_type": "author_profile",
        "content": "{}",
        "author_sig": "s",
        "writer_cert": {"x": 1},
        PROFILE_PICTURE_FIELD: "UklGRg==",
        "junk": "x" * 100_000,
    }
    kept = stored_profile_inner(inner)
    assert kept is not None and "junk" not in kept
    assert kept[PROFILE_PICTURE_FIELD] == "UklGRg=="
    assert set(kept) <= PROFILE_ITEM_KEYS
    big = {**inner, "content": "x" * (MAX_PROFILE_ITEM_BYTES + 1)}
    assert stored_profile_inner(big) is None


def test_the_kept_keys_cover_every_signed_field():
    assert set(_SIGNED_FIELDS) <= PROFILE_ITEM_KEYS
