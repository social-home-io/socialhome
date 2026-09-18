"""Tests for :mod:`socialhome.capabilities_sig` — the signed
GFS capability block served on ``GET /gfs/info``.

The block is the ONLY thing a household may trust when deciding to relay
identity-free: a bare ``anonymous_publish: true`` flag is unauthenticated, so
an on-path attacker could strip it and force every relay back to the legacy
body — one that carries a household-signed, third-party-provable "household X
relayed into space Y" artefact. Signing the block with the GFS identity key
the household already pinned at pair time (TOFU) closes that downgrade.

Structural invariants pinned here, mirroring ``tests/test_authority_sig.py``:

1. Sign/verify round-trips and the suite gate fails closed.
2. Every field of the signed statement is bound (tamper → ``False``).
3. The module's own imports stay dependency-light, and importing it does NOT
   drag in the HFS crypto stack — that is what lets BOTH the content-blind
   GFS process and the household use the same code.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

from socialhome.capabilities_sig import (
    CAPS_SIG_SUITE_ED25519,
    CAPS_SIGNING_PREFIX,
    SUPPORTED_CAPS_SIG_SUITES,
    UnsupportedCapsSigSuite,
    capabilities_signing_bytes,
    sign_capabilities,
    verify_capabilities,
)
from socialhome.crypto import generate_identity_keypair

CAPS = {"anonymous_publish": True}


def _pk_hex(kp) -> str:
    return kp.public_key.hex()


def test_sign_then_verify_round_trips():
    kp = generate_identity_keypair()
    sig, suite = sign_capabilities(kp.private_key, "gfs-1", CAPS)
    assert suite == CAPS_SIG_SUITE_ED25519
    assert verify_capabilities(_pk_hex(kp), "gfs-1", CAPS, sig, suite)


def test_signing_bytes_are_canonical_and_domain_separated():
    """Key order must not change the bytes (so a re-serialised response still
    verifies), and the versioned prefix keeps the signature from being lifted
    onto any other Ed25519 statement this key signs."""
    a = capabilities_signing_bytes("gfs-1", {"anonymous_publish": True, "z": 1})
    b = capabilities_signing_bytes("gfs-1", {"z": 1, "anonymous_publish": True})
    assert a == b
    assert a.startswith(CAPS_SIGNING_PREFIX)
    assert CAPS_SIGNING_PREFIX == b"gfs-capabilities:v1:"
    assert a == (
        b"gfs-capabilities:v1:"
        b'{"capabilities":{"anonymous_publish":true,"z":1},'
        b'"gfs_instance_id":"gfs-1"}'
    )


def test_tampering_with_the_capabilities_breaks_verification():
    kp = generate_identity_keypair()
    sig, suite = sign_capabilities(kp.private_key, "gfs-1", CAPS)
    assert not verify_capabilities(
        _pk_hex(kp), "gfs-1", {"anonymous_publish": False}, sig, suite
    )


def test_tampering_with_the_instance_id_breaks_verification():
    """A block lifted from GFS A must not authenticate GFS B."""
    kp = generate_identity_keypair()
    sig, suite = sign_capabilities(kp.private_key, "gfs-1", CAPS)
    assert not verify_capabilities(_pk_hex(kp), "gfs-2", CAPS, sig, suite)


def test_another_key_does_not_verify():
    kp = generate_identity_keypair()
    other = generate_identity_keypair()
    sig, suite = sign_capabilities(kp.private_key, "gfs-1", CAPS)
    assert not verify_capabilities(_pk_hex(other), "gfs-1", CAPS, sig, suite)


def test_unknown_suite_is_rejected_not_defaulted():
    kp = generate_identity_keypair()
    sig, _suite = sign_capabilities(kp.private_key, "gfs-1", CAPS)
    with pytest.raises(UnsupportedCapsSigSuite):
        verify_capabilities(_pk_hex(kp), "gfs-1", CAPS, sig, "ed25519+mldsa65")
    assert "ed25519+mldsa65" not in SUPPORTED_CAPS_SIG_SUITES
    assert SUPPORTED_CAPS_SIG_SUITES == frozenset({CAPS_SIG_SUITE_ED25519})


@pytest.mark.parametrize(
    "pk_hex,sig",
    [
        ("not-hex", "AAAA"),
        ("aa" * 31, "AAAA"),
        ("aa" * 32, "!!!not-base64!!!"),
        ("", ""),
    ],
)
def test_malformed_inputs_return_false(pk_hex, sig):
    """Malformed material is a verification FAILURE, never an exception — the
    household calls this on attacker-controlled bytes inside a best-effort
    fetch that must not raise."""
    assert (
        verify_capabilities(pk_hex, "gfs-1", CAPS, sig, CAPS_SIG_SUITE_ED25519) is False
    )


def test_module_only_depends_on_the_crypto_primitives():
    """Dependency discipline (mirrors ``socialhome/authority_sig.py``): the
    module is imported by BOTH the GFS process and the household service, so
    its own imports stay at json + the Ed25519/base64 helpers."""
    src = pathlib.Path("socialhome/capabilities_sig.py").read_text()
    tree = ast.parse(src)
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(("." * node.level) + (node.module or ""))
    assert modules == {"__future__", "json", ".crypto"}


def test_importing_module_does_not_pull_either_stack():
    """Importing the capability primitives in a fresh interpreter must load
    neither the HFS crypto stack nor the GFS server package — that is what
    lets the content-blind GFS and the household share one module. The
    module lives at the top level (beside ``authority_sig``) precisely so the
    household's import doesn't drag ``socialhome.global_server`` in."""
    code = (
        "import sys; import socialhome.capabilities_sig; "
        "leaked = [m for m in ("
        "'socialhome.services.space_crypto_service',"
        "'socialhome.federation.keywrap_seal',"
        "'socialhome.infrastructure.key_manager',"
        "'socialhome.global_server'"
        ") if m in sys.modules]; "
        "print(leaked)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "[]", out.stdout
