"""Release-blocker protocol tests: revoking an admin rotates the space key.

Marked ``@pytest.mark.security``.

With ``delegated_admin_authority`` on, the owner shares the space's Ed25519
authority seed (K1) with each admin household. Before v_44 a household that
was demoted kept K1 forever and could still sign config, roster gossip,
roster snapshots and content-key rekeys that every member accepted.

These tests run two REAL applications — the owner household ``O`` and a
member household ``M`` — and move the owner's actual outbound envelopes
into ``M``'s real dispatch registry. ``B`` is the household being demoted
(it holds K1, so the tests sign as it); ``A`` is a second admin household
that keeps its seat. After the demotion:

* ``O`` pins a fresh key K2 at epoch 1 and certifies it with its household
  identity key;
* every K1-signed config / roster / snapshot / rekey is refused by ``M``
  (after applying the bundle) and by ``O``;
* a forged, replayed or conflicting cert moves nothing;
* the bundle resets ``M`` to the owner's baseline even where ``B`` had
  inflated config / roster / content-key state with K1;
* ``A`` receives K2 (with the cert) and its K2 events are accepted;
* the triggers fire exactly when a household loses its LAST admin seat.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    federation_repo_key,
    federation_service_key,
    space_authority_rotation_key,
    space_crypto_service_key,
    space_remote_member_repo_key,
    space_repo_key,
    space_service_key,
    user_service_key,
)
from socialhome.authority_cert import sign_authority_cert
from socialhome.authority_sig import sign_authority_event, strip_authority_sig_fields
from socialhome.config import Config
from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)
from socialhome.federation.federation_service import FederationService
from socialhome.services.space_crypto_service import KEY_SUITE_AESGCM_256
from socialhome.services.space_service import space_metadata_for_federation

pytestmark = pytest.mark.security

FET = FederationEventType
B_ID = derive_instance_id(generate_identity_keypair().public_key)
A_ID = derive_instance_id(generate_identity_keypair().public_key)


def _cfg(tmp_dir, label: str) -> Config:
    base = tmp_dir / label
    base.mkdir()
    return Config(
        data_dir=str(base),
        db_path=str(base / "db.sqlite"),
        media_path=str(base / "media"),
        apps_path=str(base / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://t.example"})},
        ),
    )


def _peer(instance_id: str, version: int) -> RemoteInstance:
    return RemoteInstance(
        id=instance_id,
        display_name=instance_id[:6],
        remote_identity_pk="00" * 32,
        key_self_to_remote="unused",
        key_remote_to_self="unused",
        remote_inbox_url=f"https://{instance_id[:6]}.invalid/wh",
        local_inbox_id=f"wh-{instance_id[:6]}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
        proto_version=version,
    )


class World:
    """The two applications plus everything a test needs to drive them."""

    o_app: object
    m_app: object
    space_id: str
    k1_seed: bytes
    k1_pk: str
    o_id: str
    m_id: str
    sent: list[tuple[str, str, FederationEventType, dict]]

    def outbound(self, *, to: str | None = None, event_type=None) -> list[dict]:
        return [
            payload
            for sender, target, et, payload in self.sent
            if sender == self.o_id
            and (to is None or target == to)
            and (event_type is None or et is event_type)
        ]

    async def o_space(self) -> Space:
        return await self.o_app[space_repo_key].get(self.space_id)

    async def m_space(self) -> Space:
        return await self.m_app[space_repo_key].get(self.space_id)


@pytest.fixture
async def world(aiohttp_client, tmp_dir, monkeypatch):
    w = World()
    w.sent = []

    async def _capture(self, *, to_instance_id, event_type, payload, space_id=None):
        w.sent.append((self.own_instance_id, to_instance_id, event_type, payload))
        return DeliveryResult(instance_id=to_instance_id, ok=True)

    monkeypatch.setattr(FederationService, "send_with_mesh_fallback", _capture)

    o_app = create_app(_cfg(tmp_dir, "owner"))
    m_app = create_app(_cfg(tmp_dir, "member"))
    await aiohttp_client(o_app)
    await aiohttp_client(m_app)
    w.o_app, w.m_app = o_app, m_app
    o_fed = o_app[federation_service_key]
    m_fed = m_app[federation_service_key]
    w.o_id, w.m_id = o_fed.own_instance_id, m_fed.own_instance_id

    # ── Owner household: the space, its seats, its peers ──
    await o_app[user_service_key].provision(username="anna", display_name="Anna")
    space = await o_app[space_service_key].create_space(
        owner_username="anna",
        name="Family",
        features=SpaceFeatures(delegated_admin_authority=True),
    )
    w.space_id = space.id
    o_spaces = o_app[space_repo_key]
    w.k1_seed = await o_spaces.get_space_seed(space.id)
    w.k1_pk = space.identity_public_key
    o_rm = o_app[space_remote_member_repo_key]
    for inst, user, role in (
        (B_ID, "ub", SpaceRole.ADMIN),
        (A_ID, "ua", SpaceRole.ADMIN),
        (w.m_id, "um", SpaceRole.MEMBER),
    ):
        await o_rm.add(
            space_id=space.id,
            instance_id=inst,
            user_id=user,
            user_pk=None,
            display_name=user,
            role=role.value,
        )
        await o_spaces.add_space_instance(space.id, inst)
        await o_app[federation_repo_key].save_instance(_peer(inst, 44))

    # ── Member household: a stub of the owner's space, pinned to K1 ──
    m_spaces = m_app[space_repo_key]
    await m_spaces.save(
        Space(
            id=space.id,
            name="Family",
            owner_instance_id=w.o_id,
            owner_username="anna",
            identity_public_key=w.k1_pk,
            config_sequence=space.config_sequence,
            features=SpaceFeatures(delegated_admin_authority=True),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    await m_spaces.set_host_identity_pk(space.id, o_fed.own_identity_pk.hex())
    await m_spaces.save_member(
        SpaceMember(
            space_id=space.id,
            user_id="um",
            role=SpaceRole.MEMBER.value,
            joined_at="2026-01-01 00:00:00",
        )
    )
    m_rm = m_app[space_remote_member_repo_key]
    for inst, user, role in ((B_ID, "ub", "admin"), (A_ID, "ua", "admin")):
        await m_rm.add(
            space_id=space.id,
            instance_id=inst,
            user_id=user,
            user_pk=None,
            display_name=user,
            role=role,
        )
    yield w


# ── Helpers ──────────────────────────────────────────────────────────────


async def _deliver(app, *, sender: str, event_type, payload: dict) -> None:
    fed = app[federation_service_key]
    await fed._event_registry.dispatch(
        FederationEvent(
            msg_id=str(uuid.uuid4()),
            event_type=event_type,
            from_instance=sender,
            to_instance=fed.own_instance_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload=payload,
            space_id=payload.get("space_id"),
        )
    )


def _signed(seed: bytes, event_type: str, space_id: str, payload: dict) -> dict:
    return {
        **payload,
        **sign_authority_event(
            event_type=event_type,
            space_id=space_id,
            payload=strip_authority_sig_fields(payload),
            space_seed=seed,
        ),
    }


async def _config_from_b(w: World, app, *, seed: bytes, name: str, seq: int) -> None:
    """``B`` (holding ``seed``) signs and sends a config edit to ``app``."""
    space = await app[space_repo_key].get(w.space_id)
    meta = space_metadata_for_federation(space)
    meta["name"] = name
    meta["config_sequence"] = seq
    meta["config_author_instance"] = B_ID
    meta = _signed(seed, "space_config_changed", w.space_id, meta)
    await _deliver(
        app,
        sender=B_ID,
        event_type=FET.SPACE_CONFIG_CHANGED,
        payload={"space_id": w.space_id, "sequence": seq, "space_meta": meta},
    )


def _roster(
    w: World, seed: bytes, *, user: str, inst: str, role: str, version: int, left=False
) -> tuple[FederationEventType, dict]:
    et = FET.SPACE_MEMBER_LEFT if left else FET.SPACE_MEMBER_JOINED
    bare = {
        "space_id": w.space_id,
        "user_id": user,
        "instance_id": inst,
        "display_name": user,
        "user_pk": None,
        "role": role,
        "member_version": version,
        "roster_version": version,
    }
    return et, _signed(seed, et.value, w.space_id, bare)


def _rekey(w: World, seed: bytes, *, epoch: int, raw: bytes) -> dict:
    meta = {
        "epoch": epoch,
        "key_suite": KEY_SUITE_AESGCM_256,
        "key_base64": base64.b64encode(raw).decode("ascii"),
        "rotated_by": B_ID,
    }
    return {
        "space_id": w.space_id,
        "space_content_key": _signed(
            seed, "space_key_exchange_rekey", w.space_id, meta
        ),
    }


async def _demote_b(w: World) -> None:
    await w.o_app[space_service_key].set_remote_member_role(
        w.space_id,
        actor_username="anna",
        instance_id=B_ID,
        user_id="ub",
        role=SpaceRole.MEMBER.value,
    )


async def _deliver_bundle_to_m(w: World, *, index: int = -1) -> None:
    bundles = w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)
    assert bundles, "the owner sent M no rotation bundle"
    await _deliver(
        w.m_app,
        sender=w.o_id,
        event_type=FET.SPACE_AUTHORITY_ROTATED,
        payload=bundles[index],
    )


async def _rotated(w: World) -> None:
    await _demote_b(w)
    await _deliver_bundle_to_m(w)


# ── The rotation itself ──────────────────────────────────────────────────


async def test_demotion_rotates_the_key_and_the_member_follows(world):
    w = world
    await _demote_b(w)
    o = await w.o_space()
    assert o.identity_public_key != w.k1_pk
    assert o.authority_key_epoch > 0
    assert {
        target
        for sender, target, et, _p in w.sent
        if sender == w.o_id and et is FET.SPACE_AUTHORITY_ROTATED
    } == {B_ID, A_ID, w.m_id}
    await _deliver_bundle_to_m(w)
    m = await w.m_space()
    assert (m.identity_public_key, m.authority_key_epoch) == (
        o.identity_public_key,
        o.authority_key_epoch,
    )


async def test_bundle_is_encrypted_routing_only_in_plaintext_and_names_nobody(world):
    """Every field but ``space_id`` lives in the per-household encrypted
    payload (the only thing ``send_with_mesh_fallback`` takes); the cert
    names no member, reason or revoked household."""
    w = world
    await _demote_b(w)
    bundle = w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)[0]
    cert = bundle["authority_cert"]
    # Compare whole field VALUES (a substring test over the base64 signature
    # or hex keys flakes: "ub" can occur by chance).
    values = {str(v) for v in cert.values()}
    assert B_ID not in values and "ub" not in values
    assert set(cert) == {
        "space_id",
        "owner_instance_id",
        "owner_pk",
        "authority_pk",
        "authority_key_suite",
        "key_epoch",
        "issued_at",
        "cert_sig_suite",
        "cert_sig",
    }


# ── §8.1-3: K1 events refused after rotation ─────────────────────────────


async def test_old_key_config_is_refused_by_member_and_owner(world):
    w = world
    await _rotated(w)
    for app in (w.m_app, w.o_app):
        before = (await app[space_repo_key].get(w.space_id)).name
        await _config_from_b(w, app, seed=w.k1_seed, name="Hijacked", seq=999)
        assert (await app[space_repo_key].get(w.space_id)).name == before


async def test_old_key_roster_gossip_and_snapshot_are_refused(world):
    w = world
    await _rotated(w)
    m_rm = w.m_app[space_remote_member_repo_key]
    # Self-promotion back to admin.
    et, p = _roster(w, w.k1_seed, user="ub", inst=B_ID, role="admin", version=10**6)
    await _deliver(w.m_app, sender=B_ID, event_type=et, payload=p)
    assert (await m_rm.get(w.space_id, B_ID, "ub")).role == "member"
    # Kicking the remaining admin.
    et, p = _roster(
        w, w.k1_seed, user="ua", inst=A_ID, role="admin", version=10**6, left=True
    )
    await _deliver(w.m_app, sender=B_ID, event_type=et, payload=p)
    assert await m_rm.get(w.space_id, A_ID, "ua") is not None
    # The same entries as a snapshot.
    et, p = _roster(w, w.k1_seed, user="ghost", inst=B_ID, role="admin", version=7)
    await _deliver(
        w.m_app,
        sender=B_ID,
        event_type=FET.SPACE_ROSTER_SNAPSHOT,
        payload={
            "space_id": w.space_id,
            "entries": [{"event_type": et.value, "payload": p}],
        },
    )
    assert await m_rm.get(w.space_id, B_ID, "ghost") is None


async def test_old_key_rekey_is_refused(world):
    w = world
    await _rotated(w)
    crypto = w.m_app[space_crypto_service_key]
    before = await crypto.get_current_epoch(w.space_id)
    await _deliver(
        w.m_app,
        sender=B_ID,
        event_type=FET.SPACE_KEY_EXCHANGE_REKEY,
        payload=_rekey(w, w.k1_seed, epoch=500, raw=b"\x09" * 32),
    )
    assert await crypto.get_current_epoch(w.space_id) == before


# ── §8.6-7: forged / replayed / conflicting certs ────────────────────────


def _forged_bundle(
    w: World, bundle: dict, *, signer, owner_id: str | None = None
) -> dict:
    cert = dict(bundle["authority_cert"])
    forged = sign_authority_cert(
        space_id=w.space_id,
        owner_instance_id=owner_id or w.o_id,
        owner_seed=signer.private_key,
        owner_pk_hex=signer.public_key.hex(),
        authority_pk_hex=cert["authority_pk"],
        key_epoch=cert["key_epoch"],
    )
    return {**bundle, "authority_cert": forged}


@pytest.mark.parametrize(
    "forgery", ["b_household_key", "old_space_key", "foreign_owner"]
)
async def test_forged_cert_is_refused_by_the_member(world, forgery):
    w = world
    await _demote_b(w)
    bundle = w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)[0]
    if forgery == "b_household_key":
        bad = _forged_bundle(w, bundle, signer=generate_identity_keypair())
    elif forgery == "old_space_key":
        # Signed with K1 — the key B holds — while naming the real owner key.
        cert = bundle["authority_cert"]
        bad = {
            **bundle,
            "authority_cert": sign_authority_cert(
                space_id=w.space_id,
                owner_instance_id=w.o_id,
                owner_seed=w.k1_seed,
                owner_pk_hex=cert["owner_pk"],
                authority_pk_hex=cert["authority_pk"],
                key_epoch=cert["key_epoch"],
            ),
        }
    else:
        other = generate_identity_keypair()
        bad = _forged_bundle(
            w, bundle, signer=other, owner_id=derive_instance_id(other.public_key)
        )
    await _deliver(
        w.m_app, sender=w.o_id, event_type=FET.SPACE_AUTHORITY_ROTATED, payload=bad
    )
    m = await w.m_space()
    assert (m.identity_public_key, m.authority_key_epoch) == (w.k1_pk, 0)


async def test_bundle_from_anyone_but_the_owner_is_refused(world):
    """The cert is genuine, but only the owner may trigger the baseline
    reset — a relaying household (here B, who received its own copy)
    cannot replay it at M."""
    w = world
    await _demote_b(w)
    bundle = w.outbound(to=B_ID, event_type=FET.SPACE_AUTHORITY_ROTATED)[0]
    await _deliver(
        w.m_app, sender=B_ID, event_type=FET.SPACE_AUTHORITY_ROTATED, payload=bundle
    )
    assert (await w.m_space()).authority_key_epoch == 0


async def test_replayed_older_bundle_cannot_move_the_pin_back(world):
    w = world
    await _demote_b(w)
    await _deliver_bundle_to_m(w)
    epoch1_pin = (await w.m_space()).identity_public_key
    epoch1 = (await w.m_space()).authority_key_epoch
    await w.o_app[space_service_key].update_config(
        w.space_id,
        actor_username="anna",
        features=SpaceFeatures(delegated_admin_authority=False),
    )
    await _deliver_bundle_to_m(w)  # epoch 2
    m = await w.m_space()
    assert m.authority_key_epoch > epoch1 and m.identity_public_key != epoch1_pin
    await _deliver_bundle_to_m(w, index=0)  # replay epoch 1
    assert (await w.m_space()).identity_public_key == m.identity_public_key


async def test_same_epoch_different_key_is_refused(world):
    w = world
    await _rotated(w)
    bundle = w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)[-1]
    fed = w.o_app[federation_service_key]
    cert = sign_authority_cert(
        space_id=w.space_id,
        owner_instance_id=w.o_id,
        owner_seed=fed.own_identity_seed,
        owner_pk_hex=fed.own_identity_pk.hex(),
        authority_pk_hex=generate_identity_keypair().public_key.hex(),
        key_epoch=1,
    )
    pin = (await w.m_space()).identity_public_key
    await _deliver(
        w.m_app,
        sender=w.o_id,
        event_type=FET.SPACE_AUTHORITY_ROTATED,
        payload={**bundle, "authority_cert": cert},
    )
    assert (await w.m_space()).identity_public_key == pin


async def test_unknown_cert_suite_is_refused(world):
    w = world
    await _demote_b(w)
    bundle = w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)[0]
    cert = {**bundle["authority_cert"], "cert_sig_suite": "ed25519+mldsa65"}
    await _deliver(
        w.m_app,
        sender=w.o_id,
        event_type=FET.SPACE_AUTHORITY_ROTATED,
        payload={**bundle, "authority_cert": cert},
    )
    assert (await w.m_space()).authority_key_epoch == 0


# ── §8.8: a lagging member jumps to the latest epoch ─────────────────────


async def test_lagging_member_applies_the_latest_epoch_directly(world):
    w = world
    svc = w.o_app[space_service_key]
    await _demote_b(w)  # epoch 1
    await svc.update_config(
        w.space_id,
        actor_username="anna",
        features=SpaceFeatures(delegated_admin_authority=False),
    )  # epoch 2
    await svc.update_config(
        w.space_id,
        actor_username="anna",
        features=SpaceFeatures(delegated_admin_authority=True),
    )
    await svc.update_config(
        w.space_id,
        actor_username="anna",
        features=SpaceFeatures(delegated_admin_authority=False),
    )  # epoch 3
    o = await w.o_space()
    epochs = [
        b["authority_cert"]["key_epoch"]
        for b in w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)
    ]
    assert len(epochs) == 3 and epochs == sorted(set(epochs))
    await _deliver_bundle_to_m(w)  # only the newest
    m = await w.m_space()
    assert (m.authority_key_epoch, m.identity_public_key) == (
        o.authority_key_epoch,
        o.identity_public_key,
    )


# ── §8.9: the bundle resets the member to the owner's baseline ───────────


async def test_bundle_undoes_what_the_revoked_household_inflated(world):
    w = world
    m_rm = w.m_app[space_remote_member_repo_key]
    crypto = w.m_app[space_crypto_service_key]
    # While B still legitimately held K1, it inflated M's state:
    await _config_from_b(w, w.m_app, seed=w.k1_seed, name="B's name", seq=10**6)
    assert (await w.m_space()).name == "B's name"
    et, p = _roster(w, w.k1_seed, user="ghost", inst=B_ID, role="admin", version=10**9)
    await _deliver(w.m_app, sender=B_ID, event_type=et, payload=p)
    et, p = _roster(w, w.k1_seed, user="ub", inst=B_ID, role="admin", version=10**9)
    await _deliver(w.m_app, sender=B_ID, event_type=et, payload=p)
    await _deliver(
        w.m_app,
        sender=B_ID,
        event_type=FET.SPACE_KEY_EXCHANGE_REKEY,
        payload=_rekey(w, w.k1_seed, epoch=10**6, raw=b"\x07" * 32),
    )
    assert await crypto.get_current_epoch(w.space_id) == 10**6

    await _rotated(w)

    m = await w.m_space()
    o = await w.o_space()
    assert m.name == o.name == "Family"
    assert m.config_sequence == o.config_sequence
    ghost = await m_rm.get_including_tombstones(w.space_id, B_ID, "ghost")
    assert ghost is not None and ghost.tombstoned
    ub = await m_rm.get(w.space_id, B_ID, "ub")
    assert ub.role == "member" and ub.member_version < 10**9
    owner_epoch, owner_key = await w.o_app[space_crypto_service_key].export_current_key(
        w.space_id
    )
    assert await crypto.export_current_key(w.space_id) == (owner_epoch, owner_key)


# ── §8.10-11: the remaining admin gets K2; delegation off clears seeds ───


async def test_remaining_admin_gets_the_new_seed_and_its_k2_events_land(world):
    w = world
    await _demote_b(w)
    shares = w.outbound(to=A_ID, event_type=FET.SPACE_ADMIN_KEY_SHARE)
    assert len(shares) == 1
    assert not w.outbound(to=B_ID, event_type=FET.SPACE_ADMIN_KEY_SHARE)
    share = shares[0]
    assert share["key_epoch"] == (await w.o_space()).authority_key_epoch
    assert "authority_cert" in share
    # Play household A on the member app: it accepts K2 (the cert re-pins
    # first, then the seed matches the pin) …
    await _deliver(
        w.m_app, sender=w.o_id, event_type=FET.SPACE_ADMIN_KEY_SHARE, payload=share
    )
    k2_seed = await w.m_app[space_repo_key].get_space_seed(w.space_id)
    assert k2_seed is not None
    assert k2_seed == await w.o_app[space_repo_key].get_space_seed(w.space_id)
    # … and the owner accepts what it signs with K2.
    await _config_from_b(w, w.o_app, seed=k2_seed, name="Renamed by A", seq=50)
    assert (await w.o_space()).name == "Renamed by A"


async def test_a_share_whose_seed_does_not_match_the_pin_is_dropped(world):
    w = world
    await _rotated(w)
    payload = {
        "space_id": w.space_id,
        "space_seed": base64.urlsafe_b64encode(w.k1_seed).decode("ascii"),
        "seed_suite": "ed25519-seed",
    }
    await _deliver(
        w.m_app, sender=w.o_id, event_type=FET.SPACE_ADMIN_KEY_SHARE, payload=payload
    )
    assert await w.m_app[space_repo_key].get_space_seed(w.space_id) is None


async def test_delegation_off_rotates_clears_the_seed_and_shares_nothing(world):
    w = world
    # M plays an admin household that holds K1.
    await w.m_app[space_repo_key].set_space_seed(w.space_id, w.k1_seed)
    await w.o_app[space_service_key].update_config(
        w.space_id,
        actor_username="anna",
        features=SpaceFeatures(delegated_admin_authority=False),
    )
    assert (await w.o_space()).authority_key_epoch > 0
    assert not w.outbound(event_type=FET.SPACE_ADMIN_KEY_SHARE)
    await _deliver_bundle_to_m(w)
    assert await w.m_app[space_repo_key].get_space_seed(w.space_id) is None
    assert (await w.m_space()).features.delegated_admin_authority is False


# ── §8.12: the owner host never lets gossip raise a seat ─────────────────


async def test_owner_ignores_gossip_raising_a_seat_to_admin(world):
    w = world
    et, p = _roster(w, w.k1_seed, user="um", inst=w.m_id, role="admin", version=10**6)
    await _deliver(w.o_app, sender=B_ID, event_type=et, payload=p)
    got = await w.o_app[space_remote_member_repo_key].get(w.space_id, w.m_id, "um")
    assert got.role == "member"
    assert (await w.o_space()).authority_key_epoch == 0


# ── §8.13: triggers fire on the LAST admin seat only ─────────────────────


async def test_household_with_two_admins_keeps_the_key_until_the_last_goes(world):
    w = world
    o_rm = w.o_app[space_remote_member_repo_key]
    svc = w.o_app[space_service_key]
    await o_rm.add(
        space_id=w.space_id,
        instance_id=B_ID,
        user_id="ub2",
        user_pk=None,
        display_name="ub2",
        role="admin",
    )
    await _demote_b(w)
    assert (await w.o_space()).authority_key_epoch == 0
    await svc.remove_remote_member(
        w.space_id, actor_username="anna", instance_id=B_ID, user_id="ub2"
    )
    assert (await w.o_space()).authority_key_epoch > 0


async def test_admin_to_moderator_rotates(world):
    w = world
    await w.o_app[space_service_key].set_remote_member_role(
        w.space_id,
        actor_username="anna",
        instance_id=B_ID,
        user_id="ub",
        role=SpaceRole.MODERATOR.value,
    )
    assert (await w.o_space()).authority_key_epoch > 0


async def test_ban_of_a_remote_admin_rotates(world):
    w = world
    await w.o_app[space_service_key].ban(
        w.space_id, actor_username="anna", user_id="ub"
    )
    assert (
        await w.o_app[space_remote_member_repo_key].get(w.space_id, B_ID, "ub") is None
    )
    assert (await w.o_space()).authority_key_epoch > 0


async def test_admin_household_leaving_rotates_on_the_owner(world):
    """B leaves (or a delegated admin removed it) while the owner was
    offline: the owner learns from the K1-signed LEFT gossip and rotates."""
    w = world
    et, p = _roster(
        w, w.k1_seed, user="ub", inst=B_ID, role="admin", version=99, left=True
    )
    await _deliver(w.o_app, sender=B_ID, event_type=et, payload=p)
    assert (await w.o_space()).authority_key_epoch > 0


async def test_no_rotation_while_delegation_is_off(world):
    w = world
    await w.o_app[space_service_key].update_config(
        w.space_id,
        actor_username="anna",
        features=SpaceFeatures(delegated_admin_authority=False),
    )
    after_off = (await w.o_space()).authority_key_epoch
    assert after_off > 0  # the off-flip itself
    await _demote_b(w)
    assert (await w.o_space()).authority_key_epoch == after_off


# ── §8.14: households below v_44 get no bundle ───────────────────────────


async def test_peers_below_v44_get_no_bundle_but_an_unsigned_rekey(world):
    w = world
    # Pin the peer row at v_43 (the repo only ever raises a version).
    await w.o_app[db_key].enqueue(
        "UPDATE remote_instances SET proto_version=43 WHERE id=?", (w.m_id,)
    )
    await _demote_b(w)
    assert not w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)
    rekeys = w.outbound(to=w.m_id, event_type=FET.SPACE_KEY_EXCHANGE_REKEY)
    assert rekeys
    assert "authority_sig" not in rekeys[-1]["space_content_key"]
    assert not w.outbound(to=w.m_id, event_type=FET.SPACE_ADMIN_KEY_SHARE)


# ── §8.4 / §8.6: the GFS side ────────────────────────────────────────────


@pytest.fixture
async def gfs(tmp_dir):
    from pathlib import Path

    from socialhome.db.database import AsyncDatabase
    from socialhome.global_server.federation import GfsFederationService
    from socialhome.global_server.repositories import SqliteGfsFederationRepo

    db = AsyncDatabase(
        tmp_dir / "gfs.db",
        migrations_dir=Path(__file__).resolve().parent.parent.parent
        / "socialhome/global_server/migrations",
        batch_timeout_ms=10,
    )
    await db.startup()
    yield GfsFederationService(SqliteGfsFederationRepo(db))
    await db.shutdown()


def _publish_body(owner_id: str, space_id: str, pk_hex: str, *, cert, ts: str) -> dict:
    body = {
        "space_id": space_id,
        "owning_instance": owner_id,
        "name": "Rot",
        "description": "",
        "about_markdown": "",
        "cover_url": "",
        "icon_url": "",
        "min_age": 0,
        "category": "general",
        "accent_color": "#D2542A",
        "primary_color": "#D2542A",
        "identity_public_key": pk_hex,
        "ts": ts,
    }
    if cert is not None:
        body["authority_cert"] = cert
    return body


async def _gfs_publish(gfs, owner, owner_id, space_id, pk_hex, cert=None):
    import json as _json

    from socialhome.crypto import b64url_encode, sign_ed25519

    ts = datetime.now(timezone.utc).isoformat()
    body = _publish_body(owner_id, space_id, pk_hex, cert=cert, ts=ts)
    sig = b64url_encode(
        sign_ed25519(
            owner.private_key,
            _json.dumps(body, separators=(",", ":"), sort_keys=True).encode(),
        )
    )
    await gfs.publish_space(
        space_id=space_id,
        owning_instance=owner_id,
        name="Rot",
        identity_public_key=pk_hex,
        signature=sig,
        ts=ts,
        authority_cert=cert,
    )


async def _gfs_world(gfs):
    owner = generate_identity_keypair()
    owner_id = derive_instance_id(owner.public_key)
    await gfs.register_instance(
        owner_id, owner.public_key.hex(), "https://o.invalid/wh", auto_accept=True
    )
    k1 = generate_identity_keypair()
    await _gfs_publish(gfs, owner, owner_id, "sp-g", k1.public_key.hex())
    k2 = generate_identity_keypair()
    cert = sign_authority_cert(
        space_id="sp-g",
        owner_instance_id=owner_id,
        owner_seed=owner.private_key,
        owner_pk_hex=owner.public_key.hex(),
        authority_pk_hex=k2.public_key.hex(),
        key_epoch=1,
    )
    return owner, owner_id, k1, k2, cert


async def test_gfs_refuses_old_key_relays_handoffs_and_queries_after_rotation(gfs):
    from socialhome.authority_sig import (
        AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
        AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
    )

    owner, owner_id, k1, k2, cert = await _gfs_world(gfs)
    await _gfs_publish(gfs, owner, owner_id, "sp-g", k2.public_key.hex(), cert)
    for event_type in (
        AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
    ):
        payload = _signed(k1.private_key, event_type, "sp-g", {"blob": "x"})
        with pytest.raises(PermissionError):
            await gfs.publish_event("sp-g", event_type, payload)
        ok = _signed(k2.private_key, event_type, "sp-g", {"blob": event_type})
        assert await gfs.publish_event("sp-g", event_type, ok) == []
    ts = datetime.now(timezone.utc).isoformat()
    q = sign_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
        space_id="sp-g",
        payload={"space_id": "sp-g", "ts": ts},
        space_seed=k1.private_key,
    )
    with pytest.raises(PermissionError):
        await gfs.list_subscribers_with_keys(
            "sp-g",
            ts=ts,
            authority_sig=q["authority_sig"],
            authority_sig_suite=q["authority_sig_suite"],
        )


@pytest.mark.parametrize(
    "forgery", ["b_household_key", "old_space_key", "foreign_owner"]
)
async def test_gfs_refuses_a_forged_cert(gfs, forgery):
    owner, owner_id, k1, k2, _cert = await _gfs_world(gfs)
    other = generate_identity_keypair()
    signer_seed, signer_pk, claimed = {
        "b_household_key": (other.private_key, other.public_key.hex(), owner_id),
        "old_space_key": (k1.private_key, owner.public_key.hex(), owner_id),
        "foreign_owner": (
            other.private_key,
            other.public_key.hex(),
            derive_instance_id(other.public_key),
        ),
    }[forgery]
    forged = sign_authority_cert(
        space_id="sp-g",
        owner_instance_id=claimed,
        owner_seed=signer_seed,
        owner_pk_hex=signer_pk,
        authority_pk_hex=k2.public_key.hex(),
        key_epoch=1,
    )
    await _gfs_publish(gfs, owner, owner_id, "sp-g", k2.public_key.hex(), forged)
    assert (await gfs.get_space("sp-g")).identity_public_key == k1.public_key.hex()


# ── Review round 2 ───────────────────────────────────────────────────────


async def test_admin_dropping_its_own_seat_still_rotates(world):
    """C1: B tombstones its own admin seat at the host with
    SPACE_REMOTE_MEMBER_REMOVED. That ends an admin seat like any other
    path, so the key rotates and B's K1 signatures stop working."""
    w = world
    await _deliver(
        w.o_app,
        sender=B_ID,
        event_type=FET.SPACE_REMOTE_MEMBER_REMOVED,
        payload={"space_id": w.space_id, "user_id": "ub"},
    )
    assert (await w.o_space()).authority_key_epoch > 0
    await _deliver_bundle_to_m(w)
    for app in (w.m_app, w.o_app):
        await _config_from_b(w, app, seed=w.k1_seed, name="pwned-by-B", seq=10**6)
        assert (await app[space_repo_key].get(w.space_id)).name == "Family"


