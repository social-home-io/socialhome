"""The gift draw's cryptography — a decryption mix of sealed name-boxes (v_78).

Group-play design §5.4. A draw among *n* people (3 ≤ n ≤ 30) in *h*
households runs in **rounds**; each round:

1. Every household makes a fresh X25519 **mixer key** and signs its public
   half with its household identity key (Ed25519).
2. For each of its people it makes a fresh X25519 **box key** ``B`` (the
   private half never leaves the household) and wraps the public half in
   one layer per mixer, outermost first in the mixing order:
   ``seal(M1, seal(M2, … seal(Mh, B)))`` — an **onion**. All onions of a
   round have the same size. It also commits to which box is whose with a
   hiding commitment ``H(ctx, instance, user, B, nonce)``.
3. The mixers, one after the other, each peel their layer off every
   entry, check each opened, shuffle with a CSPRNG and sign the hash of
   what they got and what they give on (the **signed chain**). The last
   output is ``S``: the n box keys in an order nobody knows.
4. The person whose box sits at position ``j`` gives to whoever holds
   ``j + 1`` (the last gives to the first) — a random circle: nobody draws
   themselves, everyone gives and gets once. Each household seals each of
   its people's **card** (name, household, wish list — exactly
   :data:`CARD_BYTES`) to the box at ``j − 1`` (their giver) with a
   key-private seal; each household tries its boxes on all n cards and
   exactly one opens per person.

Primitives
----------
One construction, :func:`seal` / :func:`open_sealed`: an ephemeral X25519
key, ECDH with the recipient's public key, HKDF-SHA256 over the shared
secret with ``info`` binding the domain, the round context, a label
(``layer|k``, ``card``, ``handover``), the ephemeral public key and the
recipient's public key, then AES-256-GCM with an all-zero nonce — safe
because every seal derives a fresh key from a fresh ephemeral key, so no
nonce travels. A seal is ``eph_pub ‖ ct`` and names nobody: it is
**key-private** (Bellare–Boldyreva–Desai–Pointcheval, ASIACRYPT 2001) — the
relay can't tell whom a card is for. Every seal of one kind has one size.

Every wire shape carries ``draw_suite`` (:data:`DRAW_SUITE_X25519_AESGCM`);
a receiver refuses any other value with :class:`UnsupportedDrawSuite` —
never a default (CLAUDE.md, "crypto wire shapes carry a suite tag").

Pure functions, no I/O. Every function that needs randomness takes
``rand`` (``os.urandom`` by default) so tests can pin test vectors with a
seeded generator; production never passes one.
"""

from __future__ import annotations

import hashlib
import os
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

#: The one draw suite: X25519 + HKDF-SHA256 + AES-256-GCM seals,
#: Ed25519 household signatures. Phase 2 of ``docs/crypto.md`` adds a
#: hybrid KEM beside it (the wire shape keeps the suite field).
DRAW_SUITE_X25519_AESGCM: str = "x25519-hkdf-aesgcm256"
SUPPORTED_DRAW_SUITES: frozenset[str] = frozenset({DRAW_SUITE_X25519_AESGCM})

#: Bytes of an X25519 key (either half) and of an AES-GCM tag.
KEY_BYTES: int = 32
TAG_BYTES: int = 16
#: What one seal adds to its plaintext: the ephemeral public key + the tag.
SEAL_OVERHEAD: int = KEY_BYTES + TAG_BYTES
#: A card's plaintext — always exactly this many bytes (owner, 2026-10-31).
CARD_BYTES: int = 1024
#: A sealed card on the wire (before base64).
SEALED_CARD_BYTES: int = CARD_BYTES + SEAL_OVERHEAD
#: A hand-over's plaintext: the leaver's word around their recipient's card.
HANDOVER_BYTES: int = CARD_BYTES + 256
SEALED_HANDOVER_BYTES: int = HANDOVER_BYTES + SEAL_OVERHEAD
#: A commitment's nonce and an Ed25519 signature.
NONCE_BYTES: int = 32
SIG_BYTES: int = 64
DIGEST_BYTES: int = 32

