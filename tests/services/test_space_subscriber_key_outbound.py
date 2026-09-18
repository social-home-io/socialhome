"""Tests for :class:`SpaceSubscriberKeyOutbound` (Phase 5b-b producer).

On a GFS ``new_subscriber`` frame, a seed-holding household seals the
per-space content key to the new subscriber's published key-wrap pubkey
and relays it through the content-blind GFS as a space-authority-signed
``space_subscriber_key_handoff`` envelope.

Security invariants under test:

* the wire envelope carries NO plaintext key bytes (only inside
  ``sealed.ciphertext``);
* the wire envelope names NOBODY — no ``target_instance_id``, so neither the
  GFS nor a non-target subscriber learns which household is being onboarded;
  the seal is the gate;
* the subscriber's key-wrap binding is VERIFIED before sealing — a forged
  binding (key-wrap sig that doesn't match the identity) → NO relay
  (anti-GFS-substitution);
* a non-seed-holder → no relay; a private/household space → no relay.
"""

from __future__ import annotations

import base64
import json

import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
    strip_authority_sig_fields,
    verify_authority_event,
)
from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
)
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_space_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection, GfsSpacePublication
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.federation.keywrap_seal import open_keywrap
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_crypto_service import SpaceContentEncryption
from socialhome.services.space_subscriber_key_outbound import (
    SpaceSubscriberKeyOutbound,
)


class _CaptureGfs:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def publish_space_event(
        self, *, space_id, event_type, payload, from_instance
    ) -> int:
        self.calls.append(
            {
                "space_id": space_id,
                "event_type": event_type,
                "payload": payload,
                "from_instance": from_instance,
            }
        )
        return 1


def _subscriber_identity():
    """Return a subscriber's (identity_kp, keywrap_kp, instance_id, keywrap_sig)."""
    id_kp = generate_identity_keypair()
    kw_kp = generate_x25519_keypair()
    instance_id = derive_instance_id(id_kp.public_key)
    keywrap_sig = b64url_encode(sign_ed25519(id_kp.private_key, kw_kp.public_key))
    return id_kp, kw_kp, instance_id, keywrap_sig


def _new_subscriber_frame(space_id, instance_id, id_pub, kw_pub, keywrap_sig):
    return {
        "type": "new_subscriber",
        "space_id": space_id,
        "subscriber": {
            "instance_id": instance_id,
            "identity_public_key": id_pub.hex(),
            "keywrap_public_key": kw_pub.hex(),
            "kem_suite": "x25519",
            "keywrap_sig": keywrap_sig,
        },
    }


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    own_iid = "alpha.home"
    kek = KeyManager.from_data_dir(tmp_dir)
    space_repo = SqliteSpaceRepo(db, key_manager=kek)
    key_repo = SqliteSpaceKeyRepo(db)
    crypto = SpaceContentEncryption(key_repo, kek, own_instance_id=own_iid)
    gfs = _CaptureGfs()

    async def _make_space(space_id, stype, *, with_seed, join_mode=JoinMode.OPEN):
        skp = generate_space_keypair()
        await space_repo.save(
            Space(
                id=space_id,
                name="S",
                owner_instance_id=own_iid,
                owner_username="alice",
                identity_public_key=skp.public_key.hex(),
                config_sequence=0,
                features=SpaceFeatures(),
                space_type=stype,
                join_mode=join_mode,
            )
        )
        if with_seed:
            await space_repo.set_space_seed(space_id, skp.private_key)
        await crypto.initialise_for_space(space_id)
        return skp

    svc = SpaceSubscriberKeyOutbound(
        space_repo=space_repo,
        space_crypto=crypto,
        gfs_service=gfs,
    )
    svc.attach_identity(own_instance_id=own_iid)
    return {
        "db": db,
        "gfs": gfs,
        "crypto": crypto,
        "space_repo": space_repo,
        "make_space": _make_space,
        "svc": svc,
    }


