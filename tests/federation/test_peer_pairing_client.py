"""Tests for :class:`PeerPairingClient` — §11 bootstrap outbound."""

from __future__ import annotations

import orjson
import pytest

from socialhome.crypto import generate_identity_keypair, verify_ed25519
from socialhome.domain.federation import FederationEventType
from socialhome.federation.peer_pairing_client import (
    PeerPairingClient,
    _canonical_body_bytes,
    sign_peer_body,
)


class _FakeResponse:
    """Async-context-manager stand-in for ``aiohttp.ClientResponse``."""

    def __init__(self, status: int = 204, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    class _Content:
        def __init__(self, body: bytes) -> None:
            self._body = body

        async def read(self, n: int = -1) -> bytes:
            return self._body[:n] if n > 0 else self._body

    @property
    def content(self) -> "_FakeResponse._Content":
        return _FakeResponse._Content(self._body)

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeClient:
    """Records POST calls; returns queued ``_FakeResponse`` per call."""

    def __init__(self, *, responses: list[_FakeResponse] | None = None) -> None:
        self.calls: list[tuple[str, bytes, dict]] = []
        self.kwargs: list[dict] = []
        self._responses = list(responses or [])

    def post(self, url, *, data, headers, timeout, **kwargs):
        self.calls.append((url, data, dict(headers)))
        self.kwargs.append(kwargs)
        if self._responses:
            return self._responses.pop(0)
        return _FakeResponse(status=204)


# ── helper tests ──


def test_canonical_body_omits_signature_and_sorts_keys():
    body = {"b": 2, "a": 1, "signature": "deadbeef"}
    canonical = _canonical_body_bytes(body)
    decoded = orjson.loads(canonical)
    assert "signature" not in decoded
    assert decoded == {"a": 1, "b": 2}
    # Order-independent: rebuilt body with keys in a different order
    # produces identical canonical bytes.
    body2 = {"a": 1, "b": 2}
    assert _canonical_body_bytes(body2) == canonical


def test_canonical_body_covers_event_type():
    """``event_type`` must be in the signed bytes — otherwise an
    attacker could swap the dispatch marker on the wire without
    invalidating the Ed25519 signature.
    """
    a = _canonical_body_bytes({"event_type": "pairing_peer_accept", "token": "x"})
    b = _canonical_body_bytes({"event_type": "pairing_peer_confirm", "token": "x"})
    assert a != b


def test_sign_peer_body_produces_valid_signature():
    kp = generate_identity_keypair()
    body = {"token": "abc", "value": 42}
    signed = sign_peer_body(body, own_identity_seed=kp.private_key)

    assert "signature" in signed
    sig_bytes = bytes.fromhex(signed["signature"])
    assert verify_ed25519(
        kp.public_key,
        _canonical_body_bytes(body),
        sig_bytes,
    )


# ── client POST tests ──


async def test_send_peer_accept_posts_to_inbox_url_with_event_type():
    kp = generate_identity_keypair()
    fake = _FakeClient(responses=[_FakeResponse(status=200)])

    async def _factory():
        return fake

    client = PeerPairingClient(
        own_identity_seed=kp.private_key,
        client_factory=_factory,
    )
    inbox_url = "https://peer.example/federation/inbox/wh"
    result = await client.send_peer_accept(
        peer_inbox_url=inbox_url,
        body={"token": "abc", "verification_code": "123456"},
    )

    assert result.ok is True
    assert result.status_code == 200
    url, data, headers = fake.calls[0]
    # Posts directly to the peer's inbox URL — no path rewriting.
    assert url == inbox_url
    assert headers["Content-Type"] == "application/json"
    signed = orjson.loads(data)
    assert signed["event_type"] == FederationEventType.PAIRING_PEER_ACCEPT.value
    assert signed["token"] == "abc"
    # Signature is appended and verifies over the body INCLUDING event_type.
    sig = bytes.fromhex(signed["signature"])
    canonical = _canonical_body_bytes(
        {
            "event_type": FederationEventType.PAIRING_PEER_ACCEPT.value,
            "token": "abc",
            "verification_code": "123456",
        },
    )
    assert verify_ed25519(kp.public_key, canonical, sig)


async def test_send_peer_confirm_posts_to_inbox_url_with_event_type():
    kp = generate_identity_keypair()
    fake = _FakeClient(responses=[_FakeResponse(status=200)])

    async def _factory():
        return fake

    client = PeerPairingClient(
        own_identity_seed=kp.private_key,
        client_factory=_factory,
    )
    inbox_url = "https://peer.example/federation/inbox/wh"
    result = await client.send_peer_confirm(
        peer_inbox_url=inbox_url,
        body={"token": "abc", "instance_id": "iid-A"},
    )

    assert result.ok is True
    url, data, _ = fake.calls[0]
    assert url == inbox_url
    signed = orjson.loads(data)
    assert signed["event_type"] == FederationEventType.PAIRING_PEER_CONFIRM.value


async def test_send_reports_non_2xx_as_failure():
    kp = generate_identity_keypair()
    fake = _FakeClient(responses=[_FakeResponse(status=403, body=b"nope")])

    async def _factory():
        return fake

    client = PeerPairingClient(
        own_identity_seed=kp.private_key,
        client_factory=_factory,
    )
    result = await client.send_peer_accept(
        peer_inbox_url="https://peer/federation/inbox/wh",
        body={"token": "x"},
    )
    assert result.ok is False
    assert result.status_code == 403


async def test_send_reports_network_error_as_failure():
    kp = generate_identity_keypair()

    class _ErrorClient:
        def post(self, *args, **kwargs):
            raise RuntimeError("boom")

    async def _factory():
        return _ErrorClient()

    client = PeerPairingClient(
        own_identity_seed=kp.private_key,
        client_factory=_factory,
    )
    result = await client.send_peer_accept(
        peer_inbox_url="https://peer/federation/inbox/wh",
        body={"token": "x"},
    )
    assert result.ok is False
    assert result.status_code is None
    assert result.error == "boom"


async def test_send_rejects_empty_inbox_url():
    kp = generate_identity_keypair()
    fake = _FakeClient()

    async def _factory():
        return fake

    client = PeerPairingClient(
        own_identity_seed=kp.private_key,
        client_factory=_factory,
    )
    result = await client.send_peer_accept(peer_inbox_url="", body={"token": "x"})
    assert result.ok is False
    assert result.status_code is None
    # No POST was issued.
    assert fake.calls == []


# ── inbox URL guard ──


def _client_with(fake: _FakeClient) -> PeerPairingClient:
    async def _factory():
        return fake

    return PeerPairingClient(
        own_identity_seed=generate_identity_keypair().private_key,
        client_factory=_factory,
    )


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "ftp://peer.example/federation/inbox/wh",
        "javascript:alert(1)",
        "https:///federation/inbox/wh",
        "https://user:pw@peer.example/federation/inbox/wh",
        "https://user@peer.example/federation/inbox/wh",
        "https://peer.example/federation/inbox/wh\r\nX: y",
    ],
)
async def test_send_refuses_invalid_inbox_url_without_posting(bad_url):
    fake = _FakeClient()
    client = _client_with(fake)
    accept = await client.send_peer_accept(peer_inbox_url=bad_url, body={"t": "x"})
    confirm = await client.send_peer_confirm(peer_inbox_url=bad_url, body={"t": "x"})
    for result in (accept, confirm):
        assert result.ok is False
        assert result.status_code is None
        assert result.error is not None
    # Nothing left the household.
    assert fake.calls == []


@pytest.mark.parametrize(
    "good_url",
    [
        "https://peer.example/federation/inbox/wh",
        # LAN / loopback plain http stays supported (demo harness + home LAN).
        "http://127.0.0.1:18001/federation/inbox/wh",
        "http://192.168.1.20:8123/api/socialhome/inbox/wh",
        "http://homeassistant.local:8123/api/socialhome/inbox/wh",
    ],
)
async def test_send_posts_to_valid_inbox_url(good_url):
    fake = _FakeClient()
    client = _client_with(fake)
    result = await client.send_peer_accept(peer_inbox_url=good_url, body={"t": "x"})
    assert result.ok is True
    assert fake.calls[0][0] == good_url


async def test_send_does_not_follow_redirects():
    """A redirect would hand the signed body to a target that never went
    through the URL check — the POST must pin ``allow_redirects=False``."""
    fake = _FakeClient(responses=[_FakeResponse(status=307)])
    client = _client_with(fake)
    result = await client.send_peer_confirm(
        peer_inbox_url="https://peer.example/federation/inbox/wh",
        body={"t": "x"},
    )
    assert fake.kwargs[0].get("allow_redirects") is False
    # A 3xx is not success.
    assert result.ok is False
    assert result.status_code == 307
