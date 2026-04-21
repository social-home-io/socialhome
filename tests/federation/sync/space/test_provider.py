"""Unit tests for :class:`SpaceSyncService`."""

from __future__ import annotations

from typing import Any

import orjson
import pytest

from social_home.crypto import generate_identity_keypair
from social_home.federation.encoder import FederationEncoder
from social_home.federation.sync.space.exporter import (
    ChunkBuilder,
    SENTINEL_RESOURCE,
)
from social_home.federation.sync.space.provider import SpaceSyncService


class _FakeExporter:
    def __init__(self, resource: str, records: list[dict]) -> None:
        self.resource = resource
        self._records = records

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        return list(self._records)


class _FakeCrypto:
    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        import base64

        return 0, base64.urlsafe_b64encode(plaintext).decode("ascii")


class _FakeRtc:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_chunk(self, data):
        self.sent.append(data if isinstance(data, bytes) else data.encode())


class _FakeSession:
    def __init__(self, sync_id="sync-x", space_id="sp-1"):
        self.sync_id = sync_id
        self.space_id = space_id
        self.rtc = _FakeRtc()


@pytest.fixture
def encoder():
    return FederationEncoder(generate_identity_keypair().private_key)


@pytest.fixture
def provider(encoder):
    builder = ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())
    exporters = {
        "posts": _FakeExporter("posts", [{"id": "p-1", "author": "u-1"}]),
        "members": _FakeExporter("members", [{"user_id": "u-1", "role": "member"}]),
    }
    return SpaceSyncService(builder=builder, exporters=exporters, sig_suite="ed25519")


async def test_stream_initial_sends_chunks_then_sentinel(provider):
    session = _FakeSession()
    await provider.stream_initial(session)
    # Expect chunks for the two configured exporters + a sentinel.
    assert len(session.rtc.sent) >= 3
    # Parse the last frame — should be the sentinel.
    last = orjson.loads(session.rtc.sent[-1])
    assert last["resource"] == SENTINEL_RESOURCE
    assert last["is_last"] is True


async def test_stream_initial_skips_missing_exporters(provider):
    """Resources without a registered exporter are skipped (this
    fixture only provides 2 of the 11)."""
    session = _FakeSession()
    await provider.stream_initial(session)
    # Chunks are posts + members + sentinel = 3 frames.
    assert len(session.rtc.sent) == 3


async def test_stream_request_more_only_sends_that_resource(provider):
    session = _FakeSession()
    await provider.stream_request_more(session, {"resource": "posts"})
    assert len(session.rtc.sent) == 1
    parsed = orjson.loads(session.rtc.sent[0])
    assert parsed["resource"] == "posts"


async def test_stream_request_more_unknown_resource_is_noop(provider):
    session = _FakeSession()
    await provider.stream_request_more(session, {"resource": "not_real"})
    assert session.rtc.sent == []
