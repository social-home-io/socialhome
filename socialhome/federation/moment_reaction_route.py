"""Routed moment reactions (v_67) — the wire shapes and their crypto.

A moment travels up to three hops (author → paired household → its paired
household → …), but a reaction used to travel only one: back to the author's
household, and only from a household paired with it. A reactor two or three
hops away had no way back. v_67 carries the reaction back along the path the
moment came, without any household on the way learning who reacted, with
what, or anything about the pairing graph beyond its own neighbours.

Three pieces, all built from keys households already hold — no new key type:

1. **The origin's key-wrap key on the create.** ``MOMENT_CREATED`` carries
   ``origin_keywrap_pk`` / ``origin_keywrap_sig`` / ``origin_keywrap_suite``
   (the :func:`~socialhome.federation.keywrap_seal.keywrap_wire_fields`
   shape, prefixed), *outside* the origin signature — inside, it would
   change the ``moment-origin:v1:`` bytes and older verifiers would refuse
   the moment. It is bound instead by
   :func:`~socialhome.federation.keywrap_seal.verify_keywrap_binding`
   against ``origin_identity_pk`` (itself checked against the pinned or
   ``derive_instance_id``-bound key), so a relay can't substitute its own.
   See :func:`verified_origin_keywrap`.

2. **Return tokens.** A v_67 relay forwarding a create to its peer X appends
   ``rt = b64url(nonce ‖ AES-256-GCM(k, exp ‖ prev_hop_id))`` with
   ``k = HKDF(routing_secret, "moment-return:v1")`` (the household's existing
   ``instance_identity.routing_secret``) and AAD
   ``"moment-return:v1|" + moment_id + "|" + X``. Only that relay can open
   it, only for that moment, only when X hands it back, and only until the
   moment's (clamped) expiry. It names one neighbour — the household the
   relay got the moment from. ``rt_suite`` tags the scheme
   (:data:`RETURN_TOKEN_SUITE_AESGCM_256`). See :func:`mint_return_token` /
   :func:`open_return_token`.

3. **The sealed reaction.** The reactor's household builds the inner
   plaintext (:func:`build_reaction`), signs it with its Ed25519 identity
   key over ``b"moment-reaction:v1:" + canonical_bytes(inner minus the
   signature)`` (:data:`REACTION_SIG_SUITE_ED25519`), pads it to a 1 KB
   bucket and seals it to the origin's key-wrap key with
   :func:`~socialhome.federation.keywrap_seal.seal_to_keywrap`. Relays carry
   it as opaque bytes. See :func:`seal_reaction` / :func:`open_reaction` /
   :func:`verify_reaction`.

Every suite is tagged on the wire and an unknown one is refused, never
defaulted (``docs/crypto.md`` § "The suite contract").

Pure: depends only on ``cryptography``, :mod:`socialhome.crypto` and the
canonical-JSON helpers. No I/O.
"""

from __future__ import annotations

import os
from typing import Any

import orjson
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..canonical_json import canonical_bytes
from ..crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    derive_user_id,
    sign_ed25519,
    verify_ed25519,
)
from ..domain.moment import MOMENT_MAX_HOPS, is_valid_moment_reaction
from .keywrap_seal import (
    SUPPORTED_KEM_SUITES,
    keywrap_wire_fields,
    open_keywrap,
    seal_to_keywrap,
    verify_keywrap_binding,
)

# ── Suites ─────────────────────────────────────────────────────────────────

#: The reactor household's signature over the reaction. Same vocabulary as
#: ``Encoder.sig_suite``; Phase 2 of ``docs/crypto.md`` adds
#: ``"ed25519+mldsa65"`` as a sibling with both signatures checked.
REACTION_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_REACTION_SIG_SUITES: frozenset[str] = frozenset({REACTION_SIG_SUITE_ED25519})

#: Return tokens: AES-256-GCM under a key HKDF-derived from the relay's own
#: routing secret. Symmetric and only ever opened by the household that
#: minted it, so there is no PQ lever beyond the 256-bit key.
RETURN_TOKEN_SUITE_AESGCM_256: str = "aesgcm-256"
SUPPORTED_RETURN_TOKEN_SUITES: frozenset[str] = frozenset(
    {RETURN_TOKEN_SUITE_AESGCM_256}
)


class UnsupportedReactionSigSuite(ValueError):
    """A sealed reaction names a signature suite this build doesn't know (or
    none). Refused — never verified under a default algorithm."""


