"""Tests for GfsFederationService — register, subscribe, publish with real SQLite."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from socialhome.global_server import federation as federation_mod
from socialhome.global_server.domain import GfsSubscriber
from socialhome.global_server.federation import GfsFederationService
from socialhome.global_server.repositories import SqliteGfsFederationRepo


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


async def test_register_instance_persists_keywrap_pubkey(svc):
    """The key-wrap pubkey + KEM suite ride registration and round-trip."""
    await svc.register_instance(
        "inst-kw",
        "aa" * 32,
        "http://kw.example.com/wh",
        keywrap_public_key="cc" * 32,
        kem_suite="x25519",
    )
    inst = await svc._repo.get_instance("inst-kw")
    assert inst is not None
    assert inst.keywrap_public_key == "cc" * 32
    assert inst.kem_suite == "x25519"


async def test_register_instance_without_keywrap_defaults_empty(svc):
    """An older HFS that ships no key-wrap pubkey → empty fields, no crash."""
    await svc.register_instance("inst-old", "aa" * 32, "http://old.example.com/wh")
    inst = await svc._repo.get_instance("inst-old")
    assert inst is not None
    assert inst.keywrap_public_key == ""
    assert inst.kem_suite == ""
    assert inst.keywrap_sig == ""


async def test_register_instance_persists_keywrap_sig(svc):
    """The keywrap self-signature rides registration and round-trips."""
    await svc.register_instance(
        "inst-kws",
        "aa" * 32,
        "http://kws.example.com/wh",
        keywrap_public_key="cc" * 32,
        kem_suite="x25519",
        keywrap_sig="c2lnbmF0dXJl",
    )
    inst = await svc._repo.get_instance("inst-kws")
    assert inst is not None
    assert inst.keywrap_sig == "c2lnbmF0dXJl"


async def test_list_spaces_empty_initially(svc):
    """list_spaces() returns an empty list when no spaces exist."""
    spaces = await svc.list_spaces()
    assert spaces == []


async def _publish_known_space(
    svc,
    seed: bytes,
    *,
    owning_instance: str,
    space_id: str,
    identity_public_key: str = "",
):
    """Publish a minimal active space so a subscribe target exists.

    ``identity_public_key`` (hex) is the space's Ed25519 authority verify key;
    when supplied it is TOFU-pinned on the GFS row so authority-signed relays
    can be verified. Empty (default) mirrors an older HFS that ships none.
    """
    body = {
        "space_id": space_id,
        "owning_instance": owning_instance,
        "name": "Known",
        "description": "",
        "about_markdown": "",
        "cover_url": "",
        "icon_url": "",
        "min_age": 0,
        "category": "general",
        "accent_color": "#D2542A",
        "primary_color": "#D2542A",
        "identity_public_key": identity_public_key,
    }
    sig = _sign(seed, body)
    await svc.publish_space(
        space_id=space_id,
        owning_instance=owning_instance,
        name="Known",
        signature=sig,
        identity_public_key=identity_public_key,
    )


async def test_subscribe_self_signed_known_space_succeeds(svc):
    """subscribe() accepts a self-signed request for an already-published space."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-a", pk.hex(), "http://a.example.com/wh", auto_accept=True
    )
    await _publish_known_space(svc, seed, owning_instance="inst-a", space_id="space-1")
    ts = _now_iso()
    sig = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-a",
            "space_id": "space-1",
            "ts": ts,
        },
    )
    await svc.subscribe("inst-a", "space-1", ts, sig)
    subs = await svc._repo.list_subscribers("space-1")
    assert any(s.instance_id == "inst-a" for s in subs)


async def test_subscribe_unknown_instance_rejected(svc):
    """An instance the GFS never registered cannot subscribe — PermissionError."""
    ts = _now_iso()
    with pytest.raises(PermissionError, match="Unknown instance"):
        await svc.subscribe("ghost-inst", "space-1", ts, "AAAA")


async def test_subscribe_no_signature_rejected(svc):
    """The signature is mandatory — an empty signature is a PermissionError."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-nos", pk.hex(), "http://n.example.com/wh")
    ts = _now_iso()
    with pytest.raises(PermissionError):
        await svc.subscribe("inst-nos", "space-1", ts, "")


async def test_subscribe_signature_from_other_instance_rejected(svc):
    """A signature produced by a different key fails verification."""
    seed_a, pk_a = _make_keypair()
    other_seed, _ = _make_keypair()
    await svc.register_instance(
        "inst-x", pk_a.hex(), "http://x.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed_a, owning_instance="inst-x", space_id="space-x"
    )
    ts = _now_iso()
    # Sign with the wrong key for inst-x's own id.
    sig = _sign(
        other_seed,
        {
            "action": "subscribe",
            "instance_id": "inst-x",
            "space_id": "space-x",
            "ts": ts,
        },
    )
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.subscribe("inst-x", "space-x", ts, sig)


async def test_subscribe_cannot_sign_for_another_instance(svc):
    """A caller can only subscribe *itself*: signing inst-b's body with
    inst-a's key fails because the signature is checked against the
    instance_id in the body (inst-b's registered key)."""
    seed_a, pk_a = _make_keypair()
    seed_b, pk_b = _make_keypair()
    await svc.register_instance(
        "inst-a2", pk_a.hex(), "http://a.example.com/wh", auto_accept=True
    )
    await svc.register_instance("inst-b2", pk_b.hex(), "http://b.example.com/wh")
    await _publish_known_space(
        svc, seed_a, owning_instance="inst-a2", space_id="space-ab"
    )
    ts = _now_iso()
    # inst-a2 tries to subscribe inst-b2 (claims instance_id=inst-b2) — must
    # sign as inst-b2 to pass, which it can't.
    sig = _sign(
        seed_a,
        {
            "action": "subscribe",
            "instance_id": "inst-b2",
            "space_id": "space-ab",
            "ts": ts,
        },
    )
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.subscribe("inst-b2", "space-ab", ts, sig)


async def test_subscribe_stale_timestamp_rejected(svc):
    """A ts outside the ±300 s replay window is rejected."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-stale-sub", pk.hex(), "http://s.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="inst-stale-sub", space_id="space-stale"
    )
    ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    sig = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-stale-sub",
            "space_id": "space-stale",
            "ts": ts,
        },
    )
    with pytest.raises(PermissionError, match="Stale timestamp"):
        await svc.subscribe("inst-stale-sub", "space-stale", ts, sig)


async def test_subscribe_unknown_space_rejected(svc):
    """A subscribe to a space the GFS has never seen is rejected — no
    auto-create of a pending row from an unauthenticated demand signal."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-d", pk.hex(), "http://d.example.com/wh")
    ts = _now_iso()
    sig = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-d",
            "space_id": "space-ghost",
            "ts": ts,
        },
    )
    with pytest.raises(PermissionError, match="not published"):
        await svc.subscribe("inst-d", "space-ghost", ts, sig)
    # And no row was minted.
    assert await svc.get_space("space-ghost") is None


async def test_subscribe_and_unsubscribe(svc):
    """subscribe() then unsubscribe() removes the subscription row."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-b", pk.hex(), "http://b.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="inst-b", space_id="space-unsub"
    )
    ts = _now_iso()
    sig = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-b",
            "space_id": "space-unsub",
            "ts": ts,
        },
    )
    await svc.subscribe("inst-b", "space-unsub", ts, sig)
    ts_u = _now_iso()
    sig_u = _sign(
        seed,
        {
            "action": "unsubscribe",
            "instance_id": "inst-b",
            "space_id": "space-unsub",
            "ts": ts_u,
        },
    )
    await svc.unsubscribe("inst-b", "space-unsub", ts_u, sig_u)
    ts2 = _now_iso()
    sig2 = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-b",
            "space_id": "space-unsub",
            "ts": ts2,
        },
    )
    await svc.subscribe("inst-b", "space-unsub", ts2, sig2)


async def test_subscribe_idempotent(svc):
    """subscribe() called twice for the same (instance, space) does not raise."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-c", pk.hex(), "http://c.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="inst-c", space_id="space-idem"
    )
    ts = _now_iso()
    sig = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-c",
            "space_id": "space-idem",
            "ts": ts,
        },
    )
    await svc.subscribe("inst-c", "space-idem", ts, sig)
    ts2 = _now_iso()
    sig2 = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-c",
            "space_id": "space-idem",
            "ts": ts2,
        },
    )
    await svc.subscribe("inst-c", "space-idem", ts2, sig2)
    subs = await svc._repo.list_subscribers("space-idem")
    assert sum(1 for s in subs if s.instance_id == "inst-c") == 1


async def test_publish_unknown_instance_raises_permission_error(svc):
    """A legacy ``from_instance`` the GFS never registered is rejected — and
    the error never echoes the identity back (no existence oracle)."""
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.publish_event("space-1", "post.created", {"text": "hi"}, "ghost-inst")


async def test_publish_with_no_subscribers_returns_empty_list(svc):
    """publish_event() with zero subscribers returns an empty delivered list."""
    seed, pk = _make_keypair()
    space_seed, space_pk = _make_keypair()
    await svc.register_instance(
        "inst-pub", pk.hex(), "http://pub.example.com/wh", auto_accept=True
    )
    # The space must exist and carry a pinned authority key.
    await _publish_known_space(
        svc,
        seed,
        owning_instance="inst-pub",
        space_id="space-nosubs",
        identity_public_key=space_pk.hex(),
    )
    payload = {"ciphertext": "opaque"}
    payload.update(
        _sign_authority(space_seed, space_id="space-nosubs", payload=payload)
    )
    delivered = await svc.publish_event(
        "space-nosubs",
        "space_post_public",
        payload,
    )
    assert delivered == []


async def test_publish_event_unknown_space_rejected(svc):
    """publish_event() rejects an event for a space the GFS never saw — no
    auto-creation of an ownership row from an event (mirrors subscribe)."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-unk", pk.hex(), "http://unk.example.com/wh", auto_accept=True
    )
    sig = _sign(
        seed,
        {
            "space_id": "space-never",
            "event_type": "post.created",
            "payload": {"text": "hi"},
            "from_instance": "inst-unk",
        },
    )
    with pytest.raises(PermissionError, match="not published"):
        await svc.publish_event(
            "space-never", "post.created", {"text": "hi"}, "inst-unk", sig
        )


async def test_publish_event_from_non_owner_rejected(svc):
    """A registered peer that holds no space authority is rejected even with a
    valid household transport signature — the relay needs the space-authority
    signature, and ``post.created`` isn't even a relayable event type."""
    owner_seed, owner_pk = _make_keypair()
    other_seed, other_pk = _make_keypair()
    await svc.register_instance(
        "owner-e", owner_pk.hex(), "http://owner.example.com/wh", auto_accept=True
    )
    await svc.register_instance(
        "other-e", other_pk.hex(), "http://other.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, owner_seed, owning_instance="owner-e", space_id="space-owned"
    )
    # other-e signs a valid event for owner-e's space — must be rejected.
    sig = _sign(
        other_seed,
        {
            "space_id": "space-owned",
            "event_type": "post.created",
            "payload": {"text": "hi"},
            "from_instance": "other-e",
        },
    )
    with pytest.raises(PermissionError, match="event types"):
        await svc.publish_event(
            "space-owned", "post.created", {"text": "hi"}, "other-e", sig
        )


