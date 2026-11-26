"""Tests for GfsFederationService — register, subscribe, publish with real SQLite."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from socialhome.global_server import federation as federation_mod
from socialhome.global_server.addressee import GfsAddressee
from socialhome.global_server.federation import GfsFederationService
from socialhome.global_server.repositories import SqliteGfsFederationRepo
from socialhome.space_join_contact import sign_join_contact

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
    return datetime.now(UTC).isoformat()


#: The identity key of the GFS under test — what a household binds into a
#: request as ``gfs_key`` — and another server's.
_GFS_KEY = "5a" * 32
_OTHER_GFS_KEY = "6b" * 32

# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
async def svc(gfs_db):
    """A GfsFederationService backed by the shared GFS database fixture."""
    repo = SqliteGfsFederationRepo(gfs_db)
    service = GfsFederationService(repo)
    service.attach_addressee(
        GfsAddressee("gfs-test", (), public_key_hex=_GFS_KEY, node_id="n1")
    )
    return service


# ── Tests ──────────────────────────────────────────────────────────────────────


async def test_register_instance_succeeds(svc):
    """register_instance() persists the instance without raising."""
    await svc.register_instance("inst-1", "aa" * 32)
    spaces = await svc.list_spaces()
    assert isinstance(spaces, list)


async def test_register_instance_idempotent(svc):
    """Calling register_instance() twice with the same id updates the record."""
    await svc.register_instance("inst-dup", "aa" * 32)
    await svc.register_instance("inst-dup", "bb" * 32)


async def test_register_instance_persists_keywrap_pubkey(svc):
    """The key-wrap pubkey + KEM suite ride registration and round-trip."""
    await svc.register_instance(
        "inst-kw",
        "aa" * 32,
        keywrap_public_key="cc" * 32,
        kem_suite="x25519",
    )
    inst = await svc._repo.get_instance("inst-kw")
    assert inst is not None
    assert inst.keywrap_public_key == "cc" * 32
    assert inst.kem_suite == "x25519"


async def test_register_instance_without_keywrap_defaults_empty(svc):
    """An older HFS that ships no key-wrap pubkey → empty fields, no crash."""
    await svc.register_instance("inst-old", "aa" * 32)
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
    join_mode: str = "open",
    allow_subscribers: bool | None = True,
):
    """Publish a minimal active space so a subscribe target exists.

    ``identity_public_key`` (hex) is the space's Ed25519 authority verify key;
    when supplied it is TOFU-pinned on the GFS row so authority-signed relays
    can be verified. Empty (default) mirrors an older HFS that ships none.

    ``allow_subscribers`` defaults to ``True`` because most of these tests
    need a space a household may actually subscribe to — with the flag off
    the space is listed but NOT publicly readable, so the GFS refuses to seat
    a subscriber on it. ``None`` omits the key entirely (and from the signed
    bytes), mirroring an older household.

    ``join_mode`` is the orthogonal MEMBERSHIP gate; it no longer affects
    whether a subscribe is accepted.
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
        "join_mode": join_mode,
    }
    if allow_subscribers is not None:
        body["allow_subscribers"] = bool(allow_subscribers)
    sig = _sign(seed, body)
    await svc.publish_space(
        space_id=space_id,
        owning_instance=owning_instance,
        name="Known",
        signature=sig,
        identity_public_key=identity_public_key,
        join_mode=join_mode,
        allow_subscribers=allow_subscribers,
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
    await svc.register_instance("inst-ps", pk.hex(), auto_accept=True)
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
    await svc.register_instance("inst-ps2", pk.hex())
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
    await svc.register_instance("inst-ps3", pk.hex())
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
    await svc.register_instance("inst-ps4", pk.hex())
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
    await svc.register_instance("owner-inst", owner_pk.hex(), auto_accept=True)
    await svc.register_instance(
        "attacker-inst",
        attacker_pk.hex(),
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
    await svc.register_instance("owner2-inst", pk.hex(), auto_accept=True)
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
    await svc.register_instance("inst-pin", pk.hex(), auto_accept=True)
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
    await svc.register_instance("inst-pin2", pk.hex(), auto_accept=True)
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
    await svc.register_instance("inst-pin3", pk.hex(), auto_accept=True)
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


# ── update_instance (signed display-name change) ────────────────────────────────


async def test_update_instance_renames_with_valid_signature(svc):
    """update_instance() persists a new display_name when the signature
    verifies against the registered public key and the ts is fresh."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-rename", pk.hex(), display_name="Old")
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
    await svc.register_instance("inst-badsig2", pk.hex())
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
    await svc.register_instance("inst-nosig2", pk.hex())
    ts = _now_iso()
    with pytest.raises(PermissionError):
        await svc.update_instance("inst-nosig2", "Name", ts, "")


async def test_update_instance_stale_timestamp_raises_permission_error(svc):
    """A ts older than the 300s replay window is rejected."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-stale", pk.hex())
    ts = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    sig = _sign(
        seed,
        {"instance_id": "inst-stale", "display_name": "Late", "ts": ts},
    )
    with pytest.raises(PermissionError, match="Stale timestamp"):
        await svc.update_instance("inst-stale", "Late", ts, sig)


async def test_update_instance_unparseable_timestamp_raises_permission_error(svc):
    """A ts that isn't ISO 8601 is rejected (treated as stale/invalid)."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-badts", pk.hex())
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
    await svc.register_instance("inst-empty", pk.hex())
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
    await svc.register_instance("inst-long", pk.hex())
    ts = _now_iso()
    name = "y" * 81
    sig = _sign(
        seed,
        {"instance_id": "inst-long", "display_name": name, "ts": ts},
    )
    with pytest.raises(ValueError, match="1-80"):
        await svc.update_instance("inst-long", name, ts, sig)


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
    await svc.register_instance("inst-h1", pk.hex(), auto_accept=True)
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
    await svc.register_instance("inst-h2", pk.hex(), auto_accept=True)
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
    await svc.register_instance("inst-h3", owner_pk.hex(), auto_accept=True)
    await svc.register_instance("inst-evil", other_pk.hex(), auto_accept=True)
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
    await svc.register_instance("inst-h4", pk.hex(), auto_accept=True)
    await _publish_known_space(
        svc, seed, owning_instance="inst-h4", space_id="space-h4"
    )
    ts = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
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
    await svc.register_instance("inst-h6", pk.hex(), auto_accept=True)
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


# ── v_44: owner-certified re-pin of the space authority key ────────────────────


def _owner_identity():
    """A household identity whose instance id DERIVES from its key, as the
    cert binding requires (the older tests above use free-form ids)."""
    from socialhome.crypto import derive_instance_id

    seed, pk = _make_keypair()
    return seed, pk, derive_instance_id(pk)


def _rot_cert(
    owner_seed: bytes,
    owner_pk: bytes,
    owner_id: str,
    *,
    space_id: str,
    authority_pk_hex: str,
    epoch: int,
) -> dict:
    from socialhome.authority_cert import sign_authority_cert

    return sign_authority_cert(
        space_id=space_id,
        owner_instance_id=owner_id,
        owner_seed=owner_seed,
        owner_pk_hex=owner_pk.hex(),
        authority_pk_hex=authority_pk_hex,
        key_epoch=epoch,
    )


async def _publish_with_cert(
    svc,
    seed: bytes,
    *,
    owning_instance: str,
    space_id: str,
    identity_public_key: str,
    authority_cert: dict | None,
    ts: str | None = None,
):
    body = _publish_space_args(
        owning_instance, space_id, identity_public_key=identity_public_key
    )
    ts = _now_iso() if ts is None else ts
    if ts:
        body["ts"] = ts
    if authority_cert is not None:
        body["authority_cert"] = authority_cert
    return await svc.publish_space(
        space_id=space_id,
        owning_instance=owning_instance,
        name="My Space",
        signature=_sign(seed, body),
        identity_public_key=identity_public_key,
        authority_cert=authority_cert,
        ts=ts,
    )


async def _rotation_world(svc, space_id: str = "sp-rot"):
    seed, pk, owner_id = _owner_identity()
    await svc.register_instance(owner_id, pk.hex(), auto_accept=True)
    k1_seed, k1_pk = _make_keypair()
    await _publish_with_cert(
        svc,
        seed,
        owning_instance=owner_id,
        space_id=space_id,
        identity_public_key=k1_pk.hex(),
        authority_cert=None,
    )
    return seed, pk, owner_id, k1_seed, k1_pk


async def test_publish_with_owner_cert_repins_the_authority_key(svc):
    seed, pk, owner_id, _k1s, _k1_pk = await _rotation_world(svc)
    _k2_seed, k2_pk = _make_keypair()
    cert = _rot_cert(
        seed, pk, owner_id, space_id="sp-rot", authority_pk_hex=k2_pk.hex(), epoch=1
    )
    await _publish_with_cert(
        svc,
        seed,
        owning_instance=owner_id,
        space_id="sp-rot",
        identity_public_key=k2_pk.hex(),
        authority_cert=cert,
    )
    row = await svc.get_space("sp-rot")
    assert row.identity_public_key == k2_pk.hex()
    assert row.authority_cert == cert


async def _publish_asking(svc, seed, owner_id, space_seed, space_pk, cert):
    """An "ask to join" publish: the join contact signed with ``space_seed``."""
    contact = sign_join_contact(
        space_id="sp-ask",
        instance_id=owner_id,
        identity_pk="aa" * 32,
        keywrap_pk="bb" * 32,
        keywrap_sig="sig",
        space_seed=space_seed,
    )
    ts = _now_iso()
    body = _publish_space_args(owner_id, "sp-ask", identity_public_key=space_pk.hex())
    body |= {"join_mode": "request", "ts": ts, "join_contact": contact}
    if cert is not None:
        body["authority_cert"] = cert
    row = await svc.publish_space(
        space_id="sp-ask",
        owning_instance=owner_id,
        name="My Space",
        signature=_sign(seed, body),
        identity_public_key=space_pk.hex(),
        join_mode="request",
        join_contact=contact,
        authority_cert=cert,
        ts=ts,
    )
    return row, contact


async def test_a_repinning_publish_keeps_the_join_contact_of_the_new_key(svc, caplog):
    """The publish that re-pins a space carries the owner's join contact
    signed with the NEW key. It was checked against the key pinned BEFORE the
    re-pin, dropped ("does not verify"), and the listing lost its contact and
    with it the owner's cert — so every household that had pinned the space
    saw a new key without a chain and refused, for good, to ask to join
    through the GFS."""
    seed, pk, owner_id = _owner_identity()
    await svc.register_instance(owner_id, pk.hex(), auto_accept=True)
    k1_seed, k1_pk = _make_keypair()
    row, contact1 = await _publish_asking(svc, seed, owner_id, k1_seed, k1_pk, None)
    assert row.join_contact == contact1
    k2_seed, k2_pk = _make_keypair()
    cert = _rot_cert(
        seed, pk, owner_id, space_id="sp-ask", authority_pk_hex=k2_pk.hex(), epoch=1
    )
    with caplog.at_level(logging.WARNING):
        row, contact2 = await _publish_asking(svc, seed, owner_id, k2_seed, k2_pk, cert)
    assert row.identity_public_key == k2_pk.hex()
    assert row.join_contact == contact2
    stored = await svc.get_space("sp-ask")
    assert stored.join_contact == contact2
    assert stored.authority_cert == cert
    assert "does not verify" not in caplog.text


async def test_a_contact_of_the_old_key_is_not_served_with_the_new_pin(svc):
    """The reverse: a re-pinning publish whose contact is still signed with
    the RETIRED key serves none — a reader would refuse it."""
    seed, pk, owner_id = _owner_identity()
    await svc.register_instance(owner_id, pk.hex(), auto_accept=True)
    k1_seed, k1_pk = _make_keypair()
    await _publish_asking(svc, seed, owner_id, k1_seed, k1_pk, None)
    _k2_seed, k2_pk = _make_keypair()
    cert = _rot_cert(
        seed, pk, owner_id, space_id="sp-ask", authority_pk_hex=k2_pk.hex(), epoch=1
    )
    row, _contact = await _publish_asking(svc, seed, owner_id, k1_seed, k2_pk, cert)
    assert row.identity_public_key == k2_pk.hex()
    assert row.join_contact is None
    assert (await svc.get_space("sp-ask")).join_contact is None


async def test_a_repin_another_publish_already_made_keeps_the_contact(svc, monkeypatch):
    """Two publishes with the same cert race; the loser's compare-and-set
    does not move the pin — it already is the offered key. Its contact was
    judged against exactly that key, so it stays served. When the row pins
    ANOTHER key after a lost compare-and-set, the contact is not served."""
    seed, pk, owner_id = _owner_identity()
    await svc.register_instance(owner_id, pk.hex(), auto_accept=True)
    k1_seed, k1_pk = _make_keypair()
    await _publish_asking(svc, seed, owner_id, k1_seed, k1_pk, None)
    k2_seed, k2_pk = _make_keypair()
    cert = _rot_cert(
        seed, pk, owner_id, space_id="sp-ask", authority_pk_hex=k2_pk.hex(), epoch=1
    )
    real = SqliteGfsFederationRepo.set_space_authority
    won_by_other = {"pin": k2_pk.hex()}

    async def _lost(repo, space_id, **kw):
        # The concurrent publish lands first; ours then loses the CAS.
        if won_by_other["pin"] == k2_pk.hex():
            await real(repo, space_id, **kw)
        return False

    monkeypatch.setattr(SqliteGfsFederationRepo, "set_space_authority", _lost)
    row, contact = await _publish_asking(svc, seed, owner_id, k2_seed, k2_pk, cert)
    assert row.join_contact == contact
    assert (await svc.get_space("sp-ask")).join_contact == contact

    k3_seed, k3_pk = _make_keypair()
    cert3 = _rot_cert(
        seed, pk, owner_id, space_id="sp-ask", authority_pk_hex=k3_pk.hex(), epoch=2
    )
    won_by_other["pin"] = "someone else"
    row, _contact = await _publish_asking(svc, seed, owner_id, k3_seed, k3_pk, cert3)
    assert row.join_contact is None
    stored = await svc.get_space("sp-ask")
    assert stored.identity_public_key == k2_pk.hex()
    assert stored.join_contact is None


async def test_the_owners_same_cert_again_is_a_quiet_no_op(svc, caplog):
    """The owner re-publishes with the cert of the key already pinned on
    every reconnect and metadata change (it re-sends until a server
    confirmed the pin, and a restart forgets what was confirmed). That is no
    re-pin attempt: nothing moves, and nothing is logged at WARNING. An OLDER
    cert still is."""
    seed, pk, owner_id, _k1s, _k1_pk = await _rotation_world(svc)
    _k2_seed, k2_pk = _make_keypair()
    certs = [
        _rot_cert(
            seed,
            pk,
            owner_id,
            space_id="sp-rot",
            authority_pk_hex=k2_pk.hex(),
            epoch=5,
        )
        for _ in range(3)
    ]
    # The same cert twice, then one the owner signed again for the same key
    # and epoch (another ``issued_at``).
    for cert in (certs[0], certs[0], certs[2]):
        with caplog.at_level(logging.WARNING):
            await _publish_with_cert(
                svc,
                seed,
                owning_instance=owner_id,
                space_id="sp-rot",
                identity_public_key=k2_pk.hex(),
                authority_cert=cert,
            )
    row = await svc.get_space("sp-rot")
    assert (row.identity_public_key, row.authority_cert) == (k2_pk.hex(), certs[0])
    assert row.authority_rotation_seq == 1
    assert "not newer" not in caplog.text
    _k0_seed, k0_pk = _make_keypair()
    older = _rot_cert(
        seed, pk, owner_id, space_id="sp-rot", authority_pk_hex=k0_pk.hex(), epoch=4
    )
    with caplog.at_level(logging.WARNING):
        await _publish_with_cert(
            svc,
            seed,
            owning_instance=owner_id,
            space_id="sp-rot",
            identity_public_key=k0_pk.hex(),
            authority_cert=older,
        )
    assert (await svc.get_space("sp-rot")).identity_public_key == k2_pk.hex()
    assert "not newer" in caplog.text


@pytest.mark.parametrize(
    "forgery", ["other_household", "old_space_key", "wrong_owner_id"]
)
async def test_forged_cert_never_moves_the_pin(svc, forgery):
    seed, pk, owner_id, k1_seed, k1_pk = await _rotation_world(svc)
    evil_seed, evil_pk = _make_keypair()
    if forgery == "other_household":
        cert = _rot_cert(
            evil_seed,
            evil_pk,
            owner_id,
            space_id="sp-rot",
            authority_pk_hex=evil_pk.hex(),
            epoch=1,
        )
    elif forgery == "old_space_key":
        cert = _rot_cert(
            k1_seed,
            pk,
            owner_id,
            space_id="sp-rot",
            authority_pk_hex=evil_pk.hex(),
            epoch=1,
        )
    else:
        from socialhome.crypto import derive_instance_id

        cert = _rot_cert(
            evil_seed,
            evil_pk,
            derive_instance_id(evil_pk),
            space_id="sp-rot",
            authority_pk_hex=evil_pk.hex(),
            epoch=1,
        )
    await _publish_with_cert(
        svc,
        seed,
        owning_instance=owner_id,
        space_id="sp-rot",
        identity_public_key=evil_pk.hex(),
        authority_cert=cert,
    )
    row = await svc.get_space("sp-rot")
    assert row.identity_public_key == k1_pk.hex()
    assert row.authority_cert is None


async def test_cert_for_a_different_key_than_offered_is_refused(svc):
    seed, pk, owner_id, _k1s, k1_pk = await _rotation_world(svc)
    _k2s, k2_pk = _make_keypair()
    _k3s, k3_pk = _make_keypair()
    cert = _rot_cert(
        seed, pk, owner_id, space_id="sp-rot", authority_pk_hex=k2_pk.hex(), epoch=1
    )
    await _publish_with_cert(
        svc,
        seed,
        owning_instance=owner_id,
        space_id="sp-rot",
        identity_public_key=k3_pk.hex(),
        authority_cert=cert,
    )
    assert (await svc.get_space("sp-rot")).identity_public_key == k1_pk.hex()


async def test_replayed_older_cert_cannot_move_the_pin_back(svc):
    seed, pk, owner_id, _k1s, _k1 = await _rotation_world(svc)
    _k2s, k2_pk = _make_keypair()
    _k3s, k3_pk = _make_keypair()
    c1 = _rot_cert(
        seed, pk, owner_id, space_id="sp-rot", authority_pk_hex=k2_pk.hex(), epoch=1
    )
    c2 = _rot_cert(
        seed, pk, owner_id, space_id="sp-rot", authority_pk_hex=k3_pk.hex(), epoch=2
    )
    for cert, key in ((c1, k2_pk), (c2, k3_pk)):
        await _publish_with_cert(
            svc,
            seed,
            owning_instance=owner_id,
            space_id="sp-rot",
            identity_public_key=key.hex(),
            authority_cert=cert,
        )
    # Replaying the epoch-1 publish (fresh ts — the owner itself could not
    # be fooled into this, but a stale cert must never win on its own).
    await _publish_with_cert(
        svc,
        seed,
        owning_instance=owner_id,
        space_id="sp-rot",
        identity_public_key=k2_pk.hex(),
        authority_cert=c1,
    )
    row = await svc.get_space("sp-rot")
    assert row.identity_public_key == k3_pk.hex()
    assert row.authority_cert == c2


async def test_cert_requires_a_signed_fresh_ts(svc):
    seed, pk, owner_id, _k1s, k1_pk = await _rotation_world(svc)
    _k2s, k2_pk = _make_keypair()
    cert = _rot_cert(
        seed, pk, owner_id, space_id="sp-rot", authority_pk_hex=k2_pk.hex(), epoch=1
    )
    with pytest.raises(PermissionError):
        await _publish_with_cert(
            svc,
            seed,
            owning_instance=owner_id,
            space_id="sp-rot",
            identity_public_key=k2_pk.hex(),
            authority_cert=cert,
            ts="",
        )
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with pytest.raises(PermissionError):
        await _publish_with_cert(
            svc,
            seed,
            owning_instance=owner_id,
            space_id="sp-rot",
            identity_public_key=k2_pk.hex(),
            authority_cert=cert,
            ts=stale,
        )
    assert (await svc.get_space("sp-rot")).identity_public_key == k1_pk.hex()


async def test_cert_is_inside_the_signed_publish_body(svc):
    """A relay cannot bolt a cert onto an owner's publish body."""
    seed, pk, owner_id, _k1s, _k1 = await _rotation_world(svc)
    _k2s, k2_pk = _make_keypair()
    cert = _rot_cert(
        seed, pk, owner_id, space_id="sp-rot", authority_pk_hex=k2_pk.hex(), epoch=1
    )
    ts = _now_iso()
    body = _publish_space_args(owner_id, "sp-rot", identity_public_key=k2_pk.hex())
    body["ts"] = ts  # signed WITHOUT the cert
    with pytest.raises(PermissionError):
        await svc.publish_space(
            space_id="sp-rot",
            owning_instance=owner_id,
            name="My Space",
            signature=_sign(seed, body),
            identity_public_key=k2_pk.hex(),
            authority_cert=cert,
            ts=ts,
        )


async def test_first_publish_with_a_cert_stores_it(svc):
    """A GFS that first meets the space after a rotation stores the cert,
    so a replay of the older one is refused here too."""
    seed, pk, owner_id = _owner_identity()
    await svc.register_instance(owner_id, pk.hex(), auto_accept=True)
    _k2s, k2_pk = _make_keypair()
    cert = _rot_cert(
        seed, pk, owner_id, space_id="sp-new", authority_pk_hex=k2_pk.hex(), epoch=3
    )
    await _publish_with_cert(
        svc,
        seed,
        owning_instance=owner_id,
        space_id="sp-new",
        identity_public_key=k2_pk.hex(),
        authority_cert=cert,
    )
    row = await svc.get_space("sp-new")
    assert row.identity_public_key == k2_pk.hex()
    assert row.authority_cert == cert


# ─── ``withdrawn: true`` — a pin-only re-pin of a withdrawn listing ───────


async def _withdrawn_repin(
    svc,
    seed: bytes,
    *,
    owning_instance: str,
    space_id: str,
    identity_public_key: str,
    authority_cert: dict | None,
    ts: str | None = None,
    sign_withdrawn: bool = True,
    gfs_key: str | None = _GFS_KEY,
    sign_gfs_key: bool = True,
):
    """A ``withdrawn: true`` publish as a household sends it: BLANK metadata
    (a space that left the public tier must not leak its new name) plus the
    new key and its owner cert."""
    body = _publish_space_args(
        owning_instance, space_id, identity_public_key=identity_public_key
    )
    body["name"] = ""
    ts = _now_iso() if ts is None else ts
    if ts:
        body["ts"] = ts
    if authority_cert is not None:
        body["authority_cert"] = authority_cert
    if sign_withdrawn:
        body["withdrawn"] = True
    if gfs_key is not None and sign_gfs_key:
        body["gfs_key"] = gfs_key
    return await svc.publish_space(
        space_id=space_id,
        owning_instance=owning_instance,
        name="",
        signature=_sign(seed, body),
        identity_public_key=identity_public_key,
        authority_cert=authority_cert,
        ts=ts,
        withdrawn=True,
        gfs_key=gfs_key,
    )


async def _withdrawn_rotation_world(svc, space_id: str = "sp-rot"):
    """``_rotation_world`` whose owner then withdrew the listing."""
    seed, pk, owner_id, k1_seed, k1_pk = await _rotation_world(svc, space_id)
    await svc._repo.set_space_withdrawn(space_id, True)
    return seed, pk, owner_id, k1_seed, k1_pk


@pytest.mark.security
async def test_withdrawn_repin_moves_the_pin_and_keeps_the_listing_withdrawn(svc):
    seed, pk, owner_id, _k1s, _k1 = await _withdrawn_rotation_world(svc)
    before = await svc.get_space("sp-rot")
    _k2s, k2_pk = _make_keypair()
    cert = _rot_cert(
        seed, pk, owner_id, space_id="sp-rot", authority_pk_hex=k2_pk.hex(), epoch=1
    )
    returned = await _withdrawn_repin(
        svc,
        seed,
        owning_instance=owner_id,
        space_id="sp-rot",
        identity_public_key=k2_pk.hex(),
        authority_cert=cert,
    )
    row = await svc.get_space("sp-rot")
    assert returned == row
    assert row.identity_public_key == k2_pk.hex()
    assert row.authority_cert == cert
    assert row.withdrawn is True
    # The blank metadata the household sent on purpose is never stored.
    assert row.name == before.name == "My Space"
    # The same follow-up as a normal re-pin: this server's own counter moves.
    assert row.authority_rotation_seq == before.authority_rotation_seq + 1
    # ... and nothing else on the row does.
    assert row == replace(
        before,
        identity_public_key=k2_pk.hex(),
        authority_cert=cert,
        authority_rotation_seq=row.authority_rotation_seq,
    )


@pytest.mark.security
async def test_withdrawn_publish_for_an_unknown_space_creates_no_row(svc):
    seed, pk, owner_id = _owner_identity()
    await svc.register_instance(owner_id, pk.hex(), auto_accept=True)
    _k2s, k2_pk = _make_keypair()
    cert = _rot_cert(
        seed, pk, owner_id, space_id="sp-none", authority_pk_hex=k2_pk.hex(), epoch=1
    )
    with pytest.raises(federation_mod.SpaceNotListedError):
        await _withdrawn_repin(
            svc,
            seed,
            owning_instance=owner_id,
            space_id="sp-none",
            identity_public_key=k2_pk.hex(),
            authority_cert=cert,
        )
    assert await svc.get_space("sp-none") is None


@pytest.mark.security
async def test_withdrawn_publish_with_a_bad_signature_never_leaks_existence(svc):
    """An unsigned caller learns nothing: an unknown space answers the same
    PermissionError as a known one."""
    _seed, pk, owner_id = _owner_identity()
    evil_seed, _evil_pk = _make_keypair()
    await svc.register_instance(owner_id, pk.hex(), auto_accept=True)
    with pytest.raises(PermissionError) as exc:
        await _withdrawn_repin(
            svc,
            evil_seed,
            owning_instance=owner_id,
            space_id="sp-none",
            identity_public_key="",
            authority_cert=None,
        )
    assert not isinstance(exc.value, LookupError)
    assert await svc.get_space("sp-none") is None


@pytest.mark.security
async def test_withdrawn_publish_requires_a_signed_ts(svc):
    seed, _pk, owner_id, _k1s, k1_pk = await _withdrawn_rotation_world(svc)
    with pytest.raises(PermissionError, match="withdrawn requires a signed ts"):
        await _withdrawn_repin(
            svc,
            seed,
            owning_instance=owner_id,
            space_id="sp-rot",
            identity_public_key=k1_pk.hex(),
            authority_cert=None,
            ts="",
        )
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with pytest.raises(PermissionError, match="(?i)stale|fresh|timestamp|ts"):
        await _withdrawn_repin(
            svc,
            seed,
            owning_instance=owner_id,
            space_id="sp-rot",
            identity_public_key=k1_pk.hex(),
            authority_cert=None,
            ts=stale,
        )


@pytest.mark.security
async def test_withdrawn_publish_by_a_registered_non_owner_changes_nothing(svc):
    _seed, _pk, _owner_id, _k1s, _k1 = await _rotation_world(svc)
    before = await svc.get_space("sp-rot")
    evil_seed, evil_pk, evil_id = _owner_identity()
    await svc.register_instance(evil_id, evil_pk.hex(), auto_accept=True)
    _k2s, k2_pk = _make_keypair()
    cert = _rot_cert(
        evil_seed,
        evil_pk,
        evil_id,
        space_id="sp-rot",
        authority_pk_hex=k2_pk.hex(),
        epoch=1,
    )
    with pytest.raises(PermissionError, match="owned by another instance"):
        await _withdrawn_repin(
            svc,
            evil_seed,
            owning_instance=evil_id,
            space_id="sp-rot",
            identity_public_key=k2_pk.hex(),
            authority_cert=cert,
        )
    assert await svc.get_space("sp-rot") == before


class _RecordingInviteRepo:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_for_space(self, space_id: str) -> int:
        self.deleted.append(space_id)
        return 0


async def test_withdrawn_publish_withdraws_a_listed_space_and_a_fresh_one_relists(
    gfs_db,
):
    invites = _RecordingInviteRepo()
    svc = GfsFederationService(SqliteGfsFederationRepo(gfs_db), invite_repo=invites)
    svc.attach_addressee(
        GfsAddressee("gfs-test", (), public_key_hex=_GFS_KEY, node_id="n1")
    )
    seed, _pk, owner_id, _k1s, k1_pk = await _rotation_world(svc)
    assert (await svc.get_space("sp-rot")).withdrawn is False
    row = await _withdrawn_repin(
        svc,
        seed,
        owning_instance=owner_id,
        space_id="sp-rot",
        identity_public_key=k1_pk.hex(),
        authority_cert=None,
    )
    assert row.withdrawn is True
    assert (await svc.get_space("sp-rot")).withdrawn is True
    assert row.name == "My Space"
    # Exactly like an unpublish: the standing invite pages go down with it.
    assert invites.deleted == ["sp-rot"]
    # A second one never re-lists, and has no invite pages left to delete.
    await _withdrawn_repin(
        svc,
        seed,
        owning_instance=owner_id,
        space_id="sp-rot",
        identity_public_key=k1_pk.hex(),
        authority_cert=None,
    )
    assert (await svc.get_space("sp-rot")).withdrawn is True
    assert invites.deleted == ["sp-rot"]
    # Unchanged: a normal fresh publish is still the way back.
    await _publish_with_cert(
        svc,
        seed,
        owning_instance=owner_id,
        space_id="sp-rot",
        identity_public_key=k1_pk.hex(),
        authority_cert=None,
    )
    assert (await svc.get_space("sp-rot")).withdrawn is False


@pytest.mark.security
async def test_withdrawn_is_inside_the_signed_publish_body(svc):
    """A relay cannot turn an owner's normal publish into a pin-only one
    (nor the reverse): the flag is part of the signed bytes."""
    seed, pk, owner_id, _k1s, k1_pk = await _withdrawn_rotation_world(svc)
    _k2s, k2_pk = _make_keypair()
    cert = _rot_cert(
        seed, pk, owner_id, space_id="sp-rot", authority_pk_hex=k2_pk.hex(), epoch=1
    )
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await _withdrawn_repin(
            svc,
            seed,
            owning_instance=owner_id,
            space_id="sp-rot",
            identity_public_key=k2_pk.hex(),
            authority_cert=cert,
            sign_withdrawn=False,
        )
    assert (await svc.get_space("sp-rot")).identity_public_key == k1_pk.hex()


@pytest.mark.security
@pytest.mark.parametrize("flaw", ["stale_epoch", "wrong_signer", "no_cert"])
async def test_withdrawn_repin_with_an_unusable_cert_keeps_the_pin(svc, flaw):
    seed, pk, owner_id, _k1s, _k1 = await _withdrawn_rotation_world(svc)
    _k2s, k2_pk = _make_keypair()
    _k3s, k3_pk = _make_keypair()
    c2 = _rot_cert(
        seed, pk, owner_id, space_id="sp-rot", authority_pk_hex=k2_pk.hex(), epoch=2
    )
    await _withdrawn_repin(
        svc,
        seed,
        owning_instance=owner_id,
        space_id="sp-rot",
        identity_public_key=k2_pk.hex(),
        authority_cert=c2,
    )
    before = await svc.get_space("sp-rot")
    assert before.identity_public_key == k2_pk.hex()
    cert: dict | None
    if flaw == "stale_epoch":
        cert = _rot_cert(
            seed, pk, owner_id, space_id="sp-rot", authority_pk_hex=k3_pk.hex(), epoch=1
        )
    elif flaw == "wrong_signer":
        evil_seed, evil_pk = _make_keypair()
        cert = _rot_cert(
            evil_seed,
            evil_pk,
            owner_id,
            space_id="sp-rot",
            authority_pk_hex=k3_pk.hex(),
            epoch=3,
        )
    else:
        cert = None
    # Not an error: the publish is accepted and simply moves nothing.
    await _withdrawn_repin(
        svc,
        seed,
        owning_instance=owner_id,
        space_id="sp-rot",
        identity_public_key=k3_pk.hex(),
        authority_cert=cert,
    )
    assert await svc.get_space("sp-rot") == before


async def test_withdrawn_repin_never_lifts_a_ban(svc):
    seed, pk, owner_id, _k1s, _k1 = await _withdrawn_rotation_world(svc)
    await svc._repo.set_space_status("sp-rot", "banned")
    _k2s, k2_pk = _make_keypair()
    cert = _rot_cert(
        seed, pk, owner_id, space_id="sp-rot", authority_pk_hex=k2_pk.hex(), epoch=1
    )
    row = await _withdrawn_repin(
        svc,
        seed,
        owning_instance=owner_id,
        space_id="sp-rot",
        identity_public_key=k2_pk.hex(),
        authority_cert=cert,
    )
    assert (row.status, row.identity_public_key) == ("banned", k2_pk.hex())


# ── The addressed server is inside the signed bytes ──────────────────────


async def _repin_args(svc):
    seed, pk, owner_id, _k1s, k1_pk = await _withdrawn_rotation_world(svc)
    _k2s, k2_pk = _make_keypair()
    cert = _rot_cert(
        seed, pk, owner_id, space_id="sp-rot", authority_pk_hex=k2_pk.hex(), epoch=1
    )
    return seed, owner_id, k1_pk, k2_pk, cert


@pytest.mark.security
@pytest.mark.parametrize("gfs_key", [None, _OTHER_GFS_KEY])
async def test_a_withdrawn_publish_for_another_server_is_refused(svc, gfs_key):
    """A pin-only publish also withdraws a listing. One the owner sent to
    ANOTHER server (where the space is withdrawn) must not be replayable
    here, where the space may be listed: the addressed server's key is
    inside the signed bytes, and mandatory."""
    seed, owner_id, k1_pk, k2_pk, cert = await _repin_args(svc)
    await svc._repo.set_space_withdrawn("sp-rot", False)  # live here
    with pytest.raises(PermissionError):
        await _withdrawn_repin(
            svc,
            seed,
            owning_instance=owner_id,
            space_id="sp-rot",
            identity_public_key=k2_pk.hex(),
            authority_cert=cert,
            gfs_key=gfs_key,
        )
    row = await svc.get_space("sp-rot")
    assert row.withdrawn is False
    assert row.identity_public_key == k1_pk.hex()


@pytest.mark.security
async def test_the_addressed_server_is_inside_the_signed_publish_body(svc):
    seed, owner_id, _k1, k2_pk, cert = await _repin_args(svc)
    with pytest.raises(PermissionError, match="Invalid Ed25519 signature"):
        await _withdrawn_repin(
            svc,
            seed,
            owning_instance=owner_id,
            space_id="sp-rot",
            identity_public_key=k2_pk.hex(),
            authority_cert=cert,
            sign_gfs_key=False,
        )


@pytest.mark.security
async def test_a_withdrawal_addressed_to_another_server_is_refused(svc):
    """The same replay at withdrawal time: an unpublish the owner sent to
    one server must not delist the space on another."""
    seed, pk = _make_keypair()
    await svc.register_instance("inst-ha", pk.hex(), auto_accept=True)
    await _publish_known_space(svc, seed, owning_instance="inst-ha", space_id="sp-ha")
    ts = _now_iso()
    signed = {
        "action": "unpublish",
        "owning_instance": "inst-ha",
        "space_id": "sp-ha",
        "ts": ts,
    }
    with pytest.raises(PermissionError):
        await svc.hide_space(
            "sp-ha",
            "inst-ha",
            ts,
            _sign(seed, {**signed, "gfs_key": _OTHER_GFS_KEY}),
            gfs_key=_OTHER_GFS_KEY,
        )
    # The key is inside the signed bytes: bolting ours onto a body signed
    # without it fails the signature.
    with pytest.raises(PermissionError):
        await svc.hide_space(
            "sp-ha", "inst-ha", ts, _sign(seed, signed), gfs_key=_GFS_KEY
        )
    assert (await svc.get_space("sp-ha")).withdrawn is False
    await svc.hide_space(
        "sp-ha",
        "inst-ha",
        ts,
        _sign(seed, {**signed, "gfs_key": _GFS_KEY}),
        gfs_key=_GFS_KEY,
    )
    assert (await svc.get_space("sp-ha")).withdrawn is True


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
