"""Opaque GFS channels for PRIVATE spaces — wire shapes and codecs (v_51).

A private space whose members include households seated through an invite
link (``InstanceSource.SPACE_SESSION`` — they reach the host only over the
connection server's ``/gfs/envelope`` relay) gets member publishing over the
connection server through an **opaque channel**, so members reach each other
while the host is offline. The connection server must never learn which
space a channel belongs to: no ``space_id``, no name, no space authority
public key ever appears in channel traffic.

* ``channel_id`` — 128 random bits (32 lowercase hex chars), minted by the
  space owner. Never derived from the ``space_id``; it reaches member
  households only inside the encrypted per-peer payloads that already carry
  the content key (redeem ACK, rekey, roster snapshot, rotation bundle).
* The **channel key** — an Ed25519 key HKDF-derived from the space authority
  seed with its own domain separation (:mod:`socialhome.gfs_channel`), so the
  server pins ``channel_pk`` and never sees the space key; HKDF is one-way,
  so nobody without the seed can link the two.

Every statement the channel key signs carries ``channel_suite`` and its own
domain-separation prefix (see :mod:`socialhome.gfs_channel`):

* :class:`ChannelCert` — "household ``instance_pk`` may publish at ``scope``
  in this channel during epoch ``epoch``" (trusted mode; the server checks it
  against the pinned ``channel_pk``)::

      {channel_suite, channel_id, epoch, instance_pk, scope, issued_at, cert_sig}

* :class:`ChannelPass` — "household ``instance_pk`` is a member at epoch
  ``epoch``" (no scope, so strict mode never tells the server who may
  write); a subscribe must present one::

      {channel_suite, channel_id, epoch, instance_pk, issued_at, pass_sig}

* :class:`ChannelWriterKeyCert` — the pin of one epoch's channel writer group
  key (strict mode)::

      {writer_key_suite, channel_suite, channel_id, epoch, writer_pk, cert_sig}

What a member household is handed (SECRET — it rides only inside a per-peer
encrypted payload, and is stored KEK-wrapped), :class:`GfsChannelGrant`::

    {channel_suite, space_id, channel_id, channel_pk, epoch, epoch_offset,
     gfs_ids: [...],
     binding_sig_suite, binding_sig,            # the SPACE authority key
     channel_pass?: {...},                      # link-joined households only
     channel_cert?: {...},                      # writers, trusted spaces
     writer_key?: {writer_key_suite, channel_id, epoch, writer_seed,
                   writer_key_cert}}            # strict spaces, writers only

The ``binding_sig`` (space authority key over ``{channel_suite, space_id,
channel_id, channel_pk, epoch, epoch_offset, gfs_ids, binding_sig_suite}``)
lets a member verify the grant against the space key it already pins, so no
other member can hand it a channel of their choosing.

**Epochs on the wire are channel epochs**: ``epoch`` (the space content
epoch, local) plus a per-channel ``epoch_offset`` (40 bits, HKDF-derived
from the seed). Every pass, cert, writer key, notice, request and frame
carries the channel epoch, so a server that saw the space's content epochs
elsewhere (a space that was public once, a post-restore epoch in unix
seconds) cannot match them to the channel.

Requests to the connection server carry every identifier in the BODY, never
in the URL path, so an access log holds no channel ids:

* ``POST /gfs/channels/register`` (anonymous, channel-key-signed) —
  :class:`ChannelRegisterRequest`;
* ``POST /gfs/channels/epoch`` (anonymous, channel-key-signed) —
  :class:`ChannelEpochNotice`;
* ``POST /gfs/channels/unregister`` (anonymous, channel-key-signed) —
  :class:`ChannelUnregisterRequest`;
* ``POST /gfs/channels/subscribe`` / ``unsubscribe`` (household-signed, with a
  :class:`ChannelPass`) — :class:`ChannelSubscribeRequest` /
  :class:`ChannelUnsubscribeRequest`;
* ``POST /gfs/channels/publish`` (trusted: household-signed + channel cert) —
  :class:`ChannelPublishRequest`;
* ``POST /gfs/channels/publish-anon`` (strict: channel writer key) —
  :class:`ChannelPublishAnonRequest`.

Fan-out frame (``{"type": "relay", **frame}``), identical in both modes::

    {"channel_id", "event_type": "space_item", "epoch", "payload"}

Pure module: dataclasses + codecs only, importable by the content-blind GFS
process.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .gfs_member_publish import (
    GFS_PUBLISH_MODES,
    MAX_NONCE_CHARS,
    MEMBER_PUBLISH_MAX_PAYLOAD_CHARS,
    MIN_NONCE_CHARS,
    SPACE_ITEM_EVENT_TYPE,
)
from .writer_cert import MAX_WRITER_CERT_EPOCH, WRITER_SCOPES

#: Suite of the channel key — of ``channel_pk`` and of every statement it
#: signs (registration, notice, unregister, cert, pass, writer key cert,
#: ). Phase-2 of ``docs/crypto.md`` adds ``"ed25519+mldsa65"``.
CHANNEL_SUITE_ED25519: str = "ed25519"
SUPPORTED_CHANNEL_SUITES: frozenset[str] = frozenset({CHANNEL_SUITE_ED25519})

#: Suite of the SPACE authority signature binding a grant to its space.
CHANNEL_BINDING_SUITE_ED25519: str = "ed25519"
SUPPORTED_CHANNEL_BINDING_SUITES: frozenset[str] = frozenset(
    {CHANNEL_BINDING_SUITE_ED25519}
)

#: Hex characters of a channel id (128 random bits).
CHANNEL_ID_HEX_CHARS: int = 32

#: The wire key a channel grant rides under in a per-peer payload.
GFS_CHANNEL_FIELD: str = "gfs_channel"

#: Routes (every identifier rides in the body).
CHANNEL_REGISTER_ROUTE: str = "/gfs/channels/register"
CHANNEL_EPOCH_ROUTE: str = "/gfs/channels/epoch"
CHANNEL_UNREGISTER_ROUTE: str = "/gfs/channels/unregister"
CHANNEL_SUBSCRIBE_ROUTE: str = "/gfs/channels/subscribe"
CHANNEL_UNSUBSCRIBE_ROUTE: str = "/gfs/channels/unsubscribe"
CHANNEL_PUBLISH_ROUTE: str = "/gfs/channels/publish"
CHANNEL_PUBLISH_ANON_ROUTE: str = "/gfs/channels/publish-anon"
CHANNEL_ROUTES: frozenset[str] = frozenset(
    {
        CHANNEL_REGISTER_ROUTE,
        CHANNEL_EPOCH_ROUTE,
        CHANNEL_UNREGISTER_ROUTE,
        CHANNEL_SUBSCRIBE_ROUTE,
        CHANNEL_UNSUBSCRIBE_ROUTE,
        CHANNEL_PUBLISH_ROUTE,
        CHANNEL_PUBLISH_ANON_ROUTE,
    }
)

#: ``action`` domain separators of the HOUSEHOLD-signed requests (the same
#: canonical-JSON scheme as every signed household→GFS request).
CHANNEL_SUBSCRIBE_ACTION: str = "gfs-channel-subscribe:v1"
CHANNEL_UNSUBSCRIBE_ACTION: str = "gfs-channel-unsubscribe:v1"
CHANNEL_PUBLISH_ACTION: str = "gfs-channel-publish:v1"

#: Largest number of connection servers one grant may name.
MAX_GRANT_GFS_IDS: int = 16

#: Upper bound on identifier-shaped fields and short strings.
_MAX_ID_CHARS: int = 128
_MAX_SHORT_CHARS: int = 256
#: Longest base64url key / signature string, with room for a hybrid suite.
_MAX_B64_CHARS: int = 4096

_HEX_ID = re.compile(r"^[0-9a-f]{32}$")


class InvalidChannelWire(ValueError):
    """A channel body, cert, pass, grant or frame is malformed."""


class UnsupportedChannelSuite(ValueError):
    """A channel statement names a suite this build does not know. Rejected
    — never defaulted to Ed25519."""


def valid_channel_id(value: object) -> str:
    """``value`` as a channel id (exactly 32 lowercase hex chars), else
    :class:`InvalidChannelWire`."""
    if not isinstance(value, str) or not _HEX_ID.match(value):
        raise InvalidChannelWire("invalid field: channel_id")
    return value


def canonical(body: dict) -> bytes:
    """Canonical JSON (sorted keys, compact) — the encoding every signature
    in this module covers."""
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str(raw: dict, name: str, limit: int = _MAX_SHORT_CHARS) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value or len(value) > limit:
        raise InvalidChannelWire(f"invalid field: {name}")
    return value


def _b64(raw: dict, name: str) -> str:
    return _str(raw, name, _MAX_B64_CHARS)


def _epoch(raw: dict, name: str = "epoch") -> int:
    value = raw.get(name)
    if not _is_int(value) or not 0 <= value <= MAX_WRITER_CERT_EPOCH:  # type: ignore[operator]
        raise InvalidChannelWire(f"invalid field: {name}")
    return int(value)  # type: ignore[arg-type]


def _exact(raw: object, keys: frozenset[str], what: str) -> dict:
    if not isinstance(raw, dict) or set(raw) != keys:
        raise InvalidChannelWire(f"{what}: unexpected or missing fields")
    return raw


def _exact_optional(
    raw: object, required: frozenset[str], optional: frozenset[str], what: str
) -> dict:
    if not isinstance(raw, dict):
        raise InvalidChannelWire(f"{what}: expected a JSON object")
    keys = set(raw)
    if not required <= keys or not keys <= required | optional:
        raise InvalidChannelWire(f"{what}: unexpected or missing fields")
    return raw


#: The optional, signed addressee-key field every household request may
#: carry (see ``_with_gfs_key``).
GFS_KEY_FIELD: frozenset[str] = frozenset({"gfs_key"})
_GFS_KEY_RE = re.compile(r"[0-9a-f]{64}")


def _with_gfs_key(gfs_key: str | None, body: dict) -> dict:
    """*body* plus ``gfs_key`` when set — inside the signed bytes, so the
    request is bound to the addressed server's KEY, not only its id (an id
    may be another operator's alias). Omitted when ``None``: the exact bytes
    an older household signs."""
    if gfs_key is not None:
        body["gfs_key"] = gfs_key
    return body


def _gfs_key(raw: dict) -> str | None:
    value = raw.get("gfs_key")
    if value is None:
        return None
    if not isinstance(value, str) or not _GFS_KEY_RE.fullmatch(value):
        raise InvalidChannelWire("invalid field: gfs_key")
    return value


def _nonce(raw: dict) -> str:
    value = raw.get("nonce")
    if (
        not isinstance(value, str)
        or not MIN_NONCE_CHARS <= len(value) <= MAX_NONCE_CHARS
    ):
        raise InvalidChannelWire("invalid field: nonce")
    return value


def _event_type(raw: dict) -> None:
    if raw.get("event_type") != SPACE_ITEM_EVENT_TYPE:
        raise InvalidChannelWire("invalid field: event_type")


def _payload(raw: dict) -> str:
    return _str(raw, "payload", MEMBER_PUBLISH_MAX_PAYLOAD_CHARS)


# ─── Channel-key statements ──────────────────────────────────────────────

CHANNEL_CERT_KEYS: frozenset[str] = frozenset(
    {
        "channel_suite",
        "channel_id",
        "epoch",
        "instance_pk",
        "scope",
        "issued_at",
        "cert_sig",
    }
)


@dataclass(slots=True, frozen=True)
class ChannelCert:
    """The channel key's per-epoch writer statement for one household."""

    channel_suite: str
    channel_id: str
    epoch: int
    instance_pk: str
    scope: str
    issued_at: int
    cert_sig: str

    def signing_body(self) -> dict:
        return {
            "channel_suite": self.channel_suite,
            "channel_id": self.channel_id,
            "epoch": self.epoch,
            "instance_pk": self.instance_pk,
            "scope": self.scope,
            "issued_at": self.issued_at,
        }

    def to_wire(self) -> dict:
        return {**self.signing_body(), "cert_sig": self.cert_sig}

    @classmethod
    def from_wire(cls, raw: object) -> "ChannelCert":
        body = _exact(raw, CHANNEL_CERT_KEYS, "channel cert")
        scope = body.get("scope")
        if scope not in WRITER_SCOPES:
            raise InvalidChannelWire("channel cert: bad scope")
        issued_at = body.get("issued_at")
        if not _is_int(issued_at):
            raise InvalidChannelWire("channel cert: bad issued_at")
        return cls(
            channel_suite=_str(body, "channel_suite", _MAX_ID_CHARS),
            channel_id=valid_channel_id(body.get("channel_id")),
            epoch=_epoch(body),
            instance_pk=_b64(body, "instance_pk"),
            scope=str(scope),
            issued_at=int(issued_at),  # type: ignore[arg-type]
            cert_sig=_b64(body, "cert_sig"),
        )