#: The card's byte budget per field (UTF-8). A name or household name
#: longer than :data:`CARD_LABEL_MAX_BYTES` is cut on a character boundary;
#: a wish list that doesn't fit is refused by the caller before it is set.
CARD_LABEL_MAX_BYTES: int = 120
CARD_REF_MAX_BYTES: int = 64
_CARD_VERSION: int = 1
_HANDOVER_VERSION: int = 1

_CTX_DOMAIN = b"socialhome/app_draw/round/v1"
_ORDER_DOMAIN = b"socialhome/app_draw/order/v1"
_SEAL_DOMAIN = b"socialhome/app_draw/seal/v1"
_LIST_DOMAIN = b"socialhome/app_draw/list/v1"
_COMMIT_DOMAIN = b"socialhome/app_draw/commit/v1"
_KEY_SIG_DOMAIN = b"socialhome/app_draw/mixer_key/v1"
_BATCH_SIG_DOMAIN = b"socialhome/app_draw/onions/v1"
_MIX_SIG_DOMAIN = b"socialhome/app_draw/mixed/v1"
_COMPLAINT_DOMAIN = b"socialhome/app_draw/complaint/v1"
_CARD_SIG_DOMAIN = b"socialhome/app_draw/card/v1"
_HANDOVER_SIG_DOMAIN = b"socialhome/app_draw/handover/v1"

_ZERO_NONCE = b"\x00" * 12

#: A source of random bytes (``os.urandom``'s shape).
Rand = Callable[[int], bytes]


class DrawCryptoError(ValueError):
    """A draw value out of shape, a seal that doesn't open, a signature or a
    list that doesn't check out — the step is refused."""


class UnsupportedDrawSuite(ValueError):
    """A draw message names a suite this build doesn't know (or none).
    Refused — never a fallback."""


def check_suite(suite: object) -> str:
    """*suite* when this build speaks it; else :class:`UnsupportedDrawSuite`."""
    if not isinstance(suite, str) or suite not in SUPPORTED_DRAW_SUITES:
        raise UnsupportedDrawSuite(f"draw_suite {suite!r}")
    return suite


# ─── Encoding helpers ─────────────────────────────────────────────────────


def _fields(domain: bytes, *parts: bytes | str | int) -> bytes:
    """*domain* then each part length-prefixed (4-byte big-endian): no two
    different field lists encode the same."""
    out = bytearray(domain)
    for part in parts:
        if isinstance(part, bytes):
            raw = part
        else:
            raw = str(part).encode("utf-8")
        out += struct.pack(">I", len(raw)) + raw
    return bytes(out)


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


# ─── Keys ─────────────────────────────────────────────────────────────────


def new_keypair(rand: Rand = os.urandom) -> tuple[bytes, bytes]:
    """A fresh X25519 key pair ``(private, public)``, 32 bytes each."""
    priv = X25519PrivateKey.from_private_bytes(rand(KEY_BYTES))
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv.private_bytes_raw(), pub


def public_of(priv: bytes) -> bytes:
    """The public half of the X25519 private key *priv*."""
    if len(priv) != KEY_BYTES:
        raise DrawCryptoError("private key wrong size")
    return (
        X25519PrivateKey.from_private_bytes(priv)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )


def _exchange(priv: bytes, pub: bytes) -> bytes:
    if len(priv) != KEY_BYTES or len(pub) != KEY_BYTES:
        raise DrawCryptoError("key wrong size")
    try:
        return X25519PrivateKey.from_private_bytes(priv).exchange(
            X25519PublicKey.from_public_bytes(pub)
        )
    except ValueError as exc:  # a low-order point (all-zero secret)
        raise DrawCryptoError("key agreement failed") from exc


# ─── The round ────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Person:
    """One person in a draw round: their seat at the table, their
    household and their username there."""

    seat: int
    instance: str
    user: str