async def test_valid_binding_relays_sealed_key_handoff(env):
    skp = await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    frame = _new_subscriber_frame(
        "sp-pub", sub_iid, id_kp.public_key, kw_kp.public_key, keywrap_sig
    )

    await env["svc"].handle(frame)

    assert len(env["gfs"].calls) == 1
    call = env["gfs"].calls[0]
    assert call["event_type"] == AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF
    assert call["space_id"] == "sp-pub"
    envelope = call["payload"]
    assert envelope["space_id"] == "sp-pub"
    # The wire envelope names NOBODY: no ``target_instance_id`` (the seal is
    # the gate), so neither the GFS nor a non-target subscriber learns which
    # household is being onboarded.
    assert "target_instance_id" not in envelope
    assert sub_iid not in json.dumps(envelope)
    assert set(envelope) == {
        "space_id",
        "sealed",
        "authority_sig",
        "authority_sig_suite",
    }

    # Authority signature verifies against the space public key (under the
    # handoff event type).
    assert verify_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
        space_id="sp-pub",
        payload=strip_authority_sig_fields(envelope),
        authority_sig=envelope["authority_sig"],
        authority_sig_suite=envelope["authority_sig_suite"],
        space_public_key=skp.public_key,
    )

    # GFS-blind: the raw content key bytes / its base64 NEVER appear on the wire
    # envelope — only inside sealed.ciphertext.
    epoch, raw_key = await env["crypto"].export_current_key("sp-pub")
    key_b64 = base64.b64encode(raw_key).decode("ascii")
    blob = json.dumps(envelope)
    assert key_b64 not in blob
    assert raw_key.hex() not in blob

    # The subscriber can open the seal with its key-wrap private key and recover
    # the content-key meta the inbound consumer feeds to
    # apply_space_content_key_from_metadata.
    pt = open_keywrap(
        sealed=envelope["sealed"], recipient_keywrap_priv=kw_kp.private_key
    )
    meta = json.loads(pt)
    inner = meta["space_content_key"]
    assert inner["epoch"] == epoch
    assert inner["key_base64"] == key_b64
    assert inner["key_suite"] == "aesgcm-256"


async def test_forged_binding_no_relay(env):
    """Anti-substitution: a key-wrap pubkey whose self-signature does NOT match
    the subscriber identity (a malicious GFS swapped in a key it controls) is
    rejected — no relay, the content key is never sealed to the attacker."""
    await env["make_space"]("sp-pub2", SpaceType.PUBLIC, with_seed=True)
    id_kp, _kw_kp, sub_iid, _good_sig = _subscriber_identity()
    # Attacker substitutes its OWN key-wrap key but cannot self-sign it as the
    # subscriber's identity, so it signs with an unrelated identity.
    attacker_kw = generate_x25519_keypair()
    attacker_id = generate_identity_keypair()
    forged_sig = b64url_encode(
        sign_ed25519(attacker_id.private_key, attacker_kw.public_key)
    )
    frame = _new_subscriber_frame(
        "sp-pub2", sub_iid, id_kp.public_key, attacker_kw.public_key, forged_sig
    )

    await env["svc"].handle(frame)

    assert env["gfs"].calls == []


async def test_non_seed_holder_no_relay(env):
    await env["make_space"]("sp-noseed", SpaceType.PUBLIC, with_seed=False)
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    frame = _new_subscriber_frame(
        "sp-noseed", sub_iid, id_kp.public_key, kw_kp.public_key, keywrap_sig
    )

    await env["svc"].handle(frame)

    assert env["gfs"].calls == []


async def test_private_space_no_relay(env):
    await env["make_space"]("sp-priv", SpaceType.PRIVATE, with_seed=True)
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    frame = _new_subscriber_frame(
        "sp-priv", sub_iid, id_kp.public_key, kw_kp.public_key, keywrap_sig
    )

    await env["svc"].handle(frame)

    assert env["gfs"].calls == []


async def test_household_space_no_relay(env):
    await env["make_space"]("sp-hh", SpaceType.HOUSEHOLD, with_seed=True)
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    frame = _new_subscriber_frame(
        "sp-hh", sub_iid, id_kp.public_key, kw_kp.public_key, keywrap_sig
    )

    await env["svc"].handle(frame)

    assert env["gfs"].calls == []


async def test_no_keywrap_key_no_relay(env):
    """A subscriber that shipped no key-wrap key (older HFS) is skipped (can't
    be sealed-to) — no relay, no crash."""
    await env["make_space"]("sp-older", SpaceType.PUBLIC, with_seed=True)
    id_kp = generate_identity_keypair()
    sub_iid = derive_instance_id(id_kp.public_key)
    frame = {
        "type": "new_subscriber",
        "space_id": "sp-older",
        "subscriber": {
            "instance_id": sub_iid,
            "identity_public_key": id_kp.public_key.hex(),
            "keywrap_public_key": "",
            "kem_suite": "",
            "keywrap_sig": "",
        },
    }

    await env["svc"].handle(frame)

    assert env["gfs"].calls == []


