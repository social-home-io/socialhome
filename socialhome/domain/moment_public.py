"""Domain types for the public-Momentum-via-GFS pillar (§Momentum-public).

The Momentum pillar (``socialhome/domain/moment.py``) federates
moments only across paired households (3-hop relay, max). This module
adds an opt-in *public* layer brokered by a GFS:

* A user **registers** themself on one or more GFSes
  (:class:`MomentPublicRegistration`).
* Other instances discover registered users via the GFS directory,
  then their users **follow** specific authors
  (:class:`MomentPublicFollow`).
* When a registered author posts, the moment fans out (a) via the
  existing household federation AND (b) as a signed
  :class:`MomentPublicEnvelope` over the persistent SH↔GFS WebSocket.
  The GFS pushes the same envelope to every follower instance.
* Recipients verify the envelope's Ed25519 signature against the
  ``followed_instance_pk`` they cached at follow-time, persist the
  moment with ``received_via='gfs'``, and — critically — **do not
  re-relay** it to their own paired peers.

The encryption-first rule (§25.8.21) is preserved by the GFS holding
plaintext only in memory during fan-out (never on disk) and by the
recipient's signature check ensuring authorship can't be forged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

from .picture_hash import PICTURE_HASH_LEN, clean_picture_hash

#: Longest bio a GFS public directory accepts (``/gfs/moments/users/register``
#: refuses more). An author's bio rides the signed envelope only when the
#: directory could hold it, and a follower keeps nothing longer.
PUBLIC_BIO_MAX_LENGTH = 280

#: Longest public display name kept for a followed author — the household
#: cap (``UserService.DISPLAY_NAME_MAX_LENGTH``).
PUBLIC_NAME_MAX_LENGTH = 64

#: A GFS mirrors a picture under its full SHA-256 hex digest; a household
#: names the same bytes by the 16-char prefix (:mod:`.picture_hash`).
_FULL_DIGEST_RE = re.compile(r"[0-9a-f]{64}")

#: Ids put verbatim into a signed proxy path. The signature covers the
#: decoded ``request.path``, so an id that would need percent-encoding (or
#: a ``..`` segment) gets no picture rather than a URL that fails to verify
#: or resolves somewhere else.
_PATH_SAFE_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._:-]*$")


def public_author_name(value: object, *, user_id: str) -> str | None:
    """A followed author's public name, or ``None`` when there is none.

    A GFS directory falls back to the bare user id for an author with no
    name; an id is never a name, so it — like blank or overlong text — is
    unknown here and the SPA says "Someone".
    """
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or name == user_id or len(name) > PUBLIC_NAME_MAX_LENGTH:
        return None
    return name


def public_author_bio(value: object) -> str | None:
    """A followed author's public bio, or ``None`` (blank / not text /
    longer than a directory holds)."""
    if not isinstance(value, str):
        return None
    bio = value.strip()
    if not bio or len(bio) > PUBLIC_BIO_MAX_LENGTH:
        return None
    return bio


def public_picture_digest(value: object) -> str | None:
    """The cache-busting digest of a GFS-mirrored picture, in the
    household's 16-char form — or ``None`` for anything else.

    Accepts the author's own picture hash (16 hex) and the GFS directory's
    full SHA-256 (64 hex) of the same bytes; a hostile value never reaches
    storage or a URL.
    """
    if isinstance(value, str) and _FULL_DIGEST_RE.fullmatch(value):
        value = value[:PICTURE_HASH_LEN]
    return clean_picture_hash(value)


def gfs_picture_proxy_url(gfs_id: str, user_id: str, digest: str | None) -> str | None:
    """Our own proxy URL for a GFS-mirrored public picture, unsigned.

    Points at ``GET /api/gfs/{gfs_id}/moments/users/{user_id}/picture`` —
    the household fetches the bytes from the GFS it is already connected
    to, so the browser never learns the GFS host. ``None`` without a
    mirrored picture or for an id that is not path-safe.
    """
    if not (isinstance(digest, str) and digest):
        return None
    if not (_PATH_SAFE_ID.match(gfs_id) and _PATH_SAFE_ID.match(user_id)):
        return None
    return (
        f"api/gfs/{gfs_id}/moments/users/{user_id}/picture?v={quote(digest, safe='')}"
    )


@dataclass(slots=True, frozen=True)
class MomentPublicRegistration:
    """Author-side row: which GFS this user has opted into.

    One row per ``(user_id, gfs_id)``. ``default_share`` lets the
    author flip whether *every* moment is public or whether they want
    to opt in per-moment in the composer.
    """

    user_id: str
    gfs_id: str
    registered_at: str
    default_share: bool = True
    #: Avatar digest last successfully pushed to this GFS. The
    #: profile-sync flow compares against ``users.picture_hash`` and
    #: skips the picture upload when they match.
    last_picture_digest: str | None = None


@dataclass(slots=True, frozen=True)
class MomentPublicFollow:
    """Follower-side row: who this user follows publicly via which GFS.

    Caches the followed user's home-instance public key so every
    inbound public-moment envelope can be Ed25519-verified locally —
    no round-trip back to the GFS, no second source-of-truth.
    """

    follower_user_id: str
    followed_user_id: str
    gfs_id: str
    followed_instance_pk: str
    followed_username: str
    followed_display_name: str
    created_at: str
    #: The author's public bio and the digest of their GFS-mirrored
    #: picture — from the directory at follow time, then refreshed from
    #: every signed envelope. ``None`` when unknown.
    followed_bio: str | None = None
    followed_picture_digest: str | None = None

    @property
    def public_name(self) -> str | None:
        """The author's name as followers may show it, never their id."""
        return public_author_name(
            self.followed_display_name, user_id=self.followed_user_id
        ) or public_author_name(self.followed_username, user_id=self.followed_user_id)


@dataclass(slots=True, frozen=True)
class MomentPublicEnvelope:
    """Signed wire envelope for a public-moment fan-out frame.

    Sent by the author's instance over its persistent WS to each GFS
    on which the author is registered, and forwarded verbatim by the
    GFS to every follower instance. The recipient validates
    ``signature`` over the canonical-JSON of every other field using
    the cached ``followed_instance_pk``.
    """

    moment_id: str
    author_user_id: str
    author_username: str
    author_display_name: str
    content: str
    media_url: str | None
    media_type: str | None
    duration_ms: int | None
    parent_moment_id: str | None
    origin_instance_id: str
    created_at: str
    expires_at: str
    #: Hex-encoded Ed25519 over the canonical-JSON of all other fields.
    signature: str
