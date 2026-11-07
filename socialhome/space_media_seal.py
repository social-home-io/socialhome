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
  nonce|" + item_id + "|" + origin + "|" + filename + "|" + sha256(file))
  [:12]``. The same file sealed again for the same item by the same
  household yields the byte-identical blob (same ``blob_id``), so a
  re-upload after the cache dropped it matches the descriptor readers
  already hold, and the sender never has to keep a sealed copy. Every AAD
  input is in the derivation, so one nonce never meets two AADs (or two
  files) under one key. The only thing it reveals is "the same file was
  sealed twice for the same item", which the content address shows anyway.
* **AAD.** ``suite | space_id | epoch | item_id | origin | filename`` — the
  item that shows the file and the household that sealed it (the household
  a reader authenticated as delivering the item), so a blob can't be moved
  onto another space, epoch, item, household or file.
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


def space_media_nonce(
    media_key: bytes, filename: str, data: bytes, *, item_id: str, origin: str
) -> bytes:
    """The deterministic 96-bit nonce of *data*, the file *filename* of item
    *item_id* sealed by household *origin*."""
    digest = hashlib.sha256(data).hexdigest()
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_NONCE_BYTES,
        salt=None,
        info=_NONCE_INFO + f"{item_id}|{origin}|{filename}|{digest}".encode(),
    ).derive(media_key)


def _aad(space_id: str, epoch: int, item_id: str, origin: str, filename: str) -> bytes:
    return (
        _AAD_PREFIX
        + (
            f"{SPACE_MEDIA_SUITE_AESGCM256_PADDED}|{space_id}|{epoch}|"
            f"{item_id}|{origin}|{filename}"
        ).encode()
    )


def seal_space_media(
    *,
    media_key: bytes,
    space_id: str,
    epoch: int,
    item_id: str,
    origin: str,
    filename: str,
    data: bytes,
) -> bytes:
    """The blob for *data*, the file *filename* of item *item_id* of
    *space_id*, sealed by household *origin*. :class:`ValueError` past
    :data:`MAX_SPACE_MEDIA_BYTES`."""
    return seal_padded(
        key=media_key,
        nonce=space_media_nonce(
            media_key, filename, data, item_id=item_id, origin=origin
        ),
        aad=_aad(space_id, epoch, item_id, origin, filename),
        data=data,
    )


def open_space_media(
    *,
    blob: bytes,
    media_key: bytes,
    space_id: str,
    epoch: int,
    item_id: str,
    origin: str,
    filename: str,
) -> bytes:
    """The file inside *blob*. :class:`ValueError` for a malformed blob,
    :class:`cryptography.exceptions.InvalidTag` when the key, space, epoch,
    item, sealing household or file name don't match."""
    return open_padded(
        blob=blob,
        key=media_key,
        aad=_aad(space_id, epoch, item_id, origin, filename),
    )


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