async def test_malformed_frame_no_crash(env):
    await env["make_space"]("sp-mal", SpaceType.PUBLIC, with_seed=True)
    await env["svc"].handle({"type": "new_subscriber"})  # no subscriber dict
    await env["svc"].handle({"type": "new_subscriber", "space_id": "sp-mal"})
    await env["svc"].handle(
        {"type": "new_subscriber", "space_id": "unknown-space", "subscriber": {}}
    )
    assert env["gfs"].calls == []


# ── Phase-5b-c reconcile (owner-offline subscriber-list pull) ──────────────


class _FakeConnRepo:
    """In-memory GFS-connection repo: a connection + its space publications."""

    def __init__(self) -> None:
        self._conns: dict[str, GfsConnection] = {}
        self._pubs: dict[str, list[GfsSpacePublication]] = {}

    def add_conn(self, gfs_id: str, *, status: str = "active") -> None:
        self._conns[gfs_id] = GfsConnection(
            id=gfs_id,
            gfs_instance_id=f"{gfs_id}.gfs",
            display_name=gfs_id,
            public_key="ee" * 32,
            inbox_url=f"https://{gfs_id}.example",
            status=status,
            paired_at="2026-06-10T00:00:00+00:00",
        )

    def publish(self, gfs_id: str, space_id: str) -> None:
        self._pubs.setdefault(gfs_id, []).append(
            GfsSpacePublication(
                space_id=space_id,
                gfs_connection_id=gfs_id,
                published_at="2026-06-10T00:00:00+00:00",
            )
        )

    async def get(self, gfs_id: str) -> GfsConnection | None:
        return self._conns.get(gfs_id)

    async def list_publications(self, gfs_id: str) -> list[GfsSpacePublication]:
        return list(self._pubs.get(gfs_id, []))

    async def list_publications_for_space(
        self, space_id: str
    ) -> list[GfsSpacePublication]:
        return [
            pub
            for pubs in self._pubs.values()
            for pub in pubs
            if pub.space_id == space_id
        ]


class _FakeResp:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)


class _FakeSession:
    """Captures GET calls + returns a programmed subscribers payload."""

    def __init__(self) -> None:
        self.gets: list[tuple[str, dict]] = []
        self.responses: dict[str, _FakeResp] = {}  # by URL path

    def program(self, url: str, status: int, payload: dict) -> None:
        self.responses[url] = _FakeResp(status, payload)

    def get(self, url, *, params=None, timeout=None):
        self.gets.append((url, dict(params or {})))
        return self.responses.get(url, _FakeResp(404, {"error": "not found"}))


@pytest.fixture
async def recon_env(env):
    """Extend ``env`` with a fake GFS-connection repo + HTTP session wired into
    the reconcile path."""
    conn_repo = _FakeConnRepo()
    session = _FakeSession()
    svc = env["svc"]
    svc.attach_reconcile_context(gfs_conn_repo=conn_repo, http_session=session)
    return {**env, "conn_repo": conn_repo, "session": session}


def _subscribers_payload(*subs):
    return {"subscribers": list(subs)}


async def test_reconcile_pulls_and_relays_per_subscriber(recon_env):
    skp = await recon_env["make_space"]("sp-rec", SpaceType.PUBLIC, with_seed=True)
    recon_env["conn_repo"].add_conn("gfs-1")
    recon_env["conn_repo"].publish("gfs-1", "sp-rec")

    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    url = "https://gfs-1.example/gfs/spaces/sp-rec/subscribers"
    recon_env["session"].program(
        url,
        200,
        _subscribers_payload(
            {
                "instance_id": sub_iid,
                "identity_public_key": id_kp.public_key.hex(),
                "keywrap_public_key": kw_kp.public_key.hex(),
                "keywrap_sig": keywrap_sig,
            }
        ),
    )

    await recon_env["svc"].reconcile("gfs-1")

    # The query was signed with the SPACE seed under the query event type.
    assert len(recon_env["session"].gets) == 1
    got_url, params = recon_env["session"].gets[0]
    assert got_url == url
    assert verify_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
        space_id="sp-rec",
        payload={"space_id": "sp-rec", "ts": params["ts"]},
        authority_sig=params["authority_sig"],
        authority_sig_suite=params["authority_sig_suite"],
        space_public_key=skp.public_key,
    )

    # A sealed handoff was relayed for the subscriber.
    assert len(recon_env["gfs"].calls) == 1
    call = recon_env["gfs"].calls[0]
    assert call["space_id"] == "sp-rec"
    # Reconcile goes through the same builder — no target on the wire here
    # either; the subscriber is identified only by what it can unseal.
    assert "target_instance_id" not in call["payload"]
    assert sub_iid not in json.dumps(call["payload"])
    # The subscriber can open the seal.
    epoch, raw_key = await recon_env["crypto"].export_current_key("sp-rec")
    pt = open_keywrap(
        sealed=call["payload"]["sealed"], recipient_keywrap_priv=kw_kp.private_key
    )
    meta = json.loads(pt)
    assert meta["space_content_key"]["epoch"] == epoch


