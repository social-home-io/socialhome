"""Space writer group key — the domain shape (v_50, strict member publish).

In a space whose owner chose **strict** GFS publishing
(``SpaceFeatures.gfs_publish_mode == "strict"``), a member household
publishes over the connection server WITHOUT identifying itself: it signs the
request with the space's **writer group key** for the content epoch, one
Ed25519 key shared by every household allowed to publish anything there (both
``write`` and ``comment`` scopes, so the server cannot tell them apart). The
server verifies the signature against the key's public half, which the space
authority pinned there with a :class:`WriterKeyCert`.

Two wire shapes, both carrying the ``writer_key_suite`` tag (crypto-suite
rule — receivers reject an unknown suite, never default):

* :class:`WriterKeyCert` — the space AUTHORITY key's statement "``writer_pk``
  is the writer key of ``space_id`` at content epoch ``epoch``"::

      {"writer_key_suite": "ed25519", "space_id", "epoch": N,
       "writer_pk": <b64url Ed25519 public key>, "cert_sig": <b64url>}

  It is public: the GFS pins it, and a household verifies a delivered key
  against it.

* :class:`WriterKeyGrant` — the private half as a seed holder delivers it to
  ONE publish-capable household, sealed inside that household's per-peer
  encrypted payload (next to its writer cert)::

      {"writer_key_suite": "ed25519", "space_id", "epoch": N,
       "writer_seed": <b64url 32-byte Ed25519 seed>,
       "writer_key_cert": {<WriterKeyCert wire>}}

This module is pure (dataclasses + codec). Derivation, signing and
verification live in :mod:`socialhome.writer_key`, which depends only on
:mod:`socialhome.crypto` so the content-blind GFS process can import it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .writer_cert import MAX_WRITER_CERT_EPOCH

#: The writer key's algorithm — of ``writer_pk``, of every ``writer_sig`` it
#: makes, and of the authority signature over the cert. Phase-2 of
#: ``docs/crypto.md`` adds ``"ed25519+mldsa65"`` as a sibling.
WRITER_KEY_SUITE_ED25519: str = "ed25519"
SUPPORTED_WRITER_KEY_SUITES: frozenset[str] = frozenset({WRITER_KEY_SUITE_ED25519})

#: The wire key a writer key grant rides under, next to ``writer_cert``.
WRITER_KEY_FIELD: str = "writer_key"

#: Longest base64url string a 32-byte key / 64-byte signature encodes to,
#: with room for a future hybrid suite.
_MAX_B64_CHARS: int = 4096


class UnsupportedWriterKeySuite(ValueError):
    """The writer key cert, grant or signature names a suite this build does
    not know. Rejected — never defaulted to Ed25519."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _b64(raw: dict, field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value or len(value) > _MAX_B64_CHARS:
        raise ValueError(f"writer key: bad {field}")
    return value


def _epoch(raw: dict) -> int:
    value = raw.get("epoch")
    if not _is_int(value) or not 0 <= value <= MAX_WRITER_CERT_EPOCH:  # type: ignore[operator]
        raise ValueError("writer key: bad epoch")
    return int(value)  # type: ignore[arg-type]


def _suite(raw: dict) -> str:
    suite = raw.get("writer_key_suite")
    if not isinstance(suite, str) or not suite:
        raise ValueError("writer key: bad writer_key_suite")
    return suite


def _space_id(raw: dict) -> str:
    space_id = raw.get("space_id")
    if not isinstance(space_id, str) or not space_id or len(space_id) > 128:
        raise ValueError("writer key: bad space_id")
    return space_id


#: Exact key set of a :class:`WriterKeyCert` on the wire.
WRITER_KEY_CERT_KEYS: frozenset[str] = frozenset(
    {"writer_key_suite", "space_id", "epoch", "writer_pk", "cert_sig"}
)


@dataclass(slots=True, frozen=True)
class WriterKeyCert:
    """The authority-signed pin of one epoch's writer public key."""

    writer_key_suite: str
    space_id: str
    epoch: int
    writer_pk: str
    cert_sig: str

    def signing_body(self) -> dict:
        """Every signed field — the cert minus ``cert_sig``; the suite tag is
        inside the signature so it can't be swapped in transit."""
        return {
            "writer_key_suite": self.writer_key_suite,
            "space_id": self.space_id,
            "epoch": self.epoch,
            "writer_pk": self.writer_pk,
        }

    def to_wire(self) -> dict:
        return {**self.signing_body(), "cert_sig": self.cert_sig}

    @classmethod
    def from_wire(cls, raw: object) -> "WriterKeyCert":
        """Parse a wire dict; ``ValueError`` on any missing, extra or
        malformed field. An unknown suite STRING parses (the verifier
        rejects it with :class:`UnsupportedWriterKeySuite`)."""
        if not isinstance(raw, dict) or set(raw) != WRITER_KEY_CERT_KEYS:
            raise ValueError("writer key cert: unexpected or missing fields")
        return cls(
            writer_key_suite=_suite(raw),
            space_id=_space_id(raw),
            epoch=_epoch(raw),
            writer_pk=_b64(raw, "writer_pk"),
            cert_sig=_b64(raw, "cert_sig"),
        )


#: Exact key set of a :class:`WriterKeyGrant` on the wire.
WRITER_KEY_GRANT_KEYS: frozenset[str] = frozenset(
    {"writer_key_suite", "space_id", "epoch", "writer_seed", "writer_key_cert"}
)


@dataclass(slots=True, frozen=True)
class WriterKeyGrant:
    """One epoch's writer key (private seed + its authority cert) as a seed
    holder delivers it to a publish-capable household. SECRET: it rides only
    inside a per-peer encrypted payload and is stored KEK-wrapped."""

    writer_key_suite: str
    space_id: str
    epoch: int
    writer_seed: str
    writer_key_cert: WriterKeyCert

    def to_wire(self) -> dict:
        return {
            "writer_key_suite": self.writer_key_suite,
            "space_id": self.space_id,
            "epoch": self.epoch,
            "writer_seed": self.writer_seed,
            "writer_key_cert": self.writer_key_cert.to_wire(),
        }

    @classmethod
    def from_wire(cls, raw: object) -> "WriterKeyGrant":
        """Parse a wire dict; ``ValueError`` on any missing, extra or
        malformed field."""
        if not isinstance(raw, dict) or set(raw) != WRITER_KEY_GRANT_KEYS:
            raise ValueError("writer key grant: unexpected or missing fields")
        return cls(
            writer_key_suite=_suite(raw),
            space_id=_space_id(raw),
            epoch=_epoch(raw),
            writer_seed=_b64(raw, "writer_seed"),
            writer_key_cert=WriterKeyCert.from_wire(raw.get("writer_key_cert")),
        )
