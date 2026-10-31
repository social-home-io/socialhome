"""The gift draw's cryptography (``federation/draw_crypto.py``, v_78).

* **Test vectors** — every household must compute the same round context,
  mixing order, onion, card seal and commitment from the same inputs:
  seeded randomness pins the bytes (a change here breaks the wire between
  versions), and the seal is re-derived step by step from the primitives.
* **Properties** — a full mix over random n / h yields a single circle
  (a derangement, everyone gives and gets once); each box opens exactly
  its one card; seals of one kind all have one size (key-private).
* **Tampering** — a wrong layer, round, label, key, size, a flipped byte,
  a duplicate, a low-order point, a bad card padding, an unknown suite:
  each is refused.
"""

from __future__ import annotations

import hashlib
import random
import struct

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from socialhome.federation import draw_crypto as dc

SEED = 20261031
PEOPLE = [
    dc.Person(1, "aaa", "anna"),
    dc.Person(2, "bbb", "ben"),
    dc.Person(3, "ccc", "cleo"),
]
IDS = {"aaa": "00" * 32, "bbb": "11" * 32, "ccc": "22" * 32}


def _ctx(**over) -> bytes:
    args = {
        "suite": dc.DRAW_SUITE_X25519_AESGCM,
        "session_id": "t1",
        "gen": 1,
        "round_no": 1,
        "people": PEOPLE,
        "identities": IDS,
        "settings_digest": hashlib.sha256(b"{}").digest(),
    }
    args.update(over)
    return dc.round_context(**args)


# ─── Test vectors ─────────────────────────────────────────────────────────


def test_vectors_round_context_and_order():
    assert _ctx().hex() == (
        "ac85c841a9f3a1651890a7d078ee1eda953bf5c8cec9f8dc4b6d66c04eb86a6a"
    )
    assert dc.mix_order("t1", 1, 1, ["aaa", "bbb", "ccc"]) == ["ccc", "bbb", "aaa"]
    # The people's order on the wire doesn't change the context.
    assert _ctx(people=list(reversed(PEOPLE))) == _ctx()


def test_vectors_onion_card_commitment_with_seeded_randomness():
    r = random.Random(SEED).randbytes
    ctx = _ctx()
    mixers = [dc.new_keypair(r) for _ in range(3)]
    box = dc.new_keypair(r)
    assert mixers[0][1].hex() == (
        "e4bcd88e9f7a829aaaea42dabad3a0d4935968a7001846e4a2b4cf16eff95816"
    )
    assert box[1].hex() == (
        "a9f298f8e4315affff9efe9a119970f5eec7c5dcdbbc5e34dd73e6df3d5a3407"
    )
    onion = dc.build_onion(box[1], [m[1] for m in mixers], ctx=ctx, rand=r)
    assert len(onion) == dc.onion_size(3) == 176
    assert hashlib.sha256(onion).hexdigest() == (
        "e5a3ed2939e0c8a544dd2b541eb3739a89ffe5b0814d769dbf28ab476d5ebce3"
    )
    card = dc.encode_card(
        dc.Card("aaa", "anna", "Anna", "Home a", "A bike", 0, b"\x01" * 64)
    )
    sealed = dc.seal_card(box[1], card, ctx=ctx, rand=r)
    assert hashlib.sha256(sealed).hexdigest() == (
        "cf063c2f18efaa091bbb3858d02fa572b3f5ac9f5d9d828dd3e39edf10c44cfc"
    )
    assert dc.commitment(ctx, "aaa", "anna", box[1], b"\x02" * 32).hex() == (
        "d21f13758f72f9c89fa8be309c73602dc5e246494312f0a46fe0499c2f4c8646"
    )
    assert dc.shuffle([bytes([i]) for i in range(6)], random.Random(7).randbytes) == [
        b"\x01",
        b"\x03",
        b"\x05",
        b"\x02",
        b"\x04",
        b"\x00",
    ]