def round_context(
    *,
    suite: str,
    session_id: str,
    gen: int,
    round_no: int,
    people: Sequence[Person],
    identities: dict[str, str],
    settings_digest: bytes,
) -> bytes:
    """The 32-byte context every signature and seal of a round is bound to.

    It covers the table, the draw (*gen*: 1, then +1 per "Draw again") and
    the round, the people (by seat), each household's identity key (hex)
    and the draw's settings — so a relay can't show two households two
    different rounds and have their signatures agree."""
    check_suite(suite)
    parts: list[bytes | str | int] = [suite, session_id, gen, round_no, len(people)]
    for p in sorted(people, key=lambda p: p.seat):
        parts += [p.seat, p.instance, p.user]
    for inst in sorted(identities):
        parts += [inst, identities[inst]]
    parts.append(settings_digest)
    return sha256(_fields(_CTX_DOMAIN, *parts))


def mix_order(
    session_id: str, gen: int, round_no: int, instances: Sequence[str]
) -> list[str]:
    """The households in mixing order: sorted by
    ``SHA-256(session ‖ gen ‖ round ‖ instance)`` — nobody picks it."""
    return sorted(
        set(instances),
        key=lambda i: sha256(_fields(_ORDER_DOMAIN, session_id, gen, round_no, i)),
    )


def circle_targets(position: int, n: int) -> tuple[int, int]:
    """For the box at *position* of *n*: ``(gives_to, sealed_to)`` — the
    position whose person this one gives to, and the position (its giver)
    its own card is sealed to."""
    if n < 2 or not 0 <= position < n:
        raise DrawCryptoError("position out of range")
    return (position + 1) % n, (position - 1) % n


# ─── Seals ────────────────────────────────────────────────────────────────


def _seal_key(
    shared: bytes, *, ctx: bytes, label: str, eph_pub: bytes, to_pub: bytes
) -> bytes:
    info = _fields(_SEAL_DOMAIN, ctx, label, eph_pub, to_pub)
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(
        shared
    )


def seal(
    to_pub: bytes,
    plaintext: bytes,
    *,
    ctx: bytes,
    label: str,
    rand: Rand = os.urandom,
) -> bytes:
    """Seal *plaintext* to the X25519 public key *to_pub*:
    ``eph_pub ‖ AES-256-GCM(k, 0¹², plaintext)``. Key-private: nothing in
    it names the recipient."""
    eph_priv, eph_pub = new_keypair(rand)
    shared = _exchange(eph_priv, to_pub)
    key = _seal_key(shared, ctx=ctx, label=label, eph_pub=eph_pub, to_pub=to_pub)
    return eph_pub + AESGCM(key).encrypt(_ZERO_NONCE, plaintext, None)


def open_sealed(priv: bytes, blob: bytes, *, ctx: bytes, label: str) -> bytes:
    """Open a seal to the key pair whose private half is *priv*. Raises
    :class:`DrawCryptoError` when it isn't for that key (or was changed,
    or was made for another round or label)."""
    if len(blob) < SEAL_OVERHEAD:
        raise DrawCryptoError("seal too short")
    eph_pub = blob[:KEY_BYTES]
    shared = _exchange(priv, eph_pub)
    key = _seal_key(
        shared, ctx=ctx, label=label, eph_pub=eph_pub, to_pub=public_of(priv)
    )
    try:
        return AESGCM(key).decrypt(_ZERO_NONCE, blob[KEY_BYTES:], None)
    except InvalidTag as exc:
        raise DrawCryptoError("seal doesn't open") from exc


# ─── Onions and the mix ───────────────────────────────────────────────────


def onion_size(layers: int) -> int:
    """An onion with *layers* layers left around a 32-byte box key."""
    return KEY_BYTES + layers * SEAL_OVERHEAD


def _layer_label(k: int) -> str:
    return f"layer|{k}"


def build_onion(
    box_pub: bytes,
    mixer_pubs: Sequence[bytes],
    *,
    ctx: bytes,
    rand: Rand = os.urandom,
) -> bytes:
    """Wrap *box_pub* in one layer per mixer key (in mixing order: the
    first mixer's layer outermost)."""
    if len(box_pub) != KEY_BYTES:
        raise DrawCryptoError("box key wrong size")
    inner = box_pub
    for k in reversed(range(len(mixer_pubs))):
        inner = seal(mixer_pubs[k], inner, ctx=ctx, label=_layer_label(k), rand=rand)
    return inner