class UnsupportedReturnTokenSuite(ValueError):
    """Return tokens tagged with a suite this build doesn't know (or none).
    Refused — never opened under a default scheme."""


class ReturnTokenRefused(ValueError):
    """A return token that does not open here: not ours, minted for another
    moment or another neighbour, tampered with, or past its expiry."""


# ── Wire field names ───────────────────────────────────────────────────────

#: On ``MOMENT_CREATED`` (outside the origin signature, like ``hop_count``).
ORIGIN_KEYWRAP_PK_FIELD = "origin_keywrap_pk"
ORIGIN_KEYWRAP_SIG_FIELD = "origin_keywrap_sig"
ORIGIN_KEYWRAP_SUITE_FIELD = "origin_keywrap_suite"
RETURN_TOKENS_FIELD = "rt"
RETURN_TOKEN_SUITE_FIELD = "rt_suite"

#: The inner (sealed) reaction's ``kind`` — the key-wrap seal is shared with
#: the space-key handoff, so the plaintext names what it is.
REACTION_KIND = "moment_reaction"

#: Fields every sealed reaction carries; :data:`OPTIONAL_REACTION_FIELDS`
#: may also appear. Anything else is refused.
REACTION_FIELDS: frozenset[str] = frozenset(
    {
        "kind",
        "moment_id",
        "author_user_id",
        "origin_instance_id",
        "reactor_user_id",
        "reactor_instance_id",
        "reactor_identity_pk",
        "reactor_anchor",
        "emoji",
        "occurred_at_ms",
        "reaction_sig_suite",
        "reaction_sig",
    }
)
OPTIONAL_REACTION_FIELDS: frozenset[str] = frozenset({"display_name"})

# ── Bounds ─────────────────────────────────────────────────────────────────

#: Padding bucket of the sealed plaintext: every reaction seals to the same
#: size, so a relay can't tell an emoji from a removal or one person's
#: reaction from another's by its length.
PAD_BUCKET: int = 1024

#: Most return tokens a create or a routed reaction carries: one per relay.
MAX_RETURN_TOKENS: int = MOMENT_MAX_HOPS - 1

#: Longest return token (b64url of 12 + 8 + id + 16 bytes is ~110 chars).
MAX_RETURN_TOKEN_CHARS: int = 256

#: Longest ``sealed.ciphertext`` a relay forwards — one bucket plus the
#: AEAD tag and nonce, b64url, with room for a two-bucket reaction.
MAX_SEALED_CHARS: int = 3_000

#: Longest id-like string field in a sealed reaction.
MAX_ID_CHARS: int = 128

#: Longest optional display name.
MAX_DISPLAY_NAME_CHARS: int = 64

_SIG_DOMAIN = b"moment-reaction:v1:"
_TOKEN_INFO = b"moment-return:v1"
_TOKEN_AAD_PREFIX = b"moment-return:v1|"


# ── 1. The origin's key-wrap key on the create ─────────────────────────────


def origin_keywrap_fields(
    keywrap_public_key: bytes, keywrap_sig: str
) -> dict[str, str]:
    """The three ``origin_keywrap_*`` fields an origin adds to its creates."""
    wire = keywrap_wire_fields(keywrap_public_key, keywrap_sig)
    return {
        ORIGIN_KEYWRAP_PK_FIELD: wire["keywrap_pk"],
        ORIGIN_KEYWRAP_SIG_FIELD: wire["keywrap_sig"],
        ORIGIN_KEYWRAP_SUITE_FIELD: wire["keywrap_suite"],
    }


def verified_origin_keywrap(
    payload: dict[str, Any],
    *,
    origin_instance_id: str,
    pinned_identity_pk: bytes | None,
) -> bytes | None:
    """The origin's key-wrap public key from a ``MOMENT_CREATED`` *payload*,
    or ``None`` when it carries none or it can't be trusted.

    Trusted only when: the suite is a known KEM suite (an unknown one is
    refused, not defaulted); ``origin_identity_pk`` is present, equals
    *pinned_identity_pk* when this household pins a key for the origin, and
    derives to *origin_instance_id*; and the key-wrap key carries a valid
    binding signature by that identity key. A relay that swaps in its own
    key-wrap key has no such signature. Never raises.
    """
    pk_hex = payload.get(ORIGIN_KEYWRAP_PK_FIELD)
    if not pk_hex:
        return None
    if payload.get(ORIGIN_KEYWRAP_SUITE_FIELD) not in SUPPORTED_KEM_SUITES:
        return None
    try:
        identity_pk = bytes.fromhex(str(payload.get("origin_identity_pk") or ""))
        keywrap_pk = bytes.fromhex(str(pk_hex))
    except ValueError:
        return None
    if pinned_identity_pk is not None and identity_pk != pinned_identity_pk:
        return None
    if not verify_keywrap_binding(
        instance_id=origin_instance_id,
        identity_pub=identity_pk,
        keywrap_pub=keywrap_pk,
        keywrap_sig=str(payload.get(ORIGIN_KEYWRAP_SIG_FIELD) or ""),
    ):
        return None
    return keywrap_pk


