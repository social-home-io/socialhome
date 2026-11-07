"""Wire shapes of space media on demand (v_83) — re-upload and streaming.

A GFS keeps space blobs only as a short-lived cache. When a reader misses
one, it asks (``POST /gfs/media/{blob_id}/wanted``, body
:class:`WantedRequest`: the scope it knows and a sealed locator,
:mod:`socialhome.space_media_hint`). The GFS then asks ONE household that
holds the file over its socket (frame :data:`SPACE_MEDIA_REQUEST_FRAME`,
:class:`MediaRequestFrame`) to send the same deterministic ciphertext to
``PUT /gfs/media/serve/{ticket}`` — piped to the waiting reader(s) and, in
:data:`MODE_CACHE`, also kept in the cache when the household attaches its
normal upload authorization — or to decline
(``POST /gfs/media/serve/{ticket}/decline``).

Both requests are signed by the HOLDER's household key over
:func:`serve_signing_body` (the GFS asked that household by name, over its
own socket — nothing new learned). Nothing on any of these shapes names a
reader; the frame carries the reader's hint verbatim (sealed for space key
holders, random nonce).

**Member fallback** (listed spaces only). When the owner household is
offline, did not answer in time or is over its budget (:data:`DECLINE_BUSY`),
a GFS that advertises :data:`MEMBER_PULL_CAPABILITY_KEY` offers the want to
the space's other members — anonymously: a member pulls
(``POST /gfs/media/member-pull``, :class:`MemberPullRequest`) signed by the
space's per-epoch writer GROUP key, never by its household key, and gets at
most one :class:`MemberWant`; it serves or declines that ticket with the same
routes, again signed by the writer key over
:func:`member_serve_signing_bytes` (headers
:data:`MEMBER_WRITER_EPOCH_HEADER` / :data:`SERVE_TS_HEADER` /
:data:`MEMBER_WRITER_SIG_HEADER` / :data:`MEMBER_WRITER_SIG_SUITE_HEADER`,
never :data:`SERVE_INSTANCE_HEADER`). The GFS learns "a writer-key holder of
the space" — no household.

Pure — no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..canonical_json import canonical_bytes
from ..moment_public_media import is_blob_id
from ..space_media_hint import SpaceMediaHint, parse_hint_wire
from .gfs_space_media import (
    MEDIA_SCOPES,
    SCOPE_SPACE,
    InvalidSpaceMediaAuth,
    valid_scope_id,
)
from .writer_cert import MAX_WRITER_CERT_EPOCH
from .writer_key import SUPPORTED_WRITER_KEY_SUITES

#: The socket frame a GFS sends a holder household.
SPACE_MEDIA_REQUEST_FRAME: str = "space_media_request"

#: Pipe the bytes to the waiting reader(s) only — nothing is stored.
MODE_STREAM: str = "stream"
#: Pipe them AND keep them in the cache (the holder attaches its upload
#: authorization; without one the GFS streams only).
MODE_CACHE: str = "cache"
MODES: frozenset[str] = frozenset({MODE_STREAM, MODE_CACHE})

#: Key, inside the signed ``space_media`` capability entry, of the on-demand
#: support: ``{"wait_s": <seconds a wanted request may wait>}``.
ON_DEMAND_CAPABILITY_KEY: str = "on_demand"

#: Largest ``wanted`` body (the scope id + a hint of ≤ 1.4 KB).
MAX_WANTED_BODY_BYTES: int = 4096

#: Headers of the holder's signed serve / decline.
SERVE_INSTANCE_HEADER: str = "X-SH-Instance"
SERVE_TS_HEADER: str = "X-SH-Timestamp"
SERVE_SIG_HEADER: str = "X-SH-Signature"

#: A ticket: an optional node route tag, ``.``, 32 hex (``node_route.scoped_id``).
_TICKET_RE = re.compile(r"(?:[A-Za-z0-9-]{1,64}\.)?[0-9a-f]{32}")

#: Why a holder declined (``POST …/decline`` body ``{"reason": …}``):
#: the hint did not open / named nothing at this address; it does not hold
#: the file; it holds it but is over its budget right now (the GFS may then
#: ask the space's members — an older GFS reads an unknown reason as
#: :data:`DECLINE_NOT_HELD`, fail-soft).
DECLINE_BAD_HINT: str = "bad_hint"
DECLINE_NOT_HELD: str = "not_held"
DECLINE_BUSY: str = "busy"
DECLINE_REASONS: frozenset[str] = frozenset(
    {DECLINE_BAD_HINT, DECLINE_NOT_HELD, DECLINE_BUSY}
)

# ── Member fallback ──────────────────────────────────────────────────────

#: Key, inside the signed ``on_demand`` entry, of the member fallback:
#: ``{"hold_s": <seconds a member pull is held open>}`` — present iff on.
MEMBER_PULL_CAPABILITY_KEY: str = "member_pull"

#: Route a writer-key holder pulls a want from.
MEMBER_PULL_ROUTE: str = "/gfs/media/member-pull"

#: Largest member pull body.
MAX_MEMBER_PULL_BODY_BYTES: int = 2048

#: Hints a :class:`MemberWant` carries at most.
MAX_MEMBER_WANT_HINTS: int = 4

#: Domain separators of the writer-key signatures (distinct from every
#: household, authority and writer-key prefix in use).
MEMBER_PULL_PREFIX: bytes = b"space-media-member-pull:v1:"
MEMBER_SERVE_PREFIX: bytes = b"space-media-member-serve:v1:"

#: Headers of a member's writer-key-signed serve / decline (with
#: :data:`SERVE_TS_HEADER`; never :data:`SERVE_INSTANCE_HEADER`).
MEMBER_WRITER_EPOCH_HEADER: str = "X-SH-Writer-Epoch"
MEMBER_WRITER_SIG_HEADER: str = "X-SH-Writer-Sig"
MEMBER_WRITER_SIG_SUITE_HEADER: str = "X-SH-Writer-Sig-Suite"

_MEMBER_PULL_KEYS: frozenset[str] = frozenset(
    {
        "scope",
        "scope_id",
        "epoch",
        "gfs_instance_id",
        "ts",
        "nonce",
        "sig",
        "sig_suite",
    }
)
_NONCE_RE = re.compile(r"[0-9a-f]{32}")
_GFS_KEY_RE = re.compile(r"[0-9a-f]{64}")
_MAX_ID_CHARS: int = 128
_MAX_SIG_CHARS: int = 256


class InvalidOnDemandWire(ValueError):
    """A wanted body or a request frame that is not one."""


def valid_ticket(value: object) -> bool:
    """Whether *value* is a serve ticket's shape."""
    return isinstance(value, str) and _TICKET_RE.fullmatch(value) is not None


