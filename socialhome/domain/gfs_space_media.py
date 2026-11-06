"""Wire shape of the GFS space media cache — upload authorization.

A household uploads one encrypted space file to a GFS under one of two
**scopes**:

* ``space`` — ``PUT /gfs/spaces/{space_id}/media/{blob_id}``, for a space
  listed on the GFS (public / global, or private but listed for invite
  links);
* ``channel`` — ``PUT /gfs/channels/{channel_id}/media/{blob_id}``, for a
  PRIVATE space reached through its v_51 opaque channel: the GFS learns only
  the random channel id, never the space.

The raw blob is the body, so the authorization rides in ONE header,
:data:`SPACE_MEDIA_AUTH_HEADER`: the base64url of a canonical JSON object.
Every kind carries the claim

    {scope, scope_id, blob_id, size, expires_at, ts}

(``ts`` and ``expires_at`` unix seconds, ``size`` the body's byte count —
checked against the body, which is read only after the signature verified)
plus ``auth`` (the kind), ``sig`` and ``sig_suite``. Each kind reuses what the
GFS already verifies for that scope — no new key, roster or registration:

* scope ``space``:

  * :data:`AUTH_AUTHORITY` — a space-authority signature (any seed holder)
    over the claim under
    :data:`~socialhome.authority_sig.AUTHORITY_EVENT_SPACE_MEDIA_PUT`,
    verified against the space's TOFU-pinned key. Names nobody.
  * :data:`AUTH_WRITER_CERT` (trusted mode, v_49) — ``writer_cert`` (v1
    fields only) + ``epoch``; ``sig`` by the household key the cert names
    over :meth:`SpaceMediaClaim.writer_signing_bytes`. The cert names the
    writer's household key — the trusted-mode trade-off
    ``/gfs/member-publish`` already makes.
  * :data:`AUTH_WRITER_KEY` (strict mode, v_50) — ``epoch``; ``sig`` by the
    space's per-epoch writer GROUP key the GFS pinned. Names nobody.

* scope ``channel`` (the checks ``/gfs/channels/*`` already make):

  * :data:`AUTH_CHANNEL` — ``sig`` by the channel key the GFS pinned at
    registration (what registration, epoch notices and unregister are
    signed with) over :data:`CHANNEL_MEDIA_PUT_PREFIX` + the claim. Names
    nobody.
  * :data:`AUTH_CHANNEL_CERT` (trusted channel) — ``channel_cert`` +
    ``epoch``; ``sig`` by the household key the cert names (as above).
  * :data:`AUTH_WRITER_KEY` (strict channel) — the channel writer key pinned
    for ``epoch``.

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
from .gfs_channel import (
    CHANNEL_CERT_KEYS,
    ChannelCert,
    InvalidChannelWire,
    valid_channel_id,
)
from .writer_cert import MAX_WRITER_CERT_EPOCH, WriterCert

#: The header carrying the upload authorization.
SPACE_MEDIA_AUTH_HEADER: str = "X-SH-Space-Media-Auth"

#: Longest accepted header value (a cert is ~0.4 KB; base64url of the whole
#: object stays well under 1.5 KB).
SPACE_MEDIA_AUTH_MAX_CHARS: int = 4096

#: Key of the cache's entry in the signed ``/gfs/info`` capability block:
#: ``{"max_blob_bytes": int, "ttl_s": int, "space_max_bytes": int,
#: "space_uploads_per_hour": int, "scopes": ["channel", "space"]}``.
#: (The routes are registered in ``global_server/routes/__init__.py``; the
#: fetch, ``/gfs/media/{blob_id}``, names no scope.)
SPACE_MEDIA_CAPABILITY: str = "space_media"

SCOPE_SPACE: str = "space"
SCOPE_CHANNEL: str = "channel"
MEDIA_SCOPES: frozenset[str] = frozenset({SCOPE_SPACE, SCOPE_CHANNEL})

AUTH_AUTHORITY: str = "authority"
AUTH_WRITER_CERT: str = "writer_cert"
AUTH_WRITER_KEY: str = "writer_key"
AUTH_CHANNEL: str = "channel"
AUTH_CHANNEL_CERT: str = "channel_cert"

#: Which kinds each scope takes.
AUTH_KINDS_BY_SCOPE: dict[str, frozenset[str]] = {
    SCOPE_SPACE: frozenset({AUTH_AUTHORITY, AUTH_WRITER_CERT, AUTH_WRITER_KEY}),
    SCOPE_CHANNEL: frozenset({AUTH_CHANNEL, AUTH_CHANNEL_CERT, AUTH_WRITER_KEY}),
}

#: Suite of a cert holder's own signature (its household identity key).
#: Receivers refuse any other value, never a default.
SPACE_MEDIA_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_SPACE_MEDIA_SIG_SUITES: frozenset[str] = frozenset(
    {SPACE_MEDIA_SIG_SUITE_ED25519}
)

#: Domain separation of the writer kinds' signed bytes (the scope is inside).
SPACE_MEDIA_WRITER_PREFIX: bytes = b"space-media-put:v1:"

#: Domain separation of what a channel key signs for the cache (beside the
#: other ``gfs-channel-*:v1:`` prefixes, :mod:`socialhome.gfs_channel`).
CHANNEL_MEDIA_PUT_PREFIX: bytes = b"gfs-channel-media-put:v1:"
CHANNEL_MEDIA_DELETE_PREFIX: bytes = b"gfs-channel-media-delete:v1:"

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
    {"scope", "scope_id", "blob_id", "size", "expires_at", "ts"}
)
_SIGNED: frozenset[str] = _CLAIM_KEYS | {"auth", "sig", "sig_suite"}
_KEYS_BY_KIND: dict[str, frozenset[str]] = {
    AUTH_AUTHORITY: _SIGNED,
    AUTH_CHANNEL: _SIGNED,
    AUTH_WRITER_CERT: _SIGNED | {"epoch", "writer_cert"},
    AUTH_CHANNEL_CERT: _SIGNED | {"epoch", "channel_cert"},
    AUTH_WRITER_KEY: _SIGNED | {"epoch"},
}


class InvalidSpaceMediaAuth(ValueError):
    """A malformed upload authorization header."""


class UnsupportedSpaceMediaSigSuite(ValueError):
    """A cert holder's signature names a suite this build lacks."""


