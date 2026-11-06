"""Space media sealed for the GFS media cache.

A household reached only through a GFS — a space member seated from an
invite link, a follower of a global space — can't fetch a space picture from
the household that posted it. So the author's household seals each file
**once** and uploads the same blob to every GFS that needs it; readers fetch
it by address (``GET /gfs/media/{blob_id}``) and open it with the
space content key they already hold. The GFS sees ciphertext only.

* **Key.** ``HKDF-SHA256(content key of the epoch, info="sh-space-media:v1")``
  — whoever holds the space's content key for that epoch (a member, a
  follower that got the key) can open the file, nobody else. The raw
  content key never leaves :class:`~socialhome.services.space_crypto_service
  .SpaceContentEncryption`; only this derived key does.
* **Deterministic nonce.** ``HKDF-SHA256(media key, info="sh-space-media:v1|
  nonce|" + filename + "|" + sha256(file))[:12]``. The same file sealed again
  under the same key yields the byte-identical blob (same ``blob_id``), so a
  re-upload after the cache dropped it matches the descriptor readers
  already hold, and the sender never has to keep a sealed copy. Two
  different files never share a nonce under one key (the content hash is in
  the derivation). The only thing it reveals is "the same file was sealed
  twice", which the content address shows anyway.
* **AAD.** ``suite | space_id | epoch | filename`` — a blob can't be moved
  onto another space, epoch or file.
* **Padding.** :func:`socialhome.moment_public_media.pad_media`: the GFS
  learns a size bucket, not the size; at most :data:`MAX_SPACE_MEDIA_BYTES`.
* **Suite.** :data:`SPACE_MEDIA_SUITE_AESGCM256_PADDED` rides every
  descriptor; receivers refuse any other (:class:`UnsupportedSpaceMediaSuite`),
  never a default.

Pure — no I/O. Sealing an 8 MiB file is CPU work: callers run it in a
worker thread (``asyncio.to_thread``).
"""

from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .moment_public_media import MAX_MEDIA_BYTES, open_padded, seal_padded

#: The one space media suite: AES-256-GCM over the padded file, deterministic
#: nonce.
SPACE_MEDIA_SUITE_AESGCM256_PADDED: str = "aesgcm-256-pad-det"
SUPPORTED_SPACE_MEDIA_SUITES: frozenset[str] = frozenset(
    {SPACE_MEDIA_SUITE_AESGCM256_PADDED}
)

#: Largest file that goes through the cache (larger ones — long videos —
#: wait for the on-demand path).
MAX_SPACE_MEDIA_BYTES: int = MAX_MEDIA_BYTES

_KEY_INFO = b"sh-space-media:v1"
_NONCE_INFO = b"sh-space-media:v1|nonce|"
_AAD_PREFIX = b"socialhome/space-media/v1|"
_NONCE_BYTES = 12


class UnsupportedSpaceMediaSuite(ValueError):
    """A descriptor names a space media suite this build lacks."""


def require_space_media_suite(suite: object) -> str:
    """*suite* when supported; :class:`UnsupportedSpaceMediaSuite` otherwise."""
    if not isinstance(suite, str) or suite not in SUPPORTED_SPACE_MEDIA_SUITES:
        raise UnsupportedSpaceMediaSuite(f"unsupported space media suite {suite!r}")
    return suite


def derive_space_media_key(content_key: bytes) -> bytes:
    """The media key of one epoch, derived from its content key."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_INFO).derive(
        content_key
    )


def space_media_nonce(media_key: bytes, filename: str, data: bytes) -> bytes:
    """The deterministic 96-bit nonce of *data* stored as *filename*."""
    digest = hashlib.sha256(data).hexdigest()
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_NONCE_BYTES,
        salt=None,
        info=_NONCE_INFO + f"{filename}|{digest}".encode(),
    ).derive(media_key)


def _aad(space_id: str, epoch: int, filename: str) -> bytes:
    return (
        _AAD_PREFIX
        + f"{SPACE_MEDIA_SUITE_AESGCM256_PADDED}|{space_id}|{epoch}|{filename}".encode()
    )


def seal_space_media(
    *, media_key: bytes, space_id: str, epoch: int, filename: str, data: bytes
) -> bytes:
    """The blob for *data*, stored locally as *filename* in *space_id*.
    :class:`ValueError` past :data:`MAX_SPACE_MEDIA_BYTES`."""
    return seal_padded(
        key=media_key,
        nonce=space_media_nonce(media_key, filename, data),
        aad=_aad(space_id, epoch, filename),
        data=data,
    )


def open_space_media(
    *, blob: bytes, media_key: bytes, space_id: str, epoch: int, filename: str
) -> bytes:
    """The file inside *blob*. :class:`ValueError` for a malformed blob,
    :class:`cryptography.exceptions.InvalidTag` when the key, space, epoch or
    file name don't match."""
    return open_padded(blob=blob, key=media_key, aad=_aad(space_id, epoch, filename))


__all__ = [
    "MAX_SPACE_MEDIA_BYTES",
    "SPACE_MEDIA_SUITE_AESGCM256_PADDED",
    "SUPPORTED_SPACE_MEDIA_SUITES",
    "UnsupportedSpaceMediaSuite",
    "derive_space_media_key",
    "open_space_media",
    "require_space_media_suite",
    "seal_space_media",
    "space_media_nonce",
]