async def test_publish_event_from_owner_arbitrary_type_rejected(svc):
    """The owner path is GONE: the owning instance can no longer relay an
    arbitrary event type for its own space on its household signature alone."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "owner-ok", pk.hex(), "http://ok.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="owner-ok", space_id="space-ok"
    )
    sig = _sign(
        seed,
        {
            "space_id": "space-ok",
            "event_type": "post.created",
            "payload": {"text": "hi"},
            "from_instance": "owner-ok",
        },
    )
    with pytest.raises(PermissionError, match="event types"):
        await svc.publish_event(
            "space-ok", "post.created", {"text": "hi"}, "owner-ok", sig
        )


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


async def test_publish_event_empty_signature_rejected(svc):
    """publish_event() now REQUIRES a signature — empty is a PermissionError."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-nosig", pk.hex(), "http://nosig.example.com/wh")
    with pytest.raises(PermissionError):
        await svc.publish_event(
            "space-nosig",
            "ping",
            {},
            "inst-nosig",
            signature="",
        )


async def test_publish_event_malformed_signature_rejected(svc):
    """A signature that isn't valid base64url is rejected with PermissionError."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-mal", pk.hex(), "http://mal.example.com/wh")
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.publish_event(
            "space-mal",
            "ping",
            {},
            "inst-mal",
            signature="!!!not-base64!!!",
        )


# ── publish_space (signed space-metadata publish) ───────────────────────────────


def _publish_space_args(
    owning_instance: str, space_id: str, *, identity_public_key: str = ""
) -> dict:
    return {
        "space_id": space_id,
        "owning_instance": owning_instance,
        "name": "My Space",
        "description": "",
        "about_markdown": "",
        "cover_url": "",
        "icon_url": "",
        "min_age": 0,
        "category": "general",
        "accent_color": "#D2542A",
        "primary_color": "#D2542A",
        "identity_public_key": identity_public_key,
    }


async def test_publish_space_valid_signature_succeeds(svc):
    """publish_space() persists the row when the signature verifies."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-ps", pk.hex(), "http://ps.example.com/wh", auto_accept=True
    )
    sig = _sign(seed, _publish_space_args("inst-ps", "space-ps"))
    space = await svc.publish_space(
        space_id="space-ps",
        owning_instance="inst-ps",
        name="My Space",
        signature=sig,
    )
    assert space.space_id == "space-ps"
    assert await svc.get_space("space-ps") is not None


async def test_publish_space_empty_signature_rejected(svc):
    """publish_space() now REQUIRES a signature — empty is a PermissionError."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-ps2", pk.hex(), "http://ps2.example.com/wh")
    with pytest.raises(PermissionError):
        await svc.publish_space(
            space_id="space-ps2",
            owning_instance="inst-ps2",
            name="My Space",
            signature="",
        )


async def test_publish_space_invalid_signature_rejected(svc):
    """A forged signature is rejected with PermissionError."""
    _, pk = _make_keypair()
    other_seed, _ = _make_keypair()
    await svc.register_instance("inst-ps3", pk.hex(), "http://ps3.example.com/wh")
    sig = _sign(other_seed, _publish_space_args("inst-ps3", "space-ps3"))
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.publish_space(
            space_id="space-ps3",
            owning_instance="inst-ps3",
            name="My Space",
            signature=sig,
        )


async def test_publish_space_malformed_signature_rejected(svc):
    """A signature that isn't valid base64url is rejected with PermissionError."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-ps4", pk.hex(), "http://ps4.example.com/wh")
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.publish_space(
            space_id="space-ps4",
            owning_instance="inst-ps4",
            name="My Space",
            signature="!!!not-base64!!!",
        )


async def test_publish_space_cannot_hijack_another_owners_space(svc):
    """A registered peer can't seize a space_id another instance already owns.

    space_id is a public, owner-chosen UUID (it travels in discovery links),
    so a malicious-but-registered peer that learns it could otherwise re-publish
    the row with owning_instance=itself — validly signed by its OWN key — and
    hijack the listing. The owner is immutable after first publish: a publish
    whose owning_instance differs from the stored owner is a PermissionError,
    even with a signature valid for the attacker's own key.
    """
    owner_seed, owner_pk = _make_keypair()
    attacker_seed, attacker_pk = _make_keypair()
    await svc.register_instance(
        "owner-inst", owner_pk.hex(), "http://owner.example.com/wh", auto_accept=True
    )
    await svc.register_instance(
        "attacker-inst",
        attacker_pk.hex(),
        "http://attacker.example.com/wh",
        auto_accept=True,
    )
    # Legit owner establishes the space.
    sig = _sign(owner_seed, _publish_space_args("owner-inst", "shared-space"))
    await svc.publish_space(
        space_id="shared-space",
        owning_instance="owner-inst",
        name="My Space",
        signature=sig,
    )
    # Attacker signs a publish for the SAME space_id with itself as owner,
    # validly signed by its own key — must still be rejected.
    forged = _sign(attacker_seed, _publish_space_args("attacker-inst", "shared-space"))
    with pytest.raises(PermissionError, match="owned by another instance"):
        await svc.publish_space(
            space_id="shared-space",
            owning_instance="attacker-inst",
            name="My Space",
            signature=forged,
        )
    # The original owner is untouched.
    row = await svc.get_space("shared-space")
    assert row is not None
    assert row.owning_instance == "owner-inst"


async def test_publish_space_owner_can_refresh_own_space(svc):
    """The established owner can re-publish its own space (idempotent refresh)."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "owner2-inst", pk.hex(), "http://owner2.example.com/wh", auto_accept=True
    )
    sig = _sign(seed, _publish_space_args("owner2-inst", "refresh-space"))
    await svc.publish_space(
        space_id="refresh-space",
        owning_instance="owner2-inst",
        name="My Space",
        signature=sig,
    )
    # Same owner publishes again — allowed.
    sig2 = _sign(seed, _publish_space_args("owner2-inst", "refresh-space"))
    await svc.publish_space(
        space_id="refresh-space",
        owning_instance="owner2-inst",
        name="My Space",
        signature=sig2,
    )
    row = await svc.get_space("refresh-space")
    assert row is not None and row.owning_instance == "owner2-inst"


# ── publish_space TOFU-pin of identity_public_key (Phase 5a) ────────────────────


async def test_publish_space_pins_identity_public_key_on_first_publish(svc):
    """The space's Ed25519 authority pubkey is TOFU-pinned on first publish."""
    seed, pk = _make_keypair()
    _, space_pk = _make_keypair()
    space_pk_hex = space_pk.hex()
    await svc.register_instance(
        "inst-pin", pk.hex(), "http://pin.example.com/wh", auto_accept=True
    )
    args = _publish_space_args(
        "inst-pin", "space-pin", identity_public_key=space_pk_hex
    )
    sig = _sign(seed, args)
    await svc.publish_space(
        space_id="space-pin",
        owning_instance="inst-pin",
        name="My Space",
        identity_public_key=space_pk_hex,
        signature=sig,
    )
    row = await svc.get_space("space-pin")
    assert row is not None
    assert row.identity_public_key == space_pk_hex


async def test_publish_space_does_not_change_pinned_pubkey(svc):
    """Once pinned, a later publish with a DIFFERENT pubkey does not change it."""
    seed, pk = _make_keypair()
    _, space_pk = _make_keypair()
    _, other_space_pk = _make_keypair()
    pinned_hex = space_pk.hex()
    other_hex = other_space_pk.hex()
    await svc.register_instance(
        "inst-pin2", pk.hex(), "http://pin2.example.com/wh", auto_accept=True
    )
    args1 = _publish_space_args(
        "inst-pin2", "space-pin2", identity_public_key=pinned_hex
    )
    await svc.publish_space(
        space_id="space-pin2",
        owning_instance="inst-pin2",
        name="My Space",
        identity_public_key=pinned_hex,
        signature=_sign(seed, args1),
    )
    # Same owner re-publishes with a DIFFERENT pubkey — the pin must hold.
    args2 = _publish_space_args(
        "inst-pin2", "space-pin2", identity_public_key=other_hex
    )
    await svc.publish_space(
        space_id="space-pin2",
        owning_instance="inst-pin2",
        name="My Space",
        identity_public_key=other_hex,
        signature=_sign(seed, args2),
    )
    row = await svc.get_space("space-pin2")
    assert row is not None
    assert row.identity_public_key == pinned_hex


async def test_publish_space_same_pubkey_refresh_ok(svc):
    """Re-publishing with the SAME pinned pubkey is fine (idempotent)."""
    seed, pk = _make_keypair()
    _, space_pk = _make_keypair()
    pinned_hex = space_pk.hex()
    await svc.register_instance(
        "inst-pin3", pk.hex(), "http://pin3.example.com/wh", auto_accept=True
    )
    args = _publish_space_args(
        "inst-pin3", "space-pin3", identity_public_key=pinned_hex
    )
    await svc.publish_space(
        space_id="space-pin3",
        owning_instance="inst-pin3",
        name="My Space",
        identity_public_key=pinned_hex,
        signature=_sign(seed, args),
    )
    await svc.publish_space(
        space_id="space-pin3",
        owning_instance="inst-pin3",
        name="My Space",
        identity_public_key=pinned_hex,
        signature=_sign(seed, args),
    )
    row = await svc.get_space("space-pin3")
    assert row is not None
    assert row.identity_public_key == pinned_hex


# ── publish_event authorized by a SPACE-AUTHORITY signature (Phase 5a) ──────────


class _RecordingWsRegistry:
    """A ws-registry stub whose ``send`` always succeeds, so fan-out marks
    delivery without a real network hop — lets a relay-authorized test assert
    the event actually reached the subscriber set."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(self, instance_id: str, frame: dict) -> bool:
        self.sent.append((instance_id, frame))
        return True


class _SlowWsRegistry:
    """A ws-registry stub whose ``send`` sleeps *delay* seconds then succeeds —
    stands in for a subscriber that accepts the push only very slowly."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.sent: list[str] = []

    async def send(self, instance_id: str, frame: dict) -> bool:
        await asyncio.sleep(self.delay)
        self.sent.append(instance_id)
        return True


def _sign_authority(space_seed: bytes, *, space_id: str, payload: dict) -> dict:
    """Produce the authority-sig wire fields for a space-content relay payload."""
    from socialhome.services.space_crypto_service import (
        AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        sign_authority_event,
    )

    return sign_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        space_id=space_id,
        payload=payload,
        space_seed=space_seed,
    )


def _sign_authority_subscribers_query(
    space_seed: bytes, *, space_id: str, ts: str
) -> dict:
    """Authority-sig wire fields for a subscribers-list query."""
    from socialhome.authority_sig import AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY
    from socialhome.services.space_crypto_service import sign_authority_event

    return sign_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
        space_id=space_id,
        payload={"space_id": space_id, "ts": ts},
        space_seed=space_seed,
    )


async def _setup_authority_relay(svc, *, space_id: str):
    """Owner publishes a space pinning a SPACE authority pubkey; a separate
    non-owner (delegated-admin) household registers + subscribes a third
    household so a fan-out target exists. Returns the space seed + the
    non-owner's (seed, instance_id)."""
    owner_seed, owner_pk = _make_keypair()
    space_seed, space_pk = _make_keypair()
    admin_seed, admin_pk = _make_keypair()
    sub_seed, sub_pk = _make_keypair()
    await svc.register_instance(
        "owner-a", owner_pk.hex(), "http://owner-a.example.com/wh", auto_accept=True
    )
    await svc.register_instance(
        "admin-a", admin_pk.hex(), "http://admin-a.example.com/wh", auto_accept=True
    )
    await svc.register_instance(
        "sub-a", sub_pk.hex(), "http://sub-a.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc,
        owner_seed,
        owning_instance="owner-a",
        space_id=space_id,
        identity_public_key=space_pk.hex(),
    )
    ts = _now_iso()
    sig = _sign(
        sub_seed,
        {"action": "subscribe", "instance_id": "sub-a", "space_id": space_id, "ts": ts},
    )
    await svc.subscribe("sub-a", space_id, ts, sig)
    return space_seed, admin_seed


