"""The gift draw's cryptography — a decryption mix of sealed name-boxes (v_78).

Group-play design §5.4, hardened after the first independent review
(``docs/protocol/app-engines.md``). A draw among *n* people (3 ≤ n ≤ 30)
in *h* households runs in **rounds**; each round:

1. Every household makes a fresh X25519 **mixer key** and signs it with its
   household identity key (Ed25519).
2. For each of its people it draws a fresh 32-byte **person seed**. Every
   secret of that person's round derives from it: the X25519 **box key**
   ``B`` (the private half never leaves the household), a commitment nonce,
   and the ephemeral keys of the **onion** — ``B`` wrapped in one layer per
   mixer, ``seal(M1, seal(M2, … seal(Mh, B)))``. The onion is therefore
   *deterministic* from the seed: a complaint that reveals the seed lets
   anyone rebuild it byte for byte, so a household can't commit to one box
   and onion another. Each household also commits to a random value ``r``
   (``H(ctx ‖ instance ‖ r)``) for the circle's order.
3. The mixers, one after the other, peel their layer, check, shuffle and
   sign the hash of what they took and gave (the **signed chain**). The
   last output is the **set** ``S`` of the n box keys. Each mixer signs the
   hash of every entry it gives (in order), so a person's seed — which
   rebuilds every form of their entry — shows which mixer's output lacks it
   (:func:`onion_layers`).
4. After ``S`` is published every household reveals ``r``; the circle is
   ``S`` sorted by ``H(ctx ‖ R ‖ box)`` with ``R = H(ctx ‖ all r)`` — so no
   mixer (the last one included) chooses who sits next to whom.
5. The box at circle position ``j`` gives to ``j + 1``. Each person's
   **card** (name, household, wish list — exactly :data:`CARD_BYTES`) is
   sealed key-private to the box at ``j − 1`` (their giver); every household
   commits to its sealed cards' hashes before any card is sent.

One seal construction, :func:`seal` / :func:`open_sealed`: an ephemeral
X25519 key, ECDH with the recipient's key, HKDF-SHA256 with ``info``
binding the domain, the round context, a label (``draw|layer|k``,
``draw|card``, ``draw|handover`` — the engine id first), both public keys;
AES-256-GCM with an all-zero nonce (a fresh key per seal). A seal names
nobody: it is **key-private**. Every wire shape carries the suite
(``engine_suite: "x25519-hkdf-aesgcm256"``); a receiver refuses another
with :class:`UnsupportedDrawSuite`.

Pure functions, no I/O. Functions that need randomness take ``rand``
(``os.urandom`` by default) so tests pin vectors with a seeded generator.
"""

from __future__ import annotations

import hashlib
import hmac
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

#: The engine every label and context of this module is bound to.
ENGINE_ID: str = "draw"

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
#: Box keys a hand-over carries at most: the leaver's own and every one it
#: was handed before (a chain of leavers, at most n − 1 of 30).
HANDOVER_KEYS_MAX: int = 29
#: A hand-over's plaintext: who left, their chain of box keys, their
#: household's word — always exactly this many bytes.
HANDOVER_BYTES: int = 1152
SEALED_HANDOVER_BYTES: int = HANDOVER_BYTES + SEAL_OVERHEAD
#: A person seed, a commitment nonce, the circle randomness, a signature.
SEED_BYTES: int = 32
NONCE_BYTES: int = 32
SIG_BYTES: int = 64
DIGEST_BYTES: int = 32

#: The card's byte budget per field (UTF-8).
CARD_LABEL_MAX_BYTES: int = 120
CARD_REF_MAX_BYTES: int = 64
_CARD_VERSION: int = 1
_HANDOVER_VERSION: int = 3

_CTX_DOMAIN = b"socialhome/app_engine/draw/round/v2"
_ORDER_DOMAIN = b"socialhome/app_engine/draw/order/v1"
_SEAL_DOMAIN = b"socialhome/app_engine/draw/seal/v2"
_LIST_DOMAIN = b"socialhome/app_engine/draw/list/v1"
_COMMIT_DOMAIN = b"socialhome/app_engine/draw/commit/v1"
_SEED_DOMAIN = b"socialhome/app_engine/draw/seed/v1"
_R_COMMIT_DOMAIN = b"socialhome/app_engine/draw/r_commit/v1"
_R_DOMAIN = b"socialhome/app_engine/draw/r/v1"
_CIRCLE_DOMAIN = b"socialhome/app_engine/draw/circle/v1"
_KEY_SIG_DOMAIN = b"socialhome/app_engine/draw/mixer_key/v1"
_BATCH_SIG_DOMAIN = b"socialhome/app_engine/draw/onions/v2"
_MIX_SIG_DOMAIN = b"socialhome/app_engine/draw/mixed/v1"
_COMPLAINT_DOMAIN = b"socialhome/app_engine/draw/complaint/v2"
_CARD_SIG_DOMAIN = b"socialhome/app_engine/draw/card/v1"
_CARD_COMMIT_DOMAIN = b"socialhome/app_engine/draw/card_commit/v1"
_DONE_DOMAIN = b"socialhome/app_engine/draw/done/v1"
_OPENING_DOMAIN = b"socialhome/app_engine/draw/opening/v2"
_CARD_OPENING_DOMAIN = b"socialhome/app_engine/draw/card_opening/v1"
_HANDOVER_SIG_DOMAIN = b"socialhome/app_engine/draw/handover/v3"