CHANNEL_PASS_KEYS: frozenset[str] = frozenset(
    {"channel_suite", "channel_id", "epoch", "instance_pk", "issued_at", "pass_sig"}
)


@dataclass(slots=True, frozen=True)
class ChannelPass:
    """The channel key's per-epoch membership statement for one household —
    what a subscribe presents. Deliberately scope-free."""

    channel_suite: str
    channel_id: str
    epoch: int
    instance_pk: str
    issued_at: int
    pass_sig: str

    def signing_body(self) -> dict:
        return {
            "channel_suite": self.channel_suite,
            "channel_id": self.channel_id,
            "epoch": self.epoch,
            "instance_pk": self.instance_pk,
            "issued_at": self.issued_at,
        }

    def to_wire(self) -> dict:
        return {**self.signing_body(), "pass_sig": self.pass_sig}

    @classmethod
    def from_wire(cls, raw: object) -> "ChannelPass":
        body = _exact(raw, CHANNEL_PASS_KEYS, "channel pass")
        issued_at = body.get("issued_at")
        if not _is_int(issued_at):
            raise InvalidChannelWire("channel pass: bad issued_at")
        return cls(
            channel_suite=_str(body, "channel_suite", _MAX_ID_CHARS),
            channel_id=valid_channel_id(body.get("channel_id")),
            epoch=_epoch(body),
            instance_pk=_b64(body, "instance_pk"),
            issued_at=int(issued_at),  # type: ignore[arg-type]
            pass_sig=_b64(body, "pass_sig"),
        )


