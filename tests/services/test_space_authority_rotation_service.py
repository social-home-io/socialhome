"""Unit tests for :class:`SpaceAuthorityRotationService` (v_44).

The end-to-end behaviour (two real applications, real envelopes) lives in
``tests/protocol/test_space_authority_rotation.py``. This file pins the
service's own branches with real repos and a recording federation double.
"""

from __future__ import annotations

import asyncio
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
    SpaceAuthorityEchoDue,
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


# ── v_46: the authority epoch echo ────────────────────────────────────────


async def _begin(env, *, sender="v44", echo=None, space_id=SPACE, raw=False):
    payload: dict = {"space_id": space_id}
    if raw or echo is not None:
        payload["authority_epoch_echo"] = echo
    await env.fed.registered[FET.SPACE_SYNC_BEGIN](
        FederationEvent(
            msg_id=str(uuid.uuid4()),
            event_type=FET.SPACE_SYNC_BEGIN,
            from_instance=sender,
            to_instance=env.fed.own_instance_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload=payload,
            space_id=space_id,
        )
    )
    await env.svc.wait_idle()


async def _member_space(env, sid="sp-m", host="host-h") -> None:
    await env.spaces.save(
        Space(
            id=sid,
            name="M",
            owner_instance_id=host,
            owner_username="h",
            identity_public_key="aa" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )


async def test_attach_to_registers_the_echo_reader(env):
    assert FET.SPACE_SYNC_BEGIN in env.fed.registered


async def test_rotation_records_its_header_and_cert_on_the_owner_row(env):
    """A re-sent bundle must say what the original said: each rotation
    stores its header (kind, prior, forgotten — cumulative maxima) and the
    cert it signed."""
    first = await env.svc.rotate(SPACE)
    second = await env.svc.rotate(SPACE, baseline=False, forgotten_key_epoch=5)
    assert await env.spaces.get_authority_echo(SPACE) == {
        "epoch": second,
        "baseline": False,
        "prior_key_epoch": first,
        "forgotten_key_epoch": 5,
        "max_forgotten": 5,
        "max_baseline": first,
    }
    cert = await env.spaces.get_authority_cert(SPACE)
    assert cert is not None and cert["key_epoch"] == second


async def test_rotate_min_epoch_and_forgotten_epoch(env):
    floor = int(time.time()) + 50_000
    epoch = await env.svc.rotate(
        SPACE, baseline=False, min_epoch=floor, forgotten_key_epoch=7
    )
    assert epoch == floor
    bundle = _sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)[-1]
    assert bundle["forgotten_key_epoch"] == 7 and bundle["baseline"] is False


async def test_echo_is_ignored_unless_well_addressed(env):
    await env.svc.rotate(SPACE)
    env.fed.sent.clear()
    await _begin(env)  # no echo (an older member)
    await _begin(env, echo={"key_epoch": 0}, space_id="")  # no space
    await _begin(env, sender=env.fed.own_instance_id, echo={"key_epoch": 0})
    await _begin(env, echo={"key_epoch": 0}, space_id="no-such-space")
    await _member_space(env)
    await _begin(env, echo={"key_epoch": 0}, space_id="sp-m")  # not ours
    await _begin(env, echo=None, raw=True)  # malformed: null
    await _begin(env, echo={"owed_epoch": -3})  # malformed field
    assert env.fed.sent == []


async def test_behind_member_gets_its_bundle_resent_once_per_interval(env):
    epoch = await env.svc.rotate(SPACE)
    env.fed.sent.clear()
    await _begin(env, echo={"key_epoch": epoch, "baseline_epoch": 0})
    first = _sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)
    assert len(first) == 1 and "baseline" not in first[0]
    assert first[0]["authority_cert"]["key_epoch"] == epoch
    assert {t for t, _e, _p in env.fed.sent} == {"v44"}
    await _begin(env, echo={"key_epoch": epoch, "owed_epoch": 3})
    assert len(_sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)) == 1


async def test_caught_up_member_gets_nothing(env):
    epoch = await env.svc.rotate(SPACE)
    env.fed.sent.clear()
    await _begin(env, echo={"key_epoch": epoch, "baseline_epoch": epoch})
    assert env.fed.sent == []