async def test_reconcile_skips_space_without_seed(recon_env):
    """A space published to this GFS but whose seed this household does NOT
    hold is not reconciled (can't authority-sign the query)."""
    await recon_env["make_space"]("sp-noseed", SpaceType.PUBLIC, with_seed=False)
    recon_env["conn_repo"].add_conn("gfs-1")
    recon_env["conn_repo"].publish("gfs-1", "sp-noseed")

    await recon_env["svc"].reconcile("gfs-1")

    assert recon_env["session"].gets == []
    assert recon_env["gfs"].calls == []


async def test_reconcile_skips_private_space(recon_env):
    await recon_env["make_space"]("sp-priv", SpaceType.PRIVATE, with_seed=True)
    recon_env["conn_repo"].add_conn("gfs-1")
    recon_env["conn_repo"].publish("gfs-1", "sp-priv")

    await recon_env["svc"].reconcile("gfs-1")

    assert recon_env["session"].gets == []
    assert recon_env["gfs"].calls == []


async def test_reconcile_skips_household_space(recon_env):
    await recon_env["make_space"]("sp-hh", SpaceType.HOUSEHOLD, with_seed=True)
    recon_env["conn_repo"].add_conn("gfs-1")
    recon_env["conn_repo"].publish("gfs-1", "sp-hh")

    await recon_env["svc"].reconcile("gfs-1")

    assert recon_env["session"].gets == []
    assert recon_env["gfs"].calls == []


async def test_reconcile_skips_forged_keywrap_binding(recon_env):
    """A subscriber whose key-wrap self-signature doesn't match its identity is
    skipped — the content key is never sealed to a substituted key-wrap key."""
    await recon_env["make_space"]("sp-forge", SpaceType.PUBLIC, with_seed=True)
    recon_env["conn_repo"].add_conn("gfs-1")
    recon_env["conn_repo"].publish("gfs-1", "sp-forge")

    id_kp, _kw, sub_iid, _good = _subscriber_identity()
    attacker_kw = generate_x25519_keypair()
    attacker_id = generate_identity_keypair()
    forged_sig = b64url_encode(
        sign_ed25519(attacker_id.private_key, attacker_kw.public_key)
    )
    url = "https://gfs-1.example/gfs/spaces/sp-forge/subscribers"
    recon_env["session"].program(
        url,
        200,
        _subscribers_payload(
            {
                "instance_id": sub_iid,
                "identity_public_key": id_kp.public_key.hex(),
                "keywrap_public_key": attacker_kw.public_key.hex(),
                "keywrap_sig": forged_sig,
            }
        ),
    )

    await recon_env["svc"].reconcile("gfs-1")

    assert len(recon_env["session"].gets) == 1  # queried
    assert recon_env["gfs"].calls == []  # but nothing sealed/relayed


async def test_reconcile_is_idempotent(recon_env):
    """Re-running reconcile re-seals harmlessly (the subscriber's import is
    per-epoch idempotent; re-sealing just relays again)."""
    await recon_env["make_space"]("sp-idem", SpaceType.PUBLIC, with_seed=True)
    recon_env["conn_repo"].add_conn("gfs-1")
    recon_env["conn_repo"].publish("gfs-1", "sp-idem")

    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    url = "https://gfs-1.example/gfs/spaces/sp-idem/subscribers"
    recon_env["session"].program(
        url,
        200,
        _subscribers_payload(
            {
                "instance_id": sub_iid,
                "identity_public_key": id_kp.public_key.hex(),
                "keywrap_public_key": kw_kp.public_key.hex(),
                "keywrap_sig": keywrap_sig,
            }
        ),
    )

    await recon_env["svc"].reconcile("gfs-1")
    await recon_env["svc"].reconcile("gfs-1")

    assert len(recon_env["gfs"].calls) == 2  # re-sealed, no crash


