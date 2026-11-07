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

Pure — no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..moment_public_media import is_blob_id
from ..space_media_hint import SpaceMediaHint, parse_hint_wire
from .gfs_space_media import (
    MEDIA_SCOPES,
    InvalidSpaceMediaAuth,
    valid_scope_id,
)

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


class InvalidOnDemandWire(ValueError):
    """A wanted body or a request frame that is not one."""


def valid_ticket(value: object) -> bool:
    """Whether *value* is a serve ticket's shape."""
    return isinstance(value, str) and _TICKET_RE.fullmatch(value) is not None


def serve_signing_body(instance_id: str, ticket: str, ts: int) -> dict:
    """What a holder signs (canonical JSON, household key) to serve or
    decline *ticket* — the relay-stream scheme, its own field name."""
    return {"instance_id": instance_id, "media_ticket": ticket, "ts": ts}


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


__all__ = [
    "MAX_WANTED_BODY_BYTES",
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
    "WantedRequest",
    "serve_signing_body",
    "valid_ticket",
]
