"""Encrypted public moments: the wire crypto (§Momentum-public, v_62).

A public moment crosses a GFS on its way to the author's followers. The GFS
routes it by ``author_user_id`` and must never read it, so everything but
the routing fields — the text, the media reference, the author's profile —
is encrypted. The GFS stays **content-blind**, not blind: it still sees the
author id, the moment id, the key id, sizes, timing and the follow graph.

Keys (one author, one *generation* at a time):

* **Generation root.** 32 random bytes the author's household mints
  (:class:`~socialhome.services.moment_public_key_outbound
  .MomentPublicKeyOutbound`); ``key_gen`` is its Unix-seconds mint time, so
  a generation number never repeats. A new generation is what removes a
  follower (rotation).
* **Step keys — a hash ratchet.** ``K(g, 0) = root``,
  ``K(g, s+1) = HKDF(K(g, s), "sh-moment-ratchet:v1")``
  (:func:`ratchet`). ``key_step`` advances once a day, so a household that
  keeps only its newest step key cannot recompute yesterday's (forward
  secrecy at rest) — at zero delivery cost: whoever holds ``K(g, s)``
  derives every later step itself.
* **Item keys.** Each moment is sealed under
  ``HKDF(K(g, s), "sh-moment-item:v1|" + moment_id)`` (:func:`item_key`),
  never the step key itself — so one moment's key (a web share link) opens
  that moment and nothing else.

Wire shapes (every one suite-tagged; receivers refuse unknown suites):

* **Envelope** ``{author_user_id, instance_id, moment_id, key_gen, key_step,
  seal_suite, ciphertext, signature}`` — AES-256-GCM (``seal_suite:
  "aesgcm-256"``), fresh 96-bit nonce, associated data binding the suite,
  author, generation, step and moment id. The author's household signs the
  whole envelope, ciphertext and key id included (:func:`sign_wire`).
* **Key delivery** ``{author_user_id, instance_id, to, key_gen, key_step,
  key_suite, sealed, signature}`` — ``sealed`` is the static key-wrap seal
  (``kem_suite``-tagged) to the ``to`` household; inside, the key repeats
  the routing fields so a sealed blob can't be lifted into another delivery;
  ``key_suite`` is :data:`KEY_SUITE_AESGCM_256` (the space content-key tag).
* **Reaction (v_68)** ``POST /gfs/moments/users/{author}/react`` with
  ``{instance_id, sealed}`` — ``sealed`` is the v_67 sealed reaction
  (:mod:`socialhome.federation.moment_reaction_route`, ``kem_suite``-tagged,
  padded to one size) to the author household's key-wrap key, which the
  author's household names inside every sealed create body
  (``author_keywrap_pk`` + ``author_keywrap_suite``). The GFS pushes it to
  the author's household as ``{type: "moment_reaction", from_instance,
  sealed}`` and never learns who reacted, with what, to which moment, or
  whether it is an add or a removal.

Pure and dependency-light (only ``cryptography``, ``orjson`` + :mod:`socialhome.crypto`,
like :mod:`socialhome.authority_sig`), so the content-blind GFS process can
import the wire shapes without the household stack.
"""

from __future__ import annotations

import os
from typing import Any

import orjson
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .canonical_json import canonical_bytes
from .crypto import b64url_decode, b64url_encode, sign_ed25519, verify_ed25519

#: The one moment-content suite this build speaks: AES-256-GCM, 96-bit
#: random nonce, ``nonce:ct`` in base64url. AES-256 already resists Grover
#: (~128-bit PQ); the PQ lever is the key delivery (``kem_suite``).
MOMENT_SEAL_SUITE_AESGCM256: str = "aesgcm-256"
SUPPORTED_MOMENT_SEAL_SUITES: frozenset[str] = frozenset({MOMENT_SEAL_SUITE_AESGCM256})

#: The delivered key's suite — the same tag as a space content key
#: (``space_crypto_service.KEY_SUITE_AESGCM_256``; a test pins them equal).
KEY_SUITE_AESGCM_256: str = "aesgcm-256"
SUPPORTED_KEY_SUITES: frozenset[str] = frozenset({KEY_SUITE_AESGCM_256})

#: Raw key length of every moment key (root, step, item).
MOMENT_KEY_BYTES: int = 32

#: One ratchet step per this many seconds of a generation's life.
RATCHET_STEP_S: int = 86_400

#: A receiver derives at most this many steps forward from a key it holds
#: (over a year at one step a day) — a bound on the work one envelope can ask.
MAX_RATCHET_STEPS: int = 400