_ZERO_NONCE = b"\x00" * 12
#: A fixed scalar to test a public key with (any non-zero scalar works).
_PROBE_SCALAR = b"\x40" + b"\x11" * 30 + b"\x40"

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
        raise UnsupportedDrawSuite(f"engine_suite {suite!r}")
    return suite


# ─── Encoding helpers ─────────────────────────────────────────────────────


def _fields(domain: bytes, *parts: bytes | str | int) -> bytes:
    """*domain* then each part length-prefixed (4-byte big-endian): no two
    different field lists encode the same."""
    out = bytearray(domain)
    for part in parts:
        raw = part if isinstance(part, bytes) else str(part).encode("utf-8")
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


def is_usable_key(pub: bytes) -> bool:
    """Whether *pub* is a 32-byte X25519 key one can seal to (no low-order
    point) — every household checks every box in ``S`` publicly."""
    try:
        _exchange(_PROBE_SCALAR, pub)
    except DrawCryptoError:
        return False
    return True


def seed_stream(seed: bytes, label: str) -> Rand:
    """A deterministic byte source from *seed* (HMAC-SHA256 in counter
    mode) — what a person's onion draws its ephemeral keys from."""
    if len(seed) != SEED_BYTES:
        raise DrawCryptoError("seed wrong size")
    key = sha256(_fields(_SEED_DOMAIN, seed, label))
    counter = [0]
    buf = bytearray()

    def take(n: int) -> bytes:
        while len(buf) < n:
            buf.extend(
                hmac.new(key, struct.pack(">Q", counter[0]), hashlib.sha256).digest()
            )
            counter[0] += 1
        out = bytes(buf[:n])
        del buf[:n]
        return out

    return take


@dataclass(slots=True, frozen=True)
class PersonKeys:
    """What a person's round derives from their seed."""

    box_priv: bytes
    box_pub: bytes
    nonce: bytes


def person_keys(seed: bytes) -> PersonKeys:
    """The box key and commitment nonce a person seed derives."""
    stream = seed_stream(seed, "box")
    priv, pub = new_keypair(stream)
    return PersonKeys(box_priv=priv, box_pub=pub, nonce=stream(NONCE_BYTES))


# ─── The round ────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Person:
    """One person in a draw round: their seat, household and username."""

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
    """The 32-byte context every signature and seal of a round is bound to:
    the engine, the suite, the table, the draw and round, the people (by
    seat), each household's identity key and the settings."""
    check_suite(suite)
    parts: list[bytes | str | int] = [
        ENGINE_ID,
        suite,
        session_id,
        gen,
        round_no,
        len(people),
    ]
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
    ``SHA-256(table ‖ gen ‖ round ‖ household)`` — nobody picks it."""
    return sorted(
        set(instances),
        key=lambda i: sha256(_fields(_ORDER_DOMAIN, session_id, gen, round_no, i)),
    )


def r_commitment(ctx: bytes, instance: str, r: bytes) -> bytes:
    """A household's commitment to its circle randomness *r*."""
    if len(r) != SEED_BYTES:
        raise DrawCryptoError("r wrong size")
    return sha256(_fields(_R_COMMIT_DOMAIN, ctx, instance, r))


def round_randomness(ctx: bytes, reveals: dict[str, bytes]) -> bytes:
    """``R`` from every household's revealed *r* (households in id order)."""
    parts: list[bytes | str | int] = [ctx, len(reveals)]
    for inst in sorted(reveals):
        parts += [inst, reveals[inst]]
    return sha256(_fields(_R_DOMAIN, *parts))


def circle(S: Sequence[bytes], *, ctx: bytes, R: bytes) -> list[bytes]:
    """The circle: the box keys of ``S`` sorted by ``H(ctx ‖ R ‖ box)`` —
    the order the mixers left them in doesn't count."""
    return sorted(S, key=lambda box: sha256(_fields(_CIRCLE_DOMAIN, ctx, R, box)))