CHANNEL_WRITER_KEY_CERT_KEYS: frozenset[str] = frozenset(
    {
        "writer_key_suite",
        "channel_suite",
        "channel_id",
        "epoch",
        "writer_pk",
        "cert_sig",
    }
)


@dataclass(slots=True, frozen=True)
class ChannelWriterKeyCert:
    """The channel key's pin of one epoch's channel writer group key.
    ``writer_key_suite`` is the writer key's algorithm; ``channel_suite``
    the suite of the channel-key signature over the pin."""

    writer_key_suite: str
    channel_suite: str
    channel_id: str
    epoch: int
    writer_pk: str
    cert_sig: str

    def signing_body(self) -> dict:
        return {
            "writer_key_suite": self.writer_key_suite,
            "channel_suite": self.channel_suite,
            "channel_id": self.channel_id,
            "epoch": self.epoch,
            "writer_pk": self.writer_pk,
        }

    def to_wire(self) -> dict:
        return {**self.signing_body(), "cert_sig": self.cert_sig}

    @classmethod
    def from_wire(cls, raw: object) -> "ChannelWriterKeyCert":
        body = _exact(raw, CHANNEL_WRITER_KEY_CERT_KEYS, "channel writer key cert")
        return cls(
            writer_key_suite=_str(body, "writer_key_suite", _MAX_ID_CHARS),
            channel_suite=_str(body, "channel_suite", _MAX_ID_CHARS),
            channel_id=valid_channel_id(body.get("channel_id")),
            epoch=_epoch(body),
            writer_pk=_b64(body, "writer_pk"),
            cert_sig=_b64(body, "cert_sig"),
        )


