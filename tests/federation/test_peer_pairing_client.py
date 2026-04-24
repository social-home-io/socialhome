"""Tests for :class:`PeerPairingClient` — §11 bootstrap outbound."""

from __future__ import annotations

import orjson

from socialhome.crypto import generate_identity_keypair, verify_ed25519
from socialhome.federation.peer_pairing_client import (
    PeerPairingClient,
    _canonical_body_bytes,
    _derive_peer_base,
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
        self._responses = list(responses or [])

    def post(self, url, *, data, headers, timeout):
        self.calls.append((url, data, dict(headers)))
        if self._responses:
            return self._responses.pop(0)
        return _FakeResponse(status=204)


# ── helper tests ──


def test_derive_peer_base_strips_path():
    assert (
        _derive_peer_base("https://peer.example/federation/inbox/xyz")
        == "https://peer.example"
    )


def test_derive_peer_base_preserves_port():
    assert (
        _derive_peer_base("http://peer.example:8080/federation/inbox/xyz")
        == "http://peer.example:8080"
    )


def test_derive_peer_base_raises_for_malformed():
    import pytest

    with pytest.raises(ValueError):
        _derive_peer_base("not-a-url")


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


async def test_send_peer_accept_signs_and_posts():
    kp = generate_identity_keypair()
    fake = _FakeClient(responses=[_FakeResponse(status=204)])

    async def _factory():
        return fake

    client = PeerPairingClient(
        own_identity_seed=kp.private_key,
        client_factory=_factory,
    )
    result = await client.send_peer_accept(
        peer_inbox_url="https://peer.example/federation/inbox/wh",
        body={"token": "abc", "verification_code": "123456"},
    )

    assert result.ok is True
    assert result.status_code == 204
    url, data, headers = fake.calls[0]
    assert url == "https://peer.example/api/pairing/peer-accept"
    assert headers["Content-Type"] == "application/json"
    signed = orjson.loads(data)
    assert signed["token"] == "abc"
    # Signature is appended and verifies.
    sig = bytes.fromhex(signed["signature"])
    canonical = _canonical_body_bytes(
        {"token": "abc", "verification_code": "123456"},
    )
    assert verify_ed25519(kp.public_key, canonical, sig)


async def test_send_peer_confirm_posts_to_confirm_path():
    kp = generate_identity_keypair()
    fake = _FakeClient(responses=[_FakeResponse(status=204)])

    async def _factory():
        return fake

    client = PeerPairingClient(
        own_identity_seed=kp.private_key,
        client_factory=_factory,
    )
    result = await client.send_peer_confirm(
        peer_inbox_url="https://peer.example/federation/inbox/wh",
        body={"token": "abc", "instance_id": "iid-A"},
    )

    assert result.ok is True
    url, _, _ = fake.calls[0]
    assert url == "https://peer.example/api/pairing/peer-confirm"


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


async def test_send_reports_bad_url_as_failure():
    kp = generate_identity_keypair()
    fake = _FakeClient()

    async def _factory():
        return fake

    client = PeerPairingClient(
        own_identity_seed=kp.private_key,
        client_factory=_factory,
    )
    result = await client.send_peer_accept(
        peer_inbox_url="not-a-url",
        body={"token": "x"},
    )
    assert result.ok is False
    assert result.status_code is None
    # No POST was issued.
    assert fake.calls == []
