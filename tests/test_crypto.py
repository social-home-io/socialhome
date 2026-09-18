"""Tests for socialhome.crypto."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import (
    MOVE_LINK_SUITE_ED25519,
    REPLAY_CACHE_WINDOW,
    SUPPORTED_MOVE_LINK_SUITES,
    SUPPORTED_USER_SIG_SUITES,
    USER_SIG_SUITE_ED25519,
    MoveLinkBindingInvalid,
    MoveLinkError,
    MoveLinkReleaseDestinationMismatch,
    MoveLinkReleaseSigInvalid,
    MoveLinkUserSigInvalid,
    ReplayCache,
    UnsupportedMoveLinkSuite,
    UnsupportedUserSigSuite,
    b64url_decode,
    b64url_encode,
    build_move_link,
    build_user_identity_assertion,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    generate_routing_secret,
    generate_x25519_keypair,
    keyed_hash,
    random_token,
    sha256_hex,
    sign_ed25519,
    move_link_release_signed_bytes,
    move_link_user_signed_bytes,
    sign_user_assertion,
    sign_user_self,
    user_identity_signed_bytes,
    validate_move_link_suite,
    validate_user_sig_suite,
    verify_ed25519,
    verify_move_link,
    verify_user_identity_assertion,
    verify_user_self,
    x25519_exchange,
)
from socialhome.domain.user import UserIdentityAssertion


def test_derive_instance_id_deterministic():
    """derive_instance_id returns the same value for the same key."""
    kp = generate_identity_keypair()
    assert derive_instance_id(kp.public_key) == derive_instance_id(kp.public_key)


def test_derive_instance_id_length():
    """derive_instance_id produces a 32-character string."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    assert len(iid) == 32


def test_derive_instance_id_different_keys_differ():
    """Two different keypairs yield different instance IDs."""
    a = generate_identity_keypair()
    b = generate_identity_keypair()
    assert derive_instance_id(a.public_key) != derive_instance_id(b.public_key)


def test_derive_instance_id_rejects_bad_key_length():
    """derive_instance_id raises ValueError for a key shorter than 32 bytes."""
    with pytest.raises(ValueError):
        derive_instance_id(b"short")


def test_derive_user_id_deterministic():
    """derive_user_id is stable for a given key and username."""
    kp = generate_identity_keypair()
    assert derive_user_id(kp.public_key, "alice") == derive_user_id(
        kp.public_key, "alice"
    )


def test_derive_user_id_different_username():
    """Different usernames yield different user IDs."""
    kp = generate_identity_keypair()
    assert derive_user_id(kp.public_key, "alice") != derive_user_id(
        kp.public_key, "bob"
    )


def test_derive_user_id_different_key():
    """Same username under different instance keys yields different IDs."""
    a = generate_identity_keypair()
    b = generate_identity_keypair()
    assert derive_user_id(a.public_key, "alice") != derive_user_id(
        b.public_key, "alice"
    )


def test_derive_user_id_empty_username_rejected():
    """Empty username raises ValueError."""
    kp = generate_identity_keypair()
    with pytest.raises(ValueError):
        derive_user_id(kp.public_key, "")


def test_ed25519_sign_verify():
    """A signature produced with a private key verifies with the matching public key."""
    kp = generate_identity_keypair()
    msg = b"hello"
    sig = sign_ed25519(kp.private_key, msg)
    assert verify_ed25519(kp.public_key, msg, sig)


def test_ed25519_wrong_message():
    """Verification fails when the message does not match the signature."""
    kp = generate_identity_keypair()
    sig = sign_ed25519(kp.private_key, b"hello")
    assert not verify_ed25519(kp.public_key, b"wrong", sig)


def test_ed25519_wrong_key():
    """Verification fails when a different public key is used."""
    a = generate_identity_keypair()
    b = generate_identity_keypair()
    sig = sign_ed25519(a.private_key, b"msg")
    assert not verify_ed25519(b.public_key, b"msg", sig)


def test_x25519_shared_secret_agreement():
    """Both sides of an X25519 exchange produce the same shared secret."""
    a = generate_x25519_keypair()
    b = generate_x25519_keypair()
    s1 = x25519_exchange(a.private_key, b.public_key)
    s2 = x25519_exchange(b.private_key, a.public_key)
    assert s1 == s2


def test_x25519_different_peers_different_secrets():
    """A and B share a secret distinct from A and C."""
    a = generate_x25519_keypair()
    b = generate_x25519_keypair()
    c = generate_x25519_keypair()
    assert x25519_exchange(a.private_key, b.public_key) != x25519_exchange(
        a.private_key, c.public_key
    )