CHANNEL_WRITER_KEY_GRANT_KEYS: frozenset[str] = frozenset(
    {"writer_key_suite", "channel_id", "epoch", "writer_seed", "writer_key_cert"}
)


@dataclass(slots=True, frozen=True)
class ChannelWriterKeyGrant:
    """One epoch's channel writer key (private seed + its channel-key pin).
    SECRET: inside a grant only."""

    writer_key_suite: str
    channel_id: str
    epoch: int
    writer_seed: str
    writer_key_cert: ChannelWriterKeyCert

    def to_wire(self) -> dict:
        return {
            "writer_key_suite": self.writer_key_suite,
            "channel_id": self.channel_id,
            "epoch": self.epoch,
            "writer_seed": self.writer_seed,
            "writer_key_cert": self.writer_key_cert.to_wire(),
        }

    @classmethod
    def from_wire(cls, raw: object) -> "ChannelWriterKeyGrant":
        body = _exact(raw, CHANNEL_WRITER_KEY_GRANT_KEYS, "channel writer key")
        return cls(
            writer_key_suite=_str(body, "writer_key_suite", _MAX_ID_CHARS),
            channel_id=valid_channel_id(body.get("channel_id")),
            epoch=_epoch(body),
            writer_seed=_b64(body, "writer_seed"),
            writer_key_cert=ChannelWriterKeyCert.from_wire(body.get("writer_key_cert")),
        )


# ─── The grant a member household holds ──────────────────────────────────

GRANT_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "channel_suite",
        "space_id",
        "channel_id",
        "channel_pk",
        "epoch",
        "epoch_offset",
        "gfs_ids",
        "binding_sig_suite",
        "binding_sig",
    }
)

#: Largest per-channel epoch offset (40 bits): wire epochs stay far below
#: the signed 64-bit ceiling.
MAX_CHANNEL_EPOCH_OFFSET: int = 2**40
GRANT_OPTIONAL_KEYS: frozenset[str] = frozenset(
    {"channel_pass", "channel_cert", "writer_key"}
)