async def test_resend_after_a_non_baseline_rotation_says_so(env):
    first = await env.svc.rotate(SPACE)
    await env.svc.rotate(SPACE, baseline=False, forgotten_key_epoch=4)
    env.fed.sent.clear()
    await _begin(env, echo={"key_epoch": 0})
    bundle = _sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)[-1]
    assert bundle["baseline"] is False
    assert bundle["prior_key_epoch"] == first
    assert bundle["forgotten_key_epoch"] == 4


async def test_resend_without_a_header_or_stored_cert_is_a_baseline(env):
    """A pre-v_46 rotation left neither: re-signed cert, sent as a baseline."""
    epoch = await env.svc.rotate(SPACE, baseline=False)
    await env.spaces.set_authority_echo(SPACE, None)
    await env.db.enqueue("UPDATE spaces SET authority_cert_json=NULL")
    env.fed.sent.clear()
    await _begin(env, echo={"key_epoch": 0})
    bundle = _sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)[-1]
    assert "baseline" not in bundle
    assert bundle["authority_cert"]["key_epoch"] == epoch


async def test_proof_must_be_our_own_cert_for_that_epoch(env):
    epoch = await env.svc.rotate(SPACE)
    cert = await env.spaces.get_authority_cert(SPACE)
    svc = env.svc
    space = await env.spaces.get(SPACE)
    assert svc._cert_proves(space, cert, epoch)
    assert not svc._cert_proves(space, cert, epoch + 1)
    assert not svc._cert_proves(space, None, epoch)
    assert not svc._cert_proves(space, {**cert, "cert_sig_suite": "x"}, epoch)
    assert not svc._cert_proves(space, {**cert, "cert_sig": "00"}, epoch)
    bare = SpaceAuthorityRotationService(
        space_repo=env.spaces,
        remote_member_repo=env.remote,
        bus=EventBus(),
        own_instance_id=env.fed.own_instance_id,
    )
    assert not bare._cert_proves(space, cert, epoch)


async def test_echo_triggered_rotation_failure_is_logged(env, caplog, monkeypatch):
    async def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(SpaceAuthorityRotationService, "rotate", _boom)
    await env.svc._rotate_past_forgotten(SPACE, 3)
    assert "echo-triggered rotation" in caplog.text


async def test_resend_needs_federation_a_seed_and_our_space(env):
    await env.svc.rotate(SPACE)
    env.fed.sent.clear()
    await env.spaces.clear_space_seed(SPACE)
    await env.svc._resend_bundle(SPACE, "v44")
    await env.svc._resend_bundle("no-such-space", "v44")
    assert env.fed.sent == []
    bare = SpaceAuthorityRotationService(
        space_repo=env.spaces,
        remote_member_repo=env.remote,
        bus=EventBus(),
        own_instance_id=env.fed.own_instance_id,
    )
    await bare._resend_bundle(SPACE, "v44")  # federation not wired: no-op
    assert await bare._current_content_key(SPACE) is None


async def test_current_content_key_is_fail_soft(env):
    class _Boom:
        async def export_current_key(self, _sid):
            raise RuntimeError("boom")

    class _Empty:
        async def export_current_key(self, _sid):
            return None

    env.svc.attach_space_crypto(_Boom())
    assert await env.svc._current_content_key(SPACE) is None
    env.svc.attach_space_crypto(_Empty())
    assert await env.svc._current_content_key(SPACE) is None


async def test_parse_echo():
    parse = SpaceAuthorityRotationService._parse_echo
    assert parse({}) == (0, 0, 0, 0)
    assert parse({"key_epoch": 4, "forgotten_epoch": 2}) == (4, 0, 0, 2)
    for bad in (None, [], {"key_epoch": True}, {"baseline_epoch": "1"}):
        assert parse(bad) is None


async def test_member_echo_only_to_the_owner_with_something_to_report(env):
    await _member_space(env)
    svc = env.svc
    assert await svc.authority_epoch_echo("no-such-space", "host-h") is None
    assert await svc.authority_epoch_echo(SPACE, "v44") is None  # we host it
    assert await svc.authority_epoch_echo("sp-m", "someone") is None
    assert await svc.authority_epoch_echo("sp-m", "host-h") is None  # all zero
    await env.db.enqueue(
        "UPDATE spaces SET authority_key_epoch=9, authority_baseline_epoch=-4"
        " WHERE id='sp-m'"
    )
    # host-h has no remote_instances row (mesh-only): version unknown → sent.
    # No cert held for the pin → no proof rides along.
    assert await svc.authority_epoch_echo("sp-m", "host-h") == {
        "key_epoch": 9,
        "baseline_epoch": 0,
        "owed_epoch": 4,
        "forgotten_epoch": 0,
    }
    unwired = SpaceAuthorityRotationService(
        space_repo=env.spaces,
        remote_member_repo=env.remote,
        bus=EventBus(),
        own_instance_id="x",
    )
    assert await unwired.authority_epoch_echo("sp-m", "host-h") is None


