"""An author's public profile for a space's GFS followers (v_60).

A household that follows a public or global space through a GFS holds no
roster, so every post read "Someone". An author's household now publishes
the author's profile into the space as the generic ``author_profile`` item
(:data:`~socialhome.domain.space_item.ITEM_TYPE_AUTHOR_PROFILE`): signed by
the author's household identity key like every member item, encrypted under
the space content key, published through the existing member-publish path.
The GFS sees only ciphertext.

The profile rides the generic item's signed ``content`` field as compact
JSON — :func:`encode_profile_content` / :func:`decode_profile_content` — so
it is covered by the existing author signature with no new signing domain:

* ``display_name`` — the space name, else the household name (≤ 64 chars);
* ``bio`` — the household bio (≤ 300 chars), ``null`` for a protected
  account;
* ``picture`` — base64 WebP ≤ :data:`PROFILE_PICTURE_MAX_BYTES`, at most
  :data:`PROFILE_PICTURE_MAX_PX` square, ``null`` for a protected account;
* ``picture_hash`` — the 16-hex hash of those bytes;
* ``profile_rev`` — a digest of the rest, so a sender can tell a change.

Pure module — no I/O.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from urllib.parse import quote

from .picture_hash import compute_picture_hash

#: Largest picture a profile carries (bytes of WebP).
PROFILE_PICTURE_MAX_BYTES: int = 24 * 1024
#: Longest side of a profile picture (pixels).
PROFILE_PICTURE_MAX_PX: int = 256
#: The household caps (``UserService``).
PROFILE_NAME_MAX_LENGTH: int = 64
PROFILE_BIO_MAX_LENGTH: int = 300


@dataclass(slots=True, frozen=True)
class AuthorProfile:
    """The decoded profile content of one ``author_profile`` item."""

    display_name: str
    bio: str | None
    picture_webp: bytes | None
    picture_hash: str | None
    profile_rev: str


@dataclass(slots=True, frozen=True)
class SpaceAuthorProfile:
    """A follower's cached profile of one author in one space.

    ``ts`` is the author's signed stamp (tz-aware ISO 8601): the newest
    wins. The picture bytes live in the repo, never here.
    """

    space_id: str
    author_user_id: str
    author_pk: str
    display_name: str
    bio: str | None
    picture_hash: str | None
    profile_rev: str
    ts: str


def author_picture_url(space_id: str, user_id: str, picture_hash: str) -> str:
    """The unsigned, ingress-safe URL of a cached author profile picture
    (``GET /api/spaces/{id}/authors/{user_id}/picture``)."""
    return (
        f"api/spaces/{quote(space_id, safe='')}/authors/"
        f"{quote(user_id, safe='')}/picture?v={quote(picture_hash, safe='')}"
    )


def profile_rev(display_name: str, bio: str | None, picture_hash: str | None) -> str:
    """A short digest of the profile — changes when any part changes."""
    body = json.dumps(
        [display_name, bio, picture_hash], separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()[:16]


def encode_profile_content(
    *, display_name: str, bio: str | None, picture_webp: bytes | None
) -> tuple[str, str]:
    """``(content, profile_rev)`` for the signed item ``content``."""
    picture_hash = compute_picture_hash(picture_webp) if picture_webp else None
    rev = profile_rev(display_name, bio, picture_hash)
    content = json.dumps(
        {
            "display_name": display_name,
            "bio": bio,
            "picture": (
                base64.b64encode(picture_webp).decode("ascii") if picture_webp else None
            ),
            "picture_hash": picture_hash,
            "profile_rev": rev,
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return content, rev


def decode_profile_content(content: object) -> AuthorProfile | None:
    """The profile in a verified item's ``content``, or ``None`` when it is
    malformed: no name, an overlong name or bio, a picture that is not WebP,
    too large, or does not match its hash. Never raises."""
    if not isinstance(content, str):
        return None
    try:
        body = json.loads(content)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    name = body.get("display_name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    if len(name) > PROFILE_NAME_MAX_LENGTH:
        return None
    bio = body.get("bio")
    if bio is not None and (
        not isinstance(bio, str) or len(bio) > PROFILE_BIO_MAX_LENGTH
    ):
        return None
    bio = (bio or "").strip() or None
    picture: bytes | None = None
    raw = body.get("picture")
    if raw is not None:
        if not isinstance(raw, str) or len(raw) > PROFILE_PICTURE_MAX_BYTES * 2:
            return None
        try:
            picture = base64.b64decode(raw, validate=True)
        except binascii.Error, ValueError:
            return None
        if (
            len(picture) > PROFILE_PICTURE_MAX_BYTES
            or picture[:4] != b"RIFF"
            or picture[8:12] != b"WEBP"
            or body.get("picture_hash") != compute_picture_hash(picture)
        ):
            return None
    picture_hash = compute_picture_hash(picture) if picture else None
    return AuthorProfile(
        display_name=name,
        bio=bio,
        picture_webp=picture,
        picture_hash=picture_hash,
        profile_rev=profile_rev(name, bio, picture_hash),
    )