#: The plaintext envelope fields — everything a GFS may read. A GFS refuses
#: a publish or delete carrying any other key (``routes/moments_public``),
#: and a follower refuses an envelope that is not exactly this shape.
ENVELOPE_FIELDS: frozenset[str] = frozenset(
    {
        "author_user_id",
        "instance_id",
        "moment_id",
        "key_gen",
        "key_step",
        "seal_suite",
        "ciphertext",
        "signature",
    }
)

#: WebSocket frame types the GFS pushes a public moment and its tombstone
#: under (``{"type": ..., "payload": <envelope>}``), to every follower
#: household that can open them.
FRAME_MOMENT: str = "incoming_public_moment"
FRAME_MOMENT_DELETE: str = "incoming_public_moment_delete"
MOMENT_FRAME_TYPES: frozenset[str] = frozenset({FRAME_MOMENT, FRAME_MOMENT_DELETE})

#: v_68 — the WebSocket frame type a follower household's sealed reaction
#: reaches the author's household under: ``{"type": ..., "from_instance":
#: <the follower household the GFS authenticated>, "sealed": {...}}``.
FRAME_MOMENT_REACTION: str = "moment_reaction"

#: v_68 — fields of ``POST /gfs/moments/users/{author}/react`` (besides the
#: household's wire ``signature``). Nothing else rides: no moment id, no
#: user, no emoji, no add/remove flag — all of that is inside ``sealed``.
REACT_BODY_FIELDS: frozenset[str] = frozenset({"instance_id", "sealed"})

#: v_68 — longest ``sealed.ciphertext`` a GFS relays (one 1 KB padding
#: bucket is ~1.4 K chars; room for two, as on the mesh —
#: ``moment_reaction_route.MAX_SEALED_CHARS``, a test pins them equal).
MAX_REACT_SEALED_CHARS: int = 3_000

#: v_68 — body cap of a react post: the sealed reaction (~1.5 KB), the
#: household id and its signature, with room for a two-bucket reaction.
MAX_REACT_BODY_BYTES: int = 4096

#: v_68 — sealed create body fields naming the author household's static
#: key-wrap key (hex) and its KEM suite. Under the envelope signature (it
#: covers the ciphertext) and the AEAD — a GFS can't swap them. A follower
#: that does not know the suite ignores the key (its reactions stay home).
AUTHOR_KEYWRAP_PK_FIELD: str = "author_keywrap_pk"
AUTHOR_KEYWRAP_SUITE_FIELD: str = "author_keywrap_suite"

#: The fields of a key delivery (``moment_key`` frame / ``POST
#: /gfs/moments/keys`` item).
DELIVERY_FIELDS: frozenset[str] = frozenset(
    {
        "author_user_id",
        "instance_id",
        "to",
        "key_gen",
        "key_step",
        "key_suite",
        "sealed",
        "signature",
    }
)

#: Longest ``ciphertext`` either side accepts. A moment's text, media
#: reference and the author's profile fit in a few KB; the cap bounds what
#: one publish can make a GFS fan out.
MAX_CIPHERTEXT_CHARS: int = 16 * 1024

#: Longest moment id on the wire (owner-bound ids are ~60 chars).
MAX_MOMENT_ID_CHARS: int = 200

_NONCE_BYTES = 12
_AAD_PREFIX = b"socialhome/moment-public/v2|"
_RATCHET_INFO = b"sh-moment-ratchet:v1"
_ITEM_INFO = b"sh-moment-item:v1|"


class UnsupportedMomentSealSuite(ValueError):
    """An envelope or delivered key names a suite this build does not
    implement (or none). Refused — never decrypted under a default."""


def require_known_suite(suite: object) -> str:
    """``suite`` when this build implements it, else raise."""
    if not isinstance(suite, str) or suite not in SUPPORTED_MOMENT_SEAL_SUITES:
        raise UnsupportedMomentSealSuite(
            f"unsupported moment seal suite {suite!r}; this build supports "
            f"{sorted(SUPPORTED_MOMENT_SEAL_SUITES)!r}"
        )
    return suite


def require_known_key_suite(suite: object) -> str:
    """``suite`` when it names a key suite this build implements, else raise."""
    if not isinstance(suite, str) or suite not in SUPPORTED_KEY_SUITES:
        raise UnsupportedMomentSealSuite(f"unsupported key suite {suite!r}")
    return suite