@pytest.mark.parametrize("action", ["remove", "ban"])
async def test_remove_or_ban_of_an_already_tombstoned_admin_seat_rotates(world, action):
    """C1: the seat was tombstoned by some path that did not rotate (a
    pre-v44 build, say). The owner removing or banning it now must still
    retire the key — the tombstone keeps the seat's last role."""
    w = world
    await w.o_app[space_remote_member_repo_key].remove(w.space_id, B_ID, "ub")
    svc = w.o_app[space_service_key]
    if action == "remove":
        await svc.remove_remote_member(
            w.space_id, actor_username="anna", instance_id=B_ID, user_id="ub"
        )
    else:
        await svc.ban(w.space_id, actor_username="anna", user_id="ub")
    assert (await w.o_space()).authority_key_epoch > 0


async def test_ban_of_a_stale_seat_gossips_the_seats_own_household(world):
    w = world
    await w.o_app[space_remote_member_repo_key].remove(w.space_id, B_ID, "ub")
    await w.o_app[space_service_key].ban(
        w.space_id, actor_username="anna", user_id="ub"
    )
    lefts = [
        p
        for p in w.outbound(event_type=FET.SPACE_MEMBER_LEFT)
        if p.get("user_id") == "ub"
    ]
    assert lefts and {p["instance_id"] for p in lefts} == {B_ID}


