"""Wire shape of the GFS space media cache — upload authorization.

A household uploads one encrypted space file to a GFS — ``PUT
/gfs/media/{blob_id}``, the one scope-free path — under one of two
**scopes**, named only inside the signed claim (never in the URL):

* ``space`` — a space listed on the GFS (public / global, or private but
  listed for invite links);
* ``channel`` — a PRIVATE space reached through its v_51 opaque channel: the
  GFS learns only the random channel id, never the space.

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
  * :data:`AUTH_WRITER_KEY` — ``epoch``; ``sig`` by the space's per-epoch
    writer GROUP key the GFS pinned, over
    :meth:`SpaceMediaClaim.writer_signing_bytes`. Names nobody.

* scope ``channel`` (the checks ``/gfs/channels/*`` already make):

  * :data:`AUTH_CHANNEL` — ``sig`` by the channel key the GFS pinned at
    registration (what registration, epoch notices and unregister are
    signed with) over :data:`CHANNEL_MEDIA_PUT_PREFIX` + the claim. Names
    nobody.
  * :data:`AUTH_WRITER_KEY` — the channel writer key pinned for ``epoch``.

No kind names a household (the identified writer-cert kinds went with the
identified member publish, v_90).

**The address is bound to the scope** (security review v2, N1): a blob's
id is :func:`space_media_address` — ``sha256("sm1|" + scope_kind + "|" +
scope_id + "|" + ciphertext)`` — not the bare SHA-256 of the bytes, so the
same ciphertext has a different address in every scope. The GFS recomputes
it on upload from the claim's scope and refuses a mismatch; a reader checks
it with the scope id it knows. So a blob belongs to exactly ONE scope:
nobody who fetched a space's ciphertext can store it under their own scope
at that space's address — no cross-scope claims, no first-storer
ownership. The construction is suite-tagged (:data:`ADDR_SUITE_SM1`, the
claim's ``addr_suite``); receivers refuse an unknown suite.

Pure: no I/O, so the GFS (verifier) and the household (signer) share it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass

import orjson

from ..authority_sig import AUTHORITY_EVENT_SPACE_MEDIA_PUT, authority_signing_bytes
from ..canonical_json import canonical_bytes
from ..moment_public_media import MAX_BLOB_BYTES, is_blob_id
from .gfs_channel import InvalidChannelWire, valid_channel_id
from .writer_cert import MAX_WRITER_CERT_EPOCH

#: The header carrying the upload authorization.
SPACE_MEDIA_AUTH_HEADER: str = "X-SH-Space-Media-Auth"

#: Longest accepted header value (base64url of the whole object stays well
#: under 1 KB).
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
AUTH_WRITER_KEY: str = "writer_key"
AUTH_CHANNEL: str = "channel"

#: Which kinds each scope takes.
AUTH_KINDS_BY_SCOPE: dict[str, frozenset[str]] = {
    SCOPE_SPACE: frozenset({AUTH_AUTHORITY, AUTH_WRITER_KEY}),
    SCOPE_CHANNEL: frozenset({AUTH_CHANNEL, AUTH_WRITER_KEY}),
}

#: The scope-bound address construction, version-tagged so it can evolve:
#: ``sha256(b"sm1|" + scope_kind + b"|" + scope_id + b"|" + ciphertext)``,
#: lowercase hex. A scope id never contains ``|`` (so the input parses one
#: way only).
ADDR_SUITE_SM1: str = "sm1"
SUPPORTED_ADDR_SUITES: frozenset[str] = frozenset({ADDR_SUITE_SM1})

#: Domain separation of the writer key's signed bytes (the scope is inside).
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

_CLAIM_KEYS: frozenset[str] = frozenset(
    {"scope", "scope_id", "blob_id", "addr_suite", "size", "expires_at", "ts"}
)
_SIGNED: frozenset[str] = _CLAIM_KEYS | {"auth", "sig", "sig_suite"}
_KEYS_BY_KIND: dict[str, frozenset[str]] = {
    AUTH_AUTHORITY: _SIGNED,
    AUTH_CHANNEL: _SIGNED,
    AUTH_WRITER_KEY: _SIGNED | {"epoch"},
}


class InvalidSpaceMediaAuth(ValueError):
    """A malformed upload authorization header."""


class UnsupportedSpaceMediaAddrSuite(ValueError):
    """An address construction this build lacks — refused, never defaulted."""


def space_media_address(
    scope_kind: str, scope_id: str, data: bytes, *, suite: str = ADDR_SUITE_SM1
) -> str:
    """A space media blob's address, bound to its scope (see the module
    docstring): the same ciphertext has another address in every scope.
    :class:`ValueError` for an unknown scope kind or a scope id that is
    empty or contains ``|``; :class:`UnsupportedSpaceMediaAddrSuite` for an
    unknown suite. Hashing up to 8 MiB: callers in async code run it in a
    worker thread."""
    if suite not in SUPPORTED_ADDR_SUITES:
        raise UnsupportedSpaceMediaAddrSuite(f"unsupported address suite {suite!r}")
    if scope_kind not in MEDIA_SCOPES:
        raise ValueError(f"unknown scope {scope_kind!r}")
    if not scope_id or "|" in scope_id:
        raise ValueError("a scope id is non-empty and has no '|'")
    h = hashlib.sha256()
    h.update(f"{suite}|{scope_kind}|{scope_id}|".encode())
    h.update(data)
    return h.hexdigest()


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
        value = _short_str(scope_id, "scope_id", MAX_SPACE_ID_CHARS)
        if "|" in value:
            # The address construction separates fields with "|".
            raise InvalidSpaceMediaAuth("bad scope_id")
        return value
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
    #: How ``blob_id`` is derived from the scope and the bytes
    #: (:func:`space_media_address`).
    addr_suite: str = ADDR_SUITE_SM1

    def signing_body(self) -> dict:
        return {
            "scope": self.scope,
            "scope_id": self.scope_id,
            "blob_id": self.blob_id,
            "addr_suite": self.addr_suite,
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
        """The bytes the writer group key signs for :data:`AUTH_WRITER_KEY`;
        the kind and epoch are bound in, the scope is in the claim."""
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

    def to_wire(self) -> dict:
        wire: dict = {
            **self.claim.signing_body(),
            "auth": self.kind,
            "sig": self.sig,
            "sig_suite": self.sig_suite,
        }
        if self.epoch is not None:
            wire["epoch"] = self.epoch
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
            addr_suite=_addr_suite(obj.get("addr_suite")),
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


def _addr_suite(value: object) -> str:
    if not isinstance(value, str) or value not in SUPPORTED_ADDR_SUITES:
        raise InvalidSpaceMediaAuth("unsupported addr_suite")
    return value


def _blob_id(value: object) -> str:
    if not is_blob_id(value):
        raise InvalidSpaceMediaAuth("bad blob_id")
    return str(value)