def return_tokens_of(payload: dict[str, Any]) -> list[str] | None:
    """The well-formed return-token list of a create or routed reaction,
    ``[]`` when it carries none, or ``None`` when it is malformed (not a
    list of short strings, or too many). Tokens under an unknown or missing
    ``rt_suite`` raise :class:`UnsupportedReturnTokenSuite` — refused, never
    read under a default."""
    tokens = payload.get(RETURN_TOKENS_FIELD)
    if tokens is None:
        return []
    if (
        not isinstance(tokens, list)
        or len(tokens) > MAX_RETURN_TOKENS
        or not all(
            isinstance(t, str) and 0 < len(t) <= MAX_RETURN_TOKEN_CHARS for t in tokens
        )
    ):
        return None
    suite = payload.get(RETURN_TOKEN_SUITE_FIELD)
    if tokens and suite not in SUPPORTED_RETURN_TOKEN_SUITES:
        raise UnsupportedReturnTokenSuite(
            f"return tokens advertise unsupported rt_suite={suite!r}; this "
            f"build supports {sorted(SUPPORTED_RETURN_TOKEN_SUITES)!r}"
        )
    return list(tokens)


# ── 2. Return tokens ───────────────────────────────────────────────────────


def return_token_key(routing_secret_hex: str) -> bytes:
    """The 32-byte AES key a household seals its return tokens under —
    HKDF-SHA256 of its ``routing_secret`` (never transmitted) with the
    ``moment-return:v1`` label, so the secret's other use (relay path
    selection, §4.1.8) never shares a key with this one."""
    return HKDF(
        algorithm=_hashes.SHA256(),
        length=32,
        salt=None,
        info=_TOKEN_INFO,
    ).derive(bytes.fromhex(routing_secret_hex))


def _token_aad(moment_id: str, next_hop: str) -> bytes:
    return _TOKEN_AAD_PREFIX + f"{moment_id}|{next_hop}".encode()


def mint_return_token(
    *,
    key: bytes,
    moment_id: str,
    next_hop: str,
    prev_hop: str,
    expires_at_s: int,
) -> str:
    """A return token for the copy of *moment_id* sent to *next_hop*: it
    opens (here only) to *prev_hop*, the household the moment came from,
    until ``expires_at_s`` (epoch seconds)."""
    nonce = os.urandom(12)
    plain = int(expires_at_s).to_bytes(8, "big", signed=False) + prev_hop.encode()
    ct = AESGCM(key).encrypt(nonce, plain, _token_aad(moment_id, next_hop))
    return b64url_encode(nonce + ct)


def open_return_token(
    *,
    key: bytes,
    token: str,
    moment_id: str,
    from_instance: str,
    now_s: int,
) -> str:
    """The previous hop a return token names, or :class:`ReturnTokenRefused`.

    The AAD names the moment and *from_instance* — the neighbour handing the
    reaction back must be the one this household gave the moment to — so a
    token lifted onto another moment, or replayed by another neighbour,
    fails the tag. A token past its expiry is refused too.
    """
    try:
        raw = b64url_decode(token)
        if len(raw) < 12 + 8 + 16 + 1:
            raise ReturnTokenRefused("return token too short")
        plain = AESGCM(key).decrypt(
            raw[:12], raw[12:], _token_aad(moment_id, from_instance)
        )
    except (InvalidTag, ValueError, TypeError) as exc:
        raise ReturnTokenRefused("return token does not open here") from exc
    if int.from_bytes(plain[:8], "big") <= now_s:
        raise ReturnTokenRefused("return token expired")
    try:
        prev = plain[8:].decode()
    except UnicodeDecodeError as exc:  # pragma: no cover — we minted it
        raise ReturnTokenRefused("malformed return token") from exc
    if not prev:  # pragma: no cover — we minted it
        raise ReturnTokenRefused("malformed return token")
    return prev