async def test_publish_event_non_owner_valid_authority_sig_relays(gfs_db):
    """A non-owner (delegated admin) with a VALID space-authority signature
    over the payload relays the event (fans out to subscribers)."""
    ws = _RecordingWsRegistry()
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), ws_registry=ws)
    space_seed, admin_seed = await _setup_authority_relay(svc, space_id="sp-auth-ok")
    payload = {"ciphertext": "opaque-blob"}
    payload.update(_sign_authority(space_seed, space_id="sp-auth-ok", payload=payload))
    transport_sig = _sign(
        admin_seed,
        {
            "space_id": "sp-auth-ok",
            "event_type": "space_post_public",
            "payload": payload,
            "from_instance": "admin-a",
        },
    )
    delivered = await svc.publish_event(
        "sp-auth-ok",
        "space_post_public",
        payload,
        "admin-a",
        transport_sig,
    )
    assert "sub-a" in delivered


async def test_publish_event_authority_sig_in_set_cross_type_rejected(gfs_db):
    """Both space_post_public and space_subscriber_key_handoff are in the
    authority-relay allow-set, but the authority sig is verified UNDER the wire
    event_type — so a payload signed for space_post_public CANNOT be relayed as
    space_subscriber_key_handoff (the event_type is bound into the signed
    bytes). Pins the in-set cross-type rejection (a refactor that verified under
    a hardcoded type would silently reopen the hole)."""
    ws = _RecordingWsRegistry()
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), ws_registry=ws)
    space_seed, admin_seed = await _setup_authority_relay(svc, space_id="sp-xtype")
    payload = {"ciphertext": "opaque-blob"}
    # _sign_authority signs for AUTHORITY_EVENT_SPACE_POST_PUBLIC.
    payload.update(_sign_authority(space_seed, space_id="sp-xtype", payload=payload))
    transport_sig = _sign(
        admin_seed,
        {
            "space_id": "sp-xtype",
            "event_type": "space_subscriber_key_handoff",
            "payload": payload,
            "from_instance": "admin-a",
        },
    )
    # Relayed under a DIFFERENT (but in-set) wire event_type than it was signed
    # for → the authority sig fails to verify under the wire type → rejected.
    with pytest.raises(PermissionError):
        await svc.publish_event(
            "sp-xtype",
            "space_subscriber_key_handoff",
            payload,
            "admin-a",
            transport_sig,
        )


async def test_publish_event_non_owner_no_authority_sig_rejected(svc):
    """A payload carrying no authority signature is rejected — the signature is
    the ONLY authenticator, so there is nothing to fall back on."""
    _space_seed, admin_seed = await _setup_authority_relay(svc, space_id="sp-auth-none")
    payload = {"ciphertext": "opaque-blob"}
    transport_sig = _sign(
        admin_seed,
        {
            "space_id": "sp-auth-none",
            "event_type": "space_post_public",
            "payload": payload,
            "from_instance": "admin-a",
        },
    )
    with pytest.raises(PermissionError, match="missing space-authority signature"):
        await svc.publish_event(
            "sp-auth-none",
            "space_post_public",
            payload,
            "admin-a",
            transport_sig,
        )


async def test_publish_event_non_owner_tampered_authority_sig_rejected(svc):
    """A present-but-invalid authority sig is rejected — no fall-through to the
    owner check."""
    space_seed, admin_seed = await _setup_authority_relay(svc, space_id="sp-auth-bad")
    payload = {"ciphertext": "opaque-blob"}
    payload.update(_sign_authority(space_seed, space_id="sp-auth-bad", payload=payload))
    # Tamper with the content AFTER signing — the authority sig no longer matches.
    payload["ciphertext"] = "tampered-blob"
    transport_sig = _sign(
        admin_seed,
        {
            "space_id": "sp-auth-bad",
            "event_type": "space_post_public",
            "payload": payload,
            "from_instance": "admin-a",
        },
    )
    with pytest.raises(PermissionError, match="authority"):
        await svc.publish_event(
            "sp-auth-bad",
            "space_post_public",
            payload,
            "admin-a",
            transport_sig,
        )


async def test_publish_event_unknown_authority_suite_rejected(svc):
    """An authority sig advertising an unknown suite is rejected (no fallback)."""
    space_seed, admin_seed = await _setup_authority_relay(svc, space_id="sp-auth-suite")
    payload = {"ciphertext": "opaque-blob"}
    payload.update(
        _sign_authority(space_seed, space_id="sp-auth-suite", payload=payload)
    )
    payload["authority_sig_suite"] = "ed25519+future-pq"  # not in SUPPORTED set
    transport_sig = _sign(
        admin_seed,
        {
            "space_id": "sp-auth-suite",
            "event_type": "space_post_public",
            "payload": payload,
            "from_instance": "admin-a",
        },
    )
    with pytest.raises(PermissionError, match="authority"):
        await svc.publish_event(
            "sp-auth-suite",
            "space_post_public",
            payload,
            "admin-a",
            transport_sig,
        )


@pytest.mark.security
async def test_publish_event_owner_no_authority_sig_no_fan_out(gfs_db):
    """The owner relaying its own space content WITHOUT an authority sig is now
    rejected and nothing is fanned out — the space has a pinned authority
    pubkey, so an unsigned relay from any household (owner included) is a
    403."""
    ws = _RecordingWsRegistry()
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), ws_registry=ws)
    owner_seed, owner_pk = _make_keypair()
    _space_seed, space_pk = _make_keypair()
    sub_seed, sub_pk = _make_keypair()
    await svc.register_instance(
        "owner-bc", owner_pk.hex(), "http://owner-bc.example.com/wh", auto_accept=True
    )
    await svc.register_instance(
        "sub-bc", sub_pk.hex(), "http://sub-bc.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc,
        owner_seed,
        owning_instance="owner-bc",
        space_id="sp-auth-owner",
        identity_public_key=space_pk.hex(),
    )
    ts = _now_iso()
    sig = _sign(
        sub_seed,
        {
            "action": "subscribe",
            "instance_id": "sub-bc",
            "space_id": "sp-auth-owner",
            "ts": ts,
        },
    )
    await svc.subscribe("sub-bc", "sp-auth-owner", ts, sig)
    payload = {"ciphertext": "opaque-blob"}  # no authority_sig fields
    transport_sig = _sign(
        owner_seed,
        {
            "space_id": "sp-auth-owner",
            "event_type": "space_post_public",
            "payload": payload,
            "from_instance": "owner-bc",
        },
    )
    ws.sent.clear()
    with pytest.raises(PermissionError, match="missing space-authority signature"):
        await svc.publish_event(
            "sp-auth-owner",
            "space_post_public",
            payload,
            "owner-bc",
            transport_sig,
        )
    assert ws.sent == []


async def test_publish_event_null_pinned_pubkey_non_owner_rejected(svc):
    """A space with NULL pinned pubkey + a non-owner relay is rejected — the
    GFS can't verify authority, so only the owner may relay until a pubkey is
    pinned."""
    owner_seed, owner_pk = _make_keypair()
    space_seed, _space_pk = _make_keypair()
    admin_seed, admin_pk = _make_keypair()
    await svc.register_instance(
        "owner-null",
        owner_pk.hex(),
        "http://owner-null.example.com/wh",
        auto_accept=True,
    )
    await svc.register_instance(
        "admin-null",
        admin_pk.hex(),
        "http://admin-null.example.com/wh",
        auto_accept=True,
    )
    # First publish ships NO pubkey (older HFS) → row.identity_public_key NULL.
    await _publish_known_space(
        svc, owner_seed, owning_instance="owner-null", space_id="sp-null"
    )
    payload = {"ciphertext": "opaque-blob"}
    payload.update(_sign_authority(space_seed, space_id="sp-null", payload=payload))
    transport_sig = _sign(
        admin_seed,
        {
            "space_id": "sp-null",
            "event_type": "space_post_public",
            "payload": payload,
            "from_instance": "admin-null",
        },
    )
    with pytest.raises(PermissionError):
        await svc.publish_event(
            "sp-null",
            "space_post_public",
            payload,
            "admin-null",
            transport_sig,
        )


async def test_publish_event_non_hex_pinned_pubkey_rejected(svc):
    """A malformed pinned authority pubkey is unverifiable — fail closed as a
    ``PermissionError`` (403), never a ``ValueError`` escaping as a 500.

    ``identity_public_key`` is owner-supplied and never validated as hex at
    publish time, so every ``bytes.fromhex`` over it must sit inside a guard.
    """
    owner_seed, owner_pk = _make_keypair()
    space_seed, _space_pk = _make_keypair()
    admin_seed, admin_pk = _make_keypair()
    await svc.register_instance(
        "owner-badhex",
        owner_pk.hex(),
        "http://owner-badhex.example.com/wh",
        auto_accept=True,
    )
    await svc.register_instance(
        "admin-badhex",
        admin_pk.hex(),
        "http://admin-badhex.example.com/wh",
        auto_accept=True,
    )
    await _publish_known_space(
        svc,
        owner_seed,
        owning_instance="owner-badhex",
        space_id="sp-badhex",
        identity_public_key="not-hex!!",
    )
    payload = {"ciphertext": "opaque-blob"}
    payload.update(_sign_authority(space_seed, space_id="sp-badhex", payload=payload))
    transport_sig = _sign(
        admin_seed,
        {
            "space_id": "sp-badhex",
            "event_type": "space_post_public",
            "payload": payload,
            "from_instance": "admin-badhex",
        },
    )
    with pytest.raises(PermissionError, match="invalid authority key"):
        await svc.publish_event(
            "sp-badhex",
            "space_post_public",
            payload,
            "admin-badhex",
            transport_sig,
        )


async def test_list_subscribers_non_hex_pinned_pubkey_rejected(svc):
    """Same guard on the subscriber-release path: an unusable pinned key is a
    ``PermissionError``, so the route answers 403 rather than 500."""
    owner_seed, owner_pk = _make_keypair()
    space_seed, _space_pk = _make_keypair()
    await svc.register_instance(
        "owner-badhex2",
        owner_pk.hex(),
        "http://owner-badhex2.example.com/wh",
        auto_accept=True,
    )
    await _publish_known_space(
        svc,
        owner_seed,
        owning_instance="owner-badhex2",
        space_id="sp-badhex2",
        identity_public_key="not-hex!!",
    )
    ts = _now_iso()
    fields = _sign_authority_subscribers_query(space_seed, space_id="sp-badhex2", ts=ts)
    with pytest.raises(PermissionError, match="invalid authority key"):
        await svc.list_subscribers_with_keys(
            "sp-badhex2",
            ts=ts,
            authority_sig=fields["authority_sig"],
            authority_sig_suite=fields["authority_sig_suite"],
        )