async def test_household_removed_by_gossip_gets_no_bundle_or_key(world):
    """I1: A removed B (its only seat) while the owner was offline. The
    owner rotates on the gossip — and must not hand B the new content key,
    config or roster: B no longer holds a live seat."""
    w = world
    et, p = _roster(
        w, w.k1_seed, user="ub", inst=B_ID, role="admin", version=99, left=True
    )
    await _deliver(w.o_app, sender=A_ID, event_type=et, payload=p)
    assert (await w.o_space()).authority_key_epoch > 0
    assert not w.outbound(to=B_ID, event_type=FET.SPACE_AUTHORITY_ROTATED)
    assert not w.outbound(to=B_ID, event_type=FET.SPACE_KEY_EXCHANGE_REKEY)
    members = await w.o_app[federation_repo_key].list_member_instance_ids(w.space_id)
    assert B_ID not in members


async def test_late_bundle_never_rolls_back_newer_key_or_seats(world):
    """I2: M adopted K2 from an inline cert, then received a newer K2
    rekey and a new seat, THEN the (redelivered) bundle. The reset must
    leave everything written under K2 alone — and a second copy of the
    bundle does nothing at all."""
    w = world
    await _demote_b(w)
    bundle = w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)[-1]
    m_space = await w.m_space()
    from socialhome.services.space_authority_pin import apply_authority_cert

    await apply_authority_cert(
        w.m_app[space_repo_key],
        m_space,
        bundle["authority_cert"],
        own_instance_id=w.m_id,
    )
    k2 = await w.o_app[space_repo_key].get_space_seed(w.space_id)
    newer = bundle["space_content_key"]["epoch"] + 1
    meta = {
        "epoch": newer,
        "key_suite": KEY_SUITE_AESGCM_256,
        "key_base64": base64.b64encode(b"\x05" * 32).decode(),
        "rotated_by": w.o_id,
    }
    await _deliver(
        w.m_app,
        sender=w.o_id,
        event_type=FET.SPACE_KEY_EXCHANGE_REKEY,
        payload={
            "space_id": w.space_id,
            "space_content_key": _signed(
                k2, "space_key_exchange_rekey", w.space_id, meta
            ),
        },
    )
    et, p = _roster(w, k2, user="un", inst="n" * 32, role="member", version=10**4)
    await _deliver(w.m_app, sender=w.o_id, event_type=et, payload=p)
    crypto = w.m_app[space_crypto_service_key]
    m_rm = w.m_app[space_remote_member_repo_key]
    for _ in range(2):
        await _deliver(
            w.m_app,
            sender=w.o_id,
            event_type=FET.SPACE_AUTHORITY_ROTATED,
            payload=bundle,
        )
        assert await crypto.get_current_epoch(w.space_id) == newer
        assert await m_rm.get(w.space_id, "n" * 32, "un") is not None


