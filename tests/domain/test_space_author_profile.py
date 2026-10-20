"""Author profile content for a space's GFS followers (v_60) — pure."""

from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

from socialhome.domain.picture_hash import compute_picture_hash
from socialhome.domain.space_author_profile import (
    PROFILE_PICTURE_MAX_BYTES,
    author_picture_url,
    decode_profile_content,
    encode_profile_content,
    profile_rev,
)


def _webp() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (1, 2, 3)).save(buf, "WEBP")
    return buf.getvalue()


def test_round_trip():
    pic = _webp()
    content, rev = encode_profile_content(
        display_name="Noor Haddad", bio="Beekeeper.", picture_webp=pic
    )
    p = decode_profile_content(content)
    assert p is not None
    assert (p.display_name, p.bio, p.picture_webp) == ("Noor Haddad", "Beekeeper.", pic)
    assert p.picture_hash == compute_picture_hash(pic)
    assert p.profile_rev == rev


def test_name_only():
    content, rev = encode_profile_content(
        display_name="Kid", bio=None, picture_webp=None
    )
    p = decode_profile_content(content)
    assert p is not None
    assert (p.bio, p.picture_webp, p.picture_hash) == (None, None, None)
    assert p.profile_rev == rev == profile_rev("Kid", None, None)


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
    return json.dumps({"display_name": "Bob", "bio": None, "picture": None, **body})


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
        _raw(picture=7),
        _raw(picture="!!!not base64!!!", picture_hash="0123456789abcdef"),
        _raw(
            picture=base64.b64encode(b"GIF89a....").decode(),
            picture_hash=compute_picture_hash(b"GIF89a...."),
        ),
        _raw(
            picture=base64.b64encode(_webp()).decode(), picture_hash="0123456789abcdef"
        ),
        _raw(
            picture=base64.b64encode(
                b"RIFF\0\0\0\0WEBP" + bytes(PROFILE_PICTURE_MAX_BYTES)
            ).decode(),
            picture_hash="x",
        ),
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