def is_wire_int(value: object) -> bool:
    """A non-negative JSON integer (``True`` / ``False`` are not)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def react_sealed_ok(sealed: object) -> bool:
    """Whether *sealed* has the shape of a sealed reaction a GFS relays —
    exactly ``{kem_suite, eph_pk, ciphertext}``, short strings. The GFS
    can't open it and doesn't interpret the suite (the author's household
    refuses one it doesn't know)."""
    if not isinstance(sealed, dict) or set(sealed) != {
        "kem_suite",
        "eph_pk",
        "ciphertext",
    }:
        return False
    suite, eph, ct = sealed["kem_suite"], sealed["eph_pk"], sealed["ciphertext"]
    return (
        isinstance(suite, str)
        and 0 < len(suite) <= 32
        and isinstance(eph, str)
        and 0 < len(eph) <= 64
        and isinstance(ct, str)
        and 0 < len(ct) <= MAX_REACT_SEALED_CHARS
    )


def _hkdf(key: bytes, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=MOMENT_KEY_BYTES, salt=None, info=info
    ).derive(key)


def ratchet(key: bytes, steps: int = 1) -> bytes:
    """The step key *steps* steps after *key* (one-way)."""
    if not 0 <= steps <= MAX_RATCHET_STEPS:
        raise ValueError(f"ratchet of {steps} steps is out of bounds")
    for _ in range(steps):
        key = _hkdf(key, _RATCHET_INFO)
    return key


def step_for(generation: int, now: float) -> int:
    """The ratchet step a generation minted at *generation* is at *now*."""
    return max(int(now) - generation, 0) // RATCHET_STEP_S


def item_key(step_key: bytes, moment_id: str) -> bytes:
    """The key one moment is sealed under — and all a share link carries."""
    return _hkdf(step_key, _ITEM_INFO + moment_id.encode())


def _aad(author: str, gen: int, step: int, moment_id: str, suite: str) -> bytes:
    return _AAD_PREFIX + f"{suite}|{author}|{gen}|{step}|{moment_id}".encode()


def seal_body(
    *,
    step_key: bytes,
    author_user_id: str,
    key_gen: int,
    key_step: int,
    moment_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Encrypt *body* (a JSON object) for one moment.

    Returns the encrypted part of an envelope: ``{moment_id, key_gen,
    key_step, seal_suite, ciphertext}``. A fresh random nonce per call.
    """
    if len(step_key) != MOMENT_KEY_BYTES:
        raise ValueError("moment key must be 32 bytes")
    if not moment_id or len(moment_id) > MAX_MOMENT_ID_CHARS:
        raise ValueError("moment id missing or too long")
    suite = MOMENT_SEAL_SUITE_AESGCM256
    nonce = os.urandom(_NONCE_BYTES)
    plaintext = orjson.dumps(body, option=orjson.OPT_SORT_KEYS)
    ct = AESGCM(item_key(step_key, moment_id)).encrypt(
        nonce, plaintext, _aad(author_user_id, key_gen, key_step, moment_id, suite)
    )
    return {
        "moment_id": moment_id,
        "key_gen": key_gen,
        "key_step": key_step,
        "seal_suite": suite,
        "ciphertext": f"{b64url_encode(nonce)}:{b64url_encode(ct)}",
    }


def open_with_item_key(*, key: bytes, envelope: dict[str, Any]) -> dict[str, Any]:
    """Decrypt an envelope with its moment's item key.

    Raises :class:`UnsupportedMomentSealSuite` for an unknown suite,
    :class:`ValueError` for a malformed shape, :class:`TypeError` for a body
    that is not a JSON object, and :class:`cryptography.exceptions.InvalidTag`
    when the key, author, key id, moment id or ciphertext don't match.
    """
    suite = require_known_suite(envelope.get("seal_suite"))
    author = envelope.get("author_user_id")
    moment_id = envelope.get("moment_id")
    gen, step = envelope.get("key_gen"), envelope.get("key_step")
    raw = envelope.get("ciphertext")
    if not isinstance(author, str) or not author:
        raise ValueError("envelope has no author_user_id")
    if (
        not isinstance(moment_id, str)
        or not moment_id
        or len(moment_id) > MAX_MOMENT_ID_CHARS
    ):
        raise ValueError("envelope has no valid moment_id")
    if not is_wire_int(gen) or not is_wire_int(step):
        raise ValueError("envelope has no valid key id")
    if not isinstance(raw, str) or len(raw) > MAX_CIPHERTEXT_CHARS:
        raise ValueError("envelope ciphertext missing or too large")
    try:
        nonce_b64, ct_b64 = raw.split(":", 1)
        nonce, ct = b64url_decode(nonce_b64), b64url_decode(ct_b64)
    except ValueError as exc:
        raise ValueError(f"malformed ciphertext: {exc}") from exc
    if len(nonce) != _NONCE_BYTES:
        raise ValueError("malformed ciphertext nonce")
    assert isinstance(gen, int) and isinstance(step, int)
    plaintext = AESGCM(key).decrypt(
        nonce, ct, _aad(author, gen, step, moment_id, suite)
    )
    body = orjson.loads(plaintext)
    if not isinstance(body, dict):
        raise TypeError("sealed body is not a JSON object")
    return body


def open_body(*, step_key: bytes, envelope: dict[str, Any]) -> dict[str, Any]:
    """Decrypt an envelope with the step key it names (see
    :func:`open_with_item_key` for what raises)."""
    moment_id = envelope.get("moment_id")
    if not isinstance(moment_id, str) or not moment_id:
        raise ValueError("envelope has no valid moment_id")
    return open_with_item_key(key=item_key(step_key, moment_id), envelope=envelope)


def key_plaintext(
    *, author_user_id: str, key_gen: int, key_step: int, to: str, key: bytes
) -> bytes:
    """The bytes a key delivery seals to the follower's key-wrap key."""
    return orjson.dumps(
        {
            "author_user_id": author_user_id,
            "key_gen": key_gen,
            "key_step": key_step,
            "to": to,
            "key_suite": KEY_SUITE_AESGCM_256,
            "key": b64url_encode(key),
        },
        option=orjson.OPT_SORT_KEYS,
    )


def parse_key_plaintext(
    raw: bytes, *, author_user_id: str, key_gen: int, key_step: int, to: str
) -> bytes:
    """The raw key from an opened delivery — only when it names the same
    author, key id and recipient as the signed outer delivery (so a sealed
    blob can't be lifted into another one). Raises :class:`ValueError`,
    :class:`TypeError` (not a JSON object) or
    :class:`UnsupportedMomentSealSuite` otherwise."""
    body = orjson.loads(raw)
    if not isinstance(body, dict):
        raise TypeError("sealed key is not a JSON object")
    require_known_key_suite(body.get("key_suite"))
    if (
        body.get("author_user_id") != author_user_id
        or body.get("key_gen") != key_gen
        or body.get("key_step") != key_step
        or body.get("to") != to
    ):
        raise ValueError("sealed key does not match its delivery")
    key = b64url_decode(str(body.get("key") or ""))
    if len(key) != MOMENT_KEY_BYTES:
        raise ValueError("sealed key is not 32 bytes")
    return key


def key_at_rest_context(author_user_id: str, key_gen: int, *, own: bool) -> bytes:
    """Associated data a household binds a moment key to when it KEK-seals
    it for storage, so a sealed key can't be moved onto another author,
    generation or role (own / delivered)."""
    role = "own" if own else "delivered"
    return f"moment-public-key|{role}|{author_user_id}|{key_gen}".encode()


def _canonical(body: dict[str, Any]) -> bytes:
    unsigned = {k: v for k, v in body.items() if k != "signature"}
    return canonical_bytes(unsigned)


def sign_wire(signing_key: bytes, body: dict[str, Any]) -> dict[str, Any]:
    """*body* plus its Ed25519 ``signature`` over the canonical JSON of every
    other field — the shape every public-moment wire body uses."""
    signed = {k: v for k, v in body.items() if k != "signature"}
    signed["signature"] = b64url_encode(sign_ed25519(signing_key, _canonical(body)))
    return signed


def verify_wire(body: dict[str, Any], pubkey_hex: str | None) -> bool:
    """Whether *body* carries a valid :func:`sign_wire` signature by
    *pubkey_hex*. Never raises."""
    sig = body.get("signature")
    if not sig or not pubkey_hex:
        return False
    try:
        return verify_ed25519(
            bytes.fromhex(pubkey_hex), _canonical(body), b64url_decode(str(sig))
        )
    except ValueError, TypeError:
        return False


__all__ = [
    "AUTHOR_KEYWRAP_PK_FIELD",
    "AUTHOR_KEYWRAP_SUITE_FIELD",
    "DELIVERY_FIELDS",
    "ENVELOPE_FIELDS",
    "FRAME_MOMENT_REACTION",
    "KEY_SUITE_AESGCM_256",
    "MAX_CIPHERTEXT_CHARS",
    "MAX_MOMENT_ID_CHARS",
    "MAX_RATCHET_STEPS",
    "MAX_REACT_BODY_BYTES",
    "MAX_REACT_SEALED_CHARS",
    "MOMENT_KEY_BYTES",
    "MOMENT_SEAL_SUITE_AESGCM256",
    "RATCHET_STEP_S",
    "REACT_BODY_FIELDS",
    "SUPPORTED_KEY_SUITES",
    "SUPPORTED_MOMENT_SEAL_SUITES",
    "UnsupportedMomentSealSuite",
    "is_wire_int",
    "item_key",
    "key_at_rest_context",
    "key_plaintext",
    "open_body",
    "open_with_item_key",
    "parse_key_plaintext",
    "ratchet",
    "react_sealed_ok",
    "require_known_key_suite",
    "require_known_suite",
    "seal_body",
    "sign_wire",
    "step_for",
    "verify_wire",
]
