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
* ``picture_hash`` — the full SHA-256 (hex) of the author's public picture (a
  WebP ≤ :data:`PROFILE_PICTURE_MAX_BYTES`, at most
  :data:`PROFILE_PICTURE_MAX_PX` square), ``null`` for none / a protected
  account;
* ``profile_rev`` — a digest of the rest, so a sender can tell a change.

The picture BYTES are not in the signed content: they ride beside it as the
unsigned top-level inner field :data:`PROFILE_PICTURE_FIELD` (base64 WebP,
the "sidecar" — :func:`encode_picture_sidecar`), which a receiver accepts
only when :func:`decode_picture_sidecar` finds a WebP within the cap whose
hash IS the signed ``picture_hash``. So the author's signature still covers
the picture (through its hash), a name / bio change goes out without the
bytes (an item in the 4 KiB padding bucket instead of 64 KiB), and the
host's new-follower bundle can send every name first and the pictures in
later parts (:mod:`socialhome.domain.space_author_profile_bundle`).

Pure module — no I/O.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import quote

#: Largest picture a profile carries (bytes of WebP).
PROFILE_PICTURE_MAX_BYTES: int = 24 * 1024
#: Longest side of a profile picture (pixels).
PROFILE_PICTURE_MAX_PX: int = 256
#: The household caps (``UserService``).
PROFILE_NAME_MAX_LENGTH: int = 64
PROFILE_BIO_MAX_LENGTH: int = 300

#: The unsigned inner field the picture bytes ride in (base64 WebP) next to
#: the signed ``content`` that names them by hash.
PROFILE_PICTURE_FIELD: str = "picture"

#: The signed ``picture_hash`` of a profile picture: the FULL SHA-256 of the
#: WebP, hex. A 64-bit prefix (the household ``picture_hash`` format) would
#: let an author make two WebPs with the same signed hash (~2^32 work) and
#: show different pictures through different servers.
_PICTURE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

#: Every key a kept (and forwarded) ``author_profile`` item may carry: the
#: author-signed fields (``services.space_item_author._SIGNED_FIELDS``,
#: pinned by a test), the signature, the writer cert and the picture
#: sidecar. Anything else is unsigned padding a member could use to crowd
#: other authors out of a bundle.
PROFILE_ITEM_KEYS: frozenset[str] = frozenset(
    {
        "item_type",
        "item_target",
        "space_id",
        "post_id",
        "author_user_id",
        "author_pk",
        "author_username",
        "identity_anchor",
        "origin_instance_id",
        "ts",
        "comment_type",
        "parent_id",
        "content",
        "media_url",
        "created_at",
        "emoji",
        "author_sig_suite",
        "author_sig",
        "writer_cert",
        "picture",
    }
)

#: The most bytes a kept profile item may take without its picture (a
#: name-and-bio item with its cert is ~1–2 KiB).
MAX_PROFILE_ITEM_BYTES: int = 4096


@dataclass(slots=True, frozen=True)
class AuthorProfile:
    """The decoded profile content of one ``author_profile`` item."""

    display_name: str
    bio: str | None
    picture_hash: str | None
    profile_rev: str


@dataclass(slots=True, frozen=True)
class SpaceAuthorProfile:
    """A follower's cached profile of one author in one space.

    ``ts`` is the author's signed stamp (tz-aware ISO 8601): the newest
    wins. ``picture_hash`` is the author's SIGNED hash of their picture
    (what "do I already hold this picture" compares); the bytes live in the
    repo, never here, and may arrive after the row (a later bundle part).
    """

    space_id: str
    author_user_id: str
    author_pk: str
    display_name: str
    bio: str | None
    picture_hash: str | None
    profile_rev: str
    ts: str


@dataclass(slots=True, frozen=True)
class CachedAuthorProfile:
    """One cached profile row, as a receiver checks a new item against it:
    the profile; whether picture bytes are held for its signed hash; and
    the original picture sidecar of the kept signed item (a seed holder
    only, which forwards it in a bundle)."""

    profile: SpaceAuthorProfile
    has_picture: bool
    picture_sidecar: str | None


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
    *, display_name: str, bio: str | None, picture_hash: str | None
) -> tuple[str, str]:
    """``(content, profile_rev)`` for the signed item ``content`` — the
    picture by its hash only (the bytes go in the sidecar)."""
    rev = profile_rev(display_name, bio, picture_hash)
    content = json.dumps(
        {
            "display_name": display_name,
            "bio": bio,
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
    malformed: no name, an overlong name or bio, or a picture hash not in
    our own format. Never raises."""
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
    picture_hash = body.get("picture_hash")
    if picture_hash is not None and (
        not isinstance(picture_hash, str) or not _PICTURE_DIGEST_RE.match(picture_hash)
    ):
        return None
    return AuthorProfile(
        display_name=name,
        bio=bio,
        picture_hash=picture_hash,
        profile_rev=profile_rev(name, bio, picture_hash),
    )


def profile_picture_digest(picture_webp: bytes) -> str:
    """The signed ``picture_hash`` of a profile picture (full SHA-256, hex)."""
    return hashlib.sha256(picture_webp).hexdigest()


def stored_profile_inner(inner: dict) -> dict | None:
    """``inner`` as a cache keeps and a bundle forwards it: only
    :data:`PROFILE_ITEM_KEYS`, and ``None`` when that is still over
    :data:`MAX_PROFILE_ITEM_BYTES` without the picture."""
    kept = {k: v for k, v in inner.items() if k in PROFILE_ITEM_KEYS}
    bare = {k: v for k, v in kept.items() if k != PROFILE_PICTURE_FIELD}
    if len(json.dumps(bare, separators=(",", ":")).encode()) > MAX_PROFILE_ITEM_BYTES:
        return None
    return kept


def encode_picture_sidecar(picture_webp: bytes) -> str:
    """The :data:`PROFILE_PICTURE_FIELD` wire value of a picture."""
    return base64.b64encode(picture_webp).decode("ascii")


def decode_picture_sidecar(raw: object, picture_hash: str | None) -> bytes | None:
    """The sidecar's bytes when they are the picture the author signed for:
    base64 of a WebP (RIFF/WEBP magic) of at most
    :data:`PROFILE_PICTURE_MAX_BYTES` whose hash is ``picture_hash``.
    ``None`` otherwise (no signed hash, malformed, too big, not WebP, or a
    mismatch — the sidecar is outside the signature). Never raises."""
    if picture_hash is None or not isinstance(raw, str) or not raw:
        return None
    if len(raw) > (PROFILE_PICTURE_MAX_BYTES * 4) // 3 + 4:
        return None
    try:
        picture = base64.b64decode(raw, validate=True)
    except binascii.Error, ValueError:
        return None
    if (
        len(picture) > PROFILE_PICTURE_MAX_BYTES
        or picture[:4] != b"RIFF"
        or picture[8:12] != b"WEBP"
        or profile_picture_digest(picture) != picture_hash
    ):
        return None
    return picture


def without_picture_sidecar(inner: dict) -> dict:
    """``inner`` without its picture sidecar (a copy) — what a cache keeps:
    the author-signed item, never the bytes a second time."""
    return {k: v for k, v in inner.items() if k != PROFILE_PICTURE_FIELD}