@dataclass(slots=True, frozen=True)
class GfsChannelGrant:
    """What a seed holder delivers ONE member household for one epoch (see
    the module docstring). SECRET when it carries ``writer_key``."""

    channel_suite: str
    space_id: str
    channel_id: str
    channel_pk: str
    epoch: int
    epoch_offset: int
    gfs_ids: tuple[str, ...]
    binding_sig_suite: str
    binding_sig: str
    #: Only for a link-joined (``space_session``) household: the seats are
    #: exactly those. A paired member's grant is publish-only.
    channel_pass: ChannelPass | None = None
    channel_cert: ChannelCert | None = None
    writer_key: ChannelWriterKeyGrant | None = field(default=None)

    @property
    def channel_epoch(self) -> int:
        """The epoch on the wire (pass, cert, writer key, requests, frames):
        the content epoch shifted by the channel's secret offset, so the
        server cannot match it to an epoch it saw for the space elsewhere."""
        return self.epoch + self.epoch_offset

    def binding_body(self) -> dict:
        """What the SPACE authority signature covers."""
        return {
            "channel_suite": self.channel_suite,
            "space_id": self.space_id,
            "channel_id": self.channel_id,
            "channel_pk": self.channel_pk,
            "epoch": self.epoch,
            "epoch_offset": self.epoch_offset,
            "gfs_ids": sorted(self.gfs_ids),
            "binding_sig_suite": self.binding_sig_suite,
        }

    def to_wire(self) -> dict:
        wire: dict = {**self.binding_body(), "binding_sig": self.binding_sig}
        if self.channel_pass is not None:
            wire["channel_pass"] = self.channel_pass.to_wire()
        if self.channel_cert is not None:
            wire["channel_cert"] = self.channel_cert.to_wire()
        if self.writer_key is not None:
            wire["writer_key"] = self.writer_key.to_wire()
        return wire

    @classmethod
    def from_wire(cls, raw: object) -> "GfsChannelGrant":
        body = _exact_optional(raw, GRANT_REQUIRED_KEYS, GRANT_OPTIONAL_KEYS, "grant")
        gfs_ids = body.get("gfs_ids")
        if (
            not isinstance(gfs_ids, list)
            or not 0 < len(gfs_ids) <= MAX_GRANT_GFS_IDS
            or not all(
                isinstance(g, str) and 0 < len(g) <= _MAX_ID_CHARS for g in gfs_ids
            )
        ):
            raise InvalidChannelWire("grant: bad gfs_ids")
        cert = body.get("channel_cert")
        key = body.get("writer_key")
        passport = body.get("channel_pass")
        offset = body.get("epoch_offset")
        if not _is_int(offset) or not 0 <= offset <= MAX_CHANNEL_EPOCH_OFFSET:  # type: ignore[operator]
            raise InvalidChannelWire("grant: bad epoch_offset")
        return cls(
            channel_suite=_str(body, "channel_suite", _MAX_ID_CHARS),
            space_id=_str(body, "space_id", _MAX_ID_CHARS),
            channel_id=valid_channel_id(body.get("channel_id")),
            channel_pk=_b64(body, "channel_pk"),
            epoch=_epoch(body),
            epoch_offset=int(offset),  # type: ignore[arg-type]
            gfs_ids=tuple(sorted(set(gfs_ids))),
            binding_sig_suite=_str(body, "binding_sig_suite", _MAX_ID_CHARS),
            binding_sig=_b64(body, "binding_sig"),
            channel_pass=(
                ChannelPass.from_wire(passport) if passport is not None else None
            ),
            channel_cert=ChannelCert.from_wire(cert) if cert is not None else None,
            writer_key=(
                ChannelWriterKeyGrant.from_wire(key) if key is not None else None
            ),
        )


# ─── Channel-key-signed requests (anonymous at the server) ───────────────

REGISTER_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "channel_suite",
        "channel_id",
        "channel_pk",
        "gfs_instance_id",
        "ts",
        "nonce",
        "channel_sig",
    }
)


@dataclass(slots=True, frozen=True)
class ChannelRegisterRequest:
    """``POST /gfs/channels/register`` — pins ``channel_pk`` for a NEW
    ``channel_id`` (trust on first use), refreshes an existing one with the
    same key. There is no re-pin: another key for a pinned id is refused
    (a household starts a fresh channel instead), and a body carrying a
    ``repin_cert`` is malformed.
    ``channel_sig`` is made with the key being registered (proof of
    possession)."""

    channel_suite: str
    channel_id: str
    channel_pk: str
    gfs_instance_id: str
    ts: str
    nonce: str
    channel_sig: str
    #: The PINNED key of the addressed server (hex), signed — an
    #: additive bind beyond the id (``None`` = omitted, older
    #: households). The server refuses a mismatch.
    gfs_key: str | None = None

    def signing_body(self) -> dict:
        return _with_gfs_key(
            self.gfs_key,
            {
                "channel_suite": self.channel_suite,
                "channel_id": self.channel_id,
                "channel_pk": self.channel_pk,
                "gfs_instance_id": self.gfs_instance_id,
                "ts": self.ts,
                "nonce": self.nonce,
            },
        )

    def to_wire(self) -> dict:
        return {**self.signing_body(), "channel_sig": self.channel_sig}

    @classmethod
    def from_wire(cls, raw: object) -> "ChannelRegisterRequest":
        body = _exact_optional(raw, REGISTER_REQUIRED_KEYS, GFS_KEY_FIELD, "register")
        return cls(
            gfs_key=_gfs_key(body),
            channel_suite=_str(body, "channel_suite", _MAX_ID_CHARS),
            channel_id=valid_channel_id(body.get("channel_id")),
            channel_pk=_b64(body, "channel_pk"),
            gfs_instance_id=_str(body, "gfs_instance_id", _MAX_ID_CHARS),
            ts=_str(body, "ts"),
            nonce=_nonce(body),
            channel_sig=_b64(body, "channel_sig"),
        )


