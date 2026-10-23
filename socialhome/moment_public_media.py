"""Encrypted public-moment media (§Momentum-public, v_63).

A public moment's photo or video reaches follower households — and, through
a share link, a browser — through the GFS, which must not read it. The
author's household encrypts each file **once** and uploads the same bytes to
every GFS it publishes through; followers fetch the blob from their GFS.

* **Key.** ``HKDF(item_key, "sh-moment-media:v1|" + role)`` — derived from
  the moment's item key (:func:`socialhome.moment_public_seal.item_key`), so
  whoever can open the moment can open its media, and nobody else. ``role``
  is ``media`` (the file) or ``poster`` (a video's still).
* **Padding.** The plaintext is ``u32 length ‖ data ‖ zeros`` up to the next
  of :data:`PAD_BUCKETS`, so the GFS learns a size *bucket*, not the size.
  At most :data:`MAX_MEDIA_BYTES` of data.
* **Seal.** AES-256-GCM, a fresh 96-bit nonce, associated data binding the
  suite, author, moment id and role (a blob can't be moved onto another
  moment or role). Wire: ``nonce ‖ ciphertext+tag``.
* **Address.** ``blob_id = sha256(blob)`` (64 lowercase hex). The GFS checks
  it on upload; a fetcher checks it before decrypting. The id is
  unguessable without the blob and reveals nothing about the file.
* **Suite.** ``media_suite`` :data:`MEDIA_SUITE_AESGCM256_PADDED` rides the
  sealed moment body; receivers refuse any other value
  (:class:`UnsupportedMediaSuite`), never a default.

Pure and dependency-light (``cryptography`` + :mod:`hashlib` only), so the
GFS process can import the size bounds.
"""

from __future__ import annotations

import hashlib
import os
import re

import orjson
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

#: The one media suite this build speaks: AES-256-GCM over the padded file.
MEDIA_SUITE_AESGCM256_PADDED: str = "aesgcm-256-pad"
SUPPORTED_MEDIA_SUITES: frozenset[str] = frozenset({MEDIA_SUITE_AESGCM256_PADDED})

#: The roles a blob can play for one moment.
#: ``share`` is a moment's share copy: its text and media
#: descriptor, for a per-moment link's viewer.
MEDIA_ROLES: frozenset[str] = frozenset({"media", "poster", "share"})

#: Content types a moment's file may have → (file extension, moment
#: ``media_type``). Anything else is never sent, and never stored.
MEDIA_CONTENT_TYPES: dict[str, tuple[str, str]] = {
    "image/webp": ("webp", "image"),
    "image/jpeg": ("jpg", "image"),
    "image/png": ("png", "image"),
    "image/gif": ("gif", "image"),
    "video/webm": ("webm", "video"),
    "video/mp4": ("mp4", "video"),
}

#: File extension → content type, for the author's side.
CONTENT_TYPE_BY_EXT: dict[str, str] = {
    "webp": "image/webp",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webm": "video/webm",
    "mp4": "video/mp4",
}

#: Padded plaintext sizes (length prefix included). A file goes in the
#: smallest that holds it — about 1.3× its size on average across the larger
#: buckets; the 4 KB bucket holds a share copy.
PAD_BUCKETS: tuple[int, ...] = (
    4 * 1024,
    64 * 1024,
    256 * 1024,
    1024 * 1024,
    2 * 1024 * 1024,
    4 * 1024 * 1024,
    8 * 1024 * 1024 + 4,
)

#: Largest file a public moment carries to followers through a GFS.
MAX_MEDIA_BYTES: int = 8 * 1024 * 1024

_NONCE = 12
_TAG = 16
_LEN = 4

#: Largest blob anyone uploads or accepts.
MAX_BLOB_BYTES: int = PAD_BUCKETS[-1] + _NONCE + _TAG

_AAD_PREFIX = b"socialhome/moment-media/v1|"
_KEY_INFO = b"sh-moment-media:v1|"
_BLOB_ID = re.compile(r"[0-9a-f]{64}")


class UnsupportedMediaSuite(ValueError):
    """A moment names a media suite this build does not implement."""


def require_known_media_suite(suite: object) -> str:
    if not isinstance(suite, str) or suite not in SUPPORTED_MEDIA_SUITES:
        raise UnsupportedMediaSuite(f"unsupported media suite {suite!r}")
    return suite


def is_blob_id(value: object) -> bool:
    return isinstance(value, str) and _BLOB_ID.fullmatch(value) is not None


