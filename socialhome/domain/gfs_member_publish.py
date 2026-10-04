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

    {"instance_id", "gfs_instance_id", "ts": <tz-aware ISO 8601>,
     "signature": <b64url>,
     "target": <space_id>, "event_type": "space_item", "epoch": N,
     "writer_cert": {<WriterCert wire dict>}, "payload": <ciphertext str>}

``signature`` is the household's Ed25519 signature over the canonical JSON
(sorted keys, compact separators) of :meth:`MemberPublishRequest
.signing_payload` — every other field plus ``"action":
MEMBER_PUBLISH_ACTION``. That is the same scheme as every other signed
household→GFS request (``subscribe`` / ``unsubscribe`` / ``unpublish``);
the ``action`` value is the domain separator, so a member-publish signature
can never be replayed as another request and vice versa. ``gfs_instance_id``
is the connection server's own id as the household pinned it from
``/gfs/info``: the server refuses any other value, so a request signed for
one server can't be replayed to another.

Fan-out frame (to the space's subscribers, never naming the publisher)::

    {"space_id", "event_type": "space_item", "epoch", "writer_cert", "payload"}

**Strict mode (v_50).** A space whose owner set ``gfs_publish_mode`` to
``"strict"`` publishes over ``POST /gfs/member-publish-anon`` instead
(:class:`MemberPublishAnonRequest`)::

    {"gfs_instance_id", "ts", "nonce", "target", "event_type": "space_item",
     "epoch", "payload", "writer_sig", "writer_sig_suite"}

No ``instance_id``, no household signature, no plaintext writer cert: the
writer cert rides INSIDE the ciphertext, and ``writer_sig`` is made with the
space's per-epoch writer GROUP key (:mod:`socialhome.writer_key`) over
``b"gfs-member-publish-anon:v1:"`` + the canonical JSON of every other field.
The server verifies it against the writer key the space authority pinned for
that epoch, and learns only that *some* publisher of the space posted. Its
fan-out frame carries no ``writer_cert`` either::

    {"space_id", "event_type": "space_item", "epoch", "payload"}

Pure module: dataclasses + codec only, importable by the content-blind GFS
process.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .writer_cert import MAX_WRITER_CERT_EPOCH, WriterCert
from .writer_key import WriterKeyCert

#: The one outer event type a member-published item ever carries. The real
#: type travels inside the ciphertext.
SPACE_ITEM_EVENT_TYPE: str = "space_item"

#: Domain separator inside the signed bytes (see the module docstring).
MEMBER_PUBLISH_ACTION: str = "gfs-member-publish:v1"

#: Domain separator of the space OWNER's household-signed epoch notice.
OWNER_EPOCH_NOTICE_ACTION: str = "gfs-owner-epoch-notice:v1"

#: Route the GFS mounts the trusted member-publish endpoint at.
MEMBER_PUBLISH_ROUTE: str = "/gfs/member-publish"

#: Route of the strict-mode (anonymous) member publish (v_50).
MEMBER_PUBLISH_ANON_ROUTE: str = "/gfs/member-publish-anon"

#: Domain-separation prefix of the writer-key signature over an anonymous
#: publish — distinct from every household and authority prefix.
MEMBER_PUBLISH_ANON_PREFIX: bytes = b"gfs-member-publish-anon:v1:"

#: The owner's per-space choice of how members publish over the GFS
#: (``SpaceFeatures.gfs_publish_mode``). ``trusted`` (the default): the
#: request is identified by the household. ``strict``: anonymous, signed with
#: the epoch's writer group key; the GFS refuses identified publishes.
GFS_PUBLISH_MODE_TRUSTED: str = "trusted"
GFS_PUBLISH_MODE_STRICT: str = "strict"
GFS_PUBLISH_MODES: frozenset[str] = frozenset(
    {GFS_PUBLISH_MODE_TRUSTED, GFS_PUBLISH_MODE_STRICT}
)

#: Length bounds of the anonymous request's ``nonce`` (b64url of >= 12
#: random bytes).
MIN_NONCE_CHARS: int = 16
MAX_NONCE_CHARS: int = 64

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

#: Exact key set of the plaintext ``writer_cert`` — the v1 fields ONLY. The
#: v2 user binding (``writer_user_ids`` / ``users_sig`` / ``users_sig_suite``)
#: names the household's users, so it rides only inside the encrypted inner;
#: a request carrying it is refused, and nothing this module serializes ever
#: includes it.
PLAINTEXT_CERT_KEYS: frozenset[str] = frozenset(
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

#: Exact request key set.
MEMBER_PUBLISH_REQUEST_KEYS: frozenset[str] = frozenset(
    {
        "instance_id",
        "gfs_instance_id",
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

#: Exact key set of a strict-mode (anonymous) request — no ``instance_id``,
#: no household ``signature``, no ``writer_cert``.
MEMBER_PUBLISH_ANON_REQUEST_KEYS: frozenset[str] = frozenset(
    {
        "gfs_instance_id",
        "ts",
        "nonce",
        "target",
        "event_type",
        "epoch",
        "payload",
        "writer_sig",
        "writer_sig_suite",
    }
)

#: Exact key set of a strict-mode fan-out frame (no cert anywhere outside
#: the ciphertext).
MEMBER_PUBLISH_ANON_FRAME_KEYS: frozenset[str] = frozenset(
    {"space_id", "event_type", "epoch", "payload"}
)


def owner_epoch_notice_signing_payload(
    *,
    owning_instance: str,
    gfs_instance_id: str,
    space_id: str,
    epoch: int,
    ts: str,
    publish_mode: str | None = None,
    writer_key_cert: dict | None = None,
) -> dict:
    """What the space OWNER's household signs to confirm a content epoch at
    one connection server (``POST /gfs/spaces/{id}/epoch``). Canonical JSON of
    this dict (sorted keys, compact) is the signed message; ``action`` is the
    domain separator and ``gfs_instance_id`` binds it to one server.

    v_50 adds two OPTIONAL fields, signed only when present (a v_49 owner's
    notice keeps its exact bytes): ``publish_mode`` — the space's
    ``gfs_publish_mode``, so the server can refuse identified publishes into
    a strict space — and ``writer_key_cert``, the authority-signed pin of the
    writer group key for ``epoch``. A household sends them only to a server
    whose signed ``/gfs/info`` proves ``member_publish_strict`` (an older
    server would verify without them and refuse the notice)."""
    payload: dict = {
        "action": OWNER_EPOCH_NOTICE_ACTION,
        "owning_instance": owning_instance,
        "gfs_instance_id": gfs_instance_id,
        "space_id": space_id,
        "epoch": epoch,
        "ts": ts,
    }
    if publish_mode is not None:
        payload["publish_mode"] = publish_mode
    if writer_key_cert is not None:
        payload["writer_key_cert"] = writer_key_cert
    return payload


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
    wire = raw.get("writer_cert")
    if not isinstance(wire, dict) or set(wire) != PLAINTEXT_CERT_KEYS:
        raise InvalidMemberPublish("invalid field: writer_cert")
    try:
        return WriterCert.from_wire(wire)
    except ValueError as exc:
        raise InvalidMemberPublish("invalid field: writer_cert") from exc


def _event_type(raw: dict) -> None:
    if raw.get("event_type") != SPACE_ITEM_EVENT_TYPE:
        raise InvalidMemberPublish("invalid field: event_type")


@dataclass(slots=True, frozen=True)
class MemberPublishRequest:
    """One trusted-mode member-publish request (see the module docstring)."""

    instance_id: str
    gfs_instance_id: str
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
            "gfs_instance_id": self.gfs_instance_id,
            "ts": self.ts,
            "target": self.target,
            "event_type": SPACE_ITEM_EVENT_TYPE,
            "epoch": self.epoch,
            "writer_cert": self.writer_cert.v1().to_wire(),
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
            "writer_cert": self.writer_cert.v1().to_wire(),
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
            gfs_instance_id=_short_str(raw, "gfs_instance_id", MAX_WIRE_ID_CHARS),
            ts=_short_str(raw, "ts", _MAX_SHORT_FIELD_CHARS),
            signature=_short_str(raw, "signature", _MAX_SHORT_FIELD_CHARS),
            target=_short_str(raw, "target", MAX_WIRE_ID_CHARS),
            epoch=_epoch(raw),
            writer_cert=_cert(raw),
            payload=_payload(raw),
        )


def _nonce(raw: dict) -> str:
    value = raw.get("nonce")
    if (
        not isinstance(value, str)
        or not MIN_NONCE_CHARS <= len(value) <= MAX_NONCE_CHARS
    ):
        raise InvalidMemberPublish("invalid field: nonce")
    return value


@dataclass(slots=True, frozen=True)
class MemberPublishAnonRequest:
    """One strict-mode (anonymous) member-publish request (v_50, see the
    module docstring). Nothing in it names the publishing household."""

    gfs_instance_id: str
    ts: str
    nonce: str
    target: str
    epoch: int
    payload: str
    writer_sig: str
    writer_sig_suite: str

    @property
    def event_type(self) -> str:
        return SPACE_ITEM_EVENT_TYPE

    def signing_body(self) -> dict:
        """Every signed field: the request minus ``writer_sig`` (the suite
        tag is inside the signature)."""
        return {
            "gfs_instance_id": self.gfs_instance_id,
            "ts": self.ts,
            "nonce": self.nonce,
            "target": self.target,
            "event_type": SPACE_ITEM_EVENT_TYPE,
            "epoch": self.epoch,
            "payload": self.payload,
            "writer_sig_suite": self.writer_sig_suite,
        }

    def signing_bytes(self) -> bytes:
        """Canonical, domain-separated bytes the writer group key signs."""
        return MEMBER_PUBLISH_ANON_PREFIX + json.dumps(
            self.signing_body(), separators=(",", ":"), sort_keys=True
        ).encode("utf-8")

    def to_wire(self) -> dict:
        return {**self.signing_body(), "writer_sig": self.writer_sig}

    def fan_out_frame(self) -> dict:
        """The frame the GFS fans out: routing fields + ciphertext only."""
        return {
            "space_id": self.target,
            "event_type": SPACE_ITEM_EVENT_TYPE,
            "epoch": self.epoch,
            "payload": self.payload,
        }

    @classmethod
    def from_wire(cls, raw: object) -> "MemberPublishAnonRequest":
        """Parse a request body; :class:`InvalidMemberPublish` on any
        missing, extra or malformed field — so a body carrying
        ``instance_id``, a household ``signature`` or a ``writer_cert`` is
        refused outright."""
        if not isinstance(raw, dict):
            raise InvalidMemberPublish("expected a JSON object")
        if set(raw) != MEMBER_PUBLISH_ANON_REQUEST_KEYS:
            raise InvalidMemberPublish("unexpected or missing fields")
        _event_type(raw)
        return cls(
            gfs_instance_id=_short_str(raw, "gfs_instance_id", MAX_WIRE_ID_CHARS),
            ts=_short_str(raw, "ts", _MAX_SHORT_FIELD_CHARS),
            nonce=_nonce(raw),
            target=_short_str(raw, "target", MAX_WIRE_ID_CHARS),
            epoch=_epoch(raw),
            payload=_payload(raw),
            writer_sig=_short_str(raw, "writer_sig", _MAX_SHORT_FIELD_CHARS),
            writer_sig_suite=_short_str(raw, "writer_sig_suite", MAX_WIRE_ID_CHARS),
        )


def parse_writer_key_cert(raw: object) -> WriterKeyCert:
    """A ``writer_key_cert`` off an epoch notice, or
    :class:`InvalidMemberPublish`."""
    try:
        return WriterKeyCert.from_wire(raw)
    except ValueError as exc:
        raise InvalidMemberPublish("invalid field: writer_key_cert") from exc


@dataclass(slots=True, frozen=True)
class SpaceItemFrame:
    """A member-published item as a subscriber receives it from the GFS.

    ``writer_cert`` is the plaintext v1 cert of a trusted-mode frame, or
    ``None`` for a strict-mode frame — whose cert rides only inside the
    ciphertext (receivers take it from there either way)."""

    space_id: str
    epoch: int
    writer_cert: WriterCert | None
    payload: str

    def to_wire(self) -> dict:
        wire: dict = {
            "space_id": self.space_id,
            "event_type": SPACE_ITEM_EVENT_TYPE,
            "epoch": self.epoch,
            "payload": self.payload,
        }
        if self.writer_cert is not None:
            wire["writer_cert"] = self.writer_cert.v1().to_wire()
        return wire

    @classmethod
    def from_wire(cls, raw: object) -> "SpaceItemFrame":
        """Parse a fan-out frame. The WS ``type`` key (and any key an older
        or newer GFS adds) is ignored — receivers authenticate the item by
        the cert and the author signature inside, never by outer fields. A
        frame WITHOUT ``writer_cert`` is a strict-mode frame; a present but
        malformed one is refused."""
        if not isinstance(raw, dict):
            raise InvalidMemberPublish("expected a JSON object")
        _event_type(raw)
        return cls(
            space_id=_short_str(raw, "space_id", MAX_WIRE_ID_CHARS),
            epoch=_epoch(raw),
            writer_cert=_cert(raw) if "writer_cert" in raw else None,
            payload=_payload(raw),
        )