def test_user_identity_assertion_sign_and_verify():
    """A fresh assertion signs and verifies cleanly."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    uid = derive_user_id(kp.public_key, "pascal")
    issued = datetime.now(timezone.utc).isoformat()
    sig = sign_user_assertion(
        kp.private_key,
        user_id=uid,
        instance_id=iid,
        username="pascal",
        display_name="Pascal",
        issued_at=issued,
    )
    a = UserIdentityAssertion(
        user_id=uid,
        instance_id=iid,
        username="pascal",
        display_name="Pascal",
        issued_at=issued,
        signature=sig,
    )
    verify_user_identity_assertion(a, kp.public_key)


def test_user_identity_assertion_tampered_display_name():
    """A tampered display_name invalidates the signature."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    uid = derive_user_id(kp.public_key, "pascal")
    issued = datetime.now(timezone.utc).isoformat()
    sig = sign_user_assertion(
        kp.private_key,
        user_id=uid,
        instance_id=iid,
        username="pascal",
        display_name="Pascal",
        issued_at=issued,
    )
    bad = UserIdentityAssertion(
        user_id=uid,
        instance_id=iid,
        username="pascal",
        display_name="TAMPERED",
        issued_at=issued,
        signature=sig,
    )
    with pytest.raises(ValueError, match="Invalid"):
        verify_user_identity_assertion(bad, kp.public_key)