async def test_reconcile_unknown_gfs_no_crash(recon_env):
    await recon_env["svc"].reconcile("nope")
    assert recon_env["session"].gets == []
    assert recon_env["gfs"].calls == []


# ── Rotation-triggered per-space reconcile (GFS subscribers after a kick) ──


def _program_one_subscriber(recon_env, space_id, gfs="gfs-1"):
    """Publish *space_id* on *gfs* and program a single valid subscriber.
    Returns ``(keywrap_keypair, url)``."""
    recon_env["conn_repo"].add_conn(gfs)
    recon_env["conn_repo"].publish(gfs, space_id)
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    url = f"https://{gfs}.example/gfs/spaces/{space_id}/subscribers"
    recon_env["session"].program(
        url,
        200,
        _subscribers_payload(
            {
                "instance_id": sub_iid,
                "identity_public_key": id_kp.public_key.hex(),
                "keywrap_public_key": kw_kp.public_key.hex(),
                "keywrap_sig": keywrap_sig,
            }
        ),
    )
    return kw_kp, url


async def test_reconcile_space_relays_rotated_epoch(recon_env):
    """After a content-key rotation (the forward-secrecy rekey a member
    removal triggers), a per-space reconcile re-seals the NEW epoch's key to
    the GFS subscriber — otherwise every relayed frame it receives from now on
    fails to decrypt until the next reconnect."""
    await recon_env["make_space"]("sp-rot", SpaceType.PUBLIC, with_seed=True)
    kw_kp, url = _program_one_subscriber(recon_env, "sp-rot")

    new_epoch = await recon_env["crypto"].rotate_epoch("sp-rot")

    await recon_env["svc"].reconcile_space("gfs-1", "sp-rot")

    assert [u for u, _ in recon_env["session"].gets] == [url]
    assert len(recon_env["gfs"].calls) == 1
    envelope = recon_env["gfs"].calls[0]["payload"]
    meta = json.loads(
        open_keywrap(
            recipient_keywrap_priv=kw_kp.private_key,
            sealed=envelope["sealed"],
        )
    )
    assert meta["space_content_key"]["epoch"] == new_epoch
    expected = await recon_env["crypto"].export_current_key("sp-rot")
    assert base64.b64decode(meta["space_content_key"]["key_base64"]) == expected[1]


async def test_reconcile_space_skips_unpublished_space(recon_env):
    """A space that isn't published to that GFS is never queried."""
    await recon_env["make_space"]("sp-unpub", SpaceType.PUBLIC, with_seed=True)
    recon_env["conn_repo"].add_conn("gfs-1")

    await recon_env["svc"].reconcile_space("gfs-1", "sp-unpub")

    assert recon_env["session"].gets == []
    assert recon_env["gfs"].calls == []


async def test_reconcile_space_skips_inactive_gfs(recon_env):
    await recon_env["make_space"]("sp-inact", SpaceType.PUBLIC, with_seed=True)
    recon_env["conn_repo"].add_conn("gfs-1", status="revoked")
    recon_env["conn_repo"].publish("gfs-1", "sp-inact")

    await recon_env["svc"].reconcile_space("gfs-1", "sp-inact")

    assert recon_env["session"].gets == []
    assert recon_env["gfs"].calls == []


async def test_reconcile_space_private_space_never_leaves(recon_env):
    """A private space's content key must NEVER travel via a GFS, even if a
    stale publication row exists."""
    await recon_env["make_space"]("sp-priv", SpaceType.PRIVATE, with_seed=True)
    _program_one_subscriber(recon_env, "sp-priv")

    await recon_env["svc"].reconcile_space("gfs-1", "sp-priv")

    assert recon_env["session"].gets == []
    assert recon_env["gfs"].calls == []


async def test_reconcile_space_without_seed_no_query(recon_env):
    """No seed → can't authority-sign the subscriber query → no query at all."""
    await recon_env["make_space"]("sp-noseed", SpaceType.PUBLIC, with_seed=False)
    _program_one_subscriber(recon_env, "sp-noseed")

    await recon_env["svc"].reconcile_space("gfs-1", "sp-noseed")

    assert recon_env["session"].gets == []
    assert recon_env["gfs"].calls == []


