"""HMAC-SHA256 signed URLs for browser-loaded media (§23.21).

The SPA loads images, video, and downloads via raw browser primitives
(``<img src>``, ``<video src>``, ``<a href download>``) which cannot
carry an ``Authorization: Bearer`` header. Routing those URLs through
a query-string master token leaks the user's full account on
right-click → "Copy image address". This module signs each media URL
with a short-lived HMAC so a leaked URL only authorises **one
resource for one hour**.

Scheme: HMAC-SHA256 over ``"<path>|<exp>"`` where ``<path>`` is the
canonical request path (no query string) and ``<exp>`` is the Unix
expiry timestamp in seconds. The output URL is
``<path>?exp=<exp>&sig=<urlsafe-b64(hmac)>`` (existing query params
like ``?v=<hash>`` on picture URLs are preserved as cache busters —
they're outside the signed envelope).

The HMAC key is derived from the instance identity seed via
HKDF-Expand with a fixed domain-separation prefix so it never reuses
the federation Ed25519 key material directly. See ``app.py``
``_on_startup`` for wiring.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

#: Domain-separation tag for HKDF — bumping the version invalidates
#: all signed URLs in flight without rotating the identity seed.
_HKDF_INFO: bytes = b"sh-media-url-v1"
_HKDF_LEN: int = 32

#: Default URL lifetime. One hour is long enough for typical browse
#: sessions, short enough that a leaked URL dies fast.
DEFAULT_TTL_SECONDS: int = 3600


def derive_signing_key(identity_seed: bytes) -> bytes:
    """Derive the media-URL HMAC key from the instance identity seed.

    Domain-separated via HKDF-Expand so the derived key is
    cryptographically independent of the federation Ed25519 key. See
    §25.7 (key isolation).
    """
    if not identity_seed:
        raise ValueError("identity_seed must be non-empty")
    hkdf = HKDFExpand(algorithm=hashes.SHA256(), length=_HKDF_LEN, info=_HKDF_INFO)
    return hkdf.derive(identity_seed)


class MediaUrlSigner:
    """Sign + verify short-lived URLs.

    Stateless aside from the pre-derived HMAC key. Cheap to call on
    every serialization — the bottleneck is JSON encode/decode, not
    the HMAC.
    """

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if len(key) < 16:
            raise ValueError("HMAC key must be at least 16 bytes")
        self._key = key

    def sign(
        self, path: str, *, ttl: int = DEFAULT_TTL_SECONDS, now: int | None = None
    ) -> str:
        """Return ``path`` with ``?exp=<ts>&sig=<b64>`` appended.

        Existing query params are preserved. ``path`` may already carry
        a query string (e.g. ``/api/users/{id}/picture?v=<hash>``); the
        signature covers only the path portion before ``?``.
        """
        if now is None:
            now = int(time.time())
        exp = now + int(ttl)
        canonical = path.split("?", 1)[0]
        sig = self._compute(canonical, exp)
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}exp={exp}&sig={sig}"

    def verify(self, path: str, exp: str, sig: str, *, now: int | None = None) -> bool:
        """Validate a signed URL.

        ``path`` is the request's canonical path (no query string).
        ``exp`` and ``sig`` come from the request's query string.
        Constant-time comparison via ``hmac.compare_digest``.
        """
        if not exp or not sig:
            return False
        try:
            exp_int = int(exp)
        except ValueError:
            # ``exp`` already passed the ``not exp`` truthiness check, so
            # it can't be ``None`` here — only the malformed-int branch
            # remains, which raises ``ValueError`` (never ``TypeError``).
            return False
        if now is None:
            now = int(time.time())
        if exp_int < now:
            return False
        expected = self._compute(path, exp_int)
        return hmac.compare_digest(expected, sig)

    def _compute(self, canonical_path: str, exp: int) -> str:
        message = f"{canonical_path}|{exp}".encode("utf-8")
        digest = hmac.new(self._key, message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


#: Field names whose **string** values get signed when present in a
#: serialised payload. Each is checked against a ``/api/`` prefix so
#: external (HA-served, federation, link-preview) URLs are left alone.
#:
#: ``url`` is intentionally excluded — it's too generic and would
#: over-sign payloads like the calendar-ICS feed URL, which carries
#: its own ``?token=``. Routes that genuinely return a media-shaped
#: ``url`` (gallery items, task attachments) opt in via
#: :func:`sign_media_urls_in`'s ``extra_fields`` kwarg.
_SIGNABLE_URL_FIELDS: frozenset[str] = frozenset(
    {"media_url", "picture_url", "cover_url", "thumbnail_url"},
)
#: Field names whose **list-of-string** values get signed (e.g. bazaar
#: ``image_urls``). Each entry is signed independently.
_SIGNABLE_URL_LIST_FIELDS: frozenset[str] = frozenset({"image_urls"})


def strip_signature_query(url):
    """Drop ``?exp=…&sig=…`` (and any other query) from a client-
    supplied media URL before it reaches the service / storage layer.

    The composer's preview ``<img>`` consumes a signed URL we minted at
    upload time; if the SPA echoes that signed form back into a
    ``media_url`` field on post-create, we don't want to persist
    short-lived auth fragments in the post row. The server signs fresh
    on every read.
    """
    if not isinstance(url, str) or "?" not in url:
        return url
    return url.split("?", 1)[0]


def sign_media_urls_in(
    payload, signer: MediaUrlSigner, *, extra_fields: tuple[str, ...] = ()
):
    """Recursively walk ``payload``, signing media-shaped URLs in place.

    Signs:

    * String values under :data:`_SIGNABLE_URL_FIELDS`
      (``media_url``, ``picture_url``, ``cover_url``, ``thumbnail_url``).
    * List-of-string values under :data:`_SIGNABLE_URL_LIST_FIELDS`
      (``image_urls``) — each entry independently.
    * String values under any name in ``extra_fields`` — used by callers
      that expose a media URL on a generically-named field like
      ``url`` (gallery items, task attachments) and want it signed
      without polluting the global field set.

    Skips absolute URLs (``http://`` / ``https://``) and falsy values
    so HA-served avatars or federation URLs don't get corrupted.
    """
    fields = _SIGNABLE_URL_FIELDS | frozenset(extra_fields)
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k in fields and isinstance(v, str) and v.startswith("/api/"):
                payload[k] = signer.sign(v)
            elif k in _SIGNABLE_URL_LIST_FIELDS and isinstance(v, list):
                payload[k] = [
                    signer.sign(u)
                    if isinstance(u, str) and u.startswith("/api/")
                    else u
                    for u in v
                ]
            else:
                sign_media_urls_in(v, signer, extra_fields=extra_fields)
    elif isinstance(payload, list):
        for item in payload:
            sign_media_urls_in(item, signer, extra_fields=extra_fields)
    return payload
