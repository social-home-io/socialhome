"""Inbound authority gate for ``SPACE_KEY_EXCHANGE_REKEY`` (content-key
rotation).

The rekey ships a fresh AES-256 content key for the current epoch. Before
this gate, ``_on_key_exchange_rekey`` imported the key from ANY confirmed
peer with NO sender authentication. Combined with the Phase-4b
smallest-``rotated_by``-wins collision rule, that let any meshed peer (a
routing relay, or a removed-but-still-paired ex-member) pin the current
epoch onto an attacker-chosen key with ``rotated_by=""`` — a persistent
content-key hijack + DoS.

The fix mirrors the Phase-4a ``SPACE_CONFIG_CHANGED`` authority gate: a
rekey is a space-authority event, authenticated by an Ed25519 signature
produced with the space seed (held by the owner AND delegated admins), or
accepted from the owner host directly for back-compat. Trust is in the
SIGNATURE, not the sender. Fail-closed:

* a non-owner rekey with NO authority_sig → DROP;
* a present-but-invalid / wrong-key / unknown-suite sig → DROP (no
  fall-through to the owner gate);
* a degenerate ``rotated_by=""`` → DROP (a legit rotator stamps its real id);
* the owner host (``from_instance == owner_instance_id``) still applies an
  unsigned rekey (legacy back-compat);
* a delegated-admin authority-signed rekey (``from_instance != owner``)
  applies.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from socialhome.crypto import generate_space_keypair
from socialhome.domain.federation import FederationEventType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.federation.private_invite_handler import PrivateSpaceInviteHandler
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_crypto_service import (
    sign_authority_event,
    strip_authority_sig_fields,
)


OWNER = "owner-instance"
RELAY = "some-relay-instance"
ADMIN = "delegated-admin-instance"
SPACE_ID = "sp-rekey"


def _event(event_type, payload: dict, *, from_instance: str):
    return SimpleNamespace(
        event_type=event_type,
        payload=payload,
        from_instance=from_instance,
        space_id=SPACE_ID,
    )


def _key_b64(byte: int = 0xAA) -> str:
    return base64.b64encode(bytes([byte]) * 32).decode("ascii")


def _signed_rekey_payload(
    *,
    seed: bytes,
    epoch: int,
    rotated_by: str,
    key_byte: int = 0xAA,
) -> dict:
    """Build a SPACE_KEY_EXCHANGE_REKEY payload, authority-signing the inner
    ``space_content_key`` meta with ``seed`` (same shape the host emits)."""
    meta = {
        "epoch": epoch,
        "key_suite": "aesgcm-256",
        "key_base64": _key_b64(key_byte),
        "rotated_by": rotated_by,
    }
    signed = sign_authority_event(
        event_type="space_key_exchange_rekey",
        space_id=SPACE_ID,
        payload=strip_authority_sig_fields(meta),
        space_seed=seed,
    )
    meta.update(signed)
    return {"space_id": SPACE_ID, "space_content_key": meta}


async def _make_handler(tmp_dir):
    """Handler over a real space repo seeded with a local copy of the space
    whose ``identity_public_key`` matches a known space keypair. Returns
    ``(handler, space_crypto_mock, seed, db)``."""
    from socialhome.db.database import AsyncDatabase
    from socialhome.infrastructure.key_manager import KeyManager

    kp = generate_space_keypair()
    db = AsyncDatabase(tmp_dir / "rekey.db", batch_timeout_ms=10)
    await db.startup()
    space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x07" * 32))
    await space_repo.save(
        Space(
            id=SPACE_ID,
            name="S",
            owner_instance_id=OWNER,
            owner_username="anna",
            identity_public_key=kp.public_key.hex(),
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    space_crypto = AsyncMock()
    space_crypto.import_key = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=AsyncMock(),
        space_repo=space_repo,
        remote_member_repo=AsyncMock(),
        space_crypto_service=space_crypto,
    )
    return h, space_crypto, kp.private_key, db


# ── Hijack rejection (the defect) ──────────────────────────────────────────


@pytest.mark.security
async def test_unsigned_rekey_from_non_owner_relay_is_dropped(tmp_dir):
    """The hijack: a confirmed peer that is NOT the owner and carries NO
    authority signature must NOT import. Pre-fix this pinned the attacker's
    key onto the current epoch (smallest-``rotated_by`` wins)."""
    h, space_crypto, _seed, db = await _make_handler(tmp_dir)
    try:
        ev = _event(
            FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
            {
                "space_id": SPACE_ID,
                "space_content_key": {
                    "epoch": 5,
                    "key_suite": "aesgcm-256",
                    "key_base64": _key_b64(0xEE),
                    "rotated_by": "",  # sorts smaller than every real id
                },
            },
            from_instance=RELAY,
        )
        await h._on_key_exchange_rekey(ev)
        space_crypto.import_key.assert_not_awaited()
    finally:
        await db.shutdown()


@pytest.mark.security
async def test_degenerate_blank_rotated_by_is_dropped_even_when_signed(tmp_dir):
    """Defense-in-depth: a (validly) authority-signed rekey whose
    ``rotated_by`` is blank is rejected — the smallest-wins tiebreak must
    only ever compare NON-empty authenticated minter ids."""
    h, space_crypto, seed, db = await _make_handler(tmp_dir)
    try:
        payload = _signed_rekey_payload(seed=seed, epoch=5, rotated_by="")
        ev = _event(
            FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
            payload,
            from_instance=ADMIN,
        )
        await h._on_key_exchange_rekey(ev)
        space_crypto.import_key.assert_not_awaited()
    finally:
        await db.shutdown()


async def test_bad_sig_from_non_owner_drops_no_fallthrough(tmp_dir):
    """A present-but-invalid authority_sig from a non-owner DROPS — it never
    falls through to the owner back-compat gate."""
    h, space_crypto, _seed, db = await _make_handler(tmp_dir)
    try:
        wrong = generate_space_keypair().private_key
        payload = _signed_rekey_payload(seed=wrong, epoch=5, rotated_by="inst-attacker")
        ev = _event(
            FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
            payload,
            from_instance=RELAY,
        )
        await h._on_key_exchange_rekey(ev)
        space_crypto.import_key.assert_not_awaited()
    finally:
        await db.shutdown()


async def test_unknown_authority_suite_is_dropped(tmp_dir):
    """An unknown authority_sig_suite drops (crypto-suite rule, no default)."""
    h, space_crypto, seed, db = await _make_handler(tmp_dir)
    try:
        payload = _signed_rekey_payload(seed=seed, epoch=5, rotated_by="inst-x")
        payload["space_content_key"]["authority_sig_suite"] = "ed25519+future"
        ev = _event(
            FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
            payload,
            from_instance=ADMIN,
        )
        await h._on_key_exchange_rekey(ev)
        space_crypto.import_key.assert_not_awaited()
    finally:
        await db.shutdown()


# ── Legit paths still work ─────────────────────────────────────────────────


async def test_owner_unsigned_rekey_applies_backcompat(tmp_dir):
    """The owner host (from_instance == owner) applies an UNSIGNED rekey —
    legacy back-compat for pre-authority owners."""
    h, space_crypto, _seed, db = await _make_handler(tmp_dir)
    try:
        ev = _event(
            FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
            {
                "space_id": SPACE_ID,
                "space_content_key": {
                    "epoch": 7,
                    "key_suite": "aesgcm-256",
                    "key_base64": _key_b64(0x11),
                    "rotated_by": OWNER,
                },
            },
            from_instance=OWNER,
        )
        await h._on_key_exchange_rekey(ev)
        space_crypto.import_key.assert_awaited_once_with(
            SPACE_ID, 7, bytes([0x11]) * 32, rotated_by=OWNER, verified_pin=None
        )
    finally:
        await db.shutdown()


async def test_owner_unsigned_rekey_null_rotated_by_applies(tmp_dir):
    """The owner back-compat path accepts a NULL/absent ``rotated_by`` (older
    peers ship no minter) — only the *non-owner* path requires non-empty."""
    h, space_crypto, _seed, db = await _make_handler(tmp_dir)
    try:
        ev = _event(
            FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
            {
                "space_id": SPACE_ID,
                "space_content_key": {
                    "epoch": 7,
                    "key_suite": "aesgcm-256",
                    "key_base64": _key_b64(0x11),
                },
            },
            from_instance=OWNER,
        )
        await h._on_key_exchange_rekey(ev)
        space_crypto.import_key.assert_awaited_once_with(
            SPACE_ID, 7, bytes([0x11]) * 32, rotated_by=None, verified_pin=None
        )
    finally:
        await db.shutdown()


async def test_delegated_admin_authority_signed_rekey_applies(tmp_dir):
    """A delegated admin (from_instance != owner) whose rekey is authority-
    signed with the space seed APPLIES — this is the offline-owner path."""
    h, space_crypto, seed, db = await _make_handler(tmp_dir)
    try:
        payload = _signed_rekey_payload(
            seed=seed, epoch=9, rotated_by=ADMIN, key_byte=0x22
        )
        ev = _event(
            FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
            payload,
            from_instance=ADMIN,
        )
        await h._on_key_exchange_rekey(ev)
        space_crypto.import_key.assert_awaited_once_with(
            SPACE_ID, 9, bytes([0x22]) * 32, rotated_by=ADMIN, verified_pin=ANY
        )
    finally:
        await db.shutdown()


async def test_unknown_space_drops(tmp_dir):
    """A rekey for a space we have no local row for drops (no public key to
    verify against, and no owner to back-compat against)."""
    h, space_crypto, seed, db = await _make_handler(tmp_dir)
    try:
        payload = _signed_rekey_payload(seed=seed, epoch=9, rotated_by=ADMIN)
        payload["space_id"] = "sp-not-here"
        ev = _event(
            FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
            payload,
            from_instance=ADMIN,
        )
        ev.space_id = "sp-not-here"
        await h._on_key_exchange_rekey(ev)
        space_crypto.import_key.assert_not_awaited()
    finally:
        await db.shutdown()