def peel(mixer_priv: bytes, onion: bytes, *, ctx: bytes, k: int, h: int) -> bytes:
    """Mixer *k* (0-based, of *h*) takes its layer off *onion*."""
    if len(onion) != onion_size(h - k):
        raise DrawCryptoError("onion wrong size")
    inner = open_sealed(mixer_priv, onion, ctx=ctx, label=_layer_label(k))
    if len(inner) != onion_size(h - k - 1):
        raise DrawCryptoError("layer wrong size")
    return inner


def _randbelow(bound: int, rand: Rand) -> int:
    """A uniform integer in ``[0, bound)`` from *rand* (rejection
    sampling — no modulo bias)."""
    if bound <= 1:
        return 0
    nbytes = (bound.bit_length() + 7) // 8 + 1
    limit = (256**nbytes // bound) * bound
    while True:
        value = int.from_bytes(rand(nbytes), "big")
        if value < limit:
            return value % bound


def shuffle(items: list[bytes], rand: Rand = os.urandom) -> list[bytes]:
    """A uniformly random permutation of *items* (Fisher–Yates over a
    CSPRNG), as a new list."""
    out = list(items)
    for i in range(len(out) - 1, 0, -1):
        j = _randbelow(i + 1, rand)
        out[i], out[j] = out[j], out[i]
    return out


def mix(
    mixer_priv: bytes,
    entries: Sequence[bytes],
    *,
    ctx: bytes,
    k: int,
    h: int,
    rand: Rand = os.urandom,
) -> list[bytes]:
    """Mixer *k*'s step: peel every entry, check each opened and all are
    distinct (before and after), and shuffle. Raises
    :class:`DrawCryptoError` on anything off — the mixer then refuses."""
    if len(set(entries)) != len(entries):
        raise DrawCryptoError("duplicate entry in the input")
    out = [peel(mixer_priv, e, ctx=ctx, k=k, h=h) for e in entries]
    if len(set(out)) != len(out):
        raise DrawCryptoError("duplicate entry in the output")
    return shuffle(out, rand)


def entry_hash(entry: bytes) -> bytes:
    return sha256(entry)


def list_digest(hashes_: Sequence[bytes]) -> bytes:
    """The digest a mixer signs for a list, from its entries' hashes in
    order (so a household can check the chain from hashes alone)."""
    return sha256(_fields(_LIST_DOMAIN, len(hashes_), *hashes_))


def commitment(
    ctx: bytes, instance: str, user: str, box_pub: bytes, nonce: bytes
) -> bytes:
    """A hiding commitment to *user*'s box key this round."""
    if len(nonce) != NONCE_BYTES:
        raise DrawCryptoError("nonce wrong size")
    return sha256(_fields(_COMMIT_DOMAIN, ctx, instance, user, box_pub, nonce))


# ─── What households sign ─────────────────────────────────────────────────


def key_message(ctx: bytes, instance: str, mixer_pub: bytes) -> bytes:
    """What a household signs to vouch for its mixer key this round."""
    return _fields(_KEY_SIG_DOMAIN, ctx, instance, mixer_pub)


def batch_message(
    ctx: bytes,
    instance: str,
    onion_hashes: Sequence[bytes],
    commits: Sequence[tuple[str, bytes]],
) -> bytes:
    """What a household signs over its onions (by hash, in its order) and
    its people's commitments (by username, sorted)."""
    parts: list[bytes | str | int] = [ctx, instance, len(onion_hashes), *onion_hashes]
    parts.append(len(commits))
    for user, c in sorted(commits):
        parts += [user, c]
    return _fields(_BATCH_SIG_DOMAIN, *parts)


def mix_message(
    ctx: bytes, k: int, instance: str, in_digest: bytes, out_digest: bytes
) -> bytes:
    """What mixer *k* signs: the list it took and the list it gave on."""
    return _fields(_MIX_SIG_DOMAIN, ctx, k, instance, in_digest, out_digest)


def complaint_message(
    ctx: bytes, instance: str, kind: str, user: str, box_priv: bytes, nonce: bytes
) -> bytes:
    """What a household signs when it stops a round with its proof."""
    return _fields(_COMPLAINT_DOMAIN, ctx, instance, kind, user, box_priv, nonce)


# ─── Cards ────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Card:
    """A person's card: who they are (as their own household knows them),
    their wish list, and which revision (0, then +1 per update after the
    reveal). ``sig`` is their household's signature over all of it and the
    round (:func:`card_message`)."""

    instance: str
    user: str
    name: str
    household: str
    wish: str
    rev: int
    sig: bytes = b""


def cut_utf8(text: str, max_bytes: int) -> str:
    """*text* cut to at most *max_bytes* of UTF-8, on a character boundary."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def card_message(ctx: bytes, card: Card) -> bytes:
    return _fields(
        _CARD_SIG_DOMAIN,
        ctx,
        card.instance,
        card.user,
        card.name,
        card.household,
        card.wish,
        card.rev,
    )


def _card_body(card: Card) -> bytes:
    parts = (card.instance, card.user, card.name, card.household, card.wish)
    out = bytearray([_CARD_VERSION, card.rev & 0xFF])
    for part in parts:
        raw = part.encode("utf-8")
        out += struct.pack(">H", len(raw)) + raw
    return bytes(out)


def card_fits(card: Card) -> bool:
    """Whether *card* (signed) fits :data:`CARD_BYTES`."""
    return 2 + len(_card_body(card)) + SIG_BYTES <= CARD_BYTES


def encode_card(card: Card) -> bytes:
    """*card* as exactly :data:`CARD_BYTES`: ``u16 len ‖ body ‖ sig ‖ zeros``."""
    if (
        len(card.sig) != SIG_BYTES
        or not 0 <= card.rev <= 255
        or len(card.instance.encode()) > CARD_REF_MAX_BYTES
        or len(card.user.encode()) > CARD_REF_MAX_BYTES
        or len(card.name.encode()) > CARD_LABEL_MAX_BYTES
        or len(card.household.encode()) > CARD_LABEL_MAX_BYTES
        or not card_fits(card)
    ):
        raise DrawCryptoError("card doesn't fit")
    body = _card_body(card)
    raw = struct.pack(">H", len(body)) + body + card.sig
    return raw + b"\x00" * (CARD_BYTES - len(raw))


def _read_fields(raw: bytes, at: int, count: int) -> tuple[list[str], int]:
    out: list[str] = []
    for _ in range(count):
        if at + 2 > len(raw):
            raise DrawCryptoError("card malformed")
        (size,) = struct.unpack(">H", raw[at : at + 2])
        at += 2
        if at + size > len(raw):
            raise DrawCryptoError("card malformed")
        try:
            out.append(raw[at : at + size].decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise DrawCryptoError("card malformed") from exc
        at += size
    return out, at


def decode_card(raw: bytes) -> Card:
    """The card in *raw* (exactly :data:`CARD_BYTES`, zero-padded — strict:
    any other padding is refused). The signature is not checked here."""
    if len(raw) != CARD_BYTES:
        raise DrawCryptoError("card wrong size")
    (body_len,) = struct.unpack(">H", raw[:2])
    end = 2 + body_len
    if body_len < 2 or end + SIG_BYTES > CARD_BYTES:
        raise DrawCryptoError("card malformed")
    body = raw[2:end]
    if body[0] != _CARD_VERSION:
        raise DrawCryptoError("card version unknown")
    fields, at = _read_fields(body, 2, 5)
    if at != len(body):
        raise DrawCryptoError("card malformed")
    if any(raw[end + SIG_BYTES :]):
        raise DrawCryptoError("card padding not zero")
    instance, user, name, household, wish = fields
    return Card(
        instance=instance,
        user=user,
        name=name,
        household=household,
        wish=wish,
        rev=body[1],
        sig=raw[end : end + SIG_BYTES],
    )


def seal_card(
    giver_box_pub: bytes, card_bytes: bytes, *, ctx: bytes, rand: Rand = os.urandom
) -> bytes:
    """A card sealed (key-private) to its giver's box key."""
    if len(card_bytes) != CARD_BYTES:
        raise DrawCryptoError("card wrong size")
    return seal(giver_box_pub, card_bytes, ctx=ctx, label="card", rand=rand)


def open_card(box_priv: bytes, sealed: bytes, *, ctx: bytes) -> bytes | None:
    """The card bytes when *sealed* is for the box *box_priv*, else
    ``None``."""
    if len(sealed) != SEALED_CARD_BYTES:
        raise DrawCryptoError("sealed card wrong size")
    try:
        return open_sealed(box_priv, sealed, ctx=ctx, label="card")
    except DrawCryptoError:
        return None


def try_cards(
    box_priv: bytes, sealed_cards: Sequence[bytes], *, ctx: bytes
) -> list[bytes]:
    """Every card among *sealed_cards* that opens with *box_priv* — exactly
    one in an honest round."""
    out = []
    for sealed in sealed_cards:
        opened = open_card(box_priv, sealed, ctx=ctx)
        if opened is not None:
            out.append(opened)
    return out


# ─── Hand-over (a leaver after the reveal) ────────────────────────────────


def handover_message(
    ctx: bytes, instance: str, leaver: str, card_bytes: bytes
) -> bytes:
    """What the leaver's household signs when it passes the card its
    person drew on to that person's giver."""
    return _fields(_HANDOVER_SIG_DOMAIN, ctx, instance, leaver, sha256(card_bytes))


def encode_handover(instance: str, leaver: str, sig: bytes, card_bytes: bytes) -> bytes:
    """``version ‖ u16 len ‖ instance ‖ u16 len ‖ leaver ‖ sig ‖ card ‖
    zeros`` — exactly :data:`HANDOVER_BYTES`."""
    if len(sig) != SIG_BYTES or len(card_bytes) != CARD_BYTES:
        raise DrawCryptoError("hand-over malformed")
    head = bytearray([_HANDOVER_VERSION])
    for part in (instance, leaver):
        raw = part.encode("utf-8")
        if len(raw) > CARD_REF_MAX_BYTES:
            raise DrawCryptoError("hand-over malformed")
        head += struct.pack(">H", len(raw)) + raw
    raw = bytes(head) + sig + card_bytes
    return raw + b"\x00" * (HANDOVER_BYTES - len(raw))


def decode_handover(raw: bytes) -> tuple[str, str, bytes, bytes]:
    """``(instance, leaver, sig, card_bytes)`` from a hand-over plaintext."""
    if len(raw) != HANDOVER_BYTES or raw[0] != _HANDOVER_VERSION:
        raise DrawCryptoError("hand-over malformed")
    (instance, leaver), at = _read_fields(raw, 1, 2)
    end = at + SIG_BYTES + CARD_BYTES
    if end > HANDOVER_BYTES or any(raw[end:]):
        raise DrawCryptoError("hand-over malformed")
    return instance, leaver, raw[at : at + SIG_BYTES], raw[at + SIG_BYTES : end]


def seal_handover(
    giver_box_pub: bytes, plaintext: bytes, *, ctx: bytes, rand: Rand = os.urandom
) -> bytes:
    if len(plaintext) != HANDOVER_BYTES:
        raise DrawCryptoError("hand-over wrong size")
    return seal(giver_box_pub, plaintext, ctx=ctx, label="handover", rand=rand)


def open_handover(box_priv: bytes, sealed: bytes, *, ctx: bytes) -> bytes | None:
    if len(sealed) != SEALED_HANDOVER_BYTES:
        raise DrawCryptoError("sealed hand-over wrong size")
    try:
        return open_sealed(box_priv, sealed, ctx=ctx, label="handover")
    except DrawCryptoError:
        return None


__all__ = [
    "CARD_BYTES",
    "DRAW_SUITE_X25519_AESGCM",
    "HANDOVER_BYTES",
    "SEALED_CARD_BYTES",
    "SEALED_HANDOVER_BYTES",
    "SUPPORTED_DRAW_SUITES",
    "Card",
    "DrawCryptoError",
    "Person",
    "UnsupportedDrawSuite",
]