async def test_publish_event_authority_sig_still_requires_transport_sig(svc):
    """The household transport signature stays MANDATORY even with a valid
    authority sig — an unsigned (or bad-household-sig) publish_event still 403s."""
    space_seed, _admin_seed = await _setup_authority_relay(svc, space_id="sp-auth-tx")
    payload = {"ciphertext": "opaque-blob"}
    payload.update(_sign_authority(space_seed, space_id="sp-auth-tx", payload=payload))
    # Empty transport signature → rejected before any authority check.
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.publish_event(
            "sp-auth-tx",
            "space_post_public",
            payload,
            "admin-a",
            signature="",
        )


async def test_publish_event_authority_sig_wrong_wire_event_type_rejected(svc):
    """A non-owner holding a VALID ``space_post_public`` authority sig must NOT
    be able to relay the payload under a DIFFERENT wire ``event_type``. The
    authority sig authorizes the space + payload, not the event type — so the
    GFS must bind the wire event_type to ``space_post_public`` on the authority
    (non-owner) relay path and reject any other type with PermissionError (no
    fan-out)."""
    space_seed, admin_seed = await _setup_authority_relay(svc, space_id="sp-auth-evt")
    payload = {"ciphertext": "opaque-blob"}
    # Sign under the ONLY authorized authority event type.
    payload.update(_sign_authority(space_seed, space_id="sp-auth-evt", payload=payload))
    # But relay under a different wire event_type (e.g. an admin action).
    transport_sig = _sign(
        admin_seed,
        {
            "space_id": "sp-auth-evt",
            "event_type": "space_admin_action",
            "payload": payload,
            "from_instance": "admin-a",
        },
    )
    with pytest.raises(PermissionError):
        await svc.publish_event(
            "sp-auth-evt",
            "space_admin_action",
            payload,
            "admin-a",
            transport_sig,
        )


# ── subscribe → owner ``new_subscriber`` notify (Phase 5b-b) ────────────────────


def _sign_authority_typed(
    space_seed: bytes, *, event_type: str, space_id: str, payload: dict
) -> dict:
    """Authority-sig wire fields under an explicit ``event_type``."""
    from socialhome.services.space_crypto_service import sign_authority_event

    return sign_authority_event(
        event_type=event_type,
        space_id=space_id,
        payload=payload,
        space_seed=space_seed,
    )


async def test_subscribe_notifies_owner_with_subscriber_keywrap(gfs_db):
    """On a successful subscribe, the GFS best-effort pushes a
    ``new_subscriber`` frame to the space OWNER's WS carrying the new
    subscriber's identity public_key + keywrap pubkey + keywrap_sig (so a
    seed-holding owner can verify the binding and seal the content key)."""
    ws = _RecordingWsRegistry()
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), ws_registry=ws)
    owner_seed, owner_pk = _make_keypair()
    sub_seed, sub_pk = _make_keypair()
    await svc.register_instance(
        "owner-ns", owner_pk.hex(), "http://owner-ns.example.com/wh", auto_accept=True
    )
    await svc.register_instance(
        "sub-ns",
        sub_pk.hex(),
        "http://sub-ns.example.com/wh",
        auto_accept=True,
        keywrap_public_key="cc" * 32,
        kem_suite="x25519",
        keywrap_sig="a2V5d3JhcHNpZw",
    )
    await _publish_known_space(
        svc, owner_seed, owning_instance="owner-ns", space_id="sp-ns"
    )
    ts = _now_iso()
    sig = _sign(
        sub_seed,
        {"action": "subscribe", "instance_id": "sub-ns", "space_id": "sp-ns", "ts": ts},
    )
    await svc.subscribe("sub-ns", "sp-ns", ts, sig)

    # Exactly one frame, to the owner, of type new_subscriber.
    notifies = [
        (inst, frame)
        for inst, frame in ws.sent
        if frame.get("type") == "new_subscriber"
    ]
    assert len(notifies) == 1
    inst, frame = notifies[0]
    assert inst == "owner-ns"
    assert frame["space_id"] == "sp-ns"
    sub = frame["subscriber"]
    assert sub["instance_id"] == "sub-ns"
    assert sub["identity_public_key"] == sub_pk.hex()
    assert sub["keywrap_public_key"] == "cc" * 32
    assert sub["keywrap_sig"] == "a2V5d3JhcHNpZw"


async def test_subscribe_owner_offline_no_crash(gfs_db):
    """When the owner has no WS socket (offline), the notify is dropped and the
    subscribe still completes normally — the 5b-c reconcile catches it up."""

    class _OfflineWsRegistry:
        async def send(self, instance_id: str, frame: dict) -> bool:
            return False  # nobody connected

    svc = GfsFederationService(
        SqliteGfsFederationRepo(gfs_db), ws_registry=_OfflineWsRegistry()
    )
    owner_seed, owner_pk = _make_keypair()
    sub_seed, sub_pk = _make_keypair()
    await svc.register_instance(
        "owner-off", owner_pk.hex(), "http://owner-off.example.com/wh", auto_accept=True
    )
    await svc.register_instance(
        "sub-off", sub_pk.hex(), "http://sub-off.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, owner_seed, owning_instance="owner-off", space_id="sp-off"
    )
    ts = _now_iso()
    sig = _sign(
        sub_seed,
        {
            "action": "subscribe",
            "instance_id": "sub-off",
            "space_id": "sp-off",
            "ts": ts,
        },
    )
    await svc.subscribe("sub-off", "sp-off", ts, sig)  # must not raise
    subs = await svc._repo.list_subscribers("sp-off")
    assert any(s.instance_id == "sub-off" for s in subs)


async def test_subscribe_no_ws_registry_no_crash(svc):
    """A GFS built without a ws_registry simply skips the notify."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-nows", pk.hex(), "http://nows.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="inst-nows", space_id="sp-nows"
    )
    ts = _now_iso()
    sig = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-nows",
            "space_id": "sp-nows",
            "ts": ts,
        },
    )
    await svc.subscribe("inst-nows", "sp-nows", ts, sig)  # must not raise


# ── publish_event accepts the subscriber-key-handoff authority relay ────────────


async def test_publish_event_subscriber_key_handoff_relays(gfs_db):
    """A space-authority-signed ``space_subscriber_key_handoff`` relay from a
    non-owner seed-holder is authorized + fanned out (the second allowed
    authority event type alongside ``space_post_public``)."""
    ws = _RecordingWsRegistry()
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), ws_registry=ws)
    space_seed, admin_seed = await _setup_authority_relay(svc, space_id="sp-handoff")
    payload = {"target_instance_id": "sub-a", "sealed": {"ciphertext": "x:y"}}
    payload.update(
        _sign_authority_typed(
            space_seed,
            event_type="space_subscriber_key_handoff",
            space_id="sp-handoff",
            payload=payload,
        )
    )
    transport_sig = _sign(
        admin_seed,
        {
            "space_id": "sp-handoff",
            "event_type": "space_subscriber_key_handoff",
            "payload": payload,
            "from_instance": "admin-a",
        },
    )
    delivered = await svc.publish_event(
        "sp-handoff",
        "space_subscriber_key_handoff",
        payload,
        "admin-a",
        transport_sig,
    )
    assert "sub-a" in delivered


async def test_publish_event_unknown_authority_event_type_still_rejected(svc):
    """The authority path stays strict: an event type that is neither
    ``space_post_public`` nor ``space_subscriber_key_handoff`` is rejected even
    with a valid authority signature over the payload."""
    space_seed, admin_seed = await _setup_authority_relay(svc, space_id="sp-unk-evt")
    payload = {"ciphertext": "opaque-blob"}
    payload.update(
        _sign_authority_typed(
            space_seed,
            event_type="space_unknown_relay",
            space_id="sp-unk-evt",
            payload=payload,
        )
    )
    transport_sig = _sign(
        admin_seed,
        {
            "space_id": "sp-unk-evt",
            "event_type": "space_unknown_relay",
            "payload": payload,
            "from_instance": "admin-a",
        },
    )
    with pytest.raises(PermissionError):
        await svc.publish_event(
            "sp-unk-evt",
            "space_unknown_relay",
            payload,
            "admin-a",
            transport_sig,
        )


# ── update_instance (signed display-name change) ────────────────────────────────


async def test_update_instance_renames_with_valid_signature(svc):
    """update_instance() persists a new display_name when the signature
    verifies against the registered public key and the ts is fresh."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-rename", pk.hex(), "http://r.example.com/wh", display_name="Old"
    )
    ts = _now_iso()
    sig = _sign(
        seed,
        {"instance_id": "inst-rename", "display_name": "New Home", "ts": ts},
    )
    await svc.update_instance("inst-rename", "New Home", ts, sig)

    inst = await svc._repo.get_instance("inst-rename")
    assert inst is not None
    assert inst.display_name == "New Home"


async def test_update_instance_unknown_raises_permission_error(svc):
    """An instance the GFS never registered cannot rename — PermissionError."""
    ts = _now_iso()
    with pytest.raises(PermissionError, match="Unknown instance"):
        await svc.update_instance("ghost-inst", "Whatever", ts, "AAAA")


async def test_update_instance_bad_signature_raises_permission_error(svc):
    """A forged / mismatched signature is rejected with PermissionError."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-badsig2", pk.hex(), "http://b.example.com/wh")
    other_seed, _ = _make_keypair()
    ts = _now_iso()
    sig = _sign(
        other_seed,
        {"instance_id": "inst-badsig2", "display_name": "Hijack", "ts": ts},
    )
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.update_instance("inst-badsig2", "Hijack", ts, sig)


async def test_update_instance_empty_signature_raises_permission_error(svc):
    """The signature is REQUIRED (unlike publish_space's optional branch)."""
    _, pk = _make_keypair()
    await svc.register_instance("inst-nosig2", pk.hex(), "http://n.example.com/wh")
    ts = _now_iso()
    with pytest.raises(PermissionError):
        await svc.update_instance("inst-nosig2", "Name", ts, "")


async def test_update_instance_stale_timestamp_raises_permission_error(svc):
    """A ts older than the 300s replay window is rejected."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-stale", pk.hex(), "http://s.example.com/wh")
    ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    sig = _sign(
        seed,
        {"instance_id": "inst-stale", "display_name": "Late", "ts": ts},
    )
    with pytest.raises(PermissionError, match="Stale timestamp"):
        await svc.update_instance("inst-stale", "Late", ts, sig)


async def test_update_instance_unparseable_timestamp_raises_permission_error(svc):
    """A ts that isn't ISO 8601 is rejected (treated as stale/invalid)."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-badts", pk.hex(), "http://t.example.com/wh")
    ts = "not-a-timestamp"
    sig = _sign(
        seed,
        {"instance_id": "inst-badts", "display_name": "X", "ts": ts},
    )
    with pytest.raises(PermissionError):
        await svc.update_instance("inst-badts", "X", ts, sig)


async def test_update_instance_empty_name_raises_value_error(svc):
    """An empty (after-strip) display_name is rejected with ValueError."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-empty", pk.hex(), "http://e.example.com/wh")
    ts = _now_iso()
    sig = _sign(
        seed,
        {"instance_id": "inst-empty", "display_name": "   ", "ts": ts},
    )
    with pytest.raises(ValueError, match="1-80"):
        await svc.update_instance("inst-empty", "   ", ts, sig)


async def test_update_instance_overlong_name_raises_value_error(svc):
    """A >80-char display_name is rejected with ValueError."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-long", pk.hex(), "http://l.example.com/wh")
    ts = _now_iso()
    name = "y" * 81
    sig = _sign(
        seed,
        {"instance_id": "inst-long", "display_name": name, "ts": ts},
    )
    with pytest.raises(ValueError, match="1-80"):
        await svc.update_instance("inst-long", name, ts, sig)


