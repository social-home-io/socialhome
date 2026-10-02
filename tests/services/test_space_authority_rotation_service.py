"""Unit tests for :class:`SpaceAuthorityRotationService` (v_44).

The end-to-end behaviour (two real applications, real envelopes) lives in
``tests/protocol/test_space_authority_rotation.py``. This file pins the
service's own branches with real repos and a recording federation double.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from socialhome.authority_cert import sign_authority_cert
from socialhome.authority_sig import sign_authority_event, strip_authority_sig_fields
from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    SpaceAdminAuthorityRevoked,
    SpaceAdminSeedsRetiredAfterRestore,
)
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEvent,
    FederationEventType,
)
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services import space_authority_rotation_service as mod
from socialhome.services.space_authority_rotation_service import (
    SpaceAuthorityRotationService,
)
from socialhome.services.space_crypto_service import SpaceContentEncryption

FET = FederationEventType
SPACE = "sp-u"


class _Fed:
    """Owner identity + a recording send path + per-peer versions."""

    def __init__(self, kp, versions: dict[str, int]):
        self.own_identity_seed = kp.private_key
        self.own_identity_pk = kp.public_key
        self.own_instance_id = derive_instance_id(kp.public_key)
        self._versions = versions
        self.sent: list[tuple[str, FederationEventType, dict]] = []
        self.registered: dict = {}
        self._event_registry = SimpleNamespace(
            register=lambda et, h: self.registered.__setitem__(et, h)
        )

    async def peer_supports(self, iid, *, min_version):
        return self._versions.get(iid, 0) >= min_version

    async def send_with_mesh_fallback(
        self, *, to_instance_id, event_type, payload, space_id=None
    ):
        self.sent.append((to_instance_id, event_type, payload))
        return DeliveryResult(instance_id=to_instance_id, ok=True)


class _FedRepo:
    def __init__(self, members: list[str], known: set[str]):
        self._members = members
        self._known = known

    async def list_member_instance_ids(self, _space_id):
        return list(self._members)

    async def get_instance(self, iid):
        return object() if iid in self._known else None


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "rot.db", batch_timeout_ms=10)
    await db.startup()
    kek = KeyManager(b"\x0b" * 32)
    spaces = SqliteSpaceRepo(db, key_manager=kek)
    remote = SqliteSpaceRemoteMemberRepo(db)
    owner = generate_identity_keypair()
    fed = _Fed(owner, {"v44": 44, "v43": 43})
    k1 = generate_identity_keypair()
    await spaces.save(
        Space(
            id=SPACE,
            name="S",
            owner_instance_id=fed.own_instance_id,
            owner_username="anna",
            identity_public_key=k1.public_key.hex(),
            config_sequence=0,
            features=SpaceFeatures(delegated_admin_authority=True),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    await spaces.set_space_seed(SPACE, k1.private_key)
    crypto = SpaceContentEncryption(
        SqliteSpaceKeyRepo(db), kek, own_instance_id=fed.own_instance_id
    )
    await crypto.initialise_for_space(SPACE)
    for inst in ("v44", "v43", "mesh"):
        await remote.add(
            space_id=SPACE,
            instance_id=inst,
            user_id=f"u-{inst}",
            user_pk=None,
            display_name=None,
        )
    bus = EventBus()
    svc = SpaceAuthorityRotationService(
        space_repo=spaces,
        remote_member_repo=remote,
        bus=bus,
        own_instance_id=fed.own_instance_id,
    )
    svc.attach_federation(fed, _FedRepo(["v44", "v43", "mesh"], {"v44", "v43"}))
    svc.attach_space_crypto(crypto)
    svc.wire()
    svc.attach_to(fed)
    yield SimpleNamespace(
        db=db,
        spaces=spaces,
        remote=remote,
        fed=fed,
        svc=svc,
        bus=bus,
        crypto=crypto,
        k1=k1,
    )
    await db.shutdown()


def _sent(env, to, et):
    return [p for t, e, p in env.fed.sent if t == to and e is et]


async def test_attach_to_registers_the_bundle_handler(env):
    assert FET.SPACE_AUTHORITY_ROTATED in env.fed.registered


async def test_rotate_skips_households_without_a_live_seat(env):
    """I1: a household whose last seat is a tombstone gets nothing — not
    the bundle, not the content key."""
    await env.remote.remove(SPACE, "v44", "u-v44")
    await env.svc.rotate(SPACE)
    assert not [p for t, _e, p in env.fed.sent if t == "v44"]


async def test_rotate_routes_by_peer_version(env):
    """v44 → bundle only; below v44 → unsigned rekey only; a mesh-only
    member of unknown version → both (an older one drops the bundle)."""
    assert await env.svc.rotate(SPACE) > 0
    assert _sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)
    assert not _sent(env, "v44", FET.SPACE_KEY_EXCHANGE_REKEY)
    assert not _sent(env, "v43", FET.SPACE_AUTHORITY_ROTATED)
    legacy = _sent(env, "v43", FET.SPACE_KEY_EXCHANGE_REKEY)
    assert legacy and "authority_sig" not in legacy[0]["space_content_key"]
    assert _sent(env, "mesh", FET.SPACE_AUTHORITY_ROTATED)
    assert _sent(env, "mesh", FET.SPACE_KEY_EXCHANGE_REKEY)
    bundle = _sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)[0]
    assert set(bundle) == {
        "space_id",
        "authority_cert",
        "space_meta",
        "roster_entries",
        "roster_version",
        "space_content_key",
    }


async def test_rotate_is_owner_only_and_needs_federation(env):
    other = SpaceAuthorityRotationService(
        space_repo=env.spaces,
        remote_member_repo=env.remote,
        bus=EventBus(),
        own_instance_id="not-the-owner",
    )
    assert await other.rotate(SPACE) is None
    other.attach_federation(env.fed, _FedRepo([], set()))
    assert await other.rotate(SPACE) is None
    assert await env.svc.rotate("no-such-space") is None


async def test_rotate_losing_the_compare_and_set_is_a_noop(env):
    class _Racing:
        def __getattr__(self, name):
            return getattr(env.spaces, name)

        async def rotate_authority_key(self, *_a, **_k):
            return False

    racing = SpaceAuthorityRotationService(
        space_repo=_Racing(),
        remote_member_repo=env.remote,
        bus=EventBus(),
        own_instance_id=env.fed.own_instance_id,
    )
    racing.attach_federation(env.fed, _FedRepo(["v44"], {"v44"}))
    assert await racing.rotate(SPACE) is None
    assert not env.fed.sent


async def test_revocation_ignored_for_a_space_we_do_not_host(env):
    await env.spaces.save(
        Space(
            id="sp-other",
            name="O",
            owner_instance_id="someone-else",
            owner_username="x",
            identity_public_key="aa" * 32,
            config_sequence=0,
            features=SpaceFeatures(delegated_admin_authority=True),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    await env.bus.publish(
        SpaceAdminAuthorityRevoked(space_id="sp-other", instance_id="x")
    )
    assert (await env.spaces.get("sp-other")).authority_key_epoch == 0


async def test_delegation_off_event_rotates_unconditionally(env):
    await env.bus.publish(SpaceAdminAuthorityRevoked(space_id=SPACE))
    assert (await env.spaces.get(SPACE)).authority_key_epoch > 0


async def _deliver(env, payload, *, sender):
    await env.fed.registered[FET.SPACE_AUTHORITY_ROTATED](
        FederationEvent(
            msg_id=str(uuid.uuid4()),
            event_type=FET.SPACE_AUTHORITY_ROTATED,
            from_instance=sender,
            to_instance="us",
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload=payload,
            space_id=payload.get("space_id"),
        )
    )


async def test_bundle_for_our_own_or_an_unknown_space_is_ignored(env):
    await env.svc.rotate(SPACE)
    bundle = _sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)[0]
    pin = (await env.spaces.get(SPACE)).identity_public_key
    await _deliver(env, bundle, sender=env.fed.own_instance_id)  # our own space
    assert (await env.spaces.get(SPACE)).identity_public_key == pin
    await _deliver(env, {**bundle, "space_id": "nope"}, sender="x")  # no error
    await _deliver(env, {}, sender="x")


async def _member_env(env):
    """A member household's view: same space, owned by ``env.fed``'s
    household, pinned to K1, processed by a member-side service."""
    member = SpaceAuthorityRotationService(
        space_repo=env.spaces,
        remote_member_repo=env.remote,
        bus=env.bus,
        own_instance_id="member-household",
    )
    member.attach_space_crypto(env.crypto)
    reg: dict = {}
    member.attach_to(
        SimpleNamespace(
            _event_registry=SimpleNamespace(
                register=lambda et, h: reg.__setitem__(et, h)
            )
        )
    )
    return member, reg[FET.SPACE_AUTHORITY_ROTATED]


async def test_bundle_parts_not_signed_by_the_new_key_are_ignored(env):
    """The cert applies, but a config / content key / roster entry signed
    with anything but the NEW key is not part of the owner's baseline."""
    k2 = generate_identity_keypair()
    cert = sign_authority_cert(
        space_id=SPACE,
        owner_instance_id=env.fed.own_instance_id,
        owner_seed=env.fed.own_identity_seed,
        owner_pk_hex=env.fed.own_identity_pk.hex(),
        authority_pk_hex=k2.public_key.hex(),
        key_epoch=1,
    )
    meta = {"name": "Forged", "config_sequence": 99}
    meta.update(
        sign_authority_event(
            event_type="space_config_changed",
            space_id=SPACE,
            payload=strip_authority_sig_fields(meta),
            space_seed=env.k1.private_key,
        )
    )
    key = {"epoch": 7, "key_suite": "bogus", "key_base64": "AA==", "rotated_by": "x"}
    key.update(
        sign_authority_event(
            event_type="space_key_exchange_rekey",
            space_id=SPACE,
            payload=strip_authority_sig_fields(key),
            space_seed=k2.private_key,
        )
    )
    _member, handler = await _member_env(env)
    epoch_before = await env.crypto.get_current_epoch(SPACE)
    await handler(
        FederationEvent(
            msg_id="m",
            event_type=FET.SPACE_AUTHORITY_ROTATED,
            from_instance=env.fed.own_instance_id,
            to_instance="member-household",
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload={
                "space_id": SPACE,
                "authority_cert": cert,
                "space_meta": meta,
                "space_content_key": key,
                "roster_entries": [
                    {"event_type": "space_member_joined", "payload": {}}
                ],
                "roster_version": "x",
            },
            space_id=SPACE,
        )
    )
    got = await env.spaces.get(SPACE)
    assert got.identity_public_key == k2.public_key.hex()  # the cert applied
    assert got.name == "S"  # K1-signed config ignored
    assert await env.crypto.get_current_epoch(SPACE) == epoch_before  # bad suite