async def test_rotation_epoch_survives_a_restore_from_an_old_backup(world):
    """I3: the epoch is at least wall-clock seconds, so an owner restored
    from a backup taken before a rotation still issues a HIGHER epoch than
    the members hold, instead of being refused as stale forever."""
    import time

    w = world
    before = int(time.time())
    await _demote_b(w)
    assert (await w.o_space()).authority_key_epoch >= before


async def test_owner_drops_role_raising_gossip_entirely(world):
    """I4: a raise is refused outright — nothing of that event lands, so
    the owner's row never diverges from what the members hold."""
    w = world
    rm = w.o_app[space_remote_member_repo_key]
    before = await rm.get(w.space_id, w.m_id, "um")
    bare = {
        "space_id": w.space_id,
        "user_id": "um",
        "instance_id": w.m_id,
        "display_name": "renamed by gossip",
        "user_pk": None,
        "role": "admin",
        "member_version": 10**6,
        "roster_version": 10**6,
    }
    await _deliver(
        w.o_app,
        sender=B_ID,
        event_type=FET.SPACE_MEMBER_JOINED,
        payload=_signed(w.k1_seed, "space_member_joined", w.space_id, bare),
    )
    after = await rm.get(w.space_id, w.m_id, "um")
    assert (after.role, after.display_name, after.member_version) == (
        before.role,
        before.display_name,
        before.member_version,
    )


