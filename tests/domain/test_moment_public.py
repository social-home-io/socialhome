"""Public-moment author profile helpers (§Momentum-public) — pure."""

from __future__ import annotations

import pytest

from socialhome.domain.moment_public import (
    PUBLIC_BIO_MAX_LENGTH,
    MomentPublicFollow,
    gfs_picture_proxy_url,
    public_author_bio,
    public_author_name,
    public_picture_digest,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Noor Haddad", "Noor Haddad"),
        ("  Noor  ", "Noor"),
        ("u-noor", None),  # the directory's fallback: an id is not a name
        ("", None),
        ("   ", None),
        (None, None),
        (42, None),
        ("x" * 65, None),
    ],
)
def test_public_author_name(value, expected):
    assert public_author_name(value, user_id="u-noor") == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Beekeeper.", "Beekeeper."),
        (" Beekeeper. ", "Beekeeper."),
        ("", None),
        (None, None),
        (["list"], None),
        ("x" * PUBLIC_BIO_MAX_LENGTH, "x" * PUBLIC_BIO_MAX_LENGTH),
        ("x" * (PUBLIC_BIO_MAX_LENGTH + 1), None),
    ],
)
def test_public_author_bio(value, expected):
    assert public_author_bio(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0123456789abcdef", "0123456789abcdef"),
        ("0123456789abcdef" * 4, "0123456789abcdef"),  # a GFS's full SHA-256
        ("0123456789ABCDEF", None),
        ("ab&sig=evil", None),
        ("../x", None),
        ("0123456789abcdef0", None),
        (None, None),
    ],
)
def test_public_picture_digest(value, expected):
    assert public_picture_digest(value) == expected


def test_proxy_url_needs_a_digest_and_path_safe_ids():
    assert (
        gfs_picture_proxy_url("g1", "u-noor", "0123456789abcdef")
        == "api/gfs/g1/moments/users/u-noor/picture?v=0123456789abcdef"
    )
    assert gfs_picture_proxy_url("g1", "u-noor", None) is None
    assert gfs_picture_proxy_url("g1", "u-noor", "") is None
    assert gfs_picture_proxy_url("g1", "../admin", "0123456789abcdef") is None
    assert gfs_picture_proxy_url("g/1", "u-noor", "0123456789abcdef") is None


def _follow(display_name: str, username: str) -> MomentPublicFollow:
    return MomentPublicFollow(
        follower_user_id="u1",
        followed_user_id="u-noor",
        gfs_id="g1",
        followed_instance_pk="ab" * 32,
        followed_username=username,
        followed_display_name=display_name,
        created_at="2026-10-20 08:00:00",
    )


def test_follow_public_name_falls_back_to_the_username_never_the_id():
    assert _follow("Noor", "noor").public_name == "Noor"
    assert _follow("", "noor").public_name == "noor"
    assert _follow("u-noor", "u-noor").public_name is None