# ── 3. The sealed reaction ─────────────────────────────────────────────────


def reaction_signing_bytes(inner: dict[str, Any]) -> bytes:
    """``b"moment-reaction:v1:"`` + canonical JSON of *inner* without its
    ``reaction_sig`` — the suite and ``kind`` are inside, so neither can be
    swapped, and the domain keeps the bytes disjoint from every other
    identity-key signature in the protocol."""
    signed = {k: v for k, v in inner.items() if k != "reaction_sig"}
    return _SIG_DOMAIN + canonical_bytes(signed)


def build_reaction(
    *,
    seed: bytes,
    identity_pk: bytes,
    reactor_instance_id: str,
    reactor_user_id: str,
    reactor_anchor: str,
    moment_id: str,
    author_user_id: str,
    origin_instance_id: str,
    emoji: str | None,
    occurred_at_ms: int,
) -> dict[str, Any]:
    """The inner reaction, signed by the reactor's household identity key.
    ``emoji is None`` is a removal."""
    inner: dict[str, Any] = {
        "kind": REACTION_KIND,
        "moment_id": moment_id,
        "author_user_id": author_user_id,
        "origin_instance_id": origin_instance_id,
        "reactor_user_id": reactor_user_id,
        "reactor_instance_id": reactor_instance_id,
        "reactor_identity_pk": identity_pk.hex(),
        "reactor_anchor": reactor_anchor,
        "emoji": emoji,
        "occurred_at_ms": int(occurred_at_ms),
        "reaction_sig_suite": REACTION_SIG_SUITE_ED25519,
    }
    inner["reaction_sig"] = b64url_encode(
        sign_ed25519(seed, reaction_signing_bytes(inner))
    )
    return inner


def _pad(body: bytes) -> bytes:
    framed = len(body).to_bytes(2, "big") + body
    return framed + b"\x00" * (-len(framed) % PAD_BUCKET)


def _unpad(plain: bytes) -> bytes:
    if len(plain) < 2 or len(plain) % PAD_BUCKET:
        raise ValueError("sealed reaction is not bucket-padded")
    size = int.from_bytes(plain[:2], "big")
    if size > len(plain) - 2 or any(plain[2 + size :]):
        raise ValueError("sealed reaction padding is malformed")
    return plain[2 : 2 + size]


def seal_reaction(
    *, origin_keywrap_pub: bytes, inner: dict[str, Any]
) -> dict[str, str]:
    """Seal *inner* to the origin's key-wrap key, padded to a
    :data:`PAD_BUCKET` multiple — the ``{kem_suite, eph_pk, ciphertext}``
    dict relays carry."""
    return seal_to_keywrap(
        recipient_keywrap_pub=origin_keywrap_pub,
        plaintext=_pad(orjson.dumps(inner)),
    )


def sealed_shape_ok(sealed: object) -> bool:
    """Whether *sealed* is a key-wrap seal of a forwardable size (what a
    relay checks before passing it on — it can't open it)."""
    if not isinstance(sealed, dict) or set(sealed) != {
        "kem_suite",
        "eph_pk",
        "ciphertext",
    }:
        return False
    return (
        sealed.get("kem_suite") in SUPPORTED_KEM_SUITES
        and isinstance(sealed.get("eph_pk"), str)
        and len(sealed["eph_pk"]) <= 64
        and isinstance(sealed.get("ciphertext"), str)
        and len(sealed["ciphertext"]) <= MAX_SEALED_CHARS
    )