# ── unsubscribe authentication (self-binding, replay-guarded) ─────────────────


async def _subscribed(svc, space_id: str, instance_id: str) -> bool:
    subs = await svc._repo.list_subscribers(space_id)
    return any(s.instance_id == instance_id for s in subs)


async def test_unsubscribe_signed_removes_subscriber(svc):
    """A correctly-signed unsubscribe drops the subscription row."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-u1", pk.hex(), "http://u1.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="inst-u1", space_id="space-u1"
    )
    ts = _now_iso()
    sig = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-u1",
            "space_id": "space-u1",
            "ts": ts,
        },
    )
    await svc.subscribe("inst-u1", "space-u1", ts, sig)
    assert await _subscribed(svc, "space-u1", "inst-u1")

    ts2 = _now_iso()
    sig2 = _sign(
        seed,
        {
            "action": "unsubscribe",
            "instance_id": "inst-u1",
            "space_id": "space-u1",
            "ts": ts2,
        },
    )
    await svc.unsubscribe("inst-u1", "space-u1", ts2, sig2)
    assert not await _subscribed(svc, "space-u1", "inst-u1")


async def test_unsubscribe_unsigned_rejected_and_row_survives(svc):
    """SECURITY: an unsigned unsubscribe cannot evict a subscriber."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-u2", pk.hex(), "http://u2.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="inst-u2", space_id="space-u2"
    )
    ts = _now_iso()
    sig = _sign(
        seed,
        {
            "action": "subscribe",
            "instance_id": "inst-u2",
            "space_id": "space-u2",
            "ts": ts,
        },
    )
    await svc.subscribe("inst-u2", "space-u2", ts, sig)

    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.unsubscribe("inst-u2", "space-u2", _now_iso(), "")
    assert await _subscribed(svc, "space-u2", "inst-u2")


async def test_unsubscribe_cannot_sign_for_another_instance(svc):
    """Signing with household B's key while naming household A is rejected."""
    seed_a, pk_a = _make_keypair()
    seed_b, pk_b = _make_keypair()
    await svc.register_instance(
        "inst-u3a", pk_a.hex(), "http://u3a.example.com/wh", auto_accept=True
    )
    await svc.register_instance("inst-u3b", pk_b.hex(), "http://u3b.example.com/wh")
    await _publish_known_space(
        svc, seed_a, owning_instance="inst-u3a", space_id="space-u3"
    )
    ts = _now_iso()
    sig = _sign(
        seed_a,
        {
            "action": "subscribe",
            "instance_id": "inst-u3a",
            "space_id": "space-u3",
            "ts": ts,
        },
    )
    await svc.subscribe("inst-u3a", "space-u3", ts, sig)

    ts2 = _now_iso()
    # inst-u3b signs a body naming inst-u3a — verified against u3a's key → fails.
    forged = _sign(
        seed_b,
        {
            "action": "unsubscribe",
            "instance_id": "inst-u3a",
            "space_id": "space-u3",
            "ts": ts2,
        },
    )
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.unsubscribe("inst-u3a", "space-u3", ts2, forged)
    assert await _subscribed(svc, "space-u3", "inst-u3a")


async def test_unsubscribe_stale_timestamp_rejected_and_row_survives(svc):
    """A ts outside the ±300 s replay window is rejected — and the
    existing subscription is left intact."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-u4", pk.hex(), "http://u4.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="inst-u4", space_id="space-u4"
    )
    ts_ok = _now_iso()
    await svc.subscribe(
        "inst-u4",
        "space-u4",
        ts_ok,
        _sign(
            seed,
            {
                "action": "subscribe",
                "instance_id": "inst-u4",
                "space_id": "space-u4",
                "ts": ts_ok,
            },
        ),
    )

    ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    sig = _sign(
        seed,
        {
            "action": "unsubscribe",
            "instance_id": "inst-u4",
            "space_id": "space-u4",
            "ts": ts,
        },
    )
    with pytest.raises(PermissionError, match="Stale timestamp"):
        await svc.unsubscribe("inst-u4", "space-u4", ts, sig)
    assert await _subscribed(svc, "space-u4", "inst-u4")


async def test_unsubscribe_naive_timestamp_rejected_and_row_survives(svc):
    """A tz-less timestamp is untrusted and rejected — the existing
    subscription survives."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-u5", pk.hex(), "http://u5.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="inst-u5", space_id="space-u5"
    )
    ts_ok = _now_iso()
    await svc.subscribe(
        "inst-u5",
        "space-u5",
        ts_ok,
        _sign(
            seed,
            {
                "action": "subscribe",
                "instance_id": "inst-u5",
                "space_id": "space-u5",
                "ts": ts_ok,
            },
        ),
    )

    ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    sig = _sign(
        seed,
        {
            "action": "unsubscribe",
            "instance_id": "inst-u5",
            "space_id": "space-u5",
            "ts": ts,
        },
    )
    with pytest.raises(PermissionError, match="Stale timestamp"):
        await svc.unsubscribe("inst-u5", "space-u5", ts, sig)
    assert await _subscribed(svc, "space-u5", "inst-u5")


async def test_unsubscribe_unknown_instance_rejected(svc):
    """An instance the GFS never registered cannot unsubscribe anybody."""
    with pytest.raises(PermissionError, match="Unknown instance"):
        await svc.unsubscribe("ghost-inst", "space-u6", _now_iso(), "AAAA")


async def test_unsubscribe_unknown_space_still_succeeds(svc):
    """Unsubscribing from a never-published space stays idempotent."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-u7", pk.hex(), "http://u7.example.com/wh", auto_accept=True
    )
    ts = _now_iso()
    sig = _sign(
        seed,
        {
            "action": "unsubscribe",
            "instance_id": "inst-u7",
            "space_id": "space-ghost-u7",
            "ts": ts,
        },
    )
    await svc.unsubscribe("inst-u7", "space-ghost-u7", ts, sig)


# ── hide_space (owner withdrawal) ─────────────────────────────────────────────


def _sign_unpublish(seed: bytes, *, owning_instance: str, space_id: str, ts: str):
    return _sign(
        seed,
        {
            "action": "unpublish",
            "owning_instance": owning_instance,
            "space_id": space_id,
            "ts": ts,
        },
    )


async def test_hide_space_signed_by_owner_withdraws_without_banning(svc):
    """A signed owner withdrawal sets ``withdrawn`` and leaves ``status``
    alone — a ban is the moderator's state, not the owner's."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-h1", pk.hex(), "http://h1.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="inst-h1", space_id="space-h1"
    )
    ts = _now_iso()
    await svc.hide_space(
        "space-h1",
        "inst-h1",
        ts,
        _sign_unpublish(seed, owning_instance="inst-h1", space_id="space-h1", ts=ts),
    )
    sp = await svc.get_space("space-h1")
    assert sp is not None
    assert sp.withdrawn is True
    assert sp.status == "active"
    assert [s.space_id for s in await svc.list_spaces(status="active")] == []


async def test_hide_space_unsigned_rejected_and_space_stays_listed(svc):
    """SECURITY REGRESSION: unpublish used to be completely unauthenticated,
    so any caller could delist any space."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-h2", pk.hex(), "http://h2.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="inst-h2", space_id="space-h2"
    )
    with pytest.raises(PermissionError, match="signature"):
        await svc.hide_space("space-h2", "inst-h2", _now_iso(), "")
    sp = await svc.get_space("space-h2")
    assert sp is not None
    assert sp.withdrawn is False


async def test_hide_space_by_registered_non_owner_rejected(svc):
    """Authentication is not authorization: a valid signature from another
    registered household must not delist someone else's space."""
    owner_seed, owner_pk = _make_keypair()
    other_seed, other_pk = _make_keypair()
    await svc.register_instance(
        "inst-h3", owner_pk.hex(), "http://h3.example.com/wh", auto_accept=True
    )
    await svc.register_instance(
        "inst-evil", other_pk.hex(), "http://evil.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, owner_seed, owning_instance="inst-h3", space_id="space-h3"
    )
    ts = _now_iso()
    with pytest.raises(PermissionError, match="owner"):
        await svc.hide_space(
            "space-h3",
            "inst-evil",
            ts,
            _sign_unpublish(
                other_seed, owning_instance="inst-evil", space_id="space-h3", ts=ts
            ),
        )
    sp = await svc.get_space("space-h3")
    assert sp is not None
    assert sp.withdrawn is False


async def test_hide_space_stale_timestamp_rejected(svc):
    """The ±300 s replay guard applies to a captured withdrawal too."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-h4", pk.hex(), "http://h4.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, seed, owning_instance="inst-h4", space_id="space-h4"
    )
    ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with pytest.raises(PermissionError, match="Stale timestamp"):
        await svc.hide_space(
            "space-h4",
            "inst-h4",
            ts,
            _sign_unpublish(
                seed, owning_instance="inst-h4", space_id="space-h4", ts=ts
            ),
        )
    sp = await svc.get_space("space-h4")
    assert sp is not None
    assert sp.withdrawn is False


async def test_hide_space_unknown_instance_rejected(svc):
    """An unregistered caller is rejected before anything else happens."""
    with pytest.raises(PermissionError, match="Unknown instance"):
        await svc.hide_space("space-h5", "ghost-inst", _now_iso(), "AAAA")


async def test_hide_space_unknown_space_is_a_signed_noop(svc):
    """An unknown space stays a silent no-op — but only AFTER the signature
    verifies, so an unsigned caller can't probe for space existence."""
    seed, pk = _make_keypair()
    await svc.register_instance(
        "inst-h6", pk.hex(), "http://h6.example.com/wh", auto_accept=True
    )
    ts = _now_iso()
    await svc.hide_space(
        "space-ghost-h6",
        "inst-h6",
        ts,
        _sign_unpublish(
            seed, owning_instance="inst-h6", space_id="space-ghost-h6", ts=ts
        ),
    )
    assert await svc.get_space("space-ghost-h6") is None


# ── subscriber (re)connect → owner ``new_subscriber`` re-notify (Phase 5b-d) ────


async def _register_and_subscribe(
    svc,
    *,
    owner: str,
    subscriber: str,
    space_id: str,
    owner_seed: bytes,
    sub_seed: bytes,
) -> None:
    """Publish *space_id* under *owner* and subscribe *subscriber* to it."""
    await _publish_known_space(
        svc, owner_seed, owning_instance=owner, space_id=space_id
    )
    ts = _now_iso()
    sig = _sign(
        sub_seed,
        {
            "action": "subscribe",
            "instance_id": subscriber,
            "space_id": space_id,
            "ts": ts,
        },
    )
    await svc.subscribe(subscriber, space_id, ts, sig)