def test_user_identity_assertion_wrong_instance_id():
    """A mismatched instance_id is caught before verifying the signature."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    uid = derive_user_id(kp.public_key, "pascal")
    issued = datetime.now(timezone.utc).isoformat()
    sig = sign_user_assertion(
        kp.private_key,
        user_id=uid,
        instance_id=iid,
        username="pascal",
        display_name="P",
        issued_at=issued,
    )
    bad = UserIdentityAssertion(
        user_id=uid,
        instance_id="wrong_id",
        username="pascal",
        display_name="P",
        issued_at=issued,
        signature=sig,
    )
    with pytest.raises(ValueError, match="instance_id"):
        verify_user_identity_assertion(bad, kp.public_key)


def test_b64url_roundtrip():
    """b64url_encode / b64url_decode round-trips arbitrary bytes."""
    data = b"hello world \x00\xff"
    assert b64url_decode(b64url_encode(data)) == data


def test_replay_cache_seen_twice():
    """The second call to seen() for the same ID returns True."""
    rc = ReplayCache()
    assert rc.seen("x") is False
    assert rc.seen("x") is True


def test_replay_cache_different_ids_independent():
    """Seeing ID 'a' does not mark ID 'b' as seen."""
    rc = ReplayCache()
    rc.seen("a")
    assert rc.seen("b") is False


def test_replay_cache_load_from_persistence():
    """Replay IDs loaded from storage are immediately considered seen."""
    rc = ReplayCache()
    rc.load([("m1", datetime.now(timezone.utc).isoformat())])
    assert rc.seen("m1") is True
    assert rc.seen("m2") is False


def test_replay_cache_warm_entries_dedupe_scoped_runtime_checks():
    """A warmed (cross-restart) entry must dedupe a runtime check that
    passes the sender's ``from_instance``.

    The inbound pipeline calls ``seen(msg_id, from_instance=<verified
    signer>)``; the persisted ``federation_replay_cache`` is keyed by
    ``msg_id`` alone (its PK), so a warmed entry has to match regardless of
    the ``from_instance`` the live check supplies. Otherwise a replay (or a
    re-signed redelivery) of an event processed before a restart slips past
    dedup and is applied twice.
    """
    rc = ReplayCache()
    rc.load([("warmed-msg", datetime.now(timezone.utc).isoformat())])
    assert rc.seen("warmed-msg", from_instance="peer-instance") is True


def test_replay_cache_window_outlasts_max_jittered_redelivery():
    """Drift guard: the replay-dedup window MUST outlast the federation
    outbox's max jittered redelivery interval.

    Outbox redelivery re-signs each retry with a fresh timestamp
    (``resign_for_redelivery``), so a retry of a delivery whose 2xx ack
    was lost passes the §24.11 timestamp gate and relies SOLELY on the
    replay cache to be deduped. If the window were shorter than the max
    retry cadence the receiver would apply the event twice. This fails if
    someone later shrinks the window or raises the backoff ceiling past it.
    """
    from socialhome.infrastructure.outbox_processor import (
        BACKOFF_SECONDS,
        JITTER_RATIO,
    )

    max_jittered_interval = max(BACKOFF_SECONDS) * (1 + JITTER_RATIO)
    assert REPLAY_CACHE_WINDOW.total_seconds() > max_jittered_interval


def test_replay_cache_dedupes_lost_ack_retry_within_window():
    """A ``REPLAY_CACHE_WINDOW``-sized cache dedupes a lost-ack retry that
    lands hours after the first (successful-but-unacked) delivery, and only
    forgets it once the window has elapsed."""
    rc = ReplayCache(window=REPLAY_CACHE_WINDOW)
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # First delivery: not seen, recorded.
    assert rc.seen("m", from_instance="p", now=t0) is False
    # Lost-ack retry ~5 h later (within the ~5.2 h max jittered cadence):
    # still deduped.
    assert rc.seen("m", from_instance="p", now=t0 + timedelta(hours=5)) is True
    # Well past the window: pruned, so a brand-new event with the same id
    # is no longer falsely deduped.
    assert rc.seen("m", from_instance="p", now=t0 + timedelta(hours=26)) is False


def test_crypto_helpers_random_token():
    """random_token returns a URL-safe string of sufficient length."""
    t = random_token(32)
    assert len(t) > 40


def test_crypto_helpers_sha256_hex_bytes():
    """sha256_hex on bytes returns lowercase hex of length 64."""
    assert len(sha256_hex(b"hello")) == 64


def test_crypto_helpers_sha256_hex_str():
    """sha256_hex on str auto-encodes to utf-8, matching the bytes result."""
    assert sha256_hex("hello") == sha256_hex(b"hello")


def test_crypto_helpers_generate_routing_secret():
    """Routing secret is 64 hex chars (32 bytes)."""
    s = generate_routing_secret()
    assert len(s) == 64


def test_crypto_helpers_keyed_hash():
    """keyed_hash returns a 32-byte HMAC-SHA256 digest."""
    h = keyed_hash("aa" * 32, b"data")
    assert len(h) == 32


# ─── Defensive edge paths ─────────────────────────────────────────────────


def test_verify_ed25519_bad_key_length():
    """verify_ed25519 returns False for wrong-length public key."""
    assert verify_ed25519(b"short", b"msg", b"sig") is False


def test_verify_ed25519_invalid_signature():
    """verify_ed25519 returns False for corrupted signature."""
    kp = generate_identity_keypair()
    sig = sign_ed25519(kp.private_key, b"msg")
    assert verify_ed25519(kp.public_key, b"msg", sig[:32]) is False


def test_sign_ed25519_bad_seed():
    """sign_ed25519 with wrong-length seed raises ValueError."""
    with pytest.raises(ValueError):
        sign_ed25519(b"short", b"msg")


def test_x25519_bad_key_length():
    """x25519_exchange with wrong key length raises ValueError."""
    with pytest.raises(ValueError):
        x25519_exchange(b"short", b"also-short")


def test_derive_instance_id_bad_key():
    """derive_instance_id with wrong length raises ValueError."""
    with pytest.raises(ValueError):
        derive_instance_id(b"not-32-bytes")


def test_replay_cache_prune_removes_old_entries():
    """ReplayCache.prune removes entries older than the window."""
    rc = ReplayCache(window=timedelta(seconds=1))
    past = datetime.now(timezone.utc) - timedelta(seconds=10)
    rc._seen["old"] = past
    rc.prune(now=datetime.now(timezone.utc))
    assert "old" not in rc._seen


def test_replay_cache_prune_handles_naive_persisted_entries():
    """:meth:`ReplayCache.load` warms the cache from
    ``federation_replay_cache.received_at`` rows, which are written via
    SQLite's ``datetime('now')`` and therefore offset-naive. ``seen()``
    records new keys as offset-aware ``datetime.now(timezone.utc)``.

    Pre-fix, the next ``_prune`` raised
    ``TypeError: can't compare offset-naive and offset-aware datetimes``
    and the inbound federation pipeline returned 500 for every
    envelope on a node that had warmed its replay cache from disk.
    """
    rc = ReplayCache(window=timedelta(hours=1))
    naive_recent = "2099-01-01 00:00:00"  # offset-naive (matches DB form)
    rc.load([("persisted-msg", naive_recent)])

    # Triggering ``seen()`` runs ``_prune`` against a current
    # offset-aware ``now`` and a mix of offset-aware + offset-naive
    # entries in ``_seen`` — must not raise.
    assert rc.seen("fresh-msg") is False
    # The persisted entry is still considered seen (well within window).
    assert rc.seen("persisted-msg") is True


# ─── User-identity self-signature (independent user identity Phase 1) ───────


def test_user_sig_suite_contract():
    """USER_SIG_SUITE tag contract: ed25519 only, reject unknown suites."""
    assert USER_SIG_SUITE_ED25519 == "ed25519"
    assert USER_SIG_SUITE_ED25519 in SUPPORTED_USER_SIG_SUITES
    assert "ed25519+mldsa65" not in SUPPORTED_USER_SIG_SUITES
    assert issubclass(UnsupportedUserSigSuite, ValueError)
    assert validate_user_sig_suite(USER_SIG_SUITE_ED25519) is None
    with pytest.raises(UnsupportedUserSigSuite):
        validate_user_sig_suite("bogus")


def test_user_self_sign_roundtrip():
    """USER self-signature round-trips and rejects a tampered body."""
    kp = generate_identity_keypair()
    body = user_identity_signed_bytes(
        user_id="u1",
        instance_id="i1",
        username="alice",
        user_public_key=kp.public_key,
        user_sig_suite=USER_SIG_SUITE_ED25519,
    )
    sig = sign_user_self(kp.private_key, body)
    assert verify_user_self(kp.public_key, body, sig) is True
    assert verify_user_self(kp.public_key, body + b"x", sig) is False


def test_user_identity_signed_bytes_no_field_boundary_collision():
    """Length-prefixing prevents a NUL in one field from shifting boundaries so
    two different field tuples canonicalize to identical signed bytes."""
    kp = generate_identity_keypair()
    pk = kp.public_key
    a = user_identity_signed_bytes(
        user_id="u1\x00i1",
        instance_id="X",
        username="alice",
        user_public_key=pk,
        user_sig_suite=USER_SIG_SUITE_ED25519,
    )
    b = user_identity_signed_bytes(
        user_id="u1",
        instance_id="i1\x00X",
        username="alice",
        user_public_key=pk,
        user_sig_suite=USER_SIG_SUITE_ED25519,
    )
    assert a != b


def test_validate_user_sig_suite_fails_closed_on_non_string():
    """A non-string (unhashable) suite must raise UnsupportedUserSigSuite, not a
    bare TypeError from `suite not in frozenset`."""
    with pytest.raises(UnsupportedUserSigSuite):
        validate_user_sig_suite(["ed25519"])  # type: ignore[arg-type]
    with pytest.raises(UnsupportedUserSigSuite):
        validate_user_sig_suite({"s": 1})  # type: ignore[arg-type]


# ─── User identity binding (dual signature) — Phase 1 ──────────────────────


def _make_dual_assertion(*, username: str = "pascal", display_name: str = "Pascal"):
    """Build an assertion carrying both the instance sig and the user self-sig.

    Returns ``(assertion, instance_pubkey, user_keypair)``.
    """
    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    iid = derive_instance_id(instance_kp.public_key)
    uid = derive_user_id(instance_kp.public_key, username)
    issued = datetime.now(timezone.utc).isoformat()
    a = build_user_identity_assertion(
        instance_seed=instance_kp.private_key,
        user_id=uid,
        instance_id=iid,
        username=username,
        display_name=display_name,
        issued_at=issued,
        user_seed=user_kp.private_key,
        user_public_key=user_kp.public_key,
    )
    return a, instance_kp.public_key, user_kp


def test_dual_assertion_carries_user_binding_and_verifies():
    """An assertion built with a user key carries the binding fields and both
    signatures verify."""
    a, instance_pubkey, user_kp = _make_dual_assertion()
    assert a.user_identity_public_key == user_kp.public_key.hex()
    assert a.user_sig_suite == USER_SIG_SUITE_ED25519
    assert a.user_signature is not None
    assert a.user_pq_public_key is None
    # Does not raise -> both instance sig and user self-sig validate.
    verify_user_identity_assertion(a, instance_pubkey)


def test_dual_assertion_tampered_user_pubkey_rejected():
    """Swapping in another keypair's pubkey breaks the user self-signature."""
    a, instance_pubkey, _user_kp = _make_dual_assertion()
    other = generate_identity_keypair()
    bad = dataclasses.replace(a, user_identity_public_key=other.public_key.hex())
    with pytest.raises(ValueError, match="user"):
        verify_user_identity_assertion(bad, instance_pubkey)