async def test_remaining_admin_gets_the_seed_before_the_bundle(world):
    w = world
    await _demote_b(w)
    to_a = [et for _s, t, et, _p in w.sent if t == A_ID]
    assert to_a.index(FET.SPACE_ADMIN_KEY_SHARE) < to_a.index(
        FET.SPACE_AUTHORITY_ROTATED
    )


async def test_revocation_with_delegation_off_rotates_if_a_seed_is_out(world):
    """Delegation was turned off WITHOUT a rotation having retired the
    shared seed (a pre-v44 build): removing that admin still rotates,
    because a seed was shared at the current epoch."""
    w = world
    spaces = w.o_app[space_repo_key]
    await spaces.mark_seed_shared(w.space_id)
    await w.o_app[db_key].enqueue(
        "UPDATE spaces SET delegated_admin_authority=0 WHERE id=?", (w.space_id,)
    )
    await _demote_b(w)
    assert (await w.o_space()).authority_key_epoch > 0


async def test_revocation_with_delegation_off_and_no_seed_out_does_not_rotate(world):
    w = world
    await w.o_app[db_key].enqueue(
        "UPDATE spaces SET delegated_admin_authority=0 WHERE id=?", (w.space_id,)
    )
    await _demote_b(w)
    assert (await w.o_space()).authority_key_epoch == 0