async def test_subscriber_reconnect_renotifies_owner(gfs_db):
    """REGRESSION: a household that subscribed while its own socket was DOWN
    gets the Phase-5b-b handoff re-triggered when its socket connects — the
    owner receives the same ``new_subscriber`` frame ``subscribe`` emits."""

    class _OfflineSubscriberWs:
        """Everyone is offline (mirrors the lost-handoff production case)."""

        def __init__(self) -> None:
            self.sent: list[tuple[str, dict]] = []

        async def send(self, instance_id: str, frame: dict) -> bool:
            self.sent.append((instance_id, frame))
            return False

    ws = _OfflineSubscriberWs()
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), ws_registry=ws)
    owner_seed, owner_pk = _make_keypair()
    sub_seed, sub_pk = _make_keypair()
    await svc.register_instance(
        "owner-rc", owner_pk.hex(), "http://owner-rc/wh", auto_accept=True
    )
    await svc.register_instance(
        "sub-rc",
        sub_pk.hex(),
        "http://sub-rc/wh",
        auto_accept=True,
        keywrap_public_key="cc" * 32,
        kem_suite="x25519",
        keywrap_sig="a2V5d3JhcHNpZw",
    )
    await _register_and_subscribe(
        svc,
        owner="owner-rc",
        subscriber="sub-rc",
        space_id="sp-rc",
        owner_seed=owner_seed,
        sub_seed=sub_seed,
    )
    subscribe_frame = ws.sent[-1]
    ws.sent.clear()

    # The subscriber's socket comes up.
    await svc.on_subscriber_connected("sub-rc")

    assert len(ws.sent) == 1
    # Byte-identical to the frame ``subscribe`` emits — the owner's handler
    # parses exactly one shape.
    assert ws.sent[0] == subscribe_frame
    inst, frame = ws.sent[0]
    assert inst == "owner-rc"
    assert frame["type"] == "new_subscriber"
    assert frame["space_id"] == "sp-rc"
    assert frame["subscriber"]["instance_id"] == "sub-rc"
    assert frame["subscriber"]["keywrap_public_key"] == "cc" * 32


async def test_subscriber_reconnect_without_subscriptions_notifies_nothing(gfs_db):
    ws = _RecordingWsRegistry()
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), ws_registry=ws)
    _seed, pk = _make_keypair()
    await svc.register_instance(
        "lonely", pk.hex(), "http://lonely/wh", auto_accept=True
    )
    await svc.on_subscriber_connected("lonely")
    assert ws.sent == []


async def test_subscriber_reconnect_skips_self_owned_space(gfs_db):
    """A space the connecting instance OWNS needs no handoff to itself."""
    ws = _RecordingWsRegistry()
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), ws_registry=ws)
    seed, pk = _make_keypair()
    await svc.register_instance("solo", pk.hex(), "http://solo/wh", auto_accept=True)
    await _register_and_subscribe(
        svc,
        owner="solo",
        subscriber="solo",
        space_id="sp-solo",
        owner_seed=seed,
        sub_seed=seed,
    )
    ws.sent.clear()
    await svc.on_subscriber_connected("solo")
    assert ws.sent == []


async def test_subscriber_reconnect_owner_offline_is_fail_soft(gfs_db):
    """An owner with no socket (send raises) is skipped without raising."""

    class _RaisingWs:
        async def send(self, instance_id: str, frame: dict) -> bool:
            raise ConnectionResetError("no socket")

    svc = GfsFederationService(
        SqliteGfsFederationRepo(gfs_db), ws_registry=_RaisingWs()
    )
    owner_seed, owner_pk = _make_keypair()
    sub_seed, sub_pk = _make_keypair()
    await svc.register_instance(
        "owner-fs", owner_pk.hex(), "http://owner-fs/wh", auto_accept=True
    )
    await svc.register_instance(
        "sub-fs", sub_pk.hex(), "http://sub-fs/wh", auto_accept=True
    )
    await _register_and_subscribe(
        svc,
        owner="owner-fs",
        subscriber="sub-fs",
        space_id="sp-fs",
        owner_seed=owner_seed,
        sub_seed=sub_seed,
    )
    await svc.on_subscriber_connected("sub-fs")  # must not raise


async def test_subscriber_reconnect_repo_failure_is_swallowed(gfs_db):
    """A repo read failure on the connect path is logged, never raised."""

    class _BoomRepo(SqliteGfsFederationRepo):
        async def list_subscribed_spaces(self, instance_id: str):
            raise RuntimeError("db gone")

    ws = _RecordingWsRegistry()
    svc = GfsFederationService(_BoomRepo(gfs_db), ws_registry=ws)
    _seed, pk = _make_keypair()
    await svc.register_instance("boom", pk.hex(), "http://boom/wh", auto_accept=True)
    await svc.on_subscriber_connected("boom")  # must not raise
    assert ws.sent == []


async def test_subscriber_reconnect_unknown_instance_is_noop(gfs_db):
    ws = _RecordingWsRegistry()
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), ws_registry=ws)
    await svc.on_subscriber_connected("never-registered")
    assert ws.sent == []


async def test_subscriber_reconnect_without_ws_registry_is_noop(svc):
    await svc.on_subscriber_connected("whoever")  # must not raise


async def test_subscriber_reconnect_notifies_are_capped(gfs_db, monkeypatch):
    """The per-connect notify count is bounded so a heavily-subscribed
    household can't stall / flood on connect."""
    monkeypatch.setattr(federation_mod, "MAX_RECONNECT_NOTIFIES", 2)
    ws = _RecordingWsRegistry()
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), ws_registry=ws)
    owner_seed, owner_pk = _make_keypair()
    sub_seed, sub_pk = _make_keypair()
    await svc.register_instance(
        "owner-cap", owner_pk.hex(), "http://owner-cap/wh", auto_accept=True
    )
    await svc.register_instance(
        "sub-cap", sub_pk.hex(), "http://sub-cap/wh", auto_accept=True
    )
    for n in range(4):
        await _register_and_subscribe(
            svc,
            owner="owner-cap",
            subscriber="sub-cap",
            space_id=f"sp-cap-{n}",
            owner_seed=owner_seed,
            sub_seed=sub_seed,
        )
    ws.sent.clear()
    await svc.on_subscriber_connected("sub-cap")
    assert len(ws.sent) == 2


async def test_schedule_subscriber_connected_runs_in_background(gfs_db):
    """The connect hook is dispatched as a background task so the WS
    handshake never blocks on the notify fan-out."""
    ws = _RecordingWsRegistry()
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), ws_registry=ws)
    owner_seed, owner_pk = _make_keypair()
    sub_seed, sub_pk = _make_keypair()
    await svc.register_instance(
        "owner-bg", owner_pk.hex(), "http://owner-bg/wh", auto_accept=True
    )
    await svc.register_instance(
        "sub-bg", sub_pk.hex(), "http://sub-bg/wh", auto_accept=True
    )
    await _register_and_subscribe(
        svc,
        owner="owner-bg",
        subscriber="sub-bg",
        space_id="sp-bg",
        owner_seed=owner_seed,
        sub_seed=sub_seed,
    )
    ws.sent.clear()
    svc.schedule_subscriber_connected("sub-bg")  # returns immediately (sync)
    assert ws.sent == []
    for _ in range(100):
        await asyncio.sleep(0.01)
        if ws.sent:
            break
    assert [inst for inst, _ in ws.sent] == ["owner-bg"]


# ── Anonymous publish: the GFS never learns the relaying household ─────────────
#
# SECURITY: ``/gfs/publish`` authorizes ONLY on the space-authority signature
# inside the (opaque) payload. ``from_instance`` is a tolerated LEGACY field —
# verified when present, never trusted, never forwarded, never logged.


async def _anon_relay_env(gfs_db, *, space_id: str):
    """A GFS service + recording ws-registry with one authority-pinned space,
    a subscriber (``sub-a``) and a non-owner publisher (``admin-a``).

    Returns ``(svc, ws, space_seed, admin_seed)``.
    """
    ws = _RecordingWsRegistry()
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), ws_registry=ws)
    space_seed, admin_seed = await _setup_authority_relay(svc, space_id=space_id)
    # Drop the owner's ``new_subscriber`` notify so ``ws.sent`` holds only the
    # frames the publish under test produced.
    ws.sent.clear()
    return svc, ws, space_seed, admin_seed


def _authority_payload(space_seed: bytes, *, space_id: str) -> dict:
    """An opaque payload carrying a valid space-authority signature."""
    payload = {"ciphertext": "opaque-blob"}
    payload.update(_sign_authority(space_seed, space_id=space_id, payload=payload))
    return payload


def _legacy_transport_sig(
    seed: bytes,
    *,
    space_id: str,
    event_type: str,
    payload: dict,
    from_instance: str,
) -> str:
    """The household transport signature an older HFS still sends."""
    return _sign(
        seed,
        {
            "space_id": space_id,
            "event_type": event_type,
            "payload": payload,
            "from_instance": from_instance,
        },
    )


@pytest.mark.security
async def test_publish_event_canonical_body_frame_is_identity_free(gfs_db):
    """The canonical body ``{space_id, event_type, payload}`` carries no
    household identity at all, and the fan-out frame has exactly four keys —
    no ``from_instance``, no publisher id anywhere in the serialized frame."""
    svc, ws, space_seed, _admin_seed = await _anon_relay_env(gfs_db, space_id="sp-anon")
    payload = _authority_payload(space_seed, space_id="sp-anon")
    delivered = await svc.publish_event("sp-anon", "space_post_public", payload)
    assert delivered == ["sub-a"]
    assert [inst for inst, _ in ws.sent] == ["sub-a"]
    frame = ws.sent[0][1]
    assert set(frame) == {"type", "space_id", "event_type", "payload"}
    assert frame["type"] == "relay"
    assert "from_instance" not in frame
    assert "target_instance_id" not in frame
    assert "admin-a" not in json.dumps(frame)
    assert "owner-a" not in json.dumps(frame)


@pytest.mark.security
async def test_publish_event_legacy_body_frame_is_identity_free(gfs_db):
    """An older household still sending ``from_instance`` + its transport
    signature succeeds (its authority sig is valid) and gets the IDENTICAL
    identity-free frame — the legacy field never reaches a subscriber."""
    svc, ws, space_seed, admin_seed = await _anon_relay_env(
        gfs_db, space_id="sp-legacy"
    )
    payload = _authority_payload(space_seed, space_id="sp-legacy")
    sig = _legacy_transport_sig(
        admin_seed,
        space_id="sp-legacy",
        event_type="space_post_public",
        payload=payload,
        from_instance="admin-a",
    )
    delivered = await svc.publish_event(
        "sp-legacy", "space_post_public", payload, "admin-a", sig
    )
    assert delivered == ["sub-a"]
    frame = ws.sent[0][1]
    assert frame == {
        "type": "relay",
        "space_id": "sp-legacy",
        "event_type": "space_post_public",
        "payload": payload,
    }
    assert "admin-a" not in json.dumps(frame)


@pytest.mark.security
async def test_publish_event_owner_without_authority_sig_rejected(gfs_db):
    """The owner loophole is CLOSED: even the space's owning instance, with a
    valid transport signature, cannot relay a payload that carries no
    space-authority signature."""
    svc, ws, _space_seed, _admin_seed = await _anon_relay_env(
        gfs_db, space_id="sp-owner-loop"
    )
    # Re-register owner-a with a key we hold so we can sign AS the owner.
    owner_seed, owner_pk = _make_keypair()
    await svc.register_instance(
        "owner-a", owner_pk.hex(), "http://owner-a.example.com/wh", auto_accept=True
    )
    payload = {"ciphertext": "opaque-blob"}  # no authority_sig fields
    sig = _legacy_transport_sig(
        owner_seed,
        space_id="sp-owner-loop",
        event_type="space_post_public",
        payload=payload,
        from_instance="owner-a",
    )
    with pytest.raises(PermissionError, match="authority"):
        await svc.publish_event(
            "sp-owner-loop", "space_post_public", payload, "owner-a", sig
        )
    assert ws.sent == []