def test_dual_assertion_instance_sig_still_enforced():
    """The existing instance-signature path still rejects a wrong instance key."""
    a, _instance_pubkey, _user_kp = _make_dual_assertion()
    wrong = generate_identity_keypair()
    with pytest.raises(ValueError):
        verify_user_identity_assertion(a, wrong.public_key)


def test_dual_assertion_unknown_user_sig_suite_raises():
    """An unknown user_sig_suite on a present binding propagates
    UnsupportedUserSigSuite (no default fallback)."""
    a, instance_pubkey, _user_kp = _make_dual_assertion()
    bad = dataclasses.replace(a, user_sig_suite="bogus")
    with pytest.raises(UnsupportedUserSigSuite):
        verify_user_identity_assertion(bad, instance_pubkey)


def test_legacy_assertion_without_user_binding_unchanged():
    """An assertion built with no user key leaves all new fields None and verifies
    exactly as the legacy instance-sig-only path."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    uid = derive_user_id(kp.public_key, "pascal")
    issued = datetime.now(timezone.utc).isoformat()
    a = build_user_identity_assertion(
        instance_seed=kp.private_key,
        user_id=uid,
        instance_id=iid,
        username="pascal",
        display_name="Pascal",
        issued_at=issued,
    )
    assert a.user_identity_public_key is None
    assert a.user_pq_public_key is None
    assert a.user_sig_suite is None
    assert a.user_signature is None
    verify_user_identity_assertion(a, kp.public_key)


def test_dual_assertion_key_transplant_rejected():
    """A transplant attack must fail: an attacker who holds a victim's
    legitimately instance-signed, binding-bearing assertion swaps in their own
    user_identity_public_key and forges a VALID self-sig with their own seed,
    leaving the instance signature untouched. Because the instance signature
    now commits to the user pubkey, the swap breaks the instance sig and the
    whole assertion is rejected — binding the attacker key to the victim's
    user_id is impossible without the household's instance seed.
    """
    a, instance_pubkey, _victim_kp = _make_dual_assertion()
    attacker = generate_identity_keypair()
    # Attacker forges a PROPER self-sig for their own key over the recomputed
    # user-identity signed bytes (proves possession of the attacker key).
    body = user_identity_signed_bytes(
        user_id=a.user_id,
        instance_id=a.instance_id,
        username=a.username,
        user_public_key=attacker.public_key,
        user_sig_suite=USER_SIG_SUITE_ED25519,
    )
    forged_self_sig = b64url_encode(sign_user_self(attacker.private_key, body))
    tampered = dataclasses.replace(
        a,
        user_identity_public_key=attacker.public_key.hex(),
        user_signature=forged_self_sig,
        # instance `signature` deliberately left untouched.
    )
    # Self-sig is now internally valid, but the instance sig no longer covers
    # the swapped pubkey -> rejected.
    with pytest.raises(ValueError, match="signature"):
        verify_user_identity_assertion(tampered, instance_pubkey)


def test_dual_assertion_malformed_pubkey_hex_raises_domain_error():
    """A malformed (non-hex) user_identity_public_key raises a clear domain
    ValueError, not a raw bytes.fromhex 'non-hexadecimal number' error."""
    a, instance_pubkey, _user_kp = _make_dual_assertion()
    bad = dataclasses.replace(a, user_identity_public_key="zz not hex zz")
    with pytest.raises(ValueError, match="malformed user identity binding"):
        verify_user_identity_assertion(bad, instance_pubkey)


def test_dual_assertion_missing_suite_defaults_to_ed25519():
    """A first-revision payload that carries the pubkey but omits user_sig_suite
    defaults to ed25519 (the documented tripwire) and verifies."""
    a, instance_pubkey, _user_kp = _make_dual_assertion()
    # The signed body uses the suite passed at build time (ed25519); a
    # first-revision sender simply omits the field on the wire.
    first_rev = dataclasses.replace(a, user_sig_suite=None)
    verify_user_identity_assertion(first_rev, instance_pubkey)


# ─── identity_anchor binding (user_id derives from anchor, not username) ────


def test_anchor_derived_user_id_verifies():
    """When an assertion carries an ``identity_anchor`` distinct from the
    username, ``user_id`` derives from the anchor (not the username) and the
    assertion verifies."""
    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    iid = derive_instance_id(instance_kp.public_key)
    anchor = "deadbeef" * 4  # opaque uuid-like value, != username
    uid = derive_user_id(instance_kp.public_key, anchor)
    issued = datetime.now(timezone.utc).isoformat()
    a = build_user_identity_assertion(
        instance_seed=instance_kp.private_key,
        user_id=uid,
        instance_id=iid,
        username="loginname",
        display_name="Login Name",
        issued_at=issued,
        user_seed=user_kp.private_key,
        user_public_key=user_kp.public_key,
        identity_anchor=anchor,
    )
    assert a.identity_anchor == anchor
    # Does not raise -> user_id derives from the anchor and sigs cover it.
    verify_user_identity_assertion(a, instance_kp.public_key)
    # The anchor is genuinely the derivation input, not the username.
    assert uid != derive_user_id(instance_kp.public_key, "loginname")


def test_forged_anchor_rejected():
    """Swapping in a different ``identity_anchor`` breaks verification: the
    user_id no longer derives from it AND the signatures no longer match."""
    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    iid = derive_instance_id(instance_kp.public_key)
    anchor = "deadbeef" * 4
    uid = derive_user_id(instance_kp.public_key, anchor)
    issued = datetime.now(timezone.utc).isoformat()
    a = build_user_identity_assertion(
        instance_seed=instance_kp.private_key,
        user_id=uid,
        instance_id=iid,
        username="loginname",
        display_name="Login Name",
        issued_at=issued,
        user_seed=user_kp.private_key,
        user_public_key=user_kp.public_key,
        identity_anchor=anchor,
    )
    bad = dataclasses.replace(a, identity_anchor="cafebabe" * 4)
    with pytest.raises(ValueError):
        verify_user_identity_assertion(bad, instance_kp.public_key)


def test_legacy_no_anchor_falls_back_to_username():
    """An assertion with no binding and no anchor verifies via the legacy
    username derivation path unchanged."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    uid = derive_user_id(kp.public_key, "pascal")
    issued = datetime.now(timezone.utc).isoformat()
    a = build_user_identity_assertion(
        instance_seed=kp.private_key,
        user_id=uid,
        instance_id=iid,
        username="pascal",
        display_name="Pascal",
        issued_at=issued,
    )
    assert a.identity_anchor is None
    verify_user_identity_assertion(a, kp.public_key)