async def test_note_forgotten_is_durable_and_cleared_only_when_named(env):
    seen: list = []

    async def _rec(ev):
        seen.append(ev)

    env.bus.subscribe(SpaceAuthorityEchoDue, _rec)
    await _member_space(env)
    space = replace(await env.spaces.get("sp-m"), authority_key_epoch=100)
    svc = env.svc
    proof = {"key_epoch": 50, "cert_sig": "x"}
    # prior 10 < held 50 < 100: the owner forgot 50.
    await svc._note_forgotten(space, {"prior_key_epoch": 10}, 50, proof)
    await svc._note_forgotten(space, {"prior_key_epoch": 10}, 50, proof)  # once
    assert await env.spaces.get_authority_echo("sp-m") == {
        "forgotten_epoch": 50,
        "forgotten_cert": proof,
    }
    assert len(seen) == 1
    # Not proof of anything: no prior, prior not below held, held not below
    # the new epoch; and a bundle naming a LOWER epoch, or a prior at or
    # above ours (what an accomplice can provoke), never clears it.
    for p, held in (
        ({}, 60),
        ({"prior_key_epoch": 20}, 15),
        ({"prior_key_epoch": 10}, 100),
        ({"prior_key_epoch": 70, "forgotten_key_epoch": 1}, 70),
    ):
        await svc._note_forgotten(space, p, held, None)
    assert (await env.spaces.get_authority_echo("sp-m"))["forgotten_epoch"] == 50
    assert len(seen) == 1
    # A bundle that names 50 clears it.
    await svc._note_forgotten(space, {"forgotten_key_epoch": 50}, 100, None)
    assert await env.spaces.get_authority_echo("sp-m") == {}
    # A held epoch the bundle already names is no news; a cert for another
    # epoch is no proof.
    await svc._note_forgotten(
        space, {"prior_key_epoch": 10, "forgotten_key_epoch": 60}, 60, None
    )
    assert await env.spaces.get_authority_echo("sp-m") == {}
    await svc._note_forgotten(space, {"prior_key_epoch": 10}, 40, proof)
    assert await env.spaces.get_authority_echo("sp-m") == {
        "forgotten_epoch": 40,
        "forgotten_cert": None,
    }


async def test_post_restore_rotation_records_the_restore_window(env):
    """Only the post-restore rotation sets the window; later rotations
    (echo-triggered or not) carry it unchanged."""
    first = await env.svc.rotate(SPACE)
    restored = await env.svc.rotate(SPACE, baseline=False, after_restore=True)
    await env.svc.rotate(SPACE, baseline=False, forgotten_key_epoch=first)
    record = await env.spaces.get_authority_echo(SPACE)
    assert (record["restore_prior"], record["restore_epoch"]) == (first, restored)
    window = SpaceAuthorityRotationService._in_restore_window
    assert window(record, first) is False  # the backup's own epoch
    assert window(record, restored) is False  # the post-restore epoch
    assert window({"restore_prior": 1, "restore_epoch": 9}, 5) is True
    assert (
        window({"restore_prior": 1, "restore_epoch": 9, "max_forgotten": 5}, 5) is False
    )
    assert window({}, 5) is False


async def test_a_failed_echo_rotation_gives_the_window_back(env, monkeypatch):
    """Re-review (b): a rotation that rotated nothing (lost race, raised)
    must not spend the per-space window or drop the proof."""
    results = iter([None, RuntimeError("boom")])

    async def _fails(*_a, **_k):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(SpaceAuthorityRotationService, "rotate", _fails)
    svc = env.svc
    svc._echo_pending[SPACE] = 7
    assert svc._start_pending_rotation(SPACE)
    await svc.wait_idle()
    assert SPACE not in svc._echo_rotated and svc._echo_pending[SPACE] == 7
    svc._echo_rotated[SPACE] = time.monotonic() - 10 * mod.ECHO_ROTATION_INTERVAL_S
    earlier = svc._echo_rotated[SPACE]
    assert svc._start_pending_rotation(SPACE)
    await svc.wait_idle()
    assert svc._echo_rotated[SPACE] == earlier and svc._echo_pending[SPACE] == 7