#: What a signed holder request does (bound into its signature, so a serve
#: signature can never be replayed as a decline, or back).
SERVE_OP_SERVE: str = "serve"
SERVE_OP_DECLINE: str = "decline"


def serve_signing_body(instance_id: str, ticket: str, ts: int, op: str) -> dict:
    """What a holder signs (canonical JSON, household key) to serve or
    decline *ticket* — the relay-stream scheme, its own field names."""
    return {"instance_id": instance_id, "media_ticket": ticket, "op": op, "ts": ts}


@dataclass(slots=True, frozen=True)
class WantedRequest:
    """A reader's ``wanted`` body: the scope it knows the blob under and the
    sealed locator for the holder."""

    scope: str
    scope_id: str
    hint: SpaceMediaHint

    @classmethod
    def from_wire(cls, raw: object) -> WantedRequest:
        if not isinstance(raw, dict):
            raise InvalidOnDemandWire("body is not an object")
        scope = raw.get("scope")
        if scope not in MEDIA_SCOPES:
            raise InvalidOnDemandWire("bad scope")
        try:
            scope_id = valid_scope_id(scope, raw.get("scope_id"))
        except InvalidSpaceMediaAuth as exc:
            raise InvalidOnDemandWire("bad scope_id") from exc
        try:
            hint = parse_hint_wire(raw.get("hint"))
        except ValueError as exc:
            raise InvalidOnDemandWire(f"bad hint: {exc}") from exc
        return cls(scope=str(scope), scope_id=scope_id, hint=hint)

    def to_wire(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "scope_id": self.scope_id,
            "hint": self.hint.to_wire(),
        }