# ── Review round 3: no old-key write lands after the pin moved ───────────


async def test_k1_gossip_verified_before_the_bundle_cannot_land_after_it(
    world, monkeypatch
):
    """N1: B's K1-signed JOINED (admin, v10^9) is verified against K1, then
    the rotation bundle lands before the write. The write must notice the
    pin moved and drop — never be stamped as a new-key row."""
    w = world
    await _demote_b(w)
    bundle = w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)[-1]
    m_rm = w.m_app[space_remote_member_repo_key]
    cls = type(m_rm)
    orig = cls.apply_member_event
    fired = []

    async def interleaved(self, **kw):
        if self is m_rm and not fired:
            fired.append(1)
            await _deliver(
                w.m_app,
                sender=w.o_id,
                event_type=FET.SPACE_AUTHORITY_ROTATED,
                payload=bundle,
            )
        return await orig(self, **kw)

    monkeypatch.setattr(cls, "apply_member_event", interleaved)
    et, p = _roster(w, w.k1_seed, user="ub", inst=B_ID, role="admin", version=10**9)
    await _deliver(w.m_app, sender=B_ID, event_type=et, payload=p)
    assert fired
    row = await m_rm.get_including_tombstones(w.space_id, B_ID, "ub")
    assert row.role == "member" and row.member_version < 10**9


