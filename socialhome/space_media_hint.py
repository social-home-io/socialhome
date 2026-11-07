"""The sealed locator a reader sends with a "media wanted" (v_83).

When a GFS no longer holds a space blob (past its TTL, or evicted), a reader
asks that GFS for it (``POST /gfs/media/{blob_id}/wanted``) and the GFS asks
a household that holds the file to re-upload or stream the same
deterministic ciphertext (:mod:`socialhome.space_media_seal`). That
household needs to find the file — the blob id alone is a hash — so the
reader seals what its descriptor says about it (item, sealing household,
the author's file name) for the household, under a key only space key
holders derive. The GFS forwards the hint verbatim and can't read it.

* **Key.** ``HKDF-SHA256(the epoch's space media key,
  info="sh-space-media-hint:v1")`` — a sibling of the media key, never the
  key itself.
* **Nonce.** Random (12 bytes): two readers asking for one file send
  unlinkable hints, and the hint names no reader.
* **AAD.** ``suite | scope | scope_id | blob_id | epoch`` — a hint can't be
  moved onto another blob or scope.
* **Suite.** :data:`HINT_SUITE_AESGCM256` rides every hint; an unknown one is
  refused (:class:`UnsupportedSpaceMediaHintSuite`), never defaulted.
* **When.** The sealed locator carries when the reader sealed it
  (``issued_at``); a holder refuses one older than
  :data:`HINT_MAX_AGE_S`, so a GFS that kept a hint can't replay it later
  to see which households answer.

A hint proves nothing: any key holder can seal one naming any file. The
holder therefore acts only when the file it finds is the space's own content
(``confined_to_space``, no reserved name) AND re-sealing it gives exactly the
blob id the GFS asked for — which a reader can know only from a real
descriptor of that very file.

Pure — no I/O.
"""

from __future__ import annotations

import base64
import binascii
import os
import time
from dataclasses import dataclass

import orjson
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

#: The one hint suite: AES-256-GCM, random nonce.
HINT_SUITE_AESGCM256: str = "smh1-aesgcm-256"
SUPPORTED_HINT_SUITES: frozenset[str] = frozenset({HINT_SUITE_AESGCM256})

#: Longest base64url ciphertext a hint may carry (a locator is three short
#: strings: well under 1 KiB sealed).
MAX_HINT_CHARS: int = 1400

#: Longest locator field (the descriptor's own bounds are tighter).
MAX_LOCATOR_FIELD_CHARS: int = 200

#: How old (or how far ahead) a hint may be when a holder opens it.
HINT_MAX_AGE_S: int = 600

_KEY_INFO = b"sh-space-media-hint:v1"
_AAD_PREFIX = b"socialhome/space-media-hint/v1|"
_NONCE_BYTES = 12
_MAX_EPOCH = 2**53


class InvalidSpaceMediaHint(ValueError):
    """A hint that is not one (shape, encoding, or what it opens to)."""


class UnsupportedSpaceMediaHintSuite(InvalidSpaceMediaHint):
    """A hint names a suite this build lacks."""


@dataclass(slots=True, frozen=True)
class SpaceMediaLocator:
    """What finds a space file on a holder: the item that shows it, the
    household that sealed it and the file name that household used — the
    descriptor's fields, sealed for the holder."""

    item_id: str
    origin: str
    filename: str
    #: Unix seconds the reader sealed it (set by :func:`seal_space_media_hint`).
    issued_at: int = 0

    def fresh(self, now: float | None = None) -> bool:
        """Whether it was sealed within :data:`HINT_MAX_AGE_S` of *now*."""
        now = time.time() if now is None else now
        return abs(now - self.issued_at) <= HINT_MAX_AGE_S


@dataclass(slots=True, frozen=True)
class SpaceMediaHint:
    """A parsed hint, still sealed: the epoch says which key opens it."""

    suite: str
    epoch: int
    ct: bytes

    def to_wire(self) -> dict[str, object]:
        return {
            "suite": self.suite,
            "epoch": self.epoch,
            "ct": _b64(self.ct),
        }


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.b64decode(text + pad, altchars=b"-_", validate=True)


def _hint_key(media_key: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_INFO).derive(
        media_key
    )


def _aad(scope: str, scope_id: str, blob_id: str, epoch: int) -> bytes:
    return (
        _AAD_PREFIX
        + f"{HINT_SUITE_AESGCM256}|{scope}|{scope_id}|{blob_id}|{epoch}".encode()
    )