@pytest.mark.security
async def test_publish_event_owner_disallowed_event_type_rejected(gfs_db):
    """An event type outside ``AUTHORITY_RELAY_EVENT_TYPES`` is rejected even
    from the owner — the retired owner path used to relay ANY type."""
    svc, ws, space_seed, _admin_seed = await _anon_relay_env(
        gfs_db, space_id="sp-owner-type"
    )
    owner_seed, owner_pk = _make_keypair()
    await svc.register_instance(
        "owner-a", owner_pk.hex(), "http://owner-a.example.com/wh", auto_accept=True
    )
    payload = _authority_payload(space_seed, space_id="sp-owner-type")
    sig = _legacy_transport_sig(
        owner_seed,
        space_id="sp-owner-type",
        event_type="space_admin_action",
        payload=payload,
        from_instance="owner-a",
    )
    with pytest.raises(PermissionError, match="event types"):
        await svc.publish_event(
            "sp-owner-type", "space_admin_action", payload, "owner-a", sig
        )
    assert ws.sent == []


@pytest.mark.security
async def test_publish_event_legacy_bad_household_sig_rejected(gfs_db):
    """A garbage legacy field is never a free pass: a present-but-invalid
    household transport signature is rejected even with a valid authority
    sig."""
    svc, ws, space_seed, _admin_seed = await _anon_relay_env(
        gfs_db, space_id="sp-badtrans"
    )
    other_seed, _other_pk = _make_keypair()  # not admin-a's registered key
    payload = _authority_payload(space_seed, space_id="sp-badtrans")
    sig = _legacy_transport_sig(
        other_seed,
        space_id="sp-badtrans",
        event_type="space_post_public",
        payload=payload,
        from_instance="admin-a",
    )
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await svc.publish_event(
            "sp-badtrans", "space_post_public", payload, "admin-a", sig
        )
    assert ws.sent == []


@pytest.mark.security
async def test_publish_event_legacy_unregistered_instance_rejected(gfs_db):
    """A legacy ``from_instance`` naming an unregistered household is treated
    exactly like a bad legacy signature — rejected, with no identity echoed
    back in the error."""
    svc, ws, space_seed, admin_seed = await _anon_relay_env(gfs_db, space_id="sp-ghost")
    payload = _authority_payload(space_seed, space_id="sp-ghost")
    sig = _legacy_transport_sig(
        admin_seed,
        space_id="sp-ghost",
        event_type="space_post_public",
        payload=payload,
        from_instance="ghost-a",
    )
    with pytest.raises(PermissionError) as excinfo:
        await svc.publish_event(
            "sp-ghost", "space_post_public", payload, "ghost-a", sig
        )
    assert "ghost-a" not in str(excinfo.value)
    assert ws.sent == []


@pytest.mark.security
async def test_publish_event_legacy_banned_instance_rejected(gfs_db):
    """A banned household's legacy publish is rejected like a bad legacy sig."""
    svc, ws, space_seed, admin_seed = await _anon_relay_env(
        gfs_db, space_id="sp-banned-inst"
    )
    await svc._repo.set_instance_status("admin-a", "banned")
    payload = _authority_payload(space_seed, space_id="sp-banned-inst")
    sig = _legacy_transport_sig(
        admin_seed,
        space_id="sp-banned-inst",
        event_type="space_post_public",
        payload=payload,
        from_instance="admin-a",
    )
    with pytest.raises(PermissionError):
        await svc.publish_event(
            "sp-banned-inst", "space_post_public", payload, "admin-a", sig
        )
    assert ws.sent == []


@pytest.mark.security
async def test_publish_event_banned_space_rejected_without_fan_out(gfs_db):
    """A moderator-banned space fails closed BEFORE any fan-out."""
    svc, ws, space_seed, _admin_seed = await _anon_relay_env(
        gfs_db, space_id="sp-banned"
    )
    await svc._repo.set_space_status("sp-banned", "banned")
    payload = _authority_payload(space_seed, space_id="sp-banned")
    with pytest.raises(PermissionError, match="banned"):
        await svc.publish_event("sp-banned", "space_post_public", payload)
    assert ws.sent == []


async def test_publish_event_withdrawn_space_still_relays(gfs_db):
    """An owner-withdrawn space is DELISTED from discovery only — households
    already subscribed keep receiving the relay (pins today's behaviour)."""
    svc, ws, space_seed, _admin_seed = await _anon_relay_env(
        gfs_db, space_id="sp-withdrawn"
    )
    await svc._repo.set_space_withdrawn("sp-withdrawn", True)
    payload = _authority_payload(space_seed, space_id="sp-withdrawn")
    delivered = await svc.publish_event("sp-withdrawn", "space_post_public", payload)
    assert delivered == ["sub-a"]
    assert len(ws.sent) == 1


@pytest.mark.security
async def test_publish_event_no_pinned_key_rejected(gfs_db):
    """A space with no TOFU-pinned authority key cannot be relayed for at all —
    not even by its owner (the owner re-publishes metadata to heal the pin)."""
    ws = _RecordingWsRegistry()
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), ws_registry=ws)
    owner_seed, owner_pk = _make_keypair()
    space_seed, _space_pk = _make_keypair()
    await svc.register_instance(
        "owner-np", owner_pk.hex(), "http://owner-np/wh", auto_accept=True
    )
    await _publish_known_space(
        svc, owner_seed, owning_instance="owner-np", space_id="sp-nopin"
    )
    payload = _authority_payload(space_seed, space_id="sp-nopin")
    with pytest.raises(PermissionError, match="no pinned authority key"):
        await svc.publish_event("sp-nopin", "space_post_public", payload)
    assert ws.sent == []


@pytest.mark.security
async def test_publish_event_unknown_suite_rejected_anonymous_body(gfs_db):
    """An unknown authority-sig suite is rejected on the identity-free body
    too — no default fallback."""
    svc, ws, space_seed, _admin_seed = await _anon_relay_env(
        gfs_db, space_id="sp-suite2"
    )
    payload = _authority_payload(space_seed, space_id="sp-suite2")
    payload["authority_sig_suite"] = "ed25519+future-pq"
    with pytest.raises(PermissionError, match="suite"):
        await svc.publish_event("sp-suite2", "space_post_public", payload)
    assert ws.sent == []


async def test_publish_event_publisher_that_subscribes_receives_own_frame(gfs_db):
    """Self-exclusion is GONE: the GFS no longer knows who published, so a
    publisher that also subscribes receives its own frame (subscribers dedupe
    by the post id inside the payload)."""
    svc, _ws, space_seed, admin_seed = await _anon_relay_env(gfs_db, space_id="sp-self")
    ts = _now_iso()
    sub_sig = _sign(
        admin_seed,
        {
            "action": "subscribe",
            "instance_id": "admin-a",
            "space_id": "sp-self",
            "ts": ts,
        },
    )
    await svc.subscribe("admin-a", "sp-self", ts, sub_sig)
    payload = _authority_payload(space_seed, space_id="sp-self")
    sig = _legacy_transport_sig(
        admin_seed,
        space_id="sp-self",
        event_type="space_post_public",
        payload=payload,
        from_instance="admin-a",
    )
    delivered = await svc.publish_event(
        "sp-self", "space_post_public", payload, "admin-a", sig
    )
    assert sorted(delivered) == ["admin-a", "sub-a"]


@pytest.mark.security
async def test_publish_event_legacy_from_instance_never_logged(gfs_db, caplog):
    """No ``socialhome.global_server.*`` log record emitted during a legacy
    publish may contain the relaying household's id."""
    svc, _ws, space_seed, admin_seed = await _anon_relay_env(gfs_db, space_id="sp-log")
    payload = _authority_payload(space_seed, space_id="sp-log")
    sig = _legacy_transport_sig(
        admin_seed,
        space_id="sp-log",
        event_type="space_post_public",
        payload=payload,
        from_instance="admin-a",
    )
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="socialhome.global_server"):
        delivered = await svc.publish_event(
            "sp-log", "space_post_public", payload, "admin-a", sig
        )
    assert delivered == ["sub-a"]
    leaked = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name.startswith("socialhome.global_server")
        and "admin-a" in rec.getMessage()
    ]
    assert leaked == [], f"relaying household id leaked into logs: {leaked}"


# ── Fan-out concurrency ────────────────────────────────────────────────────────


class _SlowWsRegistry:
    """A ws-registry stub whose ``send`` takes *delay* seconds, recording the
    peak number of concurrently in-flight deliveries."""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.in_flight = 0
        self.peak = 0
        self.sent: list[str] = []

    async def send(self, instance_id: str, frame: dict) -> bool:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            self.sent.append(instance_id)
            return True
        finally:
            self.in_flight -= 1


async def test_fan_out_is_bounded_concurrent(gfs_db):
    """Sequential delivery let ONE accepted publish pin a request handler for
    N × the per-target timeout — an amplification handle for an anonymous
    caller. Delivery is concurrent, but bounded so a huge subscriber list can't
    open unbounded sockets."""
    ws = _SlowWsRegistry(delay=0.05)
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), ws_registry=ws)
    subs = [
        GfsSubscriber(instance_id=f"sub-{i}", inbox_url=f"http://s{i}/inbox")
        for i in range(24)
    ]
    started = asyncio.get_running_loop().time()
    delivered = await svc._fan_out(subs, {"space_id": "sp", "event_type": "e"}, None)
    elapsed = asyncio.get_running_loop().time() - started

    assert delivered == [s.instance_id for s in subs]
    assert ws.peak <= federation_mod.FAN_OUT_CONCURRENCY
    assert ws.peak > 1, "delivery is still sequential"
    # 24 targets at 50 ms each: sequential is ≥ 1.2 s, bounded-concurrent ≈ 0.15 s.
    assert elapsed < 0.6, elapsed


# ── Replay dedupe: content-blind payload idempotency ───────────────────────────


def test_seen_payload_cache_digest_is_canonical():
    """Key insertion order must not change the digest — the authority signature
    is computed over the SAME canonicalisation, so equal payload bytes imply
    equal signing input."""
    a = federation_mod.SeenPayloadCache.digest({"b": 1, "a": {"y": 2, "x": 3}})
    b = federation_mod.SeenPayloadCache.digest({"a": {"x": 3, "y": 2}, "b": 1})
    assert a == b
    assert len(a) == 32
    assert a != federation_mod.SeenPayloadCache.digest({"b": 1, "a": {"x": 3}})


def test_seen_payload_cache_records_and_expires():
    cache = federation_mod.SeenPayloadCache(ttl_s=300.0, cap=10)
    key = federation_mod.SeenPayloadCache.digest({"x": 1})
    assert cache.seen(key, now=1000.0) is False
    cache.record(key, now=1000.0)
    assert cache.seen(key, now=1000.0) is True
    assert cache.seen(key, now=1299.0) is True
    # Past the TTL the entry is gone — a later legitimate re-publish of the
    # same bytes fans out again.
    assert cache.seen(key, now=1301.0) is False
    assert len(cache) == 0


def test_seen_payload_cache_evicts_oldest_past_the_cap():
    cache = federation_mod.SeenPayloadCache(ttl_s=300.0, cap=3)
    keys = [federation_mod.SeenPayloadCache.digest({"i": i}) for i in range(5)]
    for i, key in enumerate(keys):
        cache.record(key, now=1000.0 + i)
    assert len(cache) == 3
    # The two oldest were evicted; the three newest survive.
    assert cache.seen(keys[0], now=1005.0) is False
    assert cache.seen(keys[1], now=1005.0) is False
    for key in keys[2:]:
        assert cache.seen(key, now=1005.0) is True