def circle_targets(position: int, n: int) -> tuple[int, int]:
    """For the box at *position* of *n*: ``(gives_to, sealed_to)``."""
    if n < 2 or not 0 <= position < n:
        raise DrawCryptoError("position out of range")
    return (position + 1) % n, (position - 1) % n


# ─── Seals ────────────────────────────────────────────────────────────────


def _seal_key(
    shared: bytes, *, ctx: bytes, label: str, eph_pub: bytes, to_pub: bytes
) -> bytes:
    info = _fields(_SEAL_DOMAIN, ctx, f"{ENGINE_ID}|{label}", eph_pub, to_pub)
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
    """Seal *plaintext* to *to_pub*: ``eph_pub ‖ AES-256-GCM(k, 0¹², pt)``.
    Key-private: nothing in it names the recipient."""
    eph_priv, eph_pub = new_keypair(rand)
    shared = _exchange(eph_priv, to_pub)
    key = _seal_key(shared, ctx=ctx, label=label, eph_pub=eph_pub, to_pub=to_pub)
    return eph_pub + AESGCM(key).encrypt(_ZERO_NONCE, plaintext, None)


def open_sealed(priv: bytes, blob: bytes, *, ctx: bytes, label: str) -> bytes:
    """Open a seal to the key whose private half is *priv* — else
    :class:`DrawCryptoError`."""
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
    """Wrap *box_pub* in one layer per mixer key (the first mixer's layer
    outermost)."""
    if len(box_pub) != KEY_BYTES:
        raise DrawCryptoError("box key wrong size")
    inner = box_pub
    for k in reversed(range(len(mixer_pubs))):
        inner = seal(mixer_pubs[k], inner, ctx=ctx, label=_layer_label(k), rand=rand)
    return inner


def onion_from_seed(seed: bytes, mixer_pubs: Sequence[bytes], *, ctx: bytes) -> bytes:
    """A person's onion, deterministic from their seed — what a complaint's
    revealed seed lets anyone rebuild byte for byte."""
    return build_onion(
        person_keys(seed).box_pub,
        mixer_pubs,
        ctx=ctx,
        rand=seed_stream(seed, "onion"),
    )


def onion_layers(
    seed: bytes, mixer_pubs: Sequence[bytes], *, ctx: bytes
) -> list[bytes]:
    """Every form a person's entry takes on its way through the mix:
    ``[k]`` is what mixer *k* takes (``[0]`` the onion, ``[h]`` the box
    key) — so a revealed seed shows which mixer's output lacks it."""
    stream = seed_stream(seed, "onion")
    inner = person_keys(seed).box_pub
    forms = [inner]
    for k in reversed(range(len(mixer_pubs))):
        inner = seal(mixer_pubs[k], inner, ctx=ctx, label=_layer_label(k), rand=stream)
        forms.append(inner)
    return list(reversed(forms))


def peel(mixer_priv: bytes, onion: bytes, *, ctx: bytes, k: int, h: int) -> bytes:
    """Mixer *k* (0-based, of *h*) takes its layer off *onion*."""
    if len(onion) != onion_size(h - k):
        raise DrawCryptoError("onion wrong size")
    inner = open_sealed(mixer_priv, onion, ctx=ctx, label=_layer_label(k))
    if len(inner) != onion_size(h - k - 1):
        raise DrawCryptoError("layer wrong size")
    return inner


