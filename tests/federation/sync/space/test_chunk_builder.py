"""Unit tests for :class:`ChunkBuilder` + the sync wire format (§25.6)."""

from __future__ import annotations

from typing import Any

import orjson
import pytest

from social_home.crypto import generate_identity_keypair
from social_home.federation.encoder import FederationEncoder
from social_home.federation.sync.space.exporter import (
    CHUNK_SIZE_BUDGET_BYTES,
    ChunkBuilder,
    SENTINEL_RESOURCE,
    parse_chunk,
    serialise_chunk,
)


class _FakeExporter:
    def __init__(self, resource: str, records: list[dict]) -> None:
        self.resource = resource
        self._records = records

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        return list(self._records)


class _FakeCrypto:
    """Stand-in for :class:`SpaceContentEncryption`.

    Uses the same wire format (``nonce:ct``) but with a non-AEAD XOR
    — enough to verify the encrypt/decrypt round-trip without pulling
    in the real space_keys table.
    """

    def __init__(self, epoch: int = 0) -> None:
        self.epoch = epoch
        self.last_aad: bytes | None = None

    async def encrypt_chunk(
        self,
        *,
        space_id: str,
        sync_id: str,
        plaintext: bytes,
    ) -> tuple[int, str]:
        self.last_aad = f"{space_id}:{self.epoch}:{sync_id}".encode("utf-8")
        import base64

        return self.epoch, base64.urlsafe_b64encode(plaintext).decode("ascii")

    async def decrypt_chunk(
        self,
        *,
        space_id: str,
        epoch: int,
        sync_id: str,
        ciphertext: str,
    ) -> bytes:
        import base64

        return base64.urlsafe_b64decode(ciphertext)


@pytest.fixture
def encoder():
    kp = generate_identity_keypair()
    return FederationEncoder(kp.private_key)


@pytest.fixture
def builder(encoder):
    return ChunkBuilder(encoder=encoder, crypto=_FakeCrypto())


async def test_build_chunks_yields_one_envelope_per_page(builder):
    """Small record set → single chunk containing all records."""
    exporter = _FakeExporter(
        resource="members",
        records=[{"user_id": f"u-{i}", "role": "member"} for i in range(3)],
    )
    chunks = [
        c
        async for c in builder.build_chunks(
            exporter=exporter,
            space_id="sp-1",
            sync_id="sync-x",
            sig_suite="ed25519",
        )
    ]
    assert len(chunks) == 1
    assert chunks[0]["resource"] == "members"
    assert chunks[0]["sync_id"] == "sync-x"
    assert "signatures" in chunks[0]
    assert "ed25519" in chunks[0]["signatures"]


async def test_build_chunks_empty_exporter_yields_nothing(builder):
    exporter = _FakeExporter(resource="members", records=[])
    chunks = [
        c
        async for c in builder.build_chunks(
            exporter=exporter,
            space_id="sp-1",
            sync_id="sync-x",
            sig_suite="ed25519",
        )
    ]
    assert chunks == []


async def test_build_chunks_splits_when_over_budget(builder):
    """A single page that overflows the budget gets halved."""
    # Each record is ~1 KB of JSON — 20 of them blow the 8 KB budget.
    big_records = [{"id": f"{i}", "blob": "x" * 1000} for i in range(20)]
    exporter = _FakeExporter(resource="posts", records=big_records)
    chunks = [
        c
        async for c in builder.build_chunks(
            exporter=exporter,
            space_id="sp-1",
            sync_id="sync-x",
            sig_suite="ed25519",
        )
    ]
    # Must have produced multiple chunks.
    assert len(chunks) > 1
    # Every chunk must stay under the budget.
    for c in chunks:
        assert len(orjson.dumps(c)) <= CHUNK_SIZE_BUDGET_BYTES * 1.2
    # All 20 records accounted for across chunks.
    total_records = sum(c["seq_end"] - c["seq_start"] for c in chunks)
    assert total_records == 20


async def test_build_sentinel_is_signed_not_encrypted(builder):
    sentinel = await builder.build_sentinel(
        space_id="sp-1",
        sync_id="sync-x",
        sig_suite="ed25519",
    )
    assert sentinel["resource"] == SENTINEL_RESOURCE
    assert sentinel["is_last"] is True
    assert "encrypted_payload" not in sentinel
    assert "signatures" in sentinel
    assert "ed25519" in sentinel["signatures"]


async def test_build_chunks_rejects_unknown_resource(builder):
    exporter = _FakeExporter(resource="not_a_real_resource", records=[{"x": 1}])
    with pytest.raises(ValueError, match="not in ALLOWED_RESOURCES"):
        async for _ in builder.build_chunks(
            exporter=exporter,
            space_id="sp-1",
            sync_id="sync-x",
            sig_suite="ed25519",
        ):
            pass


def test_serialise_parse_round_trip():
    envelope = {"sync_id": "x", "resource": "posts", "records": []}
    raw = serialise_chunk(envelope)
    assert parse_chunk(raw) == envelope


def test_parse_chunk_rejects_malformed():
    with pytest.raises(ValueError, match="malformed"):
        parse_chunk(b"not json")