@pytest.mark.security
async def test_publish_event_identical_payload_is_an_idempotent_no_op(gfs_db):
    """Replaying a captured relay frame must NOT re-fan-out. The authority sig
    carries no nonce/timestamp, so without this the same 256 KiB body could be
    re-POSTed 120x/min per IP and multiplied by every subscriber."""
    svc, ws, space_seed, _admin_seed = await _anon_relay_env(
        gfs_db, space_id="sp-replay"
    )
    payload = _authority_payload(space_seed, space_id="sp-replay")

    first = await svc.publish_event("sp-replay", "space_post_public", payload)
    assert first == ["sub-a"]
    assert len(ws.sent) == 1

    second = await svc.publish_event("sp-replay", "space_post_public", payload)
    assert second == []
    assert len(ws.sent) == 1, "a replayed payload must not reach any subscriber"


async def test_publish_event_distinct_payload_still_fans_out(gfs_db):
    """The dedupe is per-payload, not a global mute — a second, different post
    relays normally."""
    svc, ws, space_seed, _admin_seed = await _anon_relay_env(
        gfs_db, space_id="sp-distinct"
    )
    for blob in ("blob-one", "blob-two"):
        payload = {"ciphertext": blob}
        payload.update(
            _sign_authority(space_seed, space_id="sp-distinct", payload=payload)
        )
        delivered = await svc.publish_event("sp-distinct", "space_post_public", payload)
        assert delivered == ["sub-a"]
    assert len(ws.sent) == 2


@pytest.mark.security
async def test_publish_event_rejected_payload_is_not_recorded(gfs_db):
    """The digest is recorded only AFTER authorization succeeds. Otherwise a
    rejected payload would poison the cache and mute the legitimate relay of
    the very same bytes once the space heals its pin."""
    ws = _RecordingWsRegistry()
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), ws_registry=ws)
    owner_seed, owner_pk = _make_keypair()
    space_seed, space_pk = _make_keypair()
    sub_seed, sub_pk = _make_keypair()
    await svc.register_instance(
        "owner-h", owner_pk.hex(), "http://owner-h.example.com/wh", auto_accept=True
    )
    await svc.register_instance(
        "sub-h", sub_pk.hex(), "http://sub-h.example.com/wh", auto_accept=True
    )
    # Published with NO pinned authority key -> every relay is rejected.
    await _publish_known_space(
        svc, owner_seed, owning_instance="owner-h", space_id="sp-heal"
    )
    ts = _now_iso()
    sig = _sign(
        sub_seed,
        {
            "action": "subscribe",
            "instance_id": "sub-h",
            "space_id": "sp-heal",
            "ts": ts,
        },
    )
    await svc.subscribe("sub-h", "sp-heal", ts, sig)
    ws.sent.clear()  # drop the owner's new_subscriber notify

    payload = _authority_payload(space_seed, space_id="sp-heal")
    with pytest.raises(PermissionError):
        await svc.publish_event("sp-heal", "space_post_public", payload)

    # The owner re-publishes, pinning the space authority key.
    await _publish_known_space(
        svc,
        owner_seed,
        owning_instance="owner-h",
        space_id="sp-heal",
        identity_public_key=space_pk.hex(),
    )
    delivered = await svc.publish_event("sp-heal", "space_post_public", payload)
    assert delivered == ["sub-h"]
    assert len(ws.sent) == 1


# ── Fan-out deadline ──────────────────────────────────────────────────────────


async def test_fan_out_returns_within_the_deadline_with_a_partial_list(monkeypatch):
    """Without the deadline a fan-out to N unreachable subscribers pins the
    request handler for ``ceil(N / FAN_OUT_CONCURRENCY) x FAN_OUT_TIMEOUT``.
    With it the handler returns what it delivered and the stragglers are
    cancelled."""
    monkeypatch.setattr(federation_mod, "FAN_OUT_DEADLINE_SECONDS", 2.0)
    ws = _SlowWsRegistry(delay=1.0)
    svc = GfsFederationService(object(), ws_registry=ws)
    subscribers = [
        GfsSubscriber(instance_id=f"sub-{i}", inbox_url=f"http://s{i}.invalid/wh")
        for i in range(33)
    ]

    loop = asyncio.get_running_loop()
    started = loop.time()
    delivered = await svc._fan_out(subscribers, {"space_id": "sp", "payload": {}}, None)
    elapsed = loop.time() - started

    # 33 subscribers / concurrency 8 = 5 rounds x 1 s ~= 5 s without the bound.
    assert 1.9 <= elapsed < 4.0, elapsed
    assert 0 < len(delivered) < 33
    # The partial result keeps subscriber order.
    reached = set(delivered)
    assert delivered == [s.instance_id for s in subscribers if s.instance_id in reached]


def test_fan_out_deadline_stays_under_the_household_publish_timeout():
    """The household POSTs ``/gfs/publish`` under
    ``aiohttp.ClientTimeout(total=10)``
    (``socialhome.services.gfs_connection_service``). The GFS deadline must sit
    BELOW that so the SERVER decides when a slow fan-out ends and answers 200
    with a partial delivery — a client-side timeout instead leaves the client
    believing the relay failed while the GFS has already recorded the payload
    digest, so a retry of the identical bytes would be suppressed for
    ``PUBLISH_REPLAY_TTL_S``."""
    assert federation_mod.FAN_OUT_DEADLINE_SECONDS < 10


async def test_fan_out_under_the_deadline_delivers_everything():
    """The deadline is a ceiling, not a throttle — a healthy fan-out is
    unaffected and still returns every subscriber, in order."""
    ws = _SlowWsRegistry(delay=0.0)
    svc = GfsFederationService(object(), ws_registry=ws)
    subscribers = [
        GfsSubscriber(instance_id=f"sub-{i}", inbox_url=f"http://s{i}.invalid/wh")
        for i in range(20)
    ]
    delivered = await svc._fan_out(subscribers, {"space_id": "sp", "payload": {}}, None)
    assert delivered == [s.instance_id for s in subscribers]


# ── Timing-uniform authority rejections ───────────────────────────────────────


@pytest.fixture
def verify_calls(monkeypatch):
    """Record module-level ``verify_ed25519`` calls made by ``federation``.

    The REAL authority verification runs inside ``socialhome.authority_sig``,
    so anything this records on the authority branch is the deliberate dummy
    burn that keeps every early rejection as expensive as a bad-signature one.
    Tests clear the list after fixture setup (``publish_space`` verifies the
    owner's signature through this same module-level name).
    """
    calls: list[tuple] = []
    real = federation_mod.verify_ed25519

    def _counting(public_key, message, signature):
        calls.append((public_key, message, signature))
        return real(public_key, message, signature)

    monkeypatch.setattr(federation_mod, "verify_ed25519", _counting)
    return calls


@pytest.mark.security
async def test_unpublished_space_burns_a_dummy_verify(gfs_db, verify_calls):
    svc, _ws, _seed, _admin = await _anon_relay_env(gfs_db, space_id="sp-uniform")
    payload = _authority_payload(_seed, space_id="sp-uniform")
    verify_calls.clear()
    with pytest.raises(PermissionError, match="space not published"):
        await svc.publish_event("sp-missing", "space_post_public", payload)
    assert len(verify_calls) == 1


@pytest.mark.security
async def test_banned_space_burns_a_dummy_verify(gfs_db, verify_calls):
    svc, _ws, space_seed, _admin = await _anon_relay_env(gfs_db, space_id="sp-ban-t")
    await svc._repo.set_space_status("sp-ban-t", "banned")
    payload = _authority_payload(space_seed, space_id="sp-ban-t")
    verify_calls.clear()
    with pytest.raises(PermissionError, match="banned"):
        await svc.publish_event("sp-ban-t", "space_post_public", payload)
    assert len(verify_calls) == 1


@pytest.mark.security
async def test_disallowed_event_type_burns_a_dummy_verify(gfs_db, verify_calls):
    svc, _ws, space_seed, _admin = await _anon_relay_env(gfs_db, space_id="sp-type-t")
    payload = _authority_payload(space_seed, space_id="sp-type-t")
    verify_calls.clear()
    with pytest.raises(PermissionError, match="event types"):
        await svc.publish_event("sp-type-t", "space_admin_action", payload)
    assert len(verify_calls) == 1


@pytest.mark.security
async def test_missing_authority_sig_burns_a_dummy_verify(gfs_db, verify_calls):
    svc, _ws, _space_seed, _admin = await _anon_relay_env(gfs_db, space_id="sp-nosig-t")
    verify_calls.clear()
    with pytest.raises(PermissionError, match="missing space-authority signature"):
        await svc.publish_event("sp-nosig-t", "space_post_public", {"ciphertext": "x"})
    assert len(verify_calls) == 1


@pytest.mark.security
async def test_non_dict_payload_burns_a_dummy_verify(gfs_db, verify_calls):
    svc, _ws, _space_seed, _admin = await _anon_relay_env(
        gfs_db, space_id="sp-nondict-t"
    )
    verify_calls.clear()
    with pytest.raises(PermissionError, match="missing space-authority signature"):
        await svc.publish_event("sp-nondict-t", "space_post_public", "not-a-dict")
    assert len(verify_calls) == 1


@pytest.mark.security
async def test_unpinned_key_burns_a_dummy_verify(svc, verify_calls):
    owner_seed, owner_pk = _make_keypair()
    space_seed, _space_pk = _make_keypair()
    await svc.register_instance(
        "owner-nopin-t", owner_pk.hex(), "http://nopin.example.com/wh", auto_accept=True
    )
    # Published with NO ``identity_public_key`` → nothing to verify against.
    await _publish_known_space(
        svc, owner_seed, owning_instance="owner-nopin-t", space_id="sp-nopin-t"
    )
    payload = _authority_payload(space_seed, space_id="sp-nopin-t")
    verify_calls.clear()
    with pytest.raises(PermissionError, match="no pinned authority key"):
        await svc.publish_event("sp-nopin-t", "space_post_public", payload)
    assert len(verify_calls) == 1


@pytest.mark.security
async def test_unknown_suite_burns_a_dummy_verify(gfs_db, verify_calls):
    svc, _ws, space_seed, _admin = await _anon_relay_env(gfs_db, space_id="sp-suite-t")
    payload = _authority_payload(space_seed, space_id="sp-suite-t")
    payload["authority_sig_suite"] = "ed25519+future-pq"
    verify_calls.clear()
    with pytest.raises(PermissionError, match="unknown authority signature suite"):
        await svc.publish_event("sp-suite-t", "space_post_public", payload)
    assert len(verify_calls) == 1


@pytest.mark.security
async def test_malformed_pinned_key_burns_a_dummy_verify(svc, verify_calls):
    owner_seed, owner_pk = _make_keypair()
    space_seed, _space_pk = _make_keypair()
    await svc.register_instance(
        "owner-hex-t", owner_pk.hex(), "http://hex.example.com/wh", auto_accept=True
    )
    await _publish_known_space(
        svc,
        owner_seed,
        owning_instance="owner-hex-t",
        space_id="sp-hex-t",
        identity_public_key="zz-not-hex",
    )
    payload = _authority_payload(space_seed, space_id="sp-hex-t")
    verify_calls.clear()
    with pytest.raises(PermissionError, match="invalid authority key"):
        await svc.publish_event("sp-hex-t", "space_post_public", payload)
    assert len(verify_calls) == 1