def blob_id_of(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def padded_size(data_len: int) -> int:
    """The bucket a file of *data_len* bytes is padded to."""
    if data_len < 0 or data_len > MAX_MEDIA_BYTES:
        raise ValueError(f"media of {data_len} bytes is out of bounds")
    need = data_len + _LEN
    for bucket in PAD_BUCKETS:
        if need <= bucket:
            return bucket
    raise ValueError("no bucket holds this media")  # pragma: no cover


def _media_key(item_key: bytes, role: str) -> bytes:
    if role not in MEDIA_ROLES:
        raise ValueError(f"unknown media role {role!r}")
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_INFO + role.encode()
    ).derive(item_key)


def _aad(author_user_id: str, moment_id: str, role: str) -> bytes:
    return (
        _AAD_PREFIX
        + f"{MEDIA_SUITE_AESGCM256_PADDED}|{author_user_id}|{moment_id}|{role}".encode()
    )


def _seal_padded(*, key: bytes, aad: bytes, plaintext: bytes) -> bytes:
    nonce = os.urandom(_NONCE)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def seal_media(
    *, item_key: bytes, author_user_id: str, moment_id: str, role: str, data: bytes
) -> bytes:
    """The blob for one file of one moment. CPU-bound for large files —
    callers run it in a worker thread."""
    bucket = padded_size(len(data))
    plaintext = len(data).to_bytes(_LEN, "big") + data
    plaintext += b"\0" * (bucket - len(plaintext))
    return _seal_padded(
        key=_media_key(item_key, role),
        aad=_aad(author_user_id, moment_id, role),
        plaintext=plaintext,
    )


def open_media(
    *, blob: bytes, item_key: bytes, author_user_id: str, moment_id: str, role: str
) -> bytes:
    """The file inside a blob. Raises :class:`ValueError` for a malformed
    blob and :class:`cryptography.exceptions.InvalidTag` when the key,
    author, moment or role don't match."""
    if not _NONCE + _TAG + PAD_BUCKETS[0] <= len(blob) <= MAX_BLOB_BYTES:
        raise ValueError("media blob size out of bounds")
    if len(blob) - _NONCE - _TAG not in PAD_BUCKETS:
        raise ValueError("media blob is not a padded bucket")
    plaintext = AESGCM(_media_key(item_key, role)).decrypt(
        blob[:_NONCE], blob[_NONCE:], _aad(author_user_id, moment_id, role)
    )
    size = int.from_bytes(plaintext[:_LEN], "big")
    if size > min(MAX_MEDIA_BYTES, len(plaintext) - _LEN):
        raise ValueError("media length prefix out of bounds")
    return plaintext[_LEN : _LEN + size]


def seal_share(
    *, item_key: bytes, author_user_id: str, moment_id: str, body: dict
) -> bytes:
    """A moment's share copy: *body* (text, timestamps, the media
    descriptor) as a ``share`` blob — what a per-moment link's viewer opens
    with the moment's item key. Normally the 4 KB bucket."""
    data = orjson.dumps(body, option=orjson.OPT_SORT_KEYS)
    return seal_media(
        item_key=item_key,
        author_user_id=author_user_id,
        moment_id=moment_id,
        role="share",
        data=data,
    )


def open_share(
    *, blob: bytes, item_key: bytes, author_user_id: str, moment_id: str
) -> dict:
    """The body of a share copy. Raises like :func:`open_media`, and
    :class:`TypeError` when it is not a JSON object."""
    body = orjson.loads(
        open_media(
            blob=blob,
            item_key=item_key,
            author_user_id=author_user_id,
            moment_id=moment_id,
            role="share",
        )
    )
    if not isinstance(body, dict):
        raise TypeError("share copy is not a JSON object")
    return body


__all__ = [
    "CONTENT_TYPE_BY_EXT",
    "MAX_BLOB_BYTES",
    "MAX_MEDIA_BYTES",
    "MEDIA_CONTENT_TYPES",
    "MEDIA_ROLES",
    "MEDIA_SUITE_AESGCM256_PADDED",
    "PAD_BUCKETS",
    "SUPPORTED_MEDIA_SUITES",
    "UnsupportedMediaSuite",
    "blob_id_of",
    "is_blob_id",
    "open_media",
    "open_share",
    "padded_size",
    "require_known_media_suite",
    "seal_media",
    "seal_share",
]