def _seal_raw(
    media_key: bytes,
    plaintext: bytes,
    *,
    scope: str,
    scope_id: str,
    blob_id: str,
    epoch: int,
) -> dict[str, object]:
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(_hint_key(media_key)).encrypt(
        nonce, plaintext, _aad(scope, scope_id, blob_id, epoch)
    )
    return SpaceMediaHint(
        suite=HINT_SUITE_AESGCM256, epoch=epoch, ct=nonce + ct
    ).to_wire()


def seal_space_media_hint(
    *,
    media_key: bytes,
    scope: str,
    scope_id: str,
    blob_id: str,
    epoch: int,
    locator: SpaceMediaLocator,
) -> dict[str, object]:
    """The wire hint ``{suite, epoch, ct}`` for *locator*, under the media
    key of *epoch*, bound to the blob and its scope."""
    plaintext = orjson.dumps(
        {
            "i": locator.item_id,
            "o": locator.origin,
            "f": locator.filename,
            "t": locator.issued_at or int(time.time()),
        }
    )
    return _seal_raw(
        media_key,
        plaintext,
        scope=scope,
        scope_id=scope_id,
        blob_id=blob_id,
        epoch=epoch,
    )


def parse_hint_wire(raw: object) -> SpaceMediaHint:
    """The hint in *raw* — :class:`InvalidSpaceMediaHint` (or its
    :class:`UnsupportedSpaceMediaHintSuite`) when it is not one."""
    if not isinstance(raw, dict):
        raise InvalidSpaceMediaHint("hint is not an object")
    suite = raw.get("suite")
    if suite not in SUPPORTED_HINT_SUITES:
        raise UnsupportedSpaceMediaHintSuite(f"unsupported hint suite {suite!r}")
    epoch = raw.get("epoch")
    if (
        not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or not 0 <= epoch < _MAX_EPOCH
    ):
        raise InvalidSpaceMediaHint("bad epoch")
    ct = raw.get("ct")
    if not isinstance(ct, str) or not 0 < len(ct) <= MAX_HINT_CHARS:
        raise InvalidSpaceMediaHint("bad ciphertext")
    try:
        data = _unb64(ct)
    except (binascii.Error, ValueError) as exc:
        raise InvalidSpaceMediaHint("bad ciphertext encoding") from exc
    if len(data) <= _NONCE_BYTES + 16:
        raise InvalidSpaceMediaHint("ciphertext too short")
    return SpaceMediaHint(suite=str(suite), epoch=epoch, ct=data)


def open_space_media_hint(
    hint: SpaceMediaHint,
    *,
    media_key: bytes,
    scope: str,
    scope_id: str,
    blob_id: str,
) -> SpaceMediaLocator:
    """The locator inside *hint*. :class:`cryptography.exceptions.InvalidTag`
    for another key, scope or blob; :class:`InvalidSpaceMediaHint` for a
    plaintext that names no usable locator."""
    nonce, ct = hint.ct[:_NONCE_BYTES], hint.ct[_NONCE_BYTES:]
    plaintext = AESGCM(_hint_key(media_key)).decrypt(
        nonce, ct, _aad(scope, scope_id, blob_id, hint.epoch)
    )
    try:
        body = orjson.loads(plaintext)
    except orjson.JSONDecodeError as exc:
        raise InvalidSpaceMediaHint("hint plaintext is not JSON") from exc
    if not isinstance(body, dict):
        raise InvalidSpaceMediaHint("hint plaintext is not an object")
    fields: list[str] = []
    for key in ("i", "o", "f"):
        value = body.get(key)
        if not isinstance(value, str) or not 0 < len(value) <= MAX_LOCATOR_FIELD_CHARS:
            raise InvalidSpaceMediaHint("hint names no locator")
        fields.append(value)
    issued = body.get("t")
    if not isinstance(issued, int) or isinstance(issued, bool) or issued < 0:
        raise InvalidSpaceMediaHint("hint carries no time")
    return SpaceMediaLocator(
        item_id=fields[0], origin=fields[1], filename=fields[2], issued_at=issued
    )


__all__ = [
    "HINT_SUITE_AESGCM256",
    "MAX_HINT_CHARS",
    "SUPPORTED_HINT_SUITES",
    "InvalidSpaceMediaHint",
    "SpaceMediaHint",
    "SpaceMediaLocator",
    "UnsupportedSpaceMediaHintSuite",
    "open_space_media_hint",
    "parse_hint_wire",
    "seal_space_media_hint",
]
