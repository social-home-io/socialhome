"""Receiver rules for an owner-signed authority cert (v_44, spec §4).

``apply_authority_cert`` is the ONE place a member / admin / subscriber
household moves its pinned space authority key. Every inbound path that
carries a cert (the rotation bundle, a key share, a config ``space_meta``,
an invite, a redeem ACK, a roster snapshot, a GFS listing) funnels here.
"""

from __future__ import annotations

import logging

import pytest

from socialhome.authority_cert import sign_authority_cert
from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_authority_pin import (
    AuthorityCertOutcome,
    apply_authority_cert,
)

SPACE = "sp-pin"
K1 = "aa" * 32


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "pin.db", batch_timeout_ms=10)
    await db.startup()
    repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x07" * 32))
    owner = generate_identity_keypair()
    owner_id = derive_instance_id(owner.public_key)
    await repo.save(
        Space(
            id=SPACE,
            name="S",
            owner_instance_id=owner_id,
            owner_username="anna",
            identity_public_key=K1,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )

    class E:
        pass

    e = E()
    e.repo = repo
    e.owner = owner
    e.owner_id = owner_id
    yield e
    await db.shutdown()


def _cert(e, *, epoch: int, pk: str, signer=None) -> dict:
    signer = signer or e.owner
    return sign_authority_cert(
        space_id=SPACE,
        owner_instance_id=e.owner_id,
        owner_seed=signer.private_key,
        owner_pk_hex=signer.public_key.hex(),
        authority_pk_hex=pk,
        key_epoch=epoch,
    )


async def _apply(e, cert, *, own: str = "member-inst"):
    space = await e.repo.get(SPACE)
    return await apply_authority_cert(e.repo, space, cert, own_instance_id=own)


async def test_newer_epoch_repins_and_clears_the_old_seed(env):
    await env.repo.set_space_seed(SPACE, generate_identity_keypair().private_key)
    k2 = generate_identity_keypair().public_key.hex()
    assert await _apply(env, _cert(env, epoch=1, pk=k2)) is AuthorityCertOutcome.APPLIED
    got = await env.repo.get(SPACE)
    assert (got.identity_public_key, got.authority_key_epoch) == (k2, 1)
    assert await env.repo.get_space_seed(SPACE) is None


async def test_lagging_receiver_jumps_straight_to_the_latest_epoch(env):
    k4 = generate_identity_keypair().public_key.hex()
    assert await _apply(env, _cert(env, epoch=4, pk=k4)) is AuthorityCertOutcome.APPLIED
    assert (await env.repo.get(SPACE)).authority_key_epoch == 4


async def test_same_epoch_same_key_is_a_noop(env):
    k2 = generate_identity_keypair().public_key.hex()
    await _apply(env, _cert(env, epoch=1, pk=k2))
    seed = generate_identity_keypair().private_key
    await env.repo.set_space_seed(SPACE, seed)
    assert await _apply(env, _cert(env, epoch=1, pk=k2)) is AuthorityCertOutcome.CURRENT
    # A no-op never clears a seed that was legitimately shared for K2.
    assert await env.repo.get_space_seed(SPACE) == seed


async def test_replayed_older_cert_is_refused(env, caplog):
    k2 = generate_identity_keypair().public_key.hex()
    k3 = generate_identity_keypair().public_key.hex()
    old = _cert(env, epoch=1, pk=k2)
    await _apply(env, old)
    await _apply(env, _cert(env, epoch=2, pk=k3))
    with caplog.at_level(logging.WARNING):
        assert await _apply(env, old) is AuthorityCertOutcome.STALE
    assert (await env.repo.get(SPACE)).identity_public_key == k3
    assert "older" in caplog.text or "stale" in caplog.text.lower()


async def test_same_epoch_different_key_is_refused(env, caplog):
    k2 = generate_identity_keypair().public_key.hex()
    await _apply(env, _cert(env, epoch=1, pk=k2))
    other = generate_identity_keypair().public_key.hex()
    with caplog.at_level(logging.WARNING):
        assert await _apply(env, _cert(env, epoch=1, pk=other)) is (
            AuthorityCertOutcome.STALE
        )
    assert (await env.repo.get(SPACE)).identity_public_key == k2
    assert caplog.records


async def test_cert_signed_by_another_household_is_rejected(env):
    evil = generate_identity_keypair()
    out = await _apply(env, _cert(env, epoch=9, pk="bb" * 32, signer=evil))
    assert out is AuthorityCertOutcome.REJECTED
    assert (await env.repo.get(SPACE)).identity_public_key == K1


async def test_unknown_suite_is_rejected(env):
    cert = _cert(env, epoch=1, pk="bb" * 32)
    cert["cert_sig_suite"] = "future"
    assert await _apply(env, cert) is AuthorityCertOutcome.REJECTED


async def test_known_host_pk_must_match(env):
    await env.repo.set_host_identity_pk(SPACE, "cc" * 32)
    out = await _apply(env, _cert(env, epoch=1, pk="bb" * 32))
    assert out is AuthorityCertOutcome.REJECTED


async def test_owner_host_ignores_certs_for_its_own_space(env):
    out = await _apply(env, _cert(env, epoch=5, pk="bb" * 32), own=env.owner_id)
    assert out is AuthorityCertOutcome.OWN_SPACE
    assert (await env.repo.get(SPACE)).identity_public_key == K1


async def test_missing_cert_is_absent(env):
    assert await _apply(env, None) is AuthorityCertOutcome.ABSENT


def test_outcome_consistency_flag():
    assert AuthorityCertOutcome.APPLIED.consistent
    assert AuthorityCertOutcome.CURRENT.consistent
    assert not AuthorityCertOutcome.STALE.consistent
    assert not AuthorityCertOutcome.REJECTED.consistent


# ── Owner side: issuing the cert for the current key ─────────────────────


async def test_owner_cert_is_none_at_epoch_zero(env):
    from socialhome.services.space_authority_pin import owner_authority_cert

    space = await env.repo.get(SPACE)
    assert (
        owner_authority_cert(
            space,
            own_instance_id=env.owner_id,
            owner_seed=env.owner.private_key,
            owner_pk=env.owner.public_key,
        )
        is None
    )


async def test_owner_cert_names_the_current_key_and_verifies(env):
    from socialhome.authority_cert import verify_authority_cert
    from socialhome.services.space_authority_pin import owner_authority_cert

    k2 = generate_identity_keypair()
    await env.repo.rotate_authority_key(
        SPACE, public_key_hex=k2.public_key.hex(), seed=k2.private_key, key_epoch=1
    )
    space = await env.repo.get(SPACE)
    cert = owner_authority_cert(
        space,
        own_instance_id=env.owner_id,
        owner_seed=env.owner.private_key,
        owner_pk=env.owner.public_key,
    )
    got = verify_authority_cert(cert, space_id=SPACE, owner_instance_id=env.owner_id)
    assert (got.authority_pk_hex, got.key_epoch) == (k2.public_key.hex(), 1)


async def test_owner_cert_is_none_off_the_owner_or_without_key_material(env):
    from types import SimpleNamespace

    from socialhome.services.space_authority_pin import (
        owner_authority_cert,
        owner_authority_cert_via,
    )

    k2 = generate_identity_keypair()
    await env.repo.rotate_authority_key(
        SPACE, public_key_hex=k2.public_key.hex(), seed=k2.private_key, key_epoch=1
    )
    space = await env.repo.get(SPACE)
    assert (
        owner_authority_cert(
            space,
            own_instance_id="not-the-owner",
            owner_seed=env.owner.private_key,
            owner_pk=env.owner.public_key,
        )
        is None
    )
    assert owner_authority_cert_via(None, space) is None
    mock_fed = SimpleNamespace(
        own_instance_id=env.owner_id, own_identity_seed=object(), own_identity_pk=b""
    )
    assert owner_authority_cert_via(mock_fed, space) is None
    real_fed = SimpleNamespace(
        own_instance_id=env.owner_id,
        own_identity_seed=env.owner.private_key,
        own_identity_pk=env.owner.public_key,
    )
    assert owner_authority_cert_via(real_fed, space)["key_epoch"] == 1
