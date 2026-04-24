"""Verify the existing concrete classes satisfy the Strategy protocols."""

from __future__ import annotations

from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.strategies import (
    EncryptionStrategy,
    TransportStrategy,
)
from socialhome.federation.transport import HttpsInboxTransport


# ─── Transport ───────────────────────────────────────────────────────────


async def _dummy_client_factory():
    """Minimal aiohttp-like stub. We never call .post — we just need an
    object so isinstance() against the runtime-checkable protocol works
    at import time."""
    return object()


def test_https_inbox_transport_satisfies_protocol():
    wh = HttpsInboxTransport(client_factory=_dummy_client_factory)
    assert isinstance(wh, TransportStrategy)


# ─── Encryption ──────────────────────────────────────────────────────────


def test_federation_encoder_satisfies_protocol():
    enc = FederationEncoder(own_identity_seed=b"\x00" * 32)
    assert isinstance(enc, EncryptionStrategy)