NOTICE_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "channel_suite",
        "channel_id",
        "gfs_instance_id",
        "ts",
        "nonce",
        "epoch",
        "publish_mode",
        "channel_sig",
    }
)


@dataclass(slots=True, frozen=True)
class ChannelEpochNotice:
    """``POST /gfs/channels/epoch`` — the channel's content epoch and publish
    mode, and optionally the channel writer key pin of that epoch, signed by
    the channel key. There is no owner/delegated distinction: the server
    cannot tell (and must not learn) which household holds the seed."""

    channel_suite: str
    channel_id: str
    gfs_instance_id: str
    ts: str
    nonce: str
    epoch: int
    publish_mode: str
    channel_sig: str
    writer_key_cert: ChannelWriterKeyCert | None = None
    #: The PINNED key of the addressed server (hex), signed — an
    #: additive bind beyond the id (``None`` = omitted, older
    #: households). The server refuses a mismatch.
    gfs_key: str | None = None

    def signing_body(self) -> dict:
        body: dict = {
            "channel_suite": self.channel_suite,
            "channel_id": self.channel_id,
            "gfs_instance_id": self.gfs_instance_id,
            "ts": self.ts,
            "nonce": self.nonce,
            "epoch": self.epoch,
            "publish_mode": self.publish_mode,
        }
        if self.writer_key_cert is not None:
            body["writer_key_cert"] = self.writer_key_cert.to_wire()
        return _with_gfs_key(self.gfs_key, body)

    def to_wire(self) -> dict:
        return {**self.signing_body(), "channel_sig": self.channel_sig}

    @classmethod
    def from_wire(cls, raw: object) -> "ChannelEpochNotice":
        body = _exact_optional(
            raw,
            NOTICE_REQUIRED_KEYS,
            frozenset({"writer_key_cert"}) | GFS_KEY_FIELD,
            "notice",
        )
        mode = body.get("publish_mode")
        if mode not in GFS_PUBLISH_MODES:
            raise InvalidChannelWire("invalid field: publish_mode")
        wkc = body.get("writer_key_cert")
        return cls(
            gfs_key=_gfs_key(body),
            channel_suite=_str(body, "channel_suite", _MAX_ID_CHARS),
            channel_id=valid_channel_id(body.get("channel_id")),
            gfs_instance_id=_str(body, "gfs_instance_id", _MAX_ID_CHARS),
            ts=_str(body, "ts"),
            nonce=_nonce(body),
            epoch=_epoch(body),
            publish_mode=str(mode),
            channel_sig=_b64(body, "channel_sig"),
            writer_key_cert=(
                ChannelWriterKeyCert.from_wire(wkc) if wkc is not None else None
            ),
        )


UNREGISTER_KEYS: frozenset[str] = frozenset(
    {"channel_suite", "channel_id", "gfs_instance_id", "ts", "nonce", "channel_sig"}
)


@dataclass(slots=True, frozen=True)
class ChannelUnregisterRequest:
    """``POST /gfs/channels/unregister`` — the channel key retires the
    channel; the server drops it and every subscription to it."""

    channel_suite: str
    channel_id: str
    gfs_instance_id: str
    ts: str
    nonce: str
    channel_sig: str
    #: The PINNED key of the addressed server (hex), signed — an
    #: additive bind beyond the id (``None`` = omitted, older
    #: households). The server refuses a mismatch.
    gfs_key: str | None = None

    def signing_body(self) -> dict:
        return _with_gfs_key(
            self.gfs_key,
            {
                "channel_suite": self.channel_suite,
                "channel_id": self.channel_id,
                "gfs_instance_id": self.gfs_instance_id,
                "ts": self.ts,
                "nonce": self.nonce,
            },
        )

    def to_wire(self) -> dict:
        return {**self.signing_body(), "channel_sig": self.channel_sig}

    @classmethod
    def from_wire(cls, raw: object) -> "ChannelUnregisterRequest":
        body = _exact_optional(raw, UNREGISTER_KEYS, GFS_KEY_FIELD, "unregister")
        return cls(
            gfs_key=_gfs_key(body),
            channel_suite=_str(body, "channel_suite", _MAX_ID_CHARS),
            channel_id=valid_channel_id(body.get("channel_id")),
            gfs_instance_id=_str(body, "gfs_instance_id", _MAX_ID_CHARS),
            ts=_str(body, "ts"),
            nonce=_nonce(body),
            channel_sig=_b64(body, "channel_sig"),
        )


# ─── Household-signed requests ───────────────────────────────────────────

SUBSCRIBE_KEYS: frozenset[str] = frozenset(
    {"instance_id", "gfs_instance_id", "channel_id", "ts", "signature", "channel_pass"}
)


