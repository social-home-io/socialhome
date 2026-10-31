"""Seat keys and sealed seat messages at a group table (v_76)."""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    ed25519_public_key,
    generate_identity_keypair,
)
from socialhome.federation.seat_crypto import (
    SEAT_KEM_SUITE_X25519,
    SEAT_KEY_SIG_SUITE_ED25519,
    SUPPORTED_SEAT_KEM_SUITES,
    SUPPORTED_SEAT_KEY_SIG_SUITES,
    SeatSealError,
    UnsupportedSeatKemSuite,
    UnsupportedSeatKeySigSuite,
    new_msg_id,
    new_seat_keypair,
    open_sealed,
    parse_seat_key,
    seal,
    sign_seat_key,
    verify_seat_key,
)

SID = "table1"


def _identity() -> tuple[bytes, bytes]:
    kp = generate_identity_keypair()
    return kp.private_key, kp.public_key


def test_suites_are_tagged_and_known():
    assert SEAT_KEM_SUITE_X25519 in SUPPORTED_SEAT_KEM_SUITES
    assert SEAT_KEY_SIG_SUITE_ED25519 in SUPPORTED_SEAT_KEY_SIG_SUITES


def test_keypair_is_x25519_sized():
    priv, pub = new_seat_keypair()
    assert len(priv) == 32 and len(pub) == 32
    assert new_seat_keypair()[1] != pub


def test_signed_seat_key_verifies_for_its_household_and_table_only():
    seed, pk = _identity()
    _priv, pub = new_seat_keypair()
    wire = sign_seat_key(seed=seed, session_id=SID, instance="hb", pub=pub)
    assert wire["seat_kem_suite"] == "x25519"
    assert wire["sig_suite"] == "ed25519"
    assert b64url_decode(wire["pub"]) == pub
    assert verify_seat_key(wire, session_id=SID, instance="hb", identity_pk=pk)
    # Another table, another household, another identity: no.
    assert not verify_seat_key(wire, session_id="t2", instance="hb", identity_pk=pk)
    assert not verify_seat_key(wire, session_id=SID, instance="hc", identity_pk=pk)
    _seed2, pk2 = _identity()
    assert not verify_seat_key(wire, session_id=SID, instance="hb", identity_pk=pk2)


def test_swapped_pub_fails_the_signature():
    seed, pk = _identity()
    _priv, pub = new_seat_keypair()
    wire = sign_seat_key(seed=seed, session_id=SID, instance="hb", pub=pub)
    wire = {**wire, "pub": b64url_encode(new_seat_keypair()[1])}
    assert not verify_seat_key(wire, session_id=SID, instance="hb", identity_pk=pk)


def test_unknown_suites_are_refused_never_defaulted():
    seed, pk = _identity()
    wire = sign_seat_key(
        seed=seed, session_id=SID, instance="hb", pub=new_seat_keypair()[1]
    )
    with pytest.raises(UnsupportedSeatKemSuite):
        parse_seat_key({**wire, "seat_kem_suite": "x448"})
    with pytest.raises(UnsupportedSeatKemSuite):
        parse_seat_key({k: v for k, v in wire.items() if k != "seat_kem_suite"})
    with pytest.raises(UnsupportedSeatKeySigSuite):
        parse_seat_key({**wire, "sig_suite": "rsa"})
    with pytest.raises(UnsupportedSeatKeySigSuite):
        verify_seat_key(
            {**wire, "sig_suite": "rsa"}, session_id=SID, instance="hb", identity_pk=pk
        )


@pytest.mark.parametrize(
    "change",
    [
        {"pub": "AAAA"},
        {"pub": 7},
        {"sig": b64url_encode(b"x" * 10)},
        {"sig": None},
    ],
)
def test_malformed_seat_key_is_refused(change):
    seed, _pk = _identity()
    wire = sign_seat_key(
        seed=seed, session_id=SID, instance="hb", pub=new_seat_keypair()[1]
    )
    with pytest.raises(SeatSealError):
        parse_seat_key({**wire, **change})


def test_parse_seat_key_refuses_non_dict():
    with pytest.raises(SeatSealError):
        parse_seat_key("nope")


def _pair():
    a_priv, a_pub = new_seat_keypair()
    b_priv, b_pub = new_seat_keypair()
    return a_priv, a_pub, b_priv, b_pub


def test_seal_round_trip_between_two_households():
    a_priv, a_pub, b_priv, b_pub = _pair()
    mid = new_msg_id()
    entry = seal(
        b'{"hand":[1,2]}',
        my_priv=a_priv,
        peer_pub=b_pub,
        session_id=SID,
        from_seat=2,
        to_seat=4,
        msg_id=mid,
    )
    assert entry["to_seat"] == 4 and entry["seat_kem_suite"] == "x25519"
    assert b"hand" not in b64url_decode(entry["ct"])
    got = open_sealed(
        entry, my_priv=b_priv, peer_pub=a_pub, session_id=SID, from_seat=2, msg_id=mid
    )
    assert got == b'{"hand":[1,2]}'


