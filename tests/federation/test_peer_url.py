"""Tests for :mod:`socialhome.federation.peer_url` — household URL checks."""

from __future__ import annotations

import pytest

from socialhome.federation.peer_url import (
    InvalidPeerUrlError,
    is_private_host,
    validate_peer_url,
)


# ── is_private_host ──


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "localhost.",
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.20",
        "172.16.0.5",
        "[::1]",
        "::1",
        "fe80::1",
        "fd00::1",
        "169.254.10.10",
    ],
)
def test_is_private_host_true_for_loopback_and_lan(host):
    assert is_private_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["example.com", "8.8.8.8", "2001:4860:4860::8888", "homeassistant.local", ""],
)
def test_is_private_host_false_for_public_or_names(host):
    # DNS names are never resolved — they count as public.
    assert is_private_host(host) is False


# ── validate_peer_url: structural rules (every household URL) ──


@pytest.mark.parametrize(
    "url",
    [
        "https://peer.example/federation/inbox/abc",
        "https://peer.example:8443/federation/inbox/abc",
        "http://127.0.0.1:18001/federation/inbox/abc",
        "http://localhost:8080/federation/inbox/abc",
        "http://192.168.1.20:8123/api/socialhome/inbox/abc",
        "http://[::1]:8080/federation/inbox/abc",
        # LAN names (mDNS / bare hostnames) are legitimate household
        # addresses today — the admin's external-URL setting accepts them.
        "http://homeassistant.local:8123/api/socialhome/inbox/abc",
        "HTTPS://Peer.Example/federation/inbox/abc",
    ],
)
def test_valid_household_urls_accepted(url):
    assert validate_peer_url(url, field="inbox_url") == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://peer.example/inbox",
        "javascript:alert(1)",
        "gopher://peer.example/",
        "data:text/plain,hi",
        "ws://peer.example/inbox",
        "peer.example/federation/inbox",
        "//peer.example/federation/inbox",
    ],
)
def test_non_http_schemes_rejected(url):
    with pytest.raises(InvalidPeerUrlError, match="http"):
        validate_peer_url(url, field="inbox_url")


@pytest.mark.parametrize(
    "url",
    ["http://", "https://", "https:///federation/inbox", "http://:8080/inbox"],
)
def test_missing_host_rejected(url):
    with pytest.raises(InvalidPeerUrlError, match="host"):
        validate_peer_url(url, field="inbox_url")


@pytest.mark.parametrize(
    "url",
    [
        "https://user@peer.example/federation/inbox",
        "https://user:pw@peer.example/federation/inbox",
        "https://:pw@peer.example/federation/inbox",
        "http://peer.example@127.0.0.1:8080/federation/inbox",
    ],
)
def test_userinfo_rejected(url):
    with pytest.raises(InvalidPeerUrlError, match="credentials"):
        validate_peer_url(url, field="inbox_url")


@pytest.mark.parametrize(
    "url",
    [
        "https://peer.example/in box",
        " https://peer.example/inbox",
        "https://peer.example/inbox\n",
        "https://peer.example/inbox\r\nX-Evil: 1",
        "https://peer.example/\x00",
    ],
)
def test_whitespace_and_control_chars_rejected(url):
    with pytest.raises(InvalidPeerUrlError):
        validate_peer_url(url, field="inbox_url")


def test_invalid_port_rejected():
    with pytest.raises(InvalidPeerUrlError, match="port"):
        validate_peer_url("https://peer.example:99999/inbox", field="inbox_url")


def test_unparseable_url_rejected():
    with pytest.raises(InvalidPeerUrlError, match="parsed"):
        validate_peer_url("http://[::1/federation/inbox", field="inbox_url")


def test_overlong_url_rejected():
    url = "https://peer.example/" + "a" * 4096
    with pytest.raises(InvalidPeerUrlError, match="long"):
        validate_peer_url(url, field="inbox_url")


@pytest.mark.parametrize("value", [None, 42, b"https://peer.example/", ""])
def test_non_string_or_empty_rejected(value):
    with pytest.raises(InvalidPeerUrlError):
        validate_peer_url(value, field="inbox_url")


def test_error_is_value_error_and_names_field():
    with pytest.raises(ValueError) as info:
        validate_peer_url("ftp://x.example/", field="c_inbox_url")
    assert isinstance(info.value, InvalidPeerUrlError)
    assert info.value.field == "c_inbox_url"
    assert "c_inbox_url" in str(info.value)


# ── validate_peer_url: TLS-required mode (connection-server URLs) ──


@pytest.mark.parametrize(
    "url",
    [
        "https://gfs.example",
        "http://127.0.0.1:18100",
        "http://localhost:18100",
        "http://10.0.0.5:8080",
        "http://[fd00::5]:8080",
    ],
)
def test_tls_mode_accepts_https_and_private_http(url):
    assert validate_peer_url(url, field="gfs_url", require_tls_unless_private=True)


@pytest.mark.parametrize(
    "url",
    ["http://gfs.example", "http://8.8.8.8", "http://homeassistant.local:8123"],
)
def test_tls_mode_rejects_public_plain_http(url):
    with pytest.raises(InvalidPeerUrlError, match="https"):
        validate_peer_url(url, field="gfs_url", require_tls_unless_private=True)


def test_tls_mode_still_applies_structural_rules():
    with pytest.raises(InvalidPeerUrlError, match="credentials"):
        validate_peer_url(
            "https://u:p@gfs.example",
            field="gfs_url",
            require_tls_unless_private=True,
        )
