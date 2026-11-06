"""Wire shape of the GFS space media cache — upload authorization.

A household uploads one encrypted space file to a GFS with
``PUT /gfs/spaces/{space_id}/media/{blob_id}``; the raw blob is the body, so
the authorization rides in ONE header, :data:`SPACE_MEDIA_AUTH_HEADER`: the
base64url of a canonical JSON object. Every kind carries the claim

    {space_id, blob_id, size, expires_at, ts}

(``ts`` and ``expires_at`` unix seconds, ``size`` the body's byte count —
checked against the body, which is read only after the signature verified)
plus ``auth`` (the kind), ``sig`` and ``sig_suite``. The kinds reuse what the
GFS already verifies for ``/gfs/publish`` and the member publishes — no new
key, roster or registration:

* :data:`AUTH_AUTHORITY` — a space-authority signature (any seed holder:
  owner or delegated admin) over the claim under
  :data:`~socialhome.authority_sig.AUTHORITY_EVENT_SPACE_MEDIA_PUT`,
  verified against the space's TOFU-pinned key. Names nobody.
* :data:`AUTH_WRITER_CERT` (trusted-mode spaces, v_49) — ``writer_cert``
  (its v1 fields only) + ``epoch``, and ``sig`` made with the household key
  the cert names over :meth:`SpaceMediaClaim.writer_signing_bytes`. No
  instance id is sent, but the cert names the writer's household key: the
  GFS can link the upload to that household, the trusted-mode trade-off
  ``/gfs/member-publish`` already makes.
* :data:`AUTH_WRITER_KEY` (strict-mode spaces, v_50) — ``epoch`` and
  ``sig`` made with the space's per-epoch writer GROUP key, which the GFS
  pinned from the authority's ``writer_key_cert``. Names nobody.

Pure: no I/O, so the GFS (verifier) and the household (signer) share it.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

import orjson

from ..authority_sig import AUTHORITY_EVENT_SPACE_MEDIA_PUT, authority_signing_bytes
from ..canonical_json import canonical_bytes
from ..moment_public_media import MAX_BLOB_BYTES, is_blob_id
from .writer_cert import MAX_WRITER_CERT_EPOCH, WriterCert

#: The header carrying the upload authorization.
SPACE_MEDIA_AUTH_HEADER: str = "X-SH-Space-Media-Auth"

#: Longest accepted header value (a writer cert is ~0.4 KB; base64url of
#: the whole object stays well under 1.5 KB).
SPACE_MEDIA_AUTH_MAX_CHARS: int = 4096

#: Routes (aiohttp patterns).
SPACE_MEDIA_UPLOAD_ROUTE: str = "/gfs/spaces/{space_id}/media/{blob_id}"
SPACE_MEDIA_DELETE_ROUTE: str = "/gfs/spaces/{space_id}/media/{blob_id}/delete"
SPACE_MEDIA_FETCH_ROUTE: str = "/gfs/spaces/media/{blob_id}"

#: Key of the cache's entry in the signed ``/gfs/info`` capability block:
#: ``{"max_blob_bytes": int, "ttl_s": int}``.
SPACE_MEDIA_CAPABILITY: str = "space_media"

AUTH_AUTHORITY: str = "authority"
AUTH_WRITER_CERT: str = "writer_cert"
AUTH_WRITER_KEY: str = "writer_key"
SPACE_MEDIA_AUTH_KINDS: frozenset[str] = frozenset(
    {AUTH_AUTHORITY, AUTH_WRITER_CERT, AUTH_WRITER_KEY}
)

#: Suite of a writer-cert holder's own signature (its household identity
#: key). Receivers refuse any other value, never a default.
SPACE_MEDIA_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_SPACE_MEDIA_SIG_SUITES: frozenset[str] = frozenset(
    {SPACE_MEDIA_SIG_SUITE_ED25519}
)

#: Domain separation of the writer kinds' signed bytes.
SPACE_MEDIA_WRITER_PREFIX: bytes = b"space-media-put:v1:"

#: Longest space id accepted (the member-publish bound).
MAX_SPACE_ID_CHARS: int = 128

_MAX_SIG_CHARS: int = 512
_MAX_SUITE_CHARS: int = 64
_MAX_UNIX: int = 2**53

#: The v1 writer cert fields — the only ones that travel to a GFS.
_CERT_KEYS: frozenset[str] = frozenset(
    {
        "cert_suite",
        "space_id",
        "epoch",
        "instance_pk",
        "scope",
        "issued_at",
        "cert_sig",
    }
)
_CLAIM_KEYS: frozenset[str] = frozenset(
    {"space_id", "blob_id", "size", "expires_at", "ts"}
)
_KEYS_BY_KIND: dict[str, frozenset[str]] = {
    AUTH_AUTHORITY: _CLAIM_KEYS | {"auth", "sig", "sig_suite"},
    AUTH_WRITER_CERT: _CLAIM_KEYS
    | {"auth", "sig", "sig_suite", "epoch", "writer_cert"},
    AUTH_WRITER_KEY: _CLAIM_KEYS | {"auth", "sig", "sig_suite", "epoch"},
}


class InvalidSpaceMediaAuth(ValueError):
    """A malformed upload authorization header."""


class UnsupportedSpaceMediaSigSuite(ValueError):
    """A writer-cert holder's signature names a suite this build lacks."""


def require_space_media_sig_suite(suite: object) -> str:
    if not isinstance(suite, str) or suite not in SUPPORTED_SPACE_MEDIA_SIG_SUITES:
        raise UnsupportedSpaceMediaSigSuite(f"unsupported suite {suite!r}")
    return suite


