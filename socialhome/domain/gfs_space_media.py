"""Wire shape of the GFS space media cache — upload authorization.

A household uploads one encrypted space file to a GFS — ``PUT
/gfs/media/{blob_id}``, the one scope-free path — under a **scope** named
only inside the signed claim (never in the URL). Since v_96 the one scope is
``channel``: a space's opaque channel (v_51 private, v_96 global) — the GFS
learns only the random channel id, never the space. (The space-keyed
``space`` scope went with the space-keyed relay, owner decision Q5.)

The raw blob is the body, so the authorization rides in ONE header,
:data:`SPACE_MEDIA_AUTH_HEADER`: the base64url of a canonical JSON object.
Every kind carries the claim

    {scope, scope_id, blob_id, size, expires_at, ts}

(``ts`` and ``expires_at`` unix seconds, ``size`` the body's byte count —
checked against the body, which is read only after the signature verified)
plus ``auth`` (the kind), ``sig`` and ``sig_suite``. Each kind reuses what the
GFS already verifies for the channel (the checks ``/gfs/channels/*`` make) —
no new key, roster or registration:

* :data:`AUTH_CHANNEL` — ``sig`` by the channel key the GFS pinned at
  registration (what registration, epoch notices and unregister are signed
  with) over :data:`CHANNEL_MEDIA_PUT_PREFIX` + the claim. Names nobody.
* :data:`AUTH_WRITER_KEY` — ``epoch``; ``sig`` by the channel writer key
  pinned for ``epoch``, over :meth:`SpaceMediaClaim.writer_signing_bytes`.
  Names nobody.

No kind names a household (the identified writer-cert kinds went with the
identified member publish, v_90).

**The address is bound to the scope** (security review v2, N1): a blob's
id is :func:`space_media_address` — ``sha256("sm1|" + scope_kind + "|" +
scope_id + "|" + ciphertext)`` — not the bare SHA-256 of the bytes, so the
same ciphertext has a different address in every scope. The GFS recomputes
it on upload from the claim's scope and refuses a mismatch; a reader checks
it with the scope id it knows. So a blob belongs to exactly ONE scope:
nobody who fetched a channel's ciphertext can store it under their own scope
at that channel's address — no cross-scope claims, no first-storer
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

from ..canonical_json import canonical_bytes
from ..moment_public_media import MAX_BLOB_BYTES, MAX_MEDIA_BYTES, is_blob_id
from .gfs_channel import InvalidChannelWire, valid_channel_id
from .media_constraints import VIDEO_MAX_UPLOAD_BYTES
from .writer_cert import MAX_WRITER_CERT_EPOCH

#: The header carrying the upload authorization.
SPACE_MEDIA_AUTH_HEADER: str = "X-SH-Space-Media-Auth"

#: Longest accepted header value (base64url of the whole object stays well
#: under 1 KB).
SPACE_MEDIA_AUTH_MAX_CHARS: int = 4096

#: Key of the cache's entry in the signed ``/gfs/info`` capability block:
#: ``{"max_blob_bytes": int, "ttl_s": int, "space_max_bytes": int,
#: "space_uploads_per_hour": int, "scopes": ["channel"]}``.
#: (The routes are registered in ``global_server/routes/__init__.py``; the
#: fetch, ``/gfs/media/{blob_id}``, names no scope.)
SPACE_MEDIA_CAPABILITY: str = "space_media"

SCOPE_CHANNEL: str = "channel"
MEDIA_SCOPES: frozenset[str] = frozenset({SCOPE_CHANNEL})

AUTH_WRITER_KEY: str = "writer_key"
AUTH_CHANNEL: str = "channel"

#: Which kinds each scope takes.
AUTH_KINDS_BY_SCOPE: dict[str, frozenset[str]] = {
    SCOPE_CHANNEL: frozenset({AUTH_CHANNEL, AUTH_WRITER_KEY}),
}

#: The scope-bound address construction, version-tagged so it can evolve:
#: ``sha256(b"sm1|" + scope_kind + b"|" + scope_id + b"|" + ciphertext)``,
#: lowercase hex. A scope id never contains ``|`` (so the input parses one
#: way only).
ADDR_SUITE_SM1: str = "sm1"
#: v_97 — a PIECE of a file larger than one blob (see "Pieces" below): the
#: address is derived, not hashed from the bytes, and an upload proves the
#: bytes with a :class:`PieceProof`. Never a content address
#: (:func:`space_media_address` refuses it).
ADDR_SUITE_SM1P: str = "sm1p"
SUPPORTED_ADDR_SUITES: frozenset[str] = frozenset({ADDR_SUITE_SM1, ADDR_SUITE_SM1P})

# ── Pieces (sm1p, v_97) ──────────────────────────────────────────────────
#
# A file over one blob (8 MiB) is split into fixed 8 MiB pieces, each sealed
# like any space file (its AAD names the piece). Its descriptor carries ONE
# address, the file's — ``sha256("sm1p|" + scope + "|" + scope_id + "|" +
# h_0 ‖ … ‖ h_{N-1})`` over the SHA-256 of every sealed piece — and each
# piece sits at ``sha256("sm1p|piece|" + file address + "|" + index)``. A
# reader derives every piece address without a per-piece list. The GFS keeps
# its content check: an upload (or an on-demand serve) of piece *i* carries
# the manifest ``[h_0 … h_{N-1}]`` and *i* (:data:`PIECE_PROOF_HEADER`), and
# the GFS checks ``sha256(body) == h_i``, the manifest → the file address →
# the asked piece address (:func:`check_piece`). So no other bytes can ever
# sit at a real piece address, and a forged manifest only names another
# file.

#: One piece of a file: the largest file one blob holds.
PIECE_BYTES: int = MAX_MEDIA_BYTES
#: The largest pieced file (the 200 MiB upload cap) and its piece count.
MAX_PIECED_FILE_BYTES: int = VIDEO_MAX_UPLOAD_BYTES
MAX_PIECES: int = -(-MAX_PIECED_FILE_BYTES // PIECE_BYTES)

#: The header carrying a :class:`PieceProof` (unsigned: it proves itself).
PIECE_PROOF_HEADER: str = "X-SH-Space-Media-Piece"
_PIECE_PROOF_MAX_CHARS: int = 8192
_PIECE_PROOF_KEYS: frozenset[str] = frozenset({"manifest", "index"})


class InvalidPieceProof(ValueError):
    """A malformed piece proof."""


def piece_count(file_size: object) -> int:
    """How many pieces a file of *file_size* bytes is split into — only a
    file larger than one blob and at most :data:`MAX_PIECED_FILE_BYTES`
    (:class:`ValueError` otherwise)."""
    if (
        not isinstance(file_size, int)
        or isinstance(file_size, bool)
        or not PIECE_BYTES < file_size <= MAX_PIECED_FILE_BYTES
    ):
        raise ValueError("not a pieced file size")
    return -(-file_size // PIECE_BYTES)


def _scope_prefix(suite: str, scope_kind: str, scope_id: str) -> bytes:
    if scope_kind not in MEDIA_SCOPES:
        raise ValueError(f"unknown scope {scope_kind!r}")
    if not scope_id or "|" in scope_id:
        raise ValueError("a scope id is non-empty and has no '|'")
    return f"{suite}|{scope_kind}|{scope_id}|".encode()


def pieced_file_address(
    scope_kind: str, scope_id: str, piece_hashes: tuple[str, ...] | list[str]
) -> str:
    """A pieced file's address: bound to its scope and to the SHA-256 of
    every sealed piece, in order. :class:`ValueError` for a bad scope or a
    manifest entry that is no SHA-256 hex."""
    h = hashlib.sha256(_scope_prefix(ADDR_SUITE_SM1P, scope_kind, scope_id))
    for entry in piece_hashes:
        if not is_blob_id(entry):
            raise ValueError("a manifest entry is a lowercase SHA-256 hex")
        h.update(bytes.fromhex(entry))
    return h.hexdigest()


def piece_address(file_address: str, index: int) -> str:
    """Where piece *index* of the file at *file_address* sits."""
    if (
        not is_blob_id(file_address)
        or not isinstance(index, int)
        or isinstance(index, bool)
        or not 0 <= index < MAX_PIECES
    ):
        raise ValueError("bad piece address input")
    return hashlib.sha256(
        f"{ADDR_SUITE_SM1P}|piece|{file_address}|{index}".encode()
    ).hexdigest()


@dataclass(slots=True, frozen=True)
class PieceProof:
    """What proves a piece's bytes at its address: the file's manifest
    (the SHA-256 of every sealed piece, 2 … :data:`MAX_PIECES`) and which
    piece this is."""

    manifest: tuple[str, ...]
    index: int

    def to_header(self) -> str:
        return (
            base64.urlsafe_b64encode(
                canonical_bytes({"manifest": list(self.manifest), "index": self.index})
            )
            .decode()
            .rstrip("=")
        )

    @classmethod
    def from_header(cls, raw: str) -> PieceProof:
        """Parse a header value, bounded; :class:`InvalidPieceProof` on
        anything malformed (the content check is :func:`check_piece`)."""
        if not isinstance(raw, str) or not raw or len(raw) > _PIECE_PROOF_MAX_CHARS:
            raise InvalidPieceProof("bad header")
        try:
            obj = orjson.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        except (ValueError, binascii.Error) as exc:
            raise InvalidPieceProof("not base64url JSON") from exc
        if not isinstance(obj, dict) or set(obj) != _PIECE_PROOF_KEYS:
            raise InvalidPieceProof("unexpected fields")
        manifest = obj.get("manifest")
        index = obj.get("index")
        if (
            not isinstance(manifest, list)
            or not 2 <= len(manifest) <= MAX_PIECES
            or not all(is_blob_id(h) for h in manifest)
        ):
            raise InvalidPieceProof("bad manifest")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < len(manifest)
        ):
            raise InvalidPieceProof("bad index")
        return cls(manifest=tuple(str(h) for h in manifest), index=index)


def check_piece(
    scope_kind: str, scope_id: str, blob_id: str, data: bytes, proof: PieceProof
) -> bool:
    """Whether *data* is piece ``proof.index`` of the file *proof*'s
    manifest names in this scope, and that piece sits at *blob_id*. Never
    raises. Hashing up to 8 MiB: async callers run it in a worker thread."""
    try:
        if not 0 <= proof.index < len(proof.manifest):
            return False
        if hashlib.sha256(data).hexdigest() != proof.manifest[proof.index]:
            return False
        file_addr = pieced_file_address(scope_kind, scope_id, proof.manifest)
        return piece_address(file_addr, proof.index) == blob_id
    except ValueError:
        return False


#: Domain separation of the writer key's signed bytes (the scope is inside).
SPACE_MEDIA_WRITER_PREFIX: bytes = b"space-media-put:v1:"

#: Domain separation of what a channel key signs for the cache (beside the
#: other ``gfs-channel-*:v1:`` prefixes, :mod:`socialhome.gfs_channel`).
CHANNEL_MEDIA_PUT_PREFIX: bytes = b"gfs-channel-media-put:v1:"
CHANNEL_MEDIA_DELETE_PREFIX: bytes = b"gfs-channel-media-delete:v1:"

_MAX_SIG_CHARS: int = 512
_MAX_SUITE_CHARS: int = 64
_MAX_UNIX: int = 2**53

_CLAIM_KEYS: frozenset[str] = frozenset(
    {"scope", "scope_id", "blob_id", "addr_suite", "size", "expires_at", "ts"}
)
_SIGNED: frozenset[str] = _CLAIM_KEYS | {"auth", "sig", "sig_suite"}
_KEYS_BY_KIND: dict[str, frozenset[str]] = {
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
    if suite != ADDR_SUITE_SM1:
        # sm1p (a piece) is derived, never hashed from the bytes.
        raise UnsupportedSpaceMediaAddrSuite(f"no content address for {suite!r}")
    h = hashlib.sha256(_scope_prefix(suite, scope_kind, scope_id))
    h.update(data)
    return h.hexdigest()


def media_delete_payload(scope: str, scope_id: str, blob_id: str, ts: int) -> dict:
    """What a channel key holder signs (after
    :data:`CHANNEL_MEDIA_DELETE_PREFIX`) to withdraw a blob."""
    return {"scope": scope, "scope_id": scope_id, "blob_id": blob_id, "ts": ts}


def valid_scope_id(scope: object, scope_id: object) -> str:
    """*scope_id* checked for its *scope*, else :class:`InvalidSpaceMediaAuth`."""
    if scope == SCOPE_CHANNEL:
        try:
            return valid_channel_id(scope_id)
        except InvalidChannelWire as exc:
            raise InvalidSpaceMediaAuth("bad scope_id") from exc
    raise InvalidSpaceMediaAuth("bad scope")


@dataclass(slots=True, frozen=True)
class SpaceMediaClaim:
    """What every upload signs: which blob, for which channel, how big,
    until when, and when it was signed."""

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

    def channel_signing_bytes(self) -> bytes:
        """The bytes a channel key holder signs for :data:`AUTH_CHANNEL`."""
        return CHANNEL_MEDIA_PUT_PREFIX + canonical_bytes(self.signing_body())

    def writer_signing_bytes(self, *, kind: str, epoch: int) -> bytes:
        """The bytes the channel writer key signs for :data:`AUTH_WRITER_KEY`;
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