def _lp(*parts: bytes | str | int) -> bytes:
    out = b""
    for p in parts:
        raw = p if isinstance(p, bytes) else str(p).encode()
        out += struct.pack(">I", len(raw)) + raw
    return out


def test_the_seal_is_x25519_hkdf_sha256_aes256gcm_from_the_primitives():
    """Re-derive one seal by hand: X25519(eph, to) → HKDF-SHA256 (no salt,
    info = domain ‖ ctx ‖ label ‖ eph_pub ‖ to_pub, each length-prefixed)
    → AES-256-GCM with a zero nonce, ciphertext = eph_pub ‖ ct."""
    to_priv, to_pub = dc.new_keypair(random.Random(1).randbytes)
    ctx = _ctx()
    blob = dc.seal(
        to_pub, b"hello", ctx=ctx, label="card", rand=random.Random(2).randbytes
    )
    eph_pub, ct = blob[:32], blob[32:]
    shared = X25519PrivateKey.from_private_bytes(to_priv).exchange(
        X25519PublicKey.from_public_bytes(eph_pub)
    )
    info = b"socialhome/app_draw/seal/v1" + _lp(ctx, "card", eph_pub, to_pub)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(
        shared
    )
    assert AESGCM(key).decrypt(b"\x00" * 12, ct, None) == b"hello"
    assert len(blob) == 32 + 5 + 16


# ─── Properties ───────────────────────────────────────────────────────────


def _run_round(n: int, h: int, rand) -> tuple[list[bytes], list[tuple[bytes, bytes]]]:
    """A whole honest mix: n box keys in h households → S."""
    ctx = _ctx()
    mixers = [dc.new_keypair(rand) for _ in range(h)]
    boxes = [dc.new_keypair(rand) for _ in range(n)]
    entries = [
        dc.build_onion(b[1], [m[1] for m in mixers], ctx=ctx, rand=rand) for b in boxes
    ]
    assert len({len(e) for e in entries}) == 1
    for k, (priv, _pub) in enumerate(mixers):
        entries = dc.mix(priv, entries, ctx=ctx, k=k, h=h, rand=rand)
    return entries, boxes


@pytest.mark.parametrize("seed", range(25))
def test_a_mix_gives_one_circle_and_each_box_opens_exactly_its_card(seed):
    rand = random.Random(seed).randbytes
    rng = random.Random(seed)
    n = rng.randint(3, 30)
    h = rng.randint(1, n)
    S, boxes = _run_round(n, h, rand)
    assert sorted(S) == sorted(b[1] for b in boxes)  # nothing lost, nothing added
    ctx = _ctx()
    # Each person j seals their card to S[j-1]; the giver at j-1 opens it.
    cards, who = [], {}
    for j, pub in enumerate(S):
        _gives, sealed_to = dc.circle_targets(j, n)
        card = dc.encode_card(dc.Card("i", f"p{j}", f"P{j}", "H", "", 0, b"\x00" * 64))
        cards.append(dc.seal_card(S[sealed_to], card, ctx=ctx, rand=rand))
        who[pub] = j
    assert len({len(c) for c in cards}) == 1  # every card one size
    gives_to = {}
    for priv, pub in boxes:
        opened = dc.try_cards(priv, cards, ctx=ctx)
        assert len(opened) == 1
        gives_to[who[pub]] = int(dc.decode_card(opened[0]).user[1:])
    # A derangement that is one cycle through everyone.
    assert all(g != r for g, r in gives_to.items())
    assert sorted(gives_to.values()) == list(range(n))
    seen, cur = set(), 0
    while cur not in seen:
        seen.add(cur)
        cur = gives_to[cur]
    assert len(seen) == n


