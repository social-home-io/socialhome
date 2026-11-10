"""Member publish over the connection server — wire codec.

A space member household publishes its own item to every connection server
(GFS) the space is listed on, without the host signing it. There is ONE mode
(owner decision 2026-11-09, v_90): the request is ANONYMOUS. It goes to
``POST /gfs/member-publish-anon`` (:class:`MemberPublishAnonRequest`)::

    {"gfs_instance_id", "ts", "nonce", "target", "event_type": "space_item",
     "epoch", "payload", "writer_sig", "writer_sig_suite"}

No ``instance_id``, no household signature, no plaintext writer cert: the
writer cert rides INSIDE the ciphertext, and ``writer_sig`` is made with the
space's per-epoch writer GROUP key (:mod:`socialhome.writer_key`) over
``b"gfs-member-publish-anon:v1:"`` + the canonical JSON of every other field.
The server verifies it against the writer key the space authority pinned for
that epoch, and learns only that *some* publisher of the space posted. It
never learns the content or the real item type: ``payload`` is ciphertext
under the space's epoch content key and carries the real type (post,
comment, …) plus the author-signed inner; the outer ``event_type`` is ALWAYS
the generic :data:`SPACE_ITEM_EVENT_TYPE`. Its fan-out frame::

    {"space_id", "event_type": "space_item", "epoch", "payload"}

Pure module: dataclasses + codec only, importable by the content-blind GFS
process.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..canonical_json import canonical_bytes
from .space_item import (
    PAYLOAD_SUITE_FIELD,
    PAYLOAD_SUITE_JSON,
    UnsupportedPayloadSuite,
    payload_suite_of,
)
from .writer_cert import MAX_WRITER_CERT_EPOCH
from .writer_key import WriterKeyCert

#: The one outer event type a member-published item ever carries. The real
#: type travels inside the ciphertext.
SPACE_ITEM_EVENT_TYPE: str = "space_item"

#: Domain separator of the space OWNER's household-signed epoch notice.
OWNER_EPOCH_NOTICE_ACTION: str = "gfs-owner-epoch-notice:v1"

#: Route of the (anonymous) member publish.
MEMBER_PUBLISH_ANON_ROUTE: str = "/gfs/member-publish-anon"

#: Domain-separation prefix of the writer-key signature over an anonymous
#: publish — distinct from every household and authority prefix.
MEMBER_PUBLISH_ANON_PREFIX: bytes = b"gfs-member-publish-anon:v1:"

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

#: Upper bound on ``ts`` / ``writer_sig`` strings.
_MAX_SHORT_FIELD_CHARS: int = 256

#: Exact key set of an anonymous request — no ``instance_id``, no household
#: ``signature``, no ``writer_cert``.
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

#: Exact key set of a fan-out frame (no cert anywhere outside the
#: ciphertext).
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
    writer_key_cert: dict | None = None,
    gfs_key: str | None = None,
) -> dict:
    """What the space OWNER's household signs to confirm a content epoch at
    one connection server (``POST /gfs/spaces/{id}/epoch``). Canonical JSON of
    this dict (sorted keys, compact) is the signed message; ``action`` is the
    domain separator and ``gfs_instance_id`` binds it to one server.

    ``writer_key_cert`` (optional, signed when present) is the
    authority-signed pin of the writer group key for ``epoch``."""
    payload: dict = {
        "action": OWNER_EPOCH_NOTICE_ACTION,
        "owning_instance": owning_instance,
        "gfs_instance_id": gfs_instance_id,
        "space_id": space_id,
        "epoch": epoch,
        "ts": ts,
    }
    if writer_key_cert is not None:
        payload["writer_key_cert"] = writer_key_cert
    # The addressed server's pinned key, when the household binds it (see
    # ``_with_gfs_key``).
    return _with_gfs_key(gfs_key, payload)


class InvalidMemberPublish(ValueError):
    """The body or frame is not a well-formed member-publish shape."""


_GFS_KEY_RE = re.compile(r"[0-9a-f]{64}")


def _with_gfs_key(gfs_key: str | None, body: dict) -> dict:
    """*body* plus ``gfs_key`` (the addressed server's pinned key, hex) when
    set — inside the signed bytes, binding the request to the server's KEY
    and not only its id. Omitted when ``None`` (an older household's exact
    bytes)."""
    if gfs_key is not None:
        body["gfs_key"] = gfs_key
    return body


def _with_payload_suite(payload_suite: str | None, body: dict) -> dict:
    """*body* plus ``payload_suite`` when set — signed when present, omitted
    when ``None`` (the first-revision bytes, meaning
    :data:`~socialhome.domain.space_item.PAYLOAD_SUITE_JSON`). A household
    sets it only toward a server that proved ``payload_zlib1``; an older one
    refuses the unknown field."""
    if payload_suite is not None:
        body[PAYLOAD_SUITE_FIELD] = payload_suite
    return body


def _payload_suite(raw: dict) -> str | None:
    """The request's or frame's ``payload_suite`` as sent (``None`` when
    absent). An unknown suite is refused — never read as JSON."""
    value = raw.get(PAYLOAD_SUITE_FIELD)
    if value is None:
        return None
    try:
        return payload_suite_of(value)
    except UnsupportedPayloadSuite as exc:
        raise InvalidMemberPublish("invalid field: payload_suite") from exc


def _gfs_key(raw: dict) -> str | None:
    value = raw.get("gfs_key")
    if value is None:
        return None
    if not isinstance(value, str) or not _GFS_KEY_RE.fullmatch(value):
        raise InvalidMemberPublish("invalid field: gfs_key")
    return value


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


def _event_type(raw: dict) -> None:
    if raw.get("event_type") != SPACE_ITEM_EVENT_TYPE:
        raise InvalidMemberPublish("invalid field: event_type")


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
    """One (anonymous) member-publish request (see the module docstring).
    Nothing in it names the publishing household."""

    gfs_instance_id: str
    ts: str
    nonce: str
    target: str
    epoch: int
    payload: str
    writer_sig: str
    writer_sig_suite: str

    #: The PINNED key of the addressed server (hex), signed — an
    #: additive bind beyond the id (``None`` = omitted, older
    #: households). The server refuses a mismatch.
    gfs_key: str | None = None

    #: How ``payload``'s plaintext is encoded (``None`` = omitted: JSON).
    #: Signed when present.
    payload_suite: str | None = None

    @property
    def event_type(self) -> str:
        return SPACE_ITEM_EVENT_TYPE

    @property
    def suite(self) -> str:
        """The payload suite, :data:`PAYLOAD_SUITE_JSON` when omitted."""
        return self.payload_suite or PAYLOAD_SUITE_JSON

    def signing_body(self) -> dict:
        """Every signed field: the request minus ``writer_sig`` (the suite
        tag is inside the signature)."""
        return _with_payload_suite(
            self.payload_suite,
            _with_gfs_key(
                self.gfs_key,
                {
                    "gfs_instance_id": self.gfs_instance_id,
                    "ts": self.ts,
                    "nonce": self.nonce,
                    "target": self.target,
                    "event_type": SPACE_ITEM_EVENT_TYPE,
                    "epoch": self.epoch,
                    "payload": self.payload,
                    "writer_sig_suite": self.writer_sig_suite,
                },
            ),
        )

    def signing_bytes(self) -> bytes:
        """Canonical, domain-separated bytes the writer group key signs."""
        return MEMBER_PUBLISH_ANON_PREFIX + canonical_bytes(self.signing_body())

    def to_wire(self) -> dict:
        return {**self.signing_body(), "writer_sig": self.writer_sig}

    def fan_out_frame(self) -> dict:
        """The frame the GFS fans out: routing fields + ciphertext only."""
        return _with_payload_suite(
            self.payload_suite,
            {
                "space_id": self.target,
                "event_type": SPACE_ITEM_EVENT_TYPE,
                "epoch": self.epoch,
                "payload": self.payload,
            },
        )

    @classmethod
    def from_wire(cls, raw: object) -> MemberPublishAnonRequest:
        """Parse a request body; :class:`InvalidMemberPublish` on any
        missing, extra or malformed field — so a body carrying
        ``instance_id``, a household ``signature`` or a ``writer_cert`` is
        refused outright."""
        if not isinstance(raw, dict):
            raise InvalidMemberPublish("expected a JSON object")
        if (
            not MEMBER_PUBLISH_ANON_REQUEST_KEYS
            <= set(raw)
            <= MEMBER_PUBLISH_ANON_REQUEST_KEYS | {"gfs_key", PAYLOAD_SUITE_FIELD}
        ):
            raise InvalidMemberPublish("unexpected or missing fields")
        _event_type(raw)
        return cls(
            gfs_key=_gfs_key(raw),
            payload_suite=_payload_suite(raw),
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
    The writer cert rides only inside the ciphertext."""

    space_id: str
    epoch: int
    payload: str
    #: How the plaintext is encoded (``None`` = omitted: JSON). Outside the
    #: ciphertext, so a GFS could flip it — which only makes the item
    #: undecodable, never readable as something else (the author signature
    #: inside still decides).
    payload_suite: str | None = None

    @property
    def suite(self) -> str:
        """The payload suite, :data:`PAYLOAD_SUITE_JSON` when omitted."""
        return self.payload_suite or PAYLOAD_SUITE_JSON

    def to_wire(self) -> dict:
        wire: dict = {
            "space_id": self.space_id,
            "event_type": SPACE_ITEM_EVENT_TYPE,
            "epoch": self.epoch,
            "payload": self.payload,
        }
        return _with_payload_suite(self.payload_suite, wire)

    @classmethod
    def from_wire(cls, raw: object) -> SpaceItemFrame:
        """Parse a fan-out frame. The WS ``type`` key (and any key an older
        or newer GFS adds) is ignored — receivers authenticate the item by
        the cert and the author signature inside, never by outer fields."""
        if not isinstance(raw, dict):
            raise InvalidMemberPublish("expected a JSON object")
        _event_type(raw)
        return cls(
            space_id=_short_str(raw, "space_id", MAX_WIRE_ID_CHARS),
            epoch=_epoch(raw),
            payload=_payload(raw),
            payload_suite=_payload_suite(raw),
        )