async def test_stop_drains_then_cancels_stragglers(env):

    svc = env.svc
    await svc.stop()  # nothing running: no-op
    svc._closed = False  # reopen: the drain itself is what is under test
    done: list[str] = []

    async def _quick():
        done.append("quick")

    async def _stuck():
        await asyncio.Event().wait()

    svc._spawn(_quick(), "q")
    await svc.stop()
    assert done == ["quick"] and not svc._tasks
    svc._closed = False
    svc._spawn(_stuck(), "s")
    await svc.stop(timeout=0.05)
    assert not svc._tasks


async def test_spawn_after_stop_starts_nothing(env):
    """Re-review minor: once ``stop`` ran, no new echo task starts."""
    ran: list[str] = []

    async def _work():
        ran.append("x")

    await env.svc.stop()
    env.svc._spawn(_work(), "late")
    await asyncio.sleep(0)
    assert ran == [] and not env.svc._tasks


async def test_restore_gate_defers_echoes_and_rotations(env):
    """R1 defence in depth: while a restore has not been rotated for, an
    echo builds nothing (no rotation, no re-send), and a rotation already
    queued gives its window back instead of rotating."""
    pending = True

    async def _gate():
        return pending

    env.svc.attach_restore_gate(_gate)
    epoch = await env.svc.rotate(SPACE)
    env.fed.sent.clear()
    await _begin(env, echo={"key_epoch": 0})
    assert env.fed.sent == []
    env.svc._echo_pending[SPACE] = 3
    assert env.svc._start_pending_rotation(SPACE)
    await env.svc.wait_idle()
    assert (await env.spaces.get(SPACE)).authority_key_epoch == epoch
    assert env.svc._echo_pending[SPACE] == 3 and SPACE not in env.svc._echo_rotated
    pending = False
    await _begin(env, echo={"key_epoch": 0})
    assert _sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)


async def test_a_failing_restore_gate_fails_closed(env):
    async def _boom():
        raise RuntimeError("db gone")

    env.svc.attach_restore_gate(_boom)
    await env.svc.rotate(SPACE)
    env.fed.sent.clear()
    await _begin(env, echo={"key_epoch": 0})
    assert env.fed.sent == []


class _SeedSvc:
    def __init__(self):
        self.shared: list[str] = []

    async def share_admin_signing_seed(self, space, *, instance_id):
        self.shared.append(instance_id)

    async def roster_snapshot_entries(self, space, *, seed, to_instance_id):
        return []


async def test_restore_retry_rotates_only_spaces_not_rotated_for_that_marker(env):
    """Review M-a: a retry after a partial failure must not re-rotate a
    space that already rotated for this restore — that would overwrite its
    restore window (R, E1) with (E1, E1') and lose the forgotten range."""
    assert await env.svc.rotate_hosted_after_restore("m1") == 1
    header = await env.spaces.get_authority_echo(SPACE)
    window = (header["restore_prior"], header["restore_epoch"])
    assert header["restore_marker"] == "m1"
    await env.svc.rotate(SPACE)  # an ordinary rotation carries the marker
    assert (await env.spaces.get_authority_echo(SPACE))["restore_marker"] == "m1"
    epoch = (await env.spaces.get(SPACE)).authority_key_epoch
    assert await env.svc.rotate_hosted_after_restore("m1") == 0  # the retry
    header = await env.spaces.get_authority_echo(SPACE)
    assert (header["restore_prior"], header["restore_epoch"]) == window
    assert (await env.spaces.get(SPACE)).authority_key_epoch == epoch
    # A NEW restore rotates again and records its own window.
    assert await env.svc.rotate_hosted_after_restore("m2") == 1
    assert (await env.spaces.get_authority_echo(SPACE))["restore_marker"] == "m2"