def open_reaction(*, sealed: dict[str, str], keywrap_priv: bytes) -> dict[str, Any]:
    """Open a sealed reaction with this household's key-wrap private key.

    Raises :class:`~socialhome.federation.keywrap_seal.UnsupportedKemSuite`
    for an unknown KEM suite, ``InvalidTag`` when it was not sealed to this
    key, and :class:`ValueError` for anything malformed (padding, JSON,
    field set, field types). Does not check the signature — see
    :func:`verify_reaction`.
    """
    plain = _unpad(open_keywrap(sealed=sealed, recipient_keywrap_priv=keywrap_priv))
    try:
        inner = orjson.loads(plain)
    except orjson.JSONDecodeError as exc:
        raise ValueError("sealed reaction is not JSON") from exc
    if not isinstance(inner, dict):
        raise ValueError("sealed reaction is not an object")  # noqa: TRY004 — one refusal type
    keys = set(inner)
    if not REACTION_FIELDS <= keys or keys - REACTION_FIELDS - OPTIONAL_REACTION_FIELDS:
        raise ValueError("sealed reaction has the wrong fields")
    if inner["kind"] != REACTION_KIND:
        raise ValueError("sealed payload is not a moment reaction")
    for key in (
        "moment_id",
        "author_user_id",
        "origin_instance_id",
        "reactor_user_id",
        "reactor_instance_id",
        "reactor_identity_pk",
        "reactor_anchor",
        "reaction_sig",
    ):
        value = inner[key]
        if not isinstance(value, str) or not 0 < len(value) <= MAX_ID_CHARS:
            raise ValueError(f"sealed reaction field {key} is malformed")
    emoji = inner["emoji"]
    if emoji is not None and not is_valid_moment_reaction(emoji):
        raise ValueError("sealed reaction emoji is not one emoji of ≤ 32 bytes")
    occurred = inner["occurred_at_ms"]
    if not isinstance(occurred, int) or isinstance(occurred, bool) or occurred <= 0:
        raise ValueError("sealed reaction occurred_at_ms is malformed")
    name = inner.get("display_name")
    if name is not None and (
        not isinstance(name, str) or len(name) > MAX_DISPLAY_NAME_CHARS
    ):
        raise ValueError("sealed reaction display_name is malformed")
    return inner


def verify_reaction(inner: dict[str, Any]) -> str | None:
    """Why *inner* is not a reaction genuinely made by the user it names on
    the household it names — or ``None`` when it is.

    * the suite is known (else :class:`UnsupportedReactionSigSuite`);
    * ``reactor_identity_pk`` is a 32-byte key deriving to
      ``reactor_instance_id`` (§4.1.2);
    * ``derive_user_id(reactor_identity_pk, reactor_anchor)`` is
      ``reactor_user_id`` — the user id self-certifies against its
      household's key, as for space items;
    * the household's signature verifies.

    Whether the key matches the one *this* household pins for the reactor's
    household is the caller's check.
    """
    suite = inner.get("reaction_sig_suite")
    if suite not in SUPPORTED_REACTION_SIG_SUITES:
        raise UnsupportedReactionSigSuite(
            f"reaction signature advertises unsupported suite {suite!r}; "
            f"this build supports {sorted(SUPPORTED_REACTION_SIG_SUITES)!r}"
        )
    try:
        pk = bytes.fromhex(str(inner.get("reactor_identity_pk") or ""))
        if derive_instance_id(pk) != inner.get("reactor_instance_id"):
            return "reactor key does not derive to the reactor's household"
        if derive_user_id(pk, str(inner.get("reactor_anchor") or "")) != inner.get(
            "reactor_user_id"
        ):
            return "reactor user id does not derive from its household key"
        sig = b64url_decode(str(inner.get("reaction_sig") or ""))
    except ValueError:
        return "malformed reactor key or signature"
    if not verify_ed25519(pk, reaction_signing_bytes(inner), sig):
        return "reaction signature does not verify"
    return None


__all__ = [
    "MAX_RETURN_TOKENS",
    "MAX_RETURN_TOKEN_CHARS",
    "MAX_SEALED_CHARS",
    "OPTIONAL_REACTION_FIELDS",
    "ORIGIN_KEYWRAP_PK_FIELD",
    "ORIGIN_KEYWRAP_SIG_FIELD",
    "ORIGIN_KEYWRAP_SUITE_FIELD",
    "PAD_BUCKET",
    "REACTION_FIELDS",
    "REACTION_KIND",
    "REACTION_SIG_SUITE_ED25519",
    "RETURN_TOKENS_FIELD",
    "RETURN_TOKEN_SUITE_AESGCM_256",
    "RETURN_TOKEN_SUITE_FIELD",
    "SUPPORTED_REACTION_SIG_SUITES",
    "SUPPORTED_RETURN_TOKEN_SUITES",
    "ReturnTokenRefused",
    "UnsupportedReactionSigSuite",
    "UnsupportedReturnTokenSuite",
    "build_reaction",
    "mint_return_token",
    "open_reaction",
    "open_return_token",
    "origin_keywrap_fields",
    "reaction_signing_bytes",
    "return_token_key",
    "return_tokens_of",
    "seal_reaction",
    "sealed_shape_ok",
    "verified_origin_keywrap",
    "verify_reaction",
]