def _randbelow(bound: int, rand: Rand) -> int:
    """A uniform integer in ``[0, bound)`` (rejection sampling)."""
    if bound <= 1:
        return 0
    nbytes = (bound.bit_length() + 7) // 8 + 1
    limit = (256**nbytes // bound) * bound
    while True:
        value = int.from_bytes(rand(nbytes), "big")
        if value < limit:
            return value % bound


def shuffle(items: list[bytes], rand: Rand = os.urandom) -> list[bytes]:
    """A uniformly random permutation of *items* (Fisher–Yates), new list."""
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
    distinct (before and after), shuffle."""
    if len(set(entries)) != len(entries):
        raise DrawCryptoError("duplicate entry in the input")
    out = [peel(mixer_priv, e, ctx=ctx, k=k, h=h) for e in entries]
    if len(set(out)) != len(out):
        raise DrawCryptoError("duplicate entry in the output")
    return shuffle(out, rand)


def entry_hash(entry: bytes) -> bytes:
    return sha256(entry)


def list_digest(hashes_: Sequence[bytes]) -> bytes:
    """The digest a mixer signs for a list, from its entries' hashes."""
    return sha256(_fields(_LIST_DOMAIN, len(hashes_), *hashes_))


def commitment(
    ctx: bytes, instance: str, user: str, box_pub: bytes, nonce: bytes
) -> bytes:
    """A hiding commitment to *user*'s box key this round."""
    if len(nonce) != NONCE_BYTES:
        raise DrawCryptoError("nonce wrong size")
    return sha256(_fields(_COMMIT_DOMAIN, ctx, instance, user, box_pub, nonce))


def person_commitment(ctx: bytes, instance: str, user: str, seed: bytes) -> bytes:
    """:func:`commitment` for the keys *seed* derives."""
    keys = person_keys(seed)
    return commitment(ctx, instance, user, keys.box_pub, keys.nonce)


# ─── What households sign ─────────────────────────────────────────────────


def key_message(ctx: bytes, instance: str, mixer_pub: bytes) -> bytes:
    return _fields(_KEY_SIG_DOMAIN, ctx, instance, mixer_pub)


def batch_message(
    ctx: bytes,
    instance: str,
    onion_hashes: Sequence[bytes],
    commits: Sequence[tuple[str, bytes]],
    r_commit: bytes,
) -> bytes:
    """A household's onions (by hash, in its order), its people's
    commitments (by username) and its circle-randomness commitment."""
    parts: list[bytes | str | int] = [ctx, instance, len(onion_hashes), *onion_hashes]
    parts.append(len(commits))
    for user, c in sorted(commits):
        parts += [user, c]
    parts.append(r_commit)
    return _fields(_BATCH_SIG_DOMAIN, *parts)


def mix_message(
    ctx: bytes, k: int, instance: str, in_digest: bytes, out_digest: bytes
) -> bytes:
    return _fields(_MIX_SIG_DOMAIN, ctx, k, instance, in_digest, out_digest)


def complaint_message(
    ctx: bytes, instance: str, kind: str, user: str, seed: bytes
) -> bytes:
    """A household stopping a round with its proof: the person's seed."""
    return _fields(_COMPLAINT_DOMAIN, ctx, instance, kind, user, seed)


def opening_message(
    ctx: bytes,
    instance: str,
    seeds: Sequence[tuple[str, bytes]],
    *,
    stuck: int | None = None,
) -> bytes:
    """A household opening every seed of a round that broke before its
    cards — *stuck*: it is mixer *stuck* and its input doesn't peel."""
    parts: list[bytes | str | int] = [ctx, instance, len(seeds)]
    for user, seed in sorted(seeds):
        parts += [user, seed]
    parts.append("" if stuck is None else stuck)
    return _fields(_OPENING_DOMAIN, *parts)


def card_opening_message(
    ctx: bytes,
    instance: str,
    entries: Sequence[tuple[str, bytes, bytes, bytes, bytes]],
) -> bytes:
    """A household opening, for a round whose card complaint named nobody,
    each of its people's box key, commitment nonce, and where it sealed
    their card: ``(user, box_pub, nonce, target_pub, eph_priv)``."""
    parts: list[bytes | str | int] = [ctx, instance, len(entries)]
    for user, box_pub, nonce, target, eph in sorted(entries):
        parts += [user, box_pub, nonce, target, eph]
    return _fields(_CARD_OPENING_DOMAIN, *parts)


def card_commit_message(
    ctx: bytes, instance: str, card_hashes: Sequence[bytes]
) -> bytes:
    """A household committing to its sealed cards before any card is out."""
    return _fields(_CARD_COMMIT_DOMAIN, ctx, instance, *sorted(card_hashes))


def done_message(ctx: bytes, instance: str, count: int) -> bytes:
    """A household saying its people's names opened (signed: a hub can't
    complete a round for it)."""
    return _fields(_DONE_DOMAIN, ctx, instance, count)


# ─── Cards ────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Card:
    """A person's card, signed by their own household for the round."""

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
            raise DrawCryptoError("malformed")
        (size,) = struct.unpack(">H", raw[at : at + 2])
        at += 2
        if at + size > len(raw):
            raise DrawCryptoError("malformed")
        try:
            out.append(raw[at : at + size].decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise DrawCryptoError("malformed") from exc
        at += size
    return out, at


def decode_card(raw: bytes) -> Card:
    """The card in *raw* (exactly :data:`CARD_BYTES`, zero-padded — strict).
    The signature is not checked here."""
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


def card_rand(seed: bytes) -> Rand:
    """The randomness a person's card of the round is sealed with — from
    their seed, so their household can later show where it sealed it."""
    return seed_stream(seed, "card")


def card_eph(seed: bytes) -> bytes:
    """The ephemeral private key of the seal of a person's card."""
    return new_keypair(card_rand(seed))[0]


def open_card_as_sealer(
    eph_priv: bytes, target_pub: bytes, sealed: bytes, *, ctx: bytes
) -> bytes | None:
    """The card in *sealed* when it was sealed with *eph_priv* to
    *target_pub* (what its sealer opens to show where it sealed it)."""
    if len(sealed) != SEALED_CARD_BYTES or len(target_pub) != KEY_BYTES:
        return None
    try:
        eph_pub = public_of(eph_priv)
        if sealed[:KEY_BYTES] != eph_pub:
            return None
        shared = _exchange(eph_priv, target_pub)
    except DrawCryptoError:
        return None
    key = _seal_key(shared, ctx=ctx, label="card", eph_pub=eph_pub, to_pub=target_pub)
    try:
        return AESGCM(key).decrypt(_ZERO_NONCE, sealed[KEY_BYTES:], None)
    except InvalidTag:
        return None


def open_card(box_priv: bytes, sealed: bytes, *, ctx: bytes) -> bytes | None:
    """The card bytes when *sealed* is for the box *box_priv*, else ``None``."""
    if len(sealed) != SEALED_CARD_BYTES:
        raise DrawCryptoError("sealed card wrong size")
    try:
        return open_sealed(box_priv, sealed, ctx=ctx, label="card")
    except DrawCryptoError:
        return None


def try_cards(
    box_priv: bytes, sealed_cards: Sequence[bytes], *, ctx: bytes
) -> list[bytes]:
    """Every card among *sealed_cards* that opens with *box_priv*."""
    out = []
    for sealed in sealed_cards:
        opened = open_card(box_priv, sealed, ctx=ctx)
        if opened is not None:
            out.append(opened)
    return out


# ─── Hand-over (a leaver after the reveal) ────────────────────────────────


def handover_message(
    ctx: bytes, instance: str, leaver: str, chain_pubs: Sequence[bytes]
) -> bytes:
    """What the leaver's household signs when it hands its person's box
    keys (their own, then every one handed to them) to that person's
    giver."""
    return _fields(
        _HANDOVER_SIG_DOMAIN, ctx, instance, leaver, len(chain_pubs), *chain_pubs
    )


def encode_handover(
    instance: str, leaver: str, keys: Sequence[bytes], sig: bytes
) -> bytes:
    """``version ‖ lp16(instance) ‖ lp16(leaver) ‖ count ‖ keys ‖ sig ‖
    zeros`` — exactly :data:`HANDOVER_BYTES`."""
    if (
        len(sig) != SIG_BYTES
        or not 1 <= len(keys) <= HANDOVER_KEYS_MAX
        or any(len(k) != KEY_BYTES for k in keys)
    ):
        raise DrawCryptoError("hand-over malformed")
    head = bytearray([_HANDOVER_VERSION])
    for part in (instance, leaver):
        raw = part.encode("utf-8")
        if len(raw) > CARD_REF_MAX_BYTES:
            raise DrawCryptoError("hand-over malformed")
        head += struct.pack(">H", len(raw)) + raw
    head.append(len(keys))
    raw = bytes(head) + b"".join(keys) + sig
    return raw + b"\x00" * (HANDOVER_BYTES - len(raw))


def decode_handover(raw: bytes) -> tuple[str, str, list[bytes], bytes]:
    """``(instance, leaver, keys, sig)`` from a hand-over plaintext."""
    if len(raw) != HANDOVER_BYTES or raw[0] != _HANDOVER_VERSION:
        raise DrawCryptoError("hand-over malformed")
    (instance, leaver), at = _read_fields(raw, 1, 2)
    count = raw[at]
    at += 1
    end = at + count * KEY_BYTES + SIG_BYTES
    if not 1 <= count <= HANDOVER_KEYS_MAX or end > HANDOVER_BYTES or any(raw[end:]):
        raise DrawCryptoError("hand-over malformed")
    keys = [raw[at + i * KEY_BYTES : at + (i + 1) * KEY_BYTES] for i in range(count)]
    return instance, leaver, keys, raw[end - SIG_BYTES : end]


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
    "ENGINE_ID",
    "HANDOVER_BYTES",
    "HANDOVER_KEYS_MAX",
    "SEALED_CARD_BYTES",
    "SEALED_HANDOVER_BYTES",
    "SUPPORTED_DRAW_SUITES",
    "Card",
    "DrawCryptoError",
    "Person",
    "PersonKeys",
    "UnsupportedDrawSuite",
]