async def test_k1_rekey_verified_before_the_bundle_cannot_land_after_it(
    world, monkeypatch
):
    w = world
    await _demote_b(w)
    bundle = w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)[-1]
    crypto = w.m_app[space_crypto_service_key]
    repo = crypto._repo
    cls = type(repo)
    orig = cls.save
    fired = []

    async def interleaved(self, key, **kw):
        if self is repo and not fired:
            fired.append(1)
            await _deliver(
                w.m_app,
                sender=w.o_id,
                event_type=FET.SPACE_AUTHORITY_ROTATED,
                payload=bundle,
            )
        return await orig(self, key, **kw)

    monkeypatch.setattr(cls, "save", interleaved)
    await _deliver(
        w.m_app,
        sender=B_ID,
        event_type=FET.SPACE_KEY_EXCHANGE_REKEY,
        payload=_rekey(w, w.k1_seed, epoch=10**6, raw=b"\x09" * 32),
    )
    assert fired
    assert (
        await crypto.get_current_epoch(w.space_id)
        == (bundle["space_content_key"]["epoch"])
    )


async def test_k1_config_verified_before_the_bundle_cannot_land_after_it(
    world, monkeypatch
):
    w = world
    await _demote_b(w)
    bundle = w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)[-1]
    m_spaces = w.m_app[space_repo_key]
    cls = type(m_spaces)
    orig = cls.save_config_if_authority_epoch
    fired = []

    async def interleaved(self, space, *, verified_epoch):
        if self is m_spaces and not fired:
            fired.append(1)
            await _deliver(
                w.m_app,
                sender=w.o_id,
                event_type=FET.SPACE_AUTHORITY_ROTATED,
                payload=bundle,
            )
        return await orig(self, space, verified_epoch=verified_epoch)

    monkeypatch.setattr(cls, "save_config_if_authority_epoch", interleaved)
    await _config_from_b(w, w.m_app, seed=w.k1_seed, name="pwned-late", seq=10**6)
    assert fired
    assert (await w.m_space()).name == "Family"


async def test_k1_seed_share_cannot_be_stored_after_the_pin_moved(world, monkeypatch):
    """The key-share handler checks seed-vs-pin, then stores. A rotation in
    between must not leave the OLD seed stored next to the NEW pin."""
    w = world
    await w.m_app[space_repo_key].set_space_seed(w.space_id, w.k1_seed)
    await _demote_b(w)
    bundle = w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)[-1]
    await w.m_app[space_repo_key].clear_space_seed(w.space_id)
    m_spaces = w.m_app[space_repo_key]
    cls = type(m_spaces)
    orig = cls.set_space_seed_if_pin
    fired = []

    async def interleaved(self, space_id, seed, *, expected_pk):
        if self is m_spaces and not fired:
            fired.append(1)
            await _deliver(
                w.m_app,
                sender=w.o_id,
                event_type=FET.SPACE_AUTHORITY_ROTATED,
                payload=bundle,
            )
        return await orig(self, space_id, seed, expected_pk=expected_pk)

    monkeypatch.setattr(cls, "set_space_seed_if_pin", interleaved)
    await _deliver(
        w.m_app,
        sender=w.o_id,
        event_type=FET.SPACE_ADMIN_KEY_SHARE,
        payload={
            "space_id": w.space_id,
            "space_seed": base64.urlsafe_b64encode(w.k1_seed).decode("ascii"),
            "seed_suite": "ed25519-seed",
        },
    )
    assert fired
    assert await m_spaces.get_space_seed(w.space_id) is None


async def test_a_hostile_gfs_cannot_move_a_private_stubs_pin(world, monkeypatch):
    """N2: the GFS-trust heal is only for a follower of a PUBLIC space,
    and only from the GFS that seated the mirror — never a private stub with
    no local member (a pending invite), never from another GFS."""
    w = world
    mirror = w.m_app[space_service_key]._gfs_mirror
    evil = generate_identity_keypair().public_key.hex()

    class _Conn:
        id = "evil-gfs"
        inbox_url = "https://evil.invalid"

    async def _fetch(self, conn, space_id):
        return {"identity_public_key": evil, "authority_rotation_seq": 5}

    monkeypatch.setattr(type(mirror), "_fetch_listing", _fetch)
    spaces = w.m_app[space_repo_key]
    await spaces.delete_member(w.space_id, "um")
    assert not await mirror._refresh_from([_Conn()], w.space_id)
    assert (await w.m_space()).identity_public_key == w.k1_pk