def require_space_media_sig_suite(suite: object) -> str:
    if not isinstance(suite, str) or suite not in SUPPORTED_SPACE_MEDIA_SIG_SUITES:
        raise UnsupportedSpaceMediaSigSuite(f"unsupported suite {suite!r}")
    return suite


def media_delete_payload(scope: str, scope_id: str, blob_id: str, ts: int) -> dict:
    """What a seed holder signs to withdraw a blob — under
    ``AUTHORITY_EVENT_SPACE_MEDIA_DELETE`` (space) or with the channel key
    after :data:`CHANNEL_MEDIA_DELETE_PREFIX` (channel)."""
    return {"scope": scope, "scope_id": scope_id, "blob_id": blob_id, "ts": ts}


def valid_scope_id(scope: object, scope_id: object) -> str:
    """*scope_id* checked for its *scope*, else :class:`InvalidSpaceMediaAuth`."""
    if scope == SCOPE_CHANNEL:
        try:
            return valid_channel_id(scope_id)
        except InvalidChannelWire as exc:
            raise InvalidSpaceMediaAuth("bad scope_id") from exc
    if scope == SCOPE_SPACE:
        return _short_str(scope_id, "scope_id", MAX_SPACE_ID_CHARS)
    raise InvalidSpaceMediaAuth("bad scope")


@dataclass(slots=True, frozen=True)
class SpaceMediaClaim:
    """What every upload signs: which blob, for which space or channel, how
    big, until when, and when it was signed."""

    scope: str
    scope_id: str
    blob_id: str
    size: int
    expires_at: int
    ts: int

    def signing_body(self) -> dict:
        return {
            "scope": self.scope,
            "scope_id": self.scope_id,
            "blob_id": self.blob_id,
            "size": self.size,
            "expires_at": self.expires_at,
            "ts": self.ts,
        }

    def authority_signing_bytes(self) -> bytes:
        """The bytes a space seed holder signs for :data:`AUTH_AUTHORITY`."""
        return authority_signing_bytes(
            event_type=AUTHORITY_EVENT_SPACE_MEDIA_PUT,
            space_id=self.scope_id,
            payload=self.signing_body(),
        )

    def channel_signing_bytes(self) -> bytes:
        """The bytes a channel key holder signs for :data:`AUTH_CHANNEL`."""
        return CHANNEL_MEDIA_PUT_PREFIX + canonical_bytes(self.signing_body())

    def writer_signing_bytes(self, *, kind: str, epoch: int) -> bytes:
        """The bytes a writer signs (household key for the cert kinds,
        writer group key for :data:`AUTH_WRITER_KEY`); the kind and epoch
        are bound in, the scope is in the claim."""
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
    channel_cert: ChannelCert | None = None

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
        if self.channel_cert is not None:
            wire["channel_cert"] = self.channel_cert.to_wire()
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
        scope = obj.get("scope")
        kind = obj.get("auth")
        if (
            scope not in MEDIA_SCOPES
            or kind not in AUTH_KINDS_BY_SCOPE[str(scope)]
            or set(obj) != _KEYS_BY_KIND[str(kind)]
        ):
            raise InvalidSpaceMediaAuth("unexpected fields")
        claim = SpaceMediaClaim(
            scope=str(scope),
            scope_id=valid_scope_id(scope, obj.get("scope_id")),
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
        return cls(
            claim=claim,
            kind=str(kind),
            sig=_short_str(obj.get("sig"), "sig", _MAX_SIG_CHARS),
            sig_suite=_short_str(obj.get("sig_suite"), "sig_suite", _MAX_SUITE_CHARS),
            epoch=epoch,
            writer_cert=_cert(obj["writer_cert"]) if "writer_cert" in obj else None,
            channel_cert=(
                _channel_cert(obj["channel_cert"]) if "channel_cert" in obj else None
            ),
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


def _channel_cert(raw: object) -> ChannelCert:
    if not isinstance(raw, dict) or set(raw) != CHANNEL_CERT_KEYS:
        raise InvalidSpaceMediaAuth("bad channel_cert")
    try:
        return ChannelCert.from_wire(raw)
    except ValueError as exc:
        raise InvalidSpaceMediaAuth("bad channel_cert") from exc
