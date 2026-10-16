"""Tests for the sealed connect claim (:mod:`socialhome.dashboard.claim_seal`).

``docs/display-api/v1/fixtures/sealed-claim-vectors.json`` is built here
from fixed keys and nonces (:func:`build_vectors`) — the firmware
(social-home-io/esphome-dashboard) pins that file through its display-API
contract and opens every vector in its host tests, so the server's seal
and the panel's open can't drift apart. Rewrite it with
``SH_UPDATE_DISPLAY_FIXTURES=1``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from jsonschema import Draft202012Validator

import socialhome.dashboard.claim_seal as claim_seal_module
from socialhome.crypto import b64url_decode, b64url_encode, x25519_exchange
from socialhome.dashboard.claim_seal import (
    PANEL_CLAIM_HKDF_INFO,
    PANEL_CLAIM_SUITE_X25519_AESGCM,
    SUPPORTED_PANEL_CLAIM_SUITES,
    InvalidPanelKey,
    OpenedClaim,
    SealedClaimInvalid,
    UnsupportedPanelClaimSuite,
    _seal_claim_with,
    claim_aad,
    claim_key,
    open_sealed_claim,
    parse_panel_key,
    seal_claim,
)

VECTORS_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "display-api"
    / "v1"
    / "fixtures"
    / "sealed-claim-vectors.json"
)
REGEN = os.environ.get("SH_UPDATE_DISPLAY_FIXTURES") == "1"


def _seed(label: str) -> bytes:
    """A fixed 32-byte value, reproducible from its label."""
    return hashlib.sha256(f"socialhome sealed-claim vector: {label}".encode()).digest()


def _keypair(label: str) -> tuple[bytes, bytes]:
    sk = X25519PrivateKey.from_private_bytes(_seed(label))
    raw_priv = sk.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    raw_pub = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return raw_priv, raw_pub


def _sealed(text: str) -> dict:
    return json.loads(text)


def _reseal(sealed: str, **changes: object) -> str:
    body = {**_sealed(sealed), **changes}
    body = {k: v for k, v in body.items() if v is not None}
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def _flip(b64: str, index: int) -> str:
    raw = bytearray(b64url_decode(b64))
    raw[index] ^= 0x01
    return b64url_encode(bytes(raw))


_CASES = (
    ("lan-ip", "http://192.168.1.20:8100"),
    ("host-name", "https://dashboard.home.example:8443/"),
)


def build_vectors() -> dict:
    """Every vector, from fixed keys and nonces."""
    panel_priv, panel_pub = _keypair("panel")
    valid = []
    for i, (name, url) in enumerate(_CASES):
        eph_priv, eph_pub = _keypair(f"ephemeral {name}")
        nonce = _seed(f"nonce {name}")[:12]
        claim = b64url_encode(_seed(f"claim {name}"))
        shared = x25519_exchange(eph_priv, panel_pub)
        sealed = _seal_claim_with(
            claim=claim, url=url, panel_pub=panel_pub, eph_private=eph_priv, nonce=nonce
        )
        valid.append(
            {
                "name": name,
                "claim": claim,
                "url": url,
                "eph_private": b64url_encode(eph_priv),
                "eph_public": b64url_encode(eph_pub),
                "nonce": b64url_encode(nonce),
                "shared_secret_hex": shared.hex(),
                "aead_key_hex": claim_key(shared, eph_pub, panel_pub).hex(),
                "aad_hex": claim_aad(PANEL_CLAIM_SUITE_X25519_AESGCM, panel_pub).hex(),
                "plaintext": json.dumps(
                    {"claim": claim, "url": url},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "sealed": sealed,
            }
        )
    base = valid[0]["sealed"]
    body = _sealed(base)
    _, other_pub = _keypair("another panel")
    eph_priv, eph_pub = _keypair("ephemeral lan-ip")
    to_other = _seal_claim_with(
        claim=valid[0]["claim"],
        url=valid[0]["url"],
        panel_pub=other_pub,
        eph_private=eph_priv,
        nonce=b64url_decode(valid[0]["nonce"]),
    )

    def sealed_plaintext(plain: bytes) -> str:
        """``plain`` correctly sealed to the panel (as case 0, but a
        nonce of its own) — for the checks after the AEAD opens."""
        nonce = _seed(f"nonce plaintext {plain!r}")[:12]
        key = claim_key(x25519_exchange(eph_priv, panel_pub), eph_pub, panel_pub)
        ct = AESGCM(key).encrypt(
            nonce, plain, claim_aad(PANEL_CLAIM_SUITE_X25519_AESGCM, panel_pub)
        )
        return _reseal(base, ct=b64url_encode(ct), nonce=b64url_encode(nonce))

    # The order-8 point of Curve25519 (RFC 7748 §6.1 lists the small-order
    # u-coordinates): every clamped scalar maps it to the all-zero secret.
    order_8 = bytes.fromhex(
        "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800"
    )
    # Same bytes, but the last base64url character carries stray low bits.
    stray = {"k": "l", "A": "B", "Q": "R", "g": "h", "w": "x"}
    invalid = [
        ("unknown suite", _reseal(base, suite="x25519-hkdf-sha256-chacha20"), True),
        ("missing suite", _reseal(base, suite=None), True),
        (
            "suite in upper case",
            _reseal(base, suite="X25519-HKDF-SHA256-AES256GCM"),
            True,
        ),
        ("ciphertext bit flipped", _reseal(base, ct=_flip(body["ct"], 0)), False),
        ("tag bit flipped", _reseal(base, ct=_flip(body["ct"], -1)), False),
        ("nonce bit flipped", _reseal(base, nonce=_flip(body["nonce"], 0)), False),
        ("epk bit flipped", _reseal(base, epk=_flip(body["epk"], 0)), False),
        ("sealed to another panel", to_other, False),
        (
            "epk all zeros (small order)",
            _reseal(base, epk=b64url_encode(bytes(32))),
            False,
        ),
        ("epk too short", _reseal(base, epk=body["epk"][:42]), False),
        ("nonce too short", _reseal(base, nonce=body["nonce"][:15]), False),
        ("ct too short for a tag", _reseal(base, ct=body["ct"][:20]), False),
        (
            "ct length 1 mod 4",
            _reseal(base, ct=body["ct"] + "A" * ((1 - len(body["ct"])) % 4)),
            False,
        ),
        ("ct not base64url", _reseal(base, ct=body["ct"][:-1] + "+"), False),
        (
            "epk with stray low bits (non-canonical)",
            _reseal(base, epk=body["epk"][:-1] + stray[body["epk"][-1]]),
            False,
        ),
        (
            "epk order-8 point (small order)",
            _reseal(base, epk=b64url_encode(order_8)),
            False,
        ),
        (
            "suite not a string",
            _reseal(base, suite=["x25519-hkdf-sha256-aes256gcm"]),
            True,
        ),
        ("plaintext not an object", sealed_plaintext(b"[]"), False),
        (
            "plaintext claim not a string",
            sealed_plaintext(b'{"claim":1,"url":"http://h:8100"}'),
            False,
        ),
        (
            "plaintext without url",
            sealed_plaintext(b'{"claim":"' + b"A" * 43 + b'"}'),
            False,
        ),
        ("plaintext not json", sealed_plaintext(b"claim=x"), False),
        ("missing ct", _reseal(base, ct=None), False),
        ("not json", "suite=x25519-hkdf-sha256-aes256gcm", False),
        ("json array", "[]", False),
        ("empty", "", False),
    ]
    return {
        "description": (
            "Shared test vectors for the sealed Connect-from-Home-Assistant"
            " claim (docs/crypto.md, 'Sealed wall-panel connect claim')."
            " Generated by tests/dashboard/test_claim_seal.py from fixed keys"
            " and nonces. Bytes are unpadded base64url unless the key ends"
            " in _hex. Every 'invalid' vector is opened with the panel key"
            " below and must be rejected; 'unsupported_suite' ones before"
            " any key work. panel_private is a published TEST key: never"
            " ship it, or any fixed key, in firmware."
        ),
        "suite": PANEL_CLAIM_SUITE_X25519_AESGCM,
        "hkdf_info": PANEL_CLAIM_HKDF_INFO.decode("ascii"),
        "panel_private": b64url_encode(panel_priv),
        "panel_public": b64url_encode(panel_pub),
        "valid": valid,
        "invalid": [
            {"name": name, "sealed": sealed, "unsupported_suite": unsupported}
            for name, sealed, unsupported in invalid
        ],
        "panel_keys": {
            "valid": [b64url_encode(panel_pub), b64url_encode(other_pub)],
            "invalid": [
                "",
                b64url_encode(panel_pub)[:42],
                b64url_encode(panel_pub) + "A",
                b64url_encode(panel_pub) + "=",
                b64url_encode(panel_pub)[:42] + "B",
                "+" + b64url_encode(panel_pub)[1:],
                "unavailable",
            ],
        },
    }


VECTORS = build_vectors()
PANEL_PRIVATE = b64url_decode(VECTORS["panel_private"])
PANEL_PUBLIC = b64url_decode(VECTORS["panel_public"])


def test_vectors_file_is_current():
    text = json.dumps(VECTORS, indent=2, sort_keys=True) + "\n"
    if REGEN:
        VECTORS_PATH.write_text(text, encoding="utf-8")
    assert VECTORS_PATH.read_text(encoding="utf-8") == text, (
        f"{VECTORS_PATH.name} is stale — rerun with SH_UPDATE_DISPLAY_FIXTURES=1"
    )


def test_suite_contract():
    assert PANEL_CLAIM_SUITE_X25519_AESGCM == "x25519-hkdf-sha256-aes256gcm"
    assert SUPPORTED_PANEL_CLAIM_SUITES == {PANEL_CLAIM_SUITE_X25519_AESGCM}
    assert issubclass(UnsupportedPanelClaimSuite, ValueError)
    assert PANEL_CLAIM_HKDF_INFO == b"socialhome-panel-claim-v1"


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["name"])
def test_valid_vectors_open(case):
    opened = open_sealed_claim(
        case["sealed"], panel_private=PANEL_PRIVATE, panel_pub=PANEL_PUBLIC
    )
    assert opened == OpenedClaim(claim=case["claim"], url=case["url"])


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["name"])
def test_valid_vector_wire_shape(case):
    body = json.loads(case["sealed"])
    assert set(body) == {"ct", "epk", "nonce", "suite"}
    assert body["suite"] == PANEL_CLAIM_SUITE_X25519_AESGCM
    assert body["epk"] == case["eph_public"]
    assert body["nonce"] == case["nonce"]
    # ct is the plaintext's length plus the 16-byte GCM tag.
    assert len(b64url_decode(body["ct"])) == len(case["plaintext"]) + 16
    # Nothing in the clear: neither the claim nor the address.
    assert case["claim"] not in case["sealed"]
    assert case["url"] not in case["sealed"]


SCHEMA = json.loads(
    (VECTORS_PATH.parents[1] / "sealed-claim.schema.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["name"])
def test_valid_vectors_match_the_schema(case):
    Draft202012Validator.check_schema(SCHEMA)
    Draft202012Validator(SCHEMA).validate(json.loads(case["sealed"]))


def test_a_sealed_claim_at_the_size_limit_matches_the_schema():
    """The schema's ``ct`` bounds are the opener's (17..2048 bytes)."""
    body = json.loads(VECTORS["valid"][0]["sealed"])
    validator = Draft202012Validator(SCHEMA)
    for size, ok in ((17, True), (2048, True), (16, False), (2049, False)):
        body["ct"] = b64url_encode(bytes(size))
        assert validator.is_valid(body) is ok, size


@pytest.mark.parametrize("case", VECTORS["invalid"], ids=lambda c: c["name"])
def test_invalid_vectors_are_rejected(case):
    expected = (
        UnsupportedPanelClaimSuite if case["unsupported_suite"] else SealedClaimInvalid
    )
    with pytest.raises(expected):
        open_sealed_claim(
            case["sealed"], panel_private=PANEL_PRIVATE, panel_pub=PANEL_PUBLIC
        )


def test_ct_length_one_mod_four_is_the_uniform_refusal():
    (case,) = [c for c in VECTORS["invalid"] if c["name"] == "ct length 1 mod 4"]
    assert len(json.loads(case["sealed"])["ct"]) % 4 == 1


def test_aad_binds_the_panel_key():
    """Opened with the right private key but told another public key, the
    AAD (and HKDF salt) differ — refused."""
    _, other_pub = _keypair("another panel")
    with pytest.raises(SealedClaimInvalid):
        open_sealed_claim(
            VECTORS["valid"][0]["sealed"],
            panel_private=PANEL_PRIVATE,
            panel_pub=other_pub,
        )


def test_seal_claim_round_trip_and_fresh_randomness():
    first = seal_claim(
        claim="c" * 43, url="http://10.0.0.2:8100", panel_pub=PANEL_PUBLIC
    )
    second = seal_claim(
        claim="c" * 43, url="http://10.0.0.2:8100", panel_pub=PANEL_PUBLIC
    )
    a, b = json.loads(first), json.loads(second)
    assert a["epk"] != b["epk"] and a["nonce"] != b["nonce"] and a["ct"] != b["ct"]
    for sealed in (first, second):
        opened = open_sealed_claim(
            sealed, panel_private=PANEL_PRIVATE, panel_pub=PANEL_PUBLIC
        )
        assert opened.claim == "c" * 43 and opened.url == "http://10.0.0.2:8100"


def test_seal_refuses_small_order_and_short_keys():
    with pytest.raises(InvalidPanelKey):
        seal_claim(claim="x", url="http://a", panel_pub=bytes(32))
    with pytest.raises(InvalidPanelKey):
        seal_claim(claim="x", url="http://a", panel_pub=PANEL_PUBLIC[:31])


def test_seal_refuses_a_bad_nonce_length():
    eph_priv, _ = _keypair("ephemeral lan-ip")
    with pytest.raises(ValueError) as err:
        _seal_claim_with(
            claim="x",
            url="http://a",
            panel_pub=PANEL_PUBLIC,
            eph_private=eph_priv,
            nonce=bytes(11),
        )
    assert not isinstance(err.value, InvalidPanelKey)


def test_seal_claim_with_is_module_private():
    assert "_seal_claim_with" not in claim_seal_module.__all__
    assert not hasattr(claim_seal_module, "seal_claim_with")


def test_opened_claim_repr_hides_the_claim():
    assert "secret-claim" not in repr(OpenedClaim(claim="secret-claim", url="u"))


@pytest.mark.parametrize(
    "plain", [b"[]", b'{"claim":1,"url":"u"}', b'{"url":"u"}', b"\xff"]
)
def test_open_refuses_a_bad_plaintext(plain):
    """A correctly sealed body that isn't ``{claim: str, url: str}``."""
    eph_priv, eph_pub = _keypair("ephemeral lan-ip")
    nonce = bytes(12)
    shared = x25519_exchange(eph_priv, PANEL_PUBLIC)
    key = claim_key(shared, eph_pub, PANEL_PUBLIC)
    ct = AESGCM(key).encrypt(
        nonce, plain, claim_aad(PANEL_CLAIM_SUITE_X25519_AESGCM, PANEL_PUBLIC)
    )
    sealed = json.dumps(
        {
            "ct": b64url_encode(ct),
            "epk": b64url_encode(eph_pub),
            "nonce": b64url_encode(nonce),
            "suite": PANEL_CLAIM_SUITE_X25519_AESGCM,
        }
    )
    with pytest.raises(SealedClaimInvalid):
        open_sealed_claim(sealed, panel_private=PANEL_PRIVATE, panel_pub=PANEL_PUBLIC)


def test_open_refuses_an_oversized_ciphertext():
    body = json.loads(VECTORS["valid"][0]["sealed"])
    body["ct"] = b64url_encode(bytes(4096))
    with pytest.raises(SealedClaimInvalid):
        open_sealed_claim(
            json.dumps(body), panel_private=PANEL_PRIVATE, panel_pub=PANEL_PUBLIC
        )


@pytest.mark.parametrize("value", VECTORS["panel_keys"]["valid"])
def test_parse_panel_key_valid(value):
    assert parse_panel_key(value) == b64url_decode(value)


@pytest.mark.parametrize("value", [*VECTORS["panel_keys"]["invalid"], None, 42])
def test_parse_panel_key_invalid(value):
    assert parse_panel_key(value) is None