@dataclass(slots=True, frozen=True)
class ChannelSubscribeRequest:
    """``POST /gfs/channels/subscribe`` — a member household (identified, as
    a follower's subscribe is) takes a fan-out seat, proving membership
    with its :class:`ChannelPass` for an open epoch."""

    instance_id: str
    gfs_instance_id: str
    channel_id: str
    ts: str
    signature: str
    channel_pass: ChannelPass
    #: The PINNED key of the addressed server (hex), signed — an
    #: additive bind beyond the id (``None`` = omitted, older
    #: households). The server refuses a mismatch.
    gfs_key: str | None = None

    def signing_payload(self) -> dict:
        return _with_gfs_key(
            self.gfs_key,
            {
                "action": CHANNEL_SUBSCRIBE_ACTION,
                "instance_id": self.instance_id,
                "gfs_instance_id": self.gfs_instance_id,
                "channel_id": self.channel_id,
                "ts": self.ts,
                "channel_pass": self.channel_pass.to_wire(),
            },
        )

    def to_wire(self) -> dict:
        body = self.signing_payload()
        del body["action"]
        body["signature"] = self.signature
        return body

    @classmethod
    def from_wire(cls, raw: object) -> "ChannelSubscribeRequest":
        body = _exact_optional(raw, SUBSCRIBE_KEYS, GFS_KEY_FIELD, "subscribe")
        return cls(
            gfs_key=_gfs_key(body),
            instance_id=_str(body, "instance_id", _MAX_ID_CHARS),
            gfs_instance_id=_str(body, "gfs_instance_id", _MAX_ID_CHARS),
            channel_id=valid_channel_id(body.get("channel_id")),
            ts=_str(body, "ts"),
            signature=_str(body, "signature"),
            channel_pass=ChannelPass.from_wire(body.get("channel_pass")),
        )


UNSUBSCRIBE_KEYS: frozenset[str] = frozenset(
    {"instance_id", "gfs_instance_id", "channel_id", "ts", "signature"}
)


@dataclass(slots=True, frozen=True)
class ChannelUnsubscribeRequest:
    """``POST /gfs/channels/unsubscribe`` — drop our own fan-out seat."""

    instance_id: str
    gfs_instance_id: str
    channel_id: str
    ts: str
    signature: str
    #: The PINNED key of the addressed server (hex), signed — an
    #: additive bind beyond the id (``None`` = omitted, older
    #: households). The server refuses a mismatch.
    gfs_key: str | None = None

    def signing_payload(self) -> dict:
        return _with_gfs_key(
            self.gfs_key,
            {
                "action": CHANNEL_UNSUBSCRIBE_ACTION,
                "instance_id": self.instance_id,
                "gfs_instance_id": self.gfs_instance_id,
                "channel_id": self.channel_id,
                "ts": self.ts,
            },
        )

    def to_wire(self) -> dict:
        body = self.signing_payload()
        del body["action"]
        body["signature"] = self.signature
        return body

    @classmethod
    def from_wire(cls, raw: object) -> "ChannelUnsubscribeRequest":
        body = _exact_optional(raw, UNSUBSCRIBE_KEYS, GFS_KEY_FIELD, "unsubscribe")
        return cls(
            gfs_key=_gfs_key(body),
            instance_id=_str(body, "instance_id", _MAX_ID_CHARS),
            gfs_instance_id=_str(body, "gfs_instance_id", _MAX_ID_CHARS),
            channel_id=valid_channel_id(body.get("channel_id")),
            ts=_str(body, "ts"),
            signature=_str(body, "signature"),
        )


PUBLISH_KEYS: frozenset[str] = frozenset(
    {
        "instance_id",
        "gfs_instance_id",
        "channel_id",
        "ts",
        "signature",
        "event_type",
        "epoch",
        "channel_cert",
        "payload",
    }
)


