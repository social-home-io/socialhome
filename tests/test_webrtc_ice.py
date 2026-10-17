"""Tests for the shared ICE-server / TURN-credential helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging

from socialhome.webrtc_ice import (
    build_ice_servers,
    make_turn_credential,
    warn_if_no_turn,
    warn_if_turn_unusable,
)

# ─── HMAC TURN credentials ────────────────────────────────────────────────


def test_make_turn_credential_roundtrips_through_hmac_sha1():
    """The HMAC-SHA1 algorithm matches coturn's REST API spec — we
    can recompute the same digest server-side and verify it. Pin
    that contract so a future refactor (e.g. swapping to SHA-256)
    can't silently break compat with existing TURN deployments."""
    secret = "super-secret"
    user_id = "instance-abc"
    username, credential = make_turn_credential(
        secret,
        user_id,
        ttl_seconds=3600,
    )
    # ``username`` shape: "<expiry>:<user_id>"
    expiry_str, sep, returned_user = username.partition(":")
    assert sep == ":"
    assert returned_user == user_id
    assert int(expiry_str) > 0
    # Recompute HMAC and confirm it matches.
    expected = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            username.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("ascii")
    assert credential == expected


def test_make_turn_credential_clamps_ttl_minimum():
    """A 5-second TTL would expire before most ICE handshakes
    complete — clamp to a usable floor (60s)."""
    username, _ = make_turn_credential("s", "u", ttl_seconds=5)
    expiry = int(username.split(":", 1)[0])
    import time

    assert expiry - int(time.time()) >= 60


# ─── ICE-server list assembly ─────────────────────────────────────────────


def test_build_ice_servers_stun_only_when_no_turn_configured():
    """STUN alone is the baseline. Without TURN, two NAT'd
    households on opposite networks can't pair-check successfully —
    but that's the operator's tradeoff (TURN typically costs $),
    not something this helper should fix."""
    servers = build_ice_servers(
        stun_url="stun:stun.l.google.com:19302",
        turn_url=None,
        turn_user=None,
        turn_cred=None,
        turn_secret=None,
        turn_ttl_seconds=3600,
        hmac_user_id=None,
    )
    assert servers == [{"urls": ["stun:stun.l.google.com:19302"]}]


def test_build_ice_servers_static_credentials():
    """``webrtc_turn_user`` / ``webrtc_turn_cred`` (no HMAC secret)
    fall through as a static username/password TURN entry."""
    servers = build_ice_servers(
        stun_url="stun:stun.example:3478",
        turn_url="turn:turn.example:3478",
        turn_user="alice",
        turn_cred="hunter2",
        turn_secret=None,
        turn_ttl_seconds=3600,
        hmac_user_id="any",
    )
    assert servers[1]["username"] == "alice"
    assert servers[1]["credential"] == "hunter2"


def test_build_ice_servers_hmac_credentials_when_secret_set():
    """HMAC mode wins over static creds — coturn with
    ``--use-auth-secret`` will reject static creds, so the secret's
    presence is the signal to use the REST scheme."""
    servers = build_ice_servers(
        stun_url=None,
        turn_url="turn:turn.example:3478",
        turn_user="static-user",  # ignored when secret is set
        turn_cred="static-pwd",  # ignored when secret is set
        turn_secret="shared-secret",
        turn_ttl_seconds=60,
        hmac_user_id="instance-xyz",
    )
    entry = servers[0]
    assert entry["urls"] == ["turn:turn.example:3478"]
    assert entry["username"].endswith(":instance-xyz")
    assert entry["username"] != "static-user"  # HMAC overrode
    # Credential is a valid base64 string (decode round-trip).
    base64.b64decode(entry["credential"].encode("ascii"))


def test_build_ice_servers_hmac_requires_user_id():
    """``turn_secret`` without ``hmac_user_id`` falls back to static
    creds — the helper can't synthesize a user_id, that's a caller
    responsibility."""
    servers = build_ice_servers(
        stun_url=None,
        turn_url="turn:turn.example:3478",
        turn_user="alice",
        turn_cred="hunter2",
        turn_secret="shared-secret",
        turn_ttl_seconds=60,
        hmac_user_id=None,
    )
    # Static creds because HMAC user is missing.
    assert servers[0]["username"] == "alice"
    assert servers[0]["credential"] == "hunter2"


# ─── Diagnostic warning ───────────────────────────────────────────────────