def test_anchored_binding_roundtrip():
    """An anchored, binding-bearing assertion sets identity_anchor and BOTH
    signatures cover the anchor — tampering the anchor breaks the signatures
    (not just the derivation check)."""
    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    iid = derive_instance_id(instance_kp.public_key)
    anchor = "deadbeef" * 4
    uid = derive_user_id(instance_kp.public_key, anchor)
    issued = datetime.now(timezone.utc).isoformat()
    a = build_user_identity_assertion(
        instance_seed=instance_kp.private_key,
        user_id=uid,
        instance_id=iid,
        username="loginname",
        display_name="Login Name",
        issued_at=issued,
        user_seed=user_kp.private_key,
        user_public_key=user_kp.public_key,
        identity_anchor=anchor,
    )
    assert a.identity_anchor == anchor
    assert a.user_signature is not None

    # The INSTANCE signature covers the anchor: keep the user_id matching the
    # tampered anchor's derivation so the instance-sig check (not the user_id
    # derivation check) is what rejects it.
    forged_anchor = "cafebabe" * 4
    forged_uid = derive_user_id(instance_kp.public_key, forged_anchor)
    instance_tampered = dataclasses.replace(
        a, identity_anchor=forged_anchor, user_id=forged_uid
    )
    with pytest.raises(ValueError, match="signature"):
        verify_user_identity_assertion(instance_tampered, instance_kp.public_key)

    # The USER self-signature also covers the anchor. Re-sign the instance leg
    # for the forged anchor so the instance sig passes, then prove the user
    # self-sig is what breaks.
    body_for_user = user_identity_signed_bytes(
        user_id=forged_uid,
        instance_id=iid,
        username="loginname",
        user_public_key=user_kp.public_key,
        user_sig_suite=USER_SIG_SUITE_ED25519,
    )
    # NOTE: body_for_user does NOT include the anchor here on purpose — it is
    # rebuilt by verify from the (forged) anchor, so this stale self-sig fails.
    stale_self_sig = b64url_encode(sign_user_self(user_kp.private_key, body_for_user))
    reinstance_sig = sign_user_assertion(
        instance_kp.private_key,
        user_id=forged_uid,
        instance_id=iid,
        username="loginname",
        display_name="Login Name",
        issued_at=issued,
        user_identity_public_key=user_kp.public_key,
        user_sig_suite=USER_SIG_SUITE_ED25519,
        identity_anchor=forged_anchor,
    )
    user_tampered = dataclasses.replace(
        a,
        identity_anchor=forged_anchor,
        user_id=forged_uid,
        signature=reinstance_sig,
        user_signature=stale_self_sig,
    )
    with pytest.raises(ValueError, match="self-signature"):
        verify_user_identity_assertion(user_tampered, instance_kp.public_key)