def test_the_shuffle_is_uniform_enough():
    """Every position is reached by every item (a smoke test of the
    rejection-sampled Fisher–Yates, not a statistics suite)."""
    rand = random.Random(3).randbytes
    hits = {(i, j): 0 for i in range(4) for j in range(4)}
    for _ in range(2000):
        out = dc.shuffle([bytes([i]) for i in range(4)], rand)
        for j, item in enumerate(out):
            hits[(item[0], j)] += 1
    assert min(hits.values()) > 380  # ~500 each


def test_circle_targets():
    assert dc.circle_targets(0, 3) == (1, 2)
    assert dc.circle_targets(2, 3) == (0, 1)
    with pytest.raises(dc.DrawCryptoError):
        dc.circle_targets(3, 3)


# ─── Tampering ────────────────────────────────────────────────────────────


@pytest.fixture
def onion():
    rand = random.Random(9).randbytes
    mixers = [dc.new_keypair(rand) for _ in range(2)]
    box = dc.new_keypair(rand)
    return (
        mixers,
        box,
        dc.build_onion(box[1], [m[1] for m in mixers], ctx=_ctx(), rand=rand),
    )


def test_a_layer_opens_only_for_its_mixer_round_and_position(onion):
    mixers, box, o = onion
    inner = dc.peel(mixers[0][0], o, ctx=_ctx(), k=0, h=2)
    assert dc.peel(mixers[1][0], inner, ctx=_ctx(), k=1, h=2) == box[1]
    with pytest.raises(dc.DrawCryptoError):
        dc.peel(mixers[1][0], o, ctx=_ctx(), k=0, h=2)  # wrong mixer
    with pytest.raises(dc.DrawCryptoError):
        dc.peel(mixers[0][0], o, ctx=_ctx(round_no=2), k=0, h=2)  # replayed round
    with pytest.raises(dc.DrawCryptoError):
        dc.peel(mixers[0][0], o, ctx=_ctx(), k=1, h=3)  # wrong layer (size)
    flipped = bytearray(o)
    flipped[40] ^= 1
    with pytest.raises(dc.DrawCryptoError):
        dc.peel(mixers[0][0], bytes(flipped), ctx=_ctx(), k=0, h=2)
    with pytest.raises(dc.DrawCryptoError):
        dc.peel(mixers[0][0], o + b"\x00", ctx=_ctx(), k=0, h=2)  # oversize


def test_a_mix_refuses_duplicates_and_garbage(onion):
    mixers, _box, o = onion
    with pytest.raises(dc.DrawCryptoError):
        dc.mix(mixers[0][0], [o, o], ctx=_ctx(), k=0, h=2)
    with pytest.raises(dc.DrawCryptoError):
        dc.mix(mixers[0][0], [o, b"\x00" * len(o)], ctx=_ctx(), k=0, h=2)


def test_a_low_order_point_is_refused():
    with pytest.raises(dc.DrawCryptoError):
        dc.seal(b"\x00" * 32, b"x", ctx=_ctx(), label="card")
    priv, _ = dc.new_keypair()
    with pytest.raises(dc.DrawCryptoError):
        dc.open_sealed(priv, b"\x00" * 32 + b"\x00" * 16, ctx=_ctx(), label="card")


def test_a_card_seal_is_bound_to_round_and_label():
    priv, pub = dc.new_keypair()
    card = dc.encode_card(dc.Card("i", "u", "N", "H", "w", 0, b"\x00" * 64))
    sealed = dc.seal_card(pub, card, ctx=_ctx())
    assert dc.open_card(priv, sealed, ctx=_ctx()) == card
    assert dc.open_card(priv, sealed, ctx=_ctx(round_no=2)) is None
    with pytest.raises(dc.DrawCryptoError):
        dc.open_sealed(priv, sealed, ctx=_ctx(), label="handover")
    with pytest.raises(dc.DrawCryptoError):
        dc.open_card(priv, sealed[:-1], ctx=_ctx())