async def test_public_space_republishes_then_reseals(env):
    """A public / global space: every GFS that lists it re-pins (republish)
    BEFORE the K2-signed subscriber re-seal, which a GFS only accepts once
    it re-pinned. A private space touches no GFS at all."""
    calls: list[str] = []

    class _Gfs:
        async def republish_space(self, space_id):
            calls.append(f"republish:{space_id}")
            return 1

    class _Keys:
        async def reconcile_space_everywhere(self, space_id):
            calls.append(f"reseal:{space_id}")

    env.svc.attach_gfs(_Gfs())
    env.svc.attach_subscriber_keys(_Keys())
    await env.svc.rotate(SPACE)  # private
    assert calls == []
    await env.db.enqueue("UPDATE spaces SET space_type='global' WHERE id=?", (SPACE,))
    await env.svc.rotate(SPACE)
    assert calls == [f"republish:{SPACE}", f"reseal:{SPACE}"]


async def test_rotate_hosted_after_restore_only_touches_spaces_with_history(env):
    """Delegation on (or an epoch > 0) → rotated; a plain hosted space with
    neither, and a space hosted elsewhere, are left alone."""
    await env.spaces.save(
        Space(
            id="sp-plain",
            name="P",
            owner_instance_id=env.fed.own_instance_id,
            owner_username="anna",
            identity_public_key="aa" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    assert await env.svc.rotate_hosted_after_restore() == 1
    assert (await env.spaces.get(SPACE)).authority_key_epoch > 0
    assert (await env.spaces.get("sp-plain")).authority_key_epoch == 0


async def test_epoch_is_at_least_wall_clock_seconds(env):
    before = int(time.time())
    first = await env.svc.rotate(SPACE)
    second = await env.svc.rotate(SPACE)
    assert first >= before and second > first


async def test_truncated_roster_never_tombstones_unscanned_seats(env, monkeypatch):
    """A bundle listing more seats than we verify is applied up to the cap,
    but nothing is tombstoned for being 'missing' — it may lie past the cut."""
    monkeypatch.setattr(mod, "_MAX_BUNDLE_ROSTER_ENTRIES", 1)
    await env.remote.add(
        space_id=SPACE, instance_id="x", user_id="ux", user_pk=None, display_name=None
    )
    await env.spaces.adopt_authority_key(SPACE, env.k1.public_key.hex(), 9)
    space = await env.spaces.get(SPACE)
    bare = {
        "space_id": SPACE,
        "user_id": "u-v44",
        "instance_id": "v44",
        "display_name": None,
        "user_pk": None,
        "role": "member",
        "member_version": 3,
        "roster_version": 3,
    }
    entry = {
        "event_type": "space_member_joined",
        "payload": {
            **bare,
            **sign_authority_event(
                event_type="space_member_joined",
                space_id=SPACE,
                payload=bare,
                space_seed=env.k1.private_key,
            ),
        },
    }
    await env.svc._reset_roster(space, [entry, entry], 1, space.authority_key_epoch)
    assert (await env.remote.get(SPACE, "v44", "u-v44")).member_version == 3
    assert await env.remote.get(SPACE, "x", "ux") is not None  # not tombstoned


async def test_post_restore_rotation_shares_no_seed_and_asks_the_owner(env):
    """After a restore: rotate, share the new seed with nobody, and tell the
    owner to review its admins; delegation is OFF until re-enabled."""
    shared: list[str] = []

    class _Svc:
        async def share_admin_signing_seed(self, space, *, instance_id):
            shared.append(instance_id)

        async def roster_snapshot_entries(self, space, *, seed, to_instance_id):
            return []

    await env.remote.add(
        space_id=SPACE,
        instance_id="v44",
        user_id="u-admin",
        user_pk=None,
        display_name=None,
        role="admin",
    )
    env.svc.attach_space_service(_Svc())
    notices: list = []

    async def _rec(e):
        notices.append(e)

    env.bus.subscribe(SpaceAdminSeedsRetiredAfterRestore, _rec)
    assert await env.svc.rotate_hosted_after_restore() == 1
    assert shared == []
    assert [n.space_id for n in notices] == [SPACE]
    assert (await env.spaces.get(SPACE)).features.delegated_admin_authority is False
    # Nothing shares until the owner turns delegation back on.
    await env.svc.rotate(SPACE)
    assert shared == []
    space = await env.spaces.get(SPACE)
    await env.spaces.save(
        replace(space, features=replace(space.features, delegated_admin_authority=True))
    )
    await env.svc.rotate(SPACE)
    assert shared == ["v44"]


async def test_post_restore_bundle_is_marked_non_baseline_with_its_prior_epoch(env):
    """M1: the restored roster / config are stale — the bundle is marked
    ``baseline: false`` (members reset to its snapshot only when they missed
    an earlier baseline), names the epoch it replaces, and carries a content
    epoch of at least wall-clock seconds."""
    before = int(time.time())
    prior = (await env.spaces.get(SPACE)).authority_key_epoch
    assert await env.svc.rotate_hosted_after_restore() == 1
    bundle = _sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)[-1]
    assert bundle["baseline"] is False
    assert bundle["prior_key_epoch"] == prior
    assert {"space_meta", "roster_entries", "roster_version"} <= set(bundle)
    assert bundle["space_content_key"]["epoch"] >= before


def test_missed_baseline_cutoff():
    cut = SpaceAuthorityRotationService._missed_baseline_cutoff
    # Up to date: nothing missed.
    assert cut(owed_epoch=5, baseline_epoch=5, prior_key_epoch=5, new_epoch=9) is None
    # Moved past 5 (adopted inline) without its bundle.
    assert cut(owed_epoch=5, baseline_epoch=0, prior_key_epoch=None, new_epoch=9) == 5
    # Never saw the owner's rotation to 7.
    assert cut(owed_epoch=5, baseline_epoch=5, prior_key_epoch=7, new_epoch=9) == 7
    # Nothing owed and the owner's prior already claimed.
    assert cut(owed_epoch=0, baseline_epoch=5, prior_key_epoch=5, new_epoch=9) is None
    # A malformed prior is ignored.
    assert (
        cut(owed_epoch=5, baseline_epoch=5, prior_key_epoch=True, new_epoch=9) is None
    )
    assert cut(owed_epoch=5, baseline_epoch=5, prior_key_epoch="7", new_epoch=9) is None


async def test_baseline_config_never_rolls_back_a_racing_new_key_edit(env, monkeypatch):
    """F4: a new-key config that lands between the bundle's read of the
    space and its baseline save stands — the save is conditional in the
    same transaction."""
    member, _handler = await _member_env(env)
    k2 = generate_identity_keypair()
    assert await env.spaces.adopt_authority_key(SPACE, k2.public_key.hex(), 9)
    space = await env.spaces.get(SPACE)
    meta = {"name": "Baseline", "config_sequence": 1}
    meta.update(
        sign_authority_event(
            event_type="space_config_changed",
            space_id=SPACE,
            payload=strip_authority_sig_fields(meta),
            space_seed=k2.private_key,
        )
    )
    # The concurrent edit, applied under epoch 9 — and a reader that saw
    # the space just BEFORE it (a separate check-then-save would pass).
    await env.db.enqueue(
        "UPDATE spaces SET name='Racing edit', authority_config_epoch=9 WHERE id=?",
        (SPACE,),
    )

    async def _stale_read(_space_id):
        return 0

    monkeypatch.setattr(env.spaces, "get_authority_config_epoch", _stale_read)
    await member._reset_config(space, meta, env.fed.own_instance_id, 9)
    assert (await env.spaces.get(SPACE)).name == "Racing edit"