# ─── Move-out link suite tag + signed bytes (move-out link, Task 1) ────────


def test_move_link_suite_contract():
    """MOVE_LINK_SUITE tag contract: ed25519 only, reject unknown suites and
    non-strings (no TypeError leak)."""
    assert MOVE_LINK_SUITE_ED25519 == "ed25519"
    assert MOVE_LINK_SUITE_ED25519 in SUPPORTED_MOVE_LINK_SUITES
    assert "ed25519+mldsa65" not in SUPPORTED_MOVE_LINK_SUITES
    assert issubclass(UnsupportedMoveLinkSuite, ValueError)
    assert validate_move_link_suite(MOVE_LINK_SUITE_ED25519) is None
    with pytest.raises(UnsupportedMoveLinkSuite):
        validate_move_link_suite("rot13")
    with pytest.raises(UnsupportedMoveLinkSuite):
        validate_move_link_suite(object())  # type: ignore[arg-type]


def _move_link_byte_args():
    kp = generate_identity_keypair()
    new_kp = generate_identity_keypair()
    return dict(
        old_user_id="old_uid",
        new_user_id="new_uid",
        user_public_key=kp.public_key,
        new_instance_public_key=new_kp.public_key,
        issued_at="2026-06-16T00:00:00+00:00",
        suite=MOVE_LINK_SUITE_ED25519,
    )


def test_move_link_release_bytes_commit_to_destination_user_id():
    """The release signature is a destination-pin — changing only the
    destination user_id changes the signed bytes."""
    args = _move_link_byte_args()
    a = move_link_release_signed_bytes(**args)
    b = move_link_release_signed_bytes(**{**args, "new_user_id": "other_uid"})
    assert a != b


def test_move_link_user_and_release_bytes_differ_for_identical_params():
    """The user and release legs use distinct domain tags so the two signatures
    can never be cross-replayed, even over identical params."""
    args = _move_link_byte_args()
    assert move_link_user_signed_bytes(**args) != move_link_release_signed_bytes(**args)


# ─── Move-out link build + verify (move-out link, Task 2) ──────────────────