def test_cards_are_exactly_one_kib_and_strict():
    card = dc.Card("inst", "ben", "Ben", "Familie Okafor", "Dinosaurs", 2, b"\x07" * 64)
    raw = dc.encode_card(card)
    assert len(raw) == dc.CARD_BYTES
    assert dc.decode_card(raw) == card
    with pytest.raises(dc.DrawCryptoError):
        dc.decode_card(raw[:-1] + b"\x01")  # padding must be zero
    with pytest.raises(dc.DrawCryptoError):
        dc.decode_card(raw + b"\x00")
    with pytest.raises(dc.DrawCryptoError):
        dc.decode_card(b"\x03\xff" + raw[2:])  # length past the card
    too_long = dc.Card("i", "u", "N", "H", "x" * 1000, 0, b"\x00" * 64)
    assert not dc.card_fits(too_long)
    with pytest.raises(dc.DrawCryptoError):
        dc.encode_card(too_long)
    with pytest.raises(dc.DrawCryptoError):
        dc.encode_card(dc.Card("i", "u", "N", "H", "", 0, b"short"))


def test_cut_utf8_keeps_whole_characters():
    assert dc.cut_utf8("Müller", 2) == "M"
    assert dc.cut_utf8("ab", 10) == "ab"


def test_handover_round_trip_and_strictness():
    priv, pub = dc.new_keypair()
    card = dc.encode_card(dc.Card("i", "u", "N", "H", "w", 0, b"\x00" * 64))
    plain = dc.encode_handover("inst", "cleo", b"\x05" * 64, card)
    assert len(plain) == dc.HANDOVER_BYTES
    assert dc.decode_handover(plain) == ("inst", "cleo", b"\x05" * 64, card)
    sealed = dc.seal_handover(pub, plain, ctx=_ctx())
    assert len(sealed) == dc.SEALED_HANDOVER_BYTES
    assert dc.open_handover(priv, sealed, ctx=_ctx()) == plain
    assert dc.open_handover(dc.new_keypair()[0], sealed, ctx=_ctx()) is None
    with pytest.raises(dc.DrawCryptoError):
        dc.decode_handover(plain[:-1] + b"\x09")


def test_unknown_suites_are_refused():
    for suite in (None, "", "x25519", "x25519+mlkem768"):
        with pytest.raises(dc.UnsupportedDrawSuite):
            dc.check_suite(suite)
    with pytest.raises(dc.UnsupportedDrawSuite):
        _ctx(suite="aes128")
    assert dc.check_suite(dc.DRAW_SUITE_X25519_AESGCM) in dc.SUPPORTED_DRAW_SUITES


def test_list_digest_is_order_and_count_sensitive():
    a, b = dc.entry_hash(b"a"), dc.entry_hash(b"b")
    assert dc.list_digest([a, b]) != dc.list_digest([b, a])
    assert dc.list_digest([a]) != dc.list_digest([a, a])


def test_signed_messages_are_domain_separated():
    ctx = _ctx()
    pub = b"\x01" * 32
    msgs = {
        dc.key_message(ctx, "i", pub),
        dc.mix_message(ctx, 0, "i", pub, pub),
        dc.batch_message(ctx, "i", [pub], [("u", pub)]),
        dc.complaint_message(ctx, "i", "missing", "u", pub, pub),
        dc.card_message(ctx, dc.Card("i", "u", "N", "H", "w", 0)),
        dc.handover_message(ctx, "i", "u", b"\x00" * dc.CARD_BYTES),
    }
    assert len(msgs) == 6


def test_public_of_and_sizes():
    priv, pub = dc.new_keypair()
    assert dc.public_of(priv) == pub
    with pytest.raises(dc.DrawCryptoError):
        dc.public_of(b"\x00")
    with pytest.raises(dc.DrawCryptoError):
        dc.build_onion(b"\x00", [pub], ctx=_ctx())
    with pytest.raises(dc.DrawCryptoError):
        dc.commitment(_ctx(), "i", "u", pub, b"\x00")
