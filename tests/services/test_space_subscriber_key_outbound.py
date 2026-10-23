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
from collections import deque
from datetime import UTC, datetime

import orjson
import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE,
    AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
    AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
    strip_authority_sig_fields,
    verify_authority_event,
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
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceType,
)
from socialhome.domain.space_author_profile import PROFILE_PICTURE_FIELD
from socialhome.domain.space_author_profile_bundle import BundleEntry
from socialhome.domain.space_item import ITEM_SIZE_BUCKETS
from socialhome.federation.keywrap_seal import open_keywrap
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services import space_subscriber_key_outbound as mod
from socialhome.services.space_crypto_service import SpaceContentEncryption
from socialhome.services.space_subscriber_key_outbound import (
    SpaceSubscriberKeyOutbound,
)


class _CaptureGfs:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        #: ``publish_space_event_to`` calls (v_61 targeted delivery).
        self.targeted: list[dict] = []
        #: What a targeted publish answers: ``None`` = the server never
        #: proved ``targeted_publish`` (an older GFS).
        self.targeted_answer: bool | None = None

    async def publish_space_event_to(
        self, gfs_id, *, space_id, event_type, payload, to_instance_id
    ) -> bool | None:
        if self.targeted_answer is None:
            return None
        self.targeted.append(
            {
                "gfs_id": gfs_id,
                "space_id": space_id,
                "event_type": event_type,
                "payload": payload,
                "to": to_instance_id,
            }
        )
        return self.targeted_answer

    async def publish_space_event(
        self, *, space_id, event_type, payload, skip=None, compressed_payload=None
    ) -> int:
        self.calls.append(
            {
                "space_id": space_id,
                "event_type": event_type,
                "payload": payload,
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


def _new_subscriber_frame(
    space_id,
    instance_id,
    id_pub,
    kw_pub,
    keywrap_sig,
    *,
    reason=None,
    accepts=None,
):
    frame = {
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
    if reason is not None:
        frame["reason"] = reason
    if accepts is not None:
        frame["subscriber"]["accepts"] = accepts
    return frame


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

    async def _make_space(
        space_id,
        stype,
        *,
        with_seed,
        join_mode=JoinMode.OPEN,
        allow_subscribers=True,
    ):
        skp = generate_space_keypair()
        await space_repo.save(
            Space(
                id=space_id,
                name="S",
                owner_instance_id=own_iid,
                owner_username="alice",
                identity_public_key=skp.public_key.hex(),
                config_sequence=0,
                features=SpaceFeatures(allow_subscribers=allow_subscribers),
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
    yield {
        "db": db,
        "gfs": gfs,
        "crypto": crypto,
        "space_repo": space_repo,
        "make_space": _make_space,
        "svc": svc,
    }
    await db.shutdown()


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

    def get(self, url, *, params=None, timeout=None, allow_redirects=True):
        assert allow_redirects is False, "GFS calls must not follow redirects"
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
    epoch, _raw_key = await recon_env["crypto"].export_current_key("sp-rec")
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


def _sub_row(id_kp, kw_kp, sub_iid, keywrap_sig) -> dict:
    return {
        "instance_id": sub_iid,
        "identity_public_key": id_kp.public_key.hex(),
        "keywrap_public_key": kw_kp.public_key.hex(),
        "keywrap_sig": keywrap_sig,
    }


async def test_a_reconnect_reseals_only_to_subscribers_not_yet_sealed(recon_env):
    """#5a: every GFS-socket reconnect of a seed-holder re-sealed the key to
    EVERY subscriber of every space it published — one handoff each, ~140k
    a day. A subscriber this household already sealed the current epoch to
    is now skipped; a new one still gets it. (A follower that was offline
    when its seal went out is repaired by its own reconnect notify, which
    always re-seals.)"""
    await recon_env["make_space"]("sp-idem", SpaceType.PUBLIC, with_seed=True)
    recon_env["conn_repo"].add_conn("gfs-1")
    recon_env["conn_repo"].publish("gfs-1", "sp-idem")
    first = _sub_row(*_subscriber_identity())
    url = "https://gfs-1.example/gfs/spaces/sp-idem/subscribers"
    recon_env["session"].program(url, 200, _subscribers_payload(first))

    await recon_env["svc"].reconcile("gfs-1")
    assert len(recon_env["gfs"].calls) == 1
    await recon_env["svc"].reconcile("gfs-1")
    assert len(recon_env["gfs"].calls) == 1  # was 2: nothing new to seal

    second = _sub_row(*_subscriber_identity())
    recon_env["session"].program(url, 200, _subscribers_payload(first, second))
    await recon_env["svc"].reconcile("gfs-1")
    assert len(recon_env["gfs"].calls) == 2  # only the new subscriber


async def test_a_rotated_key_is_sealed_to_every_subscriber_again(recon_env):
    await recon_env["make_space"]("sp-rot2", SpaceType.PUBLIC, with_seed=True)
    _kw, _url = _program_one_subscriber(recon_env, "sp-rot2")
    await recon_env["svc"].reconcile("gfs-1")
    await recon_env["crypto"].rotate_epoch("sp-rot2")
    await recon_env["svc"].reconcile_space_everywhere("sp-rot2")
    assert len(recon_env["gfs"].calls) == 2


async def test_a_new_keywrap_key_is_sealed_to_again(recon_env):
    """Keyed on the key-wrap key too: a follower that re-keyed (a fresh
    install under the same identity) gets the seal again."""
    await recon_env["make_space"]("sp-kw", SpaceType.PUBLIC, with_seed=True)
    recon_env["conn_repo"].add_conn("gfs-1")
    recon_env["conn_repo"].publish("gfs-1", "sp-kw")
    id_kp, _kw, sub_iid, _sig = _subscriber_identity()
    url = "https://gfs-1.example/gfs/spaces/sp-kw/subscribers"
    for _ in range(2):
        kw_kp = generate_x25519_keypair()
        sig = b64url_encode(sign_ed25519(id_kp.private_key, kw_kp.public_key))
        recon_env["session"].program(
            url, 200, _subscribers_payload(_sub_row(id_kp, kw_kp, sub_iid, sig))
        )
        await recon_env["svc"].reconcile("gfs-1")
    assert len(recon_env["gfs"].calls) == 2


async def test_a_handoff_the_server_refused_is_sealed_again(recon_env):
    """Only a relayed seal counts: a targeted publish the GFS refused is
    retried on the next reconnect."""
    await recon_env["make_space"]("sp-ref", SpaceType.PUBLIC, with_seed=True)
    _program_one_subscriber(recon_env, "sp-ref")
    recon_env["gfs"].targeted_answer = False
    await recon_env["svc"].reconcile("gfs-1")
    recon_env["gfs"].targeted_answer = True
    await recon_env["svc"].reconcile("gfs-1")
    await recon_env["svc"].reconcile("gfs-1")
    assert len(recon_env["gfs"].targeted) == 2  # refused, then sealed once


async def test_a_new_subscriber_notify_spares_the_next_reconcile(recon_env):
    await recon_env["make_space"]("sp-nf", SpaceType.PUBLIC, with_seed=True)
    recon_env["conn_repo"].add_conn("gfs-1")
    recon_env["conn_repo"].publish("gfs-1", "sp-nf")
    id_kp, kw_kp, sub_iid, sig = _subscriber_identity()
    await recon_env["svc"].handle(
        _new_subscriber_frame("sp-nf", sub_iid, id_kp.public_key, kw_kp.public_key, sig)
    )
    recon_env["session"].program(
        "https://gfs-1.example/gfs/spaces/sp-nf/subscribers",
        200,
        _subscribers_payload(_sub_row(id_kp, kw_kp, sub_iid, sig)),
    )
    await recon_env["svc"].reconcile("gfs-1")
    assert len(recon_env["gfs"].calls) == 1


async def test_a_followers_own_reconnect_notify_always_reseals(recon_env):
    """The follower's socket may have been down when its seal went out —
    delivery is socket-only — so its reconnect notify re-seals even though
    this household sealed it already."""
    await recon_env["make_space"]("sp-rc", SpaceType.PUBLIC, with_seed=True)
    id_kp, kw_kp, sub_iid, sig = _subscriber_identity()
    for reason in ("subscribe", "reconnect"):
        await recon_env["svc"].handle(
            _new_subscriber_frame(
                "sp-rc", sub_iid, id_kp.public_key, kw_kp.public_key, sig, reason=reason
            )
        )
    assert len(recon_env["gfs"].calls) == 2


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


# ─── allow_subscribers OFF: listed for discovery, never publicly readable ──


async def test_space_without_subscribers_seals_no_key_on_new_subscriber(env):
    """A GLOBAL space whose owner has NOT opted into subscribers is listed in
    the GFS directory (so people can discover it and ask for an invite) but is
    NOT publicly readable: the content key is never sealed to a GFS
    subscriber, so a subscriber holds only ciphertext it can never open."""
    await env["make_space"](
        "sp-inv",
        SpaceType.GLOBAL,
        with_seed=True,
        allow_subscribers=False,
    )
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    frame = _new_subscriber_frame(
        "sp-inv", sub_iid, id_kp.public_key, kw_kp.public_key, keywrap_sig
    )

    await env["svc"].handle(frame)

    assert env["gfs"].calls == []


async def test_invite_only_space_with_subscribers_on_still_seals_the_key(env):
    """The join mode has no say: ``invite_only`` + subscribers-ON is a
    broadcast space, and a follower of one DOES get the content key."""
    await env["make_space"](
        "sp-broadcast",
        SpaceType.GLOBAL,
        with_seed=True,
        join_mode=JoinMode.INVITE_ONLY,
        allow_subscribers=True,
    )
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    frame = _new_subscriber_frame(
        "sp-broadcast", sub_iid, id_kp.public_key, kw_kp.public_key, keywrap_sig
    )

    await env["svc"].handle(frame)

    assert len(env["gfs"].calls) == 1


async def test_open_to_join_space_with_subscribers_off_seals_no_key(env):
    """…and the mirror image: ``open`` means anyone may JOIN, not that anyone
    may READ."""
    await env["make_space"](
        "sp-open-private",
        SpaceType.GLOBAL,
        with_seed=True,
        join_mode=JoinMode.OPEN,
        allow_subscribers=False,
    )
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    frame = _new_subscriber_frame(
        "sp-open-private", sub_iid, id_kp.public_key, kw_kp.public_key, keywrap_sig
    )

    await env["svc"].handle(frame)

    assert env["gfs"].calls == []


async def test_reconcile_skips_space_without_subscribers(recon_env):
    """The reconcile path is gated too — and *before* the subscriber-list HTTP
    round-trip, so such a space never even asks the GFS who subscribed to
    it."""
    await recon_env["make_space"](
        "sp-inv",
        SpaceType.GLOBAL,
        with_seed=True,
        allow_subscribers=False,
    )
    recon_env["conn_repo"].add_conn("gfs-1")
    recon_env["conn_repo"].publish("gfs-1", "sp-inv")

    await recon_env["svc"].reconcile("gfs-1")

    assert recon_env["session"].gets == []
    assert recon_env["gfs"].calls == []


async def test_reconcile_space_everywhere_skips_space_without_subscribers(recon_env):
    """The post-rotation re-seal (``reconcile_space_everywhere``) funnels
    through the same gate, so a key rotation never leaks the new epoch into
    the subscriber set of a space nobody may read."""
    await recon_env["make_space"](
        "sp-inv",
        SpaceType.GLOBAL,
        with_seed=True,
        allow_subscribers=False,
    )
    recon_env["conn_repo"].add_conn("gfs-1")
    recon_env["conn_repo"].publish("gfs-1", "sp-inv")

    await recon_env["svc"].reconcile_space_everywhere("sp-inv")

    assert recon_env["session"].gets == []
    assert recon_env["gfs"].calls == []


class _Profiles:
    """The host's author-profile cache (only what the bundle reads)."""

    def __init__(self, items, *, epoch: int = 1, roster=None) -> None:
        self.items = items
        #: Authors seated in the space's roster by :func:`_seat_authors`.
        self.roster = roster
        self.epoch = epoch
        self.asked: list[tuple] = []

    async def list_signed_items(self, space_id, *, limit):
        self.asked.append((space_id, limit))
        return [
            BundleEntry(
                epoch=self.epoch,
                item=item,
                picture_hash=f"{i:016x}" if PROFILE_PICTURE_FIELD in item else None,
            )
            for i, item in enumerate(self.items)
        ]


def _signed_profile(
    name: str, *, origin: str = "alpha.home", picture: str | None = None
) -> dict:
    # Opaque to the host: it forwards the author-signed item unchanged.
    item = {
        "item_type": "author_profile",
        "author_user_id": f"uid-{name}",
        "origin_instance_id": origin,
        "content": json.dumps({"display_name": name, "bio": f"{name}'s bio"}),
        "author_sig": "sig-" + name,
    }
    if picture is not None:
        item[PROFILE_PICTURE_FIELD] = picture
    return item


def _opened_parts(env, kw_kp) -> list[dict]:
    return [
        json.loads(
            open_keywrap(
                sealed=c["payload"]["sealed"], recipient_keywrap_priv=kw_kp.private_key
            )
        )
        for c in env["gfs"].targeted
        if c["event_type"] == AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE
    ]


_OPENS_BUNDLE = ["author_profiles_bundle"]


async def _seat_authors(env, space_id: str) -> None:
    profiles = getattr(env["svc"], "_profiles", None)
    if not isinstance(profiles, _Profiles) or profiles.roster is not None:
        return
    for item in profiles.items:
        author = str(item.get("author_user_id") or "")
        if author and await env["space_repo"].get(space_id) is not None:
            await env["space_repo"].save_member(
                SpaceMember(
                    space_id=space_id,
                    user_id=author,
                    role="member",
                    joined_at="2026-10-20T00:00:00+00:00",
                )
            )


async def _subscribe(
    env,
    space_id="sp-pub",
    *,
    gfs_id="gfs-1",
    reason="subscribe",
    accepts=_OPENS_BUNDLE,
):
    """A ``new_subscriber`` for a NEW seat (v_61 shape) by default. The
    authors of the attached profile cache are seated in the space's roster
    first (the host bundles current writers only) unless the test seats
    its own (``_Profiles(roster=[])``)."""
    await _seat_authors(env, space_id)
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    await env["svc"].handle(
        _new_subscriber_frame(
            space_id,
            sub_iid,
            id_kp.public_key,
            kw_kp.public_key,
            keywrap_sig,
            reason=reason,
            accepts=accepts,
        ),
        gfs_id=gfs_id,
    )
    return kw_kp, sub_iid


def _kinds(env) -> list[str]:
    return [c["event_type"] for c in env["gfs"].targeted]


async def test_a_target_capable_gfs_gets_the_handoff_for_that_follower_only(env):
    """v_61: the handoff goes to the one subscriber the GFS named — no
    fan-out to every other subscriber of the space."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    env["gfs"].targeted_answer = True
    kw_kp, sub_iid = await _subscribe(env)
    assert env["gfs"].calls == []  # nothing fanned out
    [handoff] = env["gfs"].targeted
    assert handoff["event_type"] == AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF
    assert (handoff["gfs_id"], handoff["to"]) == ("gfs-1", sub_iid)
    # Still identity-free inside: the routing target is outside the payload.
    assert sub_iid not in json.dumps(handoff["payload"])
    assert json.loads(
        open_keywrap(
            sealed=handoff["payload"]["sealed"],
            recipient_keywrap_priv=kw_kp.private_key,
        )
    )["space_content_key"]


async def test_a_new_follower_gets_one_sealed_bundle_of_the_cached_profiles(env):
    skp = await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    env["gfs"].targeted_answer = True
    profiles = _Profiles([_signed_profile("Erin"), _signed_profile("Noor")])
    env["svc"].attach_author_profiles(profiles)  # type: ignore[arg-type]
    kw_kp, sub_iid = await _subscribe(env)
    _handoff, bundle = env["gfs"].targeted
    assert bundle["event_type"] == AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE
    assert bundle["to"] == sub_iid and bundle["gfs_id"] == "gfs-1"
    envelope = bundle["payload"]
    # Content-blind on the wire: no name, no bio, no item, no target outside
    # the sealed blob (it opens only with the follower's key-wrap key, below).
    # The blob's random base64 is left out of the substring check: it can
    # contain "bio" by chance.
    clear_fields = {k: v for k, v in envelope.items() if k != "sealed"}
    wire = json.dumps(clear_fields)
    for clear in ("Erin", "Noor", "bio", "author_sig", sub_iid):
        assert clear not in wire
    assert verify_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE,
        space_id="sp-pub",
        payload=strip_authority_sig_fields(envelope),
        authority_sig=envelope["authority_sig"],
        authority_sig_suite=envelope["authority_sig_suite"],
        space_public_key=skp.public_key,
    )
    opened = json.loads(
        open_keywrap(
            sealed=envelope["sealed"], recipient_keywrap_priv=kw_kp.private_key
        )
    )
    assert opened["space_id"] == "sp-pub" and opened["part"] == 0
    # Forwarded author-signed and unchanged, each under the epoch it was
    # published at (H2: a rotation never forces a republish).
    assert opened["items"] == [{"epoch": 1, "item": i} for i in profiles.items]
    assert profiles.asked == [("sp-pub", 200)]


async def test_an_older_gfs_gets_the_fanned_out_handoff_and_no_bundle(env):
    """Without a proven ``targeted_publish`` a bundle would reach every
    subscriber — so none is sent; names follow each author's next post."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    profiles = _Profiles([_signed_profile("Erin")])
    env["svc"].attach_author_profiles(profiles)  # type: ignore[arg-type]
    await _subscribe(env)
    [call] = env["gfs"].calls
    assert call["event_type"] == AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF
    assert env["gfs"].targeted == [] and profiles.asked == []


async def test_a_restarted_host_sends_reconnecting_followers_no_bundle(env):
    """REGRESSION (H1): after a host restart (a FRESH service: any in-memory
    dedupe is gone) every follower's reconnect notify re-seals the key
    alone — only the NEW seat (``reason: "subscribe"``) ever earned a
    bundle. On main the restarted host re-sent every follower its bundle."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    env["gfs"].targeted_answer = True
    profiles = _Profiles([_signed_profile("Erin")])
    env["svc"].attach_author_profiles(profiles)  # type: ignore[arg-type]
    await _seat_authors(env, "sp-pub")
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    keys = (sub_iid, id_kp.public_key, kw_kp.public_key, keywrap_sig)
    await env["svc"].handle(
        _new_subscriber_frame(
            "sp-pub", *keys, reason="subscribe", accepts=_OPENS_BUNDLE
        ),
        gfs_id="gfs-1",
    )
    restarted = SpaceSubscriberKeyOutbound(
        space_repo=env["space_repo"],
        space_crypto=env["crypto"],
        gfs_service=env["gfs"],
    )
    restarted.attach_identity(own_instance_id="alpha.home")
    restarted.attach_author_profiles(profiles)  # type: ignore[arg-type]
    for _ in range(3):
        await restarted.handle(
            _new_subscriber_frame(
                "sp-pub", *keys, reason="reconnect", accepts=_OPENS_BUNDLE
            ),
            gfs_id="gfs-1",
        )
    assert _kinds(env) == [
        AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
        AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE,
        AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
        AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
        AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
    ]


async def test_one_followers_seat_churn_earns_one_bundle_an_hour(env, monkeypatch):
    """REGRESSION (review I1): unsubscribe → subscribe makes a NEW seat each
    time; one follower looping must get one bundle per hour, not eat the
    space's backstop and starve real newcomers."""
    clock = [1000.0]
    monkeypatch.setattr(
        "socialhome.services.space_subscriber_key_outbound._clock",
        lambda: clock[0],
    )
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    env["gfs"].targeted_answer = True
    env["svc"].attach_author_profiles(_Profiles([_signed_profile("Erin")]))  # type: ignore[arg-type]
    await _seat_authors(env, "sp-pub")
    id_kp, kw_kp, sub_iid, keywrap_sig = _subscriber_identity()
    frame = _new_subscriber_frame(
        "sp-pub",
        sub_iid,
        id_kp.public_key,
        kw_kp.public_key,
        keywrap_sig,
        reason="subscribe",
        accepts=_OPENS_BUNDLE,
    )
    for _ in range(5):
        await env["svc"].handle(frame, gfs_id="gfs-1")
    await _subscribe(env)  # a real newcomer still gets one
    assert _kinds(env).count(AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE) == 2
    assert _kinds(env).count(AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF) == 6
    clock[0] += 3601.0
    await env["svc"].handle(frame, gfs_id="gfs-1")
    assert _kinds(env).count(AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE) == 3


@pytest.mark.parametrize("reason", ["reconnect", None, "", "resubscribe", 7])
async def test_a_notify_without_the_subscribe_reason_sends_no_bundle(env, reason):
    """A ``reconnect`` notify — even the first one a freshly restarted host
    sees for that follower — and, fail-safe, a frame with no (or an unknown)
    ``reason`` get the key handoff only, never a bundle."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    env["gfs"].targeted_answer = True
    profiles = _Profiles([_signed_profile("Erin")])
    env["svc"].attach_author_profiles(profiles)  # type: ignore[arg-type]
    await _subscribe(env, reason=reason)
    assert _kinds(env) == [AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF]
    assert profiles.asked == []


@pytest.mark.parametrize("accepts", [None, [], ["something_else"], "x", [None]])
async def test_a_follower_that_cannot_open_a_bundle_is_sent_none(env, accepts):
    """REGRESSION (Low 2): an older follower drops the unknown relay type, so
    a ≤ 1 MB bundle to it is wasted — it is only sent to a follower whose
    subscribe listed ``author_profiles_bundle``."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    env["gfs"].targeted_answer = True
    profiles = _Profiles([_signed_profile("Erin")])
    env["svc"].attach_author_profiles(profiles)  # type: ignore[arg-type]
    await _subscribe(env, accepts=accepts)
    assert _kinds(env) == [AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF]
    assert profiles.asked == []


async def test_a_refused_targeted_handoff_sends_no_bundle(env):
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    env["gfs"].targeted_answer = False
    profiles = _Profiles([_signed_profile("Erin")])
    env["svc"].attach_author_profiles(profiles)  # type: ignore[arg-type]
    await _subscribe(env)
    assert [c["event_type"] for c in env["gfs"].targeted] == [
        AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF
    ]
    assert env["gfs"].calls == [] and profiles.asked == []


async def test_an_empty_cache_sends_no_bundle(env):
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    env["gfs"].targeted_answer = True
    env["svc"].attach_author_profiles(_Profiles([]))  # type: ignore[arg-type]
    await _subscribe(env)
    assert [c["event_type"] for c in env["gfs"].targeted] == [
        AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF
    ]


async def test_names_come_first_and_pictures_follow_in_padded_parts(env):
    """M1: part 0 carries every name with no picture bytes; the pictures
    follow in later parts, every part padded to a size bucket."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    env["gfs"].targeted_answer = True
    items = [_signed_profile(f"A{i}", picture="P" * 30_000) for i in range(6)]
    env["svc"].attach_author_profiles(_Profiles(items))  # type: ignore[arg-type]
    kw_kp, sub_iid = await _subscribe(env)
    parts = _opened_parts(env, kw_kp)
    assert len(parts) > 2
    assert all(c["to"] == sub_iid for c in env["gfs"].targeted)
    names, *pictures = parts
    assert [x["item"]["author_user_id"] for x in names["items"]] == [
        f"uid-A{i}" for i in range(6)
    ]
    assert all(PROFILE_PICTURE_FIELD not in x["item"] for x in names["items"])
    assert [p["author"] for part in pictures for p in part["pictures"]] == [
        f"uid-A{i}" for i in range(6)
    ]
    for part in parts:
        size = len(orjson.dumps(part))
        assert size in ITEM_SIZE_BUCKETS


async def test_a_flood_of_new_followers_gets_names_but_not_pictures(env, monkeypatch):
    """M1: anyone can register and subscribe, and a GFS can name any
    household. The host spends at most its hourly byte budget per space.
    Pictures stop first, so newcomers still get every name; then even the
    names stop until the hour frees up."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    env["gfs"].targeted_answer = True
    items = [_signed_profile(f"A{i}", picture="P" * 30_000) for i in range(4)]
    env["svc"].attach_author_profiles(_Profiles(items))  # type: ignore[arg-type]
    kw_kp, _sub = await _subscribe(env)
    first = _opened_parts(env, kw_kp)
    sizes = [size for _at, size in env["svc"]._bundle_bytes["sp-pub"]]
    names, pictures = sizes[0], sum(sizes[1:])
    assert len(first) > 1 and all(p["pictures"] for p in first[1:])
    # Room for this first bundle's pictures only, and two more name parts.
    monkeypatch.setattr(mod, "PICTURE_BYTES_PER_SPACE_PER_HOUR", names + pictures)
    monkeypatch.setattr(mod, "BUNDLE_BYTES_PER_SPACE_PER_HOUR", names * 3 + pictures)
    seen: list[list[dict]] = []
    for _ in range(3):
        env["gfs"].targeted.clear()
        kw_kp, _sub = await _subscribe(env)
        seen.append(_opened_parts(env, kw_kp))
    assert [len(parts) for parts in seen] == [1, 1, 0]
    assert all(parts[0]["items"] and not parts[0]["pictures"] for parts in seen[:2])
    assert _kinds(env).count(AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF) == 1


async def test_the_hourly_budget_frees_up(env, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(mod, "_clock", lambda: clock[0])
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    env["gfs"].targeted_answer = True
    env["svc"].attach_author_profiles(_Profiles([_signed_profile("Erin")]))  # type: ignore[arg-type]
    await _subscribe(env)
    [(_at, size)] = env["svc"]._bundle_bytes["sp-pub"]
    monkeypatch.setattr(mod, "BUNDLE_BYTES_PER_SPACE_PER_HOUR", size)
    await _subscribe(env)  # over budget: key only
    clock[0] += 3601.0
    await _subscribe(env)
    assert _kinds(env).count(AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE) == 2


async def test_the_tracked_spaces_and_followers_are_bounded(env, monkeypatch):
    """Review M3: both maps are LRU-bounded with O(1) eviction — the least
    recently bundled entry goes first."""
    monkeypatch.setattr(mod, "_MAX_TRACKED_SPACES", 1)
    monkeypatch.setattr(mod, "_MAX_TRACKED_FOLLOWERS", 2)
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    env["gfs"].targeted_answer = True
    env["svc"].attach_author_profiles(_Profiles([_signed_profile("Erin")]))  # type: ignore[arg-type]
    env["svc"]._bundle_bytes["sp-old"] = deque([(1e18, 1)])
    for _ in range(3):
        await _subscribe(env)
    assert list(env["svc"]._bundle_bytes) == ["sp-pub"]
    assert len(env["svc"]._bundle_bytes["sp-pub"]) == 3
    assert len(env["svc"]._bundled) == 2


async def test_a_removed_author_of_a_member_household_is_not_bundled(env):
    """REGRESSION (review I1): the household filter kept a removed user
    whose household still has other members. Only the space's current
    roster (writers, not followers) is bundled."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    env["gfs"].targeted_answer = True
    await env["space_repo"].save_member(
        SpaceMember(
            space_id="sp-pub",
            user_id="uid-Here",
            role="member",
            joined_at="2026-10-20T00:00:00+00:00",
        )
    )
    items = [_signed_profile("Here"), _signed_profile("Removed")]
    env["svc"].attach_author_profiles(_Profiles(items, roster=[]))  # type: ignore[arg-type]
    kw_kp, _sub = await _subscribe(env)
    [names] = _opened_parts(env, kw_kp)
    assert [x["item"]["author_user_id"] for x in names["items"]] == ["uid-Here"]


async def test_a_bundle_carries_a_signed_issue_time(env):
    """Review minor: the authority-signed envelope binds when it was made,
    so a GFS cannot replay an old bundle to a follower forever."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    env["gfs"].targeted_answer = True
    env["svc"].attach_author_profiles(_Profiles([_signed_profile("Erin")]))  # type: ignore[arg-type]
    await _subscribe(env)
    [bundle] = [
        c
        for c in env["gfs"].targeted
        if c["event_type"] == AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE
    ]
    issued = datetime.fromisoformat(bundle["payload"]["issued_at"])
    assert abs((datetime.now(UTC) - issued).total_seconds()) < 60
    assert "issued_at" in strip_authority_sig_fields(bundle["payload"])