def test_warn_if_no_turn_fires_for_stun_only_setup(caplog):
    """REGRESSION: an operator running with STUN-only on a real
    deployment will silently see WebRTC fail at ICE pair-checks.
    The audit must surface a useful hint pointing at TURN setup."""
    import logging

    caplog.set_level(logging.WARNING, logger="socialhome.webrtc_ice")
    warn_if_no_turn([{"urls": ["stun:stun.l.google.com:19302"]}])
    assert any("no TURN server configured" in rec.message for rec in caplog.records)


def test_warn_if_no_turn_silent_when_turn_present(caplog):
    """The hint is one-shot at startup. When TURN IS configured
    we don't want to fire it (the operator's done their job)."""
    import logging

    caplog.set_level(logging.WARNING, logger="socialhome.webrtc_ice")
    warn_if_no_turn(
        [
            {"urls": ["stun:stun.l.google.com:19302"]},
            {
                "urls": ["turn:turn.example:3478"],
                "username": "u",
                "credential": "c",
            },
        ]
    )
    assert not any("no TURN" in rec.message for rec in caplog.records)


def test_warn_if_no_turn_recognises_turns_url(caplog):
    """TURNS (TLS-secured TURN) counts as TURN for the warning."""
    import logging

    caplog.set_level(logging.WARNING, logger="socialhome.webrtc_ice")
    warn_if_no_turn(
        [
            {"urls": ["turns:turn.example:5349"]},
        ]
    )
    assert not any("no TURN" in rec.message for rec in caplog.records)


def test_warn_if_turn_unusable_fires_for_credentialless_turn(caplog):
    """A TURN entry with no username/credential is a half-wired footgun —
    warn_if_no_turn stays quiet (a TURN entry IS present) so this catches it."""
    import logging

    caplog.set_level(logging.WARNING, logger="socialhome.webrtc_ice")
    servers = [{"urls": ["turn:turn.example:3478"]}]  # no creds
    warn_if_no_turn(servers)
    warn_if_turn_unusable(servers)
    assert not any("no TURN server configured" in r.message for r in caplog.records)
    assert any("WITHOUT usable credentials" in r.message for r in caplog.records)


def test_warn_if_turn_unusable_silent_with_credentials(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="socialhome.webrtc_ice")
    warn_if_turn_unusable(
        [{"urls": ["turn:turn.example:3478"], "username": "u", "credential": "c"}]
    )
    assert not any("usable credentials" in r.message for r in caplog.records)


def test_warn_if_turn_unusable_silent_when_no_turn(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="socialhome.webrtc_ice")
    warn_if_turn_unusable([{"urls": ["stun:stun.l.google.com:19302"]}])
    assert not any("usable credentials" in r.message for r in caplog.records)


# ── warn_if_turn_unusable / warn_if_no_turn ───────────────────────────
#
# Neither had a direct test, which is how the early-return below survived.


def test_warn_if_turn_unusable_flags_a_credential_less_entry(caplog):
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        warn_if_turn_unusable([{"urls": ["turn:t.example:3478"]}])
    assert "WITHOUT usable credentials" in caplog.text


def test_warn_if_turn_unusable_quiet_when_credentials_present(caplog):
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        warn_if_turn_unusable(
            [{"urls": ["turn:t.example:3478"], "username": "u", "credential": "c"}],
        )
    assert "WITHOUT usable credentials" not in caplog.text


def test_warn_if_turn_unusable_checks_every_entry_not_just_the_first(caplog):
    """A bad TURN entry behind a good one must still be reported.

    The check used to ``return`` after the first TURN entry. That was
    harmless while the only input was ``build_ice_servers`` (at most one
    TURN entry), but the HA Core pull can deliver several — and that list
    is now passed through these diagnostics on every refresh.
    """
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        warn_if_turn_unusable(
            [
                {"urls": ["stun:stun.example:3478"]},
                {
                    "urls": ["turn:good.example:3478"],
                    "username": "u",
                    "credential": "c",
                },
                {"urls": ["turn:bad.example:3478"]},
            ],
        )
    assert "bad.example" in caplog.text


def test_warn_if_no_turn_fires_on_a_stun_only_list(caplog):
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        warn_if_no_turn([{"urls": ["stun:stun.example:3478"]}])
    assert "no TURN server configured" in caplog.text


def test_warn_if_no_turn_quiet_when_turn_present(caplog):
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        warn_if_no_turn(
            [{"urls": ["turn:t.example:3478"], "username": "u", "credential": "c"}],
        )
    assert "no TURN server configured" not in caplog.text
