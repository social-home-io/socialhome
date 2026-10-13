"""Profile-picture hash format (§23 profile).

Every picture hash this household hands out is its own
:func:`compute_picture_hash` of the stored WebP bytes — the 16-char
lowercase SHA-256 hex prefix. It is interpolated into cache-busting picture
URLs (``…/picture?v={hash}``) and carried in federation payloads.

A peer's payload can still carry a ``picture_hash`` of its own (when it
ships no bytes, or bytes we reject). That string is untrusted:
:func:`clean_picture_hash` admits it only in exactly the shape we compute,
so a hostile value (``ab&sig=evil``, ``../x``, megabytes of text) never
reaches storage or a URL. Pure — no I/O.
"""

from __future__ import annotations

import hashlib
import re

#: Length of a picture hash: the SHA-256 hex prefix kept for URLs.
PICTURE_HASH_LEN: int = 16

#: The only shape a picture hash may have (matched with ``fullmatch``).
PICTURE_HASH_RE: re.Pattern[str] = re.compile(rf"[0-9a-f]{{{PICTURE_HASH_LEN}}}")


def compute_picture_hash(bytes_webp: bytes) -> str:
    """Return the 16-char SHA-256 hex prefix for cache-busting URLs."""
    return hashlib.sha256(bytes_webp).hexdigest()[:PICTURE_HASH_LEN]


def clean_picture_hash(value: object) -> str | None:
    """``value`` when it is a picture hash in our own format, else ``None``.

    Apply to every peer-supplied hash before it is stored: anything but a
    ``str`` of exactly 16 lowercase hex characters is dropped.
    """
    if isinstance(value, str) and PICTURE_HASH_RE.fullmatch(value):
        return value
    return None
