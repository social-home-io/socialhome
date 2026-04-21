"""Tests for GfsFederationService — register, subscribe, publish with real SQLite."""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from social_home.global_server.federation import GfsFederationService
from social_home.global_server.repositories import SqliteGfsFederationRepo


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_keypair() -> tuple[bytes, bytes]:
    """Return (private_seed_bytes, public_key_bytes) for an Ed25519 keypair."""
    sk = Ed25519PrivateKey.generate()
    seed = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pk = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return seed, pk


def _sign(seed: bytes, payload: dict) -> str:
    """Return a URL-safe base64 Ed25519 signature over the canonical JSON of *payload*."""
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    sk = Ed25519PrivateKey.from_private_bytes(seed)
    sig = sk.sign(canonical)
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
async def svc(gfs_db):
    """A GfsFederationService backed by the shared GFS database fixture."""
    repo = SqliteGfsFederationRepo(gfs_db)
    return GfsFederationService(repo)


# ── Tests ──────────────────────────────────────────────────────────────────────


async def test_register_instance_succeeds(svc):
    """register_instance() persists the instance without raising."""
    await svc.register_instance("inst-1", "aa" * 32, "http://example.com/wh")
    spaces = await svc.list_spaces()
    assert isinstance(spaces, list)


async def test_register_instance_idempotent(svc):
    """Calling register_instance() twice with the same id updates the record."""
    await svc.register_instance("inst-dup", "aa" * 32, "http://old.example.com/wh")
    await svc.register_instance("inst-dup", "bb" * 32, "http://new.example.com/wh")


async def test_list_spaces_empty_initially(svc):
    """list_spaces() returns an empty list when no spaces exist."""
    spaces = await svc.list_spaces()
    assert spaces == []


async def test_subscribe_creates_space(svc):
    """subscribe() auto-creates a global_spaces row if needed."""
    await svc.register_instance("inst-a", "aa" * 32, "http://a.example.com/wh")
    await svc.subscribe("inst-a", "space-1")
    spaces = await svc.list_spaces()
    space_ids = [s.space_id for s in spaces]
    assert "space-1" in space_ids


async def test_subscribe_and_unsubscribe(svc):
    """subscribe() then unsubscribe() removes the subscription row."""
    await svc.register_instance("inst-b", "bb" * 32, "http://b.example.com/wh")
    await svc.subscribe("inst-b", "space-unsub")
    await svc.unsubscribe("inst-b", "space-unsub")
    await svc.subscribe("inst-b", "space-unsub")


async def test_subscribe_idempotent(svc):
    """subscribe() called twice for the same (instance, space) does not raise."""
    await svc.register_instance("inst-c", "cc" * 32, "http://c.example.com/wh")
    await svc.subscribe("inst-c", "space-idem")
    await svc.subscribe("inst-c", "space-idem")
    spaces = await svc.list_spaces()
    assert any(s.space_id == "space-idem" for s in spaces)


async def test_publish_unknown_instance_raises_permission_error(svc):
    """publish_event() raises PermissionError for an unregistered from_instance."""
    with pytest.raises(PermissionError, match="Unknown instance"):
        await svc.publish_event("space-1", "post.created", {"text": "hi"}, "ghost-inst")


async def test_publish_with_no_subscribers_returns_empty_list(svc):
    """publish_event() with zero subscribers returns an empty delivered list."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-pub", pk.hex(), "http://pub.example.com/wh")

    payload_dict = {
        "space_id": "space-nosubs",
        "event_type": "post.created",
        "payload": {"text": "hello"},
        "from_instance": "inst-pub",
    }
    sig = _sign(seed, payload_dict)
    delivered = await svc.publish_event(
        "space-nosubs",
        "post.created",
        {"text": "hello"},
        "inst-pub",
        sig,
    )
    assert delivered == []


async def test_publish_invalid_signature_raises_permission_error(svc):
    """publish_event() rejects a bad signature with PermissionError."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-badsig", pk.hex(), "http://badsig.example.com/wh")
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.publish_event(
            "space-sig",
            "post.created",
            {"text": "hi"},
            "inst-badsig",
            signature="invalidsignatureXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
        )


async def test_publish_without_signature_still_relays(svc):
    """publish_event() with an empty signature skips verification and relays."""
    await svc.register_instance("inst-nosig", "dd" * 32, "http://nosig.example.com/wh")
    delivered = await svc.publish_event(
        "space-nosig",
        "ping",
        {},
        "inst-nosig",
        signature="",
    )
    assert isinstance(delivered, list)