async def test_restore_retry_still_rotates_a_space_whose_first_run_failed(
    env, monkeypatch
):
    """The first run turned delegation off, then failed before rotating:
    the space now looks history-less (epoch 0, delegation off), but a seed
    was shared, so the retry still rotates it."""
    await env.spaces.mark_seed_shared(SPACE)

    async def _boom(*_a, **_k):
        raise RuntimeError("boom")

    with monkeypatch.context() as m:
        m.setattr(SpaceAuthorityRotationService, "rotate", _boom)
        with pytest.raises(RuntimeError):
            await env.svc.rotate_hosted_after_restore("m1")
    space = await env.spaces.get(SPACE)
    assert space.features.delegated_admin_authority is False
    assert space.authority_key_epoch == 0
    assert await env.svc.rotate_hosted_after_restore("m1") == 1


async def test_revocation_while_restore_pending_rotates_but_shares_no_seed(env):
    """Review M-b: a revocation never waits for the restore review — it
    rotates — but the restored admin list may be stale, so the new seed
    goes to nobody until the post-restore rotation ran."""
    seeds = _SeedSvc()
    env.svc.attach_space_service(seeds)
    await env.remote.add(
        space_id=SPACE,
        instance_id="v43",
        user_id="u-admin",
        user_pk=None,
        display_name=None,
        role="admin",
    )
    pending = True

    async def _gate():
        return pending

    env.svc.attach_restore_gate(_gate)
    before = (await env.spaces.get(SPACE)).authority_key_epoch
    await env.bus.publish(SpaceAdminAuthorityRevoked(space_id=SPACE, instance_id="x"))
    assert (await env.spaces.get(SPACE)).authority_key_epoch > before
    assert seeds.shared == []
    pending = False
    await env.bus.publish(SpaceAdminAuthorityRevoked(space_id=SPACE, instance_id="x"))
    assert seeds.shared == ["v43"]


async def test_pending_gate_warns_once_per_boot(env, caplog):
    async def _gate():
        return True

    env.svc.attach_restore_gate(_gate)
    with caplog.at_level("WARNING"):
        assert await env.svc._restore_review_pending()
        assert await env.svc._restore_review_pending()
    hits = [r for r in caplog.records if "has not completed" in r.getMessage()]
    assert len(hits) == 1 and hits[0].levelname == "WARNING"


# ─── v_49: the rotation bundle re-issues each household's writer cert ────


class _Certs:
    def __init__(self):
        self.asked: list[tuple[str, str, int | None]] = []
        self.accepted: list[tuple[str, object]] = []

    async def cert_for_peer(self, space_id, instance_id, *, epoch=None):
        self.asked.append((space_id, instance_id, epoch))
        return {"for": instance_id, "epoch": epoch}

    async def accept(self, space_id, raw):
        self.accepted.append((space_id, raw))
        return True

    async def channel_grant_for_peer(self, space_id, instance_id, *, epoch=None):
        return None

    async def accept_channel_grant(self, space_id, raw):
        self.accepted.append((space_id, raw))
        return True


async def test_rotation_bundle_carries_each_households_own_cert(env):
    certs = _Certs()
    env.svc.attach_writer_certs(certs)
    await env.svc.rotate(SPACE)
    bundle = _sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)[0]
    epoch = bundle["space_content_key"]["epoch"]
    assert bundle["writer_cert"] == {"for": "v44", "epoch": epoch}
    mesh = _sent(env, "mesh", FET.SPACE_AUTHORITY_ROTATED)[0]
    assert mesh["writer_cert"] == {"for": "mesh", "epoch": epoch}
    # The legacy (below v44) rekey path carries no cert.
    legacy = _sent(env, "v43", FET.SPACE_KEY_EXCHANGE_REKEY)[0]
    assert "writer_cert" not in legacy


async def test_rotation_bundle_without_a_cert_has_no_field(env):
    certs = _Certs()

    async def _none(*_a, **_k):
        return None

    certs.cert_for_peer = _none  # type: ignore[method-assign]
    env.svc.attach_writer_certs(certs)
    await env.svc.rotate(SPACE)
    assert "writer_cert" not in _sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)[0]