def _build_move_scenario(*, username: str = "pascal", display_name: str = "Pascal"):
    """Mint a realistic move-out scenario and return everything a test needs.

    A single portable per-user keypair ``P`` and a shared ``identity_anchor``
    span both homes (a move is NOT a re-keying). ``old_id``/``new_id`` derive
    from each home's instance pubkey + the anchor. The new home's P-binding
    rides in a real ``UserIdentityAssertion`` signed by the new home.

    Returns a dict of the pieces (keypairs, ids, the assembled MoveLink, the
    verify-time inputs).
    """
    user_kp = generate_identity_keypair()  # portable P
    old_home_kp = generate_identity_keypair()
    new_home_kp = generate_identity_keypair()

    anchor = "deadbeefcafef00d" * 2  # opaque uuid-like, spans both homes
    old_instance_id = derive_instance_id(old_home_kp.public_key)
    new_instance_id = derive_instance_id(new_home_kp.public_key)
    old_user_id = derive_user_id(old_home_kp.public_key, anchor)
    new_user_id = derive_user_id(new_home_kp.public_key, anchor)
    issued = datetime.now(timezone.utc).isoformat()

    new_home_assertion = build_user_identity_assertion(
        instance_seed=new_home_kp.private_key,
        user_id=new_user_id,
        instance_id=new_instance_id,
        username=username,
        display_name=display_name,
        issued_at=issued,
        user_seed=user_kp.private_key,
        user_public_key=user_kp.public_key,
        identity_anchor=anchor,
    )

    link = build_move_link(
        user_seed=user_kp.private_key,
        user_public_key=user_kp.public_key,
        old_home_instance_seed=old_home_kp.private_key,
        old_user_id=old_user_id,
        old_instance_id=old_instance_id,
        new_instance_public_key=new_home_kp.public_key,
        new_home_assertion=new_home_assertion,
        issued_at=issued,
    )
    return {
        "user_kp": user_kp,
        "old_home_kp": old_home_kp,
        "new_home_kp": new_home_kp,
        "anchor": anchor,
        "old_user_id": old_user_id,
        "new_user_id": new_user_id,
        "link": link,
    }


def test_verify_move_link_accepts_well_formed_link():
    """A link built by build_move_link verifies under dual consent + bindings."""
    s = _build_move_scenario()
    # Does not raise -> user sig, release sig, and both bindings verify.
    verify_move_link(
        s["link"],
        old_home_pinned_pk=s["old_home_kp"].public_key,
        stored_old_user_pubkey=s["user_kp"].public_key,
    )


def test_verify_move_link_wrong_length_new_instance_key_typed_error():
    """A valid-hex but wrong-length new_instance_public_key is rejected as a
    typed MoveLinkBindingInvalid (not a bare ValueError from derive_instance_id)."""
    s = _build_move_scenario()
    bad = dataclasses.replace(s["link"], new_instance_public_key="aa")
    with pytest.raises(MoveLinkBindingInvalid):
        verify_move_link(
            bad,
            old_home_pinned_pk=s["old_home_kp"].public_key,
            stored_old_user_pubkey=s["user_kp"].public_key,
        )


def test_verify_move_link_malformed_user_signature_encoding_typed_error():
    """A malformed-base64 user_signature is rejected as a typed
    MoveLinkUserSigInvalid (not a bare binascii.Error)."""
    s = _build_move_scenario()
    bad = dataclasses.replace(s["link"], user_signature="a")
    with pytest.raises(MoveLinkUserSigInvalid):
        verify_move_link(
            bad,
            old_home_pinned_pk=s["old_home_kp"].public_key,
            stored_old_user_pubkey=s["user_kp"].public_key,
        )


def test_verify_move_link_malformed_release_signature_encoding_typed_error():
    """A malformed-base64 release_signature is rejected as a typed
    MoveLinkReleaseSigInvalid (not a bare binascii.Error)."""
    s = _build_move_scenario()
    bad = dataclasses.replace(s["link"], release_signature="a")
    with pytest.raises(MoveLinkReleaseSigInvalid):
        verify_move_link(
            bad,
            old_home_pinned_pk=s["old_home_kp"].public_key,
            stored_old_user_pubkey=s["user_kp"].public_key,
        )


def test_verify_move_link_forged_user_signature_rejected():
    """A tampered user_signature fails the USER-consent leg."""
    s = _build_move_scenario()
    bad = dataclasses.replace(s["link"], user_signature=b64url_encode(b"\x00" * 64))
    with pytest.raises(MoveLinkUserSigInvalid):
        verify_move_link(
            bad,
            old_home_pinned_pk=s["old_home_kp"].public_key,
            stored_old_user_pubkey=s["user_kp"].public_key,
        )


def test_verify_move_link_forged_release_signature_rejected():
    """A tampered release_signature fails the old-home RELEASE-consent leg."""
    s = _build_move_scenario()
    bad = dataclasses.replace(s["link"], release_signature=b64url_encode(b"\x00" * 64))
    with pytest.raises(MoveLinkReleaseSigInvalid):
        verify_move_link(
            bad,
            old_home_pinned_pk=s["old_home_kp"].public_key,
            stored_old_user_pubkey=s["user_kp"].public_key,
        )


def test_verify_move_link_release_resigned_for_other_destination_rejected():
    """A release signed over a DIFFERENT destination, spliced onto the original
    link, fails: the recomputed release bytes commit to this link's destination
    so the foreign release doesn't verify (destination-pin)."""
    s = _build_move_scenario()
    other = _build_move_scenario()  # a different new home + new_user_id
    # Splice the foreign release sig onto the original link (everything else,
    # including the original destination, stays).
    spliced = dataclasses.replace(
        s["link"], release_signature=other["link"].release_signature
    )
    with pytest.raises((MoveLinkReleaseSigInvalid, MoveLinkReleaseDestinationMismatch)):
        verify_move_link(
            spliced,
            old_home_pinned_pk=s["old_home_kp"].public_key,
            stored_old_user_pubkey=s["user_kp"].public_key,
        )


