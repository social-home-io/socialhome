"""Profile pictures: the one way bytes become a stored avatar (§23 profile).

Every profile picture this household stores — a local upload, or a peer's
bytes off ``USER_UPDATED`` / ``USERS_SYNC`` /
``SPACE_MEMBER_PROFILE_UPDATED`` or the §25.6 ``member_pictures`` sync
resource — is decoded and re-encoded here into a square-bounded WebP, and
its hash is our own :func:`compute_picture_hash` of the re-encoded bytes.
A peer's bytes are never stored as-is: re-encoding means a malicious peer
cannot plant arbitrary bytes (an SVG, HTML, a polyglot) in the blob table
that is later served to every member.
"""

from __future__ import annotations

import base64
import binascii
from collections import OrderedDict

from ..domain.picture_hash import compute_picture_hash
from ..domain.space_author_profile import (
    PROFILE_PICTURE_MAX_BYTES,
    PROFILE_PICTURE_MAX_PX,
)
from .image_processor import ImageProcessor

#: Largest side of a stored profile picture (square WebP). 384 px keeps
#: avatars sharp at 3× retina (128 dp) so the UI never has to upscale —
#: covers watch faces and TV apps too. The previous 256 px was a
#: 2018-era target that was already getting upscaled in some surfaces.
PROFILE_PICTURE_MAX_DIMENSION = 384

#: Largest source picture (declared pixels) we decode into a profile
#: picture. Checked on the header before any pixel is decoded, so a
#: decompression bomb from a peer (or an upload) costs a header parse.
#: 40 MP covers every phone camera (≈ 160 MB as RGBA at the worst).
PROFILE_PICTURE_MAX_SOURCE_PIXELS = 40_000_000


async def normalize_profile_picture(raw: bytes) -> tuple[bytes, str]:
    """Re-encode ``raw`` into the stored WebP and return ``(webp, hash)``.

    The decode / resize / encode runs off the event loop
    (:meth:`ImageProcessor.generate_thumbnail`).

    Raises
    ------
    ImageUnreadableError
        (a ``ValueError``) when ``raw`` does not decode as an image —
        unopenable, truncated / corrupt, or over
        :data:`PROFILE_PICTURE_MAX_SOURCE_PIXELS`.
    """
    webp = await ImageProcessor().generate_thumbnail(
        raw,
        size=PROFILE_PICTURE_MAX_DIMENSION,
        max_source_pixels=PROFILE_PICTURE_MAX_SOURCE_PIXELS,
    )
    return webp, compute_picture_hash(webp)


async def reencode_peer_picture(picture_webp_base64: object) -> tuple[bytes, str]:
    """A peer's ``picture_webp_base64`` wire value, re-encoded for storage.

    Raises ``ValueError`` — :class:`ImageUnreadableError` (a ``ValueError``)
    from :func:`normalize_profile_picture`, or a plain ``ValueError`` for a
    non-string / malformed base64 value — for anything that is not base64
    of a decodable image within the pixel cap, including a truncated or
    corrupt one. The caller drops the picture (and logs it, never the
    bytes).
    """
    if not isinstance(picture_webp_base64, str) or not picture_webp_base64:
        raise ValueError("picture is not a base64 string")
    try:
        raw = base64.b64decode(picture_webp_base64)
    except binascii.Error as exc:
        raise ValueError("picture is not valid base64") from exc
    return await normalize_profile_picture(raw)


async def public_profile_picture(raw: bytes) -> bytes | None:
    """``raw`` re-encoded as a small public profile picture — a square WebP
    of at most :data:`~socialhome.domain.space_author_profile.PROFILE_PICTURE_MAX_PX`
    and :data:`~socialhome.domain.space_author_profile.PROFILE_PICTURE_MAX_BYTES`
    — or ``None`` when it does not decode or cannot fit.

    Both ends run it: the sender to fit an author profile item (v_60), the
    receiver so a peer's bytes are never stored as-is (the same rule as
    :func:`reencode_peer_picture`).
    """
    for size in (PROFILE_PICTURE_MAX_PX, 192, 128):
        try:
            webp = await ImageProcessor().generate_thumbnail(
                raw, size=size, max_source_pixels=PROFILE_PICTURE_MAX_SOURCE_PIXELS
            )
        except ValueError:
            return None
        if len(webp) <= PROFILE_PICTURE_MAX_BYTES:
            return webp
    return None


#: How many source pictures :class:`PublicPictureCache` remembers by
#: default. That is at most ~24 KB each, so ~6 MB in all.
PUBLIC_PICTURE_CACHE_ENTRIES: int = 256


class PublicPictureCache:
    """Remembers :func:`public_profile_picture` per SOURCE picture hash.

    The re-encode can take up to three decode + encode passes, and the same
    picture used to be re-encoded on every public highlight view and every
    author-profile publish. Keyed by the stored source picture's hash
    (``profile_picture_repo`` returns it beside the bytes), so a new picture
    is a new key and nothing goes stale. A miss (``None``: unusable or too
    big to fit) is remembered too. Bounded LRU. The CPU work itself stays in
    :func:`public_profile_picture`, which runs it off the event loop.
    """

    __slots__ = ("_entries", "_max")

    def __init__(self, *, max_entries: int = PUBLIC_PICTURE_CACHE_ENTRIES) -> None:
        self._max = max(1, max_entries)
        self._entries: OrderedDict[str, bytes | None] = OrderedDict()

    async def get(self, source_hash: str, raw: bytes) -> bytes | None:
        """The public picture of ``raw``, whose stored hash is
        ``source_hash``, re-encoding it only the first time."""
        if source_hash in self._entries:
            self._entries.move_to_end(source_hash)
            return self._entries[source_hash]
        public = await public_profile_picture(raw)
        self._entries[source_hash] = public
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
        return public

    def __len__(self) -> int:
        return len(self._entries)