# ── Review round 4: a restore rotates, never re-imposes the old state ────


async def _restore_rotate(w: World) -> None:
    await w.o_app[space_authority_rotation_key].rotate_hosted_after_restore()


async def test_post_restore_rotation_never_reseats_or_rolls_back(world):
    """M1: the owner was restored from a backup taken BEFORE a household was
    kicked and before a config edit. Its rotation must carry only the cert
    and a fresh content key — no roster or config baseline — so the member
    keeps the kick and its newer config."""
    w = world
    m_rm = w.m_app[space_remote_member_repo_key]
    # After the backup, but before the restore, x was kicked (M has the
    # tombstone) and the space renamed (M has the newer config). The
    # restored owner still lists x live and the old name.
    await m_rm.add(
        space_id=w.space_id,
        instance_id="x" * 32,
        user_id="ux",
        user_pk=None,
        display_name="ux",
    )
    await m_rm.remove(w.space_id, "x" * 32, "ux")
    await w.o_app[space_remote_member_repo_key].add(
        space_id=w.space_id,
        instance_id="x" * 32,
        user_id="ux",
        user_pk=None,
        display_name="ux",
    )
    await w.o_app[space_repo_key].add_space_instance(w.space_id, "x" * 32)
    await _config_from_b(w, w.m_app, seed=w.k1_seed, name="Newer name", seq=50)
    assert (await w.m_space()).name == "Newer name"

    await _restore_rotate(w)
    bundle = w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)[-1]
    assert bundle.get("baseline") is False
    assert "space_meta" not in bundle and "roster_entries" not in bundle
    await _deliver_bundle_to_m(w)

    m = await w.m_space()
    o = await w.o_space()
    assert m.identity_public_key == o.identity_public_key  # the pin moved
    assert m.name == "Newer name"  # config not rolled back
    gone = await m_rm.get_including_tombstones(w.space_id, "x" * 32, "ux")
    assert gone.tombstoned  # the kick stands
    # The fresh content key outranks every epoch the members hold, so a
    # household kicked after the backup cannot read what comes next.
    crypto = w.m_app[space_crypto_service_key]
    assert (
        await crypto.get_current_epoch(w.space_id)
        == (bundle["space_content_key"]["epoch"])
    )


async def test_after_a_restore_delegation_is_off_and_stays_off_until_re_enabled(
    world,
):
    """F1: the restore turns delegation OFF (rotating, sharing nothing), so
    a later revocation shares no seed; re-enabling delegation shares with
    the CURRENT admins only."""
    w = world
    await _restore_rotate(w)
    assert (await w.o_space()).features.delegated_admin_authority is False
    assert not w.outbound(event_type=FET.SPACE_ADMIN_KEY_SHARE)
    await _demote_b(w)
    assert not w.outbound(event_type=FET.SPACE_ADMIN_KEY_SHARE)
    await w.o_app[space_service_key].update_config(
        w.space_id,
        actor_username="anna",
        features=SpaceFeatures(delegated_admin_authority=True),
    )
    targets = {
        t for s, t, et, _p in w.sent if s == w.o_id and et is FET.SPACE_ADMIN_KEY_SHARE
    }
    assert targets == {A_ID}


async def test_member_resets_nothing_from_a_bundle_marked_non_baseline(world):
    """M1, receiver side: a bundle marked ``baseline: false`` moves the pin
    and installs the content key, but its config and roster parts — even
    validly signed ones — reset nothing, and it claims the epoch so no
    baseline can follow at that epoch."""
    w = world
    await _config_from_b(w, w.m_app, seed=w.k1_seed, name="Kept", seq=10**6)
    await _demote_b(w)
    bundle = dict(w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)[-1])
    assert "space_meta" in bundle  # an ordinary rotation IS a baseline
    full = dict(bundle)
    bundle["baseline"] = False
    await _deliver(
        w.m_app, sender=w.o_id, event_type=FET.SPACE_AUTHORITY_ROTATED, payload=bundle
    )
    m = await w.m_space()
    assert m.identity_public_key == (await w.o_space()).identity_public_key
    assert m.name == "Kept"
    crypto = w.m_app[space_crypto_service_key]
    assert (
        await crypto.get_current_epoch(w.space_id)
        == (bundle["space_content_key"]["epoch"])
    )
    # The same epoch's baseline bundle, redelivered, is now a no-op.
    await _deliver(
        w.m_app, sender=w.o_id, event_type=FET.SPACE_AUTHORITY_ROTATED, payload=full
    )
    assert (await w.m_space()).name == "Kept"


async def test_post_restore_content_epoch_outranks_epochs_the_backup_missed(world):
    """M1: members already hold content epoch 10**6 the restored owner
    never saw; the restore rotation's content key still becomes current."""
    w = world
    crypto = w.m_app[space_crypto_service_key]
    await crypto.import_key(w.space_id, 10**6, b"\x05" * 32, rotated_by=w.o_id)
    await _restore_rotate(w)
    await _deliver_bundle_to_m(w)
    bundle = w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)[-1]
    assert bundle["space_content_key"]["epoch"] > 10**6
    assert (
        await crypto.get_current_epoch(w.space_id)
        == (bundle["space_content_key"]["epoch"])
    )


async def test_lost_seed_of_a_rotated_space_is_reminted_by_a_real_rotation(world):
    """F6: an owner that lost the seed of an already-rotated space re-mints
    it through a rotation — members get a cert for the new key and follow,
    instead of the pin silently forking."""
    w = world
    await _rotated(w)
    await w.o_app[db_key].enqueue(
        "UPDATE spaces SET identity_private_key=NULL WHERE id=?", (w.space_id,)
    )
    before = len(w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED))
    seed = await w.o_app[space_service_key].ensure_space_seed(w.space_id)
    assert seed is not None
    assert (
        len(w.outbound(to=w.m_id, event_type=FET.SPACE_AUTHORITY_ROTATED)) == before + 1
    )
    await _deliver_bundle_to_m(w)
    assert (await w.m_space()).identity_public_key == (
        await w.o_space()
    ).identity_public_key