def test_a_third_household_cannot_open_it():
    a_priv, a_pub, _b_priv, b_pub = _pair()
    hub_priv, _hub_pub = new_seat_keypair()
    mid = new_msg_id()
    entry = seal(
        b"secret",
        my_priv=a_priv,
        peer_pub=b_pub,
        session_id=SID,
        from_seat=2,
        to_seat=4,
        msg_id=mid,
    )
    with pytest.raises(SeatSealError):
        open_sealed(
            entry,
            my_priv=hub_priv,
            peer_pub=a_pub,
            session_id=SID,
            from_seat=2,
            msg_id=mid,
        )


@pytest.mark.parametrize(
    "where",
    ["session", "from_seat", "to_seat", "msg_id", "sender_key", "ct"],
)
def test_moving_or_relabelling_a_sealed_message_fails(where):
    a_priv, a_pub, b_priv, b_pub = _pair()
    mid = new_msg_id()
    entry = seal(
        b"secret",
        my_priv=a_priv,
        peer_pub=b_pub,
        session_id=SID,
        from_seat=2,
        to_seat=4,
        msg_id=mid,
    )
    kw = {
        "my_priv": b_priv,
        "peer_pub": a_pub,
        "session_id": SID,
        "from_seat": 2,
        "msg_id": mid,
    }
    if where == "session":
        kw["session_id"] = "other"
    elif where == "from_seat":
        kw["from_seat"] = 3
    elif where == "to_seat":
        entry = {**entry, "to_seat": 5}
    elif where == "msg_id":
        kw["msg_id"] = new_msg_id()
    elif where == "sender_key":
        # The hub claims it came from another household's seat.
        kw["peer_pub"] = new_seat_keypair()[1]
    else:
        ct = bytearray(b64url_decode(entry["ct"]))
        ct[0] ^= 1
        entry = {**entry, "ct": b64url_encode(bytes(ct))}
    with pytest.raises(SeatSealError):
        open_sealed(entry, **kw)


def test_reply_direction_uses_another_key():
    a_priv, _a_pub, _b_priv, b_pub = _pair()
    mid = new_msg_id()
    entry = seal(
        b"x",
        my_priv=a_priv,
        peer_pub=b_pub,
        session_id=SID,
        from_seat=2,
        to_seat=4,
        msg_id=mid,
    )
    # Reflected back to the sender as if seat 4 had sent it to seat 2.
    reflected = {**entry, "to_seat": 2}
    with pytest.raises(SeatSealError):
        open_sealed(
            reflected,
            my_priv=a_priv,
            peer_pub=b_pub,
            session_id=SID,
            from_seat=4,
            msg_id=mid,
        )


def test_unknown_seal_suite_is_refused():
    a_priv, a_pub, b_priv, b_pub = _pair()
    mid = new_msg_id()
    entry = seal(
        b"x",
        my_priv=a_priv,
        peer_pub=b_pub,
        session_id=SID,
        from_seat=2,
        to_seat=4,
        msg_id=mid,
    )
    with pytest.raises(UnsupportedSeatKemSuite):
        open_sealed(
            {**entry, "seat_kem_suite": "x25519+mlkem768"},
            my_priv=b_priv,
            peer_pub=a_pub,
            session_id=SID,
            from_seat=2,
            msg_id=mid,
        )
    with pytest.raises(UnsupportedSeatKemSuite):
        open_sealed(
            {k: v for k, v in entry.items() if k != "seat_kem_suite"},
            my_priv=b_priv,
            peer_pub=a_pub,
            session_id=SID,
            from_seat=2,
            msg_id=mid,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"nonce": "AAAA"},
        {"ct": 5},
        {"nonce": None},
        {"to_seat": "4"},
        {"ct": "A" * 100_000},
    ],
)
def test_malformed_sealed_entry_is_refused(change):
    a_priv, a_pub, b_priv, b_pub = _pair()
    mid = new_msg_id()
    entry = seal(
        b"x",
        my_priv=a_priv,
        peer_pub=b_pub,
        session_id=SID,
        from_seat=2,
        to_seat=4,
        msg_id=mid,
    )
    with pytest.raises(SeatSealError):
        open_sealed(
            {**entry, **change},
            my_priv=b_priv,
            peer_pub=a_pub,
            session_id=SID,
            from_seat=2,
            msg_id=mid,
        )


def test_low_order_peer_key_is_refused():
    a_priv, _a_pub = new_seat_keypair()
    with pytest.raises(SeatSealError):
        seal(
            b"x",
            my_priv=a_priv,
            peer_pub=bytes(32),
            session_id=SID,
            from_seat=1,
            to_seat=2,
            msg_id=new_msg_id(),
        )


def test_msg_id_is_16_random_bytes():
    a, b = new_msg_id(), new_msg_id()
    assert a != b and len(b64url_decode(a)) == 16


def test_bad_msg_id_is_refused():
    a_priv, _a_pub, _b_priv, b_pub = _pair()
    with pytest.raises(SeatSealError):
        seal(
            b"x",
            my_priv=a_priv,
            peer_pub=b_pub,
            session_id=SID,
            from_seat=1,
            to_seat=2,
            msg_id="short",
        )


def test_identity_seed_matches_public_key_helper():
    seed, pk = _identity()
    assert ed25519_public_key(seed) == pk
