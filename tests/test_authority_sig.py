"""Tests for :mod:`socialhome.authority_sig` — the dependency-light space-
authority signature primitives.

The behavioural surface (sign/verify/strip/suite handling) is exercised in
detail by ``tests/services/test_space_crypto_service.py`` against the
re-exports; here we pin two structural invariants that the move introduced:

1. The primitives round-trip and the suite gate fails-closed.
2. Importing ONLY this module does NOT drag in the HFS crypto stack
   (``services.space_crypto_service`` / ``federation.keywrap_seal`` /
   ``infrastructure.key_manager``) — that is
   the whole point of the split (the content-blind GFS imports from here).
3. ``services.space_crypto_service`` re-exports the SAME objects (identity), so
   existing importers stay unchanged.
"""

from __future__ import annotations

import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
    AUTHORITY_RELAY_EVENT_TYPES,
    AUTHORITY_SIG_SUITE_ED25519,
    SUPPORTED_AUTHORITY_SIG_SUITES,
    UnsupportedAuthoritySuite,
    sign_authority_event,
    strip_authority_sig_fields,
    verify_authority_event,
)
from socialhome.crypto import generate_space_keypair


def test_sign_then_verify_round_trips():
    kp = generate_space_keypair()
    payload = {"ciphertext": "opaque", "n": 3}
    signed = sign_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        space_id="space-1",
        payload=payload,
        space_seed=kp.private_key,
    )
    assert signed["authority_sig_suite"] == AUTHORITY_SIG_SUITE_ED25519
    assert verify_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        space_id="space-1",
        payload=payload,
        authority_sig=signed["authority_sig"],
        authority_sig_suite=signed["authority_sig_suite"],
        space_public_key=kp.public_key,
    )


def test_verify_rejects_unknown_suite():
    kp = generate_space_keypair()
    with pytest.raises(UnsupportedAuthoritySuite):
        verify_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id="space-1",
            payload={},
            authority_sig="AAAA",
            authority_sig_suite="ed25519+future-pq",
            space_public_key=kp.public_key,
        )
    assert "ed25519+future-pq" not in SUPPORTED_AUTHORITY_SIG_SUITES


def test_subscribers_query_event_type_is_not_a_relay_type():
    """The subscriber-list query is a READ authenticated by a space-authority
    signature — but it is NOT a relay event type. Keeping it out of
    ``AUTHORITY_RELAY_EVENT_TYPES`` ensures a query-signed payload can never be
    replayed onto the GFS relay fan-out (and vice-versa)."""
    assert AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY == "space_subscribers_query"
    assert AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY not in AUTHORITY_RELAY_EVENT_TYPES


def test_strip_removes_only_the_two_sig_keys():
    out = strip_authority_sig_fields(
        {"a": 1, "authority_sig": "x", "authority_sig_suite": "ed25519"}
    )
    assert out == {"a": 1}


def test_importing_module_does_not_pull_hfs_crypto_stack():
    """Importing authority_sig in a fresh interpreter must NOT load the HFS
    crypto stack — this is what lets the content-blind GFS reuse it."""
    import subprocess
    import sys

    code = (
        "import sys; import socialhome.authority_sig; "
        "leaked = [m for m in ("
        "'socialhome.services.space_crypto_service',"
        "'socialhome.federation.keywrap_seal',"
        "'socialhome.infrastructure.key_manager'"
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


def test_space_crypto_service_reexports_same_objects():
    from socialhome.services import space_crypto_service as scs

    assert scs.sign_authority_event is sign_authority_event
    assert scs.verify_authority_event is verify_authority_event
    assert scs.strip_authority_sig_fields is strip_authority_sig_fields
    assert scs.UnsupportedAuthoritySuite is UnsupportedAuthoritySuite
    assert scs.AUTHORITY_EVENT_SPACE_POST_PUBLIC == AUTHORITY_EVENT_SPACE_POST_PUBLIC