def space_media_delete_payload(space_id: str, blob_id: str, ts: int) -> dict:
    """What a seed holder signs (under ``AUTHORITY_EVENT_SPACE_MEDIA_DELETE``)
    to withdraw a blob."""
    return {"space_id": space_id, "blob_id": blob_id, "ts": ts}


@dataclass(slots=True, frozen=True)
class SpaceMediaClaim:
    """What every upload signs: which blob, for which space, how big, until
    when, and when it was signed."""

    space_id: str
    blob_id: str
    size: int
    expires_at: int
    ts: int

    def signing_body(self) -> dict:
        return {
            "space_id": self.space_id,
            "blob_id": self.blob_id,
            "size": self.size,
            "expires_at": self.expires_at,
            "ts": self.ts,
        }

    def authority_signing_bytes(self) -> bytes:
        """The bytes a seed holder signs for :data:`AUTH_AUTHORITY`."""
        return authority_signing_bytes(
            event_type=AUTHORITY_EVENT_SPACE_MEDIA_PUT,
            space_id=self.space_id,
            payload=self.signing_body(),
        )

    def writer_signing_bytes(self, *, kind: str, epoch: int) -> bytes:
        """The bytes a writer signs (household key for
        :data:`AUTH_WRITER_CERT`, writer group key for
        :data:`AUTH_WRITER_KEY`); the kind and epoch are bound in."""
        return SPACE_MEDIA_WRITER_PREFIX + canonical_bytes(
            {**self.signing_body(), "auth": kind, "epoch": epoch}
        )


@dataclass(slots=True, frozen=True)
class SpaceMediaUploadAuth:
    """One parsed :data:`SPACE_MEDIA_AUTH_HEADER`."""

    claim: SpaceMediaClaim
    kind: str
    sig: str
    sig_suite: str
    epoch: int | None = None
    writer_cert: WriterCert | None = None

    def to_wire(self) -> dict:
        wire: dict = {
            **self.claim.signing_body(),
            "auth": self.kind,
            "sig": self.sig,
            "sig_suite": self.sig_suite,
        }
        if self.epoch is not None:
            wire["epoch"] = self.epoch
        if self.writer_cert is not None:
            wire["writer_cert"] = self.writer_cert.v1().to_wire()
        return wire

    def to_header(self) -> str:
        return (
            base64.urlsafe_b64encode(canonical_bytes(self.to_wire()))
            .decode()
            .rstrip("=")
        )

    @classmethod
    def from_header(cls, raw: str) -> SpaceMediaUploadAuth:
        """Parse a header value; :class:`InvalidSpaceMediaAuth` on anything
        malformed. Verifies nothing — that is the GFS's job."""
        if not isinstance(raw, str) or not raw or len(raw) > SPACE_MEDIA_AUTH_MAX_CHARS:
            raise InvalidSpaceMediaAuth("bad header")
        try:
            obj = orjson.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        except (ValueError, binascii.Error) as exc:
            raise InvalidSpaceMediaAuth("header is not base64url JSON") from exc
        if not isinstance(obj, dict):
            raise InvalidSpaceMediaAuth("header is not an object")
        kind = obj.get("auth")
        if kind not in SPACE_MEDIA_AUTH_KINDS or set(obj) != _KEYS_BY_KIND[kind]:
            raise InvalidSpaceMediaAuth("unexpected fields")
        claim = SpaceMediaClaim(
            space_id=_space_id(obj.get("space_id")),
            blob_id=_blob_id(obj.get("blob_id")),
            size=_bounded_int(obj.get("size"), "size", low=1, high=MAX_BLOB_BYTES),
            expires_at=_bounded_int(obj.get("expires_at"), "expires_at"),
            ts=_bounded_int(obj.get("ts"), "ts"),
        )
        epoch = (
            _bounded_int(obj["epoch"], "epoch", high=MAX_WRITER_CERT_EPOCH)
            if "epoch" in obj
            else None
        )
        cert = _cert(obj["writer_cert"]) if "writer_cert" in obj else None
        return cls(
            claim=claim,
            kind=str(kind),
            sig=_short_str(obj.get("sig"), "sig", _MAX_SIG_CHARS),
            sig_suite=_short_str(obj.get("sig_suite"), "sig_suite", _MAX_SUITE_CHARS),
            epoch=epoch,
            writer_cert=cert,
        )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bounded_int(
    value: object, name: str, *, low: int = 0, high: int = _MAX_UNIX
) -> int:
    if not _is_int(value) or not low <= value <= high:  # type: ignore[operator]
        raise InvalidSpaceMediaAuth(f"bad {name}")
    return value  # type: ignore[return-value]


def _short_str(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        raise InvalidSpaceMediaAuth(f"bad {name}")
    return value


def _space_id(value: object) -> str:
    return _short_str(value, "space_id", MAX_SPACE_ID_CHARS)


def _blob_id(value: object) -> str:
    if not is_blob_id(value):
        raise InvalidSpaceMediaAuth("bad blob_id")
    return str(value)


def _cert(raw: object) -> WriterCert:
    if not isinstance(raw, dict) or set(raw) != _CERT_KEYS:
        raise InvalidSpaceMediaAuth("bad writer_cert")
    try:
        return WriterCert.from_wire(raw)
    except ValueError as exc:
        raise InvalidSpaceMediaAuth("bad writer_cert") from exc