async def test_member_stores_the_cert_from_the_bundle(env):
    await env.svc.rotate(SPACE)
    bundle = _sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)[0]
    member, handler = await _member_env(env)
    certs = _Certs()
    member.attach_writer_certs(certs)
    # Make the space look hosted elsewhere for the member-side handler.
    await env.db.enqueue(
        "UPDATE spaces SET owner_instance_id=? WHERE id=?",
        (env.fed.own_instance_id, SPACE),
    )
    cert = {"for": "member-household"}
    await handler(
        FederationEvent(
            msg_id=str(uuid.uuid4()),
            event_type=FET.SPACE_AUTHORITY_ROTATED,
            from_instance=env.fed.own_instance_id,
            to_instance="member-household",
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload={**bundle, "writer_cert": cert},
            space_id=SPACE,
        )
    )
    assert certs.accepted == [(SPACE, cert)]


async def test_member_ignores_a_bundle_cert_from_a_non_owner(env):
    await env.svc.rotate(SPACE)
    bundle = _sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)[0]
    member, handler = await _member_env(env)
    certs = _Certs()
    member.attach_writer_certs(certs)
    await handler(
        FederationEvent(
            msg_id=str(uuid.uuid4()),
            event_type=FET.SPACE_AUTHORITY_ROTATED,
            from_instance="not-the-owner",
            to_instance="member-household",
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload={**bundle, "writer_cert": {"x": 1}},
            space_id=SPACE,
        )
    )
    assert certs.accepted == []


async def test_a_re_pin_re_announces_the_epoch_before_the_reseal(env):
    """v_49: the re-pin clears the GFS epoch state — the owner re-announces
    the current epoch right after the republish, before the re-seal."""
    calls: list[str] = []

    class _Gfs:
        async def republish_space(self, space_id):
            calls.append("republish")
            return 1

    class _Keys:
        async def reconcile_space_everywhere(self, space_id):
            calls.append("reseal")

    class _Member:
        async def announce_epoch(self, space_id):
            calls.append("notice")
            raise RuntimeError("fail-soft")

        async def reconcile_channel(self, space_id):
            return None

    env.svc.attach_gfs(_Gfs())
    env.svc.attach_subscriber_keys(_Keys())
    env.svc.attach_member_gfs(_Member())
    await env.db.enqueue("UPDATE spaces SET space_type='global' WHERE id=?", (SPACE,))
    await env.svc.rotate(SPACE)
    assert calls == ["republish", "notice", "reseal"]


# ── v_51: a private space's fresh channel rides the bundle ───────────────


async def test_the_channel_is_reconciled_before_bundles_carry_its_grant(env):
    order: list[str] = []

    class _Member:
        async def retire_channel(self, space_id):
            # Still signed by the OLD seed: before the swap.
            space = await env.svc._spaces.get(space_id)
            order.append(f"retire:{space.authority_key_epoch}")

        async def reconcile_channel(self, space_id):
            order.append("reconcile")

        async def announce_epoch(self, space_id):
            return 0

    class _GrantCerts(_Certs):
        async def channel_grant_for_peer(self, space_id, instance_id, *, epoch=None):
            order.append(f"grant:{instance_id}")
            return {"grant_for": instance_id, "epoch": epoch}

    env.svc.attach_member_gfs(_Member())
    env.svc.attach_writer_certs(_GrantCerts())
    await env.svc.rotate(SPACE)
    bundle = _sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)[0]
    epoch = bundle["space_content_key"]["epoch"]
    assert bundle["gfs_channel"] == {"grant_for": "v44", "epoch": epoch}
    assert order[0] == "retire:0" and order[1] == "reconcile"


async def test_member_takes_the_channel_grant_from_the_owner_bundle_only(env):
    await env.svc.rotate(SPACE)
    bundle = _sent(env, "v44", FET.SPACE_AUTHORITY_ROTATED)[0]
    member, handler = await _member_env(env)
    certs = _Certs()
    member.attach_writer_certs(certs)
    await env.db.enqueue(
        "UPDATE spaces SET owner_instance_id=? WHERE id=?",
        (env.fed.own_instance_id, SPACE),
    )
    for sender in ("not-the-owner", env.fed.own_instance_id):
        await handler(
            FederationEvent(
                msg_id=str(uuid.uuid4()),
                event_type=FET.SPACE_AUTHORITY_ROTATED,
                from_instance=sender,
                to_instance="member-household",
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload={**bundle, "gfs_channel": {"grant": sender}},
                space_id=SPACE,
            )
        )
    assert certs.accepted == [(SPACE, {"grant": env.fed.own_instance_id})]