async def test_reconcile_space_everywhere_fans_to_each_published_gfs(recon_env):
    await recon_env["make_space"]("sp-multi", SpaceType.GLOBAL, with_seed=True)
    _program_one_subscriber(recon_env, "sp-multi", gfs="gfs-1")
    _program_one_subscriber(recon_env, "sp-multi", gfs="gfs-2")

    await recon_env["svc"].reconcile_space_everywhere("sp-multi")

    assert sorted(u for u, _ in recon_env["session"].gets) == [
        "https://gfs-1.example/gfs/spaces/sp-multi/subscribers",
        "https://gfs-2.example/gfs/spaces/sp-multi/subscribers",
    ]
    assert len(recon_env["gfs"].calls) == 2


async def test_reconcile_space_everywhere_unpublished_space_noop(recon_env):
    await recon_env["make_space"]("sp-lonely", SpaceType.PUBLIC, with_seed=True)

    await recon_env["svc"].reconcile_space_everywhere("sp-lonely")

    assert recon_env["session"].gets == []
    assert recon_env["gfs"].calls == []


async def test_reconcile_space_everywhere_is_fail_soft(recon_env):
    """An unreachable/raising GFS must not propagate out — the rotation that
    triggered it already succeeded."""
    await recon_env["make_space"]("sp-down", SpaceType.PUBLIC, with_seed=True)
    _program_one_subscriber(recon_env, "sp-down")

    def _boom(url, *, params=None, timeout=None):
        raise RuntimeError("gfs down")

    recon_env["session"].get = _boom

    await recon_env["svc"].reconcile_space_everywhere("sp-down")
    await recon_env["svc"].reconcile_space("gfs-1", "sp-down")

    assert recon_env["gfs"].calls == []


# ─── invite_only: listed for discovery, never publicly readable ────────


async def test_invite_only_space_seals_no_key_on_new_subscriber(env):
    """A GLOBAL space whose ``join_mode`` is ``invite_only`` is listed in the
    GFS directory (so people can discover it and ask for an invite) but is NOT
    publicly readable: the content key is never sealed to a GFS subscriber, so
    a subscriber holds only ciphertext it can never open."""
    await env["make_space"](
        "sp-inv",
        SpaceType.GLOBAL,
        with_seed=True,
        join_mode=JoinMode.INVITE_ONLY,
    )
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    frame = _new_subscriber_frame(
        "sp-inv", sub_iid, id_kp.public_key, kw_kp.public_key, keywrap_sig
    )

    await env["svc"].handle(frame)

    assert env["gfs"].calls == []


async def test_request_join_mode_space_seals_no_key(env):
    """``request`` is gated too: a person must be admitted before they get
    anything, so the content key is never sealed to a mere subscriber. Only
    ``open`` is publicly readable."""
    await env["make_space"](
        "sp-req",
        SpaceType.GLOBAL,
        with_seed=True,
        join_mode=JoinMode.REQUEST,
    )
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    frame = _new_subscriber_frame(
        "sp-req", sub_iid, id_kp.public_key, kw_kp.public_key, keywrap_sig
    )

    await env["svc"].handle(frame)

    assert env["gfs"].calls == []


async def test_reconcile_skips_invite_only_space(recon_env):
    """The reconcile path is gated too — and *before* the subscriber-list HTTP
    round-trip, so an invite-only space never even asks the GFS who subscribed
    to it."""
    await recon_env["make_space"](
        "sp-inv",
        SpaceType.GLOBAL,
        with_seed=True,
        join_mode=JoinMode.INVITE_ONLY,
    )
    recon_env["conn_repo"].add_conn("gfs-1")
    recon_env["conn_repo"].publish("gfs-1", "sp-inv")

    await recon_env["svc"].reconcile("gfs-1")

    assert recon_env["session"].gets == []
    assert recon_env["gfs"].calls == []


async def test_reconcile_space_everywhere_skips_invite_only_space(recon_env):
    """The post-rotation re-seal (``reconcile_space_everywhere``) funnels
    through the same gate, so a key rotation never leaks the new epoch into an
    invite-only space's subscriber set."""
    await recon_env["make_space"](
        "sp-inv",
        SpaceType.GLOBAL,
        with_seed=True,
        join_mode=JoinMode.INVITE_ONLY,
    )
    recon_env["conn_repo"].add_conn("gfs-1")
    recon_env["conn_repo"].publish("gfs-1", "sp-inv")

    await recon_env["svc"].reconcile_space_everywhere("sp-inv")

    assert recon_env["session"].gets == []
    assert recon_env["gfs"].calls == []