def test_verify_move_link_stored_old_user_pubkey_mismatch_rejected():
    """If the receiver's stored P for old_user_id != the link's P, reject:
    binding #3 (P↔old_id) fails."""
    s = _build_move_scenario()
    wrong = generate_identity_keypair()
    with pytest.raises(MoveLinkBindingInvalid):
        verify_move_link(
            s["link"],
            old_home_pinned_pk=s["old_home_kp"].public_key,
            stored_old_user_pubkey=wrong.public_key,
        )


def test_verify_move_link_tampered_new_home_assertion_rejected():
    """A corrupted embedded new_home_assertion (instance signature broken)
    fails binding #4."""
    s = _build_move_scenario()
    tampered_assertion = dataclasses.replace(
        s["link"].new_home_assertion,
        signature=b64url_encode(b"\x00" * 64),
    )
    bad = dataclasses.replace(s["link"], new_home_assertion=tampered_assertion)
    with pytest.raises(MoveLinkBindingInvalid):
        verify_move_link(
            bad,
            old_home_pinned_pk=s["old_home_kp"].public_key,
            stored_old_user_pubkey=s["user_kp"].public_key,
        )


def test_verify_move_link_new_home_assertion_binds_different_p_rejected():
    """If the embedded assertion binds a P different from the link's P, reject:
    the P↔new_id binding is inconsistent with the link's portable key."""
    s = _build_move_scenario()
    other_p = generate_identity_keypair()
    tampered_assertion = dataclasses.replace(
        s["link"].new_home_assertion,
        user_identity_public_key=other_p.public_key.hex(),
    )
    bad = dataclasses.replace(s["link"], new_home_assertion=tampered_assertion)
    with pytest.raises(MoveLinkBindingInvalid):
        verify_move_link(
            bad,
            old_home_pinned_pk=s["old_home_kp"].public_key,
            stored_old_user_pubkey=s["user_kp"].public_key,
        )


def test_verify_move_link_unknown_suite_rejected():
    """An unknown move-link suite is rejected with no default fallback."""
    s = _build_move_scenario()
    bad = dataclasses.replace(s["link"], suite="ed25519+mldsa65")
    with pytest.raises(UnsupportedMoveLinkSuite):
        verify_move_link(
            bad,
            old_home_pinned_pk=s["old_home_kp"].public_key,
            stored_old_user_pubkey=s["user_kp"].public_key,
        )


def test_verify_move_link_freshness_is_caller_controlled():
    """A move-link is a DURABLE record: an ~800-day-old but otherwise valid link
    verifies SUCCESSFULLY under the default ``max_age=None`` (the resolve-backstop
    case), and the SAME link is REJECTED when the caller passes a tight
    ``max_age`` — proving the bound now reaches both the link's own freshness
    gate AND the embedded new-home assertion check."""
    s = _build_move_scenario()
    way_back = (datetime.now(timezone.utc) - timedelta(days=800)).isoformat()
    # Build a fully-consistent but OLD link (assertion + link share the old date)
    # so it fails ONLY on freshness when freshness is actually enforced.
    user_kp = s["user_kp"]
    old_home_kp = s["old_home_kp"]
    new_home_kp = s["new_home_kp"]
    anchor = s["anchor"]
    new_instance_id = derive_instance_id(new_home_kp.public_key)
    new_user_id = derive_user_id(new_home_kp.public_key, anchor)
    old_instance_id = derive_instance_id(old_home_kp.public_key)
    old_user_id = derive_user_id(old_home_kp.public_key, anchor)
    old_assertion = build_user_identity_assertion(
        instance_seed=new_home_kp.private_key,
        user_id=new_user_id,
        instance_id=new_instance_id,
        username="pascal",
        display_name="Pascal",
        issued_at=way_back,
        user_seed=user_kp.private_key,
        user_public_key=user_kp.public_key,
        identity_anchor=anchor,
    )
    old_link = build_move_link(
        user_seed=user_kp.private_key,
        user_public_key=user_kp.public_key,
        old_home_instance_seed=old_home_kp.private_key,
        old_user_id=old_user_id,
        old_instance_id=old_instance_id,
        new_instance_public_key=new_home_kp.public_key,
        new_home_assertion=old_assertion,
        issued_at=way_back,
    )

    # (a) Durable record: default max_age=None does NOT age-check anywhere — the
    #     signatures + bindings still verify, only the age gates are skipped.
    verify_move_link(
        old_link,
        old_home_pinned_pk=old_home_kp.public_key,
        stored_old_user_pubkey=user_kp.public_key,
    )

    # (b) Caller can still bound freshness: a tight max_age rejects the SAME link,
    #     proving the bound reaches the embedded assertion check too.
    with pytest.raises(MoveLinkError):
        verify_move_link(
            old_link,
            old_home_pinned_pk=old_home_kp.public_key,
            stored_old_user_pubkey=user_kp.public_key,
            max_age=timedelta(days=30),
        )