@dataclass(slots=True, frozen=True)
class ChannelPublishRequest:
    """``POST /gfs/channels/publish`` — trusted mode: identified by the
    household signature, authorized by a :class:`ChannelCert` for this
    channel — which names no space."""

    instance_id: str
    gfs_instance_id: str
    channel_id: str
    ts: str
    signature: str
    epoch: int
    channel_cert: ChannelCert
    payload: str
    #: The PINNED key of the addressed server (hex), signed — an
    #: additive bind beyond the id (``None`` = omitted, older
    #: households). The server refuses a mismatch.
    gfs_key: str | None = None

    def signing_payload(self) -> dict:
        return _with_gfs_key(
            self.gfs_key,
            {
                "action": CHANNEL_PUBLISH_ACTION,
                "instance_id": self.instance_id,
                "gfs_instance_id": self.gfs_instance_id,
                "channel_id": self.channel_id,
                "ts": self.ts,
                "event_type": SPACE_ITEM_EVENT_TYPE,
                "epoch": self.epoch,
                "channel_cert": self.channel_cert.to_wire(),
                "payload": self.payload,
            },
        )

    def signing_bytes(self) -> bytes:
        return canonical(self.signing_payload())

    def to_wire(self) -> dict:
        body = self.signing_payload()
        del body["action"]
        body["signature"] = self.signature
        return body

    def fan_out_frame(self) -> dict:
        return channel_frame(self.channel_id, self.epoch, self.payload)

    @classmethod
    def from_wire(cls, raw: object) -> "ChannelPublishRequest":
        body = _exact_optional(raw, PUBLISH_KEYS, GFS_KEY_FIELD, "publish")
        _event_type(body)
        return cls(
            gfs_key=_gfs_key(body),
            instance_id=_str(body, "instance_id", _MAX_ID_CHARS),
            gfs_instance_id=_str(body, "gfs_instance_id", _MAX_ID_CHARS),
            channel_id=valid_channel_id(body.get("channel_id")),
            ts=_str(body, "ts"),
            signature=_str(body, "signature"),
            epoch=_epoch(body),
            channel_cert=ChannelCert.from_wire(body.get("channel_cert")),
            payload=_payload(body),
        )


PUBLISH_ANON_KEYS: frozenset[str] = frozenset(
    {
        "gfs_instance_id",
        "channel_id",
        "ts",
        "nonce",
        "event_type",
        "epoch",
        "payload",
        "writer_sig",
        "writer_sig_suite",
    }
)


@dataclass(slots=True, frozen=True)
class ChannelPublishAnonRequest:
    """``POST /gfs/channels/publish-anon`` — strict mode: no household
    identity, signed with the channel's per-epoch writer group key."""

    gfs_instance_id: str
    channel_id: str
    ts: str
    nonce: str
    epoch: int
    payload: str
    writer_sig: str
    writer_sig_suite: str
    #: The PINNED key of the addressed server (hex), signed — an
    #: additive bind beyond the id (``None`` = omitted, older
    #: households). The server refuses a mismatch.
    gfs_key: str | None = None

    def signing_body(self) -> dict:
        return _with_gfs_key(
            self.gfs_key,
            {
                "gfs_instance_id": self.gfs_instance_id,
                "channel_id": self.channel_id,
                "ts": self.ts,
                "nonce": self.nonce,
                "event_type": SPACE_ITEM_EVENT_TYPE,
                "epoch": self.epoch,
                "payload": self.payload,
                "writer_sig_suite": self.writer_sig_suite,
            },
        )

    def to_wire(self) -> dict:
        return {**self.signing_body(), "writer_sig": self.writer_sig}

    def fan_out_frame(self) -> dict:
        return channel_frame(self.channel_id, self.epoch, self.payload)

    @classmethod
    def from_wire(cls, raw: object) -> "ChannelPublishAnonRequest":
        body = _exact_optional(raw, PUBLISH_ANON_KEYS, GFS_KEY_FIELD, "publish-anon")
        _event_type(body)
        return cls(
            gfs_key=_gfs_key(body),
            gfs_instance_id=_str(body, "gfs_instance_id", _MAX_ID_CHARS),
            channel_id=valid_channel_id(body.get("channel_id")),
            ts=_str(body, "ts"),
            nonce=_nonce(body),
            epoch=_epoch(body),
            payload=_payload(body),
            writer_sig=_str(body, "writer_sig"),
            writer_sig_suite=_str(body, "writer_sig_suite", _MAX_ID_CHARS),
        )


# ─── Fan-out frame ───────────────────────────────────────────────────────

#: Exact key set of a channel fan-out frame (the WS push adds ``type``).
CHANNEL_FRAME_KEYS: frozenset[str] = frozenset(
    {"channel_id", "event_type", "epoch", "payload"}
)


def channel_frame(channel_id: str, epoch: int, payload: str) -> dict:
    """The identity-free frame the server fans out — the same in both
    modes: no cert, no household, no space."""
    return {
        "channel_id": channel_id,
        "event_type": SPACE_ITEM_EVENT_TYPE,
        "epoch": epoch,
        "payload": payload,
    }


@dataclass(slots=True, frozen=True)
class ChannelItemFrame:
    """A channel item as a member household receives it."""

    channel_id: str
    epoch: int
    payload: str

    @classmethod
    def from_wire(cls, raw: object) -> "ChannelItemFrame":
        """Parse a fan-out frame; the WS ``type`` key (and any key a newer
        server adds) is ignored — the item authenticates itself inside the
        ciphertext."""
        if not isinstance(raw, dict):
            raise InvalidChannelWire("expected a JSON object")
        _event_type(raw)
        return cls(
            channel_id=valid_channel_id(raw.get("channel_id")),
            epoch=_epoch(raw),
            payload=_payload(raw),
        )