@dataclass(slots=True, frozen=True)
class MediaRequestFrame:
    """What a GFS asks a holder: send blob *blob_id* of the scope to
    *ticket* (``mode`` says whether the GFS will keep it)."""

    ticket: str
    scope: str
    scope_id: str
    blob_id: str
    mode: str
    hint: SpaceMediaHint

    def to_push(self) -> dict[str, object]:
        return {
            "type": SPACE_MEDIA_REQUEST_FRAME,
            "ticket": self.ticket,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "blob_id": self.blob_id,
            "mode": self.mode,
            "hint": self.hint.to_wire(),
        }

    @classmethod
    def from_frame(cls, raw: object) -> MediaRequestFrame:
        if not isinstance(raw, dict) or raw.get("type") != SPACE_MEDIA_REQUEST_FRAME:
            raise InvalidOnDemandWire("not a space media request")
        ticket = raw.get("ticket")
        if not valid_ticket(ticket):
            raise InvalidOnDemandWire("bad ticket")
        blob_id = raw.get("blob_id")
        if not is_blob_id(blob_id):
            raise InvalidOnDemandWire("bad blob_id")
        mode = raw.get("mode")
        if mode not in MODES:
            raise InvalidOnDemandWire("bad mode")
        wanted = WantedRequest.from_wire(raw)
        return cls(
            ticket=str(ticket),
            scope=wanted.scope,
            scope_id=wanted.scope_id,
            blob_id=str(blob_id),
            mode=str(mode),
            hint=wanted.hint,
        )


def member_serve_signing_bytes(
    ticket: str, op: str, scope: str, scope_id: str, epoch: int, ts: int
) -> bytes:
    """What a member signs with the writer group key of *epoch* to serve or
    decline *ticket* — the operation, the scope and the epoch are bound, so
    a serve is never a decline, nor valid for another space or epoch."""
    return MEMBER_SERVE_PREFIX + canonical_bytes(
        {
            "media_ticket": ticket,
            "op": op,
            "scope": scope,
            "scope_id": scope_id,
            "epoch": epoch,
            "ts": ts,
        }
    )


def _int_field(raw: dict, name: str, hi: int) -> int:
    value = raw.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= hi:
        raise InvalidOnDemandWire(f"bad {name}")
    return value


def _str_field(raw: dict, name: str, limit: int) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value or len(value) > limit:
        raise InvalidOnDemandWire(f"bad {name}")
    return value


@dataclass(slots=True, frozen=True)
class MemberPullRequest:
    """A writer-key holder's pull: "give me a want of this space, if any".
    Nothing in it names a household — only the space, the writer key's epoch
    and this GFS (no cross-server replay); ``nonce`` keeps every pull's bytes
    distinct for the replay guard."""

    scope: str
    scope_id: str
    epoch: int
    gfs_instance_id: str
    ts: int
    nonce: str
    sig: str
    sig_suite: str
    #: The PINNED key of the addressed server (hex), signed when present.
    gfs_key: str | None = None

    def signing_body(self) -> dict[str, object]:
        body: dict[str, object] = {
            "scope": self.scope,
            "scope_id": self.scope_id,
            "epoch": self.epoch,
            "gfs_instance_id": self.gfs_instance_id,
            "ts": self.ts,
            "nonce": self.nonce,
        }
        if self.gfs_key is not None:
            body["gfs_key"] = self.gfs_key
        return body

    def signing_bytes(self) -> bytes:
        """The bytes the writer group key signs."""
        return MEMBER_PULL_PREFIX + canonical_bytes(self.signing_body())

    def to_wire(self) -> dict[str, object]:
        return {**self.signing_body(), "sig": self.sig, "sig_suite": self.sig_suite}

    @classmethod
    def from_wire(cls, raw: object) -> MemberPullRequest:
        """Parse a pull body; :class:`InvalidOnDemandWire` on any missing,
        extra or malformed field (so a body naming a household — an
        ``instance_id``, a cert — is refused outright) or an unknown suite."""
        if not isinstance(raw, dict):
            raise InvalidOnDemandWire("body is not an object")
        if not _MEMBER_PULL_KEYS <= set(raw) <= _MEMBER_PULL_KEYS | {"gfs_key"}:
            raise InvalidOnDemandWire("unexpected or missing fields")
        if raw.get("scope") != SCOPE_SPACE:
            # A private channel has its own seat-holder path.
            raise InvalidOnDemandWire("bad scope")
        try:
            scope_id = valid_scope_id(SCOPE_SPACE, raw.get("scope_id"))
        except InvalidSpaceMediaAuth as exc:
            raise InvalidOnDemandWire("bad scope_id") from exc
        gfs_key = raw.get("gfs_key")
        if gfs_key is not None and (
            not isinstance(gfs_key, str) or _GFS_KEY_RE.fullmatch(gfs_key) is None
        ):
            raise InvalidOnDemandWire("bad gfs_key")
        nonce = raw.get("nonce")
        if not isinstance(nonce, str) or _NONCE_RE.fullmatch(nonce) is None:
            raise InvalidOnDemandWire("bad nonce")
        suite = _str_field(raw, "sig_suite", _MAX_ID_CHARS)
        if suite not in SUPPORTED_WRITER_KEY_SUITES:
            raise InvalidOnDemandWire("unknown sig_suite")
        return cls(
            scope=SCOPE_SPACE,
            scope_id=scope_id,
            epoch=_int_field(raw, "epoch", MAX_WRITER_CERT_EPOCH),
            gfs_instance_id=_str_field(raw, "gfs_instance_id", _MAX_ID_CHARS),
            gfs_key=gfs_key,
            ts=_int_field(raw, "ts", 1 << 40),
            nonce=nonce,
            sig=_str_field(raw, "sig", _MAX_SIG_CHARS),
            sig_suite=suite,
        )


