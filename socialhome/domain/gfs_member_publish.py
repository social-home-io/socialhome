"""Trusted-mode member publish over the connection server — wire codec (v_49).

A space member household that holds a :class:`~socialhome.domain.writer_cert
.WriterCert` publishes its own item to every connection server (GFS) the
space is listed on, without the host signing it. In **trusted mode** (the
owner-decided default) the request is *identified*: the household signs it
with its registered GFS identity, and the GFS authorizes it by verifying the
plaintext writer cert against the space authority key it already pins.

What the GFS learns: which household published into which listed space, at
which content epoch, and when. What it never learns: the content or the real
item type. ``payload`` is ciphertext under the space's epoch content key and
carries the real type (post, comment, …) plus the author-signed inner; the
outer ``event_type`` is ALWAYS the generic :data:`SPACE_ITEM_EVENT_TYPE`.
Because the type is hidden, the GFS checks the weakest scope (``comment``)
and RECEIVERS enforce the scope the real type needs.

Request (``POST /gfs/member-publish``, every field required, no others)::

    {"instance_id", "ts": <tz-aware ISO 8601>, "signature": <b64url>,
     "target": <space_id>, "event_type": "space_item", "epoch": N,
     "writer_cert": {<WriterCert wire dict>}, "payload": <ciphertext str>}

``signature`` is the household's Ed25519 signature over the canonical JSON
(sorted keys, compact separators) of :meth:`MemberPublishRequest
.signing_payload` — every other field plus ``"action":
MEMBER_PUBLISH_ACTION``. That is the same scheme as every other signed
household→GFS request (``subscribe`` / ``unsubscribe`` / ``unpublish``);
the ``action`` value is the domain separator, so a member-publish signature
can never be replayed as another request and vice versa.

Fan-out frame (to the space's subscribers, never naming the publisher)::

    {"space_id", "event_type": "space_item", "epoch", "writer_cert", "payload"}

Pure module: dataclasses + codec only, importable by the content-blind GFS
process.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .writer_cert import MAX_WRITER_CERT_EPOCH, WriterCert

#: The one outer event type a member-published item ever carries. The real
#: type travels inside the ciphertext.
SPACE_ITEM_EVENT_TYPE: str = "space_item"

#: Domain separator inside the signed bytes (see the module docstring).
MEMBER_PUBLISH_ACTION: str = "gfs-member-publish:v1"

#: Route the GFS mounts the trusted member-publish endpoint at.
MEMBER_PUBLISH_ROUTE: str = "/gfs/member-publish"

#: How long the GFS keeps accepting certs of an epoch older than the newest
#: one it knows, after it learned the newer one (seconds). Mirrors the
#: receivers' ``WRITER_CERT_EPOCH_GRACE_S`` so a member whose new cert is
#: still in flight is not refused by the relay while its receivers would
#: accept it.
MEMBER_PUBLISH_EPOCH_GRACE_S: int = 600

#: Upper bound on the ciphertext string (chars). A post's AES-GCM ciphertext
#: base64-expanded is ~55 KiB at the post-length cap; media bytes ride
#: separate blobs. Sits under the GFS body cap with room for the cert.
MEMBER_PUBLISH_MAX_PAYLOAD_CHARS: int = 200 * 1024

#: Upper bound on identifier-shaped fields (``target``, ``instance_id``) —
#: mirrors the GFS ``/gfs/publish`` guard.
MAX_WIRE_ID_CHARS: int = 128

#: Upper bound on ``ts`` / ``signature`` strings.
_MAX_SHORT_FIELD_CHARS: int = 256

#: Exact request key set.
MEMBER_PUBLISH_REQUEST_KEYS: frozenset[str] = frozenset(
    {
        "instance_id",
        "ts",
        "signature",
        "target",
        "event_type",
        "epoch",
        "writer_cert",
        "payload",
    }
)

#: Exact fan-out frame key set (the WS push adds only ``"type": "relay"``).
MEMBER_PUBLISH_FRAME_KEYS: frozenset[str] = frozenset(
    {"space_id", "event_type", "epoch", "writer_cert", "payload"}
)


class InvalidMemberPublish(ValueError):
    """The body or frame is not a well-formed member-publish shape."""


def _short_str(raw: dict, field: str, limit: int) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value or len(value) > limit:
        raise InvalidMemberPublish(f"invalid field: {field}")
    return value


def _epoch(raw: dict) -> int:
    value = raw.get("epoch")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= MAX_WRITER_CERT_EPOCH
    ):
        raise InvalidMemberPublish("invalid field: epoch")
    return value


def _payload(raw: dict) -> str:
    return _short_str(raw, "payload", MEMBER_PUBLISH_MAX_PAYLOAD_CHARS)


def _cert(raw: dict) -> WriterCert:
    try:
        return WriterCert.from_wire(raw.get("writer_cert"))
    except ValueError as exc:
        raise InvalidMemberPublish("invalid field: writer_cert") from exc


def _event_type(raw: dict) -> None:
    if raw.get("event_type") != SPACE_ITEM_EVENT_TYPE:
        raise InvalidMemberPublish("invalid field: event_type")


@dataclass(slots=True, frozen=True)
class MemberPublishRequest:
    """One trusted-mode member-publish request (see the module docstring)."""

    instance_id: str
    ts: str
    signature: str
    target: str
    epoch: int
    writer_cert: WriterCert
    payload: str

    @property
    def event_type(self) -> str:
        return SPACE_ITEM_EVENT_TYPE

    def signing_payload(self) -> dict:
        """Every signed field: the request minus ``signature``, plus the
        ``action`` domain separator."""
        return {
            "action": MEMBER_PUBLISH_ACTION,
            "instance_id": self.instance_id,
            "ts": self.ts,
            "target": self.target,
            "event_type": SPACE_ITEM_EVENT_TYPE,
            "epoch": self.epoch,
            "writer_cert": self.writer_cert.to_wire(),
            "payload": self.payload,
        }

    def signing_bytes(self) -> bytes:
        """Canonical bytes the household signs — the same encoding the GFS
        verifies every signed household request with."""
        return json.dumps(
            self.signing_payload(), separators=(",", ":"), sort_keys=True
        ).encode("utf-8")

    def to_wire(self) -> dict:
        body = self.signing_payload()
        del body["action"]
        body["signature"] = self.signature
        return body

    def fan_out_frame(self) -> dict:
        """The identity-free frame the GFS fans out to subscribers."""
        return {
            "space_id": self.target,
            "event_type": SPACE_ITEM_EVENT_TYPE,
            "epoch": self.epoch,
            "writer_cert": self.writer_cert.to_wire(),
            "payload": self.payload,
        }

    @classmethod
    def from_wire(cls, raw: object) -> "MemberPublishRequest":
        """Parse a request body; :class:`InvalidMemberPublish` on any
        missing, extra or malformed field."""
        if not isinstance(raw, dict):
            raise InvalidMemberPublish("expected a JSON object")
        if set(raw) != MEMBER_PUBLISH_REQUEST_KEYS:
            raise InvalidMemberPublish("unexpected or missing fields")
        _event_type(raw)
        return cls(
            instance_id=_short_str(raw, "instance_id", MAX_WIRE_ID_CHARS),
            ts=_short_str(raw, "ts", _MAX_SHORT_FIELD_CHARS),
            signature=_short_str(raw, "signature", _MAX_SHORT_FIELD_CHARS),
            target=_short_str(raw, "target", MAX_WIRE_ID_CHARS),
            epoch=_epoch(raw),
            writer_cert=_cert(raw),
            payload=_payload(raw),
        )


@dataclass(slots=True, frozen=True)
class SpaceItemFrame:
    """A member-published item as a subscriber receives it from the GFS."""

    space_id: str
    epoch: int
    writer_cert: WriterCert
    payload: str

    def to_wire(self) -> dict:
        return {
            "space_id": self.space_id,
            "event_type": SPACE_ITEM_EVENT_TYPE,
            "epoch": self.epoch,
            "writer_cert": self.writer_cert.to_wire(),
            "payload": self.payload,
        }

    @classmethod
    def from_wire(cls, raw: object) -> "SpaceItemFrame":
        """Parse a fan-out frame. The WS ``type`` key (and any key an older
        or newer GFS adds) is ignored — receivers authenticate the item by
        the cert and the author signature inside, never by outer fields."""
        if not isinstance(raw, dict):
            raise InvalidMemberPublish("expected a JSON object")
        _event_type(raw)
        return cls(
            space_id=_short_str(raw, "space_id", MAX_WIRE_ID_CHARS),
            epoch=_epoch(raw),
            writer_cert=_cert(raw),
            payload=_payload(raw),
        )