@dataclass(slots=True, frozen=True)
class MemberWant:
    """One want a pull answers with: serve blob *blob_id* of the pulled
    scope to *ticket* (or decline), using one of *hints* (the readers'
    sealed locators, verbatim). It names no reader and no owner."""

    ticket: str
    blob_id: str
    mode: str
    hints: tuple[SpaceMediaHint, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "ticket": self.ticket,
            "blob_id": self.blob_id,
            "mode": self.mode,
            "hints": [h.to_wire() for h in self.hints],
        }

    @classmethod
    def from_wire(cls, raw: object) -> MemberWant:
        if not isinstance(raw, dict):
            raise InvalidOnDemandWire("want is not an object")
        ticket = raw.get("ticket")
        if not valid_ticket(ticket):
            raise InvalidOnDemandWire("bad ticket")
        blob_id = raw.get("blob_id")
        if not is_blob_id(blob_id):
            raise InvalidOnDemandWire("bad blob_id")
        mode = raw.get("mode")
        if mode not in MODES:
            raise InvalidOnDemandWire("bad mode")
        hints = raw.get("hints")
        if not isinstance(hints, list) or not 1 <= len(hints) <= MAX_MEMBER_WANT_HINTS:
            raise InvalidOnDemandWire("bad hints")
        try:
            parsed = tuple(parse_hint_wire(h) for h in hints)
        except ValueError as exc:
            raise InvalidOnDemandWire(f"bad hint: {exc}") from exc
        return cls(
            ticket=str(ticket), blob_id=str(blob_id), mode=str(mode), hints=parsed
        )


__all__ = [
    "DECLINE_BAD_HINT",
    "DECLINE_BUSY",
    "DECLINE_NOT_HELD",
    "DECLINE_REASONS",
    "MAX_MEMBER_PULL_BODY_BYTES",
    "MAX_MEMBER_WANT_HINTS",
    "MAX_WANTED_BODY_BYTES",
    "MEMBER_PULL_CAPABILITY_KEY",
    "MEMBER_PULL_PREFIX",
    "MEMBER_PULL_ROUTE",
    "MEMBER_SERVE_PREFIX",
    "MEMBER_WRITER_EPOCH_HEADER",
    "MEMBER_WRITER_SIG_HEADER",
    "MEMBER_WRITER_SIG_SUITE_HEADER",
    "MODES",
    "MODE_CACHE",
    "MODE_STREAM",
    "ON_DEMAND_CAPABILITY_KEY",
    "SERVE_INSTANCE_HEADER",
    "SERVE_SIG_HEADER",
    "SERVE_TS_HEADER",
    "SPACE_MEDIA_REQUEST_FRAME",
    "InvalidOnDemandWire",
    "MediaRequestFrame",
    "MemberPullRequest",
    "MemberWant",
    "WantedRequest",
    "member_serve_signing_bytes",
    "serve_signing_body",
    "valid_ticket",
]
